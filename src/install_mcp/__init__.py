"""install-mcp: instalaciones como jobs en segundo plano expuestas por MCP."""

from .config import Config
from .jobs import Job, JobManager, JobState
from .server import create_server

__all__ = ["Config", "Job", "JobManager", "JobState", "create_server"]
__version__ = "0.1.0"
