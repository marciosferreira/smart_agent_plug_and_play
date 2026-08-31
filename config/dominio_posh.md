# skill: dominio
# descricao: Análise de pedidos de compra (Brazil Purchase Orders) — pedidos, itens, clientes, produtos, erros/inconsistências e alertas. Executa SQL no PostgreSQL (schema brazil).
# palavras-chave: pedidos, pedido, itens, clientes, produtos, erros, alertas, inconsistências, status, aprovados, rejeitados, quantidade, valor, ranking, canal, regional

---

## Regras do agente

### Colunas de data — use sempre a coluna certa

A dimensão temporal principal dos pedidos é `created_at` (data de entrada no sistema).
Para "pedidos deste mês", "pedidos de hoje", "pedidos por período" — use sempre `created_at`.
`issue_date` é a data original informada pelo cliente; `delivery_month` é o mês de entrega
desejado. NÃO existe coluna genérica `date` neste sistema.

### Dialeto e schema

- Todas as tabelas vivem no schema `brazil` — prefixe SEMPRE: `brazil.purchase_order`,
  `brazil.order_item`, etc. Nome sem prefixo falha.
- Colunas enum DEVEM ser convertidas com `::text` (ex: `status::text`, `channel::text`,
  `state::text`) — sem o cast a query pode falhar ou retornar tipos inesperados.
- Datas: `CURRENT_DATE` para hoje, `created_at >= CURRENT_DATE - INTERVAL 'N days'` para
  períodos, `TO_CHAR(created_at, 'YYYY-MM')` para agrupar por mês.

### Motivo do erro de um item — nunca use o status como descrição

`order_item.status = 'INCONSISTENCY'` indica que há problema, mas o MOTIVO está em
`brazil.error_type.name` via `order_item.error_type_id`. Para alertas
(`brazil.alert_resolve`), o tipo de erro depende do status do pedido:
- `INCONSISTENCY`/`REJECTED` → `brazil.alert_resolve_error_type` → `brazil.error_type`
- `PROCESSED` → `brazil.order_snapshot_item_issue.alert_name` (via `brazil.order_snapshot`);
  um LEFT JOIN em alert_resolve_error_type retorna NULL para PROCESSED — não significa
  "sem erro".

## Regras do orquestrador

Este é um sistema de gestão de pedidos de compra (Brazil Purchase Orders) — 'pedido' significa
SEMPRE purchase_order. NÃO é um sistema de chão-de-fábrica nem de alocação de supply.
Se o usuário NÃO mencionar período nenhum, use calcular_periodo('todo o período') como padrão
e siga direto para consultar_analista — NUNCA pergunte o período ao usuário.

## Períodos

### re:todo o per[ií]odo|hist[oó]rico completo|desde o in[ií]cio

```sql
SELECT MIN(created_at::date), MAX(created_at::date) FROM brazil.purchase_order
```

## Modelo de dados

Todas as tabelas no schema `brazil` (prefixo obrigatório). Apenas SELECT; sem SELECT *.

### `brazil.purchase_order` — pedido de compra (entidade central)

| Coluna | Significado |
|--------|-------------|
| id | PK interna |
| order_number | Número do pedido conforme enviado pelo cliente |
| customer_id | FK → brazil.customer.id |
| customer_name | Nome desnormalizado — prefira JOIN com customer.name |
| file_import_id | FK → brazil.file_import.id (arquivo que originou o pedido) |
| issue_date | Data em que o cliente emitiu o pedido |
| created_at | **Data de entrada no sistema — dimensão temporal principal** |
| delivery_month | Mês desejado de entrega |
| status | enum: `APPROVED` · `INCONSISTENCY` · `PENDING` · `REJECTED` — cast `::text` |
| customer_usage_order | enum `CU1`/`CU2` — cast `::text` |
| justification_reject | Motivo da rejeição (texto livre) |

### `brazil.order_item` — item de um pedido (1..N por pedido)

| Coluna | Significado |
|--------|-------------|
| id | PK interna |
| purchase_order_id | FK → brazil.purchase_order.id |
| product_id | FK → brazil.product.id (NULL = produto não identificado) |
| accessory_id | FK → brazil.accessory.id (NULL = acessório não identificado) |
| item_number | Posição do item no pedido |
| status | enum (mesmos valores do pedido, mas independente) — cast `::text` |
| quantity | Unidades pedidas |
| value_price_total | Valor monetário total do item |
| product_group | Família: `SMARTPHONE` · `TABLET` · `ACESSÓRIO` etc. |
| local_market_name / local_color | Nome comercial local / cor conforme pedido |
| delivery_week | Semana de entrega desejada |
| error_type_id | FK → brazil.error_type.id — preenchido quando o item tem erro |
| created_at | Data de criação do item |

### `brazil.customer` — cliente

| Coluna | Significado |
|--------|-------------|
| id, name, customer_short, cnpj | Identificação |
| channel | enum canal de venda — cast `::text` |
| state | enum UF — cast `::text` |
| regional | Regional comercial |
| city_id | FK → brazil.city.id |
| sold_customer_number / ship_customer_number | Códigos ERP |

### `brazil.product` / `brazil.accessory` — catálogos

product: part_number, market_name, local_market_name, product_group, local_color,
status (enum `ACTIVE`/`INACTIVE`), ram, rom, ean, origin.
accessory: part_number, market_name, local_market_name, product_group, status, ean.

### `brazil.error_type` — dicionário de erros

`id`, `name` (enum — cast `::text`). Valores: `CNPJ_CUSTOMER_NOT_IDENTIFIED`,
`CNPJ_DELIVERY_NOT_IDENTIFIED`, `PART_NUMBER_NOT_IDENTIFIED`, `INCORRECT_QUANTITY`,
`NOT_FOUND_DELIVERY_MONTH`, `ACCESSORY_MISSING_FOR_BUNDLE`, `ACCESSORY_NOT_MAPPED_TO_BUNDLE`.

### `brazil.alert_resolve` — resolução de inconsistências

`id`, `purchase_order_id` (FK — o status REAL do alerta é `purchase_order.status`),
`customer_id` (pode ser NULL), `file_import_id`, `created_at` (dimensão temporal),
`resolved_at`, `notify_reject_date`.
⚠️ NÃO use `resolved_at IS NOT NULL` como filtro de "resolvido" — use `purchase_order.status::text`.

### `brazil.alert_resolve_error_type` — N:N alerta↔erro (só INCONSISTENCY/REJECTED)

`alert_resolve_id`, `error_type_id`. ⚠️ Para pedidos PROCESSED os erros migram para
`brazil.order_snapshot_item_issue.alert_name` (via `brazil.order_snapshot.purchase_order_id`).

### `brazil.order_snapshot` / `brazil.order_snapshot_item_issue`

Snapshot do pedido no processamento; item_issue tem `alert_name` (nome do erro pronto,
sem JOIN com error_type), `part_number`, `resolved_at`.

### `brazil.file_import` — arquivos importados

`id`, `file_name`, `file_extension`, `date_registered`.

### Grafo de joins

```
brazil.purchase_order (1)──(N) brazil.order_item        via order_item.purchase_order_id
brazil.purchase_order (N)──(1) brazil.customer          via purchase_order.customer_id
brazil.order_item     (N)──(1) brazil.product           via order_item.product_id [nullable]
brazil.order_item     (N)──(1) brazil.error_type        via order_item.error_type_id [nullable]
brazil.alert_resolve  (N)──(1) brazil.purchase_order    via alert_resolve.purchase_order_id
brazil.alert_resolve  (N)──(N) brazil.error_type        via brazil.alert_resolve_error_type
brazil.order_snapshot (N)──(1) brazil.purchase_order    via order_snapshot.purchase_order_id
brazil.customer       (N)──(1) brazil.city              via customer.city_id [nullable]
```

### Exemplos de queries

```sql
-- Pedidos por status no período
SELECT status::text AS status, COUNT(*) AS total
FROM brazil.purchase_order
WHERE created_at::date BETWEEN '2026-01-01' AND '2026-06-30'
GROUP BY status ORDER BY total DESC;
```

```sql
-- Top clientes por valor
SELECT c.name, SUM(oi.value_price_total) AS valor_total, COUNT(DISTINCT po.id) AS pedidos
FROM brazil.purchase_order po
JOIN brazil.customer c ON c.id = po.customer_id
JOIN brazil.order_item oi ON oi.purchase_order_id = po.id
GROUP BY c.name ORDER BY valor_total DESC LIMIT 10;
```

```sql
-- Erros mais frequentes em itens
SELECT et.name::text AS erro, COUNT(*) AS ocorrencias
FROM brazil.order_item oi
JOIN brazil.error_type et ON et.id = oi.error_type_id
GROUP BY et.name ORDER BY ocorrencias DESC;
```

## Domínio de negócio

### O que é este sistema

Plataforma de gestão de pedidos de compra do mercado Brazil: clientes enviam arquivos de
pedidos, o sistema importa, valida item a item e aprova/rejeita. Itens com problemas viram
inconsistências com tipo de erro identificado; alertas de resolução acompanham o tratamento.

### Fluxo de um pedido

importação (file_import) → validação → `PENDING` → `APPROVED` ou `INCONSISTENCY`
(aguarda resolução via alert_resolve) → `PROCESSED` ou `REJECTED`.

### Status

- **APPROVED** — pedido aprovado
- **PENDING** — em análise
- **INCONSISTENCY** — tem itens com erro, aguarda resolução
- **REJECTED** — rejeitado (motivo em justification_reject)

### KPIs usuais

- Taxa de aprovação: pedidos APPROVED / total
- Volume: COUNT de pedidos ou SUM(quantity) de itens
- Valor: SUM(value_price_total)
- Clientes únicos: COUNT(DISTINCT customer_id)
- Erros por tipo: JOIN com error_type

## Exemplos task_code

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

Tabelas disponíveis (prefixo `brazil.` obrigatório): `brazil.purchase_order`,
`brazil.order_item`, `brazil.customer`, `brazil.product`. Enums com cast `::text`;
hoje = `CURRENT_DATE`.
