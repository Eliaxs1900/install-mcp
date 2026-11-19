"""``watcher-mcp doctor``: diagnostica la instalación sin modificar nada.

Comprueba, y por cada problema da el comando o el cambio concreto para arreglarlo:

* que el servidor arranca y responde a ``initialize`` (se lanza a sí mismo por stdio, con un
  ``state_dir`` temporal y un timeout acotado);
* que el TOML es válido (claves, reglas ``[backends.run]``, plugins);
* que las ``allowed_roots`` existen y no están en zonas protegidas;
* que el ``state_dir`` es escribible (sin crearlo);
* qué backends están disponibles;
* los registros de ``claude mcp list`` (si ``claude`` está instalado) y del JSON de Claude Desktop que
  apunten a ejecutables inexistentes o a módulos que no se pueden importar, y los que siguen usando el
  nombre antiguo ``install-mcp``.

Nunca ejecuta ``claude mcp add/remove`` ni escribe en configuraciones: solo lee y dice qué ejecutar.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backends import load_registry
from .config import Config, default_config_path
from .security import ensure_safe_path

SERVER_TIMEOUT_S = 15.0
CLAUDE_LIST_TIMEOUT_S = 30.0
IMPORT_TIMEOUT_S = 20.0
RELEVANT = re.compile(r"watcher[-_]mcp|install[-_]mcp")
LEGACY_NAMES = ("install-mcp", "install_mcp")


@dataclass
class Finding:
    check: str
    level: str  # ok | warn | error | skip
    message: str
    fix: str | None = None


# --------------------------------------------------------------------------- comandos de reparación
def self_executable() -> str:
    """Ruta (ya entrecomillada para la shell) con la que registrar ESTA instalación."""
    candidates = [Path(os.path.abspath(sys.argv[0])).parent / "watcher-mcp", Path(sys.executable).parent / "watcher-mcp"]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return shlex.quote(str(candidate))
    if found := shutil.which("watcher-mcp"):
        return shlex.quote(found)
    return f"{shlex.quote(sys.executable)} -m watcher_mcp"


def reregister_command(old_name: str | None, extra_args: list[str] | None = None) -> str:
    """El comando exacto para (re)registrar el servidor en Claude Code, quitando el registro roto."""
    add = f"claude mcp add --scope user watcher-mcp -- {self_executable()}"
    if extra_args:
        add += " " + " ".join(shlex.quote(a) for a in extra_args)
    if old_name:
        return f"claude mcp remove {old_name} -s user && {add}"
    return add


def legacy_notice(args: list[str]) -> str:
    return (
        "install-mcp: AVISO: el comando 'install-mcp' está obsoleto (el proyecto se llama ahora watcher-mcp) y "
        "se eliminará en una versión futura. Sigue sirviendo con normalidad, pero vuelve a registrarlo:\n"
        f"  {reregister_command('install-mcp', args)}\n"
        "(si lo registraste en otro ámbito, cambia '-s user' por '-s local' o '-s project'). Las sesiones ya "
        "abiertas no recargan sus servidores MCP: reinícialas para que usen el nombre nuevo. "
        "Diagnóstico: watcher-mcp doctor"
    )


# --------------------------------------------------------------------------- configuración
def _config_path(arg: str | None) -> tuple[Path | None, bool]:
    """(fichero, ¿pedido explícitamente?)."""
    if arg:
        return Path(arg).expanduser(), True
    if env := os.environ.get("WATCHER_MCP_CONFIG"):
        return Path(env).expanduser(), True
    default = default_config_path()
    return (default, False) if default.exists() else (None, False)


def check_config(config_arg: str | None, state_dir_arg: str | None) -> tuple[Config | None, Path | None, list[Finding]]:
    path, explicit = _config_path(config_arg)
    name = "configuración"
    if path is None:
        cfg = Config()
        if state_dir_arg:
            cfg.state_dir = Path(state_dir_arg).expanduser()
        elif env := os.environ.get("WATCHER_MCP_STATE_DIR"):
            cfg.state_dir = Path(env).expanduser()
        return cfg, None, [Finding(name, "ok", f"sin fichero ({default_config_path()} no existe): valores por defecto")]
    try:
        cfg = Config.load(path)
    except FileNotFoundError:
        fix = f"crea {path} (hay un ejemplo en examples/config.example.toml) o quita --config / $WATCHER_MCP_CONFIG"
        return None, path, [Finding(name, "error", f"no existe el fichero de configuración {path}", fix)]
    except tomllib.TOMLDecodeError as exc:
        return None, path, [Finding(name, "error", f"TOML no válido en {path}: {exc}", f"corrige la sintaxis de {path}")]
    except (ValueError, TypeError, OSError) as exc:
        return None, path, [Finding(name, "error", f"configuración no válida en {path}: {exc}",
                                    f"corrige {path} (claves en examples/config.example.toml)")]
    if state_dir_arg:
        cfg.state_dir = Path(state_dir_arg).expanduser()
    findings = [Finding(name, "ok", f"TOML válido: {path}")]
    registry: dict[str, Any] = {}
    try:
        registry = load_registry(cfg.plugins)
    except Exception as exc:  # plugin que no importa o no es un Backend
        findings.append(Finding(name, "error", f"plugins: {exc!r}", f"corrige o quita 'plugins' en {path}"))
        registry = load_registry([])
    unknown = [b for b in cfg.allowed_backends if b not in registry]
    if unknown:
        findings.append(Finding(name, "error", f"allowed_backends nombra backends que no existen: {unknown}",
                                f"quítalos de allowed_backends en {path}; registrados: {sorted(registry)}"))
    if "run" in registry:
        try:
            registry["run"](cfg).rules()
            registry["run"](cfg).inherit_env()
        except ValueError as exc:
            findings.append(Finding(name, "error", f"[backends.run] no válido: {exc}", f"corrige [backends.run] en {path}"))
    return cfg, path, findings


def check_roots(cfg: Config, path: Path | None) -> list[Finding]:
    where = str(path or "la configuración")
    findings: list[Finding] = []
    home = Path.home().resolve()
    for raw in cfg.allowed_roots:
        root = Path(raw).expanduser()
        if not root.exists():
            findings.append(Finding("allowed_roots", "error", f"la raíz {root} no existe",
                                    f"créala o quítala de allowed_roots en {where}"))
            continue
        if not root.is_dir():
            findings.append(Finding("allowed_roots", "error", f"la raíz {root} no es un directorio",
                                    f"quítala de allowed_roots en {where}"))
            continue
        problem = None
        if root.resolve() == home or root.resolve().is_relative_to(home):
            for protect_dots in (False, True):
                try:
                    ensure_safe_path(root, [home], what="raíz", protected_home_dirs=cfg.protected_home_dirs,
                                     protect_dot_paths=protect_dots)
                except ValueError as exc:
                    problem = ("warn" if protect_dots else "error", str(exc).split(" Si de verdad")[0])
                    break
        if problem is None:
            findings.append(Finding("allowed_roots", "ok", f"{root}"))
        elif problem[0] == "error":
            findings.append(Finding("allowed_roots", "error", f"la raíz {root} cae en una zona protegida: {problem[1]}",
                                    f"quítala de allowed_roots en {where} (ahí se ejecuta código al iniciar sesión o "
                                    "viven secretos); usa una subcarpeta de trabajo"))
        else:
            findings.append(Finding("allowed_roots", "warn", f"la raíz {root} pasa por un componente oculto: {problem[1]}",
                                    "si es intencionado (p. ej. ~/.local/bin) ignora este aviso"))
    if not cfg.allowed_roots:
        findings.append(Finding("allowed_roots", "warn", "allowed_roots está vacío: solo se puede escribir en state_dir"))
    return findings


def check_state_dir(cfg: Config) -> Finding:
    state = cfg.state_dir.expanduser()
    probe = state
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        return Finding("state_dir", "error", f"{state}: {probe} no es un directorio",
                       "apunta state_dir (o --state-dir / $WATCHER_MCP_STATE_DIR) a una carpeta normal")
    fix = f"da permisos de escritura a {probe} o elige otro state_dir (--state-dir, $WATCHER_MCP_STATE_DIR, state_dir en el TOML)"
    if not os.access(probe, os.W_OK | os.X_OK):
        return Finding("state_dir", "error", f"{state} no es escribible ({probe} sin permisos de escritura)", fix)
    if probe == state:
        try:  # prueba real (disco de solo lectura, cuotas...): fichero temporal que se borra al acto
            with tempfile.TemporaryFile(dir=state):
                pass
        except OSError as exc:
            return Finding("state_dir", "error", f"no se puede escribir en {state}: {exc}", fix)
        return Finding("state_dir", "ok", f"{state} (escribible)")
    return Finding("state_dir", "ok", f"{state} no existe todavía; se creará dentro de {probe} (escribible)")


def check_backends(cfg: Config) -> list[Finding]:
    try:
        registry = load_registry(cfg.plugins)
    except Exception:
        registry = load_registry([])
    findings = []
    for name, cls in sorted(registry.items()):
        if name not in cfg.allowed_backends:
            findings.append(Finding("backends", "skip", f"{name}: no permitido (allowed_backends)"))
            continue
        ok, why = cls(cfg).availability()
        findings.append(Finding("backends", "ok" if ok else "warn", f"{name}: {'disponible' if ok else 'no disponible'} – {why}",
                                None if ok else f"instala lo que falta o quítalo de allowed_backends ({why})"))
    return findings


# --------------------------------------------------------------------------- el propio servidor
def check_server(config_path: Path | None, timeout_s: float = SERVER_TIMEOUT_S) -> Finding:
    """Lanza ``python -m watcher_mcp`` por stdio y espera la respuesta a ``initialize``."""
    name = "servidor"
    with tempfile.TemporaryDirectory(prefix="watcher-mcp-doctor-") as tmp:
        cmd = [sys.executable, "-m", "watcher_mcp"]
        if config_path:
            cmd += ["--config", str(config_path)]
        # state_dir temporal: el diagnóstico no reclama huérfanos ni deja locks en el real.
        cmd += ["--state-dir", tmp]
        env = {**os.environ, "WATCHER_MCP_STATE_DIR": tmp}
        stderr_path = Path(tmp) / "stderr.txt"
        with open(stderr_path, "w", encoding="utf-8") as err:
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                                        env=env, start_new_session=True)
            except OSError as exc:
                return Finding(name, "error", f"no se pudo lanzar {' '.join(cmd)}: {exc}")
            box: dict[str, Any] = {}

            def read() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == 1:
                        box["response"] = msg
                        return

            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "watcher-mcp-doctor", "version": "1"}}}
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
            except OSError:
                pass  # ya murió: lo explica su stderr
            reader.join(timeout_s)
            try:  # cierre ordenado (EOF), y si no, por la fuerza
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    response = box.get("response")
    tail = f" Salida de error: {stderr[-600:]}" if stderr else ""
    fix = "arranca `watcher-mcp` a mano para ver el fallo; si falta un módulo, reinstala: uv pip install -e ."
    if response is None:
        why = f"no respondió a initialize en {timeout_s:g}s" if reader.is_alive() else "terminó sin responder a initialize"
        return Finding(name, "error", f"el servidor {why}.{tail}", fix)
    if "error" in response:
        return Finding(name, "error", f"initialize devolvió un error: {response['error']}.{tail}", fix)
    info = (response.get("result") or {}).get("serverInfo") or {}
    return Finding(name, "ok", f"responde a initialize ({info.get('name', '?')} {info.get('version', '')}".rstrip() + ")")


# --------------------------------------------------------------------------- registros de clientes
@dataclass
class Registration:
    name: str
    command: str
    args: list[str]
    source: str  # claude mcp list | Claude Desktop (ruta)
    status: str | None = None  # texto de estado de `claude mcp list`

    def relevant(self) -> bool:
        return bool(RELEVANT.search(" ".join([self.name, self.command, *self.args])))

    def text(self) -> str:
        return " ".join([self.command, *self.args])


def parse_claude_list(output: str) -> list[Registration]:
    """Líneas ``nombre: comando args - ✓ Connected`` de `claude mcp list` (las que no encajan se ignoran)."""
    regs = []
    for line in output.splitlines():
        head, sep, status = line.rpartition(" - ")
        name, colon, command = head.partition(": ")
        if not (sep and colon and name.strip() and command.strip()) or re.search(r"\((HTTP|SSE)\)\s*$", command):
            continue
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        if parts and not parts[0].startswith(("http://", "https://")):
            regs.append(Registration(name.strip(), parts[0], parts[1:], "claude mcp list", status.strip()))
    return regs


def desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _resolve_exe(command: str) -> str | None:
    expanded = os.path.expanduser(command)
    if "/" in expanded:
        return expanded if os.path.isfile(expanded) and os.access(expanded, os.X_OK) else None
    return shutil.which(expanded)


def _module_to_import(exe: str, args: list[str]) -> tuple[list[str], str] | None:
    """(intérprete, módulo) con que probar que el registro importa: ``python -m mod``, o el módulo
    del que importa el script de consola (``from watcher_mcp.__main__ import main``)."""
    if "-m" in args and args.index("-m") + 1 < len(args):
        return [exe], args[args.index("-m") + 1]
    try:
        with open(exe, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    text = head.decode("utf-8", errors="replace")
    shebang, _, body = text.partition("\n")
    interp = shlex.split(shebang[2:].strip())
    if interp and os.path.basename(interp[0]) == "env" and len(interp) > 1:
        found = shutil.which(interp[-1])
        interp = [found] if found else []
    # Los scripts de consola de pip empiezan con ``import re``/``import sys`` y luego ``from <paquete> import main``.
    match = re.search(r"(?m)^\s*from\s+([A-Za-z_][\w.]*)\s+import\s", body)
    if not interp or not os.path.exists(interp[0]) or match is None:
        return None
    return interp, match.group(1)


def inspect_registration(reg: Registration) -> list[Finding]:
    check = f"registro «{reg.name}» ({reg.source})"
    fix_hint = "reinicia la sesión de Claude Code después (las sesiones abiertas no recargan sus servidores MCP)"
    fix = reregister_command(reg.name if reg.source == "claude mcp list" else None)
    if reg.source != "claude mcp list":
        fix = (f"edita {reg.source}: pon \"command\": \"{self_executable()}\" en «{reg.name}» y reinicia Claude Desktop")
    else:
        fix += f"   # {fix_hint}; ámbito distinto de user: -s local|project (`claude mcp get {reg.name}`)"
    exe = _resolve_exe(reg.command)
    if exe is None:
        return [Finding(check, "error", f"apunta a un ejecutable que no existe o no es ejecutable: {reg.command}", fix)]
    findings: list[Finding] = []
    probe = _module_to_import(exe, reg.args)
    if probe is not None:
        interp, module = probe
        try:
            res = subprocess.run([*interp, "-c", "import importlib, sys; importlib.import_module(sys.argv[1])", module],
                                 capture_output=True, text=True, timeout=IMPORT_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            findings.append(Finding(check, "warn", f"no se pudo comprobar que importa {module}: {exc}"))
        else:
            if res.returncode != 0:
                last = (res.stderr.strip().splitlines() or ["?"])[-1]
                return [Finding(check, "error", f"existe {exe}, pero el módulo {module} no se puede importar ({last})", fix)]
    if reg.name in LEGACY_NAMES or os.path.basename(exe) in LEGACY_NAMES:
        findings.append(Finding(check, "warn", "usa el nombre obsoleto install-mcp (funciona, pero desaparecerá)", fix))
    elif reg.status and re.search(r"(?i)fail|error|not connected|✗", reg.status):
        findings.append(Finding(check, "error", f"`claude mcp list` la da como no conectada: {reg.status}",
                                f"ejecuta `{reg.text()}` a mano y mira su stderr; después, {fix_hint}"))
    if not findings:
        extra = f" ({reg.status})" if reg.status else ""
        findings.append(Finding(check, "ok", f"{exe}{extra}"))
    return findings


def check_claude_cli(timeout_s: float = CLAUDE_LIST_TIMEOUT_S) -> list[Finding]:
    name = "claude mcp list"
    claude = shutil.which("claude")
    if claude is None:
        return [Finding(name, "skip", "`claude` no está en PATH: se omite")]
    try:
        res = subprocess.run([claude, "mcp", "list"], capture_output=True, text=True, timeout=timeout_s,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [Finding(name, "warn", f"`claude mcp list` tardó más de {timeout_s:g}s: se omite")]
    except OSError as exc:
        return [Finding(name, "warn", f"no se pudo ejecutar `claude mcp list`: {exc}")]
    if res.returncode != 0:
        return [Finding(name, "warn", f"`claude mcp list` salió con código {res.returncode}: {res.stderr.strip()[-300:]}")]
    regs = [r for r in parse_claude_list(res.stdout) if r.relevant()]
    if not regs:
        return [Finding(name, "skip", "ningún registro de watcher-mcp/install-mcp en `claude mcp list`",
                        f"para registrarlo: {reregister_command(None)}")]
    return [f for reg in regs for f in inspect_registration(reg)]


def check_claude_desktop(path: Path | None) -> list[Finding]:
    name = "Claude Desktop"
    path = path or desktop_config_path()
    if not path.exists():
        return [Finding(name, "skip", f"no existe {path}: se omite")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Finding(name, "error", f"{path} no es un JSON válido: {exc}", f"corrígelo o restáuralo y reinicia Claude Desktop")]
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return [Finding(name, "skip", f"{path} no tiene mcpServers")]
    regs = []
    for server_name, spec in servers.items():
        if isinstance(spec, dict) and isinstance(spec.get("command"), str):
            args = [str(a) for a in spec.get("args", []) if isinstance(a, (str, int))]
            reg = Registration(server_name, spec["command"], args, str(path))
            if reg.relevant():
                regs.append(reg)
    if not regs:
        return [Finding(name, "skip", f"ningún registro de watcher-mcp/install-mcp en {path}")]
    return [f for reg in regs for f in inspect_registration(reg)]


# --------------------------------------------------------------------------- orquestación
def run_doctor(
    config_arg: str | None = None,
    state_dir_arg: str | None = None,
    *,
    check_server_too: bool = True,
    check_clients: bool = True,
    desktop_config: str | None = None,
) -> list[Finding]:
    cfg, path, findings = check_config(config_arg, state_dir_arg)
    config_ok = cfg is not None
    if cfg is None:
        cfg = Config()
        if state_dir_arg:
            cfg.state_dir = Path(state_dir_arg).expanduser()
        findings.append(Finding("allowed_roots", "skip", "se omite: la configuración no es válida"))
    else:
        findings += check_roots(cfg, path)
    findings.append(check_state_dir(cfg))
    if config_ok:
        findings += check_backends(cfg)
    if check_server_too:
        if config_ok:
            findings.append(check_server(path))
        else:
            findings.append(Finding("servidor", "skip", "se omite: la configuración no es válida (arréglala primero)"))
    if check_clients:
        findings += check_claude_cli()
        findings += check_claude_desktop(Path(desktop_config).expanduser() if desktop_config else None)
    return findings


_MARK = {"ok": "ok   ", "warn": "AVISO", "error": "FALLO", "skip": "-    "}


def doctor_command(args: argparse.Namespace) -> int:
    findings = run_doctor(
        getattr(args, "config", None), getattr(args, "state_dir", None),
        check_server_too=not args.no_server, check_clients=not args.no_clients,
        desktop_config=args.claude_desktop_config,
    )
    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warn"]
    if args.json:
        print(json.dumps({"ok": not errors, "findings": [asdict(f) for f in findings]}, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"[{_MARK[f.level]}] {f.check}: {f.message}")
            if f.fix and f.level in ("warn", "error"):
                print(f"        arreglo: {f.fix}")
            elif f.fix and f.level == "skip":
                print(f"        {f.fix}")
        print()
        print(f"{len(errors)} problema(s), {len(warnings)} aviso(s)." if errors or warnings else "Todo en orden.")
    return 1 if errors else 0
