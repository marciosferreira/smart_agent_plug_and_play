import { Component, Input, inject, signal } from '@angular/core';
import { ArtifactsService } from '../../core/services/artifacts.service';
import { ARTIFACT_LABELS, Artifact, artifactUrl } from '../../core/models/artifact.model';

@Component({
  selector: 'app-artifact-card',
  templateUrl: './artifact-card.component.html',
})
export class ArtifactCardComponent {
  readonly artifacts = inject(ArtifactsService);

  @Input({ required: true }) artifact!: Artifact;

  readonly deleting = signal(false);
  readonly labels = ARTIFACT_LABELS;

  get selected(): boolean {
    return this.artifacts.isSelected(this.artifact);
  }

  get url(): string {
    return artifactUrl(this.artifact.type, this.artifact.id);
  }

  get name(): string {
    return this.artifact.filename || this.labels[this.artifact.type];
  }

  get displayDate(): string {
    return this.artifact.created_at ? new Date(this.artifact.created_at).toLocaleString('pt-BR') : '—';
  }

  onSelectChange(checked: boolean): void {
    this.artifacts.toggleSelect(this.artifact);
  }

  async deleteArtifact(): Promise<void> {
    if (!confirm(`Deletar este ${this.labels[this.artifact.type]}?`)) return;
    this.deleting.set(true);
    await this.artifacts.deleteOne(this.artifact.type, this.artifact.id);
  }
}
