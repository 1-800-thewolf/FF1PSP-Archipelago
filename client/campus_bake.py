"""On-disc pad the FM_CAMPUS.PCK -> JOB_NAME.MSG class-name bank so each of the
12 entries has a fixed SLOT-byte budget. The status menu caches each entry's
display length from the canonical bank's offset table AT MENU-OPEN, so lifting
the per-job length cap requires the on-disc bank to be padded (the client alone
can't -- it writes names too late to feed the open-time cache). See the
class-name-bank-re memory.

Method (minimal surgery): APPEND a padded copy of the bank to the end of the
FM_CAMPUS decompressed blob, zero the old bank region (now dead), and update ONLY
JOB_NAME.MSG's inner directory record (dec offset + size) + the container total.
No other subfile shifts. Padding bytes are 0x00 (render as trailing spaces), so
vanilla display is unchanged; the client overwrites a slot in place when a scroll
is owned. Padding compresses to ~nothing, so the recompressed PCK fits the
original dpk record slot -> no dpk/ISO LBA shift (mirrors extern_bake).
"""
import struct

from . import wp16
from .extern_bake import _find_dpk, _dpk_records
from .class_names import SLOT, CLASS_COUNT

_CAMPUS = "FM_CAMPUS.PCK"
_SUBFILE = b"JOB_NAME.MSG"
_REC_STRIDE = 0x24            # inner dir record: name[16] + fA + fB + off + sz + sz


def _find_subfile_rec(blob, name):
    """Offset of the FM_CAMPUS inner directory record for `name`."""
    cnt = struct.unpack_from("<I", blob, 0)[0]
    for k in range(cnt):
        p = 0x10 + k * _REC_STRIDE
        if blob[p:p + len(name)] == name and blob[p + len(name)] in (0, 0x2e):
            return p
    raise KeyError(f"{name!r} not in FM_CAMPUS directory")


def _pad_bank(bank):
    """Rebuild a standalone TEXT bank so every entry occupies SLOT bytes
    (glyphs + term left-justified, 0x00 padded). Offset table + total rewritten."""
    total = struct.unpack_from("<I", bank, 0xC)[0]
    first = struct.unpack_from("<I", bank, 0x10)[0]
    cnt = (first - 0x10) // 4
    offs = list(struct.unpack_from(f"<{cnt}I", bank, 0x10))
    ends = offs[1:] + [total]
    header = bytes(bank[:0x10])
    new_offs, body, p = [], bytearray(), 0x10 + cnt * 4
    for i in range(cnt):
        e = bytes(bank[offs[i]:ends[i]])[:SLOT - 1]     # keep name+term, clamp
        e = e + b"\x00" * (SLOT - len(e))               # pad to SLOT
        new_offs.append(p); body += e; p += SLOT
    table = b"".join(struct.pack("<I", o) for o in new_offs)
    out = bytearray(header) + table + bytes(body)
    struct.pack_into("<I", out, 0xC, p)                 # new total
    return bytes(out), cnt


def build_padded_campus(blob):
    """Return (new_blob, dec_size) with JOB_NAME padded via append+zero-old."""
    blob = bytearray(blob)
    rec = _find_subfile_rec(blob, _SUBFILE)
    doff = struct.unpack_from("<I", blob, rec + 0x18)[0]
    dsz = struct.unpack_from("<I", blob, rec + 0x1C)[0]
    if dsz == 0x10 + CLASS_COUNT * 4 + CLASS_COUNT * SLOT:
        return bytes(blob), len(blob)               # already padded: idempotent no-op
    padded, cnt = _pad_bank(blob[doff:doff + dsz])
    if cnt != CLASS_COUNT:
        raise ValueError(f"JOB_NAME entry count {cnt} != {CLASS_COUNT}")
    blob[doff:doff + dsz] = b"\x00" * dsz               # old bank -> dead zeros
    append_off = len(blob)
    blob += padded
    struct.pack_into("<I", blob, rec + 0x18, append_off)   # dec offset -> appended
    struct.pack_into("<I", blob, rec + 0x1C, len(padded))  # size
    struct.pack_into("<I", blob, rec + 0x20, len(padded))  # size (dup)
    struct.pack_into("<I", blob, 4, len(blob))             # container total
    return bytes(blob), len(blob)


# ---- KEY_EXP.MSG (key-item DESCRIPTION bank) authoring ----------------------
# Same container, same append+zero-old strategy. The Key Items menu's bottom
# bar reads this 36-entry TEXT bank (resident copy re-read on menu open). Its
# glyph encoding is NEITHER the menu font (name_banks) NOR the msg font
# (font_map): it is the desc font cracked 2026-07-27 by aligning the live bank
# bytes against known English (entries 16 "Small boat for crossing lakes and
# rivers." + 34 "Bridge for connecting battery and chip.", then cryptogramming
# the other 34 entries). TERM here is 0x05 (the NAME banks use 0x06).
# Capitals are scattered, not a contiguous block -- only the ones below are
# proven; text using any other capital (e.g. 'G') must be reworded.
_KEYDESC_SUBFILE = b"KEY_EXP.MSG"
_KEYDESC_COUNT = 36
_KEYDESC_TERM = 0x05
KEYDESC_NEWLINE = 0x46         # '\n'; proven by CONFIG.MSG "...that<NL>opens the menu."
# Every mapping below is PROVEN against known English in this container -- the
# 36 KEY_EXP entries, CONFIG.MSG's option help, and (decisive for capitals)
# PLACE_NAME.MSG's 86 canonical FF1 place names: "Cavern of Earth B1".."B5",
# "Flying Fortress 1F", "Mount Gulg", "Dragon Caves", "Western Keep",
# "Hellfire Chasm", "Ryukahn Desert", "Yahnikurm Desert", "Modern Maze";
# plus CAMP_CMD/SAVE_LOAD for the menu capitals: "Use" (U), "Next Level"/"No"
# (N), "Current EXP" (X), "Save data to this file?" (?).
# (An earlier pass guessed D=0x29 from a "?errick's signature" cryptogram --
# WRONG twice over: place names prove D=0x20, and N=0x29 makes that vanilla
# entry "Nerrick's signature" -- the FF1 dwarf. Never re-add a mapping that
# isn't backed by a known string.)
KEYDESC_ENC = {
    " ": 0x00, "e": 0x01, "t": 0x02, "o": 0x03, "n": 0x04, "a": 0x06,
    "r": 0x07, "i": 0x08, "s": 0x09, "h": 0x0a, "l": 0x0b, "u": 0x0c,
    "d": 0x0d, "m": 0x0e, "g": 0x0f, "c": 0x10, "f": 0x11, ".": 0x12,
    "y": 0x14, "b": 0x15, "p": 0x16, "v": 0x19, "w": 0x1a, "k": 0x1f,
    "j": 0x2c, "q": 0x3b, "x": 0x38, "z": 0x36, "'": 0x2e, "-": 0x35,
    ",": 0x3d,
    "?": 0x37,
    "A": 0x1d, "B": 0x1b, "C": 0x13, "D": 0x20, "E": 0x24, "F": 0x1e,
    "G": 0x23, "H": 0x31, "I": 0x2d, "K": 0x32, "L": 0x22, "M": 0x1c,
    "N": 0x29, "O": 0x28, "P": 0x25, "R": 0x30, "S": 0x18, "T": 0x17,
    "U": 0x2f, "W": 0x21, "X": 0x42, "Y": 0x3c,
    "1": 0x26, "2": 0x27, "3": 0x2b, "4": 0x33, "5": 0x34,
    "\n": KEYDESC_NEWLINE,
}


def keydesc_encode(text):
    """Desc-font glyphs + TERM for `text` ('\\n' = line break). Raises on any
    unmapped character -- a silent drop would ship a garbled description, so
    unknown chars (incl. the capitals still unproven: J N Q U V X Z) are a
    loud, named error at bake time rather than in-game garbage."""
    missing = sorted({c for c in text if c not in KEYDESC_ENC})
    if missing:
        raise ValueError(f"keydesc_encode: no desc-font glyph for {missing!r} "
                         f"in {text!r}")
    return bytes(KEYDESC_ENC.get(c, 0) for c in text) + bytes([_KEYDESC_TERM])


def _free_span(blob, doff, dsz):
    """(start, size) of the largest region a subfile at `doff` may occupy
    without shifting any other: its own extent grown into the UNCLAIMED bytes
    on either side. Subfiles sit at ascending offsets with a few alignment
    padding bytes between them, and nothing indexes that padding. The start is
    4-byte aligned. Note a region another record has vacated (pad_class_bank
    relocates JOB_NAME, freeing the 171B right before KEY_EXP) is unclaimed by
    construction, so this picks it up automatically."""
    cnt = struct.unpack_from("<I", blob, 0)[0]
    lo, hi = 0, len(blob)
    for k in range(cnt):
        p = 0x10 + k * _REC_STRIDE
        so = struct.unpack_from("<I", blob, p + 0x18)[0]
        sz = struct.unpack_from("<I", blob, p + 0x1C)[0]
        if so == doff:
            continue                            # ourselves
        if so + sz <= doff:
            lo = max(lo, so + sz)               # claimed region ending before us
        elif so < doff + dsz:
            return doff, dsz                    # overlaps us: don't touch anything
        else:
            hi = min(hi, so)                    # claimed region starting after us
    lo = (lo + 3) & ~3
    return lo, hi - lo


def _place(blob, doff, dsz, need):
    """Where to lay a `need`-byte rebuild of the subfile at `doff`. Prefers
    NOT moving (a shifted bank costs recompression: the wp16 stream stops
    matching the surrounding data), and only slides back into unclaimed space
    when the content genuinely does not fit forward."""
    lo, span = _free_span(blob, doff, dsz)
    fwd = lo + span - doff              # room without moving
    if need <= fwd:
        return doff, fwd
    return lo, span


def author_key_desc(iso_path, entries, log=print):
    """Replace KEY_EXP.MSG entries inside `iso_path` (edited in place, same
    dpk-record slot). `entries` = {key id (1-based) -> encoded glyph bytes incl.
    TERM} (use keydesc_encode). Offsets are rebuilt, so an entry may GROW --
    the whole bank is re-laid in place. Idempotent: a bank already carrying the
    target bytes is left untouched. Raises loudly if the recompressed PCK no
    longer fits.

    I/O is TARGETED -- only the record table, the FM_CAMPUS record slot and its
    size field are touched. Slurping and rewriting the whole dpk (as
    pad_class_bank does) costs tens of MB per call and pushed test_patch past
    its 180s cap once this step joined the bake."""
    with open(iso_path, "r+b") as f:
        dpk_off, _dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        nrec = struct.unpack("<I", f.read(4))[0]
        f.seek(dpk_off)
        table = f.read(16 + nrec * 36)         # record table only, not the data
        rec_off, data_off, data_size = _dpk_records(table)[_CAMPUS[:16].split("\0")[0]]
        f.seek(dpk_off + data_off)
        dpk = bytearray(b"\0" * data_off + f.read(data_size))   # slot-relative view
        blob = bytearray(wp16.decompress(bytes(dpk[data_off:data_off + data_size])))
        rec = _find_subfile_rec(blob, _KEYDESC_SUBFILE)
        doff = struct.unpack_from("<I", blob, rec + 0x18)[0]
        dsz = struct.unpack_from("<I", blob, rec + 0x1C)[0]
        bank = blob[doff:doff + dsz]
        total = struct.unpack_from("<I", bank, 0xC)[0]
        first = struct.unpack_from("<I", bank, 0x10)[0]
        cnt = (first - 0x10) // 4
        if cnt != _KEYDESC_COUNT:
            raise ValueError(f"KEY_EXP entry count {cnt} != {_KEYDESC_COUNT}")
        offs = list(struct.unpack_from(f"<{cnt}I", bank, 0x10))
        # An entry ends at the next HIGHER offset, not at offs[i+1]: once this
        # function has run, identical entries share one body copy, so offsets
        # are no longer monotonic and the naive pairing mis-measures every
        # aliased entry (which made a second bake of the same ISO non-idempotent).
        def _end(o):
            higher = [x for x in offs if x > o]
            return min(higher) if higher else total
        ents = [bytes(bank[o:_end(o)]) for o in offs]
        want = {int(k) - 1: v for k, v in entries.items()}
        if all(ents[i] == v for i, v in want.items()):
            log("[campus_bake] KEY_EXP already authored")
            return
        for i, v in want.items():
            ents[i] = v
        # Re-lay IN PLACE (the dpk slot has almost no compressed headroom --
        # append+zero-old grew the PCK past it, live 2026-07-27). To make room
        # for grown entries, byte-identical entries share ONE body copy via
        # aliased offsets (vanilla already wastes a copy: entries 0 Lute and 18
        # Ocarina carry the same "A sonorous instrument..." text). The LAST
        # entry's end is the header total, so the highest-offset body must end
        # exactly at `p` -- guaranteed here because entry 35 is emitted last
        # and offsets grow monotonically for first occurrences.
        new_offs, body_parts = [None] * cnt, []
        seen = {}
        p = 0x10 + cnt * 4
        for i, e in enumerate(ents):
            if e in seen:
                new_offs[i] = seen[e]
                continue
            seen[e] = p
            new_offs[i] = p
            body_parts.append(e)
            p += len(e)
        # entry 35's implicit end is the total; an aliased LAST entry would
        # read past its real text into the next body chunk -- forbid it.
        if new_offs[cnt - 1] != max(new_offs):
            raise ValueError("KEY_EXP: last entry must not alias an earlier one")
        # The bank must stay WITHIN the container: relocating it to the end
        # (append + zero old) grew the recompressed PCK past its dpk slot, with
        # or without keeping the old copy as an LZSS match source (12408 /
        # 13508 vs 12320 -- measured 2026-07-27). So the ceiling is the
        # unclaimed span around it (its own bytes + neighbouring alignment
        # padding, + JOB_NAME's vacated region when pad_class_bank ran first).
        span_off, limit = _place(blob, doff, dsz, p)
        if p > limit:
            raise ValueError(
                f"KEY_EXP re-lay {p}B > {limit}B available in place ({dsz}B "
                f"subfile + {limit - dsz}B unclaimed padding). Shorten the "
                f"authored text: this bank cannot be relocated.")
        new_bank = bytearray(bank[:0x10])
        new_bank += b"".join(struct.pack("<I", o) for o in new_offs)
        new_bank += b"".join(body_parts)
        struct.pack_into("<I", new_bank, 0xC, p)
        # Zero the whole free span first: leftover bytes of the OLD bank past
        # the new end are stale text, and leaving them cost ~14B of compressed
        # PCK (measured 2026-07-27) in a container with only ~6B of headroom.
        blob[span_off:span_off + limit] = b"\x00" * limit
        blob[span_off:span_off + len(new_bank)] = new_bank
        struct.pack_into("<I", blob, rec + 0x18, span_off)
        struct.pack_into("<I", blob, rec + 0x1C, len(new_bank))
        struct.pack_into("<I", blob, rec + 0x20, len(new_bank))
        comp = wp16.compress(bytes(blob))
        if wp16.decompress(comp) != bytes(blob):
            raise ValueError("FM_CAMPUS: wp16 round-trip mismatch (KEY_EXP)")
        if len(comp) > data_size:
            raise ValueError(f"FM_CAMPUS authored PCK {len(comp)} > slot {data_size}")
        f.seek(dpk_off + data_off)                             # the record slot
        f.write(comp + b"\x00" * (data_size - len(comp)))
        f.seek(dpk_off + rec_off + 32)                         # DECOMPRESSED size
        f.write(struct.pack("<I", len(blob)))
    log(f"[campus_bake] KEY_EXP authored ({len(entries)} entr"
        f"{'y' if len(entries) == 1 else 'ies'}; compressed {len(comp)}/{data_size})")


def pad_class_bank(iso_path, log=print):
    """Pad FM_CAMPUS.PCK's class-name bank inside `iso_path` (edited in place,
    same dpk-record slot). No-op-safe: raises loudly if it cannot fit."""
    with open(iso_path, "r+b") as f:
        # Targeted I/O -- record table + this one record slot. Slurping and
        # rewriting the whole dpk costs tens of MB per call, and this step now
        # runs for description bakes too (see patch_iso).
        dpk_off, _dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        nrec = struct.unpack("<I", f.read(4))[0]
        f.seek(dpk_off)
        table = f.read(16 + nrec * 36)
        rec_off, data_off, data_size = _dpk_records(table)[_CAMPUS[:16].split("\0")[0]]
        f.seek(dpk_off + data_off)
        blob = wp16.decompress(f.read(data_size))
        new_blob, dec_size = build_padded_campus(blob)
        comp = wp16.compress(new_blob)
        if wp16.decompress(comp) != new_blob:
            raise ValueError("FM_CAMPUS: wp16 round-trip mismatch")
        if len(comp) > data_size:
            raise ValueError(f"FM_CAMPUS padded PCK {len(comp)} > slot {data_size}")
        # write recompressed stream into the record slot (zero-pad the remainder);
        # the game reads dec_size from the +32 record field, not the wp16 header.
        f.seek(dpk_off + data_off)
        f.write(comp + b"\x00" * (data_size - len(comp)))
        f.seek(dpk_off + rec_off + 32)
        f.write(struct.pack("<I", dec_size))              # DECOMPRESSED size
    log(f"[campus_bake] class-name bank padded to {SLOT}B/entry "
        f"(compressed {len(comp)}/{data_size})")
