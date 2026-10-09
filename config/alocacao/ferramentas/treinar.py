"""
Treina os modelos de demanda e de risco do pack de alocação e grava o artefato que a tool
do agente carrega.

Uso, na pasta config/alocacao:
    python -m ferramentas.treinar                 # usa ../../allocation.db
    python -m ferramentas.treinar --db OUTRO.db

Demanda: o modelo reparte entre os produtos o total de cada KA, que vem do nível dos 3
últimos meses x o perfil sazonal (reconciliação de cima para baixo, features.prever_demanda).
Validada fora do tempo (os últimos 6 meses conhecidos ficam de fora do treino) contra
referências simples (média dos 3 últimos meses; mesmo mês do ano anterior), e num backtest
do pico de fim de ano: a previsão feita em setembro do ano anterior para outubro a dezembro.

Risco: treinado com o que existe na hora da previsão. Para cada quarter do histórico, um
modelo de demanda treinado só com os meses ANTERIORES prevê os pedidos dos 3 meses do
quarter; com eles se calcula a pressão prevista sobre a capacidade, e o rótulo é o que de
fato aconteceu (a linha foi atendida ou não). Assim a probabilidade já embute o erro da
previsão de demanda. Uma calibração sigmoide (Platt), ajustada em previsões fora da amostra
(um quarter de fora por vez), corrige as probabilidades. A validação usa os 2 últimos quarters
conhecidos, com classificador e calibração treinados só nos anteriores.

Não há modelo de unidades que vão faltar: testado (fração do pedido, unidades / demanda
prevista, regra da fila e simulação de cenários), errou de -77% a +702% por quarter, porque
a falta amplifica o erro da demanda prevista (15% de erro na demanda vira 50% ou mais na
falta). O risco ordena bem (AUC ~0,92); a quantidade que falta, não.

No fim, os dois modelos são treinados de novo com tudo e gravados em
modelos/alocacao.joblib, com as métricas (também em modelos/metricas.json).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

from . import features as F

RAIZ = Path(__file__).resolve().parents[3]
PASTA = Path(__file__).parent / "modelos"
ARTEFATO = PASTA / "alocacao.joblib"
MESES_TESTE = 6
QUARTERS_TESTE = 2
INICIO_QUARTER = (1, 4, 7, 10)  # meses em que um quarter fiscal começa (ano fiscal em abril)


def _modelo_demanda() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss="poisson", max_iter=400, learning_rate=0.05, min_samples_leaf=30,
        categorical_features=F.CATEGORICAS_DEMANDA, early_stopping=False, random_state=0,
    )


def _ajustar_demanda(am: pd.DataFrame) -> HistGradientBoostingRegressor:
    """Prever com features.prever_demanda, que reconcilia com o total de cada KA."""
    return _modelo_demanda().fit(_x(am, F.FEATURES_DEMANDA), am.y)


def _modelo_risco() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.05, min_samples_leaf=40, l2_regularization=1.0,
        categorical_features=F.CATEGORICAS_RISCO, early_stopping=False, random_state=0,
    )


def _x(df: pd.DataFrame, colunas: list[str]) -> pd.DataFrame:
    """Variáveis para o fit. Uma coluna inteira sem valor (ex.: 'mesmo mês do ano anterior' nas
    primeiras origens) quebra o HistGradientBoosting do scikit-learn 1.9; vira uma constante,
    que o modelo simplesmente não usa."""
    x = df[colunas].copy()
    for c in x.columns[x.isna().all()]:
        x[c] = -1.0
    return x


def _wape(y, p) -> float:
    y, p = np.asarray(y, float), np.asarray(p, float)
    return float(np.abs(y - p).sum() / max(y.sum(), 1e-9))


def _mes(t: int) -> str:
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


# ── demanda ───────────────────────────────────────────────────────────────────

def dataset_demanda(p: dict) -> pd.DataFrame:
    am = pd.concat([F.amostras_demanda(p, o) for o in range(p["t0"] + 3, p["t1"])], ignore_index=True)
    am = am[am.y.notna() & (am.t <= p["t1"])].reset_index(drop=True)
    am["material_id"] = p["combos"].material_id.to_numpy()[am.combo]
    return am


def avaliar_demanda(am: pd.DataFrame, corte: int) -> dict:
    tr, te = am[am.t < corte], am[am.t >= corte].copy()
    te["modelo"] = F.prever_demanda(_ajustar_demanda(tr), te)
    te["media3_ref"] = te.media3.fillna(0)
    te["ano_passado_ref"] = te.ano_passado.fillna(te.media3).fillna(0)
    out = {"meses_teste": [_mes(corte), _mes(int(am.t.max()))], "linhas_teste": int(len(te))}
    for nome, col in (("modelo", "modelo"), ("media_3_meses", "media3_ref"), ("mesmo_mes_ano_passado", "ano_passado_ref")):
        mat = te.groupby(["material_id", "origem", "t"])[["y", col]].sum()
        ka_mes = te.groupby(["ka", "origem", "t"])[["y", col]].sum()
        out[nome] = {
            "wape_linha": round(_wape(te.y, te[col]), 3),
            "wape_material_mes": round(_wape(mat.y, mat[col]), 3),
            "wape_ka_mes": round(_wape(ka_mes.y, ka_mes[col]), 3),
            "wape_por_horizonte": {int(h): round(_wape(g.y, g[col]), 3) for h, g in te.groupby("h")},
        }
    out["vies_por_ka"] = {F.KAS[int(k)]: round(float(g.modelo.sum() / g.y.sum()), 3) for k, g in te.groupby("ka")}
    return out


def backtest_fim_de_ano(am: pd.DataFrame, p: dict) -> dict:
    """A previsão feita em setembro do ano anterior para outubro a dezembro (o pico sazonal
    que a tool precisa enxergar no próximo quarter), por mês: total previsto x real."""
    setembros = [o for o in range(p["t0"], p["t1"]) if o % 12 == 8 and o + 3 <= p["t1"] and o - p["t0"] >= 8]
    if not setembros:
        return {}
    o = setembros[-1]
    te = am[am.origem == o].copy()
    te["modelo"] = F.prever_demanda(_ajustar_demanda(am[am.t <= o]), te)
    tot = te.groupby("t")[["y", "modelo"]].sum()
    return {"origem": _mes(o), **{_mes(int(t)): {"real": int(r.y), "previsto": int(r.modelo),
                                                   "erro": round(float(r.modelo / r.y - 1), 3)}
                                   for t, r in tot.iterrows()}}


# ── risco ─────────────────────────────────────────────────────────────────────

def origens_de_quarter(p: dict) -> list[int]:
    """Meses de origem das previsões: o último mês antes de cada quarter, com ao menos 6
    meses de histórico e os 3 meses do quarter já conhecidos."""
    return [o for o in range(p["t0"] + 5, p["t1"] - 2) if (o + 1) % 12 + 1 in INICIO_QUARTER]


def amostras_risco(df: pd.DataFrame, p: dict, am: pd.DataFrame, origem: int) -> pd.DataFrame:
    """Previsão feita em `origem` para os 3 meses seguintes, com o desfecho real."""
    md = _ajustar_demanda(am[am.t <= origem])
    alvo = df[(df.t > origem) & (df.t <= origem + 3) & df.month_status.isin(F.CONHECIDOS)]
    real = alvo[["key_account_code", "material_id", "t", "req_qty", "alocado"]].rename(
        columns={"req_qty": "req_real", "alocado": "alocado_real"})
    r = F.prever(df, p, md, _Constante(), origem, alvo.drop(columns=["req_qty", "alocado"]))
    r = r.merge(real, on=["key_account_code", "material_id", "t"])
    r = r[r.req_real > 0].copy()
    r["nao_atendido"] = (r.alocado_real < r.req_real).astype(int)
    r["origem"] = origem
    return r


class _Constante:
    """Classificador provisório: `prever` exige um, mas aqui só as variáveis interessam."""

    def predict_proba(self, x):
        return np.full((len(x), 2), .5)


def _calibracao(amostras: pd.DataFrame) -> dict:
    """Sigmoide (Platt) sobre previsões fora da amostra: cada quarter é previsto por um
    classificador treinado nos demais. Com poucos quarters, a isotônica fazia degraus grossos
    (testado: Brier 0,1035 contra 0,0996 da sigmoide)."""
    oof = np.zeros(len(amostras))
    for o in amostras.origem.unique():
        fora = (amostras.origem == o).to_numpy()
        m = _modelo_risco().fit(_x(amostras[~fora], F.FEATURES_RISCO), amostras[~fora].nao_atendido)
        oof[fora] = m.predict_proba(amostras[fora][F.FEATURES_RISCO])[:, 1]
    lr = LogisticRegression().fit(F.logit(oof)[:, None], amostras.nao_atendido)
    return {"a": float(lr.coef_[0, 0]), "b": float(lr.intercept_[0])}


def avaliar_risco(amostras: pd.DataFrame) -> dict:
    origens = sorted(amostras.origem.unique())
    teste = origens[-QUARTERS_TESTE:]
    tr, te = amostras[~amostras.origem.isin(teste)], amostras[amostras.origem.isin(teste)]
    m = _modelo_risco().fit(_x(tr, F.FEATURES_RISCO), tr.nao_atendido)
    bruto = m.predict_proba(te[F.FEATURES_RISCO])[:, 1]
    prob = F.calibrar(bruto, _calibracao(tr))
    regra = (te.pressao_ate_meu_score > 1).astype(int)
    faixas = pd.cut(prob, [0, .2, .4, .6, .8, 1], include_lowest=True)
    calibracao = te.assign(f=faixas, p=prob).groupby("f", observed=True).agg(
        linhas=("p", "size"), prob_media=("p", "mean"), taxa_real=("nao_atendido", "mean")).round(3)
    return {
        "quarters_treino": [_mes(o + 1) for o in origens if o not in teste],
        "quarters_teste": [_mes(o + 1) for o in teste],
        "linhas_teste": int(len(te)),
        "taxa_nao_atendimento_teste": round(float(te.nao_atendido.mean()), 3),
        "modelo": {"auc": round(roc_auc_score(te.nao_atendido, prob), 3),
                   "brier": round(brier_score_loss(te.nao_atendido, prob), 3),
                   "acuracia": round(accuracy_score(te.nao_atendido, prob >= .5), 3)},
        "modelo_sem_calibracao": {"auc": round(roc_auc_score(te.nao_atendido, bruto), 3),
                                  "brier": round(brier_score_loss(te.nao_atendido, bruto), 3)},
        "regra_pressao_prevista_ate_meu_score_>1": {"acuracia": round(accuracy_score(te.nao_atendido, regra), 3)},
        "calibracao": {str(k): v for k, v in calibracao.to_dict("index").items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=RAIZ / "allocation.db")
    args = ap.parse_args()
    with sqlite3.connect(args.db) as conn:
        df = F.ler_base(conn)
        assinatura = F.assinatura(conn)
    p = F.painel(df)
    corte = p["t1"] - MESES_TESTE + 1
    am = dataset_demanda(p)
    print(f"Base: {len(df)} linhas de KAs; meses conhecidos {_mes(p['t0'])} a {_mes(p['t1'])}")

    amostras = pd.concat([amostras_risco(df, p, am, o) for o in origens_de_quarter(p)], ignore_index=True)
    metricas = {"demanda": avaliar_demanda(am, corte), "demanda_pico_fim_de_ano": backtest_fim_de_ano(am, p),
                "risco": avaliar_risco(amostras)}
    print(json.dumps(metricas, indent=2, ensure_ascii=False))

    md = _ajustar_demanda(am)
    mr = _modelo_risco().fit(_x(amostras, F.FEATURES_RISCO), amostras.nao_atendido)
    calibracao = _calibracao(amostras)
    PASTA.mkdir(exist_ok=True)
    info = {
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "sklearn": sklearn.__version__,
        "assinatura_dados": assinatura,
        "meses_conhecidos": [_mes(p["t0"]), _mes(p["t1"])],
        "amostras_risco": int(len(amostras)),
        "metricas": metricas,
    }
    info["calibracao"] = calibracao
    joblib.dump({"demanda": md, "risco": mr, **info}, ARTEFATO)
    (PASTA / "metricas.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Modelos gravados em {ARTEFATO}")


if __name__ == "__main__":
    main()
