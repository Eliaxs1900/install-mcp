# watcher-mcp — documento de diseño

## 1. Problema

Las instalaciones, descargas, clones, builds y tests (`pip install`, `git clone`,
`apt-get install`, descargas de firmware, `mvn verify`…) tardan más que el timeout de una
llamada de herramienta (120 s en el Bash de Claude Code). Lanzados a mano en segundo
plano, la salida llega vacía o con buffering y no se sabe en qué punto van, si han
fallado ni por qué.

`watcher-mcp` es un servidor MCP que **lanza y vigila cualquier trabajo largo**:

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
 │  backends/  pip · git · download · binary · npm · cargo · go ·       │
 │             maven · gradle · brew · apt · docker · run              │
 └──────────────────────────────────────────────────────────────────────┘
```

| Módulo | Responsabilidad |
|---|---|
| `config.py` | Configuración (TOML + entorno): allowlist, privilegios, paralelismo, raíces, plugins. |
| `jobs.py` | `Job`, `JobManager`, estados, logs, informe, persistencia, eventos. |
| `process.py` | Ejecución sin shell con lectura en vivo y cancelación del grupo de procesos. |
| `security.py` | `Redactor`, `require_https`, `Checksum`, `ensure_within_roots`, `reject_option_like`. |
| `logsummary.py` | Selección de `relevant_lines`: `RELEVANT`, filtro de ruido y duplicados. |
| `backends/base.py` | Interfaz `Backend`, `Plan`, `CommandStep`, `FuncStep`, `ResultContext`. |
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
límite venció), `relevant_lines` trae hasta 20 líneas de stdout/stderr y `log_tail` las
últimas 25. `relevant_lines` (`logsummary.py`) se elige así, sobre las últimas 20 000
líneas del log (las mismas que lee `summarize`):

1. solo cuentan las líneas que encajan con `RELEVANT` (`error`, `failed`, `fatal`,
   `Traceback`, `XxxError`/`XxxException`, `npm ERR!`, `not found`, `denied`…);
2. se descarta el ruido (`NOISE`): marcos de traza (`at …`, `... N more`, también con
   prefijo `[ERROR]`), líneas `[ERROR]` vacías y el texto de ayuda de Maven
   (`-> [Help 1]`, `Re-run Maven…`, `[Help 1] http…`), Gradle (`* Try:`,
   `Run with --stacktrace…`, `Get more help at…`) y npm (`A complete log of this run…`);
3. se quitan los duplicados (Maven repite bloques enteros);
4. si quedan más de 20, se toman las **10 primeras** (las de stderr tienen prioridad: suelen
   ser la causa) y las **10 últimas** (el resumen final), en orden cronológico. Así una traza
   larga después del error ya no desplaza la causa.

Resumen del backend (`summarize`): en Maven, si `error_detail` (`Failed to execute goal … :`)
acaba en `:`, se le añade la siguiente línea `[ERROR]` que no sea ruido (la causa). En
Maven y Gradle, si el job falla y no llegó a ejecutarse ningún test (`run == 0`), el titular
dice `tests: no llegaron a ejecutarse` y `tests.executed` es `false`, en lugar de
`0 ejecutados, 0 fallidos`.

`info.built` / `info.stale` (Maven y Gradle): `built` lista solo los jars que ha generado
**este** job (`mtime >=` arranque del job, con 1 s de holgura por la resolución del sistema de
ficheros) y solo si el job acaba en `succeeded`. Los jars anteriores de `target/` o
`build/libs/` (de un `package` previo) salen aparte en `stale`. Si el job falla, se cancela o
vence, no se lista ninguno: no se sabe qué se generó y sería engañoso.

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
| `notifications/message` (logging) | Eventos `queued`, `started`, `step`, `stalled`, `resumed`, `finished` | La conexión que llamó a `install` + suscritos a `job://<id>` o `jobs://list` | `data = {message, event, job_id, uri, state, current_step, …}`; `stalled` añade `pid`, `command`, `step`, `silent_for_s`, `stall_timeout_s`, `log_tail`; `finished` añade `report`. `logger = "watcher-mcp"`; nivel `info`, `warning` (stalled, cancelado) o `error` (failed, timed_out) |
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
| `backends()` | no | Backends registrados, si están permitidos/disponibles, sus límites y el JSON Schema de sus `params` |
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
    def result_info(self, spec, context=None) -> dict   # rutas útiles para el informe
```

`result_info` recibe, si declara el parámetro `context` (o `**kwargs`), un `ResultContext`
(`started_at`, `ok`, `state`) para distinguir lo que generó este job de lo que ya existía.
Los backends externos con la firma antigua `result_info(self, spec)` siguen funcionando: el
núcleo solo pasa el contexto si el método lo acepta.

`Plan(summary, steps, lock_key, secrets, timeout_s, stall_timeout_s)`:

- `steps`: `CommandStep(name, argv, env, env_remove, cwd, timeout_s, ok_exit_codes,
  stall_timeout_s, io_mode, privileged, skip_if)` o `FuncStep(name, func, skip_if)`.
- `timeout_s` / `stall_timeout_s`: límites pedidos para este job (los del backend pip
  vienen de los parámetros `job_timeout_s` / `stall_timeout_s`).
- `lock_key`: serializa jobs que tocan el mismo recurso.
- `secrets`: literales a redactar en logs, argv mostrado y parámetros.

`probe(argv, env=None, timeout_s=60) -> ProbeResult(exit_code, stdout, stderr)` es la
única forma de que un backend ejecute algo por su cuenta, y queda registrada en el log.

### Backends implementados

Agrupados por los focos de interacción habituales de un agente de código:

| Foco | Backend | Params principales | Comando(s) | Versión instalada | Priv. | `lock_key` |
|---|---|---|---|---|---|---|
| Web | **download** | `url` (https), `dest`, `checksum`, `extract`, `strip_components` | descarga en Python (hilo): solo https también tras redirecciones, progreso cada 2 s, tamaño máximo, `.part` + renombrado atómico; extracción tar/zip sin `..`, rutas absolutas ni enlaces | tamaño + sha256 (o nº de ficheros extraídos) | no | `path:<dest>` |
| Web | **binary** | `url`, `checksum` (**obligatorio**), `name`, `member`, `bin_dir` | como *download* + extrae `member` de un tar/zip + `chmod +x` | `<bin> --version` | no | `path:<bin>` |
| Código | **pip** | `packages`, `requirements`, `venv`, `python`, `installer`, `index_url`, `find_links`, `no_index`, `upgrade` | `uv venv` / `python -m venv`; `uv pip install` / `pip install --progress-bar off … -- <reqs>` | `importlib.metadata` en el venv | no | `venv:<ruta>` |
| Código | **git** | `url` (https, ssh, `git@host:…`, ruta local), `dest`, `ref`, `depth`, `submodules`, `update` | `git clone --progress … -- URL DEST` o `fetch` + `checkout` + `merge --ff-only`; `GIT_TERMINAL_PROMPT=0`, `ssh -o BatchMode=yes` | `git describe` + HEAD | no | `path:<dest>` |
| Código | **npm** | `packages` + `prefix`, o `project_dir` (+ `manager` auto/npm/pnpm/yarn), `ignore_scripts` | `npm install --prefix DIR --no-save -- pkgs`; proyecto: `npm ci` / `pnpm install --frozen-lockfile` / `yarn install --frozen-lockfile` | `npm ls --json --depth=0` | no | `path:<prefix>` |
| Código | **cargo** | `crates` (`name[@ver]`), `root`, `locked` | `cargo install --root DIR --locked -- crate` (un paso por crate) | `cargo install --list` | no | `path:<root>` |
| Código | **go** | `packages` (`ruta@versión`), `gobin` | `go install -v -- pkg@ver` con `GOBIN` | `go version -m GOBIN` | no | `path:<gobin>` |
| Código | **maven** | `artifacts` (`g:a:v`), o `project_dir` + `goals` (allowlist), `skip_tests`, `offline`, `profiles`, `properties`, `use_wrapper` | `mvn -B -ntp dependency:get -Dartifact=…`; proyecto: `mvnw`/`mvn -B -ntp -f pom.xml <goals>` | `~/.m2` / pom + `target/*.jar` | no | `maven:repo` / `path:<proyecto>` |
| Código | **gradle** | `project_dir`, `tasks` (allowlist, admite `:mod:tarea`), `exclude_test`, `offline`, `use_wrapper` | `gradlew`/`gradle --console=plain --no-daemon -p DIR <tasks>` | `build/libs/*.jar` | no | `path:<proyecto>` |
| Sistema | **brew** | `formulae`, `casks`, `upgrade` | `brew install --formula -- …` / `--cask`; sin auto-update ni prompts | `brew list --versions` | no (no admite root) | `brew` |
| Sistema | **apt** | `packages` (`nombre[=versión]`), `update`, `install_recommends` | `apt-get -y -q -o Dpkg::Lock::Timeout=120 update/install`, `DEBIAN_FRONTEND=noninteractive`; `sudo -n` si no es root | `dpkg-query -W` | **sí** | `apt` |
| Sistema | **docker** | `images`, `platform` | `docker pull [--platform P] -- img` | id de la imagen | no | `docker` |
| Comandos | **run** | `program` (allowlist `[backends.run] programs` o reglas `commands`), `args`, `cwd`, `env`, `env_from`, `io_mode`, `ok_exit_codes` | el programa con sus argumentos, sin shell | `<program> --version` | no | — |

Todos aceptan además `job_timeout_s` y `stall_timeout_s`.

**Allowlist por defecto**: todos menos `apt` (necesita root) y `run` (ejecuta programas
arbitrarios de una lista que hay que configurar). `backends()` informa de si cada uno
está disponible en la máquina; si falta el ejecutable, `install` lo rechaza al instante.
Para uno no permitido (o `run` permitido sin programas ni reglas) añade `how_to_enable`:
`config_file` (el `config.toml` activo: `--config`, `$WATCHER_MCP_CONFIG` o
`~/.config/watcher-mcp/config.toml`), `config_file_exists` y `toml`, el fragmento exacto
(`allowed_backends` con el backend añadido y lo propio de cada uno: `[backends.run]` de
ejemplo, `allow_privileged = true` para apt). El agente se lo enseña al usuario; no edita la
configuración. Cada backend lo declara con `enable_snippet()` / `setup_hint()`.

**Reglas de `run`**. Se acepta la petición si `program` está en `programs` (cualquier
argumento, cualquier `cwd` dentro de `allowed_roots`) **o** casa con una regla
`[backends.run] commands = [{program, args_prefix, cwd_under, inherit_env}]`:
mismo programa; los primeros argumentos iguales a `args_prefix`, salvo un elemento que
acaba en `/`, que casa con un argumento que empiece así y no contenga `..`; y el `cwd`
resuelto bajo `cwd_under`. Si ninguna casa, el error lista cada regla con el motivo
(`args[2] = 'x.py' no está bajo 'tools/'`, `cwd … no está bajo …`). Así se puede permitir
`uv run python tools/*.py` en un proyecto sin abrir cualquier `uv run`.

**Variables por nombre** (`inherit_env` / `env_from`). Un secreto no debe ir en los params
(quedaría en `job.json`). `Plan.inherit_env` y `Plan.env_from` llevan solo nombres y el
núcleo (`JobManager._inherit_env`) pone los valores en el `env` de cada `CommandStep` y los
añade a `Plan.secrets`, así que se redactan en todas partes (logs, `status`, `logs`,
notificaciones, informe). Fuentes (`envsource.py`):

| Origen del nombre | Entorno del servidor | Shell de login | Si falta |
|---|---|---|---|
| `inherit_env` global, `[backends.<n>] inherit_env`, regla de `commands` | sí | sí, si `login_shell_env` (por defecto) | aviso en el log |
| `env_from` (params de `run`) | sí | **no** | job rechazado |

La shell de login se lee con `$SHELL -l -i -c "printf marcador; env -0"` (sin terminal,
`stdin` nulo, timeout de 15 s, una sola vez por servidor) y solo cuando un nombre configurado
no está en el entorno del servidor. Que la petición no pueda pedir variables de la shell de
login evita que una instrucción inyectada saque secretos que el usuario no listó. No se
heredan `PATH`, `HOME`, `SHELL`, `LD_*`, `DYLD_*`, `PYTHONPATH`…

**Pendiente**: `wsl` (delegar en un backend dentro de una distro con `wsl.exe -d DISTRO --`).
Las rutas dentro de la distro no encajan con el modelo de raíces permitidas del host, así
que necesita su propio diseño de rutas.

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
   patrones extra de la configuración y literales concretos que declara cada plan
   (entre ellos los valores heredados con `inherit_env`/`env_from`).
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

1. Copia `src/watcher_mcp/backends/_template.py` (ejemplo de `git`).
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
