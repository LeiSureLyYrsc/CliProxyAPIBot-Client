from __future__ import annotations

import asyncio
import json
import logging
import random

from websockets.asyncio.client import connect

from .config import Config
from .management import ManagementClient
from .service import handle_envelope

logger = logging.getLogger("cpabot_client")


async def run_forever(cfg: Config) -> None:
    delay = cfg.reconnect_min
    client = ManagementClient(cfg.cpa_base_url, cfg.cpa_management_key, cfg.cpa_timeout)
    try:
        while True:
            try:
                await _session(cfg, client)
                delay = cfg.reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("连接中断：%s", exc)
            jitter = random.uniform(0, delay / 4)
            await asyncio.sleep(delay + jitter)
            delay = min(cfg.reconnect_max, delay * 2)
    finally:
        await client.aclose()


async def _session(cfg: Config, client: ManagementClient) -> None:
    headers = {
        "Authorization": f"Bearer {cfg.client_key}",
        "X-CPA-Client-Name": cfg.client_name,
    }
    async with connect(
        cfg.server_url,
        additional_headers=headers,
        ping_interval=20,
        ping_timeout=20,
        max_size=1_048_576,
        open_timeout=10,
        close_timeout=10,
    ) as websocket:
        logger.info("已连接 %s，名称=%s", cfg.server_url, cfg.client_name)
        inflight: set[str] = set()
        async for raw in websocket:
            if isinstance(raw, bytes):
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            req_id = str(message.get("id") or "")
            if req_id and req_id in inflight:
                continue
            if req_id:
                inflight.add(req_id)
            try:
                reply = await handle_envelope(cfg, client, message)
                if reply is not None:
                    await websocket.send(json.dumps(reply, ensure_ascii=False))
            finally:
                inflight.discard(req_id)
