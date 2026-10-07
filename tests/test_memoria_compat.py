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
from memoria import CampoPerfil, MAX_LICOES, MudancasMemoria, aplicar_licoes, aplicar_perfil, aplicar_preferencias

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


def _m(**campos):
    return MudancasMemoria(raciocinio="teste", **campos)


class MesclaLicoes(unittest.TestCase):
    def test_acrescenta_verificada_nova(self):
        doc = aplicar_licoes({}, _m(licoes_verificadas_novas=["Usar USD"]))
        self.assertEqual(doc, {"verificadas": ["Usar USD"], "hipoteses": []})

    def test_nao_duplica_frase_igual(self):
        doc = {"verificadas": ["Usar a coluna dt_producao para data"], "hipoteses": []}
        novo = aplicar_licoes(doc, _m(licoes_verificadas_novas=["usar a coluna dt_producao para data."]))
        self.assertEqual(novo["verificadas"], doc["verificadas"])

    def test_nao_confunde_regras_parecidas_mas_diferentes(self):
        doc = {"verificadas": ["Na tabela producao, a coluna de data é dt_producao"], "hipoteses": []}
        novo = aplicar_licoes(doc, _m(licoes_verificadas_novas=["Na tabela paradas, a coluna de data é dt_parada"]))
        self.assertEqual(len(novo["verificadas"]), 2)

    def test_reescrever_remove_a_antiga_e_acrescenta_a_nova(self):
        doc = {"verificadas": ["Usar apenas USD nos custos"], "hipoteses": []}
        novo = aplicar_licoes(doc, _m(licoes_remover=["Usar apenas USD nos custos"],
                                      licoes_verificadas_novas=["Custos podem ser apresentados em USD ou BRL"]))
        self.assertEqual(novo["verificadas"], ["Custos podem ser apresentados em USD ou BRL"])

    def test_remover_item_inexistente_nao_apaga_nada(self):
        doc = {"verificadas": ["Usar USD"], "hipoteses": ["Incluir a data no join"]}
        self.assertEqual(aplicar_licoes(doc, _m(licoes_remover=["algo que nunca existiu"])), doc)

    def test_promover_hipotese(self):
        doc = {"verificadas": [], "hipoteses": ["Incluir a data no join"]}
        novo = aplicar_licoes(doc, _m(licoes_promover=["Incluir a data no join"]))
        self.assertEqual(novo, {"verificadas": ["Incluir a data no join"], "hipoteses": []})

    def test_verificada_nova_substitui_a_hipotese_igual(self):
        doc = {"verificadas": [], "hipoteses": ["Incluir a data no join"]}
        novo = aplicar_licoes(doc, _m(licoes_verificadas_novas=["Incluir a data no join"]))
        self.assertEqual(novo, {"verificadas": ["Incluir a data no join"], "hipoteses": []})

    def test_hipotese_igual_a_verificada_e_ignorada(self):
        doc = {"verificadas": ["Usar USD"], "hipoteses": []}
        self.assertEqual(aplicar_licoes(doc, _m(licoes_hipoteses_novas=["Usar USD"])), doc)

    def test_limite_descarta_hipoteses_primeiro(self):
        verificadas = [f"regra verificada numero {i} " + "x" * i * 3 for i in range(MAX_LICOES)]
        novo = aplicar_licoes({"verificadas": verificadas, "hipoteses": []},
                              _m(licoes_hipoteses_novas=["uma hipotese a mais, bem diferente"]))
        self.assertEqual(len(novo["verificadas"]) + len(novo["hipoteses"]), MAX_LICOES)
        self.assertEqual(novo["verificadas"], verificadas)
        self.assertEqual(novo["hipoteses"], [])


class MesclaPreferenciasEPerfil(unittest.TestCase):
    def test_trocar_preferencia_por_assunto(self):
        doc = {"preferencias": ["Gráficos na cor laranja", "Respostas curtas"]}
        novo = aplicar_preferencias(doc, _m(preferencias_remover=["Gráficos na cor laranja"],
                                            preferencias_novas=["Gráficos na cor rosa"]))
        self.assertEqual(novo["preferencias"], ["Respostas curtas", "Gráficos na cor rosa"])

    def test_perfil_define_corrige_e_apaga_campos(self):
        doc = {"nome": "Marico", "cargo": "Cientista de dados", "outros": ["Apresenta ao diretor"]}
        novo = aplicar_perfil(doc, _m(perfil_definir=[CampoPerfil(campo="nome", valor="Marcio"),
                                                       CampoPerfil(campo="cargo", valor="")]))
        self.assertEqual(novo["nome"], "Marcio")
        self.assertIsNone(novo["cargo"])
        self.assertEqual(novo["outros"], ["Apresenta ao diretor"])

    def test_perfil_outros_sem_duplicar(self):
        doc = {"outros": ["Apresenta ao diretor toda segunda"]}
        novo = aplicar_perfil(doc, _m(perfil_outros_novos=["Apresenta ao diretor toda segunda."]))
        self.assertEqual(novo["outros"], doc["outros"])


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
