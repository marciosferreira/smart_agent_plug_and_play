"""
Ferramentas do pack de alocação: a base demo, os modelos treinados e as tools que o
agente recebe quando este domínio está ativo (carregadas por agent_multi._ferramentas_do_pack).

  simulador.py   regras de alocação simplificadas (só para gerar a base demo)
  gerar_demo.py  gera o allocation.db demo com histórico      python -m ferramentas.gerar_demo
  features.py    variáveis dos modelos, iguais no treino e na tool
  treinar.py     treina e valida os modelos                    python -m ferramentas.treinar
  modelos/       o artefato treinado (alocacao.joblib, fora do git) e as métricas

Duas portas para a mesma previsão (previsao_proximo_quarter, com cache):
  criar_tools    a tool do analista no chat (prever_risco_proximo_quarter)
  FUNCOES        funções para tarefas agendadas e painéis, via ctx.pack(nome) — o código
                 do run() roda num sandbox sem imports e não carregaria o modelo sozinho

A tool também devolve o diagnóstico de capacidade (diagnostico_capacidade, resumo_por_produto) e
a política de recomendação do domínio (seção "Recomendações de ação" do dominio.md): o modelo diz
ONDE está o risco, a política diz O QUE FAZER.

Os comandos rodam na pasta config/alocacao.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import numpy as np
import pandas as pd

ROTULOS = {
    "prever_risco_proximo_quarter": {
        "pt-BR": "📈 Prevendo demanda e risco do próximo quarter",
        "en": "📈 Forecasting next-quarter demand and risk",
        "zh": "📈 正在预测下季度需求与风险",
    },
}

_ARTEFATO = Path(__file__).parent / "modelos" / "alocacao.joblib"
_cache: dict = {"mtime": None, "artefato": None}
_trava = threading.Lock()


def _artefato() -> dict | None:
    """O artefato treinado, recarregado quando o arquivo muda (novo treino)."""
    if not _ARTEFATO.exists():
        return None
    mtime = _ARTEFATO.stat().st_mtime
    with _trava:
        if _cache["mtime"] != mtime:
            import joblib  # só aqui: carregar o scikit-learn custa ~1 s

            _cache.update(mtime=mtime, artefato=joblib.load(_ARTEFATO))
        return _cache["artefato"]


def _faixa(p: float) -> str:
    return "alto" if p >= .6 else "médio" if p >= .3 else "baixo"


def _filtrar(df: pd.DataFrame, coluna: str, termo: str) -> pd.DataFrame:
    """Casa por pedaços do termo (ignora espaço, hífen, barra e maiúsculas), como pede o
    dominio.md para nomes de produto e KA digitados de forma livre."""
    tokens = [t for t in re.split(r"[\s\-/_]+", termo.lower()) if t]
    if not tokens:
        return df
    alvo = df[coluna].astype(str).str.lower()
    return df[np.logical_and.reduce([alvo.str.contains(re.escape(t)) for t in tokens])]


def _tabela(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) if not df.empty else "(nenhuma linha)"


_previsoes: dict = {}  # (mtime do artefato, assinatura da base) → (DataFrame, info)


def _calcular(engine) -> tuple[pd.DataFrame | None, dict]:
    """A previsão completa do próximo quarter (todas as KAs e produtos), com o que o texto da
    tool precisa em `info`. Fica em cache enquanto o modelo e a base não mudam: recalcular
    custa alguns segundos, e o painel do dashboard pede a cada carga."""
    from . import features as F

    art = _artefato()
    if art is None:
        return None, {"erro": "Os modelos deste domínio ainda não foram treinados: rode "
                              "`python -m ferramentas.treinar` na pasta config/alocacao e tente de novo."}
    with engine.connect() as conn:
        assinatura = F.assinatura(conn)
        chave = (_cache["mtime"], tuple(sorted(assinatura.items())))
        with _trava:
            if chave in _previsoes:
                return _previsoes[chave]
        df = F.ler_base(conn)
    alvo = df[df.month_status == "next"]
    if alvo.empty:
        return None, {"erro": "A base não tem o próximo quarter (linhas com month_status = 'next')."}
    p = F.painel(df)
    r = F.prever(df, p, art["demanda"], art["risco"], p["t1"], alvo.drop(columns=["req_qty", "alocado"]),
                 art.get("calibracao"))
    out = pd.DataFrame({
        "fiscal_year": r.fiscal_year, "quarter": r.quarter, "year_month": r.year_month,
        "key_account_code": r.key_account_code, "product": r["product"], "origin": r.origin,
        "product_group": r.product_group,
        "material_id": r.material_id, "demanda_prevista": r.demanda_prevista,
        "rollover_estimado": r.rollover_estimado.astype(int), "capacidade": r.capacidade.astype(int),
        "pressao_prevista": r.pressao.round(2), "pressao_ate_meu_score": r.pressao_ate_meu_score.round(2),
        "score_estimado": r.score.astype(int), "woi": r.woi.round(1),
        "prob_nao_atendimento": r.prob_nao_atendimento.round(3),
        "risco": r.prob_nao_atendimento.map(_faixa),
    }).sort_values(["prob_nao_atendimento", "demanda_prevista"], ascending=False).reset_index(drop=True)
    info = {"artefato": art, "origem": f"{p['t1'] // 12}-{p['t1'] % 12 + 1:02d}",
            "base_mudou": art.get("assinatura_dados") != assinatura}
    with _trava:
        _previsoes.clear()
        _previsoes[chave] = (out, info)
    return out, info


def diagnostico_capacidade(prev: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por produto (material_id) x mês: demanda prevista de TODAS as KAs contra a
    capacidade do One Plan, com a falta de capacidade e a folga. A capacidade é do produto e é
    compartilhada entre as KAs, então `prev` deve ser a previsão completa, sem filtro de KA."""
    g = prev.groupby(["material_id", "product", "origin", "year_month"], as_index=False).agg(
        demanda_prevista=("demanda_prevista", "sum"), capacidade=("capacidade", "max"),
        pedidos=("risco", "size"), pedidos_risco_alto=("risco", lambda s: int((s == "alto").sum())),
    )
    g["falta_capacidade"] = (g.demanda_prevista - g.capacidade).clip(lower=0)
    g["folga"] = (g.capacidade - g.demanda_prevista).clip(lower=0)
    return g


def resumo_por_produto(g: pd.DataFrame) -> pd.DataFrame:
    """Por produto, no quarter: a falta, quanto dela cabe na folga de OUTRO mês do mesmo produto
    (alavanca de antecipar produção) e a falta residual, que só se resolve com mais capacidade,
    realocação entre produtos ou conversa com a KA. Só os produtos com falta, do maior para o menor."""
    p = g.groupby(["material_id", "product", "origin"], as_index=False).agg(
        capacidade_quarter=("capacidade", "sum"), falta_quarter=("falta_capacidade", "sum"),
        folga_quarter=("folga", "sum"), meses_com_falta=("falta_capacidade", lambda s: int((s > 0).sum())),
        pedidos_risco_alto=("pedidos_risco_alto", "sum"),
    )
    p["movivel_no_quarter"] = np.minimum(p.falta_quarter, p.folga_quarter)
    p["falta_residual"] = p.falta_quarter - p.movivel_no_quarter
    p["residual_pct_capacidade"] = (100 * p.falta_residual / p.capacidade_quarter.replace(0, np.nan)).round(1)
    return p[p.falta_quarter > 0].sort_values("falta_quarter", ascending=False).reset_index(drop=True)


def _texto_diagnostico(g: pd.DataFrame, p: pd.DataFrame) -> str:
    n_pm, n_falta = len(g), int((g.falta_capacidade > 0).sum())
    cap, dem = int(g.capacidade.sum()), int(g.demanda_prevista.sum())
    falta, folga = int(g.falta_capacidade.sum()), int(g.folga.sum())
    movivel, residual = int(p.movivel_no_quarter.sum()), int(p.falta_residual.sum())
    por_mes = g.groupby(g.year_month.str[:7])[["falta_capacidade", "folga"]].sum().astype(int).reset_index().rename(
        columns={"year_month": "mes"})
    por_site = g.groupby("origin")[["capacidade", "falta_capacidade", "folga"]].sum().astype(int).reset_index()
    topo = p.head(10)[["product", "origin", "falta_quarter", "meses_com_falta", "folga_quarter",
                       "movivel_no_quarter", "falta_residual", "residual_pct_capacidade", "pedidos_risco_alto"]]
    conc = int(round(100 * p.head(10).falta_quarter.sum() / falta)) if falta else 0
    n = lambda x: f"{int(x):,}".replace(",", ".")  # 1.126.970 (só os números levam ponto)
    linhas = [
        f"Capacidade do quarter {n(cap)} contra demanda prevista {n(dem)} (uso {dem / cap:.0%}). Produto-mês com "
        f"demanda acima da capacidade: {n_falta} de {n_pm}. Falta de capacidade somada: {n(falta)} unidades "
        f"({str(round(100 * falta / cap, 1)).replace('.', ',')}% da capacidade); folga nos demais: {n(folga)}.",
        f"Da falta, {n(movivel)} cabem na folga de outro mês do mesmo produto (antecipar produção) e {n(residual)} "
        f"são falta residual (só mais capacidade, realocação entre produtos ou conversa com a KA). Os 10 produtos "
        f"de maior falta respondem por {conc}% dela.",
        "Falta e folga por mês:\n" + _tabela(por_mes),
        "Por site (origem):\n" + _tabela(por_site),
        "Os 10 produtos de maior falta:\n" + _tabela(topo),
    ]
    return "\n\n".join(linhas)


def previsao_proximo_quarter(key_account: str = "", produto: str = "") -> pd.DataFrame:
    """Para tarefas e painéis (ctx.pack("previsao_proximo_quarter")): uma linha por KA x
    produto x mês do próximo quarter, com as mesmas colunas da tool do chat."""
    import db_config

    out, info = _calcular(db_config.get_engine())
    if out is None:
        raise ValueError(info["erro"])
    if key_account:
        out = _filtrar(out, "key_account_code", key_account)
    if produto:
        out = _filtrar(out, "product", produto)
    return out


FUNCOES = {"previsao_proximo_quarter": previsao_proximo_quarter}


def criar_tools(ctx) -> list:
    """`ctx` (de agent_multi): ns_get, ns_summary, get_engine, tlog."""
    from langchain_core.tools import tool

    @tool
    def prever_risco_proximo_quarter(chave: str, key_account: str = "", produto: str = "",
                                     recomendar: bool = False) -> str:
        """Prevê, para o PRÓXIMO quarter (as linhas `next`, que ainda não têm request), a demanda
        de cada KA x produto x mês e a probabilidade de essa demanda NÃO ser atendida, com
        modelos treinados no histórico desta base. Use para perguntas sobre o futuro: "quais
        KAs/produtos correm risco de faltar produto no próximo quarter?", "qual a demanda
        prevista da CLARO em novembro?", "onde a capacidade do One Plan não vai dar conta?".
        Para o que JÁ aconteceu, use SQL: esta tool não responde sobre o passado.

        O resultado vai para o ambiente como DataFrame com o nome `chave` (para analisar_dataframe,
        gráficos e Excel), uma linha por KA x produto x mês, com: demanda_prevista (pedido
        previsto, já com o rollover estimado no 1º mês), capacidade (One Plan do produto no
        mês), pressao_prevista (demanda prevista de todas as KAs / capacidade),
        pressao_ate_meu_score (só a demanda que é atendida antes ou junto com esta linha, pelo
        Score), score_estimado, woi, prob_nao_atendimento (0 a 1) e risco (alto/médio/baixo).
        O texto devolvido traz um resumo por KA e por mês, as linhas de maior risco e as
        métricas de validação dos modelos — cite-as ao apresentar a previsão.

        A resposta traz sempre um diagnóstico de capacidade (falta, folga, quanto cabe em outro
        mês do mesmo produto e a falta residual; vai ao ambiente como DataFrame
        `<chave>_capacidade`). Se o pedido for sobre o que FAZER ("o que recomenda?", "devo
        aumentar a produção?", "como resolver?"), chame com recomendar=True: a resposta passa a
        começar pela tarefa e pela política de recomendação do domínio, e você deve responder
        com as ações que ela pede (e não com um resumo da previsão).

        Ao cruzar com o histórico: demanda_prevista estima o PEDIDO INICIAL, então compare com
        `initial_req_qty` (não com `req_qty`, que inclui rollover e rollback); no 1º mês ela já
        soma o rollover_estimado. O mesmo `product` pode ter dois material_id (origens JAG e
        MAN): para contar linhas ou meses por produto, agrupe por material_id ou (product, origin).

        Args:
            chave: nome do DataFrame no ambiente (ex: 'risco_fq3').
            key_account: filtra por KA (ex: 'CLARO_BR' ou só 'claro'); vazio = todas.
            produto: filtra por pedaços do nome do produto (ex: 'naples 26'); vazio = todos.
            recomendar: True quando o usuário quer saber o que fazer com o risco.
        """
        ctx.tlog("prever_risco_proximo_quarter", "CHAMADA", chave=chave, key_account=key_account,
                 produto=produto, recomendar=recomendar)
        out, info = _calcular(ctx.get_engine())
        if out is None:
            return info["erro"]
        art = info["artefato"]
        # a capacidade é do produto e compartilhada entre as KAs: o diagnóstico usa a previsão
        # completa, e o filtro só escolhe quais produtos mostrar
        diag = diagnostico_capacidade(out)
        if key_account:
            out = _filtrar(out, "key_account_code", key_account)
        if produto:
            out = _filtrar(out, "product", produto)
        out = out.reset_index(drop=True)
        diag = diag[diag.material_id.isin(out.material_id)].reset_index(drop=True)
        ns = ctx.ns_get()
        ns[chave] = out
        ns[f"{chave}_capacidade"] = diag
        ctx.tlog("prever_risco_proximo_quarter", "RETORNO", linhas=len(out))
        if out.empty:
            return f"DataFrame '{chave}' vazio: nenhuma linha do próximo quarter com demanda prevista para esse filtro."

        quarter = f"{out.quarter.iloc[0]} de fiscal_year {int(out.fiscal_year.iloc[0])}"
        meses = ", ".join(sorted(out.year_month.str[:7].unique()))
        por_ka = out.groupby("key_account_code").agg(
            linhas=("risco", "size"), risco_alto=("risco", lambda s: int((s == "alto").sum())),
            demanda_prevista=("demanda_prevista", "sum"),
            demanda_em_risco_alto=("demanda_prevista", lambda s: int(s[out.loc[s.index, "risco"] == "alto"].sum())),
        ).reset_index()
        por_mes = out.groupby(out.year_month.str[:7]).agg(
            linhas=("risco", "size"), risco_alto=("risco", lambda s: int((s == "alto").sum())),
            prob_media=("prob_nao_atendimento", "mean"), demanda_prevista=("demanda_prevista", "sum"),
        ).round(3).reset_index().rename(columns={"year_month": "mes"})
        topo = out.head(10)[["year_month", "key_account_code", "product", "demanda_prevista", "capacidade",
                             "pressao_prevista", "score_estimado", "prob_nao_atendimento"]]
        m = art.get("metricas", {})
        md, mr = m.get("demanda", {}), m.get("risco", {})
        import db_config

        politica = db_config.get_secao("Recomendações de ação")
        avisos = []
        if info["base_mudou"]:
            avisos.append("⚠️ A base mudou desde o treino dos modelos: retreine (`python -m ferramentas.treinar`) "
                          "antes de confiar nesta previsão.")
        diagnostico = (
            f"Diagnóstico de capacidade (demanda de TODAS as KAs contra o One Plan do produto; DataFrame "
            f"'{chave}_capacidade', uma linha por produto x mês). Atenção: \"falta de capacidade\" é a "
            f"capacidade que falta no produto, e NÃO é o volume em risco alto (esse é a demanda dos pedidos "
            f"de risco alto, coluna demanda_em_risco_alto do resumo por KA):\n\n"
            + _texto_diagnostico(diag, resumo_por_produto(diag)))
        previsao = [
            f"DataFrame '{chave}' injetado no ambiente: {len(out)} linhas (KA x produto x mês), "
            f"colunas: {list(out.columns)}.",
            f"Próximo quarter: {quarter} ({meses}), previsto a partir dos pedidos até {info['origem']}.",
            "Resumo por KA:\n" + _tabela(por_ka),
            "Resumo por mês:\n" + _tabela(por_mes),
            "As 10 linhas de maior risco:\n" + _tabela(topo),
            ("Como ler: prob_nao_atendimento é a chance de a alocação ficar abaixo do Request Final da linha; "
             "pressao_prevista > 1 quer dizer que a demanda prevista de todas as KAs passa da capacidade do "
             "produto no mês, e pressao_ate_meu_score > 1 que nem a demanda atendida antes desta linha (pelo "
             "Score) cabe. São previsões: a demanda futura é incerta, deals e rollback do próximo quarter "
             "ainda não existem e ficam de fora."),
            (f"Validação fora do tempo (modelos treinados em {art.get('treinado_em', '?')}): demanda — erro "
             f"percentual (WAPE) por linha {md.get('modelo', {}).get('wape_linha', '?')} contra "
             f"{md.get('media_3_meses', {}).get('wape_linha', '?')} da média dos 3 últimos meses; risco — AUC "
             f"{mr.get('modelo', {}).get('auc', '?')} nos quarters {', '.join(mr.get('quarters_teste', []))} "
             f"(taxa real de não atendimento {mr.get('taxa_nao_atendimento_teste', '?')}). Base demo SINTÉTICA: "
             "os números não descrevem o negócio real."),
            *avisos,
        ]
        if not recomendar:
            return "\n\n".join(previsao[:5] + [diagnostico] + previsao[5:]) + ctx.ns_summary(ns)
        if not politica:
            return ("\n\n".join(previsao[:5] + [diagnostico] + previsao[5:])
                    + "\n\nO domínio não traz política de recomendação: descreva o diagnóstico e não proponha ações."
                    + ctx.ns_summary(ns))
        tarefa = (
            "TAREFA: o usuário quer saber o que FAZER com o risco. Responda com AÇÕES RECOMENDADAS, seguindo a "
            "política do domínio logo abaixo, e não com um resumo da previsão: de 3 a 5 ações, da mais barata para "
            "a mais cara, cada uma com o produto e o site, o mês, o número do diagnóstico que a justifica, o "
            "responsável sugerido e o prazo. Diga com clareza que são HIPÓTESES de demonstração. Os dados de "
            "apoio (diagnóstico e previsão) vêm depois da política.")
        return "\n\n".join([tarefa, "Política de recomendação do domínio:\n\n" + politica, diagnostico] + previsao) \
            + ctx.ns_summary(ns)

    return [prever_risco_proximo_quarter]
