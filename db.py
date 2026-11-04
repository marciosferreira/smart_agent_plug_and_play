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
            # monitor de condição composta (AND/OR/várias métricas): sem condition_sql — o próprio
            # task_code avalia a condição e sinaliza via ctx.alert(). Este texto legível é o
            # marcador desse modo e o que o card da tarefa exibe.
            "ALTER TABLE scheduled_tasks ADD COLUMN condition_description TEXT",
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
            # avaliações do usuário (botões 👍 e 👎 do chat). Guarda o conjunto pergunta, resposta,
            # nota e a mensagem que ele escreve em seguida, para melhorar o agente depois.
            # mensagem_seguinte é a PRÓXIMA mensagem do usuário depois de um 👎, literalmente: pode
            # contestar a resposta ou ser outra pergunta sem relação. turn = índice da mensagem do
            # usuário na sessão (de 0).
            """CREATE TABLE IF NOT EXISTS feedback_respostas (
                session_id          TEXT    NOT NULL,
                turn                INTEGER NOT NULL,
                user_id             TEXT,
                dominio             TEXT,
                rating              TEXT    NOT NULL DEFAULT 'up',
                pergunta            TEXT,
                resposta            TEXT,
                analista            INTEGER NOT NULL DEFAULT 0,
                abordagem           TEXT,
                mensagem_seguinte   TEXT,
                mensagem_seguinte_em TEXT,
                created_at          TEXT    NOT NULL,
                updated_at          TEXT,
                PRIMARY KEY (session_id, turn)
            )""",
            "ALTER TABLE feedback_respostas RENAME COLUMN comentario TO mensagem_seguinte",
            "ALTER TABLE feedback_respostas RENAME COLUMN comentario_em TO mensagem_seguinte_em",
            "ALTER TABLE feedback_respostas ADD COLUMN dominio TEXT",
            "ALTER TABLE feedback_respostas ADD COLUMN rating TEXT NOT NULL DEFAULT 'up'",
            "ALTER TABLE feedback_respostas ADD COLUMN pergunta TEXT",
            "ALTER TABLE feedback_respostas ADD COLUMN resposta TEXT",
            "ALTER TABLE feedback_respostas ADD COLUMN analista INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE feedback_respostas ADD COLUMN mensagem_seguinte TEXT",
            "ALTER TABLE feedback_respostas ADD COLUMN mensagem_seguinte_em TEXT",
            "ALTER TABLE feedback_respostas ADD COLUMN updated_at TEXT",
            "ALTER TABLE feedback_respostas ADD COLUMN abordagem TEXT",
            # resumo da abordagem de cada resposta de dados (consultas feitas e se deram certo, sem
            # os valores devolvidos). chave = começo normalizado da resposta (feedback.inicio_comparavel).
            # Serve ao aprendizado por avaliação e ao conjunto de dados de treino.
            """CREATE TABLE IF NOT EXISTS abordagens (
                session_id TEXT NOT NULL,
                chave      TEXT NOT NULL,
                texto      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, chave)
            )""",
            # fichas de análise: a memória de episódios (fichas.py). Uma por episódio de análise
            # de dados, escrita pelo extrator da memória; vetor = embedding do título e da intenção.
            """CREATE TABLE IF NOT EXISTS fichas (
                id            TEXT PRIMARY KEY,
                dominio       TEXT NOT NULL,
                user_id       TEXT,
                session_id    TEXT NOT NULL,
                titulo        TEXT NOT NULL,
                intencao      TEXT,
                funcionou     TEXT,
                falhou        TEXT,
                avaliacao     TEXT NOT NULL DEFAULT 'sem_avaliacao',
                vetor         BLOB,
                modelo        TEXT,
                criada_em     TEXT NOT NULL,
                atualizada_em TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_fichas_dominio ON fichas(dominio)",
            "CREATE INDEX IF NOT EXISTS idx_fichas_sessao ON fichas(session_id)",
            # a assinatura do cache da busca lê MAX(atualizada_em): sem este índice a consulta
            # percorre o vetor de cada ficha (medido: 68 ms com 20 mil fichas, 3 ms com o índice)
            "CREATE INDEX IF NOT EXISTS idx_fichas_dominio_atual ON fichas(dominio, atualizada_em)",
            # o último prompt enviado ao LLM de cada usuário (orquestrador e analista), para o painel
            # /memoria mostrar o que a memória de fato injetou. Só o último de cada tipo é mantido.
            """CREATE TABLE IF NOT EXISTS ultimos_prompts (
                user_id    TEXT NOT NULL,
                tipo       TEXT NOT NULL,
                session_id TEXT,
                dominio    TEXT,
                prompt     TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, tipo)
            )""",
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


def guardar_abordagem(session_id: str, chave: str, texto: str) -> None:
    """Guarda o resumo da abordagem de uma resposta de dados (ver memoria.renderizar_abordagem)."""
    if not chave or not (texto or "").strip():
        return
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO abordagens (session_id, chave, texto, created_at) VALUES (?,?,?,?)",
            (session_id, chave, texto, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()


def guardar_prompt(user_id: str, tipo: str, session_id: str, dominio: str, prompt: str) -> None:
    """Guarda o último prompt do usuário para o tipo (`orquestrador` ou `analista`)."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ultimos_prompts (user_id, tipo, session_id, dominio, prompt, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, tipo, session_id, dominio, prompt, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()


def buscar_abordagem(session_id: str, chave: str) -> str | None:
    if not chave:
        return None
    with get_db() as conn:
        r = conn.execute(
            "SELECT texto FROM abordagens WHERE session_id = ? AND chave = ?", (session_id, chave)
        ).fetchone()
    return r["texto"] if r else None


_CAMPOS_FICHA = ("id", "dominio", "user_id", "session_id", "titulo", "intencao", "funcionou", "falhou",
                 "avaliacao", "vetor", "modelo", "criada_em", "atualizada_em")


def gravar_ficha(f: dict) -> None:
    """Cria ou substitui uma ficha de análise (ver fichas.py)."""
    with get_db() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO fichas ({', '.join(_CAMPOS_FICHA)}) "
            f"VALUES ({', '.join('?' for _ in _CAMPOS_FICHA)})",
            tuple(f.get(c) for c in _CAMPOS_FICHA),
        )
        conn.commit()


def fichas_da_sessao(session_id: str, dominio: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM fichas WHERE session_id = ? AND dominio = ? ORDER BY criada_em", (session_id, dominio)
        ).fetchall()
    return [dict(r) for r in rows]


def fichas_do_dominio(dominio: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM fichas WHERE dominio = ?", (dominio,)).fetchall()
    return [dict(r) for r in rows]


def fichas_assinatura(dominio: str) -> tuple:
    """Muda sempre que uma ficha do domínio é criada, atualizada ou apagada: o cache da busca
    (fichas.py) compara isto, em vez de ler as fichas, para saber se ainda vale. O
    INSERT OR REPLACE de gravar_ficha dá à ficha atualizada um rowid novo, então o maior rowid
    acompanha cada atualização; o total e a maior data cobrem as remoções."""
    with get_db() as conn:
        r = conn.execute(
            "SELECT COUNT(*), MAX(rowid), MAX(atualizada_em) FROM fichas WHERE dominio = ?", (dominio,)
        ).fetchone()
    return tuple(r)


def fichas_vetores(dominio: str) -> list[dict]:
    """Só o que a busca por similaridade precisa de cada ficha com vetor: nada do texto."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, avaliacao, atualizada_em, vetor FROM fichas "
            "WHERE dominio = ? AND vetor IS NOT NULL", (dominio,)
        ).fetchall()
    return [dict(r) for r in rows]


def fichas_por_id(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM fichas WHERE id IN ({', '.join('?' for _ in ids)})", list(ids)
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def ficha_por_prefixo(user_id: str, dominio: str, prefixo: str) -> list[dict]:
    """Fichas deste usuário e deste domínio cujo id começa com `prefixo` (a ref que o
    orquestrador usa para reler uma conversa)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM fichas WHERE dominio = ? AND user_id = ? AND substr(id, 1, ?) = ?",
            (dominio, user_id, len(prefixo), prefixo),
        ).fetchall()
    return [dict(r) for r in rows]


def registrar_avaliacao(session_id: str, turn: int, user_id: str, dominio: str, rating: str,
                        pergunta: str, resposta: str, analista: bool, abordagem: str | None = None) -> str | None:
    """Grava a nota (`up` ou `down`) do usuário para a resposta do turno. Há uma nota por
    resposta: trocar de 👍 para 👎 atualiza a mesma linha (a mensagem seguinte, se houver, fica).
    Devolve a nota anterior, ou None se é a primeira."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        antes = conn.execute(
            "SELECT rating FROM feedback_respostas WHERE session_id = ? AND turn = ?", (session_id, turn)
        ).fetchone()
        if antes:
            conn.execute(
                "UPDATE feedback_respostas SET rating = ?, updated_at = ? WHERE session_id = ? AND turn = ?",
                (rating, agora, session_id, turn),
            )
        else:
            conn.execute(
                "INSERT INTO feedback_respostas (session_id, turn, user_id, dominio, rating, pergunta, resposta,"
                " analista, abordagem, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, turn, user_id, dominio, rating, pergunta, resposta, int(bool(analista)), abordagem, agora),
            )
        conn.commit()
    return antes["rating"] if antes else None


def anexar_mensagem_seguinte(session_id: str, texto: str) -> int:
    """Guarda a primeira mensagem do usuário depois de um 👎, na avaliação ainda sem mensagem
    seguinte. É literalmente a próxima mensagem: pode contestar a resposta ou ser outra
    pergunta, e quem usar o conjunto decide. Devolve quantas linhas atualizou."""
    if not (texto or "").strip():
        return 0
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE feedback_respostas SET mensagem_seguinte = ?, mensagem_seguinte_em = ? "
            "WHERE session_id = ? AND rating = 'down' AND mensagem_seguinte IS NULL",
            (texto.strip(), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id),
        )
        conn.commit()
        return cur.rowcount


def avaliacoes_da_sessao(session_id: str) -> dict[int, str]:
    """{turno: nota} das respostas já avaliadas na sessão."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT turn, rating FROM feedback_respostas WHERE session_id = ?", (session_id,)
        ).fetchall()
    return {r["turn"]: r["rating"] for r in rows}


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
