import { Component, effect, inject } from '@angular/core';
import { AuditService, AuditSort } from '../../core/services/audit.service';
import { PanelStateService } from '../../core/services/panel-state.service';
import { AuditEntryCardComponent } from './audit-entry-card.component';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

/**
 * Porta o slide-over de Error Audit de dashboard.html:854-906,2788-2969.
 */
@Component({
  selector: 'app-audit-panel',
  imports: [AuditEntryCardComponent, TranslatePipe],
  templateUrl: './audit-panel.component.html',
})
export class AuditPanelComponent {
  readonly audit = inject(AuditService);
  readonly panelState = inject(PanelStateService);

  constructor() {
    effect(() => {
      if (this.panelState.open() === 'audit' && this.audit.entries().length === 0) {
        this.audit.load();
      }
    });
  }

  onFilterInput(value: string): void {
    this.audit.filter.set(value);
  }

  onSortChange(value: string): void {
    this.audit.sort.set(value as AuditSort);
  }
}
