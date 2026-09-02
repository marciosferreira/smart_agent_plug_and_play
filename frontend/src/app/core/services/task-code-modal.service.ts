import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { UserSessionService } from './user-session.service';
import { TaskCodeResponse } from '../models/task.model';

/** Porta o Task Code Viewer de dashboard.html:2678-2747. */
@Injectable({ providedIn: 'root' })
export class TaskCodeModalService {
  private readonly http = inject(HttpClient);
  private readonly userSession = inject(UserSessionService);

  readonly open = signal(false);
  readonly loading = signal(false);
  readonly title = signal('');
  readonly errorMsg = signal<string | null>(null);
  readonly current = signal<{ kind: 'code' | 'instructions'; text: string } | null>(null);
  readonly versions = signal<TaskCodeResponse['versions']>([]);

  async openFor(id: string): Promise<void> {
    this.open.set(true);
    this.loading.set(true);
    this.errorMsg.set(null);
    this.current.set(null);
    this.versions.set([]);
    try {
      const data = await firstValueFrom(
        this.http.get<TaskCodeResponse>(`/tasks/${id}/code`, { params: { user_id: this.userSession.userId } }),
      );
      this.title.set(`[${id}] ${data.name || ''}`);
      if (data.task_code) {
        this.current.set({ kind: 'code', text: data.task_code });
      } else if (data.instructions) {
        this.current.set({ kind: 'instructions', text: data.instructions });
      }
      this.versions.set(data.versions || []);
    } catch (e: any) {
      this.errorMsg.set(e?.error?.message ?? 'Erro ao carregar código.');
    } finally {
      this.loading.set(false);
    }
  }

  close(): void {
    this.open.set(false);
  }

  async copy(): Promise<void> {
    const cur = this.current();
    if (!cur) return;
    await navigator.clipboard.writeText(cur.text);
  }
}
