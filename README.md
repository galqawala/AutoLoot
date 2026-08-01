# AutoLoot

An automatic loot pickup mod for Borderlands 2 and Borderlands: The Pre-Sequel that
intelligently picks up weapons and items while avoiding items you've already seen or
dropped.

## Features

- **Configurable Item Types**: Toggle pickup for weapons, shields, grenades, class mods and artifacts
- **Skips duplicate customizations**: Skins and heads you have already unlocked can be left where they lie
- **Smart Tracking**: Remembers items that have been in your inventory, so anything you deliberately drop is left alone
- **Adjustable Range**: Picks up within a configurable percentage of your normal interaction distance, up to 5x
- **Clears a whole pile at once**: Everything in range is collected in a single pass, and another pass runs immediately afterwards while there is still loot to take

## Installation

1. Place the `AutoLoot` folder in your `sdk_mods` directory
2. The mod will appear in your mod menu where you can configure the pickup options

## Configuration

Each item type can be toggled on/off:
- Pickup Weapons (Default: On)
- Pickup Shields (Default: On) 
- Pickup Grenades (Default: On)
- Pickup Oz Kits / Relics (Default: On) — named for whichever game you're running

Two categories can be worthless to you rather than merely unwanted, so they offer a
middle choice instead:
- **Pickup Class Mods** — *All*, *My class*, or *None* (Default: My class)
- **Pickup Customizations** — *All*, *New only*, or *None* (Default: New only)
- **Auto Use Customizations** (Default: On)
- **Pick Lower Level** (Default: Off)
- **Pickup Range %** — 100% is the game's normal distance (Default: 100%, range 100–500%)
- **Backpack HUD Summary Seconds** — 0 doesn't show it on screen at all (Default: 10, max 60)
- **Backpack Summary In Console** (Default: On)

### Backpack Summary

Once you've cleared a pile of loot, a short non-blocking message reports what your backpack
now holds, with the total against your capacity. Weapons are grouped by ammo type and
everything else by category, all in one run ordered by how many you have — so whatever is
taking up the most space comes first:

```
Backpack  34/39
Laser 5 | Pistol 4 | Shields 3 | SMG 3 | Combat Rifle 2 | Grenade Mods 2 | Shotgun 2 | Class Mods 1 | Oz Kits 1 | Sniper 1
```

The two outputs are independent, so you can have either, both, or neither.

It appears once the pile is finished rather than once per item, since the game drops HUD
messages that arrive too close together. Categories you have none of are left out.

Ammo types are read from each weapon's own `AmmoResource`, not from a built-in list, so
BL2 and TPS each show their own — including TPS's lasers — and modded ammo types are
counted too.

## How It Works

The mod tracks the unique IDs of items that have been in your inventory. When it sees a
pickup whose ID isn't on that list, and its type is enabled, it collects it. That's what
keeps it from re-collecting anything you deliberately dropped.

Being *in your inventory* is the only thing that marks an item as seen — merely trying to
pick something up does not. So an item you couldn't take because your backpack was full is
picked up normally later, once you have room for it.

### Pick Lower Level

Off by default, so AutoLoot skips gear that is weaker than the best of its kind you already
carry or have equipped. Weapons compete only within their own ammo type, so a great SMG
never stops you picking up a pistol.

With a level 2 pistol on the ground:

| Best pistol you hold | Result |
| --- | --- |
| none | picked up |
| level 1 | picked up |
| level 2 | picked up |
| level 3 or above | left behind |

Anything whose level can't be read is picked up regardless, as is anything with no level at
all — customizations are never affected by this setting. Turn it on to go back to
collecting everything.

### Class mods and customizations

A skin you have already unlocked, or a class mod your character cannot equip, does nothing
for you — so both get a three-way setting rather than a plain on/off. On *New only* and
*My class*, those are left on the ground.

Both questions are answered by the game itself rather than guessed at
(`WillowCustomizationManager.IsCustomizationUnlocked`, and the same class check the game
uses in `WillowItem.IsPlayerRestricted`), so they stay correct across characters and DLC.

*My class* is about which character can wear it, not about level — a class mod above your
current level is still yours, and is still collected.

With **Auto Use Customizations** on, a skin or head you pick up is used straight away, so
it unlocks without a trip to the backpack. This runs the same sequence the backpack screen
does when you choose *Use*, and the game itself refuses to spend one you have already
unlocked. Note it consumes the item, so you can't hand it to another player afterwards —
turn this off if you collect customizations for friends.

AutoLoot only ever picks things up — it never removes anything from your backpack.