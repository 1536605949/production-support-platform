from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session
from prometheus_client import make_asgi_app
from .config import SERVICE_VERSION, get_settings, validate_settings
from .db import Base, engine, get_db, SessionLocal
from .models import Account
from .schemas import (
    TicketCreate,
    RefundProposalCreate,
    RefundReview,
    RefundExecute,
    AftersaleEligibilityQuery,
)
from .security import require_tool_key, require_reviewer_key
from .service import (
    seed,
    get_owned_order,
    get_sku,
    check_aftersale_eligibility,
    create_ticket,
    create_refund_proposal,
    review_refund_proposal,
    execute_approved_refund,
)

settings = get_settings()
validate_settings(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Schema + seed on boot.

    Uses ``lifespan`` rather than the deprecated ``@app.on_event('startup')``,
    which Starlette warns about on every start and has slated for removal.
    """
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    yield


app = FastAPI(title='Enterprise Business Tool Service', version=SERVICE_VERSION,
              lifespan=lifespan)
app.mount('/metrics', make_asgi_app())


@app.get('/healthz')
def healthz(db: Session = Depends(get_db)):
    db.execute(text('SELECT 1'))
    return {'status': 'ok', 'version': SERVICE_VERSION}


@app.get('/orders/{order_id}', dependencies=[Depends(require_tool_key)])
def get_order(order_id: str, user_id: str = Query(...), db: Session = Depends(get_db)):
    order = get_owned_order(db, user_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail='order_not_found')
    return {
        'order_id': order.order_id,
        'user_id': order.user_id,
        'status': order.status,
        'amount': order.amount_cents / 100,
        'refund_available': order.refund_available,
    }


@app.get('/accounts/{user_id}', dependencies=[Depends(require_tool_key)])
def get_account(user_id: str, requester_user_id: str = Query(...), db: Session = Depends(get_db)):
    if requester_user_id != user_id:
        raise HTTPException(status_code=403, detail='forbidden')
    account = db.get(Account, user_id)
    if not account:
        raise HTTPException(status_code=404, detail='account_not_found')
    return {'user_id': account.user_id, 'level': account.level, 'status': account.status}


@app.get('/skus/{sku_id}', dependencies=[Depends(require_tool_key)])
def get_sku_detail(sku_id: str, db: Session = Depends(get_db)):
    """Product facts: stock and price. The only source the Agent may quote.

    No ownership check: a catalogue entry is not user-scoped. ``status`` is
    returned alongside ``stock`` because "0 in stock" and "delisted" are
    different answers to "还有货吗" and the caller should not have to guess.
    """
    sku = get_sku(db, sku_id)
    if not sku:
        raise HTTPException(status_code=404, detail='sku_not_found')
    return {
        'sku_id': sku.sku_id,
        'name': sku.name,
        'spec': sku.spec,
        'category': sku.category,
        'stock': sku.stock,
        'price': sku.price_cents / 100,
        'status': sku.status,
        'in_stock': sku.stock > 0 and sku.status == 'on_sale',
    }


@app.post('/aftersale/eligibility', dependencies=[Depends(require_tool_key)])
def post_aftersale_eligibility(
    req: AftersaleEligibilityQuery, db: Session = Depends(get_db)
):
    """Structured answer to "can this order be returned".

    ``eligible`` is three-valued on purpose — ``true`` / ``false`` / ``null``,
    where ``null`` means the inputs to decide are missing. See
    :func:`business_service.app.service.check_aftersale_eligibility` for why
    collapsing ``null`` into ``false`` would be a bug rather than a strictness.
    """
    verdict, err = check_aftersale_eligibility(db, req.user_id, req.order_id, req.rule_type)
    if err:
        raise HTTPException(status_code=404, detail=err)
    return verdict


@app.post('/tickets', dependencies=[Depends(require_tool_key)])
def post_ticket(req: TicketCreate, db: Session = Depends(get_db)):
    ticket = create_ticket(db, req.user_id, req.issue)
    return {'ticket_id': ticket.ticket_id, 'user_id': ticket.user_id, 'status': ticket.status}


@app.post('/refunds/proposals', dependencies=[Depends(require_tool_key)])
def post_refund_proposal(req: RefundProposalCreate, db: Session = Depends(get_db)):
    proposal, err = create_refund_proposal(db, req.user_id, req.order_id, req.reason)
    if err:
        raise HTTPException(status_code=400 if err == 'refund_not_available' else 404, detail=err)
    return {
        'proposal_id': proposal.proposal_id,
        'user_id': proposal.user_id,
        'order_id': proposal.order_id,
        'reason': proposal.reason,
        'status': proposal.status,
    }


@app.post('/refunds/review', dependencies=[Depends(require_reviewer_key)])
def post_refund_review(req: RefundReview, db: Session = Depends(get_db)):
    proposal, err = review_refund_proposal(
        db,
        req.user_id,
        req.order_id,
        req.decision,
        settings.reviewer_id,
        req.review_comment,
    )
    if err:
        raise HTTPException(status_code=404, detail=err)
    return {
        'proposal_id': proposal.proposal_id,
        'order_id': proposal.order_id,
        'status': proposal.status,
        'review_comment': proposal.review_comment,
        'reviewer_id': settings.reviewer_id,
    }


@app.post('/refunds/execute', dependencies=[Depends(require_tool_key)])
def post_execute_refund(req: RefundExecute, db: Session = Depends(get_db)):
    proposal, err = execute_approved_refund(db, req.user_id, req.order_id, req.proposal_id)
    if err:
        raise HTTPException(status_code=409, detail=err)
    return {
        'proposal_id': proposal.proposal_id,
        'order_id': proposal.order_id,
        'status': proposal.status,
        'message': 'refund accepted for processing',
    }
