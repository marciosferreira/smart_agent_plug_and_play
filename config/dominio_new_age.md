# skill: dominio
# descricao: Análise de custo de mão de obra direta (DL cost) pago a fábricas contratadas (CMs) — taxas horárias, tempos padrão de produção, descontos negociados e custo final por site/produto/mês. Consulta via GraphQL (reportTable) no backend new_age.
# palavras-chave: dl cost, custo, mão de obra, hourly rate, taxa horária, standard time, tempo padrão, discount, desconto, site, operação, fábrica, cm, contract manufacturer, backend, cfc, smt, l5, forecast, aprovação, lta, qoq, sales model, produto, ano fiscal, fy, quarter, vigência

---

## Regras do agente

### Acesso via GraphQL — duas tools disponíveis (mesmo mecanismo do posh)

Existem duas tools de leitura de dados neste domínio — escolha pela natureza do pedido:

- **`executar_graphql`** — linhas cruas de uma tabela (`reportTable`). Use quando
  precisa dos dados em nível de registro (listar, detalhar, ou quando o
  processamento depois em `analisar_dataframe` precisa de mais de uma coluna
  numérica/lógica combinada).
- **`executar_graphql_agregado`** — soma/contagem/média/mínimo/máximo já agrupados
  no banco (`reportAggregate`). Use SEMPRE que o pedido for do tipo "quanto custou",
  "total de", "média de", "top N por" — é mais rápido e não traz a tabela inteira
  só para agregar depois em pandas.

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
| `fact_final_dl_cost` | `month_id` + `start_month`/`end_month` (janela do trimestre fiscal) | `approved_at` (frequentemente `NULL`) |

Para "quanto custou em [período]" use a data de negócio, não `approved_at` — `approved_at`
responde "quando foi aprovado", não "a que mês/período o valor se refere".

### `status_approval` e `is_forecast` — valores observados

Valores vistos no ambiente de dev/non-prod: `APPROVED`, `EXPIRED` (podem existir outros,
ex. `PENDING`/`REJECTED`, não vistos nesta base — trate como enum não fechado). Para "custo
atual"/"taxa vigente", filtre `status_approval = 'APPROVED'` — `EXPIRED` são versões
substituídas mantidas por histórico.

⚠️ `fact_final_dl_cost` **não tem** coluna `is_forecast` (as outras três facts têm).
`is_forecast = true` marca valor projetado/estimado; `false` = valor real negociado/realizado.

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

### `Sales Model` — conceito de negócio existe, mas NÃO é filtrável no schema atual

O glossário de negócio define uma granularidade abaixo de "Produto" chamada **Sales
Model** (variante comercial de um Produto para um mercado/região específico — cada
Produto tem 1 ou mais Sales Models). Especialistas descrevem `fact_final_dl_cost` como
granular por "Produto, Sales Model, Quarter, Site" — **mas o schema exposto por este
GraphQL (ver `reportSchema`) não tem `sales_model_id` em `fact_final_dl_cost`,
`fact_hourly_rate` nem `fact_dl_discount`** — só `fact_standard_time` tem essa coluna,
e ela veio 100% `NULL` em toda a amostra verificada (1000 linhas). Não existe tabela
`dim_sales_model` na lista de `reportTables`.

Conclusão prática: perguntas do tipo "qual Sales Model é mais barato" **não podem ser
respondidas nesse nível de detalhe hoje** — a menor granularidade de produto disponível
via `executar_graphql`/`executar_graphql_agregado` é `dim_product` (o "Produto" do
glossário, ex.: `AVENGER26`, `BERLIN21` — confirmado que são linhas de `dim_product`,
não de `dim_product_family`). Ao receber esse tipo de pergunta, responda no nível de
Produto e avise explicitamente que a quebra por Sales Model não está disponível nos
dados de hoje (pode ser uma limitação só deste ambiente dev/non-prod).

## Regras do orquestrador

Este é um sistema de custo de mão de obra direta (DL cost) pago a fábricas contratadas (CMs)
que fabricam os produtos — não é um sistema de pedidos de compra (isso é o domínio posh) nem
de alocação de supply. Se o usuário NÃO mencionar período nenhum, use
calcular_periodo('ultimo quarter') como padrão (o quarter fiscal fechado mais recente — este
domínio não tem `database.url`/SQL, então `calcular_periodo('todo o período')` NÃO funciona
aqui, ao contrário de domínios com modo: sql) e siga direto para consultar_analista — NUNCA
pergunte o período ao usuário.

Ao delegar a consultar_analista, avise de antemão sobre as duas limitações de dado mais
comuns deste domínio (evita ida e volta desnecessária): (1) "Sales Model" não é filtrável —
análises de produto param no nível de Produto (`dim_product`); (2) `fact_standard_time` tem
`site_id`/`product_id` sempre nulos nesta base — perguntas de Standard Time por produto/site
provavelmente não retornam dado. Se a pergunta tocar nesses temas, inclua isso na instrução
já na primeira chamada a consultar_analista, em vez de aguardar a resposta dele para descobrir.

## Períodos

> ⚠️ Esta seção depende de acesso SQL direto (`db_config.run_select`, caminho interno que
> não passa pela tool ativa nem depende de `database.modo`). Com o domínio em `modo: graphql`
> e sem `database.url` configurada, a resolução abaixo falha silenciosamente e o sistema cai
> no fallback de calendário — "todo o período" usará a data de hoje, não o range real dos
> dados. Sem impacto para períodos comuns (hoje, esta semana, mês corrente etc.), que são
> resolvidos por calendário de qualquer forma.

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
| month_id | FK → `dim_calendar.id` — **"Effective Month"**: mês a partir do qual a taxa passa a valer (ver regra de vigência abaixo) |
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
  isso pode não retornar nada se o último HR aprovado foi de um mês anterior. O correto
  é: para cada combinação site/produto/área, pegar o registro `APPROVED` com o MAIOR
  `month_id` que seja `<= X` (o mais recente vigente naquele ponto do tempo).
- **HR médio de um Produto num Site, num mês**: média do `hourly_rate` vigente entre
  as áreas (`BACKEND`/`CFC`/`SMT`/`L5`) daquele site+produto — na prática costuma ser o
  mesmo valor nas áreas, mas trate como média, não como valor único.
- **HR médio de uma Operação (Site), por mês**: para cada site e cada mês, somar o HR
  vigente de TODOS os produtos ativos naquele site naquele mês e dividir pelo número de
  produtos — dá a média do site no mês, útil para comparar sites/operações entre si
  (ex.: "comparativo de Hourly Rate das operações no último ano").

### `fact_standard_time` — tempo padrão de produção por área/estágio

| Coluna | Significado |
|--------|-------------|
| id | PK |
| site_id | FK → `dim_site.id`, nullable — ⚠️ **100% NULL nesta base** (verificado nas 1000 linhas amostradas, não é só uma amostra pequena) — perguntas de Standard Time segmentadas por site provavelmente não retornam dado aqui, mesmo sendo suportadas pelo schema; validar se isso muda em produção |
| product_id | FK → `dim_product.id`, nullable — mesma observação de `site_id` (100% NULL nas 1000 linhas amostradas) |
| sales_model_id | Referencia o conceito de negócio "Sales Model" (ver seção "Sales Model" nas Regras do agente), mas sem `dim_sales_model` exposta pelo schema — ⚠️ **100% NULL nesta base** |
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
| month_id | FK → `dim_calendar.id` |
| quarter | Texto `"Q<n>,<ano>"` (trimestre fiscal, ex.: `"Q2,2026"`) — ver seção de calendário fiscal acima |
| start_month / end_month | Janela do trimestre fiscal correspondente (ex.: `Q2,2026` → 2026-07-01 a 2026-09-30) |
| lta_discount | Desconto tipo LTA aplicado no cálculo (pode vir `NULL` quando não se aplica) |
| qoq_configuration | Configuração de ajuste trimestre-sobre-trimestre aplicada |
| standard_time_catalog | Tempo padrão de referência usado no cálculo |
| hourly_rate | Taxa horária usada no cálculo |
| backend_cost / cfc_cost / smt_cost / l5_cost | Custo decomposto por área de processo (mesmas áreas de `fact_hourly_rate`/`fact_standard_time`) |
| dl_cost_by_cm | Custo total de DL, na moeda do CM (moeda local) |
| dl_cost_by_cm_usd | Custo total de DL convertido para USD — **preferir esta coluna para comparações entre sites/países** |
| currency_rate | Taxa de câmbio usada na conversão para USD |
| status_approval | enum — só `APPROVED` observado nesta base |
| approved_at | Frequentemente `NULL` nesta base (tabela parece ser resultado calculado, não um fluxo de aprovação manual como as demais) |

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
```

### Exemplos de uso da tool executar_graphql

```
-- Custo final de DL no período, com nome do site e do produto já juntos
executar_graphql(
    table="fact_final_dl_cost", chave="custo",
    where=[{"field": "month_id", "op": "GTE", "value": "2026-01"},
           {"field": "month_id", "op": "LTE", "value": "2026-06"}],
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
executar_graphql_agregado(
    table="fact_hourly_rate", chave="taxa_media_por_area",
    group_by="area", metric_field="hourly_rate", metric_op="AVG",
    where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
    order_by="DESC",
)
```

### Padrões de pergunta validados com especialista do produto

- **"Custo de DL de [empresa/CM]" vs. "custo de [site]"** — agregue `fact_final_dl_cost`
  por `site_id` com `expand=[{"viaColumn": "site_id", "columns": ["company"]}]` e depois
  agrupe por `site_id__company` em pandas para o nível empresa; para um site específico,
  filtre `where=[{"field":"site_id","op":"EQ","value":"<id>"}]` (resolva o id pelo `name`
  antes, ex.: `dim_site` com `where name = 'JAG'`).
- **"Qual [Produto] mais barato de produzir em [Site]"** — MIN de `dl_cost_by_cm` (ou
  `dl_cost_by_cm_usd`) em `fact_final_dl_cost`, filtrando `product_id` e `site_id`,
  com `status_approval = 'APPROVED'`, na média/menor valor até o último quarter fechado
  (ver "Período padrão"). Se o pedido for por "Sales Model", ver limitação dedicada nas
  Regras do agente — responda no nível de Produto.
- **"Em qual site foi mais barato produzir [Produto] no último quarter"** — filtre
  `fact_final_dl_cost` por `product_id` e pelo `quarter` fiscal fechado mais recente
  (via `start_month`/`end_month`, não monte o texto do quarter manualmente — ver seção
  de calendário fiscal), agrupe por `site_id` e pegue o menor `dl_cost_by_cm`.
- **"Evolução do Standard Time de [Produto] em [Site] no último ano"** — série temporal
  de `fact_standard_time.standard_time` (ou `uph`/`upph`) ao longo dos 4 quarters do ano
  fiscal, filtrando por `product_id`/`site_id`. ⚠️ Nesta base de dev, `site_id` e
  `product_id` de `fact_standard_time` estão sempre `NULL` — avise o usuário que o filtro
  não retorna dado aqui, mesmo sendo a pergunta correta pelo schema.
- **"Comparativo de Hourly Rate das operações no último ano"** — calcule o HR médio por
  Site e por mês (ver fórmula "HR médio de uma Operação" na regra de vigência do Hourly
  Rate) para os últimos 12 meses / 4 quarters fechados, depois compare entre sites.

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
  Product tem pelo menos um SM. **Não é filtrável no schema atual** — ver seção dedicada
  nas Regras do agente.
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
- Taxa horária média vigente: `AVG(hourly_rate)` de `fact_hourly_rate` com `status_approval = 'APPROVED'`
- Desconto médio negociado por tipo: `AVG(discount_value)` de `fact_dl_discount` agrupado por `discount_type`
- Produtividade: `AVG(uph)` / `AVG(upph)` de `fact_standard_time`

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
