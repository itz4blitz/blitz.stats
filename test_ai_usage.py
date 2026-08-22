"""Cursor usage parsing for the Omarchy AI bar."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_usage import _cursor_session, _openrouter_provider_key, _parse_cursor_quota


class ParseCursorQuotaTest(unittest.TestCase):
    def test_included_cents_become_monthly_percent_and_dollar_status(self) -> None:
        parsed = _parse_cursor_quota(
            {
                "planUsage": {
                    "limit": 20000,
                    "remaining": 7660,
                    "includedSpend": 12340,
                    "totalSpend": 12340,
                },
                "billingCycleEnd": 1772323200000,
            },
            membership="ultra",
        )

        self.assertEqual(
            parsed,
            {
                "tierLabel": "Cursor Ultra",
                "statusText": "$123.40 used · $76.60 left",
                "limits": [
                    {
                        "kind": "monthly",
                        "label": "Monthly included",
                        "percent": 0.617,
                        "resetsAt": "2026-03-01T00:00:00+00:00",
                    }
                ],
                "balance": {
                    "limit": 200.0,
                    "remaining": 76.6,
                    "usageMonthly": 123.4,
                    "usageWeekly": 0.0,
                },
            },
        )

    def test_used_falls_back_to_limit_minus_remaining(self) -> None:
        parsed = _parse_cursor_quota(
            {"planUsage": {"limit": 10000, "remaining": 2500}},
            membership="pro",
        )

        self.assertEqual(parsed["tierLabel"], "Cursor Pro")
        self.assertEqual(parsed["limits"][0]["percent"], 0.75)
        self.assertEqual(parsed["statusText"], "$75.00 used · $25.00 left")
        self.assertEqual(parsed["balance"]["usageMonthly"], 75.0)

    def test_missing_or_empty_plan_usage_is_unusable(self) -> None:
        self.assertIsNone(_parse_cursor_quota({}, membership="ultra"))
        self.assertIsNone(_parse_cursor_quota({"planUsage": {"limit": 0, "includedSpend": 0}}))
        self.assertIsNone(_parse_cursor_quota(None))

    def test_live_dashboard_shape_uses_included_spend_not_auto_percent(self) -> None:
        parsed = _parse_cursor_quota(
            {
                "billingCycleEnd": 1789178715000,
                "displayMessage": "You've used 1% of your included usage",
                "planUsage": {
                    "totalSpend": 440,
                    "includedSpend": 440,
                    "remaining": 39560,
                    "limit": 40000,
                    "autoPercentUsed": 0.22,
                    "totalPercentUsed": 0.176,
                },
            },
            membership="ultra",
        )

        self.assertEqual(parsed["limits"][0]["percent"], 0.011)
        self.assertEqual(parsed["limits"][0]["resetsAt"], "2026-09-12T02:05:15+00:00")
        self.assertEqual(parsed["statusText"], "$4.40 used · $395.60 left")
        self.assertEqual(parsed["tierLabel"], "Cursor Ultra")


class CursorSessionTest(unittest.TestCase):
    def test_reads_access_token_and_membership_from_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
            db.parent.mkdir(parents=True)
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?), (?, ?)",
                (
                    "cursorAuth/accessToken",
                    "eyJtest-token",
                    "cursorAuth/stripeMembershipType",
                    "ultra",
                ),
            )
            conn.commit()
            conn.close()

            session = _cursor_session(home)

            self.assertEqual(session["token"], "eyJtest-token")
            self.assertEqual(session["membership"], "ultra")

    def test_missing_database_returns_empty_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_cursor_session(Path(tmp)), {"token": "", "membership": ""})


class OpenRouterKeyTest(unittest.TestCase):
    def test_env_key_does_not_invoke_onepassword(self) -> None:
        def runner(*_a, **_k):
            raise AssertionError("op should not run when OPENROUTER_API_KEY is set")

        key = _openrouter_provider_key(
            environment={"OPENROUTER_API_KEY": "sk-or-test"},
            runner=runner,
        )
        self.assertEqual(key, "sk-or-test")

    def test_missing_secrets_file_does_not_use_hardcoded_op_ref(self) -> None:
        calls = []

        class Result:
            returncode = 0
            stdout = "should-not-be-used\n"

        def runner(cmd, **_k):
            calls.append(cmd)
            return Result()

        with tempfile.TemporaryDirectory() as tmp:
            key = _openrouter_provider_key(
                environment={},
                runner=runner,
                home=Path(tmp),
            )
        self.assertEqual(key, "")
        self.assertEqual(calls, [])

    def test_reads_op_ref_from_secrets_file(self) -> None:
        class Result:
            returncode = 0
            stdout = "sk-from-op\n"

        def runner(cmd, **_k):
            self.assertEqual(cmd[:2], ["op", "read"])
            self.assertEqual(cmd[2], "op://Private/OpenRouter API Key - opencode/credential")
            return Result()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            secrets = home / ".config" / "omarchy" / "plugins" / "blitz.ai" / "secrets.json"
            secrets.parent.mkdir(parents=True)
            secrets.write_text('{"openrouterOp": "op://Private/OpenRouter API Key - opencode/credential"}\n')
            key = _openrouter_provider_key(environment={}, runner=runner, home=home)
        self.assertEqual(key, "sk-from-op")


if __name__ == "__main__":
    unittest.main()

