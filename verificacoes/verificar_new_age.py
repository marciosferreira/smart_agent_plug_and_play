"""Verificações de consistência da base New Age (DL Cost), lida pelo GraphQL.

Não é importado nem chamado pelo app. Reaplicável: roda as mesmas
verificações sobre o que estiver na base no momento (dados mockados hoje, dados
reais quando forem carregados). Só leitura: usa reportSchema e reportTable
(paginado de 1000 em 1000) pelo cliente do próprio app (graphql_config.py), com o
endpoint e a chave definidos em config/settings.yml e NEW_AGE_API_KEY no .env.

Uso, a partir da raiz do projeto:
    python verificacoes/verificar_new_age.py                 # todas as seções
    python verificacoes/verificar_new_age.py --secao 1       # só uma seção (1 a 5)
    python verificacoes/verificar_new_age.py --anonimizar    # sites como "Site A", "Site B"

Seções:
    1. Integridade e cálculos   — testes que devem passar (OK/FALHA); sai com código 1 se algum falhar
    2. Armadilhas de interpretação — medição de cada problema conhecido
    3. Hipóteses concorrentes   — a diferença entre custo local e em dólar: câmbio ou desconto?
    4. Calendário fiscal        — mês a mês, ano civil contra ano fiscal (FY começa em abril)
    5. Método de cálculo da taxa horária — média simples contra valor vigente reconstruído
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
load_dotenv(RAIZ / ".env")

import graphql_config  # noqa: E402

AREAS = ["backend_cost", "cfc_cost", "smt_cost", "l5_cost"]
SITES_COMPARADOS = ("JAG", "BPL")  # par usado na seção 5 (ver dominio.md, regra de vigência)

_QUERY_SCHEMA = """
query { reportSchema { tables {
    table
    relations { fromTable fromColumn toTable toColumn }
} } }
"""


# ---------------------------------------------------------------- leitura

def carregar_base():
    """Lê o schema e todas as tabelas inteiras (paginando), devolve (relações, {tabela: DataFrame})."""
    tabelas = graphql_config._post(_QUERY_SCHEMA, {})["reportSchema"]["tables"]
    relacoes = {
        (r["fromTable"], r["fromColumn"], r["toTable"], r["toColumn"])
        for t in tabelas for r in (t.get("relations") or [])
    }
    dfs = {}
    for t in tabelas:
        linhas, offset = [], 0
        while True:
            pagina = graphql_config.run_query_table(t["table"], limit=1000, offset=offset)
            linhas += pagina
            if len(pagina) < 1000:
                break
            offset += 1000
        dfs[t["table"]] = pd.DataFrame(linhas)
    return sorted(relacoes), dfs


def _num(df, *cols):
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _br(valor, casas=2) -> str:
    """Número no formato brasileiro: 13289.5 -> '13.289,50'."""
    return f"{valor:,.{casas}f}".replace(",", "#").replace(".", ",").replace("#", ".")


def _quarter_fiscal(mes: int) -> int:
    return (mes - 4) % 12 // 3 + 1


def _ano_fiscal(ano: int, mes: int) -> int:
    return ano if mes >= 4 else ano - 1


def _inicio_do_quarter(rotulo: str) -> str:
    """'Q3,2026' -> '2026-10' (mês de início do trimestre fiscal, FY nomeado pelo ano de abril)."""
    q, ano = rotulo.split(",")
    q, ano = int(q[1:]), int(ano)
    mes = 4 + 3 * (q - 1)
    if mes > 12:
        mes, ano = mes - 12, ano + 1
    return f"{ano}-{mes:02d}"


# ---------------------------------------------------------------- impressão

def titulo(texto):
    print()
    print(texto)
    print("=" * len(texto))


def linha_teste(nome, resultado, ok):
    print(f"  {'[OK]' if ok else '[FALHA]':<8}{nome:<62} {resultado}")
    return ok


def linha_medida(nome, resultado):
    print(f"  - {nome:<56} {resultado}")


# ---------------------------------------------------------------- seção 1

def secao_integridade(relacoes, dfs):
    titulo("1. Integridade e cálculos")
    oks = []

    com_id = [t for t, df in dfs.items() if "id" in df.columns]
    dup = {t: int(dfs[t]["id"].duplicated().sum()) for t in com_id}
    oks.append(linha_teste(f"Chaves primárias duplicadas ({len(com_id)} tabelas com id)",
                           f"{sum(dup.values())} duplicadas", sum(dup.values()) == 0))

    orfaos = {}
    for de_t, de_c, para_t, para_c in relacoes:
        if de_t not in dfs or para_t not in dfs or de_c not in dfs[de_t].columns:
            continue
        valores = dfs[de_t][de_c].dropna()
        orfaos[(de_t, de_c)] = int((~valores.isin(dfs[para_t][para_c])).sum())
    oks.append(linha_teste(f"Registros órfãos ({len(orfaos)} relacionamentos)",
                           f"{sum(orfaos.values())} órfãos", sum(orfaos.values()) == 0))

    f = _num(dfs["fact_final_dl_cost"].copy(), *AREAS, "dl_cost_by_cm", "dl_cost_by_cm_usd", "currency_rate")
    n = len(f)
    soma = np.isclose(f[AREAS].sum(axis=1), f["dl_cost_by_cm"])
    oks.append(linha_teste("Soma das 4 áreas = custo total (custo final)",
                           f"{soma.sum()} de {n} corretos", soma.all()))
    conv = np.isclose(f["dl_cost_by_cm"] / f["currency_rate"], f["dl_cost_by_cm_usd"])
    oks.append(linha_teste("Custo em dólar = custo local / câmbio",
                           f"{conv.sum()} de {n} corretos", conv.all()))
    rotulo = f["quarter"].map(_inicio_do_quarter) == f["start_month"].str[:7]
    oks.append(linha_teste("Rótulo do trimestre coerente com a janela de datas",
                           f"{rotulo.sum()} de {n} corretos", rotulo.all()))

    cal = _num(dfs["dim_calendar"].copy(), "month", "year", "quarter")
    q_ok = cal["quarter"] == cal["month"].astype(int).map(_quarter_fiscal)
    oks.append(linha_teste("Trimestre fiscal do calendário (abr-jun = Q1)",
                           f"{q_ok.sum()} de {len(cal)} corretos", q_ok.all()))

    dc = _num(dfs["fact_dl_discount"].copy(), "discount_value")
    invertidas = int((pd.to_datetime(dc["end_date"]) < pd.to_datetime(dc["start_date"])).sum())
    oks.append(linha_teste("Descontos com data final anterior à inicial",
                           f"{invertidas}", invertidas == 0))
    acima = int(((dc["value_type"] == "PERCENTAGE") & (dc["discount_value"] > 100)).sum())
    oks.append(linha_teste("Descontos percentuais acima de 100%", f"{acima}", acima == 0))

    st = _num(dfs["fact_standard_time"].copy(), "standard_time", "upph")
    pos = st[st["standard_time"] > 0]
    inv = np.isclose(pos["upph"], 1 / pos["standard_time"])
    oks.append(linha_teste("Produtividade (upph) = 1 / tempo padrão",
                           f"{inv.sum()} de {len(pos)} corretos", inv.all()))

    aprovados = sum(oks)
    print(f"\n  Resultado: {aprovados} de {len(oks)} testes aprovados")
    return all(oks)


# ---------------------------------------------------------------- seção 2

def secao_armadilhas(dfs, hoje):
    titulo("2. Armadilhas de interpretação (medição)")
    f = _num(dfs["fact_final_dl_cost"].copy(), "dl_cost_by_cm_usd")
    n = len(f)

    janela_ini, janela_fim = f["start_month"].str[:7], f["end_month"].str[:7]
    fora = int(((f["month_id"] < janela_ini) | (f["month_id"] > janela_fim)).sum())
    linha_medida("Mês de referência fora do trimestre da própria linha", f"{fora} de {n} linhas")

    ponte = dfs["bridge_final_dl_cost_sales_model"].merge(f, left_on="final_dl_cost_id", right_on="id")
    antes, depois = f["dl_cost_by_cm_usd"].sum(), ponte["dl_cost_by_cm_usd"].sum()
    linha_medida("Custo final cruzado com a ponte de Sales Models",
                 f"{n} -> {len(ponte)} linhas; soma US$ {_br(antes, 0)} -> US$ {_br(depois, 0)}"
                 f" ({_br(depois / antes, 1)}x)")

    st = _num(dfs["fact_standard_time"].copy(), "standard_time")
    vazias_st = [c for c in ("site_id", "product_id", "sales_model_id") if st[c].isna().all()]
    linha_medida("Tempo padrão: colunas 100% vazias", f"{', '.join(vazias_st)} ({len(st)} linhas)")

    vazias_f = [c for c in f.columns if f[c].isna().all()]
    linha_medida("Custo final: colunas 100% vazias", ", ".join(vazias_f))

    dc = dfs["fact_dl_discount"]
    sem_tipo = dc[dc["value_type"].isna()]
    tipos = ", ".join(f"{k} {v}" for k, v in sem_tipo["discount_type"].value_counts().items())
    linha_medida("Descontos sem tipo de valor (% ou absoluto)", f"{len(sem_tipo)} ({tipos})")

    zero = pd.to_datetime(dc["end_date"]) == pd.to_datetime(dc["start_date"])
    status = ", ".join(f"{k} {v}" for k, v in dc[zero]["status_approval"].value_counts().items())
    linha_medida("Descontos com janela de zero dia", f"{int(zero.sum())} de {len(dc)} ({status})")

    linha_medida("Tempo padrão igual a zero", f"{int((st['standard_time'] == 0).sum())} linhas")

    hr = dfs["fact_hourly_rate"]
    versoes = hr.groupby(["site_id", "product_id", "area", "month_id"]).size()
    linha_medida("Taxa horária com mais de uma versão no mesmo mês",
                 f"{int((versoes > 1).sum())} combinações")

    hoje_iso = hoje.isoformat()
    ini, fim = f["start_month"].str[:10], f["end_month"].str[:10]
    andamento = (ini <= hoje_iso) & (fim >= hoje_iso)
    futuro = ini > hoje_iso
    qs = ", ".join(sorted(f[andamento]["quarter"].unique()))
    linha_medida(f"Custo final de trimestre não fechado em {hoje:%d/%m/%Y}",
                 f"{int(andamento.sum())} em andamento ({qs or '-'}), {int(futuro.sum())} futuras, de {n}")


# ---------------------------------------------------------------- seção 3

def secao_hipoteses(dfs):
    titulo("3. Hipóteses concorrentes: custo local x custo em dólar")
    f = _num(dfs["fact_final_dl_cost"].copy(), "dl_cost_by_cm", "dl_cost_by_cm_usd", "currency_rate")
    dc = _num(dfs["fact_dl_discount"].copy(), "discount_value")
    n = len(f)
    f["diferenca"] = f["dl_cost_by_cm"] - f["dl_cost_by_cm_usd"]
    com_diferenca = f[~np.isclose(f["diferenca"], 0)]

    h1 = np.isclose(f["dl_cost_by_cm"] / f["currency_rate"], f["dl_cost_by_cm_usd"])

    def bate_com_desconto(r):
        cand = dc[(dc["site_id"] == r["site_id"]) & (dc["product_id"] == r["product_id"])]
        absoluto = (cand["value_type"] != "PERCENTAGE") & np.isclose(cand["discount_value"], r["diferenca"])
        pct = (cand["value_type"] == "PERCENTAGE") & np.isclose(
            r["dl_cost_by_cm"] * (1 - cand["discount_value"] / 100), r["dl_cost_by_cm_usd"])
        return bool((absoluto | pct).any())

    h2 = com_diferenca.apply(bate_com_desconto, axis=1) if len(com_diferenca) else pd.Series(dtype=bool)

    print(f"  Linhas do custo final: {n}  |  com diferença entre local e dólar: {len(com_diferenca)}")
    print()
    print(f"  H1  dólar = local / câmbio                       explica {int(h1.sum()):>2} de {n} linhas")
    print(f"  H2  dólar = local - desconto negociado           explica {int(h2.sum()):>2} de {len(com_diferenca)}"
          " linhas com diferença")
    print()
    print("  Exemplos (H1):")
    exemplos = com_diferenca.drop_duplicates(["dl_cost_by_cm", "currency_rate"]).head(4)
    for _, r in exemplos.iterrows():
        calculado = r["dl_cost_by_cm"] / r["currency_rate"]
        print(f"    {_br(r['dl_cost_by_cm'], 0):>6} / {_br(r['currency_rate'], 1):<5} = {_br(calculado, 3):<8}"
              f" registrado na base: {_br(r['dl_cost_by_cm_usd'], 3)}")
    conclusao = "H1 confirmada; H2 rejeitada (coincidências pontuais)" if h1.all() else "H1 NÃO se confirma em todas as linhas"
    print(f"\n  Conclusão: {conclusao}")


# ---------------------------------------------------------------- seção 4

def secao_calendario(dfs):
    titulo("4. Calendário fiscal (ano fiscal começa em abril e leva o nome do ano em que começa)")
    cal = _num(dfs["dim_calendar"].copy(), "month", "year", "quarter").sort_values("id")
    print(f"  {'Mês':<9}{'Ano civil':>10}{'Trim. civil':>13}{'Trim. fiscal':>14}{'Rótulo de negócio':>20}  Confere")
    erros = 0
    for _, r in cal.iterrows():
        m, a, q = int(r["month"]), int(r["year"]), int(r["quarter"])
        ok = q == _quarter_fiscal(m)
        erros += not ok
        rotulo = f"FY{_ano_fiscal(a, m)} Q{q}"
        destaque = "  <- ano fiscal ≠ ano civil" if m <= 3 else ""
        print(f"  {r['id']:<9}{a:>10}{f'Q{(m - 1) // 3 + 1}':>13}{f'Q{q}':>14}{rotulo:>20}  {'OK' if ok else 'FALHA'}{destaque}")
    print(f"\n  {len(cal) - erros} de {len(cal)} meses com trimestre fiscal correto")


# ---------------------------------------------------------------- seção 5

def _vigente(hr, mes):
    """Taxa vigente em `mes` por site+produto+área: o registro de maior month_id <= mes,
    considerando APPROVED e EXPIRED (status reflete o hoje, não o passado)."""
    ate = hr[hr["month_id"] <= mes].sort_values(["month_id", "approved_at"], na_position="first")
    return ate.groupby(["site_id", "product_id", "area"]).tail(1)


def secao_metodos(dfs, anonimizar):
    titulo("5. Taxa horária por site: média simples x valor vigente reconstruído")
    hr = _num(dfs["fact_hourly_rate"].copy(), "hourly_rate")
    nomes = dfs["dim_site"].set_index("id")["name"]
    hr["site"] = hr["site_id"].map(nomes)
    rotulos = {s: (f"Site {chr(65 + i)}" if anonimizar else s) for i, s in enumerate(SITES_COMPARADOS)}

    ingenua = hr[hr["status_approval"] == "APPROVED"].groupby("site")["hourly_rate"].mean()
    # meses em que os dois sites têm registros próprios
    meses_proprios = [set(hr[hr["site"] == s]["month_id"]) for s in SITES_COMPARADOS]
    inicio = max(min(m) for m in meses_proprios)
    fim = min(max(m) for m in meses_proprios)
    meses = sorted(m for m in hr["month_id"].unique() if inicio <= m <= fim)

    print("  Método A (ingênuo): média dos registros com status APPROVED, sem olhar o mês")
    print("  Método B (vigente): para cada mês, o valor mais recente com mês <= ao mês analisado,")
    print("                      incluindo registros EXPIRED (regra de vigência do dominio.md)")
    print()
    cab = f"  {'Site':<8}{'Método A':>11}" + "".join(f"{'B ' + m:>12}" for m in meses)
    print(cab)
    vigentes = {m: _vigente(hr, m).groupby("site")["hourly_rate"].mean() for m in meses}
    for s in SITES_COMPARADOS:
        print(f"  {rotulos[s]:<8}{_br(ingenua.get(s, np.nan)):>11}"
              + "".join(f"{_br(vigentes[m].get(s, np.nan)):>12}" for m in meses))

    a, b = SITES_COMPARADOS
    mais_caro_a = a if ingenua.get(a, 0) > ingenua.get(b, 0) else b
    print()
    print(f"  Método A: mais caro = {rotulos[mais_caro_a]}")
    for m in meses:
        mais_caro = a if vigentes[m].get(a, 0) > vigentes[m].get(b, 0) else b
        marca = "  <- conclusão invertida" if mais_caro != mais_caro_a else ""
        print(f"  Método B em {m}: mais caro = {rotulos[mais_caro]}{marca}")
    print(f"\n  Janela: {inicio} a {fim} (meses em que os dois sites têm taxa registrada)")


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--secao", type=int, choices=range(1, 6), help="roda só uma seção")
    p.add_argument("--anonimizar", action="store_true", help="mostra os sites como Site A, Site B")
    args = p.parse_args()

    if not graphql_config.is_configured():
        sys.exit("GraphQL não configurado — confira config/settings.yml e NEW_AGE_API_KEY no .env")

    hoje = date.today()
    relacoes, dfs = carregar_base()
    print(f"Verificação da base New Age (DL Cost) via GraphQL — {hoje:%d/%m/%Y}")
    print("Tabelas lidas: " + ", ".join(f"{t} ({len(df)})" for t, df in sorted(dfs.items())))

    ok = True
    secoes = {
        1: lambda: secao_integridade(relacoes, dfs),
        2: lambda: secao_armadilhas(dfs, hoje),
        3: lambda: secao_hipoteses(dfs),
        4: lambda: secao_calendario(dfs),
        5: lambda: secao_metodos(dfs, args.anonimizar),
    }
    for n, fn in secoes.items():
        if args.secao in (None, n):
            r = fn()
            if n == 1:
                ok = r
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
