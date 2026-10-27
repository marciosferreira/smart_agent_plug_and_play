"""
Memória do agente: aprende sobre o banco e sobre o usuário a partir das conversas.

Quatro documentos e as fichas de análise, mantidos em background depois do turno, sem atrasar a resposta:

  LIÇÕES        -> regras sobre como consultar o banco. Um documento por domínio (compartilhado
                   por todos os usuários), com duas listas de texto: `verificadas` e `hipoteses`.
                   Lido pelo sub-agente analista, que é quem escreve o SQL.
  PREFERÊNCIAS  -> preferências que o usuário declarou ("prefiro", "sempre que possível"), em
                   frases livres, sem campos por assunto. Um documento por usuário e por domínio
                   (nada vaza de um domínio para outro). Lido pelo orquestrador (que redige a
                   resposta) e pelo analista (gráficos).
  CONVENÇÕES    -> como ESTE usuário quer que as análises sejam delimitadas ou calculadas por
                   padrão, sem ser uma definição do negócio (moeda de exibição, sites ou período
                   padrão, filtros que sempre aplica, o vocabulário dele). Um documento por usuário
                   e por domínio. Lido pelo sub-agente analista. Fica separado das lições para uma
                   correção pessoal não virar regra de todos.
  PERFIL        -> quem é o usuário, como ele mesmo contou (nome, cargo, área, atuação, local,
                   equipe ou gestor, e uma lista livre para o que não couber). Um documento por
                   usuário e por domínio. Lido só pelo orquestrador, que se dirige à pessoa.

  FICHAS        -> a memória de episódios: uma ficha por análise de dados, com a intenção do
                   usuário (o título), a abordagem que funcionou, a que falhou e por quê, e a
                   avaliação (👍, 👎, contestada e corrigida). Sem números. Ficam numa tabela
                   própria, com embedding, e são buscadas por similaridade para o analista e
                   para o orquestrador (ver fichas.py).

Uma ÚNICA chamada estruturada ao LLM (`_Extrator`) lê a conversa e os documentos e devolve
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
  MEMORIA_DELAY_SEGUNDOS     espera depois do último turno antes de extrair (padrão: 120)
  MEMORIA_MAX_PENDENTES      turnos ainda não extraídos que dispensam a espera (padrão: 2)
  MEMORIA_DEBUG              "1" loga o payload enviado ao extrator
  THINKING_LEVEL_MEMORIA     nível de raciocínio do extrator (padrão: medium; ver init_multi_agent)
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import logging
import os
import re
import sqlite3
import threading
from collections import defaultdict
from datetime import date
from concurrent.futures import Future, wait
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.store.sqlite import SqliteStore
from langmem import ReflectionExecutor
from pydantic import BaseModel, Field

from db import DB_PATH, guardar_abordagem
from feedback import inicio_comparavel, marca_da_nota
import fichas as _fichas

logger = logging.getLogger(__name__)

MAX_LICOES = 30
_MAX_ARGS = 800          # chars de entrada de ferramenta mostrados ao extrator
_MAX_ARGS_ABORDAGEM = 1200  # idem, no resumo da abordagem guardado por resposta
_MAX_SAIDA = 300         # chars de saída de ferramenta (corta dados de produção)
_RESPOSTA_CABECA = 500   # chars do começo da resposta final mostrados ao extrator
_RESPOSTA_CAUDA = 1800   # chars do fim: é onde ficam os blocos de parâmetros e de dados
_MAX_TURNOS_GUARDADOS = 6
MAX_PREFERENCIAS = 20
MAX_OUTROS = 10          # itens livres do perfil
MAX_CONVENCOES = 20
MAX_FICHAS_POR_VEZ = 3
_IGUAL = 0.9             # similaridade a partir da qual duas frases contam como a mesma
_CITADA = 0.8            # mínimo para o texto citado pelo extrator casar (de forma aproximada) com um item
_EMPATE = 0.05           # dois itens assim de parecidos com a citação: ambíguo, não mexe em nenhum

_NS_LICOES = "db_lessons"


def ativa() -> bool:
    return os.getenv("MEMORIA_ATIVA", "1").strip() != "0"


def _delay() -> int:
    try:
        return int(float(os.getenv("MEMORIA_DELAY_SEGUNDOS", "120")))
    except ValueError:
        return 120


def _teto_pendentes() -> int:
    try:
        return max(1, int(float(os.getenv("MEMORIA_MAX_PENDENTES", "2"))))
    except ValueError:
        return 2


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


class ConvencoesDoUsuario(BaseModel):
    """Convenções pessoais de análise do usuário, como frases livres.

    Diferem das lições (definições do banco e do negócio, valem para todos) e das preferências
    (forma de apresentar): são o jeito dele de delimitar ou calcular as análises por padrão.
    """

    convencoes: list[str] = Field(
        default_factory=list,
        description="Convenções que o usuário DECLAROU como permanentes para as análises dele, "
        "uma frase curta por item.",
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
    "licao_verificada", "convencao_pessoal", "preferencia", "perfil_outros",
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


class FichaAnalise(BaseModel):
    """Um episódio de análise de dados desta sessão (ver fichas.py). Descreve o que aconteceu;
    não é uma regra."""

    ficha_id: str = Field(
        default="", description="Vazio para criar uma ficha nova; o id de uma ficha DESTA sessão para atualizá-la."
    )
    titulo: str = Field(description="A intenção do usuário, normalizada, sem números, datas nem valores.")
    intencao: str = Field(default="", description="O que ele queria, em uma frase: métrica, recorte, período, filtros.")
    funcionou: str = Field(default="", description="A abordagem que deu certo: tabela, colunas, filtros, junção, período.")
    falhou: str = Field(default="", description="A abordagem que falhou e por quê. Vazio se nada falhou.")
    avaliacao: Literal["positiva", "negativa", "corrigida", "sem_avaliacao"] = "sem_avaliacao"


class MudancasMemoria(BaseModel):
    """As mudanças a aplicar nos documentos. Cada declaração vai para UM só destino."""

    raciocinio: str = Field(
        description="Uma ou duas frases: o que você decidiu e por quê. Inclua os casos em que "
        "decidiu não registrar nada."
    )
    operacoes: list[Operacao] = Field(
        default_factory=list, description="Vazia quando não há nada a mudar."
    )
    fichas: list[FichaAnalise] = Field(
        default_factory=list, description="Fichas de análise criadas ou atualizadas. No máximo 3."
    )


INSTRUCOES = """\
Você mantém a memória de um agente que analisa dados para os usuários. Você recebe o conteúdo \
atual da memória e as mensagens de um ou mais turnos, e devolve DUAS coisas: as operações nos \
documentos (lições, convenções, preferências e perfil) e as FICHAS DE ANÁLISE da sessão. As \
listas ficam vazias quando não há nada a mudar. Escreva em português, frases curtas. Mensagens \
marcadas com [CONTEXTO] já foram processadas antes; use-as só para entender o que o usuário diz.

CADA OPERAÇÃO tem um destino, uma ação, o texto antigo e o texto novo:
- acao "adicionar": preencha só "novo".
- acao "remover": preencha só "antigo", com o texto EXATO do item existente, copiado dos \
documentos atuais.
- acao "substituir": preencha "antigo" (texto EXATO do item existente) e "novo". Use \
para reescrever, atualizar ou corrigir um item. Nunca adicione o novo e deixe o antigo \
contraditório no lugar.

OS DESTINOS DAS OPERAÇÕES, e quando algo entra em cada um (uma declaração entra em UM só):

1. licao_verificada: LIÇÕES COMPARTILHADAS sobre o banco e o negócio (valem para TODOS os \
usuários). São fatos sobre o que os dados significam e como se calcula certo: qual tabela, \
coluna, join, filtro ou fórmula é o adequado para um tipo de análise, o que um campo quer \
dizer. Entram: (a) erro técnico verificado: a ferramenta devolveu erro (coluna ou tabela \
inexistente, tipo errado, join inválido) e uma nova tentativa funcionou; (b) correção ou regra \
declarada pelo usuário sobre o SIGNIFICADO ou o CÁLCULO certo ("o correto é", "sempre", \
"nunca", "o custo considera só...", "esse join está errado"). Registre-a SEMPRE que aparecer, \
mesmo que pareça específica de um cálculo: o usuário sabe do que fala. Uma contestação SEM \
causa declarada ("esse total está estranho, refaz") não é lição: vai para a ficha da análise.

2. convencao_pessoal: como ESTE usuário quer que as análises dele sejam delimitadas ou \
calculadas por padrão, sem ser uma definição do negócio, e só o que ele DECLARA como \
permanente: moeda em que quer ver os valores, sites, produtos ou período padrão, filtros que \
ele sempre aplica, o vocabulário dele ("quando digo operação, quero dizer site"). Vale só \
para ele. NA DÚVIDA ENTRE LIÇÃO COMPARTILHADA E CONVENÇÃO PESSOAL, É CONVENÇÃO PESSOAL: a \
compartilhada muda as análises de todos, então só entra o que é claramente um fato do \
negócio ou do banco.

3. preferencia: como o usuário quer que os resultados sejam APRESENTADOS, e só o que ele \
DECLARA como permanente: "prefiro", "sempre que possível", "por padrão", "daqui para frente", \
"a partir de agora" ou equivalentes em inglês e chinês. Cor, tipo de gráfico, formato, nível \
de detalhe, idioma, ordenação. Nunca por inferência, repetição ou aprovação.

4. PERFIL: quem é o usuário, só o que ELE afirma sobre si ("meu nome é Márcio", "sou gerente \
de supply", "cuido dos sites JAG e BPL", "respondo ao João"). Cada campo guarda um valor só \
(adicionar e substituir definem o valor; remover apaga): perfil_nome (como quer ser chamado), \
perfil_cargo, perfil_funcao_area (função, área ou departamento), perfil_atuacao (o que faz e \
acompanha, em uma frase), perfil_local_atuacao (planta, site, região), perfil_equipe_gestor. \
O que não couber nos campos vai em perfil_outros (lista de frases). Nunca deduza nada do que \
ele pergunta.

ONDE CADA COISA VAI, nos casos de fronteira:
- Definição do negócio ou jeito certo de calcular: lição compartilhada. Escolha pessoal de \
escopo, moeda ou filtro padrão: convenção pessoal. Só a FORMA de apresentar: preferência.
- Se a mesma frase do usuário trouxer duas coisas diferentes, registre cada uma no seu \
destino, com textos diferentes. Nunca registre a MESMA coisa em dois destinos.

COMO LER UMA CORREÇÃO:
- As respostas do AGENTE terminam com um bloco de parâmetros (em português "Parâmetros da \
análise", em inglês "Analysis parameters", ou o equivalente no idioma) que lista o período, \
as tabelas e os filtros realmente usados; o que o agente assumiu sem o usuário pedir vem \
marcado. Uma correção do usuário quase sempre contesta um desses parâmetros ou uma etapa do \
cálculo. Descubra qual e escreva a lição como a abordagem CORRETA para aquele tipo de \
análise (qual tabela, coluna, filtro ou join usar), e não só "o resultado estava errado".
- Escreva a lição como uma receita curta: o tipo de análise e como fazer. Por exemplo: "Para \
custo por hora por site, usar a taxa vigente mais recente por site, produto e área, e não a \
média do período".

FICHAS DE ANÁLISE (lista "fichas"):
- Uma ficha descreve UM episódio de ANÁLISE DE DADOS desta sessão: o que o usuário queria, \
como o agente fez, o que falhou e por quê, e como o usuário avaliou. Não é uma regra: outras \
análises, deste e de outros usuários, vão consultá-la como referência. Só crie ficha quando o \
agente consultou dados de fato (há consultas do analista que devolveram dados).
- Falha de infraestrutura (serviço fora do ar, API não configurada, sem permissão, timeout) \
não é abordagem: se nenhuma consulta chegou aos dados, não crie ficha, e nunca registre esse \
tipo de erro em falhou. falhou é para abordagem errada (tabela, coluna, filtro, junção, cálculo).
- Um episódio é a pergunta de dados e os desdobramentos dela: contestação, refeitura, \
avaliação. Uma pergunta que muda a intenção (outra métrica, outro recorte) é outro episódio.
- Você recebe as fichas que já existem nesta sessão (fichas_desta_sessao, com id). Se as \
mensagens continuam um episódio que já tem ficha (uma contestação, a refeitura, uma \
avaliação), ATUALIZE-a: repita o ficha_id e devolva a ficha inteira, revista. Só crie uma \
ficha nova (ficha_id vazio) para um episódio novo.
- titulo: a INTENÇÃO do usuário, normalizada, sem números, datas nem valores: "Custo de mão de \
obra direta por site em um mês", e não "custo de setembro". intencao: o que ele queria, em \
uma frase (métrica, recorte, período, filtros pedidos).
- funcionou: a abordagem que deu certo (tabela, colunas, filtros, junção, regra de período, \
verificações). falhou: SÓ uma abordagem que de fato estava errada, e por quê: um erro da \
ferramenta que outra abordagem resolveu, ou uma contestação que se confirmou (o agente mudou \
de abordagem, ou o usuário disse a causa). Contestação que NÃO se confirmou (o agente conferiu e \
a abordagem estava certa) não vai para falhou: no máximo acrescente em funcionou "conferido \
após contestação". Deixe vazio o que não houver.
- avaliacao: VALE O ÚLTIMO SINAL do usuário sobre o episódio. "positiva": marcou 👍 ou aprovou \
por escrito ("agora sim", "é isso mesmo"). "negativa": marcou 👎, ou contestou de novo depois \
de o agente refazer ou conferir. "corrigida": contestou e o agente refez de OUTRO jeito (um 👍 \
depois da refeitura mantém "corrigida": a correção foi aprovada). "sem_avaliacao": nenhum sinal. \
Uma contestação que não se confirmou (o agente conferiu e manteve a abordagem) não é sinal: \
MANTENHA a avaliacao que a ficha já tinha (um 👍 anterior continua "positiva").
- NUNCA números, totais, percentuais, rankings ou comparações de valores, em nenhum campo. Os \
números mudam; a abordagem não. Pode citar tabelas, colunas e filtros.

AVALIAÇÃO DAS RESPOSTAS (👍 e 👎):
- Uma resposta do AGENTE pode vir marcada com "[AVALIAÇÃO DO USUÁRIO ...]": ele clicou em 👍 \
(boa) ou 👎 (ruim). É um SINAL sobre aquela resposta, que entra na avaliacao da ficha do \
episódio. A marca não diz o que vem depois: a mensagem seguinte do usuário pode não ter \
relação nenhuma com a resposta avaliada.
- 👎 seguido de uma mensagem que diz o que estava ERRADO (tabela, coluna, filtro, junção, \
cálculo ou premissa): registre em falhou, com a causa; se a causa é um fato ou regra do banco, \
também vira licao_verificada. 👎 seguido de outra pergunta, ou sem explicação: só avaliacao \
"negativa", sem inventar a causa.
- ERRO NÃO É PEDIDO DE OUTRO RECORTE: "quero por fábrica, não por site", "só o site JAG", \
"agora por trimestre", "mostre em gráfico" são pedidos de outro recorte ou formato. A resposta \
não estava errada: não registre em falhou nem nas lições. Se mudou a intenção, é outro episódio.
- Nenhuma avaliação, sozinha, cria convenção, preferência ou perfil.

NÃO REGISTRE EM NENHUM LUGAR:
- Pedidos e perguntas pontuais como convenção ou preferência, mesmo repetidos ou explícitos: \
"faça um gráfico de barras", "mostre em tabela", "nessa análise, só o site JAG". Pedir não é \
declarar.
- Valores, resultados ou números dos dados.
- Dados pessoais sensíveis ou de contato: e-mail, telefone, endereço, documentos, saúde, \
religião, política, senhas ou credenciais, mesmo que o usuário os informe.
- Informações sobre terceiros, exceto a equipe ou o gestor dele.

CONSOLIDAÇÃO:
- Compare com o conteúdo atual antes de adicionar: se já existe a mesma coisa, não repita.
- Para trocar ou reescrever um item (mudou de cor, de cargo, de regra), use "substituir" com \
o texto EXATO do item antigo.
- Remova ou substitua um item só se as mensagens trouxerem evidência que o contraria ou o \
usuário pedir para esquecer ("esqueça meu nome", "não sou mais gerente"). Nunca para abrir \
espaço, nem porque ele não apareceu neste turno.
- Os documentos atuais NÃO indicam que a memória está completa: avalie as mensagens contra \
os critérios acima como se os documentos estivessem vazios, e só então compare.
- Limites: 30 lições, 20 convenções pessoais, 20 preferências, 10 itens em perfil_outros, e no \
máximo 3 fichas por vez.

EXEMPLOS (nomes fictícios; use os reais das mensagens):
- Erro: o agente consulta a coluna "data" de producao, a ferramenta diz que não existe e \
refaz com "dt_producao". licao_verificada, adicionar, novo "Na tabela producao, a coluna de \
data é dt_producao (não data)".
- "O custo de mão de obra deve considerar só registros aprovados": definição do negócio. \
licao_verificada, adicionar, novo "Para custo de mão de obra, considerar apenas registros \
com status aprovado".
- "Esse resultado está errado, para taxa horária use a vigência mais recente, não a média", \
com o bloco de parâmetros mostrando a média do período: licao_verificada, adicionar, novo \
"Para taxa horária por site, usar a vigência mais recente de cada site, e não a média do \
período".
- "Nas minhas análises, considere só os sites BPL e MAN": convencao_pessoal, adicionar, novo \
"Considerar por padrão só os sites BPL e MAN".
- "Quando eu falo operação, quero dizer site": convencao_pessoal, adicionar, novo "Operação \
significa site".
- "Sempre mostre o custo em USD, e nunca some valores de moedas diferentes": duas coisas. \
convencao_pessoal, adicionar, novo "Mostrar os custos em USD"; licao_verificada, adicionar, \
novo "Nunca somar valores de moedas diferentes".
- "Prefiro sempre gráficos na cor laranja": preferencia, adicionar, novo "Gráficos na cor \
laranja".
- "Faça um gráfico de barras" ou "Agora quero em linha": pedido pontual. Nenhuma operação.
- "Na verdade prefiro gráficos de linha", com "Usar gráficos de barras" já gravada: \
preferencia, substituir, antigo "Usar gráficos de barras", novo "Usar gráficos de linha".
- "Me chame de Márcio, sou gerente de supply": perfil_nome, adicionar, novo "Márcio"; \
perfil_cargo, adicionar, novo "Gerente de supply".
- "O meu nome é Marcio, não Marico", com "Marico" gravado: perfil_nome, substituir, novo \
"Marcio".
- "Esqueça meu cargo": perfil_cargo, remover.
- O usuário pede o custo de mão de obra por site no último mês; o agente consulta \
fact_final_dl_cost filtrando status aprovado e soma dl_cost_by_cm_usd por site; o usuário \
marca 👍: ficha nova, titulo "Custo de mão de obra direta por site em um mês", intencao \
"Custo de mão de obra direta aprovado, agregado por site, num mês", funcionou \
"fact_final_dl_cost com status aprovado, somando dl_cost_by_cm_usd por site", avaliacao \
"positiva". Os valores da resposta ficam de fora.
- Na mesma sessão, ele diz "esse total está estranho, refaz" e o agente refaz tratando \
dl_cost_by_cm_usd como custo unitário: atualize a ficha desse episódio (o mesmo ficha_id): \
falhou "Somar dl_cost_by_cm_usd como se fosse o custo total desembolsado", funcionou "Tratar \
dl_cost_by_cm_usd como custo unitário por produto", avaliacao "corrigida". Nenhuma lição: ele \
não declarou a causa.
- A ficha tem avaliacao "positiva" (ele deu 👍) e depois ele diz "esse total está estranho, \
refaz"; o agente confere e mantém a mesma abordagem, que estava certa: atualize a ficha só em \
funcionou ("... conferido após contestação"); falhou continua vazio e avaliacao continua \
"positiva". Se ele contestar de novo depois disso, avaliacao "negativa".
- 👎 e a mensagem seguinte "faltou filtrar só os aprovados": na ficha, falhou "Não filtrou por \
status aprovado" e avaliacao "negativa" (ou "corrigida", se o agente refez); e \
licao_verificada "Para custo, considerar apenas registros com status aprovado".
- 👎 e a mensagem seguinte "que horas são?": na ficha, só avaliacao "negativa"; nada em falhou.
- "Quero por fábrica, não por site": pedido de outro recorte. Nada em falhou na ficha anterior; \
quando o agente consultar, uma ficha nova para o novo episódio.
- "Bom dia!" ou "que horas são?", sem consulta de dados: nenhuma ficha.
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


def ler_convencoes(user_id: str, dominio: str) -> ConvencoesDoUsuario:
    _, conteudo = _ler_doc(("users", _seg(user_id), _seg(dominio), "conventions"), "ConvencoesDoUsuario")
    try:
        return ConvencoesDoUsuario(convencoes=conteudo.get("convencoes") or [])
    except Exception:
        logger.warning("[memoria] convenções ilegíveis de %s; ignoradas", user_id)
        return ConvencoesDoUsuario()


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


def bloco_convencoes(user_id: str, dominio: str) -> str:
    """Texto das convenções pessoais para o prompt do analista. Vazio se não houver nada."""
    if not ativa() or _store is None or not user_id:
        return ""
    try:
        c = ler_convencoes(user_id, dominio)
    except Exception:
        logger.warning("[memoria] falha ao ler convenções", exc_info=True)
        return ""
    linhas = [f"- {x}" for x in c.convencoes]
    if not linhas:
        return ""
    return (
        "\n\n## Convenções deste usuário (como ele quer que as análises dele sejam feitas)\n"
        "São escolhas pessoais dele, não regras do banco: aplique-as quando o pedido não disser "
        "o contrário. Esta lista pode estar atrasada: se o pedido atual trouxer algo diferente, "
        "vale o pedido.\n" + "\n".join(linhas)
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
    "preferencia": ("preferencias", "preferencias"),
    "perfil_outros": ("perfil", "outros"),
    "convencao_pessoal": ("convencoes", "convencoes"),
}
_DESTINOS_CAMPO = {
    "perfil_nome": "nome", "perfil_cargo": "cargo", "perfil_funcao_area": "funcao_area",
    "perfil_atuacao": "atuacao", "perfil_local_atuacao": "local_atuacao",
    "perfil_equipe_gestor": "equipe_gestor",
}
# Se o extrator puser a mesma frase em dois destinos, fica o primeiro desta lista: quem diz qual
# dado ou cálculo usar é lição, e só a forma de apresentar é preferência.
# A convenção pessoal vem antes da lição: na dúvida entre as duas, vale a que só afeta o usuário.
_PRIORIDADE = [
    "convencao_pessoal", "licao_verificada", *_DESTINOS_CAMPO, "perfil_outros", "preferencia",
]
_DESTINOS_DO_USUARIO = {"convencao_pessoal", "preferencia", "perfil_outros", *_DESTINOS_CAMPO}


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
    ops: list[Operacao], lic: dict, pref: dict, perfil: dict, conv: dict, tem_usuario: bool = True
) -> tuple[dict, dict, dict, dict]:
    """Aplica as operações aos documentos e devolve os novos conteúdos (lições, preferências,
    perfil, convenções); os de entrada não são alterados. Sem usuário identificado só as
    lições mudam. Impõe os limites: 30 lições (descarta hipóteses primeiro), 20 preferências,
    20 convenções e 10 itens livres no perfil."""
    docs = {
        "licoes": {"verificadas": _itens(lic.get("verificadas")), "hipoteses": _itens(lic.get("hipoteses"))},
        "preferencias": {"preferencias": _itens(pref.get("preferencias"))},
        "convencoes": {"convencoes": _itens(conv.get("convencoes"))},
        "perfil": {
            **{c: (perfil.get(c) or None) for c in PerfilUsuario.model_fields if c != "outros"},
            "outros": _itens(perfil.get("outros")),
        },
    }
    for op in _sem_duplicata_entre_destinos(ops):
        if not tem_usuario and op.destino in _DESTINOS_DO_USUARIO:
            continue
        if op.destino in _DESTINOS_CAMPO:
            _aplicar_em_campo(docs["perfil"], op)
        else:
            _aplicar_em_lista(docs, op)
    v, h = docs["licoes"]["verificadas"], docs["licoes"]["hipoteses"]
    while len(v) + len(h) > MAX_LICOES:
        logger.warning("[memoria] limite de lições atingido; sai a mais antiga: %s", (h or v).pop(0))
    for doc, chave, limite in (("preferencias", "preferencias", MAX_PREFERENCIAS),
                               ("convencoes", "convencoes", MAX_CONVENCOES), ("perfil", "outros", MAX_OUTROS)):
        itens = docs[doc][chave]
        for saiu in itens[:-limite]:
            logger.warning("[memoria] limite de %s atingido; sai o mais antigo: %s", chave, saiu)
        docs[doc][chave] = itens[-limite:]
    return docs["licoes"], docs["preferencias"], docs["perfil"], docs["convencoes"]


# ---------------------------------------------------------------------------
# Fichas de análise e resumo da abordagem
# ---------------------------------------------------------------------------
_DATAS = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\bQ\d\s*,?\s*(?:FY)?\s*\d{4}\b|\bFY\s?\d{2,4}\b", re.IGNORECASE)
_VALOR = re.compile(r"\d+[.,]\d+|\d\s*%|[$€£¥]\s*\d|R\$\s*\d")


def _parece_valor(texto: str) -> bool:
    """True se o texto traz cara de resultado numérico (decimais, percentuais, moeda). Datas e
    períodos fiscais (2026-09-01, Q2,2026, FY2026) são tirados antes. É uma rede de segurança:
    a estratégia descreve a abordagem, e os números mudam."""
    return bool(_VALOR.search(_DATAS.sub(" ", texto)))


# Uma consulta de dados que DEU CERTO no trace do analista: o resultado de uma tool de dados
# (executar_sql, executar_graphql, executar_graphql_agregado, analisar_dataframe) que não começa
# com erro. executar_graphql_schema lê o schema, não dados, e não conta.
_CONSULTA_OK = re.compile(
    r"\[resultado de (?:executar_(?!graphql_schema\b)\w+|analisar_dataframe)\] (?!\s*(?:erro|error|traceback))",
    re.IGNORECASE,
)


def _tem_analise(msgs: list[dict]) -> bool:
    """True se, nas mensagens ainda não processadas, o analista fez ao menos uma consulta de
    dados que deu certo. Se todas falharam (API fora do ar, não configurada, timeout), não houve
    análise para lembrar, e uma falha de infraestrutura não é abordagem."""
    return any(
        m["role"] == "assistant" and not m["content"].startswith("[CONTEXTO") and _CONSULTA_OK.search(m["content"])
        for m in msgs
    )


def validar_fichas(propostas: list[FichaAnalise], msgs: list[dict], existentes: set[str],
                   tem_usuario: bool) -> list[dict]:
    """O que o código aceita das fichas propostas: exige usuário; ficha NOVA só quando há
    consulta de dados nas mensagens (atualizar uma desta sessão sempre pode); título sem cara
    de resultado numérico (senão a ficha cai) e campos com números esvaziados; id que não é
    desta sessão vira ficha nova; no máximo MAX_FICHAS_POR_VEZ."""
    if not tem_usuario or not propostas:
        return []
    analise = _tem_analise(msgs)
    aceitas = []
    for f in propostas[:MAX_FICHAS_POR_VEZ]:
        campos = {c: (getattr(f, c) or "").strip() for c in ("ficha_id", "titulo", "intencao", "funcionou", "falhou")}
        campos["avaliacao"] = f.avaliacao
        if campos["ficha_id"] not in existentes:
            campos["ficha_id"] = ""
        if not campos["titulo"] or _parece_valor(campos["titulo"]):
            logger.warning("[memoria] ficha descartada (sem título ou título com números): %s", campos["titulo"])
            continue
        if not campos["ficha_id"] and not analise:
            logger.info("[memoria] ficha nova descartada: não houve consulta de dados na janela")
            continue
        for c in ("intencao", "funcionou", "falhou"):
            if campos[c] and _parece_valor(campos[c]):
                logger.warning("[memoria] campo %s da ficha esvaziado (trazia números): %s", c, campos[c])
                campos[c] = ""
        aceitas.append(campos)
    return aceitas


_ERRO = re.compile(r"^\s*(erro|error|traceback)\b", re.IGNORECASE)


def renderizar_abordagem(messages: list[BaseMessage]) -> str:
    """Resumo da ABORDAGEM do analista: o que ele chamou (consultas, scripts) e se deu certo.
    Os valores devolvidos ficam de fora; só as mensagens de erro entram (cortadas)."""
    linhas = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in getattr(m, "tool_calls", None) or []:
                args = json.dumps(tc.get("args", {}), ensure_ascii=False)
                linhas.append(f"- {tc.get('name')}: {_cortar(args, _MAX_ARGS_ABORDAGEM)}")
        elif isinstance(m, ToolMessage):
            conteudo = m.content if isinstance(m.content, str) else str(m.content)
            linhas.append(f"  -> ERRO: {_cortar(conteudo, 160)}" if _ERRO.match(conteudo) else "  -> ok")
    return "\n".join(linhas)


_abordagens: dict[str, list[str]] = defaultdict(list)  # session_id -> abordagens do turno em curso


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
_seq_notas = itertools.count(1)                      # identifica cada clique em 👍/👎


def _baixar_notas(sessao: str, notas: dict[int, int]) -> None:
    """Depois de uma extração concluída, as avaliações que ela levou deixam de estar pendentes
    (`notas`: posição absoluta do turno -> clique). Um clique feito enquanto ela rodava tem outro
    número e continua pendente. Chamada com `_lock` preso."""
    lista, base = _turnos.get(sessao) or [], _base.get(sessao, 0)
    for n_abs, clique in notas.items():
        i = n_abs - base
        if 0 <= i < len(lista) and lista[i].get("nota_pendente") == clique:
            del lista[i]["nota_pendente"]


def _cortar(txt: str, n: int) -> str:
    txt = str(txt)
    return txt if len(txt) <= n else txt[:n] + "…"


def _resumir_resposta(txt: str) -> str:
    """Encurta a resposta final para o extrator, mantendo o começo e o FIM. O fim é onde ficam
    os blocos de parâmetros e de dados da análise (qualquer que seja o idioma do título), que é
    o que a correção do usuário costuma contestar."""
    txt = str(txt)
    if len(txt) <= _RESPOSTA_CABECA + _RESPOSTA_CAUDA:
        return txt
    return txt[:_RESPOSTA_CABECA] + "\n[...]\n" + txt[-_RESPOSTA_CAUDA:]


def _texto_conversa(msgs: list[dict]) -> str:
    return "\n\n".join(
        ("USUÁRIO: " if m["role"] == "user" else "AGENTE: ") + m["content"] for m in msgs
    )


def _conteudo_para_o_llm(
    msgs: list[dict], lic: dict, pref: dict, perfil: dict, conv: dict, fichas_sessao: list[dict],
    tem_usuario: bool,
) -> str:
    atuais = {"licoes": lic.get("verificadas") or []}
    if tem_usuario:
        atuais["convencoes_pessoais"] = conv.get("convencoes") or []
        atuais["preferencias"] = pref.get("preferencias") or []
        atuais["fichas_desta_sessao"] = [
            {c: f.get(c) or "" for c in ("id", "titulo", "intencao", "funcionou", "falhou", "avaliacao")}
            for f in fichas_sessao
        ]
        atuais["perfil"] = {
            **{c: perfil.get(c) for c in PerfilUsuario.model_fields if c != "outros"},
            "outros": perfil.get("outros") or [],
        }
    aviso = "" if tem_usuario else (
        "\nNão há usuário identificado: use SÓ os destinos de lição; não registre convenções, "
        "preferências, perfil nem fichas.\n"
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
            self._extrair(payload["messages"], meta.get("user_id"), meta["dominio"], meta.get("sessao"))
        except Exception as e:
            # o ponteiro não avança: esses turnos voltam na próxima extração da sessão
            logger.warning("[memoria] falha na extração (sessão %s): %r", meta.get("sessao"), e)
            return []
        with _lock:
            _extraidos[meta.get("sessao")] = max(_extraidos[meta.get("sessao")], meta.get("n_abs", 0))
            _baixar_notas(meta.get("sessao"), meta.get("notas") or {})
        logger.info("[memoria] extração concluída (sessão %s)", meta.get("sessao"))
        return []

    def _extrair(self, msgs: list[dict], user_id: str | None, dominio: str, sessao: str | None = None) -> None:
        ns_lic = (_NS_LICOES, _seg(dominio))
        ns_pref = ("users", _seg(user_id), _seg(dominio), "preferences") if user_id else None
        ns_perf = ("users", _seg(user_id), _seg(dominio), "profile") if user_id else None
        ns_conv = ("users", _seg(user_id), _seg(dominio), "conventions") if user_id else None
        _, lic = _ler_doc(ns_lic, "LicoesDoBanco")
        _, pref = _ler_doc(ns_pref, "PreferenciasUsuario") if user_id else (None, {})
        _, perfil = _ler_doc(ns_perf, "PerfilUsuario") if user_id else (None, {})
        _, conv = _ler_doc(ns_conv, "ConvencoesDoUsuario") if user_id else (None, {})
        com_fichas = bool(user_id and sessao)
        existentes = {f["id"]: f for f in _fichas.da_sessao(sessao, dominio)} if com_fichas else {}

        conteudo = _conteudo_para_o_llm(msgs, lic, pref, perfil, conv, list(existentes.values()), bool(user_id))
        if os.getenv("MEMORIA_DEBUG", "0").strip() == "1":
            logger.info("[memoria] payload enviado ao extrator:\n%s", conteudo)
        m = self._llm.invoke([SystemMessage(content=INSTRUCOES), HumanMessage(content=conteudo)])
        if not isinstance(m, MudancasMemoria):
            raise ValueError(f"o modelo não devolveu as mudanças estruturadas: {_cortar(repr(m), 200)}")
        logger.info("[memoria] extrator: %s", _cortar(m.raciocinio, 600))

        lic_novo, pref_novo, perfil_novo, conv_novo = aplicar_operacoes(
            m.operacoes, lic, pref, perfil, conv, bool(user_id)
        )
        for campos in validar_fichas(m.fichas, msgs, set(existentes), com_fichas):
            _fichas.salvar(campos, sessao, user_id, dominio, existentes)
        _gravar(ns_lic, "LicoesDoBanco", _normalizar_licoes(lic), lic_novo)
        if user_id:
            _gravar(ns_pref, "PreferenciasUsuario", _normalizar_prefs(pref), pref_novo)
            _gravar(ns_perf, "PerfilUsuario", _normalizar_perfil(perfil), perfil_novo)
            _gravar(ns_conv, "ConvencoesDoUsuario", _normalizar_convencoes(conv), conv_novo)


def _normalizar_licoes(doc: dict) -> dict:
    return {"verificadas": _itens(doc.get("verificadas")), "hipoteses": _itens(doc.get("hipoteses"))}


def _normalizar_prefs(doc: dict) -> dict:
    return {"preferencias": _itens(doc.get("preferencias"))}


def _normalizar_convencoes(doc: dict) -> dict:
    return {"convencoes": _itens(doc.get("convencoes"))}


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
        abordagem = renderizar_abordagem(messages)
        with _lock:
            if trace:
                _traces[session_id].append(trace)
            if abordagem:
                _abordagens[session_id].append(abordagem)
    except Exception:
        logger.warning("[memoria] falha ao registrar trace", exc_info=True)


def descartar_turno(session_id: str) -> None:
    """Turno cancelado ou com erro: descarta o trace acumulado."""
    with _lock:
        _traces.pop(session_id, None)
        _abordagens.pop(session_id, None)


def _mensagens(turnos: list[dict], n_contexto: int) -> list[dict]:
    msgs = []
    for i, t in enumerate(turnos):
        marca = "[CONTEXTO, já processado] " if i < n_contexto else ""
        msgs.append({"role": "user", "content": marca + t["pergunta"]})
        partes = [t["trace"]] if t["trace"] else []
        partes.append(t["resposta"])
        if t.get("nota"):
            partes.append(marca_da_nota(t["nota"]))
        msgs.append({"role": "assistant", "content": marca + "\n".join(partes)})
    return msgs


# Palavras com que o usuário declara uma regra permanente (pt, en, zh, os idiomas do app).
_DECLARA_REGRA = re.compile(
    r"\b(sempre|nunca|jamais|o correto é|a regra é|always|never|the correct)\b|总是|永远|一直|从不|务必",
    re.IGNORECASE,
)


def _enviar(sessao: str, user_id: str, dominio: str, turnos: list[dict], base: int) -> None:
    """Agenda a extração da sessão. Uma resposta avaliada com 👍 ou 👎 cuja avaliação ainda não
    chegou a uma extração concluída (`nota_pendente`) entra na janela como nova, com os dois
    turnos anteriores como contexto, mesmo que ela já tivesse sido extraída. Vale também quando
    quem agenda é o turno seguinte: o agendamento substitui o do clique (mesmo thread_id), e o
    sinal não pode ficar de fora nem virar contexto."""
    avaliados = [i for i, t in enumerate(turnos) if t.get("nota_pendente")]
    avaliado = avaliados[0] if avaliados else None
    ja = _extraidos[sessao]
    # um turno de contexto antes dos pendentes: a contestação chega junto com a análise
    # contestada, mesmo que ela já tenha sido extraída antes.
    inicio = max(0, ja - base - 1)
    if avaliado is not None:
        inicio = max(0, min(inicio, avaliado - 2))
    janela = turnos[inicio:]
    n_contexto = (ja - base) - inicio if ja > base else 0
    if avaliado is not None:  # a resposta avaliada e o que veio depois contam como novos
        n_contexto = min(n_contexto, avaliado - inicio)
    if not janela or (avaliado is None and ja >= base + len(turnos)):
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
    # A espera (debounce) deixa a pergunta, a contestação e a refeitura chegarem juntas ao
    # extrator, mas numa conversa sem pausas ela adiaria a extração para sempre e os turnos
    # sairiam da janela: com turnos demais pendentes, extrai já.
    pendentes = base + len(turnos) - ja
    espera = 0 if pendentes >= _teto_pendentes() else _delay()
    logger.info("[memoria] agendando extração (sessão %s, %d turnos na janela, %d pendentes, em %ds)",
                sessao, len(janela), pendentes, espera)
    fut = _executor.submit(
        {
            "messages": _mensagens(janela, n_contexto),
            "_meta": {"sessao": sessao, "n_abs": base + len(turnos), "user_id": user_id, "dominio": dominio,
                      "notas": {base + i: turnos[i]["nota_pendente"] for i in avaliados}},
        },
        config={"configurable": {}},
        after_seconds=espera,
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
            abordagem = "\n".join(_abordagens.pop(session_id, []))
            lista = _turnos[session_id]
            resumo = _resumir_resposta(resposta)
            lista.append({"pergunta": pergunta, "trace": trace, "resposta": resumo, "abordagem": abordagem})
            if len(lista) > _MAX_TURNOS_GUARDADOS:
                excesso = len(lista) - _MAX_TURNOS_GUARDADOS
                del lista[:excesso]
                _base[session_id] += excesso
            _enviar(session_id, user_id, dominio, list(lista), _base[session_id])
        if abordagem:  # guardado no banco: a avaliação do usuário o copia para o conjunto de dados
            try:
                guardar_abordagem(session_id, inicio_comparavel(resposta), abordagem)
            except Exception:
                logger.warning("[memoria] falha ao guardar a abordagem", exc_info=True)
    except Exception:
        logger.warning("[memoria] falha ao agendar a extração", exc_info=True)


def avaliar_resposta(session_id: str, user_id: str, dominio: str, inicio_resposta: str, nota: str) -> bool:
    """O usuário clicou em 👍 (`up`) ou 👎 (`down`) numa resposta. É um sinal de reforço para o
    extrator único: o turno é marcado e a extração da sessão é reagendada com ele na janela (e os
    dois anteriores como contexto), para o extrator decidir, olhando a conversa toda, o que há a
    reforçar. A marca fica pendente até uma extração com ela terminar: se o usuário seguir
    conversando antes da espera, o agendamento do turno seguinte substitui este (mesmo
    thread_id) e leva o turno avaliado como novo, já com as mensagens que vêm depois. Devolve
    True se achou o turno em memória; senão (muito antigo, ou depois de um restart) a avaliação
    só fica no banco."""
    if not ativa() or _store is None or _executor is None or not user_id or nota not in ("up", "down"):
        return False
    inicio = (inicio_resposta or "").strip()
    if not inicio:
        return False
    try:
        with _lock:
            lista = _turnos.get(session_id) or []
            for pos in range(len(lista) - 1, -1, -1):
                if inicio_comparavel(lista[pos]["resposta"]).startswith(inicio):
                    lista[pos]["nota"] = nota
                    lista[pos]["nota_pendente"] = next(_seq_notas)
                    _enviar(session_id, user_id, dominio, list(lista), _base[session_id])
                    return True
    except Exception:
        logger.warning("[memoria] falha ao registrar a avaliação", exc_info=True)
    return False


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
