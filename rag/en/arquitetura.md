# Agent system architecture

The system uses a multi-agent LangGraph graph: an orchestrator, an analyst sub-agent, a
scheduling sub-agent and a deterministic pipeline that creates and edits scheduled tasks.

## Orchestrator

Central node that receives every user message and decides how to route it:

| Request type | Routing |
| --- | --- |
| Data query (chart, table, report) | `consultar_analista()` — the period goes in as natural language and is resolved inside the tool |
| Conceptual question (what is X, how is Y calculated) | `rag_*()` tools → answers directly |
| Current dashboard (what's on screen, alerts) or an attached image | `get_dashboard_charts()` |
| Create or edit a task or monitor | `criar_tarefa_agendada()` / `editar_tarefa_agendada()` |
| List, pause, resume or delete tasks | `gerenciar_agenda()` |
| Custom panel on the dashboard (add, edit, remove) | `consultar_analista()` → `test_widget_code()` → `add_chart_to_dashboard()`; also `update_widget()` and `delete_dashboard_widget()` |
| Simple question (date, time, greeting) | answers directly |

The orchestrator keeps the full session history via a SQLite checkpointer — it never loses the
context of earlier turns within the same session (the model receives a window with the most
recent interactions). When in doubt between a data query and a scheduling or panel request, it
treats it as a query: it only creates a task or panel when the request uses explicit
scheduling words or asks to pin something to the dashboard. The final answer to the user, in
the user's language, is written by the orchestrator from the concise report the analyst
returns.

## Analyst sub-agent

Triggered by the orchestrator via `consultar_analista()`. Runs analyses in three steps:

1. Skill `dominio.md` — schema, relationships and rules from the configuration pack. It comes
   already loaded in the analyst's prompt (`read_skill()` is only used for other skills)
2. Data query, which enters the environment as a DataFrame. The method depends on the domain
   (`database.modo`):
   - SQL: `executar_sql(query, chave)`
   - GraphQL: `executar_graphql(...)` (rows), `executar_graphql_agregado(...)` (sum, count,
     average, minimum or maximum grouped on the server) and, only if the skill diverges from
     the server, `executar_graphql_schema()` (the real schema)
3. `analisar_dataframe(script)` — processes it with pandas, producing a chart (`result = fig`)
   or a table (`result = df`)

The environment persists between calls within the same session (Jupyter-style) — DataFrames
created in one step remain available in the next (and expire after 10 minutes of inactivity).
Results with more than 100 rows are described (columns, types, first row) instead of shown, to
save context. The analyst only answers with real results from `analisar_dataframe`: it does
not make up or simulate data.

### Sub-agent skills

| File | What it covers |
| --- | --- |
| `dominio.md` (config/) | Analysis skill for the configured domain — schema, joins and rules |
| `task_code_sandbox.md` (skills/) | Rules and examples for the `task_code` sandbox — used when creating tasks and panels |

## Scheduling sub-agent

Triggered by the orchestrator via `gerenciar_agenda()`. It handles operations on tasks that
already exist: list, pause/resume, delete and change fields (name, frequency, time, email).
Creating a task or editing its **code** doesn't go through it — that uses the pipeline below.

## Creating and editing tasks — deterministic pipeline

`criar_tarefa_agendada()` and `editar_tarefa_agendada()` run a fixed flow inside the tool
itself; the orchestrator doesn't drive the steps:

1. Extracts the parameters from the request (name, frequency, time, period, report or monitor
   type, condition) and creates the task as `draft`
2. The analyst queries real data and returns the context (query, columns, sample, filters)
3. A generator writes the `task_code`
4. The code is tested in the sandbox (the preview appears in the chat); if it fails, it is
   fixed, in up to 3 attempts
5. If the request mentions analysis and the code doesn't include it, the generator adds it
   (up to 2 attempts)
6. The validated code is saved and the task becomes `active` — when editing, the previous
   version is archived for rollback

When editing, the flow starts from the task's current code: it applies the requested change
and continues from step 4. If the flow fails for good, or the user stops the response, the
draft is discarded and nothing gets scheduled. A task stays in `draft`, never running, until a
valid `task_code` is saved (test it first with `test_task_code`). `instructions` is only a
reference outline used to write that code — on its own it does not activate the task.

## Running tasks

The daemon checks tasks every few seconds and runs the saved `task_code` directly
(`def run(from_date, to_date, ctx)`) — **no LLM at scheduling time** (the only exception is
`ctx.analisar`, below). On failure, the task is rescheduled with a growing wait (5, 15 and 60
minutes), in up to 3 new attempts; once exhausted, it goes to `error` and waits for review.
Each run has a maximum of 10 minutes. Run history and code versions appear in the Tasks panel.

### Monitors

- **Simple** (one metric, one comparison — e.g. "orders of the day >= 160"): the daemon
  evaluates the condition *before* running the code, and the `task_code` only returns the
  current value. In SQL domains the condition is a query; in GraphQL domains, a JSON
  specification
- **Compound** (AND/OR, several metrics or entities — e.g. "JAG > 50 and BPL < 10"): the
  `task_code` itself evaluates the whole condition and calls `ctx.alert(triggered, message)`
  once per run

In both cases the alert only fires on the transition from "not met" to "met" and arms again
when the condition stops being true. It appears in the dashboard's Alerts bell.

### The `ctx` object

The `ctx` object injected into `run()` provides:

| Method | What it does |
| --- | --- |
| `ctx.sql(query)` | Runs a SELECT against the configured database and returns a list of dicts (SQL domains only) |
| `ctx.graphql_table(table, where, ...)` | Fetches raw rows via GraphQL and returns a list of dicts (GraphQL domains only) |
| `ctx.graphql_aggregate(table, group_by, metric_field, metric_op, ...)` | Aggregates on the server (SUM, COUNT, AVG, MIN or MAX by a column) via GraphQL (GraphQL domains only) |
| `ctx.api(url)` | Calls the `/alerts` endpoint (notification bell) and returns the data as a list |
| `ctx.today()` | Returns today's date in `YYYY-MM-DD` format |
| `ctx.date_range(days=N)` | Returns `(from_date, to_date)` for the last N days |
| `ctx.save_chart(fig)` | Saves a matplotlib figure and returns a `[chart:uuid]` token |
| `ctx.generate_pdf(titulo, conteudo)` | Generates a PDF and returns a `[pdf:uuid]` token |
| `ctx.generate_excel(df, nome)` | Generates an Excel file (one DataFrame, or a dict `{sheet: DataFrame}`) and returns an `[excel:uuid]` token |
| `ctx.analisar(fatos, foco, fallback)` | Analysis text written by the AI from facts the code already computed; every number it cites is checked against the facts, and if anything fails the `fallback` is used |
| `ctx.notify(msg, value, threshold)` | Fires a notification on the dashboard (in report tasks; monitors notify on their own) |
| `ctx.alert(triggered, message, value, threshold)` | Signals the result of a compound monitor; notifies only on the transition to "met" |

## Dashboard panels

Each panel is Python code (`def run(from_date, to_date, ctx)`) that returns a chart
configuration; it is run again on every refresh, with the period calculated from the window
stored in the panel (e.g. "last 30 days"). **Default** panels come from the configuration pack
(one file per language) and cannot be removed; **custom** panels are created via chat, saved in
the database and can be shared by their owner with other users. The code runs in the same kind
of sandbox as tasks, with a smaller set of libraries.

## Interface

The answer arrives in real time (SSE streaming): the reasoning and each step — query, analysis,
code generation — show up as they happen, and the "Stop response" button interrupts the agent
(cancellation is cooperative: the step in progress finishes before stopping). Input can be
text, voice (transcribed by AI) or attached screen captures. The interface, the answers and the
domain content follow the chosen language (Portuguese, English or Chinese).
