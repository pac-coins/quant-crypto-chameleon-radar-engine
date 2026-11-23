from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FORBIDDEN_EXECUTION_SYMBOLS: Final[frozenset[str]] = frozenset({"BNBUSDT"})
PHASE_1_LIVE_CONFIRMATION: Final[str] = "PHASE_1_GATES_6A_6B_PASSED"
PUBLIC_MARKET_DATA_URL: Final[str] = "https://data-api.binance.vision"
TESTNET_TRADING_URL: Final[str] = "https://testnet.binance.vision"
PRODUCTION_TRADING_URL: Final[str] = "https://api.binance.com"

Interval = Literal["1m", "1h", "4h"]


class ConfigError(RuntimeError):
    """Raised when configuration loading or mode invariant validation fails."""


class RunMode(StrEnum):
    ANALYST_ONLY = "ANALYST_ONLY"
    SHADOW_CHAMELEON = "SHADOW_CHAMELEON"
    LIVE_CHAMELEON_PHASE_1 = "LIVE_CHAMELEON_PHASE_1"


class BinanceEnv(StrEnum):
    TESTNET = "TESTNET"
    PRODUCTION = "PRODUCTION"


def _empty_to_none(val: Any) -> Any:
    if isinstance(val, str) and val.strip() == "":
        return None
    if isinstance(val, SecretStr) and val.get_secret_value().strip() == "":
        return None
    return val


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid", frozen=True)

    mode: RunMode
    binance_env: BinanceEnv = BinanceEnv.TESTNET
    binance_api_key: SecretStr | None = None
    binance_ed25519_private_key_path: Path | None = None
    live_confirmation: str | None = None
    approved_strategies_sha256: str | None = None
    shadow_initial_usdt: Decimal = Decimal("300")
    telegram_bot_token: SecretStr
    telegram_chat_id: str
    db_path: Path = Path("data/quant.db")
    backup_dir: Path = Path("data/backups")
    healthcheck_url: SecretStr | None = None
    offsite_backup_enabled: bool = False
    backup_telegram_chat_id: str | None = None
    backup_encryption_key: SecretStr | None = None

    @field_validator(
        "binance_api_key",
        "binance_ed25519_private_key_path",
        "live_confirmation",
        "approved_strategies_sha256",
        "healthcheck_url",
        "backup_telegram_chat_id",
        "backup_encryption_key",
        mode="before",
    )
    @classmethod
    def _normalize_optional_env(cls, v: Any) -> Any:
        return _empty_to_none(v)

    @field_validator("healthcheck_url")
    @classmethod
    def _validate_healthcheck_url(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        raw = v.get_secret_value().strip()
        if not (raw.startswith("http://") or raw.startswith("https://")):
            raise ValueError("HEALTHCHECK_URL must start with http:// or https://")
        return v

    @field_validator("backup_encryption_key")
    @classmethod
    def _validate_backup_encryption_key(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        from cryptography.fernet import Fernet

        try:
            Fernet(v.get_secret_value().strip().encode("ascii"))
        except Exception as exc:
            raise ValueError(
                "BACKUP_ENCRYPTION_KEY must be a valid URL-safe base64 32-byte Fernet key"
            ) from exc
        return v


# ---- Espelhos estritos dos YAMLs (extra="forbid": chave desconhecida = erro de boot) ----
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PortfolioCfg(_Strict):
    core_hold_asset: str
    core_hold_amount: float
    tactical_quote_asset: str
    fee_reserve_asset: str
    min_bnb_fee_reserve_usdt_equiv: float
    max_concurrent_positions: int
    min_notional_safety_buffer: float
    max_single_order_usdt_cap: float
    max_single_order_equity_pct: float = 0.55
    max_daily_trades_cap: int


class ExecutionResilienceCfg(_Strict):
    clock_sync_interval_minutes: int
    clock_safety_margin_ms: int
    recv_window_ms: int
    candle_close_fetch_delay_seconds: int
    candle_fetch_max_retries: int
    oco_endpoint: str
    oco_below_type: Literal["STOP_LOSS_LIMIT", "STOP_LOSS"]
    oco_stop_limit_buffer_pct: float
    fallback_to_market_on_stop_breach: bool
    sqlite_wal_mode: bool
    daily_sqlite_backup_enabled: bool


class PositionSizingCfg(_Strict):
    mode: str
    default_grade_b_risk_pct: float
    default_grade_b_max_cash_pct: float
    conviction_sizing_enabled: bool
    grade_a_plus_risk_pct: float
    grade_a_plus_max_cash_pct: float
    min_expectancy_margin_over_breakeven: float
    roundtrip_fee_assumption_pct: float
    stress_slippage_assumption_pct: float


class MidTradeManagementCfg(_Strict):
    breakeven_enabled: bool
    breakeven_trigger_r: float
    require_realtime_websocket_for_be: bool


class DrawdownShadowProtectionCfg(_Strict):
    protection_mode: Literal["CPPI_DRAWDOWN_CONTROL", "CUSUM_SHADOW_MODE", "DISABLED"]
    cppi_drawdown_threshold_pct: float
    cppi_risk_reduction_factor: float
    shadow_mode_enabled: bool
    shadow_max_consecutive_live_losses: int
    shadow_virtual_wins_to_reactivate: int


class ProfitSplitterCfg(_Strict):
    enabled: bool
    reinvest_bot_pct: float
    send_to_hodl_pct: float
    min_bucket_to_transfer_usdt: float
    transfer_method: str
    hodl_target_reference_asset: str


class UniverseCfg(_Strict):
    backtest_min_start_date: str
    phase_1_tie_breaker: str
    phase_2_tie_breaker: str
    symbols: tuple[str, ...]


class RegimeThresholds4hCfg(_Strict):
    ema_macro_period: int
    chop_period: int
    chop_bull_max: float
    chop_sideways_min: float
    adx_period: int
    adx_sideways_max: float


class BullTrendPullbackCfg(_Strict):
    enabled: bool
    ema_fast_1h: int
    ema_slow_1h: int
    stop_atr_mult: float
    target_atr_mult: float
    time_barrier_candles_1h: int
    donchian_lookback_1h: int = 12
    min_adx_4h: float = 17.0
    min_rvol_1h: float = 1.30
    min_taker_buy_ratio: float = 0.52
    min_body_atr_1h: float = 0.65
    min_rs_12h: float = 1.40
    require_beat_btc_rs_12h: bool = True
    veto_if_btc_bear: bool = True


class SidewaysPingpongCfg(_Strict):
    enabled: bool
    bollinger_period: int
    bollinger_std: float
    rsi_period: int
    rsi_oversold_entry: float
    stop_atr_mult: float
    target_band: str
    min_initial_rr_ratio: float
    time_barrier_candles_1h: int


class BearCapitulationSnapCfg(_Strict):
    enabled: bool
    rsi_fast_period: int
    rsi_panic_threshold: float
    relative_volume_min: float
    min_lower_wick_ratio: float
    stop_atr_mult: float
    target_atr_mult: float
    time_barrier_candles_1h: int


class RegimesAndStrategiesCfg(_Strict):
    macro_compass_timeframe: Interval
    execution_timeframe: Interval
    intracandle_sim_timeframe: Interval
    regime_thresholds_4h: RegimeThresholds4hCfg
    bull_trend_pullback: BullTrendPullbackCfg
    sideways_pingpong: SidewaysPingpongCfg
    bear_capitulation_snap: BearCapitulationSnapCfg


class MlJudgeValidationCfg(_Strict):
    use_ml_filter: bool
    segregate_models_by_regime: bool
    sample_uniqueness_method: str
    purge_window_hours: int
    embargo_pct: float
    calibration_method: Literal["platt_scaling", "isotonic"]
    max_allowed_ece: float
    ranking_metric: str
    reject_bottom_quantile: float
    grade_a_plus_top_quantile: float
    min_deflated_sharpe_ratio: float


class StrategiesConfig(_Strict):
    version: str
    rollout_phase: Literal["PHASE_1_CORE_VALIDATION"]
    portfolio: PortfolioCfg
    execution_and_resilience: ExecutionResilienceCfg
    position_sizing: PositionSizingCfg
    mid_trade_management: MidTradeManagementCfg
    drawdown_and_shadow_protection: DrawdownShadowProtectionCfg
    profit_splitter: ProfitSplitterCfg
    universe: UniverseCfg
    regimes_and_strategies: RegimesAndStrategiesCfg
    ml_judge_and_validation: MlJudgeValidationCfg


class ScannerUniverseCfg(_Strict):
    macro_timeframe: Interval
    trigger_timeframe: Interval
    scan_delay_after_hour_seconds: int
    symbols: tuple[str, ...]


class Layer1StructureEmaCfg(_Strict):
    ema_fast: int
    ema_mid: int
    ema_slow: int
    chop_period: int
    max_chop_for_trend: float
    pullback_touch_tolerance_atr: float


class Layer2PriceActionCfg(_Strict):
    min_candle_range_atr: float
    pinbar_min_lower_wick_ratio: float
    pinbar_max_upper_wick_ratio: float
    engulfing_min_atr_mult: float
    liquidity_sweep_lookback_candles: int


class Layer3VolumeOrderFlowCfg(_Strict):
    rvol_lookback: int
    min_rvol_for_signal: float
    high_conviction_rvol: float
    min_taker_buy_ratio: float
    strong_taker_buy_ratio: float


class OrderbookLayerCfg(_Strict):
    enabled: bool
    affects_execution: Literal[False] = False
    depth_limit_default: Literal[100, 500, 1000] = 500
    depth_limit_overrides: dict[str, Literal[100, 500, 1000]] = Field(default_factory=dict)
    max_symbols_per_cycle: int = Field(5, ge=1, le=5)
    anti_spoofing_snapshots: Literal[3] = 3
    snapshot_interval_seconds: int = Field(5, ge=3, le=10)
    imbalance_band_pct: float = 0.015
    min_valid_band_pct: float = 0.003
    bullish_obi_threshold: float
    bearish_obi_warning_threshold: float
    whale_wall_multiplier: float
    wall_bucket_pct: float = 0.001


class Layer5ScoringTargetsCfg(_Strict):
    min_score_to_alert_telegram: int
    grade_b_min_score: int
    grade_a_min_score: int
    grade_a_plus_min_score: int
    stop_buffer_below_wick_atr: float
    min_stop_distance_atr: float
    target_1_rr: float
    target_2_rr: float
    adjust_target_before_ask_wall: bool
    time_horizon_hours: int = 24


class VirtualAuditorCfg(_Strict):
    track_all_alerts: bool
    cost_model: Literal["strategies.position_sizing"] = "strategies.position_sizing"
    send_weekly_scorecard_telegram: bool


class AnalystConfig(_Strict):
    version: str
    scanner_universe: ScannerUniverseCfg
    layer_1_structure_ema: Layer1StructureEmaCfg
    layer_2_price_action: Layer2PriceActionCfg
    layer_3_volume_and_order_flow: Layer3VolumeOrderFlowCfg
    layer_4_orderbook_live: OrderbookLayerCfg
    layer_5_scoring_and_targets: Layer5ScoringTargetsCfg
    virtual_auditor: VirtualAuditorCfg


class AppConfig(_Strict):
    runtime: RuntimeSettings
    strategies: StrategiesConfig
    analyst: AnalystConfig
    strategies_sha256: str

    @property
    def execution_universe(self) -> tuple[str, ...]:
        """Execution universe excluding forbidden symbols such as BNBUSDT (C1-C)."""
        return tuple(
            s for s in self.strategies.universe.symbols if s not in FORBIDDEN_EXECUTION_SYMBOLS
        )

    @property
    def market_data_universe(self) -> tuple[str, ...]:
        """Deterministic union of radar, execution, and BTCUSDT symbols."""
        combined = (
            set(self.analyst.scanner_universe.symbols)
            | set(self.execution_universe)
            | {"BTCUSDT"}
        )
        return tuple(sorted(combined))

    @property
    def market_data_base_url(self) -> str:
        """Market data host according to ADR-013 and mode matrix 3.2."""
        if not self.is_live:
            return PUBLIC_MARKET_DATA_URL
        if self.runtime.binance_env == BinanceEnv.TESTNET:
            return TESTNET_TRADING_URL
        return PRODUCTION_TRADING_URL

    @property
    def trading_base_url(self) -> str | None:
        """Trading host (only available in LIVE mode)."""
        if not self.is_live:
            return None
        if self.runtime.binance_env == BinanceEnv.TESTNET:
            return TESTNET_TRADING_URL
        return PRODUCTION_TRADING_URL

    @property
    def is_live(self) -> bool:
        return self.runtime.mode == RunMode.LIVE_CHAMELEON_PHASE_1

    @property
    def total_roundtrip_cost_pct(self) -> float:
        """Single shared cost model across Chameleon and Radar Virtual Auditor (C2-4 / ADR-009)."""
        ps = self.strategies.position_sizing
        return ps.roundtrip_fee_assumption_pct + ps.stress_slippage_assumption_pct


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML SafeLoader that rejects duplicate keys at any mapping level."""


def _construct_mapping_no_dups(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(f"Duplicate YAML key detected: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_dups,
)


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_yaml_strict(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ConfigError(f"YAML file not found: {path}")
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML syntax in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"Expected top-level mapping in {path}")
    return loaded


def validate_mode_invariants(cfg: AppConfig) -> None:
    """Enforce mode invariants (ADR-003, ADR-004, ADR-010, C1-C)."""
    rt = cfg.runtime
    st = cfg.strategies

    has_api_key = (
        rt.binance_api_key is not None
        and bool(rt.binance_api_key.get_secret_value().strip())
    )
    has_priv_key = rt.binance_ed25519_private_key_path is not None

    # 1. ANALYST_ONLY / SHADOW_CHAMELEON must NOT have any Binance private keys configured.
    if rt.mode in (RunMode.ANALYST_ONLY, RunMode.SHADOW_CHAMELEON):
        if has_api_key or has_priv_key:
            raise ConfigError(
                f"Mode {rt.mode} forbids Binance credentials (ADR-010); "
                "remove BINANCE_API_KEY and BINANCE_ED25519_PRIVATE_KEY_PATH."
            )

    # 2. Forbidden symbols (e.g. BNBUSDT) are always banned from strategies.universe.symbols.
    forbidden_found = set(st.universe.symbols) & FORBIDDEN_EXECUTION_SYMBOLS
    if forbidden_found:
        raise ConfigError(
            f"Forbidden symbols present in strategies.universe.symbols (C1-C): "
            f"{sorted(forbidden_found)}"
        )

    # 3. Orderbook layer must never affect execution (ADR-004).
    if cfg.analyst.layer_4_orderbook_live.affects_execution is not False:
        raise ConfigError("layer_4_orderbook_live.affects_execution must be false (ADR-004).")

    # 4. LIVE_CHAMELEON_PHASE_1 strict checks.
    if rt.mode == RunMode.LIVE_CHAMELEON_PHASE_1:
        if not has_api_key:
            raise ConfigError("LIVE_CHAMELEON_PHASE_1 requires BINANCE_API_KEY.")
        if (
            rt.binance_ed25519_private_key_path is None
            or not rt.binance_ed25519_private_key_path.is_file()
        ):
            raise ConfigError(
                "LIVE_CHAMELEON_PHASE_1 requires an existing readable "
                "BINANCE_ED25519_PRIVATE_KEY_PATH."
            )
        try:
            key_text = rt.binance_ed25519_private_key_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"Cannot read Ed25519 private key file: {exc}") from exc
        if not key_text:
            raise ConfigError("Ed25519 private key file is empty.")

        if st.portfolio.max_concurrent_positions != 1:
            raise ConfigError("Phase 1 requires max_concurrent_positions == 1 (Single-Slot).")
        if st.profit_splitter.enabled:
            raise ConfigError("Phase 1 requires profit_splitter.enabled == false.")
        if st.ml_judge_and_validation.use_ml_filter:
            raise ConfigError("Phase 1 requires ml_judge_and_validation.use_ml_filter == false.")
        if st.mid_trade_management.breakeven_enabled:
            raise ConfigError("Phase 1 requires mid_trade_management.breakeven_enabled == false.")
        if st.position_sizing.conviction_sizing_enabled:
            raise ConfigError("Phase 1 requires position_sizing.conviction_sizing_enabled == false.")

        rs = st.regimes_and_strategies
        if not rs.bull_trend_pullback.enabled:
            raise ConfigError("Phase 1 requires bull_trend_pullback.enabled == true.")
        if rs.sideways_pingpong.enabled or rs.bear_capitulation_snap.enabled:
            raise ConfigError(
                "Phase 1 forbids enabling sideways_pingpong or bear_capitulation_snap."
            )

        if rt.binance_env == BinanceEnv.PRODUCTION:
            if rt.live_confirmation != PHASE_1_LIVE_CONFIRMATION:
                raise ConfigError(
                    f"LIVE + PRODUCTION requires LIVE_CONFIRMATION={PHASE_1_LIVE_CONFIRMATION!r}."
                )
            if (
                not rt.approved_strategies_sha256
                or rt.approved_strategies_sha256.lower() != cfg.strategies_sha256.lower()
            ):
                raise ConfigError(
                    "LIVE + PRODUCTION requires APPROVED_STRATEGIES_SHA256 matching "
                    "the exact SHA-256 of config/strategies.yaml."
                )


def load_config(
    env_file: Path = Path(".env"),
    strategies_path: Path = Path("config/strategies.yaml"),
    analyst_path: Path = Path("config/analyst.yaml"),
) -> AppConfig:
    try:
        runtime = RuntimeSettings(_env_file=env_file if env_file.is_file() else None)
        strategies_raw = load_yaml_strict(strategies_path)
        analyst_raw = load_yaml_strict(analyst_path)
        strategies = StrategiesConfig.model_validate(strategies_raw)
        analyst = AnalystConfig.model_validate(analyst_raw)
        strategies_hash = sha256_file(strategies_path)
        cfg = AppConfig(
            runtime=runtime,
            strategies=strategies,
            analyst=analyst,
            strategies_sha256=strategies_hash,
        )
    except ValidationError as exc:
        raise ConfigError(f"Configuration validation error: {exc}") from exc

    validate_mode_invariants(cfg)
    return cfg


def required_kline_intervals(cfg: AppConfig) -> tuple[Interval, ...]:
    _ = cfg
    return ("1h", "4h")
