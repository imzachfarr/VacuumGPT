---
name: vacuumgpt
description: Connect and operate a Roomba through the VacuumGPT MCP connector, inspect status and maps, clean named rooms, manage local schedules, and diagnose interrupted missions.
---

Use the VacuumGPT MCP connector for the owner's selected robot. Setup selects an exact Prime/V4 device; never assume a model, room ID, map, timezone, or previous owner's grant.

If tools are missing, run the bundle's Install.command in the owner's local macOS Terminal. The owner enters their iRobot login there, selects their robot and agent, and grants requested command access. Never ask for account passwords in chat. Restart the agent after installation. A skill alone does not provide robot access.

Read roomba_status before acting. Check freshness, identity, mission, faults and control_policy. When direct_chat_commands is true, execute explicit owner requests through roomba_command, roomba_clean_room or roomba_clean_rooms; the persistent grant removes the need for another Terminal approval. Keep one stable unique request_id for each human intent. Reuse that ID only for the same intent. Inspect command results/history after uncertain delivery; never resend using a new ID.

Read roomba_rooms to find map-scoped names and region IDs. Resolve the requested name against the active map. If unnamed, show the map to the owner and use roomba_name_room for their chosen alias. It saves locally and sends no robot command. Never guess which room an ID represents. A named-room request cannot become whole-house cleaning. start means whole-house and requires that intent. New cleaning requires idle state; do not silently replace an active mission.

Report broker acknowledgement separately from confirmed robot behavior. Travel is not cleaning coverage. Hardware adherence to room order, settings and drawn boundaries cannot be inferred from a running-state transition.

Faults require their physical cause to be resolved. Do not reboot, reset, wipe, clear errors, or start another mission as recovery. Bounded recovery only resumes the same confirmed fault-free mission when enabled. Explicit pause/stop/dock suppress recovery. The narrowly gated dock retry requires an explicit request and human confirmation that the path is clear; remote fault clearance is unverified.

Create schedules only when requested. Obtain the owner's IANA timezone and requested days/time. Runs depend on this Mac being awake, online and the observer running. Busy, stale, faulted or changed-map runs are skipped. The notification tool provides a durable cursor; it does not wake an idle agent.

Spot-boundary cleaning and garbage-can travel remain experimental and disabled in the public package. Saved areas and locations are drafts; saving never moves the robot. Do not enable experimental dispatch merely to work around a refusal.

For connection failures, use runtime/roo_cli.py doctor. Read-only diagnostics send no robot commands. Reconnect credentials through the local installer; do not print secrets, tokens, raw cloud responses or home maps in support posts.
