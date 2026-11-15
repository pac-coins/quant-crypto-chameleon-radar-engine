# QuantCrypto Chameleon & Radar Engine

## Visão geral

O **QuantCrypto Chameleon & Radar Engine** consolida em **um único repositório e
um único processo Python** dois sistemas projetados para operar juntos na
Binance Spot (1h/4h, sem alavancagem):

* **Chameleon Engine (v6.0 — Braço Executor):** execução Spot blindada
  (Single-Slot, OCO, Reconciliador de Boot, Teto Duplo de Risco e segregação
  via carteira `FUNDING`).
* **Radar & Analyst Engine (v1.1 — Cérebro Analítico):** scanner multi-ativo
  (15 a 20 moedas) com funil de 5 camadas (EMA 9/50/200, Price Action, Order
  Flow e Order Book anti-spoofing) e Auditor Virtual no SQLite.

As decisões centrais da arquitetura unificada são:

* **fronteira estrita:** na Fase 1, o Radar é 100% analítico (anotação
  silenciosa) e apenas `bull_trend_pullback` emite sinais executáveis;
* **três modos via `.env`:** `ANALYST_ONLY`, `SHADOW_CHAMELEON` e
  `LIVE_CHAMELEON_PHASE_1`, com chaves Binance proibidas nos dois primeiros;
* **processo único e SQLite WAL:** respeita o teto de 100 MB de RAM em VPS
  gratuita e elimina contenção de escrita via `BEGIN IMMEDIATE`;
* **governador de peso compartilhado:** prioridades `CRITICAL > HIGH > LOW` e
  `critical_section()` protegem a criação da OCO contra `429`/`418` do `depth`;
* **causalidade e rigor estatístico:** alinhamento por `close_time_utc`,
  `FeatureStore` único, `outcome_resolver` compartilhado e validação por
  gates `PASS`/`FAIL`.

## Navegação rápida

1. **[Arquitetura Unificada](unified-architecture.md):** papéis, modos, processo
   único, SQLite WAL, orçamento de rate limit, `FeatureStore` e árvore de arquivos.
2. **[Auditoria de Integração](integration-audit.md):** 5 falhas inéditas de
   Categoria 1 (C1-A a C1-E) e 9 refinamentos de Categoria 2 (C2-1 a C2-9).
3. **[Contrato de Dados](data-contract.md):** schema SQL completo (v1) e
   máquina de estados de `trades`.
4. **[Especificação do Bloco 1](block-1-spec.md):** assinaturas (`type hints`)
   de `config.py`, `database.py`, `binance_client.py` e `candle_collector.py`,
   além do Gate 1 unificado.
5. **[Executor (Chameleon v6)](architecture.md)** e **[Radar & Auditor (v1.1)](radar.md):**
   especificações matemáticas e operacionais de cada subsistema.
6. **[Decisões (ADR)](decisions.md)**, **[Metodologia e roadmap](methodology-and-roadmap.md)**
   e **[TODO](todo.md):** governança e lista de triagem atualizada.

> **Aviso:** este material é uma especificação técnica. Não constitui
> recomendação financeira e não autoriza operação real sem a validação dos
> gates descritos na metodologia.
