# Imagen para ejecutar los tests y grabar las demos sin tocar el sistema local.
#
#   docker build --target test -t install-mcp:test .
#   docker run --rm install-mcp:test                      # pytest
#
#   docker build --target demo -t install-mcp:demo .
#   docker run --rm -v "$PWD/docs/demos:/out" install-mcp:demo   # graba y renderiza docs/demos/*

FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv
# procps: los tests usan `ps` para comprobar que los procesos cancelados han muerto.
RUN apt-get update \
 && apt-get install -y --no-install-recommends procps \
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

FROM base AS test
CMD ["python", "-m", "pytest", "-q"]

FROM base AS demo
# Fuente monoespaciada para renderizar las capturas; Pillow genera GIF/PNG sin ffmpeg.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && uv pip install --python /opt/venv/bin/python -e ".[demo]"
COPY scripts ./scripts
CMD ["sh", "scripts/make_demos.sh", "/out"]
