"""The trap roster -- ONE table, everything else derived.

A trap is nothing but an effect id handed to the on-disc round-zero cave, which
casts it as a real monster ability with the game's own animation, message and
targeting. So the only thing worth writing down is which ordinal means which
effect; the item name, the target mode and the whole audit trail come from
ability_data, which is generated from the ISO.

WHAT THIS REPLACED, and why the shape matters (2026-09-01). The roster used to
be three hand-copied dicts: TRAP_ITEMS in ff1psp/__init__.py, _TRAP_NAMES plus
_TRAP_EFFECT in the client, and a third copy frozen in test_traps. Adding one
trap meant four edits agreeing on a name string, and the rule that keeps names
honest -- an AP item must be called what the battle box prints -- was enforced
by a comment. Now the name IS the bank string by construction, the client
imports these tables instead of restating them, and the invariants below are
asserted at import so a bad roster fails the moment anything loads it.

Pure data + asserts, no imports beyond ability_data -- the client, the world and
the tests all import it, and none of them may need the Archipelago runtime for
it (test_traps imports this module directly; only the AP-facing world needs the
stub harness).

KIND ORDINALS ARE AP ITEM IDS. They are frozen forever: ID.trap_item_id(kind)
goes out in a datapackage, so renumbering makes an in-flight seed built on an
older apworld deliver the WRONG trap. Retiring a kind leaves a permanent GAP --
never reuse the number, and never register the retired name anywhere.
"""
from .ability_data import ABILITY, SPELL_LAST

# kind ordinal -> magic_info effect id. THE table. Everything a trap is comes
# from the ability it names; nothing else about a kind is written down.
TRAP_EFFECTS = {
    # --- player spells (effects 0x01-0x40) -----------------------------------
    0: 0x36,     # Death     -- single target
    2: 0x56,     # Ink       -- party-wide blind. NB "Blind" is the PLAYER spell
                 #              (id 0x3C); the old kind name pointed at it by
                 #              mistake, which is exactly the class of error the
                 #              derived-name rule now makes impossible.
    3: 0x06,     # Silence   -- party-wide
    4: 0x57,     # Poison Darts -- party-wide spray (Manticore's)
    7: 0x22,     # Sleep     -- party-wide
    8: 0x28,     # Slow      -- party-wide speed debuff
    9: 0x21,     # Fire      -- single
    10: 0x24,    # Thunder   -- single
    11: 0x25,    # Blizzard  -- single
    12: 0x3A,    # Break     -- single, STONE (persists out of battle)
    13: 0x20,    # Dispel    -- single, strips party buffs
    14: 0x2A,    # Hold      -- single, paralyze
    15: 0x34,    # Slowra    -- single
    16: 0x17,    # Protera   -- buffs the ENEMY side
    17: 0x18,    # Invisira  -- buffs the ENEMY side
    18: 0x0B,    # NulBlaze  -- fire ward on the ENEMY side
    19: 0x27,    # Temper    -- enemy attack up
    20: 0x03,    # Protect   -- enemy defense up
    21: 0x2E,    # Haste     -- enemy extra hits
    22: 0x1F,    # NulAll    -- enemy all-element ward
    23: 0x04,    # Blink     -- enemy evasion up (self-cast)
    24: 0x3B,    # Saber     -- enemy weapon buff (self-cast)
    # --- enemy skills (effects 0x41-0x65) ------------------------------------
    # Picked off the full-table audit for "inconveniences without
    # incapacitating": single-target, or party-wide but non-incapacitating. The
    # party-wide KO/stone/paralysis skills (Earthquake 0x47, Deadly Gas 0x53,
    # Stone Gas 0x4F, Mind Blast 0x50) and the 64-100 power party nukes are
    # deliberately absent. All five cast live at a real party 2026-09-01.
    25: 0x5C,    # Scorch    -- single, fire, power 96
    26: 0x45,    # Flash     -- party-wide blind
    27: 0x54,    # Snort     -- single, blind. NOT the same-named 0x64, which
                 #              removes its target outright.
    28: 0x5E,    # Excalipur -- single. type 23 is a one-off opcode; verified
                 #              live as the intended low single hit.
    29: 0x44,    # Gaze      -- single, paralysis (Bigeyes': monster_stats+7 is
                 #              the AI-script index, Bigeyes 0x13 -> script 3 ->
                 #              skill 3 -> effect 0x41+3).
}

# A retired kind keeps its ordinal as a permanent gap. The name lives here for
# the client's no-op branch and for the "must not be rollable" tests; it is
# registered in no item table.
RETIRED_TRAP_KINDS = {
    1: "Mana Sap Trap",         # 2026-08-29: no MP-drain effect exists anywhere
                                # in magic_info, so there was nothing real to
                                # borrow and a trap must BE a real ability.
    5: "Monster Magnet Trap",   # 2026-08-29 (user): encounter rate x4/90s
    6: "Ogre's Toll Trap",      # 2026-08-29 (user): a stat modifier with no
                                # ability analogue -- felt, never seen.
}


def trap_name(effect_id):
    """AP item name for a trap that casts `effect_id`.

    The ability's own in-game string + " Trap", so the item and the battle box
    always agree. If a crowded shop name bank has to trim one, name_banks cuts
    at the LAST space first, which leaves "Poison Darts" exact.
    """
    return f"{ABILITY[effect_id][0]} Trap"


TRAP_ITEMS = {kind: trap_name(eff) for kind, eff in TRAP_EFFECTS.items()}
TRAP_ITEM_NAMES = sorted(TRAP_ITEMS.values())

# --- invariants, asserted at import ------------------------------------------
# Each of these was a comment asking a maintainer to be careful, and each has a
# real failure behind it.
assert not (set(TRAP_EFFECTS) & set(RETIRED_TRAP_KINDS)), \
    "a retired ordinal must stay a GAP, never be reused"
_unknown = [e for e in TRAP_EFFECTS.values() if e not in ABILITY]
assert not _unknown, f"effect ids off the magic_info table: {_unknown}"
# ONE kind per ability NAME. Five effect ids print "Gaze", four print "Blaze",
# two each print "Snort" and "Icestorm"; two of them as traps would need one AP
# item name for two different abilities while the battle box printed the same
# string for both. This is the check that used to be a paragraph of comment.
assert len(set(TRAP_ITEMS.values())) == len(TRAP_ITEMS), \
    "two trap kinds share an ability name: " + ", ".join(
        sorted(n for n in TRAP_ITEMS.values()
               if list(TRAP_ITEMS.values()).count(n) > 1))
assert not (set(TRAP_ITEM_NAMES) & set(RETIRED_TRAP_KINDS.values())), \
    "a retired name must not come back as a live trap"
del _unknown

# Skills vs spells, for tests and for anyone auditing the roster's balance.
SKILL_KINDS = frozenset(k for k, e in TRAP_EFFECTS.items() if e > SPELL_LAST)
