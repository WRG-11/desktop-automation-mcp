# OCR boundary decision

Status: accepted — 2026-09-15

## Decision

OCR will not be implemented today. On the current targets, versioned coordinate profiles, named safe regions, and masked region hashes are sufficient; no verified use case requiring OCR was found. Speculative OCR would turn screen content into a new text surface, increasing privacy and wrong-target risk.

If a real need emerges, OCR may only be added in a separate design round:

- off by default with a separate `ocr` action permission in the policy file;
- operating only on named `safe_regions`;
- the same mask, pixel/byte/rate/memory, and geometry-TOCTOU gates as screenshots;
- no raw image, full window title, or region name in the result;
- no OCR text in the durable audit log, only redacted measurements and outcome;
- live Windows privacy smoke test and wrong-region rejection evidence.

Without these triggers, `ocr`, in-image text search, and OCR-based selectors
remain out of scope.
