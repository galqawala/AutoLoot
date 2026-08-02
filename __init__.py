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
    10,
    0,
    60,
    1,
    True,
    description=(
        "After clearing a pile of loot, show what your backpack now holds on"
        " screen, for this many seconds. Set to 0 to not show it on screen at"
        " all. Shown once the pile is cleared, not once per item."
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
fill_empty_slots = BoolOption(
    "Fill Empty Equipment Slots",
    True,
    description=(
        "Equip something from your backpack into any equipment slot standing"
        " empty - a weapon slot, shield, grenade mod, class mod or artifact."
        " Only weapons that have ammo are used. Full slots are left alone."
    ),
)
switch_when_empty = BoolOption(
    "Switch Weapon When Out Of Ammo",
    True,
    description=(
        "When the gun in your hands runs dry, switch to the next slot holding"
        " one that still has ammo, wrapping round from slot 4 back to slot 1."
        " Nothing happens if none of your equipped weapons has any."
    ),
)
equip_when_all_empty = BoolOption(
    "Equip From Backpack When All Empty",
    True,
    description=(
        "When every weapon you have equipped is out of ammo, pull a loaded one"
        " out of your backpack into the slot you are holding."
    ),
)
drop_lowest_when_full = BoolOption(
    "Drop Lowest Level When Full",
    True,
    description=(
        "When your backpack is full and there is loot worth taking, throw out"
        " the worst of whatever kind is filling it most - the lowest level of"
        " those, cheapest first if they tie. Anything you have marked as a"
        " favourite is never thrown out."
    ),
)
pick_lower_level = BoolOption(
    "Pick Lower Level",
    False,
    description=(
        "Collect gear weaker than the best of its kind you already carry or have"
        " equipped. Off by default, so a level 2 pistol is left behind once you"
        " hold a level 3 one, but taken while your best is level 2 or lower."
        " Weapons compare within their own ammo type. Things without a level,"
        " such as customizations, are never affected."
    ),
)
auto_use_customizations = BoolOption(
    "Auto Use Customizations",
    True,
    description=(
        "Use customizations as soon as you pick them up, unlocking the skin or"
        " head without a trip to the backpack. Note this consumes the item, so"
        " you cannot pass it to another player afterwards."
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
    100,
    500,
    25,
    True,
    description=(
        "How far AutoLoot reaches, against your normal pickup range. 100% is"
        " the standard distance, 200% is twice as far. Only automatic pickups"
        " are affected - picking things up yourself still works as before."
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
            logging.dev_warning(f"[AutoLoot] could not use a customization: {ex!r}")


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
        logging.dev_warning(f"[AutoLoot] could not read pending fire: {ex!r}")
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


def next_occupied_slot(current, by_slot):
    """The next slot after `current` holding any weapon at all, wrapping round.

    Used only to pick a slot to bounce through on the way to a backpack
    re-equip - the weapon parked there does not need ammo, since it is left
    again immediately, unlike next_loaded_slot which is choosing where to
    actually fight from.
    """
    for offset in range(1, len(WEAPON_SLOTS)):
        slot = (current - 1 + offset) % len(WEAPON_SLOTS) + 1
        if slot in by_slot:
            return slot
    return None


def choose_loaded_backpack_weapon(caller, inventory_manager):
    """A random backpack weapon the player is carrying ammo for, or None.

    player_has_ammo_for, not has_ammo: these are backpack weapons, with no
    ammo pool attached to answer HasAnyAmmo correctly.
    """
    loaded = [
        item
        for item in inventory_manager.Backpack
        if item is not None
        and item.Class is not None
        and WEAPON_CLASS_TOKEN in item.Class.Name
        and player_has_ammo_for(caller, item)
    ]
    return random.choice(loaded) if loaded else None


def equip_loaded_weapon_from_backpack(caller, slot) -> bool:
    """Move a loaded backpack weapon directly into `slot`.

    Fallback only, for the rare character with a single weapon slot unlocked,
    where there is no other slot to bounce a re-equip through - see
    start_backpack_reequip for the normal case, and for why replacing the
    ACTIVE slot directly is best avoided. Still refuses while the player is
    holding fire, since here - and only here - the slot being replaced really
    is the one in their hands.
    """
    if is_holding_fire(caller):
        return False

    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return False

    chosen = choose_loaded_backpack_weapon(caller, inventory_manager)
    if chosen is None:
        return False

    inventory_manager.ReadyBackpackInventory(chosen, slot)
    watch_post_equip_transition(chosen)
    return True


# Pulling a weapon out of the backpack into the ACTIVE slot goes through
# WillowInventoryManager.RemoveFromInventory's rough Instigator.IsActiveWeapon
# branch (OnUnequip + SwitchToBestWeapon), bypassing the ordinary put-down
# sequence entirely. Every attempt to detect or wait out a bad moment to do
# that anyway - clearing pending fire, waiting for the real fire flag to
# clear, extending how long a diagnostic watch ran - still left the weapon and
# crosshair dead afterwards, confirmed by a hook directly on StartFire showing
# the engine trying to fire a weapon that read perfectly healthy by every
# other measure. So the active slot is never handed to ReadyBackpackInventory
# again. Instead, bounce through the next equipped slot first - the exact
# operation switch_when_empty already relies on, which has never jammed even
# under sustained fire - load the backpack weapon into the now INACTIVE
# original slot, then switch back into it the same safe way:
#
#     stage             action just issued              waiting for
#     ----------------  ------------------------------  ------------------------
#     SWITCHING_AWAY    EquipWeaponFromSlot(temp)        weapon_swap_in_progress
#     READYING          ReadyBackpackInventory(W, orig)  weapon_swap_in_progress
#     SWITCHING_BACK    EquipWeaponFromSlot(orig)        weapon_swap_in_progress
#
# `orig` is not the active slot again until SWITCHING_BACK's own wait clears,
# so RemoveFromInventory's active-weapon branch has nothing to trigger on
# during READYING.
_reequip: dict = {"stage": None, "temp_slot": None, "orig_slot": None, "item": None}


def start_backpack_reequip(caller, orig_slot, temp_slot, item):
    caller.EquipWeaponFromSlot(temp_slot)
    _reequip.update(
        stage="SWITCHING_AWAY", temp_slot=temp_slot, orig_slot=orig_slot, item=item
    )
    logging.info(
        f"[AutoLoot] re-equip: bouncing through slot {temp_slot} to safely load"
        f" a backpack weapon into slot {orig_slot}"
    )


def advance_backpack_reequip(caller) -> bool:
    """Step the pending re-equip sequence, if one is running.

    Returns whether a sequence is active, so the caller can skip its own
    empty-weapon checks entirely while one is in flight. Starting a second
    sequence - or running the ordinary dry-weapon logic - on top of one
    already under way is exactly the overlap that caused the original
    repeat-storm bug this whole mechanism replaced.
    """
    stage = _reequip["stage"]
    if stage is None:
        return False

    if weapon_swap_in_progress(caller):
        return True

    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        _reequip["stage"] = None
        return False

    if stage == "SWITCHING_AWAY":
        item = _reequip["item"]
        if item not in inventory_manager.Backpack:
            logging.dev_warning("[AutoLoot] re-equip: item left the backpack, abandoning")
            _reequip["stage"] = None
            return False
        inventory_manager.ReadyBackpackInventory(item, _reequip["orig_slot"])
        _reequip["stage"] = "READYING"
        return True

    if stage == "READYING":
        caller.EquipWeaponFromSlot(_reequip["orig_slot"])
        watch_post_equip_transition(_reequip["item"])
        _reequip["stage"] = "SWITCHING_BACK"
        return True

    if stage == "SWITCHING_BACK":
        logging.info("[AutoLoot] re-equip: complete")
        _reequip["stage"] = None
        return False

    return False


# Diagnostic only. Two guesses at what jams the newly-equipped weapon (fire
# held at the moment of the switch, then an overlapping second equip) were both
# ruled out in play: it still jammed after correctly waiting for the fire
# button to be released, with no repeated equip in the log. A third round of
# polling every state variable for 2 seconds came back entirely clean, but the
# player only tried firing again after that window had already closed - so
# "clean for 2 seconds" was never actually evidence about the moment that
# mattered. Extended to 10 seconds, and paired with a hook directly on
# StartFire below, which captures the exact instant fire is attempted instead
# of guessing whether a timer happened to land on it. Read-only throughout: it
# only calls logging and getattr.
_post_equip_watch = {"passes_left": 0, "wanted": None}
POST_EQUIP_WATCH_PASSES = 30


def watch_post_equip_transition(wanted_item):
    _post_equip_watch["passes_left"] = POST_EQUIP_WATCH_PASSES
    _post_equip_watch["wanted"] = getattr(wanted_item.Class, "Name", "?")


def report_post_equip_transition(caller):
    if _post_equip_watch["passes_left"] <= 0:
        return
    _post_equip_watch["passes_left"] -= 1

    pawn = caller.Pawn
    manager = caller.GetPawnInventoryManager()
    current = None if pawn is None else pawn.Weapon
    current_name = "none" if current is None else current.Class.Name
    still_transitioning = None
    if manager is not None:
        try:
            still_transitioning = bool(manager.InventoryTransitionInProgress())
        except Exception as ex:  # noqa: BLE001
            still_transitioning = f"<unreadable: {ex!r}>"

    logging.info(
        f"[AutoLoot] post-equip watch ({POST_EQUIP_WATCH_PASSES - _post_equip_watch['passes_left']}"
        f"/{POST_EQUIP_WATCH_PASSES}): wanted {_post_equip_watch['wanted']}"
        f" | now holding {current_name}"
        f" | HasAnyAmmo: {None if current is None else has_ammo(current)}"
        f" | IsPuttingDown: {None if current is None else bool(current.IsPuttingDown())}"
        f" | InventoryTransitionInProgress: {still_transitioning}"
        f" | bWantsToFire: {is_holding_fire(caller)}"
    )


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
    if not fill_empty_slots.value:
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
            logging.dev_warning(f"[AutoLoot] could not equip from the backpack: {ex!r}")


def manage_weapon_ammo(caller):
    """Get a loaded weapon into the player's hands when the current one is dry."""
    # Serviced first and unconditionally: a re-equip already under way must be
    # allowed to finish even if a setting changed mid-sequence, and nothing
    # below may start a second one on top of it.
    if advance_backpack_reequip(caller):
        return

    if not (switch_when_empty.value or equip_when_all_empty.value):
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
    if loaded_slot is not None:
        # Something equipped still has ammo, so the backpack is not consulted -
        # that is what makes the second option "when *all* are empty".
        if switch_when_empty.value:
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
        return

    if not equip_when_all_empty.value:
        return

    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return
    chosen = choose_loaded_backpack_weapon(caller, inventory_manager)
    if chosen is None:
        return

    temp_slot = next_occupied_slot(current_slot, by_slot)
    if temp_slot is None:
        # Only one weapon slot unlocked, so there is nothing to bounce
        # through - fall back to the direct, riskier equip rather than do
        # nothing at all.
        equip_loaded_weapon_from_backpack(caller, current_slot)
        return

    start_backpack_reequip(caller, current_slot, temp_slot, chosen)


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


def best_owned_levels(caller):
    """The highest level held of each kind, across backpack and equipped gear.

    Each is capped at the character's own level. Gear above your level is not
    something you can use yet, so letting one lucky over-level drop set the bar
    would have you walking past every weapon that actually suits you - which is
    precisely what happened.
    """
    cap = character_level(caller)
    best = {}
    for item in iter_owned_inventory(caller):
        level = item_level(item)
        if level is None:
            continue
        kind = item_kind(item)
        if kind is None:
            continue
        if cap is not None:
            level = min(level, cap)
        if level > best.get(kind, 0):
            best[kind] = level
    return best


def is_worth_taking(inventory, best_levels) -> bool:
    """Whether this is at least as good as the best of its kind already held.

    Anything without a level, or of a kind the mod does not track, is always
    worth taking - the rule simply does not apply to it. Holding none of a kind
    leaves the best at 0, so the first one is always collected.
    """
    level = item_level(inventory)
    if level is None:
        return True
    kind = item_kind(inventory)
    if kind is None:
        return True
    return level >= best_levels.get(kind, 0)


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

    # Most numerous kind, then within it the lowest level, cheapest, oldest.
    # Every tie is broken by something stable so the same backpack always gives
    # up the same item rather than whichever happened to be looked at first.
    fullest = min(by_kind, key=lambda kind: (-len(by_kind[kind]), kind))
    return min(
        by_kind[fullest],
        key=lambda item: (
            item_level(item) or 0,
            item_price(item),
            unique_id_of(item) or 0,
        ),
    )


def free_a_backpack_slot(caller) -> bool:
    """Throw out the worst item of whatever kind is filling the backpack most.

    Returns whether a slot was actually freed. Only the backpack is considered,
    never equipped gear, and never anything marked as a favourite.
    """
    worst = choose_item_to_drop(caller)
    if worst is None:
        return False

    inventory_manager = caller.GetPawnInventoryManager()

    # ThrowBackpackInventory, not WillowPlayerController.ThrowInventory. The game
    # uses two different calls and the inventory screen picks between them:
    # ThrowInventory is for the equipped item and its server half refuses unless
    # `InventoryObject.Owner == pawn`, which a backpack item never satisfies - so
    # it silently did nothing at all here. ThrowBackpackInventory looks the item
    # up in Backpack and is the one that works.
    #
    # Judge success by the backpack shrinking rather than by inspecting the item
    # afterwards - it has been handed to the engine by then.
    count_before = len(inventory_manager.Backpack)
    inventory_manager.ThrowBackpackInventory(worst)
    return len(inventory_manager.Backpack) < count_before


def backpack_tally(caller):
    """Count the backpack, ammo types and gear categories together in one tally.

    One tally rather than two, because the summary lists them as a single run:
    keeping them apart would only let the two halves be ordered by different
    rules. Returns `(counts, used, capacity)`, or None if there is no inventory
    to read. `used` and `capacity` are the game's own two numbers rather than
    anything derived here.
    """
    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return None

    counts = {}
    for item in inventory_manager.Backpack:
        label = item_kind(item)
        if label is None:
            continue
        counts[label] = counts.get(label, 0) + 1

    return (
        counts,
        inventory_manager.CountUnreadiedInventory(),
        inventory_manager.GetUnreadiedInventoryMaxSize(),
    )


def summary_line(counts) -> str:
    """One line of `Label N`, most first, empty categories left out.

    Ties break on the label so the same backpack always reads the same way -
    otherwise the order would depend on where things happen to sit in the bag.
    """
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return SUMMARY_SEPARATOR.join(f"{label} {count}" for label, count in ordered if count)


def format_tally(counts, used, capacity):
    """A title plus one line holding everything, biggest group first."""
    return f"Backpack  {used}/{capacity}", summary_line(counts)


def report_backpack(caller):
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
            show_hud_message(title, body, hud_summary_seconds.value)
    except Exception as ex:  # noqa: BLE001
        logging.dev_warning(f"[AutoLoot] could not summarise the backpack: {ex!r}")


@hook("WillowGame.WillowPlayerController:PlayerTick", Type.POST)
def player_tick(obj, _args, _ret, _func):
    global ticks_until_scan, picking_up, collected_last_pass

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
        manage_weapon_ammo(obj)
        report_post_equip_transition(obj)
    except Exception as ex:  # noqa: BLE001
        logging.dev_warning(f"[AutoLoot] could not manage equipment: {ex!r}")

    update_seen_ids(obj)

    max_dist = (
        willow_globals.GetGlobalsDefinition().PlayerInteractionDistance
        * range_percent.value
        / 100
    )
    view_location = obj.CalcViewActorLocation
    collected_any = False
    # Once per pass rather than per pickup: the answer cannot change until we
    # actually collect something, and it walks the whole inventory.
    best_levels = {} if pick_lower_level.value else best_owned_levels(obj)

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
            if not is_worth_taking(inventory, best_levels):
                continue
            # Only once we have decided we want this one, so a slot is never
            # given up for loot we were going to walk past anyway.
            if drop_lowest_when_full.value and backpack_is_full(obj):
                free_a_backpack_slot(obj)

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

            if len(willow_globals.PickupList) < count_before:
                collected_any = True
        except Exception as ex:  # noqa: BLE001
            # One bad entry must not abandon the rest of the pass.
            logging.dev_warning(f"[AutoLoot] skipped a pickup: {ex!r}")

    # After collecting, so anything picked up this pass is used straight away and
    # the summary below already reflects it being gone.
    use_customizations(obj)

    ticks_until_scan = FAST_SCAN_INTERVAL if collected_any else SCAN_INTERVAL

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


# Diagnostic only, and gated: this fires every time the player pulls the
# trigger, so it only logs while a post-equip watch is active (see
# equip_loaded_weapon_from_backpack), to stay silent during ordinary play. It
# exists because polling on a timer can straddle the exact moment fire is
# attempted - and did, in play, when the player fired again about two seconds
# after a switch, just past a 2-second poll window that had read entirely
# clean. This captures ground truth at the instant StartFire actually runs,
# rather than the nearest sample before or after it. Never blocks anything:
# returns None unconditionally.
@hook("WillowGame.WillowPlayerController:StartFire", Type.PRE)
def log_start_fire_during_watch(obj, args, _ret, _func):
    if _post_equip_watch["passes_left"] <= 0:
        return None
    try:
        pawn = obj.Pawn
        weapon = None if pawn is None else pawn.Weapon
        manager = obj.GetPawnInventoryManager()
        transitioning = None if manager is None else bool(
            manager.InventoryTransitionInProgress()
        )
        logging.info(
            f"[AutoLoot] StartFire(mode={int(getattr(args, 'FireModeNum', -1))}) fired"
            f" | wanted {_post_equip_watch['wanted']}"
            f" | now holding {'none' if weapon is None else weapon.Class.Name}"
            f" | HasAnyAmmo: {None if weapon is None else weapon.HasAnyAmmo()}"
            f" | IsPuttingDown: {None if weapon is None else bool(weapon.IsPuttingDown())}"
            f" | InventoryTransitionInProgress: {transitioning}"
            f" | IsZoomed: {bool(obj.IsZoomed())}"
        )
    except Exception as ex:  # noqa: BLE001
        logging.dev_warning(f"[AutoLoot] could not log StartFire: {ex!r}")
    return None


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
    pick_lower_level,
    drop_lowest_when_full,
    fill_empty_slots,
    switch_when_empty,
    equip_when_all_empty,
    range_percent,
    hud_summary_seconds,
    summary_in_console,
]

build_mod(options=MOD_OPTIONS)
