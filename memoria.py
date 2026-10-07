"""
Memória do agente: aprende sobre o banco e sobre o usuário a partir das conversas.

Dois documentos, mantidos em background por extratores do LangMem
(create_memory_store_manager + ReflectionExecutor), depois do turno, sem atrasar a resposta:

  LIÇÕES        -> regras sobre como consultar o banco. Um documento por domínio (compartilhado
                   por todos os usuários), com duas listas de texto: `verificadas` e `hipoteses`.
                   Lido pelo sub-agente analista, que é quem escreve o SQL.
  PREFERÊNCIAS  -> preferências de apresentação. Um documento por usuário, que vale em todos
                   os domínios. Lido pelo orquestrador (que redige a resposta) e pelo analista
                   (gráficos).

O agente não grava nada: ele só lê. Os dados e valores das consultas nunca são gravados.
Os documentos ficam no mfg.db, na tabela `store` do SqliteStore do LangGraph.

Fluxo no agent_multi.py:
  consultar_analista -> add_trace(): guarda o trace do analista (SQL, erros, retries), que
                        não aparece no histórico do orquestrador.
  fim do turno       -> finalizar_turno(): fecha o registro do turno e envia ao executor.
  prompts            -> bloco_licoes() e bloco_preferencias() devolvem o texto a injetar.
  shutdown do app    -> encerrar(): o worker do ReflectionExecutor não é daemon.

Variáveis de ambiente:
  MEMORIA_ATIVA              "0" desliga leitura e extração (padrão: ligada)
  MEMORIA_DELAY_SEGUNDOS     espera depois do último turno antes de extrair (padrão: 30)
  MEMORIA_DEBUG              "1" loga o payload enviado ao extrator
  THINKING_LEVEL_MEMORIA     nível de raciocínio do extrator (padrão: medium; ver init_multi_agent)
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sqlite3
import threading
from collections import defaultdict
from concurrent.futures import Future, wait
from typing import Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables.config import patch_config
from langgraph.config import get_config
from langgraph.store.sqlite import SqliteStore
from langmem import ReflectionExecutor, create_memory_store_manager
from pydantic import BaseModel, Field

from db import DB_PATH

logger = logging.getLogger(__name__)

MAX_LICOES = 30
_MAX_ARGS = 800          # chars de entrada de ferramenta mostrados ao extrator
_MAX_SAIDA = 300         # chars de saída de ferramenta (corta dados de produção)
_MAX_RESPOSTA = 1500     # chars da resposta final mostrados ao extrator
_MAX_TURNOS_GUARDADOS = 6
_LICAO_PARECIDA = 0.6    # similaridade a partir da qual uma lição reescrita conta como a mesma

_NS_LICOES = "db_lessons"
_NS_PREFERENCIAS = ("users", "{langgraph_user_id}", "preferences")


def ativa() -> bool:
    return os.getenv("MEMORIA_ATIVA", "1").strip() != "0"


def _sem_null(schema):
    """Remove o tipo null de um JSON Schema (ver com_schemas_sem_null)."""
    if isinstance(schema, list):
        return [_sem_null(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    saida = {}
    for chave, valor in schema.items():
        if chave in ("anyOf", "oneOf") and isinstance(valor, list):
            opcoes = [_sem_null(o) for o in valor if not (isinstance(o, dict) and o.get("type") == "null")]
            if len(opcoes) == 1:  # anyOf de uma opção só vira a própria opção
                saida.update(opcoes[0])
            else:
                saida[chave] = opcoes
        elif chave == "type" and isinstance(valor, list):
            tipos = [t for t in valor if t != "null"]
            saida[chave] = tipos[0] if len(tipos) == 1 else tipos
        else:
            saida[chave] = _sem_null(valor)
    return saida


def com_schemas_sem_null(cls):
    """Subclasse do modelo de chat que tira o tipo null dos schemas das ferramentas.

    O trustcall (motor do LangMem) descreve o valor de um JSON Patch com uniões que admitem
    null dentro de listas aninhadas, e o langchain-google-vertexai 2.1.2 não converte isso
    para o Vertex ("Invalid enum value NULL for enum type ...Type"): a chamada falha antes
    de sair da máquina, o trustcall repete até o limite de passos e nada é gravado. Só o
    modelo do extrator usa esta classe; os modelos do agente ficam como estão.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    class _ModeloSemNull(cls):
        def bind_tools(self, tools, **kwargs):
            return super().bind_tools([_sem_null(convert_to_openai_tool(t)) for t in tools], **kwargs)

    _ModeloSemNull.__name__ = f"{cls.__name__}SemNull"
    return _ModeloSemNull


def _delay() -> int:
    try:
        return int(float(os.getenv("MEMORIA_DELAY_SEGUNDOS", "30")))
    except ValueError:
        return 30


def _seg(valor: str) -> str:
    """Rótulo de namespace do LangGraph: não aceita ponto (ex.: 'dominio_x.md')."""
    return str(valor).replace(".", "_") or "_"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class LicoesDoBanco(BaseModel):
    """Lições aprendidas sobre como consultar este banco de dados.

    Duas listas de texto, e não uma lista de objetos: o LangMem atualiza o documento com
    JSON Patch, e o Gemini não consegue gerar um objeto como valor de um patch (no schema
    do trustcall esse valor é um dict genérico, com additionalProperties, que a chamada de
    função do Gemini não suporta). Ele manda `{}` e a lição é descartada na validação.
    Texto ele gera sem problema.
    """

    verificadas: list[str] = Field(
        default_factory=list,
        description="Regras confirmadas: erro técnico corrigido ou regra declarada pelo "
        "usuário como permanente. Uma regra curta por item, sem dados nem valores.",
    )
    hipoteses: list[str] = Field(
        default_factory=list,
        description="Hipóteses ainda não confirmadas: contestação sem causa declarada, com "
        "indício concreto no SQL ou no resultado. Uma por item.",
    )


class Licao(BaseModel):
    """Uma lição como o app a lê; o documento guarda só as duas listas de LicoesDoBanco."""

    regra: str
    confianca: Literal["verificada", "provavel"]


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

O documento tem duas listas de texto: "verificadas" (regras confirmadas) e "hipoteses" \
(regras ainda não confirmadas). Cada item é um texto curto com a regra. Explique o \
motivo de cada mudança em planned_edits, em uma frase.

Você recebe as mensagens de um ou mais turnos: pedidos do usuário, chamadas de \
ferramenta (SQL, erros, novas tentativas) e respostas do agente. Mensagens marcadas \
com [CONTEXTO] já foram processadas antes; use-as só para entender o que o usuário \
está contestando.

O QUE REGISTRAR (uma lição = uma regra curta e específica):
1. Erro técnico verificado: a ferramenta devolveu erro (coluna ou tabela inexistente, \
tipo errado, join inválido) e uma nova tentativa funcionou. Registre em "verificadas".
2. Regra declarada pelo usuário como permanente: ele corrige o agente ou define como \
proceder, com "sempre", "nunca" ou "o correto é", sobre QUAL dado ou coluna usar e COMO \
calcular, filtrar ou somar (moeda, status, período padrão, exclusões). Registre mesmo \
que ele a diga com "mostre" ou "apresente", e mesmo que a resposta anterior já esteja de \
acordo com a regra: o que importa é que ele a declarou como permanente. Registre-a \
SEMPRE que aparecer, mesmo que as mensagens não mostrem a análise que ele está \
corrigindo e mesmo que a regra pareça específica de um cálculo ou de uma tabela: o \
usuário sabe do que está falando, e você não precisa conferir a regra contra o SQL. \
Esta é a fonte de lições mais importante. Registre em "verificadas".
3. Contestação sem causa declarada: o usuário reclama ("esse total está estranho", \
"refaz isso") sem dizer por quê. Registre uma hipótese em "hipoteses" SOMENTE se houver \
indício concreto no SQL ou no resultado (join sem chave, filtro de período omitido, \
agregação duplicando linhas). Sem indício concreto, NÃO registre nada.

O QUE NÃO REGISTRAR:
- Valores, resultados ou números dos dados.
- Preferências sobre a FORMA de apresentar (tipo de gráfico, formato, nível de detalhe) \
pertencem a outro documento. A escolha de qual dado, moeda, filtro ou cálculo usar NÃO \
é preferência de apresentação: é lição.
- Correções pontuais, válidas só para aquele pedido ("nessa análise, só o turno A").
- Contestações cuja causa você não consegue sustentar com as mensagens recebidas.

CONSOLIDAÇÃO:
- Antes de adicionar, procure lição equivalente nas duas listas: se existir, atualize-a \
em vez de duplicar.
- Se uma hipótese reaparecer em outro episódio ou o usuário a confirmar, mova-a de \
"hipoteses" para "verificadas".
- NUNCA remova uma lição só para abrir espaço, nem porque ela não apareceu neste turno. \
Remova apenas se as mensagens trouxerem evidência que a contraria.
- As lições existentes NÃO indicam que o documento está completo: uma lição só impede \
registrar a MESMA regra de novo. Avalie o turno contra os três critérios acima como se o \
documento estivesse vazio, e só então compare com o que já existe.
- Mantenha no máximo 30 lições no total.

EXEMPLOS (os nomes são fictícios; use os nomes reais que aparecerem nas mensagens):

Exemplo 1 (erro técnico verificado)
Turno: o usuário pede "produção por linha na última semana". O agente executa um \
SELECT usando a coluna "data" na tabela producao; a ferramenta responde "coluna data \
não existe"; o agente refaz com "dt_producao" e funciona.
Em "verificadas": "Na tabela producao, a coluna de data é dt_producao (não data)".

Exemplo 2 (regra declarada, permanente)
Turno: o agente entrega o OEE por linha. Usuário: "Está errado. Sempre desconsidere as \
paradas programadas ao calcular o OEE."
Em "verificadas": "Ao calcular OEE, desconsiderar paradas programadas".

Exemplo 2b (regra permanente sobre qual dado usar, ditada como "mostre")
Turno: o agente entrega o custo por fábrica. Usuário: "Sempre mostre o custo apenas em \
USD, nunca somando moedas locais." (a resposta anterior pode até já estar em USD)
Em "verificadas": "Apresentar e calcular o custo apenas em USD; nunca somar valores em \
moedas locais".

Exemplo 3 (correção pontual, não registrar)
Turno: usuário: "Nessa análise, considere só os turnos completos."
Nada a registrar: o pedido vale apenas para aquela análise.

Exemplo 4 (contestação sem causa, com indício)
Turno: o agente junta producao e paradas apenas por linha_id e entrega o total de \
peças. Usuário: "Esse total está estranho, refaz."
Em "hipoteses": "Ao juntar producao e paradas, incluir a data na chave do join; só \
linha_id pode duplicar linhas".

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
com um agente. Você recebe as mensagens de um ou mais turnos. Mensagens marcadas com \
[CONTEXTO] já foram processadas antes.

Registre apenas o que o usuário pediu, aprovou ou reutilizou:
- graficos_preferidos: tipos de gráfico que ele pede de forma explícita ou aceita sem \
contestar. Um pedido explícito já basta.
- formato_resposta: formato e nível de detalhe das respostas que ele pede (resumo \
primeiro, tabela, etc.).
- escopo_frequente: plantas, linhas ou produtos SOMENTE se aparecerem em dois ou mais \
turnos distintos. Um filtro que ele pede uma única vez ("nessa análise, só a planta X") \
é pedido pontual: NÃO registre.

Não registre: erros de consulta ao banco (outro documento), valores dos dados, pedidos \
pontuais que ele não repete. Regras sobre dados e cálculos (moeda, filtros, colunas, \
como somar ou juntar tabelas) pertencem ao documento de lições, não a este: aqui entra \
só como o resultado é APRESENTADO. Atualize os campos existentes em vez de duplicar. Se \
ele mudar de preferência, substitua o valor antigo; não remova preferências anteriores \
sem que ele as contrarie.
"""


# ---------------------------------------------------------------------------
# Store e leitura dos documentos
# ---------------------------------------------------------------------------
class _SqliteStoreSeguro(SqliteStore):
    """SqliteStore usa uma única conexão e abre `BEGIN` a cada operação, então duas threads
    ao mesmo tempo dão "cannot start a transaction within a transaction". O manager do
    LangMem faz buscas em paralelo e o chat lê o store em outras threads: serializa tudo."""

    def __init__(self, conn, **kwargs):
        super().__init__(conn, **kwargs)
        self._mutex = threading.RLock()

    def batch(self, ops):
        with self._mutex:
            return super().batch(ops)


_store: SqliteStore | None = None
_exec_licoes: ReflectionExecutor | None = None
_exec_prefs: ReflectionExecutor | None = None


def _ler_doc(namespace: tuple[str, ...], kind: str) -> tuple[str | None, dict]:
    """(chave, conteúdo) do documento, ou (None, {}) se ainda não existe."""
    if _store is None:
        return None, {}
    for item in _store.search(namespace, limit=5):
        if item.value.get("kind") == kind:
            return item.key, item.value.get("content") or {}
    return None, {}


def _licoes_do_conteudo(conteudo: dict) -> list[Licao]:
    licoes = []
    for campo, confianca in (("verificadas", "verificada"), ("hipoteses", "provavel")):
        for regra in conteudo.get(campo) or []:
            if isinstance(regra, str) and regra.strip():
                licoes.append(Licao(regra=regra.strip(), confianca=confianca))
    return licoes


def _conteudo_das_licoes(licoes: list[Licao]) -> dict:
    return {
        "verificadas": [l.regra for l in licoes if l.confianca == "verificada"],
        "hipoteses": [l.regra for l in licoes if l.confianca == "provavel"],
    }


def ler_licoes(dominio: str) -> list[Licao]:
    _, conteudo = _ler_doc((_NS_LICOES, _seg(dominio)), "LicoesDoBanco")
    return _licoes_do_conteudo(conteudo)


def ler_preferencias(user_id: str, dominio: str) -> PreferenciasUsuario:
    _, conteudo = _ler_doc(("users", _seg(user_id), "preferences"), "PreferenciasUsuario")
    try:
        return PreferenciasUsuario(**conteudo)
    except Exception:
        logger.warning("[memoria] preferências ilegíveis de %s; ignoradas", user_id)
        return PreferenciasUsuario()


# ---------------------------------------------------------------------------
# Leitura: blocos para injetar nos prompts
# ---------------------------------------------------------------------------
def bloco_licoes(dominio: str) -> str:
    """Texto das lições para o prompt do analista. Vazio se não houver nada."""
    if not ativa() or _store is None:
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
    if not ativa() or _store is None or not user_id:
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
        "São instruções do usuário, não sugestões: siga-as em toda resposta, a menos que o "
        "pedido atual diga o contrário.\n" + "\n".join(linhas)
    )


# ---------------------------------------------------------------------------
# Trava das lições
# ---------------------------------------------------------------------------
def _norm(txt: str) -> str:
    return " ".join(txt.lower().split())


def aplicar_trava(antes: list[Licao], depois: list[Licao]) -> list[Licao]:
    """Garante que o extrator não perca lições verificadas e respeite o limite de lições.

    O LangMem reescreve o documento, e o trustcall, ao tentar corrigir um patch inválido,
    chegou a sobrescrever uma lição verificada com a regra nova. Uma lição verificada que
    sumiu, sem nenhuma lição parecida no documento novo (o que indicaria uma reescrita),
    é restaurada.
    """
    resultado = list(depois)
    for velha in antes:
        if velha.confianca != "verificada":
            continue
        alvo = _norm(velha.regra)
        mantida = any(
            difflib.SequenceMatcher(None, alvo, _norm(n.regra)).ratio() >= _LICAO_PARECIDA
            for n in resultado
        )
        if not mantida:
            resultado.append(velha)
            logger.warning("[memoria] lição verificada restaurada (o extrator a removeu): %s", velha.regra)
    # limite imposto aqui, não só no prompt: descarta hipóteses primeiro, depois as mais antigas
    while len(resultado) > MAX_LICOES:
        idx = next((i for i, l in enumerate(resultado) if l.confianca == "provavel"), 0)
        resultado.pop(idx)
    return resultado


def _log_diferenca(ns: tuple[str, ...], antes: list[Licao], depois: list[Licao]) -> None:
    a = {(l.confianca, l.regra) for l in antes}
    d = {(l.confianca, l.regra) for l in depois}
    for confianca, regra in sorted(d - a):
        logger.info("[memoria] lição %s gravada em %s: %s", confianca, "/".join(ns), regra)
    for confianca, regra in sorted(a - d):
        logger.info("[memoria] lição %s retirada de %s: %s", confianca, "/".join(ns), regra)


# ---------------------------------------------------------------------------
# Extratores (o que o ReflectionExecutor executa)
# ---------------------------------------------------------------------------
_traces: dict[str, list[str]] = defaultdict(list)    # session_id -> traces do turno em curso
_turnos: dict[str, list[dict]] = defaultdict(list)   # session_id -> turnos recentes
_base: dict[str, int] = defaultdict(int)             # session_id -> turnos já descartados do início
_extraidos: dict[tuple[str, str], int] = defaultdict(int)  # (session_id, tipo) -> turnos extraídos (absoluto)
_futuros: list[Future] = []
_lock = threading.Lock()


def _cortar(txt: str, n: int) -> str:
    txt = str(txt)
    return txt if len(txt) <= n else txt[:n] + "…"


class _LogDoPlano(BaseCallbackHandler):
    """Registra no log o `planned_edits` do extrator, onde ele explica o motivo de cada
    mudança (o documento não guarda o motivo)."""

    def __init__(self, tipo: str):
        self.tipo = tipo

    def on_llm_end(self, response, **kwargs):
        for geracoes in response.generations:
            for g in geracoes:
                for tc in getattr(getattr(g, "message", None), "tool_calls", None) or []:
                    plano = (tc.get("args") or {}).get("planned_edits")
                    if plano:
                        logger.info("[memoria] extrator de %s: %s", self.tipo, _cortar(plano, 600))


class _Extrator:
    """Envolve um manager do LangMem para o ReflectionExecutor, que só exige um atributo
    `namespace` e um `invoke(payload)` (a config vem do contexto).

    O ponteiro de turnos extraídos avança aqui dentro, antes de o Future ser concluído, e
    não num callback do Future: o `wait()` acorda antes de os callbacks rodarem, e um
    callback registrado com `_lock` preso numa tarefa já concluída rodaria na hora e
    travaria no mesmo lock."""

    def __init__(self, manager, tipo: str, trava: bool = False):
        self._manager = manager
        self.namespace = manager.namespace
        self._tipo = tipo
        self._trava = trava

    def invoke(self, payload, config=None):
        meta = payload.get("_meta") or {}
        config = patch_config(config or get_config(), callbacks=[_LogDoPlano(self._tipo)])
        entrada = {"messages": payload["messages"]}
        try:
            if self._trava:
                self._invoke_com_trava(entrada, config)
            else:
                self._manager.invoke(entrada, config)
        except Exception as e:
            # o ponteiro não avança: esses turnos voltam na próxima extração da sessão
            logger.warning("[memoria] falha na extração de %s (sessão %s): %r",
                           self._tipo, meta.get("sessao"), e)
            return []
        with _lock:
            chave = (meta.get("sessao"), self._tipo)
            _extraidos[chave] = max(_extraidos[chave], meta.get("n_abs", 0))
        logger.info("[memoria] extração de %s concluída (sessão %s)", self._tipo, meta.get("sessao"))
        return []

    def _invoke_com_trava(self, entrada, config) -> None:
        ns = self.namespace(config)
        _, antes_raw = _ler_doc(ns, "LicoesDoBanco")
        antes = _licoes_do_conteudo(antes_raw)
        self._manager.invoke(entrada, config)
        chave, depois_raw = _ler_doc(ns, "LicoesDoBanco")
        depois = _licoes_do_conteudo(depois_raw)
        final = aplicar_trava(antes, depois)
        if chave and final != depois:
            _store.put(ns, chave, {"kind": "LicoesDoBanco", "content": _conteudo_das_licoes(final)})
        _log_diferenca(ns, antes, final)


# ---------------------------------------------------------------------------
# Registro dos turnos
# ---------------------------------------------------------------------------
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
    if not ativa() or _store is None:
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


def _mensagens(turnos: list[dict], n_contexto: int) -> list[dict]:
    msgs = []
    for i, t in enumerate(turnos):
        marca = "[CONTEXTO, já processado] " if i < n_contexto else ""
        msgs.append({"role": "user", "content": marca + t["pergunta"]})
        partes = [t["trace"]] if t["trace"] else []
        partes.append(t["resposta"])
        msgs.append({"role": "assistant", "content": marca + "\n".join(partes)})
    return msgs


# Palavras com que o usuário declara uma regra permanente (pt, en, zh, os idiomas do app).
_DECLARA_REGRA = re.compile(
    r"\b(sempre|nunca|jamais|o correto é|a regra é|always|never|the correct)\b|总是|永远|一直|从不|务必",
    re.IGNORECASE,
)


def _enviar(executor: ReflectionExecutor, tipo: str, sessao: str, turnos: list[dict], base: int,
            config: dict, exige_trace: bool) -> None:
    ja = _extraidos[(sessao, tipo)]
    # um turno de contexto antes dos pendentes: a contestação chega junto com a análise
    # contestada, mesmo que ela já tenha sido extraída antes.
    inicio = max(0, ja - base - 1)
    janela = turnos[inicio:]
    n_contexto = (ja - base) - inicio if ja > base else 0
    if not janela or ja >= base + len(turnos):
        return
    # Extrair lições custa uma chamada de LLM por turno, então só se há o que aprender: um
    # trace do analista (erro técnico, contestação) ou o usuário declarando uma regra
    # permanente, o que pode acontecer num turno sem nenhuma consulta.
    if exige_trace and not (
        any(t["trace"] for t in janela)
        or any(_DECLARA_REGRA.search(t["pergunta"]) for t in janela[n_contexto:])
    ):
        return
    logger.info("[memoria] agendando extração de %s (sessão %s, %d turnos na janela)", tipo, sessao, len(janela))
    msgs = _mensagens(janela, n_contexto)
    if os.getenv("MEMORIA_DEBUG", "0").strip() == "1":
        logger.info("[memoria] payload de %s: %s", tipo, json.dumps(msgs, ensure_ascii=False))
    fut = executor.submit(
        {"messages": msgs, "_meta": {"sessao": sessao, "n_abs": base + len(turnos)}},
        config=config,
        after_seconds=_delay(),
        thread_id=sessao,
    )
    _futuros[:] = [f for f in _futuros if not f.done()]
    _futuros.append(fut)


def finalizar_turno(session_id: str, user_id: str, dominio: str, pergunta: str, resposta: str) -> None:
    """Fecha o registro do turno e agenda a extração (o executor faz o debounce por sessão)."""
    if not ativa() or _store is None:
        descartar_turno(session_id)
        return
    try:
        with _lock:
            trace = "\n".join(_traces.pop(session_id, []))
            lista = _turnos[session_id]
            lista.append({"pergunta": pergunta, "trace": trace, "resposta": _cortar(resposta, _MAX_RESPOSTA)})
            if len(lista) > _MAX_TURNOS_GUARDADOS:
                excesso = len(lista) - _MAX_TURNOS_GUARDADOS
                del lista[:excesso]
                _base[session_id] += excesso
            turnos, base = list(lista), _base[session_id]
            _enviar(
                _exec_licoes, "licoes", session_id, turnos, base,
                {"configurable": {"database_id": _seg(dominio)}}, exige_trace=True,
            )
            if user_id:
                _enviar(
                    _exec_prefs, "prefs", session_id, turnos, base,
                    {"configurable": {"langgraph_user_id": _seg(user_id)}}, exige_trace=False,
                )
    except Exception:
        logger.warning("[memoria] falha ao agendar a extração", exc_info=True)


# ---------------------------------------------------------------------------
# Inicialização e encerramento
# ---------------------------------------------------------------------------
def init(llm) -> None:
    """Chamada em init_multi_agent com o modelo do extrator."""
    global _store, _exec_licoes, _exec_prefs
    try:
        # conexão própria: o worker do executor roda em outra thread. O SqliteStore exige
        # autocommit (isolation_level=None) porque ele mesmo abre as transações.
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        store = _SqliteStoreSeguro(conn)
        store.setup()
        licoes_manager = create_memory_store_manager(
            llm,
            schemas=[LicoesDoBanco],
            default=LicoesDoBanco(),
            instructions=LICOES_INSTRUCOES,
            enable_inserts=False,
            namespace=(_NS_LICOES, "{database_id}"),
            store=store,
        )
        prefs_manager = create_memory_store_manager(
            llm,
            schemas=[PreferenciasUsuario],
            default=PreferenciasUsuario(),
            instructions=PREFERENCIAS_INSTRUCOES,
            enable_inserts=False,
            namespace=_NS_PREFERENCIAS,
            store=store,
        )
        _exec_licoes = ReflectionExecutor(_Extrator(licoes_manager, "licoes", trava=True), store=store)
        _exec_prefs = ReflectionExecutor(_Extrator(prefs_manager, "prefs"), store=store)
        _store = store
        logger.info("[memoria] %s", "ativa" if ativa() else "desligada (MEMORIA_ATIVA=0)")
    except Exception:
        logger.warning("[memoria] falha ao inicializar; memória desligada", exc_info=True)
        _store = None


def aguardar(timeout: float = 120) -> None:
    """Espera as extrações em andamento (usado em testes)."""
    with _lock:
        pendentes = list(_futuros)
    if pendentes:
        wait(pendentes, timeout=timeout)


def encerrar() -> None:
    """Chamada no shutdown do app: o worker do ReflectionExecutor não é daemon, então sem
    isto o processo não termina. Extrações já agendadas rodam antes de o worker sair."""
    for ex in (_exec_licoes, _exec_prefs):
        if ex is not None:
            try:
                ex.shutdown(wait=False)
            except Exception:
                logger.warning("[memoria] falha ao encerrar o executor", exc_info=True)
