import { Component, Input, inject, signal } from '@angular/core';
import { AuditService } from '../../core/services/audit.service';
import { AuditCodeModalService } from '../../core/services/audit-code-modal.service';
import { AuditEntry } from '../../core/models/audit.model';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

@Component({
  selector: 'app-audit-entry-card',
  imports: [TranslatePipe],
  templateUrl: './audit-entry-card.component.html',
})
export class AuditEntryCardComponent {
  private readonly audit = inject(AuditService);
  private readonly codeModal = inject(AuditCodeModalService);

  @Input({ required: true }) entry!: AuditEntry;

  readonly deleting = signal(false);
  readonly copied = signal(false);

  get selected(): boolean {
    return this.audit.selected().has(this.entry.id);
  }

  get timestamp(): string {
    return this.entry.created_at ? this.entry.created_at.replace('T', ' ').slice(0, 19) : '—';
  }

  get errorFull(): string {
    return (this.entry.error || '').replace(/❌\s*/, '');
  }

  get errorShort(): string {
    return this.errorFull.slice(0, 180);
  }

  get errorIsLong(): boolean {
    return this.errorFull.length > 180;
  }

  get hasCode(): boolean {
    return !!this.entry.code && this.entry.code.trim().length > 0;
  }

  get phaseColor(): string {
    return this.entry.phase === 'create' ? '#f97316' : '#a78bfa';
  }

  get attemptColor(): string {
    return this.entry.attempt >= 3 ? '#f87171' : this.entry.attempt === 2 ? '#fb923c' : '#fbbf24';
  }

  onSelectChange(checked: boolean): void {
    this.audit.toggleSelect(this.entry.id, checked);
  }

  openError(): void {
    this.codeModal.showError(this.entry);
  }

  openCode(): void {
    this.codeModal.showCode(this.entry);
  }

  async copyCode(): Promise<void> {
    if (!this.entry.code) return;
    await navigator.clipboard.writeText(this.entry.code);
    this.copied.set(true);
    setTimeout(() => this.copied.set(false), 1500);
  }

  async deleteEntry(): Promise<void> {
    this.deleting.set(true);
    await this.audit.deleteOne(this.entry.id);
  }
}
