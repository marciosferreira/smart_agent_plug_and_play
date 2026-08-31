"""
LangChain tools para gerenciar painéis de gráficos customizados do dashboard.

REGRA CRÍTICA: add_chart_to_dashboard deve ser chamada SOMENTE quando o usuário
pedir EXPLICITAMENTE para ADICIONAR/INSERIR/FIXAR um gráfico no dashboard.
NÃO usar para gerar gráficos normais no chat.
"""

import uuid
from datetime import datetime
from typing import Optional

from langchain_core.tools import tool

import db_config
from db import get_db
from .widget_runner import run_widget_code, default_test_range, WidgetCodeError

_DOMINIO_CLAUSE = "(dominio = ? OR dominio IS NULL)"


def _current_user_id() -> str | None:
    try:
        from agent_multi import _current_session
        sid = _current_session.get("")
        return sid[:36] if sid else None
    except Exception:
        return None


def _all_widgets(user_id: str | None = None) -> list[dict]:
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT id, title, description, created_at FROM dashboard_widgets "
                f"WHERE (user_id=? OR user_id IS NULL) AND {_DOMINIO_CLAUSE} ORDER BY created_at DESC",
                (user_id, dominio),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id, title, description, created_at FROM dashboard_widgets "
                f"WHERE {_DOMINIO_CLAUSE} ORDER BY created_at DESC",
                (dominio,),
            ).fetchall()
    return [dict(r) for r in rows]


def _format_widgets(widgets: list[dict]) -> str:
    if not widgets:
        return "Nenhum painel customizado no dashboard."
    lines = [f"**{len(widgets)} painel(is) customizado(s) no dashboard:**\n"]
    for w in widgets:
        lines.append(f"  **[{w['id'][:8]}...]** {w['title']}")
        if w.get("description"):
            lines.append(f"    {w['description']}")
        lines.append(f"    Criado em: {w['created_at']}")
        lines.append("")
    return "\n".join(lines)


@tool
def test_widget_code(title: str, code: str, from_date: str = "", to_date: str = "") -> str:
    """Valida widget_code antes de salvar no dashboard.

    O código deve definir `def run(from_date, to_date, ctx)` e retornar um dict
    Chart.js com as chaves 'type' e 'data'. Tipos aceitos: bar, line, pie,
    doughnut, radar, polarArea, scatter, bubble.

    Use SEMPRE antes de add_chart_to_dashboard para garantir que o código funciona.

    Args:
        title: Título do painel (usado apenas para identificação no teste).
        code: Código Python completo com `def run(from_date, to_date, ctx)`.
        from_date: Data inicial YYYY-MM-DD (padrão: 7 dias atrás).
        to_date: Data final YYYY-MM-DD (padrão: hoje).
    """
    if not from_date or not to_date:
        from_date, to_date = default_test_range()

    session_id = f"widget_test_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        config = run_widget_code(code, from_date, to_date, session_id)
        chart_type = config.get("type", "?")
        labels = config.get("data", {}).get("labels", [])
        datasets = config.get("data", {}).get("datasets", [])
        return (
            f"✅ **Teste bem-sucedido!** Período: {from_date} → {to_date}\n"
            f"Tipo: {chart_type} | Labels: {len(labels)} | Datasets: {len(datasets)}\n"
            f"Exemplo de labels: {labels[:5]}"
        )
    except WidgetCodeError as e:
        return f"❌ **Erro no código:**\n```\n{e}\n```\nCorreja e teste novamente antes de salvar."
    except Exception as e:
        return f"❌ **Erro inesperado:**\n```\n{e}\n```"


@tool
def add_chart_to_dashboard(title: str, description: str, code: str) -> str:
    """Adiciona um painel de gráfico customizado e persistente ao dashboard do usuário.

    ⚠️  Use SOMENTE quando o usuário pedir EXPLICITAMENTE para ADICIONAR, INSERIR
    ou FIXAR um gráfico no dashboard. NÃO use para gerar gráficos normais no chat.

    Palavras que indicam intenção de inserir no dashboard:
    'adiciona ao dashboard', 'insere no painel', 'quero ver no dashboard',
    'cria um painel com', 'fixar no dashboard', 'coloca no dashboard'.

    O código deve definir `def run(from_date, to_date, ctx)` retornando um dict
    Chart.js {"type": ..., "data": {...}, "options": {...}}.
    Use test_widget_code para validar ANTES de chamar esta tool.

    ⚠️  IMPORTANTE sobre datas: a cada refresh do dashboard, `from_date`/`to_date`
    recebidos por run() são SEMPRE os últimos 7 dias (janela fixa do dashboard) —
    NÃO refletem o período que o usuário pediu ao criar o painel.
    Se o usuário pediu um período diferente (ex: "últimos 3 meses", "últimos 30 dias",
    "este mês"), o código DEVE calcular seu próprio range internamente a partir de
    `date.today()` (ex: `pd.Timestamp(date.today()) - pd.DateOffset(months=3)`) e
    usar esse range na query — ignorando os `from_date`/`to_date` recebidos.
    Use `from_date`/`to_date` recebidos apenas quando o painel for explicitamente
    "rolling últimos 7 dias" (o padrão do dashboard).

    Args:
        title: Título exibido no painel (ex: "Defeitos por Categoria — Últimos 7 dias").
        description: Descrição curta do que o painel mostra.
        code: Código Python validado com `def run(from_date, to_date, ctx)`.
    """
    from_date, to_date = default_test_range()
    session_id = f"widget_validate_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Valida antes de salvar
    try:
        run_widget_code(code, from_date, to_date, session_id)
    except WidgetCodeError as e:
        return (
            f"❌ **Código inválido — painel NÃO foi salvo.**\n```\n{e}\n```\n"
            "Corrija e use test_widget_code antes de tentar novamente."
        )
    except Exception as e:
        return f"❌ **Erro inesperado na validação:**\n```\n{e}\n```"

    widget_id = str(uuid.uuid4())
    user_id = _current_user_id()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute(
            "INSERT INTO dashboard_widgets (id, title, description, code, created_at, user_id, dominio) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (widget_id, title, description, code, now, user_id, db_config.dominio_filename()),
        )
        conn.commit()

    # Notifica o frontend via SSE para atualizar a seção de painéis automaticamente
    try:
        from agent_multi import _push_event, _current_session
        _push_event(_current_session.get(), {"type": "widget_added", "widget_id": widget_id})
    except Exception:
        pass

    widgets = _all_widgets(user_id)
    return (
        f"✅ **Painel '{title}' adicionado ao dashboard.**\n"
        f"ID: `{widget_id[:8]}...`\n\n"
        "O painel aparecerá na seção **Painéis Customizados** ao final do dashboard "
        "e será atualizado automaticamente a cada refresh.\n\n"
        + _format_widgets(widgets)
        + "\nPara remover: **remover painel [ID]**"
    )


@tool
def list_dashboard_widgets() -> str:
    """Lista todos os painéis customizados adicionados ao dashboard."""
    return _format_widgets(_all_widgets(_current_user_id()))


def _resolve_widget(conn, widget_id: str):
    """Retorna a row do widget aceitando ID completo ou prefixo de 8 chars."""
    dominio = db_config.dominio_filename()
    if len(widget_id) <= 8:
        return conn.execute(
            f"SELECT id, title, description, code FROM dashboard_widgets WHERE id LIKE ? AND {_DOMINIO_CLAUSE}",
            (widget_id + "%", dominio),
        ).fetchone()
    return conn.execute(
        f"SELECT id, title, description, code FROM dashboard_widgets WHERE id = ? AND {_DOMINIO_CLAUSE}",
        (widget_id, dominio),
    ).fetchone()


@tool
def get_widget_code(widget_id: str) -> str:
    """Recupera o código Python de um painel customizado do dashboard.

    Use esta tool para ler o código de um painel antes de editá-lo com
    update_widget. Combine com list_dashboard_widgets para obter os IDs.

    Args:
        widget_id: ID completo ou primeiros 8 caracteres do ID do painel.
    """
    with get_db() as conn:
        row = _resolve_widget(conn, widget_id)
    if not row:
        return f"Painel '{widget_id}' não encontrado."
    return (
        f"**Painel:** {row['title']}\n"
        f"**ID:** `{row['id']}`\n\n"
        f"```python\n{row['code']}\n```"
    )


@tool
def update_widget(widget_id: str, code: str, title: str = "", description: str = "") -> str:
    """Atualiza o código (e opcionalmente título/descrição) de um painel existente.

    Fluxo correto para editar um painel:
    1. list_dashboard_widgets → obter o ID
    2. get_widget_code → ler o código atual
    3. Modificar o código
    4. test_widget_code → validar
    5. update_widget → salvar e atualizar no dashboard

    ⚠️  Mesma regra de datas do add_chart_to_dashboard: `from_date`/`to_date`
    recebidos por run() são sempre os últimos 7 dias. Para períodos fixos
    diferentes (ex: "últimos 3 meses"), calcule o range internamente a partir
    de `date.today()`.

    Args:
        widget_id: ID completo ou primeiros 8 caracteres do ID do painel.
        code: Novo código Python com `def run(from_date, to_date, ctx)`.
        title: Novo título (deixe vazio para manter o atual).
        description: Nova descrição (deixe vazio para manter a atual).
    """
    from_date, to_date = default_test_range()
    session_id = f"widget_validate_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    try:
        run_widget_code(code, from_date, to_date, session_id)
    except WidgetCodeError as e:
        return (
            f"❌ **Código inválido — painel NÃO foi atualizado.**\n```\n{e}\n```\n"
            "Corrija e use test_widget_code antes de tentar novamente."
        )
    except Exception as e:
        return f"❌ **Erro inesperado na validação:**\n```\n{e}\n```"

    with get_db() as conn:
        row = _resolve_widget(conn, widget_id)
        if not row:
            return f"Painel '{widget_id}' não encontrado."
        full_id = row["id"]
        new_title = title.strip() or row["title"]
        new_desc  = description.strip() or row["description"]
        conn.execute(
            "UPDATE dashboard_widgets SET code=?, title=?, description=? WHERE id=?",
            (code, new_title, new_desc, full_id),
        )
        conn.commit()

    try:
        from agent_multi import _push_event, _current_session
        _push_event(_current_session.get(), {"type": "widget_updated", "widget_id": full_id})
    except Exception:
        pass

    return (
        f"✅ **Painel '{new_title}' atualizado com sucesso.**\n"
        f"ID: `{full_id[:8]}...`\n\n"
        "O dashboard será atualizado automaticamente."
    )


@tool
def delete_dashboard_widget(widget_id: str) -> str:
    """Remove um painel customizado do dashboard pelo ID (ou prefixo do ID).

    Args:
        widget_id: ID completo ou primeiros 8 caracteres do ID do painel.
                   Use list_dashboard_widgets para ver os IDs.
    """
    user_id = _current_user_id()
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        # Aceita ID completo ou prefixo de 8 chars
        if len(widget_id) == 8:
            row = conn.execute(
                f"SELECT id, title FROM dashboard_widgets WHERE id LIKE ? AND {_DOMINIO_CLAUSE}",
                (widget_id + "%", dominio),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT id, title FROM dashboard_widgets WHERE id = ? AND {_DOMINIO_CLAUSE}",
                (widget_id, dominio),
            ).fetchone()
        if not row:
            return f"Painel '{widget_id}' não encontrado."
        full_id = row["id"]
        title = row["title"]
        conn.execute("DELETE FROM dashboard_widgets WHERE id = ?", (full_id,))
        conn.commit()

    try:
        from agent_multi import _push_event, _current_session
        _push_event(_current_session.get(), {"type": "widget_deleted", "widget_id": full_id})
    except Exception:
        pass

    widgets = _all_widgets(user_id)
    return (
        f"✅ Painel **'{title}'** removido do dashboard.\n\n"
        + _format_widgets(widgets)
    )
