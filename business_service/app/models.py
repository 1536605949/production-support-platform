from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def now(): return datetime.now(timezone.utc)

class Account(Base):
    __tablename__ = 'accounts'
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    level: Mapped[str] = mapped_column(String(32), default='STANDARD')
    status: Mapped[str] = mapped_column(String(32), default='active')

class Order(Base):
    __tablename__ = 'orders'
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('accounts.user_id'), index=True)
    status: Mapped[str] = mapped_column(String(32))
    amount_cents: Mapped[int] = mapped_column(Integer)
    refund_available: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Which product this order is for. Nullable on purpose: orders placed before
    #: the product table existed have no SKU, and backfilling one would mean
    #: inventing a fact. An unknown SKU must stay "unknown", never "ineligible".
    sku_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: When the buyer signed for it. Also nullable, and for the same reason: the
    #: no-reason return window is measured from this instant, so a missing value
    #: cannot be read as "expired" — that would refuse returns the shop owes.
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Sku(Base):
    """A sellable product. Stock and price live here and nowhere else.

    The Agent is not allowed to reason about availability; it asks this table.
    """
    __tablename__ = 'sku'
    sku_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    spec: Mapped[str] = mapped_column(String(200), default='')
    #: Product class, e.g. ``apparel`` / ``digital`` / ``custom`` / ``fresh``.
    #: This is the join key onto :class:`AftersaleRule`.
    category: Mapped[str] = mapped_column(String(32), index=True)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default='on_sale')

class AftersaleRule(Base):
    """Structured after-sale policy, keyed by product category.

    These rules also exist as prose in the knowledge base, where they answer
    "how does the return policy work". This table is the other half: it answers
    "is *this* order allowed to return", and it is the half that must never be
    delegated to the model. Keeping both is deliberate — a rule that only lived
    in the vector store could not refuse anything.
    """
    __tablename__ = 'aftersale_rule'
    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    rule_type: Mapped[str] = mapped_column(String(32), default='return_no_reason')
    #: Days after signing during which the request is allowed. 0 means the
    #: category never qualifies, which is expressed by ``eligible=False``.
    window_days: Mapped[int] = mapped_column(Integer, default=0)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default='')

class Ticket(Base):
    __tablename__ = 'tickets'
    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    issue: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default='created')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class RefundProposal(Base):
    __tablename__ = 'refund_proposals'
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default='pending_review')
    review_comment: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), default='')
    resource_id: Mapped[str] = mapped_column(String(128), default='')
    detail: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
