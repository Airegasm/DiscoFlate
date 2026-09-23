"""Awards must STACK on a pump that is already running.

The roulette spins on its own clock — it queues a percentage on someone's pump,
waits, and spins again whether or not that pump has finished. So a second award
lands mid-fire routinely rather than exceptionally, and "add 10%" has to mean
ten more points than were already queued.

Measuring an add from the LIVE meter instead quietly loses the difference, and
loses more of it the faster awards arrive relative to the pump — which means the
slower rig is shorted more often. That would rig a match against the very pump
pace compensation exists to protect, so it is a fairness bug, not a rounding one.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as eng

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


def rig(cal=60):
    e = eng.Engine()
    e.set_config({"devices": [{"id": "d1", "label": "P1", "host": "h",
                               "calibration_seconds_to_100": cal}],
                  "active_device_id": "d1",
                  "capacity_ranges": [{"min": 0, "max": 999}]})
    return e


def add(e, pct):
    """What the fire row now computes for fire_mode 'add'."""
    return e._pending_capacity("d1") + pct


def queued(e, target):
    e._fires["d1"] = {"deadline": 9e9, "until_capacity": target,
                      "alias": "P1", "abort": None, "extend": None}


# ---- an idle pump ----------------------------------------------------------- #
e = rig()
e.capacity = 12.0
ok(e._pending_capacity("d1") == 12.0, "an idle pump is at its live capacity")
ok(add(e, 10) == 22.0, "…so one award is just capacity + award")
ok(e._pending_capacity("nope") == 12.0, "an unknown device falls back to the meter")

# ---- the case that was broken ----------------------------------------------- #
e = rig()
e.capacity = 0.0
first = add(e, 10)
queued(e, first)
ok(first == 10.0, "spin 1: 10% from cold")
e.capacity = 7.0                     # 15s later, still climbing toward 10
second = add(e, 10)
ok(second == 20.0, "spin 2 lands mid-fire and still totals 20%, not 17%")
queued(e, second)
e.capacity = 14.0
ok(add(e, 10) == 30.0, "spin 3 keeps stacking")

# ---- the faster the spins, the more it used to lose ------------------------- #
e = rig()
e.capacity = 0.0
total = 0.0
for i in range(5):
    total = add(e, 8)
    queued(e, total)
    e.capacity += 1.0                # barely any progress between spins
ok(abs(total - 40.0) < 1e-9, "five 8% awards on a pump that never catches up = 40%")

# the old behaviour, for contrast: measured from the meter each time
naive, capm = 0.0, 0.0
for i in range(5):
    naive = capm + 8
    capm += 1.0
ok(naive < 40.0, "…where measuring from the live meter would have lost most of it")

# ---- a pump that overshot its target must not drag the next award back ------ #
e = rig()
e.capacity = 30.0
queued(e, 25.0)                       # target already passed
ok(e._pending_capacity("d1") == 30.0, "an overshot target never pulls an award backwards")
ok(add(e, 5) == 35.0, "…the next award builds on where the pump actually is")

# ---- a seconds-fire has no capacity target --------------------------------- #
e = rig()
e.capacity = 40.0
e._fires["d1"] = {"deadline": 9e9, "until_capacity": None}
ok(e._pending_capacity("d1") == 40.0,
   "a running SECONDS fire has no target to stack on, so the meter is right")


print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
