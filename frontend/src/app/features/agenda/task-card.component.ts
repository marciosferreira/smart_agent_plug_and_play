import { Component, Input, inject, signal } from '@angular/core';
import { AgendaService } from '../../core/services/agenda.service';
import { TaskCodeModalService } from '../../core/services/task-code-modal.service';
import { RunErrorModalService } from '../../core/services/run-error-modal.service';
import { ScheduledTask } from '../../core/models/task.model';

const FREQ_LABELS_WEEKDAY: Record<string, string> = {
  monday: 'Segunda',
  tuesday: 'Terça',
  wednesday: 'Quarta',
  thursday: 'Quinta',
  friday: 'Sexta',
  saturday: 'Sábado',
  sunday: 'Domingo',
};

const STATUS_MAP: Record<string, [string, string, string]> = {
  active: ['#dcfce7', '#16a34a', 'Ativa'],
  running: ['#ffedd5', '#ea580c', 'Executando'],
  error: ['#fee2e2', '#dc2626', 'Erro'],
  completed: ['#dbeafe', '#2563eb', 'Concluída'],
  cancelled: ['#f5f5f4', '#78716c', 'Cancelada'],
  paused: ['#ede9fe', '#7c3aed', 'Pausada'],
  pending_approval: ['#fef3c7', '#b45309', 'Aguardando'],
};

@Component({
  selector: 'app-task-card',
  templateUrl: './task-card.component.html',
})
export class TaskCardComponent {
  private readonly agenda = inject(AgendaService);
  private readonly taskCode = inject(TaskCodeModalService);
  private readonly runError = inject(RunErrorModalService);

  @Input({ required: true }) task!: ScheduledTask;
  @Input() isNew = false;

  readonly runBusy = signal(false);
  readonly runDone = signal(false);
  readonly pauseBusy = signal(false);
  readonly notifyBusy = signal(false);

  freqLabel(): string {
    const t = this.task;
    const f = t.frequency || '';
    const time = t.time || '';
    if (f === 'on_demand') return 'Sob demanda';
    const mM = f.match(/^every_(\d+)m$/);
    const mH = f.match(/^every_(\d+)h$/);
    const mD = f.match(/^every_(\d+)d$/);
    if (mM) return `A cada ${mM[1]} min`;
    if (mH) return `A cada ${mH[1]}h`;
    if (mD) return `A cada ${mD[1]} dias`;
    if (f === 'weekly') return `Semanal · ${FREQ_LABELS_WEEKDAY[t.weekday || ''] || t.weekday} · ${time}`;
    if (f === 'monthly') return `Mensal · dia ${t.day} · ${time}`;
    if (f === 'daily') return `Diária · ${time}`;
    if (f === 'once') return `Única vez · ${time}`;
    return f;
  }

  statusBadge(): { bg: string; color: string; label: string } {
    const [bg, color, label] = STATUS_MAP[this.task.status] || ['#e2e8f0', '#94a3b8', this.task.status];
    return { bg, color, label };
  }

  conditionLabel(): string {
    const t = this.task;
    if (t.condition_operator === 'is_empty') return 'executa se sem registros';
    if (t.condition_operator === 'is_not_empty') return 'executa se houver registros';
    return `executa se valor ${t.condition_operator} ${t.condition_threshold}`;
  }

  runTime(r: { started_at: string | null }): string {
    return r.started_at ? r.started_at.slice(0, 16).replace('T', ' ') : '—';
  }

  runDuration(r: { started_at: string | null; ended_at: string | null }): string | null {
    if (!r.started_at || !r.ended_at) return null;
    const secs = Math.round((new Date(r.ended_at).getTime() - new Date(r.started_at).getTime()) / 1000);
    if (secs < 60) return `${secs}s`;
    return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  }

  runDotColor(status: string): string {
    return { success: '#4ade80', error: '#f87171', running: '#f97316' }[status] || '#64748b';
  }

  async runNow(): Promise<void> {
    this.runBusy.set(true);
    const res = await this.agenda.runTaskNow(this.task.id);
    this.runBusy.set(false);
    if (!res.ok) {
      alert(res.message);
    } else {
      this.runDone.set(true);
      setTimeout(() => this.runDone.set(false), 1500);
    }
  }

  async togglePause(): Promise<void> {
    this.pauseBusy.set(true);
    const res = await this.agenda.togglePause(this.task.id);
    if (!res.ok) {
      alert(res.message || 'Erro ao alterar status.');
      this.pauseBusy.set(false);
    }
  }

  async toggleNotify(): Promise<void> {
    this.notifyBusy.set(true);
    const res = await this.agenda.toggleNotify(this.task.id);
    if (!res.ok) {
      alert(res.message || 'Erro ao alterar notificações.');
      this.notifyBusy.set(false);
    }
  }

  openCode(): void {
    this.taskCode.openFor(this.task.id);
  }

  async deleteTask(): Promise<void> {
    if (!confirm(`Remover tarefa [${this.task.id}]?`)) return;
    await this.agenda.deleteTask(this.task.id);
  }

  showRunError(error: string): void {
    this.runError.show(error);
  }
}
