"""index (tenant_id, created_at) for the analytics window

Revision ID: 0002_analytics_indexes
Revises: 0001_baseline
Create Date: 2026-09-24 12:52:27.890955+00:00

Why these two indexes exist
---------------------------
``/api/admin/overview`` filters both tables with
``WHERE tenant_id = ? AND created_at >= ?``.

Neither table had an index covering that predicate. What they had was:

* ``ix_messages_scope (tenant_id, conversation_id, seq)``
* ``ix_conversations_scope (tenant_id, user_id, last_message_at)``

Both lead with ``tenant_id``, so PostgreSQL can seek on the tenant — and then
has to read every one of that tenant's rows to evaluate ``created_at`` as a
residual filter. The index makes the second column of the predicate a seek
condition too, so the window becomes a range scan.

This matters most for ``messages``, which grows with every turn. Before this,
the cost of opening the console grew with the tenant's entire history rather
than with the size of the window being viewed.

``messages`` is also the table this migration will be slowest on, so:

Note for large tables
---------------------
``CREATE INDEX`` takes a lock that blocks writes for the duration. At the time
of writing ``messages`` holds a few hundred rows, so a plain build is correct
and simplest. If you are applying this to a table with millions of rows, use
``CREATE INDEX CONCURRENTLY`` instead — it does not block writes, but it cannot
run inside a transaction, so it needs ``op.get_context().autocommit_block()``
and a separate check for invalid indexes afterwards (a failed concurrent build
leaves an INVALID index behind rather than rolling back).
"""
from __future__ import annotations

from alembic import op

revision = '0002_analytics_indexes'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_conversations_tenant_created', 'conversations',
        ['tenant_id', 'created_at'], unique=False,
    )
    op.create_index(
        'ix_messages_tenant_created', 'messages',
        ['tenant_id', 'created_at'], unique=False,
    )


def downgrade() -> None:
    # Dropping an index is safe and cheap; the queries still work without them,
    # they just degrade to scanning the tenant's whole history again.
    op.drop_index('ix_messages_tenant_created', table_name='messages')
    op.drop_index('ix_conversations_tenant_created', table_name='conversations')
