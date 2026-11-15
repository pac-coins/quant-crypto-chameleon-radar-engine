# Quadro geral de triagem (Pendentes — Blocos 5 e 6)

> **Nota:** Todos os itens de Categoria 1 e Categoria 2 já concluídos e homologados nos **Blocos 1 a 4** (bem como a tabela de sugestões cosméticas descartadas da Categoria 3) foram migrados para a documentação permanente em [Metodologia e Roadmap — Seção 6](methodology-and-roadmap.md#6-registro-historico-de-triagem-homologada-blocos-1-a-4-e-itens-cosmeticos-descartados).

| Categoria | Item Validado Pendente | Quem Apontou | Onde Entra | Impacto Prático |
| --- | --- | --- | --- | --- |
| **1 — Catastrófico** | Filtro exato do erro `-2010` e Stop Pendurado | ChatGPT e DeepSeek | Bloco 5 (`oco_order_manager.py`) | Evita vender errado ou ficar sem stop |
| **1 — Catastrófico** | Saldo excedente não rastreado entra em `HALT_REVIEW` (nunca vende posição manual) (**C1-D**) | Auditoria de Integração | Bloco 5 (`state_reconciler.py`) | Evita liquidar posição manual do operador na mesma conta |
| **1 — Catastrófico** | Escritas pós-fill *best-effort* nunca interrompem criação da OCO (**C1-E**) | Auditoria de Integração | Bloco 5 (`oco_order_manager.py`) | Garante que falha de SQLite pós-fill não deixe posição nua |
| **1 — Catastrófico** | Orquestração de Kill-Switches no loop principal e Manutenção API (`/sapi/v1/system/status`) | Gemini e DeepSeek | Bloco 6 (`main.py`) | Bloqueia novas compras 2h antes de manutenção ou em `EMERGENCY_HALT` (regras de `-12%` DD e BNB `< $2` já prontas em `shadow_manager.py`) |
| **1 — Catastrófico** | Banda efetiva simétrica no `/api/v3/depth` (`band_eff` e `obi_valid`) (**C1-A**) | Auditoria de Integração | Bloco 6 — Radar L4 (`orderbook_inspector.py`) | Evita OBI e Whale Walls inválidos em BTC/ETH |
| **2 — Refinamento** | `outcome_resolver.py` compartilhado (pior caso intra-vela) (**C2-3**) | Auditoria de Integração | Blocos 5 e 6 (`src/auditing/outcome_resolver.py` e `paper_executor.py`) | Evita divergência entre Auditor Virtual e Shadow Executor |
| **2 — Refinamento** | Faixas de 0,1% do livro ancoradas em `mid_0` (**C2-1**) | Auditoria de Integração | Bloco 6 — Radar L4 (`orderbook_inspector.py`) | Evita deslizamento de faixas entre as 3 fotos |
| **2 — Refinamento** | Auditor inicia em `t+1` usando `ask` do snapshot 0 (**C2-2**) | Auditoria de Integração | Bloco 6 (`src/auditing/virtual_auditor.py`) | Entrada realista alinhada ao atraso do alerta |
| **2 — Refinamento** | Horizonte por tempo (`*_end_ms`) no Auditor Virtual (**C2-7**) | Auditoria de Integração | Bloco 6 (`src/auditing/virtual_auditor.py`) | Imunidade a janelas de manutenção da Binance (detecção de gaps já pronta no coletor do Bloco 1) |
| **2 — Refinamento** | Execução do Chameleon antes da L4; 3 fotos intercaladas (**C2-9**) | Auditoria de Integração | Bloco 6 (`main.py` / `orderbook_inspector.py`) | Não adiciona 10–15 s de latência ao slippage do Gate 6B |

---

# Checklist de Execução Pendente (Blocos 5 e 6)

## Bloco 5: Execução OCO, Paper Executor e Cura no Boot
- [ ] Implementar `src/execution/executor_protocol.py` e `src/execution/paper_executor.py` (modo `SHADOW_CHAMELEON`, usando `bookTicker` real e `outcome_resolver.py`).
- [ ] Implementar `src/execution/oco_order_manager.py` descontando comissão no ativo base, tratando erro `-2010` com verificação de preço/mensagem para fallback `MARKET`, *watchdog* de stop pendurado, fechamento de 4 passos no Time-Stop e escritas pós-fill *best-effort* (C1-E).
- [ ] Implementar `src/core/state_reconciler.py` resolvendo `PENDING_ENTRY` via `origClientOrderId` (sem duplicar compra em caso de timeout `-1007`), curando execuções parciais de OCO e aplicando `HALT_REVIEW` para saldo excedente não rastreado (C1-D).
- [ ] Passar 100% no **Gate 5** (`pytest tests/test_block5_reconciler_and_oco.py` + 4 Testes de Tortura na Testnet).

## Bloco 6: Radar (L1–L5), Auditor Virtual, Observabilidade e Graduação de Fases
- [ ] Implementar `src/radar/` (`structure_ema.py`, `orderbook_inspector.py` com `band_eff` e grade ancorada, `confluence_scorer.py`) e `src/auditing/` (`outcome_resolver.py`, `virtual_auditor.py`) + `pytest tests/test_radar_orderbook_math.py`.
- [ ] Implementar `src/notifications/telegram.py` (outbox persistente) e `src/main.py` (orquestrando os 3 `RunMode`s, checagem `/sapi/v1/system/status` e reserva de BNB).
- [ ] Aprovar **Gate 6A** (7 dias na Testnet em `LIVE_CHAMELEON_PHASE_1` + teste de restauração do backup SQLite) e **Gate 6B** (30 dias ou 25 trades em `SHADOW_CHAMELEON` com dados reais — atuando como *Holdout Prospectivo Verdadeiro*).
- [ ] Iniciar **Fase 1** com capital real (`LIVE_CHAMELEON_PHASE_1` + `PRODUCTION`, apenas `BULL`, risco fixo de 1%, IA e Radar anotando em modo silencioso) até completar 60 trades reais para avaliar subida à **Fase 2**.

