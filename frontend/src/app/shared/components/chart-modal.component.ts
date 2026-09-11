import { Component, HostListener, inject } from '@angular/core';
import { ChartModalService } from '../../core/services/chart-modal.service';
import { TranslateService } from '../../core/services/translate.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

@Component({
  selector: 'app-chart-modal',
  imports: [TranslatePipe],
  template: `
    @if (modal.open()) {
      <div class="chart-modal-overlay" (click)="modal.close()">
        <div class="chart-modal-box" (click)="$event.stopPropagation()">
          <div class="chart-modal-header">
            <div class="chart-modal-title">
              <span>{{ modal.title() }}</span>
              <span class="chart-modal-hint">{{ 'chartModal.closeHint' | translate }}</span>
            </div>
            <button class="chart-modal-close" (click)="modal.close()">{{ 'chartModal.close' | translate }}</button>
          </div>
          <div class="chart-modal-body">
            <img [src]="modal.src()" [alt]="modal.title()" draggable="false" />
          </div>
        </div>
      </div>
    }
  `,
})
export class ChartModalComponent {
  readonly modal = inject(ChartModalService);
  private readonly translate = inject(TranslateService);

  @HostListener('document:keydown.escape')
  onEscape(): void {
    this.modal.close();
  }

  // Delegação global — porta dashboard.html:2407-2415: clique em qualquer
  // <img> dentro de .msg-bubble (chat) ou <canvas> dentro de .ch-wrap
  // (widgets, Fase 6) abre o zoom, sem precisar de binding por elemento
  // (o HTML das mensagens é renderizado via [innerHTML]/marked).
  @HostListener('document:click', ['$event'])
  onDocumentClick(e: MouseEvent): void {
    const target = e.target as HTMLElement;
    if (target.tagName === 'CANVAS' && target.closest('.ch-wrap')) {
      const canvas = target as HTMLCanvasElement;
      const title = canvas.closest('.card')?.querySelector('.card-title')?.textContent?.trim() ?? this.translate.t('chat.defaultChartTitle');
      this.modal.show(canvas.toDataURL('image/png'), title);
      return;
    }
    if (target.tagName === 'IMG' && target.closest('.msg-bubble')) {
      const img = target as HTMLImageElement;
      this.modal.show(img.src, img.alt || this.translate.t('chat.defaultAnalysisTitle'));
    }
  }
}
