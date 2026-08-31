"""
Camada de persistência de gráficos gerados pelo agente.

Storage: tabela `charts` no mesmo mfg.db usado pelo checkpointer LangGraph.
Troca futura (ex: GCS): basta reimplementar save_chart / get_chart_b64
sem tocar no agente nem no frontend.
"""

import base64
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

import db_config

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _decode(v):
    """Garante que valores TEXT do SQLite sejam sempre str, nunca bytes."""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


def init_chart_store(db_path: Path) -> None:
    global _conn
    _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS charts (
            chart_id   TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            png_blob   BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            user_id    TEXT
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS pdfs (
            pdf_id     TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            pdf_blob   BLOB NOT NULL,
            filename   TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            user_id    TEXT
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS excels (
            excel_id   TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            excel_blob BLOB NOT NULL,
            filename   TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            user_id    TEXT
        )
    """)
    # Migração best-effort para bancos existentes (colunas já criadas são ignoradas)
    for stmt in (
        "ALTER TABLE charts ADD COLUMN user_id TEXT",
        "ALTER TABLE pdfs ADD COLUMN user_id TEXT",
        "ALTER TABLE excels ADD COLUMN user_id TEXT",
    ):
        try:
            _conn.execute(stmt)
        except Exception:
            pass
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS threshold_alerts (
            id         TEXT PRIMARY KEY,
            task_id    TEXT,
            message    TEXT NOT NULL,
            value      REAL,
            threshold  REAL,
            created_at TEXT NOT NULL,
            read       INTEGER NOT NULL DEFAULT 0,
            user_id    TEXT,
            dominio    TEXT
        )
    """)
    _conn.commit()


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def save_chart(session_id: str, png_bytes: bytes, user_id: str | None = None) -> str:
    if _conn is None:
        raise RuntimeError("chart_store não inicializado.")
    chart_id = str(uuid.uuid4())
    with _lock:
        _conn.execute(
            "INSERT INTO charts (chart_id, session_id, png_blob, created_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (chart_id, session_id, png_bytes, _now(), user_id or None),
        )
        _conn.commit()
    return chart_id


def save_pdf(session_id: str, pdf_bytes: bytes, filename: str, user_id: str | None = None) -> str:
    if _conn is None:
        raise RuntimeError("chart_store não inicializado.")
    pdf_id = str(uuid.uuid4())
    with _lock:
        _conn.execute(
            "INSERT INTO pdfs (pdf_id, session_id, pdf_blob, filename, created_at, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (pdf_id, session_id, pdf_bytes, filename, _now(), user_id or None),
        )
        _conn.commit()
    return pdf_id


def delete_chart(chart_id: str) -> None:
    if _conn is None:
        return
    with _lock:
        _conn.execute("DELETE FROM charts WHERE chart_id = ?", (chart_id,))
        _conn.commit()


def delete_all_charts() -> int:
    if _conn is None:
        return 0
    with _lock:
        c = _conn.execute("DELETE FROM charts").rowcount
        p = _conn.execute("DELETE FROM pdfs").rowcount
        e = _conn.execute("DELETE FROM excels").rowcount
        _conn.commit()
    return c + p + e


def delete_charts_for_session(session_id: str) -> None:
    if _conn is None:
        return
    with _lock:
        _conn.execute("DELETE FROM charts WHERE session_id = ?", (session_id,))
        _conn.commit()


def get_pdf(pdf_id: str) -> tuple[bytes, str] | None:
    if _conn is None:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT pdf_blob, filename FROM pdfs WHERE pdf_id = ?", (pdf_id,)
        ).fetchone()
    if not row:
        return None
    return row[0], row[1]


def save_excel(session_id: str, excel_bytes: bytes, filename: str, user_id: str | None = None) -> str:
    if _conn is None:
        raise RuntimeError("chart_store não inicializado.")
    excel_id = str(uuid.uuid4())
    with _lock:
        _conn.execute(
            "INSERT INTO excels (excel_id, session_id, excel_blob, filename, created_at, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (excel_id, session_id, excel_bytes, filename, _now(), user_id or None),
        )
        _conn.commit()
    return excel_id


def get_excel(excel_id: str) -> tuple[bytes, str] | None:
    if _conn is None:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT excel_blob, filename FROM excels WHERE excel_id = ?", (excel_id,)
        ).fetchone()
    if not row:
        return None
    return row[0], row[1]


def list_artifacts(user_id: str | None = None) -> list[dict]:
    """Retorna artefatos ordenados por data desc.

    Gráficos de sessões que também produziram um PDF são omitidos — eles já
    estão embutidos no PDF e não precisam aparecer como item separado.

    Filtra por user_id quando informado (aceita também registros antigos com
    user_id NULL, gerados antes da separação por usuário).
    """
    if _conn is None:
        return []
    user_clause = " WHERE (user_id = ? OR user_id IS NULL)"
    params = (user_id,)
    with _lock:
        charts = _conn.execute(
            "SELECT chart_id, session_id, created_at FROM charts"
            + (user_clause if user_id else "")
            + " ORDER BY created_at DESC",
            params if user_id else (),
        ).fetchall()
        pdfs = _conn.execute(
            "SELECT pdf_id, session_id, filename, created_at FROM pdfs"
            + (user_clause if user_id else "")
            + " ORDER BY created_at DESC",
            params if user_id else (),
        ).fetchall()
        excels = _conn.execute(
            "SELECT excel_id, session_id, filename, created_at FROM excels"
            + (user_clause if user_id else "")
            + " ORDER BY created_at DESC",
            params if user_id else (),
        ).fetchall()

    result = []
    for row in charts:
        result.append({"type": "chart", "id": _decode(row[0]), "session_id": _decode(row[1]), "created_at": _decode(row[2])})
    for row in pdfs:
        result.append({"type": "pdf", "id": _decode(row[0]), "session_id": _decode(row[1]), "filename": _decode(row[2]), "created_at": _decode(row[3])})
    for row in excels:
        result.append({"type": "excel", "id": _decode(row[0]), "session_id": _decode(row[1]), "filename": _decode(row[2]), "created_at": _decode(row[3])})

    result.sort(key=lambda x: x["created_at"], reverse=True)
    return result


def promote_test_artifacts(task_id: str, real_session_id: str) -> None:
    """Move artifacts de sessão test_{task_id}_* para a sessão real do chat.

    Chamado após save_task_code para que o preview do teste apareça no
    painel de artifacts com a sessão correta (não filtrada).
    """
    if _conn is None:
        return
    prefix = f"test_{task_id}_%"
    with _lock:
        for table in ("charts", "pdfs", "excels"):
            _conn.execute(
                f"UPDATE {table} SET session_id = ? WHERE session_id LIKE ?",
                (real_session_id, prefix),
            )
        _conn.commit()


def delete_artifact(artifact_type: str, artifact_id: str, user_id: str | None = None) -> bool:
    """Remove um artefato. Se user_id for informado, só apaga quando o artefato
    pertence a esse usuário ou não tem dono (user_id NULL)."""
    if _conn is None:
        return False
    table_col = {"chart": ("charts", "chart_id"), "pdf": ("pdfs", "pdf_id"), "excel": ("excels", "excel_id")}
    if artifact_type not in table_col:
        return False
    table, col = table_col[artifact_type]
    sql = f"DELETE FROM {table} WHERE {col} = ?"
    params: tuple = (artifact_id,)
    if user_id:
        sql += " AND (user_id = ? OR user_id IS NULL)"
        params = (artifact_id, user_id)
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
    return cur.rowcount > 0


def save_alert(session_id: str, message: str, value: float | None = None, threshold: float | None = None, user_id: str | None = None) -> str:
    if _conn is None:
        raise RuntimeError("chart_store não inicializado.")
    import re
    m = re.match(r'^daemon_(\w+)_', session_id)
    task_id = m.group(1) if m else None
    alert_id = str(uuid.uuid4())
    safe_value     = float(value)     if value     is not None else None
    safe_threshold = float(threshold) if threshold is not None else None
    with _lock:
        _conn.execute(
            "INSERT INTO threshold_alerts (id, task_id, message, value, threshold, created_at, read, user_id, dominio) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (alert_id, task_id, message, safe_value, safe_threshold, _now(), user_id, db_config.dominio_filename()),
        )
        _conn.commit()
    return alert_id


def get_alerts(unread_only: bool = True, user_id: str | None = None) -> list[dict]:
    if _conn is None:
        return []
    base = "SELECT id, task_id, message, value, threshold, created_at, read FROM threshold_alerts"
    dominio_clause = "(dominio = ? OR dominio IS NULL)"
    dominio = db_config.dominio_filename()
    if user_id and unread_only:
        sql = base + f" WHERE user_id=? AND read=0 AND {dominio_clause} ORDER BY created_at DESC"
        params = (user_id, dominio)
    elif user_id:
        sql = base + f" WHERE user_id=? AND {dominio_clause} ORDER BY created_at DESC LIMIT 100"
        params = (user_id, dominio)
    elif unread_only:
        sql = base + f" WHERE read=0 AND {dominio_clause} ORDER BY created_at DESC"
        params = (dominio,)
    else:
        sql = base + f" WHERE {dominio_clause} ORDER BY created_at DESC LIMIT 100"
        params = (dominio,)
    with _lock:
        rows = _conn.execute(sql, params).fetchall()

    def _real(v):
        if isinstance(v, bytes):
            import struct
            try:
                return float(struct.unpack('<q', v)[0])
            except Exception:
                return None
        return v

    return [
        {"id": _decode(r[0]), "task_id": _decode(r[1]), "message": _decode(r[2]),
         "value": _real(r[3]), "threshold": _real(r[4]), "created_at": _decode(r[5]), "read": r[6]}
        for r in rows
    ]


def mark_alert_read(alert_id: str, user_id: str | None = None) -> bool:
    if _conn is None:
        return False
    sql = "UPDATE threshold_alerts SET read=1 WHERE id=?"
    params: tuple = (alert_id,)
    if user_id:
        sql += " AND user_id=?"
        params = (alert_id, user_id)
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
    return cur.rowcount > 0


def mark_all_alerts_read(user_id: str | None = None) -> int:
    if _conn is None:
        return 0
    if user_id:
        sql, params = "UPDATE threshold_alerts SET read=1 WHERE read=0 AND user_id=?", (user_id,)
    else:
        sql, params = "UPDATE threshold_alerts SET read=1 WHERE read=0", ()
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
    return cur.rowcount


def delete_all_alerts(user_id: str | None = None) -> int:
    if _conn is None:
        return 0
    if user_id:
        sql, params = "DELETE FROM threshold_alerts WHERE user_id=?", (user_id,)
    else:
        sql, params = "DELETE FROM threshold_alerts", ()
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
    return cur.rowcount


def delete_alert(alert_id: str, user_id: str | None = None) -> bool:
    if _conn is None:
        return False
    sql = "DELETE FROM threshold_alerts WHERE id=?"
    params: tuple = (alert_id,)
    if user_id:
        sql += " AND user_id=?"
        params = (alert_id, user_id)
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
    return cur.rowcount > 0


def get_chart_b64(chart_id: str) -> str | None:
    if _conn is None:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT png_blob FROM charts WHERE chart_id = ?", (chart_id,)
        ).fetchone()
    if not row:
        return None
    return base64.b64encode(row[0]).decode()
