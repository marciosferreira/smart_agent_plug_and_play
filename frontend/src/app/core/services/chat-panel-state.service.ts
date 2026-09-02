import { Injectable, signal } from '@angular/core';

const MOBILE_BREAKPOINT = 768;

/**
 * Porta a IIFE "CHAT PANEL DRAG & TOGGLE" de dashboard.html:2245-2341:
 * abrir/fechar/redimensionar a sidebar de chat via a CSS var --chat-w,
 * com a largura e o estado aberto/fechado persistidos em localStorage.
 */
@Injectable({ providedIn: 'root' })
export class ChatPanelStateService {
  private readonly _isOpen = signal(false);
  readonly isOpen = this._isOpen.asReadonly();

  private readonly _showBackdrop = signal(false);
  readonly showBackdrop = this._showBackdrop.asReadonly();

  private lastWidth = 360;

  constructor() {
    const savedOpen = localStorage.getItem('chatOpen');
    if (window.innerWidth <= MOBILE_BREAKPOINT || savedOpen === '0') {
      this.setWidthVar(0);
      this._isOpen.set(false);
    } else {
      this.open();
    }
  }

  private setWidthVar(px: number): void {
    document.documentElement.style.setProperty('--chat-w', `${Math.max(0, px)}px`);
  }

  open(): void {
    const defaultW = window.innerWidth <= MOBILE_BREAKPOINT ? window.innerWidth - 80 : this.lastWidth || 360;
    this.setWidthVar(defaultW);
    this._isOpen.set(true);
    this._showBackdrop.set(window.innerWidth <= MOBILE_BREAKPOINT);
    localStorage.setItem('chatOpen', '1');
  }

  close(currentWidthPx?: number): void {
    if (currentWidthPx) this.lastWidth = currentWidthPx;
    this.setWidthVar(0);
    this._isOpen.set(false);
    this._showBackdrop.set(false);
    localStorage.setItem('chatOpen', '0');
  }

  setDragWidth(px: number): void {
    this.lastWidth = px;
    this.setWidthVar(px);
  }
}
