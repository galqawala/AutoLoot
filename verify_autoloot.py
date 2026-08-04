"""Check AutoLoot's decisions against the REAL game. Changes nothing.

Run it from the SDK console (tilde) while stood in a level:

    pyexec verify_autoloot.py

Every call below only reads state. It never picks anything up, never drops,
never equips, never consumes, and never saves - so your character is untouched.
The functions that DO change things are checked for existence only, by name.

This exists because a test suite that stands in for the engine cannot tell you
whether the engine agrees. Three shipped bugs got through that way: hooks
returning a bool where the SDK blocks only on its Block sentinel, HasAnyAmmo
asked of backpack weapons that have no ammo pool, and dropping a backpack item
with ThrowInventory, whose server half refuses anything the pawn does not own.
Each shows up here in one line, against your actual inventory.
"""

import sys

from mods_base import get_pc

autoloot = sys.modules.get("AutoLoot")
WAS_ALREADY_LOADED = autoloot is not None
if autoloot is None:  # not loaded as a mod, eg running from an extracted folder
    import AutoLoot as autoloot  # noqa: F401


FAILURES = []


def show(label, value):
    print(f"  {label:<44} {value}")


def check(label, actual, expected):
    """Assert against a value worked out by hand, not by rerunning the code."""
    if actual == expected:
        show(f"PASS  {label}", actual)
    else:
        FAILURES.append(label)
        show(f"FAIL  {label}", f"{actual!r} != expected {expected!r}")


def section(title):
    print("")
    print(f"--- {title} ".ljust(72, "-"))


def attempt(label, call):
    """Run one read-only probe, reporting a failure instead of aborting."""
    try:
        show(label, call())
    except Exception as ex:  # noqa: BLE001
        show(label, f"!! FAILED: {ex!r}")


def name_of(item):
    if item is None:
        return "none"
    try:
        return f"{item.Class.Name} lvl {autoloot.item_level(item)}"
    except Exception as ex:  # noqa: BLE001
        return f"<unreadable: {ex!r}>"


def main():
    pc = get_pc()
    manager = pc.GetPawnInventoryManager()
    print("")
    print("=" * 72)
    print("AutoLoot verification - read only, nothing is changed")
    print("=" * 72)

    section("which AutoLoot is being inspected")
    # Without this you cannot tell whether you checked the mod the game is
    # actually running or a fresh copy this script imported itself.
    show("module file", getattr(autoloot, "__file__", "?"))
    show("version", getattr(autoloot, "__version__", "?"))
    show(
        "source",
        "the running mod" if WAS_ALREADY_LOADED else "!! freshly imported, NOT the live mod",
    )

    section("logic checks - real functions, values worked out by hand")
    check(
        "prettify_ammo_name underscores",
        autoloot.prettify_ammo_name("Ammo_Combat_Rifle"),
        "Combat Rifle",
    )
    check(
        "prettify_ammo_name camel case",
        autoloot.prettify_ammo_name("Ammo_CombatRifle"),
        "Combat Rifle",
    )
    check(
        "prettify_ammo_name keeps initialisms",
        autoloot.prettify_ammo_name("Ammo_Patrol_SMG"),
        "Patrol SMG",
    )
    check(
        "summary_line orders by amount, drops zeroes",
        autoloot.summary_line(
            {"Pistol": 4, "Laser": 9, "SMG": 1, "Sniper": 0},
            {"Pistol": (14, 36), "Laser": (13, 16), "SMG": (5, 5)},
        ),
        "9 Laser (lvl 13-16) | 4 Pistol (lvl 14-36) | 1 SMG (lvl 5)",
    )
    check(
        "summary_line ties on amount but keeps every entry",
        sorted(autoloot.summary_line({"Shotgun": 2, "Pistol": 2, "Laser": 2}, {}).split(" | ")),
        ["2 Laser", "2 Pistol", "2 Shotgun"],
    )
    check(
        "summary title shows used of capacity",
        autoloot.format_tally({"Pistol": 2}, {"Pistol": (10, 10)}, 3, 39)[0],
        "Backpack  3/39",
    )
    check(
        "summary separator is ascii (the HUD drew a box for a dot)",
        autoloot.SUMMARY_SEPARATOR.isascii(),
        True,
    )
    listed = {o.identifier for o in autoloot.MOD_OPTIONS}
    declared = {
        v.identifier
        for v in vars(autoloot).values()
        if hasattr(v, "identifier") and hasattr(v, "value")
    }
    check("every option is in MOD_OPTIONS", sorted(declared - listed), [])
    slot_of = [o.identifier for o in autoloot.MOD_OPTIONS]
    check(
        "the two summary options sit together",
        abs(
            slot_of.index(autoloot.hud_summary_seconds.identifier)
            - slot_of.index(autoloot.summary_in_console.identifier)
        ),
        1,
    )

    section("character and backpack")
    attempt("character_level(pc)", lambda: autoloot.character_level(pc))
    attempt("CountUnreadiedInventory()", manager.CountUnreadiedInventory)
    attempt("GetUnreadiedInventoryMaxSize()", manager.GetUnreadiedInventoryMaxSize)
    attempt("GetEmptyBackpackSlots()", manager.GetEmptyBackpackSlots)
    attempt("backpack_is_full(pc)", lambda: autoloot.backpack_is_full(pc))
    attempt("GetWeaponReadyMax() (unlocked slots)", manager.GetWeaponReadyMax)

    backpack = [item for item in manager.Backpack if item is not None]

    section("equipped weapons")
    equipped = autoloot.equipped_weapons(pc)
    show("slots holding a weapon", sorted(equipped))
    for slot in sorted(equipped):
        weapon = equipped[slot]
        attempt(
            f"slot {slot}: {weapon.Class.Name}",
            lambda w=weapon: f"HasAnyAmmo={w.HasAnyAmmo()}",
        )
    attempt("first_empty_weapon_slot()", lambda: autoloot.first_empty_weapon_slot(manager))
    held_slot = 0 if pc.Pawn is None or pc.Pawn.Weapon is None else int(
        pc.Pawn.Weapon.QuickSelectSlot
    )
    if held_slot:
        attempt(
            f"next_loaded_slot(from {held_slot})",
            lambda: autoloot.next_loaded_slot(held_slot, equipped),
        )
        attempt(
            f"next_occupied_slot(from {held_slot})  [bounce-through target]",
            lambda: autoloot.next_occupied_slot(held_slot, equipped),
        )
    show(
        "pending backpack re-equip",
        autoloot._reequip if autoloot._reequip["stage"] else "none in progress",
    )
    held = pc.Pawn.Weapon if pc.Pawn is not None else None
    show("weapon in hand", name_of(held))
    if held is not None:
        attempt("  has_ammo(held)", lambda: autoloot.has_ammo(held))
        attempt(
            "  is_holding_fire(pc)  [bWantsToFire - confirmed live]",
            lambda: autoloot.is_holding_fire(pc),
        )
        attempt(
            "  PendingFire per mode  [self-clears on empty - not the gate]",
            lambda: [
                bool(held.PendingFire(m))
                for m in range(int(held.GetPendingFireLength()))
            ],
        )
        attempt("  weapon_swap_in_progress(pc)", lambda: autoloot.weapon_swap_in_progress(pc))
        attempt(
            "  manager.InventoryTransitionInProgress()", manager.InventoryTransitionInProgress
        )
        show(
            "  note",
            "all of these read as idle here - the console releases the mouse"
            " and stops firing. The mod logs the real values at each switch.",
        )

    section("backpack ammo - the check that silently failed before")
    weapons = [i for i in backpack if autoloot.WEAPON_CLASS_TOKEN in i.Class.Name]
    show("backpack weapons", len(weapons))
    loaded = 0
    per_kind = {}
    for weapon in weapons:
        try:
            ok = autoloot.player_has_ammo_for(pc, weapon)
        except Exception as ex:  # noqa: BLE001
            show("player_has_ammo_for", f"!! FAILED: {ex!r}")
            break
        loaded += bool(ok)
        kind = autoloot.item_kind(weapon)
        hit, total = per_kind.get(kind, (0, 0))
        per_kind[kind] = (hit + bool(ok), total + 1)
    else:
        show("report having ammo", f"{loaded} of {len(weapons)}")
        if weapons and loaded == 0:
            show("", "!! SUSPECT: no backpack weapon has ammo")
        for kind in sorted(per_kind):
            hit, total = per_kind[kind]
            show(f"  {kind}", f"{hit}/{total} loaded")

    section("marks - all favourite would block every drop")
    marks = {}
    for item in backpack:
        try:
            marks[int(item.GetMark())] = marks.get(int(item.GetMark()), 0) + 1
        except Exception as ex:  # noqa: BLE001
            marks[f"unreadable ({ex!r})"] = marks.get("unreadable", 0) + 1
    show("mark counts (0 trash, 1 standard, 2 favourite)", marks)
    attempt(
        "is_favorite() says favourite",
        lambda: sum(1 for i in backpack if autoloot.is_favorite(i)),
    )
    attempt("CanDrop() allows", lambda: sum(1 for i in backpack if pc.CanDrop(i)))

    section("what the mod would decide right now")
    attempt("worst_owned_levels(pc)", lambda: autoloot.worst_owned_levels(pc))
    attempt(
        "choose_item_to_drop(pc)  [not dropped]",
        lambda: name_of(autoloot.choose_item_to_drop(pc)),
    )
    attempt(
        "backpack items that would fill a slot",
        lambda: sum(
            1
            for i in backpack
            if autoloot.is_worth_equipping(pc, i)
            and manager.InventoryShouldBeReadiedWhenEquipped(i)
        ),
    )

    section("mutating calls - existence only, never invoked here")
    for owner, label, attr in (
        (manager, "manager.ThrowBackpackInventory", "ThrowBackpackInventory"),
        (manager, "manager.ReadyBackpackInventory", "ReadyBackpackInventory"),
        (pc, "pc.EquipWeaponFromSlot", "EquipWeaponFromSlot"),
        (pc, "pc.PickupPickupable", "PickupPickupable"),
        (pc, "pc.ThrowInventory  (equipped only!)", "ThrowInventory"),
    ):
        show(label, "present" if hasattr(owner, attr) else "!! MISSING")

    print("")
    if FAILURES:
        print(f"{len(FAILURES)} LOGIC CHECK(S) FAILED: {', '.join(FAILURES)}")
    else:
        print("All logic checks passed.")
    print("Done. Nothing was changed, and nothing was saved.")
    print("=" * 72)


main()
