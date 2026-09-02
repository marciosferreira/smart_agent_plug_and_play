import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { NgClass } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { ArtifactChatMessage, ChatMessage, ThinkingChatMessage } from '../../core/models/chat.model';
import { ARTIFACT_LABELS, artifactUrl } from '../../core/models/artifact.model';

const ARTIFACT_ICONS: Record<string, string> = { chart: '📈', pdf: '📄', excel: '📊' };

@Component({
  selector: 'app-chat-message',
  imports: [NgClass],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @switch (message.kind) {
      @case ('text') {
        <div class="chat-msg" [ngClass]="asText.role">
          <div class="msg-bubble" [innerHTML]="trust(asText.html)"></div>
          <div class="msg-time">{{ asText.time }}</div>
        </div>
      }
      @case ('artifact') {
        <div class="chat-msg assistant">
          <div class="msg-bubble">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
              <span style="font-size:12px;color:#94a3b8;">{{ icon() }} {{ label() }}</span>
              @if (asArtifact.saved) {
                <span style="background:#dcfce7;color:#16a34a;font-size:9px;padding:2px 8px;border-radius:99px;border:1px solid #16a34a44;">✔ Salvo</span>
              } @else {
                <span style="background:#dbeafe;color:#2563eb;font-size:9px;padding:2px 8px;border-radius:99px;border:1px solid #1d4ed844;">Preview</span>
              }
              @if (asArtifact.taskId) {
                <span style="background:#f1f5f9;color:#64748b;font-size:9px;padding:2px 8px;border-radius:99px;border:1px solid #e2e8f0;">Tarefa {{ asArtifact.taskId }}</span>
              }
            </div>
            @if (asArtifact.artifactType === 'chart') {
              <img [src]="artifactUrl()" style="width:100%;border-radius:6px;margin-top:8px;border:1px solid #e2e8f0;" />
            } @else {
              <a [href]="artifactUrl()" target="_blank" style="display:inline-flex;align-items:center;gap:6px;margin-top:8px;padding:7px 14px;background:#e2e8f0;border-radius:6px;color:#1e293b;font-size:13px;text-decoration:none;">
                {{ icon() }} Abrir {{ label() }}
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
                <span style="letter-spacing:.08em">RACIOCÍNIO DA IA</span>
                <span class="thinking-count">
                  @if (asThinking.steps.length) {
                    {{ asThinking.steps.length }} etapa{{ asThinking.steps.length > 1 ? 's' : '' }}
                  }
                </span>
                <span class="thinking-chevron">▼</span>
              </div>
              <div class="thinking-live" [class.is-tool]="asThinking.liveIsTool">
                {{ asThinking.live || 'Iniciando' }}
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
          <div class="msg-time">{{ asThinking.time }}</div>
        </div>
      }
    }
  `,
})
export class ChatMessageComponent {
  @Input({ required: true }) message!: ChatMessage;

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
    return ARTIFACT_LABELS[this.asArtifact.artifactType];
  }
  artifactUrl(): string {
    return artifactUrl(this.asArtifact.artifactType, this.asArtifact.artifactId);
  }

  trust(html: string): SafeHtml {
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
