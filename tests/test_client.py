from __future__ import annotations

import unittest
from pathlib import Path
import sys

_src = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_src))

from cpabot_client.config import Config
from cpabot_client.management import CPAError, ManagementClient, allowed_api_call, allowed_api_call_url
from cpabot_client.quota import (
    AccountQuota,
    QuotaWindow,
    clear_quota_cache,
    consume_codex_reset,
    count_codex_reset_credits,
    extract_subscription_expiry,
    format_subscription_expiry_label,
    is_codex_refresh_success,
    parse_antigravity_summary,
    parse_claude_usage,
    parse_codex_usage,
    parse_kimi_usage,
    parse_xai_billing,
    pick_codex_reset_credit,
    _codex_headers,
    _sanitize_plan,
    _parse_ts,
    _slugify_grok_product,
)
from cpabot_client.protocol import (
    ALLOWED_ACTIONS,
    AccountQuotaDTO,
    CodexRefreshPayload,
    CodexRefreshResult,
    QuotaQueryResult,
    QuotaWindowDTO,
)
from cpabot_client.service import account_to_dto, handle_envelope, run_codex_refresh


class AllowlistTests(unittest.TestCase):
    def test_quota_urls_allowed(self) -> None:
        self.assertTrue(
            allowed_api_call_url(
                "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
            )
        )
        self.assertTrue(
            allowed_api_call_url("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")
        )
        self.assertTrue(
            allowed_api_call("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")
        )
        self.assertFalse(
            allowed_api_call("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")
        )
        self.assertFalse(allowed_api_call_url("https://example.com/steal"))
        self.assertTrue(
            allowed_api_call_url("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume")
        )
        self.assertTrue(
            allowed_api_call("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume")
        )
        self.assertFalse(
            allowed_api_call("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume")
        )
        self.assertTrue(allowed_api_call("GET", "https://chatgpt.com/backend-api/wham/usage"))
        self.assertFalse(allowed_api_call("POST", "https://chatgpt.com/backend-api/wham/usage"))

    def test_allowed_actions(self) -> None:
        self.assertEqual(ALLOWED_ACTIONS, frozenset({"quota.query", "codex.refresh"}))


class ProtocolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_default_config_codex_refresh_disabled(self) -> None:
        cfg = Config(client_name="Home", client_key="x", cpa_management_key="y")
        self.assertFalse(cfg.codex_refresh_enabled)

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
        self.assertIn("action", reply["error"])

    async def test_codex_refresh_disabled_zero_side_effect(self) -> None:
        cfg = Config(client_name="Home", client_key="x", cpa_management_key="y", codex_refresh_enabled=False)
        # client can be a dummy object that raises if any method is called
        class ExplodingClient:
            async def list_auth_files(self):
                raise AssertionError("Should not be called when disabled")
            async def api_call(self, *args, **kwargs):
                raise AssertionError("Should not be called when disabled")

        reply = await handle_envelope(
            cfg,
            ExplodingClient(),  # type: ignore[arg-type]
            {"version": 1, "type": "request", "id": "1", "action": "codex.refresh", "payload": {"account": "acc1"}},
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertFalse(reply["ok"])
        self.assertIn("未启用 Codex 额度刷新功能", reply["error"])

    async def test_codex_refresh_enabled_flow(self) -> None:
        cfg = Config(client_name="Home", client_key="x", cpa_management_key="y", codex_refresh_enabled=True)

        class MockClient:
            def __init__(self):
                self.calls = []

            async def list_auth_files(self):
                return [
                    {"provider": "codex", "name": "cx_user1.json", "label": "cx_user1", "auth_index": "idx-1", "chatgpt_account_id": "acc-1"},
                    {"provider": "claude", "name": "cl_user.json", "label": "cl_user", "auth_index": "idx-2"},
                ]

            async def api_call(self, auth_index: str, method: str, url: str, **kwargs):
                self.calls.append((auth_index, method, url, kwargs))
                if "rate-limit-reset-credits/consume" in url:
                    return {"status_code": 200, "body": '{"status": "ok", "remaining": 1}'}
                if "rate-limit-reset-credits" in url:
                    return {
                        "status_code": 200,
                        "body": '{"credits": [{"id": "c1", "status": "available", "expires_at": 1790000000}]}',
                    }
                return {"status_code": 200, "body": "{}"}

        mock = MockClient()
        # 0 match
        reply0 = await handle_envelope(
            cfg,
            mock,  # type: ignore[arg-type]
            {"version": 1, "type": "request", "id": "1", "action": "codex.refresh", "payload": {"account": "notfound"}},
        )
        self.assertFalse(reply0["ok"])
        self.assertIn("没有找到 Codex 凭证", reply0["error"])

        # Multiple matches
        class MultiClient(MockClient):
            async def list_auth_files(self):
                return [
                    {"provider": "codex", "name": "cx_user1.json", "auth_index": "idx-1"},
                    {"provider": "codex", "name": "cx_user2.json", "auth_index": "idx-2"},
                ]

        reply_multi = await handle_envelope(
            cfg,
            MultiClient(),  # type: ignore[arg-type]
            {"version": 1, "type": "request", "id": "2", "action": "codex.refresh", "payload": {"account": "cx"}},
        )
        self.assertFalse(reply_multi["ok"])
        self.assertIn("匹配到多个 Codex 凭证", reply_multi["error"])

        # Success match
        reply_ok = await handle_envelope(
            cfg,
            mock,  # type: ignore[arg-type]
            {"version": 1, "type": "request", "id": "3", "action": "codex.refresh", "payload": {"account": "cx_user1"}},
        )
        self.assertTrue(reply_ok["ok"])
        self.assertEqual(reply_ok["result"]["remaining_credits"], 1)
        self.assertIn("已为 cx_user1", reply_ok["result"]["message"])
        self.assertNotIn("idx-1", str(reply_ok))  # Do NOT expose auth_index

    async def test_codex_refresh_business_failure_raises_and_preserves_cache(self) -> None:
        cfg = Config(client_name="Home", client_key="x", cpa_management_key="y", codex_refresh_enabled=True)

        import cpabot_client.quota as quota_mod
        quota_mod._cache_key = "test-cache"
        quota_mod._cache_expires = 9999999999.0

        class FailClient:
            def __init__(self, fail_body: str):
                self.fail_body = fail_body

            async def api_call(self, auth_index: str, method: str, url: str, **kwargs):
                if "rate-limit-reset-credits/consume" in url:
                    return {"status_code": 200, "body": self.fail_body}
                if "rate-limit-reset-credits" in url:
                    return {
                        "status_code": 200,
                        "body": '{"credits": [{"id": "c1", "status": "available"}]}',
                    }
                return {"status_code": 200, "body": "{}"}

        file = {"provider": "codex", "name": "cx1.json", "label": "cx1", "auth_index": "idx-1"}

        # 1. no_credit failure
        fail_mock1 = FailClient('{"code": "no_credit"}')
        with self.assertRaises(CPAError) as cm1:
            await consume_codex_reset(file, client=fail_mock1, cfg=cfg)  # type: ignore[arg-type]
        self.assertIn("没有可用的 Codex 重置次数", str(cm1.exception))
        self.assertEqual(quota_mod._cache_key, "test-cache")  # cache preserved

        # 2. already_redeemed failure
        fail_mock2 = FailClient('{"code": "already_redeemed"}')
        with self.assertRaises(CPAError) as cm2:
            await consume_codex_reset(file, client=fail_mock2, cfg=cfg)  # type: ignore[arg-type]
        self.assertIn("已经用过", str(cm2.exception))
        self.assertEqual(quota_mod._cache_key, "test-cache")

        # 3. unknown error
        fail_mock3 = FailClient('{"status": "error", "error": "rate limited"}')
        with self.assertRaises(CPAError) as cm3:
            await consume_codex_reset(file, client=fail_mock3, cfg=cfg)  # type: ignore[arg-type]
        self.assertIn("rate limited", str(cm3.exception))
        self.assertEqual(quota_mod._cache_key, "test-cache")

        # 4. handle_envelope on business failure returns ok=false
        class FailEnvClient:
            async def list_auth_files(self):
                return [file]
            async def api_call(self, auth_index: str, method: str, url: str, **kwargs):
                if "rate-limit-reset-credits/consume" in url:
                    return {"status_code": 200, "body": '{"code": "nothing_to_reset"}'}
                return {
                    "status_code": 200,
                    "body": '{"credits": [{"id": "c1", "status": "available"}]}',
                }

        reply_fail = await handle_envelope(
            cfg,
            FailEnvClient(),  # type: ignore[arg-type]
            {"version": 1, "type": "request", "id": "f1", "action": "codex.refresh", "payload": {"account": "cx1"}},
        )
        self.assertIsNotNone(reply_fail)
        assert reply_fail is not None
        self.assertFalse(reply_fail["ok"])
        self.assertIn("当前没有需要重置的额度窗口", reply_fail["error"])
        self.assertEqual(quota_mod._cache_key, "test-cache")

        # 5. Success clears cache
        class SuccessClient:
            async def api_call(self, auth_index: str, method: str, url: str, **kwargs):
                if "rate-limit-reset-credits/consume" in url:
                    return {"status_code": 200, "body": '{"status": "ok", "remaining": 0}'}
                return {
                    "status_code": 200,
                    "body": '{"credits": [{"id": "c1", "status": "available"}]}',
                }

        await consume_codex_reset(file, client=SuccessClient(), cfg=cfg)  # type: ignore[arg-type]
        self.assertEqual(quota_mod._cache_key, "")  # cache cleared

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

    def test_codex_refresh_payload_and_result_models(self) -> None:
        with self.assertRaises(Exception):
            CodexRefreshPayload(account="   ")
        p = CodexRefreshPayload(account="cx_user")
        self.assertEqual(p.account, "cx_user")

        res = CodexRefreshResult(message="ok", remaining_credits=2)
        dumped = res.model_dump()
        self.assertEqual(dumped["message"], "ok")
        self.assertEqual(dumped["remaining_credits"], 2)
        loaded = CodexRefreshResult.model_validate(dumped)
        self.assertEqual(loaded.remaining_credits, 2)

    def test_remote_dto_does_not_expose_auth_index(self) -> None:
        dto = account_to_dto(
            AccountQuota(
                platform="codex",
                name="CX-1",
                auth_index="secret-index",
                subscription_expires_at=1760000000.0,
                subscription_expires_label="2025-10-09 (剩20天)",
                reset_credits=3,
                windows=[QuotaWindow(id="code-5h", label="5h", reset_at=1760001000.0, reset_label="15m")],
            )
        )
        self.assertEqual(dto.auth_index, "")
        self.assertEqual(dto.subscription_expires_at, 1760000000.0)
        self.assertEqual(dto.subscription_expires_label, "2025-10-09 (剩20天)")
        self.assertEqual(dto.reset_credits, 3)
        self.assertEqual(dto.windows[0].reset_at, 1760001000.0)

        # Roundtrip via model_dump / model_validate
        dumped = dto.model_dump()
        self.assertEqual(dumped["auth_index"], "")
        self.assertEqual(dumped["subscription_expires_at"], 1760000000.0)
        self.assertEqual(dumped["reset_credits"], 3)
        validated = AccountQuotaDTO.model_validate(dumped)
        self.assertEqual(validated.subscription_expires_at, 1760000000.0)
        self.assertEqual(validated.reset_credits, 3)

    async def test_readonly_paths(self) -> None:
        client = ManagementClient("http://127.0.0.1:8317", "k", 5)
        with self.assertRaises(CPAError):
            await client.request("POST", "/reset-quota", json={})
        await client.aclose()


class ParserSmokeTests(unittest.TestCase):
    def test_grok_product_usage_and_snake_case(self) -> None:
        payload = {
            "config": {
                "creditUsagePercent": 45.0,
                "currentPeriod": {"end": "2026-09-30T00:00:00Z"},
                "productUsage": [
                    {"name": "GrokImagine", "usagePercent": 20.0},
                    {"name": "GrokChat", "used_percent": 30.0},
                    {"product": "GrokBuild", "credit_usage_percent": 10.0},
                ],
            }
        }
        windows = parse_xai_billing(payload)
        labels = [w.label for w in windows]
        ids = [w.id for w in windows]
        self.assertIn("周额度", labels)
        self.assertIn("grok-imagine", ids)
        self.assertIn("grok-chat", ids)
        self.assertIn("grok-build", ids)
        for w in windows:
            if w.id == "grok-imagine":
                self.assertEqual(w.used_percent, 20.0)
                self.assertEqual(w.remaining_percent, 80.0)

    def test_slugify_grok(self) -> None:
        self.assertEqual(_slugify_grok_product("GrokImagine"), "grok-imagine")
        self.assertEqual(_slugify_grok_product("GrokChat"), "grok-chat")
        self.assertEqual(_slugify_grok_product("Voice Mode"), "grok-voice-mode")

    def test_antigravity(self) -> None:
        windows = parse_antigravity_summary(
            {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "displayName": "Five Hour Limit",
                                "remainingFraction": 0.86,
                                "resetTime": "2026-09-17T12:00:00Z",
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(windows[0].remaining_percent, 86.0)
        self.assertIsNotNone(windows[0].reset_at)

    def test_claude_reset_at(self) -> None:
        windows, plan = parse_claude_usage(
            {
                "five_hour": {"utilization": 20.0, "resets_at": "2026-09-17T15:00:00Z"},
                "plan_type": "pro",
            }
        )
        self.assertEqual(plan, "Pro")
        self.assertEqual(len(windows), 1)
        self.assertIsNotNone(windows[0].reset_at)

    def test_codex_reset_at(self) -> None:
        windows, plan = parse_codex_usage(
            {
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 18000,
                        "used_percent": 30.0,
                        "reset_at": 1760000000.0,
                    }
                },
                "plan_type": "plus",
            }
        )
        self.assertEqual(plan, "Plus")
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].reset_at, 1760000000.0)

    def test_pick_codex_reset_credit_earliest_expiry(self) -> None:
        payload = {
            "credits": [
                {"id": "c_late", "status": "available", "expires_at": "2026-12-01T00:00:00Z"},
                {"id": "c_early", "status": "available", "expires_at": "2026-10-01T00:00:00Z"},
                {"id": "c_used", "status": "redeemed", "expires_at": "2026-09-01T00:00:00Z"},
            ]
        }
        picked = pick_codex_reset_credit(payload)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "c_early")

    def test_subscription_expiry_parsing(self) -> None:
        # Explicit subscription dictionary
        data1 = {"subscription": {"expires_at": "2026-10-01T00:00:00Z"}}
        ts1, label1 = extract_subscription_expiry(data1)
        self.assertIsNotNone(ts1)
        self.assertIn("2026-10-01", label1)

        # Codex id_token chatgpt_subscription_active_until
        data_id_token = {"id_token": {"chatgpt_subscription_active_until": 1760000000}}
        ts_id, label_id = extract_subscription_expiry(data_id_token)
        self.assertEqual(ts_id, 1760000000.0)
        self.assertTrue(label_id)

        # Top-level token expires_at is not a reliable subscription expiry.
        data2 = {"expires_at": 1760000000}
        ts2, label2 = extract_subscription_expiry(data2)
        self.assertIsNone(ts2)
        self.assertEqual(label2, "")

        # Explicit top-level subscription expiry remains supported.
        data3 = {"subscription_expires_at": 1760000000}
        ts3, label3 = extract_subscription_expiry(data3)
        self.assertEqual(ts3, 1760000000.0)
        self.assertTrue(label3)

        # xAI billingPeriodEnd must NOT be parsed as subscription expiry
        data_xai = {"billingPeriodEnd": "2026-10-01T00:00:00Z", "creditUsagePercent": 50}
        ts_xai, label_xai = extract_subscription_expiry(data_xai)
        self.assertIsNone(ts_xai)
        self.assertEqual(label_xai, "")

    def test_count_codex_reset_credits(self) -> None:
        # direct available_count
        self.assertEqual(count_codex_reset_credits({"available_count": 5}), 5)
        # items list
        self.assertEqual(
            count_codex_reset_credits(
                {
                    "credits": [
                        {"id": "1", "status": "available"},
                        {"id": "2", "status": "redeemed"},
                        {"id": "3", "state": "active"},
                    ]
                }
            ),
            2,
        )
        # total/count
        self.assertEqual(count_codex_reset_credits({"count": 0}), 0)
        # empty / None
        self.assertIsNone(count_codex_reset_credits({}))

    def test_codex_headers_account_id(self) -> None:
        # Non-email account id is included
        h1 = _codex_headers({"chatgpt_account_id": "acc-12345"})
        self.assertEqual(h1.get("Chatgpt-Account-Id"), "acc-12345")

        # Email account id is NOT included
        h2 = _codex_headers({"chatgpt_account_id": "user@example.com"})
        self.assertNotIn("Chatgpt-Account-Id", h2)

        # Fallback fields with email ignored
        h3 = _codex_headers({"account_id": "test@domain.com"})
        self.assertNotIn("Chatgpt-Account-Id", h3)

    def test_sanitize_plan_dict_or_text(self) -> None:
        # Dict plan
        self.assertEqual(_sanitize_plan({"name": "pro"}), "Pro")
        self.assertEqual(_sanitize_plan({"tier": "g1-ultra-tier"}), "Ultra")
        self.assertEqual(_sanitize_plan({"unknown": "value"}), "")
        # Text repr of dict or auth mechanism
        self.assertEqual(_sanitize_plan("{'a': 1}"), "")
        self.assertEqual(_sanitize_plan("oauth"), "")
        self.assertEqual(_sanitize_plan("plus"), "Plus")

    def test_parse_ts_ms_and_iso(self) -> None:
        self.assertEqual(_parse_ts("1760000000000"), 1760000000.0)
        self.assertEqual(_parse_ts("1760000000"), 1760000000.0)
        self.assertIsNotNone(_parse_ts("2026-09-17T12:00:00Z"))
        self.assertIsNone(_parse_ts("invalid"))

    def test_subscription_expiry_label_local_tz(self) -> None:
        label = format_subscription_expiry_label(1760000000.0)
        self.assertTrue(len(label) > 0)
        self.assertIn("(", label)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
