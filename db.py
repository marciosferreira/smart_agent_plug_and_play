import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import db_config

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "mfg.db")))


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def migrate_db():
    """Adiciona colunas e tabelas novas sem recriar o banco."""
    with get_db() as conn:
        for stmt in (
            "ALTER TABLE scheduled_tasks ADD COLUMN user_id TEXT",
            "ALTER TABLE scheduled_tasks ADD COLUMN notify INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scheduled_tasks ADD COLUMN date_range TEXT",
            "ALTER TABLE scheduled_tasks ADD COLUMN condition_state INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scheduled_tasks ADD COLUMN condition_sql TEXT",
            "ALTER TABLE scheduled_tasks ADD COLUMN condition_operator TEXT",
            "ALTER TABLE scheduled_tasks ADD COLUMN condition_threshold REAL",
            "ALTER TABLE scheduled_tasks ADD COLUMN dominio TEXT",
            # idioma no momento da criação — o daemon roda sem sessão de chat ativa
            # (sem user_id/lang de requisição), então precisa ficar gravado na própria
            # linha, igual dominio/user_id, para o relatório sair no idioma certo.
            "ALTER TABLE scheduled_tasks ADD COLUMN lang TEXT",
            "ALTER TABLE threshold_alerts ADD COLUMN user_id TEXT",
            "ALTER TABLE threshold_alerts ADD COLUMN dominio TEXT",
            # dashboard_widgets pode não existir em bancos antigos
            """CREATE TABLE IF NOT EXISTS dashboard_widgets (
                id          TEXT    PRIMARY KEY,
                title       TEXT    NOT NULL,
                description TEXT,
                code        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                user_id     TEXT,
                dominio     TEXT,
                range_unit  TEXT    NOT NULL DEFAULT 'days',
                range_value INTEGER NOT NULL DEFAULT 7
            )""",
            "ALTER TABLE dashboard_widgets ADD COLUMN dominio TEXT",
            "ALTER TABLE dashboard_widgets ADD COLUMN range_unit TEXT NOT NULL DEFAULT 'days'",
            "ALTER TABLE dashboard_widgets ADD COLUMN range_value INTEGER NOT NULL DEFAULT 7",
            # compartilhamento: 1 = visível para todos os usuários do domínio, não só o dono
            "ALTER TABLE dashboard_widgets ADD COLUMN shared INTEGER NOT NULL DEFAULT 0",
            # auditoria de erros durante geração/correção de task_code
            """CREATE TABLE IF NOT EXISTS task_code_audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT    NOT NULL,
                attempt     INTEGER NOT NULL,
                phase       TEXT    NOT NULL,
                error       TEXT    NOT NULL,
                code        TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                dominio     TEXT
            )""",
            "ALTER TABLE task_code_audit ADD COLUMN dominio TEXT",
            "CREATE INDEX IF NOT EXISTS idx_tca_task_id ON task_code_audit(task_id)",
            # marca cada sessão de chat com o domínio ativo no primeiro turno
            """CREATE TABLE IF NOT EXISTS session_dominio (
                session_id TEXT PRIMARY KEY,
                dominio    TEXT,
                created_at TEXT NOT NULL
            )""",
            # ordem de exibição dos painéis do dashboard, por usuário — inclui tanto
            # painéis padrão (config/dashboard_default_*.py, id prefixado "default:")
            # quanto customizados (dashboard_widgets); reconciliada dinamicamente contra
            # a lista atual de widgets a cada leitura (ver get_dashboard_widgets em main.py),
            # então sobrevive a inserir/remover gráficos do arquivo default sem quebrar.
            """CREATE TABLE IF NOT EXISTS dashboard_widget_order (
                user_id      TEXT NOT NULL,
                dominio      TEXT NOT NULL,
                widget_order TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (user_id, dominio)
            )""",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass  # coluna/tabela já existe
        conn.commit()


def save_dashboard_widget_order(user_id: str, dominio: str, widget_ids: list) -> None:
    """Upsert bruto de dashboard_widget_order — substitui a ordem inteira salva."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO dashboard_widget_order (user_id, dominio, widget_order, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, dominio) DO UPDATE SET widget_order = excluded.widget_order, updated_at = excluded.updated_at",
            (user_id, dominio, json.dumps(widget_ids), now),
        )
        conn.commit()


def prepend_dashboard_widget_order(user_id: "str | None", widget_id: str) -> None:
    """Coloca `widget_id` na primeira posição da ordem salva do dashboard do usuário.

    Chamada quando um painel customizado novo é fixado via chat (add_chart_to_dashboard)
    — o pedido é que ele sempre entre empurrando os demais pra frente, em vez de cair
    no final (que é o que a reconciliação normal faria pra qualquer id "não visto").
    Preserva o resto da ordem que o usuário já tinha organizado; se não havia ordem
    salva ainda, cria uma só com este id — o resto (defaults + outros customs) é
    completado pela reconciliação em GET /dashboard-widgets (main.py), que já sabe
    anexar no final qualquer id que não esteja na lista salva.

    Sem user_id (não deveria acontecer — add_chart_to_dashboard exige um usuário
    resolvido do contexto — mas defensivo) não há o que persistir, não faz nada.
    """
    if not user_id:
        return
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            "SELECT widget_order FROM dashboard_widget_order WHERE user_id = ? AND dominio = ?",
            (user_id, dominio),
        ).fetchone()
        try:
            ids = json.loads(row["widget_order"]) if row else []
        except (TypeError, ValueError):
            ids = []
    ids = [i for i in ids if i != widget_id]
    ids.insert(0, widget_id)
    save_dashboard_widget_order(user_id, dominio, ids)


def tag_session_dominio(session_id: str, dominio: str) -> None:
    """Marca uma sessão de chat com o domínio ativo no momento em que ela começou.

    Idempotente — só grava na primeira chamada por session_id (INSERT OR IGNORE),
    então o domínio de uma sessão não muda mesmo que settings.yml seja trocado
    no meio da conversa.
    """
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_dominio (session_id, dominio, created_at) VALUES (?, ?, ?)",
            (session_id, dominio, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()


def log_task_code_error(task_id: str, attempt: int, phase: str, error: str, code: str | None = None) -> None:
    """Registra um erro de geração/correção de task_code para auditoria."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO task_code_audit (task_id, attempt, phase, error, code, dominio) VALUES (?,?,?,?,?,?)",
            (task_id, attempt, phase, error, code, db_config.dominio_filename()),
        )
        conn.commit()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            -- Tarefas agendadas
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                description  TEXT NOT NULL,
                instructions TEXT,
                task_code    TEXT,
                frequency    TEXT NOT NULL,
                time         TEXT,
                weekday      TEXT,
                day          TEXT,
                email        TEXT,
                status       TEXT NOT NULL DEFAULT 'pending_approval',
                next_run     TEXT,
                last_run     TEXT,
                created_at   TEXT NOT NULL,
                retry_count  INTEGER NOT NULL DEFAULT 0,
                max_retries  INTEGER NOT NULL DEFAULT 3,
                user_id      TEXT,
                dominio      TEXT
            );

            -- Histórico de versões de código das tarefas
            CREATE TABLE IF NOT EXISTS task_code_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT    NOT NULL,
                version     INTEGER NOT NULL,
                code        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tcv_task_id ON task_code_versions(task_id);

            -- Histórico de execuções de cada tarefa
            CREATE TABLE IF NOT EXISTS task_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT    NOT NULL,
                started_at  TEXT    NOT NULL,
                ended_at    TEXT,
                status      TEXT    NOT NULL DEFAULT 'running',
                output      TEXT,
                error       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_task_runs_task_id ON task_runs(task_id);
            CREATE INDEX IF NOT EXISTS idx_task_runs_started ON task_runs(started_at);

            -- Sequência monotônica de IDs de tarefas — nunca regride, evita reutilização
            CREATE TABLE IF NOT EXISTS task_id_sequence (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                next_id INTEGER NOT NULL DEFAULT 1
            );
            INSERT OR IGNORE INTO task_id_sequence (id, next_id) VALUES (1, 1);

            -- Painéis de gráfico customizados criados sob demanda pelo agente
            -- range_unit/range_value: período pretendido do painel (ex: 30 dias, 6 meses),
            -- usado para calcular from_date/to_date corretos a cada refresh (não hardcoded no code).
            CREATE TABLE IF NOT EXISTS dashboard_widgets (
                id          TEXT    PRIMARY KEY,
                title       TEXT    NOT NULL,
                description TEXT,
                code        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                user_id     TEXT,
                dominio     TEXT,
                range_unit  TEXT    NOT NULL DEFAULT 'days',
                range_value INTEGER NOT NULL DEFAULT 7
            );

            -- Marca cada sessão de chat com o domínio ativo no primeiro turno
            CREATE TABLE IF NOT EXISTS session_dominio (
                session_id TEXT PRIMARY KEY,
                dominio    TEXT,
                created_at TEXT NOT NULL
            );
        """)
        # Sincroniza a sequência com o MAX real do banco (idempotente)
        conn.execute("""
            UPDATE task_id_sequence
            SET next_id = MAX(next_id, (
                SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) + 1
                FROM scheduled_tasks
            ))
            WHERE id = 1
        """)
        conn.commit()
