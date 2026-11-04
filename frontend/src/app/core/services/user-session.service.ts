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
/**
 * Usuários demo para simular multi-tenant no front-end sem um sistema de
 * login real. Trocar de usuário grava o novo id no localStorage e recarrega
 * a página, reaproveitando o mecanismo acima (host/query string > localStorage).
 */
export const DEMO_USER_IDS = ['123', '321', '456'];

const TOKEN_KEY = 'mfg_access_token';
const RELOAD_FLAG = 'mfg_auth_reloaded';

@Injectable({ providedIn: 'root' })
export class UserSessionService {
  readonly userId: string;
  /**
   * JWT do usuário atual, só quando o backend tem o controle de acesso ativo (POC — ver auth.py).
   * Emitido por POST /auth/dev-login, que simula o login do mdsc-api-gateway. Vazio sem controle
   * de acesso: aí o app funciona como antes, só com o user_id.
   */
  get token(): string {
    return localStorage.getItem(TOKEN_KEY) ?? '';
  }
  private readonly _sessionId = signal<string>('');
  readonly sessionId = this._sessionId.asReadonly();

  constructor() {
    const hostUserId = new URLSearchParams(location.search).get('user_id');
    let userId = hostUserId || localStorage.getItem('mfg_user_id');
    if (!userId) {
      userId = DEMO_USER_IDS[0];
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

  /**
   * Obtém o token do usuário atual antes do app subir (APP_INITIALIZER). Usa fetch direto: o
   * HttpClient passa pelo interceptor, que já depende deste token. Se o backend não tem controle
   * de acesso ativo, não faz nada; se o usuário salvo não existe lá, cai no primeiro da lista.
   */
  async initAuth(): Promise<void> {
    try {
      const info = await (await fetch('/auth/users')).json();
      if (!info.enabled) {
        localStorage.removeItem(TOKEN_KEY);
        return;
      }
      const ids: string[] = info.users.map((u: { id: string }) => u.id);
      if (!ids.includes(this.userId)) {
        localStorage.setItem('mfg_user_id', ids[0]);
        location.reload();
        return;
      }
      const resp = await fetch('/auth/dev-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: this.userId }),
      });
      if (!resp.ok) throw new Error(`dev-login ${resp.status}`);
      localStorage.setItem(TOKEN_KEY, (await resp.json()).access_token);
      sessionStorage.removeItem(RELOAD_FLAG);
    } catch (err) {
      console.error('[auth] falha ao autenticar', err);
    }
  }

  /** Token recusado pelo backend (expirou ou o segredo mudou): descarta e recarrega uma vez para refazer o login. */
  handleUnauthorized(): void {
    localStorage.removeItem(TOKEN_KEY);
    if (sessionStorage.getItem(RELOAD_FLAG)) return;
    sessionStorage.setItem(RELOAD_FLAG, '1');
    location.reload();
  }

  /** Troca o usuário ativo (demo) e recarrega a página para reiniciar todos os serviços com o novo id. */
  switchUser(userId: string): void {
    if (userId === this.userId) return;
    localStorage.setItem('mfg_user_id', userId);
    location.reload();
  }
}
