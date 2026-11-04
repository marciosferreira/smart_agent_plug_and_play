"""Controle de acesso por papel (user_role) às tabelas, colunas e linhas do GraphQL.

POC que imita o modelo do repositório dos agentes: o JWT só identifica o usuário (id, email,
name — ver auth.py); o PAPEL vem de uma tabela de usuários→papéis (aqui, `users:` do
`config/<dominio>/acl.yml`; em produção seria a tabela `user_role`). A política de cada papel
diz que tabelas, colunas e linhas ele enxerga. Sem `acl.yml` (ou com `enabled: false`) o
módulo fica inativo e nada muda no comportamento do app.

Formato do `acl.yml`:

    enabled: true
    default_role: null            # papel de quem não está em `users` (null = nega tudo)
    users:
      "123": {role: admin, name: "Ana", email: "ana@example.com"}
    roles:
      admin:   {tables: "*"}      # todas as tabelas, todas as colunas, todas as linhas
      colaborador:
        tables:
          dim_site: {}            # tabela liberada, todas as colunas e linhas
          fact_hourly_rate:
            columns: {deny: [approved_at]}      # ou {allow: [id, site_id, ...]}
            rows: [{field: site_id, op: IN, value: [1, 2]}]   # AND com o where do usuário
    relations:                    # FK → tabela, para validar `expand` (fail-closed se faltar)
      fact_hourly_rate.site_id: dim_site

Tabelas fora da lista do papel são negadas (allowlist). Os pontos de aplicação são
`graphql_config.run_query_table/run_aggregate/run_schema`, que cobrem o chat, o sandbox das
tasks e o daemon. Limitação da POC: o `reportTable` não tem projeção de colunas, então as
colunas negadas são removidas das linhas DEPOIS da busca (nunca chegam ao LLM nem ao usuário);
no backend real isso tem que ser feito no resolver.
"""
import contextvars
from dataclasses import dataclass, field

import db_config

ACL_FILE = "acl.yml"

_usuario_ctx: contextvars.ContextVar["str | None"] = contextvars.ContextVar("acesso_usuario", default=None)


class AcessoNegado(RuntimeError):
    """O papel do usuário não permite a consulta pedida."""


def set_usuario(user_id: "str | None") -> None:
    """Fixa o usuário da execução atual (chat). As tasks passam o `user_id` explicitamente,
    porque rodam em threads que não herdam o ContextVar."""
    _usuario_ctx.set(str(user_id) if user_id else None)


def usuario_atual() -> "str | None":
    return _usuario_ctx.get()


# ── carga do acl.yml ──────────────────────────────────────────────────────────

def _carregar() -> dict:
    import yaml

    texto = db_config._read_cached(db_config.pack_dir() / ACL_FILE)
    if texto is None:
        return {}
    try:
        dados = yaml.safe_load(texto) or {}
    except yaml.YAMLError:
        return {}
    return dados if isinstance(dados, dict) else {}


def ativo() -> bool:
    return bool(_carregar().get("enabled"))


def usuarios() -> "dict[str, dict]":
    """Usuários conhecidos (id → {role, name, email}) — a 'tabela user_role' da POC."""
    brutos = _carregar().get("users") or {}
    return {str(k): (v or {}) for k, v in brutos.items()}


def usuario(user_id: "str | None") -> "dict | None":
    return usuarios().get(str(user_id)) if user_id else None


# ── política ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegraTabela:
    allow: "frozenset[str] | None" = None   # None = todas as colunas
    deny: frozenset = frozenset()
    rows: tuple = ()                        # filtros {field, op, value}

    def coluna_ok(self, coluna: str) -> bool:
        return (self.allow is None or coluna in self.allow) and coluna not in self.deny


@dataclass(frozen=True)
class Politica:
    role: "str | None"
    tudo: bool = False                      # tables: "*"
    tabelas: dict = field(default_factory=dict)       # nome → RegraTabela
    relacoes: dict = field(default_factory=dict)      # "tabela.coluna" → tabela alvo

    # -- consultas básicas
    def regra(self, tabela: str) -> "RegraTabela | None":
        if self.tudo:
            return RegraTabela()
        return self.tabelas.get(tabela)

    def _regra_ou_negar(self, tabela: str) -> RegraTabela:
        regra = self.regra(tabela)
        if regra is None:
            raise AcessoNegado(
                f"Acesso negado: o papel '{self.role or 'sem papel'}' não tem acesso à tabela '{tabela}'."
            )
        return regra

    def _checar_coluna(self, tabela: str, regra: RegraTabela, coluna: str, uso: str) -> None:
        if not regra.coluna_ok(coluna):
            raise AcessoNegado(
                f"Acesso negado: o papel '{self.role or 'sem papel'}' não pode usar a coluna "
                f"'{tabela}.{coluna}' ({uso})."
            )

    def _alvo_do_expand(self, tabela: str, via_coluna: str) -> str:
        alvo = self.relacoes.get(f"{tabela}.{via_coluna}")
        if alvo is None:
            raise AcessoNegado(
                f"Acesso negado: a relação '{tabela}.{via_coluna}' não está declarada na política; "
                "o expand não pode ser validado."
            )
        return alvo

    def _checar_expand(self, tabela: str, via_coluna: str, colunas: "list[str]") -> None:
        alvo = self._alvo_do_expand(tabela, via_coluna)
        regra_alvo = self._regra_ou_negar(alvo)
        if regra_alvo.rows:
            # O join roda no servidor e não dá para injetar o filtro de linha da tabela alvo.
            raise AcessoNegado(
                f"Acesso negado: '{alvo}' tem restrição de linhas para o papel "
                f"'{self.role or 'sem papel'}'; não use expand nela."
            )
        for c in colunas:
            self._checar_coluna(alvo, regra_alvo, c, "expand")

    def _where_efetivo(self, tabela: str, regra: RegraTabela, where: "list[dict] | None") -> "list[dict] | None":
        for cond in where or []:
            self._checar_coluna(tabela, regra, cond.get("field", ""), "filtro")
        if not regra.rows:
            return where
        return [*(where or []), *(dict(r) for r in regra.rows)]

    # -- reportTable
    def preparar_tabela(self, tabela, where, order_by, expand) -> "list[dict] | None":
        """Valida a consulta e devolve o `where` efetivo (com o filtro de linhas do papel)."""
        regra = self._regra_ou_negar(tabela)
        if order_by:
            self._checar_coluna(tabela, regra, order_by.get("field", ""), "ordenação")
        for ex in expand or []:
            self._checar_expand(tabela, ex.get("viaColumn", ""), list(ex.get("columns") or []))
        return self._where_efetivo(tabela, regra, where)

    def limpar_linhas(self, tabela: str, linhas: "list[dict]", expand: "list[dict] | None") -> "list[dict]":
        """Remove das linhas as colunas negadas (o reportTable devolve a linha inteira)."""
        regra = self._regra_ou_negar(tabela)
        if regra.allow is None and not regra.deny:
            return linhas
        return [{k: v for k, v in linha.items() if self._chave_visivel(tabela, regra, k, expand)} for linha in linhas]

    def _chave_visivel(self, tabela, regra, chave, expand) -> bool:
        if "__" in chave:  # coluna do expand: "viaColumn__coluna" (já validada antes da busca)
            return True
        return regra.coluna_ok(chave)

    # -- reportAggregate
    def preparar_agregado(self, tabela, group_by, metric_field, where, expand) -> "list[dict] | None":
        regra = self._regra_ou_negar(tabela)
        self._checar_coluna(tabela, regra, group_by, "agrupamento")
        self._checar_coluna(tabela, regra, metric_field, "métrica")
        if expand:
            self._checar_expand(tabela, group_by, list(expand))
        return self._where_efetivo(tabela, regra, where)

    # -- reportSchema e prompt
    def filtrar_schema(self, tabelas: "list[dict]") -> "list[dict]":
        saida = []
        for t in tabelas:
            regra = self.regra(t.get("table", ""))
            if regra is None:
                continue
            colunas = [c for c in t.get("columns", []) if regra.coluna_ok(c.get("column", ""))]
            saida.append({**t, "columns": colunas})
        return saida

    def descricao(self) -> str:
        if self.tudo:
            return f"Papel do usuário: {self.role} — acesso a todas as tabelas."
        if not self.tabelas:
            return f"Papel do usuário: {self.role or 'sem papel'} — SEM acesso a nenhuma tabela."
        linhas = [f"Papel do usuário: {self.role}. Só estas tabelas estão liberadas (as demais existem, mas "
                  "este usuário NÃO pode consultá-las — diga isso a ele em vez de tentar):"]
        for nome, r in self.tabelas.items():
            extra = []
            if r.allow is not None:
                extra.append("só as colunas " + ", ".join(sorted(r.allow)))
            if r.deny:
                extra.append("sem as colunas " + ", ".join(sorted(r.deny)))
            if r.rows:
                extra.append("somente linhas onde " + " e ".join(f"{x['field']} {x['op']} {x.get('value')}" for x in r.rows))
            linhas.append(f"- {nome}" + (f" ({'; '.join(extra)})" if extra else ""))
        return "\n".join(linhas)


def _regra_de(spec: "dict | None") -> RegraTabela:
    spec = spec or {}
    cols = spec.get("columns")
    allow = deny = None
    if isinstance(cols, dict):
        allow = frozenset(cols["allow"]) if cols.get("allow") else None
        deny = frozenset(cols.get("deny") or ())
    return RegraTabela(allow=allow, deny=deny or frozenset(), rows=tuple(spec.get("rows") or ()))


def politica_para(user_id: "str | None") -> "Politica | None":
    """Política do usuário, ou None se o controle de acesso está inativo (sem restrição).
    Usuário desconhecido cai no `default_role`; sem ele, num papel vazio que nega tudo."""
    dados = _carregar()
    if not dados.get("enabled"):
        return None
    info = usuario(user_id) or {}
    role = info.get("role") or dados.get("default_role")
    relacoes = dict(dados.get("relations") or {})
    spec = (dados.get("roles") or {}).get(role) if role else None
    if spec is None:
        return Politica(role=role, relacoes=relacoes)
    tabelas = spec.get("tables")
    if tabelas == "*":
        return Politica(role=role, tudo=True, relacoes=relacoes)
    return Politica(
        role=role,
        tabelas={nome: _regra_de(s) for nome, s in (tabelas or {}).items()},
        relacoes=relacoes,
    )


def bloco_prompt(user_id: "str | None" = None) -> str:
    """Texto com o que o usuário pode consultar, para o prompt; '' se o controle está inativo."""
    pol = politica_para(user_id or usuario_atual())
    return pol.descricao() if pol else ""
