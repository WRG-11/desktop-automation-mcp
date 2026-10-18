# Changelog

All notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The server now runs on the MCP Python SDK 2.x (`mcp>=2.3,<3`, verified with
  2.3.0); MCP 1.x is no longer supported. `FastMCP` became `MCPServer`.
- MCP 2.x forwards only `ToolError` text to the client and reports any other
  exception as `Error executing tool <name>`. Every tool now re-raises the
  error categories in `errors.py` (`errors.CATEGORY_ERRORS`) as `ToolError`,
  so denial and input messages still reach the client exactly as before; any
  other exception, which MCP 1.x also forwarded verbatim, now stays in the
  server log. Measured over stdio with no policy configured, the same 47
  `tools/call` requests against MCP 1.30.0 (before this change) and 2.3.0
  (after it): 46 answer identically; the other is `health_check`'s dependency
  detail text. `initialize` no longer lists an empty `experimental`
  capability, and `screenshot_window` no longer advertises an
  `outputSchema` (MCP 1.x generated one for the image result).
- Client-reachable checks that raised a bare builtin now raise the matching
  category, which subclasses the same builtin: confirmation argument checks
  and a vanished `hwnd` raise `InputRejectedError` (`ValueError`); target
  identity checks and invalid coordinate profiles raise `PolicyDeniedError`
  (`PermissionError`). A missing or unreadable coordinate-profile file named
  by the caller is reported as `InputRejectedError`.
- `serverInfo.version` is the package version. MCP 1.x reported the SDK's own
  version there, and MCP 2.x would report an empty string.
- `health_check` checks the new bounds (`mcp` floor 2.3, ceiling below 3).

### Documentation

- The README opens with a Notepad policy and the audit event of one refused
  action, both taken from `schema/examples/`, before the design rationale.
- `SECURITY.md` names GitHub private vulnerability reporting as the
  reporting channel, now that it is enabled for this repository (it said
  the channel was unavailable), with email as the fallback. Supported
  versions now name the `0.2.x` line instead of `0.1.x`.
- The README has a "Usage patterns" section. It covers short, targeted
  input instead of bulk text, automating a background window, the named
  policy region a screenshot needs, and why minimized or covered windows
  receive no input.
- The module table in `docs/design/architecture-map.md` no longer lists line
  counts. They were written by hand and had drifted from the source; the
  table keeps what each module answers and what it calls.
- `docs/design/version-matrix.md` states the dependency bounds that
  `pyproject.toml` declares: MCP `>=1.29.1,<2` (it said `>=1.29,<2`) and
  Pillow `>=12.3`.

### Development

- Pinned every GitHub Actions reference in the quality, CodeQL and
  release-provenance workflows to a full commit SHA, with the release tag as
  a trailing comment. A re-pointed tag can no longer change what runs in CI;
  Dependabot updates the SHA and the comment together.
- The quality workflow now measures branch coverage with coverage.py and
  fails below 86% (86.92% measured on Python 3.12, 3.13 and 3.14).
  `coverage` joined the `dev` extra; the floor lives in `pyproject.toml`.
- `.project-layout.json` lists `LICENSE`, `NOTICE` and `SECURITY.md`, drops a
  directory that was never tracked, and orders both lists
  case-insensitively, as the layout check expects.
- Two comments named a roadmap phase with a non-English word. Both now say
  "Phase". `INTEGRITY.sha256` is regenerated because one of them is in
  `src/`; no code changed.
- The root `server.py` compatibility wrapper is now linted and format-checked
  in CI. Its import follows a `sys.path` insertion on purpose, so the E402
  finding is suppressed on that line with a comment; a blank line was added
  after the docstring for `ruff format`. Behaviour is unchanged.
- `tools/verify_stdio_handshake.py` and one test read the SDK's renamed
  attributes (`server_info`, `mime_type`); the JSON on the wire keeps its
  camelCase names.
- New tests cover the error-forwarding rule (each category forwarded with its
  text, any other exception kept generic, in-process callers keep the original
  type, tool schemas unchanged) and a stdio test asserts that a denied call's
  message arrives over the wire.

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
