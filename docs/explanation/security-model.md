# Security model

Desktop Automation MCP is a local action gateway, not a general desktop
control plane. Its main security property is that an MCP client cannot act on
an arbitrary window merely because it can name one.

Before an action, the server requires a configured title pattern, an exact
executable-path allowlist entry, and an action-specific permission. It resolves
the live window and revalidates its process identity, visibility, foreground
state, and occlusion immediately before the effect. A stale or ambiguous target
is denied rather than guessed.

Potentially persistent actions—typing text, dragging, and closing—also require
a single-use confirmation token tied to the specific target and action class.
Audit output uses opaque correlation identifiers and rejects screen content,
typed text, window identity, executable paths, and free-form private fields.

Windows UAC, sign-in, and lock-screen UI are not an alternate automation
surface. The server relies on the operating system's desktop isolation and its
normal target checks: a title pattern, exact executable path, live process
identity, foreground, and occlusion. An inaccessible process identity is not
treated as an exception to those checks; target discovery omits it and fails
closed. Disabling the Windows secure desktop for elevation prompts is outside
the supported operating model.

Screenshots are constrained by policy-defined regions and masks. They are held
in memory for the response and are not written to the audit log. This reduces
exposure but does not make a broadly configured local machine safe: the
operator remains responsible for granting only narrow policies.
