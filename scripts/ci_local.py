"""Pruebas verificadas en local: la decisión de la CI de no repetirlas (solo stdlib, más ``gh``).

Un mantenedor ejecuta la suite completa en su máquina (``scripts/verificar_local.py``) y publica un
*commit status* ``tests-locales`` = ``success`` en la cabeza del PR. El job ``select`` de
``.github/workflows/tests.yml`` llama a ``decide`` y, si el status vale, no repite las pruebas.

Modelo de confianza: el status solo cuenta si lo puso un **mantenedor** (rol ``admin`` o
``maintain`` en el repositorio, consultado con las credenciales de la CI), si el PR es del propio
repositorio y si el **árbol** que anota en su descripción es el que GitHub iba a probar (el de la
mezcla base + cabeza). Una etiqueta o un trailer en el commit no valen: los puede poner quien abre el PR.
Si algo no cuadra o falla la consulta, no se omite nada: la CI completa de siempre.

La decisión es pura (``decide``); ``main`` solo habla con ``gh``. En un PR, la CI ejecuta la versión
de este fichero que hay en la **base** (no la del PR): un PR no puede cambiar las reglas con las que se
juzga a sí mismo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

CONTEXT = "tests-locales"
MAINTAINER_ROLES = frozenset({"admin", "maintain"})
MAX_DESCRIPTION = 140  # límite de GitHub para la descripción de un status
_TREE = re.compile(r"\btree=([0-9a-f]{40})\b")
_FIELD = re.compile(r"\b(os|py|n|by)=([\w.,+-]+)")


@dataclass
class Decision:
    skip: bool
    reason: str
    who: str = ""
    os: str = ""
    details: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if self.skip:
            return f"verificado en local por {self.who} en {self.os or '?'} ({self.reason})"
        return f"CI completa: {self.reason}"


def build_description(tree: str, base: str, os_name: str, pythons: Iterable[str], tests: int, who: str) -> str:
    """Descripción del status (≤ 140 caracteres): lleva el árbol probado y lo que hace falta para auditarlo."""
    text = f"ok tree={tree} base={base[:8]} os={os_name} py={','.join(pythons)} n={tests} by={who}"
    if len(text) > MAX_DESCRIPTION:  # un login largo no debe romper el status: se acorta el resto
        text = f"ok tree={tree} os={os_name} py={','.join(pythons)} n={tests}"[:MAX_DESCRIPTION]
    return text


def parse_description(description: str | None) -> dict[str, str] | None:
    """``{'tree': …, 'os': …, …}`` o None si no lleva un árbol válido."""
    if not description or not (m := _TREE.search(description)):
        return None
    out = {"tree": m.group(1)}
    out.update({k: v for k, v in _FIELD.findall(description)})
    return out


def decide(
    statuses: Iterable[Mapping[str, Any]],
    tree: str,
    *,
    same_repo: bool,
    role_of: Callable[[str], str | None],
) -> Decision:
    """¿Se puede omitir la CI? ``statuses``: los de la cabeza del PR, el más reciente primero (como los
    devuelve la API); solo cuenta el último ``tests-locales``. ``role_of(login)``: rol en el repo o None."""
    if not same_repo:
        return Decision(False, "el PR viene de un fork")
    latest = next((s for s in statuses if s.get("context") == CONTEXT), None)
    if latest is None:
        return Decision(False, f"no hay status {CONTEXT}")
    if latest.get("state") != "success":
        return Decision(False, f"el último {CONTEXT} es {latest.get('state')!r}")
    info = parse_description(latest.get("description"))
    if info is None:
        return Decision(False, f"la descripción de {CONTEXT} no lleva un árbol")
    if info["tree"] != tree:
        return Decision(False, f"el árbol probado en local ({info['tree'][:10]}…) no es el que probaría GitHub "
                               f"({tree[:10]}…)")
    creator = latest.get("creator") or {}
    login = creator.get("login") or ""
    if not login or creator.get("type") not in (None, "User"):
        return Decision(False, f"el status no lo puso una persona ({login or 'sin autor'})")
    role = role_of(login)
    if role not in MAINTAINER_ROLES:
        return Decision(False, f"{login} no es mantenedor (rol: {role or 'ninguno'})")
    return Decision(True, f"árbol {tree[:10]}…, rol {role}", who=login, os=info.get("os", ""), details=info)


# --------------------------------------------------------------------------- acceso a GitHub (gh)
Runner = Callable[[list[str]], str]


def gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True, timeout=60).stdout


def fetch_statuses(repo: str, sha: str, run: Runner | None = None) -> list[dict[str, Any]]:
    return json.loads((run or gh)(["api", f"repos/{repo}/commits/{sha}/statuses?per_page=100"]))


def fetch_role(repo: str, login: str, run: Runner | None = None) -> str | None:
    try:
        return (run or gh)(["api", f"repos/{repo}/collaborators/{login}/permission", "--jq", ".role_name"]).strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def decide_for_pull_request(repo: str, head_sha: str, tree: str, *, same_repo: bool, run: Runner | None = None) -> Decision:
    return decide(fetch_statuses(repo, head_sha, run), tree, same_repo=same_repo,
                  role_of=lambda login: fetch_role(repo, login, run))


def decide_for_push(repo: str, sha: str, tree: str, run: Runner | None = None) -> Decision:
    """Commit empujado a main: se busca el PR que lo introdujo y se juzga el status de su cabeza.
    El árbol de un squash (o de un merge) coincide con el de la mezcla verificada si main no se movió."""
    pulls = json.loads((run or gh)(["api", f"repos/{repo}/commits/{sha}/pulls?per_page=30"]))
    pr = next((p for p in pulls if p.get("merged_at") and p.get("merge_commit_sha") == sha), None)
    if pr is None:
        return Decision(False, "el commit no viene de un PR mergeado")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    return decide_for_pull_request(repo, pr["head"]["sha"], tree, same_repo=head_repo == repo, run=run)


def _write_outputs(decision: Decision) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    lines = [f"skip={'true' if decision.skip else 'false'}", f"local={decision.summary()}"]
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and decision.skip:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Pruebas omitidas\n\n{decision.summary()}\n\n"
                     f"Descripción del status: `{json.dumps(decision.details)}`\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide", help="decide si la CI puede omitir las pruebas (escribe skip= y local= en $GITHUB_OUTPUT)")
    d.add_argument("--event", choices=("pull_request", "push"), required=True)
    d.add_argument("--repo", required=True)
    d.add_argument("--sha", required=True, help="cabeza del PR (pull_request) o commit empujado (push)")
    d.add_argument("--tree", required=True, help="árbol que GitHub va a probar")
    d.add_argument("--same-repo", choices=("true", "false"), default="false")
    args = parser.parse_args(argv)
    try:
        if args.event == "pull_request":
            decision = decide_for_pull_request(args.repo, args.sha, args.tree, same_repo=args.same_repo == "true")
        else:
            decision = decide_for_push(args.repo, args.sha, args.tree)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:  # ante la duda, CI completa
        decision = Decision(False, f"no se pudo consultar GitHub ({exc.__class__.__name__})")
    print(decision.summary())
    _write_outputs(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
