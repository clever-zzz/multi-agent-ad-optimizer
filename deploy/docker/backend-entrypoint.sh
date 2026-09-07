#!/bin/sh
# Container entrypoint: apply migrations, then hand over to the real command.
#
# Runs as PID 1's child under tini so signals reach uvicorn cleanly.
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "[entrypoint] applying database migrations"
    # Retries cover the common compose/k8s race where postgres is not yet
    # accepting connections when the API pod starts.
    attempts="${MIGRATION_ATTEMPTS:-30}"
    delay="${MIGRATION_RETRY_SECONDS:-2}"
    i=1
    until alembic upgrade head; do
        if [ "$i" -ge "$attempts" ]; then
            echo "[entrypoint] migrations failed after $attempts attempts" >&2
            exit 1
        fi
        echo "[entrypoint] database not ready, retry $i/$attempts in ${delay}s"
        i=$((i + 1))
        sleep "$delay"
    done
else
    echo "[entrypoint] RUN_MIGRATIONS=false, skipping migrations"
fi

echo "[entrypoint] starting $*"
exec "$@"