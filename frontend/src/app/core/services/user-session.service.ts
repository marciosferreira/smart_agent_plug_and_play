import { Injectable, signal } from '@angular/core';

/**
 * Porta dashboard.html:1406-1417.
 *
 * _userId identifica o usuário perante o backend (separa histórico, tarefas,
 * painéis e artefatos por usuário). Quando este app está plugado num app host
 * que já tem controle de usuários, o host informa o user_id via query string
 * (?user_id=...) ao carregar a página — esse valor é sempre a fonte da verdade
 * e sobrescreve qualquer valor salvo antes. Sem host (uso standalone/demo),
 * cai de volta para um UUID gerado no browser e persistido em localStorage.
 */
@Injectable({ providedIn: 'root' })
export class UserSessionService {
  readonly userId: string;
  private readonly _sessionId = signal<string>('');
  readonly sessionId = this._sessionId.asReadonly();

  constructor() {
    const hostUserId = new URLSearchParams(location.search).get('user_id');
    let userId = hostUserId || localStorage.getItem('mfg_user_id');
    if (!userId) {
      userId = crypto.randomUUID();
    }
    localStorage.setItem('mfg_user_id', userId);
    this.userId = userId;

    const savedSessionId = localStorage.getItem(this.sessionKey());
    this.setSession(savedSessionId || `${this.userId}_${Date.now()}`);
  }

  sessionKey(): string {
    return `mfg_session_id::${this.userId}`;
  }

  setSession(id: string): void {
    this._sessionId.set(id);
    localStorage.setItem(this.sessionKey(), id);
  }

  newSessionId(): string {
    return `${this.userId}_${Date.now()}`;
  }
}
