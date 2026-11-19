"""Punto de entrada.

* ``watcher-mcp [--config RUTA] [--state-dir DIR]``: servidor MCP por stdio (así lo registran los clientes).
* ``watcher-mcp run|status|logs|list|doctor …``: modo CLI sobre el mismo núcleo (ver ``cli.py``).
* ``install-mcp``: alias obsoleto (``legacy_main``) del antiguo nombre del proyecto.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .cli import dispatch, parse_args
from .config import Config
from .server import create_server


def serve(config_path: str | None = None, state_dir: str | None = None) -> None:
    """Arranca el servidor MCP por stdio."""
    # stdout es el canal MCP: cualquier log del servidor va a stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    config = Config.load(config_path)
    if state_dir:
        config.state_dir = Path(state_dir).expanduser()
    mcp, manager = create_server(config)

    async def run() -> None:
        try:
            await mcp.run_stdio_async()
        finally:
            # Al cerrar el cliente no se dejan procesos de instalación huérfanos.
            await manager.shutdown()

    asyncio.run(run())


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command is None:
        serve(args.config, args.state_dir)
        return
    raise SystemExit(dispatch(args))


def legacy_main(argv: Sequence[str] | None = None) -> None:
    """Entry point ``install-mcp`` (nombre anterior): avisa por stderr y sirve con normalidad, para no
    tumbar las sesiones que aún lo tienen registrado."""
    from .doctor import legacy_notice

    args = list(sys.argv[1:] if argv is None else argv)
    print(legacy_notice(args), file=sys.stderr, flush=True)
    main(args)


if __name__ == "__main__":
    main()
