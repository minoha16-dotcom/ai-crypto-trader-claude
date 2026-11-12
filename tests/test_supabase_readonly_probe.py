"""Safe live Supabase probe. Never writes, migrates, truncates, or reads trade rows."""
import os

import psycopg


def test_supabase_connection_and_required_schema_read_only():
    url = os.environ.get("TRADER_DATABASE_URL", "").strip()
    if not url:
        import pytest
        pytest.skip("TRADER_DATABASE_URL is only configured for the manual Supabase probe workflow")
    assert url.startswith(("postgres://", "postgresql://")), (
        "Set the GitHub Actions secret TRADER_DATABASE_URL to the Supabase "
        "PostgreSQL URI. The probe intentionally has no fallback database."
    )

    # Enforce read-only transactions at the PostgreSQL session level.
    with psycopg.connect(
        url,
        connect_timeout=10,
        options="-c default_transaction_read_only=on",
    ) as connection:
        with connection.cursor() as cursor:
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
            assert all(tables), "One or more required public tables are missing"

            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'trades'
                """
            )
            columns = {row[0] for row in cursor.fetchall()}
            assert {"contract_size", "contract_type"} <= columns, (
                "The deployed trades schema is missing Futures contract metadata"
            )

            # Parse the main table definitions without reading any user records.
            cursor.execute("SELECT 1 FROM public.trades LIMIT 0")
            cursor.execute("SELECT 1 FROM public.signals LIMIT 0")
            cursor.execute("SELECT 1 FROM public.daily_equity_snapshots LIMIT 0")
