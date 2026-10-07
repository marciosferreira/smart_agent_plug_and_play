# Arquitetura do sistema de agentes

O sistema usa um grafo multi-agente LangGraph: um orquestrador, um sub-agente analista, um
sub-agente de scheduling e um pipeline determinístico que cria e edita tarefas agendadas.

## Orquestrador

Nó central que recebe todas as mensagens do usuário e decide como rotear:

| Tipo de pedido | Roteamento |
| --- | --- |
| Consulta de dados (gráfico, tabela, relatório) | `consultar_analista()` — o período vai em linguagem natural e é resolvido dentro da tool |
| Pergunta conceitual (o que é X, como se calcula Y) | tools `rag_*()` → responde direto |
| Dashboard atual (o que aparece na tela, alertas) ou imagem anexada | `get_dashboard_charts()` |
| Criar ou editar uma tarefa ou monitor | `criar_tarefa_agendada()` / `editar_tarefa_agendada()` |
| Listar, pausar, retomar ou apagar tarefas | `gerenciar_agenda()` |
| Painel customizado no dashboard (adicionar, editar, remover) | `consultar_analista()` → `test_widget_code()` → `add_chart_to_dashboard()`; também `update_widget()` e `delete_dashboard_widget()` |
| Pergunta simples (data, hora, saudação) | responde direto |

O orquestrador mantém o histórico completo da sessão via checkpointer SQLite — nunca perde o
contexto de conversas anteriores dentro da mesma sessão (ao modelo vai uma janela com as
interações mais recentes). Na dúvida entre uma consulta de dados e um agendamento ou painel, ele
trata como consulta: só cria tarefa ou painel quando o pedido usa palavras explícitas de
agendamento ou de fixar no dashboard. A resposta final ao usuário, no idioma dele, é redigida
pelo orquestrador a partir do relatório conciso que o analista devolve.

## Sub-agente analista

Ativado pelo orquestrador via `consultar_analista()`. Executa análises em três etapas:

1. Skill `dominio.md` — schema, relacionamentos e regras do pack de configuração. Já vem
   carregada no prompt do analista (`read_skill()` só é usado para outras skills)
2. Consulta aos dados, que entra no ambiente como DataFrame. O método depende do domínio
   (`database.modo`):
   - SQL: `executar_sql(query, chave)`
   - GraphQL: `executar_graphql(...)` (linhas), `executar_graphql_agregado(...)` (soma,
     contagem, média, mínimo ou máximo agrupados no servidor) e, só se a skill divergir do
     servidor, `executar_graphql_schema()` (schema real)
3. `analisar_dataframe(script)` — processa com pandas, gerando gráfico (`result = fig`) ou
   tabela (`result = df`)

O ambiente persiste entre chamadas na mesma sessão (estilo Jupyter) — DataFrames criados em um
passo ficam disponíveis nos seguintes (e expiram após 10 minutos sem uso). Resultados com mais
de 100 linhas são descritos (colunas, tipos, primeira linha) em vez de exibidos, para poupar
contexto. O analista só responde com resultados reais de `analisar_dataframe`: não inventa nem
simula dados.

### Skills do sub-agente

| Arquivo | O que cobre |
| --- | --- |
| `dominio.md` (config/) | Skill de análise do domínio configurado — schema, joins e regras |
| `task_code_sandbox.md` (skills/) | Regras e exemplos do sandbox de `task_code` — usada ao criar tarefas e painéis |

## Sub-agente de scheduling

Ativado pelo orquestrador via `gerenciar_agenda()`. Cuida das operações sobre tarefas já
criadas: listar, pausar/retomar, apagar e alterar campos (nome, frequência, horário, e-mail).
Criar uma tarefa ou editar o **código** dela não passa por ele — usa o pipeline abaixo.

## Criação e edição de tarefas — pipeline determinístico

`criar_tarefa_agendada()` e `editar_tarefa_agendada()` executam um fluxo fixo dentro da própria
tool; o orquestrador não conduz os passos:

1. Extrai os parâmetros do pedido (nome, frequência, horário, período, tipo relatório ou
   monitor, condição) e cria a tarefa em `draft`
2. O analista consulta dados reais e devolve o contexto (query, colunas, amostra, filtros)
3. Um gerador escreve o `task_code`
4. O código é testado no sandbox (o preview aparece no chat); se falhar, é corrigido, em até
   3 tentativas
5. Se o pedido cita análise e o código não a inclui, o gerador a acrescenta (até 2 tentativas)
6. O código validado é salvo e a tarefa vira `active` — na edição, a versão anterior é
   arquivada para rollback

Na edição, o fluxo parte do código atual da tarefa: aplica a modificação pedida e segue do
passo 4. Se o fluxo falhar de forma definitiva, ou o usuário parar a resposta, o rascunho é
descartado e nada fica agendado. Uma tarefa fica em `draft`, sem execução, até que um
`task_code` válido seja salvo (teste antes com `test_task_code`). `instructions` é só um passo
a passo de referência usado para escrever esse código — sozinho não ativa a tarefa.

## Execução das tarefas

O daemon verifica as tarefas a cada poucos segundos e executa o `task_code` salvo diretamente
(`def run(from_date, to_date, ctx)`) — **sem LLM em tempo de agendamento** (a única exceção é
`ctx.analisar`, abaixo). Em caso de falha, a tarefa é reagendada com espera crescente (5, 15 e
60 minutos), em até 3 novas tentativas; esgotadas, vai para `error` e aguarda revisão. Cada
execução tem no máximo 10 minutos. O histórico de execuções e as versões do código aparecem no
painel Tarefas.

### Monitores

- **Simples** (uma métrica, uma comparação — ex: "pedidos do dia >= 160"): o daemon avalia a
  condição *antes* de rodar o código, e o `task_code` só devolve o valor atual. Em domínios SQL
  a condição é uma query; em domínios GraphQL, uma especificação JSON
- **Composto** (E/OU, várias métricas ou entidades — ex: "JAG > 50 e BPL < 10"): o próprio
  `task_code` avalia a condição inteira e chama `ctx.alert(triggered, message)` uma vez por
  execução

Nos dois casos o alerta só sai na transição de "não atingida" para "atingida" e volta a valer
quando a condição deixa de ser verdadeira. Ele aparece no sino de Alertas do dashboard.

### O objeto `ctx`

O objeto `ctx` injetado no `run()` oferece:

| Método | O que faz |
| --- | --- |
| `ctx.sql(query)` | Executa um SELECT no banco configurado e retorna uma lista de dicts (só em domínios SQL) |
| `ctx.graphql_table(table, where, ...)` | Busca linhas cruas via GraphQL e retorna uma lista de dicts (só em domínios GraphQL) |
| `ctx.graphql_aggregate(table, group_by, metric_field, metric_op, ...)` | Agrega no servidor (SUM, COUNT, AVG, MIN ou MAX por uma coluna) via GraphQL (só em domínios GraphQL) |
| `ctx.api(url)` | Chama o endpoint `/alerts` (sino de notificações) e retorna os dados como list |
| `ctx.today()` | Retorna a data atual no formato `YYYY-MM-DD` |
| `ctx.date_range(days=N)` | Retorna `(from_date, to_date)` para os últimos N dias |
| `ctx.save_chart(fig)` | Salva figura matplotlib e retorna token `[chart:uuid]` |
| `ctx.generate_pdf(titulo, conteudo)` | Gera PDF e retorna token `[pdf:uuid]` |
| `ctx.generate_excel(df, nome)` | Gera Excel (um DataFrame, ou um dict `{aba: DataFrame}`) e retorna token `[excel:uuid]` |
| `ctx.analisar(fatos, foco, fallback)` | Texto de análise escrito pela IA a partir de fatos que o código já calculou; todo número citado é conferido contra os fatos, e se algo falhar entra o `fallback` |
| `ctx.notify(msg, value, threshold)` | Dispara notificação no dashboard (em tarefas de relatório; monitores notificam sozinhos) |
| `ctx.alert(triggered, message, value, threshold)` | Sinaliza o resultado de um monitor composto; notifica só na transição para "atingida" |

## Painéis do dashboard

Cada painel é um código Python (`def run(from_date, to_date, ctx)`) que devolve a configuração
de um gráfico; ele é executado de novo a cada atualização, com o período calculado a partir da
janela guardada no painel (ex: "últimos 30 dias"). Os painéis **padrão** vêm do pack de
configuração (um arquivo por idioma) e não podem ser removidos; os **customizados** são criados
via chat, salvos no banco e podem ser compartilhados pelo dono com os demais usuários. O código
roda no mesmo tipo de sandbox das tarefas, com um conjunto menor de bibliotecas.

## Interface

A resposta chega em tempo real (streaming SSE): o raciocínio e cada etapa — consulta, análise,
geração de código — aparecem enquanto acontecem, e o botão "Parar resposta" interrompe o agente
(o cancelamento é cooperativo: a etapa em curso termina antes de parar). A entrada pode ser
texto, voz (transcrita por IA) ou recortes de tela anexados. A interface, as respostas e o
conteúdo de domínio seguem o idioma escolhido (português, inglês ou chinês).
