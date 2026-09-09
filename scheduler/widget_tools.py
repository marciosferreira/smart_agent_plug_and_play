"""
LangChain tools para gerenciar painéis de gráficos customizados do dashboard.

REGRA CRÍTICA: add_chart_to_dashboard deve ser chamada SOMENTE quando o usuário
pedir EXPLICITAMENTE para ADICIONAR/INSERIR/FIXAR um gráfico no dashboard.
NÃO usar para gerar gráficos normais no chat.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from langchain_core.tools import tool

import db_config
from db import get_db, prepend_dashboard_widget_order
from .widget_runner import run_widget_code, default_test_range, compute_widget_range, RANGE_UNITS, WidgetCodeError

logger = logging.getLogger(__name__)

_DOMINIO_CLAUSE = "(dominio = ? OR dominio IS NULL)"
# painel visível: dono, legado sem dono, ou compartilhado com todos
_VISIBLE_CLAUSE = "(user_id = ? OR user_id IS NULL OR shared = 1)"
# só dono (ou legado sem dono) pode editar/apagar — "shared" só dá visibilidade, não permissão de escrita
_OWNER_CLAUSE = "(user_id = ? OR user_id IS NULL)"


def _current_user_id() -> str | None:
    """Identidade do usuário atual, repassada explicitamente pelo app host.

    Lida do contextvar preenchido em invoke_multi_agent/stream_multi_agent a partir
    do user_id recebido na requisição — não é mais inferida do formato do session_id.
    """
    try:
        from agent_multi import _current_user_id as _uid_ctx
        uid = _uid_ctx.get("")
        return uid or None
    except Exception:
        return None


def _all_widgets(user_id: str | None = None) -> list[dict]:
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT id, title, description, created_at, shared, user_id FROM dashboard_widgets "
                f"WHERE {_VISIBLE_CLAUSE} AND {_DOMINIO_CLAUSE} ORDER BY created_at DESC",
                (user_id, dominio),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id, title, description, created_at, shared, user_id FROM dashboard_widgets "
                f"WHERE {_DOMINIO_CLAUSE} ORDER BY created_at DESC",
                (dominio,),
            ).fetchall()
    return [dict(r) for r in rows]


def _format_widgets(widgets: list[dict]) -> str:
    if not widgets:
        return "Nenhum painel customizado no dashboard."
    lines = [f"**{len(widgets)} painel(is) customizado(s) no dashboard:**\n"]
    for w in widgets:
        tag = " *(compartilhado)*" if w.get("shared") else ""
        lines.append(f"  **[{w['id'][:8]}...]**{tag} {w['title']}")
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
def add_chart_to_dashboard(
    title: str,
    description: str,
    code: str,
    range_unit: str = "days",
    range_value: int = 7,
) -> str:
    """Adiciona um painel de gráfico customizado e persistente ao dashboard do usuário.

    ⚠️  Use SOMENTE quando o usuário pedir EXPLICITAMENTE para ADICIONAR, INSERIR
    ou FIXAR um gráfico no dashboard. NÃO use para gerar gráficos normais no chat.

    Palavras que indicam intenção de inserir no dashboard:
    'adiciona ao dashboard', 'insere no painel', 'quero ver no dashboard',
    'cria um painel com', 'fixar no dashboard', 'coloca no dashboard'.

    O código deve definir `def run(from_date, to_date, ctx)` retornando um dict
    Chart.js {"type": ..., "data": {...}, "options": {...}}.
    Use test_widget_code para validar ANTES de chamar esta tool.

    ⚠️  SOBRE DATAS: declare o período pretendido do painel via range_unit/range_value
    (ex: usuário pediu "últimos 30 dias" → range_unit="days", range_value=30; "últimos
    3 meses" → range_unit="months", range_value=3). O dashboard calcula from_date/to_date
    corretamente a partir desses valores a CADA refresh e os passa para run(from_date,
    to_date, ctx) — o código NÃO deve recalcular ou hardcodar o período sozinho, apenas
    usar os from_date/to_date recebidos na query.

    Args:
        title: Título exibido no painel (ex: "Defeitos por Categoria — Últimos 7 dias").
        description: Descrição curta do que o painel mostra.
        code: Código Python validado com `def run(from_date, to_date, ctx)`, que usa
            os from_date/to_date RECEBIDOS (não recalcula o período internamente).
        range_unit: Unidade do período do painel: "days" ou "months" (padrão: "days").
        range_value: Quantidade da unidade acima (padrão: 7, ou seja, últimos 7 dias).
    """
    if range_unit not in RANGE_UNITS:
        return f"❌ range_unit inválido: '{range_unit}'. Use um de: {sorted(RANGE_UNITS)}"
    if range_value <= 0:
        return "❌ range_value deve ser um inteiro positivo."

    from_date, to_date = compute_widget_range(range_unit, range_value)
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
            "INSERT INTO dashboard_widgets "
            "(id, title, description, code, created_at, user_id, dominio, range_unit, range_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (widget_id, title, description, code, now, user_id, db_config.dominio_filename(),
             range_unit, range_value),
        )
        conn.commit()

    # Painel novo sempre entra na primeira posição do dashboard do usuário,
    # empurrando os demais — preserva o resto da ordem que ele já tinha.
    prepend_dashboard_widget_order(user_id, widget_id)

    # Notifica o frontend via SSE para atualizar a seção de painéis automaticamente
    _notify({"type": "widget_added", "widget_id": widget_id})

    widgets = _all_widgets(user_id)
    return (
        f"✅ **Painel '{title}' adicionado ao dashboard.**\n"
        f"ID: `{widget_id[:8]}...`\n\n"
        "O painel aparecerá em **primeiro** na seção de painéis do dashboard "
        "e será atualizado automaticamente a cada refresh.\n\n"
        + _format_widgets(widgets)
        + "\nPara remover: **remover painel [ID]**"
    )


@tool
def list_dashboard_widgets() -> str:
    """Lista todos os painéis customizados adicionados ao dashboard."""
    return _format_widgets(_all_widgets(_current_user_id()))


def _resolve_widget(conn, widget_id: str, user_id: str | None):
    """Retorna a row do widget (visível ao usuário — dono, legado ou compartilhado),
    aceitando ID completo ou prefixo de 8 chars. `user_id=None` não filtra por
    visibilidade (uso interno, ex: delete_dashboard_widget já checa dono à parte)."""
    dominio = db_config.dominio_filename()
    cols = "id, title, description, code, range_unit, range_value, user_id, shared"
    visible = f" AND {_VISIBLE_CLAUSE}" if user_id else ""
    params_tail = (user_id,) if user_id else ()
    if len(widget_id) <= 8:
        return conn.execute(
            f"SELECT {cols} FROM dashboard_widgets WHERE id LIKE ? AND {_DOMINIO_CLAUSE}{visible}",
            (widget_id + "%", dominio) + params_tail,
        ).fetchone()
    return conn.execute(
        f"SELECT {cols} FROM dashboard_widgets WHERE id = ? AND {_DOMINIO_CLAUSE}{visible}",
        (widget_id, dominio) + params_tail,
    ).fetchone()


def _is_owner(row, user_id: str | None) -> bool:
    return row["user_id"] is None or row["user_id"] == user_id


def _notify(event: dict) -> None:
    """Notifica o frontend via SSE (widget_added/updated/deleted) pra atualizar
    a seção de painéis sem precisar de refresh manual.

    Best-effort: se a sessão de chat não tiver uma queue SSE ativa no momento
    (conexão caiu, sessão sem stream aberto), o evento é descartado silenciosamente
    — o widget já foi salvo no banco, só a atualização automática da tela que não
    ocorre (usuário precisa dar refresh). Logado como warning para diagnosticar
    se isso passar a acontecer com frequência incomum.
    """
    try:
        from agent_multi import _push_event, _current_session
        _push_event(_current_session.get(), event)
    except Exception:
        logger.warning("Falha ao notificar frontend via SSE: %s", event, exc_info=True)


@tool
def get_widget_code(widget_id: str) -> str:
    """Recupera o código Python de um painel customizado do dashboard.

    Use esta tool para ler o código de um painel antes de editá-lo com
    update_widget. Combine com list_dashboard_widgets para obter os IDs.

    Args:
        widget_id: ID completo ou primeiros 8 caracteres do ID do painel.
    """
    with get_db() as conn:
        row = _resolve_widget(conn, widget_id, _current_user_id())
    if not row:
        return f"Painel '{widget_id}' não encontrado."
    return (
        f"**Painel:** {row['title']}\n"
        f"**ID:** `{row['id']}`\n"
        f"**Período:** últimos {row['range_value']} {row['range_unit']}\n\n"
        f"```python\n{row['code']}\n```"
    )


@tool
def update_widget(
    widget_id: str,
    code: str,
    title: str = "",
    description: str = "",
    range_unit: str = "",
    range_value: int = 0,
) -> str:
    """Atualiza o código (e opcionalmente título/descrição/período) de um painel existente.

    Fluxo correto para editar um painel:
    1. list_dashboard_widgets → obter o ID
    2. get_widget_code → ler o código atual (mostra o período configurado)
    3. Modificar o código
    4. test_widget_code → validar
    5. update_widget → salvar e atualizar no dashboard

    ⚠️  SOBRE DATAS: mesma regra do add_chart_to_dashboard — o código deve usar os
    from_date/to_date RECEBIDOS por run(), sem recalculá-los. Se o usuário pedir para
    mudar o período (ex: "de 30 para 90 dias"), passe range_unit/range_value novos;
    caso contrário deixe-os vazios/zero para manter o período atual do painel.

    Args:
        widget_id: ID completo ou primeiros 8 caracteres do ID do painel.
        code: Novo código Python com `def run(from_date, to_date, ctx)`.
        title: Novo título (deixe vazio para manter o atual).
        description: Nova descrição (deixe vazio para manter a atual).
        range_unit: Nova unidade de período ("days"/"months"); vazio mantém a atual.
        range_value: Novo valor do período; 0 mantém o atual.
    """
    if range_unit and range_unit not in RANGE_UNITS:
        return f"❌ range_unit inválido: '{range_unit}'. Use um de: {sorted(RANGE_UNITS)}"

    user_id = _current_user_id()
    with get_db() as conn:
        existing = _resolve_widget(conn, widget_id, user_id)
    if not existing:
        return f"Painel '{widget_id}' não encontrado."
    if not _is_owner(existing, user_id):
        return "❌ Só o dono do painel pode editá-lo — este foi compartilhado com você, mas não é seu."

    final_unit  = range_unit or existing["range_unit"]
    final_value = range_value or existing["range_value"]
    from_date, to_date = compute_widget_range(final_unit, final_value)
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
        row = _resolve_widget(conn, widget_id, user_id)
        if not row:
            return f"Painel '{widget_id}' não encontrado."
        if not _is_owner(row, user_id):
            return "❌ Só o dono do painel pode editá-lo — este foi compartilhado com você, mas não é seu."
        full_id = row["id"]
        new_title = title.strip() or row["title"]
        new_desc  = description.strip() or row["description"]
        conn.execute(
            "UPDATE dashboard_widgets SET code=?, title=?, description=?, range_unit=?, range_value=? WHERE id=?",
            (code, new_title, new_desc, final_unit, final_value, full_id),
        )
        conn.commit()

    _notify({"type": "widget_updated", "widget_id": full_id})

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
    with get_db() as conn:
        row = _resolve_widget(conn, widget_id, user_id)
        if not row:
            return f"Painel '{widget_id}' não encontrado."
        if not _is_owner(row, user_id):
            return "❌ Só o dono do painel pode removê-lo — este foi compartilhado com você, mas não é seu."
        full_id = row["id"]
        title = row["title"]
        conn.execute("DELETE FROM dashboard_widgets WHERE id = ?", (full_id,))
        conn.commit()

    _notify({"type": "widget_deleted", "widget_id": full_id})

    widgets = _all_widgets(user_id)
    return (
        f"✅ Painel **'{title}'** removido do dashboard.\n\n"
        + _format_widgets(widgets)
    )
