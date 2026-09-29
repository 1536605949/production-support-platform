# -*- coding: utf-8 -*-
"""Product catalogue and structured after-sale rules.

Two claims are being guarded here, and they are different claims:

1. **Stock and price are read, not guessed.** ``get_sku`` answers from the
   ``sku`` table, so "还有货吗" has one answer and it is a lookup.
2. **Return eligibility is decided by a rule row, not by the model.** The
   important half of that is the *negative* case: the check must be able to
   refuse. A "rule engine" that always says yes is decoration, and one that
   refuses when it merely cannot tell is worse than none — it denies returns the
   shop owes. So the three-valued verdict (``True`` / ``False`` / ``None``) is
   tested from both sides.
"""
import os

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('TOOL_API_KEY', 'test-tool-key')
os.environ.setdefault('REVIEWER_API_KEY', 'test-reviewer-key')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from business_service.app.db import Base
from business_service.app.models import Order
from business_service.app.service import (
    check_aftersale_eligibility,
    get_sku,
    seed,
)


def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(db)
    return db


# --------------------------------------------------------------------------- #
# Catalogue: stock and price are facts, not inferences
# --------------------------------------------------------------------------- #
def test_stock_comes_from_the_table():
    db = session()
    sku = get_sku(db, 'SKU-2001')
    assert sku is not None
    assert sku.stock == 0
    assert sku.price_cents == 15900


def test_unknown_sku_is_none_not_an_empty_product():
    """A missing SKU must be distinguishable from a real one with no stock."""
    assert get_sku(session(), 'SKU-9999') is None


def test_every_seeded_sku_has_a_rule_for_its_category():
    """A category with no rule makes the verdict ``None``, which silently
    disables the check for that whole product class. Seeding a catalogue whose
    categories have no policy is a configuration bug, so it is asserted here."""
    from business_service.app.models import AftersaleRule

    db = session()
    covered = {r.category for r in db.query(AftersaleRule).all()}
    for sku_id in ('SKU-1001', 'SKU-2001', 'SKU-3001', 'SKU-4001', 'SKU-5001'):
        assert get_sku(db, sku_id).category in covered, sku_id


# --------------------------------------------------------------------------- #
# Eligibility: the rule table decides
# --------------------------------------------------------------------------- #
def test_within_the_window_is_eligible():
    verdict, err = check_aftersale_eligibility(session(), 'user001', 'ORD-1001')
    assert not err
    assert verdict['eligible'] is True
    assert verdict['rule_id'] == 'AR-apparel'
    assert verdict['deadline']


def test_a_customised_item_is_refused_by_category():
    """The refusal must quote the policy, not a generic "no"."""
    verdict, err = check_aftersale_eligibility(session(), 'user001', 'ORD-1002')
    assert not err
    assert verdict['eligible'] is False
    assert verdict['category'] == 'custom'
    assert '定制类' in verdict['reason']


def test_an_expired_window_is_refused_by_time():
    verdict, err = check_aftersale_eligibility(session(), 'user001', 'ORD-1003')
    assert not err
    assert verdict['eligible'] is False
    assert verdict['category'] == 'digital'
    assert '7 天' in verdict['reason']


def test_unknown_signing_time_is_not_a_refusal():
    """The load-bearing case.

    ``ORD-2001`` is an apparel item inside a valid policy, but nothing recorded
    when it was signed for. The window is measured from that instant, so the
    honest answer is "cannot tell" — and ``None`` must stay distinct from
    ``False``. Collapsing them would refuse a return the shop owes, and the
    refusal would look like strictness rather than a bug.
    """
    verdict, err = check_aftersale_eligibility(session(), 'user002', 'ORD-2001')
    assert not err
    assert verdict['eligible'] is None
    assert verdict['reason'] == 'signing_time_unknown'
    assert verdict['eligible'] is not False


def test_an_order_without_a_sku_is_not_a_refusal():
    """Same reasoning, one step earlier: no SKU means no category, no category
    means no rule, and no rule is not a "no"."""
    db = session()
    db.add(Order(order_id='ORD-9001', user_id='user001', status='delivered',
                 amount_cents=1000, refund_available=True))
    db.commit()

    verdict, err = check_aftersale_eligibility(db, 'user001', 'ORD-9001')
    assert not err
    assert verdict['eligible'] is None
    assert verdict['reason'] == 'order_has_no_sku'


def test_an_unknown_category_is_not_a_refusal():
    db = session()
    db.add(Order(order_id='ORD-9002', user_id='user001', status='delivered',
                 amount_cents=1000, refund_available=True, sku_id='SKU-9999'))
    db.commit()

    verdict, err = check_aftersale_eligibility(db, 'user001', 'ORD-9002')
    assert not err
    assert verdict['eligible'] is None
    assert verdict['reason'] == 'sku_not_found'


def test_the_rule_check_enforces_ownership():
    """The rule check reads the order, so it must not become a way to read
    somebody else's order by asking about its return policy."""
    verdict, err = check_aftersale_eligibility(session(), 'user001', 'ORD-2001')
    assert verdict is None
    assert err == 'order_not_found'


# --------------------------------------------------------------------------- #
# Routing: a SKU question reaches the catalogue, and nothing else is stolen
# --------------------------------------------------------------------------- #
def test_a_sku_id_is_extracted_and_normalised():
    from gateway.app.core import intent as intent_mod

    for text in ('SKU-1001 还有货吗', 'sku 1001 有货吗', 'SKU1001 多少钱'):
        assert intent_mod.extract_slots(text).get('sku_id') == 'SKU-1001', text


def test_naming_a_sku_routes_to_product():
    from gateway.app.core import intent as intent_mod

    assert intent_mod.detect_intent('SKU-1001 还有货吗').intent == intent_mod.Intent.PRODUCT.value
    assert intent_mod.detect_intent('这个商品的规格是什么').intent == intent_mod.Intent.PRODUCT.value


def test_product_rules_do_not_steal_refund_or_order_messages():
    """Regression guard for the new keyword table.

    Product keywords overlap the wording of other intents ("商品" appears in
    refund complaints, "多少钱" in a refund question about the amount), so the
    weights have to leave the dominant signal in charge.
    """
    from gateway.app.core import intent as intent_mod

    cases = {
        '我的订单 ORD-1001 想申请退款': intent_mod.Intent.REFUND.value,
        '商品有质量问题，我要退款': intent_mod.Intent.REFUND.value,
        '订单 ORD-1001 到哪了': intent_mod.Intent.ORDER_QUERY.value,
        '这个订单什么时候发货': intent_mod.Intent.ORDER_QUERY.value,
        '退款能退多少钱': intent_mod.Intent.REFUND.value,
        '转人工': intent_mod.Intent.HUMAN_HANDOFF.value,
    }
    for text, expected in cases.items():
        assert intent_mod.detect_intent(text).intent == expected, text


def test_a_product_question_plans_a_catalogue_lookup():
    from gateway.app.core import engine

    state = {'intent': 'product', 'slots': {'sku_id': 'SKU-1001'}, 'user_id': 'user001'}
    # ``plan_tools`` returns a Plan (a typed graph); the tool/argument pairs are
    # what this test is about. The graph's own shape is covered by
    # tests/test_plan_verification.py.
    plan = engine.plan_tools(state, 'SKU-1001 还有货吗')
    assert [(s.tool, dict(s.arguments)) for s in plan.steps] == [
        ('get_sku', {'sku_id': 'SKU-1001'})
    ]


def test_the_rule_check_is_registered_and_read_only():
    from gateway.app.core import governance

    spec = governance.TOOL_REGISTRY['check_aftersale_eligibility']
    assert spec.risk == 'low'
    assert spec.requires_approval is False
    assert spec.method == 'POST'
    assert spec.path == '/aftersale/eligibility'

    sku = governance.TOOL_REGISTRY['get_sku']
    assert sku.risk == 'low'
    assert sku.method == 'GET'
    assert sku.path == '/skus/{sku_id}'


def test_the_catalogue_lookup_sends_the_sku_in_the_path_not_the_query():
    """``{sku_id}`` fills the path; anything else would silently become a query
    parameter and the endpoint would 404."""
    from gateway.app.core import governance

    spec = governance.TOOL_REGISTRY['get_sku']
    args = {'sku_id': 'SKU-1001'}
    path_args = {k: v for k, v in args.items() if '{' + k + '}' in spec.path}
    assert path_args == {'sku_id': 'SKU-1001'}


# --------------------------------------------------------------------------- #
# The gate, end to end over the chat API
# --------------------------------------------------------------------------- #
def _stub_business(monkeypatch, verdict):
    """Replace the HTTP hop with a fixed rule verdict."""
    from gateway.app import deps

    calls: list[str] = []

    async def fake_call(spec, arguments, trace_id=''):
        calls.append(spec.name)
        if spec.name == 'check_aftersale_eligibility':
            return {'status': 'ok', **verdict}
        return {'status': 'ok', 'proposal_id': 'RP-TEST', 'order_id': arguments.get('order_id')}

    monkeypatch.setattr(deps.business_client, 'call', fake_call)
    return calls


def test_an_ineligible_refund_is_refused_and_never_reaches_approval(client, dev_headers, monkeypatch):
    calls = _stub_business(monkeypatch, {
        'eligible': False,
        'reason': '定制类商品不支持七天无理由退货。',
        'rule_id': 'AR-custom',
    })

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': '确认退款 ORD-1001 现在就退'}).json()

    assert body['tool']['status'] == 'blocked'
    assert body['approval_id'] == ''
    # The write never happened, so no approval was created and no refund parked.
    assert 'execute_refund' not in calls
    assert 'create_refund_proposal' not in calls
    assert '定制类' in body['answer']


def test_an_eligible_refund_still_reaches_approval(client, dev_headers, monkeypatch):
    calls = _stub_business(monkeypatch, {'eligible': True, 'reason': '七天无理由退货。'})

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': '确认退款 ORD-1001 现在就退'}).json()

    assert body['tool']['status'] == 'waiting_approval'
    assert body['approval_id'].startswith('AP-')
    # ``execute_refund`` is high risk, so it parks for approval instead of
    # calling the order system — the business hop happens on resume, after a
    # human decides. The upstream calls in this turn are the rule probe and the
    # proposal it feeds; the proposal is idempotent, so re-confirming an order
    # that was already proposed returns the existing row.
    assert calls == ['check_aftersale_eligibility', 'create_refund_proposal']


def test_an_unknown_verdict_does_not_block_the_refund(client, dev_headers, monkeypatch):
    """``eligible=None`` must fall through to the normal path.

    This is the same distinction the service layer makes, asserted at the gate:
    an undecidable rule is not a refusal.
    """
    calls = _stub_business(monkeypatch, {'eligible': None, 'reason': 'signing_time_unknown'})

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': '确认退款 ORD-1001 现在就退'}).json()

    assert body['tool']['status'] == 'waiting_approval'
    assert body['approval_id'].startswith('AP-')
    assert calls == ['check_aftersale_eligibility', 'create_refund_proposal']


def test_a_broken_rule_service_does_not_refuse_the_refund(client, dev_headers, monkeypatch):
    """An unreachable rule service must not be read as a refusal either."""
    from gateway.app import deps
    from gateway.app.core.governance import ToolDenied

    async def fake_call(spec, arguments, trace_id=''):
        if spec.name == 'check_aftersale_eligibility':
            raise ToolDenied('upstream_error', '503: unavailable')
        # The real service always returns the proposal it created or reused;
        # a stub that omits ``proposal_id`` would make the execute step skip for
        # a reason that cannot happen in production, and the test would then be
        # asserting the stub rather than the product.
        return {'status': 'ok', 'proposal_id': 'RP-TEST',
                'order_id': arguments.get('order_id')}

    monkeypatch.setattr(deps.business_client, 'call', fake_call)

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': '确认退款 ORD-1001 现在就退'}).json()

    assert body['tool']['status'] == 'waiting_approval'


def test_a_product_question_is_answered_from_the_catalogue(client, dev_headers, monkeypatch):
    from gateway.app import deps

    async def fake_call(spec, arguments, trace_id=''):
        assert spec.name == 'get_sku'
        return {
            'status': 'ok', 'sku_id': 'SKU-2001', 'name': '定制刻字保温杯',
            'spec': '350ml / 激光刻字', 'category': 'custom', 'stock': 0,
            'price': 159.0, 'in_stock': False,
        }

    monkeypatch.setattr(deps.business_client, 'call', fake_call)

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': 'SKU-2001 还有货吗？'}).json()

    assert body['intent'] == 'product'
    assert body['tool']['tool'] == 'get_sku'
    assert body['tool']['result']['stock'] == 0
    assert '无货' in body['answer']


# --------------------------------------------------------------------------- #
# Regression: the deterministic answer's money rendering
# --------------------------------------------------------------------------- #
def test_the_order_answer_renders_the_amount_in_yuan():
    """``get_order`` returns ``amount`` in yuan, not ``amount_cents``.

    Reading the wrong key did not raise — ``int(None)`` was swallowed by the
    formatter's ``except`` — so the answer read "金额 None". It only ever showed
    on the path taken when the model is unavailable or has just failed, which is
    exactly the path that must not be wrong.
    """
    from gateway.app.core.engine import deterministic_answer

    answer = deterministic_answer({
        'intent': 'order_query',
        'tool': {'tool': 'get_order', 'status': 'ok', 'result': {
            'order_id': 'ORD-1001', 'status': 'shipped',
            'amount': 199.0, 'refund_available': True}},
    })
    assert '¥199.00' in answer
    assert 'None' not in answer


def test_a_missing_amount_reads_as_a_dash_not_as_none():
    from gateway.app.core.engine import deterministic_answer

    answer = deterministic_answer({
        'intent': 'order_query',
        'tool': {'tool': 'get_order', 'status': 'ok', 'result': {
            'order_id': 'ORD-1001', 'status': 'shipped', 'refund_available': True}},
    })
    assert 'None' not in answer
    assert '—' in answer


def test_the_sku_answer_renders_stock_and_price():
    from gateway.app.core.engine import deterministic_answer

    in_stock = deterministic_answer({
        'intent': 'product',
        'tool': {'tool': 'get_sku', 'status': 'ok', 'result': {
            'name': '纯棉基础款圆领T恤', 'spec': 'M / 白色',
            'stock': 128, 'price': 99.0, 'in_stock': True}},
    })
    assert '128' in in_stock
    assert '¥99.00' in in_stock

    out_of_stock = deterministic_answer({
        'intent': 'product',
        'tool': {'tool': 'get_sku', 'status': 'ok', 'result': {
            'name': '定制刻字保温杯', 'spec': '350ml', 'stock': 0,
            'price': 159.0, 'in_stock': False}},
    })
    assert '无货' in out_of_stock
