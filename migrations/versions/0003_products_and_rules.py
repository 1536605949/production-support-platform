"""product catalogue and structured after-sale rules

Revision ID: 0003_products_and_rules
Revises: 0002_analytics_indexes
Create Date: 2026-09-26 10:20:00.000000+00:00

Why this migration exists
-------------------------
Two new tables plus two new columns on ``orders``:

* ``sku`` — name, spec, category, stock, price. The catalogue is the only
  place stock and price are stated, so "还有货吗" has one answer and it is a
  lookup rather than a guess.
* ``aftersale_rule`` — the no-reason return policy as rows keyed by product
  category, not as prose. The same policy is *also* in the gateway's knowledge
  base, where it answers "退货政策是怎样的". This table answers "这一单能不能
  退", which is a different question and must not be answered by a model.
* ``orders.sku_id`` / ``orders.signed_at`` — the two inputs the rule needs.
  Both are nullable: orders placed before the catalogue existed have no SKU, and
  the return window is measured from signing, so a missing ``signed_at`` means
  "unknown", never "expired".

Why this could not be left to ``create_all()``
----------------------------------------------
``Base.metadata.create_all()`` creates tables but never alters them. The two new
tables would have appeared on the next boot, but the two new columns on
``orders`` — a table that already exists in production — would not. The failure
mode is not an error at deploy time: it is ``UndefinedColumn`` on the first
query that touches the new column, i.e. the first time anyone asks about a
return. Hence: tables and columns both go through Alembic.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0003_products_and_rules'
down_revision = '0002_analytics_indexes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('sku',
    sa.Column('sku_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('spec', sa.String(length=200), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('stock', sa.Integer(), nullable=False),
    sa.Column('price_cents', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('sku_id')
    )
    op.create_index(op.f('ix_sku_category'), 'sku', ['category'], unique=False)

    op.create_table('aftersale_rule',
    sa.Column('rule_id', sa.String(length=64), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('rule_type', sa.String(length=32), nullable=False),
    sa.Column('window_days', sa.Integer(), nullable=False),
    sa.Column('eligible', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('rule_id')
    )
    op.create_index(op.f('ix_aftersale_rule_category'), 'aftersale_rule', ['category'], unique=False)

    # Nullable with no server default on purpose. A NOT NULL column added to a
    # populated table needs a default, and any default here would be a lie: ''''
    # for sku_id and the epoch for signed_at would both read as real values, and
    # the eligibility check treats an unknown signed_at as "cannot decide"
    # rather than "expired". Writing a fake timestamp would silently start
    # refusing returns.
    op.add_column('orders', sa.Column('sku_id', sa.String(length=64), nullable=True))
    op.add_column('orders', sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_orders_sku_id'), 'orders', ['sku_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_orders_sku_id'), table_name='orders')
    op.drop_column('orders', 'signed_at')
    op.drop_column('orders', 'sku_id')
    op.drop_index(op.f('ix_aftersale_rule_category'), table_name='aftersale_rule')
    op.drop_table('aftersale_rule')
    op.drop_index(op.f('ix_sku_category'), table_name='sku')
    op.drop_table('sku')
