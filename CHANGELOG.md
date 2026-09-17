# Changelog

All notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Documentation

- Added public contribution, support, community, reference, and release-
  verification documentation.
- Corrected the confirmation-token schema and policy examples to describe the
  implemented `text`, `close`, and `drag` contract.
- Clarified the single-maintainer PR model and the public-issue-only support
  channel.

### Security

- Added scheduled CodeQL scanning and Dependabot version updates for Python
  dependencies and GitHub Actions.
- Configured Dependabot to hold MCP major-version updates for an explicit
  compatibility migration, while continuing minor and patch coverage.

### Development

- Extended the supported Ruff range through 0.16 while explicitly preserving
  the established lint-rule baseline.
- Enforced source-integrity and installed-package stdio handshake checks in
  the Windows quality workflow.
- Added tag-to-version/changelog verification and GitHub build-provenance
  attestations for release distributions; package publication remains manual.

## [0.1.4] - 2026-09-17

First public release.

### Added

- Operational visibility tools, coordinate-profile recording, virtual-screen
  diagnostics, safe drag waypoints, window restoration, and key sequences.

### Changed

- Hardened bounded read, input-hold, policy, screenshot, and audit behaviour.
- Improved target-resolution and input-validation diagnostics.

[0.1.4]: https://github.com/WRG-11/desktop-automation-mcp/releases/tag/v0.1.4
