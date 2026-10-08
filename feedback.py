"""
Avaliação das respostas pelo usuário (botões 👍 e 👎 do chat).

Funções puras sobre as mensagens do checkpoint, sem rede nem banco, usadas pela rota
`POST /chat/feedback` (main.py):

  montar_turnos(messages)    -> um item por mensagem do usuário: texto, resposta final e se a
                                resposta usou o analista (ou seja, é uma resposta de dados)
  localizar_resposta(...)    -> acha, pelo começo do texto, a resposta que o usuário avaliou
  inicio_comparavel(texto)   -> o começo normalizado com que o front e o back se entendem

Toda avaliação é gravada em `feedback_respostas` (db.py) junto com a pergunta, a resposta e,
no caso do 👎, a mensagem que o usuário escreve em seguida (que pode ou não contestar a
resposta). Esse conjunto fica guardado para melhorar o agente depois. Para a memória, 👍 e 👎
são sinais de reforço: marcam a resposta na conversa que o extrator único lê a cada 30 s, e é
só com um sinal na janela que ele pode gravar estratégias (memoria.avaliar_resposta).
"""
from __future__ import annotations

import re

from langchain_core.messages import AIMessage, HumanMessage

INICIO_DA_RESPOSTA = 120    # chars do começo (normalizado) com que o front identifica a resposta
MARCA_BOA = "[AVALIAÇÃO DO USUÁRIO: ele marcou esta resposta como boa (👍)]"
MARCA_RUIM = "[AVALIAÇÃO DO USUÁRIO: ele marcou esta resposta como ruim (👎)]"


def marca_da_nota(nota: str) -> str:
    """Marca que acompanha a resposta avaliada na conversa enviada ao extrator da memória. É
    neutra de propósito: diz só o que o usuário clicou, não o que vem depois."""
    return MARCA_BOA if nota == "up" else MARCA_RUIM


# Gráficos, PDFs e planilhas aparecem como token no checkpoint ([chart:uuid]) e como imagem ou
# link no texto que o front guarda. O começo usado para identificar a resposta ignora ambos.
_ARTEFATOS = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[(?:chart|pdf|excel):[^\]]*\]|\[📥[^\]]*\]\([^)]*\)")


def inicio_comparavel(texto: str, n: int = INICIO_DA_RESPOSTA) -> str:
    """Começo do texto sem artefatos e com espaços colapsados. O front calcula o mesmo
    (chat.service.ts, replyStart), então os dois lados casam apesar das imagens embutidas."""
    return " ".join(_ARTEFATOS.sub(" ", texto or "").split())[:n]


def _texto(content) -> str:
    """Texto de uma mensagem; o conteúdo pode vir em partes (lista) quando há imagens."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) and p.get("type") == "text"
            else (p if isinstance(p, str) else "")
            for p in content
        ).strip()
    return str(content or "")


def montar_turnos(messages: list) -> list[dict]:
    """Um turno por HumanMessage: {usuario, resposta, analista}. `analista` é True se o
    orquestrador chamou consultar_analista no turno (resposta com dados). Mensagens
    intermediárias, que chamam ferramentas, não contam como resposta."""
    turnos: list[dict] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            turnos.append({"usuario": _texto(msg.content), "resposta": None, "analista": False})
        elif isinstance(msg, AIMessage) and turnos:
            chamadas = getattr(msg, "tool_calls", None) or []
            if chamadas:
                if any(c.get("name") == "consultar_analista" for c in chamadas):
                    turnos[-1]["analista"] = True
                continue
            texto = _texto(msg.content).strip()
            if texto:
                turnos[-1]["resposta"] = texto
    return turnos


def localizar_resposta(turnos: list[dict], inicio: str) -> int | None:
    """Índice do turno cuja resposta começa com `inicio` (já normalizado por
    `inicio_comparavel`); a mais recente, se houver duas."""
    inicio = (inicio or "").strip()
    if not inicio:
        return None
    for i in range(len(turnos) - 1, -1, -1):
        resposta = turnos[i]["resposta"]
        if resposta and inicio_comparavel(resposta).startswith(inicio):
            return i
    return None


PREFIXO_MARCA = "[AVALIAÇÃO DO USUÁRIO"


def tem_sinal(msgs: list[dict]) -> bool:
    """True se a conversa enviada ao extrator traz um 👍 ou 👎 numa resposta ainda não processada
    (as de mensagens [CONTEXTO] já foram vistas). Estratégias só valem com esse sinal."""
    return any(
        m["role"] == "assistant" and not m["content"].startswith("[CONTEXTO") and PREFIXO_MARCA in m["content"]
        for m in msgs
    )
