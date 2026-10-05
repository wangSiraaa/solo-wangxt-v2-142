#!/usr/bin/env bash
# 启动 PostgreSQL（如未运行）+ FastAPI 服务
set -euo pipefail

PGENV=/workspace/pgenv
PGDATA=/workspace/pgdata
PGPORT=54329

if ! "$PGENV/bin/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  "$PGENV/bin/pg_ctl" -D "$PGDATA" \
    -o "-p $PGPORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=" \
    -l /workspace/pgdata.log start
fi

export PETRI_DATABASE_URL="${PETRI_DATABASE_URL:-postgresql+psycopg://postgres@127.0.0.1:$PGPORT/petri}"
cd "$(dirname "$0")"
exec /workspace/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
