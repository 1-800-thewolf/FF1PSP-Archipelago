"""
Plaintext spell names -- the 64 FF1 spells, index == magic-index (shop id - 1).

Pure data, no imports, so both the apworld (tome item names) and the client-side
magic-shop enumerator can share ONE list instead of drifting copies. Indexes 0..31
are white magic, 32..63 black.
"""

SPELL_NAMES = [
    # white 0..31
    "Cure", "Dia", "Protect", "Blink", "Blindna", "Silence", "NulShock",
    "Invis", "Cura", "Diara", "NulBlaze", "Heal", "Poisona", "Fear",
    "NulFrost", "Vox", "Curaga", "Life", "Diaga", "Healara", "Stona", "Exit",
    "Protera", "Invisira", "Curaja", "Diaja", "NulDeath", "Healaga",
    "Full-Life", "Holy", "NulAll", "Dispel",
    # black 32..63
    "Fire", "Sleep", "Focus", "Thunder", "Blizzard", "Dark", "Temper", "Slow",
    "Fira", "Hold", "Thundara", "Focara", "Sleepra", "Haste", "Confuse",
    "Blizzara", "Firaga", "Scourge", "Teleport", "Slowra", "Thundaga",
    "Death", "Quake", "Stun", "Blizzaga", "Break", "Saber", "Blind", "Flare",
    "Stop", "Warp", "Kill",
]
