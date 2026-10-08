export interface ChatHistoryEntry {
  role: 'user' | 'assistant';
  content: string;
  thinking?: { text: string; isTool: boolean }[];
  /** Só nas respostas do assistente: resposta de dados (mostra os botões 👍 e 👎) e a nota dada. */
  data?: boolean;
  rating?: ChatRating | null;
}

export type ChatRating = 'up' | 'down';

export interface ChatSession {
  session_id: string;
  title: string;
  message_count: number;
}

export type ChatStreamEvent =
  | { type: 'thinking'; text: string }
  | { type: 'tool'; label: string }
  | { type: 'artifact_preview'; token: string; task_id?: string }
  | { type: 'artifact_saved'; task_id?: string }
  | { type: 'widget_added' }
  | { type: 'widget_updated'; widget_id: string }
  | { type: 'widget_deleted'; widget_id: string }
  | { type: 'reply'; text: string; data?: boolean }
  | { type: 'cancelled' }
  | { type: 'error'; text: string };

export interface ThinkingStep {
  n: number;
  text: string;
  isTool: boolean;
}

export interface TextChatMessage {
  kind: 'text';
  id: string;
  role: 'user' | 'assistant' | 'error';
  html: string;
  time: string;
  /** Texto original da resposta (markdown), usado para identificá-la ao avaliar. */
  raw?: string;
  /** Resposta de dados: mostra os botões 👍 e 👎. */
  rateable?: boolean;
  rating?: ChatRating;
}

export interface ThinkingChatMessage {
  kind: 'thinking';
  id: string;
  steps: ThinkingStep[];
  live: string;
  liveIsTool: boolean;
  open: boolean;
  done: boolean;
  time: string;
}

export type ArtifactType = 'chart' | 'pdf' | 'excel';

export interface ArtifactChatMessage {
  kind: 'artifact';
  id: string;
  artifactType: ArtifactType;
  artifactId: string;
  taskId: string | null;
  saved: boolean;
  time: string;
}

export type ChatMessage = TextChatMessage | ThinkingChatMessage | ArtifactChatMessage;
