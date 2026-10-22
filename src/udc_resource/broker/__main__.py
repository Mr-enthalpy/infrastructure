"""Broker process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..config import load_config
from .docker_driver import DockerDriver
from .server import Broker


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = Path(os.environ.get("UDC_RESOURCE_CONFIG", "/etc/udc-resource/config.toml"))
    config = load_config(config_path)
    broker = Broker(config, docker=DockerDriver(config))
    try:
        asyncio.run(broker.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
