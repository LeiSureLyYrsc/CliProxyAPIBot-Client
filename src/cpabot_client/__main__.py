from __future__ import annotations

import asyncio
import logging

from .config import Config
from .connection import run_forever


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    if not cfg.client_key:
        raise SystemExit("未配置 CLIENT_KEY")
    if not cfg.cpa_management_key:
        raise SystemExit("未配置 CPA_MANAGEMENT_KEY")
    asyncio.run(run_forever(cfg))


if __name__ == "__main__":
    main()
