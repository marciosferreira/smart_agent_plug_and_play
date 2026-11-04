"""Demonstra os comportamentos da camada GraphQL do New Age citados no RDA.

Não é importado nem chamado pelo app. Só leitura. Usa o cliente do
próprio app (graphql_config.py); endpoint e chave vêm de config/settings.yml e
NEW_AGE_API_KEY no .env e nunca são impressos.

Uso, a partir da raiz do projeto:
    python verificacoes/demonstrar_graphql_new_age.py              # as três demonstrações
    python verificacoes/demonstrar_graphql_new_age.py --demo 2     # só uma (1 a 3)

Demonstrações:
    1. Consulta com junção de um salto (expand): taxa horária já com o site
    2. Proteção de campo sensível: o mesmo custo lido direto (em claro) e por junção (criptografado)
    3. Limite de proteção: pedido acima de 1.000 linhas é recusado com erro explícito
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
load_dotenv(RAIZ / ".env")

import graphql_config  # noqa: E402


def titulo(texto):
    print()
    print(texto)
    print("=" * len(texto))


def mostra_pedido(**variaveis):
    print("Pedido (reportTable):")
    for k, v in variaveis.items():
        if isinstance(v, list):
            print(f"  {k}:")
            for item in v:
                print(f"    {json.dumps(item, ensure_ascii=False)}")
        else:
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")


def demo_expand():
    titulo("1. Consulta com junção (expand): taxa horária já com o site")
    pedido = dict(
        table="fact_hourly_rate",
        where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"},
               {"field": "month_id", "op": "EQ", "value": "2026-08"}],
        expand=[{"viaColumn": "site_id", "columns": ["name", "country"]}],
        limit=3,
    )
    mostra_pedido(**pedido)
    linhas = graphql_config.run_query_table(**pedido)
    colunas = ["site_id__name", "site_id__country", "month_id", "area", "hourly_rate", "status_approval"]
    print(f"\nResposta ({len(linhas)} linhas, colunas selecionadas):")
    for r in linhas:
        print(f"  {json.dumps({c: r.get(c) for c in colunas}, ensure_ascii=False)}")


def demo_criptografia():
    titulo("2. Campo sensível: lido direto x lido por junção")
    ponte = graphql_config.run_query_table("bridge_final_dl_cost_sales_model", limit=1)
    custo_id = ponte[0]["final_dl_cost_id"]

    print("(a) Direto em fact_final_dl_cost:")
    direto = graphql_config.run_query_table(
        "fact_final_dl_cost", where=[{"field": "id", "op": "EQ", "value": custo_id}], limit=1)
    print(json.dumps({c: direto[0][c] for c in ("quarter", "dl_cost_by_cm", "dl_cost_by_cm_usd")},
                     indent=2, ensure_ascii=False))

    print("\n(b) A mesma linha por junção, a partir da ponte de Sales Models:")
    via = graphql_config.run_query_table(
        "bridge_final_dl_cost_sales_model",
        where=[{"field": "final_dl_cost_id", "op": "EQ", "value": custo_id}],
        expand=[{"viaColumn": "final_dl_cost_id", "columns": ["quarter", "dl_cost_by_cm_usd"]}],
        limit=1,
    )
    print(json.dumps({c: via[0][c] for c in ("final_dl_cost_id__quarter", "final_dl_cost_id__dl_cost_by_cm_usd")},
                     indent=2, ensure_ascii=False))


def demo_limite():
    titulo("3. Limite de proteção: pedido de 5.000 linhas")
    mostra_pedido(table="fact_hourly_rate", limit=5000)
    try:
        graphql_config.run_query_table("fact_hourly_rate", limit=5000)
        print("\nResposta: aceito (limite NÃO aplicado)")
    except graphql_config.GraphQLError as e:
        print(f"\nResposta: erro do servidor -> {e}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--demo", type=int, choices=range(1, 4), help="roda só uma demonstração")
    args = p.parse_args()
    if not graphql_config.is_configured():
        sys.exit("GraphQL não configurado — confira config/settings.yml e NEW_AGE_API_KEY no .env")
    for n, fn in {1: demo_expand, 2: demo_criptografia, 3: demo_limite}.items():
        if args.demo in (None, n):
            fn()


if __name__ == "__main__":
    main()
