"""
Simulador simplificado do algoritmo de alocação, usado SÓ para gerar a base demo
(gerar_demo.py). Os modelos e a tool do agente não dependem dele: em produção o
algoritmo real é externo, e o que o agente vê são os resultados nas tabelas.

Segue as regras do dominio.md, numa versão enxuta:
  1. Request, por ordem de Score (0 = MMICOM primeiro ... 7 por último); dentro do
     mesmo Score, rateio proporcional ao pedido. Request Final = Initial + Rollover +
     Rollback.
  2. Rollback: o que um mês do quarter não atendeu tenta a capacidade que sobrou nos
     meses ANTERIORES do mesmo quarter (do mais recente para o mais antigo) e soma ao
     Request Final do mês de destino.
  3. Rollover: o que ainda ficou sem atendimento passa para o mês seguinte (o 3º mês
     passa para o 1º do próximo quarter); uma parte é cancelada pela KA (`carry`).
  4. Deal: com o que sobrou depois do Request, cada (KA, deal_group) recebe até o
     valor do acordo, dividido pelos meses do quarter.
  5. Retail: recebe todo o resíduo da capacidade do mês.
Não simula Site Supply (cada material tem uma origem só), Sell-in Compensation nem
alocação manual: na base demo essas colunas ficam zeradas ou iguais à alocação.

O plano é calculado no início do quarter para os 3 meses, como num ciclo de
planejamento trimestral.
"""
from __future__ import annotations

import numpy as np


def distribuir(disponivel: int, pedidos: np.ndarray, prioridades: np.ndarray) -> tuple[np.ndarray, int]:
    """Atende `pedidos` (inteiros) por prioridade crescente com `disponivel` unidades.
    Dentro de uma prioridade que não cabe inteira, rateia proporcionalmente (inteiros,
    sobras pelos maiores restos) e as prioridades seguintes não recebem nada."""
    dado = np.zeros(len(pedidos), dtype=np.int64)
    for s in np.unique(prioridades[pedidos > 0]):
        idx = np.where((prioridades == s) & (pedidos > 0))[0]
        precisa = int(pedidos[idx].sum())
        if precisa <= disponivel:
            dado[idx] = pedidos[idx]
            disponivel -= precisa
            continue
        if disponivel > 0:
            exato = pedidos[idx] * disponivel / precisa
            parte = np.floor(exato).astype(np.int64)
            for j in np.argsort(-(exato - parte))[: disponivel - int(parte.sum())]:
                parte[j] += 1
            dado[idx] = parte
        disponivel = 0
        break
    return dado, disponivel


def alocar_quarter(
    capacidade: np.ndarray,       # (n_mat, 3) capacidade mensal de cada material
    combo_mat: np.ndarray,        # (n_combo,) material de cada combinação KA x material
    eh_retail: np.ndarray,        # (n_combo,) True na linha de Retail do material
    pedidos: np.ndarray,          # (n_combo, 3) Initial Request (0 no Retail e antes do lançamento)
    scores: np.ndarray,           # (n_combo,) Score do quarter (ignorado no Retail)
    deals: list[tuple[np.ndarray, int]],  # [(combos do (KA, deal_group), valor do acordo)]
    rollover_entrada: np.ndarray, # (n_combo,) rollover que chega no 1º mês
    carry: float = 0.8,
) -> dict[str, np.ndarray]:
    n = len(combo_mat)
    req = np.zeros((n, 3), dtype=np.int64)
    rollover = np.zeros((n, 3), dtype=np.int64)
    rollback = np.zeros((n, 3), dtype=np.int64)
    alocado = np.zeros((n, 3), dtype=np.int64)
    rollover[:, 0] = rollover_entrada
    rollover_saida = np.zeros(n, dtype=np.int64)
    sobra = capacidade.astype(np.int64).copy()

    # 1-3. Request, Rollback e Rollover, material a material
    for mat in range(capacidade.shape[0]):
        ks = np.where((combo_mat == mat) & ~eh_retail)[0]
        if len(ks) == 0:
            continue
        for j in range(3):
            req[ks, j] += pedidos[ks, j] + rollover[ks, j]
            dado, sobra[mat, j] = distribuir(int(sobra[mat, j]), req[ks, j] - alocado[ks, j], scores[ks])
            alocado[ks, j] += dado
            falta = req[ks, j] - alocado[ks, j]
            for p in range(j - 1, -1, -1):
                if falta.sum() == 0 or sobra[mat, p] == 0:
                    continue
                dado_p, sobra[mat, p] = distribuir(int(sobra[mat, p]), falta, scores[ks])
                rollback[ks, p] += dado_p
                req[ks, p] += dado_p
                alocado[ks, p] += dado_p
                falta = falta - dado_p
            levado = (np.floor(falta * carry / 10) * 10).astype(np.int64)
            if j < 2:
                rollover[ks, j + 1] = levado
            else:
                rollover_saida[ks] = levado

    # 4. Deal: volume além do Request, até o valor do acordo, dividido pelos meses
    for combos, valor in deals:
        restante = int(valor)
        for j in range(3):
            meta = int(np.ceil(restante / (3 - j)))
            for c in combos[np.argsort(-req[combos, j])]:
                if meta <= 0:
                    break
                mat = combo_mat[c]
                dar = min(meta, int(sobra[mat, j]))
                dar -= dar % 10
                if dar <= 0:
                    continue
                alocado[c, j] += dar
                sobra[mat, j] -= dar
                meta -= dar
                restante -= dar

    # 5. Retail: o resíduo do mês
    for c in np.where(eh_retail)[0]:
        alocado[c] += sobra[combo_mat[c]]
        sobra[combo_mat[c]] = 0

    return {"req": req, "rollover": rollover, "rollback": rollback, "alocado": alocado,
            "rollover_saida": rollover_saida}


def dividir_semanas(alocado_mes: np.ndarray, combo_mat: np.ndarray, eh_retail: np.ndarray,
                    cap_semanas: np.ndarray) -> np.ndarray:
    """Divide a alocação mensal de cada combinação pelas semanas do mês, na proporção da
    capacidade semanal do material. A soma de todas as KAs numa semana nunca passa da
    capacidade daquela semana: o Retail fica com o que sobra em cada semana."""
    n, n_sem = len(combo_mat), cap_semanas.shape[1]
    out = np.zeros((n, n_sem), dtype=np.int64)
    for mat in range(cap_semanas.shape[0]):
        cap = cap_semanas[mat].astype(np.int64)
        total = int(cap.sum())
        kas = [c for c in np.where((combo_mat == mat) & ~eh_retail)[0] if alocado_mes[c] > 0 and total > 0]
        # primeiro a parte proporcional de TODAS as KAs; só depois as sobras do arredondamento,
        # sempre na semana com mais folga que restou (se cada KA distribuísse as suas sobras
        # sozinha, uma semana podia passar da capacidade quando as outras chegassem)
        for c in kas:
            out[c] = np.floor(int(alocado_mes[c]) * cap / total).astype(np.int64)
        usado = out[kas].sum(0) if kas else np.zeros(n_sem, dtype=np.int64)
        for c in kas:
            for _ in range(int(alocado_mes[c]) - int(out[c].sum())):
                w = int(np.argmax(cap - usado))
                out[c, w] += 1
                usado[w] += 1
        for c in np.where((combo_mat == mat) & eh_retail)[0]:
            out[c] = cap - usado
    return out
