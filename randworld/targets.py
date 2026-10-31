"""Location weights per site and vanilla's share per ladder rung.

Read from the apworld's own logic tables at generation time, so they can
never drift from the real location list. The generator steers each seed's
rungs toward vanilla's shares (the median), with wiggle room
(progression.sample_targets).
"""
from .. import logic as L
from .. import _locinfo

LADDER = ("foot", "canoe", "ship", "ship+canoe", "canal", "airship")
BONUS_KEYS = {0: "earthgift", 1: "hellfire", 2: "lifespring", 3: "whisperwind"}


def site_weights(bonus_caps=None, skip_regions=()):
    """{site: AP locations at that site}. bonus_caps: {dungeon index: AP
    locations} (None -> each dungeon's default). skip_regions: location
    regions a tiny / small world leaves out (sizing.skipped_regions; the
    Sunken Shrine leaves Onrac's own count). Every placeable site is
    listed, 0 when it holds none."""
    w = {}

    def add(site, n=1):
        if site:
            w[site] = w.get(site, 0) + n

    for _idx, (reg, _tok) in _locinfo.LOC_INFO.items():
        if reg is not None and reg not in skip_regions:
            add(L.REGION_SITE.get(reg, (None,))[0])
    for _nm, _i, reg, _req in L.NPC_LOCATIONS:
        if reg not in skip_regions:
            add(L.REGION_SITE.get(reg, (None,))[0])
    for nm, _i, _req in L.SHOP_LOCATIONS:
        town = nm.split(" Weapon")[0].split(" Armor")[0].split(" Item")[0]
        add(L.SHOP_TOWN_SITE.get(town))
    for dg, _name, _mx, default, _crystal, _opt in L.BONUS_DUNGEONS:
        n = default if bonus_caps is None else bonus_caps.get(dg, default)
        add(BONUS_KEYS[dg], n)
    w.pop("airship", None)            # the desert spot: an event, not a site to place
    from . import tables as T
    for k in list(T.SITES) + list(T.SEA_SITES):
        w.setdefault(k, 0)
    return w


def _rung(rule):
    """First ladder rung under which an OR-of-AND vanilla rule passes."""
    states = (set(), {L.CANOE}, {L.SHIP}, {L.SHIP, L.CANOE},
              {L.SHIP, L.CANOE, "Canal Opened", L.STAR_RUBY},
              {L.SHIP, L.CANOE, "Canal Opened", L.STAR_RUBY, L.AIRSHIP})
    for k, st in enumerate(states):
        if any(set(alt) <= st for alt in rule):
            return LADDER[k]
    return "airship"


def vanilla_shares(weights, early=False, extended=False, northern_docks=False):
    """Location share per rung on the VANILLA map under the same toggles."""
    rules = L.vanilla_site_rules(early, extended, northern_docks)
    tot = {c: 0.0 for c in LADDER}
    for site, n in weights.items():
        r = rules.get(site)
        if r is None or n <= 0:
            continue
        tot[_rung(r)] += n
    s = sum(tot.values()) or 1.0
    return {c: v / s for c, v in tot.items()}
