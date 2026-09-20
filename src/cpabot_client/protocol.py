from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PROTOCOL_VERSION = 1
ALLOWED_ACTIONS = frozenset({"quota.query", "codex.refresh"})
MAX_CLIENT_NAME_LEN = 32
MAX_ACCOUNTS = 200
MAX_WINDOWS = 32
CLIENT_NAME_RE = re.compile(r"^[^\s/\\]{1,32}$")

QuotaAction = Literal["quota.query", "codex.refresh"]


def normalize_client_name(value: str) -> str:
    return unicodedata.normalize("NFC", (value or "").strip())


def valid_client_name(value: str) -> bool:
    name = normalize_client_name(value)
    if not name or len(name) > MAX_CLIENT_NAME_LEN:
        return False
    if name.startswith("-"):
        return False
    if any(ch in name for ch in "/\\") or any(ch.isspace() for ch in name):
        return False
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return False
    return True


class QuotaQueryPayload(BaseModel):
    platform: str | None = None
    account: str | None = None
    fresh: bool = False

    @field_validator("platform", "account", mode="before")
    @classmethod
    def empty_to_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class Envelope(BaseModel):
    version: int = PROTOCOL_VERSION
    type: Literal["request", "response", "hello", "error"]
    id: str = ""
    action: str | None = None
    payload: dict[str, Any] | None = None
    ok: bool | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def stringify_id(cls, value: Any) -> str:
        return str(value or "")


class QuotaWindowDTO(BaseModel):
    id: str
    label: str
    used_percent: float | None = None
    remaining_percent: float | None = None
    remaining: float | None = None
    limit: float | None = None
    reset_label: str = "-"
    reset_at: float | None = None


class AccountQuotaDTO(BaseModel):
    platform: str
    name: str
    auth_index: str = ""
    plan: str = ""
    status: str = "unknown"
    error: str = ""
    windows: list[QuotaWindowDTO] = Field(default_factory=list)
    disabled: bool = False
    cooling: bool = False
    client_name: str = ""
    subscription_expires_at: float | None = None
    subscription_expires_label: str = ""
    reset_credits: int | None = None

    @field_validator("windows")
    @classmethod
    def limit_windows(cls, value: list[QuotaWindowDTO]) -> list[QuotaWindowDTO]:
        return value[:MAX_WINDOWS]


class QuotaQueryResult(BaseModel):
    client_name: str
    queried_at: str = ""
    cached: bool = False
    accounts: list[AccountQuotaDTO] = Field(default_factory=list)

    @field_validator("accounts")
    @classmethod
    def limit_accounts(cls, value: list[AccountQuotaDTO]) -> list[AccountQuotaDTO]:
        return value[:MAX_ACCOUNTS]


class CodexRefreshPayload(BaseModel):
    account: str

    @field_validator("account", mode="before")
    @classmethod
    def validate_account(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("account 不能为空")
        return text


class CodexRefreshResult(BaseModel):
    message: str
    remaining_credits: int | None = None
