from __future__ import annotations

import unittest
from pathlib import Path
import sys

_src = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_src))

from cpabot_client.config import Config
from cpabot_client.management import CPAError, ManagementClient, allowed_api_call, allowed_api_call_url
from cpabot_client.protocol import ALLOWED_ACTIONS
from cpabot_client.quota import AccountQuota, parse_antigravity_summary
from cpabot_client.service import account_to_dto, handle_envelope


class AllowlistTests(unittest.TestCase):
    def test_quota_urls_allowed(self) -> None:
        self.assertTrue(
            allowed_api_call_url(
                "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
            )
        )
        self.assertFalse(allowed_api_call_url("https://example.com/steal"))
        self.assertFalse(
            allowed_api_call_url("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume")
        )
        self.assertTrue(allowed_api_call("GET", "https://chatgpt.com/backend-api/wham/usage"))
        self.assertFalse(allowed_api_call("POST", "https://chatgpt.com/backend-api/wham/usage"))

    def test_query_action_only(self) -> None:
        self.assertEqual(ALLOWED_ACTIONS, frozenset({"quota.query"}))


class ProtocolHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unknown_action(self) -> None:
        cfg = Config(client_name="Home", client_key="x", cpa_management_key="y")
        client = object()
        reply = await handle_envelope(
            cfg,
            client,  # type: ignore[arg-type]
            {"version": 1, "type": "request", "id": "1", "action": "quota.reset", "payload": {}},
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertFalse(reply["ok"])
        self.assertIn("quota.query", reply["error"])

    async def test_rejects_wrong_version(self) -> None:
        cfg = Config(client_name="Home", client_key="x", cpa_management_key="y")
        reply = await handle_envelope(
            cfg,
            object(),  # type: ignore[arg-type]
            {"version": 9, "type": "request", "id": "1", "action": "quota.query", "payload": {}},
        )
        assert reply is not None
        self.assertFalse(reply["ok"])

    async def test_rejects_malformed_payload_without_disconnect(self) -> None:
        cfg = Config(client_name="Home", client_key="x", cpa_management_key="y")
        reply = await handle_envelope(
            cfg,
            object(),  # type: ignore[arg-type]
            {"version": 1, "type": "request", "id": "1", "action": "quota.query", "payload": {"fresh": "not-a-bool"}},
        )
        assert reply is not None
        self.assertFalse(reply["ok"])

    def test_remote_dto_does_not_expose_auth_index(self) -> None:
        dto = account_to_dto(AccountQuota(platform="codex", name="CX-1", auth_index="secret-index"))
        self.assertEqual(dto.auth_index, "")

    async def test_readonly_paths(self) -> None:
        client = ManagementClient("http://127.0.0.1:8317", "k", 5)
        with self.assertRaises(CPAError):
            await client.request("POST", "/reset-quota", json={})
        await client.aclose()


class ParserSmokeTests(unittest.TestCase):
    def test_antigravity(self) -> None:
        windows = parse_antigravity_summary(
            {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [{"displayName": "Five Hour Limit", "remainingFraction": 0.86}],
                    }
                ]
            }
        )
        self.assertEqual(windows[0].remaining_percent, 86.0)


if __name__ == "__main__":
    unittest.main()
