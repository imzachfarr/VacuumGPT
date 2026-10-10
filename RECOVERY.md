# Recovery behavior

Automatic recovery is optional and applies only to a confirmed mission started through this controller. It preserves the full requested room sequence, cleaning mode, suction, passes and the native mission anchor. It resumes the vendor's retained planner; observed travel is never treated as cleaning coverage.

Every resume, including an explicit chat request, requires a complete retained native CLEAN anchor before dispatch. Ended or incomplete cycles and changed mission anchors are refused before publication; a reused mission ID is insufficient.

A candidate requires fresh ready telemetry, the unchanged native mission and map, a positively known pause origin, remaining retry budget and no owner hold. The default delay is 45 seconds, maximum two attempts in six hours, with a 180-second cooldown. Unknown or app/user pause origins, faults, low battery, changed context, completed/ended missions and explicit pause/stop/dock block automatic recovery. STOP+CLEAN counts as a retained pause only with the exact native pause status and command-origin evidence; STOP+NONE is ended.

## Durable dispatch and owner commands

Recovery claims use a private SQLite outbox. Queued attempts survive a Mac restart. Dispatch is claimed atomically before the wire command; possibly dispatched, interrupted or uncertain deliveries are never blindly replayed. Owner pause/stop/dock increments a hold generation before dispatch. A late running-state receipt cannot clear a newer owner hold.

## Inspected main-brush retry

The separate inspected-brush tool permits one owner-requested normal resume after the human confirms physical brush inspection. It requires fresh STUCK/error 2, a complete retained CLEAN native anchor, no readiness blockers, battery at least 25 percent and unchanged final state. Its durable limit is one claim per mission and at most one in six hours. An ended NONE cycle is refused before publication because ordinary resume from idle can start a new clean.

Confirmation requires a newer current-state version than the final fault check, phase and error report timestamps after the wire command, the exact resume/localApp echo and the unchanged mission anchor. A cached RUN/error 0 cannot release the hold or claim successful recovery. This tool does not reset faults or bypass onboard protection. Unfinished-only continuation through an active brush fault remains unverified on the hardware-tested model.

## Read-only diagnostics

The diagnostic tool reads a fixed allowlist through the existing authenticated observer session. It filters arbitrary native error text and exports only numeric recognized codes, report ages and allowed counters. A successful cloud GET does not update a robot field's timestamp. Missing fields or stale telemetry cannot establish a fault-free running state.

The exact reviewed 105 software reports two recognized native brush faults as cloud error 2, so the cloud value cannot distinguish their subcodes or expose brush current/RPM. On `p25-105+8.4.0+I3.7.118`, native 2102 belongs to a separate pause/broken-clean state family and is not main-brush error 2. Other native reports stay unclassified unless their specific semantics are established. These are diagnostic classifications, not new automatic recovery permissions.

A reboot ends planner continuity. The internal one-use app soft-restart implementation is separate from normal chat controls and automatic recovery; no persistent reboot permission or fault-clear tool is exposed.

## Schedules and maps

A local schedule pins the exact map ID and version, and room names must resolve to vendor region IDs with retained geometry. Busy, stale, faulted, missed or changed-map occurrences are skipped. Map-binding repair must revalidate each owner-requested room before replacing the saved version; copying a previous owner's schedule or assuming unchanged boundaries is insufficient. The runtime must remain awake and connected when the schedule is due.
