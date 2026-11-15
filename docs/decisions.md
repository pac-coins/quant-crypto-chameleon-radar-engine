# Registro de decisões de arquitetura (ADR)

Cada decisão abaixo é **vinculante** para a codificação. Mudar qualquer uma
delas exige registrar um novo ADR que a substitua (*supersede*). Nunca se
edita a decisão original em silêncio.

| ADR | Decisão | Status | Motivo principal | Referência |
| --- | --- | --- | --- | --- |
| **ADR-001** | Radar e Chameleon rodam em **um único processo Python** (uma thread de orquestração, um container com `mem_limit: 100m`) | Aceito | `pandas` + `numpy` custam cerca de 40 a 60 MB; dois processos estouram 100 MB e criam dois escritores no SQLite | [Unificada §3](unified-architecture.md#3-processo-unico-e-orcamento-de-hardware) |
| **ADR-002** | SQLite WAL com **uma única conexão escritora**, `BEGIN IMMEDIATE`, `busy_timeout=10000`, sem I/O de rede dentro de transações; escritas pós-fill nunca interrompem a sequência compra → OCO | Aceito | Elimina `database is locked`, `BUSY_SNAPSHOT` e C1-E | [Unificada §5](unified-architecture.md#5-contrato-de-concorrencia-do-sqlite) |
| **ADR-003** | Na Fase 1, o executor consome **apenas** `StrategySignal` de `bull_trend_pullback`; o Radar é anotação silenciosa | Aceito | Plano do Radar sem Gate 3, alvos parciais incompatíveis com OCO única, vazamento de universo (C1-C) | [Auditoria C1-C](integration-audit.md#c1-c) |
| **ADR-004** | O livro de ofertas é **somente analítico** (`affects_execution: Literal[False]`) | Aceito | Mantém o descarte Cat. 3 do TODO como gatilho de execução | [Radar §2](radar.md) |
| **ADR-005** | `WeightGovernor` com prioridades `CRITICAL > HIGH > LOW`, `critical_section()` entre fill e OCO, teto `k_max = 5` de inspeções de livro por ciclo e header `X-MBX-USED-WEIGHT-1M` como fonte de verdade | Aceito | C1-B (429/418 deixando posição nua) | [Auditoria C1-B](integration-audit.md#c1-b) |
| **ADR-006** | Ordem do ciclo: coleta → features/regime → **execução** → Radar L1–L3 → L4 intercalada → Telegram → Auditor | Aceito | O caminho crítico de execução não espera 10 a 15 s de fotos do livro (slippage do Gate 6B) | [Unificada §4](unified-architecture.md#4-ciclo-horario-ordem-de-execucao) |
| **ADR-007** | `FeatureStore` único: os consumidores declaram `FeatureRequest`, e a união é calculada uma vez | Aceito | EMA 20/100 e EMA 9/50/200 sem código duplicado | [Unificada §7](unified-architecture.md#7-harmonizacao-de-indicadores-ema-20100-ema-950200) |
| **ADR-008** | O clima tem **fonte única** (`regime_detector`); o limiar de CHOP do Radar só pontua | Aceito | Evita "BULL LIMPO" no Telegram com o executor em `TRANSITION` | [Unificada §7](unified-architecture.md#7-harmonizacao-de-indicadores-ema-20100-ema-950200) |
| **ADR-009** | Dois YAMLs (`strategies.yaml` congelado com SHA-256 e `analyst.yaml` livre) e **um único modelo de custo** | Aceito | Ajustar o Radar não contamina o holdout; estatísticas comparáveis | [Unificada §7](unified-architecture.md#7-harmonizacao-de-indicadores-ema-20100-ema-950200) |
| **ADR-010** | Três modos via `MODE`: `ANALYST_ONLY` e `SHADOW_CHAMELEON` **recusam** chaves da Binance; `LIVE_CHAMELEON_PHASE_1` exige chaves Ed25519, e, em produção, `LIVE_CONFIRMATION` e o hash aprovado | Aceito | Menor privilégio e trava contra ligar o real por engano | [Unificada §2](unified-architecture.md#2-os-tres-modos-de-operacao) |
| **ADR-011** | Single-Slot também garantido **pelo banco** (índice parcial único em `trades` e `shadow_trades`) | Aceito | Defesa em profundidade contra uma 2ª posição | [Contrato de Dados](data-contract.md) |
| **ADR-012** | Saldo excedente não rastreado em ativo da cesta → `HALT_REVIEW`, **nunca** venda automática; trades manuais apenas em subconta | Aceito | C1-D | [Auditoria C1-D](integration-audit.md#c1-d) |
| **ADR-013** | Market data sem chave via `data-api.binance.vision` nos modos `ANALYST_ONLY` e `SHADOW_CHAMELEON`; em LIVE, dados e ordens vêm do **mesmo ambiente** (testnet ou produção) | Aceito | Evita preços de produção contra ordens na testnet | [Unificada §2](unified-architecture.md#2-os-tres-modos-de-operacao) |
| **ADR-014** | O repositório passa a se chamar `quant-crypto-chameleon-radar-engine` | Aceito (renomeação pendente) | Reflete o sistema unificado | [README](https://github.com/pac-coins/quant-crypto-chameleon-engine-v6#renomeação-do-repositório) |
