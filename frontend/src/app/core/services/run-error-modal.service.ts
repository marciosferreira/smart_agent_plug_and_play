import { Injectable, signal } from '@angular/core';

/** Porta o modal de erro de execução de dashboard.html:1691-1713. */
@Injectable({ providedIn: 'root' })
export class RunErrorModalService {
  private readonly _open = signal(false);
  readonly open = this._open.asReadonly();

  private readonly _text = signal('');
  readonly text = this._text.asReadonly();

  show(text: string): void {
    this._text.set(text);
    this._open.set(true);
  }

  close(): void {
    this._open.set(false);
  }
}
