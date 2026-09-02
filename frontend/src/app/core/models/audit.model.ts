export interface AuditEntry {
  id: number;
  task_id: string;
  phase: 'create' | 'fix' | string;
  attempt: number;
  error: string;
  code: string | null;
  created_at: string | null;
}
