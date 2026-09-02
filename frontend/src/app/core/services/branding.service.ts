import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

export interface AppInfo {
  nome: string;
  descricao: string;
  configurado: boolean;
}

/**
 * Porta loadAppInfo() de dashboard.html:1175-1184.
 */
@Injectable({ providedIn: 'root' })
export class BrandingService {
  private readonly http = inject(HttpClient);

  private readonly _info = signal<AppInfo | null>(null);
  readonly info = this._info.asReadonly();

  async load(): Promise<void> {
    try {
      const info = await firstValueFrom(this.http.get<AppInfo>('/app-info'));
      this._info.set(info);
      if (info.nome) document.title = info.nome;
    } catch (e) {
      console.error('app-info:', e);
    }
  }
}
