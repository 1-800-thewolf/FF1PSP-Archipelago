"""Host-process and TCP-socket helpers for the PPSSPP bridge, per platform.

Everything here answers one of three questions the launcher, the WS dialer and
the debug bundle keep asking about the machine the client runs on:

  * which PPSSPP processes exist        -> ppsspp_pids / ppsspp_running / kill_ppsspp
  * which TCP ports they LISTEN on      -> listening_ports (PPSSPP's debugger
    webserver binds an EPHEMERAL port whenever RemoteISOPort is 0 / unpinned)
  * who else owns the debugger port     -> port_owners (the "squatter" diagnosis)

Windows keeps the tasklist / netstat / taskkill implementation that shipped
first (moved here verbatim from launcher.py, ppsspp_ws.py and ppsspp_mem.py).
Linux reads /proc directly: no external tool, and it sees flatpak-sandboxed
processes -- Flathub's org.ppsspp.PPSSPP runs PPSSPPSDL in a child pid
namespace, but the host's /proc still lists it, and because the flatpak is
built with --share=network its sockets appear in the host's /proc/net/tcp.
macOS has no /proc and falls back to pgrep / lsof, best effort.

Why this module exists (Steam Deck, 2026-09-07): every one of these lookups
returned "nothing" off Windows. The debugger probe therefore dialled only the
pinned port while PPSSPP sat on an ephemeral one, kill_ppsspp was a no-op (so
the "died during startup" retry launched a SECOND emulator), and
"PPSSPP process running" was always False. See memory:
linux-steam-deck-feasibility.

No imports from the rest of the package: launcher.py, ppsspp_ws.py and
ppsspp_mem.py all import this, and launcher already imports ppsspp_ws.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0        # CREATE_NO_WINDOW

# Windows image names (tasklist / taskkill).
WIN_IMAGES = ("PPSSPPWindows64.exe", "PPSSPPWindows.exe")

# Flathub id. EmuDeck installs this on the Steam Deck; its exported launcher is
# a ~90-byte `sh` wrapper around `flatpak run`, and its config root is
# ~/.var/app/<id>/config (the sandbox's XDG_CONFIG_HOME).
FLATPAK_APP_ID = "org.ppsspp.PPSSPP"

# /proc/net/tcp state column for LISTEN.
_LISTEN = "0A"

# When PPSSPP is running but /proc/<pid>/fd is unreadable (hardened ptrace
# scope, hidepid), listening_ports offers the unprivileged wildcard/loopback
# listeners instead. Capped: the launcher probe has a ~3 s budget per poll and
# every wrong candidate can cost a WS open_timeout.
FALLBACK_PORT_CAP = 8

_SOCK_RE = re.compile(r"socket:\[(\d+)\]")


# ------------------------------------------------------------------ naming ---
def comm_is_ppsspp(name: str) -> bool:
    """PPSSPPSDL / PPSSPPQt (upstream builds), ppsspp (distro packages), and
    PPSSPPWindows64 under Wine -- /proc/<pid>/comm is capped at 15 chars, so
    the '.exe' is cut off there."""
    return (name or "").strip().lower().startswith("ppsspp")


def is_flatpak_exe(path: str) -> bool:
    """True for the flatpak's exported launcher -- /var/lib/flatpak/exports/bin/
    org.ppsspp.PPSSPP, the versioned .../export/bin/ twin the file picker lands
    on, or the per-user ~/.local/share/flatpak copy. It is a tiny `sh` script
    that execs `flatpak run`, NOT the emulator binary, so nothing beside it
    (memstick, ini, logs) means anything."""
    if not path:
        return False
    p = str(path).replace("\\", "/")
    if os.path.basename(p) == FLATPAK_APP_ID or "/flatpak/" in p:
        return True
    try:
        if os.path.isfile(p) and os.path.getsize(p) < 4096:
            with open(p, "rb") as f:
                return b"flatpak run" in f.read(4096)
    except OSError:
        pass
    return False


def stray_process_hint() -> str:
    """Player-facing: how to spot a leftover emulator on THIS OS."""
    if IS_WINDOWS:
        return "check Task Manager for a stray PPSSPPWindows64.exe"
    if IS_MAC:
        return "check Activity Monitor for a stray PPSSPPSDL"
    return "check for a stray PPSSPPSDL process (ps aux | grep -i ppsspp)"


def kill_hint(pid) -> str:
    """Player-facing: the one command that ends process `pid` on this OS."""
    if IS_WINDOWS:
        return f"On Windows: taskkill /F /PID {pid}"
    return f"kill {pid}"


# ---------------------------------------------------------- Linux /proc I/O ---
def _linux_pids():
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    return [int(n) for n in names if n.isdigit()]


def _linux_read(path, binary=False):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return b"" if binary else ""
    return data if binary else data.decode("utf-8", "replace")


def _linux_comm(pid) -> str:
    return _linux_read(f"/proc/{pid}/comm").strip()


def _linux_cmdline(pid) -> str:
    raw = _linux_read(f"/proc/{pid}/cmdline", binary=True)
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _linux_stat_fields(path):
    """Fields of a /proc/<pid>/stat (or task/<tid>/stat) line AFTER the comm:
    index 0 = state, 11 = utime, 12 = stime (ticks). comm may contain spaces
    and parens, so split after the LAST ')'."""
    text = _linux_read(path)
    if not text:
        return []
    tail = text.rsplit(")", 1)[-1]
    return tail.split()


def linux_proc_state(pid) -> str:
    """One-letter scheduler state: R running, S sleeping, D disk wait,
    Z zombie, T stopped. '' when unreadable."""
    f = _linux_stat_fields(f"/proc/{pid}/stat")
    return f[0] if f else ""


def linux_thread_cpu_times(pid) -> dict:
    """tid -> cpu time in 100 ns units (same unit as the Windows collector in
    debug_bundle, so the busy-thread math is shared)."""
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError, AttributeError):
        hz = 100
    scale = 1e7 / hz
    out = {}
    base = f"/proc/{pid}/task"
    try:
        tids = os.listdir(base)
    except OSError:
        return out
    for tid in tids:
        f = _linux_stat_fields(f"{base}/{tid}/stat")
        if len(f) > 12:
            try:
                out[int(tid)] = (int(f[11]) + int(f[12])) * scale
            except ValueError:
                pass
    return out


def _decode_proc_ip(hexip: str) -> str:
    """/proc/net/tcp prints IPv4 as one little-endian u32, tcp6 as four of
    them. Decode to dotted / colon form; only the loopback/wildcard question
    matters to callers, so v6 is not canonically compressed."""
    try:
        if len(hexip) == 8:
            return ".".join(str(b) for b in bytes.fromhex(hexip)[::-1])
        if len(hexip) == 32:
            raw = "".join(bytes.fromhex(hexip[i:i + 8])[::-1].hex()
                          for i in range(0, 32, 8))
            if raw == "0" * 32:
                return "::"
            if raw == "0" * 31 + "1":
                return "::1"
            if raw.startswith("0" * 20 + "ffff"):           # v4-mapped
                return "::ffff:" + ".".join(
                    str(b) for b in bytes.fromhex(raw[24:]))
            return ":".join(raw[i:i + 4] for i in range(0, 32, 4))
    except ValueError:
        pass
    return hexip


def parse_proc_net_tcp(text: str):
    """Rows of /proc/net/tcp or /proc/net/tcp6 as (local_ip, port, state,
    inode). Pure -- exercised offline by test_hostproc.py."""
    rows = []
    for ln in text.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 10:
            continue
        local, st, inode = parts[1], parts[3], parts[9]
        hip, sep, hport = local.rpartition(":")
        if not sep:
            continue
        try:
            rows.append((_decode_proc_ip(hip), int(hport, 16), st.upper(),
                         int(inode)))
        except ValueError:
            continue
    return rows


def _linux_listen_sockets():
    """[(local_ip, port, inode)] for every LISTEN socket the host can see."""
    out = []
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        text = _linux_read(fn)
        if text:
            out += [(ip, port, ino) for ip, port, st, ino
                    in parse_proc_net_tcp(text) if st == _LISTEN]
    return out


def _linux_socket_inodes(pid) -> set:
    """Socket inodes held by `pid`. Empty when /proc/<pid>/fd is not ours to
    read (EACCES: different uid, non-dumpable, hidepid)."""
    inodes = set()
    d = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(d)
    except OSError:
        return inodes
    for fd in fds:
        try:
            m = _SOCK_RE.match(os.readlink(os.path.join(d, fd)))
        except OSError:
            continue
        if m:
            inodes.add(int(m.group(1)))
    return inodes


def _linux_ppsspp_pids():
    pids = []
    for pid in _linux_pids():
        if comm_is_ppsspp(_linux_comm(pid)) and linux_proc_state(pid) != "Z":
            pids.append(pid)
    return pids


def _linux_listening_ports(pids):
    socks = _linux_listen_sockets()
    inodes = set()
    for pid in pids:
        inodes |= _linux_socket_inodes(pid)
    ports = []
    if inodes:
        for _ip, port, ino in socks:
            if ino in inodes and port not in ports:
                ports.append(port)
        return ports
    # PPSSPP exists but its fd table is closed to us: offer the unprivileged
    # wildcard/loopback listeners. The WS handshake (subprotocol
    # debugger.ppsspp.org) rejects everything that isn't PPSSPP, so a wrong
    # guess only costs time -- hence the cap.
    for ip, port, _ino in socks:
        if (port >= 1024 and ip in ("0.0.0.0", "::", "127.0.0.1", "::1")
                and port not in ports):
            ports.append(port)
    return ports[:FALLBACK_PORT_CAP]


def _linux_port_owners(port):
    socks = [(ip, ino) for ip, p, ino in _linux_listen_sockets()
             if p == int(port)]
    if not socks:
        return []
    want = {ino for _ip, ino in socks}
    out = []
    for pid in _linux_pids():
        hit = want & _linux_socket_inodes(pid)
        if hit:
            ip = next(ip for ip, ino in socks if ino in hit)
            out.append((pid, _linux_comm(pid) or "?", f"{ip}:{port}"))
    return out


def _linux_process_table():
    rows = []
    for pid in _linux_pids():
        comm = _linux_comm(pid)
        if comm:
            rows.append((pid, comm, _linux_cmdline(pid)))
    return rows


# ------------------------------------------------------------- Windows I/O ---
def _win_ppsspp_pids():
    pids = []
    for exe in WIN_IMAGES:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3,
                creationflags=_NO_WINDOW).stdout
            pids += [int(p) for p in re.findall(r'^"[^"]+","(\d+)"', out, re.M)]
        except Exception:
            pass
    return pids


def _win_netstat_listeners():
    """[(local_addr, port, pid_str)] from `netstat -ano -p TCP` LISTENING rows."""
    rows = []
    try:
        ns = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                            capture_output=True, text=True, timeout=5,
                            creationflags=_NO_WINDOW).stdout
    except Exception:
        return rows
    for ln in (ns or "").splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[3] == "LISTENING":
            m = re.search(r":(\d+)$", parts[1])
            if m:
                rows.append((parts[1], int(m.group(1)), parts[4]))
    return rows


def _win_listening_ports(pids):
    pidset = {str(p) for p in pids}
    ports = []
    for _addr, port, pid in _win_netstat_listeners():
        if pid in pidset and port not in ports:
            ports.append(port)
    return ports


def _win_port_owners(port):
    owners = [(addr, pid) for addr, p, pid in _win_netstat_listeners()
              if p == int(port)]
    if not owners:
        return []
    names = {}
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=_NO_WINDOW).stdout
        names = dict((pid, img) for img, pid in
                     re.findall(r'^"([^"]+)","(\d+)"', out or "", re.M))
    except Exception:
        pass
    return [(pid, names.get(pid, "?"), addr) for addr, pid in owners]


# --------------------------------------------------------------- macOS I/O ---
def _mac_ppsspp_pids():
    try:
        out = subprocess.run(["pgrep", "-i", "ppsspp"], capture_output=True,
                             text=True, timeout=3).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        return []


def _mac_lsof(args):
    try:
        return subprocess.run(["lsof", "-nP"] + args, capture_output=True,
                              text=True, timeout=5).stdout
    except Exception:
        return ""


def _mac_listening_ports(pids):
    ports = []
    for pid in pids:
        for ln in _mac_lsof(["-a", "-p", str(pid), "-iTCP",
                             "-sTCP:LISTEN"]).splitlines()[1:]:
            m = re.search(r":(\d+)\s*\(LISTEN\)", ln)
            if m and int(m.group(1)) not in ports:
                ports.append(int(m.group(1)))
    return ports


def _mac_port_owners(port):
    out = []
    for ln in _mac_lsof([f"-iTCP:{port}", "-sTCP:LISTEN"]).splitlines()[1:]:
        parts = ln.split()
        if len(parts) >= 9 and parts[1].isdigit():
            out.append((int(parts[1]), parts[0], parts[8]))
    return out


def _mac_process_table():
    rows = []
    try:
        out = subprocess.run(["ps", "-axo", "pid=,comm=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return rows
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit():
            rows.append((int(parts[0]), os.path.basename(parts[1]),
                         parts[2] if len(parts) > 2 else ""))
    return rows


# ------------------------------------------------------------- public API ---
def ppsspp_pids() -> list:
    """PIDs of every live PPSSPP on this machine (any build, any packaging)."""
    if IS_WINDOWS:
        return _win_ppsspp_pids()
    if IS_LINUX:
        return _linux_ppsspp_pids()
    if IS_MAC:
        return _mac_ppsspp_pids()
    return []


def ppsspp_running() -> bool:
    return bool(ppsspp_pids())


def kill_ppsspp(grace: float = 2.0) -> None:
    """End every PPSSPP. Windows: taskkill /F by image. POSIX: SIGTERM, then
    SIGKILL whatever survives `grace` seconds; the flatpak instance is also
    told to stop through `flatpak kill` so bwrap tears the sandbox down.
    A killed PPSSPP never rewrites ppsspp.ini on the way out, which is what
    we want -- the settings patch_ini just wrote stay put."""
    if IS_WINDOWS:
        for exe in WIN_IMAGES:
            try:
                subprocess.run(["taskkill", "/F", "/IM", exe],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        return
    pids = ppsspp_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if IS_LINUX and shutil.which("flatpak"):
        try:
            subprocess.run(["flatpak", "kill", FLATPAK_APP_ID],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    if not pids:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and ppsspp_pids():
        time.sleep(0.2)
    for pid in ppsspp_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def listening_ports(pids=None) -> list:
    """TCP ports the PPSSPP processes (default: all of them) LISTEN on --
    i.e. where the debugger webserver actually landed. [] when no PPSSPP."""
    if pids is None:
        pids = ppsspp_pids()
    if not pids:
        return []
    if IS_WINDOWS:
        return _win_listening_ports(pids)
    if IS_LINUX:
        return _linux_listening_ports(pids)
    if IS_MAC:
        return _mac_listening_ports(pids)
    return []


def port_owners(port) -> list:
    """[(pid, image_name, local_addr)] for every LISTEN socket on `port`.
    Dual-stack matters: PPSSPP's own [::]:port and a squatter's
    127.0.0.1:port are two rows. [] when nothing listens or nothing is
    identifiable."""
    if IS_WINDOWS:
        return _win_port_owners(port)
    if IS_LINUX:
        return _linux_port_owners(port)
    if IS_MAC:
        return _mac_port_owners(port)
    return []


def process_table() -> list:
    """[(pid, name, cmdline)] for the debug bundle on POSIX. Windows keeps its
    tasklist CSV path in debug_bundle._process_section."""
    if IS_LINUX:
        return _linux_process_table()
    if IS_MAC:
        return _mac_process_table()
    return []
