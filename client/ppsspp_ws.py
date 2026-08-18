"""
Reusable PPSSPP WebSocket debugger client.

Shared foundation for the scanner (RE) and the Archipelago connector.
Verified against PPSSPP v1.15.3. memory.read returns base64.

SELF-HEALING: rpc() transparently reconnects and retries ONCE when the socket
times out or drops. A wedged debugger connection used to kill every client
loop for the rest of the session (each RPC timing out forever); a fresh
socket restores service in one round-trip. Debugger ERROR replies (e.g.
"CPU not started") ride a healthy socket and are NOT retried.
"""

import asyncio
import base64
import collections
import json
import os
import re
import subprocess
import time

import requests
import websockets

MATCH_LIST = "https://report.ppsspp.org/match/list"
SUBPROTOCOL = "debugger.ppsspp.org"

# Fixed local port we pinned in ppsspp.ini ([General] RemoteISOPort). Trying this
# first avoids the flaky match-server round-trip on localhost dev. GOTCHA: on
# PPSSPP v1.15.3 the startup webserver IGNORES this pin and binds an EPHEMERAL
# port instead -- local_ports() below finds the real one without a web call.
LOCAL_HINT = ("127.0.0.1", 8765)

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW

# local_ports() spawns tasklist+netstat (~0.5s); cache briefly so callers that
# poll every second (launcher wait) or fire at shutdown don't pay it each time.
_PORTS_CACHE = (0.0, [])
_PORTS_TTL = 3.0

# (ip, port) of the most recent SUCCESSFUL dial (any caller: launcher probe or
# client sockets). Tried first on the next connect() so the three client
# sockets and every reconnect skip straight to the port that actually works --
# during a heavy game boot each wrong candidate costs a full open_timeout.
_LAST_GOOD = None


def local_ports():
    """Ports local PPSSPP processes are LISTENING on (Windows netstat scan).

    PPSSPP v1.15.3 starts its webserver (which carries the WS debugger) on an
    ephemeral port at boot -- RemoteISOPort in ppsspp.ini is NOT honored -- so
    dialing the pinned LOCAL_HINT alone times out forever. This finds the real
    port purely locally; the match-server discover() stays as a last resort."""
    global _PORTS_CACHE
    if os.name != "nt":
        return []
    now = time.monotonic()
    if now - _PORTS_CACHE[0] < _PORTS_TTL:
        return _PORTS_CACHE[1]
    ports = []
    try:
        pids = set()
        for exe in ("PPSSPPWindows64.exe", "PPSSPPWindows.exe"):
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3,
                creationflags=_NO_WINDOW).stdout
            pids |= set(re.findall(r'^"[^"]+","(\d+)"', out, re.M))
        if pids:
            ns = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                capture_output=True, text=True, timeout=5,
                                creationflags=_NO_WINDOW).stdout
            for ln in ns.splitlines():
                parts = ln.split()
                if (len(parts) >= 5 and parts[3] == "LISTENING"
                        and parts[4] in pids):
                    m = re.search(r":(\d+)$", parts[1])
                    if m and int(m.group(1)) not in ports:
                        ports.append(int(m.group(1)))
    except Exception:
        pass
    _PORTS_CACHE = (now, ports)
    return ports

# PSP user RAM. Game data lives here; default scan window = full 24 MB user space.
USER_RAM_BASE = 0x08800000
USER_RAM_SIZE = 0x01800000


def discover():
    """Return list of (ip, port) for PPSSPP instances with remote debugger on."""
    try:
        data = requests.get(MATCH_LIST, timeout=5).json()
    except Exception:
        return []
    out = []
    for inst in data:
        ip = inst.get("ip")
        port = inst.get("p") or inst.get("port")
        if ip and port:
            out.append((ip, int(port)))
    return out


class PPSSPP:
    def __init__(self, ip, port):
        host = f"[{ip}]" if ":" in str(ip) else ip   # bracket IPv6 literals
        self.url = f"ws://{host}:{port}/debugger"
        self.ws = None
        self._ticket = 0
        self._lock = asyncio.Lock()   # serialize rpc: one send/recv at a time
        self.reconnects = 0           # observability: how often we had to re-dial
        self.closed = False           # close() ran: rpc must fail fast, never re-dial
        self._next_dial = 0.0         # monotonic deadline gating re-dial attempts
        # Unsolicited broadcasts (cpu.stepping etc.) buffered by rpc() instead
        # of being discarded -- see the rpc() recv loop. RE harnesses drain
        # this; empty in normal client play (no armed breakpoints).
        self.events = collections.deque()

    async def _dial(self):
        # ping_interval=None: PPSSPP's debugger does not pong while the CPU
        # is halted at a breakpoint, so the default keepalive would drop the
        # connection mid-grant. We poll often enough to detect a dead socket.
        self.ws = await websockets.connect(
            self.url, subprotocols=[SUBPROTOCOL],
            max_size=64 * 1024 * 1024, open_timeout=4,
            ping_interval=None, ping_timeout=None)

    @classmethod
    async def connect(cls, ip=None, port=None, local_only=False, scan=True):
        """Connect to a given ip/port, or auto-discover the first instance.

        Dial order: LOCAL_HINT first, then the ports local PPSSPP processes
        actually LISTEN on (local_ports() netstat scan -- v1.15.3 binds an
        ephemeral port and ignores the ini pin), then the match-server lookup
        (discover). discover() is a SYNCHRONOUS web request (up to 5s) that
        blocks the whole event loop, so no wait_for around this call can time
        it out. local_only=True skips discover entirely: shutdown/atexit
        cleanup and local-instance probes must never stall on a web call.
        scan=False also skips the (sync, ~0.5s) netstat scan -- for atexit
        paths that must fail fast."""
        async def try_dial(cip, cport):
            global _LAST_GOOD
            self = cls(cip, cport)
            await self._dial()
            _LAST_GOOD = (cip, cport)
            return self

        last = None
        cands = [(ip, port)] if ip is not None else []
        if ip is None:
            if _LAST_GOOD:
                cands.append(_LAST_GOOD)
            if LOCAL_HINT not in cands:
                cands.append(LOCAL_HINT)
            if scan:
                cands += [("127.0.0.1", p) for p in local_ports()
                          if ("127.0.0.1", p) not in cands]
        for cip, cport in cands:
            try:
                return await try_dial(cip, cport)
            except Exception as e:
                last = e
        if ip is None and not local_only:
            for dip, dport in discover():
                # the match server reports the public (often IPv6) address; the
                # debugger also listens locally, so try localhost on that port too.
                for cip, cport in (("127.0.0.1", dport), (dip, dport)):
                    try:
                        return await try_dial(cip, cport)
                    except Exception as e:
                        last = e
        raise RuntimeError(f"No PPSSPP found (enable remote debugger). last={last}")

    async def close(self):
        # Mark closed FIRST: any in-flight/queued rpc must fail fast instead of
        # re-dialing -- reconnects into a closing PPSSPP wedge its WS server
        # thread and hang its shutdown ("Not Responding").
        self.closed = True
        if self.ws:
            try:
                await asyncio.wait_for(self.ws.close(), 2)
            except Exception:
                pass
            self.ws = None

    # Per-request ceiling on how long we wait for PPSSPP to answer. The debugger can
    # briefly stop answering (boot/intro sequence, or the CPU momentarily halted), and
    # rpc holds self._lock while it waits -- so a reply that never comes would block
    # EVERY other coroutine sharing this socket indefinitely (once caused an 8.5-minute
    # stall of all memory loops). Time-boxing recv turns that into a caught exception,
    # and the transport-retry below replaces the (possibly wedged) socket.
    RPC_TIMEOUT = 8.0

    # After a FAILED re-dial, hold off further dial attempts this long. All the
    # client loops share this connection; without the gate they hammer instant
    # reconnects into a closing/gone PPSSPP, and a connection accepted mid-teardown
    # wedges its WS server thread -> PPSSPP hangs "Not Responding" on exit.
    REDIAL_BACKOFF = 5.0

    async def rpc(self, event, **params):
        # The PPSSPP debugger multiplexes one ws; concurrent send/recv from two
        # coroutines corrupts the stream. Serialize each request/response pair.
        async with self._lock:
            for attempt in (0, 1):
                if self.closed:
                    raise RuntimeError(f"{event}: PPSSPP connection closed")
                try:
                    if self.ws is None:
                        if asyncio.get_running_loop().time() < self._next_dial:
                            raise ConnectionError("re-dial backing off")
                        try:
                            await self._dial()
                        except Exception:
                            self._next_dial = (asyncio.get_running_loop().time()
                                               + self.REDIAL_BACKOFF)
                            raise
                        self.reconnects += 1
                    self._ticket += 1
                    ticket = str(self._ticket)
                    await self.ws.send(json.dumps({"event": event,
                                                   "ticket": ticket, **params}))
                    while True:
                        reply = json.loads(await asyncio.wait_for(
                            self.ws.recv(), self.RPC_TIMEOUT))
                        # Stale replies (a previous request whose recv was cancelled
                        # or timed out) are skipped by ticket mismatch.
                        if reply.get("ticket") == ticket:
                            if reply.get("event") == "error":
                                # Healthy socket, real debugger error -> raise as-is.
                                raise RuntimeError(
                                    f"{event} error: {reply.get('message')}")
                            return reply
                        # Un-ticketed frames are UNSOLICITED BROADCASTS (e.g.
                        # cpu.stepping when an exec breakpoint fires). Discarding
                        # them here silently ate breakpoint halts for any RE
                        # harness whose event wait raced an in-flight rpc -- the
                        # CPU stayed halted with nobody notified (2026-07-15
                        # all-night debugger goose chase). Buffer them for
                        # consumers (drain via events.popleft()).
                        # Production client never arms breakpoints, so this
                        # deque stays empty in normal play.
                        if reply.get("ticket") is None:
                            self.events.append(reply)
                            while len(self.events) > 256:
                                self.events.popleft()
                except RuntimeError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Timeout / closed / OS error: the socket state is unknown.
                    # Drop it and retry ONCE on a fresh connection (all our RPCs
                    # -- memory r/w, bp add/remove, resume -- are idempotent).
                    try:
                        if self.ws is not None:
                            await asyncio.wait_for(self.ws.close(), 2)
                    except Exception:
                        pass
                    self.ws = None
                    if attempt:
                        raise

    # --- memory ---
    async def read(self, address, size):
        """Read `size` bytes starting at `address`. Returns bytes."""
        r = await self.rpc("memory.read", address=address, size=size)
        return base64.b64decode(r["base64"])

    # 256 KB chunks: a 1 MB memory.read makes PPSSPP build+send a ~1.4 MB
    # base64 reply on its WS server thread -- observed taking whole seconds on
    # a busy emu, holding this socket's rpc lock long enough to starve every
    # other loop into TimeoutError (and stuttering the game). Smaller replies
    # keep each lock-hold short.
    async def read_chunked(self, address, size, chunk=0x40000, breathe=0.0):
        """Read a large region in chunks; returns the full bytes. `breathe`
        sleeps between chunks so background scans spread their emu-side cost
        across frames instead of one burst -- keep it 0 on latency-critical
        paths (e.g. reads while the CPU is halted at a breakpoint)."""
        out = bytearray()
        off = 0
        while off < size:
            n = min(chunk, size - off)
            out += await self.read(address + off, n)
            off += n
            if breathe and off < size:
                await asyncio.sleep(breathe)
        return bytes(out)

    async def write(self, address, data: bytes):
        b64 = base64.b64encode(data).decode()
        await self.rpc("memory.write", address=address, base64=b64)

    async def read_u32(self, address):
        return (await self.rpc("memory.read_u32", address=address))["value"]

    async def write_u32(self, address, value):
        await self.rpc("memory.write_u32", address=address, value=value)

    async def game_status(self):
        r = await self.rpc("game.status")
        return r.get("game")
