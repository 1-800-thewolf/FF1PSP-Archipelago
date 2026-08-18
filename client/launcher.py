"""
One-click bootstrap for the FF1 PSP Archipelago client.

Goal: the player connects to their server/slot in the AP client window FIRST,
then the game comes up in PPSSPP automatically, debugger on. This guarantees the
client is running and connected before the game launches.

ensure_ppsspp() (called after the slot connects) does:
  1. If a debugger is already reachable, reuse it.
  2. Else, if saved/auto-detected PPSSPP exe + FF1 (ULUS10251) ISO are known-good,
     launch silently -- no prompt.
  3. Else (first time / paths unknown), show a one-time path prompt asking ONLY
     for the PPSSPP exe + ISO (NOT server/slot/password). Choices are remembered.
  4. Patch PPSSPP's ini so the remote debugger starts on the fixed local port.
  5. Launch PPSSPP on the ISO and wait for the debugger socket.

Everything is best-effort and degrades to manual entry; nothing here raises out
to the Launcher.
"""

import asyncio
import glob
import json
import os
import re
import subprocess
import time

# The fixed local debugger port we pin in the ini. ppsspp_ws.LOCAL_HINT must match.
DEBUG_PORT = 8765
# FF1 20th Anniversary (US) disc id, as matched inside the ISO header read:
# UMD_DATA.BIN carries the dashed form near the disc header; the plain form lives
# deep in the data. Match either so a 1 MB header read is enough to verify.
GAME_ID_NEEDLES = (b"ULUS-10251", b"ULUS10251")

def state_dir() -> str:
    """A writable per-user directory for our config + progress. Must NOT live
    next to __file__: when distributed the apworld is a zip and that path is
    read-only / inside the archive. Prefer AP's user dir, else ~/Archipelago."""
    try:
        from Utils import user_path           # available in the AP runtime
        d = user_path("ff1psp")
    except Exception:
        d = os.path.join(os.path.expanduser("~"), "Archipelago", "ff1psp")
    os.makedirs(d, exist_ok=True)
    return d


_CFG_PATH = os.path.join(state_dir(), "launcher.json")


# ---------------------------------------------------------------- config I/O ---
def load_cfg() -> dict:
    try:
        with open(_CFG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg: dict) -> None:
    try:
        with open(_CFG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# Keys in launcher.json that make up the one-time SETUP (what ppsspp_dialog and
# /psp_remote write). Purging these puts the next connect back on the first-time
# path prompt, exactly like a brand-new install.
SETUP_KEYS = ("ppsspp", "iso", "remote_psp")


def purge_setup() -> dict:
    """Wipe the saved FF1 PSP setup so the next launch behaves like a first-ever
    connection: forget the PPSSPP exe path, the FF1 ISO path and any remote-PPSSPP
    target. Deletes launcher.json outright when nothing else lives in it, else
    strips only SETUP_KEYS (a future non-setup key survives).

    Returns {"cleared": [keys...], "path": <cfg path>, "removed": bool,
             "env": [env vars that still override]}. Never raises."""
    cfg = load_cfg()
    cleared = [k for k in SETUP_KEYS if k in cfg]
    removed = False
    leftovers = {k: v for k, v in cfg.items() if k not in SETUP_KEYS}
    if leftovers:
        save_cfg(leftovers)
    else:
        try:
            if os.path.isfile(_CFG_PATH):
                os.unlink(_CFG_PATH)
            removed = True
        except OSError:
            save_cfg({})
    # env vars are read BEFORE the config everywhere (see _ppsspp_candidates,
    # remote_psp_target) -- a purge cannot clear them, so report them instead.
    env = [v for v in ("FF1PSP_PPSSPP", "FF1PSP_REMOTE")
           if os.environ.get(v)]
    return {"cleared": cleared, "path": _CFG_PATH, "removed": removed,
            "env": env}


# ------------------------------------------------------------- remote PPSSPP ---
def remote_psp_target(cfg: dict = None):
    """(host, port_or_None) of a remote PPSSPP to drive instead of launching a
    local one -- Android phone / handheld on the LAN (memory:
    android-port-feasibility, Phase A). Set via the FF1PSP_REMOTE env var or
    launcher.json {"remote_psp": "host[:port]"} (the /psp_remote command).
    Port omitted = auto-resolve at connect time (match-server lookup covers
    Android's ephemeral default RemoteISOPort=0, then DEBUG_PORT). IPv6
    literals need brackets: [fe80::1]:8765. Returns None when unset/off."""
    raw = os.environ.get("FF1PSP_REMOTE")
    if raw is None:
        raw = (cfg if cfg is not None else load_cfg()).get("remote_psp") or ""
    raw = str(raw).strip()
    if not raw or raw.lower() in ("off", "none", "false", "0"):
        return None
    host, port = raw, None
    h, sep, p = raw.rpartition(":")
    # take a :port suffix only when what's left isn't a bare IPv6 literal
    if sep and p.isdigit() and (":" not in h or h.endswith("]")):
        host, port = h, int(p)
    host = host.strip("[]")
    return (host, port) if host else None


# ------------------------------------------------------------ PPSSPP locating ---
def _ppsspp_candidates():
    """Likely PPSSPP executable paths, best first."""
    out = []
    env = os.environ.get("FF1PSP_PPSSPP")
    if env:
        out.append(env)
    pf = [os.environ.get("ProgramFiles", ""),
          os.environ.get("ProgramFiles(x86)", ""),
          os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    for base in pf:
        if not base:
            continue
        out += [os.path.join(base, "PPSSPP", "PPSSPPWindows64.exe"),
                os.path.join(base, "PPSSPP", "PPSSPPWindows.exe")]
    for steam in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        out.append(os.path.join(steam, "steamapps", "common", "PPSSPP",
                                "PPSSPPWindows64.exe"))
    # bounded glob across shallow roots (catches portable installs like
    # C:\<something>\PPSSPP\PPSSPPWindows64.exe without scanning whole drives)
    home = os.path.expanduser("~")
    patterns = [
        r"C:\*\PPSSPP\PPSSPPWindows64.exe",
        r"C:\*\*\PPSSPP\PPSSPPWindows64.exe",
        os.path.join(home, "*", "PPSSPP", "PPSSPPWindows64.exe"),
        os.path.join(home, "Downloads", "*", "PPSSPPWindows64.exe"),
    ]
    for pat in patterns:
        out += glob.glob(pat)
    return out


def find_ppsspp(saved: str = "") -> str:
    if saved and os.path.isfile(saved):
        return saved
    for c in _ppsspp_candidates():
        if c and os.path.isfile(c):
            return c
    return ""


# --------------------------------------------------------------- ISO locating ---
def _iso_is_ff1(path: str) -> bool:
    """True if the ISO's UMD header carries the FF1 game id ULUS10251."""
    try:
        with open(path, "rb") as f:
            head = f.read(1 << 20)   # 1 MB covers the volume descriptors + boot
        if any(n in head for n in GAME_ID_NEEDLES):
            return True
        # A compressed image hides the game id behind its blocks, so the needle
        # scan cannot see it. Accept the container on its magic and let
        # cso_decompress.ensure_plain_iso expand (or clearly refuse) it at bake
        # time -- rejecting here just made a legitimate .cso look "not FF1".
        from .cso_decompress import image_kind
        return image_kind(path) in ("ciso", "ziso", "dax", "chd")
    except Exception:
        return False


def _iso_candidates(ppsspp_exe: str):
    roots = set()
    if ppsspp_exe:
        exedir = os.path.dirname(ppsspp_exe)
        roots.add(os.path.join(exedir, "PSP", "GAME"))
        roots.add(os.path.join(exedir, "memstick", "PSP", "GAME"))
    docs = os.path.join(os.path.expanduser("~"), "Documents", "PPSSPP",
                        "PSP", "GAME")
    roots.add(docs)
    roots.add(os.path.join(os.path.expanduser("~"), "Downloads"))
    out = []
    for r in roots:
        for ext in ("*.iso", "*.cso", "*.ISO", "*.CSO"):
            out += glob.glob(os.path.join(r, "**", ext), recursive=True)
    return out


def find_iso(ppsspp_exe: str = "", saved: str = "") -> str:
    if saved and os.path.isfile(saved):
        return saved
    cands = _iso_candidates(ppsspp_exe)
    # verified FF1 first; fall back to filename heuristic if none verify
    for c in cands:
        if _iso_is_ff1(c):
            return c
    for c in cands:
        n = os.path.basename(c).lower()
        if "final fantasy" in n and not any(x in n for x in
                                            (" ii", " iii", " iv", "complete",
                                             "crystal", "dissidia", "tactics")):
            return c
    return ""


# ---------------------------------------------------------------- ini patching ---
def find_inis(ppsspp_exe: str) -> list:
    """All ppsspp.ini paths the running PPSSPP might read. Which one is ACTIVE
    depends on portable-mode detection (the Documents copy has won in practice,
    see memory: battle-engine-re), so callers patch EVERY existing candidate --
    a setting left stale in the active one is exactly how FastMemoryAccess=False
    silently tanked game speed.

    Returns EVERY candidate, existing or not -- patch_ini creates the missing
    ones. First-connect on a fresh install is exactly the case that needs this:
    only the exe-dir ini existed, we patched it, and PPSSPP (installed.txt
    present -> non-portable) then CREATED the Documents ini from defaults one
    second later with RemoteDebuggerOnStartup=False, so the debugger never bound
    and the client timed out after 181s (live log 2026-08-07)."""
    if not ppsspp_exe:
        return []
    exedir = os.path.dirname(ppsspp_exe)
    cands = [
        os.path.join(exedir, "PSP", "SYSTEM", "ppsspp.ini"),
        os.path.join(exedir, "memstick", "PSP", "SYSTEM", "ppsspp.ini"),
        os.path.join(os.path.expanduser("~"), "Documents", "PPSSPP",
                     "PSP", "SYSTEM", "ppsspp.ini"),
    ]
    # A non-empty installed.txt names a custom memstick root; PPSSPP reads its
    # first line as a directory and falls back to Documents if it isn't one.
    try:
        marker = os.path.join(exedir, "installed.txt")
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8", errors="replace") as f:
                root = f.readline().strip()
            if root and os.path.isdir(root):
                cands.append(os.path.join(root, "PSP", "SYSTEM", "ppsspp.ini"))
    except Exception:
        pass
    seen, out = set(), []
    for c in cands:
        k = os.path.normcase(os.path.abspath(c))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _set_in_section(lines, section, key, value):
    """Set `key = value` inside [section] in a list of ini lines, inserting the
    section and/or key if missing. Mutates and returns `lines`."""
    sec_hdr = f"[{section}]"
    sec_start = None
    for i, ln in enumerate(lines):
        if ln.strip() == sec_hdr:
            sec_start = i
            break
    if sec_start is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(sec_hdr)
        lines.append(f"{key} = {value}")
        return lines
    # find section end
    sec_end = len(lines)
    for i in range(sec_start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            sec_end = i
            break
    for i in range(sec_start + 1, sec_end):
        s = lines[i].strip()
        if not s or s.startswith(";"):
            continue
        k = s.split("=", 1)[0].strip()
        if k == key:
            lines[i] = f"{key} = {value}"
            return lines
    lines.insert(sec_end, f"{key} = {value}")
    return lines


def patch_ini(ini_path: str) -> bool:
    """Enforce the client's required PPSSPP settings pre-launch: remote
    debugger on DEBUG_PORT, and FastMemoryAccess=True -- RE sessions flip it
    False for memory breakpoints and a stale False makes overworld/battle
    crawl. NB: under True, ALL debugger breakpoints (exec included) are
    silently dead (live-confirmed 2026-07-19) -- no client feature may rely
    on live bps; game-side detection must be baked on-disc instead (e.g.
    bonus_dyn_chests mailbox).
    Only safe while PPSSPP is CLOSED (it rewrites the ini on exit); the
    ensure_ppsspp flow guarantees that. Returns True on success."""
    try:
        os.makedirs(os.path.dirname(ini_path), exist_ok=True)
        bom = ""
        if os.path.isfile(ini_path):
            with open(ini_path, "r", encoding="utf-8-sig") as f:
                text = f.read()
            with open(ini_path, "rb") as f:
                if f.read(3) == b"\xef\xbb\xbf":
                    bom = "﻿"
            lines = text.splitlines()
        else:
            lines = ["[General]"]
        _set_in_section(lines, "General", "RemoteISOPort", DEBUG_PORT)
        _set_in_section(lines, "General", "RemoteDebuggerOnStartup", "True")
        # RE sessions leave CPUCore=0 (pure interpreter) and/or
        # FastMemoryAccess=False behind; either makes the overworld/forests/
        # battle-start grind to a crawl for the player. The client needs JIT +
        # fast mem (it uses no memory breakpoints at runtime), so force both.
        _set_in_section(lines, "CPU", "CPUCore", "1")
        _set_in_section(lines, "CPU", "FastMemoryAccess", "True")
        # Overworld/battle framerate: the bridge (native flag-gated tilemap regen)
        # and the open_world_south path edits (client grid writes) change the
        # tilemap's backing memory at runtime. With TextureBackoffCache off PPSSPP
        # re-hashes+re-uploads those tiles EVERY frame they're on screen, and
        # ReplaceTextures on adds a per-frame replacement hash/lookup on top -- so
        # framerate craters exactly when a modified region (bridge, southern path)
        # scrolls into view while vanilla static tiles (Cornelia dock) stay smooth.
        # Backoff on = treat frequently-changing textures as dynamic and stop
        # caching/replacing them; ReplaceTextures off = no per-frame hash lookup
        # (there is no HD texture pack here). See memory: openworld texture lag.
        _set_in_section(lines, "Graphics", "TextureBackoffCache", "True")
        _set_in_section(lines, "Graphics", "ReplaceTextures", "False")
        with open(ini_path, "w", encoding="utf-8") as f:
            f.write(bom + "\n".join(lines) + "\n")
        return True
    except Exception as e:
        print(f"  [ini] could not patch {ini_path}: {e!r}")
        return False


# --------------------------------------------------------------- launch / wait ---
async def _can_connect() -> bool:
    from .ppsspp_ws import PPSSPP
    try:
        # local_only: probe local candidates only (last-good port, the pinned
        # DEBUG_PORT, then a netstat port scan -- see PPSSPP.connect); the
        # discover() WEB fallback is a sync 5s call wait_for can't cancel, and
        # this runs once per wait_for_debugger poll, so it must stay off.
        p = await asyncio.wait_for(PPSSPP.connect(local_only=True), 3)
        try:
            await p.ws.close()
        except Exception:
            pass
        return True
    except Exception:
        return False


def debugger_up() -> bool:
    try:
        return asyncio.run(_can_connect())
    except Exception:
        return False


# Last launch bookkeeping for self-healing: the Popen of OUR child (so a
# startup death is detectable -- 'Memory init failed' exits within seconds)
# and the (exe, iso) pair so the client watchdog can relaunch mid-session.
_CHILD = None
_LAST_LAUNCH = None


def child_exited():
    """True iff WE launched a PPSSPP and that process has exited."""
    return _CHILD is not None and _CHILD.poll() is not None


def relaunch_last(notify=None, stop=None) -> bool:
    """Self-heal: kill any stray PPSSPP and relaunch the last (exe, iso) we
    started, waiting for the debugger. Used by the client watchdog when the
    emulator dies mid-session. False if we never launched / launch fails."""
    say = notify or print
    if _LAST_LAUNCH is None:
        return False
    exe, iso = _LAST_LAUNCH
    kill_ppsspp()
    time.sleep(1.5)
    say(f"Relaunching PPSSPP: {os.path.basename(exe)} <- {os.path.basename(iso)}")
    launch_ppsspp(exe, iso)
    return wait_for_debugger(timeout=60.0, stop=stop, notify=say)


def launch_ppsspp(exe: str, iso: str):
    global _CHILD, _LAST_LAUNCH
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # survive client exit
    _CHILD = subprocess.Popen([exe, iso], cwd=os.path.dirname(exe),
                              creationflags=flags)
    _LAST_LAUNCH = (exe, iso)
    return _CHILD


def read_u16_via_debugger(addr: int):
    """Read a u16 from the running PPSSPP over the debugger. Returns int or None.
    Used to check whether an already-running game is our patched ISO."""
    from .ppsspp_ws import PPSSPP
    import struct

    async def go():
        p = await asyncio.wait_for(PPSSPP.connect(local_only=True), 3)
        try:
            return struct.unpack("<H", await p.read(addr, 2))[0]
        finally:
            try:
                await p.ws.close()
            except Exception:
                pass
    try:
        return asyncio.run(go())
    except Exception:
        return None


def kill_ppsspp():
    """Terminate any running PPSSPP so we can relaunch on the patched ISO."""
    if os.name != "nt":
        return
    for exe in ("PPSSPPWindows64.exe", "PPSSPPWindows.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", exe],
                           capture_output=True, timeout=10)
        except Exception:
            pass


def ppsspp_process_running() -> bool:
    """True if any PPSSPP process is alive (debugger reachable or not).
    Guards against launching a SECOND instance: with two PPSSPPs the bridge
    can attach to the idle one -- every write lands in dead RAM while the
    live game ignores us (see memory: two-instances silent dead writes)."""
    if os.name != "nt":
        return False
    for exe in ("PPSSPPWindows64.exe", "PPSSPPWindows.exe"):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                capture_output=True, text=True, timeout=5).stdout
            if exe.lower() in (out or "").lower():
                return True
        except Exception:
            pass
    return False


def port_squatter(port=DEBUG_PORT):
    """Identify a NON-PPSSPP process listening on `port`, or None.

    Root cause of a whole class of silent bridge failures (live-diagnosed
    2026-07-28): PPSSPP binds the dual-stack wildcard ([::]:8765), but a
    process that grabbed the more specific 127.0.0.1:8765 first wins every
    loopback connect. Our probes then reach the SQUATTER's server, the WS
    handshake fails, and the client reports "debugger never came up" -- so
    the player toggles 'Allow remote debugger' forever with nothing baked
    wrong. Local RE dev servers (re_only/tile_editor.py) are the usual
    culprit. Returns (pid, image, local_addr) or None."""
    if os.name != "nt":
        return None
    try:
        ns = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                            capture_output=True, text=True, timeout=5).stdout
        owners = []
        for ln in (ns or "").splitlines():
            parts = ln.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                m = re.search(r":(\d+)$", parts[1])
                if m and int(m.group(1)) == int(port):
                    owners.append((parts[4], parts[1]))
        if not owners:
            return None
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=5).stdout
        names = dict((pid, img) for img, pid in
                     re.findall(r'^"([^"]+)","(\d+)"', out or "", re.M))
        for pid, addr in owners:
            img = names.get(pid, "?")
            if not img.lower().startswith("ppsspp"):
                return (pid, img, addr)
    except Exception:
        pass
    return None


def _squatter_msg(sq) -> str:
    pid, img, addr = sq
    return (f"PORT CONFLICT: {img} (pid {pid}) is listening on {addr} -- the "
            f"debugger port. PPSSPP's own listener is the wildcard ([::]:"
            f"{DEBUG_PORT}), so this process wins every localhost connection "
            f"and our probes hit ITS server, not the debugger. Nothing is "
            f"wrong with the ISO bake or 'Allow remote debugger'. Close that "
            f"process (e.g. re_only/tile_editor.py) and reconnect to the "
            f"slot. On Windows: taskkill /F /PID {pid}")


# Shown once if the debugger still hasn't answered after a while. With the
# netstat port scan a healthy PPSSPP connects in seconds; if we're still
# waiting, the webserver truly isn't up and only a live UI toggle binds it.
_TOGGLE_HINT = ("Still waiting… If this doesn't connect: in PPSSPP, open "
                "Game settings -> Tools -> Developer tools and toggle "
                "'Allow remote debugger' OFF then ON (v1.15.3 doesn't bind "
                "it at startup). I'll connect the moment it appears.")


def wait_for_debugger(timeout=180.0, stop=None, notify=None) -> bool:
    """Block until the PPSSPP debugger socket answers, or timeout.
    stop: zero-arg callable checked every poll -- this runs on an executor
    thread that asyncio.run JOINS at interpreter exit, so without an abort
    the client window sits "Not Responding" until the full timeout expires."""
    t0 = time.time()
    deadline = t0 + timeout
    hint_at = t0 + 20.0
    beat_at = t0 + 10.0          # append-only log: a periodic line is the only
    while time.time() < deadline:    # proof to the player that we're alive
        if stop and stop():
            return False
        if debugger_up():
            return True
        if notify and time.time() >= beat_at:
            notify(f"[wait] still waiting for PPSSPP… {int(time.time() - t0)}s")
            beat_at += 10.0
        if notify and hint_at and time.time() >= hint_at:
            # A squatter on the port explains the failure exactly; the generic
            # toggle hint would send the player chasing the wrong thing.
            sq = port_squatter()
            notify(_squatter_msg(sq) if sq else _TOGGLE_HINT)
            hint_at = None
        time.sleep(1.0)
    return False


# ------------------------------------------------------------------- the dialog ---
def known_good(cfg: dict):
    """Return (exe, iso) if saved/auto-detected paths are a known-good, working
    FF1 setup (both files exist AND the ISO verifies as ULUS10251), else None.
    This is the gate for whether we need to show the path prompt at all."""
    exe = find_ppsspp(cfg.get("ppsspp", ""))
    iso = find_iso(exe, cfg.get("iso", ""))
    if exe and iso and _iso_is_ff1(iso):
        return exe, iso
    return None


def ppsspp_dialog(cfg: dict):
    """First-time path prompt: PPSSPP exe + FF1 ISO ONLY (server/slot/password are
    entered by the player in the AP client window). Returns dict(ppsspp, iso) or
    None on cancel. Falls back to console prompts if tkinter is unavailable."""
    ppsspp = find_ppsspp(cfg.get("ppsspp", ""))
    iso = find_iso(ppsspp, cfg.get("iso", ""))

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception:
        return _console_dialog(ppsspp, iso)

    result = {}
    root = tk.Tk()
    root.title("Final Fantasy 1 PSP - Archipelago")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    rows = {}

    def add_row(r, label, value, browse=None):
        tk.Label(root, text=label, anchor="w").grid(row=r, column=0, sticky="w",
                                                    padx=8, pady=4)
        var = tk.StringVar(value=value)
        ent = tk.Entry(root, textvariable=var, width=58)
        ent.grid(row=r, column=1, padx=4, pady=4)
        if browse:
            tk.Button(root, text="Browse...",
                      command=lambda: browse(var)).grid(row=r, column=2, padx=6)
        rows[label] = var
        return var

    def pick_exe(var):
        p = filedialog.askopenfilename(
            title="Select PPSSPPWindows64.exe",
            filetypes=[("PPSSPP", "PPSSPP*.exe"), ("Executables", "*.exe")])
        if p:
            var.set(p)
            if not rows["FF1 ISO:"].get():
                guess = find_iso(p)
                if guess:
                    rows["FF1 ISO:"].set(guess)

    def pick_iso(var):
        p = filedialog.askopenfilename(
            title="Select the FF1 (ULUS10251) ISO",
            filetypes=[("PSP disc", "*.iso *.cso"), ("All files", "*.*")])
        if p:
            var.set(p)

    tk.Label(root, text="One-time setup: confirm where PPSSPP and the FF1 ISO live, "
                        "then click Launch.\nServer / slot / password are entered in "
                        "the Archipelago client window.",
             anchor="w", justify="left", fg="#444").grid(
                 row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 2))
    v_exe = add_row(1, "PPSSPP exe:", ppsspp, pick_exe)
    v_iso = add_row(2, "FF1 ISO:", iso, pick_iso)

    def on_launch():
        exe_p, iso_p = v_exe.get().strip(), v_iso.get().strip()
        if not os.path.isfile(exe_p):
            messagebox.showerror("FF1 PSP", "PPSSPP executable not found.")
            return
        if not os.path.isfile(iso_p):
            messagebox.showerror("FF1 PSP", "FF1 ISO not found.")
            return
        result.update(ppsspp=exe_p, iso=iso_p)
        root.destroy()

    def on_cancel():
        root.destroy()

    btns = tk.Frame(root)
    btns.grid(row=3, column=0, columnspan=3, pady=10)
    tk.Button(btns, text="Launch", width=14, command=on_launch).pack(side="left",
                                                                     padx=6)
    tk.Button(btns, text="Cancel", width=10, command=on_cancel).pack(side="left",
                                                                     padx=6)
    root.bind("<Return>", lambda e: on_launch())
    root.mainloop()
    return result or None


def _console_dialog(ppsspp, iso):
    """Last-resort prompt when no GUI toolkit is available."""
    def ask(label, default):
        try:
            v = input(f"{label} [{default}]: ").strip()
        except EOFError:
            v = ""
        return v or default
    print("\n=== Final Fantasy 1 PSP - one-time setup ===")
    exe = ask("PPSSPP exe", ppsspp)
    iso = ask("FF1 ISO", iso)
    if not (os.path.isfile(exe) and os.path.isfile(iso)):
        print("Missing PPSSPP / ISO - aborting.")
        return None
    return dict(ppsspp=exe, iso=iso)


# --------------------------------------------------- on-disc ISO patching (Route 2) ---
def bake_hash32(bake: dict) -> int:
    """Low-32 hash identifying a bake exactly: patcher version + enabled
    features + every data-patch payload. Two seeds (or two scout results)
    hash differently, so a running game can be recognized as THIS bake."""
    import hashlib
    from . import iso_patcher
    h = hashlib.sha1()
    h.update(f"v{iso_patcher.PATCHER_VERSION}".encode())
    feats = (bake or {}).get("features") or {}
    h.update(",".join(sorted(k for k, v in feats.items() if v)).encode())
    for p in (bake or {}).get("data") or []:
        h.update(p["name"].encode())
        h.update(int(p["iso_off"]).to_bytes(8, "little"))
        if "values" in p:
            for idx in sorted(p["values"]):
                h.update(int(idx).to_bytes(4, "little"))
                h.update(int(p["values"][idx]).to_bytes(4, "little"))
        else:
            h.update(p["patched"])
    # pad_key_ids widens KEY_NAME slots ON DISC (lute_tablets ratio) but lives
    # outside features/data, so without this a tablets seed and an otherwise
    # identical non-tablets seed would hash the SAME and wrongly reuse each
    # other's cached ISO (padded vs unpadded Lute slot).
    for kid in sorted((bake or {}).get("pad_key_ids") or []):
        h.update(f"padkey={int(kid)}".encode())
    # The overworld u16 companion is baked INTO the cave segment (v230), not
    # applied as a data patch, so its bytes never reach the `data` loop above.
    # Two seeds differing only in their high-byte table must still hash apart.
    if feats.get("_ow_hi"):
        h.update(b"owhi=")
        h.update(bytes(feats["_ow_hi"]))
    # crystals_needed changes the wrapper cave's baked threshold -> fold the
    # VALUE in explicitly (the truthy-name fold above would hash 3 == 2, and
    # would drop 0 entirely -- 0 is a meaningful value: orb opens immediately).
    if feats.get("crystals_needed") is not None:
        h.update(f"crystals={int(feats['crystals_needed'])}".encode())
    # boss-minion plan changes formation records + MS2 packs -> fold the plan
    # CONTENT in (feature names alone can't distinguish two seeds' plans).
    for entry in feats.get("boss_minions_plan") or []:
        fid, groups = entry[0], entry[1]
        h.update(int(fid).to_bytes(2, "little"))
        # layout is part of the baked formation record -> fold it in too.
        h.update(bytes((int(entry[2]) & 0xFF,)) if len(entry) > 2 else b"\xff")
        for g in groups:
            h.update(bytes(int(x) & 0xFF for x in g))
    # remote box names change the baked NAME bank -> must re-bake / re-detect.
    # Entries are (who, item) pairs since v241 (legacy strings still hash).
    for nm in (bake or {}).get("remote_names") or []:
        if isinstance(nm, (list, tuple)):
            nm = "\x1f".join(str(x) for x in nm)
        h.update(nm.encode("utf-8", "replace"))
        h.update(b"\0")
    # dyn name slots grow the same bank -> fold the count in
    if (bake or {}).get("dyn_name_slots"):
        h.update(f"dynslots={int(bake['dyn_name_slots'])}".encode())
    # key-item box names change the baked KEY_NAME bank -> same deal
    kn = (bake or {}).get("key_names") or {}
    for kid in sorted(kn):
        h.update(int(kid).to_bytes(2, "little"))
        h.update(str(kn[kid]).encode("utf-8", "replace"))
        h.update(b"\0")
    # per-map obtain sentences (evm_bake) change the USEVM bundles -> re-bake
    on = (bake or {}).get("obtain_names") or {}
    for kid in sorted(on):
        h.update(b"evm")
        h.update(int(kid).to_bytes(2, "little"))
        h.update(str(on[kid]).encode("utf-8", "replace"))
        h.update(b"\0")
    # the caravan presale line is baked into FM_SHOPUS.PCK -> a seed that only
    # differs in what the Caravan holds must still re-bake, not reuse a cached ISO
    co = (bake or {}).get("caravan_offer") or {}
    if co:
        h.update(str(co.get("name", "")).encode("utf-8", "replace"))
        h.update(b"\0")
        for d in co.get("descs") or []:
            h.update(str(d).encode("utf-8", "replace"))
            h.update(b"\0")
    return int.from_bytes(h.digest()[:4], "little")


def _prune_patched_isos(keep_path: str, keep=2):
    """Delete stale cached patched ISOs (each seed bakes its own ~1.6 GB copy;
    without pruning they accumulate forever). Keeps `keep` most-recent plus
    the one in use."""
    try:
        d = state_dir()
        isos = [os.path.join(d, f) for f in os.listdir(d)
                if f.startswith("ff1psp_patched_") and f.endswith(".iso")]
        isos.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in isos[keep:]:
            if os.path.abspath(p) != os.path.abspath(keep_path):
                # marker AND manifest die with their ISO,
                for victim in (p, p + ".done", p + ".f1ap.json"):
                    try:                          # or it would outlive it and
                        os.remove(victim)         # claim a deleted bake is cached
                    except Exception:
                        pass
        # Orphan markers/manifests (ISO deleted by hand / an older build's prune).
        for f in os.listdir(d):
            for suffix in (".iso.done", ".iso.f1ap.json"):
                if not (f.startswith("ff1psp_patched_") and f.endswith(suffix)):
                    continue
                stem = f[:-(len(suffix) - len(".iso"))]
                if not os.path.isfile(os.path.join(d, stem)):
                    try:
                        os.remove(os.path.join(d, f))
                    except Exception:
                        pass
    except Exception:
        pass


def _write_bake_manifest(out_iso, hash32, feats, data, src_iso, src_stat):
    """Record WHAT this cached ISO is, next to it, as `<iso>.f1ap.json`.

    The `.done` marker only says "a bake finished here"; it cannot say which
    bake, so a cache directory was previously unreadable after the fact -- the
    2026-08-08 wrong-bake reports needed the hash reconstructed by hand from
    the log. The manifest is pure diagnostics (nothing reads it to make a
    decision; the cache key is still the filename tag) and is shipped by
    /ff1psp_logs. Never raises: a failed manifest must not fail a good bake."""
    try:
        from . import iso_patcher
        with open(out_iso + ".f1ap.json", "w", encoding="utf-8") as f:
            json.dump({
                "bake_hash32": f"{hash32:08x}",
                "patcher_version": iso_patcher.PATCHER_VERSION,
                "baked_utc": time.strftime("%Y-%m-%d %H:%M:%SZ",
                                           time.gmtime()),
                "features": sorted(k for k, v in (feats or {}).items()
                                   if v and not k.startswith("_")),
                "data_tables": [p.get("name") for p in (data or [])],
                "source_iso": src_iso,
                "source_bytes": int(src_stat.st_size),
                "source_mtime": int(src_stat.st_mtime),
            }, f, indent=2)
    except Exception:
        pass


class BakeFailed(Exception):
    """The seed could not be baked into the player's ISO.

    Raised instead of silently returning the UNPATCHED ISO. That fallback
    shipped a seed that looked playable and was not: with no bake there are no
    AP shop items, no shuffled magic levels, no remote chest names and no
    shop-purchase mailbox, so buying an AP offer sends no check and hands over
    the vanilla item (user report 2026-08-08 -- an hour of play on an
    unpatched ISO, the failure banner scrolled past unnoticed)."""


def ensure_patched_iso(iso: str, bake: dict, notify=None) -> str:
    """Bake the seed (data tables + on-disc code features) into a cached
    patched COPY of the player's ISO and return that path; if nothing needs
    baking, return `iso` unchanged. The player's original ISO is never
    modified. Cache key = patcher version + iso identity + bake hash, so a new
    seed / changed scout / fixed patcher re-bakes and stale copies get pruned.

    Raises BakeFailed if the bake was needed and could not be produced."""
    say = notify or print
    try:
        from . import iso_patcher
    except Exception as e:
        # Same reasoning as BakeFailed below: no patcher = no bake = a seed that
        # looks playable and isn't. Never fall through to the unpatched ISO.
        say(f"[patch] iso_patcher unavailable: {e!r}")
        raise BakeFailed(f"iso_patcher unavailable: {e!r}") from e
    feats = (bake or {}).get("features") or {}
    data = (bake or {}).get("data") or []
    # pad_key_ids is an on-disc KEY_NAME edit that lives outside features/data,
    # so it must independently authorize a bake (lute_tablets seed with no other
    # code feature would otherwise boot unpadded and the ratio could never show).
    pad_keys = (bake or {}).get("pad_key_ids") or []
    # the caravan line is a text-only bake outside features/data -- like
    # pad_key_ids it must authorize a bake on its own, or a seed with no code
    # feature at all would boot with the vanilla "Faerie's Bottle" row.
    caravan = (bake or {}).get("caravan_offer") or None
    if not iso or not (iso_patcher.any_enabled(feats, data) or pad_keys
                       or caravan):
        return iso
    # Compressed images have no absolute disc offsets, so every seek in the
    # patcher is meaningless on one. Expand a CSO here (once, cached) before
    # anything tries to read the executable.
    from .cso_decompress import ensure_plain_iso, CompressedImage
    try:
        iso = ensure_plain_iso(iso, say)
    except CompressedImage as e:
        raise BakeFailed(str(e)) from e
    try:
        st = os.stat(iso)
        hash32 = bake_hash32(bake)
        import hashlib
        key = f"{int(st.st_size)}-{int(st.st_mtime)}-{hash32:08x}"
        tag = hashlib.sha1(key.encode()).hexdigest()[:12]
        out = os.path.join(state_dir(), f"ff1psp_patched_{tag}.iso")
        # Completion MARKER, not a size compare. The old check demanded the
        # patched ISO match the SOURCE size -- but whenever a cave grows the ELF
        # past EBOOT_SLOT, _relocate_eboot appends it and the image grows (live
        # 2026-08-03: 198,115,328 -> 199,997,440), so the check never matched and
        # every launch re-baked the same 1.6 GB for minutes. The marker is
        # written only after patch_iso RETURNS, so a crashed/half-written bake
        # still fails the check and re-bakes.
        done = out + ".done"
        if os.path.isfile(out) and os.path.isfile(done):
            say(f"Using your cached patched game ({os.path.basename(out)}) — "
                f"already baked for this seed, no wait.")
            _prune_patched_isos(out)
            return out
        enabled = sorted(k for k, v in feats.items() if v)
        n_tables = len(data)
        what = ", ".join(enabled) if enabled else "no code features"
        say(f"Baking this seed into a patched copy of your ISO ({what}; "
            f"{n_tables} data tables).")
        say("One-time for this seed; takes ~1-4 minutes depending on your disk "
            "speed.")
        say("Progress below; the client may look idle between updates. Your "
            "original ISO is untouched.")
        t0 = time.monotonic()
        iso_patcher.patch_iso(iso, out, feats, data_patches=data,
                              bake_hash32=hash32,
                              remote_names=(bake or {}).get("remote_names"),
                              key_names=(bake or {}).get("key_names"),
                              obtain_names=(bake or {}).get("obtain_names"),
                              pad_key_ids=(bake or {}).get("pad_key_ids"),
                              caravan_offer=caravan,
                              dyn_name_slots=(bake or {}).get("dyn_name_slots")
                              or 0,
                              progress=say)
        with open(done, "w", encoding="utf-8") as f:   # bake complete; cacheable
            f.write(key)
        _write_bake_manifest(out, hash32, feats, data, iso, st)
        say(f"Patched game ready (took {int(time.monotonic() - t0)} seconds).")
        _prune_patched_isos(out)
        return out
    except Exception as e:
        # LOUD: a bake failure silently boots the UNPATCHED ISO, so every
        # on-disc code feature (boss minions, dabble-in-magic, curated
        # encounters, ...) is missing for the WHOLE session. A single log line
        # scrolled past unnoticed once (2026-07-23: player only noticed via the
        # downstream TimeoutError flood) -- a bordered multi-line banner is far
        # harder to miss even at info level.
        bar = "!" * 60
        for line in (
            "", bar,
            "!!!  PATCH FAILED -- NOT LAUNCHING THE GAME  !!!",
            # str(e), not repr: our own patch errors put the player-facing
            # explanation (and the /checkiso pointer) in __str__, and repr
            # shows only the raw args tuple.
            f"!!!  reason: {type(e).__name__}: {e}",
            "!!!  Without the bake, on-disc code features (boss minions,",
            "!!!  dabble-in-magic, curated encounters, AP shop items, magic",
            "!!!  levels, remote chest names) do not exist, and the seed is",
            "!!!  not playable: shop purchases send no checks and key items",
            "!!!  are the vanilla ones. Booting anyway would only waste your",
            "!!!  time, so the launch is aborted.",
            "!!!",
            "!!!  NEXT STEP: type  /checkiso  in this window. It inspects the",
            "!!!  ISO you are already using and tells you exactly what is",
            "!!!  wrong with it and how to fix it. No file paths to type.",
            "!!!  If /checkiso says the ISO is fine, type  /ff1psp_logs  and",
            "!!!  send the zip it saves to whoever maintains this apworld.",
            bar, ""):
            say(line)
        # patch_iso can raise AFTER build_iso wrote a full-size iso_out (the
        # post-build dpk bakes edit it in place). No .done marker is written on
        # this path, so it could not be cached anyway -- but remove the
        # half-patched file too, so it never eats 1.6 GB for nothing.
        try:
            if 'out' in locals() and os.path.isfile(out):
                os.unlink(out)
        except OSError:
            pass
        raise BakeFailed(str(e)) from e


# --------------------------------------------------------------- orchestration ---
def ensure_ppsspp(bake: dict = None, notify=None, stop=None) -> bool:
    """Make sure PPSSPP is up with THIS seed's game loaded and the debugger
    reachable. Called AFTER the player has connected to a server/slot (and after
    the scout, so `bake` carries the seed's full data + feature set).

    - debugger already up AND running this exact bake (tag match) -> reuse it.
    - known-good saved/auto-detected paths -> bake + launch silently (no prompt).
    - otherwise -> show the one-time PPSSPP/ISO path prompt, save, then launch.

    Blocking (runs in an executor thread off the client event loop). Returns True
    once the debugger answers, else False (cancel / timeout). stop: zero-arg
    callable (client exit_event) -- aborts the debugger wait so this thread can't
    pin interpreter exit (asyncio.run joins executor threads -> frozen window)."""
    say = notify or print
    cfg = load_cfg()
    bake = bake or {}
    feats = bake.get("features") or {}
    data = bake.get("data") or []
    from . import iso_patcher

    if debugger_up():
        # A game is already running. Reuse it UNLESS this seed needs a bake and
        # the running game isn't this exact bake (per-seed tag) -> auto-relaunch
        # on the freshly patched copy.
        if not iso_patcher.any_enabled(feats, data):
            say("PPSSPP debugger already running — reusing it.")
            return True
        # Reads through a just-connected debugger can transiently fail
        # (read_u16 -> None). That is NOT evidence of a wrong bake -- killing a
        # correctly patched game on it loses unsaved progress. Retry a few
        # times and only relaunch on a CONFIRMED mismatch (reads succeeded,
        # values wrong).
        verdict = "unreadable"
        for attempt in range(5):
            if attempt:
                time.sleep(1.0)
            verdict = iso_patcher.patched_running_verdict(
                read_u16_via_debugger, feats, bake_hash32(bake))
            if verdict != "unreadable":
                break
            say("Couldn't read the running game's bake tag yet "
                f"(attempt {attempt + 1}/5); retrying…")
        if verdict == "ok":
            say("PPSSPP already running this seed's patched game — reusing it.")
            return True
        if verdict == "unreadable":
            say("Couldn't verify the running game's bake (debugger reads kept "
                "failing). Reusing it rather than killing a possibly-correct "
                "game; the client re-verifies the bake after connect.")
            return True
        say("The running game isn't patched for this seed (bake tag mismatch "
            "confirmed). Closing it and relaunching on the freshly baked ISO…")
        kill_ppsspp()
        time.sleep(1.5)
        # fall through to the normal (patched) launch path below
    elif ppsspp_process_running():
        # PPSSPP is up but its debugger never answered (e.g. webserver not
        # bound). NEVER launch alongside it: a second instance silently eats
        # bridge writes. Close it and start fresh on the patched ISO.
        say("A PPSSPP is already running but its debugger is unreachable. "
            "Closing it and relaunching (two instances would break the "
            "bridge)…")
        kill_ppsspp()
        time.sleep(1.5)

    good = known_good(cfg)
    if good:
        exe, iso = good
    else:
        chosen = ppsspp_dialog(cfg)
        if not chosen:
            say("PPSSPP setup cancelled.")
            return False
        cfg.update(chosen)
        save_cfg(cfg)
        exe, iso = chosen["ppsspp"], chosen["iso"]

    for ini in find_inis(exe):   # patch EVERY candidate; the active one varies
        patch_ini(ini)
    try:
        iso = ensure_patched_iso(iso, bake, say)   # bake seed data + code features
    except BakeFailed:
        return False        # banner already printed; do NOT boot an unpatched ISO
    # Self-healing launch: if OUR child exits during startup (the 'Memory init
    # failed' pattern -- a stale instance held the PSP memory and the new one
    # gave up), kill every stray and try once more instead of handing the
    # session a dead emulator (live log 2026-07-30: debugger answered for 15s,
    # then the process died and the client error-flooded forever).
    for attempt in range(2):
        say(f"Launching PPSSPP: {os.path.basename(exe)} <- {os.path.basename(iso)}")
        launch_ppsspp(exe, iso)
        say("Waiting for PPSSPP debugger… (the emulator loads the disc first; "
            "this can take a couple minutes)")
        if wait_for_debugger(stop=stop, notify=say):
            if not child_exited():
                say("PPSSPP debugger is up.")
                say("Enjoy Final Fantasy 1 Randomizer for PSP!")
                return True
            # debugger answered but our child is already dead (another
            # instance answered, or it died right after binding the port)
            say("PPSSPP exited right after starting.")
        if stop and stop():
            say("Client closing — stopped waiting for the debugger.")
            return False
        if attempt == 0 and child_exited():
            say("PPSSPP died during startup (usually a duplicate/stale "
                "instance holding the PSP memory). Killing every PPSSPP "
                "and retrying once…")
            kill_ppsspp()
            time.sleep(2.0)
            continue
        sq = port_squatter()
        if sq:
            say(_squatter_msg(sq))
        else:
            say("Timed out waiting for the PPSSPP debugger. Is "
                "'Allow remote debugger' / RemoteDebuggerOnStartup "
                "enabled?")
        return False
    return False
