"""
Runner para widget_code de painéis customizados do dashboard.

O código deve definir `def run(from_date, to_date, ctx)` e retornar um dict
com configuração Chart.js: {"type": ..., "data": {...}, "options": {...}}.

Executa no mesmo sandbox do task runner — mesmas bibliotecas aprovadas,
mesmo bloqueio de imports. A diferença é que run() deve retornar um dict,
não um token de artifact.
"""

import builtins
import traceback
from datetime import date, timedelta

from .context import TaskContext


_CHARTJS_TYPES = {"bar", "line", "pie", "doughnut", "radar", "polarArea", "scatter", "bubble", "matrix"}


def _build_globals(ctx: TaskContext, from_date: str, to_date: str) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    import pandas as pd
    from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

    # Módulos inofensivos: liberados mesmo que o código faça `import X` explícito
    # (datetime/pandas/numpy já são injetados como date/datetime/timedelta/pd/np,
    # mas o agente às vezes os importa de novo — sem risco, então permitimos).
    _ALLOWED_IMPORTS = {
        "time", "_strptime", "datetime", "pandas", "numpy", "math",
        "re", "json", "statistics", "decimal", "collections", "itertools",
    }

    def _blocked_import(name, *args, **kwargs):
        top_level = name.split(".")[0]
        if top_level in _ALLOWED_IMPORTS:
            return builtins.__import__(name, *args, **kwargs)
        raise ImportError(
            f"Import de '{name}' não é permitido dentro do run(). "
            "Use as variáveis já disponíveis no namespace: pd, np, date, datetime, timedelta, ctx, etc."
        )

    safe_builtins = {
        name: getattr(builtins, name)
        for name in (
            "abs", "all", "any", "bool", "dict", "divmod", "enumerate",
            "filter", "float", "format", "frozenset", "getattr", "hasattr",
            "hash", "int", "isinstance", "issubclass", "iter", "len", "list",
            "map", "max", "min", "next", "object", "print", "range", "repr",
            "reversed", "round", "set", "slice", "sorted", "str", "sum",
            "tuple", "type", "zip", "True", "False", "None",
            "ValueError", "TypeError", "KeyError", "IndexError",
            "RuntimeError", "StopIteration", "Exception",
        )
    }
    safe_builtins["__import__"] = _blocked_import

    return {
        "__builtins__": safe_builtins,
        "from_date": from_date,
        "to_date":   to_date,
        "date":      _date,
        "datetime":  _datetime,
        "timedelta": _timedelta,
        "ctx": ctx,
        "np": np,
        "pd": pd,
    }


class WidgetCodeError(Exception):
    """Erro de validação ou execução do widget_code."""


def run_widget_code(code: str, from_date: str, to_date: str, session_id: str) -> dict:
    """Compila e executa widget_code. Retorna Chart.js config dict.

    Lança WidgetCodeError com mensagem amigável em caso de falha.
    """
    ctx = TaskContext(session_id)
    g   = _build_globals(ctx, from_date, to_date)

    try:
        compiled = compile(code, "<widget_code>", "exec")
    except SyntaxError as e:
        raise WidgetCodeError(f"Erro de sintaxe:\n{e}") from e

    try:
        exec(compiled, g)
    except ImportError as e:
        raise WidgetCodeError(
            f"Import bloqueado: {e}\n"
            "REGRA: nunca use import dentro de run(). "
            "Variáveis já disponíveis: pd, np, ctx, from_date, to_date, date, datetime, timedelta."
        ) from e
    except Exception as e:
        raise WidgetCodeError(f"Erro ao carregar código:\n{traceback.format_exc(limit=8)}") from e

    if "run" not in g or not callable(g["run"]):
        raise WidgetCodeError("O código deve definir `def run(from_date, to_date, ctx)`.")

    try:
        result = g["run"](from_date, to_date, ctx)
    except (KeyError, AttributeError) as e:
        raise WidgetCodeError(
            f"Erro em run(): {type(e).__name__}: {e}\n"
            "DICA COMUM: ctx.api() retorna list[dict], não DataFrame. "
            "Converta antes de acessar colunas: df = pd.DataFrame(ctx.api('/endpoint...'))"
        ) from e
    except ImportError as e:
        raise WidgetCodeError(
            f"Import bloqueado dentro de run(): {e}\n"
            "Remova o import e use as variáveis pré-injetadas: pd, np, etc."
        ) from e
    except Exception as e:
        raise WidgetCodeError(f"Erro em run():\n{traceback.format_exc(limit=12)}") from e

    if not isinstance(result, dict):
        raise WidgetCodeError(
            f"run() deve retornar um dict com configuração Chart.js "
            f"(type, data, options). Recebido: {type(result).__name__}."
        )

    if "type" not in result or "data" not in result:
        raise WidgetCodeError(
            "O dict retornado deve ter pelo menos as chaves 'type' e 'data'. "
            f"Chaves encontradas: {list(result.keys())}"
        )

    if result["type"] not in _CHARTJS_TYPES:
        raise WidgetCodeError(
            f"Tipo de gráfico inválido: '{result['type']}'. "
            f"Tipos aceitos: {sorted(_CHARTJS_TYPES)}"
        )

    return result


def default_test_range() -> tuple[str, str]:
    end   = date.today()
    start = end - timedelta(days=6)
    return start.isoformat(), end.isoformat()


RANGE_UNITS = {"days", "months"}


def compute_widget_range(range_unit: str, range_value: int) -> tuple[str, str]:
    """Calcula from_date/to_date a partir do período estruturado salvo no widget.

    Substitui a antiga convenção de o próprio widget_code recalcular o período
    internamente — from_date/to_date agora sempre refletem o range configurado.
    """
    end = date.today()
    if range_unit == "months":
        import pandas as pd
        start = (pd.Timestamp(end) - pd.DateOffset(months=range_value)).date()
    else:
        start = end - timedelta(days=range_value)
    return start.isoformat(), end.isoformat()
