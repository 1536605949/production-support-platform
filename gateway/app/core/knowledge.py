# -*- coding: utf-8 -*-
"""Knowledge base: hybrid retrieval with tenant-scoped permissions.

Two recall channels, fused with Reciprocal Rank Fusion:

* **BM25 keyword** — a compact in-process implementation with a
  Chinese-friendly tokenizer (ASCII words + CJK single characters + CJK
  bigrams). Always on, no external service, works on any database.
* **Vector (semantic)** — enabled only when an embedding endpoint is
  configured. Vectors are stored per document; on PostgreSQL the same column
  can be served by ``pgvector`` (see ``deploy/migrations``), on SQLite the
  cosine scan runs in Python. Small knowledge bases are fine either way; the
  interface does not change when the storage does.

Permission filtering happens **before** scoring, not after: a tenant can only
ever retrieve documents it is allowed to see, so a high-scoring private
document from another tenant cannot leak into the candidate set at all.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KnowledgeDocument
from .observability import log_event

logger = logging.getLogger('gateway.knowledge')

_CJK = r'\u4e00-\u9fff'
_ASCII_TOKEN_RE = re.compile(r'[a-z0-9]+')
_CJK_RUN_RE = re.compile(f'[{_CJK}]+')
_CJK_CHAR_RE = re.compile(f'[{_CJK}]')

_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Split mixed Chinese/English text into searchable tokens."""
    text = (text or '').lower()
    tokens: list[str] = _ASCII_TOKEN_RE.findall(text)
    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(_CJK_CHAR_RE.findall(run))
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class KnowledgeHit:
    id: str
    title: str
    content: str
    category: str
    score: float
    keyword_score: float = 0.0
    vector_score: float = 0.0
    matched_terms: list[str] = field(default_factory=list)

    def as_source(self) -> dict:
        return {
            'id': self.id,
            'title': self.title,
            'category': self.category,
            'score': round(self.score, 4),
            'keyword_score': round(self.keyword_score, 4),
            'vector_score': round(self.vector_score, 4),
        }


@dataclass
class _Doc:
    doc: KnowledgeDocument
    length: int
    term_freq: dict[str, int]


class BM25Index:
    """In-memory BM25 over one tenant scope."""

    def __init__(self, documents: list[KnowledgeDocument]):
        self._docs: list[_Doc] = []
        self._df: dict[str, int] = {}
        self._idf: dict[str, float] = {}
        self._avg_len = 1.0
        self._build(documents)

    def _build(self, documents: list[KnowledgeDocument]) -> None:
        for document in documents:
            # Question/title and explicit keywords outweigh the body text, so a
            # close title match outranks a passing mention in an answer.
            weighted = ' '.join([
                (document.title + ' ') * 3,
                (document.keywords + ' ') * 2,
                document.content,
            ])
            tokens = tokenize(weighted)
            term_freq: dict[str, int] = {}
            for token in tokens:
                term_freq[token] = term_freq.get(token, 0) + 1
            self._docs.append(_Doc(document, len(tokens) or 1, term_freq))
            for token in term_freq:
                self._df[token] = self._df.get(token, 0) + 1

        count = len(self._docs)
        self._avg_len = (sum(d.length for d in self._docs) / count) if count else 1.0
        for token, freq in self._df.items():
            self._idf[token] = math.log(1 + (count - freq + 0.5) / (freq + 0.5))

    @property
    def size(self) -> int:
        return len(self._docs)

    def search(self, query: str, *, top_k: int = 4) -> list[KnowledgeHit]:
        query_tokens = set(tokenize(query))
        if not query_tokens or not self._docs:
            return []

        hits: list[KnowledgeHit] = []
        for entry in self._docs:
            score = 0.0
            matched: list[str] = []
            for token in query_tokens:
                freq = entry.term_freq.get(token)
                if not freq:
                    continue
                idf = self._idf.get(token, 0.0)
                denominator = freq + _K1 * (1 - _B + _B * entry.length / self._avg_len)
                score += idf * (freq * (_K1 + 1)) / denominator
                matched.append(token)
            if score <= 0:
                continue

            # Near-verbatim title match bonus: BM25 under-rewards very short entries.
            if query.strip() and (query.strip() in entry.doc.title or entry.doc.title in query.strip()):
                score += 2.5

            hits.append(KnowledgeHit(
                id=entry.doc.id,
                title=entry.doc.title,
                content=entry.doc.content,
                category=entry.doc.category,
                score=round(score, 4),
                keyword_score=round(score, 4),
                matched_terms=sorted(set(matched))[:8],
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


# --------------------------------------------------------------------------- #
# Scope-aware index cache
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[tuple, BM25Index]] = {}


def _visible_documents(db: Session, tenant_id: str, store_id: str) -> list[KnowledgeDocument]:
    """Documents this tenant/store is allowed to see.

    ``tenant_id == ''`` is a platform-global document (shipped FAQ);
    ``store_id == ''`` is shared across the tenant's stores.
    """
    stmt = select(KnowledgeDocument).where(
        KnowledgeDocument.enabled.is_(True),
        KnowledgeDocument.tenant_id.in_([tenant_id, '']),
    )
    if store_id:
        stmt = stmt.where(KnowledgeDocument.store_id.in_([store_id, '']))
    return list(db.execute(stmt).scalars())


def _fingerprint(documents: list[KnowledgeDocument]) -> tuple:
    return tuple(sorted((d.id, d.updated_at.isoformat() if d.updated_at else '') for d in documents))


def get_index(db: Session, tenant_id: str, store_id: str = '') -> BM25Index:
    """Return the cached index for a scope, rebuilding it when the KB changed."""
    documents = _visible_documents(db, tenant_id, store_id)
    fingerprint = _fingerprint(documents)
    key = (tenant_id, store_id)
    with _lock:
        cached = _cache.get(key)
        if cached is None or cached[0] != fingerprint:
            index = BM25Index(documents)
            _cache[key] = (fingerprint, index)
            log_event(logger, 'knowledge_index_rebuilt', scope=f'{tenant_id}/{store_id}',
                      documents=index.size)
            return index
        return cached[1]


def invalidate_cache() -> None:
    """Drop every cached index (called after any KB write)."""
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------- #
# Vector channel (phase 2)
# --------------------------------------------------------------------------- #
def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _vector_search(
    documents: list[KnowledgeDocument], query_vector: list[float], *, top_k: int
) -> list[KnowledgeHit]:
    hits: list[KnowledgeHit] = []
    for document in documents:
        if not document.embedding:
            continue
        score = cosine(query_vector, list(document.embedding))
        if score <= 0:
            continue
        hits.append(KnowledgeHit(
            id=document.id,
            title=document.title,
            content=document.content,
            category=document.category,
            score=round(score, 4),
            vector_score=round(score, 4),
        ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


def rrf_fuse(channels: list[list[KnowledgeHit]], *, k: int = 60, top_k: int = 4) -> list[KnowledgeHit]:
    """Reciprocal Rank Fusion: ``score(d) = Σ 1 / (k + rank_i(d))``.

    Rank-based fusion is deliberately chosen over score normalisation because
    BM25 scores and cosine similarities are not on a comparable scale, and
    normalising them per query is unstable for small result sets.
    """
    fused: dict[str, KnowledgeHit] = {}
    totals: dict[str, float] = {}

    for channel in channels:
        for rank, hit in enumerate(channel):
            totals[hit.id] = totals.get(hit.id, 0.0) + 1.0 / (k + rank + 1)
            existing = fused.get(hit.id)
            if existing is None:
                fused[hit.id] = hit
            else:
                existing.keyword_score = max(existing.keyword_score, hit.keyword_score)
                existing.vector_score = max(existing.vector_score, hit.vector_score)
                existing.matched_terms = sorted(set(existing.matched_terms) | set(hit.matched_terms))[:8]

    for doc_id, hit in fused.items():
        hit.score = round(totals[doc_id], 6)

    ordered = sorted(fused.values(), key=lambda h: h.score, reverse=True)
    return ordered[:top_k]


async def retrieve(
    db: Session,
    query: str,
    *,
    tenant_id: str,
    store_id: str = '',
    top_k: int = 4,
    min_score: float = 0.0,
    embedder=None,
    rrf_k: int = 60,
) -> list[KnowledgeHit]:
    """Hybrid retrieval. Never raises — a broken KB must not break a conversation."""
    from .. import metrics

    try:
        index = get_index(db, tenant_id, store_id)
        keyword_hits = index.search(query, top_k=max(top_k * 3, 8))

        channels: list[list[KnowledgeHit]] = [keyword_hits]

        if embedder is not None:
            vector = await embedder.embed_one(query)
            if vector:
                documents = _visible_documents(db, tenant_id, store_id)
                channels.append(_vector_search(documents, vector, top_k=max(top_k * 3, 8)))

        hits = rrf_fuse(channels, k=rrf_k, top_k=top_k) if len(channels) > 1 else keyword_hits[:top_k]

        if min_score > 0:
            # RRF scores are tiny by construction; only apply the floor to the
            # pure-keyword path where the score is a real BM25 value.
            if len(channels) == 1:
                hits = [h for h in hits if h.score >= min_score]

        metrics.KB_HITS.labels('hit' if hits else 'miss').inc()
        log_event(logger, 'knowledge_retrieve', query_len=len(query or ''), hits=len(hits),
                  channels=len(channels), top_score=round(hits[0].score, 4) if hits else 0.0)
        return hits
    except Exception as exc:  # noqa: BLE001
        metrics.KB_HITS.labels('miss').inc()
        log_event(logger, 'knowledge_error', level=logging.ERROR,
                  error=type(exc).__name__, message=str(exc)[:300])
        return []


def build_context_blocks(hits: list[KnowledgeHit], *, limit: int = 2400) -> list[str]:
    """Render hits into the context blocks handed to the LLM answer node."""
    blocks: list[str] = []
    used = 0
    for hit in hits:
        block = f'{hit.title}：{hit.content}'
        if used + len(block) > limit:
            break
        blocks.append(block)
        used += len(block)
    return blocks
