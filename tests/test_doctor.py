"""``watcher-mcp doctor`` (#8): diagnostica sin modificar nada, con ficheros y un `claude` falsos."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from watcher_mcp import doctor
from watcher_mcp.cli import dispatch, parse_args


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME falso: nada del usuario real entra en juego."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("WATCHER_MCP_CONFIG", raising=False)
    monkeypatch.delenv("WATCHER_MCP_STATE_DIR", raising=False)
    return h


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def good_config(tmp_path: Path, home: Path) -> Path:
    work = home / "trabajo"
    work.mkdir(exist_ok=True)
    return write_config(tmp_path, f'allowed_roots = ["{work}"]\nstate_dir = "{tmp_path / "state"}"\n')


def run_doctor(capsys, *argv: str) -> tuple[int, str]:
    code = dispatch(parse_args(["doctor", *argv]))
    return code, capsys.readouterr().out


def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str) -> Path:
    """`claude` falso en PATH: apunta cada invocación a un log y devuelve ``listing`` para `mcp list`."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "claude-calls.log"
    (tmp_path / "listing.txt").write_text(listing, encoding="utf-8")
    script = bindir / "claude"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\ncat "{tmp_path / "listing.txt"}"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def console_script(path: Path, module: str) -> Path:
    """Script de consola como el de pip: shebang + ``from <módulo> import main``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport sys\nfrom {module} import main\nsys.exit(main())\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def snapshot(*paths: Path) -> dict[str, tuple[str, float]]:
    out = {}
    for root in paths:
        for p in [root, *root.rglob("*")] if root.exists() else []:
            if p.is_file():
                out[str(p)] = (p.read_text(errors="replace"), p.stat().st_mtime)
    return out


# --------------------------------------------------------------------------- configuración
def test_good_config_passes(capsys, tmp_path, home):
    code, out = run_doctor(capsys, "--config", str(good_config(tmp_path, home)), "--no-server", "--no-clients")
    assert code == 0, out
    assert "[ok   ] configuración: TOML válido" in out
    assert "[ok   ] allowed_roots" in out and "[ok   ] state_dir" in out
    assert "[ok   ] backends: pip: disponible" in out
    assert "0 problema(s)" in out or "Todo en orden" in out


def test_broken_toml_fails_with_an_actionable_message(capsys, tmp_path, home):
    cfg = write_config(tmp_path, "allowed_roots = [\n")
    code, out = run_doctor(capsys, "--config", str(cfg), "--no-server", "--no-clients")
    assert code == 1
    assert "TOML no válido" in out and str(cfg) in out and "arreglo: corrige la sintaxis" in out
    assert "servidor" not in out or "se omite" in out


def test_unknown_key_and_missing_file_fail(capsys, tmp_path, home):
    code, out = run_doctor(capsys, "--config", str(write_config(tmp_path, "clave_que_no_existe = 1\n")), "--no-server",
                           "--no-clients")
    assert code == 1 and "clave_que_no_existe" in out
    code, out = run_doctor(capsys, "--config", str(tmp_path / "no-existe.toml"), "--no-server", "--no-clients")
    assert code == 1 and "no existe el fichero de configuración" in out


def test_invalid_run_rules_and_unknown_backend_fail(capsys, tmp_path, home):
    cfg = write_config(tmp_path, 'allowed_backends = ["pip", "fantasma"]\n[backends.run]\ncommands = [{ program = "a/b" }]\n')
    code, out = run_doctor(capsys, "--config", str(cfg), "--no-server", "--no-clients")
    assert code == 1
    assert "fantasma" in out and "[backends.run] no válido" in out


def test_no_config_file_uses_defaults(capsys, tmp_path, home):
    code, out = run_doctor(capsys, "--state-dir", str(tmp_path / "s"), "--no-server", "--no-clients")
    assert code == 0 and "valores por defecto" in out


# --------------------------------------------------------------------------- raíces y state_dir
def test_roots_missing_and_in_protected_zones(capsys, tmp_path, home):
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    hidden = home / ".local" / "bin"
    hidden.mkdir(parents=True)
    cfg = write_config(tmp_path, f'allowed_roots = ["{home}/no-existe", "{agents}", "{hidden}"]\n')
    code, out = run_doctor(capsys, "--config", str(cfg), "--no-server", "--no-clients", "--state-dir", str(tmp_path / "s"))
    assert code == 1
    assert "no existe" in out and "no-existe" in out
    assert "zona protegida" in out and "LaunchAgents" in out
    assert "componente oculto" in out  # ~/.local/bin: aviso, no fallo (puede ser intencionado)
    assert out.count("[FALLO]") == 2 and "[AVISO] allowed_roots" in out


def test_state_dir_not_writable(capsys, tmp_path, home):
    if os.geteuid() == 0:
        pytest.skip("root escribe en cualquier sitio")
    locked = tmp_path / "bloqueado"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        code, out = run_doctor(capsys, "--config", str(good_config(tmp_path, home)), "--state-dir", str(locked / "sub"),
                               "--no-server", "--no-clients")
    finally:
        locked.chmod(0o700)
    assert code == 1 and "no es escribible" in out and "--state-dir" in out


def test_state_dir_that_does_not_exist_yet_is_not_created(capsys, tmp_path, home):
    target = tmp_path / "nuevo" / "estado"
    code, out = run_doctor(capsys, "--config", str(good_config(tmp_path, home)), "--state-dir", str(target),
                           "--no-server", "--no-clients")
    assert code == 0 and "se creará" in out
    assert not target.exists() and not target.parent.exists()


# --------------------------------------------------------------------------- registros de clientes
def test_claude_registrations_broken_missing_and_deprecated(capsys, tmp_path, home, monkeypatch):
    missing = tmp_path / "install-mcp" / ".venv" / "bin" / "install-mcp"  # el caso de la issue #8
    no_module = console_script(tmp_path / "venv-viejo" / "bin" / "install-mcp-old", "install_mcp_que_ya_no_existe.__main__")
    alias = console_script(tmp_path / "venv-nuevo" / "bin" / "install-mcp", "watcher_mcp.__main__")
    good = console_script(tmp_path / "venv-nuevo" / "bin" / "watcher-mcp", "watcher_mcp.__main__")
    log = fake_claude(tmp_path, monkeypatch, "\n".join([
        "Checking MCP server health...",
        "",
        f"install-mcp: {missing} - ✗ Failed to connect",
        f"watcher-viejo: {no_module} --config /x.toml - ✗ Failed to connect",
        f"alias: {alias} - ✓ Connected",
        f"watcher-mcp: {good} - ✓ Connected",
        "otro-servidor: /no/es/nuestro - ✗ Failed to connect",
        "remoto: https://example.org/mcp (HTTP) - ✓ Connected",
    ]) + "\n")
    cfg = good_config(tmp_path, home)
    before = snapshot(tmp_path / "state", cfg)
    code, out = run_doctor(capsys, "--config", str(cfg), "--no-server", "--claude-desktop-config", str(tmp_path / "nada.json"))
    assert code == 1
    assert f"apunta a un ejecutable que no existe o no es ejecutable: {missing}" in out
    assert "claude mcp remove install-mcp -s user && claude mcp add --scope user watcher-mcp --" in out
    assert "el módulo install_mcp_que_ya_no_existe.__main__ no se puede importar" in out
    assert "usa el nombre obsoleto install-mcp" in out  # el alias funciona pero se avisa
    assert "[ok   ] registro «watcher-mcp»" in out
    assert "otro-servidor" not in out and "remoto" not in out  # lo que no es nuestro no se juzga
    assert "reinicia la sesión" in out
    # Solo diagnostica: `claude` se llamó únicamente con `mcp list`, y no se tocó ningún fichero.
    assert log.read_text().split("\n")[:-1] == ["mcp list"]
    assert snapshot(tmp_path / "state", cfg) == before


def test_claude_not_installed_is_skipped(capsys, tmp_path, home, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))
    findings = doctor.check_claude_cli()
    assert [f.level for f in findings] == ["skip"] and "no está en PATH" in findings[0].message


def test_claude_list_timeout_is_bounded(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    slow = bindir / "claude"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(slow.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    findings = doctor.check_claude_cli(timeout_s=0.5)
    assert findings[0].level == "warn" and "tardó más de 0.5s" in findings[0].message


def test_claude_desktop_json(capsys, tmp_path, home, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))  # sin `claude`: solo el JSON
    good = console_script(tmp_path / "ok" / "bin" / "watcher-mcp", "watcher_mcp.__main__")
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text(json.dumps({"mcpServers": {
        "watcher-mcp": {"command": str(good), "args": ["--config", "/x"]},
        "install-mcp": {"command": str(tmp_path / "borrado" / "install-mcp")},
        "por-modulo": {"command": sys.executable, "args": ["-m", "install_mcp"]},
        "ajeno": {"command": "/no/existe"},
    }}))
    before = desktop.read_text()
    code, out = run_doctor(capsys, "--config", str(good_config(tmp_path, home)), "--no-server",
                           "--claude-desktop-config", str(desktop))
    assert code == 1
    assert "[ok   ] registro «watcher-mcp»" in out
    assert "ejecutable que no existe" in out and "borrado" in out
    assert "módulo install_mcp no se puede importar" in out
    assert "ajeno" not in out
    assert "edita" in out and "reinicia Claude Desktop" in out
    assert desktop.read_text() == before  # nunca reescribe la configuración del cliente


def test_claude_desktop_invalid_json(capsys, tmp_path, home, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))
    desktop = tmp_path / "d.json"
    desktop.write_text("{ no es json")
    code, out = run_doctor(capsys, "--config", str(good_config(tmp_path, home)), "--no-server",
                           "--claude-desktop-config", str(desktop))
    assert code == 1 and "no es un JSON válido" in out


def test_json_output(capsys, tmp_path, home):
    code, out = run_doctor(capsys, "--config", str(good_config(tmp_path, home)), "--no-server", "--no-clients", "--json")
    data = json.loads(out)
    assert code == 0 and data["ok"] is True
    assert {f["check"] for f in data["findings"]} >= {"configuración", "allowed_roots", "state_dir", "backends"}


def test_reregister_command_quotes_and_keeps_args():
    cmd = doctor.reregister_command("install-mcp", ["--config", "/ruta con espacios/c.toml"])
    assert cmd.startswith("claude mcp remove install-mcp -s user && claude mcp add --scope user watcher-mcp -- ")
    assert "'/ruta con espacios/c.toml'" in cmd
