"""Level catch-up EXP -- the pure math behind ApClient._catchup_loop. No I/O,
no asyncio, no AP runtime: the loop hands it one frame (levels, per-row EXP
gained by the battle that just ended, held rows) and it says who gets a bonus.

WHY: `starting_party_members` rolls a join level of 10..30 at GENERATION, when
nobody knows how far along the party will be when that member is actually
found. A 4th member arriving at Lv13 next to 65/65/50 is dead weight (live
report 2026-09-08). Rather than rewrite the join level -- which would have to
be recomputed live and would then fight the save-resident join latch -- the
member joins at their rolled level and simply EARNS FASTER until they have
closed the gap:

    level <  50% of the rest of the party's average   ->  3x EXP
    level <  70% of the rest of the party's average   ->  2x EXP
    otherwise                                         ->  normal

Always on, no yaml option (user call 2026-09-08). It is not specific to
missing members either: any member who falls behind (deaths spent outside
battle, a late church trip) catches up the same way, which is why the input is
just levels + deltas and nothing about the party feature.

AVERAGE EXCLUDES THE MEMBER BEING JUDGED. "Behind the party" means behind the
OTHERS; including the low member in their own average drags the thresholds
down exactly when the gap is worst (65/65/50/13 -> mean 48 -> 3x would stop at
Lv24 instead of Lv30).

HELD rows (a missing member not yet found -- pseudo-dead, HP 0 with the KO bit
clear) are neither judged nor counted in anyone's average: their record still
reads Lv1 and would halve every threshold.

The bonus is EXTRA exp on top of what the engine already paid, so the caller
adds `bonus`, it does not overwrite. The engine only recomputes levels at a
victory, so a bonus written after this battle materializes as levels at the
NEXT one -- a one-fight lag, not a lost level.
"""

# (fraction of the others' average, multiplier) -- first match wins, so keep
# this ordered by ascending fraction.
CATCHUP_TIERS = ((0.50, 3), (0.70, 2))


def catchup_multiplier(level, others_avg):
    """EXP multiplier for a member at `level` whose party-mates average
    `others_avg`. 1 = no bonus (caught up, alone, or unreadable records)."""
    if level <= 0 or others_avg <= 0:
        return 1
    for frac, mult in CATCHUP_TIERS:
        if level < frac * others_avg:
            return mult
    return 1


def catchup_target(others_avg):
    """First level at which no bonus applies any more, for log lines."""
    if others_avg <= 0:
        return 0
    # The highest tier fraction is the one that stops paying last.
    top = max(frac for frac, _m in CATCHUP_TIERS)
    lvl = int(top * others_avg)
    # `level < frac*avg` pays, so the first non-paying level is the ceiling.
    return lvl + 1 if lvl < top * others_avg else lvl


def plan_awards(levels, deltas, held=()):
    """One frame -> {row: (bonus_exp, multiplier, others_avg)}.

    levels  -- per-row level (0 = record not real yet; such rows are ignored
               everywhere, both as candidates and as average material)
    deltas  -- per-row EXP the battle just paid. 0 = the row earned nothing
               (dead, or held), so it gets no bonus: a multiplier of nothing
               is nothing.
    held    -- rows the member feature owns (missing, not yet joined).
    """
    held = set(held)
    present = [r for r, lv in enumerate(levels) if lv > 0 and r not in held]
    out = {}
    for row in present:
        gained = deltas[row] if row < len(deltas) else 0
        if gained <= 0:
            continue
        others = [levels[r] for r in present if r != row]
        if not others:
            continue                       # solo party: nothing to be behind
        avg = sum(others) / len(others)
        mult = catchup_multiplier(levels[row], avg)
        if mult > 1:
            out[row] = (gained * (mult - 1), mult, avg)
    return out


class CatchupTracker:
    """The loop's whole state machine, minus the I/O -- so the part that was
    never testable (which frame counts as a battle award, which one is a
    savestate rewind) can be pinned offline. The loop only reads the game and
    writes what step() returns.

    THE INVARIANT: inside one save P_EXP never goes down, and it only goes UP
    at a battle victory or at one of our own writes. So an award frame is
    exactly: a field frame, with a battle behind it, whose deltas are all >= 0
    and plausible. Everything else REBASES (adopt the new numbers, pay
    nothing) -- a Continue onto a further-along file, a savestate rewind, the
    member loop's join floor, an EXP-bag item with no battle behind it.

    Decisions from step(): ("wait"|"snapshot"|"rebase"|"idle"|"award",
    {row: Award}, [rows that just caught up]). Award = (bonus, mult, avg,
    new_exp, announce) -- announce is True only when the row's tier CHANGED,
    so the log line fires once per tier, not once per fight."""

    def __init__(self, exp_cap, sane_delta=200_000, max_level=99):
        self.exp_cap = exp_cap
        self.sane_delta = sane_delta
        self.max_level = max_level
        # row -> multiplier last announced. Deliberately NOT cleared by
        # reset(): a reconnect should not re-announce a tier the player has
        # already been told about.
        self._tier = {}
        self.reset()

    def reset(self):
        """Save block lost / not a real frame: forget the snapshot. The next
        usable frame re-snapshots and pays nothing (one battle's bonus can be
        missed across a reconnect -- cheaper than paying a bogus one)."""
        self.prev = None
        self.fought = False

    def saw_non_field(self):
        """A non-field frame (battle, or the pause menu -- the flag cannot
        tell them apart). Arms the award: only a delta with one of these
        behind it is battle EXP."""
        self.fought = True

    def step(self, levels, exps, held=()):
        if (not levels or len(levels) != len(exps)
                or any(lv < 0 or lv > self.max_level for lv in levels)
                or any(xp < 0 or xp > self.exp_cap for xp in exps)):
            self.reset()                    # records mid-load: not a frame
            return ("wait", {}, [])
        if self.prev is None:
            self.prev, self.fought = list(exps), False
            return ("snapshot", {}, [])
        deltas = [now - old for now, old in zip(exps, self.prev)]
        if any(d < 0 for d in deltas) or max(deltas) > self.sane_delta:
            self.prev, self.fought = list(exps), False
            return ("rebase", {}, [])
        if not self.fought or not any(deltas):
            # No battle behind it (an EXP bag), or a battle that paid nothing
            # (fled, wiped, everyone held): adopt and disarm.
            self.prev, self.fought = list(exps), False
            return ("idle", {}, [])
        self.fought = False
        awards, out = plan_awards(levels, deltas, held), {}
        after = list(exps)
        for row, (bonus, mult, avg) in awards.items():
            new = min(exps[row] + bonus, self.exp_cap)
            if new <= exps[row]:
                continue                    # already capped at max level
            after[row] = new
            announce = self._tier.get(row) != mult
            self._tier[row] = mult
            out[row] = (bonus, mult, avg, new, announce)
        caught = [r for r in sorted(self._tier)
                  if r not in out and deltas[r] > 0]
        for row in caught:
            del self._tier[row]
        self.prev = after
        return (("award" if out else "idle"), out, caught)
