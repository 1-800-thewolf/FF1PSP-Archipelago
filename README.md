# Final Fantasy 1 (PSP — 20th Anniversary Edition) Archipelago Randomizer

![randomizer thumbnail]([THUMBNAIL IMAGE URL HERE])

An [Archipelago](https://archipelago.gg) multiworld randomizer for Final Fantasy 1, played in the PPSSPP emulator.

Current version: **0.1.0**

---

## Video Tutorial:
> [VIDEO TUTORIAL URL HERE]

---

## What Is This?

This turns FF1 PSP into an Archipelago multiworld randomizer. Play on your own or with your friends. Chests and Key Items are locations that can hold a random item.

You might find an early Canoe and have to sneak into the Ice Cavern early to find your Ship, or perhaps you'll see a spell tome for sale but decide to buy hints to learn what's on a floor of Mount Gulg so you can path smarter.

**What is Archipelago?**

"Archipelago is a free, open-source multiworld randomizer platform that connects players across dozens of different games simultaneously." Each player randomizes their own game, and items from one game can appear in another player's world. Learn more at [archipelago.gg](https://archipelago.gg).

> Archipelago also supports solo play if you want to run FF1 PSP on your own.

---

## Features

**Open Progression — Ship, Canoe, or Foot?**
- Open world options let you venture out on foot immediately, allow more options for travel with Canoe, or open up most of the southern continent by foot.
- Option to put Northern Docks in the world, providing the option to access more of the game before the Airship.
- The Levistone can be broken into shards, so the airship becomes a collection goal instead of one chest.

**All four PSP bonus dungeons are real AP content.** Earthgift Shrine, Hellfire Chasm, Lifespring Grotto, and Whisperwind Cove each carry a configurable number of AP locations, so you decide how much of the Soul of Chaos you're signing up for. Turn them off entirely with one option if you'd rather not.

**Job Scrolls offer specialty items that pump up party members.** So far, one scroll per job:
- Blood Knight — lifesteal and armor penetration on physical attacks
- Stealth Ninja — much better steals; damaging floors hurt less or heal you
- Grand Master — gains attack and max HP as they take damage
- Crimson Wizard — restores mana from damage taken and heals from mana spent
- White Cleric — Dia spells hurt every boss, heal the caster, and grant temporary INT<img width="624" height="267" alt="image" src="https://github.com/user-attachments/assets/85f5ba2c-de2f-4f2e-a457-b1e682477146" />


- Necrocaster — instant-death spells become reliable, and spells deal damage instead of missing

**Optional Slot Magic.** Throw out the MP pool and play the NES/Pixel Remaster vancian system with charges per spell level.

**Take on the challenge you've always wanted**
- Monster Power and Boss Difficulty turn up the heat, all the way to 500% of the base game's difficulty.
- Harder Overworld Encounters and Harder Dungeon Encounters pull tougher hand-authored pools per zone and per dungeon.
- Dangerous Forests places scary, hand-picked encounters for those unfortunate enough to find them.
- New Regional Ocean Encounters and Northern River Encounters bring hand-crafted aquatic fights to every corner of the overworld.<img width="488" height="207" alt="image" src="https://github.com/user-attachments/assets/05bcaf37-993c-4ccd-9a76-ebac39687745" />

- Boss Minions, ranging from a nuisance to absurd extra difficulty.
- Change settings on the fly, such as Encounter Rate, XP Boost, and Gil Boost.<img width="598" height="416" alt="image" src="https://github.com/user-attachments/assets/d6e82787-97c5-4634-8987-6555dd50de7a" />


**Shops built for a randomizer.** Randomization doesn't stop at shuffled stock and randomized prices. Purchase AP items from shops, buy yourself hints, and take full control of the quality of the goods for sale, ranging from mundane starting gear to exotic and priceless equipment that never appears in a vanilla store.

**Configurable goal.** Choose how many Fiends need to be defeated to break the Black Orb. An option to break the Lute into Lute Tablets. Then Chaos.

**Quality of life.** Auto Dash, Super Dash, message speed, cursor memory, spell chance colors, and a full, custom tracker built right into the AP client.

<img width="468" height="267" alt="image" src="https://github.com/user-attachments/assets/b4cd320e-96de-4b1d-a8a1-38b0e035cb34" />

<img width="600" height="302" alt="image" src="https://github.com/user-attachments/assets/f70a5061-66ab-4a62-8a27-a2ceb8273257" />


**DeathLink**, with a severity setting so you decide how much a friend dying should cost you.

---

## How It Works

**The FF1 PSP Client** manages the randomizer for you. On connect it bakes your seed's settings directly into a patched copy of your ISO, then launches PPSSPP automatically. While you play, it talks to PPSSPP's debugger to watch for checks, deliver incoming items, and keep both sides in sync.

---

## Installation

### Requirements

- **A legally-obtained ISO of _Final Fantasy_ for PSP (20th Anniversary Edition), US region, disc id `ULUS-10251`.** It is probably called `Final Fantasy Original - 20th Anniversary Edition (USA) (En,Ja) (FW3.03).iso`. No game files are distributed here and none will be provided; please acquire the iso yourself.
- **[PPSSPP](https://www.ppsspp.org/download/)** — Windows 64-bit build. You don't need to configure anything: the client patches PPSSPP's ini for you (remote debugger port and memory access mode) on first launch.
- **[Archipelago](https://github.com/ArchipelagoMW/Archipelago/releases) 0.6.0 or later**

### Steps

**1. Install the apworld**

Download `ff1psp.apworld` from the [latest release](https://github.com/1-800-thewolf/FF1PSP-Archipelago/releases/latest) and launch it, or manually place it in your Archipelago `custom_worlds` folder, normally here:
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

The **first time** you connect, the client may ask for two paths: your PPSSPP executable and your FF1 ISO. It remembers both so you won't be asked again unless you change them.

**Important**: Just connect in the client to your slot. It automatically launches the game for you. Don't open PPSSPP yourself.

**5. Play**

The client bakes your seed into a patched ISO copy and launches it. Start a New Game from the title screen. The client must stay running and connected for the whole session.

![client connected, game launching]([SCREENSHOT: client window + PPSSPP HERE])
> *Caption: the client after a successful connect — bake progress on the left, item log on the right.*

---

## Generating a Game

Each player needs a YAML. Generate a template from the Archipelago Launcher, or use the web options page.

There are a **lot** of options. They're grouped into tabs: Goal, Scale, World, Bonus Dungeons, Party, Quality of Life, Bonus Abilities, Shops, Magic, Loot, Enemies, and Death Link. Every option carries a description in the template.

If you want a place to start, the defaults will be a great experience. When you're ready to mix things up and turn up the difficulty, there are plenty of tools.

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

---

## Something Broke

Run `/ff1psp_logs` in the client's command box. It writes a single zip with the client log, the bake manifest, and the state breadcrumbs the client left behind. Attach that zip to your bug report or drop it in the Discord — it's the fastest way to get an answer, and usually the only way to diagnose a bake or sync issue.

- Bug reports: [Issues](https://github.com/1-800-thewolf/FF1PSP-Archipelago/issues)
- Discussion and support: [FF1 PSP Archipelago Discord](https://discord.com/channels/731205301247803413/1500609270620885032)
- Not in the main AP server yet? [Archipelago Discord](https://discord.gg/archipelago)

---

## Credits

Developed by **1.800.thewolf**

Thanks to the Archipelago community for the tooling, documentation, and patience, and to the PPSSPP team for a debugger good enough to build a randomizer on top of.

Thanks to the playtesters who ran broken builds so you don't have to.

### AI Disclosure

"Big chunk of this project built with help from AI. Helped with: PSP reverse engineering, MIPS hooks and ISO patching, the AP client, apworld code, and a lot of bug hunts."

Not copy-paste. Every feature designed by me first, implemented iteratively, then played and re-tested in the actual game until it worked right. Plenty of these took days, live debugger sessions, and multiple rewrites to actually fix, and several were reported by the testers credited above. AI wrote some code, I own the design, review, and final call on what ships. Bugs still mine to fix.
