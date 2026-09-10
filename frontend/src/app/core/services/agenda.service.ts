import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { UserSessionService } from './user-session.service';
import { ScheduledTask } from '../models/task.model';

const POLL_MS = 15000;
const SEEN_IDS_KEY = 'agendaSeenIds';

function readSeenIds(): string[] {
  try {
    return JSON.parse(localStorage.getItem(SEEN_IDS_KEY) || '[]');
  } catch {
    return [];
  }
}

/**
 * Porta o painel de Agenda/Tarefas de dashboard.html:826-852,2587-2781.
 */
@Injectable({ providedIn: 'root' })
export class AgendaService {
  private readonly http = inject(HttpClient);
  private readonly userSession = inject(UserSessionService);

  private readonly _tasks = signal<ScheduledTask[]>([]);
  readonly loading = signal(false);
  private readonly _seenIds = signal<Set<string>>(new Set(readSeenIds()));

  // 'draft' = registro criado mas ainda sem task_code salvo (o agente ainda está
  // gerando/testando o código) — fica oculto até virar 'active', pra não mostrar
  // uma tarefa "fantasma" que ainda não pode ser executada.
  readonly activeTasks = computed(() =>
    this._tasks().filter((t) => t.status !== 'cancelled' && t.status !== 'draft'),
  );
  readonly newCount = computed(() => {
    const seen = this._seenIds();
    return this._tasks().filter((t) => t.status === 'active' && !seen.has(String(t.id))).length;
  });
  readonly activeCount = computed(() => this._tasks().filter((t) => t.status === 'active').length);

  private readonly taskPolls = new Map<string, ReturnType<typeof setTimeout>>();

  constructor() {
    this.refresh();
    setInterval(() => this.refresh(), POLL_MS);
  }

  isNew(task: ScheduledTask): boolean {
    return !this._seenIds().has(String(task.id));
  }

  private async refresh(): Promise<void> {
    try {
      const tasks = await firstValueFrom(
        this.http.get<ScheduledTask[]>('/tasks', { params: { user_id: this.userSession.userId } }),
      );
      this._tasks.set(tasks || []);
    } catch {
      /* mantém o último estado conhecido */
    }
  }

  async loadAgenda(): Promise<void> {
    this.loading.set(true);
    await this.refresh();
    this.loading.set(false);
  }

  /** Marca as tarefas ativas atuais como "vistas" — porta openAgenda's setTimeout(...,1500) de dashboard.html:2754-2770. */
  markSeen(): void {
    setTimeout(async () => {
      await this.refresh();
      const activeIds = this._tasks()
        .filter((t) => t.status === 'active')
        .map((t) => String(t.id));
      localStorage.setItem(SEEN_IDS_KEY, JSON.stringify(activeIds));
      this._seenIds.set(new Set(activeIds));
    }, 1500);
  }

  async runTaskNow(id: string): Promise<{ ok: boolean; message?: string }> {
    try {
      const data = await firstValueFrom(
        this.http.post<{ ok?: boolean; message?: string }>(`/tasks/${id}/run-now`, null, {
          params: { user_id: this.userSession.userId },
        }),
      );
      setTimeout(() => {
        this.refresh();
        this.pollTaskUntilDone(id);
      }, 1500);
      return { ok: true, message: data?.message };
    } catch (e: any) {
      return { ok: false, message: e?.error?.message ?? 'Erro ao executar tarefa.' };
    }
  }

  private pollTaskUntilDone(id: string): void {
    if (this.taskPolls.has(id)) return;
    const tick = async () => {
      try {
        await this.refresh();
        const task = this._tasks().find((t) => String(t.id) === String(id));
        const stillRunning = !!task && (task.status === 'running' || (task.runs || []).some((r) => r.status === 'running'));
        if (stillRunning) {
          this.taskPolls.set(id, setTimeout(tick, 3000));
        } else {
          this.taskPolls.delete(id);
        }
      } catch {
        this.taskPolls.delete(id);
      }
    };
    this.taskPolls.set(id, setTimeout(tick, 3000));
  }

  async deleteTask(id: string): Promise<void> {
    await firstValueFrom(this.http.delete(`/tasks/${id}`, { params: { user_id: this.userSession.userId } }));
    await this.refresh();
  }

  async toggleNotify(id: string): Promise<{ ok: boolean; message?: string }> {
    try {
      const data = await firstValueFrom(
        this.http.post<{ ok: boolean; message?: string }>(`/tasks/${id}/toggle-notify`, null, {
          params: { user_id: this.userSession.userId },
        }),
      );
      if (data.ok) await this.refresh();
      return data;
    } catch {
      return { ok: false };
    }
  }

  async togglePause(id: string): Promise<{ ok: boolean; message?: string }> {
    try {
      const data = await firstValueFrom(
        this.http.post<{ ok: boolean; message?: string }>(`/tasks/${id}/toggle-pause`, null, {
          params: { user_id: this.userSession.userId },
        }),
      );
      if (data.ok) await this.refresh();
      return data;
    } catch {
      return { ok: false };
    }
  }

  get tasks() {
    return this._tasks.asReadonly();
  }
}
