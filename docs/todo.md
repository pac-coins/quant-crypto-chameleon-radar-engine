# Quadro geral de triagem e execução operacional (Pós-Construção)

> **Nota:** Todos os itens de engenharia e auditoria de integração dos **Blocos 1 a 6** (bem como a tabela de sugestões cosméticas descartadas da Categoria 3) foram implementados, homologados por `pytest` (29/29) e migrados para a documentação permanente em [Metodologia e Roadmap — Seção 6](methodology-and-roadmap.md#6-registro-historico-de-triagem-homologada-blocos-1-a-6-e-itens-cosmeticos-descartados).

---

# Checklist de Execução Operacional (Gates 6A, 6B e Fase 1)

## Validação em Ambiente Real (Zero Dinheiro Real em Risco)
- [ ] Aprovar **Gate 6A**: rodar 7 dias contínuos na **Binance Spot Testnet** (`MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=TESTNET`) com 0 unhandled exceptions, relógio dentro de $\pm 500\text{ ms}$ e teste real de restauração do backup SQLite.
- [ ] Aprovar **Gate 6B**: rodar 30 dias ou 25 operações em **Shadow Live** (`MODE=SHADOW_CHAMELEON`) lendo dados públicos reais (`data-api.binance.vision`) — atuando como *Holdout Prospectivo Verdadeiro* — e confirmar slippage médio $\le 0{,}08\%$.

## Operação com Capital Real
- [ ] Iniciar **Fase 1** (`MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=PRODUCTION`, apenas `BULL`, risco fixo de 1%, IA e Radar anotando em modo silencioso) até completar 60 trades reais para avaliar subida à **Fase 2**.
