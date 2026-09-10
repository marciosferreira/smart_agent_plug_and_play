# skill: dominio
# descricao: Análise de pedidos de compra (Brazil Purchase Orders) — pedidos, itens, clientes, produtos, erros/inconsistências e alertas. Consulta via GraphQL (reportTable) no schema brazil.
# palavras-chave: pedidos, pedido, itens, clientes, produtos, erros, alertas, inconsistências, status, aprovados, rejeitados, quantidade, valor, ranking, canal, regional

---

## Regras do agente

### Colunas de data — use sempre a coluna certa

A dimensão temporal principal dos pedidos é `created_at` (data de entrada no sistema).
Para "pedidos deste mês", "pedidos de hoje", "pedidos por período" — use sempre `created_at`.
`issue_date` é a data original informada pelo cliente; `delivery_month` é o mês de entrega
desejado. NÃO existe coluna genérica `date` neste sistema.

### Acesso via GraphQL — duas tools disponíveis

Existem duas tools de leitura de dados neste domínio — escolha pela natureza do pedido:

- **`executar_graphql`** — linhas cruas de uma tabela (`reportTable`). Use quando
  precisa dos dados em nível de item/registro (listar, detalhar, ou quando o
  processamento depois em `analisar_dataframe` precisa de mais de uma coluna
  numérica/lógica combinada).
- **`executar_graphql_agregado`** — contagem/soma/média/mínimo/máximo já agrupados
  no banco (`reportAggregate`). Use SEMPRE que o pedido for do tipo "quantos",
  "total de", "média de", "top N por" — é mais rápido e não traz a tabela inteira
  só para agregar depois em pandas. Ver subseção própria abaixo.

Ambas fazem parte do que está **de fato disponível para uso hoje**, mesmo que o
servidor GraphQL saiba fazer mais (ver "Recursos do servidor NÃO disponíveis
nestas tools" no fim desta seção).

#### Regras de `executar_graphql` (reportTable)

- `table` recebe o nome da tabela **sem prefixo de schema** — `"purchase_order"`,
  não `"brazil.purchase_order"`. O backend valida contra `information_schema.tables`
  comparando o nome exato; com prefixo não bate com nada e a consulta falha
  ("Tabela não encontrada").
- Não há cast de tipo — enums e datas voltam como string simples (sem `::text`,
  sem `TO_CHAR`). Filtre datas em `where` com os operadores `GTE`/`LTE` sobre
  `created_at`, comparando com string ISO (`"2026-01-01"`).
- `expand` faz join de **um salto** com uma tabela relacionada via FK — passe
  `[{"viaColumn": "customer_id", "columns": ["name", "channel"]}]` pra trazer
  colunas de `customer` já na mesma linha de `purchase_order`, prefixadas
  `customer_id__name`, `customer_id__channel` (sem merge em pandas depois).
  `viaColumn` tem que ser FK de `table` — não serve para combinar tabelas que
  não têm FK direta entre si, nem para joins de mais de um salto (ex: `order_item`
  até `customer` passa por `purchase_order` no meio — isso ainda precisa de duas
  chamadas + `pandas.merge()`). Para esses casos, ver "Grafo de relações" abaixo
  para as chaves de cada relação.
- Operadores válidos em `where`: `EQ`, `NEQ`, `GT`, `GTE`, `LT`, `LTE`, `IN`,
  `NOT_IN`, `LIKE`, `ILIKE`, `IS_NULL`, `IS_NOT_NULL`. Múltiplas condições no
  mesmo `where` são combinadas com AND. `IN`/`NOT_IN` esperam `value` como lista.
  `IS_NULL`/`IS_NOT_NULL` não usam `value`.
- Sem `SELECT *` equivalente: `reportTable` sempre retorna todas as colunas da
  tabela (`SELECT t.*`) — descarte as que não precisar já no `analisar_dataframe`.
- ⚠️ **Sem `limit`, a consulta traz a tabela inteira, sem paginação nenhuma**
  (o servidor não aplica nenhum limite por padrão). Em tabelas potencialmente
  grandes (`purchase_order`, `order_item`, `order_snapshot_item_issue`,
  `resolution_memory`), sempre passe `limit` a menos que realmente precise de
  tudo.
- Exemplo validado — pedidos processados já com nome e canal do cliente juntos:
  ```
  executar_graphql(
      table="purchase_order", chave="pedidos",
      where=[{"field": "status", "op": "EQ", "value": "PROCESSED"}],
      expand=[{"viaColumn": "customer_id", "columns": ["name", "channel"]}],
      limit=100,
  )
  -- em analisar_dataframe: pedidos já vem com customer_id__name,
  -- customer_id__channel prontos, sem precisar buscar customer à parte
  ```

#### Regras de `executar_graphql_agregado` (reportAggregate)

- Argumentos: `table`, `chave`, `group_by` (coluna de `table` pro GROUP BY),
  `metric_field` (coluna a agregar), `metric_op` (`SUM`/`COUNT`/`AVG`/`MIN`/`MAX`),
  `where` (mesmo formato de `executar_graphql`), `expand`, `order_by` (`"ASC"`/`"DESC"`,
  ordena pelo valor agregado), `limit` (default do servidor: 20, teto: 500).
- O resultado vem com colunas `groupKey` (valor de `group_by`), `metricValue`
  (resultado da agregação) e uma por coluna pedida em `expand`.
- `expand` aqui é **lista de nomes de coluna** (`["name", "channel"]`), formato
  diferente do `expand` de `reportTable` — e só funciona se `group_by` for uma
  foreign key (traz colunas da tabela referenciada por ela, um salto só; não
  serve para combinar tabelas que não têm FK direta entre si).
- `metricValue` e `groupKey` podem vir como string mesmo sendo número (o backend
  não faz cast) — converta com `pd.to_numeric` se for usar em conta matemática.
- Exemplo validado — contagem de pedidos PROCESSED por cliente, já com nome do
  cliente junto (sem precisar buscar `customer` à parte e dar merge):
  ```
  executar_graphql_agregado(
      table="purchase_order", chave="por_cliente",
      group_by="customer_id", metric_field="id", metric_op="COUNT",
      where=[{"field": "status", "op": "EQ", "value": "PROCESSED"}],
      expand=["name", "channel"], order_by="DESC", limit=10,
  )
  -- em analisar_dataframe: por_cliente já vem pronto, uma linha por cliente,
  -- colunas groupKey (customer_id), name, channel, metricValue (contagem)
  ```

#### Recursos do servidor GraphQL NÃO disponíveis nestas tools hoje

O backend (`ReportResolver`) expõe mais do que essas tools usam — documentado
aqui só como referência, para não tentar chamar algo que não existe do lado da tool:

- `reportTableRelations(table)` — relações de FK de uma tabela só (mais leve
  que `reportSchema` completo).
- `reportTables` — lista simples dos nomes de tabela do schema `brazil`.

Se algum dia esses recursos forem expostos nas tools, esta seção precisa ser
revisada.

### Motivo do erro de um item — nunca use o status como descrição

`order_item.status = 'INCONSISTENCY'` indica que há problema, mas o MOTIVO está em
`error_type.name` via `order_item.error_type_id`. Para alertas (`alert_resolve`),
o tipo de erro depende do status do pedido:
- `INCONSISTENCY`/`REJECTED` → junte (via pandas) `alert_resolve_error_type` com `error_type`
- `PROCESSED` → `order_snapshot_item_issue.alert_name` (via `order_snapshot`);
  ao juntar com `alert_resolve_error_type`, o resultado vem vazio para PROCESSED —
  não significa "sem erro".

## Regras do orquestrador

Este é um sistema de gestão de pedidos de compra (Brazil Purchase Orders) — 'pedido' significa
SEMPRE purchase_order. NÃO é um sistema de chão-de-fábrica nem de alocação de supply.
Se o usuário NÃO mencionar período nenhum, use calcular_periodo('todo o período') como padrão
e siga direto para consultar_analista — NUNCA pergunte o período ao usuário.

## Períodos

> ⚠️ A consulta SQL abaixo não executa neste domínio: depende de `db_config.run_select`,
> que exige `database.url`, ausente em `modo: graphql`. Com o domínio em `modo: graphql`,
> a resolução do range real dos dados fica a cargo do fallback genérico de
> `calcular_periodo` em `agent_multi.py` — "todo o período"/"histórico completo"/"desde o
> início" resolve para `2000-01-01` até hoje (cobre qualquer domínio sem SQL), não para
> "hoje". A regra do orquestrador abaixo (usar `calcular_periodo('todo o período')` como
> padrão) já reflete isso corretamente. O bloco SQL ficou apenas como referência para o
> caso deste domínio um dia rodar em `modo: sql`.

### re:todo o per[ií]odo|hist[oó]rico completo|desde o in[ií]cio

```sql
SELECT MIN(created_at::date), MAX(created_at::date) FROM brazil.purchase_order
```

## Modelo de dados

Todas as tabelas abaixo existem no schema `brazil`, mas **não use o prefixo** ao chamar
`executar_graphql` — o argumento `table` já assume esse schema implicitamente.

### `purchase_order` — pedido de compra (entidade central)

| Coluna | Significado |
|--------|-------------|
| id | PK interna |
| order_number | Número do pedido conforme enviado pelo cliente |
| customer_id | FK → customer.id |
| customer_name | Nome desnormalizado — prefira juntar com customer.name (mais confiável) |
| delivery_customer_id | FK → customer.id — cliente de **entrega**, quando difere do cliente do pedido (`customer_id`). NULL = mesmo cliente. Para análises de destino/entrega, junte por esta coluna, não por `customer_id` |
| file_import_id | FK → file_import.id (arquivo que originou o pedido) |
| issue_date | Data em que o cliente emitiu o pedido |
| created_at | **Data de entrada no sistema — dimensão temporal principal** |
| delivery_month | Mês desejado de entrega |
| status | enum: `PROCESSED` · `INCONSISTENCY` · `REJECTED` — **`PROCESSED` = aprovado** (corrigido, se necessário, e processado). Não existem `APPROVED` nem `PENDING` no enum real |
| customer_usage_order | enum `CU1`/`CU2` |
| justification_reject | Motivo da rejeição (texto livre) |

### `order_item` — item de um pedido (1..N por pedido)

| Coluna | Significado |
|--------|-------------|
| id | PK interna |
| purchase_order_id | FK → purchase_order.id |
| product_id | FK → product.id (NULL = produto não identificado) |
| accessory_id | FK → accessory.id (NULL = acessório não identificado) |
| item_number | Posição do item no pedido |
| status | enum: `PROCESSED` · `INCONSISTENCY` · `REJECTED` (mesmos valores do pedido, mas independente) |
| quantity | Unidades pedidas |
| value_price_total | Valor monetário total do item |
| product_group | Família: `SMARTPHONE` · `TABLET` · `ACESSÓRIO` etc. |
| local_market_name / local_color | Nome comercial local / cor conforme pedido |
| delivery_week | Semana de entrega desejada |
| error_type_id | FK → error_type.id — preenchido quando o item tem erro |
| created_at | Data de criação do item |

### `customer` — cliente

| Coluna | Significado |
|--------|-------------|
| id, name, customer_short, cnpj | Identificação |
| channel | enum canal de venda |
| state | enum UF |
| regional | Regional comercial |
| city_id | FK → city.id |
| sold_customer_number / ship_customer_number | Códigos ERP |

### `product` / `accessory` — catálogos

product: part_number, market_name, local_market_name, product_group, local_color,
status (enum `ACTIVE`/`INACTIVE`), ram, rom, ean, origin.
accessory: part_number, market_name, local_market_name, product_group, status, ean.

### `error_type` — dicionário de erros

`id`, `name` (enum). Valores: `CNPJ_CUSTOMER_NOT_IDENTIFIED`,
`CNPJ_DELIVERY_NOT_IDENTIFIED`, `PART_NUMBER_NOT_IDENTIFIED`, `INCORRECT_QUANTITY`,
`NOT_FOUND_DELIVERY_MONTH`, `ACCESSORY_MISSING_FOR_BUNDLE`, `ACCESSORY_NOT_MAPPED_TO_BUNDLE`.

### `alert_resolve` — resolução de inconsistências

`id`, `purchase_order_id` (FK — o status REAL do alerta é `purchase_order.status`),
`customer_id` (pode ser NULL), `file_import_id`, `created_at` (dimensão temporal),
`resolved_at`, `notify_reject_date`.
⚠️ NÃO use `resolved_at IS NOT NULL` como filtro de "resolvido" — use `purchase_order.status`.

### `alert_resolve_error_type` — N:N alerta↔erro (só INCONSISTENCY/REJECTED)

`alert_resolve_id`, `error_type_id`. ⚠️ Para pedidos PROCESSED os erros migram para
`order_snapshot_item_issue.alert_name` (via `order_snapshot.purchase_order_id`).

### `order_snapshot` / `order_snapshot_item_issue`

Snapshot do pedido no processamento; item_issue tem `alert_name` (nome do erro pronto,
sem precisar juntar com error_type), `part_number`, `resolved_at`. FK `order_snapshot_id`
→ `order_snapshot.id`. Também carrega `resolved_product_id` → `product.id` [nullable] e
`resolved_accessory_id` → `accessory.id` [nullable] — o produto/acessório efetivamente
resolvido para o item, quando a resolução envolveu identificação de produto.

### `product_accessory` — mapeamento produto↔acessório (N:N)

`product_id` FK → `product.id`, `accessory_id` FK → `accessory.id`. Define quais
acessórios pertencem ao bundle de qual produto — é a tabela que resolve os erros
`ACCESSORY_MISSING_FOR_BUNDLE` e `ACCESSORY_NOT_MAPPED_TO_BUNDLE` (ver `error_type` acima).

### `customer_group` / `code_association`

`customer_group`: `id`, `name`, `cnpj_prefix` — agrupamento de clientes por prefixo de CNPJ.
`code_association`: `part_number`, `alias_type`, `alias_value`, `customer_group_id`
(FK → `customer_group.id` [nullable]) — mapeia códigos/aliases alternativos de part number
usados por um grupo de clientes específico.

### `resolution_memory` — memória de resoluções anteriores

Sem relação (FK) com as demais tabelas — chave de correspondência própria (`match_key`).
Guarda o histórico de como itens não identificados (`source_*`) foram resolvidos
(`resolved_part_number`, `resolved_ean`, `resolved_ram/rom/color`, etc.), usado para
sugerir resolução automática de novos itens com descrição parecida.

### `file_import` — arquivos importados

`id`, `file_name`, `file_extension`, `date_registered`.

### Grafo de relações (para juntar em pandas, não há JOIN no GraphQL)

```
purchase_order (1)──(N) order_item        via order_item.purchase_order_id
purchase_order (N)──(1) customer          via purchase_order.customer_id
purchase_order (N)──(1) customer          via purchase_order.delivery_customer_id [nullable, cliente de entrega]
order_item     (N)──(1) product           via order_item.product_id [nullable]
order_item     (N)──(1) accessory         via order_item.accessory_id [nullable]
order_item     (N)──(1) error_type        via order_item.error_type_id [nullable]
alert_resolve  (N)──(1) purchase_order    via alert_resolve.purchase_order_id
alert_resolve  (N)──(N) error_type        via alert_resolve_error_type
order_snapshot (N)──(1) purchase_order    via order_snapshot.purchase_order_id
order_snapshot_item_issue (N)──(1) order_snapshot via order_snapshot_item_issue.order_snapshot_id
order_snapshot_item_issue (N)──(1) product   via resolved_product_id [nullable]
order_snapshot_item_issue (N)──(1) accessory via resolved_accessory_id [nullable]
product        (N)──(N) accessory        via product_accessory
customer       (N)──(1) city              via customer.city_id [nullable]
code_association (N)──(1) customer_group via code_association.customer_group_id [nullable]
```

### Exemplos de uso da tool executar_graphql

```
-- Pedidos por status no período
executar_graphql(
    table="purchase_order",
    chave="pedidos",
    where=[{"field": "created_at", "op": "GTE", "value": "2026-01-01"},
           {"field": "created_at", "op": "LTE", "value": "2026-06-30"}],
)
-- depois, em analisar_dataframe: pedidos.groupby('status').size()
```

```
-- Top clientes por valor — duas chamadas + merge em pandas
executar_graphql(table="purchase_order", chave="pedidos", where=[...])
executar_graphql(table="customer", chave="clientes")
executar_graphql(table="order_item", chave="itens", where=[...])
-- em analisar_dataframe:
--   base = itens.merge(pedidos, left_on='purchase_order_id', right_on='id')
--   base = base.merge(clientes, left_on='customer_id', right_on='id')
--   base.groupby('name')['value_price_total'].sum().sort_values(ascending=False).head(10)
```

```
-- Erros mais frequentes em itens
executar_graphql(table="order_item", chave="itens", where=[{"field": "error_type_id", "op": "IS_NOT_NULL"}])
executar_graphql(table="error_type", chave="erros")
-- em analisar_dataframe: itens.merge(erros, left_on='error_type_id', right_on='id')['name'].value_counts()
```

## Domínio de negócio

### O que é este sistema

Plataforma de gestão de pedidos de compra do mercado Brazil: clientes enviam arquivos de
pedidos, o sistema importa, valida item a item e aprova/rejeita. Itens com problemas viram
inconsistências com tipo de erro identificado; alertas de resolução acompanham o tratamento.

### Fluxo de um pedido

importação (file_import) → validação → sem erro: `PROCESSED` direto; com erro:
`INCONSISTENCY` (aguarda resolução via alert_resolve) → resolvido vira `PROCESSED`
(aprovado, corrigido e processado) ou `REJECTED`.

### Status

⚠️ O enum real tem só 3 valores — confirmado testando cada valor contra o servidor
(valores fora do enum retornam erro `invalid input value for enum ..._status_enum`).
Não existe `PENDING` nem `APPROVED` no banco.

- **PROCESSED** — pedido aprovado: sem erro, ou com erro já corrigido e processado
- **INCONSISTENCY** — tem itens com erro, aguarda resolução (via alert_resolve)
- **REJECTED** — rejeitado (motivo em justification_reject)

### KPIs usuais

- Taxa de aprovação: pedidos PROCESSED / total
- Volume: COUNT de pedidos ou SUM(quantity) de itens
- Valor: SUM(value_price_total)
- Clientes únicos: nº de customer_id distintos
- Erros por tipo: junte order_item com error_type (pandas)

## Exemplos task_code

> ⚠️ Tarefas agendadas e monitores (`ctx.sql()`) exigem acesso SQL direto e não funcionam
> enquanto o domínio estiver em `modo: graphql` sem `database.url` configurada — `ctx` ainda
> não tem um equivalente GraphQL. Os exemplos abaixo só se aplicam se este domínio voltar a
> `modo: sql`.

### Relatório PDF com gráfico

```python
def run(from_date, to_date, ctx):
    rows = ctx.sql(f"""
        SELECT status::text AS status, COUNT(*) AS total
        FROM brazil.purchase_order
        WHERE created_at::date BETWEEN '{from_date}' AND '{to_date}'
        GROUP BY status ORDER BY total DESC
    """)
    df = pd.DataFrame(rows)
    if df.empty:
        return ctx.generate_pdf('Relatório de Pedidos', '## Sem dados no período.')
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(df['status'], df['total'], color='#60a5fa')
    ax.set_title(f'Pedidos por Status — {from_date} a {to_date}')
    plt.tight_layout()
    chart = ctx.save_chart(fig)
    total = int(df['total'].sum())
    conteudo = (
        f"## Pedidos por Status\n\n{chart}\n\n"
        f"- Período: {from_date} a {to_date}\n"
        f"- Total de pedidos: {total}\n"
    )
    return ctx.generate_pdf('Relatório de Pedidos', conteudo)
```

### Planilha Excel

```python
def run(from_date, to_date, ctx):
    rows = ctx.sql(f"""
        SELECT po.order_number, po.status::text AS status, po.created_at::date AS data,
               c.name AS cliente
        FROM brazil.purchase_order po
        JOIN brazil.customer c ON c.id = po.customer_id
        WHERE po.created_at::date BETWEEN '{from_date}' AND '{to_date}'
    """)
    df = pd.DataFrame(rows)
    return ctx.generate_excel(df, 'pedidos')
```

### Exemplos de condition_sql para monitores (dialeto PostgreSQL)

```python
# Alerta se inconsistências de hoje > 50
condition_sql = "SELECT COUNT(*) FROM brazil.purchase_order WHERE status::text = 'INCONSISTENCY' AND created_at::date = CURRENT_DATE"
condition_operator = ">"
condition_threshold = 50

# Alerta se não houver pedidos hoje
condition_sql = "SELECT id FROM brazil.purchase_order WHERE created_at::date = CURRENT_DATE LIMIT 1"
condition_operator = "is_empty"
```

## Dica para condition_sql

> Este domínio usa `modo: sql` — condition_sql é uma query SELECT normal. Se este
> domínio for alternado para `modo: graphql`, condition_sql passa a ser uma string
> JSON — ver seção "Formato de condition_sql" do prompt de sistema do scheduler.

Tabelas disponíveis (prefixo `brazil.` obrigatório em SQL): `brazil.purchase_order`,
`brazil.order_item`, `brazil.customer`, `brazil.product`. Enums com cast `::text`;
hoje = `CURRENT_DATE`.
