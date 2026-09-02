import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import Chart from 'chart.js/auto';
import { UserSessionService } from './user-session.service';
import { DashboardWidget, WidgetChartConfig } from '../models/widget.model';

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

  private readonly charts = new Map<string, Chart>();
  private readonly canvases = new Map<string, HTMLCanvasElement>();

  private orderKey(): string {
    return 'widget_order_' + this.userSession.userId;
  }

  private saveOrder(): void {
    const ids = this.widgets().map((w) => w.id);
    localStorage.setItem(this.orderKey(), JSON.stringify(ids));
  }

  private applyOrder(raw: DashboardWidget[]): DashboardWidget[] {
    try {
      const saved: string[] = JSON.parse(localStorage.getItem(this.orderKey()) || '[]');
      if (!saved.length) return raw;
      const map = new Map(raw.map((w) => [w.id, w]));
      const ordered = saved.filter((id) => map.has(id)).map((id) => map.get(id)!);
      const rest = raw.filter((w) => !saved.includes(w.id));
      return [...ordered, ...rest];
    } catch {
      return raw;
    }
  }

  async load(): Promise<void> {
    try {
      const raw = await firstValueFrom(
        this.http.get<DashboardWidget[]>('/dashboard-widgets', { params: { user_id: this.userSession.userId } }),
      );
      const widgets = this.applyOrder(raw || []);
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
      const chart = new Chart(canvas, {
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
      this.charts.set(id, chart);
      this.setError(id, null);
    } catch (e) {
      const msg = e instanceof HttpErrorResponse ? e.error?.message : null;
      this.setError(id, msg || 'Erro ao carregar dados do painel.');
    }
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

  /** Porta o drop de drag-and-drop — insere `srcId` na posição de `dstId`. */
  reorder(srcId: string, dstId: string): void {
    if (srcId === dstId) return;
    const list = [...this.widgets()];
    const srcIdx = list.findIndex((w) => w.id === srcId);
    if (srcIdx === -1) return;
    const [moved] = list.splice(srcIdx, 1);
    let dstIdx = list.findIndex((w) => w.id === dstId);
    if (dstIdx === -1) dstIdx = list.length;
    list.splice(dstIdx, 0, moved);
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
  }
}
