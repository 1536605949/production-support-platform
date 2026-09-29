import os

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('TOOL_API_KEY', 'test-tool-key')
os.environ.setdefault('REVIEWER_API_KEY', 'test-reviewer-key')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from business_service.app.db import Base
from business_service.app.service import (
    create_refund_proposal,
    execute_approved_refund,
    get_owned_order,
    review_refund_proposal,
    seed,
)


def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    seed(db)
    return db


def test_resource_ownership():
    db = session()
    assert get_owned_order(db, 'user001', 'ORD-1001') is not None
    assert get_owned_order(db, 'user001', 'ORD-2001') is None


def test_rejected_refund_cannot_execute():
    db = session()
    proposal, err = create_refund_proposal(db, 'user001', 'ORD-1001', 'changed mind')
    assert not err and proposal.status == 'pending_review'

    reviewed, err = review_refund_proposal(
        db, 'user001', 'ORD-1001', 'reject', 'reviewer-01', 'not eligible'
    )
    assert not err and reviewed.status == 'rejected'

    executed, err = execute_approved_refund(
        db, 'user001', 'ORD-1001', reviewed.proposal_id
    )
    assert executed is None and err == 'approval_required'


def test_approval_is_required_and_consumed_once():
    db = session()
    proposal, err = create_refund_proposal(db, 'user001', 'ORD-1001', 'changed mind')
    assert not err and proposal.status == 'pending_review'

    executed, err = execute_approved_refund(
        db, 'user001', 'ORD-1001', proposal.proposal_id
    )
    assert executed is None and err == 'approval_required'

    reviewed, err = review_refund_proposal(
        db, 'user001', 'ORD-1001', 'approve', 'reviewer-01', 'approved by human'
    )
    assert not err and reviewed.status == 'approved'

    executed, err = execute_approved_refund(
        db, 'user001', 'ORD-1001', reviewed.proposal_id
    )
    assert not err and executed.status == 'executed'

    replay, err = execute_approved_refund(
        db, 'user001', 'ORD-1001', reviewed.proposal_id
    )
    assert replay is None and err == 'approval_required'


def test_execute_must_name_the_approved_proposal():
    """An unnamed or wrong ``proposal_id`` is refused, not resolved by guessing.

    Before v2.6 the service looked up the newest approved row for the order, so
    a caller could consume an approval it never named — and a caller naming the
    wrong one still succeeded. Both directions are wrong in the same way: the
    call and the human decision were not provably the same object.
    """
    db = session()
    proposal, err = create_refund_proposal(db, 'user001', 'ORD-1001', 'changed mind')
    assert not err
    reviewed, err = review_refund_proposal(
        db, 'user001', 'ORD-1001', 'approve', 'reviewer-01', 'approved by human'
    )
    assert not err and reviewed.status == 'approved'

    # A syntactically fine id that names nothing.
    executed, err = execute_approved_refund(db, 'user001', 'ORD-1001', 'RP-DOESNOTEXIST')
    assert executed is None and err == 'approval_required'

    # The right proposal, but attributed to another order: no match either.
    executed, err = execute_approved_refund(db, 'user001', 'ORD-2001', reviewed.proposal_id)
    assert executed is None and err == 'approval_required'

    # The right proposal, but attributed to another user: no match either.
    executed, err = execute_approved_refund(db, 'user002', 'ORD-1001', reviewed.proposal_id)
    assert executed is None and err == 'approval_required'

    # Naming it correctly still works — the refusals above were about the name,
    # not about the approval having been spoilt by the failed attempts.
    executed, err = execute_approved_refund(db, 'user001', 'ORD-1001', reviewed.proposal_id)
    assert not err and executed.status == 'executed'


def test_open_proposal_is_reused():
    db = session()
    first, err = create_refund_proposal(db, 'user001', 'ORD-1001', 'reason one')
    assert not err
    second, err = create_refund_proposal(db, 'user001', 'ORD-1001', 'reason two')
    assert not err
    assert second.proposal_id == first.proposal_id
