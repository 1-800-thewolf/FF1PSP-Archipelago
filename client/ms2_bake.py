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

ONE PACK PER FORMATION (player report 2026-10-11: Atomos + Hundlegs at light
drew Atomos as a purple Hundlegs, with Atomos' own big shadow still under it).
The per-kind sprite loader 0x08876F60 keeps ONE pack handle for the whole
battle (battle+0x80): before each kind it RELEASES that handle, then loads the
kind's pack -- boss_<mon-0x80>.pck for 0x80-0x90, ms2_<fid>.pck otherwise --
and keeps pointers INTO the pack buffer (0x08913B04 copies nothing). Release
of a bank's last resident file resets its arena cursor (0x08913C24), so the
next load lands at the SAME base. Vanilla is safe only because every kind of a
formation reloads the same bytes. A DLC boss (BOSS_0C) followed by a normal add
(MS2_10C) put Hundlegs' GIM exactly where Atomos' pointer was (both packs have
2 entries -> first body at +0x60); the palette had already been taken at sprite
creation, the shadow sat past MS2's end and survived. The fix keeps the
vanilla invariant: a DLC fid with adds gets an MS2 pack that ALSO carries the
boss's (and any boss-id add's) BOSS_xx GIM pairs, and apply_boss_minions'
pack-name cave makes the boss branch open ms2_<fid>.pck for those fids
(redirect_fids() is the single source of that set for both halves).

Source GIMs come from the per-monster singles MS_<gim>.PCK (same gids as the
formation packs use).
"""
import struct
from . import wp16
from .dpk_space import DonorPool

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
    """DLC boss ids 0x80-0x90: the engine's boss branch looks up
    boss_<mon-0x80>.gim (not ms_XX.gim), in BOSS_<mon-0x80>.PCK -- or, for a
    redirect fid, in MS2_<fid>.PCK, which then carries that pair (see the
    ONE PACK PER FORMATION note)."""
    return 0x80 <= int(mon) <= 0x90


DLC_FIDS = range(0x100, 0x111)


def redirect_fids(plan):
    """DLC boss fids whose boss branch must open ms2_<fid>.pck instead of the
    boss pack: every one that spawns ANY add (normal or boss-id -- two different
    BOSS packs alias each other exactly like BOSS + MS2 do). Shared by
    iso_patcher's pack-name cave and the bake below, so the ELF never redirects
    to a pack the dpk does not carry."""
    return sorted({int(e[0]) for e in plan or ()
                   if int(e[0]) in DLC_FIDS and e[1]})


def _add_boss_gims(f, recs, entries, mon):
    """Merge BOSS_<mon-0x80>.PCK's entries (boss_XX.gim + its _s shadow) into
    entries, verbatim (names and gids are what the boss branch looks up)."""
    have = {e["name"] for e in entries}
    for se in parse_pack(_read_pack(f, recs[f"BOSS_{int(mon) - 0x80:02X}.PCK"])):
        if se["name"] not in have:
            entries.append(se)
            have.add(se["name"])


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
# Dead-space allocation lives in dpk_space.DonorPool, shared with every other
# dpk baker (one region, one owner -- by construction, not by convention).
_Allocator = DonorPool      # name kept for the allocator unit tests


# ------------------------------------------------------------------ the bake --
def bake_minion_packs(iso_path: str, plan, pool: DonorPool) -> None:
    """For each (fid, groups) in the plan, ensure MS2_<fid>.PCK contains the
    GIM pair of every add monster: rebuild + relocate the pack if it grew, or
    CREATE it via donor-record steal when the fid ships without one (DLC
    bosses 0x100-0x110). All dead space comes from `pool`, the DonorPool
    patch_iso shares across every dpk baker; anything another baker already
    claimed or allocated is simply not in it."""
    with open(iso_path, "r+b") as f:
        recs = _read_index(f)
        alloc = pool

        # PASS 1 -- build every pack up front (reads only; the compress is the
        # expensive part). Classify each into: fits in its own record (in
        # place), needs relocation space, or needs a fresh record (creation).
        # Building all packs before any write keeps `recs` valid for the source
        # GIM lookups -- a creation's index re-sort (PASS 3) would otherwise
        # invalidate it mid-loop.
        inplace, reloc, creations = [], [], []
        redirect = set(redirect_fids(plan))
        for entry in plan:
            fid, groups = entry[0], entry[1]
            tname = f"MS2_{int(fid):03X}.PCK"
            if int(fid) in redirect:
                # the ONE pack every kind of this formation loads: the boss's
                # own pair, every boss-id add's pair, every normal add's pair.
                if tname in recs:
                    raise RuntimeError(f"boss_minions: DLC fid {int(fid):#x} "
                                       f"already has {tname} on disc")
                entries = []
                _add_boss_gims(f, recs, entries, 0x80 + int(fid) - 0x100)
                for g in groups:
                    if _is_boss_id(g[0]):
                        _add_boss_gims(f, recs, entries, g[0])
                _add_gims(f, recs, entries, groups)
                newdec = build_pack(entries)
                newcmp = wp16.compress(newdec)
                assert wp16.decompress(newcmp) == newdec
                creations.append([tname, newcmp, newdec, None, tname])
                continue
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
