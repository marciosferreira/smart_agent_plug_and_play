import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { UserSessionService } from './user-session.service';
import { AppAlert } from '../models/alert.model';

const POLL_MS = 15000;

/**
 * Porta o painel de Alertas de dashboard.html:807-824,3227-3345.
 */
@Injectable({ providedIn: 'root' })
export class AlertsService {
  private readonly http = inject(HttpClient);
  private readonly userSession = inject(UserSessionService);

  readonly alerts = signal<AppAlert[]>([]);
  readonly loading = signal(false);
  readonly unreadCount = signal(0);

  constructor() {
    this.updateBadge();
    setInterval(() => this.updateBadge(), POLL_MS);
  }

  async loadAlerts(): Promise<void> {
    this.loading.set(true);
    try {
      const alerts = await firstValueFrom(
        this.http.get<AppAlert[]>('/alerts', { params: { all: 'true', user_id: this.userSession.userId } }),
      );
      this.alerts.set(alerts || []);
    } catch {
      this.alerts.set([]);
    } finally {
      this.loading.set(false);
    }
  }

  async updateBadge(): Promise<void> {
    try {
      const unread = await firstValueFrom(
        this.http.get<AppAlert[]>('/alerts', { params: { user_id: this.userSession.userId } }),
      );
      this.unreadCount.set(unread?.length ?? 0);
    } catch {
      /* mantém o último valor conhecido */
    }
  }

  async markAllRead(): Promise<void> {
    await firstValueFrom(this.http.post(`/alerts/read-all`, {}, { params: { user_id: this.userSession.userId } }));
    await this.loadAlerts();
    await this.updateBadge();
  }

  async deleteAll(): Promise<void> {
    await firstValueFrom(this.http.delete(`/alerts`, { params: { user_id: this.userSession.userId } }));
    this.alerts.set([]);
    await this.updateBadge();
  }

  async deleteOne(id: string): Promise<void> {
    await firstValueFrom(this.http.delete(`/alerts/${id}`, { params: { user_id: this.userSession.userId } }));
    this.alerts.update((list) => list.filter((a) => a.id !== id));
    await this.updateBadge();
  }
}
