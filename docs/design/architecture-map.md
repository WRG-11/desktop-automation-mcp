# Architecture map — what this project consists of, how the pieces fit together

> Goal: when a new session (or a new person) looks at this repo for the first time,
> reading this single file answers all of "what does each module do, who calls whom,
> through which chain does an action call pass, which document is authoritative".
> When the code changes, this map must be updated too — the module
> docstrings (quoted below) are the primary source; this document is their
> SUMMARY; if the two conflict, the CODE's docstring wins.

## 1. What it does in one sentence

An MCP server: Claude Code (or another MCP client) can **see** permitted Windows
windows (list/status/screenshot) and **interact** with them
(click/key/text/drag/close) — but only for the title+process-path+action triple
explicitly permitted by the operator via environment variable or
policy file, and with the target re-verified IMMEDIATELY BEFORE each effect
as still the same, still in the foreground, still visible.

## 2. Layer diagram

```
                          server.py
                     ── MCP @tool functions ──
                                  │
          ┌───────────────┬──────┴───────┬───────────────┬──────────────┐
          │               │              │                │              │
     _execute_guarded_  confirmation.py  audit.py     rate_limit.py  coordinate_
     action (lifecycle) (confirmation    (redacted     (sliding       profile.py
                         token)           log)          window)       (coordinate
          │                                                           map)
     ┌────┼─────────────┐
     │    │             │
  policy.py         target.py      visibility.py
  (env-based        (hwnd/pid/     (focus/z-order/
   policy decision)  exe identity)  action-time
     │                   │          revalidation)
     │                   │               │
     └──> policy_file.py │               │
          (file-based    │               │
           policy, JSON  │               │
           Schema)       │               │
                          └───────┬───────┘
                                  │
                            input_.py
                      (SendInput/keyboard-mouse
                       raw Win32 calls)
                                  │
                          platform_win32.py
                    (ctypes signatures, constants,
                     DPI init + scoped adapter)

     errors.py  ──  TYPES the exceptions each module raises (horizontal layer)
     memory_budget.py ── bounds process-wide concurrent working memory
                         during image encoding (horizontal layer)
     health.py  ──  answers "is it configured, can it run" without leaking
                    screen/window content to ANY of the above
                    (deliberately does NOT import from server/target/visibility)
```

Call direction is top-down (the upper layer calls the lower one). `platform_win32.py` is the
lowest layer — it defines only Win32 ABI/constant definitions and carries the context-scoped injectable
API adapter; it makes no policy or target decisions.

## 3. Module table (`src/desktop_automation_mcp/`)

| File | Lines | Answers the question | Calls |
|---|---|---|---|
| `platform_win32.py` | 184 | How does this process talk to the Win32 API? (ctypes signatures/constants + scoped adapter + DPI runtime gate) | `errors.py` |
| `errors.py` | 107 | Why did this action fail, in which stable category? | — (only inherits builtin exceptions) |
| `policy.py` | 245 | Which title/path/action/focus-mode/image path is permitted per the environment variables? | `policy_file.py` (if a file exists) |
| `policy_file.py` | 664 | Does the at-most-1-MiB `schema/policy.schema.json` file satisfy the schema + cross-field rules? | `errors.py` |
| `target.py` | 300 | Which live HWND matches the policy, what is its identity (`TargetSnapshot`)? | `policy.py`, `platform_win32.py` adapter |
| `visibility.py` | 150 | Is the target really in the foreground/visible/same process right now? | `target.py` (to read rect), `platform_win32.py` adapter |
| `input_.py` | 159 | How is a single mouse/keyboard/Unicode-text event sent SAFELY? | `visibility.py` (re-verifies before every event), `platform_win32.py` adapter |
| `confirmation.py` | 318 | Single-use confirmation token for guarded actions (`text`/`close`/`drag`) | — (in-memory, independent) |
| `audit.py` | 208 | How is a schema-aligned redacted record kept for this action attempt? | — (in-memory ring buffer) |
| `rate_limit.py` | 83 | Has this application exceeded this minute's budget? | — (independent, sliding window) |
| `memory_budget.py` | 50 | Does concurrent image processing fit the estimated process-wide budget? | `errors.py` |
| `coordinate_profile.py` | 603 | Does the live window match a recorded, reference-zoned coordinate profile? | — (pure comparison, makes no Win32 calls) |
| `health.py` | 205 | Is the server configured and operable (WITHOUT reading screen content)? | `policy.py`, package metadata, a single DPI query |
| `server.py` | 1310 | All MCP `@tool` functions + the full `_execute_guarded_action` lifecycle | all of the above |

## 4. A typical action flow: a `click_window` call step by step

1. `click_window` in `server.py` → `_execute_guarded_action(ACTION_CLICK, ..., effect)`.
2. `_prepare_action_target` inside the shared executor, in order:
   **rate_limit** budget check →
   hwnd resolution with **target.py** (which internally asks **policy.py** — which in turn asks
   **policy_file.py** if a file exists) → focus/z-order verification with **visibility.py** (performance budget measured with `_check_budget`) →
   prepares the target; does not yet write the success audit entry.
3. If verification passes, `server.py` calls **input_.py**; just BEFORE sending the mouse
   event it calls `visibility._verify_action_target` AGAIN (to close the TOCTOU window — the target may have
   closed/changed between steps 2 and 3).
4. The `effect` function of guarded actions such as `close_window`/`send_text`/`drag_window` calls `confirmation.consume_confirmation` before the raw effect;
   without a token, with a wrong token, or with an expired token, no effect occurs.
5. The shared executor writes the final result to **audit.py** exactly once and uses the same correlation ID as the tool
   response. On error, the typed exception in `errors.py` is re-raised unchanged; builtin compatibility and the stable
   `.code` are preserved together.

`screenshot_window` and `check_coordinate_profile` pass through the same 1–3 chain
but use the shared image plan instead of input_.py: named region →
virtual-screen bounds (must fit fully, no clamping) → mask → pixel/rate/memory
reservation → geometry/identity verification →
`ImageGrab.grab()` → geometry/identity verification → PNG byte limit.
It processes the image only in memory WITHOUT WRITING it to disk/audit; only a
boolean/numeric privacy summary goes to the audit record.

## 5. Repo-wide file map

| Path | What it is for |
|---|---|
| `src/desktop_automation_mcp/` | Production package — the table above. |
| `server.py` (at root) | Backward-compatibility wrapper; the real code is under `src/`. |
| `tests/` | Regression tests. `fake_platform.py` + `test_chaos.py` use a fake platform instead of real Win32; `test_platform_win32.py` verifies scoped adapter injection and the negative-origin physical-pixel matrix. |
| `schema/*.schema.json` | JSON Schema (draft 2020-12) — policy, confirmation_token, audit_event, coordinate_profile. `schema/README.md` lists each one's design decisions (profile_id format, verification-point format, etc.) with rationale. |
| `schema/examples/` | A real, validator-passing example file for each schema. |
| `tools/validate_*.py` | Standalone CLI validators (0=OK/1=invalid/2=probe-error); NOT part of the package runtime, only developer tools. |
| `docs/design/` | Architecture decisions: this document, the UIA postponement ADR, the OCR boundary decision, the version matrix, integrity/rollback, and the transient-UI decision. |
| `docs/guides/` | Operator guides: `security-guide.md` (narrow permission examples), `migration-env-to-policy-file.md` (env→file migration guide). |
| `docs/releases/` | Redacted release-verification summaries. |
| `CHANGELOG.md` | User-visible release notes in Keep a Changelog format. |
| `INTEGRITY.sha256` | Output of `tools/compute_integrity_hash.py write` — SHA-256 over `src/**/*.py`. |

## 6. How a new session continues from here

1. **First `README.md` and `docs/README.md`** — setup, supported behaviour,
   and the right guide for the task.
2. **Then the `[Unreleased]` section of `CHANGELOG.md`** — user-visible work
   queued for the next release.
3. **This document (`architecture-map.md`)** — come back here when you forget which module answers which question; each module's own docstring
   (the first ~15 lines of the file) is more detailed than this document.
4. **`README.md`** — how the operator configures the MCP registration, what each tool's signature is, the security defaults table.
5. When adding a new module: (a) write in the docstring "which question it answers, whom it calls, whom it does not call", (b) add a row to this table, and (c) update the relevant reference and release notes.
