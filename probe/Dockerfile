# Dockerfile — DNS probe with optional Log Analytics ingestion.
#
# Build (from the repo root):
#   podman build --network=host -t docker.io/erjosito/dns-probe:latest \
#                -f probe/Dockerfile probe/
#   podman push docker.io/erjosito/dns-probe:latest
#
# The --network=host is needed for podman on Windows / WSL2: rootless build
# containers can't reach external DNS resolvers, so pip can't resolve pypi.org
# without it. Plain `docker build` on Linux does not need it.
#
# Run locally (laptop vantage, ships to LAW via your az-login creds):
#   podman run --rm -it `
#     -e VANTAGE=jomore-desktop `
#     -e LAW_DCE_ENDPOINT="https://<dce>.<region>-1.ingest.monitor.azure.com" `
#     -e LAW_DCR_IMMUTABLE_ID="dcr-..." `
#     -v "${HOME}/.azure:/home/probe/.azure:ro" `
#     docker.io/erjosito/dns-probe:latest `
#       --names db.example.com replica.db.example.com `
#       --interval 60 --duration 30m `
#       --log-file -

FROM python:3.12-slim

# Non-root user, dedicated home (DefaultAzureCredential / azure-cli token cache live under HOME)
RUN useradd --create-home --uid 10001 --shell /bin/bash probe

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/probe

# Pin the optional deps for reproducible builds
RUN pip install \
        "dnspython>=2.6,<3" \
        "azure-identity>=1.17,<2" \
        "azure-monitor-ingestion>=1.0,<2"

COPY --chown=probe:probe dns_probe.py /opt/probe/dns_probe.py
RUN chmod 0555 /opt/probe/dns_probe.py

USER probe
WORKDIR /home/probe

ENTRYPOINT ["python", "/opt/probe/dns_probe.py"]
CMD ["--help"]
