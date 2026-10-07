# Agent capabilities

The agent answers questions in natural language (Portuguese, English or Chinese) about the data
in the configured domain (see the pack in `config/` — name, schema and business rules all come
from there). It only reads data: it never changes the database it queries.

## Simple architecture

| Layer | Responsibility |
|--------|-----------------|
| `dominio.md` (config/) | **The only analysis skill** — describes the domain's schema and rules; queries go straight to the configured database, through SQL or GraphQL depending on the domain |

## How the agent answers data questions

```text
User asks a question
      ↓
Orchestrator → consultar_analista()
      ↓
Analyst sub-agent (with the dominio.md skill already loaded)
      ↓
Queries the configured database (SQL or GraphQL, depending on the domain)
      ↓
Processes the result with pandas
      ↓
Answers with a table and/or a chart
```

## Supported analysis types

**Aggregations:** totals, averages, counts by any dimension in the schema

**Rankings:** top-N by any metric

**Comparisons:** between periods, categories or groups

**Cross-references:** combining tables through the keys described in dominio.md (JOINs in SQL
domains; in GraphQL domains, one-level joins per query and cross-referencing in pandas)

**Concepts:** questions about the meaning of the domain's metrics and business rules

## Output formats

- Markdown table
- Chart (bar, line, pie, scatter, among others)
- PDF with analysis and charts
- Downloadable Excel spreadsheet
- Persistent custom panel on the dashboard

Without an explicit request for a chart, a data answer comes as a table (or as text when the
result is a single value). Ask for a "chart" to get the visualization, or "chart and table"
for both.

## Natural-language periods accepted

"today", "yesterday", "this week", "last week", "last N days/weeks/months", "this month",
"last month", "this year", "last year", "this quarter", "last quarter", "the whole period",
a specific month (e.g. "May 2026") and explicit dates (e.g. "from 05/01/2026 to 05/31/2026")
— plus any domain-specific periods defined in the configuration pack (e.g. the business's
quarters and fiscal year, its own period labels).

## After the answer

You can ask for adjustments ("the same chart, but in gray", "only the top 5") and ask about a
chart that was already generated ("why is there a spike here?") — the agent can look at the
image. Every data answer includes an **Analysis parameters** block with the period and filters
actually used, flagging what was assumed.

## Dashboard

The dashboard is a grid of panels (charts that are recalculated on every refresh):

- **Default panels** — ship with the domain pack and cannot be removed
- **Custom panels** — the agent creates, edits and removes them on request, e.g. "add a panel
  of <metric> by <dimension> for the last 30 days to the dashboard" (it asks for confirmation
  before pinning)

Chart types: bar, line, pie, doughnut, radar, polar area, scatter, bubble and heatmap. Panels
can be reordered by dragging, and the owner of a custom panel can share it with other users
from the panel's own button.

Questions about the screen: the agent sees the alerts, artifacts and tasks listed and analyzes
screen captures you attach to the chat ("Select an area of the screen" button, up to 3 images).

## Conversation

- **Voice:** record audio, listen, then discard or send it; it is transcribed and the
  transcript goes out as your message
- **Stop:** the "Stop response" button interrupts a response in progress
- **Conversations:** each user has several conversations, which can be resumed later
- **Language:** the agent replies in the language chosen in the interface (Portuguese,
  English or Chinese)

## Scheduled tasks

The agent can create tasks that run automatically. When creating one, it queries the real data,
writes the task's code, tests it and only then saves it (if the test fails it tries to fix it;
if it can't, the task is not created). After that, the task runs on schedule without needing
the agent — the result is computed by code, straight from the data (the only exception is the
written analysis, when requested).

**Recurring reports:**

- "Every Monday at 8am generate the summary of <metric>"
- "Every day at 7am generate a PDF with <analysis>"
- "On the first of every month export the data to Excel"
- "Every week generate a report with analysis and insights" — the code computes the numbers
  and the AI writes the interpretation, which is checked against those numbers

Results (PDF, chart, Excel) show up in the **Artifacts** panel. The system does not send emails
on its own.

**Threshold monitors:**

- "Let me know if <metric> goes above <N>" — simple condition (one metric, one comparison)
- "Alert me when <metric A> > <X> and <metric B> < <Y>" — compound condition (AND/OR, several
  metrics or entities)

Alerts appear in the dashboard's **Alerts** bell. The alert fires when the condition goes from
"not met" to "met" — it doesn't repeat on every check — and arms again after the condition
stops being true. Without a requested frequency, the monitor checks every 5 minutes (default
minimum interval: 5 minutes). When it creates a monitor, the agent shows **How I interpreted
your request** (condition, frequency, data window, chosen metric and aggregation) so you can
correct anything that differs from what you wanted.

Frequencies: `once`, `daily`, `weekly`, `monthly`, `every_Xm`, `every_Xh`, `every_Xd`

**Management:** "List the tasks", "Pause task 003", "Resume task 003", "Delete task 001",
"Edit task 003: change the limit to 50" (the agent changes the code, tests and saves it,
keeping the previous version). In the **Tasks** panel you can also run now (▶), turn
notifications on or off (except for monitors, which always notify), view the code and run
history, and edit via chat.

## Limits

- It only reads data: it does not change the domain database
- It does not make data up: with no real data for the requested filter, the agent says so (and
  may show the closest match, making clear it differs from what was asked)
- Very large results are summarized instead of shown in full — ask for a top-N or an
  aggregation
- It does not send emails on its own
