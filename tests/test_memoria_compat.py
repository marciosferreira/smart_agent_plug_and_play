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
import memoria
from memoria import MAX_LICOES, MudancasMemoria, Operacao, aplicar_operacoes

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


def _aplica(ops, lic=None, pref=None, perfil=None, tem_usuario=True):
    return aplicar_operacoes(ops, lic or {}, pref or {}, perfil or {}, tem_usuario)


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
