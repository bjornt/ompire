"""Entry point: `ompire-daemon` runs the FastAPI app under uvicorn."""

from __future__ import annotations

import sys

import uvicorn

from ompire_daemon.app import create_app
from ompire_daemon.config import ConfigError, load_config


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ompire-daemon: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    app = create_app(config)
    uvicorn.run(app, host=config.bind, port=config.port)


if __name__ == "__main__":
    main()
