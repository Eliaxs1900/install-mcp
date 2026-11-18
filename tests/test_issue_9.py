"""Issue #9: proteger solo lo sensible de ~/Library y explicar cómo permitir una zona."""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher_mcp.config import DEFAULT_PROTECTED_HOME_DIRS, Config
from watcher_mcp.security import ProtectedPathError, ensure_safe_path

SCRATCH = "Library/Application Support/Claude/scratch-workspaces/u1/u2/scratch-2026-11-18/fritz-image-toolkit"


def check(home: Path, rel: str, zones=DEFAULT_PROTECTED_HOME_DIRS) -> Path:
    return ensure_safe_path(home / rel, [home], home=home, protected_home_dirs=zones)


@pytest.mark.parametrize("rel", [SCRATCH, "Library/Application Support/JetBrains/x", "Library/Caches/pip/x",
                                 "Proyectos/app"])
def test_allowed_by_default(tmp_path, rel):
    assert check(tmp_path, rel) == (tmp_path / rel).resolve()


@pytest.mark.parametrize("rel", [
    "Library/LaunchAgents/evil.plist",
    "library/launchagents/evil.plist",            # el disco de macOS no distingue mayúsculas
    "Library/Preferences/com.apple.x.plist",
    "Library/Keychains/login.keychain-db",
    "Library/Containers/com.apple.mail/Data",
    "Library/Application Support/com.apple.backgroundtaskmanagementagent/backgrounditems.btm",
])
def test_sensitive_library_paths_rejected(tmp_path, rel):
    with pytest.raises(ProtectedPathError) as err:
        check(tmp_path, rel)
    assert err.value.zone.is_relative_to(tmp_path / rel.split("/")[0]) or err.value.zone.is_relative_to(tmp_path)


def test_legacy_whole_library_still_works(tmp_path):
    with pytest.raises(ProtectedPathError):
        check(tmp_path, SCRATCH, zones=["Library"])


def test_maven_project_in_claude_scratch_workspace(tmp_path, monkeypatch):
    """El caso real de la issue: un proyecto en la carpeta de sesión de Claude Desktop."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from watcher_mcp.backends.jvm import MavenBackend

    proj = tmp_path / SCRATCH
    proj.mkdir(parents=True)
    (proj / "pom.xml").write_text("<project/>")
    cfg = Config(state_dir=tmp_path / "state", allowed_roots=[tmp_path], backends={"maven": {"mvn_path": "/bin/echo"}})
    MavenBackend(cfg).validate({"project_dir": str(proj), "goals": ["test"], "offline": True})


def test_error_says_where_and_what_to_paste(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config(state_dir=tmp_path / "state", allowed_roots=[tmp_path])
    with pytest.raises(ProtectedPathError) as err:
        cfg.safe_path(tmp_path / "Library" / "LaunchAgents" / "x.plist", what="dest")
    msg = str(err.value)
    assert "~/.config/watcher-mcp/config.toml" in msg and "WATCHER_MCP_CONFIG" in msg
    assert 'allowed_roots = ["~", "~/Library/LaunchAgents"]' in msg

    toml = tmp_path / "w.toml"
    toml.write_text(f'allowed_roots = ["{tmp_path}"]\nstate_dir = "{tmp_path}/s"\n')
    loaded = Config.load(toml)
    with pytest.raises(ProtectedPathError) as err:
        loaded.safe_path(tmp_path / ".ssh" / "authorized_keys", what="dest")
    assert "en ~/w.toml" in str(err.value) and '"~/.ssh"' in str(err.value)
