# syntax=docker/dockerfile:1

# ---- build: produce a wheel so the runtime image carries no build tooling ----
FROM python:3.13-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build==1.2.2
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# ---- runtime ----
FROM python:3.13-slim
LABEL org.opencontainers.image.title="mllp-sim" \
      org.opencontainers.image.description="Scriptable HL7 v2 MLLP test server and client" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/xlsarath/mllp-sim"

# Inside a container, listen on all interfaces and always expose the status page.
# Command-line flags still win; scenario files only set what these leave open.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MLLP_SIM_HOST=0.0.0.0 \
    MLLP_SIM_HTTP_PORT=8080

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin mllp

COPY examples /examples

USER 10001
WORKDIR /home/mllp
EXPOSE 2575 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2); sys.exit(0)"]

ENTRYPOINT ["mllp-sim"]
CMD ["serve"]
