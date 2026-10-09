"""
Fichas de análise: a memória de episódios do agente.

Cada ficha descreve UM episódio de análise de dados, como o extrator da memória o interpretou
(memoria.py): a intenção do usuário (o título), a abordagem que funcionou, a que falhou e por
quê, e a avaliação (👍, 👎, contestada e corrigida, ou sem avaliação). Nunca traz números: os
dados vêm sempre de uma consulta nova. Ficam na tabela `fichas` do mfg.db (db.py), com o
embedding do título e da intenção.

A ficha descreve o que aconteceu; não é uma regra. Fichas parecidas, e até contraditórias,
coexistem, e quem as lê (o analista ou o orquestrador) decide o que aproveitar.

  init(project, credentials)        -> cliente de embeddings do Vertex (no startup do agente)
  salvar(...)                       -> cria ou atualiza uma ficha e calcula o embedding
  buscar(consulta, dominio, k)      -> as k fichas mais parecidas com a consulta (cosseno)
  bloco_para_analista(...)          -> texto injetado no prompt do analista
  texto_para_orquestrador(...)      -> resposta da tool buscar_analises_anteriores
  ficha_do_usuario(ref, ...)        -> a ficha de uma ref, só se for do usuário atual
  texto_da_conversa(ficha, ...)     -> resposta da tool recuperar_conversa_anterior: a conversa
                                       da ficha (inteira, ou um índice se for longa)

Privacidade: o prompt recebe o id do usuário atual, para o modelo saber quais fichas são dele.
As de outros usuários aparecem só como "outro usuário (A)", "(B)"... e o id real nunca chega
ao modelo, então não há como ele revelá-lo.

Variáveis de ambiente:
  EMBEDDING_MODEL          modelo de embeddings do Vertex (padrão: gemini-embedding-001)
  EMBEDDING_LOCATION       região do Vertex para embeddings (padrão: us-central1)
  MEMORIA_FICHAS_K         fichas injetadas no prompt do analista (padrão: 5)
  MEMORIA_FICHAS_LIMIAR    similaridade mínima (cosseno) para uma ficha entrar (padrão: 0.70)
  MEMORIA_CONVERSA_MAX_CHARS  tamanho a partir do qual a conversa vem como índice (padrão: 8000)
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime

import numpy as np

import feedback
from db import fichas_da_sessao as _db_da_sessao
from db import fichas_do_dominio as _db_do_dominio
from db import gravar_ficha as _db_gravar

logger = logging.getLogger(__name__)

DIMENSAO = 768
AVALIACOES = {
    "positiva": "👍",
    "negativa": "👎",
    "corrigida": "contestada e corrigida",
    "sem_avaliacao": "sem avaliação",
}

_cliente = None
_embedder = None  # função (textos, tarefa) -> np.ndarray; substitui o Vertex (usado nos testes)


def _modelo() -> str:
    return os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")


def _k() -> int:
    try:
        return max(1, int(os.getenv("MEMORIA_FICHAS_K", "5")))
    except ValueError:
        return 5


def _limiar() -> float:
    try:
        return float(os.getenv("MEMORIA_FICHAS_LIMIAR", "0.70"))
    except ValueError:
        return 0.70


def init(project: str, credentials=None) -> None:
    """Cria o cliente de embeddings. Sem ele as fichas são gravadas sem vetor e a busca não
    devolve nada; o chat segue funcionando."""
    global _cliente
    try:
        from google import genai

        _cliente = genai.Client(
            vertexai=True, project=project, credentials=credentials,
            location=os.getenv("EMBEDDING_LOCATION", "us-central1"),
        )
        logger.info("[fichas] embeddings: %s", _modelo())
    except Exception:
        logger.warning("[fichas] falha ao criar o cliente de embeddings; busca desligada", exc_info=True)
        _cliente = None


def definir_embedder(funcao) -> None:
    """Troca o Vertex por outra função de embedding (testes)."""
    global _embedder
    _embedder = funcao


def _embed(textos: list[str], tarefa: str) -> np.ndarray | None:
    """Vetores normalizados (norma 1), um por texto. `tarefa`: RETRIEVAL_DOCUMENT para as
    fichas, RETRIEVAL_QUERY para a consulta."""
    if not textos:
        return None
    try:
        if _embedder is not None:
            v = np.asarray(_embedder(textos, tarefa), dtype=np.float32)
        elif _cliente is not None:
            from google.genai import types

            r = _cliente.models.embed_content(
                model=_modelo(), contents=textos,
                config=types.EmbedContentConfig(task_type=tarefa, output_dimensionality=DIMENSAO),
            )
            v = np.array([e.values for e in r.embeddings], dtype=np.float32)
        else:
            return None
        normas = np.linalg.norm(v, axis=1, keepdims=True)
        normas[normas == 0] = 1
        return v / normas
    except Exception:
        logger.warning("[fichas] falha ao calcular embedding", exc_info=True)
        return None


def texto_da_ficha(f: dict) -> str:
    """O que é embeddado: o título (a intenção normalizada) e a descrição da intenção."""
    return f"{f.get('titulo') or ''}\n{f.get('intencao') or ''}".strip()


def da_sessao(session_id: str, dominio: str) -> list[dict]:
    return _db_da_sessao(session_id, dominio)


def salvar(campos: dict, session_id: str, user_id: str, dominio: str, existentes: dict[str, dict]) -> str:
    """Cria a ficha ou, se `campos['ficha_id']` é uma das `existentes` (desta sessão), a
    atualiza. Recalcula o embedding quando título ou intenção mudam. Devolve o id."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    antiga = existentes.get(campos.get("ficha_id") or "")
    if antiga:
        linha = dict(antiga)
        recalcular = texto_da_ficha(antiga) != texto_da_ficha(campos) or not antiga.get("vetor")
    else:
        linha = {"id": uuid.uuid4().hex, "dominio": dominio, "user_id": user_id,
                 "session_id": session_id, "criada_em": agora, "vetor": None, "modelo": None}
        recalcular = True
    for c in ("titulo", "intencao", "funcionou", "falhou", "avaliacao"):
        linha[c] = campos.get(c) or ("sem_avaliacao" if c == "avaliacao" else "")
    linha["atualizada_em"] = agora
    if recalcular:
        v = _embed([texto_da_ficha(linha)], "RETRIEVAL_DOCUMENT")
        linha["vetor"] = v[0].tobytes() if v is not None else None
        linha["modelo"] = _modelo() if v is not None else None
    _db_gravar(linha)
    logger.info("[fichas] ficha %s %s: %s", linha["id"][:8], "atualizada" if antiga else "criada", linha["titulo"])
    return linha["id"]


def buscar(consulta: str, dominio: str, k: int | None = None, limiar: float | None = None) -> list[dict]:
    """As `k` fichas do domínio mais parecidas com `consulta` (cosseno) e acima do limiar, da
    mais para a menos parecida. Cada uma vem com `similaridade`."""
    k = k or _k()
    limiar = _limiar() if limiar is None else limiar
    linhas = [r for r in _db_do_dominio(dominio) if r.get("vetor") and len(r["vetor"]) == DIMENSAO * 4]
    if not linhas or not (consulta or "").strip():
        return []
    q = _embed([consulta], "RETRIEVAL_QUERY")
    if q is None:
        return []
    matriz = np.vstack([np.frombuffer(r["vetor"], dtype=np.float32) for r in linhas])
    sim = matriz @ q[0]
    ordem = np.argsort(-sim)[:k]
    return [dict(linhas[i], similaridade=float(sim[i])) for i in ordem if sim[i] >= limiar]


def _data(texto: str) -> str:
    try:
        return datetime.strptime(texto[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


_TAM_REF = 8
_ARTEFATOS = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[(?:chart|pdf|excel):[^\]]*\]|\[📥[^\]]*\]\([^)]*\)")


def _ref(f: dict) -> str:
    """Referência curta de uma ficha, que o orquestrador usa para reler a conversa dela."""
    return (f.get("id") or "")[:_TAM_REF]


def ficha_do_usuario(ref: str, user_id: str, dominio: str) -> dict | None:
    """A ficha de `ref` (o começo do id) se for DESTE usuário e deste domínio; senão None. Uma
    referência de outro usuário e uma inexistente são indistinguíveis para quem chama."""
    ref = (ref or "").strip().lower()
    if not user_id or len(ref) < _TAM_REF - 2:
        return None
    achadas = [f for f in _db_do_dominio(dominio) if f.get("user_id") == user_id and f["id"].startswith(ref)]
    return achadas[0] if len(achadas) == 1 else None


def _limite_conversa() -> int:
    try:
        return max(1000, int(os.getenv("MEMORIA_CONVERSA_MAX_CHARS", "8000")))
    except ValueError:
        return 8000


def _cortar(texto: str, n: int) -> str:
    texto = " ".join((texto or "").split())
    return texto if len(texto) <= n else texto[: n - 1].rstrip() + "…"


def _turno_inteiro(i: int, t: dict, limite: int) -> str:
    """Pergunta e resposta de um turno. Uma resposta maior que o limite perde o meio: o começo
    e o fim (onde ficam os blocos de parâmetros e de dados) são o que importa."""
    resposta = _ARTEFATOS.sub("[gráfico ou arquivo da conversa original]", t.get("resposta") or "(sem resposta)")
    if len(resposta) > limite:
        cabeca = limite // 4
        resposta = resposta[:cabeca] + "\n[... trecho do meio omitido ...]\n" + resposta[-(limite - cabeca):]
    return f"Turno {i}{' [dados]' if t.get('analista') else ''}\nUsuário: {t.get('usuario', '')}\nAgente: {resposta}"


def texto_da_conversa(ficha: dict, mensagens: list, turno: int | None = None) -> str:
    """Resposta da tool recuperar_conversa_anterior: a conversa em que a ficha foi feita. Se ela
    couber no limite, vem inteira; senão vem um índice (um turno por linha) e o orquestrador
    pede os turnos que interessam com `turno`. Os valores são os do dia da conversa."""
    turnos = feedback.montar_turnos(mensagens)
    if not turnos:
        return "A conversa não está mais disponível (pode ter sido apagada)."
    limite = _limite_conversa()
    cab = (f"Conversa de {_data(ficha.get('criada_em', ''))}, da análise \"{ficha.get('titulo', '')}\". "
           "Os valores são os daquele dia: cite a data e, se o usuário quiser o valor de hoje, refaça "
           "a análise com consultar_analista.\n")
    if turno:
        if not 1 <= turno <= len(turnos):
            return cab + f"O turno {turno} não existe: a conversa tem {len(turnos)} turnos."
        return cab + _turno_inteiro(turno, turnos[turno - 1], limite)
    inteira = "\n\n".join(_turno_inteiro(i, t, limite) for i, t in enumerate(turnos, 1))
    if len(inteira) <= limite:
        return cab + inteira
    primeiro = max(1, len(turnos) - 59)
    indice = "\n".join(
        f"{i}.{' [dados]' if t.get('analista') else ''} Usuário: {_cortar(t.get('usuario'), 110)} | "
        f"Agente: {_cortar(_ARTEFATOS.sub('', t.get('resposta') or ''), 110)}"
        for i, t in enumerate(turnos, 1) if i >= primeiro
    )
    aviso = " (mostrando os últimos 60)" if primeiro > 1 else ""
    return (cab + f"A conversa tem {len(turnos)} turnos e é longa demais para vir inteira{aviso}. Índice:\n"
            f"{indice}\nPara ler um turno inteiro, chame recuperar_conversa_anterior(ref, turno=N).")


def formatar(fichas: list[dict], user_id_atual: str, com_conversa: bool = False) -> str:
    """Uma linha por ficha, dizendo de quem é: "sua" ou "de outro usuário (A)". O id real de
    outro usuário nunca aparece. `com_conversa` acrescenta, nas fichas do próprio usuário, a
    data da conversa em que a análise foi feita."""
    pseudonimos: dict[str, str] = {}
    linhas = []
    for f in fichas:
        if user_id_atual and f.get("user_id") == user_id_atual:
            autor = "sua"
        else:
            letra = pseudonimos.setdefault(f.get("user_id") or "?", chr(ord("A") + len(pseudonimos)))
            autor = f"de outro usuário ({letra})"
        cab = f"- Ficha {autor}, {_data(f.get('atualizada_em', ''))}, {AVALIACOES.get(f.get('avaliacao'), 'sem avaliação')}: {f.get('titulo', '')}"
        partes = [cab]
        if f.get("intencao"):
            partes.append(f"  Intenção: {f['intencao']}")
        if f.get("funcionou"):
            partes.append(f"  Funcionou: {f['funcionou']}")
        if f.get("falhou"):
            partes.append(f"  Falhou: {f['falhou']}")
        if com_conversa and autor == "sua":
            partes.append(f"  Feita na conversa de {_data(f.get('criada_em', ''))} (ref: {_ref(f)}).")
        linhas.append("\n".join(partes))
    return "\n".join(linhas)


_REGRAS = (
    "São referência, não regra: o pedido atual, o dominio.md e as lições valem mais. Não trazem "
    "números: os dados vêm sempre de uma consulta nova. Fichas \"sua\" são deste usuário; as "
    "\"de outro usuário\" podem ser aproveitadas e mencionadas (\"uma análise parecida feita por "
    "outro usuário\"), mas nunca diga quem é esse usuário."
)

_LEITURA = (
    "A avaliação diz o quanto confiar na ficha: \"👍\" é abordagem aprovada; \"contestada e "
    "corrigida\" quer dizer que o usuário contestou e o \"Funcionou\" já é a versão corrigida; "
    "\"👎\" é alerta (o usuário não aprovou), não receita; \"sem avaliação\" não confirma nem desmente."
)

# Só para o analista, que é quem refaz a análise.
_USO = (
    "Como usar: evite o que está em \"Falhou\", a não ser que o pedido atual peça isso; se a sua "
    "análise depender de uma escolha que já foi contestada antes (um filtro, uma métrica), diga na "
    "resposta qual escolha fez e por quê. Uma ficha pode ser de um pedido só parecido (outro período, "
    "outro recorte, outra métrica): aproveite o método, mas siga o recorte do pedido atual."
)


def bloco_para_analista(consulta: str, dominio: str, user_id: str) -> str:
    """Bloco do prompt do analista com as fichas mais parecidas com o pedido delegado. Vazio
    se não houver nenhuma acima do limiar. Nunca levanta exceção."""
    try:
        achadas = buscar(consulta, dominio)
    except Exception:
        logger.warning("[fichas] falha na busca", exc_info=True)
        return ""
    logger.info("[fichas] %d ficha(s) para o analista (similaridades: %s)", len(achadas),
                ", ".join(f"{f['similaridade']:.2f}" for f in achadas))
    if not achadas:
        return ""
    return (
        "\n\n## Análises parecidas já feitas (fichas da memória)\n"
        f"Usuário atual: {user_id or 'não identificado'}. Estas fichas descrevem COMO análises "
        "parecidas foram feitas antes e como foram avaliadas. " + _REGRAS + "\n"
        + _LEITURA + " " + _USO + "\n"
        + formatar(achadas, user_id)
    )


def texto_para_orquestrador(consulta: str, dominio: str, user_id: str, k: int = 10) -> str:
    """Resposta da tool buscar_analises_anteriores."""
    try:
        achadas = buscar(consulta, dominio, k=k)
    except Exception:
        logger.warning("[fichas] falha na busca", exc_info=True)
        achadas = []
    logger.info("[fichas] busca do orquestrador: %d ficha(s)", len(achadas))
    if not achadas:
        return "Nenhuma análise parecida encontrada na memória."
    sua = any(user_id and f.get("user_id") == user_id for f in achadas)
    return (
        f"Usuário atual: {user_id or 'não identificado'}. Fichas de análises parecidas, da mais "
        "para a menos parecida. " + _REGRAS + " " + _LEITURA
        + (" Para rever a conversa de uma ficha SUA, com as perguntas, respostas e valores daquele "
           "dia, chame recuperar_conversa_anterior com a ref dela." if sua else "")
        + " Para o valor de hoje, refaça a análise com consultar_analista."
        + "\n" + formatar(achadas, user_id, com_conversa=True)
    )
