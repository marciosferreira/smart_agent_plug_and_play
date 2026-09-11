import { Component, Input, inject, signal } from '@angular/core';
import { ArtifactsService } from '../../core/services/artifacts.service';
import { Artifact, artifactUrl } from '../../core/models/artifact.model';
import { TranslateService } from '../../core/services/translate.service';
import { LocaleService } from '../../core/services/locale.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

const ARTIFACT_LABEL_KEYS: Record<string, string> = { pdf: 'artifacts.typePdf', chart: 'artifacts.typeChart', excel: 'artifacts.typeExcel' };

@Component({
  selector: 'app-artifact-card',
  imports: [TranslatePipe],
  templateUrl: './artifact-card.component.html',
})
export class ArtifactCardComponent {
  readonly artifacts = inject(ArtifactsService);
  private readonly translate = inject(TranslateService);
  private readonly locale = inject(LocaleService);

  @Input({ required: true }) artifact!: Artifact;

  readonly deleting = signal(false);

  get selected(): boolean {
    return this.artifacts.isSelected(this.artifact);
  }

  get url(): string {
    return artifactUrl(this.artifact.type, this.artifact.id);
  }

  get name(): string {
    return this.artifact.filename || this.translate.t(ARTIFACT_LABEL_KEYS[this.artifact.type]);
  }

  get displayDate(): string {
    return this.artifact.created_at ? new Date(this.artifact.created_at).toLocaleString(this.locale.lang()) : '—';
  }

  onSelectChange(checked: boolean): void {
    this.artifacts.toggleSelect(this.artifact);
  }

  async deleteArtifact(): Promise<void> {
    if (!confirm(this.translate.t('artifacts.deleteConfirm', { type: this.translate.t(ARTIFACT_LABEL_KEYS[this.artifact.type]) }))) return;
    this.deleting.set(true);
    await this.artifacts.deleteOne(this.artifact.type, this.artifact.id);
  }
}
