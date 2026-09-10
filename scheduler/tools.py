"""
LangChain tools do scheduler — usam SQLite via db.get_db().
"""

import logging
import re as _re
import traceback
from datetime import datetime
from typing import Optional

from langchain_core.tools import tool

import db_config
from db import get_db
from .md_parser import calculate_next_run

logger = logging.getLogger(__name__)

_DOMINIO_CLAUSE = "(dominio = ? OR dominio IS NULL)"

_MIN_INTERVAL_MINUTES = 5


def _validate_frequency(frequency: str) -> str | None:
    """Retorna mensagem de erro se a frequência for inválida, None se OK."""
    if frequency == 'on_demand':
        return None
    m = _re.match(r'every_(\d+)m$', frequency)
    if m and int(m.group(1)) < _MIN_INTERVAL_MINUTES:
        return f"Intervalo mínimo permitido é {_MIN_INTERVAL_MINUTES} minutos. Use 'every_{_MIN_INTERVAL_MINUTES}m' ou maior."
    return None


_WEEKDAY_PT = {
    'monday': 'segunda', 'tuesday': 'terça', 'wednesday': 'quarta',
    'thursday': 'quinta', 'friday': 'sexta', 'saturday': 'sábado', 'sunday': 'domingo',
}


def _next_id() -> str:
    # Sequência monotônica — nunca regride, mesmo após deleção de tasks.
    with get_db() as conn:
        row = conn.execute(
            "UPDATE task_id_sequence SET next_id = next_id + 1 WHERE id = 1 RETURNING next_id - 1 AS cur"
        ).fetchone()
        conn.commit()
        return str(row["cur"]).zfill(3)


def _freq_label(task: dict) -> str:
    import re
    freq = task.get('frequency', '')
    time_str = task.get('time', '')
    if freq == 'on_demand':
        return 'sob demanda'
    m = re.match(r'every_(\d+)m', freq)
    if m:
        return f"a cada {m.group(1)} min"
    if freq == 'once':
        return f"única vez em {task.get('next_run', '?')}"
    if freq == 'daily':
        return f"diária às {time_str}"
    if freq == 'weekly':
        wd = _WEEKDAY_PT.get(task.get('weekday', ''), task.get('weekday', ''))
        return f"semanal ({wd}) às {time_str}"
    if freq == 'monthly':
        return f"mensal (dia {task.get('day', '?')}) às {time_str}"
    m = re.match(r'every_(\d+)h', freq)
    if m:
        return f"a cada {m.group(1)}h"
    m = re.match(r'every_(\d+)d', freq)
    if m:
        return f"a cada {m.group(1)} dias"
    return freq


def _format_list(tasks: list[dict]) -> str:
    active = [t for t in tasks if t.get('status') not in ('completed', 'cancelled')]
    if not active:
        return "Nenhuma tarefa agendada no momento."
    lines = [f"**{len(active)} tarefa(s) agendada(s):**\n"]
    for t in active:
        status_label = {
            'active':  '✅ ativa',
            'draft':   '⏳ gerando código',
            'paused':  '⏸️ pausada',
            'error':   '❌ erro',
            'completed': '✔️ concluída',
        }.get(t.get('status', ''), t.get('status', ''))
        notify_label = '🔔 sim' if t.get('notify') else '🔕 não'
        lines.append(f"**[{t['id']}]** {t.get('name', '?')}")
        lines.append(f"  Frequência  : {_freq_label(t)}")
        if t.get('frequency') != 'on_demand':
            lines.append(f"  Próxima     : {t.get('next_run', 'N/A')}")
        lines.append(f"  Status      : {status_label}")
        lines.append(f"  Notificações: {notify_label}")
        if t.get('email'):
            lines.append(f"  Email       : {t['email']}")
        lines.append(f"  Descrição   : {t.get('description', '')}")
        lines.append("")
    return '\n'.join(lines)


def _push_artifacts(event_type: str, tokens: list[str], task_id: str, session_id_override: str = "") -> None:
    """Empurra artefatos diretamente para o stream SSE do chat."""
    try:
        from agent_multi import _push_event, _current_session
        session = session_id_override or _current_session.get()
        for token in tokens:
            _push_event(session, {"type": event_type, "token": token, "task_id": task_id})
    except Exception:
        pass  # silencioso — não quebra a tool se o stream não estiver ativo


def _row_to_dict(row) -> dict:
    return dict(row) if row else {}


def _current_user_id() -> str | None:
    try:
        from agent_multi import _current_user_id as _uid_var
        uid = _uid_var.get()
        return uid or None
    except Exception:
        return None


def _all_tasks(user_id: str | None = None) -> list[dict]:
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                f"SELECT * FROM scheduled_tasks WHERE user_id=? AND {_DOMINIO_CLAUSE} ORDER BY id",
                (user_id, dominio),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM scheduled_tasks WHERE {_DOMINIO_CLAUSE} ORDER BY id", (dominio,)
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


@tool
def schedule_task(
    name: str,
    description: str,
    frequency: str,
    notify: bool = False,
    time: Optional[str] = None,
    instructions: Optional[str] = None,
    email: Optional[str] = None,
    weekday: Optional[str] = None,
    day: Optional[str] = None,
    date_range: Optional[str] = None,
) -> str:
    """Agenda uma tarefa recorrente de RELATÓRIO ou GRÁFICO — NÃO use para alertas/threshold.

    Para alertas baseados em condição (ex: "me avise se X > N"), use `schedule_monitor`.

    Args:
        name: Nome curto da tarefa (ex: "Relatório Semanal de Produção").
        description: Resumo do que a tarefa faz.
        frequency: "once" | "daily" | "weekly" | "monthly" | "every_Xm" | "every_Xh" | "every_Xd" | "on_demand"
        date_range: Período de análise — use quando diferente da janela padrão da frequência.
                    "ytd" (ano atual) | "mtd" (mês atual) | "today" | "last_7d" | "last_30d" | "last_90d"
        notify: True para notificar no dashboard quando a tarefa executar.
        time: Hora "HH:MM". Ignorado se frequency="on_demand".
        instructions: Passo a passo de referência para o task_code (não ativa a tarefa —
                      use test_task_code + save_task_code depois de criar).
        email: Email para envio do relatório (opcional).
        weekday: Dia da semana se frequency="weekly" (ex: "monday").
        day: Dia do mês se frequency="monthly" (ex: "15").
    """
    err = _validate_frequency(frequency)
    if err:
        return err

    try:
        task_id = _next_id()
        user_id = _current_user_id()
        next_run = calculate_next_run(frequency, time, weekday, day)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with get_db() as conn:
            conn.execute(
                """INSERT INTO scheduled_tasks
                   (id, name, description, instructions, frequency, time, weekday,
                    day, email, notify, date_range, status, next_run, last_run, created_at, user_id, dominio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft',?,NULL,?,?,?)""",
                (task_id, name, description, instructions, frequency, time,
                 weekday, day, email, int(notify), date_range, next_run, now, user_id,
                 db_config.dominio_filename()),
            )
            conn.commit()
    except Exception as exc:
        logger.error("[schedule_task] Erro ao criar tarefa: %s\n%s", exc, traceback.format_exc())
        return f"❌ Erro interno ao criar tarefa: {exc}"

    tasks = _all_tasks(user_id)
    schedule_info = "Execute quando quiser clicando em ▶ no painel de tarefas." if frequency == 'on_demand' else f"Próxima execução agendada: {next_run}"
    return (
        f"Tarefa **[{task_id}]** criada (aguardando código). {schedule_info}\n\n"
        + _format_list(tasks)
        + "\nPara remover tarefa redundante: **delete task [ID]**"
    )


def _default_monitor_code(name: str, condition_sql: str) -> str:
    """task_code padrão de um monitor recém-criado — repete a leitura feita por
    condition_sql (SQL direto ou spec JSON via GraphQL, ver scheduler/condition.py)
    e retorna o valor apurado como texto."""
    if db_config.modo_acesso() == "graphql":
        import json as _json
        spec = _json.loads(condition_sql)
        where = spec.get("where")
        if spec.get("type") == "aggregate":
            call = (
                "ctx.graphql_aggregate(\n"
                f"        table={spec['table']!r}, group_by={spec['group_by']!r},\n"
                f"        metric_field={spec['metric_field']!r}, metric_op={spec['metric_op']!r},\n"
                f"        where={where!r},\n"
                "    )"
            )
            extract = "val = rows[0].get('metricValue')"
        else:
            call = f"ctx.graphql_table(\n        table={spec['table']!r}, where={where!r},\n    )"
            value_field = spec.get("value_field")
            extract = (
                f"val = rows[0].get({value_field!r})" if value_field
                else "val = list(rows[0].values())[0]"
            )
        return (
            "def run(from_date, to_date, ctx):\n"
            f"    rows = {call}\n"
            "    if not rows:\n"
            "        return 'Sem dados'\n"
            f"    {extract}\n"
            f"    return '{name}: ' + str(val)\n"
        )

    _sql_escaped = condition_sql.replace('"""', "'''")
    return (
        "def run(from_date, to_date, ctx):\n"
        f'    rows = ctx.sql("""\n        {_sql_escaped}\n    """)\n'
        "    if not rows:\n"
        "        return 'Sem dados'\n"
        "    val = list(rows[0].values())[0]\n"
        f"    return '{name}: ' + str(val)\n"
    )


@tool
def schedule_monitor(
    name: str,
    description: str,
    frequency: str,
    condition_sql: str,
    condition_operator: str,
    condition_threshold: Optional[float] = None,
    date_range: Optional[str] = None,
    time: Optional[str] = None,
    weekday: Optional[str] = None,
    day: Optional[str] = None,
) -> str:
    """Cria um monitor de threshold — executa e notifica SOMENTE quando a condição for atingida.

    Use para pedidos como: "me alerte se X > N", "notifique se não houver Y",
    "monitore Z e avise quando passar de N".

    Notificações são sempre ativas (não precisam de notify=True — é automático).
    O task_code deve apenas retornar o valor atual — sem lógica condicional.

    Args:
        name: Nome do monitor (ex: "Monitor de Pedidos").
        description: O que monitora e qual a condição (ex: "Alerta se total de pedidos do dia >= 160").
        frequency: Com que frequência verificar. Use "every_5m", "every_1h", "daily", etc.
        condition_sql: Em domínios com database.modo=sql (a maioria): query SELECT que retorna
                       um escalar ou linhas para avaliação (dialeto SQLite).
                       Para escalar: "SELECT COUNT(*) FROM tabela WHERE metrica < 10"
                       Para existência: "SELECT id FROM tabela WHERE condicao LIMIT 1"
                       Em domínios com database.modo=graphql: NÃO é SQL — é uma string JSON.
                       Veja o formato exato (aggregate/table) na seção "Formato de condition_sql
                       neste domínio" do seu prompt de sistema antes de chamar esta tool.
        condition_operator: Como comparar o resultado:
                            ">"  — executa se valor > threshold
                            ">=" — executa se valor >= threshold
                            "<"  — executa se valor < threshold
                            "<=" — executa se valor <= threshold
                            "==" — executa se valor == threshold
                            "!=" — executa se valor != threshold
                            "is_empty"     — executa se a query não retornar linhas
                            "is_not_empty" — executa se a query retornar ao menos 1 linha
        condition_threshold: Valor numérico de referência. Obrigatório para operadores
                             numéricos (>, >=, <, <=, ==, !=). Ignorado para is_empty/is_not_empty.
        date_range: Período injetado como from_date/to_date no task_code.
                    "today" (padrão para monitores) | "ytd" | "mtd" | "last_7d" | "last_30d"
        time: Hora "HH:MM" (opcional).
        weekday: Dia da semana se frequency="weekly".
        day: Dia do mês se frequency="monthly".
    """
    err = _validate_frequency(frequency)
    if err:
        return err

    if condition_operator not in (">", ">=", "<", "<=", "==", "!=", "is_empty", "is_not_empty"):
        return f"❌ condition_operator inválido: '{condition_operator}'. Use: > >= < <= == != is_empty is_not_empty"

    if condition_operator not in ("is_empty", "is_not_empty") and condition_threshold is None:
        return f"❌ condition_threshold obrigatório para operador '{condition_operator}'."

    # Valida condition_sql executando antes de salvar (SQL direto ou spec JSON via
    # GraphQL, dependendo de database.modo — ver scheduler/condition.py)
    from .condition import run_condition_query, ConditionError, describe_condition_format

    try:
        run_condition_query(condition_sql)
    except ConditionError as exc:
        return (
            f"❌ **condition_sql inválido:** {exc}\n\n"
            f"Formato esperado: {describe_condition_format()}"
        )
    except Exception as exc:
        dica = db_config.get_secao("Dica para condition_sql") or "consulte o schema em config/dominio.md."
        return (
            f"❌ **condition_sql inválido:** a consulta falhou com o erro abaixo.\n"
            f"Corrija antes de criar o monitor.\n\n"
            f"Erro: `{exc}`\n\n"
            f"Dica: {dica}"
        )

    effective_date_range = date_range or "today"

    # task_code padrão: repete a leitura de condition_sql e retorna o valor atual como
    # string. Suficiente para qualquer monitor simples — pode ser substituído depois
    # via save_task_code.
    default_code = _default_monitor_code(name, condition_sql)

    try:
        task_id = _next_id()
        user_id = _current_user_id()
        next_run = calculate_next_run(frequency, time, weekday, day)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with get_db() as conn:
            conn.execute(
                """INSERT INTO scheduled_tasks
                   (id, name, description, task_code, frequency, time, weekday, day,
                    notify, date_range, condition_sql, condition_operator, condition_threshold,
                    status, next_run, last_run, created_at, user_id, dominio)
                   VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,'active',?,NULL,?,?,?)""",
                (task_id, name, description, default_code, frequency, time, weekday, day,
                 effective_date_range, condition_sql, condition_operator, condition_threshold,
                 next_run, now, user_id, db_config.dominio_filename()),
            )
            conn.commit()
    except Exception as exc:
        logger.error("[schedule_monitor] Erro ao criar monitor: %s\n%s", exc, traceback.format_exc())
        return f"❌ Erro interno ao criar monitor: {exc}"

    tasks = _all_tasks(user_id)
    op_desc = (
        f"executa se sem resultados" if condition_operator == "is_empty" else
        f"executa se houver resultados" if condition_operator == "is_not_empty" else
        f"executa se valor {condition_operator} {condition_threshold}"
    )
    return (
        f"Monitor **[{task_id}]** criado e ativo. Condição: {op_desc}. Próxima verificação: {next_run}\n\n"
        f"O monitor já tem task_code padrão (retorna valor atual). "
        f"Para personalizar o relatório gerado quando a condição for atingida, use save_task_code.\n\n"
        + _format_list(tasks)
    )


@tool
def set_task_instructions(task_id: str, instructions: str) -> str:
    """Define ou substitui as instruções de execução de uma tarefa (documentação — não ativa a tarefa).

    O daemon só executa tarefas com task_code definido — instructions é apenas o
    passo a passo de referência usado para escrever esse código. Após chamar esta
    tool, use test_task_code e save_task_code para de fato ativar a tarefa.

    Args:
        task_id: ID da tarefa (ex: "001").
        instructions: Passo a passo completo com código Python validado.
    """
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (task_id, db_config.dominio_filename()),
        ).fetchone()
        if not row:
            return f"Tarefa '{task_id}' não encontrada."
        conn.execute(
            "UPDATE scheduled_tasks SET instructions = ? WHERE id = ?",
            (instructions, task_id),
        )
        conn.commit()

    uid = _current_user_id()
    tasks = _all_tasks(uid)
    return (
        f"Instruções da tarefa **[{task_id}]** definidas. "
        "Use test_task_code + save_task_code para ativá-la.\n\n"
        + _format_list(tasks)
    )


@tool
def list_scheduled_tasks() -> str:
    """Lista todas as tarefas agendadas com status, frequência e descrição."""
    return _format_list(_all_tasks(_current_user_id()))


@tool
def get_task_instructions(task_id: str) -> str:
    """Retorna as instructions de execução de uma tarefa (passo a passo + código).

    Use antes de editar uma tarefa para obter o contexto atual do que ela executa.

    Args:
        task_id: ID da tarefa (ex: "001").
    """
    with get_db() as conn:
        row = conn.execute(
            f"SELECT name, instructions FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (task_id, db_config.dominio_filename()),
        ).fetchone()
    if not row:
        return f"Tarefa '{task_id}' não encontrada."
    instructions = row['instructions'] or '(sem instructions definidas)'
    return f"**Tarefa [{task_id}] — {row['name']}**\n\nInstruções atuais:\n{instructions}"


@tool
def toggle_pause_task(task_id: str) -> str:
    """Pausa uma tarefa ativa ou retoma uma tarefa pausada.

    Não afeta tasks em execução, concluídas ou com erro.

    Args:
        task_id: ID da tarefa (ex: "001"). Use list_scheduled_tasks para ver os IDs.
    """
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT name, status FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (task_id, dominio),
        ).fetchone()
        if not row:
            ids = [r['id'] for r in conn.execute(
                f"SELECT id FROM scheduled_tasks WHERE {_DOMINIO_CLAUSE}", (dominio,)
            ).fetchall()]
            return f"Tarefa '{task_id}' não encontrada. IDs existentes: {ids}"
        current = row["status"]
        if current not in ("active", "paused"):
            return f"Não é possível pausar/retomar tarefa com status '{current}'."
        new_status = "paused" if current == "active" else "active"
        conn.execute("UPDATE scheduled_tasks SET status=? WHERE id=?", (new_status, task_id))
        conn.commit()

    uid = _current_user_id()
    action = "pausada" if new_status == "paused" else "retomada"
    return (
        f"Tarefa **[{task_id}]** {action}.\n\n"
        + _format_list(_all_tasks(uid))
    )


@tool
def delete_scheduled_task(task_id: str) -> str:
    """Remove uma tarefa agendada pelo ID.

    Args:
        task_id: ID da tarefa (ex: "001"). Use list_scheduled_tasks para ver os IDs.
    """
    uid = _current_user_id()
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT name FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (task_id, dominio),
        ).fetchone()
        if not row:
            ids = [r['id'] for r in conn.execute(
                f"SELECT id FROM scheduled_tasks WHERE user_id=? AND {_DOMINIO_CLAUSE}", (uid, dominio)
            ).fetchall()]
            return f"Tarefa '{task_id}' não encontrada. IDs existentes: {ids}"
        name = row['name']
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_code_versions WHERE task_id = ?", (task_id,))
        conn.commit()

    return (
        f"Tarefa **[{task_id}]** ({name}) removida.\n\n"
        + _format_list(_all_tasks(uid))
    )


@tool
def update_scheduled_task(
    task_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    frequency: Optional[str] = None,
    time: Optional[str] = None,
    email: Optional[str] = None,
    notify: Optional[bool] = None,
    weekday: Optional[str] = None,
    day: Optional[str] = None,
    condition_sql: Optional[str] = None,
    condition_operator: Optional[str] = None,
    condition_threshold: Optional[float] = None,
    date_range: Optional[str] = None,
) -> str:
    """Edita campos de uma tarefa existente. Apenas os campos fornecidos são alterados.

    Se frequency, time, weekday ou day forem alterados, next_run é recalculado.
    Para editar as instruções de execução use set_task_instructions.

    Se a tarefa deveria ter condition_sql mas não tem (ex: foi criada sem threshold),
    use este tool para adicionar condition_sql + condition_operator + notify=True agora.

    Args:
        task_id: ID da tarefa (ex: "001").
        name: Novo nome curto.
        description: Nova descrição legível (o que aparece na listagem).
        frequency: Nova frequência.
        time: Novo horário "HH:MM".
        email: Novo email (string vazia para remover).
        notify: True para habilitar notificações, False para desabilitar.
        weekday: Novo dia da semana (se frequency="weekly").
        day: Novo dia do mês (se frequency="monthly").
        condition_sql: Nova query de condição (string vazia para remover).
        condition_operator: Novo operador (">" | "<" | ">=" | "<=" | "==" | "!=" | "is_empty" | "is_not_empty").
        condition_threshold: Novo threshold numérico.
    """
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}", (task_id, dominio)
        ).fetchone()
        if not row:
            ids = [r['id'] for r in conn.execute(
                f"SELECT id FROM scheduled_tasks WHERE {_DOMINIO_CLAUSE}", (dominio,)
            ).fetchall()]
            return f"Tarefa '{task_id}' não encontrada. IDs existentes: {ids}"

        task = _row_to_dict(row)
        updates: dict = {}
        if name is not None:
            updates['name'] = name
        if description is not None:
            updates['description'] = description
        if email is not None:
            updates['email'] = email or None
        if notify is not None:
            updates['notify'] = int(notify)
        if weekday is not None:
            updates['weekday'] = weekday
        if day is not None:
            updates['day'] = day
        if frequency is not None:
            err = _validate_frequency(frequency)
            if err:
                return err
            updates['frequency'] = frequency
        if time is not None:
            updates['time'] = time
        if condition_sql is not None:
            updates['condition_sql'] = condition_sql or None
        if condition_operator is not None:
            updates['condition_operator'] = condition_operator or None
        if condition_threshold is not None:
            updates['condition_threshold'] = condition_threshold
        if date_range is not None:
            updates['date_range'] = date_range or None

        sched_changed = any(p is not None for p in (frequency, time, weekday, day))
        if sched_changed:
            updates['next_run'] = calculate_next_run(
                updates.get('frequency', task.get('frequency', 'daily')),
                updates.get('time', task.get('time', '08:00')),
                updates.get('weekday', task.get('weekday')),
                updates.get('day', task.get('day')),
            )

        if not updates:
            return "Nenhum campo fornecido para atualizar."

        set_clause = ', '.join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE scheduled_tasks SET {set_clause} WHERE id = ?",
            (*updates.values(), task_id),
        )
        conn.commit()

    uid = _current_user_id()
    return (
        f"Tarefa **[{task_id}]** atualizada. Campos: {', '.join(updates)}\n\n"
        + _format_list(_all_tasks(uid))
    )


@tool
def get_task_code(task_id: str) -> str:
    """Retorna o task_code Python armazenado de uma tarefa.

    Use este tool antes de editar o código para obter a versão atual.

    Args:
        task_id: ID da tarefa (ex: "001").
    """
    with get_db() as conn:
        row = conn.execute(
            f"SELECT name, task_code FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (task_id, db_config.dominio_filename()),
        ).fetchone()
    if not row:
        return f"Tarefa '{task_id}' não encontrada."
    if not row['task_code']:
        return f"Tarefa [{task_id}] não possui task_code — está em 'draft' e o daemon a ignora."
    return f"**Tarefa [{task_id}] — {row['name']}**\n\nCódigo atual:\n```python\n{row['task_code']}\n```"


@tool
def test_task_code(task_id: str, code: str, from_date: str = "", to_date: str = "", session_id_override: str = "") -> str:
    """Compila e executa um trecho de task_code para validação antes de salvar.

    O código deve definir `def run(from_date, to_date, ctx)` e retornar token(s).
    Executa em sandbox com builtins restritos + matplotlib/numpy/pandas/openpyxl.

    Args:
        task_id: ID da tarefa (usado apenas para nomear a sessão de teste).
        code: Código Python completo a testar.
        from_date: Data inicial no formato YYYY-MM-DD (padrão: 7 dias atrás).
        to_date: Data final no formato YYYY-MM-DD (padrão: hoje).
        session_id_override: Session ID explícito para envio do preview ao chat.
                             Quando fornecido, tem prioridade sobre _current_session.
                             Use quando a tool é chamada de dentro de um grafo agendado.
    """
    from .runner import run_task_code, default_test_range, TaskCodeError
    from datetime import datetime

    if not from_date or not to_date:
        # Usa o date_range já salvo da tarefa (se houver) para que o teste
        # reflita o período real que será injetado em produção — evita
        # confusão quando o código usa filtros de data exata (ex: 'today').
        from .daemon import _date_range_for_task
        with get_db() as conn:
            row = conn.execute(
                f"SELECT date_range, frequency FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
                (task_id, db_config.dominio_filename()),
            ).fetchone()
        if row and row["date_range"]:
            from_date, to_date = _date_range_for_task(dict(row))
        else:
            from_date, to_date = default_test_range()

    session_id = f"test_{task_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        tokens, ctx = run_task_code(code, from_date, to_date, session_id, user_id=_current_user_id(), is_test=True)
        _push_artifacts("artifact_preview", tokens, task_id, session_id_override=session_id_override)

        alert_lines = []
        for a in ctx.test_alerts():
            parts = [f"  🔔 {a['message']}"]
            if a.get('value') is not None:
                parts.append(f"valor={a['value']}")
            if a.get('threshold') is not None:
                parts.append(f"threshold={a['threshold']}")
            alert_lines.append(" | ".join(parts))

        alert_section = (
            "\n\n**Notificações que seriam disparadas:**\n" + "\n".join(alert_lines)
            if alert_lines else ""
        )

        return (
            f"✅ **Teste bem-sucedido!** Período: {from_date} → {to_date}\n"
            f"Artefatos enviados para o chat: {', '.join(tokens)}"
            f"{alert_section}"
        )
    except TaskCodeError as e:
        return f"❌ **Erro no código:**\n```\n{e}\n```\nCorreja e teste novamente antes de salvar."
    except Exception as e:
        return f"❌ **Erro inesperado:**\n```\n{e}\n```"


@tool
def save_task_code(task_id: str, code: str) -> str:
    """Salva task_code Python em uma tarefa, criando versão histórica para rollback.

    Após salvar, a tarefa executa o código diretamente (modo determinístico),
    sem passar pelo LLM. Use test_task_code antes de salvar.


    Args:
        task_id: ID da tarefa (ex: "001").
        code: Código Python completo com `def run(from_date, to_date, ctx)`.
    """
    from datetime import datetime

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _notify_warning = ""
    row = None
    try:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT name, task_code, condition_sql FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
                (task_id, db_config.dominio_filename()),
            ).fetchone()
            if not row:
                return f"Tarefa '{task_id}' não encontrada."

            # Bloqueia ctx.notify() no código de tarefas com condition_sql
            if "ctx.notify" in code and row.get("condition_sql"):
                return (
                    "❌ **Código recusado:** esta tarefa tem `condition_sql` definido — "
                    "a condição e a notificação são gerenciadas automaticamente pelo daemon.\n\n"
                    "Remova qualquer chamada a `ctx.notify()` do código. "
                    "O task_code deve apenas coletar dados e gerar o relatório/gráfico."
                )

            # Prepara aviso se ctx.notify() está no código sem condition_sql (salva mesmo assim)
            if "ctx.notify" in code and not row.get("condition_sql"):
                _notify_warning = (
                    "\n\n⚠️ **Atenção:** o código usa `ctx.notify()`, mas a tarefa não tem "
                    "`condition_sql` definido. Para alertas baseados em threshold, use "
                    "`update_scheduled_task` para definir `condition_sql` + `condition_operator` "
                    "+ `condition_threshold` e remova `ctx.notify()` do código."
                )

            # Arquiva versão anterior se existir
            if row['task_code']:
                ver_row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS nxt FROM task_code_versions WHERE task_id = ?",
                    (task_id,)
                ).fetchone()
                version = ver_row['nxt']
                conn.execute(
                    "INSERT INTO task_code_versions (task_id, version, code, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, version, row['task_code'], now),
                )

            conn.execute(
                "UPDATE scheduled_tasks SET task_code = ?, status = 'active' WHERE id = ?",
                (code, task_id),
            )
            conn.commit()

    except Exception as exc:
        logger.error("[save_task_code] Erro ao salvar task %s: %s\n%s", task_id, exc, traceback.format_exc())
        return f"❌ Erro interno ao salvar task_code [{task_id}]: {exc}"

    # Promove artifacts do teste para a sessão real (aparecem no painel de artifacts)
    try:
        from agent_multi import _current_session
        import chart_store
        chart_store.promote_test_artifacts(task_id, _current_session.get())
    except Exception:
        pass

    # Notifica o chat que o código foi persistido
    _push_artifacts("artifact_saved", [], task_id)
    return (
        f"✅ **task_code salvo** na tarefa **[{task_id}]** ({row['name']}).\n"
        "A próxima execução usará este código diretamente (modo determinístico).\n"
        "Use `get_task_code_versions` para ver o histórico ou `restore_task_code_version` para reverter."
        + _notify_warning
    )


@tool
def get_task_code_versions(task_id: str) -> str:
    """Lista as versões históricas do task_code de uma tarefa (para rollback).

    Args:
        task_id: ID da tarefa (ex: "001").
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT version, created_at FROM task_code_versions WHERE task_id = ? ORDER BY version DESC",
            (task_id,)
        ).fetchall()
    if not rows:
        return f"Tarefa [{task_id}] não possui versões arquivadas."
    lines = [f"**Versões do task_code — Tarefa [{task_id}]:**\n"]
    for r in rows:
        lines.append(f"  v{r['version']} — salva em {r['created_at']}")
    lines.append("\nUse `restore_task_code_version` para restaurar uma versão.")
    return '\n'.join(lines)


@tool
def restore_task_code_version(task_id: str, version: int) -> str:
    """Restaura uma versão anterior do task_code de uma tarefa.

    O código atual é arquivado como nova versão antes de restaurar.

    Args:
        task_id: ID da tarefa (ex: "001").
        version: Número da versão a restaurar (use get_task_code_versions para listar).
    """
    from datetime import datetime

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        ver_row = conn.execute(
            "SELECT code FROM task_code_versions WHERE task_id = ? AND version = ?",
            (task_id, version)
        ).fetchone()
        if not ver_row:
            return f"Versão {version} não encontrada para a tarefa [{task_id}]."

        task_row = conn.execute(
            f"SELECT name, task_code FROM scheduled_tasks WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (task_id, db_config.dominio_filename()),
        ).fetchone()
        if not task_row:
            return f"Tarefa '{task_id}' não encontrada."

        # Arquiva versão atual antes de restaurar
        if task_row['task_code']:
            new_ver = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS nxt FROM task_code_versions WHERE task_id = ?",
                (task_id,)
            ).fetchone()['nxt']
            conn.execute(
                "INSERT INTO task_code_versions (task_id, version, code, created_at) VALUES (?, ?, ?, ?)",
                (task_id, new_ver, task_row['task_code'], now),
            )

        conn.execute(
            "UPDATE scheduled_tasks SET task_code = ? WHERE id = ?",
            (ver_row['code'], task_id),
        )
        conn.commit()

    return (
        f"✅ **Versão {version} restaurada** na tarefa **[{task_id}]** ({task_row['name']}).\n"
        "O código anterior foi arquivado como nova versão."
    )
