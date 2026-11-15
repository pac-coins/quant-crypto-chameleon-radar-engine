"""Testes determinísticos do Gate 4 (Bloco 4): Dimensionador de Teto Duplo, CPPI, Kill-Switches e Cofre HWM."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.core.config import load_config
from src.core.database import Database, SymbolFilters
from src.features.ml_judge import (
    MLJudge,
    PlattCalibrator,
    compute_expected_calibration_error,
    extract_stationary_features,
)
from src.risk.position_sizer import PositionSizer, SignalGrade
from src.risk.profit_splitter import ProfitSplitter
from src.risk.shadow_manager import KillSwitchReason, ShadowManager


def _default_filters(
    symbol: str = "SOLUSDT",
    tick_size: str = "0.01",
    step_size: str = "0.01",
    min_qty: str = "0.01",
    min_notional: str = "5.00",
) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        status="TRADING",
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        tick_size=Decimal(tick_size),
        step_size=Decimal(step_size),
        min_qty=Decimal(min_qty),
        min_notional=Decimal(min_notional),
        oco_allowed=True,
        fetched_at_ms=1_700_000_000_000,
    )


def test_gate4a_dual_cap_wide_stop_and_pending_hodl_discount(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Gate 4A:
    - Um stop largo de 6% jamais aloca 50% ou 85%/95% do caixa, sendo travado pelo teto de risco (RISK_CAP);
    - Um stop curto nunca ultrapassa o teto de caixa (50% na Nota B) nem o disjuntor min(500 USDT, 55% da banca);
    - O saldo em pending_hodl_bucket_usdt é descontado do caixa operável antes de dimensionar o lote.
    """
    monkeypatch.setenv("MODE", "SHADOW_CHAMELEON")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = load_config()

    sizer = PositionSizer(
        sizing_cfg=cfg.strategies.position_sizing,
        portfolio_cfg=cfg.strategies.portfolio,
        use_ml_filter=False,
    )
    filters = _default_filters()

    # 1. Stop largo de 6% (Entry = 100.00, Stop = 94.00, Target = 112.00) com banca de 1000 USDT:
    # d_stop = 0.06, taxas+slippage = 0.0015 + 0.0006 = 0.0021 -> perda total no stop = 0.0621 (6.21%)
    # Risco máximo 1% = 10 USDT -> V_cap_risk = 10 / 0.0621 = 161.03 USDT (~16.1% do caixa, muito abaixo de 50%!)
    dec_wide = sizer.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="94.00",
        target_price="112.00",
        free_usdt="1000.00",
        symbol_filters=filters,
    )
    assert dec_wide.approved is True
    assert dec_wide.cap_binding == "RISK_CAP"
    assert dec_wide.quote_order_qty <= Decimal("162.00")
    assert dec_wide.quote_order_qty < Decimal("500.00")

    # Mesmo na Fase 2 com Nota A+ (risco 2.0%, max_cash 85%), stop de 6% fica em ~322 USDT (~32.2% << 85%)
    sizing_a_plus = cfg.strategies.position_sizing.model_copy(
        update={"conviction_sizing_enabled": True}
    )
    sizer_phase2 = PositionSizer(
        sizing_cfg=sizing_a_plus,
        portfolio_cfg=cfg.strategies.portfolio,
        use_ml_filter=True,
    )
    dec_wide_aplus = sizer_phase2.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="94.00",
        target_price="115.00",
        free_usdt="1000.00",
        symbol_filters=filters,
        ml_calibrated_prob=0.65,
        ml_proposed_grade=SignalGrade.A_PLUS,
    )
    assert dec_wide_aplus.approved is True
    assert dec_wide_aplus.grade == SignalGrade.A_PLUS
    assert dec_wide_aplus.cap_binding == "RISK_CAP"
    assert dec_wide_aplus.quote_order_qty <= Decimal("323.00")

    # 2. Stop curto de 1% (Entry = 100.00, Stop = 99.00) com banca de 300 USDT e 40 USDT no pending_hodl_bucket:
    # Caixa operável = 300 - 40 = 260 USDT -> Teto de caixa 50% = 130 USDT (menor que V_cap_risk = 2.60 / 0.0121 = 214.87 USDT)
    dec_tight = sizer.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="99.00",
        target_price="103.00",
        free_usdt="300.00",
        pending_hodl_bucket_usdt="40.00",
        symbol_filters=filters,
    )
    assert dec_tight.approved is True
    assert dec_tight.usable_cash_usdt == Decimal("260.00")
    assert dec_tight.cap_binding == "CASH_CAP"
    assert dec_tight.quote_order_qty == Decimal("130.00")

    # 3. Disjuntor de ordem única (500 USDT) em banca grande (5000 USDT) com stop de 1%
    dec_order_cap = sizer.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="99.00",
        target_price="103.00",
        free_usdt="5000.00",
        symbol_filters=filters,
    )
    assert dec_order_cap.approved is True
    assert dec_order_cap.cap_binding == "ORDER_CAP"
    assert dec_order_cap.quote_order_qty <= Decimal("500.00")


def test_gate4b_indifference_threshold_ml_silent_observer_and_platt_scaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Gate 4B:
    - Um sinal com probabilidade calibrada abaixo ou igual a p_indiferenca + 0.03 recebe lote ZERO na Fase 2;
    - Na Fase 1 (use_ml_filter=False), o Juiz de IA roda em Modo Observador Silencioso (grava ml_grade_silent
      e ml_p_cal_silent sem bloquear o trade);
    - PlattCalibrator atinge ECE <= 0.05 fora da amostra sem decorar amostras pequenas.
    """
    monkeypatch.setenv("MODE", "SHADOW_CHAMELEON")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = load_config()
    filters = _default_filters()

    # Calibração de Platt e ECE em amostra sintética de 1200 operações (600 treino / 600 OOS)
    rng = np.random.default_rng(7)
    raw_scores = rng.normal(0.0, 1.2, size=1200)
    true_probs = 1.0 / (1.0 + np.exp(-1.5 * raw_scores + 0.3))
    outcomes = (rng.uniform(0.0, 1.0, size=1200) < true_probs).astype(int)

    cal = PlattCalibrator().fit(raw_scores[:600], outcomes[:600], lr=0.5)
    oos_probs = [cal.predict_proba(float(s)) for s in raw_scores[600:]]
    ece = compute_expected_calibration_error(oos_probs, outcomes[600:].tolist(), n_bins=5)
    assert ece <= 0.05

    # Extração das 5 features estacionárias causais
    row = pd.Series(
        {
            "close": 100.0,
            "atr_14": 2.5,
            "ema_100": 95.0,
            "rvol_20": 1.6,
            "chop_14_4h": 42.0,
            "btc_ret_12h": 0.025,
        }
    )
    feats = extract_stationary_features(row)
    assert pytest.approx(feats.atr_pct) == 0.025
    assert pytest.approx(feats.dist_ema100_atr) == 2.0

    # Fase 1 (use_ml_filter=False): prob baixa (0.25 < p_indiff ~ 0.357) gera ml_grade_silent='C',
    # mas NÃO bloqueia a ordem (Modo Observador Silencioso)
    sizer_phase1 = PositionSizer(
        sizing_cfg=cfg.strategies.position_sizing,
        portfolio_cfg=cfg.strategies.portfolio,
        use_ml_filter=False,
    )
    dec_silent = sizer_phase1.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="97.00",
        target_price="106.00",
        free_usdt="300.00",
        symbol_filters=filters,
        ml_calibrated_prob=0.25,
    )
    assert dec_silent.approved is True
    assert dec_silent.grade == SignalGrade.B
    assert dec_silent.ml_grade_silent == "C"
    assert dec_silent.ml_p_cal_silent == pytest.approx(0.25)

    # Fase 2 (use_ml_filter=True): a mesma probabilidade baixa (0.25 <= p_indiff + 0.03) recebe lote ZERO!
    sizer_phase2 = PositionSizer(
        sizing_cfg=cfg.strategies.position_sizing,
        portfolio_cfg=cfg.strategies.portfolio,
        use_ml_filter=True,
    )
    dec_blocked = sizer_phase2.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="97.00",
        target_price="106.00",
        free_usdt="300.00",
        symbol_filters=filters,
        ml_calibrated_prob=0.25,
    )
    assert dec_blocked.approved is False
    assert dec_blocked.base_qty == Decimal("0")
    assert dec_blocked.quote_order_qty == Decimal("0")
    assert dec_blocked.reject_reason == "BELOW_INDIFFERENCE_THRESHOLD"

    # Avaliação via MLJudge
    judge = MLJudge(cfg.strategies.ml_judge_and_validation, cfg.strategies.position_sizing, cal)
    verdict_low = judge.evaluate_signal(raw_score=-2.0, stop_distance_pct=0.03, gross_rr_ratio=2.0)
    assert verdict_low.grade == SignalGrade.C


def test_gate4c_min_notional_buffer_abort_and_decimal_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Gate 4C:
    - Se o volume financeiro calculado ficar abaixo de 1.05 * minNotional (ex.: minNotional=10 USDT -> exige 10.50 USDT),
      a operação é abortada com lote zero (jamais arredondada para cima).
    """
    monkeypatch.setenv("MODE", "SHADOW_CHAMELEON")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = load_config()
    sizer = PositionSizer(cfg.strategies.position_sizing, cfg.strategies.portfolio)
    filters_10 = _default_filters(min_notional="10.00")

    # Banca de 18 USDT -> teto de 50% do caixa = 9.00 USDT < 1.05 * 10.00 (10.50 USDT) -> ABORTA!
    dec_small = sizer.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="99.00",
        target_price="103.00",
        free_usdt="18.00",
        symbol_filters=filters_10,
    )
    assert dec_small.approved is False
    assert dec_small.base_qty == Decimal("0")
    assert dec_small.quote_order_qty == Decimal("0")
    assert dec_small.reject_reason == "BELOW_MIN_NOTIONAL_BUFFER"


def test_gate4d_cppi_drawdown_cusum_shadow_downgrade_and_kill_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Gate 4D:
    - Em drawdown >= 4.0%, CPPI corta o risco pela metade (1.0% -> 0.5%) e recupera ao bater novo HWM;
    - Modo Fantasma (CUSUM_SHADOW_MODE) exige 4 vitórias virtuais com soma de R > 0 (não religa com 1 vitória de +2.5R);
    - Auto-Downgrade desativa convicção se média de 30 trades < 0 ou ECE > 0.08, e desativa Sideways se soma de 20 trades <= -3.0R;
    - Os 4 Hard Kill-Switches (-12% DD, BNB < $2.00, 3 falhas de reconciliação/API, >= 6 trades/24h) acionam EMERGENCY_HALT.
    """
    monkeypatch.setenv("MODE", "SHADOW_CHAMELEON")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = load_config()

    db = Database(tmp_path / "gate4.sqlite")
    db.migrate()

    sm = ShadowManager(
        protection_cfg=cfg.strategies.drawdown_and_shadow_protection,
        portfolio_cfg=cfg.strategies.portfolio,
        db=db,
    )

    # 1. Topo inicial = 300 USDT -> risco normal 1.0x
    s0 = sm.evaluate_defense(current_equity_usdt="300.00")
    assert s0.hwm_usdt == Decimal("300.00")
    assert s0.cppi_active is False
    assert s0.risk_multiplier == Decimal("1.0")

    # 2. Cai para 287.50 USDT (drawdown = 4.167% >= 4.0%) -> CPPI corta risco pela metade (0.50x)
    s_dd = sm.evaluate_defense(current_equity_usdt="287.50")
    assert s_dd.cppi_active is True
    assert s_dd.risk_multiplier == Decimal("0.5")

    # 3. Recupera para 301.00 USDT -> restaura 1.0x e atualiza HWM
    s_rec = sm.evaluate_defense(current_equity_usdt="301.00")
    assert s_rec.hwm_usdt == Decimal("301.00")
    assert s_rec.cppi_active is False
    assert s_rec.risk_multiplier == Decimal("1.0")

    # 4. Modo Fantasma (CUSUM_SHADOW_MODE): 3 derrotas seguidas ativam Shadow; 1 vitória de +2.5R NÃO religa!
    shadow_cfg = cfg.strategies.drawdown_and_shadow_protection.model_copy(
        update={"protection_mode": "CUSUM_SHADOW_MODE", "shadow_mode_enabled": True}
    )
    sm_shadow = ShadowManager(shadow_cfg, cfg.strategies.portfolio, db=db)
    for _ in range(3):
        sm_shadow.record_trade_outcome(net_r=-1.0, is_virtual_shadow_trade=False)
    st_sh1 = sm_shadow.evaluate_defense(current_equity_usdt="295.00")
    assert st_sh1.shadow_mode_active is True
    assert st_sh1.risk_multiplier == Decimal("0")

    # 1 única vitória virtual grande (+2.5R) NÃO pode religar o robô (exige 4 vitórias virtuais)
    sm_shadow.record_trade_outcome(net_r=2.5, is_virtual_shadow_trade=True)
    assert sm_shadow.evaluate_defense(current_equity_usdt="295.00").shadow_mode_active is True

    # Completando as 4 vitórias virtuais com soma de R > 0 -> religa!
    for _ in range(3):
        sm_shadow.record_trade_outcome(net_r=0.8, is_virtual_shadow_trade=True)
    st_sh_done = sm_shadow.evaluate_defense(current_equity_usdt="295.00")
    assert st_sh_done.shadow_mode_active is False
    assert st_sh_done.risk_multiplier == Decimal("1.0")

    # 5. Auto-Downgrade de Fase
    conv_down, sw_dis = sm.evaluate_phase_downgrade(
        recent_live_r_30=[-0.2] * 30,
        recent_ece_30=0.09,
        recent_sideways_r_20=[-0.2] * 20,  # soma = -4.0R <= -3.0R
    )
    assert conv_down is True
    assert sw_dis is True

    # 6. Os 4 Hard Kill-Switches
    # (a) BNB < $2.00
    s_bnb = sm.evaluate_defense(current_equity_usdt="300.00", bnb_free_usdt_equiv="1.80")
    assert s_bnb.emergency_halt is True
    assert s_bnb.kill_switch_reason == KillSwitchReason.BNB_FEE_RESERVE_DEPLETED
    assert s_bnb.risk_multiplier == Decimal("0")
    sm.reset_emergency_halt()

    # (b) Frequência diária >= 6 trades
    s_freq = sm.evaluate_defense(current_equity_usdt="300.00", trades_last_24h=6)
    assert s_freq.emergency_halt is True
    assert s_freq.kill_switch_reason == KillSwitchReason.DAILY_TRADES_CAP_EXCEEDED
    sm.reset_emergency_halt()

    # (c) 3 falhas consecutivas de reconciliação
    assert sm.record_reconciliation_outcome(success=False) is None
    assert sm.record_reconciliation_outcome(success=False) is None
    assert (
        sm.record_reconciliation_outcome(success=False)
        == KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE
    )
    sm.reset_emergency_halt()

    # (d) Hard Stop Patrimonial de -12% (de 301.00 para 260.00 -> DD = 13.62% >= 12%)
    s_dd12 = sm.evaluate_defense(current_equity_usdt="260.00")
    assert s_dd12.emergency_halt is True
    assert s_dd12.kill_switch_reason == KillSwitchReason.HARD_STOP_DRAWDOWN_12PCT
    assert s_dd12.risk_multiplier == Decimal("0")
    db.close()


def test_gate4e_profit_splitter_hwm_and_main_funding_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Gate 4E:
    - Ganho que apenas recupera prejuízo anterior sem superar o High-Water Mark gera 0 USDT para o cofre;
    - Ganho acima do High-Water Mark acumula 30% apenas do excedente em pending_hodl_bucket_usdt;
    - Ao atingir >= 10 USDT no bucket, executa transferência MAIN_FUNDING e desconta o valor transferido do HWM;
    - Valida o Notebook 03 (03_tail_stress_cppi_cusum.ipynb).
    """
    monkeypatch.setenv("MODE", "SHADOW_CHAMELEON")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = load_config()

    db = Database(tmp_path / "splitter.sqlite")
    db.migrate()

    transfers_called: list[tuple[str, Decimal]] = []

    class _FakeSignedClient:
        def sapi_transfer_main_to_funding(self, asset: str, amount: Decimal) -> dict[str, Any]:
            transfers_called.append((asset, amount))
            return {"tranId": 999888, "asset": asset, "amount": str(amount), "type": "MAIN_FUNDING"}

    split_cfg = cfg.strategies.profit_splitter.model_copy(update={"enabled": True})
    splitter = ProfitSplitter(
        cfg=split_cfg,
        db=db,
        signed_client=_FakeSignedClient(),  # type: ignore[arg-type]
    )

    # 1. Trade 1: cai de 300.00 para 280.00 (HWM fica em 300.00, bucket = 0)
    r1 = splitter.on_trade_closed(equity_before_usdt="300.00", equity_after_usdt="280.00")
    assert r1.new_hwm_usdt == Decimal("300.00")
    assert r1.added_to_bucket_usdt == Decimal("0")

    # 2. Trade 2: sobe de 280.00 para 298.00 (+18 USDT, mas ainda abaixo do HWM de 300.00!) -> ZERO enviado ao cofre!
    r2 = splitter.on_trade_closed(equity_before_usdt="280.00", equity_after_usdt="298.00")
    assert r2.new_high_profit_usdt == Decimal("0")
    assert r2.added_to_bucket_usdt == Decimal("0")
    assert r2.pending_bucket_usdt == Decimal("0")

    # 3. Trade 3: sobe de 298.00 para 318.00 -> supera o HWM (300.00) em +18.00 USDT!
    # 30% de 18.00 = 5.40 USDT (< 10 USDT, acumula no bucket sem transferir ainda)
    r3 = splitter.on_trade_closed(equity_before_usdt="298.00", equity_after_usdt="318.00")
    assert r3.new_high_profit_usdt == Decimal("18.00")
    assert r3.added_to_bucket_usdt == Decimal("5.40")
    assert r3.pending_bucket_usdt == Decimal("5.40")
    assert r3.transferred_to_funding_usdt == Decimal("0")
    assert len(transfers_called) == 0

    # 4. Trade 4: sobe de 318.00 para 338.00 (+20.00 USDT acima do HWM 318.00) -> +6.00 USDT no bucket
    # Total no bucket = 5.40 + 6.00 = 11.40 USDT >= 10.00 USDT -> Transfere 11.40 USDT para FUNDING!
    # Novo HWM ajustado pós-transferência = 338.00 - 11.40 = 326.60 USDT
    r4 = splitter.on_trade_closed(equity_before_usdt="318.00", equity_after_usdt="338.00")
    assert r4.added_to_bucket_usdt == Decimal("6.00")
    assert r4.transferred_to_funding_usdt == Decimal("11.40")
    assert r4.pending_bucket_usdt == Decimal("0.00")
    assert r4.new_hwm_usdt == Decimal("326.60")
    assert r4.transfer_tx_id == 999888
    assert transfers_called == [("USDT", Decimal("11.40"))]
    db.close()

    # 5. Verifica e executa o Notebook 03 (03_tail_stress_cppi_cusum.ipynb)
    nb_path = Path("notebooks/03_tail_stress_cppi_cusum.ipynb")
    assert nb_path.exists()
    nb_data = json.loads(nb_path.read_text(encoding="utf-8"))
    assert nb_data.get("nbformat") == 4
    ns: dict[str, object] = {}
    for cell in nb_data.get("cells", []):
        if cell.get("cell_type") == "code":
            exec(compile("".join(cell.get("source", [])), "03_tail_stress_cppi_cusum.ipynb", "exec"), ns)
    sim_fn = ns["simulate_equity_curve_with_cppi_and_splitter"]
    res_sim = sim_fn([1.5, -1.0, -1.0, -1.0, -1.0, 2.0, 2.0], cppi_enabled=True, splitter_enabled=True)  # type: ignore[operator]
    assert res_sim["final_tactical_usdt"] > Decimal("300.00")
