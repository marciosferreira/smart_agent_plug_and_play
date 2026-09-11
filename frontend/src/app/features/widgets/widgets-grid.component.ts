import { Component, inject, signal } from '@angular/core';
import { WidgetsService } from '../../core/services/widgets.service';
import { WidgetCardComponent } from './widget-card.component';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

/**
 * Porta a seção "Painéis Customizados" de dashboard.html:981-991,1224-1328.
 */
@Component({
  selector: 'app-widgets-grid',
  imports: [WidgetCardComponent, TranslatePipe],
  templateUrl: './widgets-grid.component.html',
})
export class WidgetsGridComponent {
  readonly widgets = inject(WidgetsService);

  readonly dragSrcId = signal<string | null>(null);
  readonly dragOverId = signal<string | null>(null);

  constructor() {
    this.widgets.load();
  }

  onDragStart(id: string, ev: DragEvent): void {
    this.dragSrcId.set(id);
    ev.dataTransfer?.setData('text/plain', id);
    if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move';
  }

  onDragOver(id: string, ev: DragEvent): void {
    ev.preventDefault();
    if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
    if (this.dragSrcId() && this.dragSrcId() !== id) this.dragOverId.set(id);
  }

  onDragLeave(id: string): void {
    if (this.dragOverId() === id) this.dragOverId.set(null);
  }

  onDrop(id: string, ev: DragEvent): void {
    ev.preventDefault();
    this.dragOverId.set(null);
    const src = this.dragSrcId();
    if (src && src !== id) this.widgets.reorder(src, id);
  }

  onDragEnd(): void {
    this.dragSrcId.set(null);
    this.dragOverId.set(null);
  }
}
