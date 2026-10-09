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
        "title": 'Demanda e risco do próximo quarter — modelo de previsão',
        "description": 'Pedidos das KAs nos últimos 12 meses, a demanda que o modelo prevê para o próximo quarter e quanto dela está em pedidos com risco alto ou médio de não serem atendidos por inteiro (volume exposto ao risco, não o que vai faltar)',
        "range_unit": "months",
        "range_value": 12,
        "code": '''def run(from_date, to_date, ctx):
    # Retail fica de fora: não faz pedido, recebe o resíduo da capacidade.
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
                dict(label="Pedidos das KAs (real)", data=[float(v) for v in ultimos["pedidos"]] + vazio,
                     stack="real", order=1, pointRadius=2, **linha("#2a78d6", {})),
                dict(label="Demanda prevista (modelo)", data=[None] * (n - 1) + [float(ultimos["pedidos"].iloc[-1])] + prevista,
                     stack="prevista", order=0, pointRadius=3, **linha("#7c3aed", dict(borderDash=[6, 4]))),
                dict(label="Demanda em pedidos com risco alto (≥60%)", data=[None] * n + alto, stack="risco", order=3,
                     backgroundColor="rgba(220, 38, 38, 0.75)"),
                dict(label="Demanda em pedidos com risco médio (30–60%)", data=[None] * n + medio, stack="risco", order=3,
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
        "title": 'Pedidos em risco no próximo quarter — modelo de previsão',
        "description": 'Número de pedidos (KA × produto) previstos para cada mês do próximo quarter, por faixa de risco de não serem atendidos por inteiro',
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    # Um pedido = uma KA x produto no mês. Retail fica de fora (não faz pedido).
    prev = pd.DataFrame(ctx.pack("previsao_proximo_quarter"))
    meses = sorted(prev["year_month"].unique())
    conta = lambda faixa: [int(((prev["year_month"] == m) & (prev["risco"] == faixa)).sum()) for m in meses]
    eixo = dict(ticks=dict(color="#64748b", font=dict(size=10)), grid=dict(color="#e2e8f0"), stacked=True)
    return {
        "type": "bar",
        "data": {
            "labels": [m[:7] for m in meses],
            "datasets": [
                dict(label="Risco alto (≥60%)", data=conta("alto"), backgroundColor="rgba(220, 38, 38, 0.75)"),
                dict(label="Risco médio (30–60%)", data=conta("médio"), backgroundColor="rgba(245, 158, 11, 0.7)"),
                dict(label="Risco baixo (<30%)", data=conta("baixo"), backgroundColor="#cbd5e1"),
            ],
        },
        "options": {"scales": {"x": eixo, "y": dict(eixo, beginAtZero=True)}},
    }
''',
    },
    {
        "id": "alocacao_por_key_account",
        "title": "Alocação por Key Account — quarter atual",
        "description": "Total alocado (W1-W5) por Key Account no quarter em andamento",
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
            'datasets': [{'label': 'Alocado', 'data': values}]
        }
    }
''',
    },
    {
        "id": "woi_por_faixa_de_risco",
        "title": "WOI por Faixa de Risco — quarter atual",
        "description": "Distribuição de registros por faixa de risco de WOI no quarter em andamento",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.sql("""
        SELECT CASE
                 WHEN woi < 10 THEN 'Crítico (<10)'
                 WHEN woi < 15 THEN 'Médio (10-15)'
                 ELSE 'Saudável (>=15)'
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
            'datasets': [{'label': 'Registros', 'data': values}]
        }
    }
''',
    },
]
