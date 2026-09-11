"""Porta única de acesso ao banco de domínio e ao pack de configuração (config/).

O pack tem:
  config/settings.yml            — nome/descrição do app, dominio/idioma_default ativos,
                                    URL SQLAlchemy (ou config graphql) do banco
  config/<dominio>/<lang>/dominio.md — conhecimento de domínio daquele idioma, em
                                    seções canônicas '## ' (ver dominio_path())
  config/<dominio>/<lang>/dashboard_default.py — painéis padrão daquele idioma
                                    (ver dashboard_default_path())

Trocar o conteúdo de config/ re-aponta o sistema inteiro para outro banco.
A troca de dominio exige restart do app (os prompts são montados uma vez no init).
A troca de idioma NÃO exige restart — é resolvida por requisição (ver get_lang()/set_lang()).
"""
import contextvars
import os
import re
import threading
from pathlib import Path

_RAIZ = Path(__file__).parent
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", str(_RAIZ / "config")))

_SETTINGS_FILE = "settings.yml"
_DOMINIO_FILE_PADRAO = "dominio.md"

MSG_NAO_CONFIGURADO = (
    "Sistema sem banco configurado — crie config/settings.yml e config/dominio.md "
    "(veja ARQUITETURA.md)."
)


class ConfigError(RuntimeError):
    pass


# ── Cache por mtime ───────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict = {}  # path → (mtime, conteúdo)


def _read_cached(path: Path) -> "str | None":
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _cache_lock:
        hit = _cache.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        text = path.read_text(encoding="utf-8")
        recarga = hit is not None
        _cache[str(path)] = (mtime, text)
    print(f"[config] {'recarregado' if recarga else 'lido'}: {path}")
    return text


# ── settings.yml ──────────────────────────────────────────────────────────────

def _settings() -> dict:
    text = _read_cached(CONFIG_DIR / _SETTINGS_FILE)
    if text is None:
        return {}
    import yaml

    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


def _interpolar_env(texto: str) -> str:
    """Substitui ${VAR} pelo valor da variável de ambiente correspondente —
    segredos nunca precisam estar escritos no arquivo do pack."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), texto)


def _database_url() -> "str | None":
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    url = (_settings().get("database") or {}).get("url")
    if not url:
        return None
    url = _interpolar_env(url)
    # sqlite relativo → resolvido contra a raiz do projeto
    m = re.match(r"^sqlite:///(?!/)(.+)$", url)
    if m:
        url = f"sqlite:///{(_RAIZ / m.group(1)).as_posix()}"
    return url


_MODOS_VALIDOS = ("sql", "graphql")


def modo_acesso() -> str:
    """Método de acesso ao banco do domínio ativo: 'sql' ou 'graphql'.

    Definido em database.modo no settings.yml. Cada domínio usa só um dos
    dois — nunca ambos ao mesmo tempo. Default: 'sql' (mantém o comportamento
    original para packs que não declaram o campo).
    """
    modo = (_settings().get("database") or {}).get("modo") or "sql"
    return modo if modo in _MODOS_VALIDOS else "sql"


def graphql_config() -> "dict | None":
    """URL + credenciais do endpoint GraphQL do domínio ativo, ou None se não configurado.

    Dois esquemas de autenticação suportados (cada domínio usa só um):
      - token:   enviado como header Authorization: Bearer <token> (ex: posh)
      - api_key: enviado como header X-API-Key: <api_key> (ex: new_age)
    """
    bloco = _settings().get("graphql") or {}
    url = bloco.get("url")
    token = bloco.get("token")
    api_key = bloco.get("api_key")
    if not url or not (token or api_key):
        return None
    cfg = {"url": url}
    if token:
        cfg["token"] = _interpolar_env(token)
    if api_key:
        cfg["api_key"] = _interpolar_env(api_key)
    return cfg


def mes_inicio_fiscal() -> int:
    """Mês (1-12) em que o ano fiscal do domínio ativo começa — usado por
    calcular_periodo para resolver 'este/próximo/último quarter'. Default: 1
    (janeiro, ou seja trimestre civil — comportamento original para domínios que
    não declaram o campo). Domínios com ano fiscal diferente (ex: abril) declaram
    `app.mes_inicio_fiscal` em settings.yml.
    """
    mes = (_settings().get("app") or {}).get("mes_inicio_fiscal")
    try:
        mes = int(mes)
    except (TypeError, ValueError):
        return 1
    return mes if 1 <= mes <= 12 else 1


def app_nome() -> str:
    return (_settings().get("app") or {}).get("nome") or "Data Chat"


def app_descricao() -> str:
    return (_settings().get("app") or {}).get("descricao") or ""


def dominio_filename() -> str:
    """Nome do arquivo de domínio ativo, definido em settings.yml (app.dominio).

    Permite manter vários domínios na pasta (dominio_posh.md, dominio_alocacao.md, ...)
    e trocar o ativo mudando uma linha no settings.yml. Default: dominio.md.

    IMPORTANTE: este valor é gravado como chave de identidade (`dominio`) em várias
    tabelas do banco local (scheduled_tasks, dashboard_widgets, threshold_alerts,
    charts/pdfs/excels, task_code_audit, session_dominio — ver ARQUITETURA.md). NUNCA
    mude o formato do que esta função retorna: linhas já gravadas em produção ficariam
    órfãs (filtro `WHERE dominio=?` deixaria de encontrá-las). Para resolver caminhos de
    arquivo por idioma, use `dominio_path()`/`dashboard_default_path()` abaixo — elas
    derivam o slug internamente sem tocar neste valor.
    """
    return (_settings().get("app") or {}).get("dominio") or _DOMINIO_FILE_PADRAO


# ── Idioma ────────────────────────────────────────────────────────────────────
# Idioma é uma dimensão independente de dominio (ver dominio_filename() acima):
# dominio decide QUAL banco/negócio; idioma decide EM QUE LÍNGUA o conteúdo servido
# ao usuário (dominio.md, painéis default, docs de auto-descrição) é lido. Nenhuma
# tabela grava idioma como parte de uma chave de identidade — é só resolução de
# arquivo, por isso pode ser 100% dinâmico por requisição (ao contrário de dominio,
# que é fixo por processo).

IDIOMAS_SUPORTADOS = ("pt-BR", "en", "zh")

# Idioma padrão da INSTALAÇÃO (fallback de conteúdo quando o idioma pedido não tem
# arquivo traduzido ainda). Hoje pt-BR porque é o único idioma com conteúdo de
# domínio completo — vira "en" no settings.yml assim que a tradução em inglês
# estiver pronta de verdade. Independente do idioma default da UI (esse é decidido
# no front-end, ver LocaleService).
_IDIOMA_PADRAO_FALLBACK = "pt-BR"


def idioma_default() -> str:
    """Idioma de fallback desta instalação — settings.yml app.idioma_default."""
    idioma = (_settings().get("app") or {}).get("idioma_default") or _IDIOMA_PADRAO_FALLBACK
    return idioma if idioma in IDIOMAS_SUPORTADOS else _IDIOMA_PADRAO_FALLBACK


_lang_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar("db_config_lang", default="")


def set_lang(lang: "str | None") -> None:
    """Registra o idioma da requisição atual (propagado via contextvars — mesmo
    mecanismo de _current_user_id em agent_multi.py). Idioma inválido/vazio é ignorado
    silenciosamente; get_lang() cai no fallback da instalação nesse caso."""
    if lang and lang in IDIOMAS_SUPORTADOS:
        _lang_ctx.set(lang)


def get_lang() -> str:
    """Idioma efetivo: o da requisição atual, ou o default da instalação."""
    return _lang_ctx.get() or idioma_default()


def _dominio_slug() -> str:
    """dominio_new_age.md → new_age — nome da pasta em config/<slug>/<lang>/."""
    base = re.sub(r"\.md$", "", dominio_filename())
    base = re.sub(r"^dominio_?", "", base)
    return base or "default"


def _resolver_com_fallback(caminho_por_idioma) -> "Path | None":
    """Tenta o idioma pedido; se o arquivo não existir, cai no idioma default da
    instalação (com aviso); se nem esse existir, retorna None."""
    lang = get_lang()
    candidato = caminho_por_idioma(lang)
    if candidato.exists():
        return candidato
    fallback = idioma_default()
    if fallback != lang:
        candidato_fallback = caminho_por_idioma(fallback)
        if candidato_fallback.exists():
            print(f"[config] '{lang}' sem tradução em {candidato.name} — usando fallback '{fallback}'")
            return candidato_fallback
    return None


def dominio_path(lang: "str | None" = None) -> "Path | None":
    """Caminho do dominio.md no idioma efetivo (pedido, ou fallback da instalação).
    None se não existir em nenhum dos dois — quem chamar trata como 'sem conteúdo'."""
    if lang:
        set_lang(lang)
    slug = _dominio_slug()
    return _resolver_com_fallback(lambda l: CONFIG_DIR / slug / l / "dominio.md")


def dashboard_default_path(lang: "str | None" = None) -> "Path | None":
    """Caminho do dashboard_default.py no idioma efetivo. None se ausente — domínio
    sem arquivo (em nenhum idioma) fica sem painéis default, como antes."""
    if lang:
        set_lang(lang)
    slug = _dominio_slug()
    return _resolver_com_fallback(lambda l: CONFIG_DIR / slug / l / "dashboard_default.py")


_default_widgets_cache: dict = {}  # path → (mtime, widgets)


def dashboard_default_widgets(lang: "str | None" = None) -> "list[dict]":
    """Painéis padrão do domínio ativo, no idioma efetivo — a baseline que aparece no
    dashboard antes de qualquer painel criado via chat. Definidos em
    config/<dominio>/<lang>/dashboard_default.py, um arquivo Python com uma lista
    `WIDGETS` de dicts (mesmo formato usado por add_chart_to_dashboard): {id, title,
    description, code, range_unit, range_value}.

    Ao contrário dos painéis do chat (tabela dashboard_widgets), estes nunca tocam o
    banco — não são apagados pelo reset noturno de demo e são versionados no git junto
    com o resto do pack de domínio. Recarregado automaticamente quando o arquivo muda
    (cache por mtime, igual dominio.md). Arquivo ausente ou `WIDGETS` ausente → [].
    """
    path = dashboard_default_path(lang)
    if path is None:
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    with _cache_lock:
        hit = _default_widgets_cache.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
    namespace: dict = {}
    try:
        codigo = path.read_text(encoding="utf-8")
        exec(compile(codigo, str(path), "exec"), namespace)
    except Exception as e:
        print(f"[config] erro ao carregar {path}: {e}")
        return []
    widgets = namespace.get("WIDGETS") or []
    with _cache_lock:
        _default_widgets_cache[str(path)] = (mtime, widgets)
    print(f"[config] dashboard defaults recarregado: {path} ({len(widgets)} painéis)")
    return widgets


def is_configured() -> bool:
    if dominio_path() is None:
        return False
    if modo_acesso() == "graphql":
        return graphql_config() is not None
    return _database_url() is not None


# ── Engine SQLAlchemy (singleton lazy) ────────────────────────────────────────

_engine = None
_engine_url = None
_engine_lock = threading.Lock()


def get_engine():
    global _engine, _engine_url
    url = _database_url()
    if not url:
        raise ConfigError(MSG_NAO_CONFIGURADO)
    with _engine_lock:
        if _engine is None or _engine_url != url:
            from sqlalchemy import create_engine

            _engine = create_engine(url, pool_pre_ping=True)
            _engine_url = url
            print(f"[config] banco conectado: {_mask_url(url)}")
        return _engine


def dialect_name() -> str:
    try:
        return get_engine().dialect.name
    except Exception:
        return "desconhecido"


_DIALECT_HINTS = {
    "sqlite": "use date('now') para hoje e date('now', '-N days') para períodos passados; "
              "NUNCA use CURRENT_DATE, casts ::text ou TO_CHAR",
    "postgresql": "use CURRENT_DATE para hoje, CURRENT_DATE - INTERVAL 'N days' para períodos "
                  "passados, e casts ::text para enums",
    "mysql": "use CURDATE() para hoje e DATE_SUB(CURDATE(), INTERVAL N DAY) para períodos passados",
}


def dialect_hint() -> str:
    return _DIALECT_HINTS.get(dialect_name(), "siga a sintaxe SQL padrão do banco configurado")


def run_select(sql: str, params: "dict | None" = None) -> "list[dict]":
    """Executa um SELECT no banco configurado e retorna list[dict].

    Guarda SELECT-only centralizada — qualquer outra instrução é rejeitada.
    """
    normalized = sql.strip().lstrip("(").upper()
    if not normalized.startswith("SELECT"):
        raise ValueError("Apenas queries SELECT são permitidas. Instrução rejeitada.")
    from sqlalchemy import text

    engine = get_engine()  # levanta ConfigError se não configurado
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]


def _mask_url(url: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", url)


def url_mascarada() -> str:
    """URL com senha ocultada, para logs de startup."""
    url = _database_url()
    if not url:
        return "(não configurada)"
    return _mask_url(url)


# ── dominio.md — seções canônicas ─────────────────────────────────────────────

_SECOES_NAO_SKILL = ("Períodos", "Regras do agente", "Regras do orquestrador")


def _dominio_texto() -> str:
    path = dominio_path()
    if path is None:
        return ""
    return _read_cached(path) or ""


def _split_secoes(texto: str) -> "list[tuple[str, str]]":
    """Divide o markdown em (titulo, corpo) por headings de nível '## '.

    O preâmbulo (header de 4 linhas + qualquer texto antes do primeiro '## ')
    entra com título '' na primeira posição.
    """
    partes = re.split(r"(?m)^## +(.+?)\s*$", texto)
    secoes = [("", partes[0])]
    for i in range(1, len(partes), 2):
        secoes.append((partes[i].strip(), partes[i + 1]))
    return secoes


def get_secao(nome: str) -> str:
    """Retorna o corpo de uma seção '## <nome>' de dominio.md ('' se ausente)."""
    for titulo, corpo in _split_secoes(_dominio_texto()):
        if titulo.lower() == nome.lower():
            return corpo.strip()
    return ""


def skill_dominio_header() -> str:
    """Primeiras 4 linhas de dominio.md (formato '# skill:' etc.) para o catálogo."""
    linhas = _dominio_texto().splitlines()[:4]
    return "\n".join(linhas)


def skill_dominio_texto() -> str:
    """dominio.md completo MENOS as seções já injetadas nos prompts/engine
    (Períodos, Regras do agente, Regras do orquestrador) — evita duplicar tokens."""
    out = []
    for titulo, corpo in _split_secoes(_dominio_texto()):
        if titulo in _SECOES_NAO_SKILL:
            continue
        if titulo:
            out.append(f"## {titulo}\n{corpo.rstrip()}")
        else:
            out.append(corpo.rstrip())
    return "\n\n".join(s for s in out if s.strip())


def identidade() -> dict:
    """Nome/descrição do settings.yml + palavras-chave do header de dominio.md."""
    palavras = ""
    for linha in _dominio_texto().splitlines()[:4]:
        if linha.startswith("# palavras-chave:"):
            palavras = linha.split(":", 1)[1].strip()
            break
    return {"nome": app_nome(), "descricao": app_descricao(), "palavras_chave": palavras}


# ── '## Períodos' — machine-parsable ──────────────────────────────────────────

_PERIODO_RE = re.compile(
    r"(?m)^### +(.+?)\s*$\s*```sql\s*\n(.*?)\n```",
    re.DOTALL,
)


def periodos() -> "list[dict]":
    """Entradas parseadas de '## Períodos': [{header, is_regex, frases|regex, sql}].

    A ordem do arquivo é preservada — a primeira entrada que casar vence.
    """
    corpo = get_secao("Períodos")
    entradas = []
    for m in _PERIODO_RE.finditer(corpo):
        header, sql = m.group(1).strip(), m.group(2).strip()
        if header.startswith("re:"):
            entradas.append({"is_regex": True, "regex": header[3:].strip(), "sql": sql})
        else:
            frases = [_normalizar(f) for f in header.split("|")]
            entradas.append({"is_regex": False, "frases": frases, "sql": sql})
    return entradas


def _normalizar(frase: str) -> str:
    return re.sub(r"\s+", " ", frase.strip().lower())


def resolver_periodo(frase: str) -> "tuple[str, str] | None":
    """Casa a frase contra as entradas de '## Períodos' e executa a SQL da primeira
    que casar. Retorna (from_iso, to_iso) ou None (sem match / sem linha / erro —
    o chamador cai no fallback de calendário)."""
    if not is_configured():
        return None
    p = _normalizar(frase)
    for entrada in periodos():
        params = {}
        if entrada["is_regex"]:
            m = re.search(entrada["regex"], p, re.IGNORECASE)
            if not m:
                continue
            params = {f"g{i}": g for i, g in enumerate(m.groups(), start=1) if g is not None}
        else:
            if p not in entrada["frases"]:
                continue
        try:
            rows = run_select(entrada["sql"], params)
        except Exception:
            return None
        if not rows:
            return None
        vals = list(rows[0].values())
        if len(vals) < 2 or vals[0] is None or vals[1] is None:
            return None
        return str(vals[0])[:10], str(vals[1])[:10]
    return None
