"""Pure, dependency-free arbitration for the received-item counter.

Split into its own module so it can be unit-tested (test_grant_counter.py)
WITHOUT importing ApClient.py and the whole Archipelago framework. This is the
heart of the item-delivery hardening (2026-07-10): a save-resident counter can
spuriously under-read on a busy/transitional frame, and a naive "grant
items[counter:]" loop then re-delivers the whole list into a live inventory
(the field-duplicate bug). The rule below makes a counter DECREASE trustworthy
only when a real save reload corroborates it.
"""

# Action verbs returned by grant_decision (kept as plain strings so the test and
# ApClient agree without importing an enum).
AHEAD = "ahead"       # counter > total: bad/early, do not grant
REPAIR = "repair"     # spurious decrease: repair counter to high-water, do not grant
DELIVER = "deliver"   # deliver items[c:total] (forward, or reload-corroborated rollback)


def grant_decision(c, total, hw, reload_seen):
    """Decide what the grant loop should do this tick.

    Args:
      c:           the stable received-counter read (positions already granted).
      total:       len(items_received) -- the server-confirmed item count.
      hw:          session high-water: the highest counter value delivered so far.
      reload_seen: True iff a save (re)load / save-block relocation was observed
                   since the last grant (set by the save-delta loop on a
                   title-screen dip or a base move).

    Returns (action, arg):
      (AHEAD,   None)  c > total. The counter claims more granted than the server
                       has confirmed -- resync not landed yet, or a garbage slot.
                       Never grant on this.
      (REPAIR,  hw)    c < hw AND not reload_seen. The counter dropped below what
                       we've already delivered with NO reload to explain it: a
                       spurious under-read. Repair the counter back up to hw and do
                       NOT re-grant (this is what stops the duplication).
      (DELIVER, c)     everything else: deliver items[c:total]. Covers normal
                       forward progress (c >= hw) and a reload-corroborated rollback
                       (c < hw but reload_seen -> the save genuinely rolled back, so
                       re-granting the lost tail restores it and never duplicates,
                       because those items were lost with the rolled-back inventory).

    The asymmetry is deliberate: an unexpected INCREASE is rejected (AHEAD); a
    DECREASE is trusted only when a reload corroborates it, because an unexplained
    decrease is precisely what re-runs already-delivered grants.
    """
    if c > total:
        return (AHEAD, None)
    if c < hw and not reload_seen:
        return (REPAIR, hw)
    return (DELIVER, c)


def repair_streak_step(c, streak_c, streak, threshold):
    """Advance the in-place-reload corroboration streak on a REPAIR tick.

    A REPAIR verdict (counter below the high-water, no save-block move seen) is
    ambiguous: a one-frame spurious under-read looks identical to a game-over
    "Continue" that reloaded the last save at the SAME block base (so the save-
    delta poll never sees the block move and reload_seen stays False). They are
    told apart by PERSISTENCE -- a real in-place reload HOLDS the low counter
    value, whereas a transient glitch clears on the next tick.

    Args:
      c:         this tick's stable low counter read.
      streak_c:  the c value the current streak is tracking (None if no streak).
      streak:    how many consecutive prior ticks held streak_c.
      threshold: ticks the same low c must hold to call it a real reload.

    Returns (new_streak_c, new_streak, corroborated):
      corroborated is True once the SAME low c has held for `threshold` ticks --
      the caller re-grants items[c:total] (the tail lost with the rolled-back
      save). Until then it is False and the caller HOLDS: it neither re-grants
      nor repair-writes the counter, so the streak can build if the low read is
      real or a transient can self-heal (the game's counter reads correct again,
      landing on DELIVER with nothing new). Writing the counter back here would
      erase the very low read the streak counts.

    A jitter to a DIFFERENT low value restarts the streak at 1 rather than
    corroborating, so only a value that stays put is ever trusted.
    """
    if c == streak_c:
        streak += 1
    else:
        streak_c, streak = c, 1
    return (streak_c, streak, streak >= threshold)
