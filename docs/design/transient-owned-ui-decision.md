# Decision: target-owned transient UI layers

## Decision

The default target-identity rule remains strict: an action is permitted only
when the target resolved by the server matches the root window observed at
the intended interaction point. The implementation does not infer that an
owned, child, or transient window is equivalent to a permitted target.

## Rationale

Relaxing the rule to accept a "probably related" window would make policy
scope harder to explain and could turn an ownership-chain heuristic into an
authority expansion. A denial in this ambiguous state is the intended safe
result: the server must not send input when target identity cannot be
verified.

## Reconsideration criteria

Support for an application whose controls use transient or owned windows
requires a separate threat-model review, including:

- a bounded and documented ownership-chain definition;
- policy fields that identify the additional window surface explicitly; and
- real Windows validation covering both allowed and adversarial cases.

Until those criteria are met, the default-deny behavior is preserved.
