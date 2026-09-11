import { Component, effect, inject } from '@angular/core';
import { AlertsService } from '../../core/services/alerts.service';
import { PanelStateService } from '../../core/services/panel-state.service';
import { LocaleService } from '../../core/services/locale.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

/**
 * Porta o painel de Alertas de dashboard.html:807-824,3229-3326.
 */
@Component({
  selector: 'app-alerts-panel',
  imports: [TranslatePipe],
  templateUrl: './alerts-panel.component.html',
})
export class AlertsPanelComponent {
  readonly alerts = inject(AlertsService);
  readonly panelState = inject(PanelStateService);
  private readonly locale = inject(LocaleService);

  private autoReadTimer?: ReturnType<typeof setTimeout>;

  constructor() {
    effect(() => {
      if (this.panelState.open() === 'alerts') {
        this.alerts.loadAlerts();
        // Porta o auto "marcar todas lidas" após 3s aberto (dashboard.html:3242-3245).
        clearTimeout(this.autoReadTimer);
        this.autoReadTimer = setTimeout(() => this.alerts.markAllRead(), 3000);
      } else {
        clearTimeout(this.autoReadTimer);
      }
    });
  }

  fmtDate(iso: string): string {
    return new Date(iso).toLocaleString(this.locale.lang());
  }
}
