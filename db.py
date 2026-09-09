import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import db_config

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "mfg.db")))

DAY_NAMES = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

# Modelo produzido por linha (fixo por linha)
LINE_MODEL = {
    1: "PhoneX Pro",
    2: "PhoneX Lite",
    3: "PhoneX Ultra",
    4: "PhoneX Mini",
}

# Base diária por linha de produção (7 dias ciclando)
BASE_LINE = {
    1: [1050, 1180, 1120, 1250, 1124, 1090,  980],
    2: [ 920,  970,  890, 1020,  987,  940,  880],
    3: [ 880,  950,  820,  960,  743,  720,  810],
    4: [ 870, 1000, 1120, 1080,  993,  940,  920],
}

# Fração da produção diária por turno
SHIFT_FRACTIONS = {"A": 0.385, "B": 0.338, "C": 0.277}

# Target diário por linha calibrado para eficiência ~90% na média
# = round(media_diaria_base / 0.90)
LINE_DAILY_TARGET = {
    1: round(sum([1050, 1180, 1120, 1250, 1124, 1090,  980]) / 7 / 0.90),  # ≈ 1237
    2: round(sum([ 920,  970,  890, 1020,  987,  940,  880]) / 7 / 0.90),  # ≈ 1049
    3: round(sum([ 880,  950,  820,  960,  743,  720,  810]) / 7 / 0.90),  # ≈  934
    4: round(sum([ 870, 1000, 1120, 1080,  993,  940,  920]) / 7 / 0.90),  # ≈ 1099
}

# Base de defeitos por turno e categoria (valor diário total)
BASE_DEFECTS = {
    "A": [("Tela (display)", 34), ("Câmera", 22), ("Bateria", 19),
          ("Placa-mãe", 16), ("Chassi / Carcaça", 14), ("Conector USB", 9), ("Outros", 4)],
    "B": [("Tela (display)", 39), ("Câmera", 25), ("Bateria", 22),
          ("Placa-mãe", 18), ("Chassi / Carcaça", 16), ("Conector USB", 10), ("Outros", 5)],
    "C": [("Tela (display)", 47), ("Câmera", 31), ("Bateria", 26),
          ("Placa-mãe", 22), ("Chassi / Carcaça", 20), ("Conector USB", 13), ("Outros", 6)],
}

# Fração de defeitos por linha (proporcional à produção)
LINE_DEFECT_SHARE = {1: 0.29, 2: 0.26, 3: 0.20, 4: 0.25}

# Variação diária cíclica
VARIATION = [1.00, 1.10, 0.90, 1.15, 0.85, 1.05, 0.95, 1.20, 0.80, 1.08]

# Base de OEE/FPY/eficiência por turno (7 dias ciclando)
# fpy_a  oee_a  fpy_b  oee_b  fpy_c  oee_c  avail  perf  sa  sb  sc
BASE_METRICS = [
    (94.2, 81.4, 93.1, 71.6, 91.4, 58.6, 91.2, 87.6, 89, 85, 79),
    (95.1, 83.2, 93.8, 72.9, 92.1, 59.8, 92.8, 88.9, 91, 87, 81),
    (93.4, 80.0, 92.4, 70.4, 90.7, 57.5, 90.5, 86.2, 88, 84, 77),
    (95.8, 84.8, 94.5, 74.2, 92.8, 61.0, 93.1, 90.4, 92, 89, 83),
    (94.5, 81.9, 93.4, 72.1, 91.7, 59.2, 91.8, 87.6, 88, 84, 78),
    (96.2, 85.5, 95.0, 75.0, 93.3, 61.8, 94.0, 90.2, 93, 88, 82),
    (92.8, 79.4, 91.8, 69.8, 90.2, 57.0, 89.6, 85.8, 86, 82, 76),
]


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _shift_date_col(conn, table: str, col: str, delta: int) -> None:
    """Shift a date column by delta days using a two-step update to avoid UNIQUE conflicts."""
    pad = 10000
    conn.execute(f"UPDATE {table} SET {col} = date({col}, '+{delta + pad} days')")
    conn.execute(f"UPDATE {table} SET {col} = date({col}, '-{pad} days')")


def shift_dates_to_today() -> int:
    """Shift each table individually so its max(date) == today. Returns max days shifted."""
    today = date.today()
    max_delta = 0
    with get_db() as conn:
        for table in ("production", "defects", "metrics", "hourly_production"):
            row = conn.execute(f"SELECT MAX(date) as d FROM {table}").fetchone()
            if not row or not row["d"]:
                continue
            delta = (today - date.fromisoformat(row["d"])).days
            if delta > 0:
                _shift_date_col(conn, table, "date", delta)
                max_delta = max(max_delta, delta)
        row = conn.execute("SELECT MAX(datetime) as d FROM alerts").fetchone()
        if row and row["d"]:
            delta = (today - date.fromisoformat(row["d"][:10])).days
            if delta > 0:
                pad = 10000
                conn.execute(f"UPDATE alerts SET datetime = datetime(datetime, '+{delta + pad} days')")
                conn.execute(f"UPDATE alerts SET datetime = datetime(datetime, '-{pad} days')")
                max_delta = max(max_delta, delta)
        conn.commit()
    return max_delta


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
            -- Produção granular: uma linha por (data, turno, linha, modelo)
            CREATE TABLE IF NOT EXISTS production (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT    NOT NULL,
                shift    TEXT    NOT NULL,   -- A | B | C
                line     INTEGER NOT NULL,   -- 1 | 2 | 3 | 4
                model    TEXT    NOT NULL,   -- PhoneX Pro | PhoneX Lite | PhoneX Ultra | PhoneX Mini
                produced INTEGER NOT NULL,
                target   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_production_date  ON production(date);
            CREATE INDEX IF NOT EXISTS idx_production_shift ON production(shift);
            CREATE INDEX IF NOT EXISTS idx_production_line  ON production(line);
            CREATE INDEX IF NOT EXISTS idx_production_model ON production(model);

            -- Defeitos granulares: uma linha por (data, turno, linha, categoria)
            CREATE TABLE IF NOT EXISTS defects (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT    NOT NULL,
                shift    TEXT    NOT NULL,
                line     INTEGER NOT NULL,
                category TEXT    NOT NULL,
                count    INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_defects_date     ON defects(date);
            CREATE INDEX IF NOT EXISTS idx_defects_shift    ON defects(shift);
            CREATE INDEX IF NOT EXISTS idx_defects_line     ON defects(line);
            CREATE INDEX IF NOT EXISTS idx_defects_category ON defects(category);

            -- Métricas diárias por turno
            CREATE TABLE IF NOT EXISTS metrics (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                date               TEXT NOT NULL UNIQUE,
                label              TEXT,
                fpy_a              REAL,
                oee_a              REAL,
                fpy_b              REAL,
                oee_b              REAL,
                fpy_c              REAL,
                oee_c              REAL,
                availability       REAL,
                performance        REAL,
                shift_a_efficiency INTEGER,
                shift_b_efficiency INTEGER,
                shift_c_efficiency INTEGER
            );

            -- Status atual das linhas
            CREATE TABLE IF NOT EXISTS lines_status (
                id        INTEGER PRIMARY KEY,
                name      TEXT,
                model     TEXT,
                status    TEXT,
                produced  INTEGER,
                target    INTEGER,
                fpy       REAL,
                speed_pct INTEGER,
                operator  TEXT
            );

            -- Alertas
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                datetime     TEXT,
                severity     TEXT,
                line         INTEGER,
                message      TEXT,
                acknowledged INTEGER
            );

            -- KPIs por turno (snapshot atual)
            CREATE TABLE IF NOT EXISTS kpis (
                shift              TEXT PRIMARY KEY,
                total_produced     INTEGER,
                daily_target       INTEGER,
                first_pass_yield   REAL,
                defect_rate        REAL,
                downtime_minutes   INTEGER,
                efficiency         REAL,
                scrapped           INTEGER,
                reworked           INTEGER,
                cycle_time_seconds REAL,
                oee                REAL
            );

            -- Produção hora a hora por linha e modelo
            CREATE TABLE IF NOT EXISTS hourly_production (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT    NOT NULL,
                shift    TEXT    NOT NULL,
                line     INTEGER NOT NULL,
                model    TEXT    NOT NULL,
                hour     TEXT    NOT NULL,
                produced INTEGER NOT NULL,
                target   INTEGER NOT NULL,
                defects  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hourly_date  ON hourly_production(date);
            CREATE INDEX IF NOT EXISTS idx_hourly_shift ON hourly_production(shift);
            CREATE INDEX IF NOT EXISTS idx_hourly_model ON hourly_production(model);

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

            CREATE TABLE IF NOT EXISTS demo_baseline (
                date     TEXT    NOT NULL,
                prod_id  INTEGER NOT NULL,
                produced INTEGER NOT NULL,
                PRIMARY KEY (date, prod_id)
            );

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

            -- Histórico de modelos por linha (qual modelo cada linha estava produzindo)
            CREATE TABLE IF NOT EXISTS line_model_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                line       INTEGER NOT NULL,
                model      TEXT    NOT NULL,
                started_at TEXT    NOT NULL,
                ended_at   TEXT                -- NULL = vigente hoje
            );
            CREATE INDEX IF NOT EXISTS idx_lmh_line ON line_model_history(line);

            -- Marca cada sessão de chat com o domínio ativo no primeiro turno
            CREATE TABLE IF NOT EXISTS session_dominio (
                session_id TEXT PRIMARY KEY,
                dominio    TEXT,
                created_at TEXT NOT NULL
            );
        """)
        _seed(conn)
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


def _seed(conn: sqlite3.Connection):
    if conn.execute("SELECT COUNT(*) FROM production").fetchone()[0] > 0:
        return

    today = date.today()

    # ── production ────────────────────────────────────────────────────────────
    prod_rows = []
    for days_ago in range(89, -1, -1):
        d = today - timedelta(days=days_ago)
        v = VARIATION[days_ago % len(VARIATION)]
        for line, base_values in BASE_LINE.items():
            daily_line = round(base_values[days_ago % 7] * v)
            for shift, frac in SHIFT_FRACTIONS.items():
                line_target = round(LINE_DAILY_TARGET[line] * frac)
                prod_rows.append((d.isoformat(), shift, line, LINE_MODEL[line], round(daily_line * frac), line_target))
    conn.executemany(
        "INSERT INTO production (date,shift,line,model,produced,target) VALUES (?,?,?,?,?,?)",
        prod_rows,
    )

    # ── defects ───────────────────────────────────────────────────────────────
    defect_rows = []
    for days_ago in range(89, -1, -1):
        d = today - timedelta(days=days_ago)
        v = VARIATION[days_ago % len(VARIATION)]
        for shift, categories in BASE_DEFECTS.items():
            for category, base_count in categories:
                total = round(base_count * v)
                for line, share in LINE_DEFECT_SHARE.items():
                    count = max(0, round(total * share))
                    defect_rows.append((d.isoformat(), shift, line, category, count))
    conn.executemany(
        "INSERT INTO defects (date,shift,line,category,count) VALUES (?,?,?,?,?)",
        defect_rows,
    )

    # ── metrics ───────────────────────────────────────────────────────────────
    metrics_rows = []
    for days_ago in range(89, -1, -1):
        d = today - timedelta(days=days_ago)
        m = BASE_METRICS[days_ago % 7]
        metrics_rows.append((d.isoformat(), DAY_NAMES[d.weekday()], *m))
    conn.executemany(
        "INSERT INTO metrics (date,label,fpy_a,oee_a,fpy_b,oee_b,fpy_c,oee_c,availability,performance,shift_a_efficiency,shift_b_efficiency,shift_c_efficiency) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        metrics_rows,
    )

    # ── lines_status ──────────────────────────────────────────────────────────
    conn.executemany(
        "INSERT INTO lines_status VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "Linha 1", "PhoneX Pro",   "running",      1124, 1200, 95.8, 98, "Carlos Mendes"),
            (2, "Linha 2", "PhoneX Lite",  "running",       987, 1200, 93.1, 82, "Ana Souza"),
            (3, "Linha 3", "PhoneX Ultra", "stopped",       743, 1200, 91.4,  0, "Paulo Lima"),
            (4, "Linha 4", "PhoneX Mini",  "maintenance",   993, 1200, 96.2,  0, "Fernanda Costa"),
        ],
    )

    # ── alerts ────────────────────────────────────────────────────────────────
    today_str = today.isoformat()
    conn.executemany(
        "INSERT INTO alerts (datetime,severity,line,message,acknowledged) VALUES (?,?,?,?,?)",
        [
            (f"{today_str}T14:18:00", "critical", 3, "Parada não planejada — sensor de conveyor com falha", 0),
            (f"{today_str}T13:55:00", "warning",  2, "FPY abaixo de 90% nas últimas 30 unidades — verificar estação de câmera", 0),
            (f"{today_str}T13:40:00", "warning",  4, "Manutenção preventiva programada iniciada", 1),
            (f"{today_str}T12:30:00", "info",      1, "Meta horária atingida com 12 min de antecedência", 1),
            (f"{today_str}T11:22:00", "critical", 3, "Taxa de defeito em tela > 5% — lote BT-4821 em revisão", 1),
        ],
    )

    # ── line_model_history ────────────────────────────────────────────────────
    if conn.execute("SELECT COUNT(*) FROM line_model_history").fetchone()[0] == 0:
        first_day = (today - timedelta(days=89)).isoformat()
        conn.executemany(
            "INSERT INTO line_model_history (line, model, started_at, ended_at) VALUES (?,?,?,NULL)",
            [(line, model, first_day) for line, model in LINE_MODEL.items()],
        )

    # ── kpis ──────────────────────────────────────────────────────────────────
    conn.executemany(
        "INSERT INTO kpis VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("A", 3847, 4800, 94.2, 2.3,  38, 87.6,  88, 145, 42.3, 81.4),
            ("B", 3385, 4800, 93.1, 2.6,  52, 77.1, 101, 128, 42.3, 71.6),
            ("C", 2770, 4800, 91.4, 3.2,  71, 63.1, 127, 105, 42.3, 58.6),
        ],
    )

    # ── hourly_production ─────────────────────────────────────────────────────
    SHIFT_MULT = {"A": 1.0, "B": 0.88, "C": 0.72}
    HOURLY_HOURS = {
        "A": ["06h","07h","08h","09h","10h","11h","12h","13h"],
        "B": ["14h","15h","16h","17h","18h","19h","20h","21h"],
        "C": ["22h","23h","00h","01h","02h","03h","04h","05h"],
    }
    # perfil intradiário base: começo de turno mais lento, pico no meio, queda no final
    BASE_HOURLY_PROD = [312, 389, 401, 376, 420, 398, 290, 415]
    BASE_HOURLY_DEF  = [8,   6,   11,  9,   7,   12,  5,   8]
    # participação de cada linha na produção/defeitos horários (proporcional à capacidade)
    LINE_HOURLY_SHARE = {1: 0.29, 2: 0.26, 3: 0.20, 4: 0.25}

    hourly_rows = []
    for days_ago in range(89, -1, -1):
        d = today - timedelta(days=days_ago)
        day_v = VARIATION[days_ago % len(VARIATION)]
        for shift, hours in HOURLY_HOURS.items():
            m = SHIFT_MULT[shift]
            for i, h in enumerate(hours):
                hour_v = VARIATION[(days_ago + i * 3) % len(VARIATION)]
                v = day_v * 0.7 + hour_v * 0.3
                total_prod    = max(0, round(BASE_HOURLY_PROD[i] * m * v))
                total_defects = max(0, round(BASE_HOURLY_DEF[i] * (1 / m if m < 1 else 1) * v))
                for line, share in LINE_HOURLY_SHARE.items():
                    hourly_target = round(LINE_DAILY_TARGET[line] * SHIFT_FRACTIONS[shift] / 8)
                    hourly_rows.append((
                        d.isoformat(), shift, line, LINE_MODEL[line], h,
                        max(0, round(total_prod * share)),
                        hourly_target,
                        max(0, round(total_defects * share)),
                    ))
    conn.executemany(
        "INSERT INTO hourly_production (date,shift,line,model,hour,produced,target,defects) VALUES (?,?,?,?,?,?,?,?)",
        hourly_rows,
    )
