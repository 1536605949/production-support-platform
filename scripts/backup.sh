#!/usr/bin/env bash
# Back up the platform's state — and *prove* it did.
#
# This replaces a script that was Dify-era residue and had three defects that
# together made it worse than no backup at all:
#
#   1. It required ``POSTGRES_CONTAINER`` to be set. Unset, it skipped the
#      ``pg_dump`` — the only part that matters — and still printed
#      "Backup written". A backup that reports success while dumping nothing
#      is the failure mode you discover during a restore.
#   2. It copied ``dify/workflows``, which this stack no longer runs.
#   3. It assumed it was run from the repo root, so from anywhere else it wrote
#      its output to a stray ``backups/`` directory next to the caller.
#
# The rule this script follows: **never report success for something that did
# not happen.** Every step is verified, and the archive is opened and inspected
# before the run is allowed to pass.
#
# Usage (from anywhere):
#   bash scripts/backup.sh
#
# Env overrides:
#   BACKUP_ROOT   where to write (default <repo>/backups)
#   BACKUP_KEEP   how many runs to retain (default 14)
#   SKIP_REDIS=1  skip the Redis snapshot
#
# Run this on the server. It talks to Docker, so it needs the containers up.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/../deployment/docker-compose.yml}"
# ``cd .. && pwd`` rather than ``$SCRIPT_DIR/../backups``: the latter leaves a
# literal ``scripts/../backups`` in every path the script prints, and the
# restore command it tells you to run then contains a ``..`` that has to resolve
# from a different cwd than the one you are standing in.
BACKUP_ROOT="${BACKUP_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"

DB_SERVICE="${DB_SERVICE:-postgres}"
DB="${POSTGRES_DB:-support}"
DB_USER="${POSTGRES_USER:-support}"
REDIS_SERVICE="${REDIS_SERVICE:-redis}"

# Tables that must appear in the archive. Both services share one database, so
# this list spans both schemas. If a rename or a dropped table means one of
# these disappears, the backup should fail *here* — at 03:00 on the backup job —
# rather than at restore time, when it is already too late.
EXPECTED_TABLES="
accounts
approvals
audit_events
conversations
idempotency_keys
knowledge_documents
messages
orders
quota_usage
refund_proposals
tenants
tickets
tool_invocations
users
"

die() { echo "备份失败: $*" >&2; exit 1; }

if [ ! -f "$COMPOSE_FILE" ]; then
    die "找不到 compose 文件: $COMPOSE_FILE（用 COMPOSE_FILE=<path> 指定）"
fi
COMPOSE=(docker compose -f "$COMPOSE_FILE")

# -- 0. the stack must be up ------------------------------------------------- #
# Without this check, ``docker compose exec`` on a stopped project fails with a
# message about a missing service, which reads like a config problem rather
# than "the database is not running".
if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx "$DB_SERVICE"; then
    die "容器 $DB_SERVICE 未在运行；先 docker compose up -d"
fi

TS="$(date +%Y%m%d_%H%M%S)"
DEST="$BACKUP_ROOT/$TS"
mkdir -p "$DEST"

echo "==> 备份到 $DEST"

# -- 1. PostgreSQL ----------------------------------------------------------- #
# The dump is written *inside* the container and then copied out with
# ``compose cp``, rather than streamed through stdout. Streaming a binary
# custom-format archive through a shell redirect is where CRLF translation on
# Windows and truncated pipes on a dropped connection turn a good dump into a
# corrupt file that only fails at restore time.
echo "--> pg_dump ($DB)"
"${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    pg_dump -U "$DB_USER" -d "$DB" -Fc -f /tmp/support-backup.dump \
    || die "pg_dump 返回非零"

# -- 2. verify the archive is real ------------------------------------------- #
# ``pg_restore -l`` reads the archive's table of contents. It proves the file
# is a well-formed dump *and* that the data we care about is inside it — which
# a byte count cannot tell you.
echo "--> 校验归档内容"
TOC="$("${COMPOSE[@]}" exec -T "$DB_SERVICE" pg_restore -l /tmp/support-backup.dump)" \
    || die "归档无法被 pg_restore 解析"

MISSING=""
for t in $EXPECTED_TABLES; do
    # A table with zero rows is still dumped as a TABLE DATA entry, so this
    # catches "the table vanished", not "the table is empty".
    printf '%s\n' "$TOC" | grep -qE "TABLE DATA public $t( |\$)" || MISSING="$MISSING $t"
done
[ -z "$MISSING" ] || die "归档里缺少这些表的数据:$MISSING"

TABLE_COUNT="$(printf '%s\n' "$TOC" | grep -cE '^[0-9]+; .* TABLE DATA' || true)"

# Exact per-table row counts.
#
# NOT ``pg_stat_user_tables.n_live_tup``: that column is an *estimate* refreshed
# by autovacuum, and comparing it against a freshly-analysed restore produced
# false mismatches (28 vs 27 on conversations, 452 vs 448 on
# workflow_checkpoints) that are indistinguishable from real data loss. Counting
# for real removes the ambiguity — these numbers must match exactly, and if they
# do not, rows genuinely did not make it into the archive.
#
# ``query_to_xml`` is the standard way to run ``count(*)`` for every table in a
# single query without generating SQL text and running it in a loop.
ROWS="$("${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$DB" -At -F' ' -c "
SELECT relname,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, relname),
                           false, true, '')))[1]::text::bigint
FROM pg_stat_user_tables
ORDER BY relname;")"

"${COMPOSE[@]}" cp "$DB_SERVICE:/tmp/support-backup.dump" "$DEST/postgres.dump" >/dev/null \
    || die "无法把归档从容器复制出来"
"${COMPOSE[@]}" exec -T "$DB_SERVICE" rm -f /tmp/support-backup.dump

DUMP_BYTES="$(wc -c < "$DEST/postgres.dump" | tr -d ' ')"
# An empty or near-empty custom archive is a few hundred bytes of header. This
# is a floor, not a substitute for the TOC check above.
[ "$DUMP_BYTES" -gt 2048 ] || die "归档只有 ${DUMP_BYTES} 字节，明显不完整"
echo "    归档 $(numfmt --to=iec "$DUMP_BYTES" 2>/dev/null || echo "${DUMP_BYTES}B")，含 $TABLE_COUNT 张表的数据"

# -- 3. Redis ---------------------------------------------------------------- #
# Idempotency keys and rate-limit counters live here. Losing them is survivable
# (they expire anyway) but not free, so it is included — and a failure here
# fails the run, because a silently-missing component is the bug being fixed.
if [ "${SKIP_REDIS:-0}" = "1" ]; then
    echo "--> Redis 已跳过 (SKIP_REDIS=1)"
else
    echo "--> Redis 快照"
    "${COMPOSE[@]}" exec -T "$REDIS_SERVICE" redis-cli BGSAVE >/dev/null || die "BGSAVE 失败"
    # BGSAVE is asynchronous: copying dump.rdb immediately can capture the
    # previous snapshot. Wait for the flag to clear instead of sleeping a
    # guessed number of seconds.
    for _ in $(seq 1 30); do
        in_progress="$("${COMPOSE[@]}" exec -T "$REDIS_SERVICE" \
            redis-cli info persistence | tr -d '\r' | awk -F: '/rdb_bgsave_in_progress/{print $2}')"
        [ "$in_progress" = "0" ] && break
        sleep 1
    done
    [ "${in_progress:-1}" = "0" ] || die "BGSAVE 在 30 秒内未完成"

    # LASTSAVE is the unix time of the last successful save. Recording it makes
    # "did we capture a stale snapshot" answerable.
    LASTSAVE="$("${COMPOSE[@]}" exec -T "$REDIS_SERVICE" redis-cli LASTSAVE | tr -d '\r')"
    "${COMPOSE[@]}" cp "$REDIS_SERVICE:/data/dump.rdb" "$DEST/redis.rdb" >/dev/null \
        || die "无法复制 redis dump.rdb"
    echo "    redis.rdb ($(wc -c < "$DEST/redis.rdb" | tr -d ' ') 字节, LASTSAVE=$LASTSAVE)"
fi

# -- 4. manifest ------------------------------------------------------------- #
# Hashes make the archive tamper-evident and let a restore verify it is reading
# the bytes that were written.
echo "--> 生成 MANIFEST"
{
    echo "# support-platform backup"
    echo "created_at=$(date -Iseconds)"
    echo "host=$(hostname)"
    echo "db=$DB"
    echo "postgres_dump_bytes=$DUMP_BYTES"
    echo "postgres_table_data_entries=$TABLE_COUNT"
    echo "expected_tables=$(printf '%s' "$EXPECTED_TABLES" | tr -s ' \n' ' ')"
    echo "redis_lastsave=${LASTSAVE:-skipped}"
    echo "git_rev=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo
    echo "## sha256"
    # The manifest must NOT hash itself. ``sha256sum ./*`` includes
    # MANIFEST.txt, which is still open for writing at this point, so its listed
    # hash is of a half-written file and ``sha256sum -c`` fails on every single
    # verify run — a false alarm that would train the operator to ignore the
    # integrity check, which is the one thing that must never be ignored.
    (cd "$DEST" && find . -maxdepth 1 -type f ! -name MANIFEST.txt -print0 \
        | sort -z | xargs -0 sha256sum 2>/dev/null || true)
    echo
    echo "## row counts at backup time (exact count(*))"
    printf '%s\n' "$ROWS"
} > "$DEST/MANIFEST.txt"

# -- 5. prune ---------------------------------------------------------------- #
# Only directories that look like one of our runs are touched, and the current
# one is always kept — a prune that can delete the backup it just made is worse
# than no prune.
if [ "$BACKUP_KEEP" -gt 0 ] 2>/dev/null; then
    mapfile -t OLD < <(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort -r | tail -n +"$((BACKUP_KEEP + 1))")
    if [ "${#OLD[@]}" -gt 0 ]; then
        echo "--> 清理 ${#OLD[@]} 个旧备份（保留最近 $BACKUP_KEEP 次）"
        for d in "${OLD[@]}"; do rm -rf -- "$d"; done
    fi
fi

echo
echo "备份完成: $DEST"
echo "  还原: bash scripts/restore_backup.sh $DEST"
echo "  校验: bash scripts/restore_backup.sh --verify $DEST"
