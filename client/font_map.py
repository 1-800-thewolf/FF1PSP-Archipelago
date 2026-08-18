"""FF1 PSP (ULUS10251) glyph<->char map, cracked from the bonus-dungeon
message bank @0x0994da40. NOT a global font (2026-07-11): every TEXT bank has a
PRIVATE glyph atlas (its bundle carries its own font texture + an ASCII-indexed
atlas-id table in the GIM region -- see ApClient._mapmsg_loop). This table is
simply the CHEST/bonus message bank's atlas, which is stable for that bank, so
the chest-box machinery built on it keeps working."""

# glyph byte -> character (or control token)
FONT = {
    0x00: " ",
    # lowercase
    0x06:"a",0x16:"b",0x0f:"c",0x09:"d",0x01:"e",0x14:"f",0x1d:"g",
    0x0c:"h",0x04:"i",0x0e:"l",0x0a:"m",0x08:"n",0x02:"o",0x10:"p",
    0x19:"q",0x07:"r",0x05:"s",0x03:"t",0x0b:"u",0x18:"v",0x1c:"w",
    0x27:"x",0x13:"y",0x2b:"z",0x1f:"k",
    # UPPERCASE (filled as observed; extend when new caps are seen)
    0x15:"A",0x1b:"D",0x31:"E",0x29:"G",0x23:"I",0x25:"L",0x2a:"M",
    0x34:"N",0x2d:"O",0x1a:"P",0x32:"R",0x22:"S",0x17:"T",0x24:"Y",
    # digits
    0x1e:"0",0x21:"1",0x28:"2",0x33:"3",0x30:"4",0x2f:"5",0x2e:"6",
    # punctuation
    0x2c:"'",0x26:",",0x11:".",0x12:"!",0x20:"?",
}
# control codes (not printable glyphs)
CTRL = {0x39:"{NAME}", 0x38:"{CLR}", 0x36:"\n", 0x0d:"{END}"}

# char -> glyph (for ENCODING AP names). Only the reversible printable set.
ENC = {c: g for g, c in FONT.items()}

def decode(data: bytes) -> str:
    out = []
    for b in data:
        if b in CTRL: out.append(CTRL[b])
        elif b in FONT: out.append(FONT[b])
        else: out.append(f"[{b:02x}]")
    return "".join(out)

def encodable(s: str):
    """Return list of unencodable chars in s (empty => fully encodable)."""
    return [c for c in s if c not in ENC]

def encode(s: str) -> bytes:
    """Encode a string to glyph bytes (no terminator). TOTAL: delegates to the safe
    encode_name (uppercase->lowercase fallback, unrenderable chars dropped) so it can
    NEVER raise. A single KeyError in a glyph encoder aborts the whole ISO bake ->
    raw-ISO boot -> all runtime features dead (live 2026-07-06 KeyError('Y'); see the
    name-encode-keyerror memory). No raising glyph path exists by design; the
    test_client_policies P3 check bans reintroducing raw ENC[c] subscripts."""
    return encode_name(s)

TERM = 0x0d
NEWLINE = 0x36

def encode_name(s: str) -> bytes:
    """SAFE encode for AP names: emit ONLY validated printable glyphs (never a
    control/opcode byte). Unknown-cap letters fall back to their lowercase glyph
    (always known); anything still unencodable is dropped. No terminator appended.

    This is the guarantee that the injector can't crash the text VM: every output
    byte is a known glyph in FONT, so 0x3a..0x5f opcode bytes are never emitted."""
    out = bytearray()
    for c in s:
        if c in ENC:
            out.append(ENC[c])
        elif c.isupper() and c.lower() in ENC:   # missing cap -> lowercase glyph
            out.append(ENC[c.lower()])
        elif c == "-" and "-" not in ENC:         # dash unknown -> space
            out.append(0x00)
        # else: drop silently (unrenderable symbol)
    return bytes(out)


def encode_fit(s: str, max_glyphs: int) -> bytes:
    """Sanitized glyph bytes truncated to max_glyphs (no terminator). Length limits
    are out of scope -> we just cap; caller appends the 0x0d terminator."""
    return encode_name(s)[:max_glyphs]


def encode_wrap(s: str, max_glyphs: int, line_width: int = 28) -> bytes:
    """Sanitized glyphs word-wrapped onto TWO box lines (0x36 newline), each line
    clipped to line_width, total clipped to max_glyphs (no terminator). The chest
    reward box renders exactly two lines; anything past line 2 is dropped."""
    raw = encode_name(s)
    if len(raw) <= line_width:
        return raw[:max_glyphs]
    cut = raw.rfind(0x00, 1, line_width + 1)   # last space on line 1
    if cut > 0:
        out = raw[:cut] + bytes([NEWLINE]) + raw[cut + 1:]   # space -> newline
    else:
        out = raw[:line_width] + bytes([NEWLINE]) + raw[line_width:]
    nl = out.index(NEWLINE)
    return out[:nl + 1 + line_width][:max_glyphs]
