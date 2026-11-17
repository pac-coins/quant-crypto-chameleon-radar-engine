from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Sequence

from src.core.binance_client import (
    BinanceAPIError,
    ExchangeInfoCache,
    MonotonicClock,
    Priority,
    PublicBinanceClient,
    SignedBinanceClient,
    build_clients,
    floor_qty_to_step,
    to_api_str,
    to_decimal,
)
from src.core.config import (
    AppConfig,
    BinanceEnv,
    ConfigError,
    RunMode,
    load_config,
)
from src.core.database import Database, SymbolFilters
from src.risk.shadow_manager import (
    STATE_KEY_EMERGENCY_HALT,
    STATE_KEY_KILL_SWITCH_REASON,
    STATE_KEY_RECONCILE_FAIL_STREAK,
    ShadowManager,
)


@dataclass(frozen=True, slots=True)
class TestnetPrepSummary:
    wall_offset_ms: float
    rtt_ms: float
    sold_assets: dict[str, Decimal]
    bnb_bought_usdt: Decimal
    halt_reset: bool
    final_usdt_free: Decimal
    final_bnb_free: Decimal
    remaining_universe_assets: dict[str, Decimal]


def _ensure_testnet_only(cfg: AppConfig, signed_client: SignedBinanceClient | None) -> SignedBinanceClient:
    if cfg.runtime.mode != RunMode.LIVE_CHAMELEON_PHASE_1:
        raise ConfigError(
            f"testnet_prepare requires MODE=LIVE_CHAMELEON_PHASE_1 (got {cfg.runtime.mode.value})."
        )
    if cfg.runtime.binance_env != BinanceEnv.TESTNET:
        raise ConfigError(
            f"FORBIDDEN: testnet_prepare refuses BINANCE_ENV={cfg.runtime.binance_env.value}; "
            "only TESTNET is allowed."
        )
    if cfg.trading_base_url is None or "testnet.binance.vision" not in cfg.trading_base_url:
        raise ConfigError(f"FORBIDDEN: unexpected trading_base_url={cfg.trading_base_url!r}.")
    if signed_client is None or not signed_client.testnet:
        raise ConfigError("SignedBinanceClient must be configured with testnet=True.")
    return signed_client


def _fetch_balances_map(signed_client: SignedBinanceClient) -> dict[str, tuple[Decimal, Decimal]]:
    acct = signed_client.get_account(omit_zero_balances=True)
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for row in acct.get("balances", []):
        if isinstance(row, dict) and "asset" in row:
            asset = str(row["asset"]).upper()
            free_d = to_decimal(str(row.get("free", "0")))
            locked_d = to_decimal(str(row.get("locked", "0")))
            if free_d > Decimal("0") or locked_d > Decimal("0"):
                out[asset] = (free_d, locked_d)
    return out


def _sell_asset_with_chunk_fallback(
    *,
    signed_client: SignedBinanceClient,
    symbol: str,
    free_qty: Decimal,
    flt: SymbolFilters,
    bid_price: Decimal,
    now_ms_fn: Callable[[], int],
    max_attempts: int = 64,
) -> Decimal:
    """Sell free Testnet balance at MARKET, splitting into smaller chunks if MARKET_LOT_SIZE rejects full size."""
    remaining = floor_qty_to_step(free_qty, flt.step_size)
    if remaining < flt.min_qty or (remaining * bid_price) < flt.min_notional:
        return Decimal("0")

    initial_qty = remaining
    total_sold = Decimal("0")
    divisor = 1
    attempts = 0
    while remaining >= flt.min_qty and (remaining * bid_price) >= flt.min_notional and attempts < max_attempts:
        attempts += 1
        max_chunk = floor_qty_to_step(initial_qty / Decimal(divisor), flt.step_size)
        chunk = min(remaining, max_chunk) if max_chunk >= flt.min_qty else remaining
        if (chunk * bid_price) < flt.min_notional:
            chunk = remaining
        client_oid = f"TPREP_{symbol}_{now_ms_fn()}_{attempts}"
        try:
            resp = signed_client.new_market_sell(
                symbol=symbol,
                qty=chunk,
                client_order_id=client_oid,
            )
            executed = to_decimal(str(resp.get("executedQty", "0")))
            if executed <= Decimal("0"):
                break
            total_sold += executed
            remaining = floor_qty_to_step(max(Decimal("0"), remaining - executed), flt.step_size)
        except BinanceAPIError as exc:
            # Testnet often enforces tight MARKET_LOT_SIZE maxQty or thin book depth on full faucet qty
            if exc.code in (-1013, -2010) and divisor < 64:
                divisor *= 4
                continue
            raise
    return total_sold


def prepare_testnet_account(
    *,
    cfg: AppConfig,
    db: Database,
    public_client: PublicBinanceClient,
    signed_client: SignedBinanceClient | None,
    clock: MonotonicClock,
    execute: bool = False,
    min_bnb_usdt: Decimal = Decimal("10.00"),
    bnb_topup_usdt: Decimal = Decimal("20.00"),
) -> TestnetPrepSummary:
    signed = _ensure_testnet_only(cfg, signed_client)
    sample = clock.sync(public_client.get_server_time_ms)
    now_ms = clock.server_now_ms()

    ex_cache = ExchangeInfoCache(public_client, db, cfg.market_data_universe)
    ex_cache.refresh_if_stale(now_ms)

    balances = _fetch_balances_map(signed)
    sold_assets: dict[str, Decimal] = {}
    bnb_bought_usdt = Decimal("0")
    halt_reset = False

    if execute:
        for sym in cfg.execution_universe:
            flt = ex_cache.get(sym)
            base = flt.base_asset.upper()
            free_b, locked_b = balances.get(base, (Decimal("0"), Decimal("0")))
            if locked_b > flt.min_qty:
                try:
                    signed.signed_request(
                        "DELETE",
                        "/api/v3/openOrders",
                        {"symbol": sym},
                        weight=1,
                        priority=Priority.CRITICAL,
                    )
                    balances = _fetch_balances_map(signed)
                    free_b, locked_b = balances.get(base, (Decimal("0"), Decimal("0")))
                except BinanceAPIError:
                    pass

            if free_b < flt.min_qty:
                continue

            ticker = signed.get_book_ticker(sym, priority=Priority.CRITICAL)
            if (free_b * ticker.bid) < flt.min_notional:
                continue

            sold = _sell_asset_with_chunk_fallback(
                signed_client=signed,
                symbol=sym,
                free_qty=free_b,
                flt=flt,
                bid_price=ticker.bid,
                now_ms_fn=clock.server_now_ms,
            )
            if sold > Decimal("0"):
                sold_assets[base] = sold

        balances = _fetch_balances_map(signed)
        free_usdt = balances.get("USDT", (Decimal("0"), Decimal("0")))[0]
        free_bnb = balances.get("BNB", (Decimal("0"), Decimal("0")))[0]
        try:
            bnb_bid = signed.get_book_ticker("BNBUSDT", priority=Priority.HIGH).bid
        except Exception:
            bnb_bid = Decimal("0")
        bnb_usdt_equiv = free_bnb * bnb_bid if bnb_bid > Decimal("0") else Decimal("0")

        if bnb_bid > Decimal("0") and bnb_usdt_equiv < min_bnb_usdt and free_usdt >= bnb_topup_usdt + Decimal("5"):
            signed.new_market_buy_quote(
                symbol="BNBUSDT",
                quote_qty=bnb_topup_usdt,
                client_order_id=f"TPREP_BNB_{clock.server_now_ms()}",
            )
            bnb_bought_usdt = bnb_topup_usdt

        shadow = ShadowManager(
            cfg.strategies.drawdown_and_shadow_protection,
            cfg.strategies.portfolio,
            db=db,
        )
        shadow.reset_emergency_halt()
        halt_reset = True

    final_balances = _fetch_balances_map(signed)
    final_usdt = final_balances.get("USDT", (Decimal("0"), Decimal("0")))[0]
    final_bnb = final_balances.get("BNB", (Decimal("0"), Decimal("0")))[0]
    remaining_universe: dict[str, Decimal] = {}
    for sym in cfg.execution_universe:
        flt = ex_cache.get(sym)
        base = flt.base_asset.upper()
        free_b, locked_b = final_balances.get(base, (Decimal("0"), Decimal("0")))
        total_b = free_b + locked_b
        if total_b >= flt.min_qty:
            try:
                bid = signed.get_book_ticker(sym, priority=Priority.HIGH).bid
                if (total_b * bid) >= flt.min_notional:
                    remaining_universe[base] = total_b
            except Exception:
                remaining_universe[base] = total_b

    return TestnetPrepSummary(
        wall_offset_ms=sample.wall_offset_ms,
        rtt_ms=sample.rtt_ms,
        sold_assets=sold_assets,
        bnb_bought_usdt=bnb_bought_usdt,
        halt_reset=halt_reset,
        final_usdt_free=final_usdt,
        final_bnb_free=final_bnb,
        remaining_universe_assets=remaining_universe,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspeciona e limpa saldos não rastreados da conta Binance Spot TESTNET "
            "e reseta emergency_halt no SQLite antes do Gate 6A."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Executa vendas a mercado dos 7 ativos na TESTNET e reseta emergency_halt no SQLite.",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    db = Database(cfg.runtime.db_path)
    db.connect()
    db.migrate()
    try:
        public_client, signed_client, _gov, clock = build_clients(cfg)
        summary = prepare_testnet_account(
            cfg=cfg,
            db=db,
            public_client=public_client,
            signed_client=signed_client,
            clock=clock,
            execute=bool(args.execute),
        )
        halt_val = db.get_state(STATE_KEY_EMERGENCY_HALT) or "0"
        reason_val = db.get_state(STATE_KEY_KILL_SWITCH_REASON) or "NONE"
        streak_val = db.get_state(STATE_KEY_RECONCILE_FAIL_STREAK) or "0"
    finally:
        db.close()

    mode_lbl = "EXECUTE" if args.execute else "DRY-RUN (preview)"
    print(f"=== TESTNET PREPARE [{mode_lbl}] ===")
    print(f"Clock wall_offset_ms={summary.wall_offset_ms:+.1f} ms | rtt_ms={summary.rtt_ms:.1f} ms")
    print(f"Final USDT free={to_api_str(summary.final_usdt_free)} | BNB free={to_api_str(summary.final_bnb_free)}")
    print(f"Sold universe assets={ {k: to_api_str(v) for k, v in summary.sold_assets.items()} }")
    if summary.bnb_bought_usdt > Decimal("0"):
        print(f"Top-up BNBUSDT bought={to_api_str(summary.bnb_bought_usdt)} USDT")
    print(f"Remaining universe assets >= minNotional={ {k: to_api_str(v) for k, v in summary.remaining_universe_assets.items()} }")
    print(f"SQLite halt={halt_val} | reason={reason_val} | reconcile_fail_streak={streak_val}")
    if not args.execute and summary.remaining_universe_assets:
        print("Rode novamente com --execute (após `docker compose stop`) para zerar na Testnet e resetar o halt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
