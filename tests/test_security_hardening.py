"""Auditoría, paquete seguridad: redacción (#131 ReDoS, #100 huecos), zonas protegidas (#133) y
worktrees de Claude Code (#22). Cada prueba nace de una reproducción y está acotada en tiempo."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from watcher_mcp.config import PROTECTED_HOME_PATHS, Config
from watcher_mcp.security import REDACTED, Redactor, ensure_safe_path

# --------------------------------------------------------------------------- #131: ReDoS en la redacción
# Cada línea es el peor caso de un patrón distinto con 8 KiB. En main, la primera costaba ~1 s por línea.
ADVERSARIAL = {
    "identificadores con guion": "a-" * 4096,
    "identificadores con punto": "a." * 4096,
    "una palabra clave repetida": "token" * 1600,
    "clave larga sin valor": "x" * 4000 + "password" + "y" * 4000,
    "esquema sin ://": "a-" * 4096 + "b",
    "esquemas encadenados": "a://" * 2048,
    "authorization con espacios": "Authorization:" + " " * 8000,
    "authorization con tabulaciones": "authorization =" + "\t " * 4000,
    "dos puntos y comillas": "password:" + '"' * 8000,
    "muchas claves": "password " * 1000,
}


@pytest.mark.parametrize("name", list(ADVERSARIAL))
def test_redaction_cost_is_bounded_on_adversarial_lines(name):
    redact = Redactor(literals=["hunter2hunter2"])
    line = ADVERSARIAL[name]
    started = time.perf_counter()
    for _ in range(10):
        redact(line)
    elapsed = time.perf_counter() - started
    # En main: ~1 s por línea para los dos primeros casos (10 líneas = 10 s con el bucle parado).
    assert elapsed < 1.0, f"{name}: {elapsed:.2f}s para 10 líneas de {len(line)} caracteres"


def test_redaction_still_works_after_the_rewrite():
    redact = Redactor()
    assert redact("curl https://user:s3cr3tpass@example.org/x") == "curl https://***@example.org/x"
    assert redact("Authorization: Bearer abc.def-ghi") == "Authorization: ***"
    assert redact("proxy-authorization=Basic Zm9vOmJhcg==") == "proxy-authorization=***"
    assert redact("token=abc123def456") == "token=***"
    assert redact("MY_API_KEY: abcdef123456") == "MY_API_KEY: ***"
    assert redact("ghp_" + "a" * 36) == REDACTED
    assert redact("nada que ocultar, tokens aparte") == "nada que ocultar, tokens aparte"


# --------------------------------------------------------------------------- #100: huecos de redacción
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password="my secret pass"', "password=***"),  # valor entrecomillado con espacios
        ("api_key = 'k e y'", "api_key = ***"),
        ("export API_KEY='abc def ghi'", "export API_KEY=***"),
        ('{"password": "hunter2hunter2"}', '{"password": ***}'),  # JSON: comilla tras la clave
        ('{"api_key":"abc","x":1}', '{"api_key":***,"x":1}'),
        ("--password=hunter2hunter2", "--password=***"),
        ("secret: valor_largo_123", "secret: ***"),
    ],
)
def test_quoted_json_and_option_secrets_are_redacted(text, expected):
    assert Redactor()(text) == expected


def test_argv_redacts_the_value_after_secret_options():
    r = Redactor()
    assert r.argv(["deploy", "--password", "hunter2hunter2", "--env", "prod"]) == [
        "deploy", "--password", REDACTED, "--env", "prod",
    ]
    assert r.argv(["tool", "--api-key", "abc", "--token", "xyz"]) == ["tool", "--api-key", REDACTED, "--token", REDACTED]
    assert r.argv(["tool", "--password=abc"]) == ["tool", "--password=***"]
    # Una opción de secreto sin valor (la siguiente es otra opción) no se come el argumento ajeno.
    assert r.argv(["tool", "--password", "--verbose"]) == ["tool", "--password", "--verbose"]
    # Las que solo nombran un fichero o una ruta no son secretos.
    assert r.argv(["tool", "--token-file", "/etc/tok", "--secret-name", "n"]) == [
        "tool", "--token-file", "/etc/tok", "--secret-name", "n",
    ]


def test_argv_redacts_short_flags_only_for_known_programs():
    r = Redactor()
    assert r.argv(["sshpass", "-p", "hunter2hunter2", "ssh", "host"]) == ["sshpass", "-p", REDACTED, "ssh", "host"]
    assert r.argv(["/usr/bin/curl", "-u", "user:pass", "https://x.org"]) == ["/usr/bin/curl", "-u", REDACTED, "https://x.org"]
    assert r.argv(["mysql", "-u", "root", "-phunter2hunter2", "db"]) == ["mysql", "-u", "root", "-p***", "db"]
    # `-p` significa otras cosas en otros programas (puerto, patrón...): no se toca.
    assert r.argv(["python", "-m", "http.server", "-p", "8080"]) == ["python", "-m", "http.server", "-p", "8080"]
    assert r.argv(["pytest", "-p", "no:cacheprovider"]) == ["pytest", "-p", "no:cacheprovider"]


# --------------------------------------------------------------------------- #133: protected_home_dirs
@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "Users" / "yo"
    (h / "Library" / "LaunchAgents").mkdir(parents=True)
    (h / "proyecto").mkdir()
    return h


def safe(path: Path, roots: list[Path], home: Path) -> Path:
    return ensure_safe_path(path, roots, what="ruta", home=home, protected_home_dirs=PROTECTED_HOME_PATHS)


@pytest.mark.parametrize("root_of", ["home", "parent", "grandparent"])
def test_protected_home_dirs_apply_whatever_the_root_that_contains_home(home, tmp_path, root_of):
    root = {"home": home, "parent": home.parent, "grandparent": tmp_path}[root_of]
    with pytest.raises(ValueError, match="zona protegida"):
        safe(home / "Library" / "LaunchAgents" / "x.plist", [root], home)  # en main: aceptada con raíz /Users
    with pytest.raises(ValueError, match="zona protegida"):
        safe(home / "library" / "launchagents" / "x.plist", [root], home)  # y sin distinguir mayúsculas
    assert safe(home / "proyecto" / "src", [root], home) == (home / "proyecto" / "src").resolve()


def test_an_explicit_root_inside_home_still_allows_a_protected_zone_on_purpose(home):
    agents = home / "Library" / "LaunchAgents"
    assert safe(agents / "x.plist", [home, agents], home) == (agents / "x.plist").resolve()


def test_config_safe_path_with_a_root_above_home(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    config = Config(state_dir=tmp_path / "state", allowed_roots=[home.parent])
    with pytest.raises(ValueError, match="zona protegida"):
        config.safe_path(home / "Library" / "LaunchAgents" / "x.plist", what="dest")
    assert config.safe_path(home / "proyecto", what="dest") == (home / "proyecto").resolve()


# --------------------------------------------------------------------------- #22: worktrees de Claude Code
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "home" / "layout-mcp"
    (r / ".claude" / "worktrees" / "w1" / "tools").mkdir(parents=True)
    (r / ".claude" / "hooks").mkdir()
    return r


def check(path: Path, repo: Path) -> Path:
    return ensure_safe_path(path, [repo], what="cwd", home=repo.parent, protected_home_dirs=PROTECTED_HOME_PATHS)


def test_a_claude_worktree_is_accepted(repo):
    wt = repo / ".claude" / "worktrees" / "w1"
    assert check(wt, repo) == wt.resolve()
    assert check(wt / "tools", repo) == (wt / "tools").resolve()
    assert check(wt / "src" / "main.py", repo) == (wt / "src" / "main.py").resolve()


@pytest.mark.parametrize(
    "rel",
    [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks",
        ".claude",
        ".claude/worktrees",  # la carpeta de worktrees sin nombre no es un worktree
        ".claude/worktrees/.oculto/x",  # el nombre del worktree no puede ser oculto
        ".claude/agents/x.md",
        ".git/hooks/pre-commit",
    ],
)
def test_the_rest_of_dot_claude_stays_protected(repo, rel):
    with pytest.raises(ValueError, match="zona protegida"):
        check(repo / rel, repo)


@pytest.mark.parametrize(
    "rel",
    [
        ".claude/worktrees/w1/.claude/settings.json",
        ".claude/worktrees/w1/.claude/hooks",
        ".claude/worktrees/w1/.git/hooks/post-checkout",
        ".claude/worktrees/w1/.env",
        ".claude/worktrees/w1/.ssh/authorized_keys",
        ".CLAUDE/worktrees/w1/.Claude/settings.json",  # sin distinguir mayúsculas (APFS)
    ],
)
def test_inside_the_worktree_protected_zones_still_apply(repo, rel):
    with pytest.raises(ValueError, match="zona protegida"):
        check(repo / rel, repo)


def test_a_worktree_nested_in_a_worktree_is_still_a_worktree(repo):
    nested = repo / ".claude" / "worktrees" / "w1" / ".claude" / "worktrees" / "w2" / "src"
    assert check(nested, repo) == nested.resolve()
