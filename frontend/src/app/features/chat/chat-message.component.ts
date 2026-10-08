import { ChangeDetectionStrategy, Component, Input, inject } from '@angular/core';
import { NgClass } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { ArtifactChatMessage, ChatMessage, ThinkingChatMessage } from '../../core/models/chat.model';
import { artifactUrl } from '../../core/models/artifact.model';
import { TranslateService } from '../../core/services/translate.service';
import { ChatService } from '../../core/services/chat.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

const ARTIFACT_ICONS: Record<string, string> = { chart: '📈', pdf: '📄', excel: '📊' };
const ARTIFACT_LABEL_KEYS: Record<string, string> = { pdf: 'artifacts.typePdf', chart: 'artifacts.typeChart', excel: 'artifacts.typeExcel' };

@Component({
  selector: 'app-chat-message',
  imports: [NgClass, TranslatePipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @switch (message.kind) {
      @case ('text') {
        <div class="chat-msg" [ngClass]="asText.role">
          <div class="msg-bubble" [innerHTML]="trust(asText.html)"></div>
          <div class="msg-time">
            {{ asText.time }}
            @if (asText.role === 'assistant' && asText.rateable) {
              <button
                type="button"
                class="msg-rate up"
                [class.active]="asText.rating === 'up'"
                [title]="'chat.rateUpTip' | translate"
                [attr.aria-label]="'chat.rateUpTip' | translate"
                [attr.aria-pressed]="asText.rating === 'up'"
                (click)="chat.rateReply(asText.id, 'up')"
              >
                <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                  <path d="M5 7v6.5M5 7l2.6-4.4c.9-.2 1.9.5 1.7 1.5L8.9 6.5h3.3c.9 0 1.5.8 1.3 1.6l-.9 4.2c-.1.7-.7 1.2-1.4 1.2H5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
                  <path d="M2.5 7H5v6.5H2.5z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
                </svg>
              </button>
              <button
                type="button"
                class="msg-rate down"
                [class.active]="asText.rating === 'down'"
                [title]="'chat.rateDownTip' | translate"
                [attr.aria-label]="'chat.rateDownTip' | translate"
                [attr.aria-pressed]="asText.rating === 'down'"
                (click)="chat.rateReply(asText.id, 'down')"
              >
                <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true" style="transform: rotate(180deg)">
                  <path d="M5 7v6.5M5 7l2.6-4.4c.9-.2 1.9.5 1.7 1.5L8.9 6.5h3.3c.9 0 1.5.8 1.3 1.6l-.9 4.2c-.1.7-.7 1.2-1.4 1.2H5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
                  <path d="M2.5 7H5v6.5H2.5z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
                </svg>
              </button>
            }
          </div>
          @if (asText.role === 'assistant' && asText.rating === 'down') {
            <div class="msg-rate-hint">{{ 'chat.rateDownHint' | translate }}</div>
          }
        </div>
      }
      @case ('artifact') {
        <div class="chat-msg assistant">
          <div class="msg-bubble">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
              <span style="font-size:12px;color:#94a3b8;">{{ icon() }} {{ label() }}</span>
              @if (asArtifact.saved) {
                <span style="background:#dcfce7;color:#16a34a;font-size:9px;padding:2px 8px;border-radius:99px;border:1px solid #16a34a44;">{{ 'chat.savedBadge' | translate }}</span>
              } @else {
                <span style="background:#dbeafe;color:#2563eb;font-size:9px;padding:2px 8px;border-radius:99px;border:1px solid #1d4ed844;">{{ 'chat.previewBadge' | translate }}</span>
              }
              @if (asArtifact.taskId) {
                <span style="background:#f1f5f9;color:#64748b;font-size:9px;padding:2px 8px;border-radius:99px;border:1px solid #e2e8f0;">{{ 'chat.taskBadge' | translate: { id: asArtifact.taskId } }}</span>
              }
            </div>
            @if (asArtifact.artifactType === 'chart') {
              <img [src]="artifactUrl()" style="width:100%;border-radius:6px;margin-top:8px;border:1px solid #e2e8f0;" />
            } @else {
              <a [href]="artifactUrl()" target="_blank" style="display:inline-flex;align-items:center;gap:6px;margin-top:8px;padding:7px 14px;background:#e2e8f0;border-radius:6px;color:#1e293b;font-size:13px;text-decoration:none;">
                {{ icon() }} {{ 'chat.openArtifact' | translate: { label: label() } }}
              </a>
            }
          </div>
          <div class="msg-time">{{ asArtifact.time }}</div>
        </div>
      }
      @case ('thinking') {
        <div class="chat-msg assistant">
          <div class="msg-bubble">
            <div class="thinking-wrap" [class.open]="asThinking.open">
              <div class="thinking-header" (click)="asThinking.open = !asThinking.open">
                <span style="letter-spacing:.08em">{{ 'chat.thinkingHeader' | translate }}</span>
                <span class="thinking-count">
                  @if (asThinking.steps.length) {
                    {{ 'chat.stepsCount' | translate: { n: asThinking.steps.length, plural: asThinking.steps.length > 1 ? 's' : '' } }}
                  }
                </span>
                <span class="thinking-chevron">▼</span>
              </div>
              <div class="thinking-live" [class.is-tool]="asThinking.liveIsTool">
                {{ asThinking.live || ('chat.startingLabel' | translate) }}
                @if (!asThinking.done) {
                  <span class="thinking-cursor"></span>
                }
              </div>
              <div class="thinking-history">
                @for (step of asThinking.steps; track step.n) {
                  <div class="thinking-step" [class.is-tool]="step.isTool">
                    <span class="thinking-step-n">{{ step.n }}.</span>{{ step.text }}
                  </div>
                }
              </div>
            </div>
          </div>
        </div>
      }
    }
  `,
})
export class ChatMessageComponent {
  @Input({ required: true }) message!: ChatMessage;
  private readonly translate = inject(TranslateService);
  protected readonly chat = inject(ChatService);

  constructor(private readonly sanitizer: DomSanitizer) {}

  get asText() {
    return this.message as Extract<ChatMessage, { kind: 'text' }>;
  }
  get asArtifact(): ArtifactChatMessage {
    return this.message as ArtifactChatMessage;
  }
  get asThinking(): ThinkingChatMessage {
    return this.message as ThinkingChatMessage;
  }

  icon(): string {
    return ARTIFACT_ICONS[this.asArtifact.artifactType];
  }
  label(): string {
    return this.translate.t(ARTIFACT_LABEL_KEYS[this.asArtifact.artifactType]);
  }
  artifactUrl(): string {
    return artifactUrl(this.asArtifact.artifactType, this.asArtifact.artifactId);
  }

  trust(html: string): SafeHtml {
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
