# Security Policy

## Reporting a vulnerability

Email **winstonrgsocial@gmail.com** with the subject line
`SECURITY: desktop-automation-mcp`.

(GitHub Private Vulnerability Reporting was evaluated as the primary channel
but confirmed unavailable for this repository — `Security tab → Report a
vulnerability` returns a 404 as of 2026-09-17 — so email is the supported
channel until that changes.)

Do not report security issues through public GitHub issues, discussions, or
pull requests.

## What to include

- A minimal reproduction (target application, policy configuration used,
  the specific MCP tool call and arguments).
- The observed vs. expected behavior, and why it constitutes a security
  boundary violation (not just a bug).
- Whether the issue requires a non-default configuration (e.g. `activate`
  focus mode, `DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS=true`) to
  reproduce — that changes severity, not validity.

## Scope

In scope: anything that lets a caller bypass the policy model described in
the README (title+process allowlist, action-level permission, foreground/
occlusion re-verification, confirmation tokens for protected actions,
redaction of the audit trail), or that leaks screen content/input beyond
what the documented tool contract promises.

Out of scope: the operator's own choice to grant broad permissions (e.g. a
wide `title_patterns` glob), attacks that require local code execution on
the same machine already (this tool is not a sandbox against a already-
compromised host — see the README's "Known limitations" / threat-model notes),
and denial-of-service via resource exhaustion that the existing rate/memory
budgets are explicitly documented as not eliminating, only bounding.

## Supported versions

| Version | Supported |
|---|---|
| Latest `0.1.x` release | Yes |
| Older `0.1.x` releases | No — please upgrade before reporting |

This project has not yet reached `1.0`; there is no long-term-support
branch. Fixes land on the latest release only.

## Disclosure timeline

- Acknowledgement target: within 5 business days of the report.
- A coordinated disclosure window of **90 days** from acknowledgement is the
  default, matching common industry practice (e.g. Google Project Zero).
  This can be shortened by mutual agreement (e.g. if a fix ships quickly) or
  extended if the fix requires a breaking change and more testing time.
- Credit is given in the fix's changelog entry and, where applicable, the
  GitHub Security Advisory, unless the reporter asks to remain anonymous.
