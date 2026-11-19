"""Verifica un PR en local y publica el status ``tests-locales`` para que la CI no repita las pruebas.

Solo para mantenedores (rol admin o maintain). Necesita ``git``, ``gh`` (con sesión) y ``uv``.

    python scripts/verificar_local.py                 # el PR de la rama actual
    python scripts/verificar_local.py --pr 23
    python scripts/verificar_local.py --pythons 3.12  # solo uno
    python scripts/verificar_local.py --no-publicar   # prueba, pero no publica ni relanza la CI

Qué hace:

1. Fija lo que GitHub iba a probar: la mezcla base + cabeza del PR (``refs/pull/N/merge``) y su árbol.
2. La extrae a un worktree temporal (nunca el árbol de trabajo, con sus cambios sin commitear).
3. Ejecuta la suite COMPLETA (puntuales + e2e) con cada versión de Python, con uv.
4. Si todo pasa y quien lo lanza es mantenedor, publica el status ``tests-locales`` = success en la
   cabeza del PR (la descripción lleva el árbol probado, el SO, los Pythons y el nº de tests).
5. Cancela y relanza las ejecuciones de ``tests.yml`` de esa cabeza para que ``select`` vea el status.

Solo certifica el SO de esta máquina; la CI completa sigue siendo la referencia para los demás.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_local  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYTHONS = "3.11,3.12,3.13"


class Abort(Exception):
    pass


def run(cmd: list[str], *, cwd: Path | None = None, timeout: float = 120, check: bool = True) -> str:
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise Abort(f"falta {cmd[0]!r} en el PATH") from None
    if check and res.returncode != 0:
        raise Abort(f"{' '.join(cmd)} falló: {(res.stderr or res.stdout).strip()[-400:]}")
    return res.stdout.strip()


def gh_json(args: list[str]) -> dict:
    return json.loads(run(["gh", *args]))


def pr_info(number: str | None) -> dict:
    fields = "number,headRefOid,baseRefName,url,isCrossRepository,state,mergeable"
    return gh_json(["pr", "view", *([number] if number else []), "--json", fields])


def wait_mergeable(number: int, tries: int = 15) -> str:
    """GitHub calcula la mezcla en segundo plano: se espera a que no sea UNKNOWN."""
    for _ in range(tries):
        state = gh_json(["pr", "view", str(number), "--json", "mergeable"])["mergeable"]
        if state != "UNKNOWN":
            return state
        time.sleep(2)
    return "UNKNOWN"


def fix_merge(pr: dict) -> tuple[str, str, str]:
    """(commit de la mezcla, árbol, base) EXACTAMENTE lo que probará GitHub."""
    n = pr["number"]
    if wait_mergeable(n) != "MERGEABLE":
        raise Abort(f"el PR #{n} no es mezclable ahora mismo (resuelve conflictos o espera a GitHub)")
    # Cada ref por separado: FETCH_HEAD apunta solo al último refspec pedido.
    run(["git", "fetch", "-q", "origin", f"refs/pull/{n}/merge"], cwd=ROOT)
    merge = run(["git", "rev-parse", "FETCH_HEAD"], cwd=ROOT)
    parents = run(["git", "rev-list", "--parents", "-n", "1", merge], cwd=ROOT).split()[1:]
    if pr["headRefOid"] not in parents:
        raise Abort("la mezcla de GitHub no incluye la cabeza actual del PR: ¿falta hacer push, o GitHub "
                    "no ha recalculado todavía? Vuelve a intentarlo en unos segundos.")
    run(["git", "fetch", "-q", "origin", pr["baseRefName"]], cwd=ROOT)
    base = run(["git", "rev-parse", "FETCH_HEAD"], cwd=ROOT)
    if base not in parents:
        raise Abort(f"la mezcla de GitHub es sobre otra base que origin/{pr['baseRefName']} "
                    "(main se movió): espera a que GitHub la recalcule y repite.")
    return merge, run(["git", "rev-parse", f"{merge}^{{tree}}"], cwd=ROOT), base


def run_suite(worktree: Path, python: str) -> tuple[bool, int, str]:
    """Suite completa con una versión de Python. Devuelve (pasó, nº de tests que pasaron, cola de la salida)."""
    venv = worktree / f".venv-{python}"
    run(["uv", "venv", "--python", python, str(venv)], cwd=worktree, timeout=600)
    run(["uv", "pip", "install", "--python", str(venv / "bin" / "python"), "-e", ".[dev]"], cwd=worktree, timeout=900)
    res = subprocess.run([str(venv / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                         cwd=worktree, capture_output=True, text=True, timeout=1800)
    tail = (res.stdout or "")[-1500:]
    m = re.search(r"(\d+) passed", tail)
    return res.returncode == 0, int(m.group(1)) if m else 0, tail


def relaunch_ci(repo: str, sha: str) -> list[str]:
    """Cancela y relanza las ejecuciones de tests.yml de esa cabeza (también las ya terminadas)."""
    runs = json.loads(run(["gh", "run", "list", "--repo", repo, "--workflow", "tests.yml", "--commit", sha,
                           "--json", "databaseId,status,url", "--limit", "10"]))
    urls = []
    for item in runs:
        rid = str(item["databaseId"])
        if item["status"] != "completed":
            run(["gh", "run", "cancel", rid, "--repo", repo], check=False)
            for _ in range(30):  # una ejecución cancelándose aún no admite rerun
                if json.loads(run(["gh", "run", "view", rid, "--repo", repo, "--json", "status"]))["status"] == "completed":
                    break
                time.sleep(2)
        run(["gh", "run", "rerun", rid, "--repo", repo])
        urls.append(item["url"])
    return urls


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", help="número del PR (por defecto, el de la rama actual)")
    ap.add_argument("--pythons", default=DEFAULT_PYTHONS, help=f"versiones separadas por comas (por defecto {DEFAULT_PYTHONS})")
    ap.add_argument("--no-publicar", action="store_true", help="solo probar: no publica el status ni relanza la CI")
    args = ap.parse_args(argv)
    pythons = [p.strip() for p in args.pythons.split(",") if p.strip()]
    try:
        repo = gh_json(["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]
        login = run(["gh", "api", "user", "--jq", ".login"])
        role = ci_local.fetch_role(repo, login)
        if not args.no_publicar and role not in ci_local.MAINTAINER_ROLES:
            raise Abort(f"{login} no es mantenedor de {repo} (rol: {role}): el status no valdría. "
                        "Usa --no-publicar para probar sin publicar.")
        pr = pr_info(args.pr)
        if pr["state"] != "OPEN":
            raise Abort(f"el PR #{pr['number']} no está abierto")
        if pr["isCrossRepository"] and not args.no_publicar:
            raise Abort("el PR viene de un fork: la CI no omite pruebas en forks (CI completa)")
        merge, tree, base = fix_merge(pr)
        print(f"PR #{pr['number']} {pr['url']}\n  cabeza {pr['headRefOid'][:10]}  base {base[:10]}  "
              f"mezcla {merge[:10]}  árbol {tree[:10]}")
        tmp = Path(tempfile.mkdtemp(prefix="watcher-mcp-verificar-"))
        worktree = tmp / "arbol"
        run(["git", "worktree", "add", "--detach", str(worktree), merge], cwd=ROOT)
        try:
            counts = []
            for py in pythons:
                t0 = time.time()
                print(f"\n== Python {py}: suite completa ==", flush=True)
                ok, n, tail = run_suite(worktree, py)
                print(f"   {'OK' if ok else 'FALLÓ'}: {n} pasan en {time.time() - t0:.0f}s")
                if not ok:
                    print(tail)
                    raise Abort(f"fallaron las pruebas con Python {py}; no se publica nada")
                counts.append(n)
        finally:
            run(["git", "worktree", "remove", "--force", str(worktree)], cwd=ROOT, check=False)
            shutil.rmtree(tmp, ignore_errors=True)
        if args.no_publicar:
            print("\nTodo en verde (--no-publicar: no se publica nada).")
            return 0
        os_name = f"{platform.system()}-{platform.machine()}"
        desc = ci_local.build_description(tree, base, os_name, pythons, min(counts), login)
        run(["gh", "api", "-X", "POST", f"repos/{repo}/statuses/{pr['headRefOid']}", "-f", "state=success",
             "-f", f"context={ci_local.CONTEXT}", "-f", f"description={desc}"])
        print(f"\nStatus {ci_local.CONTEXT} publicado en {pr['headRefOid'][:10]}: {desc}")
        for url in relaunch_ci(repo, pr["headRefOid"]):
            print(f"CI relanzada: {url}")
        return 0
    except Abort as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
