"""FF1 PSP BATTLE-message font atlas (FONT_BATTLEUS.PC -> BATTLE_MSG.MSG +
MONSTER_NAME.MSG). Cracked 2026-07-11 with ZERO conflicts by aligning all 203
monster names + known battle-message templates ("Earned {N} EXP.",
"Obtained {N} gil.", "Preemptive strike", "The party was defeated.", ...).

This is the atlas the in-battle message box primitive 0x88190b0 renders -- it is
NEITHER the chest message font (font_map) NOR the menu font (name_banks); both
render BLANK in the battle box (proven by boot-tests, see battle-message-box-re).

Glyph id -> char. Terminator 0x00.
Substitution TOKENS (engine expands at render, NOT plain glyphs):
  0x47 = {N}    numeric value (e.g. "Earned {N} EXP.")
  0x46 = {NAME} a name string WITH its icon -- context sets the source:
         item name+icon for "Obtained {NAME}." (entry14), character name for
         "{NAME} gained a level!" (entry6), enemy name for "The {NAME} ran away."
         Use 0x46 to get the stolen item's name+icon in our message.
Only lowercase 'j' has no glyph in this atlas (never appears in monster names /
battle messages); everything else incl. '!' (0x2e) and A-Z/a-z is present.
"""

BATTLE_GLYPH = {
    0x01: 'e', 0x02: 'a', 0x03: 'r', 0x04: 'o', 0x05: 'i', 0x06: 'n', 0x07: ' ',
    0x08: 'l', 0x09: 't', 0x0a: 'h', 0x0b: 's', 0x0c: 'd', 0x0d: 'g', 0x0e: 'c',
    0x0f: 'm', 0x10: 'y', 0x11: 'S', 0x12: 'u', 0x13: 'G', 0x14: 'k', 0x15: 'D',
    0x16: 'W', 0x17: 'C', 0x18: 'E', 0x19: 'P', 0x1a: 'B', 0x1b: 'z', 0x1c: 'M',
    0x1d: 'p', 0x1e: 'b', 0x1f: 'T', 0x20: 'A', 0x21: 'v', 0x22: 'H', 0x23: 'f',
    0x24: 'R', 0x25: '.', 0x26: 'O', 0x27: 'F', 0x28: 'w', 0x29: 'N', 0x2a: 'L',
    0x2b: 'K', 0x2c: 'I', 0x2d: 'q', 0x2e: '!', 0x2f: 'Z', 0x30: 'Y', 0x31: '-',
    0x32: 'x', 0x33: 'V', 0x34: 'U', 0x39: 'X', 0x3a: 'J', 0x3b: 'Q',
}
TERM = 0x00
NUM = 0x47                       # {N}    numeric-substitution token
NAME = 0x46                      # {NAME} name+icon substitution token (see docstring)
BATTLE_ENC = {c: g for g, c in BATTLE_GLYPH.items()}

# --- lowercase 'j': UNVERIFIED GUESS ------------------------------------------
# 'j' is the one letter the 2026-07-11 solve never pinned down: it appears in no
# monster name and no BATTLE_MSG template, so nothing constrained it. The ids are
# ordered by frequency, not alphabetically, so it cannot be inferred by position
# -- it is simply one of the unused ids (0x35-0x38, 0x3c-0x40), which is exactly
# where the other rare letters live (0x39 'X', 0x3a 'J', 0x3b 'Q').
#
# This matters because encode() falls back to swapcase for an unknown letter, so
# "Ninja" silently rendered as "NinJa". Registering a candidate at least makes the
# guess explicit and testable in-game rather than silently wrong.
#
# TO VERIFY: trigger a Ninja steal and read the box. If the glyph is wrong, try
# the next candidate in BATTLE_J_CANDIDATES and re-test. Update this comment with
# the confirmed id (and add it to BATTLE_GLYPH properly) once one is proven.
BATTLE_J_CANDIDATES = (0x35, 0x36, 0x37, 0x38, 0x3C, 0x3D, 0x3E, 0x3F, 0x40)
BATTLE_J_GUESS = BATTLE_J_CANDIDATES[0]
BATTLE_ENC.setdefault("j", BATTLE_J_GUESS)


def encode_with_name(prefix, suffix):
    """Encode `prefix` + {NAME}(0x46 icon+name token) + `suffix` + terminator.
    Use for "Your Thief sees an extra {NAME} to steal!" so the engine inserts the
    stolen item's name AND icon (source must be set to our item before trigger)."""
    return encode(prefix, term=False) + bytes([NAME]) + encode(suffix, term=True)


def encode(s, term=True):
    """ASCII -> battle glyph bytes. Unknown glyph -> swapcase fallback -> dropped
    (so it can never emit a control/opcode byte)."""
    out = bytearray()
    for ch in s:
        g = BATTLE_ENC.get(ch)
        if g is None and ch.isalpha():
            g = BATTLE_ENC.get(ch.swapcase())
        if g is not None:
            out.append(g)
    if term:
        out.append(TERM)
    return bytes(out)


def decode(b):
    return ''.join(BATTLE_GLYPH.get(x, '{%02x}' % x) for x in b)
