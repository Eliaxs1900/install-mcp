# install-mcp — documento de diseño

## 1. Problema

Las instalaciones (`pip install`, `git clone`, `apt-get install`, descargas de
firmware…) tardan más que el timeout de una llamada de herramienta (120 s en el
Bash de Claude Code). Lanzadas a mano en segundo plano, la salida llega vacía o con
buffering y no se sabe en qué punto van, si han fallado ni por qué.

`install-mcp` es un servidor MCP que **centraliza cualquier instalación**:

- la lanza como **job en segundo plano** y devuelve un `job_id` al instante;
- guarda su salida **línea a línea, sin buffering**, en memoria y en disco;
- deja consultar estado y logs, esperar de forma acotada y cancelar;
- **avisa al terminar** con un informe (éxito/fallo, código de salida, duración,
  versiones instaladas y, si falló, las líneas relevantes del log);
- **nunca se queda colgado**: toda espera tiene límite (§4), un watchdog avisa en
  cuanto un proceso deja de producir salida (`stalled`) y cada job tiene un timeout
  global (`timed_out`). El cliente no necesita sondear: el aviso le llega por MCP.

Es común a cualquier instalador: el núcleo no sabe nada de pip; cada tipo de
instalador es un **backend** que solo traduce parámetros a comandos.

## 2. Arquitectura

```
 cliente MCP (Claude Code)
        │  tools: install · status · list_jobs · logs · wait · cancel · backends
        │  recursos: job://{id} · jobs://list      notificaciones ▲
        ▼                                                       │
 ┌─────────────── server.py (MCPServer, SDK mcp 2.x) ───────────┴──────┐
 │  traduce tools ⇄ JobManager      notify.py: JobEvent → MCP          │
 └──────────────────────┬──────────────────────────▲───────────────────┘
                        │ submit/get/wait/cancel   │ JobEvent (queued, started, step, finished)
 ┌──────────────────────▼──────────────────────────┴───────────────────┐
 │  jobs.py  — NÚCLEO (no sabe de MCP ni de pip)                        │
 │   cola (semáforo max_parallel) · locks por lock_key · ciclo de vida │
 │   logs (deque en memoria + output.log) · informe · historial        │
 │   process.py: create_subprocess_exec, stdout/stderr en vivo,        │
 │               grupo de procesos, timeout, SIGTERM→SIGKILL           │
 │   security.py: redacción, https, checksums, raíces permitidas       │
 └──────────────────────┬──────────────────────────────────────────────┘
                        │ validate → plan → (ejecuta pasos) → detect_versions
 ┌──────────────────────▼──────────────────────────────────────────────┐
 │  backends/  pip (implementado) · _template (git, ejemplo) · …        │
 └──────────────────────────────────────────────────────────────────────┘
```

| Módulo | Responsabilidad |
|---|---|
| `config.py` | Configuración (TOML + entorno): allowlist, privilegios, paralelismo, raíces, plugins. |
| `jobs.py` | `Job`, `JobManager`, estados, logs, informe, persistencia, eventos. |
| `process.py` | Ejecución sin shell con lectura en vivo y cancelación del grupo de procesos. |
| `security.py` | `Redactor`, `require_https`, `Checksum`, `ensure_within_roots`, `reject_option_like`. |
| `backends/base.py` | Interfaz `Backend`, `Plan`, `CommandStep`, `FuncStep`. |
| `backends/pip.py` | Backend pip/uv con venv aislado. |
| `notify.py` | Puente `JobEvent` → `notifications/message`, `resources/updated`, bus `subscriptions/listen`. |
| `server.py` | Tools y recursos MCP; espera con `notifications/progress`. |

## 3. Ciclo de vida de un job

```
install() ──► validate ──► plan ──► QUEUED ──(lock_key libre y hueco en max_parallel)──► RUNNING ◄──┐
                 │ error              │ cancel → CANCELLED                                  │       │ vuelve
                 ▼                    │ > max_queue_s → TIMED_OUT                           │       │ la salida
          error de tool               ▼                                                    │       │ (resumed)
          (no se crea job)                                       sin salida stall_timeout_s ▼       │
                                                                          STALLED ─────────────────┘
                                   por cada paso: skip_if? → skipped; ejecutar → ok / failed
                                   cancel o timeout → SIGTERM al grupo, 5 s, SIGKILL, 5 s
                                                                                           │
                                   todos ok → detect_versions (≤120 s) → SUCCEEDED         ▼
                                   un paso falla                       → FAILED
                                   cancel                              → CANCELLED
                                   job_timeout_s o timeout de paso     → TIMED_OUT
                                   (siempre) → report.json, job.json, evento "finished"
```

Estados: `queued`, `running`, `stalled` (no terminales) y `succeeded`, `failed`,
`cancelled`, `timed_out` (terminales).

- **Antes de existir el job** se valida todo (allowlist, parámetros, privilegios).
  Un error ahí es un error de la tool, no un job fallido.
- **queued**: el job espera dos cosas, en este orden: el lock de su `lock_key`
  (dos instalaciones en el mismo venv, o dos `apt-get` que comparten el lock de
  dpkg, nunca corren a la vez) y un hueco del semáforo `max_parallel`.
- **running**: los pasos se ejecutan en orden. Un `CommandStep` puede declarar
  `skip_if` (p. ej. "el venv ya existe"), `timeout_s`, `ok_exit_codes`, `env`,
  `env_remove` y `privileged`. Un `FuncStep` ejecuta Python (verificar un
  checksum, descomprimir…) y escribe en el mismo log.
- **Fin**: si todo fue bien, el backend detecta versiones con una *sonda* (comando
  corto con salida capturada). Se construye el informe, se escribe en disco y se
  emite el evento `finished`.
- **stalled**: es un *sub-estado* de `running`; el proceso sigue vivo. Lo marca el
  watchdog (§4) y se notifica con pid, comando, segundos sin salida y últimas líneas.
  Si la salida vuelve, el job regresa a `running` (evento `resumed`). Si nunca vuelve,
  el timeout global lo acaba convirtiendo en `timed_out`.
- **Cierre del servidor**: `shutdown()` cancela los jobs activos para no dejar
  procesos huérfanos. Al arrancar, los jobs que quedaron `running` en disco se
  marcan `failed` ("interrumpido").
- **Retención**: se guardan los últimos `max_jobs_kept` jobs terminados.

### Logs

Cada línea es `<ISO-8601 UTC> <stream> | <texto>` con `stream` ∈ `stdout`,
`stderr`, `core` (mensajes del propio núcleo: comando ejecutado, pasos, errores).

- **Sin buffering**: el núcleo fija `PYTHONUNBUFFERED=1`, `NO_COLOR=1`,
  `TERM=dumb` para todos los pasos; stdout y stderr se leen en streaming, en trozos, y
  se cortan por `\n` **y** `\r`, así que las barras de progreso que reescriben la
  línea también aparecen. Líneas de más de 8 KiB se trocean. Para programas que no
  son Python, el modo de E/S (`io_mode`, por paso, backend o global) elige:

  | `io_mode` | stdout | Cuándo |
  |---|---|---|
  | `pipe` (por defecto) | tubería | Python (con `PYTHONUNBUFFERED`), uv, git, curl… que escriben sin buffer o en stderr |
  | `pty` | pseudo-terminal | Programas C cuya libc acumula buffer cuando stdout no es un terminal (binwalk, unsquashfs, 7z…) |
  | `stdbuf` | `stdbuf -oL -eL` (o `gstdbuf`) + tubería | Igual que `pty` sin crear terminal; si no existe `stdbuf`, se usa `pty` y se anota en el log |

  stderr va siempre por tubería (la libc no lo bufferiza). Un test lo comprueba: un
  programa con buffer por tubería no muestra nada hasta terminar; con `pty`, en vivo.
- **El log nunca está vacío**: antes de que el proceso escriba nada ya contiene la
  creación del job, los límites aplicados, el comando exacto (`$ …`) y el `pid`; si se
  atasca, el watchdog añade una línea `atasco: sin salida desde hace Ns (pid …)`.
- **Persistencia**: `<state_dir>/jobs/<id>/output.log` se escribe línea a línea
  (se puede seguir con `tail -f`). En memoria se guardan las últimas
  `memory_log_lines`; si se piden más, se lee el fichero.
- **Seguimiento incremental**: cada línea tiene un `seq`. `logs(job_id, since_seq=N)`
  devuelve solo lo nuevo; `last_seq` es el cursor para la siguiente llamada.

### Informe (`report`)

```json
{
  "job_id": "c6a4cc23ea3c", "uri": "job://c6a4cc23ea3c", "backend": "pip",
  "state": "succeeded", "ok": true,
  "headline": "[c6a4cc23ea3c] OK en 0.3s – pip install pypdf en …/venvs/demo (vía uv) (pypdf 7.1.1, …)",
  "exit_code": 0, "duration_s": 0.295, "started_at": "…", "finished_at": "…",
  "steps": [{"name": "create-venv", "state": "ok", "exit_code": 0, "duration_s": 0.02, "command": ["…"]},
            {"name": "install", "state": "ok", "exit_code": 0, "duration_s": 0.24, "command": ["…"]}],
  "installed": {"pypdf": "7.1.1", "(python)": "3.12.16"},
  "info": {"venv": "…", "python": "…/bin/python", "installer": "uv", "activate": "source …"},
  "error": null, "log_file": "…/output.log",
  "log_tail": ["…últimas 5 líneas…"]
}
```

También incluye `limits` (`job_timeout_s`, `stall_timeout_s`, `io_mode`) y
`stalls` (cuántas veces se atascó).

Si falla o se agota el tiempo: `error` explica qué paso falló y con qué código (o qué
límite venció), `relevant_lines` trae
hasta 20 líneas de stdout/stderr que encajan con `error|failed|fatal|not found|
no matching|denied|…`, y `log_tail` las últimas 25.

## 4. Límites: ninguna espera es infinita

| Espera | Límite | Al vencer |
|---|---|---|
| `wait(job_id, timeout_s)` | `timeout_s` o `default_wait_s` (60 s); **tope duro** `max_wait_s` (300 s) | Devuelve `done: false`, `timeout_hit`, estado, pid, comando, `silent_for_s` y las últimas 15 líneas |
| `install(…, wait_s)` / `cancel(…, wait_s)` | `wait_s`, también limitado por `max_wait_s` / 30 s | Devuelven el estado actual |
| Cola (lock + `max_parallel`) | `max_queue_s` (3600 s) | `timed_out` |
| Ejecución del job | `job_timeout_s` (3600 s) | SIGTERM al grupo → 5 s → SIGKILL → 5 s; `timed_out` |
| Un paso concreto | `CommandStep.timeout_s` (opcional) | Igual: `timed_out` |
| Silencio del proceso | `stall_timeout_s` (300 s; pip 600 s; 0 = desactivado) | `stalled` + aviso; **no** se mata |
| Proceso que no muere ni con SIGKILL | 5 s | Se abandona y se anota |
| Sonda de versión / detección | 60 s / 120 s | Se omite |
| Envío de una notificación (listener) | `listener_timeout_s` (5 s) | Se anota en el log y se sigue |

El watchdog es una tarea por job en ejecución que despierta cada `min(1 s, stall/4)`:
comprueba el timeout global y el tiempo desde la última línea de stdout/stderr (o de
un `FuncStep` que escribe en el log).

**Prioridad de límites** (`job_timeout_s`, `stall_timeout_s`; `io_mode` sin el primer
nivel): parámetros de la petición (si el backend los expone, como pip) →
`[backends.<nombre>]` en la configuración → valores por defecto del backend
(`default_job_timeout_s`, `default_stall_timeout_s`, `default_io_mode`) → globales.
Un paso puede fijar su propio `stall_timeout_s` / `io_mode`.

## 5. Protocolo de notificaciones

El cliente **no necesita sondear**: llama a `install`, sigue con lo suyo y recibe los
avisos. `wait`/`logs` existen para quien quiera más detalle o una espera puntual.

El SDK `mcp` 2.x habla dos generaciones del protocolo; el servidor cubre ambas.

| Mecanismo | Cuándo | A quién | Contenido |
|---|---|---|---|
| `notifications/progress` | Mientras dura `wait` (o `install` con `wait_s>0`), como mucho 1/s y solo si hubo cambios | La petición en curso (su `progressToken`) | `progress`/`total` = pasos completados + fracción del paso actual; `message` = paso y última línea |
| `notifications/message` (logging) | Eventos `queued`, `started`, `step`, `stalled`, `resumed`, `finished` | La conexión que llamó a `install` + suscritos a `job://<id>` o `jobs://list` | `data = {message, event, job_id, uri, state, current_step, …}`; `stalled` añade `pid`, `command`, `step`, `silent_for_s`, `stall_timeout_s`, `log_tail`; `finished` añade `report`. `logger = "install-mcp"`; nivel `info`, `warning` (stalled, cancelado) o `error` (failed, timed_out) |
| `notifications/resources/updated` | Cada evento del job; `jobs://list` al encolar, atascarse, reanudarse y terminar | Conexiones suscritas con `resources/subscribe` | `uri` |
| `subscriptions/listen` (2026-07-28) | Igual que la anterior | Clientes modernos | `ResourceUpdated(uri)` publicado en el bus del SDK |

Notas:

- El **progreso** va ligado a una petición abierta (es la semántica de MCP); por eso
  se emite durante `wait`. El aviso **fuera de petición** es `notifications/message`.
- `logging/setLevel` está implementado: un cliente que pida `error` solo recibe los
  avisos de jobs fallidos o agotados; con `warning` recibe también los atascos.
- El progreso es estrictamente creciente (requisito del protocolo) y termina en
  `progress == total`.
- Si enviar a una conexión falla (cerrada), se olvida esa conexión. Si tarda más de
  `listener_timeout_s`, se abandona ese envío: un cliente lento no bloquea el job.
- En el protocolo 2026-07-28 el logging fuera de petición ya no existe: ahí el aviso
  es `ResourceUpdated` por `subscriptions/listen` + leer `job://<id>`.

### Recursos

- `job://{job_id}` — JSON con estado, 20 últimas líneas e informe si terminó.
- `jobs://list` — JSON con los 50 jobs más recientes.

## 6. Tools

| Tool | Bloquea | Devuelve |
|---|---|---|
| `backends()` | no | Backends registrados, si están permitidos/disponibles y el JSON Schema de sus `params` |
| `install(backend, params, wait_s=0)` | no (o hasta `wait_s`) | `job_id`, `state`, `summary`, `uri`, `log_file` |
| `status(job_id, tail=10)` | no | Estado completo (con `pid`, `command`, `silent_for_s`, `limits`) + informe si terminó |
| `list_jobs(state?, limit=20)` | no | Resumen de jobs recientes |
| `logs(job_id, tail=50, since_seq?, stream=all)` | no | `lines`, `last_seq` |
| `wait(job_id, timeout_s?)` | hasta `min(timeout_s or default_wait_s, max_wait_s)` | `{done: true, report}` o `{done: false, timeout_hit, state, pid, silent_for_s, log_tail, hint}` |
| `cancel(job_id, wait_s=10)` | hasta `wait_s` | Estado tras cancelar |

Los errores previsibles (backend no permitido, parámetros inválidos, job
inexistente) se devuelven como `isError` con un mensaje claro.

## 7. Interfaz de backend

```python
class Backend(ABC, Generic[P]):
    name: ClassVar[str]                    # "pip", "git", "apt"...
    description: ClassVar[str]
    Params: ClassVar[type[BackendParams]]  # pydantic, extra="forbid"; su schema se publica
    executables: ClassVar[tuple[...]]      # comprobados por availability()
    default_job_timeout_s / default_stall_timeout_s / default_io_mode: ClassVar  # límites propios

    def check(self, spec) -> None                       # validación semántica (ValueError)
    def requires_privileges(self, spec) -> bool         # ¿necesita root?
    def plan(self, spec) -> Plan                        # pasos con argv explícitos
    async def detect_versions(self, spec, probe) -> dict[str, str]
    def result_info(self, spec) -> dict                 # rutas útiles para el informe
```

`Plan(summary, steps, lock_key, secrets, timeout_s, stall_timeout_s)`:

- `steps`: `CommandStep(name, argv, env, env_remove, cwd, timeout_s, ok_exit_codes,
  stall_timeout_s, io_mode, privileged, skip_if)` o `FuncStep(name, func, skip_if)`.
- `timeout_s` / `stall_timeout_s`: límites pedidos para este job (los del backend pip
  vienen de los parámetros `job_timeout_s` / `stall_timeout_s`).
- `lock_key`: serializa jobs que tocan el mismo recurso.
- `secrets`: literales a redactar en logs, argv mostrado y parámetros.

`probe(argv, env=None, timeout_s=60) -> ProbeResult(exit_code, stdout, stderr)` es la
única forma de que un backend ejecute algo por su cuenta, y queda registrada en el log.

### Backends previstos

| Backend | Params principales | Pasos (argv) | Versión instalada | Privilegios | `lock_key` |
|---|---|---|---|---|---|
| **pip** ✅ | `packages`, `requirements`, `venv`, `python`, `installer`, `index_url`, `find_links`, `no_index`, `upgrade`, `job_timeout_s`, `stall_timeout_s` | `uv venv` / `python -m venv`; `uv pip install --python …` / `python -m pip install --progress-bar off … -- <reqs>` | `importlib.metadata` con el python del venv | no | `venv:<ruta>` |
| **git** (plantilla en `backends/_template.py`) | `url` (https), `dest`, `ref`, `depth` | `git clone --progress --depth N --branch REF -- URL DEST` o `git -C DEST pull --ff-only`; `GIT_TERMINAL_PROMPT=0` | `git rev-parse --short HEAD` | no | `path:<dest>` |
| **apt** | `packages` (`^[a-z0-9][a-z0-9+.-]*(=[\w.:~+-]+)?$`), `update` | `apt-get update`; `apt-get install -y --no-install-recommends -- pkgs`, `DEBIAN_FRONTEND=noninteractive`, `privileged=True` | `dpkg-query -W -f '${Package} ${Version}\n' pkgs` | **sí** | `apt` (lock global de dpkg) |
| **download** (wget/curl) | `url` (https), `dest`, `checksum` (`sha256:…`), `extract` | `curl -fL --proto =https --retry 3 -o tmp URL` (o `wget`); `FuncStep` verifica `Checksum` y mueve al destino; `FuncStep` extrae (tar/zip) comprobando *path traversal* | checksum/tamaño del fichero | no | `path:<dest>` |
| **brew** | `formulae`, `casks` | `brew install -- …`, `HOMEBREW_NO_AUTO_UPDATE=1`, `HOMEBREW_NO_ENV_HINTS=1` | `brew list --versions …` | no (brew se niega a correr como root) | `brew` |
| **npm** | `packages`, `prefix` (dir dentro de las raíces) | `npm install --prefix DIR --no-fund --no-audit -- pkgs` | `npm ls --json --prefix DIR` | no | `npm:<prefix>` |
| **cargo** | `crates`, `root`, `version`, `locked` | `cargo install --root DIR [--locked] -- crate` | `cargo install --list --root DIR` | no | `cargo:<root>` |
| **maven** | `artifacts` (`g:a:v`), `repo_url` (https) | `mvn -B -ntp dependency:get -Dartifact=g:a:v` | ruta en `~/.m2` | no | `maven` |
| **binary** | `url`, `checksum` (**obligatorio**), `dest`, `name` | como *download* + `chmod 0755` en `FuncStep` | `<bin> --version` (opcional) | no | `path:<dest>` |
| **wsl** | `distro`, `backend`, `params` | delega en otro backend anteponiendo `wsl.exe -d DISTRO --` al argv | la del backend delegado | según el delegado | `wsl:<distro>:<lock delegado>` |

Modo de E/S recomendado: `pipe` para pip, git, npm, cargo, curl/wget (escriben sin
buffer o en stderr); `pty` para apt-get/dpkg y herramientas C (binwalk, unsquashfs, 7z)
cuya salida a stdout se bufferiza.

Para herramientas típicas de análisis de firmware (binwalk, squashfs-tools,
p7zip): en macOS backend `brew` (`binwalk`, `squashfs`, `p7zip`); en Linux/WSL
backend `apt` (`binwalk`, `squashfs-tools`, `p7zip-full`), que requiere
`allow_privileged = true`.

## 8. Modelo de seguridad

1. **Nada de shell**: todo se ejecuta con `asyncio.create_subprocess_exec(*argv)`.
   No hay `shell=True` en ningún sitio; los argumentos los construye el backend.
2. **Sin inyección de opciones**: los valores que vienen del cliente se validan con
   regex estrictas (p. ej. requisitos PEP 508 sin URLs) y se rechaza cualquier valor
   que empiece por `-`. Donde la herramienta lo admite, se usa `--` antes de los
   argumentos posicionales.
3. **Allowlist de backends** (`allowed_backends`, por defecto solo `pip`). Registrar un
   plugin no lo habilita.
4. **Sin `sudo` por defecto**: los backends que necesitan root lo declaran
   (`requires_privileges` / `CommandStep.privileged`) y el servidor los rechaza salvo
   `allow_privileged = true`. Aun así se usa `sudo -n` (no interactivo): si sudo pide
   contraseña, el paso falla en vez de quedarse colgado.
5. **Rutas acotadas**: todo lo que se escribe (venvs, clones, descargas) debe caer en
   `allowed_roots` (por defecto `$HOME`) o en `state_dir`; se resuelven symlinks antes de
   comprobarlo.
6. **Redacción de secretos** en cada línea de log, en el argv mostrado, en los
   parámetros guardados y en el informe: credenciales en URLs
   (`https://user:pass@` → `https://***@`), `token=`, `password=`, `api_key=`,
   `Authorization:`, tokens con formato conocido (GitHub, GitLab, AWS, Slack, `sk-…`),
   patrones extra de la configuración y literales concretos que declara cada plan.
7. **Descargas**: solo `https://` (`require_https`) y checksum opcional
   (`Checksum.parse("sha256:…").verify_file()`), obligatorio para binarios sueltos.
8. **Sin interacción**: `stdin` es `/dev/null`; se fijan `PIP_NO_INPUT`,
   `GIT_TERMINAL_PROMPT=0`, `DEBIAN_FRONTEND=noninteractive`, etc.
9. **Límites** (§4): timeout global por job, por paso y en cola, watchdog de atasco,
   espera acotada con tope duro, `max_parallel`, cancelación del grupo de procesos
   completo con SIGTERM y luego SIGKILL.

Lo que **no** cubre el prototipo: aislamiento del sistema de ficheros (sandbox),
límites de CPU/memoria, y el contenido de un `requirements.txt` (pip lo interpreta
tal cual; lo controla quien escribe el fichero).

## 9. Cómo añadir un backend

1. Copia `src/install_mcp/backends/_template.py` (ejemplo de `git`).
2. Define `Params(BackendParams)` con tipos estrictos y descripciones (el cliente las ve
   en `backends()`).
3. En `check()` valida lo que pydantic no puede: regex de nombres, `reject_option_like`,
   `require_https`, `ensure_within_roots`, existencia de ficheros.
4. En `plan()` devuelve un `Plan` con argv explícitos, `lock_key` y `secrets`.
   Usa `skip_if` para pasos idempotentes y `FuncStep` para lógica en Python.
5. Si necesita root: `requires_privileges() -> True` y `CommandStep(privileged=True)`.
6. Implementa `detect_versions()` con `probe(...)` y `result_info()`.
   Además, fija límites razonables: `default_job_timeout_s`, `default_stall_timeout_s` (más
   alto si la herramienta pasa mucho tiempo callada) y `default_io_mode` (`pty` si es un
   programa C que bufferiza stdout). Si tiene sentido, expón `job_timeout_s` /
   `stall_timeout_s` en `Params` y pásalos al `Plan`.
7. Regístralo:
   - integrado: añádelo a `BUILTIN` en `backends/__init__.py`;
   - externo: `plugins = ["paquete.modulo:Clase"]` en la configuración.
8. Habilítalo en `allowed_backends`.
9. Tests: validación (valores aceptados y rechazados) y plan (argv esperado) sin red;
   si se puede, una instalación real offline como `tests/test_pip.py`.

## 10. Decisiones y alternativas

- **SDK `mcp` 2.x (`MCPServer`)**: es el sucesor de FastMCP en el SDK oficial
  (`mcp.server.fastmcp` ya no existe en 2.x). La API de tools/recursos es la misma idea.
  Las suscripciones `resources/subscribe` y `logging/setLevel` se registran como
  handlers de bajo nivel porque `MCPServer` no los trae (ya no existen en el protocolo
  2026-07-28, pero Claude Code usa el protocolo con handshake).
- **Un proceso por servidor, estado en memoria + disco**: suficiente para un MCP
  local por stdio. El historial sobrevive a reinicios; los jobs en curso no (se marcan
  como interrumpidos).
- **`stalled` no mata**: un silencio largo puede ser legítimo (compilar un wheel,
  `apt-get` desempaquetando). Se avisa enseguida y se deja que decida el cliente
  (`cancel`) o el timeout global.
- **Backends como planificadores puros**: hace que la seguridad y la observabilidad sean
  uniformes y que los backends sean triviales de testear sin ejecutar nada.
