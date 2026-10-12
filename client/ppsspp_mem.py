"""Direct process-memory bridge to PPSSPP (Windows + Linux).

The WS debugger stalls for whole minutes while the emulator is busy (fresh
boot JIT warmup, heavy scenes): RPC timeouts starved the client's runtime
loops and produced the "features don't work when first starting" class of
bugs. This module replaces the WS transport for MEMORY I/O with
ReadProcessMemory/WriteProcessMemory straight into PPSSPP's emulated PSP RAM
-- microsecond, stall-free, no sockets. The WS connection stays for what
process memory can't do: breakpoints (chest hooks), cpu resume, game_status.

How the emulated RAM is found: PPSSPP VirtualAllocs one big reserve and maps
PSP address space inside it, so host_base + psp_addr = host pointer for the
whole 0x08000000..0x0A000000 window. We enumerate committed RW regions of
PSP-RAM size and validate a candidate by reading the on-disc bake tag
(iso_patcher writes BAKE_TAG_MAGIC "F1AP" at SAFE_CAVE_VADDR 0x08B30E00 into
every patched ISO). The tag requirement makes false positives (e.g. the JIT
cache, also a big RW region) impossible: we NEVER accept a region without it,
and fall back to the WS transport instead. A pre-boot attach that misses the
tag (ELF not mapped yet) self-heals: HybridPSP re-tries the attach on a 2 s
gate from inside every call until it sticks.

PPSSPP maps the same RAM at several PSP views (cached/uncached mirrors of one
file mapping); whichever region validates first is used -- they alias the
same bytes, so the choice is irrelevant.

Linux (2026-09-07, Steam Deck): the same idea over /proc/<pid>/maps (RW
regions) and /proc/<pid>/mem (pread/pwrite). It matters MORE there than on
Windows: current PPSSPP runs every websocket memory.read/write inside
Core_RunOnCPUThread + LockMemory, so a WS-only client interrupts the
emulation thread on every poll and the game crawled at ~35% speed. Reading
/proc/<pid>/mem needs ptrace access to the process: same uid AND
kernel.yama.ptrace_scope permitting (SteamOS/Arch default 1 = ancestors
only; the flatpak wrapper exits, so PPSSPPSDL is often NOT our descendant).
A denied open is diagnosed once, with the one-line sysctl fix, and the
client stays on the WS fallback until it is granted.
"""
import ctypes
import os
import struct
import sys
import time

import logging
logger = logging.getLogger("Client")
# Diagnostic tier; never reaches the client window. See ApClient.dlog.
dlog = logging.getLogger("FF1PSP")

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

# PSP address window served by this transport (RAM incl. our cave/tag).
PSP_RAM_LO = 0x08000000
PSP_RAM_HI = 0x0A000000
# Bake tag (iso_patcher.BAKE_TAG_ADDR / BAKE_TAG_MAGIC): our only accepted
# proof that a host region really is FF1's emulated RAM.
TAG_ADDR = 0x08B30E00
TAG_MAGIC = b"F1AP"
# Emulated-RAM commit sizes seen from PPSSPP (24 MB retail / 32 MB).
MIN_REGION = 0x01800000


if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _MBI(ctypes.Structure):   # MEMORY_BASIC_INFORMATION (x64 layout)
        _fields_ = [("BaseAddress", ctypes.c_void_p),
                    ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", ctypes.c_uint32),
                    ("PartitionId", ctypes.c_uint16),
                    ("RegionSize", ctypes.c_size_t),
                    ("State", ctypes.c_uint32),
                    ("Protect", ctypes.c_uint32),
                    ("Type", ctypes.c_uint32)]

    _PROC_RIGHTS = (0x0400 | 0x0010 | 0x0020 | 0x0008)  # QUERY|VM_READ|VM_WRITE|VM_OP
    _MEM_COMMIT = 0x1000
    _PAGE_READWRITE = 0x04


class MemUnavailable(Exception):
    """Direct-memory transport is not attached / lost the process."""


def _ppsspp_pids():
    """PIDs of every live PPSSPP -- any platform, see hostproc. Kept under
    this name because debug_bundle imports it."""
    from .hostproc import ppsspp_pids
    return ppsspp_pids()


def parse_proc_maps(text: str, min_size: int = MIN_REGION):
    """RW regions (start, size) of a /proc/<pid>/maps dump, largest-first.
    Pure -- tested offline. PPSSPP's emulated RAM is a big rw-p/rw-s mapping
    (a memfd/shm view on Linux); mirrors of it appear as separate rows and
    all validate against the tag equally."""
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) < 2 or "-" not in parts[0]:
            continue
        if not parts[1].startswith("rw"):
            continue
        try:
            lo, hi = (int(x, 16) for x in parts[0].split("-", 1))
        except ValueError:
            continue
        if hi - lo >= min_size:
            out.append((lo, hi - lo))
    out.sort(key=lambda r: -r[1])
    return out


def ptrace_scope() -> str:
    """kernel.yama.ptrace_scope as a string ('' when Yama is absent)."""
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope", "r") as f:
            return f.read().strip()
    except OSError:
        return ""


PTRACE_FIX = ("sudo sysctl kernel.yama.ptrace_scope=0    (permanent: "
              "echo kernel.yama.ptrace_scope=0 | sudo tee "
              "/etc/sysctl.d/10-ptrace.conf ; Steam Deck: set a password "
              "first with  passwd  in Konsole)")


class PPSSPPMem:
    """Sync process-memory I/O behind the async PPSSPP transport API.
    Windows: ReadProcessMemory. Linux: /proc/<pid>/mem."""

    def __init__(self):
        self.pid = None
        self.handle = None         # Win32 HANDLE, or the /proc/<pid>/mem fd
        self.host_base = None      # host addr - psp addr (host ptr = base + psp_addr)
        self._next_attach = 0.0    # monotonic gate: don't hammer attach attempts
        self.proc_seen = True      # last attach scan found SOME PPSSPP process
                                   # (optimistic before the first scan so the WS
                                   # fallback still carries the fresh-boot window)
        self.denied = False        # Linux: open(/proc/<pid>/mem) refused (ptrace)
        self._denied_logged = False

    # ---------------- backend primitives ----------------
    def _open_process(self, pid):
        """Handle for pid, or None. Linux lets PermissionError through so
        attach can tell 'denied' from 'gone'."""
        if IS_WINDOWS:
            return _k32.OpenProcess(_PROC_RIGHTS, False, pid) or None
        if IS_LINUX:
            return os.open(f"/proc/{pid}/mem", os.O_RDWR)
        return None

    def _release(self, handle):
        try:
            if IS_WINDOWS:
                _k32.CloseHandle(handle)
            else:
                os.close(handle)
        except Exception:
            pass

    def _close_handle(self):
        if self.handle is not None:
            self._release(self.handle)
        self.handle = None
        self.pid = None
        self.host_base = None

    def _read_raw(self, handle, host_addr, size):
        if IS_WINDOWS:
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            ok = _k32.ReadProcessMemory(handle, ctypes.c_void_p(host_addr), buf,
                                        ctypes.c_size_t(size), ctypes.byref(got))
            if not ok or got.value != size:
                return None
            return buf.raw
        try:
            raw = os.pread(handle, size, host_addr)
        except OSError:                     # EIO: unmapped / process gone
            return None
        return raw if len(raw) == size else None

    def _write_raw(self, handle, host_addr, data: bytes) -> bool:
        if IS_WINDOWS:
            n = ctypes.c_size_t(0)
            ok = _k32.WriteProcessMemory(handle, ctypes.c_void_p(host_addr),
                                         data, ctypes.c_size_t(len(data)),
                                         ctypes.byref(n))
            return bool(ok) and n.value == len(data)
        try:
            return os.pwrite(handle, data, host_addr) == len(data)
        except OSError:
            return False

    def _regions(self, handle, pid=None):
        """Committed RW regions (base, size), largest-first."""
        if IS_LINUX:
            try:
                with open(f"/proc/{pid}/maps", "r") as f:
                    return parse_proc_maps(f.read())
            except OSError:
                return []
        out = []
        addr = 0
        mbi = _MBI()
        while addr < 0x00007FFFFFFF0000:
            if not _k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                       ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size == 0:
                break
            if (mbi.State == _MEM_COMMIT and mbi.Protect == _PAGE_READWRITE
                    and size >= MIN_REGION):
                out.append((base, size))
            addr = base + size
        out.sort(key=lambda r: -r[1])
        return out

    def _note_denied(self, pid, err):
        """Linux: /proc/<pid>/mem refused. Say it ONCE, with the fix -- the
        WS fallback keeps the game playable but slow, and nothing else in the
        log explains why a Linux session crawls."""
        self.denied = True
        if self._denied_logged:
            return
        self._denied_logged = True
        scope = ptrace_scope()
        logger.error(
            f"  [bridge] Linux: cannot open PPSSPP's memory (pid {pid}: "
            f"{getattr(err, 'strerror', None) or err}). "
            f"kernel.yama.ptrace_scope={scope or '?'}. The game will run SLOW "
            "on the websocket fallback until this is allowed. One-time fix in "
            f"a terminal:  {PTRACE_FIX}  -- then reconnect; no need to restart "
            "PPSSPP.")

    # ---------------- attach / locate ----------------
    def attach(self):
        """Find a PPSSPP whose RAM carries the bake tag. True on success."""
        if not (IS_WINDOWS or IS_LINUX):
            return False
        now = time.monotonic()
        if now < self._next_attach:
            return False
        self._next_attach = now + 2.0
        self._close_handle()
        pids = list(_ppsspp_pids())
        self.proc_seen = bool(pids)
        for pid in pids:
            try:
                handle = self._open_process(pid)
            except PermissionError as e:
                self._note_denied(pid, e)
                continue
            except OSError:
                continue
            if handle is None:
                continue
            found = None
            for base, size in self._regions(handle, pid):
                # Hypothesis: this region IS the RAM block mapped at
                # PSP_RAM_LO, so host_base = region base - 0x08000000.
                cand = base - PSP_RAM_LO
                tag = self._read_raw(handle, cand + TAG_ADDR, len(TAG_MAGIC))
                if tag == TAG_MAGIC:
                    found = cand
                    break
            if found is not None:
                self.pid, self.handle, self.host_base = pid, handle, found
                if self.denied:
                    logger.info("  [bridge] Linux: PPSSPP memory access granted "
                                "-- direct memory I/O from here on.")
                self.denied = False
                return True
            self._release(handle)
        return False

    @property
    def attached(self):
        return self.handle is not None

    def _host(self, address, size):
        if not (PSP_RAM_LO <= address and address + size <= PSP_RAM_HI):
            raise ValueError(f"address out of PSP RAM: {address:#x}+{size:#x}")
        if not self.attached:
            raise MemUnavailable("not attached")
        return self.host_base + address

    # ---------------- transport API (async signatures, sync work) ----------
    # A failed read/write marks the transport detached so HybridPSP falls
    # back to WS and retries attach on its gate (PPSSPP restarted -> new
    # pid/base; savestate loads keep the mapping, so those never detach us).
    async def read(self, address, size):
        raw = self._read_raw(self.handle, self._host(address, size), size)
        if raw is None:
            self._close_handle()
            raise MemUnavailable("read failed (process gone?)")
        return raw

    async def read_chunked(self, address, size, chunk=0x40000, breathe=0.0):
        # One syscall reads megabytes in ~ms with zero emulator-side cost;
        # chunking/breathing existed to protect the WS server thread.
        return await self.read(address, size)

    async def write(self, address, data: bytes):
        host = self._host(address, len(data))
        if not self._write_raw(self.handle, host, data):
            self._close_handle()
            raise MemUnavailable("write failed (process gone?)")

    async def write_guarded(self, address, data: bytes, guards=()):
        """Write `data` only while every `(addr, expected)` guard still reads
        exactly `expected`. True = written, False = a guard moved (nothing
        written).

        The guard reads and the write run back to back inside this one call
        with no await in between, so no other client coroutine can run in the
        gap and the window against the emulator thread is a few microseconds.
        That is the point: engine heaps and resource banks are recycled the
        moment the engine frees them, and the next pack's COMPRESSED stream is
        staged at the tail of the recycled chunk -- a check-await-write gap was
        long enough for v330's overworld paint to land in Crescent Lake's
        stream mid-load (Eridose 2026-10-05, RA 0x088fba28)."""
        for gaddr, want in guards:
            raw = self._read_raw(self.handle, self._host(gaddr, len(want)),
                                 len(want))
            if raw is None:
                self._close_handle()
                raise MemUnavailable("read failed (process gone?)")
            if raw != want:
                return False
        host = self._host(address, len(data))
        if not self._write_raw(self.handle, host, data):
            self._close_handle()
            raise MemUnavailable("write failed (process gone?)")
        return True

    async def read_u32(self, address):
        return struct.unpack("<I", await self.read(address, 4))[0]

    async def write_u32(self, address, value):
        await self.write(address, struct.pack("<I", value & 0xFFFFFFFF))

    async def close(self):
        self._close_handle()


class HybridPSP:
    """Transport with the PPSSPP WS API: direct memory first, WS fallback.

    Wraps one shared PPSSPPMem and this consumer's own WS socket. Memory ops
    go through process memory whenever attached (re-attach retried on a 2 s
    gate from inside the call path); anything else -- and any memory op while
    detached -- rides the WS socket exactly as before.
    """

    def __init__(self, mem: PPSSPPMem, ws):
        self.mem = mem
        self.ws = ws
        self._mode_logged = None

    # surface the WS attrs ApClient inspects
    @property
    def reconnects(self):
        return getattr(self.ws, "reconnects", 0)

    @property
    def closed(self):
        return getattr(self.ws, "closed", False)

    def _mem_ok(self):
        if not self.mem.attached:
            self.mem.attach()
        ok = self.mem.attached
        if ok is not self._mode_logged:
            self._mode_logged = ok
            dlog.info("  [bridge] memory I/O via %s",
                        "direct process memory" if ok else "WS debugger (fallback)")
        return ok

    async def _memory_op(self, name, *args, **kw):
        if self._mem_ok():
            try:
                return await getattr(self.mem, name)(*args, **kw)
            except MemUnavailable:
                pass                      # detached mid-call -> WS below
            except ValueError:
                pass                      # outside RAM window (VRAM?) -> WS can serve
        elif not self.mem.proc_seen:
            # No PPSSPP PROCESS at all (user closed it): don't fall back to WS
            # -- every fallback would re-DIAL, and dials into a closing/gone
            # PPSSPP wedge its WS server thread ("Not Responding" on close,
            # see close-freeze memory). Fail fast; loops already handle it.
            raise ConnectionError("PPSSPP process gone (skipping WS dial)")
        return await getattr(self.ws, name)(*args, **kw)

    async def read(self, address, size):
        return await self._memory_op("read", address, size)

    async def read_chunked(self, address, size, chunk=0x40000, breathe=0.0):
        return await self._memory_op("read_chunked", address, size,
                                     chunk=chunk, breathe=breathe)

    async def write(self, address, data):
        return await self._memory_op("write", address, data)

    async def write_guarded(self, address, data, guards=()):
        return await self._memory_op("write_guarded", address, data, guards)

    async def read_u32(self, address):
        return await self._memory_op("read_u32", address)

    async def write_u32(self, address, value):
        return await self._memory_op("write_u32", address, value)

    # WS-only surface
    async def rpc(self, event, **params):
        return await self.ws.rpc(event, **params)

    async def game_status(self):
        return await self.ws.game_status()

    async def close(self):
        try:
            await self.ws.close()
        finally:
            await self.mem.close()
