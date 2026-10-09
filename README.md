# VacuumGPT — connect your Roomba to your agent

Give your agent live Roomba status, maps, named-room cleaning, mission controls and local schedules. Your robot keeps its existing name.

## Install on a Mac

1. [Download VacuumGPT](https://github.com/imzachfarr/VacuumGPT/releases/latest/download/vacuumgpt-v1.0.0.zip) and unzip the bundle.
2. Double-click **Install.command**. If macOS blocks the downloaded script, open Terminal in the unzipped folder and run `bash Install.command`.
3. Sign in to iRobot in Terminal, select your robot, choose command access and select your agent.
4. Restart your agent. Ask **“What is my Roomba doing?”** or **“Clean the kitchen.”**

The installer downloads uv/Python and pinned dependencies, saves credentials in macOS Keychain, starts the observer at login, and connects the skill and MCP server. No administrator password is needed. Account login and setup send no robot movement commands. macOS may ask to allow the Python process to access the new Keychain item.

**Supported:** macOS and iRobot Prime/V4 cloud robots. Combo 105 is the hardware-tested model. Other Prime models are selectable but require owner verification of model-specific behavior. Older Classic/i/j/s-series Roombas, Windows and Linux are not supported by this release. An iRobot cloud account and internet connection are required.

## What you get

- Live status, battery, mission, faults, retained maps and observed travel.
- Requested start, stop, pause, resume, dock and locator chime.
- Named-room and multiroom cleaning, with exact map/region checks and no whole-house fallback.
- Local schedules with your timezone, durable command receipts and interruption history.
- A local dashboard at http://127.0.0.1:8766/.
- Optional bounded recovery of fault-free paused missions started through VacuumGPT.

Vendor room names load automatically. If the map has unnamed rooms, your agent can show the map and save the aliases you choose. Advanced settings, room order and physical cleaning boundaries depend on hardware. Broker acknowledgement alone does not prove a cleaning action or coverage.

Experimental spot-boundary and garbage-can movement are disabled. Area and location saving are non-moving drafts. The software cannot remove physical faults or reconstruct a lost onboard cleaning plan.

## The connector

The included stdio MCP server is the actual connector. Codex, Claude Desktop and Cursor registration are included. For another local MCP client, import the generated configuration at `~/.local/share/vacuumgpt/connector.json` and install `skills/vacuumgpt/SKILL.md` using that client's skill mechanism. If automatic agent registration fails, use this same generated file. Existing unrelated MCP entries are preserved; changed configurations receive a backup.

The bundle also includes `.codex-plugin/plugin.json` and `.mcp.json` for plugin packaging. Choose either plugin-provided MCP wiring or standalone registration; do not enable both copies. Device setup must run once before the plugin server can start. The bundle is a local connector, not a hosted ChatGPT web connector or an approved marketplace listing.

The runtime lives at `~/.local/share/vacuumgpt/runtime`. The download can be deleted after installation. The Mac must stay awake and online for observation, schedules and recovery. Missed, busy, faulted, stale or map-changed scheduled runs are skipped. Notification polling does not wake an idle chat.

## Diagnose or remove

Run these in Terminal:

```sh
~/.local/share/vacuumgpt/runtime/.venv/bin/python ~/.local/share/vacuumgpt/runtime/roo_cli.py doctor
~/.local/share/vacuumgpt/runtime/.venv/bin/python ~/.local/share/vacuumgpt/runtime/roo_cli.py uninstall
```

Add `--delete-credentials` to uninstall to remove VacuumGPT's Keychain login. Removal stops the observer and disables controls/recovery/schedules; telemetry is retained. Remove the `vacuumgpt` entry and `vacuumgpt` skill from your agent settings. Deleting the MCP entry alone does not stop the observer. To revoke controls while retaining observation, set `direct_chat_commands` and `automatic_resume` to false in `runtime/observer-data/control-policy.json` and `writes_enabled` to false in `runtime/activation-result.json`.

If login fails, verify account country and password in the iRobot app, then rerun setup. Avoid repeated failed logins. No public endpoint, router changes or LAN exposure are required.

## Privacy

Your password stays in your Mac's Keychain. Cloud tokens are held in process memory. Maps, telemetry and command history remain in private local files. The agent can read them through MCP; the agent provider's own data handling applies. VacuumGPT makes no telemetry uploads to its developer. iRobot cloud traffic is still required. Do not share runtime data or raw logs in support posts.

## Verify the source bundle

```sh
uv venv --python 3.12 .test-venv
uv pip install --python .test-venv/bin/python -r requirements.lock
PYTHONPATH=runtime:runtime/source-review/prime .test-venv/bin/python -m unittest discover -s tests
```

Tests use mock robots and temporary databases/sockets, and send no physical commands. The release includes the reviewed `roombapy-prime` source and its MIT license. `vendor-source.json` records its upstream commit and source hashes. See `THIRD_PARTY_NOTICES.md`.
