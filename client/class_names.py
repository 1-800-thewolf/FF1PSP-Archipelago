"""Class-name rename (JobScrollBoosts): when a job's scroll is owned, the party
status menu shows a custom name for that job's base + promoted forms.

The 12 class names live in FM_CAMPUS.PCK -> JOB_NAME.MSG, a standalone menu-font
TEXT bank (00 00 00 00 'TEXT', u32 total@+0xC, u32 offset table@+0x10; 12 entries
in class-id order). See the class-name-bank-re memory.

Display length is capped per entry by the entry's offset-table byte length as
CACHED AT MENU-OPEN from the canonical bank -> so long names need the on-disc
bank PADDED (campus_bake.pad_class_bank) to SLOT bytes/entry. The client then
overwrites a padded slot in place when its scroll is owned (no relocation).

Atlas = the PRIMARY menu font (distinct from the item name-bank atlas and the
message font). space=0x00, terminator=0x05.
"""

SLOT = 16                     # padded bytes per class entry (SLOT-1 char budget)
TERM = 0x05
CLASS_COUNT = 12              # 0..5 base jobs, 6..11 promoted

# char -> glyph, primary menu font (derived live from the class + camp-menu banks)
ATLAS = {
    ' ': 0x00,
    'a': 0x06, 'b': 0x15, 'c': 0x10, 'd': 0x0d, 'e': 0x01, 'f': 0x11, 'g': 0x0f,
    'h': 0x0a, 'i': 0x08, 'j': 0x2c, 'k': 0x1f, 'l': 0x0b, 'm': 0x0e, 'n': 0x04,
    'o': 0x03, 'p': 0x16, 'q': 0x3b, 'r': 0x07, 's': 0x09, 't': 0x02, 'u': 0x0c,
    'v': 0x19, 'w': 0x1a, 'x': 0x38, 'y': 0x14, 'z': 0x36,
    'A': 0x1d, 'B': 0x1b, 'C': 0x13, 'D': 0x20, 'E': 0x24, 'F': 0x1e, 'G': 0x23,
    'H': 0x31, 'I': 0x2d, 'K': 0x32, 'L': 0x22, 'M': 0x1c, 'N': 0x29, 'O': 0x28,
    'P': 0x25, 'R': 0x30, 'S': 0x18, 'T': 0x17, 'U': 0x2f, 'W': 0x21, 'X': 0x42,
    'Y': 0x3c, '.': 0x12, '-': 0x35, ':': 0x39,
}

# fj (base job id 0..5) -> (base-form name, promoted-form name). The scroll for
# base job fj gates BOTH; the game renders the entry for the character's current
# class id, so the displayed name switches on promotion automatically.
# SINGLE SOURCE OF TRUTH: __init__.JOB_SCROLL_ITEM_NAMES derives each scroll's AP
# item name from the PROMOTED name here ("<promoted> Scroll"), so editing a name
# below also renames its scroll -- which BREAKS existing seeds/yamls by design.
# Budget = SLOT-1 = 15 chars/name (longest today: "Crimson Wizard" = 14).
CLASS_RENAME = {
    0: ("Blood Warrior",  "Blood Knight"),
    1: ("Stealth Thief",  "Stealth Ninja"),
    2: ("Grand Monk",     "Grand Master"),
    3: ("Crimson Mage",   "Crimson Wizard"),
    4: ("White Student",  "White Cleric"),
    5: ("Apprentice",     "Necrocaster"),
}


def encodable(name):
    """True if every char is in the atlas and the name fits a padded slot."""
    return len(name) <= SLOT - 1 and all(c in ATLAS for c in name)


def encode_slot(name):
    """A full SLOT-byte entry: glyphs + TERM, 0x00 (space) padded to SLOT.
    Raises KeyError on an unencodable char; caller pre-checks with encodable()."""
    body = bytes(ATLAS[c] for c in name[:SLOT - 1]) + bytes([TERM])
    return body + b"\x00" * (SLOT - len(body))


# --- bank identification -------------------------------------------------------
# The bank starts with 00 00 00 00 'TEXT'; entry0 (class 0) sits at base + 0x40.
# Entry0 is NOT a stable anchor: once the Warrior scroll is owned we overwrite it
# with "Blood Warrior", so a rescan keyed on the vanilla name can never re-find the
# bank again (and any scroll obtained AFTER that first rename is silently dropped
# -- live bug, 2026-07-14). So the anchor is the header, and entry0 is validated
# against the vanilla name OR any name we ourselves would have written there.
HEADER = b"\x00\x00\x00\x00TEXT"
ENTRY0_OFF = 0x40
VANILLA_ENTRY0 = "Warrior"


def _entry0_candidates():
    names = [VANILLA_ENTRY0]
    base_nm, promo_nm = CLASS_RENAME[0]
    names += [base_nm, promo_nm]
    return [encode_slot(n)[:len(n) + 1] for n in names if encodable(n)]


def is_bank_entry0(body):
    """True if `body` (>= SLOT bytes read at base + ENTRY0_OFF) is a plausible
    class-name bank entry0: vanilla "Warrior" or one of our class-0 renames."""
    return any(body.startswith(c) for c in _entry0_candidates())
