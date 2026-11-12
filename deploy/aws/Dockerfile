# Update both patch tags together; rebuild and run hosted_smoke.sh before deploying.
# The wheel is pure Python: build it natively, even when the image targets arm64 under QEMU.
FROM --platform=$BUILDPLATFORM python:3.12.12-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build==1.3.0 && python -m build --wheel

FROM python:3.12.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/aws/requirements-hosted.txt /tmp/requirements-hosted.txt
COPY --from=builder /build/dist/*.whl /tmp/wheels/
RUN for wheel in /tmp/wheels/*.whl; do pip install --no-cache-dir -c /tmp/requirements-hosted.txt "${wheel}[hosted]"; done \
    && rm -rf /tmp/wheels /tmp/requirements-hosted.txt
# Retrieved 2026-11-09. Review the new bundle and replace the digest when AWS rotates it.
RUN python -c "import urllib.request,hashlib; b=urllib.request.urlopen('https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem',timeout=60).read(); assert hashlib.sha256(b).hexdigest() == '90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b', 'RDS CA checksum mismatch'; open('/tmp/rds-ca.pem','wb').write(b)"
RUN groupadd --gid 10001 agenticml && useradd --uid 10001 --gid 10001 --no-create-home agenticml \
    && mkdir -p /data /etc/agenticml && chown 10001:10001 /data \
    && mv /tmp/rds-ca.pem /etc/agenticml/rds-ca.pem
COPY --chmod=644 deploy/aws/limits.json /etc/agenticml/limits.json
COPY --chmod=755 deploy/aws/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV AGENTICML_SERVER_LIMITS=/etc/agenticml/limits.json PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).close()"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
