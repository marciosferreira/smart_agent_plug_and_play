# Agent capabilities

The agent answers questions in natural language about the data in the configured database
(see the pack in `config/` — name, schema and business rules all come from there).

## Simple architecture

| Layer | Responsibility |
|--------|-----------------|
| `dominio.md` (config/) | **The only analysis skill** — every data question runs direct SQL against the configured database |

## How the agent answers data questions

```
User asks a question
      ↓
Orchestrator → consultar_analista()
      ↓
Sub-agent reads dominio.md (configuration pack)
      ↓
Writes and runs SQL against the configured database
      ↓
Processes the result with pandas
      ↓
Answers with a table or a chart
```

## Supported analysis types

**Aggregations:** totals, averages, counts by any dimension in the schema

**Rankings:** top-N by any metric

**Comparisons:** between periods, categories or groups

**Cross-references:** any combination of tables via JOIN (keys described in dominio.md)

**Concepts:** questions about the meaning of the domain's metrics and business rules

## Output formats

- Markdown table
- Chart (bar, line, pie, scatter)
- PDF with analysis and charts
- Downloadable Excel spreadsheet
- Persistent custom panel on the dashboard

## Natural-language periods accepted

"today", "yesterday", "this week", "last N days", "this month", "last month", explicit
dates (e.g. "from 05/01 to 05/28") — plus any domain-specific periods defined in the
configuration pack (e.g. quarters, the business's own period labels).

## Scheduled tasks

The agent can create tasks that run automatically.

**Recurring reports:**

- "Every Monday at 8am send me the summary of <metric>"
- "Every day at 7am generate a PDF with <analysis>"
- "On the first of every month export the data to Excel"

**Threshold monitors:**

- "Let me know if <metric> goes above <N>"
- "Alert me when <condition> happens"

Frequencies: `once`, `daily`, `weekly`, `monthly`, `every_Xm`, `every_Xh`, `every_Xd`

**Management:** "List the tasks", "Pause task 003", "Delete task 001"
