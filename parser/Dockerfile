ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_CACHE_DIR=/tmp/uv-cache UV_LINK_MODE=copy OMP_NUM_THREADS=4 HF_HUB_OFFLINE=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
WORKDIR /parser
COPY parser/pyproject.toml parser/uv.lock ./
RUN --mount=type=cache,target=/tmp/uv-cache uv sync --frozen --no-dev
COPY parser/convert.py parser/validate.py parser/footnotes.py parser/pdf_quality.py parser/model_integrity.py parser/artifacts.py ./
RUN useradd --uid 10002 --create-home parser
USER 10002:10002
ENTRYPOINT ["/parser/.venv/bin/python", "/parser/convert.py"]
