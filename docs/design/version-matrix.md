# Compatibility and test coverage

The supported runtime is Windows with Python 3.12 or newer. Package metadata
currently constrains the MCP Python SDK to the 1.x line (`>=1.29,<2`).

## Continuous integration coverage

The public quality workflow runs on the current GitHub-hosted Windows runner
for the following Python versions:

| Python | Unit tests | Lint/format | Wheel build and installed-package import |
|---|---:|---:|---:|
| 3.12 | Yes | Yes | Yes |
| 3.13 | Yes | Yes | Yes |
| 3.14 | Yes | Yes | Yes |

The installed-package check intentionally runs outside the repository after
building a wheel. It catches packaging omissions that a source-tree import
cannot detect.

## Boundaries

- CI validates the library and its mocked Win32 boundaries; it does not grant
  a policy or send input to a real application.
- Other Windows releases and MCP SDK major versions have not been declared
  supported. A successful local experiment is useful evidence, but does not
  expand this compatibility statement until it is automated or released.
- Use `health_check` to inspect the versions observed by a running server.
