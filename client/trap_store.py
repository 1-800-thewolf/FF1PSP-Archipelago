"""Pure rule for trap DELIVERY identity -- no AP runtime, no debugger.

A received trap reaches the queue by TWO independent roads, and before this
module they could not see each other (live 2026-08-30: 5 traps sent, 9 queued):

  1. the received-counter walk in ApClient._grant_pending. The counter lives in
     the SAVE BLOCK, so loading an in-game save rolls it backwards and the walk
     re-delivers every trap received since that save was written. That is the
     designed behaviour for a rollback -- an EXP bag is re-granted the same way,
     because the save took the effect back with it;
  2. the DataStorage restore (_trap_apply_stored). It exists because the counter
     normally runs AHEAD of the queue: a trap that has been received but has not
     fired yet is recorded NOWHERE else, so a client restart used to eat it.

Each road is right on its own. Together, a counter rollback makes road 1
re-deliver traps road 2 has already put back, and the player pays twice.

The fix is an IDENTITY: every queued trap carries the received-items index it
came from, so the two roads can recognise the same trap.

  * the counter walk skips an index that is STILL PENDING (queued or armed) --
    it is already going to fire, delivering it again would only duplicate it;
  * the restore skips an index this session has ALREADY ENQUEUED, pending or
    long since fired -- the walk got there first and the store is just a slower
    echo of the same receipt.

The asymmetry is deliberate. Pending-only on the walk PRESERVES the rollback
re-fire: a trap that fired and was then undone by an in-place reload is not
pending any more, so it is delivered again exactly as before. Seen-set on the
restore closes the connect race, where the Get reply can land after the walk has
already delivered -- and even fired -- the same index.

Neither road ever drops an entry that is not already represented, so a trap can
still never silently vanish; that failure is what the store was added to prevent
and it stays prevented.

An entry saved by an older client has NO index. It cannot be matched, so it is
restored unconditionally, exactly as it was before -- a one-time transitional
double-delivery is strictly better than a one-time silent loss.
"""


def entry(kind, idx=None):
    """One queue entry. `idx` is the position in items_received it was
    delivered from, or None for a legacy entry read back from an older store."""
    e = {"kind": int(kind)}
    if idx is not None:
        e["idx"] = int(idx)
    return e


def pending_indices(queue, armed=None):
    """Received-items indices that are still waiting to fire (queue + the one
    whose mailbox arming is live). Legacy entries contribute nothing."""
    out = set()
    for e in list(queue) + ([armed] if armed else []):
        try:
            i = e.get("idx")
        except AttributeError:
            continue
        if i is not None:
            out.add(int(i))
    return out


def is_pending(idx, queue, armed=None):
    """True when the counter walk should NOT re-deliver `idx`: something in the
    queue already is that trap."""
    if idx is None:
        return False
    return int(idx) in pending_indices(queue, armed)


def restorable(value, seen, live_kinds):
    """The entries of a stored queue worth merging in, as fresh dicts.

    `seen` is every index this session has already enqueued (pending or fired);
    those are dropped. Kinds outside `live_kinds` are dropped too -- a retired
    trap must not sit in the queue forever. Anything malformed is skipped rather
    than raising: the value came off the network."""
    out = []
    if not isinstance(value, list):
        return out
    for t in value:
        if not isinstance(t, dict):
            continue
        try:
            k = int(t["kind"])
        except Exception:
            continue
        if k not in live_kinds:
            continue
        try:
            i = t.get("idx")
            i = None if i is None else int(i)
        except Exception:
            i = None
        if i is not None and i in seen:
            continue
        out.append(entry(k, i))
    return out
