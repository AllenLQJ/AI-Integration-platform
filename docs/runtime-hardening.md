# Runtime configuration and executor loading

These settings are read from the Hub process environment. Restart the Hub when
you intentionally apply new runtime settings; installing the code alone does
not change an already running daemon.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_HUB_SQLITE_BUSY_TIMEOUT_MS` | `5000` | SQLite busy timeout in milliseconds, clamped to zero or greater. |
| `AGENT_HUB_SESSION_LIMIT` | `1024` | Workbench session limit, at least one. |
| `AGENT_HUB_WORKBENCH_PAGE` | `200` | Workbench event page size, clamped to 1–200 (the event API limit). |
| `AGENT_HUB_MAX_HTTP_THREADS` | `64` | Maximum active HTTP workers, at least eight. At capacity, new connections are closed; clients may retry. |
| `AGENT_HUB_ALLOWED_PLUGINS` | unset | Optional comma-separated exact executor entry-point names. `*` permits any explicitly configured plugin. |

Numeric settings must be integers. An unset or whitespace-only plugin allowlist
preserves existing behavior: only plugins explicitly selected in `config.json`
are loaded. Installing a distribution does not enable it. For example,
`AGENT_HUB_ALLOWED_PLUGINS=echo,company-executor` allows those two entry-point
names, independent of the configured agent instance names. The Python
`load_plugin(..., allowed_plugins=...)` argument overrides the environment; an
empty collection denies all plugins. A nonempty comma-only environment value
also denies all plugins.

Plugin state paths are resolved before loading, including existing symlinks and
Windows junctions, and must stay under the Hub root. Plugins still execute with
the Hub user's privileges; this validation is not an operating-system sandbox.

`Store.connection(write=True)` serializes internal writers on one Store instance.
Read transactions retain WAL concurrency. SQLite's busy timeout still handles
other processes or other Store instances; it does not promise writes can never
time out. Long polls wake after committed changes and check at most once per
second while idle to observe writes from another process.

Windows executors keep suspended creation and assignment to a kill-on-close Job
Object before being resumed. `bind_process_to_job(pid)` is a best-effort helper
for existing children; callers must retain the returned handle and close it when
the tree should stop. It returns `None` when binding is unavailable. POSIX
executors start a new session and avoid signaling the daemon's own process group.

Install developer dependencies with `python -m pip install -e ".[dev]"` and run
`python -m pytest tests`. The orchestrator skill is included in source at
`skills/agent-hub-orchestrator/SKILL.md`, mirrored under `.agents` and `.claude`, and
packaged at `agent_hub/skills/agent-hub-orchestrator/SKILL.md` inside the wheel.
Edit only the source skill and run `python scripts/sync_skills.py`; CI runs
`python scripts/sync_skills.py --check` and checks the skill content in the wheel.

## Verification on Windows (2026-10-30)

- Full pytest: **583 passed, 6 skipped**, no failures. Skips cover POSIX process/SSH tests and browser tests requiring Unix file descriptors.
- All 42 source Python modules compile; `pip check` reports no broken requirements.
- Wheel build succeeds and includes the skill with matching content and dependency metadata.
- Independent read-only code review found no actionable new issues.
- Implementation decisions and the test ledger are in [the implementation plan](superpowers/plans/2026-10-30-runtime-hardening.md).
