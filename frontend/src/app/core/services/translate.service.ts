import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { LocaleService } from './locale.service';

type Dict = Record<string, unknown>;

/**
 * Tradução de UI em runtime — carrega public/i18n/<lang>.json (servido em
 * /i18n/<lang>.json, convenção Angular 19 de assets estáticos) uma vez no
 * bootstrap, mesmo padrão de carregamento único que BrandingService.load() já
 * usa pra /app-info — e expõe um signal com o dicionário atual. Chaves usam
 * notação de ponto (ex: "chat.send") resolvida contra objetos aninhados no JSON.
 *
 * Sem lib externa — só isso e o TranslatePipe (translate.pipe.ts) cobrem o caso de
 * uso do app: nenhuma pluralização/interpolação complexa é necessária hoje.
 */
@Injectable({ providedIn: 'root' })
export class TranslateService {
  private readonly http = inject(HttpClient);
  private readonly locale = inject(LocaleService);

  private readonly _dict = signal<Dict>({});
  readonly ready = signal(false);

  async load(): Promise<void> {
    try {
      const dict = await firstValueFrom(this.http.get<Dict>(`/i18n/${this.locale.lang()}.json`));
      this._dict.set(dict || {});
    } catch (e) {
      console.error('i18n load:', e);
      this._dict.set({});
    } finally {
      this.ready.set(true);
    }
  }

  /** Resolve "a.b.c" contra o dicionário carregado; sem match, devolve a própria chave
   * (visível o bastante em dev para apontar a tradução faltando, sem quebrar a UI). */
  t(key: string, params?: Record<string, string | number | null | undefined>): string {
    const parts = key.split('.');
    let node: unknown = this._dict();
    for (const p of parts) {
      if (node && typeof node === 'object' && p in (node as Dict)) {
        node = (node as Dict)[p];
      } else {
        return key;
      }
    }
    if (typeof node !== 'string') return key;
    if (!params) return node;
    return node.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? `{${name}}`));
  }
}
