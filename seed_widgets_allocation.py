"""Cria os 2 painéis padrão do pack de alocação como widgets persistentes do dashboard.

Uso: python seed_widgets_allocation.py

Equivalente via chat: "adiciona ao dashboard um painel de alocação por Key Account no
quarter atual" e "adiciona ao dashboard um painel com a distribuição de WOI por faixa
de risco". Idempotente — não duplica se os títulos já existirem.
"""
import uuid
from datetime import datetime

import db_config
from db import get_db
from scheduler.widget_runner import run_widget_code, default_test_range

WIDGETS = [
    {
        "title": "Alocação por Key Account — quarter atual",
        "description": "Total alocado (W1-W5) por Key Account no quarter em andamento",
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
        "title": "WOI por Faixa de Risco — quarter atual",
        "description": "Distribuição de registros por faixa de risco de WOI no quarter em andamento",
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


def main() -> None:
    from_date, to_date = default_test_range()
    with get_db() as conn:
        for w in WIDGETS:
            exists = conn.execute(
                "SELECT 1 FROM dashboard_widgets WHERE title = ?", (w["title"],)
            ).fetchone()
            if exists:
                print(f"  já existe: {w['title']}")
                continue
            # Valida executando de verdade antes de persistir
            config = run_widget_code(w["code"], from_date, to_date, "seed-widgets")
            assert config.get("type") and config.get("data"), "widget não retornou config Chart.js"
            conn.execute(
                "INSERT INTO dashboard_widgets (id, title, description, code, created_at, user_id, dominio) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (str(uuid.uuid4()), w["title"], w["description"], w["code"],
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), db_config.dominio_filename()),
            )
            print(f"  criado: {w['title']} ({len(config['data']['labels'])} categorias)")
        conn.commit()
    print("Seed de widgets concluído.")


if __name__ == "__main__":
    main()
