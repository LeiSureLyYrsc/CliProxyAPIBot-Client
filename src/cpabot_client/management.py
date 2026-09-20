from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

_AUTH_FAIL_COOLDOWN = 60.0
_FORBIDDEN_COOLDOWN = 300.0

_API_CALL_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    ("api.anthropic.com", "/api/oauth/usage"): frozenset({"GET"}),
    ("chatgpt.com", "/backend-api/wham/usage"): frozenset({"GET"}),
    ("chatgpt.com", "/backend-api/wham/rate-limit-reset-credits"): frozenset({"GET"}),
    ("chatgpt.com", "/backend-api/wham/rate-limit-reset-credits/consume"): frozenset({"POST"}),
    ("api.kimi.com", "/coding/v1/usages"): frozenset({"GET"}),
    ("cli-chat-proxy.grok.com", "/v1/billing"): frozenset({"GET"}),
    ("daily-cloudcode-pa.googleapis.com", "/v1internal:retrieveUserQuotaSummary"): frozenset({"POST"}),
    ("daily-cloudcode-pa.sandbox.googleapis.com", "/v1internal:retrieveUserQuotaSummary"): frozenset({"POST"}),
    ("cloudcode-pa.googleapis.com", "/v1internal:retrieveUserQuotaSummary"): frozenset({"POST"}),
    ("cloudcode-pa.googleapis.com", "/v1internal:retrieveUserQuota"): frozenset({"POST"}),
    ("daily-cloudcode-pa.googleapis.com", "/v1internal:loadCodeAssist"): frozenset({"POST"}),
    ("daily-cloudcode-pa.sandbox.googleapis.com", "/v1internal:loadCodeAssist"): frozenset({"POST"}),
    ("cloudcode-pa.googleapis.com", "/v1internal:loadCodeAssist"): frozenset({"POST"}),
}

_MANAGEMENT_ALLOWLIST = frozenset({("GET", "/auth-files"), ("POST", "/api-call")})


def allowed_api_call_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    path = parsed.path
    return (host, path) in _API_CALL_ALLOWLIST


def allowed_api_call(method: str, url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    methods = _API_CALL_ALLOWLIST.get((parsed.hostname.lower(), parsed.path), frozenset())
    return method.upper() in methods


class CPAError(Exception):
    """Management API 调用失败。"""


def management_root(base_url: str) -> str:
    url = base_url.rstrip("/")
    suffix = "/v0/management"
    if url.endswith(suffix):
        return url
    return f"{url}{suffix}"


class ManagementClient:
    def __init__(self, base_url: str, key: str, timeout: float) -> None:
        self._root = management_root(base_url)
        self._key = key
        self._client = httpx.AsyncClient(
            base_url=self._root,
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {key}",
                "X-Management-Key": key,
                "Accept": "application/json",
                "User-Agent": "CPA-Bot-Client/0.1.0",
            },
        )
        self._auth_blocked_until = 0.0
        self._auth_block_reason = ""

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ensure_ready(self) -> None:
        if not self._key:
            raise CPAError("未配置 CPA_MANAGEMENT_KEY。")
        now = time.monotonic()
        if now < self._auth_blocked_until:
            raise CPAError(self._auth_block_reason)

    def _block_auth(self, seconds: float, reason: str) -> None:
        self._auth_blocked_until = time.monotonic() + seconds
        self._auth_block_reason = reason

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        self._ensure_ready()
        verb = method.upper()
        normalized_path = "/" + path.strip("/")
        if (verb, normalized_path) not in _MANAGEMENT_ALLOWLIST:
            raise CPAError("只读客户端拒绝该管理路径。")
        try:
            response = await self._client.request(verb, path, params=params, json=json, timeout=timeout)
        except httpx.RequestError as exc:
            raise CPAError(f"无法连接 CLIProxyAPI：{exc}") from exc
        if response.status_code == 401:
            reason = "管理密钥无效（401）。1 分钟内暂停后续请求。"
            self._block_auth(_AUTH_FAIL_COOLDOWN, reason)
            raise CPAError(reason)
        if response.status_code == 403:
            reason = "管理接口拒绝访问（403）。5 分钟内暂停后续请求。"
            self._block_auth(_FORBIDDEN_COOLDOWN, reason)
            raise CPAError(reason)
        if response.status_code >= 400:
            raise CPAError(f"CLIProxyAPI 返回 HTTP {response.status_code}")
        return response

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self.request(method, path, params=params, json=json, timeout=timeout)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise CPAError("CLIProxyAPI 返回了无法解析的 JSON。") from exc

    async def list_auth_files(
        self,
        *,
        name: str | None = None,
        auth_index: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if auth_index:
            params["auth_index"] = auth_index
        data = await self.request_json("GET", "/auth-files", params=params or None)
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, list) else []

    async def api_call(
        self,
        auth_index: str,
        method: str,
        url: str,
        *,
        header: dict[str, str] | None = None,
        data: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        verb = method.upper()
        if not allowed_api_call(verb, url):
            raise CPAError("拒绝调用未列入额度白名单的方法或上游地址。")
        payload: dict[str, Any] = {"auth_index": auth_index, "method": verb, "url": url}
        if header:
            payload["header"] = header
        if data is not None:
            payload["data"] = data
        result = await self.request_json("POST", "/api-call", json=payload, timeout=timeout)
        return result if isinstance(result, dict) else {}
