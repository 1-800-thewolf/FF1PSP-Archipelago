"""Extended item NAME/DESC bank builder (B2 of the spell_tomes feature).

build_extended_banks() grows the vanilla 43-entry items banks with one entry
per spell slot (copied VERBATIM from the magic name/desc banks, school icon
included, honoring the seed's magic_text remap) and optionally with remote AP
item names. It is a pure byte builder: the ON-DISC bakers consume it
(extern_bake.plan_remote / bake_names write the result into dpk dead space and
iso_patcher.apply_spell_tomes repoints the cat1 id-array literal at a grown
bss copy).

There is deliberately NO runtime (live-RAM) authoring path anymore: the game
re-derives its heap bank-pointer registry from the on-disc banks on every menu
load, so runtime repointing flapped endlessly (see extern_bake's module note).
The old setup_tome_names live path was removed with it.
"""
import struct

from . import name_banks as NB

ITEM_ENTRIES = 43                # vanilla item NAME/DESC entries (no tome block)
BANK_ENTRIES = ITEM_ENTRIES + 64  # string ids 0..106 (with the tome block)
SCRATCH_SIZE = 0x2200            # keep == iso_patcher._BANK_SCRATCH_SZ (v167)


def _bank_entries(bank):
    p, offs = bank["payload"], bank["entry_offsets"]
    return [p[offs[i]:offs[i + 1] if i + 1 < len(offs) else len(p)]
            for i in range(len(offs))]


def _desc_entries(dbank):
    # DESC payload = offset table + entries; offsets are bank-relative
    p, offs, first = dbank["payload"], dbank["entry_offsets"], dbank["first"]
    ends = offs[1:] + [len(p) + 0x10]
    return [p[o - 0x10:e - 0x10] for o, e in zip(offs, ends)]


def items_desc_bank(item_descs):
    """The 43-entry ITEMS desc bank with `item_descs` applied and its offset
    table rebuilt -- same dict shape as NB.DESC_BANKS["items"] (payload/count/
    entry_offsets/first).

    The client's shop-desc patch searches RAM for the entries region of this
    bank and authors AP offers IN PLACE at its offsets. Once slot_magic rewrites
    the Soma/Ether entries on disc (build_extended_banks, same item_descs), the
    vanilla bank is no longer that signature and no longer those offsets, so the
    shop patch silently stops locating (Dry Ether kept its placeholder's desc,
    live 2026-08-08). Callers must use THIS bank as the baseline whenever the
    bake applied item_descs -- exactly the blood_magic mirroring next door."""
    ents = list(_desc_entries(NB.DESC_BANKS["items"]))
    for gid, text in (item_descs or {}).items():
        idx = gid - 1
        if not 0 <= idx < len(ents):
            raise ValueError(f"item_descs: gid {gid} out of range")
        # narrow target-select box: rewrap, exactly like the tome descs
        # (see _rewrap_desc). Vanilla item descs are short enough to fit
        # unwrapped; our replacements are not ("Restores 1 of each spell
        # slot" spilled off the right edge, live 2026-08-17).
        ents[idx] = _rewrap_desc(_menu_bytes(text) + ents[idx][-1:])
    count = len(ents)
    first = 0x10 + count * 4
    offs, p = [], first
    for e in ents:
        offs.append(p)
        p += len(e)
    payload = (b"".join(o.to_bytes(4, "little") for o in offs)
               + b"".join(ents))
    return {"payload": payload, "count": count, "entry_offsets": offs,
            "first": first}


def _menu_bytes(s):
    """Encode s to the MENU-page glyph bytes used by these TEXT banks (no
    terminator). ROBUST: a char with no menu glyph falls back uppercase->lowercase,
    else is dropped -- never raises, because a single KeyError in a glyph encoder
    aborts the whole ISO bake (live 2026-07-06, 'Odyssey' when MENU_FONT was still
    missing 'Q'/'Y'). Mirrors name_banks.menu_encode_fit's fallback.

    One page, always: extern_bake permutes the 18 px box face onto the menu page
    at bake time, so there is no second encoding to choose between."""
    out = []
    for c in s:
        if c in NB.MENU_ENC:
            out.append(NB.MENU_ENC[c])
        elif c.isupper() and c.lower() in NB.MENU_ENC:
            out.append(NB.MENU_ENC[c.lower()])
        # else: unencodable in the menu font -> drop (cosmetic only; the exact
        # name is still in the AP client log)
    return bytes(out)


# Chest-box-SAFE character set = everything the menu face can draw, which as of
# the 2026-08-08 .FIF/.GIM read is the WHOLE printable ASCII range (see
# name_banks' PAGE block). Both former restrictions are gone:
#   * letters only -- digits and symbols used to be dropped for fear of the
#     control codes 0x38 {CLR} / 0x39 {NAME}. Those thresholds are PER BANK
#     (control id >= that bank's glyph_count): the box TEMPLATE bank USEVMCMN
#     has 53 glyphs, so 0x38/0x39 really are controls THERE -- which is what the
#     2026-07-03 digit probe froze -- but the item NAME bank this text lives in
#     has 107, so no byte we can emit (max 0x6a) is ever a control in it.
#   * 'Q'/'Y'/'q' downcasing -- MENU_FONT simply had not learned those ids.
# The real item name is still exact in the AP client log either way.
_BOX_SAFE = set(NB.MENU_ENC) | {" "}

# Desc-box line break = the 2-byte control pair 0xc2 0x8d (vanilla magic descs
# use it: "Restores<NL>a little HP<NL>to one ally."). The TARGET-SELECT desc box
# is far NARROWER than the item-menu one, and most magic descs ship unwrapped
# (33-39 glyphs) -- plus " Level X" makes them longer -- so they spilled off the
# right edge when picking a party member. Rewrapping to the narrow width fixes
# the target box and is harmless in the wide item box (just more, shorter lines).
DESC_BREAK_CTRL = b"\xc2\x8d"   # raw line-break pair
# A SPACE GLYPH must precede it: the pair is ZERO-WIDTH in the WIDE
# item-menu box (which draws the whole entry on one line), so a bare break
# renders "Transportsparty out ofdungeons" there -- LIVE 2026-07-29, and it
# is a vanilla bug on Exit/Heal. With the space: the wide box keeps its word
# gap, the narrow target-select box breaks with a harmless trailing space.
DESC_NEWLINE = bytes([NB.MENU_ENC[" "]]) + DESC_BREAK_CTRL
DESC_WRAP_GLYPHS = 15            # conservative: font is proportional and the
                                 # widest vanilla wrapped line is 14 glyphs
                                 # ("a little HP to"); box holds 4+ lines.


def _rewrap_desc(entry, width=DESC_WRAP_GLYPHS):
    """Re-wrap a menu-font desc entry (glyphs + TERM) onto `width`-glyph lines
    using DESC_NEWLINE. Existing breaks are treated as word separators, so an
    already-wrapped entry is re-flowed rather than double-broken. The trailing
    TERM byte is preserved."""
    body, term = bytes(entry[:-1]), bytes(entry[-1:])
    body = body.replace(DESC_BREAK_CTRL, bytes([NB.MENU_ENC[" "]]))
    sp = NB.MENU_ENC[" "]
    words = [w for w in body.split(bytes([sp])) if w]
    lines, cur = [], b""
    for w in words:
        cand = w if not cur else cur + bytes([sp]) + w
        if cur and len(cand) > width:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return DESC_NEWLINE.join(lines) + term


def _cap_menu(s, cap):
    """Sanitize an arbitrary AP item name to BOX-SAFE, capped chars: keep every
    glyph the menu face can draw (see _BOX_SAFE), drop the rest, collapse runs of
    spaces, hard-truncate to `cap`."""
    out = []
    prev_sp = False
    for c in s:
        if c not in _BOX_SAFE:
            continue
        if c == " ":
            if prev_sp:
                continue
            prev_sp = True
        else:
            prev_sp = False
        out.append(c)
    return "".join(out).strip()[:cap] or "AP Item"


# --- remote chest-box name rendering (recipient-preserving) -----------------
# A remote name is a (who, item) PAIR: who = "your" / "<player>'s" (may be
# None/"" for a pre-joined legacy string). The RECIPIENT is the part a
# multiworld player can least afford to lose ("your!" live 2026-08-07: a
# blind head-truncate at cap 4 kept only the recipient and ate the item --
# with real player names the same truncate eats the RECIPIENT instead), so
# capping always applies to the ITEM portion and leaves `who` whole.
REMOTE_TOTAL_GLYPHS = 30   # one chest-box line; also the dyn slot width
# Recipient budget ("your", "Playername's"). 18 = an Archipelago slot name at
# its 16-char maximum plus the possessive, so a legal name is never clipped.
# This is a CAP, not a reservation: render_remote gives the item everything the
# recipient does not use, so short names still get a long item. It was 14 while
# digits were dropped from names, which hid the problem -- "DragoniteZ3OW's"
# sanitized to a 14-glyph "DragoniteZOW's" and fit by luck; with the digit kept
# it clipped to "DragoniteZ3OW'" (live report 2026-08-08).
REMOTE_WHO_GLYPHS = 18
REMOTE_ITEM_FLOOR = 6      # below this the item reads as noise -> generic rung


def _cap_who(who, cap=REMOTE_WHO_GLYPHS):
    """Sanitize+cap a recipient, keeping the possessive. A bare hard truncate
    turns "Averylongname's" into "Averylongname'", which reads as a typo rather
    than a shortened name -- the same failure class as the v241 "your!" box."""
    full = _cap_menu(who, len(who) + 2)
    out = _cap_menu(who, cap)
    if full.endswith("'s") and not out.endswith("'s"):
        out = out[:max(1, cap - 2)].rstrip("'") + "'s"
    return out


def render_remote(who, item, item_cap):
    """One box-safe remote name from a (who, item) pair with the item capped
    to `item_cap` glyphs and the recipient kept whole (its own generous cap).
    Total is bounded by REMOTE_TOTAL_GLYPHS, again shrinking only the item."""
    it = _cap_menu(item or "", item_cap)
    if not who:
        return it[:REMOTE_TOTAL_GLYPHS]
    w = _cap_who(who)
    room = max(REMOTE_ITEM_FLOOR // 2, REMOTE_TOTAL_GLYPHS - len(w) - 1)
    return f"{w} {it[:room]}".strip()[:REMOTE_TOTAL_GLYPHS]


def remote_rungs(pairs, cap0=32):
    """Yield (label, names) candidate lists for `pairs`, most faithful first.
    EVERY rung keeps the recipient and none is empty -- the never-blank
    invariant (mirrors test_keybox_fit's rule for the map obtain box: a blank
    or recipient-less entry reads as a broken chest, not a shortened name)."""
    for cap in range(cap0, REMOTE_ITEM_FLOOR - 1, -2):
        yield (f"item cap {cap}",
               [render_remote(w, i, cap) for w, i in pairs])
    # item unreadable at this budget: say WHOSE AP item it is, drop the what
    yield ("generic (recipient + AP item)",
           [render_remote(w, "AP item", 8) for w, i in pairs])
    # absolute floor: still never blank
    yield ("generic (AP item)", ["AP item"] * len(pairs))


def normalize_remote(remote_names):
    """Canonical [(who, item)] from a bake payload: entries may be [who, item]
    pairs (current clients) or pre-joined strings (older cached bakes)."""
    out = []
    for nm in remote_names or []:
        if isinstance(nm, (list, tuple)) and len(nm) == 2:
            out.append((nm[0] or "", str(nm[1])))
        else:
            out.append(("", str(nm)))
    return out


# --- dynamic bonus-chest name slots (runtime-authored wide entries) ---------
# Two ping-pong entries appended AFTER the remote block. Dynamic (procedural)
# bonus-dungeon chests used to bake one deduped entry per (dungeon, ordinal) --
# up to 220 entries on a 4x-bonus-dungeon seed, which is exactly what starved
# the shared budget down to cap 4 ("your!"). The BDC1 mailbox arms ONE next-sid
# per tick anyway, so the client now authors the NEXT chest's name into one of
# these two fixed slots at runtime (adjacent -> ping-pong so a still-open box
# is never rewritten under the player) and arms that slot's sid.
DYN_SLOTS = 2
DYN_SLOT_GLYPHS = REMOTE_TOTAL_GLYPHS   # writable glyph budget per slot
MENU_TERM = 0x06                        # menu-bank entry terminator
_DYN_ICON = bytes([0xC2, 0x88])         # same item icon every NAME entry needs


def dyn_slot_payload(name=""):
    """The writable body of one dyn slot (after the icon): `name` glyphs +
    TERM fill, ALWAYS exactly DYN_SLOT_GLYPHS+1 bytes so the client can rewrite
    it in place. Baked with the benign sentinel below.

    The fill is TERM, never the space glyph. Every renderer here draws
    off[i+1]-off[i] bytes and IGNORES the TERM (see name_banks.key_menu_encode),
    so a space-padded slot draws its FULL 30-glyph width -- the chest box's
    "{NAME}!" template then puts the "!" 20 blanks away from the item ("your
    Staff          !", Whisperwind Cove live 2026-08-15). Baked remote entries
    never showed this because they are exact-length; only the fixed-width dyn
    slots pad. TERM bytes draw nothing, so the box closes up to "your Staff!".

    The client authors these at runtime through ONE DataPatch, which writes the
    same bytes into every resident copy of the bank -- it cannot encode per
    bundle or per surface. That is only sound because the bake leaves exactly
    one glyph page in the image (see name_banks' PAGE block)."""
    g = _menu_bytes(_cap_menu(name, DYN_SLOT_GLYPHS) if name else "")
    g = g[:DYN_SLOT_GLYPHS]
    pad = bytes([MENU_TERM]) * (DYN_SLOT_GLYPHS - len(g))
    return g + bytes([MENU_TERM]) + pad


def dyn_slot_entry(is_desc, name="AP item"):
    """One full baked dyn-slot bank entry. NAME entries carry the icon prefix
    (the box renderer eats 2 bytes) + a fixed-size payload; DESC entries are a
    bare TERM like the remote block (never menu-visible). The default 'AP item'
    doubles as the RAM locate signature AND as what a raced/unauthored slot
    renders -- honest, never a wrong-looking vanilla name."""
    if is_desc:
        return bytes([MENU_TERM])
    return _DYN_ICON + dyn_slot_payload(name)


def build_extended_banks(remap=None, levels=None, remote=None, tomes=True,
                         item_descs=None, dyn_slots=0):
    """remap[slot] = vanilla name index of the spell in magic slot `slot`
    (magic_text u16 lo). Returns (name_bank, desc_bank) bytes.

    tomes=True  (spell_tomes ON): 107-entry TEXT banks = 43 vanilla item entries
                + 64 tome entries (verbatim magic name / desc entries, per-slot),
                + R remote entries at string ids 107..107+R-1.
    tomes=False (spell_tomes OFF): 43 vanilla item entries + R remote entries at
                string ids 43..43+R-1 (no tome block). This lets remote chest-box
                names bake without the spell_tomes feature -- the box detour
                stores the string id directly, so it never goes through the
                bounded (cat,id) getter and needs no ELF-side bound widening.

    item_descs (optional) = {consumable gid: replacement description text} for
    vanilla items whose behaviour a feature changed (slot_magic rewrites the
    Soma Drop). Bank index is gid - 1 (index 37 IS the Soma Drop -- the banks
    are 0-based over the 1-based item ids), and the entry keeps the bank's own
    TERM glyph.

    Every entry is MENU page. extern_bake permutes the 18 px box face onto that
    page (name_banks' PAGE block), so one encoding serves the item menu, the key
    menu and the chest reward box alike.

    levels[slot] (optional, tomes only) = the spell's shuffled level
    (magic_info+9). When given, " Level X" is appended to each tome's DESCRIPTION
    (before its TERM glyph) so a randomized-level game shows the real gate
    level."""
    if tomes:
        assert remap is not None and len(remap) == 64
        assert levels is None or len(levels) == 64
    # Keep magic NAME entries verbatim -- their 2-byte school-icon prefix
    # (c2 86 white / c2 87 black) is the SAME icon family vanilla item names use
    # (c2 88/89), so the item renderers handle it. Crucially the target-select
    # name box SKIPS the 2-byte icon; a stripped tome would lose its first glyph
    # ("Cure" -> "ure") and drop the target cursor. (The earlier menu freeze was
    # the header +8 entry count, not the icon.)
    m_names = _bank_entries(NB.MAGIC_BANKS["names"])
    m_descs = _bank_entries(NB.MAGIC_BANKS["descs"])
    out = []
    for hdr, vanilla in (
            (NB.ITEMS_BANK_HDRS["name"], _bank_entries(NB.BANKS["items"])),
            (NB.ITEMS_BANK_HDRS["desc"], _desc_entries(NB.DESC_BANKS["items"]))):
        is_desc = hdr is NB.ITEMS_BANK_HDRS["desc"]
        tome = m_descs if is_desc else m_names
        if is_desc and item_descs:
            vanilla = list(vanilla)
            for gid, text in item_descs.items():
                idx = gid - 1                      # banks are 0-based over 1..43
                if not 0 <= idx < len(vanilla):
                    raise ValueError(f"item_descs: gid {gid} out of range")
                term = vanilla[idx][-1:]
                # narrow target-select box: rewrap (see _rewrap_desc, and
                # items_desc_bank which MUST stay byte-identical to this)
                vanilla[idx] = _rewrap_desc(_menu_bytes(text) + term)
        tome_entries = []
        if tomes:
            for s in range(64):
                e = tome[remap[s]]
                if is_desc and levels and levels[s]:
                    # append " Level X" before the entry's TERM glyph (last byte)
                    e = e[:-1] + _menu_bytes(f" Level {levels[s]}") + e[-1:]
                if is_desc:
                    # narrow target-select box: rewrap (see _rewrap_desc)
                    e = _rewrap_desc(e)
                tome_entries.append(e)
        # Remote AP-item entries (on-disc bake only): NAME = the sanitized AP
        # name; DESC = a single TERM glyph (remote items are never menu-usable,
        # so the description box is unreachable -- keep them 1 byte to save the
        # tight FM_EXTERN12 J-dead-space budget). String ids run base..base+R-1
        # where base = 107 (tomes on) or 43 (tomes off).
        remote_entries = []
        if remote:
            term = tome[0][-1:]                       # TERM glyph from any entry
            # NAME entries need the 2-byte item icon prefix (c2 88) that every
            # vanilla item name carries -- the chest box renderer consumes it, so
            # without it the name's FIRST glyph is eaten ("Player" -> "layer").
            icon = bytes([0xC2, 0x88])
            for nm in remote:
                remote_entries.append((term if is_desc
                                       else icon + _menu_bytes(nm) + term))
        # dyn_slots wide ping-pong entries at sids base+R..base+R+dyn_slots-1
        # (see dyn_slot_entry): fixed-size, sentinel-baked, runtime-authored.
        for _ in range(dyn_slots):
            remote_entries.append(dyn_slot_entry(is_desc))
        entries = list(vanilla) + tome_entries + remote_entries
        base = BANK_ENTRIES if tomes else ITEM_ENTRIES
        n_entries = base + len(remote_entries)
        assert len(entries) == n_entries
        first = 0x10 + n_entries * 4
        offs, p = [], first
        for e in entries:
            offs.append(p)
            p += len(e)
        bank = bytearray(hdr)
        struct.pack_into("<I", bank, 0xC, p)          # total size
        # header +8 packs (entry_count << 8) | dim2 (0x2b01 vanilla = 43 << 8 |
        # 1). The item-menu renderer bounds-checks the entry index against THIS
        # count (not the offset table) and traps in an infinite loop
        # (@0x08914990) on an out-of-range index -- so it must say 107, or the
        # first tome entry (index 43) hangs the menu.
        cnt_field = struct.unpack_from("<I", bank, 8)[0]
        struct.pack_into("<I", bank, 8, (n_entries << 8) | (cnt_field & 0xFF))
        bank += struct.pack(f"<{n_entries}I", *offs)
        bank += b"".join(entries)
        assert len(bank) == p
        out.append(bytes(bank))
    # The bss SCRATCH budget bounds only the RUNTIME client-built path (tomes,
    # no remote). The on-disc extern_bake path (remote != None) writes into the
    # dpk J dead space instead and is bounded by extern_bake's fit check.
    if not remote and not dyn_slots:
        assert len(out[0]) + len(out[1]) <= SCRATCH_SIZE, \
            (len(out[0]), len(out[1]))
    return out[0], out[1]
