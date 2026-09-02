import { Injectable, signal } from '@angular/core';

/**
 * Porta a IIFE "Voice Recording" de dashboard.html:2143-2238.
 */
@Injectable({ providedIn: 'root' })
export class VoiceRecorderService {
  private readonly _recording = signal(false);
  readonly recording = this._recording.asReadonly();

  private readonly _hasAudio = signal(false);
  readonly hasAudio = this._hasAudio.asReadonly();

  private readonly _audioUrl = signal<string | null>(null);
  readonly audioUrl = this._audioUrl.asReadonly();

  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private blob: Blob | null = null;

  async toggle(): Promise<void> {
    if (this.recorder && this.recorder.state === 'recording') {
      this.recorder.stop();
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      throw new Error('Microfone não disponível: ' + (e as Error).message);
    }

    this.chunks = [];
    this.recorder = new MediaRecorder(stream);
    this.recorder.ondataavailable = (e) => {
      if (e.data.size > 0) this.chunks.push(e.data);
    };
    this.recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      this.blob = new Blob(this.chunks, { type: this.recorder?.mimeType || 'audio/webm' });
      const url = URL.createObjectURL(this.blob);
      this._audioUrl.set(url);
      this._hasAudio.set(true);
      this._recording.set(false);
    };

    this.recorder.start();
    this._recording.set(true);
  }

  discard(): void {
    this.blob = null;
    this.chunks = [];
    const url = this._audioUrl();
    if (url) URL.revokeObjectURL(url);
    this._audioUrl.set(null);
    this._hasAudio.set(false);
  }

  getBlob(): Blob | null {
    return this.blob;
  }
}
