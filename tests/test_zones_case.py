"""Las zonas protegidas de HOME no dependen de las mayúsculas (APFS no las distingue)."""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher_mcp.config import Config
from watcher_mcp.security import ensure_safe_path


@pytest.mark.parametrize("rel", [
    "library/launchagents/evil.plist",
    "LIBRARY/LaunchAgents/evil.plist",
    "Library/keychains/login.keychain-db",
    "Library/application support/com.apple.backgroundtaskmanagementagent/backgrounditems.btm",
])
def test_protected_zones_ignore_case(tmp_path: Path, rel: str):
    zones = Config().protected_home_dirs
    with pytest.raises(ValueError, match="zona"):
        ensure_safe_path(tmp_path / rel, [tmp_path], home=tmp_path, protected_home_dirs=zones)


def test_case_does_not_block_unrelated_paths(tmp_path: Path):
    zones = Config().protected_home_dirs
    ok = tmp_path / "library" / "application support" / "Claude" / "scratch-workspaces" / "p"
    assert ensure_safe_path(ok, [tmp_path], home=tmp_path, protected_home_dirs=zones) == ok.resolve()
