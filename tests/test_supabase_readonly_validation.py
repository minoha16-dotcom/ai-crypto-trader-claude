"""Read-only smoke test for the shared Supabase PostgreSQL database.

This test intentionally inspects schema metadata only. It never reads trade or
signal records and sets the current transaction to READ ONLY before any query.
"""
import os

import pytest


def test_supabase_connection_and_required_schema_read_only():
    url = os.environ.get("TRADER_DATABASE_URL", "").strip()
    if not url:
        pytest.fail("Missing repository secret TRADER_DATABASE_URL.")

    assert url.startswith(("postgres://", "postgresql://")), (
        "TRADER_DATABASE_URL must be a PostgreSQL connection URI."
    )

    import psycopg

    with psycopg.connect(
        url,
        connect_timeout=10,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            # Supabase poolers may ignore startup options, so explicitly mark
            # this transaction read-only before issuing any other statement.
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone()[0] == "on", (
                "The PostgreSQL transaction is not read-only."
            )

            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1

            cursor.execute(
                """
                SELECT
                    to_regclass('public.trades'),
                    to_regclass('public.signals'),
                    to_regclass('public.daily_equity_snapshots')
                """
            )
            tables = cursor.fetchone()
            assert all(tables), (
                "One or more required tables are missing: "
                "public.trades, public.signals, public.daily_equity_snapshots."
            )

            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'trades'
                """
            )
            columns = {row[0] for row in cursor.fetchall()}
            assert {"contract_size", "contract_type"} <= columns, (
                "public.trades is missing Futures contract metadata."
            )

            # Parse table definitions without retrieving any user records.
            cursor.execute("SELECT 1 FROM public.trades LIMIT 0")
            cursor.execute("SELECT 1 FROM public.signals LIMIT 0")
            cursor.execute("SELECT 1 FROM public.daily_equity_snapshots LIMIT 0")
