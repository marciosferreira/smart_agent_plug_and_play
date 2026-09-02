import { Injectable, signal } from '@angular/core';

/**
 * Estado do modal de zoom de imagem/gráfico (dashboard.html:1059-1071,2376-2401).
 * Usado pelas imagens do chat (Fase 1) e reaproveitado pelos canvases da
 * grid de widgets (Fase 6).
 */
@Injectable({ providedIn: 'root' })
export class ChartModalService {
  private readonly _open = signal(false);
  readonly open = this._open.asReadonly();

  private readonly _title = signal('Gráfico');
  readonly title = this._title.asReadonly();

  private readonly _src = signal('');
  readonly src = this._src.asReadonly();

  show(src: string, title = 'Análise'): void {
    this._src.set(src);
    this._title.set(title);
    this._open.set(true);
  }

  close(): void {
    this._open.set(false);
  }
}
