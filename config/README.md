# Pack de configuração

Esta pasta define **qual banco** o chat IA analisa e **como ele entende o domínio**.
Trocar a URL e o arquivo de domínio ativo no `settings.yml` re-aponta o sistema inteiro
(chat, widgets do dashboard, tarefas agendadas, monitores). **Requer restart do app.**

## Arquivos

| Arquivo | Papel |
| --- | --- |
| `settings.yml` | Nome/descrição do app + `app.dominio` (arquivo de domínio ativo) + URL SQLAlchemy do banco (segredos via `${VAR}` do ambiente; env `DATABASE_URL` tem precedência) |
| `dominio_*.md` | Conhecimento de domínio, em seções canônicas de nível `##` (ver abaixo). A pasta pode ter vários — só o apontado por `app.dominio` fica ativo |
| `dominio_example.md` | **Modelo/template** para integrar um banco novo — copie para `dominio_<nome>.md` e preencha |
| `README.md` | Este arquivo — não é lido pelo engine |

## Domínios disponíveis nesta pasta

| Arquivo | Banco esperado (url) | Domínio |
| --- | --- | --- |
| `dominio_posh.md` | `postgresql://...` (schema brazil) | Pedidos de compra (Brazil Purchase Orders) |
| `dominio_alocacao.md` | `sqlite:///allocation.db` | Alocação de supply por Key Account (KA Allocation) |

⚠️ Ao trocar `app.dominio`, troque também `database.url` para o banco correspondente —
o arquivo de domínio descreve o schema daquele banco específico.

## Seções canônicas de `dominio.md`

- `## Regras do agente` — injetada no prompt do sub-agente analista
- `## Regras do orquestrador` — injetada no prompt do orquestrador
- `## Períodos` — machine-parsable: `### frases | separadas` ou `### re:<regex>` + bloco ```sql
  que retorna 1 linha com (from, to); ordem importa, primeira que casa vence
- `## Modelo de dados` — schema/joins/exemplos; servida por `rag_dados` e `read_skill('dominio.md')`
- `## Domínio de negócio` — regras de negócio/glossário; servida por `rag_dominio`
- `## Exemplos task_code` — anexada às regras do sandbox de tarefas agendadas
- `## Dica para condition_sql` — tabelas/colunas para criação de monitores

Headings internos das seções devem ser `###` (nunca `##`, que delimita seções).

## Este pack (KA Allocation)

- **Carga de dados**: `python load_ka_allocation.py` (raiz) — lê os dois xlsx de alocação
  e recria `allocation.db` (tabelas `ka_input_data` e `ka_deal_allocation`).
- **Widgets iniciais**: `python seed_widgets_allocation.py` (raiz) cria os 2 painéis
  padrão no dashboard (alocação por KA no quarter atual; faixas de WOI). Equivalente via
  chat: "adiciona ao dashboard um painel de alocação por Key Account no quarter atual" e
  "adiciona ao dashboard um painel com a distribuição de WOI por faixa de risco".
