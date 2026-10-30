# Orchestrator skill discovery and synchronization

## Agreed scope

Use `skills/agent-hub-orchestrator/SKILL.md` as the sole editable source.
Synchronize full copies to `.agents`, `.claude`, and the Python package.
The user approved this project-level approach; user-level installation and
remote distribution are outside this change.

## Implementation and verification

- Add a dependency-free `scripts/sync_skills.py` with a read-only `--check` mode.
- Resolve paths from the script, tolerate checkout newline differences, and report missing/stale copies.
- Add `.agents` discovery, explicit wheel package data, and sdist inclusions.
- Run the check in the existing three-platform CI and verify packaged skill content.
- Document the single-edit workflow and discovery/MCP boundaries in both READMEs.
- Verify missing/stale detection, read-only checks, repeat runs, external working directories,
  newline handling, and missing-source failure in a disposable fixture.
- Build wheel and sdist; inspect their skill contents and run `git diff --check`.

No Hub runtime behavior or executor settings change. No new dependencies are needed.

## Verification results

- Initial `--check` returned 1 for the missing `.agents` mirror; synchronization
  created it and the subsequent check returned 0.
- Disposable smoke fixture passed: missing/stale detection, read-only checks,
  synchronization, idempotence, external cwd, UTF-8, CRLF, and missing-source failure.
- Wheel and sdist builds succeeded. Archive inspection confirmed matching skill
  content in the wheel and all four skill locations plus the script in the sdist.
- `git diff --check` passed. Independent read-only review found no blocking issues.
- Remote CI and real client discovery were not run. Runtime regression tests were
  not rerun because this change touches distribution, maintenance, and documentation only.
