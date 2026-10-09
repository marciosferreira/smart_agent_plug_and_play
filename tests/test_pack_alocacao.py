"""
Testes do pack de alocação: o simulador da base demo, as variáveis dos modelos, o
carregamento das tools de um pack pelo agente e, se a base demo e o modelo treinado
existirem, a tool de previsão de ponta a ponta (sem rede e sem LLM).

Rodar:  .venv/Scripts/python -m unittest discover -s tests -v
"""
import importlib
import importlib.util
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import agent_multi
import db_config

RAIZ = Path(__file__).resolve().parents[1]
PACK = RAIZ / "config" / "alocacao"


def _carregar_pacote(pasta: Path, nome: str):
    """Carrega a pasta como pacote com outro nome, como agent_multi._ferramentas_do_pack."""
    spec = importlib.util.spec_from_file_location(nome, pasta / "__init__.py", submodule_search_locations=[str(pasta)])
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


PACOTE = "teste_pack_alocacao_ferramentas"
_carregar_pacote(PACK / "ferramentas", PACOTE)
sim = importlib.import_module(f"{PACOTE}.simulador")
F = importlib.import_module(f"{PACOTE}.features")


class Simulador(unittest.TestCase):
    """As regras de alocação simplificadas que geram a base demo."""

    def test_distribuir_atende_por_prioridade_e_rateia_dentro_dela(self):
        dado, sobra = sim.distribuir(100, np.array([30, 60, 60, 50]), np.array([0, 1, 1, 7]))
        self.assertEqual(dado[0], 30)                    # prioridade 0 inteira
        self.assertEqual(dado[1] + dado[2], 70)          # prioridade 1 rateia o resto
        self.assertEqual(dado[1], dado[2])               # proporcional ao pedido (iguais)
        self.assertEqual(dado[3], 0)                     # prioridade 7 não recebe nada
        self.assertEqual(sobra, 0)

    def test_quarter_com_falta_rollback_rollover_e_retail_com_o_residuo(self):
        cap = np.array([[100, 100, 40]])                 # 1 material, 3 meses: falta no 3º
        combo_mat = np.array([0, 0, 0])
        retail = np.array([False, False, True])
        pedidos = np.array([[50, 50, 0], [20, 20, 90], [0, 0, 0]])
        plano = sim.alocar_quarter(cap, combo_mat, retail, pedidos, np.array([0, 1, 7]), [],
                                   np.array([0, 0, 0]), carry=0.8)
        aloc = plano["alocado"]
        np.testing.assert_array_equal(aloc.sum(0), cap[0])        # a capacidade é toda usada
        self.assertEqual(aloc[0, 0], 50)                          # MMICOM (Score 0) inteiro
        self.assertEqual(plano["rollback"][1, 1], 30)             # falta do 3º mês volta para o 2º
        self.assertEqual(plano["rollback"][1, 0], 20)             # e o resto para o 1º
        self.assertEqual(plano["req"][1, 1], 20 + 30)             # rollback soma ao Request Final
        self.assertEqual(plano["rollover_saida"][1], 0)           # nada sobrou para o próximo quarter
        self.assertEqual(aloc[2].sum(), cap.sum() - aloc[:2].sum())  # Retail fica com o resíduo

    def test_divisao_por_semanas_nunca_passa_da_capacidade_da_semana(self):
        rng = np.random.default_rng(1)
        for _ in range(200):
            n_mat, n_ka = 3, 4
            combo_mat = np.repeat(np.arange(n_mat), n_ka + 1)
            retail = np.tile([False] * n_ka + [True], n_mat)
            cap_sem = rng.integers(0, 400, size=(n_mat, 5))
            mes = np.zeros(len(combo_mat), dtype=np.int64)
            for m in range(n_mat):
                ks = np.where((combo_mat == m) & ~retail)[0]
                mes[ks] = rng.multinomial(int(cap_sem[m].sum() * rng.uniform(.3, 1)), np.ones(n_ka) / n_ka)
            out = sim.dividir_semanas(mes, combo_mat, retail, cap_sem)
            self.assertTrue((out >= 0).all())
            for m in range(n_mat):
                np.testing.assert_array_equal(out[combo_mat == m].sum(0), cap_sem[m])
                np.testing.assert_array_equal(out[(combo_mat == m) & ~retail].sum(1), mes[(combo_mat == m) & ~retail])


class Variaveis(unittest.TestCase):
    def test_score_segue_a_tabela_do_dominio(self):
        s = F.score_estimado(woi=[0, 12, 12, 12, 20, 20, 20, 3],
                             booked=[0, 5, 0, 0, 5, 0, 0, 0], entered=[0, 0, 5, 0, 0, 5, 0, 0],
                             ka=["CLARO_BR"] * 7 + ["MMICOM_BR"])
        self.assertEqual(list(s), [1, 2, 3, 4, 5, 6, 7, 0])

    def _painel(self, totais_por_mes: list[float]) -> dict:
        m = np.array([totais_por_mes, totais_por_mes]) / 2  # duas combinações, mesma série
        return {"pedidos": m, "t0": 2024 * 12 + 6, "t1": 2024 * 12 + 6 + len(totais_por_mes) - 1}

    def test_perfil_sazonal_e_fator_do_mes_alvo(self):
        serie = [100.0] * 12
        serie[4] = 140.0  # novembro (o painel começa em julho)
        p = self._painel(serie)
        origem = p["t1"]  # junho do ano seguinte
        perfil = F.perfil_sazonal(p, origem)
        self.assertAlmostEqual(perfil[11] / perfil[10], 1.4)
        self.assertAlmostEqual(F.fator_sazonal(perfil, origem, origem + 5), perfil[11] / perfil[6], places=6)
        self.assertEqual(F.fator_sazonal(None, origem, origem + 1), 1.0)
        self.assertIsNone(F.perfil_sazonal(self._painel([100.0] * 6), 2024 * 12 + 11))  # pouco histórico

    def test_previsao_reconciliada_soma_o_total_esperado_da_ka(self):
        am = pd.DataFrame({"origem": 1, "t": [2, 2, 2, 3], "ka": [0, 0, 1, 0],
                           "ka_media3": [100.0, 100.0, 50.0, 100.0], "sazonal": [1.2, 1.2, 1.0, 0.9]})
        modelo = mock.Mock()
        modelo.predict.return_value = np.array([30.0, 10.0, 20.0, 0.0])
        with mock.patch.object(F, "FEATURES_DEMANDA", ["ka"]):
            pred = F.prever_demanda(modelo, am)
        self.assertAlmostEqual(pred[0] + pred[1], 120.0)   # KA 0 em t=2: 100 x 1,2
        self.assertAlmostEqual(pred[0] / pred[1], 3.0)     # o modelo decide a repartição
        self.assertAlmostEqual(pred[2], 50.0)
        self.assertEqual(pred[3], 0.0)                     # nada previsto: não inventa demanda

    def test_calibracao_sigmoide(self):
        np.testing.assert_allclose(F.calibrar([.2, .8], None), [.2, .8])
        np.testing.assert_allclose(F.calibrar([.5], {"a": 1.0, "b": 0.0}), [.5])
        self.assertLess(F.calibrar([.9], {"a": .5, "b": 0.0})[0], .9)  # a < 1 puxa para o meio


class DiagnosticoDeCapacidade(unittest.TestCase):
    """A conta que sustenta as recomendações: falta, folga e o que cabe em outro mês."""

    def _prev(self):
        linhas = []
        # produto 1: falta em outubro (150 contra 100), folga em novembro (120 contra 200) e em dezembro (50 contra 200)
        # produto 2: falta em dois meses, sem folga em lugar nenhum
        for mat, ym, dem_ka, cap, risco in [
            (1, "2026-10-01", [100, 50], 100, "alto"), (1, "2026-11-01", [120], 200, "baixo"),
            (1, "2026-12-01", [50], 200, "baixo"),
            (2, "2026-10-01", [300], 200, "alto"), (2, "2026-11-01", [260], 200, "médio"),
        ]:
            for i, d in enumerate(dem_ka):
                linhas.append(dict(material_id=mat, product=f"P{mat}", origin="JAG", year_month=ym,
                                   key_account_code=f"KA{i}", demanda_prevista=d, capacidade=cap, risco=risco))
        return pd.DataFrame(linhas)

    def test_falta_folga_e_o_que_cabe_em_outro_mes_do_mesmo_produto(self):
        pacote = importlib.import_module(PACOTE)
        g = pacote.diagnostico_capacidade(self._prev())
        self.assertEqual(len(g), 5)                                    # uma linha por produto x mês
        out = g[(g.material_id == 1) & (g.year_month == "2026-10-01")].iloc[0]
        self.assertEqual((out.demanda_prevista, out.capacidade, out.falta_capacidade), (150, 100, 50))
        self.assertEqual(out.pedidos_risco_alto, 2)                    # as duas KAs do mês
        p = pacote.resumo_por_produto(g).set_index("material_id")
        self.assertEqual((p.loc[1, "falta_quarter"], p.loc[1, "folga_quarter"]), (50, 80 + 150))
        self.assertEqual((p.loc[1, "movivel_no_quarter"], p.loc[1, "falta_residual"]), (50, 0))  # cabe na folga
        self.assertEqual((p.loc[2, "falta_quarter"], p.loc[2, "movivel_no_quarter"]), (160, 0))
        self.assertEqual(p.loc[2, "falta_residual"], 160)               # sem folga: só mais capacidade
        self.assertEqual(p.loc[2, "residual_pct_capacidade"], 40.0)
        self.assertEqual(list(pacote.resumo_por_produto(g).material_id), [2, 1])  # maior falta primeiro

    def test_produto_sem_falta_nao_entra_no_resumo(self):
        pacote = importlib.import_module(PACOTE)
        g = pacote.diagnostico_capacidade(self._prev().query("material_id == 1 and year_month != '2026-10-01'"))
        self.assertTrue(pacote.resumo_por_produto(g).empty)


class FerramentasDoPack(unittest.TestCase):
    """agent_multi._ferramentas_do_pack: o pack traz as próprias tools, sem mexer no agente."""

    def _pack(self, init: str, extra: dict[str, str] | None = None) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        pasta = Path(self._tmp.name) / "meupack" / "ferramentas"
        pasta.mkdir(parents=True)
        (pasta / "__init__.py").write_text(textwrap.dedent(init), encoding="utf-8")
        for nome, codigo in (extra or {}).items():
            (pasta / nome).write_text(textwrap.dedent(codigo), encoding="utf-8")
        return pasta.parent

    def tearDown(self):
        self._tmp.cleanup()

    def test_carrega_tools_com_import_relativo_e_registra_os_rotulos(self):
        pack = self._pack('''
            from langchain_core.tools import tool
            from .ajuda import dobro
            ROTULOS = {"dobrar": {"pt-BR": "✖ Dobrando", "en": "✖ Doubling"}}
            def criar_tools(ctx):
                @tool
                def dobrar(x: int) -> int:
                    """Dobra x."""
                    return dobro(x)
                return [dobrar]
        ''', {"ajuda.py": "def dobro(x):\n    return 2 * x\n"})
        with mock.patch.object(db_config, "pack_dir", return_value=pack):
            tools = agent_multi._ferramentas_do_pack()
        self.assertEqual([t.name for t in tools], ["dobrar"])
        self.assertEqual(tools[0].invoke({"x": 21}), 42)
        self.assertEqual(agent_multi._TOOL_LABELS["dobrar"], "✖ Dobrando")
        self.assertEqual(agent_multi._LABEL_TRANSLATIONS["✖ Dobrando"], {"en": "✖ Doubling"})

    def test_pack_sem_ferramentas_ou_quebrado_nao_derruba_o_agente(self):
        with tempfile.TemporaryDirectory() as vazio, \
                mock.patch.object(db_config, "pack_dir", return_value=Path(vazio)):
            self.assertEqual(agent_multi._ferramentas_do_pack(), [])
        pack = self._pack("raise RuntimeError('pack quebrado')\n")
        with mock.patch.object(db_config, "pack_dir", return_value=pack), \
                self.assertLogs(agent_multi.logger, level="WARNING"):
            self.assertEqual(agent_multi._ferramentas_do_pack(), [])


class FuncoesDoPackParaTarefasEPaineis(FerramentasDoPack):
    """ctx.pack: o código de tarefas e painéis (sandbox sem imports) usa funções do pack."""

    def test_ctx_pack_chama_a_funcao_e_devolve_lista_de_dicts(self):
        from scheduler.context import TaskContext

        pack = self._pack('''
            import pandas as pd
            def dobros(n=2):
                return pd.DataFrame({"x": range(n), "dobro": [2 * i for i in range(n)]})
            FUNCOES = {"dobros": dobros}
        ''')
        with mock.patch.object(db_config, "pack_dir", return_value=pack):
            ctx = TaskContext("t")
            self.assertEqual(ctx.pack("dobros", n=3), [{"x": 0, "dobro": 0}, {"x": 1, "dobro": 2}, {"x": 2, "dobro": 4}])
            with self.assertRaisesRegex(ValueError, "disponíveis: dobros"):
                ctx.pack("inexistente")


def _base_demo_pronta() -> bool:
    db, art = RAIZ / "allocation.db", PACK / "ferramentas" / "modelos" / "alocacao.joblib"
    if not (db.exists() and art.exists()):
        return False
    with sqlite3.connect(db) as c:
        colunas = [r[1] for r in c.execute("PRAGMA table_info(ka_deal_allocation)")]
    return "fiscal_year" in colunas


@unittest.skipUnless(_base_demo_pronta(), "base demo ou modelo ausente: rode ferramentas.gerar_demo e ferramentas.treinar")
class ToolDePrevisao(unittest.TestCase):
    """A tool de ponta a ponta, na base demo e com o modelo treinado."""

    def test_previsao_do_proximo_quarter(self):
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{RAIZ / 'allocation.db'}")
        with mock.patch.object(db_config, "pack_dir", return_value=PACK), \
                mock.patch.object(db_config, "get_engine", return_value=engine):
            tools = {t.name: t for t in agent_multi._ferramentas_do_pack()}
        texto = tools["prever_risco_proximo_quarter"].invoke({"chave": "teste_risco", "key_account": "vivo"})
        df = agent_multi._ns_get()["teste_risco"]
        self.assertIn("Resumo por KA", texto)
        self.assertIn("AUC", texto)
        self.assertEqual(set(df.key_account_code), {"VIVO_BRA"})
        self.assertEqual(set(df.month_status) if "month_status" in df else {"next"}, {"next"})
        self.assertTrue(df.prob_nao_atendimento.between(0, 1).all())
        self.assertTrue((df.demanda_prevista > 0).all())
        self.assertTrue(df.prob_nao_atendimento.is_monotonic_decreasing)

    def _chamar(self, secao: str | None = None, **kw) -> tuple[str, dict]:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{RAIZ / 'allocation.db'}")
        real = db_config.get_secao
        with mock.patch.object(db_config, "pack_dir", return_value=PACK), \
                mock.patch.object(db_config, "get_engine", return_value=engine), \
                mock.patch.object(db_config, "_dominio_slug", return_value="alocacao"):
            if secao is not None:
                with mock.patch.object(db_config, "get_secao", side_effect=lambda n: secao if n == "Recomendações de ação" else real(n)):
                    tool = {t.name: t for t in agent_multi._ferramentas_do_pack()}["prever_risco_proximo_quarter"]
                    texto = tool.invoke({"chave": "rec", **kw})
            else:
                tool = {t.name: t for t in agent_multi._ferramentas_do_pack()}["prever_risco_proximo_quarter"]
                texto = tool.invoke({"chave": "rec", **kw})
        return texto, agent_multi._ns_get()

    def test_a_tool_devolve_sempre_o_diagnostico_e_so_com_recomendar_a_politica(self):
        texto, ns = self._chamar()
        self.assertIn("Diagnóstico de capacidade", texto)
        self.assertIn("falta residual", texto)
        self.assertIn("NÃO é o volume em risco alto", texto)       # a confusão que o analista cometeu
        self.assertNotIn("Política de recomendação", texto)         # ninguém pediu ação: não propõe
        self.assertNotIn("TAREFA:", texto)
        texto, ns = self._chamar(recomendar=True)
        self.assertTrue(texto.startswith("TAREFA:"))                # a tarefa vem primeiro, não no fim
        self.assertIn("HIPÓTESES de demonstração", texto)
        self.assertLess(texto.index("Política de recomendação do domínio"), texto.index("Diagnóstico de capacidade"))
        self.assertLess(texto.index("Diagnóstico de capacidade"), texto.index("Resumo por KA"))
        self.assertIn("HIPÓTESES PARA DEMONSTRAÇÃO", texto)        # o texto da seção do dominio.md
        self.assertIn("Antecipar produção dentro do quarter", texto)
        cap = ns["rec_capacidade"]
        self.assertTrue({"falta_capacidade", "folga", "capacidade"} <= set(cap.columns))
        self.assertEqual(cap.groupby(["material_id", "year_month"]).size().max(), 1)  # produto x mês

    def test_o_diagnostico_considera_todas_as_kas_mesmo_com_filtro(self):
        _, ns = self._chamar(key_account="vivo")
        filtrado = ns["rec"].copy()  # o ambiente é o mesmo dicionário: a próxima chamada o sobrescreve
        self.assertEqual(set(filtrado.key_account_code), {"VIVO_BRA"})
        self._chamar()
        ns_cheio = agent_multi._ns_get()["rec_capacidade"]
        # capacidade é do produto, compartilhada: a demanda do produto inclui as outras KAs
        vivo = filtrado.groupby(["material_id", "year_month"]).demanda_prevista.sum()
        demanda_produto = ns_cheio.set_index(["material_id", "year_month"]).demanda_prevista
        self.assertTrue((demanda_produto.reindex(vivo.index) >= vivo).all())
        self.assertGreater((demanda_produto.reindex(vivo.index) > vivo).sum(), 0)

    def test_sem_politica_no_dominio_a_tool_diz_para_nao_propor_acoes(self):
        texto, _ = self._chamar(secao="", recomendar=True)
        self.assertIn("Diagnóstico de capacidade", texto)
        self.assertFalse(texto.startswith("TAREFA:"))
        self.assertIn("não traz política de recomendação", texto)
        self.assertNotIn("Antecipar produção", texto)

    def test_a_politica_nao_pesa_na_skill_do_dominio(self):
        with mock.patch.object(db_config, "pack_dir", return_value=PACK), \
                mock.patch.object(db_config, "_dominio_slug", return_value="alocacao"):
            self.assertIn("Antecipar produção", db_config.get_secao("Recomendações de ação"))
            self.assertNotIn("Recomendações de ação", db_config.skill_dominio_texto())

    def test_painel_padrao_aplica_o_modelo_nos_tres_idiomas(self):
        from sqlalchemy import create_engine

        from scheduler.widget_runner import run_widget_code

        engine = create_engine(f"sqlite:///{RAIZ / 'allocation.db'}")
        with mock.patch.object(db_config, "pack_dir", return_value=PACK), \
                mock.patch.object(db_config, "get_engine", return_value=engine), \
                mock.patch.object(db_config, "run_select",
                                  side_effect=lambda q, *_: pd.read_sql(q, engine).to_dict("records")), \
                mock.patch.object(db_config, "_dominio_slug", return_value="alocacao"), \
                mock.patch.object(db_config, "modo_acesso", return_value="sql"):
            for lang in ("pt-BR", "en", "zh"):
                paineis = {w["id"]: w for w in db_config.dashboard_default_widgets(lang)}
                self.assertEqual(set(paineis), {"previsao_proximo_quarter", "pedidos_em_risco_proximo_quarter",
                                                "alocacao_por_key_account", "woi_por_faixa_de_risco"})
                pedidos = run_widget_code(paineis["pedidos_em_risco_proximo_quarter"]["code"], "2026-01-01",
                                          "2026-09-30", "teste")
                totais = [sum(v) for v in zip(*(d["data"] for d in pedidos["data"]["datasets"]))]
                self.assertEqual(len(set(totais)), 1)  # alto + médio + baixo = todos os pedidos do mês
                painel = paineis["previsao_proximo_quarter"]
                cfg = run_widget_code(painel["code"], "2026-01-01", "2026-09-30", "teste")
                rotulos, series = cfg["data"]["labels"], cfg["data"]["datasets"]
                self.assertEqual(len(rotulos), 15)  # 12 meses reais + 3 previstos
                self.assertEqual(len(series), 4)
                self.assertTrue(all(v is None for v in series[0]["data"][-3:]))   # real só no passado
                self.assertTrue(all(v and v > 0 for v in series[1]["data"][-3:]))  # previsão no futuro


if __name__ == "__main__":
    unittest.main()
