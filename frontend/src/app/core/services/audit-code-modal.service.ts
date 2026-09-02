import { Injectable, signal } from '@angular/core';
import { AuditEntry } from '../models/audit.model';

/** Porta o modal auditCodeModal de dashboard.html:892-906,2971-3024 (erro completo ou código com erro). */
@Injectable({ providedIn: 'root' })
export class AuditCodeModalService {
  readonly open = signal(false);
  readonly title = signal('');
  readonly text = signal('');
  readonly color = signal('#94a3b8');

  showError(entry: AuditEntry): void {
    this.title.set(`task ${entry.task_id} · ${entry.phase} · tentativa ${entry.attempt}`);
    this.text.set((entry.error || '').replace(/❌\s*/, ''));
    this.color.set('#fca5a5');
    this.open.set(true);
  }

  showCode(entry: AuditEntry): void {
    this.title.set(`task ${entry.task_id} · ${entry.phase} · tentativa ${entry.attempt} — código que gerou o erro`);
    this.text.set(entry.code || '');
    this.color.set('#94a3b8');
    this.open.set(true);
  }

  close(): void {
    this.open.set(false);
  }
}
