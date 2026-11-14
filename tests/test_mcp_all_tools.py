import asyncio

from mcp_server import server


def test_all_mcp_tools_route_to_paper_backend_endpoints(monkeypatch):
    calls = []

    async def fake_api(method, path, *, params=None, payload=None):
        calls.append((method, path, params, payload))
        return {"ok": True}

    monkeypatch.setattr(server, "_api", fake_api)

    async def exercise():
        await server.get_status()
        await server.list_exchanges()
        await server.get_market_data("kucoin", "btc/usdt", "spot")
        await server.scan_opportunities(
            "bybit", "futures", "btc/usdt,eth/usdt", "1h"
        )
        await server.get_instrument_spec("okx", "btc/usdt", "futures")
        await server.get_open_positions()
        await server.get_opportunities(4)
        await server.run_backtest("okx", "btc/usdt", "spot", "4h", 300)
        await server.get_signals(5)
        await server.get_paper_account()
        await server.get_risk_limits()
        await server.get_trade_journal()
        await server.open_paper_trade(
            "kucoin", "btc/usdt", "spot", "LONG", 100.0, 95.0, 110.0
        )
        await server.open_paper_trade(
            "bybit", "btc/usdt", "futures", "SHORT", 100.0, 105.0, 80.0,
            leverage=3, funding_rate=0.0002, funding_interval_hours=4,
        )
        await server.close_paper_trade(7, 105.0)

    asyncio.run(exercise())

    assert calls == [
        ("GET", "/api/health", None, None),
        ("GET", "/api/exchanges", None, None),
        ("GET", "/api/markets/kucoin/BTC/USDT", {"market_type": "spot"}, None),
        ("POST", "/api/scan", {
            "exchange": "bybit", "market_type": "futures",
            "symbols": "BTC/USDT,ETH/USDT", "timeframe": "1h",
        }, None),
        ("GET", "/api/instruments/okx/BTC/USDT", {"market_type": "futures"}, None),
        ("GET", "/api/positions", None, None),
        ("GET", "/api/opportunities", {"limit": 4}, None),
        ("GET", "/api/backtest", {
            "exchange": "okx", "symbol": "BTC/USDT", "market_type": "spot",
            "timeframe": "4h", "limit": 300, "leverage": 1.0,
        }, None),
        ("GET", "/api/signals", {"limit": 5}, None),
        ("GET", "/api/account", None, None),
        ("GET", "/api/risk", None, None),
        ("GET", "/api/journal", None, None),
        ("POST", "/api/paper/open", None, {
            "exchange": "kucoin", "symbol": "BTC/USDT",
            "market_type": "spot", "side": "LONG",
            "entry": 100.0, "stop": 95.0, "target": 110.0,
            "leverage": 1.0,
        }),
        ("POST", "/api/paper/open", None, {
            "exchange": "bybit", "symbol": "BTC/USDT",
            "market_type": "futures", "side": "SHORT",
            "entry": 100.0, "stop": 105.0, "target": 80.0,
            "leverage": 3, "funding_rate": 0.0002,
            "funding_interval_hours": 4,
        }),
        ("POST", "/api/paper/7/close", None, {"exit_price": 105.0}),
    ]
