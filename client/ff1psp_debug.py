"""Vanilla encounter_rate table data (FFRPSP adjustEncounterRate).

DATA MODULE ONLY. The playtest cheat CLI with the same filename lives at the
repo root (ff1psp_debug.py) and is NOT this file -- do not "sync" them. This
copy exists so boot_patch can ship inside the apworld with no repo-root
dependency; its two consumers are boot_patch.ENCOUNTER_RATES (restore/scale
source) and boot_patch's DataPatch("encounter_rate", ENCOUNTER_SIG, ...)
(wrong-ISO tripwire -- the table is pre-located at a fixed address, never
signature-scanned at runtime).
"""

import struct

# 96 u16 zone rates (ISO 0x2b216a8), read byte-for-byte from our ISO.
# 6 rows x 16 steps: row = terrain rate class, step = danger progression.
ENCOUNTER_RATES = (
    15, 18, 22, 27, 33, 40, 49, 60, 73, 89, 109, 134, 164, 201, 247, 303,
    12, 14, 16, 19, 22, 26, 31, 37, 44, 52, 62, 74, 88, 105, 126, 151,
    8, 9, 10, 12, 14, 16, 19, 22, 26, 31, 37, 44, 52, 62, 74, 88,
    4, 5, 6, 7, 8, 10, 11, 13, 16, 19, 23, 27, 32, 38, 46, 55,
    2, 2, 3, 3, 4, 5, 5, 6, 8, 9, 11, 13, 16, 19, 23, 27,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 4, 6, 8, 10, 14,
)
ENCOUNTER_SIG = struct.pack("<96H", *ENCOUNTER_RATES)
