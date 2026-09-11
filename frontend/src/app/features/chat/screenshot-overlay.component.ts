import { Component, HostListener, inject } from '@angular/core';
import { ScreenshotService } from '../../core/services/screenshot.service';
import { TranslatePipe } from '../../core/pipes/translate.pipe';

@Component({
  selector: 'app-screenshot-overlay',
  imports: [TranslatePipe],
  template: `
    <div id="screenshotOverlay" [class.active]="screenshot.overlayActive()">
      @if (screenshot.selectionRect(); as r) {
        <div
          id="selectionRect"
          style="display:block"
          [style.left.px]="r.left"
          [style.top.px]="r.top"
          [style.width.px]="r.width"
          [style.height.px]="r.height"
        ></div>
      }
    </div>

    @if (screenshot.confirmBtnPos(); as pos) {
      <button
        id="selectionConfirmBtn"
        style="display:flex;position:fixed;z-index:10001;background:#a78bfa;color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 2px 14px rgba(0,0,0,.5);align-items:center;gap:6px;font-family:Inter,sans-serif;white-space:nowrap;"
        [style.left.px]="pos.left"
        [style.top.px]="pos.top"
        (click)="screenshot.capture()"
      >
        {{ 'screenshot.captureArea' | translate }}
      </button>
    }

    @if (screenshot.capturing()) {
      <div
        style="display:flex;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);align-items:center;justify-content:center;flex-direction:column;gap:12px;"
      >
        <div
          style="width:40px;height:40px;border:3px solid rgba(167,139,250,.3);border-top-color:#a78bfa;border-radius:50%;animation:spin .7s linear infinite;"
        ></div>
        <span style="color:#1e293b;font-size:13px;font-family:Inter,sans-serif;">{{ 'screenshot.capturing' | translate }}</span>
      </div>
    }
  `,
})
export class ScreenshotOverlayComponent {
  readonly screenshot = inject(ScreenshotService);

  private isConfirmBtn(e: MouseEvent): boolean {
    return !!(e.target as HTMLElement | null)?.closest?.('#selectionConfirmBtn');
  }

  @HostListener('document:mousedown', ['$event'])
  onMouseDown(e: MouseEvent): void {
    if (this.isConfirmBtn(e)) return;
    if (!this.screenshot.overlayActive()) return;
    e.preventDefault();
    this.screenshot.selStart(e.clientX, e.clientY);
  }

  @HostListener('document:mousemove', ['$event'])
  onMouseMove(e: MouseEvent): void {
    this.screenshot.selMove(e.clientX, e.clientY);
  }

  @HostListener('document:mouseup', ['$event'])
  onMouseUp(e: MouseEvent): void {
    if (this.isConfirmBtn(e)) return;
    this.screenshot.selEnd(e.clientX, e.clientY);
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    if (this.screenshot.overlayActive()) this.screenshot.cancel();
  }
}
