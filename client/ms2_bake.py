"""Rebuild per-formation battle sprite packs (MS2_<fid>.PCK) for boss_minions.

Battle init opens `ms2_%03x.pck` (fid) from ff1psp.dpk and looks up
`ms_%02x.gim` / `ms_%02x_s.gim` for every ADD monster it spawns. A missing
add GIM = null monster-kind object = "CPU Jump to 00000000" at battle swirl
(RA 0x088fba28). Boss sprites themselves load pack-free (live-proven: forced
Echidna fid 0x100, no MS2 pack, rendered clean 2026-07-14) -- so packs only
ever need the ADD GIM pairs.

GIM id mapping (ground-truthed from 123 single-species formations):
  mon < 0x80        -> gim = mon
  0x91 <= mon <= 0xCA -> gim = mon - 0x11
  0x80..0x90 (DLC bosses) have NO GIM singles -- never usable as adds.

Pack format (wp16-compressed):
  hdr 16B: u32 entry count, u32 total decompressed size (16-aligned), 8B zero
  entries 36B: name[22], u16 gid (name byte-sum), u32 offset, u32 size, u32 size2
  bodies 16-aligned, laid out monster/shadow interleaved.
ENTRIES MUST BE gid-ASCENDING: the engine binary-searches by gid; an unsorted
pack silently fails every lookup (live-proven crash 2026-07-14).

dpk index record 36B: name[16] (TRUNCATED at 16 -- long names lose their
tail), 6B misc, u16 name-hash @+22, u32 offset, u32 comp size, u32 decomp
size. hash = byte-sum of the FULL (untruncated) name & 0xFFFF -- verified
2033/2033 records. THE INDEX IS HASH-SORTED (engine binary-searches it), so
creating/renaming a record requires re-sorting the whole index in place.

Grown packs are relocated into donor space: FM_DBG_* packs (debug UI, never
loaded in normal play) first, then Japanese-only *J1/J2 UI packs (~1.3MB;
only cosmetically harmful if the player switches the game to Japanese).

DLC boss fids (0x100-0x110) ship with NO MS2_<fid>.PCK at all -- for those we
STEAL a donor's index record outright: write the new pack into the donor's
space, rewrite that record's name/hash to MS2_<fid>.PCK, and re-sort the
index. The donor pack becomes unreachable (it was never loaded anyway).

Source GIMs come from the per-monster singles MS_<gim>.PCK (same gids as the
formation packs use).
"""
import struct
from . import wp16
# extern_bake owns these J1 records (grown item/key name-bank relocation
# targets) -- single source of truth so a retarget there updates this pool.
from .extern_bake import US_TO_J as _EXTERN_RESERVED
from .extern_bake import _SHOP_J_BUNDLE as _CARAVAN_RESERVED

DPK_ISO_OFF = 0x2bb0000        # ff1psp.dpk extent start inside the ISO
_REC = 36


def name_hash(name: str) -> int:
    """dpk/pack name hash: byte-sum of the FULL name (case as written)."""
    return sum(name.encode("latin1")) & 0xFFFF


# ---------------------------------------------------------------- dpk index --
def _read_index(f):
    f.seek(DPK_ISO_OFF)
    cnt = struct.unpack("<I", f.read(4))[0]
    f.seek(DPK_ISO_OFF + 16)
    raw = f.read(cnt * _REC)
    recs = {}
    for i in range(cnt):
        r = raw[i * _REC:(i + 1) * _REC]
        name = r[:16].split(b"\0")[0].decode("latin1").upper()
        off, csz, dsz = struct.unpack_from("<III", r, 24)
        recs[name] = dict(idx=i, off=off, csz=csz, dsz=dsz)
    return recs


def _read_pack(f, rec):
    f.seek(DPK_ISO_OFF + rec["off"])
    return wp16.decompress(f.read(rec["csz"]))


def _write_record(f, rec, off, csz, dsz):
    f.seek(DPK_ISO_OFF + 16 + rec["idx"] * _REC + 24)
    f.write(struct.pack("<III", off, csz, dsz))


# ------------------------------------------------------------- pack (re)build --
def parse_pack(blob):
    cnt = struct.unpack_from("<I", blob, 0)[0]
    out = []
    for i in range(cnt):
        e = blob[16 + i * _REC:16 + (i + 1) * _REC]
        name = e[:22].split(b"\0")[0].decode("latin1")
        gid, off, sz, _sz2 = struct.unpack("<HIII", e[22:36])
        out.append(dict(name=name, gid=gid, data=blob[off:off + sz]))
    return out


def build_pack(entries):
    """entries: dict(name, gid, data). Byte-exact vanilla layout: entry list
    gid-ascending, bodies interleaved monster/shadow, everything 16-aligned."""
    entries = sorted(entries, key=lambda e: e["gid"])
    hdr_len = 16 + _REC * len(entries)
    # body order: each monster GIM is immediately followed by its _S.GIM
    # shadow, and every payload is 16-byte aligned within the body (the
    # (x+15)&~15 round-ups). The final `assert len(out) == total` guards
    # exactly that interleave + alignment bookkeeping.
    order = []
    for e in entries:
        if e["name"].endswith("_S.GIM"):
            continue
        order.append(e)
        sname = e["name"].replace(".GIM", "_S.GIM")
        order += [s for s in entries if s["name"] == sname]
    assert len(order) == len(entries), "unpaired GIM entries"
    pos = (hdr_len + 15) & ~15
    body = bytearray()
    offs = {}
    for e in order:
        body += b"\0" * (pos - (hdr_len + len(body)))
        offs[e["name"]] = pos
        body += e["data"]
        pos = (hdr_len + len(body) + 15) & ~15
    total = (hdr_len + len(body) + 15) & ~15
    body += b"\0" * (total - hdr_len - len(body))
    ents = bytearray()
    for e in entries:
        ents += e["name"].encode("latin1").ljust(22, b"\0")
        ents += struct.pack("<HIII", e["gid"], offs[e["name"]],
                            len(e["data"]), len(e["data"]))
    out = struct.pack("<II", len(entries), total) + b"\0" * 8 + bytes(ents) + bytes(body)
    assert len(out) == total
    return out


def _gim_id(mon: int) -> int:
    mon = int(mon)
    if mon < 0x80:
        return mon
    if 0x91 <= mon <= 0xCA:
        return mon - 0x11
    raise ValueError(f"monster {mon:#04x} has no GIM single (DLC boss ids "
                     f"0x80-0x90 load from BOSS_<mon-0x80>.PCK instead)")


def _is_boss_id(mon: int) -> bool:
    """DLC boss ids 0x80-0x90: the engine loads their sprites per-MONSTER
    from BOSS_<mon-0x80>.PCK in any slot (live-proven: Echidna +
    Two-Headed Dragon add rendered with no MS2 support) -- they never need
    GIM injection."""
    return 0x80 <= int(mon) <= 0x90


def _steal_record(f, donor, new_name, off, csz, dsz):
    """Rename the donor's index record to new_name (repointing it at `off` with
    the new sizes), then RE-SORT the whole index by hash -- the engine
    binary-searches it, so an out-of-place hash kills every lookup past it.
    Whole-record permutation: nothing references index position.

    `off` is the allocator-assigned data offset, NOT donor["off"]: the
    allocator decouples the byte-extent a pack is written into from the index
    record it borrows to name it (see _Allocator), so the stolen record may
    point at a different donor's space -- or a residual split of one."""
    f.seek(DPK_ISO_OFF)
    cnt = struct.unpack("<I", f.read(4))[0]
    f.seek(DPK_ISO_OFF + 16)
    raw = bytearray(f.read(cnt * _REC))
    i = donor["idx"]
    nm = new_name.encode("latin1")
    raw[i * _REC:i * _REC + 16] = nm[:16].ljust(16, b"\0")
    struct.pack_into("<H", raw, i * _REC + 22, name_hash(new_name))
    struct.pack_into("<III", raw, i * _REC + 24, off, csz, dsz)
    recs = sorted((bytes(raw[j * _REC:(j + 1) * _REC]) for j in range(cnt)),
                  key=lambda r: struct.unpack_from("<H", r, 22)[0])
    f.seek(DPK_ISO_OFF + 16)
    f.write(b"".join(recs))


def _add_gims(f, recs, entries, groups):
    """Merge every add monster's GIM pair (from its MS_<gim>.PCK single) into
    entries; returns True if anything was added."""
    have = {e["name"] for e in entries}
    changed = False
    for g in groups:
        if _is_boss_id(g[0]):
            continue
        gim = _gim_id(int(g[0]))
        base = f"MS_{gim:02X}.GIM"
        if base in have:
            continue
        srec = recs[f"MS_{gim:02X}.PCK"]
        for se in parse_pack(_read_pack(f, srec)):
            if se["name"] not in have:
                entries.append(se)
                have.add(se["name"])
        changed = True
    return changed


# --------------------------------------------------------------- allocator ---
class _Allocator:
    """dpk dead-space allocator for relocated / minted MS2 packs.

    The old scheme popped a WHOLE donor record per pack: a 0x2000 pack that
    landed in the 0x11508 donor wasted the other 0xf508, and a plan minting two
    packs each larger than the second-biggest donor (only ONE donor > 0xc63c
    exists on the US disc) raised `no donor pack fits`. Instead we manage two
    resources independently:

      * FREE EXTENTS -- contiguous byte ranges we may write pack bodies into.
        alloc() best-fits, then SPLITS the residual back onto the free list, so
        one big donor can serve several packs (two 0x85c0 packs both fit inside
        the 0x11508 EXTERN18J2 extent, freeing every other donor).
      * INDEX RECORDS -- spare dpk directory slots a CREATION renames to
        MS2_<fid>.PCK (relocations reuse the target's own existing record and
        need no slot). Decoupled from extents: a stolen record is repointed at
        whatever offset alloc() handed out, so relocating packs no longer
        starve creations of records.

    A third space source: a RELOCATED pack's own old extent. Every source pack
    is read in the build pass before any allocation, so once a pack is known to
    relocate its vanilla bytes are dead -- free() returns them to the pool.
    The vanilla MS2 fiend packs sit back-to-back in the dpk (gaps <= 0xc pure
    16-align padding), so at Absurd -- where every fiend pack relocates -- the
    freed run coalesces into ~0x36000 contiguous bytes, which is what lets the
    grown ~0xccb8 fiend packs land even though no single donor that large
    exists (the 2026-07-23 'no donor pack fits' live failure).

    Safety rests on the same invariant the whole-donor scheme relied on -- a
    donor's ORIGINAL record (until stolen) is never loaded in a US boot, so it
    may harmlessly alias space we reused. Every WRITE goes to a range alloc()
    removed from the free list, and each range is removed exactly once, so live
    packs never overlap. coalesce() merges extents only across gaps proven to
    be padding: <= 0x10 bytes AND no live record starts inside the gap."""

    def __init__(self, donors, all_offsets=()):
        # free byte-extents, one per donor space (mutable [off, size] pairs).
        self._free = [[d["off"], d["csz"]] for d in donors]
        # every record's data offset -- gap-absorption safety check.
        self._offsets = sorted(all_offsets)
        # stealable index records, cheapest-to-lose first (debug packs, then
        # smaller Japanese UI packs) so a creation burns the least-missed slot.
        self._records = sorted(
            ({"name": d["name"], "idx": d["idx"]} for d in donors),
            key=lambda d: (not d["name"].startswith("FM_DBG_"), d["name"]))

    def free(self, off, size):
        """Return a relocated pack's old extent to the pool. Only call once
        every read of the vanilla dpk bytes is done (build pass complete)."""
        self._free.append([off, size])

    def coalesce(self):
        """Merge adjacent free extents. A gap between two free extents is
        absorbed only when it is provably 16-align padding: <= 0x10 bytes and
        no live record's data offset falls inside it (a tiny record COULD sit
        in a sub-0x10 hole; bisect check rules that out)."""
        import bisect
        self._free.sort()
        merged = [self._free[0][:]] if self._free else []
        for off, size in self._free[1:]:
            tail = merged[-1]
            gap = off - (tail[0] + tail[1])
            if gap < 0:               # overlap cannot happen; defensive
                raise RuntimeError("boss_minions: free-extent overlap")
            i = bisect.bisect_left(self._offsets, tail[0] + tail[1])
            gap_owned = (i < len(self._offsets) and self._offsets[i] < off)
            if gap <= 0x10 and not gap_owned:
                tail[1] = off + size - tail[0]      # absorb gap + extent
            else:
                merged.append([off, size])
        self._free = merged

    def alloc(self, need, label):
        """Reserve `need` bytes; return the data offset. Best-fit + split."""
        need16 = (need + 15) & ~15
        best = None
        for e in self._free:
            if e[1] >= need and (best is None or e[1] < best[1]):
                best = e
        if best is None:
            biggest = max((e[1] for e in self._free), default=0)
            raise RuntimeError(
                f"boss_minions: no donor space fits {label} ({need:#x} bytes); "
                f"largest free extent {biggest:#x}, "
                f"{sum(e[1] for e in self._free):#x} free across "
                f"{len(self._free)} extents")
        off = best[0]
        if best[1] - need16 >= 16:
            best[0] += need16          # shrink from the front, keep residual
            best[1] -= need16
        else:
            self._free.remove(best)    # tail too small to bother tracking
        return off

    def spare_records(self):
        return len(self._records)

    def take_record(self, label):
        if not self._records:
            raise RuntimeError(
                f"boss_minions: no spare dpk index record to mint {label} "
                f"(need one donor slot per newly-created MS2 pack)")
        return self._records.pop(0)

    def refresh_records(self, recs):
        """A creation's index re-sort moved every record; re-read idx by name
        for the records still available to steal."""
        for d in self._records:
            d["idx"] = recs[d["name"]]["idx"]


def _donor_pool(recs, reserved):
    """Records whose space is safe to reuse: debug packs (never loaded) + the
    Japanese-only UI packs, minus any records `reserved` for extern_bake."""
    return [dict(name=n, **r) for n, r in recs.items()
            if n.startswith("FM_DBG_") or
            (n.startswith("FM_") and n not in reserved and
             ("J1." in n or "J2." in n or
              n.endswith("J1.PCK") or n.endswith("J2.PCK")))]


def reserved_names(extern_active: bool, caravan_active: bool) -> set:
    """dpk records another baker owns this run, so ms2 must not reuse their
    space OR their index slot. One region, one owner.

      * extern_bake relocates the grown item/spell name+desc banks into the
        EXTERN J1 pair (18J1 alone is the biggest donor on the disc).
      * bake_caravan_offer relocates the grown FM_SHOPUS bundle into
        FM_SHOPJ1 -- also a J1-named donor, and it runs AFTER ms2_bake in
        patch_iso, so an MS2 pack parked there gets overwritten silently.

    Both bakers only relocate when their content actually grew, but reserving
    unconditionally while the feature is on is the cheap, safe direction."""
    out = set()
    if extern_active:
        out |= {us_j[1][:16].split("\0")[0] for us_j in _EXTERN_RESERVED}
    if caravan_active:
        out.add(_CARAVAN_RESERVED[:16].split("\0")[0])
    return out


# ------------------------------------------------------------------ the bake --
def bake_minion_packs(iso_path: str, plan, extern_active: bool = True,
                      caravan_active: bool = False) -> None:
    """For each (fid, groups) in the plan, ensure MS2_<fid>.PCK contains the
    GIM pair of every add monster: rebuild + relocate the pack if it grew, or
    CREATE it via donor-record steal when the fid ships without one (DLC
    bosses 0x100-0x110).

    extern_active: when spell_tomes / key_names / blood_magic are all off,
    extern_bake never runs, so its two reserved records (FM_EXTERN12J1/18J1 --
    the latter is the single biggest donor on the disc, 0x142a4) are free
    dead space; pass False to fold them into the pool. When any of those
    features IS on, extern_bake relocates its grown name banks into exactly
    those records: stealing them KeyError'd extern_bake and OOB-froze the item
    menu (live 2026-07-15). One region, one owner.

    caravan_active: extern_bake.bake_caravan_offer may relocate the grown
    FM_SHOPUS shop-text bundle into FM_SHOPJ1.PCK -- also a J1 donor by name --
    so reserve it whenever a caravan row is being authored. Same one-region-one-
    owner rule as the EXTERN J1 pair."""
    with open(iso_path, "r+b") as f:
        recs = _read_index(f)
        # Donor space (see _donor_pool): debug packs + Japanese-only UI packs.
        # The J2 EXTERN variants always stay in the pool (extern_bake never
        # touches them, and 18J2 0x11508 is the biggest donor big minted packs
        # need); the J1 pair is reserved only while extern_bake will use it.
        # HISTORY (why caravan_active must reach the pool -- it once did not):
        # before this line passed it through, a grown MS2 pack landed in
        # FM_SHOPJ1's extent and bake_caravan_offer (which runs AFTER ms2_bake
        # in patch_iso) overwrote its head with the shop bundle. Live
        # 2026-08-03: MS2_074.PCK (Marilith 2.0) decompressed to
        # SHOP_MSG/FM_SHOPUS entries, so the add GIM lookup found nothing ->
        # null monster-kind object -> "CPU Jump to 00000000", RA 0x088fba28,
        # on entering the fight.
        _reserved = reserved_names(extern_active, caravan_active)
        alloc = _Allocator(_donor_pool(recs, _reserved),
                           all_offsets=(r["off"] for r in recs.values()))

        # PASS 1 -- build every pack up front (reads only; the compress is the
        # expensive part). Classify each into: fits in its own record (in
        # place), needs relocation space, or needs a fresh record (creation).
        # Building all packs before any write keeps `recs` valid for the source
        # GIM lookups -- a creation's index re-sort (PASS 3) would otherwise
        # invalidate it mid-loop.
        inplace, reloc, creations = [], [], []
        for entry in plan:
            fid, groups = entry[0], entry[1]
            tname = f"MS2_{int(fid):03X}.PCK"
            if tname not in recs:
                if not groups:
                    continue
                entries = []
                if not _add_gims(f, recs, entries, groups):
                    continue      # all adds self-loading (boss ids) -> no pack
                newdec = build_pack(entries)
                newcmp = wp16.compress(newdec)
                assert wp16.decompress(newcmp) == newdec
                creations.append([tname, newcmp, newdec, None, tname])
                continue
            trec = recs[tname]
            entries = parse_pack(_read_pack(f, trec))
            if not _add_gims(f, recs, entries, groups):
                continue
            newdec = build_pack(entries)
            newcmp = wp16.compress(newdec)
            assert wp16.decompress(newcmp) == newdec
            if len(newcmp) <= trec["csz"]:
                inplace.append((trec, newcmp, newdec))
            else:
                reloc.append([trec, newcmp, newdec, None, tname])

        # Fail fast on record shortage before writing anything (a partial dpk
        # is worse than an aborted bake -- the caller boots unpatched).
        if len(creations) > alloc.spare_records():
            raise RuntimeError(
                f"boss_minions: {len(creations)} new MS2 packs need index "
                f"records but only {alloc.spare_records()} donor slots free")

        # PASS 2 -- assign space. First return every relocating pack's OWN old
        # extent to the pool (all vanilla-byte reads finished in PASS 1) and
        # coalesce: the fiend MS2 packs are back-to-back in the dpk, so at
        # Absurd their freed run merges into one big extent -- the only space
        # big enough for the grown ~0xccb8 fiend packs when extern_bake holds
        # the J1 records. Then allocate largest-first so big packs claim big
        # extents while small ones mop up the residual splits.
        for trec, _c, _d, _o, _lbl in reloc:
            alloc.free(trec["off"], trec["csz"])
        alloc.coalesce()
        need = reloc + creations
        for req in sorted(need, key=lambda r: len(r[1]), reverse=True):
            req[3] = alloc.alloc(len(req[1]), req[4])   # req[4] = MS2_<fid> name

        # PASS 3 -- write. In place first, then relocations (which reuse stable
        # existing record idx), then creations LAST: each steals a record and
        # re-sorts the whole index, shifting positions -- so no index edit may
        # interleave with the relocation writes above.
        for trec, newcmp, newdec in inplace:
            f.seek(DPK_ISO_OFF + trec["off"])
            f.write(newcmp)
            _write_record(f, trec, trec["off"], len(newcmp), len(newdec))
        for trec, newcmp, newdec, off, _lbl in reloc:
            f.seek(DPK_ISO_OFF + off)
            f.write(newcmp)
            _write_record(f, trec, off, len(newcmp), len(newdec))
        for tname, newcmp, newdec, off, _lbl in creations:
            donor = alloc.take_record(tname)
            f.seek(DPK_ISO_OFF + off)
            f.write(newcmp)
            _steal_record(f, donor, tname, off, len(newcmp), len(newdec))
            # the steal re-sorted the index -> refresh idx of the records still
            # available to steal (their space/offsets are untouched).
            recs = _read_index(f)
            alloc.refresh_records(recs)
