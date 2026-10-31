"""The vanilla landmark rows (ow_minimap_data.LANDMARKS) as random maps read
them: the generator's sites (tools/randworld/gen_data.py), the Map tab's pins
(ow_map.set_world) and the menu-map bake.

The vanilla rows name Bahamut's mouth (88,44) "Hellfire" and file Hellfire
Chasm's real mouth (87,53) as a Cardia dot. The exit tables say otherwise
(user 2026-10-26: Bahamut lives under a random map's "Hellfire" pin):
(88,44) -> map 0x55 -> 0x54, Bahamut's two rooms (the game's own "Dragon
Caves" cursor point); (87,53) -> 0x79, between Earthgift 0x78 and
Lifespring 0x7A. A "hellfire" site stamped on (88,44) carried Bahamut's door
and the bake parked Hellfire Chasm's on every random map. The vanilla map
keeps its rows (user call: only random maps need fixing).

The Cardia dots are five separate caves inside one map (gen_locinfo
DRAGON_CAVE_REGION, user 2026-10-28: Dragon Caves came into logic off a door
with no chests behind it). On a random map each chest cave is a site of its
own -- (71,42) Plains, (84,51) Marsh, (108,59) Forest -- and the two empty
caves (97,52), (84,41) are no site: ("", "cardia") rows, which the generator
skips (proto.site_groups) and the Map tab drops.
"""
from .. import ow_minimap_data as D

FIX = {(88, 44): ("Bahamut", "bahamut"), (87, 53): ("Hellfire", "hellfire"),
       (71, 42): ("Dragon Caves Plains", "cardia_plains"),
       (84, 51): ("Dragon Caves Marsh", "cardia_marsh"),
       (108, 59): ("Dragon Caves Forest", "cardia_forest"),
       (97, 52): ("", "cardia")}
# the random map's Dragon Caves sites (one per chest cave)
CARDIA_SITES = ("cardia_plains", "cardia_marsh", "cardia_forest")

LANDMARKS = tuple((x, y, kind) + FIX.get((x, y), (name, pin))
                  for x, y, kind, name, pin in D.LANDMARKS)
