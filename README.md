# QuantCrypto Chameleon & Radar Engine

Repositório unificado para o ecossistema **QuantCrypto** na Binance Spot (1h/4h, sem alavancagem), integrando em **um único processo Python** e **um único banco SQLite (WAL)** dois subsistemas complementares:

1. **QuantCrypto Chameleon Engine (v6.0 — Braço Executor):** motor de execução Spot blindado (Single-Slot, OCO, Reconciliador de Boot, Teto Duplo de Risco e segregação via carteira `FUNDING`).
2. **QuantCrypto Radar & Analyst Engine (v1.1 — Cérebro Analítico):** scanner multi-ativo (15 a 20 moedas) com funil de 5 camadas (EMA 9/50/200, Price Action, Order Flow e Order Book anti-spoofing) e Auditor Virtual no SQLite.

## Modos de operação

O sistema roda em três modos selecionáveis via `MODE` no `.env`:

- `ANALYST_ONLY` — roda apenas o Radar, a leitura do livro de ofertas, o Auditor Virtual e os alertas no Telegram (chaves privadas da Binance são proibidas no boot).
- `SHADOW_CHAMELEON` — adiciona o simulador do Chameleon (`paper_executor`) em dados reais de mercado, sem chaves privadas.
- `LIVE_CHAMELEON_PHASE_1` — execução real blindada na Binance Spot (Testnet ou Produção), restrita na Fase 1 ao sinal `bull_trend_pullback` e travada por `LIVE_CONFIRMATION` + hash SHA-256 do `strategies.yaml`.

## O que existe na documentação

- **Arquitetura Unificada:** papéis, fronteiras (`Radar` nunca envia ordens na Fase 1), os 3 modos, processo único (`< 100 MB` RAM), contrato de concorrência SQLite WAL, orçamento de `REQUEST_WEIGHT` da Binance, `FeatureStore` compartilhado e árvore de diretórios.
- **Executor (Chameleon v6.0):** separação Core/Satellite, regimes MECE, gestão de risco, execução OCO e resiliência operacional.
- **Radar & Auditor (v1.1):** funil de 5 camadas, banda efetiva simétrica no `/api/v3/depth`, grade ancorada para *Whale Walls* e Auditor Virtual.
- **Auditoria de Integração:** 5 falhas inéditas de Categoria 1 (C1-A a C1-E) e 9 refinamentos de Categoria 2 (C2-1 a C2-9) descobertos na unificação, com suas correções.
- **Contrato de Dados:** schema SQL completo (v1) validado em SQLite WAL, uso de tabelas por modo e máquina de estados de `trades`.
- **Especificações dos Blocos 1 e 2:** assinaturas exatas (`type hints`) de `config.py`, `database.py`, `binance_client.py` e `candle_collector.py` ([Bloco 1](docs/block-1-spec.md)) e de `indicators.py`, `order_flow.py`, `price_action.py`, `feature_store.py` e `regime_detector.py` ([Bloco 2](docs/block-2-spec.md)).
- **Decisões (ADR):** registro vinculante ADR-001 a ADR-014.
- **Metodologia, Roadmap e TODO:** construção em 6 blocos, gates `PASS`/`FAIL`, transições de fase, kill-switches e quadro de triagem atualizado.

## Documentação online

A documentação completa é publicada via MkDocs Material em
[pac-coins.github.io/quant-crypto-chameleon-radar-engine](https://pac-coins.github.io/quant-crypto-chameleon-radar-engine/).

Para visualizar localmente:

```bash
python -m pip install -r requirements-docs.txt
mkdocs serve
```

## Renomeação do repositório

O repositório foi renomeado para **`quant-crypto-chameleon-radar-engine`** (ADR-014) para refletir a unificação dos dois motores no mesmo código-fonte.

## Estado do projeto

O repositório contém a especificação consolidada, o contrato de dados e a especificação do Bloco 1 prontos para codificação. O robô não deve operar com capital real antes que os gates definidos na metodologia sejam aprovados e registrados.
