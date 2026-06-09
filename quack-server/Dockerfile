# Minimal Quack-serving DuckDB container.
#
# Uses the DuckDB CLI binary in interactive mode. init.sql installs Quack,
# seeds sample data, sets up authorization, and starts the server with
# CALL quack_serve(...). The CLI is kept alive by piping /dev/null into
# its stdin so the in-process Quack server threads outlive the init script.

# debian 12.x == bookworm; pinned to a point release so rebuilds don't
# silently pick up a new base.
FROM debian:12.14-slim

ARG TARGETARCH
ARG DUCKDB_VERSION=v1.5.2

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates gettext-base netcat-openbsd \
 && rm -rf /var/lib/apt/lists/* \
 && case "${TARGETARCH}" in \
      amd64) DUCKDB_PLATFORM=linux-amd64 ;; \
      arm64) DUCKDB_PLATFORM=linux-arm64 ;; \
      *)     DUCKDB_PLATFORM=linux-amd64 ;; \
    esac \
 && curl -fL "https://github.com/duckdb/duckdb/releases/download/${DUCKDB_VERSION}/duckdb_cli-${DUCKDB_PLATFORM}.zip" -o /tmp/d.zip \
 && unzip /tmp/d.zip -d /usr/local/bin \
 && rm /tmp/d.zip \
 && duckdb --version

# Install the Quack extension at BUILD time, not container start.
# This freezes the extension version into the image (core_nightly moves
# daily) and removes the runtime network dependency on
# extensions.duckdb.org — containers start offline and identically
# until the image is rebuilt. init.sql only needs `LOAD quack`.
RUN duckdb -c "INSTALL quack FROM core_nightly" \
 && duckdb -c "LOAD quack"

WORKDIR /quack
COPY init.sql.tmpl seed.sql /quack/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 9494

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
