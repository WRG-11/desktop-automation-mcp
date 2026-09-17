# ADR-0001: semantic UI Automation provider

- **Status:** Accepted — deferred
- **Decision:** Do not add a UI Automation provider at this time.

## Context

The server can safely operate from policy-bound coordinates, named safe
regions, and verification points. UI Automation could improve selectors for
some applications, but it also adds dependency, testability, and content-
exposure considerations. It must not bypass the existing target-identity,
policy, confirmation, or audit boundaries.

## Decision

No `pywinauto`, `uiautomation`, `comtypes`, or custom COM implementation is
currently a runtime dependency. Coordinate profiles remain the supported
semantic-targeting path for applications that need them.

## Consequences

- The package keeps its small dependency surface and deterministic unit-test
  boundary.
- A coordinate-profile mismatch produces a denial rather than silently
  adapting to a changed application UI.
- A future UI Automation contribution must be narrow: selector-to-rectangle
  resolution only, followed by the same guarded action path as coordinates.

## Reconsideration criteria

Reopen this decision only with a concrete supported application that cannot
be served by a verified coordinate profile, plus an evidence-backed review
of provider maintenance, licence, privacy impact, testability, and real
Windows behavior. Any resulting capability must be separately permitted by
policy and limited to the already-authorized target surface.
