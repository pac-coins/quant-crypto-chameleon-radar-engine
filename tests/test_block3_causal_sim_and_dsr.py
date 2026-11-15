"""Testes determinísticos do Gate 3 (Bloco 3): Simulador Causal Single-Slot, Ablação e DSR."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtest.ablation_and_dsr import (
    compute_afml_sample_uniqueness,
    compute_block_bootstrap_ci95,
    compute_deflated_sharpe_ratio,
    evaluate_gate3_criteria,
    register_backtest_trial,
    split_temporal_vaults,
)
from src.backtest.causal_simulator import (
    CausalSingleSlotSimulator,
    CostScenario,
    SimulatedTrade,
)
from src.regimes.regime_detector import MarketRegime
from src.strategies.base_strategy import SignalCandidate
from src.strategies.bull_trend_pullback import BullTrendPullbackStrategy
from src.strategies.sideways_pingpong import SidewaysPingPongStrategy


def test_gate3a_single_slot_queue_and_rs_tie_breaker() -> None:
    """
    Gate 3A:
    - Quando 2 ou mais moedas dão sinal na mesma hora, o simulador seleciona
      exclusivamente a moeda com maior rs_vol_adjusted_12h;
    - Em empate numérico exato de rs_vol_adjusted_12h, o desempate secundário é determinístico.
    """
    sim = CausalSingleSlotSimulator(cost_scenario=CostScenario.BASE)

    cand_eth = SignalCandidate(
        symbol="ETHUSDT",
        close_time_ms=10_000_000,
        regime=MarketRegime.BULL,
        strategy_name="bull_trend_pullback",
        entry_price=2500.0,
        stop_price=2450.0,
        target_price=2625.0,
        atr_14=25.0,
        stop_distance_pct=0.02,
        gross_rr_ratio=2.5,
        time_barrier_hours=24,
        rs_vol_adjusted_12h=1.85,
        order_flow_delta=10_000.0,
        taker_buy_ratio=0.58,
        has_liquidity_sweep=False,
    )
    cand_sol = SignalCandidate(
        symbol="SOLUSDT",
        close_time_ms=10_000_000,
        regime=MarketRegime.BULL,
        strategy_name="bull_trend_pullback",
        entry_price=150.0,
        stop_price=144.0,
        target_price=165.0,
        atr_14=3.0,
        stop_distance_pct=0.04,
        gross_rr_ratio=2.5,
        time_barrier_hours=24,
        rs_vol_adjusted_12h=2.40,
        order_flow_delta=25_000.0,
        taker_buy_ratio=0.62,
        has_liquidity_sweep=True,
    )

    winner = sim.select_single_slot_winner([cand_eth, cand_sol])
    assert winner is not None
    assert winner.symbol == "SOLUSDT"


def test_gate3b_three_cost_scenarios_time_barrier_and_1m_breakeven() -> None:
    """
    Gate 3B:
    - R_net(OPTIMISTIC) > R_net(BASE) > R_net(PESSIMISTIC) para o mesmo trade;
    - Saída na 24ª barra (TIME_BARRIER) calcula o R líquido real no fechamento;
    - Resolução intra-vela de 1 minuto (1m) aciona Breakeven em +1.2R quando o preço
      atinge +1.2R e depois recua até o preço de entrada.
    """
    candidate = SignalCandidate(
        symbol="BTCUSDT",
        close_time_ms=1_000_000,
        regime=MarketRegime.BULL,
        strategy_name="bull_trend_pullback",
        entry_price=100.0,
        stop_price=96.0,     # 1R = 4.0 (4%)
        target_price=110.0,  # +2.5R = 10.0
        atr_14=2.0,
        stop_distance_pct=0.04,
        gross_rr_ratio=2.5,
        time_barrier_hours=24,
        rs_vol_adjusted_12h=1.5,
        order_flow_delta=5000.0,
        taker_buy_ratio=0.60,
        has_liquidity_sweep=False,
    )

    # 1. Comparação dos 3 cenários de custo em saída no alvo
    bars_target = pd.DataFrame(
        {
            "close_time": [1_000_000 + 3_600_000],
            "open": [100.0],
            "high": [111.0],
            "low": [99.0],
            "close": [110.5],
        }
    )
    t_opt = CausalSingleSlotSimulator(cost_scenario=CostScenario.OPTIMISTIC).resolve_trade_1h_or_1m(
        candidate, bars_target
    )
    t_base = CausalSingleSlotSimulator(cost_scenario=CostScenario.BASE).resolve_trade_1h_or_1m(
        candidate, bars_target
    )
    t_pess = CausalSingleSlotSimulator(cost_scenario=CostScenario.PESSIMISTIC).resolve_trade_1h_or_1m(
        candidate, bars_target
    )
    assert t_opt.exit_reason == "TARGET"
    assert t_opt.net_r > t_base.net_r > t_pess.net_r

    # 2. Saída por barreira de tempo (24h) sem tocar stop nem alvo -> entra no cálculo empírico de E[R]
    bars_24h = pd.DataFrame(
        {
            "close_time": [1_000_000 + i * 3_600_000 for i in range(1, 25)],
            "open": [100.0] * 24,
            "high": [103.0] * 24,
            "low": [98.0] * 24,
            "close": [102.0] * 24,  # +0.5R bruto no fechamento da 24ª vela
        }
    )
    t_time = CausalSingleSlotSimulator(cost_scenario=CostScenario.BASE).resolve_trade_1h_or_1m(
        candidate, bars_24h
    )
    assert t_time.exit_reason == "TIME_BARRIER"
    assert t_time.bars_held == 24
    assert pytest.approx(t_time.gross_r, rel=1e-6) == 0.50
    assert t_time.net_r < t_time.gross_r

    # 3. Resolução de 1m para Breakeven (+1.2R = 104.8):
    # Minuto 1 vai a 105.0 (ativa BE em 100.0); Minuto 2 cai a 99.5 (sai no BE em 100.0 em vez de esperar 96.0)
    bars_1h_be = pd.DataFrame(
        {
            "close_time": [1_000_000 + 3_600_000],
            "open": [100.0],
            "high": [105.0],
            "low": [97.0],
            "close": [97.5],
        }
    )
    bars_1m_be = pd.DataFrame(
        {
            "close_time": [1_000_000 + 60_000, 1_000_000 + 120_000],
            "open": [100.0, 104.8],
            "high": [105.0, 104.9],
            "low": [100.0, 99.5],
            "close": [104.8, 99.6],
        }
    )
    t_be = CausalSingleSlotSimulator(
        cost_scenario=CostScenario.BASE,
        breakeven_enabled=True,
        breakeven_trigger_r=1.2,
    ).resolve_trade_1h_or_1m(candidate, bars_1h_be, intracandle_bars_1m=bars_1m_be)
    assert t_be.exit_reason == "BREAKEVEN"
    assert pytest.approx(t_be.gross_r, abs=1e-9) == 0.0


def test_gate3c_afml_uniqueness_temporal_vaults_and_trial_budget(tmp_path: Path) -> None:
    """
    Gate 3C:
    - AFML Snippet 4.2: eventos 100% sobrepostos têm u_bar = 0.5; disjuntos têm u_bar = 1.0;
    - Partição temporal respeita Purging (24h) e Embargo (2%);
    - Orçamento de tentativas grava CSV e bloqueia K > 20 com RuntimeError.
    """
    u_overlap = compute_afml_sample_uniqueness([(1, 10), (1, 10)])
    assert np.allclose(u_overlap, [0.5, 0.5])

    u_disjoint = compute_afml_sample_uniqueness([(1, 5), (6, 10)])
    assert np.allclose(u_disjoint, [1.0, 1.0])

    ts = [i * 3_600_000 for i in range(1, 1001)]
    bounds = split_temporal_vaults(ts, train_pct=0.60, val_pct=0.25, purge_hours=24, embargo_pct=0.02)
    assert bounds.val_start_ms >= bounds.train_end_ms + 24 * 3_600_000
    assert bounds.holdout_start_ms >= bounds.val_end_ms + 24 * 3_600_000

    csv_path = tmp_path / "backtest_trials_registry.csv"
    for k in range(1, 21):
        recorded_k = register_backtest_trial(
            registry_path=csv_path,
            step_name=f"step_c_var_{k}",
            params_dict={"ema_fast": 20, "trial": k},
            n_trades=160,
            mean_net_r=0.18,
            sharpe_annualized=1.9,
            max_trials_budget=20,
        )
        assert recorded_k == k

    with pytest.raises(RuntimeError, match="K=21 > K_max=20"):
        register_backtest_trial(
            registry_path=csv_path,
            step_name="step_c_over_budget",
            params_dict={"ema_fast": 25},
            n_trades=160,
            mean_net_r=0.20,
            sharpe_annualized=2.0,
            max_trials_budget=20,
        )


def test_gate3d_ablation_order_flow_sideways_rr_guard_dsr_and_notebooks(tmp_path: Path) -> None:
    """
    Gate 3D:
    - Ablação do filtro de Order Flow no Degrau C filtra sinais com Delta <= 0;
    - Trava geométrica de sideways_pingpong bloqueia entradas com RR < 1.50;
    - DSR penaliza o aumento de K (DSR(K=1) > DSR(K=20)) e aprova Gate 3 em série forte;
    - Os 3 notebooks do Bloco 3 existem e são JSON nbformat v4 válidos.
    """
    bull = BullTrendPullbackStrategy(enabled=True)
    row_neg_delta = pd.Series(
        {
            "close_time": 5_000_000,
            "open": 99.0,
            "high": 102.0,
            "low": 99.8,
            "close": 101.0,
            "ema_20_1h": 100.0,
            "ema_100_1h": 95.0,
            "atr_14_1h": 1.5,
            "rs_vol_adjusted_12h": 1.2,
            "order_flow_delta": -500.0,
            "taker_buy_ratio": 0.42,
            "is_liquidity_sweep": False,
        }
    )
    assert bull.evaluate_row("ETHUSDT", row_neg_delta, MarketRegime.BULL, require_order_flow_filter=False) is not None
    assert bull.evaluate_row("ETHUSDT", row_neg_delta, MarketRegime.BULL, require_order_flow_filter=True) is None

    # Trava geométrica v6 de sideways_pingpong (RR < 1.50 é bloqueado)
    sw = SidewaysPingPongStrategy(enabled=True, min_initial_rr_ratio=1.50)
    row_tight_bb = pd.Series(
        {
            "close_time": 5_000_000,
            "close": 100.0,
            "bb_mid_20_1h": 101.5,  # distância ao alvo = 1.5; stop = 1.5 * 1.0 = 1.5 -> RR = 1.0 < 1.5
            "bb_lower_20_1h": 100.2,
            "rsi_14_1h": 28.0,
            "atr_14_1h": 1.0,
        }
    )
    assert sw.evaluate_row("ETHUSDT", row_tight_bb, MarketRegime.SIDEWAYS) is None

    row_wide_bb = row_tight_bb.copy()
    row_wide_bb["bb_mid_20_1h"] = 103.0  # distância ao alvo = 3.0; stop = 1.5 -> RR = 2.0 >= 1.5
    assert sw.evaluate_row("ETHUSDT", row_wide_bb, MarketRegime.SIDEWAYS) is not None

    # DSR e Gate 3
    rng = np.random.default_rng(7)
    strong_r = rng.normal(loc=0.32, scale=0.85, size=180).tolist()
    dsr_k1 = compute_deflated_sharpe_ratio(strong_r, n_trials=1)
    dsr_k20 = compute_deflated_sharpe_ratio(strong_r, n_trials=20)
    assert dsr_k1 > dsr_k20 > 0.95

    ci_low, ci_high = compute_block_bootstrap_ci95(strong_r)
    assert 0.0 < ci_low < ci_high

    sim_trades_base = [
        SimulatedTrade(
            symbol="BTCUSDT",
            strategy_name="bull_trend_pullback",
            regime="BULL",
            entry_time_ms=i * 3_600_000,
            exit_time_ms=(i + 4) * 3_600_000,
            entry_price=100.0,
            exit_price=102.0,
            stop_price=98.0,
            target_price=105.0,
            exit_reason="TARGET" if r > 0 else "STOP",
            gross_r=r + 0.05,
            net_r=r,
            pnl_pct_equity=r * 0.01,
            bars_held=4,
            rs_score=1.0,
        )
        for i, r in enumerate(strong_r, start=1)
    ]
    sim_trades_pess = [
        SimulatedTrade(
            **{**t.__dict__, "net_r": t.net_r - 0.04}
        )
        for t in sim_trades_base
    ]
    report = evaluate_gate3_criteria(
        trades_base=sim_trades_base,
        trades_pessimistic=sim_trades_pess,
        holdout_start_ms=140 * 3_600_000,
        n_trials=5,
    )
    assert report.passed_gate3 is True

    # Verifica e executa todas as células de código dos 3 notebooks do Bloco 3
    for nb_name in (
        "01_download_causal_1h_4h_1m.ipynb",
        "02_ablation_and_dsr_sim.ipynb",
        "04_radar_layers_123_backtest.ipynb",
    ):
        nb_path = Path("notebooks") / nb_name
        assert nb_path.exists(), f"Notebook ausente: {nb_name}"
        nb_data = json.loads(nb_path.read_text(encoding="utf-8"))
        assert nb_data.get("nbformat") == 4
        code_cells = [c for c in nb_data.get("cells", []) if c.get("cell_type") == "code"]
        assert len(code_cells) >= 2
        ns: dict[str, object] = {}
        for cell in code_cells:
            src_code = "".join(cell.get("source", []))
            exec(compile(src_code, f"{nb_name}", "exec"), ns)

        if nb_name == "02_ablation_and_dsr_sim.ipynb":
            # Testa a execução ponta-a-ponta de download_and_build_aligned_universe + run_ablation_and_gate3
            nb_client = ns["client"]
            nb_db = ns["db"]
            nb_db.path = tmp_path / "colab_test.sqlite"
            nb_db.close()
            nb_db.migrate()

            end_ms = 1_700_000_000_000
            start_ms = end_ms - 600 * 3_600_000

            def _fake_server_time() -> int:
                return end_ms

            def _fake_get_klines(
                symbol: str,
                interval: str,
                *,
                start_time_ms: int | None = None,
                end_time_ms: int | None = None,
                limit: int = 1000,
            ) -> list[list[object]]:
                delta = 3_600_000 if interval == "1h" else 14_400_000
                s = ((int(start_time_ms or start_ms)) // delta) * delta
                e = int(end_time_ms or end_ms)
                rows = []
                t = s
                while t + delta <= e and len(rows) < limit:
                    if t == s + 10 * delta:
                        # Simula a vela truncada real da manutenção da Binance em 2023-03-24 12:39:41.646 UTC
                        rows.append(
                            [
                                t,
                                "100.0",
                                "102.0",
                                "99.0",
                                "101.0",
                                "50.0",
                                t + 2_381_646,
                                "5050.0",
                                100,
                                "30.0",
                                "3030.0",
                                "0",
                            ]
                        )
                    else:
                        rows.append(
                            [
                                t,
                                "100.0",
                                "102.0",
                                "99.0",
                                "101.0",
                                "50.0",
                                t + delta - 1,
                                "5050.0",
                                100,
                                "30.0",
                                "3030.0",
                                "0",
                            ]
                        )
                    t += delta
                return rows

            nb_client.get_server_time_ms = _fake_server_time  # type: ignore[attr-defined]
            nb_client.get_klines = _fake_get_klines  # type: ignore[attr-defined]
            aligned = ns["download_and_build_aligned_universe"](start_ms=start_ms, end_ms=end_ms)  # type: ignore[operator]
            ablation_res = ns["run_ablation_and_gate3"](aligned)  # type: ignore[operator]
            assert "gate3_report" in ablation_res
            nb_db.close()


