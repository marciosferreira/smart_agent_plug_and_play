export interface TaskRun {
  started_at: string | null;
  ended_at: string | null;
  status: 'success' | 'error' | 'running' | string;
  error: string | null;
}

export interface ScheduledTask {
  id: string;
  name: string;
  description: string;
  status: 'active' | 'running' | 'error' | 'completed' | 'cancelled' | 'paused' | 'pending_approval' | string;
  frequency: string;
  time: string | null;
  weekday: string | null;
  day: number | null;
  date_range: string | null;
  next_run: string | null;
  retry_count: number;
  max_retries: number;
  notify: boolean;
  condition_sql: string | null;
  condition_operator: string | null;
  condition_threshold: number | null;
  condition_state: boolean;
  condition_description: string | null;
  email: string | null;
  runs?: TaskRun[];
}

export interface TaskCodeVersion {
  version: number;
  created_at: string;
  code: string;
}

export interface TaskCodeResponse {
  name: string;
  task_code: string | null;
  instructions: string | null;
  versions: TaskCodeVersion[];
  message?: string;
}
