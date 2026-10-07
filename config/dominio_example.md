# skill: dominio
# descricao: <UMA FRASE — o que este domínio analisa (aparece no catálogo de skills do agente)>
# palavras-chave: <termo1, termo2, termo3, ...>  (usadas pelo orquestrador para reconhecer perguntas deste domínio)

---

<!--
  MODELO / TEMPLATE de arquivo de domínio para o sistema plug-and-play.

  Como funciona:
  - Este arquivo descreve UM domínio (um banco de dados ou API) para o agente. `config/settings.yml`
    escolhe qual domínio está ativo (chave `app.dominio`) e COMO os dados são acessados
    (`database.modo`): `sql` (consulta direta, com `database.url` SQLAlchemy) ou `graphql`
    (endpoint GraphQL, com o bloco `graphql`: url + token ou api_key). Cada domínio usa só um
    dos dois, e a configuração e este arquivo têm que corresponder ao MESMO banco.
  - Onde salvar: `config/<slug>/<idioma>/dominio.md`, em que <slug> é o valor de `app.dominio`
    sem o prefixo "dominio_" e sem ".md" (ex: `app.dominio: "dominio_vendas.md"` →
    `config/vendas/pt-BR/dominio.md`). Um arquivo por idioma (pt-BR, en, zh); um idioma sem
    arquivo cai no `app.idioma_default`. Os painéis padrão do dashboard (opcionais) ficam ao
    lado, em `config/<slug>/<idioma>/dashboard_default.py` (uma lista `WIDGETS`).
  - Pode haver vários domínios (pastas) ao mesmo tempo; só o apontado por `app.dominio` fica
    ativo. Copie este arquivo, preencha as seções e aponte o settings.yml para o domínio e para o
    banco novos. Não precisa mexer em código — a troca exige apenas restart do app.
  - Edições neste arquivo são relidas sozinhas, exceto as seções injetadas nos prompts na
    inicialização — "Regras do agente", "Regras do orquestrador" e "Exemplos task_code" —, que só
    valem depois de um restart.
  - As seções são delimitadas por headings de nível "## " (exatamente esse nível).
    Headings internos DENTRO de uma seção devem ser "###" — nunca "##", que o parser
    usa como delimitador de seção. Seções podem vir em qualquer ordem; as que faltarem
    simplesmente ficam vazias (o sistema degrada, não quebra).
  - Remova este comentário e os comentários de ajuda de cada seção depois de preencher —
    eles são só instruções, não fazem parte do que é servido ao agente.
-->

## Regras do agente

<!--
  Injetada verbatim no prompt do sub-agente "analista" (quem consulta os dados — por SQL ou por
  GraphQL, conforme o `database.modo` — e gera tabelas e gráficos).
  Coloque aqui regras de interpretação que evitam erros comuns de leitura do dado:
  qual coluna de data usar para "hoje"/"este mês", granularidade real dos dados
  (ex: "só existem lançamentos no dia 1 de cada mês"), armadilhas de schema
  (colunas enum que precisam de cast, campos desnormalizados a evitar, etc.).
  Em domínios `graphql`, diga também como o analista deve usar as tools: quando preferir a
  agregação no servidor (executar_graphql_agregado) à busca de linhas (executar_graphql), e como
  resolver o ID de uma entidade citada pelo nome antes de filtrar por ela.
-->

### <nome da sub-regra, ex: "Coluna de data — use sempre a coluna certa">

<explicação da regra em 2-5 linhas>

## Regras do orquestrador

<!--
  Injetada verbatim no prompt do orquestrador (quem decide se a pergunta é deste domínio,
  escolhe o período em linguagem natural e roteia para o analista). Diga em 1-2 frases o que
  este sistema É (para o orquestrador negar assuntos de outros domínios) e confirme que, sem
  período explícito na pergunta, ele deve assumir um padrão e seguir direto — nunca perguntar
  de volta ao usuário.

  "todo o período" (abaixo) é o padrão seguro para a maioria dos domínios — calcular_periodo
  tem um fallback genérico para essa frase (2000-01-01 até hoje) que funciona em QUALQUER
  domínio, com ou sem '## Períodos'/database.url preenchidos. Se este domínio tem uma noção
  própria de período "fechado" mais específica (ex: ano fiscal com trimestres, onde o quarter
  em andamento não deve entrar em totais), troque por algo como periodo='ultimo
  quarter' — só funciona bem se o domínio também documentar seu mes_inicio_fiscal em
  settings.yml (ver comentário lá). Teste com uma pergunta SEM período explícito antes de
  considerar esta seção pronta — é fácil essa regra nunca disparar durante o desenvolvimento
  e o bug só aparecer depois, em produção.
-->

Este é um sistema de <o que é este sistema, em 1 frase> — não é <domínios que NÃO são este,
se ajudar a desambiguar>. Se o usuário NÃO mencionar período nenhum, chame
consultar_analista direto com periodo='todo o período' como padrão —
NUNCA pergunte o período ao usuário.

## Períodos

<!--
  Seção "machine-parsable" (não é prosa livre) — resolve frases de período em (data_de,
  data_ate) sem depender do calendário real, útil quando os dados são um snapshot cuja
  "data atual" não é hoje. Cada entrada é um heading "### " seguido de um bloco ```sql.

  Duas formas de heading:
  - Frases literais separadas por "|": casam por igualdade após normalizar
    (trim + lower + espaços colapsados). Ex: "### este mes | mês corrente | mês atual"
  - Regex: "### re:<padrão>" — casado com re.search(padrão, frase, re.IGNORECASE).
    Grupos de captura (parênteses) viram parâmetros :g1, :g2... disponíveis na SQL.

  A SQL de cada entrada deve retornar EXATAMENTE 1 linha com 2 colunas: (data_de, data_ate)
  em formato ISO (ou algo cujos 10 primeiros caracteres sejam YYYY-MM-DD). Sem linha, com
  NULL, ou com erro → o sistema ignora essa entrada e cai no fallback de calendário do
  calcular_periodo (hoje, "esta semana", "mês passado" etc. calculados normalmente).

  ORDEM IMPORTA: a primeira entrada que casar vence — coloque padrões mais específicos
  ANTES de padrões genéricos que os englobariam (ex: "próximo trimestre" antes de
  "trimestre" sozinho).

  Se seus dados são um período corrido normal (sem snapshot, "hoje" == data real), você
  pode deixar esta seção vazia ou com só a entrada "todo o período" abaixo.

  Domínios `graphql` SEM `database.url`: essas SQLs não têm onde rodar — toda entrada é
  ignorada em silêncio e vale só o fallback de calendário, que já cobre hoje, semana, mês,
  "todo o período" e, via `app.mes_inicio_fiscal` do settings.yml, quarters e ano fiscal.
  Nesse caso omita esta seção (ou deixe-a só como referência, marcada como tal).
-->

### re:todo o per[ií]odo|hist[oó]rico completo|desde o in[ií]cio

```sql
SELECT MIN(<coluna_de_data>), MAX(<coluna_de_data>) FROM <tabela_principal>
```

## Modelo de dados

<!--
  Schema do domínio: tabelas, colunas, joins, exemplos. É carregada no prompt do analista a
  cada análise — a única fonte dos nomes reais de tabelas e colunas (sem ela o analista
  inventa nomes que não existem) — e também servida por rag_dados. Seja explícito sobre:
  PK/FK, colunas enum e como tratá-las no dialeto do banco (cast, etc.), colunas
  desnormalizadas a evitar, nullable relevante.
  Em domínios `graphql`, descreva tabelas e colunas do mesmo jeito e documente o grafo de FKs:
  é por ele que o analista escolhe `group_by` e `expand` (join de UM salto). Os exemplos de
  query viram exemplos de chamada das tools.
-->

### `<schema.tabela>` — <o que cada linha representa>

| Coluna | Significado |
|--------|-------------|
| id | PK |
| <coluna> | <significado, tipo/enum se aplicável, FK se aplicável> |

### Grafo de joins

```text
<tabela_a> (1)──(N) <tabela_b>   via <tabela_b>.<coluna_fk>
```

### Exemplos de queries

```sql
-- <o que a query responde> (domínios modo: sql)
SELECT ...
FROM <tabela>
WHERE ...
GROUP BY ...
ORDER BY ...;
```

<!--
  Domínios modo: graphql — troque o bloco acima por exemplos de chamada, p.ex.:
  executar_graphql_agregado(table="<tabela>", group_by="<coluna_fk>", metric_field="<coluna>",
      metric_op="SUM", where=[{"field": "<coluna>", "op": "EQ", "value": "<valor>"}],
      expand=["<coluna_da_tabela_relacionada>"], order_by="DESC", limit=10)
-->

## Domínio de negócio

<!--
  Glossário e regras de negócio (não-SQL): o que o sistema faz, fluxo/estados de vida de
  uma entidade, definição de métricas/siglas do domínio, KPIs usuais. Servida ao agente
  por rag_dominio e incluída na skill do analista.
-->

### O que é este sistema

<2-4 frases de contexto de negócio>

### KPIs usuais

- <métrica> — <como calcular, ex: "SUM(valor)">

## Exemplos task_code

<!--
  Anexada às regras do sandbox de tarefas agendadas — o agente a usa para gerar o código de
  relatórios, monitores e painéis do dashboard: uma função run(from_date, to_date, ctx).
  Dê pelo menos um exemplo completo contra as tabelas reais deste domínio, para o agente
  copiar o padrão. MANTENHA SÓ o exemplo do modo deste domínio (`database.modo`):
    - modo sql:     busque os dados com ctx.sql("SELECT ...") → list[dict]
    - modo graphql: ctx.sql() NÃO existe; use ctx.graphql_table(...) / ctx.graphql_aggregate(...),
                    com os mesmos argumentos das tools do analista, mas SEM o parâmetro `chave`
  Mostre também como o período (from_date/to_date, objetos date) é aplicado nas colunas de
  período REAIS do domínio: eles não filtram nada sozinhos — a consulta precisa usá-los.
  No fim, devolva o artefato (ctx.generate_pdf / ctx.generate_excel / ctx.save_chart).
-->

### Modo sql

```python
def run(from_date, to_date, ctx):
    rows = ctx.sql(f"""
        SELECT ...
        FROM <tabela>
        WHERE <coluna_data> BETWEEN '{from_date}' AND '{to_date}'
    """)
    df = pd.DataFrame(rows)
    if df.empty:
        return ctx.generate_pdf('<Título>', '## Sem dados no período.')
    # ... gráfico / cálculo ...
    return ctx.generate_pdf('<Título>', conteudo)
```

### Modo graphql

```python
def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "<tabela>", group_by="<coluna_fk>",
        metric_field="<coluna_numerica>", metric_op="SUM",
        where=[{"field": "<coluna_data>", "op": "GTE", "value": from_date},
               {"field": "<coluna_data>", "op": "LTE", "value": to_date}],
        expand=["<coluna_da_tabela_relacionada>"], order_by="DESC", limit=20,
    )
    df = pd.DataFrame(rows)  # colunas: groupKey, metricValue (vem como TEXTO) e as do expand
    if df.empty:
        return ctx.generate_pdf('<Título>', '## Sem dados no período.')
    df['metricValue'] = pd.to_numeric(df['metricValue'])
    # ... gráfico / cálculo ...
    return ctx.generate_pdf('<Título>', conteudo)
```

## Dica para condition_sql

<!--
  Injetada ao criar monitores (alertas agendados que avaliam uma condição e notificam quando
  ela é atingida). O formato de `condition_sql` depende de `database.modo`:
    - modo sql:     uma query SELECT que devolve um escalar (ou linhas, para
                    is_empty/is_not_empty). Liste as tabelas/colunas mais úteis para alertas e
                    como escrever "hoje" no dialeto do banco configurado.
    - modo graphql: NÃO é SQL — é uma string JSON (type "aggregate" ou "table"). Dê aqui um
                    exemplo pronto para o caso mais comum. O valor comparado ao threshold é
                    sempre `metricValue` (aggregate) ou o `value_field` indicado (table), e
                    filtros por entidade devem usar o ID real, não o nome.
  Mantenha só o que vale para o modo deste domínio.
-->

Tabelas disponíveis: `<tabela1>`, `<tabela2>`. Hoje = <expressão do dialeto, ex:
`CURRENT_DATE` no PostgreSQL, `date('now')` no SQLite>.

<!--
  Modo graphql — exemplo de condition_sql (JSON):
  {"type": "aggregate", "table": "<tabela>", "group_by": "<coluna_fk>",
   "metric_field": "<coluna_numerica>", "metric_op": "SUM",
   "where": [{"field": "<coluna_fk>", "op": "EQ", "value": "<id real>"}]}
-->
