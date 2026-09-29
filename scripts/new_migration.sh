#!/usr/bin/env bash
# Generate a new Alembic migration — and prove the file actually exists afterwards.
#
# Why this is a script rather than a one-line `make` target
# --------------------------------------------------------
# The naive invocation is
#
#   docker compose run --rm --no-deps gateway alembic revision --autogenerate -m "..."
#
# and it has three failure modes, all of them silent:
#
# 1. **The file never reaches the host.** ``gateway/Dockerfile`` COPYs the source
#    in; the ``gateway`` service declares no bind mount. So
#    ``/app/migrations/versions/`` lives in the container's writable layer, and
#    ``--rm`` deletes that layer on exit. alembic writes the migration, prints
#    the path, the command exits 0 — and the file is gone. Verified against the
#    deployment: writing a file into ``/app/migrations/versions/`` from a
#    ``run --rm`` container leaves nothing behind in the host checkout.
#
# 2. **autogenerate diffs the wrong models.** The diff is models-vs-live-database.
#    With the image's copy of ``models.py`` in play, an edit made on the host is
#    invisible to autogenerate and the result is an *empty* migration — the same
#    silent no-op this migration tree exists to prevent. (The same probe showed
#    the image's ``migrations/versions/`` does not contain the host's newest
#    migration at all, so the image is a snapshot, not a view of the source.)
#
# 3. **The revision id is random hex.** ``scripts/migrate.sh`` stamps the baseline
#    *by name* (``0001_baseline``), and
#    ``tests/test_migrations.py::test_revision_ids_are_human_readable`` rejects
#    anything that does not match ``^\d{4}_[a-z0-9_]+$``.
#
# One bind mount of the repository over ``/app`` fixes (1) and (2); an explicit
# ``--rev-id`` fixes (3); and the assertion at the end is what makes (1) a fact
# rather than a configuration that is merely supposed to work.
#
# Usage:
#   bash scripts/new_migration.sh "add refund audit column"
#   bash scripts/new_migration.sh "add refund audit column" refund_audit
#
# Env:
#   MIGRATE_SERVICE  container carrying alembic (default: gateway)
#   REV_ID           override the revision id (must still be 000N_slug)
#   COMPOSE_FILE     override the compose file

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deployment/docker-compose.yml}"
SERVICE="${MIGRATE_SERVICE:-gateway}"
VERSIONS="$ROOT/migrations/versions"

die() { echo "生成迁移失败: $*" >&2; exit 1; }

# ``--dry-run`` may appear anywhere; pull it out before reading positionals so it
# cannot be mistaken for the description.
DRY_RUN="${DRY_RUN:-}"
POSITIONAL=()
for _arg in "$@"; do
    case "$_arg" in
    --dry-run) DRY_RUN=1 ;;
    *) POSITIONAL+=("$_arg") ;;
    esac
done

MSG="${POSITIONAL[0]:-}"
[ -n "$MSG" ] || die '用法: scripts/new_migration.sh "迁移说明" [slug] [--dry-run]'

# The slug becomes the tail of the revision id, and the revision id is the
# filename stem (see ``file_template`` in alembic.ini), so it has to be a valid
# Python-module fragment: lowercase letters, digits, underscores.
_slugify() {
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's/[^a-z0-9]\+/_/g' -e 's/^_*//' -e 's/_*$//'
}
SLUG="$(_slugify "${POSITIONAL[1]:-$MSG}")"
[ -n "$SLUG" ] || SLUG='migration'

# The next ordinal comes from the filenames rather than from ``alembic history``:
# this has to work without a database and without alembic installed, so the id
# can be computed — and rejected if malformed — before anything is started.
LAST="$(ls -1 "$VERSIONS" 2>/dev/null | sed -n 's/^\([0-9]\{4\}\)_.*/\1/p' | sort -n | tail -1)"
NEXT="$(printf '%04d' $(( 10#${LAST:-0} + 1 )))"

REV_ID="${REV_ID:-${NEXT}_${SLUG}}"
case "$REV_ID" in
[0-9][0-9][0-9][0-9]_*) ;;
*) die "revision id 必须是 000N_xxx 形式，实际是 '$REV_ID'（autogenerate 默认的随机 hex 会被 tests/test_migrations.py 拒绝）" ;;
esac

[ -f "$COMPOSE_FILE" ] || die "找不到 compose 文件: $COMPOSE_FILE"
GENERATED="$VERSIONS/$REV_ID.py"
if [ -e "$GENERATED" ]; then
    die "$GENERATED 已存在；换一个 slug，或先删掉它"
fi

echo "==> alembic revision --autogenerate --rev-id $REV_ID -m \"$MSG\""

# ``--dry-run`` stops here and prints what would happen. It exists so the parts
# of this script that are pure logic — the ordinal, the slug, the id format —
# can be tested without Docker (see tests/test_migration_tooling.py). A guard
# that can only be exercised by running Docker on a live database does not get
# exercised.
if [ -n "$DRY_RUN" ]; then
    echo "(dry-run) 会写入: migrations/versions/$(basename "$GENERATED")"
    echo "(dry-run) 命令: docker compose -f $COMPOSE_FILE run --rm --no-deps \\"
    echo "             --user $(id -u):$(id -g) -v $ROOT:/app $SERVICE \\"
    echo "             alembic revision --autogenerate --rev-id $REV_ID -m \"$MSG\""
    exit 0
fi

# ``--user`` so the generated file is owned by whoever ran the script instead of
# by root: the bind mount writes straight into the host checkout, and a
# root-owned file there is one the deploying user can no longer edit.
#
# The output is captured rather than streamed so that alembic's two common
# refusals can be translated into an instruction. "Target database is not up to
# date" is the one that matters: autogenerate diffs models against the *current*
# database, so running it while a migration is still pending would generate that
# pending change a second time — a migration that fails on the next deploy with
# "already exists".
if ! OUT="$(docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
        --user "$(id -u):$(id -g)" \
        -v "$ROOT:/app" \
        "$SERVICE" alembic revision --autogenerate --rev-id "$REV_ID" -m "$MSG" 2>&1)"; then
    printf '%s\n' "$OUT" >&2
    case "$OUT" in
    *'not up to date'*)
        die "数据库不在 head 上。autogenerate 是对着**当前**数据库求差分的，
    带着待应用的迁移跑，会把那条迁移的改动再生成一遍。
    先应用：
        bash scripts/migrate.sh upgrade
    或者先看有没有漂移：
        bash scripts/migrate.sh check"
        ;;
    *)
        die "alembic 没有成功，见上面的输出"
        ;;
    esac
fi
printf '%s\n' "$OUT"

# The assertion is the point of this script. Everything above is a configuration
# that *should* put the file on the host; this is the check that it did.
[ -f "$GENERATED" ] || die "alembic 没有在宿主机上生成 $GENERATED
    最可能的原因：文件被写进了容器的可写层，并随 --rm 一起删掉了。
    检查上面那条 docker compose run 是否带上了 -v \"$ROOT:/app\"。"

# An autogenerate that finds no difference still writes a file with empty
# upgrade()/downgrade(). That is the exact failure this whole migration tree was
# built to remove — a schema change that produces no error and no effect — so say
# it out loud instead of letting it be committed as a "migration".
if ! grep -q 'op\.' "$GENERATED"; then
    echo
    echo "!! 警告：生成的迁移里没有任何 op. 调用 —— autogenerate 认为模型与数据库一致。"
    echo "   可能的原因："
    echo "     * 模型改动没保存，或者改的是容器里那份旧模型（本脚本已挂载 \$ROOT，不应发生）"
    echo "     * 改动是 autogenerate 看不见的类型（索引改名、CHECK 约束、数据迁移）"
    echo "   如果本来就不该有 DDL，删掉这个文件：rm $GENERATED"
fi

echo
echo "已生成: migrations/versions/$(basename "$GENERATED")"
echo "下一步："
echo "  1) 读一遍文件，确认 DDL 就是你要的"
echo "     （autogenerate 看不到索引改名、CHECK 约束、数据迁移）"
echo "  2) bash scripts/migrate.sh check     # 确认模型与迁移无漂移"
echo "  3) bash scripts/migrate.sh upgrade"
