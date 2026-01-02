from mods_base import build_mod, get_pc, hook, BoolOption
from unrealsdk.hooks import Type
import unrealsdk
import math

tick_counter = 0
seen_unique_ids = set()
picking_up = False

pickup_weapons = BoolOption("Pickup Weapons", True)
pickup_shields = BoolOption("Pickup Shields", True)
pickup_grenades = BoolOption("Pickup Grenades", True)
pickup_class_mods = BoolOption("Pickup Class Mods", True)
pickup_artifacts = BoolOption("Pickup Artifacts", True)
pickup_customizations = BoolOption("Pickup Customizations", True)

def update_seen_ids(caller):
    inventory_manager = caller.GetPawnInventoryManager()
    if not inventory_manager:
        return

    # Scan backpack items
    backpack_items = [item for item in inventory_manager.Backpack if item]
    for item in backpack_items:
        if item.DefinitionData and hasattr(item.DefinitionData, "UniqueId"):
            unique_id = item.DefinitionData.UniqueId
            seen_unique_ids.add(unique_id)

    # Scan equipped items using inventory chains
    pawn = caller.Pawn
    if pawn and hasattr(pawn, "InvManager"):
        inv_manager = pawn.InvManager
        for chain in (inv_manager.InventoryChain, inv_manager.ItemChain):
            item = chain
            while item is not None:
                if item.DefinitionData and hasattr(item.DefinitionData, "UniqueId"):
                    unique_id = item.DefinitionData.UniqueId
                    seen_unique_ids.add(unique_id)
                item = item.Inventory

def dist(a, b) -> float:
    return math.sqrt((b.X - a.X) ** 2 + (b.Y - a.Y) ** 2 + (b.Z - a.Z) ** 2)

@hook("WillowGame.WillowPlayerController:PlayerTick", Type.POST)
def player_tick(obj, __args, __ret, __func):
    global tick_counter
    
    tick_counter += 1
    if tick_counter % 100 != 0:
        return

    # Update seen IDs with current backpack
    if tick_counter % 500 == 0:  # Only check backpack every 5 seconds
        update_seen_ids(obj)

    max_dist = obj.GetWillowGlobals().GetGlobalsDefinition().PlayerInteractionDistance
    for pickup in obj.GetWillowGlobals().PickupList:
        if pickup.Inventory and pickup.Inventory.Class:
            class_name = pickup.Inventory.Class.Name
            should_pickup = False

            if pickup_weapons.value and "Weapon" in class_name:
                should_pickup = True
            elif pickup_shields.value and "Shield" in class_name:
                should_pickup = True
            elif pickup_grenades.value and "Grenade" in class_name:
                should_pickup = True
            elif pickup_class_mods.value and "ClassMod" in class_name:
                should_pickup = True
            elif pickup_artifacts.value and "Artifact" in class_name:
                should_pickup = True
            elif pickup_customizations.value and "UsableCustomization" in class_name:
                should_pickup = True

            if should_pickup:
                # Use pickup.Inventory.DefinitionData.UniqueId
                unique_id = None
                if (
                    pickup.Inventory
                    and pickup.Inventory.DefinitionData
                    and hasattr(pickup.Inventory.DefinitionData, "UniqueId")
                ):
                    unique_id = pickup.Inventory.DefinitionData.UniqueId

                if unique_id is None or unique_id not in seen_unique_ids:
                    distance = dist(pickup.Location, obj.CalcViewActorLocation)
                    if distance <= max_dist:
                        # Add to seen list when picking up
                        if unique_id:
                            seen_unique_ids.add(unique_id)
                        global picking_up
                        picking_up = True
                        obj.PickupPickupable(pickup, False)
                        picking_up = False

@hook("WillowGame.WillowPlayerController:ClientDisplayPickupFailedMessage", Type.PRE)
def block_pickup_failed_message(obj, __args, __ret, __func):
    return not picking_up

@hook("WillowGame.WillowPickup:FailedPickup", Type.PRE)
def block_failed_pickup(obj, __args, __ret, __func):
    return not picking_up

build_mod()
