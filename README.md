# Final Fantasy 1 (PSP — 20th Anniversary Edition) Archipelago Randomizer

<img width="624" height="351" alt="ff1 rando thumbnail" src="https://github.com/user-attachments/assets/c50a4567-90cc-4ab1-a07b-f80871c11300" />

An [Archipelago](https://archipelago.gg) multiworld randomizer for Final Fantasy 1, played in the PPSSPP emulator.

Current version: **0.1.0**

---

## Video Tutorial
> https://youtu.be/06Fc8MNaMPo

---

## What Is This?

This turns FF1 PSP into an Archipelago multiworld randomizer. Play on your own or with your friends. Chests and Key Items are locations that can hold a random item.

You might find an early Canoe and have to sneak into the Ice Cavern early to find your Ship, or perhaps you'll see a spell tome for sale but decide to buy hints to learn what's on a floor of Mount Gulg so you can path smarter.

**What is Archipelago?**

"Archipelago is a free, open-source multiworld randomizer platform that connects players across dozens of different games simultaneously." Each player randomizes their own game, and items from one game can appear in another player's world. Learn more at [archipelago.gg](https://archipelago.gg).

> Archipelago also supports solo play if you want to run FF1 PSP on your own.

---

## Features
Play the game how you want and turn on or off any of these game-changing features.

### Open Progression — Ship, Canoe, or Foot?
- Open world options let you venture out on foot immediately, allow more options for travel with Canoe, or open up most of the southern continent by foot.
<img width="701" height="390" alt="extended open world" src="https://github.com/user-attachments/assets/9449d0a6-b9cc-40ce-8f1d-3d304830e1b5" />

- Option to put Northern Docks in the world, providing the option to access more of the game before the Airship.
- The Levistone can be broken into shards, so the airship becomes a collection goal instead of one chest.

### Bonus Dungeons
- Earthgift Shrine, Hellfire Chasm, Lifespring Grotto, and Whisperwind Cove each carry a configurable number of AP locations
- You decide how much of the Soul of Chaos you're signing up for.
- Turn them off entirely with one option if you'd rather not.

### Job Scrolls
Specialty items that pump up party members. So far, one scroll per job:
- Blood Knight — lifesteal and armor penetration on physical attacks
- Stealth Ninja — much better steals; damaging floors hurt less or heal you
- Grand Master — gains attack and max HP as they take damage
<img width="695" height="397" alt="Grand Master" src="https://github.com/user-attachments/assets/769e34f9-76ed-40f9-b1e4-2accb21a2793" />

- Crimson Wizard — restores mana from damage taken and heals from mana spent
- White Cleric — Dia spells hurt every boss, heal the caster, and grant temporary INT
<img width="624" height="267" alt="White Cleric Dia spell healing the caster in battle" src="https://github.com/user-attachments/assets/85f5ba2c-de2f-4f2e-a457-b1e682477146" />

- Necrocaster — instant-death spells become reliable, and spells deal damage instead of missing

<img width="692" height="398" alt="cleric and necro example focused" src="https://github.com/user-attachments/assets/93a019cf-e1c1-487a-a15b-26d8c5e58454" />

### More Bonus Abilities
- Option to let Monk and Thief dabble in magic. This small boost gives these classes only 1 spell per spell level, but raises their options from 0 (only attack).
- Option to let Thief gain bonus crit chance based on Agility.
- Option to let Thief attempt to steal extra items after combat.
<img width="425" height="120" alt="Thief stealing an extra item after combat" src="https://github.com/user-attachments/assets/f1da6e20-e467-49e0-94c2-793ab7d04226" />

### Optional Slot Magic
Play with the classic NES/Pixel Remaster Vancian system with charges per spell level.
<img width="427" height="101" alt="spell charges per level in the magic menu" src="https://github.com/user-attachments/assets/8dde0e2b-cb06-48d5-a064-8751ac7261d1" />

### Take on the Challenge You've Always Wanted
- Change Monster Power and Boss Difficulty down to only 10% or turn up the heat, all the way to 500% of the base game's difficulty.
- Harder Overworld Encounters and Harder Dungeon Encounters pull tougher hand-authored pools per zone and per dungeon.
- Dangerous Forests places scary, hand-picked encounters for those unfortunate enough to find them.
- New Regional Ocean Encounters and Northern River Encounters bring hand-crafted aquatic fights to every corner of the overworld.

<img width="488" height="207" alt="ocean random encounter" src="https://github.com/user-attachments/assets/05bcaf37-993c-4ccd-9a76-ebac39687745" />

- Boss Minions, ranging from a nuisance to absurd extra difficulty.

<img width="488" height="274" alt="boss battle with extra minions" src="https://github.com/user-attachments/assets/07fe890e-959e-4431-a148-43b590945bff" />

- Change settings on the fly, such as Encounter Rate, XP Boost, and Gil Boost.

<img width="598" height="416" alt="Boost tab for on-the-fly settings in the client" src="https://github.com/user-attachments/assets/d6e82787-97c5-4634-8987-6555dd50de7a" />

### Shops Built for a Randomizer
Randomization doesn't stop at shuffled stock and randomized prices. Purchase AP items from shops, buy yourself hints, and take full control of the quality of the goods for sale, ranging from mundane to exotic to priceless.

### Configurable Goal
Choose how many Fiends need to be defeated to break the Black Orb. An option to break the Lute into Lute Tablets. Then Chaos.

### Quality of Life
- Super Dash
- Spells that miss get colored "MISS!!" text showing how close you were to hitting

<img width="468" height="267" alt="colored MISS text in battle" src="https://github.com/user-attachments/assets/b4cd320e-96de-4b1d-a8a1-38b0e035cb34" />

- Full, custom tracker built right into the AP client.

<img width="600" height="302" alt="built-in tracker in the AP client" src="https://github.com/user-attachments/assets/f70a5061-66ab-4a62-8a27-a2ceb8273257" />

### DeathLink
Severity setting lets you decide how much a death costs you.

<img width="650" height="370" alt="DeathLink severity option" src="https://github.com/user-attachments/assets/a1725854-0b8f-493d-b321-c0b6e648a267" />

---

## How It Works

**The apworld** generates your seed like any Archipelago world — items, locations, logic, spoiler.

**The FF1 PSP Client** manages the randomizer for you. On connect it bakes your seed's settings directly into a patched copy of your ISO, then launches PPSSPP automatically. While you play, it talks to PPSSPP's debugger to watch for checks, deliver incoming items, and keep both sides in sync.

Your original ISO is never modified. The client patches a cached copy per seed.

---

## Installation

### Requirements

- **Your own ISO of _Final Fantasy_ for PSP (20th Anniversary Edition), US region, disc id `ULUS-10251`.** It is probably called `Final Fantasy Original - 20th Anniversary Edition (USA) (En,Ja) (FW3.03).iso`. No game files are distributed here and none will be provided; please acquire the iso yourself.
- **[PPSSPP](https://www.ppsspp.org/download/)** — Windows 64-bit build. You don't need to configure anything: the client patches PPSSPP's ini for you (remote debugger port and memory access mode) on first launch.
- **[Archipelago](https://github.com/ArchipelagoMW/Archipelago/releases) 0.6.7 or later**

### Steps

**1. Install the apworld**

Download `ff1psp.apworld` from the [latest release](https://github.com/1-800-thewolf/FF1PSP-Archipelago/releases/latest) and launch it, or manually place it in your Archipelago `custom_worlds` folder, normally here:
```
C:\ProgramData\Archipelago\custom_worlds\
```

**2. Generate and edit the options yaml**

Open the Archipelago Launcher and launch Archipelago Options Creator

<img width="602" height="260" alt="Archipelago Options Creator in the Launcher" src="https://github.com/user-attachments/assets/a7eb2a9d-9878-4e23-adcd-65597487b5a1" />

Select Final Fantasy 1 PSP on the left and put in your name

<img width="534" height="216" alt="selecting Final Fantasy 1 PSP and entering a player name" src="https://github.com/user-attachments/assets/b4cc913c-d385-4c90-bbca-08561d972819" />

Edit the settings according to how you want to play. When you're satisfied, *Export Options* and save to your `Archipelago\Players` folder.

<img width="529" height="354" alt="Export Options button in the Options Creator" src="https://github.com/user-attachments/assets/8d5ee641-154c-41a3-969d-de90b0bccc8b" />

> Alternatively, you can generate the options yaml and edit it directly. See my other video tutorial on [how to edit a yaml](https://youtu.be/Tjp0x-ZtOP0?si=rlQFVsDvVAf-CgSV&t=78).

**3. Generate the game**

Put all the players' yamls (just yours if you're playing solo) into the Players folder and select *Generate* in the Archipelago Launcher.
<img width="794" height="366" alt="Generate button in the Archipelago Launcher" src="https://github.com/user-attachments/assets/13b81398-3ff2-4d25-b87c-ef3256e6177c" />

<img width="504" height="382" alt="compressed ff1 tut gif" src="https://github.com/user-attachments/assets/6a756b66-2b25-4188-bcb7-0d083d1841da" />

**4. Host the Output**

Host the output on [Archipelago here](https://archipelago.gg/uploads).

See more on [how to host here](https://archipelago.gg/tutorial/Archipelago/setup_en#:~:text=unique%20player%20name.-,On%20the%20website,-Gather%20all%20player).

**5. Launch the client**

Open the Archipelago Launcher and select **Final Fantasy 1 PSP Client**.

<img width="601" height="224" alt="Final Fantasy 1 PSP Client in the Archipelago Launcher" src="https://github.com/user-attachments/assets/951a2861-de1c-4ab4-9644-bae4ab04d3c2" />

**6. Connect to your server**

Connect to your Archipelago server through the client, the same way you would with any other AP game.

<img width="512" height="400" alt="client connection screen" src="https://github.com/user-attachments/assets/fbf21e13-3073-41a0-afc9-60f7cf22506d" />

<img width="509" height="402" alt="client connected to the server" src="https://github.com/user-attachments/assets/e571dfa6-ee34-46f9-b16d-2808dbbf62aa" />

If you're new to Archipelago, the general process is:
1. [Archipelago guides and overview](https://archipelago.gg/tutorial/)
2. [Generate a game](https://archipelago.gg/tutorial/Archipelago/setup_en#generating-a-game)
3. [Host the game on the website](https://archipelago.gg/tutorial/Archipelago/setup_en#hosting-an-archipelago-server)
4. [Connect to the Archipelago server](https://archipelago.gg/tutorial/Archipelago/setup_en#connecting-to-an-archipelago-server)

**7. Point the client at PPSSPP and your ISO**

The **first time** you connect, the client may ask for two paths: your PPSSPP executable and your FF1 ISO. It remembers both so you won't be asked again unless you change them.

<img width="386" height="133" alt="first-time PPSSPP and ISO path prompt" src="https://github.com/user-attachments/assets/81ea3174-f491-4c0f-aaa4-60b82b8da37b" />

**Important**: Just connect in the client to your slot. It automatically launches the game for you. Don't open PPSSPP yourself.

**8. Play**

The client bakes your seed into a patched ISO copy and launches it. Start a New Game from the title screen. The client must stay running and connected for the whole session.

> **Important**: save to the top save slot immediately, and always use the top save slot for the seed you're currently playing. If you play multiple seeds and load the wrong save slot, it can send the wrong checks.

<img width="754" height="557" alt="saving to the top save slot in-game" src="https://github.com/user-attachments/assets/219bf3f6-4367-4295-afed-41286231abca" />

---

## Choosing Your Options

There are a **lot** of options. They're grouped into tabs: Goal, Scale, World, Bonus Dungeons, Party, Quality of Life, Bonus Abilities, Shops, Magic, Loot, Enemies, and Death Link. Every option carries a description in the template.

If you want a place to start, the defaults will be a great experience. When you're ready to mix things up and turn up the difficulty, there are plenty of tools.

Key options to consider:
- `open_progression` — how much of the world is walkable or canoe-able before the Ship (`early` recommended for a first run)
- `crystals_needed` — how many Fiends before the Black Orb shatters (default 4)
- `lute_tablets_required` — break the Lute into pieces (default 10; set 0 for a single Lute item)
- `earthgift_ap_locations` / `hellfire_ap_locations` / `lifespring_ap_locations` / `whisperwind_ap_locations` — how much bonus dungeon you want in your seed (set them to 0, or use `exclude_bonus_dungeons`, to skip them)
- `monster_power_percentage` / `boss_difficulty_percentage` — the main difficulty dials
- `slot_magic` — vancian spell charges instead of MP

## Notes

- Your original ISO is never written to. The client patches a cached copy per seed.
- The client must stay running and connected for checks to register and items to arrive.
- Don't launch PPSSPP yourself — let the client do it, so the debugger comes up correctly.
- Always play on the top save slot. Loading a save from a different seed can send the wrong checks.

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
