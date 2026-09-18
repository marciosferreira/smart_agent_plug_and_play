"""
Multi-Agente LangGraph — Orquestrador + Sub-agente Analista de Dados
========================================================================
O domínio analisado (banco, schema, regras de negócio) vem do pack de configuração
em config/ (ver db_config.py) — trocar o pack re-aponta o sistema para outro banco.

Arquitetura:

    Usuário
       ↓ query
    Orquestrador (StateGraph)
       ├─ get_current_datetime()     → responde data/hora direto
       └─ consultar_analista(detalhes) → delega ao sub-agente
              └─ Sub-agente analista (StateGraph)
                    ├─ read_skill(filename)          → lê instruções do arquivo .md
                    ├─ executar_sql/executar_graphql  → busca dados (uma das duas,
                    │                                   conforme database.modo em
                    │                                   config/settings.yml)
                    └─ analisar_dataframe(script)     → sandbox Python/Pandas

Fluxo do sub-agente por análise:
  1. read_skill('dominio.md') → descobre o schema do banco configurado
  2. executar_sql ou executar_graphql → busca dados e injeta DataFrame no ambiente
  3. analisar_dataframe(script) → processa com pandas, retorna resultado formatado
"""

import contextvars
import io
import logging
import os
import queue as _queue_module
import sqlite3
import sys
import textwrap
import threading
import time as _time
import traceback as tb
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests as http_requests
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import Annotated, TypedDict

import chart_store
from scheduler.tools import (
    schedule_task,
    schedule_monitor,
    get_task_code,
    test_task_code,
    save_task_code,
)
from scheduler.widget_tools import (
    add_chart_to_dashboard,
    test_widget_code,
    list_dashboard_widgets,
    delete_dashboard_widget,
    get_widget_code,
    update_widget,
)

logger = logging.getLogger(__name__)

from db import DB_PATH, tag_session_dominio
import db_config
import graphql_config

SKILLS_FOLDER = Path(__file__).parent / "skills"
SKILL_HEADER_LINES = 4

_orchestrator_graph = None
_sub_agent_graph = None
_scheduler_agent_graph = None
_scheduling_graph = None
_checkpointer = None

# Propaga o session_id através das fronteiras de thread do ToolNode.
# contextvars.ContextVar se propaga para threads criadas por ThreadPoolExecutor,
# que é o que o ToolNode do LangGraph usa internamente.
_current_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mfg_session", default="default"
)
# Identidade do usuário, repassada explicitamente pelo app host (não inferida
# do session_id). Vazio quando não há host com controle de usuário (modo standalone).
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mfg_user_id", default=""
)
_sandbox_mode: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sandbox_mode", default=False
)
_sandbox_task_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sandbox_task_id", default=""
)

# Snapshot do dashboard enviado pelo browser antes de cada mensagem.
# Chave: session_id, Valor: dict com charts e kpis.
_chart_snapshots: dict[str, dict] = {}
_cs_lock = threading.Lock()


def set_chart_snapshot(session_id: str, data: dict) -> None:
    """Chamado pelo endpoint POST /chart-snapshot para armazenar o estado atual do dashboard."""
    with _cs_lock:
        _chart_snapshots[session_id] = data


def _tlog(tool: str, event: str, **kwargs) -> None:
    """Loga chamadas e saídas de tools quando DEBUG_TOOLS=1."""
    if os.getenv("DEBUG_TOOLS", "0").strip() != "1":
        return
    lines = [f"\n{'='*60}", f"[TOOL] {tool} | {event}"]
    truncate = int(os.getenv("DEBUG_TOOLS_TRUNCATE", "0"))
    for k, v in kwargs.items():
        text = str(v)
        if truncate > 0 and len(text) > truncate:
            text = text[:truncate] + " …(truncado)"
        lines.append(f"  {k}: {text}")
    lines.append("="*60)
    logger.info("\n".join(lines))


# ── Namespace persistente por sessão (estilo Jupyter) ─────────────────────────
# Cada sessão tem um dict de execução Python que persiste entre tool calls e
# entre invocações do sub-agente. O LLM recebe um resumo das variáveis ao final
# de cada tool para saber o que já está disponível para análises incrementais.

import matplotlib
matplotlib.use("Agg")  # backend sem display — obrigatório em servidor
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

_BUILTINS_SKIP = {"pd", "np", "plt", "sns", "stats", "__builtins__", "__doc__", "__name__",
                  "__package__", "__spec__", "__loader__", "__import__", "__build_class__"}

_namespaces: dict = {}       # session → namespace dict
_ns_last_access: dict = {}   # session → datetime do último acesso
_ns_lock = threading.Lock()
_NS_TTL_SECONDS = 600        # 10 minutos de inatividade


def _ns_get() -> dict:
    session = _current_session.get()
    with _ns_lock:
        if session not in _namespaces:
            _namespaces[session] = {"pd": pd, "np": np, "plt": plt, "sns": sns, "stats": scipy_stats}
        _ns_last_access[session] = datetime.now()
        return _namespaces[session]


def _ns_context_for_agent(ns: dict) -> tuple[str, list]:
    """Retorna (vars_summary, script_history) para injetar no sub-agente como contexto."""
    _SKIP = _BUILTINS_SKIP | {"stats", "_current_query", "_script_history"}
    lines = []
    for k, v in ns.items():
        if k.startswith("_") or k in _SKIP:
            continue
        if isinstance(v, pd.DataFrame):
            lines.append(f"  - '{k}': DataFrame {len(v)} linhas, colunas: {list(v.columns)}")
        elif isinstance(v, pd.Series):
            lines.append(f"  - '{k}': Series {len(v)} itens")
    history = ns.get("_script_history", [])
    return "\n".join(lines), history


async def ns_cleanup_loop():
    """Loop assíncrono que expira namespaces inativos. Registrar via asyncio.create_task no startup."""
    import asyncio
    while True:
        await asyncio.sleep(60)
        cutoff = datetime.now() - timedelta(seconds=_NS_TTL_SECONDS)
        with _ns_lock:
            expired = [s for s, t in _ns_last_access.items() if t < cutoff]
            for s in expired:
                _namespaces.pop(s, None)
                _ns_last_access.pop(s, None)
        if expired:
            logger.info("Namespaces expirados removidos: %s", expired)


# Acima disso, `result` DataFrame é descrito, não exibido completo. Baixo de propósito: o
# retorno da tool é reenviado ao LLM em TODAS as chamadas seguintes, então tabelas grandes
# inflam o contexto (e a latência). A tabela final da resposta já é limitada a ~20 linhas.
_MAX_RESULT_ROWS = 100


def _df_describe(df: pd.DataFrame, nome: str = "result") -> str:
    """Descreve um DataFrame sem expor os dados completos ao LLM."""
    dtypes = ", ".join(f"{c}: {str(t)}" for c, t in df.dtypes.items())
    sample = df.head(1).to_markdown(index=False)
    return (
        f"`{nome}` — DataFrame com {len(df)} linhas e {len(df.columns)} colunas\n"
        f"Colunas e tipos: {dtypes}\n"
        f"Primeira linha de exemplo:\n{sample}\n"
        f"(Linhas omitidas para poupar contexto — o DataFrame completo segue no ambiente. "
        f"Para ver valores, agregue/filtre para poucas linhas ou use print(df.head(N)).)"
    )


def _ns_summary(ns: dict) -> str:
    lines = []
    for k, v in ns.items():
        if k.startswith("_") or k in _BUILTINS_SKIP:
            continue
        if isinstance(v, pd.DataFrame):
            dtypes = ", ".join(f"{c}:{str(t)}" for c, t in v.dtypes.items())
            lines.append(f"  {k}: DataFrame — {len(v)} linhas | {dtypes}")
        elif isinstance(v, pd.Series):
            lines.append(f"  {k}: Series — {len(v)} elementos, dtype: {v.dtype}")
        elif isinstance(v, (int, float, bool)):
            lines.append(f"  {k} = {v}")
        elif isinstance(v, str):
            preview = v[:60].replace("\n", " ")
            lines.append(f"  {k} = '{preview}{'…' if len(v) > 60 else ''}'")
        else:
            lines.append(f"  {k}: {type(v).__name__}")
    if not lines:
        return "\n---\n_Ambiente vazio — nenhuma variável definida ainda._"
    return "\n---\n**Variáveis disponíveis no ambiente** (criadas em passos anteriores):\n" + "\n".join(lines)


# ── Extração de thinking / texto de mensagens Gemini ─────────────────────────

def _extract_thinking(msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            # formato Gemini thought summaries: part.thought == True
            if p.get("thought") or p.get("type") == "thinking":
                text = p.get("text") or p.get("thinking", "")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return getattr(msg, "additional_kwargs", {}).get("reasoning_content", "")


def _extract_text_content(msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", "")) for p in content
            if isinstance(p, dict) and p.get("type") in ("text", None)
        )
    return content if isinstance(content, str) else ""


def _emit_agent_events(response, push_fn) -> None:
    """Extrai thinking, texto pré-tool e tool calls de qualquer resposta de agente
    e emite eventos via push_fn(event_dict)."""
    thinking = _extract_thinking(response)
    if thinking:
        push_fn({"type": "thinking", "text": thinking})

    tool_calls = getattr(response, "tool_calls", None) or []
    fc = getattr(response, "additional_kwargs", {}).get("function_call")
    if not tool_calls and fc:
        tool_calls = [{"name": fc.get("name", "")}]

    if tool_calls:
        pre_text = _extract_text_content(response)
        if pre_text:
            push_fn({"type": "thinking", "text": pre_text})
        for tc in tool_calls:
            name = tc.get("name", "")
            label = _lbl(_TOOL_LABELS.get(name, f"⚙ {name}"))
            push_fn({"type": "tool", "name": name, "label": label})


# ── Side-channel de eventos do sub-agente ─────────────────────────────────────
# O sub-agente roda dentro de um @tool (analisar_grafico), fora do stream do
# orquestrador. Usamos uma fila por sessão para capturar thinking e tool calls
# do sub-agente e entregá-los ao stream principal após cada nó completar.

_event_queues: dict = {}  # session_id → queue.Queue
_eq_lock = threading.Lock()

_TOOL_LABELS: dict[str, str] = {
    "read_skill":              "📖 Lendo instruções da skill",
    "calcular_periodo":        "📅 Calculando período",
    "executar_sql":            "🗄️ Consultando banco de dados (SQL)",
    "executar_graphql":        "🗄️ Consultando banco de dados (GraphQL)",
    "executar_graphql_agregado": "🗄️ Consultando banco de dados (GraphQL, agregado)",
    "executar_graphql_schema": "🗄️ Consultando esquema do banco (GraphQL)",
    "analisar_dataframe":      "🔢 Processando e analisando dados",
    "consultar_analista":      "🔍 Delegando ao sub-agente analista",
    "rag_dominio":             "📚 Consultando base de conhecimento (domínio)",
    "rag_capacidades":         "📚 Consultando base de conhecimento (capacidades)",
    "rag_arquitetura":         "📚 Consultando base de conhecimento (arquitetura)",
    "rag_dados":               "📚 Consultando base de conhecimento (dados)",
    "get_current_datetime":    "🕐 Verificando data e hora",
    "gerar_pdf":               "📄 Gerando relatório PDF",
    "gerar_excel":             "📊 Gerando planilha Excel",
    "gerenciar_agenda":          "🗓️ Gerenciando agenda de relatórios",
    "criar_tarefa_agendada":    "🗓️ Criando tarefa agendada",
    "editar_tarefa_agendada":   "✏️ Editando tarefa agendada",
    "schedule_task":            "🗓️ Agendando tarefa",
    "schedule_monitor":         "🔔 Criando monitor de threshold",
    "set_task_instructions":    "📝 Salvando instruções da tarefa",
    "get_task_instructions":    "🔍 Lendo instruções da tarefa",
    "list_scheduled_tasks":     "📋 Listando tarefas agendadas",
    "delete_scheduled_task":    "🗑️ Removendo tarefa agendada",
    "update_scheduled_task":    "✏️ Editando tarefa agendada",
    "toggle_pause_task":        "⏸️ Pausando/retomando tarefa",
    "get_task_code":            "🔍 Lendo código da tarefa",
    "test_task_code":           "🧪 Testando código da tarefa",
    "save_task_code":           "💾 Salvando código da tarefa",
    "get_task_code_versions":   "📜 Listando versões do código",
    "restore_task_code_version": "↩️ Restaurando versão do código",
    "test_widget_code":          "🧪 Testando código do painel customizado",
    "add_chart_to_dashboard":    "📌 Adicionando painel ao dashboard",
    "list_dashboard_widgets":    "📋 Listando painéis customizados",
    "delete_dashboard_widget":   "🗑️ Removendo painel do dashboard",
    "get_widget_code":           "🔍 Lendo código do painel",
    "update_widget":             "✏️ Atualizando painel do dashboard",
}

# Tradução dos labels de progresso (eventos SSE "tool", ver Streaming SSE em
# ARQUITETURA.md) — tanto os de _TOOL_LABELS acima quanto os escritos inline nos
# fluxos de agendamento/painéis abaixo. Call sites continuam em português (mais
# fácil de manter/grep); _lbl() troca pelo equivalente no idioma ativo quando
# db_config.get_lang() != pt-BR. Idioma/chave ausente aqui (esquecida na
# tradução) cai no próprio texto em português — nunca quebra, só fica sem traduzir.
_LABEL_TRANSLATIONS: dict[str, dict[str, str]] = {
    "📖 Lendo instruções da skill": {"en": "📖 Reading skill instructions", "zh": "📖 正在读取技能说明"},
    "📅 Calculando período": {"en": "📅 Calculating period", "zh": "📅 正在计算时间段"},
    "🗄️ Consultando banco de dados (SQL)": {"en": "🗄️ Querying database (SQL)", "zh": "🗄️ 正在查询数据库（SQL）"},
    "🗄️ Consultando banco de dados (GraphQL)": {"en": "🗄️ Querying database (GraphQL)", "zh": "🗄️ 正在查询数据库（GraphQL）"},
    "🗄️ Consultando banco de dados (GraphQL, agregado)": {"en": "🗄️ Querying database (GraphQL, aggregate)", "zh": "🗄️ 正在查询数据库（GraphQL，聚合）"},
    "🗄️ Consultando esquema do banco (GraphQL)": {"en": "🗄️ Querying database schema (GraphQL)", "zh": "🗄️ 正在查询数据库架构（GraphQL）"},
    "🔢 Processando e analisando dados": {"en": "🔢 Processing and analyzing data", "zh": "🔢 正在处理和分析数据"},
    "🔍 Delegando ao sub-agente analista": {"en": "🔍 Delegating to the analyst sub-agent", "zh": "🔍 正在委派给分析子代理"},
    "📚 Consultando base de conhecimento (domínio)": {"en": "📚 Consulting knowledge base (domain)", "zh": "📚 正在查询知识库（业务领域）"},
    "📚 Consultando base de conhecimento (capacidades)": {"en": "📚 Consulting knowledge base (capabilities)", "zh": "📚 正在查询知识库（功能）"},
    "📚 Consultando base de conhecimento (arquitetura)": {"en": "📚 Consulting knowledge base (architecture)", "zh": "📚 正在查询知识库（架构）"},
    "📚 Consultando base de conhecimento (dados)": {"en": "📚 Consulting knowledge base (data)", "zh": "📚 正在查询知识库（数据）"},
    "🕐 Verificando data e hora": {"en": "🕐 Checking date and time", "zh": "🕐 正在核对日期和时间"},
    "📄 Gerando relatório PDF": {"en": "📄 Generating PDF report", "zh": "📄 正在生成PDF报告"},
    "📊 Gerando planilha Excel": {"en": "📊 Generating Excel spreadsheet", "zh": "📊 正在生成Excel表格"},
    "🗓️ Gerenciando agenda de relatórios": {"en": "🗓️ Managing report schedule", "zh": "🗓️ 正在管理报告日程"},
    "🗓️ Criando tarefa agendada": {"en": "🗓️ Creating scheduled task", "zh": "🗓️ 正在创建定时任务"},
    "✏️ Editando tarefa agendada": {"en": "✏️ Editing scheduled task", "zh": "✏️ 正在编辑定时任务"},
    "🗓️ Agendando tarefa": {"en": "🗓️ Scheduling task", "zh": "🗓️ 正在安排任务"},
    "🔔 Criando monitor de threshold": {"en": "🔔 Creating threshold monitor", "zh": "🔔 正在创建阈值监控"},
    "📝 Salvando instruções da tarefa": {"en": "📝 Saving task instructions", "zh": "📝 正在保存任务说明"},
    "🔍 Lendo instruções da tarefa": {"en": "🔍 Reading task instructions", "zh": "🔍 正在读取任务说明"},
    "📋 Listando tarefas agendadas": {"en": "📋 Listing scheduled tasks", "zh": "📋 正在列出定时任务"},
    "🗑️ Removendo tarefa agendada": {"en": "🗑️ Removing scheduled task", "zh": "🗑️ 正在删除定时任务"},
    "⏸️ Pausando/retomando tarefa": {"en": "⏸️ Pausing/resuming task", "zh": "⏸️ 正在暂停/恢复任务"},
    "🔍 Lendo código da tarefa": {"en": "🔍 Reading task code", "zh": "🔍 正在读取任务代码"},
    "🧪 Testando código da tarefa": {"en": "🧪 Testing task code", "zh": "🧪 正在测试任务代码"},
    "💾 Salvando código da tarefa": {"en": "💾 Saving task code", "zh": "💾 正在保存任务代码"},
    "📜 Listando versões do código": {"en": "📜 Listing code versions", "zh": "📜 正在列出代码版本"},
    "↩️ Restaurando versão do código": {"en": "↩️ Restoring code version", "zh": "↩️ 正在恢复代码版本"},
    "🧪 Testando código do painel customizado": {"en": "🧪 Testing custom panel code", "zh": "🧪 正在测试自定义面板代码"},
    "📌 Adicionando painel ao dashboard": {"en": "📌 Adding panel to dashboard", "zh": "📌 正在将面板添加到仪表盘"},
    "📋 Listando painéis customizados": {"en": "📋 Listing custom panels", "zh": "📋 正在列出自定义面板"},
    "🗑️ Removendo painel do dashboard": {"en": "🗑️ Removing panel from dashboard", "zh": "🗑️ 正在从仪表盘删除面板"},
    "🔍 Lendo código do painel": {"en": "🔍 Reading panel code", "zh": "🔍 正在读取面板代码"},
    "✏️ Atualizando painel do dashboard": {"en": "✏️ Updating dashboard panel", "zh": "✏️ 正在更新仪表盘面板"},
    # labels inline dos fluxos de agendamento (não passam por _TOOL_LABELS)
    "🗓️ Criando registro da tarefa": {"en": "🗓️ Creating task record", "zh": "🗓️ 正在创建任务记录"},
    "🔍 Consultando dados reais": {"en": "🔍 Querying real data", "zh": "🔍 正在查询真实数据"},
    "⚙️ Gerando código da tarefa": {"en": "⚙️ Generating task code", "zh": "⚙️ 正在生成任务代码"},
    "🔍 Lendo código atual da tarefa": {"en": "🔍 Reading current task code", "zh": "🔍 正在读取任务当前代码"},
    "🔍 Sem código existente — gerando do zero": {"en": "🔍 No existing code — generating from scratch", "zh": "🔍 无现有代码——正在从零开始生成"},
    "✏️ Aplicando modificação ao código": {"en": "✏️ Applying change to code", "zh": "✏️ 正在对代码应用修改"},
    "🧪 Testando código": {"en": "🧪 Testing code", "zh": "🧪 正在测试代码"},
    "🔧 Corrigindo erro no código": {"en": "🔧 Fixing error in code", "zh": "🔧 正在修复代码错误"},
    "💾 Salvando código validado": {"en": "💾 Saving validated code", "zh": "💾 正在保存已验证的代码"},
    "🗓️ Iniciando criação de tarefa agendada": {"en": "🗓️ Starting scheduled task creation", "zh": "🗓️ 正在开始创建定时任务"},
    "✏️ Editando código da tarefa": {"en": "✏️ Editing task code", "zh": "✏️ 正在编辑任务代码"},
    # mensagem gravada no histórico quando o usuário para o run (ver _cancelled_reply)
    "⏹ Resposta interrompida pelo usuário.": {"en": "⏹ Response stopped by the user.", "zh": "⏹ 用户已停止回复。"},
}


def _lbl(texto_pt: str) -> str:
    lang = db_config.get_lang()
    if lang == "pt-BR":
        return texto_pt
    return _LABEL_TRANSLATIONS.get(texto_pt, {}).get(lang, texto_pt)


def _push_event(session_id: str, event: dict) -> None:
    if _cancelled():
        return  # run interrompido: não polui a queue do run seguinte da mesma sessão
    with _eq_lock:
        q = _event_queues.get(session_id)
    if q is not None:
        q.put(event)


# ── Cancelamento cooperativo (botão "parar") ──────────────────────────────────
# Threads Python não podem ser mortas e um llm.invoke() em andamento não é
# interrompível, então o cancelamento é cooperativo: cada run de chat tem um
# _RunHandle com um Event, e os nós dos grafos o consultam antes/depois de cada
# chamada de LLM. O handle viaja por ContextVar (mesmo caminho do session_id) para
# que um run "fantasma" continue enxergando o SEU flag mesmo depois que um run
# novo da mesma sessão tenha sido registrado.

class RunCancelled(Exception):
    """Lançada nos grafos aninhados (analista/scheduler) quando o usuário parou o run.

    O ToolNode do orquestrador converte a exceção em ToolMessage, então o histórico
    do checkpoint continua válido (todo tool_call tem resposta).
    """


class RunHandle:
    def __init__(self, session_id: str, prev: "RunHandle | None" = None):
        self.session_id = session_id
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.prev = prev  # run anterior da sessão, que precisa terminar antes deste começar


_active_runs: dict[str, RunHandle] = {}  # session_id → run mais recente
_runs_lock = threading.Lock()
_current_run: contextvars.ContextVar["RunHandle | None"] = contextvars.ContextVar(
    "mfg_run", default=None
)
_PREV_RUN_WAIT_SECONDS = 45


def begin_run(session_id: str) -> RunHandle:
    """Registra um novo run da sessão e sinaliza cancelamento ao anterior (se houver).

    Chamado de forma síncrona pelo endpoint antes de iniciar a thread do agente, para
    que o handle já exista quando um disconnect/cancel chegar.
    """
    with _runs_lock:
        prev = _active_runs.get(session_id)
        handle = RunHandle(session_id, prev=prev if prev and not prev.done.is_set() else None)
        _active_runs[session_id] = handle
    if handle.prev is not None:
        handle.prev.cancel.set()
    return handle


def cancel_run(session_id: str) -> bool:
    """Sinaliza cancelamento do run mais recente da sessão. True se havia run ativo."""
    with _runs_lock:
        handle = _active_runs.get(session_id)
    if handle is None or handle.done.is_set():
        return False
    handle.cancel.set()
    return True


def _cancelled() -> bool:
    handle = _current_run.get()
    return handle is not None and handle.cancel.is_set()


def _raise_if_cancelled() -> None:
    if _cancelled():
        raise RunCancelled("Execução interrompida pelo usuário.")


def _cancelled_reply() -> dict:
    """Update de estado que encerra o turno de forma válida para o checkpoint:
    AIMessage sem tool_calls, marcada para o stream emitir 'cancelled' em vez de 'reply'."""
    return {"messages": [AIMessage(
        content=_lbl("⏹ Resposta interrompida pelo usuário."),
        additional_kwargs={"cancelled": True},
    )]}


# ── Estados compartilhados ────────────────────────────────────────────────────

class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


class SchedulingState(TypedDict):
    mode: str          # "create" | "edit"
    session_id: str
    user_request: str  # original user request / modification description
    task_id: str       # empty until created; for edit, the target task
    task_name: str     # task name (for create)
    task_code: str     # code being built or corrected
    analista_context: str  # skill + endpoint + colunas + amostra de dados (create only)
    is_monitor: bool   # True se task_type=monitor (set by node_criar_tarefa)
    condition_sql_fix: str  # condition_sql corrigida com IDs reais (monitor GraphQL, opcional)
    error: str         # last test error; empty on success
    retries: int       # correction attempts so far
    result: str        # final output message


# ── Geração de PDF ───────────────────────────────────────────────────────────

import re as _re
import base64 as _b64

_CHART_TOKEN_RE = _re.compile(r'\[chart:([a-f0-9\-]{36})\]')


_PDF_CHAR_SUBS = {
    "—": "-", "–": "-",           # em dash, en dash
    "‘": "'", "’": "'",           # aspas simples curvas
    "“": '"', "”": '"',           # aspas duplas curvas
    "•": "-", "…": "...",         # bullet, reticências
}


def _safe(text: str) -> str:
    """Converte para latin-1 para compatibilidade com fontes core do fpdf2.

    Troca antes os símbolos tipográficos comuns em texto gerado por LLM (—, •, "…")
    por equivalentes ASCII — sem isso viram "?" pelo errors="replace" abaixo.
    """
    for ch, repl in _PDF_CHAR_SUBS.items():
        text = text.replace(ch, repl)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _pdf_render_table(pdf, rows: list[list[str]]) -> None:
    if not rows:
        return
    col_n = max(len(r) for r in rows)
    if col_n == 0:
        return
    # Máximo de colunas que cabem com largura mínima de 20mm
    max_cols = max(1, int(185 / 20))
    col_n = min(col_n, max_cols)
    col_w = 185 / col_n
    for i, row in enumerate(rows):
        if i == 0:
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_fill_color(20, 28, 39)
            pdf.set_text_color(226, 232, 240)
        else:
            pdf.set_font("Helvetica", "", 8)
            fill = (10, 14, 20) if i % 2 == 0 else (14, 20, 30)
            pdf.set_fill_color(*fill)
            pdf.set_text_color(148, 163, 184)
        pdf.set_x(pdf.l_margin)
        for cell in row[:col_n]:
            text = _safe(cell)
            while text and pdf.get_string_width(text) > col_w - 1:
                text = text[:-1]
            pdf.cell(col_w, 5.5, text, border=1, fill=True)
        pdf.ln()
    pdf.set_x(pdf.l_margin)
    pdf.ln(3)


def _pdf_safe_multicell(pdf, h: float, text: str) -> None:
    """Chama multi_cell garantindo que x está no l_margin e quebrando palavras longas."""
    pdf.set_x(pdf.l_margin)
    # Sanitiza ANTES de medir — pdf.get_string_width() faz encode estrito (fpdf2
    # levanta FPDFUnicodeEncodingException para caracteres fora do latin-1, como
    # "—" ou "•", diferente de _safe() que faz errors="replace"). Sem isso, um
    # único caractere assim aborta TODO o conteúdo do PDF (inclusive charts
    # embutidos depois no fluxo), mesmo a página tendo sido gerada com sucesso.
    text = _safe(text)
    # Quebra palavras sem espaço que ultrapassem a largura disponível
    available = pdf.w - pdf.l_margin - pdf.r_margin
    words = text.split(" ")
    safe_words = []
    for w in words:
        if pdf.get_string_width(w) > available - 2:
            # Quebra o token longo em pedaços que caibam
            chunk, buf = "", ""
            for ch in w:
                if pdf.get_string_width(buf + ch) > available - 2:
                    safe_words.append(buf)
                    buf = ch
                else:
                    buf += ch
            if buf:
                safe_words.append(buf)
        else:
            safe_words.append(w)
    pdf.multi_cell(0, h, _safe(" ".join(safe_words)))


def _pdf_render_text(pdf, text: str) -> None:
    table_rows: list[list[str]] = []

    def flush_table():
        if table_rows:
            _pdf_render_table(pdf, table_rows)
            table_rows.clear()

    for line in text.split("\n"):
        s = line.strip()
        if not s:
            flush_table()
            pdf.ln(2)
            continue

        # Linha separadora de tabela (|---|---|)
        if s.startswith("|") and s.endswith("|") and _re.fullmatch(r'[\|\-\: ]+', s):
            continue

        # Linha de tabela
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s[1:-1].split("|")]
            table_rows.append(cells)
            continue

        flush_table()

        if s.startswith("### "):
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(226, 232, 240)
            _pdf_safe_multicell(pdf, 6, s[4:])
            pdf.ln(1)
        elif s.startswith("## "):
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_text_color(56, 189, 248)
            _pdf_safe_multicell(pdf, 7, s[3:])
            pdf.ln(2)
        elif s.startswith("# "):
            pdf.set_font("Helvetica", "B", 15)
            pdf.set_text_color(56, 189, 248)
            _pdf_safe_multicell(pdf, 8, s[2:])
            pdf.ln(3)
        elif s.startswith(("- ", "* ")):
            clean = _re.sub(r'\*\*(.*?)\*\*', r'\1', s[2:])
            clean = _re.sub(r'`(.*?)`', r'\1', clean)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(148, 163, 184)
            _pdf_safe_multicell(pdf, 5, f"  • {clean}")
        else:
            clean = _re.sub(r'\*\*(.*?)\*\*', r'\1', s)
            clean = _re.sub(r'\*(.*?)\*', r'\1', clean)
            clean = _re.sub(r'`(.*?)`', r'\1', clean)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(148, 163, 184)
            _pdf_safe_multicell(pdf, 5, clean)

    flush_table()


def _build_pdf(titulo: str, conteudo: str, session_id: str, user_id: str | None = None) -> str:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Cabeçalho
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(56, 189, 248)
    _pdf_safe_multicell(pdf, 10, titulo)
    pdf.ln(2)
    pdf.set_draw_color(30, 45, 61)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(5)

    # Divide conteúdo em partes de texto e tokens de chart
    try:
        parts = _CHART_TOKEN_RE.split(conteudo)
        for i, part in enumerate(parts):
            if i % 2 == 0:
                _pdf_render_text(pdf, part)
            else:
                b64 = chart_store.get_chart_b64(part)
                if b64:
                    img_bytes = _b64.b64decode(b64)
                    if pdf.get_y() > 210:
                        pdf.add_page()
                    pdf.image(io.BytesIO(img_bytes), x=10, w=190)
                    pdf.ln(3)
    except Exception:
        logger.exception("[_build_pdf] Erro ao renderizar conteúdo do PDF '%s'", titulo)
        # Fallback: página limpa com mensagem de erro de layout
        pdf.add_page()
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(148, 163, 184)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 6, _safe("Erro ao renderizar conteudo formatado. Verifique o conteudo gerado."))

    pdf_bytes = bytes(pdf.output())
    safe_name = _re.sub(r'[^\w\- ]', '', titulo)[:40].strip().replace(" ", "_")
    filename = f"{safe_name or 'relatorio'}.pdf"
    return chart_store.save_pdf(session_id, pdf_bytes, filename, user_id if user_id is not None else _current_user_id.get())


# ── Tools do orquestrador ─────────────────────────────────────────────────────

_RAG_DIR = Path(__file__).parent / "rag"


_DIRETIVA_IDIOMA = {
    "pt-BR": "Responda sempre em português do Brasil, independente do idioma em que as instruções deste prompt foram escritas.",
    "en": "Always reply in English, regardless of the language these instructions were written in.",
    "zh": "始终用中文回复，无论本提示词是用什么语言写的。",
}


def _diretiva_idioma() -> str:
    """Linha injetada no topo de cada system prompt (orquestrador/sub-agente/scheduler)
    dizendo em que idioma responder. As instruções do prompt em si continuam só em
    português — LLMs seguem instrução num idioma e respondem em outro sem problema —
    só a resposta final ao usuário precisa mudar de idioma por requisição. Avaliada a
    cada turno (não uma vez no init) porque db_config.get_lang() é por-requisição."""
    lang = db_config.get_lang()
    texto = _DIRETIVA_IDIOMA.get(lang, _DIRETIVA_IDIOMA[db_config.idioma_default()])
    return f"## IDIOMA DA RESPOSTA\n{texto}\n\n"


def _rag_path(filename: str) -> Path:
    """Resolve rag/<lang>/<filename> no idioma efetivo, com fallback para o idioma
    default da instalação — mesmo padrão de db_config.dominio_path()."""
    lang = db_config.get_lang()
    candidato = _RAG_DIR / lang / filename
    if candidato.exists():
        return candidato
    fallback = db_config.idioma_default()
    candidato_fallback = _RAG_DIR / fallback / filename
    if fallback != lang and candidato_fallback.exists():
        return candidato_fallback
    return candidato


@tool
def rag_dominio() -> str:
    """Retorna as regras de negócio e o glossário do domínio configurado: o que é o sistema,
    como as métricas são calculadas, conceitos e termos específicos do negócio.

    Use quando o usuário perguntar: o que este sistema faz, o que significa um termo ou
    métrica do domínio, como um valor é calculado, quais são as regras de negócio, etc.
    """
    _tlog("rag_dominio", "CHAMADA", arquivo=db_config.dominio_filename())
    corpo = db_config.get_secao("Domínio de negócio")
    _tlog("rag_dominio", "RETORNO", arquivo=db_config.dominio_filename(), chars=len(corpo))
    return corpo or db_config.MSG_NAO_CONFIGURADO


@tool
def rag_capacidades() -> str:
    """Retorna o catálogo completo de capacidades do agente: tipos de análise disponíveis,
    exemplos de perguntas que podem ser feitas, como solicitar gráficos/tabelas/PDFs/Excel,
    como criar relatórios periódicos agendados, como configurar monitores com alertas,
    frequências suportadas e como gerenciar tarefas agendadas (listar, pausar, deletar, editar).

    Use SOMENTE quando o usuário PERGUNTAR sobre isso (ex: "o que você pode fazer",
    "como funciona o agendamento", "quais frequências existem") — nunca como pesquisa
    prévia antes de executar um pedido de AÇÃO ("crie um alerta...", "agende..."). Para
    ações de agendamento, vá direto a criar_tarefa_agendada/gerenciar_agenda — elas já
    sabem o formato certo, não é preciso consultar esta tool antes.
    """
    caminho = _rag_path("capacidades.md")
    _tlog("rag_capacidades", "CHAMADA", arquivo=str(caminho))
    return caminho.read_text(encoding="utf-8")


@tool
def rag_arquitetura() -> str:
    """Retorna a arquitetura técnica do sistema de agentes: como o orquestrador roteia
    pedidos, como o sub-agente analista executa análises (read_skill → executar_sql →
    analisar_dataframe), o catálogo de skills disponíveis, como o sub-agente de scheduling
    gerencia tarefas e ativa a execução determinística via task_code, e os métodos do ctx.

    Use SOMENTE quando o usuário PERGUNTAR sobre isso (ex: "como o sistema funciona
    internamente", "o que é o modo determinístico") — nunca como pesquisa prévia antes
    de executar um pedido de AÇÃO. Para criar/editar tarefas, vá direto a
    criar_tarefa_agendada/editar_tarefa_agendada — não é preciso consultar esta tool antes.
    """
    caminho = _rag_path("arquitetura.md")
    _tlog("rag_arquitetura", "CHAMADA", arquivo=str(caminho))
    return caminho.read_text(encoding="utf-8")


@tool
def rag_dados() -> str:
    """Retorna a referência técnica de dados do banco configurado: schema completo
    (tabelas, colunas, tipos, descrições) e as chaves de junção entre tabelas.

    Use quando o usuário perguntar: quais tabelas existem, qual o schema do banco,
    como cruzar tabelas, quais campos estão disponíveis para análise, etc.
    """
    _tlog("rag_dados", "CHAMADA", arquivo=db_config.dominio_filename())
    corpo = db_config.get_secao("Modelo de dados")
    _tlog("rag_dados", "RETORNO", arquivo=db_config.dominio_filename(), chars=len(corpo))
    return corpo or db_config.MSG_NAO_CONFIGURADO

@tool
def gerar_pdf(titulo: str, conteudo: str) -> str:
    """Gera PDF. NÃO usar para Excel, planilha, .xlsx ou spreadsheet — use gerar_excel nesses casos.

    Use APENAS quando o usuário mencionar explicitamente "PDF" ou ".pdf".
    Deve ser chamado APÓS consultar_analista ter retornado a análise completa.

    Fluxo correto:
      1. Chame consultar_analista() para obter a análise com gráficos.
      2. Chame gerar_pdf() passando o resultado completo como conteudo.
      3. Responda ao usuário incluindo o token [pdf:uuid] retornado — ele será
         automaticamente convertido em link de download.

    REGRA DE CONTEÚDO — o argumento `conteudo` deve conter APENAS:
      - Títulos de seções (ex: "## Pedidos por Status")
      - Tokens de gráficos gerados: [chart:uuid]
      - Tabelas de dados em markdown
      - Conclusões analíticas objetivas (números, tendências, comparações)

    PROIBIDO no conteudo — NÃO inclua NENHUMA dessas formas de texto:
      - Abertura: "Aqui está o gráfico...", "Segue abaixo...", "Conforme solicitado...", "Claro!"
      - Fechamento: "Gerado a partir dos dados...", "Relatório gerado em...", "Espero que ajude!",
        "Qualquer dúvida estou à disposição.", "Este relatório foi gerado automaticamente."
      - Confirmações/meta-comentários: "Análise concluída!", "O PDF foi gerado.", "Pronto!"
      - Qualquer frase que fale sobre o próprio relatório em vez de conter dados do relatório

    Exemplos CORRETOS de conteudo:
      "## Pedidos por Status\\n[chart:abc-123]\\n\\nAPPROVED liderou com 1.240 pedidos (82%).\\nINCONSISTENCY: 198 (13%). Taxa de aprovação: 82,3%."

    Exemplos ERRADOS (nunca incluir):
      INÍCIO: "Aqui está a análise de produção que você pediu.\\n## Produção Semanal\\n..."
      FIM: "...Taxa de aprovação: 82,3%.\\n\\nGerado a partir dos dados do sistema."
      FIM: "...Taxa de aprovação: 82,3%.\\n\\nEspero que o relatório seja útil!"

    Args:
        titulo: Título do relatório (ex: "Análise de Produção — Maio 2026").
        conteudo: Análise em markdown com tokens [chart:uuid], tabelas e conclusões.
                  Sem texto introdutório ou conversacional.
    """
    _tlog("gerar_pdf", "CHAMADA", titulo=titulo, conteudo_chars=len(conteudo))
    try:
        pdf_id = _build_pdf(titulo, conteudo, _current_session.get())
        result = f"[pdf:{pdf_id}]"
        _tlog("gerar_pdf", "RETORNO", status="OK", pdf_id=pdf_id)
        return result
    except Exception as e:
        msg = f"Erro ao gerar PDF: {e}"
        _tlog("gerar_pdf", "RETORNO", status="ERRO", erro=msg)
        return msg


@tool
def gerar_excel(nome_dataframe: str, nome_arquivo: str, sheets: Optional[str] = None) -> str:
    """Cria um arquivo Excel (.xlsx) e disponibiliza link para download.

    Use quando o usuário pedir Excel, planilha, xlsx ou spreadsheet.
    Não use para PDF (use gerar_pdf) nem para exibir tabela na tela.

    Fluxo correto:
      1. Chame consultar_analista(tipo='tabela') para carregar os dados no namespace.
         O sub-agente retornará o nome do DataFrame (ex: 'producao', 'resultado').
      2. Chame gerar_excel() passando o nome do DataFrame.
      3. Responda ao usuário incluindo o token [excel:uuid] retornado — ele será
         automaticamente convertido em link de download.

    Para múltiplas abas, use sheets='Aba1=df1,Aba2=df2'.

    Args:
        nome_dataframe: Nome do DataFrame no namespace (ex: 'resultado'). Ignorado se sheets fornecido.
        nome_arquivo: Nome do arquivo sem extensão (ex: 'producao_maio').
        sheets: Opcional. Múltiplas abas no formato 'Aba1=df1,Aba2=df2'.
    """
    import io
    _tlog("gerar_excel", "CHAMADA", nome_dataframe=nome_dataframe, nome_arquivo=nome_arquivo, sheets=sheets)
    try:
        ns = _ns_get()
        output = io.BytesIO()

        if sheets and sheets.strip():
            # múltiplas abas: "Aba1=df1,Aba2=df2"
            mapa = {}
            for par in sheets.split(","):
                par = par.strip()
                if "=" not in par:
                    continue
                aba, var = par.split("=", 1)
                mapa[aba.strip()] = var.strip()

            if not mapa:
                return "Erro: parâmetro sheets inválido. Use formato 'Aba1=df1,Aba2=df2'."

            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                for aba, var in mapa.items():
                    df = ns.get(var)
                    if df is None or not isinstance(df, pd.DataFrame):
                        return f"Erro: variável '{var}' não encontrada ou não é um DataFrame."
                    df.to_excel(writer, sheet_name=aba[:31], index=False)
                    _fmt_excel_sheet(writer.sheets[aba[:31]], df)
        else:
            df = ns.get(nome_dataframe)
            if df is None or not isinstance(df, pd.DataFrame):
                return (
                    f"Erro: variável '{nome_dataframe}' não encontrada no namespace. "
                    f"Variáveis disponíveis: {[k for k, v in ns.items() if isinstance(v, pd.DataFrame)]}"
                )
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="Dados", index=False)
                _fmt_excel_sheet(writer.sheets["Dados"], df)

        excel_bytes = output.getvalue()
        safe_name = _re.sub(r'[^\w\- ]', '', nome_arquivo)[:40].strip().replace(" ", "_")
        filename = f"{safe_name or 'dados'}.xlsx"
        excel_id = chart_store.save_excel(_current_session.get(), excel_bytes, filename, _current_user_id.get())
        result = f"[excel:{excel_id}]"
        _tlog("gerar_excel", "RETORNO", status="OK", excel_id=excel_id, filename=filename)
        return result
    except Exception as e:
        msg = f"Erro ao gerar Excel: {e}"
        _tlog("gerar_excel", "RETORNO", status="ERRO", erro=msg)
        return msg


def _fmt_excel_sheet(ws, df: pd.DataFrame) -> None:
    """Aplica largura automática e cabeçalho destacado na worksheet openpyxl."""
    from openpyxl.styles import PatternFill, Font, Alignment

    header_fill = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col_idx, col_name in enumerate(df.columns, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

        # auto-width: maior entre o cabeçalho e o conteúdo da coluna
        max_len = len(str(col_name))
        for row_idx in range(2, min(len(df) + 2, 1002)):  # amostra de até 1000 linhas
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 4, 60)


@tool
def get_current_datetime() -> str:
    """Retorna a data e hora atual do sistema.

    Use esta tool sempre que o usuário perguntar que horas são,
    qual é a data de hoje, ou qualquer variação dessas perguntas.
    """
    result = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    _tlog("get_current_datetime", "RETORNO", resultado=result)
    return result


@tool
def get_dashboard_charts() -> str:
    """Retorna os dados dos gráficos e KPIs atualmente exibidos no dashboard do usuário.

    Use esta tool quando o usuário perguntar sobre o que está vendo na tela,
    mencionar valores, tendências ou comparações visíveis nos gráficos,
    ou usar expressões como 'no dashboard', 'na tela', 'o que está aparecendo',
    'o gráfico mostra', 'os KPIs', 'os alertas ativos'.
    NÃO use para análises que exijam buscar dados históricos — use consultar_analista nesses casos.
    """
    session = _current_session.get()
    with _cs_lock:
        snapshot = _chart_snapshots.get(session)

    if not snapshot:
        return "Nenhum snapshot do dashboard disponível para esta sessão."

    lines: list[str] = []

    kpis = snapshot.get("kpis", [])
    if kpis:
        lines.append("=== KPIs ===")
        for k in kpis:
            badge = f" [{k.get('badge', '')}]" if k.get("badge") else ""
            lines.append(f"  {k['label']}: {k['value']}{badge}")
        lines.append("")

    charts = snapshot.get("charts", {})
    for chart_id, chart in charts.items():
        title = chart.get("title", chart_id)
        labels = chart.get("labels", [])
        datasets = chart.get("datasets", [])
        lines.append(f"=== {title} ===")
        if labels:
            lines.append(f"  Labels: {', '.join(str(l) for l in labels)}")
        for ds in datasets:
            ds_label = ds.get("label", "")
            data_str = ", ".join(str(v) for v in ds.get("data", []))
            lines.append(f"  {ds_label}: {data_str}")
        lines.append("")

    alerts = snapshot.get("alerts", [])
    if alerts:
        lines.append(f"=== Alertas ({len(alerts)} total) ===")
        for a in alerts[:20]:
            val = a.get("value")
            thr = a.get("threshold")
            ts = a.get("created_at", "")
            detail = f" (valor: {val}, limiar: {thr})" if val is not None else ""
            read_flag = " [lido]" if a.get("read") else " [novo]"
            lines.append(f"  [{ts}]{read_flag} {a.get('message', '')}{detail}")
    else:
        lines.append("=== Alertas ===\n  Nenhum alerta ativo na tela.")
    lines.append("")

    artifacts = snapshot.get("artifacts", [])
    if artifacts:
        lines.append(f"=== Artifacts ({len(artifacts)} total) ===")
        for a in artifacts:
            lines.append(f"  [{a.get('type', '?')}] {a.get('label', a.get('id', ''))} — {a.get('created_at', '')}")
    else:
        lines.append("=== Artifacts ===\n  Nenhum artifact gerado.")
    lines.append("")

    tasks = snapshot.get("tasks", [])
    if tasks:
        lines.append(f"=== Tarefas Agendadas ({len(tasks)} total) ===")
        for t in tasks:
            lines.append(f"  [#{t.get('id', '?')}] {t.get('name', '')} — status: {t.get('status', '')} — schedule: {t.get('schedule', '')}")
    else:
        lines.append("=== Tarefas Agendadas ===\n  Nenhuma tarefa agendada.")

    _tlog("get_dashboard_charts", "RETORNO", linhas=len(lines))
    return "\n".join(lines) if lines else "Dashboard sem dados disponíveis."


# ── Tools do sub-agente ───────────────────────────────────────────────────────

@tool
def read_skill(filename: str) -> str:
    """Lê as instruções completas de uma skill do catálogo do system prompt.

    A skill 'dominio.md' (tabelas, colunas e como processar os dados) já vem carregada
    no system prompt — NÃO chame esta tool para ela, salvo se a seção
    '## Skill dominio.md' estiver ausente. Use para as demais skills do catálogo.

    Args:
        filename: Nome do arquivo .md da skill.
                  Consulte o catálogo no system prompt para ver os arquivos disponíveis.
    """
    _tlog("read_skill", "CHAMADA", filename=filename)
    if filename == "dominio.md":
        conteudo = db_config.skill_dominio_texto()
        if not conteudo:
            conteudo = db_config.MSG_NAO_CONFIGURADO
        _tlog("read_skill", "RETORNO", chars=len(conteudo), conteudo=conteudo)
        return conteudo
    caminho = SKILLS_FOLDER / filename
    if not caminho.exists():
        arquivos = ["dominio.md"] + [f for f in os.listdir(SKILLS_FOLDER) if f.endswith(".md")]
        msg = f"Arquivo '{filename}' não encontrado. Skills disponíveis: {arquivos}"
        _tlog("read_skill", "RETORNO", resultado=msg)
        return msg
    conteudo = caminho.read_text(encoding="utf-8")
    _tlog("read_skill", "RETORNO", chars=len(conteudo), conteudo=conteudo)
    return conteudo


@tool
def executar_sql(query: str, chave: str) -> str:
    """Executa uma query SQL SELECT no banco configurado e injeta o resultado como DataFrame no ambiente.

    O schema real (tabelas, colunas, joins e regras) está na skill 'dominio.md' —
    leia-a ANTES de escrever qualquer SQL.

    O DataFrame fica disponível como variável com o nome da chave (ex: chave='resultado'
    → variável `resultado` no ambiente para uso em analisar_dataframe).

    IMPORTANTE: apenas SELECT é permitido. Qualquer outra instrução será rejeitada.
    Não use SELECT * — liste apenas as colunas necessárias.

    Args:
        query: Query SQL SELECT a ser executada.
        chave: Nome da variável no ambiente onde o DataFrame será armazenado (ex: 'resultado', 'dados').
    """
    _tlog("executar_sql", "CHAMADA", query=query, chave=chave)

    normalized = query.strip().lstrip("(").upper()
    if not normalized.startswith("SELECT"):
        msg = "Apenas queries SELECT são permitidas. Instrução rejeitada."
        _tlog("executar_sql", "RETORNO", status="ERRO", erro=msg)
        return msg

    if not db_config.is_configured():
        msg = db_config.MSG_NAO_CONFIGURADO
        _tlog("executar_sql", "RETORNO", status="ERRO", erro=msg)
        return msg

    try:
        df = pd.read_sql_query(query, db_config.get_engine())
        ns = _ns_get()
        ns[chave] = df
        msg = (
            f"DataFrame '{chave}' injetado no ambiente: {len(df)} linhas, "
            f"colunas: {list(df.columns)}."
        )
        _tlog("executar_sql", "RETORNO", status="OK", linhas=len(df), colunas=list(df.columns))
    except Exception as e:
        msg = f"Erro ao executar SQL: {e}"
        _tlog("executar_sql", "RETORNO", status="ERRO", erro=msg)
        return msg

    return msg + _ns_summary(ns)


@tool
def executar_graphql(
    table: str,
    chave: str,
    where: "list[dict] | None" = None,
    order_by: "dict | None" = None,
    limit: "int | None" = None,
    offset: "int | None" = None,
    expand: "list[dict] | None" = None,
) -> str:
    """Busca linhas de uma tabela via GraphQL (reportTable) e injeta o resultado como
    DataFrame no ambiente.

    O schema real (tabelas, colunas e regras) está na skill 'dominio.md' — leia-a ANTES
    de chamar esta tool. Retorna linhas cruas de UMA tabela por vez. `expand` permite
    juntar colunas de UMA tabela relacionada (join de um salto só) na mesma chamada —
    para combinar mais de duas tabelas, ou tabelas sem FK direta entre si, chame esta
    tool mais de uma vez e junte os DataFrames em analisar_dataframe.

    Args:
        table: nome da tabela a consultar.
        chave: nome da variável no ambiente onde o DataFrame será armazenado.
        where: lista de condições combinadas com AND, ex:
            [{"field": "status", "op": "EQ", "value": "open"},
             {"field": "created_at", "op": "GTE", "value": "2026-01-01"}]
            Operadores válidos: EQ, NEQ, GT, GTE, LT, LTE, IN, NOT_IN, LIKE, ILIKE,
            IS_NULL, IS_NOT_NULL.
        order_by: ex: {"field": "created_at", "direction": "DESC"}.
        limit: máximo de linhas a retornar.
        offset: quantas linhas pular (paginação, junto com limit).
        expand: lista de joins de um salto, ex:
            [{"viaColumn": "customer_id", "columns": ["name", "channel"]}]
            `viaColumn` tem que ser uma foreign key de `table` (ver Grafo de relações
            na skill). As colunas pedidas vêm em cada linha prefixadas
            "viaColumn__coluna" (ex: "customer_id__name") — não precisa de merge
            em pandas depois para esse caso.
    """
    _tlog("executar_graphql", "CHAMADA", table=table, chave=chave, where=where, order_by=order_by, limit=limit, offset=offset, expand=expand)

    if not db_config.is_configured():
        msg = db_config.MSG_NAO_CONFIGURADO
        _tlog("executar_graphql", "RETORNO", status="ERRO", erro=msg)
        return msg

    try:
        rows = graphql_config.run_query_table(table, where=where, order_by=order_by, limit=limit, offset=offset, expand=expand)
        df = pd.DataFrame(rows)
        ns = _ns_get()
        ns[chave] = df
        msg = (
            f"DataFrame '{chave}' injetado no ambiente: {len(df)} linhas, "
            f"colunas: {list(df.columns)}."
        )
        _tlog("executar_graphql", "RETORNO", status="OK", linhas=len(df), colunas=list(df.columns))
    except Exception as e:
        msg = f"Erro ao executar GraphQL: {e}"
        _tlog("executar_graphql", "RETORNO", status="ERRO", erro=msg)
        return msg

    return msg + _ns_summary(ns)


@tool
def executar_graphql_agregado(
    table: str,
    chave: str,
    group_by: str,
    metric_field: str,
    metric_op: str,
    where: "list[dict] | None" = None,
    expand: "list[str] | None" = None,
    order_by: "str | None" = None,
    limit: "int | None" = None,
) -> str:
    """Agrega uma tabela via GraphQL (reportAggregate — GROUP BY + SUM/COUNT/AVG/MIN/MAX
    calculados no banco) e injeta o resultado como DataFrame no ambiente.

    Use esta tool em vez de executar_graphql + pandas quando o pedido é uma contagem,
    soma, média, mínimo ou máximo agrupado por uma coluna — evita trazer a tabela
    inteira (que pode ser grande) só para agregar depois em Python.

    O schema real (tabelas, colunas e regras) está na skill 'dominio.md' — leia-a ANTES
    de chamar esta tool.

    Args:
        table: nome da tabela a agregar.
        chave: nome da variável no ambiente onde o DataFrame será armazenado.
        group_by: coluna de `table` para agrupar (GROUP BY).
        metric_field: coluna de `table` a agregar.
        metric_op: SUM, COUNT, AVG, MIN ou MAX.
        where: mesmo formato de executar_graphql — filtra ANTES de agregar.
        expand: lista de nomes de coluna de OUTRA tabela a trazer junto — só funciona
            se `group_by` for uma foreign key (ex: group_by="customer_id",
            expand=["name", "channel"] traz customer.name e customer.channel prontos
            em cada linha, sem precisar de merge em pandas depois). Não confundir com
            join de múltiplos saltos — só alcança a tabela referenciada diretamente
            por `group_by`.
        order_by: "ASC" ou "DESC" — ordena pelo valor agregado. Default do servidor: DESC.
        limit: máximo de grupos a retornar. Default do servidor: 20, teto: 500.

    O DataFrame resultante tem as colunas "groupKey" (valor de group_by), "metricValue"
    (resultado da agregação) e uma por coluna pedida em `expand`.
    """
    _tlog(
        "executar_graphql_agregado", "CHAMADA",
        table=table, chave=chave, group_by=group_by, metric_field=metric_field,
        metric_op=metric_op, where=where, expand=expand, order_by=order_by, limit=limit,
    )

    if not db_config.is_configured():
        msg = db_config.MSG_NAO_CONFIGURADO
        _tlog("executar_graphql_agregado", "RETORNO", status="ERRO", erro=msg)
        return msg

    try:
        rows = graphql_config.run_aggregate(
            table, group_by=group_by, metric_field=metric_field, metric_op=metric_op,
            where=where, expand=expand, order_by=order_by, limit=limit,
        )
        df = pd.DataFrame(rows)
        ns = _ns_get()
        ns[chave] = df
        msg = (
            f"DataFrame '{chave}' injetado no ambiente: {len(df)} linhas, "
            f"colunas: {list(df.columns)}."
        )
        _tlog("executar_graphql_agregado", "RETORNO", status="OK", linhas=len(df), colunas=list(df.columns))
    except Exception as e:
        msg = f"Erro ao executar GraphQL (agregado): {e}"
        _tlog("executar_graphql_agregado", "RETORNO", status="ERRO", erro=msg)
        return msg

    return msg + _ns_summary(ns)


@tool
def executar_graphql_schema() -> str:
    """Consulta o schema real (tabelas e colunas) exposto pelo backend GraphQL (reportSchema)
    e retorna como texto — NÃO injeta DataFrame no ambiente, é só para leitura/conferência.

    Sem argumentos — traz todas as tabelas do domínio ativo de uma vez.

    Use esta tool quando a skill 'dominio.md' não for suficiente para resolver a análise:
    coluna/tabela citada na skill que o servidor rejeita como inexistente, dúvida sobre o
    tipo real de uma coluna, ou suspeita de que a skill esteja desatualizada em relação ao
    que o backend expõe hoje. Não é o fluxo padrão — leia a skill primeiro; só consulte o
    schema se estiver travado.
    """
    _tlog("executar_graphql_schema", "CHAMADA")

    if not db_config.is_configured():
        msg = db_config.MSG_NAO_CONFIGURADO
        _tlog("executar_graphql_schema", "RETORNO", status="ERRO", erro=msg)
        return msg

    try:
        tables = graphql_config.run_schema()
        linhas = [f"Schema real do backend GraphQL — {len(tables)} tabela(s):\n"]
        for t in tables:
            cols = ", ".join(
                f"{c['column']} ({c['dataType']}{', nullable' if c.get('nullable') else ''})"
                for c in t.get("columns", [])
            )
            linhas.append(f"- {t['table']}: {cols}")
        msg = "\n".join(linhas)
        _tlog("executar_graphql_schema", "RETORNO", status="OK", tabelas=len(tables))
    except Exception as e:
        msg = f"Erro ao consultar schema GraphQL: {e}"
        _tlog("executar_graphql_schema", "RETORNO", status="ERRO", erro=msg)
        return msg

    return msg


@tool
def analisar_dataframe(script: str) -> str:
    """Processa e analisa os dados disponíveis no ambiente, retornando o resultado formatado.

    Variáveis carregadas por executar_sql/executar_graphql estão disponíveis pelo nome da chave.
    Variáveis criadas em chamadas anteriores desta tool também estão disponíveis.
    Atribua à variável `result` o que deve ser exibido — DataFrames são automaticamente
    convertidos para tabela markdown. Use print() para textos adicionais.

    IMPORTANTE — nomeação de variáveis para geração de Excel:
    Quando o resultado será usado pelo orquestrador para gerar_excel, atribua o DataFrame
    a uma variável com nome descritivo no script (ex: `producao = df_filtrado`).
    Após executar, mencione explicitamente na resposta final o nome da variável
    (ex: "DataFrame 'producao' disponível no namespace com 30 linhas.").

    Args:
        script: Lógica de análise a executar sobre os dados disponíveis.
    """
    _tlog("analisar_dataframe", "CHAMADA", script=script)

    ns = _ns_get()

    if _sandbox_mode.get():
        import re as _re_sb
        # Remove imports explícitos — tudo já está pré-injetado no namespace do sandbox.
        script = "\n".join(
            ln for ln in script.splitlines()
            if not _re_sb.match(r'\s*(import |from \S+ import )', ln)
        )
        from scheduler.runner import _build_globals
        from scheduler.context import TaskContext
        globals_exec = _build_globals(
            TaskContext(_current_session.get()), "2000-01-01", "2000-01-01"
        )
        globals_exec.update({k: v for k, v in ns.items() if isinstance(v, pd.DataFrame)})
    else:
        globals_exec = ns

    # `result` persiste no namespace entre chamadas; sem limpar, um script que não o
    # reatribua exibiria o resultado da chamada anterior.
    globals_exec.pop("result", None)

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    _t0 = _time.perf_counter()
    try:
        exec(textwrap.dedent(script).lstrip("\n"), globals_exec)
        _t_exec = _time.perf_counter() - _t0
        if globals_exec is not ns:
            # No sandbox o exec roda num dict descartável; grava de volta os DataFrames
            # criados/reatribuídos para que persistam entre chamadas, como no modo normal.
            ns.update({k: v for k, v in globals_exec.items()
                       if isinstance(v, pd.DataFrame) and not k.startswith("_")})
        console = sys.stdout.getvalue().strip()
        result = globals_exec.get("result")

        parts = []
        if console:
            parts.append(f"```\n{console}\n```")
        if result is not None:
            if isinstance(result, matplotlib.figure.Figure):
                buf = io.BytesIO()
                result.savefig(
                    buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor="white", edgecolor="none",
                )
                buf.seek(0)
                chart_id = chart_store.save_chart(_current_session.get(), buf.getvalue(), _current_user_id.get())
                plt.close(result)
                parts.append(f"[chart:{chart_id}]")
            elif isinstance(result, pd.DataFrame):
                if len(result) <= _MAX_RESULT_ROWS:
                    parts.append(result.to_markdown(index=False))
                else:
                    parts.append(_df_describe(result))
            elif isinstance(result, pd.Series):
                if len(result) <= _MAX_RESULT_ROWS:
                    parts.append(result.to_frame().to_markdown())
                else:
                    parts.append(_df_describe(result.to_frame()))
            else:
                parts.append(str(result))
        if not parts:
            parts.append("Script executado (sem saída em `result`).")

        history = ns.get("_script_history", [])
        history.append({"query": ns.get("_current_query", ""), "script": script})
        ns["_script_history"] = history[-3:]

        parts.append(_ns_summary(ns))
        saida = "\n\n".join(parts)
        logger.info("[TIMING] analisar_dataframe exec=%.2fs render=%.2fs saida=%d chars",
                    _t_exec, _time.perf_counter() - _t0 - _t_exec, len(saida))
        _tlog("analisar_dataframe", "RETORNO", status="OK", saida=saida)
        return saida

    except Exception as e:
        logger.info("[TIMING] analisar_dataframe ERRO após %.2fs", _time.perf_counter() - _t0)
        msg = f"Erro:\n{type(e).__name__}: {e}\n\n{tb.format_exc()}"
        msg += _ns_summary(ns)
        _tlog("analisar_dataframe", "RETORNO", status="ERRO", erro=str(e))
        if _sandbox_mode.get():
            try:
                from db import log_task_code_error
                log_task_code_error(
                    task_id=_sandbox_task_id.get() or "unknown",
                    attempt=0,
                    phase="explore",
                    error=f"{type(e).__name__}: {e}",
                    code=script,
                )
            except Exception:
                pass
        return msg
    finally:
        sys.stdout = old_stdout


@tool
def calcular_periodo(periodo: str) -> str:
    """Converte um período em linguagem natural para from_date e to_date em YYYY-MM-DD.

    OBRIGATÓRIO: chame esta tool ANTES de consultar_analista. O resultado fornece
    os parâmetros from_date e to_date exigidos por consultar_analista — sem eles
    a consulta ao sub-agente analista não pode ser realizada.

    Períodos específicos do domínio (quarters, mês corrente dos dados, rótulos como
    'FQ1') são resolvidos pela seção '## Períodos' do pack de configuração — refletem
    o estado real dos DADOS, não o calendário. Frases sem match caem no calendário.

    Retorna uma string no formato: from=YYYY-MM-DD&to=YYYY-MM-DD

    Args:
        periodo: Descrição do período desejado. Exemplos aceitos:
            'hoje', 'ontem', 'esta semana', 'semana passada',
            'ultimos 7 dias', 'ultimos 14 dias', 'ultimos 30 dias',
            'ultimos 3 meses', 'ultimas 2 semanas',
            'este mes', 'mes passado', 'este quarter', 'proximo quarter', 'ultimo quarter',
            'quarter passado', 'trimestre anterior', 'este ano', 'ano passado', 'ultimo ano',
            'proximo ano', 'todo o periodo', 'historico completo', 'desde o inicio',
            rótulos de período definidos pelo domínio configurado (ex: 'FQ1'),
            'maio de 2026', 'maio/2026', '05/2026' (mês/ano específico),
            '01/05/2026 a 31/05/2026', '2026-05-01 to 2026-05-31' (range explícito).
    """
    import re

    _tlog("calcular_periodo", "CHAMADA", periodo=periodo)
    hoje = datetime.now().date()
    p = periodo.lower().strip()

    # 1º passo: períodos definidos pelo pack de configuração (refletem os dados)
    snap = db_config.resolver_periodo(p)
    if snap:
        result = f"from={snap[0]}&to={snap[1]}"
        _tlog("calcular_periodo", "RETORNO", status="OK", result=result, origem="config")
        return result

    # 2º passo: "todo o período" e sinônimos — o orquestrador é instruído por vários
    # dominio_*.md (inclusive o template) a usar essa frase como padrão quando o usuário
    # não menciona período nenhum. Em domínios com modo: sql e '## Períodos' preenchido,
    # o passo acima já resolveu isso com o range real dos dados. Aqui é o fallback para
    # quando isso não existe (ex: modo: graphql sem database.url) — sem este bloco, a
    # frase caía no "não reconhecido" abaixo e forçava o agente a tentar de novo com um
    # range arbitrário (ex: 'ultimos 12 meses'), o que pode SUBESTIMAR silenciosamente
    # totais de "todo o período" em vez de simplesmente errar de forma visível.
    if re.search(r"todo o per[ií]odo|hist[oó]rico completo|desde o in[ií]cio", p):
        result = f"from=2000-01-01&to={hoje.isoformat()}"
        _tlog("calcular_periodo", "RETORNO", status="OK", result=result, origem="fallback_todo_periodo")
        return result

    if p in ("hoje", "today"):
        frm = to = hoje
    elif p in ("ontem", "yesterday"):
        frm = to = hoje - timedelta(days=1)
    elif p in ("esta semana", "essa semana", "this week"):
        frm = hoje - timedelta(days=hoje.weekday())
        to = hoje
    elif p in ("semana passada", "last week"):
        start = hoje - timedelta(days=hoje.weekday() + 7)
        frm = start
        to = start + timedelta(days=6)
    elif p in ("este mes", "esse mes", "este mês", "esse mês", "this month", "mes corrente", "mês corrente"):
        frm = hoje.replace(day=1)
        to = hoje
    elif p in ("mes passado", "mês passado", "last month"):
        primeiro_deste = hoje.replace(day=1)
        to = primeiro_deste - timedelta(days=1)
        frm = to.replace(day=1)
    elif re.search(r"quarter|trimestre", p):
        # Fallback: quarter fiscal do domínio (pack sem entrada de períodos ou banco
        # indisponível). Mês de início do ano fiscal vem de db_config.mes_inicio_fiscal()
        # (default 1 = janeiro, ou seja trimestre civil) — configurável por domínio em
        # settings.yml (app.mes_inicio_fiscal), sem precisar mexer neste código.
        is_next = bool(re.search(r"pr[oó]xim|seguinte|que vem|vindouro|next", p))
        is_prev = bool(re.search(r"passad|anterior|[uú]ltim|last|previous", p))
        fm = db_config.mes_inicio_fiscal()

        # Quarter fiscal corrente: acha em que ano civil o ano fiscal atual começou,
        # depois quantos meses (0-11) se passaram desde esse início — isso dá o índice
        # do quarter (0-3) e seu mês de início, já no ano civil certo.
        fiscal_start_year = hoje.year if hoje.month >= fm else hoje.year - 1
        pos = (hoje.month - fm) % 12
        q_start_month = fm + (pos // 3) * 3
        q_start_year = fiscal_start_year
        if q_start_month > 12:
            q_start_month -= 12
            q_start_year += 1

        # Desloca para o quarter anterior/próximo, se pedido (aritmética em meses
        # absolutos evita bugs de virada de ano nos dois sentidos).
        shift = 1 if is_next else (-1 if is_prev else 0)
        total_months = (q_start_month - 1) + shift * 3
        q_start_year += total_months // 12
        q_start_month = total_months % 12 + 1

        frm = date(q_start_year, q_start_month, 1)
        end_month, end_year = q_start_month + 3, q_start_year
        if end_month > 12:
            end_month, end_year = end_month - 12, end_year + 1
        to = date(end_year, end_month, 1) - timedelta(days=1)
    elif re.search(r"\bano\b|\byear\b", p):
        # Fallback: ano fiscal do domínio (mesma fonte de mes_inicio_fiscal do quarter
        # acima) — cobre 'este ano', 'ano passado'/'ultimo ano', 'proximo ano'. Sem esta
        # entrada, essas frases caíam no fallback genérico abaixo e retornavam erro
        # ("não reconhecido"), obrigando o agente a tentar de novo com 'ultimos N meses'.
        is_next = bool(re.search(r"pr[oó]xim|seguinte|que vem|vindouro|next", p))
        is_prev = bool(re.search(r"passad|anterior|[uú]ltim|last|previous", p))
        fm = db_config.mes_inicio_fiscal()
        fiscal_start_year = hoje.year if hoje.month >= fm else hoje.year - 1

        if is_prev:
            frm = date(fiscal_start_year - 1, fm, 1)
            to = date(fiscal_start_year, fm, 1) - timedelta(days=1)
        elif is_next:
            frm = date(fiscal_start_year + 1, fm, 1)
            to = date(fiscal_start_year + 2, fm, 1) - timedelta(days=1)
        else:
            frm = date(fiscal_start_year, fm, 1)
            to = hoje
    else:
        import calendar

        # Range explícito: "01/05/2026 a 31/05/2026", "2026-05-01 to 2026-05-31",
        # "de 01/05/2026 até 31/05/2026"
        m_range = re.search(
            r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{1,2}-\d{1,2})\s*(?:a|à|ate|até|to|-)\s*"
            r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{1,2}-\d{1,2})",
            p,
        )
        if m_range:
            from dateutil import parser as _dateparser

            def _parse_data(s: str):
                if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", s):
                    return _dateparser.parse(s, yearfirst=True).date()
                return _dateparser.parse(s, dayfirst=True).date()

            try:
                frm = _parse_data(m_range.group(1))
                to = _parse_data(m_range.group(2))
                result = f"from={frm.isoformat()}&to={to.isoformat()}"
                _tlog("calcular_periodo", "RETORNO", status="OK", result=result)
                return result
            except (ValueError, OverflowError):
                pass

        # Mês/ano específico: "maio de 2026", "maio/2026", "maio 2026", "05/2026"
        _MESES_PT = {
            "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
            "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
            "outubro": 10, "novembro": 11, "dezembro": 12,
        }
        m_mes_ano = re.search(
            r"(" + "|".join(_MESES_PT) + r")\s*(?:de|/|\s)?\s*(\d{4})", p
        )
        if not m_mes_ano:
            m_mes_ano = re.search(r"(?:^|\s)(\d{1,2})/(\d{4})(?:\s|$)", p)

        if m_mes_ano:
            g1 = m_mes_ano.group(1)
            ano = int(m_mes_ano.group(2))
            mes = _MESES_PT[g1] if g1 in _MESES_PT else int(g1)
            if 1 <= mes <= 12:
                frm = date(ano, mes, 1)
                ultimo_dia = calendar.monthrange(ano, mes)[1]
                to = date(ano, mes, ultimo_dia)
                result = f"from={frm.isoformat()}&to={to.isoformat()}"
                _tlog("calcular_periodo", "RETORNO", status="OK", result=result)
                return result

        m_mes = re.search(r"(\d+)\s*(m[eê]s(?:es)?|months?)", p)
        m_sem = re.search(r"(\d+)\s*(semanas?|weeks?)", p)
        m_dia = re.search(r"(\d+)\s*(dias?|days?)", p)
        m = re.search(r"(\d+)", p)
        if m_mes:
            from dateutil.relativedelta import relativedelta
            n = int(m_mes.group(1))
            frm = hoje - relativedelta(months=n)
            to = hoje
        elif m_sem:
            n = int(m_sem.group(1))
            frm = hoje - timedelta(weeks=n)
            to = hoje
        elif m_dia:
            days = int(m_dia.group(1))
            frm = hoje - timedelta(days=days - 1)
            to = hoje
        elif m:
            # Número sem unidade clara: assume dias (compatibilidade)
            days = int(m.group(1))
            frm = hoje - timedelta(days=days - 1)
            to = hoje
        else:
            msg = (
                f"Período '{periodo}' não reconhecido. "
                "Use: 'hoje', 'ontem', 'esta semana', 'semana passada', "
                "'ultimos N dias', 'este mes', 'mes passado', 'este quarter', 'proximo quarter'."
            )
            _tlog("calcular_periodo", "RETORNO", status="ERRO", msg=msg)
            return msg

    result = f"from={frm.isoformat()}&to={to.isoformat()}"
    _tlog("calcular_periodo", "RETORNO", status="OK", result=result)
    return result


# ── Catálogo de skills (injetado no system prompt do sub-agente) ──────────────

def _montar_catalogo_skills() -> str:
    """Lê os headers dos arquivos .md e monta o texto de catálogo para o system prompt.

    Além dos .md em skills/, inclui a skill de domínio do pack de configuração
    (config/dominio.md), exposta como 'dominio.md' via read_skill.
    """
    try:
        arquivos = sorted(f for f in os.listdir(SKILLS_FOLDER) if f.endswith(".md"))
    except FileNotFoundError:
        arquivos = []

    linhas = ["Skills disponíveis (use read_skill para ver as instruções completas):\n"]
    if db_config.is_configured():
        linhas.append(
            f"  Arquivo: dominio.md (JÁ CARREGADA no prompt — não use read_skill)\n"
            f"{db_config.skill_dominio_header().strip()}\n"
        )
    for arq in arquivos:
        caminho = SKILLS_FOLDER / arq
        with open(caminho, encoding="utf-8") as f:
            header = "".join(f.readline() for _ in range(SKILL_HEADER_LINES))
        linhas.append(f"  Arquivo: {arq}\n{header.strip()}\n")

    if len(linhas) == 1:
        return "Nenhuma skill disponível."
    return "\n".join(linhas)


# ── Sub-agente analista ───────────────────────────────────────────────────────

def _build_sub_agent(llm):
    catalogo = _montar_catalogo_skills()

    modo = db_config.modo_acesso()
    tool_dados = executar_sql if modo == "sql" else executar_graphql
    nome_tool_dados = tool_dados.name
    tools_extras = [executar_graphql_agregado, executar_graphql_schema] if modo == "graphql" else []

    hoje = datetime.now().strftime("%d/%m/%Y")
    ident = db_config.identidade()
    regras_dominio = db_config.get_secao("Regras do agente")
    bloco_regras = (
        f"## Regras do domínio (do pack de configuração)\n{regras_dominio}\n\n"
        if regras_dominio else ""
    )
    bloco_schema = (
        "## Schema real — use quando travar\n"
        "A skill 'dominio.md' é a fonte principal de tabelas/colunas, mas pode ficar "
        "desatualizada em relação ao que o backend realmente expõe. Se você travar numa "
        "análise por causa do schema — o servidor rejeitar uma tabela/coluna citada na "
        "skill como inexistente, dúvida sobre o tipo real de uma coluna, ou suspeita de "
        "divergência entre a skill e o servidor — chame `executar_graphql_schema()` "
        "(sem argumentos) para conferir o schema real antes de desistir ou responder com "
        "base em suposição. Não é passo obrigatório do fluxo normal, só um recurso para "
        "destravar quando a skill não é suficiente.\n\n"
        if modo == "graphql" else ""
    )

    SUB_SYSTEM_PROMPT = (
        f"Data de hoje: {hoje}\n\n"
        f"Você é um sub-agente especializado em análise de dados do sistema "
        f"{ident['nome']} — {ident['descricao']}\n\n"
        "## Fluxo OBRIGATÓRIO — sem exceção\n"
        "Para QUALQUER pedido de análise, siga exatamente estes passos:\n\n"
        "  1. ⛔ A skill 'dominio.md' JÁ VEM CARREGADA no fim deste prompt, na seção "
        "'## Skill dominio.md'. NÃO chame read_skill('dominio.md') — só faça isso se essa "
        "seção estiver ausente.\n"
        f"     NUNCA chame {nome_tool_dados} ou analisar_dataframe sem se basear nela.\n"
        "     Motivo: a skill é a única fonte dos nomes reais de tabelas e colunas do banco.\n"
        f"     Sem segui-la, você inventará nomes que não existem e a consulta vai falhar.\n"
        "     Use read_skill apenas para OUTRAS skills do catálogo.\n\n"
        f"  2. Siga o fluxo que a skill define ({nome_tool_dados}).\n\n"
        "  3. Chame analisar_dataframe() para processar os dados e obter o resultado.\n"
        "     - Você pode chamar analisar_dataframe() várias vezes para análises em etapas.\n"
        "     - Variáveis criadas em chamadas anteriores continuam disponíveis.\n"
        "     - Cada chamada custa uma rodada inteira do modelo (segundos): junte cálculo, "
        "ordenação/formatação e a atribuição de `result` num ÚNICO script sempre que possível. "
        "Só divida em etapas se precisar VER o resultado intermediário para decidir o próximo "
        "passo (ex: conferir se há linhas, validar a chave de um merge). NÃO gaste uma chamada "
        "só para 'formatar a tabela final' ou renomear colunas.\n"
        f"     - CONSULTAS INDEPENDENTES EM PARALELO: se precisar de várias consultas de dados "
        f"que NÃO dependem umas das outras (ex: tabelas diferentes, cujos filtros você já "
        f"conhece), emita TODAS as chamadas de {nome_tool_dados} na MESMA resposta, cada uma "
        "com uma `chave` distinta — elas rodam juntas e poupam rodadas. Só faça em sequência "
        "quando uma consulta precisar do RESULTADO da anterior (ex: um ID descoberto). "
        "Vale só para consultas de dados: chamadas de analisar_dataframe são sempre uma por vez.\n\n"
        "  4. Só então redija a resposta final.\n\n"
        "## Modificação de gráfico ou análise já feita\n"
        "Se o usuário pedir para alterar algo em um gráfico ou análise anterior "
        "(cores, tipo de gráfico, estilo, filtro, agrupamento, etc.), "
        "repita o fluxo completo (passos 1-3) usando os MESMOS parâmetros da análise anterior "
        "— você os encontra no histórico da conversa — e aplique a modificação solicitada no script "
        "passado para analisar_dataframe. NUNCA responda que não é possível modificar um gráfico.\n\n"
        f"{bloco_regras}"
        f"{bloco_schema}"
        "## Disciplina de saída — nunca despeje linhas cruas\n"
        "Para perguntas do tipo 'quais KAs/produtos...', SEMPRE agregue "
        "(SELECT DISTINCT ou GROUP BY) — nunca liste a mesma combinação repetida por "
        "mês/semana. Tabelas na resposta final devem ter NO MÁXIMO ~20 linhas; se houver "
        "mais, agregue num nível acima ou mostre o top-N e informe o total restante.\n\n"
        "## Regras\n"
        "- NUNCA invente dados — use apenas resultados de analisar_dataframe.\n"
        "  Isso vale mesmo DENTRO do script passado para analisar_dataframe: nunca escreva um "
        "  script que declare valores fabricados/simulados (ex: `custos = [98.5, 101.2, ...]` "
        "  digitados por você) para preencher um gráfico ou tabela — todo valor exibido tem que "
        "  vir de uma variável populada por executar_sql/executar_graphql. Isso vale MESMO que o "
        "  tipo de saída obrigatório seja GRAFICO e o filtro pedido não tenha retornado nenhuma "
        "  linha real: nesse caso, ignore a obrigatoriedade do gráfico, NÃO simule uma série para "
        "  cumprir o formato, e responda em texto/tabela dizendo que não há dado real para esse "
        "  filtro — mostre o que existe de mais próximo (outro site/produto/período com dado real), "
        "  se ajudar o usuário, mas deixe claro que é um dado diferente do pedido, nunca uma projeção "
        "  ou simulação do que o pedido teria dado.\n"
        "- QUANDO PARAR DE INVESTIGAR UM 'NÃO ENCONTRADO': depois de confirmar por ID (não só por "
        "  nome aproximado) que a combinação pedida (produto/site/período) não existe em NENHUMA "
        "  tabela relevante, isso já é uma conclusão final e completa — pare de investigar. NÃO amplie "
        "  o período 'só para garantir', não repita a mesma busca de outra forma, não busque por nome "
        "  parecido de novo depois de já ter feito isso uma vez. Responda com o que foi encontrado "
        "  (incluindo o dado real mais próximo, se ajudar) e ENCERRE — continuar investigando um "
        "  resultado negativo já bem verificado só consome o orçamento de passos da análise sem "
        "  agregar informação nova, e pode fazer a análise inteira falhar por limite de passos.\n"
        "- Se analisar_dataframe retornar erro, corrija e chame novamente.\n\n"
        "## Disciplina de raciocínio sobre dados — vale para qualquer domínio\n"
        "- RESSALVA TEM QUE BLOQUEAR A CONCLUSÃO, NÃO SÓ ACOMPANHÁ-LA: se uma limitação do "
        "dado invalida uma comparação/número (período incompleto, amostra pequena, grupos "
        "não comparáveis entre si, etc.), a resposta não formula essa comparação/número — "
        "nem como conclusão principal, nem 'com ressalva ao lado'. Mencionar a limitação não "
        "é suficiente se a conclusão problemática ainda aparece na resposta.\n"
        "- NUNCA COMPARE/RANQUEIE DADO DE UM PERÍODO JÁ FECHADO/CONSOLIDADO COM UM PERÍODO "
        "AINDA EM ANDAMENTO/INCOMPLETO como se fossem equivalentes — diga explicitamente que "
        "um deles ainda está em andamento/aberto (evite eufemismos como 'período seguinte'), "
        "e não lidere a resposta com esse tipo de comparação.\n"
        "- NUNCA TRATE UM REGISTRO COM DATA DE REFERÊNCIA NO FUTURO como fato já realizado — "
        "confira a data do registro contra a data de hoje antes de apresentá-lo como resultado "
        "consolidado; se for projeção/planejamento, diga isso explicitamente.\n"
        "- NUNCA INVENTE UMA LINHA/EXPLICAÇÃO AUXILIAR só para fazer dois números baterem "
        "(ex: uma decomposição, um 'desconto', um ajuste que você não verificou) — se não "
        "souber a operação exata que liga dois valores, não a invente; ou você conhece a "
        "fórmula real, ou não a apresenta.\n"
        "- CUIDADO COM 'MÉDIA DAS MÉDIAS': ao combinar métricas JÁ agregadas de subgrupos "
        "(ex: a média de cada site) para obter uma métrica geral, a média simples dessas "
        "médias só bate com a média real se os subgrupos tiverem o MESMO tamanho — raramente "
        "garantido. Para uma média geral correta, prefira recalcular sobre os dados crus "
        "(linha a linha), não sobre agregados intermediários.\n"
        "- SÉRIE TEMPORAL/'EVOLUÇÃO': antes de montar gráfico ou texto de tendência, confirme "
        "que os pontos são da MESMA entidade (não misture produtos/categorias diferentes como "
        "se fosse uma linha do tempo contínua) e que há mais de 1 ponto em período já "
        "fechado/consolidado — com 0-1 ponto real, responda que não há série suficiente em vez "
        "de forçar um gráfico.\n"
        "- VÁRIOS DATAFRAMES (merge/concat): use uma `chave` DISTINTA por consulta — reutilizar "
        "uma chave sobrescreve o DataFrame anterior sem aviso. Se as tabelas estão no mesmo "
        "banco, prefira um JOIN no SQL a trazer duas tabelas e unir em pandas. Ao fazer "
        "`merge`, confira `len()` antes e depois (ou use `validate='m:1'`): chave duplicada "
        "multiplica linhas e infla somas/contagens. Selecione as colunas necessárias ou passe "
        "`suffixes` explícitos para evitar `_x`/`_y`. `concat` só entre DataFrames com o mesmo "
        "significado de colunas.\n\n"
        "- Formate SEMPRE a resposta final em markdown: tabelas para dados tabulares, "
        "**negrito** para valores relevantes.\n"
        "- LEGENDA DOS GRÁFICOS: NUNCA use `bbox_to_anchor` para posicionar a legenda fora "
        "dos eixos — isso gera figuras extremamente largas com a legenda separada do gráfico. "
        "Use sempre `ax.legend(loc='best')` ou outra posição interna (upper right, lower left, etc). "
        "Se o gráfico tiver muitas séries, use `ax.legend(loc='upper right', fontsize=8)` "
        "dentro dos eixos. PROIBIDO: `bbox_to_anchor`, `loc='outside'`, `fig.legend()`.\n"
        "- RÓTULOS DO EIXO X: para rotacionar rótulos use SOMENTE "
        "`plt.setp(ax.get_xticklabels(), rotation=45, ha='right')`. "
        "NUNCA passe `ha=` dentro de `ax.tick_params()` — esse parâmetro não existe lá e causa TypeError.\n"
        "- TIPO DE SAÍDA: a mensagem pode começar com um prefixo `[TIPO DE SAÍDA OBRIGATÓRIO: X]`.\n"
        "  ⚠️ Essa obrigatoriedade de formato NUNCA justifica inventar/simular dados — se a consulta "
        "  não trouxe nenhuma linha real para o filtro pedido, essa regra de formato deixa de valer: "
        "  responda em texto explicando a ausência de dado, não force um gráfico ou tabela fictícia.\n"
        "  - GRAFICO → `result = fig` (figura matplotlib). NUNCA retorne tabela quando tipo=GRAFICO.\n"
        "  - TABELA  → `result = df`. Mencione na resposta final o nome da variável DataFrame\n"
        "    ex: 'DataFrame `producao` disponível no namespace com 30 linhas.'\n"
        "  - AMBOS   → gráfico em uma chamada de analisar_dataframe, tabela em outra.\n"
        "  Se não houver prefixo, padrão é GRAFICO.\n"
        "- PRINT ANTES DO GRÁFICO: no script que gera um gráfico, sempre faça print() dos "
        "valores agregados principais ANTES de atribuir `result = fig`. Exemplo:\n"
        "    print(df_grouped.to_string())  # ou print(f'Total: {total}')\n"
        "    result = fig\n"
        "  Isso garante que os valores reais apareçam no retorno de analisar_dataframe e "
        "possam ser usados no bloco **Dados do gráfico:**.\n"
        "- TOKENS DE GRÁFICO: copie LITERALMENTE o token `[chart:uuid]` retornado por "
        "analisar_dataframe na resposta final. Nunca omita — sem ele o gráfico não aparece.\n"
        "- RESUMO DO GRÁFICO: sempre que incluir um token `[chart:uuid]`, adicione logo abaixo "
        "um bloco **Dados do gráfico:** com 5 a 10 pontos-chave extraídos dos dados reais "
        "(totais, máximos, mínimos, médias, desvios relevantes, top-N). "
        "Exemplo:\n"
        "  **Dados do gráfico:**\n"
        "  - Total no período: 12.400 unidades\n"
        "  - Categoria com maior valor: X (34%)\n"
        "  - Ocorrências críticas: 8\n"
        "  Isso permite ao orquestrador responder follow-ups sem reanalisar os dados.\n"
        "- RESUMO DO RELATÓRIO PDF: sempre que chamar gerar_pdf(), inclua na resposta final "
        "um bloco **Dados do relatório:** com as seções geradas e os valores principais "
        "(você já tem esses dados — usou-os para compor o conteudo do PDF). Exemplo:\n"
        "  **Dados do relatório:**\n"
        "  - Seções: <nomes das seções geradas>\n"
        "  - Período: <período analisado>\n"
        "  - Total: 12.400 | Ocorrências críticas: 8 | Percentual X: 62%\n"
        "  Isso permite ao orquestrador responder follow-ups sobre o PDF sem reabri-lo.\n"
        "- RESUMO DA PLANILHA EXCEL: sempre que chamar gerar_excel(), inclua na resposta final "
        "um bloco **Dados da planilha:** com o shape, as colunas e os valores agregados principais "
        "(você viu esses dados no retorno de analisar_dataframe). Exemplo:\n"
        "  **Dados da planilha:**\n"
        "  - Abas: Dados (1472 linhas × 12 colunas)\n"
        "  - Colunas principais: <colunas reais do DataFrame>\n"
        "  - Total: 12.400 | Ocorrências críticas: 8 | Percentual X: 62%\n"
        "  Isso permite ao orquestrador responder follow-ups sobre a planilha sem reprocessar os dados.\n\n"
        "## Bloco obrigatório: Parâmetros da análise\n"
        "TODA resposta final que tiver consultado dados (via executar_sql/executar_graphql ou "
        "qualquer variante, direto ou dentro de analisar_dataframe) termina com um bloco assim, "
        "logo ANTES de qualquer bloco 'Dados do gráfico/relatório/planilha':\n\n"
        "  **Parâmetros da análise:**\n"
        "  - Período: <from_date> a <to_date> (<rótulo de negócio do período, ex: 'Q1,2026 — "
        "fechado', se o domínio tiver esse conceito>)\n"
        "  - <Dimensão>: <valor> — uma linha para CADA filtro/agrupamento que realmente entrou "
        "no WHERE/GROUP BY da consulta (produto, site, sales model, categoria, status etc. — os "
        "nomes das dimensões variam por domínio, use os reais observados na query)\n\n"
        "Regras deste bloco:\n"
        "- Marque com '(assumido — não pedido explicitamente)' qualquer valor que você mesmo "
        "escolheu por o usuário não ter especificado (ex: período veio de "
        "calcular_periodo('ultimo quarter') porque a pergunta não mencionou data, ou um filtro "
        "veio de um padrão do domínio) — é isso que permite ao usuário identificar rápido se um "
        "default errado explica um resultado estranho.\n"
        "- QUANDO O PERÍODO ASSUMIDO VEIO DE calcular_periodo('ultimo quarter') (domínio não "
        "mencionou data), não basta marcar '(assumido)' — explicite a REGRA por extenso na "
        "própria linha: 'último quarter FECHADO — nunca o quarter em andamento' e nomeie qual é "
        "o quarter atual (em andamento) que ficou de fora por causa dessa regra. Exemplo:\n"
        "  - Período: 2026-04-01 a 2026-06-30 (Q1,2026 — assumido, regra: sempre o último "
        "quarter FECHADO, nunca o em andamento; Q2,2026 está em andamento e não entrou)\n"
        "  Isso vale mesmo que o usuário tenha digitado literalmente 'último quarter' — a regra "
        "do domínio é que essa frase também significa o último FECHADO, não o corrente, e isso "
        "precisa ficar visível na resposta, não só aplicado silenciosamente.\n"
        "- Liste SÓ o que realmente foi usado na query — nunca inclua uma dimensão que não "
        "filtrou nem agrupou nada.\n"
        "- Inclua esse bloco mesmo quando a conclusão for 'não há dado para esse filtro' — "
        "mostre os parâmetros usados na tentativa.\n"
        "- Se analisar_dataframe for chamado mais de uma vez na mesma resposta, não repita o "
        "bloco a cada chamada — só uma vez, no final, refletindo os parâmetros da versão dos "
        "dados que você de fato apresentou.\n\n"
        "## Modo agendamento [PARA_AGENDAMENTO]\n"
        "Se a mensagem contiver [PARA_AGENDAMENTO], após a análise normal escreva um bloco\n"
        "```task_code com o código Python completo pronto para agendamento:\n\n"
        "```task_code\n"
        "def run(from_date, to_date, ctx):\n"
        "    # implementação baseada EXATAMENTE no que você executou acima\n"
        "    # endpoint real, colunas reais, lógica real — sem inventar nada\n"
        "```\n\n"
        "REGRAS CRÍTICAS do bloco task_code:\n"
        "- Baseie-se EXCLUSIVAMENTE na query SQL real, colunas reais e lógica que você observou\n"
        "- NUNCA escreva datas específicas como '2026-05-18' — use from_date/to_date ou ctx.today()\n"
        "- NUNCA inclua linhas de import — pd, np, plt, ctx etc já estão no namespace do sandbox\n"
        "- Retorne via ctx.save_chart(fig), ctx.generate_pdf(titulo, conteudo) ou ctx.generate_excel(df, nome)\n"
        "- NUNCA retorne a figura diretamente — sempre passe por ctx.save_chart(fig)\n"
        "- Busque dados EXCLUSIVAMENTE via ctx.sql() — query SELECT direta no banco configurado\n"
        "  exemplo: rows = ctx.sql(\"SELECT coluna, valor FROM tabela WHERE valor < 10\")\n"
        "  df = pd.DataFrame(rows)\n"
        "- Se a mensagem contiver um bloco de REGRAS DO SANDBOX, siga-as com prioridade máxima\n"
        "- NÃO use df.to_markdown() no conteúdo do PDF — use apenas bullet points e tokens [chart:uuid]\n\n"
        f"## Skills disponíveis\n{catalogo}\n\n"
        "## RACIOCÍNIO OBRIGATÓRIO\n"
        "SEMPRE que for chamar uma tool, inclua na mesma resposta um texto curto explicando "
        "o que vai fazer e por quê, ANTES do bloco de função. "
        "NUNCA emita uma tool call sem texto explicativo."
    )

    sub_tools = [read_skill, tool_dados, *tools_extras, analisar_dataframe, gerar_pdf, gerar_excel]
    llm_sub = llm.bind_tools(sub_tools)
    no_sub_tools = ToolNode(sub_tools)

    def no_sub_agente(state: State) -> dict:
        from langchain_core.messages import AIMessage as _AI
        ns = _ns_get()
        history = ns.get("_script_history", [])
        if history:
            lines = [
                "\n\n## Scripts anteriores desta sessão",
                "Os scripts abaixo foram executados em análises anteriores desta conversa.",
                "Se o pedido atual for uma modificação de um desses gráficos/análises, use o "
                "script correspondente como base — chame apenas analisar_dataframe() com o "
                f"script modificado, sem refazer read_skill nem {nome_tool_dados}.\n",
            ]
            for i, entry in enumerate(history, 1):
                lines.append(f"### Script {i} — pedido: \"{entry['query']}\"")
                lines.append(f"```python\n{entry['script']}\n```")
            extra = "\n".join(lines)
        else:
            extra = ""
        # dominio.md vai no system prompt (não via read_skill) para economizar uma rodada do
        # LLM por análise. Lido a cada chamada: db_config.get_lang() é por-requisição e o
        # arquivo é recarregado por mtime. Fica ANTES de `extra` para manter o prefixo estável.
        dominio_txt = db_config.skill_dominio_texto()
        bloco_dominio = (
            f"\n\n## Skill dominio.md (já carregada — não chame read_skill para ela)\n{dominio_txt}"
            if dominio_txt and db_config.is_configured() else ""
        )
        msgs = [SystemMessage(
            content=_diretiva_idioma() + SUB_SYSTEM_PROMPT + bloco_dominio + extra
        )] + list(state["messages"])
        _tlog("sub_agente", "LLM INVOCADO", mensagens=len(msgs))
        _raise_if_cancelled()
        _t_llm = _time.perf_counter()
        response = llm_sub.invoke(msgs)
        _raise_if_cancelled()

        _ctx_chars = sum(len(str(getattr(m, "content", ""))) for m in msgs)
        _usage = getattr(response, "usage_metadata", None) or {}
        # usage completo (não só input/output): pode incluir output_token_details com os
        # tokens de raciocínio, que explicam passos lentos com pouca saída visível.
        logger.info("[TIMING] sub_agente llm=%.2fs msgs=%d contexto=%d chars usage=%s tool_calls=%s",
                    _time.perf_counter() - _t_llm, len(msgs), _ctx_chars, dict(_usage),
                    [tc.get("name") for tc in (getattr(response, "tool_calls", None) or [])])

        meta = getattr(response, "response_metadata", {})
        finish = meta.get("finish_reason", "")

        # Empurra thinking e tool calls do sub-agente para o side-channel
        session = _current_session.get()
        _emit_agent_events(response, lambda ev: _push_event(session, ev))

        if finish == "MALFORMED_FUNCTION_CALL":
            bad = meta.get("finish_message", "")
            _tlog("sub_agente", "MALFORMED_FUNCTION_CALL detectado", tentativa=bad)
            response = _AI(content=(
                "Erro interno: o modelo tentou computar datas em código Python em vez de "
                "chamar a tool `calcular_periodo`. Use calcular_periodo() para obter as "
                f"datas before/after e depois chame {nome_tool_dados} normalmente.\n\n"
                f"Tentativa inválida capturada:\n```\n{bad}\n```"
            ))

        if os.getenv("DEBUG_TOOLS", "0").strip() == "1":
            if hasattr(response, "tool_calls") and response.tool_calls:
                nomes = [tc["name"] for tc in response.tool_calls]
                logger.info(f"\n[SUB-AGENTE] → tool calls: {nomes}")
            else:
                logger.info(
                    f"\n[SUB-AGENTE] → SEM tool call"
                    f"\n  type       : {type(response).__name__}"
                    f"\n  content    : {repr(response.content)}"
                    f"\n  tool_calls : {getattr(response, 'tool_calls', 'N/A')}"
                    f"\n  add_kwargs : {getattr(response, 'additional_kwargs', {})}"
                    f"\n  response_metadata: {meta}"
                )
        return {"messages": [response]}

    def sub_para_onde(state: State) -> str:
        ultima = state["messages"][-1]
        if hasattr(ultima, "tool_calls") and ultima.tool_calls:
            return "tools"
        return END

    builder = StateGraph(State)
    builder.add_node("sub_agente", no_sub_agente)
    builder.add_node("tools", no_sub_tools)
    builder.add_edge(START, "sub_agente")
    builder.add_conditional_edges("sub_agente", sub_para_onde, {"tools": "tools", END: END})
    builder.add_edge("tools", "sub_agente")

    return builder.compile()


# ── Tool do orquestrador que dispara o sub-agente ────────────────────────────

@tool
def consultar_analista(detalhes: str, from_date: str, to_date: str, tipo: str = "grafico", para_agendamento: bool = False) -> str:
    """Delega a análise ao sub-agente analista de dados.

    O sub-agente irá ler a skill de domínio (dominio.md), consultar o banco
    configurado via SQL e retornar uma análise baseada nos dados reais.

    IMPORTANTE: Sempre chame calcular_periodo() antes para obter from_date e to_date.

    Args:
        detalhes: O que o usuário quer analisar (ex: "total por categoria este mês",
                  "itens em estado crítico", "ranking por volume", "comparação entre períodos").
        from_date: Data inicial no formato YYYY-MM-DD. Obter via calcular_periodo().
        to_date: Data final no formato YYYY-MM-DD. Obter via calcular_periodo().
        tipo: Tipo de saída desejado. Use EXATAMENTE um dos valores abaixo:
              - "grafico"  → o sub-agente DEVE gerar um gráfico matplotlib (result = fig).
                             Use quando o usuário usou palavras como "gráfico", "chart",
                             "plot", "visualize", "visualização", "mostre", "desenhe".
              - "tabela"   → o sub-agente retorna os dados como tabela exibida na tela.
                             Use quando o usuário pediu "tabela", "lista", "dados", "números"
                             ou qualquer exibição tabular na resposta.
                             Também use como pré-passo quando gerar_excel for chamado na sequência.
              - "ambos"    → gráfico E tabela juntos. Use apenas quando o usuário pediu
                             explicitamente visualização gráfica E dados numéricos ao mesmo tempo.
                             NÃO use "ambos" para pedidos de PDF ou Excel.
              Padrão: "grafico". Em caso de dúvida, prefira "grafico".
        para_agendamento: Se True, o sub-agente inclui na resposta um bloco técnico
              ```task_context``` com endpoint real, colunas reais e código pandas executado.
              Use SEMPRE que a análise for para criar ou editar um task_code agendado.
    """
    if _sub_agent_graph is None:
        return "Sub-agente analista não inicializado."
    ns = _ns_get()
    ns["_current_query"] = detalhes
    flags = f"[TIPO DE SAÍDA OBRIGATÓRIO: {tipo.upper()}]\n[PERÍODO: from={from_date} to={to_date}]"

    # Injeta o último script como referência para edições — sem dados, só código
    last_scripts = ns.get("_script_history", [])
    if last_scripts:
        last_script = last_scripts[-1]["script"]
        flags += (
            "\n\n[ÚLTIMO SCRIPT EXECUTADO — use como base se for ajuste sobre análise anterior; "
            "ainda assim re-busque os dados normalmente antes de executar]\n"
            f"```python\n{last_script}\n```"
        )

    if para_agendamento:
        sandbox_skill = _sandbox_skill_texto()
        flags += (
            "\n[PARA_AGENDAMENTO]\n\n"
            "--- REGRAS DO SANDBOX (leia antes de escrever o bloco task_code) ---\n"
            + sandbox_skill
            + "\n--- FIM DAS REGRAS DO SANDBOX ---"
        )
    msg_content = f"{flags}\n{detalhes}"
    try:
        resultado = _sub_agent_graph.invoke(
            {"messages": [HumanMessage(content=msg_content)]},
            # 40, não 30: perguntas que legitimamente precisam checar várias tabelas antes de
            # confirmar uma ausência (ver regra "QUANDO PARAR DE INVESTIGAR...") podem consumir
            # ~30 passos só coletando dado, sem sobrar orçamento pra escrever a conclusão.
            config={"configurable": {"thread_id": _current_session.get()}, "recursion_limit": 40},
        )
    except GraphRecursionError:
        # O sub-agente ficou reverificando/ampliando escopo sem concluir e estourou o limite
        # de passos — sintoma típico de investigar demais um resultado já negativo (ver regra
        # "QUANDO PARAR DE INVESTIGAR..." no prompt). Reformular e chamar de novo tende a cair
        # no mesmo padrão — instrua o orquestrador a NÃO retentar automaticamente.
        return (
            "O sub-agente analista excedeu o limite de passos investigando esta análise sem "
            "concluir (provavelmente por confirmar demais um resultado já negativo). "
            "NÃO chame consultar_analista de novo para a mesma pergunta — isso tende a repetir "
            "o mesmo padrão. Informe ao usuário que a consulta era ampla demais ou que os dados "
            "para esse filtro específico parecem não existir, e pergunte se ele quer restringir "
            "o pedido (produto/site/período mais específico)."
        )
    except RunCancelled:
        return "Análise cancelada pelo usuário. Não tente novamente; aguarde o próximo pedido."
    except Exception as e:
        return (
            f"O sub-agente analista encontrou um erro interno ({type(e).__name__}) e não pôde "
            "completar a análise. Informe o usuário e sugira reformular a pergunta."
        )
    content = resultado["messages"][-1].content
    if not content or not content.strip():
        return (
            "O sub-agente analista não retornou nenhuma resposta. "
            "Isso geralmente indica uma falha interna ao processar a análise. "
            "Informe o usuário e sugira reformular a pergunta."
        )
    return content


@tool
def ver_grafico(chart_id: str) -> list:
    """Visualiza um gráfico já gerado para responder perguntas de follow-up visuais.

    Use quando o usuário perguntar algo sobre um gráfico exibido anteriormente e
    os dados textuais do histórico não forem suficientes para responder
    (ex: 'por que há um pico?', 'onde está a queda?', 'o que mostra essa parte?').

    Args:
        chart_id: UUID do gráfico, extraído do token [chart:UUID] no histórico.
    """
    _tlog("ver_grafico", "CHAMADA", chart_id=chart_id)
    png_b64 = chart_store.get_chart_b64(chart_id)
    if not png_b64:
        _tlog("ver_grafico", "RETORNO", status="NOT_FOUND")
        return [{"type": "text", "text": f"Gráfico {chart_id} não encontrado."}]
    _tlog("ver_grafico", "RETORNO", status="OK", bytes=len(png_b64))
    return [
        {"type": "text", "text": "Imagem do gráfico para análise:"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
    ]


# ── Sub-agente de scheduling ─────────────────────────────────────────────────

def _discover_scheduler_tools() -> list:
    """Retorna todas as LangChain tools definidas em scheduler.tools, em ordem de definição."""
    import inspect
    from langchain_core.tools import BaseTool
    import scheduler.tools as _mod

    tools = []
    for name, obj in inspect.getmembers(_mod):
        if isinstance(obj, BaseTool):
            tools.append(obj)
    return tools


def _describe_scheduler_tools(tools: list) -> str:
    """Gera a seção '## Suas tools' dinamicamente a partir das tools descobertas."""
    lines = []
    for t in tools:
        first_line = (t.description or "").split("\n")[0].strip()
        lines.append(f"- {t.name:<30} → {first_line}")
    return "\n".join(lines)


def _describe_task_context() -> str:
    """Gera documentação do TaskContext dinamicamente a partir das docstrings."""
    from scheduler.context import TaskContext
    import inspect

    skip = {"__init__", "tokens"}
    lines = ["Métodos disponíveis em `ctx` (objeto TaskContext injetado no run()):\n"]

    for name, method in inspect.getmembers(TaskContext, predicate=inspect.isfunction):
        if name.startswith("_") or name in skip:
            continue
        sig = inspect.signature(method)
        params = [
            str(p) for pname, p in sig.parameters.items()
            if pname not in ("self",)
        ]
        doc = (inspect.getdoc(method) or "").split("\n")[0]  # primeira linha da docstring
        lines.append(f"  ctx.{name}({', '.join(params)})")
        lines.append(f"    → {doc}")

    return "\n".join(lines)


def _sandbox_skill_texto() -> str:
    """Regras do sandbox de task_code + modo de acesso a dados + exemplos do pack de configuração."""
    texto = (SKILLS_FOLDER / "task_code_sandbox.md").read_text(encoding="utf-8")
    if db_config.modo_acesso() == "graphql":
        texto += (
            "\n\n> ⚠️ Este domínio está em **modo: graphql** — `ctx.sql()` NÃO existe aqui.\n"
            "> Use `ctx.graphql_table()`/`ctx.graphql_aggregate()` (seção \"Modo graphql\" acima),\n"
            "> SEM o parâmetro `chave` (isso é só das tools do analista, não do ctx).\n"
        )
    else:
        texto += (
            f"\n\n> Dialeto SQL do banco configurado: **{db_config.dialect_name()}** — "
            f"{db_config.dialect_hint()}.\n"
        )
    exemplos = db_config.get_secao("Exemplos task_code")
    if exemplos:
        texto += f"\n## Exemplos do domínio configurado\n\n{exemplos}\n"
    return texto


def _condition_format_texto() -> str:
    """Formato de condition_sql (schedule_monitor) para o modo de acesso a dados do
    domínio ativo — SQL direto ou spec JSON via GraphQL. Ver scheduler/condition.py."""
    from scheduler.condition import describe_condition_format
    return describe_condition_format()


def _build_scheduler_agent(llm):
    sch_tools = _discover_scheduler_tools()

    _sandbox_skill = _sandbox_skill_texto()

    SCH_SYSTEM_PROMPT = (
        "Você é o sub-agente responsável por gerenciar o agendamento de tarefas.\n\n"
        "## Suas tools\n"
        + _describe_scheduler_tools(sch_tools) + "\n\n"
        + "## Sandbox do task_code — leia antes de escrever qualquer código\n"
        + _sandbox_skill + "\n\n"
        "## Execução da tarefa\n"
        "O daemon SEMPRE executa o `task_code` Python diretamente — não há execução via LLM em\n"
        "tempo de agendamento. Uma tarefa só fica ativa e executável depois que task_code é salvo\n"
        "com save_task_code (teste antes com test_task_code). Enquanto não houver task_code, a\n"
        "tarefa fica em 'draft' e o daemon a ignora.\n\n"
        "## Campos importantes\n"
        "- description: texto curto e legível que descreve o que a tarefa faz. "
        "Aparece na listagem para o usuário. Ex: 'Relatório semanal de <métrica> por <dimensão> toda segunda às 8h.'\n"
        "- instructions: passo a passo de referência (não é executado pelo daemon) usado para\n"
        "escrever o task_code. Defina com set_task_instructions se útil, mas isso sozinho não\n"
        "ativa a tarefa.\n"
        "- task_code: código Python com `def run(from_date, to_date, ctx)`. "
        "Obrigatório para a tarefa ficar ativa. Versões anteriores ficam disponíveis para rollback.\n\n"
        "## Formato de condition_sql neste domínio\n"
        + _condition_format_texto() + "\n\n"
        "## O objeto ctx — use SEMPRE estas funções, não reimplemente\n"
        "O código recebe `ctx` com os seguintes métodos. Use-os diretamente:\n\n"
        + _describe_task_context() + "\n\n"
        "  Todos os métodos de geração/salvamento retornam tokens '[tipo:uuid]' e os\n"
        "  acumulam internamente. A função run() pode retorná-los ou deixar o ctx acumulá-los.\n\n"
        "  BIBLIOTECAS DISPONÍVEIS NO NAMESPACE (NÃO use import dentro do run()):\n"
        "  pd, np, plt, sns, mticker, date, datetime, timedelta,\n"
        "  openpyxl, Font, PatternFill, Alignment, Border, Side, get_column_letter\n"
        "  O sandbox bloqueia __import__ — qualquer `import` dentro do run() causa erro.\n\n"
        "## Salvar task_code recebido do orquestrador\n"
        "Quando receber instrução 'save_task_code task [ID]: [código]':\n"
        "1. Chame test_task_code(task_id=ID, code=código) para validar.\n"
        "   - Se retornar erro: analise a mensagem, corrija o código e teste novamente.\n"
        "   - NUNCA salve um código que falhou no teste.\n"
        "2. Somente após teste bem-sucedido, chame save_task_code(task_id=ID, code=código).\n"
        "3. Retorne o resultado de save_task_code literalmente ao orquestrador.\n\n"
        "REGRA CRÍTICA: o código que o orquestrador envia foi construído com dados reais\n"
        "(queries SQL e colunas observadas durante execução interativa). NÃO altere nomes de\n"
        "colunas ou tabelas — corrija apenas erros de sintaxe ou lógica Python.\n\n"
        "## Exemplo de task_code (estrutura — use as tabelas/colunas REAIS da skill de domínio)\n"
        "```python\n"
        "def run(from_date, to_date, ctx):\n"
        "    rows = ctx.sql('''\n"
        "        SELECT dimensao, metrica\n"
        "        FROM tabela_real_do_dominio\n"
        "        WHERE <filtro de período conforme a skill>\n"
        "    ''')\n"
        "    df = pd.DataFrame(rows)\n"
        "    fig, ax = plt.subplots(figsize=(10, 4))\n"
        "    agg = df.groupby('dimensao')['metrica'].mean().reset_index()\n"
        "    ax.bar(agg['dimensao'], agg['metrica'], color='steelblue')\n"
        "    ax.set_title('Métrica média por dimensão')\n"
        "    token_chart = ctx.save_chart(fig)\n"
        "    # NÃO use to_markdown() — causa erro de layout no PDF\n"
        "    # Use apenas bullet points com valores agregados\n"
        "    total = df['dimensao'].nunique()\n"
        "    conteudo = (\n"
        "        f'## Métrica por dimensão\\n\\n{token_chart}\\n\\n'\n"
        "        f'- Período: {from_date} a {to_date}\\n'\n"
        "        f'- Total de itens: {total}\\n'\n"
        "    )\n"
        "    return ctx.generate_pdf('Relatório', conteudo)\n"
        "```\n\n"
        "## Edição de task_code\n"
        "Quando o usuário pedir para alterar qualquer aspecto do relatório (cor, escala, colunas):\n"
        "1. Chame get_task_code para obter o código atual.\n"
        "2. Modifique o código conforme solicitado.\n"
        "3. Chame test_task_code para validar.\n"
        "4. Chame save_task_code para salvar (a versão anterior é arquivada automaticamente).\n\n"
        "## Frequências aceitas\n"
        "once | daily | weekly (requer weekday) | monthly (requer day) |\n"
        "every_Xm (ex: every_2m) | every_Xh (ex: every_6h) | every_Xd (ex: every_3d)\n\n"
        "## Monitores e alertas — usar schedule_monitor, NUNCA schedule_task\n"
        "Se a instrução recebida mencionar alerta/avise/notifique/monitore/threshold/caso X>N:\n"
        "→ A tarefa já foi criada via schedule_monitor com condition_sql/operator/threshold.\n"
        "→ O task_code deve APENAS retornar o valor atual — sem ctx.notify(), sem lógica condicional.\n"
        "→ Exemplo: return f'Valor atual do monitor: {rows[0][\"total\"]}'\n"
        "→ NÃO use ctx.notify() no task_code de monitores — o daemon notifica automaticamente.\n\n"
        "## REGRA CRÍTICA — criar vs editar\n"
        "schedule_task SOMENTE quando o usuário pedir explicitamente para CRIAR uma tarefa nova.\n"
        "Para qualquer outra operação sobre tarefa existente (salvar instructions, editar campos,\n"
        "deletar), use a tool correspondente: set_task_instructions, update_scheduled_task ou\n"
        "delete_scheduled_task. NUNCA chame schedule_task quando receber um task_id existente.\n\n"
        "## Salvar instruções\n"
        "Se a instrução recebida começar com 'set_instructions task [ID]:' ou pedir para\n"
        "salvar/definir instructions de uma tarefa existente: extraia o ID e o texto e chame\n"
        "set_task_instructions(task_id=ID, instructions=texto). Nada mais.\n\n"
        "## Ler instruções\n"
        "Se a instrução recebida começar com 'get_instructions task [ID]': chame\n"
        "get_task_instructions(task_id=ID) e retorne o resultado completo.\n\n"
        "## REGRA DE RESPOSTA — OBRIGATÓRIA\n"
        "Sempre retorne o output LITERAL e COMPLETO da tool na sua resposta final.\n"
        "NUNCA resuma, NUNCA substitua por frases como 'concluído' ou 'listagem feita'.\n"
        "O orquestrador depende do conteúdo exato para repassar ao usuário.\n\n"
        "## RACIOCÍNIO OBRIGATÓRIO\n"
        "SEMPRE que for chamar uma tool, inclua na mesma resposta um texto curto explicando "
        "o que vai fazer e por quê, ANTES do bloco de função. "
        "NUNCA emita uma tool call sem texto explicativo."
    )

    llm_sch = llm.bind_tools(sch_tools)
    no_sch_tools = ToolNode(sch_tools)

    def no_scheduler(state: State) -> dict:
        msgs = [SystemMessage(content=_diretiva_idioma() + SCH_SYSTEM_PROMPT)] + list(state["messages"])
        _raise_if_cancelled()
        response = llm_sch.invoke(msgs)
        _raise_if_cancelled()
        session = _current_session.get()
        _emit_agent_events(response, lambda ev: _push_event(session, ev))
        return {"messages": [response]}

    def sch_para_onde(state: State) -> str:
        ultima = state["messages"][-1]
        if hasattr(ultima, "tool_calls") and ultima.tool_calls:
            return "tools"
        return END

    builder = StateGraph(State)
    builder.add_node("scheduler", no_scheduler)
    builder.add_node("tools", no_sch_tools)
    builder.add_edge(START, "scheduler")
    builder.add_conditional_edges("scheduler", sch_para_onde, {"tools": "tools", END: END})
    builder.add_edge("tools", "scheduler")

    return builder.compile()


def _build_scheduling_graph(llm):
    """Grafo determinístico para criação e edição de task_code agendado.

    Fluxo create: criar_tarefa → consultar_dados → gerar_codigo → testar_codigo ↔ corrigir_codigo → salvar_codigo → responder
    Fluxo edit:   editar_codigo → testar_codigo ↔ corrigir_codigo → salvar_codigo → responder
    """
    import json as _json
    import re as _re_sg

    _MAX_RETRIES = 3
    _sandbox_skill = _sandbox_skill_texto()

    def _extract_code_block(text: str) -> str:
        """Extrai bloco ```task_code``` ou ```python``` que contenha def run."""
        for marker in ("task_code", "python"):
            m = _re_sg.search(rf'```{marker}\n(.*?)```', text, _re_sg.DOTALL)
            if m and "def run" in m.group(1):
                return m.group(1).strip()
        m = _re_sg.search(r'```[^\n]*\n(.*?def run.*?)```', text, _re_sg.DOTALL)
        if m:
            return m.group(1).strip()
        m = _re_sg.search(r'(def run\s*\(from_date.*)', text, _re_sg.DOTALL)
        if m:
            return m.group(1).strip()
        return ""

    def _extract_json_block(text: str, marker: str) -> dict | None:
        """Extrai e faz parse de um bloco ```<marker>\\n{...}\\n``` do texto, ou None."""
        m = _re_sg.search(rf'```{marker}\n(.*?)```', text, _re_sg.DOTALL)
        if not m:
            return None
        try:
            return _json.loads(m.group(1).strip())
        except Exception:
            return None

    def _push(event: dict) -> None:
        _push_event(_current_session.get(), event)

    # ── Node: criar_tarefa ───────────────────────────────────────────────────

    def node_criar_tarefa(state: SchedulingState) -> dict:
        _push({"type": "tool", "name": "schedule_task", "label": _lbl("🗓️ Criando registro da tarefa")})

        from scheduler.condition import describe_condition_format
        condition_format_hint = describe_condition_format()

        param_prompt = (
            "Extraia os parâmetros de agendamento da instrução abaixo. "
            "Responda APENAS com JSON válido, sem explicação nem marcação markdown.\n\n"
            f"Instrução: {state['user_request']}\n\n"
            'Formato: {\n'
            '  "task_type": "monitor" ou "report",\n'
            '  "name": "nome curto (máx 60 chars)",\n'
            '  "description": "descrição clara",\n'
            '  "frequency": "once|daily|weekly|monthly|every_Xm|every_Xh|every_Xd",\n'
            '  "weekday": "monday|tuesday|wednesday|thursday|friday|saturday|sunday ou null",\n'
            '  "day": "número 1-31 para monthly ou null",\n'
            '  "time": "HH:MM ou null",\n'
            '  "date_range": "ytd|mtd|today|last_7d|last_30d|last_90d ou null",\n'
            '  "condition_sql": "condição do monitor — formato depende do domínio, ver regra abaixo — ou null",\n'
            '  "condition_operator": ">= | > | <= | < | == | != | is_empty | is_not_empty ou null",\n'
            '  "condition_threshold": "número ou null"\n'
            '}\n\n'
            "REGRAS:\n"
            "- task_type=monitor se o pedido menciona: alerte/avise/notifique/monitore/caso X>N/threshold\n"
            "- task_type=report para relatórios, gráficos, planilhas recorrentes\n"
            "- monitor: frequency=every_5m por padrão, time=null; preencha condition_sql/operator/threshold\n"
            f"- {condition_format_hint}\n"
            f"- {db_config.get_secao('Dica para condition_sql') or 'consulte o schema em config/dominio.md'}\n"
            "- date_range: preencha quando o pedido especifica período (ano atual→ytd, mês→mtd)\n"
            "- weekly → weekday obrigatório. monthly → day obrigatório.\n"
        )
        _raise_if_cancelled()
        response = llm.invoke([HumanMessage(content=param_prompt)])
        try:
            raw = response.content.strip()
            raw = _re_sg.sub(r'^```[^\n]*\n?', '', raw).rstrip('`').strip()
            params = _json.loads(raw)
        except Exception as e:
            return {"error": f"Erro ao interpretar parâmetros de agendamento: {e}", "task_id": "", "task_name": ""}

        is_monitor = params.get("task_type") == "monitor"
        base_params: dict = {
            "name": params.get("name", "Nova Tarefa"),
            "description": params.get("description", ""),
            "frequency": params.get("frequency", "daily"),
        }
        if params.get("weekday"):
            base_params["weekday"] = params["weekday"]
        if params.get("day") is not None:
            base_params["day"] = str(params["day"])
        if params.get("time"):
            base_params["time"] = params["time"]
        if params.get("date_range"):
            base_params["date_range"] = params["date_range"]

        if is_monitor:
            condition_sql = params.get("condition_sql") or ""
            condition_operator = params.get("condition_operator") or ""
            if not condition_sql or not condition_operator:
                return {"error": "Monitor sem condition_sql ou condition_operator extraídos.", "task_id": "", "task_name": ""}
            base_params["condition_sql"] = condition_sql
            base_params["condition_operator"] = condition_operator
            if params.get("condition_threshold") is not None:
                try:
                    base_params["condition_threshold"] = float(params["condition_threshold"])
                except (TypeError, ValueError):
                    pass
            result = schedule_monitor.invoke(base_params)
        else:
            result = schedule_task.invoke(base_params)

        m = _re_sg.search(r'\[(\d+)\]', result)
        task_id = m.group(1) if m else ""

        if not task_id:
            return {"error": f"Erro ao criar tarefa: {result}", "task_id": "", "task_name": ""}

        # Mantém a tarefa em 'draft' (invisível no painel, não executável) até o
        # pipeline terminar de gerar/testar/salvar o código real em node_salvar_codigo.
        # schedule_monitor cria monitores já 'active' com um task_code padrão (contrato
        # dela quando chamada direto via gerenciar_agenda) — aqui rebaixamos de volta
        # para 'draft' porque o código final ainda vai ser (re)gerado pelos próximos
        # nós. node_responder também depende de status='draft' para poder limpar o
        # registro se o pipeline falhar antes de salvar_codigo.
        from db import get_db as _get_db
        with _get_db() as _conn:
            _conn.execute("UPDATE scheduled_tasks SET status='draft' WHERE id=?", (task_id,))
            _conn.commit()

        return {"task_id": task_id, "task_name": params.get("name", ""), "error": "", "is_monitor": is_monitor}

    # ── Node: consultar_dados ────────────────────────────────────────────────
    # Responsabilidade única: o analista identifica a skill correta, busca dados
    # reais via SQL e devolve um contexto estruturado (skill, query, colunas,
    # amostra). NÃO escreve código — isso fica no nó seguinte.

    def node_consultar_dados(state: SchedulingState) -> dict:
        if _sub_agent_graph is None:
            return {"error": "Sub-agente analista não inicializado.", "analista_context": ""}

        _push({"type": "tool", "name": "consultar_analista", "label": _lbl("🔍 Consultando dados reais")})

        hoje = datetime.now().date().isoformat()
        nome_tool_dados = "executar_sql" if db_config.modo_acesso() == "sql" else "executar_graphql"

        resolucao_entidade = ""
        if state.get("is_monitor") and db_config.modo_acesso() == "graphql":
            resolucao_entidade = (
                f"⚠️ IMPORTANTE — este é um MONITOR em domínio GraphQL: se o pedido do usuário\n"
                f"referencia uma entidade pelo NOME (ex: nome de site, produto, cliente), você DEVE\n"
                f"resolver o ID real dela consultando a tabela de dimensão correspondente ANTES de\n"
                f"montar o filtro final — nunca use o nome digitado como valor de um campo de ID/FK\n"
                f"(ex: `site_id`). Inclua o resultado no campo FILTROS abaixo.\n\n"
            )

        msg_content = (
            f"[PARA_AGENDAMENTO — FASE 1: COLETA DE CONTEXTO]\n"
            f"[PERÍODO: from={hoje}&to={hoje}]\n\n"
            f"Pedido do usuário: {state['user_request']}\n\n"
            f"{resolucao_entidade}"
            f"Sua tarefa nesta fase:\n"
            f"1. Use a skill dominio.md, já carregada no seu prompt (não chame read_skill para ela; "
            f"só use read_skill se precisar de OUTRA skill do catálogo).\n"
            f"2. Chame {nome_tool_dados}() para buscar dados reais do período acima.\n"
            f"3. Chame analisar_dataframe() para observar colunas e valores.\n"
            f"4. Retorne um relatório de contexto com EXATAMENTE este formato:\n\n"
            f"SKILL: <nome do arquivo .md usado>\n"
            f"QUERY: <consulta completa usada na chamada {nome_tool_dados}>\n"
            f"COLUNAS: <lista das colunas relevantes do dataframe>\n"
            f"AMOSTRA: <3-5 linhas representativas do dataframe em formato texto>\n"
            f"FILTROS: <lista JSON de filtros com valores REAIS já resolvidos (IDs, não nomes), "
            f"formato [{{\"field\": \"...\", \"op\": \"EQ\", \"value\": \"...\"}}], ou 'nenhum' se o "
            f"pedido não filtra por entidade nomeada>\n"
            f"TIPO_SAIDA: <PDF_COM_GRAFICO | GRAFICO | EXCEL | MONITOR>\n\n"
            f"⛔ NÃO gere task_code nesta fase.\n"
            f"⛔ NÃO chame gerar_pdf nem gerar_excel.\n"
            f"Apenas colete e reporte o contexto de dados."
        )

        session_id = state["session_id"]
        _sandbox_mode.set(True)
        _sandbox_task_id.set(state.get("task_id", ""))
        try:
            resultado = _sub_agent_graph.invoke(
                {"messages": [HumanMessage(content=msg_content)]},
                config={"configurable": {"thread_id": session_id}, "recursion_limit": 30},
            )
        finally:
            _sandbox_mode.set(False)
            _sandbox_task_id.set("")
        response_text = resultado["messages"][-1].content

        if not response_text or len(response_text.strip()) < 20:
            return {"error": "Analista não retornou contexto de dados.", "analista_context": ""}

        return {"analista_context": response_text, "error": ""}

    # ── Node: gerar_codigo ───────────────────────────────────────────────────
    # Responsabilidade única: dado o contexto do analista, gera o task_code
    # via LLM dedicado com as regras do sandbox como foco total do prompt.

    def node_gerar_codigo(state: SchedulingState) -> dict:
        _push({"type": "tool", "name": "consultar_analista", "label": _lbl("⚙️ Gerando código da tarefa")})

        context = state.get("analista_context", "")

        is_graphql_monitor = state.get("is_monitor") and db_config.modo_acesso() == "graphql"

        if db_config.modo_acesso() == "graphql":
            query_instrucao = (
                "- QUERY acima é a chamada `executar_graphql(...)`/`executar_graphql_agregado(...)` "
                "usada pelo analista — ela tem um parâmetro `chave` que NÃO existe no ctx. Use os "
                "valores REAIS já resolvidos em FILTROS (não nomes digitados pelo usuário) no `where`.\n"
                "- Reescreva a MESMA consulta (mesma `table`, `where`, `group_by`, `metric_field`, "
                "`metric_op`, `expand`) como `ctx.graphql_table(...)` ou `ctx.graphql_aggregate(...)` "
                "— NUNCA use `ctx.sql()` (não existe neste domínio) e NUNCA passe `chave` para o ctx.\n"
            )
        else:
            query_instrucao = (
                "- Use EXATAMENTE a query SQL informada em QUERY, adaptada para `ctx.sql(f'''...''')` "
                "(com from_date e to_date interpolados como parâmetros de data).\n"
            )

        condition_fix_instrucao = ""
        if is_graphql_monitor:
            condition_fix_instrucao = (
                "\n- Esta tarefa é um MONITOR: além do bloco de código, responda também com um "
                "segundo bloco ```conditionjson\\n{...}\\n``` contendo a condition_sql CORRIGIDA "
                "(mesmo `where` que você usou no código, com os valores REAIS de FILTROS — nunca "
                "nomes) no formato: "
                '{"type": "aggregate"|"table", "table": "...", "group_by": "...", '
                '"metric_field": "...", "metric_op": "...", "where": [...]} (campos de "aggregate") '
                'ou {"type": "table", "table": "...", "where": [...], "value_field": "..."}.\n'
            )

        gen_prompt = (
            f"Você é um gerador de task_code Python para tarefas agendadas.\n\n"
            f"## Pedido do usuário\n"
            f"{state['user_request']}\n\n"
            f"## Contexto coletado pelo analista\n"
            f"{context}\n\n"
            f"## Regras obrigatórias do sandbox\n"
            f"{_sandbox_skill}\n\n"
            f"## Instruções\n"
            f"- Escreva a função `def run(from_date, to_date, ctx)` usando a query e colunas acima.\n"
            f"{query_instrucao}"
            f"- Use EXATAMENTE os nomes de colunas informados em COLUNAS.\n"
            f"- Siga o template canônico correspondente ao TIPO_SAIDA:\n"
            f"    PDF_COM_GRAFICO → salve gráfico com ctx.save_chart(fig) e gere PDF com ctx.generate_pdf()\n"
            f"    GRAFICO         → retorne apenas ctx.save_chart(fig)\n"
            f"    EXCEL           → retorne ctx.generate_excel(df, 'nome')\n"
            f"    MONITOR         → NÃO use ctx.notify() (a notificação já é automática pela "
            f"condition_sql da tarefa) — apenas retorne uma string com o valor atual\n"
            f"- NUNCA use import. pd, np, plt, date, datetime, timedelta etc já estão no namespace.\n"
            f"{condition_fix_instrucao}"
            f"- Retorne o bloco Python completo, sem texto explicativo, como ```python\\n...\\n```"
            f"{' seguido do bloco ```conditionjson```, se aplicável.' if is_graphql_monitor else '.'}"
        )

        _raise_if_cancelled()
        response = llm.invoke([HumanMessage(content=gen_prompt)])
        code = _extract_code_block(response.content)
        condition_sql_fix = ""
        if is_graphql_monitor:
            fix_obj = _extract_json_block(response.content, "conditionjson")
            if fix_obj:
                condition_sql_fix = _json.dumps(fix_obj)

        if not code:
            return {
                "error": (
                    "LLM não gerou bloco task_code válido. "
                    f"Resposta (primeiros 400 chars): {response.content[:400]}"
                ),
                "task_code": "",
            }

        return {"task_code": code, "error": "", "condition_sql_fix": condition_sql_fix}

    # ── Node: editar_codigo ──────────────────────────────────────────────────

    def node_editar_codigo(state: SchedulingState) -> dict:
        _push({"type": "tool", "name": "get_task_code", "label": _lbl("🔍 Lendo código atual da tarefa")})

        result = get_task_code.invoke({"task_id": state["task_id"]})
        current_code = _extract_code_block(result)
        if not current_code and "def run" in result:
            current_code = result

        if not current_code:
            # Sem código existente — gera do zero passando pelos dois nós de criação
            _push({"type": "tool", "name": "consultar_analista", "label": _lbl("🔍 Sem código existente — gerando do zero")})
            ctx_result = node_consultar_dados(state)
            if ctx_result.get("error"):
                return ctx_result
            return node_gerar_codigo({**state, **ctx_result})

        _push({"type": "tool", "name": "get_task_code", "label": _lbl("✏️ Aplicando modificação ao código")})

        edit_prompt = (
            f"Modifique o código Python abaixo conforme solicitado.\n\n"
            f"MODIFICAÇÃO: {state['user_request']}\n\n"
            f"CÓDIGO ATUAL:\n```python\n{current_code}\n```\n\n"
            f"REGRAS DO SANDBOX:\n{_sandbox_skill}\n\n"
            f"INSTRUÇÕES:\n"
            f"- Faça APENAS a mudança mínima necessária\n"
            f"- Preserve toda a estrutura, variáveis, endpoints e lógica inalterada\n"
            f"- NÃO inclua import — pd, np, plt etc já estão disponíveis\n"
            f"- Retorne APENAS o código Python completo (com def run), sem texto explicativo\n"
            f"- Responda com bloco ```python\\n...\\n```"
        )

        _raise_if_cancelled()
        response = llm.invoke([HumanMessage(content=edit_prompt)])
        new_code = _extract_code_block(response.content)
        if not new_code:
            return {"error": f"LLM não retornou código válido: {response.content[:300]}", "task_code": current_code}

        return {"task_code": new_code, "error": ""}

    # ── Node: testar_codigo ──────────────────────────────────────────────────

    def node_testar_codigo(state: SchedulingState) -> dict:
        if not state.get("task_code"):
            return {"error": "Nenhum código para testar.", "retries": state.get("retries", 0)}

        _push({"type": "tool", "name": "test_task_code", "label": _lbl("🧪 Testando código")})

        result = test_task_code.invoke({
            "task_id": state["task_id"],
            "code": state["task_code"],
            "session_id_override": state.get("session_id", ""),
        })

        is_error = result.startswith("❌") or "❌" in result[:30]

        if is_error:
            attempt = state.get("retries", 0) + 1
            try:
                from db import log_task_code_error
                phase = "create" if state.get("mode") == "create" else "edit"
                log_task_code_error(
                    task_id=state["task_id"],
                    attempt=attempt,
                    phase=phase,
                    error=result,
                    code=state["task_code"],
                )
            except Exception:
                pass  # auditoria nunca bloqueia o fluxo principal
            return {
                "error": result,
                "retries": attempt,
            }
        return {"error": "", "result": result}

    # ── Node: corrigir_codigo ────────────────────────────────────────────────

    def node_corrigir_codigo(state: SchedulingState) -> dict:
        _push({"type": "tool", "name": "test_task_code", "label": _lbl("🔧 Corrigindo erro no código")})

        error = state["error"]

        # Instrução extra targetada para erros de import bloqueado
        import_match = _re_sg.search(r"Import de '(\w+)' não é permitido", error)
        if not import_match:
            # Também captura "O task_code contém imports, que são bloqueados"
            import_match = _re_sg.search(r"bloqueados no sandbox[:\s]+(.+?)$", error, _re_sg.MULTILINE)
        import_hint = ""
        if import_match:
            blocked = import_match.group(1).strip().split()[0].strip("'\"")
            import_hint = (
                f"\n🚫 ERRO ESPECÍFICO DE IMPORT BLOQUEADO: o módulo '{blocked}' não existe no sandbox.\n"
                f"OBRIGATÓRIO — faça as DUAS coisas:\n"
                f"  1. Remova TODA linha `import {blocked}` (incluindo dentro de def run)\n"
                f"  2. Substitua CADA uso de `{blocked}.X(...)` por equivalente disponível:\n"
                f"     - time.sleep / time.time → desnecessário em tasks, remova\n"
                f"     - time.strftime → use from_date.strftime() ou datetime.now().strftime()\n"
                f"     - math.sqrt/log → use np.sqrt(), np.log()\n"
                f"     - json.dumps/loads → use str() ou dict diretamente\n"
                f"     - re.match/search → reescreva sem regex se possível\n"
                f"     - Para qualquer outro: verifique se pd, np, datetime, timedelta cobrem a necessidade\n"
            )

        fix_prompt = (
            f"O código Python abaixo falhou no teste. Corrija-o.\n\n"
            f"ERRO:\n{error}\n"
            f"{import_hint}\n"
            f"CÓDIGO COM ERRO:\n```python\n{state['task_code']}\n```\n\n"
            f"REGRAS DO SANDBOX:\n{_sandbox_skill}\n\n"
            f"INSTRUÇÕES:\n"
            f"- Corrija APENAS o erro acima, preservando toda a lógica restante\n"
            f"- NÃO inclua NENHUM import — pd, np, plt, date, datetime, timedelta etc já estão disponíveis\n"
            f"- Retorne APENAS o código Python completo (com def run), sem texto explicativo\n"
            f"- Responda com bloco ```python\\n...\\n```"
        )

        _raise_if_cancelled()
        response = llm.invoke([HumanMessage(content=fix_prompt)])
        new_code = _extract_code_block(response.content)
        if not new_code:
            return {}  # keep existing task_code, will fail again on next test

        # Pós-processamento determinístico: remove linhas de import que o LLM teima em gerar
        stripped_lines = [
            ln for ln in new_code.splitlines()
            if not _re_sg.match(r'\s*(import |from \S+ import )', ln)
        ]
        new_code = "\n".join(stripped_lines)

        return {"task_code": new_code}

    # ── Node: salvar_codigo ──────────────────────────────────────────────────

    def node_salvar_codigo(state: SchedulingState) -> dict:
        # Se node_gerar_codigo corrigiu a condition_sql (monitor GraphQL cujo filtro
        # inicial, montado às cegas em node_criar_tarefa, usava o nome digitado pelo
        # usuário em vez do ID real resolvido pelo analista), aplica a correção antes
        # de salvar — senão o monitor fica ativo mas com uma condição que nunca bate.
        condition_sql_fix = state.get("condition_sql_fix") or ""
        if condition_sql_fix:
            from db import get_db as _get_db
            with _get_db() as _conn:
                _conn.execute(
                    "UPDATE scheduled_tasks SET condition_sql=? WHERE id=?",
                    (condition_sql_fix, state["task_id"]),
                )
                _conn.commit()

        _push({"type": "tool", "name": "save_task_code", "label": _lbl("💾 Salvando código validado")})

        result = save_task_code.invoke({
            "task_id": state["task_id"],
            "code": state["task_code"],
        })

        return {"result": result}

    # ── Node: responder ──────────────────────────────────────────────────────

    def node_responder(state: SchedulingState) -> dict:
        tid = state.get("task_id", "?")
        err = state.get("error", "")
        code = state.get("task_code", "")

        if err and not code:
            msg = f"Não foi possível completar a operação: {err}"
        elif err:
            retries = state.get("retries", 0)
            # Tarefa foi criada como draft mas o código falhou — remove o registro
            if state.get("mode") == "create" and tid and tid != "?":
                try:
                    from db import get_db as _get_db
                    with _get_db() as _conn:
                        _conn.execute("DELETE FROM scheduled_tasks WHERE id = ? AND status = 'draft'", (tid,))
                        _conn.commit()
                except Exception:
                    pass
            msg = (
                f"Não foi possível gerar o código da tarefa após {retries} tentativa(s).\n\n"
                f"**Último erro:**\n```\n{err[:500]}\n```\n\n"
                f"Tente novamente ou reformule o pedido com mais detalhes."
            )
        elif state["mode"] == "create":
            msg = (
                f"Tarefa **[{tid}]** criada e agendada com código determinístico.\n\n"
                f"Para ajustar: **editar tarefa {tid}**"
            )
        else:
            msg = (
                f"Tarefa **[{tid}]** atualizada com sucesso.\n\n"
                f"Para novos ajustes: **editar tarefa {tid}**"
            )

        return {"result": msg}

    # ── Routing ──────────────────────────────────────────────────────────────

    def route_entry(state: SchedulingState) -> str:
        return "criar_tarefa" if state["mode"] == "create" else "editar_codigo"

    def route_after_criar(state: SchedulingState) -> str:
        if not state.get("task_id") or state.get("error"):
            return "responder"
        return "consultar_dados"

    def route_after_test(state: SchedulingState) -> str:
        if not state.get("error"):
            return "salvar_codigo"
        if state.get("retries", 0) < _MAX_RETRIES:
            return "corrigir_codigo"
        return "responder"

    # ── Build ────────────────────────────────────────────────────────────────

    def route_after_consultar(state: SchedulingState) -> str:
        if state.get("error"):
            return "responder"
        return "gerar_codigo"

    builder = StateGraph(SchedulingState)
    builder.add_node("criar_tarefa",    node_criar_tarefa)
    builder.add_node("consultar_dados", node_consultar_dados)
    builder.add_node("gerar_codigo",    node_gerar_codigo)
    builder.add_node("editar_codigo",   node_editar_codigo)
    builder.add_node("testar_codigo",   node_testar_codigo)
    builder.add_node("corrigir_codigo", node_corrigir_codigo)
    builder.add_node("salvar_codigo",   node_salvar_codigo)
    builder.add_node("responder",       node_responder)

    builder.add_conditional_edges(START, route_entry, {
        "criar_tarefa": "criar_tarefa",
        "editar_codigo": "editar_codigo",
    })
    builder.add_conditional_edges("criar_tarefa", route_after_criar, {
        "consultar_dados": "consultar_dados",
        "responder": "responder",
    })
    builder.add_conditional_edges("consultar_dados", route_after_consultar, {
        "gerar_codigo": "gerar_codigo",
        "responder": "responder",
    })
    builder.add_edge("gerar_codigo",  "testar_codigo")
    builder.add_edge("editar_codigo", "testar_codigo")
    builder.add_conditional_edges("testar_codigo", route_after_test, {
        "salvar_codigo":  "salvar_codigo",
        "corrigir_codigo": "corrigir_codigo",
        "responder":      "responder",
    })
    builder.add_edge("corrigir_codigo", "testar_codigo")
    builder.add_edge("salvar_codigo",   "responder")
    builder.add_edge("responder",       END)

    return builder.compile()


@tool
def gerenciar_agenda(instrucao: str) -> str:
    """Delega ao sub-agente de scheduling operações sobre tarefas agendadas.

    Use para: criar, listar, editar, deletar tarefas e salvar instruções de execução.

    Exemplos de instruções:
      - "Criar tarefa 'Relatório Semanal de Pedidos', frequência weekly, weekday monday, time 08:00"
      - "Listar todas as tarefas agendadas"
      - "Deletar tarefa 003"
      - "set_instructions task 001: Passo 1 — buscar produção dos últimos 7 dias com
         consultar_analista(periodo='last_7_days'). Passo 2 — gerar PDF com gerar_pdf()."

    Args:
        instrucao: Descrição completa da operação. Para salvar instruções de uma tarefa
                   recém-criada, use o formato 'set_instructions task [ID]: [passos]'.
    """
    if _scheduler_agent_graph is None:
        return "Sub-agente de scheduling não inicializado."
    resultado = _scheduler_agent_graph.invoke(
        {"messages": [HumanMessage(content=instrucao)]},
        config={"configurable": {"thread_id": _current_session.get()}, "recursion_limit": 15},
    )
    return resultado["messages"][-1].content


@tool
def criar_tarefa_agendada(instrucao: str, config: RunnableConfig = None) -> str:
    """Cria uma nova tarefa agendada com código Python determinístico gerado automaticamente.

    Use para CRIAR/AGENDAR uma nova tarefa automática, relatório periódico ou monitor.
    Este tool executa o fluxo completo:
      1. Infere parâmetros (nome, frequência, horário) da instrução
      2. Cria o registro da tarefa no banco
      3. Executa análise prévia para observar os dados reais
      4. Gera o código Python (def run) baseado nos dados reais
      5. Testa o código (até 3 tentativas de correção automática)
      6. Salva o código validado

    Args:
        instrucao: Descrição completa do que a tarefa deve fazer, com frequência e
                   qualquer parâmetro relevante. Exemplos:
                   "Relatório semanal de <métrica> por <dimensão> toda segunda às 8h"
                   "Monitor a cada 5 minutos, alerta se <métrica> passar de 10"
                   "Planilha mensal de <dados> no dia 1 de cada mês"
    """
    if _scheduling_graph is None:
        return "SchedulingGraph não inicializado."

    session_id = ((config or {}).get("configurable") or {}).get("thread_id") or _current_session.get()
    _current_session.set(session_id)  # garante propagação para o thread atual e sub-chamadas
    _push_event(session_id, {"type": "tool", "name": "criar_tarefa_agendada", "label": _lbl("🗓️ Iniciando criação de tarefa agendada")})

    initial_state: SchedulingState = {
        "mode": "create",
        "session_id": session_id,
        "user_request": instrucao,
        "task_id": "",
        "task_name": "",
        "task_code": "",
        "error": "",
        "retries": 0,
        "result": "",
    }

    # Usa stream() em vez de invoke() para conseguir capturar o task_id assim que
    # node_criar_tarefa o gera — se algum nó seguinte (consultar_dados, gerar_codigo,
    # etc.) lançar uma exceção não tratada, invoke() perderia TODO o estado e deixaria
    # a tarefa 'draft' órfã no banco, invisível pra sempre (é isso que estava
    # acontecendo). Com stream(), guardamos o último estado conhecido e limpamos o
    # draft mesmo quando o pipeline quebra de forma inesperada.
    last_state: dict = {}
    try:
        for step_state in _scheduling_graph.stream(initial_state, stream_mode="values"):  # type: ignore[arg-type]
            last_state = step_state
        return last_state.get("result", "Tarefa criada.")
    except Exception as exc:
        if not isinstance(exc, RunCancelled):
            logger.exception("[criar_tarefa_agendada] Falha inesperada no pipeline de agendamento")
        task_id = last_state.get("task_id") or ""
        if task_id:
            try:
                from db import get_db as _get_db
                with _get_db() as _conn:
                    _conn.execute("DELETE FROM scheduled_tasks WHERE id = ? AND status = 'draft'", (task_id,))
                    _conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
                    _conn.execute("DELETE FROM task_code_versions WHERE task_id = ?", (task_id,))
                    _conn.commit()
            except Exception:
                pass
        if isinstance(exc, RunCancelled):
            return "Criação da tarefa cancelada pelo usuário; o rascunho foi descartado."
        return f"Não foi possível criar a tarefa — erro inesperado no pipeline de agendamento: {exc}"


@tool
def editar_tarefa_agendada(task_id: str, modificacao: str, config: RunnableConfig = None) -> str:
    """Edita o código Python de uma tarefa agendada existente.

    Use quando o usuário pedir para EDITAR/ALTERAR/AJUSTAR qualquer aspecto de uma
    tarefa existente: cor, escala, colunas, períodos, threshold, lógica, etc.
    Este tool executa o fluxo completo:
      1. Lê o código atual da tarefa
      2. Aplica a modificação via LLM
      3. Testa o código modificado (até 3 tentativas de correção automática)
      4. Salva o código validado

    Args:
        task_id: ID da tarefa a editar (ex: "001", "42").
        modificacao: O que deve ser alterado. Exemplos:
                     "Mude a cor das barras para laranja"
                     "Adicione linha de meta (target) no gráfico"
                     "Mude o threshold de inconsistências de 50 para 30"
    """
    if _scheduling_graph is None:
        return "SchedulingGraph não inicializado."

    session_id = ((config or {}).get("configurable") or {}).get("thread_id") or _current_session.get()
    _current_session.set(session_id)  # garante propagação para o thread atual e sub-chamadas
    _push_event(session_id, {"type": "tool", "name": "editar_tarefa_agendada", "label": _lbl("✏️ Editando código da tarefa")})

    initial_state: SchedulingState = {
        "mode": "edit",
        "session_id": session_id,
        "user_request": modificacao,
        "task_id": task_id,
        "task_name": "",
        "task_code": "",
        "error": "",
        "retries": 0,
        "result": "",
    }

    resultado = _scheduling_graph.invoke(initial_state)  # type: ignore[arg-type]
    return resultado.get("result", "Tarefa atualizada.")


# ── Orquestrador ──────────────────────────────────────────────────────────────

MAX_INTERACOES = int(os.getenv("MAX_INTERACOES", "10"))


def _build_orchestrator(llm, checkpointer=None):
    ident = db_config.identidade()
    regras_orq = db_config.get_secao("Regras do orquestrador")
    bloco_regras_orq = (
        f"## Regras do domínio (do pack de configuração)\n{regras_orq}\n\n"
        if regras_orq else ""
    )

    ORQ_SYSTEM_PROMPT = (
        f"Você é o agente orquestrador do sistema {ident['nome']} — {ident['descricao']}\n\n"
        f"{bloco_regras_orq}"
        "## HISTÓRICO DE CONVERSA\n"
        "As mensagens acima (antes desta instrução) são o histórico completo desta sessão, "
        "incluindo perguntas e respostas anteriores ao último refresh da página do usuário. "
        "NUNCA diga que não tem memória de conversas anteriores — você tem acesso total ao "
        "histórico desta sessão. Se o usuário perguntar o que perguntou antes, o que foi "
        "analisado, ou pedir para repetir algo, consulte as mensagens anteriores.\n\n"
        "## ROTEAMENTO — leia isto antes de qualquer ação\n"
        "Classifique o pedido do usuário em UMA das categorias abaixo e siga APENAS o fluxo correspondente:\n\n"
        "  A) CONSULTA PONTUAL — o usuário quer ver dados, gráficos, tabelas ou análises AGORA.\n"
        "     Palavras-chave: 'me diga', 'mostre', 'qual é', 'quanto', 'quantos', 'como está',\n"
        "     'gráfico de', 'análise de', 'compare', 'relatório de',\n"
        f"     e termos do domínio: {ident['palavras_chave']}.\n"
        "     → Chame calcular_periodo() + consultar_analista(). NÃO envolva gerenciar_agenda.\n"
        "     REGRA: qualquer pergunta sobre volumes, contagens ou dados do sistema é categoria A.\n"
        "     ⚠️ Se consultar_analista voltar com uma mensagem de erro interno/limite excedido, NÃO "
        "     chame consultar_analista de novo para a mesma pergunta mais de UMA vez — se a segunda "
        "     tentativa também falhar ou vier vaga, repasse a limitação ao usuário (ex: 'não consegui "
        "     concluir essa análise, pode tentar de forma mais específica?') em vez de tentar de novo "
        "     silenciosamente. Retentar sem mudar nada tende a repetir a mesma falha.\n\n"
        "  B) PERGUNTA CONCEITUAL — o usuário quer saber o que algo significa ou como é calculado.\n"
        "     → Chame a tool rag_* mais adequada ao tema da pergunta. Responda diretamente sem buscar dados.\n"
        "       rag_dominio()      → regras de negócio, glossário e conceitos do domínio\n"
        "       rag_capacidades()  → o que o agente pode fazer, agendamento, monitores\n"
        "       rag_arquitetura()  → como o sistema funciona, sub-agentes, skills\n"
        "       rag_dados()        → schema do banco configurado, tabelas e colunas\n\n"
        "  C) AGENDAMENTO — o usuário quer CRIAR, EDITAR, LISTAR, PAUSAR ou DELETAR uma tarefa recorrente.\n"
        "     Palavras-chave obrigatórias: 'agendar', 'todo dia', 'toda semana', 'automaticamente',\n"
        "     'criar tarefa', 'monitor', 'me avise quando', 'editar tarefa [ID]'.\n"
        "     → Siga o fluxo de agendamento detalhado abaixo.\n\n"
        "  D) PERGUNTA SIMPLES — data, hora, saudação, dúvida geral sem dados.\n"
        "     → Responda diretamente sem chamar nenhuma tool de dados.\n\n"
        "  E) PERGUNTA SOBRE O DASHBOARD (texto ou imagem) — o usuário pergunta sobre o que está\n"
        "     visível na tela: valores nos gráficos, KPIs, alertas, notificações.\n"
        "     Palavras-chave: 'na tela', 'no dashboard', 'o que aparece', 'o gráfico\n"
        "     mostra', 'quantos alertas', 'quantas notificações', 'KPI'. Também se enquadra aqui\n"
        "     qualquer mensagem que contenha uma imagem anexada pelo usuário.\n"
        "     → Chame get_dashboard_charts() para obter os dados numéricos do dashboard.\n"
        "     → Se a pergunta envolver o SIGNIFICADO de algo visível (o que é uma métrica\n"
        "       ou termo do domínio), chame rag_dominio() em seguida para complementar.\n"
        "     → NÃO chame consultar_analista — os dados relevantes já estão na tela.\n\n"
        "  F) PAINEL CUSTOMIZADO NO DASHBOARD — o usuário quer ADICIONAR, INSERIR, FIXAR ou REMOVER\n"
        "     um gráfico como painel permanente no dashboard (seção 'Painéis Customizados').\n"
        "     Palavras-chave OBRIGATÓRIAS: 'adiciona ao dashboard', 'insere no dashboard',\n"
        "     'quero ver no dashboard', 'cria um painel com', 'fixar no dashboard',\n"
        "     'coloca no dashboard', 'remove o painel', 'lista os painéis'.\n"
        "     ⚠️ NÃO confunda com pedidos de gráfico no chat (categoria A).\n"
        "     Um gráfico no chat é temporário. Um painel no dashboard é permanente.\n"
        "     → Siga o fluxo de painel customizado detalhado abaixo.\n\n"
        "REGRA CRÍTICA: na dúvida entre A e F, escolha A. NUNCA adicione um painel ao dashboard\n"
        "sem que o usuário tenha usado explicitamente palavras de inserção no dashboard (categoria F).\n\n"
        "## Análise de imagens do dashboard\n"
        "Quando o usuário envia uma imagem junto com sua mensagem, trata-se de um recorte da\n"
        f"própria tela do dashboard — pode conter gráficos, KPIs, alertas ou qualquer outro\n"
        f"elemento visual do sistema {ident['nome']}.\n"
        "Analise visualmente o conteúdo da imagem e:\n"
        "  1. Descreva o que está sendo exibido (tipo de gráfico, eixos, tendências, valores\n"
        "     destacados, anomalias visíveis).\n"
        "  2. Se precisar dos valores numéricos exatos por trás do gráfico, chame\n"
        "     get_dashboard_charts() — ela retorna os dados brutos de todos os gráficos.\n"
        "  3. Se precisar explicar o significado de algo visível (métricas ou termos do\n"
        "     domínio), chame rag_dominio() para buscar a definição precisa.\n"
        "  4. Combine o que você vê na imagem com os dados retornados pelas tools para dar\n"
        "     uma resposta completa e contextualizada.\n\n"
        "## Follow-up sobre gráficos gerados por consultar_analista\n"
        "Quando o usuário fizer uma pergunta de follow-up sobre um gráfico gerado nesta sessão,\n"
        "consulte o histórico: a resposta do sub-agente inclui um bloco **Dados do gráfico:**\n"
        "com os pontos-chave (totais, máximos, mínimos, médias). Use esses dados para responder\n"
        "diretamente — SEM chamar consultar_analista novamente.\n"
        "Se a pergunta for visual ('por que há um pico?', 'onde está a queda?', 'o que mostra\n"
        "o trecho X?') ou se os dados textuais do histórico forem insuficientes, chame\n"
        "ver_grafico(chart_id=UUID) — ela injeta a imagem no contexto para análise visual.\n"
        "Se o usuário pedir uma MODIFICAÇÃO VISUAL do gráfico (cor, paleta, estilo, tipo de "
        "gráfico, labels, título, etc.), chame consultar_analista novamente passando em `detalhes` "
        "o pedido original completo E a modificação solicitada. Exemplo: 'Gráfico de produção "
        "diária agregada por dia — mesmos dados e período, mas com paleta de cores cinza/preto.'\n"
        "O UUID está no histórico no formato [chart:UUID].\n\n"
        "REGRA CRÍTICA: na dúvida entre A e C, escolha A. Nunca inicie um fluxo de agendamento\n"
        "a menos que o usuário tenha usado explicitamente palavras de agendamento (categoria C).\n\n"
        "## Agendamento de tarefas — CRIAR nova tarefa\n"
        "Quando o usuário pedir para criar uma tarefa agendada (relatório periódico, monitor,\n"
        "alerta automático), chame APENAS:\n\n"
        "  criar_tarefa_agendada(instrucao='descrição completa do que a tarefa deve fazer,\n"
        "                                   frequência e qualquer parâmetro relevante')\n\n"
        "O fluxo completo (criar registro, executar análise, gerar código, testar, salvar)\n"
        "é executado automaticamente dentro do tool. NUNCA faça esse fluxo manualmente.\n"
        "NUNCA peça ao usuário nome ou descrição — infira do contexto.\n"
        "Inclua na instrução: o que a tarefa faz, a frequência e (se mencionado) o horário.\n"
        "Exemplos de instrucao:\n"
        "  'Relatório semanal de <métrica> por <dimensão> toda segunda às 8h'\n"
        "  'Monitor a cada 5 minutos, alerta se <métrica> passar de <N>'\n"
        "  'Planilha mensal de <dados> no dia 1 de cada mês'\n\n"
        "Após criar_tarefa_agendada retornar, repasse o resultado ao usuário.\n"
        "NÃO inclua tokens [chart:uuid], [pdf:uuid] ou [excel:uuid] na sua resposta —\n"
        "os artefatos já foram enviados ao chat automaticamente.\n\n"
        "⚠️ Se criar_tarefa_agendada retornar erro ou limitação, NÃO tente contornar chamando\n"
        "rag_capacidades/rag_arquitetura/rag_dados, gerenciar_agenda, schedule_monitor,\n"
        "schedule_task, update_scheduled_task, test_task_code ou save_task_code manualmente —\n"
        "esse fluxo manual é só para LISTAR/PAUSAR/RETOMAR/DELETAR (ver seção abaixo), nunca\n"
        "para criar. Se o usuário pedir para tentar de novo (inclusive após você corrigir algo\n"
        "no sistema), chame criar_tarefa_agendada de novo com a mesma instrução — o pipeline já\n"
        "tenta se corrigir sozinho (até 3 tentativas). Só repasse a limitação ao usuário como veio.\n\n"
        "## Agendamento de tarefas — EDITAR tarefa existente\n"
        "Quando o usuário pedir para editar/ajustar qualquer aspecto de uma tarefa\n"
        "(cor, escala, colunas, threshold, período, lógica), chame APENAS:\n\n"
        "  editar_tarefa_agendada(task_id='ID', modificacao='o que deve mudar')\n\n"
        "O fluxo completo (ler código atual, aplicar mudança, testar, salvar) é automático.\n"
        "NUNCA leia, modifique ou salve o código manualmente para edição.\n"
        "NÃO inclua tokens na sua resposta após a edição — os artefatos já foram enviados.\n\n"
        "## Agendamento de tarefas — operações simples\n"
        "Para LISTAR, PAUSAR, RETOMAR ou DELETAR tarefas: chame gerenciar_agenda(instrucao).\n"
        "Para VER o código atual de uma tarefa: chame get_task_code(task_id=ID).\n\n"
        "## Painéis customizados no dashboard — fluxo obrigatório ao CRIAR\n"
        "Quando o usuário pedir para ADICIONAR/INSERIR/FIXAR um gráfico no dashboard,\n"
        "siga EXATAMENTE estes passos:\n\n"
        "1. Confirme a intenção antes de agir:\n"
        "   Responda: 'Vou criar e fixar um painel [descrição] no dashboard. Confirma?'\n"
        "   Aguarde confirmação. Se o usuário confirmar, prossiga.\n\n"
        "2. Execute consultar_analista() com para_agendamento=True para descobrir\n"
        "   os dados reais (query SQL, colunas, valores). Chame calcular_periodo() antes.\n"
        "   ⛔ OBRIGATÓRIO — nunca escreva código sem ter observado os dados reais.\n\n"
        "3. Escreva o widget_code com base no bloco task_context do passo 2.\n"
        "   REGRAS CRÍTICAS DO WIDGET CODE (viole qualquer uma = erro garantido):\n\n"
        "   ⛔ NUNCA use `import` dentro de run() — o sandbox bloqueia qualquer import.\n"
        "      Variáveis pré-injetadas disponíveis: pd, np, ctx, from_date, to_date,\n"
        "      date, datetime, timedelta. Use-as diretamente.\n\n"
        "   ⛔ ctx.sql() SEMPRE retorna lista Python (list[dict]). NUNCA é um DataFrame.\n"
        "      Converta SEMPRE: `df = pd.DataFrame(ctx.sql('SELECT ...'))`\n"
        "      Acessar colunas antes de converter causa KeyError.\n\n"
        "   ⛔ run() DEVE retornar um dict Chart.js com 'type' e 'data'.\n"
        "      Tipos aceitos: bar, line, pie, doughnut, radar, polarArea.\n"
        "      'data' deve ter: 'labels' (list[str]) e 'datasets' (list[dict]).\n"
        "      Cada dataset: {'label': str, 'data': list[float]}.\n\n"
        "   ⛔ NUNCA escreva datas específicas — use from_date/to_date recebidos como parâmetro.\n"
        "   ⛔ NUNCA recalcule o período dentro do código (ex: date.today() - timedelta(...)).\n"
        "      O período pedido pelo usuário vai em range_unit/range_value no passo 5,\n"
        "      e o dashboard calcula from_date/to_date certos a cada refresh automaticamente.\n\n"
        "   Template COMPLETO e correto (copie e adapte — use tabelas/colunas REAIS observadas\n"
        "   no passo 2, nunca os nomes de exemplo abaixo):\n"
        "   ```python\n"
        "   def run(from_date, to_date, ctx):\n"
        "       rows = ctx.sql('''\n"
        "           SELECT categoria, COUNT(*) AS total\n"
        "           FROM tabela_real GROUP BY categoria\n"
        "       ''')\n"
        "       df = pd.DataFrame(rows)  # OBRIGATÓRIO: converter list→DataFrame\n"
        "       labels = df['categoria'].tolist()\n"
        "       values = df['total'].tolist()\n"
        "       return {\n"
        "           'type': 'pie',\n"
        "           'data': {\n"
        "               'labels': labels,\n"
        "               'datasets': [{'label': 'Total por categoria', 'data': values}]\n"
        "           }\n"
        "       }\n"
        "   ```\n\n"
        "4. Chame test_widget_code(title=TÍTULO, code=CÓDIGO) para validar.\n"
        "   Se retornar erro, leia a mensagem, corrija O ERRO EXATO e teste novamente.\n"
        "   Erros comuns: (a) import bloqueado → remova o import, use a variável pré-injetada;\n"
        "   (b) KeyError/AttributeError → você esqueceu o pd.DataFrame() após ctx.sql().\n"
        "   NUNCA avance com erro.\n\n"
        "5. Chame add_chart_to_dashboard(title=TÍTULO, description=DESCRIÇÃO, code=CÓDIGO,\n"
        "   range_unit=UNIDADE, range_value=VALOR) — declare o período que o usuário pediu\n"
        "   (ex: \"últimos 30 dias\" → range_unit='days', range_value=30; \"últimos 3 meses\"\n"
        "   → range_unit='months', range_value=3). Se o usuário não especificar período,\n"
        "   use o padrão range_unit='days', range_value=7.\n\n"
        "6. Responda: 'Painel **[título]** adicionado ao dashboard. "
        "Ele aparece em primeiro na seção de painéis, e a tela atualiza sozinha.'\n\n"
        "## Painéis customizados — listar, editar e remover\n"
        "- Para LISTAR painéis: chame list_dashboard_widgets().\n"
        "- Para LER o código de um painel: chame get_widget_code(widget_id=ID).\n"
        "- Para EDITAR um painel existente (alterar cores, lógica, título etc.):\n"
        "  1. list_dashboard_widgets → obter o ID\n"
        "  2. get_widget_code → ler o código atual\n"
        "  3. Modificar o código conforme solicitado\n"
        "  4. test_widget_code → validar\n"
        "  5. update_widget(widget_id, code, title?, description?, range_unit?, range_value?) → salvar\n"
        "     Só passe range_unit/range_value se o usuário pediu para MUDAR o período;\n"
        "     caso contrário deixe-os vazios/0 para manter o período atual do painel.\n"
        "  NÃO delete e recrie — use update_widget para editar in-place.\n"
        "- Para REMOVER um painel: chame delete_dashboard_widget(widget_id=ID).\n\n"
        "## RACIOCÍNIO OBRIGATÓRIO\n"
        "SEMPRE que for chamar uma tool, você DEVE incluir na mesma resposta um texto curto "
        "em linguagem natural explicando o que vai fazer e por quê, ANTES do bloco de função. "
        "Exemplo: 'Vou calcular o período solicitado para obter as datas exatas.' "
        "O texto e a chamada de tool devem vir juntos na mesma resposta. "
        "NUNCA emita uma tool call sem texto explicativo."
    )

    orq_tools = [
        get_current_datetime, get_dashboard_charts, calcular_periodo, consultar_analista,
        ver_grafico,
        rag_dominio, rag_capacidades, rag_arquitetura, rag_dados,
        gerar_pdf, gerar_excel,
        gerenciar_agenda,
        criar_tarefa_agendada, editar_tarefa_agendada,
        get_task_code,
        test_widget_code, add_chart_to_dashboard,
        list_dashboard_widgets, delete_dashboard_widget,
        get_widget_code, update_widget,
    ]
    llm_orq = llm.bind_tools(orq_tools)
    no_orq_tools = ToolNode(orq_tools)

    def no_orquestrador(state: State) -> dict:
        historico_recente = state["messages"][-(MAX_INTERACOES * 2):]
        msgs = [SystemMessage(content=_diretiva_idioma() + ORQ_SYSTEM_PROMPT)] + historico_recente
        if _cancelled():
            return _cancelled_reply()
        _t_llm = _time.perf_counter()
        response = llm_orq.invoke(msgs)
        _usage = getattr(response, "usage_metadata", None) or {}
        logger.info("[TIMING] orquestrador llm=%.2fs msgs=%d contexto=%d chars usage=%s tool_calls=%s",
                    _time.perf_counter() - _t_llm, len(msgs),
                    sum(len(str(getattr(m, "content", ""))) for m in msgs), dict(_usage),
                    [tc.get("name") for tc in (getattr(response, "tool_calls", None) or [])])
        # Descarta a resposta (que poderia trazer tool_calls) se o usuário parou durante a chamada.
        if _cancelled():
            return _cancelled_reply()
        return {"messages": [response]}

    def orq_para_onde(state: State) -> str:
        ultima = state["messages"][-1]
        if hasattr(ultima, "tool_calls") and ultima.tool_calls:
            return "tools"
        return END

    builder = StateGraph(State)
    builder.add_node("orquestrador", no_orquestrador)
    builder.add_node("tools", no_orq_tools)
    builder.add_edge(START, "orquestrador")
    builder.add_conditional_edges("orquestrador", orq_para_onde, {"tools": "tools", END: END})
    builder.add_edge("tools", "orquestrador")

    return builder.compile(checkpointer=checkpointer)


# ── API pública ───────────────────────────────────────────────────────────────

def init_multi_agent(project: str, location: str, model_name: str, credentials=None) -> None:
    """Inicializa orquestrador e sub-agente. Chamado no startup do FastAPI."""
    global _orchestrator_graph, _sub_agent_graph, _scheduler_agent_graph, _scheduling_graph, _checkpointer
    try:
        from langchain_google_vertexai import ChatVertexAI
        from langgraph.checkpoint.sqlite import SqliteSaver

        # Temperatura separada por papel — analista/scheduler fazem trabalho mecânico
        # (ler schema, montar filtro/query, gerar código, decidir quando parar de investigar)
        # onde determinismo importa mais que variedade. Temperatura alta nesses papéis já
        # causou, observado em calibração: fabricação de dado/gráfico simulado quando não
        # achava resultado, chamadas GraphQL com JSON malformado, e variação de 13 a 68
        # passos pra responder a MESMA pergunta. O orquestrador lida com tom de conversa,
        # então mantém uma temperatura um pouco maior.
        # Orçamento de raciocínio (thinking) por papel, opcional via env. Os logs [TIMING]
        # mostram que ~metade do tempo de cada chamada é raciocínio (output_token_details.
        # reasoning), até em decisões triviais como escolher a próxima tool. Sem a variável,
        # nada muda (o modelo decide sozinho). 0 desliga o raciocínio; -1 = dinâmico; N = teto
        # em tokens. Testar valores baixos (ex: 128-512) e conferir a qualidade das respostas.
        def _thinking_kwargs(env_var: str) -> dict:
            raw = os.getenv(env_var, "").strip()
            try:
                return {"thinking_budget": int(raw)} if raw else {}
            except ValueError:
                logger.warning("%s inválido (%r) — ignorado", env_var, raw)
                return {}

        llm_analitico = ChatVertexAI(
            model_name=model_name,
            project=project,
            location=location,
            credentials=credentials,
            temperature=0.15,
            **_thinking_kwargs("THINKING_BUDGET_ANALISTA"),
        )
        llm_orquestrador = ChatVertexAI(
            model_name=model_name,
            project=project,
            location=location,
            credentials=credentials,
            temperature=0.4,
            **_thinking_kwargs("THINKING_BUDGET_ORQUESTRADOR"),
        )

        # Conexão persistente para o checkpointer (check_same_thread=False pois
        # o FastAPI despacha requests em threads diferentes)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _checkpointer = SqliteSaver(conn)

        chart_store.init_chart_store(DB_PATH)
        _sub_agent_graph = _build_sub_agent(llm_analitico)
        _scheduler_agent_graph = _build_scheduler_agent(llm_analitico)
        _scheduling_graph = _build_scheduling_graph(llm_analitico)
        _orchestrator_graph = _build_orchestrator(llm_orquestrador, _checkpointer)
        logger.info("Multi-agente inicializado: orquestrador + analista + scheduler + scheduling_graph")
    except Exception:
        import traceback
        logger.warning("Falha ao inicializar multi-agente:\n%s", traceback.format_exc())


def _build_human_message(query: str, session_id: str) -> HumanMessage:
    """Constrói HumanMessage simples ou multimodal se houver imagens no snapshot."""
    with _cs_lock:
        snapshot = _chart_snapshots.get(session_id, {})
    images = snapshot.get("images_b64") or []
    if not images:
        return HumanMessage(content=query)
    parts: list = [{"type": "text", "text": query or "Analise as imagens anexadas."}]
    for b64 in images:
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    return HumanMessage(content=parts)


def invoke_multi_agent(query: str, session_id: str = "default", user_id: str = "", lang: str = "") -> str:
    """Executa o orquestrador com a query do usuário e retorna a resposta final.

    O session_id isola o histórico de conversa — cada sessão tem seu próprio contexto
    persistido no mfg.db via SqliteSaver, e seus DataFrames no store em memória.
    O user_id, quando informado pelo app host, isola painéis, tarefas e artefatos
    (gráficos/PDF/Excel) entre usuários dentro do mesmo domínio. O lang, quando
    informado, decide em que idioma o agente responde e lê conteúdo de domínio —
    ver db_config.set_lang().
    """
    if _orchestrator_graph is None:
        raise RuntimeError("Multi-agente não inicializado.")
    _current_session.set(session_id)
    _current_user_id.set(user_id)
    db_config.set_lang(lang)
    tag_session_dominio(session_id, db_config.dominio_filename())
    resultado = _orchestrator_graph.invoke(
        {"messages": [_build_human_message(query, session_id)]},
        config={"configurable": {"thread_id": session_id}, "recursion_limit": 50},
    )
    return resultado["messages"][-1].content



def stream_multi_agent(query: str, session_id: str = "default", user_id: str = "", lang: str = "",
                       run: "RunHandle | None" = None):
    """Gerador que yields dicts de eventos SSE enquanto o agente processa.

    Tipos de evento:
      {"type": "thinking", "text": "..."}   — bloco de raciocínio do modelo
      {"type": "tool",     "name": "...", "label": "..."}  — tool sendo chamada
      {"type": "reply",    "text": "..."}   — resposta final (markdown)
      {"type": "error",    "text": "..."}   — erro irrecuperável
      {"type": "cancelled"}                 — run interrompido pelo usuário (ver begin_run/cancel_run)

    Arquitetura de threading:
      - O orquestrador roda em uma thread separada e coloca eventos numa queue.Queue.
      - O sub-agente (dentro do ToolNode) também coloca eventos via _push_event na mesma
        queue. Como rodam em threads diferentes, os eventos chegam em tempo real ao gerador.
    """
    if _orchestrator_graph is None:
        yield {"type": "error", "text": "Agente não inicializado."}
        return

    # begin_run já foi chamado pelo endpoint quando ele precisa do handle para cancelar
    # em disconnect; chamadores simples (scripts) deixam a criação para cá.
    if run is None:
        run = begin_run(session_id)

    # Serializa runs da mesma sessão: um run parado ainda pode estar finalizando a chamada
    # de LLM em andamento e gravaria no checkpoint/namespace/queue que este run vai usar.
    if run.prev is not None:
        if not run.prev.done.wait(timeout=_PREV_RUN_WAIT_SECONDS):
            logger.warning("[stream] run anterior da sessão %s não terminou em %ss; seguindo mesmo assim",
                           session_id, _PREV_RUN_WAIT_SECONDS)
        run.prev = None

    if run.cancel.is_set():  # parado antes mesmo de começar (ex: disconnect durante a espera)
        with _runs_lock:
            if _active_runs.get(session_id) is run:
                _active_runs.pop(session_id, None)
        run.done.set()
        yield {"type": "cancelled"}
        return

    _current_session.set(session_id)
    _current_user_id.set(user_id)
    db_config.set_lang(lang)
    tag_session_dominio(session_id, db_config.dominio_filename())

    event_queue: _queue_module.Queue = _queue_module.Queue()
    with _eq_lock:
        _event_queues[session_id] = event_queue

    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 50}

    human_message = _build_human_message(query, session_id)

    def _run_orchestrator():
        # ContextVar não se propaga para threading.Thread — precisa setar explicitamente.
        _current_session.set(session_id)
        _current_user_id.set(user_id)
        _current_run.set(run)
        db_config.set_lang(lang)
        try:
            for chunk in _orchestrator_graph.stream(
                {"messages": [human_message]},
                config=config,
                stream_mode="updates",
            ):
                for node_name, update in chunk.items():
                    messages = update.get("messages", [])
                    if not isinstance(messages, list):
                        messages = [messages]

                    for msg in messages:
                        _emit_agent_events(msg, event_queue.put)

                        if node_name == "orquestrador":
                            tool_calls = getattr(msg, "tool_calls", None) or []
                            fc = getattr(msg, "additional_kwargs", {}).get("function_call")
                            has_tool = bool(tool_calls or fc)
                            text = _extract_text_content(msg)
                            if getattr(msg, "additional_kwargs", {}).get("cancelled"):
                                event_queue.put({"type": "cancelled"})
                            elif text and not has_tool:
                                event_queue.put({"type": "reply", "text": text})

        except RunCancelled:
            event_queue.put({"type": "cancelled"})
        except Exception as e:
            import traceback as _tb
            import sys
            print(f"\n[STREAM ERROR] {e}\n{_tb.format_exc()}", file=sys.stderr, flush=True)
            logger.error("[STREAM ERROR] %s\n%s", e, _tb.format_exc())
            event_queue.put({"type": "error", "text": f"Erro interno: {e}"})
        finally:
            event_queue.put(None)  # sentinel — sinaliza fim do stream
            with _runs_lock:
                if _active_runs.get(session_id) is run:
                    _active_runs.pop(session_id, None)
            run.done.set()

    orchestrator_thread = threading.Thread(target=_run_orchestrator, daemon=True)
    orchestrator_thread.start()

    try:
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield event
    finally:
        with _eq_lock:
            if _event_queues.get(session_id) is event_queue:
                _event_queues.pop(session_id, None)


def is_multi_agent_ready() -> bool:
    return _orchestrator_graph is not None
