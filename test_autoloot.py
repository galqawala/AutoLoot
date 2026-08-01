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

import enum
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
    mods_base.DropdownOption = _Option
    mods_base.build_mod = lambda *_a, **_k: None

    class Game(enum.Flag):
        BL2 = enum.auto()
        TPS = enum.auto()

        @staticmethod
        def get_current():
            return Game.TPS

    mods_base.Game = Game

    ui_utils = types.ModuleType("ui_utils")
    ui_utils.shown = []
    # Record the duration too: a setting that never reaches the call it configures
    # looks identical to one that works, from every other assertion here.
    ui_utils.show_hud_message = lambda title, msg, duration=2.5: ui_utils.shown.append(
        (title, msg, duration)
    )
    sys.modules["ui_utils"] = ui_utils

    def _hook(_name, _type=None):
        # Return the plain function so tests can call it directly.
        return lambda fn: fn

    mods_base.hook = _hook

    unrealsdk = types.ModuleType("unrealsdk")
    logging_mod = types.ModuleType("unrealsdk.logging")
    logging_mod.dev_warning = lambda *a, **k: None
    logging_mod.logged = []
    logging_mod.info = lambda *args: logging_mod.logged.append(" ".join(map(str, args)))
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


class FakeItemDefinition:
    def __init__(self, my_class=True):
        self.my_class = my_class

    def PlayerClassRequirementMet(self, _pc):
        return self.my_class


class FakeResource:
    def __init__(self, name):
        self.Name = name


class FakeWeaponTypeDefinition:
    def __init__(self, ammo_name):
        self.AmmoResource = None if ammo_name is None else FakeResource(ammo_name)


class FakeDefinitionData:
    def __init__(self, unique_id, my_class=True, ammo="Ammo_Repeater_Pistol", level=0):
        self.UniqueId = unique_id
        self.ItemDefinition = FakeItemDefinition(my_class)
        self.WeaponTypeDefinition = FakeWeaponTypeDefinition(ammo)
        # 0 means "no level to compare", which is how most tests want it.
        self.GameStage = level


CUSTOMIZATION_CLASS = "WillowUsableCustomizationItem"
CLASS_MOD_CLASS = "WillowClassMod"

# WillowInventory.PlayerMark, as the game's own toggle uses them.
MARK_TRASH = 0
MARK_STANDARD = 1
MARK_FAVORITE = 2


class FakeInventory:
    def __init__(
        self,
        unique_id,
        class_name="WillowWeapon",
        useful=True,
        my_class=True,
        ammo="Ammo_Repeater_Pistol",
        level=0,
        price=0,
        mark=MARK_STANDARD,
    ):
        self.Class = FakeClass(class_name)
        self.DefinitionData = FakeDefinitionData(unique_id, my_class, ammo, level)
        self.Inventory = None  # next link when held in an equipped chain
        self.useful = useful
        self.dlc_met = True
        self.consumed = False
        self.backpack = None  # set once it is sitting in someone's backpack
        self.price = price
        self.mark = mark

    def GetMonetaryValue(self):
        return self.price

    def GetMark(self):
        return self.mark

    def IsUsefulToThisPlayer(self, _pc):
        # False for an already-unlocked customization, as the game reports it.
        return self.useful

    def IsConsumable(self):
        return CUSTOMIZATION_CLASS in self.Class.Name

    def IsDLCRequirementMet(self, _pc):
        return self.dlc_met

    def TryConsume(self):
        """WillowUsableCustomizationItem.TryConsume: refuses an already-unlocked one."""
        if not self.useful:
            return False
        if self.backpack is not None:
            self.backpack.remove(self)
        self.consumed = True
        return True


class TouchedDestroyedPickup(BaseException):
    """Deliberately not an Exception, so the mod's own except cannot swallow it."""


class FakePickup:
    """A WillowPickup that reports being read after the engine destroyed it.

    Reading a property of a destroyed actor is reading memory the game has
    handed back, which is a plausible route to a native crash rather than a
    Python traceback. Making it loud here is the only way to keep it out.
    """

    def __init__(
        self,
        unique_id,
        class_name="WillowWeapon",
        distance=0.0,
        useful=True,
        my_class=True,
        ammo="Ammo_Repeater_Pistol",
        level=0,
    ):
        self._inventory = FakeInventory(
            unique_id, class_name, useful, my_class, ammo, level
        )
        self._location = Vec(distance, 0.0, 0.0)
        self.destroyed = False

    def _assert_alive(self):
        if self.destroyed:
            raise TouchedDestroyedPickup(
                "read a property of a WillowPickup the engine already destroyed"
            )

    @property
    def Inventory(self):
        self._assert_alive()
        return self._inventory

    @Inventory.setter
    def Inventory(self, value):
        self._inventory = value

    @property
    def Location(self):
        self._assert_alive()
        return self._location


def customization(unique_id, unlocked):
    """A customization pickup; unlocked ones are the worthless duplicates."""
    return FakePickup(unique_id, CUSTOMIZATION_CLASS, useful=not unlocked)


def class_mod(unique_id, mine):
    """A class mod pickup; `mine` is whether this character can equip it."""
    return FakePickup(unique_id, CLASS_MOD_CLASS, my_class=mine)


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

    def CountUnreadiedInventory(self):
        return len(self.Backpack)

    def GetUnreadiedInventoryMaxSize(self):
        return self.capacity

    def GetEmptyBackpackSlots(self):
        return max(0, self.capacity - len(self.Backpack))


class FakePawn:
    def __init__(self, inv_manager):
        self.InvManager = inv_manager


class FakePlayerController:
    """Mirrors the TPS pickup flow closely enough to exercise the mod's logic."""

    def __init__(self, pickups, capacity=99, backpack=None):
        self.inv_manager = FakeInventoryManager(list(backpack or []), capacity)
        for item in self.inv_manager.Backpack:
            item.backpack = self.inv_manager.Backpack
        self.Pawn = FakePawn(self.inv_manager)
        self.willow_globals = FakeWillowGlobals(pickups)
        self.CalcViewActorLocation = Vec(0.0, 0.0, 0.0)
        self.attempts = 0
        self.thrown = []

    def GetPawnInventoryManager(self):
        return self.inv_manager

    def GetWillowGlobals(self):
        return self.willow_globals

    def PickupPickupable(self, pickup, _ready):
        self.attempts += 1
        if len(self.inv_manager.Backpack) >= self.inv_manager.capacity:
            return  # no room: pickup keeps its Inventory and stays in the world
        collected = pickup.Inventory
        collected.backpack = self.inv_manager.Backpack
        self.inv_manager.Backpack.append(collected)
        pickup.Inventory = None  # DroppedPickup.GiveTo clears it, then...
        self.willow_globals.PickupList.remove(pickup)  # Destroyed -> RemovePickup
        pickup.destroyed = True  # ...the actor itself is gone

    def CanDrop(self, _item):
        return True

    def ThrowInventory(self, item, _quantity=1):
        # ServerThrowInventory: out of the backpack, onto the ground as a pickup.
        self.inv_manager.Backpack.remove(item)
        self.thrown.append(item)
        dropped = FakePickup(item.DefinitionData.UniqueId, item.Class.Name)
        dropped.Inventory = item
        self.willow_globals.PickupList.append(dropped)
        return dropped


def run_one_scan(pc):
    autoloot.ticks_until_scan = 0
    autoloot.player_tick(pc, None, None, None)


class AutoLootTests(unittest.TestCase):
    def setUp(self):
        # Every test starts from the same state; nothing carries over.
        autoloot.seen_unique_ids.clear()
        autoloot.ticks_until_scan = 0
        autoloot.picking_up = False
        autoloot.range_percent.value = 100
        autoloot.pickup_customizations.value = autoloot.CHOICE_ALL
        autoloot.pickup_class_mods.value = autoloot.CHOICE_ALL
        autoloot.hud_summary_seconds.value = 0
        autoloot.summary_in_console.value = False
        autoloot.auto_use_customizations.value = False
        autoloot.pick_lower_level.value = True  # off unless a test is about it
        autoloot.drop_lowest_when_full.value = False
        autoloot.collected_last_pass = False
        sys.modules["ui_utils"].shown.clear()
        sys.modules["unrealsdk.logging"].logged.clear()
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

    def test_range_percent_extends_reach(self):
        just_out_of_reach = BASE_INTERACTION_DISTANCE * 1.5
        pc = FakePlayerController([FakePickup(1, distance=just_out_of_reach)])

        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0, "should be out of range at 100%")

        autoloot.range_percent.value = 200
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_disabled_categories_are_left_alone(self):
        pc = FakePlayerController([FakePickup(1, class_name="WillowShield")])
        for option, token in autoloot.PICKUP_FILTERS:
            if token == "Shield":
                option.value = False
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)

    # --- customization modes ---

    def test_new_only_leaves_already_unlocked_customizations(self):
        autoloot.pickup_customizations.value = autoloot.CUSTOMIZATIONS_NEW
        pc = FakePlayerController([customization(1, unlocked=True)])
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)
        self.assertEqual(len(pc.willow_globals.PickupList), 1)

    def test_new_only_still_takes_new_customizations(self):
        autoloot.pickup_customizations.value = autoloot.CUSTOMIZATIONS_NEW
        pc = FakePlayerController([customization(1, unlocked=False)])
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_all_takes_customizations_it_already_owns(self):
        autoloot.pickup_customizations.value = autoloot.CHOICE_ALL
        pc = FakePlayerController([customization(1, unlocked=True)])
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_none_leaves_every_customization(self):
        autoloot.pickup_customizations.value = autoloot.CHOICE_NONE
        pc = FakePlayerController(
            [customization(1, unlocked=False), customization(2, unlocked=True)]
        )
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)
        self.assertEqual(len(pc.willow_globals.PickupList), 2)

    def test_customization_mode_does_not_affect_other_categories(self):
        autoloot.pickup_customizations.value = autoloot.CHOICE_NONE
        pc = FakePlayerController([FakePickup(1)])  # a weapon
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    # --- class mod modes ---

    def test_my_class_leaves_other_characters_class_mods(self):
        autoloot.pickup_class_mods.value = autoloot.CLASS_MODS_MINE
        pc = FakePlayerController([class_mod(1, mine=False)])
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)
        self.assertEqual(len(pc.willow_globals.PickupList), 1)

    def test_my_class_takes_ones_this_character_can_equip(self):
        autoloot.pickup_class_mods.value = autoloot.CLASS_MODS_MINE
        pc = FakePlayerController([class_mod(1, mine=True)])
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_all_takes_class_mods_for_any_character(self):
        autoloot.pickup_class_mods.value = autoloot.CHOICE_ALL
        pc = FakePlayerController([class_mod(1, mine=False)])
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_none_leaves_every_class_mod(self):
        autoloot.pickup_class_mods.value = autoloot.CHOICE_NONE
        pc = FakePlayerController([class_mod(1, mine=True), class_mod(2, mine=False)])
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)
        self.assertEqual(len(pc.willow_globals.PickupList), 2)

    def test_class_mod_mode_does_not_affect_other_categories(self):
        autoloot.pickup_class_mods.value = autoloot.CHOICE_NONE
        pc = FakePlayerController([FakePickup(1)])  # a weapon
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_the_two_dropdowns_are_independent(self):
        """A class mod is not a customization and must not be judged as one."""
        autoloot.pickup_class_mods.value = autoloot.CHOICE_NONE
        autoloot.pickup_customizations.value = autoloot.CHOICE_ALL
        pc = FakePlayerController([customization(1, unlocked=True), class_mod(2, mine=True)])
        run_one_scan(pc)
        self.assertEqual(len(pc.willow_globals.PickupList), 1, "class mod should remain")
        self.assertEqual(len(pc.inv_manager.Backpack), 1)

    # --- drop lowest level when full ---

    def full_backpack_run(self, backpack, loot=None):
        """One scan with a backpack exactly at capacity and one thing worth taking."""
        autoloot.drop_lowest_when_full.value = True
        loot = loot or FakePickup(99, ammo="Ammo_Repeater_Pistol", level=5)
        pc = FakePlayerController(
            [loot], capacity=len(backpack), backpack=list(backpack)
        )
        run_one_scan(pc)
        return pc

    def test_drops_from_the_kind_taking_the_most_space(self):
        smgs = [FakeInventory(i, ammo="Ammo_Patrol_SMG", level=40) for i in range(3)]
        shield = FakeInventory(8, "WillowShield", level=1)  # lower level, but rarer
        pc = self.full_backpack_run([*smgs, shield])

        self.assertEqual(len(pc.thrown), 1)
        self.assertIn(pc.thrown[0], smgs, "should give up an SMG, not the lone shield")

    def test_drops_the_lowest_level_of_that_kind(self):
        smgs = [
            FakeInventory(1, ammo="Ammo_Patrol_SMG", level=40),
            FakeInventory(2, ammo="Ammo_Patrol_SMG", level=12),
            FakeInventory(3, ammo="Ammo_Patrol_SMG", level=30),
        ]
        pc = self.full_backpack_run(smgs)
        self.assertEqual(pc.thrown, [smgs[1]])

    def test_price_breaks_a_tie_on_level(self):
        smgs = [
            FakeInventory(1, ammo="Ammo_Patrol_SMG", level=12, price=50),
            FakeInventory(2, ammo="Ammo_Patrol_SMG", level=12, price=7),
            FakeInventory(3, ammo="Ammo_Patrol_SMG", level=40, price=1),
        ]
        pc = self.full_backpack_run(smgs)
        self.assertEqual(pc.thrown, [smgs[1]], "cheapest of the two level 12s")

    def test_favourite_mark_value_matches_both_games(self):
        """PlayerMark values are identical in BL2 and TPS - verified, not assumed.

        Each game's InventoryListPanelGFxObject.CycleSelectedThingAsTrashOrFavorite
        sets a value and then displays the matching icon:

            BL2   mark 1 -> SetMark(0) shows TF_Trash      so 0 = Trash
                  mark 0 -> SetMark(2) shows TF_Favorite   so 2 = Favorite
                  mark 2 -> SetMark(1) shows TF_Standard   so 1 = Standard

            TPS   mark 1 -> SetMark(2) shows TF_Favorite   so 2 = Favorite
                  mark 2 -> SetMark(1) shows TF_Standard   so 1 = Standard
                  (no trash branch - TPS exposes favourites only)

        TPS never marks anything as trash through its UI, but the numbering is
        the same, so one constant is correct for both.
        """
        self.assertEqual(autoloot.MARK_FAVORITE, MARK_FAVORITE)
        self.assertEqual(autoloot.MARK_FAVORITE, 2)

    def test_trash_marked_items_are_droppable(self):
        """BL2 only. Trash is not protected - it is the stuff you want gone."""
        smgs = [
            FakeInventory(1, ammo="Ammo_Patrol_SMG", level=40, mark=MARK_TRASH),
            FakeInventory(2, ammo="Ammo_Patrol_SMG", level=50, mark=MARK_STANDARD),
            FakeInventory(3, ammo="Ammo_Patrol_SMG", level=60, mark=MARK_STANDARD),
        ]
        pc = self.full_backpack_run(smgs)
        self.assertEqual(pc.thrown, [smgs[0]], "lowest level, and trash is fair game")

    def test_never_drops_a_favourite(self):
        smgs = [
            FakeInventory(1, ammo="Ammo_Patrol_SMG", level=1, mark=MARK_FAVORITE),
            FakeInventory(2, ammo="Ammo_Patrol_SMG", level=40),
            FakeInventory(3, ammo="Ammo_Patrol_SMG", level=30),
        ]
        pc = self.full_backpack_run(smgs)
        self.assertEqual(pc.thrown, [smgs[2]], "lowest level that is not a favourite")

    def test_the_freed_slot_is_used_for_the_loot(self):
        smgs = [FakeInventory(i, ammo="Ammo_Patrol_SMG", level=40) for i in range(3)]
        loot = FakePickup(99, ammo="Ammo_Repeater_Pistol", level=5)
        wanted = loot.Inventory  # read now; collecting destroys the pickup
        pc = self.full_backpack_run(smgs, loot=loot)

        self.assertNotIn(loot, pc.willow_globals.PickupList, "loot was not collected")
        self.assertIn(wanted, pc.inv_manager.Backpack)
        # The thrown SMG is on the ground now, so the world is not empty.
        self.assertEqual(len(pc.willow_globals.PickupList), 1)
        self.assertEqual(len(pc.inv_manager.Backpack), 3, "still exactly full")

    def test_drops_nothing_when_the_backpack_has_room(self):
        smgs = [FakeInventory(i, ammo="Ammo_Patrol_SMG", level=40) for i in range(3)]
        autoloot.drop_lowest_when_full.value = True
        loot = FakePickup(99, ammo="Ammo_Repeater_Pistol", level=5)
        pc = FakePlayerController([loot], capacity=39, backpack=list(smgs))
        run_one_scan(pc)
        self.assertEqual(pc.thrown, [])

    def test_option_off_drops_nothing_even_when_full(self):
        smgs = [FakeInventory(i, ammo="Ammo_Patrol_SMG", level=40) for i in range(3)]
        autoloot.drop_lowest_when_full.value = False
        loot = FakePickup(99, ammo="Ammo_Repeater_Pistol", level=5)
        pc = FakePlayerController([loot], capacity=3, backpack=list(smgs))
        run_one_scan(pc)
        self.assertEqual(pc.thrown, [])
        self.assertEqual(len(pc.willow_globals.PickupList), 1, "loot stays on ground")

    def test_drops_nothing_when_everything_is_a_favourite(self):
        smgs = [
            FakeInventory(i, ammo="Ammo_Patrol_SMG", level=40, mark=MARK_FAVORITE)
            for i in range(3)
        ]
        pc = self.full_backpack_run(smgs)
        self.assertEqual(pc.thrown, [])
        self.assertEqual(len(pc.inv_manager.Backpack), 3)

    def test_a_dropped_item_is_not_picked_straight_back_up(self):
        smgs = [FakeInventory(i, ammo="Ammo_Patrol_SMG", level=40) for i in range(3)]
        pc = self.full_backpack_run(smgs)
        thrown = pc.thrown[0]
        run_one_scan(pc)
        run_one_scan(pc)
        self.assertNotIn(thrown, pc.inv_manager.Backpack)

    # --- pick lower level ---

    def collected(self, loot, backpack=(), equipped=()):
        """Run one scan with the level rule active; True if the loot was taken."""
        autoloot.pick_lower_level.value = False
        pc = FakePlayerController([loot], backpack=list(backpack))
        if equipped:
            chain = None
            for item in reversed(equipped):
                item.Inventory = chain
                chain = item
            pc.inv_manager.InventoryChain = chain
        run_one_scan(pc)
        return pc.willow_globals.PickupList == []

    def test_takes_loot_when_none_of_that_kind_is_owned(self):
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=2)
        self.assertTrue(self.collected(loot))

    def test_takes_loot_above_the_best_owned(self):
        owned = FakeInventory(9, ammo="Ammo_Repeater_Pistol", level=1)
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=2)
        self.assertTrue(self.collected(loot, backpack=[owned]))

    def test_takes_loot_equal_to_the_best_owned(self):
        owned = FakeInventory(9, ammo="Ammo_Repeater_Pistol", level=2)
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=2)
        self.assertTrue(self.collected(loot, backpack=[owned]))

    def test_leaves_loot_below_the_best_owned(self):
        owned = FakeInventory(9, ammo="Ammo_Repeater_Pistol", level=3)
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=2)
        self.assertFalse(self.collected(loot, backpack=[owned]))

    def test_equipped_gear_counts_towards_the_best_owned(self):
        """Not just the backpack - the gun in your hands is the one to beat."""
        equipped = FakeInventory(9, ammo="Ammo_Repeater_Pistol", level=5)
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=2)
        self.assertFalse(self.collected(loot, equipped=[equipped]))

    def test_a_better_smg_does_not_block_a_pistol(self):
        """Weapons only compete within their own ammo type."""
        owned = FakeInventory(9, ammo="Ammo_Patrol_SMG", level=9)
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=2)
        self.assertTrue(self.collected(loot, backpack=[owned]))

    def test_gear_competes_within_its_own_category(self):
        shield = FakeInventory(9, "WillowShield", level=5)
        better_grenade = FakePickup(1, "WillowGrenadeMod", level=2)
        worse_shield = FakePickup(2, "WillowShield", level=2)
        self.assertTrue(self.collected(better_grenade, backpack=[shield]))
        self.assertFalse(self.collected(worse_shield, backpack=[shield]))

    def test_takes_loot_whose_level_cannot_be_read(self):
        """A missing or zero GameStage means the rule simply does not apply."""
        owned = FakeInventory(9, ammo="Ammo_Repeater_Pistol", level=50)
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=0)
        self.assertTrue(self.collected(loot, backpack=[owned]))

    def test_customizations_are_never_level_filtered(self):
        owned = FakeInventory(9, CUSTOMIZATION_CLASS, level=50)
        loot = customization(1, unlocked=False)
        loot.Inventory.DefinitionData.GameStage = 2
        self.assertTrue(self.collected(loot, backpack=[owned]))

    def test_pick_lower_level_on_collects_regardless(self):
        autoloot.pick_lower_level.value = True
        owned = FakeInventory(9, ammo="Ammo_Repeater_Pistol", level=50)
        loot = FakePickup(1, ammo="Ammo_Repeater_Pistol", level=2)
        pc = FakePlayerController([loot], backpack=[owned])
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [])

    # --- auto use customizations ---

    def test_auto_use_consumes_a_new_customization(self):
        autoloot.auto_use_customizations.value = True
        skin = FakeInventory(1, CUSTOMIZATION_CLASS, useful=True)
        pc = FakePlayerController([], backpack=[skin])
        run_one_scan(pc)
        self.assertTrue(skin.consumed)
        self.assertEqual(pc.inv_manager.Backpack, [])

    def test_auto_use_picks_up_then_uses_in_the_same_pass(self):
        autoloot.auto_use_customizations.value = True
        pc = FakePlayerController([customization(1, unlocked=False)])
        run_one_scan(pc)
        self.assertEqual(pc.willow_globals.PickupList, [], "should have collected it")
        self.assertEqual(pc.inv_manager.Backpack, [], "and used it straight away")

    def test_auto_use_covers_customizations_from_any_source(self):
        """It scans the backpack, so a mission or challenge reward counts too.

        Nothing is on the ground and pickup is switched off entirely, yet the
        skin still gets used - the two settings are independent.
        """
        autoloot.auto_use_customizations.value = True
        autoloot.pickup_customizations.value = autoloot.CHOICE_NONE
        reward = FakeInventory(1, CUSTOMIZATION_CLASS, useful=True)
        pc = FakePlayerController([], backpack=[reward])
        run_one_scan(pc)
        self.assertTrue(reward.consumed)

    def test_auto_use_does_not_spend_an_already_unlocked_one(self):
        """TryConsume refuses these itself, so the duplicate survives."""
        autoloot.auto_use_customizations.value = True
        dupe = FakeInventory(1, CUSTOMIZATION_CLASS, useful=False)
        pc = FakePlayerController([], backpack=[dupe])
        run_one_scan(pc)
        self.assertFalse(dupe.consumed)
        self.assertIn(dupe, pc.inv_manager.Backpack)

    def test_auto_use_respects_the_dlc_requirement(self):
        autoloot.auto_use_customizations.value = True
        locked = FakeInventory(1, CUSTOMIZATION_CLASS, useful=True)
        locked.dlc_met = False
        pc = FakePlayerController([], backpack=[locked])
        run_one_scan(pc)
        self.assertFalse(locked.consumed)

    def test_auto_use_off_leaves_customizations_alone(self):
        autoloot.auto_use_customizations.value = False
        skin = FakeInventory(1, CUSTOMIZATION_CLASS, useful=True)
        pc = FakePlayerController([], backpack=[skin])
        run_one_scan(pc)
        self.assertFalse(skin.consumed)
        self.assertIn(skin, pc.inv_manager.Backpack)

    def test_auto_use_never_touches_anything_else(self):
        autoloot.auto_use_customizations.value = True
        gun = FakeInventory(1, "WillowWeapon")
        mod = FakeInventory(2, CLASS_MOD_CLASS)
        pc = FakePlayerController([], backpack=[gun, mod])
        run_one_scan(pc)
        self.assertEqual(pc.inv_manager.Backpack, [gun, mod])

    def test_auto_use_consumes_every_customization_not_every_other_one(self):
        """Consuming shortens the backpack - the same hazard as PickupList."""
        autoloot.auto_use_customizations.value = True
        skins = [FakeInventory(i, CUSTOMIZATION_CLASS, useful=True) for i in range(5)]
        pc = FakePlayerController([], backpack=list(skins))
        run_one_scan(pc)
        self.assertTrue(all(skin.consumed for skin in skins))
        self.assertEqual(pc.inv_manager.Backpack, [])

    # --- backpack summary ---

    def test_summary_is_shown_once_when_the_pile_is_cleared(self):
        autoloot.hud_summary_seconds.value = 6
        shown = sys.modules["ui_utils"].shown
        pc = FakePlayerController([FakePickup(i) for i in range(3)], capacity=40)

        run_one_scan(pc)  # collects the pile
        self.assertEqual(shown, [], "must not report while still collecting")

        run_one_scan(pc)  # nothing left: report now
        self.assertEqual(len(shown), 1)

        run_one_scan(pc)  # and not again on every later idle pass
        self.assertEqual(len(shown), 1)

    def test_summary_counts_weapons_by_ammo_and_gear_by_category(self):
        autoloot.hud_summary_seconds.value = 6
        backpack = [
            FakeInventory(1, "WillowWeapon", ammo="Ammo_Repeater_Pistol"),
            FakeInventory(2, "WillowWeapon", ammo="Ammo_Repeater_Pistol"),
            FakeInventory(3, "WillowWeapon", ammo="Ammo_CombatRifle"),
            FakeInventory(4, "WillowShield"),
            FakeInventory(5, "WillowGrenadeMod"),
            FakeInventory(6, "WillowClassMod"),
            FakeInventory(7, "WillowArtifact"),
        ]
        pc = FakePlayerController([], capacity=39, backpack=backpack)
        autoloot.collected_last_pass = True  # pretend a pile just finished
        run_one_scan(pc)

        title, body, _duration = sys.modules["ui_utils"].shown[0]
        self.assertEqual(title, "Backpack  7/39")
        self.assertIn("Pistol 2", body)
        self.assertIn("Combat Rifle 1", body, "camel case ammo names must split")
        self.assertIn("Shields 1", body)
        self.assertIn("Grenade Mods 1", body)
        self.assertIn("Class Mods 1", body)
        self.assertIn(autoloot.ARTIFACT_LABEL + " 1", body)
        self.assertNotIn("Customizations", body, "empty categories are left out")

    def test_summary_ammo_names_come_from_the_game_not_a_table(self):
        """TPS lasers and BL2's lack of them both fall out of reading AmmoResource."""
        self.assertEqual(autoloot.prettify_ammo_name("Ammo_Laser"), "Laser")
        self.assertEqual(
            autoloot.prettify_ammo_name("Ammo_Combat_Rifle"), "Combat Rifle"
        )
        self.assertEqual(autoloot.prettify_ammo_name("Ammo_CombatRifle"), "Combat Rifle")
        self.assertEqual(autoloot.prettify_ammo_name("Ammo_Patrol_SMG"), "Patrol SMG")

    def test_summary_lines_are_sorted_by_amount(self):
        """Most first, so the thing you have most of leads the line."""
        line = autoloot.summary_line({"Pistol": 4, "Laser": 9, "SMG": 1, "Sniper": 0})
        self.assertEqual(line, "Laser 9 | Pistol 4 | SMG 1")

    def test_every_option_appears_in_the_menu_list(self):
        """MOD_OPTIONS is explicit, so a new option left out of it never renders."""
        option_type = sys.modules["mods_base"].BoolOption  # all stubs share a class
        declared = {
            name: value
            for name, value in vars(autoloot).items()
            if isinstance(value, option_type)
        }
        self.assertTrue(declared, "found no options at all - test is not working")
        missing = [
            name for name, value in declared.items() if value not in autoloot.MOD_OPTIONS
        ]
        self.assertEqual(missing, [], "these options would be invisible in game")

    def test_related_summary_options_are_adjacent(self):
        """They were rendering at opposite ends, sorted by variable name."""
        identifiers = [option.identifier for option in autoloot.MOD_OPTIONS]
        gap = abs(
            identifiers.index(autoloot.hud_summary_seconds.identifier)
            - identifiers.index(autoloot.summary_in_console.identifier)
        )
        self.assertEqual(gap, 1)

    def test_weapons_and_gear_share_one_sorted_run(self):
        """Gear outranks a weapon type when there is more of it, and vice versa."""
        backpack = [
            FakeInventory(1, "WillowShield"),
            FakeInventory(2, "WillowShield"),
            FakeInventory(3, "WillowShield"),
            FakeInventory(4, "WillowWeapon", ammo="Ammo_Laser"),
            FakeInventory(5, "WillowWeapon", ammo="Ammo_Laser"),
            FakeInventory(6, "WillowClassMod"),
        ]
        pc = FakePlayerController([], capacity=39, backpack=backpack)
        counts, used, capacity = autoloot.backpack_tally(pc)
        _title, body = autoloot.format_tally(counts, used, capacity)

        self.assertEqual(body, "Shields 3 | Laser 2 | Class Mods 1")
        self.assertNotIn("\n", body, "everything belongs on one line now")

    def test_equal_amounts_break_ties_by_name(self):
        """Otherwise the order would vary with backpack layout, run to run."""
        counts = {"Shotgun": 2, "Pistol": 2, "Laser": 2}
        self.assertEqual(
            autoloot.summary_line(counts), "Laser 2 | Pistol 2 | Shotgun 2"
        )

    def test_summary_duration_setting_reaches_the_hud(self):
        autoloot.hud_summary_seconds.value = 11
        pc = FakePlayerController([FakePickup(1)], capacity=39)
        run_one_scan(pc)
        run_one_scan(pc)
        _title, _body, duration = sys.modules["ui_utils"].shown[0]
        self.assertEqual(duration, 11)

    def test_zero_seconds_disables_the_on_screen_summary(self):
        autoloot.hud_summary_seconds.value = 0
        autoloot.summary_in_console.value = True
        pc = FakePlayerController([FakePickup(1)], capacity=39)
        run_one_scan(pc)
        run_one_scan(pc)
        self.assertEqual(sys.modules["ui_utils"].shown, [])
        self.assertEqual(len(sys.modules["unrealsdk.logging"].logged), 1)

    def test_summary_text_is_pure_ascii(self):
        """The HUD font drew a box for the middle dot that used to separate these."""
        title, body = autoloot.format_tally({"Pistol": 2, "Shields": 1}, 3, 39)
        for text in (title, body, autoloot.SUMMARY_SEPARATOR):
            self.assertTrue(text.isascii(), f"non-ascii will render as boxes: {text!r}")

    def test_both_outputs_can_be_on_at_once(self):
        autoloot.hud_summary_seconds.value = 6
        autoloot.summary_in_console.value = True
        pc = FakePlayerController([FakePickup(1)], capacity=39)
        run_one_scan(pc)
        run_one_scan(pc)
        self.assertEqual(len(sys.modules["ui_utils"].shown), 1)
        self.assertEqual(len(sys.modules["unrealsdk.logging"].logged), 1)

    def test_summary_off_shows_nothing(self):
        autoloot.hud_summary_seconds.value = 0
        autoloot.summary_in_console.value = False
        pc = FakePlayerController([FakePickup(1)])
        run_one_scan(pc)
        run_one_scan(pc)
        self.assertEqual(sys.modules["ui_utils"].shown, [])

    def test_summary_console_mode_logs_instead_of_drawing(self):
        autoloot.summary_in_console.value = True
        pc = FakePlayerController([FakePickup(1)], capacity=39)
        run_one_scan(pc)
        run_one_scan(pc)
        self.assertEqual(sys.modules["ui_utils"].shown, [])
        self.assertEqual(len(sys.modules["unrealsdk.logging"].logged), 1)

    def test_summary_is_not_shown_when_nothing_was_collected(self):
        autoloot.hud_summary_seconds.value = 6
        pc = FakePlayerController([])
        run_one_scan(pc)
        run_one_scan(pc)
        self.assertEqual(sys.modules["ui_utils"].shown, [])

    def test_never_reads_a_pickup_after_the_engine_destroys_it(self):
        """FakePickup raises a BaseException if this is violated, so a plain pass
        of the other tests already proves it - this states the rule explicitly."""
        pc = FakePlayerController([FakePickup(i) for i in range(3)])
        run_one_scan(pc)  # would raise TouchedDestroyedPickup
        self.assertEqual(pc.willow_globals.PickupList, [])

    def test_skips_the_pass_while_the_pawn_is_missing(self):
        """PlayerTick fires during loading, before there is anyone to give loot to."""
        pc = FakePlayerController([FakePickup(1)])
        pc.Pawn = None
        run_one_scan(pc)
        self.assertEqual(pc.attempts, 0)
        self.assertEqual(autoloot.ticks_until_scan, autoloot.SCAN_INTERVAL)

    def test_nothing_is_ever_removed_from_the_backpack(self):
        """AutoLoot only ever collects - it must never discard the player's items."""
        held = [
            FakeInventory(1, CUSTOMIZATION_CLASS, useful=False),  # already unlocked
            FakeInventory(2, "WillowWeapon", useful=False),
        ]
        pc = FakePlayerController([], backpack=list(held))
        autoloot.pickup_customizations.value = autoloot.CUSTOMIZATIONS_NEW
        run_one_scan(pc)
        self.assertEqual(pc.inv_manager.Backpack, held)

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
