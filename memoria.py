"""
Memória do agente: aprende sobre o banco e sobre o usuário a partir das conversas.

Três documentos, mantidos em background depois do turno, sem atrasar a resposta:

  LIÇÕES        -> regras sobre como consultar o banco. Um documento por domínio (compartilhado
                   por todos os usuários), com duas listas de texto: `verificadas` e `hipoteses`.
                   Lido pelo sub-agente analista, que é quem escreve o SQL.
  PREFERÊNCIAS  -> preferências que o usuário declarou ("prefiro", "sempre que possível"), em
                   frases livres, sem campos por assunto. Um documento por usuário e por domínio
                   (nada vaza de um domínio para outro). Lido pelo orquestrador (que redige a
                   resposta) e pelo analista (gráficos).
  PERFIL        -> quem é o usuário, como ele mesmo contou (nome, cargo, área, atuação, local,
                   equipe ou gestor, e uma lista livre para o que não couber). Um documento por
                   usuário e por domínio. Lido só pelo orquestrador, que se dirige à pessoa.

Uma ÚNICA chamada estruturada ao LLM (`_Extrator`) lê a conversa e os três documentos e devolve
uma lista de operações (`MudancasMemoria`: destino, ação, texto antigo e texto novo); o código as
aplica por casamento de texto (`aplicar_operacoes`). Como o mesmo modelo decide, na mesma
chamada, em qual documento cada declaração entra, ela nunca vai para dois (nem contradiz um deles). Do LangMem só usamos o `ReflectionExecutor`, que agenda a extração em
background e faz o debounce por sessão.

O agente não grava nada: ele só lê. Os dados e valores das consultas nunca são gravados.
Os documentos ficam no mfg.db, na tabela `store` do SqliteStore do LangGraph.

Fluxo no agent_multi.py:
  consultar_analista -> add_trace(): guarda o trace do analista (SQL, erros, retries), que
                        não aparece no histórico do orquestrador.
  fim do turno       -> finalizar_turno(): fecha o registro do turno e envia ao executor.
  prompts            -> bloco_licoes(), bloco_preferencias() e bloco_perfil() devolvem o texto
                        a injetar.
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

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.store.sqlite import SqliteStore
from langmem import ReflectionExecutor
from pydantic import BaseModel, Field

from db import DB_PATH

logger = logging.getLogger(__name__)

MAX_LICOES = 30
_MAX_ARGS = 800          # chars de entrada de ferramenta mostrados ao extrator
_MAX_SAIDA = 300         # chars de saída de ferramenta (corta dados de produção)
_MAX_RESPOSTA = 1500     # chars da resposta final mostrados ao extrator
_MAX_TURNOS_GUARDADOS = 6
MAX_PREFERENCIAS = 20
MAX_OUTROS = 10          # itens livres do perfil
_IGUAL = 0.9             # similaridade a partir da qual duas frases contam como a mesma
_CITADA = 0.8            # mínimo para o texto citado pelo extrator casar (de forma aproximada) com um item
_EMPATE = 0.05           # dois itens assim de parecidos com a citação: ambíguo, não mexe em nenhum

_NS_LICOES = "db_lessons"


def ativa() -> bool:
    return os.getenv("MEMORIA_ATIVA", "1").strip() != "0"


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

    Duas listas de texto, uma regra curta por item.
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
    """Preferências do usuário, como frases livres.

    Lista de texto, sem campos por assunto (cor, tipo de gráfico, formato...): ninguém prevê
    as preferências de um usuário, e um campo novo a cada tipo de pedido não escala.
    """

    preferencias: list[str] = Field(
        default_factory=list,
        description="Preferências que o usuário DECLAROU como permanentes, uma frase curta "
        "por item, nas palavras dele. Nunca inferidas de pedidos pontuais.",
    )


class PerfilUsuario(BaseModel):
    """Quem é o usuário, só o que ele mesmo declarou.

    Campos de texto simples (ou vazios) mais uma lista livre: a lista `outros` guarda o que
    ninguém previu, sem exigir campo novo.
    """

    nome: str | None = Field(None, description="Como a pessoa quer ser chamada.")
    cargo: str | None = Field(None, description="Cargo ou posição.")
    funcao_area: str | None = Field(None, description="Função, área ou departamento.")
    atuacao: str | None = Field(
        None, description="O que faz no dia a dia e o que acompanha, em uma frase."
    )
    local_atuacao: str | None = Field(
        None, description="Planta, site, região ou unidade de que cuida."
    )
    equipe_gestor: str | None = Field(None, description="Equipe a que pertence ou gestor.")
    outros: list[str] = Field(
        default_factory=list,
        description="Outros fatos sobre a pessoa que ela declarou e não cabem nos campos "
        "acima. Uma frase curta por item.",
    )


# ---------------------------------------------------------------------------
# Saída e instruções do extrator
# ---------------------------------------------------------------------------
Destino = Literal[
    "licao_verificada", "licao_hipotese", "preferencia", "perfil_outros",
    "perfil_nome", "perfil_cargo", "perfil_funcao_area", "perfil_atuacao",
    "perfil_local_atuacao", "perfil_equipe_gestor",
]


class Operacao(BaseModel):
    """Uma mudança em UM documento. O destino diz qual documento e qual lista ou campo."""

    destino: Destino
    acao: Literal["adicionar", "remover", "substituir"]
    antigo: str = Field(
        default="",
        description="Texto EXATO do item existente que sai (remover ou substituir). Vazio ao adicionar.",
    )
    novo: str = Field(
        default="", description="Texto do item que entra (adicionar ou substituir). Vazio ao remover."
    )


class MudancasMemoria(BaseModel):
    """As mudanças a aplicar nos três documentos. Cada declaração vai para UM só destino."""

    raciocinio: str = Field(
        description="Uma ou duas frases: o que você decidiu e por quê. Inclua os casos em que "
        "decidiu não registrar nada."
    )
    operacoes: list[Operacao] = Field(
        default_factory=list, description="Vazia quando não há nada a mudar."
    )


INSTRUCOES = """\
Você mantém a memória de um agente que analisa dados para os usuários. A memória tem TRÊS \
documentos, e você recebe o conteúdo atual deles e as mensagens de um ou mais turnos. \
Devolva SÓ as operações necessárias; a lista fica vazia quando não há nada a mudar. Escreva \
os itens em português, uma frase curta cada. Mensagens marcadas com [CONTEXTO] já foram \
processadas antes; use-as só para entender o que o usuário diz.

CADA OPERAÇÃO tem um destino, uma ação, o texto antigo e o texto novo:
- acao "adicionar": preencha só "novo".
- acao "remover": preencha só "antigo", com o texto EXATO do item existente, copiado dos \
documentos atuais.
- acao "substituir": preencha "antigo" (texto EXATO do item existente) e "novo". Use \
para reescrever, atualizar ou corrigir um item. Nunca adicione o novo e deixe o antigo \
contraditório no lugar.

OS DESTINOS, e quando algo entra em cada um (uma declaração entra em UM só):

1. LIÇÕES sobre o banco de dados (compartilhadas por todos os usuários). Ajudam o agente a \
não repetir erros ao consultar.
   - licao_verificada: (a) erro técnico verificado: a ferramenta devolveu erro (coluna ou \
tabela inexistente, tipo errado, join inválido) e uma nova tentativa funcionou; (b) regra \
declarada pelo usuário como permanente sobre QUAL dado, coluna, moeda ou filtro usar e COMO \
calcular, filtrar, somar ou juntar ("sempre", "nunca", "o correto é"), mesmo que ele a diga \
com "mostre" ou "apresente". Registre-a SEMPRE que aparecer, mesmo que pareça específica de \
um cálculo: o usuário sabe do que fala.
   - licao_hipotese: contestação sem causa declarada ("esse total está estranho, refaz"), \
SOMENTE se houver indício concreto no SQL ou no resultado (join sem chave, filtro de período \
omitido, agregação duplicando linhas). Sem indício, nada. Uma hipótese confirmada passa a \
verificada: remova-a de licao_hipotese e adicione em licao_verificada.

2. preferencia: como o usuário quer receber os resultados, e só o que ele DECLARA como \
permanente: "prefiro", "sempre que possível", "por padrão", "daqui para frente", "a partir \
de agora" ou equivalentes em inglês e chinês. Cor, tipo de gráfico, formato, nível de \
detalhe, idioma, ordenação. Nunca por inferência, repetição ou aprovação.

3. PERFIL: quem é o usuário, só o que ELE afirma sobre si ("meu nome é Márcio", "sou gerente \
de supply", "cuido dos sites JAG e BPL", "respondo ao João"). Cada campo guarda um valor só \
(adicionar e substituir definem o valor; remover apaga): perfil_nome (como quer ser chamado), \
perfil_cargo, perfil_funcao_area (função, área ou departamento), perfil_atuacao (o que faz e \
acompanha, em uma frase), perfil_local_atuacao (planta, site, região), perfil_equipe_gestor. \
O que não couber nos campos vai em perfil_outros (lista de frases). Nunca deduza nada do que \
ele pergunta.

ONDE CADA COISA VAI, nos casos de fronteira:
- Qual dado, moeda, coluna, filtro ou cálculo usar é LIÇÃO, nunca preferência, mesmo dito \
como "mostre em USD". Só a FORMA de apresentar é preferência.
- Se ficar na dúvida entre lição e preferência, é lição. Nunca registre a mesma coisa em \
dois destinos.

NÃO REGISTRE EM NENHUM:
- Pedidos e perguntas pontuais, mesmo repetidos ou explícitos: "faça um gráfico de barras", \
"mostre em tabela", "nessa análise, só o site JAG". Pedir não é declarar.
- Valores, resultados ou números dos dados.
- Dados pessoais sensíveis ou de contato: e-mail, telefone, endereço, documentos, saúde, \
religião, política, senhas ou credenciais, mesmo que o usuário os informe.
- Informações sobre terceiros, exceto a equipe ou o gestor dele.
- Contestações cuja causa você não sustenta com as mensagens.

CONSOLIDAÇÃO:
- Compare com o conteúdo atual antes de adicionar: se já existe a mesma coisa, não repita.
- Para trocar ou reescrever um item (mudou de cor, de cargo, de regra), use "substituir" com \
o texto EXATO do item antigo.
- Remova ou substitua um item só se as mensagens trouxerem evidência que o contraria ou o \
usuário pedir para esquecer ("esqueça meu nome", "não sou mais gerente"). Nunca para abrir \
espaço, nem porque ele não apareceu neste turno.
- Os documentos atuais NÃO indicam que a memória está completa: avalie as mensagens contra \
os critérios acima como se os documentos estivessem vazios, e só então compare.
- Limites: 30 lições no total, 20 preferências, 10 itens em perfil_outros.

EXEMPLOS (nomes fictícios; use os reais das mensagens):
- Erro: o agente consulta a coluna "data" de producao, a ferramenta diz que não existe e \
refaz com "dt_producao". Operação: destino licao_verificada, acao adicionar, novo "Na tabela \
producao, a coluna de data é dt_producao (não data)".
- "Sempre desconsidere as paradas programadas no OEE": licao_verificada, adicionar, novo "Ao \
calcular OEE, desconsiderar paradas programadas".
- "Sempre mostre o custo em USD, nunca somando moedas locais": LIÇÃO, não preferência. \
licao_verificada, adicionar, novo "Apresentar e calcular o custo apenas em USD; nunca somar \
moedas locais".
- "Prefiro sempre gráficos na cor laranja": preferencia, adicionar, novo "Gráficos na cor \
laranja".
- "Faça um gráfico de barras" ou "Agora quero em linha": pedido pontual. operacoes vazia.
- "Na verdade prefiro gráficos de linha", com "Usar gráficos de barras" já gravada: \
preferencia, substituir, antigo "Usar gráficos de barras", novo "Usar gráficos de linha".
- "Agora pode mostrar também em BRL, a regra de só USD não vale mais", com a regra de USD \
gravada: licao_verificada, substituir, antigo (texto exato da regra de USD), novo "Os custos \
podem ser apresentados também em BRL, além de USD".
- "Me chame de Márcio, sou gerente de supply": perfil_nome, adicionar, novo "Márcio"; \
perfil_cargo, adicionar, novo "Gerente de supply".
- "O meu nome é Marcio, não Marico", com "Marico" gravado: perfil_nome, substituir, novo \
"Marcio".
- "Toda segunda apresento esses números ao diretor": perfil_outros, adicionar, novo \
"Apresenta os números ao diretor toda segunda-feira".
- "Esqueça meu cargo": perfil_cargo, remover.
- "Nessa análise, considere só os turnos completos": pontual. operacoes vazia.
- "Esse total está estranho, refaz", com o SQL juntando producao e paradas só por linha_id: \
licao_hipotese, adicionar, novo "Ao juntar producao e paradas, incluir a data na chave do join".
- "Não gostei, faz de novo", sem indício no SQL: operacoes vazia.
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
_executor: ReflectionExecutor | None = None


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
    _, conteudo = _ler_doc(("users", _seg(user_id), _seg(dominio), "preferences"), "PreferenciasUsuario")
    try:
        return PreferenciasUsuario(preferencias=conteudo.get("preferencias") or [])
    except Exception:
        logger.warning("[memoria] preferências ilegíveis de %s; ignoradas", user_id)
        return PreferenciasUsuario()


# ---------------------------------------------------------------------------
# Leitura: blocos para injetar nos prompts
# ---------------------------------------------------------------------------
def ler_perfil(user_id: str, dominio: str) -> PerfilUsuario:
    _, conteudo = _ler_doc(("users", _seg(user_id), _seg(dominio), "profile"), "PerfilUsuario")
    try:
        return PerfilUsuario(**{k: v for k, v in conteudo.items() if k in PerfilUsuario.model_fields})
    except Exception:
        logger.warning("[memoria] perfil ilegível de %s; ignorado", user_id)
        return PerfilUsuario()


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
        partes.append(
            "Regras confirmadas, siga-as. Esta lista pode estar atrasada: se o pedido atual "
            "trouxer uma regra diferente ou contrária, vale o pedido.\n"
            + "\n".join(f"- {r}" for r in verificadas)
        )
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
    linhas = [f"- {x}" for x in p.preferencias]
    if not linhas:
        return ""
    return (
        "\n\n## Preferências deste usuário (memória de conversas anteriores)\n"
        "São instruções do usuário, não sugestões: siga-as em toda resposta. Esta lista pode "
        "estar atrasada: o que o usuário disse NESTA conversa vale mais que ela. Se ele trocou, "
        "desfez ou contrariou uma preferência na conversa, ou se o pedido atual diz o "
        "contrário, siga a conversa.\n" + "\n".join(linhas)
    )


_ROTULOS_PERFIL = (
    ("nome", "Nome"), ("cargo", "Cargo"), ("funcao_area", "Função/área"),
    ("atuacao", "Atuação"), ("local_atuacao", "Local de atuação"), ("equipe_gestor", "Equipe/gestor"),
)


def bloco_perfil(user_id: str, dominio: str) -> str:
    """Texto do perfil do usuário para o orquestrador. Vazio se não houver user_id ou nada gravado."""
    if not ativa() or _store is None or not user_id:
        return ""
    try:
        p = ler_perfil(user_id, dominio)
    except Exception:
        logger.warning("[memoria] falha ao ler perfil", exc_info=True)
        return ""
    linhas = [f"- {rotulo}: {getattr(p, campo)}" for campo, rotulo in _ROTULOS_PERFIL if getattr(p, campo)]
    linhas += [f"- {x}" for x in p.outros]
    if not linhas:
        return ""
    return (
        "\n\n## Perfil deste usuário (o que ele mesmo contou em conversas anteriores)\n"
        "Use para se dirigir a ele pelo nome e ajustar o tom. Não o repita nem o cite sem "
        "necessidade, e não o trate como dado da análise. Pode estar atrasado: se nesta "
        "conversa ele corrigiu ou mudou algo (nome, cargo, gestor), vale a conversa.\n"
        + "\n".join(linhas)
    )


# ---------------------------------------------------------------------------
# Aplicação das operações propostas pelo extrator
# ---------------------------------------------------------------------------
_DESTINOS_LISTA = {
    "licao_verificada": ("licoes", "verificadas"),
    "licao_hipotese": ("licoes", "hipoteses"),
    "preferencia": ("preferencias", "preferencias"),
    "perfil_outros": ("perfil", "outros"),
}
_DESTINOS_CAMPO = {
    "perfil_nome": "nome", "perfil_cargo": "cargo", "perfil_funcao_area": "funcao_area",
    "perfil_atuacao": "atuacao", "perfil_local_atuacao": "local_atuacao",
    "perfil_equipe_gestor": "equipe_gestor",
}
# Se o extrator puser a mesma frase em dois destinos, fica o primeiro desta lista: quem diz qual
# dado ou cálculo usar é lição, e só a forma de apresentar é preferência.
_PRIORIDADE = [
    "licao_verificada", "licao_hipotese", *_DESTINOS_CAMPO, "perfil_outros", "preferencia",
]


def _chave(txt: str) -> str:
    return " ".join(txt.lower().split()).strip(" .;:,!")


def _semelhanca(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _chave(a), _chave(b)).ratio()


def _parecido_com_algum(texto: str, itens: list[str]) -> bool:
    return any(_semelhanca(texto, x) >= _IGUAL for x in itens)


def _casar(texto: str, itens: list[str]) -> int | None:
    """Índice do item que o extrator citou: o texto igual (sem contar maiúsculas, espaços e
    pontuação final) ou, na falta dele, o mais parecido. Com dois candidatos quase empatados a
    citação é ambígua e nada é alterado, para não mexer no item errado."""
    if not texto.strip():
        return None
    alvo = _chave(texto)
    for i, item in enumerate(itens):
        if _chave(item) == alvo:
            return i
    pontuados = sorted(((_semelhanca(texto, x), i) for i, x in enumerate(itens)), reverse=True)
    if not pontuados or pontuados[0][0] < _CITADA:
        return None
    if len(pontuados) > 1 and pontuados[1][0] >= _CITADA and pontuados[0][0] - pontuados[1][0] < _EMPATE:
        logger.warning("[memoria] citação ambígua, nada foi alterado: %s", texto)
        return None
    return pontuados[0][1]


def _sem_duplicata_entre_destinos(ops: list[Operacao]) -> list[Operacao]:
    """A mesma frase em dois destinos diferentes fica só no de maior prioridade."""
    ordem = {d: i for i, d in enumerate(_PRIORIDADE)}
    mantidas: list[Operacao] = []
    for op in ops:
        if op.acao != "remover" and op.novo.strip():
            outra = next(
                (m for m in mantidas
                 if m.acao != "remover" and m.destino != op.destino and m.novo.strip()
                 and _semelhanca(m.novo, op.novo) >= _IGUAL),
                None,
            )
            if outra is not None:
                perdedora, vencedora = (op, outra) if ordem[outra.destino] <= ordem[op.destino] else (outra, op)
                logger.warning("[memoria] mesma frase em dois destinos (%s e %s); fica só em %s: %s",
                               outra.destino, op.destino, vencedora.destino, op.novo)
                if perdedora is op:
                    continue
                mantidas.remove(outra)
        mantidas.append(op)
    return mantidas


def _itens(valor) -> list[str]:
    return [x.strip() for x in (valor or []) if isinstance(x, str) and x.strip()]


def _aplicar_em_lista(docs: dict, op: Operacao) -> None:
    doc, chave = _DESTINOS_LISTA[op.destino]
    itens = docs[doc][chave]
    antigo, novo = op.antigo.strip(), op.novo.strip()
    if op.acao == "remover":
        i = _casar(antigo or novo, itens)
        if i is None:
            logger.info("[memoria] item a remover não encontrado em %s: %s", op.destino, antigo or novo)
            return
        (logger.warning if op.destino == "licao_verificada" else logger.info)(
            "[memoria] item retirado de %s: %s", op.destino, itens[i])
        itens.pop(i)
        return
    if not novo:
        logger.info("[memoria] operação %s sem texto novo em %s; ignorada", op.acao, op.destino)
        return
    if op.acao == "substituir" and antigo:
        i = _casar(antigo, itens)
        if i is not None:
            duplicata = any(k != i and _semelhanca(x, novo) >= _IGUAL for k, x in enumerate(itens))
            if duplicata:  # o novo já existe em outro item: só retira o antigo
                itens.pop(i)
            else:
                itens[i] = novo
            return
        logger.info("[memoria] item a substituir não encontrado em %s (tratado como adição): %s",
                    op.destino, antigo)
    if op.destino == "licao_hipotese" and _parecido_com_algum(novo, docs["licoes"]["verificadas"]):
        return  # já é uma regra confirmada
    if op.destino == "licao_verificada":  # a hipótese virou regra confirmada
        hipoteses = docs["licoes"]["hipoteses"]
        j = next((k for k, x in enumerate(hipoteses) if _semelhanca(x, novo) >= _IGUAL), None)
        if j is not None:
            hipoteses.pop(j)
    if not _parecido_com_algum(novo, itens):
        itens.append(novo)


def _aplicar_em_campo(perfil: dict, op: Operacao) -> None:
    campo = _DESTINOS_CAMPO[op.destino]
    if op.acao == "remover":
        perfil[campo] = None
    elif op.novo.strip():
        perfil[campo] = op.novo.strip()
    else:
        logger.info("[memoria] operação %s sem valor em %s; ignorada", op.acao, op.destino)


def aplicar_operacoes(
    ops: list[Operacao], lic: dict, pref: dict, perfil: dict, tem_usuario: bool = True
) -> tuple[dict, dict, dict]:
    """Aplica as operações aos três documentos e devolve os novos conteúdos (os de entrada
    não são alterados). Sem usuário identificado só as lições mudam. Impõe os limites: 30
    lições (descarta hipóteses primeiro), 20 preferências e 10 itens livres no perfil."""
    docs = {
        "licoes": {"verificadas": _itens(lic.get("verificadas")), "hipoteses": _itens(lic.get("hipoteses"))},
        "preferencias": {"preferencias": _itens(pref.get("preferencias"))},
        "perfil": {
            **{c: (perfil.get(c) or None) for c in PerfilUsuario.model_fields if c != "outros"},
            "outros": _itens(perfil.get("outros")),
        },
    }
    for op in _sem_duplicata_entre_destinos(ops):
        if not tem_usuario and (op.destino == "preferencia" or op.destino.startswith("perfil_")):
            continue
        if op.destino in _DESTINOS_CAMPO:
            _aplicar_em_campo(docs["perfil"], op)
        else:
            _aplicar_em_lista(docs, op)
    v, h = docs["licoes"]["verificadas"], docs["licoes"]["hipoteses"]
    while len(v) + len(h) > MAX_LICOES:
        (h or v).pop(0)
    docs["preferencias"]["preferencias"] = docs["preferencias"]["preferencias"][-MAX_PREFERENCIAS:]
    docs["perfil"]["outros"] = docs["perfil"]["outros"][-MAX_OUTROS:]
    return docs["licoes"], docs["preferencias"], docs["perfil"]


def _gravar(ns: tuple[str, ...], kind: str, antes: dict, depois: dict) -> bool:
    """Grava o documento se mudou. Devolve True se gravou."""
    if depois == antes:
        return False
    chave, _ = _ler_doc(ns, kind)
    _store.put(ns, chave or "default", {"kind": kind, "content": depois})
    logger.info("[memoria] %s atualizado em %s", kind, "/".join(ns))
    return True


# ---------------------------------------------------------------------------
# Extrator (o que o ReflectionExecutor executa)
# ---------------------------------------------------------------------------
_traces: dict[str, list[str]] = defaultdict(list)    # session_id -> traces do turno em curso
_turnos: dict[str, list[dict]] = defaultdict(list)   # session_id -> turnos recentes
_base: dict[str, int] = defaultdict(int)             # session_id -> turnos já descartados do início
_extraidos: dict[str, int] = defaultdict(int)        # session_id -> turnos extraídos (absoluto)
_futuros: list[Future] = []
_lock = threading.Lock()


def _cortar(txt: str, n: int) -> str:
    txt = str(txt)
    return txt if len(txt) <= n else txt[:n] + "…"


def _texto_conversa(msgs: list[dict]) -> str:
    return "\n\n".join(
        ("USUÁRIO: " if m["role"] == "user" else "AGENTE: ") + m["content"] for m in msgs
    )


def _conteudo_para_o_llm(msgs: list[dict], lic: dict, pref: dict, perfil: dict, tem_usuario: bool) -> str:
    atuais = {"licoes": {"verificadas": lic.get("verificadas") or [], "hipoteses": lic.get("hipoteses") or []}}
    if tem_usuario:
        atuais["preferencias"] = pref.get("preferencias") or []
        atuais["perfil"] = {
            **{c: perfil.get(c) for c in PerfilUsuario.model_fields if c != "outros"},
            "outros": perfil.get("outros") or [],
        }
    aviso = "" if tem_usuario else (
        "\nNão há usuário identificado: preencha SÓ as listas de lições; deixe preferências e perfil vazios.\n"
    )
    return (
        "DOCUMENTOS ATUAIS (JSON):\n" + json.dumps(atuais, ensure_ascii=False, indent=1)
        + aviso + "\n\nMENSAGENS:\n" + _texto_conversa(msgs)
    )


class _Extrator:
    """Uma chamada estruturada ao LLM por extração; o código aplica o resultado.

    O ReflectionExecutor só exige um atributo `namespace` (os namespaces reais dependem do
    domínio e do usuário de cada extração, vindos de `_meta`) e um `invoke(payload)`.

    O ponteiro de turnos extraídos avança aqui dentro, antes de o Future ser concluído, e
    não num callback do Future: o `wait()` acorda antes de os callbacks rodarem, e um
    callback registrado com `_lock` preso numa tarefa já concluída rodaria na hora e
    travaria no mesmo lock."""

    namespace = ()

    def __init__(self, llm):
        self._llm = llm.with_structured_output(MudancasMemoria)

    def invoke(self, payload, config=None):
        meta = payload.get("_meta") or {}
        try:
            self._extrair(payload["messages"], meta.get("user_id"), meta["dominio"])
        except Exception as e:
            # o ponteiro não avança: esses turnos voltam na próxima extração da sessão
            logger.warning("[memoria] falha na extração (sessão %s): %r", meta.get("sessao"), e)
            return []
        with _lock:
            _extraidos[meta.get("sessao")] = max(_extraidos[meta.get("sessao")], meta.get("n_abs", 0))
        logger.info("[memoria] extração concluída (sessão %s)", meta.get("sessao"))
        return []

    def _extrair(self, msgs: list[dict], user_id: str | None, dominio: str) -> None:
        ns_lic = (_NS_LICOES, _seg(dominio))
        ns_pref = ("users", _seg(user_id), _seg(dominio), "preferences") if user_id else None
        ns_perf = ("users", _seg(user_id), _seg(dominio), "profile") if user_id else None
        _, lic = _ler_doc(ns_lic, "LicoesDoBanco")
        _, pref = _ler_doc(ns_pref, "PreferenciasUsuario") if user_id else (None, {})
        _, perfil = _ler_doc(ns_perf, "PerfilUsuario") if user_id else (None, {})

        conteudo = _conteudo_para_o_llm(msgs, lic, pref, perfil, bool(user_id))
        if os.getenv("MEMORIA_DEBUG", "0").strip() == "1":
            logger.info("[memoria] payload enviado ao extrator:\n%s", conteudo)
        m = self._llm.invoke([SystemMessage(content=INSTRUCOES), HumanMessage(content=conteudo)])
        if not isinstance(m, MudancasMemoria):
            raise ValueError(f"o modelo não devolveu as mudanças estruturadas: {_cortar(repr(m), 200)}")
        logger.info("[memoria] extrator: %s", _cortar(m.raciocinio, 600))

        lic_novo, pref_novo, perfil_novo = aplicar_operacoes(m.operacoes, lic, pref, perfil, bool(user_id))
        _gravar(ns_lic, "LicoesDoBanco", _normalizar_licoes(lic), lic_novo)
        if user_id:
            _gravar(ns_pref, "PreferenciasUsuario", _normalizar_prefs(pref), pref_novo)
            _gravar(ns_perf, "PerfilUsuario", _normalizar_perfil(perfil), perfil_novo)


def _normalizar_licoes(doc: dict) -> dict:
    return {"verificadas": _itens(doc.get("verificadas")), "hipoteses": _itens(doc.get("hipoteses"))}


def _normalizar_prefs(doc: dict) -> dict:
    return {"preferencias": _itens(doc.get("preferencias"))}


def _normalizar_perfil(doc: dict) -> dict:
    return {
        **{c: (doc.get(c) or None) for c in PerfilUsuario.model_fields if c != "outros"},
        "outros": _itens(doc.get("outros")),
    }


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


def _enviar(sessao: str, user_id: str, dominio: str, turnos: list[dict], base: int) -> None:
    ja = _extraidos[sessao]
    # um turno de contexto antes dos pendentes: a contestação chega junto com a análise
    # contestada, mesmo que ela já tenha sido extraída antes.
    inicio = max(0, ja - base - 1)
    janela = turnos[inicio:]
    n_contexto = (ja - base) - inicio if ja > base else 0
    if not janela or ja >= base + len(turnos):
        return
    # Com usuário identificado a extração roda em todo turno: uma declaração de preferência ou
    # de perfil pode vir em qualquer mensagem. Sem usuário só há lições, e extraí-las custa uma
    # chamada de LLM, então só se há o que aprender: um trace do analista (erro técnico,
    # contestação) ou o usuário declarando uma regra permanente.
    if not user_id and not (
        any(t["trace"] for t in janela)
        or any(_DECLARA_REGRA.search(t["pergunta"]) for t in janela[n_contexto:])
    ):
        return
    logger.info("[memoria] agendando extração (sessão %s, %d turnos na janela)", sessao, len(janela))
    fut = _executor.submit(
        {
            "messages": _mensagens(janela, n_contexto),
            "_meta": {"sessao": sessao, "n_abs": base + len(turnos), "user_id": user_id, "dominio": dominio},
        },
        config={"configurable": {}},
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
            _enviar(session_id, user_id, dominio, list(lista), _base[session_id])
    except Exception:
        logger.warning("[memoria] falha ao agendar a extração", exc_info=True)


# ---------------------------------------------------------------------------
# Inicialização e encerramento
# ---------------------------------------------------------------------------
def init(llm) -> None:
    """Chamada em init_multi_agent com o modelo do extrator."""
    global _store, _executor
    try:
        # conexão própria: o worker do executor roda em outra thread. O SqliteStore exige
        # autocommit (isolation_level=None) porque ele mesmo abre as transações.
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        store = _SqliteStoreSeguro(conn)
        store.setup()
        _executor = ReflectionExecutor(_Extrator(llm), store=store)
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
    if _executor is not None:
        try:
            _executor.shutdown(wait=False)
        except Exception:
            logger.warning("[memoria] falha ao encerrar o executor", exc_info=True)
