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

## Verifying it

`verify_autoloot.py` checks the mod against the running game rather than against a mock of
it. Put it in your `sdk_mods` folder, then from the SDK console (tilde), while stood in a
level:

```
pyexec verify_autoloot.py
```

It prints what every decision function returns for your actual character — how many backpack
weapons report having ammo, which item would be dropped to make room, which slots are free,
and so on — plus a handful of pure-logic checks. **It changes nothing:** it calls only
functions that read state, never picks up, drops, equips or consumes anything, and does not
save. The functions that do change things are checked for existence only.

There is deliberately no mock-based test suite. One existed and was deleted: it stood in for
the engine, so it only ever confirmed what the mod's author already believed, and it passed
cleanly through three separate shipped bugs.

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
- **Drop Lowest Level When Full** (Default: On)
- **Fill Empty Equipment Slots** (Default: On)
- **Switch Weapon When Out Of Ammo** (Default: On)
- **Equip From Backpack When All Empty** (Default: On)
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

### Filling empty slots

**Fill Empty Equipment Slots** equips something from your backpack into any slot standing
empty — a weapon slot, or your shield, grenade mod, class mod or artifact. Slots that
already hold something are never touched, and only weapons that have ammo are used.

Whether a slot is available at all is the game's own answer
(`InventoryShouldBeReadiedWhenEquipped`), so weapon slots you haven't unlocked yet are
skipped, class mods your character can't use are skipped, and nothing is equipped while
you're riding a vehicle.

### Running dry

When the gun in your hands runs out of ammo, **Switch Weapon When Out Of Ammo** moves you to
the next slot holding one that still has some, wrapping round from slot 4 back to slot 1.
Holding slot 3 with both 3 and 4 empty puts you on slot 1; if slot 4 had ammo you'd get slot
4 instead. Slots you haven't unlocked yet are simply skipped, so this works the same with
one slot or four.

If *every* equipped weapon is dry, **Equip From Backpack When All Empty** pulls a loaded
weapon out of your backpack into the slot you're holding. If there's nothing loaded
anywhere, nothing happens and you keep what you have.

"Out of ammo" is the game's own `HasAnyAmmo` test, so it counts the clip as well as the
reserve, and it won't switch away from a TPS laser that only overheats.

Pulling a backpack weapon straight into your active slot turned out to be unreliable — the
game's own removal path for the weapon in your hands takes a rougher route than a normal
weapon switch, and no amount of waiting for the right moment (fire released, no transition in
flight) made it safe; it could still leave the new weapon unable to fire, zoom, or even show
a crosshair, with no error anywhere. So AutoLoot never touches your active slot directly for
this any more. Instead it bounces through another equipped slot first — the same plain weapon
switch **Switch Weapon When Out Of Ammo** already uses safely — loads the backpack weapon into
your *previous* slot while you're standing on the other one, then switches back. Three quick,
ordinary weapon switches instead of one risky backpack operation on the gun in your hands —
you'll briefly see the other weapon in your hands while this happens. If only one weapon slot
is unlocked there's nothing to bounce through, so that one case still equips directly as
before.

### Drop Lowest Level When Full

On by default. When your backpack is full and there's loot worth taking, AutoLoot makes room
by throwing out the worst item of whatever kind is filling the bag most — so the category
you're hoarding gives up a slot rather than the one thing you own of something.

Say the bag is full and a pistol is on the ground:

| Kind | What you're carrying | |
| --- | --- | --- |
| SMG | level 40 (9$), level 12 (50$), level 12 (7$) | most of these |
| Pistol | level 30 (80$) | |
| Shields | level 22 (60$) | |

SMGs are the fullest kind, the two level 12s are the lowest, and the cheaper one loses the
tie — so the **level 12 SMG worth 7$** is dropped and the pistol collected.

Only the backpack is touched: equipped gear is never dropped, and neither is anything you've
marked as a **favourite**. If everything is a favourite, nothing is dropped and the loot is
simply left. A slot is only given up once AutoLoot has decided it actually wants the loot.

Dropped items land on the ground and are never re-collected, since they've been in your
inventory.

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