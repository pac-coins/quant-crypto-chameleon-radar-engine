"""Cofre de Lucros Blindado com Regra de High-Water Mark e Transferência MAIN_FUNDING (Bloco 4)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from src.core.binance_client import SignedBinanceClient, to_decimal
from src.core.config import ProfitSplitterCfg
from src.core.database import Database

STATE_KEY_PROFIT_HWM: str = "profit_hwm_usdt"
STATE_KEY_PENDING_HODL_BUCKET: str = "pending_hodl_bucket_usdt"


@dataclass(frozen=True, slots=True)
class ProfitSplitResult:
    """Resultado auditável do processamento de lucro pós-operação."""

    equity_before_usdt: Decimal
    equity_after_usdt: Decimal
    previous_hwm_usdt: Decimal
    new_hwm_usdt: Decimal
    new_high_profit_usdt: Decimal
    added_to_bucket_usdt: Decimal
    pending_bucket_usdt: Decimal
    transferred_to_funding_usdt: Decimal
    transfer_tx_id: Optional[int] = None


class ProfitSplitter:
    """
    Alimentador blindado da carteira de longo prazo (FUNDING):
    1. Regra de High-Water Mark (Item Cat. 2 do TODO): só separa 30% (send_to_hodl_pct)
       da parcela de lucro que superar o Topo Histórico (profit_hwm_usdt). Se um trade
       vencedor apenas recuperar prejuízo anterior sem bater novo recorde patrimonial,
       nenhum centavo sai da Carteira Tática.
    2. Acúmulo Contábil Isolado: o valor separado fica em pending_hodl_bucket_usdt
       (descontado do caixa operável pelo PositionSizer).
    3. Transferência Atômica MAIN_FUNDING: quando pending_hodl_bucket_usdt >= 10.00 USDT,
       dispara POST /sapi/v1/asset/transfer (type='MAIN_FUNDING') e desconta o valor
       transferido tanto do bucket quanto da referência de High-Water Mark (para que a
       saída legítima de lucro para a Funding não seja vista como queda da banca).
    """

    def __init__(
        self,
        cfg: ProfitSplitterCfg,
        db: Optional[Database] = None,
        signed_client: Optional[SignedBinanceClient] = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.signed_client = signed_client
        self._mem_state: dict[str, str] = {}

    def _get(self, key: str, default: str) -> Decimal:
        if self.db is not None:
            val = self.db.get_state(key)
            if val is not None:
                return to_decimal(val)
        return to_decimal(self._mem_state.get(key, default))

    def _set(self, key: str, value: Decimal) -> None:
        s = format(value, "f")
        self._mem_state[key] = s
        if self.db is not None:
            self.db.set_state(key, s)

    @property
    def pending_hodl_bucket_usdt(self) -> Decimal:
        return self._get(STATE_KEY_PENDING_HODL_BUCKET, "0")

    @property
    def profit_hwm_usdt(self) -> Decimal:
        return self._get(STATE_KEY_PROFIT_HWM, "0")

    def on_trade_closed(
        self,
        *,
        equity_before_usdt: Decimal | str,
        equity_after_usdt: Decimal | str,
        cppi_drawdown_active: bool = False,
        quote_asset: str = "USDT",
    ) -> ProfitSplitResult:
        eq_before = to_decimal(equity_before_usdt)
        eq_after = to_decimal(equity_after_usdt)

        stored_hwm = self._get(STATE_KEY_PROFIT_HWM, "0")
        prev_hwm = max(stored_hwm, eq_before)
        bucket = self._get(STATE_KEY_PENDING_HODL_BUCKET, "0")

        if not self.cfg.enabled or cppi_drawdown_active:
            new_hwm = max(prev_hwm, eq_after)
            self._set(STATE_KEY_PROFIT_HWM, new_hwm)
            return ProfitSplitResult(
                equity_before_usdt=eq_before,
                equity_after_usdt=eq_after,
                previous_hwm_usdt=prev_hwm,
                new_hwm_usdt=new_hwm,
                new_high_profit_usdt=Decimal("0"),
                added_to_bucket_usdt=Decimal("0"),
                pending_bucket_usdt=bucket,
                transferred_to_funding_usdt=Decimal("0"),
                transfer_tx_id=None,
            )

        # Lucro elegível é estritamente a parcela que supera o High-Water Mark anterior
        if eq_after > prev_hwm:
            new_high_profit = eq_after - prev_hwm
            new_hwm = eq_after
        else:
            new_high_profit = Decimal("0")
            new_hwm = prev_hwm

        send_pct = Decimal(str(self.cfg.send_to_hodl_pct))
        added_to_bucket = (new_high_profit * send_pct).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        bucket = bucket + added_to_bucket

        min_transfer = Decimal(str(self.cfg.min_bucket_to_transfer_usdt))
        transferred = Decimal("0")
        tx_id: Optional[int] = None

        if bucket >= min_transfer:
            transfer_amount = bucket.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if self.signed_client is not None:
                resp = self.signed_client.sapi_transfer_main_to_funding(
                    quote_asset, transfer_amount
                )
                raw_tx = resp.get("tranId")
                tx_id = int(raw_tx) if raw_tx is not None else None
            transferred = transfer_amount
            bucket = bucket - transfer_amount
            # Desconta do HWM o valor efetivamente retirado da carteira Spot para a Funding
            new_hwm = max(Decimal("0"), new_hwm - transferred)

        self._set(STATE_KEY_PROFIT_HWM, new_hwm)
        self._set(STATE_KEY_PENDING_HODL_BUCKET, bucket)

        return ProfitSplitResult(
            equity_before_usdt=eq_before,
            equity_after_usdt=eq_after,
            previous_hwm_usdt=prev_hwm,
            new_hwm_usdt=new_hwm,
            new_high_profit_usdt=new_high_profit,
            added_to_bucket_usdt=added_to_bucket,
            pending_bucket_usdt=bucket,
            transferred_to_funding_usdt=transferred,
            transfer_tx_id=tx_id,
        )
