"""One allocator for ff1psp.dpk dead space, shared by every baker.

WHY ONE OBJECT. Three separate live crashes (2026-08-03 caravan vs MS2,
2026-08-31 job donors vs MS2, 2026-09-03 FM_DBG_ vs MS2) were the same bug:
ownership of dead space was tracked by CONVENTION -- a hand-maintained
`reserved_names` set that every baker had to remember to consult and every
relocating baker had to remember to report into. The third incident was a
one-branch omission in that convention (`FM_DBG_` skipped the reserved test)
and it corrupted JOB_NORMAL.PCK on a player's disc.

This module replaces the convention with a mechanism. patch_iso builds ONE
DonorPool from the dpk index and hands it to each baker in bake order:

  * a baker that relocates INTO a fixed record calls `claim(name)` up front
    (extern -> the EXTERN J1 pair, caravan -> FM_SHOPJ1);
  * a baker that best-fits calls `free()` / `coalesce()` / `alloc()` /
    `take_record()` (job sprites, boss minions).

Every extent leaves the pool exactly once, so two live records cannot share
bytes by construction. `iso_patcher.assert_no_dpk_collisions` still runs after
the last baker as the invariant check; with this object it can only fail if a
baker writes somewhere it never asked for.

Donor set = FM_DBG_* (debug packs, never loaded on a US boot) + FM_*J1/J2 (the
Japanese-only UI packs). Everything else is live and never offered.
"""
import bisect

COALESCE_GAP = 0x10      # 16-align padding between adjacent dpk records


def is_donor(name):
    return (name.startswith("FM_DBG_")
            or (name.startswith("FM_")
                and ("J1." in name or "J2." in name
                     or name.endswith("J1.PCK") or name.endswith("J2.PCK"))))


class DonorPool:
    def __init__(self, donors, all_offsets=()):
        """donors: iterable of dicts with name/idx/off/csz. all_offsets: every
        record's data offset (live or not) -- coalesce() refuses to absorb a gap
        a record starts inside."""
        self._free = [[d["off"], d["csz"], False] for d in donors]   # [off, size, own]
        self._byname = {d["name"]: (d["off"], d["csz"]) for d in donors}
        self._offsets = sorted(all_offsets)
        self._records = sorted(({"name": d["name"], "idx": d["idx"]} for d in donors),
                               key=lambda d: (not d["name"].startswith("FM_DBG_"), d["name"]))
        self.claimed = set()

    @classmethod
    def from_index(cls, recs):
        return cls([dict(name=n, **r) for n, r in recs.items() if is_donor(n)],
                   all_offsets=(r["off"] for r in recs.values()))

    # ----------------------------------------------------------- ownership --
    def claim(self, name):
        """Hand a whole donor record to a fixed-target baker. Raises if it is
        not a donor or has already left the pool -- that is the collision the
        old convention silently allowed."""
        if name not in self._byname:
            raise RuntimeError(f"dpk: {name!r} is not an available donor "
                               f"(claimed={sorted(self.claimed)})")
        off, size = self._byname.pop(name)
        self._free = [e for e in self._free if not (e[0] == off and e[1] == size)]
        self._records = [r for r in self._records if r["name"] != name]
        self.claimed.add(name)

    def free(self, off, size, own=False):
        """Return an extent a relocating baker vacated. own=True marks a run
        the same baker's packs may prefer to re-fill (see alloc)."""
        self._free.append([off, size, own])

    def coalesce(self):
        self._free.sort(key=lambda e: e[0])
        merged = []
        for ext in self._free:
            if merged:
                prev = merged[-1]
                gap = ext[0] - (prev[0] + prev[1])
                if gap < 0:
                    raise RuntimeError("dpk: free-extent overlap")
                i = bisect.bisect_left(self._offsets, prev[0] + prev[1])
                owned = i < len(self._offsets) and self._offsets[i] < ext[0]
                if gap <= COALESCE_GAP and not owned:
                    prev[1] = ext[0] + ext[1] - prev[0]
                    prev[2] = prev[2] and ext[2]
                    continue
            merged.append(list(ext))
        self._free = merged

    def alloc(self, need, label, prefer_own=False):
        """Best-fit `need` bytes, split the residual back. prefer_own=True
        tries runs marked own first (job packs go back where they came from,
        leaving donor space for later bakers)."""
        need16 = (need + 15) & ~15
        cands = [e for e in self._free if e[1] >= need]
        if prefer_own and any(e[2] for e in cands):
            cands = [e for e in cands if e[2]]
        if not cands:
            biggest = max((e[1] for e in self._free), default=0)
            raise RuntimeError(f"dpk: no dead space fits {label} ({need:#x} bytes); "
                               f"largest free extent {biggest:#x}, "
                               f"{sum(e[1] for e in self._free):#x} free across "
                               f"{len(self._free)} extents")
        best = min(cands, key=lambda e: e[1])
        off = best[0]
        if best[1] - need16 >= 16:
            best[0] += need16
            best[1] -= need16
        else:
            self._free.remove(best)
        return off

    # ------------------------------------------------------- index records --
    def spare_records(self):
        return len(self._records)

    def take_record(self, label):
        if not self._records:
            raise RuntimeError(f"dpk: no spare index record to mint {label}")
        return self._records.pop(0)

    def refresh_records(self, recs):
        for d in self._records:
            d["idx"] = recs[d["name"]]["idx"]

    # ------------------------------------------------------------- probes --
    def free_extents(self):
        return [(e[0], e[1]) for e in self._free]
