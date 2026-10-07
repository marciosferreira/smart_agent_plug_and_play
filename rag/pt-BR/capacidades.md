# Capacidades do agente

O agente responde em linguagem natural (português, inglês ou chinês) sobre os dados do domínio
configurado (ver pack em `config/` — nome, schema e regras de negócio vêm de lá). Ele só lê
dados: nunca altera o banco consultado.

## Arquitetura simples

| Camada | Responsabilidade |
|--------|-----------------|
| `dominio.md` (config/) | **Única skill de análise** — descreve o schema e as regras do domínio; as consultas vão direto ao banco configurado, por SQL ou por GraphQL, conforme o domínio |

## Como o agente responde perguntas de dados

```text
Usuário faz uma pergunta
      ↓
Orquestrador → consultar_analista()
      ↓
Sub-agente analista (com a skill dominio.md já carregada)
      ↓
Consulta o banco configurado (SQL ou GraphQL, conforme o domínio)
      ↓
Processa resultado com pandas
      ↓
Responde com tabela e/ou gráfico
```

## Tipos de análise suportados

**Agregações:** totais, médias, contagens por qualquer dimensão do schema

**Rankings:** top-N por qualquer métrica

**Comparações:** entre períodos, categorias ou grupos

**Cruzamentos:** combinação de tabelas pelas chaves descritas em dominio.md (JOIN em domínios
SQL; em domínios GraphQL, junção de um nível por consulta e cruzamento em pandas)

**Conceitos:** perguntas sobre o significado de métricas e regras de negócio do domínio

## Formatos de saída

- Tabela markdown
- Gráfico (barra, linha, pizza, dispersão, entre outros)
- PDF com análise e gráficos
- Planilha Excel para download
- Painel customizado persistente no dashboard

Sem pedido explícito de gráfico, a resposta de dados vem em tabela (ou em texto, se o resultado
for um único valor). Peça "gráfico" para ver a visualização, ou "gráfico e tabela" para os dois.

## Períodos aceitos em linguagem natural

"hoje", "ontem", "esta semana", "semana passada", "últimos N dias/semanas/meses", "este mês",
"mês passado", "este ano", "ano passado", "este quarter", "último quarter", "todo o período",
um mês específico (ex: "maio de 2026") e datas explícitas (ex: "de 01/05/2026 a 31/05/2026")
— além dos períodos específicos do domínio definidos no pack de configuração (ex: quarters e
ano fiscal do negócio, rótulos de período próprios).

## Depois da resposta

É possível pedir ajustes ("o mesmo gráfico, mas em cinza", "só os 5 maiores") e fazer perguntas
sobre um gráfico já gerado ("por que há um pico aqui?") — o agente consegue olhar a imagem.
Toda resposta de dados traz um bloco **Parâmetros da análise** com o período e os filtros
realmente usados, indicando o que foi assumido.

## Dashboard

O dashboard é uma grade de painéis (gráficos que se recalculam a cada atualização):

- **Painéis padrão** — vêm prontos com o pack do domínio e não podem ser removidos
- **Painéis customizados** — o agente cria, edita e remove sob pedido, ex: "adiciona ao
  dashboard um painel de <métrica> por <dimensão> dos últimos 30 dias" (ele confirma antes
  de fixar)

Tipos de gráfico: barra, linha, pizza, rosca, radar, área polar, dispersão, bolhas e mapa de
calor. Os painéis podem ser reordenados arrastando, e o dono de um painel customizado pode
compartilhá-lo com os outros usuários pelo botão do próprio painel.

Perguntas sobre a tela: o agente enxerga os alertas, artefatos e tarefas listados e analisa
recortes de tela que você anexar ao chat (botão "Selecionar área da tela", até 3 imagens).

## Conversa

- **Voz:** grave um áudio, ouça e descarte ou envie; ele é transcrito e a transcrição segue
  como sua mensagem
- **Parar:** o botão "Parar resposta" interrompe uma resposta em andamento
- **Conversas:** cada usuário tem várias conversas, que podem ser retomadas depois
- **Idioma:** o agente responde no idioma escolhido na interface (português, inglês ou chinês)

## Tarefas agendadas

O agente pode criar tarefas que rodam automaticamente. Ao criar, ele consulta os dados reais,
escreve o código da tarefa, testa e só então salva (se o teste falhar, tenta corrigir; se não
conseguir, a tarefa não é criada). Depois disso a tarefa roda no horário sem precisar do agente
— o resultado é calculado por código, direto dos dados (a única exceção é a análise escrita,
quando pedida).

**Relatórios periódicos:**

- "Toda segunda às 8h gera o resumo de <métrica>"
- "Todo dia às 7h gera um PDF com <análise>"
- "Todo primeiro do mês exporta os dados para Excel"
- "Toda semana gera um relatório com análise e insights" — o código calcula os números e a IA
  escreve a interpretação, que é conferida contra esses números

Os resultados (PDF, gráfico, Excel) aparecem no painel **Artefatos**. O sistema não envia
e-mails por conta própria.

**Monitores com alertas:**

- "Me avise se <métrica> passar de <N>" — condição simples (uma métrica, uma comparação)
- "Alerta quando <métrica A> > <X> e <métrica B> < <Y>" — condição composta (E/OU, várias
  métricas ou entidades)

Os alertas aparecem no sino de **Alertas** do dashboard. O aviso sai quando a condição passa de
"não atingida" para "atingida" — não se repete a cada verificação — e volta a valer depois que
ela deixa de ser verdadeira. Sem frequência pedida, o monitor verifica a cada 5 minutos
(intervalo mínimo padrão: 5 minutos). Ao criar um monitor, o agente mostra **Como interpretei
o pedido** (condição, frequência, janela de dados, métrica e agregação escolhidas) para você
corrigir o que estiver diferente do que queria.

Frequências: `once`, `daily`, `weekly`, `monthly`, `every_Xm`, `every_Xh`, `every_Xd`

**Gerenciamento:** "Liste as tarefas", "Pause a tarefa 003", "Retome a tarefa 003", "Delete a
tarefa 001", "Edite a tarefa 003: mude o limite para 50" (o agente altera o código, testa e
salva, guardando a versão anterior). No painel **Tarefas** também dá para executar agora (▶),
ligar ou desligar notificações (exceto em monitores, que sempre notificam), ver o código e o
histórico de execuções e editar via chat.

## Limites

- Só lê dados: não altera o banco do domínio
- Não inventa dados: sem dado real para o filtro pedido, o agente diz isso (e pode mostrar o
  mais próximo, deixando claro que é diferente do pedido)
- Resultados muito grandes são resumidos em vez de exibidos por inteiro — peça um top-N ou
  uma agregação
- Não envia e-mails por conta própria
