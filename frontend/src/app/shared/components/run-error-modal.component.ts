import { Component, HostListener, inject, signal } from '@angular/core';
import { RunErrorModalService } from '../../core/services/run-error-modal.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

@Component({
  selector: 'app-run-error-modal',
  imports: [TranslatePipe],
  template: `
    @if (modal.open()) {
      <div style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:2000;backdrop-filter:blur(3px);" (click)="modal.close()"></div>
      <div style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2001;background:#ffffff;border:1px solid #fecaca;border-radius:12px;width:min(680px,92vw);max-height:80vh;display:flex;flex-direction:column;box-shadow:0 25px 60px rgba(0,0,0,.6);">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid #e2e8f0;">
          <span style="font-size:12px;font-weight:600;color:#f87171;font-family:'JetBrains Mono',monospace;letter-spacing:.05em;">{{ 'runErrorModal.header' | translate }}</span>
          <div style="display:flex;gap:8px;align-items:center;">
            <button (click)="copy()" style="font-size:11px;padding:4px 10px;background:#e2e8f0;border:1px solid #94a3b8;border-radius:6px;color:#94a3b8;cursor:pointer;">{{ (copied() ? 'runErrorModal.copied' : 'runErrorModal.copy') | translate }}</button>
            <button (click)="modal.close()" style="background:none;border:none;color:#64748b;font-size:18px;cursor:pointer;line-height:1;">✕</button>
          </div>
        </div>
        <pre style="flex:1;overflow:auto;margin:0;padding:18px;font-size:12px;font-family:'JetBrains Mono',monospace;color:#fca5a5;line-height:1.6;white-space:pre-wrap;word-break:break-word;background:#f8fafc;border-radius:0 0 12px 12px;">{{ modal.text() }}</pre>
      </div>
    }
  `,
})
export class RunErrorModalComponent {
  readonly modal = inject(RunErrorModalService);
  readonly copied = signal(false);

  @HostListener('document:keydown.escape')
  onEscape(): void {
    this.modal.close();
  }

  async copy(): Promise<void> {
    await navigator.clipboard.writeText(this.modal.text());
    this.copied.set(true);
    setTimeout(() => this.copied.set(false), 2000);
  }
}
