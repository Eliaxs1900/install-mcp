"""Modo CLI: usar el motor de jobs sin cliente MCP.

``watcher-mcp run [opciones] -- cmd args…`` lanza el comando con el mismo núcleo que el servidor
(``JobManager``, ``process.py``): mismos límites, mismo ``io_mode``, misma escalada SIGTERM -> SIGKILL
sobre el grupo de procesos y mismo log en ``<state_dir>/jobs/<id>/``. ``status``, ``logs`` y ``list``
leen ese mismo directorio en SOLO LECTURA (no reclaman huérfanos ni borran nada), así que ven también
los jobs de un servidor MCP en marcha. ``doctor`` vive en ``doctor.py``.

Códigos de salida de ``run``:

* el del comando, si falla (una muerte por señal N sale como 128+N);
* 124 si se agota el tiempo (``--timeout``), igual que ``timeout(1)``;
* 130 si se cancela (Ctrl-C o SIGTERM al propio watcher-mcp);
* 127 si no se puede lanzar el programa (no existe) y 126 si no es ejecutable;
* 2 por error de uso o de configuración.

Seguridad: ``run`` NO aplica la allowlist ``[backends.run]`` ni ``allowed_roots`` (la lanza una persona o
un agente desde su shell, que ya pasa por sus propios permisos); con ``--strict`` sí. Nunca usa shell.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

from .backends.base import Backend, CommandStep, Plan, Probe
from .backends.common import LimitParams
from .backends.system import RunBackend
from .config import Config
from .jobs import ACTIVE, TERMINAL, Job, JobError, JobEvent, JobManager, JobState, LogLine
from .security import Redactor

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127

SUBCOMMANDS = ("run", "status", "logs", "list", "doctor")
PREFIX = "watcher-mcp: "


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- argumentos
def _non_negative(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} no es un número") from None
    if number < 0:
        raise argparse.ArgumentTypeError("no puede ser negativo")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} no es un entero") from None
    if number < 1:
        raise argparse.ArgumentTypeError("debe ser al menos 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    # Las opciones globales también se aceptan tras el subcomando: allí su valor por defecto es
    # SUPPRESS para que no pisen lo que se pasó antes (``--config X run …``).
    def common(p: argparse.ArgumentParser, *, suppress: bool) -> None:
        default = argparse.SUPPRESS if suppress else None
        p.add_argument("--config", default=default, help="Fichero TOML de configuración")
        p.add_argument("--state-dir", default=default,
                       help="Directorio de jobs/logs/venvs (por defecto ~/.local/state/watcher-mcp)")

    parser = argparse.ArgumentParser(
        prog="watcher-mcp",
        description="Servidor MCP que lanza y vigila trabajos largos. Sin subcomando arranca el servidor "
                    "(stdio); con subcomando funciona como CLI sobre el mismo núcleo.",
    )
    common(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", metavar="{run,status,logs,list,doctor}")

    run = sub.add_parser(
        "run", help="lanza un comando vigilado: watcher-mcp run [opciones] -- cmd args…",
        description="Ejecuta un comando con salida en vivo, aviso de atasco, timeout e informe final. "
                    "El comando va tras '--'. Códigos de salida: el del comando; 124 tiempo agotado; "
                    "130 cancelado; 127 no se puede lanzar; 2 error de uso.",
    )
    common(run, suppress=True)
    run.add_argument("--idle", type=_non_negative, metavar="S",
                     help="segundos sin salida para avisar de atasco (0 = sin vigilante; por defecto el de la configuración)")
    run.add_argument("--timeout", type=_non_negative, metavar="S",
                     help="tiempo máximo del comando; al vencer SIGTERM y luego SIGKILL al grupo (0 = hasta el tope duro)")
    run.add_argument("--cwd", metavar="DIR", help="directorio de trabajo (por defecto, el actual)")
    run.add_argument("--io-mode", choices=("pipe", "pty", "stdbuf"), dest="io_mode",
                     help="cómo se lee la salida (pty/stdbuf para programas C que bufferizan)")
    run.add_argument("--quiet", "-q", action="store_true", help="no muestra la salida en vivo, solo los avisos de atasco")
    run.add_argument("--json", action="store_true",
                     help="imprime el informe como JSON en stdout (la salida en vivo pasa a stderr)")
    run.add_argument("--strict", action="store_true",
                     help="aplica la allowlist [backends.run] y allowed_roots de la configuración, como el servidor MCP")

    status = sub.add_parser("status", help="estado de un job (en solo lectura)")
    common(status, suppress=True)
    status.add_argument("job", help="id del job (o un prefijo único)")
    status.add_argument("--tail", type=int, default=10, metavar="N", help="últimas N líneas del log (por defecto 10)")
    status.add_argument("--json", action="store_true", help="salida JSON")

    logs = sub.add_parser("logs", help="últimas líneas del log de un job")
    common(logs, suppress=True)
    logs.add_argument("job", help="id del job (o un prefijo único)")
    logs.add_argument("--tail", type=_positive_int, default=50, metavar="N", help="últimas N líneas (por defecto 50)")
    logs.add_argument("--stream", choices=("all", "stdout", "stderr", "core"), default="all")

    lst = sub.add_parser("list", help="jobs recientes (en solo lectura)")
    common(lst, suppress=True)
    lst.add_argument("--state", dest="job_state", choices=[s.value for s in JobState], help="filtra por estado")
    lst.add_argument("--limit", type=_positive_int, default=20, metavar="N")
    lst.add_argument("--json", action="store_true", help="salida JSON")

    doctor = sub.add_parser(
        "doctor", help="diagnostica la instalación y los registros; no modifica nada",
        description="Comprueba que el servidor arranca y responde a initialize, la configuración, las raíces, "
                    "el state_dir, los backends y los registros de Claude Code / Claude Desktop. "
                    "Sale con código distinto de 0 si hay algún problema. Solo diagnostica: dice qué ejecutar.",
    )
    common(doctor, suppress=True)
    doctor.add_argument("--json", action="store_true", help="salida JSON")
    doctor.add_argument("--no-server", action="store_true", help="no lanza el servidor para probar initialize")
    doctor.add_argument("--no-clients", action="store_true", help="no mira `claude mcp list` ni Claude Desktop")
    doctor.add_argument("--claude-desktop-config", metavar="FICHERO",
                        help="JSON de Claude Desktop (por defecto el de la plataforma)")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Analiza la línea de comandos. Sin subcomando, ``args.command`` es None (hay que servir).

    El comando de ``run`` va tras ``--`` y no pasa por argparse: así no se confunden sus opciones con
    las de watcher-mcp ni se pierden argumentos que parezcan opciones."""
    argv = list(sys.argv[1:] if argv is None else argv)
    head, tail, has_sep = argv, [], False
    if "--" in argv:
        i = argv.index("--")
        head, tail, has_sep = argv[:i], argv[i + 1:], True
    parser = build_parser()
    args = parser.parse_args(head)
    if args.command == "run":
        if not tail:
            parser.error("falta el comando: watcher-mcp run [opciones] -- comando args…")
        args.cmd = tail
    elif has_sep:
        parser.error("'--' solo se usa con el subcomando run")
    return args


def load_config(args: argparse.Namespace) -> Config:
    """Misma resolución que el servidor: ``--config`` > $WATCHER_MCP_CONFIG > ~/.config; ``--state-dir`` manda."""
    config = Config.load(getattr(args, "config", None))
    if getattr(args, "state_dir", None):
        config.state_dir = Path(args.state_dir).expanduser()
    return config


def dispatch(args: argparse.Namespace) -> int:
    """Ejecuta un subcomando y devuelve el código de salida del proceso."""
    if args.command == "doctor":
        from .doctor import doctor_command

        return doctor_command(args)
    try:
        config = load_config(args)
    except Exception as exc:  # TOML roto, fichero inexistente, claves desconocidas...
        eprint(f"{PREFIX}no se pudo cargar la configuración: {exc}. Compruébalo con `watcher-mcp doctor`.")
        return EXIT_USAGE
    if args.command == "run":
        try:
            return asyncio.run(run_command_cli(args, config))
        except KeyboardInterrupt:  # Ctrl-C antes de instalar los manejadores o tras cerrarlos
            return EXIT_CANCELLED
    if args.command == "status":
        return status_command(args, config)
    if args.command == "logs":
        return logs_command(args, config)
    return list_command(args, config)


# --------------------------------------------------------------------------- run
class CliRunParams(LimitParams):
    program: str
    args: list[str] = []
    cwd: str
    io_mode: Literal["pipe", "pty", "stdbuf"] | None = None


class CliRunBackend(Backend[CliRunParams]):
    """``run`` sin allowlist: el comando lo escribe quien invoca la CLI (ver la nota de seguridad del módulo)."""

    name = "run"
    description = "Comando arbitrario lanzado desde la CLI de watcher-mcp (sin allowlist)."
    Params = CliRunParams

    def check(self, spec: CliRunParams) -> None:
        if not spec.program or "\x00" in spec.program or any("\x00" in a for a in spec.args):
            raise ValueError("programa o argumentos no válidos (vacío o con NUL)")
        if not Path(spec.cwd).is_dir():
            raise ValueError(f"cwd no existe o no es un directorio: {spec.cwd!r}")

    def plan(self, spec: CliRunParams) -> Plan:
        cwd = Path(spec.cwd).resolve()
        program = spec.program
        if "/" in program and not os.path.isabs(program):
            # ``./gradlew`` es relativo a --cwd (donde se ejecuta), no al directorio desde el que se lanzó.
            program = os.path.normpath(cwd / program)
        shown = Redactor(self.config.redact_patterns).argv([spec.program, *spec.args])  # ``--password X`` va en dos argumentos
        summary = f"{' '.join(shown)[:80]} (en {cwd})"
        return Plan(
            summary=summary,
            steps=[CommandStep(Path(spec.program).name, [program, *spec.args], cwd=str(cwd), io_mode=spec.io_mode)],
            **spec.limits(),
        )

    async def detect_versions(self, spec: CliRunParams, probe: Probe) -> dict[str, str]:
        return {}  # no se vuelve a ejecutar el comando con --version


class StrictRunBackend(RunBackend):
    """El backend ``run`` del servidor (allowlist, reglas, allowed_roots) sin la sonda ``--version`` final."""

    async def detect_versions(self, spec: Any, probe: Probe) -> dict[str, str]:
        return {}


def exit_code_for(job: Job) -> int:
    """Código de salida de ``run`` para un job terminado (ver la tabla del módulo)."""
    if job.state == JobState.SUCCEEDED:
        return 0
    if job.state == JobState.TIMED_OUT:
        return EXIT_TIMEOUT
    if job.state == JobState.CANCELLED:
        return EXIT_CANCELLED
    code = job.exit_code
    if code is not None and code != 0:
        return 128 - code if code < 0 else min(code, 255)
    error = job.error or ""
    if "Permission denied" in error:
        return EXIT_NOT_EXECUTABLE
    if "no se encuentra" in error or "No such file" in error:
        return EXIT_NOT_FOUND
    return 1


@dataclass
class _Shown:
    quiet: bool
    json_mode: bool

    @property
    def out(self) -> IO[str]:
        return sys.stderr if self.json_mode else sys.stdout


async def _follow_log(path: Path, shown: _Shown, stop: asyncio.Event) -> None:
    """Muestra el log del job a medida que se escribe (stdout del comando a stdout, el resto a stderr)."""
    pos, buf = 0, b""

    def drain() -> None:
        nonlocal pos, buf
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
        except OSError:
            return
        pos += len(data)
        buf += data
        while b"\n" in buf:
            raw, _, buf = buf.partition(b"\n")
            line = LogLine.parse(0, raw.decode("utf-8", errors="replace"))
            if line.stream == "stdout":
                print(line.text, file=shown.out, flush=True)
            elif line.stream == "stderr":
                print(line.text, file=sys.stderr, flush=True)

    while not stop.is_set():
        drain()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), 0.05)
    drain()


def _format_report(job: Job) -> list[str]:
    report = job.report or {}
    out = [report.get("headline") or f"[{job.id}] {job.state}"]
    out.append(f"  log: {job.log_path}")
    if job.stalls:
        out.append(f"  atascos detectados: {job.stalls}")
    if report.get("hint"):
        out.append(f"  pista: {report['hint']}")
    if not report.get("ok", job.state == JobState.SUCCEEDED):
        lines = report.get("relevant_lines") or [
            ln for ln in report.get("log_tail", []) if "| " in ln and " core " not in ln[:40]
        ][-10:]
        if lines:
            out.append("  líneas relevantes:" if report.get("relevant_lines") else "  últimas líneas:")
            out += [f"    {ln}" for ln in lines]
    return out


async def run_command_cli(args: argparse.Namespace, config: Config) -> int:
    cwd = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    cwd = cwd.resolve()
    config.allowed_backends = [*config.allowed_backends, "run"] if "run" not in config.allowed_backends else config.allowed_backends
    backend_cls = StrictRunBackend if args.strict else CliRunBackend
    params: dict[str, Any] = {"program": args.cmd[0], "args": args.cmd[1:], "cwd": str(cwd)}
    if args.io_mode:
        params["io_mode"] = args.io_mode
    if args.timeout is not None:
        params["job_timeout_s"] = args.timeout
    if args.idle is not None:
        params["stall_timeout_s"] = args.idle

    manager = JobManager(config, {"run": backend_cls})
    shown = _Shown(args.quiet, args.json)
    finished = asyncio.Event()
    forced = False
    job_box: list[Job] = []
    signals_seen = 0
    main_task = asyncio.current_task()

    async def on_event(event: JobEvent) -> None:
        if event.kind == "started":
            eprint(f"{PREFIX}job {event.job.id} · log {event.job.log_path}")
        elif event.kind in ("stalled", "resumed"):
            eprint(f"{PREFIX}{event.message}")
        elif event.kind == "finished":
            finished.set()

    def on_signal() -> None:
        nonlocal signals_seen, forced
        signals_seen += 1
        if signals_seen == 1:
            eprint(f"\n{PREFIX}cancelando (SIGTERM al grupo de procesos y luego SIGKILL; otra señal lo fuerza)…")
            if job_box:
                job_box[0].add_line("core", "cancelación solicitada (señal)")
                job_box[0].cancel_event.set()
        else:
            forced = True
            if main_task is not None:
                main_task.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, on_signal)
    follower: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    try:
        manager.load_history()
        manager.listeners.append(on_event)
        try:
            job = await manager.submit("run", params)
        except JobError as exc:
            eprint(f"{PREFIX}{exc}")
            return EXIT_USAGE
        job_box.append(job)
        if signals_seen:  # Ctrl-C mientras se preparaba el job
            job.cancel_event.set()
        if not shown.quiet:
            follower = asyncio.create_task(_follow_log(job.log_path, shown, stop))
        await job.done.wait()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(finished.wait(), 5)
        stop.set()
        if follower is not None:
            await follower
        code = exit_code_for(job)
        if args.json:
            print(json.dumps({**(job.report or {}), "cli_exit_code": code}, ensure_ascii=False, indent=2), flush=True)
        else:
            for line in _format_report(job):
                eprint(line)
        return code
    except asyncio.CancelledError:
        if not forced:
            raise
        eprint(f"{PREFIX}forzado: SIGKILL al grupo de procesos")
        return EXIT_CANCELLED
    finally:
        stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
        if follower is not None and not follower.done():
            follower.cancel()
        await manager.shutdown(timeout_s=0.2 if forced else 10)


# --------------------------------------------------------------------------- lectura (status/logs/list)
def _owner_alive(config: Config, owner: str | None) -> bool:
    """True si la instancia ``owner`` sigue viva (mantiene su flock). Solo lectura: no borra nada."""
    if not owner or fcntl is None:
        return False
    try:
        with open(config.instances_dir / f"{owner}.lock", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def load_jobs(config: Config) -> list[Job]:
    """Jobs del ``state_dir`` sin tocar el disco. Los que seguían en curso en otra instancia viva
    quedan como ``foreign`` (solo lectura) y los de una que murió se muestran como interrumpidos
    (el servidor los recoge al arrancar; aquí no se termina ningún proceso)."""
    jobs: list[Job] = []
    if not config.jobs_dir.is_dir():
        return jobs
    redactor = Redactor(config.redact_patterns)
    for directory in config.jobs_dir.iterdir():
        job = Job.from_disk(directory, redactor, config.memory_log_lines) if directory.is_dir() else None
        if job is None:
            continue
        if job.state not in TERMINAL:
            if _owner_alive(config, job.owner):
                job.foreign = True
            else:
                job.state = JobState.FAILED
                job.error = job.error or "interrumpido: el proceso que lo ejecutaba terminó mientras estaba en curso"
        jobs.append(job)
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)


def find_job(jobs: list[Job], wanted: str) -> Job:
    wanted = wanted.strip().removeprefix("job://")
    exact = [j for j in jobs if j.id == wanted]
    matches = exact or [j for j in jobs if wanted and j.id.startswith(wanted)]
    if not matches:
        raise JobError(f"No existe el job {wanted!r} en este state_dir (mira `watcher-mcp list`)")
    if len(matches) > 1:
        raise JobError(f"El prefijo {wanted!r} es ambiguo: {', '.join(j.id for j in matches)}")
    return matches[0]


def _state_label(job: Job) -> str:
    return f"{job.state} (en curso en otra sesión, solo lectura)" if job.foreign else str(job.state)


def status_command(args: argparse.Namespace, config: Config) -> int:
    try:
        job = find_job(load_jobs(config), args.job)
    except JobError as exc:
        eprint(f"{PREFIX}{exc}")
        return 1
    tail = max(args.tail, 0)
    if args.json:
        data = job.status(tail=tail)
        if job.report:
            data["report"] = job.report
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    status = job.status()
    print(f"job {job.id} ({job.backend}) – {_state_label(job)}")
    print(f"  resumen:  {job.summary}")
    if job.started_at:
        extra = f", código {job.exit_code}" if job.exit_code is not None else ""
        print(f"  duración: {job.duration()}s{extra}")
    if job.state in ACTIVE:
        print(f"  pid: {job.pid} · sin salida desde hace {status.get('silent_for_s')}s")
    if job.error:
        print(f"  error:    {job.error}")
    print(f"  log:      {job.log_path} ({status['log_lines']} líneas)")
    if job.report and job.report.get("headline"):
        print(f"  informe:  {job.report['headline']}")
    if tail:
        lines = job.tail(tail)
        if lines:
            print(f"  últimas {len(lines)} líneas:")
            for ln in lines:
                print(f"    {ln.format()}")
    return 0


def logs_command(args: argparse.Namespace, config: Config) -> int:
    try:
        job = find_job(load_jobs(config), args.job)
    except JobError as exc:
        eprint(f"{PREFIX}{exc}")
        return 1
    for line in job.tail(args.tail, stream=args.stream):
        print(line.format())
    return 0


def list_command(args: argparse.Namespace, config: Config) -> int:
    jobs = load_jobs(config)
    if args.job_state:
        jobs = [j for j in jobs if j.state == args.job_state]
    jobs = jobs[: args.limit]
    if args.json:
        rows = [{"job_id": j.id, "backend": j.backend, "state": str(j.state), "foreign": j.foreign,
                 "summary": j.summary, "exit_code": j.exit_code, "duration_s": j.duration(),
                 "created_at": j.status()["created_at"], "log_file": str(j.log_path)} for j in jobs]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not jobs:
        print(f"(sin jobs en {config.jobs_dir})")
        return 0
    for j in jobs:
        dur = f"{j.duration():.1f}s" if j.duration() is not None else "-"
        state = f"{j.state}*" if j.foreign else str(j.state)
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(j.created_at))
        print(f"{j.id}  {created}  {state:<10} {dur:>8}  {j.summary[:70]}")
    if any(j.foreign for j in jobs):
        print("* en curso en otra sesión (solo lectura)")
    return 0
