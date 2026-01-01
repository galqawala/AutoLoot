# to extract files you need to run the package decompressor first, get it from https://www.gildor.org/downloads
# it's a command line tool, most useful ones to extract are  WillowGame, Engine, and maybe GearboxFramework and GameFramework, rest is generally useless
# once they're decompressed you can run ue explorer over them https://github.com/UE-Explorer/UE-Explorer/
# I recommend exporting to disk and using another editor to browse though the files. there's a vscode plugin with basic go to definition for example

# https://bl-sdk.github.io/developing/#adding-to-the-mod-db

import unrealsdk
import math
from Mods import ModMenu


class AutoLoot(ModMenu.SDKMod):
    Name: str = "AutoLoot"
    Author: str = "Q Developer"
    Description: str = (
        "Automatically picks up weapons and items with configurable options"
    )
    Version: str = "1.0.0"
    SupportedGames: ModMenu.Game = ModMenu.Game.TPS
    Types: ModMenu.ModTypes = ModMenu.ModTypes.Utility
    SaveEnabledState: ModMenu.EnabledSaveType = ModMenu.EnabledSaveType.LoadOnMainMenu

    Pickinup = False
    seen_unique_ids = set()
    tick_counter = 0
    logged_inventory = False

    PickupWeapons = ModMenu.Options.Boolean(
        Caption="Pickup Weapons",
        Description="Automatically pickup weapons",
        StartingValue=True,
    )
    PickupShields = ModMenu.Options.Boolean(
        Caption="Pickup Shields",
        Description="Automatically pickup shields",
        StartingValue=True,
    )
    PickupGrenades = ModMenu.Options.Boolean(
        Caption="Pickup Grenades",
        Description="Automatically pickup grenades",
        StartingValue=True,
    )
    PickupClassMods = ModMenu.Options.Boolean(
        Caption="Pickup Class Mods",
        Description="Automatically pickup class mods",
        StartingValue=True,
    )
    PickupArtifacts = ModMenu.Options.Boolean(
        Caption="Pickup Artifacts",
        Description="Automatically pickup artifacts/relics",
        StartingValue=True,
    )
    PickupCustomizations = ModMenu.Options.Boolean(
        Caption="Pickup Customizations",
        Description="Automatically pickup customization items",
        StartingValue=True,
    )

    Options = [
        PickupWeapons,
        PickupShields,
        PickupGrenades,
        PickupClassMods,
        PickupArtifacts,
        PickupCustomizations,
    ]

    def update_seen_ids(self, caller):
        inventory_manager = caller.GetPawnInventoryManager()
        if not inventory_manager:
            return

        # Scan backpack items
        backpack_items = [item for item in inventory_manager.Backpack if item]
        for item in backpack_items:
            if item.DefinitionData and hasattr(item.DefinitionData, "UniqueId"):
                unique_id = item.DefinitionData.UniqueId
                self.seen_unique_ids.add(unique_id)

        # Scan equipped items using inventory chains
        pawn = caller.Pawn
        if pawn and hasattr(pawn, "InvManager"):
            inv_manager = pawn.InvManager
            for chain in (inv_manager.InventoryChain, inv_manager.ItemChain):
                item = chain
                while item is not None:
                    if item.DefinitionData and hasattr(item.DefinitionData, "UniqueId"):
                        unique_id = item.DefinitionData.UniqueId
                        self.seen_unique_ids.add(unique_id)
                    item = item.Inventory

    def get_item_id(self, pickup):
        if (
            pickup.Inventory
            and pickup.Inventory.DefinitionData
            and pickup.Inventory.DefinitionData.ItemDefinition
        ):
            return (
                pickup.Inventory.DefinitionData.ItemDefinition.GetFullDefinitionName(),
                pickup.Inventory.DefinitionData.ItemGrade,
                pickup.Inventory.DefinitionData.GameStage,
            )
        return None

    def dist(self, a, b) -> float:
        return math.sqrt((b.X - a.X) ** 2 + (b.Y - a.Y) ** 2 + (b.Z - a.Z) ** 2)

    @ModMenu.Hook("WillowGame.WillowPlayerController.PlayerTick")
    def PlayerTick(
        self,
        caller: unrealsdk.UObject,
        function: unrealsdk.UFunction,
        params: unrealsdk.FStruct,
    ) -> bool:
        self.tick_counter += 1
        if self.tick_counter % 100 != 0:
            return True

        # Update seen IDs with current backpack
        if self.tick_counter % 500 == 0:  # Only check backpack every 5 seconds
            self.update_seen_ids(caller)

        maxDist = (
            caller.GetWillowGlobals().GetGlobalsDefinition().PlayerInteractionDistance
        )
        for pickup in caller.GetWillowGlobals().PickupList:
            if pickup.Inventory and pickup.Inventory.Class:
                class_name = pickup.Inventory.Class.Name
                should_pickup = False

                if self.PickupWeapons.CurrentValue and "Weapon" in class_name:
                    should_pickup = True
                elif self.PickupShields.CurrentValue and "Shield" in class_name:
                    should_pickup = True
                elif self.PickupGrenades.CurrentValue and "Grenade" in class_name:
                    should_pickup = True
                elif self.PickupClassMods.CurrentValue and "ClassMod" in class_name:
                    should_pickup = True
                elif self.PickupArtifacts.CurrentValue and "Artifact" in class_name:
                    should_pickup = True
                elif (
                    self.PickupCustomizations.CurrentValue
                    and "UsableCustomization" in class_name
                ):
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

                    if unique_id is None or unique_id not in self.seen_unique_ids:
                        distance = self.dist(
                            pickup.Location, caller.CalcViewActorLocation
                        )
                        if distance <= maxDist:
                            # Add to seen list when picking up
                            if unique_id:
                                self.seen_unique_ids.add(unique_id)
                            self.Pickinup = True
                            caller.PickupPickupable(pickup, False)
                            self.Pickinup = False
        return True

    @ModMenu.Hook("WillowGame.WillowPlayerController.ClientDisplayPickupFailedMessage")
    def ClientDisplayPickupFailedMessage(
        self,
        caller: unrealsdk.UObject,
        function: unrealsdk.UFunction,
        params: unrealsdk.FStruct,
    ) -> bool:
        return not self.Pickinup

    @ModMenu.Hook("WillowGame.WillowPickup.FailedPickup")
    def FailedPickup(
        self,
        caller: unrealsdk.UObject,
        function: unrealsdk.UFunction,
        params: unrealsdk.FStruct,
    ) -> bool:
        return not self.Pickinup

    @ModMenu.Hook("WillowGame.WillowPlayerController.ThrowInventoryItem")
    def TrackDroppedItems(
        self,
        caller: unrealsdk.UObject,
        function: unrealsdk.UFunction,
        params: unrealsdk.FStruct,
    ) -> bool:
        if (
            params.Item
            and params.Item.DefinitionData
            and hasattr(params.Item.DefinitionData, "UniqueId")
        ):
            unique_id = params.Item.DefinitionData.UniqueId
            self.seen_unique_ids.add(unique_id)
        return True


instance = AutoLoot()

if __name__ == "__main__":
    unrealsdk.Log(f"[{instance.Name}] Manually loaded")
    for mod in ModMenu.Mods:
        if mod.Name == instance.Name:
            if mod.IsEnabled:
                mod.Disable()
            ModMenu.Mods.remove(mod)
            unrealsdk.Log(f"[{instance.Name}] Removed last instance")
            instance.__class__.__module__ = mod.__class__.__module__
            break

ModMenu.RegisterMod(instance)
