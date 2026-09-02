import { Injectable, inject, signal } from '@angular/core';
import { toCanvas } from 'html-to-image';
import { ChatPanelStateService } from './chat-panel-state.service';

export interface SelectionRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

const MAX_IMAGES = 3;

/**
 * Porta a IIFE de captura de tela (seleção de área) de dashboard.html:1832-2050.
 */
@Injectable({ providedIn: 'root' })
export class ScreenshotService {
  private readonly chatPanel = inject(ChatPanelStateService);

  private readonly _pendingImages = signal<string[]>([]);
  readonly pendingImages = this._pendingImages.asReadonly();

  private readonly _overlayActive = signal(false);
  readonly overlayActive = this._overlayActive.asReadonly();

  private readonly _selectionRect = signal<SelectionRect | null>(null);
  readonly selectionRect = this._selectionRect.asReadonly();

  private readonly _confirmBtnPos = signal<{ left: number; top: number } | null>(null);
  readonly confirmBtnPos = this._confirmBtnPos.asReadonly();

  private readonly _capturing = signal(false);
  readonly capturing = this._capturing.asReadonly();

  private dragging = false;
  private startX = 0;
  private startY = 0;
  private selCoords: SelectionRect | null = null;
  private chatWasOpen = false;

  start(): void {
    this.chatWasOpen = this.chatPanel.isOpen();
    if (this.chatWasOpen) this.chatPanel.close();
    this.hideConfirm();
    this._overlayActive.set(true);
  }

  private reopenChatIfNeeded(): void {
    if (this.chatWasOpen) {
      this.chatWasOpen = false;
      this.chatPanel.open();
    }
  }

  private hideConfirm(): void {
    this._confirmBtnPos.set(null);
    this.selCoords = null;
  }

  selStart(cx: number, cy: number): void {
    if (!this._overlayActive()) return;
    this.hideConfirm();
    this.dragging = true;
    this.startX = cx;
    this.startY = cy;
    this._selectionRect.set({ left: cx, top: cy, width: 0, height: 0 });
  }

  selMove(cx: number, cy: number): void {
    if (!this.dragging) return;
    const left = Math.min(this.startX, cx);
    const top = Math.min(this.startY, cy);
    this._selectionRect.set({ left, top, width: Math.abs(cx - this.startX), height: Math.abs(cy - this.startY) });
  }

  selEnd(cx: number, cy: number): void {
    if (!this.dragging) return;
    this.dragging = false;
    const x = Math.min(this.startX, cx);
    const y = Math.min(this.startY, cy);
    const w = Math.abs(cx - this.startX);
    const h = Math.abs(cy - this.startY);
    if (w < 10 || h < 10) {
      this._selectionRect.set(null);
      return;
    }
    this.selCoords = { left: x, top: y, width: w, height: h };
    const bw = 130;
    const bh = 36;
    let left = x + w - bw;
    let top = y + h + 10;
    if (left < 8) left = 8;
    if (top + bh > window.innerHeight - 8) top = y - bh - 10;
    this._confirmBtnPos.set({ left, top });
  }

  cancel(): void {
    this._overlayActive.set(false);
    this._selectionRect.set(null);
    this.hideConfirm();
    this.dragging = false;
    this.reopenChatIfNeeded();
  }

  async capture(): Promise<void> {
    if (!this.selCoords) return;
    const { left: x, top: y, width: w, height: h } = this.selCoords;
    this.hideConfirm();
    this._overlayActive.set(false);
    this._selectionRect.set(null);

    await new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())));

    this._capturing.set(true);
    let frame: HTMLCanvasElement;
    try {
      const scale = Math.min(window.devicePixelRatio || 1, 1.5);
      frame = await toCanvas(document.body, { pixelRatio: scale, skipFonts: true, cacheBust: false });
    } catch (err) {
      this._capturing.set(false);
      this.reopenChatIfNeeded();
      alert('Erro ao capturar tela: ' + (err as Error).message);
      return;
    }
    this._capturing.set(false);
    this.reopenChatIfNeeded();

    const s = Math.min(window.devicePixelRatio || 1, 1.5);
    const crop = document.createElement('canvas');
    crop.width = Math.round(w * s);
    crop.height = Math.round(h * s);
    crop
      .getContext('2d')!
      .drawImage(
        frame,
        Math.round((x + window.scrollX) * s),
        Math.round((y + window.scrollY) * s),
        Math.round(w * s),
        Math.round(h * s),
        0,
        0,
        crop.width,
        crop.height,
      );

    if (this._pendingImages().length >= MAX_IMAGES) {
      alert('Máximo de 3 imagens por mensagem.');
      return;
    }
    const b64 = crop.toDataURL('image/png').split(',')[1];
    this._pendingImages.update((imgs) => [...imgs, b64]);
  }

  removeImage(i: number): void {
    this._pendingImages.update((imgs) => imgs.filter((_, idx) => idx !== i));
  }

  clear(): void {
    this._pendingImages.set([]);
  }
}
