import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { LocaleService } from './locale.service';
import { TranslateService } from './translate.service';

export type ApiStatus = 'connecting' | 'online' | 'offline';

/**
 * Porta setApiOnline/setApiOffline/refreshAll de dashboard.html:1121-1132,1186-1200.
 *
 * O original usa loadCustomWidgets() como sonda de saúde da API dentro de
 * refreshAll(); como a grid de widgets só existe a partir da Fase 6, aqui a
 * sonda é GET /app-info (mesmo endpoint da branding) — TODO(Fase 6): trocar
 * para recarregar os widgets de verdade quando a grid existir.
 */
@Injectable({ providedIn: 'root' })
export class ApiStatusService {
  private readonly http = inject(HttpClient);
  private readonly locale = inject(LocaleService);
  private readonly translate = inject(TranslateService);

  private readonly _status = signal<ApiStatus>('connecting');
  readonly status = this._status.asReadonly();

  private readonly _refreshing = signal(false);
  readonly refreshing = this._refreshing.asReadonly();

  private readonly _lastUpdate = signal<string | null>(null);
  readonly lastUpdate = this._lastUpdate.asReadonly();

  async refreshAll(): Promise<void> {
    this._refreshing.set(true);
    try {
      await firstValueFrom(this.http.get('/app-info'));
      this._status.set('online');
    } catch (e) {
      this._status.set('offline');
      console.error('refreshAll:', e);
    }
    const time = new Date().toLocaleTimeString(this.locale.lang(), { hour12: false });
    this._lastUpdate.set(this.translate.t('header.updatedAt', { time }));
    this._refreshing.set(false);
  }
}
