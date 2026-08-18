"""Bake AP names into the per-map key-item obtain boxes ON DISC.

The "You obtain the {key}." sentence an NPC handover / event pickup shows is a
per-map string: ff1psp.dpk record USEVM<mapid>.PCK carries MAP<id>.MSG (a TEXT
bank, same format ApClient._mapmsg_scan parses in RAM), _MAP<id>.FIF (the
ASCII->glyph remap table: u16 header {0x12, glyph_count}, ASCII-indexed u16
table at +4) and _MAP<id>.GIM (the glyph atlas image, a per-map SUBSET of the
font). The client's _mapmsg_loop authors the resident RAM copies, but the game
re-copies the bundle (dialog open / post-battle reload) and a fresh copy reads
vanilla until the next authoring tick -- live 2026-08-05 the Waterfall robot's
box showed vanilla seconds after a [keybox] author. Baking the sentence into
the SOURCE bundle makes every copy correct from birth; the RAM loop stays as a
repair net for pre-bake ISOs.

Strategy per bundle: decompress, decode every MSG entry with the bundle's own
FIF, rewrite entries matching the obtain sentence whose key has an AP name,
rebuild the inner PCK (payload offsets shift), recompress. A rebuilt bundle
that still fits its dpk record slot is written back in place zero-padded;
one that grew is RELOCATED into the same map's J2EVM<id>.PCK (else EVM<id>.PCK)
record extent -- the Japanese copies, never loaded in a US boot (the
extern_bake pattern). OWNERSHIP ([[dpk-dead-space-ownership]]): ms2_bake's
donor pool is FM_DBG_*/FM_* records only, so the EVM/J2EVM extents have no
other writer; this module is their sole owner.

Glyph budget (v225): the per-map atlas is tiny (map 0F: 0x26 glyphs) and no
EVM-family atlas on the disc has the digits 7/9, so authored bundles take the
DONOR-FONT SWAP (see the part-2 block below) and render full clean ASCII.
Bundles the swap cannot round-trip (orphan glyphs) fall back to the same
degrade ladder as the RAM loop -- exact glyph, case-swap, visual lookalike,
placeholder.

FIF format (RE'd 2026-08-05): u16 0x12 magic, u16 glyph_count, fixed 276-byte
char->glyph u16 table (chars 0x00..0x89; [0] = the terminator glyph id), then
glyph_count x {u32 strip_x, u16 advance}. GIM: 512-wide 4bpp swizzled strip,
36 px cell rows.

COSMETIC: every failure skips that one bundle with a log line; never raises
past bake_obtain_boxes.
"""
import re
import struct

from . import wp16
from . import ff1_data as FD
from .extern_bake import _find_dpk, _dpk_records

# Map-bundle names are NOT all bare 2-hex map ids: rooms get their own bundle
# (USEVM0200 = map 02 room 00) and the bonus dungeons use USEVMEX/NEW forms.
# Matching only ([0-9A-F]{2}) covered 25 of the 103 USEVM records, so the Elf
# Prince's "You obtain the mystic key." -- which lives in USEVM0200.PCK -- was
# never authored on disc and fell to the RAM loop's lookalike ladder against a
# 0x2d-glyph atlas: "Experience Bag (Small)" rendered "E?perience bag ?Small?"
# (player report 2026-08-10). Every USEVM record has same-named J2EVM/EVM twins,
# so the donor pool generalizes the same way. CMN is excluded on purpose: that
# bundle is the CHEST box template (box_template.py), its control codes are
# relative to its own 53-glyph count, and swapping a 107-glyph donor font into
# it would silently reinterpret them.
_USEVM = re.compile(r"^USEVM(?!CMN)([0-9A-Z]{2,10})\.PCK$")
_JPOOL = re.compile(r"^(J2EVM|EVM)(?!CMN)([0-9A-Z]{2,10})\.PCK$")
_OBTAIN = re.compile(r"You obtain (?:the |a |an )?(.+?)\.?$")

# Mirrors ApClient._MAPMSG_LOOKALIKE (visually-similar fallbacks) -- keep the
# two ladders aligned so a baked box and a RAM-authored box degrade identically.
_LOOKALIKE = {
    "0": "Oo", "1": "lIi", "2": "Zz", "5": "Ss", "6": "b", "8": "B",
    "O": "0o", "o": "O0", "l": "1Ii", "I": "1li", "i": "1Il",
    "S": "5s", "s": "S5", "Z": "2z", "z": "2Z", "B": "8b", "b": "6B",
    ":": ";.", ";": ":.", "'": "`", "`": "'", "-": "_", "_": "-",
}
_PLACEHOLDERS = "*?.- "

# One box line is ~34 glyphs at this face on the PSP's 480 px screen, and the
# authored sentence carries no line-break controls -- cap it so a hostile /
# very long multiworld item name cannot render past the box edge. Same
# lead-drop ladder as ApClient._mapmsg_fit: full sentence, then the name
# alone, then a hard truncate.
_MAX_BOX = 34


def _safe(s):
    """ASCII-escape for LOG LINES ONLY: a multiworld item name can carry any
    codepoint, and a cp1252 console sink raises on emoji -- from inside the
    per-bundle except handler that raise aborted the whole stage (found via
    hostile-name fuzz 2026-08-05)."""
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


def _fit_sentence(ap):
    full = f"You obtained {ap}."
    if len(full) <= _MAX_BOX:
        return full
    alone = f"{ap}."
    if len(alone) <= _MAX_BOX:
        return alone
    return ap[:_MAX_BOX]


def _parse_inner(blob):
    """Inner PCK directory -> [(name, dir_off, off, size)] (stride 36 from 0x10)."""
    cnt = struct.unpack_from("<I", blob, 0)[0]
    recs = []
    o = 0x10
    for _ in range(cnt):
        name = blob[o:o + 24].split(b"\0")[0].decode("latin1")
        off, size = struct.unpack_from("<II", blob, o + 24)
        recs.append((name, o, off, size))
        o += 36
    return recs


def _fif_maps(fif):
    """(char->glyph remap, glyph->char inv, control-code floor h1) or None."""
    if len(fif) < 4 + 0x7F * 2 or struct.unpack_from("<H", fif, 0)[0] != 0x12:
        return None
    h1 = struct.unpack_from("<H", fif, 2)[0]
    if not (0 < h1 < 0x200):
        return None
    remap, inv = {}, {}
    # The table spans chars 0x00..0x89 (fixed 276 bytes; chars past 0x7E are
    # non-ASCII extras like the ellipsis) -- map them all, or a bundle whose
    # dialog uses one extra glyph can never take the donor-font swap (maps
    # 08/18, found 2026-08-05).
    for c in range(0x01, 0x8A):
        g = struct.unpack_from("<H", fif, 4 + c * 2)[0]
        remap[c] = g
        if g != 0xFFFF and g not in inv:
            inv[g] = chr(c)
    return remap, inv, h1


def _encode(text, remap):
    """Encode `text` through the degrade ladder; (bytes, dropped_count)."""
    out, dropped = bytearray(), 0
    for ch in text:
        cands = [ch, ch.swapcase()] + list(_LOOKALIKE.get(ch, ""))
        cands += list(_PLACEHOLDERS)
        for cand in cands:
            g = remap.get(ord(cand), 0xFFFF)
            if g != 0xFFFF:
                if cand in _PLACEHOLDERS and cand != ch:
                    dropped += 1
                out.append(g)
                break
        else:
            dropped += 1
    return bytes(out), dropped


def _rebuild_bank(bank, replacements, term_all=None):
    """Return the TEXT bank with entry k's body replaced per {k: body_bytes}
    (terminator preserved per entry, or forced to `term_all` -- the donor-font
    swap re-encodes every entry, whose terminator glyph id is per-atlas);
    offsets/total recomputed."""
    cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
    total = struct.unpack_from("<I", bank, 0xC)[0]
    offs = list(struct.unpack_from(f"<{cnt}I", bank, 0x10))
    ends = offs[1:] + [total]
    bodies = []
    for k, (a, b) in enumerate(zip(offs, ends)):
        body = bank[a:b]
        if k in replacements:
            term = bytes((term_all,)) if term_all is not None else body[-1:]
            body = replacements[k] + term
        bodies.append(bytes(body))
    head = 0x10 + cnt * 4
    new_offs, p = [], head
    for body in bodies:
        new_offs.append(p)
        p += len(body)
    out = bytearray(bank[:0x10])
    struct.pack_into("<I", out, 0xC, p)             # new total
    for o in new_offs:
        out += struct.pack("<I", o)
    for body in bodies:
        out += body
    return bytes(out)


def _rebuild_pck(blob, replacements):
    """Rebuild the inner PCK with payloads replaced per {inner name: bytes}.
    Payload offsets recomputed 0x10-aligned in original order."""
    recs = _parse_inner(blob)
    first = min(off for _, _, off, _ in recs)
    payloads = []
    for name, _do, off, size in recs:
        data = replacements.get(name, bytes(blob[off:off + size]))
        payloads.append((name, data))
    out = bytearray(blob[:first])                    # header + dir, patched below
    p = first
    for (name, data), (_, do, _, _) in zip(payloads, recs):
        p = (p + 0xF) & ~0xF
        while len(out) < p:
            out.append(0)
        struct.pack_into("<II", out, do + 24, p, len(data))
        out += data
        p += len(data)
    return bytes(out)


# ---- full-font donor swap (part 2, 2026-08-05) ----------------------------
# No per-map atlas covers the digits (7 and 9 exist in NO EVM-family FIF on
# the whole disc), so appending borrowed glyph tiles cannot reach full
# coverage. Instead each authored bundle's FIF+GIM is REPLACED wholesale by
# the FM_EXTERN18US pair (the item-name menu font: same face, same 512-wide
# 4bpp GIM, same 36 px cell rows, 95 printable ASCII incl. all digits) and
# EVERY MSG entry is re-encoded through the donor table. The map's own GIM
# palette block is spliced into the donor image (one tint entry is map-tuned).
# Control codes are COUNT-RELATIVE (byte >= glyph_count, delta 1 = line
# break...; verified across maps 2026-08-05), so re-encoding shifts them by
# (donor_count - map_count). The terminator glyph id is ascii_map[0].
# Any bundle the swap cannot fully re-encode falls back to the lookalike
# ladder above.
_DONOR_REC = "FM_EXTERN18US.PC"


def _load_donor(dpk, recs):
    """(fif_bytes, gim_bytes, remap, count, term) of the donor font, or None.
    Reads through the dpk record table, so it follows extern_bake's
    relocation of the donor bundle (which runs earlier in patch_iso)."""
    r = recs.get(_DONOR_REC)
    if not r:
        return None
    _p, off, size = r
    blob = wp16.decompress(bytes(dpk[off:off + size]))
    inner = _parse_inner(blob)
    fif = next((x for x in inner if x[0].endswith(".FIF")), None)
    gim = next((x for x in inner if x[0].endswith(".GIM")), None)
    if not fif or not gim:
        return None
    fb = bytes(blob[fif[2]:fif[2] + fif[3]])
    maps = _fif_maps(fb)
    if not maps:
        return None
    remap, _inv, count = maps
    term = struct.unpack_from("<H", fb, 4)[0]        # ascii_map[NUL] = terminator
    return fb, bytes(blob[gim[2]:gim[2] + gim[3]]), remap, count, term


def _pal_block(gim):
    """(start, end) extents of the GIM palette block (id 5) payload+header."""
    # GIM block walk: u16 block id at +0, u32 block size at +4, u32 next-
    # block offset at +8. ids 2/3 are CONTAINER blocks -- descend past their
    # 16-byte header rather than skipping their whole extent; id 5 (the
    # palette block) ends the search; nxt == 0 guards a truncated chain.
    p = 0x10
    while p + 16 <= len(gim):
        bid = struct.unpack_from("<H", gim, p)[0]
        nxt = struct.unpack_from("<I", gim, p + 8)[0]
        if bid in (2, 3):
            p += 16
            continue
        if bid == 5:
            return p, p + struct.unpack_from("<I", gim, p + 4)[0]
        if nxt == 0:
            break
        p += nxt
    return None


def _splice_palette(donor_gim, map_gim):
    """Donor GIM with the map GIM's palette block payload (same-size splice --
    keeps the map's tuned text tint). Returns donor unchanged on any shape
    mismatch."""
    d = _pal_block(donor_gim)
    m = _pal_block(map_gim)
    if not d or not m or (d[1] - d[0]) != (m[1] - m[0]):
        return donor_gim
    out = bytearray(donor_gim)
    out[d[0]:d[1]] = map_gim[m[0]:m[1]]
    return bytes(out)


# A glyph below the control floor that NO char in the bundle's 138-entry table
# names. Every one observed is the EM-DASH (16 px advance, e.g. USEVM0200 0x2b /
# USEVM0A00 0x2d / USEVM08 0x31 / USEVM18 0x29 -- rendered and eyeballed
# 2026-08-10); the donor face has no em-dash of its own (its single orphan 0x46
# is an 18 px blank), so it becomes a hyphen. Returning None here instead used
# to veto the WHOLE bundle's donor swap over one dash in an unrelated line of
# vanilla dialogue -- which is why the Elf Prince's box degraded to
# "E?perience bag ?Small?" while other maps rendered AP names cleanly.
_ORPHAN_CH = "-"


def _tokenize(bank, cnt, total, gcount, inv):
    """([(entry idx, [token...])], orphan count) for every entry; token = str
    char or ('ctrl', delta). Undecodable glyphs become _ORPHAN_CH and are
    counted so the caller can report them -- never silent."""
    offs = list(struct.unpack_from(f"<{cnt}I", bank, 0x10))
    ends = offs[1:] + [total]
    out, orphans = [], 0
    for k, (a, b) in enumerate(zip(offs, ends)):
        if not (0x14 <= a < b <= total):
            out.append((k, None))
            continue
        toks = []
        for x in bank[a:b - 1]:
            if x >= gcount:
                toks.append(("ctrl", x - gcount))
            elif x in inv:
                toks.append(inv[x])
            else:
                toks.append(_ORPHAN_CH)
                orphans += 1
        out.append((k, toks))
    return out, orphans


def bake_obtain_boxes(iso_path, key_names, log=print):
    """Author every USEVM bundle's obtain sentence whose key is in `key_names`
    ({key id: AP item name}). Edits `iso_path` in place; cosmetic -- skips any
    bundle it cannot process."""
    if not key_names:
        return
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        recs = _dpk_records(dpk)
        try:
            donor = _load_donor(dpk, recs)
        except Exception as e:
            donor = None
            log(f"[evm_bake] donor font unavailable ({_safe(repr(e))}) -- lookalike "
                f"ladder only")
        # Pre-scan which USEVM bundles will be authored: their own J records
        # must never be handed to another bundle's cross-map relocation.
        authored_maps = set()
        for name, (_ro, off, size) in recs.items():
            if not _USEVM.match(name):
                continue
            try:
                blob = wp16.decompress(bytes(dpk[off:off + size]))
                inner = _parse_inner(blob)
                msgr = next((r for r in inner if r[0].endswith(".MSG")), None)
                fifr = next((r for r in inner if r[0].endswith(".FIF")), None)
                if not msgr or not fifr:
                    continue
                mp = _fif_maps(blob[fifr[2]:fifr[2] + fifr[3]])
                if not mp:
                    continue
                _rm, inv0, _h = mp
                bank = blob[msgr[2]:msgr[2] + msgr[3]]
                cnt0 = struct.unpack_from("<I", bank, 8)[0] >> 8
                tot0 = struct.unpack_from("<I", bank, 0xC)[0]
                o0 = list(struct.unpack_from(f"<{cnt0}I", bank, 0x10))
                for a, b in zip(o0, o0[1:] + [tot0]):
                    if not (0x14 <= a < b <= tot0):
                        continue
                    mo = _OBTAIN.match("".join(inv0.get(x, "?")
                                               for x in bank[a:b - 1]))
                    kid = FD.key_item_id(mo.group(1)) if mo else None
                    if kid is not None and key_names.get(kid):
                        authored_maps.add(name)
                        break
            except Exception:
                pass
        used = set()
        done = 0
        for name, (rec_off, off, size) in sorted(recs.items()):
            m = _USEVM.match(name)
            if not m:
                continue
            try:
                blob = wp16.decompress(bytes(dpk[off:off + size]))
                inner = _parse_inner(blob)
                msg = next((r for r in inner if r[0].endswith(".MSG")), None)
                fif = next((r for r in inner if r[0].endswith(".FIF")), None)
                if not msg or not fif:
                    continue
                maps = _fif_maps(blob[fif[2]:fif[2] + fif[3]])
                if not maps:
                    log(f"[evm_bake] {name}: FIF parse failed -- skipped")
                    continue
                remap, inv, h1 = maps
                bank = blob[msg[2]:msg[2] + msg[3]]
                if len(bank) < 0x14 or bank[4:8] != b"TEXT":
                    continue
                cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
                total = struct.unpack_from("<I", bank, 0xC)[0]
                if not (0 < cnt < 0x80) or total > len(bank):
                    continue
                offs = list(struct.unpack_from(f"<{cnt}I", bank, 0x10))
                ends = offs[1:] + [total]
                authored = {}                     # entry idx -> new plain text
                for k, (a, b) in enumerate(zip(offs, ends)):
                    if not (0x14 <= a < b <= total):
                        continue
                    txt = "".join(inv.get(x, "?") for x in bank[a:b - 1])
                    mo = _OBTAIN.match(txt)
                    if not mo:
                        continue
                    kid = FD.key_item_id(mo.group(1))
                    ap = key_names.get(kid) if kid is not None else None
                    if not ap:
                        continue
                    authored[k] = (mo.group(1), _fit_sentence(ap))
                if not authored:
                    continue
                # Preferred path: swap in the donor font and re-encode every
                # entry -- full ASCII, no degradation. Falls through to the
                # lookalike ladder on any coverage gap.
                pck_repl = None
                if donor is not None:
                    d_fif, d_gim, d_remap, d_count, d_term = donor
                    toks, orphans = _tokenize(bank, cnt, total, h1, inv)
                    if toks is not None:
                        for k, (_v, ap_txt) in authored.items():
                            toks[k] = (k, list(ap_txt))
                        bodies, ok = {}, True
                        for k, tk in toks:
                            if tk is None:
                                continue
                            enc = bytearray()
                            for t in tk:
                                if isinstance(t, tuple):
                                    enc.append(d_count + t[1])
                                else:
                                    g = d_remap.get(ord(t), 0xFFFF)
                                    if g == 0xFFFF:
                                        ok = False
                                        break
                                    enc.append(g)
                            if not ok:
                                break
                            bodies[k] = bytes(enc) + bytes((d_term,))
                        if ok:
                            gim = next((r for r in inner
                                        if r[0].endswith(".GIM")), None)
                            if gim:
                                pck_repl = {
                                    msg[0]: _rebuild_bank(
                                        bank[:total],
                                        {k: v[:-1] for k, v in bodies.items()},
                                        term_all=d_term),
                                    fif[0]: d_fif,
                                    gim[0]: _splice_palette(
                                        d_gim,
                                        bytes(blob[gim[2]:gim[2] + gim[3]])),
                                }
                                for k, (vn, ap_txt) in authored.items():
                                    log(f"[evm_bake] {name}: '{vn}' -> "
                                        f"{_safe(ap_txt)!r} (donor font)")
                                if orphans:
                                    log(f"[evm_bake] {name}: {orphans} orphan "
                                        f"glyph(s) in vanilla text rendered "
                                        f"'{_ORPHAN_CH}' (no donor equivalent)")
                def _ladder_repl():
                    # Lookalike-ladder fallback: author only the obtain
                    # entries with the map's own atlas.
                    repl = {}
                    for k, (vn, ap_txt) in authored.items():
                        enc, dropped = _encode(ap_txt, remap)
                        repl[k] = enc
                        log(f"[evm_bake] {name}: '{vn}' -> {_safe(ap_txt)!r}"
                            + (f" ({dropped} glyph(s) degraded)"
                               if dropped else ""))
                    return {msg[0]: _rebuild_bank(bank[:total], repl)}

                def _write(new_blob):
                    """Fit the rebuilt bundle in place, in the same map's
                    Japanese record, or in a free J record of an UNAUTHORED
                    map (all US-unused dead space; `used` prevents two
                    relocations sharing an extent); False if nowhere fits."""
                    comp = wp16.compress(new_blob)
                    if len(comp) <= size:
                        dpk[off:off + len(comp)] = comp
                        dpk[off + len(comp):off + size] = (
                            b"\0" * (size - len(comp)))
                        struct.pack_into("<I", dpk, rec_off + 32,
                                         len(new_blob))
                        return True
                    same = [f"J2EVM{m.group(1)}.PCK", f"EVM{m.group(1)}.PCK"]
                    # Cross-map pool, best-fit: every J2EVM/EVM record whose
                    # map's US bundle is NOT being authored this run stays
                    # dead space forever -- fair game for a bundle whose own
                    # J records are too small (small-map atlas + donor GIM).
                    cross = sorted(
                        (jn for jn in recs
                         if _JPOOL.match(jn) and jn not in same
                         and f"USEVM{_JPOOL.match(jn).group(2)}.PCK"
                         not in authored_maps),
                        key=lambda jn: recs[jn][2])
                    for jn in same + cross:
                        j = recs.get(jn)
                        if not j or jn in used or len(comp) > j[2]:
                            continue
                        _j_rec, j_off, j_size = j
                        dpk[j_off:j_off + len(comp)] = comp
                        dpk[j_off + len(comp):j_off + j_size] = (
                            b"\0" * (j_size - len(comp)))
                        struct.pack_into("<III", dpk, rec_off + 24,
                                         j_off, len(comp), len(new_blob))
                        used.add(jn)
                        log(f"[evm_bake] {name}: relocated into {jn} "
                            f"@{j_off:#x}")
                        return True
                    return False

                wrote = pck_repl is not None and _write(_rebuild_pck(blob,
                                                                     pck_repl))
                if not wrote and pck_repl is not None:
                    log(f"[evm_bake] {name}: donor-font bundle fits nowhere "
                        f"-- retrying with the map's own atlas")
                if not wrote:
                    wrote = _write(_rebuild_pck(blob, _ladder_repl()))
                if not wrote:
                    log(f"[evm_bake] {name}: grew past every donor -- skipped")
                    continue
                done += 1
            except Exception as e:                    # cosmetic: never abort bake
                log(f"[evm_bake] {name}: {_safe(repr(e))} -- skipped")
        if done:
            f.seek(dpk_off)
            f.write(dpk)
        log(f"[evm_bake] obtain boxes baked in {done} map bundle(s)")
