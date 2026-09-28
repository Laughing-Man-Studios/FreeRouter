"""SQLAlchemy Core table definitions (no ORM).

M0 creates the database shell only. ``models`` mirrors the provider mapping
loaded from ``config.yaml`` so the router has a stable internal identity for
each model, and ``request_logs`` records operational metadata for auditing.

Neither table stores prompt or completion text (spec 9, CONSTITUTION 9.1).
Quota accounting against these tables is deferred to M1/M2.
"""

from __future__ import annotations

from sqlalchemy import Column, Float, Integer, MetaData, String, Table

__all__ = ["metadata", "models_table", "request_logs_table"]

metadata = MetaData()

models_table = Table(
    "models",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider", String, nullable=False),
    Column("provider_id", String, nullable=False),
)

request_logs_table = Table(
    "request_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String, nullable=False, index=True),
    Column("created_at", String, nullable=False),
    Column("model", String, nullable=False),
    Column("provider", String, nullable=False),
    # provider_id is a free-form string in the M0 schema: request_logs is a
    # generic audit table, so it must not block when a provider id is absent.
    Column("provider_id", String, nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("http_status", Integer, nullable=True),
    Column("error_category", String, nullable=True),
)
