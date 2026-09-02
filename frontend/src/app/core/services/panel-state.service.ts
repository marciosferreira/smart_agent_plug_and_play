import { Injectable, signal } from '@angular/core';

export type PanelName = 'alerts' | 'agenda' | 'artifacts' | 'audit';

/**
 * Registro de qual painel lateral (slide-over) está aberto — Alertas, Agenda,
 * Artefatos, Auditoria (Fases 3-5). Só um por vez, como na prática do
 * dashboard.html original (toggleAlertsPanel/openAgenda/openArtifacts/openAudit).
 */
@Injectable({ providedIn: 'root' })
export class PanelStateService {
  private readonly _open = signal<PanelName | null>(null);
  readonly open = this._open.asReadonly();

  toggle(name: PanelName): void {
    this._open.update((cur) => (cur === name ? null : name));
  }

  close(): void {
    this._open.set(null);
  }
}
