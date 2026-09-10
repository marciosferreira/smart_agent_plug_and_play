"""
Daemon do scheduler — lê scheduled_tasks do SQLite, verifica horários e
dispara execuções em thread separada. Zero LLM neste módulo.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import db_config
from db import get_db
from .md_parser import calculate_next_run
from .runner import run_task_code, TaskCodeError

logger = logging.getLogger(__name__)

import os

REPORTS_DIR = Path(__file__).parent.parent / "reports"
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

_CHECK_INTERVAL_SECONDS = 60
_TASK_TIMEOUT_SECONDS   = 600          # 10 min — mata execuções travadas
_RETRY_BACKOFF_MINUTES  = [5, 15, 60]  # espera entre tentativas (1ª, 2ª, 3ª)

_PDF_TOKEN = re.compile(r'\[pdf:([a-f0-9\-]{36})\]')


def _resolve_pdf_links(content: str) -> str:
    return _PDF_TOKEN.sub(
        lambda m: f"[📄 Abrir relatório PDF]({BACKEND_URL}/pdf/{m.group(1)})",
        content,
    )


def _save_report(task: dict, content: str, now: datetime) -> Path:
    safe_name = re.sub(r'[^\w\-]', '_', task.get('name', 'report'))[:30].strip('_')
    folder_name = f"{task['id']}_{safe_name}_{now.strftime('%Y-%m-%d_%H%M')}"
    folder = REPORTS_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    pdf_ids = _PDF_TOKEN.findall(content)
    content_with_links = _resolve_pdf_links(content)
    (folder / 'content.md').write_text(content_with_links, encoding='utf-8')

    pdf_urls = [f"{BACKEND_URL}/pdf/{pid}" for pid in pdf_ids]
    metadata = {
        'task_id': task['id'],
        'task_name': task.get('name'),
        'run_at': now.isoformat(),
        'status': 'pending_send',
        'pdf_urls': pdf_urls,
        'email': {
            'to': task.get('email'),
            'subject': f"{task.get('name')} — {now.strftime('%Y-%m-%d %H:%M')}",
            'body': f"Segue em anexo o relatório: {task.get('name')}.",
            'attachments': pdf_urls,
        } if task.get('email') else None,
        'schedule': {
            'frequency': task.get('frequency'),
            'weekday': task.get('weekday'),
            'day': task.get('day'),
            'time': task.get('time'),
        },
    }
    (folder / 'metadata.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return folder


def _date_range_for_task(task: dict) -> tuple[str, str]:
    """Calcula from_date/to_date para a tarefa.

    Se date_range estiver definido, usa ele (independente da frequência).
    Caso contrário, usa a janela padrão baseada na frequência.
    """
    import re
    today = datetime.now().date()

    dr = (task.get('date_range') or '').strip()
    if dr == 'ytd':
        return f"{today.year}-01-01", today.isoformat()
    if dr == 'mtd':
        return f"{today.year}-{today.month:02d}-01", today.isoformat()
    if dr == 'today':
        return today.isoformat(), today.isoformat()
    if dr.startswith('last_'):
        m = re.match(r'last_(\d+)d', dr)
        if m:
            return (today - timedelta(days=int(m.group(1)))).isoformat(), today.isoformat()

    # Fallback: janela baseada na frequência
    freq = task.get('frequency', 'daily')
    if freq == 'daily':
        delta = 1
    elif freq == 'weekly':
        delta = 7
    elif freq == 'monthly':
        delta = 30
    elif freq in ('once', 'on_demand'):
        delta = 7
    else:
        m = re.match(r'every_(\d+)d', freq)
        if m:
            delta = int(m.group(1))
        elif re.match(r'every_(\d+)[hm]', freq):
            delta = 1
        else:
            delta = 7

    return (today - timedelta(days=delta)).isoformat(), today.isoformat()



def _start_run(task_id: str, started_at: str) -> int:
    """Insere uma linha em task_runs e retorna o run_id."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, started_at, status) VALUES (?, ?, 'running')",
            (task_id, started_at),
        )
        conn.commit()
        return cur.lastrowid


def _finish_run(run_id: int, status: str, output: str = None, error: str = None) -> None:
    ended_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        conn.execute(
            "UPDATE task_runs SET ended_at=?, status=?, output=?, error=? WHERE id=?",
            (ended_at, status, output, error, run_id),
        )
        conn.commit()


def _evaluate_condition(task: dict) -> tuple[bool, str]:
    """Avalia condition_sql antes de executar a tarefa.

    Retorna (should_run, detail) onde detail é incluído na notificação automática.
    """
    from .condition import run_condition_query

    sql      = (task.get("condition_sql") or "").strip()
    operator = (task.get("condition_operator") or "is_not_empty").strip()
    threshold = task.get("condition_threshold")

    try:
        rows = run_condition_query(sql)
    except Exception as exc:
        logger.warning("[daemon] Erro ao avaliar condition_sql da task %s: %s", task.get("id"), exc)
        return False, f"erro na condição: {exc}"

    if operator == "is_empty":
        met = len(rows) == 0
        detail = "sem registros" if met else f"{len(rows)} registro(s) — condição não atingida"
        return met, detail

    if operator == "is_not_empty":
        met = len(rows) > 0
        detail = f"{len(rows)} registro(s)" if met else "sem registros — condição não atingida"
        return met, detail

    # Operadores numéricos — usa o primeiro valor da primeira linha
    if not rows:
        return False, "sem dados"

    first_val = list(dict(rows[0]).values())[0]
    try:
        value = float(first_val)
    except (TypeError, ValueError):
        return False, f"valor não numérico: {first_val}"

    th = float(threshold) if threshold is not None else 0.0
    met = {">": value > th, "<": value < th, ">=": value >= th,
           "<=": value <= th, "==": value == th, "!=": value != th}.get(operator, False)

    val_fmt = int(value) if value == int(value) else round(value, 2)
    th_fmt  = int(th)    if th    == int(th)    else round(th, 2)
    detail  = f"{val_fmt} {operator} {th_fmt}"
    return met, detail


def _execute_task(task: dict, manual: bool = False) -> None:
    now      = datetime.now()
    task_id  = task['id']
    now_str  = now.strftime('%Y-%m-%d %H:%M:%S')

    # ── 0. Avalia condição (se definida) — sem criar run record se não atingida,
    # exceto em execução manual (▶ na UI): esta sempre roda o task_code de verdade,
    # para o usuário ver o resultado real em vez de um clique que não faz nada
    # visível — mas sem disparar notificação de alerta quando a condição não foi
    # realmente atingida (ver notify_enabled mais abaixo).
    condition_detail = ""
    condition_met = True
    if task.get("condition_sql"):
        should_run, condition_detail = _evaluate_condition(task)
        condition_met = should_run
        next_run = calculate_next_run(task.get('frequency', 'daily'), task.get('time'), task.get('weekday'), task.get('day'))

        if not should_run:
            logger.info("[daemon] Task %s '%s' — condição não atingida: %s", task_id, task.get('name'), condition_detail)
            with get_db() as conn:
                conn.execute(
                    "UPDATE scheduled_tasks SET next_run=?, condition_state=0 WHERE id=?",
                    (next_run, task_id),
                )
                conn.commit()
            if not manual:
                return
            logger.info("[daemon] Task %s — execução manual força o teste mesmo com condição não atingida", task_id)
        else:
            last_state = int(task.get("condition_state") or 0)
            if last_state == 1 and not manual:
                # Daemon: condição ainda ativa, já notificou — aguarda reset
                logger.info("[daemon] Task %s '%s' — condição ativa, aguardando reset", task_id, task.get('name'))
                with get_db() as conn:
                    conn.execute("UPDATE scheduled_tasks SET next_run=? WHERE id=?", (next_run, task_id))
                    conn.commit()
                return

        # Condição atingida, ou execução manual forçada — executa
        # (manual ignora condition_state, daemon só executa em transição 0→1)

    # ── 1. Marca como running (impede execução dupla) ────────────────────────
    with get_db() as conn:
        updated = conn.execute(
            "UPDATE scheduled_tasks SET status='running' WHERE id=? AND status='active'",
            (task_id,),
        ).rowcount
        conn.commit()

    if updated == 0:
        # Outra thread já pegou esta task (ou foi pausada/cancelada entre o
        # SELECT e o UPDATE). Abandona silenciosamente.
        logger.info("[daemon] Task %s ignorada (já em running ou inativa)", task_id)
        return

    run_id = _start_run(task_id, now_str)
    logger.info("[daemon] Iniciando task %s '%s' (run #%d)", task_id, task.get('name'), run_id)

    # ── 2. Executa com timeout ───────────────────────────────────────────────
    try:
        import concurrent.futures
        session_id = f"daemon_{task_id}_{now.strftime('%Y%m%d%H%M')}"

        if not task.get("task_code"):
            msg = "Tarefa sem task_code — salve o código via chat antes de ativar."
            logger.warning("[daemon] Task %s ignorada: %s", task_id, msg)
            _finish_run(run_id, 'error', error=msg)
            _handle_retry(task, now_str, msg)
            return

        from_date, to_date = _date_range_for_task(task)

        user_id = task.get("user_id")

        task_name = task.get("name", "")
        if condition_detail:
            task_name = f"{task_name}: {condition_detail}"

        # Em teste manual forçado (condição não atingida), não dispara a notificação
        # automática de alerta — ela sinalizaria um threshold ultrapassado que não
        # aconteceu de verdade.
        notify_enabled = bool(task.get("notify", 0)) and condition_met

        def _run_code():
            tokens = run_task_code(
                task["task_code"], from_date, to_date, session_id,
                user_id=user_id, notify_enabled=notify_enabled,
                task_name=task_name,
            )
            return " ".join(tokens)

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_run_code)
        try:
            result = future.result(timeout=_TASK_TIMEOUT_SECONDS)
        finally:
            # shutdown(wait=False) evita bloquear o daemon enquanto a thread
            # travada termina — sem isso o `with` esperaria eternamente
            ex.shutdown(wait=False, cancel_futures=True)

        logger.info("[daemon] Task %s concluída (run #%d)", task_id, run_id)

        if task.get("condition_sql") and not condition_met:
            result = f"[Teste manual — condição NÃO atingida: {condition_detail}]\n\n{result}"

        folder = _save_report(task, result, now)
        logger.info("[daemon] Relatório salvo em %s", folder)

        _finish_run(run_id, 'success', output=result[:4000])

        # ── 3a. Sucesso: agenda próxima execução ─────────────────────────────
        last_run = now_str
        if task.get('frequency') == 'once':
            with get_db() as conn:
                conn.execute(
                    "UPDATE scheduled_tasks SET last_run=?, status='completed', retry_count=0 WHERE id=?",
                    (last_run, task_id),
                )
                conn.commit()
        else:
            next_run = calculate_next_run(
                task.get('frequency', 'daily'),
                task.get('time', '08:00'),
                task.get('weekday'),
                task.get('day'),
            )
            # Se havia condição REALMENTE atingida, marca estado como ativo (1) para
            # evitar re-notificação — um teste manual forçado (condition_met=False)
            # mantém o estado em 0, senão suprimiria a notificação real seguinte.
            if task.get("condition_sql"):
                new_cond_state = 1 if condition_met else 0
            else:
                new_cond_state = int(task.get("condition_state") or 0)
            with get_db() as conn:
                conn.execute(
                    "UPDATE scheduled_tasks SET last_run=?, next_run=?, status='active', retry_count=0, condition_state=? WHERE id=?",
                    (last_run, next_run, new_cond_state, task_id),
                )
                conn.commit()

    except concurrent.futures.TimeoutError:
        msg = f"Timeout após {_TASK_TIMEOUT_SECONDS}s"
        logger.error("[daemon] Task %s excedeu timeout de %ds (run #%d)", task_id, _TASK_TIMEOUT_SECONDS, run_id)
        _finish_run(run_id, 'error', error=msg)
        _handle_retry(task, now_str, msg)

    except TaskCodeError as exc:
        logger.error("[daemon] Erro no task_code da task %s (run #%d): %s", task_id, run_id, exc)
        _finish_run(run_id, 'error', error=str(exc))
        _handle_retry(task, now_str, str(exc))

    except Exception as exc:
        logger.exception("[daemon] Falha na task %s (run #%d)", task_id, run_id)
        _finish_run(run_id, 'error', error=str(exc))
        _handle_retry(task, now_str, str(exc))


def _handle_retry(task: dict, last_run: str, error_msg: str) -> None:
    """Incrementa retry_count. Se ainda há tentativas, agenda reexecução com backoff.
    Caso contrário, marca a task como 'error' para revisão manual."""
    task_id     = task['id']
    retry_count = task.get('retry_count', 0) + 1
    max_retries = task.get('max_retries', 3)

    if retry_count <= max_retries:
        backoff_min = _RETRY_BACKOFF_MINUTES[min(retry_count - 1, len(_RETRY_BACKOFF_MINUTES) - 1)]
        next_run    = (datetime.now() + timedelta(minutes=backoff_min)).strftime('%Y-%m-%d %H:%M:%S')
        logger.warning(
            "[daemon] Task %s tentativa %d/%d — reagendada para %s (backoff %dmin)",
            task_id, retry_count, max_retries, next_run, backoff_min,
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET status='active', retry_count=?, next_run=?, last_run=? WHERE id=?",
                (retry_count, next_run, last_run, task_id),
            )
            conn.commit()
    else:
        logger.error(
            "[daemon] Task %s esgotou %d tentativas. Status: error. Erro: %s",
            task_id, max_retries, error_msg,
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET status='error', retry_count=?, last_run=? WHERE id=?",
                (retry_count, last_run, task_id),
            )
            conn.commit()


def check_due_tasks() -> None:
    with get_db() as conn:
        rows = conn.execute(
            # 'running' é excluído — evita execução dupla em tasks lentas
            # tarefas de outro domínio (dominio != ativo) nunca disparam aqui
            "SELECT * FROM scheduled_tasks WHERE status = 'active' AND (dominio = ? OR dominio IS NULL)",
            (db_config.dominio_filename(),),
        ).fetchall()

    now = datetime.now()
    for row in rows:
        task = dict(row)
        next_run_str = task.get('next_run')
        if not next_run_str:
            continue
        try:
            next_run = datetime.strptime(next_run_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        if next_run > now:
            continue
        _execute_task(task)


async def scheduler_loop() -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    logger.info("[daemon] Scheduler iniciado — intervalo: %ds", _CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.to_thread(check_due_tasks)
        except Exception:
            logger.exception("[daemon] Erro no loop do scheduler")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
