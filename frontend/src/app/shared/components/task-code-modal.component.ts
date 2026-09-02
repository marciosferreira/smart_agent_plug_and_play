import { Component, HostListener, inject, signal } from '@angular/core';
import { TaskCodeModalService } from '../../core/services/task-code-modal.service';

@Component({
  selector: 'app-task-code-modal',
  template: `
    @if (modal.open()) {
      <div style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:2000;backdrop-filter:blur(3px);" (click)="modal.close()"></div>
      <div style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2001;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;width:min(760px,95vw);max-height:85vh;display:flex;flex-direction:column;box-shadow:0 25px 60px rgba(0,0,0,.6);">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid #e2e8f0;flex-shrink:0;">
          <div>
            <span style="font-size:10px;font-family:'JetBrains Mono',monospace;letter-spacing:.1em;color:#6366f1;">CÓDIGO DA TAREFA</span>
            <span style="margin-left:10px;font-size:13px;font-weight:600;color:#1e293b;">{{ modal.title() }}</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button (click)="copy()" style="font-size:11px;padding:4px 10px;background:#e2e8f0;border:1px solid #94a3b8;border-radius:6px;color:#94a3b8;cursor:pointer;">{{ copied() ? 'Copiado ✓' : 'Copiar' }}</button>
            <button (click)="modal.close()" style="background:none;border:none;color:#64748b;font-size:18px;cursor:pointer;line-height:1;">✕</button>
          </div>
        </div>
        <div style="flex:1;overflow:auto;display:flex;flex-direction:column;">
          <div style="flex:1;overflow:auto;">
            @if (modal.loading()) {
              <div style="padding:32px;text-align:center;color:#475569;font-size:13px;">Carregando…</div>
            } @else if (modal.errorMsg()) {
              <div style="padding:20px;color:#f87171;font-size:12px;">{{ modal.errorMsg() }}</div>
            } @else if (modal.current()) {
              @let cur = modal.current()!;
              @if (cur.kind === 'code') {
                <div style="font-size:9px;color:#475569;font-family:'JetBrains Mono',monospace;letter-spacing:.1em;padding:10px 18px 6px;">CÓDIGO ATUAL</div>
                <pre style="margin:0;padding:0 18px 18px;font-size:12px;font-family:'JetBrains Mono',monospace;color:#1e293b;line-height:1.65;white-space:pre;overflow:auto;background:#f8fafc;">{{ cur.text }}</pre>
              } @else {
                <div style="font-size:9px;color:#475569;font-family:'JetBrains Mono',monospace;letter-spacing:.1em;padding:10px 18px 6px;">INSTRUÇÕES (modo LLM)</div>
                <pre style="margin:0;padding:0 18px 18px;font-size:12px;font-family:'JetBrains Mono',monospace;color:#94a3b8;line-height:1.65;white-space:pre-wrap;word-break:break-word;background:#f8fafc;">{{ cur.text }}</pre>
              }
            } @else {
              <div style="padding:32px;text-align:center;color:#475569;font-size:12px;">Nenhum código definido para esta tarefa.</div>
            }
          </div>
          @if (modal.versions().length) {
            <div style="border-top:1px solid #e2e8f0;padding:12px 18px;flex-shrink:0;">
              <div style="font-size:9px;color:#475569;font-family:'JetBrains Mono',monospace;letter-spacing:.1em;margin-bottom:10px;">VERSÕES ANTERIORES ({{ modal.versions().length }})</div>
              <div style="max-height:220px;overflow-y:auto;">
                @for (v of modal.versions(); track v.version) {
                  <div style="margin-bottom:8px;">
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                      <span style="font-size:10px;font-family:'JetBrains Mono',monospace;color:#6366f1;">v{{ v.version }}</span>
                      <span style="font-size:10px;color:#475569;">{{ v.created_at.slice(0, 16).replace('T', ' ') }}</span>
                    </div>
                    <pre style="margin:0;padding:8px 12px;font-size:11px;font-family:'JetBrains Mono',monospace;color:#64748b;line-height:1.55;white-space:pre;overflow:auto;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;max-height:140px;">{{ v.code }}</pre>
                  </div>
                }
              </div>
            </div>
          }
        </div>
      </div>
    }
  `,
})
export class TaskCodeModalComponent {
  readonly modal = inject(TaskCodeModalService);
  readonly copied = signal(false);

  @HostListener('document:keydown.escape')
  onEscape(): void {
    this.modal.close();
  }

  async copy(): Promise<void> {
    await this.modal.copy();
    this.copied.set(true);
    setTimeout(() => this.copied.set(false), 2000);
  }
}
