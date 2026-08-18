"""
FF1 PSP Archipelago runtime client.

Built on Archipelago's CommonContext, so it gets the standard AP client WINDOW
(received/sent item log, server-connection box, /commands) like Text Client and
SNIClient. On top of that it runs a PPSSPP debugger bridge that grants received
items into game RAM and reports chest opens as location checks.

Spawned by the Archipelago Launcher via ff1psp.__init__.run_client. The player
connects to their server/slot in this window FIRST; PPSSPP is then auto-launched
on the FF1 ISO after the slot connects (see launcher.ensure_ppsspp).

Status: item/weapon/armor/gil/key-item grants work; location detection is
poll-based (chest bitfield; no breakpoints, so JIT block-linking stays enabled).
Chaos goal detection is automatic (flags loop polls the Chaos-defeated bit
0x11520 b6); `/goal` remains as a manual fallback.
"""

import asyncio
import atexit
import contextlib
import logging
import os
import re
import struct
import threading
import time
import traceback
import zipimport
from typing import Optional

import Utils
from CommonClient import (CommonContext, server_loop, gui_enabled,
                          ClientCommandProcessor, logger)
from NetUtils import ClientStatus

from . import ff1_data as D
from . import boot_patch as BP
from . import battle_font as BFONT
from . import grant_policy as GP
from . import name_banks as NB
from . import tome_names as TN
from . import iso_patcher as IP
# Imported EAGERLY on purpose: the watcher's whole job is to survive the zip
# being swapped, so it must already be in memory before that can happen.
from . import apworld_watch as APWATCH
from .monster_names import MONSTER_NAME as MON_NAME
from .ppsspp_ws import PPSSPP, USER_RAM_BASE, USER_RAM_SIZE
from .. import data as DATA
from .. import hints as HINTS
from .. import ids as ID
from .. import logic as LOGIC
from .. import rando as RANDO
from .. import tracker as TRACKER


async def _clear_breakpoints_fresh():
    """Open a SHORT-LIVED debugger connection, drop our chest breakpoints and unhalt
    the CPU. Used as a last-resort cleanup (atexit) so a client that died/crashed
    cannot leave PPSSPP halted on a leftover breakpoint -> hard game freeze. Every
    call is time-boxed so a wedged PPSSPP can't hang interpreter shutdown.
    local_only/scan=False: the discover() fallback is a sync 5s web request and
    the netstat port scan a sync ~0.5s one; wait_for CANNOT cancel sync calls
    (they block the loop thread) -- never stall exit on them."""
    psp = await asyncio.wait_for(PPSSPP.connect(local_only=True, scan=False), 3)
    try:
        for a in (D.CHEST_ITEM_CALL, D.CHEST_GIL_CALL):
            try:
                await asyncio.wait_for(psp.rpc("cpu.breakpoint.remove", address=a), 1)
            except Exception:
                pass
        for _ in range(20):
            try:
                await asyncio.wait_for(psp.rpc("cpu.resume"), 1)
                s = await asyncio.wait_for(psp.rpc("cpu.status"), 1)
            except Exception:
                break
            if not s.get("stepping"):
                break
            await asyncio.sleep(0.03)
    finally:
        await psp.close()


def _atexit_clear_breakpoints():
    """atexit hook: best-effort breakpoint teardown on interpreter exit (covers an
    uncaught-exception crash of the client, where shutdown() never ran). Time-boxed
    via _clear_breakpoints_fresh; swallows everything so exit is never blocked."""
    try:
        asyncio.run(asyncio.wait_for(_clear_breakpoints_fresh(), 6))
    except Exception:
        pass


# --- close watchdog ---------------------------------------------------------
# Closing the window sets ctx.exit_event on the KIVY thread; everything after
# that (server disconnect, debugger socket close, task cancellation, the event
# loop's own executor drain) runs on the asyncio thread. Any ONE of those that
# blocks -- a sync call sitting in the default executor (tasklist/netstat/ISO
# bake), a socket close against a wedged PPSSPP, a task that ignores cancel --
# leaves the process alive with a dead window: "Not Responding", never closes.
# So: from the moment the user asks to close, a plain daemon thread (no asyncio,
# nothing to wedge it) counts down and hard-exits if the orderly path hasn't
# finished. Armed on the GUI close path AND at the top of shutdown().
_EXIT_WATCHDOG_ARMED = threading.Event()
EXIT_WATCHDOG_SECONDS = 10.0


def arm_exit_watchdog(seconds: float = EXIT_WATCHDOG_SECONDS) -> None:
    """Guarantee the process dies within `seconds` of the user asking to close.
    Idempotent: the first arm wins. Safe from any thread."""
    if _EXIT_WATCHDOG_ARMED.is_set():
        return
    _EXIT_WATCHDOG_ARMED.set()

    def _kill():
        time.sleep(seconds)
        try:
            logger.warning(
                f"Orderly shutdown did not finish in {seconds:.0f}s -- forcing "
                f"exit (a debugger socket or a background call is wedged).")
        except Exception:
            pass
        try:
            logging.shutdown()
        except Exception:
            pass
        # os._exit: sys.exit only raises on this thread, and a normal
        # interpreter exit would still wait on the loop's executor threads --
        # which is one of the things that hangs us here.
        os._exit(0)

    threading.Thread(target=_kill, name="FF1ExitWatchdog", daemon=True).start()


VALID_IDX = {idx for (_lid, _name, idx) in DATA.LOCATIONS}

GAME_NAME = "Final Fantasy 1 PSP"


def _flags_quality(flags):
    """AP NetworkItem.flags -> quality bucket for GUI/log lines. Progression
    (bit0) wins, then trap (bit2), then useful (bit1); no bits = filler."""
    return ("progression" if flags & 0b001 else
            "trap" if flags & 0b100 else
            "useful" if flags & 0b010 else "filler")

# All seed-static tables (shuffles, scaling, chest contents) are BAKED into the
# patched ISO before boot (iso_patcher.apply_data_patches). The runtime loops
# below only RECONCILE: cheap fixed-address verifies that re-write a table when
# a save-state load reverts it, and a full runtime-patch fallback if the bake
# failed. The BOOT path never sweeps RAM anymore (its old scanning fallback
# re-scanned every 5 s forever when a baked code feature changed a table's
# signature -- the "game lags after new game" bug). Full-RAM sweeps survive only
# as last-resort fallbacks behind cached anchors (BANK/OW windows below).
BOOT_RECONCILE_S = 3.0
# small gap between per-table reconcile reads so the pass never bursts the
# debugger (each RPC briefly touches the emu side; spreading them keeps frame
# pacing smooth)
RECONCILE_SPACING_S = 0.05
# floating (heap) patches: minimum seconds between signature scans
FLOAT_SCAN_MIN_S = 10.0
# chest/mailbox poll cadence: 10/s is plenty (the old 33/s was a steady RPC drizzle)
CHEST_POLL_S = 0.1
# Heap-resident tables (shop name banks) relocate; scan these windows in order
# (observed home region first, full user RAM as fallback).
BANK_SCAN_WINDOWS = ((0x09000000, 0x01000000),
                     (USER_RAM_BASE, USER_RAM_SIZE))
# Overworld map arena (open-progression edits + canal shallows) lives in the high heap.
OW_SCAN_WINDOWS = ((0x09800000, 0x00800000),
                   (USER_RAM_BASE, USER_RAM_SIZE))
# Periodic re-anchor cadence while walking the overworld: catches an in-place arena
# relocation (no town/dungeon excursion) that the canary can't see because the stale
# freed buffer still holds our edits. One narrow-window scan every this-many seconds.
OW_REANCHOR_S = 6.0
# Shop AP-slot purchase watcher: one 2-byte mailbox-head read per tick, and the
# ring + inventory only when the head has moved (i.e. only on a real purchase).
SHOP_POLL_S = 0.5
# Shop-loop ticks between placeholder equip-mask re-syncs (_shop_sync_masks).
# 4 ticks = ~2s, so a placeholder-gid drop/chest/grant becomes equippable within
# a couple of seconds instead of waiting for the next shop-interior edge.
_MASK_RESYNC_TICKS = 4
# Reconcile granted items vs. the save-resident counter this often. A death/load
# rolls the counter back (AP sends NO message on load), so we must poll to detect it
# and re-grant the lost items.
GRANT_POLL_HZ = 1

# In-place-reload corroboration: how many consecutive stable ticks the received
# counter must HOLD below the session high-water (with no save-block move seen)
# before the grant loop treats it as a genuine game-over/in-place reload and
# re-grants the lost tail, instead of a spurious under-read to repair. At
# GRANT_POLL_HZ this is ~this many seconds; long enough that a one-frame glitch
# (which clears on the next tick) can never reach it, short enough to recover an
# item promptly after a game over. See _grant_pending REPAIR branch.
GRANT_INPLACE_RELOAD_TICKS = 3

# PATH A: chest contents are baked on-disc; this loop VERIFIES the runtime
# treasure table every tick and heals entries a stale save state reverted
# (and is the sole writer in no-bake fallback mode).
TT_VERIFY_S = 3.0

# Sleep between read_chunked chunks on BACKGROUND scans only: spreads the
# emu-side cost of a big RAM sweep across frames (one 1 MB read per ~frame)
# instead of a single stutter burst.
SCAN_BREATHE_S = 0.015


def _enc_item_tt(cat, game_id):
    """Encode an item chest value for RUNTIME_TREASURE_TABLE (bit31 set)."""
    return 0x80000000 | ((game_id & 0xFF) << 8) | (cat & 0xFF)


def _enc_gil_tt(amount):
    """Encode a gil chest value (bit31 clear)."""
    return amount & 0x7FFFFFFF


class _ByteSnapshot:
    """One span read serving many single-byte observations for one loop tick.

    _npc_loop watches ~15 flag/possession bytes that all live within a few
    dozen bytes of each other in the save block. Read individually that is 15
    RPCs; over the WS debugger on a remote device (Android, ~17 ms/call --
    memory: android-port-feasibility) that alone was ~7.5 calls/s and a
    quarter-second per tick. One span read collapses it to a single RPC.

    Writes go straight to the wire AND update the buffer, so a later read of a
    byte this tick just wrote sees the new value (the Earth-Rod batch strips
    the detector bit then re-reads the same address as its function bit).
    Addresses outside the span fall through to a normal read/write.
    """

    def __init__(self, psp, base, blob):
        self.psp = psp
        self.base = base          # LIVE address of blob[0] (already sa()-mapped)
        self.buf = bytearray(blob)

    async def rd(self, addr):     # addr: live (sa()-mapped)
        off = addr - self.base
        if 0 <= off < len(self.buf):
            return self.buf[off]
        return (await self.psp.read(addr, 1))[0]

    async def wr(self, addr, value):
        await self.psp.write(addr, bytes([value]))
        off = addr - self.base
        if 0 <= off < len(self.buf):
            self.buf[off] = value


class FF1PSPCommandProcessor(ClientCommandProcessor):
    def _cmd_goal(self):
        """Report the goal (Chaos defeated) to the server. Manual fallback --
        the flags loop now auto-detects Chaos-defeated (0x11520 b6) and reports."""
        if isinstance(self.ctx, FF1PSPContext):
            self.ctx.run_async(self.ctx.send_goal())
            self.output("Goal sent to server.")

    def _cmd_encounters(self, percent: str = ""):
        """Set the encounter rate live, as a PERCENT of vanilla (0=off,
        100=normal, 200=double) -- the same scale as the Monster Encounter Rate
        yaml option. No argument reports the current value. Applies immediately
        -- no reboot, no re-patch -- and persists across save/load via the
        boot-patch loop."""
        if not isinstance(self.ctx, FF1PSPContext):
            return
        c = self.ctx
        usage = "usage: /encounters <percent>  (0=off, 100=normal, 200=double)"
        if not percent:
            self.output(f"encounter rate {c.enc_mult * 100:g}%  ({usage})")
            return
        try:
            m = max(0.0, float(percent)) / 100.0
        except ValueError:
            self.output(usage)
            return

        async def apply():
            ok = await c.set_encounter_rate(m)
            c.refresh_boost()    # keep the Boost tab's selection honest
            self.output(f"encounter rate -> {m * 100:g}%"
                        + ("" if ok else
                           "  (write NOT verified -- PPSSPP bridge down?)"))
        c.run_async(apply())

    def _cmd_psp(self):
        """Show the PPSSPP bridge status."""
        if isinstance(self.ctx, FF1PSPContext):
            from .launcher import remote_psp_target
            c = self.ctx
            self.output(f"PPSSPP connected: {c.psp is not None}")
            mem = getattr(c.psp, "mem", None)
            self.output("memory transport: "
                        + ("direct process memory"
                           if mem is not None and mem.attached
                           else "WS debugger (fallback)"))
            r = remote_psp_target()
            if r:
                self.output(f"remote PPSSPP target: {r[0]}"
                            + (f":{r[1]}" if r[1] else " (port auto)"))
            self.output(f"chests checked: {len(c.sent_locations)} | "
                        f"items granted: {c.received_count}")
            # Percent everywhere player-facing -- same scale as the yaml options.
            self.output(f"scaling: encounter {c.enc_mult * 100:g}%, "
                        f"xp {c.xp_mult * 100:g}%, gil {c.gil_mult * 100:g}%, "
                        f"monster {c.monster_mult * 100:g}%, "
                        f"boss {c.boss_mult * 100:g}%")

    def _cmd_psp_remote(self, target: str = ""):
        """Drive PPSSPP on another device (Android phone / handheld) at
        host[:port] instead of launching one locally. /psp_remote off returns
        to local launching; no argument shows the current target. Applies
        when the bridge next starts (reconnect or client restart)."""
        from .launcher import load_cfg, save_cfg, remote_psp_target
        if not target:
            r = remote_psp_target()
            self.output("remote PPSSPP: "
                        + (f"{r[0]}" + (f":{r[1]}" if r[1] else " (port auto)")
                           if r else "off (launching locally)"))
            if os.environ.get("FF1PSP_REMOTE"):
                self.output("(set by the FF1PSP_REMOTE env var -- it "
                            "overrides the saved config)")
            self.output("usage: /psp_remote <host[:port]> | off")
            return
        cfg = load_cfg()
        if target.lower() in ("off", "none"):
            cfg.pop("remote_psp", None)
            save_cfg(cfg)
            self.output("remote PPSSPP cleared -- launching locally from the "
                        "next bridge start.")
            return
        cfg["remote_psp"] = target
        r = remote_psp_target(cfg)
        if not r:
            self.output(f"could not parse '{target}' -- use host or host:port "
                        "([addr]:port for IPv6)")
            return
        save_cfg(cfg)
        self.output(f"remote PPSSPP set: {r[0]}"
                    + (f":{r[1]}" if r[1] else " (port auto-discovery)")
                    + " -- applies when the bridge next starts (restart the "
                      "client if it's already connected).")

    def _cmd_checkiso(self, path: str = ""):
        """Check whether your FF1 disc image can actually be patched, and say
        what to do if not. Uses the ISO the client is already configured to
        use, so you do not have to find or type a path; pass one to test a
        different file. Run this whenever you see PATCH FAILED."""
        import os
        from .launcher import load_cfg, find_iso
        from . import eboot_patch as E
        from .cso_decompress import image_kind

        iso = path.strip().strip('"') or find_iso(
            load_cfg().get("ppsspp", ""), load_cfg().get("iso", ""))
        if not iso:
            self.output("No FF1 ISO is configured yet and none was found "
                        "automatically. Connect once to get the setup prompt, "
                        "or run:  /checkiso <path to your .iso>")
            return
        self.output(f"ISO: {iso}")
        if not os.path.isfile(iso):
            self.output("VERDICT: that file does not exist. Run "
                        "/purge_ff1psp_setup confirm to clear the saved path "
                        "and pick your ISO again.")
            return
        self.output(f"size: {os.path.getsize(iso) / (1 << 20):.0f} MB")

        kind = image_kind(iso)
        if kind not in ("iso", "unknown"):
            pretty = {"ciso": "CSO (compressed)", "ziso": "ZSO (compressed)",
                      "dax": "DAX (compressed)", "chd": "CHD (compressed)"}[kind]
            self.output(f"format: {pretty}")
            if kind == "ciso":
                self.output("VERDICT: OK -- the client expands CSO to a plain "
                            "ISO by itself the first time it bakes. Nothing "
                            "for you to do.")
            else:
                self.output("VERDICT: this format cannot be expanded here. "
                            "Convert it to a plain .iso (PPSSPP: Game "
                            "settings > Tools, or maxcso), then run "
                            "/purge_ff1psp_setup confirm and pick the .iso.")
            return

        try:
            with open(iso, "rb") as f:
                cands = E._iso_find_boot_bins(f)
                if not cands:
                    self.output("VERDICT: no BOOT.BIN found -- this is not a "
                                "readable PSP disc image. Re-dump or "
                                "re-convert it.")
                    return
                blank = elf_ok = False
                for p, off, size in cands:
                    f.seek(off)
                    head = bytearray(f.read(size))
                    tag = ("plaintext ELF (good)" if head[:4] == b"\x7fELF"
                           else "ALL ZEROS (blank)" if not any(head)
                           else f"unknown, starts {bytes(head[:4]).hex(' ')}")
                    self.output(f"  {p} @ {off:#x} ({size:#x} bytes): {tag}")
                    elf_ok |= head[:4] == b"\x7fELF"
                    blank |= not any(head)
        except OSError as e:
            self.output(f"VERDICT: could not read the file ({e}).")
            return

        if elf_ok:
            try:
                E.load_boot_elf(iso)
            except Exception as e:
                # strip the "type /checkiso" pointer: they just did
                self.output("VERDICT: unusable -- "
                            + str(e).replace(E.CHECKISO_HINT, ""))
                return
            self.output("VERDICT: OK -- this ISO can be patched. If a bake "
                        "still fails, send your client log.")
            return
        if blank:
            self.output(
                "VERDICT: unusable -- BOOT.BIN is blank. The randomizer "
                "patches that file; your image only has the game code in the "
                "encrypted EBOOT.BIN, which cannot be read without PSP "
                "console keys.")
            self.output(
                "FIX: if you converted this from a .cso/.chd, re-convert it "
                "(PPSSPP: Game settings > Tools, or maxcso) -- a bad "
                "conversion can write blank sectors. If it was always a .iso, "
                "you need a different dump: the one this randomizer is built "
                "against is 'Final Fantasy - 20th Anniversary Edition (USA) "
                "(En,Ja) (FW3.03)'.")
        else:
            self.output("VERDICT: unusable -- BOOT.BIN is not a plaintext "
                        "ELF, so this is not the FF1 PSP dump this randomizer "
                        "is built against.")
        self.output("Once you have a good file: /purge_ff1psp_setup confirm, "
                    "then restart the client and pick it at the prompt.")

    def _cmd_shop_find(self, *words):
        """Search every shop in the towns you have visited and show the hits on
        the Shops tab's Find view -- the same thing as typing in that tab's search
        box, for when the command line is where your hands already are.
        No argument clears the search.
        Usage: /shop_find phoenix"""
        if not isinstance(self.ctx, FF1PSPContext):
            return
        query = " ".join(words).strip()
        ui = getattr(self.ctx, "ui", None)
        if ui is None or not hasattr(ui, "set_shops_query"):
            self.output("The Shops tab is not available in this window.")
            return
        ui.set_shops_query(query)
        if not query:
            self.output("Shop search cleared.")
            return
        try:
            payload = self.ctx._shops_payload()
        except Exception:
            payload = None          # not connected yet -- no seed, no shops
        found = TRACKER.shop_search(payload, query)
        self.output(f"Shops tab -> Find: {found['hits']} match(es) for "
                    f"{query!r} across {found['towns_visited']} visited town(s).")
        for grp in found["groups"]:
            for it in grp["offers"]:
                self.output(f"  {grp['title']}: {it['item']}  {it['price']}g"
                            + ("  (bought)" if it["found"] else ""))
            for it in grp["stock"]:
                self.output(f"  {grp['title']}: {it['name']}  {it['price']}g")
            for sp in grp["spells"]:
                self.output(f"  {grp['title']}: {sp['name']}  {sp['price']}g")

    def _cmd_ff1psp_logs(self):
        """Save and export debug files: logs, seed's options, the game's patch
        state and a read-only snapshot of the running game. Run this whenever
        something looks wrong, then send the zip file it makes with whomever
        maintains this apworld."""
        import functools
        from . import debug_bundle as DB
        ctx = self.ctx if isinstance(self.ctx, FF1PSPContext) else None
        self.output("Collecting debug info… (a few seconds)")

        def finish(path, err=None):
            if err is not None:
                self.output(f"Could not write the debug bundle: {err!r}")
                self.output("Send your client log instead -- it is in the "
                            "'logs' folder next to the Archipelago launcher.")
                return
            for line in DB.handoff_lines(path):
                self.output(line)
            if not DB.reveal(path):
                self.output("(Could not open the folder automatically -- the "
                            "path above is the file.)")

        async def go():
            # The RAM read must happen on the asyncio thread that owns the
            # bridge; the zip write is blocking I/O, so it goes to an executor.
            ram = None
            if ctx is not None and ctx.psp is not None:
                try:
                    ram = await asyncio.wait_for(DB.snapshot_ram(ctx), 20)
                except Exception as e:
                    ram = {"error": repr(e)}
            try:
                path = await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(DB.build_bundle, ctx, ram))
            except Exception as e:      # build_bundle is defensive, but the
                finish(None, e)         # disk can still be full / read-only
                return
            finish(path)

        if ctx is None:                 # no context: still worth a log bundle
            try:
                finish(DB.build_bundle(None, None))
            except Exception as e:
                finish(None, e)
            return
        ctx.run_async(go())

    def _cmd_purge_ff1psp_setup(self, confirm: str = ""):
        """Forget this machine's FF1 PSP setup so the next connect behaves like a
        brand-new install: clears the saved PPSSPP exe path, the FF1 ISO path and
        any /psp_remote target, and the one-time path prompt comes back. Does NOT
        touch your server/slot, your save data or your ISOs on disk.
        Usage: /purge_ff1psp_setup confirm"""
        from .launcher import load_cfg, purge_setup, SETUP_KEYS
        cfg = load_cfg()
        present = [k for k in SETUP_KEYS if cfg.get(k)]
        if confirm.lower() not in ("confirm", "yes", "y"):
            self.output("This wipes the saved FF1 PSP setup (PPSSPP exe path, "
                        "FF1 ISO path, remote PPSSPP target).")
            self.output("currently saved: "
                        + (", ".join(f"{k}={cfg[k]}" for k in present)
                           if present else "(nothing -- already clean)"))
            self.output("Re-run as:  /purge_ff1psp_setup confirm")
            return
        res = purge_setup()
        self.output("FF1 PSP setup purged"
                    + (f" ({', '.join(res['cleared'])})" if res["cleared"]
                       else " (nothing was saved)")
                    + ".")
        self.output(("deleted " if res["removed"] else "rewrote ")
                    + res["path"])
        if res["env"]:
            self.output("NOTE: still overridden by env var(s): "
                        + ", ".join(res["env"])
                        + " -- unset them for a truly clean first run.")
        self.output("The setup prompt will reappear the next time the client "
                    "launches PPSSPP (restart the client to redo setup now).")


class FF1PSPContext(CommonContext):
    game = GAME_NAME
    items_handling = 0b111          # remote + own-from-own + starting inv
    command_processor = FF1PSPCommandProcessor

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        self.psp = None              # PPSSPP conn for grants / memory I/O
        self.psp_bp = None           # SEPARATE conn for breakpoint+resume
                                     # must remove+unhalt; stays False on the
                                     # poll-based chest path (no bps at all)
        self.psp_scan = None         # SEPARATE conn for big background scans
        # Grant counter now lives in the SAVE (D.RECEIVED_COUNTER_ADDR_SA), not a JSON
        # file, so it rolls back with the items on death/load. received_count here is
        # just a last-known cache for /psp display; the RAM value is the truth.
        self.received_count = 0
        # (loop|stage name, repr(exc)) pairs already logged with a traceback --
        # a 2s poll must not spam the same stack forever.
        self._loop_err_seen = set()
        # NPC_MAP_RESET rows whose "won key held out of the menu" hint has been
        # printed this session (see D.PREARM_HOLD_HINT).
        self._prearm_hinted = set()
        self._grant_lock = asyncio.Lock()   # serialize grant<->counter writes
        self._warned_bad_counter = False    # one-shot guard-trip warning
        # Counter-hardening state (see _grant_pending). The save-resident counter
        # can spuriously under-read on a busy/transitional frame with NO real
        # save reload -- that mass-duplicated items live 2026-07-10. _counter_hw
        # is the session high-water of what we've actually delivered; a counter
        # that drops below it WITHOUT a corroborating reload is treated as a
        # glitch and repaired, never re-granted. _reload_pending is set by
        # _save_delta_loop whenever the save block vanishes/relocates (a genuine
        # death/load, which always dwells on the title screen long enough for the
        # 1 Hz save-delta poll to catch) -> the next counter decrease is honored
        # as a real rollback and its lost tail re-granted.
        # BUT a game-over "Continue" can reload the last save IN PLACE (same block
        # base), so the save-delta poll never sees a move and _reload_pending stays
        # False even though the counter (and inventory) really rolled back (Prime
        # lost a Defender this way 2026-08-13). That in-place reload is caught by
        # PERSISTENCE: a low counter that HOLDS for _repair_streak ticks (our AP
        # counter can only fall below hw via a reload or a transient glitch, and a
        # glitch clears within a tick) is a real reload -> re-grant the lost tail.
        self._counter_hw = 0                 # highest counter value delivered this run
        self._reload_pending = False         # a save (re)load/relocation was observed
        self._warned_glitch_counter = False  # one-shot spurious-decrease warning
        self._repair_streak = 0              # consecutive stable low reads (in-place
        self._repair_streak_c = None         # reload detector); the c value tracked
        self._inv_full_warned = False        # one-shot inventory-full stall warning
        self._grant_stall = None             # (why, since_monotonic, warned) for
                                             # the silent-gate stall report
        # Server item list (self.items_received) is EMPTIED during a disconnect and
        # only refills on the post-reconnect resync. Making a destructive decision
        # (strip a native key item, resend a grant) off that transient-empty list
        # cost the player owned key items live 2026-07-08 (strips fired 1s after a
        # drop). _ever_won is a STICKY, only-grows mirror of every AP item id we've
        # ever seen won -- immune to the disconnect blip; _had_sync gates loops so
        # they never act before the first real resync has landed. See memory
        # item-delivery-opt-a / key-item-loss-on-disconnect.
        self._ever_won = set()              # AP item ids ever present in items_received
        self._scroll_msg_seen = set()       # base jobs whose scroll blurb was logged
        self._had_sync = False              # a non-empty resync has landed this run
        self._towns_visited = set()   # city ids whose reveal bit is set (Shops tab)
        self.sent_locations = set()   # location ids already reported
        self.tt_values = {}          # treasure idx -> AP-mapped u32 (baked + verified)
        self.bake_ok = False         # running game carries this seed's on-disc bake
        self.idx_desc = {}
        # poll-based chest detection (tier2-poll-chests)
        self._own_chest_idxs = set()     # idx of own item/gil chests (native grant).
        self._event_key_natives = {}     # idx -> vanilla native key id (D.EVENT_KEY_CHESTS).
                                         # Route A: _evk_pathb_loop forces PATH B on these
                                         # chests (correct AP box name + grant-loop delivery);
                                         # this dict feeds the strip safety net that cleans up
                                         # a stray native key if the flag write ever loses the
                                         # race and the FIF event fires.
        self._remote_chest_idxs = set()  # idx of remote chests (filler grant + cleanup)
        self._opened_own_locs = set()    # own-chest loc ids the poll saw opened ->
                                         # _grant_pending skips them (native delivered)
        self._remote_names = []          # ordered UNIQUE (who, item) pairs -> baked bank
        self._remote_name_idx = {}       # (who, item) -> its index in _remote_names
        self._remote_base = D.CHEST_REMOTE_SID_BASE  # first remote sid (tomes-dependent)
        self._dyn_names = {}             # (dungeon, ordinal) -> (who, item) box name
        self._dyn_slot_patch = None      # floating DataPatch over the 2 wide dyn slots
        self._key_names = {}             # key item id -> AP name @ its granting loc
        self._keybox_extra = {}          # non-key obtain-box subject (lower) -> AP name
        self._citadel_orig = {}          # (bank_addr, entry_off) -> vanilla slot bytes
        self._hold_oos = {}              # map-scoped hold -> consecutive out-of-scope ticks
        self._hold_spent_seen = set()    # map-scoped hold rows whose item is spent (sticky)
        self._sm_canary_warned = False   # slot_magic guard-band alarm (once)
        self._sage_orig = {}             # ditto, for the giver sage's lore boxes
        self._lute_slab_orig = {}        # ditto, for the Chaos Shrine slab inscription
        self._chest_bf_prev = None       # last opened-chest bitfield snapshot
        self._chest_bf_delta = None      # save_delta the snapshot was taken at
        self._chest_bf_warned = False
        self._init_needed = None         # run the new-game one-shots? Latched once
                                         # per save-block acquisition by
                                         # _init_marker_tick, and read ONLY by the
                                         # four short-lived one-shot loops -- see
                                         # the NEW GAME vs LOADED SAVE note. Every
                                         # long-lived consumer uses the live
                                         # _newgame_block_live() instead.
        self.enc_mult = 1.0
        self.xp_mult = 1.0
        self.gil_mult = 1.0
        self.boss_mult = 1.0
        self.monster_mult = 1.0
        self.slot_data = {}
        self.auto_dash = True             # yaml QoL: new-game Config defaults
        self.message_speed = D.MSG_SPEED_DEFAULT
        self.cursor_mode = D.CURSOR_MODE_DEFAULT
        self.party_jobs = [None, None, None, None]
        # Live party classes + magic levels for the Shops tab's usability shading
        # (refreshed by _shop_hint_loop). None = never read; fall back to slot_data.
        self._party_view = None
        self.naked_monks = False
        self.starting_gil = None          # yaml starting_gil (None = pre-option seed)
        self._starting_gil_applied = False  # gate _naked_monks_loop waits on
        self.thief_steal = False          # yaml: Thief end-of-battle extra-item ability
        self.auto_sell_unusable = True    # yaml auto_sell_unusable_items (DefaultOnToggle:
                                          # absent from old slot_data means ON, not off)
        self._auto_sell_cache = {}        # (cat, gid) -> gil value (0 = keep); per-seed
        self._battlemsg_bank = None       # cached BATTLE_MSG.MSG bank addr (steal box)
        self._scroll_mb = None            # cached SCRL mailbox addr (job-scroll boosts)
        self._mb_miss = {}                # mailbox tag -> consecutive scan misses
        self._mp_mb = None                # cached MPWR mailbox addr (magic power scaling)
        self._mp_tables = None            # last (eff, van, shr) written; skip redundant writes
        self._dia_int_was_battle = False  # v108: edge-detect battle exit to zero
                                          # the White Cleric dia INT accumulator
        self._bdc_mb = None               # cached BDC1 mailbox addr (bonus dyn chests)
        self._buy_mb = None               # cached BUYB mailbox addr (shop purchases)
        self._steal_icon_mb = None        # cached SPRB mailbox addr (steal-cue icon)
        self._steal_icon_restore = None   # (entry addr, saved bytes) of borrowed def 19
        self._steal_icon_task = None      # pending fade-out task for the loot cue
        self._steal_arm_task = None       # pending DELAYED-arm task for the loot cue
        self._steal_box_restore = None    # (bank, off15_bytes, orig_span_bytes) to undo
        self.bonus_dyn_caps = {}          # dungeon idx (int) -> dynamic-chest AP-check cap
        self.bonus_dungeon_crystals = False   # bonus_dungeon_crystals yaml: crystals
                                          # activate by beating a Soul-of-Chaos
                                          # superboss (see _bonus_crystal_loop)
        self.lute_tablets_required = 0    # lute_tablets yaml: tablets to assemble the
                                          # Lute (0 = off, Lute is a normal AP item)
        self._tablet_hw = 0               # STICKY high-water of Lute Tablet copies seen
                                          # in items_received (the list empties on a
                                          # disconnect; the assembled Lute must not
                                          # un-assemble). Same rationale as _ever_won.
        self.equipment_runes_required = 0 # equipment_runes yaml: runes to assemble the
                                          # Equipment Rune Key (0 = gate off)
        self._rune_hw = 0                 # STICKY high-water of Equipment Rune copies
                                          # seen (same disconnect rationale as
                                          # _tablet_hw; the Key must not un-assemble)
        self.levistone_shards_required = 0  # levistone_shards yaml: shards to assemble
                                          # the Levistone (0 = off, normal AP item)
        self._shard_hw = 0                # STICKY high-water of Levistone Shard copies
                                          # seen (same disconnect rationale as
                                          # _tablet_hw; the airship must not un-raise)
        self._shard_logged_hw = 0         # highest shard count already announced in
                                          # the log (shard receipts are counter-only,
                                          # invisible otherwise -- same as runes)
        self._rune_logged_hw = 0          # highest rune count already announced in the
                                          # log. A rune receipt is invisible otherwise:
                                          # _ap_item_to_game returns (None, None, 0) for
                                          # it and the cat-is-None branch only logs when
                                          # qty > 0, so nothing ever printed per pickup
        self.death_link_on = False        # yaml death_link: DeathLink tag + wipe loop
        self.death_link_severity = 3      # yaml: living members killed per received death
        self._dl_pending = False          # a received DeathLink awaits application
        self._dl_guard = False            # suppress sending a death WE caused (received
                                          # link killed the whole party); clears when
                                          # any member is alive again
        self._dl_wipe_latch = False       # a death WE applied left 0 living -> the
                                          # battle-limbo recovery net is armed. Only
                                          # OUR wipes arm it, so an enemy-caused wipe
                                          # can never be interrupted mid-game-over.
        self._dl_limbo = False            # we revived someone out of limbo: keep the
                                          # send-guard until the party is genuinely
                                          # healthy again (no bounce-back)
        self._field_all_dead = False      # last FIELD read had zero living members ->
                                          # a battle started now was entered dead
                                          # (survives reloads, unlike _dl_wipe_latch)
        self._dl_fail_streak = 0          # consecutive death_link_loop read failures
                                          # (Invalid address on a bad/relocating delta);
                                          # drives exponential backoff so a wedged read
                                          # doesn't spam every tick forever
        self._last_soft_log = None        # last softened id-set we logged; the set is
                                          # re-applied on every map load but only
                                          # changes rarely, so log on change only
        self._cameo_soft = ()             # boss ids currently softened in the live
                                          # monster_rewards block. REAL state, not just
                                          # a log dedupe key: set_monster_scaling has to
                                          # rebuild the block from the same soft set or
                                          # a Boost-tab xp/gil/boss change would clobber
                                          # the current map's cameo softening until the
                                          # next map load
        self.aio_loop = None              # the client's asyncio loop, captured in
                                          # launch(). The Kivy GUI runs on its own
                                          # thread, so Boost-tab presses need
                                          # run_coroutine_threadsafe, not create_task
        # Chaos-defeated auto-goal edge guard: the goal bit lives IN the save block,
        # so a New Game started from a BEATEN save (NG+ bestiary carryover) carries it
        # SET -- a first-read auto-report would false-goal the seed (ruined a slot
        # 2026-07-23). Only auto-report if we OBSERVED the bit clear first at the
        # current delta, i.e. it flipped clear->set during play. Set-on-first-read is
        # carried-in state -> leave the manual /goal fallback. Same carried-in-on-first-
        # tick logic as _npc_reset_lastmap below.
        self._chaos_ever_clear = False    # saw Chaos bit CLEAR at _chaos_guard_delta
        self._chaos_guard_delta = None    # delta _chaos_ever_clear was observed at
        self._chaos_carryin_logged = False
        # NG+ carried-transient handling: a New Game from a BEATEN save briefly loads
        # the cleared save's completed state (Chaos bit set, chests open, counter huge,
        # exp>0) into the buffer before zeroing it for the new game. One-shot new-game
        # loops that fire during that window latch the wrong state -> party never set,
        # 200+ phantom chest checks, grants blocked by a carried counter. All keyed off
        # _carried_save_snapshot() (Chaos-defeated bit still set). See [[fixed-bug-ledger]].
        self._chest_carryin_skips = 0     # chest-baseline ticks deferred past the window
        self.shop_slots = []              # [(shop, cat, row0 gid, [prices])] derived view
        self.shop_rows = {}               # shop -> [(cat, gid, price)] per offer row
        self._shop_base = {}              # shop -> normal stock rows before the AP tail
        self._shop_gid_row = {}           # (shop, cat, gid) -> offer row index
        self._shop_sold_recent = set()    # (shop, row) bought without leaving the shop yet
        self.hint_rows = {}               # shop -> [(cat, gid, price, label, [lids])]
        self._hint_gid_row = {}           # (shop, cat, gid) -> hint row index
        self._hint_bought = set()         # (shop, row) bought (server DataStorage)
        self._hint_dirty = False          # bought set changed -> re-render shelves
        self._hint_sold_recent = set()    # (shop, row) spent without leaving the shop
        self._hint_rendered = {}          # shop -> consumed rows at last render
        self._shop_equip_gids = frozenset()   # per-seed weapon/armor placeholder (cat,gid)
        self.shop_desc = {}               # (shop, k) -> "who's item" (from the scout)
        self.shop_offers = {}             # shop -> [(lid, item_name), ...] scout info
        self._extra_patches = []          # scout-built DataPatches (shop name banks)
        self._float_rescan = False        # force a floating-bank rescan (save load /
                                          # town toggle can spawn a NEW bank copy)
        self._bonus_latch = False         # in a Soul-of-Chaos bonus dungeon (latched)
        self._rune_zone = None            # latched answer for _rune_borrow_zone:
                                          # None = unknown (make no borrow writes),
                                          # True = release the id-35 borrow, False = hold
        self._bank_vanilla = False        # shop name/desc banks held at VANILLA
                                          # unless inside a shop building
                                          # (see _shop_loop tick)
        self._shared_tails = False        # slot_data shop_ap_shared: tail gids are
                                          # the reserved constants SHARED across
                                          # stores; identity authored per TOWN
        self._cur_town = None             # store-city index the party last stood
                                          # in (street map id latch; v2 only)
        self._town_prices_stamped = None  # town whose row prices are in the tables
        self._masks_synced = False        # first _shop_sync_masks done this session
        self._masks_tick = 0              # shop-loop ticks since the last re-sync
                                          # (see _MASK_RESYNC_TICKS)
        self._gil_healed = False          # one-shot over-cap gil clamp done
        self._party_applied = False
        self._patches = []
        self._bridge_started = False
        self._tasks = []
        # The 0x08D1xxxx save block is heap-allocated and MOVES between
        # sessions (deltas 0/+0x1000/+0x4000 observed). save_delta is the
        # session's offset vs the canonical layout, resolved from the game's
        # own static pointer (D.SAVE_BLOCK_PTR) by _save_delta_loop; None
        # until a real game is running. Every save-block consumer goes
        # through self.sa() and skips its tick while unresolved.
        self.save_delta = None
        # NPC map-entry native-refresh (D.NPC_MAP_RESET) state: key ids whose
        # gate/possession bits were just set by OUR OWN grant_key_item -- the
        # detector must re-clear those instead of reading the rise as the NPC
        # firing (a save-reload re-grant in-map false-fired the robot check,
        # live 2026-07-13). _npc_reset_lastmap edge-detects map entry: a set
        # gate on the FIRST in-map tick is carried-in state (the outside-map
        # restore), never the NPC -- clear it, don't fire.
        self._npc_reset_selfgrant = set()
        self._npc_reset_lastmap = None
        # Watchdog for the edge-detect above: how many times each row's gate has
        # been cleared AS AN ENTRY TICK during one continuous stay in its map.
        # A correct edge-detect can only ever produce ONE per entry -- a second
        # means the row's `entered` test is broken and every NPC handover is
        # being eaten as carried-in state (the Caravan tuple-vs-scalar bug, live
        # 2026-08-08). Reset on leaving the map; see _npc_reset_entry_clear.
        self._npc_reset_entryclears = {}

    # ---------------- save-block base resolution ----------------
    def sa(self, addr):
        """Canonical save-block address -> this session's live address."""
        return addr + (self.save_delta or 0)

    async def _resolve_save_delta(self):
        """Read the game's static save-struct pointer and derive the session
        delta. Returns None until the game has allocated a real save struct
        (title screen / party creation): validated by a sane char-0 level."""
        v = await self.psp.read_u32(D.SAVE_BLOCK_PTR)
        delta = v - D.SAVE_BLOCK_PTR_CANON
        if not (0x08800000 <= v < 0x0A000000) or delta % 4:
            return None
        # sa-ok: this is the ONE site that computes the delta itself (sa() would
        # be circular -- we're resolving what sa() returns), so it applies `delta`
        # by hand. The policy test honors the `sa-ok` marker to skip this line.
        lv = await self.psp.read_u32(D.PARTY_BASE_SA + delta + D.P_LEVEL)  # sa-ok
        if not 1 <= lv <= 99:
            return None
        return delta

    async def _carried_save_snapshot(self):
        """True while the save block still holds a COMPLETED-game snapshot -- the
        Chaos-defeated bit is set. That is the transient a New Game started from a
        BEATEN save shows while the game copies the cleared save in (bestiary
        carryover) BEFORE zeroing it for the new game; New-Game init clears the bit.
        A genuine in-progress, non-endgame save never has it set, so one-shot
        new-game actions can safely hold off until this returns False. (Our own
        _flags_loop never sets bit6, only the game/carryover does.)"""
        if self.save_delta is None:
            return False
        try:
            b = (await self.psp.read(self.sa(D.CHAOS_DEFEATED_ADDR), 1))[0]
        except Exception:
            return False
        return bool(b & D.CHAOS_DEFEATED_MASK)

    async def _party_records_fresh(self):
        """RAW check: all 4 party records at Lv1 with EXP 0. NOT a new-game
        verdict -- a loaded save whose party has never fought reads exactly the
        same. This is a SAFETY GATE: it can only ever prevent action, never
        authorise it. Pair it with _save_is_initialised (one-shots) or use
        _newgame_block_live (the grant loop); see the note below."""
        if self.save_delta is None:
            return False
        try:
            for ci in range(D.PARTY_COUNT):
                if await self.psp.read_u32(self.sa(D.party_addr_sa(ci, D.P_LEVEL))) != 1:
                    return False
                if await self.psp.read_u32(self.sa(D.party_addr_sa(ci, D.P_EXP))) != 0:
                    return False
        except Exception:
            return False
        return True

    # === NEW GAME vs LOADED SAVE ================================================
    #
    # THE QUESTION IS THE WRONG ONE. "Is this a new game?" cannot be answered from
    # game state: a committed new game and a save file the player made before their
    # first battle present IDENTICALLY (Lv1, EXP 0, no chests, story flags clear).
    # Every heuristic built on that state was wrong, and loading such a save re-fired
    # every new-game one-shot (live 2026-08-17, reported by a player who saved in
    # Cornelia at Lv1):
    #   * [party] re-stamped the yaml jobs onto rows 0..3, but a Formation swap MOVES
    #     the character records between rows -- so the class byte + level-1 stat block
    #     landed on the WRONG character. Names and learned SPELLS travel with the
    #     record, the class does not, so the party came back as "the White Mage knows
    #     Firaga" / "the Thief knows Curaga".
    #   * [starting_gil] re-paid the yaml purse on every client restart
    #     (500 -> 12332 -> 22687 -> 35262 across three sessions in one bundle).
    #   * [grant] zeroed the save-resident received counter -> re-delivered items.
    #   * [slot_magic] wiped the spent-charge array.
    #
    # ASK INSTEAD: "have I already initialised this save block?" That one HAS a
    # direct answer -- D.AP_INIT_MARKER_SA, a byte we stamp ourselves. Measured
    # 2026-08-17: the game's new-game init ZEROES our region while a load RESTORES
    # it from the file, so marker-present is a statement of fact, not an inference.
    # It is also per-SAVE-FILE, which is exactly the scope these one-shots need.
    #
    # GIL IS NOT EVIDENCE. It was, briefly, and it was never sound: starting_gil is
    # yaml-adjustable, and an AP gil item from another world can land before the
    # player's first connect. Deleted. Same for the chest bitfield and the map-record
    # list -- both real signals (see ff1_data), both redundant once the marker
    # answers directly.
    #
    # TWO CONSUMERS, TWO LIFETIMES, TWO FUNCTIONS. This is the distinction whose
    # absence caused the second bug of 2026-08-17:
    #   * The ONE-SHOT loops (jobs, starting gil, naked monks, config defaults) live
    #     for about two seconds and then return forever. They share ONE latch,
    #     _init_needed, so they cannot disagree with each other while converging.
    #   * The GRANT LOOP's carried-counter reset lives for the whole session and must
    #     therefore be LIVE -- _newgame_block_live(), which caches nothing. A latched
    #     verdict there is what let a 77-minute-old "fresh new game" survive a
    #     quit-to-title-and-load and re-grant the entire item list.
    # Because the live path caches nothing, a save (re)load needs no detecting: the
    # next tick simply reads the reloaded block. No reload edge, no play-time
    # discontinuity watcher, no re-arm.
    #
    # NG+ is the one case that can lie: a New Game from a BEATEN save copies the
    # cleared save in for bestiary carryover and brings our region with it, marker
    # included. Handled by CLEARING the marker while that snapshot is on screen
    # (Chaos bit set) rather than by threading a "distrust the marker" flag through
    # the verdict -- so the marker is trustworthy at every read. Safe because
    # _party_records_fresh() still gates every one-shot, so even a misread on a real
    # endgame save cannot act.
    async def _save_is_initialised(self):
        """Has client init already run against this save block?

        LIVE -- never cached, safe to call from anywhere. Fails SAFE: anything we
        cannot read returns True (= stand down), because doing nothing can never
        corrupt a save while acting wrongly demonstrably can."""
        if self.save_delta is None:
            return True
        try:
            marker = (await self.psp.read(self.sa(D.AP_INIT_MARKER_SA), 1))[0]
            if marker == D.AP_INIT_MARKER_VALUE:
                return True
            # LEGACY LEG -- retire once every live save carries the marker (no
            # earlier than PATCHER_VERSION 280; delete this branch and the
            # PLAY_TIME_SECONDS_SA read with it). A save file written before the
            # marker existed reads marker-absent, so fall back to the one native
            # field that is serialized, restored on load, exactly 0 on a committed
            # new game, and -- unlike gil -- never perturbed by anything we write.
            secs = await self.psp.read_u32(self.sa(D.PLAY_TIME_SECONDS_SA))
        except Exception:
            return True
        return secs > D.NEW_GAME_PLAY_TIME_MAX

    async def _newgame_block_live(self):
        """LIVE "this save block is a genuinely fresh new game", for the grant
        loop's carried-NG+-counter reset. Caches NOTHING, deliberately.

        Does NOT consult the init marker: we stamp that ourselves the moment the
        one-shots arm, so by the time the grant loop runs it would always read
        "initialised" and the NG+ reset -- which a NG+ seed depends on to un-wedge
        every grant -- could never fire. Play time serves instead: it is zero at
        the commit and climbs, so D.NEW_GAME_PLAY_TIME_MAX bounds the reset to the
        opening minute of a game. See that constant for the window's residual and
        why a roomier one was tried and rejected."""
        if self.save_delta is None:
            return False
        if not await self._party_records_fresh():
            return False
        if await self._carried_save_snapshot():
            return False
        try:
            secs = await self.psp.read_u32(self.sa(D.PLAY_TIME_SECONDS_SA))
            bf = await self.psp.read(self.sa(D.CHEST_OPEN_BF_SA),
                                     D.CHEST_OPEN_BF_BYTES)
        except Exception:
            return False
        return secs <= D.NEW_GAME_PLAY_TIME_MAX and not any(bf)

    async def _init_marker_tick(self):
        """Maintain the init marker and the one-shot latch. One call per
        save-delta tick, and the only place either is written."""
        if self.save_delta is None:
            return
        if await self._carried_save_snapshot():
            # NG+ character creation on screen: our region is the BEATEN save's
            # carryover, so its marker is not ours. Clear it now and stay
            # undecided; the commit that follows reads a clean absent marker.
            try:
                a = self.sa(D.AP_INIT_MARKER_SA)
                if (await self.psp.read(a, 1))[0] != 0:
                    await self.psp.write(a, b"\x00")
                    logger.info("  [newgame] NG+ carried snapshot on screen -> "
                                "cleared the carried init marker")
            except Exception:
                pass
            return
        if self._init_needed is None:
            if not await self._party_records_fresh():
                self._init_needed = False
                return              # ordinary in-progress game; nothing to say
            self._init_needed = not await self._save_is_initialised()
            logger.info(
                "  [newgame] fresh new game -> one-shots armed"
                if self._init_needed else
                "  [newgame] party reads Lv1/EXP0, but this save block has "
                "already been through client init -> LOADED SAVE: jobs, starting "
                "gil, monk strip and config defaults all stand down")
        # Keep it stamped once decided, in BOTH directions: a new game needs the
        # stamp to ride into its first save, and a legacy file that got here on the
        # play-time leg needs it so the next load lands on the marker instead.
        # Re-asserting because _reset_slotmagic_state zeroes 0x80C..0x83B on NG+.
        try:
            a = self.sa(D.AP_INIT_MARKER_SA)
            if (await self.psp.read(a, 1))[0] != D.AP_INIT_MARKER_VALUE:
                await self.psp.write(a, bytes([D.AP_INIT_MARKER_VALUE]))
        except Exception:
            pass

    async def _save_delta_loop(self):
        """Keep self.save_delta current (1 u32 + 1 u32 read per tick). The
        struct can move on returning to the title screen / reloading, so this
        re-checks forever, and every consumer gates on save_delta."""
        async def tick():
            d = await self._resolve_save_delta()
            if d != self.save_delta:
                if d is None:
                    logger.info("  [save] block lost (title screen?) -- "
                                "grants paused")
                else:
                    logger.info(f"  [save] block located: delta {d:+#x}")
                    # a save (re)load can spawn a fresh copy of the heap name
                    # banks (the shop UI reads it) -- rescan and patch it
                    self._float_rescan = True
                # The save block just vanished (title screen) or moved to a new
                # base -- i.e. a genuine save (re)load/relocation. The received-
                # counter may legitimately roll back with the reloaded save, so
                # authorize the grant loop to honor the NEXT counter decrease as a
                # real rollback (re-grant the lost tail) instead of repairing it as
                # a spurious glitch. Set on BOTH edges (lost and relocated) so a
                # fast reload that never nulls the delta is still corroborated.
                self._reload_pending = True
                # A save (re)load also means we must RE-OBSERVE this new game's Chaos
                # bit go clear before auto-reporting the goal. Without this, a mid-session
                # reload of a beaten/clear save that lands on the SAME save_delta inherits
                # _chaos_ever_clear from the prior committed game and false-reports the
                # goal -> auto-releases the whole seed (live 2026-07-23). flags_loop's own
                # re-arm only triggers on a delta VALUE change, so it misses a same-delta
                # reload; re-arm here on every (re)load edge, including through None.
                self._chaos_ever_clear = False
                self._chaos_carryin_logged = False
                # Re-latch the one-shot gate for the new block. NOTE this is the only
                # thing a missed edge can strand, and it is harmless: the one-shot
                # loops have all returned by then. The long-lived consumers never
                # read it -- they call _newgame_block_live(), which caches nothing --
                # so a same-delta reload needs no detecting.
                self._init_needed = None
                self.save_delta = d
            # Latch the one-shot gate and keep the marker stamped. Runs before any
            # one-shot loop can act, and is the only writer of either.
            await self._init_marker_tick()

        await self._poll(1.0, "save_delta_loop", tick)

    # ---------------- AP auth ----------------
    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def run_async(self, coro):
        """Schedule a coroutine on the running loop (for command handlers)."""
        asyncio.create_task(coro)

    def run_async_threadsafe(self, coro, done=None):
        """Schedule a coroutine from ANOTHER thread -- specifically the Kivy GUI
        thread, which is where the Boost tab's buttons fire. create_task() is not
        thread-safe and would either raise or attach the task to the wrong loop.

        `done` (if given) is called with the coroutine's result, ON THE ASYNCIO
        THREAD; a GUI caller must bounce it back through Clock itself. Returns the
        Future, or None if no loop has been captured yet (headless/CLI path)."""
        loop = self.aio_loop
        if loop is None:
            coro.close()
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        if done is not None:
            def _cb(f):
                try:
                    done(f.result())
                except Exception as ex:
                    logger.info(f"  [gui] action failed: {ex!r}")
            fut.add_done_callback(_cb)
        return fut

    def refresh_boost(self):
        """Repaint the Boost tab from the live multipliers. Cheap and idempotent."""
        ui = getattr(self, "ui", None)
        if ui is None or not hasattr(ui, "update_boost"):
            return
        try:
            ui.update_boost({
                "enc": self.enc_mult, "xp": self.xp_mult,
                "gil": self.gil_mult, "monster": self.monster_mult,
                "boss": self.boss_mult,
                "attached": self.psp is not None,
            })
        except Exception as ex:
            logger.warning(f"Boost refresh failed: {ex}")

    # ---------------- GUI / tracker ----------------
    def make_gui(self):
        """Wrap this AP build's stock client window with our Tracker + Shops tabs.
        Import ApGui lazily: it pulls in kivy, which must not be a hard dependency
        of the headless/CLI path (gui_enabled False)."""
        ui = super().make_gui()
        try:
            from .ApGui import make_manager
            return make_manager(ui)
        except Exception as ex:
            logger.warning(f"Tracker GUI unavailable, using the stock window: {ex}")
            return ui

    def _effective_table(self, name):
        """The byte block for a shuffle table as this seed actually uses it: the
        slot_data patch if the table was shuffled, else the vanilla bytes. Used to
        read magic-shop contents (which spells + their level/price) without a live
        RAM read."""
        from .. import rando_data as RD
        van = RD.VANILLA.get(name)
        for nm, _v, patched in RANDO.patches_from_slot_data(self.slot_data):
            if nm == name:
                return patched
        return van

    def _auto_sell_value(self, cat, gid):
        """Gil to pay instead of delivering this item (auto_sell_unusable_items),
        or 0 to grant it normally. Only weapons/armor no party job -- current OR
        promoted -- can ever equip, and never activatable gear; the whole rule
        lives in rando.gear_auto_sell_value against the seed's EFFECTIVE tables,
        so the verdict honors shuffle_who_equips_what + the price shuffles.

        Judged from slot_data party_jobs (base ids): promotion only ever widens
        equippability to "now", so base ids are safely conservative -- no live
        party read, which is what lets _scout bake own chests as gil pre-boot.
        A choose-at-game-start slot makes usability_jobs return every base job,
        suppressing selling for the seed. Memoised: _scout asks across ~250
        chests and the answer is constant per (seed, connection).

        AP shop/hint PLACEHOLDER ids are exempt outright. force_shop_ap_prices
        zeroes their equip masks as the buy-dupe guard and overwrites +20 with
        the AP offer price, so their record reads "nobody can equip this, worth
        the offer price" -- both halves are lies about the real item, and the
        client restores the vanilla masks after purchase (_shop_sync_masks).
        Judging one would sell a perfectly equippable bonus-dungeon drop."""
        if not self.auto_sell_unusable or cat not in (D.CAT_WEAPON, D.CAT_ARMOR):
            return 0
        if (cat, gid) in self._shop_equip_gids:
            return 0
        key = (cat, gid)
        val = self._auto_sell_cache.get(key)
        if val is None:
            blk = self._effective_table(
                "weapons" if cat == D.CAT_WEAPON else "armor")
            val = RANDO.gear_auto_sell_value(
                blk, gid, RANDO.usability_jobs(self.party_jobs))
            self._auto_sell_cache[key] = val
        return val

    # store_row -> (kind tag, ff1_data category) for native shop stock.
    _SHOP_ROW = {0: ("weapon", D.CAT_WEAPON), 1: ("armor", D.CAT_ARMOR),
                 2: ("item", D.CAT_ITEM)}

    def _shops_payload(self):
        """Pure-data snapshot for the Shops tab: per town, its shops with their
        full native stock AND their scouted AP offers, plus the native white/black
        magic-shop spells. The GUI only renders this; all the AP lookups (item
        name, owning player, quality flag, checked-state) and table decoding
        happen here where the data is."""
        checked = set(self.checked_locations) | set(self.sent_locations)
        shops_tbl = self._effective_table("shops")
        magic_tbl = self._effective_table("magic_info")
        # Buy prices are per ITEM, not per shop -- shop_stock_for_city reads them
        # out of these three (see rando._STOCK_PRICE).
        price_tbls = {nm: self._effective_table(nm)
                      for nm in ("weapons", "armor", "item_buy_prices")}
        # shop index -> its offer price list, from slot_data (empty if the seed
        # doesn't inject AP items into shops).
        prices_by_shop = {s: list(pr) for (s, _c, _g, pr) in self.shop_slots}
        any_ap = bool(prices_by_shop)
        # Which stores carry placeholders in their tail rows, and how many --
        # those shelves render as AP offers, never as native stock. Count the
        # rows the SEED authored, not the ones still listed in game: `shops_tbl`
        # is the static slot_data block, where every authored tail row is always
        # present. Counting live rows instead (pre-v267) un-masked each sold row
        # and the tab showed the placeholder's own identity as native stock --
        # Crescent Lake's item shop grew a Megalixir and a Golden Apple once its
        # two AP offers were bought.
        # Hint rows share that tail, so they are part of the same count -- miss
        # them and the tab would show a hint's placeholder as native stock.
        ap_ordinals = {s: len(self.shop_rows.get(s, ())) + len(self.hint_rows.get(s, ()))
                       for s in set(prices_by_shop) | set(self.hint_rows)}
        # Usability shading: which gear this party can equip and which spells it
        # can learn, now / after a class change / never. Party is live when the
        # game is attached, else the seed's base jobs from slot_data.
        jobs, mlv, live = self._party_for_shops()
        known_jobs = [j for j in jobs if j is not None]
        learn_tbl = self._effective_magic_learn() if known_jobs else b""

        towns = []
        for city, name, shop_idxs in TRACKER.SHOP_TOWNS:
            stock = self._city_stock(shops_tbl, city, price_tbls, ap_ordinals,
                                     jobs)
            shops = []
            for s in shop_idxs:
                # store_row 0/1/2 -> weapon/armor/item, from the AP-slot table.
                row = RANDO.SHOP_AP_SLOTS[s][1] \
                    if s < len(RANDO.SHOP_AP_SLOTS) else None
                kind = self._SHOP_ROW.get(row, ("other", 0))[0]
                offers = []
                for k in range(len(prices_by_shop.get(s, []))):
                    lid = ID.shop_loc_id(s, k)
                    info = self.locations_info.get(lid)
                    if info is None:
                        item_name, who, quality = "(unknown)", "", "filler"
                    else:
                        try:
                            item_name = self.item_names.lookup_in_slot(
                                info.item, info.player) or "(unknown)"
                        except Exception:
                            item_name = "(unknown)"
                        who = self.player_names.get(info.player, "")
                        if who == self.username:
                            who = ""            # your own item -> no "(player)" tag
                        fl = getattr(info, "flags", 0) or 0
                        quality = _flags_quality(fl)
                    offers.append({
                        "item": item_name, "player": who, "quality": quality,
                        "price": prices_by_shop[s][k],
                        "found": lid in checked,
                    })
                # Hint rows: what each still-listed one reveals and what it
                # costs. `spent` covers both halves of gone -- bought, or its
                # tile fully found -- so the tab agrees with the shelf.
                hints = []
                for k, (_c, _g, price, label, lids) in enumerate(
                        self.hint_rows.get(s, ())):
                    hints.append({
                        "place": label, "short": HINTS.short_name(label),
                        "price": price, "checks": len(lids),
                        "left": sum(1 for l in lids if l not in checked),
                        "spent": self._hint_done(s, k),
                    })
                bought = (bool(offers) and all(o["found"] for o in offers)
                          and all(h["spent"] for h in hints))
                shops.append({"name": TRACKER.shop_display(s), "kind": kind,
                              "bought": bought, "offers": offers,
                              "hints": hints,
                              "stock": stock.get(row, [])})
            # The Onrac desert caravan: native-only (no AP shelf), and not inside
            # the town, so it is tagged for the GUI to say so.
            if stock.get("caravan"):
                shops.append({"name": "Onrac Caravan", "kind": "caravan",
                              "bought": False, "offers": [],
                              "stock": stock["caravan"],
                              "note": "desert, needs Faerie's Bottle"})
            magic = None
            if shops_tbl and magic_tbl:
                # store-city index == our town city id for Cornelia..Gaia (0..6).
                try:
                    magic = RANDO.magic_shops_for_city(shops_tbl, magic_tbl, city)
                except Exception:
                    magic = None
                if magic and learn_tbl:
                    for school in magic.values():
                        for sp in school:
                            sp["use"] = self._spell_use(learn_tbl, magic_tbl,
                                                        sp["index"], jobs, mlv)
            towns.append({
                "city": city, "name": name, "shops": shops, "magic": magic,
                "visited": city in self._towns_visited,
            })
        return {"any_ap": any_ap, "towns": towns,
                "party": {"live": live, "known": len(known_jobs),
                          "shaded": bool(known_jobs)}}

    @staticmethod
    def _spell_use(learn_tbl, magic_tbl, idx, jobs, mlv):
        try:
            return RANDO.spell_learn_state(learn_tbl, magic_tbl, idx, jobs, mlv)
        except Exception:
            return "now"            # never fade on a decode failure

    def _party_for_shops(self):
        """(jobs, magic_levels, live) for the Shops tab's usability shading.

        LIVE reading (classes + magic level, refreshed by _shop_hint_loop) when
        the game is attached. Otherwise the seed's slot_data party_jobs: base
        classes only, so promotion state and magic level are unknown -- which the
        state helpers treat as "do not fade on a guess"."""
        live = self._party_view
        if live:
            return live["jobs"], live["magiclv"], True
        return list(self.party_jobs or []), None, False

    async def _read_party_view(self):
        """One contiguous read of the class array + party records -> current class
        and magic level per member. Same read shape the steal loop uses:
        class_addr_sa(ci) is party_addr_sa(ci) - 2, so the four class bytes sit two
        bytes ahead of the records ([[class-byte]])."""
        blk = await self.psp.read(self.sa(D.class_addr_sa(0)),
                                  2 + D.PARTY_COUNT * D.PARTY_STRIDE)
        jobs, mlv = [], []
        for ci in range(D.PARTY_COUNT):
            jobs.append(blk[ci * D.PARTY_STRIDE])
            mlv.append(blk[2 + ci * D.PARTY_STRIDE + D.P_MAGICLV])
        if not all(0 <= j <= D.BLACK_WIZARD for j in jobs):
            return None                 # title screen / uninitialised party
        return {"jobs": jobs, "magiclv": mlv}

    def _effective_magic_learn(self):
        """The seed's final magic_learn: the shuffled table with the monk/thief
        dabble learn bits ORed on, exactly as the bake writes them."""
        learn = bytearray(self._effective_table("magic_learn") or b"")
        if not learn:
            return learn
        if ((self.slot_data or {}).get("on_disc") or {}) \
                .get("monk_thief_dabble_in_magic"):
            try:
                IP.apply_dabble_learn_overlay(learn, self._effective_table("shops"))
            except Exception:
                pass
        return learn

    def _city_stock(self, shops_tbl, city, price_tbls, ap_ordinals, jobs=()):
        """{store_row -> [{"name","price","gid","use"}]} of a town's NATIVE shop
        stock (plus "caravan" for Onrac), named, priced, and tagged with whether
        this party can equip it ("now"/"later"/"never"; consumables are always
        "now"). The AP shelves are already dropped by rando.shop_stock_for_city.
        Decoding is best-effort: a table this seed doesn't ship just yields an
        empty town rather than a dead tab."""
        if not shops_tbl:
            return {}
        try:
            raw = RANDO.shop_stock_for_city(
                shops_tbl, city, price_tbls, ap_ordinals=ap_ordinals,
                caravan=(city == 5))
        except Exception as ex:
            logger.warning(f"Shop stock decode failed for city {city}: {ex}")
            return {}
        known = [j for j in jobs if j is not None]
        # Activatable gear is usable by ANYONE as a battle item, so it never
        # shades "never" -- gear_equip_state returns "now" for it, or "later"
        # while the equipment_runes gate still locks activation (rune_ok).
        rune_ok = (not self.equipment_runes_required
                   or self._rune_count() >= self.equipment_runes_required)
        out = {}
        for row, entries in raw.items():
            gear_row = row in (0, 1)
            cat = self._SHOP_ROW[2 if row == "caravan" else row][1]
            gear_tbl = price_tbls.get("weapons" if row == 0 else "armor")
            out[row] = []
            for e in entries:
                use = "now"          # consumables: anyone can use them
                if gear_row and known and gear_tbl:
                    try:
                        use = RANDO.gear_equip_state(gear_tbl, e["gid"], known,
                                                     rune_ok=rune_ok)
                    except Exception:
                        use = "now"  # never fade on a decode failure
                out[row].append({"name": self._game_item_name(cat, e["gid"]),
                                 "price": e["price"], "gid": e["gid"],
                                 "use": use})
        return out

    def refresh_shops(self):
        """Rebuild + repaint the Shops tab. Cheap; safe to call on any event that
        can change visited towns, scouted contents, or checks."""
        ui = getattr(self, "ui", None)
        if ui is None or not hasattr(ui, "update_shops"):
            return
        try:
            ui.update_shops(self._shops_payload())
        except Exception as ex:
            logger.warning(f"Shops refresh failed: {ex}")

    def _tracker_item_names(self):
        """Names of every AP item this slot has received.

        UNION of the sticky _ever_won and the live items_received, because neither
        alone is right here:
          - items_received alone is emptied for the duration of a disconnect, which
            would blink every area to out-of-logic until the resync lands.
          - _ever_won alone is only refreshed when _synced() is CALLED, and its
            callers are the grant/strip loops -- which only run once the PPSSPP
            bridge is up. With no game attached it stays empty and the tracker
            claims you own nothing (seen live 2026-07-17).
        The union is sticky AND immediate. Safe precisely because the tracker only
        DISPLAYS: the key-item-loss-on-disconnect rule bars items_received from
        driving destructive decisions (native strips, re-grants), not from being
        read. Those loops still gate on _synced()."""
        names = set()
        for iid in set(self._ever_won) | {it.item for it in self.items_received}:
            try:
                nm = self.item_names.lookup_in_slot(iid, self.slot)
            except Exception:
                continue
            if nm:
                names.add(nm)
        return names

    def _tablet_count(self):
        """Sticky count of Lute Tablet copies received (lute_tablets seeds). All
        copies share one AP item id, so the sticky-SET _ever_won can't count them;
        count the live items_received list and keep a high-water instead (the list
        empties during a disconnect -- the assembled Lute must never un-assemble)."""
        cnt = sum(1 for it in self.items_received if ID.is_tablet(it.item))
        self._tablet_hw = max(self._tablet_hw, cnt)
        return self._tablet_hw

    def _rune_count(self):
        """Sticky count of Equipment Rune copies received (equipment_runes seeds).
        Same high-water scheme as _tablet_count: all copies share one AP item id
        and items_received empties on disconnect, so keep the max ever seen (the
        assembled Equipment Rune Key must never un-assemble).

        Also the announce point for a pickup: a rune receipt is otherwise
        SILENT -- _ap_item_to_game returns (None, None, 0) for it and the
        cat-is-None branch only logs when qty > 0 -- which left the player with
        no way to see progress while the in-game line was suppressed. Edge-gated
        on the high-water, so it prints once per new rune and once on connect."""
        cnt = sum(1 for it in self.items_received if ID.is_rune(it.item))
        self._rune_hw = max(self._rune_hw, cnt)
        need = self.equipment_runes_required
        if need and self._rune_hw > self._rune_logged_hw:
            self._rune_logged_hw = self._rune_hw
            logger.info("  [equipment-runes] Equipment Rune held: "
                        f"{min(self._rune_hw, need)} of {need}"
                        + (" -- activatable equipment is LIVE"
                           if self._rune_hw >= need else ""))
        return self._rune_hw

    def _shard_count(self):
        """Sticky count of Levistone Shard copies received (levistone_shards
        seeds). Same high-water scheme as _tablet_count: all copies share one AP
        item id and items_received empties on disconnect, so keep the max ever
        seen (the assembled Levistone / raised airship must never un-assemble).
        Also the announce point per new shard, same rationale as _rune_count."""
        cnt = sum(1 for it in self.items_received if ID.is_shard(it.item))
        self._shard_hw = max(self._shard_hw, cnt)
        need = self.levistone_shards_required
        if need and self._shard_hw > self._shard_logged_hw:
            self._shard_logged_hw = self._shard_hw
            logger.info("  [levistone-shards] Levistone Shard held: "
                        f"{min(self._shard_hw, need)} of {need}"
                        + (" -- Levistone assembled, the airship rises!"
                           if self._shard_hw >= need else ""))
        return self._shard_hw

    def _key_won(self, kid):
        """Is cat-0 key item `kid` "won" for strip/restore/hold purposes?
        Normally = its AP item id is in the sticky _ever_won. Two piece modes
        DERIVE their key instead of finding it, so the AP item never exists and
        the _ever_won test would read permanently un-won -- which would strip
        the assembled item's bits forever (the Levistone map-reset row's restore
        is what keeps the airship flying): kid 1 (Lute) from lute_tablets, kid
        11 (Levistone) from levistone_shards."""
        if kid == 1 and self.lute_tablets_required:
            return self._tablet_count() >= self.lute_tablets_required
        if kid == 11 and self.levistone_shards_required:
            return self._shard_count() >= self.levistone_shards_required
        return ID.item_id(D.CAT_KEY, kid) in self._ever_won

    def refresh_tracker(self):
        """Recompute + repaint. Cheap (a few hundred dict lookups) and idempotent,
        so it's fine to call from every event that can move the needle."""
        ui = getattr(self, "ui", None)
        if ui is None or not hasattr(ui, "update_tracker"):
            return
        try:
            state = TRACKER.evaluate(
                self._tracker_item_names(),
                set(self.missing_locations) | set(self.checked_locations),
                set(self.checked_locations) | set(self.sent_locations),
                self.slot_data,
                tablet_count=self._tablet_count(),
                rune_count=self._rune_count(),
                shard_count=self._shard_count(),
            )
        except Exception as ex:
            logger.warning(f"Tracker evaluation failed: {ex}")
            return
        ui.update_tracker(state)

    def _register_dyn_chest_names(self):
        """Teach the LOCAL name lookup the dynamic-chest locations this seed
        created but the datapackage does not carry.

        __init__.py registers only `range(_floors)` ordinals per bonus dungeon
        (5/10/20/40) into LOCATION_NAME_TO_ID, while create_regions creates
        locations all the way up to the yaml cap (up to 100). Every ordinal in
        between is a REAL location -- it generates, it holds items, its check
        sends -- but its id is not in the datapackage, so the server, the
        tracker and every hint render it as "Unknown location (ID: 17371219)".
        Live 2026-08-08: Prime's Oxyale, a progression item, was hinted at
        exactly that id (Hellfire Chasm ordinal 19, past its 10 floors).

        Fixing the id registration would change the datapackage and break every
        in-flight seed, so this is deliberately CLIENT-SIDE ONLY: it repairs the
        display for seeds that already exist, and changes nothing about
        generation, ids, or what gets sent.

        Ordinals at or past DYNCHEST_STRIDE are INCLUDED. The first version
        skipped them, reasoning that they alias into the next dungeon's id
        block. That was wrong: `dyn_chest_loc_id` is what the GENERATOR used
        too, so such an id still denotes exactly one real location unless some
        other (dungeon, ordinal) pair lands on the same number -- and with only
        four dungeons, a cap above the stride usually collides with nothing at
        all. Live 2026-08-09: Prime goaled and the release announced 35
        locations as "Unknown location (ID: 17371393…17371427)", every one of
        them Whisperwind Cove ordinals 65..99, all unambiguous. So claim the
        ids, but compute the claims FIRST and drop any that more than one pair
        wants (dungeon 0 with a cap of 100 really would collide with dungeon
        1). A colliding id is genuinely unnameable; an uncontested one is not.

        Note this only fixes the DISPLAY. `_bonus_dyn_loop.next_ordinal` scans
        `range(DYNCHEST_STRIDE)`, so past-stride chests still never send a
        check in-game; they resolve only when the server releases them. See the
        dyn-chest-caps memory.

        Hints already printed before this runs keep their old text; anything
        rendered afterwards resolves."""
        try:
            caps = self.bonus_dyn_caps or {}
            game = getattr(self, "game", None)
            if not caps or not game:
                return
            # Every (dungeon, ordinal) this seed's caps actually created, keyed
            # by the id it resolves to -- including ordinals below the floor
            # count, because those are what a past-stride ordinal would collide
            # WITH and the collision has to be visible from both sides.
            claims = {}
            for dg, dname, floors, _defcap, _tok, _attr in LOGIC.BONUS_DUNGEONS:
                for o in range(int(caps.get(dg, 0))):
                    claims.setdefault(ID.dyn_chest_loc_id(dg, o), []).append(
                        (dname, floors, o))
            extra, ambiguous = {}, 0
            for lid, owners in claims.items():
                if len(owners) != 1:
                    ambiguous += 1          # two locations share this id
                    continue
                dname, floors, o = owners[0]
                if o < floors:
                    continue                # datapackage already names it
                extra[LOGIC.dyn_chest_location_name(dname, o)] = lid
            if ambiguous:
                logger.info(f"  [dyn_names] {ambiguous} dynamic-chest id(s) are "
                            f"claimed by two dungeons at these caps and stay "
                            f"unnamed")
            if not extra:
                return
            # NameLookupDict stores id -> name and rebuilds from name -> id, so
            # invert what the datapackage gave us, merge, and hand the whole
            # table back. Anything already named wins (never overwrite the
            # server's own naming with ours).
            current = self.location_names[game]
            merged = {name: code for code, name in current.items()}
            added = {n: i for n, i in extra.items() if n not in merged}
            if not added:
                return
            merged.update(added)
            self.location_names.update_game(game, merged)
            logger.info(f"  [dyn_names] named {len(added)} dynamic-chest "
                        f"locations the datapackage does not carry (bonus "
                        f"dungeon caps exceed the registered floor counts)")
        except Exception as e:
            logger.info(f"  [dyn_names] {e!r} -- dynamic chests past a "
                        f"dungeon's floor count will keep showing as "
                        f"'Unknown location' in hints")

    def on_package(self, cmd, args):
        super().on_package(cmd, args)
        if cmd == "Connected":
            sd = args.get("slot_data") or {}
            self.enc_mult = float(sd.get("encounter_rate", 1.0))
            self.xp_mult = float(sd.get("xp_boost", 1.0))
            self.gil_mult = float(sd.get("gil_boost", 1.0))
            self.boss_mult = float(sd.get("boss_difficulty", 1.0))
            self.monster_mult = float(sd.get("monster_power", 1.0))
            self.slot_data = sd   # carries Tier-A shuffle tables (rando.SLOT_KEY)
            # Random starting party: 4 entries (job id 0..5, or None = vanilla).
            self.party_jobs = list(sd.get("party_jobs") or [None, None, None, None])
            self.thief_steal = bool(sd.get("thief_steal", False))
            # DefaultOnToggle: absent means "seed predates the option", not "off".
            self.auto_sell_unusable = bool(
                sd.get("auto_sell_unusable_items", True))
            self._auto_sell_cache = {}    # tables/jobs may differ per connection
            self.naked_monks = bool(sd.get("naked_monks", False))
            # Starting gil (500 = vanilla). None for a pre-option seed -> the
            # one-shot loop stays out of the way entirely.
            sg = sd.get("starting_gil")
            self.starting_gil = None if sg is None else int(sg)
            # Per-dungeon dynamic-chest AP-check caps: {dungeon idx -> count}. slot_data
            # keys are strings ("0".."3"); normalize to int keys. Missing/0 = that
            # dungeon's procedural chests are never hooked (stay vanilla).
            self.bonus_dyn_caps = {int(k): int(v)
                                   for k, v in (sd.get("bonus_dyn_caps") or {}).items()}
            self._register_dyn_chest_names()
            # bonus_dungeon_crystals: crystals activate on a Soul-of-Chaos superboss
            # kill instead of the Fiend. Default False keeps old seeds byte-identical.
            self.bonus_dungeon_crystals = bool(sd.get("bonus_dungeon_crystals", False))
            # lute_tablets: tablets needed before the Lute possession bit is set
            # (0 = option off; the Lute arrives as a normal key-item grant).
            self.lute_tablets_required = int(sd.get("lute_tablets_required") or 0)
            # equipment_runes: runes needed before story flag 62 is set (the
            # on-disc usability gate then allows activating equipment; 0 = off).
            self.equipment_runes_required = int(
                sd.get("equipment_runes_required") or 0)
            # levistone_shards: shards needed before the client grants the real
            # Levistone (possession + airship bits; 0 = off, normal AP item).
            self.levistone_shards_required = int(
                sd.get("levistone_shards_required") or 0)
            # Shop AP stock. A shop lists its offers in PARALLEL -- one shelf row
            # per offer, each with its OWN placeholder item id, because price,
            # name and description all live on the item record rather than the
            # shop row. That is also what makes purchases attributable: the buy
            # mailbox reports (store, cat, gid), so a distinct gid per row names
            # the row exactly.
            #
            # shop_ap_rows: [[shop, cat, base_width, [[gid, price], ...]], ...]
            # Legacy shop_ap ([shop, cat, gid, [prices]]) is a seed generated
            # before parallel rows: one row, and the store's base width is
            # whatever is on the shelf minus that row.
            # v2 shared tails: the row gids are RESERVED_SHOP_PLACEHOLDERS
            # constants shared across stores. Purchase attribution needs no
            # change (the mailbox consume is (store, cat, gid)-keyed), but
            # name/desc/price authoring becomes PER TOWN: the same gid means a
            # different row in every town, so the banks and price tables carry
            # exactly one town's identity at a time (street map id latch).
            self._shared_tails = bool(sd.get("shop_ap_shared"))
            self._cur_town = None
            self._town_prices_stamped = None
            self.shop_rows, self._shop_base = RANDO.parse_shop_ap_slot_data(sd)
            # Which row of which shop a purchased (cat, gid) belongs to.
            self._shop_gid_row = {(s, c, g): k
                                  for s, rws in self.shop_rows.items()
                                  for k, (c, g, _p) in enumerate(rws)}
            # Derived view kept in the historical 4-tuple shape for the readers
            # that only care about the shop and its offer COUNT (scout set, town
            # hint loop, Shops tab payload).
            self.shop_slots = [(s, rws[0][0], rws[0][1], [p for _c, _g, p in rws])
                               for s, rws in sorted(self.shop_rows.items()) if rws]
            # Per-seed weapon/armor placeholder (cat, gid) set -- replaces the
            # static RANDO.SHOP_AP_EQUIP_GIDS now that the exotic/priceless loot
            # pool can choose placeholders per seed. Every ROW's gid, not just
            # the first: each is a real item id whose equip mask is zeroed while
            # it is still buyable.
            # Hint rows share the AP tail: one more shelf row per store, each
            # with its own placeholder id and baked price, but NO location id --
            # buying one scouts a tracker tile's contents as a hint instead of
            # sending a check (see hints.py). Rows are [[shop, cat, base_width,
            # [[gid, price, product label, [location ids]], ...]], ...]; a seed
            # from before the feature ships no key and reads as no hint rows.
            self.hint_rows, hint_base = RANDO.parse_hint_shop_slot_data(sd)
            # One tail, one base width. A store with hints but no AP offer (the
            # seed set shop_ap_offers to 0) is only in the hint copy.
            for _s, _bw in hint_base.items():
                self._shop_base.setdefault(_s, _bw)
            self._hint_gid_row = {(s, c, g): k
                                  for s, rws in self.hint_rows.items()
                                  for k, (c, g, _p, _l, _ids) in enumerate(rws)}
            self._hint_bought = set()
            self._shop_equip_gids = frozenset(
                [(c, g) for rws in self.shop_rows.values()
                 for (c, g, _p) in rws if c in (2, 3)]
                # Hint placeholders are gear ids too, and the bake zeroes their
                # equip masks the same way -- leave them out and a bonus-dungeon
                # drop of that weapon stays unequippable for the whole seed.
                + [(c, g) for rws in self.hint_rows.values()
                   for (c, g, _p, _l, _ids) in rws if c in (2, 3)])
            if self.hint_rows:
                # Which hint rows are already bought is SERVER state: nothing in
                # the save records it, and the hint it bought is durable on the
                # server. Read it back (and subscribe) so a reconnect does not
                # put a paid-for row back on the shelf.
                asyncio.create_task(self._hint_store_fetch())
            # death_link: opt-in. Join the DeathLink bounce group (tag) so we
            # both receive others' deaths and may send our own party wipes.
            self.death_link_on = bool(sd.get("death_link", False))
            self.death_link_severity = int(sd.get("death_link_severity") or 3)
            if self.death_link_on:
                asyncio.create_task(self.update_death_link(True))
            # Quality-of-life Config-menu defaults, written once at new game.
            # Absent keys keep the constructor defaults, so an older seed's
            # slot_data still behaves exactly as it did before these existed.
            self.auto_dash = bool(sd.get("auto_dash", True))
            self.message_speed = int(sd.get("message_speed",
                                            D.MSG_SPEED_DEFAULT))
            self.cursor_mode = int(sd.get("cursor_mode",
                                          D.CURSOR_MODE_DEFAULT))
            self.sent_locations.update(args.get("checked_locations", []))
            logger.info(f"Scaling: encounter {self.enc_mult * 100:g}%, "
                        f"xp {self.xp_mult * 100:g}%, gil {self.gil_mult * 100:g}%, "
                        f"monster {self.monster_mult * 100:g}%, "
                        f"boss {self.boss_mult * 100:g}%")
            if not self._bridge_started:
                self._bridge_started = True
                asyncio.create_task(self._start_bridge_guarded())
        # Bought hint rows live in this slot's DataStorage key (see
        # _hint_store_key): `Retrieved` answers our Get at connect, `SetReply`
        # is the subscription echo -- including our own writes, which is what
        # confirms a purchase actually landed on the server.
        if cmd == "Retrieved" and self.hint_rows:
            self._hint_apply_bought(
                (args.get("keys") or {}).get(self._hint_store_key()))
        elif cmd == "SetReply" and self.hint_rows:
            if args.get("key") == self._hint_store_key():
                self._hint_apply_bought(args.get("value"))
        # A check anywhere can empty a hinted tile, which retires its row.
        if cmd in ("Connected", "RoomUpdate") and self.hint_rows:
            self._hint_dirty = True
        # Refresh LAST: the Connected branch above is what installs slot_data, and
        # the tracker's rules are derived from it. Refreshing first would evaluate
        # the seed's very first paint against default (all-toggles-off) logic.
        # RoomUpdate covers checks that never pass through our own send path
        # (another client on the slot, a !release). LocationInfo is the scout reply
        # that fills self.locations_info -> the Shops tab's offer contents.
        if cmd in ("Connected", "ReceivedItems", "RoomUpdate"):
            self.refresh_tracker()
        if cmd in ("Connected", "ReceivedItems", "RoomUpdate", "LocationInfo"):
            self.refresh_shops()
        if cmd == "Connected":
            # Connected is where the seed's own scaling lands (enc/xp/gil/boss above),
            # so the Boost tab has nothing real to show until now.
            self.refresh_boost()

    # ---------------- PPSSPP side ----------------
    async def _resolve_remote_port(self, host, port):
        """Debugger port on a remote device. An explicit port wins; else ask
        the PPSSPP match server (a device with the remote debugger on
        registers its LAN ip+port there -- covers Android's ephemeral default
        RemoteISOPort=0), else fall back to the pinned local default."""
        from .launcher import DEBUG_PORT
        if port:
            return port
        from .ppsspp_ws import discover
        try:
            found = await asyncio.get_event_loop().run_in_executor(None, discover)
        except Exception:
            found = []
        for ip, p in found:
            if ip == host:
                logger.info(f"  [remote] match server reports {host}:{p}")
                return p
        return DEBUG_PORT

    async def _connect_psp(self, remote=None):
        # Retry UNTIL the debugger answers (or PPSSPP's process dies / the
        # client closes). A fresh boot of a newly baked ISO can stall PPSSPP's
        # WS server for minutes (JIT warmup + ISO load starve the accept
        # thread); the old fixed 30-attempt budget expired during that window
        # and the whole session ran with NO bridge -- party jobs / flags /
        # chests silently dead (the "features don't work on first start" bug).
        # local_only: discover() is a sync 5s web call that blocks the event
        # loop and is useless here -- the launcher guarantees a LOCAL PPSSPP.
        # Two sockets: bp control must not block grants.
        #
        # remote=(host, port_or_None): drive a PPSSPP on ANOTHER device
        # (Android phone / handheld) over its WS debugger. There is no local
        # process to launch, watch, or attach to, so the proc-gone give-up
        # below is skipped (retry forever -- enabling the debugger on the
        # device can take the player a while) and memory I/O stays on the raw
        # WS sockets: HybridPSP's ReadProcessMemory would attach to a LOCAL
        # PPSSPP -- the wrong instance -- and its no-process fail-fast would
        # kill every op when none is running here.
        from .launcher import ppsspp_process_running
        attempt = 0
        proc_gone = 0
        while not self.exit_event.is_set():
            try:
                if remote:
                    host, want_port = remote
                    port = await self._resolve_remote_port(host, want_port)
                    self.psp = await PPSSPP.connect(ip=host, port=port)
                    self.psp_bp = await PPSSPP.connect(ip=host, port=port)
                    try:
                        self.psp_scan = await PPSSPP.connect(ip=host, port=port)
                    except Exception:
                        self.psp_scan = self.psp   # degraded: shared socket
                    game = await self.psp.game_status()
                    logger.info(f"PPSSPP connected (remote {host}:{port}), "
                                f"game={game}")
                    logger.info("  [bridge] memory I/O via WS debugger "
                                "(remote mode)")
                    return True
                self.psp = await PPSSPP.connect(local_only=True)
                self.psp_bp = await PPSSPP.connect(local_only=True)
                # Third socket DEDICATED to big background scans (read_chunked
                # sweeps). Each rpc serializes on its socket's lock, so a slow
                # multi-MB scan on the main socket used to starve grants/flags/
                # movement into TimeoutError for its whole duration. Scans now
                # queue only behind each other.
                try:
                    self.psp_scan = await PPSSPP.connect(local_only=True)
                except Exception:
                    self.psp_scan = self.psp   # degraded: shared socket
                game = await self.psp.game_status()
                logger.info(f"PPSSPP connected, game={game}")
                # Wrap memory I/O in the direct process-memory transport: the
                # WS debugger stalls (minutes) on fresh boots / busy scenes,
                # which used to leave save-block location, party jobs, flags
                # and grants dead. WS stays underneath for breakpoints,
                # game_status, and as automatic fallback (see ppsspp_mem).
                from .ppsspp_mem import PPSSPPMem, HybridPSP
                mem = PPSSPPMem()
                if mem.attach():
                    logger.info(f"Direct memory bridge attached "
                                f"(pid {mem.pid}, base {mem.host_base:#x})")
                else:
                    logger.info("Direct memory bridge not attached yet -- "
                                "will keep trying; WS carries memory I/O "
                                "until then.")
                # Identity test guards the degraded case above: if the third
                # socket failed, psp_scan was ALIASED to psp, and the alias
                # must stay one shared HybridPSP -- wrapping it again would
                # double-wrap the same WS socket.
                scan_ws = self.psp_scan
                self.psp = HybridPSP(mem, self.psp)
                self.psp_scan = (self.psp if scan_ws is self.psp.ws
                                 else HybridPSP(mem, scan_ws))
                return True
            except Exception as e:
                # Half-open sockets from a partially successful attempt must
                # not leak into the next one.
                for sock in (self.psp, self.psp_bp, self.psp_scan):
                    if sock is not None:
                        try:
                            await sock.close()
                        except Exception:
                            pass
                self.psp = self.psp_bp = self.psp_scan = None
                if attempt % 15 == 0:   # first try + every ~30s
                    if remote:
                        logger.info(
                            f"Waiting for the remote PPSSPP debugger at "
                            f"{remote[0]}... ({e!r}) -- on the device: load "
                            f"the game, then Settings > Tools > Developer "
                            f"tools > 'Allow remote debugger' ON.")
                    else:
                        from .launcher import port_squatter, _squatter_msg
                        sq = port_squatter()
                        if sq:
                            logger.error(_squatter_msg(sq))
                        else:
                            logger.info(
                                f"Waiting for PPSSPP debugger... ({e!r})")
                attempt += 1
                if not remote:
                    # Only give up when PPSSPP itself is gone (a few consecutive
                    # checks: tasklist can transiently miss a live process).
                    # Remote mode never gives up: there is no local process to
                    # watch, and the device may simply not be ready yet.
                    alive = await asyncio.get_event_loop().run_in_executor(
                        None, ppsspp_process_running)
                    proc_gone = 0 if alive else proc_gone + 1
                    if proc_gone >= 5:
                        logger.error("PPSSPP process is gone -- bridge not "
                                     "started. Reconnect to the server to "
                                     "relaunch it.")
                        return False
                await asyncio.sleep(2)
        return False

    async def read_gil(self):
        return await self.psp.read_u32(self.sa(D.GIL_ADDR_SA))

    async def grant_key_item(self, item_id, present=True, quiet=False):
        try:
            addr, mask = D.key_item_bit(item_id)
        except ValueError as e:
            logger.info(f"  [skip] {e}")
            return False
        addr = self.sa(addr)
        cur = (await self.psp.read(addr, 1))[0]
        new = (cur | mask) if present else (cur & ~mask)
        if new != cur:
            await self.psp.write(addr, bytes([new]))
        # Progression items: the possession bit above is inventory DISPLAY only.
        # The real functional gate (river sailing, airship raised, Unne translate,
        # ...) is a separate event-register bit -- sync it so a delivered item
        # actually works and a removed one stops. Only CONFIRMED-split items are in
        # the table (Canoe/Levistone/Rosetta); no overworld reload needed.
        fb = D.KEY_ITEM_FUNCTION_BITS.get(item_id)
        if fb:
            faddr, fmask = self.sa(fb[0]), fb[1]
            fcur = (await self.psp.read(faddr, 1))[0]
            fnew = (fcur | fmask) if present else (fcur & ~fmask)
            if fnew != fcur:
                await self.psp.write(faddr, bytes([fnew]))
        # On-disc gate-split shadow flag (v260, e.g. Star Ruby -> Titan): on a
        # baked ISO the REAL gate reads this private flag, not the vanilla
        # function bit above. Set/clear it in lockstep. Harmless on an old ISO
        # (unused flag); reasserted while owned in _npc_loop's func-reassert.
        sb = D.GATE_SPLIT_SHADOW_BITS.get(item_id)
        if sb:
            saddr, smask = self.sa(sb[0]), sb[1]
            scur = (await self.psp.read(saddr, 1))[0]
            snew = (scur | smask) if present else (scur & ~smask)
            if snew != scur:
                await self.psp.write(saddr, bytes([snew]))
        if not quiet:
            name = D.KEY_ITEMS.get(item_id, f"id{item_id}")
            logger.info(f"  {'granted' if present else 'stripped'} key item {name}")
        # Tell the NPC map-entry native-refresh detector this rise was OUR write,
        # not the NPC firing -- a save-reload re-grant while standing in the NPC's
        # map false-fired the robot check (live 2026-07-13). The npc loop consumes
        # this flag by re-clearing the bits instead of sending the check.
        if present and any(r[4] == item_id for r in D.NPC_MAP_RESET):
            self._npc_reset_selfgrant.add(item_id)
        return True

    async def grant_item(self, category, item_id, qty=1):
        if category == D.CAT_KEY:
            return await self.grant_key_item(item_id)
        if category not in (D.CAT_ITEM, D.CAT_WEAPON, D.CAT_ARMOR):
            logger.info(f"  [skip] cat {category} grant unsupported (spell TBD)")
            return False
        base = self.sa(D.INVENTORY_BASE_SA)
        blob = bytearray(await self.psp.read(base, D.INV_RECORD_SIZE * 0x80))
        # ATOMIC: the caller re-runs this with the FULL qty when it returns False
        # (counter parked until a slot frees). So a partial write here would be
        # re-delivered on the retry and DUPLICATE the placed portion. Compute total
        # capacity first and bail writing NOTHING unless the whole qty fits; then
        # every retry is a clean no-op-or-full delivery.
        topup = sum(D.INV_QTY_MAX - blob[i + 2]
                    for i in range(0, len(blob), D.INV_RECORD_SIZE)
                    if blob[i] == category and blob[i + 1] == item_id
                    and 0 < blob[i + 2] < D.INV_QTY_MAX)
        empties = sum(1 for i in range(0, len(blob), D.INV_RECORD_SIZE)
                      if blob[i] == 0)
        if topup + empties * D.INV_QTY_MAX < qty:
            # No per-call log here or it floods every poll tick while the bag stays
            # full; the sole caller (_grant_pending) parks the counter and warns once.
            return False
        remaining = qty
        # 1) Top up existing stacks of the same (category, item_id) first. The game
        #    stacks by (cat,id) via the qty byte, but its display "Sort" only
        #    REORDERS records -- it never merges -- so without this every grant lands
        #    in a fresh slot and you get "Gold Needle : 1" x6 that no sort can fix.
        #    Native own-chest grants DO stack (game add-item routine), which is why
        #    a big native stack sits next to a pile of client-made singletons.
        for i in range(0, len(blob), D.INV_RECORD_SIZE):
            if remaining <= 0:
                break
            if (blob[i] == category and blob[i + 1] == item_id
                    and 0 < blob[i + 2] < D.INV_QTY_MAX):
                add = min(D.INV_QTY_MAX - blob[i + 2], remaining)
                blob[i + 2] += add
                remaining -= add
                await self.psp.write(base + i + D.INV_QTY_OFFSET, bytes([blob[i + 2]]))
        # 2) Spill any leftover into empty slots, one full stack at a time.
        for i in range(0, len(blob), D.INV_RECORD_SIZE):
            if remaining <= 0:
                break
            if blob[i] == 0:
                add = min(D.INV_QTY_MAX, remaining)
                blob[i:i + 3] = bytes([category, item_id, add])
                remaining -= add
                await self.psp.write(base + i, bytes([category, item_id, add]))
        return True

    async def grant_gil(self, amount):
        # clamp cur too: self-heals a save left over-cap by the old 9_999_999
        # bound (live 2026-07-21: 999_999 + 3_400 regrant -> "003399" display)
        cur = min(await self.read_gil(), D.GIL_MAX)
        await self.psp.write_u32(self.sa(D.GIL_ADDR_SA), min(cur + amount, D.GIL_MAX))

    async def grant_exp(self, amount):
        """Add `amount` EXP to EVERY party member's P_EXP save field, clamped to
        EXP_CAP (Lv.99 cumulative). The game recomputes level/stats on its next
        level-up check (after a battle), so banked EXP materializes as levels then --
        it does not level up instantly. Read-modify-write per member, like grant_gil."""
        for ci in range(D.PARTY_COUNT):
            a = self.sa(D.party_addr_sa(ci, D.P_EXP))
            cur = await self.psp.read_u32(a)
            await self.psp.write_u32(a, min(cur + amount, D.EXP_CAP))

    async def _remove_filler(self, n):
        """Remove up to n filler (Potion) stacks the native remote-chest grant
        added. Decrements the matching packed record(s); zeros one that hits 0.
        A leftover (player consumed the filler before the poll) is dropped."""
        if n <= 0:
            return
        base = self.sa(D.INVENTORY_BASE_SA)
        blob = bytearray(await self.psp.read(base, D.INV_RECORD_SIZE * 0x80))
        cat, iid = D.CHEST_FILLER_CAT, D.CHEST_FILLER_ID
        for i in range(0, len(blob), D.INV_RECORD_SIZE):
            if n <= 0:
                break
            if blob[i] == cat and blob[i + 1] == iid and blob[i + 2] > 0:
                take = min(blob[i + 2], n)
                left = blob[i + 2] - take
                n -= take
                await self.psp.write(base + i,
                                     bytes([0, 0, 0]) if left == 0
                                     else bytes([cat, iid, left]))

    async def _send_chest_checks(self, idxs):
        """Send an AP location check for each chest idx not already sent."""
        lids = []
        for idx in idxs:
            lid = ID.loc_id(idx)
            if lid not in self.sent_locations:
                self.sent_locations.add(lid)
                lids.append(lid)
        if lids:
            await self.check_locations(lids)

    async def _chest_poll_loop(self):
        """Breakpoint-FREE chest detection (replaces the exec-BP _chest_loop, so
        JIT block-linking is restored -> the dungeon lag is gone). Polls the
        opened-chest bitfield sa(D.CHEST_OPEN_BF_SA); each newly-set bit -> an AP
        check. Own item/gil chests grant natively (deduped in _grant_pending via
        _opened_own_locs); remote chests granted a benign filler removed here."""
        def bit(bf, idx):
            return bf[idx >> 3] >> (idx & 7) & 1

        async def tick():
            if self.psp is None or self.save_delta is None:
                return                                  # title / no live save
            # Re-baseline whenever the save block relocates: sa() now points at a
            # fresh copy, so a stale snapshot would read as a huge bogus diff.
            if self._chest_bf_delta != self.save_delta:
                self._chest_bf_delta = self.save_delta
                self._chest_bf_prev = None
            bf = await self.psp.read(self.sa(D.CHEST_OPEN_BF_SA),
                                     D.CHEST_OPEN_BF_BYTES)
            # Fail-safe: bits at/after TREASURE_COUNT must be clear; an implausibly
            # dense field (>250/268) is also junk. Either -> the address/frame is
            # wrong; stay idle rather than spam bogus checks (logged once).
            hi = range(D.TREASURE_COUNT, D.CHEST_OPEN_BF_BYTES * 8)
            pop = sum(bin(b).count("1") for b in bf)
            if any(bit(bf, i) for i in hi) or pop > 250:
                if not self._chest_bf_warned:
                    self._chest_bf_warned = True
                    logger.warning("  [chest_poll] implausible bitfield "
                                   f"(pop={pop}) -- address may be wrong; poll idle")
                self._chest_bf_prev = None
                return
            self._chest_bf_warned = False
            # Defer the WHOLE tick (strip + baseline + diff) while the NG+ carried snapshot
            # is loaded (Chaos-defeated bit set). A New Game from a beaten save shows the
            # cleared save's 200+ open bits for the ENTIRE character-creation screen;
            # baselining then mass-sends them as phantom checks (live 2026-07-23: 229), and
            # the event-key strip would fire against natives that don't exist yet. Wait --
            # no timer, commit is player-paced -- for the game to zero the block on commit,
            # then baseline the fresh all-zero field. A chaos-set save is otherwise a
            # FINISHED game (goal already reported) with no chests left to track, so
            # deferring indefinitely loses nothing.
            if await self._carried_save_snapshot():
                if self._chest_carryin_skips == 0:
                    self._chest_carryin_skips = 1
                    logger.info("  [chest_poll] completed-save snapshot loaded "
                                "(NG+ char-creation?) -- deferring baseline until commit")
                return
            self._chest_carryin_skips = 0
            # Event-key chests are routed to PATH B by _evk_pathb_loop, so opening one
            # normally grants NO native key. This strip is a safety net for the near-
            # impossible case where the per-floor flag write lost the race and the FIF
            # event fired anyway: it removes the stray native key (idempotent; gated on
            # _synced + _ever_won so a legitimately-won AP key is never stripped).
            if self._event_key_natives:
                await self._strip_event_key_natives(bf)
            prev = self._chest_bf_prev
            if prev is None:
                # Baseline: catch chests opened while the client was off (checks
                # are idempotent server-side). No filler cleanup -- those grants
                # are historical (already in the saved inventory).
                opened = [idx for idx in VALID_IDX if bit(bf, idx)]
                for idx in opened:
                    if idx in self._own_chest_idxs:
                        self._opened_own_locs.add(ID.loc_id(idx))
                await self._send_chest_checks(opened)
                logger.info(f"  [chest_poll] baseline: {len(opened)} chest(s) "
                            f"already open")
                self._chest_bf_prev = bytes(bf)
                return
            newly = [idx for idx in VALID_IDX
                     if bit(bf, idx) and not bit(prev, idx)]
            # Garbage guard: real play opens ~1 chest per 100ms tick. A burst of
            # many new bits at once means the field slipped (bad read/relocation)
            # -> don't send, re-baseline from the current field.
            if len(newly) > 12:
                logger.warning(f"  [chest_poll] {len(newly)} chests in one tick -- "
                               f"treating as noise, re-baselining")
                self._chest_bf_prev = bytes(bf)
                return
            if newly:
                fillers = 0
                for idx in newly:
                    if idx in self._own_chest_idxs:
                        self._opened_own_locs.add(ID.loc_id(idx))
                    elif idx in self._remote_chest_idxs:
                        fillers += 1
                await self._send_chest_checks(newly)
                if fillers:
                    await self._remove_filler(fillers)
                for idx in newly:
                    logger.info(f"Chest idx={idx} -> check "
                                f"({self.idx_desc.get(idx, '?')})")
            self._chest_bf_prev = bytes(bf)

        await self._poll(CHEST_POLL_S, "chest_poll", tick)

    async def _strip_event_key_natives(self, bf):
        """Remove the free native key item from every OPENED cat-0 event chest
        (D.EVENT_KEY_CHESTS). Those chests grant the vanilla key item via a map event
        that ignores the treasure table; the real AP item is delivered by the grant
        loop. Clears the possession bit AND any KEY_ITEM_FUNCTION_BITS gate (Nitro's
        canal bit, etc.) so a stripped item stops working -- exactly the same effect as
        the NPC-loop strips. Idempotent: only writes/logs when the native key is
        actually present, so it can run every poll tick to survive a save/reload.
        Gated on _synced() + skips keys won as AP items (_ever_won), so a disconnect
        blip or a legitimately-delivered key can never be stripped (see the
        key-item-loss-on-disconnect memory)."""
        if not self._synced():
            return
        # Soul-of-Chaos floors RE-RUN vanilla event scripts, so a bonus dungeon
        # can hand out a native key item of its own: live 2026-08-09 (Prime), a
        # dwarf event on a Whisperwind Cove floor gives a Star Ruby. This strip
        # keys only on a GLOBAL chest bit plus GLOBAL possession, so it cannot
        # tell that handover apart from the Cavern of Earth chest (idx 41) the
        # player opened days earlier -- it stripped the ruby, and with it the
        # Titan accept/fed bits (KEY_ITEM_FUNCTION_BITS[9] = 0x1151D & 0x60),
        # on every poll tick. The floor's own Titan gate could then never be
        # fed: a hard softlock inside the dungeon.
        # AP owns the DYNAMIC chests in these dungeons (_bonus_dyn_loop) and
        # nothing else -- none of EVENT_KEY_CHESTS is a bonus-dungeon chest --
        # so hold the strip while inside one and let the dungeon's own puzzle
        # currency work. This is not an escape hatch: the strip is idempotent
        # and resumes on the first tick after the party reaches the true
        # overworld, which is a separate map load away from any place a key
        # item could be spent, so nothing can be carried out and used.
        if await self._in_bonus_dungeon():
            return
        for idx, key_id in self._event_key_natives.items():
            if not (bf[idx >> 3] >> (idx & 7) & 1):
                continue                     # chest still closed -> nothing granted yet
            try:
                if self._key_won(key_id):
                    continue                 # player owns it via AP -- never strip
                addr, mask = D.key_item_bit(key_id)
                owned = bool((await self.psp.read(self.sa(addr), 1))[0] & mask)
                fb = D.KEY_ITEM_FUNCTION_BITS.get(key_id)
                fset = bool((await self.psp.read(self.sa(fb[0]), 1))[0] & fb[1]) if fb else False
                if owned or fset:
                    await self.grant_key_item(key_id, present=False, quiet=True)
                    logger.info(f"  [event-key] stripped native "
                                f"{D.KEY_ITEMS.get(key_id, key_id)} (chest idx {idx}; "
                                f"AP item delivered via the grant loop)")
            except Exception as e:
                logger.info(f"  [event-key] {e!r}")

    # ---------------- shared poll-loop skeleton ----------------
    async def _poll(self, interval, name, tick):
        """Run `tick` every `interval`s until client exit. A raising tick is
        logged and retried next round (a transient WS error must never kill a
        reconcile loop); a tick returning False ends the loop for good. The
        stateful loops keep their state in the tick closure. Loops with bespoke
        pacing/halting (chest, grant, boot_patch, party, thief_steal, watchdog)
        do NOT use this -- their control flow is the feature."""
        while not self.exit_event.is_set():
            try:
                if await tick() is False:
                    return
            except Exception as e:
                logger.info(f"  [{name}] {e!r}")
                # First occurrence of each distinct failure gets a traceback --
                # a bare repr() hid the _npc_loop `_s.unpack` NameError (2026-08-07)
                # behind an anonymous 2s-repeating one-liner for a whole seed.
                key = (name, repr(e))
                if key not in self._loop_err_seen:
                    self._loop_err_seen.add(key)
                    logger.info(f"  [{name}] first occurrence:\n"
                                f"{traceback.format_exc()}")
            await asyncio.sleep(interval)

    @contextlib.contextmanager
    def _stage(self, name):
        """Fault isolation for one independent section of a reconcile tick.

        _npc_loop's tick is ~13 unrelated reconciles (function-bit reassert,
        tablet/rune assembly, NPC map-reset holds, native strips, ...) sharing
        one byte-snapshot read. Before 2026-08-07 an exception anywhere killed
        the REST of the tick forever: a `_s.unpack` typo in the Sage/canoe
        stage stopped the promoted-key reconcile below it, so a player's Warp
        Cube -- deliberately cleared by the prearm so the NPC re-offers -- was
        never restored and the Flying Fortress stayed locked.

        Now a failing stage is logged once (with traceback) and skipped; every
        other stage still runs, so the tick self-heals on the next poll for
        everything the broken stage didn't own."""
        try:
            yield
        except Exception as e:
            key = (name, repr(e))
            if key not in self._loop_err_seen:
                self._loop_err_seen.add(key)
                logger.info(f"  [stage:{name}] FAILED (isolated -- other stages "
                            f"still ran): {e!r}\n{traceback.format_exc()}")

    # ---------------- goal ----------------
    async def send_goal(self):
        if not self.finished_game:
            await self.send_msgs([{"cmd": "StatusUpdate",
                                   "status": ClientStatus.CLIENT_GOAL}])
            self.finished_game = True
            logger.info("Goal reported -> AP")

    # ---------------- item grants (driven by CommonContext.items_received) ----
    def _ap_item_to_game(self, ap_item):
        iid = ap_item.item
        if ID.is_victory(iid):
            return (None, None, 0)
        if ID.is_gil(iid):
            return (None, None, ID.gil_amount(iid))
        if ID.is_exp(iid):
            # EXP bag: synthetic, no native item. Route to grant_exp via the CAT_EXP
            # sentinel; qty carries the per-member EXP amount (encoded in the id).
            return (D.CAT_EXP, None, ID.exp_amount(iid))
        if ID.is_tablet(iid):
            # Lute Tablet: synthetic piece of the Lute, no game item. Counter-only
            # here; _npc_loop counts copies (sticky _tablet_count) and sets the
            # Lute possession bit once lute_tablets_required is reached.
            return (None, None, 0)
        if ID.is_rune(iid):
            # Equipment Rune: synthetic piece of the Equipment Rune Key, no game
            # item. Counter-only; _npc_loop counts copies (sticky _rune_count)
            # and sets story flag 62 once equipment_runes_required is reached.
            return (None, None, 0)
        if ID.is_shard(iid):
            # Levistone Shard: synthetic piece of the Levistone, no game item.
            # Counter-only; _npc_loop counts copies (sticky _shard_count) and
            # grants the real Levistone (possession + airship bits) once
            # levistone_shards_required is reached.
            return (None, None, 0)
        if ID.is_job_item(iid) or ID.is_event(iid) or ID.is_vehicle(iid):
            # Event gate tokens are logic-graph markers, not game items at
            # all; vehicle items (Ship) reconcile via _npc_loop's flag write.
            # Job items are no longer placed (promotion is native Bahamut only),
            # but the guard stays defensive -> counter-only, never a bad grant.
            return (None, None, 0)
        cat, gid = ID.item_cat_gid(iid)
        return (cat, gid, 1)

    async def _grant_safe(self):
        """True only when the save struct is fully loaded and the game is in a
        stable, non-battle frame -- so the received-counter read reflects the real
        saved value, not a transitional/half-loaded/garbage frame. This is the
        foundation that makes even the FIRST counter read of the session
        trustworthy (cf. FF4 Free Enterprise's sentinel gate / FF6WC's menu gate).

        Gates:
          * bridge attached + save_delta resolved -- save_delta resolves ONLY when
            char-0's level reads 1..99 at the candidate base (_resolve_save_delta),
            so this also rejects a wrong/half-loaded base.
          * _synced() -- the server list is authoritative (not the disconnect-window
            transient-empty list).
          * NOT mid-battle -- battle reuses save-struct-adjacent RAM and a mid-fight
            inventory write can be clobbered; a won item simply waits for the field.
        Fail-safe: any read error -> False (skip the tick), never grant blind."""
        if self.psp is None or self.save_delta is None:
            return False
        if not self._synced():
            return False
        try:
            if await self._in_battle():
                return False
        except Exception:
            return False
        return True

    # How long a grant may sit undelivered before the stall is called out. Long
    # enough that ordinary battles/menus never trip it, short enough that a
    # player who notices a missing item and grabs a debug bundle finds the
    # reason already in the log.
    GRANT_STALL_WARN_S = 20.0

    async def _note_grant_stall(self, why):
        """Say WHY a grant tick bailed, once per stall, when items are actually
        waiting. Both early returns in _grant_pending used to be silent, so a
        stuck delivery left no trace at all: the 2026-08-09 report (Ice Cave
        NPC check sent, Spell Tome: Life never delivered) produced a bundle
        showing items_received 77 vs counter 76 and NOTHING in the log to say
        which gate was holding it -- undiagnosable after the fact. Silence is
        only correct while there is nothing to deliver."""
        pending = len(self.items_received) - (self.received_count or 0)
        if pending <= 0:
            self._grant_stall = None
            return
        now = time.monotonic()
        prev = getattr(self, "_grant_stall", None)
        if prev is None or prev[0] != why:
            self._grant_stall = (why, now, False)
            return
        _why, since, warned = prev
        if not warned and now - since >= self.GRANT_STALL_WARN_S:
            nxt = self.items_received[self.received_count or 0]
            logger.warning(
                f"  [grant] {pending} item(s) waiting for {now - since:.0f}s -- "
                f"delivery held by: {why}. Next up: "
                f"{self.item_names.lookup_in_game(nxt.item)}. This clears "
                f"itself when the game returns to a normal field frame; if it "
                f"persists, reconnect the client (nothing is lost -- the save "
                f"counter re-delivers from where it stopped).")
            self._grant_stall = (why, since, True)

    async def _read_counter_stable(self):
        """Read the received-counter twice back-to-back and return it only if the
        two agree. A transient garbage frame (the game mid-write to adjacent RAM,
        a half-applied relocation) shows two different values microseconds apart
        and is rejected (returns None -> caller waits for a calm frame). A
        stably-wrong value is caught instead by _grant_safe's base plausibility
        and the monotonic guard in _grant_pending."""
        a = await self.psp.read_u32(self.sa(D.RECEIVED_COUNTER_ADDR_SA))
        b = await self.psp.read_u32(self.sa(D.RECEIVED_COUNTER_ADDR_SA))
        return a if a == b else None

    async def _reset_carried_counter(self):
        """Zero the save-resident grant counter and every client-side mirror of
        it. Only called when a carried NG+ counter is detected on a genuinely
        fresh new game (both call sites in _grant_pending gate on
        _newgame_block_live first)."""
        await self.psp.write_u32(self.sa(D.RECEIVED_COUNTER_ADDR_SA), 0)
        self.received_count = 0
        self._counter_hw = 0
        await self._reset_slotmagic_state()

    async def _grant_pending(self):
        """Grant every received item whose absolute position is >= the SAVE-resident
        counter (D.RECEIVED_COUNTER_ADDR_SA), advancing that counter in the save after
        each grant. self.items_received (CommonContext, server-fed) is the durable
        item list; the save counter is how many we've granted.

        Death/load rolls the save counter BACK together with the lost items, so the
        next call re-grants exactly those items -- this is the fix for "open chest,
        die, item gone forever". This is the ONE writer of both grants and the
        counter, lock-guarded so it can't race itself."""
        async with self._grant_lock:
            # NG+ CARRIED-COUNTER EARLY RESET (pre-_synced). A fresh New Game from a
            # beaten save carries the old counter (348 live 2026-07-23), and on a
            # BRAND-NEW game the server has sent NOTHING yet -> items_received is
            # empty -> _synced() False -> _grant_safe() False -> the AHEAD-branch
            # reset below never runs during the fresh window. By the time the player
            # makes their first check (populating items_received), chests are open /
            # EXP>0 -> no longer fresh -> reset refused -> EVERY grant wedged for the
            # seed (live 2026-07-23: Flame Mail check sent, item never delivered).
            # So zero the carried counter here, gated ONLY on a provably-fresh new
            # game (Lv1/EXP0/no chests) -- 0 is the unconditionally-correct counter
            # for a fresh game, independent of sync state. Once per save delta.
            # No per-delta latch: after the reset the counter reads 0 (this block's own
            # `if c0` and the AHEAD branch both no-op at 0), so it cannot re-fire within a
            # game -- but a MID-SESSION reload of the clear slot at the SAME save_delta
            # re-inflates the carried counter and MUST reset again (live 2026-07-23).
            if self.save_delta is not None:
                try:
                    in_battle = await self._in_battle()
                except Exception:
                    in_battle = True
                if not in_battle and await self._newgame_block_live():
                    c0 = await self._read_counter_stable()
                    # ONLY a counter AHEAD of what we ourselves delivered this run is
                    # carried junk. Without this guard the reset fires on our OWN
                    # grants: a fresh game stays Lv1/EXP0/no-chests indefinitely, so
                    # every tick zeroed the counter and re-granted the whole received
                    # list -- infinite gil / infinite items (live 2026-07-31, an AP-shop
                    # "9900 Gil" re-granted to the 999,999 cap). Our grants keep
                    # _counter_hw in lockstep with the counter, so c0 == hw -> no reset;
                    # a genuine NG+ carry-in (348 live 2026-07-23) or a same-delta
                    # clear-slot reload reads far above hw and still resets.
                    if c0 and c0 > self._counter_hw:   # stable + ahead -> carried junk
                        logger.info(f"  [grant] carried NG+ counter {c0} on a fresh "
                                    f"new game (pre-sync) -> resetting to 0")
                        await self._reset_carried_counter()
            # SAFE-STATE GATE: only read/act on the counter when the save struct is
            # fully loaded and the frame is stable (see _grant_safe). This subsumes
            # the old (save_delta is None) + _synced() checks and adds not-in-battle.
            if not await self._grant_safe():
                await self._note_grant_stall("unsafe frame (battle / save block "
                                             "not resolved / server not synced)")
                return
            # A save (re)load/relocation flagged by _save_delta_loop means the
            # counter may LEGITIMATELY roll back with the reloaded save. Consume the
            # flag now so it authorizes exactly the decrease we handle this tick.
            reload_seen = self._reload_pending
            self._reload_pending = False
            total = len(self.items_received)
            c = await self._read_counter_stable()
            if c is None:
                # unstable read -> wait for a calm frame
                await self._note_grant_stall("received-counter read unstable "
                                             "(two reads disagreed)")
                return
            self._grant_stall = None         # made it past every silent gate
            action, repair_to = GP.grant_decision(
                c, total, self._counter_hw, reload_seen)
            if action == GP.AHEAD:
                # NG+ CARRIED COUNTER: a New Game started from a BEATEN save inherits
                # the old save's grant counter -- the game zeroes its own fields but
                # not our AP-repurposed one, so it reads huge (348 live 2026-07-23) on
                # a brand-new game and blocks EVERY grant for the whole seed. Only
                # reset when the block underneath is a genuinely fresh new game (all
                # Lv1/EXP0, no chests open, carried snapshot cleared), which an
                # in-progress or endgame save can never satisfy. Re-fires on a same-delta
                # clear-slot reload (no latch); harmless at steady state (c==0 not AHEAD).
                #
                # The `c > _counter_hw` guard mirrors the pre-sync path above and is
                # NOT optional (live 2026-08-17): a SERVER RECONNECT briefly leaves
                # items_received empty, so total drops to 0 and our own perfectly
                # good counter reads AHEAD. On a fresh-looking party that zeroed the
                # counter and re-granted the entire list -- twice in one session, two
                # extra 5450-gil payouts. Our own grants keep _counter_hw in lockstep
                # (c == hw -> not carried), while a genuine NG+ carry-in (348 live
                # 2026-07-23) reads far above a session hw of 0 and still resets.
                if c > self._counter_hw and await self._newgame_block_live():
                    logger.info(f"  [grant] carried NG+ counter {c} on a fresh new "
                                f"game -> resetting to 0 so items can grant")
                    await self._reset_carried_counter()
                    return                   # next tick delivers from 0
                # Counter ahead of anything the server has confirmed: either the
                # initial resync hasn't landed (harmless; a later one extends the
                # list) OR the slot holds garbage (slot NOT verified free -> would be
                # silent loss). Never grant on a bad counter; warn once so a bad slot
                # is loud, not silent.
                if not self._warned_bad_counter:
                    logger.warning(f"  [grant] counter {c} > received {total}; not "
                                   f"granting. If persistent, RECEIVED_COUNTER_ADDR_SA "
                                   f"is wrong/unverified.")
                    self._warned_bad_counter = True
                self.received_count = c
                return
            if action == GP.REPAIR:
                # MONOTONIC GUARD tripped: the counter dropped below what we've
                # already delivered this session with NO save-block move observed.
                # TWO different faults land here and look identical on one frame:
                #   (1) a spurious under-read on a busy/transitional frame -- the
                #       exact fault that mass-duplicated items 2026-07-10 (a bad low
                #       read re-ran the whole grant list into a LIVE inventory).
                #   (2) a genuine IN-PLACE save reload -- a game-over "Continue"
                #       reloads the last save at the SAME block base, so
                #       _save_delta_loop never sees the block move and _reload_pending
                #       stays False, yet the counter AND the inventory really rolled
                #       back (Prime lost a Defender this way, 2026-08-13).
                # The relocating/title-screen reload is already handled up front by
                # _reload_pending (-> DELIVER); this branch must separate (1) from
                # the SAME-BASE case of (2). Distinguish by PERSISTENCE: our AP
                # counter can only fall below hw via a reload or a transient glitch
                # (no normal play lowers it), and a transient clears on the next
                # tick. A low value that HOLDS for GRANT_INPLACE_RELOAD_TICKS stable
                # ticks means the save block itself reloaded (the counter lives in
                # that block, so the inventory rolled back with it) -> re-granting
                # items[c:total] restores the lost tail and never dups. While
                # observing, do NOT write the counter -- writing repair_to back
                # would erase the very low read the streak is counting.
                self._repair_streak_c, self._repair_streak, corroborated = \
                    GP.repair_streak_step(c, self._repair_streak_c,
                                          self._repair_streak,
                                          GRANT_INPLACE_RELOAD_TICKS)
                if corroborated:
                    logger.warning(f"  [grant] counter held at {c} < delivered "
                                   f"{repair_to} for {self._repair_streak} ticks with "
                                   f"no block move -- a genuine in-place reload (game "
                                   f"over?), NOT a glitch; re-granting items[{c}:"
                                   f"{total}].")
                    self._repair_streak = 0
                    self._repair_streak_c = None
                    self._warned_glitch_counter = False   # re-arm for the next event
                    # fall through to the DELIVER loop below (re-grants items[c:total])
                else:
                    if not self._warned_glitch_counter:
                        logger.warning(f"  [grant] counter {c} < delivered {repair_to} "
                                       f"with no save reload seen -- holding "
                                       f"({self._repair_streak}/"
                                       f"{GRANT_INPLACE_RELOAD_TICKS}) to tell a "
                                       f"spurious glitch from an in-place reload; NOT "
                                       f"re-granting yet.")
                        self._warned_glitch_counter = True
                    # Leave the live counter untouched so a persistent low read can
                    # build the streak; a transient glitch self-heals (the game's own
                    # counter reads correct again next tick -> DELIVER, nothing new).
                    self.received_count = c
                    return
            # action == "deliver": normal forward progress (c >= high-water) OR a
            # reload-corroborated rollback (c < high-water AND reload_seen -> the
            # save really rolled back, so re-grant the lost tail; those items were
            # lost with the rolled-back inventory, so this restores, never dups).
            # Reaching DELIVER means the counter is no longer anomalously low, so
            # clear any in-place-reload streak (covers a transient that self-healed
            # and the corroborated-reload fall-through alike).
            self._repair_streak = 0
            self._repair_streak_c = None
            self.received_count = c
            for pos in range(c, total):
                it = self.items_received[pos]
                if ID.is_job_item(it.item):
                    # Counter-only item (no game grant), so this log line is the
                    # player's only notice of what the scroll does.
                    await self._log_scroll_effect(it.item)
                cat, iid, qty = self._ap_item_to_game(it)
                if cat == D.CAT_EXP:
                    # EXP bag: add qty EXP to every party member. Additive (not
                    # idempotent) but counter-driven exactly like grant_gil -- a
                    # death/load rollback rolls the counter back and re-grants it,
                    # which is correct (the EXP was lost with the rolled-back save).
                    if qty > 0:
                        await self.grant_exp(qty)
                        logger.info(f"  [grant] +{qty} EXP to all party members")
                elif cat is None:
                    # Gil (qty = amount): an OWN GIL CHEST grants natively (the
                    # real amount is baked into the treasure table and the open-
                    # chest fn adds it + shows "<n> gil obtained"), so those are
                    # counter-only to avoid a double-add. Every OTHER source --
                    # NPC locations (Princess), shop AP purchases, gil found in
                    # another player's world -- has NO native grant, so add it
                    # here (this was the "AP said 6400 Gil but gil didn't move"
                    # bug). Victory (qty==0): nothing to grant.
                    native = (getattr(it, "player", None) == self.slot and
                              getattr(it, "location", None) in self._opened_own_locs)
                    if qty > 0 and not native:
                        await self.grant_gil(qty)
                        logger.info(f"  [grant] +{qty} gil (non-chest gil item)")
                elif getattr(it, "location", None) in self._opened_own_locs:
                    # Own item in an own chest the poll saw OPENED: the game's
                    # native grant already put it in the (saved) inventory, so
                    # this is counter-only (avoids double-grant). Not-yet-opened
                    # own locations (e.g. server !collect) are NOT in the set ->
                    # they still grant here, so nothing is lost.
                    # EXCEPTION (2026-07-07): key items with a FUNCTION bit. The
                    # native chest grant sets the POSSESSION bit only, never the
                    # function/event bit (e.g. Warp Cube 0x11520 b0 that enables the
                    # Mirage Tower -> Flying Fortress warp), so a function-gated key
                    # item found in your OWN chest showed in inventory but did
                    # nothing. grant_key_item is idempotent on possession and also
                    # sets the function bit, so re-run it for key items only
                    # (weapons/armor/consumables would genuinely duplicate).
                    if cat == D.CAT_KEY:
                        await self.grant_key_item(iid)
                else:
                    # auto_sell_unusable_items: gear no party job can ever
                    # equip (and that isn't activatable) pays gil instead of
                    # taking an inventory record. Mechanism per FF4FE: the item
                    # pool is untouched -- hints/tracker name the real item --
                    # and the swap happens here at application time. location
                    # > 0 skips start inventory and server-granted items, which
                    # carry a non-positive (or missing) location -- FF4FE's own
                    # guard, and every real location id is >= ids.BASE. Own-chest
                    # gear never reaches this branch at all: the scout already
                    # baked those chests as native gil.
                    _loc = getattr(it, "location", None)
                    sell = self._auto_sell_value(cat, iid) * qty
                    if sell and isinstance(_loc, int) and _loc > 0:
                        await self.grant_gil(sell)
                        logger.info(
                            f"  [auto-sell] {self._game_item_name(cat, iid)} "
                            f"-- no party member can ever equip it; sold for "
                            f"{sell} gil")
                    elif not await self.grant_item(cat, iid, qty):
                        # Inventory full: grant_item put nothing in, so DON'T
                        # advance the counter -- leave it parked on this item and
                        # stop the pass. The grant loop re-runs on the next tick /
                        # watcher event and retries in-order once the player frees
                        # a slot (use/sell), so the item is deferred, never lost.
                        # Advancing here (the old behaviour) marked it delivered and
                        # dropped it forever. Warn once per stall to avoid flooding
                        # the log every poll; re-armed below on the next success.
                        if not self._inv_full_warned:
                            logger.warning(
                                f"  [grant] inventory full -- delivery paused on "
                                f"cat{cat}/id{iid}; frees automatically when you "
                                f"make a slot (use/sell an item).")
                            self._inv_full_warned = True
                        return
                    self._inv_full_warned = False
                await self.psp.write_u32(self.sa(D.RECEIVED_COUNTER_ADDR_SA), pos + 1)
                self.received_count = pos + 1
                self._counter_hw = max(self._counter_hw, pos + 1)

    async def _reset_slotmagic_state(self):
        """NG+ hygiene for slot_magic: a New Game from a beaten save inherits
        the old save's SPENT-charge array / CW point pool (the game zeroes its
        own fields, not our repurposed run at save+0x80C..0x83B) -- live
        2026-07-30: a fresh party spawned with every slot already spent.
        Zero the whole 48-byte region (spent + CW pool + Soma counts + marker
        + pad); 0 = nothing spent = full charges and no Soma drunk, the
        unconditionally-correct fresh-game state. No-op when slot_magic off."""
        if not (self.slot_data.get("on_disc") or {}).get("slot_magic"):
            return
        try:
            await self.psp.write(self.sa(D.SPELL_SLOTS_SPENT_BASE_SA),
                                 bytes(0x30))
            logger.info("  [slot_magic] NG+ carried charge state cleared "
                        "(spent + CW pool -> fresh)")
        except Exception:
            pass

    async def _grant_loop(self):
        """Reconcile granted items against the save-resident counter. CommonContext
        pokes watcher_event on new items; a death/load emits NO event, so we also
        poll (the timeout) to catch the counter rolling back and re-grant the gap.
        Steady state is one u32 read per tick (cheap)."""
        while not self.exit_event.is_set():
            try:
                await self._grant_pending()
                # one-shot heal: a save written while gil sat over-cap (old
                # 9_999_999 clamp bug) keeps the over-cap u32; clamp it once.
                if not self._gil_healed and self.save_delta is not None:
                    self._gil_healed = True
                    g = await self.read_gil()
                    if g > D.GIL_MAX:
                        await self.psp.write_u32(self.sa(D.GIL_ADDR_SA), D.GIL_MAX)
                        logger.info(f"  [grant] gil {g} over cap -> {D.GIL_MAX}")
            except Exception as e:
                logger.info(f"  [grant failed] {e!r}")
            try:
                await asyncio.wait_for(self.watcher_event.wait(), 1.0 / GRANT_POLL_HZ)
                self.watcher_event.clear()
            except asyncio.TimeoutError:
                pass

    # ---------------- PATH A: scout -> AP chest contents (baked on-disc) --------
    def _server_up(self):
        """True when the AP websocket is live. A momentary drop right after
        join sets CommonContext.server back to None; we must not send scouts
        into a dead socket (raises) nor treat the empty reply as authoritative."""
        if not hasattr(self, "server"):
            return True  # unknown CommonContext shape -> let send_msgs decide
        srv = self.server
        return srv is not None and getattr(srv, "socket", None) is not None

    def _synced(self):
        """True once the server list is authoritative and safe to make destructive
        decisions from: the socket is live AND at least one non-empty resync has
        landed this run. Also folds each non-empty snapshot into _ever_won (sticky)
        so a later disconnect (which empties items_received) can't un-win an item.

        Loops that strip native items or resend grants MUST gate on this; a raw
        `any(... in self.items_received)` check reads False during the disconnect
        window and wrongly strips/regrants (key-item loss, live 2026-07-08)."""
        if self._server_up() and self.items_received:
            self._had_sync = True
            self._ever_won.update(it.item for it in self.items_received)
        return self._had_sync and self._server_up()

    async def _await_scout(self, wanted, rounds=8, round_timeout=15.0):
        """(Re)send LocationScouts and wait for EVERY wanted location to resolve,
        surviving a disconnect/reconnect. The launch-time blip that motivated
        this (server drops 1s after join) ate the reply and left locations_info
        empty -> a nameless ISO baked + cached (see bridge-connect / bake memos).
        Each round waits for a live socket, resends the scout, then polls
        locations_info. Returns True once all resolve, False if still short."""
        for r in range(rounds):
            if self.exit_event.is_set():
                return False
            # 1. wait (up to a round) for the socket to come back after a blip
            waited = 0.0
            while not self._server_up():
                if self.exit_event.is_set() or waited >= round_timeout:
                    break
                await asyncio.sleep(0.25)
                waited += 0.25
            if not self._server_up():
                continue  # still down -> next round
            # 2. (re)send the scout
            try:
                await self.send_msgs([{"cmd": "LocationScouts",
                                       "locations": wanted, "create_as_hint": 0}])
            except Exception as e:
                logger.info(f"  [scout] send failed ({e!r}); retrying")
                await asyncio.sleep(1.0)
                continue
            # 3. poll for the LocationInfo reply; a mid-wait drop restarts the round
            deadline = round_timeout
            while deadline > 0:
                if all(lid in self.locations_info for lid in wanted):
                    return True
                if not self._server_up():
                    break
                await asyncio.sleep(0.2)
                deadline -= 0.2
            got = sum(1 for lid in wanted if lid in self.locations_info)
            logger.info(f"  [scout] attempt {r + 1}/{rounds}: "
                        f"{got}/{len(wanted)} locations resolved -- retrying")
        return all(lid in self.locations_info for lid in wanted)

    async def _scout_locations(self):
        """Scout every chest + shop location from the SERVER (no PPSSPP needed --
        this runs BEFORE the game launches so the results can be baked into the
        patched ISO). Builds:
          tt_values  : treasure idx -> u32 chest value (own item/gil = the real
                       thing, remote = sentinel) -- baked into the treasure table
                       and verified/healed at runtime by _table_loop.
          idx_desc   : chest idx -> human log line for the chest-open message.
          shop_desc / _extra_patches : AP shop offer strings + name-bank patches."""
        # Skip phantom (non-chest) treasure indices -- the apworld does NOT create
        # locations for these (LOGIC.PHANTOM_TREASURE_INDICES is the shared source of
        # truth with create_regions), so scouting them would send the server an id it
        # doesn't own -> it closes the socket on the LocationScouts. test_scout_parity
        # keeps this in lockstep with create_regions.
        # Skip the SAME idx create_regions skips: always the phantoms, PLUS each
        # static DLC boss chest (idx 252-255) whose dungeon produces zero dynamic AP
        # chests -- its *_ap_locations is 0 (individually, or all four under
        # exclude_bonus_dungeons), so the seed owns none of those ids. The Labyrinth of
        # Time puzzle chests (256-267) aren't in DATA.LOCATIONS at all
        # (gen_apdata.LABYRINTH_DROP), so they never reach this loop.
        skip = set(LOGIC.PHANTOM_TREASURE_INDICES)
        skip |= LOGIC.removed_static_dlc_idx(
            self.bonus_dyn_caps or {},
            bool((self.slot_data or {}).get("exclude_bonus_dungeons")))
        # ...PLUS the normally-empty (alias-duplicate) chests when
        # loot_in_normally_empty_chests is off: their dedup records are not
        # baked, so they keep the vanilla treasure index they share with a
        # neighbour and the seed owns no location for them. The flag rides the
        # on_disc dict. A seed generated BEFORE the 2026-08-12 rename speaks the
        # old dialect (loot_in_gulg_b5_chests, covering only the Gulg B5 three),
        # so both keys are read; absent = predates the option = always deduped.
        _on_disc = ((self.slot_data or {}).get("on_disc") or {})
        skip |= LOGIC.removed_normally_empty_idx(
            _on_disc.get("loot_in_normally_empty_chests"),
            _on_disc.get("loot_in_gulg_b5_chests"))
        loc_ids = [ID.loc_id(idx) for (_lid, _name, idx) in DATA.LOCATIONS
                   if idx not in skip]
        shop_lids = [ID.shop_loc_id(s, k)
                     for (s, _c, _g, prices) in self.shop_slots
                     for k in range(len(prices))]
        # NPC locations that natively grant a key item (D.KEY_NPC_ORDINALS):
        # scouted so the KEY_NAME.MSG bake can show each granting location's AP
        # item in the "You obtain the {key}." box. Unknown ids are dropped by
        # the `known` filter below like any phantom.
        npc_lids = [ID.npc_loc_id(o) for o in D.KEY_NPC_ORDINALS.values()]
        npc_lids.append(ID.npc_loc_id(D.BIKKE_NPC_ORDINAL))  # "You obtain a ship."
        npc_lids.append(ID.npc_loc_id(D.SMITH_NPC_ORDINAL))  # "You obtain Excalibur."
        # Dynamic bonus-dungeon chests: scouted so the BDC1 next_sid box naming
        # (and the client check log) can show each ordinal's AP item name.
        dyn_lids = [ID.dyn_chest_loc_id(dg, o)
                    for dg, cap in sorted((self.bonus_dyn_caps or {}).items())
                    for o in range(cap)]
        # Scout ONLY locations the server actually owns for this slot. DATA.LOCATIONS
        # is the full static chest table, but the apworld drops some treasure indices
        # from the generated seed (e.g. the phantom Levistone chest idx 198 -- an
        # event pickup with no chest bit, see logic.LEVISTONE_TREASURE_IDX). A single
        # id the seed doesn't define makes the SERVER throw and CLOSE the socket, so
        # the whole LocationScouts (and every retry) fails -> the disconnect loop that
        # motivated this guard. missing+checked = every location this slot owns.
        known = set(self.missing_locations) | set(self.checked_locations)
        wanted = loc_ids + shop_lids + npc_lids + dyn_lids
        if known:
            phantom = [lid for lid in wanted if lid not in known]
            if phantom:
                logger.info(f"  [scout] dropping {len(phantom)} location(s) not in "
                            f"this seed (e.g. phantom chests): {sorted(phantom)[:5]}")
                wanted = [lid for lid in wanted if lid in known]
        # CommonContext fills self.locations_info on the LocationInfo reply.
        # Retry across reconnects and REFUSE to proceed on an incomplete scout:
        # an empty/partial scout would bake a nameless ISO that then CACHES, so
        # every relaunch reuses the broken seed (chests/shops show filler while
        # the server still grants the right items). Fail loud instead.
        if not await self._await_scout(wanted):
            got = sum(1 for lid in wanted if lid in self.locations_info)
            raise RuntimeError(
                f"scout incomplete: only {got}/{len(wanted)} locations resolved "
                "-- server connection unstable at launch")
        # Shop AP stock: remember whose item each offer holds (purchase log +
        # sequential name/desc/price updates between sales).
        for (s, _cat, _gid, prices) in self.shop_slots:
            self.shop_offers[s] = []
            for k, price in enumerate(prices):
                lid = ID.shop_loc_id(s, k)
                info = self.locations_info.get(lid)
                if not info:
                    self.shop_offers[s].append((lid, None))
                    continue
                who = ("your" if info.player == self.slot
                       else self.player_names.get(info.player,
                                                  f"Player{info.player}") + "'s")
                try:
                    item_name = self.item_names.lookup_in_slot(info.item, info.player)
                except Exception:
                    item_name = f"item{info.item}"
                self.shop_offers[s].append((lid, item_name))
                self.shop_desc[(s, k)] = f"{who} {item_name} ({price} gil)"
        if self.shop_slots or self.hint_rows:
            # Item-level shop contents SPOIL the seed for anyone watching the
            # console -- keep the full stock dump for troubleshooting (visible at
            # --loglevel debug) but off the normal log.
            logger.debug("AP shop stock: " + "; ".join(
                f"shop {s}: " + ", ".join(
                    self.shop_desc.get((s, k), f"offer {k}")
                    for k in range(len(prices)))
                for (s, _c, _g, prices) in self.shop_slots))
            self._extra_patches = (self._build_shop_name_patches()
                                   + self._build_shop_desc_patches())
        n_own = n_remote = 0
        # Remote chest-box names bake into the extended item NAME bank, which is
        # now grown on disc WHETHER OR NOT spell_tomes is on (see remote-chest-
        # name-gating memory). The first remote string id (base) is 107 when the
        # 64-entry tome block is present, 43 without it. Names are DEDUPED: a seed
        # with 100 "your Lute Tablet" chests shares ONE bank entry, keeping the
        # tight FM_EXTERN J dead-space budget and avoiding name truncation.
        tomes_on = bool((self.slot_data.get("on_disc") or {}).get("spell_tomes"))
        self._remote_base = (D.CHEST_REMOTE_SID_BASE if tomes_on
                             else D.CHEST_REMOTE_SID_BASE_NO_TOMES)
        self._remote_names = []
        self._remote_name_idx = {}
        for (_lid, _name, idx) in DATA.LOCATIONS:
            info = self.locations_info.get(ID.loc_id(idx))
            if not info:
                continue
            iid, player = info.item, info.player
            cat, gid = (ID.item_cat_gid(iid) if ID.is_item(iid) else (None, None))
            # Own SPELL TOMES (cat1 id 44+) resolve their box name through the
            # SAME FM_EXTERN cat1 name bank the menu uses -- getter 0x088d4718,
            # cave-extended to 108 entries + array filled on disc by
            # apply_spell_tomes (see chest-box-name-source memory). So encode the
            # REAL tome (cat,id) into the treasure table: the native chest-box
            # {NAME} shows the real tome name with NO Path B authoring and NO
            # msg-bank relocation race. (The grant is still skipped at ITEM_SITE;
            # AP delivers the item. Naturally gated: tomes only exist when the
            # spell_tomes feature -- hence the cave -- is enabled.)
            # Poll-based chests (tier2-poll-chests): every chest grants NATIVELY
            # (no exec-BP skip). OWN item/gil chests bake the REAL (cat,id)/gil ->
            # native grant delivers + native box name; _grant_pending dedupes them
            # via _opened_own_locs. REMOTE chests (incl. our own synthetic AP
            # items like Lute Tablet, which have no native game id) bake a benign
            # FILLER (Potion) in byte0/1 (safe native grant) + the remote name-
            # bank string id in bits16-30 -> the on-disc detour shows the real
            # (sanitized) AP name; the poll removes the filler.
            filler = _enc_item_tt(D.CHEST_FILLER_CAT, D.CHEST_FILLER_ID)
            # Vanilla key-item chests are cat-0 EVENT-granted (D.EVENT_KEY_CHESTS): the
            # map event hands out the native key item + a hardcoded box, ignoring the
            # treasure table. Route A (2026-07-09): _evk_pathb_loop pre-sets each event's
            # per-floor COMPLETION flag on floor load, so opening runs PATH B instead --
            # the chest behaves as an ordinary treasure chest (reads the runtime table ->
            # correct AP box name, no native key). To make that safe we FORCE these chests
            # down the remote-style path below (bake a benign FILLER + an authored AP name,
            # deliver the real item via the grant loop) even for OUR OWN items: path B would
            # DOUBLE-grant a real baked own item (native treasure grant + grant loop), and
            # filler is harmless (the poll removes it). The native key is remembered so the
            # strip loop can clean it up in the (near-impossible) event that the flag write
            # loses the race and path A fires anyway.
            native_key = D.EVENT_KEY_CHESTS.get(idx)
            is_evk = native_key is not None
            if is_evk:
                self._event_key_natives[idx] = native_key
            _sell = (self._auto_sell_value(cat, gid)
                     if not is_evk and player == self.slot else 0)
            if _sell:
                # auto_sell_unusable_items: gear no party job can ever equip.
                # Bake the chest as a GIL chest -- the game grants natively and
                # shows its own "<n> gil" box, and (as an own chest) the grant
                # loop stays counter-only, exactly like a real gil chest.
                val = _enc_gil_tt(_sell)
                self.idx_desc[idx] = (f"your {_sell} gil (auto-sold "
                                      f"{self._game_item_name(cat, gid)})")
                self._own_chest_idxs.add(idx)
                n_own += 1
            elif (not is_evk and player == self.slot
                    and cat in (D.CAT_KEY, D.CAT_ITEM,
                                D.CAT_WEAPON, D.CAT_ARMOR)):
                val = _enc_item_tt(cat, gid)
                self.idx_desc[idx] = f"your {self._game_item_name(cat, gid)}"
                self._own_chest_idxs.add(idx)
                n_own += 1
            elif not is_evk and player == self.slot and ID.is_gil(iid):
                val = _enc_gil_tt(ID.gil_amount(iid))
                self.idx_desc[idx] = f"your {ID.gil_amount(iid)} gil"
                self._own_chest_idxs.add(idx)   # native gil + counter-only grant
                n_own += 1
            else:
                who = ("your" if player == self.slot
                       else self.player_names.get(player, f"Player{player}") + "'s")
                try:
                    item_name = self.item_names.lookup_in_slot(iid, player)
                except Exception:
                    item_name = f"item{iid}"
                # Own auto-sellable gear forced down this path (event-key
                # chests) is delivered by the grant loop, which pays gil for
                # it -- name the box for what actually arrives.
                if is_evk and player == self.slot:
                    _v = self._auto_sell_value(cat, gid)
                    if _v:
                        item_name = f"{_v} Gil"
                self.idx_desc[idx] = f"{who} {item_name}"
                self._remote_chest_idxs.add(idx)
                sid = self._remote_sid(who, item_name)
                val = filler | ((sid & 0x7FFF) << 16)
                n_remote += 1
            self.tt_values[idx] = val
        # Dynamic bonus-dungeon chest names: NOT baked per (dungeon, ordinal)
        # anymore -- up to 220 bank entries on a 4x-bonus-dungeon seed, which
        # starved the shared budget down to a 4-char cap ("your!" live
        # 2026-08-07). The names are kept CLIENT-SIDE; _bonus_dyn_loop authors
        # the next chest's name into one of the two wide on-disc dyn slots
        # (sids base+R / base+R+1, see tome_names.DYN_SLOTS) and arms that sid
        # via the BDC1 mailbox. Decoupled from spell_tomes like the statics.
        self._dyn_names = {}
        for dg, cap in sorted((self.bonus_dyn_caps or {}).items()):
            for o in range(cap):
                info = self.locations_info.get(ID.dyn_chest_loc_id(dg, o))
                if not info:
                    continue
                who = ("your" if info.player == self.slot
                       else self.player_names.get(info.player,
                                                  f"Player{info.player}")
                       + "'s")
                try:
                    item_name = self.item_names.lookup_in_slot(info.item,
                                                               info.player)
                except Exception:
                    item_name = f"item{info.item}"
                # Own auto-sold gear arrives as gil (Path B), so name the box
                # for what the player actually receives, not the phantom item.
                if info.player == self.slot and ID.is_item(info.item):
                    _c, _g = ID.item_cat_gid(info.item)
                    _v = self._auto_sell_value(_c, _g)
                    if _v:
                        item_name = f"{_v} Gil"
                self._dyn_names[(dg, o)] = (who, item_name)
        # The dyn-slot bank entries live in relocating heap like the shop name
        # banks -- author them through the same floating-DataPatch machinery
        # (locate by the baked sentinel bytes, reconcile per tick, rescan on
        # relocation). Rides _extra_patches whether or not AP shops are on.
        self._dyn_slot_patch = None
        if self._dyn_names:
            sig = (TN.dyn_slot_entry(False) * TN.DYN_SLOTS)  # adjacent entries
            self._dyn_slot_patch = BP.DataPatch("dynslot:names", sig, sig)
            # replace (not append) any prior scout's instance -- a reconnect
            # re-runs this block and must not stack dead patch objects
            self._extra_patches = [p for p in self._extra_patches
                                   if p.name != "dynslot:names"] \
                + [self._dyn_slot_patch]
        # Key-item box names (KEY_NAME.MSG bake): every native key-item grant
        # (event-key chest or NPC handover) is stripped by the client, so each
        # vanilla key id surfaces in the "You obtain the {key}." box at exactly
        # ONE location per seed -- rename its bank entry to that location's AP
        # item. Own items show the bare name; remote items show "{player}'s
        # {item}" (mild "the {player}'s" clunk, but ownership beats mystery).
        key_locs = {kid: ID.loc_id(idx)
                    for idx, kid in D.EVENT_KEY_CHESTS.items()}
        key_locs.update({kid: ID.npc_loc_id(o)
                         for kid, o in D.KEY_NPC_ORDINALS.items()})
        for kid, lid in key_locs.items():
            info = self.locations_info.get(lid)
            if not info:
                continue
            try:
                nm = self.item_names.lookup_in_slot(info.item, info.player)
            except Exception:
                nm = f"item{info.item}"
            if info.player != self.slot:
                who = self.player_names.get(info.player, f"Player{info.player}")
                nm = f"{who}'s {nm}"
            self._key_names[kid] = nm
        # The Bikke scene's "You obtain a ship." box is the same map-MSG obtain
        # message but the Ship is a VEHICLE, not one of the 36 key items -- map
        # it by sentence subject for the _mapmsg_loop.
        # Same for the Smith's "You obtain Excalibur." (native Excalibur is a
        # stripped weapon; the Smith spot is a randomized AP location).
        for subj, ordn in (("ship", D.BIKKE_NPC_ORDINAL),
                           ("excalibur", D.SMITH_NPC_ORDINAL)):
            binfo = self.locations_info.get(ID.npc_loc_id(ordn))
            if not binfo:
                continue
            try:
                bnm = self.item_names.lookup_in_slot(binfo.item, binfo.player)
            except Exception:
                bnm = f"item{binfo.item}"
            if binfo.player != self.slot:
                who = self.player_names.get(binfo.player,
                                            f"Player{binfo.player}")
                bnm = f"{who}'s {bnm}"
            self._keybox_extra[subj] = bnm
        if self._key_names:
            # This mapping reveals which AP item hides behind every key-item
            # location -- a direct seed spoiler. Debug-only, like the shop dump.
            logger.debug("Key-item box names: " + "; ".join(
                f"{D.KEY_ITEMS.get(k, k)} -> {v}"
                for k, v in sorted(self._key_names.items())))
        base_tag = "base 107 +tomes" if tomes_on else "base 43 no-tomes"
        logger.info(f"Chests: {n_own} own (native name), {n_remote} remote "
                    f"(on-disc AP names via {len(self._remote_names)} deduped "
                    f"bank entries, {base_tag})"
                    + (f"; {len(self._event_key_natives)} event-key "
                       f"(path-B forced, AP name, grant-loop delivery)"
                       if self._event_key_natives else ""))

    def _build_bake(self, on_disc):
        """The full on-disc bake spec for launcher.ensure_ppsspp: enabled code
        features + every seed data table (shuffles, scaling, AP chest contents).
        Built AFTER the scout so the treasure table carries the AP mapping."""
        data = BP.bake_data_patches(enc_mult=self.enc_mult, xp_mult=self.xp_mult,
                                    gil_mult=self.gil_mult, slot_data=self.slot_data,
                                    boss_mult=self.boss_mult,
                                    monster_mult=self.monster_mult)
        if self.tt_values:
            data.append({"name": "treasure", "iso_off": D.TREASURE_TABLE_START,
                         "count": D.TREASURE_COUNT, "values": dict(self.tt_values)})
        # Event-key chests (D.EVENT_KEY_CHESTS): opening runs the FIF event (path A);
        # the true path-A/B gate is unfound (read-bps dead in 1.15.3; the completion-
        # flag poke and the 0x08843c00 detour were both REFUTED). DELIVERY is correct
        # regardless -- scout classifies these remote-style (filler baked + grant-loop
        # delivery + _strip_event_key_natives removes the native key). The box's
        # vanilla key name is fixed COSMETICALLY by the key_names bake below: the
        # "You obtain the {key}." box reads KEY_NAME.MSG by key id, and extern_bake
        # re-authors each granted key's entry to its location's AP item name (safe:
        # every native key grant is stripped, so each key id surfaces at exactly one
        # location per seed). Also covers NPC handovers. See [[event-key-chests]].
        feats = dict(on_disc or {})
        # ON_DISC_ALWAYS features added after a seed was GENERATED are missing
        # from that seed's slot-data on_disc dict (it is frozen at gen time).
        # They are always-on by definition, so assert them client-side too --
        # otherwise an in-progress older seed never gets the fix (live lesson
        # 2026-08-01: mystic_door_gate baked as a no-op on a pre-198 seed).
        from ..options import ON_DISC_ALWAYS
        for _always in ON_DISC_ALWAYS:
            feats.setdefault(_always, True)
        # Chaos Shrine basement pools + Chaos'-floor encounters. These shipped as
        # TOP-LEVEL slot_data keys before 2026-08-04 instead of on_disc entries,
        # so _build_bake never saw them and they were baked on no seed at all --
        # the pools were dead in-game while looking enabled everywhere else. The
        # gen side is fixed; derive them here too so seeds rolled before the fix
        # (and any seed predating the options entirely) still get them.
        # setdefault, so a correctly-shipped on_disc value always wins.
        _sd = self.slot_data or {}
        feats.setdefault("chaos_floor_pools",
                         bool(_sd.get("harder_dungeon_encounters")))
        # DefaultOnToggle: absent means "seed predates the option", not "off".
        feats.setdefault("chaos_floor_encounters",
                         bool(_sd.get("chaos_floor_encounters", True)))
        # Super Dash needs no derivation: it is ON_DISC_ALWAYS as of v268, so
        # the loop above already asserts it on every seed, old ones included.
        # (Through v267 it rode auto_dash, which meant an auto_dash-off seed
        # could never opt in from the in-game Config menu. The Config Dash bit
        # + held dash button remain the runtime gate.)
        # Desert + marsh/river per-tier pools (v230). Rides overworld_u16, which
        # every harder-overworld seed already carries -- derive it here so seeds
        # rolled before the option existed still get the fix, exactly like
        # chaos_floor_pools above. Also load-bearing on an OLD seed: through v229
        # the overworld u16 companion was written over the DESERT table, so
        # without a re-bake that seed keeps rolling Goblins on every desert tile.
        feats.setdefault("terrain_pools", bool(feats.get("overworld_u16")))
        # The u16 companion high-byte table used to be a DataPatch homed on the
        # desert table; it now lives inside the cave segment, so its bytes have
        # to reach patch_iso as bake context. Missing (a pre-companion seed) ->
        # apply_overworld_u16 bakes an all-zero table and every id stays u8.
        _hi = RANDO.ow_hi_from_slot_data(_sd)
        if _hi:
            feats["_ow_hi"] = _hi
        # Open-progression map edits as BAKE CONTEXT (not a FEATURES entry, like
        # _ow_hi above): map_bake writes the selected foot trails / canoe rivers /
        # northern docks into MAP_00_AMD.BIN so they exist before the overworld
        # arena is ever decompressed. Folding the three toggles into the spec means
        # bake_hash32 covers them, so flipping one re-bakes instead of booting a
        # cached ISO carrying the other layout. Absent (all three off) -> no bake.
        _ow_map = {k: bool(_sd.get(k)) for k in
                   ("early_open_progression", "extended_open_progression",
                    "northern_docks")}
        if any(_ow_map.values()):
            feats["_ow_map"] = {"early": _ow_map["early_open_progression"],
                                "extended": _ow_map["extended_open_progression"],
                                "docks": _ow_map["northern_docks"]}
        # DangerousForests picks its normal vs harder tier list at bake time from
        # the harder_encounters flag. Inject it as bake context ONLY when forests are
        # on (it's not a FEATURES entry, so patch_iso never tries to "apply" it; but
        # it folds into bake_hash32 so toggling harder re-bakes the forest tables).
        if feats.get("dangerous_forests"):
            feats["harder_encounters"] = bool((self.slot_data or {}).get("harder_encounters"))
        # crystals_needed < 4: bake context for the bikke_ship_split wrapper
        # cave (crystal-count leg; see [[crystal-count-re]]). Not a FEATURES
        # key; folds into bake_hash32 so a different N re-bakes. 0 is valid
        # (orb opens immediately) -- gate on None, not truthiness.
        _cn = (self.slot_data or {}).get("crystals_needed")
        if _cn is not None and int(_cn) < 4:
            feats["crystals_needed"] = int(_cn)
        # bonus_dungeon_crystals: bake context for the SAME wrapper cave -- flips its
        # counting source from the four Fiend flags to the 4 client-set shadow bits
        # (installs the leg even at the default N=4). Not a FEATURES key (rides
        # bikke_ship_split); folds into bake_hash32 via the truthy-name path so
        # toggling re-bakes. See bonus-dungeon-crystals memory.
        if (self.slot_data or {}).get("bonus_dungeon_crystals"):
            feats["bonus_dungeon_crystals"] = True
        # lute_tablets: physical descent blocker below the Chaos Shrine block,
        # spawned while the Lute is unassembled. Value = tablets required (folds
        # into bake_hash32 so a different N re-bakes; the future "N tablets"
        # message reads it). Enabled only in tablets seeds. See [[lute-tablets]].
        if self.lute_tablets_required:
            feats["lute_block_gate"] = int(self.lute_tablets_required)
        # levistone_shards: BAKE CONTEXT ONLY -- there is no ELF feature by this
        # name (the FEATURES loop iterates its own dict, so an unknown key is
        # simply ignored), and no cave. It tells the KEY_EXP desc author that
        # this seed borrows key id 36, and folds the shard count into
        # bake_hash32 so changing N re-bakes. See iso_patcher's _descs block.
        if self.levistone_shards_required:
            feats["levistone_shard_gate"] = int(self.levistone_shards_required)
        # equipment_runes: battle-usability gate on activatable equipment until
        # story flag 62 is set (this client sets it at the rune threshold). The
        # gate itself is threshold-independent (it only reads the flag), so a
        # bare True suffices for the bake. See [[equipment-rune-gate]].
        if self.equipment_runes_required:
            feats["equipment_rune_gate"] = True
        # Boss minions: the gen-rolled plan rides in as bake context (folds into
        # bake_hash32 -> a different plan re-bakes). apply_boss_minions edits the
        # formation records; ms2_bake rebuilds the MS2_<fid> sprite packs.
        if feats.get("boss_minions"):
            plan = (self.slot_data or {}).get("boss_minions_plan")
            if plan:
                feats["boss_minions_plan"] = plan
            else:
                feats["boss_minions"] = False   # old seed w/o plan: stay vanilla
        # Same-instant multi-kills race the boss-dissolve machinery and freeze
        # the battle (minion-multikill-dissolve-freeze memory); the serializer
        # detour rides along whenever minions are actually baked.
        feats["minion_death_serializer"] = bool(feats.get("boss_minions"))
        # Bonus-dungeon dynamic chests: on-disc strip+mailbox detour. Exec bps
        # are dead in player sessions (launcher forces FastMemoryAccess=True),
        # so the game-side detour is the ONLY working detection. Baked whenever
        # the seed has any dynamic cap; inert (remaining=0) until armed.
        feats["bonus_dyn_chests"] = any(
            v > 0 for v in (self.bonus_dyn_caps or {}).values())
        bake = {"features": feats, "data": data}
        # Remote box names: install the name-override detour + grow the extended
        # name bank. DECOUPLED from spell_tomes (2026-07-24) -- extern_bake grows
        # the bank with or without the tome block, and the box detour stores the
        # string id directly (never through the bounded getter), so remote AP
        # names (Lute Tablet, other players' items, bonus-dungeon chests) show
        # regardless of spell_tomes. The scout picks the sid base (43 vs 107) to
        # match; empty _remote_names keeps this inert.
        if self._remote_names or self._dyn_names:
            feats["remote_chest_names"] = True
            bake["remote_names"] = [list(p) for p in self._remote_names]
            if self._dyn_names:
                # two wide runtime-authored bank entries for the bonus dynamic
                # chest names (sids base+R, base+R+1; see tome_names.DYN_SLOTS)
                bake["dyn_name_slots"] = TN.DYN_SLOTS
        # lute_tablets / equipment_runes: widen the progress-line KEY_NAME slots
        # on disc so the client can rewrite them in place to live ratios in the
        # Key Items menu. The resident bank buffer has NO slack to grow at
        # runtime, so the wide slots must exist on disc; padding is space glyphs
        # -> an un-written slot still just reads its native name. Two DEDICATED
        # lines (user 2026-07-27 -- a shared line clipped the right column):
        #   * Lute (kid 1), aliased by spare id 37  -> "Lute Tabs N of M"
        #   * Battery Circuit (kid 35), borrowed    -> "Runes N of M"
        # BOTH ids are padded when EITHER feature is on: every padded slot is
        # KEY_NAME_GLYPHS+TERM = 25 bytes, and entry 0's exact width 25 is the
        # locator's bank fingerprint (see _keyname_slot_addrs) -- padding only
        # the rune slot would break it in rune-only seeds.
        # levistone_shards joins the pad set (id 36 "Energy Chip", the shard
        # line's borrowed slot). ALL THREE ids are padded when ANY feature is
        # on -- same fingerprint rationale as before (entry 0's exact width is
        # the locator's bank test), and a single pad shape keeps every
        # feature-seed ISO interchangeable.
        if (self.lute_tablets_required or self.equipment_runes_required
                or self.levistone_shards_required):
            bake["pad_key_ids"] = [D.key_item_id("Lute"),
                                   D.RUNE_MENU_SLOT_KEY_ID,
                                   D.SHARD_MENU_SLOT_KEY_ID]
        # Onrac Caravan presale line (FM_SHOPUS.PCK bake, see shop_font): the
        # caravan sells the Bottled Faerie -- an AP location -- but its shop row
        # draws a HARDCODED shop-UI string ("Faerie's Bottle") in the shop's own
        # font, so neither the item tables nor KEY_NAME nor the shop name-bank
        # pipeline ever renamed it (user report 2026-08-01). Baked, not patched
        # at runtime: the bank is resident only while a shop window is open, so
        # a floating DataPatch would rescan 24 MB forever while you walk around,
        # and the shop UI caches each row's LENGTH when the window opens, so a
        # live rewrite can't lengthen the row anyway (both live-proven).
        cinfo = self.locations_info.get(ID.npc_loc_id(D.BOTTLE_NPC_ORDINAL))
        if cinfo:
            try:
                cnm = self.item_names.lookup_in_slot(cinfo.item, cinfo.player)
            except Exception:
                cnm = f"item{cinfo.item}"
            cwho = self.player_names.get(cinfo.player, f"Player{cinfo.player}")
            if cinfo.player != self.slot:
                cnm = f"{cwho}'s {cnm}"
            cflags = getattr(cinfo, "flags", 0) or 0
            quality = _flags_quality(cflags)
            bake["caravan_offer"] = {
                "name": cnm,
                "descs": self._ap_desc_cands(quality, cwho),
            }
        # NOTE: the KEY_NAME.MSG bake (extern_bake key_names) was tried here and
        # REVERTED 2026-07-11: KEY_NAME feeds the Key Items MENU, which lists
        # keys the player actually OWNS (delivered AP key items), so renaming
        # entries to each granting location's AP item shows WRONG names for real
        # owned keys. The obtain BOX is fixed by _mapmsg_loop instead; the menu
        # must stay vanilla.
        #
        # v224 obtain_names is NOT that revert coming back: it feeds evm_bake,
        # which authors the per-map USEVM MSG obtain SENTENCE only ("You obtain
        # the warp cube.") -- exactly the box _mapmsg_loop authors in RAM, now
        # done at the source so a fresh bundle copy is born authored (the RAM
        # loop raced relocation, live 2026-08-05). KEY_NAME.MSG / the menu are
        # untouched. Deliberately a DIFFERENT bake key from "key_names" so the
        # menu bake can never be re-enabled by accident.
        if self._key_names:
            bake["obtain_names"] = dict(self._key_names)
        return bake

    def _remote_sid(self, who, item):
        """Absolute bank string id for a remote chest-box name, DEDUPED: the
        same (who, item) pair always maps to one bank entry (100 "your Lute
        Tablet" chests -> one entry). sid = base + index-in-_remote_names,
        where base is 43 (spell_tomes off) or 107 (on); extern_bake grows the
        bank in the same order so sid always indexes a valid entry. Entries
        are PAIRS (v241) so the bake's cap ladder can truncate the item while
        keeping the recipient whole (the "your!" fix)."""
        key = (who, item)
        j = self._remote_name_idx.get(key)
        if j is None:
            j = len(self._remote_names)
            self._remote_name_idx[key] = j
            self._remote_names.append(key)
        return self._remote_base + j

    # ---------------- hint rows ----------------
    # A hint row is not a location, so "already bought" is not in
    # sent_locations and not in the save either -- the gil is spent and the hint
    # is recorded on the SERVER. Keep the bought set there too, in this slot's
    # own DataStorage key, so a reconnect (or a reload to before the purchase)
    # cannot put a paid-for row back on the shelf.
    def _hint_store_key(self):
        return f"ff1psp_hints_{self.team}_{self.slot}"

    async def _hint_store_fetch(self):
        """Read the bought-hints key and subscribe to its updates."""
        key = self._hint_store_key()
        try:
            await self.send_msgs([{"cmd": "Get", "keys": [key]},
                                  {"cmd": "SetNotify", "keys": [key]}])
        except Exception as e:
            logger.info(f"  [hint] could not read purchased hints ({e!r}) -- "
                        f"rows bought earlier may reappear on the shelf")

    async def _hint_store_add(self, shop, k):
        """Record (shop, row) as bought. `add` on a list appends, and `default`
        creates the key on first use, so this needs no read-modify-write."""
        try:
            await self.send_msgs([{
                "cmd": "Set", "key": self._hint_store_key(), "default": [],
                "want_reply": True,
                "operations": [{"operation": "add", "value": [f"{shop}:{k}"]}]}])
        except Exception as e:
            logger.info(f"  [hint] could not persist the purchase ({e!r})")

    def _hint_apply_bought(self, value):
        """Fold a stored ["shop:row", ...] list into the bought set."""
        got = set()
        for ent in (value or []):
            try:
                s, k = str(ent).split(":", 1)
                got.add((int(s), int(k)))
            except (ValueError, TypeError):
                continue
        if got - self._hint_bought:
            self._hint_bought |= got
            self._hint_dirty = True

    def _hint_cleared(self, shop, k):
        """True when every location this row would reveal is already found --
        the row has nothing left to sell, so it leaves the shelf instead of
        taking the player's gil for a hint about checks they already made."""
        try:
            lids = self.hint_rows[shop][k][4]
        except (KeyError, IndexError):
            return False
        return bool(lids) and all(l in self.checked_locations for l in lids)

    def _hint_done(self, shop, k):
        """Row k of `shop` is off the shelf: bought, or nothing left to reveal."""
        return (shop, k) in self._hint_bought or self._hint_cleared(shop, k)

    def _hint_done_rows(self, shop):
        """Set of consumed hint row indices, in the argument shape
        render_shop_ap_tail wants (offset past the AP offers by the caller)."""
        return {k for k in range(len(self.hint_rows.get(shop, ())))
                if self._hint_done(shop, k)}

    def _hint_live_rows(self, shop):
        """[(k, cat, gid, price, label, lids)] for the hint rows still on sale."""
        return [(k, c, g, p, lbl, ids)
                for k, (c, g, p, lbl, ids) in enumerate(self.hint_rows.get(shop, ()))
                if not self._hint_done(shop, k)]

    def _hint_unhinted(self, shop, k):
        """The row's locations that are still worth scouting: this slot's own,
        and not already found. Scouting an id the slot does not own makes the
        SERVER close the socket (see _scout_locations)."""
        try:
            lids = self.hint_rows[shop][k][4]
        except (KeyError, IndexError):
            return []
        known = set(self.missing_locations) | set(self.checked_locations)
        return [l for l in lids
                if l in known and l not in self.checked_locations]

    def _shop_sold(self, shop, k):
        """Has offer row k of `shop` already been bought this seed?"""
        return ID.shop_loc_id(shop, k) in self.sent_locations

    def _shop_sold_rows(self, shop):
        """Set of sold row indices of `shop` -- the argument render_shop_ap_tail
        takes. Rows sell in ANY order, so this is a set, not a cursor."""
        return {k for k in range(len(self.shop_rows.get(shop, ())))
                if self._shop_sold(shop, k)}

    def _shop_live_rows(self, shop):
        """[(k, cat, gid, price)] for the rows still on the shelf."""
        return [(k, c, g, p)
                for k, (c, g, p) in enumerate(self.shop_rows.get(shop, ()))
                if not self._shop_sold(shop, k)]

    # A row bought while its shop menu is OPEN stays drawn: the list snapshots
    # its rows at dialog open, but names and descriptions are re-read from the
    # bank every frame. Reverting that row's entry straight to vanilla therefore
    # repaints it as the placeholder's real identity ("X-Potion", "Fully
    # restores HP.") -- an item that looks buyable but isn't. Label it instead,
    # and let the existing map-gated revert (_bank_vanilla, which flips when the
    # party leaves a shop interior) put the vanilla text back once no shop menu
    # can be showing it. Same deferral the Crescent Lake sage's box uses.
    SOLD_NAME = "Sold Out"
    SOLD_DESC = "Already purchased."
    # Shop-shelf tome shortening (user 2026-08-17): "Spell Tome: Firaga" does
    # not survive the name bank's fair-share trim (13-glyph equalize cut the
    # whole spell off, leaving rows that all read "Spell"), so ON THE SHELF a
    # tome shows just its spell name -- "Firaga" -- and the description bar
    # carries the tome-ness ("Spell Tome for test."). Prefix match covers every
    # FF1PSP player's tomes; everywhere else (GUI Shops tab, reward box, hint
    # text) the full name stays.
    TOME_NAME_PREFIX = "Spell Tome: "

    def _shelf_item_name(self, nm):
        """The name a multiworld item wears on an in-game shop SHELF."""
        if nm.startswith(self.TOME_NAME_PREFIX):
            return nm[len(self.TOME_NAME_PREFIX):]
        return nm

    def _info_is_tome(self, info):
        """Is this scouted network item a Spell Tome (any FF1PSP player's)?"""
        try:
            nm = self.item_names.lookup_in_slot(info.item, info.player)
        except Exception:
            return False
        return nm.startswith(self.TOME_NAME_PREFIX)

    def _shop_bank_rows(self, shop):
        """[(k, cat, gid, label_as_sold)] for every row this shop should author
        into the name/desc banks: the live ones under their AP identity, and the
        ones bought DURING THIS VISIT under the sold label.

        Only this visit's sales are labelled. A row sold earlier is already gone
        from the shelf, so no open list can be drawing it, and leaving its entry
        vanilla keeps a natively-dropped copy of that same item id reading its
        real name."""
        rows = []
        for k, (c, g, _p) in enumerate(self.shop_rows.get(shop, ())):
            sold_now = (shop, k) in self._shop_sold_recent
            if self._shop_sold(shop, k) and not sold_now:
                continue                  # long sold -> leave the entry vanilla
            rows.append((k, c, g, sold_now))
        return rows

    def _hint_bank_rows(self, shop):
        """The hint half of _shop_bank_rows: [(k, cat, gid, label_as_sold)] for
        the rows this shop should author. Same rule -- live rows under their
        hint identity, rows spent DURING THIS VISIT under the sold label, and
        anything spent earlier left vanilla."""
        rows = []
        for k, (c, g, _p, _l, _i) in enumerate(self.hint_rows.get(shop, ())):
            sold_now = (shop, k) in self._hint_sold_recent
            if self._hint_done(shop, k) and not sold_now:
                continue
            rows.append((k, c, g, sold_now))
        return rows

    def _bank_tail_shops(self):
        """{shop -> category} for every store with a tail row of either kind.
        A hints-only seed (shop_ap_offers 0) has stores that are not in
        shop_slots at all, and they still need their names authored.

        Deliberately NOT town-filtered, even in shared-tails mode: the bank
        builders run once at scout time to CREATE the located DataPatches, and
        a patch that was never created can never be mutated by a later refresh
        (live 2026-08-17: the first v2 build filtered here while the party was
        outside any town at connect, so zero shopname/shopdesc patches existed
        and every shelf kept its vanilla text). The town gate lives in the
        builders' ROW loops (_town_rows_ok): out-of-town stores register their
        bank and author nothing."""
        out = {}
        for (s, cat, _g, _p) in self.shop_slots:
            out[s] = cat
        for s, rws in self.hint_rows.items():
            if rws:
                out.setdefault(s, rws[0][0])
        return out

    def _town_rows_ok(self, shop):
        """May `shop`'s tail rows be authored into the banks right now?

        Legacy (unique gids): always -- one global authoring serves all towns.
        Shared tails (v2): only while the party stands in that shop's town
        (street map-id latch, _cur_town) -- the same gid means a different row
        in every town, so the banks carry one town's identity at a time. No
        latch yet (save loaded inside a building, before the store-id fallback
        fires) -> author nothing rather than the wrong town."""
        return (not self._shared_tails
                or (self._cur_town is not None
                    and D.SHOP_CITY[shop] == self._cur_town))

    def _build_shop_name_patches(self):
        """DataPatches that write every LIVE AP offer's REAL multiworld item name
        into the game's menu NAME text bank entry for that offer's placeholder
        id, so the shop list shows e.g. 'Kyles High Jump B' instead of the
        placeholder name. A shop's offers are listed in parallel and each owns
        its own placeholder id, so a shop contributes one entry per unsold row;
        after a sale _shop_refresh_banks drops that row's entry back to vanilla.
        Vanilla payload (from the ISO, see name_banks.py) is the RAM search
        signature; both resident copies get patched. Names are menu-font encoded.
        WEAPONS/ARMOR banks are standalone TEXT containers, so they get RE-LAID
        OUT (offset table rewritten, entries repacked) letting long multiworld
        names exceed their placeholder entry's vanilla byte budget (capped at
        NB.MAX_NAME_GLYPHS, word-boundary trimmed if the bank overflows). The
        ITEMS bank lives inside a larger shared container (no re-layout possible
        -- verified live 2026-07-07), so its names stay truncated + space-padded
        to the entry's fixed byte budget."""
        cat_key = {D.CAT_WEAPON: "weapons", D.CAT_ARMOR: "armor",
                   D.CAT_ITEM: "items"}
        per = {}
        hint_gids = {}          # bank key -> {gid} authored as a HINT row
        for s, cat in sorted(self._bank_tail_shops().items()):
            key = cat_key.get(cat)
            if key is None:
                continue
            # Bank tracked -> restore to vanilla if nothing ends up authored.
            # Outside the row loop so a shop whose every row is sold still
            # rebuilds its bank from the vanilla payload.
            per.setdefault(key, [])
            if self._bank_vanilla:
                # _bank_vanilla: inside a bonus dungeon (see _shop_loop). Those
                # DLC shops stock placeholder ids natively and stay VANILLA, so
                # the AP name must not follow the id in there -- same "leave the
                # entry alone" path as sold-out.
                continue
            if not self._town_rows_ok(s):
                continue    # shared gids: only the standing town's identity
            for k, _c, gid, sold in self._shop_bank_rows(s):
                # A SOLD row reads "Sold Out" rather than reverting to vanilla,
                # because the shop list may still be drawing it (see SOLD_NAME).
                # Leaving the shop interior flips _bank_vanilla and restores the
                # real text, which matters because these ids exist outside
                # shops: bonus bosses natively drop some placeholders
                # (Rubicante -> Kikuichimonji w56), and the old icon+TERM shrink
                # rendered those drops with a BLANK name (live 2026-07-20).
                # Residual accepted gap: while an offer is UNSOLD a native drop
                # of its placeholder reads the AP offer's name (drop table is
                # un-RE'd, no safe ids exist to repick).
                if sold:
                    per.setdefault(key, []).append((gid, self.SOLD_NAME))
                    continue
                info = self.locations_info.get(ID.shop_loc_id(s, k))
                if not info:
                    continue
                try:
                    nm = self.item_names.lookup_in_slot(info.item, info.player)
                except Exception:
                    continue
                per.setdefault(key, []).append((gid, self._shelf_item_name(nm)))
            # Hint rows sit in the same banks. Their label is static (no scout
            # needed) and carries the HINT: prefix so a shelf row can never read
            # as an item -- see hints.shelf_name.
            for k, _c, gid, sold in self._hint_bank_rows(s):
                if sold:
                    per.setdefault(key, []).append((gid, self.SOLD_NAME))
                    continue
                per.setdefault(key, []).append(
                    (gid, HINTS.shelf_name(self.hint_rows[s][k][3])))
                hint_gids.setdefault(key, set()).add(gid)
        patches = []
        for key, pairs in per.items():
            cls = BP.ShopBankPatch        # similarity locate (stray-byte proof)
            bank = NB.BANKS[key]
            payload, offs = bank["payload"], bank["entry_offsets"]
            icon = bank["icon_len"]
            g2e = bank["gameid_to_entry"]
            if not pairs:
                # every shop of this category sold out: restore the whole bank
                # to VANILLA (refresh mutates the located patch in place, so
                # without this the LAST offer's authored name would linger).
                if key in NB.RELAYOUT_KEYS:
                    van_full, _base = NB.bank_container(key)
                    patches.append(cls(f"shopname:{key}", van_full,
                                                van_full))
                else:
                    patches.append(cls(f"shopname:{key}", payload,
                                                payload))
                continue
            if key in NB.RELAYOUT_KEYS:
                authored, keep = {}, {}
                for gid, nm in pairs:
                    ei = g2e.get(gid)
                    if ei is None:
                        continue
                    authored[ei] = nm
                    # A hint row spends 6 of its glyphs on the "HINT: " prefix
                    # every name in this bank does not pay. Discount it, or the
                    # overflow trimmer treats a hint as the longest name in the
                    # bank and eats it down to the prefix (see relayout).
                    if gid in hint_gids.get(key, ()):
                        keep[ei] = len(HINTS.NAME_PREFIX)
                res = (NB.relayout_name_bank(key, authored, prefix_keep=keep)
                       if authored else None)
                if res is not None:
                    patches.append(cls(f"shopname:{key}", res[0],
                                                res[1]))
                    continue
                # res None = unfittable/no-change -> fixed-budget path below
            patched = bytearray(payload)
            for gid, nm in pairs:
                ei = g2e.get(gid)
                if ei is None:
                    continue
                s = offs[ei]
                e = offs[ei + 1] if ei + 1 < len(offs) else len(payload)
                budget = e - s - icon - 1          # keep icon bytes + terminator
                if budget <= 0:
                    continue
                patched[s + icon:e - 1] = NB.menu_encode_fit(nm, budget)
            if bytes(patched) != payload:
                patches.append(cls(f"shopname:{key}", payload,
                                            bytes(patched)))
        if patches:
            logger.info(f"  [shop] item-name bank patches ready: "
                        f"{', '.join(p.name for p in patches)}")
        return patches

    @staticmethod
    def _ap_desc_cands(quality, who):
        """AP-offer description phrasings, LONGEST FIRST. Callers with a byte
        budget take the first that fits whole (never a mid-word cut)."""
        Q = quality.capitalize()
        return [f"This is a {quality} item for {who}.",
                f"{Q} item for {who}.", f"This is a {quality} item.",
                f"For {who}: {quality}.", f"{Q} item.", f"{Q}.",
                "AP item.", "AP."]

    def _ap_desc_text(self, quality, who, budget=None):
        """The AP-offer description (quality + recipient always shown). With
        `budget` set, pick the longest phrasing that fits `budget` bytes whole
        (for the fixed-length ITEMS bank, whose entries can't be re-laid out);
        without it, the full phrasing."""
        cands = self._ap_desc_cands(quality, who)
        if budget is None:
            return cands[0]
        for c in cands:
            if len(c) <= budget:
                return c
        return "AP."

    def _tome_desc_text(self, who, budget=None):
        """Description for a shelf row whose NAME was tome-shortened (a bare
        "Firaga" says nothing about being a tome, so the bar does): longest
        phrasing that fits, same ladder discipline as _ap_desc_text."""
        cands = [f"This is a Spell Tome for {who}.",
                 f"Spell Tome for {who}.", "This is a Spell Tome.",
                 f"Tome for {who}.", "Spell Tome.", "Tome."]
        if budget is None:
            return cands[0]
        for c in cands:
            if len(c) <= budget:
                return c
        return "Tome."

    def _items_desc_baseline(self):
        """The ITEMS desc bank as the RUNNING disc has it: vanilla, or with the
        slot_magic Soma/Ether rewrites applied (iso_patcher.slot_magic_item_descs
        -> tome_names.items_desc_bank). Both the search signature and the
        in-place authoring offsets come from here -- same reason the weapon/armor
        path swaps in D.blood_desc_bank."""
        on = bool(((self.slot_data or {}).get("on_disc") or {}).get("slot_magic"))
        if not on:
            return NB.DESC_BANKS["items"]
        cached = getattr(self, "_items_desc_bank_cache", None)
        if cached is None:
            from . import iso_patcher as IP, tome_names as TN
            cached = TN.items_desc_bank(IP.slot_magic_item_descs(True))
            self._items_desc_bank_cache = cached
        return cached

    def _build_shop_items_desc_patch(self, enc):
        """The ITEMS desc bank is grown to 107 entries on disc when spell_tomes
        is on (extern_bake), so the whole-bank RE-LAYOUT the weapon/armor path
        uses can't locate it (its offset table changed). Author the item AP
        offers IN PLACE instead: the 43 vanilla desc ENTRIES survive verbatim
        (right after the grown offset table), so a patch keyed on that
        entries-region blob locates in BOTH the vanilla and grown banks. Each
        placeholder desc is fit-laddered + space-padded to its own byte length,
        keeping the trailing terminator, so no offsets move."""
        bank = self._items_desc_baseline()
        payload, count = bank["payload"], bank["count"]
        offs = bank["entry_offsets"]                     # rel bank start (0x10 hdr)
        base = count * 4                                 # entries region start
        blob = bytearray(payload[base:])                 # RAM search signature
        g2e = NB.BANKS["items"]["gameid_to_entry"]
        touched = False
        any_item_shop = False
        for (s, cat, _gid0, _prices) in self.shop_slots:
            if cat != D.CAT_ITEM:
                continue
            any_item_shop = True
            if self._bank_vanilla:
                continue    # outside a shop interior (_shop_loop)
            if not self._town_rows_ok(s):
                continue    # shared gids: only the standing town's identity
            # One entry per LIVE row: parallel offers each own an item id, so
            # each has its own description entry. Per-entry budgets are fixed
            # here (no re-layout), so the count of authored rows does not shrink
            # anyone's budget.
            for k, _c, gid, sold in self._shop_bank_rows(s):
                info = self.locations_info.get(ID.shop_loc_id(s, k))
                if not info and not sold:
                    continue
                ei = g2e.get(gid)
                if ei is None:
                    continue
                es = offs[ei] - 0x10 - base              # entry start within blob
                ee = ((offs[ei + 1] - 0x10 - base) if ei + 1 < count
                      else len(blob))
                usable = ee - es - 1                      # keep terminator byte
                if usable <= 0:
                    continue
                if sold:
                    text = self.SOLD_DESC
                else:
                    flags = getattr(info, "flags", 0) or 0
                    quality = _flags_quality(flags)
                    who = self.player_names.get(info.player,
                                                f"Player{info.player}")
                    text = (self._tome_desc_text(who, budget=usable)
                            if self._info_is_tome(info)
                            else self._ap_desc_text(quality, who, budget=usable))
                body = enc(text)[:usable]
                body += bytes([NB.MENU_ENC[' ']]) * (usable - len(body))
                blob[es:es + usable] = body              # terminator untouched
                touched = True
        if not touched:
            if any_item_shop:
                # all item shops sold out: restore baseline (see the sold-out
                # lingering-desc note in _build_shop_desc_patches).
                return [BP.ShopBankPatch("shopdesc:items", bytes(payload[base:]),
                                     bytes(payload[base:]))]
            return []
        return [BP.ShopBankPatch("shopdesc:items", bytes(payload[base:]),
                             bytes(blob))]

    def _build_shop_desc_patches(self):
        """DataPatches that author each AP shop offer's DESCRIPTION (the shop's
        bottom text bar) into the menu DESC text bank, e.g. "This is a filler
        item for Player2." (quality + recipient always shown).

        WEAPONS/ARMOR: the desc bank has no per-entry slack, so this RE-LAYS the
        whole bank out (offset table + all entries), authored entries replace the
        placeholders' vanilla text, offsets rebuilt, total length kept.
        ITEMS: authored in place by _build_shop_items_desc_patch (the on-disc
        spell_tomes bank-grow moves the items offset table, defeating re-layout);
        see that method."""
        def enc(s):
            out = bytearray()
            for c in s:
                if c in NB.MENU_ENC:
                    out.append(NB.MENU_ENC[c])
                elif c.isupper() and c.lower() in NB.MENU_ENC:
                    out.append(NB.MENU_ENC[c.lower()])
                elif c in ' _':
                    out.append(NB.MENU_ENC[' '])
            return bytes(out)

        # (gid, maker): maker(budget) -> the entry's text, or None for a row
        # sold during this visit (fixed short label). Deferred because the
        # per-entry budget is only known once every authored row is counted.
        cat_key = {D.CAT_WEAPON: "weapons", D.CAT_ARMOR: "armor"}
        per = {}
        for s, cat in sorted(self._bank_tail_shops().items()):
            key = cat_key.get(cat)
            if key is None:
                continue
            # bank tracked -> baseline restore if nothing gets authored
            per.setdefault(key, [])
            if self._bank_vanilla:
                continue    # in a bonus dungeon, see _shop_loop._bank_vanilla
            if not self._town_rows_ok(s):
                continue    # shared gids: only the standing town's identity
            for k, _c, gid, sold in self._shop_bank_rows(s):
                if sold:
                    per.setdefault(key, []).append((gid, None))
                    continue
                info = self.locations_info.get(ID.shop_loc_id(s, k))
                if not info:
                    continue
                flags = getattr(info, "flags", 0) or 0
                quality = _flags_quality(flags)
                who = self.player_names.get(info.player, f"Player{info.player}")
                if self._info_is_tome(info):
                    # tome-shortened NAME ("Firaga") -> the bar says tome
                    per.setdefault(key, []).append(
                        (gid, lambda b, w=who: self._tome_desc_text(w, budget=b)))
                else:
                    per.setdefault(key, []).append(
                        (gid, lambda b, q=quality, w=who:
                         self._ap_desc_text(q, w, budget=b)))
            # Hint rows: the NAME is only the place, so the description bar is
            # where the count lives -- it is what tells the player whether the
            # price is worth paying.
            for k, _c, gid, sold in self._hint_bank_rows(s):
                if sold:
                    per.setdefault(key, []).append((gid, None))
                    continue
                label = self.hint_rows[s][k][3]
                n = len(self.hint_rows[s][k][4])
                per.setdefault(key, []).append(
                    (gid, lambda b, l=label, cnt=n:
                     HINTS.desc_text(l, cnt, budget=b)))

        # items-bank donors: cut "Can only be used outdoors." (after ctrl 0x6c)
        DONOR_GIDS = {"items": (16, 17, 18)}   # Sleeping Bag / Tent / Cottage
        patches = self._build_shop_items_desc_patch(enc)
        # blood_magic bakes the HP-cost sentence into the on-disc weapon/armor
        # desc banks (extern_bake), so the RAM banks differ from vanilla. The
        # DataPatch search signature and re-layout baseline must be the SAME
        # transformed bank (D.blood_desc_bank == the bake's transform) or the
        # signature misses and shop descs silently stop authoring.
        blood = bool(((self.slot_data or {}).get("on_disc") or {})
                     .get("blood_magic"))
        for key, pairs in per.items():
            bank = D.blood_desc_bank(key) if blood else NB.DESC_BANKS[key]
            payload, count = bank["payload"], bank["count"]
            offs = bank["entry_offsets"]                 # rel bank start (0x10 hdr)
            capacity = len(payload) - count * 4
            ents = [bytearray(payload[offs[k] - 0x10:
                                      (offs[k + 1] - 0x10) if k + 1 < count
                                      else len(payload)])
                    for k in range(count)]
            g2e = NB.BANKS[key]["gameid_to_entry"]
            # Fair share of the bank: the entries we are NOT authoring keep their
            # vanilla bytes, so what is left divides evenly among the ones we
            # are. With several offers per shop this bank holds many more
            # authored entries than it used to, and without a budget every one
            # of them would be built at full length and then hacked back by the
            # mid-word truncation ladder below. _ap_desc_text picks a whole
            # phrasing that fits instead.
            eis = [g2e[gid] for gid, _mk in pairs if g2e.get(gid) is not None]
            spare = capacity - sum(len(e) for i, e in enumerate(ents)
                                   if i not in set(eis))
            # Sold rows carry a fixed short label, so charge their real cost and
            # split what remains among the live ones instead of letting them
            # shrink everybody's share.
            sold_n = sum(1 for _g, mk in pairs if mk is None)
            spare -= sold_n * (len(enc(self.SOLD_DESC)) + 1)
            live_n = max(1, len(eis) - sold_n)
            share = max(8, spare // live_n - 1)            # -1 keeps TERM
            authored = {}
            for gid, maker in pairs:
                ei = g2e.get(gid)
                if ei is not None:
                    text = self.SOLD_DESC if maker is None else maker(share)
                    authored[ei] = bytearray(enc(text) + bytes([NB.TERM]))
            if not authored:
                if not pairs:
                    # whole bank sold out: restore baseline so the LAST offer's
                    # desc doesn't linger on a natively-dropped placeholder.
                    patches.append(BP.ShopBankPatch(f"shopdesc:{key}", payload,
                                                payload))
                continue
            for ei, body in authored.items():
                ents[ei] = body
            if sum(map(len, ents)) > capacity:
                for gid in DONOR_GIDS.get(key, ()):      # free donor tails
                    ei = g2e.get(gid)
                    if ei is None or ei in authored:
                        continue
                    cut = ents[ei].find(0x6c)
                    if cut > 0:
                        ents[ei] = ents[ei][:cut] + bytes([NB.TERM])
            while sum(map(len, ents)) > capacity:        # last resort: truncate
                ei = max(authored, key=lambda k: len(ents[k]))
                if len(ents[ei]) <= 2:
                    break
                ents[ei] = ents[ei][:-2] + bytes([NB.TERM])
            if sum(map(len, ents)) > capacity:
                logger.info(f"  [shop] desc bank {key}: over budget, skipped")
                continue
            new_offs, p = [], 0x10 + count * 4
            for e in ents:
                new_offs.append(p)
                p += len(e)
            body = b"".join(bytes(e) for e in ents)
            new_payload = (b"".join(o.to_bytes(4, "little") for o in new_offs)
                           + body + b"\x00" * (capacity - len(body)))
            assert len(new_payload) == len(payload)
            patches.append(BP.ShopBankPatch(f"shopdesc:{key}", payload, new_payload))
        if patches:
            logger.info(f"  [shop] item-desc bank patches ready: "
                        f"{', '.join(p.name for p in patches)}")
        return patches

    @staticmethod
    def _game_item_name(cat, gid):
        tbl = {D.CAT_KEY: D.KEY_ITEMS, D.CAT_ITEM: D.CONSUMABLE_ITEMS,
               D.CAT_WEAPON: D.WEAPONS, D.CAT_ARMOR: D.ARMOR}.get(cat, {})
        return tbl.get(gid, f"cat{cat}/id{gid}")

    async def _table_loop(self):
        """Verify the runtime treasure table against the scouted AP mapping and
        heal drift. The mapping is BAKED into the ISO, so in normal play every
        tick is one read + zero writes; a save state from before this seed (or
        a failed bake) shows drift and gets the mapped entries re-written."""
        if not self.tt_values:
            return
        base = D.RUNTIME_TREASURE_TABLE
        size = D.TREASURE_COUNT * 4
        announced = False

        async def tick():
            nonlocal announced
            blob = await self.psp.read(base, size)
            bad = [(i, v) for i, v in self.tt_values.items()
                   if struct.unpack_from("<I", blob, i * 4)[0] != v]
            if not bad:
                if not announced:
                    announced = True
                    logger.info(f"  [chests] treasure table verified "
                                f"({len(self.tt_values)} AP entries)")
                return
            if len(bad) > 8:                       # bulk revert -> one block write
                b = bytearray(blob)
                for i, v in bad:
                    struct.pack_into("<I", b, i * 4, v)
                await self.psp.write(base, bytes(b))
            else:
                for i, v in bad:
                    await self.psp.write_u32(base + i * 4, v)
            logger.info(f"  [chests] healed {len(bad)} treasure entries "
                        f"(stale save state or no bake)")

        await self._poll(TT_VERIFY_S, "table_loop", tick)

    # ------------- key-item-add box: author the map-MSG obtain message -------------
    # "You obtain the {key}." (event-key chests + NPC handovers, path A) is a
    # COMPLETE per-key sentence stored in the current MAP's .MSG TEXT bank inside
    # the decompressed map bundle in heap (e.g. MAP01.MSG for Cavern of Earth).
    # Each bank uses a PRIVATE glyph atlas (the bundle carries its own font
    # texture); the GIM region holds a u16 atlas-id table INDEXED BY ASCII CODE
    # (0xffff = glyph not in this map's atlas) -- which is why every global-font
    # search for this text failed for weeks. Cracked 2026-07-11 via a
    # substitution-pattern scan; live-verified (Cavern Star Ruby -> AP name).
    # The loop rewrites each resident bank's obtain-entries to the granting
    # location's AP item name (self._key_names). The bank reloads from disc on
    # every floor load, so the loop re-authors continuously (cheap once cached).
    _MAPMSG_WINDOW = (0x09000000, 0x01000000)
    # Periodic full rescan of the CURRENT known-box map (counted in 2 s ticks) so
    # a bank that RELOCATED to a fresh copy -- leaving the cached address
    # re-authoring a now-dead copy every tick while the LIVE one reads vanilla --
    # gets re-found and authored (the scan authors every resident copy). This is
    # the case a per-tick re-author of the cache can never catch: the 2026-07-17
    # Rosetta box stayed vanilla for a ~3-minute floor visit (~90 ticks), so it
    # was not a timing race -- the loop was authoring the wrong copy / had frozen
    # its cache. Bounded to box-maps (a handful), so boxless maps never pay it.
    # (v224: the race this mitigates is now closed at the source -- evm_bake
    # authors the on-disc bundle, so fresh copies are born authored; this loop
    # remains the repair net for pre-v224 bakes.)
    _MAPMSG_REVERIFY_TICKS = 8
    # Retries when a map-change scan comes back with NOTHING cached (2 s ticks).
    # That is the "bundle not resident yet" signature: the scan fired while the
    # map was still loading, so `cached` stays empty, `box_maps` never learns the
    # map, and NONE of the other rescan triggers can fire -- the loop is frozen
    # vanilla for the whole visit. Live 2026-08-05: player walked into the
    # Western Keep, fought Astos ~80 s later, and his handover box still read
    # "You obtain the crystal eye." though the AP item was a Megalixir; walking
    # out and back in authored it immediately. The box-map self-heal above
    # cannot cover this -- it is gated on a map that has ALREADY yielded a bank.
    # Bounded, and skipped on the OVERWORLD (the frequent, genuinely boxless
    # map) so the common case still pays exactly one scan per entry.
    _MAPMSG_EMPTY_RETRIES = 4
    # Map ids are HEX: MAP0C.MSG (Gaia/oxyale), MAP0D.MSG (rat's tail) -- a
    # \d\d suffix missed them (2026-07-11 disc sweep).
    _MAPMSG_RE = re.compile(rb"[A-Z][A-Z0-9_]{1,7}[0-9A-F]{2}\.MSG\x00")
    # Vanilla phrasing varies per key: "the lute" / "a ship" / "nitro powder"
    # (article + casing live in the stored sentence).
    _MAPMSG_OBTAIN = re.compile(r"You obtain (?:the |a |an )?(.+?)\.?$")

    # Visually-similar ASCII fallbacks tried before the wildcard placeholder.
    # A per-map atlas often carries only part of the printable set; recovering a
    # lookalike ('S'->'5', '0'->'O') reads far better than dropping the glyph.
    _MAPMSG_LOOKALIKE = {
        "0": "Oo", "1": "lIi", "2": "Zz", "5": "Ss", "6": "b", "8": "B",
        "O": "0o", "o": "O0", "l": "1Ii", "I": "1li", "i": "1Il",
        "S": "5s", "s": "S5", "Z": "2z", "z": "2Z", "B": "8b", "b": "6B",
        ":": ";.", ";": ":.", "'": "`", "`": "'", "-": "_", "_": "-",
    }
    # Placeholder glyphs tried, in order, for a character the atlas can't render
    # even via case/lookalike. The first one present in this atlas is used as a
    # wildcard so "Spell Tome: Holy" degrades to "S?ell To?e: ?ol?" rather than
    # the unreadable "sell toe olY" (2026-07-13). '.'/space are last resorts.
    _MAPMSG_WILDCARD = "?*#.-' "

    # Citadel of Trials crown gate (v70, pairs with the iso_patcher detour that
    # blocks setStoryFlag(22,1) while the Crown is unowned): while story flag 6
    # (Crown obtained) is CLEAR, the elder's unconditional grant monologue and
    # the throne's refusal box are rewritten in the Citadel .MSG bank so the
    # player learns WHY nothing happens. Matched on normalized decoded text
    # (lowercase letters/spaces only -- page-break control glyphs decode as
    # noise), replacements kept to one box line (<=35 chars; wrap behavior of
    # authored multi-line text is unverified). Original slot bytes are cached
    # and restored the moment the Crown arrives (map reload also restores).
    # Live+disc-verified 2026-07-16: the Citadel bank (USEVM0D -> MAP0D.MSG)
    # holds the elder's ENTIRE 4-box grant monologue as ONE 285-byte entry [1]
    # and the throne refusal as entry [2], so rewriting these two entries
    # covers the whole crownless flow (the elder's walk-off scene still plays;
    # he is present again on room re-entry since his despawn keys on flag 22).
    # '\n' in a replacement = in-box line break, glyph id 0x28 in this bank
    # (control ids sit past glyph_count 0x27; page break = 0x2A, term = 0x12 --
    # decoded from the vanilla disc bundle). The box does NOT auto-wrap
    # (overflow live-verified), so keep authored lines <= ~39 chars.
    _MAPMSG_GATE_BREAK = 0x28
    _MAPMSG_GATE = [
        ("you come bearing the crown",
         "Travelers, you must find the crown in\n"
         "your multiworld before I can allow you\n"
         "into the Citadel."),
        ("granted permission may undergo", "Bring the crown to the elder."),
    ]

    # --- Lute-block slab (lute_tablets seeds) --------------------------------
    # The Chaos Shrine altar-room blocker object (iso_patcher.apply_lute_block_gate)
    # carries message id 0x1ff7 -- the SAME "stone slab" inscription the real
    # descent block uses. While the Lute is unassembled the blocker seals the
    # only tile you can interact from, so rewrite that inscription to say so;
    # once assembled the blocker despawns and the player reads the real block,
    # so the vanilla line is restored (same restore-on-condition pattern as the
    # crown gate above). Message id -> object param was live-proven 2026-07-25.
    # DIGIT-FREE BY NECESSITY: this bank's atlas carries only digits 1-5
    # (live-verified), so an "N of M" count would garble -- and the exact count
    # already shows as "Lute Tabs N of M" in the Key Items menu.
    _MAPMSG_LUTE_SLAB = "stone slab is set in the floor"
    # "lute tablets" lowercase ON PURPOSE: this bank's atlas has no capital 'L',
    # so "Lute" always renders "lute" (case-swap fallback) -- capitalising only
    # "Tablets" looked inconsistent in-game. Vanilla FF1 PSP writes key items
    # lowercase in these boxes anyway ("You obtain the lute."), so this matches
    # the game's own style. Written lowercase explicitly rather than relying on
    # the fallback, so the intent survives any future atlas change.
    _MAPMSG_LUTE_TEXT = ("A stone slab seals the way below.\n"
                         "Assemble the lute tablets to pass.")

    @staticmethod
    def _mapmsg_break(body, inv, default):
        """This bank's in-box line-break glyph id. It is PER-BANK (Citadel 0x28,
        Chaos Shrine 0x3b -- where 0x28 is a real LETTER), so derive it from the
        vanilla entry: control bytes are those with no ASCII mapping, and the
        entry's own terminator is excluded by the caller passing body[:-1]."""
        ctrl = [x for x in body if x not in inv]
        return ctrl[0] if ctrl else default

    def _mapmsg_encode(self, s, remap):
        """ASCII string -> this bank's atlas glyphs. Per glyph: exact -> other
        case (both free) -> visual lookalike -> wildcard placeholder -> dropped.
        Returns (glyph_bytes, dropped) where dropped counts NON-space source
        glyphs that fell through to a lookalike, placeholder, or true drop --
        the caller uses it to prefer a fully-clean render (exact/case only) and
        to fall back to a generic name when a box would come out garbled. Case
        swaps stay free so the clean-render gate keeps its prior meaning."""
        out = bytearray()
        dropped = 0
        wild = 0xFFFF
        for pc in self._MAPMSG_WILDCARD:
            g = remap.get(ord(pc), 0xFFFF)
            if g != 0xFFFF:
                wild = g
                break
        for ch in s:
            g = remap.get(ord(ch), 0xFFFF)
            if g == 0xFFFF and ch.isalpha():
                g = remap.get(ord(ch.swapcase()), 0xFFFF)
            if g != 0xFFFF:
                out.append(g)                    # exact or case swap -- faithful
                continue
            for alt in self._MAPMSG_LOOKALIKE.get(ch, ""):
                g = remap.get(ord(alt), 0xFFFF)
                if g != 0xFFFF:
                    break
            if g != 0xFFFF:
                out.append(g)
                dropped += 1                     # rendered, but not as itself
            elif ch.isspace():
                pass
            elif wild != 0xFFFF:
                out.append(wild)                 # wildcard placeholder
                dropped += 1
            else:
                dropped += 1                     # nothing usable -> drop
        return bytes(out), dropped

    # Generic last-resort rungs for an AP item name that cannot be rendered in
    # this bank's atlas / slot at any length. They say NOTHING FALSE, which is
    # the whole point of the ladder below.
    _MAPMSG_GENERIC = ("An AP item", "AP item", "??? item", "???", "?")

    def _mapmsg_fit(self, ap_name, remap, budget, lead="You obtained ",
                    tag="keybox"):
        """Best renderable text for an AP item name in a `budget`-glyph .MSG
        slot -> (glyph_bytes, authored_text). NEVER returns None: the caller
        must always overwrite the slot.

        THE VANILLA SENTENCE IS NEVER LEFT IN PLACE (2026-07-30). Silently
        bailing when nothing fit used to be the fallback, and it is the worst
        possible outcome: the Cavern Star Ruby box read "You obtain the Star
        Ruby." while the location actually held "sts's Ironclad Card Reward"
        (26 glyphs into a 25-glyph slot -- ONE over). The box named a real,
        WRONG item, so the chest looked un-randomized and the player got no cue
        that an AP item had been sent at all.

        The ladder, most informative first -- every rung is either faithful or
        visibly non-vanilla:
          1. "{lead}{player}'s {item}"      full sentence
          2. "{player}'s {item}"            bare name
          3. "{player}: {item}"             sheds the possessive (1 glyph)
          4. "{item}"                       sheds the owner
          5. "Gil"                          gil amounts only (see below)
          6. any of 1-4 with dropped glyphs best-effort render
          7. "{item cut}.."                 truncated, MARKED as truncated
          8. "An AP item" / "???"           generic, says nothing false
          9. b""                            blank slot (atlas has no '?' at all)

        Glyph availability is PER BANK (every map bundle carries its own atlas),
        so no rung can be assumed present -- ':' included. _mapmsg_encode reports
        a lookalike substitution as a non-clean render, so rungs whose glyphs are
        missing self-skip on the clean pass."""
        # Gil names ("5454 Gil") garble on the many map atlases that lack digit
        # glyphs -- every digit drops, leaving "Gil"/"1 Gil". When the numeric
        # name won't render cleanly, author a GENERIC "Gil" (legible; the player
        # reads the exact amount in the AP client) instead of a broken number.
        # Slot names and item names are ARBITRARY player-supplied UTF-8 -- "¯\\_
        # (ツ)_/¯" is a legal AP slot name and item names carry CJK, emoji, RTL
        # marks, combining accents. _mapmsg_encode is total over any codepoint
        # (unmapped -> case -> lookalike -> '?' wildcard -> dropped), so nothing
        # here can raise, but two things must be scrubbed FIRST:
        #   * C0/C1 CONTROL chars and DEL. These banks store control glyphs
        #     in-band (page break, in-box newline, TERMINATOR), and the slot is
        #     bounded by the offset table -- a stray control byte reaching the
        #     bank could cut the box short or spill into the next entry. Also
        #     collapses \n/\t so a multi-line name can't smuggle a break.
        #   * absurd LENGTH. Nothing beyond the slot can ever be shown, and the
        #     truncation search below is O(budget); cap well above any real
        #     name so the cost stays flat no matter what a slot is called.
        name = "".join(" " if (ch.isspace() or not ch.isprintable()) else ch
                       for ch in ap_name)
        name = " ".join(name.split())[:200]
        is_gil = (name.rstrip().lower().endswith("gil")
                  and any(c.isdigit() for c in name))
        # Remote names are "{player}'s {item}". Split so the ladder sheds the
        # possessive and then the owner before it starts cutting the ITEM name
        # -- the item is what the player needs to read. Split on the SANITIZED
        # name so a control char can't hide the possessive.
        item_only = owner = name
        mo = re.match(r"(.+?)'s (.+)$", name)
        # An all-control / all-whitespace name leaves NOTHING to author: skip
        # straight to the generic rungs rather than "rendering" it as an empty
        # box that silently claims success.
        cands = [lead + name, name] if name else []
        if mo:
            owner, item_only = mo.group(1), mo.group(2)
            cands += [f"{owner}: {item_only}", item_only]

        def _first(strings, clean_only):
            """First candidate that fits. clean_only -> exact/case glyphs only.
            The best-effort pass still refuses a render that lost MORE THAN HALF
            its glyphs: on a sparse atlas an unbounded best-effort happily
            returns near-empty bytes for a full name (an all-drop atlas returned
            b"" while reporting the name as authored), which is a blank box that
            silently claims success and never reaches the generic rungs."""
            for cand in strings:
                e, drop = self._mapmsg_encode(cand, remap)
                if len(e) > budget:
                    continue
                if drop == 0:
                    return e, cand
                if not clean_only and drop * 2 <= len(cand.replace(" ", "")):
                    return e, cand
            return None, None

        # Clean pass, but WITHOUT the owner-shedding rung: shedding is a real
        # loss of information, so the wildcarded-owner rung below gets a shot at
        # it first. (cands still carries it for the best-effort pass.)
        enc, authored = _first(cands[:3] if mo else cands, True)
        if enc is None and is_gil:                   # number unrenderable
            enc, _ = _first(("You obtained Gil", "Gil"), False)
            authored = "Gil"
        if enc is None and mo:
            # The owner is unrenderable in this atlas but the ITEM is clean
            # (slot names are arbitrary UTF-8 -- "¯\\_(ツ)_/¯" is a legal one).
            # Keep a WILDCARDED owner rather than dropping it: a bare "Potion"
            # reads as the player's OWN item, while "?????: Potion" still says
            # this belongs to somebody else. Ownership beats mystery, same call
            # as the _key_names builder makes. The half-drop rule is waived here
            # because the garbling is confined to the owner -- but the owner must
            # render at least one glyph, or there is nothing to signal with.
            e_item, d_item = self._mapmsg_encode(item_only, remap)
            e_own, _ = self._mapmsg_encode(owner, remap)
            cand = f"{owner}: {item_only}"
            e, _ = self._mapmsg_encode(cand, remap)
            if d_item == 0 and e_own and len(e) <= budget:
                enc, authored = e, cand
        if enc is None and mo:                       # now shed the owner
            enc, authored = _first([item_only], True)
        if enc is None:
            enc, authored = _first(cands, False)     # best-effort (drops a few)
        if enc is None and item_only:
            # Cut the ITEM name and MARK the cut with ".." so the box reads as
            # truncated rather than as a different item.
            #
            # Search SOURCE PREFIXES, never a byte slice of the full render: a
            # source char is 0 OR 1 glyph bytes (spaces and unrenderable chars
            # emit nothing when the atlas has no wildcard), so byte index and
            # char index desync the moment anything drops -- a byte slice would
            # write different text than the log reports. Bounded by `budget`
            # (~25), so this is a couple dozen encodes of a short string.
            dots, ddrop = self._mapmsg_encode("..", remap)
            if ddrop == 0 and dots and budget > len(dots) + 2:
                room = budget - len(dots)
                for k in range(min(len(item_only), budget), 0, -1):
                    e, drop = self._mapmsg_encode(item_only[:k], remap)
                    if (len(e) <= room and e
                            and drop * 2 <= len(item_only[:k].replace(" ", ""))):
                        enc, authored = e + dots, item_only[:k].rstrip() + ".."
                        break
        if enc is None:
            enc, authored = _first(self._MAPMSG_GENERIC, True)
        if enc is None:
            enc, authored = _first(self._MAPMSG_GENERIC, False)
        if enc is None:
            # Not one printable rung survives this atlas -- blank the slot rather
            # than leave a vanilla item name asserting something false.
            enc, authored = b"", ""
        if ap_name not in authored:                  # a rung BELOW the full name
            logger.info(f"  [{tag}] {ap_name!r} does not fit the "
                        f"{budget}-glyph slot -> {authored!r}")
        return enc, authored

    async def _mapmsg_scan(self):
        """Scan the heap window for resident map bundles' .MSG banks and cache
        (text_addr, size, remap_addr) triples. Full-window read -- called only
        on map change / cache invalidation, and it breathes between chunks."""
        start, size = self._MAPMSG_WINDOW
        try:
            buf = await self.psp.read_chunked(start, size, breathe=SCAN_BREATHE_S)
        except Exception:
            return None      # transient read failure -- caller retries (NOT "no
            #                  box here"; returning [] would freeze the cache).
        banks = []
        for hit in self._MAPMSG_RE.finditer(buf):
            pos = hit.start()
            # MSG directory record: (offset, size, size2) u32 triple at +24;
            # sz = the bank's byte length. 0x40..0x4000 = empirical sane
            # bounds for these banks, nothing structural.
            try:
                _off, sz, _sz2 = struct.unpack_from("<III", buf, pos + 24)
            except struct.error:
                continue
            if not (0x40 <= sz <= 0x4000):
                continue
            # The MSG payload follows its directory record; find its TEXT magic.
            t = buf.find(b"TEXT", pos, pos + 0x4000)
            if t < 0 or t < 4:
                continue
            bank_off = t - 4                     # bank = 4 zero bytes + 'TEXT'
            bank = buf[bank_off:bank_off + sz]
            if len(bank) < 0x14 or bank[4:8] != b"TEXT":
                continue
            # TEXT bank header: the u32 at +8 packs the entry count SHIFTED
            # LEFT 8 (low byte is a flags/dim field); total size u32 at +0xC;
            # 0x14 = fixed header size, so header + offset table must fit
            # under total.
            cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
            total = struct.unpack_from("<I", bank, 0xC)[0]
            if not (0 < cnt < 0x80) or not (0x14 + cnt * 4 <= total <= sz):
                continue
            # ASCII->atlas remap table: after the bank (and variable zero pad)
            # sits a 6-byte header {u16 0x0012, u16 glyph_count, u16 term_id};
            # the ASCII-indexed u16 table starts at header+0xC. (The old
            # positional space/'e' probe locked onto the pad zeros on banks
            # with longer padding -- MAP03/05/08/0B/11/12/17/19/1B never
            # authored; found via the 2026-07-11 disc sweep.)
            # ASCII->atlas remap table: sits 4 bytes after a u16 header
            # {0x0012, glyph_count} that follows the bank (+ variable pad).
            # Glyph ids are per-atlas frequency-ordered -- space is NOT always
            # 0 (MAP0F: '.'=0, space=1), which killed every fixed-content
            # probe (missed canoe/levistone/warp-cube/crystal-eye banks;
            # 2026-07-11 disc sweep). So SELF-VALIDATE: a candidate base must
            # cleanly decode the bank's first entry to mapped ASCII.
            first_a = struct.unpack_from("<I", bank, 0x10)[0]
            first_b = (struct.unpack_from("<I", bank, 0x14)[0]
                       if cnt > 1 else total)
            if not (0x14 <= first_a < first_b <= total):
                continue
            probe_body = bank[first_a:first_b - 1]      # sans terminator
            table = None
            for cand in range(bank_off + total, bank_off + total + 0x80):
                if cand + 0x100 > len(buf):
                    break
                if struct.unpack_from("<H", buf, cand)[0] != 0x12:
                    continue
                h1 = struct.unpack_from("<H", buf, cand + 2)[0]
                if not (0 < h1 < 0x200):
                    continue
                base = cand + 4
                inv = {}
                for c in range(0x20, 0x7F):
                    g = struct.unpack_from("<H", buf, base + c * 2)[0]
                    if g != 0xFFFF and g not in inv:
                        inv[g] = c
                # bytes >= h1 (the glyph count) are CONTROL OPCODES, not
                # glyphs. Accept only if EVERY byte of entry 0 is either a
                # mapped ASCII glyph or a control byte -- that is what rejects
                # lookalike 0x12-headed tables in the window.
                if probe_body and all(x in inv or x >= h1
                                      for x in probe_body):
                    table = base
                    break
            if table is None:
                continue
            banks.append((start + bank_off, total, start + table))
        return banks

    async def _mapmsg_author(self, text_addr, total, table_addr):
        """Decode one cached bank; rewrite any 'You obtain the {key}.' entry whose
        key has an AP name. Returns False if the cache is stale (bank moved)."""
        try:
            bank = await self.psp.read(text_addr, total)
        except Exception:
            return False
        if len(bank) < 0x14 or bank[4:8] != b"TEXT":
            return False
        cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
        if struct.unpack_from("<I", bank, 0xC)[0] != total or not (0 < cnt < 0x80):
            return False
        raw = await self.psp.read(table_addr, 0x100)
        remap = {c: struct.unpack_from("<H", raw, c * 2)[0]
                 for c in range(0x20, 0x7F)}
        inv = {}
        for c, g in remap.items():
            if g != 0xFFFF and g not in inv:
                inv[g] = chr(c)
        offs = struct.unpack_from(f"<{cnt}I", bank, 0x10)
        ends = list(offs[1:]) + [total]
        # Crown-obtained story flag drives the Citadel gate lines below; if the
        # read fails, treat as owned so vanilla text is never wrongly replaced.
        try:
            _ca, _cm = D.KEY_ITEM_FUNCTION_BITS[2]      # Crown (0x08D1151C, 0x40)
            crown = bool((await self.psp.read(self.sa(_ca), 1))[0] & _cm)
        except Exception:
            crown = True
        # Giver-sage boxes: authored while his check is unsent, restored after.
        # Restore is DEFERRED until the player leaves Crescent Lake (2026-08-01):
        # restoring 2s after the check repainted the still-open box mid-read --
        # the vanilla lore's tail ("...world...") bled into the obtain sentence.
        sage_done = ID.npc_loc_id(D.SAGE_NPC_ORDINAL) in self.sent_locations
        if sage_done and self._sage_orig:
            try:
                _mid = struct.unpack("<I", await self.psp.read(
                    self.sa(D.FIELD_MAP_ID_SA), 4))[0]
                if _mid == D.CRESCENT_LAKE_MAP_ID:
                    sage_done = False        # keep authored text until he leaves
            except Exception:
                pass
        sage_name = self._key_names.get(D.key_item_id("Canoe"))
        # Lute-block slab: only in tablets seeds, and only while unassembled.
        # Fail SAFE (treat as owned -> don't author) if the read fails, so a
        # transient error can never leave a "seals the way" line on the real
        # block after the player has earned the Lute.
        lute_gate = bool(self.lute_tablets_required)
        lute_owned = True
        if lute_gate:
            try:
                lute_owned = bool((await self.psp.read(
                    self.sa(D.LUTE_KEYITEM_ADDR), 1))[0] & D.LUTE_KEYITEM_MASK)
            except Exception:
                lute_owned = True
        for a, b in zip(offs, ends):
            if not (0x14 <= a < b <= total):
                continue
            body = bank[a:b]
            # Entries are exactly offset-delimited: last byte = the terminator.
            # The terminator id is PER-ATLAS like everything else (Cavern 0x0d,
            # Cornelia Castle 0x16 -- where 0x0d is the letter 'y'!), so never
            # search for a fixed TERM byte; strip/reuse the entry's own.
            term = body[-1:]
            txt = "".join(inv.get(x, "�") for x in body[:-1])
            key = (text_addr, a)
            if crown and key in self._citadel_orig:
                # Crown arrived mid-visit: put the vanilla line back.
                orig = self._citadel_orig.pop(key)
                if bytes(body) != orig:
                    await self.psp.write(text_addr + a, orig)
                    logger.info("  [citadel] vanilla line restored")
            elif not crown:
                norm = " ".join(re.sub(r"[^a-z ]+", " ", txt.lower()).split())
                repl = next((r for p, r in self._MAPMSG_GATE if p in norm), None)
                if repl is not None:
                    self._citadel_orig.setdefault(key, bytes(body))
                    # The break id is PER-BANK. 0x28 was live-verified as this
                    # bank's line break on 2026-07-16, but it is a real LETTER in
                    # other atlases -- and live 2026-08-08 it rendered as 'z' here
                    # ("find the crown inZyour multiworld"), so the constant no
                    # longer describes the bank the game is actually showing.
                    # inv maps glyph -> ASCII, so a break id PRESENT in inv is by
                    # definition a letter: derive from the vanilla entry instead,
                    # exactly as the lute-slab path below already does.
                    brk = (self._MAPMSG_GATE_BREAK
                           if self._MAPMSG_GATE_BREAK not in inv
                           else self._mapmsg_break(body[:-1], inv,
                                                   self._MAPMSG_GATE_BREAK))
                    enc = bytes([brk]).join(
                        self._mapmsg_encode(seg, remap)[0]
                        for seg in repl.split("\n"))
                    sp = remap.get(0x20, 0xFFFF)
                    pad = bytes([sp]) if sp != 0xFFFF else term
                    budget = b - a - 1
                    enc = enc[:budget] + pad * (budget - len(enc)) + term
                    if bytes(body[:len(enc)]) != enc:
                        await self.psp.write(text_addr + a, enc)
                        logger.info(f"  [citadel] gated line authored "
                                    f"(entry @{a:#x}, break {brk:#04x}"
                                    f"{' DERIVED' if brk != self._MAPMSG_GATE_BREAK else ''}"
                                    f"): {repl!r}")
                    continue
            # --- Lute-block slab inscription (lute_tablets seeds) -------------
            if lute_gate:
                lnorm = " ".join(re.sub(r"[^a-z ]+", " ", txt.lower()).split())
                if lute_owned and key in self._lute_slab_orig:
                    # Lute assembled mid-visit: put the vanilla inscription back
                    # (the blocker is gone; this entry is now the real block's).
                    orig = self._lute_slab_orig.pop(key)
                    if bytes(body) != orig:
                        await self.psp.write(text_addr + a, orig)
                        logger.info("  [lute-slab] vanilla inscription restored")
                elif not lute_owned and self._MAPMSG_LUTE_SLAB in lnorm:
                    self._lute_slab_orig.setdefault(key, bytes(body))
                    brk = self._mapmsg_break(body[:-1], inv,
                                             self._MAPMSG_GATE_BREAK)
                    enc = bytes([brk]).join(
                        self._mapmsg_encode(seg, remap)[0]
                        for seg in self._MAPMSG_LUTE_TEXT.split("\n"))
                    sp = remap.get(0x20, 0xFFFF)
                    pad = bytes([sp]) if sp != 0xFFFF else term
                    budget = b - a - 1
                    enc = enc[:budget] + pad * (budget - len(enc)) + term
                    if bytes(body[:len(enc)]) != enc:
                        await self.psp.write(text_addr + a, enc)
                        logger.info("  [lute-slab] gated inscription authored")
                    continue

            # --- Crescent Lake giver-sage boxes (talk-detect, 2026-07-24) -----
            # While the Sage check is unsent, rewrite his two lore boxes (MAP08
            # entries 0x17/0x18, matched by their unique vanilla text) to the AP
            # obtain sentence; after the check, restore vanilla lore. The talk
            # DETECTOR is the dialog-state latch in _npc_loop -- this is only
            # the on-screen text.
            if sage_done and key in self._sage_orig:
                orig = self._sage_orig.pop(key)
                if bytes(body) != orig:
                    await self.psp.write(text_addr + a, orig)
                    logger.info("  [sage-box] vanilla lore restored")
            elif not sage_done and sage_name:
                snorm = " ".join(re.sub(r"[^a-z ]+", " ", txt.lower()).split())
                budget = b - a - 1
                enc = authored = None
                if D.SAGE_BOX_VANILLA in snorm:
                    # Same rule as the key-item obtain box below: NEVER fall
                    # through to the vanilla lore here -- an unauthored giver box
                    # leaves the player no cue that a check exists at all.
                    enc, authored = self._mapmsg_fit(
                        sage_name, remap, budget, tag="sage-box")
                elif D.SAGE_BOX2_VANILLA in snorm:
                    # Flavour line, not an item claim -- vanilla is a fine
                    # fallback here, so no generic ladder.
                    for clean in (True, False):
                        for cand in ("Take it, Warrior of Light!",
                                     "Take it, Warrior of Light"):
                            e, drop = self._mapmsg_encode(cand, remap)
                            if len(e) <= budget and (drop == 0 or not clean):
                                enc, authored = e, cand
                                break
                        if enc is not None:
                            break
                if enc is not None:
                    self._sage_orig.setdefault(key, bytes(body))
                    sp = remap.get(0x20, 0xFFFF)
                    pad = bytes([sp]) if sp != 0xFFFF else term
                    enc = enc[:budget] + pad * (budget - len(enc)) + term
                    if bytes(body[:len(enc)]) != enc:
                        await self.psp.write(text_addr + a, enc)
                        # Address logged since 2026-08-10: without it a bundle
                        # showing "authored but no check" cannot say WHERE the
                        # talk latch should have pointed, which is what made
                        # this class need a live session every single time.
                        logger.info(f"  [sage-box] authored: {authored!r} "
                                    f"at {text_addr + a:#x} ({len(enc)}B)")
                    continue
            mo = self._MAPMSG_OBTAIN.match(txt)
            if not mo:
                continue
            kid = D.key_item_id(mo.group(1))
            ap_name = self._key_names.get(kid) if kid else None
            if not ap_name:
                # non-key-item obtain boxes (the Ship at Bikke's scene)
                ap_name = self._keybox_extra.get(mo.group(1).lower())
            if not ap_name:
                continue
            budget = b - a - 1
            enc, authored = self._mapmsg_fit(ap_name, remap, budget,
                                             lead="You obtained ", tag="keybox")
            # Fill the whole slot: truncate long, SPACE-pad short (the chest-box
            # renderer bounds by the offset table, not the terminator -- assume
            # this one may too, so never leave stale glyphs after our text).
            sp = remap.get(0x20, 0xFFFF)
            pad = bytes([sp]) if sp != 0xFFFF else term
            enc = (enc[:budget] + pad * (budget - len(enc)) + term)
            if bytes(body[:len(enc)]) != enc:
                await self.psp.write(text_addr + a, enc)
                logger.info(f"  [keybox] authored obtain box: "
                            f"{mo.group(1)} -> {authored}")
        return True

    async def _mapmsg_loop(self):
        """Keep every resident map bundle's obtain-message authored. Rescans the
        heap window on map change or cache loss; otherwise each tick just
        re-validates/re-authors the few cached banks (a handful of small reads)."""
        # NOTE: runs even with no key names -- the Citadel crown-gate lines
        # (_MAPMSG_GATE) are authored by this loop regardless of the seed.
        cached = []
        last_map = None
        box_maps = set()     # fine map ids proven to carry an obtain box
        tick_n = 0
        empty_tries = 0      # consecutive empty scans since this map was entered
        async def tick():
            nonlocal cached, last_map, box_maps, tick_n, empty_tries
            if self.save_delta is None:
                return
            # Skip while in battle (no boxes there; keep frame pacing clean).
            # MUST be _in_battle() (BATTLE_ACTIVE_FLAG_SA, cleared on exit) --
            # BATTLE_ACTOR_OBJ_PTR_SA LATCHES the last battle_base forever and
            # permanently wedges any loop gating on it (hit live 2026-07-11:
            # this loop authored once, then went silent after the first fight).
            if await self._in_battle():
                return
            # FINE per-map id (0x13108, u32) -- the coarse LOADED_MAP_ID reads
            # the same value for whole map classes (all dungeons = 1, town ==
            # castle), so gating the rescan on it missed e.g. entering Cornelia
            # Castle from town and the Princess/Lute bank was never authored
            # (live 2026-07-11 fresh-seed playtest).
            mid = struct.unpack("<I", await self.psp.read(
                self.sa(D.FIELD_MAP_ID_SA), 4))[0]
            tick_n += 1
            fresh = []
            stale = False
            for entry in cached:
                if await self._mapmsg_author(*entry):
                    fresh.append(entry)
                else:
                    stale = True
            cached = fresh
            # Rescan triggers. The original "map change OR stale cache" pair had a
            # trap: a single scan that came back empty (transient miss, or the
            # bank not yet loaded) left `cached` empty, and with the map id
            # unchanged and nothing to go stale, the loop NEVER rescanned again --
            # so an obtain box could stay vanilla for the whole floor visit (the
            # 2026-07-17 Rosetta box: vanilla after ~3 min / ~90 ticks on-floor).
            # HARDENED so a box-map self-heals:
            #   * mid != last_map / stale         -- as before;
            #   * box-map with an EMPTY cache      -- the freeze case above; and
            #   * box-map periodic re-sweep        -- re-find a RELOCATED live copy
            #     the cached-address author can't see (scan authors every copy).
            # Boxless maps still cost one scan on entry, then nothing (never enter
            # box_maps), so frame pacing on the common case is unchanged.
            box_here = mid in box_maps
            # Nothing cached on a FIRST visit means the scan very likely beat the
            # map bundle into memory (Astos box, 2026-08-05). Retry a bounded few
            # times; the overworld is exempt because it is boxless and entered
            # constantly, and a scan is a 16 MB windowed read.
            retry_empty = (not cached
                           and mid != D.OVERWORLD_FIELD_MAP_ID
                           and empty_tries < self._MAPMSG_EMPTY_RETRIES)
            if (mid != last_map or stale or (box_here and not cached)
                    or (box_here and tick_n % self._MAPMSG_REVERIFY_TICKS == 0)
                    or retry_empty):
                banks = await self._mapmsg_scan()
                if banks is None:            # transient read failure --
                    last_map = None          # force a retry next tick,
                    return                   # keep the re-authored cache as-is.
                if mid != last_map:
                    empty_tries = 0          # fresh map -> fresh retry budget
                last_map = mid
                got = []
                for entry in banks:
                    if await self._mapmsg_author(*entry):
                        got.append(entry)
                cached = got
                if got:
                    box_maps.add(mid)
                    empty_tries = 0
                else:
                    empty_tries += 1
        await self._poll(2.0, "mapmsg_loop", tick)

    # ---------------- data tables: reconcile (bake) / apply (fallback) ----------
    async def _boot_patch_loop(self):
        """Keep the seed's data tables in force.

        NORMAL (bake_ok): every table was baked into the ISO at fixed ELF
        offsets, so this loop only RECONCILES -- one small fixed-address read
        per table per tick, rewriting only when a save-state load reverts one.
        No RAM scanning, no boot burst, no writes in steady state.

        FALLBACK (bake failed / unpatched ISO): same reconcile IS the applier --
        each fixed-address table reads back as vanilla and gets rewritten once,
        which is exactly the old runtime patching, minus the signature scans.

        Shop NAME BANKS are the exception: they live in relocating heap (from a
        compressed file, so they can't be baked) and still need a signature
        scan -- but through BANK_SCAN_WINDOWS (observed 16 MB home region
        first), only while unlocated, and only when shop AP slots are on."""
        if not self._patches:   # build ONCE: delist state lives in these objects
            dabble_baked = self.bake_ok and bool(
                (self.slot_data.get("on_disc") or {}).get("monk_thief_dabble_in_magic"))
            self._patches = BP.build_patches(enc_mult=self.enc_mult,
                                             xp_mult=self.xp_mult,
                                             gil_mult=self.gil_mult,
                                             slot_data=self.slot_data,
                                             dabble_baked=dabble_baked,
                                             boss_mult=self.boss_mult,
                                             monster_mult=self.monster_mult) \
                + list(self._extra_patches)   # scout-built shop name-bank patches
        # active/floating are RECOMPUTED each pass (see loop body): the dyn
        # name-slot patch is born a noop (patched == sentinel sig) and only
        # becomes active when _bonus_dyn_loop mutates it with the first
        # authored name -- a one-shot filter here would exclude it forever.
        active = [p for p in self._patches if not p.is_noop]
        if not active and not any(not p.fixed for p in self._patches):
            return
        floating = [p for p in active if not p.fixed]   # name banks only

        async def scan_floating(rescan_all=False):
            """Locate+write still-missing heap patches, narrow windows first.
            `rescan_all` re-sweeps LOCATED patches too: a bank can gain a new
            resident copy after boot (save load / town entry -- the weapons and
            armor shop name banks load a second copy the shop UI reads;
            locate_in merges, so known copies are kept)."""
            missing = [p for p in floating if rescan_all or not p.addrs]
            if not missing:
                return
            for start, size in BANK_SCAN_WINDOWS:
                try:
                    blob = await (self.psp_scan or self.psp).read_chunked(
                        start, size, breathe=SCAN_BREATHE_S)
                except Exception:
                    continue
                for p in missing:
                    try:
                        addrs = await p.locate_in(self.psp, blob, base=start)
                        if addrs:
                            logger.info(f"  patched {p.name} @ "
                                        f"{', '.join(hex(a) for a in addrs)}")
                    except Exception as e:
                        logger.info(f"  [boot_patch locate {p.name}] {e!r}")
                # after the first window a rescan-all is done: any extra copy
                # lives in the home region; later windows only chase patches
                # still located nowhere.
                missing = [p for p in floating if not p.addrs]
                if not missing:
                    return

        announced = False
        last_float_scan = 0.0
        loop = asyncio.get_event_loop()
        while not self.exit_event.is_set():
            active = [p for p in self._patches if not p.is_noop]
            floating = [p for p in active if not p.fixed]
            need_scan = False
            for p in active:
                try:
                    if await p.reconcile(self.psp) and not p.fixed:
                        need_scan = True
                except Exception as e:
                    logger.info(f"  [boot_patch reconcile {p.name}] {e!r}")
                # spread the per-table reads so the pass never bursts the emu
                await asyncio.sleep(RECONCILE_SPACING_S)
            force = self._float_rescan
            if force or (need_scan
                         and loop.time() - last_float_scan >= FLOAT_SCAN_MIN_S):
                self._float_rescan = False
                last_float_scan = loop.time()
                await scan_floating(rescan_all=force)
            if not announced and all(p.addrs for p in active):
                announced = True
                logger.info(f"  [boot_patch] all {len(active)} tables in force "
                            f"({'baked on-disc' if self.bake_ok else 'runtime fallback'})")
            await asyncio.sleep(BOOT_RECONCILE_S)

    async def _cameo_boss_loop(self):
        """Soften bosses met OUTSIDE their own boss room -- random-encounter CAMEOS
        (harder_dungeon/overworld_encounters) and boss MINIONS (boss_minions) -- to
        BP._cameo_mult(boss_difficulty), without touching their scripted fights.

        Neither is distinguishable at battle time (same formation record, same
        monster record as the real fight), but the MAP is: a boss is soft everywhere
        except its home dungeon, so its own fight keeps full strength while its
        cameos and its guest appearances in other bosses' fights are softened. See
        rando.boss_soft_plan for the gating and the two skipped bosses. While the
        party stands somewhere the soft version applies, that boss's monster_stats
        record holds the softened numbers; stepping off restores full strength.
        Battle init copies the table as it stands, well after the map settled, so
        there is no battle-start race and no per-battle write.

        Map id = D.BONUS_MAPID_ADDR (0x130F4), the CANONICAL encounter map id --
        the same id space zones_caves is indexed by, NOT FIELD_MAP_ID (which
        collides numerically: FIELD 0x4e = Chaos Shrine, cave 0x4e = Citadel of
        Trials). Overworld = LOADED_MAP_ID_SA == 0.

        The write goes through the monster_rewards DataPatch (retargeting `patched`,
        old bytes onto `stale`), exactly like set_encounter_rate: the boot-patch
        reconcile loop then MAINTAINS the current variant across a save-state load
        instead of seeing it as foreign bytes and refusing to heal it."""
        plan = None
        cur_key = object()      # sentinel: nothing applied yet
        while not self.exit_event.is_set():
            await asyncio.sleep(0.5)
            try:
                if not self.slot_data or self.save_delta is None:
                    continue
                if not self._patches:
                    continue    # wait for _boot_patch_loop: we RETARGET its patch,
                                # and writing first would leave softened bytes that
                                # its later full-strength build reads as foreign
                if plan is None:
                    plan = RANDO.boss_soft_plan(
                        dungeon_harder=bool(
                            self.slot_data.get("harder_dungeon_encounters")),
                        overworld_harder=bool(
                            self.slot_data.get("harder_encounters")),
                        minion_plan=self.slot_data.get("boss_minions_plan"))
                    everywhere, _homes, map_soft = plan
                    if not everywhere and not map_soft:
                        return          # nothing soft in this seed -> strict no-op
                    logger.info(
                        f"  [cameo bosses] softening to "
                        f"{BP._cameo_mult(self.boss_mult):.0%} power "
                        f"(boss difficulty {self.boss_mult:.0%}): "
                        f"{len(everywhere)} away from home, "
                        f"{len(map_soft)} map-gated")
                loaded = (await self.psp.read(self.sa(D.LOADED_MAP_ID_SA), 1))[0]
                if loaded == D.OVERWORLD_LOADED_MAP_ID:
                    key = RANDO.CAMEO_OVERWORLD
                else:
                    key = (await self.psp.read(self.sa(D.BONUS_MAPID_ADDR), 1))[0]
                if key == cur_key:
                    continue
                if await self._set_cameo_soft(RANDO.boss_soft_ids(plan, key)):
                    cur_key = key
            except Exception as e:
                logger.info(f"  [cameo_boss_loop] {e!r}")

    async def _write_monster_rewards(self, soft_ids):
        """Put the monster_stats block into the variant that scales rewards by the
        current xp/gil mults, bosses by boss_mult, and softens `soft_ids` to the
        cameo multiplier. Returns True once those bytes are live.

        Shared by the cameo-boss loop (which varies soft_ids per map) and
        set_monster_scaling (which varies the mults) -- both write the SAME table,
        so they have to go through one builder or each would drop the other's
        contribution."""
        blk = BP.monster_stats_block(self.xp_mult, self.gil_mult, self.boss_mult,
                                     soft_ids, monster_mult=self.monster_mult)
        addr = BP.table_ram_addr("monster_rewards")
        if addr is None:
            return False
        p = next((q for q in (self._patches or [])
                  if q.name == "monster_rewards"), None)
        if p is not None:
            # Same contract as set_encounter_rate: the PREVIOUS target must go on
            # `stale` or reconcile treats a save state holding it as foreign bytes
            # at a fixed address and refuses to re-patch (leaving the wrong variant).
            if (p.patched != blk and p.patched != p.vanilla_sig
                    and p.patched not in p.stale):
                p.stale.append(p.patched)
            p.patched = bytes(blk)
            p.is_noop = p.vanilla_sig == p.patched
            if not p.addrs:
                p.addrs = [addr]
            if p.is_noop:
                # Vanilla everywhere: nothing to write or maintain for the stat
                # block -- but the magic_power tables still have to be published.
                # An all-sentinel table IS how a Boost-tab return to 100% reverts
                # the caves to vanilla, so skipping this would strand the last
                # scaled tables live after the player dialled power back down.
                if self.psp is not None:
                    await self._write_magic_power_tables(soft_ids)
                return True
        if self.psp is None:
            # No game attached. The patch above is already retargeted, so the
            # reconcile loop applies the new block as soon as one comes up; only
            # the immediate write is unavailable.
            return False
        await self.psp.write(addr, blk)
        await self._write_magic_power_tables(soft_ids)
        return True

    async def _set_cameo_soft(self, soft_ids):
        """Put the monster_stats block into the variant with `soft_ids` softened.
        Returns True once the new bytes are live (so the caller only latches the
        map on success and retries next tick otherwise)."""
        if self.psp is None:
            return False
        if not await self._write_monster_rewards(soft_ids):
            return False
        self._cameo_soft = tuple(soft_ids)
        # The same soft set is re-written on every map load; only announce it when
        # it actually changes (boss entered/left a soft zone) instead of per-map.
        key = frozenset(soft_ids)
        if soft_ids and key != self._last_soft_log:
            logger.info(f"  [cameo bosses] softened "
                        f"{', '.join(f'{i:#04x}' for i in sorted(soft_ids))}")
        self._last_soft_log = key
        return True

    async def set_monster_scaling(self, xp_mult=None, gil_mult=None,
                                  boss_mult=None, monster_mult=None):
        """Live-apply new xp / gil / monster-power / boss-difficulty multipliers.
        All four live in the one monster_rewards block, so they are set together;
        None keeps the current value. Rebuilds with the soft set the cameo loop
        last applied so a change made while standing in a softened zone doesn't
        restore those bosses to full strength. Returns True iff the new bytes are
        live."""
        if xp_mult is not None:
            self.xp_mult = float(xp_mult)
        if gil_mult is not None:
            self.gil_mult = float(gil_mult)
        if boss_mult is not None:
            self.boss_mult = float(boss_mult)
        if monster_mult is not None:
            self.monster_mult = float(monster_mult)
        try:
            ok = await self._write_monster_rewards(self._cameo_soft)
        except Exception as e:
            logger.info(f"  [scaling] live write failed: {e!r}")
            return False
        logger.info(f"  [scaling] xp x{self.xp_mult}, gil x{self.gil_mult}, "
                    f"monster x{self.monster_mult}, boss x{self.boss_mult} "
                    f"({'live' if ok else 'queued for next boot'})")
        return ok

    async def set_encounter_rate(self, mult):
        """Live-apply a new encounter multiplier. The encounter_rate table is a
        96xu16 DATA block at a fixed ELF home (0x8945654) -- pure data, no JIT
        wall -- so we can rewrite it in RAM and the game reads the new value on
        the next step-danger roll. We (a) retarget the boot-patch DataPatch so
        the reconcile loop maintains the new value across save/load, then (b)
        write it now and read it back. Returns True iff the read-back matches."""
        self.enc_mult = mult
        new_bytes = BP.scale_encounter_block(mult)
        addr = BP.table_ram_addr("encounter_rate")
        if addr is None:
            return False
        # Keep the reconcile loop in sync. reconcile() only heals bytes it knows
        # (vanilla or `stale`); the PREVIOUS target must go on `stale` or it would
        # be treated as foreign at the fixed addr and left alone. Done BEFORE the
        # psp check so a Boost-tab press with no game attached still takes effect
        # on the next boot instead of being silently dropped.
        for p in (self._patches or []):
            if p.name == "encounter_rate":
                if (p.patched != new_bytes and p.patched != p.vanilla_sig
                        and p.patched not in p.stale):
                    p.stale.append(p.patched)
                p.patched = bytes(new_bytes)
                p.is_noop = p.vanilla_sig == p.patched
                if not p.addrs:
                    p.addrs = [addr]
                break
        if self.psp is None:
            return False
        try:
            await self.psp.write(addr, new_bytes)
            back = await self.psp.read(addr, len(new_bytes))
        except Exception as e:
            logger.info(f"  [encounters] live write failed: {e!r}")
            return False
        ok = back == new_bytes
        logger.info(f"  [encounters] set x{mult} @ {addr:#x} "
                    f"({'verified' if ok else 'READ-BACK MISMATCH'})")
        return ok

    # ---------------- movement (auto-dash) ----------------
    async def _movement_loop(self):
        """Auto-dash: set bit0 of the persistent Config "Dash" byte
        (D.DASH_CONFIG_ADDR_SA) so the party auto-runs without holding the dash button.
        This drives native engine behavior (the Config menu shows Dash=On too).
        Written ONCE at new game (see below), never re-asserted. Fixed save-block address,
        stable across boots -- NOT the dynamic per-actor runtime flag (which
        corrupted RAM); see ff1_data note.

        ONE-SHOT at NEW GAME, same freshness gate as _party_loop (all 4 party
        records Lv1/EXP 0). Dash is a PLAYER PREFERENCE, not a seed rule: we set
        a good default alongside the party jobs, then never touch it again, so a
        player who turns Dash off in the Config menu stays off. The old loop
        re-asserted the bit every 3s and fought them.

        Also sets CURSOR (bit1) and MESSAGE SPEED (bits6-7) from yaml -- all
        three live in the SAME config byte, same one-shot, same
        hands-off-afterwards rule. They are Config-menu preferences, so the seed
        only picks the STARTING value.

        Runs even with auto_dash off: dash is then left at its vanilla value
        while the message-speed and cursor defaults still land."""
        logger.info(f"  [config] new-game defaults loop start "
                    f"(auto_dash={self.auto_dash}, "
                    f"message_speed={self.message_speed}, "
                    f"cursor_mode={self.cursor_mode})")
        while not self.exit_event.is_set():
            try:
                if self.save_delta is None:
                    await asyncio.sleep(0.5)
                    continue
                state = await self._party_state()
                if state == "underway":
                    # "underway" (exp>0) is also the NG+ carried snapshot through the
                    # whole character-creation screen; wait for commit (Chaos bit clears)
                    # instead of bailing, so the dash/message-speed default still lands.
                    if await self._carried_save_snapshot():
                        await asyncio.sleep(0.5)
                        continue
                    logger.info("  [movement] game underway -> leaving Dash "
                                "to the player")
                    return
                if state != "fresh":
                    await asyncio.sleep(0.5)
                    continue
                # Dash (bit0) and Message Speed (bits6-7) share one config byte, so
                # one read-modify-write sets both. Mirror byte written to match, as
                # a real menu change writes both copies (see ff1_data).
                # Dash (bit0), Cursor (bit1) and Message Speed (bits6-7) are all
                # in ONE byte, so a single read-modify-write sets all three.
                speed = max(0, min(3, int(self.message_speed)))
                addr = self.sa(D.DASH_CONFIG_ADDR_SA)
                cur = (await self.psp.read(addr, 1))[0]
                new = cur
                if self.auto_dash:
                    new |= D.DASH_CONFIG_MASK
                new &= ~D.CURSOR_CONFIG_MASK
                if self.cursor_mode:
                    new |= D.CURSOR_CONFIG_MASK
                new = ((new & ~D.MSG_SPEED_MASK)
                       | (speed << D.MSG_SPEED_SHIFT)) & 0xFF
                if new != cur:
                    await self.psp.write(addr, bytes([new]))
                await self.psp.write(self.sa(D.MSG_SPEED_MIRROR_ADDR_SA),
                                     bytes([1 << speed]))
                logger.info(f"  [config] new-game defaults: dash "
                            f"{'on' if self.auto_dash else 'vanilla'}, cursor "
                            f"{'memory' if self.cursor_mode else 'default'}, "
                            f"message speed {speed} ({addr:#x}: {cur:#04x} -> "
                            f"{new:#04x}) -- all yours to change from here on")
                return
            except Exception as e:
                logger.info(f"  [movement] {e!r}")
                await asyncio.sleep(0.5)

    # ---------------- always-set save flags (bridge, cutscene skips) ----------------
    async def _flags_loop(self):
        """Keep every D.ALWAYS_SET_FLAGS bit set: Cornelia bridge built (flag-gated
        tilemap gen, redrawn on fresh overworld load -- see [[bridge-map-state]]) and
        the one-shot intro-cutscene "watched" bits so players never see those scenes.
        All live in the save block, so the bits survive save/load; one span read +
        per-byte OR-writes per tick keeps WS traffic at a single RPC when idle."""
        lo = min(a for a, _, _ in D.ALWAYS_SET_FLAGS)
        hi = max(a for a, _, _ in D.ALWAYS_SET_FLAGS)
        announced = set()

        async def tick():
            if self.save_delta is None:
                return
            span = await self.psp.read(self.sa(lo), hi - lo + 1)
            for addr, mask, label in D.ALWAYS_SET_FLAGS:
                cur = span[addr - lo]
                if (cur & mask) != mask:
                    await self.psp.write(self.sa(addr), bytes([cur | mask]))
                    logger.info(f"  [flags] {label} "
                                f"({self.sa(addr):#x}: {cur:#04x} -> {cur | mask:#04x})")
                    announced.add(label)
                elif label not in announced:
                    logger.info(f"  [flags] {label}: already set")
                    announced.add(label)
            # Chaos-defeated -> auto-report the AP goal. 0x11520 b6 is set on
            # Chaos's death (live-verified 2026-07-07, persists past the cutscene);
            # replaces the manual-only /goal. One 1-byte read/tick until goal met,
            # then skipped (finished_game short-circuits).
            if not self.finished_game:
                # Re-arm the edge guard whenever the save block relocates (genuine
                # save (re)load): a fresh delta means we have NOT yet observed this
                # save's Chaos bit clear, so a set-on-first-read is carried-in.
                if self._chaos_guard_delta != self.save_delta:
                    self._chaos_guard_delta = self.save_delta
                    self._chaos_ever_clear = False
                    self._chaos_carryin_logged = False
                gb = (await self.psp.read(self.sa(D.CHAOS_DEFEATED_ADDR), 1))[0]
                if not (gb & D.CHAOS_DEFEATED_MASK):
                    self._chaos_ever_clear = True   # clear now -> a later set is a real kill
                elif await self._party_records_fresh():
                    # RAW record check on purpose: _party_state is gated on the
                    # new-game verdict, which stays UNDECIDED for exactly as long
                    # as the carried Chaos snapshot is on screen -- i.e. for the
                    # whole window this re-arm exists to cover.
                    # Chaos bit set on a FRESH (Lv1/EXP0, char-creation) party = a carried
                    # completed snapshot, NEVER a real kill (Chaos falls only to a leveled
                    # party). This also catches a MID-SESSION reload of the clear slot at
                    # the SAME save_delta, which would otherwise inherit _chaos_ever_clear
                    # from the prior committed game and false-report the goal + auto-release
                    # the whole seed (live 2026-07-23). Re-arm so the new game starts fresh.
                    self._chaos_ever_clear = False
                    if not self._chaos_carryin_logged:
                        self._chaos_carryin_logged = True
                        logger.info("  [goal] Chaos flag set on a fresh party "
                                    "(carried-in / NG+ load) -- NOT auto-reporting; "
                                    "use /goal if this is a genuine victory")
                elif self._chaos_ever_clear:
                    # Final guard against a MID-SESSION reload of a beaten/clear save at
                    # the SAME save_delta (die -> load NG+ clear slot): the reload briefly
                    # holds the carried beaten party (reads "underway", so the fresh check
                    # above misses it) with Chaos set, and _chaos_ever_clear is still True
                    # from the prior committed game -> false goal + auto-release (live
                    # 2026-07-23). Discriminator: a carried save's grant counter is junk,
                    # AHEAD of what the server actually delivered this game (348 > 301);
                    # a continuously-progressed game's counter never exceeds items_received.
                    counter = await self._read_counter_stable()
                    if counter is None:
                        return          # unstable frame -> re-check next tick (2s)
                    if counter > len(self.items_received):
                        self._chaos_ever_clear = False        # reloaded save -> re-arm
                        if not self._chaos_carryin_logged:
                            self._chaos_carryin_logged = True
                            logger.info("  [goal] Chaos set but the grant counter is "
                                        "carried/ahead (reloaded beaten save?) -- NOT "
                                        "auto-reporting; use /goal for a real victory")
                    else:
                        logger.info("  [goal] Chaos defeated flag detected "
                                    f"({self.sa(D.CHAOS_DEFEATED_ADDR):#x} b6) -> reporting goal")
                        await self.send_goal()
                elif not self._chaos_carryin_logged:
                    # Set on the FIRST read at this delta with a non-fresh party ->
                    # reconnect to an already-won save. Do NOT auto-goal; use /goal.
                    self._chaos_carryin_logged = True
                    logger.info("  [goal] Chaos flag already set on first read "
                                "(carried-in from a beaten save?) -- NOT auto-reporting; "
                                "use /goal if this is a genuine victory")

        await self._poll(2.0, "flags_loop", tick)

    # ---------------- auto-hint town shop AP offers on first entry ----------------
    async def _shop_hint_loop(self):
        """First time the party steps into a town (its name gets added to the
        overworld map), HINT every AP shop offer that town sells so the player
        learns what its shops hold without buying blind.

        Driven by the durable per-town map-reveal bits in D.TOWN_MAP_FLAGS (all 7
        AP-shop towns mapped live 2026-07-08). Also feeds the Shops GUI tab: each
        town's reveal bit unlocks its Shops sub-tab (self._towns_visited). A town is
        hinted once per session; the server dedups repeat hints, so re-hinting after
        a reconnect is harmless. Uses create_as_hint=2 (records the hint without
        broadcasting to all players' chat -- change to 1 to broadcast)."""
        entries = [(c, a, m) for (c, a, m) in D.TOWN_MAP_FLAGS if a is not None]
        if not entries:
            logger.info("  [shop-hint] no town map-reveal flags mapped -- loop idle "
                        "(capture with tools/capture_town_flags.py)")
            return
        lo = min(a for _c, a, _m in entries)
        hi = max(a for _c, a, _m in entries)
        hinted = set()          # city ids already hinted this session

        def shop_lids_for_city(city):
            out = []
            for (s, _c, _g, prices) in self.shop_slots:
                if D.SHOP_CITY[s] == city:
                    out += [ID.shop_loc_id(s, k) for k in range(len(prices))]
            return out

        async def tick():
            if self.save_delta is None or not self._server_up():
                return
            # NG+ char-creation: the town map-reveal bits are carried from the beaten
            # save (all 7 towns read "visited"), so hinting now floods every shop offer
            # before the game exists. Defer until the new game commits (Chaos bit clears);
            # the fresh save has all town bits clear -> hints fire on real town entry.
            if await self._carried_save_snapshot():
                return
            span = await self.psp.read(self.sa(lo), hi - lo + 1)
            # Record every town whose reveal bit is now set -> the Shops tab keys its
            # per-town unlock off this (same "first visit" signal that fires hints).
            newly = {city for (city, addr, mask) in entries
                     if (span[addr - lo] & mask) and city not in self._towns_visited}
            # Same 2s tick refreshes the party view the Shops tab shades its stock
            # with (class change and magic-level ups both move it). ~370 bytes;
            # repaint only when it actually changed.
            try:
                view = await self._read_party_view()
            except Exception:
                view = None
            changed = view is not None and view != self._party_view
            if changed:
                self._party_view = view
            if newly or changed:
                self._towns_visited |= newly
                self.refresh_shops()
            # loop-invariant: the server-known location set (missing+checked)
            known = set(self.missing_locations) | set(self.checked_locations)
            for (city, addr, mask) in entries:
                if city in hinted:
                    continue
                if not (span[addr - lo] & mask):
                    continue                       # town not entered yet
                lids = shop_lids_for_city(city)
                lids = [l for l in lids if l in known]  # only unchecked offers this slot owns
                if not lids:
                    hinted.add(city)               # nothing (left) to hint here
                    continue
                try:
                    await self.send_msgs([{"cmd": "LocationScouts",
                                           "locations": lids, "create_as_hint": 2}])
                    hinted.add(city)
                    logger.info(f"  [shop-hint] entered {D.CITY_NAME.get(city, city)} "
                                f"-> hinted {len(lids)} shop offer(s)")
                except Exception as e:
                    logger.info(f"  [shop-hint] hint send failed ({e!r}); will retry")

        await self._poll(2.0, "shop_hint_loop", tick)

    # ---------------- spell-tome names/descriptions (B2) ----------------
    # ---------------- open-progression runtime map edits + canal shallows ----------------
    async def _openworld_loop(self):
        """Overworld (MAP_00) maintenance. Two INDEPENDENT yaml toggles carve foot
        trails + canoe rivers into the decompressed grid: early_open_progression ->
        EARLY_GRID_EDITS, extended_open_progression -> EXTENDED_GRID_EDITS (unioned
        per slot_data). The canal SHALLOWS (CANAL_ATT = walk+sail attrs) are applied
        ALWAYS, regardless of the toggles -- but we NO LONGER force the canal-open
        bit, so they only take effect once the player blows the canal normally
        (Nitro -> Nerrick). See [[open-progression-rework]] / [[canal-shallows-plan]].

        The grid/ATT arena is re-decompressed on (some) overworld loads and relocates
        between boots: cheap canary check at the cached arena addresses; on mismatch
        rewrite all edits; on anchor loss re-scan RAM. Alignment is verified against
        the vanilla anchor bytes before ANY write (misaligned bulk writes corrupt
        the map).

        MAP-GATED: the arena only exists ON THE OVERWORLD (map id 9). Scanning from
        a town/dungeon can never succeed, and the old always-scan version swept up to
        24 MB of RAM every 1.5 s the whole time you walked around a town -- that was
        the in-town stutter. Off the overworld this loop costs two 1-byte reads per
        tick. Scans are rate-limited (narrow window >=3 s apart, full-RAM >=30 s)."""
        from . import openworld_data as OW
        while not self.slot_data and not self.exit_event.is_set():
            await asyncio.sleep(1.0)
        early = bool(self.slot_data.get("early_open_progression"))
        extended = bool(self.slot_data.get("extended_open_progression"))
        docks = bool(self.slot_data.get("northern_docks"))
        grid_edits = {}
        if early:
            grid_edits.update(getattr(OW, "EARLY_GRID_EDITS", {}))
        if extended:
            grid_edits.update(getattr(OW, "EXTENDED_GRID_EDITS", {}))
        if docks:
            grid_edits.update(getattr(OW, "NORTHERN_DOCKS_GRID_EDITS", {}))
        # v242 bakes exactly these grid cells into MAP_00_AMD.BIN. On a verified
        # bake the loop would rewrite values that are already correct -- and pay
        # for it with the 8 MB anchor rescan (measured 1.0-2.1 s per pass, the
        # in-game stutter in the 2026-08-08 report). Drop them and keep only
        # what the bake does NOT cover: the canal shallows, the canal-open-gated
        # bridge art and the river terrain-CLASS cells (map_bake writes the tile
        # grid only). Pre-242/unbaked seeds keep the full repair path.
        ow_baked = bool(getattr(self, "bake_ok", False)) and bool(
            ((getattr(self, "_bake", None) or {}).get("features")
             or {}).get("_ow_map"))
        if ow_baked and grid_edits:
            logger.info(f"  [openworld] {len(grid_edits)} grid edits are on "
                        f"disc (baked) -- skipping the runtime grid rescan")
            grid_edits = {}
        # Terrain-CLASS edits for the carved rivers. Overworld encounters ignore the
        # tile grid entirely: the selector reads the STATIC class map
        # (OW.TERRAINMAP_ADDR) and routes class -> (rate_zone, terrain_type). A
        # carved river keeps the class of what it replaced (ocean -> sea fights,
        # marsh -> rate_zone 6 = rate bytes 1..3 = effectively never), which is why
        # the Melmond run had no encounters. Writing OW.RIVER_CLASS on the canoe
        # cells puts them on the zoned LAND table like every vanilla river.
        class_edits = {}
        if early:
            class_edits.update(getattr(OW, "EARLY_RIVER_CLASS_EDITS", {}))
        if extended:
            class_edits.update(getattr(OW, "EXTENDED_RIVER_CLASS_EDITS", {}))
        tm_addr = getattr(OW, "TERRAINMAP_ADDR", None)
        tm_off = getattr(OW, "TERRAINMAP_DATA_OFF", 4)
        tm_w = getattr(OW, "TERRAINMAP_W", 255)
        tm_h = getattr(OW, "TERRAINMAP_H", 255)
        if tm_addr is None:
            class_edits = {}
        class_canary = next(iter(sorted(class_edits))) if class_edits else None
        canal_att = getattr(OW, "CANAL_ATT", {})
        # Canal BRIDGE art (3 cells on column x=94): drawn INSTEAD of the shallows
        # look, but only once the canal is actually blown -- the bridge tiles are
        # walkable, so writing them before Nerrick's event would hand the player a
        # free crossing. Gate = the canal-open tilemap bit (canal-flag-bit memory).
        canal_bridge = getattr(OW, "CANAL_BRIDGE_GRID_EDITS", {})
        # map_bake.bake_canal_ford writes the ford's attr + art + river anim on
        # disc; the CANAL_ATT poke is documented there as a belt-and-suspenders
        # fallback. On a verified bake it is pure cost -- and it is the LAST
        # thing keeping the arena scan alive once the grid edits are baked, so
        # dropping it is what actually retires the 8 MB rescan.
        if ow_baked and canal_att:
            canal_att = {}
        canal_open_addr_sa = getattr(OW, "CANAL_OPEN_ADDR_SA", D.CANAL_OPEN_ADDR_SA)
        canal_open_bit = getattr(OW, "CANAL_OPEN_BIT", 0x08)
        # Canal shallows are always-on; the loop still runs with all toggles off to
        # maintain them. Bail only if there is genuinely nothing to write.
        if not grid_edits and not canal_att and not canal_bridge and not class_edits:
            return
        logger.info(f"  [openworld] early={early} extended={extended} docks={docks} "
                    f"({len(grid_edits)} grid edits, {len(class_edits)} river-class "
                    f"cells; canal shallows always-on)")
        grid_base = None
        grid_bases = None       # all live anchor hits (rescan fills; see below)
        att_base = None         # primary (canary) ATT copy
        att_bases = None        # all live ATT_PREFIX hits (same freed-copy hazard)
        # Live edit set = the toggle-selected edits, plus the canal bridge once the
        # canal is open. `cur` is rebound in tick() when the canal bit flips; apply()
        # and the canary check both read it so the bridge is maintained like any
        # other edit (fresh decompress reverts it -> drift -> reapply).
        cur = {"edits": dict(grid_edits), "canary": None, "val": None}

        def set_edits(edits, canary=None):
            # Prefer an explicit canary: the canal cells are REPAINTED by the game
            # itself when Nerrick blows the canal, so once the bridge is live a
            # bridge cell is the only canary that can see that repaint.
            cur["edits"] = edits
            if canary is None:
                canary = min(edits) if edits else None
            cur["canary"] = canary
            cur["val"] = edits[canary] if canary is not None else None

        set_edits(dict(grid_edits))
        canal_state = {"open": False}
        att_canary = min(canal_att) if canal_att else None
        loop = asyncio.get_event_loop()
        # Miss-backoff: consecutive failed rescans stretch the narrow-window
        # interval 3s -> 30s cap (any hit resets). The arena being absent for
        # a while (menu screens, transitions) used to mean a multi-MB sweep
        # every 3 s for the whole stretch.
        last_scan = {"window": 0.0, "full": 0.0, "wait": 3.0}
        # Relocation guard: the overworld arena is re-decompressed to a FRESH
        # (often different) address every time the player re-enters the overworld
        # -- from a town, dungeon, or battle. The canary/anchor checks can't see
        # this: they read the CACHED grid_base, which now points at the freed old
        # buffer whose bytes still hold our edited canary value (drift=False), so
        # the loop silently maintains a dead buffer while the live arena stays
        # vanilla -> the intermittent "river not connected". Force a fresh rescan
        # on every overworld re-entry so grid_base tracks the live arena.
        map_state = {"away": True, "verify": 0.0}
        # apply() runs on every re-anchor/re-decompress (every few seconds on the
        # overworld) and the base set is almost always identical -- remember what
        # we last announced and only log when the arena actually relocates.
        applied_log = {"bases": None}

        # Two DISTINCT address spaces -- never mix their strides. cell_addr:
        # the RELOCATING tile arena, 2 bytes/cell, OW.GRID_STRIDE bytes/row.
        # class_addr (below): the FIXED terrain-class map, 1 byte/cell, tm_w
        # bytes/row, tm_off skipping its 4-byte W/H header. A stride mixup
        # corrupts the overworld.
        def cell_addr(base, xy):
            x, y = xy
            return base + y * OW.GRID_STRIDE + x * 2

        async def rescan(narrow_only=False):
            # Arena lives in the high heap: observed window first; full user
            # RAM only as a rare, heavily rate-limited fallback.
            # Collect ALL anchor hits, not just the first: a freed old arena
            # keeps its (vanilla) anchor bytes, so find()-first can lock onto a
            # dead copy while the live arena sits later in the window. apply()
            # writes every verified copy -- writing a freed buffer is harmless,
            # and the live one is guaranteed covered.
            # narrow_only: skip the 24MB fallback sweep AND leave the full-sweep
            # timer untouched (the periodic re-anchor uses this so it never starves
            # the excursion path's genuine 30s fallback).
            nonlocal grid_base, grid_bases, att_base, att_bases
            now = loop.time()
            if now - last_scan["window"] < last_scan["wait"]:
                return
            last_scan["window"] = now
            windows = [OW_SCAN_WINDOWS[0]]
            if not narrow_only and now - last_scan["full"] >= 30.0:
                last_scan["full"] = now
                windows = list(OW_SCAN_WINDOWS)
            for start, size in windows:
                t0 = loop.time()
                blob = await (self.psp_scan or self.psp).read_chunked(
                    start, size, breathe=SCAN_BREATHE_S)
                dt = loop.time() - t0
                if dt > 1.0:
                    logger.info(f"  [openworld] rescan {size >> 20}MB "
                                f"@{start:#x} took {dt:.1f}s")
                grid_bases = []
                i = blob.find(OW.ANCHOR)
                while i >= 0:
                    grid_bases.append(start + i - OW.ANCHOR_OFF)
                    i = blob.find(OW.ANCHOR, i + 1)
                grid_base = grid_bases[0] if grid_bases else None
                att_bases = []
                j = blob.find(OW.ATT_PREFIX)
                while j >= 0:
                    att_bases.append(start + j)
                    j = blob.find(OW.ATT_PREFIX, j + 1)
                att_base = att_bases[0] if att_bases else None
                if grid_base is not None:
                    last_scan["wait"] = 3.0
                    return
            last_scan["wait"] = min(last_scan["wait"] * 2, 30.0)

        def class_addr(xy):
            x, y = xy
            return tm_addr + tm_off + y * tm_w + x

        async def apply_classes():
            """River terrain-class cells -> OW.RIVER_CLASS. The class map is at a
            FIXED address (it does not relocate like the tile arena), but the game
            rebuilds it on overworld load, so this runs from apply() like any other
            edit. Header check first: the u16 W/H must read 255x255 before we poke
            anything, so a write can never land in a churning/unloaded buffer."""
            if not class_edits:
                return
            hdr = await self.psp.read(tm_addr, 4)
            if int.from_bytes(hdr[0:2], "little") != tm_w or \
                    int.from_bytes(hdr[2:4], "little") != tm_h:
                return
            for xy, cls in class_edits.items():
                await self.psp.write(class_addr(xy), bytes([cls]))

        async def apply():
            # Write every anchor-verified arena copy (live + any freed stale
            # ones -- stale writes are harmless, and we can't tell them apart).
            await apply_classes()
            wrote = 0
            repaired = 0
            for base in (grid_bases or [grid_base]):
                # verify alignment first — never bulk-write on a stale base
                chk = await self.psp.read(base + OW.ANCHOR_OFF, len(OW.ANCHOR))
                if chk != OW.ANCHOR:
                    continue
                # PER-BASE state probe. The cached-canary drift check upstream can
                # only speak for ONE base: a freed copy still holding our edits
                # reads clean forever while the LIVE arena renders vanilla (the
                # "restart put the docks back" report, 2026-08-08). Reading the
                # canary in EVERY anchor hit is the only signal that says which
                # copies were actually vanilla when we got here -- purely
                # diagnostic, since we write them all either way.
                if cur["canary"] is not None:
                    pv = await self.psp.read(cell_addr(base, cur["canary"]), 2)
                    if int.from_bytes(pv, "little") != cur["val"]:
                        repaired += 1
                for xy, v in cur["edits"].items():
                    await self.psp.write(cell_addr(base, xy),
                                         v.to_bytes(2, "little"))
                wrote += 1
            if repaired:
                # Far visual-only edits (the northern docks) do not repaint until
                # the chunk scrolls out and back, so a repair here can be silent
                # in game -- log it or the next report is unfalsifiable again.
                logger.info(f"  [openworld] re-applied edits to {repaired} arena "
                            f"cop{'y' if repaired == 1 else 'ies'} that read vanilla")
            if not wrote:
                return False
            for abase in (att_bases or ([att_base] if att_base is not None else [])):
                # verify the ATT prefix before poking -- a freed/reused copy is
                # skipped, exactly like the grid anchor check above.
                achk = await self.psp.read(abase, len(OW.ATT_PREFIX))
                if achk != OW.ATT_PREFIX:
                    continue
                for tid, attr in canal_att.items():
                    await self.psp.write(abase + tid * 2,
                                         attr.to_bytes(2, "little"))
            bases = tuple(grid_bases or [grid_base])
            if bases != applied_log["bases"]:
                applied_log["bases"] = bases
                logger.info(f"  [openworld] map edits applied @ "
                            f"{', '.join(hex(b) for b in bases)}")
            return True

        async def tick():
            nonlocal grid_base, grid_bases, att_base, att_bases
            if self.save_delta is None:
                return
            # NOTE: the canal-open bit is NO LONGER forced -- the player blows the
            # canal normally; we only make it walk+sail (CANAL_ATT) once it opens.
            # Map edits: overworld only (arena not resident elsewhere). Gate on the
            # TRUE map id (LOADED_MAP_ID_SA==0); the old gate used 0x08D13121 which is
            # a scroll coord (6..14) so it only matched at coord==9 -- edits went
            # unmaintained across most of the world map.
            mid = (await self.psp.read(self.sa(D.LOADED_MAP_ID_SA), 1))[0]
            if mid != D.OVERWORLD_LOADED_MAP_ID:
                map_state["away"] = True
                return
            # In battle the map id still reads 9 (battle entered FROM the overworld)
            # but the arena is freed/churning -- every canary read missed and
            # triggered a rescan RIGHT AT battle start, the heaviest lag report. No
            # walking happens in battle; skip. Gate on the real flag: the
            # battle_base pointer LATCHES, so a range check here would return early
            # forever after the first fight and never re-apply map edits again.
            if await self._in_battle():
                map_state["away"] = True
                return
            # Canal-open bit: while it is clear the canal is not dug and the bridge
            # must NOT be drawn (its tiles are walkable = a free crossing). On the
            # rising edge, fold the bridge cells into the live edit set and force an
            # immediate apply so the bridge appears the moment the canal opens.
            if canal_bridge:
                # sa() wrap is MANDATORY: the save block shifts per session, so a
                # raw read of the canonical address returns 0x00 forever.
                is_open = bool((await self.psp.read(self.sa(canal_open_addr_sa), 1))[0]
                               & canal_open_bit)
                if is_open != canal_state["open"]:
                    canal_state["open"] = is_open
                    edits = dict(grid_edits)
                    if is_open:
                        edits.update(canal_bridge)
                        set_edits(edits, min(canal_bridge))
                    else:
                        set_edits(edits)
                    logger.info(f"  [openworld] canal bridge "
                                f"{'drawn' if is_open else 'withheld (canal closed)'}")
                    if grid_base is not None and not map_state["away"]:
                        await apply()
            # Nothing left that lives in the relocating arena (baked ISO, canal
            # not yet blown) -> never scan for it. The rescan is unconditional
            # below, so without this bail a fully-baked seed still paid the
            # 8 MB sweep every re-entry to write cells that were already right.
            # The river terrain-CLASS map is at a FIXED address and is NOT
            # baked, so it still gets maintained here.
            if not cur["edits"] and not canal_att:
                grid_base = None
                att_base = att_bases = None
                await apply_classes()
                return
            # Back on the overworld after an excursion (town/dungeon/battle): the
            # arena was re-decompressed elsewhere. Drop the stale base so the
            # located branch below can't validate a dead buffer -- rescan finds
            # the live arena.
            if map_state["away"]:
                map_state["away"] = False
                grid_base = None
                att_base = None
                att_bases = None
                # Force the re-entry rescan to run NOW: a recent scan from just
                # before the excursion could otherwise rate-limit it for up to
                # `wait` seconds, leaving far visual-only edits (e.g. the northern
                # docks) unwritten while the player sails into view -> the coast
                # draws vanilla and won't redraw until it scrolls out and back.
                last_scan["window"] = 0.0
                # ...and drop the miss-backoff. After a death (or any load), the
                # map id reads 9 before the arena is decompressed, so the first
                # rescans legitimately miss and stretch `wait` toward its 30s cap
                # -- the player then stands on the overworld for up to half a
                # minute with edits unapplied (foot trails missing) before a tick
                # finally lands. Re-entry is exactly when we want the fast cadence.
                last_scan["wait"] = 3.0
            if grid_base is None:
                await rescan()
                if grid_base is None:
                    return                      # arena not decompressed yet
                await apply()
                return
            # arena located: revalidate via canary(s). Check the grid canary (if any
            # grid edits) and the canal-shallows attr canary; any drift -> reapply.
            drifted = False
            if cur["canary"] is not None:
                cv = await self.psp.read(cell_addr(grid_base, cur["canary"]), 2)
                if int.from_bytes(cv, "little") != cur["val"]:
                    drifted = True
            if not drifted and class_canary is not None:
                # The class map is rebuilt on overworld load without moving, so the
                # grid canary can't see it revert -- check a river cell directly.
                cc = await self.psp.read(class_addr(class_canary), 1)
                if cc[0] != class_edits[class_canary]:
                    drifted = True
            if not drifted and att_base is not None and att_canary is not None:
                av = await self.psp.read(att_base + att_canary * 2, 2)
                if int.from_bytes(av, "little") != canal_att[att_canary]:
                    drifted = True
            if drifted:
                if not await apply():           # reverted (fresh decompress) or moved
                    grid_base = None
                    await rescan()
                    if grid_base is not None:
                        await apply()
                return
            # Periodic re-anchor: the arena can be re-decompressed to a FRESH
            # address WITHOUT leaving the overworld (the "away" guard only trips on
            # a town/dungeon/battle excursion). The cached canary then reads the
            # FREED old buffer -- which still holds our edited value, so drift=False
            # forever while the live arena renders VANILLA. Symptom: a foot pass you
            # just crossed turns back into mountains behind you. So on a slow timer,
            # force a fresh full rescan and re-apply to EVERY anchor hit (live +
            # stale); if the live base has moved, adopt it.
            now = loop.time()
            if now - map_state["verify"] >= OW_REANCHOR_S:
                map_state["verify"] = now
                prev = grid_base
                grid_base = None
                last_scan["window"] = 0.0        # bypass the miss-backoff gate
                await rescan(narrow_only=True)   # cheap: no 24MB sweep, timer intact
                if grid_base is None:
                    grid_base = prev             # keep the cached base; try later
                    return
                # ALWAYS re-apply, not only when the base set changed. An arena
                # re-decompressed IN PLACE keeps the same address, so the old
                # changed-bases condition saw nothing while the live copy had
                # reverted to vanilla -- the player then had to restart the game
                # to get the northern docks back (2026-08-08). ~43 cells x N bases
                # every OW_REANCHOR_S over the direct bridge is negligible, and
                # apply() re-verifies each base's anchor before writing anything.
                await apply()

        await self._poll(1.5, "openworld_loop", tick)

    # NOTE: Dangerous Forests is an ON-DISC feature (iso_patcher
    # apply_dangerous_forests, PATCHER_VERSION 20, live-verified 2026-07-06): the
    # encounter fn itself reads the party tile's ATT attr (==0x0006 = forest) via
    # the field struct's live map pointers and rolls forest fights from a cave
    # danger pool -- frame-exact, no client loop. The old client zone-table-swap
    # loop that lived here could never win the race with the per-step roll and was
    # removed. The live party-position RE it produced (ff1_data FIELD_STRUCT_PTR /
    # FIELD_PARTY_X_OFF / FIELD_ONFIELD_OFF) is kept for future features
    # (per-region water encounters etc.).

    async def _hold_spent(self, name, spent):
        """True once a MAP_SCOPED_FUNCTION_HOLD row's key item has been SPENT, so
        its function bit has no consumer left and the hold may stay on forever.

        Sticky: a spend is irreversible in the game, and the verdict costs a
        party read, so the first True is cached for the session. Only the
        'promotion' condition exists today (Bahamut consumed the Rat's Tail --
        the same party-job fingerprint D.KEY_ITEM_CONSUMED_ON_PROMOTION uses,
        because the tail's "accepted" record IS the bit we are holding). Fails
        SAFE: a read error answers False, i.e. the pre-2026-08-15 behaviour.
        """
        if spent is None:
            return False
        if name in self._hold_spent_seen:
            return True
        if spent != "promotion":
            return False
        try:
            jobs = await self.psp.read(
                self.sa(D.class_addr_sa(0)),
                D.CLASS_STRIDE * (D.PARTY_COUNT - 1) + 1)
        except Exception:
            return False
        if not any(D.PROMOTED_JOB_MIN <= jobs[r * D.CLASS_STRIDE] <= D.BLACK_WIZARD
                   for r in range(D.PARTY_COUNT)):
            return False
        self._hold_spent_seen.add(name)
        logger.info(f"  [{name.lower()}] spent (party promoted) -> function bit "
                    f"held clear from now on (nothing reads it any more)")
        return True

    async def _at_ow_tile(self, ow_x, ow_y, radius):
        """True if the party is walking the OVERWORLD within `radius` tiles of
        (ow_x, ow_y). Used to prearm map-load-gated NPCs (see D.NPC_MAP_RESET).

        `radius` is widened to at least D.PREARM_MIN_RADIUS -- see that constant
        for why every doorstep net is deliberately generous.

        Reads the LIVE position by dereferencing FIELD_STRUCT_PTR -- NOT the
        vehicle rec0 copy at 0x08D11400, which is a stale frozen save-copy (the
        dangerous-forests bug). The deref follows save-block relocation on its
        own, so no sa() here.

        Deliberately NOT gated on FIELD_ONFIELD_OFF. That flag is documented as
        "coords valid" from the battle-flag RE, but a live sample while walking
        the OVERWORLD (2026-07-16, 150 samples over 15 s) read 0 in 98 of them
        while the coords tracked movement correctly across 66 distinct tiles --
        so on the overworld it is not a validity signal. Gating on it fired the
        prearm on only ~1/3 of ticks, which would intermittently miss a player
        walking straight into the cave: exactly the bug this prearm exists to
        fix. The FINE map-id check is the real guard -- it pins us to the
        overworld, so a same-coords tile inside some dungeon can never match,
        and a stale coord read mid-transition is still a coord near the mouth.
        """
        try:
            fine = struct.unpack("<I", await self.psp.read(
                self.sa(D.FIELD_MAP_ID_SA), 4))[0]
            if fine != D.OVERWORLD_FIELD_MAP_ID:
                return False
            fbase = struct.unpack("<I", await self.psp.read(
                D.FIELD_STRUCT_PTR, 4))[0]
            if not (0x08000000 <= fbase < 0x0A000000):
                return False
            x = struct.unpack("<H", await self.psp.read(
                fbase + D.FIELD_PARTY_X_OFF, 2))[0]
            y = struct.unpack("<H", await self.psp.read(
                fbase + D.FIELD_PARTY_Y_OFF, 2))[0]
        except Exception:
            return False
        radius = max(radius, D.PREARM_MIN_RADIUS)
        return abs(x - ow_x) <= radius and abs(y - ow_y) <= radius

    # ---------------- NPC story-event location (Princess) ----------------
    async def _npc_loop(self):
        """Send the Princess location check when the player NORMALLY receives the
        Lute. Detector = the Lute key-item bit 0x08D1153B & 0x80, which sets exactly
        at the princess-gives-Lute scene -- so the check fires at that moment, not
        on the next chest open. King was removed (bridge always built now). The item
        received is delivered by the normal received-items pipeline (_grant_loop).

        The Lute is a SHUFFLED AP item placed elsewhere in the multiworld, so the
        game handing the player a free Lute at this scene is wrong -- it would grant
        the key item outside the AP pool. We STRIP the native Lute bit here (the same
        bit we detect on) so the Lute is obtainable only as its randomized AP item.
        The strip is skipped if the player has already legitimately received the Lute
        AP item, since the grant loop owns the bit in that case -- clearing it would
        wipe a found Lute. Runs every tick (not just at detection) so a save/reload
        between the scene and the strip can't leave a free Lute behind."""
        lute_ap_iid = ID.item_id(D.CAT_KEY, D.key_item_id("Lute"))
        checks = [
            (D.LUTE_KEYITEM_ADDR, D.LUTE_KEYITEM_MASK,
             ID.npc_loc_id(D.PRINCESS_NPC_ORDINAL), "Princess"),
        ]
        # Every single-byte address this loop observes, so one span read can
        # serve them all (see _ByteSnapshot). Canonical (pre-sa) addresses:
        # the 12 story/event flag bytes, the whole 5-byte key-item possession
        # bitfield, and every function-bit byte. FIELD_MAP_ID / INVENTORY live
        # far outside this window and stay separate, conditional reads.
        span_addrs = {
            D.LUTE_KEYITEM_ADDR, D.SHIP_FLAG_ADDR, D.BIKKE_DEFEATED_ADDR,
            D.CANOE_KEYITEM_ADDR,
            D.CANOE_FUNCTION_ADDR, D.LEVISTONE_EVENT_ADDR,
            D.EARTH_ROD_EVENT_ADDR, D.CHIME_EVENT_ADDR, D.WARP_CUBE_EVENT_ADDR,
            D.BOTTLE_EVENT_ADDR, D.OXYALE_EVENT_ADDR, D.ADAMANTITE_EVENT_ADDR,
            D.SMITH_EVENT_ADDR,
            D.ELF_PRINCE_QUEST_ADDR,     # shadow flag 69 (prince_gate_split)
        }
        span_addrs |= {fb[0] for fb in D.KEY_ITEM_FUNCTION_BITS.values()}
        # the whole 5-byte key-item possession bitfield (ids 1..36, running
        # BACKWARD from KEY_ITEM_BITFIELD_HIGH -- see ff1_data.key_item_bit)
        span_addrs |= {D.key_item_bit(k)[0] for k in range(1, 37)}
        span_lo, span_hi = min(span_addrs), max(span_addrs)
        span_len = span_hi - span_lo + 1

        async def tick():
            if self.save_delta is None:
                return
            if not self._synced():
                # Server list not authoritative (pre-first-resync or emptied by a
                # live disconnect). EVERY strip below gates "is this the free native
                # grant?" on the item NOT being won; an empty list reads everything
                # as not-won and strips the player's OWN key items (Canoe/Levistone
                # lost live 2026-07-08). Skip the whole tick until resynced -- we
                # also couldn't send the location checks over a dead socket anyway.
                return
            # NG+ char-creation: every NPC/story/key-item bit this loop reads is carried
            # from the beaten save, so it would fire the Princess/Bikke/Sage/Smith/... checks
            # and strip natives against a game that doesn't exist yet. Defer until the new
            # game commits (Chaos bit clears); the fresh save has these bits clear.
            if await self._carried_save_snapshot():
                return
            # ONE read serves every single-byte observation below.
            snap = _ByteSnapshot(self.psp, self.sa(span_lo),
                                 await self.psp.read(self.sa(span_lo), span_len))

            with self._stage("func-reassert"):
                # Reconcile function bits for WON usage/movement key items. grant_key_item
                # sets these only at delivery; the grant-counter anti-dup never re-runs for
                # an already-delivered item, so a bit delivered under an old mask or dropped
                # by a save reload is otherwise never restored (the Oxyale mermaid bug,
                # 2026-07-17). Idempotent: writes only when a bit is actually missing. The
                # allowlist deliberately excludes NPC_MAP_RESET gate items and trade-chain
                # turn-in items (see ff1_data.OWNED_FUNCTION_REASSERT).
                for rk in D.OWNED_FUNCTION_REASSERT:
                    if not self._key_won(rk):
                        continue
                    faddr_raw, fmask = D.KEY_ITEM_FUNCTION_BITS[rk]
                    # Per-key narrowing (Oxyale: b1 spawn bit only -- b2/b3 belong
                    # to its NPC_MAP_RESET row and reasserting them fights the hold).
                    fmask = D.OWNED_FUNCTION_REASSERT_MASK.get(rk, fmask)
                    fsa = self.sa(faddr_raw)
                    fcur = await snap.rd(fsa)
                    if (fcur & fmask) != fmask:
                        await snap.wr(fsa, fcur | fmask)
                        logger.info(f"  [func-reassert] key {rk}: set function bits "
                                    f"{faddr_raw:#x} |= {fmask:#04x} (won item, was "
                                    f"{fcur:#04x})")
                    # On-disc gate-split shadow flag (v260): on a baked ISO the
                    # REAL gate reads this private flag, not the vanilla bit
                    # above, so a save reload dropping it silently re-locks the
                    # gate (the Titan re-blocks). Pin it while owned, same as
                    # the function bit. May sit outside the snapshot span, so
                    # read/write directly.
                    sb = D.GATE_SPLIT_SHADOW_BITS.get(rk)
                    if sb:
                        ssa, smask = self.sa(sb[0]), sb[1]
                        scur = (await self.psp.read(ssa, 1))[0]
                        if (scur & smask) != smask:
                            await self.psp.write(ssa, bytes([scur | smask]))
                            logger.info(f"  [func-reassert] key {rk}: set gate-split "
                                        f"shadow flag {sb[0]:#x} |= {smask:#04x} "
                                        f"(won item, was {scur:#04x})")

            with self._stage("lute-tablets"):
                # lute_tablets mode: the Lute is never an AP item; it is EARNED by
                # holding lute_tablets_required tablet copies. "Won" then means
                # "assembled" -- drives both the strip below and the bit-set here.
                need = self.lute_tablets_required
                if need:
                    lute_won = self._tablet_count() >= need
                    if lute_won:
                        # Idempotent: set the possession bit once assembled (there is
                        # no Lute grant in this mode; nothing else ever sets it). The
                        # strip branch below can then never fire (lute_won is True),
                        # and this re-set every tick survives a save/load rollback.
                        cur = await snap.rd(self.sa(D.LUTE_KEYITEM_ADDR))
                        if (cur & D.LUTE_KEYITEM_MASK) != D.LUTE_KEYITEM_MASK:
                            await snap.wr(self.sa(D.LUTE_KEYITEM_ADDR),
                                          cur | D.LUTE_KEYITEM_MASK)
                            logger.info(f"  [lute-tablets] {self._tablet_hw}/{need} "
                                        f"tablets -> Lute assembled (possession bit set)")
                else:
                    lute_won = lute_ap_iid in self._ever_won

            with self._stage("equipment-runes"):
                # equipment_runes: set story flag 62 (Equipment Rune Key assembled)
                # once enough rune copies are held. Idempotent re-set every tick --
                # nothing native touches ids 49-62, but a save/load rollback to a
                # pre-assembly save must re-learn the unlock. Never stripped (the
                # sticky _rune_count can only grow).
                rneed = self.equipment_runes_required
                if rneed and self._rune_count() >= rneed:
                    cur = await snap.rd(self.sa(D.RUNE_KEY_FLAG_ADDR))
                    if (cur & D.RUNE_KEY_FLAG_MASK) != D.RUNE_KEY_FLAG_MASK:
                        await snap.wr(self.sa(D.RUNE_KEY_FLAG_ADDR),
                                      cur | D.RUNE_KEY_FLAG_MASK)
                        logger.info(
                            f"  [equipment-runes] {self._rune_hw}/{rneed} runes -> "
                            f"Equipment Rune Key assembled (story flag 62 set; "
                            f"equipment activation unlocked)")

            with self._stage("npc-checks"):
                for addr, mask, lid, name in checks:
                    cur = await snap.rd(self.sa(addr))
                    if (cur & mask) != mask:
                        continue
                    if lid not in self.sent_locations:
                        self.sent_locations.add(lid)
                        await self.check_locations([lid])
                        logger.info(f"NPC ({name}) flag set -> check")
                    # Strip the free native Lute unless it was won as an AP item
                    # (or assembled from tablets -- lute_won covers both).
                    if not lute_won:
                        await snap.wr(self.sa(addr), cur & ~mask)
                        logger.info("  [princess] stripped native Lute "
                                    "(Lute is a randomized AP item)")

            with self._stage("npc-map-reset"):
                # --- NPC map-entry native-refresh (2026-07-13) -------------------
                # See D.NPC_MAP_RESET. For NPCs whose native-grant gate bit doubles
                # as a randomized AP key item's function bit (Warp Cube / Waterfall
                # robot): while the player is IN the NPC's map with its AP location
                # unchecked, HOLD the gate + possession bits clear every tick so the
                # NPC offers vanilla-style regardless of when the AP key arrived. A
                # rise of the gate between ticks = the NPC fired -> send the check;
                # the location's AP item is delivered by the normal received-items
                # pipeline and the obtain-box shows its AP name via _mapmsg_loop.
                # EXCEPT: a rise caused by our own grant_key_item (save-reload
                # re-grant while standing in the map) is flagged via
                # _npc_reset_selfgrant and re-cleared, NOT fired -- that exact race
                # false-fired the robot check live 2026-07-13. Outside the map (or
                # once checked), a won key's bits are restored so its function works.
                if D.NPC_MAP_RESET:
                    mid = struct.unpack("<I", await self.psp.read(
                        self.sa(D.FIELD_MAP_ID_SA), 4))[0]

                    # Map-scoped function hold (D.MAP_SCOPED_FUNCTION_HOLD): the
                    # same gate-bit collision as the rows below, for a bit whose
                    # victim is a whole DUNGEON rather than one NPC location, so
                    # there is no location to gate on -- scope it to the map
                    # instead. Rat's Tail: 0x1151E b7 is both the tail's Bahamut
                    # accept gate and story flag 23 = "Citadel trial done", so a
                    # won AP tail despawned the admitting elder and locked all 10
                    # Citadel chests out of the seed (live 2026-08-03). Hold the
                    # bit clear inside the Citadel, restore it everywhere else.
                    # Idempotent: writes only when a bit actually differs.
                    # The doorstep prearm is REQUIRED, not a nicety: the in-map hold
                    # alone clears the bit one tick AFTER the map has loaded, and
                    # the NPC roster is bound at load -- live 2026-08-03 the Citadel
                    # loaded with flag 23 set (elder already despawned) and the hold
                    # only took effect afterwards. Holding it clear at the overworld
                    # doorstep makes the dungeon LOAD in the not-done state.
                    # The out-of-scope RESTORE is the dangerous half (player
                    # report 2026-08-15: Crown owned, flag 22 clear, Citadel
                    # entrance floor empty again). Two guards, both added then:
                    # a spent item is never restored at all, and a lone
                    # out-of-scope tick -- which the map-transition window
                    # produces -- is debounced instead of believed.
                    # Row legend (mirrors the NPC_MAP_RESET unpack below): name,
                    # map-id set, key-item id gate, flag address, bit mask,
                    # prearm ow-tile-or-None, mode 'won'/'unwon', reset ordinal,
                    # spent condition or None.
                    for (hname, hmaps, hkid, haddr_raw, hmask, hprearm,
                         hmode, hrord, hspent) in D.MAP_SCOPED_FUNCTION_HOLD:
                        hwon = self._key_won(hkid)
                        # 'won' rows guard against OWNING the item too early;
                        # 'unwon' rows guard against the native event firing before
                        # the item is found. Either way the row is inert in the
                        # other state -- see ff1_data for the two cases.
                        if hwon != (hmode == "won"):
                            continue
                        hsa = self.sa(haddr_raw)
                        hcur = await snap.rd(hsa)
                        in_scope = mid in hmaps or (
                            hprearm is not None and await self._at_ow_tile(*hprearm))
                        # Spent = the bit has no consumer left, so the hold goes
                        # GLOBAL and permanent -- which also self-heals a save
                        # that is already carrying the bit set from an earlier
                        # out-of-scope restore (see D.MAP_SCOPED_FUNCTION_HOLD).
                        spent_now = (not in_scope
                                     and await self._hold_spent(hname, hspent))
                        if in_scope or spent_now:
                            self._hold_oos[hname] = 0
                            if hcur & hmask:
                                await snap.wr(hsa, hcur & ~hmask)
                                where = ("in-map" if mid in hmaps
                                         else "at doorstep" if in_scope
                                         else "everywhere (item spent)")
                                logger.info(f"  [{hname.lower()}] holding function "
                                            f"bit clear {where} -> native content "
                                            f"stays available")
                            continue
                        # Out of scope: restore. An 'unwon' row may only restore a
                        # bit whose native event genuinely happened (its location is
                        # checked) -- setting it unconditionally would fake the
                        # event and suppress the very NPC that grants the check.
                        if hmode == "unwon" and not (
                                hrord is not None
                                and ID.npc_loc_id(hrord) in self.sent_locations):
                            continue
                        # Debounce: one out-of-scope tick can be the map-transition
                        # window, not a real departure (D.MAP_SCOPED_HOLD_RESTORE_TICKS).
                        seen = self._hold_oos.get(hname, 0) + 1
                        self._hold_oos[hname] = seen
                        if seen < D.MAP_SCOPED_HOLD_RESTORE_TICKS:
                            continue
                        if (hcur & hmask) != hmask:
                            await snap.wr(hsa, hcur | hmask)

                    for row in D.NPC_MAP_RESET:
                        (rname, rmap, gaddr, gmask, rkid, rordn,
                         hold_poss, prearm, consumed) = row[:9]
                        # Optional 10th field: bits a won key must have SET, when
                        # that differs from the detect/hold mask (Levistone: hold
                        # b4 only, restore b4+b5 -- holding the airship bit clear
                        # made the airship vanish on every cave exit).
                        rmask = row[9] if len(row) > 9 else gmask
                        prearm_poss = row[10] if len(row) > 10 else False
                        # Optional 12th field hold_if=(addr,mask): the row's
                        # holds (prearm + in-map) are ARMED only while that bit
                        # is set. For a gate bit that doubles as a live gameplay
                        # function in the very map being held, an unconditional
                        # hold deadlocks the player when the NPC cannot fire
                        # yet (historical case: Mystic Key b1 = the locked-door
                        # bit, four doors inside Elven Castle -- that row is
                        # now GONE, cured on disc by prince_gate_split, but the
                        # mechanism stays for any future collision row).
                        # Disarmed -> fall through to the won-key restore, so
                        # the function bit stays SET.
                        hold_if = row[11] if len(row) > 11 else None
                        disarmed = False
                        if hold_if is not None:
                            _haddr, _hmask = hold_if
                            disarmed = not (await snap.rd(self.sa(_haddr))
                                            & _hmask)
                        lid = ID.npc_loc_id(rordn)
                        won = self._key_won(rkid)
                        gsa = self.sa(gaddr)
                        gcur = await snap.rd(gsa)
                        paddr_raw, pmask = D.key_item_bit(rkid)
                        psa = self.sa(paddr_raw)
                        pcur = await snap.rd(psa)
                        # rmap is a single FIELD_MAP_ID or a TUPLE of them: the
                        # Onrac Caravan is two nested maps (outer camp 0x1F +
                        # tent interior 0x17) and the gate must be held across
                        # both, or stepping camp -> tent reloads it gated shut.
                        in_map = (mid in rmap if isinstance(rmap, tuple)
                                  else mid == rmap)
                        if not in_map:
                            # left the row's map -> the next in-map tick is a
                            # genuine entry again (watchdog counter, see below)
                            self._npc_reset_entryclears.pop(rkid, None)
                        if disarmed or lid in self.sent_locations or not in_map:
                            # PREARM (map-load-gated NPCs, e.g. Sarda): his state is
                            # fixed when his map LOADS, so the entry-tick clear below
                            # is one map load too late -- he refuses on the first
                            # visit and only offers after a walk-out/walk-in. While
                            # the player stands on the overworld at his cave mouth,
                            # hold the gate clear so his map loads a willing NPC.
                            # Radius-scoped on purpose: this bit is also the rod's
                            # Lich-gate function bit (see ff1_data).
                            #
                            # (D.PREARM_GLOBAL, exempt_map...) is the same idea for
                            # a map-load-gated pickup with NO overworld doorstep to
                            # scope to (the Flying Fortress Adamantite: you arrive
                            # by airship + interior stairs, so there is no tile to
                            # watch). Hold the gate clear EVERYWHERE until the
                            # location is checked, so the room always loads with its
                            # pickup drawn. Without this the out-of-map restore
                            # below re-sets the bit the moment you step out, and the
                            # walk-out/walk-in that is supposed to redraw the pickup
                            # reloads the map gated shut again -- the location stays
                            # dead no matter how many times you re-enter. The
                            # exempt maps (e.g. Mount Duergar 0x2E, where the Smith
                            # READS this bit as his accept gate) fall through to the
                            # won-key restore, so the bit's function consumers in
                            # other rooms keep working while the pickup is unchecked.
                            # (D.PREARM_OVERWORLD,) holds ONLY on the overworld:
                            # for an ow-entered map whose gate bit is a function
                            # bit consumed in OTHER interiors (Warp Cube / Mirage
                            # warp) -- everywhere else falls to the restore so
                            # the won key stays in the menu and functional.
                            _global = (isinstance(prearm, tuple)
                                       and prearm[0] == D.PREARM_GLOBAL)
                            _ow = (isinstance(prearm, tuple)
                                   and prearm[0] == D.PREARM_OVERWORLD)
                            if (prearm and won and not disarmed
                                    and lid not in self.sent_locations
                                    and ((_global and mid not in prearm[1:])
                                         or (_ow and mid ==
                                             D.OVERWORLD_FIELD_MAP_ID)
                                         or (not _global and not _ow
                                             and await self._at_ow_tile(*prearm)))):
                                where = ("(global)" if _global
                                         else "(overworld)" if _ow
                                         else "at cave mouth")
                                if gcur & gmask:
                                    await snap.wr(gsa, gcur & ~gmask)
                                    logger.info(f"  [{rname.lower()}] prearmed gate "
                                                f"clear {where} -> NPC offers on "
                                                f"entry")
                                # See prearm_poss (ff1_data): the robot's already-
                                # gave check reads POSSESSION too, and it binds at
                                # map load -- clearing possession only on the entry
                                # tick is a map load too late.
                                if prearm_poss:
                                    if pcur & pmask:
                                        await snap.wr(psa, pcur & ~pmask)
                                        logger.info(f"  [{rname.lower()}] prearmed "
                                                    f"possession clear {where}")
                                elif not (pcur & pmask):
                                    # A won key whose row does NOT hide possession
                                    # must still SHOW in the menu while its GATE is
                                    # held clear -- the hold is about the NPC/pickup
                                    # re-offering, not about the item. This branch
                                    # `continue`s past the won-restore below, so
                                    # possession has to be topped up HERE.
                                    #
                                    # A no-op for an AP-item seed (grant_key_item
                                    # set it at delivery), but a DERIVED key has no
                                    # grant to set it: levistone_shards assembles
                                    # the Levistone from a counter, so the player
                                    # hit 5 of 5 and owned nothing anywhere except
                                    # the overworld -- the one map this global
                                    # prearm exempts (live 2026-08-12, in Marsh
                                    # Cave). The in-map hold (hold_poss) is
                                    # untouched, so the Ice Cavern pickup still
                                    # loads collectable.
                                    await snap.wr(psa, pcur | pmask)
                                    logger.info(f"  [{rname.lower()}] won key "
                                                f"possession restored {where} "
                                                f"(gate stays held for the check)")
                                # A prearm_poss hold makes a WON key item
                                # invisible in the Key Items menu while it is
                                # active. That is correct (see the Warp Cube
                                # row) but looks exactly like a lost item, so
                                # say so once per session.
                                hint = D.PREARM_HOLD_HINT.get(rname)
                                if hint and rname not in self._prearm_hinted:
                                    self._prearm_hinted.add(rname)
                                    logger.info(
                                        f"[FF1 PSP] Your {rname} is held out of "
                                        f"the menu for now: {hint}.")
                                continue
                            # Checked, or not at the NPC: keep a won key's bits SET
                            # (function + menu), restoring any hold-clears. EXCEPT:
                            # once the row's `consumed` bit is up (e.g. the Smith
                            # forged the Adamantite, 0x11520 b3), the key is spent
                            # forever -- hold POSSESSION clear instead so the dead
                            # key leaves the menu (vanilla consumes it too; the
                            # unconditional restore kept resurrecting it, live
                            # 2026-07-20). The gate/function bit is still restored.
                            if won:
                                # FULL-mask compare, not truthiness: the Levistone
                                # row's gate mask is TWO bits (0x30) and a partial
                                # state (obtained set, airship clear) must still be
                                # topped up -- `gcur & gmask` was truthy on the
                                # partial match, so the airship bit never restored
                                # and the airship vanished (live 2026-07-20).
                                if (gcur & rmask) != rmask:
                                    await snap.wr(gsa, gcur | rmask)
                                spent = False
                                if consumed is not None:
                                    caddr, cmask = consumed
                                    spent = bool(await snap.rd(self.sa(caddr))
                                                 & cmask)
                                if spent:
                                    if pcur & pmask:
                                        await snap.wr(psa, pcur & ~pmask)
                                        logger.info(
                                            f"  [{rname.lower()}] spent (turn-in "
                                            f"done) -> possession stripped")
                                elif not (pcur & pmask):
                                    await snap.wr(psa, pcur | pmask)
                            continue
                        # In the NPC's map, location unchecked.
                        # rmap may be a TUPLE of map ids (the Onrac Caravan's
                        # nested camp 0x1F + tent 0x17) while _npc_reset_lastmap
                        # is a SCALAR mid -- `scalar != tuple` is ALWAYS True, so
                        # EVERY in-map tick read as an entry tick and the gate
                        # rise from a genuine purchase was wiped as carried-in
                        # state instead of firing the check. "Onrac - Caravan"
                        # could never be bought: the merchant re-offered the
                        # Faerie forever and no check ever sent (live 2026-08-08,
                        # Prime -- the very first tuple-rmap row, added with the
                        # Faerie's NPC_MAP_RESET migration 2026-08-05).
                        entered = (self._npc_reset_lastmap not in rmap
                                   if isinstance(rmap, tuple)
                                   else self._npc_reset_lastmap != rmap)
                        # Self-heal for the bug class above: one continuous stay
                        # in the map can only ever produce ONE entry clear. A
                        # second means the edge-detect is broken for this row --
                        # say so loudly and treat the rise as the handover it
                        # almost certainly is, rather than silently eating the
                        # player's location for the rest of the seed. Own-grant
                        # clears are legitimate and repeatable, so they are NOT
                        # counted (each is consumed from _npc_reset_selfgrant).
                        selfgrant = rkid in self._npc_reset_selfgrant
                        if (gcur & gmask) and entered and not selfgrant:
                            seen = self._npc_reset_entryclears.get(rkid, 0) + 1
                            self._npc_reset_entryclears[rkid] = seen
                            if seen > 1:
                                logger.warning(
                                    f"  [{rname.lower()}] gate rose again "
                                    f"WITHOUT leaving the map ({seen} entry "
                                    f"clears in one stay) -- the entry edge-"
                                    f"detect is wrong for this row. Treating it "
                                    f"as the NPC firing so the check is not "
                                    f"lost; please report this line.")
                                entered = False
                        if (gcur & gmask) and (entered or selfgrant):
                            # Set gate that is NOT the NPC firing: carried-in state
                            # on the entry tick (outside-map restore / save load),
                            # or our own grant's write. Clear both bits, no check.
                            self._npc_reset_selfgrant.discard(rkid)
                            await snap.wr(gsa, gcur & ~gmask)
                            if hold_poss and (pcur & pmask):
                                await snap.wr(psa, pcur & ~pmask)
                            logger.info(f"  [{rname.lower()}] cleared gate"
                                        f"{'+possession' if hold_poss else ''} in-map "
                                        f"({'entry' if entered else 'own grant'})"
                                        f" -> NPC re-offers")
                            continue
                        if gcur & gmask:
                            # Genuine rise = the NPC fired its handover.
                            self.sent_locations.add(lid)
                            await self.check_locations([lid])
                            logger.info(f"NPC ({rname}) fired -> check")
                            if not won:
                                # Strip the free native key; a won key's bits are
                                # restored by the checked-branch on the next tick.
                                if pcur & pmask:
                                    await snap.wr(psa, pcur & ~pmask)
                                await snap.wr(gsa, gcur & ~gmask)
                                logger.info(f"  [{rname.lower()}] stripped native "
                                            f"{rname} (randomized AP item)")
                            continue
                        # Quiescent in-map: hold the gate clear so the NPC keeps
                        # offering, and -- only for NPCs whose already-gave check
                        # reads possession too (the robot) -- hold that clear as
                        # well. Sarda does not (live 2026-07-16), so his rod stays
                        # in the menu.
                        if gcur & gmask:
                            await snap.wr(gsa, gcur & ~gmask)
                        if hold_poss and (pcur & pmask):
                            await snap.wr(psa, pcur & ~pmask)
                            logger.info(f"  [{rname.lower()}] holding possession "
                                        f"clear in-map -> NPC re-offers")
                    self._npc_reset_lastmap = mid

            with self._stage("prince-quest"):
                # --- Elf Prince via shadow quest flag (prince_gate_split v247) --
                # The on-disc split (iso_patcher._NGS_SITES) moved the whole
                # Elven Castle quest chain to flag 69, so this is the healthy
                # distinct-event-bit pattern: RISE -> check; no holds, no
                # prearms, no map scoping -- flag 69 is set by exactly one
                # `2d 04 45 00` on the whole disc (the handover cutscene).
                qsa = self.sa(D.ELF_PRINCE_QUEST_ADDR)
                qcur = await snap.rd(qsa)
                qset = bool(qcur & D.ELF_PRINCE_QUEST_MASK)
                qlid = ID.npc_loc_id(D.MYSTIC_KEY_NPC_ORDINAL)
                if qset and qlid not in self.sent_locations:
                    self.sent_locations.add(qlid)
                    await self.check_locations([qlid])
                    logger.info("NPC (Mystic Key) fired -> check (quest flag)")
                    # The native cutscene also hands over the vanilla key
                    # (possession 0x1153B b3). Randomized location -> strip it
                    # unless the AP key is already won; the real item arrives
                    # via the grant loop. Possession only: the cutscene no
                    # longer touches flag 9, and a won key's flag 9 is owned by
                    # OWNED_FUNCTION_REASSERT.
                    if ID.item_id(D.CAT_KEY, 5) not in self._ever_won:
                        kaddr, kmask = D.key_item_bit(5)
                        kcur = await snap.rd(self.sa(kaddr))
                        if kcur & kmask:
                            await snap.wr(self.sa(kaddr), kcur & ~kmask)
                        logger.info("  [mystic key] stripped native Mystic Key "
                                    "(randomized AP item)")
                elif not qset and qlid in self.sent_locations:
                    # Checked (this session or the server's list) but the save
                    # under us says undone -- a rollback reload, or an older
                    # save from before this fix. Re-assert so the quest cannot
                    # re-run (no duplicate native key) and the healer/prince
                    # speak their post-quest lines.
                    await snap.wr(qsa, qcur | D.ELF_PRINCE_QUEST_MASK)
                    logger.info("  [mystic key] quest flag re-asserted from "
                                "checked location (save rollback)")

            with self._stage("consumed-keys"):
                # --- Spent key items leave the menu (2026-07-21) ----------------
                # See D.KEY_ITEM_CONSUMED. Vanilla consumes several key items at
                # their turn-in; our AP delivery sets the possession bit once and
                # nothing ever cleared it, so spent keys lingered forever (live
                # report: canal built + party promoted, powder + tail still listed).
                # Same idea as the NPC_MAP_RESET `consumed` field, for the keys that
                # have no map-reset row. HOLD (not one-shot): re-runs every tick so
                # a save reload or a grant-counter rollback re-grant cannot
                # resurrect a spent key. Function bits are deliberately untouched --
                # they carry the turn-in record.
                for ckid, (caddr_raw, cmask) in D.KEY_ITEM_CONSUMED.items():
                    cpaddr_raw, cpmask = D.key_item_bit(ckid)
                    cpsa = self.sa(cpaddr_raw)
                    cpcur = await snap.rd(cpsa)
                    if not (cpcur & cpmask):
                        continue
                    if not (await snap.rd(self.sa(caddr_raw)) & cmask):
                        continue
                    await snap.wr(cpsa, cpcur & ~cpmask)
                    logger.info(f"  [{D.KEY_ITEMS[ckid].lower()}] spent (turn-in "
                                f"done) -> possession stripped")

            with self._stage("rats-tail"):
                # Rat's Tail: no safe durable flag (its "accepted" bit is its own
                # function bit, which the AP grant sets) -- detect the promotion in
                # the PARTY instead: any row whose job id is >= 6 was promoted by
                # Bahamut, and nothing else ever writes a promoted id. The party
                # class array is OUTSIDE the snapshot span, so read it only while
                # the tail is actually still in the bag.
                tkid = D.KEY_ITEM_CONSUMED_ON_PROMOTION
                tpaddr_raw, tpmask = D.key_item_bit(tkid)
                tpsa = self.sa(tpaddr_raw)
                tpcur = await snap.rd(tpsa)
                if tpcur & tpmask:
                    jobs = await self.psp.read(
                        self.sa(D.class_addr_sa(0)),
                        D.CLASS_STRIDE * (D.PARTY_COUNT - 1) + 1)
                    promoted = any(
                        D.PROMOTED_JOB_MIN <= jobs[r * D.CLASS_STRIDE] <= D.BLACK_WIZARD
                        for r in range(D.PARTY_COUNT))
                    if promoted:
                        await snap.wr(tpsa, tpcur & ~tpmask)
                        logger.info("  [rat's tail] spent (party promoted) -> "
                                    "possession stripped")

            # field_map_id/in_pravoka are produced here and consumed by
            # the sage stage; pre-seed so an isolated failure here cannot
            # turn into a NameError there.
            field_map_id = None
            in_pravoka = False
            with self._stage("bikke-ship"):
                # Bikke (Provoka): the bikke_ship_split EBOOT feature (v65, always-on)
                # remaps story-flag id5 -> id63 inside Pravoka, so his defeat event
                # natively sets id63 ("Bikke defeated", persistent -- the pirates-
                # presence gate reads it too, so they never re-offer the fight) and
                # id5 is now purely "ship available". Send the check on id63's rising
                # edge; mirror id5 from Ship AP-item ownership. See ff1_data comment.
                #
                # BOTH Bikke detectors are gated on standing in PRAVOKA (2026-07-31).
                # id63 and id5 are SAVE-CARRIED bits: loading a save slot from another
                # session (or a debug grant) set them with no fight in this session, so
                # the client sent the Bikke check and STRIPPED the player's ship far from
                # Pravoka -- same bug class as the Sage/canoe one below. The fight can
                # only happen on map 0x37, so presence there is the corroborating signal.
                # Tradeoff: a defeat that happened while the client was disconnected now
                # reports on the next visit to Pravoka rather than on reconnect.
                field_map_id = struct.unpack("<I", await self.psp.read(
                    self.sa(D.FIELD_MAP_ID_SA), 4))[0]
                in_pravoka = field_map_id == D.PRAVOKA_MAP_ID
                bikke_lid = ID.npc_loc_id(D.BIKKE_NPC_ORDINAL)
                baddr = self.sa(D.BIKKE_DEFEATED_ADDR)
                bcur = await snap.rd(baddr)
                if (bcur & D.BIKKE_DEFEATED_MASK) and in_pravoka:
                    if bikke_lid not in self.sent_locations:
                        self.sent_locations.add(bikke_lid)
                        await self.check_locations([bikke_lid])
                        logger.info("NPC (Bikke) defeated flag (id63) -> check")
                saddr = self.sa(D.SHIP_FLAG_ADDR)
                scur = await snap.rd(saddr)
                ship_won = any(ID.is_vehicle(i) for i in self._ever_won)
                if (scur & D.SHIP_FLAG_MASK) and not ship_won and in_pravoka:
                    # id5 without the Ship item: an old-scheme save, or a native
                    # defeat that leaked through an unpatched window. Treat as
                    # defeat evidence (check + durable id63), then strip -- id5 is
                    # ship-only now, and stripping no longer respawns the pirates.
                    if bikke_lid not in self.sent_locations:
                        self.sent_locations.add(bikke_lid)
                        await self.check_locations([bikke_lid])
                        logger.info("NPC (Bikke) legacy flag5 edge -> check")
                    if not (bcur & D.BIKKE_DEFEATED_MASK):
                        await snap.wr(baddr, bcur | D.BIKKE_DEFEATED_MASK)
                        logger.info("  [bikke] defeated flag (id63) backfilled")
                    await snap.wr(saddr, scur & ~D.SHIP_FLAG_MASK)
                    logger.info("  [bikke] stripped native ship "
                                "(Ship is a randomized AP item)")
                elif ship_won and not (scur & D.SHIP_FLAG_MASK):
                    await snap.wr(saddr, scur | D.SHIP_FLAG_MASK)
                    logger.info("  [ship] set flag5 -> ship spawns at Provoka")

            with self._stage("sage-canoe"):
                # Crescent Lake sage: natively gives the CANOE key item (0x08D11539 & 0x80),
                # only after the Earth Crystal. In the rando the Canoe is a POOL item and the
                # sage is a randomized NPC location. The sage sets NO durable flag beyond the
                # canoe possession + sailing bits (RE-confirmed 2026-07-15: his grant event
                # 0x1b1 is op37-grant + op30, no setStoryFlag), and our own grant loop sets
                # those SAME bits when delivering the AP Canoe -- so possession alone cannot
                # tell "sage gave it" from "we delivered it". Two complementary detectors:
                #
                # TALK detector (2026-07-24, replaces the map-presence proxy that fired
                # the check on mere town entry): the field-dialog state struct
                # (D.DIALOG_STATE_ADDR, static BSS) latches the last shown dialog's text
                # pointer + entry index. The giver sage's boxes are MAP08 entries
                # 0x17/0x18 and no other Crescent Lake NPC shows either (live-swept), so
                # map==Crescent Lake + latched entry in that pair == the player actually
                # TALKED to him. Works in any canoe-ownership order, no Lich/Earth-Crystal
                # prerequisite. A stale latch can only exist if the player already talked,
                # so re-fires are harmless (sent_locations dedups).
                #
                # The native-canoe STRIP is gated on that same talk proof (2026-07-31).
                # It used to run on "possession set + Canoe not in _ever_won" alone,
                # anywhere in the world: loading a save slot carried over from ANOTHER
                # session (or a debug-granted canoe) reads as "the sage gave it", so the
                # client sent the Sage check and stripped the player's canoe while they
                # were nowhere near Crescent Lake. Talk proof is the only signal that
                # actually distinguishes the sage's grant from our own delivery.
                # --- Crescent Lake sage via shadow quest flag (v255) ------------
                # Same healthy pattern as the Elf Prince above: RISE -> check. No
                # map scoping, no dialog latch, no fast poll, no rising edge.
                #
                # What this replaces and why: the detector used to be a live
                # pointer to the box THIS client authored, and that pointer is
                # live ONLY while the box is on screen. _npc_loop ticks every
                # 2.0s, so a player who pressed through the box was never
                # sampled and the check silently never sent (live 2026-08-10,
                # the Blue Curtain report). It had "died" twice before and each
                # time was blamed on DIALOG_STATE_ADDR moving -- it does move,
                # but the self-locating scan already absorbed that; the sampling
                # race was the real defect. A save-resident story flag has no
                # such window, which is why every OTHER npc handover was already
                # bit-based. See [[dialog-state-addr-moves-between-builds]].
                # --- PROXIMITY detector (v258, user decision 2026-08-10) --------
                # The check sends when the player stands inside a 10x10 box
                # centered on the giver sage (map 0x43, tile (47,22), captured
                # live standing beside him). The Bahamut check has always worked
                # this way (room + Rat's Tail), so this is an established shape.
                #
                # Why not his handover: his give latches THREE conditions at map
                # load -- Lich flag 17 SET, sailing flag 18 CLEAR, possession
                # CLEAR -- each proven blocking live 2026-08-10, and defeating
                # all three simultaneously required a flag-17 lie (rejected:
                # black-orb crystal bit), a flag-18 lie, a possession doorstep
                # prearm whose net reaches the lake river, AND an in-town hold.
                # After all of it he still refused. Proximity needs none of his
                # script to run. NOTE: the original 2026-07 design was map
                # presence and was replaced BECAUSE it fired on mere town entry;
                # the 10x10 near-him box is the deliberate middle ground.
                #
                # KNOWN TRADE (accepted): the check fires on approaching the
                # sages' circle, no talk needed. His box still shows the AP
                # item name (sage-box authoring) when talked to afterwards.
                canoe_ap_iid = ID.item_id(D.CAT_KEY, D.key_item_id("Canoe"))
                canoe_won = canoe_ap_iid in self._ever_won
                sage_lid = ID.npc_loc_id(D.SAGE_NPC_ORDINAL)
                ssa = self.sa(D.SAGE_QUEST_ADDR)
                scur = await snap.rd(ssa)
                sset = bool(scur & D.SAGE_QUEST_MASK)
                if (sage_lid not in self.sent_locations
                        and field_map_id == D.CRESCENT_LAKE_MAP_ID):
                    try:
                        fbase = struct.unpack("<I", await self.psp.read(
                            D.FIELD_STRUCT_PTR, 4))[0]
                        px = py = None
                        if 0x08000000 <= fbase < 0x0A000000:
                            px = struct.unpack("<H", await self.psp.read(
                                fbase + D.FIELD_PARTY_X_OFF, 2))[0]
                            py = struct.unpack("<H", await self.psp.read(
                                fbase + D.FIELD_PARTY_Y_OFF, 2))[0]
                    except Exception:
                        px = py = None
                    sx, sy, sr = D.SAGE_TILE
                    if (px is not None
                            and abs(px - sx) <= sr and abs(py - sy) <= sr):
                        self.sent_locations.add(sage_lid)
                        await self.check_locations([sage_lid])
                        logger.info(f"NPC (Sage) fired -> check (stood at "
                                    f"({px},{py}), within {2*sr}x{2*sr} of "
                                    f"the sage)")
                # Flag 81 (his GENUINE native handover, v255 repoint) is now
                # dedup-only: if a canoe-less player triggers his real give
                # (Lich genuinely dead), strip the duplicate native canoe.
                if sset and not canoe_won:
                    # His cutscene still hands over the vanilla canoe. Randomized
                    # location -> strip possession; the real item arrives via the
                    # grant loop. The SAILING bit no longer needs clearing here --
                    # the repointed set writes flag 81 instead of flag 18 -- but
                    # keep clearing it defensively for saves made on a pre-255
                    # ISO, where the native give did set it.
                    ccur = await snap.rd(self.sa(D.CANOE_KEYITEM_ADDR))
                    fcur = await snap.rd(self.sa(D.CANOE_FUNCTION_ADDR))
                    if ccur & D.CANOE_KEYITEM_MASK or fcur & D.CANOE_FUNCTION_MASK:
                        await snap.wr(self.sa(D.CANOE_KEYITEM_ADDR),
                                      ccur & ~D.CANOE_KEYITEM_MASK)
                        await snap.wr(self.sa(D.CANOE_FUNCTION_ADDR),
                                      fcur & ~D.CANOE_FUNCTION_MASK)
                        logger.info("  [sage] stripped native Canoe (possession + "
                                    "sailing; Canoe is a randomized AP item)")
                elif not sset and sage_lid in self.sent_locations:
                    # Checked, but the save under us says undone -- a rollback
                    # reload or a save from before this fix. Re-assert so his
                    # cutscene cannot re-run and hand out a second native canoe.
                    await snap.wr(ssa, scur | D.SAGE_QUEST_MASK)
                    logger.info("  [sage] quest flag re-asserted from checked "
                                "location (save rollback)")

                # (v258: the doorstep prearm, in-town both-bits hold and the
                # outside restore were all DELETED -- proximity detection needs
                # none of his script to run, so nothing holds any canoe bit
                # anywhere, ever. Key id 17 is back on OWNED_FUNCTION_REASSERT.)

            with self._stage("promoted-keys"):
                # Ice Cave Levistone: MIGRATED to NPC_MAP_RESET (2026-07-20). Its
                # "obtained" event bit 0x1151E b4 doubles as the floor pickup's
                # "already collected" gate (map-load, same class as the Adamantite):
                # an AP Levistone received before reaching the Ice Cavern spot made
                # the pickup vanish and the old not-owned-gated poll here was
                # suppressed by the very same ownership. The map-reset row (map 0x24,
                # gate mask 0x30, PREARM_GLOBAL exempting the overworld so the won
                # key's airship keeps flying) handles detection, strip, and the
                # function-bit restore. See the NPC_MAP_RESET row in ff1_data.

                # --- Six more promoted key items (2026-07-06) -----------------------
                # Earth Rod / Chime / Warp Cube / Bottled Faerie / Oxyale are now REAL
                # AP pool items whose ORIGINAL NPC source is a randomized location. Each
                # is detected on its native "obtained" event bit while the matching AP
                # item is NOT owned (that = the game's native grant, not our own delivery):
                # send the check once + strip the free native key item (possession bit +
                # the detector event bit) so it is obtainable ONLY as its randomized AP
                # item. POSSESSION-ONLY grants -- none are in KEY_ITEM_FUNCTION_BITS yet
                # (splits UNVERIFIED; a live strip/receive sweep will resolve them). Same
                # not-owned gate + WART as Sage/Levistone. Detector bits + ordinals live
                # in ff1_data (captured live 2026-07-05). Star Ruby is NOT here -- it is a
                # normal chest handled by _chest_poll_loop.
                # NOTE: Warp Cube (2026-07-13) and Earth Rod (2026-07-16) were MOVED
                # OUT of this not-owned-gated batch to the map-entry native-refresh
                # mechanism (D.NPC_MAP_RESET, see below): their gate bit doubles as
                # the AP key item's function bit (Warp Cube 0x11520 b0 = warp;
                # Earth Rod 0x1151D b7 = Lich gate), so obtaining the AP key BEFORE
                # visiting the NPC set the gate and suppressed the NPC's location.
                # Chime MIGRATED to NPC_MAP_RESET 2026-08-02 (Lefein map 0x50,
                # doorstep (230,92)): 0x1151F b7 is both the Sky-Castle-ascend
                # function bit and the elder's already-gave gate, so a won AP Chime
                # made him refuse AND the not-owned gate here swallowed the check --
                # "Lefein - Elder" was unreachable (live player report).
                # Oxyale MIGRATED to NPC_MAP_RESET 2026-07-21 (Gaia fairy's
                # already-gave gate = b1 fairy-freed, live player report; map 0x4B).
                # Bottled Faerie MIGRATED to NPC_MAP_RESET 2026-08-05, emptying this
                # batch too: the Faerie gained a function bit (0x11521 b2, the state
                # the Gaia spring actually reads -- live-proven), and that bit is
                # also the Caravan's sold/revert state, so it needs the row's
                # hold-until-checked + restore-when-won lifecycle. Its Gaia half is
                # a MAP_SCOPED_FUNCTION_HOLD 'unwon' row. See both ff1_data notes.
                for kname, evaddr_raw, evmask, ordn in ():
                    ap_iid = ID.item_id(D.CAT_KEY, D.key_item_id(kname))
                    won = ap_iid in self._ever_won
                    evaddr = self.sa(evaddr_raw)
                    evcur = await snap.rd(evaddr)
                    if not (evcur & evmask) or won:
                        continue
                    lid = ID.npc_loc_id(ordn)
                    if lid not in self.sent_locations:
                        self.sent_locations.add(lid)
                        await self.check_locations([lid])
                        logger.info(f"NPC ({kname}) event bit -> check")
                    # Strip the free native key item so only the randomized AP item is
                    # ever obtainable. Clear the POSSESSION bit, and -- for items with a
                    # function/event REGISTER bit -- clear that too (the
                    # KEY_ITEM_FUNCTION_BITS block below). We deliberately do NOT blindly
                    # clear the detector EVENT bit any more: for this batch's function-bit
                    # items (Earth Rod / Chime / Oxyale) the event bit IS their function
                    # bit, so the block below already clears it; but for the Bottled Faerie
                    # (NO function bit) that "event" bit is the Onrac Caravan's own
                    # "already sold" state. Clearing it un-reverts the shop, so the Caravan
                    # kept offering the Faerie forever AND the possession-only strip left
                    # the bought Faerie in the bag (bug 2026-07-16, live-verified: only
                    # possession + event flip on purchase, no inventory record). Leaving
                    # the bit set lets the Caravan revert to its tonic shop. The loop then
                    # re-enters each tick while the AP item is unowned, so the strip+log is
                    # guarded to fire only when it actually changes a bit (no spam, cf. the
                    # Excalibur strip below).
                    kid = D.key_item_id(kname)
                    paddr_raw, pmask = D.key_item_bit(kid)
                    paddr = self.sa(paddr_raw)
                    pcur = await snap.rd(paddr)
                    did_strip = False
                    if pcur & pmask:
                        await snap.wr(paddr, pcur & ~pmask)
                        did_strip = True
                    # Clear the FUNCTION event-register bit if this item has one (Earth Rod
                    # / Chime / Oxyale). For Earth Rod/Chime the function bit IS the
                    # detector event bit (same addr+mask); Oxyale's event bit is one bit of
                    # its function mask -- either way this is the only place the event bit
                    # gets cleared now, and it gates the loop off on the next tick. Bottled
                    # Faerie has no entry, so its event bit (the Caravan sold-state)
                    # persists -- exactly what makes the shop revert.
                    fb = D.KEY_ITEM_FUNCTION_BITS.get(kid)
                    if fb:
                        faddr, fmask = self.sa(fb[0]), fb[1]
                        fcur = await snap.rd(faddr)
                        if fcur & fmask:
                            await snap.wr(faddr, fcur & ~fmask)
                            did_strip = True
                    if did_strip:
                        logger.info(f"  [{kname.lower()}] stripped native {kname} "
                                    f"({kname} is a randomized AP item)")

            with self._stage("trade-chain"):
                # --- Classic-7 Mystic-Key trade chain (2026-07-06) -------------------
                # Crystal Eye (Astos) / Jolt Tonic (Matoya) / Mystic Key (Elf Prince) are
                # now REAL AP pool items whose grantor NPC is a randomized location. Unlike
                # the Earth Rod/Chime batch above, these three detect on the item's OWN
                # POSSESSION bit (0x1153B b5/b4/b3) -- the SAME bit the native NPC sets on
                # hand-over -- exactly like the Princess/Lute. Gate on the matching AP item
                # NOT owned (that = the native grant, not our own delivery which sets the
                # same possession bit via grant_key_item): send the check once + STRIP the
                # native possession bit so the item is obtainable ONLY as its randomized AP
                # item. POSSESSION-ONLY: nothing is in KEY_ITEM_FUNCTION_BITS for these
                # (splits UNVERIFIED). Same not-owned gate + WART as Sage/Levistone. Runs
                # every tick so a save/reload between the scene and the strip can't leave a
                # free key item behind.
                # Crystal Eye + Mystic Key MIGRATED to NPC_MAP_RESET (2026-07-20):
                # their function bits double as their grantor NPCs' "already gave"
                # gates (the Western Keep king's MAP-LOAD despawn / the Elf Prince's
                # talk-time skip), so an AP copy received early killed the location
                # -- and this poll's `or won` gate then swallowed the check after a
                # genuine handover (the Smith lesson, live 2026-07-20 x2).
                # Jolt Tonic MIGRATED 2026-08-03, the last of the Classic-7 batch and
                # the eleventh confirmed instance: 0x1151D b0 IS Matoya's own
                # already-traded gate (live player report -- AP tonic owned, Crystal
                # Eye in hand, and she answered "I don't need you anymore" while
                # `Matoya's Cave - Matoya` sat unchecked). Matoya's Cave map 0x23 +
                # overworld doorstep (161,110) captured live. This loop is now EMPTY;
                # it stays as the place a future promoted key item would land.
                for kname, ordn in ():
                    kid = D.key_item_id(kname)
                    ap_iid = ID.item_id(D.CAT_KEY, kid)
                    won = ap_iid in self._ever_won
                    paddr_raw, pmask = D.key_item_bit(kid)
                    paddr = self.sa(paddr_raw)
                    pcur = await snap.rd(paddr)
                    if not (pcur & pmask) or won:
                        continue
                    lid = ID.npc_loc_id(ordn)
                    if lid not in self.sent_locations:
                        self.sent_locations.add(lid)
                        await self.check_locations([lid])
                        logger.info(f"NPC ({kname}) possession bit -> check")
                    await snap.wr(paddr, pcur & ~pmask)
                    # ALSO clear the FUNCTION event-register bit (Crown/Crystal-Eye/
                    # Jolt-Tonic/Mystic-Key splits confirmed live 2026-07-06): the
                    # possession bit is display-only; the grantor NPCs / locked doors gate
                    # on the event bit, so stripping possession alone would leave the item
                    # functional. grant_key_item sets both on delivery -> the strip must
                    # clear both. Separate address from possession (unlike the Earth-Rod
                    # batch where detector == function), so it is a distinct write.
                    fb = D.KEY_ITEM_FUNCTION_BITS.get(kid)
                    if fb:
                        faddr, fmask = self.sa(fb[0]), fb[1]
                        fcur = await snap.rd(faddr)
                        if fcur & fmask:
                            await snap.wr(faddr, fcur & ~fmask)
                    logger.info(f"  [{kname.lower()}] stripped native {kname} "
                                f"({kname} is a randomized AP item)")

            with self._stage("smith"):
                # --- Adamantite pickup: MIGRATED to NPC_MAP_RESET (2026-07-19) ------
                # Was: a not-owned-gated poll on obtained-event 0x11520 b1, exactly
                # like the Earth-Rod batch. That inherited WART became a live dead
                # location: 0x11520 b1 is ALSO the Adamantite function bit
                # (KEY_ITEM_FUNCTION_BITS[7]), so an AP Adamantite obtained before
                # reaching the fortress set b1 -> the native pickup read "already
                # collected" and never appeared, while the poll's `not adam_won`
                # gate suppressed the check for the same reason. Both halves dead.
                # The map-reset mechanism below (D.NPC_MAP_RESET, map 0x3A) handles
                # the whole lifecycle now: hold b1 clear in-map so the pickup keeps
                # offering, fire the check on its genuine rise, strip the free native
                # ore when unowned, and restore b1 outside the map so the Smith
                # turn-in still works. See the NPC_MAP_RESET row in ff1_data.

                # --- Dwarf Smith turn-in -> Excalibur (2026-07-06) ------------------
                # Handing the Smith the Adamantite forges Excalibur and sets durable event
                # 0x11520 b3. The CHECK fires on b3 UNCONDITIONALLY (2026-07-20 fix):
                # b3 is set ONLY by the Smith's own turn-in -- receiving the AP
                # Excalibur is a plain inventory weapon add that never touches it --
                # so unlike the Sage/Levistone detectors this bit is unambiguous and
                # the old `and not exc_won` gate was pure loss: live 2026-07-20, the
                # player owned AP Excalibur already, forged with the Smith, and the
                # check was silently swallowed (the exact WART the old comment
                # predicted -- but here it was avoidable). Only the native-Excalibur
                # STRIP stays gated on not-won: once the AP Excalibur is owned, a
                # [2,39,qty] inventory record is the DELIVERED item, not the forge
                # freebie, and must not be zeroed.
                exc_ap_iid = ID.item_id(D.CAT_WEAPON, D.EXCALIBUR_WEAPON_ID)
                exc_won = exc_ap_iid in self._ever_won
                smaddr = self.sa(D.SMITH_EVENT_ADDR)
                smcur = await snap.rd(smaddr)
                if smcur & D.SMITH_EVENT_MASK:
                    smith_lid = ID.npc_loc_id(D.SMITH_NPC_ORDINAL)
                    if smith_lid not in self.sent_locations:
                        self.sent_locations.add(smith_lid)
                        await self.check_locations([smith_lid])
                        logger.info("NPC (Smith) event bit -> check")
                    # Strip the native Excalibur weapon record ([2,39,qty] -> zeroed)
                    # -- ONLY while the AP Excalibur is not owned (a record when it IS
                    # owned is the delivered AP item). Only write+log when a record
                    # actually exists; once zeroed the condition (smith bit set,
                    # Excalibur AP not won) stays true every tick forever, so an
                    # unconditional strip+log spammed the console (2026-07-07:
                    # Adamantite turned in but Excalibur AP never received).
                    if not exc_won:
                        invbase = self.sa(D.INVENTORY_BASE_SA)
                        blob = await self.psp.read(invbase, D.INV_RECORD_SIZE * 0x80)
                        stripped = False
                        for i in range(0, len(blob), D.INV_RECORD_SIZE):
                            if (blob[i] == D.CAT_WEAPON
                                    and blob[i + 1] == D.EXCALIBUR_WEAPON_ID):
                                await self.psp.write(invbase + i, bytes([0, 0, 0]))
                                stripped = True
                                break
                        if stripped:
                            logger.info("  [smith] stripped native Excalibur "
                                        "(Excalibur is a randomized AP item)")

            with self._stage("bahamut"):
                # --- Bahamut = AP LOCATION ONLY, no promotion (2026-07-08) ----------
                # Rat's Tail is granted possession-only (not in KEY_ITEM_FUNCTION_BITS),
                # so Bahamut refuses it and never promotes -- the old 0x1151F b0
                # class-change-done bit never sets. Detector instead: player is standing
                # in Bahamut's room (FIELD_MAP_ID == BAHAMUT_ROOM_ID) AND owns the AP
                # Rat's Tail (possession bit). FIELD_MAP_ID (0x13108, u32) is the fine
                # per-map id; the coarse LOADED_MAP_ID (0x13118) reads 1 for every
                # dungeon and would false-fire in Chaos Shrine. NOTHING to strip.
                bahamut_lid = ID.npc_loc_id(D.BAHAMUT_NPC_ORDINAL)
                if bahamut_lid not in self.sent_locations:
                    mid = struct.unpack("<I", await self.psp.read(
                        self.sa(D.FIELD_MAP_ID_SA), 4))[0]
                    if mid == D.BAHAMUT_ROOM_ID:
                        taddr, tmask = D.key_item_bit(D.RATS_TAIL_KEY_ID)
                        owned = (await snap.rd(self.sa(taddr))) & tmask
                        if owned:
                            self.sent_locations.add(bahamut_lid)
                            await self.check_locations([bahamut_lid])
                            logger.info("NPC (Bahamut) room + Rat's Tail -> check")

        await self._poll(2.0, "npc_loop", tick)

    # ---------------- random starting party ----------------
    def _job_l1_block(self, job):
        """The level-1 stat block to write for `job`. JOB_L1_BLOCK was harvested
        from a VANILLA game; when the running ISO is baked with
        monk_thief_dabble_in_magic, the game creates Thief/Monk with MP 3 and
        magic level 1 -- writing the vanilla block over that WIPED the feature
        (the 'my thief has no magic' bug). Mirror the feature's start-stats here.
        Block covers record offsets 0x08..0x1C: MP @+0x0C, maxMP @+0x0E,
        MagicLv @+0x10 (see ff1_data)."""
        blk = bytearray(D.JOB_L1_BLOCK[job])
        dabble = bool((self.slot_data.get("on_disc") or {})
                      .get("monk_thief_dabble_in_magic"))
        if dabble and self.bake_ok and job in IP.DABBLE_JOBS:
            struct.pack_into("<H", blk, 0x0C - D.L1_BLOCK_OFF, IP.DABBLE_START_MP)
            struct.pack_into("<H", blk, 0x0E - D.L1_BLOCK_OFF, IP.DABBLE_START_MP)
            blk[0x10 - D.L1_BLOCK_OFF] = IP.DABBLE_START_MAGICLV
        return bytes(blk)

    async def _party_state(self):
        """NEW-GAME freshness of the party records, for one-shot-at-new-game loops.

        "fresh"    -- all 4 records Lv1 / EXP 0: character creation just committed.
        "underway" -- some EXP > 0: a battle has been fought; never touch again.
        "uninit"   -- records not initialized yet (pre-creation), OR the new-game
                      verdict is not latched yet; caller should wait.
        "loaded"   -- a SAVE FILE whose party has not fought yet. Looks identical
                      to "fresh" in the records, so it is separated by the latched
                      _init_needed gate. Callers must NOT run new-game one-shots
                      here, and must NOT give up either: a title-screen round trip
                      can still bring a real new game (the gate is re-latched on
                      every save-block acquisition).
        Single source of truth for the gate _party_loop / _naked_monks_loop use."""
        if self._init_needed is None:
            return "uninit"          # gate not latched yet -> caller waits
        levels, exps = [], []
        for ci in range(D.PARTY_COUNT):
            levels.append(await self.psp.read_u32(self.sa(D.party_addr_sa(ci, D.P_LEVEL))))
            exps.append(await self.psp.read_u32(self.sa(D.party_addr_sa(ci, D.P_EXP))))
        if all(lv == 1 for lv in levels) and all(xp == 0 for xp in exps):
            return "fresh" if self._init_needed else "loaded"
        return "underway" if any(xp > 0 for xp in exps) else "uninit"

    async def _party_loop(self):
        """One-shot: write the yaml-chosen jobs into the party at NEW GAME.

        Only acts while the party is freshly created (all 4 records Lv1, EXP 0) so
        it never resets a party that has already leveled. For each configured
        member it writes the job's level-1 stat block (party_addr_sa(row, L1_BLOCK_OFF))
        and class/sprite byte (class_addr_sa(row) -- the menu reads a row's class from
        the PREVIOUS record's 0x5A, so class is its own array at PARTY_BASE_SA-2). Once
        the on-screen classes already match the request, it stops touching memory.
        See [[class-byte]]. Idempotent: re-applies after a save-state load that
        reverts to a still-fresh party, but goes idle the moment a battle is fought.
        """
        if not any(j is not None for j in self.party_jobs):
            return   # all vanilla -> nothing to do
        cfg = [(r, j) for r, j in enumerate(self.party_jobs) if j is not None]
        wrote_once = False
        while not self.exit_event.is_set():
            try:
                if self.save_delta is None:
                    await asyncio.sleep(0.5)
                    continue
                state = await self._party_state()
                if state != "fresh":
                    if state == "underway":
                        # exp>0 = a real battle (never reset a leveled party) OR the NG+
                        # carried snapshot: a New Game from a beaten save shows the cleared
                        # save's exp for the WHOLE character-creation screen (can be
                        # minutes), until the player commits (START: Done) and the game
                        # zeroes the block. That snapshot has the Chaos-defeated bit set;
                        # wait -- with NO timer, since commit is player-paced -- until it
                        # clears, then apply jobs to the freshly-zeroed party. A truly
                        # underway or finished game keeps exp>0 with the bit clear (real
                        # play) or set-forever (finished), so we bail or idle correctly.
                        if not wrote_once and await self._carried_save_snapshot():
                            await asyncio.sleep(0.5)
                            continue
                        logger.info("  [party] game underway -> stop setting party")
                        return        # battle fought; never touch again this run
                    await asyncio.sleep(0.5)
                    continue          # records not initialized yet; wait
                # ONE-SHOT at new game: write job stats + class, then STOP forever so
                # the player can rearrange Formation and Bahamut class-ups aren't
                # fought. Convergence is judged on the STABLE stats block (the game
                # re-syncs the volatile leader class byte every frame, so a class
                # check would never settle -> perpetual rewrite -> input lag + the
                # party "snapping back"). We require the stats to stay correct across
                # TWO consecutive passes (~1s apart) so a char-creation commit that
                # lands AFTER our first write is caught and re-applied before we exit.
                need_write = False
                for row, job in cfg:
                    blk = self._job_l1_block(job)   # dabble-aware (MP 3 / MagicLv 1)
                    if (await self.psp.read(self.sa(D.party_addr_sa(row, D.L1_BLOCK_OFF)), len(blk))) != blk:
                        await self.psp.write(self.sa(D.party_addr_sa(row, D.L1_BLOCK_OFF)), blk)
                        need_write = True
                    await self.psp.write(self.sa(D.class_addr_sa(row)), bytes([job]))
                if not need_write and wrote_once:
                    self._party_applied = True
                    logger.info(f"  [party] set jobs {self.party_jobs} "
                                f"({[D.JOB_NAMES[j] if j is not None else 'vanilla' for j in self.party_jobs]}) -> stable, done")
                    return            # confirmed stable: stop polling, no more load
                wrote_once = True
            except Exception as e:
                logger.info(f"  [party_loop] {e!r}")
            await asyncio.sleep(0.5)

    # ---------------- starting gil ----------------
    async def _starting_gil_loop(self):
        """One-shot at NEW GAME: move the purse to the yaml's starting_gil.

        Same freshness gate as _party_loop / _naked_monks_loop (all 4 records
        Lv1, EXP 0, and the NG+ carried snapshot waited out) so a game already
        underway is never re-funded -- spending the starting gil is permanent.

        Applied as a DELTA off D.VANILLA_START_GIL, not as an absolute write: an
        AP gil item can be granted during character creation, and an absolute
        write would silently eat it. Runs BEFORE _naked_monks_loop's strip
        payout by construction (that loop waits on _starting_gil_applied), so
        the two never fight over the same purse.

        Returns after the single write, so a save-state load back into a still-
        fresh party does NOT re-fund (the flag is per client session, exactly
        like the jobs write)."""
        if self.starting_gil is None or self.starting_gil == D.VANILLA_START_GIL:
            self._starting_gil_applied = True     # nothing to do; unblock monks
            return
        while not self.exit_event.is_set():
            try:
                if self.save_delta is None:
                    await asyncio.sleep(0.5)
                    continue
                state = await self._party_state()
                if state == "underway":
                    if await self._carried_save_snapshot():
                        await asyncio.sleep(0.5)
                        continue          # NG+ char-creation snapshot; wait it out
                    self._starting_gil_applied = True
                    logger.info("  [starting_gil] game underway -> left alone")
                    return
                if state != "fresh":
                    # "uninit" (pre-creation / verdict not latched) or "loaded"
                    # (a save file, however early) -- never re-pay the purse. Keep
                    # polling rather than returning: a title-screen round trip can
                    # still bring a real new game that DOES want the yaml purse.
                    await asyncio.sleep(0.5)
                    continue
                cur = min(await self.read_gil(), D.GIL_MAX)
                new = max(0, min(cur + self.starting_gil - D.VANILLA_START_GIL,
                                 D.GIL_MAX))
                await self.psp.write_u32(self.sa(D.GIL_ADDR_SA), new)
                self._starting_gil_applied = True
                logger.info(f"  [starting_gil] {cur} -> {new} "
                            f"(yaml {self.starting_gil}, vanilla "
                            f"{D.VANILLA_START_GIL})")
                return
            except Exception as e:
                logger.info(f"  [starting_gil] {e!r}")
            await asyncio.sleep(0.5)

    # ---------------- naked monks ----------------
    MONK_JOB = 2

    async def _naked_monks_loop(self):
        """One-shot at NEW GAME: strip every starting Monk's equipped gear, pay 7 gil each.

        Same freshness gate as _party_loop (all 4 records Lv1, EXP 0) so a party that
        has fought is never touched. For each Monk row: zero its equipped-gear block
        (record +0x1c..+0x1f = weapon + armor; the gear lives HERE, not in inventory,
        so zeroing destroys it -- nothing enters the bag), then grant D.NAKED_MONK_GIL
        per Monk actually stripped.

        Idempotent by construction: the gil is paid only for gear actually removed, so
        a re-entry on a still-fresh save (save-state load) strips nothing and pays
        nothing. Waits for _party_loop's job write to settle so it sees the FINAL
        classes; if no Monk is ever in the party it simply idles until the party stops
        being fresh, then returns.
        """
        if not self.naked_monks:
            return
        while not self.exit_event.is_set():
            try:
                if self.save_delta is None:
                    await asyncio.sleep(0.5)
                    continue
                state = await self._party_state()
                if state == "underway":
                    # exp>0 is also the NG+ carried snapshot through the whole
                    # character-creation screen; wait for commit (Chaos bit clears)
                    # instead of bailing, else the starting Monk is never stripped
                    # (live 2026-07-23: monk spawned with staff+clothes, no gil).
                    if await self._carried_save_snapshot():
                        await asyncio.sleep(0.5)
                        continue
                    return                    # game underway; never strip
                if state != "fresh":
                    # records not initialized yet / verdict not latched ("uninit"),
                    # or a SAVE FILE ("loaded") -- a re-strip would destroy gear the
                    # player bought and re-pay the 7 gil. Keep polling.
                    await asyncio.sleep(0.5)
                    continue
                # The yaml party write (_party_loop) may not have landed yet: wait for
                # it to settle so we see the FINAL classes, not the char-creation ones.
                if self.party_jobs and any(j is not None for j in self.party_jobs) \
                        and not getattr(self, "_party_applied", False):
                    await asyncio.sleep(0.5)
                    continue
                # Likewise wait for the starting_gil write, so the strip payout
                # rides ON TOP of the yaml purse instead of being overwritten.
                if not getattr(self, "_starting_gil_applied", False):
                    await asyncio.sleep(0.5)
                    continue
                stripped = 0
                for row in range(D.PARTY_COUNT):
                    cls = (await self.psp.read(self.sa(D.class_addr_sa(row)), 1))[0]
                    if cls != self.MONK_JOB:
                        continue
                    if await self._strip_equipment(row):
                        stripped += 1
                if stripped:
                    await self.grant_gil(D.NAKED_MONK_GIL * stripped)
                    logger.info(f"  [naked_monks] stripped {stripped} Monk(s) -> "
                                f"+{D.NAKED_MONK_GIL * stripped} gil")
                return
            except Exception as e:
                logger.info(f"  [naked_monks] {e!r}")
            await asyncio.sleep(0.5)

    async def _strip_equipment(self, row):
        """Zero row's 4-byte equipped-gear block (record +0x1c..+0x1f: weapon+armor).
        The gear is stored here, not in inventory, so this destroys it. Returns True
        if anything was actually equipped."""
        base = self.sa(D.party_addr_sa(row, D.EQUIP_OFF))
        blk = await self.psp.read(base, D.EQUIP_LEN)
        if not any(blk):
            return False
        await self.psp.write(base, bytes(D.EQUIP_LEN))
        return True

    # ---------------- Thief end-of-battle extra-item ability ----------------
    # Loot tiers keyed on the battle's total vanilla XP payout (sum of
    # D.MONSTER_XP over the spawned enemies), NOT party level: a low-level
    # party that downs a rich monster earns top-tier loot, a high-level party
    # farming goblins gets scraps. On a successful steal there is a
    # STEAL_RARE_CHANCE roll for the tier's rare list instead of the common
    # one. Pool entries are (cat, id) per D.CONSUMABLE_ITEMS; a nested LIST is
    # a CATEGORY that weighs as one slot but resolves to a random member on
    # pick (so "Fangs"/"Curtains"/"Tonics" = one pool slot each, not N).
    # duplicate ids = heavier weight (uniform pick within the list).
    STEAL_RARE_CHANCE = 0.05
    # Independent LUCK roll, rolled alongside the thief-AGI roll every battle.
    # L = sum of P_LCK across party members whose class is Thief/Ninja (same
    # filter as the AGI sum), so max L = 99*4. P = min(0.99, (L/50) * 0.45^tier):
    # low tiers stay very likely at modest Luck (t0 maxes ~L50, t1 ~L110), higher
    # tiers scale meaningfully but stay gated (t2 ~61% / t5 ~5.5% at L150). The two
    # rolls are independent but the steal is capped at ONE extra item per battle --
    # if EITHER roll hits, exactly one item is found.
    STEAL_LUCK_CAP = 0.99
    STEAL_LUCK_L_DIV = 50.0
    STEAL_LUCK_TIER_DECAY = 0.45
    # --- Luck RARITY UPGRADE (2026-07-31). A THIRD Luck use, rolled only when a
    # steal already hit and only after the base rarity is drawn: p = sumLCK/500
    # (x2 with the Stealth Ninja Scroll), promoting the result ONE step --
    # common -> rare, rare -> super, super unchanged. Deliberately NOT capped and
    # NOT tier-decayed: a max-Luck party (4 thieves ~396 Luck = 79%, 158% scrolled)
    # is meant to upgrade nearly always, at every tier. sumLCK is the same active,
    # gear-inclusive Thief/Ninja sum the hit roll uses, so a party whose thieves
    # are all KO'd/petrified has 0% to find loot AND 0% to upgrade it.
    # Unscrolled route to a super item: land the 5% rare, then land this.
    STEAL_LUCK_UPGRADE_DIV = 500.0
    STEAL_LUCK_UPGRADE_SCROLL_MULT = 2.0
    # categories (weigh as 1 slot; resolve to a random member)
    FANGS    = [(D.CAT_ITEM, 20), (D.CAT_ITEM, 21), (D.CAT_ITEM, 22),        # White/Red/Blue/
                (D.CAT_ITEM, 29)]                                           #   Vampire Fang
    CURTAINS = [(D.CAT_ITEM, 23), (D.CAT_ITEM, 24), (D.CAT_ITEM, 25),        # Light/Red/White/
                (D.CAT_ITEM, 26), (D.CAT_ITEM, 27)]                          #   Blue/Lunar Curtain
    TONICS   = [(D.CAT_ITEM, 31), (D.CAT_ITEM, 32), (D.CAT_ITEM, 33),        # Giant's/Faerie/Strength Tonic/
                (D.CAT_ITEM, 34), (D.CAT_ITEM, 35)]                          #   Protect/Speed Drink
    # Tables below are authored in tools/steal_pool_editor.html (drag/drop weight
    # editor; its "Export -> Python" output is pasted here verbatim). Rebalanced
    # 2026-07-31 -- see that tool for the live-% summary of any edit.
    #
    # Eye Drops (13) x2 + Echo Grass (14) were traded for Potions AT THE SAME
    # WEIGHT 2026-07-29 because both ids had become shop AP placeholders, which
    # the grant below re-rolled -> dead weights biasing the draw. That is no
    # longer true: since v202 (BUYB purchase mailbox) NO id is reserved and every
    # entry here carries its full weight, so the Potions could be traded back if
    # the old low-tier mix is wanted. Spider's Silk (19) and Cockatrice Claw (30)
    # were kept through that period and are simply live now.
    #
    # Rebalanced 2026-08-01: Eye Drops/Echo Grass restored to t0 (the re-roll that
    # made them dead is gone), t0 losing its duplicate Potion and its Antidote so
    # the tier is a flat 4-way. t1/t2 commons re-weighted much finer (11 and 16
    # slots) -- t1 is now Antidote-led with Cottage a 9% tail, t2 leads on Ether
    # and folds in Emergency Exit + single Red/White/Blue Curtains. Cottage and
    # Cockatrice Claw moved out of t1's rare list only in weight, not presence.
    # t3 (4000) commons shed Emergency Exit/Faerie Tonic and gained a doubled
    # Curtains slot + Fangs; its rare is now Turbo-Ether-led (50%) with Tonics.
    # t4 (8000) commons doubled Tonics-to-Turbo-Ether parity. t5 unchanged.
    STEAL_POOLS = [
        # (max_battle_xp_incl, common [...], rare [...])
        (300,   [(D.CAT_ITEM, 1), (D.CAT_ITEM, 16), (D.CAT_ITEM, 13),        # Potion/Sleeping Bag/
                 (D.CAT_ITEM, 14)],                                          #   Eye Drops/Echo Grass
                [(D.CAT_ITEM, 19), (D.CAT_ITEM, 2), (D.CAT_ITEM, 12)]),      #   rare: Spider's Silk/Hi-Potion/Gold Needle
        (800,   [(D.CAT_ITEM, 2), (D.CAT_ITEM, 17), (D.CAT_ITEM, 11),        # Antidote x4/Hi-Potion x3/
                 (D.CAT_ITEM, 11), (D.CAT_ITEM, 18), (D.CAT_ITEM, 11),       #   Tent x3/Cottage
                 (D.CAT_ITEM, 11), (D.CAT_ITEM, 2), (D.CAT_ITEM, 2),
                 (D.CAT_ITEM, 17), (D.CAT_ITEM, 17)],
                [(D.CAT_ITEM, 9), (D.CAT_ITEM, 30), (D.CAT_ITEM, 26),        #   rare: Phoenix Down x3/
                 (D.CAT_ITEM, 25), (D.CAT_ITEM, 24), (D.CAT_ITEM, 9),        #     Cockatrice Claw x2/
                 (D.CAT_ITEM, 9), (D.CAT_ITEM, 30)]),                        #     Blue/White/Red Curtain
        (2000,  [(D.CAT_ITEM, 4), (D.CAT_ITEM, 19), (D.CAT_ITEM, 9),         # Ether x4/Phoenix Down x3/
                 (D.CAT_ITEM, 12), (D.CAT_ITEM, 4), (D.CAT_ITEM, 25),        #   Spider's Silk x2/Gold Needle x2/
                 (D.CAT_ITEM, 26), (D.CAT_ITEM, 24), (D.CAT_ITEM, 4),        #   Emergency Exit x2/
                 (D.CAT_ITEM, 9), (D.CAT_ITEM, 4), (D.CAT_ITEM, 9),          #   White/Blue/Red Curtain
                 (D.CAT_ITEM, 12), (D.CAT_ITEM, 19), (D.CAT_ITEM, 15),
                 (D.CAT_ITEM, 15)],
                [FANGS, (D.CAT_ITEM, 32), (D.CAT_ITEM, 28)]),                #   rare: Fangs/Faerie Tonic/Hermes' Shoes
        (4000,  [(D.CAT_ITEM, 10), (D.CAT_ITEM, 28), (D.CAT_ITEM, 32),       # Remedy/Hermes' Shoes/Faerie Tonic/
                 CURTAINS, CURTAINS, FANGS],                                 #   Curtains x2/Fangs
                [TONICS, (D.CAT_ITEM, 5)]),                                  #   rare: Tonics/Turbo Ether (50%)
        (8000,  [(D.CAT_ITEM, 3), TONICS, (D.CAT_ITEM, 5), TONICS],          # X-Potion/Tonics x2/Turbo Ether
                [(D.CAT_ITEM, 23), (D.CAT_ITEM, 6)]),                        #   rare: Light Curtain/Dry Ether
        (10**9, [(D.CAT_ITEM, 3), (D.CAT_ITEM, 37), (D.CAT_ITEM, 6),         # Silver Apple x3/Dry Ether x2/
                 (D.CAT_ITEM, 37), (D.CAT_ITEM, 37), (D.CAT_ITEM, 6)],       #   X-Potion
                [(D.CAT_ITEM, 36), (D.CAT_ITEM, 7), (D.CAT_ITEM, 38),        #   rare: Golden Apple/Elixir x2/
                 (D.CAT_ITEM, 43), (D.CAT_ITEM, 38), (D.CAT_ITEM, 7),        #     Soma Drop x2/Luck Plus x2
                 (D.CAT_ITEM, 43)]),
    ]

    # --- Stealth Ninja Scroll (job_scroll_boosts): steal upgrades while a Ninja(7) is in
    # the party and the "Stealth Ninja Scroll" AP item is owned. All numbers are
    # The curated per-tier tables (user-set 2026-07-13). ONE categorical draw
    # decides super-rare vs rare vs common (see the roll in _thief_steal_loop).
    NINJA_SCROLL_AGI_MULT   = 1.1    # effective sumAGI multiplier for the AGI roll
    # The Luck tier falloff is NOT changed by the scroll (STEAL_LUCK_TIER_DECAY
    # applies scrolled or not).
    # Scrolled Ninja re-weights the loot table on a successful steal: instead of
    # the baseline 95% common / 5% rare, roll 5% super / 35% rare / 60% common
    # (one categorical draw; the two chances below must sum to <= 1, common =
    # remainder). Rare was pulled down from 80% on 2026-07-31 because the scroll
    # ALSO doubles the Luck rarity upgrade (STEAL_LUCK_UPGRADE_*) -- a scrolled
    # party promotes a large share of these commons into rares after the fact, so
    # 35% here still lands frequent rares without the table alone guaranteeing them.
    NINJA_SCROLL_SUPER_CHANCE = 0.05
    NINJA_SCROLL_RARE_CHANCE  = 0.35
    # Super-rare loot per battle-XP tier (index 0..5). A pick may be a (cat,id)
    # tuple or a category list (resolves to a random member). User-set 2026-07-13.
    # Reachable WITHOUT the scroll since 2026-07-31 via the Luck rarity upgrade
    # (rare -> super); the scroll only adds the 5% direct draw and doubles that
    # upgrade chance. Same table either way.
    PILLS = [(D.CAT_ITEM, 39), (D.CAT_ITEM, 40), (D.CAT_ITEM, 41),  # Power/Stamina/
             (D.CAT_ITEM, 42), (D.CAT_ITEM, 43)]                   #   Mind/Speed/Luck Plus
    STEAL_SUPER_POOLS = [
        [(D.CAT_ITEM, 9)],                        # t0: Phoenix Down
        [(D.CAT_ITEM, 3)],                        # t1: X-Potion
        [(D.CAT_ITEM, 7)],                        # t2: Elixir
        [(D.CAT_ITEM, 38)],                       # t3: Soma Drop
        [(D.CAT_ITEM, 36), (D.CAT_ITEM, 43),      # t4: Golden Apple 2/3, Luck Plus 1/3
         (D.CAT_ITEM, 36)],
        [PILLS, (D.CAT_ITEM, 42), PILLS,          # t5: random pill x4 + a Speed Plus
         PILLS, PILLS],                           #     slot (Speed 36% / others 16%)
    ]

    def _job_scrolls_on(self):
        """The job_scroll_boosts option (rides the on_disc slot dict -- the WW/BW
        legs are baked; the client loops key off the same flag)."""
        return bool((self.slot_data.get("on_disc") or {}).get("job_scroll_boosts"))

    def _scroll_owned(self, from_job):
        """Sticky ownership of a Job Scroll AP item (ids.job_item_id, keyed by
        BASE job: 1=Ninja, 2=Master, 3=RedWiz, 4=WhiteWiz, 5=BlackWiz). Boosts
        are additive-only (no strip risk), so the sticky _ever_won set is the
        right source -- it survives the disconnect blip that empties
        items_received. _synced() folds the latest snapshot in."""
        self._synced()
        return ID.job_item_id(from_job) in self._ever_won

    # --- received-scroll effect blurb (client log only) --------------------------
    # A Job Scroll grants nothing visible in-game (the boost is on-disc / a client
    # loop), so the log line is the ONLY place a player learns what they just won.
    # Text is the player-facing wording from options.JobScrollBoosts, trimmed of
    # yaml-config talk. Keyed by BASE job (0..5) like every other scroll table.
    SCROLL_EFFECT_MSG = {
        0: "Gain lifesteal and armor penetration with physical attacks.",
        2: "As they take damage, they gain attack and max HP, and heal.",
        3: "Restore mana based on damage taken, and heal based on mana spent.",
        4: "Dia spells hurt all bosses. Casting a Dia-family spell also heals "
           "the caster and grants temporary INT.",
        5: "Instant-kill spells are far more reliable, and spells deal damage "
           "instead of nothing when they miss. Kill deals a little more damage "
           "than Flare.",
    }
    # Ninja (from_job 1) is composed at grant time instead: the steal half only
    # applies with thief_steal on, and the floor half scales with the party's
    # Thief/Ninja count (NINJA_FLOOR_TIERS). The party layout is fixed for the
    # whole game -- only promotion changes a class (1 -> 7, both counted) -- so
    # the count read here stays true for the rest of the seed.
    NINJA_FLOOR_MSG = {
        1: "Damaging floors deal half damage.",
        2: "Damaging floors deal no damage.",
        3: "Damaging floors heal you each step.",
        4: "Damaging floors heal you and restore mana each step.",
    }
    NINJA_FLOOR_MSG_SLOTMAGIC = ("Damaging floors heal you and refill a spell "
                                 "charge each step.")
    # The scroll only enters the pool when the party HAS the job, so 0 should be
    # unreachable; kept as a non-lying fallback rather than an empty line.
    NINJA_FLOOR_MSG_NONE = ("Damaging floors deal less damage with a Thief or "
                            "Ninja in the party.")

    async def _ninja_scroll_msg(self):
        """Stealth Ninja blurb, composed from the seed's options and the live
        Thief/Ninja count. Returns the fallback wording if the party read fails
        (a grant can land before the save block is readable)."""
        parts = []
        if self.thief_steal:
            parts.append("Much better steals.")
        try:
            blk = await self.psp.read(self.sa(D.class_addr_sa(0)),
                                      2 + D.PARTY_COUNT * D.PARTY_STRIDE)
            ninjas = sum(1 for r in range(D.PARTY_COUNT)
                         if blk[r * D.PARTY_STRIDE] in (1, 7))
        except Exception:
            ninjas = 0
        if ninjas == 0:
            parts.append(self.NINJA_FLOOR_MSG_NONE)
        elif ninjas >= 4 and (self.slot_data.get("on_disc") or {}).get("slot_magic"):
            parts.append(self.NINJA_FLOOR_MSG_SLOTMAGIC)
        else:
            parts.append(self.NINJA_FLOOR_MSG[min(ninjas, 4)])
        return " ".join(parts)

    async def _log_scroll_effect(self, iid):
        """One '<name> Scroll: <what it does>' line per scroll, at grant time.
        Latched per id: the grant loop legitimately re-runs the tail after a
        death/load rollback, and the effect is not re-earned by that."""
        from_job = iid - ID.job_item_id(0)
        if from_job not in range(6) or from_job in self._scroll_msg_seen:
            return
        self._scroll_msg_seen.add(from_job)
        from . import class_names as CN
        name = f"{CN.CLASS_RENAME[from_job][1]} Scroll"
        msg = (await self._ninja_scroll_msg() if from_job == 1
               else self.SCROLL_EFFECT_MSG.get(from_job))
        if msg:
            logger.info(f"  [grant] {name}: {msg}")

    # --- class rename (job_scroll_boosts): custom class name per owned scroll ---
    _CLASSNAME_WINDOWS = ((0x09000000, 0x01000000),)   # heap region the bank lives in

    async def _classname_bank_base(self, CN):
        """Live base of the resident FM_CAMPUS class-name bank (padded on-disc to
        CN.SLOT bytes/entry). Anchored on the bank HEADER (00000000 'TEXT') and
        validated by entry0 at +0x40 being either the vanilla "Warrior" or one of
        OUR class-0 renames -- anchoring on the vanilla name alone was a live bug:
        the Warrior-scroll rename ("Blood Warrior") destroyed the anchor, so the
        bank could never be re-found and every scroll obtained after it silently
        stopped renaming (2026-07-14). Cached; rescans on a miss (the bank
        relocates on save-state load / heap churn, and is only resident while the
        menu is open)."""
        b = getattr(self, "_classname_base", None)
        if b is not None:
            try:
                hdr = await self.psp.read(b, CN.ENTRY0_OFF + CN.SLOT)
                if hdr[:len(CN.HEADER)] == CN.HEADER \
                        and CN.is_bank_entry0(hdr[CN.ENTRY0_OFF:]):
                    return b
            except Exception:
                pass
            self._classname_base = None
        for start, size in self._CLASSNAME_WINDOWS:
            try:
                buf = await self.psp.read_chunked(start, size)
            except Exception:
                continue
            j = buf.find(CN.HEADER)
            while j >= 0:
                e0 = j + CN.ENTRY0_OFF
                if CN.is_bank_entry0(buf[e0:e0 + CN.SLOT]):
                    self._classname_base = start + j
                    return self._classname_base
                j = buf.find(CN.HEADER, j + 1)
        return None

    # --- lute_tablets: "Lute Tablets N of M" line in the Key Items menu ------
    # Region the FM_EXTERN bundles load into (same heap the class-name bank uses).
    _KEYNAME_WINDOWS = ((0x09000000, 0x01000000),)
    _KEYNAME_HDR = b"\x00\x00\x00\x00TEXT"
    _KEYNAME_COUNT = 36                 # cat-0 key bank = the 36 key items

    async def _keyname_slot_addrs(self):
        """Per resident KEY_NAME bank copy (FM_EXTERN12/18 both load, and the
        menu may read either -- the probes had to write BOTH to see a change):
        (bank_base, entry0_addr, width, rune_addr_or_None, shard_addr_or_None).
        entry0 = the Lute slot (aliased by spare id 37, the tablet line);
        rune_addr = the entry of RUNE_MENU_SLOT_KEY_ID ("Battery Circuit",
        borrowed for the rune line); shard_addr = the entry of
        SHARD_MENU_SLOT_KEY_ID ("Energy Chip", borrowed for the Levistone
        Shards line). Each is present only when that slot is disc-padded (all
        are padded together by _build_bake, so None just means an older baked
        ISO).

        Located by TEXT header + an entry count of exactly 36, which uniquely
        identifies the key bank among the resident TEXT banks (weapons 67,
        armor 75, items 43/107, descs elsewhere) and, unlike anchoring on the
        entry-0 STRING, survives us overwriting that string -- the class-name
        bank hit exactly that bug (see _classname_bank_base). Entry addresses
        are read from the bank's own offset table rather than assumed; each
        padded slot's width (off[i+1]-off[i]) must be EXACTLY
        LUTE_TABLET_SLOT_GLYPHS+1 -- that width doubles as both the "is this
        the NAME bank, not the same-shaped DESC bank" test (live bug
        2026-07-24: a cnt==36 match alone wrote into the desc bank -> core-font
        garbage) and the "is this ISO padded?" gate. Cached; rescans on a
        validation miss (the bundles relocate on map change / save-state
        load).

        NOTE: _keydesc_slot_addr below is a near-clone -- same cache
        revalidation, same window sweep, same header decode -- diverging only
        in the entry-width test and the entry-end math. Keep the two in sync
        when touching either."""
        cached = getattr(self, "_keyname_slots", None)
        if cached:
            ok = True
            for base, _e0, _w, _rune, _shard in cached:
                try:
                    if await self.psp.read(base, len(self._KEYNAME_HDR)) \
                            != self._KEYNAME_HDR:
                        ok = False
                        break
                except Exception:
                    ok = False
                    break
            if ok:
                return cached
            self._keyname_slots = None
        pad_w = D.LUTE_TABLET_SLOT_GLYPHS + 1
        ri = D.RUNE_MENU_SLOT_KEY_ID - 1        # entry index of the rune slot
        si = D.SHARD_MENU_SLOT_KEY_ID - 1      # entry index of the shard slot
        found = []
        for start, size in self._KEYNAME_WINDOWS:
            try:
                buf = await self.psp.read_chunked(start, size)
            except Exception:
                continue
            j = buf.find(self._KEYNAME_HDR)
            while j >= 0:
                try:
                    cnt = int.from_bytes(buf[j + 8:j + 12], "little") >> 8
                    if cnt == self._KEYNAME_COUNT:
                        def off(i):
                            return int.from_bytes(
                                buf[j + 0x10 + i * 4:j + 0x14 + i * 4], "little")
                        # The offset table holds EXACTLY cnt entries with NO
                        # trailing sentinel (extern_bake._author_key_bank), so
                        # the LAST entry's end is the bank's total-size header
                        # word at +0xC -- off(cnt) would read the first 4 bytes
                        # of entry-0 string data as a bogus offset. That is not
                        # academic: the shard slot IS the last entry (id 36),
                        # so an off(si+1) width test failed every time and the
                        # shard line never drew on a correctly padded ISO
                        # (live 2026-08-12).
                        total = int.from_bytes(buf[j + 0xC:j + 0x10], "little")

                        def width(i):
                            return (off(i + 1) if i + 1 < cnt else total) - off(i)
                        if width(0) == pad_w:
                            rune = (start + j + off(ri)
                                    if width(ri) == pad_w else None)
                            shard = (start + j + off(si)
                                     if width(si) == pad_w else None)
                            found.append((start + j, start + j + off(0),
                                          pad_w, rune, shard))
                except Exception:
                    pass
                j = buf.find(self._KEYNAME_HDR, j + 1)
        self._keyname_slots = found
        return found

    async def _keydesc_slot_addr(self):
        """Address+width of the KEY_EXP (description) entry for the borrowed
        rune slot, per resident copy: [(addr, width), ...].

        The description bank is the OTHER 36-entry TEXT bank -- same shape as
        KEY_NAME, which is exactly how a cnt==36 match once wrote a ratio into
        it and rendered a description as core-font garbage (2026-07-24). It is
        told apart the same way _keyname_slot_addrs identifies the name bank,
        by entry 0's width: the name bank's entry 0 is padded to exactly
        LUTE_TABLET_SLOT_GLYPHS+1, and nothing else is.

        Width comes from the bank's own offset table, so whatever patch_iso
        baked into this entry sets the ceiling for what we may write here --
        we only ever overwrite IN PLACE (space-padded), never re-lay."""
        cached = getattr(self, "_keydesc_slots", None)
        if cached:
            ok = True
            for base, _a, _w in cached:
                try:
                    if await self.psp.read(base, len(self._KEYNAME_HDR)) \
                            != self._KEYNAME_HDR:
                        ok = False
                        break
                except Exception:
                    ok = False
                    break
            if ok:
                return [(a, w) for _b, a, w in cached]
            self._keydesc_slots = None
        elif cached == []:
            # An EMPTY result must never stick. Unlike the always-resident
            # FM_EXTERN name banks, this bank rides FM_CAMPUS, which is not
            # loaded until the menu opens -- so the first scan of a session
            # legitimately finds nothing. Caching that froze the description
            # for the whole session (live bug 2026-07-27: the name flipped to
            # "Rune Key" but the text stayed on the locked message). Retry,
            # but not every tick: the scan reads a 16 MB window.
            n = getattr(self, "_keydesc_miss", 0) + 1
            self._keydesc_miss = n
            if n % 20:
                return []
        pad_w = D.LUTE_TABLET_SLOT_GLYPHS + 1
        ri = D.RUNE_MENU_SLOT_KEY_ID - 1
        found = []
        for start, size in self._KEYNAME_WINDOWS:
            try:
                buf = await self.psp.read_chunked(start, size)
            except Exception:
                continue
            j = buf.find(self._KEYNAME_HDR)
            while j >= 0:
                try:
                    cnt = int.from_bytes(buf[j + 8:j + 12], "little") >> 8
                    if cnt == self._KEYNAME_COUNT:
                        def off(i):
                            return int.from_bytes(
                                buf[j + 0x10 + i * 4:j + 0x14 + i * 4], "little")
                        total = int.from_bytes(buf[j + 0xC:j + 0x10], "little")
                        if off(1) - off(0) != pad_w:      # NOT the name bank
                            # entry end = next HIGHER offset (identical entries
                            # share one body copy, so offsets are not sorted)
                            o = off(ri)
                            higher = [off(k) for k in range(cnt) if off(k) > o]
                            end = min(higher) if higher else total
                            if end > o:
                                found.append((start + j, start + j + o, end - o))
                except Exception:
                    pass
                j = buf.find(self._KEYNAME_HDR, j + 1)
        self._keydesc_slots = found
        if found:
            self._keydesc_miss = 0
        return [(a, w) for _b, a, w in found]

    # Key Items column clips past ~18 glyphs (live 2026-07-24: a 20-glyph label
    # ran off-screen, and the RIGHT column clips even earlier: 18 fell off live
    # 2026-07-27). Each line is a DEDICATED entry (user call -- no sharing):
    #   id 37 (spare, aliases entry 0 "Lute")        -> "Lute Tabs N of M"
    #   id 35 ("Battery Circuit", display-borrowed)  -> "Runes N of M" (max 14)
    # Counts are DISPLAY-capped at 99 so a wide yaml can't blow the budget;
    # assembly always gates on the TRUE counts, never these.
    # NOTE the menu font has NO '/' glyph (silently DROPPED -- 2/10 renders
    # "210"); only letters, digits, '-' and space are safe here.

    async def _keyratio_loop(self):
        """Live progress lines in the Key Items menu (lute_tablets and/or
        equipment_runes seeds).

        LUTE line: the spare id-37 possession bit makes a gate-inert entry
        appear whose cat-0 array slot aliases KEY_NAME entry 0 (the Lute's
        string); restore "Lute" + clear the bit at assembly.

        RUNE line: there is no second spare id (38 fails the getter bound AND
        its array slot lies inside the cat-1 array), so we BORROW key id 35
        "Battery Circuit" -- a Whisperwind robot part -- for DISPLAY only (the
        activation gate is story flag 62, owned by _npc_loop). The borrow is
        guarded, but SCOPED: only Whisperwind Cove can grant that part, so only
        Whisperwind releases it (_rune_borrow_zone). Earthgift, Hellfire and
        Lifespring keep the line -- which matters, because those are places
        runes are found and the gate is what the player is watching.
          * inside Whisperwind the slot is un-hijacked -- bit cleared, native
            name + description restored -- so the minigame sees the truth;
          * ownership is PROVEN, not guessed: RUNE_BORROW_OWNED_* is a
            save-persistent shadow flag we stamp while we hold the bit, so a
            natively-earned part is never stolen (sticky back-off, logged once)
            and our own borrow is never abandoned across a restart;
          * with the borrow released -- or on an unpadded ISO -- the count still
            lives on the Tracker strip and in this log, so it is never invisible.

        Writes IN PLACE within the on-disc-padded slots and NEVER touches the
        offset table -- a malformed table freezes the menu (class-name lesson),
        and the resident buffer has no slack to grow anyway. Re-applied every
        tick because the menu re-inits the bank from the resource on open."""
        self._keyname_slots = None
        self._rune_slot_we_set = False    # WE set the id-35 bit (this session)
        self._rune_slot_native = False    # sticky: real Battery Circuit owned
        self._rune_slot_prev_want = None  # last tick's want (None = no tick yet)
        self._rune_hidden_logged = False  # one-shot "hidden here, you're at N of M"
        self._shard_slot_we_set = False   # WE set the id-36 bit (this session)
        self._shard_slot_native = False   # sticky: real Energy Chip owned
        self._shard_slot_prev_want = None # last tick's want (None = no tick yet)
        self._shard_hidden_logged = False # one-shot "hidden here" (Whisperwind)

        async def show(slots, which, label, cap):
            # which: 1 = lute (entry 0), 3 = rune entry, 4 = shard entry --
            # indexes the 5-tuples _keyname_slot_addrs returns
            # (base, entry0_addr, width, rune_addr, shard_addr); an address of
            # None = that slot not padded on this ISO.
            # tup[2] is the slot byte width; min(tup[2]-1, cap) reserves the
            # terminator byte.
            for tup in slots:
                addr = tup[which]
                if addr is None:
                    continue
                body = NB.key_menu_encode(label, min(tup[2] - 1, cap))
                if await self.psp.read(addr, len(body)) != body:
                    await self.psp.write(addr, body)

        async def set_bit(addr_raw, mask, want):
            cur = (await self.psp.read(self.sa(addr_raw), 1))[0]
            have = bool(cur & mask)
            if have != want:
                await self.psp.write(self.sa(addr_raw),
                                     bytes([(cur & ~mask) | (mask if want else 0)]))
            return have

        async def tick():
            lute_need = self.lute_tablets_required
            rune_need = self.equipment_runes_required
            shard_need = self.levistone_shards_required
            if ((not lute_need and not rune_need and not shard_need)
                    or self.save_delta is None):
                return
            # Locate FIRST: a shown entry without a padded slot to write into
            # would render its NATIVE name -- a fake "Lute" / "Battery Circuit"
            # the player must not appear to have. No padded slots (bake failed /
            # pre-pad ISO) => leave everything hidden.
            slots = await self._keyname_slot_addrs()
            if not slots:
                if not getattr(self, "_lute_slot_warned", False):
                    self._lute_slot_warned = True
                    logger.info("  [keyratio] no padded KEY_NAME slot found -- "
                                "progress lines disabled (unpatched ISO?); the "
                                "Lute / Equipment Rune / Levistone gates are "
                                "unaffected")
                return

            # ---- Lute Tablets line (id 37 -> entry 0) -----------------------
            if lute_need:
                count = min(self._tablet_count(), lute_need)
                assembled = count >= lute_need
                was = await set_bit(D.LUTE_TABLET_SLOT_ADDR,
                                    D.LUTE_TABLET_SLOT_MASK, not assembled)
                if was == assembled:
                    logger.info(f"  [keyratio] lute line "
                                f"{'cleared (assembled)' if assembled else 'shown'}")
                if assembled:
                    label = "Lute"
                else:
                    dc, dn = min(count, 99), min(lute_need, 99)
                    label = f"Lute Tabs {dc} of {dn}"
                    if len(label) > 18:
                        label = f"Tabs {dc} of {dn}"
                await show(slots, 1, label, D.LUTE_TABLET_SLOT_GLYPHS)

            # ---- Levistone Shards line (borrowed id 36) ---------------------
            # Same borrow pattern as the rune line below (id 36 "Energy Chip"
            # is another Whisperwind robot part, so _rune_borrow_zone and every
            # safety argument are shared), with two simplifications: no
            # legacy-save adoption heuristic (this feature ships WITH its
            # shadow flag, so bit-set + shadow-clear is ALWAYS the natively
            # earned part), and the borrow is released FOR GOOD at assembly --
            # the real Levistone entry (id 11, possession set by the map-reset
            # row) takes over, no duplicate line. The borrowed slot keeps its
            # native Energy Chip DESCRIPTION throughout (only the rune slot's
            # desc is bake-authored); cosmetic, revisit if it bothers anyone.
            # Structured with no returns so the rune section below always runs.
            if (shard_need and not self._shard_slot_native
                    and any(t[4] for t in slots)):
                count = min(self._shard_count(), shard_need)
                assembled = count >= shard_need
                zone = await self._rune_borrow_zone()
                if zone is not None:
                    want = not zone and not assembled
                    have = bool((await self.psp.read(
                        self.sa(D.SHARD_MENU_SLOT_ADDR), 1))[0]
                        & D.SHARD_MENU_SLOT_MASK)
                    shadow = bool((await self.psp.read(
                        self.sa(D.SHARD_BORROW_OWNED_ADDR), 1))[0]
                        & D.SHARD_BORROW_OWNED_MASK)
                    prev = self._shard_slot_prev_want
                    if have and not self._shard_slot_we_set:
                        if shadow and prev is not False:
                            self._shard_slot_we_set = True
                        else:
                            self._shard_slot_native = True
                            await show(slots, 4, "Energy Chip",
                                       D.LUTE_TABLET_SLOT_GLYPHS)
                            logger.info(
                                "  [keyratio] the Energy Chip possession bit "
                                "is not ours -> shard progress line disabled "
                                "(it borrows that menu slot); the Tracker tab "
                                "and this log carry the count")
                    if not self._shard_slot_native:
                        # Ordered exactly like the rune pair: shadow FIRST on
                        # the way out, so bit-set + shadow-clear is unreachable
                        # in the release zone; both orders fail safe.
                        if want:
                            await set_bit(D.SHARD_MENU_SLOT_ADDR,
                                          D.SHARD_MENU_SLOT_MASK, True)
                            await set_bit(D.SHARD_BORROW_OWNED_ADDR,
                                          D.SHARD_BORROW_OWNED_MASK, True)
                        else:
                            await set_bit(D.SHARD_BORROW_OWNED_ADDR,
                                          D.SHARD_BORROW_OWNED_MASK, False)
                            await set_bit(D.SHARD_MENU_SLOT_ADDR,
                                          D.SHARD_MENU_SLOT_MASK, False)
                        self._shard_slot_we_set = want
                        self._shard_slot_prev_want = want
                        if want:
                            self._shard_hidden_logged = False
                            # Always this exact string -- no shortened fallback
                            # rung. LevistoneShardsRequired is capped at 9 for
                            # precisely this reason (user 2026-08-12: a vaguer
                            # "Shards N of M" is worse than a tighter cap), so
                            # both numbers are single-digit and the label is
                            # 18 glyphs, the width the menu column takes
                            # without clipping. Raising that cap means
                            # re-solving this line, not adding a rung here.
                            await show(slots, 4,
                                       f"Levi Shards {count} of {shard_need}",
                                       D.LUTE_TABLET_SLOT_GLYPHS)
                            if not have:
                                logger.info(f"  [keyratio] shard line shown "
                                            f"({count} of {shard_need})")
                        else:
                            # Restore the native name so the minigame -- or a
                            # later native grant -- reads correctly. Behind
                            # `have` like the rune restore (write once).
                            if have:
                                await show(slots, 4, "Energy Chip",
                                           D.LUTE_TABLET_SLOT_GLYPHS)
                                logger.info(
                                    "  [keyratio] shard line released "
                                    + ("(Levistone assembled -- its own menu "
                                       "entry takes over)" if assembled
                                       else "in Whisperwind Cove"))
                            if (zone and not assembled
                                    and not self._shard_hidden_logged):
                                self._shard_hidden_logged = True
                                logger.info(
                                    "  [keyratio] shard line hidden in "
                                    "Whisperwind Cove (it borrows the Energy "
                                    "Chip key slot) -- you are at "
                                    f"{count} of {shard_need}; the Tracker "
                                    f"tab shows it live")

            # ---- Equipment Runes line (borrowed id 35) ----------------------
            if not rune_need or self._rune_slot_native:
                return
            if not any(t[3] for t in slots):
                return                     # ISO pre-dates the rune-slot pad
            count = min(self._rune_count(), rune_need)
            assembled = count >= rune_need
            # Release the borrow ONLY where the real part can be granted, i.e.
            # Whisperwind Cove. None = the map state is not knowable yet; make
            # no borrow writes at all rather than guess (guessing "safe" on a
            # cold latch would stamp a fake bit inside Whisperwind itself).
            zone = await self._rune_borrow_zone()
            if zone is None:
                return
            # The slot stays SHOWN after assembly -- it becomes the permanent
            # "Rune Key" item (user 2026-07-27: the line vanishing at the
            # threshold read as losing something).
            want = not zone
            # READ first, decide, THEN write: whose bit is this?
            have = bool((await self.psp.read(
                self.sa(D.RUNE_MENU_SLOT_ADDR), 1))[0] & D.RUNE_MENU_SLOT_MASK)
            shadow = bool((await self.psp.read(
                self.sa(D.RUNE_BORROW_OWNED_ADDR), 1))[0]
                & D.RUNE_BORROW_OWNED_MASK)
            prev = self._rune_slot_prev_want
            if have and not self._rune_slot_we_set:
                # A set bit we did not set THIS session. The shadow flag settles
                # it: we stamp it only while holding the bit, and we clear it
                # BEFORE the bit on release, so "bit set + shadow clear" is
                # never our own leftover -- it is the earned part. prev is False
                # means we cleared the bit last tick, so its return is native
                # regardless of what the shadow says.
                ours = shadow and prev is not False
                if not ours and prev is None and not shadow and not zone:
                    # LEGACY save (written before the shadow existed): ambiguous
                    # by construction. Adopt only if the player holds NO other
                    # bonus-dungeon key item -- anyone who got far enough into
                    # the minigame to earn Battery Circuit holds its siblings.
                    ours = not await self._other_bonus_keys_held()
                if not ours:
                    self._rune_slot_native = True
                    await show(slots, 3, "Battery Circuit",
                               D.LUTE_TABLET_SLOT_GLYPHS)
                    await self._write_keydesc(D.RUNE_SLOT_NATIVE_DESC)
                    logger.info("  [keyratio] the Battery Circuit possession bit "
                                "is not ours -> rune progress line disabled (it "
                                "borrows that menu slot); the Tracker tab and "
                                "this log carry the count")
                    return
                self._rune_slot_we_set = True
            # Ordered, and re-asserted every tick (set_bit is a no-op when the
            # byte already agrees, so this also heals a lost write). Both orders
            # fail SAFE: a crash mid-pair leaves bit-set + shadow-clear, which
            # reads as native and is then never touched.
            if want:
                await set_bit(D.RUNE_MENU_SLOT_ADDR,
                              D.RUNE_MENU_SLOT_MASK, True)
                await set_bit(D.RUNE_BORROW_OWNED_ADDR,
                              D.RUNE_BORROW_OWNED_MASK, True)
            else:
                # Shadow FIRST on the way out: that makes "shadow set while the
                # bit is clear" unreachable inside the release zone, so a native
                # grant landing there can never be misread as ours.
                await set_bit(D.RUNE_BORROW_OWNED_ADDR,
                              D.RUNE_BORROW_OWNED_MASK, False)
                await set_bit(D.RUNE_MENU_SLOT_ADDR,
                              D.RUNE_MENU_SLOT_MASK, False)
            self._rune_slot_we_set = want
            self._rune_slot_prev_want = want
            if want:
                self._rune_hidden_logged = False
                if assembled:
                    name, desc = D.RUNE_KEY_NAME, D.RUNE_KEY_DESC
                else:
                    dc, dn = min(count, 99), min(rune_need, 99)
                    name, desc = f"Runes {dc} of {dn}", None
                await show(slots, 3, name, D.LUTE_TABLET_SLOT_GLYPHS)
                # Description: the LOCKED text is what patch_iso baked, so only
                # the assembled state needs a runtime rewrite. Written in place,
                # space-padded to the baked slot -- never re-laid (a malformed
                # offset table freezes the menu).
                await self._write_keydesc(desc)
                if not have:
                    logger.info("  [keyratio] rune line shown "
                                + ("(Rune Key -- assembled)" if assembled
                                   else f"({count} of {rune_need})"))
            else:
                # Restore the native name AND description so the minigame -- or
                # a later native grant -- reads correctly. Kept behind `have`:
                # _write_keydesc's miss path sweeps a 16 MB window, and this
                # branch runs for a whole 40-floor stay.
                if have:
                    await show(slots, 3, "Battery Circuit",
                               D.LUTE_TABLET_SLOT_GLYPHS)
                    await self._write_keydesc(D.RUNE_SLOT_NATIVE_DESC)
                if not self._rune_hidden_logged:
                    # Says it once per entry, INCLUDING a session resumed inside
                    # the dungeon (where `have` starts clear and the restore
                    # above never runs) -- that silence is what made this look
                    # like a bug live 2026-08-06.
                    self._rune_hidden_logged = True
                    logger.info(
                        "  [keyratio] rune line hidden in Whisperwind Cove (it "
                        "borrows the Battery Circuit key slot) -- you are at "
                        f"{count} of {rune_need}; the Tracker tab shows it live")

        await self._poll(0.5, "keyratio", tick)

    async def _write_keydesc(self, text):
        """Overwrite the borrowed rune slot's KEY_EXP description in place.
        `text` None = leave whatever patch_iso baked (the locked message).

        The desc bank uses its OWN font (campus_bake.KEYDESC_ENC, TERM 0x05) --
        neither the menu font nor the msg font. Encoding is best-effort here:
        an unmappable character must never take down the client mid-session, so
        it is dropped, and the whole write is skipped if the text does not fit
        the baked slot (patch_iso sized that slot from the longest string it
        baked, so every string we write here is shorter by construction).

        DISABLED 2026-08-07 pending RE. campus_bake.author_key_desc ALIASES
        byte-identical entries onto ONE shared body (its re-lay loop notes that
        vanilla already shares Lute's text with Ocarina's), so the entry located
        here is not necessarily ours alone: an in-place write can rewrite
        several key items' descriptions at once, and the space padding runs to
        the next HIGHER offset rather than to the end of a body we own. That is
        the leading candidate for the corrupted KEY_EXP bank behind the live
        freeze on the Carobo row and the truncated Canoe text ("Small boat"
        where the bank holds "Small boat for crossing lakes and rivers.").
        The NAME write is what the minigame and the player actually need; the
        description now stays on whatever patch_iso baked."""
        return
        if text is None:
            return
        try:
            from . import campus_bake as CBK
        except ImportError:
            return
        body = bytes(g for g in (CBK.KEYDESC_ENC.get(c) for c in text)
                     if g is not None)
        body += bytes([0x05])                       # TERM
        for addr, width in await self._keydesc_slot_addr():
            if len(body) > width:
                continue                            # never overrun the slot
            # Pad with the space glyph: if the renderer is offset-bounded
            # rather than TERM-bounded, stale tail glyphs would show through.
            out = body + bytes([CBK.KEYDESC_ENC.get(" ", 0)]) * (width - len(body))
            if await self.psp.read(addr, width) != out:
                await self.psp.write(addr, out)

    async def _classname_loop(self):
        """Class rename: when a job's scroll is owned, overwrite that job's base +
        promoted entries in the class-name bank with CN.CLASS_RENAME names. The
        on-disc campus_bake padded every entry to CN.SLOT bytes so names aren't
        length-capped; we write IN PLACE within the padded slot (never touch the
        offset table -> a malformed table freezes the menu). Re-applied each tick
        because the menu re-inits the bank from the resource on open."""
        from . import class_names as CN
        self._classname_base = None

        async def tick():
            if not self.slot_data or not self._job_scrolls_on():
                return
            want = {}
            for fj, (base_nm, promo_nm) in CN.CLASS_RENAME.items():
                if not self._scroll_owned(fj):
                    continue
                if CN.encodable(base_nm):
                    want[fj] = CN.encode_slot(base_nm)
                if CN.encodable(promo_nm):
                    want[fj + 6] = CN.encode_slot(promo_nm)
            if not want:
                return
            base = await self._classname_bank_base(CN)
            if base is None:
                return
            for ei, body in want.items():
                addr = base + CN.ENTRY0_OFF + ei * CN.SLOT
                if await self.psp.read(addr, CN.SLOT) != body:
                    await self.psp.write(addr, body)

        await self._poll(0.5, "classname", tick)

    async def _jobsprite_loop(self):
        """Scroll-gated custom party sprites (battle + pause menu).

        Pins job_sprites art over the resident JOBxx.GIM copies for every
        class whose scroll is owned. The engine restores vanilla bytes from
        disc constantly (menu open, field streaming) and buffers move between
        loads, so this is a PIN, not a one-shot: a cheap 64-byte signature
        read per known sheet per tick, a rewrite only when vanilla bytes
        reappear, and a full GIM-magic rescan when addresses die or on the
        slow cadence (new copies can appear at fresh addresses, e.g. the
        pause menu loading JOB_ALL). Field walking sprites are a different,
        load-baked pipeline this loop deliberately does not touch -- see
        job_sprites.py's module docstring."""
        from . import job_sprites as JS
        known = {}                       # addr -> cls (main sheets only)
        tick_n = 0
        RESCAN_TICKS = 12                # * 0.25s = every ~3s
        SCAN_LO, SCAN_HI, SCAN_CHUNK = 0x08800000, 0x0A000000, 0x100000
        # Largest in-sheet offset this loop ever touches. EVERY read/write must
        # stay inside [SCAN_LO, SCAN_HI): an out-of-window request makes
        # PPSSPPMem raise ValueError, which HybridPSP treats as "VRAM -> WS can
        # serve" and dials the WebSocket debugger -- a 1MB WS read parks this
        # tick for minutes and the pin silently stops (live 2026-08-12: battle
        # loaded a vanilla sheet and nothing re-pinned it for 4+ minutes).
        SHEET_SPAN = JS.PIX_OFF + JS.PIX_LEN

        async def apply(addr, pal, pix):
            # pixels first: the repaint keys on pixel bytes changing; a
            # palette-only write is invisible (live-proven 2026-08-12)
            await self.psp.write(addr + JS.PIX_OFF, pix)
            await self.psp.write(addr + JS.PAL_OFF, pal)

        async def sheet_sig(addr):
            """Signature window of the sheet at addr, b"" on any failure.
            Bounds-checked so a sheet near the top of RAM can never trigger
            the ValueError->WS-fallback wedge."""
            if addr + SHEET_SPAN > SCAN_HI:
                return b""
            try:
                return await self.psp.read(
                    addr + JS.PIX_OFF + JS.SIG_OFF, JS.SIG_LEN)
            except Exception:
                return b""

        async def tick():
            nonlocal tick_n
            tick_n += 1
            if not self.slot_data or not self._job_scrolls_on():
                return
            owned = [fj for fj in range(6) if self._scroll_owned(fj)]
            targets = JS.targets_for(owned)
            if not targets:
                return
            # cheap pass: re-assert art at known addresses
            lost = False
            for addr, cls in list(known.items()):
                if cls not in targets:
                    del known[addr]
                    continue
                sig = await sheet_sig(addr)
                if sig == JS.VANILLA_SIG[cls]:
                    await apply(addr, *targets[cls])
                elif sig != JS.custom_sig(cls):
                    del known[addr]      # freed or repurposed -> rescan finds it
                    lost = True
            if known and not lost and tick_n % RESCAN_TICKS:
                return
            # rescan: find every resident sheet of a target class
            want = {JS.VANILLA_SIG[c]: c for c in targets}
            done = {JS.custom_sig(c): c for c in targets}
            addr = SCAN_LO
            while addr < SCAN_HI:
                # overlap so a magic string straddling a chunk edge still hits,
                # but NEVER read past SCAN_HI (the WS-fallback wedge above)
                size = min(SCAN_CHUNK + len(JS.GIM_MAGIC), SCAN_HI - addr)
                try:
                    blob = await self.psp.read(addr, size)
                except Exception:
                    blob = b""
                pos = blob.find(JS.GIM_MAGIC)
                while pos != -1:
                    base = addr + pos
                    sig = await sheet_sig(base)
                    cls = want.get(sig)
                    if cls is not None and base + SHEET_SPAN <= SCAN_HI:
                        await apply(base, *targets[cls])
                        known[base] = cls
                    elif sig in done:
                        known[base] = done[sig]
                    pos = blob.find(JS.GIM_MAGIC, pos + 1)
                addr += SCAN_CHUNK
                await asyncio.sleep(0)   # yield between 1MB chunks

        await self._poll(0.25, "jobsprite", tick)

    async def _in_battle(self):
        """True iff a battle is ACTIVE right now.

        Reads D.BATTLE_ACTIVE_FLAG_SA (u8, 1=battle/0=field), which is cleared on
        battle exit. Do NOT use "*(BATTLE_ACTOR_OBJ_PTR_SA) is in RAM range" for
        this: that pointer LATCHES the last battle_base forever (never zeroed on
        exit), so it reads "in battle" permanently after the first fight and
        silently kills every loop that gates on it. Fail safe = False (not in
        battle) so a bad/early read never wedges a loop shut. Caller must have
        resolved save_delta (uses sa())."""
        try:
            return (await self.psp.read(self.sa(D.BATTLE_ACTIVE_FLAG_SA), 1))[0] == 1
        except Exception:
            return False

    # --- thief-steal custom victory box -------------------------------------
    # The native victory-drop routine announces our stolen item as "Obtained
    # <item>." from BATTLE_MSG.MSG entry 14 ("Obtained {NAME}.", {NAME}=icon+name
    # token 0x46). We overwrite entry 14 with a thief-flavored line JUST for the
    # stolen item, then restore it when the battle ends (so a boss's real drop in
    # a later fight still reads "Obtained <item>."). Uses the cracked battle font
    # (battle_font); see battle-message-box-re memory. The message spans entry 14 +
    # entry 15 ("The party was defeated.", only shown on a total-party-kill, which
    # never coincides with a winning steal) via an offset-table bump, chest-box
    # style -- restored on battle exit.
    _STEAL_BOX_SIG = bytes([0x26, 0x1e, 0x09, 0x02, 0x05, 0x06, 0x01, 0x0c])  # "Obtained"
    _STEAL_BOX_SUFFIX = "!"
    # The line names the thief's ACTUAL class, including the scroll rename, so it
    # tracks promotion and the Stealth Ninja Scroll: Thief / Ninja / Stealth Thief
    # / Stealth Ninja (the scrolled names come from class_names.CLASS_RENAME so
    # there is one source of truth with the status-menu rename).
    #
    # VERB IS LOAD-BEARING: entry 14 + entry 15 give exactly 36 bytes, and
    # "Your Stealth Ninja stole an extra {NAME}!" is 37 -- one over. "took" lands
    # on exactly 36. If you change it, keep every variant <= 36 or the author
    # silently no-ops and the box falls back to the native "Obtained {NAME}."
    _STEAL_BOX_VERB = "took"

    def _steal_box_prefix(self, classes):
        """"Your <class> took an extra " for the party's best thief."""
        from . import class_names as CN
        ninja = 7 in classes                       # promoted beats base
        base = "Ninja" if ninja else "Thief"
        if self._job_scrolls_on() and self._scroll_owned(1):
            base = CN.CLASS_RENAME[1][1 if ninja else 0]
        return f"Your {base} {self._STEAL_BOX_VERB} an extra "

    async def _find_battlemsg_bank(self):
        """Locate the resident BATTLE_MSG.MSG TEXT bank (16 entries) and cache it."""
        if self._battlemsg_bank is not None:
            return self._battlemsg_bank
        # Sweep for _STEAL_BOX_SIG, an 8-byte glyph signature from the bank's
        # text payload. Each hit reads 0x420 bytes from hit-0x400: the TEXT
        # container header precedes the glyph payload by at most 0x400.
        # Accept filter: a real BATTLE_MSG bank has exactly 16 entries and
        # total 0x164 -- lookalike hits fail one of the two.
        for off in range(0, USER_RAM_SIZE, 0x100000):
            buf = await self.psp.read_chunked(
                USER_RAM_BASE + off, min(0x100000, USER_RAM_SIZE - off))
            j = buf.find(self._STEAL_BOX_SIG)
            while j >= 0:
                h = USER_RAM_BASE + off + j
                cbuf = await self.psp.read(h - 0x400, 0x420)
                t = cbuf.rfind(b"TEXT", 0, 0x400)   # header sits in the first 0x400
                if t >= 4 and cbuf[t - 4:t] == b"\0\0\0\0":
                    base = (h - 0x400) + t - 4
                    hdr = await self.psp.read(base, 0x14)
                    cnt = struct.unpack_from("<I", hdr, 8)[0] >> 8
                    tot = struct.unpack_from("<I", hdr, 0xC)[0]
                    if cnt == 16 and tot == 0x164:
                        self._battlemsg_bank = base
                        return base
                j = buf.find(self._STEAL_BOX_SIG, j + 1)
        return None

    async def _steal_box_author(self, prefix):
        """Overwrite BATTLE_MSG entry 14 with the thief-flavored steal line so the
        native victory-drop announcement shows it (item name+icon via 0x46). Saves
        the original bytes for restore. No-op (leaves native text) on any failure.

        The bank is a loaded asset bundle whose address moves not just per boot
        but WITHIN a session (heap reshuffle on area reload -- live log
        2026-07-21 showed the cached address going 'Invalid address' mid-run
        while steals kept firing). So on any read failure we drop the cache and
        rescan once before giving up on this battle."""
        for attempt in range(2):
            try:
                await self._steal_box_author_once(prefix)
                return
            except Exception as e:
                self._battlemsg_bank = None      # stale cache -> rescan
                if attempt:
                    logger.info(f"  [steal-box] author skipped: {e!r}")

    async def _steal_box_author_once(self, prefix):
        base = await self._find_battlemsg_bank()
        if base is None:
            return
        hdr = await self.psp.read(base, 0x60)
        off14, off15, tot = (struct.unpack_from("<I", hdr, 0x10 + 14 * 4)[0],
                             struct.unpack_from("<I", hdr, 0x10 + 15 * 4)[0],
                             struct.unpack_from("<I", hdr, 0xC)[0])
        if not (off14 < off15 <= tot):
            raise RuntimeError(f"bank @{base:#x} header implausible "
                               f"(off14={off14:#x} off15={off15:#x} tot={tot:#x})")
        msg = BFONT.encode_with_name(prefix, self._STEAL_BOX_SUFFIX)
        span = tot - off14                      # entry14 spans through entry15
        if len(msg) > span:
            return                              # too long even combined; skip
        orig_off15 = await self.psp.read(base + 0x10 + 15 * 4, 4)
        orig_span = await self.psp.read(base + off14, span)
        self._steal_box_restore = (base, orig_off15, orig_span, off14)
        # bump entry15's offset to end-of-bank so entry14 owns [off14, tot)
        # -- the renderer takes entry 14's span as off15-off14, so it now
        # draws 14's bytes straight through 15's slot (15 is sacrificed while
        # the steal box is shown; _steal_box_restore_now puts both back).
        await self.psp.write(base + 0x10 + 15 * 4, struct.pack("<I", tot))
        sp = BFONT.BATTLE_ENC[" "]
        # msg[:-1] strips the encoder's own terminator so space padding +
        # TERM can be appended at the exact span end.
        body = msg[:-1] + bytes([sp]) * (span - len(msg)) + bytes([BFONT.TERM])
        await self.psp.write(base + off14, body[:span])

    async def _steal_box_restore_now(self):
        """Undo _steal_box_author (restore native 'Obtained {NAME}.' entry 14/15)."""
        if not self._steal_box_restore:
            return
        base, orig_off15, orig_span, off14 = self._steal_box_restore
        self._steal_box_restore = None
        try:
            await self.psp.write(base + 0x10 + 15 * 4, orig_off15)
            await self.psp.write(base + off14, orig_span)
        except Exception:
            pass

    # --- slot_magic level-up line ("MP increased by N." -> slots wording) ----
    # Under slot_magic the level-up message's {N} is already the SLOT count
    # (iso_patcher v172: statIdx-1 jump-table cave) and the line self-hides at
    # 0 -- only the wording is wrong. BATTLE_MSG.MSG entry 8 is 19 bytes and
    # "{N} new spell slots." encodes to EXACTLY 19 (token 0x47 + 17 glyphs +
    # term), so this is a pure in-place rewrite: no offset-table edit, no
    # collision with the steal box's entry-14/15 borrow. The bank moves per
    # boot AND mid-session (heap reshuffle -- see _steal_box_author), so a slow
    # poll re-authors whenever the resident copy reverts or relocates. A
    # literal "Gained 1 Level 4 spell slot." is NOT renderable: the battle
    # font has no digit glyphs (numbers exist only via the one {N} token) and
    # one char level can grant slots at several spell levels at once.
    # v192 NUMBER LAST (user 2026-07-31): putting {N} at the END sidesteps the
    # plural problem entirely -- the noun no longer has to agree with N, so no
    # runtime singular/plural swap is needed (the cave-side SMSG tail rewrite
    # built earlier the same day was dropped with it). The {N} token may sit
    # ANYWHERE in the string, not only at the front.
    # HARD CAP 19 BYTES = entry 8's span (off9 - off8). The requested
    # "New spell slots gained... {N}" encodes to 28 and does NOT fit; going
    # longer means pushing entry 9's offset (the steal-box entry-14/15 trick)
    # and thereby DESTROYING entry 9, which is another live level-up stat line
    # -- not worth it. "New spell slots {N}" is 18 (16 glyphs + token + term);
    # the spare byte sits past the terminator and is never read, so there is
    # still no offset-table edit.
    # Trailing "!" (user 2026-07-31) puts the body at EXACTLY 19 bytes = the
    # span; anything longer would need entry 9's offset and destroy that line.
    _SLOTBOX_TEXT = "New spell slots {N}!"
    _SLOTBOX_NUMTOK = 0x47
    _SLOTBOX_SPAN = 19                  # entry 8's span (off9 - off8) in vanilla

    @classmethod
    def _slotbox_body(cls):
        pre, _, post = cls._SLOTBOX_TEXT.partition("{N}")
        return (BFONT.encode(pre, term=False) + bytes([cls._SLOTBOX_NUMTOK])
                + BFONT.encode(post, term=True))

    async def _slotbox_loop(self):
        body = self._slotbox_body()
        assert len(body) <= self._SLOTBOX_SPAN, len(body)
        while not self.exit_event.is_set():
            await asyncio.sleep(4.0)
            # gate INSIDE the loop: slot_data is empty until Connected arrives
            # (policy P5 -- an early return would kill the loop for every seed)
            if not (self.slot_data.get("on_disc") or {}).get("slot_magic"):
                continue
            # stamp the save-file MARKER (0x5A @ save+0x838) so every save this
            # session writes identifies itself as a slot-magic file -- the
            # save/load preview shows "Magic N" per-file only when the marker
            # rode into that file (mana files keep vanilla "MP n"). Idempotent;
            # re-stamps after the NG+ reset wipes the region.
            try:
                if self.save_delta is not None:
                    a = self.sa(D.SPELL_SLOTS_MARKER_SA)
                    cur = await self.psp.read(a, 1)
                    if cur and cur[0] != D.SPELL_SLOTS_MARKER_VALUE:
                        await self.psp.write(
                            a, bytes([D.SPELL_SLOTS_MARKER_VALUE]))
                        logger.info("  [slot_magic] save-file marker stamped")
            except Exception:
                pass
            # CANARY: the v193 home (save+0x808..0x83B) was chosen because it
            # is zero in a mid-game AND an endgame save -- but zero-in-two-
            # saves is exactly the evidence that failed for the original
            # 0x464 home (a native rolling map-record list grew into it).
            # Watch the guard band below the block (save+0x7EC..0x807, the
            # two 16B-aligned record starts native growth would hit first)
            # and the 3-byte pad after the marker; any nonzero byte = native
            # data marching on the block -> alarm loudly BEFORE charges or
            # the grant counter get eaten.
            try:
                if self.save_delta is not None and not self._sm_canary_warned:
                    guard = await self.psp.read(
                        self.sa(D.SPELL_SLOTS_GUARD_LO_SA),
                        D.SPELL_SLOTS_GUARD_LO_LEN)
                    pad = await self.psp.read(self.sa(D.SPELL_SLOTS_PAD_SA), 3)
                    if any(guard) or any(pad):
                        self._sm_canary_warned = True
                        logger.warning(
                            "[slot_magic] CANARY TRIPPED: native data inside "
                            "the guard band around save+0x808..0x83B "
                            f"(guard={guard.hex()} pad={pad.hex()}). The "
                            "native map-record list may be reaching the "
                            "slot_magic block -- report this save!")
            except Exception:
                pass
            try:
                base = await self._find_battlemsg_bank()
                if base is None:
                    continue
                hdr = await self.psp.read(base, 0x60)
                off8 = struct.unpack_from("<I", hdr, 0x10 + 8 * 4)[0]
                off9 = struct.unpack_from("<I", hdr, 0x10 + 9 * 4)[0]
                if off9 - off8 != self._SLOTBOX_SPAN:   # not the layout we RE'd
                    continue
                cur = await self.psp.read(base + off8, len(body))
                if cur == body:
                    continue                        # already ours
                await self.psp.write(base + off8, body)
                logger.info("  [slotbox] level-up line -> "
                            f"'{self._SLOTBOX_TEXT}' @{base + off8:#x}")
            except Exception:
                self._battlemsg_bank = None         # stale cache -> rescan

    async def _thief_steal_loop(self):
        """Chance for one extra item per battle, delivered as a NATIVE battle drop
        so the game grants it AND shows "Obtained <item>." itself.

        At the START of each battle we sum, over party members whose class
        (class_addr_sa) is Thief (1) or Ninja (7), both their AGI and Luck -- the
        EFFECTIVE values from the battle-unit record (BU_AGI_EFF/BU_LCK_EFF, i.e.
        equipment + tonics included), falling back to the base P_AGI/P_LCK party
        bytes only if that read fails -- then roll TWO independent chances: the
        AGI roll rand()%255 < sumAGI,
        and the Luck roll rand() < min(0.99, (sumLCK/50) * 0.45^tier) (low tiers
        very likely at modest Luck, high tiers gated even at max Luck 99*4). If
        EITHER roll hits we grant exactly ONE extra item (never two), by writing one
        entry into the battle DROP LIST (battle_base+0x6848, stride 3, <=9 slots:
        [cat,id,qty]) -- into a slot whose ENEMY ROW is empty (slot N belongs to
        enemy row N and the victory writer stamps occupied rows; see
        _steal_drop_slot). If the party then WINS, the native victory routine
        (fn 0x887fb90, see thief-steal-ability memory) grants the item and
        announces it in the correct font -- no font RE. If they flee/lose, the
        entry is ignored and the game clears the list next battle. We roll AT
        MOST once per battle (the `handled` latch below) and wait one tick after
        battle start so the game's own list-clear can't wipe us.

        battle_base = *(D.BATTLE_ACTOR_OBJ_PTR_SA); valid only during a battle.
        The in-game battle box is re-authored per steal ("Your <class> took an
        extra <item>!", see _steal_box_author); a fuller line goes to the AP
        client log. Replaces the old post-victory grant_item path, which fired
        too late (after the drop phase) to inject into the same battle."""
        import random
        if not self.thief_steal:
            return
        handled = False       # already rolled for the CURRENT battle
        field_streak = 0      # consecutive not-in-battle polls (debounce)
        while not self.exit_event.is_set():
            try:
                # BATTLE_ACTOR_OBJ_PTR_SA lives in the same 0x08D1/0x08D2 save arena
                # that relocates between sessions (delta 0/+0x1000/+0x4000), so its
                # holder must be sa()-adjusted like every other consumer. Skip the
                # tick until the delta is resolved (else we read a stale address,
                # never see a battle, and the loop is silently dead -- the bug that
                # made this feature look broken at +0x4000).
                if self.save_delta is None:
                    await asyncio.sleep(0.5)
                    continue
                # Gate on the real in-battle flag, NOT the battle_base pointer:
                # that pointer LATCHES (never zeroed on exit) so it reads
                # "in battle" forever after the first fight, which used to make
                # this loop roll exactly once per session then go silent.
                #
                # Re-arm (clear `handled`) after the field state is confirmed for a
                # couple consecutive polls. This debounce exists so a single
                # spurious "not in battle" (a transient read failing safe to False)
                # can't re-arm mid-battle and roll a SECOND time. It was 6 polls
                # (~3 s), but at high encounter rates the field gap between battles
                # is often only 1-2 s, so 6 skipped every other battle (2026-07-08
                # live diag: the flag is rock-solid 0/1 with NO mid-battle dips, and
                # a re-roll can no longer double-write anyway -- the write below is
                # gated on slot0 being empty). 2 polls (~1 s) catches back-to-back
                # battles while still ignoring a lone flaky read.
                if not await self._in_battle():
                    field_streak += 1
                    if field_streak >= 2:      # ~1 s of confirmed field
                        handled = False
                        # battle over (victory announce already read entry 14) ->
                        # restore the native "Obtained {item}." text for future drops
                        await self._steal_box_restore_now()
                        # ... and give back the borrowed red-MISS def (loot icon)
                        await self._steal_icon_restore_now()
                    await asyncio.sleep(0.5)
                    continue
                field_streak = 0
                if handled:
                    # already rolled for this battle
                    await asyncio.sleep(0.5)
                    continue
                bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))
                if not (0x08800000 <= bb < 0x0A000000):
                    # flag is up but the struct pointer isn't populated yet
                    await asyncio.sleep(0.5)
                    continue
                # New battle: wait one tick so battle-start init (which clears
                # the drop list) is done, then roll+write. `handled` is only set
                # once the roll fully succeeds -- a TimeoutError mid-way leaves it
                # unset so the next tick retries the same battle (the old code
                # marked it handled up-front and a single flaky RPC silently ate
                # the whole battle). The write below therefore always happens
                # EARLY (0.4 s after detect), never near victory processing.
                await asyncio.sleep(0.4)
                if not await self._in_battle():
                    continue   # battle ended during the wait
                bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))
                if not (0x08800000 <= bb < 0x0A000000):
                    continue   # battle ended during the wait
                # one contiguous read covers all 4 class bytes + party records:
                # class_addr_sa(ci) = party_addr_sa(ci) - 2 (prev record's 0x5A byte)
                blk = await self.psp.read(self.sa(D.class_addr_sa(0)),
                                          2 + D.PARTY_COUNT * D.PARTY_STRIDE)
                agis, lcks, classes = [], [], []
                for ci in range(D.PARTY_COUNT):
                    off = 2 + ci * D.PARTY_STRIDE
                    agis.append(blk[off + D.P_AGI])
                    lcks.append(blk[off + D.P_LCK])
                    classes.append(blk[ci * D.PARTY_STRIDE])
                # A KO'd or PETRIFIED member has no steal turn, so it contributes
                # neither AGI nor Luck. Both conditions live in the battle-unit
                # status word (BU_STATUS bit0=KO, bit1=stone; &3 = out of action),
                # read from the per-battle unit copies (rows 0-3 = party in menu
                # order, same order as the party records above). One contiguous
                # read covers all four rows. Fail safe = active (True) so a bad
                # read never silently zeros the steal.
                active = [True] * D.PARTY_COUNT
                # The SAME read also carries the EFFECTIVE (equipment-inclusive)
                # AGI/LCK the game fights with -- P_AGI/P_LCK above are BASE stats
                # and miss every gear bonus (Thief's Gloves +5 AGI was invisible to
                # the roll, live 2026-07-31). Prefer the unit values; fall back to
                # the base stats if the read fails or a row reads 0 (a zeroed row
                # would silently kill the steal).
                gear_bonus = lck_gear_bonus = 0
                try:
                    ublk = await self.psp.read(bb + D.BATTLE_UNIT_OFF,
                                               D.PARTY_COUNT * D.BATTLE_UNIT_STRIDE)
                    for ci in range(D.PARTY_COUNT):
                        rec = ci * D.BATTLE_UNIT_STRIDE
                        st = int.from_bytes(ublk[rec + D.BU_STATUS:
                                                 rec + D.BU_STATUS + 2], "little")
                        active[ci] = (st & 3) == 0
                        ea, el = ublk[rec + D.BU_AGI_EFF], ublk[rec + D.BU_LCK_EFF]
                        counts = classes[ci] in (1, 7) and active[ci]
                        if ea:
                            if counts:
                                gear_bonus += max(0, ea - agis[ci])
                            agis[ci] = ea
                        if el:
                            if counts:
                                lck_gear_bonus += max(0, el - lcks[ci])
                            lcks[ci] = el
                except Exception:
                    pass
                # Thief class id = 1 (see [[class-byte]]); Ninja (7) = promoted Thief.
                # Only ACTIVE (not KO'd/petrified) thieves/ninjas contribute.
                thief_agi = sum(a for a, c, al in zip(agis, classes, active)
                                if c in (1, 7) and al)
                # Luck roll draws from the SAME active Thief/Ninja members (max 99*4).
                thief_lck = sum(l for l, c, al in zip(lcks, classes, active)
                                if c in (1, 7) and al)
                # Any thief/ninja who exists but is KO'd/petrified = excluded, so
                # the AP client can flag the reduced steal chance.
                downed_thieves = sum(1 for c, al in zip(classes, active)
                                     if c in (1, 7) and not al)
                downed_note = (f" [{downed_thieves} thief"
                               f"{'ves' if downed_thieves != 1 else ''} "
                               f"KO'd/petrified -- not contributing]"
                               if downed_thieves else "")
                if thief_agi <= 0 and thief_lck <= 0:
                    if downed_thieves:
                        # Every thief is out of action -- no steal possible this
                        # battle; surface it so the reduced/zero chance isn't a
                        # silent mystery.
                        logger.info(f"  [thief-steal] no active thief"
                                    f"{downed_note} -- no steal this battle")
                    handled = True
                    continue
                # Tier the loot on the battle's total XP payout. The enemy-info
                # block is populated a beat after battle start, so a single early
                # read sometimes misses -- retry briefly. If it NEVER reads, this
                # is not a real battle: BATTLE_ACTIVE_FLAG_SA also goes 1 in some
                # field UI (chest pickup -> menu), and in a real battle the block
                # is always readable within the retry window. The old party-avg-
                # level fallback turned those false positives into out-of-battle
                # rolls (the stray "avgLv=" lines) and wrote a drop entry through
                # the LATCHED stale battle_base, so skip instead.
                bxp = None
                for _ in range(6):             # ~1.5 s
                    bxp = await self._battle_xp(bb)
                    if bxp is not None:
                        break
                    if not await self._in_battle():
                        break
                    await asyncio.sleep(0.25)
                if bxp is None:
                    # Unreadable enemy-info block = not a real battle (proven
                    # decisive): BATTLE_ACTIVE_FLAG_SA also goes 1 in field UI
                    # like chest pickup -> menu. Silently skip -- no steal, no log.
                    handled = True             # debounce re-arms on real field
                    continue
                lims = [lim for lim, _, _ in self.STEAL_POOLS]
                tier = next(i for i, l in enumerate(lims) if bxp <= l)
                # Show WHICH XP band picked the tier (tier = first pool whose
                # max_battle_xp >= this battle's summed XP payout).
                lo = lims[tier - 1] + 1 if tier > 0 else 0
                hi = lims[tier]
                band = f"{lo}..{hi}" if hi < 10 ** 9 else f"{lo}+"
                why = f"battleXP={bxp} in [{band}] -> tier{tier + 1}/{len(lims)}"
                # Stealth Ninja Scroll (job_scroll_boosts): with the scroll owned AND a
                # Thief/Ninja actually in the party, the AGI roll is multiplied and a
                # super-rare tier unlocks below. The Luck tier falloff is unchanged.
                # Needs an ACTIVE member (a KO'd/petrified one grants no bonus).
                # Base Thief (1) counts, not just promoted Ninja (7): the scroll
                # renames the BASE class too, and the other scroll legs (steal
                # AGI/LCK pool above, floor loop) already accept c in (1, 7).
                live_ninja = any(c in (1, 7) and al for c, al in zip(classes, active))
                scrolled = (self._job_scrolls_on() and live_ninja
                            and self._scroll_owned(1))
                eff_agi = int(thief_agi * self.NINJA_SCROLL_AGI_MULT) if scrolled \
                    else thief_agi
                decay = self.STEAL_LUCK_TIER_DECAY
                if scrolled:
                    why += " NINJA-SCROLL"
                # Log the raw sumAGI and, when scrolled, the effective value the
                # roll actually uses (e.g. "AGI=50 -> 55/255").
                gear_note = f" (incl +{gear_bonus} gear)" if gear_bonus else ""
                lck_note = f" (incl +{lck_gear_bonus} gear)" if lck_gear_bonus else ""
                agi_note = (f"AGI={thief_agi}{gear_note} -> {min(eff_agi, 254)}/255"
                            if scrolled else f"AGI={thief_agi}{gear_note}/255")
                # Two independent rolls; the steal fires if EITHER hits, and yields
                # AT MOST one extra item per battle (no double-write).
                agi_hit = random.randint(0, 254) < min(eff_agi, 254)
                luck_p = min(self.STEAL_LUCK_CAP,
                             (thief_lck / self.STEAL_LUCK_L_DIV)
                             * (decay ** tier))
                luck_hit = random.random() < luck_p
                if not (agi_hit or luck_hit):
                    handled = True
                    logger.info(f"  [thief-steal] {agi_note}, "
                                f"LCK={thief_lck}{lck_note} (p={luck_p:.3f}) miss "
                                f"({why}){downed_note}")
                    continue
                why += " " + "+".join(t for t, h in (("AGI", agi_hit),
                                                     ("LCK", luck_hit)) if h)
                _, common, rare = self.STEAL_POOLS[tier]
                supers = self.STEAL_SUPER_POOLS[tier]
                if scrolled:
                    # Scrolled Ninja: one categorical draw over super/rare/common
                    # (default 5% / 35% / 60%).
                    r = random.random()
                    is_super = r < self.NINJA_SCROLL_SUPER_CHANCE
                    is_rare = (not is_super and bool(rare) and r <
                               self.NINJA_SCROLL_SUPER_CHANCE
                               + self.NINJA_SCROLL_RARE_CHANCE)
                    rar_note = (f"draw={r:.3f} vs super<{self.NINJA_SCROLL_SUPER_CHANCE:.2f}"
                                f"/rare<{self.NINJA_SCROLL_SUPER_CHANCE + self.NINJA_SCROLL_RARE_CHANCE:.2f}"
                                f", scrolled table")
                else:
                    # Baseline: 95% common / 5% rare. The super tier is not drawable
                    # here -- but the Luck upgrade below can still reach it.
                    is_super = False
                    r = random.random()
                    is_rare = bool(rare) and r < self.STEAL_RARE_CHANCE
                    rar_note = f"draw={r:.3f} vs rare<{self.STEAL_RARE_CHANCE:.2f}, no super draw (unscrolled)"
                # --- Luck rarity upgrade (rolls ONLY on a steal that already hit,
                # and only AFTER the base rarity is settled). p = sumLCK/500, x2
                # while scrolled, NO cap -- a max-Luck party is meant to upgrade
                # every time. Exactly ONE step: common -> rare, rare -> super,
                # super unchanged (the roll is simply spent). This is the only way
                # an UNSCROLLED party reaches the super table, and it draws from
                # the same STEAL_SUPER_POOLS the scroll uses. sumLCK counts only
                # ACTIVE Thief/Ninja and is gear-inclusive (BU_LCK_EFF), so an
                # all-thieves-down party has 0 Luck -> 0% find AND 0% upgrade.
                up_mult = (self.STEAL_LUCK_UPGRADE_SCROLL_MULT if scrolled else 1.0)
                up_p = (thief_lck / self.STEAL_LUCK_UPGRADE_DIV) * up_mult
                if not is_super:
                    ur = random.random()
                    if ur < up_p:
                        if is_rare:
                            is_super, is_rare = True, False
                            step = "RARE -> SUPER-RARE"
                        elif rare:
                            is_rare = True
                            step = "NORMAL -> RARE"
                        else:
                            step = "no rare list -- no upgrade"
                        rar_note += (f"; LUCK-UPGRADE {step} "
                                     f"(roll={ur:.3f} < p={up_p:.3f}"
                                     f"{' x2 scroll' if scrolled else ''})")
                    else:
                        rar_note += (f"; no luck-upgrade (roll={ur:.3f} "
                                     f"vs p={up_p:.3f})")
                # Pool/icon/SFX all key off the POST-upgrade flags, so an upgraded
                # steal shows the gem cue and plays the super sting.
                pool = supers if is_super else (rare if is_rare else common)
                rarity = "SUPER-RARE" if is_super else ("RARE" if is_rare else "NORMAL")
                # Always label the rarity outcome + the roll that produced it, so a
                # NORMAL result reads as an explicit decision, not silence.
                why += f" [{rarity}: {rar_note}]"
                # Battle-start visual cue (steal-sprite-cue): pop a rarity-coded
                # icon over a (random) thief at the first action of this battle.
                # No-op if the sprite feature isn't baked (mailbox absent).
                await self._arm_steal_icon(is_super, is_rare, classes, bb, active)
                # v202: steal-granting a shop AP placeholder is safe now -- the
                # BUYB mailbox attributes purchases by store, so a stolen copy
                # in the bag no longer reads as a purchase.
                blocked = set()
                # slot_magic replaces the MP pool, so the Faerie Tonic (a full
                # MP restore) would be a consumable that does nothing. Block it
                # from the steal tables the same way -- the loop below re-rolls.
                if (self.slot_data.get("on_disc") or {}).get("slot_magic"):
                    blocked.add((D.CAT_ITEM, D.FAERIE_TONIC_ID))
                for _ in range(10):
                    pick = random.choice(pool)   # a (cat,id) tuple or a category list
                    cat, iid = random.choice(pick) if isinstance(pick, list) else pick
                    if (cat, iid) not in blocked:
                        break
                else:
                    cat, iid = D.CAT_ITEM, 1     # Potion
                name = D.CONSUMABLE_ITEMS.get(iid, f"item{iid}") if cat == D.CAT_ITEM else f"{cat}:{iid}"
                # Write ONE entry into the drop list -> native victory grants +
                # announces. handled is set regardless so we roll AT MOST once
                # per battle.
                handled = True
                dbase = bb + D.BATTLE_DROP_LIST_OFF
                # Drop-record order is [cat, id, qty] (disasm-proven: the
                # native victory-drop fn 0x887fb90 reads +0=category
                # (blez->skip), +1=id). The 2026-07-08 [id,cat,qty] swap
                # was WRONG -- it put an invalid category (>3) in +0 and
                # FROZE the loot phase (out-of-bounds category index).
                # The list is 9 entries x 3 bytes and the victory fn SKIPS
                # zero-cat entries (it doesn't stop at the first), so several
                # entries coexist and are all granted (user ask 2026-07-24).
                # WHICH slot matters -- see _steal_drop_slot. Remember the entry
                # so the fade task (~3s in) can re-assert it if the game's late
                # battle-start init wipes the list (slow intros; live
                # 2026-07-24, lost steals).
                self._steal_drop = bytes([cat, iid, 1])
                wrote = None
                for _ in range(6):             # brief retry for a free slot
                    if not await self._in_battle():
                        break                  # battle ended -> abandon this steal
                    lst = await self.psp.read(dbase, 27)
                    free = await self._steal_drop_slot(bb, lst)
                    if free is not None:
                        wrote = free
                        await self.psp.write(dbase + free * 3, self._steal_drop)
                        break
                    await asyncio.sleep(0.25)
                if wrote is not None:
                    # Custom victory announcement: overwrite "Obtained {item}."
                    # with "Your <class> took an extra {item}!" (name+icon via
                    # the 0x46 token). The class name tracks promotion and the
                    # Stealth Ninja Scroll. Restored when the battle ends.
                    steal_prefix = self._steal_box_prefix(classes)
                    await self._steal_box_author(steal_prefix)
                    logger.info(f"  [thief-steal] {agi_note}, LCK={thief_lck}{lck_note} "
                                f"HIT ({why}) -> {steal_prefix}{name}! "
                                f"(drop slot{wrote} + custom box){downed_note}")
                    vb = await self.psp.read(dbase + wrote * 3, 3)
                    logger.info(f"  [thief-steal] drop slot{wrote} verify = "
                                f"{vb[0]:02x} {vb[1]:02x} {vb[2]:02x} @0x{dbase + wrote * 3:08x}")
                    # Ground truth: what the game ACTUALLY put in the bag. The
                    # drop-record write is only a REQUEST -- the victory routine
                    # resolves it, and the announcement box renders whatever IT
                    # decided. Live 2026-07-31: a (CAT_ITEM, 1) Potion request
                    # announced (and banked) as game id 14, so log the inventory
                    # delta instead of trusting the record.
                    asyncio.create_task(self._steal_verify_grant(cat, iid, name))
                else:
                    self._steal_drop = None
                    logger.info(f"  [thief-steal] {agi_note}, LCK={thief_lck}{lck_note} "
                                f"HIT ({why}) but no free drop slot -> skipped")
            except Exception as e:
                logger.info(f"  [thief_steal_loop] {e!r} (will retry this battle)")
            await asyncio.sleep(0.5)

    async def _steal_drop_slot(self, bb, lst):
        """Pick a drop-list slot the game's victory writer can never claim.

        The drop list is NOT an append-anywhere list: `set_drop` (0x8886654) is
        called from the victory reward loop (0x888646c) as
            set_drop(ctx, slot = s0, monster = species[s0])
        with s0 the ENEMY UNIT ROW index 0..8 -- so slot N belongs to enemy row
        N and is stamped unconditionally (if that species has a drop entry and
        rand%100 clears its chance) with `[cat, id, 1]` from the monster drop
        table @0x0894c446 + species*36.

        So writing into "the first free slot" put our steal in slot 0 = the
        FIRST ENEMY's slot, and the victory writer simply overwrote it. That is
        both live mismatches: Blue Curtain (1,26) -> Antidote (1,11) is the 15%
        Antidote drop on species 0xa5/0xa6/0xbe (2026-08-06), and the earlier
        Potion (1,1) -> Echo Grass (1,14) is species 0x15's 4% drop
        (2026-07-31). Nothing remapped our id -- our record was replaced.

        A row whose species reads 0xFF is an EMPTY formation row: the loop skips
        it before ever calling set_drop, so its slot is ours for good. Take the
        highest such row (furthest from the rows a big formation fills in).
        Returns None if the slot is unusable, so the caller retries/skips.
        """
        base = bb + D.BATTLE_UNIT_OFF + D.PARTY_COUNT * D.BATTLE_UNIT_STRIDE
        try:
            rows = await self.psp.read(base, D.BATTLE_DROP_SLOTS
                                       * D.BATTLE_UNIT_STRIDE)
        except Exception:
            return None
        spec = [rows[i * D.BATTLE_UNIT_STRIDE + D.BU_SPECIES]
                for i in range(D.BATTLE_DROP_SLOTS)]
        safe = [i for i in range(D.BATTLE_DROP_SLOTS)
                if spec[i] == 0xFF and lst[i * D.BATTLE_DROP_STRIDE] == 0]
        if safe:
            return safe[-1]
        # Every row is occupied (a full 9-enemy formation). No slot is safe from
        # the victory writer, so fall back to the first free one and say so --
        # the steal may still be replaced by that row's natural drop.
        free = next((i for i in range(D.BATTLE_DROP_SLOTS)
                     if lst[i * D.BATTLE_DROP_STRIDE] == 0), None)
        if free is not None:
            logger.info(f"  [thief-steal] no empty enemy row -- drop slot{free} "
                        f"can still be overwritten by a natural drop "
                        f"(species {' '.join(f'{s:02x}' for s in spec)})")
        return free

    async def _inv_counts(self):
        """{(cat, game_id): qty} of every occupied inventory record."""
        inv = await self.psp.read(self.sa(D.INVENTORY_BASE_SA),
                                  D.INV_RECORD_SIZE * 0x80)
        out = {}
        for i in range(0, len(inv), D.INV_RECORD_SIZE):
            if inv[i]:
                out[(inv[i], inv[i + 1])] = inv[i + 2]
        return out

    async def _steal_verify_grant(self, cat, iid, name):
        """Log the inventory delta a steal actually produced. Snapshots now
        (mid-battle, before the victory routine banks anything), waits for the
        battle to end, then reports every count that went UP -- so a request/grant
        mismatch (wrong id banked, nothing banked, two items banked) shows up in
        the log by itself instead of having to be inferred from the box text."""
        try:
            before = await self._inv_counts()
            for _ in range(240):                # up to ~2 min of battle
                await asyncio.sleep(0.5)
                if not await self._in_battle():
                    break
            else:
                return
            # Re-sample: the victory routine banks the drop some way into the
            # results sequence, and a fast battle end can beat a single read
            # (live 2026-07-31: two "gained NOTHING" reports on ~3 s battles that
            # had in fact granted). Stop as soon as the asked-for item shows up.
            gained = {}
            for _ in range(8):                  # ~8 s of settling
                await asyncio.sleep(1.0)
                after = await self._inv_counts()
                gained = {k: after[k] - before.get(k, 0) for k in after
                          if after[k] > before.get(k, 0)}
                if gained.get((cat, iid), 0) > 0:
                    break
            def nm(k):
                c, g = k
                return (D.CONSUMABLE_ITEMS.get(g, f"item{g}") if c == D.CAT_ITEM
                        else f"cat{c}:{g}")
            got = ", ".join(f"{nm(k)} +{v}" for k, v in gained.items()) or "NOTHING"
            ok = gained.get((cat, iid), 0) > 0
            logger.info(f"  [thief-steal] grant check: asked for {name} "
                        f"({cat},{iid}) -> inventory gained {got}"
                        + ("" if ok else "  <-- MISMATCH"))
        except Exception as e:
            logger.info(f"  [thief-steal] grant check failed: {e!r}")

    async def _battle_xp(self, bb):
        """Total vanilla XP this battle awards: decode the resolved-encounter
        block at bb+D.BATTLE_ENEMY_INFO_OFF ([ids u8x4][counts u8x4], 0xFF =
        empty slot) and sum D.MONSTER_XP[id]*count. None if unreadable or the
        block looks implausible (the caller then treats the "battle" as a
        false positive and skips the steal)."""
        try:
            raw = await self.psp.read(bb + D.BATTLE_ENEMY_INFO_OFF,
                                      4 + 2 * D.BATTLE_ENEMY_TYPES)
            ids = raw[4:4 + D.BATTLE_ENEMY_TYPES]
            cnts = raw[4 + D.BATTLE_ENEMY_TYPES:4 + 2 * D.BATTLE_ENEMY_TYPES]
            total = sum(D.MONSTER_XP[i] * c
                        for i, c in zip(ids, cnts)
                        if i != 0xFF and i < len(D.MONSTER_XP))
            return total if total > 0 else None
        except Exception:
            return None

    # ---------------- save-or-suffer miss feedback (v102) ----------------------
    # When an instant-kill / status spell FAILS, tell the player whether it was
    # ever winnable: "7% Warp chance on Orthros". Nothing is logged on a success --
    # the kill speaks for itself. The on-disc fail cave stamps a report into the
    # SCRL mailbox the instant the spell fails (only it can see the event in
    # time); we reproduce the engine's own score formula from static tables.
    #
    #   type-3 roll spells:  score = acc + 148 - magic_def
    #                                - 148 if the target resists the element
    #                                + INT bonus (scrolled Necrocaster only)
    #                        chance = clamp(score + 1, 0, 201) / 201
    #   v228 (magic_power_scaling): while the monster's domain multiplier is not
    #   1.0 the engine instead rebuilds the score from the VANILLA magic defence
    #   and scales it -- score = (acc + 148 - mdef_vanilla) * 0.5**(mult-1) --
    #   with the resist/weak steps and the INT bonus riding on top UNSCALED.
    #   _sos_chance branches accordingly; iso_patcher._mp_shrink_s7 is the
    #   authority. Kill (64) is NOT covered (its roll is in the bwk cave).
    #   Kill:                same shape but with the fallback accuracy (it only
    #                        reaches the roll after failing the HP autohit).
    #   Stun / Blind:        no roll at all -- if they missed, it was never
    #                        possible against that target's HP, so 0%.
    #
    # (acc, element) per spell id -- magic_info +8 / +4, dumped 2026-07-21.
    _SOS_SPELLS = {
        50: ("Scourge", 40, 0x0100),
        54: ("Death",   24, 0x0008),
        55: ("Quake",   40, 0x0080),
        58: ("Break",   64, 0x0002),
        63: ("Warp",    32, 0x0004),
        64: ("Kill",    IP.NECRO_KILL_FB_ACC, 0x0008),
        56: ("Stun",     0, 0x0001),   # threshold-only: a miss means 0%
        60: ("Blind",    0, 0x0200),   # threshold-only: a miss means 0%
    }
    _SOS_NO_ROLL = (56, 60)
    # Offsets into the 36-byte (MONSTER_STATS_STRIDE) monster record:
    # +0x14 = u8 magic defense, +0x18 = u16 LE elemental-resist bitmask.
    # _sos_chance consumes both to reproduce the engine's save-or-suffer roll.
    _MON_MDEF_OFF, _MON_RESIST_OFF = 0x14, 0x18

    def _sos_monster_at(self, ids, cnts, unit):
        """Resolve an enemy unit index (0-based) to its monster id. Units spawn in
        formation type-slot order, so walk the per-slot counts. Returns
        (monster_id, slot) or (None, None)."""
        acc = 0
        for slot, (mid, cnt) in enumerate(zip(ids, cnts)):
            if mid == 0xFF:
                continue
            if unit < acc + cnt:
                return mid, slot
            acc += cnt
        return None, None

    def _sos_chance(self, spell_id, mon_id, intel, gated, is_boss):
        """Reproduce the engine's landing chance as a percentage."""
        _, acc, elem = self._SOS_SPELLS[spell_id]
        base = mon_id * D.MONSTER_STATS_STRIDE
        rec = D.MONSTER_STATS_BLOCK[base:base + D.MONSTER_STATS_STRIDE]
        mdef = rec[self._MON_MDEF_OFF]
        # Which multiplier owns this monster. boot_patch.magic_power_tables
        # makes exactly the same split (boss_difficulty for boss ids, monster
        # power otherwise), and this log MUST mirror it or the number here and
        # the popup colour -- which reads the engine's live score -- disagree.
        stat_mult = (self.boss_mult if mon_id in BP._boss_stat_ids()
                     else self.monster_mult)
        resist = int.from_bytes(rec[self._MON_RESIST_OFF:self._MON_RESIST_OFF + 2],
                                "little")
        if spell_id in self._SOS_NO_ROLL:
            return 0.0
        if resist & elem:
            return 0.0          # the -148 is unreachable in practice
        bonus = 0
        if gated:
            bonus = intel * IP.SCROLL_TOHIT_INT_MULT
            if is_boss:
                bonus //= IP.NECRO_BOSS_INT_DIV
        # v228 (magic_power_scaling): for every spell the caves cover, the engine
        # no longer subtracts the SCALED mdef from a linear score. It rebuilds
        # the score from the VANILLA mdef and scales the whole thing:
        #     score = (acc + 148 - mdef_vanilla) * 0.5**(mult-1) + bonus
        # The Necrocaster INT bonus and the engine's own resist/weak steps ride
        # on top UNSCALED -- see iso_patcher._mp_shrink_s7, which is the
        # authority for these semantics.
        #
        # Kill (id 64) is the exception: its fallback roll lives in the
        # job_scroll bwk cave, which reads the SCALED mdef straight off the unit
        # and rolls its own rand%201. magic_power_scaling never touches it, so
        # it keeps the pre-v228 maths.
        shrunk256 = BP._mp_shrink256(stat_mult)
        if spell_id == 64 or shrunk256 == BP.MP_SENTINEL:
            if stat_mult != 1.0:
                damped = (stat_mult if stat_mult < 1.0
                          else 1.0 + (stat_mult - 1.0) * BP._BS_DAMP)
                mdef = min(255, int(round(mdef * damped)))
            score = acc + IP.NECRO_ROLL_CONST - mdef + bonus
        else:
            score = int((acc + IP.NECRO_ROLL_CONST - mdef)
                        * (shrunk256 / 256.0)) + bonus
        return max(0, min(IP.NECRO_ROLL_RANGE, score + 1)) / IP.NECRO_ROLL_RANGE * 100

    async def _sos_feedback_loop(self):
        """Log a one-line verdict whenever a save-or-suffer spell fails.

        The feature gate is re-checked INSIDE the loop. Checking it once up front
        and returning kills the task permanently, because loops start before
        slot_data arrives -- _job_scrolls_on() reads slot_data and is therefore
        False at startup for EVERY seed. That is why the first build logged
        nothing at all (2026-07-21 live): the loop had already exited before the
        player ever entered a battle. Same silent-death class as the thief-steal
        save_delta bug; a dead loop here produces no error, just nothing."""
        last_seq = None
        while not self.exit_event.is_set():
            try:
                if not self.slot_data or not self._job_scrolls_on():
                    await asyncio.sleep(1.0)
                    continue
                mb = await self._scroll_mailbox()
                if mb is None or self.save_delta is None:
                    await asyncio.sleep(0.5)
                    continue
                ring = await self.psp.read(mb + IP.SCROLL_MB_REPORT_OFF,
                                           4 + 4 * IP.SOS_RING)
                wr = ring[0]
                if last_seq is None:
                    last_seq = wr                   # don't replay stale entries
                elif wr != last_seq:
                    # Drain EVERY entry published since the last poll -- one
                    # multi-target cast fails once per enemy, all within a single
                    # frame, so there is normally more than one waiting.
                    pending = (wr - last_seq) & 0xFF
                    dropped = max(0, pending - IP.SOS_RING)
                    pending = min(pending, IP.SOS_RING)
                    if dropped:
                        logger.info(f"  [sos] {dropped} miss report(s) overran "
                                    f"the ring and were dropped")
                    for k in range(pending):
                        slot = (wr - pending + k) % IP.SOS_RING
                        off = 4 + slot * 4
                        await self._sos_report(ring[off:off + 4])
                    last_seq = wr
            except Exception as e:
                logger.debug(f"  [sos] {e!r}")
            await asyncio.sleep(0.2)

    async def _sos_report(self, rep):
        spell_id, unit_raw, intel, gated = rep[0], rep[1], rep[2], rep[3]
        if spell_id not in self._SOS_SPELLS:
            return
        if unit_raw < 4:
            return                                  # a party member was targeted
        # Gate on the real in-battle flag first: BATTLE_ACTOR_OBJ_PTR_SA LATCHES
        # (never zeroed on exit), so range-checking it alone would happily decode
        # a stale encounter block after the fight ended.
        if not await self._in_battle():
            return
        bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))  # latch-ok
        if not (0x08800000 <= bb < 0x0A000000):     # sanity only -- the
            return                                  # _in_battle() above is the gate
        raw = await self.psp.read(bb + D.BATTLE_ENEMY_INFO_OFF,
                                  4 + 2 * D.BATTLE_ENEMY_TYPES)
        ids = raw[4:4 + D.BATTLE_ENEMY_TYPES]
        cnts = raw[4 + D.BATTLE_ENEMY_TYPES:4 + 2 * D.BATTLE_ENEMY_TYPES]
        # Resolve the target's monster id from ITS OWN battle-unit record first
        # (row = unit_raw; species u8 @ BU_SPECIES). The type-count walk below
        # mis-maps centered single big-enemy formations -- a Chimera reserves 3
        # enemy rows and the live one is the CENTER (row 5), so unit_raw is NOT
        # the packed type-expansion ordinal (live 2026-07-25). Authenticate the
        # byte against the formation's id set so a dead reserve row (species 0)
        # or a stale read can't be accepted; fall back to the walk otherwise.
        mon_id = slot = None
        if 4 <= unit_raw < 9:
            try:
                sp = (await self.psp.read(
                    bb + D.BATTLE_UNIT_OFF + unit_raw * D.BATTLE_UNIT_STRIDE
                    + D.BU_SPECIES, 1))[0]
                if sp != 0xFF and sp in set(ids):
                    mon_id = sp
            except Exception:
                pass
        if mon_id is None:
            mon_id, _slot = self._sos_monster_at(ids, cnts, unit_raw - 4)
        if mon_id is None or mon_id >= D.MONSTER_STATS_COUNT:
            # Say SOMETHING rather than vanishing: a silent return here is how the
            # first build hid a dead loop for a whole playtest. If this line shows
            # up, the unit -> monster walk is wrong, not the cave.
            logger.info(f"  ?% {self._SOS_SPELLS[spell_id][0]} chance on enemy "
                        f"unit {unit_raw - 4} (unresolved; slots={list(ids)} "
                        f"counts={list(cnts)})")
            return
        # "the boss" = enemy unit 0 whose formation slot-0 id is a boss (a boss
        # used as an ADD is a later unit and is NOT damped) -- mirrors the cave.
        is_boss = (unit_raw == IP.NECRO_BOSS_UNIT_ID
                   and ids[0] in IP.SCROLL_BOSS_IDS)
        pct = self._sos_chance(spell_id, mon_id, intel, gated, is_boss)
        name = self._SOS_SPELLS[spell_id][0]
        mon = MON_NAME.get(mon_id, f"monster {mon_id:#04x}")
        logger.info(f"  {pct:.0f}% {name} chance on {mon}")

    # ---------------- job-scroll boosts (Red Wizard / Master / WW+BW mailbox) --------
    # Client legs of job_scroll_boosts (the WW/BW legs are on-disc caves gated by
    # the SCRL mailbox this loop arms). All numbers are PROTOTYPE-tunable consts.
    # Master: on damage taken, gain temp attack AND temp max HP, then flip to a
    # defensive mode once ONE shared per-battle cap is spent.
    #   the cap : IP.master_atk_cap(level), counted in ATTACK gained, on disc in
    #             the MB_MATK accumulator (v220 -- the max-HP leg used to carry a
    #             second, differently-paced cap of its own; one cap now governs
    #             both, so growth and the boosted heal expire together).
    #   attack  : MOVED ON-DISC (v130) -- iso_patcher MASTER_ATK_DMG_DIV /
    #             master_atk_cap() -- so it can draw a yellow "attack gained"
    #             number with exact timing.
    #   max HP  : ceil(dmg * M_HP_DMG_PCT/100) per hit while under the cap AND
    #             under the engine's 999 max-HP ceiling (v229, see below).
    # Under the cap, current HP rises by only HALF the tick (M_HP_HEAL_NUM/DEN) =
    # 10% of damage taken: the buffer is mostly headroom you still have to heal
    # into, not free sustain. AT the cap max HP stops growing, the heal jumps to
    # M_HP_CAPPED_PCT% of damage taken, and the cave swaps its yellow number for a
    # green one -- offence stops scaling, defence takes over.
    # v218: the tick/heal ratios are OWNED BY iso_patcher -- the on-disc cave
    # mirrors this formula to draw the green heal number, and a drifted copy would
    # show a number the client never applies.
    M_HP_DMG_PCT          = IP.MASTER_HP_DMG_PCT
    M_HP_HEAL_NUM         = IP.MASTER_HP_HEAL_NUM
    M_HP_HEAL_DEN         = IP.MASTER_HP_HEAL_DEN
    M_HP_CAPPED_PCT       = IP.MASTER_HP_CAPPED_PCT   # heal % once ATTACK-capped
    # Every on-disc feature mailbox lives in the cave segment that starts at
    # iso_patcher.BAKE_TAG_ADDR; the OFFSET of each cave inside it shifts with
    # the enabled feature set, so each mailbox is found once by scanning the
    # first _MB_SCAN_LEN bytes for its 4-byte magic and cached for the session.
    #
    # v272: 0x4000 was NOT the whole segment. On a full-feature v271 bake the
    # cave segment is 0x6340 file bytes (0x9190 with the bss tail) and SCRL --
    # the last mailbox appended -- sits at BAKE_TAG_ADDR+0x4528, i.e. past the
    # old window. The scan silently found nothing, so the scroll FLAGS byte was
    # never armed and every job scroll's on-disc leg did nothing at all (report
    # 2026-08-18: a White Cleric's Dia line neither damaged Marilith nor healed
    # the caster). The window now covers the segment with room for future caves;
    # even so it ends far below 0x0A000000, crossing which wedges HybridPSP.
    _MB_SCAN_LEN = 0x10000
    _MB_MISS_WARN = 20        # ticks of not-found before saying so, once

    @staticmethod
    def _scrl_verify(buf, i):
        """Recognise a genuine SCRL cave by its BAKED boss table: every id in
        iso_patcher.SCROLL_BOSS_IDS is flagged and id 0 is not. Reading the set
        rather than a hardcoded id keeps this honest if the boss list is
        retuned."""
        o = i + IP.SCROLL_MB_BOSSTAB_OFF
        if o + 0x100 > len(buf) or buf[o] != 0:
            return False
        return all(buf[o + mid] == 1 for mid in IP.SCROLL_BOSS_IDS)

    async def _find_mailbox(self, magic, attr, tag, verify=None):
        """Shared locate-once helper: scan the cave segment for `magic`, cache
        the hit on `self.<attr>`, log under `tag`. Returns the cached address,
        or None when the owning feature isn't baked into this disc.

        The window deliberately runs PAST the cave segment's end (its length is
        not knowable from RAM), so a hit is accepted only when it is word-aligned
        -- every cave is, by construction -- and, when the caller supplies one,
        when `verify(buf, i)` recognises the cave's baked content. Without that a
        stray magic in the heap that follows the segment would hand back an
        address the arming writes would then corrupt."""
        if getattr(self, attr) is not None:
            return getattr(self, attr)
        try:
            buf = await self.psp.read(IP.BAKE_TAG_ADDR, self._MB_SCAN_LEN)
        except Exception:
            return None
        i = buf.find(magic)
        while i >= 0:
            if i % 4 == 0 and (verify is None or verify(buf, i)):
                setattr(self, attr, IP.BAKE_TAG_ADDR + i)
                logger.info(f"  [{tag}] mailbox @0x{getattr(self, attr):08x}")
                return getattr(self, attr)
            i = buf.find(magic, i + 1)
        # An ENABLED feature whose mailbox never turns up is otherwise invisible:
        # the loop simply keeps not arming it, which is exactly how the SCRL miss
        # above survived several builds. Say it once, in the log.
        n = self._mb_miss.get(tag, 0) + 1
        self._mb_miss[tag] = n
        if n == self._MB_MISS_WARN:
            logger.warning(f"  [{tag}] mailbox NOT found in the cave segment "
                           f"({self._MB_SCAN_LEN:#x} bytes scanned) -- that "
                           f"feature's on-disc half will do nothing")
        return None

    async def _scroll_mailbox(self):
        """On-disc SCRL mailbox cave: 16 bytes, magic 'SCRL', flags u8 @+4
        (bit0 = WW dia, bit1 = BW kill), u16 @+8/+10 = BW tuning."""
        return await self._find_mailbox(b"SCRL", "_scroll_mb", "scrolls",
                                        verify=self._scrl_verify)

    async def _mp_mailbox(self):
        """On-disc MPWR mailbox (magic_power_scaling, v228). None on a
        pre-v228 bake, which leaves every leg vanilla."""
        return await self._find_mailbox(b"MPWR", "_mp_mb", "magic_power")

    async def _write_magic_power_tables(self, soft_ids):
        """Publish the three per-monster-id u16[256] tables the magic_power
        caves read. Called from _write_monster_rewards so it rides the SAME
        choke point as every live monster_stats rescale -- Boost-tab changes to
        Monster Power / Boss Difficulty and per-map cameo softening both land
        here, so the tables can never disagree with the stat block they
        describe. Cheap no-op when the values have not moved.

        BOUNDARY is deliberately NOT written here (see _magic_power_loop): it is
        a live battle address, not a per-seed constant."""
        mb = await self._mp_mailbox()
        if mb is None:
            return False
        try:
            tables = BP.magic_power_tables(boss_mult=self.boss_mult,
                                           soft_ids=soft_ids,
                                           monster_mult=self.monster_mult)
        except Exception as e:
            logger.info(f"  [magic_power] table build failed: {e!r}")
            return False
        if tables == self._mp_tables:
            return True
        eff, van, shr = tables
        try:
            from .iso_patcher import _MP_MB_TABLES
            # The tables live in the cave segment's bss tail (no file bytes to
            # spare); the baked header carries their address.
            base = await self.psp.read_u32(mb + _MP_MB_TABLES)
            if not (0x08800000 <= base < 0x0A000000):
                return False        # pre-v228 bake, or tail not reserved
            await self.psp.write(base, eff + van + shr)
        except Exception as e:
            logger.info(f"  [magic_power] table write failed: {e!r}")
            return False
        self._mp_tables = tables
        return True

    async def _magic_power_loop(self):
        """Arm the MPWR mailbox BOUNDARY while a battle is up, disarm on exit.

        BOUNDARY is the address of the first MONSTER battle-unit record. Every
        magic_power cave compares its target against it: below = party member
        (leave vanilla), at-or-above = monster (consult the per-id tables). The
        two arrays are contiguous -- party rows 0-3 then monsters -- so
            BOUNDARY = battle_base + BATTLE_UNIT_OFF + PARTY_COUNT*BATTLE_UNIT_STRIDE
                     = battle_base + 0xC714 + 4*0x6C = battle_base + 0xC8C4
        and the test is exact by construction rather than data-dependent.

        Re-armed EVERY tick, not once per battle: the mailbox is cave-segment
        RAM, so a savestate load reverts it (same contract as the SCRL flags).
        A reverted BOUNDARY reads as 0 = unarmed = every leg vanilla, which is
        the safe direction to fail.

        Disarmed on battle exit so a stale battle_base can never be compared
        against a later battle's unit addresses -- the pointer LATCHES (policy
        P4), so the gate here is _in_battle(), never the pointer itself."""
        armed = False
        while not self.exit_event.is_set():
            await asyncio.sleep(0.5)
            try:
                if self.psp is None or self.save_delta is None:
                    continue        # sa() would resolve against a stale base
                mb = await self._mp_mailbox()
                if mb is None:
                    continue        # pre-v228 bake: nothing to arm
                from .iso_patcher import _MP_MB_BOUND
                if not await self._in_battle():
                    if armed:
                        await self.psp.write_u32(mb + _MP_MB_BOUND, 0)
                        armed = False
                    continue
                bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))  # latch-ok (gated by _in_battle() above; only used to validate + locate)
                if not (0x08800000 <= bb < 0x0A000000):
                    continue
                if await self._battle_xp(bb) is None:
                    continue        # active flag up in a field UI, not a real fight
                await self.psp.write_u32(
                    mb + _MP_MB_BOUND,
                    bb + D.BATTLE_UNIT_OFF + D.PARTY_COUNT * D.BATTLE_UNIT_STRIDE)
                armed = True
            except Exception:
                pass                # transient RPC failure: retry next tick

    async def _buy_mailbox(self):
        """On-disc BUYB mailbox cave (shop_buy_mailbox): magic 'BUYB', u16 head
        @+4 (total purchases this boot), ring of 8 8-byte entries @+8:
        (store_id u8, type u8, cat u8, gid u8, qty u16, seq u16). The
        purchase-commit hook appends every town-shop buy; the store_id is the
        game's shop-def index (== rando._DEF_IDX values)."""
        return await self._find_mailbox(b"BUYB", "_buy_mb", "shop")

    # --- steal-cue icon (steal-sprite-cue) -----------------------------------
    # Loot-cue icons are PIXELS ONLY in the baked BATTLEICON atlas (popup_bake
    # STEAL_*); no def slot is free for them (defs 13-17 are the ENEMY STATUS
    # balloons -- statically cloning them live-broke sleep indicators
    # 2026-07-23). Instead the client BORROWS def 19 (red MISS!!, the rarest
    # spawn) at runtime: before arming, it repoints def 19's resident OTI entry
    # at the chosen rarity's cell, and restores it at battle end. While
    # borrowed, a real red-tinted MISS!! would draw as the icon (rare,
    # cosmetic). The icon persists for the fight and clears at battle end
    # (float/fade needs deeper popup-updater RE -- deferred).
    # popup slot records on the battle ctx (see steal-sprite-cue memory).
    # Slot 0 sits at ctx+0x420; records are 124 bytes apart. Used both to
    # find the template slot's resident OTI copy and to scan live popup slots.
    _SLOT_TMPL_IDX_OFF = 0x68c2        # u8: template slot index
    _SLOT_STRIDE, _SLOT_BASE0 = 124, 0x420
    _SLOT_OTI_PTR_OFF = 0x3c           # template slot +0x3c -> resident OTI copy

    async def _steal_icon_mailbox(self):
        """On-disc SPRB mailbox cave (feature_steal_sprite): magic 'SPRB',
        count u8 @+4 (frames to spawn), kind u8 @+5 (BATTLEICON def), unit u8
        @+6 (battle unit id)."""
        return await self._find_mailbox(b"SPRB", "_steal_icon_mb", "steal-icon")

    async def _steal_icon_oti(self, bb):
        """Resident BATTLEICON.OTI copy, pointer-chased from the battle ctx:
        template slot = ctx + tmpl_idx*124 + 0x420, OTI ptr at +0x3c. Validated
        by its header (grown def count + matching first-def offset -- was
        (24, 0x64) pre-v143, which silently killed the whole cue once the
        table grew, live 2026-07-25). None if anything looks off -- the
        caller then just skips the cue."""
        from . import popup_bake as PB
        try:
            tmpl = (await self.psp.read(bb + self._SLOT_TMPL_IDX_OFF, 1))[0]
            slot = bb + tmpl * self._SLOT_STRIDE + self._SLOT_BASE0
            oti = await self.psp.read_u32(slot + self._SLOT_OTI_PTR_OFF)
            if not (0x08800000 <= oti < 0x0A000000):
                return None
            hdr = await self.psp.read(oti, 8)
            want = (PB.DEF_COUNT_NEW, 4 + 4 * PB.DEF_COUNT_NEW)
            if struct.unpack_from("<II", hdr)[:2] != want:
                logger.info(f"  [steal-icon] resident OTI header mismatch "
                            f"(want {want}) -- cue skipped")
                return None
            return oti
        except Exception:
            return None

    # Popup slot recs: ctx + 0x420 + idx*124; kind u8 @+0x44, active u8 @+0x77.
    # The spawn lands at base + count (count is NOT always 0), so the icon can sit
    # anywhere from _SC_SLOT_BASE up -- scan the transient pool by KIND rather
    # than assuming a fixed 5 slots (a fixed 0x3C..0x40 scan found nothing and
    # left "MISS!!" text on screen, live 2026-07-23).
    # (record geometry: the shared _SLOT_BASE0/_SLOT_STRIDE declared above)
    _STEAL_SLOT_KIND_OFF, _STEAL_SLOT_ACTIVE_OFF = 0x44, 0x77
    # Scan the REAL transient pool and nothing past it. The cave writes base
    # 0x3C, but the game recomputes base from its own counter before the spawn
    # lands (live 2026-07-24: icon at slots 0x25..0x29, base ~0x21 + in-flight
    # count) -- so a 0x3C.. window misses it entirely. The pool is idx ~0x21..
    # ~0x42; the old 0x50 ceiling ran PAST the end of the slot array and zeroed
    # "active" bytes in whatever battle-ctx memory followed whenever a stray
    # byte read 19 -> party sprites vanished + crash ~2.5s after the cue.
    _STEAL_SLOT_SCAN_LO, _STEAL_SLOT_SCAN_HI = 0x21, 0x43
    _STEAL_SHOW_SEC = 2.5              # how long the cue stays up before it fades out

    async def _steal_icon_restore_now(self):
        """Put the borrowed def back (battle over, or before re-borrowing).
        Also cancels any pending fade-out AND delayed-arm task."""
        for attr in ("_steal_icon_task", "_steal_arm_task"):
            t = getattr(self, attr, None)
            if t is not None and not t.done():
                t.cancel()
            setattr(self, attr, None)
        ent = self._steal_icon_restore
        self._steal_icon_restore = None
        if ent is not None:
            try:
                await self.psp.write(ent[0], ent[1])
            except Exception:
                pass

    async def _steal_block_live(self, bb, map0):
        """True iff it is still safe to WRITE into the battle block at `bb`.

        The fade task sleeps up to ~6.5 s (spawn wait + _STEAL_SHOW_SEC) while
        holding the `bb` it was created with. If the battle ends and the player
        walks into a new map inside that window, the map loader reuses that
        memory and the late drop-list write lands on a live object. A garbage-
        but-NON-NULL pointer in the 48-entry table swept by 0x08915cac then
        passes its null check, `lw ($a0)` reads unmapped (0), and `jalr $zero`
        faults with PC=00000000 / RA=08915ce8 -- three playtester crashes on
        dungeon entry (Chaos Shrine + Sunken Shrine on v258, Ice Cavern on
        v259). Across 16 logged fades the fatal state was ALWAYS "_in_battle()
        true AND the block reads all-0xff", 3/3 with zero false positives.

        _in_battle() alone is NOT enough: it answers "is *a* battle active",
        not "is the battle whose bb I captured still the live one" -- and
        BATTLE_ACTOR_OBJ_PTR_SA LATCHES (never zeroed on exit), so a stale bb
        keeps looking sane. Fail safe = False: skipping the rewrite costs at
        most one stolen item in a rare race, writing costs the whole session.

        NONE of these checks can rule out a NEW battle at the SAME bb: the
        allocator hands out the identical base every battle (0x08d276a0 in
        every playtester bundle), so `cur == bb` passes, the map never changed
        (overworld fight -> overworld), and the low slots are the first thing
        the loader reinitializes so a probe there reads live data while the
        transient pool ~0x1000 bytes up is still dead (crash 5, 2026-08-17:
        all four checks passed 1 ms after the kinds scan read all-0xff). That
        is why the caller ALSO gates on its own scan verdict (`pool_dead`) --
        this method is the cheap outer fence, not the discriminator."""
        try:
            if not await self._in_battle():
                return False
            # a NEW battle relocated the context -> our bb is stale
            cur = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))
            if cur != bb:
                logger.info(f"  [steal-icon] fade: battle base moved 0x{bb:08x}"
                            f" -> 0x{cur:08x} -- drop rewrite skipped")
                return False
            # the map changed under us -> the block was torn down
            mid = await self.psp.read_u32(self.sa(D.FIELD_MAP_ID_SA))
            if map0 is not None and mid != map0:
                logger.info(f"  [steal-icon] fade: map changed {map0} -> {mid}"
                            f" -- drop rewrite skipped")
                return False
            # torn-down / unmapped memory reads back all-0xff. Probe the
            # TRANSIENT POOL region (slot 0x21+), not slot 0: the low slots
            # (party sprite layers) are reinitialized FIRST when the block is
            # reused, so they read live data while the pool is still dead --
            # probing slot 0 is the hole crash 5 walked through.
            probe = await self.psp.read(
                bb + self._SLOT_BASE0
                + self._STEAL_SLOT_SCAN_LO * self._SLOT_STRIDE, 64)
            if probe.count(0xFF) == len(probe):
                logger.info(f"  [steal-icon] fade: block at 0x{bb:08x} reads "
                            f"all-0xff (torn down) -- drop rewrite skipped")
                return False
            return True
        except Exception as e:
            logger.info(f"  [steal-icon] fade: liveness check failed ({e!r})"
                        f" -- drop rewrite skipped")
            return False

    async def _steal_icon_fadeout(self, mb, bb):
        """Guard wrapper. Anything that escapes the fade leaves def 19 BORROWED
        for the rest of the battle -- every later MISS!! then draws the loot
        icon (live 2026-08-06: a NameError in the finally block turned a Confuse
        miss into the rare-coin sprite, and the traceback only surfaced as an
        unretrieved-task warning). Log it loudly and let the battle-end
        _steal_icon_restore_now backstop take over."""
        try:
            await self._steal_icon_fadeout_inner(mb, bb)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"  [steal-icon] fade FAILED: {e!r} -- def borrow left "
                        f"to the battle-end restore")

    async def _steal_icon_fadeout_inner(self, mb, bb):
        """Show the cue briefly, then remove it and give def 19 back so a red
        insta-kill MISS!! renders correctly for the rest of the fight (the icon
        and red MISS!! share def 19 -- no free sprite def, see steal-sprite-cue).
        Waits for the cave to actually spawn (count -> 0) before timing the fade,
        so a slow battle-start intro can't restore the def before the icon shows."""
        cleared_n = 0        # slots we deactivated (0 = icon never found)
        # pool_dead = this task's OWN verdict on the transient pool, from the
        # kind-byte scan below. All-0xff there is the one state that separated
        # every fatal fade from every harmless one (5/5 vs 0/19 across the
        # Eridose bundles), and crash 5 proved _steal_block_live's fresher
        # reads can still pass 1 ms after the scan read dead memory (same-bb
        # new battle reinitializes the low slots first). None = scan never
        # ran, treated as dead.
        pool_dead = None
        # Map we started the fade on. If it changes before the finally block
        # commits, the battle block `bb` points at has been torn down and
        # reused -- see _steal_block_live.
        try:
            map0 = await self.psp.read_u32(self.sa(D.FIELD_MAP_ID_SA))
        except Exception:
            map0 = None
        try:
            for _ in range(20):                        # up to ~4 s for the spawn
                await asyncio.sleep(0.2)
                if (await self.psp.read(mb + 4, 1))[0] == 0:
                    break
            await asyncio.sleep(self._STEAL_SHOW_SEC)
            # Deactivate the 5 icon slots FIRST (sprite gone), THEN restore the
            # def -- restoring while the slot still reads def 19 would flash the
            # icon into red MISS!! text instead of clearing it. Done inline (not
            # via _steal_icon_restore_now, which would cancel THIS task).
            from . import popup_bake as PB
            lo, hi = self._STEAL_SLOT_SCAN_LO, self._STEAL_SLOT_SCAN_HI
            base = bb + self._SLOT_BASE0 + lo * self._SLOT_STRIDE
            blk = await self.psp.read(base, (hi - lo) * self._SLOT_STRIDE)
            pool_dead = blk.count(0xFF) == len(blk)
            for i in range(hi - lo):
                rec = i * self._SLOT_STRIDE
                # only clear slots holding OUR icon (kind == borrowed def), never
                # a native popup that reused this transient slot
                if blk[rec + self._STEAL_SLOT_KIND_OFF] == PB.STEAL_BORROW_DEF:
                    await self.psp.write(
                        base + rec + self._STEAL_SLOT_ACTIVE_OFF, b"\x00")
                    cleared_n += 1
                    logger.info(f"  [steal-icon] fade: cleared slot "
                                f"0x{lo + i:02x} (bb=0x{bb:08x})")
            # NEVER touch 0x68CA/0x68CB/0x68C3. They are a free-batch descriptor
            # -- free_range(0x888703C) clears `active` for count(0x68CA) slots
            # from first(0x68CB) and drops the real allocator (bb+0x67B6) -- not
            # allocation state (corrected RE 2026-08-06, memory
            # popup-pool-cursor-is-not-a-refcount). Writing count here (the old
            # "give the pool back" decrement, or any well-meant fixup) manufac-
            # tures descriptor states vanilla never produces; the (first=0,
            # count=5) shape our SPAWN used to leave made a stray free_range
            # wipe slots 0..4 = the party sprite layers (live 2026-08-06 and
            # Prime 2026-08-10). Since v259 the spawn cave saves/restores the
            # descriptor itself; this task only clears our icon slots by KIND.
            if cleared_n:
                cnt = (await self.psp.read(bb + 0x68CA, 1))[0]
                logger.info(f"  [steal-icon] fade: cleared {cleared_n} slot(s); "
                            f"batch count 0x68ca reads {cnt} (never written)")
            if not cleared_n:
                # Diagnostic: the icon is somewhere else -- dump every slot's
                # kind byte so the real landing zone can be pinned from the log.
                dump = await self.psp.read(bb + self._SLOT_BASE0,
                                           0x51 * self._SLOT_STRIDE)
                kinds = [dump[i * self._SLOT_STRIDE +
                              self._STEAL_SLOT_KIND_OFF] for i in range(0x51)]
                logger.info(f"  [steal-icon] fade: NOTHING in 0x{lo:02x}..0x{hi:02x}; "
                            f"kinds[0..0x50] = "
                            + " ".join(f"{k:02x}" for k in kinds))
        finally:
            try:
                # Self-heal the steal drop: in slow battle intros the game's
                # battle-start init runs AFTER the steal loop's write and
                # REPLACES the whole 9-slot list (natural drops only; live
                # 2026-07-24 -> lost steals). By fade time (~3s in) that init
                # is long done, so if our entry is missing from every slot and
                # we're still in this battle, re-place it -- into a slot the
                # victory writer cannot claim (_steal_drop_slot).
                dbase = bb + D.BATTLE_DROP_LIST_OFF
                lst = await self.psp.read(dbase, 27)
                logger.info("  [steal-icon] fade: drop list now = "
                            + " ".join(f"{lst[i*3]:02x}{lst[i*3+1]:02x}"
                                       for i in range(9)))
                drop = getattr(self, "_steal_drop", None)
                if drop is not None and pool_dead:
                    logger.info(f"  [steal-icon] fade: pool at 0x{bb:08x} read "
                                f"all-0xff in this task's scan -- drop rewrite "
                                f"skipped (dead battle block)")
                if (drop is not None and pool_dead is False
                        and await self._steal_block_live(bb, map0)):
                    # Re-read the list AFTER the liveness probe: that probe
                    # costs several RPC round-trips, so `lst` above is already
                    # stale by the time we would commit.
                    lst = await self.psp.read(dbase, 27)
                    present = any(lst[i*3] == drop[0] and lst[i*3+1] == drop[1]
                                  for i in range(9))
                    free = await self._steal_drop_slot(bb, lst)
                    if not present and free is not None:
                        await self.psp.write(dbase + free * 3, drop)
                        logger.info(f"  [steal-icon] fade: steal entry was wiped "
                                    f"by battle-start init -- rewrote to slot"
                                    f"{free} ({drop[0]:02x} {drop[1]:02x} "
                                    f"{drop[2]:02x})")
            except Exception:
                pass
            self._steal_drop = None
            self._steal_icon_task = None
            # Give def 19 back ONLY if the icon is actually gone. If we could not
            # find/clear it, restoring would turn the still-visible icon into
            # "MISS!!" text (live 2026-07-23) -- far worse than leaving the cue
            # up; the battle-end restore is the backstop either way.
            if cleared_n:
                ent = self._steal_icon_restore
                self._steal_icon_restore = None
                if ent is not None:
                    try:
                        await self.psp.write(ent[0], ent[1])
                    except Exception:
                        pass

    # Both the icon AND its SFX are held back this long after the steal fires,
    # so the cue lands just past the battle-start jingle instead of colliding
    # with it (user 2026-07-25). Tunable; the fade timing keys off the actual
    # spawn (count -> 0), so it self-adjusts to the delay.
    _STEAL_CUE_DELAY_SEC = 0.25

    async def _arm_steal_icon(self, is_super, is_rare, classes, bb, alive=None):
        """Schedule the loot cue ~0.25 s into the battle as a background task,
        so the steal-grant path that follows this call never stalls on it. The
        real work is in _arm_steal_icon_now."""
        if self._steal_arm_task is not None and not self._steal_arm_task.done():
            self._steal_arm_task.cancel()

        async def _delayed():
            try:
                await asyncio.sleep(self._STEAL_CUE_DELAY_SEC)
                await self._arm_steal_icon_now(is_super, is_rare, classes, bb,
                                               alive)
            except asyncio.CancelledError:
                pass
        self._steal_arm_task = asyncio.ensure_future(_delayed())

    async def _arm_steal_icon_now(self, is_super, is_rare, classes, bb, alive=None):
        """Arm the SPRB mailbox to pop a rarity-coded loot icon over a thief.

        Borrows def 19 (red MISS!!) for the icon: saves its two OTI entries,
        repoints entry 0 at the chosen rarity's 16x16 atlas cell (entry 1 gets
        zero size), then arms the cave with kind 19. The borrow is restored at
        battle end by _thief_steal_loop (same edge as the victory-box restore).
        If several party members are thieves (class 1 Thief / 7 Ninja), a random
        one is chosen. Fully best-effort: any miss (feature not baked, no thief,
        transient read) just skips the cue -- it never affects the steal grant."""
        import random
        from . import popup_bake as PB
        mb = await self._steal_icon_mailbox()
        if mb is None:
            return
        units = [ci for ci, c in enumerate(classes)
                 if c in (1, 7) and (alive is None or alive[ci])]
        if not units:
            return
        # Rarity -> icon art: bag = common, coin = rare, gem = super-rare
        # (Stealth Ninja Scroll tier). User's mapping 2026-07-23.
        name = "gem" if is_super else ("coin" if is_rare else "bag")
        cell_x = PB.STEAL_ICON_X0 + PB.STEAL_PLACEMENT.index(name) * 16
        try:
            oti = await self._steal_icon_oti(bb)
            if oti is None:
                return
            off = await self.psp.read_u32(oti + 4 + 4 * PB.STEAL_BORROW_DEF)
            ent = oti + off
            cur = await self.psp.read(ent, 28)             # 2 entries x 14 bytes
            if self._steal_icon_restore is None:
                self._steal_icon_restore = (ent, bytes(cur))
            # 14-byte OTI sprite-def record: s16 dx, s16 dy, u16 atlas u,
            # u16 atlas v, u16 w, u16 h, u16 id -- two records per def entry.
            # The repack below keeps both ids and swaps the borrowed def's
            # cell for the loot icon's atlas cell.
            e0_id = struct.unpack_from("<H", cur, 12)[0]
            e1_id = struct.unpack_from("<H", cur, 26)[0]
            body = struct.pack("<hhHHHHH", PB.STEAL_DX, PB.STEAL_DY,
                               cell_x, PB.STEAL_ICON_Y, 16, 16, e0_id)
            body += struct.pack("<hhHHHHH", 0, 0, cell_x, PB.STEAL_ICON_Y,
                                0, 0, e1_id)               # 2nd piece: zero size
            await self.psp.write(ent, body)
            # kind (+5) + unit (+6) + SE id (+8) BEFORE count (+4): the cave
            # reads count first, so arming it last means it never fires on a
            # stale kind/unit/sound. Sound tracks rarity (user's mapping
            # 2026-07-24): chest-open = common, Antidote = rare, Ether =
            # super-rare (ids captured live, see memory sfx-se-play).
            se = 0xB6 if is_super else (0xCF if is_rare else 0x68)
            await self.psp.write(mb + 5, bytes([PB.STEAL_BORROW_DEF,
                                                random.choice(units)]))
            await self.psp.write(mb + 8, struct.pack("<H", se))
            await self.psp.write(mb + 4, bytes([1]))
            # Fade the cue out after a couple seconds and hand def 19 back, so a
            # red insta-kill MISS!! later in the fight renders correctly (they
            # share def 19). Battle-end restore is the backstop if this doesn't
            # complete (flee, quick KO).
            self._steal_icon_task = asyncio.ensure_future(
                self._steal_icon_fadeout(mb, bb))
        except Exception:
            pass

    async def _scroll_battle_loop(self):
        """Job-scroll boost driver (job_scroll_boosts).

        1) Arms the on-disc SCRL mailbox flags each tick: bit0 = White Wizard
           Scroll owned (dia damages non-undead), bit1 = Necrocaster Scroll
           owned (instant-kill boost). Rewritten every tick because a savestate
           load restores old RAM (the mailbox is code-segment RAM, not save
           block).
        2) Crimson Wizard Scroll (class 3/9 MP<->HP conversion): fully ON-DISC
           since v120 (iso_patcher cwdmg/cwpay caves); the client only arms the
           mailbox flag -- there is no client-side delta leg anymore.
        3) Grand Master Scroll: party Monks/Masters (class 2/8) grow as they take
           damage -- but only if they SURVIVE the hit (v270): a blow that KOs them
           grants no attack, no max HP and no heal, on-disc and here alike. They
           grow under ONE per-battle cap of IP.master_atk_cap(level) attack
           (on-disc MB_MATK). Under it: attack +ceil(dmg/20) (on-disc), max HP
           +ceil(dmg*20%), and current HP +half that tick -- the buffer is mostly
           headroom to heal into rather than free sustain. At it: growth stops on
           both stats and the heal jumps to M_HP_CAPPED_PCT% of the damage taken,
           uncapped, for the rest of the battle (the cave draws it as a green
           number instead of the yellow attack-gain one).
           v229: max HP ALSO stops at the engine's 999 ceiling, which a long fight
           can reach before the attack cap is spent. The heal jumps there too, but
           attack keeps growing to its cap -- so the Master turns defensive early
           without giving up the offence it has not yet earned.

        Both 2) and 3) poll the per-battle unit records (D.BATTLE_UNIT_OFF --
        temp copies; HP/MP write back at battle end, stats reset next battle =
        tonic-style by construction) and react to DELTAS between ticks, so a
        fast exchange between two polls collapses into one delta (fine). Known
        prototype race: the read-modify-write can clobber a change landing in
        the same ~ms window; accepted for now. Writes go to the live battle_base
        (absolute runtime addr, not sa-relative)."""
        prev = None          # row -> dict(hp=..., mp=...) baseline
        hp_gain = [0] * D.PARTY_COUNT    # Master: temp max HP gained this battle
        while not self.exit_event.is_set():
            await asyncio.sleep(0.3)
            try:
                if not self.slot_data or not self._job_scrolls_on():
                    await asyncio.sleep(2.0)
                    continue
                if self.save_delta is None:
                    continue
                # --- 1) mailbox arming (works on the field too) ---
                mb = await self._scroll_mailbox()
                if mb is not None:
                    flags = ((1 if self._scroll_owned(4) else 0)
                             | (2 if self._scroll_owned(5) else 0)
                             | (4 if self._scroll_owned(0) else 0)   # bit2 = Knight lifesteal
                             | (8 if self._scroll_owned(3) else 0)   # bit3 = Crimson Wizard
                             | (16 if self._scroll_owned(2) else 0))  # bit4 = Grand Master
                    await self.psp.write(mb + 4, bytes([flags]))
                    # v108 White Cleric: zero the dia INT accumulator on the
                    # battle->field edge, so the next battle does not start
                    # pre-stacked. Edge-triggered, not every tick: this must sit
                    # ABOVE the rw_on/m_on gate below, which would skip it for a
                    # party that owns only the White Cleric scroll.
                    #
                    # The BOOST itself needs no cleanup -- it only ever lands in
                    # the battle-unit record, which the engine re-derives from
                    # the party record anyway (live 2026-07-22), so it cannot
                    # survive the battle or reach menu casting. This clears the
                    # COUNTER, nothing else.
                    in_batt = await self._in_battle()
                    if self._dia_int_was_battle and not in_batt:
                        from .iso_patcher import (SCROLL_MB_DIAINT_OFF,
                                                  SCROLL_MB_DIAINT_LEN,
                                                  SCROLL_MB_MATK_OFF,
                                                  SCROLL_MB_MATK_LEN)
                        # dia INT accumulator + v130 Grand Master attack
                        # accumulator both reset on the battle->field edge so the
                        # next battle does not start pre-stacked (on-disc caves
                        # own them; the client only zeroes them).
                        await self.psp.write(mb + SCROLL_MB_DIAINT_OFF,
                                             b"\x00" * SCROLL_MB_DIAINT_LEN)
                        await self.psp.write(mb + SCROLL_MB_MATK_OFF,
                                             b"\x00" * SCROLL_MB_MATK_LEN)
                    self._dia_int_was_battle = in_batt
                # --- 2)+3) battle-record deltas ---
                # v120: the Crimson Wizard MP<->HP conversion moved ON-DISC
                # (caves at the cast prologue + physical epilogue, armed by
                # mailbox bit3 above) so the engine draws teal/green numbers.
                # The client delta leg is retired -- running both would apply
                # every conversion twice. KNOWN GAP until the executor-scan
                # leg lands: magic damage taken by an RW yields no MP refund.
                m_on = self._scroll_owned(2)
                if not m_on:
                    prev = None
                    continue
                if not await self._in_battle():
                    prev = None
                    hp_gain = [0] * D.PARTY_COUNT
                    continue
                bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))  # latch-ok (in-battle gate is _in_battle() above; this only validates the ptr)
                if not (0x08800000 <= bb < 0x0A000000):
                    continue
                if prev is None:
                    # new battle: let battle-start init finish, then baseline
                    await asyncio.sleep(0.4)
                    if not await self._in_battle():
                        continue
                blk = await self.psp.read(bb + D.BATTLE_UNIT_OFF,
                                          D.PARTY_COUNT * D.BATTLE_UNIT_STRIDE)
                cls_blk = await self.psp.read(self.sa(D.class_addr_sa(0)),
                                              2 + D.PARTY_COUNT * D.PARTY_STRIDE)
                # v219: the on-disc attack accumulator (MB_MATK[unit], u8 per party
                # slot) is what tells us a Master is ATTACK-capped -- the same
                # condition the cave uses to draw a green heal number instead of a
                # yellow attack-gain one. The boosted heal rate must key off THAT,
                # not off our own max-HP pool, or the number drawn and the HP
                # written would be two different events. Read-only here; the cave
                # owns the writes and the mailbox-arming leg above owns the reset.
                matk = b"\x00" * D.PARTY_COUNT
                if mb is not None:
                    from .iso_patcher import SCROLL_MB_MATK_OFF, SCROLL_MB_MATK_LEN
                    matk = await self.psp.read(mb + SCROLL_MB_MATK_OFF,
                                               SCROLL_MB_MATK_LEN)
                cur = []
                for row in range(D.PARTY_COUNT):
                    o = row * D.BATTLE_UNIT_STRIDE
                    cur.append({
                        "st": int.from_bytes(blk[o:o + 2], "little"),
                        "hp": int.from_bytes(blk[o + D.BU_HP:o + D.BU_HP + 2], "little"),
                        "maxhp": int.from_bytes(blk[o + D.BU_MAXHP:o + D.BU_MAXHP + 2], "little"),
                        "mp": int.from_bytes(blk[o + D.BU_MP:o + D.BU_MP + 2], "little"),
                        "maxmp": int.from_bytes(blk[o + D.BU_MAXMP:o + D.BU_MAXMP + 2], "little"),
                        "atk": int.from_bytes(blk[o + D.BU_ATTACK:o + D.BU_ATTACK + 2], "little"),
                        # the game's own temp-bonus inputs (also the tonics' fields)
                        "atkbonus": int.from_bytes(
                            blk[o + D.BU_ATTACK_BONUS:o + D.BU_ATTACK_BONUS + 2], "little"),
                        "hpbonus": int.from_bytes(
                            blk[o + D.BU_MAXHP_BONUS:o + D.BU_MAXHP_BONUS + 2], "little"),
                        "cls": cls_blk[row * D.PARTY_STRIDE],
                    })
                if prev is None:
                    prev = cur
                    continue
                for row in range(D.PARTY_COUNT):
                    c, p = cur[row], prev[row]
                    base = bb + D.BATTLE_UNIT_OFF + row * D.BATTLE_UNIT_STRIDE
                    dhp = p["hp"] - c["hp"]
                    dmp = p["mp"] - c["mp"]
                    alive = (c["st"] & 3) == 0
                    # (Crimson Wizard delta leg removed in v120 -- on-disc now.)
                    # (v130: the Grand Master ATTACK leg also moved on-disc -- to
                    # the same damage epilogues as the CW refund -- so it can draw
                    # a yellow "attack gained" number with exact timing. Only the
                    # max-HP leg stays here; it has no popup.)
                    # v270: survive-the-hit rule. The KO status bit and curHP==0
                    # are set by the same apply pass, but a tick that catches the
                    # HP write before the status one would otherwise pay max HP +
                    # a heal to a corpse -- and healing a 0-HP unit off the blow
                    # that killed it reads as a revive. Both must say alive.
                    if (m_on and c["cls"] in (2, 8) and alive and c["hp"] > 0
                            and dhp > 0):
                        # The max-HP leg goes through the GAME'S OWN temp-bonus
                        # field (BU_MAXHP_BONUS, the Giant's Tonic input):
                        #  1. Writing BU_MAXHP directly does NOT stick -- the engine
                        #     re-derives it as (party stat + bonus) on every damage
                        #     event, so the bonus is the only durable input.
                        #  2. It may already hold a tonic, so ADD (never assign),
                        #     tracking the running total on OUR OWN hp_gain
                        #     accumulator so a tonic is never throttled.
                        lvl = cls_blk[2 + row * D.PARTY_STRIDE]   # u8 low byte of level
                        # v220: ONE CAP. The Master's max-HP pool used to have its
                        # own per-battle ceiling (level*4+30 of max HP) which ran on
                        # a different accumulator, at a different rate, off a
                        # different damage stream than the on-disc ATTACK cap -- so
                        # the two expired at different moments and the character
                        # spent a stretch of every battle half-capped. The attack
                        # cap (IP.master_atk_cap(level), tracked on-disc in
                        # MB_MATK) is now the ONLY cap: growth and the boosted heal
                        # flip together, at the same instant the cave switches its
                        # popup from the yellow attack-gain number to the green heal
                        # number, so the number drawn is always the number applied.
                        #
                        # v229 CORRECTION: this used to warn that the accumulator
                        # was physical-only, so a Master who ate nothing but spells
                        # would grow forever and never reach the boosted heal. Not
                        # true, and it was not true when it was written -- the v130
                        # MAGIC leg (iso_patcher RWMMST, in the magic-executor
                        # epilogue, same apply_job_scroll_boosts install, same
                        # `acc += gain` on MB_MATK) advances the cap off spell
                        # damage exactly as the physical epilogue does. Both damage
                        # streams feed one counter; nothing here is stream-blind.
                        #
                        # A tick that could not read the mailbox leaves matk 0 =
                        # "not capped" = keep growing at the conservative rate.
                        atk_capped = matk[row] >= IP.master_atk_cap(lvl)
                        # v229: the SECOND stop condition. The engine clamps a
                        # battle unit's derived max HP to 999 -- both in the
                        # Giant's Tonic case and in the re-derive it runs on every
                        # damage event (IP.MASTER_MAXHP_CEIL, sites cited there).
                        # BU_MAXHP_BONUS, the field we actually write, is NOT
                        # clamped, so without this the loop would keep inflating a
                        # bonus the engine discards -- growth that costs the player
                        # the boosted heal and buys nothing. At the ceiling the
                        # max-HP leg goes quiet and the heal jumps early, while
                        # ATTACK keeps accruing to its own cap (so the cave, which
                        # knows only the attack accumulator, still draws its yellow
                        # attack-gain number -- correct, and no cave change needed).
                        hp_full = c["maxhp"] >= IP.MASTER_MAXHP_CEIL
                        inc_h = -(-(dhp * self.M_HP_DMG_PCT) // 100)         # ceil
                        gain_h = 0 if (atk_capped or hp_full) else inc_h
                        # Never bank more than the ceiling can hold: an unclamped
                        # overshoot would leave hpbonus claiming max HP the unit
                        # does not have, and a Giant's Tonic drunk later would then
                        # appear to do nothing.
                        gain_h = min(gain_h, IP.MASTER_MAXHP_CEIL - c["maxhp"])
                        if atk_capped or hp_full:
                            heal_h = -(-(dhp * self.M_HP_CAPPED_PCT) // 100)   # ceil
                        else:
                            heal_h = (inc_h * self.M_HP_HEAL_NUM) // self.M_HP_HEAL_DEN
                        if gain_h > 0:
                            hp_gain[row] += gain_h
                            nb = min(0xFFFF, c["hpbonus"] + gain_h)    # ADD, don't assign
                            await self.psp.write(base + D.BU_MAXHP_BONUS,
                                                 nb.to_bytes(2, "little"))
                            c["hpbonus"] = nb
                            new_max = min(IP.MASTER_MAXHP_CEIL, c["maxhp"] + gain_h)
                            await self.psp.write(base + D.BU_MAXHP,
                                                 new_max.to_bytes(2, "little"))
                            c["maxhp"] = new_max
                        if heal_h > 0:
                            new_hp = min(c["maxhp"], c["hp"] + heal_h)
                            if new_hp != c["hp"]:
                                await self.psp.write(base + D.BU_HP,
                                                     new_hp.to_bytes(2, "little"))
                                c["hp"] = new_hp
                        if gain_h > 0 or heal_h > 0:
                            mode = ("CAPPED" if atk_capped else
                                    "HPFULL" if hp_full else "growing")
                            logger.info(
                                f"  [scrolls] Master row{row}: took {dhp} "
                                f"-> +{gain_h} maxHP, +{heal_h} HP "
                                f"({mode} atk {matk[row]}/{IP.master_atk_cap(lvl)}"
                                f", maxHP {c['maxhp']}/{IP.MASTER_MAXHP_CEIL}; "
                                f"mine hp+{hp_gain[row]}; field hp+{c['hpbonus']})")
                    prev[row] = c
            except Exception as e:
                logger.info(f"  [scroll_battle_loop] {e!r}")
                prev = None
                await asyncio.sleep(1.0)

    # --- Stealth Ninja Scroll leg 2: damaging-floor mitigation (Ice Cave / Mt Gulg / DLC) ---
    # With the Stealth Ninja Scroll owned, the mitigation SCALES with how many
    # Thieves/Ninjas are in the party (more sure-footed guides = better path):
    #   1 -> half damage    (refund 50% of each floor tick)
    #   2 -> no damage      (refund 100%)
    #   3 -> net +5 HP/step (refund 100% + heal 5x the tick's size on top)
    #   4 -> net +5 HP AND +5 MP/step (tier 3 + 5x the tick's size as MP)
    # Under slot_magic MP does not exist, so tier 4's MP leg becomes a SPELL
    # SLOT restore instead (user 2026-07-31): a flat ONE charge per member per
    # step -- not scaled by the tick's size -- refunded to the LOWEST spell
    # level that still has a spent charge (level 1 refills to full, then level
    # 2, and so on). Every living member gets it; non-casters need no filter
    # because a character who never cast has spent == 0 at every level.
    # Tiers 1-3 are unchanged, and a non-slot_magic seed keeps the MP write.
    # Refund fractions carry over per character (accumulator) so a 1 HP/step
    # floor at 50% keeps costing 1 HP every OTHER step instead of rounding the
    # boost away. count -> (refund %, extra HP heal x dmg, MP gain x dmg):
    NINJA_FLOOR_TIERS = {1: (50, 0, 0), 2: (100, 0, 0),
                         3: (100, 5, 0), 4: (100, 5, 5)}
    _FLOOR_TICK = 0.08          # poll fast enough to catch each step's HP drop

    async def _floor_damage_loop(self):
        """Refund/invert damaging-floor hits while Thieves/Ninjas are in the party
        and the Stealth Ninja Scroll is owned (job_scroll_boosts). Tier by
        Thief/Ninja count: see NINJA_FLOOR_TIERS.

        There is no known "floor damage" event to hook, so this reacts to FIELD
        (out-of-battle) HP deltas on the save-block party records. A floor tick
        hits EVERY living party member at once; per-character chip damage (poison)
        does not. So a refund only fires when all living members (>= 2 of them)
        dropped HP on the same poll -- that discriminates floors from poison and
        keeps the mitigation off anything else. A member already at 0 HP is left
        alone (never revive), and a member the floor KILLED gets no refund either
        (mitigating a death would rewrite a native game-over path).
        """
        acc = [0.0] * D.PARTY_COUNT      # carried fractional refund per row
        prev = None                      # last poll's HP list, or None
        while not self.exit_event.is_set():
            await asyncio.sleep(self._FLOOR_TICK)
            try:
                if not self.slot_data or not self._job_scrolls_on() \
                        or not self._scroll_owned(1):
                    prev = None
                    await asyncio.sleep(2.0)
                    continue
                if self.save_delta is None or await self._in_battle():
                    prev = None
                    continue
                blk = await self.psp.read(self.sa(D.class_addr_sa(0)),
                                          2 + D.PARTY_COUNT * D.PARTY_STRIDE)
                classes = [blk[r * D.PARTY_STRIDE] for r in range(D.PARTY_COUNT)]
                ninjas = sum(1 for c in classes if c in (1, 7))
                if ninjas == 0:
                    prev = None
                    await asyncio.sleep(1.0)
                    continue
                refund_pct, heal_x, mp_x = self.NINJA_FLOOR_TIERS[min(ninjas, 4)]
                slotmagic = bool((self.slot_data.get("on_disc")
                                  or {}).get("slot_magic"))
                hp, maxhp, mp, maxmp = [], [], [], []
                # hp/maxhp/mp/maxmp are four CONTIGUOUS LE u16s in exactly
                # that order (P_HP..P_MAXMP layout assumption -- the +2/+4/+6
                # reads below bake it in). The leading `2 +` skips the class
                # byte array (blk begins 2 bytes before the party records).
                for r in range(D.PARTY_COUNT):
                    o = 2 + r * D.PARTY_STRIDE + D.P_HP   # blk starts at CLASS_BASE_SA = PARTY_BASE_SA-2
                    hp.append(int.from_bytes(blk[o:o + 2], "little"))
                    maxhp.append(int.from_bytes(blk[o + 2:o + 4], "little"))
                    mp.append(int.from_bytes(blk[o + 4:o + 6], "little"))
                    maxmp.append(int.from_bytes(blk[o + 6:o + 8], "little"))
                if prev is None or len(prev) != len(hp):
                    prev = hp
                    continue
                living = [r for r in range(D.PARTY_COUNT) if prev[r] > 0]
                hit = [r for r in living if prev[r] - hp[r] > 0]
                # floor tick == every living member took damage, at least 2 of them
                if len(living) >= 2 and len(hit) == len(living):
                    for r in hit:
                        if hp[r] <= 0:            # floor killed them: no refund
                            acc[r] = 0.0
                            continue
                        dmg = prev[r] - hp[r]
                        acc[r] += dmg * refund_pct / 100.0
                        heal = int(acc[r]) + dmg * heal_x
                        acc[r] -= int(acc[r])
                        if heal > 0:
                            new_hp = min(maxhp[r], hp[r] + heal)
                            if new_hp != hp[r]:
                                await self.psp.write(
                                    self.sa(D.party_addr_sa(r, D.P_HP)),
                                    new_hp.to_bytes(2, "little"))
                                logger.info(f"  [scrolls] Ninja floor row{r} "
                                            f"(x{ninjas}): {dmg} dmg -> "
                                            f"+{heal} HP back")
                                hp[r] = new_hp
                        if mp_x and slotmagic:
                            # slot_magic: MP is gone -- refund one charge at the
                            # lowest spell level that has one spent. Flat 1 per
                            # step, not scaled by dmg; a member with nothing
                            # spent (or no magic at all) is all-zero and skipped.
                            sa = self.sa(D.SPELL_SLOTS_SPENT_BASE_SA
                                         + r * D.SPELL_SLOTS_PER_CHAR)
                            spent = await self.psp.read(
                                sa, D.SPELL_SLOTS_PER_CHAR)
                            for lv in range(D.SPELL_SLOTS_PER_CHAR):
                                if spent[lv]:
                                    await self.psp.write(
                                        sa + lv, bytes([spent[lv] - 1]))
                                    logger.info(
                                        f"  [scrolls] Ninja floor row{r} "
                                        f"(x{ninjas}): +1 L{lv + 1} spell slot")
                                    break
                        elif mp_x and maxmp[r] > 0:
                            new_mp = min(maxmp[r], mp[r] + dmg * mp_x)
                            if new_mp != mp[r]:
                                await self.psp.write(
                                    self.sa(D.party_addr_sa(r, D.P_MP)),
                                    new_mp.to_bytes(2, "little"))
                                logger.info(f"  [scrolls] Ninja floor row{r} "
                                            f"(x{ninjas}): +{new_mp - mp[r]} MP")
                prev = hp
            except Exception as e:
                logger.info(f"  [floor_damage_loop] {e!r}")
                prev = None
                await asyncio.sleep(1.0)

    # ---------------- field KO reconciler (soft-dead repair) ----------------------
    _KOSYNC_TICK = 1.0

    async def _ko_sync_loop(self):
        """Repair SOFT-DEAD party members: HP 0 but the field KO byte clear.

        In FF1 a field record at 0 HP is dead, but the KO STATE is a separate byte
        (D.status_addr_sa, RE'd 2026-07-29). Any HP write that doesn't also set it
        leaves a member who can't act, shows no fallen pose, and whom the church
        won't revive. Two known producers: blood_magic's self-inflicted cost when
        it kills the caster (the in-battle result-array KO doesn't carry the field
        byte through write-back), and any client HP write. Rather than fix each
        source, reconcile the invariant here: HP == 0 <-> KO bit set.

        Field only (battle runs on unit-record copies; touching the save block
        mid-battle would fight the engine's own write-back) and only on records
        that look real (MAXHP > 0), so an unpopulated/mid-rewrite read can't
        stamp KO on a live party. Never CLEARS the bit -- a revive is the game's
        job, and clearing would fight a legitimately KO'd member."""
        while not self.exit_event.is_set():
            await asyncio.sleep(self._KOSYNC_TICK)
            try:
                if self.save_delta is None or await self._in_battle():
                    continue
                for row in range(D.PARTY_COUNT):
                    b = await self.psp.read(self.sa(D.party_addr_sa(row, D.P_HP)), 4)
                    hp = int.from_bytes(b[0:2], "little")
                    mx = int.from_bytes(b[2:4], "little")
                    if hp != 0 or mx == 0:
                        continue
                    st = (await self.psp.read(self.sa(D.status_addr_sa(row)), 1))[0]
                    if not (st & D.P_STATUS_KO):
                        await self.psp.write(self.sa(D.status_addr_sa(row)),
                                             bytes([st | D.P_STATUS_KO]))
                        logger.info(f"  [ko_sync] row{row} was 0 HP without the KO "
                                    "state (blood magic?) -- marked KO")
            except Exception:
                # transient read failures (save block relocating) are expected
                await asyncio.sleep(2.0)

    # ---------------- Death Link (yaml death_link / death_link_severity) ----------
    _DL_TICK = 0.5
    _WIPE_CONFIRM = 3   # consecutive valid all-dead polls (~1.5s) before sending

    def on_deathlink(self, data):
        """A DeathLink bounce arrived from another player. CommonContext's super
        prints the death message and bumps last_death_link (which also filters our
        own echoed bounce, since send_death stamps the same time). We just flag
        the death; _death_link_loop applies it on its next tick (it needs the
        PSP bridge + save_delta, which this sync callback can't await)."""
        super().on_deathlink(data)
        if self.death_link_on:
            self._dl_pending = True

    async def _read_party_living(self, in_battle):
        """(living_rows, valid). In battle, read the per-battle unit records
        (BU_HP > 0 and not KO/stoned); on the field, the save-block party records
        (P_HP > 0). valid=False when the records are clearly not real party data
        -- every row's MAXHP is 0. That happens in the battle-START window (the
        active flag is up before the engine copies the party into the unit
        records, so everything reads 0 -- treating that as a wipe caused a false
        DeathLink send, live 2026-07-29). An invalid poll must be discarded, not
        interpreted."""
        living, any_maxhp = [], False
        if in_battle:
            bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))  # latch-ok (in-battle gate is caller's _in_battle())
            blk = await self.psp.read(bb + D.BATTLE_UNIT_OFF,
                                      D.PARTY_COUNT * D.BATTLE_UNIT_STRIDE)
            for row in range(D.PARTY_COUNT):
                o = row * D.BATTLE_UNIT_STRIDE
                hp = int.from_bytes(blk[o + D.BU_HP:o + D.BU_HP + 2], "little")
                mx = int.from_bytes(blk[o + D.BU_MAXHP:o + D.BU_MAXHP + 2], "little")
                st = int.from_bytes(blk[o + D.BU_STATUS:o + D.BU_STATUS + 2], "little")
                any_maxhp = any_maxhp or mx > 0
                if hp > 0 and not (st & 3):      # &3 = KO/stone -> out of action
                    living.append(row)
        else:
            for row in range(D.PARTY_COUNT):
                b = await self.psp.read(self.sa(D.party_addr_sa(row, D.P_HP)), 4)
                hp = int.from_bytes(b[0:2], "little")     # P_HP
                mx = int.from_bytes(b[2:4], "little")     # P_MAXHP (P_HP+2)
                any_maxhp = any_maxhp or mx > 0
                if hp > 0:
                    living.append(row)
        return living, any_maxhp

    async def _apply_deathlink(self, in_battle, living):
        """Kill min(severity, len(living)) living party members. In battle: zero
        the battle-unit HP and set the KO status bit (the engine writes HP back to
        the party record at battle end natively). On the field: zero the party
        record's HP (a field member at 0 HP is KO'd, exactly like walking into
        poison death). Victims are drawn at random among the living."""
        import random as _random
        n = min(self.death_link_severity, len(living))
        victims = _random.sample(living, n)
        for row in sorted(victims):
            if in_battle:
                bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))  # latch-ok (in-battle gate is caller's _in_battle())
                base = bb + D.BATTLE_UNIT_OFF + row * D.BATTLE_UNIT_STRIDE
                st = int.from_bytes(await self.psp.read(base + D.BU_STATUS, 2),
                                    "little")
                await self.psp.write(base + D.BU_HP, (0).to_bytes(2, "little"))
                await self.psp.write(base + D.BU_STATUS,
                                     (st | 1).to_bytes(2, "little"))
            # ALWAYS zero the save-block record too: in battle the engine's
            # end-of-battle write-back covers it, but a battle that ends before
            # this row acts again (run/win) must not resurrect them.
            await self.psp.write(self.sa(D.party_addr_sa(row, D.P_HP)),
                                 (0).to_bytes(2, "little"))
            # ...and set the field KO byte, or the member is only SOFT-dead
            # (no KO pose/label, church won't revive; live 2026-07-29)
            st = (await self.psp.read(self.sa(D.status_addr_sa(row)), 1))[0]
            await self.psp.write(self.sa(D.status_addr_sa(row)),
                                 bytes([st | D.P_STATUS_KO]))
        logger.info(f"  [death_link] received death killed {n} party member(s): "
                    f"rows {sorted(victims)}")
        return n

    # --- battle limbo recovery -------------------------------------------------
    # The engine's "party is wiped" check runs during DAMAGE RESOLUTION, not at
    # battle start. A battle entered with all four members already dead (only
    # death_link can produce that state -- natural deaths always happen inside a
    # battle) therefore never resolves anything: it waits for input from an actor
    # that cannot act, and the game hangs (live 2026-07-29). Same shape if a
    # received death kills the last living member during the input phase.
    #
    # Recovery: revive ONE member at 1 HP so input is possible again. They act,
    # take the next hit, and the engine's own resolution delivers the game over --
    # so the death still lands, just a beat later, through the native path (which
    # is exactly what the preemptive-strike case does).
    #
    # ARMED ONLY BY OUR OWN WIPES (_dl_wipe_latch). An enemy-caused wipe must
    # never be interrupted: reviving during the engine's game-over sequence would
    # make the party unkillable.
    _LIMBO_TICK = 1.0
    _LIMBO_CONFIRM = 3        # ~3s of in-battle zero-living before intervening

    async def _limbo_revive(self):
        """Revive the highest-MAXHP party member at 1 HP, in the battle-unit
        record (so they can act NOW) and in the save block (so the state stays
        consistent if the battle ends some other way). Returns the row, or None."""
        bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))  # latch-ok (caller gated on _in_battle())
        blk = await self.psp.read(bb + D.BATTLE_UNIT_OFF,
                                  D.PARTY_COUNT * D.BATTLE_UNIT_STRIDE)
        best, best_mx = None, 0
        for row in range(D.PARTY_COUNT):
            o = row * D.BATTLE_UNIT_STRIDE
            mx = int.from_bytes(blk[o + D.BU_MAXHP:o + D.BU_MAXHP + 2], "little")
            if mx > best_mx:
                best, best_mx = row, mx
        if best is None:
            return None
        base = bb + D.BATTLE_UNIT_OFF + best * D.BATTLE_UNIT_STRIDE
        st = int.from_bytes(blk[best * D.BATTLE_UNIT_STRIDE:
                                best * D.BATTLE_UNIT_STRIDE + 2], "little")
        await self.psp.write(base + D.BU_HP, (1).to_bytes(2, "little"))
        await self.psp.write(base + D.BU_STATUS, (st & ~3).to_bytes(2, "little"))
        await self.psp.write(self.sa(D.party_addr_sa(best, D.P_HP)),
                             (1).to_bytes(2, "little"))
        fst = (await self.psp.read(self.sa(D.status_addr_sa(best)), 1))[0]
        await self.psp.write(self.sa(D.status_addr_sa(best)),
                             bytes([fst & ~D.P_STATUS_KO]))
        return best

    async def _battle_limbo_loop(self):
        """Watch for the un-actionable battle described above and break it.

        ARMING is what keeps this off legitimate wipes. Two arming conditions:
          * _field_all_dead -- the last FIELD observation before this battle had
            zero living members, i.e. the party walked in dead. Tracked here every
            tick we are out of battle, so it also covers a RELOADED save that was
            already in that state (the in-session latch alone missed that -- live
            2026-07-29).
          * _dl_wipe_latch -- a death we applied MID-battle killed the last actor.
        An enemy-caused wipe arms neither (the party was alive on the field going
        in), so the engine's own game-over sequence is never interrupted."""
        streak = 0
        while not self.exit_event.is_set():
            await asyncio.sleep(self._LIMBO_TICK)
            try:
                if self.save_delta is None:
                    streak = 0
                    continue
                if not await self._in_battle():
                    # out of battle: keep the "walked in dead" observation fresh
                    living, valid = await self._read_party_living(False)
                    if valid:
                        self._field_all_dead = not living
                    streak = 0
                    continue
                if not (self._field_all_dead or self._dl_wipe_latch):
                    streak = 0
                    continue
                living, valid = await self._read_party_living(True)
                if not valid:
                    streak = 0          # records not copied in yet
                    continue
                if living:
                    self._dl_wipe_latch = False   # someone can act: nothing to fix
                    self._field_all_dead = False
                    streak = 0
                    continue
                streak += 1
                if streak < self._LIMBO_CONFIRM:
                    continue
                streak = 0
                row = await self._limbo_revive()
                if row is None:
                    continue
                self._dl_wipe_latch = False
                self._field_all_dead = False   # a living member exists again
                self._dl_limbo = True     # hold the send-guard: the game over that
                self._dl_guard = True     # follows is the received death, not ours
                logger.info(f"  [death_link] battle entered with the whole party "
                            f"dead (no actor can act) -- revived row{row} at 1 HP "
                            "so the game can resolve the death natively")
            except Exception:
                streak = 0
                await asyncio.sleep(2.0)

    async def _death_link_loop(self):
        """Death Link (opt-in yaml). Two jobs on one poll:

        SEND: detect a total party wipe (living count edges >0 -> 0, in battle
        or on the field) and broadcast a DeathLink bounce. _dl_guard suppresses
        the wipe WE caused by applying a received full-severity death, so links
        never ping-pong; it clears once anyone is alive again (revive/reload).

        RECEIVE: on_deathlink flags _dl_pending; here we kill severity living
        members -- in battle via the battle-unit records, on the field via the
        save-block party records (both paths work mid-session, whichever state
        the game is in when the death arrives)."""
        prev_living = None
        wipe_polls = 0        # consecutive VALID all-dead polls (debounce)
        while not self.exit_event.is_set():
            await asyncio.sleep(self._DL_TICK)
            try:
                if not self.slot_data or not self.death_link_on:
                    prev_living, wipe_polls = None, 0
                    await asyncio.sleep(2.0)
                    continue
                if self.save_delta is None:
                    prev_living, wipe_polls = None, 0
                    continue
                in_b = await self._in_battle()
                living, valid = await self._read_party_living(in_b)
                self._dl_fail_streak = 0     # read succeeded -> reset backoff
                if not valid:
                    # unpopulated/garbage records (battle-START copy window, save
                    # block mid-rewrite): every field reads 0, which is NOT a wipe.
                    # One such poll sent a false DeathLink live 2026-07-29; discard.
                    prev_living, wipe_polls = None, 0
                    continue
                if self._dl_pending:
                    self._dl_pending = False
                    if living:
                        n = await self._apply_deathlink(in_b, living)
                        if n >= len(living):        # we wiped the whole party:
                            self._dl_guard = True   # don't bounce it back out
                            # arm the limbo recovery net -- the engine cannot
                            # resolve a battle in which nobody can act
                            self._dl_wipe_latch = True
                    prev_living, wipe_polls = None, 0
                    continue
                if living:
                    # _dl_limbo: we broke a limbo battle by reviving someone at
                    # 1 HP. The game over that follows IS the received death, so
                    # hold the guard until the party is genuinely back on its feet
                    # (2+ alive on the field = a real revive or a reloaded save).
                    if self._dl_limbo and not in_b and len(living) >= 2:
                        self._dl_limbo = False
                    if not self._dl_limbo:
                        self._dl_guard = False
                    wipe_polls = 0
                elif prev_living and not self._dl_guard:
                    # all dead on a valid read: demand _WIPE_CONFIRM consecutive
                    # polls (~1.5s) before believing it -- a real wipe persists
                    # through the game-over fade, a transition glitch doesn't
                    wipe_polls += 1
                    if wipe_polls >= self._WIPE_CONFIRM:
                        self._dl_guard = True       # one send per wipe
                        wipe_polls = 0
                        logger.info("  [death_link] party wiped -- sending "
                                    "DeathLink")
                        await self.send_death(
                            f"{self.player_names.get(self.slot, 'The party')}'s "
                            f"party was wiped out")
                    continue                        # keep prev_living latched
                prev_living = len(living) if living else 0
            except Exception as e:
                # A bad/relocating save_delta makes the party read fail every tick
                # (Invalid address). Back off exponentially (cap 30s) and throttle the
                # log so a wedged read doesn't flood -- instead of the old fixed 1s
                # retry that spammed one line per tick forever (2026-07-23).
                self._dl_fail_streak += 1
                n = self._dl_fail_streak
                # These "Invalid address"/TimeoutError reads are the EXPECTED
                # transient every battle/map transition (the save delta relocates)
                # and the streak resets the moment a read succeeds, so info-level
                # re-floods ~3 lines per battle. Keep them for troubleshooting at
                # debug; escalate to a visible warning only if a read stays wedged
                # (streak climbing past the backoff cap = a genuinely stuck bridge).
                if n <= 3 or n % 20 == 0:
                    (logger.warning if n >= 20 else logger.debug)(
                        f"  [death_link_loop] {e!r}"
                        + (f" (x{n})" if n > 3 else ""))
                prev_living, wipe_polls = None, 0
                await asyncio.sleep(min(30.0, 2.0 ** min(n, 5)))

    # ---------------- shop AP stock (sequential AP purchases in stores) ----------------
    async def _patch_mutate(self, patch, new):
        """Swap a DataPatch's `patched` bytes and rewrite its located copies. The
        OLD bytes are remembered in patch.stale so reconcile() recognizes a
        save-state image taken before the mutation and re-applies instead of
        rescanning. Mutations therefore survive save/load: a revert to vanilla
        or to any stale version is rewritten with the current bytes."""
        old = patch.patched
        if new == old:
            return
        patch.stale.append(old)
        patch.patched = new
        # A patch born as a pure locator (patched == vanilla_sig, e.g. the dyn
        # name slots waiting for their first authored name) stops being a noop
        # the moment it is mutated -- recompute, or the patch loop skips it
        # forever (is_noop is otherwise only set in __init__).
        patch.is_noop = patch.vanilla_sig == patch.patched
        # _write_all so per-copy policies apply (ShopBankPatch holds the
        # inventory copy at vanilla); base DataPatch writes `patched` everywhere.
        await patch._write_all(self.psp)

    async def _shop_render(self, patch, shop):
        """Re-render `shop`'s AP tail in the boot-patched shops table so the
        shelf shows exactly its UNSOLD offers.

        rando.render_shop_ap_tail is a total function of (placeholder ids, sold
        rows): it sizes the store to base_width + unsold and writes the
        survivors just past the normal stock. Selling any row -- first, middle or
        last -- shrinks the store by one row and slides the rest down, and
        re-running it changes nothing. That is why the post-sale path and the
        reconnect-reconcile path are the same call.

        HINT rows sit in the SAME tail, after the offers: one gid list, one sold
        set, so a hint bought between two offers slides the survivors down
        exactly like an offer would. Their indices are offset by the number of
        offer rows, which is also how the store's base width was computed at
        generation.

        A seed generated before parallel offers ships no base width; those have
        exactly one row, so they take the old delist path unchanged."""
        rows = self.shop_rows.get(shop) or []
        hints = self.hint_rows.get(shop) or []
        if not rows and not hints:
            return
        block = bytearray(patch.patched)
        if shop in self._shop_base:
            gids = ([g for _c, g, _p in rows]
                    + [g for _c, g, _p, _l, _i in hints])
            sold = (self._shop_sold_rows(shop)
                    | {len(rows) + k for k in self._hint_done_rows(shop)})
            RANDO.render_shop_ap_tail(block, shop, gids,
                                      self._shop_base[shop], sold=sold)
            self._hint_rendered[shop] = self._hint_done_rows(shop)
        elif rows and self._shop_sold(shop, 0):
            RANDO.delist_shop_ap_slot(block, shop, (rows[0][0], rows[0][1]))
        if bytes(block) != patch.patched:
            await self._patch_mutate(patch, bytes(block))

    async def _shop_refresh_banks(self):
        """Re-author the shop NAME/DESC text banks so each shop shows its CURRENT
        (next unsold) offer. Rebuilds the bank payloads and mutates the existing
        located DataPatches in place."""
        try:
            for np in (self._build_shop_name_patches()
                       + self._build_shop_desc_patches()):
                p = next((q for q in self._extra_patches if q.name == np.name), None)
                if p is None:
                    continue
                if len(np.patched) != len(p.patched):
                    # name-bank build style flipped (re-layout <-> fixed-budget
                    # fallback) between connect and refresh; sizes differ, so
                    # in-place mutation is impossible -- keep the stale name.
                    logger.info(f"  [shop] {np.name}: refresh size mismatch, "
                                f"kept previous")
                    continue
                await self._patch_mutate(p, np.patched)
        except Exception as e:
            logger.info(f"  [shop] bank refresh failed: {e!r}")

    async def _hint_purchase(self, patch, shop, k, qty, price):
        """Consume a bought HINT row and return the gil refund owed.

        The row's product is a tracker tile: scout the locations in it that this
        slot owns and has not found yet, as a HINT rather than a plain scout
        (create_as_hint=2 -- recorded for this player, not broadcast to
        everyone's chat, same mode _shop_hint_loop uses). Only then is the sale
        recorded and the row dropped: if the scout cannot be sent, the player
        gets their gil back instead of paying for nothing."""
        try:
            _cat, _gid, _p, label, _lids = self.hint_rows[shop][k]
        except (KeyError, IndexError):
            return qty * price
        if self._hint_done(shop, k):
            # Stale shop menu (the row was already gone) or a tile that emptied
            # while the list was open -- nothing left to reveal, refund the lot.
            logger.info(f"  [hint] shop {shop} hint {k} already spent -- "
                        f"refunding {qty * price}")
            return qty * price
        wanted = self._hint_unhinted(shop, k)
        if not wanted:
            logger.info(f"  [hint] {label}: nothing left to reveal -- "
                        f"refunding {qty * price}")
            self._hint_bought.add((shop, k))
            await self._shop_render(patch, shop)
            return qty * price
        try:
            await self.send_msgs([{"cmd": "LocationScouts",
                                   "locations": wanted, "create_as_hint": 2}])
        except Exception as e:
            logger.info(f"  [hint] {label}: hint send failed ({e!r}) -- "
                        f"refunding {qty * price}")
            return qty * price
        self._hint_bought.add((shop, k))
        # The open shop list keeps drawing this row until the menu is reopened,
        # and it re-reads the text bank every frame -- so relabel it "Sold Out"
        # until the party leaves the counter rather than letting it repaint as
        # the placeholder item (see SOLD_NAME).
        self._hint_sold_recent.add((shop, k))
        await self._hint_store_add(shop, k)
        logger.info(f"  [hint] bought {HINTS.shelf_name(label)} for {price} gil "
                    f"-> hinted {len(wanted)} location(s) in {label}")
        # qty copies of ONE row: one hint, the rest refunded.
        await self._shop_render(patch, shop)
        await self._shop_sync_masks(False)
        await self._shop_refresh_banks()
        self.refresh_shops()
        return (qty - 1) * price

    async def _shop_sync_masks(self, in_bonus):
        """Equip-mask leg of [[placeholder-name-collision]]: the bake zeroes the
        weapon/armor AP placeholders' equip masks (dupe guard -- an EQUIPPED
        copy survives the purchase strip), but those ids are real vanilla items
        that bonus dungeons drop natively, and the zero made a legit drop
        unequippable for the whole seed (live 2026-07-21: Lightbringer,
        Deathbringer).

        Restore the VANILLA masks while INSIDE a bonus dungeon (AP shops exist
        only in overworld towns, so nothing is buyable there) and for any
        placeholder gid the player legitimately holds -- in the inventory (a
        drop, chest or another town's shelf; since v202 those are common),
        equipped on the party, or whose shop is SOLD OUT (no dupe left to
        guard). Everything else stays zeroed. Mutates the boot-patched
        shuffle:weapons/armor tables so reconcile keeps the state across
        save/load; rerun on every bonus-dungeon toggle, first tick and
        sell-out."""
        wp = next((p for p in self._patches if p.name == "shuffle:weapons"), None)
        ap = next((p for p in self._patches if p.name == "shuffle:armor"), None)
        if wp is None or ap is None:
            return
        if in_bonus:
            restore = set(self._shop_equip_gids)
        else:
            # Per ROW, not per shop: each parallel offer has its own placeholder
            # id, so a sold row's id has no dupe left to guard even while its
            # neighbours are still buyable. SHARED tails (v2): the same gid
            # backs a row in several stores, so it stays guarded until EVERY
            # row wearing it -- offer or hint, any store -- is off the shelf.
            unsold = set()
            if self._shared_tails:
                unsold = {(cat, gid) for s, rws in self.shop_rows.items()
                          for k, (cat, gid, _pr) in enumerate(rws)
                          if not self._shop_sold(s, k)}
                unsold |= {(cat, gid) for s, rws in self.hint_rows.items()
                           for k, (cat, gid, _pr, _l, _i) in enumerate(rws)
                           if not self._hint_done(s, k)}
            restore = {(cat, gid) for s, rws in self.shop_rows.items()
                       for k, (cat, gid, _pr) in enumerate(rws)
                       if (cat, gid) in self._shop_equip_gids
                       and self._shop_sold(s, k) and (cat, gid) not in unsold}
            # Spent hint rows the same way: their placeholder is a real weapon
            # or shield once the row is off the shelf, so a drop of it must be
            # equippable again.
            restore |= {(cat, gid) for s, rws in self.hint_rows.items()
                        for k, (cat, gid, _pr, _l, _i) in enumerate(rws)
                        if (cat, gid) in self._shop_equip_gids
                        and self._hint_done(s, k) and (cat, gid) not in unsold}
            if self.save_delta is not None:
                inv = await self.psp.read(self.sa(D.INVENTORY_BASE_SA),
                                          D.INV_RECORD_SIZE * 0x80)
                for i in range(0, len(inv), D.INV_RECORD_SIZE):
                    if (inv[i], inv[i + 1]) in self._shop_equip_gids:
                        restore.add((inv[i], inv[i + 1]))
                for row in range(4):
                    blk = await self.psp.read(
                        self.sa(D.party_addr_sa(row, D.EQUIP_OFF)), D.EQUIP_LEN)
                    restore.add((D.CAT_WEAPON, blk[0]))
                    restore.add((D.CAT_ARMOR, blk[3]))
        weapons, armor = bytearray(wp.patched), bytearray(ap.patched)
        if RANDO.set_shop_ap_masks(weapons, armor, restore,
                                   equip_gids=self._shop_equip_gids):
            await self._patch_mutate(wp, bytes(weapons))
            await self._patch_mutate(ap, bytes(armor))
            names = sorted(D.WEAPONS.get(g, f"w{g}") if c == D.CAT_WEAPON
                           else D.ARMOR.get(g, f"a{g}")
                           for c, g in restore & self._shop_equip_gids)
            logger.info("  [shop] placeholder equip masks: "
                        + (f"restored {', '.join(names)}" if names
                           else "all zeroed (dupe guard)"))

    async def _shop_sync_prices(self):
        """Shared tails (v2): stamp the STANDING town's AP row prices onto the
        placeholder item records. Rows in different stores share gids, and a
        price lives on the item record, so the tables can carry only one town's
        prices at a time.

        Mutates the boot-patched shuffle:weapons/armor/item_buy_prices tables
        (same choke point as _shop_sync_masks), so reconcile keeps the stamp
        across save/load and the bake's last-store-wins price is corrected the
        moment a town is entered. Timing is what makes this safe: the Buy list
        SNAPSHOTS prices at dialog open but the charge reads the live record
        (both proven 2026-08-16), and this runs on the street map-id edge --
        seconds before any counter -- or at worst on the store-id fallback,
        which the shop UI sets before the Buy list can open."""
        if not self._shared_tails or self._cur_town is None:
            return
        if self._town_prices_stamped == self._cur_town:
            return
        tables = {"weapons": (2, 28, 20), "armor": (3, 28, 20),
                  "item_buy_prices": (1, 16, 0)}
        patches = {nm: next((p for p in self._patches
                             if p.name == f"shuffle:{nm}"), None)
                   for nm in tables}
        if any(p is None for p in patches.values()):
            return                      # boot patches not built yet; next tick
        blocks = {nm: bytearray(p.patched) for nm, p in patches.items()}
        for s, rws in list(self.shop_rows.items()) + [
                (s, [(c, g, pr) for (c, g, pr, _l, _i) in rws])
                for s, rws in self.hint_rows.items()]:
            if D.SHOP_CITY[s] != self._cur_town:
                continue
            for cat, gid, price in rws:
                for nm, (tcat, stride, poff) in tables.items():
                    if cat != tcat:
                        continue
                    rec = (gid - 1) * stride + poff
                    blk = blocks[nm]
                    if 0 <= rec + 3 <= len(blk):
                        blk[rec:rec + 3] = int(price).to_bytes(3, "little")
        changed = False
        for nm, p in patches.items():
            if bytes(blocks[nm]) != p.patched:
                await self._patch_mutate(p, bytes(blocks[nm]))
                changed = True
        self._town_prices_stamped = self._cur_town
        if changed:
            logger.info(f"  [shop] AP row prices stamped for town "
                        f"{self._cur_town}")

    async def _in_bonus_dungeon(self):
        """True while the party is inside a Soul-of-Chaos bonus dungeon.

        Same gate as _bonus_dyn_loop.dungeon_idx, minus the dungeon identity:
        live encounter map-id >= BONUS_MAPID_MIN means a bonus floor, and the
        answer LATCHES because Whisperwind has gimmick town/field floors whose
        map-id reads < 0x87 mid-dungeon. Only the true overworld
        (LOADED_MAP_ID_SA == 0) clears the latch -- normal maps are reachable
        only via the overworld. Own latch (not _bonus_dyn_loop's) so the gate
        still works with exclude_bonus_dungeons on, where that loop idles."""
        if self.save_delta is None:
            return self._bonus_latch
        lm = (await self.psp.read(self.sa(D.LOADED_MAP_ID_SA), 1))[0]
        if lm == 0:
            self._bonus_latch = False
        elif (await self.psp.read(self.sa(D.BONUS_MAPID_ADDR), 1))[0] >= D.BONUS_MAPID_MIN:
            self._bonus_latch = True
        return self._bonus_latch

    async def _rune_borrow_zone(self):
        """Should the id-35 display borrow be released right now?

        True  = yes: we are in Whisperwind Cove (RUNE_BORROW_RELEASE_BANDS), the
                only dungeon whose events can grant the real Battery Circuit.
        False = no: overworld, normal maps, or a bonus dungeon that never touches
                the bit -- hold the borrow and keep the counter readable.
        None  = UNKNOWN: make no borrow writes at all this tick.

        Latched exactly like _in_bonus_dungeon (only the true overworld,
        LOADED_MAP_ID_SA == 0, clears it) because Whisperwind has gimmick
        town/field floors whose encounter map-id reads < 0x87 mid-dungeon. The
        three-valued return is the fix for the COLD latch: a client started on
        one of those floors would otherwise resolve False and stamp a fake
        possession bit inside the one dungeon this whole mechanism protects.

        BONUS_MAPID_ADDR alone is not enough to answer this -- it is the STALE
        encounter map-id and still reads >= 0x87 after walking out onto the
        overworld.

        The true-overworld test is the FINE map id (FIELD_MAP_ID_SA ==
        OVERWORLD_FIELD_MAP_ID), NOT the coarse LOADED_MAP_ID_SA bucket that
        _in_bonus_dungeon / _bonus_dyn_loop use. Whisperwind Cove contains
        floors whose COARSE id reads 0 -- the overworld value -- and trusting
        that cleared the latch mid-dungeon and re-took the borrow INSIDE
        Whisperwind, setting key id 35 while its dwarf trading quest owns ids
        18-26 (live 2026-08-07: `[bonus_dyn] mailbox disarmed` immediately
        followed by `[keyratio] rune line shown` at 10:46:39, two floors deep,
        ending in a hard freeze on the Carobo row). The coarse bucket is fine
        for loops that only lose a tick to a false clear; it is NOT fine for
        deciding whether to write another quest's possession bit."""
        if self.save_delta is None:
            return None
        fine = await self.psp.read_u32(self.sa(D.FIELD_MAP_ID_SA))
        if fine == D.OVERWORLD_FIELD_MAP_ID:
            self._rune_zone = False
            return False
        mid = (await self.psp.read(self.sa(D.BONUS_MAPID_ADDR), 1))[0]
        if mid >= D.BONUS_MAPID_MIN:
            band = D.bonus_mapid_band(mid)
            # band None = the 0xD2/0xD3 special DLC floors, which sit outside
            # every band -> resolve conservatively to RELEASE.
            self._rune_zone = (band is None
                               or band in D.RUNE_BORROW_RELEASE_BANDS)
        elif self._rune_zone is None:
            # COLD latch on a non-overworld map reading < 0x87: either a normal
            # cave or one of Whisperwind's gimmick floors. Break the tie with
            # the same band vote _bonus_dyn_loop uses for dungeon identity. A
            # vote for the releasing band means we may be standing IN it -> stay
            # UNKNOWN and write nothing; anything else is a normal map, so the
            # line comes up immediately instead of waiting for the overworld.
            # (The floor table is save-persistent, so a vote alone can't prove we
            # are still there -- hence unknown rather than release.)
            if (await self._floor_table_band()) in D.RUNE_BORROW_RELEASE_BANDS:
                return None
            self._rune_zone = False
        return self._rune_zone

    async def _floor_table_band(self):
        """Majority band vote over the bonus floor table -> which bonus dungeon
        the party is in, or was in LAST (the table is save-persistent, so this
        never proves the party is still there). Same read and vote as
        _bonus_dyn_loop.dungeon_idx. Returns a band index or None."""
        tbl = await self.psp.read(self.sa(D.BONUS_FLOOR_TABLE_SA),
                                  D.BONUS_BAND_VOTE * D.BONUS_FLOOR_STRIDE)
        votes = [b for b in
                 (D.bonus_mapid_band(tbl[k * D.BONUS_FLOOR_STRIDE
                                         + D.BONUS_FLOOR_MAPID_OFF])
                  for k in range(D.BONUS_BAND_VOTE))
                 if b is not None]
        return max(set(votes), key=votes.count) if votes else None

    async def _other_bonus_keys_held(self):
        """True if the player holds any bonus-dungeon key item OTHER than the
        borrowed id 35. Only used to adjudicate a legacy save (written before the
        ownership shadow existed), where a set id-35 bit carries no evidence of
        whose it is: a player who genuinely earned Battery Circuit is
        overwhelmingly likely to hold other parts from the same minigame."""
        want = {}
        for kid in D.RUNE_BORROW_PEER_KEY_IDS:
            addr, mask = D.key_item_bit(kid)
            want[addr] = want.get(addr, 0) | mask
        for addr, mask in want.items():
            if (await self.psp.read(self.sa(addr), 1))[0] & mask:
                return True
        return False

    async def _shop_loop(self):
        """Detect AP shop purchases. Each shop lists its offers IN PARALLEL.

        Every offer is its own shelf row carrying its own placeholder game item
        (rando.pick_seed_placeholders) with no other legitimate source: not in
        the AP pool, not stocked by any vanilla or shuffled store. Price, name
        and description all live on the ITEM record rather than the shop row, so
        one id per row is what lets three offers sit side by side with three
        names and three prices -- and it is also what makes a purchase
        attributable. Every price is baked at generation; nothing is repriced at
        runtime. Buying a row lands its placeholder in the inventory; we poll for
        the (cat, id) pair, remove it, send THAT ROW's check, and re-render the
        shelf without it.

        ATTRIBUTION (v202): the baked BUYB mailbox (shop_buy_mailbox) records
        every town-shop purchase commit as (store_id, type, cat, gid, qty, seq)
        -- store_id is the game's shop-def index, mapped back to the AP shop
        ordinal via rando._DEF_IDX, and gid names the row within that shop. Only
        a placeholder bought IN ITS OWN AP STORE counts; the same item dropped by
        a monster, found in a chest, or bought anywhere else is simply left in
        the bag (this replaced the old gil-drop inference, which could neither
        attribute the shop nor tell a drop from a purchase -- see
        [[shop-buy-mailbox]]).

        Multi-buys (qty > 1) are qty copies of ONE row, so they send one check
        and refund the extra copies. A re-buy of an already-sold row (stale shop
        menu open while the row was removed) is refunded in full.

        HINT rows (hints.py) are read from the same mailbox and are the same
        shape of purchase, minus the location: buying one scouts its tracker
        tile as a hint, records the sale in the slot's DataStorage, and drops
        the row."""
        if not self.shop_slots and not self.hint_rows:
            return
        # the shops DataPatch is built by _boot_patch_loop; wait for it
        patch = None
        while patch is None and not self.exit_event.is_set():
            patch = next((p for p in self._patches if p.name == "shuffle:shops"), None)
            if patch is None:
                await asyncio.sleep(0.5)
        if self.exit_event.is_set():
            return
        # Reconnect: rows already checked on the server stay sold for good, then
        # re-render every shelf. Same call as the post-sale path -- the render is
        # total and idempotent, so there is no separate "sold out" or "partially
        # sold" case to get wrong.
        for (s, _cat, _gid, prices) in self.shop_slots:
            for k in range(len(prices)):
                lid = ID.shop_loc_id(s, k)
                if lid in self.checked_locations:
                    self.sent_locations.add(lid)
        # Hint-only stores have no offer row to reconcile, but still need their
        # tail rendered (a row bought in an earlier session, or one whose tile
        # is now fully found, must not come back).
        for s in sorted(set(self.shop_rows) | set(self.hint_rows)):
            await self._shop_render(patch, s)
        self._hint_dirty = False
        # store_id (game shop-def index, s0+0x7064 at the commit hook) -> AP
        # shop ordinal. Purchases in any other store -- magic shops, the
        # caravan, DLC bonus-dungeon shops -- simply don't map and are ignored.
        def_to_shop = {RANDO._DEF_IDX[(city, row)]: s
                       for s, (city, row, _ph) in enumerate(RANDO.SHOP_AP_SLOTS)}
        state = {"head": None}

        async def tick():
            if self.save_delta is None:
                return
            in_bonus = await self._in_bonus_dungeon()
            # NAME BLEED: the name/desc bank rewrite is per-item-id and global,
            # and placeholder ids are real vanilla items (drops, chests, DLC
            # stock, and -- since v202 -- other towns' shuffled shelves). Hold
            # the banks at VANILLA everywhere EXCEPT inside a normal town
            # (coarse LOADED_MAP_ID 2; overworld 0, dungeons 1 -- live-mapped
            # 2026-08-01): that keeps AP names on the town shop shelves where
            # they belong, while a battle drop of the same id announces its
            # real vanilla self (the "skeletons dropped a Lute Tablet" report).
            # Bonus dungeons stay vanilla even on their gimmick town floors
            # (the _in_bonus_dungeon latch). The builders read _bank_vanilla.
            coarse = (await self.psp.read(self.sa(D.LOADED_MAP_ID_SA), 1))[0]
            # All shop UIs and the inventory menu render from the SAME (lowest)
            # resident bank copy -- the shop list merely SNAPSHOTS it when its
            # dialog opens (live probing 2026-08-05; the higher copy is the
            # unused second-language region). So the authoring window is INSIDE
            # a shop building only (fine FIELD_MAP_ID in the three generic
            # shop-interior maps -- full 17-shop tour captured 2026-08-05, see
            # D.SHOP_INTERIOR_FIELD_MAP_IDS): shelves show AP names at the
            # counter, the street/world
            # inventory shows real names. Residual: the inventory opened AT the
            # counter still bleeds.
            fine = struct.unpack("<I", await self.psp.read(
                self.sa(D.FIELD_MAP_ID_SA), 4))[0]
            # Shared tails (v2): latch WHICH town the party is in. Primary is
            # the town STREET map id (unique per town, crossed seconds before
            # any counter); fallback is the live store-id field, which the
            # shop UI sets at the Buy/Sell prompt -- covers a save loaded
            # inside a shop building where no street was ever crossed. Both
            # are edges: the store-id field is STALE outside shops, so it is
            # only consulted while a latch is missing, never to move one.
            if self._shared_tails and not in_bonus:
                town = D.TOWN_STREET_MAP_IDS.get(fine)
                if (town is None and self._cur_town is None and coarse == 2
                        and fine in D.SHOP_INTERIOR_FIELD_MAP_IDS):
                    sid = struct.unpack("<I", await self.psp.read(
                        self.sa(D.SHOP_STORE_ID_SA), 4))[0]
                    town = next((city for (city, row), idx
                                 in RANDO._DEF_IDX.items() if idx == sid), None)
                if town is not None and town != self._cur_town:
                    self._cur_town = town
                    logger.info(f"  [shop] town latch -> {town}")
                    await self._shop_refresh_banks()
                # Stamp (or re-try, if the boot patches were not built at the
                # edge) the standing town's row prices; no-op once stamped.
                if (self._cur_town is not None
                        and self._town_prices_stamped != self._cur_town):
                    await self._shop_sync_prices()
            want_vanilla = (in_bonus or coarse != 2
                            or fine not in D.SHOP_INTERIOR_FIELD_MAP_IDS)
            if want_vanilla != self._bank_vanilla:
                self._bank_vanilla = want_vanilla
                if want_vanilla:
                    # Left the counter: no shop list can still be drawing a row
                    # bought in there, so this visit's "Sold Out" labels retire
                    # and those ids go back to their real names.
                    self._shop_sold_recent.clear()
                    self._hint_sold_recent.clear()
                await self._shop_refresh_banks()
                logger.info("  [shop] shop item names "
                            + ("held at vanilla" if want_vanilla
                               else "AP-authored (shop interior)"))
                # entering/leaving town can load a bank copy the boot scan never
                # saw (weapons/armor name banks) -- have the boot loop re-sweep
                self._float_rescan = True
                self._masks_synced = False
            # Re-sync on a timer as well as on the map/bank edge: a placeholder
            # gid the player picks up in the FIELD (chest, drop, AP grant) only
            # becomes equippable once the inventory scan sees it, and the edge
            # alone left a legit Genji Armor unequippable for six minutes --
            # until the party happened to walk into a shop (live 2026-08-12,
            # Prime). set_shop_ap_masks writes nothing when the restore set is
            # unchanged, so a quiet tick costs the two reads and no patch churn.
            self._masks_tick += 1
            if not self._masks_synced or self._masks_tick >= _MASK_RESYNC_TICKS:
                self._masks_synced = True
                self._masks_tick = 0
                await self._shop_sync_masks(in_bonus)
            if self._hint_dirty:
                # A hint bought in an earlier session (DataStorage) or a tile
                # that just became fully found: both retire a row, and neither
                # goes through the purchase path below.
                self._hint_dirty = False
                changed = False
                for hs in sorted(self.hint_rows):
                    if self._hint_done_rows(hs) != self._hint_rendered.get(hs):
                        await self._shop_render(patch, hs)
                        changed = True
                if changed:
                    await self._shop_refresh_banks()
                    self.refresh_shops()
            mb = await self._buy_mailbox()
            if mb is None:
                return      # pre-feature bake: no hook on disc, nothing to read
            head = struct.unpack("<H", await self.psp.read(
                mb + IP.BUYMB_HEAD_OFF, 2))[0]
            last = state["head"]
            if last is None or head < last:
                # first sight, or a reboot/savestate reverted the mailbox to its
                # baked zeros. Consume whatever the ring still holds (sends are
                # idempotent via sent_locations; item removal keys off the live
                # inventory), never phantom "future" entries.
                last = max(0, head - IP.BUYMB_RING_ENTRIES)
            if head == last:
                state["head"] = head
                return
            if head - last > IP.BUYMB_RING_ENTRIES:
                logger.warning(f"  [shop] buy mailbox overflow ({head - last} "
                               f"buys since last poll) -- oldest "
                               f"{head - last - IP.BUYMB_RING_ENTRIES} lost")
                last = head - IP.BUYMB_RING_ENTRIES
            ring = await self.psp.read(mb + IP.BUYMB_RING_OFF,
                                       IP.BUYMB_RING_ENTRIES * 8)
            state["head"] = head
            # 8-byte ring entry: store, type, cat, gid u8s + qty u16 + its own
            # u16 sequence stamp. eseq vs the low 16 bits of the running head
            # detects a lapped (overwritten) slot; qty == 0 marks a slot the
            # hook never wrote.
            for seq in range(last, head):
                store, typ, cat, gid, qty, eseq = struct.unpack_from(
                    "<BBBBHH", ring, (seq % IP.BUYMB_RING_ENTRIES) * 8)
                if eseq != (seq & 0xFFFF) or qty == 0:
                    continue                 # slot lapped by a newer purchase
                s = def_to_shop.get(store)
                if s is None:
                    continue                 # magic/caravan/DLC store
                if in_bonus:
                    continue                 # unreachable for town shops; belt+braces
                # WHICH ROW: every offer of a shop carries a distinct
                # placeholder id, so the mailbox's (store, cat, gid) names one
                # row exactly. Anything else bought here is ordinary stock.
                k = self._shop_gid_row.get((s, cat, gid))
                hk = None if k is not None else self._hint_gid_row.get((s, cat, gid))
                if k is None and hk is None:
                    continue                 # ordinary shuffled stock, not a tail row
                price = (self.shop_rows[s][k][2] if k is not None
                         else self.hint_rows[s][hk][2])
                # eat exactly the copies this purchase added; pre-owned copies
                # of the same item (drops, other shops) stay in the bag
                inv = await self.psp.read(self.sa(D.INVENTORY_BASE_SA),
                                          D.INV_RECORD_SIZE * 0x80)
                rec_off = next((i for i in range(0, len(inv), D.INV_RECORD_SIZE)
                                if inv[i] == cat and inv[i + 1] == gid), None)
                if rec_off is not None:
                    have = inv[rec_off + 2]
                    if qty >= have:
                        await self.psp.write(
                            self.sa(D.INVENTORY_BASE_SA) + rec_off,
                            bytes(D.INV_RECORD_SIZE))
                    else:
                        await self.psp.write(
                            self.sa(D.INVENTORY_BASE_SA) + rec_off + 2,
                            bytes([have - qty]))
                if hk is not None:
                    refund = await self._hint_purchase(patch, s, hk, qty, price)
                elif self._shop_sold(s, k):
                    # Stale shop menu: the row was already bought and removed,
                    # but the open list still offered it. Refund the lot.
                    refund = qty * price
                    logger.info(f"  [shop] shop {s} offer {k} already sold -- "
                                f"refunding {refund}")
                else:
                    lid = ID.shop_loc_id(s, k)
                    self.sent_locations.add(lid)
                    # The shop list snapshotted its rows at dialog open, so this
                    # row stays drawn until the menu is reopened. Label it "Sold
                    # Out" until the party leaves the counter (_shop_bank_rows)
                    # rather than letting it repaint as the placeholder item.
                    self._shop_sold_recent.add((s, k))
                    await self.check_locations([lid])
                    # qty copies of ONE row: one check, the rest refunded. Each
                    # row's price is baked on its own item record, so the game
                    # charged exactly qty x this row's price -- no settling.
                    refund = (qty - 1) * price
                    bought = self.shop_desc.get((s, k), f"offer {k}")
                    logger.info(f"  [shop] purchased {bought} -> 1 check sent"
                                + (f" (gil adjustment {refund:+d})" if refund else ""))
                    await self._shop_render(patch, s)
                    # that row's id has no buyable dupe left to guard
                    await self._shop_sync_masks(False)
                    await self._shop_refresh_banks()
                if refund:
                    cur = await self.psp.read_u32(self.sa(D.GIL_ADDR_SA))
                    await self.psp.write_u32(self.sa(D.GIL_ADDR_SA),
                                             max(0, min(cur + refund, D.GIL_MAX)))

        await self._poll(SHOP_POLL_S, "shop_loop", tick)

    # ---------------- persistence ----------------
    # NOTE: there is intentionally NO external received-items counter file anymore.
    # The grant counter lives in the SAVE (D.RECEIVED_COUNTER_ADDR_SA) so it rolls back
    # with the items on death/load; the server is the durable source of the item
    # list. Persisting the counter outside the save was the item-loss bug.

    # ------------- debugger-socket utilities (defensive teardown only) -------------
    # The client no longer arms exec breakpoints anywhere (chest detection is
    # poll-based; see _chest_poll_loop). These helpers remain so _guard and the
    # atexit hook can clear a stale breakpoint left by an older client or an RE
    # session and unhalt the CPU -- a leftover hook freezes the game hard.
    async def _bp_rpc(self, event, **kw):
        try:
            # Local debugger socket: a healthy reply lands in ms. The only reason to
            # wait is a DROPPED reply (PPSSPP eats one when the hot give-item fn
            # re-halts on resume). A long timeout = game frozen at the breakpoint for
            # the whole wait (the chest-open lag). Keep it short -> self-heals fast.
            return await asyncio.wait_for(self.psp_bp.rpc(event, **kw), 0.3)
        except (asyncio.TimeoutError, RuntimeError):
            return None

    async def _resume(self):
        dead = 0
        for _ in range(40):
            await self._bp_rpc("cpu.resume")
            st = await self._bp_rpc("cpu.status")
            if st and not st.get("stepping"):
                return
            if st is None:
                # No status reply at all: the debugger connection is dead
                # (PPSSPP closing/gone). Spinning the full 40 rounds here --
                # each a pair of 0.3 s dead-socket timeouts -- was ~25 s of
                # the frozen-on-close wedge; bail after a few dead rounds.
                dead += 1
                if dead >= 3:
                    return
            else:
                dead = 0
            await asyncio.sleep(0.03)


    async def _bonus_dyn_mailbox(self):
        """On-disc BDC1 bonus-chest mailbox cave: magic 'BDC1', remaining u8
        @+4 (client-armed strips left), head u8 @+5 (cave-owned ring cursor),
        hit-diagnostic u16 @+6, ring u16[16] @+8 (stripped chest idxs, 0xFFFF
        = empty), last-raw-idx u16 @+40, next-box-name sid u16 @+42 (the cave
        shows this authored name in the in-game chest box)."""
        return await self._find_mailbox(b"BDC1", "_bdc_mb", "bonus_dyn")

    async def _dyn_arm_name(self, dg, nxt):
        """Author the NEXT dynamic chest's box name into the on-disc wide dyn
        slots and return the string id to arm (0 = leave the vanilla box).

        The two slots (sids base+R, base+R+1; tome_names.DYN_SLOTS) are
        PING-PONGED by ordinal parity so the slot a still-open reward box is
        rendering from is never rewritten under the player.

        ONLY the current slot is authored. The old version ALSO pre-authored
        the FOLLOWING chest's name into the other slot in the same mutation,
        which with DYN_SLOTS == 2 defeats the entire ping-pong: the follower
        lands on slot (nxt+1) % 2, and (nxt+1) % 2 == (nxt-1) % 2, i.e. exactly
        the slot the chest the player JUST opened is rendering from. Within one
        poll tick of opening a chest its box name was rewritten underneath them
        with a later chest's item, so the game showed a name for a chest they
        had not opened yet (live 2026-08-09, Prime: box read "Spell Tome:
        Invisira", Whisperwind chest 7, while the check being sent was chest 6).
        The AP side was never wrong -- only the rendered name.
        Pre-authoring bought nothing anyway: this function mutates the slot
        bytes and RETURNS the sid in the same call, and the caller writes the
        mailbox's next_sid immediately after, so a published sid can never
        point at unauthored bytes. Leave the other slots exactly as they are.

        Authoring goes through the floating
        DataPatch machinery (self._dyn_slot_patch: sentinel-signature locate,
        per-tick reconcile, rescan on heap relocation), the exact mechanism the
        shop name banks use. An unlocated/raced slot renders the baked 'AP
        item' sentinel -- honest, never a wrong-looking vanilla name."""
        patch = self._dyn_slot_patch
        pair = self._dyn_names.get((dg, nxt))
        if patch is None or pair is None:
            return 0
        slot = nxt % TN.DYN_SLOTS
        entry = TN.dyn_slot_entry(
            False, TN.render_remote(pair[0], pair[1], TN.DYN_SLOT_GLYPHS))
        width = len(entry)                 # every NAME entry is fixed-size
        # Start from what is CURRENTLY in the bank so the other slots keep the
        # bytes a live reward box may still be rendering. Only a first arm (or
        # an unexpected width) falls back to an all-sentinel blob.
        cur = patch.patched or b""
        if len(cur) != width * TN.DYN_SLOTS:
            cur = TN.dyn_slot_entry(False) * TN.DYN_SLOTS      # sentinel
        entries = [cur[i * width:(i + 1) * width]
                   for i in range(TN.DYN_SLOTS)]
        entries[slot] = entry
        patched = b"".join(entries)
        if patched != patch.patched:
            first = not patch.addrs
            await self._patch_mutate(patch, patched)
            if first:
                self._float_rescan = True     # locate the bank copies now
        if self._dyn_slot_logged != (dg, nxt):
            self._dyn_slot_logged = (dg, nxt)
            # Phrased as a PREVIEW on purpose. The old wording ("slot N armed:
            # your Angel's Ring") reads exactly like a grant, and a player who
            # saw it, then opened the nearby STATIC boss chest, reported the ring
            # as a lost item -- it was never opened at all (live 2026-08-08,
            # Prime). Say plainly that this is the name the next chest will show.
            logger.info(f"  [bonus_dyn] next dynamic chest here will contain "
                        f"{pair[0]} {pair[1]} (dungeon {dg}, chest {nxt + 1}) "
                        f"-- not received until you open it [slot {slot}]")
        return self._remote_base + len(self._remote_names) + slot

    async def _bonus_dyn_loop(self):
        """AP-ify DYNAMIC (procedural) Soul-of-Chaos bonus-dungeon chests via the
        on-disc BDC1 mailbox (bonus_dyn_chests detour). These chests regenerate
        every entry, set NO CHEST_OPEN_BF bit (live-verified), and are invisible
        to _chest_poll_loop.

        The original exec-bp version of this loop NEVER fired in player sessions:
        launcher.patch_ini forces FastMemoryAccess=True (framerate), under which
        PPSSPP debugger breakpoints are silently dead (live-confirmed 2026-07-19,
        Earthgift -- "armed" hooks, vanilla loot, zero hits). Detection now runs
        IN-GAME: while `remaining` (mailbox u8 @+4) is nonzero, the detour strips
        any chest grant whose idx is NOT a bonus boss chest (252-267) and pushes
        the idx to the ring; boss chests always grant vanilla and stay
        _chest_poll_loop's. (Arming happens only inside a bonus dungeon, where
        252-267 are the only reachable static chests -- so no assumption about
        what idx procedural chests carry.)

        Each tick: (1) consume ring entries -> send the current dungeon's next
        dynamic AP location (persistence rides AP sent_locations; the AP item is
        delivered by _grant_loop); (2) re-arm remaining = cap - checked for the
        dungeon we're standing in (0 elsewhere). Rewritten every tick because a
        savestate load reverts the mailbox; the cave-side decrement means a burst
        of opens between ticks can't overrun the cap."""

        latched = None   # dungeon idx held across GIMMICK floors: Whisperwind
        # has town/field bonus floors whose live encounter mapid reads < 0x87,
        # which made the old per-floor gate disarm MID-DUNGEON (live 2026-07-20:
        # "mailbox disarmed" on a forest floor -> chests 8+ fell to vanilla).

        async def dungeon_idx():
            """Current bonus dungeon (0..3), LATCHED: resolved on a bonus floor
            and held until the TRUE overworld (LOADED_MAP_ID_SA == 0) clears it
            -- gimmick floors (live mapid < 0x87 mid-dungeon) keep the latch.

            RESOLUTION = the per-dungeon MAPID BAND (D.BONUS_MAPID_BANDS,
            live-dumped 2026-07-21): each dungeon draws its floor mapids from a
            fixed contiguous range whose width equals its floor count. Majority
            band vote over the floor table's first BONUS_BAND_VOTE records
            (always freshly rewritten -- every dungeon has >= 5 floors), with
            the live mapid's own band as tiebreak/fallback. The OLD floor-COUNT
            discriminator (contiguous-prefix over the whole table) was wrong on
            re-visits: the table is SAVE-PERSISTENT and a dungeon only rewrites
            its first <floor count> records, so a bigger dungeon's STALE TAIL
            made every smaller dungeon read as Whisperwind (live 2026-07-21:
            Lifespring -> prefix 40 -> dg=3 'cap reached' -> vanilla chest)."""
            nonlocal latched
            if self.save_delta is None:
                dbg["why"] = "no save_delta"
                return None
            lm = (await self.psp.read(self.sa(D.LOADED_MAP_ID_SA), 1))[0]
            dbg["lm"] = lm
            if lm == 0:                       # true overworld -> left dungeon
                latched = None
                return None
            if latched is not None:           # resolved this entry -> hold it
                dbg["latched"] = latched
                return latched
            mid = (await self.psp.read(self.sa(D.BONUS_MAPID_ADDR), 1))[0]
            dbg["mid"] = mid
            if mid >= D.BONUS_MAPID_MIN:
                tbl = await self.psp.read(self.sa(D.BONUS_FLOOR_TABLE_SA),
                                          D.BONUS_BAND_VOTE * D.BONUS_FLOOR_STRIDE)
                votes = []
                for k in range(D.BONUS_BAND_VOTE):
                    b = D.bonus_mapid_band(
                        tbl[k * D.BONUS_FLOOR_STRIDE + D.BONUS_FLOOR_MAPID_OFF])
                    if b is not None:
                        votes.append(b)
                dbg["votes"] = votes
                dg = None
                if votes:
                    dg = max(set(votes), key=votes.count)
                if dg is None:
                    dg = D.bonus_mapid_band(mid)   # special/ambiguous table
                if dg is not None:
                    latched = dg
                    logger.info(f"  [bonus_dyn] dungeon resolved: dg={dg} "
                                f"(band votes {votes}, live mapid 0x{mid:02x})")
            dbg["latched"] = latched
            return latched

        def next_ordinal(dg):
            """Count of this dungeon's dynamic locations already checked = the next
            0-based ordinal to send (persistence rides AP sent_locations)."""
            return sum(1 for o in range(ID.DYNCHEST_STRIDE)
                       if ID.dyn_chest_loc_id(dg, o) in self.sent_locations)

        armed_remain = -1   # last remaining value written (log arm/disarm edges)
        last_hits = None    # cave-entry diagnostic counter (u8 @+6, wraps)
        dbg = {}            # last raw gate inputs, for the not-arming diagnostic
        last_dbg = None
        self._dyn_slot_logged = None    # last (dg, nxt) logged by _dyn_arm_name
        while not self.exit_event.is_set():
            await asyncio.sleep(CHEST_POLL_S)
            try:
                if not self.bonus_dyn_caps or self.save_delta is None:
                    continue
                mb = await self._bonus_dyn_mailbox()
                if mb is None:
                    continue
                # 0) diagnostics: cave entries + last raw idx seen (pre-gate).
                # A chest open that does NOT move `hits` never reached the
                # chest-handler grant sites at all (different code path).
                diag = await self.psp.read(mb + 6, 1)
                hits = diag[0]
                if last_hits is not None and hits != last_hits:
                    li = await self.psp.read(mb + 40, 2)
                    logger.info(f"  [bonus_dyn] cave hits {hits} "
                                f"(last idx {li[0] | li[1] << 8})")
                last_hits = hits
                # 1) consume stripped-chest ring entries (0xFFFF = empty slot)
                ring = await self.psp.read(mb + 8, 32)
                hits = [k for k in range(16)
                        if (ring[2 * k] | ring[2 * k + 1] << 8) != 0xFFFF]
                # one dungeon resolve per tick: consistent for both the ring
                # consume and the re-arm below (it costs map/floor reads)
                dg = await dungeon_idx()
                for k in hits:
                    if dg is not None:
                        # next_ordinal is re-derived per entry on purpose --
                        # sent_locations grows inside this loop
                        o = next_ordinal(dg)
                        if o < self.bonus_dyn_caps.get(dg, 0):
                            lid = ID.dyn_chest_loc_id(dg, o)
                            if lid not in self.sent_locations:
                                self.sent_locations.add(lid)
                                await self.check_locations([lid])
                                logger.info(
                                    f"Bonus dynamic chest -> check (dungeon "
                                    f"{dg}, chest {o + 1}/"
                                    f"{self.bonus_dyn_caps.get(dg, 0)})")
                    await self.psp.write(mb + 8 + 2 * k, b"\xFF\xFF")
                # 2) re-arm remaining + next box-name sid for the dungeon we're
                # standing in (sid 0 = cave leaves the vanilla box name)
                remain = 0
                sid = 0
                nxt = None if dg is None else next_ordinal(dg)
                if dg is not None:
                    remain = max(0, min(255,
                                        self.bonus_dyn_caps.get(dg, 0) - nxt))
                    if remain:
                        sid = await self._dyn_arm_name(dg, nxt)
                await self.psp.write(mb + 4, bytes([remain]))
                await self.psp.write(mb + 42, struct.pack("<H", sid))
                if (remain > 0) != (armed_remain > 0):
                    logger.info("  [bonus_dyn] mailbox "
                                + (f"armed (remaining {remain})" if remain
                                   else "disarmed"))
                armed_remain = remain
                # Not arming while the map looks like a bonus floor is the failure
                # mode every live bug so far has produced (gimmick floor, phantom
                # floor count, stale latch). Log the raw gate inputs once per
                # distinct state so the next report needs no memory forensics.
                # A plain town/dungeon/overworld map with no bonus-floor signal
                # (no votes, no latch, no candidate dungeon) is the overwhelming
                # majority of ticks and is NOT diagnostic -- only log when the map
                # actually looked like a bonus floor yet failed to arm.
                votes = tuple(dbg.get("votes") or ())
                looked_bonus = bool(votes) or dbg.get("latched") or dg is not None
                if not remain and dbg.get("lm") and looked_bonus:
                    snap = (dbg.get("lm"), dbg.get("mid"), votes,
                            dbg.get("latched"), dg, nxt)
                    if snap != last_dbg:
                        last_dbg = snap
                        cap = 0 if dg is None else self.bonus_dyn_caps.get(dg, 0)
                        if dg is not None and cap and snap[5] >= cap:
                            # normal end-state, NOT a fault: every AP check for
                            # this dungeon is done, so further chests are vanilla
                            logger.info(
                                f"  [bonus_dyn] dungeon {dg} cap reached "
                                f"({cap}/{cap}) -- further chests stay vanilla")
                        else:
                            logger.info(
                                f"  [bonus_dyn] not arming: map={snap[0]} "
                                f"mapid={snap[1]} votes={snap[2]} "
                                f"latch={snap[3]} dg={snap[4]} "
                                f"checked={snap[5]} "
                                f"caps={dict(self.bonus_dyn_caps)}")
            except Exception as e:
                logger.info(f"  [bonus_dyn] {e!r} -- retrying next tick")

    async def _bonus_crystal_loop(self):
        """bonus_dungeon_crystals: credit a dungeon's Crystal when its Soul-of-Chaos
        superboss dies. While standing in a bonus dungeon (band-scoped, latched like
        _bonus_dyn_loop), when one of that dungeon's END bosses (D.BONUS_END_BOSS_IDS)
        dies in battle, latch that dungeon; each tick OR its durable shadow bit
        (save+0x834 bit dg, a client-owned byte -- see BONUS_CRYSTAL_SHADOW_ADDR) so
        the on-disc crystals_needed wrapper counts it toward the Black Orb. Sticky +
        re-asserted every tick (like
        the lute/rune bits), and the sticky is re-seeded from the save byte each tick,
        so a disconnect / reload / client restart can never un-light a crystal.

        No breakpoints (dead under FastMemoryAccess) and no getStoryFlag wrapper poke
        (that freezes -- see crystal-count-re); we set the save bit directly. Ticks
        only when the option is on, so default seeds pay nothing."""
        latched = None
        beaten = set()          # sticky: dungeons whose superboss we've credited
        while not self.exit_event.is_set():
            await asyncio.sleep(CHEST_POLL_S)
            try:
                if not self.bonus_dungeon_crystals or self.save_delta is None:
                    continue
                # NG+ hygiene: the sticky `beaten` set is GAME progress (boss kills),
                # NOT AP-item progress, so it must NOT carry into a New Game started in
                # the same client session -- else the re-assert below would re-light
                # crystals the fresh game hasn't earned (the save byte zeroes on new
                # game, but the sticky would write the bits straight back). Every other
                # NG+-sensitive loop guards this; clear the sticky on a genuinely-fresh
                # new game and skip the assert (the byte is already 0 there).
                if await self._newgame_block_live():
                    if beaten:
                        logger.info("  [bonus_crystal] fresh new game -> clearing "
                                    "carried crystal set")
                    beaten = set()
                    latched = None
                    continue
                # Band-scoped dungeon identity, latched until true overworld (a
                # self-contained copy of _bonus_dyn_loop.dungeon_idx -- gimmick
                # floors read mapid < 0x87 mid-dungeon, so we must hold the latch).
                lm = (await self.psp.read(self.sa(D.LOADED_MAP_ID_SA), 1))[0]
                if lm == 0:
                    latched = None
                elif latched is None:
                    mid = (await self.psp.read(self.sa(D.BONUS_MAPID_ADDR), 1))[0]
                    if mid >= D.BONUS_MAPID_MIN:
                        tbl = await self.psp.read(
                            self.sa(D.BONUS_FLOOR_TABLE_SA),
                            D.BONUS_BAND_VOTE * D.BONUS_FLOOR_STRIDE)
                        votes = []
                        for k in range(D.BONUS_BAND_VOTE):
                            b = D.bonus_mapid_band(
                                tbl[k * D.BONUS_FLOOR_STRIDE
                                    + D.BONUS_FLOOR_MAPID_OFF])
                            if b is not None:
                                votes.append(b)
                        dg = (max(set(votes), key=votes.count) if votes
                              else D.bonus_mapid_band(mid))
                        if dg is not None:
                            latched = dg
                dg = latched

                # Detect the superboss kill: an END-boss enemy of THIS dungeon dead
                # (HP 0 or KO/stone status) in battle. The whitelist is per-dungeon
                # (D.BONUS_END_BOSS_IDS) -- ONLY that dungeon's end bosses count, so a
                # MIDPOINT blue-flame boss (same 0x80-0x90 band, but not an end boss)
                # can't light the crystal before the dungeon is actually cleared. The
                # formation-id pre-check also anti-false-fires: only a species in the
                # CURRENT formation counts, so a stale dead-boss row from a previous
                # fight can't credit this dungeon.
                end_set = D.BONUS_END_BOSS_IDS.get(dg, frozenset())
                if (end_set and dg not in beaten and await self._in_battle()):
                    # latch-ok: _in_battle() (BATTLE_ACTIVE_FLAG_SA) is the real gate
                    # above; the range test below only VALIDATES the pointer before
                    # dereferencing it -- the latching pointer is never the in-battle
                    # signal here (same pattern as _thief_steal_loop, P4_LATCH_OK).
                    bb = await self.psp.read_u32(self.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))  # latch-ok
                    if 0x08800000 <= bb < 0x0A000000:
                        ids = await self.psp.read(
                            bb + D.BATTLE_ENEMY_INFO_OFF + 4, D.BATTLE_ENEMY_TYPES)
                        boss_ids = {i for i in ids if i in end_set}
                        if boss_ids:
                            # scan the 9 enemy rows for a THIS-formation boss unit
                            # that is dead (HP 0, or KO/stone status for an
                            # instant-death kill that leaves HP nonzero).
                            buf = await self.psp.read(
                                bb + D.BATTLE_UNIT_OFF + 4 * D.BATTLE_UNIT_STRIDE,
                                9 * D.BATTLE_UNIT_STRIDE)
                            for r in range(9):
                                o = r * D.BATTLE_UNIT_STRIDE
                                sp = buf[o + D.BU_SPECIES]
                                if sp not in boss_ids:      # not this fight's boss
                                    continue
                                hp = buf[o + D.BU_HP] | (buf[o + D.BU_HP + 1] << 8)
                                status = (buf[o + D.BU_STATUS]
                                          | (buf[o + D.BU_STATUS + 1] << 8))
                                if hp == 0 or (status & 0x03):
                                    beaten.add(dg)
                                    logger.info(
                                        f"  [bonus_crystal] dungeon {dg} superboss "
                                        f"(species 0x{sp:02x}) down -> crystal lit")
                                    break

                # Re-seed the sticky from the durable save byte (so a bit set in a
                # prior session keeps being asserted), then OR every credited
                # dungeon's shadow bit -- idempotent, re-asserted so a save/load
                # rollback can't drop it. The on-disc wrapper reads these bits.
                cur = (await self.psp.read(
                    self.sa(D.BONUS_CRYSTAL_SHADOW_ADDR), 1))[0]
                for i in range(4):
                    if cur & D.bonus_crystal_shadow_mask(i):
                        beaten.add(i)
                want = cur
                for i in beaten:
                    want |= D.bonus_crystal_shadow_mask(i)
                if want != cur:
                    await self.psp.write(
                        self.sa(D.BONUS_CRYSTAL_SHADOW_ADDR), bytes([want]))
                    logger.info(f"  [bonus_crystal] shadow bits {sorted(beaten)} "
                                f"(0x{cur:02x}->0x{want:02x})")
            except Exception as e:
                logger.info(f"  [bonus_crystal] {e!r} -- retrying next tick")

    # ---------------- bridge orchestration ----------------
    async def _await_game_loaded(self, timeout=90.0):
        """Wait until PPSSPP reports a LOADED GAME, before anything reads RAM.

        The client attaches as soon as the debugger port answers, which is 2-3
        seconds after launch -- while the emulator is still mounting the disc
        ("PPSSPP connected, game=None" in every session's log). Reading memory
        in that window is what the 2026-08-11 freeze looked like: the emulator
        stopped answering entirely (window not responding, ~0 CPU, RAM probe
        TimeoutError) while the tag read below burned RPC_TIMEOUT x 2 per try,
        ten tries, with NOTHING on screen -- the player sees a frozen PPSSPP and
        an idle client. `game.status` is answered by the debugger's own thread
        and reads no memory, so polling it is safe here.

        Returns True once a game is up. On timeout it says so and returns False;
        the caller carries on regardless (a wrong-but-running emulator is still
        better handled by _verify_bake's own mismatch path than by refusing to
        bridge)."""
        said = False
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.exit_event.is_set():
                return False
            try:
                game = await asyncio.wait_for(self.psp.game_status(), 10)
            except Exception:
                game = None
            if game:
                if said:
                    logger.info(f"  [bridge] game is up ({game}); reading the "
                                f"seed tag now")
                return True
            if not said:
                said = True
                logger.info("  [bridge] PPSSPP is still loading the disc -- "
                            "waiting for the game before touching its memory.")
            await asyncio.sleep(1.0)
        logger.warning(
            "  [bridge] PPSSPP never reported a loaded game. If its window is "
            "unresponsive, close it and reconnect -- the emulator can wedge "
            "while mounting a freshly baked disc.")
        return False

    async def _verify_bake(self, bake):
        """Read the bake tag from the running game and compare against this
        seed's bake hash. True = every table/feature is on-disc (runtime loops
        reconcile only); False = fall back to full runtime patching."""
        from .launcher import bake_hash32
        if not IP.any_enabled(bake.get("features") or {}, bake.get("data") or []):
            return True                     # nothing to bake -> nothing to verify
        # Never read RAM out of a still-loading emulator (see the freeze note in
        # _await_game_loaded). This replaces most of the retry budget below,
        # which was doing the waiting the expensive and silent way.
        await self._await_game_loaded()
        want = IP.BAKE_TAG_MAGIC + struct.pack("<I", bake_hash32(bake))
        # The retry used to fire ONLY on an exception, and `break` on any
        # readable-but-different value. But before the game has loaded the
        # module, 0x08B30E00 reads back as ZEROS -- a perfectly successful read
        # of the wrong bytes -- so the very first pass broke out and condemned a
        # bake that was in fact correct. Live 2026-08-06: the tag on disc and
        # the hash in the .done marker both read c029849e, every feature
        # signature passed on disc, the caves demonstrably ran in game, and the
        # client still fell back to runtime patching (which also forces
        # dabble_baked=False, leaving the magic_learn reconcile fighting the
        # on-disc table all session).
        # The MAGIC is what separates the two cases: no magic = the module is
        # not up yet, keep waiting; magic present but hash different = a genuinely
        # different bake, fail immediately, no point burning 10 seconds.
        # A CONFIRMED mismatch (magic present, hash different) is a wholly
        # different failure from "no magic yet": some OTHER seed's patched game
        # is running -- a stale PPSSPP instance, a savestate carried over from a
        # previous seed, or a hand-loaded ISO. The caller escalates that to a
        # relaunch/hard stop (see _recover_wrong_bake); it must never be quietly
        # played through. Live 2026-08-08 (Prime): tag 87d9ef98 vs wanted
        # d9e24463, whole session ran another bake's caves and name banks.
        self._bake_mismatch = False
        seen = None
        stalled = 0
        for _ in range(10):                 # game may still be settling
            try:
                seen = await self.psp.read(IP.BAKE_TAG_ADDR, 8)
            except Exception as e:
                # A read that TIMES OUT means the emulator is not servicing the
                # debugger at all -- say so once instead of burning the whole
                # retry budget in silence (2026-08-11: ~170s of nothing while
                # PPSSPP sat wedged).
                stalled += 1
                if stalled == 1:
                    logger.warning(
                        f"  [bake] PPSSPP is not answering the debugger "
                        f"({type(e).__name__}). If its window is unresponsive, "
                        f"close it and reconnect.")
                await asyncio.sleep(1.0)
                continue
            if seen == want:
                logger.info("  [bake] on-disc seed patch verified (tag match)")
                return True
            if seen[:4] == IP.BAKE_TAG_MAGIC:
                self._bake_mismatch = True
                break                       # a real, different bake -> stop now
            await asyncio.sleep(1.0)        # no magic yet -> module still loading
        logger.info(f"  [bake] tag at {IP.BAKE_TAG_ADDR:#010x} read "
                    f"{seen.hex(' ') if seen else 'nothing'}, wanted {want.hex(' ')}")
        logger.warning(
            "  [bake] the running game does NOT carry this seed's on-disc patch. "
            "Falling back to runtime patching (data tables still apply; on-disc "
            "CODE features like dabble-in-magic will be missing this session).")
        return False

    async def _recover_wrong_bake(self, bake, notify):
        """A CONFIRMED wrong bake is running (see _verify_bake). Close it and
        relaunch ONCE on this seed's freshly baked ISO; if the second verify
        still mismatches, refuse to bridge.

        Degrading to runtime patching here is worse than useless: the player is
        connected to their slot and believes they are playing the randomizer,
        while the on-disc caves, shop rows, remote-name banks and the Caravan
        offer all belong to a different bake. Nobody ever wants to play an
        unbaked/wrong-baked game, so a hard stop beats a silent half-seed.
        Returns True to continue the bridge, False to abort it."""
        import functools
        from .launcher import kill_ppsspp, ensure_ppsspp
        say = notify or (lambda m: logger.info(f"[FF1 PSP] {m}"))
        loop = asyncio.get_event_loop()
        say("The running game is patched for a DIFFERENT seed (bake tag "
            "mismatch confirmed). Closing it and relaunching on this seed's "
            "baked ISO — nothing is lost, load your save when it boots.")
        # Drop the sockets pointing at the game we are about to kill, or the
        # reconnect below inherits half-open handles into a dead WS server.
        for sock in {id(x): x for x in (self.psp, self.psp_bp,
                                        self.psp_scan)}.values():
            if sock is not None:
                with contextlib.suppress(Exception):
                    await sock.close()
        self.psp = self.psp_bp = self.psp_scan = None
        await loop.run_in_executor(None, kill_ppsspp)
        await asyncio.sleep(1.5)
        ok = await loop.run_in_executor(
            None, functools.partial(ensure_ppsspp, bake, notify=notify,
                                    stop=self.exit_event.is_set))
        if ok and await self._connect_psp():
            self.bake_ok = await self._verify_bake(bake)
            if self.bake_ok:
                say("Relaunched on the correct bake — everything is applied.")
                return True
        bar = "!" * 60
        for line in (
            "", bar,
            "!!!  WRONG GAME PATCH -- NOT BRIDGING  !!!",
            "!!!  PPSSPP is running a build patched for a DIFFERENT seed, and",
            "!!!  relaunching on this seed's ISO did not fix it. Playing on",
            "!!!  would look like the randomizer while the on-disc features,",
            "!!!  shop rows and item-name banks belong to another seed --",
            "!!!  checks would go missing and key items would be the vanilla",
            "!!!  ones, so the bridge is stopped instead.",
            "!!!  FIX: close EVERY PPSSPP window (check Task Manager for a",
            "!!!  stray PPSSPPWindows64.exe), do NOT load a savestate made on",
            "!!!  another seed (use an in-game save), then reconnect.",
            "!!!  STILL BROKEN? Type  /ff1psp_logs  -- it saves one zip with",
            "!!!  everything needed to diagnose this; send it to whoever",
            "!!!  maintains this apworld.",
            bar, ""):
            say(line)
        return False

    async def _prepare_remote(self, bake, remote, notify):
        """Remote-PPSSPP session prep: bake this seed's ISO locally so the
        player can copy it to the device (its PPSSPP loads it by hand), and
        say exactly what to do over there. Never blocks the bridge: connect
        proceeds regardless, and _verify_bake falls back to runtime patching
        if the device is running the wrong/unpatched ISO."""
        import functools
        from .launcher import (load_cfg, find_iso, ensure_patched_iso,
                               BakeFailed)
        host, port = remote
        notify(f"Remote PPSSPP mode: driving {host}"
               + (f":{port}" if port else " (port auto)")
               + " -- not launching a local emulator.")
        cfg = load_cfg()
        iso = find_iso(cfg.get("ppsspp", ""), cfg.get("iso", ""))
        if not iso:
            notify("No local FF1 ISO found to bake this seed into. Unless the "
                   "device already has this seed's patched ISO, the game will "
                   "run in runtime-fallback mode (on-disc code features "
                   "disabled). Set the ISO path once by running in local "
                   "mode, or put the ISO in Documents/PPSSPP or Downloads.")
            return
        try:
            patched = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(ensure_patched_iso, iso, bake, notify))
        except BakeFailed:
            # Banner already shown. Remote mode can't stop the device's own
            # PPSSPP, so say plainly that whatever it loads will be wrong.
            notify("This seed has NO patched ISO to copy over. Do not play the "
                   "unpatched game on the device -- AP shop items, magic "
                   "levels and remote chest names will all be missing and "
                   "purchases will send no checks.")
            return
        if patched != iso:
            notify(f"GO! COPY THIS SEED'S PATCHED ISO TO THE DEVICE AND LOAD "
                   f"IT IN PPSSPP THERE: {patched}")
        notify("On the device (one-time): PPSSPP > Settings > Tools > "
               "Developer tools > 'Allow remote debugger' ON. I'll connect "
               "the moment it answers.")

    async def _start_bridge_guarded(self):
        """_start_bridge as a fire-and-forget task swallowed its own crashes:
        asyncio only printed 'Task exception was never retrieved' to stderr,
        so the player saw an idle window and no reason (2026-08-08: a swapped
        apworld killed the first lazy import in here). Never let that repeat --
        one plain line on screen, the full traceback in the client log file."""
        try:
            await self._start_bridge()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.exit_event.is_set():
                return
            self._bridge_started = False     # let a reconnect retry
            logger.error(f"  [FF1 PSP] BRIDGE FAILED TO START: "
                         f"{type(e).__name__}: {e}")
            if isinstance(e, zipimport.ZipImportError):
                logger.error(
                    "  [FF1 PSP] That error means ff1psp.apworld was replaced "
                    "on disk while this client was running. Close the client "
                    "and reopen it -- reconnecting alone will not fix it.")
            else:
                logger.error("  [FF1 PSP] PPSSPP was not launched. Full "
                             "traceback is in the FF1PSPClient log; run "
                             "/ff1psp_logs to bundle it.")
            logger.debug("bridge start traceback:\n" + traceback.format_exc())

    async def _start_bridge(self):
        # Player has connected to a server/slot in the AP window. Order matters:
        #  1. SCOUT first (server-only, no PPSSPP): learn what every chest/shop
        #     holds, so the AP chest contents can be baked into the ISO.
        #  2. Bake + launch PPSSPP on the patched copy (reuse a running game only
        #     if its bake tag matches this seed).
        #  3. Verify the tag, then start the (thin) runtime loops.
        import functools
        from .launcher import ensure_ppsspp, remote_psp_target
        on_disc = (self.slot_data or {}).get("on_disc") or {}   # Route-2 code features
        try:
            await self._scout_locations()
        except Exception as e:
            if self.exit_event.is_set():
                return
            # Do NOT bake+cache a nameless ISO (chests/shops -> filler forever).
            # Re-arm the trigger so the next Connected package retries the whole
            # bridge start once the connection stabilises.
            logger.error(
                f"  [scout] FAILED to map chests/shops from the server ({e}). "
                "NOT launching on an unmapped seed -- reconnect and it will "
                "retry automatically. If this persists, check your server link.")
            self._bridge_started = False
            return
        # Surface launcher/patcher status in the AP client log so the player sees
        # what's happening (patching, auto-relaunch, etc.).
        notify = lambda m: logger.info(f"[FF1 PSP] {m}")
        # _build_bake is seconds of pure computation (every shuffle/scaling/AP
        # table) with no output of its own, and it sits between the scout's last
        # line and the launcher's first -- so the window looked frozen right
        # after "Chests: ...". Bracket it with status lines.
        notify("Loading… building this seed's bake spec (shuffles, scaling, "
               "chest/shop contents). No input needed.")
        bake = self._build_bake(on_disc)
        notify(f"Bake spec ready ({len((bake or {}).get('data') or [])} data "
               "tables). Checking PPSSPP…")
        remote = remote_psp_target()
        if remote:
            # Android/handheld: bake locally for the player to copy over, then
            # drive the device's PPSSPP over its WS debugger. No local launch/
            # kill/ini step (memory: android-port-feasibility, Phase A).
            await self._prepare_remote(bake, remote, notify)
        else:
            # stop=exit_event: this thread must abort when the window closes --
            # asyncio.run JOINS executor threads at exit, so a 90s debugger wait
            # still polling = client frozen "Not Responding" until it expires.
            ok = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(ensure_ppsspp, bake, notify=notify,
                                        stop=self.exit_event.is_set))
            if not ok:
                logger.error("PPSSPP not available -- bridge not started.")
                return
        if not await self._connect_psp(remote=remote):
            return
        # Last-resort safety: if this client ever dies via an uncaught exception (so
        # shutdown() never runs), clear our chest breakpoints on interpreter exit so
        # PPSSPP isn't left halted on a leftover hook -> hard game freeze.
        atexit.register(_atexit_clear_breakpoints)
        self._bake = bake            # kept so _cpu_watchdog can re-verify if a stale
                                     # PPSSPP instance delayed the boot past this check
        self.bake_ok = await self._verify_bake(bake)
        # Wrong-seed bake in RAM -> relaunch once, then hard stop. Remote
        # (Android/handheld) targets are never launched or killed from here, so
        # they keep the old warn-and-degrade behaviour; the player copies the
        # ISO by hand there and _prepare_remote already told them which one.
        if (not self.bake_ok and getattr(self, "_bake_mismatch", False)
                and not remote):
            if not await self._recover_wrong_bake(bake, notify):
                return
        loops = [
            ("watchdog", self._cpu_watchdog),
            ("save_delta", self._save_delta_loop),
            ("party", self._party_loop),
            ("starting_gil", self._starting_gil_loop),
            ("naked_monks", self._naked_monks_loop),
            ("thief_steal", self._thief_steal_loop),
            # job-scroll boosts: RW conversion + Master reactive stats + the
            # SCRL mailbox arming for the on-disc WW/BW caves
            ("scroll_battle", self._scroll_battle_loop),
            # save-or-suffer miss feedback: "7% Warp chance on Orthros"
            ("sos_feedback", self._sos_feedback_loop),
            # magic_power_scaling: arm/disarm the MPWR mailbox BOUNDARY per battle
            ("magic_power", self._magic_power_loop),
            # Stealth Ninja Scroll leg 2: damaging-floor mitigation (field HP deltas)
            ("floor_damage", self._floor_damage_loop),
            # Death Link: party-wipe detect (send) + received-death kills (receive)
            ("death_link", self._death_link_loop),
            # break a battle entered with the whole party already dead (nobody can
            # act -> the engine hangs); armed only by our own death-link wipes
            ("battle_limbo", self._battle_limbo_loop),
            # field KO reconciler: 0 HP without the KO state (blood magic, any
            # client HP write) -> stamp KO so the pose/church behave normally
            ("ko_sync", self._ko_sync_loop),
            # class rename: scroll-gated custom class names in the status menu
            ("classname", self._classname_loop),
            # scroll-gated custom party sprites: pins job_sprites art over the
            # resident JOBxx.GIM copies (battle + pause menu; field is baked
            # at map load and out of scope -- job-sprite-surfaces memory)
            ("jobsprite", self._jobsprite_loop),
            # lute_tablets: "Lute Tablets N of M" progress line in Key Items
            ("keyratio", self._keyratio_loop),
            ("movement", self._movement_loop),
            ("flags", self._flags_loop),
            ("openworld", self._openworld_loop),
            ("grant", self._grant_loop),
            # poll-based chest detection (tier2-poll-chests): reads the chest
            # bitfield; no exec breakpoints, so JIT block-linking stays enabled
            # (the old exec-BP chest path was deleted -- it tanked frame rates)
            ("chest_poll", self._chest_poll_loop),
            # dynamic (procedural) bonus-dungeon chests: invisible to the bitfield
            # poll, detected via the on-disc BDC1 mailbox. See _bonus_dyn_loop.
            ("bonus_dyn", self._bonus_dyn_loop),
            # bonus_dungeon_crystals: light a Crystal shadow bit on a Soul-of-Chaos
            # superboss kill (only ticks when the option is on). See _bonus_crystal_loop.
            ("bonus_crystal", self._bonus_crystal_loop),
            ("table", self._table_loop),
            # key-item-add box ("You obtain the {key}.") -> AP name authoring
            ("mapmsg", self._mapmsg_loop),
            ("boot", self._boot_patch_loop),
            # map-gated softening of random-encounter boss CAMEOS (must run after
            # "boot" builds self._patches -- it retargets the monster_rewards patch)
            ("cameo_boss", self._cameo_boss_loop),
            ("npc", self._npc_loop),
            ("shop", self._shop_loop),
            ("shop_hint", self._shop_hint_loop),
            # slot_magic: re-word the level-up "MP increased by N." line
            ("slotbox", self._slotbox_loop),
        ]
        self._tasks = [asyncio.create_task(self._guard(fn, name))
                       for name, fn in loops]
        logger.info("PPSSPP bridge running.")

    async def _cpu_watchdog(self):
        """Watch for the "attached to PPSSPP but no game is running" state and say
        so ONCE, clearly, instead of letting every loop flood the log with
        'CPU not started'. This is the symptom of PPSSPP's "Could not load game.
        Memory init failed" dialog -- almost always a second/stale PPSSPP instance
        holding the PSP memory, so the game never boots but the debugger socket is
        still up and we happily attach to a dead emulator.

        Emits one advisory when the CPU is down, and one 'CPU running' line when it
        recovers, so the log tells the player what to do without 200 repeats."""
        import functools
        warned = False
        self._relaunch_last_try = getattr(self, "_relaunch_last_try", 0.0)
        self._relaunch_strikes = getattr(self, "_relaunch_strikes", 0)
        while not self.exit_event.is_set():
            try:
                # a 1-byte read at the user-RAM base is the cheapest 'is the CPU up?'
                await self.psp.read(USER_RAM_BASE, 1)
                self._relaunch_strikes = 0        # emulator healthy again
                if warned:
                    logger.info("  [ppsspp] CPU running -- game booted, patches "
                                "will apply now.")
                    warned = False
                    # The first _verify_bake may have run while the CPU was still down
                    # (a stale PPSSPP instance delaying boot) and latched bake_ok=False,
                    # silently dropping on-disc CODE features -- the dabble-in-magic MP
                    # never lands on the forced Monk/Thief (live 2026-07-23). Now that the
                    # game is actually up, re-verify: party_loop reads bake_ok live at
                    # commit, so a flip to True restores dabble for this session.
                    if not self.bake_ok and getattr(self, "_bake", None) is not None:
                        if await self._verify_bake(self._bake):
                            self.bake_ok = True
                            logger.info("  [bake] re-verified after boot -> on-disc "
                                        "features (dabble) restored")
            except Exception as e:
                if "CPU not started" in repr(e) and not warned:
                    logger.error(
                        "  [ppsspp] Attached to PPSSPP but NO GAME is running "
                        "(CPU not started). This is usually PPSSPP's "
                        "'Could not load game. Memory init failed' error, caused "
                        "by a DUPLICATE/STALE PPSSPP instance holding the PSP "
                        "memory. FIX: close ALL PPSSPP windows (check Task Manager "
                        "for stray PPSSPPWindows64.exe), then relaunch so the game "
                        "actually boots. Nothing (party, items, shuffles) can apply "
                        "until the game is running.")
                    warned = True
                # Self-heal: PPSSPP PROCESS gone entirely (crashed / closed /
                # startup death) -> auto-relaunch the last exe+ISO instead of
                # error-flooding forever (live log 2026-07-30). Bounded: one
                # attempt per 60s, three strikes then we stand down and tell
                # the player. Remote targets are never auto-launched.
                from .launcher import ppsspp_process_running, relaunch_last, \
                    remote_psp_target
                now = time.monotonic()
                if (not remote_psp_target()
                        and not ppsspp_process_running()
                        and now - self._relaunch_last_try > 60.0
                        and self._relaunch_strikes < 3):
                    self._relaunch_last_try = now
                    self._relaunch_strikes += 1
                    logger.info(
                        "  [ppsspp] Emulator process is GONE -- auto-relaunching "
                        f"(attempt {self._relaunch_strikes}/3)…")
                    ok = await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(
                            relaunch_last,
                            notify=lambda m: logger.info(f"[FF1 PSP] {m}"),
                            stop=self.exit_event.is_set))
                    if ok:
                        logger.info("  [ppsspp] Relaunched -- bridge loops will "
                                    "reattach; load your save to continue.")
                        self._relaunch_strikes = 0
                    elif self._relaunch_strikes >= 3:
                        logger.error(
                            "  [ppsspp] Auto-relaunch failed 3 times -- giving "
                            "up. Start PPSSPP manually (the patched ISO is in "
                            "ProgramData/Archipelago/ff1psp) or reconnect.")
            await asyncio.sleep(3.0)

    async def _guard(self, loop_fn, name):
        """Run a loop (given as a coroutine FACTORY so it can be re-entered); a
        crash is logged, any leftover chest breakpoints are cleared defensively
        (nothing arms them anymore, but a stale one from an older client/RE
        session must never leave PPSSPP halted -> hard freeze), and the loop
        RESTARTS with backoff. One transient error used to kill a loop -- and
        everything it maintained -- for the rest of the session; with the
        reconnecting socket underneath, restarting is always safe."""
        backoff = 3.0
        while not self.exit_event.is_set():
            try:
                await loop_fn()
                return                    # loop finished its job cleanly
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"  [{name}] loop crashed: {e!r} -- clearing "
                             f"breakpoints, restarting in {backoff:.0f}s")
                try:
                    if self.psp_bp is not None:
                        for a in (D.CHEST_ITEM_CALL, D.CHEST_GIL_CALL):
                            await self._bp_rpc("cpu.breakpoint.remove", address=a)
                        await self._resume()
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def shutdown(self):
        # Backstop first: everything below is best-effort and time-boxed, but the
        # loop's own teardown (asyncio.run drains the default executor, waiting on
        # any sync call still in flight) is not ours to bound. See arm_exit_watchdog.
        arm_exit_watchdog()
        # The client arms no exec breakpoints (chest detection is poll-based), so
        # there is nothing to tear down here -- and running a remove+resume dance
        # against a gone PPSSPP is all dead-socket timeouts (it was the bulk of a
        # ~50 s frozen-on-close wedge before it was gated out).
        # Drop BOTH debugger sockets now and mark them closed so no winding-down
        # loop re-dials: reconnects into a closing PPSSPP wedge its WS server
        # thread ("Not Responding" on both windows).
        for c in {id(x): x for x in (self.psp, self.psp_bp, self.psp_scan)
                  if x is not None}.values():
            try:
                await asyncio.wait_for(c.close(), 2)
            except Exception:
                pass
        # Orderly shutdown reached -- the crash-only atexit hook would otherwise
        # open a FRESH connection at interpreter exit (dial timeouts against a
        # gone PPSSPP = seconds of frozen UI). No-op if it was never registered.
        atexit.unregister(_atexit_clear_breakpoints)
        # Cancel AND collect: a cancelled task is not finished until it has
        # actually unwound. CommonContext.shutdown awaits server_task next, so a
        # loop still mid-RPC would otherwise race the socket teardown. Bounded --
        # a task parked in run_in_executor cannot be cancelled at all.
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), 3)
        try:
            await asyncio.wait_for(super().shutdown(), 5)
        except asyncio.TimeoutError:
            logger.warning("  server shutdown timed out -- closing anyway.")


def launch(connect: Optional[str] = None,
           password: Optional[str] = None,
           name: Optional[str] = None) -> None:
    """Run the windowed client. The player enters server/slot/password in the AP
    client window; PPSSPP/FF1 is launched AFTER connecting to the slot (see
    FF1PSPContext._start_bridge -> launcher.ensure_ppsspp), so the client is
    guaranteed running and connected before the game comes up."""
    Utils.init_logging("FF1PSPClient")
    # Ring-buffer the log from the very first line: /ff1psp_logs ships it even
    # when the log FILE rotated or the interesting line scrolled hours ago.
    from . import debug_bundle as _DB
    _DB.install_breadcrumbs()

    # A watcher-triggered restart hands the password over in the environment,
    # never on the command line (argv is world-readable in the process list).
    if not password:
        password = os.environ.pop("FF1PSP_RESTART_PASSWORD", "") or None

    if connect:
        url = connect if "://" in connect else "ws://" + connect
        slot, pw = name, (password or "")
    else:
        # No CLI server: open the window empty and let the player connect.
        url, slot, pw = None, name, (password or "")

    async def _run():
        ctx = FF1PSPContext(url, pw)
        # The Kivy GUI runs on its own thread; the Boost tab needs this loop to
        # schedule live writes onto (see run_async_threadsafe).
        ctx.aio_loop = asyncio.get_running_loop()
        if slot:
            ctx.auth = slot
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")
        # Rebuilding the apworld under a running client poisons every import
        # this process has not done yet (see apworld_watch).
        asyncio.create_task(APWATCH.watch(ctx), name="ApworldWatch")
        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()
        await ctx.exit_event.wait()
        ctx.server_address = None
        await ctx.shutdown()

    import colorama
    colorama.init()
    try:
        asyncio.run(_run())
    finally:
        colorama.deinit()


# Backwards-compatible entry point name used by ff1psp.__init__.run_client.
def main(connect: Optional[str] = None,
         password: Optional[str] = None,
         name: Optional[str] = None) -> None:
    launch(connect, password, name)


if __name__ == "__main__":
    import sys
    _url = sys.argv[1] if len(sys.argv) > 1 else None
    _slot = sys.argv[2] if len(sys.argv) > 2 else None
    main(_url, None, _slot)
