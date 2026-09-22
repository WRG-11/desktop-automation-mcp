# Changelog

All notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-09-22

### Security

- Policy-file errors that reach the MCP client no longer contain the
  configured path's directory (usually a user profile) or the raw file lines
  a parse error used to quote; they name only the file and point to
  `tools/validate_policy.py`, which keeps full detail for the operator. The
  chained exception that carried the path is dropped as well.
- `preview_action` no longer returns the live confirmation token, its expiry
  or the target identity. A read-only preview had become a second way to read
  an approval token back without `request_confirmation`.
- Documented the human-approval boundary for confirmation tokens and added
  regression coverage for action- and target-confusion attempts.
- Added scheduled CodeQL scanning and Dependabot version updates for Python
  dependencies and GitHub Actions.
- Configured Dependabot to hold MCP major-version updates for an explicit
  compatibility migration, while continuing minor and patch coverage.

### Changed (breaking)

- `preview_action` returns `has_live_token` (boolean) instead of `token_id`,
  `token_expires_at` and `target_identity`. Clients that read a token from the
  preview must call `request_confirmation` instead.
- The example coordinate profile's second region is now `stage-center`
  (was a non-English name); update profiles copied from the example.

### Added

- `policy_visibility_summary` (`observe`): explains an empty `list_windows`
  result with four redacted counts (allowed, title match, wrong executable,
  unreadable process identity) without enumerating any window outside the
  allowed title patterns.
- A source-checkout stdio regression test that completes `initialize`,
  `tools/list` and an action-free `health_check` call over raw JSON-RPC, and a
  test that pins the entry point to the stdio transport.

### Documentation

- Rewrote the README around the security model and a three-step quick start.
- Moved the per-tool reference into `docs/reference/tools.md` and the
  settings, limits and focus-mode detail into the new
  `docs/reference/configuration.md`; design principles now live in the
  security model.
- Added public contribution, support, community, reference, and release-
  verification documentation.
- Corrected the confirmation-token schema and policy examples to describe the
  implemented `text`, `close`, and `drag` contract.
- Clarified the single-maintainer PR model and the public-issue-only support
  channel.

### Development

- Extended the supported Ruff range through 0.16 while explicitly preserving
  the established lint-rule baseline.
- Enforced source-integrity and installed-package stdio handshake checks in
  the Windows quality workflow.
- Added tag-to-version/changelog verification and GitHub build-provenance
  attestations for release distributions; package publication remains manual.
- Added the explicit stdio transport and lifecycle compatibility contract, with
  MCP tool-discovery coverage in the installed-package release gate.

## [0.1.4] - 2026-09-17

First public release.

### Added

- Operational visibility tools, coordinate-profile recording, virtual-screen
  diagnostics, safe drag waypoints, window restoration, and key sequences.

### Changed

- Hardened bounded read, input-hold, policy, screenshot, and audit behaviour.
- Improved target-resolution and input-validation diagnostics.

[0.1.4]: https://github.com/WRG-11/desktop-automation-mcp/releases/tag/v0.1.4
