import {
  AfterViewInit,
  Component,
  ElementRef,
  Input,
  OnDestroy,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { WidgetsService } from '../../core/services/widgets.service';
import { DashboardWidget } from '../../core/models/widget.model';

@Component({
  selector: 'app-widget-card',
  templateUrl: './widget-card.component.html',
})
export class WidgetCardComponent implements AfterViewInit, OnDestroy {
  readonly widgets = inject(WidgetsService);

  @Input({ required: true }) widget!: DashboardWidget;
  @Input() dragOver = false;

  private readonly canvasRef = viewChild<ElementRef<HTMLCanvasElement>>('canvas');

  readonly error = computed(() => this.widgets.errors()[this.widget.id]);
  readonly rangeLabel = computed(() => `últimos ${this.widget.range_value} ${this.widget.range_unit === 'months' ? 'meses' : 'dias'}`);

  ngAfterViewInit(): void {
    const canvas = this.canvasRef()?.nativeElement;
    if (canvas) this.widgets.registerCanvas(this.widget.id, canvas);
  }

  ngOnDestroy(): void {
    this.widgets.unregisterCanvas(this.widget.id);
  }

  removeWidget(): void {
    this.widgets.removeWidget(this.widget.id);
  }
}
