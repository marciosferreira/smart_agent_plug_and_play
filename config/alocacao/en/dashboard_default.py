"""Default dashboard panels for the allocation domain (dominio_alocacao.md).

Loaded dynamically by db_config.dashboard_default_widgets() — just a Python
file with a `WIDGETS` list, reloaded automatically whenever this file changes
(no restart needed). Each item becomes a fixed panel on the dashboard, always
present and not removable by the user, run through the same runner
(scheduler/widget_runner) as panels created via chat — the only difference is
that this code is never written to the database, so it survives the nightly
demo reset and is versioned in git.

Shape of each item (same shape accepted by add_chart_to_dashboard):
  id:           short, stable string (used in the URL /dashboard-widgets/default:<id>/data
                — don't reuse it across panels, but title/description can be
                renamed freely without breaking anything).
  title:        title shown on the card.
  description:  short description (optional).
  code:         `def run(from_date, to_date, ctx)` returning a Chart.js dict.
  range_unit:   "days" or "months" (default: "days").
  range_value:  quantity of the unit above (default: 7).

To add/edit a panel here, test the `code` first via test_widget_code
in the chat (same validation the agent uses) and paste in the already-validated result.
"""

WIDGETS = [
    {
        "id": "alocacao_por_key_account",
        "title": "Allocation by Key Account — current quarter",
        "description": "Total allocated (W1-W5) by Key Account in the current quarter",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.sql("""
        SELECT key_account_code,
               SUM(allocation_W1 + allocation_W2 + allocation_W3 + allocation_W4 + allocation_W5) AS total
        FROM ka_deal_allocation
        WHERE month_status IN ('done', 'ongoing')
        GROUP BY key_account_code
        ORDER BY total DESC
        LIMIT 10
    """)
    labels = [r['key_account_code'] for r in rows]
    values = [r['total'] for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Allocated', 'data': values}]
        }
    }
''',
    },
    {
        "id": "woi_por_faixa_de_risco",
        "title": "WOI by Risk Band — current quarter",
        "description": "Distribution of records by WOI risk band in the current quarter",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.sql("""
        SELECT CASE
                 WHEN woi < 10 THEN 'Critical (<10)'
                 WHEN woi < 15 THEN 'Medium (10-15)'
                 ELSE 'Healthy (>=15)'
               END AS faixa,
               COUNT(*) AS total
        FROM ka_deal_allocation
        WHERE month_status IN ('done', 'ongoing')
        GROUP BY faixa
    """)
    labels = [r['faixa'] for r in rows]
    values = [r['total'] for r in rows]
    return {
        'type': 'doughnut',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Records', 'data': values}]
        }
    }
''',
    },
]
