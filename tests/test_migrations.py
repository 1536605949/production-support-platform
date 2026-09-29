# -*- coding: utf-8 -*-
"""Guards for the Alembic migration tree.

The bug this tree was introduced to fix is a *silent* one: models were created
with ``Base.metadata.create_all()``, which only ever creates. Adding a column to
a model therefore did nothing to a database that already had the table, and the
result was not an error at deploy time but a failed query at run time.

These tests cover the offline half of that guard — the revision chain and the
table set. The column-level half needs a live PostgreSQL and is covered by
``alembic check`` (see ``scripts/migrate.sh check`` and the deploy runbook),
because autogenerate's type comparison is dialect-dependent and asserting it
against SQLite would prove nothing about production.
"""
from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / 'migrations' / 'versions'


def _scripts() -> ScriptDirectory:
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(ROOT / 'migrations'))
    return ScriptDirectory.from_config(cfg)


def _upgrade_body(src: str) -> str:
    """Source of ``upgrade()`` only.

    ``downgrade()`` drops the same tables ``upgrade()`` creates, so parsing the
    whole file would subtract every table from itself and report an empty schema.
    A ``drop_table`` *inside* an ``upgrade()`` is meaningful — it means a later
    migration removed a table — so only the downgrade half is excluded.
    """
    after = src.split('def upgrade() -> None:', 1)[1]
    return after.split('def downgrade() -> None:', 1)[0]


def _table_names_by_revision() -> dict[str, set[str]]:
    """Tables each revision's ``upgrade()`` creates, minus the ones it drops.

    Parsed from source rather than by executing the revision: running an
    ``upgrade()`` needs an alembic migration context and a connection, and this
    test must work without a database.
    """
    out: dict[str, set[str]] = {}
    for path in sorted(VERSIONS.glob('*.py')):
        body = _upgrade_body(path.read_text(encoding='utf-8'))
        created = set(re.findall(r"op\.create_table\(\s*['\"]([a-z_]+)['\"]", body))
        dropped = set(re.findall(r"op\.drop_table\(\s*['\"]([a-z_]+)['\"]", body))
        out[path.name] = created - dropped
    return out


def test_there_is_exactly_one_head() -> None:
    """A branched revision tree means ``upgrade head`` is ambiguous.

    Alembic refuses to guess when there are multiple heads, so a merge revision
    would be needed — better to catch the branch the moment it is created.
    """
    heads = _scripts().get_heads()
    assert len(heads) == 1, f'迁移历史出现分叉，需要 merge revision: {heads}'


def test_baseline_is_the_root() -> None:
    script = _scripts().get_revision('0001_baseline')
    assert script is not None, '找不到基线 revision 0001_baseline'
    assert script.down_revision is None, '基线不应有前置 revision'


def test_revision_ids_are_human_readable() -> None:
    """Revision ids are chosen, not auto-generated hex.

    ``scripts/migrate.sh`` has to stamp the baseline by name when adopting an
    existing database; a random hex id would make that script reference a value
    nobody can predict.
    """
    for rev in _scripts().walk_revisions():
        assert re.match(r'^\d{4}_[a-z0-9_]+$', rev.revision), (
            f'revision id 应为 0001_xxx 形式，实际是 {rev.revision!r}；'
            f'生成时请加 --rev-id'
        )


def test_migration_table_set_matches_the_models() -> None:
    """Every model table has a migration, and no migration invents a table.

    This is the offline drift guard: add a model without a migration and this
    fails immediately, instead of failing in production with
    "column does not exist" the first time the new field is written.
    """
    from business_service.app import models as business_models  # noqa: F401
    from business_service.app.db import Base as BusinessBase
    from gateway.app import models as gateway_models  # noqa: F401
    from gateway.app.db import Base as GatewayBase

    declared = set(GatewayBase.metadata.tables) | set(BusinessBase.metadata.tables)

    migrated: set[str] = set()
    for tables in _table_names_by_revision().values():
        migrated |= tables

    missing = declared - migrated
    extra = migrated - declared
    assert not missing, f'这些模型表没有对应的迁移: {sorted(missing)}'
    assert not extra, f'这些迁移建了模型里不存在的表: {sorted(extra)}'


def test_baseline_covers_both_services() -> None:
    """One database, one schema history — the baseline must span both services.

    The gateway and the business service share a PostgreSQL database. If the
    baseline only covered one of them, adopting Alembic would stamp the revision
    as applied while leaving the other service's tables outside the history, so
    their future changes could never be migrated.
    """
    baseline = _table_names_by_revision()['0001_baseline_baseline_schema.py']
    assert 'orders' in baseline, '基线缺少业务服务的表（orders）'
    assert 'conversations' in baseline, '基线缺少网关的表（conversations）'
    assert len(baseline) == 17, f'基线应含 17 张表，实际 {len(baseline)}'


def test_baseline_is_reversible() -> None:
    """A baseline whose downgrade is ``pass`` makes ``downgrade base`` a lie."""
    src = (VERSIONS / '0001_baseline_baseline_schema.py').read_text(encoding='utf-8')
    downgrade_body = src.split('def downgrade() -> None:', 1)[1]
    assert 'op.drop_table(' in downgrade_body, '基线的 downgrade 没有真正删表'


def test_env_targets_both_metadatas() -> None:
    """``target_metadata`` must be a list of both Bases, not a single one.

    With only the gateway's metadata, autogenerate would treat every business
    table as "to be dropped" and the next migration would delete the order data.
    """
    src = (ROOT / 'migrations' / 'env.py').read_text(encoding='utf-8')
    assert 'target_metadata = [GatewayBase.metadata, BusinessBase.metadata]' in src


def test_type_and_default_comparison_is_enabled() -> None:
    """Autogenerate only reports what it is asked to compare.

    With ``compare_type`` off, changing ``String(32)`` to ``String(64)`` produces
    an empty migration — the same silent no-op this tree exists to prevent.
    """
    src = (ROOT / 'migrations' / 'env.py').read_text(encoding='utf-8')
    assert src.count('compare_type=True') >= 2, 'offline 与 online 两条路径都要开 compare_type'
    assert src.count('compare_server_default=True') >= 2
