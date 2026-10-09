# Arquitetura Agêntica — Smart Agent Plug and Play

---

## Visão geral

Nesta arquitetura, o agente atua como uma camada de inteligência sobre os dados operacionais da empresa. A partir de uma instrução em linguagem natural — digitada ou dita pelo usuário, em português, inglês ou chinês — o sistema consulta o banco do domínio ativo em tempo real (SQL direto ou GraphQL), processa os dados em um sandbox Python e entrega a análise na forma de texto, gráficos, planilhas Excel ou relatórios PDF. O mesmo agente fixa painéis no dashboard e agenda relatórios e monitores que rodam sozinhos. E ele aprende com as conversas: guarda lições sobre como consultar o banco, fichas das análises já feitas (o que foi pedido, o que funcionou, o que falhou e como o usuário avaliou), e as convenções, preferências e o perfil de cada usuário, e as usa nas conversas seguintes.

O objetivo é eliminar a dependência de intermediários para consultas analíticas rotineiras: o operador ou gestor formula a pergunta diretamente ao sistema e recebe uma resposta fundamentada em dados reais, sem dado inventado e sem espera por um analista disponível.

Este documento descreve como cada componente foi construído e por que as decisões de projeto foram tomadas dessa forma.

---

## A Arquitetura em uma frase

> Um **orquestrador** que interpreta pedidos em linguagem natural — digitados ou ditos, em vários idiomas — delega para um **sub-agente especialista** que consulta o **banco do domínio ativo em tempo real (SQL ou GraphQL)**, analisa os dados em um sandbox Python e entrega gráficos, tabelas, planilhas Excel ou PDFs — tudo transmitido ao vivo via **streaming SSE**. Em paralelo, um **daemon de agendamento** executa o código Python que o próprio agente gerou e validou para cada tarefa, produzindo **relatórios periódicos** (com análise narrativa opcional) e **monitores de threshold** com alertas no dashboard. Depois de cada turno, uma **memória de longo prazo** aprende lições sobre o banco, guarda fichas das análises já feitas (buscadas por similaridade para perguntas parecidas) e as convenções, preferências e o perfil de cada um.

---

## Mapa do código

| Caminho | Responsabilidade |
| --- | --- |
| `main.py` | FastAPI: rotas HTTP, SSE do chat, startup (migrações, Vertex AI, daemon) e serviço do build do Angular |
| `agent_multi.py` | O agente inteiro: tools, prompts, grafos LangGraph (orquestrador, analista, scheduler, pipeline de agendamento), streaming, cancelamento e criação dos LLMs |
| `db_config.py` | Porta única para o pack `config/` (settings, idioma, seções do `dominio.md`, períodos, painéis padrão) e para o banco de domínio em modo SQL |
| `graphql_config.py` | Cliente GraphQL do modo `graphql`: `reportTable`, `reportAggregate`, `reportSchema` |
| `db.py` | Banco local do app (`mfg.db`): criação e migração das tabelas de tarefas, painéis, ordem, auditoria, sessão×domínio, avaliações das respostas e resumo da abordagem |
| `memoria.py` | Memória do agente: uma extração em background (chamada estruturada ao LLM, agendada pelo `ReflectionExecutor` do LangMem) que mantém lições do banco, convenções, preferências, perfil do usuário e as fichas de análise no `mfg.db`, e os blocos injetados nos prompts (ver [Memória do agente](#memória-do-agente--o-que-ele-aprende-entre-sessões)) |
| `feedback.py` | Funções puras da avaliação das respostas (👍 e 👎): acham a resposta avaliada no checkpoint, dizem se é uma resposta de dados e normalizam o começo do texto com que o front a identifica |
| `fichas.py` | Fichas de análise, a memória de episódios: embeddings do Vertex (`gemini-embedding-001`), gravação, busca por similaridade de cosseno com `numpy` e o texto injetado no analista e devolvido pela tool `buscar_analises_anteriores` |
| `tests/` | Testes offline (sem rede e sem LLM) da memória e das avaliações: `.venv/Scripts/python -m unittest discover -s tests`. Trava as versões validadas das bibliotecas, confere que o Vertex aceita o schema da saída do extrator, e cobre a mesclagem das operações, as fichas de análise (validação, gravação, busca com um embedding de teste e a privacidade dos ids), os blocos injetados nos prompts, o banco das avaliações e o bypass de `thought_signature` |
| `chart_store.py` | Artefatos (gráficos, PDFs, Excels) e alertas de threshold, também em `mfg.db` |
| `scheduler/daemon.py` | Loop do agendador e execução das tarefas (retry, timeout, relatórios) |
| `scheduler/runner.py`, `scheduler/context.py` | Sandbox do `task_code` e o objeto `ctx` injetado nele |
| `scheduler/condition.py` | Avaliação da condição dos monitores simples (SQL ou spec JSON via GraphQL) |
| `scheduler/tools.py` | Tools de agendamento (criar, listar, editar, testar e salvar `task_code`) |
| `scheduler/widget_runner.py`, `scheduler/widget_tools.py` | Sandbox e tools dos painéis do dashboard |
| `scheduler/md_parser.py` | Só `calculate_next_run` (o nome é histórico) |
| `skills/` | Skills genéricas em `.md` (hoje, `task_code_sandbox.md`) |
| `rag/<idioma>/` | Textos de autodescrição lidos por `rag_capacidades` e `rag_arquitetura` |
| `config/` | Pack de configuração (ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código)) |
| `frontend/` | SPA Angular 19 (ver [Frontend](#frontend--angular-19-standalone-components)) |
| `reports/` | Saída do daemon: um diretório por execução de tarefa agendada |
| `static/` | `images/logo.png` |

---

## Plug-and-play — trocar de banco e domínio sem mexer em código

O sistema não é escrito para um schema ou cliente específico. Tudo que ele sabe sobre "que banco consultar" e "como entender esse domínio" vem de um **pack de configuração** na pasta `config/`, lido em runtime — trocar de cliente/domínio é trocar arquivos, não código.

```text
config/
  settings.yml                  # conexão, domínio ativo, idioma default
  dominio_example.md            # template comentado para integrar um domínio novo
  <slug>/                       # slug = app.dominio sem o prefixo "dominio_" e sem ".md"
    pt-BR/  en/  zh/            # um diretório por idioma
      dominio.md                # conhecimento de domínio nesse idioma
      dashboard_default.py      # painéis padrão do dashboard nesse idioma
```

### `settings.yml`

```yaml
app:
  nome: "New Age"
  descricao: "Análise de custo de mão de obra direta (DL cost) pago a fábricas contratadas (CMs)"
  dominio: "dominio_new_age.md"   # chave de identidade — ver aviso abaixo
  mes_inicio_fiscal: 4            # opcional (default 1)
  idioma_default: "pt-BR"
database:
  modo: graphql                   # sql | graphql (default: sql)
  url: "sqlite:///allocation.db"  # só no modo sql
graphql:                          # só no modo graphql
  url: "https://.../graphql"
  api_key: "${NEW_AGE_API_KEY}"   # ou token: "${GRAPHQL_TOKEN}"
```

| Chave | Papel |
| --- | --- |
| `app.nome`, `app.descricao` | Branding do dashboard (`/app-info`) e identidade injetada nos prompts |
| `app.dominio` | Qual domínio está ativo. O valor é, ao mesmo tempo, o nome histórico do arquivo e a **chave de identidade gravada no banco** (ver aviso abaixo); a pasta `config/<slug>/` é derivada dele |
| `app.mes_inicio_fiscal` | Mês (1–12) em que o ano fiscal começa. `calcular_periodo` usa para resolver "este/próximo/último quarter" e "este ano" no calendário fiscal. Ausente = 1 (trimestre civil) |
| `app.idioma_default` | Idioma de fallback de conteúdo (`pt-BR`, `en` ou `zh`) — ver [Idioma](#idioma-i18n--ui-conteúdo-de-domínio-e-respostas) |
| `database.modo` | Método de acesso aos dados do domínio: `sql` ou `graphql` — ver [Modos de acesso](#modos-de-acesso-a-dados--sql-ou-graphql) |
| `database.url` | URL SQLAlchemy (só no modo `sql`). Segredos via `${VAR}`; a env `DATABASE_URL`, se definida, tem precedência total. Caminho SQLite relativo é resolvido contra a raiz do projeto |
| `graphql.url` + `token` ou `api_key` | Endpoint e credencial (só no modo `graphql`). `token` vira `Authorization: Bearer`; `api_key` vira `X-API-Key`. Cada domínio usa um dos dois |

Trocar `app.dominio` (e o bloco `database`/`graphql` correspondente) reaponta o sistema inteiro — chat, painéis do dashboard, tarefas agendadas, monitores — para outro banco. **Requer restart do app**: a identidade do app, as seções `## Regras do agente` e `## Regras do orquestrador`, a escolha das tools SQL × GraphQL e as regras do sandbox de agendamento (`## Exemplos task_code`) são montadas uma vez no init. O que é lido a cada uso — `## Modelo de dados`, `## Domínio de negócio`, `## Períodos`, `## Dica para condition_sql` e o `dashboard_default.py` — é relido sozinho quando o arquivo muda (cache por `mtime` em `db_config.py`), sem restart.

> **A chave `dominio` é um contrato com o banco local.** O valor de `app.dominio` (ex: `dominio_new_age.md`) é gravado, literalmente, na coluna `dominio` de `scheduled_tasks`, `dashboard_widgets`, `dashboard_widget_order`, `threshold_alerts`, `charts`/`pdfs`/`excels`, `task_code_audit` e `session_dominio`, e toda listagem filtra por `(dominio = ? OR dominio IS NULL)`. Renomear ou reformatar esse valor deixa as linhas existentes "órfãs" — não são apagadas, mas deixam de aparecer. O nome da pasta por idioma é derivado dele sem alterá-lo (`db_config._dominio_slug`).

### Seções canônicas de um `dominio.md`

As primeiras linhas do arquivo (`# skill:`, `# descricao:`, `# palavras-chave:`) alimentam o catálogo de skills do analista; as palavras-chave também entram no prompt de roteamento do orquestrador. O corpo é dividido por headings `##`:

- `## Regras do agente` — injetada no prompt do sub-agente analista (armadilhas de schema, qual coluna de data usar, granularidade real dos dados)
- `## Regras do orquestrador` — injetada no prompt do orquestrador (o que este sistema É/não é, para negar assuntos de outros domínios; qual período assumir quando o usuário não diz nenhum)
- `## Períodos` — machine-parsable: heading `### frases | separadas` ou `### re:<regex>` seguido de um bloco ```` ```sql ```` que retorna 1 linha `(from, to)`. Resolve frases como "mês passado" sem depender do calendário real — útil quando os dados são um snapshot cuja "data atual" não é hoje. Ordem importa: a primeira entrada que casar vence. Em domínios `graphql` sem `database.url`, essas SQLs não têm onde rodar: falham em silêncio e `calcular_periodo` cai no fallback de calendário (que cobre "todo o período" = `2000-01-01` até hoje e, via `mes_inicio_fiscal`, quarters e ano fiscal)
- `## Modelo de dados` — schema, joins, exemplos; entra no prompt do analista e é servida por `rag_dados`
- `## Domínio de negócio` — glossário e regras de negócio (não-SQL); servida por `rag_dominio`
- `## Exemplos task_code` — anexada às regras do sandbox de tarefas agendadas, para o agente copiar o padrão de `run(from_date, to_date, ctx)` com `ctx.sql` (modo `sql`) ou `ctx.graphql_*` (modo `graphql`)
- `## Dica para condition_sql` — tabelas/colunas úteis e o formato esperado para a criação de monitores/alertas

Headings internos de uma seção devem ser `###` — nunca `##`, que é o delimitador de seção. Seção ausente não quebra o sistema, só degrada: o parser (`db_config._split_secoes`) trata cada uma como opcional.

### Packs que acompanham o repositório

| Pack (`config/<slug>/`) | Modo | Negócio |
| --- | --- | --- |
| `new_age` | `graphql` (`api_key`) | Custo de mão de obra direta (DL cost) pago a fábricas contratadas; ano fiscal começando em abril. É o ativo no `settings.yml` do repositório |
| `posh` | `graphql` (`token`) | Pedidos de compra (Brazil Purchase Orders) |
| `alocacao` | `sql` (SQLite `allocation.db`) | Alocação de supply por Key Account |

O `settings.yml` do repositório guarda os blocos dos outros packs comentados — alternar de domínio é comentar/descomentar. Cada pack tem os três idiomas. O diretório `demo/config-vendas/` ainda usa o layout antigo (arquivos planos) e precisaria ser migrado para `<slug>/<idioma>/` para ser carregado pelo resolvedor atual. Para um domínio novo, copie `config/dominio_example.md` para `config/<slug>/<idioma>/dominio.md` — o template explica cada seção, nos dois modos de acesso.

---

## Modos de acesso a dados — SQL ou GraphQL

Cada domínio usa **exatamente um** método de acesso, definido em `database.modo`. A escolha é feita no init e muda as tools do analista, o `ctx` do sandbox e o formato das condições de monitor:

| | `sql` | `graphql` |
| --- | --- | --- |
| Configuração | `database.url` (SQLAlchemy) | `graphql.url` + `token` ou `api_key` |
| Tools do analista (chat) | `executar_sql(query, chave)` | `executar_graphql` (linhas cruas, `reportTable`), `executar_graphql_agregado` (GROUP BY no servidor, `reportAggregate`) e `executar_graphql_schema` (schema real, `reportSchema` — só para destravar quando a skill diverge do servidor) |
| `ctx` no sandbox de tarefas e painéis | `ctx.sql(query)` | `ctx.graphql_table(...)` e `ctx.graphql_aggregate(...)` — mesmos argumentos das tools, mas **sem** o parâmetro `chave` |
| `condition_sql` de um monitor | Query `SELECT` | String JSON `{"type": "aggregate" ou "table", ...}` |
| Salvaguarda | `run_select()` rejeita tudo que não for `SELECT` | O cliente só emite as três queries de leitura |

`ctx.sql()` em um domínio `graphql` levanta um erro que aponta para os métodos GraphQL, e as regras do sandbox injetadas nos prompts são adaptadas ao modo ativo (`_sandbox_skill_texto`).

O `graphql_config.py` é o único ponto de contato com o endpoint (timeout de 30 s, erros do servidor viram `GraphQLError`). Alguns detalhes que existem por causa de casos reais:

- `expand` faz join de **um salto** por FK. Em `reportTable` as colunas vêm prefixadas (`site_id__name`); em `reportAggregate` vêm com o nome puro, ao lado de `groupKey` e `metricValue`
- `run_aggregate` acrescenta o nome real do `group_by` como alias de `groupKey`, porque o código gerado pelo LLM esperava `df["site_id"]` e quebrava com `KeyError`
- Objetos `date` (o `from_date`/`to_date` das tarefas) são serializados em ISO antes de ir ao servidor
- Em `reportAggregate` o servidor assume `limit` 20 e aceita no máximo 500

---

## Duas bases de dados: banco de domínio × banco local do app

É fácil confundir os dois bancos que o sistema usa — eles têm papéis completamente diferentes:

| | Banco de domínio | Banco local do app |
| --- | --- | --- |
| **O que é** | O banco de negócio que o chat consulta (SQL via `database.url`, ou o endpoint GraphQL do pack) | `mfg.db` (SQLite), no caminho de `DB_PATH` |
| **Quem lê/escreve** | Apenas leitura (ver [Modos de acesso](#modos-de-acesso-a-dados--sql-ou-graphql)) | Leitura e escrita — é o "estado" do próprio app |
| **O que guarda** | Os dados que o negócio quer analisar | Tarefas agendadas (`scheduled_tasks`, `task_runs`, `task_code_versions`, `task_id_sequence`), auditoria de erros de geração de código (`task_code_audit`), alertas de threshold (`threshold_alerts`), painéis customizados (`dashboard_widgets`) e a ordem dos painéis por usuário (`dashboard_widget_order`), artefatos gerados (`charts`, `pdfs`, `excels`), qual domínio cada sessão de chat usava (`session_dominio`), as avaliações das respostas pelo usuário, com pergunta, resposta, abordagem, nota e mensagem seguinte (`feedback_respostas`), o resumo da abordagem de cada resposta de dados (`abordagens`), o histórico de conversa do LangGraph (`checkpoints`, `writes`) e a memória do agente: lições, convenções, preferências e perfil (`store`, `store_migrations`) e as fichas de análise (`fichas`) |
| **Troca com a config?** | Sim — é exatamente o que `settings.yml` aponta | Não — é fixo por instalação, independe de qual domínio está ativo |

Quem cria o quê: `db.py` (`init_db` + `migrate_db`, no startup) cuida das tabelas de tarefas, painéis, auditoria, ordem, sessão×domínio, avaliações das respostas (`feedback_respostas`), resumo da abordagem (`abordagens`) e fichas de análise (`fichas`); `chart_store.init_chart_store` cria `charts`, `pdfs`, `excels` e `threshold_alerts`; o `SqliteSaver` do LangGraph cria `checkpoints` e `writes`; o `SqliteStore` do LangGraph, aberto por `memoria.init`, cria `store` e `store_migrations`. As tabelas de tarefas, painéis, alertas e artefatos carregam `user_id` e `dominio`; o histórico de execuções e as versões de código herdam o escopo da tarefa pelo `task_id`; a memória do agente separa por namespace do store (domínio nas lições; `user_id` e domínio nas preferências, no perfil e nas convenções), não por coluna; as fichas são uma tabela com `dominio` e `user_id` (o autor). Os **painéis padrão** do dashboard não estão aqui — vivem em arquivos do pack (ver [Painéis do dashboard](#painéis-do-dashboard--padrão-do-domínio-e-customizados)).

Na prática: apagar/mover `mfg.db` reseta tarefas, alertas, painéis, artefatos, **o histórico de conversa e a memória do agente** sem afetar o banco de domínio; trocar o banco de domínio via `settings.yml` não afeta nada que esteja em `mfg.db` — só quais linhas dele ficam visíveis.

Observação: `chart_store` só é inicializado junto com o agente (`init_multi_agent`). Sem `PROJECT_ID`, o agente não sobe e as rotas de artefatos e alertas devolvem vazio.

---

## Identidade do usuário (`user_id`) — como é coletada e usada

O sistema **não implementa login**. Ele é pensado para ser embutido (iframe/embed) dentro de um app host que já resolveu autenticação — o `user_id` é apenas um identificador que separa os dados de cada usuário dentro do mesmo domínio ativo. A responsabilidade de garantir que a pessoa certa está vendo o `user_id` certo é do app host, não deste sistema.

### Como o `user_id` chega ao sistema

```text
App host (já autenticado)
  └── carrega a página com ?user_id=<id-do-usuário-no-host>
        └── UserSessionService (Angular) lê o parâmetro na URL (new URLSearchParams(location.search))
              ├── se presente → sobrescreve o valor salvo em localStorage (fonte da verdade é sempre o host)
              └── se ausente  → usa o valor já salvo em localStorage, ou o primeiro usuário demo ('123')
                    └── persiste em localStorage['mfg_user_id'] para sobreviver a reloads
```

Esse `_userId` do frontend é então enviado **explicitamente** em toda chamada que precisa de isolamento por usuário: `/chat/stream?...&user_id=...`, `/tasks?user_id=...`, `/dashboard-widgets?user_id=...`, `/artifacts?user_id=...`, etc. Não existe cookie de sessão nem header de autenticação — é um parâmetro de query passado a cada request, no mesmo espírito plug-and-play do resto do sistema.

**Modo standalone/demo**: quando não há host embutindo a página (ex: `http://localhost:8000` aberto direto no browser), o sistema assume um dos usuários demo (`123`, `321`, `456` — `DEMO_USER_IDS`) e o header traz um seletor para alternar entre eles (troca o `user_id` no `localStorage` e recarrega a página). Serve para demonstrar o isolamento multiusuário; não é autenticação real.

### Como o `user_id` percorre o backend

No FastAPI, `/chat` e `/chat/stream` recebem `user_id` como parâmetro e o repassam para `invoke_multi_agent()` / `stream_multi_agent()`. De lá, o valor entra num `contextvars.ContextVar` próprio (`_current_user_id`, em `agent_multi.py`), separado do `_current_session` (que carrega o `session_id`/thread de conversa). A separação existe porque as duas coisas são independentes: o mesmo `user_id` pode ter várias conversas (`session_id`s) abertas, e o `session_id` sozinho nunca deveria ser usado para inferir quem é o usuário.

Como `ContextVar` não atravessa `threading.Thread` criadas manualmente (só atravessa pools como o `ThreadPoolExecutor` que o LangGraph usa internamente), o valor é re-setado explicitamente no ponto onde o streaming SSE sobe uma thread própria para rodar o orquestrador — o mesmo cuidado vale para `session_id`, para o handle de cancelamento e para o idioma.

Tools como as de painéis customizados (`add_chart_to_dashboard`, `list_dashboard_widgets`, etc., em `scheduler/widget_tools.py`) e as de agendamento (`scheduler/tools.py`) leem o usuário atual direto desse `ContextVar` — não inferem mais nada a partir do formato do `session_id`.

Tarefas agendadas são o único fluxo que não passa pelo `ContextVar`: `user_id`, `dominio` e `lang` ficam gravados na própria linha da tarefa (`scheduled_tasks`) no momento da criação, e o daemon os repassa diretamente ao `TaskContext` de cada execução — por isso um monitor criado por um usuário continua rodando (e gerando artefatos e alertas com o `user_id` correto) mesmo sem nenhuma sessão de chat ativa.

### Onde o `user_id` separa dados

| Tabela / recurso | Coluna | Sem `user_id` no request |
| --- | --- | --- |
| `scheduled_tasks` (tarefas agendadas) | `user_id` | Vê só as tarefas sem dono (`user_id IS NULL`) |
| `dashboard_widgets` (painéis customizados) | `user_id` | Vê só os painéis sem dono (e os compartilhados) |
| `dashboard_widget_order` (ordem dos painéis) | `user_id` (chave junto com `dominio`) | Sem ordem salva — ordem natural |
| `threshold_alerts` (alertas de threshold) | `user_id` | Vê só alertas sem dono |
| `charts` / `pdfs` / `excels` (artefatos gerados) | `user_id` | Vê só artefatos sem dono |
| `store`, namespaces `users/<user_id>/<dominio>/preferences`, `.../profile` e `.../conventions` (preferências, perfil e convenções, por usuário) da [memória do agente](#memória-do-agente--o-que-ele-aprende-entre-sessões)) | Namespace, não coluna | Não lê nem grava preferências, perfil nem convenções |

Nas tabelas, a regra é sempre a mesma cláusula SQL — `(user_id = ? OR user_id IS NULL)` — aplicada com o parâmetro vindo do request: uma linha com `user_id` preenchido só aparece para o dono; uma linha com `user_id NULL` é tratada como "compartilhada" e aparece para qualquer um (esse é o caso de registros antigos, criados antes dessa separação existir, e serve como modo degradado seguro quando o host esquece de mandar o parâmetro — ninguém vê dados de outro usuário, só dados sem dono). O filtro por `dominio` (`(dominio = ? OR dominio IS NULL)`) é aplicado por cima, sempre.

Painéis customizados têm uma regra a mais: com `shared = 1` o painel fica **visível** para todos os usuários do domínio, mas só o dono (ou uma linha legada sem dono) pode editar, remover ou alterar o compartilhamento — "compartilhado" dá visibilidade, não permissão de escrita.

A memória do agente segue outra lógica, e **sempre separada por domínio**: as preferências, o perfil e as convenções pessoais ficam num namespace por `user_id` **e** domínio (o que o usuário diz num domínio não aparece em outro); as lições ficam num namespace só por domínio e são **compartilhadas por todos os usuários** dele, porque descrevem o banco e o negócio, não a pessoa. Uma escolha pessoal de análise (moeda de exibição, sites padrão) vai para as convenções, para não virar regra de todos.

### Três eixos independentes: `dominio`, `user_id` e `lang`

Não confundir os três:

| Eixo | Escopo | Quem define | O que decide |
| --- | --- | --- | --- |
| `dominio` | **Processo** (muda com restart) | `settings.yml` | Qual banco/negócio está ativo para toda a instalação, e quais lições da memória do agente valem |
| `user_id` | **Requisição** | App host (`?user_id=`) | De quem são tarefas, painéis, alertas e artefatos dentro daquele domínio, e as preferências, o perfil e as convenções da memória do agente (também separadas por domínio) |
| `lang` | **Requisição** | App host (`?lang=`) ou seletor do header | Em que idioma a interface, as respostas e o conteúdo de domínio são servidos |

Trocar o domínio não redistribui usuários; trocar de usuário não troca o banco de negócio consultado; trocar de idioma não muda nem um nem outro.

### Histórico de conversa

O histórico de chat (checkpointer LangGraph) continua isolado por `session_id`, não por `user_id` diretamente — mas o frontend gera `session_id` como `<user_id>_<timestamp>` (`UserSessionService.newSessionId()`), e `/chat/sessions?user_id=...` lista as sessões cujo `thread_id` começa com esse prefixo e cujo domínio (`session_dominio`) é o ativo. Ou seja, a listagem de conversas de um usuário depende da convenção de nomenclatura do `session_id`, enquanto tarefas, painéis, alertas e artefatos usam uma coluna `user_id` real no banco.

Isso tem uma implicação direta para onde o `session_id` **ativo** fica guardado no browser: a chave de `localStorage` que lembra "qual foi a última conversa aberta" é namespaced por `user_id` (`mfg_session_id::<user_id>`, em `UserSessionService`) — nunca uma chave global única. Sem esse namespacing, trocar o `user_id` na URL (ex: simular um segundo usuário no mesmo browser) continuaria devolvendo a última conversa de *outro* usuário salva naquele mesmo `localStorage`, porque a busca de histórico é por `session_id` e o `session_id` reaproveitado não teria trocado junto. Cada `user_id` mantém assim sua própria sessão ativa lembrada entre reloads, sem vazar para outro usuário do mesmo browser.

---

## Idioma (i18n) — UI, conteúdo de domínio e respostas

O idioma é resolvido **por requisição** (ao contrário do `dominio`, que é fixo por processo), então trocar de idioma não exige restart. Nenhuma tabela grava idioma como parte de uma chave de identidade — é só resolução de arquivo e de texto. Idiomas suportados: `pt-BR`, `en` e `zh`.

```text
Host (opcional) carrega a página com ?lang=pt-BR | en | zh
  └── LocaleService (Angular)
        ├── ?lang= vence; senão localStorage['mfg_lang']; senão 'en'
        ├── TranslateService carrega /i18n/<lang>.json  → interface
        └── envia `lang` em /chat, /chat/stream, /chat/start-task-*, /dashboard-widgets e /dashboard-widgets/{id}/data
              └── backend: db_config.set_lang(lang) (ContextVar) → db_config.get_lang() no resto da requisição
```

Trocar de idioma pelo seletor do header grava no `localStorage` e recarrega a página. O idioma default da **interface** é `en` (decidido no front); o `app.idioma_default` do `settings.yml` é outra coisa — o fallback do **conteúdo** de domínio quando o idioma pedido ainda não tem arquivo.

| Camada | Como é traduzida |
| --- | --- |
| Interface | `frontend/public/i18n/<lang>.json`, chaves em notação de ponto, usadas pelo `TranslatePipe`. Chave ausente aparece como a própria chave |
| Conteúdo de domínio | `config/<slug>/<lang>/dominio.md` e `dashboard_default.py`. Arquivo ausente no idioma pedido → fallback para `app.idioma_default` (`db_config._resolver_com_fallback`) |
| Autodescrição do agente | `rag/<lang>/capacidades.md` e `arquitetura.md`, mesmo fallback |
| Resposta do agente | Os prompts continuam escritos em português; cada turno recebe no topo a diretiva `## IDIOMA DA RESPOSTA` (`_diretiva_idioma`). LLMs seguem instruções num idioma e respondem em outro sem problema |
| Rótulos de progresso (eventos SSE `tool`) | O código escreve em português; `_lbl()` troca pelo equivalente em `_LABEL_TRANSLATIONS`. Chave não traduzida cai no português |
| Textos injetados pelo backend | `_ARTIFACT_TEXT` (alt do gráfico, "artefato indisponível"), os turnos "canned" de criar/editar tarefa e o corpo do e-mail dos relatórios (`_EMAIL_TEXT`) |
| Tarefas agendadas | `scheduled_tasks.lang` guarda o idioma da requisição que criou a tarefa. O daemon roda sem requisição, então usa esse valor para o idioma do código gerado, da análise (`ctx.analisar`) e do e-mail |

Ficam só em português as mensagens de erro das rotas da API e o prompt da transcrição de voz. Para **adicionar um idioma**: incluí-lo em `IDIOMAS_SUPORTADOS` (backend em `db_config.py`, frontend em `locale.service.ts`), criar o JSON de interface, as pastas `config/<slug>/<lang>/` e `rag/<lang>/`, e as entradas correspondentes em `_DIRETIVA_IDIOMA`, `_IDIOMA_NOME`, `_LABEL_TRANSLATIONS`, `_ARTIFACT_TEXT` e `_EMAIL_TEXT`.

---

## Grafo multi-agente (LangGraph)

A espinha dorsal é um conjunto de grafos de estados (StateGraph) em dois níveis — um orquestrador e, abaixo dele, sub-grafos especializados que rodam dentro de `@tool`s:

```text
Usuário (voz ou texto, com recortes de tela opcionais)
  └── Orquestrador (LangGraph + checkpointer)
        ├── consultar_analista()          → sub-agente analista
        ├── calcular_periodo()            → resolve datas em linguagem natural
        ├── get_current_datetime(), get_dashboard_charts(), ver_grafico()
        ├── rag_dominio(), rag_capacidades(), rag_arquitetura(), rag_dados()
        ├── buscar_analises_anteriores()  → fichas de análises parecidas já feitas
        ├── gerar_pdf(), gerar_excel()
        ├── criar_tarefa_agendada()       → pipeline determinístico de criação de tarefa
        ├── editar_tarefa_agendada()      → pipeline determinístico de edição
        ├── gerenciar_agenda()            → sub-agente de scheduling (listar, pausar, apagar...)
        ├── get_task_code()
        └── painéis: test_widget_code(), add_chart_to_dashboard(), list_dashboard_widgets(),
                     get_widget_code(), update_widget(), delete_dashboard_widget()

              Sub-agente Analista (sub-grafo separado, sem checkpointer)
                ├── read_skill()                → outras skills do catálogo (dominio.md já vem no prompt)
                ├── executar_sql()              → modo sql
                │   executar_graphql()          → modo graphql: linhas cruas
                │   executar_graphql_agregado() → modo graphql: GROUP BY no servidor
                │   executar_graphql_schema()   → modo graphql: schema real, só para destravar
                ├── analisar_dataframe()        → executa código Python/Pandas no namespace da sessão
                └── gerar_pdf(), gerar_excel()

              Sub-agente de Scheduling (tools descobertas em scheduler/tools.py)
                └── schedule_task, schedule_monitor, update_scheduled_task, list/delete/toggle_pause,
                    get/set_task_instructions, get_task_code, test_task_code, save_task_code,
                    get_task_code_versions, restore_task_code_version

Memória do agente (memoria.py — LangMem, em background, fora do caminho da resposta)
  ├── consultar_analista() → add_trace()   → guarda o trace do analista (SQL, erros, retries)
  ├── fim do turno → finalizar_turno()     → ReflectionExecutor: extração única (lições, convenções, preferências, perfil e fichas de análise)
  └── turnos seguintes                     → lições, convenções e fichas parecidas no prompt do analista; preferências nos dois; perfil no do orquestrador

Daemon de agendamento (loop asyncio + threads, no mesmo processo do FastAPI)
  └── Executa o task_code salvo de cada tarefa — sem LLM (exceto ctx.analisar)
        ├── ctx.notify() / ctx.alert()   → grava alerta de threshold no banco
        └── reports/<id>_<nome>_<data>/  → content.md + metadata.json (preparado para um envio externo por e-mail)
```

O orquestrador **não sabe analisar dados** — ele só sabe a quem perguntar. O sub-agente **não tem acesso ao histórico de conversa** — recebe só o pedido pontual delegado (mais o último script executado na sessão, como referência para ajustes, e o que a [memória do agente](#memória-do-agente--o-que-ele-aprende-entre-sessões) aprendeu: lições do banco e preferências do usuário). Essa separação de responsabilidades é deliberada: cada camada faz uma coisa e faz bem.

### Roteamento do orquestrador

O prompt do orquestrador classifica cada mensagem em uma de seis categorias e segue só o fluxo dela:

| Categoria | Pedido | Caminho |
| --- | --- | --- |
| A | Consulta pontual de dados | `consultar_analista(periodo=..., tipo=...)`. O período vai em linguagem natural e é resolvido dentro da tool; `tipo` é `tabela` (padrão), `grafico` ou `ambos` — "mostre" e "compare" não são pedido de gráfico |
| B | Pergunta conceitual (o que é X, como se calcula Y) | Tools `rag_*`, sem buscar dados |
| B2 | Menção a uma análise anterior ("lembra da análise que fiz?", "alguém já analisou X?") | `buscar_analises_anteriores`; para os valores, refaz com `consultar_analista` |
| C | Agendamento (criar, editar, listar, pausar, apagar) | `criar_tarefa_agendada`, `editar_tarefa_agendada` ou `gerenciar_agenda` |
| D | Pergunta simples (data, hora, saudação) | Responde direto |
| E | Pergunta sobre o que está na tela (texto ou imagem anexada) | `get_dashboard_charts()` e, se preciso, `rag_dominio()` |
| F | Painel customizado no dashboard (adicionar, fixar, editar, remover) | Confirma a intenção, descobre os dados via `consultar_analista(para_agendamento=True)`, valida com `test_widget_code` e salva com `add_chart_to_dashboard` |

Na dúvida entre A e C/F, vale A: nunca se cria tarefa nem painel sem palavras explícitas de agendamento ou de inserção no dashboard.

### Quem escreve a resposta final

O sub-agente analista devolve um relatório técnico **conciso**, sem prosa: bullets com os achados e os números exatos, a tabela (no máximo ~20 linhas), o token `[chart:uuid]` e blocos obrigatórios — **Parâmetros da análise** (sempre que consultou dados: período e todo filtro/agrupamento que realmente entrou na consulta, marcando o que foi assumido) e **Dados do gráfico/relatório/planilha** (quando há artefato: pontos-chave para responder follow-ups sem reprocessar). Quem redige a resposta ao usuário, no idioma dele, é o orquestrador, que só pode usar números presentes no retorno e deve preservar todas as ressalvas. Para follow-ups visuais ("por que há um pico?") existe `ver_grafico`, que injeta a imagem do gráfico no contexto.

### LLMs por papel

O modelo é o `MODEL_NAME` (padrão `gemini-3.8-flash`, via Vertex AI), instanciado em cinco papéis com temperatura e nível de raciocínio próprios — o analista faz trabalho mecânico (ler schema, montar filtro, gerar código) onde determinismo importa mais que variedade:

| Papel | Temperatura | Nível de raciocínio (env) | Usado em |
| --- | --- | --- | --- |
| Analista | 0,15 | `THINKING_LEVEL_ANALISTA` | Sub-agente analista, sub-agente de scheduling e pipeline de agendamento |
| Orquestrador | 0,4 | `THINKING_LEVEL_ORQUESTRADOR` | Roteamento e redação da resposta |
| Orquestrador (roteamento) | 0,4 | `THINKING_LEVEL_ORQUESTRADOR_ROTEAMENTO` (opcional) | Só a 1ª chamada de cada turno (decidir a tool); sem a env, usa o papel acima |
| Relatório | 0,4 | `THINKING_LEVEL_RELATORIO` (padrão `medium`) | `ctx.analisar`, que roda no daemon sem ninguém esperando |
| Memória (extrator) | 0,1 | `THINKING_LEVEL_MEMORIA` (padrão `medium`) | Extração única de lições, convenções, preferências, perfil e fichas de análise da [memória do agente](#memória-do-agente--o-que-ele-aprende-entre-sessões), em background |

Os níveis aceitos são `low`, `medium` e `high`; sem a env, vale o padrão do modelo. O `ChatVertexAI` é estendido (`ChatVertexAIThinkingLevel`) para enviar `thinking_level` pelo endpoint v1 e reinserir as `thought_signature` das chamadas de função, que o wrapper original descarta e que o Gemini 3 exige na chamada seguinte a qualquer tool call. Para chamadas de função sem assinatura real, a classe usa o valor de bypass documentado pelo Google (`skip_thought_signature_validator`). A memória já dependeu disso (quando o trustcall reinjetava o documento como uma tool call), mas hoje nenhum fluxo conhecido precisa dele.

---

## Skills — documentação sob demanda

As skills são arquivos `.md` com instruções que o sub-agente consulta quando precisa. Há dois tipos:

| Skill | Origem | Como chega ao agente |
| --- | --- | --- |
| `dominio.md` | O pack ativo (`config/<slug>/<lang>/dominio.md`), sem as seções já injetadas em outros prompts (`Regras do agente`, `Regras do orquestrador`, `Períodos`) | **Carregada no system prompt do analista** a cada chamada — economiza uma rodada de LLM por análise, já que o analista sempre precisa do schema |
| Skills genéricas | `skills/*.md` (hoje, `task_code_sandbox.md`: regras e exemplos do sandbox de `task_code`) | Só o cabeçalho (4 primeiras linhas) entra no catálogo; o conteúdo vem por `read_skill('nome.md')` ou é injetado nos prompts de geração de código |

O catálogo no prompt lista o cabeçalho de cada skill. É lazy-loading aplicado ao contexto do LLM: skills genéricas que não serão usadas não custam tokens, e o sistema escala para várias skills sem inflar o custo por token de cada requisição.

Para conectar um domínio novo **não é preciso criar skill**: o conhecimento específico vive nas seções do `dominio.md` (ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código)).

Os textos de `rag/<lang>/` são outra coisa: são o que o agente lê **sobre si mesmo** quando o usuário pergunta "o que você faz?" ou "como você funciona?" (`rag_capacidades`, `rag_arquitetura`). Precisam ser mantidos em sincronia com o sistema, como este documento.

---

## Namespace persistente — o ambiente Jupyter do agente

O problema central de análise incremental: quando o agente busca dados e depois chama `analisar_dataframe()`, os DataFrames precisam estar disponíveis entre as chamadas. Mas tools em LangGraph rodam em threads separadas do `ThreadPoolExecutor` — `threading.local()` não funciona aqui.

A solução: um `dict` global indexado por `session_id`, protegido por `threading.Lock`, com o `session_id` atual propagado por `contextvars.ContextVar` (que, diferente de `threading.local()`, **se propaga automaticamente para threads filhas**).

O resultado é um ambiente de execução Python que se comporta exatamente como um notebook Jupyter:

- `executar_sql(query, chave='producao')` (ou uma das tools `executar_graphql*`) → injeta o DataFrame como variável `producao` no namespace
- `analisar_dataframe(script)` → o script acessa `producao` diretamente, pode criar `resultado`, que fica disponível na próxima chamada
- Variáveis persistem entre múltiplas chamadas de tools e entre invocações consecutivas do sub-agente
- O namespace de uma sessão expira após 10 minutos sem uso (`ns_cleanup_loop`)
- Os últimos 3 scripts executados ficam guardados e são reapresentados ao analista, para que um pedido do tipo "mesmo gráfico, mas em cinza" reaproveite o código em vez de recomeçar

Quando o analista explora dados para **criar uma tarefa agendada**, `analisar_dataframe` passa a rodar com os mesmos builtins e variáveis pré-injetadas do sandbox de produção (`_sandbox_mode`) — erros de código que só apareceriam na primeira execução do daemon aparecem já na exploração.

---

## Proteção de contexto — o agente que não se afoga em dados

Um DataFrame com 30 dias de produção horária por linha pode ter milhares de linhas. Enviar isso para o modelo a cada tool call é caro, lento e desnecessário.

A regra implementada:

| Saída | Condição | O que o LLM recebe |
| --- | --- | --- |
| Tabela completa | ≤ 100 linhas (`_MAX_RESULT_ROWS`) | Markdown completo |
| Descrição estrutural | > 100 linhas | shape + dtypes + primeira linha |
| Resumo de namespace | Sempre | Metadados das variáveis (nome, tipo, shape) |
| Dados brutos | Nunca | — Protegido no backend |

O limite é baixo de propósito: o retorno de uma tool é reenviado ao LLM em **todas** as chamadas seguintes do turno. Além dele, o prompt do analista manda agregar (`DISTINCT`/`GROUP BY`) em vez de listar linhas repetidas e limitar as tabelas da resposta final a ~20 linhas.

O agente sempre sabe o que está disponível no ambiente (via `_ns_summary`) sem nunca receber os dados em si. Ele só vê dados quando o resultado da análise é pequeno o suficiente para ser relevante.

---

## Checkpointer — memória de conversa por sessão

O histórico de mensagens do orquestrador é persistido automaticamente pelo `SqliteSaver` do LangGraph — **no mesmo `mfg.db`** do app (tabelas `checkpoints` e `writes`). Cada `session_id` tem seu próprio thread de conversa — o usuário pode retomar uma análise horas depois e o agente lembra o contexto. Apagar uma conversa (`DELETE /chat/sessions/{id}`) remove também seus artefatos.

Para evitar que o contexto cresça indefinidamente (e o custo por token junto), o orquestrador envia ao modelo apenas as últimas `MAX_INTERACOES × 2` mensagens (padrão: 10 interações) — o histórico completo fica no banco, mas a janela ativa é controlada.

Cada sessão é marcada com o domínio ativo no primeiro turno (`session_dominio`, gravação idempotente). `/chat/history` e `/chat/sessions` escondem as sessões de outro domínio, então trocar o `settings.yml` não mistura conversas de negócios diferentes. Ao recarregar o histórico, `/chat/history` reconstrói o balão de raciocínio de cada resposta a partir das mensagens intermediárias (as que chamaram tools).

Os botões "Agendar nova tarefa via chat" e "Editar via chat" abrem uma sessão nova já com um turno inicial pronto (`start_task_creation_chat` / `start_task_edit_chat`): as duas mensagens são gravadas direto no checkpoint com `update_state`, **sem chamar o LLM**, e entram no histórico que o orquestrador vê no próximo turno real — o que ajuda o roteamento a reconhecer a continuação como pedido de agendamento mesmo sem palavras-chave.

O checkpointer guarda a conversa de cada sessão. O que o agente aprende **entre** sessões (lições sobre o banco e preferências do usuário) fica na [Memória do agente](#memória-do-agente--o-que-ele-aprende-entre-sessões).

---

## Memória do agente — o que ele aprende entre sessões

Além do histórico de cada sessão (checkpointer), o agente aprende entre sessões. Ele **não grava nada**: depois de cada turno, **uma única chamada estruturada ao LLM** (`_Extrator`, em `memoria.py`) lê o que aconteceu junto com o conteúdo atual dos documentos, em background, e devolve uma lista de operações (`MudancasMemoria`: destino, ação, texto antigo e texto novo); o código as aplica por casamento de texto (`aplicar_operacoes`) nos documentos do `mfg.db`. Os cliques 👍 e 👎 entram na mesma conversa como sinais, e vão para a avaliação da ficha da análise (ver abaixo); não há um segundo extrator. Como o mesmo modelo decide, na mesma chamada, em qual documento cada declaração entra, ela não vai para dois deles nem fica contraditória entre eles. Do **LangMem** só se usa o `ReflectionExecutor`, que agenda a extração e faz o debounce por sessão.

| Documento | Namespace no store | Escopo | Quem lê | Conteúdo |
| --- | --- | --- | --- | --- |
| **Lições do banco** | `db_lessons / <dominio>` | Por domínio (compartilhado por todos os usuários) | Sub-agente analista (quem escreve o SQL) | Duas listas de texto, uma regra curta por item. `verificadas`: erro técnico corrigido, ou correção ou regra declarada pelo usuário sobre o significado ou o cálculo certo (qual coluna, join ou fórmula usar para um tipo de análise). A lição é escrita como uma receita curta: o tipo de análise e como fazê-la. Não há mais hipóteses: uma contestação sem causa declarada vai para a ficha da análise, com contexto |
| **Convenções pessoais** | `users / <user_id> / <dominio> / conventions` | Por `user_id` dentro de cada domínio | Sub-agente analista | Como **este** usuário quer que as análises dele sejam delimitadas ou calculadas por padrão, sem ser uma definição do negócio: moeda de exibição, sites ou período padrão, filtros que sempre aplica, o vocabulário dele ("operação" = site). Lista de frases livres, só o que ele declara como permanente. Na dúvida entre uma lição compartilhada e uma convenção pessoal, é convenção pessoal, porque a lição muda as análises de todos |
| **Fichas de análise** | tabela `fichas` (não o store) | Por domínio; cada ficha tem autor (`user_id`), mas a busca considera as de **todos** os usuários | Sub-agente analista (as mais parecidas com o pedido) e orquestrador (tool `buscar_analises_anteriores`) | Uma por episódio de análise de dados: `titulo` (a intenção do usuário, normalizada), `intencao`, `funcionou`, `falhou` (e por quê), `avaliacao` (positiva, negativa, corrigida ou sem avaliação), mais o embedding de título e intenção. Descreve o que aconteceu, não é regra. **Nunca traz números** |
| **Preferências** | `users / <user_id> / <dominio> / preferences` | Por `user_id` dentro de cada domínio (não passam de um domínio para outro) | Orquestrador (redige a resposta) e analista (gráficos) | Uma lista de frases livres (`preferencias`), sem campos por assunto: cor, tipo de gráfico, formato, nível de detalhe, idioma etc. Só entra o que o usuário **declara** como permanente ("prefiro", "sempre que possível", "daqui para frente"); um pedido pontual ("faça um gráfico de barras") não é preferência, nem se repete, e nada é inferido por recorrência. Regras sobre qual dado usar ou como calcular são lições, não preferências |
| **Perfil** | `users / <user_id> / <dominio> / profile` | Por `user_id` dentro de cada domínio | Só o orquestrador (se dirige à pessoa e ajusta o tom) | Quem é o usuário, só como ele mesmo contou: `nome`, `cargo`, `funcao_area`, `atuacao`, `local_atuacao`, `equipe_gestor` (texto curto, ou vazio) e `outros`, lista livre para o que não couber nos campos. Nunca inferido do que ele pergunta; nunca dados de contato nem sensíveis (e-mail, telefone, documentos, saúde, credenciais). Dá para corrigir e esquecer ("esqueça meu nome"). O perfil diz **quem** é a pessoa; as preferências, **como** ela quer receber os resultados |

**O que nunca é gravado:** valores e resultados dos dados, cada pedido como registro, histórico de SQLs.

Os documentos ficam na tabela `store` do `SqliteStore` do LangGraph, dentro do próprio `mfg.db`. Pontos em `<dominio>` e `<user_id>` viram `_` (o LangGraph não aceita ponto em namespace).

### Fluxo

1. `consultar_analista` guarda o trace do analista (SQL, erros, retries, com a saída cortada em 300 caracteres) via `memoria.add_trace`. O orquestrador só vê o texto final do analista, então é a única chance de capturar isso.
2. No fim do turno, `memoria.finalizar_turno` fecha o registro e envia ao `ReflectionExecutor` (um só, para todos os documentos). O `thread_id` é o `session_id`: um turno novo substitui a extração ainda pendente da mesma sessão, e `MEMORIA_DELAY_SEGUNDOS` (padrão 120) é a espera antes de rodar. Essa espera deixa a pergunta, a contestação e a refeitura chegarem juntas ao extrator; para uma conversa sem pausas não adiá-la para sempre, a extração roda sem esperar quando há `MEMORIA_MAX_PENDENTES` (padrão 2) turnos ainda não extraídos. Turnos cancelados ou com erro são descartados. Com `user_id` a extração roda em todo turno (uma chamada de LLM), porque uma declaração de preferência, de convenção ou de perfil pode vir em qualquer mensagem. Sem `user_id` só há lições, e a extração só roda se a janela tiver trace do analista ou se o usuário declarar uma regra ("sempre", "nunca", "o correto é" e equivalentes em inglês e chinês).
3. A janela enviada ao LLM tem os turnos ainda não extraídos **mais um turno de contexto** (marcado `[CONTEXTO, já processado]`), para que uma contestação chegue junto com a análise contestada mesmo que ela já tenha sido extraída antes. O ponteiro de turnos extraídos avança dentro da própria extração (`_Extrator`), só quando ela termina sem erro; se falhar, os turnos voltam na próxima extração da sessão. Os turnos ficam num buffer em RAM de no máximo 6 por sessão (`_MAX_TURNOS_GUARDADOS`), de onde a janela é recortada; o extrator nunca lê o buffer inteiro. O limite existe para (1) fixar até onde um 👍 ou 👎 consegue voltar, (2) limitar a memória usada e (3) limitar a janela enviada ao LLM se a extração falhar várias vezes seguidas (o ponteiro não avança e os pendentes se acumulam). Com o teto de pendentes, o limite não faz mais turnos saírem antes de serem extraídos.
4. No turno seguinte, `bloco_licoes` entra no prompt do analista (depois do `dominio.md`), seguido de `bloco_convencoes` e, quando houver, das fichas mais parecidas com o pedido (só no analista; buscadas uma vez em `consultar_analista`); `bloco_preferencias` entra nos prompts do orquestrador e do analista e `bloco_perfil` só no do orquestrador. As preferências entram como instruções do usuário; as fichas, como referência.

### Proteções e detalhes de integração

- **Operações, não documentos.** O modelo não devolve os documentos, e sim `MudancasMemoria`: um `raciocinio` (vai para o log) e uma lista de `operacoes`, cada uma com `destino`, `acao`, `antigo` e `novo`. O `destino` diz o documento e a lista ou o campo: `licao_verificada`, `convencao_pessoal`, `preferencia`, `perfil_outros` ou um campo do perfil (`perfil_nome`, `perfil_cargo`, `perfil_funcao_area`, `perfil_atuacao`, `perfil_local_atuacao`, `perfil_equipe_gestor`). A `acao` é `adicionar`, `remover` ou `substituir` (reescrever, atualizar ou corrigir um item em uma só operação; em campo de perfil, adicionar e substituir definem o valor e remover o apaga). Ao lado das operações, o modelo devolve as `fichas` da sessão (ver abaixo).
- **Como o código aplica.** O texto que o modelo cita em `antigo` é casado com o item existente primeiro por igualdade (sem contar maiúsculas, espaços e pontuação final) e só então por semelhança (≥ 0,8); com dois candidatos quase empatados a citação é ambígua e nada é alterado. `substituir` de um item que não existe vira uma adição, com aviso no log. Frase igual (≥ 0,9) não é duplicada. Se a mesma frase aparecer em dois destinos, fica no de maior prioridade (convenção pessoal, lição, perfil e preferência, nessa ordem: na dúvida vale o destino que afeta menos gente). Os limites são impostos em código: 30 lições, 20 convenções, 20 preferências e 10 itens livres no perfil (descartam os mais antigos). Sem `user_id` só as lições mudam. Retirar uma lição verificada vai para o log como aviso.
- **Sem JSON Patch.** Antes o LangMem (trustcall) editava os documentos por JSON Patch, o que obrigava a ter lições em lista de texto (o Gemini não gera um objeto como valor de patch), a tirar o `null` dos schemas (`com_schemas_sem_null`), a usar o bypass de `thought_signature` e a ter uma trava contra a sobrescrita de lições verificadas. Nada disso existe mais: quem retira uma lição agora é uma decisão explícita no resultado, e não um efeito colateral de um patch.
- **A correção é ancorada nos parâmetros da análise.** Toda resposta de dados do analista termina com o bloco de parâmetros (período, tabelas e filtros realmente usados, com o que foi assumido marcado) e o de dados do gráfico. O prompt do extrator diz que uma correção do usuário quase sempre contesta um desses parâmetros ou uma etapa do cálculo, e manda registrar a abordagem correta, e não só "o resultado estava errado". Como esses blocos ficam no fim e o título muda com o idioma, o resumo da resposta enviado ao extrator mantém o começo (500 caracteres) e o fim (1800), em vez de cortar só o começo.
- **Avaliação das respostas (👍 e 👎).** Toda resposta de dados (a que passou pelo analista) mostra, ao lado do horário, dois botões pequenos; ao passar o mouse aparece "Avalie e aprimore as próximas respostas". Cada avaliação é gravada em `feedback_respostas`, com a pergunta, o texto da resposta, o resumo da abordagem, a nota, o usuário e o domínio. Há uma nota por resposta, e o usuário pode trocá-la. O botão volta marcado ao recarregar o histórico.
  - **Conjunto de dados:** depois de um 👎 o front mostra o lembrete "Diga no chat o que estava errado", e a primeira mensagem do usuário depois dele é guardada na mesma linha (`mensagem_seguinte`, via `db.anexar_mensagem_seguinte`). É literalmente a próxima mensagem: pode contestar a resposta ou ser outra pergunta sem relação, e o banco não presume nada.
  - **Resumo da abordagem.** Ao fim de cada resposta de dados, `memoria.renderizar_abordagem` resume o que o analista fez: as consultas e scripts que chamou e se cada uma deu certo ou erro (só as mensagens de erro entram, cortadas), **sem os valores devolvidos**. Fica na tabela `abordagens`, é copiado para a avaliação e apagado com a conversa.
  - **Para a memória, o clique é um sinal.** `memoria.avaliar_resposta` marca o turno na conversa (`[AVALIAÇÃO DO USUÁRIO: ... boa (👍)]` ou `... ruim (👎)]`, sem dizer o que vem depois) e reagenda a extração da sessão com o turno avaliado e os dois anteriores na janela. O extrator usa o sinal na `avaliacao` da ficha daquele episódio. Turno que já saiu da memória (mais de 6 turnos atrás, ou depois de um restart) só é registrado no banco.
  - Excluir a conversa apaga também as avaliações, os resumos de abordagem e as fichas dela. Essas tabelas guardam texto de perguntas e respostas, inclusive números de negócio (as fichas não), então vale tratá-las como dado sensível.
- **Fichas de análise (memória de episódios, `fichas.py`).** Na mesma chamada do extrator, o modelo devolve `fichas`: uma por episódio de análise de dados da sessão (a pergunta e os desdobramentos: contestação, refeitura, avaliação). Ele recebe as fichas que já existem na sessão e **atualiza** a do episódio (mesmo `ficha_id`) quando chega uma contestação, uma refeitura ou uma avaliação; cria uma nova só para um episódio novo. A ficha descreve o que aconteceu: não é regra, e não se mescla com fichas de outras sessões. Fichas parecidas, e até contraditórias, coexistem, com data, autor e avaliação.
  - **Avaliação e "falhou".** Vale o último sinal do usuário sobre o episódio: 👍 ou aprovação escrita é "positiva"; 👎, ou nova contestação depois de o agente refazer ou conferir, é "negativa"; contestação seguida de refeitura de **outro** jeito é "corrigida" (um 👍 depois mantém "corrigida"). Uma contestação que **não se confirmou** (o agente conferiu e a abordagem estava certa) não é sinal: a ficha mantém a avaliação que tinha, e `funcionou` ganha "conferido após contestação". `falhou` só recebe uma abordagem que de fato estava errada (erro resolvido por outra abordagem, ou contestação confirmada).
  - **Em código** (`validar_fichas`): ficha nova só se, nas mensagens ainda não processadas, ao menos uma consulta de dados deu certo (resultado de `executar_sql`, `executar_graphql*` ou `analisar_dataframe` sem erro; ler o schema não conta), para uma falha de infraestrutura (API fora do ar, não configurada, timeout) não virar ficha; atualizar uma desta sessão sempre pode; título com cara de resultado numérico derruba a ficha, e outro campo com números é esvaziado (`_parece_valor`); id que não é desta sessão vira ficha nova; no máximo 3 por vez; sem `user_id` não há fichas. Uma contestação sem causa declarada vai para a ficha (`falhou`), e não para as lições.
  - **Embedding e busca.** Ao gravar, `fichas.salvar` calcula o embedding do título mais a intenção (`gemini-embedding-001`, 768 dimensões, tarefa `RETRIEVAL_DOCUMENT`, região `EMBEDDING_LOCATION`), e só recalcula se esses textos mudarem. Em `consultar_analista`, o pedido delegado ao analista (que já vem autocontido) é embeddado como `RETRIEVAL_QUERY`, e as fichas do domínio são comparadas por cosseno com `numpy`, sem FAISS (centenas ou milhares de vetores respondem em milissegundos). Entram as `MEMORIA_FICHAS_K` (padrão 5) mais parecidas acima de `MEMORIA_FICHAS_LIMIAR` (padrão 0,70; calibrado com poucos exemplos: a ficha certa ficou em torno de 0,80, as de outra análise entre 0,73 e 0,77, as sem relação abaixo de 0,62). A busca é feita uma vez por consulta, não a cada passo do analista.
  - **Como o analista lê as fichas.** O bloco diz que elas descrevem como análises parecidas foram feitas e avaliadas, sem números, e que valem menos que o pedido atual, o `dominio.md` e as lições. Explica a avaliação ("👍" é abordagem aprovada; "contestada e corrigida" quer dizer que o "Funcionou" já é a versão corrigida; "👎" é alerta, não receita; "sem avaliação" não confirma nem desmente) e o uso: evitar o que está em "Falhou", a não ser que o pedido atual peça; dizer na resposta qual escolha fez quando ela já foi contestada antes; e, como a ficha pode ser de um pedido só parecido, aproveitar o método e seguir o recorte do pedido atual. A tool do orquestrador repete a leitura da avaliação, mas não o modo de usar, que é de quem refaz a análise.
  - **De quem é a ficha.** O bloco traz o id do usuário atual e marca cada ficha como "sua" ou "de outro usuário (A)", "(B)"... O id real de outro usuário **nunca chega ao modelo**, então não há como ele revelá-lo; o prompt também manda não dizer quem é. Fichas de outros usuários podem ser aproveitadas e mencionadas ("uma análise parecida feita por outro usuário").
  - **Tool do orquestrador.** `buscar_analises_anteriores(consulta)` (categoria B2 do roteamento) devolve até 10 fichas parecidas, com a data da conversa nas do próprio usuário. Para "lembra da análise que fiz?" ou "alguém já analisou X?". As fichas não trazem números: para os valores, o orquestrador refaz a análise com `consultar_analista`.
  - **Sem embeddings** (Vertex indisponível), as fichas são gravadas sem vetor e a busca não devolve nada; o chat segue normal.
- **`SqliteStore` e threads.** O store usa uma conexão única e exige autocommit (`isolation_level=None`); o extrator e o chat leem e gravam o store em threads diferentes, então `_SqliteStoreSeguro` serializa as operações com um lock.
- **Shutdown.** O worker do `ReflectionExecutor` não é daemon: o `main.py` chama `memoria.encerrar()` no evento de shutdown, senão o processo não termina.
- **Logs.** Para cada extração, o log mostra o `raciocinio` do extrator (o motivo da decisão, inclusive a de não gravar nada), cada documento atualizado e cada item retirado. `MEMORIA_DEBUG=1` loga também o payload enviado ao extrator.
- **A conversa vale mais que a memória.** A extração roda em background, só 2 minutos depois do último turno da sessão (cada turno novo adia a anterior; `MEMORIA_DELAY_SEGUNDOS`), mas sem esperar quando já há 2 turnos ainda não extraídos (`MEMORIA_MAX_PENDENTES`; sem esse teto, uma conversa sem pausas adiaria a extração até os turnos saírem da janela de 6). Assim a pergunta, a contestação e a refeitura chegam juntas ao extrator, e durante uma conversa ativa a memória fica atrasada em relação ao que o usuário acabou de dizer. Os blocos injetados avisam isso ao modelo: se o usuário trocou, desfez ou contrariou algo na conversa, ou se o pedido atual diz o contrário, vale a conversa. O orquestrador vê o histórico (as últimas `MAX_INTERACOES` interações) e acata a correção. O analista não tem histórico: para ele vale o pedido delegado, que o orquestrador redige, e a memória só complementa.
- Sem `user_id` não há convenções, preferências, perfil nem fichas (só lições). Falha na extração ou na leitura nunca afeta a resposta do chat: tudo é try/except com log. A leitura é feita a cada chamada de LLM (consulta pequena ao SQLite), então uma lição nova vale já na próxima mensagem.

### Limites conhecidos

- Validada na stack do `.venv` (Python 3.11, `langchain-core 0.3.86`, `langchain-google-vertexai 2.1.2`) com o Gemini 3.8 Flash no Vertex: cenários de teste (erro de coluna, regra declarada com e sem lição existente, contestação com e sem indício, pedido pontual, preferência) e uma conversa real no app. Ainda são poucos cenários; vale revisar os documentos por amostragem nas primeiras semanas.
- O modelo trata as preferências como instruções, mas pode ignorá-las em parte.
- A fronteira entre "pedido" e "preferência declarada", como a que separa lição, convenção, preferência e o que vai para a ficha, depende do prompt do extrator (`INSTRUCOES`); em código há só as proteções descritas acima (filtro de números nas fichas, ficha nova só com consulta de dados, deduplicação entre destinos). Documentos gravados no schema antigo (campos `graficos_preferidos`, `formato_resposta`, `escopo_frequente`) não são mais lidos.
- As instruções do extrator têm exemplos de domínio fictícios (`producao`, `paradas`); trocar por casos reais do seu banco melhora o resultado.
- Os turnos pendentes ficam em memória: um restart do app antes da extração perde o turno.
- `langchain-google-vertexai` está fixado em `2.1.2` no `requirements.txt`. `tests/test_memoria_compat.py` (sem rede e sem LLM: `.venv/Scripts/python -m unittest discover -s tests`) trava as versões validadas do `langchain-google-vertexai` e do `langmem`, confere que o Vertex aceita o schema da saída estruturada do extrator, testa a mesclagem das mudanças e o bypass de `thought_signature`. Ao atualizar as bibliotecas, repita também uma conversa real com o Vertex.

---

## Streaming SSE — a interface que pensa em voz alta

O frontend não espera a resposta final. Cada etapa do processo chega em tempo real via **Server-Sent Events** (`GET /chat/stream`):

```text
→ {"type": "thinking", "text": "preciso verificar os dados de pedidos..."}
→ {"type": "tool",     "name": "executar_graphql", "label": "🗄️ Consultando banco de dados (GraphQL)"}
→ {"type": "tool",     "name": "analisar_dataframe", "label": "🔢 Processando e analisando dados"}
→ {"type": "reply",    "text": "## Pedidos por status\n\n![gráfico](/chart/...)\n\n..."}
```

| Evento | Campos | Quando |
| --- | --- | --- |
| `thinking` | `text` | Raciocínio do modelo ou frase que antecede uma tool call |
| `tool` | `name`, `label` | Tool chamada ou etapa de um pipeline (o `label` já vem no idioma ativo) |
| `artifact_preview` | `token`, `task_id` | O teste de um `task_code` gerou gráfico, PDF ou Excel — o preview aparece no chat |
| `artifact_saved` | `task_id` | O `save_task_code` salvou o código — o front troca o selo "Preview" por "✔ Salvo" em todos os previews daquela tarefa (o backend promove todos os artefatos de teste dela ao salvar). Não é emitido quando o salvamento é recusado |
| `widget_added`, `widget_updated`, `widget_deleted` | `widget_id` | O agente mexeu num painel — o dashboard se atualiza sem F5 |
| `reply` | `text`, `data` | Resposta final em markdown; os tokens `[chart:]`, `[pdf:]` e `[excel:]` já chegam resolvidos em URLs. `data` é verdadeiro quando o orquestrador chamou o analista no turno (resposta de dados: o front mostra os botões 👍 e 👎) |
| `cancelled` | — | O usuário parou o run |
| `error` | `text` | Erro irrecuperável |

O desafio aqui é que o sub-agente roda dentro de um `@tool` do orquestrador — fora do stream principal do LangGraph. A solução é um **side-channel por sessão**: uma `queue.Queue` por `session_id` captura os eventos do sub-agente em tempo real e os entrega ao gerador SSE que está servindo o frontend. Os pipelines de agendamento usam o mesmo canal para os eventos de etapa.

**Parar a resposta.** Threads Python não podem ser mortas e um `llm.invoke()` em andamento não é interrompível, então o cancelamento é cooperativo. Cada run de chat tem um `RunHandle` (`begin_run`) com um `threading.Event`, propagado por `ContextVar`; os nós do orquestrador, analista e scheduler o consultam antes e depois de cada chamada de LLM. No orquestrador, cancelar devolve um `AIMessage` sem `tool_calls` (evento SSE `cancelled`), o que mantém o histórico do checkpoint válido; nos grafos aninhados, lança `RunCancelled`, que o `ToolNode` converte em `ToolMessage`. Depois de cancelado, `_push_event` descarta eventos novos, para não poluir a queue do run seguinte. O front dispara o cancel fechando o `EventSource` e chamando `POST /chat/cancel`; o disconnect do SSE também cancela o run, como rede de segurança. Um novo run da mesma sessão espera o anterior terminar (até 45s) antes de tocar no checkpoint, no namespace de DataFrames e na queue. Se o cancelamento pegar um pipeline de criação de tarefa, o rascunho é descartado. Limite conhecido: a chamada de LLM ou o script `analisar_dataframe` em andamento termina antes de o cancelamento surtir efeito.

**Importante: SSE é só para o chat.** O resto do dashboard não é push-based — não há WebSocket nem um segundo canal SSE. O sino de alertas 🔔 faz **polling a cada 3s** (`AlertsService`, `GET /alerts`) e o badge da agenda a cada **5s** (`AgendaService`, `GET /tasks`), ambos via `setInterval`. O `AlertsService.pollFast()` acelera o polling (1,5s por 30s) logo depois de uma resposta do chat e de um ▶ manual, para que o alerta de um monitor recém-criado apareça em segundos. Os painéis do dashboard carregam uma vez ao abrir a página, de novo quando o usuário clica em atualizar na seção de painéis e quando chega um evento `widget_*`; não se auto-atualizam por tempo. Um novo desenvolvedor que for adicionar "tempo real" em outra parte do dashboard deve decidir entre reaproveitar esse padrão de polling (simples, já testado) ou abrir um canal push novo — hoje não existe abstração compartilhada para isso além da SSE do chat.

---

## Comando de voz — gravar, ouvir e enviar

O usuário pode interagir com o agente por voz diretamente no chat. Ele revisa o **áudio** antes de qualquer processamento:

```text
[🎤 gravar]  →  MediaRecorder (webm/opus, browser)
     ↓
[player de áudio]  →  usuário ouve, descarta ou envia
     ↓ envia
POST /chat/transcribe
     ↓
Gemini (Vertex AI) — transcrição literal, sem interpretação
     ↓
transcript aparece como mensagem do usuário no chat
     ↓
GET /chat/stream?message=transcript  →  agente processa normalmente
```

O usuário ouve a gravação e pode descartá-la antes de enviar. Depois do envio, porém, o fluxo é direto: o front transcreve o áudio, mostra a transcrição como mensagem do usuário e a envia ao agente na hora (`ChatService.sendAudio` seguido de `sendChat`) — não há etapa para editar o texto transcrito. A transcrição usa o mesmo modelo Gemini já configurado no projeto (`MODEL_NAME`, com raciocínio no nível mínimo aceito, `LOW`), sem dependência de um serviço de speech externo.

---

## Recortes de tela — perguntar sobre o que está no dashboard

O usuário pode selecionar uma área da tela (até 3 recortes, capturados no browser com `html-to-image`) e anexá-la à mensagem. Antes de cada envio, o front posta em `POST /chart-snapshot` um snapshot do estado do dashboard — alertas, artefatos, tarefas e as imagens (`images_b64`) — guardado por `session_id`. Se há imagens, `_build_human_message` monta uma mensagem multimodal; o orquestrador as trata como categoria E do roteamento e pode complementar com `get_dashboard_charts()`, que lê o restante do snapshot.

Os campos `charts` e `kpis` do snapshot seguem existindo no formato, mas o front de hoje os envia vazios: os painéis do dashboard não alimentam o snapshot.

---

## Agendamento — tarefas, relatórios e monitores

Uma **tarefa agendada** é um `task_code` Python — `def run(from_date, to_date, ctx)` — que o próprio agente escreve, testa e salva, e que o daemon executa depois, sem LLM no caminho. Há dois tipos de tarefa: **relatório** (gráfico, PDF ou Excel recorrente) e **monitor** (verifica uma condição e notifica quando ela é atingida).

### Criação e edição por conversa — um pipeline determinístico

Pedir "todo dia às 7h gere o relatório de pedidos em PDF" chama `criar_tarefa_agendada`; "mude o limite do monitor 003 para 50" chama `editar_tarefa_agendada`. Cada uma executa um grafo (`SchedulingGraph`) inteiro dentro da tool — o orquestrador não conduz os passos:

```text
criar:   criar_tarefa → consultar_dados → gerar_codigo ─┐
editar:  editar_codigo ─────────────────────────────────┤
                                                        ▼
                                  ┌──────────────► testar_codigo
                                  │                     │
                    corrigir_codigo ◄── erro (até 3×) ──┤
                                                        └── ok → verificar_analise
                                                                    │
              incluir_analise (até 2×, volta ao teste) ◄── falta ───┤
                                                                    └── ok → salvar_codigo → responder
```

| Nó | O que faz |
| --- | --- |
| `criar_tarefa` | Um LLM extrai os parâmetros do pedido (nome, frequência, horário, `date_range`, tipo, condição simples ou composta) e `schedule_task` / `schedule_monitor` cria o registro como `draft` — invisível no painel e ignorado pelo daemon. Monitor sem frequência explícita recebe `every_5m`; essa regra é imposta em código, não só no prompt, porque o LLM às vezes escolhia "diário" por conta própria |
| `consultar_dados` | O analista, em modo sandbox, lê dados reais e devolve `SKILL`, `QUERY`, `COLUNAS`, `AMOSTRA`, `FILTROS` e `TIPO_SAIDA`. Em monitores GraphQL ele resolve nomes citados pelo usuário (site, produto) para os IDs reais. Erros de exploração vão para `task_code_audit` |
| `gerar_codigo` | Um prompt dedicado recebe o pedido, o contexto coletado, as regras do sandbox e os exemplos do domínio. O código sai no idioma da tarefa. Para monitores, também devolve um bloco `interpretacao` (as escolhas que o pedido não especificava) e, nos simples em GraphQL, a `condition_sql` corrigida com IDs reais |
| `testar_codigo` | `test_task_code` executa no sandbox com a janela de datas da própria tarefa. O gráfico/PDF/Excel resultante aparece no chat como preview. Erros vão para `task_code_audit` |
| `corrigir_codigo` | Devolve o erro ao LLM, até 3 tentativas; remove `import`s de forma determinística |
| `verificar_analise` / `incluir_analise` | Se o pedido cita análise/insights mas o código não chama `ctx.analisar`, o relatório sairia sem o que foi pedido — não é erro de execução, é entrega faltando. Pede ao LLM, até 2 vezes, para incluí-la |
| `salvar_codigo` | `save_task_code` valida as regras de notificação, arquiva a versão anterior e ativa a tarefa. Um monitor recém-salvo ganha `next_run = agora`: a 1ª checagem acontece no próximo tick do daemon |
| `responder` | Monta a confirmação. Em falha, apaga o rascunho. Para monitores, anexa **"Como interpretei o pedido"** — condição, frequência, janela e as escolhas do gerador — que o orquestrador é instruído a repassar por inteiro, para o usuário corrigir uma premissa errada antes de descobrir pelo alerta |

Se o pipeline quebrar no meio, ou o usuário cancelar, o rascunho é apagado; um varredor do daemon remove também `draft`s órfãos com mais de 30 minutos. Listar, pausar, retomar e apagar tarefas passam por `gerenciar_agenda`, um sub-agente com as tools de `scheduler/tools.py` — que também sabe alterar campos como nome, frequência e e-mail.

### Execução — o daemon

O daemon vive no mesmo processo do FastAPI (`asyncio.create_task(scheduler_loop())` no startup) e a cada **5 s** (`SCHEDULER_TICK_SECONDS`) varre as tarefas `active` com `next_run` vencido, do domínio ativo. Cada execução:

1. Avalia a condição, se for um monitor simples (ver abaixo)
2. Marca a tarefa como `running` com `UPDATE ... WHERE status='active'` — o `rowcount` impede execução dupla
3. Calcula `from_date`/`to_date` a partir do `date_range` da tarefa (`ytd`, `mtd`, `today`, `last_Nd`) ou, sem ele, da janela padrão da frequência
4. Roda o `task_code` no sandbox, com `user_id`, `lang` e o nome da tarefa gravados na linha
5. Salva o relatório (ver abaixo), registra a execução em `task_runs` e agenda a próxima

Timeout de 10 minutos por execução. Em falha, a tarefa é reagendada com backoff de 5, 15 e 60 minutos, até `max_retries` (3); esgotado, vai para `status='error'` e espera revisão manual. Tarefas `once` terminam em `completed`; `on_demand` só rodam quando o usuário clica em ▶ (`POST /tasks/{id}/run-now`, em thread própria). Uma execução manual de monitor ignora o estado da condição para o usuário ver um resultado real, mas não dispara notificação se a condição não foi de fato atingida.

Cada gravação de `save_task_code` arquiva a versão anterior em `task_code_versions` (rollback por `restore_task_code_version`), e o painel de auditoria da UI lista os erros de geração de código (`task_code_audit`) do domínio.

### O sandbox do `task_code`

O código roda via `exec()` num namespace controlado (`scheduler/runner.py`): builtins por lista de permitidos, `import` rejeitado antes de compilar (só `time`, `math`, `json` e `re` já vêm injetados) e um conjunto fixo de variáveis pré-injetadas — `pd`, `np`, `plt`, `sns`, `openpyxl` (+ estilos), `date`/`datetime`/`timedelta`, `Counter`, `defaultdict`, `from_date`/`to_date` (objetos `date`) e `ctx`. O `ctx` (`TaskContext`) é a única porta para fora:

| Método | O que faz |
| --- | --- |
| `ctx.sql(query)` | `SELECT` no banco do domínio — só no modo `sql` |
| `ctx.graphql_table(...)`, `ctx.graphql_aggregate(...)` | Leitura via GraphQL — só no modo `graphql` |
| `ctx.api(path)` | `GET` na própria API, restrito a `/alerts` |
| `ctx.today()`, `ctx.date_range(days)` | Datas |
| `ctx.save_chart(fig)`, `ctx.generate_pdf(titulo, md)`, `ctx.generate_excel(df, nome)` | Geram o artefato e devolvem o token `[chart:]`, `[pdf:]` ou `[excel:]` |
| `ctx.save_excel(wb)`, `ctx.save_pdf(html)` | Controle total do formato (o `save_pdf` usa WeasyPrint, hoje fora do `requirements.txt`) |
| `ctx.analisar(fatos, foco, fallback)` | Análise narrativa de fatos já calculados (ver abaixo) |
| `ctx.notify(...)`, `ctx.alert(...)` | Alertas no sino (ver abaixo) |

O `run()` pode devolver os tokens ou deixar o `ctx` acumulá-los; uma execução que não produz nenhum artefato é erro.

### Monitores e alertas de threshold

O usuário pode pedir ao agente para monitorar qualquer condição de negócio: *"me avise se os pedidos do dia passarem de 160"* ou *"alerte quando JAG > 50 e BPL < 10"*. Há dois modos de monitor, com quem avalia a condição diferente:

| Modo | Quando | Quem avalia a condição | O `task_code` |
| --- | --- | --- | --- |
| **Simples** | Uma métrica, uma comparação | O **daemon**, antes de rodar o código, usando `condition_sql` + `condition_operator` + `condition_threshold` (`scheduler/condition.py`) | Só devolve o valor atual; `ctx.notify()` é recusado |
| **Composto** | E/OU, várias métricas ou entidades | O **próprio `task_code`**, que chama `ctx.alert(triggered, message)` uma vez por execução. A tarefa não tem `condition_sql`, só `condition_description` (o texto que o card mostra) | Obrigatoriamente chama `ctx.alert`; `ctx.notify()` é recusado |

Em ambos a notificação sai **apenas na transição** de "não atingida" para "atingida" (`condition_state` 0→1) e só rearma quando a condição deixa de valer — o sino não repete a cada verificação. `ctx.alert(False, ...)` é o jeito correto de dizer "sem dado": nunca assuma 0, que pode satisfazer um `<` e gerar alerta falso. `ctx.alert` é recusado em tarefas com `condition_sql`.

```python
def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(table="...", group_by="site_id", metric_field="...", metric_op="SUM",
                                 expand=["name"])
    por_site = {r["name"]: float(r["metricValue"]) for r in rows}
    jag, bpl = por_site.get("JAG"), por_site.get("BPL")
    if jag is None or bpl is None:
        ctx.alert(False, "")
        return "Sem dados de algum site monitorado"
    triggered = jag > 50 and bpl < 10
    ctx.alert(triggered, f"JAG {jag:.1f} > 50 e BPL {bpl:.1f} < 10", value=jag, threshold=50)
    return f"JAG {jag:.1f} | BPL {bpl:.1f} — {'ATINGIDA' if triggered else 'não atingida'}"
```

Em domínios `graphql`, a `condition_sql` de um monitor simples é uma spec JSON (`aggregate` ou `table`) executada por `scheduler/condition.py`; o valor comparado ao threshold é sempre `metricValue` (ou o `value_field` indicado). Outras regras: o menor intervalo aceito para uma tarefa `every_Xm` é de 5 minutos (`MIN_MONITOR_INTERVAL_MINUTES`). Quanto à janela de dados, o prompt de extração do pipeline manda usar `last_90d` num monitor cujo pedido não cita período — a mesma janela "últimos 3 meses" dos painéis padrão, para o alerta bater com o número que o usuário vê no gráfico, em vez de `today`, que só captura registros cujo período cobre o dia. Quando `schedule_monitor` é chamada direto, sem `date_range`, o padrão dela é `today`.

Uma tarefa de relatório com `notify=True` também avisa: ao fim de cada execução bem-sucedida o runner dispara uma notificação com o nome da tarefa, sem precisar de `ctx.notify()` no código.

O alerta é salvo em `threshold_alerts` com a mensagem e, se o código os informou, o valor observado e o de referência; a ligação com a tarefa vem do prefixo `daemon_<id>_` da sessão. O dashboard exibe os não lidos no sino 🔔 do header (`GET /alerts`, polling de 3 s), sem recarregar a página.

### Relatórios com análise narrativa — `ctx.analisar`

O `task_code` roda sem LLM, mas um relatório pode pedir uma análise escrita ("com insights", "analysis"). A divisão de trabalho é rígida: **o código calcula todos os números; o LLM só interpreta**. O código monta os `fatos` (totais, participações, razões, variações, já calculados) e chama `ctx.analisar(fatos, foco=..., fallback=...)`, que usa o papel "Relatório" dos LLMs. O texto devolvido passa por uma conferência: todo número citado precisa constar nos fatos (com tolerância de arredondamento). Se não constar, o LLM é chamado de novo uma vez, avisado dos números inventados; persistindo, ou em caso de timeout (150 s) ou falha, o `fallback` determinístico escrito pelo próprio código entra no lugar — o relatório sai de qualquer forma. São no máximo 3 chamadas por execução. Quando o pedido cita análise e o código gerado não usa `ctx.analisar`, o pipeline de criação percebe e corrige (`verificar_analise`); se mesmo assim falhar, a resposta avisa o usuário.

### Saída dos relatórios agendados e integração com e-mail

<!-- TODO(email): o envio por e-mail está previsto. Quando existir, reescrever esta seção (hoje ela
     diz que o envio não é feito por este sistema) e o item sobre `reports/` em "Limitações conhecidas". -->

Quando o daemon executa uma tarefa no horário programado:

1. Roda o `task_code` e coleta os tokens dos artefatos produzidos (PDFs, gráficos, Excels), que ficam em `mfg.db`, acessíveis em `/pdf/{id}`, `/chart/{id}` e `/excel/{id}`, e aparecem no painel de artefatos com a origem `task`
2. Grava um diretório `reports/<id>_<nome>_<data>/` com `content.md` (o resultado, com os tokens de PDF resolvidos para URLs completas, via `BACKEND_URL`) e `metadata.json`
3. O `metadata.json` traz `status: pending_send`, a recorrência e — só se a tarefa tiver um destinatário — o bloco do e-mail (destinatário, assunto, corpo no idioma da tarefa, links dos anexos); sem destinatário, esse bloco é `null`

O relatório fica disponível localmente (`GET /reports/{task_id}`), e o metadado estruturado permite integração com qualquer serviço de envio de e-mail externo sem acoplar o daemon a um servidor SMTP específico — **o envio em si não é feito por este sistema**.

Um detalhe a ter em mente: o destinatário (`scheduled_tasks.email`) só é gravado por `schedule_task` e `update_scheduled_task`, ou seja, via `gerenciar_agenda`. O pipeline de criação (`criar_tarefa_agendada`) não extrai endereço de e-mail do pedido, então uma tarefa criada por ele nasce sem destinatário até que alguém o defina depois. O card da tarefa mostra o e-mail quando existe.

---

## Gráficos, PDFs e Excels como artefatos persistentes

Quando o sub-agente (ou um `task_code`) gera um gráfico matplotlib, ele não trafega pelo contexto do LLM. O que acontece:

1. O script salva `result = fig` (ou chama `ctx.save_chart(fig)`)
2. A tool serializa para PNG e armazena no banco com um UUID
3. O modelo recebe apenas o token `[chart:3f8a1b...]`
4. O FastAPI troca o token por uma imagem (`/chart/{id}`) na resposta, e o frontend a renderiza

O mesmo padrão se aplica a PDFs gerados por `gerar_pdf()` (que embute os gráficos referenciados por token) e a planilhas geradas por `gerar_excel()`. Para o Excel, o orquestrador recebe o token `[excel:3f8a1b...]`, que o FastAPI resolve em um link de download `/excel/{id}` — o usuário clica e baixa o `.xlsx` diretamente.

Os artefatos ficam em `mfg.db` — o banco funciona como object storage local — até serem apagados, pelo painel de artefatos da UI ou junto com a conversa que os gerou. O painel (`GET /artifacts`) mostra a origem de cada um: `chat`, ou `task` quando a sessão tem o prefixo `daemon_<id>_`. Os previews gerados durante o teste de um `task_code` ficam numa sessão `test_<id>_...`, que o front esconde da lista; quando o código é salvo, esses artefatos são promovidos para a sessão real do chat.

---

## Painéis do dashboard — padrão do domínio e customizados

O dashboard é uma grade de painéis (gráficos Chart.js), de dois tipos. Em ambos, **o que existe é código-fonte que gera a configuração do gráfico** — o `<canvas>` já está fixo no `WidgetCardComponent` (Angular); não há HTML gerado pelo agente. Isso difere dos gráficos do chat, que são imagens PNG estáticas.

### Os dois tipos

| | Padrão do domínio | Customizado |
| --- | --- | --- |
| Onde mora | `config/<slug>/<lang>/dashboard_default.py` (lista `WIDGETS`) | Tabela `dashboard_widgets` em `mfg.db` |
| Quem cria | Quem mantém o pack, versionado no git | O usuário, pedindo ao agente para fixar um gráfico |
| Visível para | Todos os usuários | O dono e, se `shared = 1`, todos |
| Remoção | Não pode (`DELETE` devolve 400) | Só o dono |
| Id na API | `default:<id>` | UUID |

Os padrão nunca tocam o banco: sobrevivem a resets do `mfg.db`, são recarregados quando o arquivo muda e existem uma versão por idioma. Cada item é um dict `{id, title, description, code, range_unit, range_value}`.

### O que é salvo num painel customizado (`dashboard_widgets` em `mfg.db`)

```sql
CREATE TABLE dashboard_widgets (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT,
    code         TEXT NOT NULL,   -- código-fonte Python, NÃO o gráfico renderizado
    created_at   TEXT NOT NULL,
    user_id      TEXT,
    dominio      TEXT,
    range_unit   TEXT NOT NULL DEFAULT 'days',   -- período pretendido: 'days' | 'months'
    range_value  INTEGER NOT NULL DEFAULT 7,
    shared       INTEGER NOT NULL DEFAULT 0      -- 1 = visível para todos os usuários do domínio
);
```

A coluna `code` guarda uma função Python (`def run(from_date, to_date, ctx): ...`) escrita pelo LLM — nunca o JSON Chart.js pronto, nunca uma imagem. `range_unit`/`range_value` guardam o período que o usuário pediu ao criar o painel (ex: "últimos 30 dias", "últimos 3 meses") como dado estruturado, não embutido no código.

### Onde o JSON é gerado — sandbox, sob demanda, a cada refresh

```text
Criação/edição do painel (via chat, uma única vez)
  Usuário: "adiciona ao dashboard as vendas por produto dos últimos 30 dias"
    └── Orquestrador confirma a intenção ("Vou criar e fixar um painel ... Confirma?")
    └── consultar_analista(para_agendamento=True)   → descobre consulta e colunas reais
    └── LLM escreve code = "def run(from_date, to_date, ctx): ..."
          └── test_widget_code(code)              → valida em sandbox (scheduler/widget_runner.py)
          └── add_chart_to_dashboard(code, range_unit="days", range_value=30)
                ├── valida de novo e faz INSERT em dashboard_widgets
                ├── coloca o painel na 1ª posição da ordem do usuário
                └── emite o evento SSE widget_added

Refresh do dashboard (página carrega, usuário clica em atualizar na seção de painéis,
ou chega um evento widget_*)
  WidgetsService (Angular) → GET /dashboard-widgets/{id}/data?lang=...
    └── main.py lê o `code` (do arquivo default ou da coluna) + range_unit/range_value
          └── compute_widget_range(range_unit, range_value)   → calcula from_date/to_date
                a partir de date.today() NAQUELE INSTANTE (scheduler/widget_runner.py)
          └── run_widget_code(code, from_date, to_date, session_id)
                └── exec() do `code` num sandbox restrito (mesmo padrão do task runner):
                      imports bloqueados exceto uma lista curta (pandas, numpy, datetime, math, re, json, ...);
                      variáveis pré-injetadas: pd, np, ctx, from_date, to_date, date, datetime, timedelta
                └── run(from_date, to_date, ctx) executa consultas reais (ctx.sql ou ctx.graphql_*)
                      contra o banco de domínio NAQUELE INSTANTE
                └── retorna um dict Chart.js validado: {"type", "data": {"labels","datasets"}, "options"?}
    └── response JSON volta para o browser (nunca é gravado de volta no banco)
```

Esse JSON é **efêmero**: existe só na resposta HTTP daquela requisição. Não há tabela nem cache que guarde o resultado — a próxima chamada a `/dashboard-widgets/{id}/data` reexecuta tudo do zero. Duas diferenças em relação ao sandbox de tarefas: aqui `from_date`/`to_date` chegam como **strings ISO** (nas tarefas são objetos `date`) e o conjunto de variáveis é menor (sem `plt`, `sns` nem `openpyxl`).

Tipos aceitos: `bar`, `line`, `pie`, `doughnut`, `radar`, `polarArea`, `scatter`, `bubble` e `matrix`. O `matrix` (heatmap) tem payload próprio — `{label, xLabels, yLabels, points: [{x, y, v}]}` — porque a cor de cada célula depende do mínimo e do máximo do conjunto inteiro, o que só o cliente calcula.

### Onde e como o front renderiza — Chart.js client-side, sem HTML injetado

Cada painel é um `WidgetCardComponent` (Angular) com um `<canvas>` no seu template. O ponto não-óbvio: as instâncias do **Chart.js** não pertencem ao componente — elas ficam centralizadas dentro do `WidgetsService` (`frontend/src/app/core/services/widgets.service.ts`), num `Map<widgetId, Chart>`, no mesmo espírito do dict global `_widgetCharts` que existia no `dashboard.html` original. `WidgetCardComponent.ngAfterViewInit()` só registra seu `<canvas>` no serviço (`registerCanvas(id, canvas)`); é o serviço que decide quando criar/recriar/destruir o gráfico:

```ts
new Chart(canvas, { type: config.type, data: config.data, options: {...} });
```

Essa centralização existe por um motivo concreto: os eventos SSE do chat (`widget_added`, `widget_updated`, `widget_deleted`, ver [Streaming SSE](#streaming-sse--a-interface-que-pensa-em-voz-alta)) chegam no `ChatService`, que não tem — e não deveria precisar ter — uma referência ao componente visual de um widget específico. Em vez disso, `ChatService` chama `WidgetsService.onWidgetAdded()` / `onWidgetUpdated(id)` / `onWidgetDeleted(id)` diretamente; o serviço recarrega a lista, busca o `/dashboard-widgets/{id}/data` de novo e redesenha (ou destrói) a instância Chart.js correspondente — sem precisar dar F5, e sem o componente precisar "escutar" nada.

### Ordem e compartilhamento

Os cards se reordenam por drag-and-drop. A ordem é salva **no servidor**, por usuário e domínio (`PUT /dashboard-widgets/order` → `dashboard_widget_order`), então acompanha o usuário em qualquer navegador. Ao ler, o backend a **reconcilia** com os painéis existentes: ids salvos que sumiram (painel removido do arquivo default ou apagado) são descartados e painéis novos entram no fim, de modo que o drag-and-drop nunca quebra quando o conjunto muda. Um painel recém-criado via chat é a exceção — entra sempre na primeira posição. O dono de um painel customizado alterna o compartilhamento em `PATCH /dashboard-widgets/{id}/share`.

### Regra crítica sobre datas — por que `range_unit`/`range_value` existem

Antes de existirem essas duas colunas, `from_date`/`to_date` eram sempre calculados como "hoje − 7 dias" fixo, e o único jeito de um painel mostrar um período diferente era o próprio `code` recalcular a data internamente (`date.today() - timedelta(...)`), ignorando os parâmetros recebidos — uma convenção sustentada só por instrução de prompt, sem garantia estrutural. Isso ainda funciona (o LLM foi instruído a seguir o padrão), mas o comportamento correto e a fonte de verdade agora são `range_unit`/`range_value`: o backend calcula `from_date`/`to_date` certos a partir deles antes de chamar `run()`, e o `code` gerado deve apenas usar o que recebeu — não recalcular nada. (Os painéis padrão do domínio podem ignorar a janela de propósito, quando a tabela tem vigência por registro; aí `range_unit`/`range_value` viram só um rótulo.)

---

## Referência de rotas HTTP

Toda a API vive num único `FastAPI` (`main.py`), sem prefixo/versionamento (`/chat`, não `/api/v1/chat`). Rotas que isolam dado por usuário aceitam `user_id` como **query param opcional** — ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada) para a semântica de quando ele falta. Rotas que servem conteúdo em idioma aceitam `lang` (ver [Idioma](#idioma-i18n--ui-conteúdo-de-domínio-e-respostas)).

### Chat e voz

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/chat` | POST | Envia uma mensagem e espera a resposta completa (sem streaming) — `{message, session_id, user_id, lang}` |
| `/chat/stream` | GET (SSE) | Mesma coisa, mas via Server-Sent Events — `message`, `session_id`, `user_id`, `lang`. É o que o frontend usa de fato |
| `/chat/cancel` | POST | Botão "parar": sinaliza cancelamento cooperativo do run em andamento — `{session_id}` |
| `/chat/start-task-creation` | POST | Abre uma sessão já com o turno inicial de agendamento, sem chamar o LLM — `{session_id, lang}` |
| `/chat/start-task-edit` | POST | Abre uma sessão já com o resumo da tarefa e a pergunta de edição, sem chamar o LLM — `{session_id, task_id, lang}` |
| `/chat/history` | GET | Histórico de mensagens (Human/AI) de um `session_id`, com o raciocínio reconstruído. Cada resposta traz `turn` (índice da mensagem do usuário), `data` (resposta de dados) e `confirmed` (já confirmada pelo botão) |
| `/chat/sessions` | GET | Lista as conversas de um `user_id` (título + contagem de mensagens), filtradas pelo domínio ativo |
| `/chat/sessions/{session_id}` | DELETE | Apaga uma conversa, os artefatos (charts/pdfs/excels) e as avaliações, os resumos de abordagem e as fichas de análise associados a ela |
| `/chat/transcribe` | POST | Recebe áudio (webm/opus) e devolve a transcrição via Gemini |
| `/chat/feedback` | POST | Botões 👍 e 👎 de uma resposta de dados — `{session_id, user_id, reply_start, rating}`, com `rating` `up` ou `down`. `reply_start` é o começo normalizado do texto da resposta (`feedback.inicio_comparavel`). A sessão tem que ser do `user_id`. Há uma nota por resposta: trocar de nota atualiza a mesma linha. Devolve a nota anterior e `memoria`: `agendada` (o turno estava em memória e a extração foi reagendada com o sinal) ou `so_registrada` (o turno é antigo ou o app reiniciou: fica só no banco) |
| `/chart-snapshot` | POST | O front envia, antes de cada mensagem, alertas, artefatos, tarefas e recortes de tela (`images_b64`) — lido por `get_dashboard_charts()` e pela mensagem multimodal |

### Artefatos (gráficos, PDF, Excel)

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/chart/{chart_id}` | GET | Serve o PNG de um gráfico gerado pelo agente |
| `/pdf/{pdf_id}` | GET | Download do PDF gerado |
| `/excel/{excel_id}` | GET | Download da planilha gerada |
| `/artifacts` | GET | Lista os artefatos (chart/pdf/excel) de um `user_id`, com origem (`chat` ou `task`) |
| `/artifacts/{type}/{id}` | DELETE | Apaga um artefato específico (`type` = `chart`, `pdf` ou `excel`) |

### Agenda (tarefas agendadas)

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/tasks` | GET | Lista as tarefas de um `user_id`, com as últimas 5 execuções de cada |
| `/tasks/{id}/runs` | GET | Histórico completo de execuções de uma tarefa |
| `/tasks/{id}/run-now` | POST | Dispara execução imediata (só funciona se `status=active`) |
| `/tasks/{id}/toggle-pause` | POST | Alterna `active` ↔ `paused` (ao retomar, recalcula o `next_run`) |
| `/tasks/{id}/toggle-notify` | POST | Liga/desliga notificações da tarefa (bloqueado se ela tiver condição de threshold, simples ou composta) |
| `/tasks/{id}` | DELETE | Remove a tarefa |
| `/tasks/{id}/code` | GET | Código atual (`task_code`) + histórico de versões |
| `/tasks/{id}/code-audit` | GET | Log de erros durante a geração/correção automática do código da tarefa |
| `/tasks/code-audit/summary` | GET | Resumo agregado de erros de geração de código do domínio ativo (últimos 200) |
| `/tasks/code-audit/{id}` | DELETE | Apaga uma entrada de auditoria |
| `/tasks/code-audit` | DELETE | Apaga toda a auditoria do domínio ativo |
| `/reports/{task_id}` | GET | Lista os relatórios gerados por execuções passadas dessa tarefa (data, status e URLs dos PDFs) |

### Painéis do dashboard

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/dashboard-widgets` | GET | Lista os painéis na ordem salva do usuário: padrão do domínio (`is_default`) mais os customizados visíveis (`shared`, `is_owner`). Aceita `lang` |
| `/dashboard-widgets/order` | PUT | Salva a ordem dos painéis — `{user_id, widget_ids}` |
| `/dashboard-widgets/{id}/share` | PATCH | Compartilha ou revoga o compartilhamento de um painel customizado (só o dono) — `{user_id, shared}` |
| `/dashboard-widgets/{id}/data` | GET | Reexecuta o `code` do painel e devolve a config Chart.js atualizada — é o que o dashboard chama a cada refresh. Aceita `lang` |
| `/dashboard-widgets/{id}` | DELETE | Remove um painel customizado (painéis padrão devolvem 400) |

### Alertas

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/alerts` | GET | Alertas de threshold; por padrão só não-lidos, `?all=true` traz os últimos 100 |
| `/alerts/{id}/read` | POST | Marca um alerta como lido |
| `/alerts/read-all` | POST | Marca todos como lidos |
| `/alerts/{id}` | DELETE | Apaga um alerta |
| `/alerts` | DELETE | Apaga todos os alertas |

### Diversos

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/app-info` | GET | `{nome, descricao, configurado}` do pack ativo — usado pelo dashboard para branding e como sonda de saúde |
| `/` | GET | Serve o `index.html` do build Angular (`frontend/dist/frontend/browser/`) — a SPA inteira |
| `/{arquivo estático do build}` | GET | JS/CSS com hash, `favicon.ico`, `i18n/<lang>.json` etc. — mount `StaticFiles` do build Angular, registrado por último para não sombrear as rotas de API acima |
| `/static/*` | GET | Assets estáticos legados — hoje só `images/logo.png`, usado pelo header |
| `/docs`, `/redoc` | GET | Swagger/Redoc gerados automaticamente pelo FastAPI |

---

## Transporte, CORS e autenticação

Pontos que um novo desenvolvedor precisa saber antes de expor isso além do `localhost`:

- **HTTP puro.** O `uvicorn` (ver `run.py`) não termina TLS — ele serve HTTP simples na porta 8000. Em produção, HTTPS é responsabilidade da camada na frente do app (reverse proxy/PaaS) — a aplicação em si não sabe nem precisa saber que existe TLS.
- **CORS totalmente aberto, mas só relevante para consumidores externos.** `CORSMiddleware` está configurado com `allow_origins=["*"]` e `allow_methods=["GET", "POST"]` (`main.py`) — note que `DELETE`, `PUT` e `PATCH` não estão liberados. Isso não afeta o dashboard Angular porque ele é servido **same-origin** (o próprio FastAPI serve o `index.html` na raiz — ver [Frontend](#frontend--angular-19-standalone-components)), então suas chamadas `DELETE`, `PUT` e `PATCH` nunca passam por preflight de CORS. Só importa se algum dia um cliente rodando em outra origem precisar chamar essas rotas.
- **Sem autenticação na API.** Não há API key, JWT nem sessão de servidor — nenhuma rota valida quem está fazendo a chamada. O único controle de acesso é a separação de dados por `user_id` (ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada)), e esse `user_id` é um query param em texto puro, não assinado — qualquer cliente que descubra/adivinhe o `user_id` de outra pessoa consegue ler os dados dela. O modelo de segurança inteiro depende do app host controlar quem recebe qual `user_id`.
- **Algumas rotas nem aplicam o filtro por `user_id`.** Quem souber o identificador acessa: `/chat/history` e `DELETE /chat/sessions/{id}` (por `session_id`), `/chart/{id}`, `/pdf/{id}` e `/excel/{id}` (por UUID), `/dashboard-widgets/{id}/data`, `/reports/{task_id}` e as rotas `/tasks/code-audit*`, que operam sobre o domínio inteiro.
- **O sandbox é um filtro, não um isolamento.** O `task_code` e o `widget_code` rodam via `exec()` no próprio processo do servidor, com builtins por lista de permitidos e imports bloqueados. Isso contém os erros comuns de código gerado por LLM, mas não é uma fronteira de segurança contra código malicioso — por isso o código executado vem do próprio agente do sistema, nunca de entrada de terceiros.

---

## Por que isso importa para a sua fábrica

A combinação desses elementos resolve os problemas reais de quem quer colocar IA generativa num ambiente industrial:

| Problema | Solução nesta arquitetura |
| --- | --- |
| "O modelo inventa dados" | O analista só responde com resultados de `analisar_dataframe` — sem dados, sem resposta — e a análise narrativa dos relatórios é conferida número a número contra os fatos calculados pelo código |
| "A tela trava esperando a resposta" | Streaming SSE com thinking e tool calls em tempo real, e botão de parar |
| "Não consigo analisar mês inteiro, é muita coisa" | Namespace incrementa análises em etapas, contexto protegido por limites de exibição |
| "O agente esquece o que foi dito antes" | Checkpointer SQLite persiste histórico por session_id |
| "Como conecto outro banco ou API sem mexer no agente?" | Um pack em `config/` (`settings.yml` + `dominio.md` por idioma), em modo SQL ou GraphQL — nenhuma linha de código muda |
| "O modelo fica confuso com muitas APIs ao mesmo tempo" | Catálogo de skills com lazy-loading; só o schema do domínio ativo entra no prompt do analista |
| "Preciso exportar os dados para o Excel" | `gerar_excel()` cria um `.xlsx` formatado e disponibiliza link de download direto no chat |
| "Preciso atender outro cliente/domínio sem duplicar o projeto" | Trocar `config/settings.yml` + o pack do domínio reaponta o sistema inteiro — ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código) |
| "Preciso plugar isso num app que já tem usuários e login" | O host passa `?user_id=...`; o sistema não reimplementa autenticação, só separa tarefas, painéis, alertas e artefatos por esse `user_id` — ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada) |
| "Meu time fala idiomas diferentes" | Interface, respostas e conteúdo de domínio em português, inglês e chinês, escolhidos por requisição — ver [Idioma](#idioma-i18n--ui-conteúdo-de-domínio-e-respostas) |
| "Quero ser avisado sem ficar olhando o dashboard" | Monitores de threshold (simples ou compostos) e relatórios agendados, gerados por conversa e executados sem LLM no caminho |

---

## Instalação e execução

### Pré-requisitos

- Python 3.11 — é a versão do `.venv` do projeto, criado com [uv](https://docs.astral.sh/uv/). No Python 3.14 o `requirements.txt` não resolve: o pip não encontra uma versão do `google-cloud-aiplatform` compatível com o `google-genai==2.8.0` fixado. As versões 3.12 e 3.13 não foram testadas.
- Node.js 20+ e npm, para o frontend Angular (ver [Frontend](#frontend--angular-19-standalone-components))
- Um projeto Google Cloud com Vertex AI habilitado, se for usar o chat IA (`PROJECT_ID`)
- Credencial do endpoint GraphQL do domínio (`GRAPHQL_TOKEN`, `NEW_AGE_API_KEY`...), se o pack ativo for `graphql`

### Setup

```bash
uv venv --python 3.11 .venv
uv pip install -r requirements.txt   # o uv usa o .venv da pasta atual

cd frontend && npm install && cd ..
```

Para rodar, use o Python do ambiente (`.venv\Scripts\python run.py` no Windows, `.venv/bin/python run.py` no Linux/Mac) ou ative o `.venv` antes. Sem uv, `python -m venv .venv` e `pip install -r requirements.txt` também funcionam, desde que com Python 3.11. O `.venv` criado pelo uv não traz `pip`: instale pacotes nele com `uv pip`.

### Configurar o pack de domínio

Ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código) — editar `config/settings.yml` (nome do app, domínio ativo, `database.modo` e a conexão correspondente) e garantir que existe `config/<slug>/<idioma>/dominio.md` para o domínio escolhido. Sem isso, `db_config.is_configured()` retorna falso e o chat funciona sem análise de dados.

### Variáveis de ambiente

As variáveis podem vir do ambiente ou de um arquivo `.env` na raiz (carregado no startup).

| Variável | Papel | Default |
| --- | --- | --- |
| `DATABASE_URL` | Sobrepõe `database.url` do `settings.yml` (modo `sql`) — tem precedência total | — |
| `GRAPHQL_TOKEN`, `NEW_AGE_API_KEY`... | Segredos referenciados por `${VAR}` no bloco `graphql` do `settings.yml`; o nome depende do domínio | — |
| `CONFIG_DIR` | Pasta do pack de configuração, se não for `config/` na raiz | `config/` |
| `DB_PATH` | Caminho do banco local do app (`mfg.db`) — em produção pode apontar para uma pasta de dados fora do código, ex. `/data/mfg.db` | `<raiz do projeto>/mfg.db` |
| `PROJECT_ID` | Projeto GCP para o Vertex AI — sem ele o chat IA fica desabilitado | — |
| `LOCATION` | Região do Vertex AI | `us-central1` |
| `MODEL_NAME` | Modelo Gemini usado por todos os papéis do agente e pela transcrição | `gemini-3.8-flash` |
| `THINKING_LEVEL_ANALISTA`, `THINKING_LEVEL_ORQUESTRADOR`, `THINKING_LEVEL_ORQUESTRADOR_ROTEAMENTO`, `THINKING_LEVEL_RELATORIO` | Nível de raciocínio por papel: `low`, `medium` ou `high` (ver [LLMs por papel](#llms-por-papel)) | Padrão do modelo (relatório: `medium`) |
| `GOOGLE_CREDENTIALS_JSON` | Credenciais de service account em base64 (alternativa a `credentials.json` na raiz) | — |
| `BACKEND_URL` | URL pública do backend, usada para montar links de artefatos nos relatórios | `http://localhost:8000` |
| `MAX_INTERACOES` | Janela de interações recentes enviada ao modelo pelo checkpointer | `10` |
| `SCHEDULER_TICK_SECONDS` | Intervalo do loop do daemon de agendamento | `5` |
| `MIN_MONITOR_INTERVAL_MINUTES` | Menor intervalo aceito para um monitor (`every_Xm`) | `5` |
| `THINKING_LEVEL_MEMORIA` | Nível de raciocínio do extrator da memória, que roda em background (ver [LLMs por papel](#llms-por-papel)) | `medium` |
| `MEMORIA_ATIVA` | `0` desliga a leitura e a extração da memória do agente | `1` |
| `MEMORIA_DEBUG` | `1` loga o payload enviado ao extrator da memória | `0` |
| `MEMORIA_DELAY_SEGUNDOS` | Espera depois do último turno da sessão antes de extrair | `120` |
| `MEMORIA_MAX_PENDENTES` | Turnos ainda não extraídos a partir dos quais a extração roda sem esperar | `2` |
| `EMBEDDING_MODEL` | Modelo de embeddings do Vertex usado pelas fichas de análise | `gemini-embedding-001` |
| `EMBEDDING_LOCATION` | Região do Vertex para os embeddings (o `LOCATION` do chat pode ser `global`) | `us-central1` |
| `MEMORIA_FICHAS_K` | Quantas fichas parecidas entram no prompt do analista | `5` |
| `MEMORIA_FICHAS_LIMIAR` | Similaridade mínima (cosseno) para uma ficha entrar | `0.70` |
| `DEBUG_TOOLS` | `1` loga o payload completo de cada tool call do agente | `0` |
| `DEBUG_TOOLS_TRUNCATE` | Com `DEBUG_TOOLS=1`, trunca cada campo logado nesse nº de caracteres (0 = não trunca) | `0` |

### Rodar

**Desenvolvimento** (dois processos em paralelo, cada um no seu terminal):

```bash
python run.py            # backend — http://localhost:8000
# equivalente a: uvicorn main:app --reload --host 0.0.0.0

cd frontend && npm start # frontend — http://localhost:4200, com proxy para o backend acima
```

Acesse `http://localhost:4200` durante o desenvolvimento — é o Angular com hot-reload. `http://localhost:8000` sozinho só serve o frontend se `npm run build` já tiver sido rodado (ver [Frontend](#frontend--angular-19-standalone-components)).

**"Produção local"** (um único processo):

```bash
cd frontend && npm run build && cd ..
python run.py
```

Acesse `http://localhost:8000` — o próprio FastAPI serve o Angular já compilado. Docs automáticas do FastAPI em `/docs` (Swagger) e `/redoc`.

No startup, `db.py` cria o `mfg.db` (se não existir) e aplica as migrações aditivas das tabelas do app; as demais tabelas nascem quando o agente sobe. Não há carga de dados de demonstração — isso é independente do banco de domínio configurado em `config/`.

---

## Frontend — Angular 19, standalone components

O frontend vive em `frontend/` (workspace Angular separado do backend Python) e é uma SPA sem rotas — não há Angular Router, porque não existe hoje nenhuma view endereçável por URL: tudo é painel/slide-over/modal sobre uma única tela, exatamente como no `dashboard.html` que ela substituiu. Standalone components (padrão do Angular 19, sem `NgModule`) e **signals** no lugar de `RxJS`/`Observable` para todo o estado local — a única exceção é `ChatStreamService`, que envolve o `EventSource` do SSE num `Observable` porque é o formato que a API do browser expõe.

```text
frontend/
  public/i18n/          # <lang>.json — tradução da interface (en, pt-BR, zh)
  src/app/
    core/services/      # UserSessionService, LocaleService, TranslateService, ChatService,
                        # ChatStreamService, AlertsService, AgendaService, ArtifactsService,
                        # WidgetsService, AuditService, ScreenshotService, VoiceRecorderService, ...
    core/models/        # interfaces TypeScript dos payloads da API
    core/pipes/         # TranslatePipe
    shared/components/  # modais reaproveitados entre features (ChartModal, TaskCodeModal, RunErrorModal, ...)
    features/           # chat/, header/, agenda/, alerts/, audit/, artifacts/, widgets/
    app.component.ts    # shell: header + painéis + grade de widgets + chat sidebar
```

Cada `feature` segue o mesmo padrão: um `*Service` (`providedIn: 'root'`, injetável, dono do estado e das chamadas HTTP) e um ou mais componentes "burros" que só leem signals do serviço e disparam métodos nele — nenhum componente chama `HttpClient` diretamente.

O que cada área da tela faz:

| Área | Papel |
| --- | --- |
| Header | Branding (`/app-info`), status da API, relógio, seletor de usuário demo, seletor de idioma, atalhos para os painéis |
| Chat (sidebar) | Conversa, lista e seleção de conversas, voz, recortes de tela, botão de parar e, em cada resposta de dados, os botões 👍 e 👎 (ver [Memória do agente](#memória-do-agente--o-que-ele-aprende-entre-sessões)) |
| Painéis do dashboard | Grade de gráficos Chart.js, com drag-and-drop e compartilhamento |
| Agenda | Cards das tarefas: executar agora, pausar, notificações, ver código, editar via chat, histórico de execuções e erro de cada uma |
| Alertas | Lista de alertas de threshold, marcar como lido, apagar |
| Artefatos | Gráficos, PDFs e Excels gerados, com a origem de cada um |
| Auditoria | Erros de geração de `task_code` do domínio, com o código que falhou |

Identidade (`UserSessionService.userId`), sessão (`UserSessionService.sessionId`, um `signal`) e idioma (`LocaleService.lang`) — ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada) e [Idioma](#idioma-i18n--ui-conteúdo-de-domínio-e-respostas) — persistem em `localStorage`, com o mesmo comportamento: o `?user_id=` / `?lang=` do host sempre vence sobre o valor salvo.

### Rodando em desenvolvimento

```bash
cd frontend
npm install
npm start            # = ng serve — sobe em http://localhost:4200
```

`ng serve` usa `frontend/proxy.conf.json` para redirecionar `/chat`, `/tasks`, `/alerts`, `/artifacts`, `/dashboard-widgets`, `/reports`, `/app-info`, `/chart`, `/pdf`, `/excel`, `/chart-snapshot` e `/static` para o backend FastAPI local (`http://localhost:8000`, precisa estar rodando via `python run.py` em paralelo). Isso torna o dev server **same-origin** do ponto de vista do browser — sem isso, chamadas `DELETE`, `PUT` e `PATCH` esbarrariam no `allow_methods=["GET", "POST"]` do CORS (ver [Transporte, CORS e autenticação](#transporte-cors-e-autenticação)). As traduções (`/i18n/<lang>.json`) o próprio dev server serve, por serem assets do Angular. **Rota nova no backend precisa de uma entrada nesse arquivo**, senão o dev server a responde com o `index.html`.

### Build de produção e como o FastAPI serve o resultado

```bash
cd frontend
npm run build         # = ng build — gera frontend/dist/frontend/browser/
```

Em produção, `main.py` **não roda o Angular** — ele só serve os arquivos estáticos já compilados:

- `GET /` devolve `frontend/dist/frontend/browser/index.html` (`FileResponse`)
- Um `StaticFiles` é montado em `/` para os demais arquivos do build (JS/CSS com hash, `favicon.ico`, `i18n/`) — **registrado por último no arquivo**, depois de todas as rotas de API, para que o Starlette resolva as rotas explícitas (`/chat`, `/tasks`, etc.) antes de cair no mount

Isso significa: **rodar `python run.py` sem antes rodar `npm run build` serve uma tela em branco** (o backend loga um warning e a rota `/` devolve um `index.html` inexistente). O build gera `frontend/dist/frontend/browser/`, que o FastAPI serve; em produção só esse resultado é necessário, não o Node.

---

## Limitações conhecidas — estado em memória, processo único

Vários pontos do sistema guardam estado em `dict`s Python globais dentro do processo do `uvicorn`, não em algo externo (Redis, banco). Isso é simples e suficiente para **um único processo/worker**, mas é uma armadilha real se alguém tentar escalar horizontalmente (`--workers N` do uvicorn, múltiplas réplicas atrás de um load balancer) sem revisar esse ponto primeiro:

| Estado | Onde vive | Por que importa |
| --- | --- | --- |
| Namespace Python por sessão (`_namespaces`) | `agent_multi.py` | Os DataFrames de uma análise incremental (ver [Namespace persistente](#namespace-persistente--o-ambiente-jupyter-do-agente)) só existem na memória do worker que os criou. Duas réplicas = a próxima mensagem pode cair num worker que nunca viu o DataFrame anterior |
| Fila de eventos SSE por sessão (`_event_queues`) | `agent_multi.py` | Se o request de streaming e a tool que produz eventos caírem em processos diferentes, os eventos nunca chegam ao gerador SSE |
| Runs ativos (`_active_runs`) | `agent_multi.py` | O cancelamento e a serialização de runs da mesma sessão só enxergam os runs do próprio processo |
| Snapshot do dashboard (`_chart_snapshots`) | `agent_multi.py` | Usado por `get_dashboard_charts()` e pelas imagens anexadas — mesma limitação |
| Daemon de agendamento | `main.py` (startup) | Roda dentro de cada processo web, então N workers = N daemons varrendo as mesmas tarefas. O `UPDATE ... WHERE status='active'` evita execução dupla de uma tarefa, mas o loop em si não foi pensado para isso |
| Turnos pendentes e executor da memória (`_turnos`, `_traces`, `_extraidos`, `ReflectionExecutor`) | `memoria.py` | Cada processo tem seu próprio executor e sua visão dos turnos de cada sessão: com N workers, turnos da mesma sessão caem em processos diferentes, a extração perde o turno de contexto e o lock do store só serializa dentro de um processo |
| `mfg.db` (SQLite) | `db.py`, `chart_store.py`, `memoria.py` | SQLite aceita um único escritor por vez; sob carga concorrente alta (muitos usuários simultâneos escrevendo tarefas/alertas/artefatos ao mesmo tempo) isso vira gargalo antes da CPU/rede virarem gargalo |

Dois pontos de comportamento que também valem registro:

- **O daemon executa as tarefas em sequência.** Cada tick percorre as tarefas vencidas uma a uma, e uma execução lenta (até o timeout de 10 minutos) atrasa as seguintes. Só o ▶ manual roda em thread própria.
- **`reports/` mora fora do volume de dados.** O diretório fica ao lado do código, enquanto o `mfg.db` pode estar em outra pasta (`DB_PATH`). Num redeploy que recrie a pasta do código, os `metadata.json` com `status: pending_send` somem, a menos que esse caminho seja preservado (os PDFs em si estão no `mfg.db`).

Hoje o app sobe um único processo (`python run.py` ou `uvicorn main:app`, sem `--workers`), o que é consistente com essas premissas. Se o requisito mudar para múltiplas réplicas, os `dict`s em memória precisam virar algo compartilhado (Redis, por exemplo) e o daemon precisa de um único dono antes disso funcionar corretamente — não é só trocar a infra.

---

## Stack

- **LLM**: Gemini (padrão `gemini-3.8-flash`, configurável por `MODEL_NAME`) via Google Vertex AI
- **Orquestração**: LangGraph 1.0+ (StateGraph, ToolNode, SqliteSaver)
- **Memória de longo prazo**: LangMem (só o `ReflectionExecutor`, que agenda a extração) sobre o `SqliteStore` do LangGraph
- **API**: FastAPI + SSE (sse-starlette)
- **Dados do domínio**: SQLAlchemy (modo `sql`) ou `requests` contra um endpoint GraphQL (modo `graphql`)
- **Frontend**: Angular 19 (standalone components, signals), Chart.js (+ chartjs-chart-matrix para heatmaps), marked, html-to-image — ver [Frontend](#frontend--angular-19-standalone-components)
- **Análise**: Python / Pandas / Matplotlib / Seaborn / SciPy — sandbox via `exec()` em namespace restrito
- **Documentos**: fpdf2 (PDF), openpyxl (Excel)
- **Persistência**: SQLite (`mfg.db`: estado do app, histórico de conversa, memória do agente, artefatos e alertas)
- **Threading**: `contextvars.ContextVar` para isolamento de sessão, usuário, idioma e cancelamento através de thread pools
