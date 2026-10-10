# Changelog

## Unreleased

- Preserve complete cleaning intent and exact native anchors for bounded same-mission recovery.
- Add durable recovery claims, restart reconciliation and owner-command hold generations; never replay uncertain dispatch.
- Recognize retained STOP+CLEAN pauses only with verified native status and origin evidence.
- Add filtered read-only fault diagnostics, recovery status and an explicitly inspected, bounded normal brush-resume tool.
- Reject ended or changed brush missions and require fresh post-command state/version/timestamps and command echo for a successful receipt.
- Keep robot identity, credentials, URLs, room aliases and control grants installation-specific; experimental movement remains disabled by default.
- Keep map selection stable after owner choice and hide old-mission position markers. Update blocked spot feedback and remove the empty-dustbox UI flow coherently.
- Use the browser timezone for dashboard dates and new schedule defaults; MCP schedule creation requires an explicit IANA timezone.
- Expand offline recovery and source-bundle checks, including isolated MCP refusal tests.

## 1.0.0

Initial guided macOS installer, Prime robot selection, skill and MCP connector, local dashboard, room controls and schedules.
