# Agent system architecture

The system uses a multi-agent LangGraph graph with three agents cooperating.

## Orchestrator

Central node that receives every user message and decides how to route it:

| Request type                                            | Routing                                            |
|-----------------------------------------------------------|-----------------------------------------------------|
| Data query (chart, table, report)                       | `calcular_periodo()` → `consultar_analista()`      |
| Conceptual question (what is X, how is Y calculated)    | `rag_*()` tools → answers directly                 |
| Current dashboard (what's on screen, KPIs, alerts)       | `get_dashboard_charts()`                           |
| Scheduling (create, edit, list, pause, delete)           | `gerenciar_agenda()`                               |
| Simple question (date, time, greeting)                   | answers directly                                   |

The orchestrator keeps the full session history via a SQLite checkpointer — it never loses
the context of earlier turns within the same session.

After any code-editing action (`get_task_code`), a guardian node automatically checks
whether `save_task_code` was called. If not, it injects a correction forcing the
orchestrator to complete the flow.

## Analyst sub-agent

Triggered by the orchestrator via `consultar_analista()`. Runs analyses in three mandatory steps:

1. `read_skill('dominio.md')` — reads schema, relationships and rules from the configuration pack
2. `executar_sql(query, chave)` — runs a SELECT against the configured database and injects a DataFrame into the environment
3. `analisar_dataframe(script)` — processes it with pandas, producing a chart (`result = fig`) or a table (`result = df`)

The environment persists between calls within the same session (Jupyter-style) — DataFrames created in one step remain available in the next.

### Sub-agent skill

| File | What it covers |
| --- | --- |
| `dominio.md` (config/) | The only analysis skill — schema, joins and rules for the configured domain |

## Scheduling sub-agent

Triggered by the orchestrator via `gerenciar_agenda()`. Responsible exclusively for
operations on scheduled tasks: create, list, edit, pause, delete, save and test code.

The daemon runs `task_code` directly (`def run(from_date, to_date, ctx)`) — no LLM at
scheduling time. A task stays in `draft`, never running, until `save_task_code` saves a
valid `task_code` (test it first with `test_task_code`). `instructions` is only a reference
outline used to write that code — on its own it does not activate the task.

The `ctx` object injected into `run()` provides:

| Method | What it does |
| --- | --- |
| `ctx.api(url)` | Calls the `/alerts` endpoint (notification bell) and returns the data as a list |
| `ctx.sql(query)` | Runs a SELECT against the configured database and returns a list of dicts |
| `ctx.today()` | Returns today's date in `YYYY-MM-DD` format |
| `ctx.date_range(days=N)` | Returns `(from_date, to_date)` for the last N days |
| `ctx.save_chart(fig)` | Saves a matplotlib figure and returns a `[chart:uuid]` token |
| `ctx.generate_pdf(titulo, conteudo)` | Generates a PDF and returns a `[pdf:uuid]` token |
| `ctx.generate_excel(df, nome)` | Generates an Excel file and returns an `[excel:uuid]` token |
| `ctx.notify(msg, value, threshold)` | Fires a notification on the dashboard |
