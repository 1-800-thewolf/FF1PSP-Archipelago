"""One-click troubleshooting bundle -- the `/ff1psp_logs` command.

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
  * breadcrumbs           -- last few hundred log records even if the log file
                             rotated or the player scrolled past the banner

DEFENSIVE BY CONTRACT: every collector is individually wrapped. A section that
fails records its own traceback into the bundle and the bundle still builds --
a debug tool that dies on the broken machine it was written for is worthless.

Nothing here writes to the game, opens a debugger connection of its own, or
touches breakpoints; the RAM snapshot rides the client's existing bridge and is
read-only (see CLAUDE.md: never leave the CPU halted).
"""

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

# The player-facing note that ships inside the zip. Deliberately generic: it
# must still read correctly for whoever maintains this apworld in future.
README_TEXT = """\
FF1 PSP Archipelago -- debug bundle
===================================

This zip was made by the /ff1psp_logs command in the FF1 PSP client.

WHAT TO DO WITH IT
  Send this single file to the developer who maintains the FF1 PSP apworld
  (post it wherever you report bugs for it -- the Archipelago Discord thread
  or channel for this game is the usual place). Say what you were doing when
  the problem happened; that plus this zip is normally enough to diagnose it.

WHAT IS IN IT
  report.txt        readable summary -- versions, game patch state, setup
  client_log*.txt   the client's own log (the main diagnostic)
  breadcrumbs.log   the most recent log lines, even if a log file rotated
  ap_state.json     which checks you have sent and items you have received
  slot_data.json    the options your seed was generated with
  ram_snapshot.json a read-only peek at story flags / key items / inventory
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
        return {"note": "process listing is Windows-only"}
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
              os.path.join(home, ".config", "ppsspp", "PSP", "PPSSPP_STATE")]
    found = []
    for r in roots:
        for p in glob.glob(os.path.join(r, "*ULUS10251*")):
            found.append(_stat(p))
    return {"searched": roots, "states": found}


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
        },
    }


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


def build_bundle(ctx=None, ram=None, out_dir=None):
    """Write the zip and return its path. Never raises: a collector that blows
    up lands in the bundle as an error section instead."""
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
        "ram": ram if ram is not None else {"note": "not collected"},
    }
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
    ]
