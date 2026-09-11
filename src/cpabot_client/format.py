from __future__ import annotations

import re
from typing import Any

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def display_name(file: dict[str, Any], *, public: bool = False) -> str:
    label = str(file.get("label") or "").strip()
    if public:
        if label and "@" not in label and not _EMAIL.search(label):
            return label
        provider = str(file.get("provider") or file.get("type") or "acct").strip() or "acct"
        index = str(file.get("auth_index") or "")
        short = index[:4] if index else "????"
        return f"{provider}-{short}"
    for key in ("label", "email", "account", "id", "name"):
        value = file.get(key)
        if value:
            return str(value)
    return "(unknown)"


def is_cooling(file: dict[str, Any]) -> bool:
    if file.get("next_retry_after"):
        return True
    status = str(file.get("status") or "").lower()
    message = str(file.get("status_message") or "").lower()
    if "quota" in message or "429" in message or "cooldown" in message or "cooling" in message:
        return True
    return status in {"exhausted", "quota"}


def is_unhealthy(file: dict[str, Any]) -> bool:
    if is_cooling(file):
        return True
    if file.get("unavailable"):
        return True
    status = str(file.get("status") or "").lower()
    return status not in {"", "ready", "ok", "active"}


def match_auth(files: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return []
    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for file in files:
        fields = [
            str(file.get("auth_index") or ""),
            str(file.get("name") or ""),
            str(file.get("id") or ""),
            str(file.get("email") or ""),
            str(file.get("label") or ""),
            str(file.get("account") or ""),
        ]
        lowered = [item.lower() for item in fields if item]
        if needle in lowered:
            exact.append(file)
        elif any(item.startswith(needle) or needle in item for item in lowered):
            partial.append(file)
    return exact or partial
