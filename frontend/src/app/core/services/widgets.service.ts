import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import Chart from 'chart.js/auto';
import { MatrixController, MatrixElement } from 'chartjs-chart-matrix';
import { UserSessionService } from './user-session.service';
import { DashboardWidget, WidgetChartConfig, MatrixWidgetData } from '../models/widget.model';

Chart.register(MatrixController, MatrixElement);

/** Rampa sequencial azul (references/palette.md do skill dataviz) — um hue só, claro→escuro. */
const HEAT_LOW = { r: 0xcd, g: 0xe2, b: 0xfb }; // #cde2fb
const HEAT_HIGH = { r: 0x0d, g: 0x36, b: 0x6b }; // #0d366b

function heatColor(t: number): string {
  const r = Math.round(HEAT_LOW.r + (HEAT_HIGH.r - HEAT_LOW.r) * t);
  const g = Math.round(HEAT_LOW.g + (HEAT_HIGH.g - HEAT_LOW.g) * t);
  const b = Math.round(HEAT_LOW.b + (HEAT_HIGH.b - HEAT_LOW.b) * t);
  return `rgb(${r},${g},${b})`;
}

/**
 * Porta a grid de Painéis Customizados de dashboard.html:981-991,1202-1380.
 *
 * Assim como no original (`_widgetCharts`), as instâncias Chart.js vivem
 * aqui, centralizadas por widget id — não dentro do componente do card —
 * porque os eventos SSE do chat (`widget_added/updated/deleted`, Fase 1)
 * precisam conseguir re-renderizar/destruir um gráfico sem depender de o
 * componente estar "escutando" ativamente.
 */
@Injectable({ providedIn: 'root' })
export class WidgetsService {
  private readonly http = inject(HttpClient);
  private readonly userSession = inject(UserSessionService);

  readonly widgets = signal<DashboardWidget[]>([]);
  readonly loaded = signal(false);
  readonly errors = signal<Record<string, string>>({});
  /** Tipo Chart.js (bar/pie/line/...) de cada painel, descoberto ao carregar seus dados — usado pelo card para exibir o tipo (ver widget-card.component.ts). */
  readonly chartTypes = signal<Record<string, string>>({});

  private readonly charts = new Map<string, Chart>();
  private readonly canvases = new Map<string, HTMLCanvasElement>();

  /**
   * Persiste a ordem atual no backend (dashboard_widget_order), por usuário — não
   * mais localStorage, pra funcionar em qualquer dispositivo/navegador do usuário.
   * O servidor reconcilia contra a lista atual de widgets a cada leitura (ver
   * _reconcile_widget_order em main.py), então sobrevive a gráficos inseridos ou
   * removidos do arquivo default entre um reorder e outro.
   */
  private async saveOrder(): Promise<void> {
    const widget_ids = this.widgets().map((w) => w.id);
    try {
      await firstValueFrom(
        this.http.put('/dashboard-widgets/order', { user_id: this.userSession.userId, widget_ids }),
      );
    } catch (e) {
      console.warn('saveOrder error', e);
    }
  }

  async load(): Promise<void> {
    try {
      const widgets = await firstValueFrom(
        this.http.get<DashboardWidget[]>('/dashboard-widgets', { params: { user_id: this.userSession.userId } }),
      ) || [];
      const currentIds = new Set(widgets.map((w) => w.id));
      for (const [id] of this.charts) {
        if (!currentIds.has(id)) this.destroyChart(id);
      }
      this.widgets.set(widgets);
      this.loaded.set(true);
      for (const w of widgets) {
        this.renderWidget(w.id);
      }
    } catch (e) {
      console.warn('loadCustomWidgets error', e);
      this.loaded.set(true);
    }
  }

  /** Chamado pelo WidgetCardComponent quando seu <canvas> fica disponível. */
  registerCanvas(id: string, canvas: HTMLCanvasElement): void {
    this.canvases.set(id, canvas);
    this.renderWidget(id);
  }

  unregisterCanvas(id: string): void {
    this.canvases.delete(id);
  }

  private destroyChart(id: string): void {
    this.charts.get(id)?.destroy();
    this.charts.delete(id);
  }

  async renderWidget(id: string): Promise<void> {
    const canvas = this.canvases.get(id);
    if (!canvas) return;
    try {
      const config = await firstValueFrom(this.http.get<WidgetChartConfig>(`/dashboard-widgets/${id}/data`));
      this.destroyChart(id);
      const chart = config.type === 'matrix' ? this.buildMatrixChart(canvas, config) : this.buildStandardChart(canvas, config);
      this.charts.set(id, chart);
      this.chartTypes.update((cur) => ({ ...cur, [id]: config.type }));
      this.setError(id, null);
    } catch (e) {
      const msg = e instanceof HttpErrorResponse ? e.error?.message : null;
      this.setError(id, msg || 'Erro ao carregar dados do painel.');
    }
  }

  private buildStandardChart(canvas: HTMLCanvasElement, config: WidgetChartConfig): Chart {
    return new Chart(canvas, {
      type: config.type as never,
      data: config.data as never,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
          tooltip: { backgroundColor: '#e2e8f0', titleColor: '#1e293b', bodyColor: '#94a3b8' },
        },
        scales: ['bar', 'line', 'scatter'].includes(config.type)
          ? {
              x: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#e2e8f0' } },
              y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#e2e8f0' } },
            }
          : undefined,
        ...(config.options || {}),
      } as never,
    });
  }

  /**
   * Heatmap (chartjs-chart-matrix) — payload tem shape própria (points/xLabels/yLabels),
   * diferente do labels/datasets padrão dos outros tipos, porque a cor de cada célula
   * depende do min/max do conjunto inteiro (rampa sequencial azul, ver palette.md do
   * skill dataviz) — isso só dá pra calcular no cliente, o backend manda os valores crus.
   */
  private buildMatrixChart(canvas: HTMLCanvasElement, config: WidgetChartConfig): Chart {
    const data = config.data as MatrixWidgetData;
    const values = data.points.map((p) => p.v).filter((v): v is number => v != null);
    const min = values.length ? Math.min(...values) : 0;
    const max = values.length ? Math.max(...values) : 1;
    const colorFor = (v: number | null): string => {
      if (v == null) return 'rgba(0,0,0,0)';
      const t = max > min ? (v - min) / (max - min) : 0.5;
      return heatColor(t);
    };
    return new Chart(canvas, {
      type: 'matrix' as never,
      data: {
        datasets: [
          {
            label: data.label || '',
            data: data.points as never,
            backgroundColor: (ctx: any) => colorFor(ctx.raw?.v ?? null),
            borderColor: '#fcfcfb',
            borderWidth: 2,
            width: (ctx: any) => (ctx.chart.chartArea?.width ?? 0) / data.xLabels.length - 2,
            height: (ctx: any) => (ctx.chart.chartArea?.height ?? 0) / data.yLabels.length - 2,
          } as never,
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#e2e8f0',
            titleColor: '#1e293b',
            bodyColor: '#94a3b8',
            callbacks: {
              title: () => '',
              label: (item: any) => `${item.raw.y} / ${item.raw.x}: ${item.raw.v ?? 'sem dado'}`,
            },
          },
        },
        scales: {
          x: { type: 'category', labels: data.xLabels, offset: true, ticks: { color: '#64748b', font: { size: 10 } }, grid: { display: false } },
          y: { type: 'category', labels: data.yLabels, offset: true, ticks: { color: '#64748b', font: { size: 10 } }, grid: { display: false } },
        },
      } as never,
    });
  }

  private setError(id: string, msg: string | null): void {
    this.errors.update((cur) => {
      const next = { ...cur };
      if (msg) next[id] = msg;
      else delete next[id];
      return next;
    });
  }

  async removeWidget(id: string): Promise<void> {
    if (!confirm('Remover este painel do dashboard?')) return;
    try {
      await firstValueFrom(this.http.delete(`/dashboard-widgets/${id}`, { params: { user_id: this.userSession.userId } }));
      this.destroyChart(id);
      this.canvases.delete(id);
      this.widgets.update((list) => list.filter((w) => w.id !== id));
      this.saveOrder();
    } catch {
      alert('Erro ao remover painel.');
    }
  }

  /** Alterna o compartilhamento de um painel customizado — só o dono pode chamar (o botão já fica oculto pra quem não é). */
  async toggleShare(id: string, shared: boolean): Promise<void> {
    const prev = this.widgets();
    this.widgets.update((list) => list.map((w) => (w.id === id ? { ...w, shared } : w)));
    try {
      await firstValueFrom(
        this.http.patch(`/dashboard-widgets/${id}/share`, { user_id: this.userSession.userId, shared }),
      );
    } catch {
      this.widgets.set(prev);
      alert('Erro ao alterar compartilhamento do painel.');
    }
  }

  /**
   * Porta o drop de drag-and-drop — move `srcId` para perto de `dstId`.
   *
   * Direção importa: ao mover pra frente (índice cresce — direita/baixo no grid),
   * o item de origem termina LOGO DEPOIS do alvo; ao mover pra trás, termina LOGO
   * ANTES. Sem essa distinção, mover um card pra frente até seu vizinho imediato
   * vira um no-op (o índice do alvo já recua 1 ao remover a origem, então o item
   * volta pro mesmo lugar) — é exatamente o gesto mais natural pra testar "pra
   * direita/baixo", por isso parecia que só funcionava pra esquerda/cima.
   */
  reorder(srcId: string, dstId: string): void {
    if (srcId === dstId) return;
    const list = [...this.widgets()];
    const srcIdx = list.findIndex((w) => w.id === srcId);
    const dstIdx = list.findIndex((w) => w.id === dstId);
    if (srcIdx === -1 || dstIdx === -1) return;
    const movingForward = srcIdx < dstIdx;
    const [moved] = list.splice(srcIdx, 1);
    const shiftedDstIdx = movingForward ? dstIdx - 1 : dstIdx;
    const insertAt = movingForward ? shiftedDstIdx + 1 : shiftedDstIdx;
    list.splice(insertAt, 0, moved);
    this.widgets.set(list);
    this.saveOrder();
  }

  // ── Hooks para os eventos SSE do chat (Fase 1 → aqui) ──

  onWidgetAdded(): void {
    this.load();
  }

  onWidgetUpdated(id: string): void {
    this.renderWidget(id);
  }

  onWidgetDeleted(id: string): void {
    this.destroyChart(id);
    this.canvases.delete(id);
    this.widgets.update((list) => list.filter((w) => w.id !== id));
    this.chartTypes.update((cur) => {
      const next = { ...cur };
      delete next[id];
      return next;
    });
  }
}
