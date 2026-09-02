import { Injectable, computed, effect, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { AuditEntry } from '../models/audit.model';

export type AuditSort = 'newest' | 'task';

const COLOR_MAP: Record<string, string> = {
  'import bloqueado': '#f87171',
  'tick_params ha=': '#fb923c',
  SyntaxError: '#fbbf24',
  NameError: '#a78bfa',
  AttributeError: '#60a5fa',
  KeyError: '#34d399',
  TypeError: '#fb923c',
  outro: '#475569',
};

function classifyError(error: string): string {
  const e = (error || '').toLowerCase();
  if (e.includes('import de') || e.includes('bloqueados no sandbox')) return 'import bloqueado';
  if (e.includes('ha=') || e.includes('tick_params')) return 'tick_params ha=';
  if (e.includes('syntaxerror')) return 'SyntaxError';
  if (e.includes('nameerror')) return 'NameError';
  if (e.includes('attributeerror')) return 'AttributeError';
  if (e.includes('keyerror')) return 'KeyError';
  if (e.includes('typeerror')) return 'TypeError';
  return 'outro';
}

/**
 * Porta o painel de Error Audit de dashboard.html:854-906,2783-3024.
 */
@Injectable({ providedIn: 'root' })
export class AuditService {
  private readonly http = inject(HttpClient);

  readonly entries = signal<AuditEntry[]>([]);
  readonly loading = signal(false);
  readonly loadError = signal<string | null>(null);
  readonly loadedOnce = signal(false);

  readonly filter = signal('');
  readonly sort = signal<AuditSort>('newest');
  readonly selected = signal<Set<number>>(new Set());

  readonly filteredRows = computed(() => {
    const filter = this.filter().toLowerCase();
    let rows = this.entries().filter(
      (r) => !filter || (r.task_id || '').toLowerCase().includes(filter) || (r.error || '').toLowerCase().includes(filter),
    );
    if (this.sort() === 'task') {
      rows = [...rows].sort((a, b) => (a.task_id || '').localeCompare(b.task_id || ''));
    }
    return rows;
  });

  readonly patterns = computed(() => {
    const counts: Record<string, number> = {};
    for (const r of this.entries()) {
      const p = classifyError(r.error);
      counts[p] = (counts[p] || 0) + 1;
    }
    return Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([label, count]) => ({ label, count, color: COLOR_MAP[label] || '#475569' }));
  });

  constructor() {
    // Porta o "reset seleção ao re-renderizar" — a seleção é descartada sempre
    // que o filtro/ordenação mudam ou os dados são recarregados
    // (dashboard.html:2862-2864).
    effect(() => {
      this.filteredRows();
      this.selected.set(new Set());
    });
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.loadError.set(null);
    try {
      const data = await firstValueFrom(this.http.get<AuditEntry[]>('/tasks/code-audit/summary'));
      this.entries.set(data || []);
    } catch (e: any) {
      this.loadError.set('Erro ao carregar: ' + (e?.message ?? 'falha desconhecida'));
    } finally {
      this.loading.set(false);
      this.loadedOnce.set(true);
    }
  }

  toggleSelect(id: number, checked: boolean): void {
    this.selected.update((set) => {
      const next = new Set(set);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  selectAllVisible(): void {
    const ids = this.filteredRows().map((r) => r.id);
    this.selected.update((set) => new Set([...set, ...ids]));
  }

  async deleteOne(id: number): Promise<void> {
    await firstValueFrom(this.http.delete(`/tasks/code-audit/${id}`));
    this.entries.update((list) => list.filter((r) => r.id !== id));
  }

  async deleteSelected(): Promise<void> {
    const ids = [...this.selected()];
    if (!ids.length) return;
    if (!confirm(`Apagar ${ids.length} registro(s) selecionado(s)?`)) return;
    await Promise.all(ids.map((id) => firstValueFrom(this.http.delete(`/tasks/code-audit/${id}`))));
    const idSet = new Set(ids);
    this.entries.update((list) => list.filter((r) => !idSet.has(r.id)));
    this.selected.set(new Set());
  }

  async clearAll(): Promise<void> {
    if (!confirm('Apagar todos os registros do Error Audit?')) return;
    await firstValueFrom(this.http.delete('/tasks/code-audit'));
    this.entries.set([]);
  }

  entryById(id: number): AuditEntry | undefined {
    return this.entries().find((r) => r.id === id);
  }
}
