"""Bake merged class-sprite palettes into the dpk (JOBxx.GIM records).

WHY ON DISC (live 2026-08-30). Custom party sprites used to be a pure RAM pin:
pixels AND palette written over every resident JOBxx.GIM. That could not work,
because a resident sheet is not the only place a class's palette lives -- when a
party member is on screen the engine also holds STANDALONE 0x400 palette records
in a separate heap, built at load time from the DISC record, and several draw
paths CLUT from those. Measured: three copies of the vanilla Thief palette at
0x08da4380/0x08da4810/0x08dadd80, none of them near any GIM. With only the
sheets pinned, our custom indices were coloured by the VANILLA palette -- the
"golden"/blotchy Thief in battle, on the load screen and after a steal.

Chasing those copies in RAM is whack-a-mole (it also costs a full 32MB scan
every few seconds and still flashes the wrong palette at battle start). Baking
the palette on disc is the fix: every copy the engine makes, wherever it makes
it, is already ours.

SUPERSET INVARIANT -- this is what makes the bake safe unconditionally, with the
feature off, and for a class whose scroll the player has not found yet:
job_sprite_tool's `merge` builds each shipped palette so that every index the
VANILLA pixels use keeps its exact vanilla colour, and custom-only colours go in
slots the vanilla art never references (31/32 of 256 used, ~225 free). Vanilla
pixels through a merged palette are therefore pixel-identical to vanilla, and
the scroll gate stays exactly where it was: the client pins PIXELS only.

LAYOUT. Five dpk packs carry the 12 main sheets, so each class appears in three:
  base 0-5      JOB_NORMAL.PCK, M_JOB_NORMAL.PCK, JOB_ALL.PCK
  promoted 6-11 JOB_CHANGE.PCK, M_JOB_CHANGE.PCK, JOB_ALL.PCK
Patching a palette adds ~100 bytes of fresh literals, so a repacked pack is
~112-238 bytes BIGGER than vanilla and cannot go back in place (wp16's greedy
parse is already optimal for the format -- see wp16.compress). The packs sit in
three contiguous runs (JOB_NORMAL+JOB_CHANGE, M_JOB_NORMAL+M_JOB_CHANGE,
JOB_ALL alone), so relocation is a best-fit pack into those freed runs plus one
donor extent for the overflow. Donor space we consume MUST be reserved from
ms2_bake -- one region, one owner (see the dpk-dead-space-ownership memory).
"""
import struct

from . import wp16
from . import job_sprites as JS
from .ms2_bake import DPK_ISO_OFF, _read_index, _read_pack, _write_record

# every dpk pack that carries main JOBxx.GIM sheets
PACKS = ("JOB_NORMAL.PCK", "M_JOB_NORMAL.PCK",
         "JOB_CHANGE.PCK", "M_JOB_CHANGE.PCK", "JOB_ALL.PCK")
_PACK_REC = 36            # pack entry header stride
PAL_IN_GIM = 0x80         # palette offset inside a GIM record
COALESCE_GAP = 0x10       # 16-align padding between packs, nothing else


def _patch_pack(blob, pals):
    """Splice each class's merged palette into its GIM inside a decompressed
    pack. Returns (new blob, list of classes patched)."""
    out = bytearray(blob)
    cnt = struct.unpack_from("<I", blob, 0)[0]
    done = []
    for i in range(cnt):
        e = blob[16 + i * _PACK_REC:16 + (i + 1) * _PACK_REC]
        name = e[:22].split(b"\0")[0].decode("latin1").upper()
        off = struct.unpack_from("<I", e, 24)[0]
        for cls, pal in pals.items():
            if name == "JOB%02X.GIM" % cls:
                out[off + PAL_IN_GIM:off + PAL_IN_GIM + JS.PAL_LEN] = pal
                done.append(cls)
    return bytes(out), done


def bake_job_palettes(iso_path: str, pool) -> None:
    """Write every shipped class's merged palette into all dpk packs that carry
    its sheet. Grown packs relocate into `pool` (the DonorPool patch_iso shares
    with every dpk baker), preferring the runs their own vanilla copies vacate
    so donor space is left for later bakers."""
    pals = {}
    for cls in range(JS.CLASS_COUNT):
        art = JS.sheet(cls)
        if art:
            pals[cls] = art[0]
    if not pals:
        return
    with open(iso_path, "r+b") as f:
        recs = _read_index(f)
        # PASS 1 -- build every patched pack before writing anything, so the
        # index stays valid for reads and we know each pack's final size.
        built = {}
        for pk in PACKS:
            rec = recs.get(pk)
            if rec is None:
                continue
            patched, done = _patch_pack(_read_pack(f, rec), pals)
            if done:
                built[pk] = wp16.compress(patched)
        if not built:
            return
        # PASS 2 -- place them. A pack that still fits its own record stays put;
        # everything else is relocated out of the pool of freed pack runs plus
        # donor extents.
        moving = {pk: blob for pk, blob in built.items()
                  if len(blob) > recs[pk]["csz"]}
        for pk in moving:
            pool.free(recs[pk]["off"], recs[pk]["csz"], own=True)
        pool.coalesce()
        # biggest first: a large pack has the fewest extents that can hold it
        for pk in sorted(moving, key=lambda p: -len(built[p])):
            blob = built[pk]
            off = pool.alloc(len(blob), pk, prefer_own=True)
            f.seek(DPK_ISO_OFF + off)
            f.write(blob)
            _write_record(f, recs[pk], off, len(blob), recs[pk]["dsz"])
        for pk, blob in built.items():
            if pk in moving:
                continue
            f.seek(DPK_ISO_OFF + recs[pk]["off"])
            f.write(blob)
            _write_record(f, recs[pk], recs[pk]["off"], len(blob),
                          recs[pk]["dsz"])
        # PASS 3 -- read the packs back through the index we just wrote and
        # prove every palette landed. A silently half-baked sheet would show as
        # the vanilla-palette sprite the whole feature exists to fix.
        recs = _read_index(f)
        for pk in built:
            blob = _read_pack(f, recs[pk])
            cnt = struct.unpack_from("<I", blob, 0)[0]
            seen = 0
            for i in range(cnt):
                e = blob[16 + i * _PACK_REC:16 + (i + 1) * _PACK_REC]
                name = e[:22].split(b"\0")[0].decode("latin1").upper()
                off = struct.unpack_from("<I", e, 24)[0]
                for cls, pal in pals.items():
                    if name == "JOB%02X.GIM" % cls:
                        got = blob[off + PAL_IN_GIM:
                                   off + PAL_IN_GIM + JS.PAL_LEN]
                        if got != pal:
                            raise RuntimeError(
                                "%s: JOB%02X.GIM palette did not bake"
                                % (pk, cls))
                        seen += 1
            if not seen:
                raise RuntimeError("%s: no job palette baked" % pk)
