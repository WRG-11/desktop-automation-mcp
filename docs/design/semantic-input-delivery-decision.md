# Semantic input delivery boundary

Status: accepted — 2026-09-15

## Problem

Even when re-verified for target HWND/process identity, foreground, visibility, and focus, `send_text` and `send_key` can only emit the Win32 input event. From that alone it cannot be inferred that the application's correct editor accepted the event and inserted the text or executed the command. A focused local application trial demonstrated this distinction: input sent without first selecting the editor never appeared in the UI.

## Decision

1. Successful `send_text` and `send_key` returns state only **event delivery**; they do not claim acceptance by the target control or any visible behavior change.
2. No universal `verify_text_delivery` / `verify_key_delivery` tool is added today. Such a tool would have to either read the control value via UI Automation (a new authority/dependency surface that the current ADR conditionally postpones), extract image text via OCR (contrary to the OCR boundary decision), or produce fragile pixel-diff output. None of the three is a general solution for canvas/Electron, and all carry sensitive-text leakage risk.
3. A future provider may only be opened under a proven, narrow application+control contract: a separate action permission, named safe regions, masks, byte/pixel/rate/memory/geometry gates, redacted audit, and target-specific negative tests are mandatory. Control values/text are never written to the audit log or the working directory.

## Target classes

| Target | Reliable claim today | Semantic verification status |
|---|---|---|
| Standard native edit control | Win32 event delivered | Closed without a UIA probe + real need |
| Electron/Chromium | Win32 event delivered | No general, privacy-safe acceptance signal |
| Canvas/Ruffle | Win32 keyboard event delivered | No UIA/OCR; coordinate profiles are for targeting only |
| Owned/child control | Win32 event delivered or foreground rejection | No support contract; fail-closed |

## Reopening condition

A new verification authority is designed only once two pieces of evidence exist together: (a) post-delivery acceptance evidence on a real permitted target becomes operationally mandatory, (b) that target's UIA or other narrow provider can emit a stable success/failure signal without carrying the sensitive value. A real probe and a privacy threat analysis are written first; the capability is designed afterwards.
