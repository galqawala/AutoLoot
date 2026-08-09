import math
import random
import re

from mods_base import BoolOption, DropdownOption, Game, SliderOption, build_mod, hook
from ui_utils import show_hud_message
from unrealsdk import logging
from unrealsdk.hooks import Block, Type

# TPS calls them Oz Kits, BL2 calls them Relics, and the class is WillowArtifact
# in both. Derived once so the option and the summary cannot disagree.
ARTIFACT_LABEL = "Oz Kits" if Game.get_current() == Game.TPS else "Relics"

# PlayerTick fires once per frame, so this is roughly a fifth of a second. A pass
# that actually collects something rescans on the very next tick instead, so a
# pile of loot never waits out a full interval between items.
SCAN_INTERVAL = 20
FAST_SCAN_INTERVAL = 1

ticks_until_scan = 0
seen_unique_ids = set()
picking_up = False
collected_last_pass = False
next_near_loot_report = 0.0
last_shown_body = None

pickup_weapons = BoolOption("Pickup Weapons", True)
pickup_shields = BoolOption("Pickup Shields", True)
pickup_grenades = BoolOption("Pickup Grenades", True)
pickup_artifacts = BoolOption(f"Pickup {ARTIFACT_LABEL}", True)

# Two categories can be worthless to you rather than merely unwanted, so they get
# a middle choice instead of an on/off. The two ends mean the same thing in both,
# so they are named once here; only the middle differs.
CHOICE_ALL = "All"
CHOICE_NONE = "None"

CUSTOMIZATIONS_NEW = "New only"
CLASS_MODS_MINE = "My class"

# Substrings identifying an inventory class. Named once because both the pickup
# filters and the backpack summary match on them, and a category that drifted
# between the two would be collected but never counted.
WEAPON_CLASS_TOKEN = "Weapon"
SHIELD_CLASS_TOKEN = "Shield"
GRENADE_CLASS_TOKEN = "Grenade"
ARTIFACT_CLASS_TOKEN = "Artifact"
CLASS_MOD_CLASS_TOKEN = "ClassMod"
CUSTOMIZATION_CLASS_TOKEN = "UsableCustomization"

# The seconds double as the on/off switch - a separate checkbox could disagree
# with a duration of zero, leaving two controls for one decision. The two outputs
# are independent of each other, so either, both or neither can be on.
hud_summary_seconds = SliderOption(
    "Backpack HUD Summary Seconds",
    19,
    0,
    60,
    1,
    True,
    description=(
        "Show what your backpack now holds on screen after clearing a pile of"
        " loot, for this many seconds. 0 disables the on-screen summary."
    ),
)
summary_in_console = BoolOption(
    "Backpack Summary In Console",
    True,
    description="Write the same summary to the SDK console.",
)

pickup_customizations = DropdownOption(
    "Pickup Customizations",
    CUSTOMIZATIONS_NEW,
    [CHOICE_ALL, CUSTOMIZATIONS_NEW, CHOICE_NONE],
    description=(
        "Skins and heads. \"New only\" leaves behind the ones you have already"
        " unlocked, since picking those up gains you nothing."
    ),
)
auto_equip = BoolOption(
    "Auto Equip",
    True,
    description=(
        "Fill empty gear slots from your backpack, and swap a dry weapon for a"
        " loaded one in any slot but the one you're holding."
    ),
)
switch_when_empty = BoolOption(
    "Switch Weapon When Out Of Ammo",
    True,
    description=(
        "When the gun in your hands runs dry, switch to the next equipped slot"
        " that still has ammo. Nothing happens if none of your weapons do."
    ),
)
drop_lowest_when_full = BoolOption(
    "Drop Worst Item When Full",
    True,
    description=(
        "When full, drop whatever is worth least: an item over your level"
        " first (you cannot use it yet), else the weakest one. Never a"
        " favourite."
    ),
)
auto_use_customizations = BoolOption(
    "Auto Use Customizations",
    True,
    description=(
        "Use customizations as soon as you pick them up, unlocking the skin or"
        " head. This consumes the item, so you can't trade it to another player."
    ),
)
pickup_class_mods = DropdownOption(
    "Pickup Class Mods",
    CLASS_MODS_MINE,
    [CHOICE_ALL, CLASS_MODS_MINE, CHOICE_NONE],
    description=(
        "\"My class\" leaves behind class mods your character cannot equip."
        " Pick \"All\" if you collect them for your other characters."
    ),
)
# Whole percent rather than a fractional multiplier: willow2-mod-menu logs
# "non-integer slider, which willow2-mod-menu does not support due to engine
# limitations" and cannot render one, so a float slider is unusable in game.
range_percent = SliderOption(
    "Pickup Range %",
    100,
    25,
    500,
    25,
    True,
    description=(
        "How far AutoLoot reaches, as % of your normal pickup range. 200% is"
        " twice as far. Manual pickups are unaffected."
    ),
)

# Each on/off option paired with the substring of the inventory class name it
# enables. Keeping the pair together means adding a category is one row, not a
# new branch. Customizations and class mods are deliberately absent - they are
# three-way choices, handled in should_pickup below.
PICKUP_FILTERS = (
    (pickup_weapons, "Weapon"),
    (pickup_shields, "Shield"),
    (pickup_grenades, "Grenade"),
    (pickup_artifacts, "Artifact"),
)


def is_already_unlocked(inventory, caller) -> bool:
    """Whether the player already owns this customization.

    WillowUsableCustomizationItem.IsUsefulToThisPlayer returns false for exactly
    those customizations WillowCustomizationManager.IsCustomizationUnlocked
    reports as unlocked, so let the game answer instead of reimplementing the
    profile lookup.
    """
    return not inventory.IsUsefulToThisPlayer(caller)


def is_for_my_class(inventory, caller) -> bool:
    """Whether this character can equip this class mod.

    The same test the game itself applies in WillowItem.IsPlayerRestricted.
    Deliberately not WillowInventory.CanBeUsedBy, which looks like the obvious
    call but additionally returns false for every equippable item while the
    player is riding a vehicle - which would strand class mods on the ground for
    as long as you stayed in the moonbuggy.
    """
    return inventory.DefinitionData.ItemDefinition.PlayerClassRequirementMet(caller)


def should_pickup(inventory, caller) -> bool:
    class_name = inventory.Class.Name

    if CUSTOMIZATION_CLASS_TOKEN in class_name:
        if pickup_customizations.value == CHOICE_NONE:
            return False
        if pickup_customizations.value == CHOICE_ALL:
            return True
        return not is_already_unlocked(inventory, caller)

    if CLASS_MOD_CLASS_TOKEN in class_name:
        if pickup_class_mods.value == CHOICE_NONE:
            return False
        if pickup_class_mods.value == CHOICE_ALL:
            return True
        return is_for_my_class(inventory, caller)

    return any(option.value and token in class_name for option, token in PICKUP_FILTERS)


def unique_id_of(inventory):
    """The id distinguishing one rolled item from another, or None if it has none."""
    definition_data = inventory.DefinitionData if inventory is not None else None
    if definition_data is None:
        return None
    return getattr(definition_data, "UniqueId", None)


def iter_owned_inventory(caller):
    """Every item the player holds - backpack first, then what is equipped.

    One definition of "owned", so the seen list and the level comparison can
    never disagree about whether an equipped gun counts.
    """
    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager:
        yield from inventory_manager.Backpack

    pawn = caller.Pawn
    if pawn is None or pawn.InvManager is None:
        return
    for chain in (pawn.InvManager.InventoryChain, pawn.InvManager.ItemChain):
        item = chain
        while item is not None:
            yield item
            item = item.Inventory


def update_seen_ids(caller):
    """Record everything the player is currently carrying as already seen.

    This is the *only* writer of seen_unique_ids, deliberately. Marking an item
    seen because we tried to pick it up burns it permanently whenever the attempt
    fails - a full backpack being the everyday case - so "seen" is defined as
    "has been in my inventory" and nothing else. Anything the player drops is
    therefore still ignored, while anything we failed to collect is retried.
    """
    for item in iter_owned_inventory(caller):
        unique_id = unique_id_of(item)
        if unique_id is not None:
            seen_unique_ids.add(unique_id)


def use_customizations(caller):
    """Consume backpack customizations, unlocking the skin or head each carries.

    The same sequence the backpack screen runs when you pick "Use": check the
    item is consumable and its DLC requirement is met, then TryConsume, which
    unlocks it and destroys the item. TryConsume refuses and returns false when
    the customization is already unlocked, so a duplicate is never spent.
    """
    if not auto_use_customizations.value:
        return

    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return

    # Snapshot: consuming removes the item from the very list being walked.
    for item in list(inventory_manager.Backpack):
        try:
            if item is None or item.Class is None:
                continue
            if CUSTOMIZATION_CLASS_TOKEN not in item.Class.Name:
                continue
            if not item.IsConsumable():
                continue
            if not item.IsDLCRequirementMet(caller):
                continue
            item.TryConsume()
        except Exception as ex:  # noqa: BLE001
            logging.warning(f"[AutoLoot] could not use a customization: {ex!r}")


# EQuickWeaponSlot, skipping QuickSelectNone at 0: Up, Down, Left, Right.
WEAPON_SLOTS = (1, 2, 3, 4)


def has_ammo(weapon) -> bool:
    """Whether this weapon can still shoot at all.

    HasAnyAmmo is the game's own test - the one it uses to decide whether to
    play the dry fire sound - so it already accounts for the clip as well as the
    reserve, for infinite ammo, and for TPS lasers that overheat rather than run
    out. WillowWeapon.HasActiveAmmo is the narrower "clip has rounds" check and
    would switch away from a full clip.
    """
    return bool(weapon.HasAnyAmmo())


def weapon_swap_in_progress(caller) -> bool:
    """Whether a weapon change is already underway.

    A swap is not instant: the old weapon holsters and the new one is brought
    up over several frames, and `Pawn.Weapon` keeps pointing at the old one
    until it completes. Without this check the next scan still sees a dry weapon
    in hand, issues the same switch again, and interrupts the swap in progress.
    Observed in play at roughly one scan interval apart:

        19:59:39.510  slot 1 -> 4
        19:59:39.844  slot 1 -> 4
        19:59:40.172  slot 1 -> 4

    Three interrupted swaps left the weapon unable to fire or zoom until the
    backpack was opened, which forces a re-equip.
    """
    pawn = caller.Pawn
    if pawn is None or pawn.InvManager is None:
        return False
    manager = pawn.InvManager
    if manager.PendingWeapon is not None:
        return True
    if bool(manager.InventoryTransitionInProgress()):
        # Covers ReadyBackpackInventory's own in-flight markers -
        # BackpackInventoryBeingEquipped and friends - which PendingWeapon and
        # IsPuttingDown do not. Without this, equipping from the backpack is a
        # network round trip: SendSlottedThingToBackpack swaps the pawn onto a
        # temporary "best weapon" (often one of the very dry weapons that
        # triggered this in the first place) while waiting for the server to
        # confirm the real one, so the very next scan sees a dry weapon in hand
        # again and issues ANOTHER equip on top of the one still resolving.
        # Observed in play: the same "equipping from backpack into 4" line
        # repeating once per scan for close to a minute straight.
        return True
    weapon = pawn.Weapon
    return weapon is not None and bool(weapon.IsPuttingDown())


def is_holding_fire(caller) -> bool:
    """Whether the player has primary fire held down right now.

    Two wrong signals were tried and ruled out against real play before this
    one:

    - `Weapon.PendingFire` self-clears the moment the weapon runs dry, so it
      reads false while the trigger is still physically held - which is always
      the case exactly when this mod wants to act.
    - `Controller.bFire` (`var input byte bFire`) is the classic UE3 mechanism,
      but this game does not use it - fire is bound to the `StartFire`/
      `StopFire` exec functions instead, so `bFire` sits at 0 permanently
      regardless of input. It read False in every logged switch, including ones
      the player confirmed were mid-fire, which is what exposed it as dead
      rather than merely unlucky.

    `WillowPlayerController.bWantsToFire` is what those two exec functions
    themselves maintain: StartFire(0) sets it true, StopFire(0) sets it false,
    unconditionally, before anything else in either function runs. That makes
    it the game's own record of the button state rather than a proxy for it.
    """
    return bool(getattr(caller, "bWantsToFire", False))


def pending_fire_modes(weapon):
    """Which fire modes the game still considers pending on this weapon.

    Note this is NOT "is the trigger physically down". A weapon that has run out
    of ammo ends its own firing state, so these read empty while the player is
    still holding the button - which is why an earlier log line reporting
    "fire held: False" was misleading rather than wrong.
    """
    try:
        modes = int(weapon.GetPendingFireLength())
        return [mode for mode in range(modes) if weapon.PendingFire(mode)]
    except Exception as ex:  # noqa: BLE001
        # Unreadable: report nothing pending so a switch is never blocked forever.
        logging.warning(f"[AutoLoot] could not read pending fire: {ex!r}")
        return []


def any_pending_fire(weapon) -> bool:
    return bool(pending_fire_modes(weapon))


def stop_firing(weapon):
    """End any held fire. Returns `(was_firing, is_clear)`.

    `was_firing` is reported so the caller can log what the state actually was
    at the moment of a switch - the only way to confirm from real play whether
    pending fire is what jams the next weapon.

    Swapping weapons while a fire mode is still pending leaves the next weapon
    unable to shoot or zoom until it is changed again by hand - the trigger is
    still logically down, but belongs to a weapon that has been put away. The
    engine's own swap does not clean that up, and a mod cannot change the engine,
    so end the fire first through the game's own ForceEndFire.

    Safe here because this is only ever done to a weapon that has just run out
    of ammo, so ending its fire cannot discharge a shot. If the fire is somehow
    still pending afterwards the caller must not switch - better a late switch
    than a jammed weapon.
    """
    if not any_pending_fire(weapon):
        return False, True
    weapon.ForceEndFire()
    return True, not any_pending_fire(weapon)


def equipped_weapons(caller):
    """The weapons in the quick slots, keyed by slot number.

    Only slots actually holding a weapon appear, so a character who has not yet
    unlocked slots 3 and 4 simply yields a shorter mapping and the search below
    skips past them.
    """
    pawn = caller.Pawn
    if pawn is None or pawn.InvManager is None:
        return {}

    by_slot = {}
    weapon = pawn.InvManager.InventoryChain
    while weapon is not None:
        slot = int(getattr(weapon, "QuickSelectSlot", 0))
        if slot in WEAPON_SLOTS:
            by_slot[slot] = weapon
        weapon = weapon.Inventory
    return by_slot


def next_loaded_slot(current, by_slot):
    """The next slot after `current` holding a loaded weapon, wrapping round.

    `current` is never the answer - it is the slot that just ran dry. Returns
    None when nothing else is loaded.
    """
    for offset in range(1, len(WEAPON_SLOTS)):
        slot = (current - 1 + offset) % len(WEAPON_SLOTS) + 1
        weapon = by_slot.get(slot)
        if weapon is not None and has_ammo(weapon):
            return slot
    return None


def loaded_backpack_weapons(caller, inventory_manager):
    """Every backpack weapon the player is carrying ammo for.

    player_has_ammo_for, not has_ammo: these are backpack weapons, with no
    ammo pool attached to answer HasAnyAmmo correctly.
    """
    return [
        item
        for item in inventory_manager.Backpack
        if item is not None
        and item.Class is not None
        and WEAPON_CLASS_TOKEN in item.Class.Name
        and player_has_ammo_for(caller, item)
    ]


def weapon_signature(weapon):
    """(ammo resource, elemental part) - what a weapon draws on to fire.

    Used to spread backup slots across different ammo pools and elements
    instead of refilling three dry-prone slots with the same kind of gun. Both
    are plain attribute reads off DefinitionData, the same as ammo_label uses
    for AmmoResource - never a function call. A native UFunction call here
    (StaticGetWeaponDamageType, tried first) crashed the game: a bad call
    fails outside Python's own exception handling, and this runs every scan
    for every equipped weapon, so a marshalling mismatch was hit constantly.
    ElementalPartDefinition is None for a non-elemental weapon.
    """
    weapon_type = weapon.DefinitionData.WeaponTypeDefinition
    ammo = None if weapon_type is None else weapon_type.AmmoResource
    element = getattr(weapon.DefinitionData, "ElementalPartDefinition", None)
    return (ammo, element)


def choose_backup_slot_weapon(caller, inventory_manager, avoid_signatures):
    """A random loaded backpack weapon, preferring one unlike what's already equipped.

    Only falls back to a signature already sitting in another slot when
    nothing else carries ammo - see refill_dry_backup_slots.
    """
    loaded = loaded_backpack_weapons(caller, inventory_manager)
    if not loaded:
        return None
    fresh = [item for item in loaded if weapon_signature(item) not in avoid_signatures]
    return random.choice(fresh or loaded)


# A backpack weapon is never readied directly into the ACTIVE slot. That goes
# through WillowInventoryManager.RemoveFromInventory's rough
# Instigator.IsActiveWeapon branch (OnUnequip + SwitchToBestWeapon), bypassing
# the ordinary put-down sequence entirely - confirmed in play to leave the
# weapon and crosshair dead afterwards even after clearing pending fire and
# waiting out every timing guess tried. So the active slot is only ever
# reached by EquipWeaponFromSlot (an ordinary switch), never by
# ReadyBackpackInventory - manage_weapon_ammo only switches to a slot that is
# already loaded, and auto_equip / refill_dry_backup_slots only ready
# inactive slots. Loading a fresh weapon into the CURRENT slot, if ever wanted
# again, needs the same care the game's own inventory screen takes and should
# not be re-added lightly.


def first_empty_weapon_slot(inventory_manager):
    """The lowest unlocked quick slot with no weapon in it, or None."""
    unlocked = min(int(inventory_manager.GetWeaponReadyMax()), len(WEAPON_SLOTS))
    for slot in WEAPON_SLOTS[:unlocked]:
        if inventory_manager.GetWeaponInSlot(slot) is None:
            return slot
    return None


def player_has_ammo_for(caller, weapon) -> bool:
    """Whether the player is carrying ammo of this weapon's type.

    HasAnyAmmo is only meaningful for a weapon that is actually equipped.
    AssociateAmmoPool hooks a weapon up to the player's pool when it is handed
    to the pawn, so one sitting in the backpack has no pool behind it and
    reports empty however many rockets you are carrying. Ask the player's own
    resource pool instead.

    Anything that cannot be determined counts as having ammo, so an unfamiliar
    weapon still gets equipped into an empty slot rather than silently passed
    over - which is exactly how the backpack check failed before.
    """
    weapon_type = weapon.DefinitionData.WeaponTypeDefinition
    resource = None if weapon_type is None else weapon_type.AmmoResource
    if resource is None:
        return True  # nothing to run out of, such as an overheating laser

    pool = caller.GetResourcePoolForResourceDefinition(resource, False)
    data = None if pool is None else pool.Data
    if data is None:
        return True
    return data.GetCurrentValue() > 0


def is_worth_equipping(caller, item) -> bool:
    """A backpack thing worth putting into an empty slot.

    Only the ammo rule is ours; whether the thing is equippable at all is left
    to the game, which knows about slot types the mod has never heard of.
    """
    if item is None or item.Class is None:
        return False
    if WEAPON_CLASS_TOKEN in item.Class.Name:
        return player_has_ammo_for(caller, item)
    return True


def fill_empty_equip_slots(caller):
    """Equip backpack gear into any equipment slot standing empty.

    InventoryShouldBeReadiedWhenEquipped is the game's own test for exactly
    this. It confirms the character can use the thing, that its equipment
    location is free - or for a weapon that a slot is both unlocked and unused,
    via CountReadiedWeapons() < GetWeaponReadyMax() - and refuses outright while
    riding a vehicle. Leaning on it means slot unlock state, class requirements
    and vehicles are the game's answers rather than the mod's guesses.
    """
    if not auto_equip.value:
        return

    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return

    # Snapshot and shuffle: equipping mutates the backpack being walked, and
    # which of several candidates fills a slot should not depend on where it
    # happens to sit in the bag.
    candidates = [
        item for item in inventory_manager.Backpack if is_worth_equipping(caller, item)
    ]
    random.shuffle(candidates)

    for item in candidates:
        try:
            # Asked again per item, so once a slot is filled the next candidate
            # for that same slot is turned down by the game rather than by us.
            if not inventory_manager.InventoryShouldBeReadiedWhenEquipped(item):
                continue
            if WEAPON_CLASS_TOKEN in item.Class.Name:
                slot = first_empty_weapon_slot(inventory_manager)
                if slot is None:
                    continue
                inventory_manager.ReadyBackpackInventory(item, slot)
            else:
                inventory_manager.ReadyBackpackInventory(item)
        except Exception as ex:  # noqa: BLE001
            logging.warning(f"[AutoLoot] could not equip from the backpack: {ex!r}")


def refill_dry_backup_slots(caller):
    """Swap a dry weapon in any slot but the active one for a loaded backpack one.

    Never touches the active slot - ReadyBackpackInventory on the slot
    currently in the player's hands is the one call proven to jam the weapon
    (see the comment above first_empty_weapon_slot). A dry weapon parked in a
    slot that is not being fired needs none of that care.
    """
    if not auto_equip.value:
        return

    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return

    pawn = caller.Pawn
    current = None if pawn is None else pawn.Weapon
    current_slot = None if current is None else int(getattr(current, "QuickSelectSlot", 0))

    by_slot = equipped_weapons(caller)
    signatures = {slot: weapon_signature(weapon) for slot, weapon in by_slot.items()}

    for slot, weapon in by_slot.items():
        if slot == current_slot or has_ammo(weapon):
            continue
        try:
            avoid = {sig for other_slot, sig in signatures.items() if other_slot != slot}
            chosen = choose_backup_slot_weapon(caller, inventory_manager, avoid)
            if chosen is None:
                continue
            inventory_manager.ReadyBackpackInventory(chosen, slot)
            signatures[slot] = weapon_signature(chosen)
        except Exception as ex:  # noqa: BLE001
            logging.warning(f"[AutoLoot] could not refill backup slot {slot}: {ex!r}")


def manage_weapon_ammo(caller):
    """Switch the player off a dry weapon and onto an equipped, loaded one.

    Never pulls a backpack weapon into the CURRENT slot - see the comment
    above first_empty_weapon_slot. refill_dry_backup_slots keeps the other
    slots topped up from the backpack instead, so there is almost always
    something loaded here to switch to.
    """
    if not switch_when_empty.value:
        return

    # Leave an in-flight swap alone. Issuing another one interrupts it, and
    # repeating that jams the weapon.
    if weapon_swap_in_progress(caller):
        return

    pawn = caller.Pawn
    current = None if pawn is None else pawn.Weapon
    if current is None or has_ammo(current):
        return

    current_slot = int(getattr(current, "QuickSelectSlot", 0))
    if current_slot not in WEAPON_SLOTS:
        return

    by_slot = equipped_weapons(caller)
    loaded_slot = next_loaded_slot(current_slot, by_slot)
    if loaded_slot is None:
        return

    modes = pending_fire_modes(current)
    _was_firing, is_clear = stop_firing(current)
    # Logged every time: switches are rare, and this line is the only
    # record of the real state when one happened. Repeats of the same
    # switch a scan apart are the signature of an interrupted swap.
    logging.info(
        f"[AutoLoot] slot {current_slot} -> {loaded_slot}"
        f" | pending fire modes: {modes} | cleared: {is_clear}"
        f"{'' if is_clear else ' | POSTPONED'}"
    )
    if is_clear:
        caller.EquipWeaponFromSlot(loaded_slot)


def dist(a, b) -> float:
    return math.sqrt((b.X - a.X) ** 2 + (b.Y - a.Y) ** 2 + (b.Z - a.Z) ** 2)


# Class-name token paired with the label the summary lists it under. Weapons are
# absent deliberately: they are grouped by ammo type instead, and that comes from
# the game rather than from any table here, because BL2 and TPS do not share the
# same ammo (TPS adds lasers) and either could be modded to add more.
GEAR_CATEGORIES = (
    (SHIELD_CLASS_TOKEN, "Shields"),
    (GRENADE_CLASS_TOKEN, "Grenade Mods"),
    (CLASS_MOD_CLASS_TOKEN, "Class Mods"),
    (ARTIFACT_CLASS_TOKEN, ARTIFACT_LABEL),
    (CUSTOMIZATION_CLASS_TOKEN, "Customizations"),
)

# Plain ASCII: the HUD's Scaleform font has no glyph for a middle dot and drew a
# square box for it instead. Anything beyond ASCII here needs checking in game.
SUMMARY_SEPARATOR = " | "


def prettify_ammo_name(name: str) -> str:
    """Turn a resource's object name into something readable.

    The game is not consistent about it - both `Ammo_Combat_Rifle` and
    `Ammo_CombatRifle` appear - so handle underscores and camel case, and give
    back `Combat Rifle` either way.
    """
    name = str(name).removeprefix("Ammo_").replace("_", " ")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def ammo_label(weapon) -> str:
    """Which ammo pool this weapon draws from.

    Read off the weapon's own AmmoResource rather than mapped from a weapon-type
    enum: the enum's members differ per game (index 6 is WT_Laser in TPS but
    WT_MAX in BL2), so a fixed table would confidently mislabel one of them.
    """
    weapon_type = weapon.DefinitionData.WeaponTypeDefinition
    resource = None if weapon_type is None else weapon_type.AmmoResource
    if resource is None:
        return "Other"
    return prettify_ammo_name(resource.Name)


def item_kind(item):
    """The group an item is compared and counted within.

    Weapons group by ammo type, everything else by its category. None for
    anything the mod does not track.
    """
    if item is None or item.Class is None:
        return None
    class_name = item.Class.Name
    if WEAPON_CLASS_TOKEN in class_name:
        return ammo_label(item)
    return next(
        (label for token, label in GEAR_CATEGORIES if token in class_name),
        None,
    )


def item_level(item):
    """The item's level, or None when it has no level worth comparing.

    GameStage is the level an item is generated at and what its card shows.
    Customizations are excluded outright - they are graded by whether you have
    unlocked them, not by level, and already have their own setting.
    """
    if item is None or item.Class is None:
        return None
    if CUSTOMIZATION_CLASS_TOKEN in item.Class.Name:
        return None
    definition_data = item.DefinitionData
    if definition_data is None:
        return None
    stage = getattr(definition_data, "GameStage", None)
    if stage is None or stage <= 0:
        return None
    return stage


def character_level(caller):
    """The player's own level, or None when it cannot be read."""
    info = caller.PlayerReplicationInfo
    if info is None:
        return None
    level = int(info.ExpLevel)
    return level if level > 0 else None


# WillowInventory.PlayerMark. Derived from the game's own toggle, which sets 2
# and then shows TF_Favorite, and sets 1 and then shows TF_Standard.
MARK_FAVORITE = 2


def item_price(item) -> int:
    """What a vendor would pay, used only to break a tie between equal levels."""
    try:
        return int(item.GetMonetaryValue())
    except Exception:  # noqa: BLE001
        return 0


def is_favorite(item) -> bool:
    try:
        return int(item.GetMark()) == MARK_FAVORITE
    except Exception:  # noqa: BLE001
        # If the mark cannot be read, treat it as precious rather than risk
        # throwing away something the player deliberately kept.
        return True


def backpack_is_full(caller) -> bool:
    inventory_manager = caller.GetPawnInventoryManager()
    return (
        inventory_manager is not None and inventory_manager.GetEmptyBackpackSlots() <= 0
    )


def usability_rank(item, cap):
    """How little this item is worth keeping right now: smaller sorts first.

    Above your own level (`cap`), an item is dead weight - you cannot equip
    it until you level up to it, so it is worth nothing right now no matter
    how high its number looks. Every over-level item ranks below every
    usable one, and among over-level items the ONE FURTHEST above your level
    ranks lowest, since it is the one you are furthest from ever being able
    to use - keeping the ones closer to your level instead means the item
    nearest to becoming useful is the one lost. Only once nothing is
    over-level does level ordering flip back to the old rule: the weakest
    USABLE item ranks lowest.

        character level 28, backpack: 29, 29, 31, 24
        over-level group (ranks lowest, worth least): 31, then the 29s
        at-or-under group (only ranks lowest once the above is empty): 24

    Shared by choose_item_to_drop, deciding what to sacrifice, and the
    never-trade-down check in player_tick, deciding whether a pickup is
    actually better than what it would cost - the same ranking must answer
    both, or an over-level item could rank "worse" for one purpose and
    "better" for the other.
    """
    level = item_level(item) or 0
    over_level = cap is not None and level > cap
    return (0, -level) if over_level else (1, level)


def choose_item_to_drop(caller):
    """The item that would be thrown out to make room, or None.

    Split from the throwing so the decision can be checked against a live game
    without changing anything - see verify_autoloot.py.
    """
    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return None

    by_kind = {}
    for item in inventory_manager.Backpack:
        kind = item_kind(item)
        if kind is None or is_favorite(item) or not caller.CanDrop(item):
            continue
        by_kind.setdefault(kind, []).append(item)

    if not by_kind:
        return None

    # Most numerous kind, then within it the item worth least right now.
    # Every tie is broken by something stable so the same backpack always gives
    # up the same item rather than whichever happened to be looked at first.
    fullest = min(by_kind, key=lambda kind: (-len(by_kind[kind]), kind))
    cap = character_level(caller)
    return min(
        by_kind[fullest],
        key=lambda item: (usability_rank(item, cap), item_price(item), unique_id_of(item) or 0),
    )


def throw_backpack_item(caller, item) -> bool:
    """Throw a specific backpack item out. Returns whether a slot was freed.

    ThrowBackpackInventory, not WillowPlayerController.ThrowInventory. The game
    uses two different calls and the inventory screen picks between them:
    ThrowInventory is for the equipped item and its server half refuses unless
    `InventoryObject.Owner == pawn`, which a backpack item never satisfies - so
    it silently did nothing at all here. ThrowBackpackInventory looks the item
    up in Backpack and is the one that works.

    Judge success by the backpack shrinking rather than by inspecting the item
    afterwards - it has been handed to the engine by then.

    Marks the item seen before throwing it, regardless of whether
    update_seen_ids already covered it this tick. update_seen_ids runs once,
    at the top of player_tick, before any pickups happen - an item picked up
    earlier in that same pass, then chosen as the worst to drop for a later
    one, was never owned at that point and so was never recorded. Without
    this, its dropped copy reads as brand new loot, gets picked back up, and
    - if the backpack is still full - promptly becomes the next worst item
    to throw, cycling forever instead of settling after one drop.
    """
    unique_id = unique_id_of(item)
    if unique_id is not None:
        seen_unique_ids.add(unique_id)

    inventory_manager = caller.GetPawnInventoryManager()
    count_before = len(inventory_manager.Backpack)
    inventory_manager.ThrowBackpackInventory(item)
    count_after = len(inventory_manager.Backpack)
    freed = count_after < count_before
    if freed:
        # Rare enough (only when the backpack is actually full) to be worth a
        # line every time, rather than only on failure - it is exactly the
        # kind of "AutoLoot just threw away something I owned" event a player
        # wants a record of.
        logging.warning(
            f"[AutoLoot] dropped {item_kind(item)} (lvl {item_level(item)}) to"
            " make room"
        )
    else:
        # If ThrowBackpackInventory turns out to be deferred rather than
        # synchronous (a network RPC resolved next tick rather than inline),
        # this is exactly what that would look like: the count read back
        # immediately still shows the old size even though the drop is really
        # in flight. Logging the raw counts lets that be told apart from a
        # genuine refusal (e.g. the game itself blocking the drop) after the
        # fact, rather than guessed at.
        logging.warning(
            f"[AutoLoot] tried to drop {item_kind(item)} (lvl {item_level(item)})"
            f" to free a backpack slot, but Backpack size stayed at {count_after}"
            " right after the call"
        )
    return freed


def backpack_tally(caller):
    """Count the backpack, ammo types and gear categories together in one tally.

    One tally rather than two, because the summary lists them as a single run:
    keeping them apart would only let the two halves be ordered by different
    rules. Returns `(counts, levels, used, capacity)`, or None if there is no
    inventory to read. `used` and `capacity` are the game's own two numbers
    rather than anything derived here. `levels` holds each kind's `(min, max)`
    item level seen, skipping items with no level to compare.
    """
    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return None

    counts = {}
    levels = {}
    for item in inventory_manager.Backpack:
        label = item_kind(item)
        if label is None:
            continue
        counts[label] = counts.get(label, 0) + 1
        level = item_level(item)
        if level is None:
            continue
        low, high = levels.get(label, (level, level))
        levels[label] = (min(low, level), max(high, level))

    return (
        counts,
        levels,
        inventory_manager.CountUnreadiedInventory(),
        inventory_manager.GetUnreadiedInventoryMaxSize(),
    )


def summary_line(counts, levels) -> str:
    """One line of `N Label (lvl X-Y)`, most first, empty categories left out.

    Ties break randomly rather than on the label, so no one category is always
    stuck last in a tied group - the count is what the player asked to sort by,
    and everything below that is genuinely arbitrary.
    """
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], random.random()))
    parts = []
    for label, count in ordered:
        if not count:
            continue
        level_range = levels.get(label)
        if level_range is None:
            parts.append(f"{count} {label}")
            continue
        low, high = level_range
        span = f"lvl {low}" if low == high else f"lvl {low}-{high}"
        parts.append(f"{count} {label} ({span})")
    return SUMMARY_SEPARATOR.join(parts)


def format_tally(counts, levels, used, capacity):
    """A title plus one line holding everything, biggest group first."""
    return f"Backpack  {used}/{capacity}", summary_line(counts, levels)


def report_backpack(caller):
    global last_shown_body
    if not (hud_summary_seconds.value or summary_in_console.value):
        return
    try:
        tally = backpack_tally(caller)
        if tally is None:
            return
        title, body = format_tally(*tally)
        if not body:
            return
        if summary_in_console.value:
            logging.info(f"{title}\n{body}")
        if hud_summary_seconds.value:
            if body == last_shown_body:
                # Identical to what's already on screen. show_hud_message
                # always clears the training-text box before re-adding to
                # it, so calling it again here would visibly flicker/restart
                # an unchanged message rather than smoothly extend it - do
                # nothing instead. A genuine change (a pickup or drop while
                # still looking at the same item, say) always has a
                # different body and redraws normally.
                return
            show_hud_message(title, body, hud_summary_seconds.value)
            last_shown_body = body
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"[AutoLoot] could not summarise the backpack: {ex!r}")


@hook("WillowGame.WillowPlayerController:PlayerTick", Type.POST)
def player_tick(obj, _args, _ret, _func):
    global ticks_until_scan, picking_up, collected_last_pass, next_near_loot_report

    ticks_until_scan -= 1
    if ticks_until_scan > 0:
        return

    # PlayerTick also fires while a level is still loading, before the pawn
    # exists. Nothing below is meaningful then - there is no one to give loot
    # to, and CalcViewActorLocation has no pawn to read a viewpoint from.
    willow_globals = obj.GetWillowGlobals()
    if obj.Pawn is None or willow_globals is None:
        ticks_until_scan = SCAN_INTERVAL
        return

    # Before looting: an empty gun is more urgent than a pickup, and this runs
    # whether or not there is anything on the ground.
    try:
        # Fill empty slots first, so a gun that lands in one is available to the
        # dry-weapon logic below on this same pass.
        fill_empty_equip_slots(obj)
        refill_dry_backup_slots(obj)
        manage_weapon_ammo(obj)
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"[AutoLoot] could not manage equipment: {ex!r}")

    update_seen_ids(obj)

    max_dist = (
        willow_globals.GetGlobalsDefinition().PlayerInteractionDistance
        * range_percent.value
        / 100
    )
    view_location = obj.CalcViewActorLocation
    # Read once per pass: it cannot change mid-pass, and choose_item_to_drop
    # and the never-trade-down check below must agree on the same value.
    cap = character_level(obj)

    # Re-uses hud_summary_seconds as its own re-trigger interval - once a
    # shown summary would have faded, standing near loot shows it again,
    # rather than adding a separate duration to configure.
    try:
        now = obj.WorldInfo.TimeSeconds
        if now >= next_near_loot_report:
            in_range = any(
                pickup.Inventory is not None
                and dist(pickup.Location, view_location) <= max_dist
                for pickup in willow_globals.PickupList
            )
            if in_range:
                report_backpack(obj)
                next_near_loot_report = now + hud_summary_seconds.value
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"[AutoLoot] could not check for nearby loot: {ex!r}")

    collected_any = False
    # Set once this pass drops something to make room. The engine's own
    # fullness check has been confirmed (via the warning below) to still
    # report full immediately after ThrowBackpackInventory has already
    # shrunk Backpack, so a second item in the same pile must not pay for
    # a drop whose slot has not registered yet - see freed_this_pass below.
    freed_any_slot = False

    # Snapshot the array before touching it. A successful pickup destroys the
    # WillowPickup, and WillowPickup.Destroyed() calls WillowGlobals.RemovePickup,
    # so iterating the live list shifts every later entry down one and skips
    # whatever followed each item taken. That, rather than any per-tick limit, is
    # why walking over a pile of loot only ever collected part of it.
    for pickup in list(willow_globals.PickupList):
        try:
            inventory = pickup.Inventory
            if inventory is None or inventory.Class is None:
                continue
            if unique_id_of(inventory) in seen_unique_ids:
                continue
            if dist(pickup.Location, view_location) > max_dist:
                continue
            # Last, because deciding about a customization calls into the game.
            if not should_pickup(inventory, obj):
                continue
            # Only once we have decided we want this one, so a slot is never
            # given up for loot we were going to walk past anyway.
            was_full = backpack_is_full(obj)
            if drop_lowest_when_full.value and was_full:
                worst = choose_item_to_drop(obj)
                if worst is None:
                    logging.warning(
                        "[AutoLoot] backpack is full and nothing in it can be"
                        " dropped (all favourited or undroppable) - pickups"
                        " will keep failing until something is freed up"
                        " manually"
                    )
                else:
                    if usability_rank(inventory, cap) < usability_rank(worst, cap):
                        # Never trade down: dropping worst to make room for
                        # something ranked even lower would be a pure loss -
                        # same ranking choose_item_to_drop used to pick worst
                        # in the first place, so an over-level worst never
                        # looks "better" here than an on-level pickup just
                        # because its raw number happens to be lower. Leave
                        # this one on the ground.
                        continue
                    # Dropping something does not make room in time for THIS
                    # SAME pickup attempt - confirmed in play, a slot that
                    # Backpack itself already shows as freed still reads back
                    # as full to whatever check gates the pickup (see the
                    # warning below, from before this was added). Wait for
                    # the next tick, which the fast rescan below brings very
                    # soon, rather than attempt a doomed pickup now. If the
                    # throw itself failed, fall through and attempt anyway -
                    # the bottom warning covers that genuinely-stuck case.
                    if throw_backpack_item(obj, worst):
                        freed_any_slot = True
                        continue

            # A collected pickup is destroyed inside this call, and Destroyed()
            # takes it out of PickupList. Read success off the list's length
            # rather than off the pickup: touching a property of an actor the
            # engine has just destroyed reads memory we no longer own, and the
            # answer is the same either way. This only decides whether to rescan
            # early - a refusal must not pin us to the fast interval.
            count_before = len(willow_globals.PickupList)

            picking_up = True
            try:
                obj.PickupPickupable(pickup, False)
            finally:
                picking_up = False

            picked_up = len(willow_globals.PickupList) < count_before
            if picked_up:
                collected_any = True
            elif was_full:
                # Only reached with nothing freed this iteration - dropping is
                # off, nothing droppable existed, or the throw itself failed
                # (each already logged its own reason above). The one path
                # whose failure is deliberately suppressed
                # (block_pickup_failed_message / block_failed_pickup), so
                # without this line a rejected pickup here leaves no trace at
                # all - it just looks like the loot was never seen.
                logging.warning(
                    f"[AutoLoot] wanted {item_kind(inventory)} (lvl"
                    f" {item_level(inventory)}) but backpack was full and"
                    f" nothing could be freed for it"
                )
        except Exception as ex:  # noqa: BLE001
            # One bad entry must not abandon the rest of the pass.
            logging.warning(f"[AutoLoot] skipped a pickup: {ex!r}")

    # After collecting, so anything picked up this pass is used straight away and
    # the summary below already reflects it being gone.
    use_customizations(obj)

    ticks_until_scan = FAST_SCAN_INTERVAL if (collected_any or freed_any_slot) else SCAN_INTERVAL

    # Report when the pile is finished rather than per item. show_hud_message
    # drops messages shown too close together, and one message per gun would be
    # unreadable anyway. A collecting pass rescans on the very next tick, so the
    # first pass that takes nothing lands almost immediately after the last one
    # that did.
    if collected_any:
        collected_last_pass = True
    elif collected_last_pass:
        collected_last_pass = False
        report_backpack(obj)


# Suppress the "inventory full" toast and the item's rejection hop, but only for
# our own attempts - picking something up by hand should still report normally.
# These used to return a bool, which pyunrealsdk ignores: it blocks a function
# only on the Block sentinel, so neither was ever actually suppressed.
@hook("WillowGame.WillowPlayerController:ClientDisplayPickupFailedMessage", Type.PRE)
def block_pickup_failed_message(_obj, _args, _ret, _func):
    return Block if picking_up else None


@hook("WillowGame.WillowPickup:FailedPickup", Type.PRE)
def block_failed_pickup(_obj, _args, _ret, _func):
    return Block if picking_up else None


# Listed explicitly, and in the order they should appear. Left to itself,
# build_mod discovers options with inspect.getmembers, which sorts by *variable
# name* - which put "hud_summary_seconds" first in the menu and
# "summary_in_console" last, with everything else between the two halves of one
# setting. Anything added here must be added to this list or it never renders,
# which test_every_option_appears_in_the_menu_list checks.
MOD_OPTIONS = [
    pickup_weapons,
    pickup_shields,
    pickup_grenades,
    pickup_artifacts,
    pickup_class_mods,
    pickup_customizations,
    auto_use_customizations,
    drop_lowest_when_full,
    auto_equip,
    switch_when_empty,
    range_percent,
    hud_summary_seconds,
    summary_in_console,
]

build_mod(options=MOD_OPTIONS)
