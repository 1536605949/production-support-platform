# -*- coding: utf-8 -*-
"""Database wiring for the platform trunk.

Works against SQLite (unit tests / offline demo) and PostgreSQL (production).
Anything PostgreSQL-specific is declared with ``with_variant`` so a single
metadata definition serves both.
"""
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

# JSON that becomes JSONB on PostgreSQL and stays JSON on SQLite.
JSONType = JSON().with_variant(JSONB, 'postgresql')


class Base(DeclarativeBase):
    pass


def _prepare_sqlite_path(url: str) -> None:
    if url.startswith('sqlite:///') and not url.endswith(':memory:'):
        raw = url.removeprefix('sqlite:///')
        Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def build_engine(url: str):
    _prepare_sqlite_path(url)
    settings = get_settings()

    if url.startswith('sqlite'):
        # A file-backed SQLite connection is not a network resource, so the
        # pooling knobs below are meaningless (and QueuePool + threads would
        # turn concurrent writers into "database is locked"). Keep the tests on
        # the simplest thing that works.
        return create_engine(
            url, connect_args={'check_same_thread': False}, pool_pre_ping=True, future=True,
        )

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_timeout=settings.db_pool_timeout_seconds,
        # Applied server-side to every connection this pool hands out, so a
        # transaction nobody closes cannot outlive ``db_idle_tx_timeout_ms``.
        # Without it the failure mode is silent: the transaction holds a
        # snapshot, autovacuum cannot reclaim rows newer than it, and the table
        # grows without the application doing anything visibly wrong.
        connect_args={
            'options': f'-c idle_in_transaction_session_timeout={settings.db_idle_tx_timeout_ms}',
        },
        future=True,
    )


_s = get_settings()
engine = build_engine(_s.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_schema() -> None:
    """Create every table declared on :class:`Base` (idempotent)."""
    from . import models  # noqa: F401 - ensures the metadata is populated

    Base.metadata.create_all(bind=engine)
