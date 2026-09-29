# -*- coding: utf-8 -*-
"""Deterministic routing: scoring, slots, context smoothing, escalation ladder."""
from gateway.app.core import intent as intent_mod


def test_keyword_scoring_picks_the_dominant_intent():
    result = intent_mod.detect_intent('我的订单 ORD-1001 想申请退款')
    assert result.intent == intent_mod.Intent.REFUND.value
    assert result.confidence > 0.5
    assert '退款' in result.matched


def test_order_id_is_extracted_and_normalised():
    for text in ('订单号 1001 到哪了', 'ord-1001 物流', 'ORD 1001 发货了吗'):
        slots = intent_mod.extract_slots(text)
        assert slots.get('order_id') == 'ORD-1001', text


def test_explicit_handoff_wins_over_everything():
    result = intent_mod.detect_intent('订单有问题，我要转人工客服')
    assert result.intent == intent_mod.Intent.HUMAN_HANDOFF.value
    assert result.confidence > 0.9


def test_short_follow_up_inherits_the_previous_topic():
    # No business keyword of its own: the topic can only come from context.
    result = intent_mod.detect_intent('那要多久呢', previous_intent='refund')
    assert result.intent == intent_mod.Intent.REFUND.value
    assert result.confidence < 0.55  # inherited, not freshly confident


def test_context_smoothing_boosts_the_previous_intent():
    plain = intent_mod.detect_intent('退款多久到账')
    boosted = intent_mod.detect_intent('退款多久到账', previous_intent='refund')
    assert boosted.scores['refund'] >= plain.scores['refund']


def test_greeting_is_smalltalk_not_a_business_intent():
    result = intent_mod.detect_intent('你好')
    assert result.smalltalk is True
    assert result.intent == intent_mod.Intent.OTHER.value


def test_gibberish_has_low_confidence():
    result = intent_mod.detect_intent('zzzz qqqq xxxx')
    assert result.confidence < 0.35
    assert result.intent == intent_mod.Intent.OTHER.value


# --------------------------------------------------------------------------- #
# Escalation ladder
# --------------------------------------------------------------------------- #
def test_confident_message_never_calls_the_llm():
    decision = intent_mod.route('我要申请退款 ORD-1001', llm_available=True)
    assert decision.source == 'deterministic'
    assert decision.need_llm is False
    assert decision.handoff_level == 0


def test_low_confidence_routes_to_the_llm_when_available():
    decision = intent_mod.route('zzzz qqqq', llm_available=True)
    assert decision.need_llm is True
    assert decision.source == 'llm'


def test_low_confidence_without_llm_escalates_to_a_human():
    decision = intent_mod.route('zzzz qqqq xxxx yyyy', llm_available=False)
    assert decision.need_llm is False
    assert decision.source == 'fallback'
    assert decision.handoff_level == 2


def test_repeated_low_confidence_escalates_even_without_a_model():
    decision = intent_mod.route(
        '那个东西怎么样', llm_available=False, low_confidence_streak=2,
        handoff_streak_limit=3,
    )
    assert decision.handoff_level == 2


def test_explicit_request_is_level_three():
    decision = intent_mod.route('转人工', llm_available=True)
    assert decision.handoff_level == 3
    assert decision.source == 'handoff'


def test_frustration_language_suggests_handoff():
    decision = intent_mod.route('退款说了多少遍还是不行，太差了', llm_available=False)
    assert decision.result.frustration > 0.9
    assert decision.handoff_level >= 1
