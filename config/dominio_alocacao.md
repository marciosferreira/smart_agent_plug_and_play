# skill: dominio
# descricao: Análise de alocação de supply por Key Account (KA Allocation) — Request/Deal/Retail, Score de priorização, WOI, rollover/rollback, KPIs de Health Check. Executa SQL no banco de alocação (ka_input_data, ka_deal_allocation).
# palavras-chave: alocação, allocation, key account, KA, WOI, score, deal, request, retail, rollover, rollback, supply, site supply, health check, week supply, woi crítico, sell-in, booked, entered, one plan week

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
real). Só considere o próximo quarter ou compare os dois se o usuário pedir explicitamente.
Ao citar o rótulo do quarter na resposta, NUNCA deduza — leia dos dados:
inclua `quarter` no SELECT (ou rode `SELECT DISTINCT quarter FROM ka_deal_allocation
WHERE month_status='ongoing'`) e use o valor retornado.

⚠️ **`deal` tem uma repetição AINDA MAIOR — cuidado redobrado.** Diferente de WOI/Booked/
Entered (que repetem só entre os 3 meses do MESMO produto+KA), a coluna `deal` repete o MESMO
valor para TODOS os produtos de um mesmo `deal_group` (para a mesma KA e quarter), além de
repetir nos 3 meses. Exemplo real: `deal_group='GUITAR25 ACCESSORY'` da KA `CLARO_BR` tem
`deal=3700` em 3 produtos diferentes × 3 meses = 9 linhas com o mesmo 3700. Se você fizer
`SUM(deal)` agrupando por produto ou por mês, o resultado sai até 9x inflado. Para obter o
Deal Value real de um grupo, agrupe por `(key_account_code, deal_group)` e use `MAX(deal)`
(nunca `SUM(deal)` direto sobre múltiplas linhas do mesmo grupo).

### Snapshot estático — o [PERÍODO] é uma DICA, não um filtro literal

Os dados são uma exportação estática: o 'agora' deles é o mês com
month_status='ongoing', que pode NÃO coincidir com a data de hoje. O prefixo
[PERÍODO: from=...&to=...] serve para rotular o período no texto/título — o filtro
SQL real deve usar as colunas dos dados (`quarter`, `month_status`, `year_month`).
ANTES de responder 'não há dados para o período', execute um
`SELECT quarter, month_status, MIN(year_month), MAX(year_month) FROM ka_deal_allocation
GROUP BY quarter, month_status` para ver o range real. Se o período pedido não
intersectar os dados, diga qual range existe e responda com o quarter em andamento,
deixando claro o ajuste. 'FQ1', 'FQ2' são valores LITERAIS da coluna `quarter`.

⚠️ **O atraso do snapshot pode ser de um QUARTER INTEIRO, não só de dias/semanas — avise
sempre, não só quando faltar dado.** Exemplo real verificado: com a data de hoje em
24/07/2026, o `month_status='ongoing'` dos dados aponta para junho/2026 (FQ1, que vai de
abril a junho/2026 — já encerrado há mais de 3 semanas pelo calendário real). O quarter que
conteria a data de hoje (FQ2, jul-set/2026) os dados rotulam como `next` (ainda não
iniciado). Ou seja, "quarter atual"/"este mês" respondido pelos dados pode ser um quarter
INTEIRO anterior ao que o usuário esperaria pelo calendário — não é só uma questão de dias.
Por isso, em TODA resposta que resolver uma expressão de período relativo ("este mês",
"quarter atual", "próximo mês" etc.), NÃO baste rodar o SQL corretamente — declare também,
na resposta, a data/quarter real resolvido (ex: "usando o quarter atual dos dados, FQ1,
abril–junho/2026"). Se esse período resolvido já tiver ficado no passado em relação à data
de hoje (comparando com a data de hoje informada no contexto da conversa), avise
explicitamente, por exemplo: "nota: a base está congelada em FQ1 (jun/2026); pelo calendário
de hoje, já estaríamos no FQ2". Não presuma que essa defasagem é sempre pequena — confira o
range real a cada nova sessão/pergunta temporal, pois o snapshot não se atualiza sozinho.

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
   pode já estar 1 quarter atrás do mês real (ex: dados congelados em junho/2026 com hoje já em
   julho/2026). Além disso, `allocation_W1..W5` NÃO têm uma data de calendário associada a cada
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
2. **KA é MMICOM (`MMICOM_BR`)?** MMICOM não usa Deal (`deal` sempre 0). O excesso sobre
   `req_qty` tende a vir do `rollover` — lembre que `req_qty` pode não incluir o rollover já
   somado (ver ressalva na tabela de colunas), então cheque `req_qty + rollover` (e `rollback`,
   se houver) antes de concluir que a diferença é inexplicada.
3. **KA normal com `deal > 0`?** O excesso costuma vir da etapa Deal sendo atendida (a soma
   final inclui Request + Deal, então superar só o Request é o comportamento esperado quando
   há Deal a cumprir) — não é um erro nem uma exceção.
4. **Nenhum dos casos acima explica a diferença?** Cheque `sell_in_comp_w1..5` (Sell-in
   Compensation, RN_003/RN_014 — permite alocação acima do Request quando o Sell-in já
   faturado supera o Request) e `allocation_allowed` (se 0, o valor pode ser alocação manual
   do usuário mantida, não calculada pela IA).

### "Por que a alocação da KA X foi maior/menor que a da KA Y?"

Comparação entre duas KAs é ambígua se o usuário não especificar produto e período — o
resultado pode até se INVERTER dependendo do recorte. Exemplo real: no quarter atual, o total
agregado de `TIM_BRA` (11.780) é MENOR que o de `VIVO_BRA` (52.811) — mas em produtos
específicos que as duas compartilham, `TIM_BRA` aloca mais em ~7 casos. Passos:

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

## Regras do orquestrador

Este NÃO é um sistema de chão-de-fábrica — não há OEE, FPY, produção ou linhas de
montagem, e NÃO é um sistema de pedidos de compra.

Períodos e quarters — regras deste domínio:
- Os dados são um SNAPSHOT estático: o 'agora' deles é o mês em andamento nos dados,
  que pode não coincidir com a data de hoje.
- 'FQ1', 'FQ2' etc. são RÓTULOS LITERAIS de quarter que existem nos dados —
  NUNCA peça ao usuário para converter em datas; passe direto para calcular_periodo
  (ex: calcular_periodo('FQ1')) e inclua o rótulo em `detalhes` de consultar_analista.
- Se o usuário NÃO mencionar período nenhum, use calcular_periodo('este quarter')
  como padrão — nunca invente um mês específico.

## Períodos

### re:\bfq\s*(\d)\b

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE quarter = 'FQ' || :g1
```

### este mes | esse mes | este mês | esse mês | this month | mes corrente | mês corrente

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE month_status = 'ongoing'
```

### re:(pr[oó]xim|seguinte|que vem|vindouro|next).*(quarter|trimestre)|(quarter|trimestre).*(seguinte|que vem|vindouro)

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE month_status = 'next'
```

### re:quarter|trimestre

```sql
SELECT MIN(year_month), date(MAX(year_month), '+1 month', '-1 day')
FROM ka_deal_allocation WHERE month_status IN ('done', 'ongoing')
```

## Modelo de dados

### ⚠️ Colunas com semântica especial — leia antes de escrever qualquer SQL

| Coluna | Tabela | Observação |
| --- | --- | --- |
| `year_month` | ambas | Confiável em ambas as tabelas. O arquivo de origem de `ka_deal_allocation` vinha com essa coluna corrompida (bug de exportação, colapsava para ~1969-12-31) — o valor foi corrigido no momento da carga (`load_ka_allocation.py`), usando o `year_month` de `ka_input_data` via join em `(quarter, material_id, key_account_id, month_seq)`. |
| `month_seq` | ambas | Posição do mês (1/2/3) dentro do grupo `(quarter, material_id, key_account_id)`, na ordem original do arquivo — útil para ordenar sem depender de `year_month`. |
| `month_status` | ambas | `done`/`ongoing` = meses do quarter atual (sempre nessa ordem: 2× `done`, depois `ongoing`); `next` = os 3 meses do próximo quarter. |
| `allocation_allowed` | `ka_deal_allocation` | Armazenada como INTEGER (1/0), não texto `"true"/"false"`. |
| `req_qty` | ambas | ⚠️ Apesar do nome sugerir "Request final (após Rollover/Rollback)", os dados observados mostram que, para MMICOM (onde dá pra isolar por `deal=0`), `alocado = req_qty + rollover` bate exatamente — ou seja, na prática `req_qty` parece NÃO incluir o rollover já somado. Ao comparar alocação vs. request, NÃO assuma que `req_qty` já reflete o rollover/rollback — valide somando `rollover` (e possivelmente `rollback`) por cima antes de classificar uma diferença como "inexplicada". |
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
| current_month | Mês de referência atual do sistema (data) |
| current_week | Semana corrente dentro do mês/quarter |
| quarter | Identificador do quarter (ex: FQ1, FQ2) |
| year_month | Mês/ano do registro |
| month_status | `done` (mês fechado do quarter atual), `ongoing` (mês corrente) ou `next` (quarter seguinte) |
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
| month_seq | Derivada no load — posição do mês (1/2/3) dentro do grupo (quarter, material_id, key_account_id) |

### `ka_deal_allocation` — RESULTADO computado pela IA

Mesma granularidade da tabela acima, mas com os valores efetivamente calculados pelo algoritmo de alocação.

| Coluna | Significado |
|--------|-------------|
| quarter, material_id, key_account_id, key_account_code, deal_group, product, origin, current_week, month_status | Mesmo significado de `ka_input_data` |
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
| month_seq | Derivada no load — posição do mês (1/2/3) dentro do grupo (quarter, material_id, key_account_id) |

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
  JOIN: quarter = quarter AND material_id = material_id
        AND key_account_id = key_account_id AND month_seq = month_seq
  Para: comparar a demanda/capacidade de ENTRADA com o resultado computado pela IA
```

### KPIs do Health Check — tradução para SQL

| KPI | Padrão SQL (sobre `ka_deal_allocation`) |
| --- | --- |
| Critical WOI | `COUNT(*) WHERE woi < 10` |
| Input Data Inconsistencies | `COUNT(*) WHERE allocation_W1 < 0 OR ... OR allocation_W5 < 0` |
| Rounded Allocations (blocos de 10) | `COUNT(*) WHERE allocation_W1 % 10 = 0` (repetir por semana ou considerar o total mensal) |
| Complete Request | `COUNT(*) WHERE (allocation_W1+allocation_W2+allocation_W3+allocation_W4+allocation_W5) >= req_qty` |
| Rollover / Rollback | `COUNT(*) WHERE rollover > 0` / `COUNT(*) WHERE rollback > 0` |
| Completed Deals | `COUNT(*) WHERE deal > 0 AND (soma das allocation_W1..5) >= deal` |

⚠️ **Ressalva**: "Week Supply Violation" (capacidade semanal excedida) e "Site Supply Occurrences" (redistribuição entre JAG/MAN) exigiriam comparar a soma de todas as KAs numa mesma semana contra `one_plan_week*`, ou dados de site não presentes nestas duas tabelas — trate essas duas métricas como aproximações e deixe explícito na resposta que a precisão total do KPI original (tela Health Check) depende de dados agregados que essas tabelas planas não guardam sozinhas. "Site Supply Occurrences" só é válida para alocação feita pela IA (não se aplica a alocação manual do usuário).

⚠️ **Ressalva Rollover/Rollback**: por especificação, cada KA (exceto Retail) tem no máximo 2 ocorrências de rollover e 2 de rollback por quarter (não há rollover/rollback no mês corrente/`ongoing`) — considere isso ao validar contagens anormalmente altas. Essas duas métricas não se aplicam a alocação feita manualmente pelo usuário.

### Exemplos de queries

```sql
-- WOI crítico por Key Account, quarter atual
SELECT key_account_code, product, month_status, woi
FROM ka_deal_allocation
WHERE woi < 10 AND month_status IN ('done', 'ongoing')
ORDER BY woi ASC;
```

```sql
-- Alocação total por mês (month_seq) para uma Key Account
SELECT month_seq, month_status,
       SUM(allocation_W1 + allocation_W2 + allocation_W3 + allocation_W4 + allocation_W5) AS alocacao_mensal
FROM ka_deal_allocation
WHERE key_account_code = 'CLARO_BR'
GROUP BY month_seq, month_status
ORDER BY month_seq;
```

```sql
-- Comparar Request de entrada vs. alocação resultante (join input x output)
SELECT i.key_account_code, i.product, i.month_seq, i.req_qty,
       (o.allocation_W1+o.allocation_W2+o.allocation_W3+o.allocation_W4+o.allocation_W5) AS alocado
FROM ka_input_data i
JOIN ka_deal_allocation o
  ON i.quarter = o.quarter AND i.material_id = o.material_id
 AND i.key_account_id = o.key_account_id AND i.month_seq = o.month_seq
WHERE i.key_account_code = 'CLARO_BR';
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

O sistema trabalha com quarters (`FQ1`, `FQ2`, ...) e, dentro de cada quarter, 3 meses
identificados por `month_seq` (1/2/3), `month_status` (`done`, `ongoing` no quarter atual;
`next` no próximo quarter) e `year_month` (mês/ano confiável em ambas as tabelas).

**Não existe granularidade diária.** `year_month` é sempre o dia 1 do mês — o detalhamento
dentro do mês é por semana (`allocation_W1..W5`), não por data de calendário. Perguntas sobre
"alocação de hoje" só fazem sentido em termos de mês/semana corrente, nunca de um dia exato.

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
condition_sql = "SELECT COUNT(DISTINCT key_account_code) FROM ka_deal_allocation WHERE woi < 10"
condition_operator = ">"
condition_threshold = 10

# Alerta se não houver nenhum deal completo no quarter atual
condition_sql = "SELECT id FROM ka_deal_allocation WHERE deal > 0 AND month_status = 'ongoing' LIMIT 1"
condition_operator = "is_not_empty"

# Alerta se houver rollback aplicado
condition_sql = "SELECT id FROM ka_deal_allocation WHERE rollback > 0 LIMIT 1"
condition_operator = "is_not_empty"
```

## Dica para condition_sql

Tabelas disponíveis: `ka_input_data`, `ka_deal_allocation` (colunas: key_account_code,
material_id, quarter, month_seq, month_status, woi, allocation_W1..W5, rollover, rollback).
