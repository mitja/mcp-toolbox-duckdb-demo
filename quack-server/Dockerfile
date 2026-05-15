# Minimal Quack-serving DuckDB container.
#
# Uses the DuckDB CLI binary in interactive mode. init.sql installs Quack,
# seeds sample data, sets up authorization, and starts the server with
# CALL quack_serve(...). The CLI is kept alive by piping /dev/null into
# its stdin so the in-process Quack server threads outlive the init script.

FROM debian:bookworm-slim

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

WORKDIR /quack
COPY init.sql.tmpl seed.sql /quack/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 9494

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
