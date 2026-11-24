from __future__ import annotations

import base64
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Final, Iterator, Literal, Mapping, Sequence
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from src.core.config import AppConfig, BinanceEnv, ConfigError, Interval
from src.core.database import Database, SymbolFilters

# ---------------- Pesos e governador (C1-B) ----------------
ENDPOINT_WEIGHTS: Final[dict[str, int]] = {
    "GET /api/v3/time": 1,
    "GET /api/v3/klines": 2,
    "GET /api/v3/exchangeInfo": 20,
    "GET /api/v3/ticker/bookTicker": 2,
    "GET /api/v3/account": 20,
    "GET /api/v3/account/commission": 20,
    "GET /api/v3/order": 4,
    "GET /api/v3/orderList": 4,
    "GET /api/v3/openOrderList": 6,
    "GET /api/v3/myTrades": 5,
    "POST /api/v3/order": 1,
    "POST /api/v3/orderList/oco": 1,
    "DELETE /api/v3/orderList": 1,
}


def depth_weight(limit: int) -> int:
    if limit <= 0:
        raise ValueError(f"depth limit must be > 0, got {limit}")
    if limit <= 100:
        return 5
    if limit <= 500:
        return 25
    if limit <= 1000:
        return 50
    return 250


class Priority(IntEnum):
    CRITICAL = 0  # ordens, OCO, cancelamentos, reconciliação
    HIGH = 1      # klines, exchangeInfo, time, bookTicker
    LOW = 2       # depth (Radar L4)


class BinanceAPIError(RuntimeError):
    def __init__(
        self,
        http_status: int,
        code: int | None,
        msg: str,
        endpoint: str,
    ) -> None:
        self.http_status = int(http_status)
        self.code = int(code) if code is not None else None
        self.msg = str(msg)
        self.endpoint = str(endpoint)
        super().__init__(
            f"Binance API error on {endpoint}: HTTP {http_status} (code={code}): {msg}"
        )


class RateLimitedError(BinanceAPIError):
    """Raised when Binance returns HTTP 429 (Too Many Requests)."""

    def __init__(
        self,
        http_status: int,
        code: int | None,
        msg: str,
        endpoint: str,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        self.retry_after_s = float(retry_after_s) if retry_after_s is not None else None
        super().__init__(http_status, code, msg, endpoint)


class IPBannedError(BinanceAPIError):
    """Raised when Binance returns HTTP 418 (IP auto-banned)."""

    def __init__(
        self,
        http_status: int,
        code: int | None,
        msg: str,
        endpoint: str,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        self.retry_after_s = float(retry_after_s) if retry_after_s is not None else None
        super().__init__(http_status, code, msg, endpoint)


class WeightBudgetExceeded(RuntimeError):
    """Raised locally by WeightGovernor before sending a request that breaches its tier budget."""


class RequestTimeoutUnknownState(RuntimeError):
    """Raised when a signed POST times out (-1007): order state is unknown; caller MUST query origClientOrderId."""


class WeightGovernor:
    def __init__(
        self,
        limit_per_min: int = 6000,
        *,
        high_ceiling: float = 0.75,
        low_ceiling: float = 0.50,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit_per_min = int(limit_per_min)
        self.high_ceiling = float(high_ceiling)
        self.low_ceiling = float(low_ceiling)
        self._now = now
        self._lock = threading.RLock()
        self._used_weight_1m: int = 0
        self._peak_weight_1m: int = 0
        self._window_started_at: float = self._now()
        self._critical_depth: int = 0
        self._rate_limit_until: float = 0.0
        self._non_critical_suspended_until: float = 0.0
        self._ip_banned_until: float = 0.0

    def _roll_window_unlocked(self, now_s: float) -> None:
        if (now_s - self._window_started_at) >= 60.0:
            self._window_started_at = now_s
            self._used_weight_1m = 0

    def acquire(self, weight: int, priority: Priority) -> None:
        w = int(weight)
        if w < 0:
            raise ValueError(f"weight must be >= 0, got {w}")
        with self._lock:
            now_s = self._now()
            self._roll_window_unlocked(now_s)

            if now_s < self._ip_banned_until:
                raise WeightBudgetExceeded(
                    f"IP ban active for another {self._ip_banned_until - now_s:.1f}s."
                )

            if priority != Priority.CRITICAL:
                if self._critical_depth > 0:
                    raise WeightBudgetExceeded(
                        f"Priority {priority.name} blocked during critical_section() (C1-B)."
                    )
                if now_s < self._non_critical_suspended_until:
                    raise WeightBudgetExceeded(
                        f"Priority {priority.name} suspended after 429 cooldown."
                    )

            projected = self._used_weight_1m + w
            if priority == Priority.LOW:
                max_allowed = int(self.limit_per_min * self.low_ceiling)
                if projected > max_allowed:
                    raise WeightBudgetExceeded(
                        f"LOW priority budget exceeded: projected={projected} > {max_allowed}"
                    )
            elif priority == Priority.HIGH:
                max_allowed = int(self.limit_per_min * self.high_ceiling)
                if projected > max_allowed:
                    raise WeightBudgetExceeded(
                        f"HIGH priority budget exceeded: projected={projected} > {max_allowed}"
                    )

            if self._used_weight_1m == 0 and projected > 0:
                self._window_started_at = now_s
            self._used_weight_1m = projected
            if self._used_weight_1m > self._peak_weight_1m:
                self._peak_weight_1m = self._used_weight_1m

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        with self._lock:
            now_s = self._now()
            self._roll_window_unlocked(now_s)
            for k, v in headers.items():
                if k.upper() == "X-MBX-USED-WEIGHT-1M":
                    try:
                        reported = int(v)
                    except ValueError:
                        return
                    if reported < self._used_weight_1m:
                        # Binance UTC minute rolled over: trust authoritative lower server header.
                        self._used_weight_1m = reported
                        self._window_started_at = now_s
                    else:
                        if self._used_weight_1m == 0 and reported > 0:
                            self._window_started_at = now_s
                        self._used_weight_1m = max(self._used_weight_1m, reported)
                    if self._used_weight_1m > self._peak_weight_1m:
                        self._peak_weight_1m = self._used_weight_1m
                    return

    def on_rate_limited(self, retry_after_s: float, banned: bool) -> None:
        cooldown = max(float(retry_after_s), 1.0)
        with self._lock:
            now_s = self._now()
            self._rate_limit_until = max(self._rate_limit_until, now_s + cooldown)
            if banned:
                self._ip_banned_until = max(self._ip_banned_until, now_s + cooldown)
            self._non_critical_suspended_until = max(
                self._non_critical_suspended_until,
                now_s + 2.0 * cooldown,
            )

    @property
    def retry_after_remaining_s(self) -> float:
        with self._lock:
            return max(0.0, self._rate_limit_until - self._now())

    @contextmanager
    def critical_section(self) -> Iterator[None]:
        with self._lock:
            self._critical_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._critical_depth = max(0, self._critical_depth - 1)

    @property
    def used_weight_1m(self) -> int:
        with self._lock:
            self._roll_window_unlocked(self._now())
            return self._used_weight_1m

    @property
    def peak_weight_1m(self) -> int:
        with self._lock:
            return self._peak_weight_1m

    def reset_peak(self) -> int:
        with self._lock:
            prev = self._peak_weight_1m
            self._peak_weight_1m = self._used_weight_1m
            return prev


# ---------------- Relógio monotônico (Cristian RTT/2) ----------------
@dataclass(frozen=True, slots=True)
class ClockSample:
    offset_ms: float
    rtt_ms: float
    sampled_at_mono: float
    wall_offset_ms: float = 0.0


class MonotonicClock:
    def __init__(
        self,
        safety_margin_ms: int = 200,
        *,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.safety_margin_ms = int(safety_margin_ms)
        self._mono = mono
        self._wall = wall
        self._last_sample: ClockSample | None = None

    def sync(
        self,
        fetch_server_time_ms: Callable[[], int],
        samples: int = 3,
    ) -> ClockSample:
        if samples < 1:
            raise ValueError("samples must be >= 1")
        best: ClockSample | None = None
        for _ in range(samples):
            t0 = self._mono() * 1000.0
            w0 = self._wall() * 1000.0
            server_ms = float(fetch_server_time_ms())
            t1 = self._mono() * 1000.0
            w1 = self._wall() * 1000.0
            rtt_ms = max(0.0, t1 - t0)
            midpoint_mono_ms = t0 + rtt_ms / 2.0
            midpoint_wall_ms = (w0 + w1) / 2.0
            offset_ms = server_ms - midpoint_mono_ms
            wall_offset_ms = server_ms - midpoint_wall_ms
            sample = ClockSample(
                offset_ms=offset_ms,
                rtt_ms=rtt_ms,
                sampled_at_mono=t1 / 1000.0,
                wall_offset_ms=wall_offset_ms,
            )
            if best is None or sample.rtt_ms < best.rtt_ms:
                best = sample
        assert best is not None
        self._last_sample = best
        return best

    def server_now_ms(self) -> int:
        if self._last_sample is None:
            return int(math.floor(self._wall() * 1000.0))
        now_mono_ms = self._mono() * 1000.0
        return int(math.floor(now_mono_ms + self._last_sample.offset_ms))

    def signing_timestamp_ms(self) -> int:
        if self._last_sample is None:
            return int(math.floor(self._wall() * 1000.0 - self.safety_margin_ms))
        now_mono_ms = self._mono() * 1000.0
        return int(
            math.floor(now_mono_ms + self._last_sample.offset_ms - self.safety_margin_ms)
        )

    @property
    def last_sample(self) -> ClockSample | None:
        return self._last_sample


# ---------------- Aritmética Decimal (Gate 1b) ----------------
def to_decimal(x: str | int | Decimal) -> Decimal:
    if isinstance(x, float) or isinstance(x, bool):
        raise TypeError(
            f"Binary float/bool is forbidden in order arithmetic; pass str, int, or Decimal (got {type(x).__name__})."
        )
    if isinstance(x, Decimal):
        if not x.is_finite():
            raise ValueError(f"Non-finite Decimal is not allowed: {x}")
        return x
    if isinstance(x, (str, int)):
        try:
            d = Decimal(str(x))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid Decimal literal: {x!r}") from exc
        if not d.is_finite():
            raise ValueError(f"Non-finite Decimal is not allowed: {d}")
        return d
    raise TypeError(f"Unsupported type for to_decimal: {type(x).__name__}")


def round_price_to_tick(
    price: Decimal,
    tick: Decimal,
    direction: Literal["DOWN", "UP"],
) -> Decimal:
    p = to_decimal(price)
    t = to_decimal(tick)
    if t <= Decimal("0"):
        raise ValueError(f"tick must be > 0, got {t}")
    if p < Decimal("0"):
        raise ValueError(f"price must be >= 0, got {p}")
    rounding = ROUND_DOWN if direction == "DOWN" else ROUND_UP
    steps = (p / t).to_integral_value(rounding=rounding)
    return (steps * t).quantize(t)


def floor_qty_to_step(qty: Decimal, step: Decimal) -> Decimal:
    q = to_decimal(qty)
    s = to_decimal(step)
    if s <= Decimal("0"):
        raise ValueError(f"step must be > 0, got {s}")
    if q < Decimal("0"):
        raise ValueError(f"qty must be >= 0, got {q}")
    steps = (q / s).to_integral_value(rounding=ROUND_DOWN)
    return (steps * s).quantize(s)


def to_api_str(x: Decimal) -> str:
    d = to_decimal(x)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def passes_filters(
    price: Decimal,
    qty: Decimal,
    f: SymbolFilters,
) -> tuple[bool, str | None]:
    p = to_decimal(price)
    q = to_decimal(qty)
    if p <= Decimal("0"):
        return False, "PRICE_NON_POSITIVE"
    if q <= Decimal("0"):
        return False, "QTY_NON_POSITIVE"
    if p % f.tick_size != Decimal("0"):
        return False, "PRICE_FILTER_TICK_SIZE"
    if q % f.step_size != Decimal("0"):
        return False, "LOT_SIZE_STEP_SIZE"
    if q < f.min_qty:
        return False, "LOT_SIZE_MIN_QTY"
    if (p * q) < f.min_notional:
        return False, "MIN_NOTIONAL"
    return True, None


# ---------------- DTOs ----------------
@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    bid: Decimal
    bid_qty: Decimal
    ask: Decimal
    ask_qty: Decimal


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    symbol: str
    last_update_id: int
    taken_at_ms: int
    requested_limit: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class SystemStatus:
    normal: bool
    msg: str
    mocked: bool


# ---------------- Clientes ----------------
class PublicBinanceClient:
    def __init__(
        self,
        base_url: str,
        governor: WeightGovernor,
        *,
        session: requests.Session | None = None,
        timeout_s: tuple[float, float] = (3.05, 10.0),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.governor = governor
        self._session = session if session is not None else requests.Session()
        self.timeout_s = timeout_s

    def _parse_error_payload(self, resp: requests.Response) -> tuple[int | None, str]:
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                code = payload.get("code")
                msg = payload.get("msg", resp.text)
                return (int(code) if code is not None else None), str(msg)
        except ValueError:
            pass
        return None, resp.text or f"HTTP {resp.status_code}"

    def _handle_response(self, resp: requests.Response, endpoint: str) -> Any:
        self.governor.update_from_headers(resp.headers)
        if resp.status_code in (429, 418):
            retry_after = 60.0
            for k, v in resp.headers.items():
                if k.lower() == "retry-after":
                    try:
                        retry_after = float(v)
                    except ValueError:
                        pass
                    break
            banned = resp.status_code == 418
            self.governor.on_rate_limited(retry_after, banned=banned)
            code, msg = self._parse_error_payload(resp)
            if banned:
                raise IPBannedError(
                    resp.status_code, code, msg, endpoint, retry_after_s=retry_after
                )
            raise RateLimitedError(
                resp.status_code, code, msg, endpoint, retry_after_s=retry_after
            )

        if resp.status_code >= 400:
            code, msg = self._parse_error_payload(resp)
            raise BinanceAPIError(resp.status_code, code, msg, endpoint)

        if not resp.content:
            return {}
        return resp.json()

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        *,
        weight: int,
        priority: Priority,
    ) -> Any:
        endpoint = f"{method.upper()} {path}"
        self.governor.acquire(weight, priority)
        url = f"{self.base_url}{path}"
        resp = self._session.request(
            method=method.upper(),
            url=url,
            params=dict(params) if params else None,
            timeout=self.timeout_s,
        )
        return self._handle_response(resp, endpoint)

    def get_server_time_ms(self, *, priority: Priority = Priority.HIGH) -> int:
        data = self._request(
            "GET",
            "/api/v3/time",
            None,
            weight=ENDPOINT_WEIGHTS["GET /api/v3/time"],
            priority=priority,
        )
        return int(data["serverTime"])

    def get_klines(
        self,
        symbol: str,
        interval: Interval,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": int(limit),
        }
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        data = self._request(
            "GET",
            "/api/v3/klines",
            params,
            weight=ENDPOINT_WEIGHTS["GET /api/v3/klines"],
            priority=Priority.HIGH,
        )
        return list(data)

    def get_depth(self, symbol: str, limit: int = 500) -> DepthSnapshot:
        w = depth_weight(limit)
        data = self._request(
            "GET",
            "/api/v3/depth",
            {"symbol": symbol, "limit": int(limit)},
            weight=w,
            priority=Priority.LOW,
        )
        taken_ms = int(time.time() * 1000)
        bids = tuple((float(p), float(q)) for p, q in data.get("bids", []))
        asks = tuple((float(p), float(q)) for p, q in data.get("asks", []))
        return DepthSnapshot(
            symbol=symbol,
            last_update_id=int(data["lastUpdateId"]),
            taken_at_ms=taken_ms,
            requested_limit=int(limit),
            bids=bids,
            asks=asks,
        )

    def get_book_ticker(
        self,
        symbol: str,
        *,
        priority: Priority = Priority.HIGH,
    ) -> BookTicker:
        data = self._request(
            "GET",
            "/api/v3/ticker/bookTicker",
            {"symbol": symbol},
            weight=ENDPOINT_WEIGHTS["GET /api/v3/ticker/bookTicker"],
            priority=priority,
        )
        return BookTicker(
            symbol=str(data["symbol"]),
            bid=to_decimal(str(data["bidPrice"])),
            bid_qty=to_decimal(str(data["bidQty"])),
            ask=to_decimal(str(data["askPrice"])),
            ask_qty=to_decimal(str(data["askQty"])),
        )

    def get_exchange_info(self, symbols: Sequence[str]) -> dict[str, SymbolFilters]:
        import json as _json

        sym_list = list(dict.fromkeys(symbols))
        params = {"symbols": _json.dumps(sym_list, separators=(",", ":"))} if sym_list else None
        data = self._request(
            "GET",
            "/api/v3/exchangeInfo",
            params,
            weight=ENDPOINT_WEIGHTS["GET /api/v3/exchangeInfo"],
            priority=Priority.HIGH,
        )
        fetched_ms = int(data.get("serverTime") or int(time.time() * 1000))
        out: dict[str, SymbolFilters] = {}
        for item in data.get("symbols", []):
            sym = str(item["symbol"])
            tick_size = Decimal("0.00000001")
            step_size = Decimal("0.00000001")
            min_qty = Decimal("0")
            min_notional = Decimal("0")
            for flt in item.get("filters", []):
                ftype = flt.get("filterType")
                if ftype == "PRICE_FILTER":
                    tick_size = to_decimal(str(flt["tickSize"]))
                elif ftype == "LOT_SIZE":
                    step_size = to_decimal(str(flt["stepSize"]))
                    min_qty = to_decimal(str(flt["minQty"]))
                elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                    raw_notional = flt.get("minNotional")
                    if raw_notional is not None:
                        min_notional = to_decimal(str(raw_notional))
            out[sym] = SymbolFilters(
                symbol=sym,
                status=str(item.get("status", "TRADING")),
                base_asset=str(item.get("baseAsset", "")),
                quote_asset=str(item.get("quoteAsset", "")),
                tick_size=tick_size,
                step_size=step_size,
                min_qty=min_qty,
                min_notional=min_notional,
                oco_allowed=bool(item.get("ocoAllowed", True)),
                fetched_at_ms=fetched_ms,
            )
        return out


class ExchangeInfoCache:
    def __init__(
        self,
        client: PublicBinanceClient,
        db: Database,
        symbols: Sequence[str],
        max_age_s: int = 3600,
    ) -> None:
        self._client = client
        self._db = db
        self._symbols = tuple(dict.fromkeys(symbols))
        self._max_age_ms = int(max_age_s) * 1000
        self._cache: dict[str, SymbolFilters] = {}
        self._last_refresh_ms: int | None = None
        for sym in self._symbols:
            persisted = self._db.get_exchange_filters(sym)
            if persisted is not None:
                self._cache[sym] = persisted
                if (
                    self._last_refresh_ms is None
                    or persisted.fetched_at_ms < self._last_refresh_ms
                ):
                    self._last_refresh_ms = persisted.fetched_at_ms

    def refresh_if_stale(self, now_ms: int) -> bool:
        missing = any(sym not in self._cache for sym in self._symbols)
        stale = (
            self._last_refresh_ms is None
            or (int(now_ms) - self._last_refresh_ms) >= self._max_age_ms
        )
        if not missing and not stale:
            return False
        fresh = self._client.get_exchange_info(self._symbols)
        self._db.upsert_exchange_filters(list(fresh.values()))
        self._cache.update(fresh)
        self._last_refresh_ms = int(now_ms)
        return True

    def get(self, symbol: str) -> SymbolFilters:
        if symbol in self._cache:
            return self._cache[symbol]
        persisted = self._db.get_exchange_filters(symbol)
        if persisted is not None:
            self._cache[symbol] = persisted
            return persisted
        raise KeyError(f"Symbol filters not found in ExchangeInfoCache: {symbol}")


def _load_ed25519_key(path: Path) -> Ed25519PrivateKey:
    raw = path.read_bytes()
    loaded = load_pem_private_key(raw, password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ConfigError(f"Expected Ed25519 private key in {path}, got {type(loaded).__name__}")
    return loaded


class SignedBinanceClient(PublicBinanceClient):
    def __init__(
        self,
        base_url: str,
        governor: WeightGovernor,
        clock: MonotonicClock,
        *,
        api_key: str,
        ed25519_private_key_path: Path,
        recv_window_ms: int = 5000,
        testnet: bool,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(base_url, governor, session=session)
        self.clock = clock
        self._api_key = str(api_key)
        self._private_key = _load_ed25519_key(Path(ed25519_private_key_path))
        self.recv_window_ms = int(recv_window_ms)
        self.testnet = bool(testnet)

    def _sign(self, query_string: str) -> str:
        sig_bytes = self._private_key.sign(query_string.encode("ascii"))
        return base64.b64encode(sig_bytes).decode("ascii")

    def signed_request(
        self,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        params: Mapping[str, Any],
        *,
        weight: int,
        priority: Priority = Priority.CRITICAL,
    ) -> Any:
        endpoint = f"{method.upper()} {path}"
        self.governor.acquire(weight, priority)

        full_params: list[tuple[str, str]] = [
            (str(k), str(v)) for k, v in params.items() if v is not None
        ]
        full_params.append(("recvWindow", str(self.recv_window_ms)))
        full_params.append(("timestamp", str(self.clock.signing_timestamp_ms())))
        query_string = urlencode(full_params)
        signature = self._sign(query_string)
        signed_qs = f"{query_string}&signature={ urlencode({'s': signature})[2:] }"
        url = f"{self.base_url}{path}?{signed_qs}"
        headers = {"X-MBX-APIKEY": self._api_key}

        try:
            resp = self._session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                timeout=self.timeout_s,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if method.upper() == "POST":
                raise RequestTimeoutUnknownState(
                    f"Timeout/connection drop during {endpoint}; "
                    "order execution state is unknown (-1007). Query origClientOrderId before acting."
                ) from exc
            raise

        try:
            return self._handle_response(resp, endpoint)
        except BinanceAPIError as exc:
            if method.upper() == "POST" and (exc.code == -1007 or 500 <= exc.http_status < 600):
                raise RequestTimeoutUnknownState(
                    f"Binance returned HTTP {exc.http_status} (code={exc.code}) on {endpoint}; "
                    "execution status is UNKNOWN. Query origClientOrderId before retrying."
                ) from exc
            raise

    def get_account(self, *, omit_zero_balances: bool = True) -> dict[str, Any]:
        data = self.signed_request(
            "GET",
            "/api/v3/account",
            {"omitZeroBalances": "true" if omit_zero_balances else "false"},
            weight=ENDPOINT_WEIGHTS["GET /api/v3/account"],
            priority=Priority.CRITICAL,
        )
        return dict(data)

    def get_order(
        self,
        symbol: str,
        *,
        orig_client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> dict[str, Any]:
        if orig_client_order_id is None and order_id is None:
            raise ValueError("get_order requires orig_client_order_id or order_id.")
        params: dict[str, Any] = {"symbol": symbol}
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        if order_id is not None:
            params["orderId"] = int(order_id)
        data = self.signed_request(
            "GET",
            "/api/v3/order",
            params,
            weight=ENDPOINT_WEIGHTS["GET /api/v3/order"],
            priority=Priority.CRITICAL,
        )
        return dict(data)

    def get_open_order_lists(self) -> list[dict[str, Any]]:
        data = self.signed_request(
            "GET",
            "/api/v3/openOrderList",
            {},
            weight=ENDPOINT_WEIGHTS["GET /api/v3/openOrderList"],
            priority=Priority.CRITICAL,
        )
        return list(data)

    def get_my_trades(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": symbol, "limit": int(limit)}
        weight = 20
        if order_id is not None:
            params["orderId"] = int(order_id)
            weight = ENDPOINT_WEIGHTS["GET /api/v3/myTrades"]
        data = self.signed_request(
            "GET",
            "/api/v3/myTrades",
            params,
            weight=weight,
            priority=Priority.CRITICAL,
        )
        return list(data)

    def get_account_commission(self, symbol: str) -> dict[str, Any]:
        data = self.signed_request(
            "GET",
            "/api/v3/account/commission",
            {"symbol": symbol},
            weight=ENDPOINT_WEIGHTS["GET /api/v3/account/commission"],
            priority=Priority.CRITICAL,
        )
        return dict(data)

    # /sapi/*: mock automático quando testnet=True (TODO Cat.2); pool SAPI separado (unified-architecture.md:221)
    def sapi_system_status(self) -> SystemStatus:
        if self.testnet:
            return SystemStatus(normal=True, msg="normal (mocked on TESTNET)", mocked=True)
        data = self._request(
            "GET",
            "/sapi/v1/system/status",
            None,
            weight=0,
            priority=Priority.HIGH,
        )
        status_code = int(data.get("status", 0))
        msg = str(data.get("msg", "normal"))
        return SystemStatus(normal=(status_code == 0), msg=msg, mocked=False)

    def sapi_transfer_main_to_funding(self, asset: str, amount: Decimal) -> dict[str, Any]:
        amt_str = to_api_str(amount)
        if self.testnet:
            return {
                "tranId": int(self.clock.server_now_ms()),
                "asset": asset,
                "amount": amt_str,
                "type": "MAIN_FUNDING",
                "mocked": True,
            }
        data = self.signed_request(
            "POST",
            "/sapi/v1/asset/transfer",
            {"type": "MAIN_FUNDING", "asset": asset, "amount": amt_str},
            weight=0,
            priority=Priority.CRITICAL,
        )
        return dict(data)

    # Wrappers de ordem (assinaturas congeladas no Bloco 1; orquestração completa no Bloco 5):
    def new_market_buy_quote(
        self,
        symbol: str,
        quote_qty: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        data = self.signed_request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": to_api_str(quote_qty),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
            weight=ENDPOINT_WEIGHTS["POST /api/v3/order"],
            priority=Priority.CRITICAL,
        )
        return dict(data)

    def new_market_sell(
        self,
        symbol: str,
        qty: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        data = self.signed_request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": to_api_str(qty),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
            weight=ENDPOINT_WEIGHTS["POST /api/v3/order"],
            priority=Priority.CRITICAL,
        )
        return dict(data)

    def new_oco_sell(
        self,
        symbol: str,
        qty: Decimal,
        take_profit: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal | None,
        list_client_order_id: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": "SELL",
            "quantity": to_api_str(qty),
            "aboveType": "LIMIT_MAKER",
            "abovePrice": to_api_str(take_profit),
            "belowType": "STOP_LOSS_LIMIT" if stop_limit_price is not None else "STOP_LOSS",
            "belowStopPrice": to_api_str(stop_price),
            "listClientOrderId": list_client_order_id,
            "newOrderRespType": "FULL",
        }
        if stop_limit_price is not None:
            params["belowPrice"] = to_api_str(stop_limit_price)
            params["belowTimeInForce"] = "GTC"
        data = self.signed_request(
            "POST",
            "/api/v3/orderList/oco",
            params,
            weight=ENDPOINT_WEIGHTS["POST /api/v3/orderList/oco"],
            priority=Priority.CRITICAL,
        )
        return dict(data)

    def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
        data = self.signed_request(
            "DELETE",
            "/api/v3/orderList",
            {"symbol": symbol, "orderListId": int(order_list_id)},
            weight=ENDPOINT_WEIGHTS["DELETE /api/v3/orderList"],
            priority=Priority.CRITICAL,
        )
        return dict(data)

    def get_order_list(
        self,
        order_list_id: int | None = None,
        *,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if order_list_id is None and orig_client_order_id is None:
            raise ValueError("get_order_list requires order_list_id or orig_client_order_id.")
        params: dict[str, Any] = {}
        if order_list_id is not None:
            params["orderListId"] = int(order_list_id)
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        data = self.signed_request(
            "GET",
            "/api/v3/orderList",
            params,
            weight=ENDPOINT_WEIGHTS["GET /api/v3/orderList"],
            priority=Priority.CRITICAL,
        )
        return dict(data)


def build_clients(
    cfg: AppConfig,
) -> tuple[PublicBinanceClient, SignedBinanceClient | None, WeightGovernor, MonotonicClock]:
    governor = WeightGovernor()
    er = cfg.strategies.execution_and_resilience
    clock = MonotonicClock(safety_margin_ms=er.clock_safety_margin_ms)
    public_client = PublicBinanceClient(cfg.market_data_base_url, governor)

    if not cfg.is_live:
        return public_client, None, governor, clock

    assert cfg.trading_base_url is not None
    assert cfg.runtime.binance_api_key is not None
    assert cfg.runtime.binance_ed25519_private_key_path is not None

    signed_client = SignedBinanceClient(
        cfg.trading_base_url,
        governor,
        clock,
        api_key=cfg.runtime.binance_api_key.get_secret_value(),
        ed25519_private_key_path=cfg.runtime.binance_ed25519_private_key_path,
        recv_window_ms=er.recv_window_ms,
        testnet=(cfg.runtime.binance_env == BinanceEnv.TESTNET),
    )
    return public_client, signed_client, governor, clock
