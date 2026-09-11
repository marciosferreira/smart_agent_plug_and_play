# Capacidades do agente

O agente responde em linguagem natural sobre os dados do banco configurado (ver pack em
`config/` — nome, schema e regras de negócio vêm de lá).

## Arquitetura simples

| Camada | Responsabilidade |
|--------|-----------------|
| `dominio.md` (config/) | **Única skill de análise** — toda pergunta de dados usa SQL direto no banco configurado |

## Como o agente responde perguntas de dados

```
Usuário faz uma pergunta
      ↓
Orquestrador → consultar_analista()
      ↓
Sub-agente lê dominio.md (pack de configuração)
      ↓
Escreve e executa SQL no banco configurado
      ↓
Processa resultado com pandas
      ↓
Responde com tabela ou gráfico
```

## Tipos de análise suportados

**Agregações:** totais, médias, contagens por qualquer dimensão do schema

**Rankings:** top-N por qualquer métrica

**Comparações:** entre períodos, categorias ou grupos

**Cruzamentos:** qualquer combinação de tabelas via JOIN (chaves descritas em dominio.md)

**Conceitos:** perguntas sobre o significado de métricas e regras de negócio do domínio

## Formatos de saída

- Tabela markdown
- Gráfico (barra, linha, pizza, dispersão)
- PDF com análise e gráficos
- Planilha Excel para download
- Painel customizado persistente no dashboard

## Períodos aceitos em linguagem natural

"hoje", "ontem", "esta semana", "últimos N dias", "este mês", "mês passado", datas explícitas
(ex: "de 01/05 a 28/05") — além dos períodos específicos do domínio definidos no pack de
configuração (ex: quarters, rótulos de período próprios do negócio).

## Tarefas agendadas

O agente pode criar tarefas que rodam automaticamente.

**Relatórios periódicos:**

- "Toda segunda às 8h me manda o resumo de <métrica>"
- "Todo dia às 7h gera um PDF com <análise>"
- "Todo primeiro do mês exporta os dados para Excel"

**Monitores com alertas:**

- "Me avise se <métrica> passar de <N>"
- "Alerta quando <condição> acontecer"

Frequências: `once`, `daily`, `weekly`, `monthly`, `every_Xm`, `every_Xh`, `every_Xd`

**Gerenciamento:** "Liste as tarefas", "Pause a tarefa 003", "Delete a tarefa 001"
