# skill: task_code_sandbox
# descricao: Regras e template obrigatórios para escrever task_code — namespace do sandbox, variáveis pré-injetadas, proibições e exemplos
# palavras-chave: task_code, sandbox, run, imports, namespace, ctx, pd, plt, np

---

## Workflow obrigatório

**Sempre nesta ordem — nunca salve sem testar:**

1. **Escrever** o código com `def run(from_date, to_date, ctx)`
2. **Testar** com `test_task_code(task_id, code)` — valida sintaxe, executa e mostra o artifact no chat
3. **Salvar** com `save_task_code(task_id, code)` **somente após teste bem-sucedido**

Após `save_task_code`, o daemon executa o código diretamente no horário agendado, sem LLM.

---

## Regra principal

**NUNCA use `import` dentro de `run()` nem fora dela.**
Todas as bibliotecas já estão no namespace — qualquer `import` causa erro imediato.

---

## Como buscar dados

O método de acesso depende de `database.modo` do domínio ativo (**sql** ou **graphql** —
ver aviso injetado no fim desta skill dizendo qual está ativo agora). Em domínios `sql`,
use **exclusivamente `ctx.sql()`**; em domínios `graphql`, `ctx.sql()` NÃO EXISTE — use
`ctx.graphql_table()`/`ctx.graphql_aggregate()`. Nunca misture os dois no mesmo código.

### Modo sql

Query SELECT direta no banco configurado (schema real — tabelas, colunas, regras — na
skill `dominio.md`). Retorno sempre `list[dict]`; converta com `pd.DataFrame(rows)`.

```python
rows = ctx.sql("SELECT categoria, COUNT(*) AS total FROM tabela GROUP BY categoria")
df = pd.DataFrame(rows)
```

### Modo graphql

`ctx.graphql_table(table, where=None, order_by=None, limit=None, offset=None, expand=None)`
e `ctx.graphql_aggregate(table, group_by, metric_field, metric_op, where=None, expand=None,
order_by=None, limit=None)` — **NÃO aceitam `chave`** (esse parâmetro só existe nas tools
`executar_graphql`/`executar_graphql_agregado` usadas pelo analista durante a exploração
interativa dos dados — usar `chave` aqui causa `TypeError`). Retorno sempre `list[dict]`.

```python
rows = ctx.graphql_table(
    table="tabela", where=[{"field": "coluna", "op": "EQ", "value": "x"}],
    expand=[{"viaColumn": "fk_id", "columns": ["name"]}],
)
df = pd.DataFrame(rows)

rows_agg = ctx.graphql_aggregate(
    table="tabela", group_by="categoria", metric_field="valor", metric_op="SUM",
)
```

> `ctx.api()` existe no namespace mas serve apenas para o endpoint `/alerts` (sino de notificações) — não use em tasks de dados.

### Colunas do resultado (modo graphql) — não confunda os dois formatos

- `ctx.graphql_aggregate(group_by="site_id", metric_field=..., expand=["name", "company"])` →
  colunas **`groupKey`** (valor de `site_id`, também disponível como `site_id`), **`name`**,
  **`company`** (as do `expand`, com o nome puro) e **`metricValue`** (vem como TEXTO — use
  `pd.to_numeric(df['metricValue'], errors='coerce')`). Exemplo: `df['name']`, `df['metricValue']`.
- `ctx.graphql_table(..., expand=[{"viaColumn": "site_id", "columns": ["name"]}])` → colunas
  da tabela (incluindo `site_id`, um uuid) e as expandidas como **`site_id__name`** (prefixo
  `<viaColumn>__`). NÃO existe `df['name']` nesse formato.
- Antes de acessar uma coluna que você não tem certeza que existe, confira `df.columns` ou use
  `df.get('coluna')`; nunca assuma o nome — o `KeyError` derruba o relatório inteiro.

### Vários DataFrames (merge / concat)

Dentro do `run()`, crie um DataFrame por consulta (`df_a`, `df_b`, ...) e una com
`pd.merge`/`pd.concat` normalmente — tudo é variável local da função. Cuidado com chave
duplicada no `merge` (multiplica linhas): confira `len()` antes e depois ou use
`validate='m:1'`.

Na exploração (`analisar_dataframe`), DataFrames criados num script (ex: `base = a.merge(b, ...)`)
ficam disponíveis nas chamadas seguintes pelo mesmo nome. Outros tipos de variável (listas,
números, dicts) NÃO persistem entre chamadas.

---

## Variáveis pré-injetadas no namespace

| Variável / Símbolo | O que é |
|---|---|
| `pd` | pandas |
| `np` | numpy |
| `plt` | matplotlib.pyplot |
| `sns` | seaborn |
| `mticker` | matplotlib.ticker |
| `mdates` | matplotlib.dates — use `mdates.DateFormatter('%d/%m')` para formatar eixo de datas |
| `openpyxl` | openpyxl (módulo) |
| `Font`, `PatternFill`, `Alignment`, `Border`, `Side` | openpyxl.styles |
| `get_column_letter` | openpyxl.utils |
| `date`, `datetime`, `timedelta` | do módulo datetime |
| `time`, `math`, `json`, `re` | stdlib leve (pré-injetados) |
| `Counter`, `defaultdict` | collections (pré-injetados) |
| `from_date`, `to_date` | objetos `date` — janela de execução do daemon (veja regra abaixo) |
| `ctx` | TaskContext — métodos abaixo |

### Métodos do ctx

| Chamada | Retorna |
|---|---|
| `ctx.sql('SELECT ...')` | list de dicts via SQL direto no banco configurado (só SELECT) — só existe em domínios `modo: sql` |
| `ctx.graphql_table(table, where=None, ...)` | list de dicts via GraphQL — só existe em domínios `modo: graphql`, sem `chave` |
| `ctx.graphql_aggregate(table, group_by, metric_field, metric_op, where=None, ...)` | list de dicts agregados via GraphQL — só existe em domínios `modo: graphql`, sem `chave` |
| `ctx.today()` | string YYYY-MM-DD com a data atual |
| `ctx.date_range(days=N)` | `(from_date, to_date)` dos últimos N dias |
| `ctx.save_chart(fig)` | token `[chart:uuid]` |
| `ctx.generate_excel(df, 'nome_arquivo')` | token `[excel:uuid]` — aba única chamada "Dados" |
| `ctx.generate_excel({'Aba1': df1, 'Aba2': df2}, 'nome_arquivo')` | token `[excel:uuid]` — múltiplas abas |
| `ctx.generate_pdf('Título', 'conteudo')` | token `[pdf:uuid]` |
| `ctx.notify(msg, value=None, threshold=None)` | dispara notificação no dashboard |
| `ctx.analisar(fatos, foco='', fallback='')` | texto markdown de **análise escrita pelo LLM** a partir de fatos que o código já calculou (ver seção "Relatório com análise"). Se o LLM falhar, devolve `fallback` |

---

## Relatório com análise (use quando o pedido citar análise, insights, padrões, "analyses")

O daemon executa o `task_code` **sem LLM** — mas `ctx.analisar()` chama o LLM na hora de rodar.
Divisão de trabalho: **o código calcula todos os números; o LLM só interpreta**. O LLM não
calcula nada e todo número que ele citar é conferido contra os `fatos` — por isso os fatos
precisam trazer as métricas **já calculadas** (percentuais, razões, diferenças, variações).

Passos: (1) buscar dados, (2) calcular fatos, (3) gráfico, (4) `ctx.analisar(..., fallback=...)`,
(5) montar o markdown e chamar `ctx.generate_pdf`. Escreva títulos e rótulos no **idioma do
pedido do usuário**; o texto de `ctx.analisar` já sai no idioma da tarefa.

```python
def run(from_date, to_date, ctx):
    # 1) dados — use a query/colunas reais do contexto do analista
    df = pd.DataFrame(ctx.graphql_aggregate(...))          # ou ctx.sql(...) em domínios sql
    df = df.sort_values('valor', ascending=False).reset_index(drop=True)
    total = df['valor'].sum()
    df['participacao_pct'] = (df['valor'] / total * 100).round(2)

    # 2) fatos JÁ CALCULADOS (nada de deixar o LLM calcular)
    fatos = {
        'Período': f'{from_date} a {to_date}',
        'Consolidação': df,
        'Métricas': {
            'total': round(total, 2), 'n_grupos': len(df),
            'maior_grupo': df.loc[0, 'grupo'], 'maior_grupo_pct': df.loc[0, 'participacao_pct'],
            'razao_maior_sobre_segundo': round(df.loc[0, 'valor'] / df.loc[1, 'valor'], 2) if len(df) > 1 else None,
            'soma_top3_pct': round(df['participacao_pct'].head(3).sum(), 2),
            'indice_concentracao_hhi': round(((df['valor'] / total) ** 2).sum(), 2),
        },
    }

    # 3) gráfico (regras de pizza abaixo)
    fig, ax = plt.subplots(figsize=(8, 6))
    ...
    token_grafico = ctx.save_chart(fig)

    # 4) análise — SEMPRE com fallback determinístico, para o relatório sair mesmo se o LLM falhar
    fallback = f"## Análise\n- Total: {total:,.2f}. Maior grupo: {df.loc[0, 'grupo']} ({df.loc[0, 'participacao_pct']}%)."
    analise = ctx.analisar(fatos, foco='<o que o usuário pediu para enfatizar>', fallback=fallback)

    # 5) conteúdo do PDF (tabela em markdown, token do gráfico em linha própria)
    tabela = df.to_markdown(index=False)
    conteudo = f"## Consolidação\n{tabela}\n\n{token_grafico}\n\n{analise}"
    return ctx.generate_pdf('Título do relatório', conteudo)
```

Regras de `ctx.analisar`:
- Máximo 3 chamadas por execução; passe **todos** os fatos numa só chamada.
- Não é para monitores nem para tarefas que só geram gráfico/Excel sem pedido de análise.
- `foco` é opcional e curto (ex: 'concentração de custo entre sites').

### Checklist de métricas a calcular antes de chamar `ctx.analisar` (as que fizerem sentido nos dados)

- **Concentração**: participação de cada grupo (%), do maior grupo, soma dos 3 maiores, índice HHI.
- **Ranking e distância**: razão maior/segundo, diferença entre extremos (valor e %), empates ou quase empates.
- **Cauda**: soma dos menores grupos (%) — se irrelevante, dizer.
- **Dispersão**: média, mediana, desvio padrão / coeficiente de variação, outliers (fora de 1,5×IQR).
- **Comparação temporal** (se houver série): variação contra o período anterior (valor e %), mês a mês, tendência (sinal da variação).
- **Qualidade do dado**: nº de registros por grupo, meses/grupos sem dado, filtros aplicados (ex: só APPROVED).

Com poucos pontos (2 a 3 períodos, 2 a 5 grupos) o LLM deve declarar a limitação em vez de concluir tendência.

### Gráfico de pizza (só quando o usuário pedir pizza)

Pizza com fatias muito pequenas fica ilegível (rótulos sobrepostos). Sempre:
- agrupe fatias abaixo de 3% em uma única fatia "Outros" (e mostre o detalhe na tabela);
- não desenhe rótulos em cima das fatias: use `ax.legend(...)` com o valor e o percentual de cada fatia
  (ex: `f"{grupo} — ${valor:,.0f} ({pct:.1f}%)"`), e `autopct` só para fatias ≥ 5%;
- se uma fatia passar de ~80%, mantenha a pizza (foi pedida) mas destaque o valor na legenda e cite a
  concentração na análise.

---

## Regra: date_range vs from_date/to_date

`from_date` e `to_date` são calculados pelo daemon a partir do **`date_range`** da tarefa.
Se `date_range` não estiver definido, usa a janela padrão da frequência:

| `date_range` | Janela injetada |
|---|---|
| `ytd` | 1 jan do ano atual → hoje |
| `mtd` | 1º do mês atual → hoje |
| `today` | hoje → hoje |
| `last_7d` | 7 dias atrás → hoje |
| `last_30d` | 30 dias atrás → hoje |
| `last_90d` | 90 dias atrás → hoje |
| *(não definido)* | janela padrão da frequência (daily=1d, weekly=7d, monthly=30d, every_Xm=1d) |

**Sempre que o usuário especificar um intervalo de análise, defina `date_range` ao criar a tarefa.**
O task_code usa `from_date`/`to_date` normalmente — o daemon garante os valores corretos.
Se o domínio configurado não tiver granularidade por data de calendário (ver skill `dominio.md`),
`from_date`/`to_date` servem para rotular o período no título/conteúdo do relatório, e os
filtros reais de dados usam as colunas de período descritas na skill.

| O usuário pediu | `date_range` correto |
|---|---|
| "do ano atual" / "do ano" | `ytd` |
| "do mês" / "do mês atual" | `mtd` |
| "últimos 30 dias" | `last_30d` |
| "de hoje" | `today` |
| "relatório diário" | *(não definir — usa padrão da frequência)* |

> **NUNCA** calcule o intervalo dentro do task_code com `ctx.today()` para substituir `date_range`. Defina `date_range` na tarefa e use `from_date`/`to_date` no código.
>
> ⚠️ **Ao testar**: `test_task_code(task_id, code)` sem `from_date`/`to_date` usa por padrão
> "últimos 7 dias" — isso NÃO reflete o `date_range` real da tarefa. Para tarefas com
> `date_range='today'`, `'mtd'` ou `'ytd'`, passe explicitamente `from_date`/`to_date`
> equivalentes ao `date_range` para que o resultado do teste corresponda ao que será gerado
> em produção.

---

## Monitores com gatilho condicional (preferido)

> Para tarefas que só devem executar (e notificar) quando uma condição for atingida,
> use os parâmetros `condition_sql`, `condition_operator` e `condition_threshold` ao criar a tarefa.
> **Não escreva `ctx.notify()` no código** — a notificação é automática quando a condição é atingida.
> `condition_sql` usa o formato do modo de acesso do domínio ativo — SQL direto (`modo: sql`)
> ou spec JSON via GraphQL (`modo: graphql`), nunca `ctx.graphql_table`/`ctx.graphql_aggregate`
> diretamente (informado junto com estas regras).

A notificação automática inclui o detalhe da condição: ex. "Monitor de X: 75 > 50".

### Monitores de condição composta (AND / OR / várias métricas)

`condition_sql` só compara **um** valor com **um** threshold. Quando o pedido combina várias
condições (ex: "JAG > 50 **e** BPL < 10 **e** MAN > 40", "A > 5 **ou** B < 2"), a tarefa é criada
**sem** `condition_sql` (com `condition_description`) e o próprio `task_code` avalia a condição
inteira, sinalizando o resultado com `ctx.alert(triggered, message)`:

```python
def run(from_date, to_date, ctx):
    rows = ctx.graphql_table(table="...", where=[...], expand=[...], limit=500)
    df = pd.DataFrame(rows)
    por_site = df.groupby("site_id__name")["custo"].mean().to_dict()

    jag, bpl, man = por_site.get("JAG"), por_site.get("BPL"), por_site.get("MAN")
    if None in (jag, bpl, man):          # sem dado → não dispara (nunca assuma 0)
        ctx.alert(False, "")
        return "Sem dados de algum site monitorado"

    triggered = jag > 50 and bpl < 10 and man > 40   # E→and, OU→or, com parênteses
    ctx.alert(triggered, f"JAG {jag:.2f} > 50, BPL {bpl:.2f} < 10, MAN {man:.2f} > 40")
    return f"JAG {jag:.2f} | BPL {bpl:.2f} | MAN {man:.2f} — {'ATINGIDA' if triggered else 'não atingida'}"
```

- Chame `ctx.alert` **uma vez** por execução, com o booleano da condição **completa** — nunca de
  apenas uma das partes.
- Use `ctx.alert(...)` em vez de `ctx.notify(...)`: a notificação só sai na transição
  "não atingida → atingida" e reseta quando a condição deixa de valer (igual ao `condition_sql`).
- Se faltar dado de qualquer parte, `triggered=False` — um `.get(chave, 0)` pode satisfazer um
  `<` e gerar alerta falso.
- `ctx.alert` é recusado em tarefas com `condition_sql`, e `ctx.notify` é recusado nas de
  condição composta.

---

## Notificações automáticas

Quando a tarefa tem `notify=True`, o runner dispara automaticamente uma notificação com o nome da tarefa ao final de cada execução bem-sucedida — **sem precisar de `ctx.notify()` no código**.

**PROIBIDO usar `ctx.notify()` no task_code para implementar condições de threshold.**
Se o objetivo é "alertar quando X > N", use `condition_sql` + `condition_operator` + `condition_threshold` na tarefa.
Para condições compostas (AND/OR/várias métricas), use `ctx.alert(triggered, message)` — veja a seção acima.
O sistema bloqueia o `save_task_code` se detectar `ctx.notify()` em tarefas que já têm `condition_sql`.

## Monitores (every\_Xm / every\_Xh)

Monitores DEVEM sempre retornar uma string com o valor atual, mesmo quando o threshold não for atingido.

---

## Erros comuns — NÃO faça isso

```python
# ERRADO — ctx.notify() sem o usuário ter pedido alertas
def run(from_date, to_date, ctx):
    ...
    ctx.notify('Gráfico gerado')   # proibido em tarefas de relatório/gráfico

# CERTO — ctx.notify() só quando notify=True foi definido na criação da tarefa
# e o usuário pediu monitoramento/alertas explicitamente
```

```python
# ERRADO — import dentro do run() causa ImportError
def run(from_date, to_date, ctx):
    import pandas as pd   # proibido — use pd diretamente
```

```python
# ERRADO — nunca retorne a figura diretamente
def run(from_date, to_date, ctx):
    fig, ax = plt.subplots()
    return fig   # use ctx.save_chart(fig)
```

```python
# ERRADO — ha= não existe em tick_params
ax.tick_params(axis='x', rotation=45, ha='right')

# CERTO
plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
```

```python
# ERRADO — usar sintaxe de outro dialeto SQL (verifique o dialeto informado)
# e inventar nomes de tabela/coluna que não estão na skill dominio.md

# CERTO — usar apenas tabelas/colunas reais da skill, no dialeto do banco configurado
```

```python
# ERRADO — salvar sem testar
save_task_code(task_id, code)

# CERTO — testar primeiro, salvar só se passar
test_task_code(task_id, code)
save_task_code(task_id, code)   # só após teste bem-sucedido
```
