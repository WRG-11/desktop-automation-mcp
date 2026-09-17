# Contributing

Thank you for improving Desktop Automation MCP. This project deliberately
exposes a narrow Windows automation surface: every change must preserve its
deny-by-default policy model and avoid broadening an agent's authority by
accident.

## Before opening an issue

- Report security vulnerabilities privately through [SECURITY.md](SECURITY.md),
  not through a public issue.
- For bugs, include the released version, Windows and Python versions, the MCP
  client, expected and actual behaviour, and a **redacted** policy example.
  Never attach screenshots, window titles, executable paths, audit logs, or
  confirmation tokens that identify a person or machine.

## Development

This project targets Windows and Python 3.12 or later.

```powershell
py -3.12 -m pip install -e ".[dev]"
py -3.12 -m ruff check src tests
py -3.12 -m ruff format --check src tests
py -3.12 -m unittest discover -s tests -v
py -3.12 tools/compute_integrity_hash.py check
```

Run the relevant schema validators whenever a schema or example changes.
The GitHub quality workflow is authoritative for the supported CI baseline.

## Pull requests

Use a focused branch and open a pull request before merging. Keep unrelated
formatting and generated files out of a functional change. Changes that add or
alter an MCP action must include tests for the denied path as well as the
allowed path.

Every PR must state its policy, confirmation, audit, privacy, and documentation
impact. Do not commit local policy files, real target paths, raw screenshots,
audit events, dogfooding transcripts, credentials, or other machine-specific
evidence.

## Design constraints

- A new tool receives no permission implicitly; policy authorization remains
  action-specific and default-deny.
- Effects must validate the live target immediately before execution.
- Protected effects keep their short-lived, target-bound confirmation flow.
- Public responses and audit output must not disclose screen content, typed
  text, window identity, or executable paths beyond the documented contract.
- Preserve compatibility for stable error codes, schemas, and documented MCP
  tool arguments, or call out a breaking change clearly.
