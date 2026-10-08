"""
Testes da memória do agente (sem rede e sem LLM).

Rodar:  .venv/Scripts/python -m unittest discover -s tests -v

Cobrem: as versões validadas das bibliotecas, o schema da chamada estruturada do extrator
(que precisa ser aceito pelo Vertex), a mesclagem das mudanças nos documentos e a leitura
dos blocos injetados nos prompts. Se um upgrade quebrar algo, repita também uma conversa
real com o Vertex (ver "Limites conhecidos" em ARQUITETURA.md).
"""
import base64
import unittest
from importlib.metadata import version

from google.auth.credentials import AnonymousCredentials
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent_multi
import feedback
import memoria
from memoria import MAX_CONVENCOES, MAX_LICOES, MudancasMemoria, Operacao, aplicar_operacoes

# Versões em que a memória foi validada com o Vertex de verdade.
VERSOES_VALIDADAS = {
    "langchain-google-vertexai": "2.1.2",
    "langmem": "0.0.30",
}


def _modelo(cls):
    return cls(model_name="gemini-3.8-flash", project="p", location="us-central1",
               credentials=AnonymousCredentials())


class VersoesValidadas(unittest.TestCase):
    def test_versoes_conhecidas(self):
        for pacote, esperada in VERSOES_VALIDADAS.items():
            with self.subTest(pacote=pacote):
                self.assertEqual(
                    version(pacote), esperada,
                    f"{pacote} mudou: revalide a chamada estruturada do extrator e o bypass de "
                    "thought_signature (testes + conversa real) e atualize VERSOES_VALIDADAS.",
                )


class SchemaDoExtrator(unittest.TestCase):
    def test_vertex_aceita_o_schema_da_saida_estruturada(self):
        """A chamada estruturada do extrator monta o request sem erro de conversão do schema."""
        modelo = _modelo(agent_multi._chat_vertex_with_thinking_level()).bind_tools([MudancasMemoria])
        modelo.bound._prepare_request_gemini([HumanMessage("x")], **modelo.kwargs)


class ThoughtSignature(unittest.TestCase):
    def _assinaturas(self, messages):
        modelo = _modelo(agent_multi._chat_vertex_with_thinking_level())
        request = modelo._prepare_request_gemini(messages)
        return [p.thought_signature for c in request.contents for p in c.parts if "function_call" in p]

    def _par(self, additional_kwargs=None):
        return [
            HumanMessage("oi"),
            AIMessage("", tool_calls=[{"name": "f", "args": {"a": 1}, "id": "1"}],
                      additional_kwargs=additional_kwargs or {}),
            ToolMessage("ok", tool_call_id="1", name="f"),
        ]

    def test_chamada_sem_assinatura_recebe_o_bypass(self):
        self.assertEqual(self._assinaturas(self._par()), [b"skip_thought_signature_validator"])

    def test_assinatura_real_e_preservada(self):
        real = base64.b64encode(b"assinatura-real").decode()
        sigs = {"__gemini_function_call_thought_signatures__": {"1": real}}
        self.assertEqual(self._assinaturas(self._par(sigs)), [b"assinatura-real"])


def _op(destino, acao, antigo="", novo=""):
    return Operacao(destino=destino, acao=acao, antigo=antigo, novo=novo)


def _aplica4(ops, lic=None, pref=None, perfil=None, conv=None, tem_usuario=True):
    """Devolve (lições, preferências, perfil, convenções)."""
    return aplicar_operacoes(ops, lic or {}, pref or {}, perfil or {}, conv or {}, tem_usuario)


def _aplica(ops, lic=None, pref=None, perfil=None, tem_usuario=True):
    return _aplica4(ops, lic, pref, perfil, None, tem_usuario)[:3]


class OperacoesDeLicoes(unittest.TestCase):
    def test_adiciona_verificada(self):
        lic, _, _ = _aplica([_op("licao_verificada", "adicionar", novo="Usar USD")])
        self.assertEqual(lic, {"verificadas": ["Usar USD"], "hipoteses": []})

    def test_nao_duplica_frase_igual(self):
        doc = {"verificadas": ["Usar a coluna dt_producao para data"]}
        lic, _, _ = _aplica([_op("licao_verificada", "adicionar", novo="usar a coluna dt_producao para data.")], doc)
        self.assertEqual(lic["verificadas"], doc["verificadas"])

    def test_nao_confunde_regras_parecidas_mas_diferentes(self):
        doc = {"verificadas": ["Na tabela producao, a coluna de data é dt_producao"]}
        lic, _, _ = _aplica([_op("licao_verificada", "adicionar",
                                 novo="Na tabela paradas, a coluna de data é dt_parada")], doc)
        self.assertEqual(len(lic["verificadas"]), 2)

    def test_substituir_troca_no_mesmo_lugar(self):
        doc = {"verificadas": ["Regra A", "Usar apenas USD nos custos", "Regra C"]}
        lic, _, _ = _aplica([_op("licao_verificada", "substituir", "Usar apenas USD nos custos",
                                 "Custos podem ser apresentados em USD ou BRL")], doc)
        self.assertEqual(lic["verificadas"], ["Regra A", "Custos podem ser apresentados em USD ou BRL", "Regra C"])

    def test_substituir_aceita_citacao_com_pequena_diferenca(self):
        doc = {"verificadas": ["Usar apenas USD nos custos, nunca somando moedas locais."]}
        lic, _, _ = _aplica([_op("licao_verificada", "substituir",
                                 "usar apenas USD nos custos, nunca somando moedas locais",
                                 "Custos em USD ou BRL")], doc)
        self.assertEqual(lic["verificadas"], ["Custos em USD ou BRL"])

    def test_substituir_item_inexistente_vira_adicao(self):
        doc = {"verificadas": ["Usar USD"]}
        lic, _, _ = _aplica([_op("licao_verificada", "substituir", "algo que nunca existiu", "Regra nova")], doc)
        self.assertEqual(lic["verificadas"], ["Usar USD", "Regra nova"])

    def test_remover_item_inexistente_nao_apaga_nada(self):
        doc = {"verificadas": ["Usar USD"], "hipoteses": ["Incluir a data no join"]}
        lic, _, _ = _aplica([_op("licao_verificada", "remover", "algo que nunca existiu")], doc)
        self.assertEqual(lic, doc)

    def test_citacao_ambigua_nao_altera_nada(self):
        doc = {"verificadas": ["Na tabela producao a coluna de data é dt_prod_a",
                               "Na tabela producao a coluna de data é dt_prod_b"]}
        lic, _, _ = _aplica([_op("licao_verificada", "remover", "Na tabela producao a coluna de data é dt_prod")], doc)
        self.assertEqual(lic["verificadas"], doc["verificadas"])

    def test_casamento_exato_vence_o_aproximado(self):
        doc = {"verificadas": ["Na tabela producao a coluna de data é dt_prod_a",
                               "Na tabela producao a coluna de data é dt_prod_b"]}
        lic, _, _ = _aplica([_op("licao_verificada", "remover", "Na tabela producao a coluna de data é dt_prod_b")], doc)
        self.assertEqual(lic["verificadas"], ["Na tabela producao a coluna de data é dt_prod_a"])

    def test_promover_hipotese_e_remover_mais_adicionar(self):
        doc = {"verificadas": [], "hipoteses": ["Incluir a data no join"]}
        lic, _, _ = _aplica([_op("licao_hipotese", "remover", "Incluir a data no join"),
                             _op("licao_verificada", "adicionar", novo="Incluir a data no join")], doc)
        self.assertEqual(lic, {"verificadas": ["Incluir a data no join"], "hipoteses": []})

    def test_verificada_nova_substitui_a_hipotese_igual(self):
        doc = {"verificadas": [], "hipoteses": ["Incluir a data no join"]}
        lic, _, _ = _aplica([_op("licao_verificada", "adicionar", novo="Incluir a data no join")], doc)
        self.assertEqual(lic, {"verificadas": ["Incluir a data no join"], "hipoteses": []})

    def test_hipotese_igual_a_verificada_e_ignorada(self):
        doc = {"verificadas": ["Usar USD"], "hipoteses": []}
        lic, _, _ = _aplica([_op("licao_hipotese", "adicionar", novo="Usar USD")], doc)
        self.assertEqual(lic, doc)

    def test_limite_descarta_hipoteses_primeiro(self):
        verificadas = [(f"regra verificada numero {i} " + "x" * i * 3).strip() for i in range(MAX_LICOES)]
        lic, _, _ = _aplica([_op("licao_hipotese", "adicionar", novo="uma hipotese a mais, bem diferente")],
                            {"verificadas": verificadas})
        self.assertEqual(len(lic["verificadas"]) + len(lic["hipoteses"]), MAX_LICOES)
        self.assertEqual(lic["verificadas"], verificadas)
        self.assertEqual(lic["hipoteses"], [])


class OperacoesDePreferenciasEPerfil(unittest.TestCase):
    def test_trocar_preferencia_por_assunto(self):
        doc = {"preferencias": ["Gráficos na cor laranja", "Respostas curtas"]}
        _, pref, _ = _aplica([_op("preferencia", "substituir", "Gráficos na cor laranja",
                                  "Gráficos na cor rosa")], pref=doc)
        self.assertEqual(pref["preferencias"], ["Gráficos na cor rosa", "Respostas curtas"])

    def test_perfil_define_corrige_e_apaga_campos(self):
        doc = {"nome": "Marico", "cargo": "Cientista de dados", "outros": ["Apresenta ao diretor"]}
        _, _, perfil = _aplica([_op("perfil_nome", "substituir", novo="Marcio"),
                                _op("perfil_cargo", "remover")], perfil=doc)
        self.assertEqual(perfil["nome"], "Marcio")
        self.assertIsNone(perfil["cargo"])
        self.assertEqual(perfil["outros"], ["Apresenta ao diretor"])

    def test_perfil_outros_sem_duplicar(self):
        doc = {"outros": ["Apresenta ao diretor toda segunda"]}
        _, _, perfil = _aplica([_op("perfil_outros", "adicionar", novo="Apresenta ao diretor toda segunda.")],
                               perfil=doc)
        self.assertEqual(perfil["outros"], doc["outros"])

    def test_sem_usuario_so_as_licoes_mudam(self):
        lic, pref, perfil = _aplica([_op("licao_verificada", "adicionar", novo="Usar USD"),
                                     _op("preferencia", "adicionar", novo="Gráficos azuis"),
                                     _op("perfil_nome", "adicionar", novo="Ana")], tem_usuario=False)
        self.assertEqual(lic["verificadas"], ["Usar USD"])
        self.assertEqual(pref["preferencias"], [])
        self.assertIsNone(perfil["nome"])


class ConvencoesPessoais(unittest.TestCase):
    def test_adiciona_e_substitui(self):
        _, _, _, conv = _aplica4([_op("convencao_pessoal", "adicionar", novo="Mostrar os custos em USD")])
        self.assertEqual(conv["convencoes"], ["Mostrar os custos em USD"])
        _, _, _, conv = _aplica4([_op("convencao_pessoal", "substituir", "Mostrar os custos em USD",
                                      "Mostrar os custos em USD ou BRL")], conv=conv)
        self.assertEqual(conv["convencoes"], ["Mostrar os custos em USD ou BRL"])

    def test_nao_vaza_para_as_licoes_compartilhadas(self):
        lic, _, _, conv = _aplica4([_op("convencao_pessoal", "adicionar", novo="Considerar só os sites BPL e MAN")])
        self.assertEqual(lic, {"verificadas": [], "hipoteses": []})
        self.assertEqual(conv["convencoes"], ["Considerar só os sites BPL e MAN"])

    def test_duas_coisas_na_mesma_frase_vao_cada_uma_ao_seu_destino(self):
        lic, _, _, conv = _aplica4([_op("convencao_pessoal", "adicionar", novo="Mostrar os custos em USD"),
                                    _op("licao_verificada", "adicionar", novo="Nunca somar valores de moedas diferentes")])
        self.assertEqual(conv["convencoes"], ["Mostrar os custos em USD"])
        self.assertEqual(lic["verificadas"], ["Nunca somar valores de moedas diferentes"])

    def test_mesma_frase_em_licao_e_convencao_fica_so_na_convencao(self):
        frase = "Considerar por padrão só os sites BPL e MAN"
        lic, _, _, conv = _aplica4([_op("licao_verificada", "adicionar", novo=frase),
                                    _op("convencao_pessoal", "adicionar", novo=frase + ".")])
        self.assertEqual(lic["verificadas"], [])
        self.assertEqual(len(conv["convencoes"]), 1)

    def test_sem_usuario_nao_grava_convencao(self):
        _, _, _, conv = _aplica4([_op("convencao_pessoal", "adicionar", novo="Mostrar em USD")], tem_usuario=False)
        self.assertEqual(conv["convencoes"], [])

    def test_limite_descarta_as_mais_antigas(self):
        antigas = [f"convencao numero {i} " + "y" * i * 2 for i in range(MAX_CONVENCOES)]
        _, _, _, conv = _aplica4([_op("convencao_pessoal", "adicionar", novo="uma convencao nova e bem diferente")],
                                 conv={"convencoes": [a.strip() for a in antigas]})
        self.assertEqual(len(conv["convencoes"]), MAX_CONVENCOES)
        self.assertEqual(conv["convencoes"][-1], "uma convencao nova e bem diferente")


class ResumoDaResposta(unittest.TestCase):
    def test_resposta_curta_nao_muda(self):
        self.assertEqual(memoria._resumir_resposta("curta"), "curta")

    def test_resposta_longa_mantem_o_comeco_e_o_fim(self):
        corpo = "achado " * 600
        bloco = "**Parâmetros da análise:**\n- Período: 2026-09-01 a 2026-09-30\n- Tabela: fact_hourly_rate"
        resumo = memoria._resumir_resposta("INICIO " + corpo + "\n" + bloco)
        self.assertTrue(resumo.startswith("INICIO"))
        self.assertTrue(resumo.endswith(bloco))
        self.assertIn("[...]", resumo)
        self.assertLess(len(resumo), 2400)


class AvaliacaoDeResposta(unittest.TestCase):
    """Módulo feedback.py: identificar a resposta avaliada pelo usuário."""

    def _mensagens(self):
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        return [
            HumanMessage("Qual o custo por site?"),
            AIMessage("", tool_calls=[{"name": "consultar_analista", "args": {}, "id": "1"}]),
            ToolMessage("ok", tool_call_id="1", name="consultar_analista"),
            AIMessage("Custo por site: BPL 10, MAN 20.\n[chart:3f8a1b]\n**Parâmetros da análise:** período X"),
            HumanMessage("Está errado, use só aprovados."),
            AIMessage("", tool_calls=[{"name": "consultar_analista", "args": {}, "id": "2"}]),
            ToolMessage("ok", tool_call_id="2", name="consultar_analista"),
            AIMessage("Refeito só com aprovados: BPL 8, MAN 15.\n**Parâmetros da análise:** status aprovado"),
            HumanMessage("Que horas são?"),
            AIMessage("São 10h."),
        ]

    def test_montar_turnos_marca_respostas_de_dados(self):
        turnos = feedback.montar_turnos(self._mensagens())
        self.assertEqual([t["analista"] for t in turnos], [True, True, False])
        self.assertEqual(turnos[0]["usuario"], "Qual o custo por site?")
        self.assertTrue(turnos[1]["resposta"].startswith("Refeito só com aprovados"))
        self.assertEqual(turnos[2]["resposta"], "São 10h.")

    def test_inicio_comparavel_ignora_artefatos_e_espacos(self):
        no_checkpoint = "Custo por site: BPL 10.\n[chart:3f8a1b]\nfim"
        no_front = "Custo por site:   BPL 10.\n\n![gráfico](/chart/3f8a1b)\nfim"
        self.assertEqual(feedback.inicio_comparavel(no_checkpoint), feedback.inicio_comparavel(no_front))

    def test_localizar_resposta_pelo_inicio_normalizado(self):
        turnos = feedback.montar_turnos(self._mensagens())
        inicio_do_front = feedback.inicio_comparavel("Custo por site: BPL 10, MAN 20.\n![gráfico](/chart/3f8a1b)\nxx")
        self.assertEqual(feedback.localizar_resposta(turnos, inicio_do_front[:30]), 0)
        self.assertEqual(feedback.localizar_resposta(turnos, "Refeito só com aprovados"), 1)
        self.assertIsNone(feedback.localizar_resposta(turnos, "texto que nunca existiu"))
        self.assertIsNone(feedback.localizar_resposta(turnos, ""))


class AvaliacaoNaMemoria(unittest.TestCase):
    """memoria.avaliar_resposta: 👍 e 👎 são sinais; a extração é reagendada com o turno avaliado."""

    class _ExecutorFalso:
        def __init__(self):
            self.envios = []

        def submit(self, payload, config=None, after_seconds=0, thread_id=None):
            from concurrent.futures import Future

            self.envios.append({"payload": payload, "thread_id": thread_id})
            return Future()

    def setUp(self):
        self._store, self._executor = memoria._store, memoria._executor
        memoria._store = object()
        memoria._executor = self._ExecutorFalso()
        self.sessao = "321_teste_avaliacao"
        memoria._turnos[self.sessao] = [
            {"pergunta": "Qual o custo por site?", "trace": "", "resposta": "Custo por site: BPL 10, MAN 20."},
            {"pergunta": "Esse total está estranho, refaz.", "trace": "", "resposta": "Refeito só com aprovados: BPL 8."},
            {"pergunta": "Que horas são?", "trace": "", "resposta": "São 10h."},
        ]
        memoria._base[self.sessao] = 0
        memoria._extraidos[self.sessao] = 0

    def tearDown(self):
        for d in (memoria._turnos, memoria._base, memoria._extraidos):
            d.pop(self.sessao, None)
        memoria._store, memoria._executor = self._store, self._executor

    def _avalia(self, inicio, nota, user="321"):
        return memoria.avaliar_resposta(self.sessao, user, "dom.md", inicio, nota)

    def test_a_marca_e_neutra_e_so_vai_no_turno_avaliado(self):
        self.assertTrue(self._avalia("Refeito só com aprovados", "up"))
        msgs = memoria._mensagens(memoria._turnos[self.sessao], 0)
        self.assertIn(feedback.MARCA_BOA, msgs[3]["content"])
        self.assertNotIn("AVALIAÇÃO", msgs[1]["content"] + msgs[5]["content"])
        self.assertNotIn("mensagem seguinte", feedback.MARCA_BOA + feedback.MARCA_RUIM)  # não presume nada

    def test_thumbs_down_usa_a_marca_de_ruim(self):
        self._avalia("Custo por site", "down")
        self.assertIn(feedback.MARCA_RUIM, memoria._mensagens(memoria._turnos[self.sessao], 0)[1]["content"])

    def test_turno_ainda_nao_extraido_reagenda_a_extracao_da_sessao_com_a_marca(self):
        self._avalia("Refeito só com aprovados", "up")
        envio = memoria._executor.envios[-1]
        self.assertEqual(envio["thread_id"], self.sessao)  # substitui a extração pendente da sessão
        conteudo = " ".join(m["content"] for m in envio["payload"]["messages"])
        self.assertIn(feedback.MARCA_BOA, conteudo)

    def test_turno_ja_extraido_volta_a_janela_com_dois_turnos_de_contexto(self):
        memoria._extraidos[self.sessao] = 3  # tudo já foi extraído
        self._avalia("Refeito só com aprovados", "up")  # o turno do meio
        msgs = memoria._executor.envios[-1]["payload"]["messages"]
        self.assertEqual(len(msgs), 6)                       # os três turnos
        self.assertTrue(msgs[0]["content"].startswith("[CONTEXTO"))  # o anterior é contexto
        self.assertFalse(msgs[2]["content"].startswith("[CONTEXTO"))  # o avaliado conta como novo
        self.assertFalse(msgs[4]["content"].startswith("[CONTEXTO"))  # e o que veio depois também
        self.assertIn(feedback.MARCA_BOA, msgs[3]["content"])

    def test_avaliacao_sem_nada_a_extrair_do_resto_nao_perde_o_sinal_do_ultimo_turno(self):
        memoria._extraidos[self.sessao] = 3
        self._avalia("São 10h", "down")
        msgs = memoria._executor.envios[-1]["payload"]["messages"]
        self.assertFalse(msgs[-1]["content"].startswith("[CONTEXTO"))
        self.assertIn(feedback.MARCA_RUIM, msgs[-1]["content"])

    def test_nao_agenda_quando_nao_ha_o_que_marcar(self):
        self.assertFalse(self._avalia("resposta que não está aqui", "down"))
        self.assertFalse(self._avalia("", "down"))
        self.assertFalse(self._avalia("Custo por site", "talvez"))
        self.assertFalse(memoria.avaliar_resposta("outra_sessao", "321", "dom.md", "Custo por site", "up"))
        memoria._store = None
        self.assertFalse(self._avalia("Custo por site", "up"))
        self.assertEqual(memoria._executor.envios, [])


class BancoDeAvaliacoes(unittest.TestCase):
    """db.py: o conjunto pergunta, resposta, nota e mensagem seguinte, num banco temporário."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        import db

        self.db = db
        self._antigo = db.DB_PATH
        self._dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._dir.name) / "teste.db"
        db.migrate_db()

    def tearDown(self):
        self.db.DB_PATH = self._antigo
        self._dir.cleanup()

    def _avalia(self, sessao="321_a", turn=1, rating="down", resposta="Custo: BPL 10."):
        return self.db.registrar_avaliacao(sessao, turn, "321", "dom.md", rating, "Qual o custo?", resposta, True)

    def _linha(self):
        with self.db.get_db() as conn:
            return dict(conn.execute("SELECT * FROM feedback_respostas").fetchone())

    def test_grava_o_conjunto_completo(self):
        self.assertIsNone(self._avalia(rating="up"))
        r = self._linha()
        self.assertEqual((r["pergunta"], r["resposta"], r["rating"], r["analista"], r["dominio"], r["user_id"]),
                         ("Qual o custo?", "Custo: BPL 10.", "up", 1, "dom.md", "321"))
        self.assertIsNone(r["mensagem_seguinte"])

    def test_trocar_a_nota_atualiza_a_mesma_linha_e_devolve_a_anterior(self):
        self._avalia(rating="up")
        self.assertEqual(self._avalia(rating="down"), "up")
        self.assertEqual(self.db.avaliacoes_da_sessao("321_a"), {1: "down"})
        with self.db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_respostas").fetchone()[0], 1)

    def test_guarda_literalmente_a_proxima_mensagem_depois_do_thumbs_down(self):
        """Pode ser uma contestação ou outra pergunta: o banco não presume que contesta."""
        self._avalia(rating="down")
        self.assertEqual(self.db.anexar_mensagem_seguinte("321_a", "Que horas são?"), 1)
        self.assertEqual(self.db.anexar_mensagem_seguinte("321_a", "O custo tem que ser só aprovado."), 0)
        r = self._linha()
        self.assertEqual(r["mensagem_seguinte"], "Que horas são?")
        self.assertTrue(r["mensagem_seguinte_em"])

    def test_thumbs_up_e_outras_sessoes_nao_recebem_a_mensagem(self):
        self._avalia(sessao="321_a", turn=0, rating="up")
        self._avalia(sessao="321_b", turn=0, rating="down")
        self.assertEqual(self.db.anexar_mensagem_seguinte("321_a", "uma pergunta nova"), 0)
        self.assertEqual(self.db.anexar_mensagem_seguinte("321_a", ""), 0)
        with self.db.get_db() as conn:
            msgs = {r["session_id"]: r["mensagem_seguinte"] for r in conn.execute("SELECT * FROM feedback_respostas")}
        self.assertEqual(msgs, {"321_a": None, "321_b": None})

    def test_trocar_para_up_mantem_a_mensagem_ja_guardada(self):
        self._avalia(rating="down")
        self.db.anexar_mensagem_seguinte("321_a", "faltou filtrar por aprovados")
        self._avalia(rating="up")
        r = self._linha()
        self.assertEqual((r["rating"], r["mensagem_seguinte"]), ("up", "faltou filtrar por aprovados"))

    def test_migracao_renomeia_a_coluna_antiga_sem_perder_dados(self):
        import sqlite3

        self._dir.cleanup()
        import tempfile
        from pathlib import Path

        self._dir = tempfile.TemporaryDirectory()
        self.db.DB_PATH = Path(self._dir.name) / "legado.db"
        conn = sqlite3.connect(self.db.DB_PATH)
        conn.execute("CREATE TABLE feedback_respostas (session_id TEXT NOT NULL, turn INTEGER NOT NULL, user_id TEXT,"
                     " dominio TEXT, rating TEXT NOT NULL DEFAULT 'up', pergunta TEXT, resposta TEXT,"
                     " analista INTEGER NOT NULL DEFAULT 0, comentario TEXT, comentario_em TEXT,"
                     " created_at TEXT NOT NULL, updated_at TEXT, PRIMARY KEY (session_id, turn))")
        conn.execute("INSERT INTO feedback_respostas (session_id, turn, rating, comentario, created_at)"
                     " VALUES ('321_x', 0, 'down', 'texto antigo', '2026-01-01 00:00:00')")
        conn.commit()
        conn.close()
        self.db.migrate_db()
        with self.db.get_db() as c:
            colunas = [r["name"] for r in c.execute("PRAGMA table_info(feedback_respostas)")]
            linha = dict(c.execute("SELECT * FROM feedback_respostas").fetchone())
        self.assertIn("mensagem_seguinte", colunas)
        self.assertNotIn("comentario", colunas)
        self.assertEqual(linha["mensagem_seguinte"], "texto antigo")


class DuplicataEntreDestinos(unittest.TestCase):
    def test_mesma_frase_em_licao_e_preferencia_fica_so_na_licao(self):
        frase = "Apresentar os custos apenas em USD, nunca somando moedas locais"
        lic, pref, _ = _aplica([_op("preferencia", "adicionar", novo=frase + "."),
                                _op("licao_verificada", "adicionar", novo=frase)])
        self.assertEqual(lic["verificadas"], [frase])
        self.assertEqual(pref["preferencias"], [])

    def test_frases_diferentes_em_destinos_diferentes_ficam(self):
        lic, pref, _ = _aplica([_op("licao_verificada", "adicionar", novo="Custos apenas em USD"),
                                _op("preferencia", "adicionar", novo="Gráficos na cor rosa")])
        self.assertEqual(lic["verificadas"], ["Custos apenas em USD"])
        self.assertEqual(pref["preferencias"], ["Gráficos na cor rosa"])


class BlocosDeLeitura(unittest.TestCase):
    """Leitura de perfil e preferências num SqliteStore temporário, separada por domínio."""

    def setUp(self):
        import sqlite3

        from langgraph.store.sqlite import SqliteStore

        self._antigo = memoria._store
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        memoria._store = SqliteStore(conn)
        memoria._store.setup()

    def tearDown(self):
        memoria._store = self._antigo

    def _grava(self, ns, kind, content):
        memoria._store.put(ns, "default", {"kind": kind, "content": content})

    def test_perfil_so_no_dominio_e_usuario_certos(self):
        self._grava(("users", "321", "dom_a_md", "profile"), "PerfilUsuario",
                    {"nome": "Márcio", "cargo": None, "outros": ["Apresenta ao diretor"]})
        bloco = memoria.bloco_perfil("321", "dom_a.md")
        self.assertIn("Nome: Márcio", bloco)
        self.assertIn("- Apresenta ao diretor", bloco)
        self.assertNotIn("Cargo", bloco)
        self.assertEqual(memoria.bloco_perfil("321", "dom_b.md"), "")
        self.assertEqual(memoria.bloco_perfil("999", "dom_a.md"), "")
        self.assertEqual(memoria.bloco_perfil("", "dom_a.md"), "")

    def test_preferencias_so_no_dominio_certo(self):
        self._grava(("users", "321", "dom_a_md", "preferences"), "PreferenciasUsuario",
                    {"preferencias": ["Gráficos na cor rosa"]})
        self.assertIn("Gráficos na cor rosa", memoria.bloco_preferencias("321", "dom_a.md"))
        self.assertEqual(memoria.bloco_preferencias("321", "dom_b.md"), "")

    def test_convencoes_so_no_dominio_e_usuario_certos(self):
        self._grava(("users", "321", "dom_a_md", "conventions"), "ConvencoesDoUsuario",
                    {"convencoes": ["Considerar só os sites BPL e MAN"]})
        bloco = memoria.bloco_convencoes("321", "dom_a.md")
        self.assertIn("Considerar só os sites BPL e MAN", bloco)
        self.assertIn("vale o pedido", bloco)
        self.assertEqual(memoria.bloco_convencoes("321", "dom_b.md"), "")
        self.assertEqual(memoria.bloco_convencoes("999", "dom_a.md"), "")
        self.assertEqual(memoria.bloco_convencoes("", "dom_a.md"), "")

    def test_gravar_mantem_a_chave_e_so_grava_se_mudou(self):
        ns = ("db_lessons", "dom_a_md")
        antes = {"verificadas": ["Usar USD"], "hipoteses": []}
        self._grava(ns, "LicoesDoBanco", antes)
        self.assertFalse(memoria._gravar(ns, "LicoesDoBanco", antes, dict(antes)))
        depois = {"verificadas": ["Usar USD", "Outra regra"], "hipoteses": []}
        self.assertTrue(memoria._gravar(ns, "LicoesDoBanco", antes, depois))
        self.assertEqual(memoria.ler_licoes("dom_a.md")[1].regra, "Outra regra")
        self.assertEqual(len(memoria._store.search(ns)), 1)  # atualizou o mesmo documento


if __name__ == "__main__":
    unittest.main()
