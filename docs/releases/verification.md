# Release verification

Each release candidate must be merged through the protected `master` branch
and pass the Windows quality workflow. That workflow enforces source linting,
formatting, unit tests, source-integrity verification, wheel build, clean
wheel installation/import outside the checkout, and a stdio MCP handshake
against the installed console entry point.

After those checks pass, create a version tag matching `pyproject.toml` and a
GitHub Release whose notes match `CHANGELOG.md`. Do not tag a branch or a
failed/incomplete PR.

Live desktop verification is performed under a narrow, application-specific
policy. Raw screenshots, local paths, window identifiers, policy files, and
operator transcripts are intentionally not published. The release notes record
user-visible changes and security fixes without exposing that evidence.
