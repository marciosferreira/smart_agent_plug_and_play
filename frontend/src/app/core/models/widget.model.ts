export interface DashboardWidget {
  id: string;
  title: string;
  description: string | null;
  created_at: string | null;
  range_unit: 'days' | 'months';
  range_value: number;
  is_default: boolean;
  /** Visível a todos os usuários do domínio, não só ao dono. Sempre false para is_default. */
  shared: boolean;
  /** Dono deste painel (ou dono de um legado sem user_id) — só quem tem is_owner pode editar/apagar/compartilhar. Sempre false para is_default. */
  is_owner: boolean;
}

export interface WidgetChartConfig {
  type: string;
  data: unknown;
  options?: Record<string, unknown>;
}

/** Shape de `data` quando `type === 'matrix'` (heatmap) — ver WidgetsService.buildMatrixChart. */
export interface MatrixWidgetData {
  points: { x: string; y: string; v: number | null }[];
  xLabels: string[];
  yLabels: string[];
  label?: string;
}
