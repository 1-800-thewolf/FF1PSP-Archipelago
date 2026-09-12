"""One-click troubleshooting bundle -- the `/logs` command.

A player who sees something strange runs one command and gets ONE zip on their
desktop, with a README telling them to hand it to whoever maintains this
apworld. Everything in it exists because a real playtest report needed it and
it had to be extracted by hand over several rounds of back-and-forth:

  * client log tails      -- every diagnosis so far started in the log
  * bake identity         -- cached-ISO hash vs the tag actually in RAM
                             ("the running game does NOT carry this seed's
                             on-disc patch": a whole session ran the wrong bake)
  * cache dir + manifests -- which patched ISOs exist, for which bake
  * savestate listing     -- a savestate restores a PREVIOUS seed's ELF, so the
                             right ISO on the command line still runs old code
  * stale processes       -- duplicate PPSSPP / client instances
  * ppsspp.ini values     -- FastMemoryAccess / debugger port, per candidate ini
  * ISO verdict           -- the /checkiso answer, without asking them to run it
  * slot_data             -- the options AS GENERATED (a yaml can be edited after)
  * AP state              -- checks sent, items received, goal
  * RAM snapshot          -- story flags, key-item bits, map, inventory, counter
  * CPU / freeze check    -- is the emulated CPU still ADVANCING, and if not,
                             where it is pinned (memory I/O rides direct process
                             memory, so a wedged game still answers every poll:
                             a clean client log is not evidence the game ran)
  * PPSSPP's own log      -- when the player has "Log to file" on
  * breadcrumbs           -- last few hundred log records even if the log file
                             rotated or the player scrolled past the banner

DEFENSIVE BY CONTRACT: every collector is individually wrapped. A section that
fails records its own traceback into the bundle and the bundle still builds --
a debug tool that dies on the broken machine it was written for is worthless.

Nothing here writes to the game, opens a debugger connection of its own, or
touches breakpoints; the RAM snapshot rides the client's existing bridge and is
read-only (see CLAUDE.md: never leave the CPU halted).
"""

import asyncio
import collections
import datetime
import glob
import io
import json
import logging
import os
import platform
import struct
import subprocess
import sys
import traceback
import zipfile

BUNDLE_PREFIX = "FF1PSP_debug"
LOG_TAIL_BYTES = 3 << 20         # per log file; whole file when smaller
LOG_FILES_KEPT = 3               # current + 2 previous runs
BREADCRUMB_MAX = 500
ERROR_MAX = 12                   # WARNING+ records quoted at the top of report.txt
ERROR_LINES_EACH = 25            # per record, so one traceback can't eat the page
PPSSPP_LOG_TAIL_BYTES = 512 << 10  # PPSSPP's own log, when it is writing one

# --- CPU / freeze check -------------------------------------------------------
# A "hard freeze" report used to be undiagnosable from a bundle: memory I/O
# rides DIRECT PROCESS MEMORY, so every client poll keeps succeeding against a
# game whose CPU is wedged, and the log looks perfectly healthy (KingDragonite
# 2026-08-20, frozen on a random encounter in Giant's Cave -- the bundle could
# not say whether the emulated CPU was executing at all). Sampling the PC a few
# times answers exactly that, and a pinned PC also says WHERE it is stuck.
#
# READ-ONLY BY CONTRACT: cpu.status + cpu.getAllRegs and nothing else. Never
# cpu.resume, never cpu.stepping, never a breakpoint -- a player-facing debug
# command must not be able to halt (or un-halt) their game.
#
# TWO signals, because neither alone answers it (both measured live 2026-08-20
# against a running game):
#   ticks -- cpu.status's emulated cycle counter. Moves ~1.1e8 per 0.5 s (the
#            PSP's 222 MHz clock) while the emulator executes; flat means the
#            CPU is not running at all. THE pc IS NOT A LIVENESS SIGNAL: with
#            the JIT running, PPSSPP answers getAllRegs from the last SYNCED
#            state -- live probe read the same pc five times in a row and
#            0xDEADBEEF in v0/v1/s0/s1 while the game played perfectly.
#   churn -- how much of the game's own RAM changes over a moment. Ticks still
#            advance when the game spins in an infinite loop (the freeze class
#            we are hunting), and only this tells that apart: a healthy title
#            screen moved 5 of 96 sampled chunks, a spinning one moves none.
WEDGE_SAMPLES = 5
WEDGE_GAP_S = 0.3
WEDGE_RPC_TIMEOUT = 5.0
_WEDGE_REGS = ("pc", "ra", "sp", "v0", "v1", "a0", "a1", "s0", "s1")
_STALE_REG = 0xDEADBEEF           # PPSSPP's marker for an unsynced register file
CHURN_LO, CHURN_HI = 0x08800000, 0x0A000000     # PSP user RAM
CHURN_STEP, CHURN_CHUNK = 0x40000, 0x1000       # 96 samples of 4 KB
CHURN_GAP_S = 0.4
CHURN_BUDGET_S = 8.0              # WS fallback is ~2.5 s a pass; direct mem is free

# --- host process check -------------------------------------------------------
# The 2026-08-20 freeze took the WHOLE PPSSPP window down ("Not Responding"),
# which also means the player cannot take a savestate as evidence. None of the
# emulator-side signals need PPSSPP's cooperation though: IsHungAppWindow asks
# Windows whether the UI thread has stopped pumping messages, and GetThreadTimes
# sampling shows WHICH thread is burning CPU (a spin) vs none (a deadlock).
# Read-only against the process; works when the debugger and the UI are dead.
HOST_GAP_S = 0.4                  # between the two thread-time samples
HOST_TOP_THREADS = 6              # busiest threads reported

# The player-facing note that ships inside the zip. Deliberately generic: it
# must still read correctly for whoever maintains this apworld in future.
README_TEXT = """\
FF1 PSP Archipelago -- debug bundle
===================================

This zip was made by the /logs command in the FF1 PSP client.

WHAT TO DO WITH IT
  Send this single file to the developer who maintains the FF1 PSP apworld
  (post it wherever you report bugs for it -- the Archipelago Discord thread
  or channel for this game is the usual place). Say what you were doing when
  the problem happened; that plus this zip is normally enough to diagnose it.

WHAT IS IN IT
  report.txt        readable summary -- versions, game patch state, setup
  client_log*.txt   the client's own log (the main diagnostic). Lines tagged
                    [FF1PSP at ...] are the client's internal telemetry --
                    deliberately kept out of the client window, not errors
  breadcrumbs.log   the most recent log lines, even if a log file rotated
  ap_state.json     which checks you have sent and items you have received
  slot_data.json    the options your seed was generated with
  ram_snapshot.json a read-only peek at story flags / key items / inventory
  ppsspp_log.txt    PPSSPP's own log, if it was writing one
  launcher.json     your saved PPSSPP + ISO paths
  manifest.json     the same facts as report.txt, machine-readable

PRIVACY
  Your server password and any auth token are removed. The bundle does include
  your slot name, the server address you connected to, and file paths on this
  computer (which contain your Windows user name). It never contains save data
  or a copy of your game.
"""


# --------------------------------------------------------------- breadcrumbs ---
class _Ring(logging.Handler):
    """Keeps the last BREADCRUMB_MAX formatted log records in memory.

    The log FILE is the primary artifact, but it is not always enough: a player
    can be many hours into a session (the interesting line long since scrolled
    and the file huge), or a fresh client run can rotate to a new file after
    the incident. This ring always holds the tail, costs nothing, and is what
    gets shipped when a log file cannot be read at all."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.buf = collections.deque(maxlen=BREADCRUMB_MAX)
        # WARNING+ kept SEPARATELY so report.txt can lead with the failure.
        # The 2026-08-08 test bundle proved why: the whole diagnosis was one
        # ZipImportError, and it was only visible seven files deep in the zip
        # while the report's own summary said nothing was wrong.
        self.errors = collections.deque(maxlen=ERROR_MAX)
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        try:
            line = self.format(record)          # includes exc_info if present
            self.buf.append(line)
            if record.levelno >= logging.WARNING:
                self.errors.append(line)
        except Exception:
            pass                          # a logging handler must never raise


_RING = None


def install_breadcrumbs():
    """Attach the ring to the root logger. Idempotent; safe to call twice."""
    global _RING
    if _RING is not None:
        return _RING
    _RING = _Ring()
    try:
        logging.getLogger().addHandler(_RING)
    except Exception:
        pass
    return _RING


# ------------------------------------------------------------------- helpers ---
def _safe(fn, *a, **kw):
    """Run a collector; return its value, or an {"error": traceback} marker."""
    try:
        return fn(*a, **kw)
    except Exception:
        return {"error": traceback.format_exc(limit=6)}


def _redactions(ctx):
    """Secrets to scrub out of every text file we ship."""
    out = []
    for v in (getattr(ctx, "password", None),
              getattr(ctx, "auth_token", None),
              os.environ.get("FF1PSP_PASSWORD")):
        if v and isinstance(v, str) and len(v) >= 3:
            out.append(v)
    return out


def _scrub(text, secrets):
    for s in secrets:
        text = text.replace(s, "<redacted>")
    return text


def _stat(path):
    try:
        st = os.stat(path)
        return {"path": path, "bytes": st.st_size,
                "mtime": _iso_time(st.st_mtime)}
    except Exception as e:
        return {"path": path, "error": repr(e)}


def _iso_time(epoch=None):
    t = datetime.datetime.fromtimestamp(
        epoch if epoch is not None else _now(), datetime.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%SZ")


def _now():
    import time
    return time.time()


def _tail(path, limit=LOG_TAIL_BYTES):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read()
    head = (b"" if size <= limit else
            f"[... {size - limit} earlier bytes trimmed ...]\n"
            .encode())
    return (head + data).decode("utf-8", "replace")


# ---------------------------------------------------------------- collectors ---
def _errors_section(ctx):
    """The WARNING+ records this session, newest last, each trimmed to
    ERROR_LINES_EACH lines. This is the FIRST thing report.txt prints: in
    practice the answer is a single traceback, and burying it behind a clean-
    looking summary costs a round-trip with the player."""
    if _RING is None:
        return {"note": "breadcrumbs not installed (bundle built outside the "
                        "client) -- see the log files in this zip"}
    out = []
    for line in _RING.errors:
        rows = line.splitlines()
        if len(rows) > ERROR_LINES_EACH:
            rows = (rows[:ERROR_LINES_EACH]
                    + [f"    [... {len(rows) - ERROR_LINES_EACH} more lines, "
                       f"full text in breadcrumbs.log ...]"])
        out.append("\n".join(rows))
    return out


def _env_section(ctx):
    from . import iso_patcher as IP
    from .launcher import state_dir, remote_psp_target, load_cfg, find_ppsspp
    cfg = load_cfg()
    exe = find_ppsspp(cfg.get("ppsspp", ""))
    remote = remote_psp_target(cfg)
    try:
        import Utils
        ap_ver = getattr(Utils, "version_tuple", None)
        ap_ver = ".".join(str(x) for x in ap_ver[:3]) if ap_ver else "unknown"
    except Exception:
        ap_ver = "unknown"
    return {
        "generated": _iso_time(),
        "patcher_version": IP.PATCHER_VERSION,
        "archipelago_version": ap_ver,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} "
                    f"({platform.version()})",
        "frozen": bool(getattr(sys, "frozen", False)),
        "state_dir": state_dir(),
        "ppsspp_exe": exe or "(not found)",
        "ppsspp_exe_stat": _stat(exe) if exe else None,
        "remote_psp": (f"{remote[0]}:{remote[1]}" if remote and remote[1]
                       else remote[0] if remote else "off (launching locally)"),
        "env_overrides": {k: os.environ[k] for k in
                          ("FF1PSP_PPSSPP", "FF1PSP_REMOTE", "FF1PSP_ISO")
                          if os.environ.get(k)},
    }


def _iso_section(ctx):
    """Path/size/format of the ISO plus the /checkiso verdict, computed here so
    the player never has to be talked through running a second command."""
    from .launcher import load_cfg, find_iso
    from . import eboot_patch as E
    from .cso_decompress import image_kind
    cfg = load_cfg()
    iso = find_iso(cfg.get("ppsspp", ""), cfg.get("iso", ""))
    out = {"iso": iso or "(none configured)"}
    if not iso or not os.path.isfile(iso):
        out["verdict"] = "MISSING -- the configured ISO does not exist"
        return out
    out.update(_stat(iso))
    out["format"] = image_kind(iso)
    if out["format"] not in ("iso", "unknown"):
        out["verdict"] = f"compressed image ({out['format']})"
        return out
    boots = []
    with open(iso, "rb") as f:
        for p, off, size in E._iso_find_boot_bins(f) or []:
            f.seek(off)
            head = f.read(size)
            boots.append({
                "file": p, "offset": off, "size": size,
                "state": ("ELF" if head[:4] == b"\x7fELF"
                          else "ALL ZEROS (blank)" if not any(head)
                          else "unknown " + head[:4].hex(" ")),
            })
    out["boot_bins"] = boots
    if not boots:
        out["verdict"] = "UNUSABLE -- no BOOT.BIN (not a PSP disc image)"
    elif any(b["state"] == "ELF" for b in boots):
        try:
            E.load_boot_elf(iso)
            out["verdict"] = "OK -- patchable"
        except Exception as e:
            out["verdict"] = f"UNUSABLE -- {e}"
    elif any(b["state"].startswith("ALL ZEROS") for b in boots):
        out["verdict"] = "UNUSABLE -- BOOT.BIN is blank (bad conversion/dump)"
    else:
        out["verdict"] = "UNUSABLE -- BOOT.BIN is not a plaintext ELF"
    return out


def _bake_section(ctx):
    """This seed's expected bake vs what is cached vs what is in RAM. The three
    of them disagreeing is the single most expensive class of bug so far."""
    from .launcher import bake_hash32, state_dir
    from . import iso_patcher as IP
    out = {"bake_ok": getattr(ctx, "bake_ok", None),
           "confirmed_mismatch": getattr(ctx, "_bake_mismatch", None)}
    bake = getattr(ctx, "_bake", None) if ctx is not None else None
    if bake:
        feats = bake.get("features") or {}
        out["expected_hash"] = f"{bake_hash32(bake):08x}"
        out["data_tables"] = len(bake.get("data") or [])
        out["features_enabled"] = sorted(k for k, v in feats.items()
                                         if v and not k.startswith("_"))
    out["tag_addr"] = f"{IP.BAKE_TAG_ADDR:#010x}"
    out["cache"] = _cache_listing(state_dir())
    return out


def _cache_listing(d):
    """Cached patched ISOs, their .done markers and their bake manifests."""
    rows = []
    for iso in sorted(glob.glob(os.path.join(d, "ff1psp_patched_*.iso"))):
        row = _stat(iso)
        row["done_marker"] = os.path.isfile(iso + ".done")
        try:
            with open(iso + ".f1ap.json") as f:
                row["manifest"] = json.load(f)
        except Exception:
            row["manifest"] = None       # baked by a build before manifests
        rows.append(row)
    orphans = [os.path.basename(p) for p in
               glob.glob(os.path.join(d, "ff1psp_patched_*.iso.done"))
               if not os.path.isfile(p[:-len(".done")])]
    return {"patched_isos": rows, "orphan_markers": orphans}


def _ini_section(ctx):
    """Every ppsspp.ini candidate and the three settings that break the bridge
    when stale (memory: ini-patch-all-candidates)."""
    from .launcher import find_inis, load_cfg, find_ppsspp
    exe = find_ppsspp(load_cfg().get("ppsspp", ""))
    want = ("FastMemoryAccess", "EnableRemoteDebugger", "RemoteISOPort",
            "RemoteDebuggerOnStartup")
    rows = []
    for ini in find_inis(exe) if exe else []:
        row = {"path": ini, "exists": os.path.isfile(ini)}
        if row["exists"]:
            vals = {}
            try:
                with open(ini, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        k, sep, v = line.partition("=")
                        if sep and k.strip() in want:
                            vals[k.strip()] = v.strip()
            except Exception as e:
                vals = {"error": repr(e)}
            row["settings"] = vals
            row["mtime"] = _stat(ini).get("mtime")
        rows.append(row)
    return rows


def _process_section(ctx):
    """Running PPSSPP / client processes. A second emulator or a second client
    is a recurring cause of 'my checks stopped working'."""
    if os.name != "nt":
        from .hostproc import process_table, ppsspp_pids
        try:
            rows = process_table()
        except Exception as e:
            return {"error": repr(e)}
        hits = [f"{pid} {name} {cmd[:200]}" for pid, name, cmd in rows
                if any(n in (name + " " + cmd).lower() for n in
                       ("ppsspp", "archipelago", "ff1psp", "python"))]
        return {"matching": hits, "ppsspp_instances": len(ppsspp_pids())}
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ).stdout
    except Exception as e:
        return {"error": repr(e)}
    hits = [line.strip() for line in out.splitlines()
            if any(n in line.lower() for n in
                   ("ppsspp", "archipelago", "ff1psp", "python"))]
    return {"matching": hits,
            "ppsspp_instances": sum(1 for h in hits if "ppsspp" in h.lower())}


def _savestate_section(ctx):
    """PPSSPP savestates for this disc id.

    A savestate restores the whole ELF, so loading one made under a PREVIOUS
    seed runs that seed's baked code even though the launcher passed the right
    ISO on the command line -- the failure mode killing can never fix."""
    from .launcher import load_cfg, find_ppsspp
    exe = find_ppsspp(load_cfg().get("ppsspp", ""))
    roots = []
    if exe:
        roots.append(os.path.join(os.path.dirname(exe), "memstick", "PSP",
                                  "PPSSPP_STATE"))
    home = os.path.expanduser("~")
    roots += [os.path.join(home, "Documents", "PPSSPP", "PSP", "PPSSPP_STATE"),
              os.path.join(home, ".config", "ppsspp", "PSP", "PPSSPP_STATE"),
              os.path.join(home, ".var", "app", "org.ppsspp.PPSSPP", "config",
                           "ppsspp", "PSP", "PPSSPP_STATE")]
    found = []
    for r in roots:
        for p in glob.glob(os.path.join(r, "*ULUS10251*")):
            found.append(_stat(p))
    return {"searched": roots, "states": found}


def _bestiary_section(ctx):
    """Bestiarysanity as the kill-count loop sees it: per hunted monster, the
    live count, the floor it must rise above, and the resulting state. The
    first "killed it but no check" report (2026-09-07) was undiagnosable
    without this -- the bundle carried the sampled ids and nothing else."""
    if ctx is None:
        return {"note": "no client context (bundle built offline)"}
    fl = getattr(ctx, "_bestiary_floor", None)
    if not getattr(ctx, "bestiary", None) or fl is None:
        return {"note": "bestiarysanity off for this seed"}
    from .monster_names import MONSTER_NAME
    sent = set()
    try:
        sent = ctx._bestiary_sent_mons()
    except Exception:
        pass
    return {"summary": fl.summary(), "rows": fl.rows(sent, MONSTER_NAME),
            "floor": fl.floor, "counts": fl.snap}


def _ap_section(ctx):
    """Server/slot identity plus what this slot has actually sent and received.
    The seed name is how the maintainer matches their own spoiler log."""
    if ctx is None:
        return {"note": "no client context (bundle built offline)"}

    def name_of(kind, i):
        try:
            table = (ctx.item_names if kind == "item" else ctx.location_names)
            return table.lookup_in_game(i)
        except Exception:
            return str(i)

    recv = list(getattr(ctx, "items_received", []) or [])
    return {
        "seed_name": getattr(ctx, "seed_name", None),
        "slot": getattr(ctx, "auth", None),
        "server": str(getattr(ctx, "server_address", None)),
        "connected": bool(getattr(ctx, "server", None)),
        "goal_sent": getattr(ctx, "finished_game", None),
        "checks_sent": len(getattr(ctx, "checked_locations", []) or []),
        "checks_missing": len(getattr(ctx, "missing_locations", []) or []),
        "items_received": len(recv),
        "received_counter_cache": getattr(ctx, "received_count", None),
        # Undelivered tail + WHY delivery is held. items_received ahead of the
        # save counter is the "check sent but item never arrived" signature
        # (2026-08-09), and the reason used to exist nowhere -- both stalling
        # gates in _grant_pending returned silently.
        "grant_pending": [
            name_of("item", getattr(it, "item", it))
            for it in recv[(getattr(ctx, "received_count", None) or 0):]],
        "grant_stall": (lambda s: None if not s else
                        {"why": s[0], "warned": s[2]})(
            getattr(ctx, "_grant_stall", None)),
        "sent_locations": sorted(
            name_of("loc", i) for i in (getattr(ctx, "sent_locations", set())
                                        or set())),
        "missing_locations": sorted(
            name_of("loc", i) for i in (getattr(ctx, "missing_locations", set())
                                        or set())),
        "received_order": [
            {"index": n, "item": name_of("item", getattr(it, "item", it)),
             "from_player": getattr(it, "player", None)}
            for n, it in enumerate(recv)],
        "multipliers": {k: getattr(ctx, k, None) for k in
                        ("enc_mult", "xp_mult", "gil_mult", "monster_mult",
                         "boss_mult")},
        "bridge": {
            "psp_connected": getattr(ctx, "psp", None) is not None,
            "save_delta": getattr(ctx, "save_delta", None),
            "transport": ("direct process memory"
                          if getattr(getattr(ctx, "psp", None), "mem", None)
                          is not None
                          and getattr(ctx.psp.mem, "attached", False)
                          else "WS debugger"),
            "mem_denied": bool(getattr(getattr(getattr(ctx, "psp", None),
                                               "mem", None), "denied", False)),
            "ptrace_scope": _ptrace_scope(),
        },
    }


def _ptrace_scope():
    try:
        from .ppsspp_mem import ptrace_scope
        return ptrace_scope()
    except Exception:
        return ""


# ------------------------------------------------------------- RAM snapshot ---
# (address, length, label). Save-relative rows go through ctx.sa(); the bake tag
# is an absolute module address and is read separately.
_SNAP_REGIONS = (
    (0x08D1151C, 0x10, "story_flags"),        # every story/event bit we gate on
    (0x08D11536, 0x06, "key_item_bits"),      # ids 1..36 possession bitfield
    (0x08D11EE4, 0x120, "party"),             # 4 rows of stats
    (0x08D12034, 0x100, "inventory"),         # packed consumable records
)


async def snapshot_ram(ctx):
    """Read-only peek at the live save. Never writes, never sets a breakpoint,
    and returns whatever it managed to read if the bridge dies mid-way."""
    from . import ff1_data as D
    from . import iso_patcher as IP
    out = {}
    psp = getattr(ctx, "psp", None)
    if psp is None:
        return {"note": "PPSSPP bridge is not connected -- no RAM snapshot"}
    out["save_delta"] = getattr(ctx, "save_delta", None)
    try:
        out["bake_tag"] = (await psp.read(IP.BAKE_TAG_ADDR, 8)).hex(" ")
    except Exception as e:
        out["bake_tag"] = f"unreadable: {e!r}"
    if ctx.save_delta is None:
        out["note"] = ("the save block has not been located yet (game not at a "
                       "loaded save?) -- save-relative reads skipped")
        return out
    for label, addr, size in (("field_map_id", D.FIELD_MAP_ID_SA, 4),
                              ("loaded_map_id", D.LOADED_MAP_ID_SA, 4),
                              ("bonus_map_id", D.BONUS_MAPID_ADDR, 1),
                              ("gil", D.GIL_ADDR_SA, 4),
                              ("received_counter",
                               D.RECEIVED_COUNTER_ADDR_SA, 4)):
        try:
            raw = await psp.read(ctx.sa(addr), size)
            out[label] = int.from_bytes(raw, "little")
        except Exception as e:
            out[label] = f"unreadable: {e!r}"
    for addr, size, label in _SNAP_REGIONS:
        try:
            out[label] = (await psp.read(ctx.sa(addr), size)).hex(" ")
        except Exception as e:
            out[label] = f"unreadable: {e!r}"
    # Decoded key items: the hex above is the evidence, this is the readable
    # form -- "does the player actually hold X" is asked in nearly every report.
    try:
        held = []
        for kid, name in D.KEY_ITEMS.items():
            a, mask = D.key_item_bit(kid)
            byte = (await psp.read(ctx.sa(a), 1))[0]
            if byte & mask:
                held.append(name)
        out["key_items_held"] = held
    except Exception as e:
        out["key_items_held"] = f"unreadable: {e!r}"
    # Battle popup-object forensics (v260, after two sprite-vanish reports that
    # arrived while the reporter was STILL in the broken battle): if a battle is
    # up, dump the whole object slot array + the allocator/batch bookkeeping +
    # the party/unit anchor records. Read-only; skipped silently on the field.
    try:
        out["battle_popup"] = await _snapshot_battle_popups(ctx, psp)
    except Exception as e:
        out["battle_popup"] = f"unreadable: {e!r}"
    return out


async def _snapshot_battle_popups(ctx, psp):
    """In-battle only: the popup/object slot array (kind/active/link words per
    slot), the 0x67B0..0x67C0 allocator neighbourhood, the 0x68C0..0x68D8
    bookkeeping block, and each party slot's full record head. This is the
    evidence pack for the sprite-vanish family -- a vanished actor shows up
    here as a party record whose active byte or link words differ from its
    healthy neighbours."""
    from . import ff1_data as D
    if not await ctx._in_battle():
        return "not in a battle -- skipped"
    bb = await psp.read_u32(ctx.sa(D.BATTLE_ACTOR_OBJ_PTR_SA))
    if not (0x08800000 <= bb < 0x0A000000):
        return f"battle ptr implausible: {bb:#x}"
    out = {"bb": f"{bb:#x}"}
    out["alloc_67b0"] = (await psp.read(bb + 0x67B0, 0x10)).hex(" ")
    out["book_68c0"] = (await psp.read(bb + 0x68C0, 0x18)).hex(" ")
    blk = await psp.read(bb + 0x420, 0x50 * 124)
    slots = []
    for i in range(0x50):
        r = i * 124
        kind, act = blk[r + 0x44], blk[r + 0x77]
        head = blk[r:r + 16].hex(" ")           # def ptrs / template id words
        link = blk[r + 0x5C:r + 0x64].hex(" ")  # the words free_range detaches
        if act or kind or any(blk[r:r + 16]):
            slots.append(f"{i:#04x}: kind={kind:02x} act={act:02x} "
                         f"head={head} link={link}")
    out["slots"] = slots
    return out


# ------------------------------------------------------- CPU / freeze check ---
def _addr_where(a):
    """Readable home of a PC/RA: vanilla module, our cave segment, or wild.

    The cave segment starts exactly at the module's memsz end (eboot_patch
    SAFE_CAVE_VADDR) and grows upward, so "cave + 0x…" is directly comparable
    against a bake's cave layout. Its END is not knowable from RAM alone (the
    game heap continues above it), hence the hedge in the label."""
    from .eboot_patch import VADDR, SAFE_CAVE_VADDR
    if not isinstance(a, int):
        return "?"
    if VADDR <= a < SAFE_CAVE_VADDR:
        return f"vanilla module (+{a - VADDR:#x})"
    if SAFE_CAVE_VADDR <= a < 0x08D00000:
        return (f"CAVE SEGMENT +{a - SAFE_CAVE_VADDR:#x} -- patched code, or "
                f"the heap just above it (check against this seed's bake)")
    if 0x08000000 <= a < 0x0A000000:
        return "PSP RAM outside the module (kernel / heap)"
    if 0x04000000 <= a < 0x04800000:
        return "VRAM"
    return "OUTSIDE PSP RAM -- wild jump"


async def _peek(psp, addr, size, align=True):
    """Hex dump of `size` bytes at `addr`, or the reason it could not be read.
    Used for the words around a pinned PC/RA -- enough to disassemble by hand."""
    if not isinstance(addr, int) or not (0x08000000 <= addr < 0x0A000000 - size):
        return f"not readable ({addr!r})"
    if align:
        addr &= ~3
    try:
        # Individually time-boxed: over a WS-only bridge to a hung PPSSPP one
        # read can ride the full rpc timeout, and three of those must not eat
        # the caller's whole budget (the verdict is already computed by now).
        data = await asyncio.wait_for(psp.read(addr, size), 3.0)
        return f"{addr:#010x}: " + data.hex(" ")
    except Exception as e:
        return f"unreadable: {e!r}"


async def _ram_churn(psp):
    """How much of the game's own RAM is still moving? A sparse two-pass sweep
    of user RAM: a game whose frame loop runs keeps buffers turning over even
    on a static screen, a game spinning in a loop changes nothing at all."""
    addrs = list(range(CHURN_LO, CHURN_HI, CHURN_STEP))
    try:
        first = [await psp.read(a, CHURN_CHUNK) for a in addrs]
        await asyncio.sleep(CHURN_GAP_S)
        second = [await psp.read(a, CHURN_CHUNK) for a in addrs]
    except Exception as e:
        return {"error": repr(e)}
    moved = [a for a, x, y in zip(addrs, first, second) if x != y]
    return {"chunks": len(addrs), "changed": len(moved), "gap_s": CHURN_GAP_S,
            "changed_at": [f"{a:#x}" for a in moved[:12]]}


async def snapshot_wedge(ctx):
    """Is the game actually running? Read-only (see the WEDGE_* block) and
    individually timed out, so a debugger that has stopped answering costs a
    second, not the bundle."""
    psp = getattr(ctx, "psp", None)
    if psp is None:
        return {"note": "PPSSPP bridge is not connected -- no CPU sample"}
    if not hasattr(psp, "rpc"):
        return {"note": "this bridge has no debugger channel -- no CPU sample"}
    out = {"how": ("read-only: cpu.status + cpu.getAllRegs sampled "
                   f"{WEDGE_SAMPLES}x {WEDGE_GAP_S}s apart, plus a two-pass "
                   "RAM churn sweep; the bundle never resumes, steps or sets "
                   "a breakpoint"),
           "samples": []}
    # Churn first: it is nearly free over direct process memory, and it is the
    # only signal that separates "spinning in a loop" from "playing fine".
    try:
        out["churn"] = await asyncio.wait_for(_ram_churn(psp), CHURN_BUDGET_S)
    except Exception as e:
        out["churn"] = {"error": repr(e)}
    for n in range(WEDGE_SAMPLES):
        if n:
            await asyncio.sleep(WEDGE_GAP_S)
        s = {}
        try:
            st = await asyncio.wait_for(psp.rpc("cpu.status"),
                                        WEDGE_RPC_TIMEOUT)
            for k in ("stepping", "paused", "pc", "ticks"):
                if k in st:
                    s[k] = st[k]
        except Exception as e:
            s["status_error"] = repr(e)
        try:
            r = await asyncio.wait_for(psp.rpc("cpu.getAllRegs"),
                                       WEDGE_RPC_TIMEOUT)
            cat = (r.get("categories") or [{}])[0]
            reg = dict(zip(cat.get("registerNames") or [],
                           cat.get("uintValues") or []))
            s["regs"] = {k: reg[k] for k in _WEDGE_REGS if k in reg}
        except Exception as e:
            s["regs_error"] = repr(e)
        out["samples"].append(s)
        # A fully hung PPSSPP takes its debugger down with it: both rpcs then
        # ride the whole WEDGE_RPC_TIMEOUT, and 5 samples of that would blow
        # the caller's overall budget and throw away the churn result -- the
        # ONE signal that still works against a hung process. One dead sample
        # is proof enough; stop probing.
        if "status_error" in s and "regs_error" in s:
            out["samples_stopped"] = ("debugger not answering -- remaining "
                                      "samples skipped")
            break

    def pc_of(s):
        v = (s.get("regs") or {}).get("pc", s.get("pc"))
        return v if isinstance(v, int) else None

    pcs = [p for p in (pc_of(s) for s in out["samples"]) if p is not None]
    ticks = [s["ticks"] for s in out["samples"] if isinstance(s.get("ticks"), int)]
    churn = out.get("churn") or {}
    moved = churn.get("changed") if isinstance(churn.get("changed"), int) else None
    span = WEDGE_GAP_S * (max(len(pcs), len(ticks)) - 1)
    out["pc_samples"] = [f"{p:#010x}" for p in pcs]
    if ticks:
        out["ticks"] = f"{ticks[0]} -> {ticks[-1]} (+{ticks[-1] - ticks[0]})"
    if any(s.get("stepping") for s in out["samples"]):
        out["verdict"] = ("CPU HALTED (debugger reports stepping) -- stopped "
                          "at a breakpoint, not executing")
    elif not ticks and moved is None:
        out["verdict"] = ("UNAVAILABLE -- the PPSSPP debugger did not answer "
                          "(RemoteDebuggerOnStartup off, or PPSSPP closed). "
                          "Nothing here says whether the game was running.")
        return out
    elif ticks and ticks[-1] == ticks[0]:
        out["verdict"] = (f"CPU NOT EXECUTING -- 0 emulated cycles over "
                          f"{span:.1f}s: the game is stopped dead (hard "
                          f"freeze, or the emulator is paused)")
    elif moved == 0 and not ticks:
        out["verdict"] = (f"GAME FROZEN -- the debugger did not answer AND "
                          f"nothing changed in {churn.get('chunks')} sampled "
                          f"RAM chunks over {churn.get('gap_s')}s: the "
                          f"emulator itself is likely hung (see the host "
                          f"process check)")
    elif moved == 0:
        out["verdict"] = (f"GAME NOT ADVANCING -- the cpu ran "
                          f"{ticks[-1] - ticks[0]} cycles but NOTHING changed "
                          f"in {churn.get('chunks')} sampled RAM chunks over "
                          f"{churn.get('gap_s')}s: consistent with code "
                          f"spinning in a loop while the picture is frozen")
    else:
        out["verdict"] = (
            f"running normally"
            + (f" (+{ticks[-1] - ticks[0]} cycles over {span:.1f}s" if ticks
               else " (")
            + (f", {moved}/{churn.get('chunks')} RAM chunks moving)"
               if moved is not None else ")"))
    # The register file is only trustworthy when the CPU is SYNCED: with the JIT
    # running PPSSPP answers from the last sync, which reads as a pinned pc and
    # 0xDEADBEEF GPRs (live-measured 2026-08-20). Say so rather than shipping a
    # believable-looking pc that means nothing.
    last = out["samples"][-1].get("regs") or {}
    # WHICH register carries the marker varies run to run (live: a0/a1 one
    # sample, v0/v1 the next), so test every one we captured.
    if any(v == _STALE_REG for v in last.values()):
        out["regs_are_stale"] = ("registers were NOT synced (0xDEADBEEF in the "
                                 "GPRs) -- pc/ra below are from the last sync, "
                                 "not the live JIT state")
    pc = last.get("pc", pcs[-1] if pcs else None)
    ra, sp = last.get("ra"), last.get("sp")
    out["pc"] = f"{pc:#010x}" if isinstance(pc, int) else None
    out["ra"] = f"{ra:#010x}" if isinstance(ra, int) else None
    out["sp"] = f"{sp:#010x}" if isinstance(sp, int) else None
    out["pc_where"], out["ra_where"] = _addr_where(pc), _addr_where(ra)
    # The words either side of pc/ra, and the top of the stack: enough to
    # disassemble the loop by hand without asking the player for anything else.
    if isinstance(pc, int):
        out["code_at_pc"] = await _peek(psp, pc - 0x20, 0x40)
    if isinstance(ra, int):
        out["code_at_ra"] = await _peek(psp, ra - 0x20, 0x40)
    if isinstance(sp, int):
        out["stack_at_sp"] = await _peek(psp, sp, 0x40)
    return out


# --------------------------------------------------------- host process check ---
def _windows_of_pid(pid):
    """[(title, is_hung), ...] for a pid's visible top-level windows."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(hwnd, buf, 128)
            out.append((buf.value, bool(user32.IsHungAppWindow(hwnd))))
        return True

    user32.EnumWindows(cb, 0)
    return out


def _thread_cpu_times(pid):
    """{tid: kernel+user time in 100ns units} for every thread of `pid`."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    snap = k32.CreateToolhelp32Snapshot(0x4, 0)          # TH32CS_SNAPTHREAD
    if snap == -1:
        return {}
    out = {}
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                h = k32.OpenThread(0x0040 | 0x0800, False,   # QUERY(+LIMITED)
                                   te.th32ThreadID)
                if h:
                    ft = (wintypes.FILETIME * 4)()
                    if k32.GetThreadTimes(h, ctypes.byref(ft[0]),
                                          ctypes.byref(ft[1]),
                                          ctypes.byref(ft[2]),
                                          ctypes.byref(ft[3])):
                        t = 0
                        for i in (2, 3):                  # kernel + user
                            t += (ft[i].dwHighDateTime << 32) \
                                 | ft[i].dwLowDateTime
                        out[te.th32ThreadID] = t
                    k32.CloseHandle(h)
            ok = k32.Thread32Next(snap, ctypes.byref(te))
    finally:
        k32.CloseHandle(snap)
    return out


def _host_verdict(hung_any, busy, gap_s):
    """Pure verdict from the window state + the busiest threads' cpu shares."""
    top = busy[0][1] if busy else 0.0
    if hung_any and top >= 50.0:
        return (f"PPSSPP WINDOW HUNG and thread {busy[0][0]} is at "
                f"{top:.0f}% cpu: the emulator is SPINNING (a runaway loop), "
                f"not deadlocked")
    if hung_any:
        return ("PPSSPP WINDOW HUNG with no thread burning cpu: the emulator "
                "is DEADLOCKED (threads waiting on each other)")
    if top >= 50.0:
        return (f"window responding; thread {busy[0][0]} busy at {top:.0f}% "
                f"cpu (normal for an emulator)")
    return "PPSSPP window is responding and the process is mostly idle"


def _host_section(ctx=None):
    """Is the PPSSPP PROCESS healthy? Answered from the outside -- window
    responsiveness + which threads burn CPU -- so it works when the emulator
    is fully hung and every in-emu channel is dead. Read-only. The sleep is
    fine: build_bundle runs collectors in an executor, not the asyncio loop."""
    from .ppsspp_mem import _ppsspp_pids
    from . import hostproc
    if not (hostproc.IS_WINDOWS or hostproc.IS_LINUX):
        return {"note": "host process check is Windows/Linux-only"}
    pids = _ppsspp_pids()
    if not pids:
        return {"note": "no PPSSPP process running"}
    import time
    out = {"pids": pids}
    pid = pids[0]                     # snapshot_ram already flags >1 instance
    if hostproc.IS_WINDOWS:
        try:
            wins = _windows_of_pid(pid)
            out["windows"] = [{"title": t or "(untitled)", "hung": h}
                              for t, h in wins]
            hung_any = any(h for _t, h in wins)
        except Exception as e:
            out["windows"] = {"error": repr(e)}
            hung_any = False
        cpu_times = _thread_cpu_times
    else:
        # No window probe on Linux; a stopped/zombie scheduler state is the
        # nearest "hung" signal. Thread cpu comes from /proc in the same
        # 100 ns units the Windows collector uses, so the math below is shared.
        state = hostproc.linux_proc_state(pid)
        out["state"] = state
        hung_any = state in ("Z", "T")
        cpu_times = hostproc.linux_thread_cpu_times
    try:
        a = cpu_times(pid)
        time.sleep(HOST_GAP_S)
        b = cpu_times(pid)
        deltas = {tid: b[tid] - a.get(tid, b[tid]) for tid in b}
        # 100 ns units -> percent of one core over the gap
        busy = sorted(((tid, d / (HOST_GAP_S * 1e7) * 100.0)
                       for tid, d in deltas.items() if d > 0),
                      key=lambda kv: -kv[1])[:HOST_TOP_THREADS]
        out["busy_threads"] = [f"tid {tid}: {pct:.0f}% cpu" for tid, pct in busy]
        out["thread_count"] = len(b)
    except Exception as e:
        out["busy_threads"] = {"error": repr(e)}
        busy = []
    out["verdict"] = _host_verdict(hung_any, busy, HOST_GAP_S)
    return out


def _ppsspp_log(ctx=None):
    """PPSSPP's OWN log file (not the client's). Absent unless the player has
    'Log to file' switched on in Developer Tools -- when a bundle comes back
    with a frozen CPU and no PPSSPP log, asking them to turn it on is the next
    step, so say so in the note rather than leaving the section blank."""
    from .launcher import find_ppsspp, load_cfg, find_inis
    try:
        exe = find_ppsspp(load_cfg().get("ppsspp", ""))
    except Exception:
        exe = None
    roots = []
    if exe:
        d = os.path.dirname(exe)
        roots += [d, os.path.join(d, "memstick"),
                  os.path.join(d, "memstick", "PSP", "SYSTEM"),
                  os.path.join(d, "PSP", "SYSTEM")]
    home = os.path.expanduser("~")
    roots += [os.path.join(home, "Documents", "PPSSPP", "PSP", "SYSTEM"),
              os.path.join(home, ".config", "ppsspp", "PSP", "SYSTEM"),
              os.path.join(home, ".var", "app", "org.ppsspp.PPSSPP", "config",
                           "ppsspp", "PSP", "SYSTEM")]
    try:
        roots += [os.path.dirname(p) for p in (find_inis(exe) if exe else [])]
    except Exception:
        pass
    hits = set()
    for r in roots:
        # ppsspp*.log only: a bare *.log would sweep whatever else lives beside
        # the exe and then pick the newest of THOSE.
        hits.update(glob.glob(os.path.join(r, "ppsspp*.log")))
    hits = [p for p in hits if os.path.isfile(p)]
    if not hits:
        return {"note": ("no PPSSPP log file found -- PPSSPP only writes one "
                         "when 'Log to file' is enabled in Developer Tools"),
                "searched": roots}
    newest = max(hits, key=lambda p: os.path.getmtime(p))
    row = _stat(newest)
    try:
        row["tail"] = _tail(newest, PPSSPP_LOG_TAIL_BYTES)
    except Exception as e:
        row["error"] = repr(e)
    return row


# ------------------------------------------------------------------- report ---
def _render_report(m):
    """report.txt -- the part a human reads first. Same facts as manifest.json,
    ordered by how often each one turns out to be the answer."""
    w = io.StringIO()
    p = lambda s="": w.write(s + "\n")
    env, ap, bake = m["environment"], m["archipelago"], m["bake"]
    p("FF1 PSP Archipelago -- debug bundle")
    p("=" * 60)
    p(f"generated        : {env.get('generated')}")
    p(f"slot / seed      : {ap.get('slot')} / {ap.get('seed_name')}")
    p(f"server           : {ap.get('server')} "
      f"(connected: {ap.get('connected')})")
    p(f"patcher version  : {env.get('patcher_version')}")
    p(f"AP / python      : {env.get('archipelago_version')} / "
      f"{env.get('python')}")
    p(f"platform         : {env.get('platform')}")
    p()
    # Errors FIRST -- see _errors_section. Everything below is context for this.
    errs = m.get("recent_errors")
    p("-- recent errors and warnings " + "-" * 30)
    if isinstance(errs, dict):                     # collector failed / not armed
        p(errs.get("note") or errs.get("error") or "(unavailable)")
    elif not errs:
        p("(none logged this session)")
    else:
        p(f"{len(errs)} recorded, newest last:")
        for line in errs:
            p("")
            for row in line.splitlines():
                p("  " + row)
    p()
    # Second, because "was the game even executing?" reframes everything under
    # it: with direct process memory the client polls a frozen game happily.
    p("-- cpu / freeze check " + "-" * 38)
    cpu = m.get("cpu")
    if not isinstance(cpu, dict):
        p("(not collected)")
    elif not cpu.get("verdict"):
        p(cpu.get("note") or cpu.get("error") or "(not collected)")
    else:
        p(f"verdict          : {cpu.get('verdict')}")
        if cpu.get("ticks"):
            p(f"emulated cycles  : {cpu['ticks']}")
        _ch = cpu.get("churn") or {}
        if isinstance(_ch.get("changed"), int):
            p(f"ram churn        : {_ch['changed']}/{_ch.get('chunks')} "
              f"sampled chunks changed in {_ch.get('gap_s')}s")
        elif _ch.get("error"):
            p(f"ram churn        : {_ch['error']}")
        p(f"pc               : {cpu.get('pc')}  {cpu.get('pc_where')}")
        p(f"ra               : {cpu.get('ra')}  {cpu.get('ra_where')}")
        if cpu.get("regs_are_stale"):
            p(f"  note           : {cpu['regs_are_stale']}")
        p("  (code around pc/ra and the stack top are in manifest.json)")
    host = m.get("host")
    if isinstance(host, dict) and host.get("verdict"):
        p(f"host process     : {host['verdict']}")
        for row in (host.get("busy_threads") or [])[:3] \
                if isinstance(host.get("busy_threads"), list) else []:
            p(f"  {row}")
    elif isinstance(host, dict) and (host.get("note") or host.get("error")):
        p(f"host process     : {host.get('note') or host.get('error')}")
    plog = m.get("ppsspp_log")
    if isinstance(plog, dict):
        p(f"PPSSPP log       : "
          f"{plog.get('path') or plog.get('note') or plog.get('error')}")
    p()
    p("-- game patch state " + "-" * 40)
    p(f"bake verified    : {bake.get('bake_ok')}"
      + ("   *** CONFIRMED WRONG BAKE IN RAM ***"
         if bake.get("confirmed_mismatch") else ""))
    p(f"expected hash    : {bake.get('expected_hash')}")
    p(f"tag in RAM       : {(m.get('ram') or {}).get('bake_tag')}")
    p(f"data tables      : {bake.get('data_tables')}")
    p(f"features         : {', '.join(bake.get('features_enabled') or []) or '(none)'}")
    for row in (bake.get("cache") or {}).get("patched_isos") or []:
        man = row.get("manifest") or {}
        p(f"  cached ISO     : {os.path.basename(row.get('path', '?'))} "
          f"({row.get('bytes', 0) >> 20} MB, done={row.get('done_marker')}, "
          f"hash={man.get('bake_hash32', '?')}, "
          f"patcher={man.get('patcher_version', '?')})")
    orph = (bake.get("cache") or {}).get("orphan_markers")
    if orph:
        p(f"  orphan markers : {orph}")
    p()
    p("-- setup " + "-" * 51)
    p(f"PPSSPP exe       : {env.get('ppsspp_exe')}")
    p(f"remote PPSSPP    : {env.get('remote_psp')}")
    if env.get("env_overrides"):
        p(f"env overrides    : {env['env_overrides']}")
    iso = m["iso"]
    p(f"ISO              : {iso.get('iso')}")
    p(f"ISO verdict      : {iso.get('verdict')} "
      f"({iso.get('bytes', 0) >> 20} MB, {iso.get('format')})")
    for row in m["ppsspp_ini"] if isinstance(m["ppsspp_ini"], list) else []:
        p(f"  ini            : {row.get('path')} "
          f"{row.get('settings') if row.get('exists') else '(absent)'}")
    st = m["savestates"]
    if isinstance(st, dict) and st.get("states"):
        p("savestates       : PRESENT -- a savestate from an earlier seed runs "
          "that seed's code")
        for s in st["states"]:
            p(f"  {s.get('mtime')}  {os.path.basename(s.get('path', '?'))}")
    procs = m["processes"]
    if isinstance(procs, dict):
        p(f"PPSSPP instances : {procs.get('ppsspp_instances')}")
    p()
    p("-- progress " + "-" * 48)
    p(f"checks sent      : {ap.get('checks_sent')} "
      f"(missing {ap.get('checks_missing')})")
    p(f"items received   : {ap.get('items_received')} "
      f"(save counter cache {ap.get('received_counter_cache')})")
    _pend = ap.get("grant_pending") or []
    if _pend:
        p(f"NOT YET DELIVERED: {len(_pend)} -- {', '.join(_pend[:6])}"
          + (" …" if len(_pend) > 6 else ""))
        _st = ap.get("grant_stall")
        _why = _st.get("why") if _st else "nothing (should deliver next tick)"
        p(f"  delivery held by: {_why}")
    p(f"goal reported    : {ap.get('goal_sent')}")
    p(f"bridge           : {ap.get('bridge')}")
    ram = m.get("ram") or {}
    if ram.get("key_items_held") is not None:
        p(f"key items held   : {ram.get('key_items_held')}")
    p(f"map (field/loaded/bonus): {ram.get('field_map_id')} / "
      f"{ram.get('loaded_map_id')} / {ram.get('bonus_map_id')}")
    bst = m.get("bestiary")
    if isinstance(bst, dict) and bst.get("rows") is not None:
        p()
        p("-- bestiarysanity " + "-" * 42)
        s = bst.get("summary") or {}
        p(f"floor            : {'set' if s.get('floor_present') else 'NOT set'}"
          f"{' (junk on server)' if s.get('floor_malformed') else ''}, server "
          f"{'answered' if s.get('server_answered') else 'silent'}, "
          f"{s.get('under_floor')} entries under floor, last decision "
          f"{(s.get('last_decision') or {}).get('kind')} "
          f"{(s.get('last_decision') or {}).get('detail') or ''}")
        if s.get("rejected_frames"):
            p(f"rejected frames  : {s.get('rejected_frames')}")
        for r in bst["rows"]:
            p(f"  {r.get('name'):<16} kills {r.get('count')!s:>4}  "
              f"floor {r.get('floor')!s:>4}  {r.get('state')}")
    p()
    p("Full detail: manifest.json, ap_state.json, ram_snapshot.json, and the "
      "client logs in this zip.")
    return w.getvalue()


# -------------------------------------------------------------- bundle build ---
def _out_dir():
    """Desktop if there is one, else Documents, else the client's state dir --
    the player has to be able to FIND this file without being talked to it."""
    from .launcher import state_dir
    home = os.path.expanduser("~")
    for d in (os.path.join(home, "Desktop"), os.path.join(home, "OneDrive",
                                                          "Desktop"),
              os.path.join(home, "Documents")):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return state_dir()


def _log_files():
    """The client's own log files, newest first."""
    try:
        import Utils                      # absent outside the AP runtime
        d = Utils.user_path("logs")
    except Exception:
        return []
    files = glob.glob(os.path.join(d, "FF1PSPClient*.txt"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[:LOG_FILES_KEPT]


def _yaml_files():
    """The player's own yaml, if this machine is also the one that generated.
    Usually absent (the host has it) -- slot_data.json is the real source."""
    try:
        import Utils
        d = Utils.user_path("Players")
    except Exception:
        return []
    out = []
    for p in glob.glob(os.path.join(d, "*.yaml")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
            if "Final Fantasy 1 PSP" in head:
                out.append(p)
        except Exception:
            pass
    return out[:5]


def build_bundle(ctx=None, ram=None, wedge=None, out_dir=None):
    """Write the zip and return its path. Never raises: a collector that blows
    up lands in the bundle as an error section instead.

    `ram` and `wedge` are collected by the CALLER (they need the asyncio thread
    that owns the bridge); everything else is gathered here."""
    secrets = _redactions(ctx)
    manifest = {
        "bundle_format": 1,
        "recent_errors": _safe(_errors_section, ctx),
        "environment": _safe(_env_section, ctx),
        "iso": _safe(_iso_section, ctx),
        "bake": _safe(_bake_section, ctx),
        "ppsspp_ini": _safe(_ini_section, ctx),
        "processes": _safe(_process_section, ctx),
        "savestates": _safe(_savestate_section, ctx),
        "archipelago": _safe(_ap_section, ctx),
        "bestiary": _safe(_bestiary_section, ctx),
        "ram": ram if ram is not None else {"note": "not collected"},
        "cpu": wedge if wedge is not None else {"note": "not collected"},
        "host": _safe(_host_section, ctx),
        "ppsspp_log": _safe(_ppsspp_log, ctx),
    }
    # The log body ships as its own member; the manifest keeps only the metadata
    # (path/size/mtime) so manifest.json does not carry a second copy.
    _plog = manifest["ppsspp_log"]
    ppsspp_log_tail = _plog.pop("tail", None) if isinstance(_plog, dict) else None
    ap = manifest["archipelago"]
    slot = str(ap.get("slot") or "noslot")
    seed = str(ap.get("seed_name") or "noseed")[:20]
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = lambda s: "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    dest = os.path.join(out_dir or _out_dir(),
                        f"{BUNDLE_PREFIX}_{safe(slot)}_{safe(seed)}_{stamp}.zip")

    def text(zf, name, body):
        zf.writestr(name, _scrub(body, secrets))

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        text(zf, "READ_ME_FIRST.txt", README_TEXT)
        text(zf, "manifest.json", json.dumps(manifest, indent=2, default=str))
        text(zf, "report.txt", _safe_render(manifest))
        text(zf, "ap_state.json", json.dumps(ap, indent=2, default=str))
        text(zf, "slot_data.json", json.dumps(
            getattr(ctx, "slot_data", None) or {}, indent=2, default=str))
        text(zf, "ram_snapshot.json", json.dumps(manifest["ram"], indent=2,
                                                 default=str))
        if ppsspp_log_tail:
            text(zf, "ppsspp_log.txt", ppsspp_log_tail)
        if _RING is not None:
            text(zf, "breadcrumbs.log", "\n".join(_RING.buf))
        try:
            from .launcher import load_cfg
            cfg = {k: v for k, v in load_cfg().items()
                   if k not in ("password", "token")}
            text(zf, "launcher.json", json.dumps(cfg, indent=2, default=str))
        except Exception:
            pass
        for n, p in enumerate(_log_files()):
            try:
                text(zf, f"logs/{n}_{os.path.basename(p)}", _tail(p))
            except Exception as e:
                text(zf, f"logs/{n}_{os.path.basename(p)}.ERROR.txt", repr(e))
        for p in _yaml_files():
            try:
                text(zf, f"yaml/{os.path.basename(p)}",
                     open(p, encoding="utf-8", errors="replace").read())
            except Exception:
                pass
    return dest


def _safe_render(manifest):
    try:
        return _render_report(manifest)
    except Exception:
        return ("report.txt could not be rendered; every fact is still in "
                "manifest.json.\n\n" + traceback.format_exc())


def reveal(path):
    """Open the containing folder with the zip selected. Best-effort: returns
    True if a file manager was launched."""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except Exception:
        return False


# The console/GUI wording. Generic on purpose -- it must age well past whoever
# maintains the apworld today.
def handoff_lines(path):
    return [
        "",
        "Debug bundle saved:",
        f"  {path}",
        "",
        "Send that ONE file to the developer who maintains the FF1 PSP "
        "apworld (post it wherever you report bugs for this game), along with "
        "a sentence about what you were doing when the problem happened.",
        "It contains your logs, seed options, patch state and a read-only "
        "snapshot of the game. Your server password is not included.",
        "",
        "Tip: if the game ever freezes, run this command BEFORE closing "
        "PPSSPP. It can examine a frozen emulator and capture exactly where "
        "it got stuck.",
        "",
    ]
