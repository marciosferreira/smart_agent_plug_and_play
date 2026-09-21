"""
TaskContext — objeto injetado no namespace de execução das tasks de código.
Fornece acesso seguro à API local e helpers para salvar e gerar artifacts,
reutilizando as mesmas funções internas do agente.
"""

import io
from datetime import date, timedelta

import db_config


class TaskContext:
    # Teto de chamadas ao LLM por execução de tarefa (ctx.analisar) — protege contra loops.
    _MAX_ANALISES = 3

    def __init__(self, session_id: str, backend_url: str = "http://localhost:8000", user_id: str | None = None, is_test: bool = False, notify_enabled: bool = True, task_name: str = "", lang: str = ""):
        self.session_id = session_id
        self.user_id    = user_id
        self._backend_url = backend_url.rstrip("/")
        self._tokens: list[str] = []
        self._is_test = is_test
        self._notify_enabled = notify_enabled
        self._task_name = task_name
        self._lang = lang
        self._analises = 0
        self._notify_called = False
        self._alert_triggered: bool | None = None  # definido por ctx.alert() (monitor de condição composta)
        self._alert_prev_state = 0  # condition_state anterior; só notifica na transição 0→1
        self._test_alerts: list[dict] = []  # alertas capturados durante teste (não persistidos)

    # ── API local ─────────────────────────────────────────────────────────────

    _VALID_PREFIXES = ("/alerts",)

    def sql(self, query: str) -> "list[dict]":
        """Executa uma query SELECT no banco configurado (config/) e retorna lista de dicts.

        Só funciona em domínios com database.modo: sql (ver config/settings.yml) — em
        modo graphql, use graphql_table()/graphql_aggregate() em vez desta.

        O schema real (tabelas, colunas) está descrito em config/dominio.md.
        Apenas SELECT é permitido.

        Exemplo:
            rows = ctx.sql("SELECT categoria, COUNT(*) AS total FROM tabela GROUP BY categoria")
            df = pd.DataFrame(rows)
        """
        if db_config.modo_acesso() == "graphql":
            raise ValueError(
                "ctx.sql() não está disponível neste domínio (database.modo: graphql). "
                "Use ctx.graphql_table(table, ...) ou ctx.graphql_aggregate(table, ...) "
                "— ver seção 'Exemplos task_code' da skill de domínio."
            )
        return db_config.run_select(query)

    def graphql_table(
        self,
        table: str,
        where: "list[dict] | None" = None,
        order_by: "dict | None" = None,
        limit: "int | None" = None,
        offset: "int | None" = None,
        expand: "list[dict] | None" = None,
    ) -> "list[dict]":
        """Busca linhas cruas de uma tabela via GraphQL (reportTable). Equivalente,
        para domínios em database.modo: graphql, à tool executar_graphql do chat —
        mesmos argumentos, mesmo schema (ver config/dominio.md).

        Exemplo:
            rows = ctx.graphql_table(
                "fact_hourly_rate",
                where=[{"field": "status_approval", "op": "EQ", "value": "APPROVED"}],
                expand=[{"viaColumn": "site_id", "columns": ["name", "company"]}],
                limit=500,
            )
            df = pd.DataFrame(rows)
        """
        import graphql_config
        return graphql_config.run_query_table(table, where=where, order_by=order_by, limit=limit, offset=offset, expand=expand)

    def graphql_aggregate(
        self,
        table: str,
        group_by: str,
        metric_field: str,
        metric_op: str,
        where: "list[dict] | None" = None,
        expand: "list[str] | None" = None,
        order_by: "str | None" = None,
        limit: "int | None" = None,
    ) -> "list[dict]":
        """Agrega uma tabela via GraphQL (reportAggregate — GROUP BY + SUM/COUNT/AVG/MIN/MAX
        calculados no servidor). Equivalente, para domínios em database.modo: graphql, à
        tool executar_graphql_agregado do chat — mesmos argumentos, mesmo schema.

        Retorna list[dict] com chaves "groupKey", "metricValue" e uma por coluna de `expand`.

        Exemplo:
            rows = ctx.graphql_aggregate(
                "fact_final_dl_cost", group_by="site_id",
                metric_field="dl_cost_by_cm_usd", metric_op="SUM",
                expand=["name", "country"], order_by="DESC", limit=20,
            )
            df = pd.DataFrame(rows)
        """
        import graphql_config
        return graphql_config.run_aggregate(
            table, group_by=group_by, metric_field=metric_field, metric_op=metric_op,
            where=where, expand=expand, order_by=order_by, limit=limit,
        )

    def api(self, path: str) -> any:
        """GET na API local. path deve começar com '/'.
        Retorna o JSON deserializado (dict ou list)."""
        import requests
        from urllib.parse import urlparse
        # tolerância: se vier URL completa, extrai só o path+query
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        base = path.split("?")[0].rstrip("/")
        if not any(base == p or base.startswith(p + "/") for p in self._VALID_PREFIXES):
            valid = ", ".join(self._VALID_PREFIXES)
            raise ValueError(
                f"Endpoint '{base}' não existe na API. "
                f"Endpoints válidos: {valid}"
            )
        url = self._backend_url + path
        res = requests.get(url, timeout=30)
        res.raise_for_status()
        return res.json()

    def today(self) -> str:
        """Retorna a data de hoje no formato YYYY-MM-DD. Use para tarefas de 'hoje'."""
        return date.today().isoformat()

    def date_range(self, days: int = 7) -> tuple:
        """Retorna (from_date, to_date) como objetos date para os últimos N dias.
        Use str(from_date) ou from_date.strftime(...) conforme necessário."""
        end   = date.today()
        start = end - timedelta(days=days - 1)
        return start, end

    # ── Análise narrativa (LLM) ───────────────────────────────────────────────

    def analisar(self, fatos, foco: str = "", fallback: str = "") -> str:
        """Pede ao LLM uma análise em texto (markdown) a partir de fatos JÁ CALCULADOS.

        O código calcula os números (totais, participação, ranking, variações, contagens…);
        o LLM só os interpreta: resumo, achados, padrões, anomalias e limitações. Ele é
        instruído a usar apenas os números fornecidos, e o texto devolvido é conferido — todo
        número citado precisa constar nos `fatos`.

        Args:
            fatos: o que analisar — str, dict/list (JSON), DataFrame, ou um dict
                   {'Seção': str|dict|DataFrame}. Inclua percentuais, razões e variações
                   já calculados; o LLM não deve calcular números novos.
            foco: (opcional) o que o usuário quer enfatizar (ex: 'concentração de custo').
            fallback: texto usado se o LLM falhar, exceder o teto de chamadas ou citar números
                      que não estão nos fatos — o relatório sai mesmo assim. Passe a análise
                      determinística (bullets calculados no código).

        Returns:
            Markdown com títulos '## ' e listas '- ', pronto para o `conteudo` de generate_pdf().
        """
        import logging
        log = logging.getLogger(__name__)
        if self._analises >= self._MAX_ANALISES:
            log.warning("[ctx.analisar] teto de %d chamadas por execução atingido — usando fallback", self._MAX_ANALISES)
            return fallback
        self._analises += 1
        try:
            from agent_multi import gerar_analise_relatorio
            texto = gerar_analise_relatorio(fatos, foco=foco, lang=self._lang)
        except Exception as e:  # o relatório não pode deixar de sair por falha do LLM
            log.warning("[ctx.analisar] falhou (%s: %s) — usando fallback", type(e).__name__, e)
            return fallback
        return texto or fallback

    # ── Gerar artifacts (reutilizam funções existentes do agente) ─────────────

    def generate_pdf(self, titulo: str, conteudo: str) -> str:
        """Gera PDF a partir de conteúdo markdown com tokens [chart:uuid].

        Usa a mesma engine (fpdf2) e estilos que o agente usa — suporta
        títulos, tabelas markdown, bullet points e tokens [chart:uuid] embutidos.

        Args:
            titulo: Título do relatório (ex: 'Relatório de Produção — Maio 2026').
            conteudo: Conteúdo em markdown. Tokens [chart:uuid] são substituídos
                      pelas imagens correspondentes.

        Returns:
            Token '[pdf:uuid]' para incluir na resposta.
        """
        from agent_multi import _build_pdf
        pdf_id = _build_pdf(titulo, conteudo, self.session_id, self.user_id)
        token = f"[pdf:{pdf_id}]"
        self._tokens.append(token)
        return token

    def generate_excel(self, df_or_sheets, nome_arquivo: str = "relatorio") -> str:
        """Gera Excel (.xlsx) com formatação padrão (cabeçalho destacado, auto-width).

        Usa a mesma formatação que o agente usa internamente.

        Args:
            df_or_sheets: Um DataFrame para aba única, ou dict {'Nome da Aba': df}
                          para múltiplas abas.
            nome_arquivo: Nome do arquivo sem extensão (ex: 'producao_semanal').

        Returns:
            Token '[excel:uuid]' para incluir na resposta.
        """
        import re
        import pandas as pd
        import chart_store
        from agent_multi import _fmt_excel_sheet

        def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
            # Excel não suporta datetimes com timezone (ex.: timestamptz do Postgres).
            for col in df.columns:
                if isinstance(df[col].dtype, pd.DatetimeTZDtype):
                    df[col] = df[col].dt.tz_localize(None)
            return df

        output = io.BytesIO()

        if isinstance(df_or_sheets, dict):
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                for aba, df in df_or_sheets.items():
                    df = _strip_tz(df)
                    df.to_excel(writer, sheet_name=aba[:31], index=False)
                    _fmt_excel_sheet(writer.sheets[aba[:31]], df)
        else:
            df_or_sheets = _strip_tz(df_or_sheets)
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df_or_sheets.to_excel(writer, sheet_name="Dados", index=False)
                _fmt_excel_sheet(writer.sheets["Dados"], df_or_sheets)

        safe_name = re.sub(r'[^\w\- ]', '', nome_arquivo)[:40].strip().replace(" ", "_")
        filename = f"{safe_name or 'relatorio'}.xlsx"
        excel_id = chart_store.save_excel(self.session_id, output.getvalue(), filename, self.user_id)
        token = f"[excel:{excel_id}]"
        self._tokens.append(token)
        return token

    # ── Salvar artifacts raw (controle total) ─────────────────────────────────

    def save_chart(self, fig, dpi: int = 150) -> str:
        """Salva figura matplotlib como PNG. Retorna token '[chart:uuid]'."""
        import chart_store
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        buf.seek(0)
        chart_id = chart_store.save_chart(self.session_id, buf.read(), self.user_id)
        token = f"[chart:{chart_id}]"
        self._tokens.append(token)
        import matplotlib.pyplot as plt
        plt.close(fig)
        return token

    def save_excel(self, wb, filename: str = "relatorio.xlsx") -> str:
        """Salva workbook openpyxl como XLSX com controle total. Retorna token '[excel:uuid]'.

        Use quando precisar de formatação personalizada além do padrão.
        Para uso simples com DataFrame, prefira generate_excel().
        """
        import chart_store
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        excel_id = chart_store.save_excel(self.session_id, buf.read(), filename, self.user_id)
        token = f"[excel:{excel_id}]"
        self._tokens.append(token)
        return token

    def save_pdf(self, html: str, filename: str = "relatorio.pdf") -> str:
        """Gera PDF a partir de HTML completo (WeasyPrint). Retorna token '[pdf:uuid]'.

        Use quando precisar de layout HTML/CSS personalizado.
        Para uso simples com markdown, prefira generate_pdf().
        """
        import chart_store
        from weasyprint import HTML
        pdf_bytes = HTML(string=html).write_pdf()
        pdf_id = chart_store.save_pdf(self.session_id, pdf_bytes, filename, self.user_id)
        token = f"[pdf:{pdf_id}]"
        self._tokens.append(token)
        return token

    # ── Notificações ─────────────────────────────────────────────────────────

    def notify(self, message: str, value: float | None = None, threshold: float | None = None) -> None:
        """Dispara uma notificação visível no sino (🔔) do dashboard.

        Use para qualquer condição que mereça atenção do usuário:
        - Status de equipamento ('Linha 1 está Inoperante')
        - Threshold numérico ('OEE em 71% — abaixo de 80%')
        - Contagem ('3 ordens atrasadas')
        - Qualquer regra de negócio personalizada

        Cada chamada gera uma notificação independente.

        Args:
            message: Texto da notificação (ex: 'Linha 2 — status: Inoperante').
            value: Valor numérico observado (opcional, exibido no painel).
            threshold: Valor de referência (opcional, exibido no painel).
        """
        self._notify_called = True
        if not self._notify_enabled:
            return
        if self._is_test:
            # Teste: captura sem persistir no painel de alertas
            self._test_alerts.append({"message": message, "value": value, "threshold": threshold})
            return
        import chart_store
        chart_store.save_alert(self.session_id, message, value, threshold, user_id=self.user_id)

    def alert(self, triggered: bool, message: str, value: float | None = None, threshold: float | None = None) -> None:
        """Sinaliza o resultado de um monitor de condição composta (AND/OR/várias métricas).

        Chame UMA vez por execução, com o resultado da condição inteira — `triggered` é
        True só quando a condição composta é verdadeira. A notificação é disparada apenas
        na transição de não-atingida para atingida (não repete a cada verificação enquanto
        a condição continua verdadeira); o estado é controlado pelo daemon.

        Args:
            triggered: Resultado booleano da condição completa (ex: a > 50 and b < 10).
            message: Texto da notificação, usado quando triggered é True.
            value: Valor numérico observado (opcional, exibido no painel).
            threshold: Valor de referência (opcional, exibido no painel).
        """
        self._alert_triggered = bool(triggered)
        # Impede a auto-notificação do runner (que dispararia mesmo com triggered=False)
        self._notify_called = True
        if self._alert_triggered and self._alert_prev_state == 0:
            self.notify(message, value, threshold)

    def notify_alert(self, message: str, value: float | None = None, threshold: float | None = None) -> None:
        """Alias de notify() — mantido para compatibilidade com task_codes existentes."""
        self.notify(message, value, threshold)

    # ── Resultado ─────────────────────────────────────────────────────────────

    def tokens(self) -> list[str]:
        return list(self._tokens)

    def test_alerts(self) -> list[dict]:
        """Alertas capturados durante teste (não foram salvos no painel)."""
        return list(self._test_alerts)
