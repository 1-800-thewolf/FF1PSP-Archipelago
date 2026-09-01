"""Pure rule for how far apart Archipelago traps are spaced -- no AP runtime, no
debugger, no imports beyond random.

Lives outside ApClient so it can be unit-tested: importing ApClient needs the
whole Archipelago framework (see test_client_policies, which parses the source
instead). ApClient delegates to this; nothing here touches RAM.

THE RULE (user design 2026-08-30). A trap normally springs at the very next
battle -- no fuse at all. Spacing only appears once the player is sitting on a
BACKLOG, because arming is one-trap-per-battle: without spacing, ten traps
received while disconnected would mean ten consecutive trapped fights.

So after each trap fires, look at how many are still waiting:

    >= BACKLOG_MIN waiting -> wait randint(0, GAP_MAX) battles before the next
    fewer than that        -> arm immediately, next battle

BACKLOG_MIN is 2 rather than 3 on purpose, and the arithmetic is worth spelling
out because it is the one part that reads wrong. The user's rule is "3 traps in
hand starts spacing", counting the one that is firing. At the moment a trap
fires, the count in hand is `remaining + 1`, so `remaining + 1 >= 3` is exactly
`remaining >= 2`. The resulting shapes (T = trapped fight, . = clear):

    2 queued -> T T
    3 queued -> T . T T
    4 queued -> T . T . T T
   10 queued -> a gap after every fire until the last two

A gap of 0 is a legitimate draw and is deliberate: mid-backlog traps may still
land back to back about a third of the time, so a backlog stays unpredictable
rather than settling into a metronome.
"""
import random as _random

GAP_MAX = 2          # widest spacing, in battles, once a backlog is spacing
BACKLOG_MIN = 2      # traps STILL WAITING after a fire that trigger spacing;
                     # == "3 in hand" counting the one that just fired


def next_gap(remaining, rng=None):
    """Battles to wait before arming the next trap, given how many are still
    waiting after one just fired. 0 means "arm now, it springs next battle".

    `rng` takes a random.Random for deterministic tests; the client passes none
    and gets the module's shared stream."""
    if remaining < BACKLOG_MIN:
        return 0
    return (rng or _random).randint(0, GAP_MAX)


def simulate(count, rng):
    """The battle-by-battle pattern for `count` traps queued at once, as a list
    of bools: True = this battle springs a trap, False = a clear battle.

    Exists for the tests -- it is the only way to assert the SHAPES the rule was
    specified in ("3 queued -> T . T T") rather than just its arithmetic."""
    out = []
    waiting = count
    while waiting > 0:
        out.append(True)                     # this battle springs one
        waiting -= 1
        if waiting:
            out.extend([False] * next_gap(waiting, rng))
    return out

# NO MID-BATTLE RULE LIVES HERE. A trap that lands during a fight cannot spring
# in that same fight, and the reason is in delivery, not in any rule this module
# could express: ApClient._trap_enqueue runs inside _grant_pending's counter
# walk, behind a safe-state gate that includes not-in-battle, so the trap is not
# queued until the battle is over. Proven live 2026-08-30 -- the arrival always
# reported in_battle=False with every other condition clear. Reviving the
# feature means moving trap enqueue off the received-counter pipeline first.
