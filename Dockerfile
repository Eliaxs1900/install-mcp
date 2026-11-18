# Imagen para ejecutar los tests y grabar las demos sin tocar el sistema local.
#
#   docker build --target test -t watcher-mcp:test .
#   docker run --rm watcher-mcp:test                      # pytest (sin red)
#   (otra versión de Python: --build-arg PYTHON=3.11)
#
#   docker build --target integration -t watcher-mcp:integration .
#   docker run --rm watcher-mcp:integration               # instalaciones reales (apt, npm, go...)
#
#   docker build --target demo -t watcher-mcp:demo .
#   docker run --rm -v "$PWD/docs/demos:/out" watcher-mcp:demo   # graba y renderiza docs/demos/*

ARG PYTHON=3.12
FROM python:${PYTHON}-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv
# procps: los tests usan `ps` para comprobar que los procesos cancelados han muerto.
# git: backend git (los tests clonan repos locales).
RUN apt-get update \
 && apt-get install -y --no-install-recommends procps git \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv venv /opt/venv && uv pip install --python /opt/venv/bin/python -e ".[dev]"
ENV PATH="/opt/venv/bin:$PATH"
COPY tests ./tests
COPY examples ./examples
# tests/test_select_tests.py importa scripts/select_tests.py.
COPY scripts ./scripts

FROM base AS test
CMD ["python", "-m", "pytest", "-q"]

# Instalaciones reales contra internet (apt, npm, go, pip, git, download).
#   docker build --target integration -t watcher-mcp:integration .
#   docker run --rm watcher-mcp:integration
FROM base AS integration
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm golang-go ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV WATCHER_MCP_NETWORK_TESTS=1
CMD ["python", "-m", "pytest", "-q", "-m", "network", "-rA"]

FROM base AS demo
# Fuente monoespaciada para renderizar las capturas; Pillow genera GIF/PNG sin ffmpeg.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && uv pip install --python /opt/venv/bin/python -e ".[demo]"
CMD ["sh", "scripts/make_demos.sh", "/out"]
