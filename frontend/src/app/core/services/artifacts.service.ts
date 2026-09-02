import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { UserSessionService } from './user-session.service';
import { Artifact, ArtifactType } from '../models/artifact.model';

export type ArtifactFilter = 'all' | 'task' | 'chat';

function keyOf(a: Pick<Artifact, 'type' | 'id'>): string {
  return `${a.type}:${a.id}`;
}

/**
 * Porta o painel de Artifacts de dashboard.html:908-975,3026-3218.
 */
@Injectable({ providedIn: 'root' })
export class ArtifactsService {
  private readonly http = inject(HttpClient);
  private readonly userSession = inject(UserSessionService);

  readonly all = signal<Artifact[]>([]);
  readonly loading = signal(false);
  readonly loadError = signal<string | null>(null);
  readonly filter = signal<ArtifactFilter>('all');
  readonly selected = signal<Set<string>>(new Set());

  /** Exclui artefatos de sessões de teste, como no original (renderArtifactList). */
  readonly visible = computed(() => this.all().filter((a) => !a.session_id?.startsWith('test_')));

  readonly filtered = computed(() => {
    const f = this.filter();
    const visible = this.visible();
    return f === 'all' ? visible : visible.filter((a) => a.origin === f);
  });

  async load(): Promise<void> {
    this.loading.set(true);
    this.loadError.set(null);
    this.selected.set(new Set());
    try {
      const data = await firstValueFrom(
        this.http.get<Artifact[]>('/artifacts', { params: { user_id: this.userSession.userId } }),
      );
      this.all.set(data || []);
    } catch {
      this.loadError.set('Erro ao carregar artifacts.');
    } finally {
      this.loading.set(false);
    }
  }

  setFilter(f: ArtifactFilter): void {
    this.filter.set(f);
  }

  toggleSelect(a: Pick<Artifact, 'type' | 'id'>): void {
    const key = keyOf(a);
    this.selected.update((set) => {
      const next = new Set(set);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  isSelected(a: Pick<Artifact, 'type' | 'id'>): boolean {
    return this.selected().has(keyOf(a));
  }

  selectAllVisible(): void {
    const visible = this.filtered();
    const allSelected = visible.every((a) => this.isSelected(a));
    this.selected.update((set) => {
      const next = new Set(set);
      for (const a of visible) {
        const key = keyOf(a);
        if (allSelected) next.delete(key);
        else next.add(key);
      }
      return next;
    });
  }

  async deleteOne(type: ArtifactType, id: string): Promise<void> {
    await firstValueFrom(this.http.delete(`/artifacts/${type}/${id}`, { params: { user_id: this.userSession.userId } }));
    this.all.update((list) => list.filter((a) => !(a.type === type && a.id === id)));
  }

  async deleteSelected(): Promise<void> {
    const keys = [...this.selected()];
    if (!keys.length) return;
    if (!confirm(`Deletar ${keys.length} artifact${keys.length > 1 ? 's' : ''} selecionado${keys.length > 1 ? 's' : ''}?`)) return;
    await Promise.all(
      keys.map((key) => {
        const [type, id] = key.split(':');
        return firstValueFrom(this.http.delete(`/artifacts/${type}/${id}`, { params: { user_id: this.userSession.userId } }));
      }),
    );
    const keySet = new Set(keys);
    this.all.update((list) => list.filter((a) => !keySet.has(keyOf(a))));
    this.selected.set(new Set());
  }
}
