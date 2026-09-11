import { Component, inject } from '@angular/core';
import { TranslateService } from './core/services/translate.service';
import { HeaderComponent } from './features/header/header.component';
import { ChatSidebarComponent } from './features/chat/chat-sidebar.component';
import { ScreenshotOverlayComponent } from './features/chat/screenshot-overlay.component';
import { ChartModalComponent } from './shared/components/chart-modal.component';
import { AlertsPanelComponent } from './features/alerts/alerts-panel.component';
import { AgendaPanelComponent } from './features/agenda/agenda-panel.component';
import { TaskCodeModalComponent } from './shared/components/task-code-modal.component';
import { RunErrorModalComponent } from './shared/components/run-error-modal.component';
import { AuditPanelComponent } from './features/audit/audit-panel.component';
import { AuditCodeModalComponent } from './shared/components/audit-code-modal.component';
import { ArtifactsPanelComponent } from './features/artifacts/artifacts-panel.component';
import { WidgetsGridComponent } from './features/widgets/widgets-grid.component';

@Component({
  selector: 'app-root',
  imports: [
    HeaderComponent,
    ChatSidebarComponent,
    ScreenshotOverlayComponent,
    ChartModalComponent,
    AlertsPanelComponent,
    AgendaPanelComponent,
    TaskCodeModalComponent,
    RunErrorModalComponent,
    AuditPanelComponent,
    AuditCodeModalComponent,
    ArtifactsPanelComponent,
    WidgetsGridComponent,
  ],
  templateUrl: './app.component.html',
})
export class AppComponent {
  private readonly translate = inject(TranslateService);

  constructor() {
    this.translate.load();
  }
}
