"""Acesso ao endpoint GraphQL do domínio ativo — alternativa a db_config.run_select()
para domínios com database.modo = graphql ou both (ver config/settings.yml).

Expõe três métodos de leitura: reportTable (linhas cruas, com filtro, ordenação,
paginação e join de um salto via `expand`), reportAggregate (GROUP BY + agregação
feitos no Postgres, com join de um salto via `expand` se o campo de agrupamento
for FK — formato de `expand` diferente do de reportTable) e reportSchema (schema
real do domínio ativo — tabelas e colunas). Ver ARQUITETURA.md.
"""
import json
from datetime import date

import requests

import db_config

_TIMEOUT = 30


def _json_default(o):
    """Serializa tipos que o `json` padrão recusa. Principal caso: `from_date`/`to_date` do
    task_code são objetos `date` e o exemplo do dominio.md os passa direto no `where`
    (`"value": to_date`) — sem isso a 1ª execução de todo relatório falhava com
    "Object of type date is not JSON serializable" e gastava uma rodada de correção."""
    if isinstance(o, date):  # inclui datetime
        return o.isoformat()
    if hasattr(o, "item"):  # escalar numpy/pandas (np.int64, Timestamp não — cai no TypeError)
        return o.item()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

_QUERY_TABLE = """
query ReportTable($table: String!, $where: [FilterInput!], $orderBy: SortInput, $limit: Int, $offset: Int, $expand: [ExpandInput!]) {
  reportTable(table: $table, where: $where, orderBy: $orderBy, limit: $limit, offset: $offset, expand: $expand)
}
"""

_QUERY_AGGREGATE = """
query ReportAggregate($input: ReportAggregateInput!) {
  reportAggregate(input: $input)
}
"""

_QUERY_SCHEMA = """
query ReportSchema {
  reportSchema {
    tables {
      table
      columns {
        column
        dataType
        nullable
      }
    }
  }
}
"""

FILTER_OPERATORS = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "IN", "NOT_IN", "LIKE", "ILIKE", "IS_NULL", "IS_NOT_NULL")
AGGREGATE_FUNCTIONS = ("SUM", "COUNT", "AVG", "MIN", "MAX")


class GraphQLError(RuntimeError):
    pass


def is_configured() -> bool:
    return db_config.graphql_config() is not None


def _post(query: str, variables: dict) -> dict:
    cfg = db_config.graphql_config()
    if cfg is None:
        raise GraphQLError("GraphQL não configurado para este domínio (ver config/settings.yml).")

    headers = {"Content-Type": "application/json"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    if cfg.get("api_key"):
        headers["X-API-Key"] = cfg["api_key"]

    resp = requests.post(
        cfg["url"],
        headers=headers,
        data=json.dumps({"query": query, "variables": variables}, default=_json_default),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise GraphQLError("; ".join(e.get("message", str(e)) for e in payload["errors"]))
    return payload["data"]


def run_query_table(
    table: str,
    where: "list[dict] | None" = None,
    order_by: "dict | None" = None,
    limit: "int | None" = None,
    offset: "int | None" = None,
    expand: "list[dict] | None" = None,
) -> "list[dict]":
    """Busca linhas de `table` via reportTable e retorna list[dict].

    Args:
        table: nome da tabela (ver dominio.md do domínio ativo).
        where: lista de condições, ex: [{"field": "status", "op": "EQ", "value": "open"}].
               Operadores válidos: EQ, NEQ, GT, GTE, LT, LTE, IN, NOT_IN, LIKE, ILIKE,
               IS_NULL, IS_NOT_NULL. Múltiplas condições são combinadas com AND.
        order_by: ex: {"field": "created_at", "direction": "DESC"}.
        limit / offset: paginação.
        expand: lista de joins de um salto, ex:
                [{"viaColumn": "customer_id", "columns": ["name", "channel"]}].
                `viaColumn` tem que ser uma foreign key de `table`; as colunas pedidas
                vêm em cada linha prefixadas "viaColumn__coluna" (ex: "customer_id__name").
    """
    variables = {
        "table": table, "where": where, "orderBy": order_by,
        "limit": limit, "offset": offset, "expand": expand,
    }
    data = _post(_QUERY_TABLE, variables)
    return data["reportTable"]


def run_aggregate(
    table: str,
    group_by: str,
    metric_field: str,
    metric_op: str,
    where: "list[dict] | None" = None,
    expand: "list[str] | None" = None,
    order_by: "str | None" = None,
    limit: "int | None" = None,
) -> "list[dict]":
    """Agrega `table` (GROUP BY `group_by` + função de agregação) via reportAggregate.

    Args:
        table: nome da tabela (ver dominio.md do domínio ativo).
        group_by: coluna de `table` usada no GROUP BY.
        metric_field: coluna de `table` a agregar.
        metric_op: SUM, COUNT, AVG, MIN ou MAX.
        where: mesmo formato de run_query_table — filtra ANTES de agregar.
        expand: lista de nomes de coluna da tabela relacionada a `group_by` (só
                funciona se `group_by` for uma foreign key) — vêm juntas em cada
                linha do resultado, sem precisar de merge em pandas.
        order_by: "ASC" ou "DESC" — ordena pelo valor agregado (metricValue).
                  Default do servidor: DESC.
        limit: máximo de grupos a retornar. Default do servidor: 20, teto: 500.

    Retorna list[dict] com chaves "groupKey", "metricValue" e uma por coluna de `expand`.
    """
    input_obj: dict = {
        "table": table,
        "groupBy": group_by,
        "metric": {"field": metric_field, "op": metric_op},
    }
    if where is not None:
        input_obj["where"] = where
    if expand is not None:
        input_obj["expand"] = expand
    if order_by is not None:
        input_obj["orderBy"] = order_by
    if limit is not None:
        input_obj["limit"] = limit

    data = _post(_QUERY_AGGREGATE, {"input": input_obj})
    rows = data["reportAggregate"]
    # O servidor devolve o valor do group_by como "groupKey" — não como coluna com o nome do
    # group_by. Quem escreve o código (LLM) tende a esperar `df["site_id"]` depois de agregar
    # por site_id e quebrava com KeyError, o erro mais frequente na 1ª execução de relatórios.
    # Acrescenta o alias (sem remover "groupKey", e sem sobrescrever uma coluna do expand).
    for r in rows:
        if isinstance(r, dict) and group_by not in r and "groupKey" in r:
            r[group_by] = r["groupKey"]
    return rows


def run_schema() -> "list[dict]":
    """Busca o schema real (tabelas e colunas) exposto pelo backend via reportSchema.

    Sem argumentos — retorna todas as tabelas do domínio ativo de uma vez. Cada item:
    {"table": str, "columns": [{"column": str, "dataType": str, "nullable": bool}, ...]}.

    Útil para confirmar nomes de tabela/coluna reais quando a skill `dominio.md` estiver
    desatualizada ou divergir do que o servidor expõe (ver seção de discrepâncias em
    config/new_age/*/dominio.md como exemplo de caso real já encontrado).
    """
    data = _post(_QUERY_SCHEMA, {})
    return data["reportSchema"]["tables"]
