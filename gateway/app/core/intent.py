# -*- coding: utf-8 -*-
"""Deterministic intent routing.

The trunk never starts with an LLM. It starts with a weighted keyword scorer
that is cheap, explainable and reproducible:

1. **Weighted phrase rules** produce a score per intent. The weight encodes how
   strong the signal is (``申请退款`` is a much stronger refund signal than a
   bare ``退``).
2. **Slot extraction** pulls order / ticket / proposal identifiers and amounts
   out of the message, including identifiers carried over from earlier turns.
3. **Contextual smoothing** boosts the previous topic when the current message
   is short and ambiguous (``那能退多少？`` after a refund question).
4. **Routing decision** — only when the deterministic score is *not* confident
   enough does the trunk escalate to the LLM (see ``core/engine.py``). Below a
   second, lower threshold even the LLM is not trusted, and the turn is routed
   to a human.

The module is pure: no DB, no network, no FastAPI. That makes the routing
behaviour fully unit-testable, and means the service still routes sensibly when
the model provider is down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Intent(str, Enum):
    REFUND = 'refund'
    ORDER_QUERY = 'order_query'
    PRODUCT = 'product'
    TECH_SUPPORT = 'tech_support'
    HUMAN_HANDOFF = 'human_handoff'
    FAQ = 'faq'
    OTHER = 'other'


INTENT_LABELS: dict[str, str] = {
    Intent.REFUND.value: '退款',
    Intent.ORDER_QUERY.value: '订单查询',
    Intent.PRODUCT.value: '商品咨询',
    Intent.TECH_SUPPORT.value: '技术支持',
    Intent.HUMAN_HANDOFF.value: '转人工',
    Intent.FAQ.value: '常见问题',
    Intent.OTHER.value: '其他',
}

# Intents whose tools are write operations and therefore risk-graded.
INTENT_TOOL_HINTS: dict[str, list[str]] = {
    Intent.REFUND.value: ['get_order', 'create_refund_proposal'],
    Intent.ORDER_QUERY.value: ['get_order'],
    Intent.PRODUCT.value: ['get_sku'],
    Intent.TECH_SUPPORT.value: ['create_ticket'],
    Intent.FAQ.value: [],
    Intent.OTHER.value: [],
    Intent.HUMAN_HANDOFF.value: [],
}

INTENT_SUGGESTIONS: dict[str, list[str]] = {
    Intent.REFUND.value: ['我的订单 ORD-1001 想申请退款', '退款多久到账？', '退款被审核拒绝了怎么办？'],
    Intent.ORDER_QUERY.value: ['帮我查一下订单 ORD-1001 的物流', '我的订单什么时候发货？', '怎么修改收货地址？'],
    Intent.PRODUCT.value: ['SKU-1001 还有货吗？', 'SKU-2001 多少钱？', '这款的规格是什么？'],
    Intent.TECH_SUPPORT.value: ['App 打不开一直闪退', '收不到验证码怎么办？', '登录提示密码错误'],
    Intent.HUMAN_HANDOFF.value: ['我想转人工客服', '帮我转接人工'],
    Intent.FAQ.value: ['怎么开发票？', '优惠券怎么使用？', '支持哪些支付方式？'],
    Intent.OTHER.value: ['我想申请退款', '帮我查订单物流', 'App 登录不了'],
}


# --------------------------------------------------------------------------- #
# Rule table: phrase -> weight
# --------------------------------------------------------------------------- #
RULES: dict[str, dict[str, float]] = {
    Intent.REFUND.value: {
        '申请退款': 3.0, '我要退款': 3.0, '退款': 2.4, '退货': 2.2, '退钱': 2.6, '退单': 2.0,
        '退运费': 2.0, '退款进度': 2.6, '退款到账': 2.6, '退款审核': 2.6, '取消订单': 1.8,
        '不想要了': 1.8, '买错了': 1.6, '差价': 1.4, '补偿': 1.4, '赔付': 1.4, '退一下': 1.8,
        '退掉': 1.8, '想退': 1.8, '能退': 1.6, '退货退款': 2.6, '仅退款': 2.6, '退款申请': 2.6,
        'refund': 2.4,
    },
    Intent.ORDER_QUERY.value: {
        '订单': 2.0, '订单号': 2.2, '物流': 2.4, '快递': 2.2, '运单': 2.2, '发货': 2.2,
        '什么时候到': 2.2, '到哪了': 2.2, '派送': 2.0, '签收': 1.8, '收货': 1.6, '查订单': 2.6,
        '我的订单': 2.4, '订单状态': 2.6, '配送': 1.8, '修改地址': 2.0, '收货地址': 1.8,
        '下单': 1.2, '付款': 1.0, '单号': 1.8, '多久到': 2.0, 'order': 1.6, 'tracking': 2.0,
    },
    Intent.PRODUCT.value: {
        '库存': 2.6, '有货': 2.6, '还有货': 2.8, '有现货': 2.6, '现货': 2.4, '缺货': 2.6,
        '无货': 2.6, '多少钱': 2.4, '价格': 2.0, '售价': 2.2, '价位': 2.0, '贵不贵': 2.0,
        '规格': 2.0, '型号': 2.0, '尺码': 2.0, '尺寸': 1.8, '材质': 1.8, '颜色': 1.6,
        'sku': 2.6, '商品': 1.6, '这款': 1.8, '这个商品': 2.0, '货号': 2.2,
    },
    Intent.TECH_SUPPORT.value: {
        '报错': 2.6, '错误': 2.0, '打不开': 2.6, '登录不了': 2.8, '登录不上': 2.8, '登不上去': 2.6,
        '无法登录': 2.8, '闪退': 2.8, '崩溃': 2.6, 'bug': 2.4, '故障': 2.4, '白屏': 2.6,
        '卡顿': 2.2, '很卡': 2.2, '支付失败': 2.6, '收不到验证码': 2.8, '验证码': 1.6,
        '密码错误': 2.4, '忘记密码': 2.4, '重置密码': 2.4, '安装失败': 2.6, '更新失败': 2.6,
        '不兼容': 2.2, '闪屏': 2.2, '没反应': 2.2, '怎么设置': 1.8, '不会用': 1.8, '无法使用': 2.4,
        '用不了': 2.4, '打不开页面': 2.6, '一直转圈': 2.4, '加载失败': 2.4,
    },
    Intent.HUMAN_HANDOFF.value: {
        '转人工': 3.4, '人工客服': 3.4, '转接人工': 3.4, '找客服': 3.0, '真人': 3.0,
        '人工服务': 3.2, '客服电话': 2.6, '投诉': 3.0, '我要投诉': 3.2, '举报': 2.4,
        '经理': 2.2, '人工': 2.4, '转一下人工': 3.2, '叫人工': 3.0, 'human': 2.6, 'agent': 1.8,
    },
    Intent.FAQ.value: {
        '发票': 2.2, '开票': 2.2, '优惠券': 2.2, '优惠': 1.6, '会员': 1.8, '积分': 1.8,
        '配送范围': 2.0, '运费': 1.8, '包邮': 1.8, '支付方式': 2.0, '售后': 1.8, '保修': 2.0,
        '质保': 2.0, '七天无理由': 2.4, '发票怎么开': 2.6, '政策': 1.8, '怎么用': 1.6,
        '规则': 1.6, '活动': 1.4, '账号': 1.4, '实名': 1.8, '注销': 1.8,
    },
}

_SMALL_TALK = {
    '你好': 1.0, '您好': 1.0, '在吗': 0.8, '在么': 0.8, 'hi': 0.8, 'hello': 0.8,
    '谢谢': 0.8, '多谢': 0.8, '好的': 0.6, '嗯': 0.6, '哦': 0.6, '拜拜': 0.8,
}

# Phrases that mean "this is going badly" — they push the conversation toward a
# human even when the topic itself was classified confidently.
_FRUSTRATION = {
    '说了多少遍': 1.0, '还是不行': 0.9, '没用': 0.8, '根本': 0.6, '气死': 1.0,
    '垃圾': 0.9, '差评': 1.0, '太差': 0.9, '无语': 0.8, '什么意思': 0.6,
}

_ORDER_ID_PATTERNS = (
    re.compile(r'\bORD[\s\-_]?(\d{3,8})\b', re.IGNORECASE),
    re.compile(
        r'(?:订单(?:号|编号|id)?|order\s*(?:id|no|number)?)\s*[:：]?\s*([A-Za-z]{0,4}[\s\-_]?\d{4,})',
        re.IGNORECASE,
    ),
)
_TICKET_ID_RE = re.compile(r'\bT[\s\-_]?[0-9A-F]{6,}\b', re.IGNORECASE)
_PROPOSAL_ID_RE = re.compile(r'\bRP[\s\-_]?[0-9A-F]{6,}\b', re.IGNORECASE)
# Only the explicit ``SKU`` form. A bare "商品编号 1001" is indistinguishable
# from an order number, and guessing wrong would send a stock question to the
# order service — a wrong answer rather than no answer.
_SKU_ID_RE = re.compile(r'\bSKU[\s\-_]?(\d{3,8})\b', re.IGNORECASE)
_AMOUNT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:元|块|rmb|人民币)', re.IGNORECASE)


def normalize_order_id(raw: str) -> str:
    """Turn ``ord 1001`` / ``ORD-1001`` / ``订单号 1001`` into ``ORD-1001``."""
    digits = re.sub(r'\D', '', raw)
    return f'ORD-{digits}' if digits else raw.strip().upper()


def normalize_sku_id(raw: str) -> str:
    """Turn ``sku 1001`` / ``SKU-1001`` into ``SKU-1001``."""
    digits = re.sub(r'\D', '', raw)
    return f'SKU-{digits}' if digits else raw.strip().upper()


@dataclass
class IntentResult:
    intent: str
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    matched: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    smalltalk: bool = False
    frustration: float = 0.0

    @property
    def label(self) -> str:
        return INTENT_LABELS.get(self.intent, self.intent)

    @property
    def is_handoff(self) -> bool:
        return self.intent == Intent.HUMAN_HANDOFF.value


@dataclass
class RoutingDecision:
    """Outcome of the routing stage, before any retrieval or model call."""

    intent: str
    confidence: float
    source: str          # deterministic | llm | fallback | handoff
    need_llm: bool
    handoff_level: int   # 0 = none, 1 = suggest, 2 = auto, 3 = explicit
    result: IntentResult

    @property
    def handoff(self) -> bool:
        return self.handoff_level > 0


def extract_slots(text: str) -> dict[str, Any]:
    slots: dict[str, Any] = {}

    for pattern in _ORDER_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            slots['order_id'] = normalize_order_id(match.group(1))
            break

    ticket = _TICKET_ID_RE.search(text)
    if ticket:
        slots['ticket_id'] = ticket.group(0).upper().replace(' ', '-')

    sku = _SKU_ID_RE.search(text)
    if sku:
        slots['sku_id'] = normalize_sku_id(sku.group(1))

    proposal = _PROPOSAL_ID_RE.search(text)
    if proposal:
        slots['proposal_id'] = proposal.group(0).upper().replace(' ', '-')

    amount = _AMOUNT_RE.search(text)
    if amount:
        try:
            slots['amount'] = float(amount.group(1))
        except ValueError:
            pass

    return slots


def _score(text: str) -> tuple[dict[str, float], list[str]]:
    lowered = text.lower()
    scores: dict[str, float] = {}
    matched: list[str] = []

    for intent, phrases in RULES.items():
        total = 0.0
        for phrase, weight in phrases.items():
            if phrase in lowered:
                total += weight
                matched.append(phrase)
        if total:
            scores[intent] = round(total, 3)

    for phrase, weight in _SMALL_TALK.items():
        if phrase in lowered:
            scores[Intent.OTHER.value] = round(scores.get(Intent.OTHER.value, 0.0) + weight, 3)

    return scores, matched


def _frustration(text: str) -> float:
    lowered = text.lower()
    total = sum(w for p, w in _FRUSTRATION.items() if p in lowered)
    # Repeated punctuation / exclamation is a weak but real signal.
    total += min(0.6, 0.2 * (text.count('!') + text.count('！')))
    return round(min(1.5, total), 3)


def detect_intent(
    text: str,
    *,
    previous_intent: str = '',
    context_slots: dict[str, Any] | None = None,
    slot_hints: dict[str, Any] | None = None,
) -> IntentResult:
    """Classify ``text`` into one of the supported intents (deterministic)."""
    text = (text or '').strip()
    if not text:
        return IntentResult(Intent.OTHER.value, 0.0, slots=dict(context_slots or {}))

    scores, matched = _score(text)
    slots = dict(context_slots or {})
    slots.update(extract_slots(text))
    if slot_hints:
        slots.update({k: v for k, v in slot_hints.items() if v})

    # Naming a SKU is itself a strong product signal — the user is pointing at
    # one specific item, whatever else the sentence says. It is *added* to the
    # score rather than forced, so an explicit refund request in the same
    # message still outranks it.
    if 'sku_id' in slots:
        scores[Intent.PRODUCT.value] = round(scores.get(Intent.PRODUCT.value, 0.0) + 1.5, 3)

    frustration = _frustration(text)

    # An explicit request for a human wins outright.
    if Intent.HUMAN_HANDOFF.value in scores and scores[Intent.HUMAN_HANDOFF.value] >= 2.4:
        return IntentResult(
            Intent.HUMAN_HANDOFF.value, confidence=0.98, scores=scores,
            matched=matched, slots=slots, frustration=frustration,
        )

    if not scores:
        # No business keyword at all. A short follow-up may still belong to the
        # previous topic; otherwise it is small talk.
        if previous_intent and previous_intent != Intent.OTHER.value and len(text) <= 12:
            return IntentResult(previous_intent, confidence=0.42, slots=slots,
                                frustration=frustration)
        return IntentResult(Intent.OTHER.value, confidence=0.15, slots=slots,
                            frustration=frustration)

    if set(scores) == {Intent.OTHER.value}:
        return IntentResult(
            Intent.OTHER.value, confidence=0.5, scores=scores, matched=matched,
            slots=slots, smalltalk=True, frustration=frustration,
        )

    # Contextual smoothing for short follow-ups.
    if previous_intent and previous_intent in scores and len(text) <= 20:
        scores[previous_intent] = round(scores[previous_intent] * 1.25, 3)

    top_intent, top_score = max(scores.items(), key=lambda item: item[1])
    total = sum(scores.values())
    dominance = top_score / total if total else 0.0
    strength = min(1.0, top_score / 3.0)
    confidence = round(min(0.97, 0.35 + 0.5 * dominance * (0.4 + 0.6 * strength)), 3)

    if 'order_id' in slots and top_intent == Intent.OTHER.value:
        top_intent, confidence = Intent.ORDER_QUERY.value, 0.5

    return IntentResult(top_intent, confidence=confidence, scores=scores, matched=matched,
                        slots=slots, frustration=frustration)


def route(
    text: str,
    *,
    previous_intent: str = '',
    context_slots: dict[str, Any] | None = None,
    slot_hints: dict[str, Any] | None = None,
    llm_threshold: float = 0.55,
    handoff_threshold: float = 0.35,
    low_confidence_streak: int = 0,
    handoff_streak_limit: int = 3,
    llm_available: bool = False,
) -> RoutingDecision:
    """Turn a message into a routing decision.

    Escalation ladder (three levels of human handoff, as required by the
    target architecture):

    * **level 3 — explicit**: the user asked for a human (``转人工``).
    * **level 2 — auto**: the deterministic score is low, no LLM is available
      to rescue it, *or* the user has been stuck for ``handoff_streak_limit``
      turns in a row.
    * **level 1 — suggest**: the answer is uncertain but the trunk still has
      something useful to say; the reply offers a handoff instead of forcing it.
    """
    result = detect_intent(
        text,
        previous_intent=previous_intent,
        context_slots=context_slots,
        slot_hints=slot_hints,
    )

    if result.is_handoff:
        return RoutingDecision(result.intent, result.confidence, 'handoff', False, 3, result)

    # Frustration is evaluated *before* the confidence shortcut: a user who is
    # angry about a clearly-classified topic still deserves a human, and the
    # topic being unambiguous must not suppress that.
    frustration_handoff = 1 if result.frustration >= 0.9 else 0

    if result.confidence >= llm_threshold:
        return RoutingDecision(result.intent, result.confidence, 'deterministic',
                               False, frustration_handoff, result)

    # Not confident enough: this is exactly the node where the LLM is allowed in.
    if llm_available:
        return RoutingDecision(result.intent, result.confidence, 'llm', True,
                               frustration_handoff, result)

    # No model to fall back on. Do not bluff — route to a human.
    if result.confidence < handoff_threshold or low_confidence_streak + 1 >= handoff_streak_limit:
        return RoutingDecision(result.intent, result.confidence, 'fallback', False, 2, result)
    return RoutingDecision(result.intent, result.confidence, 'fallback', False,
                           max(1, frustration_handoff), result)
