from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence

from app.config.loader import ConfigError, load_config
from app.db.connection import open_database
from app.db.migrations import migrate
from app.runtime.daemon import AgentController

logger = logging.getLogger(__name__)


async def _serve(controller: AgentController) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, controller.request_stop)
        except NotImplementedError:
            break  # Windows: rely on KeyboardInterrupt
    await controller.run_forever()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-controller")
    parser.add_argument("--config", default="config.yaml")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="validate config and migrate DB")
    group.add_argument("--once", action="store_true", help="start, run one cleanup, stop")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        if args.check:
            conn = open_database(
                config.database.path, busy_timeout_ms=config.database.busy_timeout_ms
            )
            try:
                version = migrate(conn)
            finally:
                conn.close()
            print(f"config OK; schema version {version}; database {config.database.path}")
            return 0
        controller = AgentController(config)
        if args.once:
            asyncio.run(controller.run_once())
        else:
            asyncio.run(_serve(controller))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.exception("fatal error")
        return 1
