"""
Memória do agente: aprende sobre o banco e sobre o usuário a partir das conversas.

Dois documentos, atualizados em background por um extrator (uma chamada de LLM separada,
depois do turno, que não atrasa a resposta):

  LIÇÕES        -> regras sobre como consultar o banco. Uma lista por domínio (compartilhada
                   por todos os usuários). Cada lição tem confiança "verificada" ou "provavel".
                   Lida pelo sub-agente analista, que é quem escreve o SQL.
  PREFERÊNCIAS  -> preferências de apresentação. Um documento por usuário e domínio. Lido
                   pelo orquestrador (que redige a resposta) e pelo analista (gráficos).

O agente não grava nada: ele só lê. Os dados e valores das consultas nunca são gravados.

Fluxo no agent_multi.py:
  consultar_analista -> add_trace(): guarda o trace do analista (SQL, erros, retries), que
                        não aparece no histórico do orquestrador.
  fim do turno       -> finalizar_turno(): fecha o registro do turno e agenda a extração.
  prompts            -> bloco_licoes() e bloco_preferencias() devolvem o texto a injetar.

Variáveis de ambiente:
  MEMORIA_ATIVA              "0" desliga leitura e extração (padrão: ligada)
  MEMORIA_DELAY_SEGUNDOS     espera depois do último turno antes de extrair (padrão: 30)
  THINKING_LEVEL_MEMORIA     nível de raciocínio do extrator (padrão: low; ver init_multi_agent)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from db import get_db

logger = logging.getLogger(__name__)

MAX_LICOES = 30
_MAX_ARGS = 800          # chars de entrada de ferramenta mostrados ao extrator
_MAX_SAIDA = 300         # chars de saída de ferramenta (corta dados de produção)
_MAX_RESPOSTA = 1500     # chars da resposta final mostrados ao extrator
_MAX_TURNOS_GUARDADOS = 6

_llm = None
_lock = threading.Lock()


def ativa() -> bool:
    return os.getenv("MEMORIA_ATIVA", "1").strip() != "0"


def _delay() -> float:
    try:
        return float(os.getenv("MEMORIA_DELAY_SEGUNDOS", "30"))
    except ValueError:
        return 30.0


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class Licao(BaseModel):
    """Uma regra aprendida sobre como consultar o banco."""

    regra: str = Field(description="Regra curta e específica, sem dados nem valores.")
    confianca: Literal["verificada", "provavel"] = Field(
        description="verificada: erro técnico ou causa declarada pelo usuário. "
        "provavel: hipótese após contestação sem causa declarada."
    )
    motivo: str = Field(description="O que aconteceu, em uma frase, sem dados.")


class AtualizarLicao(BaseModel):
    id: int = Field(description="Número da lição existente a substituir.")
    licao: Licao


class RemoverLicao(BaseModel):
    id: int = Field(description="Número da lição existente a remover.")
    motivo: str = Field(description="Evidência nas mensagens que contraria a lição.")


class AtualizacaoLicoes(BaseModel):
    """Mudanças a aplicar no documento de lições. Lição não citada fica intacta."""

    avaliacao: str = Field(
        description="Antes de decidir, em 1 a 3 frases: houve erro técnico corrigido? o usuário "
        "declarou uma regra permanente? o usuário contestou a análise e há indício concreto "
        "no SQL ou no resultado? Responda a cada pergunta, mesmo que seja 'não'."
    )
    novas: list[Licao] = Field(default_factory=list)
    atualizar: list[AtualizarLicao] = Field(default_factory=list)
    remover: list[RemoverLicao] = Field(default_factory=list)


class PreferenciasUsuario(BaseModel):
    """Preferências de apresentação do usuário."""

    graficos_preferidos: list[str] = Field(
        default_factory=list, description="Tipos de gráfico que o usuário pede ou aprova."
    )
    formato_resposta: str | None = Field(
        None, description="Ex.: resumo curto primeiro, tabela, nível de detalhe."
    )
    escopo_frequente: list[str] = Field(
        default_factory=list, description="Plantas, linhas ou produtos que ele consulta."
    )


# ---------------------------------------------------------------------------
# Instruções dos extratores
# ---------------------------------------------------------------------------
LICOES_INSTRUCOES = """\
Você mantém o documento de LIÇÕES sobre como consultar um banco de dados. As lições \
ajudam um agente de análise a não repetir erros. Escreva as lições em português.

Você recebe as lições existentes (cada uma com um número) e a transcrição de um ou \
mais turnos: pedidos do usuário, chamadas de ferramenta (SQL, erros, novas tentativas) \
e respostas do agente. Turnos marcados como CONTEXTO já foram processados antes; use-os \
só para entender o que o usuário está contestando. Devolva apenas as MUDANÇAS: lição \
que você não citar permanece como está.

O QUE REGISTRAR (uma lição = uma regra curta e específica):
1. Erro técnico verificado: a ferramenta devolveu erro (coluna ou tabela inexistente, \
tipo errado, join inválido) e uma nova tentativa funcionou. Confiança "verificada".
2. Causa declarada pelo usuário como permanente: ele corrige o agente e diz a causa com \
"sempre", "nunca" ou "o correto é". Confiança "verificada".
3. Contestação sem causa declarada: o usuário reclama ("esse total está estranho", \
"refaz isso") sem dizer por quê. Registre uma hipótese com confiança "provavel" SOMENTE \
se houver indício concreto no SQL ou no resultado (join sem chave, filtro de período \
omitido, agregação duplicando linhas). Sem indício concreto, NÃO registre nada.

O QUE NÃO REGISTRAR:
- Valores, resultados ou números dos dados.
- Preferências de apresentação (tipo de gráfico, formato): pertencem a outro documento.
- Correções pontuais, válidas só para aquele pedido ("nessa análise, só o turno A").
- Contestações cuja causa você não consegue sustentar com as mensagens recebidas.

CONSOLIDAÇÃO:
- Antes de adicionar, procure lição equivalente: se existir, use "atualizar" em vez de \
"novas".
- Se uma hipótese "provavel" reaparecer em outro episódio ou o usuário a confirmar, \
atualize-a para "verificada".
- NUNCA remova uma lição só para abrir espaço ou porque ela não apareceu neste turno. \
Remova apenas se as mensagens trouxerem evidência que a contraria, e cite essa \
evidência em "motivo".
- As lições existentes NÃO indicam que o documento está completo: uma lição só impede registrar a MESMA regra de novo. Avalie o turno contra os três critérios acima como se o documento estivesse vazio, e só então compare com o que já existe.
- Se, depois dessa avaliação, não há nada novo a registrar, devolva tudo vazio.

EXEMPLOS (os nomes são fictícios; use os nomes reais que aparecerem nas mensagens):

Exemplo 1 (erro técnico verificado)
Turno: o usuário pede "produção por linha na última semana". O agente executa um \
SELECT usando a coluna "data" na tabela producao; a ferramenta responde "coluna data \
não existe"; o agente refaz com "dt_producao" e funciona.
Mudança: novas = regra "Na tabela producao, a coluna de data é dt_producao (não data)"; \
confianca "verificada"; motivo "erro de coluna inexistente corrigido na nova tentativa".

Exemplo 2 (causa declarada, permanente)
Turno: o agente entrega o OEE por linha. Usuário: "Está errado. Sempre desconsidere as \
paradas programadas ao calcular o OEE."
Mudança: novas = regra "Ao calcular OEE, desconsiderar paradas programadas"; confianca \
"verificada"; motivo "usuário declarou a regra como permanente".

Exemplo 3 (correção pontual, não registrar)
Turno: usuário: "Nessa análise, considere só os turnos completos."
Nada a registrar: o pedido vale apenas para aquela análise.

Exemplo 4 (contestação sem causa, com indício)
Turno: o agente junta producao e paradas apenas por linha_id e entrega o total de \
peças. Usuário: "Esse total está estranho, refaz."
Mudança: novas = regra "Ao juntar producao e paradas, incluir a data na chave do join; \
só linha_id pode duplicar linhas"; confianca "provavel"; motivo "análise contestada; o \
join por linha_id apenas pode ter duplicado linhas".

Exemplo 5 (contestação sem causa e sem indício, não registrar)
Turno: o agente entrega a tabela de refugo por produto e o SQL parece correto. Usuário: \
"Não gostei, faz de novo."
Nada a registrar: não há indício concreto de causa nas mensagens.

Exemplo 6 (preferência, não é lição)
Turno: usuário: "Prefiro gráfico de barras."
Nada a registrar neste documento.
"""

PREFERENCIAS_INSTRUCOES = """\
Você mantém o documento de PREFERÊNCIAS de apresentação de um usuário que analisa dados \
com um agente. Você recebe o documento atual e a transcrição de um ou mais turnos. \
Turnos marcados como CONTEXTO já foram processados antes. Devolva o documento completo, \
atualizado; se nada mudou, devolva-o igual.

Registre apenas o que o usuário pediu, aprovou ou reutilizou:
- graficos_preferidos: tipos de gráfico que ele pede de forma explícita ou aceita sem \
contestar. Um pedido explícito já basta.
- formato_resposta: formato e nível de detalhe das respostas que ele pede (resumo \
primeiro, tabela, etc.).
- escopo_frequente: plantas, linhas ou produtos SOMENTE se aparecerem em dois ou mais \
turnos distintos. Um filtro que ele pede uma única vez ("nessa análise, só a planta X") \
é pedido pontual: NÃO registre.

Não registre: erros de consulta ao banco (outro documento), valores dos dados, pedidos \
pontuais que ele não repete. Atualize os campos existentes em vez de duplicar. Se ele \
mudar de preferência, substitua o valor antigo; não remova preferências anteriores sem \
que ele as contrarie.
"""


# ---------------------------------------------------------------------------
# Armazenamento (SQLite do app, mfg.db)
# ---------------------------------------------------------------------------
def _garantir_tabelas() -> None:
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_lessons (
                dominio    TEXT PRIMARY KEY,
                doc        TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_preferences (
                user_id    TEXT NOT NULL,
                dominio    TEXT NOT NULL,
                doc        TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, dominio)
            )"""
        )
        conn.commit()


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ler_licoes(dominio: str) -> list[Licao]:
    with get_db() as conn:
        row = conn.execute("SELECT doc FROM agent_lessons WHERE dominio = ?", (dominio,)).fetchone()
    if not row:
        return []
    try:
        return [Licao(**d) for d in json.loads(row["doc"])]
    except Exception:
        logger.warning("[memoria] documento de lições ilegível em %s; ignorado", dominio)
        return []


def gravar_licoes(dominio: str, licoes: list[Licao]) -> None:
    doc = json.dumps([l.model_dump() for l in licoes], ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_lessons (dominio, doc, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(dominio) DO UPDATE SET doc = excluded.doc, updated_at = excluded.updated_at",
            (dominio, doc, _agora()),
        )
        conn.commit()


def ler_preferencias(user_id: str, dominio: str) -> PreferenciasUsuario:
    with get_db() as conn:
        row = conn.execute(
            "SELECT doc FROM agent_preferences WHERE user_id = ? AND dominio = ?",
            (user_id, dominio),
        ).fetchone()
    if not row:
        return PreferenciasUsuario()
    try:
        return PreferenciasUsuario(**json.loads(row["doc"]))
    except Exception:
        logger.warning("[memoria] preferências ilegíveis de %s; ignoradas", user_id)
        return PreferenciasUsuario()


def gravar_preferencias(user_id: str, dominio: str, prefs: PreferenciasUsuario) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_preferences (user_id, dominio, doc, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, dominio) DO UPDATE SET doc = excluded.doc, updated_at = excluded.updated_at",
            (user_id, dominio, prefs.model_dump_json(), _agora()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Leitura: blocos para injetar nos prompts
# ---------------------------------------------------------------------------
def bloco_licoes(dominio: str) -> str:
    """Texto das lições para o prompt do analista. Vazio se não houver nada."""
    if not ativa():
        return ""
    try:
        licoes = ler_licoes(dominio)
    except Exception:
        logger.warning("[memoria] falha ao ler lições", exc_info=True)
        return ""
    if not licoes:
        return ""
    verificadas = [l.regra for l in licoes if l.confianca == "verificada"]
    provaveis = [l.regra for l in licoes if l.confianca == "provavel"]
    partes = ["\n\n## Lições aprendidas sobre este banco (memória de conversas anteriores)"]
    if verificadas:
        partes.append("Regras confirmadas, siga-as:\n" + "\n".join(f"- {r}" for r in verificadas))
    if provaveis:
        partes.append(
            "Hipóteses ainda não confirmadas. Trate como dica, confirme antes de aplicar:\n"
            + "\n".join(f"- {r}" for r in provaveis)
        )
    return "\n".join(partes)


def bloco_preferencias(user_id: str, dominio: str) -> str:
    """Texto das preferências do usuário. Vazio se não houver user_id ou nada gravado."""
    if not ativa() or not user_id:
        return ""
    try:
        p = ler_preferencias(user_id, dominio)
    except Exception:
        logger.warning("[memoria] falha ao ler preferências", exc_info=True)
        return ""
    linhas = []
    if p.graficos_preferidos:
        linhas.append("- Gráficos que costuma pedir: " + ", ".join(p.graficos_preferidos))
    if p.formato_resposta:
        linhas.append(f"- Formato de resposta preferido: {p.formato_resposta}")
    if p.escopo_frequente:
        linhas.append("- Consulta com frequência: " + ", ".join(p.escopo_frequente))
    if not linhas:
        return ""
    return (
        "\n\n## Preferências deste usuário (memória de conversas anteriores)\n"
        "Aplique quando o pedido atual não disser o contrário.\n" + "\n".join(linhas)
    )


# ---------------------------------------------------------------------------
# Registro dos turnos
# ---------------------------------------------------------------------------
_traces: dict[str, list[str]] = defaultdict(list)   # session_id -> traces do turno em curso
_turnos: dict[str, list[dict]] = defaultdict(list)  # session_id -> turnos recentes
_extraidos: dict[str, int] = defaultdict(int)       # session_id -> qtd de turnos já extraídos
_timers: dict[str, threading.Timer] = {}


def _cortar(txt: str, n: int) -> str:
    txt = str(txt)
    return txt if len(txt) <= n else txt[:n] + "…"


def renderizar_trace(messages: list[BaseMessage]) -> str:
    """Resume as mensagens do analista (SQL, erros, retries) em texto curto, sem dados."""
    linhas = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in getattr(m, "tool_calls", None) or []:
                args = json.dumps(tc.get("args", {}), ensure_ascii=False)
                linhas.append(f"[analista chamou {tc.get('name')}] {_cortar(args, _MAX_ARGS)}")
        elif isinstance(m, ToolMessage):
            nome = getattr(m, "name", None) or "ferramenta"
            linhas.append(f"[resultado de {nome}] {_cortar(m.content, _MAX_SAIDA)}")
    return "\n".join(linhas)


def add_trace(session_id: str, messages: list[BaseMessage]) -> None:
    """Chamada por consultar_analista com as mensagens do sub-agente."""
    if not ativa():
        return
    try:
        trace = renderizar_trace(messages)
        if trace:
            with _lock:
                _traces[session_id].append(trace)
    except Exception:
        logger.warning("[memoria] falha ao registrar trace", exc_info=True)


def descartar_turno(session_id: str) -> None:
    """Turno cancelado ou com erro: descarta o trace acumulado."""
    with _lock:
        _traces.pop(session_id, None)


def finalizar_turno(session_id: str, user_id: str, dominio: str, pergunta: str, resposta: str) -> None:
    """Fecha o registro do turno e agenda a extração em background (com debounce)."""
    if not ativa() or _llm is None:
        descartar_turno(session_id)
        return
    with _lock:
        trace = "\n".join(_traces.pop(session_id, []))
        _turnos[session_id].append(
            {
                "user_id": user_id,
                "dominio": dominio,
                "pergunta": pergunta,
                "trace": trace,
                "resposta": _cortar(resposta, _MAX_RESPOSTA),
            }
        )
        if len(_turnos[session_id]) > _MAX_TURNOS_GUARDADOS:
            excesso = len(_turnos[session_id]) - _MAX_TURNOS_GUARDADOS
            del _turnos[session_id][:excesso]
            _extraidos[session_id] = max(0, _extraidos[session_id] - excesso)
        antigo = _timers.get(session_id)
        if antigo:
            antigo.cancel()
        t = threading.Timer(_delay(), _extrair_sessao, args=(session_id,))
        t.daemon = True
        _timers[session_id] = t
        t.start()


def _transcricao(turnos: list[dict], n_contexto: int) -> str:
    blocos = []
    for i, t in enumerate(turnos):
        rotulo = "CONTEXTO (já processado)" if i < n_contexto else "TURNO"
        partes = [f"### {rotulo}", f"Usuário: {t['pergunta']}"]
        if t["trace"]:
            partes.append(t["trace"])
        partes.append(f"Agente: {t['resposta']}")
        blocos.append("\n".join(partes))
    return "\n\n".join(blocos)


def _extrair_sessao(session_id: str) -> None:
    """Roda em thread de background. Nunca propaga exceção."""
    try:
        with _lock:
            _timers.pop(session_id, None)
            turnos = list(_turnos.get(session_id, []))
            ja = _extraidos.get(session_id, 0)
            if ja >= len(turnos):
                return
            _extraidos[session_id] = len(turnos)
        # um turno de contexto antes dos pendentes: a contestação chega junto com a análise
        # contestada, mesmo que ela já tenha sido extraída antes.
        inicio = max(0, ja - 1)
        janela = turnos[inicio:]
        n_contexto = ja - inicio
        texto = _transcricao(janela, n_contexto)
        dominio = janela[-1]["dominio"]
        user_id = janela[-1]["user_id"]

        if any(t["trace"] for t in janela):
            _atualizar_licoes(dominio, texto)
        if user_id:
            _atualizar_preferencias(user_id, dominio, texto)
    except Exception:
        logger.warning("[memoria] falha na extração da sessão %s", session_id, exc_info=True)


# ---------------------------------------------------------------------------
# Extração
# ---------------------------------------------------------------------------
def aplicar_mudancas(atuais: list[Licao], mud: AtualizacaoLicoes) -> list[Licao]:
    """Aplica as mudanças do extrator. Ids inválidos são ignorados; nada é apagado sem
    que o extrator tenha citado o motivo."""
    licoes = list(atuais)
    remover: set[int] = set()
    for r in mud.remover:
        if 0 <= r.id < len(atuais) and r.motivo.strip():
            remover.add(r.id)
            logger.info("[memoria] lição removida (%s): %s | motivo: %s",
                        atuais[r.id].confianca, atuais[r.id].regra, r.motivo)
    for a in mud.atualizar:
        if 0 <= a.id < len(atuais) and a.id not in remover:
            licoes[a.id] = a.licao
    licoes = [l for i, l in enumerate(licoes) if i not in remover]
    existentes = {l.regra.strip().lower() for l in licoes}
    for n in mud.novas:
        if n.regra.strip() and n.regra.strip().lower() not in existentes:
            licoes.append(n)
            existentes.add(n.regra.strip().lower())
    # limite imposto aqui, não só no prompt: descarta as mais antigas, hipóteses primeiro
    while len(licoes) > MAX_LICOES:
        idx = next((i for i, l in enumerate(licoes) if l.confianca == "provavel"), 0)
        licoes.pop(idx)
    return licoes


def _atualizar_licoes(dominio: str, transcricao: str) -> None:
    atuais = ler_licoes(dominio)
    existentes = "\n".join(
        f"{i}. [{l.confianca}] {l.regra} (motivo: {l.motivo})" for i, l in enumerate(atuais)
    ) or "(nenhuma)"
    msgs = [
        SystemMessage(content=LICOES_INSTRUCOES),
        HumanMessage(content=f"LIÇÕES EXISTENTES:\n{existentes}\n\nTRANSCRIÇÃO:\n{transcricao}"),
    ]
    mud = _llm.with_structured_output(AtualizacaoLicoes).invoke(msgs)
    if mud is None:
        return
    novas = aplicar_mudancas(atuais, mud)
    if [l.model_dump() for l in novas] != [l.model_dump() for l in atuais]:
        gravar_licoes(dominio, novas)
        logger.info("[memoria] lições de %s atualizadas (%d -> %d)", dominio, len(atuais), len(novas))


def _atualizar_preferencias(user_id: str, dominio: str, transcricao: str) -> None:
    atual = ler_preferencias(user_id, dominio)
    msgs = [
        SystemMessage(content=PREFERENCIAS_INSTRUCOES),
        HumanMessage(
            content=f"DOCUMENTO ATUAL:\n{atual.model_dump_json(indent=2)}\n\nTRANSCRIÇÃO:\n{transcricao}"
        ),
    ]
    novo = _llm.with_structured_output(PreferenciasUsuario).invoke(msgs)
    if novo is not None and novo != atual:
        gravar_preferencias(user_id, dominio, novo)
        logger.info("[memoria] preferências de %s atualizadas", user_id)


def init(llm) -> None:
    """Chamada em init_multi_agent com o modelo do extrator."""
    global _llm
    try:
        _garantir_tabelas()
        _llm = llm
        logger.info("[memoria] %s", "ativa" if ativa() else "desligada (MEMORIA_ATIVA=0)")
    except Exception:
        logger.warning("[memoria] falha ao inicializar; memória desligada", exc_info=True)
        _llm = None
