#!/usr/bin/env bash
# Apply schema migrations to the shared database.
#
# Why this is not simply ``alembic upgrade head``: the database in production was
# created by ``Base.metadata.create_all()`` and has no ``alembic_version`` table.
# Running ``upgrade head`` against it would try to CREATE TABLE everything that
# already exists and abort on the first conflict — leaving a schema that is
# neither the old one nor the new one. So the first run has to *adopt* the
# existing schema by stamping the baseline revision, which records "these
# migrations are already reflected in the database" without executing any DDL.
#
# Usage:
#   bash scripts/migrate.sh              # adopt if needed, then upgrade to head
#   bash scripts/migrate.sh check        # fail if models drifted from migrations
#   bash scripts/migrate.sh current      # what revision the database is at
#   bash scripts/migrate.sh history      # list revisions
#   bash scripts/migrate.sh sql          # print the SQL instead of running it
#
# Env:
#   MIGRATE_SERVICE  which container carries the migration tool (default gateway)
#   BASELINE_REV     revision id of the baseline (default 0001_baseline)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deployment/docker-compose.yml}"
DB_SERVICE="${DB_SERVICE:-postgres}"
DB="${POSTGRES_DB:-support}"
DB_USER="${POSTGRES_USER:-support}"
SERVICE="${MIGRATE_SERVICE:-gateway}"
BASELINE_REV="${BASELINE_REV:-0001_baseline}"

die() { echo "迁移失败: $*" >&2; exit 1; }
[ -f "$COMPOSE_FILE" ] || die "找不到 compose 文件: $COMPOSE_FILE"

COMPOSE=(docker compose -f "$COMPOSE_FILE")

# ``-v "$ROOT:/app"`` is load-bearing, not a convenience.
#
# The gateway image COPYs the source in (gateway/Dockerfile) and the service
# declares no bind mount, so a plain ``docker compose run gateway alembic ...``
# reads the *image's* ``migrations/`` and ``models.py``. On a checkout that has
# been edited since the last build that means:
#
#   * ``upgrade head`` resolves "head" to whatever the image knew about, applies
#     nothing, and still prints 完成. Observed on this deployment: the run
#     reported success and left ``alembic_version`` at ``0001_baseline`` while a
#     ``0002_analytics_indexes`` migration sat on disk unapplied.
#   * ``check`` compares the image's models against the database, so the drift
#     guard passes exactly when a model change is pending — the one moment it
#     exists to catch.
#
# Mounting the working copy makes both commands describe the source that the next
# ``up --build`` will bake into the image, which is what a migration is supposed
# to do. ``--user`` keeps alembic's ``__pycache__`` out of root's ownership in
# the host tree.
alembic() {
    "${COMPOSE[@]}" run --rm --no-deps \
        --user "$(id -u):$(id -g)" \
        -v "$ROOT:/app" \
        "$SERVICE" alembic "$@"
}

# ``docker compose run`` starts dependencies, so postgres comes up if it is down.
# ``--no-deps`` is deliberately NOT used for the psql helper: we need the
# database running to query it at all.
q() { "${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$DB" -At -c "$1"; }

case "${1:-upgrade}" in
upgrade)
    echo "==> 检查数据库现状"
    HAS_VERSION="$(q "SELECT to_regclass('public.alembic_version') IS NOT NULL;")"
    HAS_SCHEMA="$(q "SELECT to_regclass('public.orders') IS NOT NULL;")"

    if [ "$HAS_SCHEMA" = "t" ] && [ "$HAS_VERSION" = "f" ]; then
        echo "    已有 schema 但没有 alembic_version —— 这是 create_all() 建出来的库。"
        echo "    先把基线标记为已应用（不执行任何 DDL），再升级。"
        alembic stamp "$BASELINE_REV"
    elif [ "$HAS_SCHEMA" = "f" ]; then
        echo "    空数据库：将从头执行全部迁移。"
    else
        echo "    已由 alembic 管理，当前版本：$(q "SELECT version_num FROM alembic_version;" || echo '(空)')"
    fi

    echo "==> alembic upgrade head"
    alembic upgrade head
    echo
    echo "完成。当前版本："
    alembic current
    ;;
check)
    # The guard that stops the original bug from coming back. ``alembic check``
    # compares the models against the migration history and exits non-zero if a
    # model change has no migration — i.e. if someone edited models.py and
    # forgot. Run it before deploying; without it the drift is invisible until
    # a query fails against a column that was never created.
    echo "==> alembic check（模型与迁移是否一致）"
    alembic check
    echo "一致：没有未迁移的模型改动。"
    ;;
current | history)
    alembic "$1"
    ;;
sql)
    alembic upgrade head --sql
    ;;
*)
    echo "用法: $0 [upgrade|check|current|history|sql]" >&2
    exit 2
    ;;
esac
