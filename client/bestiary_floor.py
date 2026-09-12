"""Bestiarysanity kill-count floor -- the pure state machine behind
ApClient._bestiary_loop. No I/O, no asyncio, no AP runtime: the loop feeds it
raw frames of the save-resident u16[203] kill table and acts on what it says.

THE INVARIANT: inside one save, a kill count never goes down. So a frame in
which ANY entry sits below the last stable frame (or below the floor) proves
the save block now holds a different save (New Game, Continue on another
file, a savestate rewind) -- and every one of those means "start counting
from here". The floor follows the drop; it is never a permanent high-water
mark. The reference is the last stable frame, not just the floor: firing a
check never raises the floor, so a file that is behind what we last saw yet
still above the floor would otherwise pass as "this save" and its higher
entries would be credited as kills.

Why (2026-09-07, Violet): her first-ever connect happened with an OLDER seed's
save on screen (Continue before New Game). The one-shot baseline snapshot
took THAT save's counts and the client compared every later kill against
them forever -- Ogre needed more kills than the old file had, 4 was not
enough, and three sessions of grinding sent nothing while logging nothing.

Every decision is taken on a STABLE frame only: two consecutive identical
sane reads. Mid-load frames are transient, so they can never snapshot,
rebase, or fire a check; the price is one extra 2s tick of latency.

Decisions (kind, detail) from step():
  reject    (reason)        frame unusable; reason counted for a once-log
  wait      (why)           nothing to do yet (server unknown / unstable)
  snapshot  (counts)        no floor anywhere yet -> caller stores this one
  rebase    (dropped ids)   floor lowered to this frame -> caller pushes it
  fire      (monster ids)   sampled monsters now above the floor
  idle      (None)          stable frame, nothing changed
"""

# A u16 can hold 65535, but no real bestiary entry gets anywhere near this;
# the guard exists to throw away a frame of not-yet-loaded bytes, not to
# bound real data. The old 999 was a real-data landmine (a heavy grinder can
# pass it), and tripping it was fatal AND silent.
KILLS_MAX = 9999


class BestiaryFloor:
    def __init__(self, n, sampled):
        self.n = int(n)
        self.sampled = [int(m) for m in (sampled or [])
                        if 0 <= int(m) < self.n]
        self.floor = None        # list[n] once the server (or a rebase) set it
        self.known = False       # True once the server answered the connect Get
        self.malformed = False   # server holds junk under our key -> replace it
        self.snap = None         # last sane frame (tab display, /bestiary)
        self.snap_changed = False
        self.rejected = {}       # reason -> times seen (once-log support)
        self.last = ("wait", "no frame yet")
        self._prev = None        # previous sane frame (stability witness)
        self._seen = None        # last STABLE frame acted on: the drop reference.
                                 # Firing never raises the floor, so "below the
                                 # floor" alone misses a file that is behind what
                                 # we last saw but still above the floor.

    # ---------------------------------------------------------- server side
    def apply_server(self, value):
        """Install the value the server holds under our key. None = absent."""
        self.known = True
        if isinstance(value, list) and len(value) == self.n:
            try:
                self.floor = [int(v) for v in value]
                self.malformed = False
                return True
            except (TypeError, ValueError):
                pass
        if value is not None:
            self.malformed = True
        return False

    # ---------------------------------------------------------------- frames
    def parse(self, raw):
        """u16[n] little-endian, or None if the read came back short."""
        if raw is None or len(raw) != self.n * 2:
            return None
        return [int.from_bytes(raw[i:i + 2], "little")
                for i in range(0, self.n * 2, 2)]

    def step(self, raw, sent_mons=()):
        """Feed one frame; return (kind, detail) -- see the module docstring.
        `sent_mons` = sampled monster ids whose check already went out."""
        counts = self.parse(raw)
        if counts is None:
            return self._reject(f"short read ({0 if raw is None else len(raw)} "
                                f"of {self.n * 2} bytes)")
        hi = max(counts)
        if hi > KILLS_MAX:
            return self._reject(f"count {hi} at id {counts.index(hi)} exceeds "
                                f"{KILLS_MAX} (not a loaded save frame)")
        self.snap_changed = counts != self.snap
        self.snap = counts
        stable = self._prev == counts
        self._prev = list(counts)
        if not self.known:
            return self._done("wait", "server has not answered the baseline Get")
        if not stable:
            return self._done("wait", "frame not yet stable")
        if self.floor is None:
            # Space repeated stores out: the SetReply echo installs the floor,
            # and until it lands every stable frame would re-send the Set.
            self._prev = None
            return self._done("snapshot", list(counts))
        ref = self._seen or self.floor
        dropped = [i for i in range(self.n)
                   if counts[i] < self.floor[i] or counts[i] < ref[i]]
        self._seen = list(counts)
        if dropped:
            self.floor = list(counts)
            return self._done("rebase", dropped)
        fire = [m for m in self.sampled
                if counts[m] > self.floor[m] and m not in sent_mons]
        if fire:
            return self._done("fire", fire)
        return self._done("idle", None)

    def _reject(self, reason):
        self._prev = None
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        return self._done("reject", reason)

    def _done(self, kind, detail):
        self.last = (kind, detail)
        return kind, detail

    # --------------------------------------------------------------- reports
    def rows(self, sent_mons=(), names=None):
        """One dict per sampled monster: what the loop sees for it right now."""
        names = names or {}
        out = []
        for m in self.sampled:
            c = self.snap[m] if self.snap is not None else None
            f = self.floor[m] if self.floor is not None else None
            if m in sent_mons:
                state = "sent"
            elif c is None or f is None:
                state = "unknown"
            elif c > f:
                state = "over floor, check pending"
            elif c == f:
                state = "at floor, next kill sends"
            else:
                state = f"under floor by {f - c}"
            out.append({"id": m, "name": names.get(m, f"monster {m:#04x}"),
                        "count": c, "floor": f, "state": state})
        return out

    def summary(self):
        """Machine-readable state for the debug bundle."""
        nz = (sum(1 for c in self.snap if c) if self.snap is not None else None)
        top = (max(range(self.n), key=lambda i: self.snap[i])
               if self.snap is not None else None)
        return {
            "server_answered": self.known,
            "floor_present": self.floor is not None,
            "floor_malformed": self.malformed,
            "snapshot_present": self.snap is not None,
            "entries_nonzero": nz,
            "top_entry": ({"id": top, "count": self.snap[top]}
                          if top is not None else None),
            "under_floor": (sum(1 for i in range(self.n)
                                if self.snap[i] < self.floor[i])
                            if self.snap is not None and self.floor is not None
                            else None),
            "last_decision": {"kind": self.last[0],
                              "detail": (self.last[1] if not isinstance(
                                  self.last[1], list) else
                                  f"{len(self.last[1])} ids")},
            "rejected_frames": dict(self.rejected),
        }
