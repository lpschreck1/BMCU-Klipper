# Resume sweep

Reference print `T0 | T4 T6 | T3`, each fault injected just before the trigger line, then the user clears it and presses Resume (up to 2 times). Regenerate with `python tests/sim/sweep.py --write`.

`no_effect` for BMCU faults at a tool change or layer start means BMCU had already loaded that source in the background (it prestages while the previous tool prints), so the fault came too late; the faults that matter for BMCU loads are the `print start` and `auto feed` rows. `deformed` and `clog` clears model the user hand-feeding the filament through (faults.py).

| Trigger | Fault | Outcome | First pause message |
|---|---|---|---|
| print start | snag T4 58mm | resumed | ENDPOINT_NOT_REACHED: buffer_limit after 58.0 mm. Check the path for a snag or kink, and that the tip is cut clean; this Head is shared, so also check that no other source's filament is parked past the splitter (bmcu0 Ch |
| print start | snag T6 58mm | resumed | ENDPOINT_NOT_REACHED: buffer_limit after 58.0 mm. Check the path for a snag or kink, and that the tip is cut clean; this Head is shared, so also check that no other source's filament is parked past the splitter (bmcu0 Ch |
| print start | clog head 3 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 3 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| print start | clog head 4 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 4 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| print start | deformed T4 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 3 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| print start | deformed T6 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 4 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| print start | deformed stock T0 | resumed | extruder[0]: state: load_extruding, error: move_extrude! |
| print start | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |
| auto feed T0 | deformed stock T0 | resumed | extruder[0]: state: load_extruding, error: move_extrude! |
| auto feed head 3 | snag T4 58mm | resumed | ENDPOINT_NOT_REACHED: buffer_limit after 58.0 mm. Check the path for a snag or kink, and that the tip is cut clean; this Head is shared, so also check that no other source's filament is parked past the splitter (bmcu0 Ch |
| auto feed head 3 | clog head 3 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 3 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| auto feed head 3 | deformed T4 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 3 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| auto feed head 4 | clog head 4 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 4 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| auto feed head 4 | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |
| tool change T4 | snag T4 58mm | no_effect |  |
| tool change T4 | clog head 3 | no_effect |  |
| tool change T4 | deformed T4 | no_effect |  |
| tool change T6 | snag T6 58mm | no_effect |  |
| tool change T6 | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |
| tool change T6 | deformed T6 | no_effect |  |
| tool change T3 | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |
| tool change T3 | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |
| layer 2 start | snag T4 58mm | no_effect |  |
| layer 2 start | clog head 3 | no_effect |  |
| layer 2 start | deformed T4 | no_effect |  |
| layer 2 start | snag T6 58mm | no_effect |  |
| layer 2 start | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |
| layer 2 start | deformed T6 | no_effect |  |
| layer 3 start | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |
| layer 3 start | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! |

**30 cases, 10 no effect, 20 resumed, 0 stuck.**
