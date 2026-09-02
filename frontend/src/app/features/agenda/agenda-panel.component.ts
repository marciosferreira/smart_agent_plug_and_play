import { Component, effect, inject } from '@angular/core';
import { AgendaService } from '../../core/services/agenda.service';
import { PanelStateService } from '../../core/services/panel-state.service';
import { ChatPanelStateService } from '../../core/services/chat-panel-state.service';
import { TaskCardComponent } from './task-card.component';

/**
 * Porta o slide-over de Agenda de dashboard.html:826-852,2749-2781.
 */
@Component({
  selector: 'app-agenda-panel',
  imports: [TaskCardComponent],
  templateUrl: './agenda-panel.component.html',
})
export class AgendaPanelComponent {
  readonly agenda = inject(AgendaService);
  readonly panelState = inject(PanelStateService);
  private readonly chatPanel = inject(ChatPanelStateService);

  constructor() {
    effect(() => {
      if (this.panelState.open() === 'agenda') {
        this.agenda.loadAgenda();
        this.agenda.markSeen();
      }
    });
  }

  newTaskViaChat(): void {
    this.panelState.close();
    this.chatPanel.open();
  }
}
