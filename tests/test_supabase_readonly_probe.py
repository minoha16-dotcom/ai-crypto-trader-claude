"""Read-only live Supabase schema probe; skips in ordinary CI without the secret."""
import os


def test_supabase_connection_and_required_schema_read_only():
    url = os.environ.get("TRADER_DATABASE_URL", "").strip()
    if not url:
        import pytest
        pytest.skip("TRADER_DATABASE_URL is reserved for the manual Supabase probe workflow")
    assert url.startswith(("postgres://", "postgresql://")), (
        "TRADER_DATABASE_URL must be a PostgreSQL URI."
    )

    import psycopg

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
            assert all(cursor.fetchone()), "A required application table is missing"
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'trades'
                """
            )
            columns = {row[0] for row in cursor.fetchall()}
            assert {"contract_size", "contract_type"} <= columns
            cursor.execute("SELECT 1 FROM public.trades LIMIT 0")
            cursor.execute("SELECT 1 FROM public.signals LIMIT 0")
            cursor.execute("SELECT 1 FROM public.daily_equity_snapshots LIMIT 0")
