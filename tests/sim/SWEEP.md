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
| print start | deformed stock T0 | resumed | extruder[0]: state: load_extruding, error: move_extrude! Head 1: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| print start | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| auto feed T0 | deformed stock T0 | resumed | extruder[0]: state: load_extruding, error: move_extrude! Head 1: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| auto feed head 3 | snag T4 58mm | resumed | ENDPOINT_NOT_REACHED: buffer_limit after 58.0 mm. Check the path for a snag or kink, and that the tip is cut clean; this Head is shared, so also check that no other source's filament is parked past the splitter (bmcu0 Ch |
| auto feed head 3 | clog head 3 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 3 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| auto feed head 3 | deformed T4 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 3 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| auto feed head 4 | clog head 4 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 4 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| auto feed head 4 | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| tool change T4 | snag T4 58mm | no_effect |  |
| tool change T4 | clog head 3 | no_effect |  |
| tool change T4 | deformed T4 | no_effect |  |
| tool change T6 | snag T6 58mm | no_effect |  |
| tool change T6 | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| tool change T6 | deformed T6 | no_effect |  |
| tool change T3 | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| tool change T3 | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| layer 2 start | snag T4 58mm | no_effect |  |
| layer 2 start | clog head 3 | no_effect |  |
| layer 2 start | deformed T4 | no_effect |  |
| layer 2 start | snag T6 58mm | no_effect |  |
| layer 2 start | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| layer 2 start | deformed T6 | no_effect |  |
| layer 3 start | clog head 4 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |
| layer 3 start | deformed stock T3 | resumed | extruder[3]: state: load_extruding, error: move_extrude! Head 4: the stock feeder pushed the filament to the head but the extruder could not grip it. Pull the filament out, cut the tip clean at an angle, check the nozzle |

**30 cases, 10 no effect, 20 resumed, 0 stuck.**

## M8 disconnect sweep

A BMCU cable glitch (3 s), an unplug and replug (30 s) and a cable that stays out until the user replugs it, injected at BMCU phases (faults.py phase triggers). The user replugs and presses Resume. The plan is `T0 | T4 T6 | T5 | T6 T0`.

Outcomes: `no_effect` (finished without pausing), `resumed` (paused, fix + Resume finished it), `stuck` (no user action finished it), `dirty` (finished, but the shared checks of `sim/checks.py` failed: BMCU and the world disagree, a lock was left, or a head printed dry).

| Point | Fault | Outcome | First pause message / problem |
|---|---|---|---|
| idle mid-layer | disconnect 3 s | no_effect |  |
| idle mid-layer | disconnect 30 s | no_effect |  |
| idle mid-layer | disconnect until replug | stuck | stuck: paused after 3 resumes (u1_head2 ownership is safety_hold; reconcile it before moving filament) |
| load 50 mm | disconnect 3 s | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |
| load 50 mm | disconnect 30 s | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |
| load 50 mm | disconnect until replug | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |
| load 500 mm | disconnect 3 s | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |
| load 500 mm | disconnect 30 s | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |
| load 500 mm | disconnect until replug | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |
| unload 50 mm | disconnect 3 s | stuck | stuck: paused after 3 resumes (u1_head2 route journal disagrees with the live EMPTY snapshot: bmcu0 Channel 1; confirm the physical route before moving filament) |
| unload 50 mm | disconnect 30 s | stuck | stuck: paused after 3 resumes (u1_head2 route journal disagrees with the live EMPTY snapshot: bmcu0 Channel 1; confirm the physical route before moving filament) |
| unload 50 mm | disconnect until replug | stuck | stuck: paused after 3 resumes (u1_head2 route journal disagrees with the live EMPTY snapshot: bmcu0 Channel 1; confirm the physical route before moving filament) |
| BEFORE_ON_USE | disconnect 3 s | stuck | stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check it against) |
| BEFORE_ON_USE | disconnect 30 s | stuck | stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check it against) |
| BEFORE_ON_USE | disconnect until replug | stuck | stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check it against) |
| prestaging T5 | disconnect 3 s | stuck | stuck: paused after 3 resumes (u1_head2 has an UNCERTAIN BMCU route: bmcu0 Channel 1) |
| prestaging T5 | disconnect 30 s | stuck | stuck: paused after 3 resumes (u1_head2 has an UNCERTAIN BMCU route: bmcu0 Channel 1) |
| prestaging T5 | disconnect until replug | stuck | stuck: paused after 3 resumes (u1_head2 has an UNCERTAIN BMCU route: bmcu0 Channel 1) |

**18 cases, 2 no effect, 0 resumed, 16 stuck.**

## M8 staging faults

Snag, deformed tip and runout injected through BMCU phases, so they hit the load itself and not the tool-change line (the open item of the M6 sweep).

Outcomes: `no_effect` (finished without pausing), `resumed` (paused, fix + Resume finished it), `stuck` (no user action finished it), `dirty` (finished, but the shared checks of `sim/checks.py` failed: BMCU and the world disagree, a lock was left, or a head printed dry).

| Point | Fault | Outcome | First pause message / problem |
|---|---|---|---|
| prestaging T5 | snag bmcu1 58 mm | stuck | stuck: refused after 3 resumes (resume refused: e2 filament runout) |
| prestaging T5 | deformed bmcu1 | dirty | head 2 printed 53.0 mm of plastic it did not have |
| prestaging T5 | runout bmcu1 | stuck | stuck: refused after 3 resumes (resume refused: e2 filament runout) |
| load T5 20 mm | snag bmcu1 58 mm | stuck | stuck: refused after 3 resumes (resume refused: e2 filament runout) |
| load T5 20 mm | deformed bmcu1 | dirty | head 2 printed 53.0 mm of plastic it did not have |
| load T5 20 mm | runout bmcu1 | stuck | stuck: refused after 3 resumes (resume refused: e2 filament runout) |
| load T4 20 mm | snag bmcu0 58 mm | resumed | ENDPOINT_NOT_REACHED: buffer_limit after 58.0 mm. Check the path for a snag or kink, and that the tip is cut clean; this Head is shared, so also check that no other source's filament is parked past the splitter (bmcu0 Ch |
| load T4 20 mm | deformed bmcu0 | resumed | U1_TOOLHEAD_NOT_LOADED: Head 3 saw no convincing downstream start-to-finish coil response during the normal BEFORE_ON_USE path (path_delta=0.0, gentle_required>=400.0) after 2 final read(s). BMCU pressure and Head motion |
| load T4 20 mm | runout bmcu0 | resumed | ENDPOINT_NOT_REACHED: no_filament after 23.7 mm. Check the path for a snag or kink, and that the tip is cut clean; this Head is shared, so also check that no other source's filament is parked past the splitter (bmcu0 Cha |

**9 cases, 0 no effect, 3 resumed, 4 stuck, 2 finished dirty.**

## M8 helper death

The transport helper process exits mid-print. Its socket vanishes; the supervisor (`bmcu_host_bootstrap.py --repair`) starts a new one when the user (or 30 s later) does.

Outcomes: `no_effect` (finished without pausing), `resumed` (paused, fix + Resume finished it), `stuck` (no user action finished it), `dirty` (finished, but the shared checks of `sim/checks.py` failed: BMCU and the world disagree, a lock was left, or a head printed dry).

| Point | Fault | Outcome | First pause message / problem |
|---|---|---|---|
| idle mid-layer | helper exit, repaired by user | stuck | stuck: paused after 3 resumes (u1_head2 ownership is safety_hold; reconcile it before moving filament) |
| load 50 mm | helper exit, repaired by user | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |
| unload 50 mm | helper exit, repaired by user | stuck | stuck: paused after 3 resumes (u1_head2 route journal disagrees with the live EMPTY snapshot: bmcu0 Channel 1; confirm the physical route before moving filament) |
| BEFORE_ON_USE | helper exit, repaired by user | stuck | pause message does not say what to do: 'transport sidecar hangup (EOF)'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check it ag |
| load 50 mm | helper exit, repaired after 30 s | stuck | pause message does not say what to do: 'DEVICE_OFFLINE during feed operation'; stuck: paused after 3 resumes (Head 3 has filament at its sensor that BMCU has no record of, and none of its BMCU Channels is online to check |

**5 cases, 0 no effect, 0 resumed, 5 stuck.**

## M9 restarts

Klipper restarts (`klipper`: the BMCU helper and firmware keep running, the user starts the print again, cancelling and starting once more if the first start fails) and power loss (`power`: everything restarts, the user takes the power-loss resume). After the boot the BMCU view must match the world or be UNCERTAIN; at the end no tool change may be skipped or repeated.

Outcomes: `no_effect` (finished without pausing), `resumed` (paused, fix + Resume finished it), `stuck` (no user action finished it), `dirty` (finished, but the shared checks of `sim/checks.py` failed: BMCU and the world disagree, a lock was left, or a head printed dry).

| Point | Fault | Outcome | First pause message / problem |
|---|---|---|---|
| idle mid-layer | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| BMCU load 50 mm | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| BMCU load near arrival | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| BMCU unload 50 mm | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| between unload and load | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| stock hand-back, load_feeding | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| BEFORE_ON_USE | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| durable write queued, not landed | klipper restart | resumed | cannot replace the active BMCU print plan while printer state is printing |
| state file empty at boot | klipper restart | no_effect |  |
| state file truncated at boot | klipper restart | no_effect |  |
| idle mid-layer | power restart | stuck | stuck: paused after 3 resumes (u1_head2 route journal disagrees with the live EMPTY snapshot: bmcu0 Channel 1; confirm the physical route before moving filament) |
| BMCU load 50 mm | power restart | stuck | the print failed outright (error): BMCU print plan is not committed before automatic feed; keep the generated BMCU_PRINT_BEGIN/MAP/COMMIT block above PRINT_START |
| BMCU load near arrival | power restart | stuck | the print failed outright (error): BMCU print plan is not committed before automatic feed; keep the generated BMCU_PRINT_BEGIN/MAP/COMMIT block above PRINT_START |
| BMCU unload 50 mm | power restart | stuck | stuck: refused after 3 resumes (resume refused: e2 filament runout); user action refused: BMCU route confirmation failed safely; no further action was applied. Check klippy.log before retrying: route confirmation is unav |
| between unload and load | power restart | no_effect |  |
| stock hand-back, load_feeding | power restart | no_effect |  |
| BEFORE_ON_USE | power restart | stuck | stuck: paused after 3 resumes (automatic feed cannot determine the first physical source for hybrid head 2 from the current print position); user action refused: BMCU route confirmation failed safely; no further action w |
| durable write queued, not landed | power restart | stuck | stuck: paused after 3 resumes (u1_head2 route journal disagrees with the live EMPTY snapshot: bmcu0 Channel 1; confirm the physical route before moving filament) |
| state file empty at boot | power restart | no_effect |  |
| state file truncated at boot | power restart | no_effect |  |

**20 cases, 6 no effect, 8 resumed, 6 stuck.**
