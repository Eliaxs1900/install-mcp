"""Punto de entrada: ``install-mcp [--config RUTA] [--state-dir DIR]`` (transporte stdio)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import Config
from .server import create_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="install-mcp", description="Servidor MCP de instalaciones en segundo plano")
    parser.add_argument("--config", help="Fichero TOML de configuración")
    parser.add_argument("--state-dir", help="Directorio de jobs/logs/venvs (por defecto ~/.local/state/install-mcp)")
    args = parser.parse_args(argv)

    # stdout es el canal MCP: cualquier log del servidor va a stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    config = Config.load(args.config)
    if args.state_dir:
        config.state_dir = Path(args.state_dir).expanduser()
    mcp, manager = create_server(config)

    async def run() -> None:
        try:
            await mcp.run_stdio_async()
        finally:
            # Al cerrar el cliente no se dejan procesos de instalación huérfanos.
            await manager.shutdown()

    asyncio.run(run())


if __name__ == "__main__":
    main()
