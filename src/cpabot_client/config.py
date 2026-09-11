from __future__ import annotations

import unicodedata

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .protocol import valid_client_name


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    client_name: str = "Server"
    server_url: str = "ws://127.0.0.1:8320/v1/client/ws"
    client_key: str = ""
    cpa_base_url: str = "http://127.0.0.1:8317"
    cpa_management_key: str = ""
    cpa_timeout: float = 15.0
    cpa_quota_timeout: float = 25.0
    cpa_quota_concurrency: int = 4
    cpa_quota_cache_ttl: float = 60.0
    reconnect_min: float = 1.0
    reconnect_max: float = 30.0

    @field_validator("client_name")
    @classmethod
    def check_name(cls, value: str) -> str:
        name = unicodedata.normalize("NFC", value.strip() or "Server")
        if not valid_client_name(name):
            raise ValueError("CLIENT_NAME 非法。")
        return name

    @field_validator("cpa_base_url", "server_url", "cpa_management_key", "client_key")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()
