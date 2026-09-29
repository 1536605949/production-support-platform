import json, uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from .models import Account, Order, Ticket, RefundProposal, AuditEvent, Sku, AftersaleRule, now


def audit(db: Session, event_type: str, user_id='', resource_id='', detail=None):
    db.add(AuditEvent(
        event_type=event_type,
        user_id=user_id,
        resource_id=resource_id,
        detail=json.dumps(detail or {}, ensure_ascii=False),
    ))


#: Demo catalogue. ``SKU-2001`` having zero stock is intentional — it is the
#: out-of-stock case the acceptance run exercises, and the only way the Agent
#: can answer "还有货吗" truthfully is by reading this column.
DEMO_SKUS = [
    # sku_id, name, spec, category, stock, price_cents
    ('SKU-1001', '纯棉基础款圆领T恤', 'M / 白色', 'apparel', 128, 9900),
    ('SKU-2001', '定制刻字保温杯', '350ml / 激光刻字', 'custom', 0, 15900),
    ('SKU-3001', '阳澄湖大闸蟹礼券', '8只装', 'fresh', 42, 39900),
    ('SKU-4001', '主动降噪蓝牙耳机', '白色 / 标准版', 'digital', 7, 29900),
    ('SKU-5001', '在线课程兑换码', '年度会员', 'digital_opened', 15, 19900),
]

#: Structured after-sale policy. The same rules also live as prose in the
#: gateway's knowledge base (``gateway/app/seed.py``), which is what answers
#: "退货政策是怎样的". This table is what answers "这一单能不能退" — the part
#: that must be decided by a lookup, not by a model.
DEMO_AFTERSALE_RULES = [
    # rule_id, category, window_days, eligible, reason
    ('AR-apparel', 'apparel', 7, True,
     '自签收次日起 7 天内，商品未使用、包装完好、配件齐全可申请无理由退货。'),
    ('AR-digital', 'digital', 7, True,
     '自签收次日起 7 天内，商品未使用、包装完好可申请无理由退货。'),
    ('AR-custom', 'custom', 0, False, '定制类商品不支持七天无理由退货。'),
    ('AR-fresh', 'fresh', 0, False, '生鲜类商品不支持七天无理由退货。'),
    ('AR-digital-opened', 'digital_opened', 0, False,
     '拆封的数字化商品不支持七天无理由退货。'),
]

#: ``order_id, user_id, sku_id, status, amount_cents, signed_days_ago``
#:
#: Three of the four exist to be *refused*, so the acceptance run has something
#: real to refuse: ``ORD-1002`` is a customised item, ``ORD-1003`` is well past
#: the 7-day window. ``ORD-2001`` must stay owned by ``user002`` — the ownership
#: test depends on ``user001`` being unable to see it.
DEMO_ORDERS = [
    ('ORD-1001', 'user001', 'SKU-1001', 'shipped', 19900, 3),
    ('ORD-1002', 'user001', 'SKU-2001', 'delivered', 15900, 3),
    ('ORD-1003', 'user001', 'SKU-4001', 'delivered', 29900, 30),
    ('ORD-2001', 'user002', 'SKU-1001', 'processing', 8900, None),
]


def seed(db: Session):
    if not db.get(Account, 'user001'):
        db.add(Account(user_id='user001', level='VIP', status='active'))
    if not db.get(Account, 'user002'):
        db.add(Account(user_id='user002', level='STANDARD', status='active'))

    for sku_id, name, spec, category, stock, price_cents in DEMO_SKUS:
        if db.get(Sku, sku_id) is None:
            db.add(Sku(sku_id=sku_id, name=name, spec=spec, category=category,
                       stock=stock, price_cents=price_cents, status='on_sale'))

    for rule_id, category, window_days, eligible, reason in DEMO_AFTERSALE_RULES:
        if db.get(AftersaleRule, rule_id) is None:
            db.add(AftersaleRule(rule_id=rule_id, category=category,
                                 rule_type='return_no_reason',
                                 window_days=window_days, eligible=eligible,
                                 reason=reason))

    for order_id, user_id, sku_id, status, amount_cents, days_ago in DEMO_ORDERS:
        signed_at = (now() - timedelta(days=days_ago)) if days_ago is not None else None
        order = db.get(Order, order_id)
        if order is None:
            db.add(Order(order_id=order_id, user_id=user_id, sku_id=sku_id,
                         status=status, amount_cents=amount_cents,
                         refund_available=True, signed_at=signed_at))
        else:
            # Re-anchor the demo deadlines on every boot. The window is measured
            # *from signing*, so a timestamp written once at first deploy would
            # push ORD-1001 out of its own 7-day window a week later and the
            # demo would start refusing a return it is meant to allow.
            order.sku_id = sku_id
            order.signed_at = signed_at
    db.commit()


def get_owned_order(db: Session, user_id: str, order_id: str):
    order = db.get(Order, order_id)
    if not order or order.user_id != user_id:
        return None
    return order


def get_sku(db: Session, sku_id: str):
    return db.get(Sku, sku_id)


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to UTC-aware.

    SQLite hands back naive datetimes and PostgreSQL returns aware ones;
    comparing a naive value to an aware ``now()`` raises ``TypeError`` instead
    of answering, so the difference has to be absorbed here rather than assumed
    away at the call site.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def check_aftersale_eligibility(
    db: Session, user_id: str, order_id: str, rule_type: str = 'return_no_reason'
):
    """Decide return eligibility by looking it up, never by reasoning.

    Returns ``(verdict, error)``. ``verdict['eligible']`` is deliberately
    three-valued:

    * ``True``  — a rule was found and the order satisfies it;
    * ``False`` — a rule was found and the order violates it;
    * ``None``  — the inputs needed to decide are missing (the order carries no
      SKU, the category has no rule, the signing time is unknown).

    ``None`` must not be collapsed into ``False``. "I cannot tell" is not "no",
    and refusing on it would deny returns the shop owes — the exact failure mode
    that makes a fake rule engine worse than none.
    """
    order = get_owned_order(db, user_id, order_id)
    if not order:
        return None, 'order_not_found'

    base = {'order_id': order.order_id, 'sku_id': order.sku_id or ''}

    if not order.sku_id:
        return {**base, 'eligible': None, 'reason': 'order_has_no_sku'}, None
    sku = db.get(Sku, order.sku_id)
    if sku is None:
        return {**base, 'eligible': None, 'reason': 'sku_not_found'}, None

    base.update({'sku_name': sku.name, 'category': sku.category})

    rule = (
        db.query(AftersaleRule)
        .filter(
            AftersaleRule.category == sku.category,
            AftersaleRule.rule_type == rule_type,
        )
        .first()
    )
    if rule is None:
        return {**base, 'eligible': None, 'reason': 'no_rule_for_category'}, None

    base.update({'rule_id': rule.rule_id, 'policy': rule.reason})

    if not rule.eligible:
        return {**base, 'eligible': False, 'reason': rule.reason}, None

    signed_at = _aware(order.signed_at)
    if signed_at is None:
        return {**base, 'eligible': None, 'reason': 'signing_time_unknown'}, None

    deadline = signed_at + timedelta(days=rule.window_days)
    if now() > deadline:
        return {
            **base,
            'eligible': False,
            'reason': f'已超过签收后 {rule.window_days} 天的无理由退货期限。',
            'deadline': deadline.isoformat(),
        }, None

    return {**base, 'eligible': True, 'reason': rule.reason,
            'deadline': deadline.isoformat()}, None


def create_ticket(db: Session, user_id: str, issue: str):
    ticket = Ticket(ticket_id='T-' + uuid.uuid4().hex[:8].upper(), user_id=user_id, issue=issue, status='created')
    db.add(ticket)
    audit(db, 'ticket_created', user_id, ticket.ticket_id, {'issue': issue})
    db.commit()
    db.refresh(ticket)
    return ticket


def create_refund_proposal(db: Session, user_id: str, order_id: str, reason: str):
    order = get_owned_order(db, user_id, order_id)
    if not order:
        return None, 'order_not_found'
    if not order.refund_available:
        return None, 'refund_not_available'

    existing = (
        db.query(RefundProposal)
        .filter(
            RefundProposal.user_id == user_id,
            RefundProposal.order_id == order_id,
            RefundProposal.status.in_(['pending_review', 'approved']),
        )
        .order_by(RefundProposal.created_at.desc())
        .first()
    )
    if existing:
        return existing, None

    proposal = RefundProposal(
        proposal_id='RP-' + uuid.uuid4().hex[:8].upper(),
        user_id=user_id,
        order_id=order_id,
        reason=reason,
        status='pending_review',
    )
    db.add(proposal)
    audit(db, 'refund_proposal_created', user_id, proposal.proposal_id, {'order_id': order_id, 'reason': reason})
    db.commit()
    db.refresh(proposal)
    return proposal, None


def review_refund_proposal(
    db: Session,
    user_id: str,
    order_id: str,
    decision: str,
    reviewer_id: str,
    review_comment: str = '',
):
    """Human review is the only transition from pending_review to approved/rejected."""
    proposal = (
        db.query(RefundProposal)
        .filter(
            RefundProposal.user_id == user_id,
            RefundProposal.order_id == order_id,
            RefundProposal.status == 'pending_review',
        )
        .order_by(RefundProposal.created_at.desc())
        .first()
    )
    if not proposal:
        return None, 'pending_proposal_not_found'

    proposal.status = 'approved' if decision == 'approve' else 'rejected'
    proposal.review_comment = review_comment
    audit(
        db,
        'refund_reviewed',
        user_id,
        proposal.proposal_id,
        {
            'order_id': order_id,
            'decision': decision,
            'reviewer_id': reviewer_id,
            'review_comment': review_comment,
        },
    )
    db.commit()
    db.refresh(proposal)
    return proposal, None


def execute_approved_refund(db: Session, user_id: str, order_id: str, proposal_id: str):
    """Execute the *named* approved proposal, exactly once.

    ``proposal_id`` is required and matched exactly. The previous version
    filtered on ``(user_id, order_id, status='approved')`` and took the newest
    row, which left the caller unable to prove which approval it consumed: if
    one order ever carried two approved rows, "the proposal a human approved"
    and "the proposal this call consumed" would be two different rows and
    nothing in the trail would say so. Naming the row turns that identity from
    an assumption into a check.

    The exactly-once guard is unchanged and still a conditional UPDATE on
    ``proposal_id + status='approved'``: only the caller that wins the update
    may move the order and write the audit row.
    """
    proposal = (
        db.query(RefundProposal)
        .filter(
            RefundProposal.proposal_id == proposal_id,
            RefundProposal.user_id == user_id,
            RefundProposal.order_id == order_id,
            RefundProposal.status == 'approved',
        )
        .first()
    )
    if not proposal:
        return None, 'approval_required'

    order = get_owned_order(db, user_id, order_id)
    if not order:
        return None, 'order_not_found'
    if not order.refund_available:
        return None, 'refund_already_processed'

    # Conditional state transition prevents a second execution from consuming the same approval.
    updated = (
        db.query(RefundProposal)
        .filter(RefundProposal.proposal_id == proposal.proposal_id, RefundProposal.status == 'approved')
        .update({'status': 'executed'}, synchronize_session=False)
    )
    if updated != 1:
        db.rollback()
        return None, 'approval_already_consumed'

    order.status = 'refund_processing'
    order.refund_available = False
    audit(db, 'refund_executed_after_human_review', user_id, proposal.proposal_id, {'order_id': order_id})
    db.commit()
    db.expire_all()
    return db.get(RefundProposal, proposal.proposal_id), None
