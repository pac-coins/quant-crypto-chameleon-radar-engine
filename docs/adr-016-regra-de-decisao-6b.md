# ADR-016 — Regra de decisão pré-registrada do Gate 6B

- **Status:** Aceito (pré-registrado antes de qualquer dado do Gate 6B)
- **Data:** 2026-11-24
- **Contexto:** `K9.3` (`bull_trend_pullback`), `MODE=SHADOW_CHAMELEON`
- **Referências vinculantes:** [Metodologia e Roadmap](methodology-and-roadmap.md) (§2.3, §4.1, §4.3, §5.2), [Auditoria do Backtest e Edge](backtest-edge-audit.md) (`AUD-02`, `AUD-03`, `AUD-07`), [ADR-009 / ADR-010](decisions.md)

---

## 1. Por que esta regra existe antes do Gate 6B

A auditoria do Bloco 3 ([Auditoria do Backtest e Edge](backtest-edge-audit.md)) registrou dois fatos críticos sobre a estratégia congelada **`K9.3`** (`n = 282` trades, $E[R]_{\text{net}} = +0{,}230\text{R}$, IC 95% $[+0{,}068\text{R};\, +0{,}392\text{R}]$, `PF = 1,62` base / `1,53` pessimista, ~1,3 trade/semana):

1. **`AUD-02`:** o cofre de Holdout histórico foi consultado em 4 candidatos (`K8.2`, `K9.0`, `K9.2`, `K9.3`) antes do congelamento.
2. **`AUD-03`:** o registro de tentativas do `DSR` contabilizou 9 ensaios em vez de $\ge 13$.

Por isso, o **Gate 6B** (`SHADOW_CHAMELEON` em tempo real, com book real via `COALESCE(depth_vwap_ask, decision_ask_price)`) é o **primeiro Holdout Prospectivo Verdadeiro** (*forward out-of-sample*, [Metodologia §2.3](methodology-and-roadmap.md)) com zero contaminação.

Para impedir o viés de mover a trave depois de ver o resultado, esta ADR fixa **antes do Dia 1 do Gate 6B** o significado exato de cada desfecho.

---

## 2. Amostra mínima e janela de avaliação

- **Horizonte mínimo operacional ([Metodologia §4.1](methodology-and-roadmap.md)):** pelo menos **30 dias corridos** em `SHADOW_CHAMELEON` **e** **25 trades fechados** (`shadow_trades.status = 'CLOSED'`) para uma decisão estatística definitiva de promoção a capital real.
- **Pré-requisito obrigatório de engenharia:** `Gate 6A` aprovado (168 ciclos horários consecutivos sem erro não tratado), `strategies.yaml` inalterado (SHA-256 intacto) e **zero** violações de kill-switch (`KS-1` a `KS-4 = PASS` em `tools/gate_report.py`).
- **Métrica de slippage:** sempre medida sobre o preço real de execução estimada (`COALESCE(depth_vwap_ask, decision_ask_price)`) contra o `close` da vela 1h no `signal_close_time_ms` (nunca pela constante de simulação).

---

## 3. Matriz de decisão (com $n \ge 25$ trades fechados)

Todos os retornos em `R` são **líquidos de taxa e slippage** (`shadow_trades.r_net`).

| Desfecho | Critérios exatos | Ação vinculante |
| --- | --- | --- |
| **1. Seguir para a Fase 1 (`GO`)** | **Todos simultaneamente:**<br>• $n \ge 25$ trades fechados e janela $\ge 30$ dias;<br>• $\overline{R}_{\text{net}} \ge +0{,}10\text{R}$ *(piso canônico do Gate 3 e da Fase 1→2, acima do IC 95% inferior histórico de $+0{,}068\text{R}$)*;<br>• Slippage médio realizado $\le 0{,}08\%$ ($\le 8{,}0\text{ bps}$);<br>• Frequência semanal dentro de $[0{,}25;\, 7{,}00]$ trades/semana;<br>• Drawdown máximo virtual $< 10\%$ e `KS-1..KS-4 = PASS`;<br>• `tools/preflight_check.py --target-gate phase1` aprovado (`PASS`), incluindo `ML_JUDGE_ARTIFACT_PATH` calibrado ($\text{ECE} \le 0{,}05$). | Promover **manualmente** para `LIVE_CHAMELEON_PHASE_1` + `PRODUCTION` (risco 1%, capital tático inicial, IA e Radar silenciosos), cumprindo `LIVE_CONFIRMATION` e `APPROVED_STRATEGIES_SHA256` (ADR-009/010). |
| **2. Ajustar / Investigar (`HOLD`)** | **Não atende `GO` nem `NO-GO`:**<br>• **Caso A (execução):** $\overline{R}_{\text{net}} \ge +0{,}10\text{R}$, porém slippage médio em $(0{,}08\%,\, 0{,}12\%]$ ($8{,}0$ a $12{,}0\text{ bps}$) **ou** frequência fora de $[0{,}25;\, 7{,}00]$ trades/semana;<br>• **Caso B (vantagem marginal):** $+0{,}00\text{R} \le \overline{R}_{\text{net}} < +0{,}10\text{R}$ com slippage $\le 0{,}12\%$. | **Não alocar dinheiro real.**<br>• *Caso A:* corrigir apenas camada de execução/microestrutura (novo ADR, sem mexer nos filtros de sinal do `strategies.yaml`) e reiniciar um Gate 6B limpo (30 dias / 25 trades).<br>• *Caso B:* manter `K9.3` congelado sem dinheiro real; qualquer mudança de regra exige nova hipótese formal (novo ADR), revalidação completa no Bloco 3 respeitando $K_{\max}$ e novo Gate 6B do zero. |
| **3. Abandonar `K9.3` (`NO-GO`)** | **Qualquer um dos gatilhos abaixo:**<br>• Com $n \ge 25$ trades: $\overline{R}_{\text{net}} < 0{,}00\text{R}$ *(expectância líquida negativa no primeiro teste virgem)*;<br>• Slippage médio realizado $> 0{,}12\%$ ($> 12{,}0\text{ bps}$, destrói o orçamento friccional estrutural da estratégia);<br>• Drawdown máximo virtual $\ge 12\%$ (`KS-1` Hard Stop atingido mesmo com $n < 25$);<br>• Divergência estrutural não sanável que comprove inexistência da vantagem fora da amostra. | **Encerrar `K9.3` formalmente** via novo ADR. Não colocar dinheiro real. Voltar o bot para `ANALYST_ONLY` (ou desligar) e, se desejar continuar a pesquisa, desenvolver uma nova família de estratégia a partir do zero no Bloco 3. |

---

## 4. Protocolo para amostra pequena ($n < 25$ após 30 dias)

Como `K9.3` só opera em clima `BULL` (~1,3 trade/semana na média de 4 anos), um mês em `BEAR`, `SIDEWAYS` ou `TRANSITION` pode gerar poucos trades sem que isso indique defeito. Ainda assim, **amostra pequena nunca autoriza promoção automática para dinheiro real**:

1. **Auditoria de integridade no Dia 30:** consultar `regime_history` nos 7 pares de execução.
   - Se os pares ficaram em `BULL` em $\ge 30\%$ das velas 4h e ainda assim $n < 5$ trades, **pausar e auditar sinais/paridade** (descartar falha silenciosa de coleta, warmup ou filtro antes de continuar).
   - Se o mercado ficou majoritariamente fora de `BULL` ($< 30\%$ das velas 4h em `BULL`), o silêncio é comportamento esperado de defesa de capital.
2. **Extensão controlada (Dias 31 a 60, teto absoluto de 90 dias):** manter `SHADOW_CHAMELEON` rodando sem alterar nenhum parâmetro enquanto `MaxDD < 10%`, `KS-1..KS-4 = PASS` e slippage nos trades já fechados $\le 0{,}08\%$, até atingir $n = 25$ trades fechados e aplicar a tabela da §3.
3. **Regra de corte no Dia 90 (se $n$ continuar $< 25$):**
   - **$15 \le n < 25$:** só permite exceção se $\overline{R}_{\text{net}} \ge +0{,}15\text{R}$ *(margem maior para compensar incerteza amostral)*, slippage $\le 0{,}08\%$, `MaxDD < 8%` e zero falhas de kill-switch — e mesmo assim exige novo ADR explícito (ex.: estender mais 30 dias ou iniciar Fase 1 com risco reduzido de `0,5%`).
   - **$n < 15$ após 90 dias:** classificar como **`HOLD` / inviável por baixa frequência** (não é possível validar edge em prazo razoável); **não promover para Fase 1**.

---

## 5. Cláusula de pré-compromisso (anti-viés)

Esta regra entra em vigor **antes do primeiro ciclo de `SHADOW_CHAMELEON`**. Relaxar qualquer limiar de `R`, slippage ou tamanho de amostra depois de observar trades do Gate 6B **invalida** o Gate 6B como Holdout Prospectivo Verdadeiro e exige registrar uma ADR que substitua esta decisão e reiniciar a contagem do zero.
