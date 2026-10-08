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


def _est(destino, texto):
    return Operacao(destino=destino, acao="adicionar", novo=texto)


class EstrategiasReforcadas(unittest.TestCase):
    """Estratégias compartilhadas (boas e evitar): reforço, filtro de números, visibilidade."""

    def test_reforca_a_mesma_abordagem_e_conta_usuarios_distintos(self):
        t = "Para custo por site, agregar dl_cost_by_cm_usd por site filtrando status aprovado"
        doc = memoria.aplicar_estrategias({}, [_est("estrategia_boa", t)], "u1", hoje="2026-10-01")
        doc = memoria.aplicar_estrategias(doc, [_est("estrategia_boa", t.lower() + ".")], "u1", hoje="2026-10-02")
        self.assertEqual((doc["boas"][0]["n"], len(doc["boas"][0]["usuarios"])), (2, 1))
        doc = memoria.aplicar_estrategias(doc, [_est("estrategia_boa", t)], "u2", hoje="2026-10-03")
        self.assertEqual((len(doc["boas"]), doc["boas"][0]["n"], len(doc["boas"][0]["usuarios"])), (1, 3, 2))
        self.assertEqual(doc["boas"][0]["ultima"], "2026-10-03")

    def test_abordagem_diferente_vira_entrada_nova(self):
        doc = memoria.aplicar_estrategias({}, [_est("estrategia_boa", "Agregar o custo por site, filtrando aprovados")], "u1")
        doc = memoria.aplicar_estrategias(doc, [_est("estrategia_boa", "Calcular a taxa horária pela vigência mais recente")], "u1")
        self.assertEqual(len(doc["boas"]), 2)

    def test_descarta_estrategia_com_resultado_numerico_mas_aceita_datas(self):
        doc = memoria.aplicar_estrategias({}, [
            _est("estrategia_boa", "O site BPL concentrou 88,6% do custo"),
            _est("estrategia_boa", "O custo total foi de $798,76"),
            _est("estrategia_boa", "Para o último mês fechado (2026-09-01 a 2026-09-30, Q2,2026), agregar por site"),
        ], "u1")
        self.assertEqual([e["texto"] for e in doc["boas"]],
                         ["Para o último mês fechado (2026-09-01 a 2026-09-30, Q2,2026), agregar por site"])

    def test_limite_por_lista_descarta_as_de_avaliacao_mais_antiga(self):
        doc = {"boas": [{"texto": f"abordagem numero {i} " + "z" * i * 3, "n": 1, "usuarios": ["u"],
                         "ultima": f"2026-01-{i + 1:02d}"} for i in range(memoria.MAX_ESTRATEGIAS)]}
        novo = memoria.aplicar_estrategias(doc, [_est("estrategia_boa", "uma abordagem nova e bem diferente")], "u1",
                                           hoje="2026-10-01")
        self.assertEqual(len(novo["boas"]), memoria.MAX_ESTRATEGIAS)
        self.assertEqual(novo["boas"][-1]["texto"], "uma abordagem nova e bem diferente")

    def test_visibilidade_respeita_o_minimo_de_usuarios(self):
        from unittest import mock

        doc = {"boas": [{"texto": "Agregar por site", "n": 1, "usuarios": ["u1"], "ultima": "2026-10-01"}]}
        with mock.patch.dict("os.environ", {"MEMORIA_MIN_USUARIOS": "1"}):
            self.assertEqual(len(memoria.estrategias_visiveis(doc)[0]), 1)  # padrão: já na primeira avaliação
        with mock.patch.dict("os.environ", {"MEMORIA_MIN_USUARIOS": "2"}):
            self.assertEqual(memoria.estrategias_visiveis(doc)[0], [])
            doc["boas"][0]["usuarios"].append("u2")
            self.assertEqual(len(memoria.estrategias_visiveis(doc)[0]), 1)

    def test_abordagem_contestada_some_ate_um_lado_ganhar(self):
        def e(texto, n):
            return {"texto": texto, "n": n, "usuarios": [f"u{i}" for i in range(n)], "ultima": "2026-10-01"}

        igual = {"boas": [e("Somar dl_cost_by_cm_usd como custo total", 1)],
                 "evitar": [e("Somar dl_cost_by_cm_usd como custo total", 1)]}
        self.assertEqual(memoria.estrategias_visiveis(igual), ([], []))
        ganha_boa = {"boas": [e("Somar dl_cost_by_cm_usd como custo total", 2)],
                     "evitar": [e("Somar dl_cost_by_cm_usd como custo total", 1)]}
        boas, evitar = memoria.estrategias_visiveis(ganha_boa)
        self.assertEqual((len(boas), evitar), (1, []))

    def test_resumo_da_abordagem_leva_as_consultas_e_o_resultado_ok_ou_erro_sem_valores(self):
        from langchain_core.messages import AIMessage, ToolMessage

        msgs = [
            AIMessage("", tool_calls=[{"name": "executar_graphql_agregado", "args": {"table": "fact", "group_by": "site_id"}, "id": "1"}]),
            ToolMessage("Erro ao executar GraphQL: coluna inexistente", tool_call_id="1", name="executar_graphql_agregado"),
            AIMessage("", tool_calls=[{"name": "executar_graphql_agregado", "args": {"table": "fact", "group_by": "site"}, "id": "2"}]),
            ToolMessage('[{"groupKey": "BPL", "metricValue": 708.0}]', tool_call_id="2", name="executar_graphql_agregado"),
        ]
        resumo = memoria.renderizar_abordagem(msgs)
        self.assertIn("group_by", resumo)
        self.assertIn("ERRO: Erro ao executar GraphQL", resumo)
        self.assertNotIn("708", resumo)
        self.assertNotIn("BPL", resumo)


class BlocoDeEstrategias(unittest.TestCase):
    def setUp(self):
        import sqlite3

        from langgraph.store.sqlite import SqliteStore

        self._antigo = memoria._store
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        memoria._store = SqliteStore(conn)
        memoria._store.setup()

    def tearDown(self):
        memoria._store = self._antigo

    def test_bloco_mostra_a_confianca_e_so_o_dominio_certo(self):
        memoria._store.put(("strategies", "dom_a_md"), "default", {"kind": "Estrategias", "content": {
            "boas": [{"texto": "Agregar o custo por site, filtrando aprovados", "n": 3, "usuarios": ["a", "b"], "ultima": "2026-10-01"}],
            "evitar": [{"texto": "Somar custo unitário como total", "n": 1, "usuarios": ["a"], "ultima": "2026-10-01"}],
        }})
        bloco = memoria.bloco_estrategias("dom_a.md")
        self.assertIn("- Agregar o custo por site, filtrando aprovados (2 usuários)", bloco)
        self.assertIn("- Somar custo unitário como total (1 usuário)", bloco)
        self.assertIn("o dominio.md e as lições confirmadas valem mais", bloco)
        self.assertIn("nunca resultados", bloco)
        self.assertEqual(memoria.bloco_estrategias("dom_b.md"), "")


class EstrategiaExigeSinal(unittest.TestCase):
    """Estratégia só entra com um 👍/👎 na janela, e a lição vence a estratégia igual."""

    COM_SINAL = [{"role": "user", "content": "Qual o custo?"},
                 {"role": "assistant", "content": "Custo por site... " + feedback.MARCA_BOA}]
    SO_CONTEXTO = [{"role": "user", "content": "[CONTEXTO, já processado] Qual o custo?"},
                   {"role": "assistant", "content": "[CONTEXTO, já processado] Custo... " + feedback.MARCA_BOA}]
    SEM_SINAL = [{"role": "user", "content": "Qual o custo?"}, {"role": "assistant", "content": "Custo por site..."}]

    def test_sem_sinal_na_janela_a_estrategia_cai(self):
        ops = [_est("estrategia_boa", "Agregar por site")]
        self.assertEqual(memoria.validar_estrategias(ops, self.SEM_SINAL, True), [])
        self.assertEqual(memoria.validar_estrategias(ops, self.SO_CONTEXTO, True), [])  # sinal já processado

    def test_com_sinal_e_usuario_a_estrategia_passa(self):
        ops = [_est("estrategia_boa", "Agregar por site")]
        self.assertEqual(memoria.validar_estrategias(ops, self.COM_SINAL, True), ops)
        self.assertEqual(memoria.validar_estrategias(ops, self.COM_SINAL, False), [])  # sem usuário

    def test_so_a_acao_adicionar_e_no_maximo_tres(self):
        ops = [_est("estrategia_boa", f"abordagem {i}") for i in range(5)]
        ops.append(Operacao(destino="estrategia_evitar", acao="remover", antigo="x"))
        validas = memoria.validar_estrategias(ops, self.COM_SINAL, True)
        self.assertEqual(len(validas), 3)
        self.assertTrue(all(o.acao == "adicionar" for o in validas))

    def test_a_mesma_frase_em_licao_e_estrategia_fica_so_na_licao(self):
        frase = "Para custo, considerar apenas registros com status aprovado"
        ops = memoria._sem_duplicata_entre_destinos([
            _est("estrategia_evitar", frase + "."),
            Operacao(destino="licao_verificada", acao="adicionar", novo=frase),
        ])
        self.assertEqual([o.destino for o in ops], ["licao_verificada"])

    def test_estrategia_nao_chega_ao_aplicador_dos_outros_documentos(self):
        lic, pref, perfil, conv = aplicar_operacoes([_est("estrategia_boa", "Agregar por site")], {}, {}, {}, {}, True)
        self.assertEqual((lic["verificadas"], pref["preferencias"], conv["convencoes"]), ([], [], []))


class PromptDoExtratorUnico(unittest.TestCase):
    def test_regras_das_estrategias_estao_no_prompt(self):
        for trecho in ("NUNCA números", "ERRO NÃO É PEDIDO DE OUTRO RECORTE",
                       "Evitar <abordagem rejeitada>; em vez disso", "texto EXATO",
                       "SÓ existem quando há uma marca", "NÃO repita como estratégia"):
            with self.subTest(trecho=trecho):
                self.assertIn(trecho, memoria.INSTRUCOES)

    def test_destinos_de_estrategia_existem_no_schema_e_o_vertex_o_aceita(self):
        self.assertIn("estrategia_boa", str(Operacao.model_json_schema()))
        modelo = _modelo(agent_multi._chat_vertex_with_thinking_level()).bind_tools([MudancasMemoria])
        modelo.bound._prepare_request_gemini([HumanMessage("x")], **modelo.kwargs)


class AvaliacaoComoSinal(unittest.TestCase):
    """memoria.avaliar_resposta: 👍/👎 marcam o turno e reagendam a extração de sempre."""

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
        self.assertIn(feedback.MARCA_BOA, " ".join(m["content"] for m in envio["payload"]["messages"]))
        self.assertTrue(feedback.tem_sinal(envio["payload"]["messages"]))

    def test_turno_ja_extraido_volta_a_janela_com_dois_turnos_de_contexto(self):
        memoria._extraidos[self.sessao] = 3  # tudo já foi extraído
        self._avalia("Refeito só com aprovados", "up")  # o turno do meio
        msgs = memoria._executor.envios[-1]["payload"]["messages"]
        self.assertEqual(len(msgs), 6)
        self.assertTrue(msgs[0]["content"].startswith("[CONTEXTO"))      # o anterior é contexto
        self.assertFalse(msgs[2]["content"].startswith("[CONTEXTO"))     # o avaliado conta como novo
        self.assertFalse(msgs[4]["content"].startswith("[CONTEXTO"))     # e o que veio depois também
        self.assertTrue(feedback.tem_sinal(msgs))

    def test_avaliar_o_ultimo_turno_ja_extraido_nao_perde_o_sinal(self):
        memoria._extraidos[self.sessao] = 3
        self._avalia("São 10h", "down")
        msgs = memoria._executor.envios[-1]["payload"]["messages"]
        self.assertFalse(msgs[-1]["content"].startswith("[CONTEXTO"))
        self.assertIn(feedback.MARCA_RUIM, msgs[-1]["content"])

    def test_nao_agenda_quando_nao_ha_o_que_marcar(self):
        self.assertFalse(self._avalia("resposta que não está aqui", "down"))
        self.assertFalse(self._avalia("", "down"))
        self.assertFalse(self._avalia("Custo por site", "talvez"))
        self.assertFalse(self._avalia("Custo por site", "up", user=""))
        self.assertFalse(memoria.avaliar_resposta("outra_sessao", "321", "dom.md", "Custo por site", "up"))
        memoria._store = None
        self.assertFalse(self._avalia("Custo por site", "up"))
        self.assertEqual(memoria._executor.envios, [])

    def test_o_resumo_da_abordagem_vai_para_o_banco(self):
        from unittest import mock

        memoria._abordagens[self.sessao].append("- executar_graphql: {...}\n  -> ok")
        with mock.patch.object(memoria, "guardar_abordagem") as guardar:
            memoria.finalizar_turno(self.sessao, "321", "dom.md", "Outra pergunta", "Refeito só com aprovados.")
        chave, texto = guardar.call_args[0][1:]
        self.assertEqual(chave, feedback.inicio_comparavel("Refeito só com aprovados."))
        self.assertIn("executar_graphql", texto)


class SinalNaJanela(unittest.TestCase):
    def test_tem_sinal_ignora_o_que_e_contexto_ou_so_do_usuario(self):
        marca = feedback.MARCA_RUIM
        self.assertTrue(feedback.tem_sinal([{"role": "assistant", "content": "resposta " + marca}]))
        self.assertFalse(feedback.tem_sinal([{"role": "assistant", "content": "[CONTEXTO, já processado] r " + marca}]))
        self.assertFalse(feedback.tem_sinal([{"role": "user", "content": "digitei " + marca}]))
        self.assertFalse(feedback.tem_sinal([]))


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

    def test_resumo_da_abordagem_guarda_e_busca_por_sessao_e_chave(self):
        self.db.guardar_abordagem("321_a", "chave1", "- consulta X\n  -> ok")
        self.db.guardar_abordagem("321_a", "chave1", "- consulta Y\n  -> ok")  # troca, não duplica
        self.assertEqual(self.db.buscar_abordagem("321_a", "chave1"), "- consulta Y\n  -> ok")
        self.assertIsNone(self.db.buscar_abordagem("321_b", "chave1"))
        self.assertIsNone(self.db.buscar_abordagem("321_a", ""))
        self.db.guardar_abordagem("321_a", "", "sem chave")
        self.assertIsNone(self.db.buscar_abordagem("321_a", ""))

    def test_a_avaliacao_guarda_a_abordagem_junto(self):
        self.db.registrar_avaliacao("321_a", 0, "321", "dom.md", "up", "Qual o custo?", "Custo: BPL 10.", True,
                                    "- consulta X\n  -> ok")
        self.assertEqual(self._linha()["abordagem"], "- consulta X\n  -> ok")

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
