# Final Fantasy I (PSP — 20th Anniversary Edition) Archipelago Randomizer

![randomizer thumbnail]([THUMBNAIL IMAGE URL HERE])

An [Archipelago](https://archipelago.gg) multiworld randomizer for **Final Fantasy (PSP, ULUS-10251)**, played in the PPSSPP emulator.

Current version: **0.1.0**

---

## Video Tutorial:
> [VIDEO TUTORIAL URL HERE]

---

## What Is This?

This turns FF1 PSP into an Archipelago multiworld randomizer. Every chest, every key item, and every "talk to the guy who gives you the thing" is a location. What you get back is whatever the multiworld decided to put there — which might be a Ninja job scroll for your Thief, or a Warrior's sword sitting in somebody else's world.

You might sail out of Cornelia with the Canoe and no Ship. You might walk into a shop and find a spell nobody in your party can cast, sitting next to a hint about where your Levistone shards went.

**What is Archipelago?**

"Archipelago is a free, open-source multiworld randomizer platform that connects players across dozens of different games simultaneously." Each player randomizes their own game, and items from one game can appear in another player's world. Learn more at [archipelago.gg](https://archipelago.gg).

> Archipelago also supports solo play if you want to run FF1 PSP on your own.

---

## Features

**Open Progression — the world doesn't wait for the Ship.**
- `early` carves a foot trail from Cornelia to Mount Duergar and a Canoe river linking the Crescent Lake water system to the Ice Cavern rivers. The Canoe alone opens the southern continent.
- `extended` adds a trail from Mount Duergar to the Western Keep and a river from Duergar to Melmond. The whole Cornelia/Marsh section becomes sphere 1 and the Earth crystal is reachable on the Canoe.
- Northern Docks gives the north its own port, so the Ship isn't a single-key bottleneck.
- The Levistone can be broken into shards, so the airship becomes a collection goal instead of one chest.

**All four PSP bonus dungeons are real AP content.** Earthgift Shrine, Hellfire Chasm, Lifespring Grotto, and Whisperwind Cove each carry a configurable number of AP locations, so you decide how much of the Soul of Chaos you're signing up for. Turn them off entirely with one option if you'd rather not.

**Job Scrolls — specialty items that rewrite a party member.** One scroll per job in your party, each a permanent, build-defining upgrade:
- Blood Knight — lifesteal and armor penetration on physical attacks
- Stealth Ninja — much better steals; damaging floors hurt less, and with enough of them, actually heal you
- Grand Master — gains attack and max HP as they take damage
- Crimson Wizard — restores mana from damage taken, and heals from mana spent
- White Cleric — Dia spells hurt every boss; Dia-family casts heal the caster and grant INT
- Necrocaster — instant-death spells become reliable, and spells deal damage instead of whiffing

**Optional Slot Magic.** Throw out the MP pool and play the NES/Pixel Remaster vancian system — charges per spell level. Pair it with shuffled magic shops if you want Flare to show up as a level 1 spell, which is exactly as unbalanced as it sounds. That's the point.

**Difficulty knobs that actually bite.**
- Monster Power % and Boss Difficulty % scale the whole bestiary
- Harder Overworld Encounters and Harder Dungeon Encounters pull tougher hand-authored pools per zone and per dungeon
- Dangerous Forests — forest tiles run their own, meaner encounter table, hand-picked per overworld zone
- Regional Ocean Encounters, Northern River Encounters, Boss Minions, and encounters on Chaos' floor
- Encounter Rate %, XP Boost %, and Gil Boost % if you'd rather tune the other direction

**Shops are a whole system.** Randomized prices, shuffled magic shops, extra items per shop, AP items offered for sale, hints for sale in gear shops, exotic and priceless gear that normally doesn't exist in stores, and auto-sell for junk your party can't use.

**Configurable goal.** Crystals Needed (0–4) decides how many Fiends must fall before the Black Orb shatters. Lute Tablets breaks the Lute into pieces, with Required / Percentage / Extra knobs so you control both how many you need and how many exist. Then Chaos.

**Quality of life, on by default.** Auto Dash, message speed, cursor memory, spell chance colors, and tracker support.

**DeathLink**, with a severity setting so you decide how much a friend dying should cost you.

---

## How It Works

**The apworld** generates your seed like any Archipelago world — items, locations, logic, spoiler.

**The FF1 PSP Client** does the heavy lifting. On connect it bakes your seed's settings directly into a patched copy of your ISO (encounter tables, shop stock, prices, spell data, job sprites, text banks), then launches PPSSPP on it. While you play, it talks to PPSSPP's debugger to watch for checks, deliver incoming items, and keep both sides in sync.

Your original ISO is never modified. The patched copy is cached per seed.

---

## Installation

### Requirements

- **A legally-obtained ISO of _Final Fantasy_ for PSP (20th Anniversary Edition), US region, disc id `ULUS-10251`.** This is the US retail UMD dump. Other regions and the digital releases are **not** supported — the client verifies the disc id in the ISO header and refuses anything else. `.iso` and `.cso` both work. No game files are distributed here and none will be provided; dump your own disc.
- **[PPSSPP](https://www.ppsspp.org/download/)** — Windows 64-bit build. You don't need to configure anything: the client patches PPSSPP's ini for you (remote debugger port and memory access mode) on first launch.
- **[Archipelago](https://github.com/ArchipelagoMW/Archipelago/releases) 0.6.0 or later**

### Steps

**1. Install the apworld**

Download `ff1psp.apworld` from the [latest release]([REPO URL HERE]/releases/latest) and launch it, or manually place it in your Archipelago `custom_worlds` folder, normally here:
```
C:\ProgramData\Archipelago\custom_worlds\
```

**2. Launch the client**

Open the Archipelago Launcher and select **Final Fantasy 1 PSP Client**.

**3. Connect to your server**

Connect to your Archipelago server through the client, the same way you would with any other AP game.

If you're new to Archipelago, the general process is:
1. [Archipelago guides and overview](https://archipelago.gg/tutorial/)
2. [Generate a game](https://archipelago.gg/tutorial/Archipelago/setup_en#generating-a-game)
3. [Host the game on the website](https://archipelago.gg/tutorial/Archipelago/setup_en#hosting-an-archipelago-server)
4. [Connect to the Archipelago server](https://archipelago.gg/tutorial/Archipelago/setup_en#connecting-to-an-archipelago-server)

**4. Point the client at PPSSPP and your ISO**

The **first time** you connect, the client asks for two paths: your PPSSPP executable and your FF1 ISO. It remembers both — you won't be asked again unless you change them.

**Important**: connect in the client _first_, then let it launch the game. The client starts PPSSPP itself so it's guaranteed to be connected before the game boots. Don't open PPSSPP yourself.

**5. Play**

The client bakes your seed into a patched ISO copy and launches it. Start a New Game from the title screen. The client must stay running and connected for the whole session.

![client connected, game launching]([SCREENSHOT: client window + PPSSPP HERE])
> *Caption: the client after a successful connect — bake progress on the left, item log on the right.*

---

## Generating a Game

Each player needs a YAML. Generate a template from the Archipelago Launcher, or use the web options page.

There are a **lot** of options. They're grouped into tabs: Goal, Scale, World, Bonus Dungeons, Party, Quality of Life, Bonus Abilities, Shops, Magic, Loot, Enemies, and Death Link. Every option carries a description in the template — read them, they're written for players.

If you want a good first seed, the defaults are a good first seed. If you want to change exactly one thing, set `open_progression` to `early`.

Key options to consider:
- `open_progression` — how much of the world is walkable or canoe-able before the Ship (`early` recommended for a first run)
- `crystals_needed` — how many Fiends before the Black Orb shatters (default 4)
- `lute_tablets_required` — break the Lute into pieces (default 10; set 0 for a single Lute item)
- `earthgift_ap_locations` / `hellfire_ap_locations` / `lifespring_ap_locations` / `whisperwind_ap_locations` — how much bonus dungeon you want in your seed (set them to 0, or use `exclude_bonus_dungeons`, to skip them)
- `monster_power_percentage` / `boss_difficulty_percentage` — the main difficulty dials
- `slot_magic` — vancian spell charges instead of MP

![web options page]([SCREENSHOT: options page HERE])
> *Caption: the options page — goal settings first, then everything else.*

---

## Notes

- Your original ISO is never written to. The client patches a cached copy per seed.
- The client must stay running and connected for checks to register and items to arrive.
- Don't launch PPSSPP yourself — let the client do it, so the debugger comes up correctly.
- Save anywhere is still save anywhere. Reloading a save doesn't lose received items.

---

## Something Broke

Run `/ff1psp_logs` in the client's command box. It writes a single zip with the client log, the bake manifest, and the state breadcrumbs the client left behind. Attach that zip to your bug report or drop it in the Discord — it's the fastest way to get an answer, and usually the only way to diagnose a bake or sync issue.

- Bug reports: [Issues]([REPO URL HERE]/issues)
- Discussion and support: [FF1 PSP Archipelago Discord]([DISCORD INVITE HERE])
- Not in the main AP server yet? [Archipelago Discord](https://discord.gg/archipelago)

---

## Credits

Developed by **1.800.thewolf**

Thanks to the Archipelago community for the tooling, documentation, and patience, and to the PPSSPP team for a debugger good enough to build a randomizer on top of.

Thanks to the playtesters who ran broken builds, filed real bug reports, and sent me their debug bundles instead of "it crashed."

### AI Disclosure

"Big chunk of this project built with help from Claude. Helped with: PSP reverse engineering, MIPS hooks and ISO patching, the AP client, apworld code, and a lot of bug hunts."

Not copy-paste. Every feature designed by me first, implemented iteratively, then played and re-tested in the actual game until it worked right. Plenty of these took days, live debugger sessions, and multiple rewrites to actually fix, and several were reported by the testers credited above. AI wrote some code, I own the design, review, and final call on what ships. Bugs still mine to fix.
