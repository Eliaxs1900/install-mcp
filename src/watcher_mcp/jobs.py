"""Núcleo: jobs en segundo plano, logs persistentes, límites y eventos.

Independiente del instalador (los backends solo aportan un ``Plan``) y de MCP
(los avisos salen como ``JobEvent`` hacia listeners; el servidor MCP es uno de ellos).

Ninguna espera es infinita:

* cola: ``max_queue_s``;
* ejecución: timeout global del job (``job_timeout_s``) y, opcionalmente, por paso;
* atasco: watchdog que marca ``stalled`` si no hay salida en ``stall_timeout_s``;
* terminación: SIGTERM -> espera acotada -> SIGKILL -> espera acotada;
* sondas de versión, listeners (notificaciones) y ``wait``: con timeout.

Disco, por job, en ``<state_dir>/jobs/<id>/``:

* ``output.log``  – una línea por evento: ``<ISO-8601> <stream> | <texto>`` (escritura
  línea a línea, se puede seguir con ``tail -f``).
* ``job.json``    – estado actual (se reescribe en cada transición).
* ``report.json`` – informe final.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from .backends.base import Backend, CommandStep, FuncStep, Plan, ProbeResult, StepContext
from .config import Config
from .process import build_env, run_capture, run_command
from .security import Redactor

try:  # bloqueo entre procesos para saber qué instancias del servidor siguen vivas
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STALLED = "stalled"  # en ejecución pero sin salida desde hace stall_timeout_s
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.TIMED_OUT})
ACTIVE = frozenset({JobState.RUNNING, JobState.STALLED})

# Líneas que suelen explicar un fallo; se destacan en el informe.
RELEVANT = re.compile(
    r"(?i)\b(error|failed|failure|fatal|exception|traceback|denied|not found|no matching|"
    r"could not|cannot|unable|conflict|timed? ?out|no solution|e:)\b"
)
DETECT_TIMEOUT_S = 120.0


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def first(*values: Any) -> Any:
    """Primer valor que no es None (0 cuenta como valor: desactiva)."""
    return next((v for v in values if v is not None), None)


@dataclass
class LogLine:
    seq: int
    ts: float
    stream: str  # stdout | stderr | core
    text: str

    def format(self) -> str:
        return f"{iso(self.ts)} {self.stream:<6} | {self.text}"

    @classmethod
    def parse(cls, seq: int, raw: str) -> LogLine:
        head, _, text = raw.rstrip("\n").partition(" | ")
        ts_s, _, stream = head.partition(" ")
        try:
            ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0
        return cls(seq, ts, stream.strip() or "?", text)


@dataclass
class StepRecord:
    name: str
    state: str = "pending"  # pending | running | ok | failed | skipped | cancelled | timed_out
    exit_code: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    command: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "state": self.state, "exit_code": self.exit_code}
        if self.started_at and self.finished_at:
            d["duration_s"] = round(self.finished_at - self.started_at, 3)
        if self.command:
            d["command"] = self.command
        return d


@dataclass
class Limits:
    job_timeout_s: float | None
    stall_timeout_s: float | None
    io_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {"job_timeout_s": self.job_timeout_s, "stall_timeout_s": self.stall_timeout_s, "io_mode": self.io_mode}


@dataclass
class JobEvent:
    kind: str  # queued | started | step | stalled | resumed | finished
    job: Job
    level: str  # info | warning | error (niveles de notifications/message)
    message: str
    data: dict[str, Any] = field(default_factory=dict)


Listener = Callable[[JobEvent], Awaitable[None]]


class Job:
    def __init__(
        self,
        job_id: str,
        backend: str,
        params: dict[str, Any],
        plan: Plan,
        directory: Path,
        redactor: Redactor,
        memory_lines: int,
        limits: Limits | None = None,
        owner: str | None = None,
    ) -> None:
        self.id = job_id
        # Instancia del servidor que lo ejecuta (cada sesión de Claude Code arranca la suya).
        self.owner = owner
        # True si se cargó del disco y sigue en curso en OTRA instancia viva: solo lectura.
        self.foreign = False
        self.backend = backend
        self.params = params
        self.summary = plan.summary
        self.lock_key = plan.lock_key
        self.limits = limits or Limits(None, None, "pipe")
        self.state = JobState.QUEUED
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self.installed: dict[str, str] = {}
        self.report: dict[str, Any] | None = None
        self.steps = [StepRecord(s.name) for s in plan.steps]
        self.current_step: str | None = None
        self.current_command: list[str] | None = None
        self.step_stall_s: float | None = None  # umbral de atasco del paso en curso
        self.pid: int | None = None
        self.last_output_at: float | None = None
        self.stalls = 0
        self.timed_out_reason: str | None = None
        self.dir = directory
        self.redact = redactor
        self._lines: deque[LogLine] = deque(maxlen=memory_lines)
        self._seq = 0
        self._fh: IO[str] | None = None
        self.done = asyncio.Event()
        self.cancel_event = asyncio.Event()
        self.lines_in_step = 0

    # --- logs -------------------------------------------------------------
    @property
    def uri(self) -> str:
        return f"job://{self.id}"

    @property
    def log_path(self) -> Path:
        return self.dir / "output.log"

    @property
    def last_seq(self) -> int:
        return self._seq

    def open_log(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.log_path, "a", buffering=1, encoding="utf-8")

    def close_log(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def add_line(self, stream: str, text: str, *, activity: bool | None = None) -> None:
        """Añade una línea. stdout/stderr (o ``activity=True``) cuentan para el watchdog."""
        now = time.time()
        self._seq += 1
        if activity if activity is not None else stream != "core":
            self.last_output_at = now
            self.lines_in_step += 1
        line = LogLine(self._seq, now, stream, self.redact(text))
        self._lines.append(line)
        if self._fh:
            self._fh.write(line.format() + "\n")

    def tail(self, n: int, since_seq: int | None = None, stream: str | None = None) -> list[LogLine]:
        lines: list[LogLine] = list(self._lines)
        oldest_mem = lines[0].seq if lines else self._seq + 1
        need_disk = (
            (since_seq is not None and since_seq + 1 < oldest_mem)
            or (since_seq is None and n > len(lines) and oldest_mem > 1)
            or (not lines and self.log_path.exists())
        )
        if need_disk and self.log_path.exists():
            # Recorrido en streaming quedándose solo con las últimas n que encajan: memoria O(n)
            # aunque el log ocupe gigas (antes se cargaba el fichero entero en cada consulta).
            selected: deque[LogLine] = deque(maxlen=max(n, 0))
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                for i, raw in enumerate(fh, start=1):
                    if since_seq is not None and i <= since_seq:
                        continue
                    if n <= 0:
                        continue
                    line = LogLine.parse(i, raw)
                    if stream and stream != "all" and line.stream != stream:
                        continue
                    selected.append(line)
            return list(selected)
        if since_seq is not None:
            lines = [ln for ln in lines if ln.seq > since_seq]
        if stream and stream != "all":
            lines = [ln for ln in lines if ln.stream == stream]
        return lines[-n:] if n > 0 else []

    # --- estado -----------------------------------------------------------
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        return round((self.finished_at or time.time()) - self.started_at, 3)

    def silent_for(self) -> float | None:
        if self.state not in ACTIVE or self.last_output_at is None:
            return None
        return round(time.time() - self.last_output_at, 1)

    def progress(self) -> tuple[float, float]:
        """(progreso, total) monótono: pasos completados + fracción del paso según líneas."""
        total = float(max(len(self.steps), 1))
        done = sum(1 for s in self.steps if s.state not in ("pending", "running"))
        if self.state in TERMINAL:
            return total, total
        frac = 1 - 1 / (1 + self.lines_in_step / 20)
        return min(done + frac * 0.99, total), total

    def stall_info(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "command": self.current_command,
            "step": self.current_step,
            "silent_for_s": self.silent_for(),
            "stall_timeout_s": self.step_stall_s,
            "log_tail": [ln.format() for ln in self.tail(15)],
        }

    def status(self, tail: int = 0) -> dict[str, Any]:
        d: dict[str, Any] = {
            "job_id": self.id,
            "uri": self.uri,
            "backend": self.backend,
            "summary": self.summary,
            "state": str(self.state),
            "current_step": self.current_step,
            "steps": [s.to_dict() for s in self.steps],
            "exit_code": self.exit_code,
            "error": self.error,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "duration_s": self.duration(),
            "last_output_at": iso(self.last_output_at),
            "limits": self.limits.to_dict(),
            "stalls": self.stalls,
            "log_lines": self._seq,
            "log_file": str(self.log_path),
            "params": self.params,
            "owner": self.owner,
        }
        if self.foreign:
            d["foreign"] = True
            d["note"] = "en curso en otra sesión del servidor (solo lectura desde aquí)"
        if self.state in ACTIVE:
            d.update(pid=self.pid, command=self.current_command, silent_for_s=self.silent_for())
        if self.installed:
            d["installed"] = self.installed
        if tail:
            d["log_tail"] = [ln.format() for ln in self.tail(tail)]
        return d

    def persist(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(self.status(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "job.json")

    @classmethod
    def from_disk(cls, directory: Path, redactor: Redactor, memory_lines: int) -> Job | None:
        """Carga un job persistido tal cual. Nunca lanza: un job.json ilegible o de otra versión
        se omite con un aviso en vez de impedir que arranque el servidor."""
        try:
            return cls._from_disk(directory, redactor, memory_lines)
        except Exception as exc:
            log.warning("job en %s omitido: no se pudo cargar (%r)", directory, exc)
            return None

    @classmethod
    def _from_disk(cls, directory: Path, redactor: Redactor, memory_lines: int) -> Job | None:
        try:
            data = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        plan = Plan(summary=data.get("summary", ""), steps=[])
        job = cls(data["job_id"], data["backend"], data.get("params", {}), plan, directory, redactor, memory_lines,
                  owner=data.get("owner"))
        job.pid = data.get("pid")
        job.current_command = data.get("command")
        job.steps = [StepRecord(s["name"], s.get("state", "?"), s.get("exit_code")) for s in data.get("steps", [])]
        job.exit_code = data.get("exit_code")
        job.error = data.get("error")
        job.installed = data.get("installed", {})
        job.stalls = data.get("stalls", 0)

        def ts(key: str) -> float | None:
            v = data.get(key)
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() if v else None

        job.created_at = ts("created_at") or 0.0
        job.started_at, job.finished_at = ts("started_at"), ts("finished_at")
        try:
            job.state = JobState(data["state"])
        except ValueError:
            job.state = JobState.FAILED
        if job.log_path.exists():
            with open(job.log_path, encoding="utf-8", errors="replace") as fh:
                job._seq = sum(1 for _ in fh)
        with contextlib.suppress(OSError, ValueError):
            job.report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        if job.state in TERMINAL:
            job.done.set()
        return job


class JobError(ValueError):
    """Error de uso (job inexistente, backend no permitido...). Se devuelve tal cual al cliente."""


class JobManager:
    def __init__(self, config: Config, registry: Mapping[str, type[Backend[Any]]]) -> None:
        self.config = config
        self.registry = dict(registry)
        self.redactor = Redactor(config.redact_patterns)
        self.jobs: dict[str, Job] = {}
        self.listeners: list[Listener] = []
        self._sem = asyncio.Semaphore(max(1, config.max_parallel))
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._backends: dict[str, Backend[Any]] = {}
        # Cada servidor (uno por sesión de Claude Code) comparte state_dir con los demás: se
        # identifica con un id y un flock que mantiene mientras vive, para que los otros sepan
        # si sus jobs siguen en curso o quedaron huérfanos.
        self.instance_id = secrets.token_hex(4)
        self._instance_fh: IO[str] | None = None
        self._register_instance()

    # --- instancias -----------------------------------------------------------
    def _instance_lock(self, instance_id: str) -> Path:
        return self.config.instances_dir / f"{instance_id}.lock"

    def _register_instance(self) -> None:
        if fcntl is None:
            return
        try:
            self.config.instances_dir.mkdir(parents=True, exist_ok=True)
            fh = open(self._instance_lock(self.instance_id), "w", encoding="utf-8")  # noqa: SIM115
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            self._instance_fh = fh
        except OSError as exc:
            log.warning("no se pudo registrar la instancia en %s: %r", self.config.instances_dir, exc)

    def _release_instance(self) -> None:
        if self._instance_fh is not None:
            with contextlib.suppress(OSError):
                self._instance_lock(self.instance_id).unlink()
                self._instance_fh.close()
            self._instance_fh = None

    def owner_alive(self, owner: str | None) -> bool:
        """True si la instancia ``owner`` sigue viva (mantiene su flock)."""
        if not owner or fcntl is None:
            return False
        if owner == self.instance_id:
            return True
        path = self._instance_lock(owner)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            return False
        with contextlib.suppress(OSError):
            path.unlink()  # el dueño murió sin limpiar
        return False

    def _reap_orphan(self, job: Job) -> None:
        """Termina el grupo de procesos de un job cuya instancia murió de golpe (kill -9, crash).

        Los procesos arrancan en su propio grupo (start_new_session), así que no reciben SIGHUP
        cuando muere el servidor y seguirían corriendo sin vigilancia. Antes de matar se
        comprueba con ``ps`` que el pid sigue siendo el mismo programa (un pid se puede reutilizar).
        """
        pid, command = job.pid, job.current_command
        if not pid or not command or not hasattr(os, "killpg"):
            return
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return
        program = Path(command[0]).name
        if out.returncode != 0 or program not in out.stdout:
            return  # ya no existe, o el pid es ahora de otro programa
        job.add_line("core", f"proceso huérfano {pid} ({program}) de una instancia que murió: terminando su grupo")
        for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, sig)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                try:
                    os.killpg(pid, 0)
                except (ProcessLookupError, PermissionError):
                    return
                time.sleep(0.1)

    # --- backends -----------------------------------------------------------
    def backend(self, name: str) -> Backend[Any]:
        if name not in self.config.allowed_backends:
            raise JobError(
                f"Backend {name!r} no permitido. Permitidos: {self.config.allowed_backends} "
                "(configurable con allowed_backends)"
            )
        if name not in self.registry:
            raise JobError(f"Backend {name!r} no existe. Registrados: {sorted(self.registry)}")
        if name not in self._backends:
            self._backends[name] = self.registry[name](self.config)
        return self._backends[name]

    def describe_backends(self) -> list[dict[str, Any]]:
        out = []
        for name, cls in sorted(self.registry.items()):
            allowed = name in self.config.allowed_backends
            item: dict[str, Any] = {"name": name, "description": cls.description, "allowed": allowed}
            if allowed:
                backend = self.backend(name)
                ok, why = backend.availability()
                item.update(
                    available=ok,
                    availability=why,
                    default_limits=self.limits(backend, Plan("", [])).to_dict(),
                    params_schema=cls.params_schema(),
                )
            out.append(item)
        return out

    def limits(self, backend: Backend[Any], plan: Plan) -> Limits:
        """Prioridad: Plan (petición) > [backends.<n>] en config > backend > global."""
        opts = backend.options
        job_t = first(plan.timeout_s, opts.get("job_timeout_s"), backend.default_job_timeout_s,
                      self.config.default_job_timeout_s)
        stall_t = first(plan.stall_timeout_s, opts.get("stall_timeout_s"), backend.default_stall_timeout_s,
                        self.config.default_stall_timeout_s)
        io_mode = first(opts.get("io_mode"), backend.default_io_mode, self.config.default_io_mode)
        if io_mode not in ("pipe", "pty", "stdbuf"):
            raise JobError(f"io_mode no válido: {io_mode!r} (pipe | pty | stdbuf)")
        job_t = job_t or None
        cap = self.config.max_job_timeout_s or None
        if cap:  # ninguna espera es infinita: "sin límite" (0) significa "hasta el tope duro"
            job_t = min(job_t, cap) if job_t else cap
        return Limits(job_t, stall_t or None, io_mode)

    # --- historial ------------------------------------------------------------
    def load_history(self) -> None:
        if not self.config.jobs_dir.exists():
            return
        found = []
        for d in self.config.jobs_dir.iterdir():
            if d.is_dir() and (job := Job.from_disk(d, self.redactor, self.config.memory_log_lines)):
                found.append(job)
        for job in sorted(found, key=lambda j: j.created_at):
            if job.state not in TERMINAL:
                if job.owner and self.owner_alive(job.owner):
                    job.foreign = True  # sigue en curso en otra sesión: no se toca
                else:
                    self._reap_orphan(job)
                    job.state = JobState.FAILED
                    job.error = "interrumpido: el servidor que lo ejecutaba terminó mientras estaba en curso"
                    job.finished_at = job.finished_at or time.time()
                    job.add_line("core", job.error)
                    with contextlib.suppress(OSError):
                        job.persist()
                    job.done.set()
            self.jobs.setdefault(job.id, job)

    def _prune(self) -> None:
        alive: dict[str, bool] = {}

        def prunable(j: Job) -> bool:
            if j.foreign or j.state not in TERMINAL:
                return False
            if j.owner in (None, self.instance_id):
                return True
            if j.owner not in alive:
                alive[j.owner] = self.owner_alive(j.owner)
            return not alive[j.owner]  # nunca se borra la carpeta de un job de otra sesión viva

        finished = [j for j in self.jobs.values() if prunable(j)]
        excess = len(finished) - self.config.max_jobs_kept
        for job in sorted(finished, key=lambda j: j.created_at)[: max(excess, 0)]:
            self.jobs.pop(job.id, None)
            shutil.rmtree(job.dir, ignore_errors=True)

    # --- consultas --------------------------------------------------------------
    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id.strip().removeprefix("job://"))
        if job is None:
            raise JobError(f"No existe el job {job_id!r}")
        return job

    def list(self, state: str | None = None, limit: int = 20) -> list[Job]:
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        if state:
            jobs = [j for j in jobs if j.state == state]
        return jobs[: max(limit, 0)]

    # --- ciclo de vida ------------------------------------------------------------
    async def submit(
        self,
        backend_name: str,
        params: Mapping[str, Any],
        *,
        on_created: Callable[[Job], None] | None = None,
    ) -> Job:
        """Valida, planifica y lanza el job en segundo plano. Vuelve al instante.

        ``on_created`` se llama antes del primer evento (el servidor lo usa para
        registrar qué conexión es la dueña del job y avisarla desde ``queued``).
        """
        backend = self.backend(backend_name)
        available, why = backend.availability()
        if not available:
            raise JobError(f"Backend {backend_name!r} no disponible en esta máquina: {why}")
        try:
            spec = backend.validate(params)
        except ValueError as exc:  # incluye pydantic.ValidationError
            raise JobError(f"Parámetros inválidos para {backend_name}: {self.redactor(str(exc))}") from exc
        if backend.requires_privileges(spec) and not self.config.allow_privileged:
            raise JobError(
                f"El backend {backend_name!r} necesita privilegios para esta operación y la "
                "configuración no lo permite (allow_privileged = false)"
            )
        plan = backend.plan(spec)
        if any(isinstance(s, CommandStep) and s.privileged for s in plan.steps) and not self.config.allow_privileged:
            raise JobError(f"El plan de {backend_name!r} incluye pasos privilegiados y allow_privileged = false")
        limits = self.limits(backend, plan)
        redactor = self.redactor.with_literals(plan.secrets)
        job_id = secrets.token_hex(6)
        safe_params = redactor.redact_obj(spec.model_dump(mode="json", exclude_none=True))
        job = Job(
            job_id, backend_name, safe_params, plan, self.config.jobs_dir / job_id, redactor,
            self.config.memory_log_lines, limits, owner=self.instance_id,
        )
        job.open_log()
        job.add_line("core", f"job {job_id} creado: {job.summary}")
        job.add_line("core", f"límites: {json.dumps(limits.to_dict())}, máximo en cola {self.config.max_queue_s}s")
        job.persist()
        self.jobs[job_id] = job
        if on_created:
            on_created(job)
        self._tasks[job_id] = asyncio.create_task(self._run(job, backend, spec, plan), name=f"job-{job_id}")
        await self._emit(JobEvent("queued", job, "info", f"[{job_id}] en cola: {job.summary}"))
        return job

    async def wait(self, job_id: str, timeout_s: float) -> bool:
        """Espera acotada. Devuelve True si el job terminó. Nunca espera más de ``timeout_s``."""
        job = self.get(job_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(job.done.wait()), max(timeout_s, 0))
        return job.done.is_set()

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.foreign:
            raise JobError(f"El job {job.id} se está ejecutando en otra sesión del servidor; cancélalo desde allí")
        if job.state not in TERMINAL:
            job.add_line("core", "cancelación solicitada")
            job.cancel_event.set()
        return job

    async def shutdown(self, timeout_s: float = 10) -> None:
        """Cancela los jobs activos (al cerrar el servidor) y espera, acotado, a que terminen."""
        for job in self.jobs.values():
            if job.state not in TERMINAL:
                job.cancel_event.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        try:
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=timeout_s)
                for t in pending:
                    t.cancel()
        finally:
            self._release_instance()

    # --- ejecución ------------------------------------------------------------------
    async def _emit(self, event: JobEvent) -> None:
        for listener in list(self.listeners):
            try:
                await asyncio.wait_for(listener(event), self.config.listener_timeout_s)
            except TimeoutError:
                event.job.add_line("core", f"aviso: un listener tardó más de {self.config.listener_timeout_s}s")
            except Exception as exc:  # un listener roto no debe romper el job
                event.job.add_line("core", f"aviso: listener falló: {exc!r}")

    async def _acquire(self, job: Job, primitive: asyncio.Semaphore | asyncio.Lock, deadline: float) -> str:
        """'ok' | 'cancelled' | 'timeout'. Nunca espera más allá de ``deadline``."""
        acq = asyncio.create_task(primitive.acquire())
        cancel = asyncio.create_task(job.cancel_event.wait())
        done, _ = await asyncio.wait(
            {acq, cancel}, timeout=max(deadline - time.monotonic(), 0), return_when=asyncio.FIRST_COMPLETED
        )
        cancel.cancel()
        if acq in done and not job.cancel_event.is_set():
            return "ok"
        if acq in done:
            primitive.release()
        else:
            acq.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await acq
            if acq.done() and not acq.cancelled():  # lo obtuvo justo al cancelar
                primitive.release()
        return "cancelled" if job.cancel_event.is_set() else "timeout"

    def _probe(self, job: Job) -> Callable[..., Awaitable[ProbeResult]]:
        async def probe(
            argv: list[str], env: Mapping[str, str] | None = None, timeout_s: float = 60, cwd: str | None = None
        ) -> ProbeResult:
            job.add_line("core", "$ " + " ".join(job.redact.argv(argv)) + "   (sonda de versión)")
            try:
                code, out, err = await run_capture(argv, env=build_env(env or {}), cwd=cwd, timeout_s=timeout_s)
            except OSError as exc:
                job.add_line("core", f"sonda fallida: {exc}")
                return ProbeResult(-1, "", str(exc))
            if code != 0:
                job.add_line("core", f"sonda terminó con código {code}: {job.redact(err.strip()[-500:])}")
            return ProbeResult(code, out, err)

        return probe

    async def _watchdog(self, job: Job, deadline: float | None) -> None:
        """Timeout global del job y detección de atascos. Vive mientras el job corre."""
        while True:
            stall = job.step_stall_s
            tick = min(1.0, max(0.05, stall / 4)) if stall else 1.0
            await asyncio.sleep(tick)
            now = time.monotonic()
            if deadline is not None and now >= deadline and not job.cancel_event.is_set():
                job.timed_out_reason = f"superó el tiempo máximo del job ({job.limits.job_timeout_s}s)"
                job.add_line("core", f"timeout: {job.timed_out_reason}; terminando (SIGTERM y luego SIGKILL)")
                job.cancel_event.set()
            if job.cancel_event.is_set() or job.last_output_at is None:
                continue
            silent = time.time() - job.last_output_at
            if stall and job.state == JobState.RUNNING and silent >= stall:
                job.state = JobState.STALLED
                job.stalls += 1
                cmd = " ".join(job.current_command or []) or job.current_step
                job.add_line("core", f"atasco: sin salida desde hace {silent:.0f}s (pid {job.pid}, paso "
                             f"{job.current_step!r}, comando: {cmd})", activity=False)
                job.persist()
                await self._emit(
                    JobEvent("stalled", job, "warning",
                             f"[{job.id}] ATASCADO: sin salida desde hace {silent:.0f}s en el paso "
                             f"{job.current_step!r} (pid {job.pid})", job.stall_info())
                )
            elif job.state == JobState.STALLED and stall and silent < stall:
                job.state = JobState.RUNNING
                job.add_line("core", "la salida se ha reanudado", activity=False)
                job.persist()
                await self._emit(JobEvent("resumed", job, "info", f"[{job.id}] se reanuda la salida"))

    async def _run_command(self, job: Job, rec: StepRecord, step: CommandStep) -> str:
        """Devuelve 'ok' | 'failed' | 'cancelled' | 'timed_out'."""
        argv = list(step.argv)
        if step.privileged and not (hasattr(os, "geteuid") and os.geteuid() == 0):
            argv = [*self.config.privilege_command, *argv]
        exe = shutil.which(argv[0]) if not Path(argv[0]).is_absolute() else argv[0]
        if not exe:
            job.error = f"no se encuentra el ejecutable {argv[0]!r} en PATH"
            job.add_line("core", job.error)
            return "failed"
        argv[0] = exe
        rec.command = job.current_command = job.redact.argv(argv)
        io_mode = step.io_mode or job.limits.io_mode
        job.add_line("core", "$ " + " ".join(rec.command) + (f"   [io: {io_mode}]" if io_mode != "pipe" else ""))

        def on_start(pid: int) -> None:
            job.pid = pid
            job.add_line("core", f"pid {pid}", activity=True)
            with contextlib.suppress(OSError):
                job.persist()  # pid y comando en disco: otra instancia podrá recoger el huérfano

        try:
            res = await run_command(
                argv,
                env=build_env(step.env, step.env_remove),
                cwd=step.cwd,
                on_line=job.add_line,
                cancel_event=job.cancel_event,
                timeout_s=step.timeout_s,
                on_start=on_start,
                io_mode=io_mode,
            )
        except OSError as exc:
            job.error = f"no se pudo lanzar {argv[0]!r}: {exc}"
            job.add_line("core", job.error)
            return "failed"
        rec.exit_code = res.exit_code
        job.exit_code = res.exit_code
        if res.abandoned:
            job.add_line("core", f"el proceso {res.pid} no terminó ni con SIGKILL; se abandona")
        if res.cancelled:
            return "timed_out" if job.timed_out_reason else "cancelled"
        if res.timed_out:
            job.timed_out_reason = f"el paso {step.name!r} superó su tiempo máximo ({step.timeout_s}s)"
            job.add_line("core", f"timeout: {job.timed_out_reason}; proceso terminado")
            return "timed_out"
        if res.exit_code not in step.ok_exit_codes:
            job.error = f"el paso {step.name!r} terminó con código {res.exit_code}"
            job.add_line("core", job.error)
            return "failed"
        return "ok"

    async def _run_func(self, job: Job, step: FuncStep) -> str:
        loop = asyncio.get_running_loop()

        def log(message: str) -> None:  # seguro también desde hilos (descargas en to_thread)
            loop.call_soon_threadsafe(functools.partial(job.add_line, "core", message, activity=True))

        ctx = StepContext(log=log, cancelled=job.cancel_event.is_set)
        task = asyncio.create_task(step.func(ctx))
        cancel = asyncio.create_task(job.cancel_event.wait())
        try:
            await asyncio.wait({task, cancel}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel.cancel()
        if not task.done():  # cancelado o timeout global mientras corría
            # Un paso que trabaja en un hilo (descargas) no se detiene con task.cancel(): consulta
            # ctx.cancelled entre bloques. Se espera a que termine de verdad (acotado), para no
            # liberar su lock mientras el hilo sigue escribiendo y un reintento empieza en paralelo.
            job.add_line("core", "esperando a que el paso se detenga…")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), self.config.func_cancel_grace_s)
            if not task.done():
                task.cancel()
                job.add_line("core", f"el paso no se detuvo en {self.config.func_cancel_grace_s}s; se abandona")
            return "timed_out" if job.timed_out_reason else "cancelled"
        try:
            task.result()
            return "ok"
        except Exception as exc:
            job.error = f"el paso {step.name!r} falló: {exc}"
            job.add_line("core", job.error)
            return "failed"

    async def _run(self, job: Job, backend: Backend[Any], spec: Any, plan: Plan) -> None:
        lock = self._locks.setdefault(plan.lock_key, asyncio.Lock()) if plan.lock_key else None
        got_sem = got_lock = False
        watchdog: asyncio.Task[None] | None = None
        try:
            queue_deadline = time.monotonic() + self.config.max_queue_s
            outcome = "ok" if lock is None else await self._acquire(job, lock, queue_deadline)
            got_lock = outcome == "ok"
            if got_lock:
                outcome = await self._acquire(job, self._sem, queue_deadline)
                got_sem = outcome == "ok"
            if outcome != "ok":
                if outcome == "timeout":
                    job.state = JobState.TIMED_OUT
                    job.timed_out_reason = f"esperó en cola más de {self.config.max_queue_s}s"
                    job.add_line("core", f"timeout: {job.timed_out_reason}")
                else:
                    job.state = JobState.CANCELLED
                return
            job.state = JobState.RUNNING
            job.started_at = time.time()
            job.last_output_at = job.started_at
            job.persist()
            deadline = time.monotonic() + job.limits.job_timeout_s if job.limits.job_timeout_s else None
            watchdog = asyncio.create_task(self._watchdog(job, deadline))
            await self._emit(JobEvent("started", job, "info", f"[{job.id}] iniciado: {job.summary}"))
            outcome = "ok"
            for rec, step in zip(job.steps, plan.steps, strict=True):
                if job.cancel_event.is_set():
                    outcome = "timed_out" if job.timed_out_reason else "cancelled"
                    break
                if step.skip_if and step.skip_if():
                    rec.state = "skipped"
                    job.add_line("core", f"paso {step.name!r} omitido (no es necesario)")
                    continue
                rec.state, rec.started_at, job.current_step = "running", time.time(), step.name
                job.current_command = None
                job.pid = None
                job.lines_in_step = 0
                job.last_output_at = time.time()
                job.step_stall_s = first(getattr(step, "stall_timeout_s", None), job.limits.stall_timeout_s) or None
                if job.state == JobState.STALLED:
                    job.state = JobState.RUNNING
                job.persist()
                await self._emit(JobEvent("step", job, "info", f"[{job.id}] paso: {step.name}"))
                if isinstance(step, CommandStep):
                    outcome = await self._run_command(job, rec, step)
                else:
                    outcome = await self._run_func(job, step)
                rec.finished_at = time.time()
                rec.state = outcome
                if outcome != "ok":
                    break
            watchdog.cancel()
            job.current_step = None
            job.step_stall_s = None
            if outcome == "ok" and job.cancel_event.is_set():
                outcome = "timed_out" if job.timed_out_reason else "cancelled"
            if outcome == "ok":
                try:
                    job.installed = await asyncio.wait_for(
                        backend.detect_versions(spec, self._probe(job)), DETECT_TIMEOUT_S
                    )
                except TimeoutError:
                    job.add_line("core", f"la detección de versiones superó {DETECT_TIMEOUT_S}s; se omite")
                job.state = JobState.SUCCEEDED
            elif outcome == "timed_out":
                job.state = JobState.TIMED_OUT
            elif outcome == "cancelled":
                job.state = JobState.CANCELLED
                job.error = job.error or "cancelado por el usuario"
            else:
                job.state = JobState.FAILED
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            job.error = "cancelado: el servidor se está cerrando"
            raise
        except Exception as exc:
            job.state = JobState.FAILED
            job.error = f"error interno: {exc!r}"
            job.add_line("core", job.error)
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if got_sem:
                self._sem.release()
            if got_lock and lock is not None:
                lock.release()
            await self._finish(job, backend, spec)

    async def _finish(self, job: Job, backend: Backend[Any], spec: Any) -> None:
        """Cierra el job. Pase lo que pase (disco lleno, informe que falla...), ``done`` se activa:
        si no, ``wait`` nunca lo vería terminar y el job quedaría "en curso" para siempre."""
        try:
            job.finished_at = time.time()
            if job.state == JobState.TIMED_OUT:
                job.error = job.timed_out_reason or "tiempo agotado"
            if job.state in (JobState.CANCELLED, JobState.TIMED_OUT):
                for rec in job.steps:
                    if rec.state in ("pending", "running"):
                        rec.state = "cancelled"
            job.report = self._report(job, backend, spec)
            job.add_line("core", f"job {job.state}: {job.report['headline']}")
            with contextlib.suppress(OSError):
                (job.dir / "report.json").write_text(json.dumps(job.report, ensure_ascii=False, indent=2), "utf-8")
            job.persist()
        except Exception as exc:
            if job.state not in TERMINAL:
                job.state = JobState.FAILED
            job.error = job.error or f"error interno al cerrar el job: {exc!r}"
            job.report = job.report or {"job_id": job.id, "state": str(job.state), "ok": False,
                                        "headline": f"[{job.id}] {job.state} – {job.error}", "error": job.error}
            with contextlib.suppress(Exception):
                job.add_line("core", job.error)
        finally:
            with contextlib.suppress(Exception):
                job.close_log()
            job.done.set()
            self._tasks.pop(job.id, None)
        level = {JobState.SUCCEEDED: "info", JobState.CANCELLED: "warning"}.get(job.state, "error")
        await self._emit(JobEvent("finished", job, level, job.report["headline"], {"report": job.report}))
        with contextlib.suppress(Exception):
            self._prune()

    def _report(self, job: Job, backend: Backend[Any], spec: Any) -> dict[str, Any]:
        ok = job.state == JobState.SUCCEEDED
        dur = job.duration() or 0.0
        if ok:
            pkgs = ", ".join(f"{k} {v}" for k, v in job.installed.items()) or "sin versiones detectadas"
            headline = f"[{job.id}] OK en {dur:.1f}s – {job.summary} ({pkgs})"
        elif job.state == JobState.CANCELLED:
            headline = f"[{job.id}] cancelado tras {dur:.1f}s – {job.summary}"
        elif job.state == JobState.TIMED_OUT:
            headline = f"[{job.id}] TIEMPO AGOTADO tras {dur:.1f}s – {job.error}"
        else:
            headline = f"[{job.id}] FALLÓ (código {job.exit_code}) tras {dur:.1f}s – {job.error}"
        recent = job.tail(400)
        report: dict[str, Any] = {
            "job_id": job.id,
            "uri": job.uri,
            "backend": job.backend,
            "summary": job.summary,
            "state": str(job.state),
            "ok": ok,
            "headline": headline,
            "exit_code": job.exit_code,
            "duration_s": round(dur, 3),
            "started_at": iso(job.started_at),
            "finished_at": iso(job.finished_at),
            "steps": [s.to_dict() for s in job.steps],
            "installed": job.installed,
            "error": job.error,
            "limits": job.limits.to_dict(),
            "stalls": job.stalls,
            "log_file": str(job.log_path),
        }
        with contextlib.suppress(Exception):
            report["info"] = backend.result_info(spec)
        if ok:
            report["log_tail"] = [ln.format() for ln in recent[-5:]]
        else:
            report["relevant_lines"] = [ln.format() for ln in recent if ln.stream != "core" and RELEVANT.search(ln.text)][
                -20:
            ]
            report["log_tail"] = [ln.format() for ln in recent[-25:]]
        return report
