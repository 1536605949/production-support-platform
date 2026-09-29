#!/usr/bin/env bash
# Reset the demo order data so the acceptance probe (and a live demo) can be
# replayed from a clean slate.
#
# Why this exists: the refund flow is *stateful on purpose*. Executing a refund
# flips ``orders.refund_available`` to false, and the order system then refuses a
# second one — that is the property the probe verifies. The cost is that the
# demo can only be run once per order unless the state is reset, and resetting
# it by hand means remembering which tables the business service owns.
#
# Scope: only the business service's demo tables. Conversations, approvals and
# audit history in the platform are left alone, because they are the evidence a
# reviewer would want to look at after a demo.
#
# Usage (from any directory):
#   bash scripts/reset_demo_data.sh
#
# Set COMPOSE="docker compose -f /path/to/docker-compose.yml" to override.

set -euo pipefail

# Locate the deployment directory from *this script's* location rather than the
# caller's cwd. Previously the script assumed it was run from
# ``deployment/`` and, when it was not, ``docker compose`` aborted with the
# unhelpful ``no configuration file provided: not found`` — a silent no-op that
# looked like a successful reset. Passing ``-f`` also makes Compose use the
# compose file's own directory as the project dir, so relative build contexts
# and ``.env`` resolve exactly as they do for a manual ``docker compose`` run.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/../deployment/docker-compose.yml}"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "错误: 找不到 compose 文件: $COMPOSE_FILE" >&2
    echo "请用 COMPOSE_FILE=<path> 指定，或在 deployment/ 目录下手动执行。" >&2
    exit 1
fi

COMPOSE="${COMPOSE:-docker compose -f $COMPOSE_FILE}"
SERVICE="${SERVICE:-postgres}"
DB="${POSTGRES_DB:-support}"
DB_USER="${POSTGRES_USER:-support}"

echo "==> 重置演示订单数据 (db=${DB})"
${COMPOSE} exec -T "${SERVICE}" psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${DB}" <<'SQL'
-- Business-service demo tables only. Order matters: children first.
TRUNCATE refund_proposals, tickets, audit_events RESTART IDENTITY;

-- Put the demo orders back to their seeded state.
UPDATE orders SET refund_available = true, status = 'shipped' WHERE order_id = 'ORD-1001';
UPDATE orders SET refund_available = true, status = 'processing' WHERE order_id = 'ORD-2001';

-- Verify rather than assume. If a seed changed the order ids or an UPDATE
-- matched nothing, the probe would skip the approval section and report a
-- green run that never exercised the approval path. Fail loudly here instead.
DO $$
DECLARE missing int;
BEGIN
    SELECT count(*) INTO missing
    FROM (VALUES ('ORD-1001'), ('ORD-2001')) AS wanted(order_id)
    WHERE NOT EXISTS (
        SELECT 1 FROM orders o
        WHERE o.order_id = wanted.order_id AND o.refund_available IS TRUE
    );
    IF missing > 0 THEN
        RAISE EXCEPTION '重置未生效：% 个演示订单仍不可退款', missing;
    END IF;
END $$;

SELECT order_id, user_id, status, refund_available FROM orders ORDER BY order_id;
SQL

echo "==> 重置完成（已校验 ORD-1001 / ORD-2001 均可退款）。可重新运行验收："
echo "    python3 scripts/acceptance_probe.py https://<host>/support"
