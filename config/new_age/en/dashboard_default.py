# Título/descrição dos painéis e rótulos de gráfico (Chart.js) abaixo estão traduzidos —
# são o que aparece de fato na tela em inglês. O docstring e os comentários de código
# seguem em português de propósito (conteúdo interno, nunca renderizado ao usuário).
"""Painéis padrão do dashboard do domínio New Age (dominio_new_age.md).

Lido dinamicamente por db_config.dashboard_default_widgets() — ver docstring de
config/dashboard_default_alocacao.py para o mecanismo geral (arquivo por domínio,
recarregado sem restart, nunca gravado no banco).

Este domínio usa `database.modo: graphql` (config/settings.yml) — o `code` abaixo
usa `ctx.graphql_aggregate(...)`/`ctx.graphql_table(...)`, não `ctx.sql(...)` (que
não funciona nesse modo; ver aviso em "Exemplos task_code" de dominio_new_age.md).
Espelham as tools `executar_graphql_agregado`/`executar_graphql` do chat: mesmos
argumentos, mesmo schema.

Todos os valores abaixo foram checados ao vivo contra a API GraphQL do domínio
(ambiente dev/non-prod) antes de entrar aqui — não são só "deveria funcionar".

Dois grupos de painel, com filtro de data diferente:
  - `custo_dl_por_*` (fact_final_dl_cost): usam from_date/to_date recebidos, com
    filtro de SOBREPOSIÇÃO de intervalo (start_month <= to_date AND end_month >=
    from_date) — nunca contenção total, que descarta o trimestre em andamento
    (ver aviso na skill).
  - Todos os outros: NÃO filtram por from_date/to_date. `fact_hourly_rate`,
    `fact_standard_time` e `fact_dl_discount` têm vigência por SCD (um novo
    registro APPROVED expira o anterior da mesma combinação — ver "Regra de
    vigência do Hourly Rate" na skill) — filtrar esses por data ingenuamente
    (ex: por `approved_at` ou `month_id` isolado) dá resultado errado sem
    implementar a lógica de "o registro APPROVED mais recente até X" por grupo,
    que `reportAggregate` não expressa em uma chamada só. Por isso estes painéis
    mostram o estado atual (`status_approval = 'APPROVED'`, sem recorte de tempo)
    em vez de arriscar um filtro de período sutilmente incorreto — mesmo
    raciocínio para as tabelas de dimensão (`dim_site`, `dim_product`), que não
    têm noção de período. `range_unit`/`range_value` seguem preenchidos (campo
    obrigatório do schema de widget) mas o `code` não os usa — é só rótulo.
"""

WIDGETS = [
    {
        "id": "custo_dl_por_site",
        "title": "Final DL Cost by Site — last 3 months",
        "description": "Sum of dl_cost_by_cm_usd (fact_final_dl_cost) grouped by site, approved rates",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_final_dl_cost", group_by="site_id",
        metric_field="dl_cost_by_cm_usd", metric_op="SUM",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"},
               {"field": "start_month", "op": "LTE", "value": to_date},
               {"field": "end_month", "op": "GTE", "value": from_date}],
        expand=["name", "country"], order_by="DESC", limit=20,
    )
    labels = [r.get("name") or r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'DL Cost (USD)', 'data': values, 'backgroundColor': '#2a78d6'}]
        }
    }
''',
    },
    {
        "id": "custo_dl_por_produto",
        "title": "Final DL Cost by Product — last 3 months",
        "description": "Sum of dl_cost_by_cm_usd (fact_final_dl_cost) grouped by product, approved rates",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_final_dl_cost", group_by="product_id",
        metric_field="dl_cost_by_cm_usd", metric_op="SUM",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"},
               {"field": "start_month", "op": "LTE", "value": to_date},
               {"field": "end_month", "op": "GTE", "value": from_date}],
        expand=["name"], order_by="DESC", limit=15,
    )
    labels = [r.get("name") or r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'DL Cost (USD)', 'data': values, 'backgroundColor': '#eb6834'}]
        }
    }
''',
    },
    {
        "id": "custo_dl_por_area",
        "title": "Final DL Cost by Process Area — total",
        "description": "Sum of backend_cost/cfc_cost/smt_cost/l5_cost (fact_final_dl_cost), approved rates, whole table (a few dozen rows). Radar: single series — this shape reads the breakdown's 'shape' better than 4 separate bars.",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_table(
        "fact_final_dl_cost",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        limit=1000,
    )
    df = pd.DataFrame(rows)
    areas = ["backend_cost", "cfc_cost", "smt_cost", "l5_cost"]
    labels = ["BACKEND", "CFC", "SMT", "L5"]
    values = [float(pd.to_numeric(df[c], errors="coerce").sum()) if not df.empty else 0.0 for c in areas]
    return {
        'type': 'radar',
        'data': {
            'labels': labels,
            'datasets': [{
                'label': 'DL Cost (USD)',
                'data': values,
                'borderColor': '#2a78d6',
                'backgroundColor': 'rgba(42, 120, 214, 0.1)',
                'borderWidth': 2,
                'pointBackgroundColor': '#2a78d6',
            }]
        }
    }
''',
    },
    {
        "id": "hr_medio_por_area",
        "title": "Average Hourly Rate by Process Area",
        "description": "AVG(hourly_rate) from fact_hourly_rate grouped by area, currently approved rates",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_hourly_rate", group_by="area",
        metric_field="hourly_rate", metric_op="AVG",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Average hourly rate', 'data': values, 'backgroundColor': '#1baf7a'}]
        }
    }
''',
    },
    {
        "id": "hr_medio_por_site",
        "title": "Average Hourly Rate by Site — top 15",
        "description": "AVG(hourly_rate) from fact_hourly_rate grouped by site, currently approved rates",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_hourly_rate", group_by="site_id",
        metric_field="hourly_rate", metric_op="AVG",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        expand=["name"], order_by="DESC", limit=15,
    )
    labels = [r.get("name") or r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Average hourly rate', 'data': values, 'backgroundColor': '#eda100'}]
        }
    }
''',
    },
    {
        "id": "desconto_medio_por_tipo",
        "title": "Average Negotiated Discount by Type",
        "description": "AVG(discount_value) from fact_dl_discount grouped by discount_type, currently approved discounts",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_dl_discount", group_by="discount_type",
        metric_field="discount_value", metric_op="AVG",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Average discount', 'data': values, 'backgroundColor': '#e87ba4'}]
        }
    }
''',
    },
    {
        "id": "registros_desconto_por_tipo",
        "title": "Discount Records by Type — full history",
        "description": "COUNT from fact_dl_discount grouped by discount_type, approved and expired (historical volume, not just current)",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_dl_discount", group_by="discount_type",
        metric_field="id", metric_op="COUNT",
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Records', 'data': values, 'backgroundColor': '#008300'}]
        }
    }
''',
    },
    {
        "id": "uph_medio_por_area",
        "title": "Average Productivity (UPH) by Process Area",
        "description": "AVG(uph) from fact_standard_time grouped by area, currently approved times",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_standard_time", group_by="area",
        metric_field="uph", metric_op="AVG",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Average UPH', 'data': values, 'backgroundColor': '#4a3aa7'}]
        }
    }
''',
    },
    {
        "id": "tempo_padrao_medio_por_estagio",
        "title": "Average Standard Time by Validation Stage",
        "description": "AVG(standard_time) from fact_standard_time grouped by stage (RFQ/30 days/post-30 days), currently approved times. PolarArea: only 3 categories, within the safe contrast/CVD limit for color-per-slice (see the dataviz skill).",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_standard_time", group_by="stage",
        metric_field="standard_time", metric_op="AVG",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'polarArea',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Average standard time', 'data': values, 'backgroundColor': ['#2a78d6', '#eb6834', '#1baf7a']}]
        }
    }
''',
    },
    {
        "id": "sites_por_pais",
        "title": "Sites by Country",
        "description": "COUNT from dim_site grouped by country",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "dim_site", group_by="country",
        metric_field="id", metric_op="COUNT",
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Sites', 'data': values, 'backgroundColor': '#2a78d6'}]
        }
    }
''',
    },
    {
        "id": "sites_por_empresa",
        "title": "Sites by Contract Manufacturer (CM)",
        "description": "COUNT from dim_site grouped by company — a CM can have more than one site (see Glossary)",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "dim_site", group_by="company",
        metric_field="id", metric_op="COUNT",
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Sites', 'data': values, 'backgroundColor': '#eb6834'}]
        }
    }
''',
    },
    {
        "id": "produtos_por_tipo",
        "title": "Products by Type",
        "description": "COUNT from dim_product grouped by type (PHONE/TABLET/OTHER) — 3 categories, within the safe contrast/CVD limit for a pie chart (see the dataviz skill)",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "dim_product", group_by="type",
        metric_field="id", metric_op="COUNT",
        order_by="DESC",
    )
    labels = [r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'pie',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Products', 'data': values, 'backgroundColor': ['#2a78d6', '#eb6834', '#1baf7a']}]
        }
    }
''',
    },
    {
        "id": "produtos_por_familia",
        "title": "Products by Family — top 15",
        "description": "COUNT from dim_product grouped by product_family_id",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "dim_product", group_by="product_family_id",
        metric_field="id", metric_op="COUNT",
        expand=["name"], order_by="DESC", limit=15,
    )
    labels = [r.get("name") or r["groupKey"] for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Products', 'data': values, 'backgroundColor': '#eda100'}]
        }
    }
''',
    },
    {
        "id": "descontos_por_tipo_valor",
        "title": "Discounts by Value Type",
        "description": "COUNT from fact_dl_discount grouped by value_type (PERCENTAGE/VALUE/unclassified), full history — 3 categories, safe for a doughnut chart",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_dl_discount", group_by="value_type",
        metric_field="id", metric_op="COUNT",
        order_by="DESC",
    )
    labels = [r["groupKey"] or "Not classified" for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'doughnut',
        'data': {
            'labels': labels,
            'datasets': [{'label': 'Discounts', 'data': values, 'backgroundColor': ['#2a78d6', '#eb6834', '#1baf7a']}]
        }
    }
''',
    },
    {
        "id": "custo_dl_por_quarter",
        "title": "Final DL Cost — trend by quarter",
        "description": "Sum of dl_cost_by_cm_usd (fact_final_dl_cost) by fiscal quarter, approved rates, whole table — sorted chronologically (not alphabetically, see the fiscal calendar note in the skill)",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_final_dl_cost", group_by="quarter",
        metric_field="dl_cost_by_cm_usd", metric_op="SUM",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        limit=100,
    )

    def quarter_key(label):
        try:
            q, year = label.split(",")
            return (int(year), int(q.replace("Q", "")))
        except Exception:
            return (0, 0)

    rows_sorted = sorted(rows, key=lambda r: quarter_key(r["groupKey"]))
    labels = [r["groupKey"] for r in rows_sorted]
    values = [float(r["metricValue"]) for r in rows_sorted]
    return {
        'type': 'line',
        'data': {
            'labels': labels,
            'datasets': [{
                'label': 'Total DL Cost (USD)',
                'data': values,
                'borderColor': '#e34948',
                'backgroundColor': 'rgba(227, 73, 72, 0.1)',
                'borderWidth': 2,
                'pointRadius': 4,
                'pointHoverRadius': 6,
                'pointBackgroundColor': '#e34948',
                'fill': True,
                'tension': 0.2,
            }]
        }
    }
''',
    },
    {
        "id": "site_hr_vs_custo",
        "title": "Sites — Average Hourly Rate vs Total DL Cost",
        "description": "Scatter by site: X axis = approved AVG(hourly_rate), Y axis = approved SUM(dl_cost_by_cm_usd). Each site is its own dataset (series), so the legend identifies it by name without needing a per-point label",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    hr_rows = ctx.graphql_aggregate(
        "fact_hourly_rate", group_by="site_id",
        metric_field="hourly_rate", metric_op="AVG",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        expand=["name"], limit=50,
    )
    custo_rows = ctx.graphql_aggregate(
        "fact_final_dl_cost", group_by="site_id",
        metric_field="dl_cost_by_cm_usd", metric_op="SUM",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        expand=["name"], limit=50,
    )
    hr_by_site = {(r.get("name") or r["groupKey"]): float(r["metricValue"]) for r in hr_rows}
    custo_by_site = {(r.get("name") or r["groupKey"]): float(r["metricValue"]) for r in custo_rows}
    sites = sorted(set(hr_by_site) & set(custo_by_site))
    palette = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948']
    datasets = [
        {
            'label': site,
            'data': [{'x': hr_by_site[site], 'y': custo_by_site[site]}],
            'backgroundColor': palette[i % len(palette)],
            'pointRadius': 6,
            'pointHoverRadius': 8,
        }
        for i, site in enumerate(sites)
    ]
    return {
        'type': 'scatter',
        'data': {'datasets': datasets}
    }
''',
    },
    {
        "id": "hr_por_site_x_area",
        "title": "Heatmap — Average Hourly Rate (Site x Area)",
        "description": "AVG(hourly_rate) from fact_hourly_rate pivoted site x process area, approved rates — heatmap (chartjs-chart-matrix), blank cell = no approved data for that combination",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_table(
        "fact_hourly_rate",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
        expand=[{"viaColumn": "site_id", "columns": ["name"]}],
        limit=1000,
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return {'type': 'matrix', 'data': {'points': [], 'xLabels': [], 'yLabels': [], 'label': 'Average Hourly Rate (USD)'}}
    df['hourly_rate'] = pd.to_numeric(df['hourly_rate'], errors='coerce')
    piv = df.pivot_table(index='site_id__name', columns='area', values='hourly_rate', aggfunc='mean')
    x_labels = sorted(piv.columns.tolist())
    y_labels = sorted(piv.index.tolist())
    points = []
    for y in y_labels:
        for x in x_labels:
            v = piv.loc[y, x]
            points.append({'x': x, 'y': y, 'v': None if pd.isna(v) else round(float(v), 1)})
    return {
        'type': 'matrix',
        'data': {'points': points, 'xLabels': x_labels, 'yLabels': y_labels, 'label': 'Average Hourly Rate (USD)'}
    }
''',
    },
]
