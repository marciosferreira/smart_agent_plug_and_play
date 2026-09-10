"""
Avaliação de condition_sql dos monitores — abstrai SQL direto (database.modo: sql)
vs. um spec JSON executado via GraphQL (database.modo: graphql). Usado tanto na
criação do monitor (validação em schedule_monitor) quanto na checagem periódica
do daemon (_evaluate_condition), para que os dois caminhos concordem sobre o
formato aceito e sobre como extrair o valor comparado ao threshold.
"""
import json as _json

import db_config


class ConditionError(RuntimeError):
    pass


def run_condition_query(condition_sql: str) -> "list[dict]":
    """Executa a condição do monitor e retorna list[dict].

    Modo sql: condition_sql é uma query SELECT, executada direto no banco.
    Modo graphql: condition_sql é uma string JSON (ver describe_condition_format).
    Em ambos os casos, quando o valor for numérico (operadores >, <, >=, <=, ==, !=),
    o chamador extrai list(dict(rows[0]).values())[0] — por isso normalizamos o
    resultado do graphql para uma única chave "value".
    """
    if db_config.modo_acesso() == "graphql":
        return _run_graphql_condition(condition_sql)
    return db_config.run_select(condition_sql)


def _run_graphql_condition(spec_json: str) -> "list[dict]":
    import graphql_config

    try:
        spec = _json.loads(spec_json)
    except Exception as exc:
        raise ConditionError(
            f"condition_sql deve ser um JSON válido neste domínio (modo graphql): {exc}"
        )

    tipo = spec.get("type")
    table = spec.get("table")
    if not table:
        raise ConditionError("condition_sql (JSON): campo 'table' é obrigatório.")
    where = spec.get("where")

    if tipo == "aggregate":
        group_by = spec.get("group_by")
        metric_field = spec.get("metric_field")
        metric_op = spec.get("metric_op")
        if not (group_by and metric_field and metric_op):
            raise ConditionError(
                "condition_sql (JSON, type='aggregate'): 'group_by', 'metric_field' e "
                "'metric_op' são obrigatórios."
            )
        rows = graphql_config.run_aggregate(
            table, group_by, metric_field, metric_op, where=where, limit=spec.get("limit"),
        )
        return [{"value": r.get("metricValue")} for r in rows]

    if tipo == "table":
        value_field = spec.get("value_field")
        rows = graphql_config.run_query_table(
            table, where=where, order_by=spec.get("order_by"), limit=spec.get("limit") or 1,
        )
        if value_field:
            return [{"value": r.get(value_field)} for r in rows]
        return rows

    raise ConditionError(
        f"condition_sql (JSON): 'type' deve ser 'aggregate' ou 'table' (recebido: {tipo!r})."
    )


def describe_condition_format() -> str:
    """Explica o formato esperado de condition_sql no modo de acesso ativo — usado no
    docstring de schedule_monitor para instruir o LLM corretamente por domínio."""
    if db_config.modo_acesso() != "graphql":
        return "condition_sql: query SELECT que retorna um escalar ou linhas (dialeto SQLite)."
    return (
        "Este domínio usa GraphQL (não SQL direto) — condition_sql deve ser uma "
        "string JSON com um destes formatos:\n"
        '  Agregação: \'{"type": "aggregate", "table": "...", "group_by": "...", '
        '"metric_field": "...", "metric_op": "SUM|COUNT|AVG|MIN|MAX", "where": [...]}\'\n'
        '  Linha crua: \'{"type": "table", "table": "...", "where": [...], "value_field": "..."}\'\n'
        '  where (opcional): lista de {"field": "...", "op": "EQ|NEQ|GT|GTE|LT|LTE|IN|NOT_IN|'
        'LIKE|ILIKE|IS_NULL|IS_NOT_NULL", "value": ...}\n'
        "  O valor comparado ao threshold é sempre 'metricValue' (aggregate) ou o campo "
        "indicado em 'value_field' (table)."
    )
