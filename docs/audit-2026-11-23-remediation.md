# Registro de Remediação — Auditoria Adversarial (2026-11-23)

* **Data:** 2026-11-23
* **Branch:** `audit/2026-11-23-adversarial`
* **Relatório original (imutável):** `docs/audit-2026-11-23.md` (commit `24de206` — mantido intacto, sem edição retroativa, no padrão ADR).
* **Escopo resolvido:** **17/17 achados** (11 Categoria 1, 5 Categoria 2, 1 Categoria 3).
* **Integridade preservada:**
  * `config/strategies.yaml` e `src/risk/position_sizer.py` **não alterados** (`git diff main -- config/strategies.yaml src/risk/position_sizer.py` vazio).
  * Holdout **não executado** (`docs/methodology-and-roadmap.md` §2).
  * Nenhum deploy executado e nenhum container reiniciado.
  * Suíte permanente de regressão em `tests/test_audit_2026_11_23_regressions.py` (**26 testes novos** somados aos **31 testes existentes** = **57/57 passando**).

---

## Resumo dos Commits de Remediação

| # | Commit | Escopo | Achados |
|---|---|---|---|
| 1 | `692ebd0` | `src/core/binance_client.py` | A1-04, A2-03 + APIs de consulta (`get_order(order_id=...)`, `get_order_list(orig_client_order_id=...)`, `get_my_trades`) |
| 2 | `0eba5e9` | `src/execution/oco_order_manager.py` | A1-03b, A1-11 (a/b/c), A2-04 |
| 3 | `ec13406` | `src/execution/oco_order_manager.py` | A1-02, A1-05 (a/b), A1-07 |
| 4 | `ad16d91` | `src/core/state_reconciler.py` | A1-01, A1-03a, A1-06, A1-10, A2-01 (reconciler), A2-02 |
| 5 | `fd320db` | `src/main.py`, `src/data/candle_collector.py` | A1-08, A1-09, A2-01 (wiring), A2-05 (a/b) |
| 6 | `d0327ff` | `src/main.py`, `src/execution/__init__.py`, `src/notifications/telegram.py` | A3-01 (a/b/c) |

---

## Rastreabilidade por Achado

### Categoria 1 — Catastróficos

| ID | Status | Commit | Código corrigido (`arquivo:linha`) | Testes de regressão (`tests/test_audit_2026_11_23_regressions.py`) |
|---|---|---|---|---|
| **A1-01** | Resolvido | `ad16d91` | `src/core/state_reconciler.py:585-698` (handlers explícitos para `HALT_REVIEW` -> `HALT_REVIEW_PERSISTENT` e `CLOSING` -> cancelamento de OCO residual + venda a mercado limitada ao rastreado -> `RECOVERED_CLOSING_TO_CLOSED`) | `test_audit_a1_01_reconciler_heals_closing_and_never_returns_clean_for_halt_review` (`:1039`) |
| **A1-02** | Resolvido | `ec13406` | `src/execution/oco_order_manager.py:1435-1486` (`close_on_time_barrier` valida `listOrderStatus == "ALL_DONE"` após erro em `cancel_order_list`; se seguir ativa, reverte `trades.status` para `PROTECTED` e relança o erro sem marcar `CLOSED`) | `test_audit_a1_02_time_stop_must_not_mark_closed_when_cancel_fails_and_oco_still_executing` (`:642`) |
| **A1-03** | Resolvido | `0eba5e9`, `ad16d91` | **A1-03a:** `src/core/state_reconciler.py:187-278,427-467,707-745` (`_find_open_oco_for_symbol` + `_heal_existing_oco_record` anexam OCO já ativa pós-C1-E e encerram `free=0, locked=0` sem enviar `qty=0`). **A1-03b:** `src/execution/oco_order_manager.py:592-701,855-887` (`_recover_oco_after_timeout` + persistência pré-POST de `oco_list_client_id` + recuperação idempotente de `RequestTimeoutUnknownState`) | `test_audit_a1_03_reconciler_heals_after_c1e_when_oco_already_exists_on_exchange` (`:1123`), `test_audit_a1_03b_oco_post_timeout_recovers_idempotently` (`:225`) |
| **A1-04** | Resolvido | `692ebd0` | `src/core/binance_client.py:710-766` (`signed_request` converte qualquer `HTTP 5XX` ou `-1007` em `POST` para `RequestTimeoutUnknownState`) | `test_audit_a1_04_http_503_on_market_buy_raises_unknown_state` (`:138`) |
| **A1-05** | Resolvido | `ec13406` | `src/execution/oco_order_manager.py:1123-1433` (`check_hanging_stop_watchdog` dentro de `governor.critical_section()`; teto C1-D em `tracked_qty`; em falha de `get_account` usa `remaining_from_oco` limitado ao rastreado; se a venda falhar por saldo insuficiente `-2010`, relê o saldo e tenta uma única vez; se falhar novamente, chama `_enter_halt_review` com `trades.status = 'HALT_REVIEW'`, `emergency_halt = '1'` e alerta Telegram `KILL_SWITCH`) | `test_audit_a1_05a_watchdog_cancels_oco_then_uses_remaining_from_oco_on_account_error` (`:693`), `test_audit_a1_05a_watchdog_insufficient_balance_retries_once_then_halt_review_with_alert` (`:763`), `test_audit_a1_05b_watchdog_must_not_sell_untracked_manual_balance_beyond_oco_qty` (`:900`) |
| **A1-06** | Resolvido | `ad16d91` | `src/core/state_reconciler.py:816-845` (em `PROTECTED` com OCO `EXECUTING`, após `cancel_order_list` em `critical_section()`, reconsulta `_fetch_account_balances()` e protege `min(new_free_b, tracked_qty)`) | `test_audit_a1_06_reconciler_protected_canceling_executing_oco_must_protect_unlocked_qty_too` (`:1258`) |
| **A1-07** | Resolvido | `ec13406` | `src/execution/oco_order_manager.py:1060-1117,1488-1511` (`_extract_oco_executed_qty` lê `orderReports[]` quando presente e consulta cada perna de `orders[]` via `get_order` no schema oficial de `GET /api/v3/orderList`) | `test_audit_a1_07_time_stop_get_order_list_has_no_order_reports_and_oversells` (`:955`) |
| **A1-08** | Resolvido | `fd320db` | `src/main.py:821-859` (`stale_set` exclui símbolos `stale` do carregamento de features, Chameleon e Radar; `cycle_close_ms` calculado como `max(observed_closes)` apenas sobre símbolos não-stale) | `test_audit_a1_08_run_once_must_exclude_stale_symbols_from_chameleon_entries` (`:1585`) |
| **A1-09** | Resolvido | `fd320db` | `src/main.py:396-480,757-804` (`_manage_live_open_positions` executa em `run_once` antes de `exchange_cache.refresh_if_stale` e `collector.run_cycle`; `refresh_if_stale` degrada para cache quando o universo de execução já está presente) e `src/data/candle_collector.py:244-354` (isolamento por símbolo de erros transitórios e `KlineIntegrityError` em `collect_closed`/`repair_gaps`) | `test_audit_a1_09_radar_symbol_kline_failure_must_not_block_live_time_stop` (`:1706`) |
| **A1-10** | Resolvido | `ad16d91` | `src/core/state_reconciler.py:165-185,530-578` (Step 2 C1-D falha fechado em `HALT_REVIEW_UNTRACKED_BALANCE` se `get_book_ticker` e o fallback de `candles` não conseguirem comprovar que o excedente está abaixo de `min_notional`) | `test_audit_a1_10_reconciler_c1d_must_fail_closed_when_book_ticker_errors` (`:1318`) |
| **A1-11** | Resolvido | `0eba5e9` | `src/execution/oco_order_manager.py:302-360` (pré-checagem de `status == "TRADING"`, `oco_allowed` e `passes_filters` na perna `STOP_LOSS_LIMIT` e `tp_price` antes da compra — **A1-11c**), `src/execution/oco_order_manager.py:795-964` (saída `ExitReason.TARGET` quando `bid >= tp_price` ou `-2010` de relação de preços — **A1-11a**; remoção do fallback cego `is_immediate_trigger = True` em erro de ticker para `-2010` alheio — **A1-11b**) | `test_audit_a1_11a_price_above_take_profit_exits_at_market_target` (`:282`), `test_audit_a1_11b_unrelated_2010_plus_ticker_error_does_not_liquidate_healthy_position` (`:344`), `test_audit_a1_11c_preflight_rejects_lot_failing_oco_stop_limit_notional_or_oco_disabled` (`:406`) |

### Categoria 2 — Refinamentos Úteis

| ID | Status | Commit | Código corrigido (`arquivo:linha`) | Testes de regressão (`tests/test_audit_2026_11_23_regressions.py`) |
|---|---|---|---|---|
| **A2-01** | Resolvido | `ad16d91`, `fd320db` | `src/core/state_reconciler.py:108-121,340-346,796-798` (`get_order_list` não engole `BinanceAPIError`; toda falha em `reconcile()` notifica `shadow_manager.record_reconciliation_outcome(success=False, binance_error_code=...)` e dispara HALT imediato em `-1002/-1022/-2014/-2015` ou após 3 falhas) + `src/main.py:224-252,396-435` (alerta `KILL_SWITCH` e bloqueio de novas entradas) | `test_audit_a2_01_reconciler_auth_error_2015_and_3_failures_trigger_emergency_halt` (`:1358`) |
| **A2-02** | Resolvido | `ad16d91` | `src/core/state_reconciler.py:280-338,808-978` (avaliação de resíduo livre em `PROTECTED` usando preço ao vivo `bid` alinhado ao Step 2 C1-D; cálculo real de `exit_reason`, `exit_avg_price`, `exit_quote_received`, `realized_pnl_usdt`, `realized_r` a partir das pernas preenchidas da OCO + chamada a `shadow_manager.record_trade_outcome`; prorrateamento de `quote_spent` em saídas parciais) | `test_audit_a2_02_completed_oco_records_real_exit_reason_pnl_and_shadow_outcome` (`:1433`), `test_audit_a2_02b_completed_oco_dust_revalued_at_bid_avoids_false_halt` (`:1522`) |
| **A2-03** | Resolvido | `692ebd0` | `src/core/binance_client.py:75-105,137-205,833-863` (janela de `WeightGovernor` baseada em tempo decorrido `_window_started_at >= 60.0s` + detecção de virada autoritativa via header `X-MBX-USED-WEIGHT-1M`; `retry_after_s` preservado em `RateLimitedError`/`IPBannedError`; SAPI com `weight=0` no pool Spot) | `test_audit_a2_03_weight_governor_window_and_retry_after` (`:171`) |
| **A2-04** | Resolvido | `692ebd0`, `0eba5e9` | `src/core/binance_client.py:801-831` (`get_my_trades`) + `src/execution/oco_order_manager.py:238-300,715-793` (esgota `fills[]`, `get_my_trades` com retry e `get_account` com retry; somente após esgotar todas as 3 fontes aplica desconto conservador `-0,1%`, grava no log, persiste em `order_events` + `system_state` e dispara alerta Telegram `OCO_QTY_ESTIMATED`) | `test_audit_a2_04_commission_recovery_via_mytrades_and_conservative_estimate_alert` (`:488`) |
| **A2-05** | Resolvido | `fd320db` | `src/main.py:285-330,505-533` (`_count_trades_last_24h` exclui `status != 'ENTRY_REJECTED'`; `_get_live_balances` tenta último candle `BNBUSDT` no SQLite e falha fechado em `Decimal("0")` em vez de fabricar `$10.00`; consulta best-effort de `get_account_commission` passada a `evaluate_defense`) | `test_audit_a2_05a_rejected_entries_must_not_trigger_daily_trade_cap` (`:1816`), `test_audit_a2_05b_bnb_ticker_error_must_not_fabricate_10usd_reserve` (`:1872`) |

### Categoria 3 — Conformidade Estrutural com as Specs

| ID | Status | Commit | Código corrigido (`arquivo:linha`) | Testes de regressão (`tests/test_audit_2026_11_23_regressions.py`) |
|---|---|---|---|---|
| **A3-01** | Resolvido | `d0327ff` | **A3-01a:** `src/main.py:38-40,159-185,224-225,407-408` + `src/execution/__init__.py:14-35` (importação preguiçosa via `TYPE_CHECKING` e PEP 562 `__getattr__`, garantindo que `oco_order_manager` e `state_reconciler` não sejam importados fora de `LIVE_CHAMELEON_PHASE_1`). **A3-01b:** `src/notifications/telegram.py:162-175` + `src/main.py:674-706` (rodapé `"⚠️ Ativo gerido pelo executor — não opere nesta conta."` em cards do Radar em modo `LIVE` para ativos da cesta de execução). **A3-01c:** `src/main.py:721-755,893-900` (`_backfill_trade_radar_signal_id` anota `trades`/`shadow_trades.radar_signal_id` pós-Radar preservando ordem C2-9 e FK `virtual_signals`) | `test_audit_a3_01a_main_must_not_import_live_executor_modules_outside_live` (`:1954`), `test_audit_a3_01b_live_radar_card_includes_c1d_execution_basket_footer` (`:2013`), `test_audit_a3_01c_radar_signal_id_backfilled_on_trade_preserving_fk` (`:2144`) |

---

## Notas Operacionais

* Todos os testes acima validam o comportamento de forma determinística em SQLite temporário e dublês da Binance Spot API; o comportamento de rede em produção/Testnet ao vivo não foi executado diretamente durante esta remediação (em conformidade com a restrição de não reiniciar containers nem alterar serviços em execução no Gate 6A).
