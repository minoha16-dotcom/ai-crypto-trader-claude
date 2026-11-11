from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AI Crypto Trader V2"
    database_url: str = "sqlite:///./data/trader.db"
    default_exchange: str = "binance"
    default_market_type: str = "spot"

    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=100)
    max_open_risk_pct: float = Field(default=2.0, gt=0, le=100)
    max_daily_loss_pct: float = Field(default=3.0, gt=0, le=100)
    max_position_notional_pct: float = Field(default=20.0, gt=0, le=100)

    taker_fee_rate: float = Field(default=0.0006, ge=0, lt=1)
    slippage_bps: float = Field(default=2.0, ge=0, lt=10000)

    signal_min_adx: float = Field(default=20.0, ge=0, le=100)
    signal_min_volume_ratio: float = Field(default=1.0, ge=0)
    signal_min_atr_pct: float = Field(default=0.1, gt=0)

    paper_starting_balance: float = Field(default=10000.0, gt=0)
    paper_entry_max_deviation_pct: float = Field(default=5.0, gt=0, le=100)
    scan_symbols: str = "BTC/USDT,ETH/USDT,SOL/USDT"
    scan_timeframe: str = "1h"

    @model_validator(mode="after")
    def validate_risk_relationships(self):
        if self.risk_per_trade_pct > self.max_open_risk_pct:
            raise ValueError(
                "risk_per_trade_pct cannot exceed max_open_risk_pct"
            )
        return self

    @property
    def symbols(self):
        return [symbol.strip() for symbol in self.scan_symbols.split(",") if symbol.strip()]


settings = Settings()
