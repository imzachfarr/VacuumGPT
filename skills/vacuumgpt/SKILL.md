---
name: vacuumgpt
description: Connect and operate a Roomba through the VacuumGPT MCP connector, inspect status and maps, clean named rooms, manage local schedules, and diagnose interrupted missions.
---

Use the VacuumGPT MCP connector for the owner's selected robot. Setup selects an exact Prime/V4 device; never assume a model, room ID, map, timezone, or previous owner's grant.

If tools are missing, run the bundle's Install.command in the owner's local macOS Terminal. The owner enters their iRobot login there, selects their robot and agent, and grants requested command access. Never ask for account passwords in chat. Restart the agent after installation. A skill alone does not provide robot access.

Read roomba_status before acting. Check freshness, identity, mission, faults and control_policy. When direct_chat_commands is true, execute explicit owner requests through roomba_command, roomba_clean_room or roomba_clean_rooms; the persistent grant removes the need for another Terminal approval. Keep one stable unique request_id for each human intent. Reuse that ID only for the same intent. Inspect command results/history after uncertain delivery; never resend using a new ID.

Read roomba_rooms to find map-scoped names and region IDs. Resolve the requested name against the active map. If unnamed, show the map to the owner and use roomba_name_room for their chosen alias. It saves locally and sends no robot command. Never guess which room an ID represents. A named-room request cannot become whole-house cleaning. start means whole-house and requires that intent. New cleaning requires idle state; do not silently replace an active mission.

Report broker acknowledgement separately from confirmed robot behavior. Travel is not cleaning coverage. Hardware adherence to room order, settings and drawn boundaries cannot be inferred from a running-state transition.

Read roomba_recovery_status for the preserved room/setting intent, native anchor, eligibility reason and durable attempts. Retained STOP+CLEAN is a pause only when exact native status and lastCommand evidence establish that origin. A mission ID alone, cached state or a cloud GET timestamp cannot prove continuity. Every resume requires a complete retained native CLEAN anchor and an unchanged final anchor before dispatch, including explicit manual requests. Recovery must keep the same native mission, map and targets; it cannot reconstruct unfinished area after an ended mission or reboot. Queued attempts may survive a Mac restart, while possibly dispatched attempts are never replayed. Unknown/app/user pauses remain blocked.

Read roomba_fault_diagnostics for filtered standard errors, native reports, field presence and robot report ages. Unmapped native reports are not automatically motor faults. Classification of native 2102 as a pause/broken-clean state is specific to the exact reviewed firmware; other reports remain unclassified. Missing or stale diagnostics are unknown.

Faults require their physical cause to be resolved. Do not reboot, reset, wipe, clear errors, or start another mission as recovery. Bounded recovery only resumes the same confirmed fault-free mission when enabled. Explicit pause/stop/dock suppress recovery. The narrowly gated dock retry requires an explicit request and human confirmation that the path is clear; remote fault clearance is unverified. The roomba_retry_resume_after_brush_inspection tool requires an explicit owner request and confirmation that the main brush was inspected and turns freely. It permits one normal resume on fresh error 2 only with a complete retained clean-cycle anchor, ready state and adequate battery, once per mission and at most once in six hours. It does not clear errors or disable motor protection. Ended cycles are refused. Cached running state cannot confirm recovery; require the fresh same-mission receipt. Never retry uncertain delivery or use a new clean as a recovery fallback.

Create schedules only when requested. Obtain the owner's IANA timezone and requested days/time. Runs depend on this Mac being awake, online and the observer running. Busy, stale, faulted or changed-map runs are skipped. The notification tool provides a durable cursor; it does not wake an idle agent.

Spot-boundary cleaning and garbage-can travel remain experimental and disabled in the public package. Saved areas and locations are drafts; saving never moves the robot. Do not enable experimental dispatch merely to work around a refusal.

For connection failures, use runtime/roo_cli.py doctor. Read-only diagnostics send no robot commands. Reconnect credentials through the local installer; do not print secrets, tokens, raw cloud responses or home maps in support posts.
