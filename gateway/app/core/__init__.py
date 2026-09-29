# -*- coding: utf-8 -*-
"""Deterministic trunk: intent routing, retrieval, governance, workflow.

Nothing in this package imports FastAPI. The trunk is plain Python so it can be
unit-tested without spinning up an ASGI app, and so the same code could be
driven by a worker, a CLI or a Temporal activity without modification.
"""
