"""Pruebas selectivas: el mapa omite lo que no se toca y nunca omite pruebas por un olvido."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("select_tests", ROOT / "scripts" / "select_tests.py")
assert _spec and _spec.loader
select_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(select_tests)
select = select_tests.select


def test_docs_only_runs_nothing():
    assert select(["README.md", "docs/design.md", "examples/demo.py", "Dockerfile"]) == "NONE"


def test_core_change_runs_everything():
    assert select(["README.md", "src/watcher_mcp/jobs.py"]) == "ALL"
    assert select(["tests/conftest.py"]) == "ALL"
    assert select([".github/workflows/tests.yml"]) == "ALL"
    assert select(["scripts/select_tests.py"]) == "ALL"


def test_backend_change_runs_only_its_tests():
    assert select(["src/watcher_mcp/backends/git.py"]) == (
        "tests/test_backends.py tests/test_revision.py tests/test_web_git_run.py"
    )
    assert select(["src/watcher_mcp/backends/jvm.py", "README.md"]) == "tests/test_backends.py tests/test_revision.py"


def test_changed_test_runs_itself_and_dependants():
    assert select(["tests/test_pip.py"]) == "tests/test_pip.py"
    assert select(["tests/test_core.py"]) == "tests/test_core.py tests/test_limits.py"


def test_unknown_file_is_safe_by_default():
    assert select(["src/watcher_mcp/backends/container.py"]) == "ALL"  # backend nuevo sin mapear
    assert select(["algo/nuevo.txt"]) == "ALL"


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="no es un checkout de git (p. ej. la imagen Docker)")
def test_every_tracked_file_is_classified():
    """Cada fichero del repo cae en una regla explícita (si no, ejecuta todo: válido, pero se avisa)."""
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    unclassified = [
        f for f in files
        if not (select_tests._matches(f, select_tests.CORE) or f in select_tests.MAP
                or select_tests._matches(f, select_tests.NO_TESTS) or f.startswith("tests/test_"))
    ]
    assert unclassified == [], f"añade estos ficheros a scripts/select_tests.py: {unclassified}"


def test_every_mapped_test_exists():
    targets = {t for tests in select_tests.MAP.values() for t in tests}
    missing = [t for t in targets if not (ROOT / t).exists()]
    assert missing == []


def test_tiers_split_unit_and_e2e():
    tiers = select_tests.select_tiers
    assert tiers(["src/watcher_mcp/backends/git.py"]) == (
        "tests/test_backends.py tests/test_revision.py", "tests/test_web_git_run.py"
    )
    assert tiers(["tests/test_pip.py"]) == ("NONE", "tests/test_pip.py")
    assert tiers(["src/watcher_mcp/backends/jvm.py"]) == ("tests/test_backends.py tests/test_revision.py", "NONE")
    assert tiers(["README.md"]) == ("NONE", "NONE")
    assert tiers(["src/watcher_mcp/jobs.py"]) == ("ALL", "ALL")


def test_e2e_files_match_the_marker():
    """La lista E2E del selector coincide con los ficheros marcados con pytest.mark.e2e."""
    module_mark = re.compile(r"(?m)^pytestmark = pytest\.mark\.e2e$|^pytestmark = \[[^\]]*pytest\.mark\.e2e,")
    marked = {f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py") if module_mark.search(p.read_text())}
    assert marked == set(select_tests.E2E_FILES)
