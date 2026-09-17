# Release verification

Each release candidate must be merged through the protected `master` branch
and pass the Windows quality workflow. That workflow enforces source linting,
formatting, unit tests, source-integrity verification, wheel build, clean
wheel installation/import outside the checkout, and a stdio MCP handshake
against the installed console entry point.

After those checks pass, create a version tag matching `pyproject.toml` and a
GitHub Release whose notes match `CHANGELOG.md`. Do not tag a branch or a
failed/incomplete PR.

Pushing a matching `vX.Y.Z` tag runs the release-provenance workflow. It
builds the source and wheel distributions, rejects a mismatch between the tag,
`pyproject.toml`, and `CHANGELOG.md`, and creates a GitHub build-provenance
attestation for the distributions. Consumers can verify a downloaded artifact
with `gh attestation verify <artifact> -R WRG-11/desktop-automation-mcp`.

A tag does **not** publish to PyPI or mutate the GitHub Release. Publishing is
intentionally a separate future step: first configure a PyPI Trusted Publisher
for this repository and protect its `pypi` environment, then add an OIDC-only
publish job. Do not add a long-lived PyPI token to repository secrets.

Live desktop verification is performed under a narrow, application-specific
policy. Raw screenshots, local paths, window identifiers, policy files, and
operator transcripts are intentionally not published. The release notes record
user-visible changes and security fixes without exposing that evidence.
