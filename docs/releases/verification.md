# Release verification

Each release candidate must pass the Windows quality workflow, build a wheel,
install that wheel into a clean environment, import the installed package, and
complete a stdio MCP handshake. The source integrity manifest is checked before
release.

Live desktop verification is performed under a narrow, application-specific
policy. Raw screenshots, local paths, window identifiers, policy files, and
operator transcripts are intentionally not published. The release notes record
user-visible changes and security fixes without exposing that evidence.
