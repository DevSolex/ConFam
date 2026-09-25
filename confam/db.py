"""
confam.db — database connection pool.

Returns psycopg2 connections as the confam_app role (Engineering Rule — the
application must never connect as confam_migrator at runtime).

All database access goes through get_conn(). The connection is obtained from
the pool and must be used as a context manager so it is returned cleanly:

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(...)
        conn.commit()

Engineering Rule 6: amounts are always int (kobo). This module never touches
float values; that validation happens in the domain layer before DB writes.
"""

import os
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.pool

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. "
                "Copy .env.example to .env and set DATABASE_URL "
                "to a confam_app connection string."
            )
        _pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=url)
    return _pool


@contextmanager
def get_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a connection from the pool; return it on exit."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def close_pool() -> None:
    """Close all pool connections. Call on application shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
