"""watcher-mcp: lanza y vigila trabajos largos (instalaciones, descargas, builds, tests) por MCP."""

from .config import Config
from .jobs import Job, JobManager, JobState
from .server import create_server

__all__ = ["Config", "Job", "JobManager", "JobState", "create_server"]
__version__ = "0.2.0"
