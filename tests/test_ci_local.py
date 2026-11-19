"""Pruebas verificadas en local: cuándo la CI puede omitirlas (scripts/ci_local.py)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ci_local", ROOT / "scripts" / "ci_local.py")
assert _spec and _spec.loader
ci_local = importlib.util.module_from_spec(_spec)
sys.modules["ci_local"] = ci_local  # los dataclasses lo necesitan
_spec.loader.exec_module(ci_local)

TREE = "a" * 40
OTHER = "b" * 40


def status(desc=None, *, state="success", login="mantenedor", ctx="tests-locales", type_="User"):
    return {"context": ctx, "state": state, "creator": {"login": login, "type": type_},
            "description": desc if desc is not None else ci_local.build_description(
                TREE, "c" * 40, "Darwin-arm64", ["3.11", "3.12"], 226, login)}


def roles(table):
    return lambda login: table.get(login)


def test_description_roundtrip_and_limit():
    d = ci_local.build_description(TREE, "c" * 40, "Darwin-arm64", ["3.11", "3.12", "3.13"], 226, "Eliaxs1900")
    assert len(d) <= 140
    info = ci_local.parse_description(d)
    assert info and info["tree"] == TREE and info["os"] == "Darwin-arm64" and info["n"] == "226"
    long = ci_local.build_description(TREE, "c" * 40, "Linux-x86_64", ["3.11"] * 8, 9999, "x" * 39)
    assert len(long) <= 140 and ci_local.parse_description(long)["tree"] == TREE
    assert ci_local.parse_description("ok sin árbol") is None and ci_local.parse_description(None) is None
    assert ci_local.parse_description("tree=" + "z" * 40) is None


def test_skips_when_a_maintainer_verified_the_same_tree():
    for role in ("admin", "maintain"):
        d = ci_local.decide([status()], TREE, same_repo=True, role_of=roles({"mantenedor": role}))
        assert d.skip and d.who == "mantenedor" and d.os == "Darwin-arm64"
        assert "mantenedor" in d.summary() and "Darwin" in d.summary()


@pytest.mark.parametrize("case", ["fork", "sin_status", "fallo", "otro_arbol", "sin_arbol", "write", "sin_rol",
                                  "bot", "otro_contexto"])
def test_never_skips_otherwise(case):
    st, same, tree, table = [status()], True, TREE, {"mantenedor": "admin"}
    if case == "fork":
        same = False
    elif case == "sin_status":
        st = []
    elif case == "fallo":
        st = [status(state="failure")]
    elif case == "otro_arbol":
        tree = OTHER  # GitHub probaría otro árbol (main se movió o el PR cambió)
    elif case == "sin_arbol":
        st = [status("todo bien, confía en mí")]
    elif case == "write":
        table = {"mantenedor": "write"}  # autor del PR con permiso de escritura: no basta
    elif case == "sin_rol":
        table = {}
    elif case == "bot":
        st = [status(type_="Bot")]
    elif case == "otro_contexto":
        st = [status(ctx="otro")]
    d = ci_local.decide(st, tree, same_repo=same, role_of=roles(table))
    assert not d.skip and d.reason


def test_latest_status_wins():
    ok, bad = status(), status(state="failure")
    assert not ci_local.decide([bad, ok], TREE, same_repo=True, role_of=roles({"mantenedor": "admin"})).skip
    assert ci_local.decide([ok, bad], TREE, same_repo=True, role_of=roles({"mantenedor": "admin"})).skip


class FakeGh:
    """Respuestas de ``gh api`` por fragmento de la ruta."""

    def __init__(self, **routes):
        self.routes, self.calls = routes, []

    def __call__(self, args):
        self.calls.append(args)
        path = " ".join(args)
        for key, value in self.routes.items():
            if key in path:
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, str) else json.dumps(value)
        raise AssertionError(f"ruta inesperada: {path}")


def test_pull_request_flow_uses_status_and_role():
    gh = FakeGh(statuses=[status()], permission="maintain\n")
    d = ci_local.decide_for_pull_request("o/r", "h" * 40, TREE, same_repo=True, run=gh)
    assert d.skip
    gh = FakeGh(statuses=[status()], permission=subprocess.CalledProcessError(1, "gh"))  # sin acceso: CI completa
    assert not ci_local.decide_for_pull_request("o/r", "h" * 40, TREE, same_repo=True, run=gh).skip


def _pull(sha, merged=True, repo="o/r", head="h" * 40):
    return {"merged_at": "2026-01-01T00:00:00Z" if merged else None, "merge_commit_sha": sha,
            "head": {"sha": head, "repo": {"full_name": repo}}}


def test_push_to_main_looks_up_the_merged_pull_request():
    sha = "d" * 40
    ok = dict(statuses=[status()], permission="admin")
    assert ci_local.decide_for_push("o/r", sha, TREE, run=FakeGh(pulls=[_pull(sha)], **ok)).skip
    assert not ci_local.decide_for_push("o/r", sha, TREE, run=FakeGh(pulls=[_pull(sha, repo="otro/fork")], **ok)).skip
    assert not ci_local.decide_for_push("o/r", sha, TREE, run=FakeGh(pulls=[], **ok)).skip  # commit directo
    assert not ci_local.decide_for_push("o/r", sha, TREE, run=FakeGh(pulls=[_pull("e" * 40)], **ok)).skip
    assert not ci_local.decide_for_push("o/r", sha, OTHER, run=FakeGh(pulls=[_pull(sha)], **ok)).skip  # main se movió


def test_cli_writes_outputs_and_fails_safe(tmp_path, monkeypatch):
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    gh = FakeGh(statuses=[status()], permission="admin")
    monkeypatch.setattr(ci_local, "gh", gh)
    args = ["decide", "--event", "pull_request", "--repo", "o/r", "--sha", "h" * 40, "--tree", TREE,
            "--same-repo", "true"]
    assert ci_local.main(args) == 0
    text = out.read_text()
    assert "skip=true" in text and "verificado en local por mantenedor" in text
    assert "Pruebas omitidas" in summary.read_text()

    out.write_text("")
    boom = FakeGh(statuses=subprocess.CalledProcessError(1, "gh"))
    monkeypatch.setattr(ci_local, "gh", boom)
    assert ci_local.main(args) == 0
    assert "skip=false" in out.read_text() and "CI completa" in out.read_text()


def test_workflow_wiring():
    wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    assert "statuses: read" in wf and "pull-requests: read" in wf and "contents: read" in wf
    assert 'git show "$BASE_SHA:scripts/ci_local.py"' in wf  # las reglas son las de la base
    assert "needs: [select, puntuales, e2e]" in wf and "origen del verde" in wf
    # nada de interpolar texto de la API dentro de scripts de shell: va por variables de entorno
    for line in wf.splitlines():
        if "outputs.local" in line:
            assert "run:" not in line and "echo" not in line
