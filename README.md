# watcher-mcp

[![tests](https://github.com/Eliaxs1900/watcher-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/Eliaxs1900/watcher-mcp/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![MCP SDK](https://img.shields.io/badge/mcp-2.x-6f42c1)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Servidor MCP que **lanza y vigila trabajos largos**: instalaciones, descargas, clones,
builds y tests. Cada trabajo corre en segundo plano y la llamada devuelve un `job_id` al
instante. La salida se guarda línea a línea, sin buffering. Un vigilante avisa por
notificación MCP si el proceso se queda en silencio (`stalled`) y cuando termina, con un
informe: éxito o fallo, código de salida, duración, versiones instaladas y líneas
relevantes del log. **Ninguna espera es infinita**: `wait` tiene un tope duro y cada
trabajo tiene un tiempo máximo (`timed_out`).

> **English:** an MCP server that launches and watches long-running jobs (pip/uv, git,
> https downloads and binaries, npm, cargo, go, maven, gradle, brew, apt, docker pull,
> allowlisted build/test commands). It returns a job id immediately and streams unbuffered
> logs. A watchdog flags stalled processes, every wait is bounded, and an MCP
> notification carries a report when the job ends.

![Comparativa: Bash + pip frente a watcher-mcp](docs/demos/compare.gif)

## Por qué: la comparativa

Los agentes como Claude Code instalan con `Bash` + `pip install`. Si la instalación
tarda más que el límite de la herramienta (120 s), el agente se queda bloqueado. Luego
el comando pasa a segundo plano y el agente solo tiene un fichero de salida que debe
releer de vez en cuando. Ese fichero no le dice si el proceso sigue vivo o colgado,
ni cuándo terminó.

[`examples/compare.py`](examples/compare.py) hace las dos cosas con **el mismo paquete**:
`slowpkg`, un sdist local que tarda 25 s en compilar sin escribir nada, como cualquier
wheel que se compila desde el código fuente. Todas las cifras se miden en la propia
ejecución. Esta tabla sale de la grabación del GIF de arriba, hecha en un contenedor
Docker:

| | ANTES · Bash + pip | DESPUÉS · watcher-mcp |
|---|---|---|
| El agente recupera el control | a los 8 s (límite de Bash a escala; 120 s reales) | **0,04 s** |
| Durante la compilación ve… | el log congelado en `Building wheel … started` | estado, pid, comando, segundos sin salida |
| Aviso de atasco | ninguno | **`stalled` a los 7 s** con pid, comando y últimas líneas |
| Se entera de que terminó | por sondeo, 2,5 s tarde (y solo si sigue sondeando) | notificación al instante |
| Llamadas del agente | 1 Bash + 5 sondeos (`tail`) | 2 (`install` + `wait`) |
| Informe final | texto en bruto | exit code, versión instalada (`slowpkg 1.0`), duración, pasos |

Con pip, el log no llega a estar vacío: pip escribe unas líneas y luego se calla
mientras compila. Con programas que acumulan stdout en un buffer (herramientas en C,
scripts de Python sin `-u`), el fichero sí queda **vacío** hasta el final.
watcher-mcp lo resuelve con `io_mode = "pty"`. El test
[`test_buffered_program_pipe_vs_pty`](tests/test_limits.py) lo demuestra: por tubería,
0 líneas mientras el programa trabaja; con pty, en vivo.

## Demos

Todas son ejecuciones reales del servidor por stdio (como lo lanza Claude Code),
grabadas en un contenedor Docker con [`scripts/make_demos.sh`](scripts/make_demos.sh).
Las rutas del usuario se acortan a `~`. En el GIF, las pausas largas se acortan; los
tiempos `[ 12.3s]` impresos son los reales. Cada demo tiene también su grabación
`.cast` ([asciinema](https://asciinema.org)) y una captura PNG completa.

### Atasco detectado y recuperado (`stalled`)

La compilación pasa 18 s sin escribir nada. A los 4 s llega el aviso `stalled` con el
pid, el comando y las últimas líneas. Cuando pip vuelve a escribir, el servidor avisa
de que se reanuda la salida y, al acabar, envía el informe.

![stalled](docs/demos/stall.gif)

### Timeout global (`timed_out`)

Una compilación que no acabaría nunca, con `job_timeout_s: 12`. El servidor envía
SIGTERM al grupo de procesos y, si hace falta, SIGKILL. El aviso llega como `error`, y
la demo comprueba al final que el proceso ya no existe.

![timed_out](docs/demos/timeout.gif)

### Instalación real desde PyPI (uv)

![install](docs/demos/install.gif)

### Fallo con informe

Un paquete que no existe: el informe trae el código de salida y las líneas de error
relevantes, sin tener que leer el log entero. Las `relevant_lines` descartan las trazas
(`at …`) y el texto de ayuda de Maven/Gradle, y mezclan las primeras líneas relevantes
(la causa) con las últimas. En `maven`/`gradle`, `info.built` lista solo los jars que generó
ese job (y solo si acabó bien); los anteriores salen en `info.stale`.

![fail](docs/demos/fail.gif)

Capturas completas: [compare](docs/demos/compare.png) ·
[stall](docs/demos/stall.png) · [timeout](docs/demos/timeout.png) ·
[install](docs/demos/install.png) · [fail](docs/demos/fail.png)

### Usado desde Claude Code

Registrado con `claude mcp add` (ver más abajo), Claude Code ve las tools
`mcp__watcher-mcp__*`. Esta es una ejecución real desde una sesión de Claude Code
(solo se acortan las rutas):

```text
→ install(backend="pip", params={"packages": ["pypdf"], "venv": "claude-code-demo"})
← {"job_id": "718a09b210da", "state": "queued",
   "summary": "pip install pypdf en ~/.local/state/watcher-mcp/venvs/claude-code-demo (vía uv)",
   "next": "wait('718a09b210da', timeout_s=60) o logs('718a09b210da', tail=30). …"}

→ wait(job_id="718a09b210da", timeout_s=60)
← {"done": true, "report": {
     "headline": "[718a09b210da] OK en 0.6s – pip install pypdf en ~/.local/state/watcher-mcp/venvs/claude-code-demo (vía uv) (pypdf 7.1.1, (python) 3.15.2)",
     "state": "succeeded", "exit_code": 0, "duration_s": 0.645,
     "steps": [{"name": "create-venv", "state": "ok", "exit_code": 0, "duration_s": 0.343, …},
               {"name": "install", "state": "ok", "exit_code": 0, "duration_s": 0.262,
                "command": ["uv", "pip", "install", "--no-progress", "--python", "…/bin/python", "--", "pypdf"]}],
     "installed": {"pypdf": "7.1.1", "(python)": "3.15.2"},
     "limits": {"job_timeout_s": 3600, "stall_timeout_s": 600, "io_mode": "pipe"},
     "info": {"venv": "~/.local/state/watcher-mcp/venvs/claude-code-demo", "installer": "uv", …}}}
```

## Cómo funciona

```mermaid
flowchart LR
    C["Cliente MCP<br/>(Claude Code)"] -- "install · status · logs<br/>wait · cancel · backends" --> S["server.py<br/>MCPServer"]
    S -- "notifications/message<br/>resources/updated · progress" --> C
    S --> J["jobs.py · núcleo<br/>cola · locks · límites<br/>watchdog · informe"]
    J --> P["process.py<br/>exec sin shell · pipe/pty<br/>SIGTERM → SIGKILL"]
    J --> B["backends/<br/>web: download · binary<br/>código: pip · git · npm · cargo · go · maven · gradle<br/>sistema: brew · apt · docker · run"]
    J -. "output.log · job.json<br/>report.json" .-> D[("~/.local/state/<br/>watcher-mcp")]
    J -. "fin del job (opcional)<br/>busbridge.py" .-> SB["sessionbus<br/>(aviso a la sesión<br/>que lo lanzó)"]
```

- **Backends como planificadores puros**: un backend solo valida parámetros y devuelve
  un `Plan` con argv explícitos. El núcleo ejecuta, registra, limita y notifica. Por
  eso añadir git, apt o curl no necesita tocar la cola, los logs ni la seguridad.
- **Estados**: `queued` → `running` ⇄ `stalled` → `succeeded` | `failed` | `cancelled` | `timed_out`.
- **Seguridad**:
  - nada de `shell=True`;
  - se rechazan los valores que empiezan por `-`;
  - allowlist de backends;
  - sin `sudo` salvo opt-in (y entonces `sudo -n`);
  - rutas limitadas a raíces permitidas y, dentro de ellas, **zonas protegidas**: nada con un
    componente oculto (`~/.zshrc`, `~/.ssh`, `proyecto/.git/hooks`…) ni las subrutas sensibles de
    `~/Library` (`LaunchAgents`, `Preferences`, `Keychains`, `Cookies`, `Containers`…), para que una
    instrucción inyectada al cliente no pueda escribir ni leer ahí. El resto de `~/Library` (p. ej. las
    carpetas de sesión de Claude Desktop en `~/Library/Application Support/Claude`) se puede usar. Una
    zona protegida se permite a propósito añadiéndola como raíz explícita: el error dice dónde y da la
    línea exacta;
  - las descargas no sobrescriben nada salvo `overwrite: true`; la extracción tiene límites de
    tamaño y de número de ficheros;
  - `git` neutraliza la configuración local del repo (hooks, fsmonitor, credential helpers) y no
    actualiza repos que no clonó salvo `trust_existing: true`;
  - `maven` rechaza las propiedades `-D` que son rutas de salida o de configuración (`outputFile`,
    `maven.repo.local`, `argLine`…), y cualquier valor con forma de ruta pasa por las raíces y las
    zonas protegidas;
  - `npm` sin scripts de instalación por defecto; `run` con allowlist de programas o reglas por
    prefijo de argumentos y carpeta, y lista de variables de entorno permitidas; los secretos se
    heredan por nombre (`inherit_env`/`env_from`) y su valor se redacta;
  - los jobs no heredan las credenciales con las que la app habla con el servidor (mensajería entre
    sesiones, OAuth, `CLAUDE_CODE_*TOKEN*`, `ANTHROPIC_*`…) ni los datos de la cuenta: un `setup.py`
    cualquiera no puede leerlas. El resto del entorno (PATH, HOME, proxies, `GH_TOKEN`…) sí se
    hereda. Para dar una a propósito, `inherit_env` en la configuración; `env_from` no vale;
  - cancelar o agotar el tiempo termina el grupo de procesos y también a los descendientes que se
    habían separado de él (`setsid`);
  - solo HTTPS;
  - redacción de tokens y contraseñas en logs, argv, informes y parámetros guardados.
- **Varias sesiones a la vez**: cada sesión de Claude Code arranca su propio servidor y todos
  comparten `state_dir`. Cada servidor se identifica con un `flock`: los jobs en curso de otra
  sesión se ven en solo lectura (no se marcan, cancelan ni borran), y los de un servidor que murió
  de golpe se recogen al arrancar el siguiente (se termina su grupo de procesos tras comprobar que
  el pid sigue siendo el mismo programa). En Linux, además, el hijo muere con el servidor.

Diseño completo en [docs/design.md](docs/design.md).

## Instalación

Requiere Python 3.11+.

```bash
git clone https://github.com/Eliaxs1900/watcher-mcp.git
cd watcher-mcp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
```

Sin uv: `python3 -m venv .venv && .venv/bin/pip install -e .`

## Registrarlo en Claude Code

Desde la carpeta del proyecto, para todos tus proyectos:

```bash
claude mcp add --scope user watcher-mcp -- "$PWD/.venv/bin/watcher-mcp"
```

O solo para un proyecto, con un `.mcp.json` en su raíz:

```json
{
  "mcpServers": {
    "watcher-mcp": {
      "command": "/RUTA/ABSOLUTA/watcher-mcp/.venv/bin/watcher-mcp",
      "args": ["--config", "/RUTA/ABSOLUTA/config.toml"]
    }
  }
}
```

`args` es opcional; sin `--config` se usa `~/.config/watcher-mcp/config.toml` si existe.
Comprueba que conecta con `claude mcp list` (o con `watcher-mcp doctor`).

### Migración desde `install-mcp`

El proyecto se llamaba `install-mcp`. Si tienes un registro antiguo (`claude mcp list` muestra
`install-mcp`), o un cliente cuyo JSON apunta a `…/bin/install-mcp`:

1. `watcher-mcp doctor` te dice qué registros están rotos o obsoletos y el comando exacto para
   arreglarlos. A mano: `claude mcp remove install-mcp -s user` y
   `claude mcp add --scope user watcher-mcp -- "$PWD/.venv/bin/watcher-mcp"` (cambia `-s user`
   si lo registraste en otro ámbito; en Claude Desktop, edita `command` en
   `claude_desktop_config.json`).
2. **Reinicia las sesiones ya abiertas** de Claude Code (y Claude Desktop): los clientes conectan
   los servidores MCP solo al arrancar la sesión. Una sesión abierta con un registro roto no
   recupera las tools aunque luego lo arregles; mientras tanto, [el modo CLI](#modo-cli) funciona.
3. Al instalar watcher-mcp en ese mismo entorno, el comando `install-mcp` pasa a ser un **alias de
   transición** (durante una o dos versiones): avisa por stderr con el comando exacto para volver a
   registrarlo y después sirve con normalidad, para no tumbar las sesiones abiertas. Si el paquete
   antiguo se desinstaló sin instalar este, el script viejo ya no puede importarlo y el cliente solo
   ve `CONNECTION_CLOSED`: ahí `watcher-mcp doctor` es lo que lo explica.

## Modo CLI

Para cuando el cliente MCP no tiene cargado el servidor (una sesión abierta antes de registrarlo
no recarga sus servidores MCP), `watcher-mcp` también funciona como comando de consola sobre el
**mismo núcleo**: mismos límites, mismo `io_mode`, misma escalada SIGTERM → SIGKILL sobre el grupo
de procesos y el mismo log en el `state_dir`. Un agente o una persona sigue teniendo detección de
atascos y timeouts con un comando de Bash, en vez de un `timeout`/`perl alarm` que no mata el árbol
de procesos ni deja log.

```bash
watcher-mcp run --idle 120 --timeout 900 --cwd ./proyecto -- mvn -o -B test
watcher-mcp list
watcher-mcp status 3f9a1c            # un prefijo único del id basta
watcher-mcp logs 3f9a1c --tail 100
watcher-mcp doctor
```

### `run`

```
watcher-mcp run [--idle S] [--timeout S] [--cwd DIR] [--io-mode pipe|pty|stdbuf]
                [--quiet] [--json] [--strict] [--config F] [--state-dir D] -- cmd args…
```

- El comando va **tras `--`** y se ejecuta sin shell (nunca `shell=True`). Un programa con `/`
  (`./gradlew`) es relativo a `--cwd`.
- `--idle S`: segundos sin salida para avisar de atasco (`stall_timeout_s`); `--timeout S`: tiempo
  máximo (`job_timeout_s`). Sin ellos valen los de la configuración (300 s y 3600 s). `0` desactiva
  el vigilante, y el timeout sigue acotado por el tope duro `max_job_timeout_s`. Un atasco **avisa**
  (`stalled`), no mata: lo que acota el tiempo es `--timeout`.
- La salida va en vivo: stdout del comando a stdout y stderr a stderr, así que se puede encadenar
  con pipes. Con `--quiet` solo salen los avisos de atasco. Los avisos y el informe final (titular,
  duración, ruta del log y, si falla, las líneas relevantes) van a stderr. Con `--json`, stdout es
  solo el informe en JSON (con `cli_exit_code`) y la salida en vivo pasa a stderr.
- **Ctrl-C** (o SIGTERM al propio `watcher-mcp`) cancela el job: SIGTERM al grupo de procesos y,
  pasado un plazo acotado, SIGKILL. Una segunda señal salta directamente al SIGKILL.
- El job queda en `<state_dir>/jobs/<id>/` como cualquier otro: `status`, `logs` y `list` (y las
  tools `status`/`logs`/`list_jobs` de un servidor MCP que use ese `state_dir`) lo ven después.

Códigos de salida:

| Código | Significado |
|---|---|
| `0` | el comando terminó bien |
| el del comando | el comando falló (si lo mató una señal N, `128+N`) |
| `124` | tiempo agotado (`--timeout`), como `timeout(1)` |
| `130` | cancelado (Ctrl-C o SIGTERM) |
| `127` / `126` | el programa no existe / no es ejecutable |
| `2` | error de uso o de configuración |

Un comando puede devolver 124 o 130 por su cuenta; si importa distinguirlo, `--json` da el estado
(`state`: `timed_out`, `cancelled`, `failed`…).

### `status`, `logs` y `list`

Leen el `state_dir` en **solo lectura**: no reclaman jobs huérfanos ni borran nada (eso lo hace el
servidor al arrancar). Un job en curso en otro servidor o `run` vivo se muestra como en curso y de
solo lectura (marcado con `*` en `list`); si su proceso murió de golpe, se muestra como
interrumpido. `status` y `list` aceptan `--json`; `logs` acepta `--tail N` y `--stream`.

### Seguridad: `run` de la CLI no aplica la allowlist

`[backends.run]` (programas permitidos, reglas `commands`) y `allowed_roots` protegen al servidor
MCP frente a **un agente que pide ejecutar cosas** sin que nadie lo vea. `watcher-mcp run` lo lanza
una persona o un agente desde su shell, y esa shell ya pasa por sus propios permisos (en Claude
Code, las reglas de `Bash(...)`). Por eso la CLI **no** aplica esa allowlist por defecto: exigir
una configuración previa dejaría la CLI inútil justo cuando no hay servidor MCP. Lo que sí
mantiene: nunca usa shell, redacta secretos en logs e informe, respeta `inherit_env`, y los
límites de tiempo y el tope duro.

Consecuencia que conviene conocer: **una regla de permisos como `Bash(watcher-mcp run *)` deja
ejecutar cualquier comando**, igual que `Bash(bash *)`. Si lo permites, es una autorización
completa. Para acotarla, usa reglas más estrechas (`Bash(watcher-mcp run --idle 120 -- mvn *)`) o
`--strict`, que aplica la misma allowlist que el servidor: solo `programs` y `commands` de
`[backends.run]` y solo `cwd` dentro de `allowed_roots` (y fuera de las zonas protegidas), con los
mismos mensajes de error. Con `--strict`, el programa debe ser un nombre en PATH (no una ruta).

## Diagnóstico: `watcher-mcp doctor`

```bash
watcher-mcp doctor [--config F] [--state-dir D] [--json] [--no-server] [--no-clients]
```

Comprueba y, por cada problema, imprime un mensaje accionable (`arreglo: …`); sale con código
distinto de 0 si hay algún `FALLO` (los `AVISO` no cuentan):

- **servidor**: se lanza a sí mismo por stdio (con un `state_dir` temporal, así que no toca el real)
  y espera la respuesta a `initialize`, con timeout acotado;
- **configuración**: TOML válido, sin claves desconocidas, reglas `[backends.run]` correctas,
  plugins importables y `allowed_backends` que existan;
- **`allowed_roots`**: existen y no caen en zonas protegidas (`~/Library/LaunchAgents`,
  `Keychains`…: fallo; componentes ocultos como `~/.local/bin`: aviso, porque suele ser a propósito);
- **`state_dir`**: escribible (sin crearlo);
- **backends**: cuáles están permitidos y disponibles;
- **registros de clientes**: si existe `claude`, lee `claude mcp list` (timeout de 30 s; si no
  está, se salta) y el JSON de Claude Desktop (`--claude-desktop-config` para otra ruta), y marca
  los registros de watcher-mcp/install-mcp que apuntan a un ejecutable que no existe, a un módulo
  que ya no se puede importar, que no conectan o que siguen usando el nombre `install-mcp`.

**No modifica ningún registro ni configuración**: solo lee y escribe en la salida el comando que
debes ejecutar (p. ej. `claude mcp remove install-mcp -s user && claude mcp add --scope user
watcher-mcp -- /ruta/.venv/bin/watcher-mcp`). `--no-server` y `--no-clients` omiten esas dos
comprobaciones (útil en CI).

## Tools

| Tool | Para qué |
|---|---|
| `backends()` | Qué instaladores hay, si están permitidos, sus límites y el esquema de sus `params`. |
| `install(backend, params, wait_s=0)` | Lanza el job y devuelve `job_id` al instante. |
| `status(job_id, tail=10)` | Estado, paso actual, pid, comando, segundos sin salida, código de salida, duración, límites, informe. |
| `list_jobs(state?, limit=20)` | Jobs recientes. |
| `logs(job_id, tail=50, since_seq?, stream=all)` | Últimas líneas; con `since_seq`, solo las nuevas. |
| `wait(job_id, timeout_s?)` | Espera acotada (60 s por defecto, tope duro 300 s): el informe, o "sigue en curso" con pid y últimas líneas. Emite `notifications/progress`. |
| `cancel(job_id)` | Cancela: SIGTERM y luego SIGKILL al grupo de procesos y a los hijos que se habían separado de él con `setsid`. |

Recursos: `job://<id>` (estado + informe) y `jobs://list`. Los avisos van a quien lanzó
el job (`notifications/message`, con el informe en `data.report`) y a quien esté
suscrito a `job://<id>` (`notifications/resources/updated`).

### Backends

Cubren los focos de interacción habituales de un agente de código. Todos aceptan además
`job_timeout_s` y `stall_timeout_s`. `backends()` devuelve el esquema completo de cada uno.

| Foco | Backend | Para qué | Ejemplo de `params` |
|---|---|---|---|
| Web | `download` | Fichero por HTTPS, checksum opcional, extracción segura de tar/zip (ver [zonas protegidas al extraer](#zonas-protegidas-al-extraer)) | `{"url": "https://…/fw.tar.gz", "dest": "~/fw", "extract": true, "checksum": "sha256:…"}` |
| Web | `binary` | Ejecutable suelto (checksum **obligatorio**), también dentro de un tar/zip | `{"url": "https://…/jq-linux-arm64", "checksum": "sha256:…", "name": "jq"}` |
| Código | `pip` | Paquetes Python en un venv aislado (uv si está) | `{"packages": ["pypdf"], "venv": "tools"}` |
| Código | `git` | Clonar/actualizar (https, ssh, local) sin prompts de credenciales | `{"url": "git@github.com:org/repo.git", "dest": "/abs/repo", "depth": 1}` |
| Código | `npm` | Paquetes en un prefijo aislado, o dependencias de un proyecto (npm ci/pnpm/yarn) | `{"project_dir": "/abs/web"}` |
| Código | `cargo` | `cargo install` en una raíz aislada | `{"crates": ["ripgrep@14.1.1"]}` |
| Código | `go` | `go install ruta@versión` en un GOBIN aislado | `{"packages": ["golang.org/x/tools/gopls@latest"]}` |
| Código | `maven` | Artefactos a `~/.m2`, o objetivos de un proyecto (allowlist) con `mvnw` si existe | `{"project_dir": "/abs/app", "goals": ["package"], "skip_tests": true}` |
| Código | `gradle` | Tareas de un proyecto (allowlist) con `gradlew` si existe | `{"project_dir": "/abs/app", "tasks": ["build"]}` |
| Sistema | `brew` | Fórmulas y casks sin auto-update ni prompts | `{"formulae": ["binwalk", "p7zip"]}` |
| Sistema | `apt` | apt-get (Debian/Ubuntu/WSL). **Privilegiado**: requiere `allow_privileged = true` | `{"packages": ["binwalk", "squashfs-tools"]}` |
| Sistema | `docker` | `docker pull` de imágenes | `{"images": ["python:3.12-slim"]}` |
| Comandos | `run` | Builds, tests y scripts largos con un programa de una allowlist o una regla `commands`. **Desactivado** por defecto | `{"program": "mvn", "args": ["-B", "verify"], "cwd": "/abs/app"}` |

Por defecto están permitidos todos menos `apt` y `run`. `backends()` dice cómo activar uno que no
lo está: en `how_to_enable` da la ruta del `config.toml` activo (`config_file`, y si existe) y el
fragmento TOML exacto para pegar (`toml`). Si `run` está permitido pero sin programas ni reglas,
también lo da. A mano:

```toml
allowed_backends = ["pip", "git", "download", "binary", "npm", "cargo", "go", "maven", "gradle",
                    "brew", "docker", "apt", "run"]
allow_privileged = true                  # solo si quieres apt (usa `sudo -n`: nunca pide contraseña)

[backends.run]
programs = ["mvn", "pytest", "make"]     # con cualquier argumento, en cualquier cwd permitido
# Reglas más estrechas: programa + prefijo de argumentos + carpeta de trabajo.
commands = [
  { program = "uv", args_prefix = ["run", "python", "tools/"], cwd_under = "~/Desktop/layout-mcp",
    inherit_env = ["GEMINI_API_KEY"] },
]
```

#### Reglas de `run`

Una petición se acepta si `program` está en `programs` **o** casa con alguna regla de `commands`:

- `program`: el mismo nombre (en PATH).
- `args_prefix`: los primeros argumentos deben ser exactamente esos; un elemento que acaba en `/`
  casa con cualquier argumento que empiece así y no tenga `..` (`"tools/"` admite
  `tools/gemini_compare.py`, no `tools/../x.py`). Lo que va después del prefijo es libre.
- `cwd_under`: el `cwd` (resuelto) debe ser esa carpeta o estar dentro. Sigue valiendo además
  `allowed_roots`.
- `inherit_env` (opcional): variables que se copian por nombre solo para esa regla.

Si nada casa, el error dice por qué falló cada regla, p. ej.
`commands[0] = uv run python tools/ (cwd bajo /Users/yo/Desktop/layout-mcp): args[2] = 'evil.py' no está bajo 'tools/'`.

#### Variables de entorno por nombre (secretos)

Para que un job reciba un secreto (`GEMINI_API_KEY`) sin escribir su valor en los params:

- `inherit_env = ["GEMINI_API_KEY"]` en la configuración: global (todos los jobs), por backend
  (`[backends.run] inherit_env`) o por regla de `commands`. Se copia del entorno del servidor MCP
  y, si no está ahí, de la shell de login (`$SHELL -lic env`, capturada **una vez** al primer job
  que la necesita; `login_shell_env = false` lo desactiva, `login_shell` elige otra shell). Si no
  aparece en ninguno, el job sigue y el log lo avisa.
- `env_from: ["GEMINI_API_KEY"]` en los params de `run`: la pide la petición, solo del entorno del
  servidor (p. ej. el `env` con que el cliente registra watcher-mcp). Si falta, el job se rechaza.
  La shell de login solo se consulta para nombres listados en la configuración.

Los valores se redactan (`***`) en logs, `status`, `logs`, notificaciones, `job.json` y el informe;
el registro del job guarda solo los nombres. No se aceptan `PATH`, `HOME`, `SHELL`, `LD_*`, `DYLD_*`…

### Zonas protegidas al extraer

`download` con `extract` valida **todo** el archivo antes de escribir y cada miembro por zonas, no por «cualquier punto»:

- siempre: dentro de las raíces y fuera de `protected_home_dirs` (`Library/LaunchAgents`…);
- si `dest` es HOME (o un ancestro): ningún componente oculto (`.zshrc`, `.ssh`, `.gitconfig`…);
- si `dest` es una carpeta de trabajo: los ocultos inocuos de un tarball normal (`.gitignore`,
  `.github/`, `.editorconfig`, `.vscode/`…) se extraen; se rechazan `.ssh`, `.gnupg`, `.aws`, `.azure`,
  `.kube`, `.docker`, `.config`, `.netrc`, `.npmrc`, `.pypirc`, `.yarnrc(.yml)`, `.pgpass`, `.my.cnf`,
  `.claude`, `.git/hooks` y `.git/config` (sin distinguir mayúsculas).

### Límites

| Qué | Por defecto | Al vencer |
|---|---|---|
| `wait` | 60 s (`default_wait_s`), tope duro 300 s (`max_wait_s`) | `done: false` + estado, pid, `silent_for_s`, últimas líneas |
| Tiempo en cola | 3600 s (`max_queue_s`) | `timed_out` |
| Duración del job | 3600 s (`default_job_timeout_s`); tope duro 24 h (`max_job_timeout_s`), también con "sin límite" | SIGTERM al grupo, 5 s, SIGKILL; `timed_out` |
| Sin salida | 300 s (`default_stall_timeout_s`; pip 600 s) | `stalled` + aviso con pid, comando y últimas líneas (no mata el proceso) |

Se fijan por petición (pip: `job_timeout_s`, `stall_timeout_s`), por backend en la
configuración (`[backends.pip] job_timeout_s = 1800`) o globalmente.

### Parámetros del backend `pip`

| Param | Ejemplo | Notas |
|---|---|---|
| `packages` | `["pypdf", "requests>=2.32"]` | PEP 508 sin URLs; nada que empiece por `-`. |
| `requirements` | `"/abs/requirements.txt"` | Alternativa o complemento a `packages`. Dentro de `allowed_roots` y fuera de zonas protegidas (pip/uv copian en su error las líneas que no entienden). Justo antes de lanzar pip se lee **una vez** (con lo que incluye por `-r`/`-c`), se normaliza y pip recibe una copia privada (`<state_dir>/requirements/<id>/`), nunca el original: solo se admiten requisitos sin URL ni rutas, `--index-url`/`--extra-index-url`/`-f` con https (o un directorio permitido), `-r`/`-c` a ficheros permitidos y los flags habituales (`--no-index`, `--pre`, `--hash=…`…); `-e`, variables `${VAR}` y lo demás se rechazan. Así un cambio del fichero mientras el job espera en la cola no cuenta. |
| `venv` | `"tools"` o `"/abs/ruta/.venv"` | Un nombre crea `<state_dir>/venvs/<nombre>`; una ruta debe estar dentro de `allowed_roots`. |
| `python` | `"3.12"` o `"/abs/python3"` | Versión con uv (o `python3.12` en PATH). Una ruta debe llamarse `python3…`. |
| `installer` | `"auto"` / `"uv"` / `"pip"` | `auto` = uv si está. |
| `upgrade`, `pre` | `true` | |
| `index_url`, `extra_index_urls` | `"https://…/simple"` | Solo https; credenciales redactadas. |
| `find_links`, `no_index` | `["/abs/wheels"]`, `true` | Instalación offline. |
| `job_timeout_s` | `1800` | Al vencer: `timed_out` (0 = hasta el tope duro `max_job_timeout_s`). |
| `stall_timeout_s` | `120` | Sin salida N s → `stalled` (0 = sin watchdog). |

## Configuración

Ver [examples/config.example.toml](examples/config.example.toml):

- `allowed_backends`: la allowlist (por defecto, todos menos `apt` y `run`; ver [Backends](#backends)).
- `allow_privileged`: `false` por defecto.
- `allowed_roots`: dónde pueden escribir (y leer ficheros pedidos) los backends.
- `protect_dot_paths` (`true`) y `protected_home_dirs` (subrutas de HOME con comodines; por defecto
  las sensibles de `~/Library`: `LaunchAgents`, `Preferences`, `Keychains`, `Cookies`, `Mail`,
  `Messages`, `Safari`, `Containers`, `Group Containers`, `Application Support/com.apple.*`): zonas
  protegidas dentro de las raíces. Para usar una a propósito, añádela como raíz explícita (p. ej.
  `~/.local/bin`). Las subrutas de HOME valen aunque la raíz permitida sea un ancestro de HOME
  (`/Users`, `/`). **Excepción**: los worktrees de Claude Code, `<repo>/.claude/worktrees/<nombre>/…`,
  se aceptan (ahí viven las sesiones lanzadas con worktree); dentro del worktree siguen
  protegidos `.git`, `.env`, `.claude/settings*`… y fuera de `worktrees/<nombre>`, `.claude` también.
- `max_job_timeout_s` (24 h): tope duro de cualquier job. `func_cancel_grace_s` (75 s): cuánto se
  espera a que un paso interno cancelado (una descarga) suelte su hilo antes de liberar su lock.
- Límites: `max_parallel`, `default_wait_s`, `max_wait_s`, `max_queue_s`,
  `default_job_timeout_s`, `default_stall_timeout_s`, `default_io_mode`.
- `[backends.<nombre>]`: límites e `io_mode` propios de un backend (y `inherit_env`).
- `inherit_env`, `login_shell_env` (`true`), `login_shell`: variables copiadas por nombre en el
  entorno de los jobs, con el valor redactado (ver [Reglas de `run`](#reglas-de-run)).
- `plugins`: backends externos, como `"paquete.modulo:Clase"`.

`WATCHER_MCP_STATE_DIR` cambia el directorio de estado (por defecto
`~/.local/state/watcher-mcp`).

### Aviso por sessionbus (opcional)

Claude Code no le enseña al modelo las `notifications/message` de MCP: la sesión que lanzó un job
no se entera de que terminó hasta que llama a `wait`/`status`. Con
[sessionbus](https://github.com/Eliaxs1900/sessionbus-mcp) instalado, watcher-mcp puede publicar el
fin del job en el bus y el hook de sessionbus se lo inyecta a esa sesión. Está **desactivado por
defecto** y watcher-mcp no depende de sessionbus:

```toml
[sessionbus]
enabled = true
# command = ["/ruta/a/sessionbus"]     # por defecto: `sessionbus` en el PATH o
#                                      # ~/Desktop/sessionbus-mcp/.venv/bin/sessionbus
# events = ["finished", "stalled"]     # por defecto solo ["finished"]
# priorities = { succeeded = "normal", failed = "alerta" }   # ver la tabla de abajo
```

Qué llega: una trama `info` en `nodo/<sesión que lanzó el job>/jobs/<job_id>` (solo a esa sesión,
nunca a `todos/**`; caduca en un día), con el titular del informe como resumen (≤ 200 caracteres) y
en el cuerpo (≤ 16 KB, redactado) el estado, `exit_code`, la duración, el `error`, las
`relevant_lines`, `log_file` y `job://<id>`. Prioridades por defecto: `normal` si termina bien o se
cancela, `alerta` si falla, se agota el tiempo o se atasca (`baja` no se inyecta nunca en la sesión,
solo queda en su bandeja).

Cómo funciona: cada sesión de Claude Code arranca su propio servidor; su
`CLAUDE_CODE_HOST_SESSION_ID` (o `CLAUDE_CODE_SESSION_ID`) es el nodo del bus. El aviso sale con un
subproceso `sessionbus publish` (sin shell, `timeout_s` ≤ 5 s, en segundo plano) lanzado **sin esas
variables** y con `SESSIONBUS_NODE_NAME=watcher-mcp`; el remitente es `cli-watcher-mcp`. (Si
publicara como la propia sesión, el bus no se lo entregaría a ella.) Si falta el CLI o la identidad,
se cuelga o falla, el job termina igual y a tiempo: el fallo solo queda en el log del job y en
stderr (la falta de identidad se dice una sola vez).

## Pruebas selectivas: puntuales y E2E

Hay dos niveles de prueba:

- **Puntuales**: validación, planes y núcleo con backends falsos. Son rápidas.
- **E2E** (marcador `e2e`): herramientas y servicios reales (pip/uv, git, HTTPS local, cliente MCP, red).

Si un cambio no toca una parte, se omite su prueba. Lo decide [`scripts/select_tests.py`](scripts/select_tests.py)
con un mapa explícito fichero → tests:

| Cambio | Se ejecuta |
|---|---|
| Núcleo (`jobs`, `process`, `config`, `security`, `server`, `notify`, base de backends, `conftest`, `pyproject`, workflow) | todo |
| Un backend (p. ej. `backends/git.py`) | solo sus tests, separados en puntuales y E2E |
| Solo documentación, `examples/`, demos, `Dockerfile` | nada |
| Un fichero que no está en el mapa | todo (un olvido nunca omite pruebas) |

En CI:

- en un PR, las puntuales seleccionadas corren en la matriz completa y las E2E seleccionadas en un Python por sistema;
- en `main` se ejecuta siempre todo.
- el check `resultado` resume los demás (pasa si todo lo ejecutado pasó; un nivel omitido cuenta como bien). Es el único check obligatorio para integrar en `main`, porque los nombres de la matriz cambian según la selección.

En local:

```bash
python scripts/select_tests.py --base origin/main --tiers         # qué tocaría
.venv/bin/python -m pytest -q -m "not e2e"                          # todas las puntuales
.venv/bin/python -m pytest -q -m "not e2e" $(python scripts/select_tests.py --base origin/main)
```

### Pruebas verificadas en local (mantenedores)

Si ya pasaste la suite completa en tu máquina, no tiene sentido que GitHub la repita. Un
**mantenedor** (rol `admin` o `maintain`) puede certificarlo:

```bash
python scripts/verificar_local.py            # el PR de la rama actual (--pr N, --pythons 3.12, --no-publicar)
```

Necesita `git`, `gh` (con sesión) y `uv`. El script:

1. toma lo que GitHub iba a probar: la mezcla base + cabeza del PR (`refs/pull/N/merge`) y su **árbol**;
2. la extrae a un worktree temporal (nunca tu árbol de trabajo, con sus cambios sin commitear);
3. ejecuta la suite **completa** (puntuales + E2E) con Python 3.11, 3.12 y 3.13;
4. si todo pasa, publica el commit status `tests-locales` = `success` en la cabeza del PR (la descripción lleva
   el árbol probado, el SO, los Pythons y el número de tests);
5. cancela y relanza las ejecuciones de `tests.yml` de esa cabeza.

Entonces el job `select` ve el status y deja `puntuales=NONE`, `e2e=NONE`; `resultado` sale en verde y
dice de dónde viene («verificado en local por X en Darwin-arm64»). Lo mismo en el push a `main`: si el PR
mergeado tenía un `tests-locales` válido con el mismo árbol que el commit empujado, tampoco se repite.

Qué cubre y qué no: solo **el sistema operativo de quien lo lanza** (la CI completa cubre Linux y macOS), y solo
ese árbol exacto: si el PR cambia, o `main` se mueve y GitHub recalcula la mezcla, el árbol ya no coincide y la CI
corre entera.

Modelo de confianza: el mantenedor lo certifica con sus credenciales. La CI solo omite si **todo** esto se cumple
(`scripts/ci_local.py`): el status lo puso una persona con rol `admin` o `maintain` (se consulta con la API, no
con lo que diga el PR), el PR es del propio repositorio (no de un fork) y el árbol anotado es el que GitHub probaría.
Una etiqueta o un trailer en el commit **no** cuentan: los puede poner el autor del PR. En un PR la CI ejecuta la
versión de `ci_local.py` de la **base**, no la del PR, así que un PR no puede cambiar las reglas con las que se
juzga. Ante cualquier duda o fallo de la consulta: CI completa, como siempre. El check obligatorio sigue siendo `resultado`.

## Tests y demos en Docker (sin ensuciar tu máquina)

```bash
docker build --target test -t watcher-mcp:test .
docker run --rm watcher-mcp:test
```

Los tests sin red (más de 200) cubren:
- núcleo: jobs en paralelo, logs en vivo, cancelación, `stalled`, `timed_out`, `wait`
  acotado, pipe frente a pty, notificaciones de punta a punta por MCP y redacción de
  secretos;
- procesos: líneas de más de 8 KiB sin pérdida, sondas con tope de salida, hijos que se separan
  del grupo con `setsid` y entorno de los jobs sin las credenciales del servidor;
- el informe de fallo: la causa antes que la traza (Maven, Gradle, Python, npm) y `built`/`stale`;
- validación y plan de los 13 backends, incluidas las propiedades `-D` de Maven;
- el aviso por sessionbus, con un CLI falso;
- instalaciones reales offline: pip/uv con un wheel local, git con repos locales,
  descargas y binarios contra un servidor HTTPS local (checksum, extracción segura,
  redirección a http rechazada, atasco y cancelación) y `run`.

Otros 6 hacen instalaciones reales contra internet (pip, git, download, apt, npm y go).
Se ejecutan en su propio contenedor:

```bash
docker build --target integration -t watcher-mcp:integration .
docker run --rm watcher-mcp:integration
```

Para regenerar las demos (graban en un pty dentro del contenedor y renderizan GIF/PNG):

```bash
docker build --target demo -t watcher-mcp:demo .
docker run --name imcp-demos watcher-mcp:demo
docker cp imcp-demos:/out/. docs/demos/
docker rm imcp-demos
```

Sin Docker: `uv pip install -e ".[dev]"` y `.venv/bin/python -m pytest -q`. Las demos
también se pueden ejecutar directamente: `python examples/demo.py stall`,
`python examples/compare.py`.

## Estructura

```
src/watcher_mcp/
  server.py              tools y recursos MCP, espera acotada con progreso
  cli.py                 modo CLI: run, status, logs, list (mismo núcleo, sin cliente MCP)
  doctor.py              watcher-mcp doctor (diagnóstico) y el aviso del alias install-mcp
  jobs.py                núcleo: jobs, cola, límites, watchdog, logs, informe
  logsummary.py          relevant_lines: causa del fallo sin trazas ni texto de ayuda
  process.py             exec sin shell, lectura en vivo (pipe/pty/stdbuf), terminación acotada
  notify.py              eventos -> notifications/message, resources/updated, subscriptions/listen
  busbridge.py           aviso opcional del fin de job por sessionbus (desactivado por defecto)
  security.py            redacción, https, checksums, raíces permitidas
  config.py              configuración
  backends/base.py       interfaz Backend / Plan / CommandStep / FuncStep
  backends/pip.py        pip/uv            backends/git.py         git
  backends/download.py   download, binary  backends/ecosystems.py  npm, cargo, go
  backends/jvm.py        maven, gradle     backends/system.py      brew, apt, docker, run
  backends/_template.py  esqueleto para nuevos backends
docs/design.md           diseño
docs/demos/              GIF, PNG y .cast de las demos
examples/                demos, comparativa, slowpkg, cliente mínimo y configuración
scripts/                 grabación (pty -> .cast) y render (.cast -> GIF/PNG)
tests/                   pytest
```

## Limitaciones

- `wsl` (delegar un backend dentro de una distro) está diseñado pero no implementado.
- `brew`, `cargo`, `maven`, `gradle` y `docker` tienen tests de validación y plan, pero
  no de instalación real en CI.
- Funciona en macOS y Linux (también WSL). En Windows nativo faltaría la cancelación
  por grupo de procesos.
- Los jobs en curso no sobreviven a un reinicio del servidor: se marcan como interrumpidos y sus
  procesos se terminan al arrancar la siguiente instancia (en macOS, hasta entonces siguen vivos).
- `binary` ejecuta el binario descargado para leer su versión: instalar un ejecutable de Internet
  implica confiar en él (el checksum lo da la misma petición).
- Filtrar las credenciales del entorno de los jobs no es una barrera completa: un job puede leer el
  entorno del propio servidor (`ps eww -p $PPID` en macOS, `/proc/$PPID/environ` en Linux). Solo
  evita que se hereden por descuido; quien ejecuta código arbitrario (un `setup.py`, un `run`) corre
  con los permisos del usuario.
- Un proceso que se separa del job con un doble fork (ya adoptado por init antes de cancelar) no se
  puede atribuir al job: si sobrevive a un `cancel`, no se mata.
- Usa el SDK `mcp` 2.x (`MCPServer`, el sucesor de FastMCP).

## Licencia

[MIT](LICENSE)
