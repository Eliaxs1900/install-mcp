"""watcher-mcp: lanza y vigila trabajos largos (instalaciones, descargas, builds, tests) por MCP."""

# Antes de los imports: ``server`` lo lee al cargarse (``from . import __version__``).
__version__ = "0.4.0"

from .config import Config
from .jobs import Job, JobManager, JobState
from .server import create_server

__all__ = ["Config", "Job", "JobManager", "JobState", "create_server"]
