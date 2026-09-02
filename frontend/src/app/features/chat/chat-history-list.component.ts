import { Component, inject } from '@angular/core';
import { ChatService } from '../../core/services/chat.service';

@Component({
  selector: 'app-chat-history-list',
  template: `
    <div class="chat-hist-panel" [class.open]="chat.historyOpen()">
      <div class="chat-hist-head">
        <span class="chat-hist-label">CONVERSAS</span>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="chi-sel-btn" [class.active]="chat.selectionMode()" (click)="chat.toggleSelectionMode()">
            {{ chat.selectionMode() ? 'CANCELAR' : 'SELECIONAR' }}
          </button>
          <button class="chat-hist-close" (click)="chat.hideHistory()">✕</button>
        </div>
      </div>

      <div class="chat-hist-sel-bar" [class.hidden]="!chat.selectionMode()">
        <span class="chat-hist-sel-count">
          {{ chat.selectedSessionIds().size === 0 ? 'Nenhum selecionado' : chat.selectedSessionIds().size + ' selecionado' + (chat.selectedSessionIds().size !== 1 ? 's' : '') }}
        </span>
        <button class="chat-hist-sel-all" (click)="chat.toggleSelectAll()">TODOS</button>
        <button class="chat-hist-del-sel" [disabled]="chat.selectedSessionIds().size === 0" (click)="chat.deleteSelectedSessions()">
          DELETAR
        </button>
      </div>

      <div class="chat-hist-list" [class.sel-mode]="chat.selectionMode()">
        <button class="chat-hist-new" (click)="newChat()">+ Nova conversa</button>

        @if (!chat.sessions().length) {
          <div class="chat-hist-empty">Nenhuma conversa anterior.</div>
        }

        @for (s of chat.sessions(); track s.session_id) {
          <div
            class="chat-hist-item"
            [class.active]="s.session_id === chat.sessionId"
            (click)="onItemClick(s.session_id)"
          >
            <input
              type="checkbox"
              class="chi-checkbox"
              [checked]="chat.selectedSessionIds().has(s.session_id)"
              (click)="$event.stopPropagation()"
              (change)="chat.toggleSessionSelected(s.session_id)"
            />
            <div class="chi-info">
              <div class="chi-title">{{ s.title }}</div>
              <div class="chi-meta">{{ s.message_count }} mensagem{{ s.message_count !== 1 ? 's' : '' }}</div>
            </div>
            <button class="chi-del" title="Deletar conversa" (click)="onDeleteClick($event, s.session_id)">✕</button>
          </div>
        }
      </div>
    </div>
  `,
})
export class ChatHistoryListComponent {
  readonly chat = inject(ChatService);

  newChat(): void {
    this.chat.newChat();
    this.chat.hideHistory();
  }

  onItemClick(sessionId: string): void {
    if (this.chat.selectionMode()) {
      this.chat.toggleSessionSelected(sessionId);
    } else {
      this.chat.switchSession(sessionId);
    }
  }

  onDeleteClick(e: Event, sessionId: string): void {
    e.stopPropagation();
    this.chat.deleteSession(sessionId);
  }
}
