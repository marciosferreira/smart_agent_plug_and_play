import { Component, Input, inject, signal } from '@angular/core';
import { AgendaService } from '../../core/services/agenda.service';
import { TaskCodeModalService } from '../../core/services/task-code-modal.service';
import { RunErrorModalService } from '../../core/services/run-error-modal.service';
import { TranslateService } from '../../core/services/translate.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';
import { ScheduledTask } from '../../core/models/task.model';

const FREQ_LABELS_WEEKDAY: Record<string, string> = {
  monday: 'taskCard.weekday.monday',
  tuesday: 'taskCard.weekday.tuesday',
  wednesday: 'taskCard.weekday.wednesday',
  thursday: 'taskCard.weekday.thursday',
  friday: 'taskCard.weekday.friday',
  saturday: 'taskCard.weekday.saturday',
  sunday: 'taskCard.weekday.sunday',
};

const STATUS_MAP: Record<string, [string, string, string]> = {
  active: ['#dcfce7', '#16a34a', 'taskCard.status.active'],
  running: ['#ffedd5', '#ea580c', 'taskCard.status.running'],
  error: ['#fee2e2', '#dc2626', 'taskCard.status.error'],
  completed: ['#dbeafe', '#2563eb', 'taskCard.status.completed'],
  cancelled: ['#f5f5f4', '#78716c', 'taskCard.status.cancelled'],
  paused: ['#ede9fe', '#7c3aed', 'taskCard.status.paused'],
  pending_approval: ['#fef3c7', '#b45309', 'taskCard.status.pending_approval'],
};

@Component({
  selector: 'app-task-card',
  imports: [TranslatePipe],
  templateUrl: './task-card.component.html',
})
export class TaskCardComponent {
  private readonly agenda = inject(AgendaService);
  private readonly taskCode = inject(TaskCodeModalService);
  private readonly runError = inject(RunErrorModalService);
  private readonly translate = inject(TranslateService);

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
    if (f === 'on_demand') return this.translate.t('taskCard.onDemand');
    const mM = f.match(/^every_(\d+)m$/);
    const mH = f.match(/^every_(\d+)h$/);
    const mD = f.match(/^every_(\d+)d$/);
    if (mM) return this.translate.t('taskCard.everyMin', { n: mM[1] });
    if (mH) return this.translate.t('taskCard.everyHour', { n: mH[1] });
    if (mD) return this.translate.t('taskCard.everyDay', { n: mD[1] });
    if (f === 'weekly') {
      const weekdayKey = FREQ_LABELS_WEEKDAY[t.weekday || ''];
      const weekday = weekdayKey ? this.translate.t(weekdayKey) : t.weekday;
      return this.translate.t('taskCard.weekly', { weekday: weekday || '', time });
    }
    if (f === 'monthly') return this.translate.t('taskCard.monthly', { day: t.day ?? '', time });
    if (f === 'daily') return this.translate.t('taskCard.daily', { time });
    if (f === 'once') return this.translate.t('taskCard.once', { time });
    return f;
  }

  statusBadge(): { bg: string; color: string; label: string } {
    const [bg, color, labelKey] = STATUS_MAP[this.task.status] || ['#e2e8f0', '#94a3b8', this.task.status];
    return { bg, color, label: this.translate.t(labelKey) };
  }

  conditionLabel(): string {
    const t = this.task;
    if (t.condition_operator === 'is_empty') return this.translate.t('taskCard.condIsEmpty');
    if (t.condition_operator === 'is_not_empty') return this.translate.t('taskCard.condIsNotEmpty');
    return this.translate.t('taskCard.condCompare', { op: t.condition_operator ?? '', threshold: t.condition_threshold ?? '' });
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
      alert(res.message || this.translate.t('taskCard.runNowError'));
    } else {
      this.runDone.set(true);
      setTimeout(() => this.runDone.set(false), 1500);
    }
  }

  async togglePause(): Promise<void> {
    this.pauseBusy.set(true);
    const res = await this.agenda.togglePause(this.task.id);
    if (!res.ok) {
      alert(res.message || this.translate.t('taskCard.pauseError'));
      this.pauseBusy.set(false);
    }
  }

  async toggleNotify(): Promise<void> {
    this.notifyBusy.set(true);
    const res = await this.agenda.toggleNotify(this.task.id);
    if (!res.ok) {
      alert(res.message || this.translate.t('taskCard.notifyError'));
      this.notifyBusy.set(false);
    }
  }

  openCode(): void {
    this.taskCode.openFor(this.task.id);
  }

  async deleteTask(): Promise<void> {
    if (!confirm(this.translate.t('taskCard.deleteConfirm', { id: this.task.id }))) return;
    await this.agenda.deleteTask(this.task.id);
  }

  showRunError(error: string): void {
    this.runError.show(error);
  }
}
