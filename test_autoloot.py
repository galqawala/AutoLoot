"""Tests for AutoLoot, runnable without the game: `python test_autoloot.py`.

The SDK modules are stubbed, and the fakes reproduce the two behaviours that were
verified against the TPS UnrealScript in C:\\Users\\Omistaja\\tps_classes:

  * WillowPickup.Destroyed() calls WillowGlobals.RemovePickup, so a successful
    pickup removes itself from the very array the mod iterates.
  * DroppedPickup.GiveTo sets Inventory to none before destroying the actor, so
    an emptied pickup is exactly the set of pickups that were collected.

test_harness_really_mutates_during_iteration is what gives the rest their teeth:
it proves the fake can actually expose the skipping bug, so a pass from
test_collects_every_pickup_in_one_call means something.
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_sdk_stubs():
    mods_base = types.ModuleType("mods_base")

    class _Option:
        def __init__(self, identifier, value, *_args, **_kwargs):
            self.identifier = identifier
            self.value = value

    mods_base.BoolOption = _Option
    mods_base.SliderOption = _Option
    mods_base.build_mod = lambda *_a, **_k: None

    def _hook(_name, _type=None):
        # Return the plain function so tests can call it directly.
        return lambda fn: fn

    mods_base.hook = _hook

    unrealsdk = types.ModuleType("unrealsdk")
    logging_mod = types.ModuleType("unrealsdk.logging")
    logging_mod.dev_warning = lambda *a, **k: None
    hooks_mod = types.ModuleType("unrealsdk.hooks")

    class Block:
        pass

    class Type:
        PRE = "PRE"
        POST = "POST"

    hooks_mod.Block = Block
    hooks_mod.Type = Type
    unrealsdk.logging = logging_mod
    unrealsdk.hooks = hooks_mod

    for name, module in (
        ("mods_base", mods_base),
        ("unrealsdk", unrealsdk),
        ("unrealsdk.logging", logging_mod),
        ("unrealsdk.hooks", hooks_mod),
    ):
        sys.modules[name] = module


_install_sdk_stubs()
_spec = importlib.util.spec_from_file_location(
    "autoloot_under_test", Path(__file__).with_name("__init__.py")
)
autoloot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autoloot)


# --- fakes -------------------------------------------------------------------

BASE_INTERACTION_DISTANCE = 100.0


class Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.X, self.Y, self.Z = x, y, z


class FakeClass:
    def __init__(self, name):
        self.Name = name


class FakeDefinitionData:
    def __init__(self, unique_id):
        self.UniqueId = unique_id


class FakeInventory:
    def __init__(self, unique_id, class_name="WillowWeapon"):
        self.Class = FakeClass(class_name)
        self.DefinitionData = FakeDefinitionData(unique_id)
        self.Inventory = None  # next link when held in an equipped chain


class FakePickup:
    def __init__(self, unique_id, class_name="WillowWeapon", distance=0.0):
        self.Inventory = FakeInventory(unique_id, class_name)
        self.Location = Vec(distance, 0.0, 0.0)


class FakeGlobalsDefinition:
    PlayerInteractionDistance = BASE_INTERACTION_DISTANCE


class FakeWillowGlobals:
    def __init__(self, pickups):
        # One stable list, as WillowGlobals.PickupList is - a fresh copy per
        # access would hide the very bug these tests exist to catch.
        self.PickupList = list(pickups)

    def GetGlobalsDefinition(self):
        return FakeGlobalsDefinition()


class FakeInventoryManager:
    def __init__(self, backpack, capacity):
        self.Backpack = backpack
        self.capacity = capacity

    # equipped chains: nothing readied in these tests
    InventoryChain = None
    ItemChain = None


class FakePawn:
    def __init__(self, inv_manager):
        self.InvManager = inv_manager


class FakePlayerController:
    """Mirrors the TPS pickup flow closely enough to exercise the mod's logic."""

    def __init__(self, pickups, capacity=99, backpack=None):
        self.inv_manager = FakeInventoryManager(list(backpack or []), capacity)
        self.Pawn = FakePawn(self.inv_manager)
        self.willow_globals = FakeWillowGlobals(pickups)
        self.CalcViewActorLocation = Vec(0.0, 0.0, 0.0)
        self.attempts = 0

    def GetPawnInventoryManager(self):
        return self.inv_manager

    def GetWillowGlobals(self):
        return self.willow_globals

    def PickupPickupable(self, pickup, _ready):
        self.attempts += 1
        if len(self.inv_manager.Backpack) >= self.inv_manager.capacity:
            return  # no room: pickup keeps its Inventory and stays in the world
        self.inv_manager.Backpack.append(pickup.Inventory)
        pickup.Inventory = None  # DroppedPickup.GiveTo
        self.willow_globals.PickupList.remove(pickup)  # Destroyed -> RemovePickup


def run_one_scan(pc):
    autoloot.ticks_until_scan = 0
    autoloot.player_tick(pc, None, None, None)


class AutoLootTests(unittest.TestCase):
    def setUp(self):
        # Every test starts from the same state; nothing carries over.
        autoloot.seen_unique_ids.clear()
        autoloot.ticks_until_scan = 0
        autoloot.picking_up = False
        autoloot.range_multiplier.value = 1.0
        for option, _token in autoloot.PICKUP_FILTERS:
            option.value = True

    def test_harness_really_mutates_during_iteration(self):
        """The fake must be able to expose the bug, or the other tests prove nothing."""
        pc = FakePlayerController([FakePickup(i) for i in range(5)])
        visited = []
        for pickup in pc.willow_globals.PickupList:  # deliberately live, not a copy
            visited.append(pickup)
            pc.PickupPickupable(pickup, False)
        self.assertLess(
            len(visited),
            5,
            "harness does not reproduce the skipping bug, so it cannot detect the fix",
        )

    def test_collects_every_pickup_in_one_call(self):
        pc = FakePlayerController([FakePickup(i) for i in range(5)])
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])
        self.assertEqual(len(pc.inv_manager.Backpack), 5)

    def test_failed_pickup_is_not_marked_seen_and_is_retried(self):
        """The reported bug: a full backpack used to burn the item permanently."""
        pc = FakePlayerController([FakePickup(42)], capacity=0)
        run_one_scan(pc)

        self.assertEqual(pc.attempts, 1)
        self.assertNotIn(42, autoloot.seen_unique_ids)
        self.assertEqual(len(pc.willow_globals.PickupList), 1)

        pc.inv_manager.capacity = 99  # made room
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])
        self.assertEqual(len(pc.inv_manager.Backpack), 1)

    def test_dropped_items_are_still_ignored(self):
        held = FakeInventory(7)
        pc = FakePlayerController([], backpack=[held])
        run_one_scan(pc)
        self.assertIn(7, autoloot.seen_unique_ids)

        # Drop it: same UniqueId, now lying on the ground.
        pc.inv_manager.Backpack.remove(held)
        pc.willow_globals.PickupList.append(FakePickup(7))
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)
        self.assertEqual(len(pc.willow_globals.PickupList), 1)

    def test_a_refusal_does_not_pin_the_scan_to_the_fast_interval(self):
        pc = FakePlayerController([FakePickup(1)], capacity=0)
        run_one_scan(pc)
        self.assertEqual(autoloot.ticks_until_scan, autoloot.SCAN_INTERVAL)

    def test_a_success_schedules_an_early_rescan(self):
        pc = FakePlayerController([FakePickup(1)])
        run_one_scan(pc)
        self.assertEqual(autoloot.ticks_until_scan, autoloot.FAST_SCAN_INTERVAL)

    def test_range_multiplier_extends_reach(self):
        just_out_of_reach = BASE_INTERACTION_DISTANCE * 1.5
        pc = FakePlayerController([FakePickup(1, distance=just_out_of_reach)])

        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0, "should be out of range at 1x")

        autoloot.range_multiplier.value = 2.0
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_disabled_categories_are_left_alone(self):
        pc = FakePlayerController([FakePickup(1, class_name="WillowShield")])
        for option, token in autoloot.PICKUP_FILTERS:
            if token == "Shield":
                option.value = False
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)

    def test_one_broken_pickup_does_not_abandon_the_pass(self):
        class ExplodingPickup:
            @property
            def Inventory(self):
                raise RuntimeError("stale actor")

        pc = FakePlayerController([ExplodingPickup(), FakePickup(1), FakePickup(2)])
        run_one_scan(pc)
        self.assertEqual(len(pc.inv_manager.Backpack), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
