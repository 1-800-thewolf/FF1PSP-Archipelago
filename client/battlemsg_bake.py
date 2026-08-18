"""On-disc rewrite of the level-up "MP increased by {N}." battle message
(slot_magic wording), inside FONT_BATTLEUS.PC in ff1psp.dpk.

WHY ON DISC (live 2026-07-31): the client's _slotbox_loop wrote the same text
into the RESIDENT bank every 4 s, and the scan proves the write lands -- yet the
level-up box still read "MP increased by 1." for a Red Mage AND a Black Mage in
the same session. The client log shows the loop RE-writing the line right after
each of those level-up battles, i.e. the resident copy reverts (the level-up
sequence re-loads the bank from its source) and the poll only repairs it after
the box has already drawn. A RAM poll can never win that race; the source copy
has to say the right thing, so it is baked here.

BATTLE_MSG.MSG is not a standalone dpk record: it is a TEXT bank inside the
wp16-compressed FONT_BATTLEUS.PC (found by signature -- 16 entries, total size
0x164 -- not by a hard-coded offset). Entry 8's span is 19 bytes and the
replacement encodes to exactly 19, so this is a pure in-place body rewrite: the offset
table is untouched and no other entry moves (entry 9 is another live level-up
line -- growing entry 8 would destroy it).

Same in-place strategy as map_bake/extern_bake: decompress, edit, recompress,
write back zero-padded, so neither the dpk nor the ISO changes size.

RELOCATION (measured): the re-worded blob RECOMPRESSES 12 bytes LARGER than the
vanilla slot (0x2664 vs 0x2658) -- vanilla's "MP increased by {N}." was one cheap
back-ref away from entry 7's "HP increased by {N}.", and the new wording gives
that up. So when the rewrite does not fit, the compressed pack is written into
FONT_BATTLEJ1.PC's extent (0x568c, the Japanese battle font -- never loaded in a
US boot) and the FONT_BATTLEUS record is repointed there, extern_bake's US->J
trick. OWNERSHIP (see [[dpk-dead-space-ownership]]): ms2_bake's donor pool is
FM_DBG_* plus FM_*J1/J2 and extern_bake reserves FM_EXTERN*J1, so FONT_BATTLEJ1
belongs to nobody else. The dpk never grows, so no LBA-shift repack.

The client keeps its resident-bank loop as a belt-and-braces repair for saves
made before this bake existed; with the bake in place it simply finds the text
already correct and never writes.
"""
import struct

from . import wp16
from . import battle_font as BF
from .extern_bake import _find_dpk, _dpk_records

_PCK = "FONT_BATTLEUS.PC"
_DONOR = "FONT_BATTLEJ1.PC"        # Japanese battle font: never loaded in a US
                                   # boot, and outside every other bake's donor
                                   # pool (ms2_bake takes FM_DBG_*/FM_*J1/J2)
_BANK_ENTRIES = 16                 # BATTLE_MSG.MSG entry count
_BANK_TOTAL = 0x164                # and its total size -- together a signature
_MP_ENTRY = 8                      # "MP increased by {N}."
_MP_SPAN = 19                      # off9 - off8; HARD CAP for the replacement

# Wording mirrors ApClient._SLOTBOX_TEXT (number LAST -- see the note there:
# it sidesteps singular/plural agreement, since {N} may sit anywhere).
SLOT_LINE = "New spell slots {N}!"     # 19 bytes = the span EXACTLY (user 2026-07-31)


def slot_line_body(text=SLOT_LINE):
    pre, _, post = text.partition("{N}")
    return (BF.encode(pre, term=False) + bytes([BF.NUM])
            + BF.encode(post, term=True))


def _find_bank(blob):
    """Offset of the BATTLE_MSG TEXT bank inside the decompressed FONT blob."""
    i = 0
    while (t := blob.find(b"TEXT", i)) >= 0:
        i = t + 4
        if t < 4 or blob[t - 4:t] != b"\0\0\0\0":
            continue
        base = t - 4
        cnt = struct.unpack_from("<I", blob, base + 8)[0] >> 8
        tot = struct.unpack_from("<I", blob, base + 0xC)[0]
        if cnt == _BANK_ENTRIES and tot == _BANK_TOTAL:
            return base
    raise KeyError("BATTLE_MSG bank not found in " + _PCK)


def build_slot_font(blob, text=SLOT_LINE):
    """Return the FONT_BATTLEUS blob with entry 8 re-worded. Idempotent."""
    blob = bytearray(blob)
    base = _find_bank(blob)
    off8, off9 = struct.unpack_from("<II", blob, base + 0x10 + _MP_ENTRY * 4)
    if off9 - off8 != _MP_SPAN:
        raise ValueError(f"{_PCK}: entry {_MP_ENTRY} span {off9 - off8}, "
                         f"expected {_MP_SPAN}")
    body = slot_line_body(text)
    if len(body) > _MP_SPAN:
        raise ValueError(f"slot line encodes to {len(body)} > {_MP_SPAN} bytes")
    at = base + off8
    if bytes(blob[at:at + len(body)]) == body:
        return bytes(blob)                              # already ours
    # pad with the terminator so no stale vanilla glyph survives past the end
    blob[at:at + _MP_SPAN] = body + bytes([BF.TERM]) * (_MP_SPAN - len(body))
    return bytes(blob)


def bake_slot_line(iso_path, text=SLOT_LINE, log=print):
    """Bake the slot_magic level-up wording into `iso_path` (in place, same dpk
    record slot)."""
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        recs = _dpk_records(dpk)
        rec_off, data_off, data_size = recs[_PCK]
        blob = wp16.decompress(bytes(dpk[data_off:data_off + data_size]))
        new_blob = build_slot_font(blob, text)
        comp = wp16.compress(new_blob)
        if wp16.decompress(comp) != new_blob:
            raise ValueError(f"{_PCK}: wp16 round-trip mismatch")
        where = "in place"
        if len(comp) > data_size:
            # does not fit its own slot -> relocate into the J font's extent.
            # Idempotent: a re-bake of an already-relocated ISO sees data_off
            # ALREADY inside the donor and simply rewrites it there.
            _drec, donor_off, donor_size = recs[_DONOR]
            if data_off == donor_off:
                donor_size = max(donor_size, data_size)
            if len(comp) > donor_size:
                raise ValueError(f"{_PCK} reworded {len(comp)} > donor "
                                 f"{_DONOR} {donor_size}")
            dpk[donor_off:donor_off + donor_size] = (
                comp + b"\x00" * (donor_size - len(comp)))
            struct.pack_into("<II", dpk, rec_off + 24, donor_off, len(comp))
            where = f"relocated into {_DONOR}"
            slot = donor_size
        else:
            dpk[data_off:data_off + data_size] = (
                comp + b"\x00" * (data_size - len(comp)))
            struct.pack_into("<I", dpk, rec_off + 28, len(comp))
            slot = data_size
        struct.pack_into("<I", dpk, rec_off + 32, len(new_blob))  # DECOMPRESSED size
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[battlemsg_bake] level-up line -> {text!r} {where} "
        f"(compressed {len(comp)}/{slot})")
