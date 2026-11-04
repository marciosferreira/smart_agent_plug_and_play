"""
Painel de inspeção da memória dos usuários (página /memoria).

O perfil, as preferências e as convenções podem ser editados (cada documento é regravado
inteiro; vazio continua no banco, e um que ainda não existe é criado ao salvar); as fichas de
análise podem ser apagadas. Lê direto das tabelas `store` (documentos do LangGraph, ver memoria.py) e
`fichas` (ver fichas.py) do mfg.db, sem depender do agente estar inicializado.

  GET    /memoria                       a página
  GET    /memoria/api/usuarios          usuários que têm algo na memória
  GET    /memoria/api/usuarios/{id}     perfil, preferências, convenções e fichas do usuário
  GET    /memoria/api/usuarios/{id}/prompts  o último prompt do orquestrador e do analista
  GET    /memoria/api/dominios          domínios conhecidos (para os modelos vazios da tela)
  PUT    /memoria/api/documentos        regrava (ou cria) um documento do usuário
  DELETE /memoria/api/fichas/{id}       apaga uma ficha
"""
import json
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import fichas
from db import get_db
from memoria import _normalizar_convencoes, _normalizar_perfil, _normalizar_prefs, _seg

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGINA = Path(__file__).parent / "static" / "memoria.html"
_DOCS = {"profile": "perfil", "preferences": "preferencias", "conventions": "convencoes"}
_SUFIXO = {tipo: sufixo for sufixo, tipo in _DOCS.items()}
_KIND = {"perfil": "PerfilUsuario", "preferencias": "PreferenciasUsuario", "convencoes": "ConvencoesDoUsuario"}
_NORMALIZAR = {"perfil": _normalizar_perfil, "preferencias": _normalizar_prefs,
               "convencoes": _normalizar_convencoes}
_CAMPOS_FICHA = ("id", "dominio", "user_id", "session_id", "titulo", "intencao", "funcionou",
                 "falhou", "avaliacao", "modelo", "criada_em", "atualizada_em")


def _documentos() -> list[dict]:
    """Os documentos de usuário do store: (usuário, domínio, tipo, conteúdo, atualizado em).
    O namespace é `users.<usuário>.<domínio>.<tipo>`; o `_seg` da memória tira os pontos de cada parte."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT prefix, value, updated_at FROM store WHERE prefix LIKE 'users.%'"
        ).fetchall()
    docs = []
    for r in rows:
        partes = r["prefix"].split(".")
        if len(partes) != 4 or partes[3] not in _DOCS:
            continue
        try:
            valor = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
        docs.append({"user": partes[1], "dominio": partes[2], "tipo": _DOCS[partes[3]],
                     "conteudo": valor.get("content") or {}, "atualizado_em": r["updated_at"]})
    return docs


def _fichas(user_id: str | None = None) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_CAMPOS_FICHA)} FROM fichas "
            + ("WHERE user_id = ? " if user_id is not None else "")
            + "ORDER BY atualizada_em DESC",
            (user_id,) if user_id is not None else (),
        ).fetchall()
    return [dict(r) for r in rows]


def _add_dominio(entrada: dict, dominio: str | None) -> None:
    """Domínios sempre no formato do namespace (sem pontos), que é o que os documentos têm."""
    d = _seg(dominio or "")
    if d not in entrada["dominios"]:
        entrada["dominios"].append(d)


@router.get("/memoria", include_in_schema=False)
def pagina():
    return FileResponse(_PAGINA, media_type="text/html")


@router.get("/memoria/api/usuarios")
def usuarios():
    """Cada usuário com o que há na memória dele. O id das fichas é o real; o dos documentos
    vem sem pontos, então os dois são ligados pelo `_seg`."""
    por_chave: dict[str, dict] = {}

    def entrada(chave: str, nome: str) -> dict:
        return por_chave.setdefault(chave, {"user_id": nome, "fichas": 0, "perfil": False,
                                            "preferencias": 0, "convencoes": 0, "dominios": []})

    for f in _fichas():
        u = f["user_id"] or ""
        e = entrada(_seg(u), u)
        e["fichas"] += 1
        _add_dominio(e, f["dominio"])
    for d in _documentos():
        e = entrada(d["user"], d["user"])
        _add_dominio(e, d["dominio"])
        c = d["conteudo"]
        if d["tipo"] == "perfil":
            e["perfil"] = e["perfil"] or any(v for v in c.values())
        elif d["tipo"] == "preferencias":
            e["preferencias"] += len(c.get("preferencias") or [])
        else:
            e["convencoes"] += len(c.get("convencoes") or [])
    return sorted(por_chave.values(), key=lambda u: u["user_id"])


@router.get("/memoria/api/dominios")
def dominios():
    """Todos os domínios que já apareceram na memória (documentos, lições ou fichas), sem pontos."""
    achados = {d["dominio"] for d in _documentos()} | {_seg(f["dominio"] or "") for f in _fichas()}
    with get_db() as conn:
        for r in conn.execute("SELECT prefix FROM store WHERE prefix LIKE 'db_lessons.%'"):
            achados.add(r["prefix"].split(".", 1)[1])
    return sorted(achados)


@router.get("/memoria/api/usuarios/{user_id}/prompts")
def prompts(user_id: str):
    """O último prompt enviado ao orquestrador e ao analista para o usuário (ver
    memoria.registrar_prompt): é onde se confere o que a memória de fato injetou."""
    chave = _seg(user_id)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, tipo, session_id, dominio, prompt, created_at FROM ultimos_prompts"
        ).fetchall()
    return [{k: r[k] for k in ("tipo", "session_id", "dominio", "prompt", "created_at")}
            for r in rows if _seg(r["user_id"]) == chave]


@router.get("/memoria/api/usuarios/{user_id}")
def usuario(user_id: str):
    chave = _seg(user_id)
    docs = [d for d in _documentos() if d["user"] == chave]
    fichas_do_usuario = [f for f in _fichas() if _seg(f["user_id"] or "") == chave]
    return {
        "user_id": user_id,
        "documentos": [{k: d[k] for k in ("dominio", "tipo", "conteudo", "atualizado_em")} for d in docs],
        "fichas": fichas_do_usuario,
    }


class DocumentoEdicao(BaseModel):
    user_id: str
    dominio: str
    tipo: str
    conteudo: dict
    atualizado_em: str | None = None  # o que a tela viu; None = ela viu o modelo vazio (documento inexistente)


@router.put("/memoria/api/documentos")
def editar_documento(corpo: DocumentoEdicao):
    """Regrava o conteúdo de um documento, ou o cria se ainda não existe. Vazio é gravado como
    vazio: o agente trata documento vazio e ausente do mesmo jeito. A escrita é direta no
    SQLite: o agente lê o store a cada turno, então não há cache a invalidar."""
    if corpo.tipo not in _SUFIXO:
        return JSONResponse(status_code=400, content={"error": "Tipo inválido"})
    if not corpo.user_id.strip() or not corpo.dominio.strip():
        return JSONResponse(status_code=400, content={"error": "Usuário e domínio são obrigatórios"})
    conteudo = _NORMALIZAR[corpo.tipo](corpo.conteudo)
    prefix = f"users.{_seg(corpo.user_id)}.{_seg(corpo.dominio)}.{_SUFIXO[corpo.tipo]}"
    with get_db() as conn:
        linha = conn.execute("SELECT key, value, updated_at FROM store WHERE prefix = ?", (prefix,)).fetchone()
        if (linha["updated_at"] if linha else None) != corpo.atualizado_em:
            return JSONResponse(status_code=409, content={
                "error": "O documento mudou desde que a tela o carregou. Recarregue e tente de novo."})
        if linha is None:
            conn.execute("INSERT INTO store (prefix, key, value) VALUES (?, 'default', ?)",
                         (prefix, json.dumps({"kind": _KIND[corpo.tipo], "content": conteudo}, ensure_ascii=False)))
        else:
            valor = json.loads(linha["value"])
            valor["content"] = conteudo
            conn.execute("UPDATE store SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE prefix = ? AND key = ?",
                         (json.dumps(valor, ensure_ascii=False), prefix, linha["key"]))
        conn.commit()
    logger.info("[memoria] %s %s pelo painel", prefix, "criado" if linha is None else "editado")
    return {"ok": True}


@router.delete("/memoria/api/fichas/{ficha_id}")
def apagar_ficha(ficha_id: str):
    with get_db() as conn:
        dominio = conn.execute("SELECT dominio FROM fichas WHERE id = ?", (ficha_id,)).fetchone()
        if dominio is None:
            return JSONResponse(status_code=404, content={"error": "Ficha não encontrada"})
        conn.execute("DELETE FROM fichas WHERE id = ?", (ficha_id,))
        conn.commit()
    fichas.invalidar()
    logger.info("[memoria] ficha %s apagada pelo painel", ficha_id[:8])
    return {"ok": True}
