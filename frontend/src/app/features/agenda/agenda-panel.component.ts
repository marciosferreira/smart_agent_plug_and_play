import { Component, effect, inject } from '@angular/core';
import { AgendaService } from '../../core/services/agenda.service';
import { PanelStateService } from '../../core/services/panel-state.service';
import { ChatPanelStateService } from '../../core/services/chat-panel-state.service';
import { ChatService } from '../../core/services/chat.service';
import { TaskCardComponent } from './task-card.component';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

/**
 * Porta o slide-over de Agenda de dashboard.html:826-852,2749-2781.
 */
@Component({
  selector: 'app-agenda-panel',
  imports: [TaskCardComponent, TranslatePipe],
  templateUrl: './agenda-panel.component.html',
})
export class AgendaPanelComponent {
  readonly agenda = inject(AgendaService);
  readonly panelState = inject(PanelStateService);
  private readonly chatPanel = inject(ChatPanelStateService);
  private readonly chat = inject(ChatService);

  constructor() {
    effect(() => {
      if (this.panelState.open() === 'agenda') {
        this.agenda.loadAgenda();
        this.agenda.markSeen();
      }
    });
  }

  async newTaskViaChat(): Promise<void> {
    this.panelState.close();
    await this.chat.startTaskCreationChat();
    this.chatPanel.open();
  }
}
