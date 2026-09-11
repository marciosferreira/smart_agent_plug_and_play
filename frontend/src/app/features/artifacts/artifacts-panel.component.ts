import { Component, effect, inject } from '@angular/core';
import { ArtifactFilter, ArtifactsService } from '../../core/services/artifacts.service';
import { PanelStateService } from '../../core/services/panel-state.service';
import { ArtifactCardComponent } from './artifact-card.component';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

/**
 * Porta o slide-over de Artifacts de dashboard.html:908-975,3183-3218.
 */
@Component({
  selector: 'app-artifacts-panel',
  imports: [ArtifactCardComponent, TranslatePipe],
  templateUrl: './artifacts-panel.component.html',
})
export class ArtifactsPanelComponent {
  readonly artifacts = inject(ArtifactsService);
  readonly panelState = inject(PanelStateService);

  constructor() {
    effect(() => {
      if (this.panelState.open() === 'artifacts') {
        this.artifacts.load();
      }
    });
  }

  setFilter(f: ArtifactFilter): void {
    this.artifacts.setFilter(f);
  }
}
