"""Porta única de acesso ao banco de domínio e ao pack de configuração (config/).

O pack tem 2 arquivos:
  config/settings.yml — nome/descrição do app + URL SQLAlchemy do banco
  config/dominio.md   — todo o conhecimento de domínio, em seções canônicas '## '

Trocar o conteúdo de config/ re-aponta o sistema inteiro para outro banco.
A troca exige restart do app (os prompts são montados uma vez no init).
"""
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
    """
    return (_settings().get("app") or {}).get("dominio") or _DOMINIO_FILE_PADRAO


def is_configured() -> bool:
    if not (CONFIG_DIR / dominio_filename()).exists():
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
    return _read_cached(CONFIG_DIR / dominio_filename()) or ""


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
