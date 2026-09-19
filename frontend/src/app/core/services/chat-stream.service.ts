import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { ChatStreamEvent } from '../models/chat.model';

/**
 * Envolve EventSource nativo — porta dashboard.html:2080-2136.
 * EventSource não passa pelo HttpClient/interceptors do Angular; a URL é
 * montada manualmente a partir de identidade (user/session) + texto.
 */
@Injectable({ providedIn: 'root' })
export class ChatStreamService {
  open(message: string, sessionId: string, userId: string, lang: string): Observable<ChatStreamEvent> {
    return new Observable<ChatStreamEvent>((subscriber) => {
      const url =
        '/chat/stream?' +
        new URLSearchParams({ message, session_id: sessionId, user_id: userId, lang }).toString();
      const es = new EventSource(url);
      // Diagnóstico de latência: compare com as linhas "[TIMING]" do log do servidor.
      const stamp = () => new Date().toISOString().slice(11, 23);
      console.info('[chat-timing] EventSource criado', stamp());
      es.onopen = () => console.info('[chat-timing] SSE conectado (headers recebidos)', stamp());
      let firstEvent = true;

      es.onmessage = (e) => {
        if (firstEvent) {
          firstEvent = false;
          console.info('[chat-timing] 1º evento recebido', stamp());
        }
        try {
          subscriber.next(JSON.parse(e.data) as ChatStreamEvent);
        } catch (err) {
          subscriber.error(err);
        }
      };

      es.onerror = () => {
        subscriber.error(new Error('sse-connection-lost'));
      };

      return () => es.close();
    });
  }
}
