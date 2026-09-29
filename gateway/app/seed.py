# -*- coding: utf-8 -*-
"""Idempotent bootstrap data.

Runs at startup when ``AUTO_SEED=1`` (the default outside production). It
creates:

* the demo tenant plus one admin / one agent / one customer account,
* a platform-global knowledge base (``tenant_id=''``) that every tenant can
  read but none can modify.

Global documents are the reason a brand-new tenant is useful on day one
without uploading anything, while still being unable to see another tenant's
private documents.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from .config import get_settings
from .core import auth as auth_mod
from .core import knowledge, tenant as tenant_mod
from .db import SessionLocal
from .models import KnowledgeDocument, Store, User

logger = logging.getLogger('gateway.seed')

DEMO_TENANT = 'tenant-demo'
DEMO_STORE = 'store-demo'

#: ``email, password, role, display name, upstream customer id``
#:
#: The last field maps the platform account onto the order system's customer id.
#: The two are separate identity spaces; the demo customer is the account that
#: owns ``ORD-1001`` over in the business service, so the refund flow has a real
#: order to act on. Accounts with no order-system counterpart leave it blank,
#: which means "same id as the platform user id".
DEMO_ACCOUNTS = [
    ('admin@demo.local', 'DemoAdmin123', 'admin', 'Demo Admin', ''),
    ('agent@demo.local', 'DemoAgent123', 'agent', 'Demo Agent', ''),
    ('reviewer@demo.local', 'DemoReviewer123', 'reviewer', 'Demo Reviewer', ''),
    ('customer@demo.local', 'DemoCustomer123', 'customer', 'Demo Customer', 'user001'),
]

GLOBAL_KB: list[dict] = [
    # -- refund ---------------------------------------------------------- #
    {'category': 'refund', 'title': '退款到账时间',
     'keywords': '退款 到账 多久 几天 时间',
     'content': '退款审核通过后，原路退回一般 1-3 个工作日到账；银行卡渠道最长 7 个工作日。'
                '如超时未到账，请提供订单号由人工客服核实。'},
    {'category': 'refund', 'title': '七天无理由退货规则',
     'keywords': '七天无理由 退货 规则 条件',
     'content': '自签收次日起 7 天内，商品未使用、包装完好、配件齐全可申请无理由退货。'
                '定制类、生鲜类、拆封的数字化商品不支持。'},
    {'category': 'refund', 'title': '退款申请被拒绝怎么办',
     'keywords': '退款 被拒 拒绝 不通过 申诉',
     'content': '退款被拒通常因为超出可退期限、商品已影响二次销售，或订单已完成退款。'
                '可在会话中回复「转人工」，由人工客服复核并给出处理方案。'},
    {'category': 'refund', 'title': '退款可以退到别的账户吗',
     'keywords': '退款 其他账户 换账户 提现',
     'content': '为防范资金风险，退款只能原路退回原支付账户，不支持更换收款账户。'},
    # -- order ----------------------------------------------------------- #
    {'category': 'order_query', 'title': '订单发货时间',
     'keywords': '发货 时间 多久 什么时候',
     'content': '现货商品在付款后 24 小时内发货，预售商品以商品页标注的发货时间为准。'
                '发货后可在会话中提供订单号查询物流。'},
    {'category': 'order_query', 'title': '修改收货地址',
     'keywords': '修改 收货地址 地址 改地址',
     'content': '订单未发货时可修改收货地址；已发货的订单无法修改，可联系快递协商转运或拒收后重新下单。'},
    {'category': 'order_query', 'title': '物流长时间不更新',
     'keywords': '物流 不更新 停滞 卡住 没动',
     'content': '物流信息通常每 12 小时更新一次。超过 48 小时无更新可能是中转滞留，'
                '请提供订单号，我们代为向承运商发起查询。'},
    # -- tech support ---------------------------------------------------- #
    {'category': 'tech_support', 'title': '收不到验证码',
     'keywords': '验证码 收不到 短信 手机号',
     'content': '请依次排查：1) 确认手机号输入正确；2) 检查短信是否被拦截；'
                '3) 等待 60 秒后重新获取；4) 尝试切换 Wi-Fi 与移动网络。'
                '仍无法收到可回复「转人工」由人工核验。'},
    {'category': 'tech_support', 'title': 'App 闪退或打不开',
     'keywords': '闪退 打不开 崩溃 白屏 黑屏',
     'content': '请先升级到最新版本并清理缓存后重试；若仍闪退，请提供手机型号与系统版本，'
                '我们会在 24 小时内跟进。'},
    {'category': 'tech_support', 'title': '登录提示密码错误',
     'keywords': '登录 密码错误 登不上 忘记密码',
     'content': '可在登录页点击「忘记密码」通过手机号重置。'
                '连续 5 次输错会锁定 15 分钟，请稍后再试。'},
    {'category': 'tech_support', 'title': '支付失败怎么办',
     'keywords': '支付失败 付款失败 扣款 支付不了',
     'content': '支付失败常见原因是银行卡限额、网络超时或风控拦截。'
                '若已扣款但订单未生成，通常 24 小时内自动退回，请勿重复支付。'},
    # -- faq ------------------------------------------------------------- #
    {'category': 'faq', 'title': '如何开具发票',
     'keywords': '发票 开票 报销 电子发票',
     'content': '订单完成后可在「我的-订单详情-申请开票」提交电子发票，'
                '一般 1 个工作日内发送到预留邮箱。企业抬头需提供税号。'},
    {'category': 'faq', 'title': '优惠券使用规则',
     'keywords': '优惠券 优惠 满减 使用规则',
     'content': '优惠券在有效期内、满足门槛时可叠加平台活动使用，'
                '单笔订单限用一张，不找零、不兑现、过期作废。'},
    {'category': 'faq', 'title': '支持的支付方式',
     'keywords': '支付方式 微信 支付宝 银行卡',
     'content': '支持微信支付、支付宝、银联卡以及部分地区的货到付款，'
                '具体以结算页展示为准。'},
    {'category': 'faq', 'title': '会员等级与积分',
     'keywords': '会员 等级 积分 成长值',
     'content': '会员等级由近 12 个月消费金额决定，等级越高享受的折扣与专属客服权益越多；'
                '积分可在积分商城兑换优惠券。'},
    {'category': 'faq', 'title': '运费与包邮政策',
     'keywords': '运费 包邮 邮费 配送费',
     'content': '单笔订单实付满 99 元包邮（偏远地区除外）；'
                '未满额收取 8 元运费，偏远地区按实际计费。'},
]


def seed_platform() -> None:
    settings = get_settings()
    try:
        with SessionLocal() as db:
            tenant_mod.ensure_tenant(
                db, DEMO_TENANT, name='Demo 商户',
                daily_quota=settings.tenant_daily_message_quota,
                rate_limit=settings.rate_limit_per_minute,
            )
            if db.get(Store, DEMO_STORE) is None:
                db.add(Store(id=DEMO_STORE, tenant_id=DEMO_TENANT, name='Demo 旗舰店'))
                db.commit()

            for email, password, role, display, business_customer_id in DEMO_ACCOUNTS:
                exists = (
                    db.execute(
                        select(User).where(User.tenant_id == DEMO_TENANT, User.email == email)
                    )
                    .scalars()
                    .first()
                )
                if exists is None:
                    db.add(User(
                        id=f'U-{email.split("@")[0]}',
                        tenant_id=DEMO_TENANT,
                        store_id=DEMO_STORE,
                        email=email,
                        password_hash=auth_mod.hash_password(password),
                        display_name=display,
                        role=role,
                        status='active',
                        business_customer_id=business_customer_id,
                    ))
                elif exists.business_customer_id != business_customer_id:
                    # Repair the mapping on an already-seeded database, so a
                    # redeploy does not need the volume to be thrown away.
                    exists.business_customer_id = business_customer_id
            db.commit()

            created = 0
            for entry in GLOBAL_KB:
                doc_id = 'KB-GLOBAL-' + entry['title']
                if db.get(KnowledgeDocument, doc_id) is None:
                    db.add(KnowledgeDocument(
                        id=doc_id,
                        tenant_id='',      # platform-global
                        store_id='',
                        category=entry['category'],
                        title=entry['title'],
                        content=entry['content'],
                        keywords=entry['keywords'],
                        enabled=True,
                    ))
                    created += 1
            db.commit()

            if created:
                knowledge.invalidate_cache()
                logger.info('seeded %d global knowledge documents', created)
    except Exception:  # noqa: BLE001 - a seed failure must not stop the service
        logger.exception('platform seed failed (continuing without seed data)')
