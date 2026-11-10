from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_name: str = 'AI Crypto Trader V2'
    database_url: str = 'sqlite:///./data/trader.db'
    default_exchange: str = 'binance'
    default_market_type: str = 'spot'
    risk_per_trade_pct: float = 0.5
    max_open_risk_pct: float = 2.0
    max_daily_loss_pct: float = 3.0
    max_position_notional_pct: float = 20.0
    taker_fee_rate: float = 0.0006
    slippage_bps: float = 2.0
    signal_min_adx: float = 20.0
    signal_min_volume_ratio: float = 1.0
    signal_min_atr_pct: float = 0.1
    paper_starting_balance: float = 10000.0
    scan_symbols: str = 'BTC/USDT,ETH/USDT,SOL/USDT'
    scan_timeframe: str = '1h'
    @property
    def symbols(self):
        return [s.strip() for s in self.scan_symbols.split(',') if s.strip()]

settings = Settings()
