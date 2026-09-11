# 面板标题/描述和 Chart.js 图表标签（下方）已翻译——这些是屏幕上实际显示的中文内容。
# docstring 和代码注释按设计保留葡萄牙语（内部内容，不会展示给用户）。
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
        "title": "按站点划分的最终 DL 成本 — 最近3个月",
        "description": "按站点汇总的 dl_cost_by_cm_usd（fact_final_dl_cost），已批准费率",
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
            'datasets': [{'label': 'DL 成本（美元）', 'data': values, 'backgroundColor': '#2a78d6'}]
        }
    }
''',
    },
    {
        "id": "custo_dl_por_produto",
        "title": "按产品划分的最终 DL 成本 — 最近3个月",
        "description": "按产品汇总的 dl_cost_by_cm_usd（fact_final_dl_cost），已批准费率",
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
            'datasets': [{'label': 'DL 成本（美元）', 'data': values, 'backgroundColor': '#eb6834'}]
        }
    }
''',
    },
    {
        "id": "custo_dl_por_area",
        "title": "按流程区域划分的最终 DL 成本 — 总计",
        "description": "backend_cost/cfc_cost/smt_cost/l5_cost（fact_final_dl_cost）之和，已批准费率，全表数据（几十行）。雷达图：单一数据系列，这种形式比4根独立柱状图更能体现成本构成的'形状'。",
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
                'label': 'DL 成本（美元）',
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
        "title": "按流程区域划分的平均时薪",
        "description": "按区域汇总 fact_hourly_rate 的 AVG(hourly_rate)，当前已批准费率",
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
            'datasets': [{'label': '平均时薪', 'data': values, 'backgroundColor': '#1baf7a'}]
        }
    }
''',
    },
    {
        "id": "hr_medio_por_site",
        "title": "按站点划分的平均时薪 — 前15名",
        "description": "按站点汇总 fact_hourly_rate 的 AVG(hourly_rate)，当前已批准费率",
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
            'datasets': [{'label': '平均时薪', 'data': values, 'backgroundColor': '#eda100'}]
        }
    }
''',
    },
    {
        "id": "desconto_medio_por_tipo",
        "title": "按类型划分的平均协商折扣",
        "description": "按 discount_type 汇总 fact_dl_discount 的 AVG(discount_value)，当前已批准的折扣",
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
            'datasets': [{'label': '平均折扣', 'data': values, 'backgroundColor': '#e87ba4'}]
        }
    }
''',
    },
    {
        "id": "registros_desconto_por_tipo",
        "title": "按类型划分的折扣记录 — 完整历史",
        "description": "按 discount_type 汇总 fact_dl_discount 的 COUNT，已批准和已过期记录（历史总量，非仅当前有效）",
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
            'datasets': [{'label': '记录数', 'data': values, 'backgroundColor': '#008300'}]
        }
    }
''',
    },
    {
        "id": "uph_medio_por_area",
        "title": "按流程区域划分的平均生产力（UPH）",
        "description": "按区域汇总 fact_standard_time 的 AVG(uph)，当前已批准的时间",
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
            'datasets': [{'label': '平均 UPH', 'data': values, 'backgroundColor': '#4a3aa7'}]
        }
    }
''',
    },
    {
        "id": "tempo_padrao_medio_por_estagio",
        "title": "按验证阶段划分的平均标准时间",
        "description": "按 stage（RFQ/30天/30天后）汇总 fact_standard_time 的 AVG(standard_time)，当前已批准的时间。极坐标图：仅3个类别，符合按扇区区分颜色的对比度/色觉安全上限（参见 dataviz skill）。",
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
            'datasets': [{'label': '平均标准时间', 'data': values, 'backgroundColor': ['#2a78d6', '#eb6834', '#1baf7a']}]
        }
    }
''',
    },
    {
        "id": "sites_por_pais",
        "title": "按国家划分的站点数",
        "description": "按 country 汇总 dim_site 的 COUNT",
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
            'datasets': [{'label': '站点数', 'data': values, 'backgroundColor': '#2a78d6'}]
        }
    }
''',
    },
    {
        "id": "sites_por_empresa",
        "title": "按受托代工厂（CM）划分的站点数",
        "description": "按 company 汇总 dim_site 的 COUNT — 一个 CM 可能拥有多个站点（详见术语表）",
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
            'datasets': [{'label': '站点数', 'data': values, 'backgroundColor': '#eb6834'}]
        }
    }
''',
    },
    {
        "id": "produtos_por_tipo",
        "title": "按类型划分的产品数",
        "description": "按 type（PHONE/TABLET/OTHER）汇总 dim_product 的 COUNT — 3个类别，符合饼图的对比度/色觉安全上限（参见 dataviz skill）",
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
            'datasets': [{'label': '产品数', 'data': values, 'backgroundColor': ['#2a78d6', '#eb6834', '#1baf7a']}]
        }
    }
''',
    },
    {
        "id": "produtos_por_familia",
        "title": "按产品系列划分的产品数 — 前15名",
        "description": "按 product_family_id 汇总 dim_product 的 COUNT",
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
            'datasets': [{'label': '产品数', 'data': values, 'backgroundColor': '#eda100'}]
        }
    }
''',
    },
    {
        "id": "descontos_por_tipo_valor",
        "title": "按数值类型划分的折扣",
        "description": "按 value_type（PERCENTAGE/VALUE/未分类）汇总 fact_dl_discount 的 COUNT，完整历史 — 3个类别，适合环形图",
        "range_unit": "months",
        "range_value": 3,
        "code": '''def run(from_date, to_date, ctx):
    rows = ctx.graphql_aggregate(
        "fact_dl_discount", group_by="value_type",
        metric_field="id", metric_op="COUNT",
        order_by="DESC",
    )
    labels = [r["groupKey"] or "未分类" for r in rows]
    values = [float(r["metricValue"]) for r in rows]
    return {
        'type': 'doughnut',
        'data': {
            'labels': labels,
            'datasets': [{'label': '折扣数', 'data': values, 'backgroundColor': ['#2a78d6', '#eb6834', '#1baf7a']}]
        }
    }
''',
    },
    {
        "id": "custo_dl_por_quarter",
        "title": "最终 DL 成本 — 按季度变化趋势",
        "description": "按财政季度汇总的 dl_cost_by_cm_usd（fact_final_dl_cost）之和，已批准费率，全表数据 — 按时间顺序排序（非按文本排序，参见 skill 中关于财政日历的说明）",
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
                'label': '总 DL 成本（美元）',
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
        "title": "站点 — 平均时薪 x 总 DL 成本",
        "description": "按站点的散点分布：X轴 = 已批准的 AVG(hourly_rate)，Y轴 = 已批准的 SUM(dl_cost_by_cm_usd)。每个站点各自作为一个数据系列，图例即可通过名称识别，无需为每个点单独标注",
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
        "title": "热力图 — 平均时薪（站点 x 区域）",
        "description": "按站点 x 流程区域交叉汇总 fact_hourly_rate 的 AVG(hourly_rate)，已批准费率 — 热力图（chartjs-chart-matrix），空白单元格 = 该组合无已批准数据",
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
        return {'type': 'matrix', 'data': {'points': [], 'xLabels': [], 'yLabels': [], 'label': '平均时薪（美元）'}}
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
        'data': {'points': points, 'xLabels': x_labels, 'yLabels': y_labels, 'label': '平均时薪（美元）'}
    }
''',
    },
]
