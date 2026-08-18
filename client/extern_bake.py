"""Bake the spell-tome NAME/DESC banks into the ISO (B2 real names).

The menu NAME/DESC text banks live wp16-compressed inside ff1psp.dpk ->
FM_EXTERN12US.PC and FM_EXTERN18US.PC (both loaded into RAM; the game re-derives
the heap bank-pointer registry from these on every menu load -- which is why the
runtime bank-authoring in tome_names.py flapped). Baking the grown banks into the
disc image makes the loader register the extended banks itself: no client repoint,
no flap.

Each FM_EXTERN*.PC decompresses to a small archive:
  header 0x10 (u32 count, u32 total, 8 pad) + count * 36-byte dir records
    record = name[22] + u16 id + u32 offset + u32 size + u32 size(dup)
  then the .MSG payloads (each an 0x10-aligned TEXT bank).
We grow ITEM_NAME.MSG (43 -> 107 entries) and ITEM_EXP.MSG (descs, same) to add
the 64 spell-tome entries (verbatim magic name/desc entries, per slot), rebuild
the archive + directory, recompress, and RELOCATE each grown US bundle into the
matching Japanese-extern record's dead space (FM_EXTERN12J1 / 18J1 -- never loaded
in a US boot) so the dpk and ISO stay byte-for-byte the same size (no LBA-shift
repacker needed). The 12US/18US dpk record offsets are repointed to the J region.

Pairs with iso_patcher.apply_spell_tomes: the boot cave fills the cat1 (name,desc)
id-array's tome entries with string ids (43+slot, 43+slot) that index these new
bank entries. Both are gated on the spell_tomes feature, so they always ship
together (the array fill without the grown banks would OOB-read the vanilla bank).
"""
import struct

from . import wp16
from . import tome_names as TN
from . import ff1_data as FD

SECTOR = 2048
US_TO_J = (("FM_EXTERN12US.PC", "FM_EXTERN12J1.PC"),
           ("FM_EXTERN18US.PC", "FM_EXTERN18J1.PC"))
_ITEM_NAME = "ITEM_NAME.MSG"
_ITEM_DESC = "ITEM_EXP.MSG"
_KEY_NAME = "KEY_NAME.MSG"
# blood_magic desc leg: equipment desc banks re-authored with the HP-cost
# sentence (FD.blood_desc_bank; both bundles carry both banks).
_BLOOD_DESC_RECORDS = {"WEAPON_EXP.MSG": "weapons", "ARMOR_EXP.MSG": "armor"}

# "You obtain the {key}." box: keep authored key names comfortably inside the
# box width (same ballpark as name_banks.MAX_NAME_GLYPHS).
KEY_NAME_GLYPHS = 24


# ---- ISO9660: locate ff1psp.dpk -------------------------------------------
def _iso_records(f, lba, size):
    # ISO9660 directory record fields read below: +2 extent LBA (u32 LE),
    # +10 data length (u32 LE), +25 flags (bit1 = directory), +32 name
    # length, +33 name bytes. A zero record-length means the listing
    # continues at the next 2048-byte sector boundary, not end-of-dir.
    f.seek(lba * SECTOR)
    data = f.read(size)
    p = 0
    while p < len(data):
        rl = data[p]
        if rl == 0:
            p = (p // SECTOR + 1) * SECTOR
            continue
        elba = struct.unpack_from("<I", data, p + 2)[0]
        elen = struct.unpack_from("<I", data, p + 10)[0]
        flags = data[p + 25]
        nlen = data[p + 32]
        name = data[p + 33:p + 33 + nlen]
        yield name, elba, elen, flags
        p += rl


def _iso_walk(f, lba, size):
    for name, elba, elen, flags in _iso_records(f, lba, size):
        if name in (b"\x00", b"\x01"):
            continue
        nm = name.split(b";")[0].decode("latin1")
        if flags & 2:
            yield from _iso_walk(f, elba, elen)
        else:
            yield nm, elba, elen


def _find_dpk(f):
    f.seek(16 * SECTOR)
    pvd = f.read(SECTOR)
    root = pvd[156:156 + 34]
    rl = struct.unpack_from("<I", root, 2)[0]
    rn = struct.unpack_from("<I", root, 10)[0]
    for nm, lba, size in _iso_walk(f, rl, rn):
        if nm.upper() == "FF1PSP.DPK":
            return lba * SECTOR, size
    raise KeyError("ff1psp.dpk not found in ISO")


# ---- dpk record table ------------------------------------------------------
def _dpk_records(dpk):
    """name -> (record_table_offset, data_offset, data_size). Record layout
    (36 bytes, table at +16): name (NUL-padded; LONGER names are truncated by
    our 16-byte read -- 'FM_EXTERN12US.PCK' keys as 'FM_EXTERN12US.PC') +
    u32 offset @+24 + u32 compressed size @+28 + u32 DECOMPRESSED size @+32.
    The game allocates its load buffer from the +32 field, NOT the wp16
    stream header -- any rebuild that grows a bundle MUST update it."""
    cnt = struct.unpack_from("<I", dpk, 0)[0]
    out = {}
    p = 16
    for _ in range(cnt):
        name = dpk[p:p + 16].split(b"\0")[0].decode("latin1")
        off, size = struct.unpack_from("<II", dpk, p + 24)
        out.setdefault(name, (p, off, size))
        p += 36
    return out


# ---- FM_EXTERN bundle rebuild ---------------------------------------------
def _parse_bundle_dir(blob):
    cnt = struct.unpack_from("<I", blob, 0)[0]
    recs = []
    o = 0x10
    for _ in range(cnt):
        name = blob[o:o + 22].split(b"\0")[0].decode("latin1")
        off, size = struct.unpack_from("<II", blob, o + 24)  # +24 off, +28 size
        recs.append((name, o, off, size))
        o += 36
    return cnt, recs


# ---- glyph-page unification (FM_EXTERN18US) --------------------------------
# The two extern bundles hold the same 107 glyphs in two id orders (name_banks'
# PAGE block): 12US is the 12 px menu face, 18US the 18 px face the CHEST REWARD
# BOX and the KEY ITEMS menu draw with. Square stores each bundle's NAME banks
# in its own order, but the runtime does NOT honour that pairing -- it serves the
# 12US banks to both faces, so every X/U/Z/Q in a box came out a digit
# ("DragoniteZ3OW" -> "Dragonite9OW"; vanilla "Ultima Weapon" -> "8ltima
# Weapon", live 2026-08-08/10) while the item menu drew the same bytes fine.
#
# Rather than encode per surface -- impossible for the runtime-authored dyn
# slots, which are one DataPatch writing one byte string into every copy -- the
# bake REPOINTS THE 18US FACE onto the menu page. A FIF is
#     u16 magic, u16 glyph_count, 276-byte char->glyph u16 table,
#     glyph_count x { u32 strip_x, u16 advance }
# and a bank byte is an INDEX into that metric array, so permuting 14 metric
# records (plus the char table that names them) is the entire change: no pixel
# in the .GIM moves, the face keeps its size and metrics, only the ids do.
# 18US's own banks are re-encoded to match, leaving ONE page in the image.
_FIF_MAGIC_OFF, _FIF_CNT_OFF = 0, 2
_FIF_CHARS = 0x8A                    # char->glyph table covers chars 0x00..0x89
_FIF_TBL = 4                         # ...starting here
_FIF_METRICS = _FIF_TBL + _FIF_CHARS * 2
_FIF_METRIC_SZ = 6                   # u32 strip_x + u16 advance
_UNIFY_REC = "FM_EXTERN18US.PC"      # the bundle whose face gets repointed
# 18US banks holding box-page glyphs (its *_EXP desc banks and KEY_NAME are
# byte-identical to 12US's already, so they are menu page and must NOT move).
_UNIFY_BANKS = ("ITEM_NAME.MSG", "WEAPON_NAME.MSG", "ARMOR_NAME.MSG",
                "MAGIC_NAME.MSG", "VALUE.MSG")


def _repoint_fif(fif):
    """FM_EXTERN18US's font table with the 14 swapped ids moved onto the MENU
    page. Pure permutation: metric[m] takes what metric[PAGE_SWAP[m]] held, and
    the char table is relabelled the same way. Raises on any shape surprise --
    a silently-skipped repoint would leave the image half-converted, which is
    worse than a failed bake (the client refuses to boot an unpatched ISO)."""
    from . import name_banks as NB
    fif = bytearray(fif)
    cnt = struct.unpack_from("<H", fif, _FIF_CNT_OFF)[0]
    want = _FIF_METRICS + cnt * _FIF_METRIC_SZ
    if len(fif) != want:
        raise ValueError(f"18US FIF is {len(fif)} bytes, expected {want} "
                         f"for {cnt} glyphs")
    if cnt <= max(NB.PAGE_SWAP):
        raise ValueError(f"18US FIF has only {cnt} glyphs -- the swapped block "
                         f"reaches {max(NB.PAGE_SWAP):#x}")
    # metrics: dst id m gets the record currently under its box-page id
    def _rec(i):
        o = _FIF_METRICS + i * _FIF_METRIC_SZ
        return bytes(fif[o:o + _FIF_METRIC_SZ])
    moved = {m: _rec(b) for m, b in NB.PAGE_SWAP.items()}
    for m, rec in moved.items():
        o = _FIF_METRICS + m * _FIF_METRIC_SZ
        fif[o:o + _FIF_METRIC_SZ] = rec
    # char table: every entry that named a swapped id now names its menu id
    for ch in range(_FIF_CHARS):
        o = _FIF_TBL + ch * 2
        g = struct.unpack_from("<H", fif, o)[0]
        if g in NB.PAGE_UNSWAP:
            struct.pack_into("<H", fif, o, NB.PAGE_UNSWAP[g])
    return bytes(fif)


def _to_menu_page_bank(bank):
    """A TEXT name bank with every ENTRY translated box page -> menu page. The
    u32 offset table is left alone (it can hold any byte value, so a blanket
    blob translate would corrupt it)."""
    from . import name_banks as NB
    cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
    total = struct.unpack_from("<I", bank, 0xC)[0]
    offs = list(struct.unpack_from(f"<{cnt}I", bank, 0x10))
    out = bytearray(bank)
    for a, b in zip(offs, offs[1:] + [total]):
        if not (0x10 + cnt * 4 <= a < b <= len(bank)):
            raise ValueError(f"bank entry [{a:#x},{b:#x}) out of range")
        out[a:b] = NB.from_box_page(bank[a:b])
    return bytes(out)


def _unify_glyph_page(payloads):
    """Repoint this bundle's face and re-encode its own banks, in place on the
    {record name: payload} dict. Returns the FIF record's name (for logging)."""
    fif_name = next((n for n in payloads if n.upper().endswith(".FIF")), None)
    if fif_name is None:
        raise KeyError("18US bundle has no .FIF font table")
    payloads[fif_name] = _repoint_fif(payloads[fif_name])
    for n in _UNIFY_BANKS:
        if n in payloads:
            payloads[n] = _to_menu_page_bank(payloads[n])
    return fif_name


def _author_key_bank(bank, key_names, pad_ids=None):
    """Re-lay the KEY_NAME.MSG TEXT bank with the granted keys' entries replaced
    by their location's AP item name. bank layout (vanilla 0x21f bytes): 0x10
    header (u32 pad, 'TEXT', u32 (entry_count<<8)|dim, u32 total size), then
    count u32 offsets (relative to bank start), then packed entries of menu-font
    glyphs + TERM 0x06 (no icon prefix; entry index = key id - 1). Offsets are
    rewritten, so authored names may exceed their vanilla byte budget.

    pad_ids = key ids whose entry is WIDENED to KEY_NAME_GLYPHS space glyphs
    (existing name kept, trailing spaces added). lute_tablets needs this: the
    client rewrites entry 0 to the live "Lute Tablets N of M" ratio at runtime,
    and the RESIDENT bank buffer has no slack to grow -- the wide slot must be
    pre-sized on disc, exactly like campus_bake pads the class-name slots. The
    padding is invisible in-game (space glyph 0x00), so a seed that never
    writes a ratio still just reads "Lute"."""
    total = struct.unpack_from("<I", bank, 0xC)[0]
    cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
    offs = struct.unpack_from(f"<{cnt}I", bank, 0x10)
    ends = list(offs[1:]) + [total]
    ents = [bytes(bank[a:b]) for a, b in zip(offs, ends)]
    for kid, nm in (key_names or {}).items():
        kid = int(kid)
        if 1 <= kid <= cnt:
            # Menu page like everything else: the KEY ITEMS menu draws with the
            # 18 px face, which _unify_glyph_page has repointed onto that page.
            # (Vanilla KEY_NAME is byte-identical in both bundles -- no vanilla
            # key name uses a swapped glyph -- so it needs no translate.)
            ents[kid - 1] = (TN._menu_bytes(TN._cap_menu(nm, KEY_NAME_GLYPHS))
                             + b"\x06")
    for kid in (pad_ids or ()):
        kid = int(kid)
        if not 1 <= kid <= cnt:
            continue
        body = ents[kid - 1][:-1][:KEY_NAME_GLYPHS]      # drop TERM, cap width
        ents[kid - 1] = (body + b"\x00" * (KEY_NAME_GLYPHS - len(body))
                         + b"\x06")
    first = 0x10 + cnt * 4
    new_offs, p = [], first
    for e in ents:
        new_offs.append(p)
        p += len(e)
    out = bytearray(bank[:0x10])
    struct.pack_into("<I", out, 0xC, p)
    out += struct.pack(f"<{cnt}I", *new_offs) + b"".join(ents)
    return bytes(out)


def _blood_desc_msg(bank, key):
    """WEAPON_EXP/ARMOR_EXP.MSG bank with the blood_magic cost sentence appended
    to every activatable entry. Built from FD.blood_desc_bank (the SAME transform
    the client's shop-desc DataPatch baseline uses -- byte parity required, so
    the vanilla payload is asserted first). Header kept, total-size patched."""
    from . import name_banks as NB
    if bytes(bank[0x10:]) != NB.DESC_BANKS[key]["payload"]:
        raise ValueError(f"{key} desc bank is not vanilla")
    d = FD.blood_desc_bank(key)
    out = bytearray(bank[:0x10])
    struct.pack_into("<I", out, 0xC, 0x10 + len(d["payload"]))
    return bytes(out) + d["payload"]


def rebuild_bundle(blob, name_bank, desc_bank, key_names=None, blood=False,
                   pad_ids=None, unify_page=False):
    """Return a new decompressed bundle with ITEM_NAME/ITEM_EXP replaced by the
    grown banks (either may be None = keep vanilla) and, when key_names or
    pad_ids is given, KEY_NAME re-authored (key id -> AP item name, plus widened
    slots for pad_ids; see _author_key_bank). blood=True re-authors
    WEAPON_EXP/ARMOR_EXP with the blood_magic cost text. Directory
    offsets/sizes recomputed, payload order preserved.

    unify_page=True (FM_EXTERN18US only) repoints this bundle's 18 px face onto
    the menu glyph page and re-encodes its own banks to match -- see
    _unify_glyph_page. It runs BEFORE the replacements below on purpose: those
    are authored menu-page already, so translating them too would undo them."""
    cnt, recs = _parse_bundle_dir(blob)
    payloads = {n: bytes(blob[off:off + size]) for n, _do, off, size in recs}
    if _ITEM_NAME not in payloads or _ITEM_DESC not in payloads:
        raise KeyError("bundle missing item name/desc banks")
    if unify_page:
        _unify_glyph_page(payloads)
    if name_bank is not None:
        payloads[_ITEM_NAME] = name_bank
    if desc_bank is not None:
        payloads[_ITEM_DESC] = desc_bank
    if key_names or pad_ids:
        if _KEY_NAME not in payloads:
            raise KeyError("bundle missing key-item name bank")
        payloads[_KEY_NAME] = _author_key_bank(payloads[_KEY_NAME], key_names,
                                               pad_ids=pad_ids)
    if blood:
        for rec_name, key in _BLOOD_DESC_RECORDS.items():
            if rec_name in payloads:
                payloads[rec_name] = _blood_desc_msg(payloads[rec_name], key)

    hdr_end = 0x10 + cnt * 36
    # place payloads in original file order, each 0x10-aligned (as vanilla)
    order = sorted(recs, key=lambda r: r[2])
    new_off, cur = {}, (hdr_end + 0xF) & ~0xF
    for name, _do, _o, _s in order:
        new_off[name] = cur
        cur = (cur + len(payloads[name]) + 0xF) & ~0xF
    total = cur

    out = bytearray(total)
    out[:hdr_end] = blob[:hdr_end]           # header + dir (patched below)
    struct.pack_into("<I", out, 4, total)    # total size
    for name, do, _o, _s in recs:
        no, ns = new_off[name], len(payloads[name])
        struct.pack_into("<III", out, do + 24, no, ns, ns)  # off, size, size(dup)
    for name in new_off:
        no = new_off[name]
        out[no:no + len(payloads[name])] = payloads[name]
    return bytes(out)


# ---- top-level bake --------------------------------------------------------
def _slot_capacity(recs, dpk_len, off, size):
    """True writable capacity of the dpk extent starting at `off`: the gap to
    the NEXT record's data (records never overlap; the inter-record pad belongs
    to nobody). Never less than the record's own size."""
    nxt = min((o for _ro, o, _s in recs.values() if o > off), default=dpk_len)
    return max(size, nxt - off)


def _pair_layouts(recs, dpk):
    """Per US/J extern pair, decide the copy layout ONCE (shared by plan_remote
    and bake_names so the plan can never disagree with the bake):

    split (v241, the normal case): copy A = the grown US bundle gets the WHOLE
    J extent; copy B = the VANILLA blob J-retagged goes back into the old US
    extent. The pre-v241 layout packed BOTH grown copies into the J extent --
    halving the remote-name budget to serve a J variant a US boot never loads
    (live 2026-08-07: 175 distinct multiworld names collapsed the shared cap
    to 4 chars -> the chest box read "your!"). The J record still serves a
    VALID bundle (the v11 rule) -- just the vanilla one, not the grown one.

    legacy: if the retagged-vanilla copy B compresses a hair past the US
    extent (tag bytes shift the wp16 stream), fall back to both-grown-in-J.

    Returns [{us_tag, j_tag, us_off, j_off, blob, cap_a, comp_b, split}]."""
    out = []
    for us_tag, j_tag in US_TO_J:
        rec_off, us_off, us_size = recs[us_tag[:16].split("\0")[0]]
        jrec_off, j_off, j_size = recs[j_tag[:16].split("\0")[0]]
        blob = bytes(wp16.decompress(bytes(dpk[us_off:us_off + us_size])))
        blob_b = blob.replace(us_tag[:-3].encode("latin1"),
                              j_tag[:-3].encode("latin1"))
        comp_b = wp16.compress(blob_b)
        us_cap = _slot_capacity(recs, len(dpk), us_off, us_size)
        j_cap = _slot_capacity(recs, len(dpk), j_off, j_size)
        split = (len(comp_b) <= us_cap
                 and wp16.decompress(comp_b) == blob_b)
        out.append(dict(us_tag=us_tag, j_tag=j_tag, rec_off=rec_off,
                        jrec_off=jrec_off, us_off=us_off, j_off=j_off,
                        blob=blob, blob_b=blob_b, comp_b=comp_b,
                        cap_a=j_cap, split=split,
                        # copy B is the J-retagged VANILLA bundle, never loaded
                        # in a US boot, so it keeps its vanilla face untouched
                        unify_page=(us_tag == _UNIFY_REC)))
    return out


def _fits_layouts(layouts, name_bank, desc_bank, key_names=None,
                  blood=False, pad_ids=None):
    """True iff the grown US bundle fits every pair's copy-A budget (split
    layout) resp. both grown copies fit the J extent (legacy layout)."""
    for L in layouts:
        grown = rebuild_bundle(L["blob"], name_bank, desc_bank,
                               key_names=key_names, blood=blood,
                               pad_ids=pad_ids, unify_page=L["unify_page"])
        comp = wp16.compress(grown)
        if L["split"]:
            if len(comp) > L["cap_a"]:
                return False
        else:
            comp_j = wp16.compress(grown.replace(
                L["us_tag"][:-3].encode("latin1"),
                L["j_tag"][:-3].encode("latin1")))
            if ((len(comp) + 0xF) & ~0xF) + len(comp_j) > L["cap_a"]:
                return False
    return True


def plan_remote(iso_path, remote_names, remap=None, levels=None,
                cap0=32, key_names=None, blood=False, tomes=True,
                pad_ids=None, item_descs=None, dyn_slots=0, log=print):
    """Decide the rendering at which all remote AP names fit on disc. Returns
    (baked_names, string_id_base): baked_names[k] is the sanitized/capped name
    baked at string id (string_id_base + k). COUNT-PRESERVING: len(baked_names)
    == len(remote_names) always, so the client's fixed sid = base + k is always
    a valid bank entry.

    remote_names entries are (who, item) PAIRS (who = "your"/"<player>'s"; the
    part truncation must never eat) or legacy pre-joined strings. The rung
    ladder (TN.remote_rungs) shrinks the ITEM portion only and NEVER yields a
    blank -- worst case every entry still reads "<who> AP item" / "AP item"
    (the pre-v241 blanking rung showed an EMPTY box; and its uniform head-
    truncate produced the "your!" box, live 2026-08-07).

    dyn_slots appends that many wide runtime-authored entries after the remote
    block (bonus dynamic chest names; see TN.dyn_slot_entry).

    tomes=True: remote rides on the tome-extended bank, base = 43 + 64 = 107.
    tomes=False (spell_tomes off): the bank has no tome block, base = 43."""
    if remap is None:
        remap = list(range(64))
    with open(iso_path, "rb") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
    recs = _dpk_records(dpk)
    layouts = _pair_layouts(recs, dpk)
    base = TN.BANK_ENTRIES if tomes else TN.ITEM_ENTRIES  # remote sids start here
    pairs = TN.normalize_remote(remote_names)
    def _banks(capped):
        return TN.build_extended_banks(remap, levels, remote=capped,
                                       tomes=tomes, item_descs=item_descs,
                                       dyn_slots=dyn_slots)

    for label, capped in TN.remote_rungs(pairs, cap0=cap0):
        if _fits_layouts(layouts, *_banks(capped), key_names=key_names,
                         blood=blood, pad_ids=pad_ids):
            mode = "split" if all(L["split"] for L in layouts) else "legacy"
            log(f"[extern_bake] remote names: {len(capped)} entries at "
                f"{label} (+{dyn_slots} dyn slots, {mode} layout)")
            return capped, base
    # Even the all-generic rung failed -- something is structurally wrong with
    # the dpk (not a name-volume problem: that rung is ~9 bytes/entry). Blank
    # trailing entries as the absolute last resort (still count-preserving).
    capped = ["AP item"] * len(pairs)
    blanks = 0
    while capped:
        if _fits_layouts(layouts, *_banks(capped), key_names=key_names,
                         blood=blood, pad_ids=pad_ids):
            break
        for i in range(len(capped) - 1, -1, -1):
            if capped[i]:
                capped[i] = ""
                blanks += 1
                break
        else:
            break
    log(f"[extern_bake] WARNING remote bank overflow: {blanks}/"
        f"{len(pairs)} names blanked (generic rung failed -- dpk anomaly?)")
    return capped, base


def bake_names(iso_path, remap=None, levels=None, remote=None, key_names=None,
               items=True, blood=False, tomes=True, pad_ids=None,
               item_descs=None, dyn_slots=0, log=print):
    """Grow + relocate both US extern bundles inside `iso_path` (edited in place,
    same size). remap[slot] = magic name index for spell slot (default identity;
    FF1 PSP does not name-shuffle spell slots -- see spell-tome-items-re memory).
    levels[slot] (optional) = shuffled spell level (magic_info+9) -> " Level X"
    is appended to each tome's description. remote (optional) = list of already
    sanitized/capped AP names (from plan_remote) appended at string ids
    base..base+R-1 (base = 107 with tomes, 43 without) for the poll-based
    remote-chest box name detour. tomes=False drops the 64-entry tome block so
    remote names can bake with spell_tomes off.

    key_names (optional) = {key item id (1..36): AP item name} authored into
    KEY_NAME.MSG so the 'You obtain the {key}.' key-item-add box (event-key
    chests + NPC handovers) shows the AP item actually placed at that location.
    item_descs (optional) = {consumable gid: replacement description} for items
    a feature repurposed (slot_magic -> Soma Drop). Honoured with items=True and,
    as a desc-bank-only rewrite, with items=False.

    items=False skips the item NAME/DESC growth (spell_tomes off) and only
    re-authors KEY_NAME -- the bundle still relocates to the J dead space.
    blood=True appends the blood_magic HP-cost sentence to every activatable
    entry of WEAPON_EXP/ARMOR_EXP.MSG (FD.blood_desc_bank; the client's
    shop-desc baseline mirrors the same transform).

    pad_ids (optional) = key ids whose KEY_NAME entry is widened to
    KEY_NAME_GLYPHS spaces so the client can rewrite it in place at runtime
    (lute_tablets ratio; see _author_key_bank)."""
    if remap is None:
        remap = list(range(64))
    if items:
        name_bank, desc_bank = TN.build_extended_banks(
            remap, levels, remote=remote, tomes=tomes, item_descs=item_descs,
            dyn_slots=dyn_slots)
    elif item_descs:
        # DESC-ONLY rewrite (e.g. slot_magic's Soma Drop text in a seed with no
        # tomes and no remote names): rebuild the 43-entry banks and hand over
        # only the description one, so the NAME bank stays the untouched vanilla
        # payload rather than a re-encoded copy of it.
        _nb, desc_bank = TN.build_extended_banks(remap, None, tomes=False,
                                                 item_descs=item_descs)
        name_bank = None
    else:
        name_bank = desc_bank = None
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        recs = _dpk_records(dpk)
        layouts = _pair_layouts(recs, dpk)
        for L in layouts:
            us_tag, j_tag = L["us_tag"], L["j_tag"]
            rec_off, jrec_off = L["rec_off"], L["jrec_off"]
            us_off, j_off = L["us_off"], L["j_off"]
            grown = rebuild_bundle(L["blob"], name_bank, desc_bank,
                                   key_names=key_names, blood=blood,
                                   pad_ids=pad_ids, unify_page=L["unify_page"])
            comp = wp16.compress(grown)
            if wp16.decompress(comp) != grown:
                raise ValueError(f"{us_tag}: wp16 round-trip mismatch")
            # The J record is NOT dead weight: it still points at real space,
            # so it MUST keep serving a valid bundle (v11 left it aliased onto
            # the US stream with its OLD size + US-named internal records --
            # anything loading the J variant then parsed garbage).
            if L["split"]:
                # v241 split layout: copy A (grown US) takes the WHOLE J
                # extent; copy B = the VANILLA bundle J-retagged goes back
                # into the old US extent (a US boot never loads the J record,
                # so it does not need the grown banks -- packing a second
                # GROWN copy into the J extent halved the remote-name budget
                # and produced the cap-4 "your!" box, live 2026-08-07).
                comp_b, blob_b = L["comp_b"], L["blob_b"]
                if len(comp) > L["cap_a"]:
                    raise ValueError(f"{us_tag}: grown {len(comp):#x} exceeds "
                                     f"{j_tag} space {L['cap_a']:#x}")
                dpk[j_off:j_off + len(comp)] = comp            # copy A (US)
                dpk[us_off:us_off + len(comp_b)] = comp_b      # copy B (J)
                # Record layout carries a FOURTH u32 at +32: the DECOMPRESSED
                # size. The GAME allocates its load buffer from THIS field (not
                # the wp16 stream header): leaving it stale truncated the grown
                # bundle mid-copy (the shop-open freeze).
                struct.pack_into("<III", dpk, rec_off + 24,
                                 j_off, len(comp), len(grown))
                struct.pack_into("<III", dpk, jrec_off + 24,
                                 us_off, len(comp_b), len(blob_b))
                log(f"[extern_bake] {us_tag}: grown {len(comp):#x} @ "
                    f"{j_off:#x} (whole {j_tag} space {L['cap_a']:#x}); "
                    f"{j_tag} re-served vanilla {len(comp_b):#x} @ {us_off:#x}")
                continue
            # legacy layout (retagged-vanilla would not fit the US extent):
            # both grown copies packed into the J extent, as pre-v241.
            grown_j = grown.replace(us_tag[:-3].encode("latin1"),
                                    j_tag[:-3].encode("latin1"))
            comp_j = wp16.compress(grown_j)
            if wp16.decompress(comp_j) != grown_j:
                raise ValueError(f"{j_tag}: wp16 round-trip mismatch")
            b_off = j_off + ((len(comp) + 0xF) & ~0xF)
            if b_off + len(comp_j) > j_off + L["cap_a"]:
                raise ValueError(f"{us_tag}: grown x2 ({len(comp):#x} + "
                                 f"{len(comp_j):#x}) exceeds {j_tag} space "
                                 f"{L['cap_a']:#x}")
            dpk[j_off:j_off + len(comp)] = comp                    # copy A (US)
            dpk[b_off:b_off + len(comp_j)] = comp_j                # copy B (J)
            struct.pack_into("<III", dpk, rec_off + 24,
                             j_off, len(comp), len(grown))
            struct.pack_into("<III", dpk, jrec_off + 24,
                             b_off, len(comp_j), len(grown_j))
            log(f"[extern_bake] {us_tag}: legacy layout, grown x2 "
                f"{len(comp):#x} + {len(comp_j):#x} @ {j_off:#x} "
                f"(space {L['cap_a']:#x})")
        f.seek(dpk_off)
        f.write(dpk)
    return iso_path


_SHOP_BUNDLE = "FM_SHOPUS.PCK"
# Japanese twin, never loaded in a US boot -> dead space the grown US bundle
# can move into (same trick as US_TO_J above). Both tags are 13 chars, so the
# bundle-internal FIF/GIM record names retag in place.
_SHOP_J_BUNDLE = "FM_SHOPJ1.PCK"


def _repack_bundle(blob, cnt, brecs, payloads):
    """Bundle rebuilt from `payloads` (record name -> bytes), file order and
    0x10 alignment preserved, directory offsets/sizes recomputed. The
    record-name-agnostic core of rebuild_bundle."""
    hdr_end = 0x10 + cnt * 36
    order = sorted(brecs, key=lambda r: r[2])
    new_off, cur = {}, (hdr_end + 0xF) & ~0xF
    for nm, _do, _o, _s in order:
        new_off[nm] = cur
        cur = (cur + len(payloads[nm]) + 0xF) & ~0xF
    out = bytearray(cur)
    out[:hdr_end] = blob[:hdr_end]
    struct.pack_into("<I", out, 4, cur)
    for nm, do, _o, _s in brecs:
        no, ns = new_off[nm], len(payloads[nm])
        struct.pack_into("<III", out, do + 24, no, ns, ns)
        out[no:no + ns] = payloads[nm]
    return bytes(out)


def bake_caravan_offer(iso_path, name, descs, log=print):
    """Author the Onrac Caravan's presale row inside FM_SHOPUS.PCK: the shop-UI
    string bank's hardcoded "Faerie's Bottle" entry (SHOP_INDEX.MSG entry 20)
    and the shop-font key-item description behind it (KEY_EXP.MSG entry 14).
    See shop_font for the font and the bank layout. `iso_path` is edited in
    place; nothing else in the bundle is touched.

    The two records are re-authored at whatever size the text needs and the
    bundle is repacked (directory offsets rebuilt, same as rebuild_bundle) and
    recompressed. It goes back in its own dpk slot when it still fits; when the
    authored text pushed it over, the whole bundle RELOCATES into FM_SHOPJ1.PCK
    -- a Japanese shop bundle a US boot never loads -- carrying a second,
    J-retagged copy so the J record keeps serving a valid bundle of its own
    (the discipline bake_names uses for the FM_EXTERN J dead space).

    `descs` = description phrasings LONGEST FIRST (ApClient._ap_desc_cands);
    the first that fits whole is used, so the text bar never shows a sentence
    cut mid-word. `name` is capped at shop_font.CARAVAN_NAME_GLYPHS to stay
    inside the row's name column.

    Returns (baked_name, baked_desc), or None when the bundle/banks/space were
    not found -- the caravan then keeps its vanilla row (cosmetic miss, never a
    corrupt bundle)."""
    from . import shop_font as SFT
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        recs = _dpk_records(dpk)
        if _SHOP_BUNDLE not in recs or _SHOP_J_BUNDLE not in recs:
            log(f"[extern_bake] {_SHOP_BUNDLE} not on disc -- caravan row kept")
            return None
        rec_off, off, size = recs[_SHOP_BUNDLE]
        jrec_off, j_off, j_size = recs[_SHOP_J_BUNDLE]
        blob = wp16.decompress(bytes(dpk[off:off + size]))
        cnt, brecs = _parse_bundle_dir(blob)
        payloads = {n: bytes(blob[o:o + s]) for n, _do, o, s in brecs}
        nb = payloads.get(SFT.NAME_RECORD)
        db = payloads.get(SFT.DESC_RECORD)
        # Guard: only author banks that still read VANILLA at the entry we are
        # about to replace. A modded/foreign disc gets left alone.
        if (nb is None or db is None
                or SFT.bank_entry_text(nb, SFT.CARAVAN_NAME_IDX) != SFT.CARAVAN_NAME
                or SFT.bank_entry_text(db, SFT.CARAVAN_DESC_IDX) != SFT.CARAVAN_DESC):
            log(f"[extern_bake] caravan banks not vanilla in {_SHOP_BUNDLE} "
                f"-- row kept")
            return None
        nm = SFT.decode(SFT.encode_fit(name, SFT.CARAVAN_NAME_GLYPHS))
        cands = [descs] if isinstance(descs, str) else list(descs or [""])
        ds = SFT.decode(SFT.encode(cands[0]))

        def repack(nm, ds):
            out = dict(payloads)
            out[SFT.NAME_RECORD] = SFT.author_bank(nb, SFT.CARAVAN_NAME_IDX, nm)
            out[SFT.DESC_RECORD] = SFT.author_bank(db, SFT.CARAVAN_DESC_IDX, ds)
            grown = _repack_bundle(blob, cnt, brecs, out)
            comp = wp16.compress(grown)
            if wp16.decompress(comp) != grown:
                raise ValueError(f"{_SHOP_BUNDLE}: wp16 round-trip mismatch")
            return grown, comp

        grown, comp = repack(nm, ds)
        if len(comp) <= size:
            dpk[off:off + len(comp)] = comp
            dpk[off + len(comp):off + size] = bytes(size - len(comp))
            # +28 compressed size, +32 DECOMPRESSED size -- the game allocates
            # its load buffer from the LATTER, so a grown bundle that leaves it
            # stale gets truncated mid-copy (the FM_EXTERN shop-open freeze).
            struct.pack_into("<II", dpk, rec_off + 28, len(comp), len(grown))
            where = f"in place @{off:#x}"
        else:
            grown_j = grown.replace(_SHOP_BUNDLE[:-4].encode("latin1"),
                                    _SHOP_J_BUNDLE[:-4].encode("latin1"))
            comp_j = wp16.compress(grown_j)
            if wp16.decompress(comp_j) != grown_j:
                raise ValueError(f"{_SHOP_J_BUNDLE}: wp16 round-trip mismatch")
            b_off = j_off + ((len(comp) + 0xF) & ~0xF)
            if b_off + len(comp_j) > j_off + j_size:
                log(f"[extern_bake] caravan row: {len(comp):#x}+{len(comp_j):#x} "
                    f"exceeds {_SHOP_J_BUNDLE} space {j_size:#x} -- row kept")
                return None
            dpk[j_off:j_off + len(comp)] = comp                    # copy A (US)
            dpk[b_off:b_off + len(comp_j)] = comp_j                # copy B (J)
            struct.pack_into("<III", dpk, rec_off + 24,
                             j_off, len(comp), len(grown))
            struct.pack_into("<III", dpk, jrec_off + 24,
                             b_off, len(comp_j), len(grown_j))
            where = (f"relocated to {_SHOP_J_BUNDLE} @{j_off:#x}; J re-served "
                     f"{len(comp_j):#x} @{b_off:#x} (space {j_size:#x})")
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[extern_bake] caravan row: {nm!r} / {ds!r} "
        f"({size:#x} -> {len(comp):#x}, {where})")
    return nm, ds
