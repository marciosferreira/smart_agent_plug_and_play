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
        "id": "previsao_proximo_quarter",
        "title": 'Next-quarter demand and risk — forecast model',
        "description": 'Key Account requests over the last 12 months, the demand the model forecasts for the next quarter and how much of it is in requests at high or medium risk of not being fully met (volume exposed to risk, not the shortfall)',
        "range_unit": "months",
        "range_value": 12,
        "code": '''def run(from_date, to_date, ctx):
    # Retail is left out: it places no requests and takes the leftover capacity.
    hist = pd.DataFrame(ctx.sql("""
        SELECT year_month, SUM(initial_req_qty) AS pedidos
        FROM ka_deal_allocation
        WHERE key_account_code <> 'RETAIL_BRA' AND month_status IN ('closed', 'done', 'ongoing')
        GROUP BY year_month
        ORDER BY year_month
    """))
    # O modelo treinado do pack (ctx.pack): uma linha por KA x produto x mês do próximo quarter.
    prev = pd.DataFrame(ctx.pack("previsao_proximo_quarter"))
    # A linha prevista compara com o pedido inicial; o rollover estimado do 1º mês fica nas barras.
    prev["inicial"] = prev["demanda_prevista"] - prev["rollover_estimado"]
    meses_prev = sorted(prev["year_month"].unique())
    soma = lambda d: [float(d[d["year_month"] == m]["demanda_prevista"].sum()) for m in meses_prev]
    prevista = [float(prev[prev["year_month"] == m]["inicial"].sum()) for m in meses_prev]
    alto, medio = soma(prev[prev["risco"] == "alto"]), soma(prev[prev["risco"] == "médio"])

    ultimos = hist.tail(12)
    meses = list(ultimos["year_month"]) + meses_prev
    n, k = len(ultimos), len(meses_prev)
    vazio = [None] * k
    linha = lambda cor, extra: dict(type="line", borderColor=cor, backgroundColor=cor, tension=0.25, **extra)
    eixo = dict(ticks=dict(color="#64748b", font=dict(size=10)), grid=dict(color="#e2e8f0"), stacked=True)
    return {
        "type": "bar",
        "data": {
            "labels": [m[:7] for m in meses],
            "datasets": [
                dict(label="Key Account requests (actual)", data=[float(v) for v in ultimos["pedidos"]] + vazio,
                     stack="real", order=1, pointRadius=2, **linha("#2a78d6", {})),
                dict(label="Forecast demand (model)", data=[None] * (n - 1) + [float(ultimos["pedidos"].iloc[-1])] + prevista,
                     stack="prevista", order=0, pointRadius=3, **linha("#7c3aed", dict(borderDash=[6, 4]))),
                dict(label="Demand in requests at high risk (≥60%)", data=[None] * n + alto, stack="risco", order=3,
                     backgroundColor="rgba(220, 38, 38, 0.75)"),
                dict(label="Demand in requests at medium risk (30–60%)", data=[None] * n + medio, stack="risco", order=3,
                     backgroundColor="rgba(245, 158, 11, 0.7)"),
            ],
        },
        "options": {
            "interaction": {"mode": "index", "intersect": False},
            "scales": {"x": eixo, "y": dict(eixo, beginAtZero=True)},
        },
    }
''',
    },
    {
        "id": "pedidos_em_risco_proximo_quarter",
        "title": 'Requests at risk next quarter — forecast model',
        "description": 'Number of requests (Key Account × product) forecast for each month of the next quarter, by risk of not being fully met',
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    # One request = one Key Account x product in the month. Retail is left out (it places no requests).
    prev = pd.DataFrame(ctx.pack("previsao_proximo_quarter"))
    meses = sorted(prev["year_month"].unique())
    conta = lambda faixa: [int(((prev["year_month"] == m) & (prev["risco"] == faixa)).sum()) for m in meses]
    eixo = dict(ticks=dict(color="#64748b", font=dict(size=10)), grid=dict(color="#e2e8f0"), stacked=True)
    return {
        "type": "bar",
        "data": {
            "labels": [m[:7] for m in meses],
            "datasets": [
                dict(label="High risk (≥60%)", data=conta("alto"), backgroundColor="rgba(220, 38, 38, 0.75)"),
                dict(label="Medium risk (30–60%)", data=conta("médio"), backgroundColor="rgba(245, 158, 11, 0.7)"),
                dict(label="Low risk (<30%)", data=conta("baixo"), backgroundColor="#cbd5e1"),
            ],
        },
        "options": {"scales": {"x": eixo, "y": dict(eixo, beginAtZero=True)}},
    }
''',
    },
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
