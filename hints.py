"""Hint products sold on weapon/armor shop shelves (HintShopOffers).

A hint row is a purchase that triggers a CLIENT ACTION, not a multiworld
location: it owns a shelf row and a placeholder game item id (so the BUYB buy
mailbox can attribute the sale), but no location id, no pool balance item and no
logic rule. A player who never buys one loses nothing, and the scarce shop
location id space (ids.SHOP_STRIDE / logic.SHOP_MAX_OFFERS) is untouched.

WHAT a row reveals is one TRACKER TILE GROUP -- the rectangles on the client's
Tracker tab, grouped by their DISPLAY name so the four "Cavern of Earth Upper"
region tiles read as one place instead of four identical products. Buying it
scouts that group's still-unchecked locations with create_as_hint=2 (recorded
for this slot, not broadcast to everyone's chat).

Everything here is PURE -- no Archipelago, no ISO, no client -- so generation
picks the products and the client resolves them from the same code.
"""
from . import tracker as TRACKER

# Tiles that are never sold as a product. Shop tiles are the AP offer rows in a
# town (a shop hinting its own shelf), and Unmapped is the region=None fallback
# tile that classify() empties by construction.
EXCLUDED_AREAS = frozenset(
    [k for k, _d, _s, _i in TRACKER.SHOP_AREAS] + [TRACKER.UNMAPPED])

_CHESTS_SUFFIX = " Chests"


def _label(display):
    """Tile display -> product label. Region tiles are labelled "<place>
    Chests" for the tracker; a hint sells the PLACE, so drop the suffix."""
    return (display[:-len(_CHESTS_SUFFIX)]
            if display.endswith(_CHESTS_SUFFIX) else display)


# area key -> product label, and the product order (AREAS order, deduped). Two
# tiles with the same display are ONE product: e.g. cavern_of_earth_b1..b4 all
# read "Cavern of Earth Upper", and mount_gulg_b2 + mount_gulg_b5 both read
# "Mount Gulg". Merging them also disposes of the parent tiles that the floor
# splits leave empty (see tracker.GULG_SUBAREAS).
LABEL_OF_AREA = {}
PRODUCTS = []                     # product labels, in tracker tile order
AREAS_OF_LABEL = {}               # label -> [area key, ...]
for _k, _d, _s in TRACKER.AREAS:
    if _k in EXCLUDED_AREAS:
        continue
    _lbl = _label(_d)
    LABEL_OF_AREA[_k] = _lbl
    if _lbl not in AREAS_OF_LABEL:
        AREAS_OF_LABEL[_lbl] = []
        PRODUCTS.append(_lbl)
    AREAS_OF_LABEL[_lbl].append(_k)

PRODUCT_SECTION = {_label(_d): _s for _k, _d, _s in TRACKER.AREAS
                   if _k not in EXCLUDED_AREAS}


# ------------------------------------------------------------- shelf name ----
# The shelf row is prefixed so a hint can never be mistaken for an item, which
# leaves 18 of the name bank's MAX_NAME_GLYPHS = 24 for the place. Weapon/armor
# name banks are re-laid out (name_banks.relayout_name_bank), so these fit; the
# full label always rides in the description bar underneath.
NAME_PREFIX = "HINT: "
# 13, not the 18 the 24-glyph bank cap would allow: a seed authoring 20-30 rows
# equalizes every name at ~13 glyphs INCLUDING the prefix, so anything longer is
# trimmed on the shelf anyway (see SHORT_NAME).
SHORT_BUDGET = 13

# Hand-written short names. Only labels whose auto-abbreviation would be
# unclear or over budget need an entry; _auto_short covers the rest.
#
# Two rules, both driven by how the bank trims (name_banks.relayout_name_bank):
#   1. SHORT. 804 bytes across 67 entries means a crowded seed equalizes every
#      authored name at ~13 glyphs, prefix included.
#   2. NO INTERNAL SPACES on anything that has a same-family sibling. The
#      trimmer cuts on the last space first, so "Gulg B5" loses "B5" whole and
#      becomes indistinguishable from "Gulg B2"; "GulgB5" has no space to cut
#      on, so it degrades a character at a time and keeps its identity longer.
# The full place name always shows in the description bar and on the Shops tab,
# so a trimmed shelf row is a nuisance, never a dead end.
SHORT_NAME = {
    "Chaos Shrine F1":                "ChaosF1",
    "Chaos Shrine F1 (Mystic Key)":   "ChaosF1MK",
    "Castle Cornelia":                "Cornelia",
    "Castle Cornelia (Mystic Key)":   "CorneliaMK",
    "Mount Duergar":                  "Duergar",
    "Mount Duergar (Mystic Key)":     "DuergarMK",
    "Elven Castle":                   "ElfCastle",
    "Elven Castle (Mystic Key)":      "ElfCastleMK",
    "Western Keep":                   "WestKeep",
    "Western Keep (Mystic Key)":      "WestKeepMK",
    "Marsh Cave":                     "MarshCave",
    "Marsh Cave (Mystic Key)":        "MarshCaveMK",
    # Floor split (tracker.MARSH_SUBAREAS). No internal spaces: the three B2/B3
    # siblings would otherwise all trim down to "Marsh Cave".
    "Marsh Cave B2 North":            "MarshB2N",
    "Marsh Cave B2 South":            "MarshB2S",
    "Marsh Cave B3":                  "MarshB3",
    "Marsh Cave Piscodemon Chest":    "MarshPisco",
    "Cavern of Earth Upper":          "EarthUpper",
    "Cavern of Earth Lower":          "EarthLower",
    "Giant's Cavern":                 "GiantsCave",
    "Cavern of Ice":                  "IceCave",
    "Cavern of Ice Backdoor":         "IceBackdoor",
    "Cavern of Ice Treasury":         "IceTreasury",
    "Mount Gulg":                     "Gulg",
    "Mount Gulg B2":                  "GulgB2",
    "Mount Gulg B4":                  "GulgB4",
    "Mount Gulg B5":                  "GulgB5",
    "Mount Gulg Agama":               "GulgAgama",
    "Waterfall Cavern":               "Waterfall",
    "Sunken Shrine":                  "SunkShrine",
    "Sunken Shrine Split":            "SunkSplit",
    "Sunken Shrine Vertical":         "SunkVert",
    "Sunken Shrine Entrance":         "SunkEntry",
    "Sunken Shrine Path to Mermaids": "SunkPath",
    "Sunken Shrine Mermaid Village":  "SunkMermaid",
    "Sunken Shrine Depths":           "SunkDepths",
    "Mirage Tower":                   "Mirage",
    "Flying Fortress":                "Fortress",
    "Dragon Caves":                   "DragonCave",
    "Dragon Caves Forest":            "DragonForest",
    "Dragon Caves Marsh":             "DragonMarsh",
    "Citadel of Trials":              "Citadel",
    "Chaos Shrine Plaza":             "ChaosPlaza",
    "Chaos Shrine Basement":          "ChaosBasement",
    "Crescent Lake":                  "Crescent",
    "Crescent Lake NPC: Sage":        "CrescentSage",
    "Lefein NPC: Elder":              "LefeinElder",
    "Gaia NPC: Fairy":                "GaiaFairy",
    "Onrac NPC: Caravan":             "CaravanNPC",
    "Onrac Caravan":                  "OnracCaravan",
    "Matoya's Cave":                  "MatoyaCave",
    "Matoya's Cave NPC: Matoya":      "Matoya",
    "Sage's Cave NPC: Sarda":         "Sarda",
    "Sarda's Cave":                   "SardaCave",
    # Bonus dungeons: the (Random)/(Static) half MUST survive, or the two tiles
    # of one dungeon become the same shelf row.
    "Earthgift Shrine (Random)":      "EarthgiftRnd",
    "Earthgift Shrine (Static)":      "EarthgiftSta",
    "Hellfire Chasm (Random)":        "HellfireRnd",
    "Hellfire Chasm (Static)":        "HellfireSta",
    "Lifespring Grotto (Random)":     "LifespringRnd",
    "Lifespring Grotto (Static)":     "LifespringSta",
    "Whisperwind Cove (Random)":      "WhisperRnd",
    "Whisperwind Cove (Static)":      "WhisperSta",
}

# Suffix rewrites for the auto path, longest first.
_SUFFIX_SHORT = (("(Mystic Key)", "MK"), ("(Random)", "Rnd"),
                 ("(Static)", "Stat"))


def _auto_short(label):
    """Fallback short name: an NPC tile keeps only the person ("Western Keep
    NPC: Astos" -> "Astos"), tile suffixes contract, and anything still over
    budget is cut on a word boundary.

    The suffix is held back from the trim on purpose. Cutting the PLACE is
    lossy but readable; cutting the suffix collapses a dungeon's two tiles onto
    one shelf name, which is not recoverable."""
    s = label
    if "NPC: " in s:
        s = s.split("NPC: ", 1)[1]
    suffix = ""
    for long, short in _SUFFIX_SHORT:
        if s.endswith(long):
            s = s[:-len(long)].rstrip()
            suffix = " " + short
            break
    s = s.strip()
    room = SHORT_BUDGET - len(suffix)
    if len(s) > room:
        cut = s.rfind(" ", 0, room + 1)
        s = s[:cut] if cut > 0 else s[:room]
    return (s + suffix).strip()


def short_name(label):
    """The place half of the shelf row, <= SHORT_BUDGET glyphs."""
    s = SHORT_NAME.get(label) or _auto_short(label)
    return s[:SHORT_BUDGET]


def shelf_name(label):
    """Full shelf row text, e.g. "HINT: Gulg B5"."""
    return NAME_PREFIX + short_name(label)


def desc_candidates(label, n):
    """Description-bar phrasings, LONGEST FIRST -- the shop desc banks hand out
    a byte budget per authored entry, and the caller takes the first phrasing
    that fits whole (same ladder discipline as ApClient._ap_desc_cands)."""
    place = label.split("NPC: ", 1)[1] if "NPC: " in label else label
    short = short_name(label)
    if "NPC: " in label and n <= 1:
        return [f"Reveals what {place} is holding.",
                f"Reveals {place}'s item.", f"Hint: {short}.", "Hint."]
    unit = "check" if n == 1 else "checks"
    return [f"Reveals {n} {unit} in {place}.",
            f"{n} {unit} in {place}.",
            f"{n} {unit} in {short}.",
            f"Hint: {n} {unit}.", "Hint."]


def desc_text(label, n, budget=None):
    """The description-bar text; with `budget` set, the longest phrasing that
    fits that many bytes whole."""
    cands = desc_candidates(label, n)
    if budget is None:
        return cands[0]
    for c in cands:
        if len(c) <= budget:
            return c
    return "Hint."


# ------------------------------------------------------------------ price ----
# Sublinear in the number of locations revealed, so a big tile is a BULK
# DISCOUNT rather than a multiple -- but a mild one: 1 loc = 3,000 gil at 3,000
# per location, 18 locs = 35,000 at ~1,940 each. The exponent IS the discount
# (1.0 would be linear, no discount at all); 0.85 is what puts an 18-chest floor
# at 35,000. Rounded to the nearest 100 gil so a shelf price reads cleanly.
PRICE_BASE = 3000
PRICE_EXP = 0.85
PRICE_ROUND = 10
PRICE_MAX = 99999                  # same ceiling rando clamps every price to


def round_price(gil):
    """A shelf-legible price: nearest 10 gil, never free, never over the cap.

    The cap is the one place the rounding is deliberately abandoned: a bundle
    that prices out at or above the ceiling reads as a flat 99999 rather than a
    rounded-down 99990, so the game's maximum price looks like the statement it
    is instead of an accident of arithmetic."""
    if gil >= PRICE_MAX:
        return PRICE_MAX
    v = int(round(gil / PRICE_ROUND)) * PRICE_ROUND
    return PRICE_MAX if v >= PRICE_MAX else max(PRICE_ROUND, v)


def price_for(n):
    """Shelf price for a product revealing `n` locations."""
    return round_price(PRICE_BASE * (max(1, int(n)) ** PRICE_EXP))


# ------------------------------------------------------------- the products --
def area_of(lid):
    """Tracker tile key for a location id, or None if it is not ours.

    classify() also returns the tile's ACCESS RULE, which needs the seed's
    region rules -- a product only needs the tile, and every branch of
    classify derives the key from the id alone, so the empty rule maps are
    safe here."""
    res = TRACKER.classify(lid, {}, {}, False)
    return None if res is None else res[0]


def products_for(loc_ids):
    """{product label -> sorted location ids} for a seed's location pool.

    Only tiles that actually hold a location THIS seed appear, which is what
    keeps EMPTY tiles (bonus dungeons off, no Mystic Key chests, Gulg B5 loot
    off) out of the draw instead of selling a hint that reveals nothing."""
    out = {}
    for lid in loc_ids:
        key = area_of(lid)
        if key is None or key in EXCLUDED_AREAS:
            continue
        label = LABEL_OF_AREA.get(key)
        if label is None:
            continue
        out.setdefault(label, []).append(lid)
    return {lbl: sorted(out[lbl]) for lbl in PRODUCTS if lbl in out}


def plan_hint_products(rng, loc_ids, want):
    """[(label, price, [location ids]), ...] -- up to `want` priced products, in
    the order they should be put on shelves.

    A QUEUE, not a per-shop map: which store ends up selling which row is
    decided later, against the placeholder ids each store's category actually
    has left (rando.build_shuffle_tables). What this owns is the draw: uniform
    over this seed's non-empty products and WITHOUT REPLACEMENT, so a tile is
    sold by at most one shop and a second purchase can never re-reveal what the
    first one did. A seed with fewer products than `want` simply yields fewer.

    `rng` must be a dedicated random.Random -- never the world's, since the
    caller may run inside create_regions (see rando.shop_offer_counts)."""
    pool = list(products_for(loc_ids).items())
    rng.shuffle(pool)
    return [(label, price_for(len(lids)), list(lids))
            for label, lids in pool[:max(0, int(want))]]
