"""Pruebas selectivas: qué tests ejecutar según los ficheros cambiados.

Si un cambio no toca una parte, se omite su prueba. La regla es explícita (un mapa
fichero -> tests) porque la detección por imports no sirve aquí: ``conftest.py`` importa el
registro de backends, que importa todos, así que cualquier cambio "afectaría" a todo.

Seguro por defecto:

* el núcleo (jobs, process, config, security, server, notify, base de backends, conftest,
  pyproject, el workflow y este mismo script) ejecuta **todo**;
* un fichero que no aparece en el mapa ejecuta **todo** (un olvido nunca omite pruebas);
* solo documentación y herramientas de demos no ejecutan **nada**.

Dos niveles: **puntuales** (validación, planes, núcleo con backends falsos: rápidas, en toda
la matriz) y **E2E** (marcador ``e2e``: pip/uv, git, HTTPS local, cliente MCP, red). En local::

    pytest -m "not e2e" $(python scripts/select_tests.py --base origin/main)   # rápido

Uso::

    python scripts/select_tests.py --base origin/main      # cambios de la rama frente a main
    python scripts/select_tests.py src/watcher_mcp/backends/git.py README.md
    python scripts/select_tests.py --base origin/main --tiers   # puntuales=… / e2e=…

Imprime ``ALL``, ``NONE`` o la lista de ficheros de test separados por espacios.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys

ALL = "ALL"
NONE = "NONE"

# Cambios que pueden afectar a cualquier test.
CORE = [
    "src/watcher_mcp/jobs.py",
    "src/watcher_mcp/process.py",
    "src/watcher_mcp/config.py",
    "src/watcher_mcp/security.py",
    "src/watcher_mcp/envsource.py",
    "src/watcher_mcp/server.py",
    "src/watcher_mcp/notify.py",
    "src/watcher_mcp/logsummary.py",  # lo usa el informe de cualquier job
    "src/watcher_mcp/__init__.py",
    "src/watcher_mcp/backends/__init__.py",
    "src/watcher_mcp/backends/base.py",
    "src/watcher_mcp/backends/common.py",
    "tests/conftest.py",
    "tests/__init__.py",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/*",
    "scripts/select_tests.py",
]

# Cambios que no afectan a ningún test de pytest (la CI no los usa).
NO_TESTS = [
    "*.md",
    "docs/*",
    "LICENSE",
    "examples/*",
    "scripts/make_demos.sh",
    "scripts/record_cast.py",
    "scripts/render_cast.py",
    "Dockerfile",
    ".dockerignore",
    ".gitignore",
]

# Módulos concretos -> tests que los ejercitan.
MAP: dict[str, list[str]] = {
    "src/watcher_mcp/__main__.py": ["tests/test_server.py"],
    "src/watcher_mcp/busbridge.py": ["tests/test_busbridge.py"],
    "src/watcher_mcp/backends/pip.py": ["tests/test_pip.py", "tests/test_backends.py", "tests/test_revision.py"],
    "src/watcher_mcp/backends/download.py": [
        "tests/test_web_git_run.py", "tests/test_backends.py", "tests/test_revision.py", "tests/test_network.py",
    ],
    "src/watcher_mcp/backends/git.py": ["tests/test_web_git_run.py", "tests/test_backends.py", "tests/test_revision.py"],
    "src/watcher_mcp/backends/system.py": [
        "tests/test_web_git_run.py", "tests/test_backends.py", "tests/test_revision.py", "tests/test_run_rules_env.py",
    ],
    "src/watcher_mcp/backends/ecosystems.py": ["tests/test_backends.py", "tests/test_revision.py"],
    "src/watcher_mcp/backends/jvm.py": [
        "tests/test_backends.py", "tests/test_revision.py", "tests/test_issues_4_5.py", "tests/test_report_12_14.py",
        "tests/test_maven_props.py",
    ],
    "src/watcher_mcp/backends/_template.py": ["tests/test_backends.py"],
    # test_limits importa helpers de test_core.
    "tests/test_core.py": ["tests/test_core.py", "tests/test_limits.py"],
    "tests/test_report_12_14.py": ["tests/test_report_12_14.py"],
    "tests/test_maven_props.py": ["tests/test_maven_props.py"],
    "tests/test_core_audit.py": ["tests/test_core_audit.py"],
    "scripts/ci_local.py": ["tests/test_ci_local.py"],
    "scripts/verificar_local.py": ["tests/test_ci_local.py"],
    "tests/test_ci_local.py": ["tests/test_ci_local.py"],
    "tests/test_select_tests.py": ["tests/test_select_tests.py"],
}


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def select(changed: list[str]) -> str:
    """``ALL``, ``NONE`` o los tests a ejecutar, separados por espacios y ordenados."""
    selected: set[str] = set()
    for path in (p.strip() for p in changed):
        if not path:
            continue
        if _matches(path, CORE):
            return ALL
        if path in MAP:
            selected.update(MAP[path])
        elif fnmatch.fnmatch(path, "tests/test_*.py"):
            selected.add(path)  # un test cambiado se prueba a sí mismo
        elif _matches(path, NO_TESTS):
            continue
        else:
            return ALL  # desconocido: seguro por defecto
    return " ".join(sorted(selected)) if selected else NONE


# Pruebas E2E (marcador ``e2e``): herramientas y servicios reales. El resto son puntuales.
E2E_FILES = frozenset({
    "tests/test_pip.py", "tests/test_web_git_run.py", "tests/test_server.py", "tests/test_network.py",
})


def select_tiers(changed: list[str]) -> tuple[str, str]:
    """(puntuales, e2e): cada nivel es ``ALL``, ``NONE`` o su lista de ficheros de test."""
    chosen = select(changed)
    if chosen in (ALL, NONE):
        return chosen, chosen
    files = chosen.split()
    unit = [f for f in files if f not in E2E_FILES]
    e2e = [f for f in files if f in E2E_FILES]
    return (" ".join(unit) or NONE), (" ".join(e2e) or NONE)


def changed_since(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"], capture_output=True, text=True, check=True, timeout=60,
    )
    return out.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="ref base (p. ej. origin/main): usa git diff base...HEAD")
    parser.add_argument("paths", nargs="*", help="ficheros cambiados (si no se usa --base)")
    parser.add_argument("--tiers", action="store_true",
                        help="imprime 'puntuales=…' y 'e2e=…' (formato de $GITHUB_OUTPUT)")
    args = parser.parse_args(argv)
    try:
        changed = changed_since(args.base) if args.base else args.paths
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"no se pudo calcular el diff ({exc}); se ejecuta todo", file=sys.stderr)
        changed = ["(diff no disponible)"]  # desconocido -> ALL
    if args.tiers:
        unit, e2e = select_tiers(changed)
        print(f"puntuales={unit}")
        print(f"e2e={e2e}")
    else:
        print(select(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
