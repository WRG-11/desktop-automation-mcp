# Security guide

This guide explains the day-to-day application of default deny, least
privilege, confirmation for consequential actions, and local privacy.

## Narrow permission examples

Rule: write only the action required for that job; there is no "we may
need it later" permission.

### (a) Window observation only — screenshots and input forbidden

```json
"DESKTOP_AUTOMATION_ALLOWED_TITLES": "*Ruffle*",
"DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS": "C:\\Program Files\\ruffle\\bin\\ruffle.exe",
"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"
```

With this legacy-env entry, the client can list the window and read its
state, but screenshots are rejected fail-closed by default; it cannot move
the mouse, press keys, type text, or close the window. If an image is
needed, the preferred path is a policy file with named `safe_regions`,
masks, and a PNG-byte limit, such as `schema/examples/ruffle-policy.yaml`.
In legacy env mode, full-window cropping is enabled only via the explicit
opt-in `DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS=true` for
backward compatibility.

### (b) Observation + text — Notepad scenario

For a Notepad window where text corrections will be made, only content
actions are added next to `observe`; `click` and `key` are not granted. The
policy-file-format counterpart stands as a working example in
`schema/examples/notepad-text-policy.yaml`: `allowed_actions` contains only
`observe, text, close`, and `text` and `close` are inside
`protected_actions` there, meaning that even though permitted, they
additionally require a short-lived, single-use confirmation token.
Lesson: a content-changing action is not a natural extension of the
observation permission; it is granted on a separate line, with separate
consideration.

For the required human-approval boundary and exact client sequence, see the
[confirmation contract](../reference/confirmation.md). A confirmation token
is deliberately not a substitute for authenticated human approval.

### (c) Full interaction but `close`/`text` protected — Ruffle scenario

Gameplay needs clicks and keyboard, but needs neither closing the window
nor injecting text from outside:

```yaml
application:
  executable_path: "C:\\Program Files\\ruffle\\bin\\ruffle.exe"
  title_patterns:
    - "*Ruffle*"
  allowed_actions:
    - observe
    - hover
    - click
    - key
```

This is identical to the working Ruffle entry in the README: `text` and
`close` are NOT in the list, so they are denied as required by the
default-deny policy. Even in an interactive setup, close/type permission is
granted only through a separate decision, tied to the protected class (the
`protected_actions` + approval-token design); no "it can click, so let it
close too" inference is made.

## Risky globs

A title pattern matches the window's current LABEL, not the process
identity. The label carries user content; therefore a broad pattern means
broad data.

- **Bad:** `DESKTOP_AUTOMATION_ALLOWED_TITLES=*` — covers EVERY window on
  the machine: password manager, bank tab, private chat included. A single
  wildcard renders the entire policy file meaningless.
  **Narrow alternative:** write each application's own exact pattern
  (`*Ruffle*`, `*Notepad*`); a general wildcard never enters the allow
  list.
- **Bad:** an over-broad pattern such as `*Chrome*` — covers not one site
  but ALL of the user's Chrome tabs/windows, because the title usually also
  carries the page title (both "My Bank Account - Google Chrome" and "Cat
  Video - Google Chrome" match).
  **Narrow alternative:** do not allowlist the browser wholesale. If a web
  task requires automation, narrow it with a dedicated single-purpose
  profile/window for that task plus the distinguishing title fragment of
  that window; keep the general daily browser out of scope.
- **Bad:** applying the `*Notepad*` pattern to Notepad++ or another editor
  on "they are similar anyway" grounds — different exe, different process,
  different trust assumption. The title + full exe path dependency (the
  "Title + process dependency" line in the README) already mandates this:
  even if the pattern is narrow, the EXE PATH is also written in full, and
  there is no access unless both match together.

## Forbidden applications

The following uses are deliberately out of scope; below is a one-sentence
rationale per item:

- **Controlling hidden or background-invisible windows** — rationale: the
  visibility gate is the core of this project; a tool that controls what is
  not visible removes the operator's chance to see what is being permitted.
- **Automatically bypassing the login screen, the elevated UAC desktop, or
  the lock screen** — rationale: these screens draw the operating system's
  privilege boundary; automation crossing them means piercing the privilege
  defense.
- **General-purpose automation for password managers, MFA codes, payments,
  financial transactions, or social media posting** — rationale: these are
  irreversible actions or ones directly convertible to identity theft; a
  look-and-click architecture from a single-user window lacks the audit
  depth to carry that risk.
- **Uncontrolled full-screen OCR scanning** — rationale: full-screen
  scanning effectively removes the permitted-window boundary and turns every
  sensitive on-screen content (notification, tab, document) into data.
- **Circumventing an application's anti-cheat, permission, or security
  mechanism** — rationale: automation that bypasses another program's
  security decision stops being a tool and becomes an attack apparatus.

## Audit retention and incident reporting

### What is stored, what is not stored

Per the `schema/audit_event.schema.json` design, the record kept for each
action is: `timestamp`, `correlation_id`, `policy_id`, `action_type`,
`outcome`, (`denied`/`error` only) `denial_reason`, `duration_ms`, and the
optional `target_identity` (`hwnd`/`pid`/`process_started_at`).
DELIBERATELY not stored: window image, typed/visible text, and window
title. Because the schema uses root-level `additionalProperties: false`, if
someone later accidentally adds a sensitive field such as
`screenshot_bytes`, the record trips the schema (fail-closed leak
prevention). A sample denial record
(`schema/examples/audit_event-denied-text.json`) passes the validator; the
following command was actually run on this machine and its output verified:

```powershell
py -3.12 tools/validate_audit_event.py schema/examples/audit_event-denied-text.json
```

Actual output:

```text
OK: schema/examples/audit_event-denied-text.json
```

### Retention decision

The audit default is IN-MEMORY: records are not written to persistent
storage (file, log directory) on their own.
Persistent audit is written to an access-restricted directory only when
explicitly enabled. This is not a deficiency but a design decision — it is
required by the local-privacy principle: images, logs, and error output are
not written to persistent storage by default.
Turning on persistent retention means separately deciding where, for how
long, and with whose access the records are written; until that decision is
made, the log directory is not filled with a "we will look at it as it
accumulates" mindset.

### Incident reporting

For private vulnerability reporting, follow [SECURITY.md](../../SECURITY.md).
If an immediate safety concern appears (wrong-window action, unexpected
permission expansion, suspicious denial pattern), first stop or narrow the
relevant policy and restart the MCP session before collecting a minimal
report.
