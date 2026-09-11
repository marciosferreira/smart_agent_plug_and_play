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

/** Nome amigável (pt-BR) de cada tipo Chart.js — exibido nos painéis padrão como demo dos tipos de gráfico disponíveis. */
const CHART_TYPE_LABELS: Record<string, string> = {
  bar: 'barra',
  line: 'linha',
  pie: 'pizza',
  doughnut: 'rosca',
  radar: 'radar',
  polarArea: 'área polar',
  scatter: 'dispersão',
  bubble: 'bolhas',
  matrix: 'mapa de calor',
};

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
  readonly chartTypeLabel = computed(() => {
    const type = this.widgets.chartTypes()[this.widget.id];
    return type ? CHART_TYPE_LABELS[type] || type : null;
  });

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

  toggleShare(): void {
    this.widgets.toggleShare(this.widget.id, !this.widget.shared);
  }
}
