import { Component, ElementRef, HostListener, effect, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ChatService } from '../../core/services/chat.service';
import { ChatPanelStateService } from '../../core/services/chat-panel-state.service';
import { ScreenshotService } from '../../core/services/screenshot.service';
import { VoiceRecorderService } from '../../core/services/voice-recorder.service';
import { ChartModalService } from '../../core/services/chart-modal.service';
import { ChatMessageComponent } from './chat-message.component';
import { ChatHistoryListComponent } from './chat-history-list.component';

const MIN_WIDTH = 200;

@Component({
  selector: 'app-chat-sidebar',
  imports: [FormsModule, ChatMessageComponent, ChatHistoryListComponent],
  templateUrl: './chat-sidebar.component.html',
})
export class ChatSidebarComponent {
  readonly chat = inject(ChatService);
  readonly chatPanel = inject(ChatPanelStateService);
  readonly screenshot = inject(ScreenshotService);
  readonly voice = inject(VoiceRecorderService);
  private readonly modal = inject(ChartModalService);

  readonly text = signal('');
  readonly transcribing = signal(false);
  readonly sidebarEl = viewChild<ElementRef<HTMLElement>>('sidebar');
  private readonly msgsEl = viewChild<ElementRef<HTMLElement>>('chatMsgs');

  private dragging = false;
  private dragStartX = 0;
  private dragStartW = 0;
  readonly handleActive = signal(false);

  constructor() {
    effect(() => {
      this.chat.messages();
      queueMicrotask(() => {
        const el = this.msgsEl()?.nativeElement;
        if (el) el.scrollTop = el.scrollHeight;
      });
    });
    this.chat.loadChatHistory();
  }

  onInput(e: Event): void {
    const el = e.target as HTMLTextAreaElement;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  }

  onKeydown(e: KeyboardEvent): void {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      this.handleSend(e.target as HTMLTextAreaElement);
    }
  }

  async handleSend(textarea?: HTMLTextAreaElement): Promise<void> {
    if (this.voice.hasAudio()) {
      await this.sendAudioFlow(textarea);
      return;
    }
    const value = this.text().trim();
    this.text.set('');
    if (textarea) textarea.style.height = 'auto';
    await this.chat.sendChat(value);
  }

  private async sendAudioFlow(textarea?: HTMLTextAreaElement): Promise<void> {
    const blob = this.voice.getBlob();
    if (!blob) return;
    this.transcribing.set(true);
    try {
      const transcript = await this.chat.sendAudio(blob);
      this.voice.discard();
      this.transcribing.set(false);
      this.text.set(transcript);
      if (textarea) textarea.style.height = 'auto';
      await this.chat.sendChat(transcript);
    } catch (e) {
      this.transcribing.set(false);
      this.chat.messages.update((list) => [
        ...list,
        { kind: 'text', id: `err${Date.now()}`, role: 'error', html: 'Falha ao transcrever: ' + (e as Error).message, time: '' },
      ]);
    }
  }

  async toggleRecording(): Promise<void> {
    try {
      await this.voice.toggle();
    } catch (e) {
      this.chat.messages.update((list) => [
        ...list,
        { kind: 'text', id: `err${Date.now()}`, role: 'error', html: (e as Error).message, time: '' },
      ]);
    }
  }

  openImagePreview(b64: string): void {
    this.modal.show('data:image/png;base64,' + b64, 'Imagem anexada');
  }

  close(): void {
    this.chatPanel.close(this.sidebarEl()?.nativeElement.offsetWidth);
  }

  // ── Resize handle ────────────────────────────────────
  onHandleMouseDown(e: MouseEvent): void {
    this.beginDrag(e.clientX);
    e.preventDefault();
  }
  onHandleTouchStart(e: TouchEvent): void {
    this.beginDrag(e.touches[0].clientX);
    e.preventDefault();
  }
  private beginDrag(clientX: number): void {
    this.dragging = true;
    this.dragStartX = clientX;
    this.dragStartW = this.sidebarEl()?.nativeElement.offsetWidth ?? 360;
    this.handleActive.set(true);
    document.body.style.userSelect = 'none';
  }

  @HostListener('document:mousemove', ['$event'])
  onDocMouseMove(e: MouseEvent): void {
    this.dragMove(e.clientX);
  }
  @HostListener('document:mouseup')
  onDocMouseUp(): void {
    this.endDrag();
  }
  @HostListener('document:touchmove', ['$event'])
  onDocTouchMove(e: TouchEvent): void {
    if (!this.dragging) return;
    this.dragMove(e.touches[0].clientX);
    e.preventDefault();
  }
  @HostListener('document:touchend')
  onDocTouchEnd(): void {
    this.endDrag();
  }

  private dragMove(clientX: number): void {
    if (!this.dragging) return;
    const delta = this.dragStartX - clientX;
    const maxW = window.innerWidth - 60;
    const newW = Math.min(Math.max(this.dragStartW + delta, MIN_WIDTH), maxW);
    this.chatPanel.setDragWidth(newW);
  }

  private endDrag(): void {
    if (!this.dragging) return;
    this.dragging = false;
    this.handleActive.set(false);
    document.body.style.userSelect = '';
  }
}
