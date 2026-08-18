from dataclasses import dataclass

from Options import (Choice, Toggle, DefaultOnToggle, Range, OptionList,
                     PerGameCommonOptions, OptionGroup)

try:
    from Options import Visibility as _Visibility
except ImportError:      # older / stubbed Options predate Visibility
    _Visibility = None

from .spell_data import SPELL_NAMES
try:
    from Options import DeathLink
except ImportError:      # test stubs of Options may predate DeathLink
    class DeathLink(Toggle):
        """When you die, everyone dies. Of course the reverse is true too."""
        display_name = "Death Link"

# start_inventory_from_pool is OPT-IN per world: it is NOT part of
# PerGameCommonOptions, so without this field AP silently ignores the yaml key
# and the player starts with nothing (live 2026-08-10: a seed asking for Ship +
# Canoe + Levistone generated with an empty Start Inventory and no error, while
# every other option on the same yaml applied). start_inventory still works
# without it, but adds the items ON TOP of the pool instead of removing them.
try:
    from Options import StartInventoryPool
except ImportError:      # test stubs of Options may predate StartInventoryPool
    try:
        from Options import ItemDict as _ItemDictBase
    except ImportError:  # ... and may not carry ItemDict either
        _ItemDictBase = object

    class StartInventoryPool(_ItemDictBase):
        """Start with these items and don't place them in the multiworld."""
        display_name = "Start Inventory from Pool"


class EncounterRate(Range):
    """Random-encounter rate as a percent.

    0 = no random encounters at all, 100 = vanilla rate, 500 = x5.

    This setting can be changed mid-game from the Boost tab.
    """
    internal_name = "encounter_rate"
    display_name = "Monster Encounter Rate (%)"
    range_start = 0
    range_end = 500
    default = 50


class XPBoostPercentage(Range):
    """Experience rewarded from battles, as a percent of vanilla.

    100 = vanilla, 200 = double XP, 10 = one-tenth.

    This setting can be changed mid-game from the Boost tab."""
    internal_name = "xp_boost_percentage"
    display_name = "XP Boost (%)"
    range_start = 10
    range_end = 500
    default = 250


class GilBoostPercentage(Range):
    """💰 Gil dropped by battles, as a percent of vanilla.

    100 = vanilla, 0 = no gil ever drops, 500 = x5.

    This setting can be changed mid-game from the Boost tab."""
    internal_name = "gil_boost_percentage"
    display_name = "Gil Boost (%)"
    range_start = 0
    range_end = 500
    default = 250


class StartingGil(Range):
    """💰 Gil the party starts a new game with. 500 = vanilla."""
    internal_name = "starting_gil"
    display_name = "Starting Gil"
    range_start = 0
    range_end = 99999
    default = 500


class BossDifficultyPercentage(Range):
    """🏋🏻‍♂️ Boss strength, as a percent of vanilla.

    100 = vanilla. 10 = pushover bosses (10% stats). 200 = twice as difficult
    (200% HP and offense, plus massive defense), a huge jump.

    Expect to grind a lot for any value above 200, and around 300 most
    bosses one-shot your party members.

    This setting can be changed mid-game from the Boost tab.
    """
    internal_name = "boss_difficulty_percentage"
    display_name = "Boss Difficulty (%)"
    range_start = 10
    range_end = 500
    default = 100


class MonsterPowerPercentage(Range):
    """🏋🏻‍♂️ Regular monster strength, as a percent of vanilla. Applies to
    every enemy that is not a boss (bosses use Boss Difficulty instead).

    100 = vanilla. 10 = pushover mobs (10% stats). 200 = twice as difficult.
    Scales HP, offense and defenses.

    Spells that have a chance to miss (Sleep, Dark, Death, Warp...) get less
    reliable as this rises, roughly half as likely to land at 200%.

    This setting can be changed mid-game from the Boost tab.
    """
    internal_name = "monster_power_percentage"
    display_name = "Monster Power (%)"
    range_start = 10
    range_end = 500
    default = 100



# --- Random starting party -------------------------------------------------
# One option per party member (top-to-bottom menu order). Each is a weighted
# Choice over the 6 jobs (players put weights in their yaml the normal AP way);
# `choose_at_game_start` keeps whatever the game's own character-creation screen
# produced (the client leaves that slot alone), `random` rolls one
# of the 6 jobs uniformly in fill_slot_data (seed-reproducible). The client then
# writes the chosen job's class byte + level-1 stat block into the save at new
# game (see ff1_data.JOB_L1_BLOCK / class_addr_sa and [[class-byte]]).
class _StartingJob(Choice):
    option_random_job = -1   # roll one of the 6 jobs uniformly
    option_warrior = 0
    option_thief = 1
    option_monk = 2
    option_red_mage = 3
    option_white_mage = 4
    option_black_mage = 5
    # Player-facing again 2026-08-08. Downstream (CHOOSE_AT_GAME_START,
    # resolve_party_jobs, the client's "leave that slot alone" path) handles
    # value 6: the client writes nothing for that slot, so the vanilla new-game
    # party-creation screen's pick stands.
    option_choose_at_game_start = 6
    default = option_random_job


class StartingJob1(_StartingJob):
    """Job for party member 1 (top of the menu).
    random_job picks a random job for the party member,
    adhering to Party Diversity (below) if enabled."""
    internal_name = "starting_job_1"
    display_name = "Starting Job — Member 1"


class StartingJob2(_StartingJob):
    """Job for party member 2.
    random_job picks a random job for the party member,
    adhering to Party Diversity (below) if enabled."""
    internal_name = "starting_job_2"
    display_name = "Starting Job — Member 2"


class StartingJob3(_StartingJob):
    """Job for party member 3.
    random_job picks a random job for the party member,
    adhering to Party Diversity (below) if enabled."""
    internal_name = "starting_job_3"
    display_name = "Starting Job — Member 3"


class StartingJob4(_StartingJob):
    """Job for party member 4 (bottom of the menu).
    random_job picks a random job for the party member,
    adhering to Party Diversity (below) if enabled."""
    internal_name = "starting_job_4"
    display_name = "Starting Job — Member 4"


class PartyDiversity(DefaultOnToggle):
    """☯️ When on, random jobs avoid stacking the same job: never
    3 or more of one job; never two separate pairs. So it will not roll
    2 White Mages + 2 Warriors, or 3 Thieves.

    Jobs you picked yourself aren't changed."""
    internal_name = "party_diversity"
    display_name = "Party Diversity"


class WhiteAndBlackMagics(DefaultOnToggle):
    """☯️ When on, random jobs avoid stacking all one magic color.
    There will be at least one white magic user and at least one black magic user.
    White: White Mage, Warrior, Monk (only if dabble in magic is on)
    Black: Black Mage, Thief
    Both: Red Mage
    
    Jobs you picked yourself aren't changed."""
    internal_name = "white_and_black_magics"
    display_name = "White And Black Magics"


# --- Tier-A data-table shuffles (boot-patched DATA tables; see rando.py) -----
# Each is a plain on/off toggle. When on, the apworld shuffles the table with
# self.random (seed-reproducible) in fill_slot_data and ships the patched bytes;
# the client boot-patches them into RAM (same model as encounter_rate).
class ShopItemPool(Choice):
    """❔💰 Which weapons, armor and items get shuffled into shops. Each level
    is a strict superset of the one below:

      unshuffled  (0) -- shops keep their vanilla inventories (no shuffle).
      mundane     (1) -- shuffle, but only ordinary stock: what vanilla town
                         shops sell (Nunchaku, Rapier, Potion, Knight's Armor)
                         plus plain treasure gear (Flame Sword, Ice Brand,
                         Diamond Armor, etc.). No exotic gear.
      exotic      (2) -- also exotic weapons, armor and items (Duel Rapier,
                         Genji gear, Curtains, Emergency Exit, etc.), but not
                         activatable weapons and priceless gear (like Black Robe).
      activatable (3) -- also exotic weapons that cast a free spell when used
                         (Wizard's Staff, Thor's Hammer, Healing Staff, etc.) plus
                         fangs (White/Red/Blue/Vampire Fang, Cockatrice Claw).
                         This equipment costs an extra x1.5.
      all         (4) -- also priceless gear and consumables (Ultima, Barbarian
                         Sword, Excalibur, Stat Pills, Apples, etc.). Priceless
                         items cost an extra x2.

    (Magic shops are covered by Shuffle Magic Shops instead.)"""
    internal_name = "shop_item_pool"
    display_name = "Shop Item Pool"
    option_unshuffled = 0
    option_mundane = 1
    alias_overworld = 1          # pre-2026-07-31 name; keeps old yamls working
    option_exotic = 2
    option_activatable = 3
    option_all = 4
    default = 2


class ShuffleMagicShops(DefaultOnToggle):
    """🔮 Shuffle which spells each magic shop sells. Gil and MP costs are
    still based on vanilla prices.

    Each class learns the same number of spells from each shop as vanilla, so
    the White Mage learns all 4 spells in Cornelia's white shop, the Red Mage
    learns 2 of them, and later the Red Wizard learns those 2 plus 1 more."""
    internal_name = "shuffle_magic_shops"
    display_name = "Shuffle Magic Shops"


class RandomizePrices(DefaultOnToggle):
    """Randomize gil prices (items, weapons, armor, spells)."""
    internal_name = "randomize_prices"
    display_name = "Randomize Prices"


class ItemPriceRangeLow(Range):
    """🪙🧤 Low bound (percent of vanilla) for randomized item, weapon and
    armor prices. So 25 here means an item can randomly cost as little as a
    quarter of its vanilla price (never below 5 gil).
    Needs Randomize Prices on."""
    internal_name = "item_price_range_low"
    display_name = "Item Price Range — Low (%)"
    range_start = 1
    range_end = 1000
    default = 25


class ItemPriceRangeHigh(Range):
    """🪙🧤 High bound (percent of vanilla) for randomized item, weapon and
    armor prices. So 200 here means an item can randomly cost as much as 2
    times its vanilla price.
    Needs Randomize Prices on."""
    internal_name = "item_price_range_high"
    display_name = "Item Price Range — High (%)"
    range_start = 1
    range_end = 10000
    default = 200


class SpellGilPriceRangeLow(Range):
    """🪙🔮 Low bound (percent of vanilla) for randomized spell gil prices.
    So 25 here means a spell can randomly cost as little as a quarter of its
    vanilla gil price.
    Independent from item prices. Needs Randomize Prices on."""
    internal_name = "spell_gil_price_range_low"
    display_name = "Spell Gil Price Range — Low (%)"
    range_start = 1
    range_end = 1000
    default = 25


class SpellGilPriceRangeHigh(Range):
    """🪙🔮 High bound (percent of vanilla) for randomized spell gil prices.
    So 200 here means a spell can randomly cost as much as 2 times its vanilla
    gil price.
    Independent from item prices. Needs Randomize Prices on."""
    internal_name = "spell_gil_price_range_high"
    display_name = "Spell Gil Price Range — High (%)"
    range_start = 1
    range_end = 10000
    default = 200


class ShuffleSpellManaCosts(DefaultOnToggle):
    """🪄 Randomize each spell's MP cost."""
    internal_name = "randomize_spell_mana_costs"
    display_name = "Randomize Spell Mana Costs"


class SpellManaCostRangeLow(Range):
    """🪄 Low bound (percent of vanilla) for randomized spell MP costs.
    So 50 here means a spell can randomly cost as little as half its vanilla
    MP cost.
    Needs Randomize Spell Mana Costs on."""
    internal_name = "spell_mana_cost_range_low"
    display_name = "Spell Mana Cost Range — Low (%)"
    range_start = 1
    range_end = 1000
    default = 50


class SpellManaCostRangeHigh(Range):
    """🪄 High bound (percent of vanilla) for randomized spell MP costs.
    So 250 here means a spell can randomly cost as much as 2.5 times its
    vanilla MP cost (capped at 99 MP).
    Needs Randomize Spell Mana Costs on."""
    internal_name = "spell_mana_cost_range_high"
    display_name = "Spell Mana Cost Range — High (%)"
    range_start = 1
    range_end = 1000
    default = 250


class CostlyBestSpells(OptionList):
    """🪄 Best spells cost more mana.

    With Randomize Spell Mana Costs on, these roll their MP cost over the upper
    half of the mana-cost range instead of the whole range, so a lucky roll
    can't make them dirt cheap.

    For Slot Magic, these are instead more likely to cost higher-level
    spell slots.
    """
    internal_name = "costly_best_spells"
    display_name = "Costly Best Spells"
    valid_keys = frozenset(SPELL_NAMES)
    default = ["Temper", "Haste", "Flare", "Full-Life", "Holy",
               "Heal", "Healara", "Healaga"]


def _spell_name_doc_lines(names, per_line=7, indent="    "):
    """The valid-name roster as wrapped, quoted doc lines -- generated from
    SPELL_NAMES so the yaml comment can never drift from what valid_keys
    actually accepts."""
    out = []
    for k in range(0, len(names), per_line):
        chunk = ", ".join(f"'{n}'" for n in names[k:k + per_line])
        out.append(indent + chunk + ("," if k + per_line < len(names) else ""))
    return "\n".join(out)


# White (0..31) then black (32..63), same order the game stores them in.
CostlyBestSpells.__doc__ += "\n" + _spell_name_doc_lines(SPELL_NAMES) + "\n"


class SlotMagic(Toggle):
    """🪄 Replace the MP pool with NES/Pixel-Remaster-style spell slots.

    Each spell level has its own pool of charges.

    This setting is not balanced around Shuffle Magic Shops. Flare could land
    as a level 1 spell with this vancian system.
    """
    internal_name = "slot_magic"
    display_name = "Slot Magic"


class SlotMagicBattleDisplay(Toggle):
    """🪄 With Slot Magic on: show a compact list of per-level spell charges
    for every party member in battle.

    Needs Slot Magic on; does nothing without it."""
    internal_name = "slot_magic_battle_display"
    display_name = "Slot Magic Battle Display"
    # TEMPORARILY HIDDEN 2026-08-08: not player-facing. The feature +
    # all its wiring stay intact -- this only drops it from the generated yaml
    # template and the web options page; it still WORKS if a yaml sets it, and
    # still appears in the spoiler for testing. RE-ENABLE = delete these two lines.
    # It stays listed in the "Magic" OptionGroup while hidden -- see the note there.
    # Removed because it's too noisy on the battle screen.
    if _Visibility is not None:
        visibility = _Visibility.spoiler


class ShuffleWhoEquipsWhat(Toggle):
    """🧤 Randomize which jobs can equip which weapons and armor.

    This keeps the overall distribution intact: Ninja can still equip
    nearly everything, Monk still almost nothing.
    """
    internal_name = "shuffle_who_equips_what"
    display_name = "Shuffle Who Equips What"


class MonkThiefDabbleInMagic(DefaultOnToggle):
    """✨ Monk and Thief can learn a little magic. They each learn only 1 spell
    per spell level, and they gain max MP very slowly.

    The Thief can learn spells like Sleep and Confuse; the Monk can learn
    spells like Protect and NulBlaze. (With Shuffle Magic Shops on, those will
    be other random spells instead.)

    This changes these classes from having zero options in battle
    (always attack) to having 1 or 2 things they can do per long rest.
    """
    internal_name = "monk_thief_dabble_in_magic"
    display_name = "Monk & Thief Dabble in Magic"


class BloodMagic(Toggle):
    """🩸 Activating equipment (spell-on-use gear like Thor's Hammer, Judgment
    Staff, Healing Staff, Black Robe) costs the user 10% of their max HP.

    Activatable gear stops being a free infinite spell.

    (Equipment Runes Required gates whether you can activate at all. This
    governs what it costs once you can.)
    """
    internal_name = "blood_magic"
    display_name = "Blood Magic (activatable equipment HP cost)"


class HarderOverworldEncounters(Toggle):
    """👹 Overworld zones throw a harder, hand-picked pool of monsters
    at you."""
    internal_name = "harder_overworld_encounters"
    display_name = "Harder Overworld Encounters"


class HarderDungeonEncounters(Toggle):
    """🐲 Dungeons are much tougher, on a deliberately rising curve. Most roll
    from the next dungeon's difficulty tier; several use hand-picked pools
    instead."""
    internal_name = "harder_dungeon_encounters"
    display_name = "Harder Dungeon Encounters"


class ChaosFloorEncounters(DefaultOnToggle):
    """👑 Random encounters on Chaos' floor. In the vanilla game, there are
    no encounters on the last floor of the Chaos Shrine, just before Chaos.
    Turn this off to leave that floor quiet.
    """
    internal_name = "chaos_floor_encounters"
    display_name = "Encounters on Chaos' Floor"


class BossMinions(Choice):
    """👥 Bosses bring friends.

      off       -- vanilla; bosses fight alone.
      light     -- +1 minor minion.
      difficult -- +3 minor minions.
      absurd    -- +3 minor minions and 1 elite minion.
    """
    internal_name = "boss_minions"
    display_name = "Boss Minions"
    option_off = 0
    option_light = 1
    option_difficult = 2
    option_absurd = 3
    default = 1


class DangerousForests(Toggle):
    """🌳 Watch out for forest tiles! Random encounters in forests are
    significantly harder than the surrounding overworld encounters.
    These encounters are carefully hand-picked per overworld zone.
    """
    internal_name = "dangerous_forests"
    display_name = "Dangerous Forests"


class RegionalOceanEncounters(DefaultOnToggle):
    """🌊 The sea has regions. The world's four ocean quadrants get their own
    encounter pools. The starting sea around Cornelia is left vanilla. Sail
    into the open ocean and the monsters get harder to match where you are.

    Pairs well with Northern Docks.
    """
    internal_name = "regional_ocean_encounters"
    display_name = "Regional Ocean Encounters"


class NorthernRiverEncounters(Toggle):
    """🛶 Northern rivers get their own encounter pool.

    With this on, the rivers of the northern continent draw from a separate,
    harder, themed pool of monsters.
    """
    internal_name = "northern_river_encounters"
    display_name = "Northern River Encounters"


class ThiefSteal(DefaultOnToggle):
    """🎁 Thieves (and Ninja) scavenge after battle. If a thief spots an
    item to take, there is a sound effect and an extra sprite shows up briefly
    next to your thief.

    The chance is based on Agility, and Luck gives an extra chance to find
    lower-tier loot. More thieves means more chances at extra loot.
    """
    internal_name = "thief_steal"
    display_name = "Thieves Steal"


class NakedMonks(DefaultOnToggle):
    """🥋 A Monk's defense actually goes up with their starting Clothes
    removed, so with this on, starting Monks auto-sell their Clothes and
    Staff. Nothing stops you re-arming a Monk later."""
    internal_name = "naked_monks"
    display_name = "Naked Monks"


class ThiefExtraCrit(DefaultOnToggle):
    """🗡️ Thieves (and Ninja) get an extra chance to land a critical
    hit, based on their Agility.

    When your thief scores an extra crit from this, there's an extra
    sound effect.
    """
    internal_name = "thief_extra_crit"
    display_name = "Thief Extra Crit"


class JobScrollBoosts(DefaultOnToggle):
    """📜 Adds Job Scrolls to the item pool, one for each job in your party.
    Each grants a permanent boost to the matching party member.

    - 🩸⚔️ Blood Knight Scroll: gain lifesteal and armor penetration with
      physical attacks.
    - 🥷🎁 Stealth Ninja Scroll: much better steals (needs Thieves Steal on;
      without it, this half of the scroll does nothing). Damaging floors
      also deal less damage. With more Stealth Thieves or Ninja, damaging
      floors actually heal you and restore mana each step.
    - 🥋💪 Grand Master Scroll: as they take damage, they gain attack and max
      HP, and heal.
    - 🎩🩸 Crimson Wizard Scroll: restore mana based on damage taken, and heal
      based on mana spent.
    - 🪄☀️ White Cleric Scroll: Dia spells hurt all bosses. Also, casting a
      Dia-family spell heals the caster and grants temporary INT.
    - ☠️🔮 Necrocaster Scroll: instant-kill spells are far more reliable
      (Death/Quake/Scourge/Warp/Kill hit more often), and spells deal damage
      instead of nothing when they miss. Kill deals a little more
      damage than Flare.
    """
    internal_name = "job_scroll_boosts"
    display_name = "Job Scroll Boosts"


class ShopMaxExtraItems(Range):
    """🏪 Max number of EXTRA normal items added to each shop, on top of the
    stock it would carry anyway."""
    internal_name = "shop_max_extra_items"
    display_name = "Max Extra Items Per Shop"
    range_start = 0
    range_end = 6
    default = 2


class ShopApOffers(Range):
    """🛒 Max number of Archipelago items for sale in each shop."""
    internal_name = "shop_ap_offers"
    display_name = "AP Offers Per Shop"
    range_start = 0
    range_end = 6
    default = 3


class ShopMaxHints(Range):
    """🔮 Max number of HINTS for sale in each shop.

    Sold at Weapon and Armor shops, purchased hints reveal the ap item at
    a location or a set of locations within your world."""
    internal_name = "shop_max_hints"
    display_name = "Max Hints Per Gear Shop"
    range_start = 0
    range_end = 3
    default = 2


class ExoticLootAmount(Range):
    """💍 How much exotic or high-end equipment (Genji set, Sun Blade,
    Thor's Hammer, Elven Cloak, Black Robe, etc.) to shuffle into the
    multiworld, on a scale from 0 (none) to 10 (the entire exotic pool).

    Mundane loot (Flame Sword, Diamond Armor, Aegis Shield, etc.) fills
    in the loot pool gaps."""
    internal_name = "exotic_loot_amount"
    display_name = "Exotic Loot Amount"
    range_start = 0
    range_end = 10
    default = 7


class PricelessLootAmount(Range):
    """💎 How much priceless equipment (Ultima, Excalibur, Masamune, Rune Axe,
    Judgment Staff, Ragnarok, Ribbon, etc.) to shuffle into the multiworld, on a
    scale from 0 (none) to 10 (the entire priceless pool). By default, there
    are restrictions on who can equip priceless gear (see Only Advanced Jobs
    Equip Priceless Gear)."""
    internal_name = "priceless_loot_amount"
    display_name = "Priceless Loot Amount"
    range_start = 0
    range_end = 10
    default = 3


class OnlyAdvancedJobsEquipPricelessGear(DefaultOnToggle):
    """🛡️ Lock priceless gear (the same gear Priceless Loot Amount and
    Shop Item Pool call priceless) so only promoted jobs can equip it. A
    Warrior cannot wield the Barbarian Sword until they class-change into a Knight,
    and nobody wears a Ribbon pre-promotion.

    Off: Priceless gear keeps whatever equip permissions it would otherwise
    have in vanilla (Ribbon = anyone, Barbarian Sword = Warrior, Knight, Red Mage,
    Red Wizard)."""
    internal_name = "only_advanced_jobs_equip_priceless_gear"
    display_name = "Only Advanced Jobs Equip Priceless Gear"


class AutoSellUnusableItems(DefaultOnToggle):
    """💰 When you get equipment that NOBODY in your party can ever equip 
    you are paid its shop sell value.

    Never sold: consumables, anything you bought in a store, your starting
    inventory, and activatable gear."""
    internal_name = "auto_sell_unusable_items"
    display_name = "Auto-Sell Unusable Items"


class OpenProgression(Choice):
    """⛰️ How much of the world opens up early on foot/canoe:

      off      (0)  -- vanilla routing. The southern continent needs the Ship.
      early    (1)  -- carves a foot trail from Cornelia to Mount Duergar, and
                       a Canoe river linking the Crescent Lake water system to
                       the Ice Cavern rivers. The Canoe opens up the entire
                       southern continent.
      extended (2)  -- also adds a foot trail from Mount Duergar to the
                       Western Keep, and a new Canoe river linking Mount Duergar 
                       to Melmond. The whole Cornelia/Marsh section is sphere 1
                       and the Earth crystal is reachable with only the Canoe.
    """
    internal_name = "open_progression"
    display_name = "Open Progression"
    option_off = 0
    option_early = 1
    option_extended = 2
    default = 1


class NorthernDocks(DefaultOnToggle):
    """⚓ Adds two ship docks to the northern continents: one south of Onrac,
    one south of the Mirage Tower desert. Much more of the map becomes
    reachable by ship, well before you get the airship.

    Pairs well with Regional Ocean Encounters.
    """
    internal_name = "northern_docks"
    display_name = "Northern Docks"


class LootInNormallyEmptyChests(DefaultOnToggle):
    """📦 Puts real loot in the chests that are empty in vanilla. 

    These empty chests are in Marsh Cave, Mt Gulg, and Citadel of Trials.

    Turn this off to leave those chests empty."""
    internal_name = "loot_in_normally_empty_chests"
    display_name = "Loot in Normally Empty Chests"


class SpellTomes(DefaultOnToggle):
    """📙 Adds Spell Tomes to the item pool. A tome is a consumable item
    that can be consumed to teach its spell to a party member that can
    learn it. Shops still sell spells normally."""
    internal_name = "spell_tomes"
    display_name = "Spell Tomes"


# --- Quality of life -------------------------------------------------------
# Player-comfort settings, not seed rules. Auto Dash / Message Speed / Cursor
# are in-game Config menu settings that the client writes ONCE at new game and
# then never touches again -- change them in the Config menu mid-run and they
# stay changed (see ApClient._movement_loop). Spell Chance Colors is on-disc.
class AutoDash(DefaultOnToggle):
    """🏃 Start with the Config "Dash" setting on, so the party
    auto runs without you holding the dash button.

    Super Dash is always available in every seed: with Config "Dash" on,
    hold the Dash button to move at double speed anywhere. Towns, dungeons,
    the overworld, the ship, the canoe and the airship.

    Off = Config "Dash" starts off, and you can turn it on any time from
    the in game Config menu to get both auto dash and Super Dash."""
    internal_name = "auto_dash"
    display_name = "Auto Dash"


class MessageSpeed(Choice):
    """💬 Starting value for the Config "Message Speed" setting.

    This only sets the default at new game."""
    internal_name = "message_speed"
    display_name = "Message Speed"
    option_slow = 0
    option_medium = 1
    option_fast = 2
    option_instant = 3
    default = 2


class CursorMode(Choice):
    """🖐️ Starting value for the Config "Cursor" setting.

      default -- menu cursors start at the top every time you open a menu.
      memory  -- menus remember where the cursor was and put it back.

    This only sets the default at new game.
    Change Cursor in the Config menu whenever you like."""
    internal_name = "cursor_mode"
    display_name = "Cursor"
    option_default = 0
    option_memory = 1
    default = 1


class SpellChanceColors(DefaultOnToggle):
    """🎨 Color the battle "miss!!" text by the odds the spell actually had of
    landing on that target, so a near-miss looks different from a cast that
    never had a chance.

      white  -- 0% chance; the target was immune.
      yellow -- 1% to 15%; small chance, but possible.
      red    -- over 15%; it had a real chance of working.

    This even covers spells sent at your party!
    It will show you how close Quake was to killing your whole party."""
    internal_name = "spell_chance_colors"
    display_name = "Spell Chance Colors"


class SpellsHitLowHpEnemies(DefaultOnToggle):
    """🎯 Your spells that can miss hit more on low health enemies.
    Soften up enemies to more reliably land spells like Break or Sleep."""
    internal_name = "spells_hit_low_hp_enemies"
    display_name = "Spells Hit Low HP Enemies"


NUM_JOBS = 6          # job ids 0..5
RANDOM_JOB = -1       # _StartingJob.option_random_job (value -1)
CHOOSE_AT_GAME_START = 6       # _StartingJob.option_choose_at_game_start (value 6)


def _diversity_ok(counts, j):
    """True if adding job id `j` keeps the party within diversity limits:
    no job used 3+ times, and at most one job duplicated (one pair)."""
    if counts.get(j, 0) >= 2:
        return False  # would make a triple
    pairs = sum(1 for c in counts.values() if c >= 2)
    if counts.get(j, 0) == 1 and pairs >= 1:
        return False  # would make a second pair
    return True


# --- white/black magic coverage (WhiteAndBlackMagics) ------------------------
# Which magic colors each job counts as. The Monk is the only job whose entry
# depends on another option: with Monk & Thief Dabble in Magic on he learns
# white-ish spells (Protect, NulBlaze) and counts white; with it off he counts
# as neither color. The Thief counts black either way -- his dabble spells
# (Sleep, Confuse) are black, and he is the black-side pick even without them.
WHITE, BLACK = "white", "black"
_MAGIC_COLORS = (WHITE, BLACK)
_JOB_COLORS = {
    0: (WHITE,),          # Warrior
    1: (BLACK,),          # Thief
    2: (),                # Monk -- + WHITE when dabble is on, see _job_colors
    3: (WHITE, BLACK),    # Red Mage
    4: (WHITE,),          # White Mage
    5: (BLACK,),          # Black Mage
}


def _job_colors(j, dabble=False):
    """The magic colors job id `j` counts as, as a set."""
    if j == 2 and dabble:
        return {WHITE}
    return set(_JOB_COLORS.get(j, ()))


def resolve_party_jobs(option_values, rng, diversity=False,
                       magics=False, dabble=False):
    """Map the 4 raw option values to a slot_data list of 4 entries.
    Each entry is a job id 0..5, or None for 'choose at game start' (the client
    leaves that slot exactly as character creation made it). `random_job`
    rolls uniformly via the supplied seeded rng (the world's self.random).

    Fixed jobs (concrete ids -- whether picked directly or via yaml weights)
    and choose-at-game-start slots are resolved first. When `diversity` is on,
    the remaining
    random_job slots are then filled one at a time, each preferring a job that
    keeps the party diverse (no triples, at most one duplicate pair); it counts
    the fixed jobs as constraints. If every job would break diversity, the slot
    falls back to a plain uniform roll.

    When `magics` is on (WhiteAndBlackMagics), random slots are additionally
    steered so the finished party holds at least one white and at least one
    black magic user (`dabble` = monk_thief_dabble_in_magic, which makes the
    Monk count white). Enforcement is LAZY: a slot is only restricted once the
    colors still missing can no longer all be covered by the slots after it, so
    with 4 random slots the first two roll freely. A slot that must cover both
    missing colors can only be a Red Mage.

    `magics` is dropped entirely if any slot is choose-at-game-start: that
    slot's job isn't known at gen time, so it is assumed to cover whatever is
    missing."""
    out = [None] * len(option_values)
    random_slots = []
    counts = {}
    for i, v in enumerate(option_values):
        if v == CHOOSE_AT_GAME_START:
            out[i] = None
        elif v == RANDOM_JOB:
            random_slots.append(i)
        else:
            out[i] = int(v)
            counts[out[i]] = counts.get(out[i], 0) + 1
    if magics and any(v == CHOOSE_AT_GAME_START for v in option_values):
        magics = False
    have = set()
    if magics:
        for j in out:
            if j is not None:
                have |= _job_colors(j, dabble)
    for n, i in enumerate(random_slots):
        if magics:
            # How many of the still-missing colors THIS slot has to cover: the
            # slots after it can carry one color each (a Red Mage carries two,
            # but never count on rolling one).
            missing = set(_MAGIC_COLORS) - have
            need = len(missing) - (len(random_slots) - n - 1)
        else:
            missing, need = set(), 0
        div_ok = [j for j in range(NUM_JOBS)
                  if not diversity or _diversity_ok(counts, j)]
        if need > 0:
            mag_ok = [j for j in range(NUM_JOBS)
                      if len(_job_colors(j, dabble) & missing) >= need]
            # Both lists are non-empty in practice and they always intersect: a
            # job diversity blocks is one already IN the party (a triple needs
            # 2 of it, a second pair needs 1), and such a job's colors are
            # already covered -- so no job that fixes a MISSING color can be
            # blocked. The fallback order (magic first) is only insurance
            # against a future change to the diversity rule.
            allowed = [j for j in div_ok if j in mag_ok] or mag_ok or div_ok
            j = rng.choice(allowed) if allowed else rng.randrange(NUM_JOBS)
        elif diversity:
            j = rng.choice(div_ok) if div_ok else rng.randrange(NUM_JOBS)
        else:
            j = rng.randrange(NUM_JOBS)
        out[i] = j
        counts[j] = counts.get(j, 0) + 1
        if magics:
            have |= _job_colors(j, dabble)
    return out


# --- Soul-of-Chaos bonus dungeons: per-dungeon dynamic-chest AP location counts ---
# The four bonus dungeons regenerate their layout every entry and can be RE-ENTERED
# without limit. The client turns the first N procedural chests OPENED into AP checks
# (emptied of vanilla loot, give the randomized AP item); chests opened after the Nth
# behave like vanilla. N is set here per dungeon and EXACTLY N AP locations are created
# (N chosen => N in the pool, no padding). exclude_bonus_dungeons overrides all four to 0.
# The 4 STATIC boss-chamber chests are separate AP checks (see exclude_bonus_dungeons).
#
# The "first N opened" count is CUMULATIVE ACROSS DESCENTS -- the client tracks how many
# of the dungeon's ordinals are already checked (persisted via AP sent_locations) and
# sends the next one on each procedural open. So there is NO per-run reachability limit:
# if one clear surfaces fewer than N chests, the player re-enters and keeps opening until
# all N are found. N is purely how MANY AP checks the dungeon contributes (higher = more
# grinding), never a softlock risk -- progression can never strand in an unreachable
# surplus. The ranges below are just how far the count can be pushed.
#
# HARD CEILING: every range_end here must stay <= ids.DYNCHEST_STRIDE (64).
# ------------------------------------------------------------------------------
# Each dungeon owns a block of DYNCHEST_STRIDE consecutive location ids
# (ids.dyn_chest_loc_id = BASE + DYNCHEST_OFF + dungeon * STRIDE + ordinal), so a
# cap above the stride runs a dungeon's ordinals straight into the NEXT dungeon's
# block. Two separate failures follow, both live-diagnosed 2026-08-09 on a seed
# whose Whisperwind cap was 100:
#
#   1. Nothing past ordinal 63 is ever CHECKED. _bonus_dyn_loop.next_ordinal
#      counts `range(DYNCHEST_STRIDE)`, so once ordinals 0..63 are sent it
#      returns 64 forever: ordinal 64 sends once and the counter freezes. Worse,
#      `remain = cap - nxt` stays positive, so the on-disc cave keeps STRIPPING
#      the vanilla grant while no AP check is sent. Every chest past the 65th
#      gave the player nothing at all: no vanilla item, no AP item, no check.
#      Re-entering does not help; this is not a per-run limit.
#   2. For dungeons 0..2 a past-stride ordinal COLLIDES with a real location in
#      the next dungeon (dungeon 0 ordinal 64 == dungeon 1 ordinal 0), so two
#      locations share one id. Whisperwind (the last block) collides with
#      nothing, which is why the bug read as "some chests are dead" and not as
#      corruption.
#
# Whisperwind's range_end was 100 and is now 64. Raising it again REQUIRES
# widening the id space first; the options, in increasing order of cost:
#
#   a. Bump ids.DYNCHEST_STRIDE to >= the new ceiling (128 covers every dungeon's
#      floor count with room to spare) AND register every ordinal up to the new
#      range_end in __init__.py's LOCATION_NAME_TO_ID loop, which currently
#      registers only `range(_floors)`. This SHIFTS the ids of dungeons 1..3, so
#      it is a datapackage break: in-flight seeds stop matching and must be
#      regenerated. Do it alongside another break, never on its own.
#   b. Give each dungeon its own DYNCHEST_OFF-style base far enough apart that a
#      cap can never reach the next one. Same break, but leaves room to raise
#      caps again later without touching ids a second time.
#   c. Keep the stride and drop the per-dungeon blocks entirely: allocate ids
#      sequentially from a single counter over (dungeon, ordinal) pairs. Densest,
#      but dyn_chest_dungeon_ord stops being pure arithmetic.
#
# Whichever is chosen, next_ordinal's `range(DYNCHEST_STRIDE)` must be widened to
# match, or the caps will silently stop working again at the old bound. The
# client-side name repair (_register_dyn_chest_names) already tolerates any of
# these: it derives names from the same dyn_chest_loc_id and withholds only ids
# that two (dungeon, ordinal) pairs claim.
class EarthgiftApLocations(Range):
    """🏔️ Number of procedural chests in Earthgift Shrine (5 floors) that
    become AP locations. After you open this many chests here, additional
    chests work like vanilla chests.

    0 = none, and this dungeon's static boss-chamber chest is also dropped.
    Forced to 0 by Exclude Bonus Dungeons."""
    internal_name = "earthgift_ap_locations"
    display_name = "Earthgift Shrine AP Locations"
    range_start = 0
    range_end = 20
    default = 5


class HellfireApLocations(Range):
    """🌋 Number of procedural chests in Hellfire Chasm (10 floors) that
    become AP locations. After you open this many chests here, additional
    chests work like vanilla chests.

    0 = none, and this dungeon's static boss-chamber chest is also dropped.
    Forced to 0 by Exclude Bonus Dungeons."""
    internal_name = "hellfire_ap_locations"
    display_name = "Hellfire Chasm AP Locations"
    range_start = 0
    range_end = 40
    default = 10


class LifespringApLocations(Range):
    """🌊 Number of procedural chests in Lifespring Grotto (20 floors) that
    become AP locations. After you open this many chests here, additional
    chests work like vanilla chests.

    0 = none, and this dungeon's static boss-chamber chest is also dropped.
    Forced to 0 by Exclude Bonus Dungeons."""
    internal_name = "lifespring_ap_locations"
    display_name = "Lifespring Grotto AP Locations"
    range_start = 0
    range_end = 60
    default = 15


class WhisperwindApLocations(Range):
    """💨 Number of procedural chests in Whisperwind Cove (40 floors) that
    become AP locations. After you open this many chests here, additional
    chests work like vanilla chests.

    0 = none, and this dungeon's static boss-chamber chest is also dropped.
    Forced to 0 by Exclude Bonus Dungeons."""
    internal_name = "whisperwind_ap_locations"
    display_name = "Whisperwind Cove AP Locations"
    range_start = 0
    # 64 = ids.DYNCHEST_STRIDE, the hard ceiling explained above this class.
    # Was 100 until 2026-08-09; chests 66..100 were silently dead. A yaml still
    # asking for more than 64 now fails generation with Archipelago's normal
    # out-of-range error, which is the intended outcome: the alternative was a
    # seed that quietly ate a third of the dungeon.
    range_end = 64
    default = 40


# --- Endgame: Lute Tablets -------------------------------------------------
class CrystalsNeeded(Range):
    """💠 How many of the four Crystals (defeated Fiends) you need before the
    Black Orb in the Chaos Shrine will shatter, opening the way to Chaos.
    0 = the orb opens right away.

    See Lute Tablets Required for the other key required to win.
    """
    internal_name = "crystals_needed"
    display_name = "Crystals Needed"
    range_start = 0
    range_end = 4
    default = 4


class BonusDungeonCrystals(Toggle):
    """💠 Move crystal activation from the four Fiends to the Soul of Chaos bonus
    dungeons. When ON, defeating a Fiend still opens its bonus dungeon, but the
    matching Crystal only activates once you beat one of that dungeon's end bosses.
    Any of a dungeon's end bosses counts."""
    internal_name = "bonus_dungeon_crystals"
    display_name = "Bonus Dungeon Crystals"
    # TEMPORARILY HIDDEN 2026-08-07 (user call): not player-facing yet (needs a live
    # playtest of the end-boss detection + the crystals_needed map-scope fix). The
    # feature + all its wiring stay intact -- this only drops it from the generated
    # yaml template and the web options page; it still WORKS if a yaml sets it, and
    # still appears in the spoiler for testing. RE-ENABLE = delete these two lines.
    if _Visibility is not None:
        visibility = _Visibility.spoiler


# Following A Link to the Past's Triforce-Pieces convention: REQUIRED is the
# anchor the player reasons about, and the number of pieces actually placed in
# the multiworld (the "available" pool) is DERIVED from it and clamped so it can
# never drop below required -- an unwinnable "need 10, only 5 exist" config is
# unrepresentable. The pool = round(Required x Percentage/100) + Extra, so the
# two spare-piece knobs stack: Percentage gives proportional slack, Extra gives
# a flat amount on top. Setting Percentage 100 + Extra 0 gives exactly Required
# pieces (alttp's "available" idea).
class LuteTabletsRequired(Range):
    """🪕 How many Lute Tablets you need to collect to reach the very end of
    the game. The Lute is broken into pieces; collect this many to win.
    0 = off (the Lute is a single randomized item).

    See Crystals Needed for the other key required to win.
    """
    internal_name = "lute_tablets_required"
    display_name = "Lute Tablets Required"
    range_start = 0
    range_end = 99
    default = 10


class LuteTabletsPercentage(Range):
    """🪕 How many Lute Tablet pieces exist in the whole multiworld, as a percent
    of Lute Tablets Required (Lute Tablets Extra is then added on top). 100 =
    exactly Required, 150 = 50% extra tablets. Extra tablets found above
    Required don't do anything."""
    internal_name = "lute_tablets_percentage"
    display_name = "Lute Tablets Percentage"
    range_start = 100
    range_end = 1000
    default = 150


class LuteTabletsExtra(Range):
    """🪕 How many extra Lute Tablet pieces exist on top of
    Lute Tablets Percentage (a flat count, not a percent)."""
    internal_name = "lute_tablets_extra"
    display_name = "Lute Tablets Extra"
    range_start = 0
    range_end = 98
    default = 0


class EquipmentRunesRequired(Range):
    """🗝️ Set this above 0 to lock equipment activation (Healing Helm, Thor's
    Hammer, White/Black Robe, etc.) behind Equipment Runes. Collect this
    many to earn the Equipment Rune Key, which unlocks activation for good.
    0 = off, so equipment activates normally.

    Related: Blood Magic sets what activating costs. Shop Item Pool's
    "activatable" level decides whether shops stock this gear."""
    internal_name = "equipment_runes_required"
    display_name = "Equipment Runes Required"
    range_start = 0
    range_end = 50
    default = 0


class EquipmentRunesPercentage(Range):
    """🗝️ How many Equipment Rune pieces exist in the whole multiworld, as a
    percent of Equipment Runes Required (Equipment Runes Extra is then added on
    top). 100 = exactly Required, 150 = 50% extra runes. Extra runes found
    above Required don't do anything."""
    internal_name = "equipment_runes_percentage"
    display_name = "Equipment Runes Percentage"
    range_start = 100
    range_end = 1000
    default = 150


class EquipmentRunesExtra(Range):
    """🗝️ How many extra Equipment Rune pieces exist on top of
    Equipment Runes Percentage (a flat count, not a percent)."""
    internal_name = "equipment_runes_extra"
    display_name = "Equipment Runes Extra"
    range_start = 0
    range_end = 49
    default = 0


# Same Triforce-Pieces convention as the Lute Tablets above. The airship half of
# the world (Mirage Tower, Flying Fortress, Lefein, Gaia, ...) is only reachable
# once the shards assemble, so shards are forbidden from landing there (set_rules);
# range_end stays modest so the pre-airship pool can always seat the required count.
# The 9 ceiling is also a DISPLAY contract: single-digit counts keep the in-game
# Key Items line at exactly "Levi Shards N of M" (18 glyphs, the width the right
# menu column takes without clipping), so it never falls back to a vaguer label.
# Raising this past 9 means re-solving that line's text -- see ApClient._keyratio_loop.
class LevistoneShardsRequired(Range):
    """🪨 Set this above 0 to break the Levistone into shards. Collect this
    many Levistone Shards to assemble the Levistone and raise the airship.
    0 = off (the Levistone is a single randomized item)."""
    internal_name = "levistone_shards_required"
    display_name = "Levistone Shards Required"
    range_start = 0
    range_end = 9
    default = 0


class LevistoneShardsPercentage(Range):
    """🪨 How many Levistone Shard pieces exist in the whole multiworld, as a
    percent of Levistone Shards Required (Levistone Shards Extra is then added
    on top). 100 = exactly Required, 150 = 50% extra shards. Extra shards found
    above Required don't do anything."""
    internal_name = "levistone_shards_percentage"
    display_name = "Levistone Shards Percentage"
    range_start = 100
    range_end = 1000
    default = 150


class LevistoneShardsExtra(Range):
    """🪨 How many extra Levistone Shard pieces exist on top of
    Levistone Shards Percentage (a flat count, not a percent)."""
    internal_name = "levistone_shards_extra"
    display_name = "Levistone Shards Extra"
    range_start = 0
    range_end = 8
    default = 0


class FF1DeathLink(DeathLink):
    """💀 When you die, everyone who enabled death link dies. Of course, the
    reverse is true too.

    For FF1 PSP, "dying" means a total party wipe (every party member KO'd).
    Receiving a death kills Death Link Severity of your living party members
    (see that option)."""
    internal_name = "death_link"


class DeathLinkSeverity(Range):
    """💀 How many party members are killed when a death link is received."""
    internal_name = "death_link_severity"
    display_name = "Death Link Severity"
    range_start = 1
    range_end = 4
    default = 3


class ExcludeBonusDungeons(Toggle):
    """🚫 Keep all Soul-of-Chaos bonus-dungeon content out of the item pool.
    This includes all dynamic and static chests from Earthgift Shrine,
    Hellfire Chasm, Lifespring Grotto, and Whisperwind Cove.

    Turn this on to skip all those bonus dungeons.
    """
    internal_name = "exclude_bonus_dungeons"
    display_name = "Exclude Bonus Dungeons"


# On-disc (Route-2 code-patch) features. The launcher bakes these into a patched
# copy of the player's ISO before boot (SLOT key = "on_disc").
ON_DISC_OPTIONS = {
    "monk_thief_dabble_in_magic": MonkThiefDabbleInMagic,
    "thief_extra_crit": ThiefExtraCrit,
    "spell_tomes": SpellTomes,
    "dangerous_forests": DangerousForests,
    "boss_minions": BossMinions,
    "regional_ocean_encounters": RegionalOceanEncounters,
    "northern_river_encounters": NorthernRiverEncounters,
    "blood_magic": BloodMagic,
    "job_scroll_boosts": JobScrollBoosts,
    "spell_chance_colors": SpellChanceColors,
    "slot_magic": SlotMagic,
    "slot_magic_battle_display": SlotMagicBattleDisplay,
    # Rides the popup-colour roll caves (no apply fn of its own): the to-hit
    # score is ramped x1.0 -> x1.5 there, AFTER magic_power_scaling's shrink and
    # BEFORE the colour classify, so the odds shown and the odds rolled agree.
    "spells_hit_low_hp_enemies": SpellsHitLowHpEnemies,
    # The whole chest dedup (10 alias-duplicate records across the Citadel,
    # Marsh Cave and Mount Gulg). Off means every one of them keeps vanilla's
    # shared treasure index and stays empty, and world._removed_chest_idx drops
    # the matching AP locations. Was LootInGulgB5Chests, which covered only the
    # Mount Gulg B5 third -- the other seven were ON_DISC_ALWAYS "chest_dedup"
    # until 2026-08-12. Both legacy feature keys are still honored by
    # iso_patcher.FEATURES so already-generated seeds bake unchanged.
    "loot_in_normally_empty_chests": LootInNormallyEmptyChests,
}
# On-disc features that are ALWAYS baked (not yaml-controlled).
# shop_spell_level: magic shops always honor a spell's (possibly shuffled)
# level -- vanilla or randomized, a shop's spells match the shop's tier.
# bikke_ship_split: story-flag id5 remapped to id63 inside Pravoka so "Bikke
# defeated" and "ship available" are separate bits -- required whenever the
# Ship is a randomized AP item (always, in this world).
# (chest_dedup was here until 2026-08-12: it re-pointed 7 of the 10
# physically-duplicated chest records unconditionally, with only the Mount Gulg
# B5 three under a yaml toggle. All ten now ride
# ON_DISC_OPTIONS["loot_in_normally_empty_chests"], so nothing about the dedup
# is always-on any more. The old feature key still exists in
# iso_patcher.FEATURES, unreferenced by new seeds, so an ALREADY-GENERATED
# seed's slot_data keeps baking exactly what it baked before.)
# giant_cave_gate: moves the Giant onto a choke point + boulder so the four
# Giant's Cave chests are physically behind Titan (logic already required
# TITAN_FED regardless, so it is always-on -- no yaml toggle).
# mystic_door_gate: drops EVERY Mystic Key locked-door record on the disc (12
# records across 6 maps -- see iso_patcher._MDG_DOORS) once the Mystic Key is
# owned. v250: this is the ONLY thing that opens those doors. Vanilla opened
# them from story flag 9, which is also the Elf Prince's "already gave it"
# gate; that coupling is what made the NPC-gate class unfixable, so doors are
# now a pure function of POSSESSION and read no story flag at all (live-proven
# 2026-08-10: flag 9 forced clear + records dropped -> every door opened).
# The Mystic Key is always a randomized AP item -> unconditional, no toggle.
# prince_gate_split: repoints the Elf Prince quest chain off the Mystic Key
# function bit (story flag 9) onto shadow flag 69, so an AP-granted key can
# never kill the quest and the client never has to hold the door bit clear.
# The Mystic Key is always a randomized AP item -> unconditional, no toggle.
ON_DISC_ALWAYS = ("shop_spell_level", "bikke_ship_split",
                  "giant_cave_gate", "mystic_door_gate", "prince_gate_split",
                  "shop_buy_mailbox",
                  # Magic-defence rework. Deliberately NOT a yaml toggle: every
                  # leg is a runtime no-op while Monster Power and Boss
                  # Difficulty are both 100%, so a default seed is unaffected,
                  # and a player who raises either one always wants the fix (a
                  # toggle would just be a trap -- "why do my spells still never
                  # land at 300%?"). Being ON_DISC_ALWAYS also means in-progress
                  # seeds generated before v228 pick it up client-side.
                  "magic_power_scaling",
                  # Super Dash. The IN-GAME Config menu is the opt-in, not the
                  # yaml: both caves are gated on the Config Dash bit
                  # ([save+0x1170] bit0) AND the dash button being held, so a
                  # player who leaves Config Dash off gets byte-for-byte vanilla
                  # movement at runtime even though the bake is present. It rode
                  # AutoDash through v267, which made the yaml decide whether the
                  # feature could exist at all AND seed the Config default -- two
                  # jobs for one toggle, and an auto_dash-off player could never
                  # opt in from the Config menu. AutoDash keeps its real job
                  # (seeding the new-game Config defaults, ApClient._config_loop).
                  "super_dash")
ON_DISC_SLOT_KEY = "on_disc"

# Web-template buckets, modeled on the FF1 NES randomizer's tab layout (Goal /
# Scale / World / Treasures / Shops / Magic / Party / Enemies) with
# EarthBound's habit of putting the goal options first.
# Every option belongs to exactly ONE group -- anything left out silently falls
# into an untitled bucket at the bottom of the page. There is a test for this.
ff1psp_option_groups = [
    OptionGroup("Goal", [
        CrystalsNeeded, BonusDungeonCrystals,
        LuteTabletsRequired, LuteTabletsPercentage, LuteTabletsExtra,
    ]),
    OptionGroup("Scale", [
        EncounterRate, XPBoostPercentage, GilBoostPercentage,
        MonsterPowerPercentage, BossDifficultyPercentage,
    ]),
    OptionGroup("World", [
        OpenProgression, NorthernDocks,
        LevistoneShardsRequired, LevistoneShardsPercentage, LevistoneShardsExtra,
        LootInNormallyEmptyChests,
    ]),
    OptionGroup("Bonus Dungeons", [
        ExcludeBonusDungeons, EarthgiftApLocations, HellfireApLocations,
        LifespringApLocations, WhisperwindApLocations,
    ]),
    OptionGroup("Party", [
        StartingJob1, StartingJob2, StartingJob3, StartingJob4,
        PartyDiversity, WhiteAndBlackMagics,
    ]),
    OptionGroup("Quality of Life", [
        AutoDash, MessageSpeed, CursorMode, SpellChanceColors, NakedMonks,
    ]),
    OptionGroup("Bonus Abilities", [
        MonkThiefDabbleInMagic, ThiefSteal, ThiefExtraCrit, JobScrollBoosts,
    ]),
    # Randomize Prices is the parent toggle for BOTH price ranges below and the
    # spell gil ranges over in Magic; it lives here with the item ranges.
    OptionGroup("Shops", [
        ShopItemPool, ShopMaxExtraItems, ShopApOffers, ShopMaxHints,
        RandomizePrices, ItemPriceRangeLow, ItemPriceRangeHigh,
    ]),
    OptionGroup("Magic", [
        # SlotMagicBattleDisplay is HIDDEN via visibility=spoiler on the class
        # itself; it stays listed here anyway. Grouping is only where an option
        # renders IF it renders -- visibility decides whether it renders at all
        # (same arrangement as BonusDungeonCrystals in "Goal"). Dropping a hidden
        # option from its group buys nothing and trips test_option_groups.
        SlotMagic, SlotMagicBattleDisplay, SpellsHitLowHpEnemies,
        SpellTomes, ShuffleMagicShops,
        SpellGilPriceRangeLow, SpellGilPriceRangeHigh,
        ShuffleSpellManaCosts, CostlyBestSpells,
        SpellManaCostRangeLow, SpellManaCostRangeHigh,
    ]),
    OptionGroup("Loot", [
        BloodMagic,
        EquipmentRunesRequired, EquipmentRunesPercentage, EquipmentRunesExtra,
        ExoticLootAmount, PricelessLootAmount,
        OnlyAdvancedJobsEquipPricelessGear, ShuffleWhoEquipsWhat,
        AutoSellUnusableItems,
        StartingGil,
    ]),
    OptionGroup("Enemies", [
        RegionalOceanEncounters, NorthernRiverEncounters, DangerousForests,
        HarderOverworldEncounters, HarderDungeonEncounters, BossMinions,
        ChaosFloorEncounters,
    ]),
    OptionGroup("Death Link", [
        FF1DeathLink, DeathLinkSeverity,
    ]),
]


@dataclass
class FF1PSPOptions(PerGameCommonOptions):
    # Opt-in AP option (see the import guard): without this field the yaml key
    # is silently ignored rather than rejected.
    start_inventory_from_pool: StartInventoryPool
    encounter_rate: EncounterRate
    xp_boost_percentage: XPBoostPercentage
    gil_boost_percentage: GilBoostPercentage
    starting_gil: StartingGil
    boss_difficulty_percentage: BossDifficultyPercentage
    monster_power_percentage: MonsterPowerPercentage
    starting_job_1: StartingJob1
    starting_job_2: StartingJob2
    starting_job_3: StartingJob3
    starting_job_4: StartingJob4
    party_diversity: PartyDiversity
    white_and_black_magics: WhiteAndBlackMagics
    naked_monks: NakedMonks
    shop_item_pool: ShopItemPool
    shuffle_magic_shops: ShuffleMagicShops
    randomize_prices: RandomizePrices
    item_price_range_low: ItemPriceRangeLow
    item_price_range_high: ItemPriceRangeHigh
    spell_gil_price_range_low: SpellGilPriceRangeLow
    spell_gil_price_range_high: SpellGilPriceRangeHigh
    slot_magic: SlotMagic
    slot_magic_battle_display: SlotMagicBattleDisplay
    # Dataclass order drives the generated yaml template's order, so this field
    # sits here (not with the other on-disc toggles) to match its position in
    # the "Magic" OptionGroup below.
    spells_hit_low_hp_enemies: SpellsHitLowHpEnemies
    randomize_spell_mana_costs: ShuffleSpellManaCosts
    costly_best_spells: CostlyBestSpells
    spell_mana_cost_range_low: SpellManaCostRangeLow
    spell_mana_cost_range_high: SpellManaCostRangeHigh
    shuffle_who_equips_what: ShuffleWhoEquipsWhat
    harder_overworld_encounters: HarderOverworldEncounters
    harder_dungeon_encounters: HarderDungeonEncounters
    dangerous_forests: DangerousForests
    boss_minions: BossMinions
    chaos_floor_encounters: ChaosFloorEncounters
    regional_ocean_encounters: RegionalOceanEncounters
    northern_river_encounters: NorthernRiverEncounters
    blood_magic: BloodMagic
    monk_thief_dabble_in_magic: MonkThiefDabbleInMagic
    thief_steal: ThiefSteal
    thief_extra_crit: ThiefExtraCrit
    job_scroll_boosts: JobScrollBoosts
    shop_max_extra_items: ShopMaxExtraItems
    shop_ap_offers: ShopApOffers
    shop_max_hints: ShopMaxHints
    exotic_loot_amount: ExoticLootAmount
    priceless_loot_amount: PricelessLootAmount
    only_advanced_jobs_equip_priceless_gear: OnlyAdvancedJobsEquipPricelessGear
    auto_sell_unusable_items: AutoSellUnusableItems
    open_progression: OpenProgression
    northern_docks: NorthernDocks
    loot_in_normally_empty_chests: LootInNormallyEmptyChests
    exclude_bonus_dungeons: ExcludeBonusDungeons
    earthgift_ap_locations: EarthgiftApLocations
    hellfire_ap_locations: HellfireApLocations
    lifespring_ap_locations: LifespringApLocations
    whisperwind_ap_locations: WhisperwindApLocations
    crystals_needed: CrystalsNeeded
    bonus_dungeon_crystals: BonusDungeonCrystals
    lute_tablets_required: LuteTabletsRequired
    lute_tablets_percentage: LuteTabletsPercentage
    lute_tablets_extra: LuteTabletsExtra
    levistone_shards_required: LevistoneShardsRequired
    levistone_shards_percentage: LevistoneShardsPercentage
    levistone_shards_extra: LevistoneShardsExtra
    equipment_runes_required: EquipmentRunesRequired
    equipment_runes_percentage: EquipmentRunesPercentage
    equipment_runes_extra: EquipmentRunesExtra
    spell_tomes: SpellTomes
    auto_dash: AutoDash
    message_speed: MessageSpeed
    cursor_mode: CursorMode
    spell_chance_colors: SpellChanceColors
    death_link: FF1DeathLink
    death_link_severity: DeathLinkSeverity
