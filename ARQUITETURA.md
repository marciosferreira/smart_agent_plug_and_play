# Arquitetura Agêntica — Smart Agent Plug and Play

---

## Visão geral

Nesta arquitetura, o agente atua como uma camada de inteligência sobre os dados operacionais da fábrica. A partir de uma instrução em linguagem natural — digitada ou dita pelo usuário — o sistema consulta APIs e banco de dados em tempo real, processa os dados em ambiente Python isolado e entrega a análise na forma de texto, gráficos, planilhas Excel ou relatórios PDF.

O objetivo é eliminar a dependência de intermediários para consultas analíticas rotineiras: o operador ou gestor formula a pergunta diretamente ao sistema e recebe uma resposta fundamentada em dados reais, sem dado inventado e sem espera por um analista disponível.

Este documento descreve como cada componente foi construído e por que as decisões de projeto foram tomadas dessa forma.

---

## A Arquitetura em uma frase

> Um **orquestrador** que interpreta pedidos em linguagem natural — digitados ou ditos pelo usuário — delega para um **sub-agente especialista** que consulta **APIs e banco de dados em tempo real**, analisa os dados em sandbox Python e entrega gráficos, tabelas, planilhas Excel ou PDFs — tudo transmitido ao vivo via **streaming SSE**. Em paralelo, um **daemon de agendamento** executa monitores periódicos e dispara **alertas de threshold** visíveis no dashboard e **relatórios automáticos por e-mail**.

---

## Plug-and-play — trocar de banco e domínio sem mexer em código

O sistema não é escrito para uma fábrica ou um schema específico. Tudo que ele sabe sobre "que banco consultar" e "como entender esse domínio" vem de um **pack de configuração** na pasta `config/`, lido em runtime — trocar de cliente/domínio é trocar arquivos, não código.

O pack tem dois tipos de arquivo:

| Arquivo | Papel |
| --- | --- |
| `settings.yml` | Nome/descrição do app + `app.dominio` (qual arquivo de domínio está ativo) + URL SQLAlchemy do banco (segredos via `${VAR}`; a env `DATABASE_URL`, se definida, tem precedência total) |
| `dominio_*.md` | Conhecimento de domínio daquele banco, em seções canônicas de nível `##`. A pasta pode ter vários — só o apontado por `app.dominio` fica ativo |
| `dominio_example.md` | Template comentado para integrar um banco novo — copiar para `dominio_<nome>.md` e preencher |

Trocar `app.dominio` no `settings.yml` (e a `database.url` correspondente) reaponta o sistema inteiro — chat, widgets do dashboard, tarefas agendadas, monitores — para outro banco. **Requer restart do app**, porque os prompts são montados uma vez no init; o arquivo é lido com cache por `mtime` (`db_config.py`), então uma edição só é percebida depois de reiniciar ou quando o cache expira.

### Seções canônicas de um `dominio_*.md`

- `## Regras do agente` — injetada no prompt do sub-agente analista (armadilhas de schema, qual coluna de data usar, granularidade real dos dados)
- `## Regras do orquestrador` — injetada no prompt do orquestrador (o que este sistema É/não é, para negar assuntos de outros domínios)
- `## Períodos` — machine-parsable: heading `### frases | separadas` ou `### re:<regex>` seguido de um bloco ```sql``` que retorna 1 linha `(from, to)`. Resolve frases como "mês passado" sem depender do calendário real — útil quando os dados são um snapshot cuja "data atual" não é hoje. Ordem importa: a primeira entrada que casar vence
- `## Modelo de dados` — schema, joins, exemplos de query; servida ao agente por `read_skill('dominio.md')`
- `## Domínio de negócio` — glossário e regras de negócio (não-SQL); servida por `rag_dominio`
- `## Exemplos task_code` — anexada às regras do sandbox de tarefas agendadas, para o agente copiar o padrão de `run(from_date, to_date, ctx)`
- `## Dica para condition_sql` — tabelas/colunas úteis para a criação de monitores/alertas

Headings internos de uma seção devem ser `###` — nunca `##`, que é o delimitador de seção. Seção ausente não quebra o sistema, só degrada: o parser (`db_config._split_secoes`) trata cada uma como opcional.

Hoje a pasta `config/` traz dois domínios prontos como referência: `dominio_posh.md` (Postgres — pedidos de compra) e `dominio_alocacao.md` (SQLite `allocation.db` — alocação de supply por Key Account).

---

## Duas bases de dados: banco de domínio × banco local do app

É fácil confundir os dois bancos que o sistema usa — eles têm papéis completamente diferentes:

| | Banco de domínio | Banco local do app |
| --- | --- | --- |
| **O que é** | O banco de negócio que o chat consulta (Postgres, SQLite, MySQL, o que `config/settings.yml` apontar) | `mfg.db` (SQLite), criado e migrado por `db.py` na raiz do projeto |
| **Quem lê/escreve** | Apenas leitura — `db_config.run_select()` recusa qualquer instrução que não seja `SELECT` | Leitura e escrita — é o "estado" do próprio app |
| **O que guarda** | Os dados que o negócio quer analisar | Tarefas agendadas (`scheduled_tasks`), histórico de execução (`task_runs`, `task_code_versions`), alertas de threshold (`threshold_alerts`), painéis customizados do dashboard (`dashboard_widgets`), auditoria de erros de geração de código (`task_code_audit`), qual domínio cada sessão de chat usava (`session_dominio`) |
| **Troca com a config?** | Sim — é exatamente o que `database.url` aponta | Não — é fixo por instalação, independe de qual domínio está ativo |
| **Histórico de conversa** | — | Também SQLite local, mas em arquivo próprio via `SqliteSaver` do LangGraph (checkpointer, ver abaixo) — não mistura com `mfg.db` |

Na prática: apagar/mover `mfg.db` reseta tarefas, alertas e widgets sem afetar o banco de domínio; trocar o banco de domínio via `settings.yml` não afeta nada que esteja em `mfg.db`.

---

## Identidade do usuário (`user_id`) — como é coletada e usada

O sistema **não implementa login**. Ele é pensado para ser embutido (iframe/embed) dentro de um app host que já resolveu autenticação — o `user_id` é apenas um identificador que separa os dados de cada usuário dentro do mesmo domínio ativo. A responsabilidade de garantir que a pessoa certa está vendo o `user_id` certo é do app host, não deste sistema.

### Como o `user_id` chega ao sistema

```text
App host (já autenticado)
  └── carrega a página com ?user_id=<id-do-usuário-no-host>
        └── UserSessionService (Angular) lê o parâmetro na URL (new URLSearchParams(location.search))
              ├── se presente → sobrescreve o valor salvo em localStorage (fonte da verdade é sempre o host)
              └── se ausente  → usa o valor já salvo em localStorage, ou gera um UUID novo (modo standalone/demo)
                    └── persiste em localStorage['mfg_user_id'] para sobreviver a reloads
```

Esse `_userId` do frontend é então enviado **explicitamente** em toda chamada que precisa de isolamento por usuário: `/chat/stream?...&user_id=...`, `/tasks?user_id=...`, `/dashboard-widgets?user_id=...`, `/artifacts?user_id=...`, etc. Não existe cookie de sessão nem header de autenticação — é um parâmetro de query passado a cada request, no mesmo espírito plug-and-play do resto do sistema.

**Modo standalone/demo**: quando não há host embutindo a página (ex: `http://localhost:8000` aberto direto no browser), o sistema gera um UUID aleatório na primeira visita e o guarda em `localStorage`. Cada browser/perfil vira, na prática, "um usuário" — suficiente para demonstração, mas não é autenticação real.

### Como o `user_id` percorre o backend

No FastAPI, `/chat` e `/chat/stream` recebem `user_id` como parâmetro e o repassam para `invoke_multi_agent()` / `stream_multi_agent()`. De lá, o valor entra num `contextvars.ContextVar` próprio (`_current_user_id`, em `agent_multi.py`), separado do `_current_session` (que carrega o `session_id`/thread de conversa). A separação existe porque as duas coisas são independentes: o mesmo `user_id` pode ter várias conversas (`session_id`s) abertas, e o `session_id` sozinho nunca deveria ser usado para inferir quem é o usuário.

Como `ContextVar` não atravessa `threading.Thread` criadas manualmente (só atravessa pools como o `ThreadPoolExecutor` que o LangGraph usa internamente), o valor é re-setado explicitamente no ponto onde o streaming SSE sobe uma thread própria para rodar o orquestrador — o mesmo cuidado que já existia para `session_id`.

Tools como as de painéis customizados (`add_chart_to_dashboard`, `list_dashboard_widgets`, etc., em `scheduler/widget_tools.py`) leem o usuário atual direto desse `ContextVar` — não inferem mais nada a partir do formato do `session_id`.

Tarefas agendadas são o único fluxo que não passa pelo `ContextVar`: o `user_id` fica gravado na própria linha da tarefa (`scheduled_tasks.user_id`) no momento da criação, e o daemon o repassa diretamente ao `TaskContext` de cada execução — por isso um monitor criado por um usuário continua rodando (e gerando artefatos com o `user_id` correto) mesmo sem nenhuma sessão de chat ativa.

### Onde o `user_id` separa dados

| Tabela / recurso | Coluna | Sem `user_id` no request |
| --- | --- | --- |
| `scheduled_tasks` (tarefas agendadas) | `user_id` | Vê só as tarefas sem dono (`user_id IS NULL`) |
| `dashboard_widgets` (painéis customizados) | `user_id` | Vê só os painéis sem dono |
| `threshold_alerts` (alertas de threshold) | `user_id` | Vê só alertas sem dono |
| `charts` / `pdfs` / `excels` (artefatos gerados no chat) | `user_id` | Vê só artefatos sem dono |

Em todos os casos a regra é a mesma cláusula SQL — `(user_id = ? OR user_id IS NULL)` — aplicada com o parâmetro vindo do request: uma linha com `user_id` preenchido só aparece para o dono; uma linha com `user_id NULL` é tratada como "compartilhada" e aparece para qualquer um (esse é o caso de registros antigos, criados antes dessa separação existir, e serve como modo degradado seguro quando o host esquece de mandar o parâmetro — ninguém vê dados de outro usuário, só dados sem dono).

### `user_id` × `dominio` — eixos independentes

Não confundir os dois: `dominio` (ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código)) é **global ao processo**, definido em `settings.yml` — decide *qual banco de negócio* está ativo para toda a instalação. `user_id` é **por requisição**, informado pelo host — decide *de quem* são os dados dentro daquele domínio. Trocar o domínio não redistribui usuários; trocar de usuário não troca o banco de negócio consultado.

### Histórico de conversa

O histórico de chat (checkpointer LangGraph) continua isolado por `session_id`, não por `user_id` diretamente — mas o frontend gera `session_id` como `<user_id>_<timestamp>` (`ChatService.newChat()`, Angular), e `/chat/sessions?user_id=...` lista as sessões cujo `thread_id` começa com esse prefixo. Ou seja, a listagem de conversas de um usuário depende da convenção de nomenclatura do `session_id`, enquanto tarefas, painéis, alertas e artefatos usam uma coluna `user_id` real no banco.

Isso tem uma implicação direta para onde o `session_id` **ativo** fica guardado no browser: a chave de `localStorage` que lembra "qual foi a última conversa aberta" é namespaced por `user_id` (`mfg_session_id::<user_id>`, em `UserSessionService`) — nunca uma chave global única. Sem esse namespacing, trocar o `user_id` na URL (ex: simular um segundo usuário no mesmo browser) continuaria devolvendo a última conversa de *outro* usuário salva naquele mesmo `localStorage`, porque a busca de histórico é por `session_id` e o `session_id` reaproveitado não teria trocado junto. Cada `user_id` mantém assim sua própria sessão ativa lembrada entre reloads, sem vazar para outro usuário do mesmo browser.

---

## Grafo multi-agente (LangGraph)

A espinha dorsal é um grafo de estados (StateGraph) em dois níveis:

```text
Usuário (voz ou texto)
  └── Orquestrador (LangGraph)
        ├── get_current_datetime()   → responde diretamente
        ├── analisar_grafico()       → dispara o sub-agente
        ├── gerar_excel()            → exporta DataFrame como planilha .xlsx
        ├── gerar_pdf()              → empacota análise em PDF
        └── schedule_task()          → agenda tarefa no daemon

              Sub-agente Analista (sub-grafo separado)
                ├── read_skill()         → lê instruções da skill
                ├── calcular_periodo()   → resolve datas em linguagem natural
                ├── chamar_api()         → busca dados via REST
                ├── executar_sql()       → queries ad-hoc no banco
                └── analisar_dataframe() → executa código Python/Pandas

Daemon de agendamento (thread separada)
  └── Executa task_code gerado pelo agente
        ├── ctx.notify()             → salva alerta de threshold no banco
        └── metadata.json            → registra relatório pronto para envio por e-mail
```

O orquestrador **não sabe analisar dados** — ele só sabe a quem perguntar. O sub-agente **não tem acesso ao histórico de conversa** — ele só recebe o pedido pontual delegado. Essa separação de responsabilidades é deliberada: cada camada faz uma coisa e faz bem.

---

## Skills — documentação sob demanda

As skills são arquivos `.md` que descrevem como interagir com cada endpoint da API industrial. O sub-agente não recebe todo o conteúdo no startup — isso desperdiçaria contexto para skills que não serão usadas.

O que acontece de fato:

1. **Na inicialização**: apenas os cabeçalhos (primeiras 4 linhas de cada `.md`) são injetados no system prompt. O agente sabe que as skills existem e do que tratam.
2. **Sob demanda**: quando identifica qual skill precisa, chama `read_skill('nome.md')` e recebe o documento completo.

Isso é lazy-loading aplicado ao contexto do LLM. A consequência prática é que o sistema escala para dezenas de endpoints sem inflar o custo por token de cada requisição.

---

## Namespace persistente — o ambiente Jupyter do agente

O problema central de análise incremental: quando o agente chama `chamar_api()` e depois `analisar_dataframe()`, os DataFrames precisam estar disponíveis entre as chamadas. Mas tools em LangGraph rodam em threads separadas do `ThreadPoolExecutor` — `threading.local()` não funciona aqui.

A solução: um `dict` global indexado por `session_id`, protegido por `threading.Lock`, com o `session_id` atual propagado por `contextvars.ContextVar` (que, diferente de `threading.local()`, **se propaga automaticamente para threads filhas**).

O resultado é um ambiente de execução Python que se comporta exatamente como um notebook Jupyter:

- `chamar_api(url, chave='producao')` → injeta `df` como variável `producao` no namespace
- `analisar_dataframe(script)` → o script acessa `producao` diretamente, pode criar `resultado`, que fica disponível na próxima chamada
- Variáveis persistem entre múltiplas chamadas de tools e entre invocações consecutivas do sub-agente

---

## Proteção de contexto — o agente que não se afoga em dados

Um DataFrame com 30 dias de produção horária por linha pode ter milhares de linhas. Enviar isso para o modelo a cada tool call é caro, lento e desnecessário.

A regra implementada:

| Saída                | Condição      | O que o LLM recebe                          |
| -------------------- | ------------- | ------------------------------------------- |
| Tabela completa      | ≤ 15 linhas   | Markdown completo                           |
| Descrição estrutural | > 15 linhas   | shape + dtypes + primeira linha             |
| Resumo de namespace  | Sempre        | Metadados das variáveis (nome, tipo, shape) |
| Dados brutos         | Nunca         | — Protegido no backend                      |

O agente sempre sabe o que está disponível no ambiente (via `_ns_summary`) sem nunca receber os dados em si. Ele só vê dados quando o resultado da análise é pequeno o suficiente para ser relevante.

---

## Checkpointer — memória de conversa por sessão

O histórico de mensagens do orquestrador é persistido automaticamente pelo `SqliteSaver` do LangGraph no banco local. Cada `session_id` tem seu próprio thread de conversa — o usuário pode retomar uma análise horas depois e o agente lembra o contexto.

Para evitar que o contexto cresça indefinidamente (e o custo por token junto), o orquestrador envia ao modelo apenas as últimas `N` interações — o histórico completo fica no banco, mas o janela ativa é controlada.

---

## Streaming SSE — a interface que pensa em voz alta

O frontend não espera a resposta final. Cada etapa do processo chega em tempo real via **Server-Sent Events**:

```text
→ {"type": "thinking",  "text": "preciso verificar os dados de produção..."}
→ {"type": "tool",      "label": "📖 Lendo instruções da skill"}
→ {"type": "tool",      "label": "🌐 Buscando dados da API"}
→ {"type": "tool",      "label": "🔢 Processando e analisando dados"}
→ {"type": "reply",     "text": "## Produção Semanal\n\n![grafico](...)\n\n..."}
```

O desafio aqui é que o sub-agente roda dentro de um `@tool` do orquestrador — fora do stream principal do LangGraph. A solução é um **side-channel por sessão**: uma `queue.Queue` por `session_id` captura os eventos do sub-agente em tempo real e os entrega ao gerador SSE que está servindo o frontend.

**Importante: SSE é só para o chat.** O resto do dashboard não é push-based — não há WebSocket nem um segundo canal SSE. O sino de alertas 🔔 e o badge da agenda fazem **polling simples via `setInterval` a cada 15s** (`AlertsService`/`AgendaService`, Angular, GET em `/alerts` e `/tasks`). Os gráficos e KPIs principais do dashboard (produção, OEE, etc.) carregam uma vez no load da página e não se auto-atualizam sozinhos. Um novo desenvolvedor que for adicionar "tempo real" em outra parte do dashboard deve decidir entre reaproveitar esse padrão de polling (simples, já testado) ou abrir um canal push novo — hoje não existe abstração compartilhada para isso além da SSE do chat.

---

## Comando de voz — gravar, revisar e enviar

O usuário pode interagir com o agente por voz diretamente no chat. O fluxo tem três etapas explícitas antes de qualquer processamento:

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

O usuário sempre vê e pode editar o transcript antes de ele chegar ao agente — não há submissão silenciosa. A transcrição usa o mesmo modelo Gemini já configurado no projeto, sem dependência de um serviço de speech externo.

---

## Alertas de threshold — monitoramento contínuo

O usuário pode pedir ao agente para monitorar qualquer condição de negócio: *"me avise se o OEE cair abaixo de 80%"* ou *"alerte quando a linha 2 ficar inoperante"*. O agente gera um `task_code` Python que o daemon executa periodicamente. Quando a condição é satisfeita, o código chama `ctx.notify()`:

```python
if oee < 80:
    ctx.notify(f"OEE {oee:.1f}% — abaixo de 80%", value=oee, threshold=80)
```

O alerta é salvo no banco com valor observado e valor de referência. O dashboard exibe os alertas ativos no sino 🔔 do header em tempo real, sem necessidade de recarregar a página.

---

## Relatórios agendados por e-mail

Tarefas recorrentes (diárias, semanais, mensais) são criadas via chat: *"todo dia às 7h gere o relatório de produção e envie para gerencia at empresa.com"*. O agente chama `schedule_task()`, que persiste a tarefa no banco com frequência, horário e e-mail de destino.

Quando o daemon executa a tarefa no horário programado:

1. Invoca o agente com o `task_code` gerado
2. Coleta os artefatos produzidos (PDFs, gráficos)
3. Salva um `metadata.json` com status `pending_send` e os detalhes do e-mail (destinatário, assunto, links dos anexos)

O relatório fica disponível localmente e o metadado estruturado permite integração com qualquer serviço de envio de e-mail externo sem acoplar o daemon a um servidor SMTP específico.

---

## Gráficos, PDFs e Excels como artefatos persistentes

Quando o sub-agente gera um gráfico matplotlib, ele não trafega pelo contexto do LLM. O que acontece:

1. O script salva `result = fig` (figura matplotlib)
2. A tool serializa para PNG e armazena no banco com um UUID
3. O modelo recebe apenas o token `[chart:3f8a1b...]`
4. O FastAPI expõe `/chart/{id}` e o frontend renderiza a imagem

O mesmo padrão se aplica a PDFs gerados por `gerar_pdf()` e a planilhas geradas por `gerar_excel()`. Para o Excel, o orquestrador recebe o token `[excel:3f8a1b...]`, que o FastAPI resolve em um link de download `/excel/{id}` — o usuário clica e baixa o `.xlsx` diretamente. Os artefatos ficam disponíveis via URL enquanto a sessão estiver ativa — o banco funciona como object storage local.

---

## Painéis customizados do dashboard — JSON gerado em sandbox, nunca persistido

Este fluxo é fácil de confundir com o de "Gráficos, PDFs e Excels" acima, mas é um mecanismo **diferente**: um gráfico de chat é uma imagem PNG estática; um painel fixado no dashboard (seção "Painéis Customizados") é recalculado a cada refresh, e **o que fica salvo no banco não é o gráfico, é o código-fonte que o gera**.

Não existe HTML gerado pelo agente em nenhum dos dois casos — o `<canvas>` do painel já existe fixo no `WidgetCardComponent` (Angular); o agente só produz configuração de dados.

### O que é salvo (`dashboard_widgets` em `mfg.db`)

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
    range_value  INTEGER NOT NULL DEFAULT 7
);
```

A coluna `code` guarda uma função Python (`def run(from_date, to_date, ctx): ...`) escrita pelo LLM — nunca o JSON Chart.js pronto, nunca uma imagem. `range_unit`/`range_value` guardam o período que o usuário pediu ao criar o painel (ex: "últimos 30 dias", "últimos 3 meses") como dado estruturado, não embutido no código.

### Onde o JSON é gerado — sandbox, sob demanda, a cada refresh

```text
Criação/edição do painel (via chat, uma única vez)
  Usuário: "adiciona ao dashboard as vendas por produto dos últimos 30 dias"
    └── LLM escreve code = "def run(from_date, to_date, ctx): ..."
          └── test_widget_code(code)              → valida em sandbox (scheduler/widget_runner.py)
          └── add_chart_to_dashboard(code, range_unit="days", range_value=30)
                └── valida de novo e faz INSERT em dashboard_widgets

Refresh do dashboard (toda vez que a página carrega ou o usuário clica "Atualizar")
  WidgetsService (Angular) → GET /dashboard-widgets/{id}/data
    └── main.py lê a coluna `code` + range_unit/range_value do banco
          └── compute_widget_range(range_unit, range_value)   → calcula from_date/to_date
                a partir de date.today() NAQUELE INSTANTE (scheduler/widget_runner.py)
          └── run_widget_code(code, from_date, to_date, session_id)
                └── exec() do `code` num sandbox restrito (mesmo padrão do task runner):
                      imports bloqueados exceto pandas/numpy/datetime/etc,
                      variáveis pré-injetadas: pd, np, ctx, from_date, to_date, date, datetime, timedelta
                └── run(from_date, to_date, ctx) executa queries reais (ctx.sql/ctx.api)
                      contra o banco de domínio NAQUELE INSTANTE
                └── retorna um dict Chart.js validado: {"type", "data": {"labels","datasets"}, "options"?}
    └── response JSON volta para o browser (nunca é gravado de volta no banco)
```

Esse JSON é **efêmero**: existe só na resposta HTTP daquela requisição. Não há tabela nem cache que guarde o resultado — a próxima chamada a `/dashboard-widgets/{id}/data` reexecuta tudo do zero.

### Onde e como o front renderiza — Chart.js client-side, sem HTML injetado

Cada painel é um `WidgetCardComponent` (Angular) com um `<canvas>` no seu template. O ponto não-óbvio: as instâncias do **Chart.js** não pertencem ao componente — elas ficam centralizadas dentro do `WidgetsService` (`frontend/src/app/core/services/widgets.service.ts`), num `Map<widgetId, Chart>`, no mesmo espírito do dict global `_widgetCharts` que existia no `dashboard.html` original. `WidgetCardComponent.ngAfterViewInit()` só registra seu `<canvas>` no serviço (`registerCanvas(id, canvas)`); é o serviço que decide quando criar/recriar/destruir o gráfico:

```ts
new Chart(canvas, { type: config.type, data: config.data, options: {...} });
```

Essa centralização existe por um motivo concreto: os eventos SSE do chat (`widget_added`, `widget_updated`, `widget_deleted`, ver [Streaming SSE](#streaming-sse--a-interface-que-pensa-em-voz-alta)) chegam no `ChatService`, que não tem — e não deveria precisar ter — uma referência ao componente visual de um widget específico. Em vez disso, `ChatService` chama `WidgetsService.onWidgetAdded()` / `onWidgetUpdated(id)` / `onWidgetDeleted(id)` diretamente; o serviço recarrega a lista, busca o `/dashboard-widgets/{id}/data` de novo e redesenha (ou destrói) a instância Chart.js correspondente — sem precisar dar F5, e sem o componente precisar "escutar" nada.

### Regra crítica sobre datas — por que `range_unit`/`range_value` existem

Antes de existirem essas duas colunas, `from_date`/`to_date` eram sempre calculados como "hoje − 7 dias" fixo, e o único jeito de um painel mostrar um período diferente era o próprio `code` recalcular a data internamente (`date.today() - timedelta(...)`), ignorando os parâmetros recebidos — uma convenção sustentada só por instrução de prompt, sem garantia estrutural. Isso ainda funciona (o LLM foi instruído a seguir o padrão), mas o comportamento correto e a fonte de verdade agora são `range_unit`/`range_value`: o backend calcula `from_date`/`to_date` certos a partir deles antes de chamar `run()`, e o `code` gerado deve apenas usar o que recebeu — não recalcular nada.

---

## Referência de rotas HTTP

Toda a API vive num único `FastAPI` (`main.py`), sem prefixo/versionamento (`/chat`, não `/api/v1/chat`). Rotas que isolam dado por usuário aceitam `user_id` como **query param opcional** — ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada) para a semântica de quando ele falta.

### Chat e voz

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/chat` | POST | Envia uma mensagem e espera a resposta completa (sem streaming) — `{message, session_id, user_id}` |
| `/chat/stream` | GET (SSE) | Mesma coisa, mas via Server-Sent Events — thinking/tool/reply em tempo real. É o que o frontend usa de fato |
| `/chat/history` | GET | Histórico de mensagens (Human/AI) de um `session_id` |
| `/chat/sessions` | GET | Lista as conversas de um `user_id` (título + contagem de mensagens), filtradas pelo domínio ativo |
| `/chat/sessions/{session_id}` | DELETE | Apaga uma conversa e os artefatos (charts/pdfs/excels) associados a ela |
| `/chat/transcribe` | POST | Recebe áudio (webm/opus) e devolve a transcrição via Gemini |
| `/chart-snapshot` | POST | Frontend envia o estado atual dos gráficos/KPIs do dashboard antes de cada mensagem — usado por `get_dashboard_charts()` |

### Artefatos (gráficos, PDF, Excel)

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/chart/{chart_id}` | GET | Serve o PNG de um gráfico gerado pelo agente |
| `/pdf/{pdf_id}` | GET | Download do PDF gerado |
| `/excel/{excel_id}` | GET | Download da planilha gerada |
| `/artifacts` | GET | Lista todos os artefatos (chart/pdf/excel) de um `user_id`, com origem (`chat` ou `task`) |
| `/artifacts/{type}/{id}` | DELETE | Apaga um artefato específico (`type` = `chart`\|`pdf`\|`excel`) |

### Agenda (tarefas agendadas)

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/tasks` | GET | Lista as tarefas de um `user_id`, com as últimas 5 execuções de cada |
| `/tasks/{id}/runs` | GET | Histórico completo de execuções de uma tarefa |
| `/tasks/{id}/run-now` | POST | Dispara execução imediata (só funciona se `status=active`) |
| `/tasks/{id}/toggle-pause` | POST | Alterna `active` ↔ `paused` |
| `/tasks/{id}/toggle-notify` | POST | Liga/desliga notificações da tarefa (bloqueado se ela tiver `condition_sql`) |
| `/tasks/{id}` | DELETE | Remove a tarefa |
| `/tasks/{id}/code` | GET | Código atual (`task_code`) + histórico de versões |
| `/tasks/{id}/code-audit` | GET | Log de erros durante a geração/correção automática do código da tarefa |
| `/tasks/code-audit/summary` | GET | Resumo agregado de erros de geração de código (todas as tarefas, últimos 200) |
| `/tasks/code-audit/{id}` | DELETE | Apaga uma entrada de auditoria |
| `/tasks/code-audit` | DELETE | Apaga toda a auditoria |
| `/reports/{task_id}` | GET | Lista os relatórios (PDF/e-mail) gerados por execuções passadas dessa tarefa |

### Painéis customizados do dashboard

| Rota | Método | Para que serve |
| --- | --- | --- |
| `/dashboard-widgets` | GET | Lista os painéis fixados de um `user_id` |
| `/dashboard-widgets/{id}/data` | GET | Reexecuta o `widget_code` e devolve a config Chart.js atualizada — é o que o dashboard chama a cada refresh |
| `/dashboard-widgets/{id}` | DELETE | Remove um painel |

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
| `/app-info` | GET | Nome/descrição do pack de configuração ativo — usado pelo dashboard para branding |
| `/` | GET | Serve o `index.html` do build Angular (`frontend/dist/frontend/browser/`) — a SPA inteira |
| `/{arquivo estático do build}` | GET | JS/CSS com hash, `favicon.ico` etc. — mount `StaticFiles` do build Angular, registrado por último para não sombrear as rotas de API acima |
| `/static/*` | GET | Assets estáticos legados (hoje só `logo.png` é de fato usado pelo header — as demais libs de terceiro do `dashboard.html` antigo, como Chart.js/marked/Tailwind, viraram dependências `npm` do Angular) |
| `/docs`, `/redoc` | GET | Swagger/Redoc gerados automaticamente pelo FastAPI |

---

## Transporte, CORS e autenticação

Pontos que um novo desenvolvedor precisa saber antes de expor isso além do `localhost`:

- **HTTP puro dentro do container.** O `uvicorn` (ver `Dockerfile`/`run.py`) não termina TLS — ele serve HTTP simples na porta 8000. Em produção (o `Dockerfile` é feito para EasyPanel), HTTPS é responsabilidade da camada na frente do container (reverse proxy/PaaS) — a aplicação em si não sabe nem precisa saber que existe TLS.
- **CORS totalmente aberto, mas só relevante para consumidores externos.** `CORSMiddleware` está configurado com `allow_origins=["*"]` e `allow_methods=["GET", "POST"]` (`main.py`) — note que `DELETE`/`PUT` não estão liberados. Isso não afeta o dashboard Angular porque ele é servido **same-origin** (o próprio FastAPI serve o `index.html` na raiz — ver [Frontend](#frontend--angular-19-standalone-components)), então suas chamadas `DELETE` nunca passam por preflight de CORS. Só importa se algum dia um cliente rodando em outra origem precisar chamar essas rotas.
- **Sem autenticação na API.** Não há API key, JWT nem sessão de servidor — nenhuma rota valida quem está fazendo a chamada. O único controle de acesso é a separação de dados por `user_id` (ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada)), e esse `user_id` é um query param em texto puro, não assinado — qualquer cliente que descubra/adivinhe o `user_id` de outra pessoa consegue ler os dados dela. O modelo de segurança inteiro depende do app host controlar quem recebe qual `user_id`.

---

## Por que isso importa para a sua fábrica

A combinação desses elementos resolve os problemas reais de quem quer colocar IA generativa num ambiente industrial:

| Problema | Solução nesta arquitetura |
| --- | --- |
| "O modelo inventa dados" | Sub-agente só responde com resultados de `analisar_dataframe` — sem dados, sem resposta |
| "A tela trava esperando a resposta" | Streaming SSE com thinking e tool calls em tempo real |
| "Não consigo analisar mês inteiro, é muita coisa" | Namespace incrementa análises em etapas, contexto protegido por limites de exibição |
| "O agente esquece o que foi dito antes" | Checkpointer SQLite persiste histórico por session_id |
| "Como adiciono novos endpoints sem mexer no agente?" | Basta criar um novo `.md` na pasta de skills |
| "O modelo fica confuso com muitas APIs ao mesmo tempo" | Lazy-loading de skills — só carrega o que vai usar |
| "Preciso exportar os dados para o Excel" | `gerar_excel()` cria um `.xlsx` formatado e disponibiliza link de download direto no chat |
| "Preciso atender outro cliente/domínio sem duplicar o projeto" | Trocar `config/settings.yml` + `dominio_*.md` reaponta o sistema inteiro — ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código) |
| "Preciso plugar isso num app que já tem usuários e login" | O host passa `?user_id=...`; o sistema não reimplementa autenticação, só separa tarefas, painéis, alertas e artefatos por esse `user_id` — ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada) |

---

## Instalação e execução

### Pré-requisitos

- Python 3.11+ (o projeto usa `langgraph`/`langchain` atuais — ver `requirements.txt`)
- Node.js 20+ e npm, para o frontend Angular (ver [Frontend](#frontend--angular-19-standalone-components))
- Um projeto Google Cloud com Vertex AI habilitado, se for usar o chat IA (`PROJECT_ID`)

### Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux/Mac

pip install -r requirements.txt

cd frontend && npm install && cd ..
```

### Configurar o pack de domínio

Ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código) — editar `config/settings.yml` (nome do app, domínio ativo, URL do banco) e garantir que existe um `config/dominio_<nome>.md` correspondente. Sem isso, `db_config.is_configured()` retorna falso e o chat funciona sem análise de dados.

### Variáveis de ambiente

| Variável | Papel | Default |
| --- | --- | --- |
| `DATABASE_URL` | Sobrepõe `database.url` do `settings.yml` — tem precedência total | — |
| `CONFIG_DIR` | Pasta do pack de configuração, se não for `config/` na raiz | `config/` |
| `DB_PATH` | Caminho do banco local do app (`mfg.db`) — em produção (Docker/EasyPanel) aponta para um volume montado, ex. `/data/mfg.db` | `<raiz do projeto>/mfg.db` |
| `PROJECT_ID` | Projeto GCP para o Vertex AI — sem ele o chat IA fica desabilitado | — |
| `LOCATION` | Região do Vertex AI | `us-central1` |
| `MODEL_NAME` | Modelo Gemini usado pelo agente | `gemini-2.5-flash` |
| `GOOGLE_CREDENTIALS_JSON` | Credenciais de service account em base64 (alternativa a `credentials.json` na raiz) | — |
| `BACKEND_URL` | URL pública do backend, usada para montar links de artefatos | `http://localhost:8000` |
| `DEBUG_TOOLS` | `1` loga o payload completo de cada tool call do agente | `0` |
| `MAX_INTERACOES` | Janela de mensagens recentes enviada ao modelo pelo checkpointer | `10` |

### Rodar

**Desenvolvimento** (dois processos em paralelo, cada um no seu terminal):

```bash
python run.py            # backend — http://localhost:8000
# equivalente a: uvicorn main:app --reload --host 0.0.0.0

cd frontend && npm start # frontend — http://localhost:4200, com proxy para o backend acima
```

Acesse `http://localhost:4200` durante o desenvolvimento — é o Angular com hot-reload. `http://localhost:8000` sozinho só serve o frontend se `npm run build` já tiver sido rodado (ver [Frontend](#frontend--angular-19-standalone-components)).

**"Produção local"** (um único processo, caminho igual ao do Docker):

```bash
cd frontend && npm run build && cd ..
python run.py
```

Acesse `http://localhost:8000` — o próprio FastAPI serve o Angular já compilado. Docs automáticas do FastAPI em `/docs` (Swagger) e `/redoc`.

Na primeira execução, `db.py` cria e semeia `mfg.db` (dados de demo de manufatura) se ele ainda não existir — isso é independente do banco de domínio configurado em `config/`.

---

## Frontend — Angular 19, standalone components

O frontend vive em `frontend/` (workspace Angular separado do backend Python) e é uma SPA sem rotas — não há Angular Router, porque não existe hoje nenhuma view endereçável por URL: tudo é painel/slide-over/modal sobre uma única tela, exatamente como no `dashboard.html` que ela substituiu. Standalone components (padrão do Angular 19, sem `NgModule`) e **signals** no lugar de `RxJS`/`Observable` para todo o estado local — a única exceção é `ChatStreamService`, que envolve o `EventSource` do SSE num `Observable` porque é o formato que a API do browser expõe.

```text
frontend/src/app/
  core/services/      # UserSessionService, ChatService, AlertsService, AgendaService,
                       # ArtifactsService, WidgetsService, ChatStreamService, ...
  core/models/         # interfaces TypeScript dos payloads da API
  shared/components/   # modais reaproveitados entre features (ChartModal, TaskCodeModal, ...)
  features/            # chat/, header/, agenda/, alerts/, audit/, artifacts/, widgets/
  app.component.ts      # shell: header + painéis + grid de widgets + chat sidebar
```

Cada `feature` segue o mesmo padrão: um `*Service` (`providedIn: 'root'`, injetável, dono do estado e das chamadas HTTP) e um ou mais componentes "burros" que só leem signals do serviço e disparam métodos nele — nenhum componente chama `HttpClient` diretamente.

Identidade (`UserSessionService.userId`) e sessão (`UserSessionService.sessionId`, um `signal`) — ver [Identidade do usuário](#identidade-do-usuário-user_id--como-é-coletada-e-usada) — persistem em `localStorage`, portados do `dashboard.html` original com o mesmo comportamento: o `?user_id=` do host sempre vence sobre o valor salvo.

### Rodando em desenvolvimento

```bash
cd frontend
npm install
npm start            # = ng serve — sobe em http://localhost:4200
```

`ng serve` usa `frontend/proxy.conf.json` para redirecionar `/chat`, `/tasks`, `/alerts`, `/artifacts`, `/dashboard-widgets`, `/static`, etc. para o backend FastAPI local (`http://localhost:8000`, precisa estar rodando via `python run.py` em paralelo). Isso torna o dev server **same-origin** do ponto de vista do browser — sem isso, chamadas `DELETE` esbarrariam no `allow_methods=["GET", "POST"]` do CORS (ver [Transporte, CORS e autenticação](#transporte-cors-e-autenticação)).

### Build de produção e como o FastAPI serve o resultado

```bash
cd frontend
npm run build         # = ng build — gera frontend/dist/frontend/browser/
```

Em produção, `main.py` **não roda o Angular** — ele só serve os arquivos estáticos já compilados:

- `GET /` devolve `frontend/dist/frontend/browser/index.html` (`FileResponse`)
- Um `StaticFiles` é montado em `/` para os demais arquivos do build (JS/CSS com hash, `favicon.ico`) — **registrado por último no arquivo**, depois de todas as rotas de API, para que o Starlette resolva as rotas explícitas (`/chat`, `/tasks`, etc.) antes de cair no mount

Isso significa: **rodar `python run.py` sem antes rodar `npm run build` serve uma tela em branco** (o backend loga um warning e a rota `/` devolve um `index.html` inexistente). O `Dockerfile` cuida disso automaticamente com um estágio `node:20-slim` que roda `npm ci && npm run build` antes do estágio Python final, que só copia o resultado (`frontend/dist/frontend/browser/`) — a imagem final não tem Node, só o build já pronto.

---

## Limitações conhecidas — estado em memória, processo único

Vários pontos do sistema guardam estado em `dict`s Python globais dentro do processo do `uvicorn`, não em algo externo (Redis, banco). Isso é simples e suficiente para **um único processo/worker**, mas é uma armadilha real se alguém tentar escalar horizontalmente (`--workers N` do uvicorn, múltiplas réplicas atrás de um load balancer) sem revisar esse ponto primeiro:

| Estado | Onde vive | Por que importa |
| --- | --- | --- |
| Namespace Python por sessão (`_namespaces`) | `agent_multi.py` | Os DataFrames de uma análise incremental (ver [Namespace persistente](#namespace-persistente--o-ambiente-jupyter-do-agente)) só existem na memória do worker que os criou. Duas réplicas = a próxima mensagem pode cair num worker que nunca viu o DataFrame anterior |
| Fila de eventos SSE por sessão (`_event_queues`) | `agent_multi.py` | Se o request de streaming e a tool que produz eventos caírem em processos diferentes, os eventos nunca chegam ao gerador SSE |
| Snapshot do dashboard (`_chart_snapshots`) | `agent_multi.py` | Usado por `get_dashboard_charts()`/perguntas sobre "o que está na tela" — mesma limitação |
| `mfg.db` (SQLite) | `db.py`, `chart_store.py` | SQLite aceita um único escritor por vez; sob carga concorrente alta (muitos usuários simultâneos escrevendo tarefas/alertas/artefatos ao mesmo tempo) isso vira gargalo antes da CPU/rede virarem gargalo |

Hoje o deploy de referência (`Dockerfile`) sobe um único processo (`uvicorn main:app`, sem `--workers`), o que é consistente com essas premissas. Se o requisito mudar para múltiplas réplicas, os três `dict`s em memória precisam virar algo compartilhado (Redis, por exemplo) antes disso funcionar corretamente — não é só trocar a infra.

---

## Stack

- **LLM**: Gemini 2.5 Flash (Google Vertex AI)
- **Orquestração**: LangGraph 1.0+ (StateGraph, ToolNode, SqliteSaver)
- **API**: FastAPI + SSE (sse-starlette)
- **Frontend**: Angular 19 (standalone components, signals), Chart.js, marked, html-to-image — ver [Frontend](#frontend--angular-19-standalone-components)
- **Análise**: Python / Pandas / Matplotlib — sandbox via `exec()` em namespace isolado
- **Persistência**: SQLite (histórico de conversa + gráficos + PDFs na mesma base)
- **Threading**: `contextvars.ContextVar` para isolamento de sessão através de thread pools
