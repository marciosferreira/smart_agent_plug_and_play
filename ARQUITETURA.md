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

---

## Instalação e execução

### Pré-requisitos

- Python 3.11+ (o projeto usa `langgraph`/`langchain` atuais — ver `requirements.txt`)
- Um projeto Google Cloud com Vertex AI habilitado, se for usar o chat IA (`PROJECT_ID`)

### Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux/Mac

pip install -r requirements.txt
```

### Configurar o pack de domínio

Ver [Plug-and-play](#plug-and-play--trocar-de-banco-e-domínio-sem-mexer-em-código) — editar `config/settings.yml` (nome do app, domínio ativo, URL do banco) e garantir que existe um `config/dominio_<nome>.md` correspondente. Sem isso, `db_config.is_configured()` retorna falso e o chat funciona sem análise de dados.

### Variáveis de ambiente

| Variável | Papel | Default |
| --- | --- | --- |
| `DATABASE_URL` | Sobrepõe `database.url` do `settings.yml` — tem precedência total | — |
| `CONFIG_DIR` | Pasta do pack de configuração, se não for `config/` na raiz | `config/` |
| `PROJECT_ID` | Projeto GCP para o Vertex AI — sem ele o chat IA fica desabilitado | — |
| `LOCATION` | Região do Vertex AI | `us-central1` |
| `MODEL_NAME` | Modelo Gemini usado pelo agente | `gemini-2.5-flash` |
| `GOOGLE_CREDENTIALS_JSON` | Credenciais de service account em base64 (alternativa a `credentials.json` na raiz) | — |
| `BACKEND_URL` | URL pública do backend, usada para montar links de artefatos | `http://localhost:8000` |
| `DEMO_RESET` | `true` reseta os dados de demo (`mfg.db`) a cada início | `false` |
| `DEBUG_TOOLS` | `1` loga o payload completo de cada tool call do agente | `0` |
| `MAX_INTERACOES` | Janela de mensagens recentes enviada ao modelo pelo checkpointer | `10` |

### Rodar

```bash
python run.py
# equivalente a: uvicorn main:app --reload --host 0.0.0.0
```

Servidor sobe em `http://localhost:8000`. Docs automáticas do FastAPI em `/docs` (Swagger) e `/redoc`.

Na primeira execução, `db.py` cria e semeia `mfg.db` (dados de demo de manufatura) se ele ainda não existir — isso é independente do banco de domínio configurado em `config/`.

---

## Stack

- **LLM**: Gemini 2.5 Flash (Google Vertex AI)
- **Orquestração**: LangGraph 1.0+ (StateGraph, ToolNode, SqliteSaver)
- **API**: FastAPI + SSE (sse-starlette)
- **Análise**: Python / Pandas / Matplotlib — sandbox via `exec()` em namespace isolado
- **Persistência**: SQLite (histórico de conversa + gráficos + PDFs na mesma base)
- **Threading**: `contextvars.ContextVar` para isolamento de sessão através de thread pools
