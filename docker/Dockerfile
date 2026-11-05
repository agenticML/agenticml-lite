# python:3.12-slim multi-arch index, resolved with buildx imagetools inspect.
FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e AS build
WORKDIR /build
COPY . .
RUN pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/agenticML/agenticml-lite" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.title="AgenticML Server" \
      org.opencontainers.image.description="Self-hosted AgenticML experiment tracking server" \
      org.opencontainers.image.version="${VERSION}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir /wheels/*.whl
RUN groupadd --gid 10001 aml \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin aml \
    && mkdir /data && chown 10001:10001 /data
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTICML_SERVER_DATA=/data
VOLUME /data
EXPOSE 8080
USER 10001:10001
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]
ENTRYPOINT ["agenticml", "server", "serve"]
CMD ["--data", "/data", "--host", "0.0.0.0", "--port", "8080"]
