# 面板标题/描述和 Chart.js 图表标签（下方）已翻译——这些是屏幕上实际显示的中文内容。
# docstring 和代码注释按设计保留葡萄牙语（内部内容，不会展示给用户）。
"""Painéis padrão do dashboard do domínio de alocação (dominio_alocacao.md).

Lido dinamicamente por db_config.dashboard_default_widgets() — é só um arquivo
Python com uma lista `WIDGETS`, recarregado automaticamente quando este arquivo
muda (sem restart). Cada item vira um painel fixo no dashboard, sempre presente
e não removível pelo usuário, executado com o mesmo runner (scheduler/widget_runner)
dos painéis criados via chat — a única diferença é que este código nunca é
gravado no banco, então sobrevive ao reset noturno de demo e é versionado no git.

Formato de cada item (mesmo shape aceito por add_chart_to_dashboard):
  id:           string curta e estável (usada na URL /dashboard-widgets/default:<id>/data
                — não reaproveite entre painéis, mas pode renomear title/description
                livremente sem quebrar nada).
  title:        título exibido no card.
  description:  descrição curta (opcional).
  code:         `def run(from_date, to_date, ctx)` retornando dict Chart.js.
  range_unit:   "days" ou "months" (padrão: "days").
  range_value:  quantidade da unidade acima (padrão: 7).

Para adicionar/editar um painel aqui, teste o `code` antes via test_widget_code
no chat (mesma validação usada pelo agente) e cole o resultado já validado.
"""

WIDGETS = [
    {
        "id": "previsao_proximo_quarter",
        "title": '下季度需求与风险 — 预测模型',
        "description": '关键客户近 12 个月的请求量、模型对下季度的需求预测，以及其中处于高/中风险（可能无法完全满足）请求中的需求量（为风险敞口量，并非缺口量）',
        "range_unit": "months",
        "range_value": 12,
        "code": '''def run(from_date, to_date, ctx):
    # 不含 Retail：它不下请求，只接收剩余产能。
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
                dict(label="关键客户请求量（实际）", data=[float(v) for v in ultimos["pedidos"]] + vazio,
                     stack="real", order=1, pointRadius=2, **linha("#2a78d6", {})),
                dict(label="预测需求（模型）", data=[None] * (n - 1) + [float(ultimos["pedidos"].iloc[-1])] + prevista,
                     stack="prevista", order=0, pointRadius=3, **linha("#7c3aed", dict(borderDash=[6, 4]))),
                dict(label="高风险请求中的需求（≥60%）", data=[None] * n + alto, stack="risco", order=3,
                     backgroundColor="rgba(220, 38, 38, 0.75)"),
                dict(label="中风险请求中的需求（30–60%）", data=[None] * n + medio, stack="risco", order=3,
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
        "title": '下季度风险请求数 — 预测模型',
        "description": '下季度每个月预测的请求数（关键客户 × 产品），按无法完全满足的风险分组',
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    # 一个请求 = 某关键客户在该月对某产品的请求。不含 Retail（它不下请求）。
    prev = pd.DataFrame(ctx.pack("previsao_proximo_quarter"))
    meses = sorted(prev["year_month"].unique())
    conta = lambda faixa: [int(((prev["year_month"] == m) & (prev["risco"] == faixa)).sum()) for m in meses]
    eixo = dict(ticks=dict(color="#64748b", font=dict(size=10)), grid=dict(color="#e2e8f0"), stacked=True)
    return {
        "type": "bar",
        "data": {
            "labels": [m[:7] for m in meses],
            "datasets": [
                dict(label="高风险（≥60%）", data=conta("alto"), backgroundColor="rgba(220, 38, 38, 0.75)"),
                dict(label="中风险（30–60%）", data=conta("médio"), backgroundColor="rgba(245, 158, 11, 0.7)"),
                dict(label="低风险（<30%）", data=conta("baixo"), backgroundColor="#cbd5e1"),
            ],
        },
        "options": {"scales": {"x": eixo, "y": dict(eixo, beginAtZero=True)}},
    }
''',
    },
    {
        "id": "alocacao_por_key_account",
        "title": "按 Key Account 分配 — 当前季度",
        "description": "当前进行中季度内按 Key Account 汇总的分配量（W1-W5）",
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
            'datasets': [{'label': '已分配量', 'data': values}]
        }
    }
''',
    },
    {
        "id": "woi_por_faixa_de_risco",
        "title": "按风险区间划分的 WOI — 当前季度",
        "description": "当前进行中季度内按 WOI 风险区间划分的记录分布",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.sql("""
        SELECT CASE
                 WHEN woi < 10 THEN '危险（<10）'
                 WHEN woi < 15 THEN '中等（10-15）'
                 ELSE '健康（>=15）'
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
            'datasets': [{'label': '记录数', 'data': values}]
        }
    }
''',
    },
]
