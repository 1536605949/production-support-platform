#!/usr/bin/env bash
# Restore a backup produced by scripts/backup.sh — or, better, *prove* one is
# restorable without touching production.
#
# Usage:
#   bash scripts/restore_backup.sh --verify <backup_dir>   # safe: scratch DB
#   bash scripts/restore_backup.sh --yes <backup_dir>      # destructive: live DB
#
# ``--verify`` is the mode worth running on a schedule. A backup that has never
# been restored is a hypothesis, not a backup: the failure modes that matter —
# a truncated archive, a missing table, a dump taken with an incompatible
# pg_dump — are all invisible until you actually load it. So this restores into
# a throwaway database, compares row counts against the manifest, and drops it.
#
# This replaces scripts/restore_business_db.sh, which required an undocumented
# POSTGRES_CONTAINER variable and piped the dump through ``docker exec -i``.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/../deployment/docker-compose.yml}"
DB_SERVICE="${DB_SERVICE:-postgres}"
DB="${POSTGRES_DB:-support}"
DB_USER="${POSTGRES_USER:-support}"

MODE=""
case "${1:-}" in
    --verify) MODE=verify; shift ;;
    --yes)    MODE=live;   shift ;;
    *) echo "用法: $0 --verify <backup_dir>   (安全：还原到临时库比对行数)" >&2
       echo "      $0 --yes <backup_dir>      (破坏性：覆盖当前数据库)" >&2
       exit 2 ;;
esac
SRC="${1:-}"
[ -n "$SRC" ] && [ -d "$SRC" ] || { echo "错误: 备份目录不存在: ${SRC:-<空>}" >&2; exit 2; }
DUMP="$SRC/postgres.dump"
[ -f "$DUMP" ] || { echo "错误: $DUMP 不存在" >&2; exit 2; }
[ -f "$COMPOSE_FILE" ] || { echo "错误: 找不到 compose 文件: $COMPOSE_FILE" >&2; exit 2; }

COMPOSE=(docker compose -f "$COMPOSE_FILE")
die() { echo "还原失败: $*" >&2; exit 1; }

# -- 0. integrity before anything else --------------------------------------- #
if [ -f "$SRC/MANIFEST.txt" ] && command -v sha256sum >/dev/null 2>&1; then
    echo "--> 校验 sha256（对照 MANIFEST）"
    ( cd "$SRC" && grep -E '^[0-9a-f]{64}  \./' MANIFEST.txt | sha256sum -c - ) \
        || die "校验和不匹配——备份文件已损坏或被改动"
else
    echo "--> 跳过 sha256（无 MANIFEST 或本机无 sha256sum）"
fi

CONTAINER_DUMP=/tmp/restore-$(basename "$SRC").dump
"${COMPOSE[@]}" cp "$DUMP" "$DB_SERVICE:$CONTAINER_DUMP" >/dev/null || die "无法把归档复制进容器"
trap '"${COMPOSE[@]}" exec -T "$DB_SERVICE" rm -f "$CONTAINER_DUMP" >/dev/null 2>&1 || true' EXIT

if [ "$MODE" = "live" ]; then
    # -- 1a. destructive restore --------------------------------------------- #
    echo "!! 即将用 $SRC 覆盖数据库 '$DB'（服务 $DB_SERVICE）"
    echo "!! 当前数据会被删除。"
    # Drop and recreate rather than restoring on top: pg_restore into a
    # populated schema produces a pile of "already exists" errors and leaves a
    # database that is half old, half new — the worst possible outcome.
    "${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres \
        -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE);" \
        -c "CREATE DATABASE \"$DB\";" \
        || die "重建数据库失败"
    "${COMPOSE[@]}" exec -T "$DB_SERVICE" \
        pg_restore -U "$DB_USER" -d "$DB" --no-owner --no-privileges "$CONTAINER_DUMP" \
        || die "pg_restore 返回非零（可能只是 ACL 警告，请检查上面的输出）"
    echo
    echo "还原完成 -> $DB"
    echo "业务服务与网关持有的是旧连接，请重启它们："
    echo "    docker compose -f $COMPOSE_FILE restart business-service gateway"
    exit 0
fi

# -- 1b. non-destructive verification ---------------------------------------- #
SCRATCH="support_verify_$$"
echo "--> 还原到临时库 $SCRATCH 并比对行数"

"${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS \"$SCRATCH\";" \
    -c "CREATE DATABASE \"$SCRATCH\";" \
    || die "无法创建临时库"

# Make sure the scratch DB never outlives this script, even on failure.
trap '"${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"'"$SCRATCH"'\" WITH (FORCE);" >/dev/null 2>&1 || true; "${COMPOSE[@]}" exec -T "$DB_SERVICE" rm -f "$CONTAINER_DUMP" >/dev/null 2>&1 || true' EXIT

"${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    pg_restore -U "$DB_USER" -d "$SCRATCH" --no-owner --no-privileges "$CONTAINER_DUMP" \
    || die "pg_restore 失败——这个备份不可用"

# Compare against the exact counts recorded at backup time. A table that
# restored with fewer rows than it had is precisely the silent corruption this
# catches.
#
# The same ``query_to_xml`` count(*) the backup used — deliberately not
# ``n_live_tup``, which is an autovacuum-refreshed estimate and produced false
# mismatches (28 vs 27) that look identical to real data loss. Both sides now
# count for real, so a difference means rows are genuinely missing.
ACTUAL="$("${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$SCRATCH" -At -F' ' -c "
SELECT relname,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, relname),
                           false, true, '')))[1]::text::bigint
FROM pg_stat_user_tables
ORDER BY relname;")"

FAILED=0
echo
printf '  %-28s %10s %10s\n' 表 备份时 还原后
while read -r name expected; do
    [ -n "$name" ] || continue
    got="$(printf '%s\n' "$ACTUAL" | awk -v t="$name" '$1==t{print $2}')"
    got="${got:-0}"
    if [ "$got" = "$expected" ]; then
        printf '  %-28s %10s %10s\n' "$name" "$expected" "$got"
    else
        printf '  %-28s %10s %10s  <== 不一致\n' "$name" "$expected" "$got"
        FAILED=1
    fi
done < <(awk '/^## row counts/{f=1;next} f&&NF==2{print}' "$SRC/MANIFEST.txt")

echo
if [ "$FAILED" -ne 0 ]; then
    die "行数与备份时不符——备份不完整"
fi
echo "校验通过: $SRC 可以完整还原（临时库 $SCRATCH 已删除）"
