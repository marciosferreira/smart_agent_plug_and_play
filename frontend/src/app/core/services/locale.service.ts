import { Injectable, signal } from '@angular/core';

/**
 * Idioma ativo do front-end — mesmo padrão de UserSessionService (user-session.service.ts):
 * o app host (embed) manda ?lang=... na URL, que sempre vence sobre o valor salvo;
 * sem host (standalone/demo), cai de volta para o valor salvo em localStorage, ou
 * o default abaixo. Trocável em runtime — nenhum rebuild do Angular é necessário
 * para adicionar um idioma novo, só o JSON de tradução (ver TranslateService) e o
 * conteúdo de domínio correspondente em config/<dominio>/<lang>/ no backend.
 */
export const IDIOMAS_SUPORTADOS = ['en', 'pt-BR', 'zh'] as const;
export type Idioma = (typeof IDIOMAS_SUPORTADOS)[number];

const IDIOMA_PADRAO: Idioma = 'en';
const STORAGE_KEY = 'mfg_lang';

function isIdiomaSuportado(v: string | null): v is Idioma {
  return !!v && (IDIOMAS_SUPORTADOS as readonly string[]).includes(v);
}

@Injectable({ providedIn: 'root' })
export class LocaleService {
  private readonly _lang = signal<Idioma>(IDIOMA_PADRAO);
  readonly lang = this._lang.asReadonly();

  constructor() {
    const hostLang = new URLSearchParams(location.search).get('lang');
    const saved = localStorage.getItem(STORAGE_KEY);
    const resolved = isIdiomaSuportado(hostLang) ? hostLang : isIdiomaSuportado(saved) ? saved : IDIOMA_PADRAO;
    localStorage.setItem(STORAGE_KEY, resolved);
    this._lang.set(resolved);
  }

  /** Troca o idioma ativo e recarrega a página — mesmo padrão de UserSessionService.switchUser(). */
  setLang(lang: Idioma): void {
    if (lang === this._lang()) return;
    localStorage.setItem(STORAGE_KEY, lang);
    location.reload();
  }
}
