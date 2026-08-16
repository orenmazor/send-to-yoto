# Builder must match the distroless runtime's Python (3.11) so the vendored
# packages land on a compatible version. All our deps are pure Python.
FROM python:3.11-slim AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir --target /deps -r requirements.txt

# Deno is yt-dlp's highest-priority JS runtime and ships as a single binary,
# which is why it's here instead of node — nothing to vendor but the executable.
FROM debian:12-slim AS deno
ARG TARGETARCH
# yt-dlp requires deno >= 2.3.0 (utils/_jsruntime.py MIN_SUPPORTED_VERSION);
# older versions are detected but reported as "unsupported" and skipped.
ARG DENO_VERSION=v2.9.5
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && case "$TARGETARCH" in \
      amd64) target=x86_64-unknown-linux-gnu ;; \
      arm64) target=aarch64-unknown-linux-gnu ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-${target}.zip" -o /tmp/deno.zip \
 && unzip -q /tmp/deno.zip -d /usr/local/bin \
 && chmod 755 /usr/local/bin/deno

# Staging for directories, since distroless has no shell to mkdir with.
FROM debian:12-slim AS layout
RUN mkdir -p /config

FROM gcr.io/distroless/python3-debian12:nonroot

COPY --from=deps /deps /deps
COPY --from=deno /usr/local/bin/deno /usr/local/bin/deno
COPY --from=layout --chown=nonroot:nonroot /config /config
COPY --chown=nonroot:nonroot app.py gunicorn.conf.py /app/
COPY --chown=nonroot:nonroot templates/ /app/templates/

WORKDIR /app
ENV PYTHONPATH=/deps \
    PATH=/usr/local/bin:/deps/bin:$PATH \
    PORT=8080 \
    YOTO_TOKEN_FILE=/config/tokens.json

EXPOSE 8080

# Entrypoint of the distroless python image is already /usr/bin/python3.
CMD ["-m", "gunicorn", "-c", "/app/gunicorn.conf.py", "app:app"]
