# -*- coding: utf-8 -*-
"""Alembic environment.

Two things here are not boilerplate:

1. **Two metadata objects, one database.** The gateway and the business service
   each declare their own ``DeclarativeBase``, and both write to the same
   PostgreSQL database. Alembic accepts a *list* of metadata for
   ``target_metadata``, which is used instead of merging the two Bases — merging
   would mean changing production code to make the migration tool happy, and the
   next person to add a model would have to know about the merge.

2. **``compare_type`` / ``compare_server_default`` are on.** Autogenerate only
   detects what it is asked to compare. With these off, changing a column from
   ``String(32)`` to ``String(64)``, or adding a server-side default, produces an
   empty migration — the exact class of silent no-op this migration tree exists
   to eliminate.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# The migration runner executes inside the gateway image, where the repo is at
# /app. Adding the root explicitly means ``alembic`` works from any cwd.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from business_service.app.db import Base as BusinessBase  # noqa: E402
from gateway.app.config import get_settings  # noqa: E402
from gateway.app.db import Base as GatewayBase  # noqa: E402

# Importing the model modules is what *populates* the metadata. Without these
# two imports the autogenerate diff would be "drop everything", because empty
# metadata looks like a request to remove every table.
from business_service.app import models as _business_models  # noqa: E402,F401
from gateway.app import models as _gateway_models  # noqa: E402,F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = [GatewayBase.metadata, BusinessBase.metadata]


def _database_url() -> str:
    """The URL the services themselves connect with — single source of truth."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section['sqlalchemy.url'] = _database_url()

    connectable = engine_from_config(
        section, prefix='sqlalchemy.', poolclass=pool.NullPool, future=True
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
