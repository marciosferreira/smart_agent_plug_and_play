import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { marked } from 'marked';
import { UserSessionService } from './user-session.service';
import { LocaleService } from './locale.service';
import { ChatStreamService } from './chat-stream.service';
import { ScreenshotService } from './screenshot.service';
import { WidgetsService } from './widgets.service';
import { AlertsService } from './alerts.service';
import { TranslateService } from './translate.service';
import {
  ArtifactChatMessage,
  ChatHistoryEntry,
  ChatMessage,
  ChatSession,
  ThinkingChatMessage,
} from '../models/chat.model';

marked.use({
  hooks: {
    postprocess(html: string) {
      return html.replace(/<a href="/g, '<a target="_blank" rel="noopener" href="');
    },
  },
});

function escHtml(s: string): string {
  return s.replace(/</g, '&lt;');
}

function nowLabel(lang: string): string {
  return new Date().toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

let _msgSeq = 0;
function nextId(prefix: string): string {
  return `${prefix}${++_msgSeq}`;
}

/**
 * Orquestra o chat: histórico, sessões, envio (texto/voz/imagens) e o
 * dispatch dos eventos do SSE. Porta a lógica de dashboard.html:1399-2238.
 */
@Injectable({ providedIn: 'root' })
export class ChatService {
  private readonly http = inject(HttpClient);
  private readonly userSession = inject(UserSessionService);
  private readonly locale = inject(LocaleService);
  private readonly stream = inject(ChatStreamService);
  private readonly screenshot = inject(ScreenshotService);
  private readonly widgets = inject(WidgetsService);
  private readonly alerts = inject(AlertsService);
  private readonly translate = inject(TranslateService);

  private welcomeMsg(): string {
    return this.translate.t('chat.welcome');
  }

  readonly messages = signal<ChatMessage[]>([]);
  readonly sessions = signal<ChatSession[]>([]);
  readonly sending = signal(false);

  /** Incrementado a cada envio e a cada stop; um sendChat cujo token ficou velho aborta. */
  private runSeq = 0;
  private cancelActive: (() => void) | null = null;
  readonly historyOpen = signal(false);
  readonly selectionMode = signal(false);
  readonly selectedSessionIds = signal<Set<string>>(new Set());

  get sessionId(): string {
    return this.userSession.sessionId();
  }

  // ── Histórico da sessão ativa ──────────────────────────

  async loadChatHistory(): Promise<void> {
    try {
      const msgs = await firstValueFrom(
        this.http.get<ChatHistoryEntry[]>('/chat/history', { params: { session_id: this.sessionId } }),
      );
      if (!Array.isArray(msgs) || msgs.length === 0) {
        this.pushText('assistant', this.welcomeMsg());
        return;
      }
      for (const m of msgs) {
        if (m.role === 'user') {
          this.pushText('user', escHtml(m.content));
        } else if (m.role === 'assistant') {
          if (m.thinking?.length) this.pushThinkingHistory(m.thinking);
          if (m.content.trim()) this.pushText('assistant', marked.parse(m.content) as string);
        }
      }
    } catch {
      this.pushText('assistant', this.welcomeMsg());
    }
  }

  newChat(): void {
    this.userSession.setSession(this.userSession.newSessionId());
    this.messages.set([]);
    this.pushText('assistant', this.welcomeMsg());
  }

  /**
   * Abre uma sessão nova já com o turno inicial de agendamento (pergunta guia) —
   * usado pelo botão "Agendar nova tarefa via chat" do painel de agenda. O turno é
   * injetado no backend sem chamar o LLM (ver start_task_creation_chat em
   * agent_multi.py), então aqui só recarregamos o histórico dessa sessão.
   */
  async startTaskCreationChat(): Promise<void> {
    this.userSession.setSession(this.userSession.newSessionId());
    this.messages.set([]);
    try {
      await firstValueFrom(
        this.http.post('/chat/start-task-creation', { session_id: this.sessionId, lang: this.locale.lang() }),
      );
      await this.loadChatHistory();
    } catch {
      this.pushText('assistant', this.welcomeMsg());
    }
  }

  /**
   * Idem startTaskCreationChat, mas o turno inicial já traz o resumo da tarefa
   * (buscado no banco pelo backend) — usado pelo botão "Editar via chat" do card de tarefa.
   */
  async startTaskEditChat(taskId: string): Promise<void> {
    this.userSession.setSession(this.userSession.newSessionId());
    this.messages.set([]);
    try {
      await firstValueFrom(
        this.http.post('/chat/start-task-edit', {
          session_id: this.sessionId,
          task_id: taskId,
          lang: this.locale.lang(),
        }),
      );
      await this.loadChatHistory();
    } catch {
      this.pushText('assistant', this.welcomeMsg());
    }
  }

  // ── Lista de conversas ──────────────────────────────────

  async loadSessions(): Promise<void> {
    try {
      const sessions = await firstValueFrom(
        this.http.get<ChatSession[]>('/chat/sessions', { params: { user_id: this.userSession.userId } }),
      );
      this.sessions.set(Array.isArray(sessions) ? sessions : []);
    } catch {
      this.sessions.set([]);
    }
  }

  showHistory(): void {
    if (this.selectionMode()) this.toggleSelectionMode();
    this.loadSessions();
    this.historyOpen.set(true);
  }

  hideHistory(): void {
    this.historyOpen.set(false);
  }

  async switchSession(sessionId: string): Promise<void> {
    if (sessionId === this.sessionId) {
      this.hideHistory();
      return;
    }
    this.userSession.setSession(sessionId);
    this.hideHistory();
    this.messages.set([]);
    await this.loadChatHistory();
  }

  async deleteSession(sessionId: string): Promise<void> {
    if (!confirm(this.translate.t('chat.deleteSessionConfirm'))) return;
    try {
      await firstValueFrom(this.http.delete(`/chat/sessions/${encodeURIComponent(sessionId)}`));
      this.sessions.update((list) => list.filter((s) => s.session_id !== sessionId));
      if (sessionId === this.sessionId) this.newChat();
    } catch {
      alert(this.translate.t('chat.deleteSessionError'));
    }
  }

  toggleSelectionMode(): void {
    this.selectionMode.update((v) => !v);
    this.selectedSessionIds.set(new Set());
  }

  toggleSessionSelected(sessionId: string): void {
    this.selectedSessionIds.update((set) => {
      const next = new Set(set);
      if (next.has(sessionId)) next.delete(sessionId);
      else next.add(sessionId);
      return next;
    });
  }

  toggleSelectAll(): void {
    const all = this.sessions().map((s) => s.session_id);
    const allSelected = all.length > 0 && all.every((id) => this.selectedSessionIds().has(id));
    this.selectedSessionIds.set(allSelected ? new Set() : new Set(all));
  }

  async deleteSelectedSessions(): Promise<void> {
    const ids = [...this.selectedSessionIds()];
    if (!ids.length) return;
    if (!confirm(this.translate.t('chat.deleteSessionsConfirm', { n: ids.length, plural: ids.length !== 1 ? 's' : '' })))
      return;
    let deletedActive = false;
    for (const sessionId of ids) {
      try {
        await firstValueFrom(this.http.delete(`/chat/sessions/${encodeURIComponent(sessionId)}`));
        this.sessions.update((list) => list.filter((s) => s.session_id !== sessionId));
        if (sessionId === this.sessionId) deletedActive = true;
      } catch {
        /* segue tentando as demais */
      }
    }
    this.toggleSelectionMode();
    if (deletedActive) this.newChat();
  }

  // ── Mensagens ────────────────────────────────────────────

  private pushText(role: 'user' | 'assistant' | 'error', html: string): string {
    const id = nextId('cm');
    this.messages.update((list) => [...list, { kind: 'text', id, role, html, time: nowLabel(this.locale.lang()) }]);
    return id;
  }

  /** Reconstrói o balão de raciocínio (fechado) de uma resposta antiga do histórico. */
  private pushThinkingHistory(steps: { text: string; isTool: boolean }[]): void {
    const id = nextId('tm');
    const thinking: ThinkingChatMessage = {
      kind: 'thinking',
      id,
      steps: steps.map((s, i) => ({ n: i + 1, text: s.text, isTool: s.isTool })),
      live: steps[steps.length - 1]?.text ?? '',
      liveIsTool: steps[steps.length - 1]?.isTool ?? false,
      open: false,
      done: true,
      time: nowLabel(this.locale.lang()),
    };
    this.messages.update((list) => [...list, thinking]);
  }

  // ── Envio ────────────────────────────────────────────────

  async sendChat(text: string): Promise<void> {
    const pendingImages = this.screenshot.pendingImages();
    if ((!text && !pendingImages.length) || this.sending()) return;

    let userHtml = text ? escHtml(text) : '';
    if (pendingImages.length) {
      userHtml +=
        '<br>' +
        pendingImages
          .map(
            (b64) =>
              `<img src="data:image/png;base64,${b64}" style="max-height:100px;border-radius:6px;margin-top:6px;margin-right:4px;border:1px solid #e2e8f0;">`,
          )
          .join('');
    }
    this.pushText('user', userHtml);
    this.sending.set(true);
    const runToken = ++this.runSeq;

    const tSend = performance.now();
    console.info('[chat-timing] envio clicado', new Date().toISOString().slice(11, 23));
    await this.postChartSnapshot();
    console.info(`[chat-timing] snapshot concluído em ${Math.round(performance.now() - tSend)} ms`);
    this.screenshot.clear();
    if (this.runSeq !== runToken) return; // parado pelo usuário antes de abrir o stream

    const thinkingId = nextId('tm');
    const thinking: ThinkingChatMessage = {
      kind: 'thinking',
      id: thinkingId,
      steps: [],
      live: '',
      liveIsTool: false,
      open: false,
      done: false,
      time: nowLabel(this.locale.lang()),
    };
    this.messages.update((list) => [...list, thinking]);

    let stepN = 0;
    let hasEvents = false;
    let lastThought = '';

    const updateThinking = (patch: Partial<ThinkingChatMessage>) => {
      this.messages.update((list) =>
        list.map((m) => (m.kind === 'thinking' && m.id === thinkingId ? { ...m, ...patch } : m)),
      );
    };
    const appendStep = (stepText: string, isTool: boolean) => {
      this.messages.update((list) =>
        list.map((m) =>
          m.kind === 'thinking' && m.id === thinkingId
            ? { ...m, steps: [...m.steps, { n: ++stepN, text: stepText, isTool }] }
            : m,
        ),
      );
    };
    const removeThinking = () => {
      this.messages.update((list) => list.filter((m) => !(m.kind === 'thinking' && m.id === thinkingId)));
    };
    const finalizeThinking = () => {
      if (!hasEvents) removeThinking();
      else updateThinking({ done: true });
    };

    const finishSending = () => {
      this.cancelActive = null;
      this.sending.set(false);
    };

    const subscription = this.stream.open(text, this.sessionId, this.userSession.userId, this.locale.lang()).subscribe({
      next: (ev) => {
        switch (ev.type) {
          case 'thinking':
            hasEvents = true;
            lastThought = ev.text;
            appendStep(ev.text, false);
            updateThinking({ live: ev.text, liveIsTool: false });
            break;
          case 'tool':
            hasEvents = true;
            appendStep(ev.label, true);
            if (lastThought) {
              updateThinking({ live: `${lastThought}\n⚙ ${ev.label}`, liveIsTool: false });
              lastThought = '';
            } else {
              updateThinking({ live: ev.label, liveIsTool: true });
            }
            break;
          case 'artifact_preview': {
            hasEvents = true;
            const match = ev.token.match(/^\[(chart|pdf|excel):([a-f0-9-]{36})\]$/);
            if (match) {
              const artifactId = nextId('am');
              const artifact: ArtifactChatMessage = {
                kind: 'artifact',
                id: artifactId,
                artifactType: match[1] as 'chart' | 'pdf' | 'excel',
                artifactId: match[2],
                taskId: ev.task_id ?? null,
                saved: false,
                time: nowLabel(this.locale.lang()),
              };
              this.messages.update((list) => [...list, artifact]);
            }
            break;
          }
          case 'artifact_saved':
            // Ao salvar, o backend promove TODOS os artefatos de teste da tarefa
            // (chart_store.promote_test_artifacts) — então todos os previews dela viram
            // "Salvo", não só o último (um teste pode gerar vários: gráfico, PDF, Excel).
            this.messages.update((list) =>
              list.map((m) => (m.kind === 'artifact' && m.taskId === ev.task_id ? { ...m, saved: true } : m)),
            );
            break;
          case 'widget_added':
            this.widgets.onWidgetAdded();
            break;
          case 'widget_updated':
            this.widgets.onWidgetUpdated(ev.widget_id);
            break;
          case 'widget_deleted':
            this.widgets.onWidgetDeleted(ev.widget_id);
            break;
          case 'reply':
            subscription.unsubscribe();
            this.alerts.pollFast(); // monitor recém-criado: 1ª checagem no próximo tick do daemon
            finalizeThinking();
            this.pushText('assistant', marked.parse(ev.text) as string);
            finishSending();
            break;
          case 'cancelled':
            subscription.unsubscribe();
            finalizeThinking();
            this.pushText('assistant', this.translate.t('chat.stopped'));
            finishSending();
            break;
          case 'error':
            subscription.unsubscribe();
            removeThinking();
            this.pushText('error', ev.text);
            finishSending();
            break;
        }
      },
      error: () => {
        removeThinking();
        this.pushText('error', this.translate.t('chat.connectionLost'));
        finishSending();
      },
    });
    this.cancelActive = () => {
      subscription.unsubscribe();
      finalizeThinking();
    };
  }

  /**
   * Botão "parar": fecha o SSE, avisa o back para interromper o agente e libera o
   * input na hora. O back serializa runs da mesma sessão, então uma nova pergunta
   * feita logo em seguida só começa quando o run interrompido terminar de fato.
   */
  stopChat(): void {
    if (!this.sending()) return;
    this.runSeq++;
    this.cancelActive?.();
    this.cancelActive = null;
    this.http.post('/chat/cancel', { session_id: this.sessionId }).subscribe({ error: () => {} });
    this.pushText('assistant', this.translate.t('chat.stopped'));
    this.sending.set(false);
  }

  async sendAudio(blob: Blob): Promise<string> {
    const form = new FormData();
    form.append('audio', blob, 'recording.webm');
    const data = await firstValueFrom(this.http.post<{ transcript: string; error?: string }>('/chat/transcribe', form));
    return data.transcript;
  }

  // ── Snapshot do dashboard, enviado antes de cada mensagem ─

  private async postChartSnapshot(): Promise<void> {
    try {
      const snapshot: Record<string, unknown> = {
        // charts/kpis ficam sempre vazios mesmo no original: getChartSnapshot()
        // lê do dict `charts` top-level de dashboard.html:1080 (helper de uma
        // seção de KPIs que não existe mais no markup atual), não da grid de
        // widgets customizados (`_widgetCharts`) — confirmado que essa grid
        // nunca alimentou o snapshot.
        charts: {},
        kpis: [],
        alerts: [] as unknown[],
        images_b64: this.screenshot.pendingImages(),
      };
      try {
        const alerts = await firstValueFrom(
          this.http.get<any[]>('/alerts', { params: { all: 'true', user_id: this.userSession.userId } }),
        );
        snapshot['alerts'] = (alerts || []).map((a) => ({
          message: a.message ?? '',
          value: a.value ?? null,
          threshold: a.threshold ?? null,
          created_at: a.created_at ?? '',
          read: a.read ?? false,
        }));
      } catch {
        /* não bloqueia se falhar */
      }
      try {
        const artifacts = await firstValueFrom(
          this.http.get<any[]>('/artifacts', { params: { user_id: this.userSession.userId } }),
        );
        snapshot['artifacts'] = (artifacts || [])
          .filter((a) => !a.session_id?.startsWith('test_'))
          .map((a) => ({ id: a.id, type: a.type, label: a.label ?? a.filename ?? a.id, created_at: a.created_at ?? '' }));
      } catch {
        /* não bloqueia se falhar */
      }
      try {
        const tasks = await firstValueFrom(
          this.http.get<any[]>('/tasks', { params: { user_id: this.userSession.userId } }),
        );
        snapshot['tasks'] = (tasks || [])
          .filter((t) => t.status !== 'cancelled')
          .map((t) => ({ id: t.id, name: t.name ?? '', status: t.status ?? '', schedule: t.schedule ?? '' }));
      } catch {
        /* não bloqueia se falhar */
      }

      await firstValueFrom(this.http.post('/chart-snapshot', { session_id: this.sessionId, snapshot }));
    } catch {
      /* não bloqueia o chat se falhar */
    }
  }
}
