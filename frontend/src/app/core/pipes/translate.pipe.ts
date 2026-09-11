import { Pipe, PipeTransform, inject } from '@angular/core';
import { TranslateService } from '../services/translate.service';

/**
 * {{ 'chat.send' | translate }} — impuro de propósito: TranslateService.load() é
 * assíncrono (fetch do JSON), então o dicionário muda de vazio → carregado depois
 * da primeira detecção de mudança. Pipe puro travaria no valor (a chave) calculado
 * antes do load terminar; overhead de reavaliar é desprezível no tamanho deste app.
 */
@Pipe({ name: 'translate', pure: false, standalone: true })
export class TranslatePipe implements PipeTransform {
  private readonly translate = inject(TranslateService);

  transform(key: string, params?: Record<string, string | number | null | undefined>): string {
    return this.translate.t(key, params);
  }
}
