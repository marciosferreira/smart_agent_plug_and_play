"""
Smart Agent Plug and Play — Backend FastAPI
============================================
Rodar:
    python -m uvicorn main:app --reload

Docs automáticas:
    http://localhost:8000/docs
"""

import asyncio
import logging
import os
import re
import traceback
from pathlib import Path

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import (
    avaliacoes_da_sessao, buscar_abordagem, get_db, init_db, migrate_db, registrar_avaliacao,
    save_dashboard_widget_order,
)
import db_config

try:
    from dotenv import load_dotenv
    _VERTEX_AVAILABLE = True
except ImportError:
    _VERTEX_AVAILABLE = False

import json
import threading
from sse_starlette.sse import EventSourceResponse
from agent_multi import (
    init_multi_agent, invoke_multi_agent, is_multi_agent_ready, stream_multi_agent,
    begin_run, cancel_run, start_task_creation_chat, start_task_edit_chat,
    set_chart_snapshot, ns_cleanup_loop, _extract_thinking, _extract_text_content, _TOOL_LABELS, _lbl,
)
from scheduler.daemon import scheduler_loop, _execute_task
import chart_store as cs
from typing import Literal

import feedback
import memoria
import memoria_admin

_CHART_RE = re.compile(r'\[chart:([a-f0-9\-]{36})\]')
_PDF_RE   = re.compile(r'\[pdf:([a-f0-9\-]{36})\]')
_EXCEL_RE = re.compile(r'\[excel:([a-f0-9\-]{36})\]')

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")


# Textos que o backend injeta na resposta (alt da imagem, artefato ausente), por idioma.
_ARTIFACT_TEXT = {
    "chart":            {"pt-BR": "gráfico",              "en": "chart",             "zh": "图表"},
    "chart_missing":    {"pt-BR": "[gráfico indisponível]", "en": "[chart unavailable]", "zh": "[图表不可用]"},
    "pdf_missing":      {"pt-BR": "[PDF indisponível]",    "en": "[PDF unavailable]",   "zh": "[PDF不可用]"},
    "excel_missing":    {"pt-BR": "[Excel indisponível]",  "en": "[Excel unavailable]", "zh": "[Excel不可用]"},
}


def _at(key: str) -> str:
    d = _ARTIFACT_TEXT[key]
    return d.get(db_config.get_lang()) or d["pt-BR"]


def _embed_charts(text: str) -> str:
    def _chart(m: re.Match) -> str:
        chart_id = m.group(1)
        if not cs.get_chart_b64(chart_id):
            return _at("chart_missing")
        return f"![{_at('chart')}](/chart/{chart_id})"

    def _pdf(m: re.Match) -> str:
        pdf_id = m.group(1)
        row = cs.get_pdf(pdf_id)
        if not row:
            return _at("pdf_missing")
        _, filename = row
        return f"[📥 {filename}](/pdf/{pdf_id})"

    def _excel(m: re.Match) -> str:
        excel_id = m.group(1)
        row = cs.get_excel(excel_id)
        if not row:
            return _at("excel_missing")
        _, filename = row
        return f"[📥 {filename}](/excel/{excel_id})"

    text = _CHART_RE.sub(_chart, text)
    text = _PDF_RE.sub(_pdf, text)
    text = _EXCEL_RE.sub(_excel, text)
    return text


def _load_credentials():
    """Carrega credenciais Google como objeto, a partir de env var (base64) ou arquivo local."""
    import base64
    import json as _json
    from google.oauth2 import service_account

    SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

    b64 = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if b64:
        info = _json.loads(base64.b64decode(b64))
        logger.info("Credenciais Google carregadas via GOOGLE_CREDENTIALS_JSON")
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    creds_path = Path(__file__).parent / "credentials.json"
    if creds_path.exists():
        logger.info("Credenciais Google carregadas via credentials.json local")
        return service_account.Credentials.from_service_account_file(str(creds_path), scopes=SCOPES)

    return None


def _init_vertex():
    if not _VERTEX_AVAILABLE:
        logger.warning("python-dotenv não instalado — chat IA desabilitado")
        return
    try:
        load_dotenv()
        project    = os.getenv("PROJECT_ID")
        location   = os.getenv("LOCATION", "us-central1")
        model_name = os.getenv("MODEL_NAME", "gemini-3.8-flash")
        if not project:
            logger.warning("PROJECT_ID não definido — chat IA desabilitado")
            return
        credentials = _load_credentials()
        init_multi_agent(project=project, location=location, model_name=model_name, credentials=credentials)
    except Exception:
        logger.warning("Falha ao inicializar Vertex AI:\n%s", traceback.format_exc())

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Smart Agent Plug and Play API", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(memoria_admin.router)


@app.get("/app-info")
def app_info():
    """Identidade do pack de configuração — usada pelo dashboard para branding."""
    return {
        "nome": db_config.app_nome(),
        "descricao": db_config.app_descricao(),
        "configurado": db_config.is_configured(),
    }


def _error_body(status: int, exc_type: str, message: str, path: str) -> dict:
    return {"error": True, "status": status, "type": exc_type, "message": message, "path": path}


@app.exception_handler(Exception)
async def handler_500(request: Request, exc: Exception):
    logger.error("500 %s\n%s", request.url, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content=_error_body(500, type(exc).__name__, str(exc), str(request.url.path)),
    )


@app.exception_handler(404)
async def handler_404(request: Request, _: Exception):
    return JSONResponse(
        status_code=404,
        content=_error_body(404, "NotFound", f"Endpoint não encontrado: {request.url.path}", str(request.url.path)),
    )


@app.exception_handler(422)
async def handler_422(request: Request, exc: Exception):
    logger.warning("422 %s — %s", request.url, exc)
    return JSONResponse(
        status_code=422,
        content=_error_body(422, "ValidationError", str(exc), str(request.url.path)),
    )


@app.on_event("shutdown")
async def shutdown():
    # o worker do ReflectionExecutor (memória do agente) não é daemon: sem isto o processo não sai
    memoria.encerrar()


@app.on_event("startup")
async def startup():
    init_db()
    migrate_db()
    if db_config.is_configured():
        logger.info(
            "Pack de configuração: %s | banco: %s (%s)",
            db_config.app_nome(), db_config.url_mascarada(), db_config.dialect_name(),
        )
    else:
        logger.warning(db_config.MSG_NAO_CONFIGURADO)
    _init_vertex()
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(ns_cleanup_loop())


# ── chat ──────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    user_id: str = ""
    lang: str = ""


class FeedbackRequest(BaseModel):
    session_id: str
    user_id: str = ""
    reply_start: str  # começo normalizado (feedback.inicio_comparavel) da resposta avaliada
    rating: Literal["up", "down"]


class CancelRequest(BaseModel):
    session_id: str = "default"


class StartTaskCreationRequest(BaseModel):
    session_id: str = "default"
    lang: str = ""


class StartTaskEditRequest(BaseModel):
    session_id: str = "default"
    task_id: str
    lang: str = ""


class ChartSnapshotRequest(BaseModel):
    session_id: str
    snapshot: dict


@app.post("/chart-snapshot")
async def chart_snapshot(req: ChartSnapshotRequest):
    """Recebe o snapshot atual dos gráficos do dashboard e armazena por session_id."""
    set_chart_snapshot(req.session_id, req.snapshot)
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest):
    """Envia uma mensagem ao agente LangGraph (Gemini via Vertex AI) e retorna a resposta."""
    if not is_multi_agent_ready():
        return JSONResponse(
            status_code=503,
            content=_error_body(503, "ServiceUnavailable", "Agente IA não configurado. Verifique PROJECT_ID, LOCATION e credentials.json.", "/chat"),
        )
    reply = await asyncio.to_thread(lambda: invoke_multi_agent(req.message, req.session_id, req.user_id, req.lang))
    return {"reply": _embed_charts(reply)}


@app.post("/chat/start-task-creation")
async def start_task_creation(req: StartTaskCreationRequest):
    """Abre uma sessão de chat já com o turno inicial de agendamento (pergunta guia),
    sem chamar o LLM — usado pelo botão "Agendar nova tarefa via chat" do painel de agenda."""
    if not is_multi_agent_ready():
        return JSONResponse(
            status_code=503,
            content=_error_body(503, "ServiceUnavailable", "Agente IA não configurado.", "/chat/start-task-creation"),
        )
    await asyncio.to_thread(start_task_creation_chat, req.session_id, req.lang)
    return {"ok": True}


@app.post("/chat/start-task-edit")
async def start_task_edit(req: StartTaskEditRequest):
    """Abre uma sessão de chat já com o resumo da tarefa e a pergunta guia de edição,
    sem chamar o LLM — usado pelo botão "Editar via chat" do card de tarefa."""
    if not is_multi_agent_ready():
        return JSONResponse(
            status_code=503,
            content=_error_body(503, "ServiceUnavailable", "Agente IA não configurado.", "/chat/start-task-edit"),
        )
    await asyncio.to_thread(start_task_edit_chat, req.session_id, req.task_id, req.lang)
    return {"ok": True}


@app.get("/chart/{chart_id}")
def serve_chart(chart_id: str):
    """Serve um gráfico gerado pelo agente como PNG."""
    b64 = cs.get_chart_b64(chart_id)
    if not b64:
        return JSONResponse(status_code=404, content={"error": True, "message": "Gráfico não encontrado."})
    import base64
    png_bytes = base64.b64decode(b64)
    return Response(content=png_bytes, media_type="image/png")


@app.get("/pdf/{pdf_id}")
def download_pdf(pdf_id: str):
    """Serve um PDF gerado pelo agente para download."""
    row = cs.get_pdf(pdf_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": True, "message": "PDF não encontrado."})
    pdf_bytes, filename = row
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/excel/{excel_id}")
def download_excel(excel_id: str):
    """Serve uma planilha Excel gerada pelo agente para download."""
    row = cs.get_excel(excel_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": True, "message": "Excel não encontrado."})
    excel_bytes, filename = row
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/chat/stream")
async def chat_stream(request: Request, message: str, session_id: str = "default", user_id: str = "", lang: str = ""):
    """Endpoint SSE — transmite thinking, tool calls e resposta final em tempo real."""
    if not is_multi_agent_ready():
        async def _err():
            yield {"data": json.dumps({"type": "error", "text": "Agente IA não configurado."})}
        return EventSourceResponse(_err())

    async def generate():
        import time as _t
        # Relógio de parede (HH:MM:SS.mmm) para comparar com o console do navegador
        # ([chat-timing]) — ambos na mesma máquina.
        _wall = lambda: _t.strftime("%H:%M:%S", _t.localtime()) + f".{int((_t.time() % 1) * 1000):03d}"  # noqa: E731
        logger.info("[TIMING] /chat/stream recebido às %s", _wall())
        _first_sent = False
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        # Registrado aqui (e não dentro da thread) para que o disconnect cancele ESTE run
        # específico — um cancel tardio nunca atinge o run seguinte da mesma sessão.
        run = begin_run(session_id)

        def _run():
            try:
                for event in stream_multi_agent(message, session_id, user_id, lang, run=run):
                    if event.get("type") == "reply":
                        event["text"] = _embed_charts(event["text"])
                    asyncio.run_coroutine_threadsafe(queue.put(event), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    queue.put({"type": "error", "text": str(e)}), loop
                )
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        threading.Thread(target=_run, daemon=True).start()

        finished = False
        try:
            while True:
                if await request.is_disconnected():
                    break
                event = await queue.get()
                if event is None:
                    finished = True
                    break
                if not _first_sent:
                    _first_sent = True
                    logger.info("[TIMING] 1º evento (%s) enviado ao cliente às %s", event.get("type"), _wall())
                yield {"data": json.dumps(event)}
        finally:
            # Cliente sumiu (botão parar, aba fechada, queda de rede): interrompe o agente
            # em vez de deixá-lo gastando tokens até o fim sem ninguém ouvindo.
            if not finished:
                run.cancel.set()

    return EventSourceResponse(generate())


@app.post("/chat/cancel")
async def chat_cancel(req: CancelRequest):
    """Botão "parar": sinaliza cancelamento cooperativo do run em andamento da sessão."""
    return {"cancelled": cancel_run(req.session_id)}


@app.get("/chat/history")
async def chat_history(session_id: str = "default"):
    """Retorna o histórico de mensagens de uma sessão (HumanMessage e AIMessage apenas)."""
    if not is_multi_agent_ready():
        return []
    try:
        from agent_multi import _checkpointer
        if _checkpointer is None:
            return []
        state = _checkpointer.get({"configurable": {"thread_id": session_id}})
        if not state:
            return []
        with get_db() as conn:
            tag = conn.execute(
                "SELECT dominio FROM session_dominio WHERE session_id = ?", (session_id,)
            ).fetchone()
        if tag and tag["dominio"] and tag["dominio"] != db_config.dominio_filename():
            return []
        from langchain_core.messages import HumanMessage, AIMessage
        result = []
        pending_thinking: list[dict] = []
        avaliacoes = avaliacoes_da_sessao(session_id)
        turn_idx = -1            # índice da mensagem do usuário a que a resposta pertence
        usou_analista = False    # a resposta é de dados (o front mostra o botão "resposta correta")
        for msg in state.get("channel_values", {}).get("messages", []):
            if isinstance(msg, HumanMessage):
                turn_idx += 1
                usou_analista = False
                content = msg.content
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
                pending_thinking = []
                result.append({"role": "user", "content": content or ""})
            elif isinstance(msg, AIMessage):
                thought = _extract_thinking(msg)
                if thought:
                    pending_thinking.append({"text": thought, "isTool": False})

                tool_calls = getattr(msg, "tool_calls", None) or []
                if tool_calls:
                    if any(tc.get("name") == "consultar_analista" for tc in tool_calls):
                        usou_analista = True
                    # AIMessage intermediária (chamou tool) — vira etapa de "pensamento",
                    # nunca um balão de resposta próprio (era a causa do bug: cada uma
                    # dessas mensagens virava um balão separado ao recarregar o histórico).
                    pre_text = _extract_text_content(msg)
                    if pre_text:
                        pending_thinking.append({"text": pre_text, "isTool": False})
                    for tc in tool_calls:
                        name = tc.get("name", "")
                        label = _lbl(_TOOL_LABELS.get(name, f"⚙ {name}"))
                        pending_thinking.append({"text": label, "isTool": True})
                    continue

                final_text = _extract_text_content(msg)
                entry: dict = {
                    "role": "assistant", "content": _embed_charts(final_text),
                    "turn": turn_idx, "data": usou_analista, "rating": avaliacoes.get(turn_idx),
                }
                if pending_thinking:
                    entry["thinking"] = pending_thinking
                    pending_thinking = []
                result.append(entry)
        return result
    except Exception:
        return []


@app.post("/chat/feedback")
async def chat_feedback(req: FeedbackRequest):
    """Botões 👍 e 👎 de uma resposta de dados. Toda avaliação é gravada (pergunta, resposta e
    nota; depois de um 👎, também a mensagem seguinte do usuário) para melhorar o agente depois.
    Para a memória, 👍 e 👎 são sinais de reforço (ver memoria.avaliar_resposta)."""
    path = "/chat/feedback"
    if not is_multi_agent_ready():
        return JSONResponse(status_code=503, content=_error_body(503, "ServiceUnavailable", "Agente IA não configurado.", path))
    if not req.user_id or not req.session_id.startswith(f"{req.user_id}_"):
        return JSONResponse(status_code=403, content=_error_body(403, "Forbidden", "Sessão não pertence ao usuário.", path))
    from agent_multi import _checkpointer
    state = _checkpointer.get({"configurable": {"thread_id": req.session_id}}) if _checkpointer else None
    mensagens = (state or {}).get("channel_values", {}).get("messages", [])
    turnos = feedback.montar_turnos(mensagens)
    indice = feedback.localizar_resposta(turnos, req.reply_start)
    if indice is None:
        return JSONResponse(status_code=404, content=_error_body(404, "NotFound", "Resposta não encontrada na sessão.", path))
    with get_db() as conn:
        tag = conn.execute("SELECT dominio FROM session_dominio WHERE session_id = ?", (req.session_id,)).fetchone()
    if tag and tag["dominio"] and tag["dominio"] != db_config.dominio_filename():
        return JSONResponse(status_code=404, content=_error_body(404, "NotFound", "Sessão de outro domínio.", path))
    turno = turnos[indice]

    def abordagem_de(t):
        return buscar_abordagem(req.session_id, feedback.inicio_comparavel(t["resposta"] or ""))

    anterior = registrar_avaliacao(
        req.session_id, indice, req.user_id, db_config.dominio_filename(), req.rating,
        turno["usuario"], turno["resposta"], turno["analista"], abordagem_de(turno),
    )
    tratado = None
    if anterior != req.rating:
        # Para a memória, o clique é um sinal de reforço: marca o turno e reagenda a extração de
        # sempre. Turno que já saiu da memória (muito antigo, ou após um restart) só fica no banco.
        agendada = memoria.avaliar_resposta(
            req.session_id, req.user_id, db_config.dominio_filename(), req.reply_start, req.rating
        )
        tratado = "agendada" if agendada else "so_registrada"
    return {"ok": True, "turn": indice, "rating": req.rating, "anterior": anterior, "memoria": tratado}


@app.get("/chat/sessions")
async def list_chat_sessions(user_id: str = Query(default=None)):
    """Lista todas as sessões de chat de um usuário com título e contagem de mensagens."""
    if not is_multi_agent_ready() or not user_id:
        return []
    try:
        from agent_multi import _checkpointer
        from langchain_core.messages import HumanMessage, AIMessage
        if _checkpointer is None:
            return []
        conn = _checkpointer.conn
        rows = conn.execute(
            "SELECT c.thread_id, MAX(c.checkpoint_id) as latest FROM checkpoints c "
            "LEFT JOIN session_dominio sd ON sd.session_id = c.thread_id "
            "WHERE (c.thread_id = ? OR c.thread_id LIKE ?) AND (sd.dominio IS NULL OR sd.dominio = ?) "
            "GROUP BY c.thread_id ORDER BY latest DESC",
            (user_id, user_id + "_%", db_config.dominio_filename()),
        ).fetchall()
        sessions = []
        for row in rows:
            tid = row[0]
            state = _checkpointer.get({"configurable": {"thread_id": tid}})
            if not state:
                continue
            msgs = state.get("channel_values", {}).get("messages", [])
            human_msgs = [
                m for m in msgs
                if isinstance(m, HumanMessage)
                and not str(getattr(m, "content", "")).startswith("[VALIDAÇÃO")
            ]
            if not human_msgs:
                continue
            _tc = human_msgs[0].content
            if isinstance(_tc, list):
                _tc = " ".join(p.get("text", "") for p in _tc if isinstance(p, dict) and p.get("type") == "text")
            title = str(_tc)[:70].strip() or "(sem título)"
            visible_count = sum(
                1 for m in msgs
                if isinstance(m, (HumanMessage, AIMessage))
                and not getattr(m, "tool_calls", None)
                and not str(getattr(m, "content", "")).startswith("[VALIDAÇÃO")
            )
            sessions.append({
                "session_id": tid,
                "title": title,
                "message_count": visible_count,
            })
        return sessions
    except Exception:
        logger.exception("Erro em /chat/sessions")
        return []


@app.delete("/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str):
    """Deleta uma sessão de chat e todos os artefatos associados."""
    if not is_multi_agent_ready():
        return JSONResponse(status_code=503, content={"error": "Agente IA não configurado"})
    try:
        with get_db() as conn:
            for tbl in ("checkpoints", "writes"):
                try:
                    conn.execute(f"DELETE FROM {tbl} WHERE thread_id = ?", (session_id,))
                except Exception:
                    pass
            conn.execute("DELETE FROM session_dominio WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM feedback_respostas WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM abordagens WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM fichas WHERE session_id = ?", (session_id,))
            conn.commit()
        import chart_store as cs
        cs.delete_charts_for_session(session_id)
        if cs._conn is not None:
            with cs._lock:
                cs._conn.execute("DELETE FROM pdfs WHERE session_id = ?", (session_id,))
                cs._conn.execute("DELETE FROM excels WHERE session_id = ?", (session_id,))
                cs._conn.commit()
        return {"ok": True}
    except Exception:
        logger.exception("Erro em DELETE /chat/sessions/%s", session_id)
        return JSONResponse(status_code=500, content={"error": "Falha ao deletar sessão"})


@app.post("/chat/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """Recebe áudio (webm/opus), transcreve via Gemini (Vertex AI) e retorna texto."""
    if not is_multi_agent_ready():
        return JSONResponse(status_code=503, content={"error": "Agente IA não configurado"})

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return JSONResponse(status_code=500, content={"error": "google-genai SDK não disponível"})

    project    = os.getenv("PROJECT_ID", "")
    location   = os.getenv("LOCATION", "us-central1")
    model_name = os.getenv("MODEL_NAME", "gemini-3.8-flash")

    audio_bytes = await audio.read()
    mime_type   = audio.content_type or "audio/webm"

    try:
        credentials = _load_credentials()
        client = genai.Client(vertexai=True, project=project, location=location, credentials=credentials)
        contents = genai_types.Content(role="user", parts=[
            genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            genai_types.Part.from_text(text="Transcreva literalmente o áudio. Regras: (1) copie palavra por palavra o que foi dito, sem corrigir, resumir, interpretar ou adicionar nada; (2) retorne apenas o texto transcrito, sem introduções, aspas ou comentários; (3) se não houver fala, retorne uma string vazia."),
        ])
        # Transcrição literal não precisa de raciocínio: nível mínimo aceito pelo 3.8 Flash.
        config = genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_level="LOW"),
        )
        response = await asyncio.to_thread(
            lambda: client.models.generate_content(model=model_name, contents=contents, config=config)
        )
        transcript = (getattr(response, "text", None) or "").strip()
    except Exception as e:
        logger.error("Transcrição via Gemini falhou: %s", e)
        return JSONResponse(status_code=502, content={"error": f"Falha na transcrição: {e}"})

    if not transcript:
        return JSONResponse(status_code=422, content={"error": "Não foi possível transcrever o áudio"})

    return {"transcript": transcript}


# ── raiz ──────────────────────────────────────────────────────────────────────

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist" / "frontend" / "browser"


@app.get("/")
def root():
    return FileResponse(FRONTEND_DIST / "index.html", media_type="text/html")


# ── agenda ────────────────────────────────────────────────────────────────────

_DOMINIO_CLAUSE = "(dominio = ? OR dominio IS NULL)"
# user_id=? combinado com user_id IS NULL: quando o request não informa user_id
# (parâmetro None), a comparação "user_id = NULL" nunca é verdadeira em SQL, então
# a cláusula naturalmente restringe a linhas sem dono (compartilhadas), em vez de
# devolver os dados de todos os usuários por omissão.
_USER_CLAUSE = "(user_id = ? OR user_id IS NULL)"


@app.get("/tasks")
def get_tasks(user_id: str = Query(default=None)):
    """Retorna as tarefas agendadas com as últimas 5 execuções de cada uma."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        tasks = conn.execute(
            "SELECT id, name, description, frequency, time, weekday, day, email, notify, "
            "date_range, condition_sql, condition_operator, condition_threshold, condition_state, condition_description, "
            "status, next_run, last_run, created_at, retry_count, max_retries "
            f"FROM scheduled_tasks WHERE {_USER_CLAUSE} AND {_DOMINIO_CLAUSE} ORDER BY id DESC",
            (user_id, dominio),
        ).fetchall()
        task_ids = tuple(dict(t)["id"] for t in tasks)
        runs = (
            conn.execute(
                f"SELECT id, task_id, started_at, ended_at, status, error "
                f"FROM task_runs WHERE task_id IN ({','.join('?'*len(task_ids))}) ORDER BY started_at DESC",
                task_ids,
            ).fetchall()
            if task_ids else []
        )

    runs_by_task: dict = {}
    for r in runs:
        d = dict(r)
        runs_by_task.setdefault(d["task_id"], [])
        if len(runs_by_task[d["task_id"]]) < 5:
            runs_by_task[d["task_id"]].append(d)

    result = []
    for t in tasks:
        row = dict(t)
        row["runs"] = runs_by_task.get(row["id"], [])
        result.append(row)
    return result


@app.get("/tasks/{task_id}/runs")
def get_task_runs(task_id: str, user_id: str = Query(default=None)):
    """Retorna o histórico completo de execuções de uma tarefa."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        exists = conn.execute(
            f"SELECT id FROM scheduled_tasks WHERE id=? AND {_USER_CLAUSE} AND {_DOMINIO_CLAUSE}",
            (task_id, user_id, dominio),
        ).fetchone()
        if not exists:
            return JSONResponse(status_code=404, content={"error": True, "message": f"Task {task_id} não encontrada."})
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at DESC",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/tasks/{task_id}/code-audit")
def get_task_code_audit(task_id: str, user_id: str = Query(default=None)):
    """Retorna histórico de erros de geração/correção de task_code para auditoria."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        exists = conn.execute(
            f"SELECT id FROM scheduled_tasks WHERE id=? AND {_USER_CLAUSE} AND {_DOMINIO_CLAUSE}",
            (task_id, user_id, dominio),
        ).fetchone()
        if not exists:
            return JSONResponse(status_code=404, content={"error": True, "message": f"Task {task_id} não encontrada."})
        rows = conn.execute(
            "SELECT id, task_id, attempt, phase, error, code, created_at "
            "FROM task_code_audit WHERE task_id=? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/tasks/code-audit/summary")
def get_code_audit_summary():
    """Resumo de erros de geração de código do domínio ativo: padrões, módulos bloqueados, frequência."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, task_id, attempt, phase, error, code, created_at "
            f"FROM task_code_audit WHERE {_DOMINIO_CLAUSE} ORDER BY created_at DESC LIMIT 200",
            (dominio,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/tasks/code-audit/{audit_id}")
def delete_code_audit_entry(audit_id: int):
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        cur = conn.execute(
            f"DELETE FROM task_code_audit WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (audit_id, dominio),
        )
        conn.commit()
    if cur.rowcount == 0:
        return JSONResponse(status_code=404, content={"error": "Entrada não encontrada."})
    return {"ok": True}


@app.delete("/tasks/code-audit")
def delete_all_code_audit():
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        conn.execute(f"DELETE FROM task_code_audit WHERE {_DOMINIO_CLAUSE}", (dominio,))
        conn.commit()
    return {"ok": True}


@app.post("/tasks/{task_id}/run-now")
async def run_task_now(task_id: str, user_id: str = Query(default=None)):
    """Dispara execução imediata da task em background. Só funciona se status=active."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM scheduled_tasks WHERE id=? AND {_DOMINIO_CLAUSE} AND {_USER_CLAUSE}",
            (task_id, dominio, user_id),
        ).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": True, "message": f"Task {task_id} não encontrada."})
    task = dict(row)
    if task["status"] != "active":
        return JSONResponse(status_code=409, content={"error": True, "message": f"Task deve estar ativa para execução manual (status atual: {task['status']})."})
    asyncio.create_task(asyncio.to_thread(_execute_task, task, True))
    return {"ok": True, "message": "Execução iniciada."}


@app.post("/tasks/{task_id}/toggle-pause")
def toggle_pause_task(task_id: str, user_id: str = Query(default=None)):
    """Alterna entre pausado e ativo. Não afeta tasks em execução ou concluídas."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id, status, frequency, time, weekday, day, next_run FROM scheduled_tasks WHERE id=? AND {_DOMINIO_CLAUSE} AND {_USER_CLAUSE}",
            (task_id, dominio, user_id),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": True, "message": f"Task {task_id} não encontrada."})
        current = row["status"]
        if current not in ("active", "paused"):
            return JSONResponse(status_code=409, content={"error": True, "message": f"Não é possível pausar/retomar task com status '{current}'."})
        new_status = "paused" if current == "active" else "active"
        if new_status == "active" and row["frequency"] not in ("once", "on_demand"):
            # next_run ficou parado no passado durante a pausa — sem recalcular, a tarefa
            # dispararia imediatamente ao ser retomada em vez de seguir sua agenda.
            from scheduler.md_parser import calculate_next_run
            conn.execute(
                "UPDATE scheduled_tasks SET status=?, next_run=? WHERE id=?",
                (new_status, calculate_next_run(row["frequency"], row["time"], row["weekday"], row["day"]), task_id),
            )
        else:
            conn.execute("UPDATE scheduled_tasks SET status=? WHERE id=?", (new_status, task_id))
        conn.commit()
    return {"ok": True, "task_id": task_id, "status": new_status}


@app.post("/tasks/{task_id}/toggle-notify")
def toggle_notify_task(task_id: str, user_id: str = Query(default=None)):
    """Alterna notificações de uma tarefa (notify 0↔1). Bloqueado para tarefas com condition_sql."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id, notify, condition_sql, condition_description FROM scheduled_tasks WHERE id=? AND {_DOMINIO_CLAUSE} AND {_USER_CLAUSE}",
            (task_id, dominio, user_id),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": True, "message": f"Task {task_id} não encontrada."})
        if row["condition_sql"] or row["condition_description"]:
            return JSONResponse(status_code=409, content={"error": True, "message": "Notificações não podem ser desativadas em tarefas com condição de threshold."})
        new_notify = 0 if row["notify"] else 1
        conn.execute("UPDATE scheduled_tasks SET notify=? WHERE id=?", (new_notify, task_id))
        conn.commit()
    return {"ok": True, "task_id": task_id, "notify": bool(new_notify)}


@app.delete("/tasks/{task_id}")
def delete_task_endpoint(task_id: str, user_id: str = Query(default=None)):
    """Remove uma tarefa do banco pelo ID."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id FROM scheduled_tasks WHERE id=? AND {_DOMINIO_CLAUSE} AND {_USER_CLAUSE}",
            (task_id, dominio, user_id),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": True, "message": f"Task {task_id} não encontrada."})
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
    return {"ok": True, "deleted": task_id}


@app.get("/tasks/{task_id}/code")
def get_task_code(task_id: str, user_id: str = Query(default=None)):
    """Retorna o código atual da tarefa e o histórico de versões anteriores."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id, name, task_code, instructions FROM scheduled_tasks WHERE id=? AND {_DOMINIO_CLAUSE} AND {_USER_CLAUSE}",
            (task_id, dominio, user_id),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": True, "message": f"Task {task_id} não encontrada."})
        versions = conn.execute(
            "SELECT version, code, created_at FROM task_code_versions WHERE task_id=? ORDER BY version DESC",
            (task_id,),
        ).fetchall()
    return {
        "task_id": task_id,
        "name": row["name"],
        "task_code": row["task_code"],
        "instructions": row["instructions"],
        "versions": [dict(v) for v in versions],
    }


@app.get("/artifacts")
def get_artifacts(user_id: str = Query(default=None)):
    """Lista os artefatos (PDFs, gráficos, Excel) do usuário, com origem e data."""
    import re as _re
    artifacts = cs.list_artifacts(user_id)
    for a in artifacts:
        sid = a.get("session_id", "")
        m = _re.match(r'^daemon_(\d+)_', sid)
        if m:
            a["origin"] = "task"
            a["task_id"] = m.group(1)
        else:
            a["origin"] = "chat"
            a["task_id"] = None
    return artifacts


# ── painéis customizados do dashboard ────────────────────────────────────────

_DEFAULT_WIDGET_PREFIX = "default:"


def _default_widget_by_id(widget_id: str, lang: str = "") -> "dict | None":
    if not widget_id.startswith(_DEFAULT_WIDGET_PREFIX):
        return None
    raw_id = widget_id[len(_DEFAULT_WIDGET_PREFIX):]
    for w in db_config.dashboard_default_widgets(lang):
        if w.get("id") == raw_id:
            return w
    return None


def _reconcile_widget_order(widgets: list, saved_ids: list[str]) -> list:
    """Aplica a ordem salva sobre a lista atual de widgets, tolerando divergência.

    IDs salvos que não existem mais (painel removido do arquivo default ou
    apagado do banco) são descartados; widgets atuais que não estavam na ordem
    salva (painel novo — inserido no arquivo default, ou criado via chat depois
    do último reorder) entram no final, na ordem natural (defaults primeiro,
    depois customizados por created_at). Assim o drag-and-drop nunca quebra
    quando o conjunto de widgets muda.
    """
    by_id = {w["id"]: w for w in widgets}
    ordered = [by_id[i] for i in saved_ids if i in by_id]
    seen = {w["id"] for w in ordered}
    rest = [w for w in widgets if w["id"] not in seen]
    return ordered + rest


# painel visível se: dono, legado sem dono (user_id NULL), ou explicitamente compartilhado —
# diferente de _USER_CLAUSE (que também governa scheduled_tasks e não conhece "shared")
_WIDGET_VISIBLE_CLAUSE = "(user_id = ? OR user_id IS NULL OR shared = 1)"


class WidgetOrderRequest(BaseModel):
    user_id: str
    widget_ids: list[str]


class WidgetShareRequest(BaseModel):
    user_id: str
    shared: bool


@app.get("/dashboard-widgets")
def get_dashboard_widgets(user_id: str = Query(default=None), lang: str = Query(default="")):
    """Lista os painéis do dashboard, na ordem salva do usuário: defaults do
    domínio (arquivo de config, sempre presentes) e customizados visíveis —
    do próprio usuário ou compartilhados por outros — criados via chat (banco),
    reconciliados contra dashboard_widget_order."""
    defaults = [
        {
            "id": f"{_DEFAULT_WIDGET_PREFIX}{w['id']}",
            "title": w["title"],
            "description": w.get("description"),
            "created_at": None,
            "range_unit": w.get("range_unit", "days"),
            "range_value": w.get("range_value", 7),
            "is_default": True,
            "shared": False,
            "is_owner": False,
        }
        for w in db_config.dashboard_default_widgets(lang)
    ]

    dominio = db_config.dominio_filename()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, description, created_at, range_unit, range_value, user_id, shared "
            f"FROM dashboard_widgets WHERE {_WIDGET_VISIBLE_CLAUSE} AND {_DOMINIO_CLAUSE} ORDER BY created_at ASC",
            (user_id, dominio),
        ).fetchall()
        customs = [
            {
                "id": r["id"],
                "title": r["title"],
                "description": r["description"],
                "created_at": r["created_at"],
                "range_unit": r["range_unit"],
                "range_value": r["range_value"],
                "is_default": False,
                "shared": bool(r["shared"]),
                "is_owner": r["user_id"] is None or r["user_id"] == user_id,
            }
            for r in rows
        ]

        all_widgets = defaults + customs
        if not user_id:
            return all_widgets

        order_row = conn.execute(
            "SELECT widget_order FROM dashboard_widget_order WHERE user_id = ? AND dominio = ?",
            (user_id, dominio),
        ).fetchone()

    if not order_row:
        return all_widgets
    try:
        saved_ids = json.loads(order_row["widget_order"])
    except (TypeError, ValueError):
        return all_widgets
    return _reconcile_widget_order(all_widgets, saved_ids)


@app.put("/dashboard-widgets/order")
def save_dashboard_widgets_order(req: WidgetOrderRequest):
    """Salva a ordem de exibição dos painéis do dashboard para um usuário."""
    save_dashboard_widget_order(req.user_id, db_config.dominio_filename(), req.widget_ids)
    return {"ok": True}


@app.patch("/dashboard-widgets/{widget_id}/share")
def share_dashboard_widget(widget_id: str, req: WidgetShareRequest):
    """Compartilha (ou revoga o compartilhamento) de um painel customizado com
    todos os usuários do domínio. Só o dono pode alternar — painéis padrão não
    passam por aqui (não têm essa noção, já são visíveis a todos)."""
    dominio = db_config.dominio_filename()
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id, user_id FROM dashboard_widgets WHERE id = ? AND {_DOMINIO_CLAUSE}",
            (widget_id, dominio),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": True, "message": "Painel não encontrado."})
        if row["user_id"] is not None and row["user_id"] != req.user_id:
            return JSONResponse(
                status_code=403,
                content={"error": True, "message": "Só o dono do painel pode alterar o compartilhamento."},
            )
        conn.execute("UPDATE dashboard_widgets SET shared = ? WHERE id = ?", (1 if req.shared else 0, widget_id))
        conn.commit()
    return {"ok": True, "shared": req.shared}


@app.get("/dashboard-widgets/{widget_id}/data")
def get_dashboard_widget_data(widget_id: str, lang: str = Query(default="")):
    """Re-executa o widget_code e retorna a configuração Chart.js atualizada."""
    from scheduler.widget_runner import run_widget_code, compute_widget_range, WidgetCodeError
    from datetime import datetime as _dt

    default = _default_widget_by_id(widget_id, lang)
    if default is not None:
        code, range_unit, range_value = default["code"], default.get("range_unit", "days"), default.get("range_value", 7)
    else:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT code, range_unit, range_value FROM dashboard_widgets WHERE id = ? AND {_DOMINIO_CLAUSE}",
                (widget_id, db_config.dominio_filename()),
            ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": True, "message": "Painel não encontrado."})
        code, range_unit, range_value = row["code"], row["range_unit"], row["range_value"]

    from_date, to_date = compute_widget_range(range_unit, range_value)
    session_id = f"widget_{widget_id[:8]}_{_dt.now().strftime('%Y%m%d%H%M%S')}"
    try:
        config = run_widget_code(code, from_date, to_date, session_id)
    except WidgetCodeError as e:
        return JSONResponse(status_code=422, content={"error": True, "message": str(e)})
    except Exception as e:
        logger.exception("Erro ao executar widget %s", widget_id)
        return JSONResponse(status_code=500, content={"error": True, "message": str(e)})

    return config


@app.delete("/dashboard-widgets/{widget_id}")
def delete_dashboard_widget_endpoint(widget_id: str, user_id: str = Query(default=None)):
    """Remove um painel customizado do dashboard."""
    if widget_id.startswith(_DEFAULT_WIDGET_PREFIX):
        return JSONResponse(
            status_code=400,
            content={"error": True, "message": "Painéis padrão do domínio não podem ser removidos."},
        )
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id FROM dashboard_widgets WHERE id = ? AND {_DOMINIO_CLAUSE} AND {_USER_CLAUSE}",
            (widget_id, db_config.dominio_filename(), user_id),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": True, "message": "Painel não encontrado."})
        conn.execute("DELETE FROM dashboard_widgets WHERE id = ?", (widget_id,))
        conn.commit()
    return {"ok": True, "deleted": widget_id}


@app.delete("/artifacts/{artifact_type}/{artifact_id}")
def delete_artifact(artifact_type: str, artifact_id: str, user_id: str = Query(default=None)):
    """Remove um artefato do banco pelo tipo e ID."""
    ok = cs.delete_artifact(artifact_type, artifact_id, user_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": True, "message": "Artefato não encontrado."})
    return {"ok": True}


@app.get("/reports/{task_id}")
def get_reports_for_task(task_id: str):
    """Lista os relatórios gerados para uma task, mais recente primeiro."""
    reports_dir = Path(__file__).parent / "reports"
    if not reports_dir.exists():
        return []
    results = []
    for folder in sorted(reports_dir.iterdir(), reverse=True):
        if not folder.is_dir() or not folder.name.startswith(f"{task_id}_"):
            continue
        meta_file = folder / "metadata.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        results.append({
            "folder": folder.name,
            "run_at": meta.get("run_at"),
            "status": meta.get("status"),
            "pdf_urls": meta.get("pdf_urls", []),
        })
    return results


# ── alertas ───────────────────────────────────────────────────────────────────

@app.get("/alerts")
def get_alerts(all: bool = False, user_id: str = Query(default=None)):
    """Retorna alertas de threshold. Por padrão apenas não lidos; ?all=true retorna os últimos 100."""
    alerts = cs.get_alerts(unread_only=not all, user_id=user_id)
    task_ids = list({a["task_id"] for a in alerts if a.get("task_id")})
    if task_ids:
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT id, name FROM scheduled_tasks WHERE id IN ({','.join('?'*len(task_ids))})",
                task_ids,
            ).fetchall()
        names = {r["id"]: r["name"] for r in rows}
        for a in alerts:
            a["task_name"] = names.get(a.get("task_id") or "")
    return alerts


@app.post("/alerts/{alert_id}/read")
def mark_alert_read(alert_id: str, user_id: str = Query(default=None)):
    """Marca um alerta como lido."""
    ok = cs.mark_alert_read(alert_id, user_id=user_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": True, "message": "Alerta não encontrado."})
    return {"ok": True}


@app.post("/alerts/read-all")
def mark_all_alerts_read(user_id: str = Query(default=None)):
    """Marca todos os alertas não lidos como lidos."""
    count = cs.mark_all_alerts_read(user_id=user_id)
    return {"ok": True, "marked": count}


@app.delete("/alerts")
def delete_all_alerts(user_id: str = Query(default=None)):
    """Apaga todos os alertas permanentemente."""
    count = cs.delete_all_alerts(user_id=user_id)
    return {"ok": True, "deleted": count}


@app.delete("/alerts/{alert_id}")
def delete_alert(alert_id: str, user_id: str = Query(default=None)):
    """Apaga permanentemente um alerta."""
    ok = cs.delete_alert(alert_id, user_id=user_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": True, "message": "Alerta não encontrado."})
    return {"ok": True}


# ── assets do build Angular ─────────────────────────────────────────────────
# Registrado por último para que nenhuma rota de API acima seja sombreada —
# o Starlette resolve rotas explícitas antes de considerar um mount.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST), name="frontend")
else:
    logger.warning("Build do Angular não encontrado em %s — rode 'npm run build' em frontend/.", FRONTEND_DIST)


