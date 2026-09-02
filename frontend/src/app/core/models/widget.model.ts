export interface DashboardWidget {
  id: string;
  title: string;
  description: string | null;
  created_at: string;
  range_unit: 'days' | 'months';
  range_value: number;
}

export interface WidgetChartConfig {
  type: string;
  data: unknown;
  options?: Record<string, unknown>;
}
