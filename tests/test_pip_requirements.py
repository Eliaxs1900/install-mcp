"""Auditoría #105: el contenido de un requirements.txt (``-r ~/.netrc``, ``--index-url http://…``) se valida."""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher_mcp.backends.pip import PipBackend, validate_requirements_file
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
