# Arquitetura do sistema de agentes

O sistema usa um grafo multi-agente LangGraph com três agentes cooperando.

## Orquestrador

Nó central que recebe todas as mensagens do usuário e decide como rotear:

| Tipo de pedido                                         | Roteamento                                        |
|--------------------------------------------------------|---------------------------------------------------|
| Consulta de dados (gráfico, tabela, relatório)         | `calcular_periodo()` → `consultar_analista()`     |
| Pergunta conceitual (o que é X, como se calcula Y)     | tools `rag_*()` → responde direto                 |
| Dashboard atual (o que aparece na tela, KPIs, alertas) | `get_dashboard_charts()`                          |
| Agendamento (criar, editar, listar, pausar, deletar)   | `gerenciar_agenda()`                              |
| Pergunta simples (data, hora, saudação)                | responde direto                                   |

O orquestrador mantém histórico completo da sessão via checkpointer SQLite — nunca perde
o contexto de conversas anteriores dentro da mesma sessão.

Após qualquer ação de edição de código (`get_task_code`), um nó guardião verifica
automaticamente se `save_task_code` foi chamado. Se não, injeta uma correção forçando
o orquestrador a completar o fluxo.

## Sub-agente analista

Ativado pelo orquestrador via `consultar_analista()`. Executa análises em três etapas obrigatórias:

1. `read_skill('dominio.md')` — lê schema, relacionamentos e regras do pack de configuração
2. `executar_sql(query, chave)` — executa SELECT no banco configurado e injeta DataFrame no ambiente
3. `analisar_dataframe(script)` — processa com pandas, gera gráfico (`result = fig`) ou tabela (`result = df`)

O ambiente persiste entre chamadas na mesma sessão (estilo Jupyter) — DataFrames criados em um passo ficam disponíveis nos seguintes.

### Skill do sub-agente

| Arquivo | O que cobre |
| --- | --- |
| `dominio.md` (config/) | Única skill de análise — schema, joins e regras do domínio configurado |

## Sub-agente de scheduling

Ativado pelo orquestrador via `gerenciar_agenda()`. Responsável exclusivamente por
operações em tarefas agendadas: criar, listar, editar, pausar, deletar, salvar e testar código.

O daemon executa `task_code` diretamente (`def run(from_date, to_date, ctx)`) — sem LLM em tempo
de agendamento. Uma tarefa fica em `draft`, sem execução, até que `save_task_code` salve um
`task_code` válido (teste antes com `test_task_code`). `instructions` é só o passo a passo de
referência usado para escrever esse código — sozinho não ativa a tarefa.

O objeto `ctx` injetado no `run()` oferece:

| Método | O que faz |
| --- | --- |
| `ctx.api(url)` | Chama o endpoint `/alerts` (sino de notificações) e retorna os dados como list |
| `ctx.sql(query)` | Executa SELECT no banco configurado e retorna list de dicts |
| `ctx.today()` | Retorna a data atual no formato `YYYY-MM-DD` |
| `ctx.date_range(days=N)` | Retorna `(from_date, to_date)` para os últimos N dias |
| `ctx.save_chart(fig)` | Salva figura matplotlib e retorna token `[chart:uuid]` |
| `ctx.generate_pdf(titulo, conteudo)` | Gera PDF e retorna token `[pdf:uuid]` |
| `ctx.generate_excel(df, nome)` | Gera Excel e retorna token `[excel:uuid]` |
| `ctx.notify(msg, value, threshold)` | Dispara notificação no dashboard |
