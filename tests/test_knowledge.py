# -*- coding: utf-8 -*-
"""Knowledge base: tokenizer, BM25, permission filtering, RRF fusion."""
import pytest

from gateway.app.core import knowledge
from gateway.app.core.knowledge import BM25Index, KnowledgeHit, cosine, rrf_fuse, tokenize
from gateway.app.models import KnowledgeDocument


class _Doc:
    """Lightweight stand-in for a KnowledgeDocument row."""

    def __init__(self, doc_id, title, content, category='faq', keywords='', tenant_id=''):
        self.id = doc_id
        self.title = title
        self.content = content
        self.category = category
        self.keywords = keywords
        self.tenant_id = tenant_id
        self.store_id = ''
        self.enabled = True
        self.embedding = []
        self.updated_at = None


def test_tokenizer_handles_mixed_chinese_and_ascii():
    tokens = tokenize('退款 refund 政策')
    assert 'refund' in tokens
    assert '退' in tokens and '款' in tokens
    assert '退款' in tokens          # bigram, gives Chinese real recall


def test_bm25_ranks_the_closest_title_first():
    index = BM25Index([
        _Doc('a', '退款到账时间', '退款审核通过后 1-3 个工作日到账'),
        _Doc('b', '修改收货地址', '未发货时可修改收货地址'),
        _Doc('c', '如何开具发票', '订单完成后可申请电子发票'),
    ])
    hits = index.search('退款多久到账', top_k=3)
    assert hits
    assert hits[0].id == 'a'


def test_bm25_returns_nothing_for_unrelated_ascii_noise():
    index = BM25Index([_Doc('a', '退款到账时间', '退款审核通过后到账')])
    assert index.search('zzzz qqqq xxxx') == []


def test_rrf_fusion_rewards_documents_found_by_both_channels():
    keyword = [
        KnowledgeHit(id='shared', title='t', content='c', category='faq', score=9.0, keyword_score=9.0),
        KnowledgeHit(id='kw-only', title='t', content='c', category='faq', score=8.0, keyword_score=8.0),
    ]
    vector = [
        KnowledgeHit(id='shared', title='t', content='c', category='faq', score=0.9, vector_score=0.9),
        KnowledgeHit(id='vec-only', title='t', content='c', category='faq', score=0.8, vector_score=0.8),
    ]
    fused = rrf_fuse([keyword, vector], top_k=3)
    assert fused[0].id == 'shared'          # top of both channels
    assert {h.id for h in fused} == {'shared', 'kw-only', 'vec-only'}
    assert fused[0].keyword_score == 9.0
    assert fused[0].vector_score == 0.9


def test_cosine_similarity():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([], [1.0]) == 0.0


# --------------------------------------------------------------------------- #
# Permission filtering
# --------------------------------------------------------------------------- #
def test_retrieval_is_scoped_to_the_tenant(db_session):
    db_session.add_all([
        KnowledgeDocument(id='KB-T1-PRIVATE', tenant_id='tenant-t1', title='T1 专属退款政策',
                          content='T1 私有：退款需 3 天', keywords='退款'),
        KnowledgeDocument(id='KB-T2-PRIVATE', tenant_id='tenant-t2', title='T2 专属退款政策',
                          content='T2 私有：退款需 5 天', keywords='退款'),
        KnowledgeDocument(id='KB-GLOBAL-X', tenant_id='', title='全局退款政策',
                          content='全局：退款原路退回', keywords='退款'),
    ])
    db_session.commit()
    knowledge.invalidate_cache()

    t1_index = knowledge.get_index(db_session, 'tenant-t1')
    ids = {d.doc.id for d in t1_index._docs}
    assert 'KB-T1-PRIVATE' in ids
    assert 'KB-GLOBAL-X' in ids
    assert 'KB-T2-PRIVATE' not in ids      # another tenant's private doc is invisible


@pytest.mark.asyncio
async def test_retrieve_never_raises_and_reports_hits(db_session):
    hits = await knowledge.retrieve(
        db_session, '退款多久到账', tenant_id='tenant-demo', top_k=3, min_score=0.0
    )
    assert hits
    assert all(hit.title for hit in hits)


@pytest.mark.asyncio
async def test_retrieve_on_an_empty_scope_returns_no_hits(db_session):
    hits = await knowledge.retrieve(
        db_session, '退款多久到账', tenant_id='tenant-nobody', top_k=3, min_score=0.0
    )
    assert hits == [] or all(h.id.startswith('KB-GLOBAL') for h in hits)


def test_context_blocks_respect_the_budget():
    hits = [
        KnowledgeHit(id=str(i), title=f'标题{i}', content='内容' * 400, category='faq', score=1.0)
        for i in range(10)
    ]
    blocks = knowledge.build_context_blocks(hits, limit=1000)
    assert 0 < len(blocks) < 10
    assert sum(len(b) for b in blocks) <= 1100
