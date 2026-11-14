from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Exchanges available for new market-data scans and paper trades. Binance remains
# a legacy-only adapter so existing persisted paper positions can still be marked.
SUPPORTED_EXCHANGES = ("kucoin", "bybit", "okx")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AI Crypto Trader V2"
    database_url: str = "sqlite:///./data/trader.db"
    default_exchange: str = "kucoin"
    # Official global Bybit REST hostname; api.bytick.com is an alternate official host.
    bybit_api_hostname: str = "bybit.com"
    default_market_type: str = "spot"
    # Dashboard login is loaded from process environment or the local .env file.
    dashboard_username: str = Field(default="", repr=False)
    dashboard_password: str = Field(default="", repr=False)

    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=100, allow_inf_nan=False)
    max_open_risk_pct: float = Field(default=2.0, gt=0, le=100, allow_inf_nan=False)
    max_daily_loss_pct: float = Field(default=3.0, gt=0, le=100, allow_inf_nan=False)
    max_position_notional_pct: float = Field(default=20.0, gt=0, le=100, allow_inf_nan=False)
    max_open_trades: int = Field(default=3, ge=1, le=100)
    reward_risk_ratio: float = Field(default=2.0, gt=0, le=20, allow_inf_nan=False)

    taker_fee_rate: float = Field(default=0.0006, ge=0, lt=1, allow_inf_nan=False)
    slippage_bps: float = Field(default=2.0, ge=0, lt=10000, allow_inf_nan=False)

    signal_min_adx: float = Field(default=20.0, ge=0, le=100, allow_inf_nan=False)
    signal_min_volume_ratio: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    signal_min_atr_pct: float = Field(default=0.1, gt=0, allow_inf_nan=False)

    paper_starting_balance: float = Field(default=10000.0, gt=0, allow_inf_nan=False)
    paper_entry_max_deviation_pct: float = Field(default=5.0, gt=0, le=100, allow_inf_nan=False)
    position_monitor_max_price_age_seconds: float = Field(
        default=120.0, gt=0, le=3600, allow_inf_nan=False
    )

    # Paper Futures model: isolated margin, bounded leverage and explicit
    # funding assumptions. Rates are simulation inputs, not live venue rates.
    max_futures_leverage: float = Field(default=5.0, ge=1, le=50, allow_inf_nan=False)
    max_futures_margin_pct: float = Field(default=50.0, gt=0, le=100, allow_inf_nan=False)
    paper_futures_default_funding_rate: float = Field(
        default=0.0001, ge=-0.02, le=0.02, allow_inf_nan=False
    )
    paper_futures_funding_interval_hours: float = Field(
        default=8.0, gt=0, le=24, allow_inf_nan=False
    )
    paper_futures_maintenance_margin_rate: float = Field(
        default=0.005, gt=0, lt=0.2, allow_inf_nan=False
    )
    paper_futures_liquidation_fee_rate: float = Field(
        default=0.005, ge=0, lt=0.05, allow_inf_nan=False
    )
    scan_symbols: str = "BTC/USDT,ETH/USDT,SOL/USDT"
    scan_timeframe: str = "1h"

    @field_validator("bybit_api_hostname")
    @classmethod
    def validate_bybit_api_hostname(cls, value):
        normalized = str(value).strip().lower()
        if normalized not in ("bybit.com", "bytick.com"):
            raise ValueError("bybit_api_hostname must be bybit.com or bytick.com")
        return normalized

    @field_validator("default_exchange")
    @classmethod
    def validate_default_exchange(cls, value):
        normalized = str(value).strip().lower()
        if normalized not in SUPPORTED_EXCHANGES:
            raise ValueError(
                "default_exchange must be one of: " + ", ".join(SUPPORTED_EXCHANGES)
            )
        return normalized

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
