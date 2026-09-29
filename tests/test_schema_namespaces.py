# -*- coding: utf-8 -*-
"""The two services share one PostgreSQL database, so their table names must not overlap.

Why this needs a test rather than a convention:

``Base.metadata.create_all()`` is idempotent *by name*. If two services declare
the same table, the second declaration does not create a second table and does
not raise — it silently produces a model whose columns may not exist in the
database. The failure surfaces much later, as an ``UndefinedColumn`` on the
first INSERT, in whichever service happened to lose the race.

That is exactly what happened: the gateway declared ``audit_events`` with
``tenant_id``/``trace_id``/``actor_id`` columns while the business service
declared ``audit_events`` with none of them. Whichever started first created the
table; the other's model became unusable. Nothing warned.

A shared database is a deliberate simplification for this deployment, so the
disjointness has to be asserted rather than assumed. If the services are ever
split into separate databases, this test can be deleted along with the comment.
"""
from __future__ import annotations

import pytest


def _table_names(module) -> set[str]:
    from sqlalchemy.orm import DeclarativeBase

    names = set()
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, DeclarativeBase):
            table = getattr(value, '__tablename__', None)
            if table:
                names.add(table)
    return names


@pytest.fixture(scope='module')
def gateway_tables() -> set[str]:
    from gateway.app import models as gateway_models

    return _table_names(gateway_models)


@pytest.fixture(scope='module')
def business_tables() -> set[str]:
    from business_service.app import models as business_models

    return _table_names(business_models)


def test_both_services_declare_tables(gateway_tables, business_tables):
    """Guards the guard: an empty set would make the disjointness check vacuous."""
    assert len(gateway_tables) >= 10, sorted(gateway_tables)
    assert len(business_tables) >= 4, sorted(business_tables)


def test_table_names_do_not_collide(gateway_tables, business_tables):
    overlap = gateway_tables & business_tables
    assert not overlap, (
        'these table names are declared by BOTH services and share one database; '
        f'the second create_all() is a silent no-op: {sorted(overlap)}'
    )
