export interface AppAlert {
  id: string;
  message: string;
  value: number | null;
  threshold: number | null;
  created_at: string;
  read: boolean;
  task_id: string | null;
  task_name: string | null;
}
