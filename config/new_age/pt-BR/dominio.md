# skill: dominio
# descricao: Análise de custo de mão de obra direta (DL cost) pago a fábricas contratadas (CMs) — taxas horárias, tempos padrão de produção, descontos negociados e custo final por site/produto/mês. Consulta via GraphQL (reportTable) no backend new_age.
# palavras-chave: dl cost, custo, mão de obra, hourly rate, taxa horária, standard time, tempo padrão, discount, desconto, site, operação, fábrica, cm, contract manufacturer, backend, cfc, smt, l5, forecast, aprovação, lta, qoq, sales model, produto, ano fiscal, fy, quarter, vigência

---

## Regras do agente

### Sempre informe ressalvas relevantes na resposta

Sempre que o dado usado numa resposta tiver uma limitação que afete a confiabilidade
ou o escopo do número entregue, diga isso explicitamente na resposta — não entregue só
o número "limpo". Isso vale, por exemplo, para: quarter em andamento (não fechado,
potencialmente incompleto) misturado ou comparado com quarter fechado; coluna
`NULL`/vazia numa amostra relevante (`site_id`/`product_id` de `fact_standard_time`,
`sales_model_id`); grupo com um único candidato numa pergunta de comparação/ranking (não
existe "mais barato" com um item só); custo de `fact_final_dl_cost` que não pode ser
dividido por Sales Model (grupo N:N); ou qualquer outra divergência entre o que a skill
descreve e o que o servidor realmente retornou. A ressalva é parte da resposta, não um
rodapé opcional — omiti-la é dar uma resposta tecnicamente correta mas enganosa.

⚠️ **A ressalva tem que BLOQUEAR a conclusão problemática, não só acompanhá-la.**
Testado ao vivo e confirmado como falha real: numa resposta sobre custo de `AFFINITY`
por site, a ressalva "os períodos são diferentes" apareceu, mas a resposta AINDA assim
liderou com "BPL foi significativamente mais barato" como conclusão — cumpriu a letra da
regra (mencionou a ressalva) mas não o espírito (a ressalva deveria ter impedido aquela
conclusão de aparecer). Regra prática: se a ressalva invalida uma comparação/número, a
resposta não formula essa comparação — nem como conclusão principal, nem "com ressalva
ao lado". A ressalva não é decoração da conclusão errada, ela substitui a conclusão
errada (ver exemplo completo no bullet "Em qual site foi mais barato produzir [Produto]").

### Acesso via GraphQL — três tools disponíveis (mesmo mecanismo do posh)

Existem três tools de leitura de dados neste domínio — escolha pela natureza do pedido:

- **`executar_graphql`** — linhas cruas de uma tabela (`reportTable`). Use quando
  precisa dos dados em nível de registro (listar, detalhar, ou quando o
  processamento depois em `analisar_dataframe` precisa de mais de uma coluna
  numérica/lógica combinada).
- **`executar_graphql_agregado`** — soma/contagem/média/mínimo/máximo já agrupados
  no banco (`reportAggregate`). Use SEMPRE que o pedido for do tipo "quanto custou",
  "total de", "média de", "top N por" — é mais rápido e não traz a tabela inteira
  só para agregar depois em pandas.
- **`executar_graphql_schema`** — schema real (tabelas e colunas) do servidor
  (`reportSchema`). NÃO é fluxo padrão — só chame se travar numa análise por causa
  do schema (ex.: já aconteceu de `dim_sales_model`/`bridge_final_dl_cost_sales_model`
  aparecerem no servidor depois da última verificação desta skill) ou qualquer outra
  suspeita de que esta skill esteja desatualizada. Ver subseção própria abaixo.

#### Regras de `executar_graphql` (reportTable)

- `table` recebe o nome exato da tabela — `"dim_site"`, `"fact_hourly_rate"` etc.
  (sem prefixo de schema).
- ⚠️ **`limit` tem teto de 1.000** — diferente do posh, aqui o servidor REJEITA
  a chamada com erro se `limit` passar de 1000 (não é omisso/silencioso, é erro
  explícito: "O limit excede o máximo permitido (1000)"). `fact_hourly_rate`,
  `fact_standard_time` e `fact_dl_discount` têm mais de 1000 linhas — sem `limit`
  E sem paginação (`offset`) você NUNCA vê a tabela inteira nessas três. Para
  totais reais, prefira `executar_graphql_agregado` a paginar manualmente.
- Datas voltam como timestamp ISO completo com hora (ex.: `"2026-08-04T18:22:53.843Z"`),
  não como data pura. Comparações `GTE`/`LTE` com string `"2026-01-01"` ainda funcionam
  (ordenação lexicográfica de ISO bate com a cronológica), mas se precisar do dia
  exato lembre que a hora vem junto.
- `expand` faz join de **um salto** com uma tabela relacionada via FK — mesmo formato
  do posh: `[{"viaColumn": "site_id", "columns": ["name", "company"]}]` traz colunas
  de `dim_site` já na linha de `fact_hourly_rate`, prefixadas `site_id__name`,
  `site_id__company`.
- Operadores válidos em `where`: `EQ`, `NEQ`, `GT`, `GTE`, `LT`, `LTE`, `IN`,
  `NOT_IN`, `LIKE`, `ILIKE`, `IS_NULL`, `IS_NOT_NULL`. Múltiplas condições no
  mesmo `where` são combinadas com AND.
- Exemplo validado — taxas horárias aprovadas já com nome e empresa do site:
  ```
  executar_graphql(
      table="fact_hourly_rate", chave="taxas",
      where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
      expand=[{"viaColumn": "site_id", "columns": ["name", "company"]}],
      limit=500,
  )
  -- em analisar_dataframe: taxas já vem com site_id__name, site_id__company prontos
  ```

#### Regras de `executar_graphql_agregado` (reportAggregate)

- Mesmos argumentos e formato do posh: `table`, `chave`, `group_by`, `metric_field`,
  `metric_op` (`SUM`/`COUNT`/`AVG`/`MIN`/`MAX`), `where`, `expand` (lista de nomes de
  coluna, só funciona se `group_by` for FK), `order_by` (`"ASC"`/`"DESC"`), `limit`
  (default do servidor: 20, teto: 500).
- Resultado vem com `groupKey`, `metricValue` e uma coluna por item de `expand`.
  `metricValue`/`groupKey` podem vir como string mesmo sendo número — use
  `pd.to_numeric` antes de contas.
- Exemplo validado — custo final de DL por site, já com nome e país do site:
  ```
  executar_graphql_agregado(
      table="fact_final_dl_cost", chave="custo_por_site",
      group_by="site_id", metric_field="dl_cost_by_cm_usd", metric_op="SUM",
      expand=["name", "country"], order_by="DESC", limit=20,
  )
  -- em analisar_dataframe: custo_por_site já vem pronto, uma linha por site,
  -- colunas groupKey (site_id), name, country, metricValue (custo total USD)
  ```

#### Regras de `executar_graphql_schema` (reportSchema)

- Sem argumentos — retorna de uma vez todas as tabelas deste domínio (hoje 10:
  `dim_calendar`, `dim_product`, `dim_product_family`, `dim_sales_model`, `dim_site`,
  `bridge_final_dl_cost_sales_model`, `fact_dl_discount`, `fact_final_dl_cost`,
  `fact_hourly_rate`, `fact_standard_time`) com suas colunas (`column`, `dataType`,
  `nullable`). Não injeta DataFrame no ambiente, retorna texto — é só para
  conferência, não para alimentar `analisar_dataframe`.
- **Não é passo do fluxo normal.** O fluxo padrão continua: `read_skill('dominio.md')`
  → `executar_graphql`/`executar_graphql_agregado` → `analisar_dataframe`. Só chame
  `executar_graphql_schema` se travar por causa do schema — ex: uma tabela/coluna
  citada nesta skill que o servidor rejeite como inexistente, ou suspeita de que
  esta skill esteja desatualizada (já aconteceu: `dim_sales_model` e
  `bridge_final_dl_cost_sales_model` foram adicionadas ao schema depois da última
  verificação — ver "Sales Model" no Modelo de dados abaixo).

### Calendário fiscal — `dim_calendar.quarter` NÃO é o trimestre civil

`dim_calendar.id` é `"YYYY-MM"` (texto, ex.: `"2026-08"`). O campo `quarter` segue o
ano fiscal Motorola/Lenovo (Abril a Março), não o calendário civil:
**Abr–Jun = 1, Jul–Set = 2, Out–Dez = 3, Jan–Mar = 4**. Se o usuário perguntar "este
trimestre"/"Q1" sem mais contexto, confirme que é o trimestre fiscal (Abr–Jun = Q1),
não Jan–Mar.

⚠️ **O número do ano em `dim_calendar.year` NÃO é o mesmo "ano fiscal" (FY) usado por
`fact_final_dl_cost.quarter` e pelos usuários de negócio, especificamente para Q4
(Jan–Mar).** Regra oficial (confirmada com especialista do produto): o FY é nomeado
pelo ano em que ele COMEÇA (abril) — ex. **FY27 Q1 = abr/mai/jun de 2027**, e
**FY27 Q4 = jan/fev/mar de 2028** (mesmo FY27, mas já no ano civil seguinte). Ou seja:
- Para meses de Abr a Dez: `ano_fiscal = dim_calendar.year` (bate direto).
- Para meses de Jan a Mar (`quarter = 4`): `ano_fiscal = dim_calendar.year - 1`
  (ex.: Jan/2026 tem `dim_calendar.year = 2026`, mas é **FY2025 Q4**).

Isso é o oposto do que a coluna `year` de `dim_calendar` sugere à primeira vista — não
assuma que `"Q4," + year` de `dim_calendar` forma o mesmo rótulo `"Q4,<ano>"` visto em
`fact_final_dl_cost.quarter`. Para converter com segurança, prefira casar pelo par
`start_month`/`end_month` (ou pelo `month_id`, via `dim_calendar.id`) em vez de montar
o texto do quarter manualmente a partir de `dim_calendar.year`.

Sinônimo de negócio: "trimestre" = "quarter" = `"FY<ano> Q<n>"` — ver Glossário de negócio.

### Dimensão temporal — três datas diferentes, não confunda

Cada fact tem uma data "de negócio" (quando a taxa/tempo/desconto VALE) diferente da
data "de aprovação" (quando foi aprovado no sistema):

| Tabela | Data de negócio (vigência) | Data de aprovação |
|--------|------------------------------|--------------------|
| `fact_hourly_rate` | `month_id` (mês de referência, via `dim_calendar`) | `approved_at` |
| `fact_standard_time` | `effective_date` | `approved_at` |
| `fact_dl_discount` | `start_date` / `end_date` (janela de vigência) | `approved_at` |
| `fact_final_dl_cost` | `start_month`/`end_month` (janela do trimestre fiscal) — ⚠️ **NÃO use `month_id` para filtrar período nesta tabela**, ver nota abaixo | `approved_at` (frequentemente `NULL`) |

Para "quanto custou em [período]" use a data de negócio, não `approved_at` — `approved_at`
responde "quando foi aprovado", não "a que mês/período o valor se refere".

⚠️ **`fact_final_dl_cost.month_id` já foi observado inconsistente com o próprio
`start_month`/`end_month` da mesma linha** — caso real verificado: uma linha com
`quarter = "Q1,2026"` e `start_month`/`end_month` = Abr–Jun/2026 tinha `month_id =
"2026-09"` (Setembro, fora da janela do próprio trimestre). Para período nesta tabela,
filtre SEMPRE por `start_month`/`end_month` (sobreposição de intervalo, ver "Exemplos
task_code" abaixo) — nunca por `month_id`, mesmo que pareça mais direto.

### `status_approval` e `is_forecast` — valores observados

Valores vistos no ambiente de dev/non-prod: `APPROVED`, `EXPIRED` (podem existir outros,
ex. `PENDING`/`REJECTED`, não vistos nesta base — trate como enum não fechado). Para "custo
atual"/"taxa vigente", filtre `status_approval = 'APPROVED'` — `EXPIRED` são versões
substituídas mantidas por histórico.

⚠️ `fact_final_dl_cost` **não tem** coluna `is_forecast` (as outras três facts têm).
`is_forecast = true` marca valor projetado/estimado; `false` = valor real negociado/realizado.

⚠️⚠️ **Consequência prática, testada ao vivo: `fact_final_dl_cost` pode ter linhas de
quarters que AINDA NEM COMEÇARAM** — não é só "em andamento" (ver seção "Período
padrão" abaixo), pode ser totalmente futuro. Caso real: `ARUBA21` em `JAG` tem uma linha
`APPROVED` pro Q3,2026 (Out–Dez), mas em 16/09/2026 (Q2,2026 em andamento) esse quarter
nem começou. Sem `is_forecast` nesta tabela, **não tem como distinguir pelo schema** se
é um valor já realizado ou uma projeção/planejamento futuro. Antes de tratar qualquer
linha como "custo real do período", confira `start_month` contra a data de hoje — se
`start_month` for no futuro, deixe isso explícito na resposta (não apresente como custo
já incorrido). Isso é ainda mais importante em perguntas de série temporal/"evolução ao
longo do tempo" — ver bullet dedicado em "Padrões de pergunta validados".

### Período padrão E a frase "último quarter" — SEMPRE o quarter FECHADO, nunca o em andamento

Isso vale em DOIS casos, não só quando o usuário fica em silêncio sobre período:

1. **Usuário não menciona período nenhum** — considere o ano fiscal corrente até o
   último quarter já fechado.
2. **Usuário diz literalmente "último quarter"/"quarter passado"/"trimestre anterior"**
   — ⚠️ **testado ao vivo contra o agente real e confirmado como erro recorrente**: o
   agente interpretou "último quarter" como o quarter EM ANDAMENTO (ex.: perguntado em
   setembro, respondeu com Jul–Set, que é o quarter corrente) em vez do último FECHADO
   (Abr–Jun) — o oposto da convenção usada pelo especialista do produto ("estamos em
   Setembro, ou seja Quarter 2 de 2026" [em andamento] "... o último quarter foi
   Q1,2026" [fechado]). NÃO delegue essa frase para `calcular_periodo` genérico sem
   revisar o resultado — ele tende a devolver o trimestre civil/fiscal CORRENTE, não o
   anterior a ele.

**Algoritmo para resolver "último quarter" (não depende de data fixa, calcule a partir
de hoje)**:
1. Ache o mês corrente e determine o quarter fiscal corrente pela tabela de meses
   (Abr–Jun=1, Jul–Set=2, Out–Dez=3, Jan–Mar=4 — ver "Calendário fiscal" acima).
2. "Último quarter" = o quarter fiscal **imediatamente anterior** a esse, não o corrente.
   Se o corrente é Q1, o anterior é Q4 do FY anterior (ver regra de rótulo de ano fiscal
   acima — o número do FY muda nessa borda).
3. Nunca inclua o quarter em andamento nem quarters futuros/forecast como "último
   quarter".

Isso é mais específico que o fallback genérico de `calcular_periodo('todo o período')`
usado por outros domínios — aqui "até agora"/"último quarter" tem um corte de quarter
fechado, não a data de hoje.

### `Sales Model` — agora filtrável via tabela ponte, mas custo NÃO é alocado por Sales Model

O glossário de negócio define uma granularidade abaixo de "Produto" chamada **Sales
Model** (variante comercial de um Produto para um mercado/região específico — cada
Produto tem 1 ou mais Sales Models). Especialistas descrevem `fact_final_dl_cost` como
granular por "Produto, Sales Model, Quarter, Site".

⚠️ **Isto já foi uma limitação do schema (Sales Model não filtrável) — não é mais.**
O schema foi ampliado (confirmado via `executar_graphql_schema`) e agora tem:
- **`dim_sales_model`** — `id`, `name` (ex.: `PAG50037SA`), `description`. Catálogo
  maior (observadas 1000 linhas) — nem todo Sales Model cadastrado tem custo de DL
  associado.
- **`bridge_final_dl_cost_sales_model`** — tabela ponte N:N, só `final_dl_cost_id`
  (FK → `fact_final_dl_cost.id`) e `sales_model_id` (FK → `dim_sales_model.id`).
  `fact_hourly_rate`, `fact_dl_discount` e `dim_product` continuam sem `sales_model_id`
  próprio — só `fact_final_dl_cost` tem esse relacionamento (via a ponte), e
  `fact_standard_time.sales_model_id` (coluna própria, sem passar pela ponte) segue
  100% `NULL` na amostra verificada.

**Agora dá pra responder "quais Sales Models existem para o Produto/Site/Quarter X"**
— exemplo validado (retorna nome do Sales Model junto):
```
executar_graphql(
    table="bridge_final_dl_cost_sales_model", chave="bridge",
    expand=[{"viaColumn": "sales_model_id", "columns": ["name"]}],
    limit=500,
)
-- bridge vem com final_dl_cost_id, sales_model_id, sales_model_id__name
```

⚠️ **NUNCA peça `expand` de colunas de custo de `fact_final_dl_cost` (ex.:
`dl_cost_by_cm_usd`, `dl_cost_by_cm`) a partir de outra tabela** — testado e confirmado:
essas colunas voltam **criptografadas** quando acessadas via `expand` a partir de
`bridge_final_dl_cost_sales_model` (um objeto `{"iv":..., "authTag":..., "ciphertext":...,
"keyVersion":"finalDlCost-v1"}` no lugar do número), mesmo que a MESMA coluna, consultada
DIRETO em `fact_final_dl_cost`, volte como número normal. Para juntar custo com Sales
Model, sempre faça duas chamadas separadas e junte em pandas dentro de
`analisar_dataframe`:
1. `executar_graphql(table="bridge_final_dl_cost_sales_model", expand=[{"viaColumn": "sales_model_id", "columns": ["name"]}])`
2. `executar_graphql(table="fact_final_dl_cost", where=[...])` — **sem** `expand` pra
   trazer as colunas de custo, sempre direto na tabela.
3. `pandas.merge(bridge, custo, left_on="final_dl_cost_id", right_on="id")`

⚠️⚠️ **`dl_cost_by_cm`/`dl_cost_by_cm_usd` NÃO é alocado/dividido por Sales Model — é o
custo TOTAL do grupo Produto+Site+Quarter, repetido igual para CADA Sales Model
associado a ele pela ponte.** A relação é N:N: na amostra verificada, `fact_final_dl_cost`
tem só 19 linhas (todas com pelo menos 1 Sales Model associado), mas uma única linha de
custo chega a estar ligada a 45 Sales Models diferentes. Consequências práticas:
- **NUNCA some `dl_cost_by_cm_usd` agrupado por Sales Model após o merge** — o mesmo
  valor de custo é contado uma vez por Sales Model associado, inflando o total
  artificialmente (fan-out clássico de relação N:N). Se o usuário pedir "custo total
  por Sales Model somado", ou você dedup antes de somar (mantendo a granularidade
  Produto+Site+Quarter) ou recuse dar esse número e explique o motivo.
- **"Quanto custou o Sales Model X" não tem resposta exata neste schema** — o que dá
  pra responder com segurança é "quais Sales Models fazem parte do grupo
  Produto+Site+Quarter que custou R$Y no total", não o custo individual de cada Sales
  Model dentro do grupo. Deixe isso explícito na resposta se a pergunta for sobre
  custo POR Sales Model.
- **"Qual Sales Model é mais barato/mais caro de produzir" também não tem resposta**
  — pela mesma razão: não existe custo individual por Sales Model pra comparar/ranquear,
  só o custo do grupo Produto+Site+Quarter inteiro (mesmo valor repetido pra todos os SMs
  daquele grupo). Responda no nível de Produto+Site+Quarter (ver padrão "Qual Produto
  mais barato de produzir em Site") e avise que a quebra por Sales Model não é possível.
- **"Quais/quantos Sales Models existem para o Produto X" já pode ser respondido com
  segurança** via `bridge_final_dl_cost_sales_model` (junte com `dim_product`/
  `fact_final_dl_cost` conforme necessário).

## Regras do orquestrador

Este é um sistema de custo de mão de obra direta (DL cost) pago a fábricas contratadas (CMs)
que fabricam os produtos — não é um sistema de pedidos de compra (isso é o domínio posh) nem
de alocação de supply. Se o usuário NÃO mencionar período nenhum, use
calcular_periodo('ultimo quarter') como padrão (o quarter fiscal fechado mais recente — escolha
deliberada de granularidade, não workaround: `calcular_periodo('todo o período')` também
funciona aqui via fallback genérico de calendário, cobrindo 2000-01-01 até hoje) e siga direto
para consultar_analista — NUNCA pergunte o período ao usuário.

Ao delegar a consultar_analista, avise de antemão sobre as duas limitações de dado mais
comuns deste domínio (evita ida e volta desnecessária): (1) "Sales Model" é filtrável via
`bridge_final_dl_cost_sales_model`, mas o custo (`dl_cost_by_cm_usd`) NÃO é alocado por
Sales Model — é o total do grupo Produto+Site+Quarter repetido para cada Sales Model
associado, então "custo por Sales Model" não tem resposta exata (só "quais Sales Models
existem" tem); (2) `fact_standard_time` tem `site_id`/`product_id` sempre nulos nesta
base — perguntas de Standard Time por produto/site provavelmente não retornam dado. Se a
pergunta tocar nesses temas, inclua isso na instrução já na primeira chamada a
consultar_analista, em vez de aguardar a resposta dele para descobrir.

## Períodos

> ⚠️ A consulta SQL abaixo não executa neste domínio: depende de `db_config.run_select`,
> que exige `database.url`, ausente em `modo: graphql`. Com o domínio em `modo: graphql`,
> a resolução do range real dos dados fica a cargo do fallback genérico de
> `calcular_periodo` em `agent_multi.py` — "todo o período"/"histórico completo"/"desde o
> início" resolve para `2000-01-01` até hoje (cobre qualquer domínio sem SQL), não para
> "hoje". O bloco SQL ficou apenas como referência para o caso deste domínio um dia rodar
> em `modo: sql`.

### re:todo o per[ií]odo|hist[oó]rico completo|desde o in[ií]cio

```sql
SELECT MIN(id), MAX(id) FROM dim_calendar
```

## Modelo de dados

### `dim_calendar` — mês de referência (dimensão de tempo)

| Coluna | Significado |
|--------|-------------|
| id | PK — texto `"YYYY-MM"` (ex.: `"2026-08"`), usado como FK (`month_id`) nas facts |
| month | Mês civil (1–12) |
| year | Ano civil do mês |
| quarter | Trimestre **fiscal** (Abr–Jun=1, Jul–Set=2, Out–Dez=3, Jan–Mar=4) — ver seção acima |

### `dim_product` — produto (celular/tablet)

| Coluna | Significado |
|--------|-------------|
| id | PK |
| name | Codinome comercial do produto (ex.: `OAKLAND27`, `STOCKHOLMLITE27`) |
| type | enum: `PHONE` · `TABLET` · `OTHER` |
| product_family_id | FK → `dim_product_family.id` |
| sa_date | Provável "Start of Availability" — data de início de disponibilidade comercial do produto (termo comum na indústria de celulares; não confirmado com o time de negócio) |

### `dim_product_family` — família de produto

`id`, `name` (ex.: `A SERIES`, `K SERIES`, `MOTOROLA ONE`, `GAMING`, `MOTO Z`).

### `dim_sales_model` — variante comercial de um Produto (SM)

`id`, `name` (ex.: `PAG50037SA`), `description`. Catálogo com 1000 linhas observadas —
maior que o número de Sales Models efetivamente ligados a algum custo de DL (ver
`bridge_final_dl_cost_sales_model` e seção "Sales Model" acima para a ressalva de
alocação de custo N:N — não some `dl_cost_by_cm_usd` agrupado por Sales Model).

### `bridge_final_dl_cost_sales_model` — ponte N:N entre custo final e Sales Model

| Coluna | Significado |
|--------|-------------|
| final_dl_cost_id | FK → `fact_final_dl_cost.id` |
| sales_model_id | FK → `dim_sales_model.id` |

Tabela só de ligação (sem coluna própria de custo) — uma linha de `fact_final_dl_cost`
pode estar ligada a dezenas de Sales Models diferentes (observado até 45 na mesma base).
⚠️ Ver aviso de criptografia e de fan-out de custo na seção "Sales Model" acima antes de
usar esta tabela para qualquer cálculo de custo.

### `dim_site` — site produtivo da fábrica contratada (CM)

| Coluna | Significado |
|--------|-------------|
| id | PK |
| name | Código/nome do site (ex.: `BPL`, `JAG`, `MAN`) |
| company | Fábrica contratada (CM) dona do site — valores observados: `DIXON`, `FLEX`, `HUAQUIN`, `LENOVO CN`, `NEOLYNC`, `NEWSAN`, `PTSN` |
| country | País do site — observados: `Argentina`, `Brasil`, `China`, `India`, `Indonesia` |
| status | enum — só `ACTIVE` observado nesta base (pode existir `INACTIVE` em produção) |

Sinônimo de negócio: "Operação" = Site (ver Glossário). Usuários às vezes se referem a
um site como `"<name>-<company>"` (ex.: "JAG-FLEX" para o site `JAG` da empresa `FLEX`)
— um mesmo `company` pode ter mais de um site (ex.: `FLEX` tem `JAG` e `MAN`, ambos no
Brasil), então "custo da FLEX" (agregado por `company`, via `expand` em `site_id`) é uma
pergunta DIFERENTE de "custo do JAG" (um site específico) — confirme qual o usuário quer
quando o nome usado for o da empresa (`company`), não o código do site (`name`).

### `fact_hourly_rate` — taxa horária de mão de obra por site/produto/mês

| Coluna | Significado |
|--------|-------------|
| id | PK |
| site_id | FK → `dim_site.id` |
| product_id | FK → `dim_product.id` |
| month_id | FK → `dim_calendar.id` — **"Effective Month"**: mês a partir do qual a taxa passa a valer (ver regra de vigência abaixo) — ⚠️ **histórico real da tabela vai só de `2026-03` a `2026-09`** (~6 meses, NÃO um ano completo), e a cobertura varia muito por site: `MAN` só tem dado a partir de `2026-09` (mês corrente, zero profundidade histórica), `JAG` só a partir de `2026-06`, `BAT` é o mais profundo (`2026-03`). Antes de responder "último ano", confirme quanto histórico existe de verdade pra cada site — não assuma 12 meses. |
| area | enum área de processo produtivo: `BACKEND` · `CFC` · `SMT` (Surface Mount Technology) · `L5` |
| hourly_rate | Valor da taxa horária |
| status_approval | enum: `APPROVED` · `EXPIRED` (ver seção acima) |
| is_forecast | `true` = projeção, `false` = negociado/real |
| approved_at | Timestamp de aprovação (não é a data de vigência — ver seção acima) |

#### Regra de vigência do Hourly Rate (SCD — confirmado com especialista)

Cada combinação **SITE + PRODUTO + ÁREA** tem sua própria linha do tempo de HR — não é
"um valor por mês para todo mundo". Regras:

- Um novo registro `APPROVED` para a MESMA combinação site/produto/área **expira** o
  registro anterior (`status_approval` dele vira `EXPIRED`).
- O valor novo só passa a valer a partir do seu `month_id` (Effective Month) em diante —
  **antes** desse mês, o valor vigente é o registro anterior (mesmo que ele já exista
  há muito tempo, ex.: um HR incluído em janeiro pode continuar `APPROVED` e vigente até
  dezembro se nenhum novo registro da mesma combinação for aprovado nesse intervalo).
- Portanto, para "qual o HR vigente em [mês X]" **NÃO filtre só por `month_id = X`** —
  isso pode não retornar nada se o último HR aprovado foi de um mês anterior.
- ⚠️ **NÃO filtre por `status_approval = 'APPROVED'` pra achar o vigente em meses
  passados** — `status_approval` reflete o status ATUAL (se esse registro já foi
  substituído ou não), não se ele estava vigente no mês X. Pra reconstruir o passado,
  considere TODOS os registros da combinação (`APPROVED` e `EXPIRED` juntos) e pegue o de
  MAIOR `month_id` que seja `<= X` — ignorar os `EXPIRED` faz o agente perder o valor
  histórico certo. Caso real confirmado: uma combinação site+produto+área tem
  `month_id=2026-05` (rate=2, hoje `EXPIRED`), `month_id=2026-06` (rate=66, hoje
  `EXPIRED`) e `month_id=2026-09` (rate=23, `APPROVED`). O vigente em julho/2026 é **66**
  (o registro `EXPIRED` de junho) — filtrar só por `APPROVED` erraria feio, devolvendo
  vazio (nenhum `APPROVED` tem `month_id <= 2026-07`) ou, pior, o valor errado de 23 se
  alguém simplesmente pegar "o `APPROVED` atual" sem checar `month_id`.
- **HR médio de um Produto num Site, num mês**: média do `hourly_rate` vigente (calculado
  como acima, sem filtrar por `status_approval`) entre as áreas (`BACKEND`/`CFC`/`SMT`/`L5`)
  daquele site+produto — na prática costuma ser o mesmo valor nas áreas, mas trate como
  média, não como valor único.
- **HR médio de uma Operação (Site), por mês**: para cada site e cada mês, somar o HR
  vigente (reconstruído mês a mês, TODOS os registros) de TODOS os produtos ativos
  naquele site naquele mês e dividir pelo número de produtos — dá a média do site no mês,
  útil para comparar sites/operações entre si (ex.: "comparativo de Hourly Rate das
  operações no último ano").
  - ⚠️⚠️ **Testado ao vivo: usar `AVG(hourly_rate) WHERE status_approval='APPROVED'` em
    vez da reconstrução acima INVERTE o ranking entre sites.** Comparação real (últimos
    meses com dado real, ver limite de histórico acima):
    | Site | Média ingênua (só `APPROVED`) | Média correta (vigente mês a mês) |
    |---|---|---|
    | JAG | $73,25 (a mais cara das duas) | $45,00 |
    | BPL | $47,91 | **$77,00 (a mais cara)** |

    O método errado aponta `JAG` como mais cara; o correto aponta `BPL`. **NUNCA
    responda "qual operação está mais barata" com a média ingênua** — sempre reconstrua
    o vigente mês a mês primeiro.

### `fact_standard_time` — tempo padrão de produção por área/estágio

| Coluna | Significado |
|--------|-------------|
| id | PK |
| site_id | FK → `dim_site.id`, nullable — ⚠️ **100% NULL nesta base** (verificado nas 1000 linhas amostradas, não é só uma amostra pequena) — perguntas de Standard Time segmentadas por site provavelmente não retornam dado aqui, mesmo sendo suportadas pelo schema; validar se isso muda em produção |
| product_id | FK → `dim_product.id`, nullable — mesma observação de `site_id` (100% NULL nas 1000 linhas amostradas) |
| sales_model_id | FK → `dim_sales_model.id` (coluna própria desta tabela, não passa pela ponte `bridge_final_dl_cost_sales_model` — ver seção "Sales Model" nas Regras do agente) — ⚠️ **100% NULL nesta base**, mesmo já existindo `dim_sales_model` no schema |
| area | enum: `BACKEND` · `CFC` · `SMT` · `L5` (mesmos valores de `fact_hourly_rate`) |
| stage | enum do estágio de validação do tempo: `RFQ` (cotação inicial) · `THIRTY_DAYS_REVIEW` (revisão aos 30 dias) · `POST_30DAYS` (pós 30 dias) |
| standard_time | Tempo padrão (unidade não confirmada — provável minutos/peça) |
| efficiency / overhead | Percentuais de eficiência e overhead aplicados |
| uph | Units Per Hour — unidades produzidas por hora |
| dl_frame | Frame de mão de obra direta usado no cálculo |
| upph | Units Per Person per Hour — produtividade por pessoa/hora |
| effective_date | Data de vigência do tempo padrão — **use esta, não `approved_at`, para "período"** |
| status_approval | enum: `APPROVED` · `EXPIRED` |
| is_forecast | `true` = projeção, `false` = real |
| approved_at | Timestamp de aprovação |

### `fact_dl_discount` — desconto negociado sobre custo de mão de obra

| Coluna | Significado |
|--------|-------------|
| id | PK |
| dl_discount_id | Identificador do desconto (nesta base, 1:1 com `id` — sem múltiplas linhas/tiers observadas, mas trate como possível chave de agrupamento de tiers) |
| group_id | Identificador de agrupamento do desconto — sem tabela dimensão correspondente exposta |
| site_id | FK → `dim_site.id` |
| product_id | FK → `dim_product.id` |
| area | enum: `BACKEND` · `CFC` · `SMT` · `L5` |
| discount_type | enum: `AUTOMATION` · `LTA` (Long Term Agreement — provável) · `OTHER` · `QOQ` (Quarter over Quarter — provável, ver `fact_final_dl_cost.qoq_configuration`) · `SPECIAL` |
| value_type | enum: `PERCENTAGE` · `VALUE` · `NULL` (ainda não classificado, observado em parte das linhas) |
| q_index | Índice de tier/faixa de quantidade — **observado sempre NULL** nesta base |
| discount_value | Valor do desconto (percentual ou absoluto, conforme `value_type`) |
| tier_discount | Desconto por faixa — **observado sempre NULL** nesta base |
| final_value | Valor final pós-desconto — **observado sempre NULL** nesta base |
| min_volume | Volume mínimo para o desconto valer, quando aplicável |
| start_date / end_date | Janela de vigência do desconto — **use estas, não `approved_at`, para "período"** |
| status_approval | enum: `APPROVED` · `EXPIRED` |
| is_forecast | `true` = projeção, `false` = negociado/real |
| approved_at | Timestamp de aprovação |

### `fact_final_dl_cost` — custo final de DL por site/produto/mês (saída do cálculo)

| Coluna | Significado |
|--------|-------------|
| id | PK |
| site_id | FK → `dim_site.id` |
| product_id | FK → `dim_product.id` |
| month_id | FK → `dim_calendar.id` — ⚠️ **já observado inconsistente com `start_month`/`end_month` da própria linha** (ver "Dimensão temporal" acima) — não use para filtrar período |
| quarter | Texto `"Q<n>,<ano>"` (trimestre fiscal, ex.: `"Q2,2026"`) — ver seção de calendário fiscal acima |
| start_month / end_month | Janela do trimestre fiscal correspondente (ex.: `Q2,2026` → 2026-07-01 a 2026-09-30) |
| lta_discount | Desconto tipo LTA aplicado no cálculo (pode vir `NULL` quando não se aplica) |
| qoq_configuration | Configuração de ajuste trimestre-sobre-trimestre aplicada |
| standard_time_catalog | Tempo padrão de referência usado no cálculo |
| hourly_rate | Taxa horária usada no cálculo |
| backend_cost / cfc_cost / smt_cost / l5_cost | Custo decomposto por área de processo (mesmas áreas de `fact_hourly_rate`/`fact_standard_time`) — soma exata das 4 áreas = `dl_cost_by_cm` (confirmado em múltiplos casos reais, ex.: `3+6+0+5=14`, `20+10+40+30=100`) |
| dl_cost_by_cm | Custo total de DL, na moeda do CM (moeda local) = soma das 4 áreas acima |
| dl_cost_by_cm_usd | `dl_cost_by_cm / currency_rate` — **é conversão de câmbio, NÃO subtração de desconto** (confirmado: `14 / 2 = 7`, `100 / 1 = 100`, `737 / 2 = 368.5`) — preferir esta coluna para comparações entre sites/países |
| currency_rate | Taxa de câmbio usada na conversão para USD |
| status_approval | enum — só `APPROVED` observado nesta base |
| approved_at | Frequentemente `NULL` nesta base (tabela parece ser resultado calculado, não um fluxo de aprovação manual como as demais) |

⚠️ **NUNCA insira uma linha "Desconto" ao decompor `dl_cost_by_cm_usd` a partir de
`backend_cost`+`cfc_cost`+`smt_cost`+`l5_cost`** — testado ao vivo e confirmado como
alucinação real: numa resposta sobre `AFFINITY` em `BPL`, o agente apresentou
`3+6+0+5 - Desconto($7,00) = $7,00`, quando na verdade é `(3+6+0+5=14) / currency_rate(2)
= 7` (conversão de câmbio, ver linha `dl_cost_by_cm_usd` acima) — não existe coluna de
desconto em `fact_final_dl_cost`, e os descontos negociados reais de `fact_dl_discount`
para esse produto/site estavam todos `EXPIRED` (não aplicáveis) e com valores diferentes
($33 valor fixo, 4%, 6% — nenhum bate com "$7"). Se quiser mostrar descontos negociados,
busque-os EXPLICITAMENTE em `fact_dl_discount` com `status_approval = 'APPROVED'` — nunca
invente uma linha de desconto só para fechar a conta entre moeda local e USD.

⚠️ Esta é a tabela mais pequena das facts (poucas dezenas de linhas na base observada) —
é o resultado consolidado, não um detalhe transacional. Para "quanto custou" no agregado,
comece por aqui antes de tentar recompor o cálculo a partir de `fact_hourly_rate` +
`fact_standard_time` + `fact_dl_discount`.

💡 Por ser pequena, para perguntas de "existe custo de [produto] em [site]?" busque a tabela
INTEIRA sem filtro logo na primeira chamada (`executar_graphql(table="fact_final_dl_cost",
expand=[...site, produto...])`, sem `where`) e confirme visualmente se a combinação existe —
é mais rápido e gasta menos passos da análise do que tentar primeiro filtros específicos por
ID de produto/site e só recorrer à tabela inteira depois de vários filtros não confirmarem
nada. Essa ordem (filtro específico → filtro mais amplo → tabela inteira por último) já
consumiu o orçamento de passos da análise inteira em pelo menos um caso observado, sem sobrar
passo pra escrever a conclusão.

### Grafo de relações (para juntar em pandas, não há JOIN no GraphQL)

```
dim_calendar       (1)──(N) fact_hourly_rate     via fact_hourly_rate.month_id
dim_calendar       (1)──(N) fact_final_dl_cost   via fact_final_dl_cost.month_id
dim_product        (N)──(1) dim_product_family   via dim_product.product_family_id
dim_product        (1)──(N) fact_hourly_rate     via fact_hourly_rate.product_id
dim_product        (1)──(N) fact_standard_time   via fact_standard_time.product_id [nullable, observado sempre NULL]
dim_product        (1)──(N) fact_dl_discount     via fact_dl_discount.product_id
dim_product        (1)──(N) fact_final_dl_cost   via fact_final_dl_cost.product_id
dim_site           (1)──(N) fact_hourly_rate     via fact_hourly_rate.site_id
dim_site           (1)──(N) fact_standard_time   via fact_standard_time.site_id [nullable, observado sempre NULL]
dim_site           (1)──(N) fact_dl_discount     via fact_dl_discount.site_id
dim_site           (1)──(N) fact_final_dl_cost   via fact_final_dl_cost.site_id
fact_final_dl_cost (1)──(N) bridge_final_dl_cost_sales_model via bridge_final_dl_cost_sales_model.final_dl_cost_id
dim_sales_model    (1)──(N) bridge_final_dl_cost_sales_model via bridge_final_dl_cost_sales_model.sales_model_id
dim_sales_model    (1)──(N) fact_standard_time   via fact_standard_time.sales_model_id [nullable, observado sempre NULL]
```

⚠️ `fact_final_dl_cost` ↔ `dim_sales_model` é N:N na prática (via a ponte) — não trate
como 1:N ao juntar em pandas, e nunca faça `expand` de colunas de custo de
`fact_final_dl_cost` a partir da ponte (ver seção "Sales Model" acima).

### Exemplos de uso da tool executar_graphql

```
-- Custo final de DL no período, com nome do site e do produto já juntos
-- ⚠️ filtra por start_month/end_month, NUNCA por month_id nesta tabela (ver "Dimensão
-- temporal" acima — month_id já observado inconsistente com o período real da linha)
executar_graphql(
    table="fact_final_dl_cost", chave="custo",
    where=[{"field": "start_month", "op": "LTE", "value": "2026-06-30"},
           {"field": "end_month", "op": "GTE", "value": "2026-01-01"}],
    expand=[{"viaColumn": "site_id", "columns": ["name", "company"]}],
)
-- depois, em analisar_dataframe: custo.groupby('site_id__name')['dl_cost_by_cm_usd'].sum()
```

```
-- Descontos vigentes por tipo, com nome do site
executar_graphql(
    table="fact_dl_discount", chave="descontos",
    where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
    expand=[{"viaColumn": "site_id", "columns": ["name", "country"]}],
    limit=500,
)
-- em analisar_dataframe: descontos.groupby('discount_type')['discount_value'].mean()
```

```
-- Taxa horária média por área de processo (agregado no banco)
-- ⚠️ Isso é a média dos registros ATUALMENTE aprovados — serve só pra "taxa vigente
-- agora, sem olhar histórico". NÃO use esse padrão pra perguntas que cruzam meses/
-- comparam sites ao longo do tempo (ex.: "comparativo... no último ano") — nesses
-- casos, filtrar por status_approval=APPROVED sozinho já foi provado errado (inverte
-- o ranking entre sites) — ver "Regra de vigência do Hourly Rate" no Modelo de dados.
executar_graphql_agregado(
    table="fact_hourly_rate", chave="taxa_media_por_area",
    group_by="area", metric_field="hourly_rate", metric_op="AVG",
    where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
    order_by="DESC",
)
```

### Padrões de pergunta validados com especialista do produto

- **"Custo de DL de [empresa/CM]" vs. "custo de [site]"** — uma empresa (CM) pode ter mais
  de um site (ex.: `FLEX` tem `JAG` e `MAN` — ver Glossário/`dim_site`). `where` não filtra
  por coluna de tabela relacionada, então primeiro resolva os `site_id` da empresa em
  `dim_site` (`where company = '<empresa>'`) e SÓ DEPOIS filtre `fact_final_dl_cost` por
  `where site_id IN [<ids resolvidos>]`; para um site específico, filtre por um único
  `site_id` (resolva pelo `name`, ex.: `dim_site` com `where name = 'JAG'`).
  - Para **custo TOTAL da empresa**: `executar_graphql_agregado(group_by="site_id",
    metric_op="SUM", where=[site_id IN os ids da empresa, ...])` e some `metricValue` das
    linhas retornadas em pandas — soma de somas = soma total, sem problema.
  - ⚠️ **Para custo MÉDIO da empresa, NÃO agregue por site e depois "junte" as médias em
    pandas** — testado ao vivo e confirmado como armadilha real: `FLEX` tem 2 linhas em
    `JAG` e 5 em `MAN` na base observada (contagem desigual), então média(média do JAG,
    média do MAN) dá um número diferente — e errado — da média real das 7 linhas juntas.
    "Média das médias" só bate com a média real quando todos os sites têm o MESMO número
    de linhas, o que não é garantido. `executar_graphql_agregado` também sempre exige
    `group_by` — não existe forma de pedir uma média geral já sem quebrar por grupo nesse
    endpoint. Para a média correta cruzando sites de uma empresa: busque as linhas CRUAS
    com `executar_graphql(table="fact_final_dl_cost", where=[{"field": "site_id", "op": "IN", "value": [<ids da empresa>]}, ...])`
    (não use `executar_graphql_agregado` aqui) e calcule `.mean()` direto em pandas sobre
    TODAS as linhas retornadas, sem passar por um agregado intermediário por site.
- **"Qual [Produto] mais barato de produzir em [Site]"** — MIN de `dl_cost_by_cm` (ou
  `dl_cost_by_cm_usd`) em `fact_final_dl_cost`, filtrando `product_id` e `site_id`,
  com `status_approval = 'APPROVED'`, na média/menor valor até o último quarter fechado
  (ver "Período padrão"). Se o pedido for por "Sales Model", ver seção dedicada nas
  Regras do agente — dá pra listar os Sales Models do grupo, mas NÃO pra dividir o
  custo entre eles (custo é do grupo Produto+Site+Quarter, não por Sales Model).
- **"Em qual site foi mais barato produzir [Produto]"** (com ou sem "no último quarter"
  explícito — sem período mencionado, o padrão deste domínio já é o último quarter
  fechado, ver "Regras do orquestrador") — filtre `fact_final_dl_cost` por `product_id`
  E pelo `quarter` fiscal fechado mais recente (via `start_month`/`end_month`, não monte
  o texto do quarter manualmente — ver seção de calendário fiscal), agrupe por `site_id`
  e pegue o menor `dl_cost_by_cm`/`dl_cost_by_cm_usd`.
  - ⚠️ **NUNCA compare/ranqueie linhas de quarters diferentes sem excluir o quarter em
    andamento — e a ressalva tem que BLOQUEAR a conclusão, não só acompanhá-la.**
    Testado ao vivo DUAS vezes: `AFFINITY` tem custo em `BPL` (Q2,2026, `$7,00`) e em
    `JAG` (Q1,2026, `$100,00`). Em 16/09/2026, Q2,2026 (Jul–Set) ainda está EM
    ANDAMENTO — só Q1,2026 (Abr–Jun) está fechado. Na primeira tentativa, mesmo já
    com a ressalva desta seção documentada, a resposta liderou com "BPL foi
    significativamente mais barato ($7 vs $100)" como conclusão principal e só citou a
    diferença de período como nota secundária ("trimestre seguinte") — isso é a
    ressalva FALHANDO em bloquear a conclusão errada, mesmo tecnicamente presente.
    Pra evitar repetir esse erro:
    - **NÃO lidere a resposta com um número/comparação que cruze um quarter aberto.**
      A resposta começa pelo resultado do quarter FECHADO (ou pela ausência de
      comparação possível), não por "X foi mais barato que Y".
    - **Diga "Q2/2026 ainda está em andamento" (ou equivalente), nunca só "trimestre
      seguinte"** — "trimestre seguinte" soa como só uma diferença de rótulo; "ainda em
      andamento" deixa claro que o valor pode mudar e não é definitivo.
    - **Proibido usar "substancialmente mais barato/inferior" (ou qualquer linguagem de
      comparação/ranking) cruzando um quarter fechado com um aberto** — isso é
      precisamente a conclusão que a ressalva deveria impedir, não uma observação ao
      lado dela.
  - Depois de filtrar corretamente pelo quarter fechado, **se sobrar só 1 site com
    dado**, não há "mais barato" pra apontar (não é uma comparação, é um dado único) —
    diga isso explicitamente na resposta, e opcionalmente mencione o dado do quarter em
    andamento à parte (rotulado como preliminar/não fechado, sem comparar valores)
    (ver "Sempre informe ressalvas relevantes" nas Regras do agente).
- **"Evolução do custo de DL de [Site/Produto] ao longo dos trimestres"** — antes de
  montar qualquer gráfico/série, confira o que realmente existe: `fact_final_dl_cost`
  tem só ~19 linhas no total, então é comum um site/produto ter só 1-2 pontos, não uma
  série contínua. Passos:
  1. Busque TODAS as linhas do site/produto pedido (sem filtro de período — é pra
     mapear o que existe, não pra responder ainda).
  2. Verifique se as linhas são do MESMO produto — se vierem produtos diferentes (caso
     real: `JAG` só tem `AFFINITY` no Q1,2026 e `ARUBA21` no Q3,2026, nenhum overlap),
     **não é uma evolução/série temporal válida**, são pontos isolados de coisas
     diferentes. Diga isso explicitamente em vez de plotar como se fosse uma tendência.
  3. Verifique `start_month` de cada linha contra a data de hoje — linhas com
     `start_month` no futuro (quarter que nem começou, ver "não tem `is_forecast`"
     acima) NÃO são custo já incorrido; nunca as trate como ponto real da série sem
     rotular explicitamente como projeção/planejamento, se é que é isso (o schema não
     confirma, só a data sugere).
  4. Só monte gráfico de evolução se sobrar mais de 1 quarter FECHADO do MESMO produto
     — caso contrário, responda em texto explicando por que não há série pra mostrar
     (dado insuficiente ou descontínuo), em vez de forçar um gráfico com 1-2 pontos.
- **"Evolução do Standard Time de [Produto] em [Site] no último ano"** — série temporal
  de `fact_standard_time.standard_time` (ou `uph`/`upph`) ao longo dos 4 quarters do ano
  fiscal, filtrando por `product_id`/`site_id`. ⚠️ Nesta base de dev, `site_id` e
  `product_id` de `fact_standard_time` estão sempre `NULL` — avise o usuário que o filtro
  não retorna dado aqui, mesmo sendo a pergunta correta pelo schema.
- **"Comparativo de Hourly Rate das operações no último ano" / "qual está mais barato"**
  — calcule o HR médio por Site e por mês reconstruindo o vigente (ver fórmula "HR médio
  de uma Operação" na regra de vigência do Hourly Rate — **nunca a média ingênua de
  `APPROVED`**, já provado que inverte o ranking entre sites). Dois cuidados extras
  específicos desta pergunta:
  - ⚠️ **`fact_hourly_rate` não tem 12 meses de histórico** — confira o range real de
    `month_id` antes de assumir "último ano" (na base observada vai só de `2026-03` a
    `2026-09`, ~6 meses, e varia por site — `MAN` só tem dado do mês corrente). Responda
    com o período que realmente existe, e avise que não é um ano completo.
  - Aplica-se aqui a regra geral "Sempre informe ressalvas relevantes" e a de não liderar
    com comparação cruzando dado do quarter/mês em andamento — mesmo raciocínio já usado
    pra custo de `fact_final_dl_cost`.

## Domínio de negócio

### Glossário de negócio (termos oficiais, confirmados com especialista do produto)

- **DL (Direct Labor)** — módulo responsável pelo gerenciamento e cálculo dos custos de
  mão de obra direta do processo produtivo. Custos de DL podem ser cadastrados,
  simulados, aprovados e consolidados para compor depois o custo total de manufatura.
- **DL Cost** — valor monetário da mão de obra direta necessária para fabricar o
  produto; representado no módulo/tabela `fact_final_dl_cost` (coluna `dl_cost_by_cm`
  / `dl_cost_by_cm_usd`).
- **Operação** = **Site** (`dim_site`) — local onde o produto é produzido, identificado
  por país, abreviação do site, ou nome da empresa que executa a manufatura (= CM).
- **CM (Contracted Manufacturer)** = `dim_site.company` — a fábrica contratada dona do
  site.
- **Product / Produto** = `dim_product` (ex.: `AVENGER26`, `BERLIN21`) — modelo principal
  do produto, usado para agrupar variantes comerciais (Sales Models). Confirmado nos
  dados: `AVENGER26` e `BERLIN21` são linhas de `dim_product`, não de `dim_product_family`.
- **Sales Model (SM)** — variante comercial de um Product, para um mercado/região/negócio
  específico. Um Product pode ter vários SMs; cada SM pertence a um único Product; todo
  Product tem pelo menos um SM. `dim_sales_model` no schema. Filtrável via a tabela
  ponte `bridge_final_dl_cost_sales_model`, mas o custo NÃO é alocado por SM — ver
  seção dedicada nas Regras do agente antes de responder qualquer pergunta de custo
  por Sales Model.
- **Quarter** — unidade temporal do calendário fiscal Motorola/Lenovo (ano fiscal
  Abril–Março), usada para vigência de custos, comparações QoQ (quarter-over-quarter) e
  Forecast. Ver seção "Calendário fiscal" nas Regras do agente para a regra de rótulo do
  ano fiscal (FY) — não confundir com `dim_calendar.year`.

### O que é este sistema

Sistema de cálculo e acompanhamento do custo de mão de obra direta (DL — Direct Labor)
pago a fábricas contratadas (CMs — Contract Manufacturers) que fabricam os produtos da
marca (celulares e tablets). Rastreia os insumos do cálculo — taxa horária por site/produto/mês,
tempo padrão de produção por área/estágio de processo, descontos negociados (automação,
acordos de longo prazo, ajustes trimestre-sobre-trimestre) — e o resultado final: o custo
de DL por site/produto/mês, decomposto por área de processo e convertido para USD.

### Fluxo dos dados

Os insumos (`fact_hourly_rate`, `fact_standard_time`, `fact_dl_discount`) passam por um
fluxo de aprovação próprio (`status_approval`: `APPROVED` = vigente, `EXPIRED` = substituído)
e podem ser valores projetados (`is_forecast = true`) ou negociados/reais (`false`). A partir
desses insumos, o backend calcula `fact_final_dl_cost` — uma linha por site/produto/mês/trimestre
fiscal com o custo já decomposto por área (`backend_cost`, `cfc_cost`, `smt_cost`, `l5_cost`) e
totalizado (`dl_cost_by_cm`, convertido em `dl_cost_by_cm_usd`).

### Áreas de processo produtivo

`BACKEND`, `CFC`, `SMT` (Surface Mount Technology — montagem de componentes em placa,
termo padrão da indústria), `L5` (estágio final de montagem/teste, termo comum em
manufatura de celulares) — mesmas quatro áreas em `fact_hourly_rate`, `fact_standard_time`,
`fact_dl_discount` e nas colunas decompostas de `fact_final_dl_cost`.

### KPIs usuais

- Custo DL total (USD): `SUM(dl_cost_by_cm_usd)` de `fact_final_dl_cost`
- Custo DL por site: `SUM(dl_cost_by_cm_usd)` agrupado por `site_id`
- Custo DL por área de processo: somar `backend_cost`/`cfc_cost`/`smt_cost`/`l5_cost` de `fact_final_dl_cost`
- Taxa horária "atual" (snapshot de agora, sem olhar histórico): `AVG(hourly_rate)` de
  `fact_hourly_rate` com `status_approval = 'APPROVED'`. ⚠️ **NÃO é a fórmula de "taxa
  vigente" pra perguntas que cruzam meses/comparam sites ao longo do tempo** (ex.:
  "comparativo de Hourly Rate no último ano") — pra isso, já provado que essa fórmula
  simples inverte o ranking entre sites; use a reconstrução mês a mês em "Regra de
  vigência do Hourly Rate" (Modelo de dados).
- Desconto médio negociado por tipo: `AVG(discount_value)` de `fact_dl_discount`
  agrupado por `discount_type` — ⚠️ **não testado** se `fact_dl_discount` tem o mesmo
  problema de vigência do Hourly Rate (a tabela também tem `status_approval`
  `APPROVED`/`EXPIRED`, mas usa janela `start_date`/`end_date` em vez de um único
  `month_id` — pode se comportar diferente). Trate esta fórmula com cautela em
  perguntas que cruzam período, até confirmar com dado real.
- Produtividade: `AVG(uph)` / `AVG(upph)` de `fact_standard_time` — ⚠️ **não testado**
  se há o mesmo problema de vigência (mesma ressalva acima; esta tabela também tem
  `status_approval`). Além disso, lembre que `site_id`/`product_id` desta tabela estão
  100% `NULL` na base observada (ver "Modelo de dados").

## Exemplos task_code

> Neste domínio (`database.modo: graphql`), `ctx.sql()` NÃO funciona — use
> `ctx.graphql_table(...)` (equivalente a `executar_graphql`) ou `ctx.graphql_aggregate(...)`
> (equivalente a `executar_graphql_agregado`), mesmos argumentos e schema documentados em
> "Regras do agente" acima. Os exemplos com `ctx.sql()` abaixo só se aplicam se este domínio
> voltar a `modo: sql`.

### Painel/relatório com ctx.graphql_aggregate — custo final de DL por site

⚠️ `start_month`/`end_month` são a janela do TRIMESTRE FISCAL inteiro (ver "Modelo de
dados" acima), não uma data pontual — filtrar com `start_month GTE from_date AND
end_month LTE to_date` exige que o trimestre INTEIRO caiba dentro do período pedido e
descarta o trimestre em andamento (cujo `end_month` é sempre no futuro). Use
sobreposição de intervalo (`start_month LTE to_date AND end_month GTE from_date`), que
é a comparação certa para "existe uma linha cujo trimestre toca este período".

```python
def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_final_dl_cost", group_by="site_id",
        metric_field="dl_cost_by_cm_usd", metric_op="SUM",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"},
               {"field": "start_month", "op": "LTE", "value": to_date},
               {"field": "end_month", "op": "GTE", "value": from_date}],
        expand=["name", "country"], order_by="DESC", limit=20,
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return ctx.generate_pdf('Custo DL por Site', '## Sem dados no período.')
    df['metricValue'] = pd.to_numeric(df['metricValue'])
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(df['name'], df['metricValue'], color='#60a5fa')
    ax.set_title(f'Custo DL por Site (USD) — {from_date} a {to_date}')
    plt.tight_layout()
    chart = ctx.save_chart(fig)
    conteudo = (
        f"## Custo DL por Site\n\n{chart}\n\n"
        f"- Período: {from_date} a {to_date}\n"
        f"- Custo total (USD): {df['metricValue'].sum():,.2f}\n"
    )
    return ctx.generate_pdf('Custo DL por Site', conteudo)
```

### Relatório PDF com gráfico (referência — só em modo: sql)

```python
def run(from_date, to_date, ctx):
    rows = ctx.sql(f"""
        SELECT s.name AS site, SUM(f.dl_cost_by_cm_usd) AS custo_usd
        FROM fact_final_dl_cost f
        JOIN dim_site s ON s.id = f.site_id
        WHERE f.start_month BETWEEN '{from_date}' AND '{to_date}'
        GROUP BY s.name ORDER BY custo_usd DESC
    """)
    df = pd.DataFrame(rows)
    if df.empty:
        return ctx.generate_pdf('Custo DL por Site', '## Sem dados no período.')
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(df['site'], df['custo_usd'], color='#60a5fa')
    ax.set_title(f'Custo DL por Site (USD) — {from_date} a {to_date}')
    plt.tight_layout()
    chart = ctx.save_chart(fig)
    total = float(df['custo_usd'].sum())
    conteudo = (
        f"## Custo DL por Site\n\n{chart}\n\n"
        f"- Período: {from_date} a {to_date}\n"
        f"- Custo total (USD): {total:,.2f}\n"
    )
    return ctx.generate_pdf('Custo DL por Site', conteudo)
```

### Planilha Excel (referência — só em modo: sql)

```python
def run(from_date, to_date, ctx):
    rows = ctx.sql(f"""
        SELECT f.month_id, s.name AS site, p.name AS produto,
               f.dl_cost_by_cm_usd, f.status_approval
        FROM fact_final_dl_cost f
        JOIN dim_site s ON s.id = f.site_id
        JOIN dim_product p ON p.id = f.product_id
        WHERE f.start_month BETWEEN '{from_date}' AND '{to_date}'
    """)
    df = pd.DataFrame(rows)
    return ctx.generate_excel(df, 'custo_dl')
```

## Dica para condition_sql

> ⚠️ Este domínio usa `modo: graphql` — `condition_sql` NÃO é uma query SQL aqui, é uma
> string JSON (ver formato completo na seção "Formato de condition_sql" do prompt de
> sistema do scheduler). Exemplo pronto para monitorar `dl_cost_by_cm_usd` de um site
> específico (resolva o `id` do site via `dim_site` antes — filtre por `id`, não por
> `name` — ver padrão "Custo de [site]" acima):
>
> ```json
> {"type": "aggregate", "table": "fact_final_dl_cost", "group_by": "site_id",
>  "metric_field": "dl_cost_by_cm_usd", "metric_op": "SUM",
>  "where": [{"field": "site_id", "op": "EQ", "value": "<uuid do site>"},
>            {"field": "status_approval", "op": "EQ", "value": "APPROVED"}]}
> ```
>
> O valor comparado ao `condition_threshold` é sempre `metricValue`.

Tabelas disponíveis: `dim_site`, `dim_product`, `fact_hourly_rate`, `fact_dl_discount`,
`fact_final_dl_cost`. Datas de vigência ficam em `month_id`/`effective_date`/`start_date`,
não em `approved_at`.
