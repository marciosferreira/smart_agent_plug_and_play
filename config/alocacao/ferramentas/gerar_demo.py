"""
Gera a base DEMO de alocação (allocation.db) com histórico, a partir da base de teste
original. Os dados são SINTÉTICOS: servem para demonstrar o agente e treinar os modelos
do pack, não para tirar conclusões de negócio.

Uso, na pasta config/alocacao:
    python -m ferramentas.gerar_demo                 # usa ../../allocation.db
    python -m ferramentas.gerar_demo --db OUTRO.db

Na primeira execução a base original é copiada para allocation_original.db (ao lado), e
é dela que sai a semente: os mesmos 229 materiais, 5 KAs, as 736 combinações KA x material
e as ordens de grandeza dos pedidos do FQ1 real. Com a mesma semente, o resultado é sempre
o mesmo.

Linha do tempo (ano fiscal começa em abril; `fiscal_year` = ano em que ele começa):
    jul/2024 a jun/2026   8 quarters fechados          month_status = 'closed'
    jul a set/2026        FQ2 FY2026, o quarter atual   'done', 'done', 'ongoing' (semana 3)
    out a dez/2026        FQ3 FY2026, o próximo         'next' (só capacidade, Booked/Entered e WOI)

Como os números nascem:
  - Sell-out (ativações) semanal de cada KA x material = nível base x ciclo de vida do
    modelo (lançamento, pico, declínio) x sazonalidade x tendência da KA x ruído.
  - O pedido (Initial Request) é a previsão da KA para o mês, com ruído, mais um ajuste
    de estoque diluído nos 3 meses do quarter (busca WOI de 12 semanas). Arredondado em 10.
  - A capacidade (One Plan) é planejada no início do quarter pela média dos 3 meses
    anteriores, SEM sazonalidade, com ruído; produto novo é planejado em 65% da demanda.
  - A alocação sai do simulador (simulador.py): Request por Score, Rollback, Rollover,
    Deal e Retail com o resíduo. O estoque de cada KA evolui com o que é entregue e o que
    é ativado; WOI = (sell_in_lifetime - activations_lifetime) / avg_activations.

Padrões plantados (o que um modelo bem feito deve encontrar):
  - Sazonalidade: novembro x1,4 e dezembro x1,3 (Black Friday e Natal), maio x1,15,
    janeiro e fevereiro x0,85.
  - Tendência por KA, ao mês: TIM_BRA +2,5%, MMICOM_BR +1%, VIVO_BRA -1%, CLARO_BR estável.
  - Choque de capacidade no site JAG: fev e mar/2026 a 60%, abr/2026 a 70%.
  - Lançamentos com oferta restrita (65% da demanda planejada) nos primeiros meses.
  - Como o One Plan não acompanha a sazonalidade, os picos (nov, dez, maio) faltam
    produto; isso vale também para o FQ3 (out a dez/2026), o quarter 'next'.
  - Acessórios não têm ativações rastreadas: avg_activations = 0 e WOI = 0 (a limitação
    de dado descrita no dominio.md).
"""
from __future__ import annotations

import argparse
import calendar
import re
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from .simulador import alocar_quarter, dividir_semanas

RAIZ = Path(__file__).resolve().parents[3]
SEMENTE = 20260930
INICIO = (2024, 7)
AGORA = (2026, 9)
SEMANA_ATUAL = 3
WOI_ALVO = 12.0
CARRY = 0.8

SAZONAL = {1: .85, 2: .85, 3: .95, 4: 1.0, 5: 1.15, 6: 1.0, 7: .95, 8: 1.0, 9: 1.0, 10: 1.05, 11: 1.4, 12: 1.3}
TENDENCIA = {"CLARO_BR": 1.0, "MMICOM_BR": 1.01, "TIM_BRA": 1.025, "VIVO_BRA": 0.99, "RETAIL_BRA": 1.0}
CHOQUE_JAG = {(2026, 2): .6, (2026, 3): .6, (2026, 4): .7}
KAS_COM_DEAL = ("CLARO_BR", "TIM_BRA", "VIVO_BRA")
PERFIL_SEMANAS = {4: [.22, .26, .26, .26], 5: [.16, .21, .23, .23, .17]}


# ── calendário ────────────────────────────────────────────────────────────────

def _abs(y: int, m: int) -> int:
    return y * 12 + m - 1


def _ym(i: int) -> tuple[int, int]:
    return i // 12, i % 12 + 1


def _fiscal(y: int, m: int) -> tuple[str, int]:
    return f"FQ{(m - 4) % 12 // 3 + 1}", (y if m >= 4 else y - 1)


def _semanas(y: int, m: int) -> int:
    return 5 if calendar.monthrange(y, m)[1] == 31 else 4


# ── semente: a base original ──────────────────────────────────────────────────

def _ler_semente(db: Path) -> dict:
    with sqlite3.connect(db) as c:
        inp = pd.read_sql("SELECT * FROM ka_input_data", c)
        out = pd.read_sql("SELECT * FROM ka_deal_allocation", c)
    mats = (inp[["material_id", "family", "product_group", "deal_group", "product", "sales_model", "origin"]]
            .drop_duplicates("material_id").sort_values("material_id").reset_index(drop=True))
    combos = (inp[["key_account_id", "key_account_code", "material_id"]].drop_duplicates()
              .sort_values(["key_account_id", "material_id"]).reset_index(drop=True))
    atual = out[out.month_status.isin(["done", "ongoing"]) & (out.req_qty > 0)]
    nivel = atual.groupby(["key_account_code", "material_id"]).req_qty.mean()
    return {"mats": mats, "combos": combos, "nivel": nivel}


def _modelo(product_group: str) -> tuple[str, int | None]:
    """'LAGOS25 4GB-128GB' -> ('LAGOS25', 2025). Acessórios seguem o ano do modelo."""
    token = (product_group or "").split(" ")[0]
    m = re.search(r"(\d{2})$", token)
    return token, (2000 + int(m.group(1)) if m else None)


def _ciclo(idade: int, pico: int) -> float:
    """Ciclo de vida: rampa nos 2 primeiros meses, pico e declínio lento."""
    if idade < 0:
        return 0.0
    if idade == 0:
        return .35
    if idade == 1:
        return .7
    if idade <= pico:
        return 1.0
    return max(.15, float(np.exp(-(idade - pico) / 12)))


# ── geração ───────────────────────────────────────────────────────────────────

def gerar(db_original: Path, rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    s = _ler_semente(db_original)
    mats, combos = s["mats"], s["combos"]
    n_mat, n = len(mats), len(combos)
    mat_idx = {m: i for i, m in enumerate(mats.material_id)}
    combo_mat = combos.material_id.map(mat_idx).to_numpy()
    ka = combos.key_account_code.to_numpy()
    eh_retail = np.array(["RETAIL" in k for k in ka])
    eh_mmicom = np.array(["MMICOM" in k for k in ka])
    acessorio = mats.deal_group.str.contains("ACCESSORY", na=False).to_numpy()
    rastreado = ~acessorio[combo_mat]

    # lançamento por modelo (variantes e acessórios do mesmo modelo lançam juntos)
    lanc_modelo: dict[str, int] = {}
    pico_modelo: dict[str, int] = {}
    for pg in mats.product_group:
        token, ano = _modelo(pg)
        if token in lanc_modelo:
            continue
        if ano == 2024:
            lanc_modelo[token] = _abs(2024, int(rng.integers(1, 7)))
        elif ano == 2025:
            lanc_modelo[token] = _abs(2025, int(rng.integers(2, 12)))
        elif ano == 2026:
            lanc_modelo[token] = _abs(2026, int(rng.integers(1, 7)))
        else:
            lanc_modelo[token] = _abs(2023, 6)
        pico_modelo[token] = int(rng.integers(10, 17))
    lanc = np.array([lanc_modelo[_modelo(pg)[0]] for pg in mats.product_group])
    pico = np.array([pico_modelo[_modelo(pg)[0]] for pg in mats.product_group])

    meses = list(range(_abs(*INICIO), _abs(*AGORA) + 4))
    agora = _abs(*AGORA)
    ref = _abs(2026, 5)  # mês de referência: os níveis do FQ1 real valem aqui

    # nível base semanal de cada combinação, calibrado no FQ1 real
    medianas = s["nivel"].groupby(level=0).median()
    base = np.zeros(n)
    for i in range(n):
        if eh_retail[i]:
            continue
        chave = (ka[i], combos.material_id[i])
        if chave in s["nivel"].index:
            mensal = float(s["nivel"][chave])
        elif rng.random() < .4:
            mensal = float(medianas.get(ka[i], 300)) * .4 * float(rng.lognormal(0, .6))
        else:
            continue
        mi = combo_mat[i]
        r = max(ref, lanc[mi] + 2)
        base[i] = mensal / 4.33 / _ciclo(r - lanc[mi], pico[mi]) / TENDENCIA[ka[i]] ** (r - ref)
    base[eh_retail] = float(medianas.median()) / 4.33 * .5  # Retail sem KA no material

    def ciclo(i_mes: int) -> np.ndarray:
        idade = i_mes - lanc[combo_mat]
        return np.array([_ciclo(int(a), int(p)) for a, p in zip(idade, pico[combo_mat])])

    # O volume TOTAL de cada KA segue a tendência dela; o ciclo de vida só redistribui esse
    # volume entre os produtos (lançamentos ganham participação, modelos velhos perdem). O
    # fator é limitado a 2,5 para os poucos produtos do início da janela não inflarem demais.
    kas_req = [k for k in dict.fromkeys(ka) if "RETAIL" not in k]
    ref_ka = {k: float((base * ciclo(ref))[ka == k].sum()) for k in kas_req}

    def esperado_semana(i_mes: int) -> np.ndarray:
        """Ativações esperadas por semana de 7 dias, por combinação."""
        _, m = _ym(i_mes)
        bruto = base * ciclo(i_mes)
        e = np.zeros(n)
        for k in kas_req:
            sel = ka == k
            soma = bruto[sel].sum()
            if soma > 0:
                e[sel] = bruto[sel] * min(2.5, ref_ka[k] / soma) * TENDENCIA[k] ** (i_mes - ref)
        for mi in range(n_mat):  # Retail: 60% do sell-out das KAs do material
            r = (combo_mat == mi) & eh_retail
            soma = e[(combo_mat == mi) & ~eh_retail].sum()
            e[r] = .6 * soma if soma > 0 else bruto[r]
        return e * SAZONAL[m]

    def esperado_mes(i_mes: int) -> np.ndarray:
        """Demanda esperada do mês: proporcional aos dias, não ao número de semanas."""
        y, m = _ym(i_mes)
        return esperado_semana(i_mes) * calendar.monthrange(y, m)[1] / 7

    # estado do estoque de cada combinação no início da janela
    e0 = esperado_semana(meses[0])
    inv = np.round(e0 * WOI_ALVO).astype(np.int64)
    act_lt = np.round(e0 * 4.33 * np.clip(meses[0] - lanc[combo_mat], 1, None)).astype(np.int64)
    act_lt[~rastreado] = 0
    sell_lt = act_lt + inv
    hist = [list(np.repeat(v, 8)) for v in e0]

    # histórico mensal de pedidos e da demanda esperada do Retail, para o planejador
    pedidos_mes: dict[int, np.ndarray] = {}
    retail_mes: dict[int, np.ndarray] = {}
    rollover_entrada = np.zeros(n, dtype=np.int64)
    linhas_in, linhas_out = [], []

    for q0 in range(0, len(meses), 3):
        q_meses = meses[q0:q0 + 3]
        y0, m0 = _ym(q_meses[0])
        quarter, fy = _fiscal(y0, m0)
        if q_meses[0] > agora:
            status = ["next"] * 3
        elif q_meses[-1] < agora:
            status = ["closed"] * 3
        else:
            status = ["done" if mm < agora else "ongoing" for mm in q_meses]
        atual = status[0] != "closed" and status[0] != "next"

        # métricas do quarter (constantes nos 3 meses, como no dominio.md)
        avg_act = np.array([int(round(np.mean(h[-8:]))) for h in hist])
        avg_act[~rastreado] = 0
        woi = np.where(avg_act > 0, (sell_lt - act_lt) / np.maximum(avg_act, 1), 0.0).round(1)
        sell_q, act_q = sell_lt.copy(), act_lt.copy()

        # pedidos dos 3 meses (previsão da KA com ruído e um ajuste de estoque, diluído nos 3 meses)
        pedidos = np.zeros((n, 3), dtype=np.int64)
        esperado = np.stack([esperado_mes(mm) for mm in q_meses], axis=1)
        if status[0] != "next":
            prev = esperado * rng.lognormal(0, .15, size=(n, 3))
            ajuste = np.where(avg_act > 0, (WOI_ALVO - woi) * avg_act * .5 / 3, 0)[:, None]
            prev += np.clip(ajuste, -.4 * prev, .6 * prev)
            pedidos = (np.round(np.clip(prev, 0, None) / 10) * 10).astype(np.int64)
            pedidos[eh_retail] = 0
        retail_esp = np.where(eh_retail[:, None], esperado, 0)

        # Booked / Entered do quarter
        p_book = np.where(eh_mmicom, .3, np.where(eh_retail, .05, .15)) * (.5 if status[0] == "next" else 1)
        vol_q = esperado.sum(1)
        booked = np.where(rng.random(n) < p_book, np.round(vol_q * rng.uniform(.2, .6, n) / 10) * 10, 0).astype(np.int64)
        entered = np.where(rng.random(n) < .1 * (.5 if status[0] == "next" else 1),
                           np.round(vol_q * rng.uniform(.1, .4, n) / 10) * 10, 0).astype(np.int64)

        # Score estimado (tabela do dominio.md)
        score = np.full(n, 7)
        score[(woi >= 15) & (entered > 0)] = 6
        score[(woi >= 15) & (booked > 0)] = 5
        meio = (woi >= 10) & (woi < 15)
        score[meio] = 4
        score[meio & (entered > 0)] = 3
        score[meio & (booked > 0)] = 2
        score[woi < 10] = 1
        score[eh_mmicom] = 0

        # One Plan: média dos 3 meses anteriores, sem sazonalidade, com ruído e choques
        cap = np.zeros((n_mat, 3), dtype=np.int64)
        for j, mm in enumerate(q_meses):
            y, m = _ym(mm)
            ant = [k for k in range(q_meses[0] - 3, q_meses[0]) if k in pedidos_mes]
            for mi in range(n_mat):
                sel = combo_mat == mi
                if mm < lanc[mi]:
                    continue
                if q_meses[0] - lanc[mi] >= 3 and len(ant) == 3:
                    ka_trail = np.mean([pedidos_mes[k][sel].sum() for k in ant])
                    rt_trail = np.mean([retail_mes[k][sel].sum() for k in ant])
                    plano = ka_trail + .5 * rt_trail
                elif q_meses[0] - lanc[mi] >= 3:  # início da janela, ainda sem histórico
                    plano = (esperado[sel & ~eh_retail, j].sum() + .5 * esperado[sel & eh_retail, j].sum()) / SAZONAL[m]
                else:  # lançamento: planejado em 65% da demanda esperada
                    plano = .65 * (esperado[sel & ~eh_retail, j].sum() + .5 * esperado[sel & eh_retail, j].sum())
                choque = CHOQUE_JAG.get((y, m), 1.0) if mats.origin[mi] == "JAG" else 1.0
                cap[mi, j] = int(round(plano * rng.lognormal(np.log(1.15), .2) * choque / 10) * 10)

        # Deals do quarter por (KA, deal_group)
        deals, valor_deal = [], np.zeros(n, dtype=np.int64)
        if status[0] != "next":
            grupos = pd.DataFrame({"ka": ka, "dg": mats.deal_group.to_numpy()[combo_mat], "i": np.arange(n)})
            for (k, dg), g in grupos[grupos.ka.isin(KAS_COM_DEAL)].groupby(["ka", "dg"]):
                idx = g.i.to_numpy()
                total = int(pedidos[idx].sum())
                if total > 0 and rng.random() < .35:
                    v = max(50, int(round(total * rng.uniform(.2, .5) / 10) * 10))
                    deals.append((idx, v))
                    valor_deal[idx] = v

        if status[0] == "next":  # diagnóstico: pressão esperada do próximo quarter
            for j, mm in enumerate(q_meses):
                dem = pd.Series(esperado[~eh_retail, j]).groupby(combo_mat[~eh_retail]).sum()
                p = (dem / pd.Series(cap[:, j]).replace(0, np.nan)).dropna()
                p = p[dem[p.index] > 0]
                print(f"  {_ym(mm)[0]}-{_ym(mm)[1]:02d} (next): pressão esperada mediana {p.median():.2f}, "
                      f"materiais com pressão > 1: {(p > 1).mean():.0%}")

        # plano de alocação do quarter
        if status[0] != "next":
            plano = alocar_quarter(cap, combo_mat, eh_retail, pedidos, score, deals, rollover_entrada, CARRY)
            rollover_entrada = plano["rollover_saida"]
        else:
            z = np.zeros((n, 3), dtype=np.int64)
            plano = {"req": z, "rollover": z, "rollback": z, "alocado": z}

        for j, mm in enumerate(q_meses):
            y, m = _ym(mm)
            n_sem = _semanas(y, m)
            pesos = np.array(PERFIL_SEMANAS[n_sem])
            cap_sem = np.zeros((n_mat, 5), dtype=np.int64)
            for mi in range(n_mat):
                w = pesos * rng.lognormal(0, .1, n_sem)
                exato = cap[mi, j] * w / w.sum()
                parte = np.floor(exato).astype(np.int64)
                for k in np.argsort(-(exato - parte))[: cap[mi, j] - int(parte.sum())]:
                    parte[k] += 1
                cap_sem[mi, :n_sem] = parte
            aloc_sem = dividir_semanas(plano["alocado"][:, j], combo_mat, eh_retail, cap_sem[:, :n_sem])
            aloc_sem = np.pad(aloc_sem, ((0, 0), (0, 5 - n_sem)))

            # projeção de WOI semana a semana com a alocação (a partir do estoque do início do mês)
            proj = np.zeros((n, 5))
            if status[j] != "next":
                acum = np.cumsum(aloc_sem, axis=1)
                semanas = np.arange(1, 6)
                proj = np.where(avg_act[:, None] > 0,
                                np.maximum(0, (inv[:, None] + acum - avg_act[:, None] * semanas) / np.maximum(avg_act, 1)[:, None]),
                                0.0).round(1)
                proj[:, n_sem:] = 0

            # sell-in = entrega da semana nos meses que já passaram
            if status[j] in ("closed", "done"):
                sell_sem = aloc_sem.copy()
            elif status[j] == "ongoing":
                sell_sem = aloc_sem.copy()
                sell_sem[:, SEMANA_ATUAL - 1:] = 0
            else:
                sell_sem = np.zeros_like(aloc_sem)

            # simula as semanas que já aconteceram: entrega, ativação (limitada ao estoque)
            semanas_reais = {"closed": n_sem, "done": n_sem, "ongoing": SEMANA_ATUAL - 1}.get(status[j], 0)
            if semanas_reais:
                esp = esperado_mes(mm) / n_sem  # o mês é dividido nas semanas da grade
                for w in range(semanas_reais):
                    inv += aloc_sem[:, w]
                    sell_lt += aloc_sem[:, w]
                    procura = np.round(esp * rng.lognormal(0, .25, n)).astype(np.int64)
                    act = np.minimum(procura, inv)
                    inv -= act
                    act_lt[rastreado] += act[rastreado]
                    for i in range(n):
                        hist[i].append(float(act[i]))

            if status[j] != "next":
                pedidos_mes[mm] = pedidos[:, j].copy()
                retail_mes[mm] = retail_esp[:, j].copy()

            grupo_supply = pd.Series(cap[:, j]).groupby(mats.deal_group.to_numpy()).transform("sum").to_numpy()
            ym = f"{y:04d}-{m:02d}-01"
            for i in np.where(mm >= lanc[combo_mat])[0]:
                mi = combo_mat[i]
                mat = mats.iloc[mi]
                comum = dict(
                    material_id=int(mat.material_id), key_account_id=int(combos.key_account_id[i]),
                    key_account_code=ka[i], deal_group=mat.deal_group, product=mat["product"], origin=mat.origin,
                    quarter=quarter, fiscal_year=fy, year_month=ym, month_status=status[j], month_seq=j + 1,
                    current_week=SEMANA_ATUAL, booked_quarter=int(booked[i]), entered_quarter=int(entered[i]),
                    req_qty=int(plano["req"][i, j]), deal=int(valor_deal[i]),
                    sell_in_lifetime=int(sell_q[i]), activations_lifetime=int(act_q[i]),
                    avg_activations=int(avg_act[i]), woi=float(woi[i]),
                    **{f"sell_in{w + 1}": int(sell_sem[i, w]) for w in range(5)},
                    **{f"one_plan_week{w + 1}": int(cap_sem[mi, w]) for w in range(5)},
                )
                linhas_in.append(dict(
                    comum, family=mat.family, product_group=mat.product_group, sales_model=mat.sales_model,
                    current_quarter=int(atual), current_month=f"{AGORA[0]:04d}-{AGORA[1]:02d}-01",
                    init_req_qty=int(pedidos[i, j]),
                    **{f"user_allocation{w + 1}": 0 for w in range(5)},
                ))
                linhas_out.append(dict(
                    comum, initial_req_qty=int(pedidos[i, j]),
                    rollover=int(plano["rollover"][i, j]), rollback=int(plano["rollback"][i, j]),
                    **{f"allocation_W{w + 1}": int(aloc_sem[i, w]) for w in range(5)},
                    **{f"allocation_alone_W{w + 1}": int(aloc_sem[i, w]) for w in range(5)},
                    **{f"sell_in_comp_w{w + 1}": 0 for w in range(5)},
                    allocation_allowed=1,
                    **{f"proj_woi_w{w + 1}": float(proj[i, w]) for w in range(5)},
                    deal_group_supply=int(grupo_supply[mi]),
                ))
    return pd.DataFrame(linhas_in), pd.DataFrame(linhas_out)


# ── gravação ──────────────────────────────────────────────────────────────────

COLUNAS_IN = [
    "material_id", "family", "product_group", "deal_group", "product", "sales_model", "origin",
    "key_account_id", "key_account_code", "current_quarter", "current_month", "current_week", "quarter",
    "year_month", "month_status", "entered_quarter", "booked_quarter",
    *[f"user_allocation{w}" for w in range(1, 6)], *[f"sell_in{w}" for w in range(1, 6)],
    *[f"one_plan_week{w}" for w in range(1, 6)], "req_qty", "init_req_qty", "deal", "sell_in_lifetime",
    "activations_lifetime", "avg_activations", "woi", "month_seq", "fiscal_year",
]
COLUNAS_OUT = [
    "quarter", "deal_group", "product", "origin", "key_account_code", "material_id", "key_account_id",
    "year_month", "month_status", "current_week", "booked_quarter", "entered_quarter", "initial_req_qty",
    "req_qty", "rollover", "rollback", "deal", *[f"sell_in{w}" for w in range(1, 6)],
    *[f"one_plan_week{w}" for w in range(1, 6)], *[f"allocation_W{w}" for w in range(1, 6)],
    *[f"allocation_alone_W{w}" for w in range(1, 6)], *[f"sell_in_comp_w{w}" for w in range(1, 6)],
    "sell_in_lifetime", "activations_lifetime", "avg_activations", "woi", "allocation_allowed",
    *[f"proj_woi_w{w}" for w in range(1, 6)], "deal_group_supply", "month_seq", "fiscal_year",
]
_TEXTO = {"family", "product_group", "deal_group", "product", "sales_model", "origin", "key_account_code",
          "current_month", "quarter", "year_month", "month_status"}
_REAL = {"woi", *[f"proj_woi_w{w}" for w in range(1, 6)]}


def _ddl(tabela: str, colunas: list[str]) -> str:
    tipos = [f'    "{c}" {"TEXT" if c in _TEXTO else "REAL" if c in _REAL else "INTEGER"}' for c in colunas]
    return f'CREATE TABLE {tabela} (\n    "id" INTEGER PRIMARY KEY,\n' + ",\n".join(tipos) + "\n)"


def gravar(db: Path, entrada: pd.DataFrame, saida: pd.DataFrame) -> None:
    with sqlite3.connect(db) as c:
        for tabela, df, colunas in (("ka_input_data", entrada, COLUNAS_IN), ("ka_deal_allocation", saida, COLUNAS_OUT)):
            c.execute(f"DROP TABLE IF EXISTS {tabela}")
            c.execute(_ddl(tabela, colunas))
            c.executemany(
                f"INSERT INTO {tabela} ({', '.join(chr(34) + x + chr(34) for x in colunas)}) "
                f"VALUES ({', '.join('?' for _ in colunas)})",
                df[colunas].itertuples(index=False, name=None),
            )
            for col in ("key_account_code", "deal_group", "origin", "month_status", "year_month"):
                c.execute(f'CREATE INDEX idx_{tabela}_{col} ON {tabela} ("{col}")')
            c.execute(f'CREATE INDEX idx_{tabela}_composite ON {tabela} ("material_id", "key_account_id", "fiscal_year", "quarter")')
        c.commit()
        c.execute("VACUUM")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=RAIZ / "allocation.db")
    args = ap.parse_args()
    original = args.db.with_name(args.db.stem + "_original" + args.db.suffix)
    if not original.exists():
        if not args.db.exists():
            raise SystemExit(f"Base original não encontrada: {args.db}")
        shutil.copy2(args.db, original)
        print(f"Base original copiada para {original}")
    entrada, saida = gerar(original, np.random.default_rng(SEMENTE))
    gravar(args.db, entrada, saida)
    print(f"{args.db}: {len(entrada)} linhas em ka_input_data e {len(saida)} em ka_deal_allocation")


if __name__ == "__main__":
    main()
