"""Genera ``slowpkg``: un sdist local cuya compilación tarda N segundos sin escribir nada.

Reproduce, sin red ni dependencias, lo que pasa al instalar un paquete que se compila
desde el código fuente: pip se queda callado durante la compilación. El backend de
compilación va dentro del propio sdist (PEP 517 ``backend-path``), así que no hace
falta descargar setuptools.

    python examples/slowpkg.py DIRECTORIO [--seconds 20]
"""

from __future__ import annotations

import argparse
import io
import tarfile
from pathlib import Path

BACKEND = '''
import base64, hashlib, os, time, zipfile

NAME, VERSION = "slowpkg", "1.0"
DIST = f"{NAME}-{VERSION}.dist-info"
METADATA = f"Metadata-Version: 2.1\\nName: {NAME}\\nVersion: {VERSION}\\nSummary: paquete de demo que tarda en compilar\\n"
WHEEL = "Wheel-Version: 1.0\\nGenerator: slow_backend\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"


def get_requires_for_build_wheel(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    d = os.path.join(metadata_directory, DIST)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "METADATA"), "w").write(METADATA)
    open(os.path.join(d, "WHEEL"), "w").write(WHEEL)
    return DIST


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    time.sleep(BUILD_SECONDS)  # "compilando"... sin escribir nada, como un build real largo
    files = {
        f"{NAME}/__init__.py": f"__version__ = '{VERSION}'\\n",
        f"{DIST}/METADATA": METADATA,
        f"{DIST}/WHEEL": WHEEL,
    }
    record = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(content.encode())}")
    record.append(f"{DIST}/RECORD,,")
    files[f"{DIST}/RECORD"] = "\\n".join(record) + "\\n"
    name = f"{NAME}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return name


def build_sdist(sdist_directory, config_settings=None):
    raise NotImplementedError
'''

PYPROJECT = """[build-system]
requires = []
build-backend = "slow_backend"
backend-path = ["."]

[project]
name = "slowpkg"
version = "1.0"
"""


def make_slow_sdist(dest: Path, seconds: float = 20) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    files = {
        "slowpkg-1.0/pyproject.toml": PYPROJECT,
        "slowpkg-1.0/slow_backend.py": f"BUILD_SECONDS = {seconds}\n" + BACKEND,
        "slowpkg-1.0/PKG-INFO": "Metadata-Version: 2.1\nName: slowpkg\nVersion: 1.0\n",
        "slowpkg-1.0/slowpkg/__init__.py": "__version__ = '1.0'\n",
    }
    out = dest / "slowpkg-1.0.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dest", type=Path)
    ap.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()
    print(make_slow_sdist(a.dest, a.seconds))
