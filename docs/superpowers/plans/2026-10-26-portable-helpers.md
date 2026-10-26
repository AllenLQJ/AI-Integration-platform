# Portable helper scripts and publication hygiene

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Preserve existing uncommitted application changes.

**Goal:** Remove personal environment details from publication candidates and make optional diagnostics usable from configured installations.

**Architecture:** Keep runtime configuration outside the source tree. Resolve optional Python and browser commands through explicit arguments, environment variables, or PATH. Prepare any historical metadata cleanup in a separate repository; replacing published history is a separate publication step.

**Tech Stack:** Python 3.10+, PowerShell, Git.

## Constraints and review focus

- Preserve bilingual documentation and Unicode behavior; language alone does not establish a development location.
- Never export credentials, runtime state, or private audit backups.
- Do not contact real executors, authenticate accounts, or restart the shared Hub during tests.
- Test configured executable paths containing spaces, inherited proxies, missing programs, and CLI help without authentication.
- Preserve historical test outcomes. Normalize historical timestamps without changing their instants.

## Tasks

- [x] Back up the working files and Git refs privately before edits.
- [x] Add failing regression tests for portable Muse verification and helper configuration.
- [x] Replace fixed helper installation paths and placeholder proxy overrides; document configuration.
- [x] Remove the remaining personal working-directory reference and normalize timezone fixtures while preserving test semantics.
- [x] Run helper regressions, full Python/frontend regressions, packaging, and publication-file scans.
- [x] Prepare and verify a sanitized copy of published heads, retaining a private commit map and original ref values for lease-protected publication.

## Verification and review

- Final local Python regression: 518 tests, 513 passed and 5 platform-dependent skips, no failures or errors.
- Frontend regression: 19 passed. The publication wheel contains 48 runtime resources matching the candidate source bytes.
- Independent read-only review found two issues: overwritten evidence of an unconfirmed process stop, and accepting Windows npm browser shims that Muse cannot launch. Both were reproduced by failing regressions and corrected before the final full suite. Explicit browser directories also fail clearly instead of falling back to PATH.
- Historical commit parents and timestamp instants are preserved. Publishing rewritten history requires an explicit decision; old pull-request references, cached views, other clones and forks are outside ordinary branch updates.
- No shared Hub restart or live executor task was part of final verification; helper tests use isolated temporary copies and do not authenticate.
