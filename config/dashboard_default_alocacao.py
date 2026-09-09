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
