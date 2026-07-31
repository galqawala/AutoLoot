import math

from mods_base import BoolOption, SliderOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Block, Type

# PlayerTick fires once per frame, so this is roughly a fifth of a second. A pass
# that actually collects something rescans on the very next tick instead, so a
# pile of loot never waits out a full interval between items.
SCAN_INTERVAL = 20
FAST_SCAN_INTERVAL = 1

ticks_until_scan = 0
seen_unique_ids = set()
picking_up = False

pickup_weapons = BoolOption("Pickup Weapons", True)
pickup_shields = BoolOption("Pickup Shields", True)
pickup_grenades = BoolOption("Pickup Grenades", True)
pickup_class_mods = BoolOption("Pickup Class Mods", True)
pickup_artifacts = BoolOption("Pickup Artifacts", True)
pickup_customizations = BoolOption("Pickup Customizations", True)
range_multiplier = SliderOption(
    "Pickup Range Multiplier",
    2.0,
    1.0,
    5.0,
    0.5,
    False,
    description=(
        "How far AutoLoot reaches, as a multiple of your normal pickup range."
        " Only automatic pickups are affected - picking things up yourself"
        " still works exactly as before."
    ),
)

# Each option paired with the substring of the inventory class name it enables.
# Keeping the pair together means adding a category is one row, not a new branch.
PICKUP_FILTERS = (
    (pickup_weapons, "Weapon"),
    (pickup_shields, "Shield"),
    (pickup_grenades, "Grenade"),
    (pickup_class_mods, "ClassMod"),
    (pickup_artifacts, "Artifact"),
    (pickup_customizations, "UsableCustomization"),
)


def should_pickup(class_name: str) -> bool:
    return any(option.value and token in class_name for option, token in PICKUP_FILTERS)


def unique_id_of(inventory):
    """The id distinguishing one rolled item from another, or None if it has none."""
    definition_data = inventory.DefinitionData if inventory is not None else None
    if definition_data is None:
        return None
    return getattr(definition_data, "UniqueId", None)


def update_seen_ids(caller):
    """Record everything the player is currently carrying as already seen.

    This is the *only* writer of seen_unique_ids, deliberately. Marking an item
    seen because we tried to pick it up burns it permanently whenever the attempt
    fails - a full backpack being the everyday case - so "seen" is defined as
    "has been in my inventory" and nothing else. Anything the player drops is
    therefore still ignored, while anything we failed to collect is retried.
    """
    inventory_manager = caller.GetPawnInventoryManager()
    if not inventory_manager:
        return

    for item in inventory_manager.Backpack:
        unique_id = unique_id_of(item)
        if unique_id is not None:
            seen_unique_ids.add(unique_id)

    pawn = caller.Pawn
    if pawn is None or pawn.InvManager is None:
        return
    for chain in (pawn.InvManager.InventoryChain, pawn.InvManager.ItemChain):
        item = chain
        while item is not None:
            unique_id = unique_id_of(item)
            if unique_id is not None:
                seen_unique_ids.add(unique_id)
            item = item.Inventory


def dist(a, b) -> float:
    return math.sqrt((b.X - a.X) ** 2 + (b.Y - a.Y) ** 2 + (b.Z - a.Z) ** 2)


@hook("WillowGame.WillowPlayerController:PlayerTick", Type.POST)
def player_tick(obj, _args, _ret, _func):
    global ticks_until_scan, picking_up

    ticks_until_scan -= 1
    if ticks_until_scan > 0:
        return

    update_seen_ids(obj)

    willow_globals = obj.GetWillowGlobals()
    max_dist = (
        willow_globals.GetGlobalsDefinition().PlayerInteractionDistance
        * range_multiplier.value
    )
    view_location = obj.CalcViewActorLocation
    collected_any = False

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
            if not should_pickup(inventory.Class.Name):
                continue
            if unique_id_of(inventory) in seen_unique_ids:
                continue
            if dist(pickup.Location, view_location) > max_dist:
                continue

            picking_up = True
            try:
                obj.PickupPickupable(pickup, False)
            finally:
                picking_up = False

            # DroppedPickup.GiveTo clears Inventory before the actor is destroyed,
            # all synchronously inside the call above, so an emptied pickup is one
            # we genuinely got. Anything refused still holds its inventory and is
            # simply retried on a later pass. Note this only decides whether to
            # rescan early - a refusal must not pin us to the fast interval.
            if pickup.Inventory is None:
                collected_any = True
        except Exception as ex:  # noqa: BLE001
            # The snapshot can outlive its entries; one stale pickup must not
            # abandon the rest of the pass.
            logging.dev_warning(f"[AutoLoot] skipped a pickup: {ex!r}")

    ticks_until_scan = FAST_SCAN_INTERVAL if collected_any else SCAN_INTERVAL


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


build_mod()
