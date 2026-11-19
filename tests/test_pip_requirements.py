"""Auditoría #105: el contenido de un requirements.txt (``-r ~/.netrc``, ``--index-url http://…``) se valida."""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher_mcp.backends.pip import (
    PipBackend,
    normalize_requirements,
    prune_private_copies,
    validate_requirements_file,
    write_private_copy,
)
from watcher_mcp.config import Config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "proyecto").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    (h / ".netrc").write_text("machine api.example.com login admin password hunter2hunter2\n")
    return h


@pytest.fixture
def config(home: Path, tmp_path: Path) -> Config:
    return Config(state_dir=tmp_path / "state", allowed_roots=[home], allowed_backends=["pip"])


def write(home: Path, body: str, name: str = "requirements.txt") -> Path:
    path = home / "proyecto" / name
    path.write_text(body, encoding="utf-8")
    return path


ACCEPTED = [
    "pypdf\nrequests>=2.32,<3  # comentario\n\n# solo comentario\n",
    "rich[jupyter]==13.9.4 ; python_version >= '3.9'\n",
    "pkg==1.0 --hash=sha256:" + "ab" * 32 + "\n",
    "pkg \\\n    >=1.0\n",
    "--index-url https://pypi.example.org/simple\n--extra-index-url=https://otro.example.org/simple\nrequests\n",
    "-i https://pypi.example.org/simple\n",
    "--no-index\n--pre\n--prefer-binary\n--require-hashes\n",
    "--only-binary :all:\n--no-binary pkg,otro\n",
    "-f https://wheels.example.org/\n",
]

REJECTED = {
    "-r ~/.netrc\n": "fuera de las raíces|zona protegida",  # el caso de la auditoría
    "-r/etc/passwd\n": "fuera de las raíces",
    "--requirement=/etc/hosts\n": "fuera de las raíces",
    "-c ~/.netrc\n": "fuera de las raíces|zona protegida",
    "--constraint ../../.netrc\n": "fuera de las raíces|zona protegida",
    "-r no-existe.txt\n": "no existe",
    "--index-url http://pypi.example.org/simple\n": "https",
    "--extra-index-url http://x.org/simple\n": "https",
    "-i ftp://x.org/\n": "https",
    "-f http://x.org/wheels\n": "https",
    "-f /etc\n": "fuera de las raíces",
    "-e .\n": "opción no admitida",
    "--editable git+https://github.com/a/b\n": "opción no admitida",
    "--trusted-host evil.example.org\n": "opción no admitida",
    "--use-feature=fast-deps\n": "opción no admitida",
    "--index-url\n": "necesita un valor",
    "pkg @ https://example.org/pkg.whl\n": "requisito no válido",
    "git+https://github.com/a/b.git\n": "requisito no válido",
    "https://example.org/pkg.whl\n": "requisito no válido",
    "./local/pkg\n": "requisito no válido",
    "-r ${HOME}/.netrc\n": "variables de entorno",
    "pkg==${VERSION}\n": "variables de entorno",
    "--no-binary https://x\n": "valor no válido",
    "-r 'sin cerrar\n": "comillas",
}


@pytest.mark.parametrize("body", ACCEPTED)
def test_valid_requirement_files_are_accepted(config, home, body):
    validate_requirements_file(write(home, body), config)


@pytest.mark.parametrize(("body", "why"), list(REJECTED.items()))
def test_unverifiable_lines_are_rejected(config, home, body, why):
    with pytest.raises(ValueError, match=why):
        validate_requirements_file(write(home, body), config)


def test_nested_includes_are_validated_recursively(config, home):
    write(home, "pypdf\n", "base.txt")
    write(home, "-r base.txt\n-c constraints.txt\n", "requirements.txt")
    write(home, "rich==13.0\n", "constraints.txt")
    validate_requirements_file(home / "proyecto" / "requirements.txt", config)
    # El incluido es el que trae la línea peligrosa.
    write(home, "-r ../.netrc\n", "base.txt")
    with pytest.raises(ValueError, match="zona protegida|fuera de las raíces"):
        validate_requirements_file(home / "proyecto" / "requirements.txt", config)
    write(home, "--index-url http://x.org/\n", "base.txt")
    with pytest.raises(ValueError, match="https"):
        validate_requirements_file(home / "proyecto" / "requirements.txt", config)


def test_include_cycles_and_excess_depth_are_bounded(config, home):
    write(home, "-r b.txt\n", "a.txt")
    write(home, "-r a.txt\n", "b.txt")
    validate_requirements_file(home / "proyecto" / "a.txt", config)  # un ciclo no se recorre dos veces
    for i in range(12):
        write(home, f"-r n{i + 1}.txt\n", f"n{i}.txt")
    write(home, "pypdf\n", "n12.txt")
    with pytest.raises(ValueError, match="niveles"):
        validate_requirements_file(home / "proyecto" / "n0.txt", config)


def test_oversized_and_binary_files_are_rejected(config, home):
    big = home / "proyecto" / "big.txt"
    big.write_text("pypdf\n" * 300_000)
    with pytest.raises(ValueError, match="no es un requirements.txt razonable"):
        validate_requirements_file(big, config)
    binary = home / "proyecto" / "bin.txt"
    binary.write_bytes(b"\xff\xfe\x00binario")
    with pytest.raises(ValueError, match="no se puede leer"):
        validate_requirements_file(binary, config)


def test_the_backend_validates_the_file_before_running_pip(config, home):
    """La reproducción de la auditoría: ``requirements`` limpio por fuera, con ``-r ~/.netrc`` dentro."""
    backend = PipBackend(config)
    path = write(home, "pypdf\n-r ~/.netrc\n")
    with pytest.raises(ValueError, match="requirements.txt"):
        backend.validate({"requirements": str(path), "venv": "t", "no_index": True})
    path.write_text("pypdf\n")
    assert backend.validate({"requirements": str(path), "venv": "t", "no_index": True}).requirements == str(path)


# --------------------------------------------------------------------------- rodeos del validador (trama 245)
BYPASSES = {
    "comentario que acaba en barra": "# nota \\\n-r {home}/.netrc\n",
    "comentario con sangría": "  # nota \\\n-r {home}/.netrc\n",
    "comentario y CR suelto": "# nota \\\r-r {home}/.netrc\n",
    "comentario y salto de página": "# nota \\\x0c-r {home}/.netrc\n",
    "índice http tras comentario": "  # x \\\n--index-url http://evil.example/simple\n",
    "con BOM": "\ufeff-r {home}/.netrc\n",
    "requisito y barra": "pytest \\\n-r {home}/.netrc\n",
}


@pytest.mark.parametrize("body", list(BYPASSES.values()), ids=list(BYPASSES))
def test_line_continuation_tricks_cannot_hide_a_dangerous_line(config, home, body):
    with pytest.raises(ValueError):
        normalize_requirements(write(home, body.format(home=home)), config)


def test_comment_lines_do_not_continue_but_requirement_lines_do(config, home):
    # Como pip: «pkg \» se une a la línea siguiente, «# nota \» no.
    path = write(home, "pkg \\\n  >=1.0\n# nota \\\nrich\n")
    assert normalize_requirements(path, config).lines == ["pkg   >=1.0", "rich"]


def test_normalized_copy_has_only_validated_lines_and_flattens_includes(config, home):
    write(home, "pypdf  # comentario\n--index-url https://pypi.example.org/simple\n", "base.txt")
    write(home, "rich==13.0\n", "constraints.txt")
    main = write(home, "# cabecera\n-r base.txt\n-c constraints.txt\nrequests>=2 --hash=sha256:" + "ab" * 32 + "\n--pre\n")
    result = normalize_requirements(main, config)
    assert result.lines == [
        "pypdf", "--index-url https://pypi.example.org/simple",
        "requests>=2 --hash=sha256:" + "ab" * 32, "--pre",
    ]
    assert result.constraints == [["rich==13.0"]]
    copy = write_private_copy(result, home / "privado")
    text = copy.read_text()
    # Por líneas: la ruta temporal puede contener «-r» (/tmp/pytest-of-runner/… en los runners de GitHub).
    lines = text.splitlines()
    assert "#" not in text and not any(ln.startswith("-r") for ln in lines) and any(ln.startswith("-c ") for ln in lines)
    assert (copy.parent / "constraints-0.txt").read_text() == "rich==13.0\n"
    assert oct(copy.stat().st_mode & 0o777) == "0o600" and oct(copy.parent.stat().st_mode & 0o777) == "0o700"


def test_bad_hashes_are_rejected(config, home):
    with pytest.raises(ValueError, match="requisito no válido"):
        normalize_requirements(write(home, "pkg==1 --hash=md5:zzzz\n"), config)


def test_the_requirements_step_reads_the_file_when_it_runs_not_when_it_was_submitted(config, home):
    """TOCTOU: válido al enviar, cambiado mientras el job esperaba en la cola."""
    import asyncio

    backend = PipBackend(config)
    path = write(home, "pypdf\n")
    spec = backend.validate({"requirements": str(path), "venv": "t", "no_index": True, "installer": "pip"})
    plan = backend.plan(spec)
    assert [s.name for s in plan.steps] == ["create-venv", "requirements", "install"]
    install = plan.steps[-1].argv
    private = Path(install[install.index("-r") + 1])
    assert private != path and private.name == "requirements.txt"  # pip nunca recibe la ruta original
    path.write_text(f"pypdf\n-r {home}/.netrc\n")  # el fichero cambia después de validar
    logged: list[str] = []
    from watcher_mcp.backends.base import StepContext

    with pytest.raises(ValueError, match="fuera de las raíces|zona protegida|requirements"):
        asyncio.run(plan.steps[1].func(StepContext(log=logged.append, cancelled=lambda: False)))
    assert not private.exists()  # y no se llega a escribir nada que pip pueda leer


def test_the_private_copy_is_not_affected_by_later_changes_to_the_original(config, home):
    import asyncio

    from watcher_mcp.backends.base import StepContext

    backend = PipBackend(config)
    path = write(home, "pypdf\n")
    plan = backend.plan(backend.validate({"requirements": str(path), "venv": "t", "no_index": True, "installer": "pip"}))
    asyncio.run(plan.steps[1].func(StepContext(log=lambda m: None, cancelled=lambda: False)))
    private = Path(plan.steps[-1].argv[plan.steps[-1].argv.index("-r") + 1])
    path.write_text(f"-r {home}/.netrc\n")  # tras el paso: pip lee la copia, no esto
    assert private.read_text() == "pypdf\n"


def test_symlink_swapped_in_after_submission_is_still_checked(config, home):
    import asyncio

    from watcher_mcp.backends.base import StepContext

    backend = PipBackend(config)
    path = write(home, "pypdf\n")
    plan = backend.plan(backend.validate({"requirements": str(path), "venv": "t", "no_index": True, "installer": "pip"}))
    path.unlink()
    path.symlink_to(home / ".netrc")  # el fichero pasa a ser un enlace a un fichero protegido
    with pytest.raises(ValueError, match="zona protegida|fuera de las raíces"):
        asyncio.run(plan.steps[1].func(StepContext(log=lambda m: None, cancelled=lambda: False)))


def test_old_private_copies_are_pruned(tmp_path):
    import os
    import time

    root = tmp_path / "requirements"
    (root / "viejo").mkdir(parents=True)
    (root / "nuevo").mkdir()
    old = time.time() - 3 * 86400
    os.utime(root / "viejo", (old, old))
    prune_private_copies(root)
    assert not (root / "viejo").exists() and (root / "nuevo").exists()
