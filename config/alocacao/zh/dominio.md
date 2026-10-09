# skill: dominio
# descricao: Análise de alocação de supply por Key Account (KA Allocation) — Request/Deal/Retail, Score de priorização, WOI, rollover/rollback, KPIs de Health Check. Executa SQL no banco de alocação (ka_input_data, ka_deal_allocation).
# palavras-chave: alocação, allocation, key account, KA, WOI, score, deal, request, retail, rollover, rollback, supply, site supply, health check, week supply, woi crítico, sell-in, booked, entered, one plan week

<!-- TODO: 中文翻译待完成。本文件是 pt-BR/dominio.md 的原样副本，用于验证多语言回退机制在业务翻译完成前可以端到端正常工作。此提示存在期间，实际内容仍为葡萄牙语。 -->
---

## Regras do agente

### Granularidade temporal — invariante crítico

`year_month` é SEMPRE o dia 1 do mês (ex: '2026-06-01') — representa o mês inteiro,
não um dia específico. Não existe dado por dia de calendário; o detalhamento dentro
do mês é por SEMANA (allocation_W1..W5, sell_in1..5, one_plan_week1..5).
Se o usuário pedir 'alocação de hoje' (ou qualquer data que não seja dia 1 de um mês),
NUNCA filtre por essa data específica nem responda como se existisse alocação para
aquele dia exato — isso é impossível dado o schema. Em vez disso, explique que a
granularidade é mensal (linhas sempre no dia 1) e semanal (W1-W5) dentro do mês,
e ofereça a alocação do mês corrente (month_status='ongoing') como alternativa.

### Período padrão sem especificação

WOI, `booked_quarter` e `entered_quarter` MUDAM entre quarters (mesmo produto+KA pode ter
valores bem diferentes em FQ1 vs FQ2), mas são constantes nos 3 meses (`month_seq` 1/2/3) de
um mesmo quarter, para um mesmo produto+KA — os nomes já entregam isso ("booked_QUARTER",
"entered_QUARTER"). Se o usuário perguntar algo (ex: 'quais KAs têm WOI crítico') SEM
especificar período, assuma o quarter em andamento (`month_status IN ('done','ongoing')`),
deixe explícito na resposta qual quarter foi usado, e deduplique por produto+KA (o valor se
repete nos 3 meses — se você somar/contar sem deduplicar, o resultado sai 3x maior que o
real). Só considere o próximo quarter, o histórico ou compare períodos se o usuário pedir
explicitamente. Ao citar o quarter na resposta, NUNCA deduza — leia dos dados: inclua
`fiscal_year` e `quarter` no SELECT (ou rode `SELECT DISTINCT fiscal_year, quarter FROM
ka_deal_allocation WHERE month_status='ongoing'`) e use os valores retornados.

### Histórico e ano fiscal — o rótulo do quarter se repete

Além do quarter atual (`done`/`ongoing`) e do próximo (`next`), a base guarda os quarters
anteriores com `month_status = 'closed'` (desde jul/2024). O ano fiscal começa em ABRIL:
FQ1 = abr–jun, FQ2 = jul–set, FQ3 = out–dez, FQ4 = jan–mar do ano civil seguinte. A coluna
`fiscal_year` é o ano em que o ano fiscal começa (o FQ4 de `fiscal_year = 2025` é jan–mar/2026).

- 'FQ1'…'FQ4' se REPETEM a cada ano fiscal: filtre e agrupe SEMPRE por `(fiscal_year, quarter)`,
  nunca só por `quarter` — senão o FQ1 de um ano se mistura com o do outro.
- Joins entre `ka_input_data` e `ka_deal_allocation` usam `year_month` (ou `fiscal_year` +
  `quarter` + `month_seq`) junto com `material_id` e `key_account_id`.
- Séries no tempo (evolução, sazonalidade, comparação ano contra ano): agrupe por `year_month`
  e filtre `month_status IN ('closed','done','ongoing')` — o `next` ainda não tem request nem
  alocação. Lembre que o mês `ongoing` está incompleto (sell-in só até a semana corrente).

⚠️ **`deal` tem uma repetição AINDA MAIOR — cuidado redobrado.** Diferente de WOI/Booked/
Entered (que repetem só entre os 3 meses do MESMO produto+KA), a coluna `deal` repete o MESMO
valor para TODOS os produtos de um mesmo `deal_group` (para a mesma KA e quarter), além de
repetir nos 3 meses. Exemplo: um `deal_group` de acessórios com 3 produtos numa KA tem o mesmo
valor de `deal` em 3 produtos × 3 meses = 9 linhas. Se você fizer
`SUM(deal)` agrupando por produto ou por mês, o resultado sai até 9x inflado. Para obter o
Deal Value real de um grupo, agrupe por `(key_account_code, deal_group)` e use `MAX(deal)`
(nunca `SUM(deal)` direto sobre múltiplas linhas do mesmo grupo).

### Snapshot estático — o [PERÍODO] é uma DICA, não um filtro literal

Os dados são uma exportação estática: o 'agora' deles é o mês com
month_status='ongoing', que pode NÃO coincidir com a data de hoje. O prefixo
[PERÍODO: from=...&to=...] serve para rotular o período no texto/título — o filtro
SQL real deve usar as colunas dos dados (`fiscal_year`, `quarter`, `month_status`, `year_month`).
ANTES de responder 'não há dados para o período', execute um
`SELECT fiscal_year, quarter, month_status, MIN(year_month), MAX(year_month) FROM
ka_deal_allocation GROUP BY fiscal_year, quarter, month_status ORDER BY 4` para ver o range
real. Se o período pedido não intersectar os dados, diga qual range existe e responda com o
quarter em andamento, deixando claro o ajuste. 'FQ1'…'FQ4' são valores LITERAIS da coluna
`quarter` e se repetem a cada `fiscal_year`.

⚠️ **O snapshot fica atrás do calendário e o atraso pode crescer — avise sempre, não só
quando faltar dado.** Nesta base, o `month_status='ongoing'` é setembro/2026 (FQ2 de
`fiscal_year = 2026`, jul–set/2026), na semana 3, e o FQ3 (out–dez/2026) aparece como `next`.
Com a data de hoje já em outubro/2026 ou depois, "este mês" e "quarter atual" respondidos
pelos dados ficam um mês (ou mais) atrás do calendário, e a distância aumenta com o tempo:
o snapshot não se atualiza sozinho.
Por isso, em TODA resposta que resolver uma expressão de período relativo ("este mês",
"quarter atual", "próximo mês" etc.), NÃO baste rodar o SQL corretamente — declare também,
na resposta, a data/quarter real resolvido (ex: "usando o quarter atual dos dados, FQ2,
julho–setembro/2026"). Se esse período resolvido já tiver ficado no passado em relação à data
de hoje (comparando com a data de hoje informada no contexto da conversa), avise
explicitamente, por exemplo: "nota: a base está congelada em setembro/2026 (FQ2); pelo
calendário de hoje, já estaríamos no FQ3". Não presuma que essa defasagem é sempre pequena —
confira o range real a cada nova sessão/pergunta temporal, pois o snapshot não se atualiza
sozinho.

### Produto/KA não encontrado — sugira nomes semelhantes, nunca pare em "não encontrado"

`product` (e também `key_account_code`, `deal_group`) são texto livre (ex: `'XT2621 -
NAPLES26 4/256 - ARCTIC SEAL'`) — o usuário raramente vai escrever o nome exatamente como está
armazenado (espaçamento, hífen, barra, maiúsculas/minúsculas, ou só um pedaço do nome). Se uma
busca exata (`=` ou `LIKE '%termo exato%'`) não retornar nenhuma linha, NÃO responda
simplesmente "produto não encontrado" — antes disso:

1. Quebre o termo pedido em palavras/tokens relevantes (ignorando espaço, hífen, barra) e busque
   combinando `LIKE '%token%' AND LIKE '%token2%' ...` para cada token. `LIKE` no SQLite já é
   case-insensitive para ASCII — não precisa de `LOWER()`/`UPPER()`.
2. Se ainda não encontrar nada com todos os tokens (`AND`), tente cada token isoladamente
   (`OR`) para achar candidatos parciais.
3. Traga até ~5-10 valores distintos (`product`, ou `key_account_code`/`deal_group` conforme o
   caso) como SUGESTÃO na resposta — ex: "Não encontrei exatamente 'X', você quis dizer um
   destes?" — em vez de simplesmente dizer que não existe.
4. Só afirme que não há nada parecido se a busca por token isolado (`OR`) também não retornar
   nenhum candidato.

```sql
-- Usuário pediu "Naples 26 4/256" — não bate exato com o nome armazenado
SELECT DISTINCT product
FROM ka_deal_allocation
WHERE product LIKE '%naples%' AND product LIKE '%26%' AND product LIKE '%4%256%'
LIMIT 10;
-- Se nada vier, repita trocando os AND por OR para sugestões mais soltas.
```

### Perguntas de explicabilidade ("por que a IA alocou X?")

Quando o usuário perguntar o MOTIVO de uma alocação (ex: 'por que a IA alocou 550 produtos
na semana 3 deste mês?'), NÃO existe uma coluna que diga "motivo" — a resposta precisa ser
CONSTRUÍDA combinando dados + regras de negócio (ver seção Domínio de negócio). Siga estes
passos:

0. **"Deste mês" e "semana 3" também sofrem do atraso do snapshot (ver "Snapshot estático" e
   "O atraso do snapshot pode ser de um QUARTER INTEIRO" acima) — resolva pelos dados, não pelo
   calendário real, e declare isso na resposta.** "Deste mês" = `month_status='ongoing'`, que
   pode já estar 1 quarter atrás do mês real (ex: dados congelados em setembro/2026 com hoje já em
   outubro/2026). Além disso, `allocation_W1..W5` NÃO têm uma data de calendário associada a cada
   semana — não existe coluna que diga "W3 = 15 a 21 de julho"; W3 é só a 3ª posição semanal
   DENTRO do mês resolvido pelos dados. Portanto: (a) informe explicitamente qual mês/quarter
   foi usado como "este mês" e (b) deixe claro que "semana 3" se refere à 3ª semana DAQUELE mês
   nos dados, não a uma semana específica do calendário real — nunca deixe essas resoluções
   implícitas numa pergunta de explicabilidade.
1. **Desambiguar o escopo.** "550 produtos" quase sempre é um agregado (soma de
   várias linhas KA/produto), não uma decisão única. Antes de explicar, identifique via SQL
   quais linhas (`key_account_code`, `product`, `material_id`) compõem esse total naquela
   semana/mês. Se for mais de uma linha, ou peça ao usuário para restringir (KA ou produto
   específico), ou explique cada linha relevante separadamente — nunca invente uma causa
   única para um número que é soma de decisões independentes.
2. **Buscar os fatores da(s) linha(s)** em `ka_deal_allocation` (e `ka_input_data` se for
   comparar entrada vs. resultado): `req_qty`, `deal`, `rollover`, `rollback`, `woi`,
   `booked_quarter`, `entered_quarter`, `one_plan_week3` (capacidade da semana),
   `sell_in3`, `allocation_allowed`, `allocation_alone_W3` (alocação sem compensações,
   para comparar com `allocation_W3`).
3. **Aplicar as regras do Domínio de negócio** para montar a narrativa: qual etapa do
   waterfall (Request/Deal/Retail) provavelmente dominou, qual Score estimado (via WOI/
   Booked/Entered) explica a prioridade, se houve Rollover/Rollback empurrando o Request,
   se `one_plan_week3` foi o limitador de capacidade, se há sinal de Site Supply ou Sell-in
   Compensation (`sell_in_comp_w3` diferente de zero), se o registro estava elegível
   (`allocation_allowed`).
4. **Ser transparente sobre o limite de precisão.** As tabelas guardam apenas o resultado
   FINAL da alocação (`allocation_W1..W5`) — não existe coluna que separe quanto veio de
   cada etapa (Request/Deal/Retail), nem uma coluna de Score real (é sempre estimado). Se a
   pergunta pedir uma decomposição exata etapa-a-etapa, deixe claro que a resposta é uma
   INFERÊNCIA baseada nas regras de negócio, não um valor armazenado.

### "Por que a IA alocou mais/menos do que o Request?"

Alocação diferente do Request (mensal ou semanal) NÃO é, por padrão, uma anomalia — é o
resultado esperado do waterfall (RN_002/RN_008: alocação final = Request + Deal + Retail).
A causa muda conforme o TIPO de KA (ver "Identificando KAs especiais" no Modelo de dados) —
verifique isso ANTES de explicar:

1. **KA é Retail (`RETAIL_BRA`)?** `req_qty` é estruturalmente 0 — "comparar com o request"
   não se aplica; toda a alocação é o residual da etapa Retail.
2. **KA é MMICOM (`MMICOM_BR`)?** MMICOM não usa Deal (`deal` sempre 0) e é a primeira da
   fila (Score 0): a alocação tende a ser igual ao `req_qty`, que já inclui `rollover` e
   `rollback`. Alocação abaixo do `req_qty` no MMICOM indica que a capacidade do produto no mês
   não cobriu nem a primeira prioridade.
3. **KA normal com `deal > 0`?** O excesso costuma vir da etapa Deal sendo atendida (a soma
   final inclui Request + Deal, então superar só o Request é o comportamento esperado quando
   há Deal a cumprir) — não é um erro nem uma exceção.
4. **Nenhum dos casos acima explica a diferença?** Cheque `sell_in_comp_w1..5` (Sell-in
   Compensation, RN_003/RN_014 — permite alocação acima do Request quando o Sell-in já
   faturado supera o Request) e `allocation_allowed` (se 0, o valor pode ser alocação manual
   do usuário mantida, não calculada pela IA).

### "Por que a alocação da KA X foi maior/menor que a da KA Y?"

Comparação entre duas KAs é ambígua se o usuário não especificar produto e período — o
resultado pode até se INVERTER dependendo do recorte. Exemplo: no quarter atual, o total
agregado de `TIM_BRA` é bem MENOR que o de `VIVO_BRA` — mas em produtos específicos que as
duas compartilham, a `TIM_BRA` pode alocar mais. Passos:

1. **Desambiguar o escopo.** Se o usuário não disser produto/mês, pergunte ou deixe explícito
   que a resposta é sobre o TOTAL agregado do quarter (e não sobre um produto específico) —
   nunca assuma que a comparação vale igualmente em todos os recortes possíveis.
2. **Comparar linha a linha (mesmo produto, mesmo mês) entre as duas KAs**: `woi`,
   `booked_quarter`, `entered_quarter` (lembrando: repetem nos 3 meses do quarter — não
   dedupe errado ao comparar), `req_qty`, `deal`, `rollover`, `key_account_code` (para saber
   se uma delas é MMICOM/Retail, que muda toda a lógica).
3. **A causa mais comum NÃO é prioridade (Score) — é o próprio Request.** Quando WOI de ambas
   está na mesma faixa (ex: as duas ≥15, sem Booked/Entered → mesmo Score), a diferença de
   alocação normalmente reflete simplesmente que uma KA pediu mais (`req_qty` maior), não uma
   decisão de priorização da IA. Só atribua a diferença ao Score/WOI quando os valores de
   WOI/Booked/Entered das duas KAs realmente colocam elas em Scores diferentes.

### "Quanto ficou o WOI projetado (final) da KA X?"

`proj_woi_w1..w5` é uma projeção POR LINHA (produto + KA + mês), não existe um valor único
"do KA" — uma KA como `VIVO_BRA` tem uma linha (e um WOI projetado) por produto. Se o usuário
não especificar produto, ou peça para especificar, ou devolva a lista (produto → WOI projetado)
em vez de inventar uma média sem sentido de negócio. "Final da alocação" mapeia para
`proj_woi_w5` (última semana projetada) do mês relevante (padrão: `month_status='ongoing'`,
salvo indicação em contrário). É normal e esperado que `proj_woi_w1..w5` caia ~1 por semana
dentro do mês (consumo semanal de estoque, ver RN_012) — não trate essa queda como anomalia.

### "Quanto faltou alocar para cada KA para atender os Deals?" (Deal Missing)

Não existe uma coluna "Deal Allocation" armazenada — só o resultado final (`allocation_W1..5`)
e o `req_qty`. O Deal Missing (RN_005/RN_019, Tabela 3) precisa ser APROXIMADO, e a granularidade
correta é `(key_account_code, deal_group)` — NUNCA por produto isolado, porque `deal` repete
por todos os produtos do grupo (ver ressalva de `deal` acima). Abordagem:

1. Agrupe por `(key_account_code, deal_group)`, usando `month_status IN ('done','ongoing')`.
2. `deal_valor = MAX(deal)` (não SUM — evita o problema de repetição).
3. `req_total = SUM(req_qty)` e `aloc_total = SUM(allocation_W1+...+W5)` somando TODOS os
   produtos e os 3 meses do grupo.
4. `deal_aloc_aprox = MAX(aloc_total - req_total, 0)` (o excesso sobre o Request é o proxy de
   quanto foi atendido via Deal — mesma aproximação e mesmas ressalvas da seção "Por que a IA
   alocou mais/menos do que o Request", incluindo a possível contaminação por Rollover/Sell-in
   Compensation).
5. `deal_missing_aprox = MAX(deal_valor - deal_aloc_aprox, 0)`.

MMICOM e Retail naturalmente ficam de fora (sempre têm `deal = 0`, não precisam de filtro
extra). Deixe claro na resposta que é uma aproximação, já que a etapa Deal não é isolada nos
dados brutos.

```sql
-- Deal Missing aproximado por KA e grupo de acordo, quarter atual
WITH agg AS (
  SELECT key_account_code, deal_group,
         MAX(deal) AS deal_valor,
         SUM(req_qty) AS req_total,
         SUM(allocation_W1+allocation_W2+allocation_W3+allocation_W4+allocation_W5) AS aloc_total
  FROM ka_deal_allocation
  WHERE month_status IN ('done','ongoing') AND deal > 0
  GROUP BY key_account_code, deal_group
)
SELECT key_account_code, deal_group, deal_valor,
       MAX(deal_valor - MAX(aloc_total - req_total, 0), 0) AS deal_missing_aprox
FROM agg
ORDER BY deal_missing_aprox DESC;
```

### Previsões do próximo quarter — tool `prever_risco_proximo_quarter`

Perguntas sobre o FUTURO — demanda prevista, risco de faltar produto, onde a capacidade do One
Plan não vai dar conta no próximo quarter (as linhas `next`, que ainda não têm request) — se
respondem com a tool `prever_risco_proximo_quarter(chave, key_account, produto)`. NÃO improvise
previsão com SQL ou pandas (média, tendência, regressão feita na hora): a tool usa modelos
treinados e validados no histórico desta base. Para o passado e o presente, SQL como sempre.

- A tool injeta um DataFrame (uma linha por KA × produto × mês do próximo quarter) e devolve um
  resumo por KA e por mês, as linhas de maior risco e as métricas de validação. Para outros
  recortes (por família, só novembro, um gráfico), use `analisar_dataframe` sobre o DataFrame.
- Como explicar um risco alto: `pressao_prevista` > 1 (a demanda prevista de todas as KAs passa
  da capacidade do produto no mês) e a fila do Score — `pressao_ate_meu_score` > 1 quer dizer
  que nem a demanda atendida antes desta linha cabe. MMICOM (Score 0) é atendida primeiro.
- Ao comparar a previsão com o histórico, use `initial_req_qty` (o pedido inicial, que é o que
  `demanda_prevista` estima), não `req_qty` (que inclui rollover e rollback). O mesmo `product`
  pode ter dois `material_id` (origens JAG e MAN): para contar linhas ou meses por produto,
  agrupe por `material_id` ou por `(product, origin)`.
- Apresente como previsão, não como fato: probabilidades (`prob_nao_atendimento`, faixas
  alto/médio/baixo), a partir dos pedidos até o mês `ongoing`; cite as métricas de validação
  que a tool devolve; deals e rollback do próximo quarter ainda não existem e ficam de fora; o
  Retail não entra (não faz pedido, recebe o resíduo). A base é DEMO sintética.
- Perguntas sobre o que FAZER com o risco ("o que recomenda?", "devo aumentar a produção?"): a
  tool devolve, além da previsão, um diagnóstico de capacidade (falta, folga, quanto cabe em
  outro mês do mesmo produto, falta residual) e a política de recomendação do domínio
  (seção "Recomendações de ação"). Responda seguindo essa política, com os números do
  diagnóstico, e diga que as recomendações são hipóteses de demonstração.
- Em painéis do dashboard e tarefas agendadas (código `run(from_date, to_date, ctx)`), a mesma
  previsão vem de `ctx.pack("previsao_proximo_quarter")` (lista de dicts com as mesmas colunas;
  aceita `key_account=` e `produto=`): o sandbox não permite carregar o modelo de outro jeito.

## Regras do orquestrador

Este NÃO é um sistema de chão-de-fábrica — não há OEE, FPY, produção ou linhas de
montagem, e NÃO é um sistema de pedidos de compra.

Períodos e quarters — regras deste domínio:
- Os dados são um SNAPSHOT estático: o 'agora' deles é o mês em andamento nos dados,
  que pode não coincidir com a data de hoje.
- 'FQ1'…'FQ4' são RÓTULOS LITERAIS de quarter que existem nos dados e se REPETEM a cada ano
  fiscal (o ano fiscal começa em abril). NUNCA peça ao usuário para converter em datas; passe
  o rótulo direto em `periodo` de consultar_analista, com o ano se o usuário disse
  (ex: periodo='FQ1 2025' ou 'FQ1 FY25'); sem ano, o rótulo resolve para a ocorrência mais
  recente nos dados. Inclua o rótulo (e o ano) também em `detalhes`.
- A base guarda o histórico desde jul/2024 (quarters fechados), além do quarter atual e do
  próximo: perguntas sobre evolução, sazonalidade ou comparação com o ano anterior têm dados.
- Se o usuário NÃO mencionar período nenhum, passe periodo='este quarter' para
  consultar_analista como padrão — nunca invente um mês específico.
- Perguntas sobre o futuro (previsão de demanda, risco de faltar produto ou de a capacidade não
  dar conta no próximo quarter) também vão para consultar_analista, com
  periodo='próximo quarter': o analista tem um modelo treinado para isso.
  Quando o usuário pedir o que FAZER com o risco ("o que você recomenda?", "devo aumentar a
  produção?"), diga isso em `detalhes`: peça AÇÕES RECOMENDADAS segundo a política de recomendação
  do domínio (por produto, mês, responsável e prazo), e não só a previsão. Ao responder, mantenha
  as ações que o analista trouxer, com os responsáveis e prazos, e o aviso de que são hipóteses
  de demonstração.

## Períodos

### re:\bfq\s*(\d)\D{0,15}?(?:fy\s*)?(20\d{2})\b

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE quarter = 'FQ' || :g1 AND fiscal_year = CAST(:g2 AS INTEGER)
```

### re:\bfq\s*(\d)\D{0,15}?fy\s*(\d{2})\b

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE quarter = 'FQ' || :g1 AND fiscal_year = 2000 + CAST(:g2 AS INTEGER)
```

### re:\bfq\s*(\d)\b

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation
WHERE quarter = 'FQ' || :g1
  AND fiscal_year = (SELECT MAX(fiscal_year) FROM ka_deal_allocation WHERE quarter = 'FQ' || :g1)
```

### 上个月 | 上月 | 上一个月

```sql
SELECT MAX(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE month_status = 'done'
```

### 这个月 | 本月

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE month_status = 'ongoing'
```

### re:(下|即将|接下来).*(季度)|(季度).*(下|即将|接下来)

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE month_status = 'next'
```

### re:上一?个?季度

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation
WHERE month_status = 'closed'
  AND year_month >= (SELECT date(MAX(year_month), '-2 months') FROM ka_deal_allocation WHERE month_status = 'closed')
```

### re:季度|quarter

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE month_status IN ('done', 'ongoing')
```

## Modelo de dados

### ⚠️ Colunas com semântica especial — leia antes de escrever qualquer SQL

| Coluna | Tabela | Observação |
| --- | --- | --- |
| `year_month` | ambas | Sempre o dia 1 do mês; confiável em ambas as tabelas e a forma mais simples de ordenar e de juntar as duas. |
| `fiscal_year` | ambas | Ano em que o ano fiscal começa (o ano fiscal vai de abril a março). O FQ4 de `fiscal_year = 2025` é jan–mar/2026. Combine SEMPRE com `quarter`: o rótulo 'FQ1'…'FQ4' se repete a cada ano. |
| `month_seq` | ambas | Posição do mês dentro do quarter (1/2/3). Um produto lançado no meio de um quarter começa em 2 ou 3. |
| `month_status` | ambas | `closed` = meses de quarters já encerrados (histórico desde jul/2024); `done`/`ongoing` = meses do quarter atual (sempre nessa ordem: 2× `done`, depois `ongoing`); `next` = os 3 meses do próximo quarter, que ainda não têm request, deal nem alocação. |
| `id` | ambas | Chave da linha (inteiro), útil em `condition_sql` de monitores. |
| `allocation_allowed` | `ka_deal_allocation` | Armazenada como INTEGER (1/0), não texto `"true"/"false"`. |
| `req_qty` | ambas | Request Final, já com os ajustes: `req_qty = initial_req_qty (init_req_qty no input) + rollover + rollback`. Compare a alocação com `req_qty` diretamente, sem somar o rollover de novo. |
| Colunas sem uso nesta base | ambas | `user_allocation1..5` e `sell_in_comp_w1..5` são sempre 0, `allocation_alone_W1..5` é igual a `allocation_W1..5` e `allocation_allowed` é sempre 1 (não há alocação manual, Sell-in Compensation nem Site Supply nesta base). |
| `key_account_code` | ambas | Os valores reais incluem uma KA de Retail (`RETAIL_BRA`) e uma de MMICOM (`MMICOM_BR`) — ver seção "Identificando KAs especiais (MMICOM/Retail)" abaixo antes de aplicar as regras de Score/Deal, pois essas KAs seguem lógica diferente das demais. |
| `one_plan_week1..5` | ambas | Repete o MESMO valor entre KAs diferentes para o mesmo produto+origem+mês (é capacidade do SITE, não da KA) — nunca some `one_plan_week*` entre linhas de KAs diferentes do mesmo produto/mês, o resultado sai multiplicado pelo nº de KAs que compartilham aquele produto. Para a capacidade real de uma semana, pegue 1 linha (`DISTINCT` ou `MAX`) por produto+origem+mês. |

### Regras críticas de SQL

| Regra | Como aplicar |
|-------|-------------|
| Apenas SELECT | Qualquer outro comando será rejeitado |
| Sem SELECT * | Liste sempre as colunas necessárias |
| Tabelas | `ka_input_data` (entrada do algoritmo) e `ka_deal_allocation` (resultado computado pela IA) |
| Ordenar/filtrar por mês | `year_month` é confiável em ambas as tabelas; `month_seq` (1/2/3) + `month_status` também servem para ordenar sem depender de data |
| Booleanos | `allocation_allowed`, `current_quarter` são INTEGER 0/1 |
| Quarter | Sempre `(fiscal_year, quarter)`: o rótulo 'FQ1'…'FQ4' se repete a cada ano fiscal |
| Período padrão | Sem período pedido, filtre `month_status IN ('done','ongoing')` (quarter atual) — sem filtro, a consulta soma todo o histórico |

### `ka_input_data` — dado de ENTRADA do algoritmo de alocação

Cada linha é uma combinação (produto, KA, quarter, mês) com a demanda e capacidade consideradas pela IA antes de calcular a alocação.

| Coluna | Significado |
|--------|-------------|
| material_id | Identificador do material/produto |
| family | Família comercial do produto |
| product_group | Grupo de produto |
| deal_group | Grupo de acordo comercial (Deal) ao qual o produto pertence |
| product | Nome/descrição completa do produto |
| sales_model | Modelo de vendas (código comercial) |
| origin | Site de origem do produto — JAG ou MAN |
| key_account_id | Identificador numérico da Key Account |
| key_account_code | Código da Key Account (ex: CLARO_BR, MMICOM_BR) |
| current_quarter | 1 se este registro pertence ao quarter atual, 0 caso contrário |
| fiscal_year | Ano em que o ano fiscal começa (abril a março) — combine sempre com `quarter` |
| current_month | Mês de referência atual do sistema (data) |
| current_week | Semana corrente dentro do mês/quarter |
| quarter | Identificador do quarter (FQ1 a FQ4; se repete a cada `fiscal_year`) |
| year_month | Mês/ano do registro |
| month_status | `closed` (quarter já encerrado), `done` (mês fechado do quarter atual), `ongoing` (mês corrente) ou `next` (quarter seguinte) |
| entered_quarter | Volume "Entered" (intenção de pedido) do quarter — ver Score |
| booked_quarter | Volume "Booked" (reservado/confirmado) do quarter — ver Score |
| user_allocation1..5 | Alocação manual definida pelo usuário, por semana (1 a 5) do mês |
| sell_in1..5 | Volume faturado por semana |
| one_plan_week1..5 | Capacidade semanal definida no One Plan (limite de supply da semana) |
| req_qty | Request final considerado pela IA (Initial Request + Rollover − Rollback) |
| init_req_qty | Request inicial, antes de ajustes de Rollover/Rollback |
| deal | Valor do acordo comercial (Deal Value) aplicável ao registro |
| sell_in_lifetime | Total acumulado de unidades faturadas (usado no cálculo do WOI) |
| activations_lifetime | Total acumulado de unidades ativadas (usado no cálculo do WOI) |
| avg_activations | Média de ativações por semana (usado no cálculo do WOI) |
| woi | Weeks of Inventory — ver fórmula no domínio de negócio |
| month_seq | Posição do mês dentro do quarter (1/2/3) |

### `ka_deal_allocation` — RESULTADO computado pela IA

Mesma granularidade da tabela acima, mas com os valores efetivamente calculados pelo algoritmo de alocação.

| Coluna | Significado |
|--------|-------------|
| fiscal_year, quarter, material_id, key_account_id, key_account_code, deal_group, product, origin, current_week, month_status | Mesmo significado de `ka_input_data` |
| year_month | Mês/ano do registro — confiável (corrigida a partir de `ka_input_data` no momento da carga) |
| booked_quarter, entered_quarter | Mesmo significado de `ka_input_data` |
| initial_req_qty | Request inicial informado, antes de ajustes |
| req_qty | Request final considerado no cálculo (após Rollover/Rollback) |
| rollover | Volume de demanda não atendida em períodos anteriores, transferido para este período (aumenta o Request) |
| rollback | Volume de demanda não atendida DENTRO do quarter atual, redistribuído para meses anteriores ainda abertos do mesmo quarter (ordem cronológica inversa) — soma-se ao Request Final do mês de destino, AUMENTANDO a demanda (não reduz, apesar do nome) |
| deal | Deal Value aplicável ao registro |
| sell_in1..5 | Volume faturado por semana |
| one_plan_week1..5 | Capacidade semanal (One Plan) |
| allocation_W1..W5 | **Volume final alocado pela IA**, por semana do mês — é a saída principal do algoritmo |
| allocation_alone_W1..W5 | Alocação isolada por semana (sem efeito de compensações), útil para comparar com `allocation_W1..5` |
| sell_in_comp_w1..5 | Compensação de Sell-in (ajuste técnico de faturamento, não é regra de validação) |
| sell_in_lifetime, activations_lifetime, avg_activations | Usados no cálculo do WOI |
| woi | WOI no momento da alocação |
| allocation_allowed | 1 se o registro (KA/produto/período) foi elegível para participar do cálculo da IA, 0 se foi ignorado (equivalente ao "Enable Allocation" da especificação) |
| proj_woi_w1..w5 | WOI projetado, semana a semana, após aplicar a alocação calculada |
| month_seq | Posição do mês dentro do quarter (1/2/3) |

### Identificando KAs especiais (MMICOM/Retail)

`key_account_code` não tem uma coluna booleana de tipo — o tipo é identificado pelo próprio
código. Valores reais observados: `CLARO_BR`, `TIM_BRA`, `VIVO_BRA` (KAs "normais", passam por
Request→Deal→Retail), `MMICOM_BR` (KA especial — Score 0, ignora Deal) e `RETAIL_BRA` (a etapa
Retail em si — recebe o residual, por isso `req_qty = 0` estruturalmente nessas linhas). Para
identificar via SQL: `key_account_code LIKE '%MMICOM%'` / `key_account_code LIKE '%RETAIL%'`.
Antes de aplicar regras de Score/Deal/WOI a uma linha, cheque o tipo de KA — a explicação de
"por que alocou X" muda completamente dependendo se é MMICOM, Retail ou uma KA normal com Deal.

### Grafo de relacionamentos

```
ka_input_data (1) ──→ (1) ka_deal_allocation
  JOIN: year_month = year_month AND material_id = material_id
        AND key_account_id = key_account_id
  Para: comparar a demanda/capacidade de ENTRADA com o resultado computado pela IA
```

### KPIs do Health Check — tradução para SQL

Sem período pedido, os KPIs são do quarter atual: acrescente `month_status IN ('done','ongoing')`
a cada padrão abaixo (sem o filtro, a contagem soma todo o histórico).

| KPI | Padrão SQL (sobre `ka_deal_allocation`) |
| --- | --- |
| Critical WOI | `COUNT(*) WHERE woi < 10` |
| Input Data Inconsistencies | `COUNT(*) WHERE allocation_W1 < 0 OR ... OR allocation_W5 < 0` |
| Rounded Allocations (blocos de 10) | `COUNT(*) WHERE allocation_W1 % 10 = 0` (repetir por semana ou considerar o total mensal) |
| Complete Request | `COUNT(*) WHERE (allocation_W1+allocation_W2+allocation_W3+allocation_W4+allocation_W5) >= req_qty` |
| Rollover / Rollback | `COUNT(*) WHERE rollover > 0` / `COUNT(*) WHERE rollback > 0` |
| Completed Deals | `COUNT(*) WHERE deal > 0 AND (soma das allocation_W1..5) >= deal` |

⚠️ **Ressalva**: "Week Supply Violation" (capacidade semanal excedida) e "Site Supply Occurrences" (redistribuição entre JAG/MAN) exigiriam comparar a soma de todas as KAs numa mesma semana contra `one_plan_week*`, ou dados de site não presentes nestas duas tabelas — trate essas duas métricas como aproximações e deixe explícito na resposta que a precisão total do KPI original (tela Health Check) depende de dados agregados que essas tabelas planas não guardam sozinhas. "Site Supply Occurrences" só é válida para alocação feita pela IA (não se aplica a alocação manual do usuário).

⚠️ **Ressalva Rollover/Rollback**: nesta base, o que um mês não atende tenta primeiro os meses anteriores do mesmo quarter (rollback, que soma ao Request Final do mês de destino); o que ainda sobra vira rollover do mês seguinte (o 3º mês do quarter passa para o 1º do quarter seguinte), e parte dessa demanda é cancelada pela KA. Por isso pode haver rollover em qualquer mês, inclusive no `ongoing`. Essas duas métricas não se aplicam a alocação feita manualmente pelo usuário.

### Exemplos de queries

```sql
-- WOI crítico por Key Account, quarter atual
SELECT key_account_code, product, month_status, woi
FROM ka_deal_allocation
WHERE woi < 10 AND month_status IN ('done', 'ongoing')
ORDER BY woi ASC;
```

```sql
-- Alocação total por mês para uma Key Account, quarter atual
SELECT fiscal_year, quarter, year_month, month_status,
       SUM(allocation_W1 + allocation_W2 + allocation_W3 + allocation_W4 + allocation_W5) AS alocacao_mensal
FROM ka_deal_allocation
WHERE key_account_code = 'CLARO_BR' AND month_status IN ('done', 'ongoing')
GROUP BY fiscal_year, quarter, year_month, month_status
ORDER BY year_month;
```

```sql
-- Evolução mensal no histórico (pedido x alocado), para tendência e sazonalidade
SELECT year_month, fiscal_year, quarter,
       SUM(initial_req_qty) AS pedido_inicial,
       SUM(req_qty) AS request_final,
       SUM(allocation_W1 + allocation_W2 + allocation_W3 + allocation_W4 + allocation_W5) AS alocado
FROM ka_deal_allocation
WHERE key_account_code = 'CLARO_BR' AND month_status IN ('closed', 'done', 'ongoing')
GROUP BY year_month, fiscal_year, quarter
ORDER BY year_month;
```

```sql
-- Comparar Request de entrada vs. alocação resultante (join input x output)
SELECT i.key_account_code, i.product, i.year_month, i.req_qty,
       (o.allocation_W1+o.allocation_W2+o.allocation_W3+o.allocation_W4+o.allocation_W5) AS alocado
FROM ka_input_data i
JOIN ka_deal_allocation o
  ON i.year_month = o.year_month AND i.material_id = o.material_id
 AND i.key_account_id = o.key_account_id
WHERE i.key_account_code = 'CLARO_BR' AND i.month_status IN ('done', 'ongoing');
```

```sql
-- Explicabilidade: fatores por trás da alocação da semana 3 de um mês/quarter
-- (1º passo: descobrir quais linhas compõem o total perguntado pelo usuário)
SELECT key_account_code, product, material_id, month_status,
       allocation_W3, allocation_alone_W3, sell_in_comp_w3,
       req_qty, deal, rollover, rollback,
       woi, booked_quarter, entered_quarter,
       one_plan_week3, sell_in3, allocation_allowed
FROM ka_deal_allocation
WHERE month_status = 'ongoing'
  AND allocation_W3 > 0
ORDER BY allocation_W3 DESC;
-- Use as colunas acima para montar a narrativa (Score estimado, Rollover/Rollback,
-- limite de one_plan_week3, Sell-in Compensation) — ver seção Domínio de negócio.
```

## Domínio de negócio

### O que é este sistema

Plataforma de análise e explicabilidade da alocação de supply (produto) por Key Account (KA),
calculada por um algoritmo de IA. O sistema não decide a alocação em si — ele expõe, de forma
rastreável, como a IA distribuiu o volume disponível entre os clientes estratégicos (KAs) a
partir de demanda (Request), acordos comerciais (Deal) e capacidade de estoque (Supply).

Esta instalação usa uma base DEMO, com dados sintéticos gerados a partir dos produtos e KAs
reais: o histórico (desde jul/2024), o quarter atual e o próximo seguem as regras deste
documento, mas os números não descrevem o negócio real.

### Como a alocação é calculada (waterfall)

A IA processa o volume disponível em 3 etapas sequenciais, cada uma consumindo o que sobrou da anterior:

1. **Request** (demanda) — distribuído por ordem de prioridade (Score, ver abaixo)
2. **Deal** (acordo comercial) — só usa o volume remanescente após o Request
3. **Retail** — recebe o volume residual final

MMICOM (KA especial) não passa pela etapa de Deal — segue só Request/Score. Retail não usa WOI —
segue só Booked/Entered.

### WOI (Weeks of Inventory) e faixas de risco

```
woi = (sell_in_lifetime - activations_lifetime) / avg_activations
```

| Faixa | Classificação | Prioridade de alocação |
| --- | --- | --- |
| WOI = 0 | Ruptura grave (estoque esgotado) | Máxima |
| WOI < 10 | Crítico | Alta |
| 10 ≤ WOI < 15 | Risco médio | Moderada |
| WOI ≥ 15 | Fora de risco (saudável) | Menor |

Exceção: WOI não se aplica a MMICOM (segue só Request) nem a Retail (segue só Booked/Entered).

⚠️ WOI = 0 nem sempre é ruptura de estoque: para produtos descontinuados ou categorias sem
histórico de ativações suficiente (ex.: acessórios), WOI = 0 ou 1 pode ser apenas uma limitação
do cálculo da métrica (falta de dado), não uma ruptura real. Ao reportar WOI crítico/zero, considere
mencionar essa ressalva se o produto parecer ser um acessório ou não tiver `avg_activations`
consistente.

### Score de priorização (0 a 7)

Dentro da etapa de Request, cada KA é classificado num Score que determina a ordem de
atendimento — Score 0 é atendido primeiro, Score 7 só recebe se ainda sobrar supply:

| Score | Condição | Peso |
| --- | --- | --- |
| 0 | MMICOM (sempre) | Ignora WOI/Booked/Entered — 100% do Request |
| 1 | WOI < 10 | 100% WOI |
| 2 | 10 ≤ WOI < 15 e Booked > 0 | 50% WOI / 50% Booked |
| 3 | 10 ≤ WOI < 15, Booked = 0, Entered > 0 | 75% WOI / 25% Entered |
| 4 | 10 ≤ WOI < 15, sem Booked/Entered | 100% WOI |
| 5 | WOI ≥ 15 e Booked > 0 | 100% Booked |
| 6 | WOI ≥ 15, Booked = 0, Entered > 0 | 100% Entered |
| 7 | Demais casos | Menor prioridade |

Em cenário de escassez, apenas Score 0 e 1 tendem a ser atendidos completamente — scores
maiores só recebem se ainda sobrar supply. O Score não é uma coluna armazenada nas tabelas;
para estimá-lo numa análise, combine `woi`, `booked_quarter` e `entered_quarter` conforme a
tabela acima.

### Rollover e Rollback

Os dois SOMAM ao Request Final (`Request Final = Initial Request + Rollover + Rollback`) — a
diferença está na direção temporal do ajuste, não no sinal:

- **Rollover**: demanda não atendida em períodos anteriores, transferida PARA A FRENTE, para o
  período atual — funciona como um incremento de demanda (aumenta o Request do mês atual).
- **Rollback**: demanda não atendida DENTRO do quarter atual, redistribuída PARA TRÁS, para meses
  anteriores ainda abertos do mesmo quarter (ordem cronológica inversa: penúltimo mês do quarter,
  depois antepenúltimo) — tenta antecipar o atendimento. Apesar do nome sugerir redução, o Rollback
  AUMENTA o Request Final do mês de destino, assim como o Rollover.

### Site Supply

Site Supply é a redistribuição de capacidade de atendimento entre os sites de origem (JAG e MAN)
para um mesmo produto, quando o site originalmente previsto no Initial Request não tem capacidade
(One Plan Week) suficiente para atender o Request Final (`Request + Rollover + Rollback`) daquele
site:

- O sistema verifica, semana a semana (semanas ainda abertas), se o site definido tem capacidade;
  se não tiver, tenta realocar o volume não atendido para o outro site (JAG ↔ MAN) que ainda tenha
  capacidade disponível.
- Se mesmo após a redistribuição entre os dois sites não houver capacidade suficiente, o sistema
  NÃO força a alocação além da capacidade — o volume não atendido vira demanda pendente e gera
  Rollover para períodos futuros.
- Ordem de execução: cada site processa sua própria alocação de Request no mês; depois verifica-se
  o que ficou pendente e aplica-se o Site Supply (só se habilitado pelo usuário); depois disso
  calcula-se o Rollover. Site Supply também pode ocorrer quando há Rollback, se habilitado.
- Só é aplicável a registros calculados pela IA (não existe Site Supply em alocação manual do
  usuário).

### Compensação de Sell-in

Ajuste técnico/operacional (não é regra de priorização da IA) para corrigir divergências entre o
volume de Sell-in (faturado) e o Supply disponível de uma semana:

- Se o Sell-in de uma semana ultrapassa o Supply disponível daquela semana, o sistema pode gerar um
  valor de compensação (`sell_in_comp_w1..5`) para refletir esse ajuste já faturado.
- Aplica-se aos KAs da semana correspondente, EXCETO Retail.
- Não altera as regras de priorização/distribuição da IA (Score, Request, Deal) — é apenas refletido
  no resultado, podendo gerar alocações maiores que o Request planejado.
- É uma regra de backend, sem tela ou elemento de visualização dedicado — trate como explicação de
  possíveis discrepâncias entre Sell-in e Request/Allocation, não como algo que o usuário configura.

### Enable Allocation (elegibilidade na IA)

Controla quais combinações (KA, Produto, Origem, Período) participam do cálculo da IA — equivale à
coluna `allocation_allowed`:

- `allocation_allowed = 1`: o registro participa normalmente de todas as etapas (Score, Request,
  Supply, Deal, Retail).
- `allocation_allowed = 0`: o registro é ignorado pela IA — ela NÃO recalcula nem altera os valores
  desse registro (mantém a alocação manual do usuário, se houver). Isso pode aumentar o Rollover
  de KAs não atendidos e redistribuir o supply entre os demais KAs habilitados.
- Definido no momento em que o usuário executa uma nova distribuição (seleção de quais KAs entram
  no cálculo) — não é uma regra visível nas telas de Allocation Breakdown/Health Check.

### Glossário

| Termo | Significado |
| --- | --- |
| KA | Key Account, cliente estratégico |
| WOI | Weeks of Inventory — semanas de cobertura de estoque |
| Rollover | Demanda não atendida transferida de períodos anteriores para o atual (aumenta o Request) |
| Rollback | Demanda não atendida no quarter atual, redistribuída para meses anteriores ainda abertos do mesmo quarter (aumenta o Request desses meses) |
| Allocation | Volume distribuído para o KA |
| Deal | Acordo comercial, definido por quarter |
| Supply | Volume em estoque, por semana, disponível para alocação |
| Site Supply | Redistribuição de capacidade entre sites de origem (JAG/MAN) quando o site previsto não tem capacidade suficiente |
| Sell-in Compensation | Ajuste técnico quando o Sell-in de uma semana excede o Supply disponível (não se aplica a Retail) |
| Enable Allocation | Elegibilidade do registro para participar do cálculo da IA (coluna `allocation_allowed`) |
| Booked | Volume já reservado/confirmado comercialmente no quarter |
| Entered | Volume em intenção de reserva/pedido em andamento |
| Request | Volume de demanda considerado pela IA |
| Sell-in | Volume efetivamente faturado |

### Períodos e meses

O sistema trabalha com quarters fiscais (`FQ1` a `FQ4`, com o ano fiscal começando em abril e
identificado por `fiscal_year`) e, dentro de cada quarter, 3 meses identificados por
`month_seq` (1/2/3), `month_status` (`closed` nos quarters encerrados; `done`, `ongoing` no
quarter atual; `next` no próximo quarter) e `year_month` (mês/ano confiável em ambas as tabelas).

**Não existe granularidade diária.** `year_month` é sempre o dia 1 do mês — o detalhamento
dentro do mês é por semana (`allocation_W1..W5`), não por data de calendário. Perguntas sobre
"alocação de hoje" só fazem sentido em termos de mês/semana corrente, nunca de um dia exato.

## Recomendações de ação

Política de trabalho para o que fazer quando a previsão do próximo quarter aponta risco de falta
(tool `prever_risco_proximo_quarter`, que devolve esta seção junto com o diagnóstico de capacidade).

⚠️ **HIPÓTESES PARA DEMONSTRAÇÃO.** Os responsáveis, prazos e limites abaixo são fictícios: não
vêm de nenhuma política real da empresa. Ao recomendar, diga que são hipóteses de trabalho.

### Como recomendar

- Proponha de 3 a 5 ações, da mais barata para a mais cara, cada uma com: produto (e site), mês,
  o número do diagnóstico que a justifica, o responsável sugerido e o prazo. Nada de conselho
  genérico ("aumentar a produção"): cite o produto e a quantidade.
- Use os números do diagnóstico de capacidade (falta, folga, quanto cabe em outro mês, falta
  residual) e a probabilidade de risco; não invente valores.
- A previsão é incerta, principalmente a 2 e 3 meses: apresente como alerta para preparar a
  decisão, não como decisão tomada. Refazer a previsão quando os pedidos reais do mês chegarem.

### Alavancas, em ordem (do menor custo para o maior)

1. **Antecipar produção dentro do quarter.** Mover capacidade de um mês com folga para o mês com
   falta, no mesmo produto. Vale para a parte `movivel_no_quarter`. Responsável sugerido:
   Planejamento de Supply (S&OP). Prazo: até a 2ª semana do mês anterior ao da falta.
2. **Realocar capacidade entre produtos.** Tirar de produtos do mesmo site com folga maior que 40%
   da capacidade e levar para o produto com falta; mesma família quando possível. Responsável:
   Planejamento da fábrica. Prazo: antes do fechamento do One Plan do mês.
3. **Aumentar o One Plan em produtos críticos.** Só para a `falta_residual` (o que sobra depois das
   alavancas 1 e 2) quando ela passar de 5% da capacidade do produto no quarter ou de 1.000
   unidades, e o produto tiver pedidos de risco alto de KA com Score 0 ou 1 (MMICOM ou WOI
   crítico). Limite a ação aos 10 produtos de maior falta residual. Responsável: gerência de
   Planejamento com a fábrica. Prazo longo: decidir com 6 a 8 semanas de antecedência.
4. **Combinar prazo ou volume com a KA.** Quando a falta residual dura 2 meses ou mais, ou o risco
   está em KA com Score 2 ou maior (atendida depois). Responsável: gestão da conta. Prazo:
   antes de fechar o Request do mês.
5. **Mudar a prioridade (Score) ou o Request.** Último recurso: só reparte a falta entre as KAs,
   não a elimina. Exige aprovação da gerência.

### Quando NÃO recomendar aumento de capacidade

- Risco só médio (30–60%): monitorar. Parte se resolve sozinha com o erro natural da previsão.
- Falta residual abaixo de 1% da capacidade do produto: monitorar.
- Falta que nasce do rollover do mês anterior (no 1º mês do próximo quarter): antes de agir,
  confirmar com as KAs se o pedido pendente continua valendo, porque parte dele costuma ser
  cancelada.
- Falta que cabe inteira na folga de outro mês do mesmo produto (alavanca 1): resolve sem
  investimento.

### Gravidade (falta residual / capacidade do produto no quarter)

| Faixa | O que fazer |
| --- | --- |
| menos de 1% | Monitorar; refazer a previsão no mês seguinte |
| 1% a 5% | Alavancas 1 e 2 |
| mais de 5% | Alavancas 1 a 4; avaliar a 3 para os produtos de maior falta |

## Exemplos task_code

### Relatório PDF com gráfico

```python
def run(from_date, to_date, ctx):
    rows = ctx.sql("""
        SELECT
            key_account_code,
            woi,
            (allocation_W1 + allocation_W2 + allocation_W3 + allocation_W4 + allocation_W5) AS alocado
        FROM ka_deal_allocation
        WHERE month_status IN ('done', 'ongoing')
    """)

    df = pd.DataFrame(rows)
    if df.empty:
        return ctx.generate_pdf('Relatório de Alocação', '## Sem dados no período.')

    por_ka = df.groupby('key_account_code').agg(
        alocado=('alocado', 'sum'),
        woi_medio=('woi', 'mean'),
    ).reset_index()

    total_alocado = df['alocado'].sum()
    kas_criticos  = df[df['woi'] < 10]['key_account_code'].nunique()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(f'Alocação — {from_date.strftime("%d/%m/%Y")} a {to_date.strftime("%d/%m/%Y")}',
                 fontsize=13, fontweight='bold')

    ax1 = axes[0]
    ax1.bar(por_ka['key_account_code'], por_ka['alocado'], color='#3b82f6')
    ax1.set_title('Alocação Total por Key Account')
    plt.setp(ax1.get_xticklabels(), rotation=45, ha='right')

    ax2 = axes[1]
    ax2.bar(por_ka['key_account_code'], por_ka['woi_medio'], color='#f87171')
    ax2.set_title('WOI Médio por Key Account')
    plt.setp(ax2.get_xticklabels(), rotation=45, ha='right')

    plt.tight_layout()
    chart = ctx.save_chart(fig)

    linhas = '\n'.join(
        f"| {r['key_account_code']} | {r['alocado']:,.0f} | {r['woi_medio']:.1f} |"
        for _, r in por_ka.iterrows()
    )
    conteudo = (
        f"## Resumo\n\n{chart}\n\n"
        f"| Métrica | Valor |\n|---|---|\n"
        f"| Total alocado | {total_alocado:,.0f} |\n"
        f"| KAs com WOI crítico | {kas_criticos} |\n\n"
        f"## Por Key Account\n\n| KA | Alocado | WOI médio |\n|---|---|---|\n{linhas}\n"
    )
    return ctx.generate_pdf('Relatório de Alocação', conteudo)
```

### Planilha Excel — aba única

```python
def run(from_date, to_date, ctx):
    rows = ctx.sql("""
        SELECT key_account_code, product, quarter, month_seq, woi,
               allocation_W1, allocation_W2, allocation_W3, allocation_W4, allocation_W5
        FROM ka_deal_allocation
        WHERE month_status IN ('done', 'ongoing')
    """)
    df = pd.DataFrame(rows)
    return ctx.generate_excel(df, 'alocacao')
```

### Planilha Excel — múltiplas abas

```python
def run(from_date, to_date, ctx):
    rows_input = ctx.sql("""
        SELECT key_account_code, product, quarter, month_seq, req_qty, woi
        FROM ka_input_data
        WHERE month_status IN ('done', 'ongoing')
    """)
    rows_output = ctx.sql("""
        SELECT key_account_code, product, quarter, month_seq,
               allocation_W1, allocation_W2, allocation_W3, allocation_W4, allocation_W5
        FROM ka_deal_allocation
        WHERE month_status IN ('done', 'ongoing')
    """)
    df_input  = pd.DataFrame(rows_input)
    df_output = pd.DataFrame(rows_output)
    return ctx.generate_excel(
        {'Entrada': df_input, 'Alocação': df_output},
        'alocacao_detalhado',
    )
```

### Exemplos de condition_sql para monitores

```python
# Alerta se KAs com WOI crítico > 10
condition_sql = "SELECT COUNT(DISTINCT key_account_code) FROM ka_deal_allocation WHERE woi < 10 AND month_status IN ('done', 'ongoing')"
condition_operator = ">"
condition_threshold = 10

# Alerta se não houver nenhum deal completo no quarter atual
condition_sql = "SELECT id FROM ka_deal_allocation WHERE deal > 0 AND month_status = 'ongoing' LIMIT 1"
condition_operator = "is_not_empty"

# Alerta se houver rollback aplicado
condition_sql = "SELECT id FROM ka_deal_allocation WHERE rollback > 0 AND month_status IN ('done', 'ongoing') LIMIT 1"
condition_operator = "is_not_empty"
```

## Dica para condition_sql

Tabelas disponíveis: `ka_input_data`, `ka_deal_allocation` (colunas: id, key_account_code,
material_id, fiscal_year, quarter, year_month, month_seq, month_status, woi, allocation_W1..W5,
rollover, rollback). Filtre o período com `month_status` (`'done','ongoing'` = quarter atual):
a tabela guarda também o histórico (`closed`) e o próximo quarter (`next`).
