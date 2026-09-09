# skill: dominio
# descricao: <UMA FRASE — o que este domínio analisa (aparece no catálogo de skills do agente)>
# palavras-chave: <termo1, termo2, termo3, ...>  (usadas pelo orquestrador para reconhecer perguntas deste domínio)

---

<!--
  MODELO / TEMPLATE de arquivo de domínio para o sistema plug-and-play.

  Como funciona:
  - Este arquivo descreve UM banco de dados para o agente. `config/settings.yml` escolhe
    qual arquivo `dominio_*.md` está ativo (chave `app.dominio`) e qual `database.url`
    (SQLAlchemy) ele usa — os dois têm que corresponder ao MESMO banco.
  - Pode haver vários `dominio_*.md` na pasta ao mesmo tempo (ex: dominio_posh.md,
    dominio_alocacao.md); só o apontado por `app.dominio` fica ativo.
  - Copie este arquivo para `dominio_<seunome>.md`, preencha as seções, aponte o
    settings.yml para ele e para a URL do banco novo. Não precisa mexer em código —
    troca exige apenas restart do app.
  - As seções são delimitadas por headings de nível "## " (exatamente esse nível).
    Headings internos DENTRO de uma seção devem ser "###" — nunca "##", que o parser
    usa como delimitador de seção. Seções podem vir em qualquer ordem; as que faltarem
    simplesmente ficam vazias (o sistema degrada, não quebra).
  - Remova este comentário e os comentários de ajuda de cada seção depois de preencher —
    eles são só instruções, não fazem parte do que é servido ao agente.
-->

## Regras do agente

<!--
  Injetada verbatim no prompt do sub-agente "analista" (quem gera e executa SQL).
  Coloque aqui regras de interpretação que evitam erros comuns de leitura do dado:
  qual coluna de data usar para "hoje"/"este mês", granularidade real dos dados
  (ex: "só existem lançamentos no dia 1 de cada mês"), armadilhas de schema
  (colunas enum que precisam de cast, campos desnormalizados a evitar, etc.).
-->

### <nome da sub-regra, ex: "Coluna de data — use sempre a coluna certa">

<explicação da regra em 2-5 linhas>

## Regras do orquestrador

<!--
  Injetada verbatim no prompt do orquestrador (quem decide se pergunta é deste domínio,
  calcula período e roteia para o analista). Diga em 1-2 frases o que este sistema É
  (para o orquestrador negar assuntos de outros domínios) e confirme que, sem período
  explícito na pergunta, ele deve assumir um padrão e seguir direto — nunca perguntar
  de volta ao usuário.

  "todo o período" (abaixo) é o padrão seguro para a maioria dos domínios — calcular_periodo
  tem um fallback genérico para essa frase (2000-01-01 até hoje) que funciona em QUALQUER
  domínio, com ou sem '## Períodos'/database.url preenchidos. Se este domínio tem uma noção
  própria de período "fechado" mais específica (ex: ano fiscal com trimestres, onde o quarter
  em andamento não deve entrar em totais), troque por algo como calcular_periodo('ultimo
  quarter') — só funciona bem se o domínio também documentar seu mes_inicio_fiscal em
  settings.yml (ver comentário lá). Teste com uma pergunta SEM período explícito antes de
  considerar esta seção pronta — é fácil essa regra nunca disparar durante o desenvolvimento
  e o bug só aparecer depois, em produção.
-->

Este é um sistema de <o que é este sistema, em 1 frase> — não é <domínios que NÃO são este,
se ajudar a desambiguar>. Se o usuário NÃO mencionar período nenhum, use
calcular_periodo('todo o período') como padrão e siga direto para consultar_analista —
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
-->

### re:todo o per[ií]odo|hist[oó]rico completo|desde o in[ií]cio

```sql
SELECT MIN(<coluna_de_data>), MAX(<coluna_de_data>) FROM <tabela_principal>
```

## Modelo de dados

<!--
  Schema do banco: tabelas, colunas, joins, exemplos de query. Servida ao agente por
  read_skill('dominio.md') (nome lógico fixo — sempre resolve para o arquivo ativo) e
  usada para gerar SQL correto. Seja explícito sobre: PK/FK, colunas enum e como tratá-las
  no dialeto do banco (cast, etc.), colunas desnormalizadas a evitar, nullable relevante.
-->

### `<schema.tabela>` — <o que cada linha representa>

| Coluna | Significado |
|--------|-------------|
| id | PK |
| <coluna> | <significado, tipo/enum se aplicável, FK se aplicável> |

### Grafo de joins

```
<tabela_a> (1)──(N) <tabela_b>   via <tabela_b>.<coluna_fk>
```

### Exemplos de queries

```sql
-- <o que a query responde>
SELECT ...
FROM <tabela>
WHERE ...
GROUP BY ...
ORDER BY ...;
```

## Domínio de negócio

<!--
  Glossário e regras de negócio (não-SQL): o que o sistema faz, fluxo/estados de vida de
  uma entidade, definição de métricas/siglas do domínio, KPIs usuais. Servida ao agente
  por rag_dominio / read_skill('dominio.md').
-->

### O que é este sistema

<2-4 frases de contexto de negócio>

### KPIs usuais

- <métrica> — <como calcular, ex: "SUM(valor)">

## Exemplos task_code

<!--
  Anexada às regras do sandbox de tarefas agendadas (geração de PDF/Excel via chat).
  Dê pelo menos um exemplo completo de função run(from_date, to_date, ctx) usando
  ctx.sql(...) contra as tabelas reais deste domínio, para o agente copiar o padrão.
-->

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

## Dica para condition_sql

<!--
  Injetada ao criar monitores (alertas agendados que rodam uma condition_sql e comparam
  contra um threshold). Liste as tabelas/colunas mais úteis para alertas e como escrever
  "hoje" no dialeto do banco configurado.
-->

Tabelas disponíveis: `<tabela1>`, `<tabela2>`. Hoje = <expressão do dialeto, ex:
`CURRENT_DATE` no PostgreSQL, `date('now')` no SQLite>.
