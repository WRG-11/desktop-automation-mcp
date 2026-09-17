# Confirmation contract

Confirmation is an additional gate for consequential actions. It is not an
authorization grant and it is not evidence that a human has approved an
action. Policy permission, target resolution, foreground/visibility checks,
and the confirmation gate must all succeed before the server performs the
effect.

## Client workflow

Use this sequence for a protected action:

1. Resolve and present the target and intended action to the human operator.
2. Obtain the operator's explicit approval **outside the MCP tool call**.
3. In a later client turn, call `request_confirmation` for that exact action
   and target.
4. Immediately use the returned `token_id` only with the matching effect
   tool; discard it after a denial, expiry, restart, or completed effect.

An MCP client or model must not request and consume a token automatically in
the same unattended turn. The server deliberately does not infer a human
decision from a tool call: it has no authenticated operator-approval channel.
Deployments that need machine-enforced approval must add such a channel before
using protected actions.

## What the server enforces

Each in-memory token is short-lived (60 seconds by default, 300 seconds
maximum), single-use, and bound to all of:

- the action class;
- `hwnd`, process ID, and process start time of the resolved target; and
- the running server process.

The token is consumed before the native effect. It cannot be reused, applied
to a different action class, or applied to another target identity. A process
restart loses all tokens and fails closed. The server re-verifies the target
immediately before the effect, so an expired or recycled window handle is not
treated as continuing authority.

These controls limit token confusion and replay, but they do not replace the
operator-approval boundary described above. Do not log, persist, or relay a
`token_id` beyond the client session that received it.
