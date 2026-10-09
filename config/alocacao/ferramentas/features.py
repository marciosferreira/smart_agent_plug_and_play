"""
Variáveis dos dois modelos do pack, calculadas do mesmo jeito no treino (treinar.py) e na
tool do agente (__init__.py). Só usam o que se sabe na data da previsão, e nada do
simulador que gerou a base demo: com uma base real, o mesmo código vale.

Modelo de DEMANDA: o pedido inicial (`initial_req_qty`) de cada KA x material num mês
futuro t, visto de um mês de origem o (o último com pedido conhecido), h = t - o meses à
frente. Usa os pedidos até o, o mesmo mês do ano anterior, a idade do produto, a KA e o mês
do ano.

Modelo de RISCO: a chance de uma linha KA x material x mês NÃO ser atendida (alocação menor
que o Request Final). Usa a pressão sobre a capacidade do material no mês e a demanda que
está à frente na fila de prioridade (Score estimado pela tabela do dominio.md).
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

KAS = ("CLARO_BR", "MMICOM_BR", "TIM_BRA", "VIVO_BRA")  # as que fazem pedido (Retail fica com o resíduo)
CONHECIDOS = ("closed", "done", "ongoing")
SEMANAS = [f"allocation_W{w}" for w in range(1, 6)]
CAPACIDADE = [f"one_plan_week{w}" for w in range(1, 6)]

FEATURES_DEMANDA = [
    "h", "mes_alvo", "ka", "acessorio", "geracao", "idade_alvo", "desde_inicio",
    "lag0", "lag1", "lag2", "media3", "media6", "media12", "ano_passado", "zeros6",
    "ka_media3", "ka_tendencia", "ka_sazonal", "woi", "avg_act",
]
CATEGORICAS_DEMANDA = ["mes_alvo", "ka"]
FEATURES_RISCO = [
    "ka", "score", "woi", "tem_booked", "tem_entered", "log_req", "log_cap",
    "pressao", "pressao_a_frente", "pressao_ate_meu_score", "participacao", "h", "cv6",
]
CATEGORICAS_RISCO = ["ka"]


def ler_base(conn) -> pd.DataFrame:
    """As linhas das KAs que fazem pedido, com o que os modelos precisam."""
    df = pd.read_sql(
        "SELECT a.key_account_code, a.material_id, a.product, a.origin, i.product_group, a.deal_group, "
        "a.year_month, a.month_status, a.fiscal_year, a.quarter, a.initial_req_qty, a.req_qty, "
        "a.booked_quarter, a.entered_quarter, a.woi, a.avg_activations, "
        + ", ".join(f"a.{c}" for c in SEMANAS + CAPACIDADE)
        + " FROM ka_deal_allocation a JOIN ka_input_data i ON i.year_month = a.year_month "
        "AND i.material_id = a.material_id AND i.key_account_id = a.key_account_id "
        "WHERE a.key_account_code IN (" + ", ".join(f"'{k}'" for k in KAS) + ")",
        conn,
    )
    df["alocado"] = df[SEMANAS].sum(1)
    df["capacidade"] = df[CAPACIDADE].sum(1)
    df["t"] = pd.to_datetime(df.year_month).dt.year * 12 + pd.to_datetime(df.year_month).dt.month - 1
    return df


def score_estimado(woi, booked, entered, ka) -> np.ndarray:
    """Tabela de Score do dominio.md (0 = MMICOM ... 7 = menor prioridade)."""
    woi, booked, entered = np.asarray(woi, float), np.asarray(booked), np.asarray(entered)
    s = np.full(len(woi), 7)
    s[(woi >= 15) & (entered > 0)] = 6
    s[(woi >= 15) & (booked > 0)] = 5
    meio = (woi >= 10) & (woi < 15)
    s[meio] = 4
    s[meio & (entered > 0)] = 3
    s[meio & (booked > 0)] = 2
    s[woi < 10] = 1
    s[np.char.find(np.asarray(ka, str), "MMICOM") >= 0] = 0
    return s


def _geracao(product_group: str) -> float:
    token = (product_group or "").split(" ")[0]
    return float(token[-2:]) if token[-2:].isdigit() else np.nan


# ── demanda ───────────────────────────────────────────────────────────────────

def painel(df: pd.DataFrame) -> dict:
    """Pedidos em matriz (combinação KA x material) x mês, só nos meses conhecidos."""
    conhecido = df[df.month_status.isin(CONHECIDOS)]
    t0, t1 = int(conhecido.t.min()), int(conhecido.t.max())
    combos = df[["key_account_code", "material_id"]].drop_duplicates().reset_index(drop=True)
    idx = {(k, m): i for i, (k, m) in enumerate(zip(combos.key_account_code, combos.material_id))}
    mat = np.full((len(combos), t1 - t0 + 1), np.nan)
    linhas = conhecido.apply(lambda r: idx[(r.key_account_code, r.material_id)], axis=1).to_numpy()
    mat[linhas, conhecido.t.to_numpy() - t0] = conhecido.initial_req_qty.to_numpy()
    primeiro_mat = df.groupby("material_id").t.min()
    info = df.drop_duplicates("material_id").set_index("material_id")
    ultimo = df.sort_values("t").drop_duplicates(["key_account_code", "material_id"], keep="last")
    ultimo = ultimo.set_index(["key_account_code", "material_id"])
    return {
        "t0": t0, "t1": t1, "pedidos": mat, "combos": combos,
        "ka": combos.key_account_code.map({k: i for i, k in enumerate(KAS)}).to_numpy(),
        "primeiro": combos.material_id.map(primeiro_mat).to_numpy(),
        "inicio_janela": int(df.t.min()),
        "acessorio": combos.material_id.map(info.deal_group.str.contains("ACCESSORY", na=False)).to_numpy().astype(float),
        "geracao": combos.material_id.map(info.product_group.map(_geracao)).to_numpy(),
        "woi_por_t": df.pivot_table(index=["key_account_code", "material_id"], columns="t", values="woi"),
        "avg_por_t": df.pivot_table(index=["key_account_code", "material_id"], columns="t", values="avg_activations"),
    }


def perfil_sazonal(p: dict, origem: int) -> dict[int, float] | None:
    """Índice sazonal de cada mês do ano: o total pedido (todas as KAs e produtos) no mês,
    dividido pela média mensal dos 12 meses até a origem. Agregado, o perfil não sofre com o
    ciclo de vida de cada produto; com menos de 9 meses de histórico, não há perfil."""
    totais = np.nansum(p["pedidos"], axis=0)
    o = origem - p["t0"]
    janela = range(max(o - 11, 0), o + 1)
    if len(janela) < 9:
        return None
    media = totais[list(janela)].mean()
    return {(p["t0"] + i) % 12 + 1: totais[i] / media for i in janela} if media > 0 else None


def fator_sazonal(perfil: dict[int, float] | None, origem: int, t: int) -> float:
    """Quanto o mês alvo t tende a ser maior que o nível dos 3 últimos meses até a origem."""
    if not perfil or t % 12 + 1 not in perfil:
        return 1.0
    nivel = np.mean([perfil.get(m % 12 + 1, 1.0) for m in range(origem - 2, origem + 1)])
    return float(perfil[t % 12 + 1] / nivel) if nivel > 0 else 1.0


def prever_demanda(modelo, am: pd.DataFrame) -> np.ndarray:
    """Previsão reconciliada de cima para baixo: o total de cada KA no mês alvo é o nível dos
    3 últimos meses x o fator sazonal (estável no agregado), e o modelo só reparte esse total
    entre os produtos (onde ele é bom). Linha a linha, o modelo sozinho projeta o declínio de
    cada produto com a idade e não vê o pico sazonal com poucos anos de histórico; a
    reconciliação corrige as duas coisas (validação em treinar.py)."""
    bruto = np.clip(modelo.predict(am[FEATURES_DEMANDA]), 0, None)
    alvo_ka = am.ka_media3.to_numpy() * am.sazonal.to_numpy()
    soma = pd.Series(bruto).groupby([am.origem.to_numpy(), am.ka.to_numpy(), am.t.to_numpy()]).transform("sum").to_numpy()
    ok = np.isfinite(alvo_ka) & (soma > 0)
    return np.where(ok, bruto * np.where(ok, alvo_ka, 0) / np.where(ok, soma, 1), bruto)


def amostras_demanda(p: dict, origem: int, horizontes=(1, 2, 3)) -> pd.DataFrame:
    """Uma linha por combinação e horizonte, vista do mês de origem (índice absoluto t)."""
    m, t0 = p["pedidos"], p["t0"]
    o = origem - t0
    col = lambda t: m[:, t - t0] if t0 <= t <= p["t1"] else np.full(m.shape[0], np.nan)
    ka_tot = np.array([np.nansum(m[p["ka"] == k], axis=0) for k in range(len(KAS))])  # KA x mês
    linhas = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        base = {
            "lag0": col(origem), "lag1": col(origem - 1), "lag2": col(origem - 2),
            "media3": np.nanmean(m[:, max(o - 2, 0):o + 1], axis=1),
            "media6": np.nanmean(m[:, max(o - 5, 0):o + 1], axis=1),
            "media12": np.nanmean(m[:, max(o - 11, 0):o + 1], axis=1),
            "zeros6": np.sum(m[:, max(o - 5, 0):o + 1] == 0, axis=1),
            "cv6": np.nanstd(m[:, max(o - 5, 0):o + 1], axis=1)
            / np.maximum(np.nanmean(m[:, max(o - 5, 0):o + 1], axis=1), 1),
        }
        k3 = np.nanmean(ka_tot[:, max(o - 2, 0):o + 1], axis=1)
        k12 = np.nanmean(ka_tot[:, max(o - 11, 0):o + 1], axis=1)
    chaves = list(zip(p["combos"].key_account_code, p["combos"].material_id))
    perfil = perfil_sazonal(p, origem)
    woi = p["woi_por_t"].reindex(chaves).get(origem, pd.Series(np.nan, index=range(len(chaves)))).to_numpy()
    avg = p["avg_por_t"].reindex(chaves).get(origem, pd.Series(np.nan, index=range(len(chaves)))).to_numpy()
    for h in horizontes:
        t = origem + h
        ano = t - 12
        ano_o = origem - 12
        kas = np.full(len(KAS), np.nan)
        if ano_o - t0 >= 0 and ano - t0 >= 0:
            with np.errstate(all="ignore"):
                kas = ka_tot[:, ano - t0] / np.where(ka_tot[:, ano_o - t0] > 0, ka_tot[:, ano_o - t0], np.nan)
        d = pd.DataFrame({
            "combo": np.arange(m.shape[0]), "origem": origem, "t": t, "h": h,
            "mes_alvo": t % 12 + 1, "ka": p["ka"], "acessorio": p["acessorio"], "geracao": p["geracao"],
            "idade_alvo": t - p["primeiro"], "desde_inicio": (p["primeiro"] == p["inicio_janela"]).astype(float),
            **base,
            "ano_passado": col(ano) if ano <= origem else np.nan,
            "ka_media3": k3[p["ka"]], "ka_tendencia": k3[p["ka"]] / np.where(k12[p["ka"]] > 0, k12[p["ka"]], np.nan),
            "ka_sazonal": kas[p["ka"]], "woi": woi, "avg_act": avg,
            "sazonal": fator_sazonal(perfil, origem, t),
            "y": col(t),
        })
        linhas.append(d[d.idade_alvo >= 0])  # o produto já existe no mês alvo
    return pd.concat(linhas, ignore_index=True)


# ── risco ─────────────────────────────────────────────────────────────────────

def features_risco(df: pd.DataFrame, demanda: str = "req_qty") -> pd.DataFrame:
    """Uma linha por KA x material x mês com demanda > 0. `demanda` é a coluna usada como
    pedido da linha: o Request Final real no treino, a demanda prevista na previsão."""
    d = df[df[demanda] > 0].copy()
    d["score"] = score_estimado(d.woi, d.booked_quarter, d.entered_quarter, d.key_account_code)
    d["ka"] = d.key_account_code.map({k: i for i, k in enumerate(KAS)})
    grupo = ["material_id", "t"]
    cap = d.groupby(grupo).capacidade.transform("max").replace(0, np.nan)
    total = d.groupby(grupo)[demanda].transform("sum")
    # demanda à frente na fila: as linhas do mesmo material e mês com Score menor (atendidas antes)
    a_frente = np.zeros(len(d))
    ate_meu = np.zeros(len(d))
    for _, g in d.groupby(grupo):
        s, q = g.score.to_numpy(), g[demanda].to_numpy()
        a_frente[d.index.get_indexer(g.index)] = [q[s < x].sum() for x in s]
        ate_meu[d.index.get_indexer(g.index)] = [q[s <= x].sum() for x in s]
    d["pressao"] = (total / cap).fillna(10).clip(upper=10)
    d["pressao_a_frente"] = (a_frente / cap).fillna(10).clip(upper=10)
    d["pressao_ate_meu_score"] = (ate_meu / cap).fillna(10).clip(upper=10)
    d["participacao"] = d[demanda] / total
    d["tem_booked"] = (d.booked_quarter > 0).astype(float)
    d["tem_entered"] = (d.entered_quarter > 0).astype(float)
    d["log_req"] = np.log1p(d[demanda])
    d["log_cap"] = np.log1p(d.capacidade)
    return d


# ── a cadeia: demanda prevista -> pressão -> risco ────────────────────────────

def logit(q) -> np.ndarray:
    q = np.clip(np.asarray(q, float), 1e-4, 1 - 1e-4)
    return np.log(q / (1 - q))


def calibrar(prob, calibracao: dict | None) -> np.ndarray:
    """Calibração sigmoide (Platt) ajustada em treinar.py: {"a": ..., "b": ...}."""
    if not calibracao:
        return np.asarray(prob, float)
    return 1 / (1 + np.exp(-(calibracao["a"] * logit(prob) + calibracao["b"])))


def prever(df: pd.DataFrame, p: dict, modelo_demanda, modelo_risco, origem: int, alvo: pd.DataFrame,
           calibracao: dict | None = None) -> pd.DataFrame:
    """Prevê, para as linhas de `alvo` (KA x material x mês futuro, com capacidade, WOI,
    Booked e Entered já conhecidos), a demanda e a chance de não atendimento.

    A demanda de cada linha é o pedido previsto; no 1º mês após a origem soma-se o que
    faltou atender no mês de origem (vira rollover). Rollback e deals do quarter futuro
    ainda não existem e ficam de fora."""
    am = amostras_demanda(p, origem, horizontes=sorted(set(int(x) for x in alvo.t - origem)))
    am["demanda_prevista"] = prever_demanda(modelo_demanda, am)
    am["key_account_code"] = p["combos"].key_account_code.to_numpy()[am.combo]
    am["material_id"] = p["combos"].material_id.to_numpy()[am.combo]
    d = alvo.merge(am[["key_account_code", "material_id", "t", "h", "cv6", "demanda_prevista"]],
                   on=["key_account_code", "material_id", "t"], how="left")
    falta = df[df.t == origem].assign(rollover_estimado=(df.req_qty - df.alocado).clip(lower=0))
    d = d.merge(falta[["key_account_code", "material_id", "rollover_estimado"]],
                on=["key_account_code", "material_id"], how="left")
    d["rollover_estimado"] = np.where(d.t == origem + 1, d.rollover_estimado.fillna(0), 0)
    d["demanda_prevista"] = (np.round((d.demanda_prevista.fillna(0) + d.rollover_estimado) / 10) * 10).astype(int)
    r = features_risco(d, "demanda_prevista")
    prob = modelo_risco.predict_proba(r[FEATURES_RISCO])[:, 1]  # classe 1 = não atendido
    r["prob_nao_atendimento"] = calibrar(prob, calibracao)
    return r


def assinatura(conn) -> dict:
    """Identifica a versão dos dados: muda quando a base é regenerada ou atualizada."""
    r = pd.read_sql(  # pandas: vale para sqlite3 (treino) e SQLAlchemy (o app)
        "SELECT COUNT(*) AS n, MAX(CASE WHEN month_status = 'ongoing' THEN year_month END) AS ongoing, "
        "SUM(initial_req_qty) AS soma FROM ka_deal_allocation", conn,
    ).iloc[0]
    return {"linhas": int(r.n), "ongoing": r.ongoing, "soma_pedidos": int(r.soma or 0)}
