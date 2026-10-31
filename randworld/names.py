"""Item names of the random map's Nitro doors (gates.py), kept in a module
with no imports: the apworld registers them at import time and must not pull
the generator's big data tables in to do it.

Archipelago's datapackage is static, so every name a seed can give a Nitro
door is pre-declared here; slot_data tells the client which name rides which
gate bit. NITRO_ITEM_VARIANTS' ORDER IS FROZEN: entry k is world-item ordinal
ids.WORLD_NITRO_VARIANT_BASE + k (test_randworld_gates pins it). Append only.
The names follow the user's 2026-10-16 call: a Nitro Powder is named after the
site its boulder walls in ("Cavern of Ice Nitro"); one pocket with two doors
names them by compass side ("Gaia Nitro (West)").
"""

AREA_NAME = {
    "cornelia": "Cornelia", "pravoka": "Pravoka", "elfheim": "Elfheim",
    "melmond": "Melmond", "crescent": "Crescent Lake", "onrac": "Onrac",
    "gaia": "Gaia", "lufenia": "Lefein", "caravan": "Caravan",
    "chaos": "Chaos Shrine", "matoya": "Matoya's Cave", "duergar": "Mount Duergar",
    "earthgift": "Earthgift Shrine", "wkeep": "Western Keep", "marsh": "Marsh Cave",
    "earth": "Cavern of Earth", "titan": "Giant's Cavern", "sarda": "Sage's Cave",
    "gulg": "Mount Gulg", "ice": "Cavern of Ice", "whisperwind": "Whisperwind Cove",
    "trials": "Citadel of Trials", "hellfire": "Hellfire Chasm", "cardia": "Dragon Caves",
    "lifespring": "Lifespring Grotto", "mirage": "Mirage Tower", "waterfall": "Waterfall Cavern",
}
COMPASS8 = ("East", "Southeast", "South", "Southwest", "West", "Northwest", "North", "Northeast")
COMPASS4 = ("East", "South", "West", "North")
# sites a Nitro pocket may wall in: Gaia (the Gaian door's), plus any location
# site the Lufenian door can take (not the start, not a site with no AP
# location, not Titan's two-mouth tunnel)
POCKETABLE = tuple(sorted(k for k in AREA_NAME
                          if k not in ("cornelia", "lufenia", "titan", "caravan", "earthgift",
                                       "hellfire", "whisperwind", "lifespring")))
NITRO_ITEM_VARIANTS = tuple(
    [f"{AREA_NAME[k]} Nitro" for k in POCKETABLE]
    + [f"Gaia Nitro ({d})" for d in COMPASS8])

# 2026-10-23 (user): any site may stand behind any bridge or door -- a bridge
# is named after its site too ("Pravoka Bridge"), Lufenia and Gaia are no
# longer reserved, and a two-door pocket can wall in any site. Appended AFTER
# NITRO_ITEM_VARIANTS (frozen; see the module docstring).
GATE_SITES = tuple(sorted((set(POCKETABLE) - {"waterfall"}) | {"lufenia"}))
ITEM_SITE_NAME = {**AREA_NAME, "lufenia": "Lufenia"}   # a new item name: the place's name


def site_name(key):
    return ITEM_SITE_NAME.get(key, str(key).title())


BRIDGE_ITEM_VARIANTS = tuple(f"{site_name(k)} Bridge" for k in GATE_SITES)
NITRO_EXTRA_VARIANTS = tuple(
    [f"{site_name(k)} Nitro" for k in GATE_SITES if f"{site_name(k)} Nitro" not in NITRO_ITEM_VARIANTS]
    + [f"{site_name(k)} Nitro ({d})" for k in GATE_SITES if k != "gaia" for d in COMPASS4])
# every gate item name a random map can produce, in ordinal order
GATE_ITEM_VARIANTS = NITRO_ITEM_VARIANTS + BRIDGE_ITEM_VARIANTS + NITRO_EXTRA_VARIANTS
