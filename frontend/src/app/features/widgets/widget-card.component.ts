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
import { TranslateService } from '../../core/services/translate.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

/** Chave i18n ("chartType.<tipo>") de cada tipo Chart.js — exibido nos painéis padrão como demo dos tipos de gráfico disponíveis. */
const CHART_TYPE_KEYS: Record<string, string> = {
  bar: 'chartType.bar',
  line: 'chartType.line',
  pie: 'chartType.pie',
  doughnut: 'chartType.doughnut',
  radar: 'chartType.radar',
  polarArea: 'chartType.polarArea',
  scatter: 'chartType.scatter',
  bubble: 'chartType.bubble',
  matrix: 'chartType.matrix',
};

@Component({
  selector: 'app-widget-card',
  imports: [TranslatePipe],
  templateUrl: './widget-card.component.html',
})
export class WidgetCardComponent implements AfterViewInit, OnDestroy {
  readonly widgets = inject(WidgetsService);
  private readonly translate = inject(TranslateService);

  @Input({ required: true }) widget!: DashboardWidget;
  @Input() dragOver = false;

  private readonly canvasRef = viewChild<ElementRef<HTMLCanvasElement>>('canvas');

  readonly error = computed(() => this.widgets.errors()[this.widget.id]);
  readonly rangeLabel = computed(() => this.translate.t('widgetCard.rangeLabel', {
    n: this.widget.range_value,
    unit: this.translate.t(this.widget.range_unit === 'months' ? 'widgetCard.unitMonths' : 'widgetCard.unitDays'),
  }));
  readonly chartTypeLabel = computed(() => {
    const type = this.widgets.chartTypes()[this.widget.id];
    if (!type) return null;
    const key = CHART_TYPE_KEYS[type];
    return key ? this.translate.t(key) : type;
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
