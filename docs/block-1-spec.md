# Especificação do Bloco 1 unificado

Esta página congela as **assinaturas (type hints) dos 4 arquivos do Bloco 1**
(`config.py`, `database.py`, `binance_client.py` e `candle_collector.py`) e o
**Gate 1 unificado**. Ela incorpora os itens de Categoria 1 e 2 do [TODO](todo.md)
e as emendas C1-B, C1-C e C1-E da [Auditoria de Integração](integration-audit.md).

!!! warning "Regra de avanço"
    Nenhuma linha do Bloco 2 é escrita antes de o Gate 1 (seção 5) retornar `100% PASS`.

## 1. `src/core/config.py`

```python
from __future__ import annotations
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FORBIDDEN_EXECUTION_SYMBOLS: Final[frozenset[str]] = frozenset({"BNBUSDT"})
PHASE_1_LIVE_CONFIRMATION: Final[str] = "PHASE_1_GATES_6A_6B_PASSED"
Interval = Literal["1m", "1h", "4h"]

class ConfigError(RuntimeError): ...

class RunMode(StrEnum):
    ANALYST_ONLY = "ANALYST_ONLY"
    SHADOW_CHAMELEON = "SHADOW_CHAMELEON"
    LIVE_CHAMELEON_PHASE_1 = "LIVE_CHAMELEON_PHASE_1"

class BinanceEnv(StrEnum):
    TESTNET = "TESTNET"
    PRODUCTION = "PRODUCTION"

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

# ---- Espelhos estritos dos YAMLs (extra="forbid": chave desconhecida = erro de boot) ----
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

class PortfolioCfg(_Strict): ...            # campos exatos de strategies.yaml -> portfolio
class ExecutionResilienceCfg(_Strict): ...  # clock_sync_interval_minutes, recv_window_ms, candle_close_fetch_delay_seconds...
class PositionSizingCfg(_Strict): ...       # roundtrip_fee_assumption_pct, stress_slippage_assumption_pct (custo ÚNICO, C2-4)
class UniverseCfg(_Strict):
    backtest_min_start_date: str
    phase_1_tie_breaker: str
    phase_2_tie_breaker: str
    symbols: tuple[str, ...]
class StrategiesConfig(_Strict):            # strategies.yaml completo (portfolio, ..., ml_judge_and_validation)
    version: str
    rollout_phase: Literal["PHASE_1_CORE_VALIDATION"]
    # ... demais seções tipadas

class OrderbookLayerCfg(_Strict):
    enabled: bool
    affects_execution: Literal[False] = False          # TRAVA: Cat.3 do TODO preservada
    depth_limit_default: Literal[100, 500, 1000] = 500
    depth_limit_overrides: dict[str, Literal[100, 500, 1000]] = Field(default_factory=dict)
    max_symbols_per_cycle: int = Field(5, ge=1, le=5)  # k_max (ADR-005)
    anti_spoofing_snapshots: Literal[3] = 3
    snapshot_interval_seconds: int = Field(5, ge=3, le=10)
    imbalance_band_pct: float = 0.015
    min_valid_band_pct: float = 0.003                  # C1-A
    bullish_obi_threshold: float
    bearish_obi_warning_threshold: float
    whale_wall_multiplier: float
class AnalystConfig(_Strict): ...            # analyst.yaml completo; layer_4_orderbook_live: OrderbookLayerCfg

class AppConfig(_Strict):
    runtime: RuntimeSettings
    strategies: StrategiesConfig
    analyst: AnalystConfig
    strategies_sha256: str

    @property
    def execution_universe(self) -> tuple[str, ...]: ...      # strategies.symbols − FORBIDDEN (C1-C)
    @property
    def market_data_universe(self) -> tuple[str, ...]: ...    # ordenado(∪ radar ∪ execução ∪ {"BTCUSDT"})
    @property
    def market_data_base_url(self) -> str: ...                # matriz de modos
    @property
    def trading_base_url(self) -> str | None: ...             # None fora de LIVE
    @property
    def is_live(self) -> bool: ...

def sha256_file(path: Path) -> str: ...
def load_yaml_strict(path: Path) -> dict[str, object]: ...    # yaml.safe_load + rejeita chaves duplicadas
def load_config(
    env_file: Path = Path(".env"),
    strategies_path: Path = Path("config/strategies.yaml"),
    analyst_path: Path = Path("config/analyst.yaml"),
) -> AppConfig: ...
def validate_mode_invariants(cfg: AppConfig) -> None:
    """Levanta ConfigError se:
    - ANALYST_ONLY/SHADOW com qualquer chave Binance presente;
    - LIVE sem api_key ou sem arquivo Ed25519 legível;
    - LIVE com execution_universe ∩ FORBIDDEN_EXECUTION_SYMBOLS ≠ ∅;
    - LIVE com max_concurrent_positions != 1, profit_splitter.enabled, use_ml_filter,
      breakeven_enabled, conviction_sizing_enabled, ou estratégia != bull_trend_pullback habilitada;
    - LIVE+PRODUCTION sem LIVE_CONFIRMATION correto ou com strategies_sha256 != approved_strategies_sha256.
    """
def required_kline_intervals(cfg: AppConfig) -> tuple[Interval, ...]: ...   # ("1h","4h")
```

## 2. `src/core/database.py`

```python
from __future__ import annotations
import sqlite3, threading
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, Iterator, Sequence
import pandas as pd
from src.core.config import Interval

SCHEMA_VERSION: Final[int] = 1
PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000", "PRAGMA wal_autocheckpoint=1000",
)

class DatabaseError(RuntimeError): ...
class WrongThreadError(DatabaseError): ...

@dataclass(frozen=True, slots=True)
class CandleRow:
    symbol: str; interval: Interval
    open_time_ms: int; close_time_ms: int
    open: float; high: float; low: float; close: float
    volume: float; quote_volume: float; number_of_trades: int
    taker_buy_base_volume: float; taker_buy_quote_volume: float
    ingested_at_ms: int

@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str; status: str; base_asset: str; quote_asset: str
    tick_size: Decimal; step_size: Decimal; min_qty: Decimal; min_notional: Decimal
    oco_allowed: bool; fetched_at_ms: int

@dataclass(frozen=True, slots=True)
class CycleHealth:
    cycle_close_time_ms: int; mode: str; clock_offset_ms: float; clock_rtt_ms: float
    used_weight_1m_peak: int; stale_symbols: tuple[str, ...]; gaps_detected: int
    duration_ms: int; unhandled_errors: int

class Database:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 10_000) -> None: ...
    def connect(self) -> None: ...                 # isolation_level=None; aplica PRAGMAS; assert journal_mode=='wal'
    def close(self) -> None: ...                   # PRAGMA wal_checkpoint(TRUNCATE) + close
    def migrate(self) -> int: ...                  # DDL da Seção 3.3 idempotente; retorna versão
    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]: ...  # BEGIN IMMEDIATE; COMMIT/ROLLBACK; WrongThreadError
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]: ...

    # --- candles ---
    def upsert_candles(self, rows: Sequence[CandleRow]) -> int: ...           # INSERT ... ON CONFLICT DO UPDATE
    def last_open_time(self, symbol: str, interval: Interval) -> int | None: ...
    def load_candles(self, symbol: str, interval: Interval, *, limit: int,
                     until_close_time_ms: int | None = None) -> pd.DataFrame: ...  # ordenado asc, float64
    def close_times(self, symbol: str, interval: Interval, since_ms: int) -> list[int]: ...

    # --- exchange filters ---
    def upsert_exchange_filters(self, filters: Sequence[SymbolFilters]) -> None: ...
    def get_exchange_filters(self, symbol: str) -> SymbolFilters | None: ...

    # --- estado / saúde ---
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str | Decimal) -> None: ...
    def log_cycle_health(self, h: CycleHealth) -> None: ...

    # --- backup (TODO Cat.1) ---
    def backup_to(self, dest_dir: Path) -> Path: ...   # src.backup(dst, pages=-1) num passo; retorna arquivo
    @staticmethod
    def verify_backup(path: Path) -> bool: ...         # PRAGMA integrity_check == 'ok' + schema_meta.version
```

!!! note "DAOs"
    DAOs de `trades`, `shadow_trades`, `virtual_signals` e `orderbook_*` entram nos blocos que os consomem. **O schema inteiro, porém, é criado já no Bloco 1**, porque o contrato de dados fica congelado desde o início.

## 3. `src/core/binance_client.py`

```python
from __future__ import annotations
import threading, time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Final, Iterator, Literal, Mapping, Sequence
import requests
from src.core.config import AppConfig, Interval
from src.core.database import SymbolFilters

# ---------------- Pesos e governador (C1-B) ----------------
ENDPOINT_WEIGHTS: Final[dict[str, int]] = {
    "GET /api/v3/time": 1, "GET /api/v3/klines": 2, "GET /api/v3/exchangeInfo": 20,
    "GET /api/v3/ticker/bookTicker": 2, "GET /api/v3/account": 20,
    "GET /api/v3/account/commission": 20, "GET /api/v3/order": 4,
    "GET /api/v3/orderList": 4, "GET /api/v3/openOrderList": 6,
    "POST /api/v3/order": 1, "POST /api/v3/orderList/oco": 1, "DELETE /api/v3/orderList": 1,
}
def depth_weight(limit: int) -> int: ...          # 5 / 25 / 50 / 250

class Priority(IntEnum):
    CRITICAL = 0   # ordens, OCO, cancelamentos, reconciliação
    HIGH = 1       # klines, exchangeInfo, time, bookTicker
    LOW = 2        # depth (Radar L4)

class BinanceAPIError(RuntimeError):
    def __init__(self, http_status: int, code: int | None, msg: str, endpoint: str) -> None: ...
class RateLimitedError(BinanceAPIError): ...      # 429
class IPBannedError(BinanceAPIError): ...         # 418
class WeightBudgetExceeded(RuntimeError): ...     # recusa LOCAL (não chegou à Binance)
class RequestTimeoutUnknownState(RuntimeError): ...   # timeout em POST: estado desconhecido (-1007)

class WeightGovernor:
    def __init__(self, limit_per_min: int = 6000, *, high_ceiling: float = 0.75,
                 low_ceiling: float = 0.50, now: Callable[[], float] = time.monotonic) -> None: ...
    def acquire(self, weight: int, priority: Priority) -> None: ...   # levanta WeightBudgetExceeded
    def update_from_headers(self, headers: Mapping[str, str]) -> None: ...  # X-MBX-USED-WEIGHT-1M
    def on_rate_limited(self, retry_after_s: float, banned: bool) -> None: ...
    @contextmanager
    def critical_section(self) -> Iterator[None]: ...   # bloqueia HIGH/LOW entre o fill e a OCO confirmada
    @property
    def used_weight_1m(self) -> int: ...
    @property
    def peak_weight_1m(self) -> int: ...
    def reset_peak(self) -> int: ...

# ---------------- Relógio monotônico (Cristian RTT/2) ----------------
@dataclass(frozen=True, slots=True)
class ClockSample:
    offset_ms: float; rtt_ms: float; sampled_at_mono: float; wall_offset_ms: float = 0.0

class MonotonicClock:
    def __init__(self, safety_margin_ms: int = 200, *, mono: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None: ...
    def sync(self, fetch_server_time_ms: Callable[[], int], samples: int = 3) -> ClockSample: ...  # menor RTT vence
    def server_now_ms(self) -> int: ...                 # estimativa do relógio da Binance
    def signing_timestamp_ms(self) -> int: ...          # floor(server_now - safety_margin)
    @property
    def last_sample(self) -> ClockSample | None: ...

# ---------------- Aritmética Decimal (Gate 1b) ----------------
def to_decimal(x: str | int | Decimal) -> Decimal: ...          # proíbe float
def round_price_to_tick(price: Decimal, tick: Decimal, direction: Literal["DOWN", "UP"]) -> Decimal: ...
def floor_qty_to_step(qty: Decimal, step: Decimal) -> Decimal: ...
def to_api_str(x: Decimal) -> str: ...                          # format(x, 'f'), sem notação científica
def passes_filters(price: Decimal, qty: Decimal, f: SymbolFilters) -> tuple[bool, str | None]: ...

# ---------------- DTOs ----------------
@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str; bid: Decimal; bid_qty: Decimal; ask: Decimal; ask_qty: Decimal

@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    symbol: str; last_update_id: int; taken_at_ms: int; requested_limit: int
    bids: tuple[tuple[float, float], ...]; asks: tuple[tuple[float, float], ...]   # análise apenas (float ok)

@dataclass(frozen=True, slots=True)
class SystemStatus:
    normal: bool; msg: str; mocked: bool

# ---------------- Clientes ----------------
class PublicBinanceClient:
    def __init__(self, base_url: str, governor: WeightGovernor, *,
                 session: requests.Session | None = None,
                 timeout_s: tuple[float, float] = (3.05, 10.0)) -> None: ...
    def _request(self, method: str, path: str, params: Mapping[str, Any] | None,
                 *, weight: int, priority: Priority) -> Any: ...   # governor + headers + mapeia 429/418/-1021
    def get_server_time_ms(self) -> int: ...
    def get_klines(self, symbol: str, interval: Interval, *, start_time_ms: int | None = None,
                   end_time_ms: int | None = None, limit: int = 1000) -> list[list[Any]]: ...
    def get_depth(self, symbol: str, limit: int = 500) -> DepthSnapshot: ...       # Priority.LOW, sem retry
    def get_book_ticker(self, symbol: str, *, priority: Priority = Priority.HIGH) -> BookTicker: ...
    def get_exchange_info(self, symbols: Sequence[str]) -> dict[str, SymbolFilters]: ...

class ExchangeInfoCache:
    def __init__(self, client: PublicBinanceClient, db: "Database", symbols: Sequence[str],
                 max_age_s: int = 3600) -> None: ...
    def refresh_if_stale(self, now_ms: int) -> bool: ...
    def get(self, symbol: str) -> SymbolFilters: ...   # KeyError => símbolo não negociável

class SignedBinanceClient(PublicBinanceClient):
    def __init__(self, base_url: str, governor: WeightGovernor, clock: MonotonicClock, *,
                 api_key: str, ed25519_private_key_path: Path, recv_window_ms: int = 5000,
                 testnet: bool, session: requests.Session | None = None) -> None: ...
    def _sign(self, query_string: str) -> str: ...                  # Ed25519 -> base64
    def signed_request(self, method: Literal["GET", "POST", "DELETE"], path: str,
                       params: Mapping[str, Any], *, weight: int,
                       priority: Priority = Priority.CRITICAL) -> Any: ...
        # timeout em POST => RequestTimeoutUnknownState (NUNCA reenvia; quem chama consulta origClientOrderId)
    def get_account(self, *, omit_zero_balances: bool = True) -> dict[str, Any]: ...
    def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]: ...
    def get_open_order_lists(self) -> list[dict[str, Any]]: ...
    def get_account_commission(self, symbol: str) -> dict[str, Any]: ...
    # /sapi/*: mock automático quando testnet=True (TODO Cat.2)
    def sapi_system_status(self) -> SystemStatus: ...
    def sapi_transfer_main_to_funding(self, asset: str, amount: Decimal) -> dict[str, Any]: ...
    # Wrappers de ordem (assinaturas congeladas agora; corpo implementado no Bloco 5):
    def new_market_buy_quote(self, symbol: str, quote_qty: Decimal, client_order_id: str) -> dict[str, Any]: ...
    def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]: ...
    def new_oco_sell(self, symbol: str, qty: Decimal, take_profit: Decimal, stop_price: Decimal,
                     stop_limit_price: Decimal | None, list_client_order_id: str) -> dict[str, Any]: ...
    def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]: ...
    def get_order_list(self, order_list_id: int) -> dict[str, Any]: ...

def build_clients(cfg: AppConfig) -> tuple[PublicBinanceClient, SignedBinanceClient | None,
                                           WeightGovernor, MonotonicClock]: ...
    # Um único WeightGovernor compartilhado. SignedBinanceClient só é construído em LIVE.
```

## 4. `src/data/candle_collector.py`

```python
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Sequence
import pandas as pd
from src.core.binance_client import MonotonicClock, PublicBinanceClient
from src.core.config import Interval
from src.core.database import CandleRow, Database

INTERVAL_MS: Final[dict[str, int]] = {"1m": 60_000, "1h": 3_600_000, "4h": 14_400_000}

class KlineIntegrityError(ValueError): ...

def normalize_close_time(raw_close_time_ms: int, open_time_ms: int, interval: Interval) -> int: ...
    # retorna raw+1 e valida == open_time + INTERVAL_MS (TODO Cat.1)
def parse_kline(raw: Sequence[Any], symbol: str, interval: Interval, ingested_at_ms: int) -> CandleRow: ...
    # cols 0..10 (inclui 7 quote_volume, 8 trades, 9 taker_buy_base, 10 taker_buy_quote)
def last_closed_close_time(server_now_ms: int, interval: Interval) -> int: ...  # floor(now/Δ)*Δ
def is_interval_boundary(close_time_ms: int, interval: Interval) -> bool: ...
def drop_unclosed(rows: Sequence[CandleRow], server_now_ms: int) -> list[CandleRow]: ...  # close_time <= now

@dataclass(slots=True)
class CollectionReport:
    interval: Interval
    expected_close_time_ms: int
    ok: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)          # excluídos DESTE ciclo (sem sinal), não derrubam o loop
    gaps: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    retries_used: int = 0

class CandleCollector:
    def __init__(self, client: PublicBinanceClient, db: Database, clock: MonotonicClock, *,
                 fetch_delay_s: float = 3.0, max_retries: int = 5, retry_sleep_s: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep) -> None: ...
    def wait_for_cycle(self) -> int: ...
        # dorme até XX:00:03 (relógio da Binance); retorna o close_time esperado (XX:00:00.000)
    def backfill(self, symbols: Sequence[str], interval: Interval, *, lookback_candles: int = 1000) -> dict[str, int]: ...
    def collect_closed(self, symbols: Sequence[str], interval: Interval, *,
                       expected_close_time_ms: int) -> CollectionReport: ...
        # busca limit=3, descarta a vela em formação, confere se a última fechada == esperada; retry 1s x5
    def detect_gaps(self, symbol: str, interval: Interval, since_ms: int) -> list[tuple[int, int]]: ...
    def repair_gaps(self, symbol: str, interval: Interval, gaps: Sequence[tuple[int, int]]) -> int: ...
    def run_cycle(self, symbols: Sequence[str]) -> list[CollectionReport]: ...  # 1h sempre; 4h na fronteira

def load_frame(db: Database, symbol: str, interval: Interval, *, limit: int,
               until_close_time_ms: int | None = None) -> pd.DataFrame: ...
def align_4h_to_1h(df_1h: pd.DataFrame, df_4h: pd.DataFrame, *, suffix: str = "_4h") -> pd.DataFrame: ...
    # pd.merge_asof(df_1h, df_4h, on="close_time_ms", direction="backward", allow_exact_matches=True)
def align_btc_context(df_asset: pd.DataFrame, df_btc: pd.DataFrame) -> pd.DataFrame: ...
    # close_time_btc <= close_time_asset (mesmo merge_asof backward)
```

## 5. Gate 1 Unificado (critério binário)

`pytest tests/test_block1_causal_and_decimal.py tests/test_block1_db_governor_modes.py` precisa passar em **100%**:

| # | Teste | Origem |
|---|---|---|
| a | Truncamento às 02:00 UTC gera o vetor alinhado idêntico ao da tabela completa (zero vazamento); às 04:00:00.000 enxerga a vela de 4h recém-fechada | Roadmap |
| b | 10.000 preços e lotes aleatórios saem múltiplos exatos de `tickSize`/`stepSize` via `Decimal`; `to_api_str` nunca emite `E-` | Roadmap |
| c | Restauração real de um arquivo de `backup_to()` com `integrity_check == ok` | Roadmap |
| d | `WeightGovernor`: com 3.100 de peso usado, `LOW` é recusado, `HIGH` passa e `CRITICAL` passa; dentro de `critical_section()`, `HIGH` é recusado | [C1-B](integration-audit.md#c1-b) |
| e | `validate_mode_invariants`: LIVE com `BNBUSDT` em `strategies.symbols` levanta `ConfigError`; ANALYST_ONLY com API key levanta `ConfigError`; `affects_execution: true` no YAML levanta erro de validação | [C1-C](integration-audit.md#c1-c) |
| f | Segunda linha de `trades` com status aberto viola `ux_trades_single_slot`; `write_tx` chamado de outra thread levanta `WrongThreadError`; um leitor com transação longa não bloqueia `write_tx` | [C1-E](integration-audit.md#c1-e) / ADR-002 |
| g | `parse_kline` persiste as colunas 7–10; `close_time` normalizado é igual a `open_time + Δ`; a vela em formação é descartada | TODO + C2-5 |

!!! tip "Ordem de codificação"
    Ordem de codificação: `config.py` → `database.py` (com `migrate()` criando o schema do [Contrato de Dados](data-contract.md)) → `binance_client.py` (primeiro funções puras de Decimal e o governador, que são 100% testáveis offline; depois o relógio e o HTTP) → `candle_collector.py`. Os testes **a, b, d, e, f e g rodam sem rede**. Só o smoke test opcional consulta a API pública.
