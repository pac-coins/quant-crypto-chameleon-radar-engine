# Quadro geral de triagem (Pendentes — Bloco 6)

> **Nota:** Todos os itens de Categoria 1 e Categoria 2 já concluídos e homologados nos **Blocos 1 a 5** (bem como a tabela de sugestões cosméticas descartadas da Categoria 3) foram migrados para a documentação permanente em [Metodologia e Roadmap — Seção 6](methodology-and-roadmap.md#6-registro-historico-de-triagem-homologada-blocos-1-a-5-e-itens-cosmeticos-descartados).

| Categoria | Item Validado Pendente | Quem Apontou | Onde Entra | Impacto Prático |
| --- | --- | --- | --- | --- |
| **1 — Catastrófico** | Orquestração de Kill-Switches no loop principal e Manutenção API (`/sapi/v1/system/status`) | Gemini e DeepSeek | Bloco 6 (`main.py`) | Bloqueia novas compras 2h antes de manutenção ou em `EMERGENCY_HALT` (regras de `-12%` DD e BNB `< $2` já prontas em `shadow_manager.py`) |
| **1 — Catastrófico** | Banda efetiva simétrica no `/api/v3/depth` (`band_eff` e `obi_valid`) (**C1-A**) | Auditoria de Integração | Bloco 6 — Radar L4 (`orderbook_inspector.py`) | Evita OBI e Whale Walls inválidos em BTC/ETH |
| **2 — Refinamento** | Faixas de 0,1% do livro ancoradas em `mid_0` (**C2-1**) | Auditoria de Integração | Bloco 6 — Radar L4 (`orderbook_inspector.py`) | Evita deslizamento de faixas entre as 3 fotos |
| **2 — Refinamento** | Auditor inicia em `t+1` usando `ask` do snapshot 0 (**C2-2**) | Auditoria de Integração | Bloco 6 (`src/auditing/virtual_auditor.py`) | Entrada realista alinhada ao atraso do alerta |
| **2 — Refinamento** | Horizonte por tempo (`*_end_ms`) no Auditor Virtual (**C2-7**) | Auditoria de Integração | Bloco 6 (`src/auditing/virtual_auditor.py`) | Imunidade a janelas de manutenção da Binance (detecção de gaps e `outcome_resolver.py` já prontos nos Blocos 1 e 5) |
| **2 — Refinamento** | Execução do Chameleon antes da L4; 3 fotos intercaladas (**C2-9**) | Auditoria de Integração | Bloco 6 (`main.py` / `orderbook_inspector.py`) | Não adiciona 10–15 s de latência ao slippage do Gate 6B |

---

# Checklist de Execução Pendente (Bloco 6)

## Bloco 6: Radar (L1–L5), Auditor Virtual, Observabilidade e Graduação de Fases
- [ ] Implementar `src/radar/` (`structure_ema.py`, `orderbook_inspector.py` com `band_eff` e grade ancorada, `confluence_scorer.py`) e `src/auditing/` (`virtual_auditor.py` apoiado em `outcome_resolver.py`) + `pytest tests/test_radar_orderbook_math.py`.
- [ ] Implementar `src/notifications/telegram.py` (outbox persistente) e `src/main.py` (orquestrando os 3 `RunMode`s, checagem `/sapi/v1/system/status` e reserva de BNB).
- [ ] Aprovar **Gate 6A** (7 dias na Testnet em `LIVE_CHAMELEON_PHASE_1` + teste de restauração do backup SQLite) e **Gate 6B** (30 dias ou 25 trades em `SHADOW_CHAMELEON` com dados reais — atuando como *Holdout Prospectivo Verdadeiro*).
- [ ] Iniciar **Fase 1** com capital real (`LIVE_CHAMELEON_PHASE_1` + `PRODUCTION`, apenas `BULL`, risco fixo de 1%, IA e Radar anotando em modo silencioso) até completar 60 trades reais para avaliar subida à **Fase 2**.


