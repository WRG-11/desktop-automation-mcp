# Tool reference

The server exposes tool families rather than unrestricted desktop control.
Every target-taking tool requires an allowed title and full executable path;
effects also revalidate identity, visibility, focus, and occlusion immediately
before execution.

| Family | Tools | Permission |
|---|---|---|
| Diagnostics | `health_check`, `get_rate_limit_state`, `get_virtual_screen_bounds` | none / observe as documented |
| Discovery | `list_windows`, `get_window_state`, `wait_for_window`, `wait_for_title_change` | `observe` |
| Profiles and image | coordinate-profile helpers, `screenshot_window` | `observe`; policy-controlled regions |
| Pointer input | click, double-click, right-click, hover, scroll | `click` or `hover` |
| Keyboard input | `send_key`, `send_key_sequence` | `key` |
| Protected effects | `send_text`, `drag_window`, `close_window` | action permission plus confirmation token |

The complete parameter reference lives in the repository [README](../../README.md).
New tools never inherit permission from an existing tool.

Pointer tools move the shared operating-system cursor. Treat `hover`, click,
scroll, and drag as visible operator-disrupting effects even when they do not
change window content. UAC, sign-in, and lock-screen UI are outside the
supported target model: Windows desktop isolation plus the live target checks
are the boundary; the server never bypasses them.
