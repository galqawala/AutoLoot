import math
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


def best_owned_levels(caller):
    """The highest level held of each kind, across backpack and equipped gear."""
    best = {}
    for item in iter_owned_inventory(caller):
        level = item_level(item)
        kind = None if level is None else item_kind(item)
        if kind is not None and level > best.get(kind, 0):
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


def free_a_backpack_slot(caller) -> bool:
    """Throw out the worst item of whatever kind is filling the backpack most.

    Returns whether a slot was actually freed. Only the backpack is considered,
    never equipped gear, and never anything marked as a favourite.
    """
    inventory_manager = caller.GetPawnInventoryManager()
    if inventory_manager is None:
        return False

    by_kind = {}
    for item in inventory_manager.Backpack:
        kind = item_kind(item)
        if kind is None or is_favorite(item) or not caller.CanDrop(item):
            continue
        by_kind.setdefault(kind, []).append(item)

    if not by_kind:
        return False

    # Most numerous kind, then within it the lowest level, cheapest, oldest.
    # Every tie is broken by something stable so the same backpack always gives
    # up the same item rather than whichever happened to be looked at first.
    fullest = min(by_kind, key=lambda kind: (-len(by_kind[kind]), kind))
    worst = min(
        by_kind[fullest],
        key=lambda item: (
            item_level(item) or 0,
            item_price(item),
            unique_id_of(item) or 0,
        ),
    )

    # Judge success by the backpack shrinking rather than by inspecting the item
    # afterwards - it has been handed to the engine by then.
    count_before = len(inventory_manager.Backpack)
    caller.ThrowInventory(worst, 1)
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
    range_percent,
    hud_summary_seconds,
    summary_in_console,
]

build_mod(options=MOD_OPTIONS)
