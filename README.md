# AutoLoot

An automatic loot pickup mod for Borderlands: The Pre-Sequel that intelligently picks up weapons and items while avoiding items you've already seen or dropped.

## Features

- **Configurable Item Types**: Toggle pickup for weapons, shields, grenades, class mods, artifacts, and customizations
- **Smart Tracking**: Remembers items that have been in your inventory, so anything you deliberately drop is left alone
- **Adjustable Range**: Picks up within a configurable multiple of your normal interaction distance (2x by default)
- **Clears a whole pile at once**: Everything in range is collected in a single pass, and another pass runs immediately afterwards while there is still loot to take

## Installation

1. Place the `AutoLoot` folder in your `sdk_mods` directory
2. The mod will appear in your mod menu where you can configure the pickup options

## Configuration

Each item type can be toggled on/off:
- Pickup Weapons (Default: On)
- Pickup Shields (Default: On) 
- Pickup Grenades (Default: On)
- Pickup Class Mods (Default: On)
- Pickup Artifacts (Default: On)
- Pickup Customizations (Default: On)

Plus **Pickup Range Multiplier** (Default: 2x, range 1x–5x).

## How It Works

The mod tracks the unique IDs of items that have been in your inventory. When it sees a
pickup whose ID isn't on that list, and its type is enabled, it collects it. That's what
keeps it from re-collecting anything you deliberately dropped.

Being *in your inventory* is the only thing that marks an item as seen — merely trying to
pick something up does not. So an item you couldn't take because your backpack was full is
picked up normally later, once you have room for it.