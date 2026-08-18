"""The SHOP-UI text font + the two caravan text banks inside FM_SHOPUS.PCK.

The shop window draws its own strings through a THIRD glyph table -- not the
menu font (name_banks.MENU_ENC), not the map/message font (font_map.FONT), not
the battle font (battle_font.BATTLE_GLYPH). Solved live 2026-08-01 off the
resident caravan banks; every id was read back off-screen with a poke probe.

Two banks matter for the Onrac Caravan's Bottled Faerie sale (AP location
"Onrac - Caravan"):
  * SHOP UI bank (21 entries): "Weapons"/"Buy"/"Stock"/... plus entry 20,
    "Faerie's Bottle" -- a HARDCODED shop-UI string. The caravan's presale line
    does NOT read KEY_NAME.MSG (which says "Bottled Faerie") nor any item
    table, which is why the whole shop name-bank pipeline never touched it.
  * KEY-ITEM DESC bank (36 entries, shop font): entry 14 (key id 15) =
    "A bottle containing a faerie." -- the bottom text bar.

Both are standalone TEXT containers (0x10 header + u32 offset table + packed
entries + TERM 0x08). author_bank() rebuilds a bank around one replaced entry
(growing/shrinking it; extern_bake repacks the bundle around the new size).

FONT LIMITS: the table ends at 0x35. There are NO DIGITS and no capital
G J O Q U V X Z; encode() falls back to the lowercase glyph for a missing
capital and drops anything else (0x36/0x37 draw nothing, 0x38 terminates).
"""
import struct

GLYPH = {
    0x00: ' ', 0x01: 'e', 0x02: 'o', 0x03: 'n', 0x04: 't', 0x05: 'a',
    0x06: 'i', 0x07: 'r', 0x09: 's', 0x0a: 'l', 0x0b: 'h', 0x0c: 'u',
    0x0d: 'c', 0x0e: '.', 0x0f: 'd', 0x10: 'g', 0x11: 'm', 0x12: 'y',
    0x13: 'p', 0x14: 'f', 0x15: 'k', 0x16: 'w', 0x17: 'A', 0x18: 'b',
    0x19: 'W', 0x1a: 'v', 0x1b: "'", 0x1c: '?', 0x1d: 'M', 0x1e: 'C',
    0x1f: 'Y', 0x20: 'T', 0x21: 'S', 0x22: 'B', 0x23: '!', 0x24: 'x',
    0x25: 'L', 0x26: 'z', 0x27: 'H', 0x28: 'F', 0x29: 'I', 0x2a: 'E',
    0x2b: 'q', 0x2c: 'R', 0x2d: 'K', 0x2e: 'N', 0x2f: 'j', 0x30: '-',
    0x31: ',', 0x32: 'D', 0x35: 'P',
}
ENC = {c: g for g, c in GLYPH.items()}
TERM = 0x08

# Vanilla text the two banks are FOUND by (content signature -- the bundle's
# bank offsets are not stable across regions/builds, so never hardcode them).
CARAVAN_NAME = "Faerie's Bottle"
CARAVAN_NAME_IDX = 20
CARAVAN_DESC = "A bottle containing a faerie."
CARAVAN_DESC_IDX = 14
# The banks are bundle records, addressed by name (see extern_bake).
NAME_RECORD = "SHOP_INDEX.MSG"      # shop-UI strings, entry 20 = the caravan row
DESC_RECORD = "KEY_EXP.MSG"         # key-item descriptions, shop font
# Shop row width: the name column runs out well before the price column
# ("Faerie's Bottle" is 15). Keep authored names inside it.
CARAVAN_NAME_GLYPHS = 20


def decode(b):
    return "".join(GLYPH.get(x) or f"<{x:02x}>" for x in b)


def encode(s):
    """Glyphs for `s`. Missing capitals fall back to their lowercase glyph
    (the font has no G J O Q U V X Z); '_' becomes a space; every other
    unencodable character (digits included) is DROPPED."""
    out = bytearray()
    for c in s:
        g = ENC.get(c)
        if g is None and c.isupper():
            g = ENC.get(c.lower())
        if g is None and c in '_\t':
            g = ENC[' ']
        if g is not None:
            out.append(g)
    return bytes(out)


def encode_fit(s, budget):
    """encode(), truncated to `budget` glyphs (no terminator)."""
    return encode(s)[:budget]


def entries(blob, base, cnt, total):
    offs = struct.unpack_from(f"<{cnt}I", blob, base + 0x10)
    ends = list(offs[1:]) + [total]
    return [bytes(blob[base + a:base + b]) for a, b in zip(offs, ends)]


def bank_entries(bank):
    """Entries of a standalone bank payload (a bundle record, so offset 0 is
    the container's own 0x10 header)."""
    cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
    total = struct.unpack_from("<I", bank, 0xC)[0]
    return entries(bank, 0, cnt, total)


def author_bank(bank, idx, text):
    """Bank payload with entry `idx` replaced by `text` -- offsets and total
    rebuilt, the bank GROWN or shrunk as needed (callers repack the bundle, so
    there is no fixed region to stay inside). Returns the vanilla bytes
    unchanged when `idx` is out of range."""
    cnt = struct.unpack_from("<I", bank, 8)[0] >> 8
    if not 0 <= idx < cnt:
        return bytes(bank)
    ents = bank_entries(bank)
    ents[idx] = encode(text) + bytes([TERM])
    first = 0x10 + cnt * 4
    out = bytearray(bank[:0x10])
    offs, p = [], first
    for e in ents:
        offs.append(p)
        p += len(e)
    struct.pack_into("<I", out, 0xC, p)
    return bytes(out) + struct.pack(f"<{cnt}I", *offs) + b"".join(ents)


def bank_entry_text(bank, idx):
    """Decoded text of entry `idx` (terminator dropped)."""
    e = bank_entries(bank)[idx]
    return decode(e[:-1] if e and e[-1] == TERM else e)
