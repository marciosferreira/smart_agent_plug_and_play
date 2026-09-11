import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { NgClass } from '@angular/common';
import { BrandingService } from '../../core/services/branding.service';
import { ApiStatusService } from '../../core/services/api-status.service';
import { PanelStateService } from '../../core/services/panel-state.service';
import { AlertsService } from '../../core/services/alerts.service';
import { AgendaService } from '../../core/services/agenda.service';
import { DEMO_USER_IDS, UserSessionService } from '../../core/services/user-session.service';
import { IDIOMAS_SUPORTADOS, Idioma, LocaleService } from '../../core/services/locale.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

/**
 * Porta dashboard.html:767-801 (header + nav de auditoria).
 */
@Component({
  selector: 'app-header',
  imports: [NgClass, TranslatePipe],
  templateUrl: './header.component.html',
})
export class HeaderComponent implements OnInit, OnDestroy {
  readonly branding = inject(BrandingService);
  readonly apiStatus = inject(ApiStatusService);
  readonly panelState = inject(PanelStateService);
  readonly alerts = inject(AlertsService);
  readonly agenda = inject(AgendaService);
  readonly userSession = inject(UserSessionService);
  readonly locale = inject(LocaleService);

  readonly idiomas = IDIOMAS_SUPORTADOS;
  readonly demoUserIds = DEMO_USER_IDS.includes(this.userSession.userId)
    ? DEMO_USER_IDS
    : [...DEMO_USER_IDS, this.userSession.userId];
  readonly clock = signal(new Date().toLocaleTimeString(this.locale.lang(), { hour12: false }));
  readonly userIdCopied = signal(false);
  private clockTimer?: ReturnType<typeof setInterval>;
  private copiedTimer?: ReturnType<typeof setTimeout>;

  copyUserId(): void {
    navigator.clipboard.writeText(this.userSession.userId).then(() => {
      this.userIdCopied.set(true);
      clearTimeout(this.copiedTimer);
      this.copiedTimer = setTimeout(() => this.userIdCopied.set(false), 1500);
    });
  }

  onUserChange(event: Event): void {
    const userId = (event.target as HTMLSelectElement).value;
    this.userSession.switchUser(userId);
  }

  onLangChange(event: Event): void {
    const lang = (event.target as HTMLSelectElement).value as Idioma;
    this.locale.setLang(lang);
  }

  ngOnInit(): void {
    this.branding.load();
    this.apiStatus.refreshAll();
    this.clockTimer = setInterval(() => {
      this.clock.set(new Date().toLocaleTimeString(this.locale.lang(), { hour12: false }));
    }, 1000);
  }

  ngOnDestroy(): void {
    clearInterval(this.clockTimer);
    clearTimeout(this.copiedTimer);
  }
}
