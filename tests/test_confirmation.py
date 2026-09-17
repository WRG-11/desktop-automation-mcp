"""Confirmation token flow tests (ROADMAP §9: issue/consume/preview).

Scope: `src/desktop_automation_mcp/confirmation.py`. Each test uses its
own `ConfirmationStore` instance (no state leakage, order-independent).
Wiring it into `server.py` is a separate concern; only the module's
behavior is proven here.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import confirmation, errors
from desktop_automation_mcp.confirmation import ConfirmationStore

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "validate_confirmation_token",
    REPO_ROOT / "tools" / "validate_confirmation_token.py",
)
assert _spec is not None and _spec.loader is not None
validate_token = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_token)


def _identity(**overrides):
    base = {"hwnd": 65986, "pid": 12340, "process_started_at": 134021234567890000}
    base.update(overrides)
    return base


class IssueConsumeFlowTests(unittest.TestCase):
    def test_valid_flow_issue_consume_succeeds_and_matches_schema(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "close")
        # The produced document ACTUALLY passes the schema validator.
        self.assertEqual(validate_token.validate_token(token), [])
        self.assertIsNone(store.consume(token["token_id"], _identity(), "close"))
        # A consumed record (with consumed_at set) also conforms to the schema.
        stored = store._tokens[token["token_id"]]
        self.assertIn("consumed_at", stored)
        self.assertEqual(validate_token.validate_token(stored), [])

    def test_second_consume_fails_single_use(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "text")
        store.consume(token["token_id"], _identity(), "text")
        with self.assertRaises(PermissionError) as ctx:
            store.consume(token["token_id"], _identity(), "text")
        self.assertIn("already consumed", str(ctx.exception))
        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)
        self.assertNotIn(token["token_id"], str(ctx.exception))

    def test_expired_token_is_rejected(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "close", ttl_seconds=60)
        # White-box aging (deterministic; no wait): the record's format is
        # always valid since it is machine-generated.
        store._tokens[token["token_id"]]["expires_at"] = "2020-01-01T00:00:00+03:00"
        with self.assertRaises(PermissionError) as ctx:
            store.consume(token["token_id"], _identity(), "close")
        self.assertIn("has expired", str(ctx.exception))
        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)
        self.assertNotIn(token["token_id"], str(ctx.exception))

    def test_wrong_pid_is_rejected_naming_the_field(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "close")
        with self.assertRaises(PermissionError) as ctx:
            store.consume(token["token_id"], _identity(pid=9999), "close")
        message = str(ctx.exception)
        self.assertIn("target identity mismatch", message)
        self.assertIn("pid", message)

    def test_wrong_action_class_is_rejected(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "text")
        with self.assertRaises(PermissionError) as ctx:
            store.consume(token["token_id"], _identity(), "close")
        self.assertIn("action class mismatch", str(ctx.exception))

        # A rejected confused-deputy attempt must not consume the token, but
        # its only remaining valid use is the original action and target.
        self.assertIsNone(store.consume(token["token_id"], _identity(), "text"))

    def test_token_cannot_cross_target_then_can_only_serve_original_target(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "close")
        with self.assertRaises(PermissionError):
            store.consume(token["token_id"], _identity(process_started_at=1), "close")

        self.assertIsNone(store.consume(token["token_id"], _identity(), "close"))

    def test_unknown_token_id_is_rejected(self):
        store = ConfirmationStore()
        unknown_token = "00000000-0000-4000-8000-000000000000"
        with self.assertRaises(PermissionError) as ctx:
            store.consume(unknown_token, _identity(), "close")
        self.assertIn("unknown confirmation token", str(ctx.exception))
        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)
        self.assertNotIn(unknown_token, str(ctx.exception))

    def test_issue_validates_inputs_with_value_error(self):
        store = ConfirmationStore()
        with self.assertRaises(ValueError):
            store.issue(_identity(), "click")
        with self.assertRaises(ValueError):
            store.issue({"hwnd": 1, "pid": 2}, "close")
        with self.assertRaises(ValueError):
            store.issue(_identity(hwnd=True), "close")

    def test_drag_full_flow_issue_consume_and_matches_schema(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "drag")
        self.assertEqual(token["action_class"], "drag")
        # The produced drag document ACTUALLY passes the schema validator.
        self.assertEqual(validate_token.validate_token(token), [])
        self.assertIsNone(store.consume(token["token_id"], _identity(), "drag"))
        with self.assertRaises(PermissionError) as ctx:
            store.consume(token["token_id"], _identity(), "drag")
        self.assertIn("already consumed", str(ctx.exception))

    def test_drag_token_consumed_as_close_is_rejected(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "drag")
        with self.assertRaises(PermissionError) as ctx:
            store.consume(token["token_id"], _identity(), "close")
        self.assertIn("action class mismatch", str(ctx.exception))

    def test_ttl_bounds_are_value_error(self):
        store = ConfirmationStore()
        with self.assertRaises(ValueError):
            store.issue(_identity(), "close", ttl_seconds=301)
        with self.assertRaises(ValueError):
            store.issue(_identity(), "close", ttl_seconds=0)
        with self.assertRaises(ValueError):
            store.issue(_identity(), "close", ttl_seconds=-5)

    def test_issue_copies_identity_defensively(self):
        store = ConfirmationStore()
        identity = _identity()
        token = store.issue(identity, "close")
        identity["pid"] = 1  # binding holds even if the caller mutates it afterward
        self.assertIsNone(store.consume(token["token_id"], _identity(), "close"))

    def test_stores_are_isolated(self):
        first, second = ConfirmationStore(), ConfirmationStore()
        token = first.issue(_identity(), "close")
        with self.assertRaises(PermissionError):
            second.consume(token["token_id"], _identity(), "close")

    def test_module_level_functions_delegate(self):
        token = confirmation.issue_confirmation(_identity(), "text")
        self.assertEqual(validate_token.validate_token(token), [])
        self.assertIsNone(
            confirmation.consume_confirmation(token["token_id"], _identity(), "text")
        )


class PreviewTests(unittest.TestCase):
    def test_requires_confirmation_per_action_class(self):
        store = ConfirmationStore()
        # `drag` was added to the protected class on 2026-09-14 (the
        # schema enum was extended); the previous `False` assertion would
        # now be WRONG.
        for action in ("text", "close", "drag"):
            summary = store.preview(action, _identity())
            self.assertTrue(summary["requires_confirmation"], msg=action)
            self.assertIsNone(summary["token_id"])
        for action in ("observe", "hover", "click", "key"):
            summary = store.preview(action, _identity())
            self.assertFalse(summary["requires_confirmation"], msg=action)

    def test_preview_reports_live_token(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "close")
        summary = store.preview("close", _identity())
        self.assertEqual(summary["token_id"], token["token_id"])
        self.assertEqual(summary["token_expires_at"], token["expires_at"])
        self.assertEqual(summary["action_class"], "close")
        self.assertEqual(summary["target_identity"], _identity())

    def test_preview_hides_consumed_and_expired_tokens(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "close")
        store.consume(token["token_id"], _identity(), "close")
        self.assertIsNone(store.preview("close", _identity())["token_id"])
        other = store.issue(_identity(), "text")
        store._tokens[other["token_id"]]["expires_at"] = "2020-01-01T00:00:00+03:00"
        self.assertIsNone(store.preview("text", _identity())["token_id"])

    def test_preview_has_no_side_effects(self):
        store = ConfirmationStore()
        token = store.issue(_identity(), "close")
        before = {tid: dict(record) for tid, record in store._tokens.items()}
        with (
            patch.object(
                ConfirmationStore, "issue", side_effect=AssertionError("issue yok")
            ),
            patch.object(
                ConfirmationStore, "consume", side_effect=AssertionError("consume yok")
            ),
        ):
            first = store.preview("close", _identity())
            second = store.preview("close", _identity())
        self.assertEqual(first, second)
        self.assertEqual(
            {tid: dict(record) for tid, record in store._tokens.items()}, before
        )
        self.assertEqual(first["token_id"], token["token_id"])


class PurgeTests(unittest.TestCase):
    def test_issuing_a_new_token_amortizes_expired_token_cleanup(self):
        store = ConfirmationStore()
        dead = store.issue(_identity(), "text")
        store._tokens[dead["token_id"]]["expires_at"] = "2020-01-01T00:00:00+03:00"

        live = store.issue(_identity(), "close")

        self.assertNotIn(dead["token_id"], store._tokens)
        self.assertIn(live["token_id"], store._tokens)

    def test_purge_expired_returns_count_and_keeps_live(self):
        store = ConfirmationStore()
        live = store.issue(_identity(), "close")
        dead = store.issue(_identity(), "text")
        store._tokens[dead["token_id"]]["expires_at"] = "2020-01-01T00:00:00+03:00"
        self.assertEqual(store.purge_expired(), 1)
        self.assertIn(live["token_id"], store._tokens)
        self.assertNotIn(dead["token_id"], store._tokens)
        self.assertEqual(store.purge_expired(), 0)


if __name__ == "__main__":
    unittest.main()
