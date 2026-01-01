# AutoLoot

An automatic loot pickup mod for Borderlands: The Pre-Sequel that intelligently picks up weapons and items while avoiding items you've already seen or dropped.

## Features

- **Configurable Item Types**: Toggle pickup for weapons, shields, grenades, class mods, artifacts, and customizations
- **Smart Tracking**: Remembers items you've picked up or had in your inventory to avoid re-picking dropped items
- **Performance Optimized**: Runs pickup checks every 60 ticks and inventory scans every 5 seconds
- **Distance-Based**: Only picks up items within your normal interaction range

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

## How It Works

The mod tracks unique IDs of items you've seen in your inventory and items you've picked up. When it encounters a pickup, it checks if the item's unique ID is in the "seen" list. If not, and the item type is enabled, it will automatically pick up the item.

This prevents the mod from picking up items you've intentionally dropped while still collecting new loot.