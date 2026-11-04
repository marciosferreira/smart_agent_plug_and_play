# Principais atividades de AI do Capex (PoC)

O agente de AI do CAPEX atua como uma camada de inteligência sobre os dados e o conhecimento do domínio. A partir de uma instrução em linguagem natural — digitada ou dita —, um **orquestrador** interpreta o pedido e delega o trabalho especializado a **sub-agentes**: o analista consulta o banco do domínio em tempo real (SQL ou GraphQL), processa os dados em um sandbox Python e entrega texto, gráficos, planilhas Excel ou relatórios em PDF, transmitidos ao usuário ao vivo por SSE. Em paralelo, um **daemon de agendamento** executa relatórios e monitores de threshold cujo código o próprio agente gerou e validou, e o agente fixa painéis no dashboard.

A arquitetura é plug-and-play: tudo o que o agente sabe sobre qual banco consultar e como entender o domínio (aqui, o CAPEX) vem de um pack de configuração lido em runtime, e não do código. O objetivo é que cada resposta seja fundamentada em dados reais, sem dado inventado e sem depender de um analista disponível.

As principais atividades realizadas foram:

- **Arquitetura agêntica com LangGraph**: orquestrador com cerca de 20 tools, sub-agentes especializados (analista e agendamento) e pipelines determinísticos, com o roteamento decidido pelo próprio agente a cada passo.
- **Integração e configuração dos modelos Gemini (Vertex AI)**: um modelo único em quatro papéis, com temperatura e nível de raciocínio próprios, e extensão do cliente para suportar o Gemini 3.
- **Conhecimento de domínio plug-and-play**: pack de configuração por domínio, tools de consulta (`rag_*`) e skills carregadas sob demanda, sem embeddings nem banco vetorial.
- **Estruturação da base de conhecimento do CAPEX**: documentação funcional consolidada em um único arquivo Markdown, dividido por requisito funcional e lido apenas na parte necessária, preservando ambiguidades e inconsistências da especificação.
- **Consulta a dados em tempo real, análise em sandbox Python e proteção de contexto**: SQL ou GraphQL, namespace persistente de DataFrames e limites sobre o que chega ao modelo.
- **Gráficos, PDFs e Excels como artefatos persistentes**: o modelo trabalha com tokens, e o backend os resolve em imagem ou link de download.
- **Síntese da resposta e controle de alucinação**: o analista devolve um relatório técnico, e o orquestrador redige a resposta final usando apenas os números do retorno.
- **Guardrails e segurança**: escopo por prompt, banco somente leitura, sandbox para o código gerado e regras críticas impostas em código.
- **Prompts e contexto temporal**: prompts compostos a partir do pack e resolução de períodos em linguagem natural por meio de tools.
- **Streaming SSE e cancelamento**: eventos em tempo real, side-channel para os sub-agentes e cancelamento cooperativo.
- **Agendamento de relatórios e monitores de threshold**: pipeline que gera, testa e salva o código; daemon que o executa sem LLM; alertas no dashboard.
- **Painéis do dashboard**: painéis padrão do domínio e customizados por conversa, renderizados em Chart.js a partir de código Python executado sob demanda.
- **Interação por voz e recortes de tela**: transcrição via Gemini e perguntas sobre o que está na tela.

## Atividade 1: Arquitetura agêntica com LangGraph

### Descrição

Foi construída a arquitetura agêntica do sistema sobre o LangGraph (StateGraph, ToolNode e checkpointer SqliteSaver), organizada em dois níveis. No primeiro, um **orquestrador** conduz a conversa: a cada passo, o próprio modelo decide se responde diretamente ou chama uma tool, e o ciclo se repete até que o objetivo do turno seja atingido. No segundo, **sub-agentes especializados** rodam dentro de tools do orquestrador: o **analista**, que consulta e analisa dados, e o de **agendamento**, que lista, pausa, retoma, apaga e altera tarefas. A criação e a edição de tarefas agendadas seguem pipelines determinísticos (SchedulingGraph), descritos na Atividade 11.

O orquestrador dispõe de cerca de 20 tools: delegação ao analista (`consultar_analista`), resolução de períodos e de data/hora, leitura do que está no dashboard, consulta de conhecimento (`rag_*`), geração de PDF e Excel, agendamento e gestão de painéis.

Não há um nó classificador separado. O prompt do orquestrador define seis categorias de pedido, e a decisão se concretiza quando o próprio modelo escolhe a tool a chamar (a primeira chamada de cada turno tem um papel próprio, o de roteamento, descrito na Atividade 2):

| Categoria | Pedido | Caminho |
| --- | --- | --- |
| A | Consulta pontual de dados | `consultar_analista`, com o período em linguagem natural e `tipo` tabela (padrão), gráfico ou ambos |
| B | Pergunta conceitual (o que é X, como se calcula Y) | Tools `rag_*`, sem buscar dados |
| C | Agendamento (criar, editar, listar, pausar, apagar) | `criar_tarefa_agendada`, `editar_tarefa_agendada` ou `gerenciar_agenda` |
| D | Pergunta simples (data, hora, saudação) | Resposta direta do orquestrador |
| E | Pergunta sobre o que está na tela (texto ou imagem anexada) | `get_dashboard_charts()` e, se preciso, `rag_dominio()` |
| F | Painel customizado no dashboard (adicionar, fixar, editar, remover) | Confirma a intenção, descobre os dados com o analista, valida com `test_widget_code` e salva com `add_chart_to_dashboard` |

Na dúvida entre A e C/F, vale A: nunca se cria tarefa nem painel sem palavras explícitas de agendamento ou de inserção no dashboard.

### Justificativa

Um agente baseado em tools, em vez de um pipeline de etapas fixas, permite que o modelo decida quantos passos cada pedido exige: uma consulta simples pode terminar em uma chamada, enquanto uma análise com gráfico encadeia consulta, processamento e geração do artefato. A separação em níveis é deliberada: o orquestrador não sabe analisar dados, só sabe a quem perguntar; o analista não tem acesso ao histórico da conversa e recebe apenas o pedido pontual delegado. Cada camada faz uma coisa, com prompt, temperatura e nível de raciocínio próprios, e o contexto de cada uma permanece pequeno.

Os pipelines determinísticos são usados onde o comportamento não pode depender de o modelo decidir o próximo passo, como na criação de tarefas agendadas, em que o código gerado precisa ser testado e corrigido antes de ser salvo.

### Principais desafios

- **Separar intenção de consulta de intenção de ação.** Criar uma tarefa ou fixar um painel altera o estado do sistema. Por isso o desempate do roteamento favorece a consulta, a criação de painel exige confirmação do usuário e as regras que o modelo não seguia de forma consistente passaram a ser impostas em código. Por exemplo, um monitor sem frequência explícita recebe `every_5m` por regra de código, porque o modelo às vezes escolhia “diário” por conta própria.
- **Compartilhar contexto entre threads.** As tools do LangGraph executam em threads de um pool, onde `threading.local` não funciona. O usuário, a sessão e o handle de cancelamento são propagados por `ContextVar` (re-setados explicitamente onde o streaming sobe uma thread própria), e os dados de sessão ficam em dicts globais indexados por `session_id`.
- **Controlar o histórico.** O checkpointer persiste a conversa por `session_id` no mesmo `mfg.db` do app, mas o orquestrador envia ao modelo apenas as últimas `MAX_INTERACOES × 2` mensagens (padrão: 10 interações). O histórico completo permanece no banco, e a janela ativa é limitada para conter custo, latência e ruído.
- **Montar o agente uma única vez.** A identidade do app, as regras do orquestrador e do analista, a escolha entre tools SQL e GraphQL e as regras do sandbox são montadas na inicialização. Por isso, trocar de domínio exige restart (ver Atividade 3).

### Arquitetura

**Usuário → Orquestrador (LangGraph + checkpointer) → Tools → Sub-agentes e pipelines → Resposta**

| Componente | Papel |
| --- | --- |
| Orquestrador | Conduz a conversa, roteia por tools e redige a resposta final; histórico persistido por `session_id` |
| Sub-agente analista | Consulta o banco do domínio, analisa em Python e devolve um relatório técnico; sub-grafo sem checkpointer e sem histórico |
| Sub-agente de agendamento | Lista, pausa, retoma, apaga e altera tarefas (`gerenciar_agenda`) |
| Pipelines de criação e edição de tarefas | Grafo determinístico que gera, testa, corrige e salva o código da tarefa |
| Daemon de agendamento | Executa o código salvo, sem LLM, no horário programado |

O estado compartilhado do agente é formado por três elementos: o **histórico de mensagens**, persistido pelo checkpointer por `session_id`; as **ContextVars**, que carregam `user_id`, sessão e handle de cancelamento; e **dicts globais por sessão**, que guardam os namespaces de DataFrames, as filas de eventos SSE, os runs ativos e o snapshot do dashboard. O analista roda em um sub-grafo sem checkpointer: recebe apenas o pedido pontual delegado pelo orquestrador, mais os últimos scripts executados na sessão como referência para ajustes.

### Resultados

- O agente atende, pela mesma conversa, as seis categorias de pedido: consulta de dados, pergunta conceitual, agendamento, pergunta simples, pergunta sobre a tela e painel customizado.
- O trabalho especializado fica nos sub-agentes, e o orquestrador permanece sem lógica de análise de dados.

### Evidências

**Figura 1.** Construção do grafo do orquestrador (nó do modelo, ToolNode e checkpointer) e lista de tools.  
**[INSERIR PRINT DO CÓDIGO: trecho de agent_multi.py com a construção do grafo]**

**Figura 2.** Seção do prompt do orquestrador com as categorias A a F e a regra de desempate.  
**[INSERIR PRINT DO CÓDIGO: prompt do orquestrador em agent_multi.py]**

## Atividade 2: Integração e configuração dos modelos Gemini (Vertex AI)

### Descrição

Foi implementada a camada de criação e configuração dos modelos Gemini via Vertex AI, dentro de `agent_multi.py`. O sistema utiliza um único modelo, definido por `MODEL_NAME` (padrão `gemini-3.8-flash`), instanciado em quatro papéis com temperatura e nível de raciocínio (thinking level) próprios. O mesmo modelo é usado na transcrição de voz, com raciocínio no nível mínimo aceito (`LOW`).

| Papel | Temp. | Nível de raciocínio (variável de ambiente) | Usado em |
| --- | --- | --- | --- |
| Analista | 0,15 | `THINKING_LEVEL_ANALISTA` | Sub-agente analista, sub-agente de agendamento e pipeline de agendamento |
| Orquestrador | 0,4 | `THINKING_LEVEL_ORQUESTRADOR` | Roteamento e redação da resposta |
| Orquestrador (roteamento) | 0,4 | `THINKING_LEVEL_ORQUESTRADOR_ROTEAMENTO` (opcional) | Só a primeira chamada de cada turno, que decide a tool; sem a variável, usa o papel Orquestrador |
| Relatório | 0,4 | `THINKING_LEVEL_RELATORIO` (padrão `medium`) | `ctx.analisar`, que roda no daemon sem ninguém esperando |

Os níveis aceitos são `low`, `medium` e `high`; sem a variável de ambiente correspondente, vale o padrão do modelo.

### Justificativa

Cada papel tem uma natureza de trabalho diferente. O analista executa trabalho mecânico — ler o schema, montar filtros, gerar código —, em que determinismo importa mais do que variedade, e por isso usa temperatura baixa (0,15). O orquestrador interpreta pedidos e redige respostas e usa 0,4. O papel de relatório (`ctx.analisar`) roda no daemon, sem ninguém aguardando a resposta, o que torna a latência menos crítica.

Configurar o nível de raciocínio por papel permite calibrar qualidade, latência e custo em cada ponto do fluxo sem trocar de modelo e sem alterar código: a calibração é feita por variáveis de ambiente.

### Principais desafios

- **Adequar a configuração ao papel.** Como o modelo é único, a diferenciação é feita por temperatura e nível de raciocínio. A primeira chamada de cada turno, que apenas decide qual tool chamar, tem um papel próprio (roteamento), com nível de raciocínio configurável separadamente.
- **Compatibilidade com o Gemini 3.** O cliente `ChatVertexAI` foi estendido (`ChatVertexAIThinkingLevel`) para enviar `thinking_level` pelo endpoint v1 e reinserir as `thought_signature` das chamadas de função, que o wrapper original descarta e que o Gemini 3 exige na chamada seguinte a qualquer tool call.
- **Autenticação em diferentes ambientes.** As credenciais de service account podem vir de `credentials.json` na raiz do projeto ou da variável `GOOGLE_CREDENTIALS_JSON` (base64), o que permite a execução local e em container. O projeto e a região vêm de `PROJECT_ID` e `LOCATION`; sem `PROJECT_ID`, o agente não sobe.

### Arquitetura

**Variáveis de ambiente (`MODEL_NAME`, `PROJECT_ID`, `LOCATION`, `THINKING_LEVEL_*`) → Credenciais → Criação dos LLMs (`ChatVertexAIThinkingLevel`) → Quatro papéis → Orquestrador, analista, relatório e transcrição**

### Resultados

- Um único modelo atende os quatro papéis do agente e a transcrição de voz, com ajuste por papel feito apenas por variáveis de ambiente.
- O ciclo de tool calls funciona com o Gemini 3, com `thinking_level` e `thought_signature` tratados pela extensão do cliente.

### Evidências

**Figura 3.** Criação dos LLMs nos quatro papéis, com temperatura e nível de raciocínio.  
**[INSERIR PRINT DO CÓDIGO: criação dos LLMs em agent_multi.py]**

**Figura 4.** Extensão `ChatVertexAIThinkingLevel` (envio de `thinking_level` e reinserção de `thought_signature`).  
**[INSERIR PRINT DO CÓDIGO: classe ChatVertexAIThinkingLevel em agent_multi.py]**

## Atividade 3: Conhecimento de domínio plug-and-play

### Descrição

O agente não tem nenhum schema nem regra de negócio embutidos no código. Tudo o que ele sabe sobre qual banco consultar e como entender o domínio vem de um **pack de configuração** na pasta `config/`, lido em runtime. O `settings.yml` define a identidade do app, o domínio ativo, o modo de acesso aos dados (`sql` ou `graphql`), a conexão e o mês de início do ano fiscal; e, para cada domínio, o `dominio.md` (conhecimento de domínio) e o `dashboard_default.py` (painéis padrão). Alterar a fonte de dados ou o conhecimento do domínio é alterar arquivos, não código.

O `dominio.md` é dividido por seções `##`, cada uma com um destino diferente:

| Seção | Papel |
| --- | --- |
| Regras do agente | Injetada no prompt do analista: armadilhas do schema, qual coluna de data usar, granularidade real dos dados |
| Regras do orquestrador | Injetada no prompt do orquestrador: o que o sistema é e não é, para negar assuntos de outros domínios, e qual período assumir quando o usuário não diz nenhum |
| Períodos | Frases como “mês passado” associadas a uma SQL que devolve uma linha `(from, to)`; usada por `calcular_periodo` |
| Modelo de dados | Schema, joins e exemplos; entra no prompt do analista e é servida pela tool `rag_dados` |
| Domínio de negócio | Glossário e regras de negócio; servida pela tool `rag_dominio` |
| Exemplos task_code | Padrões de código para as tarefas agendadas, anexados às regras do sandbox |
| Dica para condition_sql | Tabelas e colunas úteis e o formato esperado para monitores e alertas |

O conhecimento chega ao agente por três caminhos:

- **Injeção direta no prompt.** O `dominio.md`, sem as seções já injetadas em outros prompts, é carregado no system prompt do analista a cada chamada. Como o analista sempre precisa do schema, carregá-lo diretamente economiza uma rodada de LLM por análise.
- **Tools de consulta.** `rag_dominio`, `rag_dados`, `rag_capacidades` e `rag_arquitetura` devolvem apenas o trecho necessário do pack e dos textos de autodescrição (`rag/`) quando a pergunta é conceitual ou sobre o próprio sistema.
- **Skills sob demanda.** Skills genéricas (`skills/*.md`, hoje `task_code_sandbox.md`) entram no prompt apenas pelo cabeçalho (as quatro primeiras linhas); o conteúdo é lido por `read_skill` ou injetado nos prompts de geração de código quando necessário.

Não há embeddings nem banco vetorial: o conhecimento é lido por tools a partir de arquivos Markdown.

### Justificativa

Fundamentar o agente em conhecimento de domínio explícito reduz a dependência do conhecimento geral do LLM e o risco de respostas sem respaldo, em especial sobre o schema, em que um nome de coluna inventado quebra a consulta. A separação entre conhecimento e código permite evoluir o conhecimento do CAPEX, ou trocar a fonte de dados, sem alterar o código do agente: apenas o pack muda.

O carregamento sob demanda das skills evita que instruções não utilizadas custem tokens em cada requisição e permite que o sistema escale para várias skills sem inflar o prompt.

### Principais desafios

- **Decidir o que fica sempre no prompt e o que vem sob demanda.** O schema do domínio ativo é sempre necessário e fica no prompt do analista; skills genéricas e textos de autodescrição só são carregados quando usados. É uma decisão de engenharia de contexto, que equilibra custo de tokens, latência e aderência às regras.
- **Saber quando cada parte é atualizada.** Identidade, regras do agente e do orquestrador, escolha entre tools SQL e GraphQL e regras do sandbox são montadas uma vez na inicialização, então trocar `app.dominio` exige restart. Modelo de dados, domínio de negócio, períodos, dica para `condition_sql` e `dashboard_default.py` são relidos quando o arquivo muda (cache por `mtime`), sem restart.
- **Preservar a chave de identidade do domínio.** O valor de `app.dominio` é gravado nas tabelas do banco local e usado como filtro em toda listagem. Renomeá-lo deixa as linhas existentes órfãs: elas não são apagadas, mas deixam de aparecer.
- **Tolerar packs incompletos.** O parser trata cada seção como opcional, de modo que uma seção ausente degrada o agente, mas não o quebra. Os títulos internos de uma seção usam `###`, nunca `##`, que é o delimitador de seção.
- **Manter a autodescrição em sincronia.** Os textos de `rag/` descrevem o agente a ele mesmo e precisam ser atualizados junto com o sistema.

### Arquitetura

**`settings.yml` + `dominio.md` do domínio → `db_config.py` (seções, cache por `mtime`) → Prompts do orquestrador e do analista · Tools `rag_*` · Catálogo de skills → Agente**

### Resultados

- O conhecimento do CAPEX entra no agente por um pack de configuração, sem alterações no código do agente.
- As skills e os trechos de conhecimento usados em perguntas conceituais são lidos sob demanda, o que contém o custo de tokens por requisição.

### Evidências

**Figura 5.** `settings.yml` do pack ativo (domínio e modo de acesso).  
**[INSERIR PRINT DO CÓDIGO: arquivo config/settings.yml]**

**Figura 6.** Seções do `dominio.md` do CAPEX.  
**[INSERIR PRINT DO CÓDIGO: arquivo dominio.md do pack do CAPEX]**

**Figura 7.** Catálogo de skills e tools `rag_*`.  
**[INSERIR PRINT DO CÓDIGO: trecho de agent_multi.py com o catálogo de skills e as tools rag\_\*]**

## Atividade 4: Estruturação da base de conhecimento do CAPEX

### Descrição

Foi realizada a estruturação da documentação funcional do sistema CAPEX como base de conhecimento do agente, consolidando os requisitos em um único arquivo Markdown (o `dominio.md` do pack), dividido em seções. O agente lê apenas a parte necessária a cada pergunta, por meio das tools de conhecimento (Atividade 3). O arquivo consolida objetivos funcionais, fluxos, campos, regras de negócio, validações, critérios de aceite, exceções e pontos de ambiguidade das diferentes funcionalidades do sistema.

### Justificativa

Para que o agente responda perguntas específicas sobre o CAPEX, é necessário fornecer uma fonte de conhecimento controlada e alinhada à documentação funcional do projeto. A estruturação em um arquivo textual dividido em seções permite que o agente leia apenas o trecho relevante e o use como contexto, reduzindo a dependência do conhecimento genérico do LLM e o volume de contexto enviado ao modelo.

A organização do arquivo também separa requisitos confirmados, recomendações técnicas e ambiguidades da especificação. Isso impede que o agente apresente como comportamento oficial uma informação que ainda necessita de validação funcional.

### Principais desafios

- **Heterogeneidade dos requisitos funcionais.** Os requisitos abrangem áreas funcionais distintas — gerenciamento de CAPEX, transferências financeiras e de materiais, dashboard, configurações, fornecedores, produtos, compras e planejamento financeiro —, cada um com campos, regras de negócio, cálculos e fluxos próprios, que precisam permanecer semanticamente separados, em seções distintas do mesmo arquivo.
- **Preservar ambiguidades sem transformá-las em regra.** Foram identificados casos como fórmulas que necessitam de confirmação, divergências de nomenclatura, comportamentos incompletamente especificados e diferenças entre objetivos, fluxos e critérios de aceite. O conhecimento registra essas lacunas explicitamente, para que o agente as sinalize em vez de afirmá-las como regra.
- **Inconsistência na documentação de Gestão de Assets.** O conteúdo interno dessa seção corresponde majoritariamente à Gestão de Compras. A base registra explicitamente essa inconsistência e orienta que o material não seja interpretado como uma especificação completa do módulo de Assets.
- **Estruturar para consumo por LLM e para leitura parcial.** Regras, exceções e pontos de atenção precisam estar explícitos para que o modelo distinga o que está definido do que depende de esclarecimento funcional. Como o agente lê apenas a parte necessária, cada seção precisa ser compreensível isoladamente.

### Arquitetura

A base é um único arquivo Markdown, segmentado por requisito funcional, em que cada seção representa uma área funcional do CAPEX. A estrutura favorece a leitura parcial pelo agente, com divisões semânticas — objetivo, acesso, glossário, campos, fluxos, regras de negócio, validações, cenários de teste e ambiguidades —, conforme aplicável a cada requisito.

**Requisitos funcionais → Arquivo Markdown único, dividido em seções → Tools `rag_*` (leitura apenas da parte necessária) → Contexto para o agente**

### Resultados

- A documentação funcional do CAPEX está disponível ao agente em um único arquivo Markdown, organizado por requisito funcional e lido apenas na parte necessária.
- Ambiguidades e inconsistências da especificação ficam registradas explicitamente, sem virar regra do sistema.

### Evidências

**Figura 8.** Seção do arquivo Markdown sobre o gerenciamento das ordens e itens de compra vinculados ao CAPEX.  
**[INSERIR PRINT DO CÓDIGO: trecho do arquivo Markdown da base de conhecimento do CAPEX; reaproveitar a Figura 16 do RDA anterior]**

**Figura 9.** Seção do arquivo Markdown sobre Assets e as inconsistências identificadas em relação ao conteúdo de Gestão de Compras.  
**[INSERIR PRINT DO CÓDIGO: trecho do arquivo Markdown da base de conhecimento do CAPEX; reaproveitar a Figura 17 do RDA anterior]**

## Atividade 5: Consulta a dados em tempo real, análise em sandbox Python e proteção de contexto

### Descrição

O sub-agente analista consulta o banco do domínio ativo em tempo real e processa os resultados em um sandbox Python. O método de acesso é definido por configuração (`database.modo`, no `settings.yml`): quando há um servidor GraphQL para o sistema de origem, o agente o utiliza (`graphql`); quando não há, a configuração passa para o acesso direto ao banco por SQL (`sql`). A troca de um modo para o outro não exige alteração de código, e cada modo expõe ao analista um conjunto próprio de tools:

|  | `sql` | `graphql` |
| --- | --- | --- |
| Configuração | `database.url` (SQLAlchemy) | `graphql.url` + `token` ou `api_key` |
| Tools do analista | `executar_sql(query, chave)` | `executar_graphql` (linhas cruas), `executar_graphql_agregado` (GROUP BY no servidor) e `executar_graphql_schema` (schema real, só para destravar quando a skill diverge do servidor) |
| Salvaguarda | `run_select()` rejeita tudo que não for `SELECT` | O cliente só emite as três queries de leitura |

Cada consulta injeta um DataFrame, com o nome indicado em `chave`, no namespace da sessão. `analisar_dataframe(script)` executa Python/Pandas nesse namespace: o script acessa os DataFrames diretamente e pode criar variáveis (como `resultado`) que ficam disponíveis na chamada seguinte. O ambiente se comporta como um notebook Jupyter: as variáveis persistem entre chamadas de tools e entre invocações consecutivas do analista.

### Justificativa

Consultar o banco em tempo real elimina a dependência de intermediários nas análises rotineiras: o gestor formula a pergunta e recebe uma resposta fundamentada em dados reais, sem esperar por um analista disponível. A análise incremental — buscar, filtrar, agregar, plotar — exige que os DataFrames sobrevivam entre as chamadas. E, como um mês de dados pode ter milhares de linhas, enviá-los ao modelo a cada tool call seria caro, lento e desnecessário.

### Principais desafios

- **Persistir o ambiente entre tool calls.** As tools rodam em threads de um pool, onde `threading.local` não funciona. A solução é um dict global indexado por `session_id`, protegido por `threading.Lock`, com o `session_id` atual propagado por `ContextVar`. O namespace expira após 10 minutos sem uso, e os três últimos scripts executados são guardados e reapresentados ao analista, de modo que um pedido como “mesmo gráfico, mas em cinza” reaproveita o código em vez de recomeçar.
- **Proteger o contexto.** O retorno de uma tool é reenviado ao LLM em todas as chamadas seguintes do turno, então o limite é baixo de propósito: tabelas de até 100 linhas (`_MAX_RESULT_ROWS`) voltam completas em Markdown; acima disso, o modelo recebe só a descrição estrutural (shape, dtypes e primeira linha); e, em qualquer caso, recebe um resumo do namespace (nome, tipo e shape de cada variável, via `_ns_summary`). Dados brutos nunca chegam ao modelo. O prompt do analista também manda agregar (`DISTINCT` ou `GROUP BY`) em vez de listar linhas repetidas e limitar as tabelas da resposta final a cerca de 20 linhas.
- **Garantir acesso somente leitura.** No modo `sql`, `run_select()` rejeita qualquer comando que não seja `SELECT`; no modo `graphql`, o cliente só emite as três queries de leitura (`reportTable`, `reportAggregate` e `reportSchema`).
- **Lidar com as particularidades do GraphQL.** O parâmetro `expand` faz join de um salto por chave estrangeira, com colunas prefixadas em `reportTable` e nomes puros em `reportAggregate`, ao lado de `groupKey` e `metricValue`. O cliente acrescenta o nome real do `group_by` como alias de `groupKey`, porque o código gerado pelo LLM esperava a coluna de agrupamento pelo nome e quebrava com `KeyError`. Em `reportAggregate`, o servidor assume `limit` 20 e aceita no máximo 500; o cliente tem timeout de 30 s e converte erros do servidor em `GraphQLError`.

### Arquitetura

**Pedido delegado → Analista → `executar_sql` / `executar_graphql*` → DataFrame no namespace da sessão → `analisar_dataframe` → Resultado → Retorno ao orquestrador**

O que o modelo recebe de cada saída segue uma regra fixa:

| Saída | Condição | O que o LLM recebe |
| --- | --- | --- |
| Tabela completa | até 100 linhas | Markdown completo |
| Descrição estrutural | mais de 100 linhas | shape, dtypes e primeira linha |
| Resumo de namespace | sempre | Metadados das variáveis (nome, tipo, shape) |
| Dados brutos | nunca | Nada: ficam protegidos no backend |

### Resultados

- O analista consulta o banco do domínio em tempo real e analisa os dados de forma incremental, com os DataFrames preservados entre as chamadas.
- O contexto enviado ao modelo é limitado por regra (até 100 linhas por retorno de tool), sem perda da capacidade de análise.

### Evidências

**Figura 10.** Consulta e análise executadas pelo agente no chat, com a tabela de resultado.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: tela do chat com uma consulta respondida]**

**Figura 11.** Regra de proteção de contexto (`_MAX_RESULT_ROWS` e `_ns_summary`).  
**[INSERIR PRINT DO CÓDIGO: trecho de agent_multi.py]**

## Atividade 6: Gráficos, PDFs e Excels como artefatos persistentes

### Descrição

Gráficos, PDFs e planilhas gerados pelo agente não trafegam pelo contexto do LLM. Quando o analista (ou o código de uma tarefa agendada) gera um gráfico matplotlib, o script salva `result = fig` (ou chama `ctx.save_chart(fig)`), a tool serializa a figura para PNG e a armazena no banco local (`mfg.db`) com um UUID, e o modelo recebe apenas o token `[chart:uuid]`. O FastAPI troca o token por uma imagem (`/chart/{id}`) na resposta, e o frontend a renderiza.

O mesmo padrão vale para PDFs (`gerar_pdf`, que embute os gráficos referenciados por token, servido em `/pdf/{id}`) e para planilhas Excel (`gerar_excel`, com o token `[excel:uuid]` resolvido em um link de download `/excel/{id}`). O painel de artefatos lista tudo o que foi gerado, com a origem de cada item (chat ou tarefa agendada), e permite apagá-los. Para perguntas visuais de acompanhamento, como “por que há um pico?”, a tool `ver_grafico` injeta a imagem do gráfico no contexto.

### Justificativa

Imagens e arquivos no contexto do LLM seriam caros e desnecessários: o modelo não precisa dos bytes, só de uma referência estável. O token mantém o contexto pequeno, reduz custo e latência e permite que o usuário receba o artefato sem que ele passe pelo modelo. Usar o `mfg.db` como armazenamento local de objetos simplifica a implantação e mantém os artefatos acessíveis até que sejam apagados.

### Principais desafios

- **Entregar o artefato sem passar pelo modelo.** O token é resolvido no backend: os tokens `[chart:]`, `[pdf:]` e `[excel:]` chegam ao frontend já convertidos em URLs no evento `reply`. Como o modelo não enxerga o artefato, o analista devolve junto do token um bloco com os pontos-chave dos dados (Atividade 7), para que follow-ups sejam respondidos sem reprocessar.
- **Permitir perguntas sobre o que foi desenhado.** O gráfico é uma imagem que o modelo não vê. A tool `ver_grafico` a injeta sob demanda, em vez de enviá-la em toda chamada.
- **Controlar escopo e ciclo de vida.** Cada artefato carrega `user_id` e `dominio`, e é apagado junto com a conversa que o gerou ou pelo painel de artefatos. Os previews gerados durante o teste de um `task_code` ficam em uma sessão `test_<id>_...`, escondida da lista, e são promovidos para a sessão real quando o código é salvo.

### Arquitetura

**Script (matplotlib · `gerar_pdf` · `gerar_excel` · `ctx.save_*`) → Serialização → `mfg.db` (charts, pdfs, excels) → Token no contexto do LLM → FastAPI resolve o token → Frontend renderiza ou baixa**

### Resultados

- Gráficos, PDFs e Excels são gerados por conversa e permanecem disponíveis para visualização e download.
- O modelo trabalha apenas com tokens, o que mantém o contexto enxuto.

### Evidências

**Figura 12.** Resposta do agente com gráfico renderizado no chat.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: tela do chat com um gráfico]**

**Figura 13.** Painel de artefatos e link de download de um PDF ou Excel gerado.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: painel de artefatos]**

## Atividade 7: Síntese da resposta e controle de alucinação

### Descrição

A geração da resposta foi dividida em dois papéis. O **analista** devolve ao orquestrador um relatório técnico conciso, sem prosa: bullets com os achados e os números exatos; a tabela (no máximo cerca de 20 linhas); o token do artefato, quando há; e dois blocos obrigatórios. O primeiro, **Parâmetros da análise**, informa o período e todo filtro ou agrupamento que realmente entrou na consulta, marcando o que foi assumido. O segundo, **Dados do gráfico, relatório ou planilha**, traz os pontos-chave do artefato, para responder follow-ups sem reprocessar.

Quem redige a resposta ao usuário é o **orquestrador**, que só pode usar números presentes no retorno do analista e deve preservar todas as ressalvas. Perguntas conceituais (tools `rag_*`) e perguntas simples (data, hora, saudação) são respondidas diretamente pelo orquestrador.

### Justificativa

Separar quem calcula de quem comunica reduz o espaço para alucinação: o modelo que escreve a resposta não calcula, e o que calcula não escreve prosa. O analista só responde com resultados de `analisar_dataframe` — sem dados, sem resposta —, e o bloco de parâmetros torna visível o que foi assumido na consulta, permitindo ao usuário corrigir uma premissa antes de tomar uma decisão com base no número.

### Principais desafios

- **Impedir números inventados.** O orquestrador só pode usar números presentes no retorno do analista. Nos relatórios agendados, em que ninguém revisa o texto antes da entrega, a regra é mais rígida: o código calcula todos os números, e o LLM apenas os interpreta, por meio de `ctx.analisar(fatos, foco, fallback)`. O texto devolvido passa por uma conferência: todo número citado precisa constar nos fatos, com tolerância de arredondamento. Se não constar, o LLM é chamado de novo, uma única vez, avisado dos números inventados; persistindo o problema, ou em caso de timeout (150 s) ou falha, o `fallback` determinístico escrito pelo próprio código entra no lugar. São, no máximo, três chamadas por execução.
- **Preservar as ressalvas na passagem entre papéis.** O orquestrador é instruído a repassar as ressalvas do analista por inteiro, e o bloco de parâmetros evita que uma premissa assumida chegue ao usuário como fato.
- **Manter o retorno curto.** O relatório técnico é deliberadamente conciso, porque o retorno de uma tool é reenviado ao modelo em todas as chamadas seguintes do turno.

### Arquitetura

**Pedido → Orquestrador → Analista → Relatório técnico (achados, tabela, token, parâmetros, dados do artefato) → Orquestrador → Resposta final ao usuário**

### Resultados

- Respostas fundamentadas em números calculados pelo código, com parâmetros e premissas explícitos.
- Análises narrativas dos relatórios agendados com números conferidos contra os fatos calculados e com fallback determinístico.

### Evidências

**Figura 14.** Formato do relatório técnico do analista, com os blocos “Parâmetros da análise” e “Dados do gráfico”.  
**[INSERIR PRINT DO CÓDIGO: trecho do prompt do analista em agent_multi.py que define o relatório]**

**Figura 15.** Resposta final do orquestrador com as ressalvas preservadas.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: tela do chat]**

## Atividade 8: Guardrails e segurança

### Descrição

Foram implementadas proteções em camadas, combinando regras de prompt — que cobrem escopo e intenção — com garantias estruturais e regras impostas em código, que não dependem de o modelo obedecer:

| Camada | Mecanismo | Natureza |
| --- | --- | --- |
| Escopo do domínio | A seção Regras do orquestrador do pack define o que o sistema é e não é, para negar assuntos de outros domínios | Prompt |
| Intenção explícita | Na dúvida entre consulta e ação, vale a consulta; nunca se cria tarefa nem painel sem palavras explícitas de agendamento ou de inserção no dashboard; o painel só é criado após confirmação | Prompt |
| Banco do domínio | Somente leitura: `run_select()` rejeita tudo que não for `SELECT`, e o cliente GraphQL só emite as três queries de leitura | Estrutural |
| Código gerado pelo agente | Sandbox: `exec()` com builtins por lista de permitidos, `import` rejeitado antes de compilar e `ctx` como única porta para fora (`ctx.api` restrito a `GET /alerts`) | Estrutural |
| Agendamento e alertas | Frequência padrão e intervalo mínimo de 5 minutos para monitores; regras de notificação validadas ao salvar; execução sem artefato é erro | Código |
| Números nas respostas | O orquestrador só usa números do retorno do analista; `ctx.analisar` confere cada número contra os fatos | Código e prompt |
| Dados por usuário e domínio | Filtros por `user_id` e `dominio`; painel compartilhado é visível a todos, mas só o dono pode editá-lo | Código |

### Justificativa

Um agente que escreve código, cria tarefas agendadas e fixa painéis precisa de limites que não dependam apenas de instruções ao modelo. As proteções estruturais — banco somente leitura, sandbox e regras em código — valem mesmo quando o modelo erra; o prompt cobre o que só ele consegue cobrir, que é o escopo e a interpretação da intenção do usuário.

### Principais desafios

- **Distinguir consulta de ação.** Criar tarefas e painéis altera o estado do sistema, por isso a regra é assimétrica: na dúvida, vale a consulta, e a ação só ocorre com palavras explícitas do usuário (e, no caso do painel, após confirmação).
- **Transformar regra de prompt em regra de código.** Quando o modelo não seguia uma regra de forma consistente, ela foi movida para o código. Um monitor sem frequência explícita recebe `every_5m` mesmo que o modelo sugira outra; `ctx.notify()` é recusado em monitores e `ctx.alert()` é recusado em tarefas com `condition_sql`; uma execução que não produz nenhum artefato é tratada como erro.
- **Evitar alertas falsos.** `ctx.alert(False, ...)` é a forma correta de informar “sem dado”: o sistema nunca assume 0, que poderia satisfazer uma comparação `<` e gerar um alerta falso. A notificação sai apenas na transição de “não atingida” para “atingida”.
- **Executar código gerado por LLM no próprio processo.** O sandbox contém os erros comuns desse tipo de código, mas é um filtro, não um isolamento. Por isso o código executado vem sempre do próprio agente, nunca de entrada de terceiros.

As limitações de segurança assumidas na PoC, como a autenticação delegada ao app host, estão listadas ao final deste documento.

### Arquitetura

**Pedido → Prompt do orquestrador (escopo e intenção) → Tools somente leitura e pipelines validados → Sandbox → Artefato ou alerta**

### Resultados

- O agente não consegue escrever no banco do domínio, e o código gerado executa em um ambiente restrito.
- Regras críticas de agendamento e alerta são validadas em código, e não apenas pedidas ao modelo.

### Evidências

**Figura 16.** Garantia de somente leitura: `run_select()` e a restrição do cliente GraphQL.  
**[INSERIR PRINT DO CÓDIGO: trecho do modo SQL (run_select) e de graphql_config.py]**

**Figura 17.** Builtins permitidos e variáveis pré-injetadas do sandbox do `task_code`.  
**[INSERIR PRINT DO CÓDIGO: scheduler/runner.py]**

**Figura 18.** Recusa de um assunto fora do escopo no chat.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: tela do chat]**

## Atividade 9: Prompts e contexto temporal

### Descrição

Os prompts do agente são compostos por partes, e cada papel recebe só o que precisa. Entram na composição a identidade do app (`app.nome` e `app.descricao` do `settings.yml`), as seções do pack (Regras do agente, Regras do orquestrador, Modelo de dados, entre outras), as regras do sandbox adaptadas ao modo de acesso ativo (`sql` ou `graphql`) e o catálogo de skills.

O tratamento de datas é feito por tools, e não por regras soltas no prompt. `get_current_datetime()` informa a data e a hora atuais. `calcular_periodo()` resolve expressões como “mês passado”, “este quarter” ou “este ano” a partir da seção Períodos do pack — uma SQL que devolve uma linha `(from, to)` — e, quando nenhuma entrada casa, cai em um fallback de calendário, que cobre “todo o período” (de `2000-01-01` até hoje) e, via `mes_inicio_fiscal`, trimestres e ano fiscal. O orquestrador passa o período ao analista em linguagem natural, e ele é resolvido dentro da própria tool.

### Justificativa

Compor o prompt por partes mantém cada papel com um contexto pequeno e específico e coloca o conhecimento do domínio no pack, e não no código. Resolver períodos por tool, com regras do domínio, é necessário porque os dados podem ser um snapshot cuja “data atual” não é hoje: “mês passado” precisa ser interpretado em relação aos dados, e não ao calendário real.

### Principais desafios

- **Separar o que é montado uma vez do que é relido.** Identidade, regras do agente e do orquestrador, escolha das tools e regras do sandbox são montadas na inicialização; schema, domínio de negócio, períodos e painéis padrão são relidos quando o arquivo muda (Atividade 3).
- **Resolver períodos sem depender do calendário real.** A ordem das entradas da seção Períodos importa: a primeira que casar vence. Quando o acesso é por `graphql` e não há `database.url`, as SQLs dessa seção não têm onde rodar: falham em silêncio, e `calcular_periodo` cai no fallback de calendário.
- **Reaproveitar código entre pedidos.** Os três últimos scripts executados são reapresentados ao analista, para que ajustes de um pedido anterior partam do código já escrito.

### Arquitetura

**Pack (`settings.yml`, `dominio.md`) → Composição dos prompts → LLM**

**Período em linguagem natural → `calcular_periodo` → Seção Períodos (ou fallback de calendário) → `from_date` e `to_date`**

### Resultados

- Prompts específicos por papel, compostos a partir do pack, sem conhecimento de domínio embutido no código.
- Períodos e datas resolvidos de forma consistente com os dados do domínio, inclusive no calendário fiscal.

### Evidências

**Figura 19.** Composição do prompt do orquestrador, com a identidade do app e as seções do pack.  
**[INSERIR PRINT DO CÓDIGO: trecho de agent_multi.py]**

**Figura 20.** Seção Períodos de um `dominio.md`.  
**[INSERIR PRINT DO CÓDIGO: arquivo dominio.md do pack]**

## Atividade 10: Streaming SSE e cancelamento

### Descrição

O frontend não espera a resposta final: cada etapa chega em tempo real por Server-Sent Events (`GET /chat/stream`). Os eventos são:

| Evento | Quando |
| --- | --- |
| `thinking` | Raciocínio do modelo ou frase que antecede uma tool call |
| `tool` | Tool chamada ou etapa de um pipeline, com um rótulo de progresso legível ao usuário |
| `artifact_preview` e `artifact_saved` | O teste de um `task_code` gerou gráfico, PDF ou Excel; o código foi salvo |
| `widget_added`, `widget_updated` e `widget_deleted` | O agente alterou um painel; o dashboard se atualiza sem F5 |
| `reply` | Resposta final em markdown, com os tokens de artefatos já resolvidos em URLs |
| `cancelled` e `error` | O usuário parou o run; erro irrecuperável |

A resposta textual é entregue inteira no evento `reply`, sem transmissão incremental de tokens. O usuário pode interromper um run em andamento pelo botão de parar (`POST /chat/cancel`). O SSE é usado apenas no chat: o sino de alertas e o badge da agenda fazem polling, a cada 3 s e a cada 5 s, respectivamente.

### Justificativa

Operações com LLM e tools têm latência perceptível. Mostrar o raciocínio e os passos em andamento informa o estágio do processamento sem depender da resposta final, e o botão de parar devolve o controle ao usuário quando o caminho escolhido pelo agente não é o desejado.

### Principais desafios

- **Capturar eventos de um sub-agente que roda fora do stream principal.** O analista roda dentro de um `@tool` do orquestrador, fora do stream do LangGraph. A solução foi um side-channel por sessão: uma `queue.Queue` por `session_id` captura os eventos do sub-agente em tempo real e os entrega ao gerador SSE. Os pipelines de agendamento usam o mesmo canal para os eventos de etapa.
- **Propagar o contexto para a thread do streaming.** O streaming SSE sobe uma thread própria para rodar o orquestrador, e `ContextVar` não atravessa `threading.Thread` criadas manualmente. Por isso `user_id`, `session_id` e o handle de cancelamento são re-setados explicitamente nesse ponto.
- **Cancelar sem poder matar a thread.** Threads Python não podem ser mortas e um `llm.invoke()` em andamento não é interrompível, então o cancelamento é cooperativo. Cada run tem um `RunHandle` com um `threading.Event`, propagado por `ContextVar`; os nós do orquestrador, do analista e do agendamento o consultam antes e depois de cada chamada de LLM. No orquestrador, cancelar devolve um `AIMessage` sem `tool_calls`, o que mantém o histórico do checkpoint válido; nos grafos aninhados, lança `RunCancelled`, que o `ToolNode` converte em `ToolMessage`. Depois de cancelado, `_push_event` descarta eventos novos, e um novo run da mesma sessão espera o anterior terminar (até 45 s). Se o cancelamento pegar um pipeline de criação de tarefa, o rascunho é descartado. Limite conhecido: a chamada de LLM ou o script em andamento termina antes de o cancelamento surtir efeito.

### Arquitetura

**Orquestrador (thread própria) e sub-agentes → `_push_event` → Fila por sessão → Gerador SSE → EventSource no frontend**

**Botão de parar → Fecha o EventSource e chama `POST /chat/cancel` → `RunHandle` sinalizado → Nós consultam o evento → Run encerrado (`cancelled`)**

### Resultados

- O usuário acompanha o raciocínio e os passos do agente em tempo real, inclusive os do sub-agente.
- Um run em andamento pode ser interrompido sem corromper o histórico da conversa.

### Evidências

**Figura 21.** Raciocínio e passos do agente (eventos `thinking` e `tool`) exibidos no chat.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: tela do chat durante uma resposta]**

**Figura 22.** Fila de eventos por sessão e `_push_event`.  
**[INSERIR PRINT DO CÓDIGO: trecho de agent_multi.py]**

**Figura 23.** Botão de parar e resposta cancelada.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: tela do chat]**

## Atividade 11: Agendamento de relatórios e monitores de threshold

### Descrição

Uma tarefa agendada é um `task_code` Python — `def run(from_date, to_date, ctx)` — que o próprio agente escreve, testa e salva, e que um daemon executa depois, sem LLM no caminho (exceto na análise narrativa, via `ctx.analisar`). Há dois tipos: **relatório** (gráfico, PDF ou Excel recorrente) e **monitor** (verifica uma condição de negócio e notifica quando ela é atingida). Exemplos de pedido: “todo dia às 7h gere o relatório em PDF” e “me avise se o total do dia passar de 160”.

**Criação e edição por conversa.** Os pedidos de agendamento acionam `criar_tarefa_agendada` ou `editar_tarefa_agendada`, que executam um grafo determinístico inteiro dentro da tool: o orquestrador não conduz os passos.

**Criar: `criar_tarefa` → `consultar_dados` → `gerar_codigo` → `testar_codigo` → `verificar_analise` → `salvar_codigo` → `responder`**

**Editar: `editar_codigo` → `testar_codigo` → (mesmo caminho). Em caso de erro, `corrigir_codigo` volta ao teste, até 3 vezes; se faltar a análise pedida, `incluir_analise` volta ao teste, até 2 vezes**

| Nó | O que faz |
| --- | --- |
| `criar_tarefa` | Um LLM extrai os parâmetros do pedido (nome, frequência, horário, `date_range`, tipo, condição) e a tarefa nasce como `draft`, invisível no painel e ignorada pelo daemon |
| `consultar_dados` | O analista, em modo sandbox, lê dados reais e devolve skill, query, colunas, amostra, filtros e tipo de saída; em monitores no modo GraphQL, resolve os nomes citados pelo usuário para os IDs reais |
| `gerar_codigo` | Um prompt dedicado gera o `task_code`; para monitores, devolve também a interpretação do pedido |
| `testar_codigo` | Executa o código no sandbox com a janela de datas da própria tarefa; o gráfico, PDF ou Excel resultante aparece no chat como preview |
| `corrigir_codigo` | Devolve o erro ao LLM, até 3 tentativas, e remove `import`s de forma determinística |
| `verificar_analise` e `incluir_analise` | Se o pedido cita análise ou insights, mas o código não chama `ctx.analisar`, pede ao LLM, até 2 vezes, que a inclua |
| `salvar_codigo` | Valida as regras de notificação, arquiva a versão anterior e ativa a tarefa |
| `responder` | Monta a confirmação; para monitores, anexa “Como interpretei o pedido” (condição, frequência, janela e escolhas do gerador) |

**Execução: o daemon.** O daemon vive no mesmo processo do FastAPI e, a cada 5 s, varre as tarefas `active` do domínio ativo com `next_run` vencido. Cada execução marca a tarefa como `running` com `UPDATE ... WHERE status='active'` (o `rowcount` impede a execução dupla), calcula `from_date` e `to_date` a partir do `date_range` da tarefa (`ytd`, `mtd`, `today`, `last_Nd`) ou da janela padrão da frequência, roda o `task_code` no sandbox, salva o relatório, registra a execução em `task_runs` e agenda a próxima. O timeout é de 10 minutos; em falha, a tarefa é reagendada com backoff de 5, 15 e 60 minutos, até 3 tentativas, e então vai para `status='error'`, aguardando revisão manual. Tarefas `once` terminam em `completed`, e tarefas `on_demand` só rodam quando o usuário aciona a execução manual (“executar agora”).

**Monitores e alertas.** Há dois modos. No **simples** (uma métrica, uma comparação), o daemon avalia a condição antes de rodar o código, com `condition_sql`, operador e threshold. No **composto** (E/OU, várias métricas ou entidades), o próprio `task_code` chama `ctx.alert(triggered, message)` a cada execução. Em ambos, a notificação sai apenas na transição de “não atingida” para “atingida” e só rearma quando a condição deixa de valer. O alerta é gravado em `threshold_alerts` e aparece no sino do dashboard.

**Saída dos relatórios.** Cada execução gera os artefatos, acessíveis no painel de artefatos com origem `task`, e um diretório `reports/<id>_<nome>_<data>/` com `content.md` e `metadata.json` (`status: pending_send`, recorrência e, se houver destinatário, o bloco do e-mail). O envio por e-mail em si não é feito por este sistema: o metadado estruturado permite a integração com um serviço externo.

### Justificativa

O agendamento elimina a dependência de uma pessoa para a rotina: relatórios recorrentes são produzidos sozinhos, e monitores avisam quando algo sai do esperado, sem que ninguém precise olhar o dashboard. Gerar o código uma vez e executá-lo muitas vezes, sem LLM no caminho, torna a execução determinística e sem custo de tokens, e validar o código em sandbox antes de salvar evita que o erro apareça apenas na primeira execução do daemon.

### Principais desafios

- **Código gerado por LLM que funcione na primeira execução.** O pipeline testa o código com a janela real da tarefa, devolve os erros ao modelo para correção (até 3 vezes) e registra cada erro de geração em `task_code_audit`. Durante a exploração dos dados, `analisar_dataframe` roda com os mesmos builtins e variáveis do sandbox de produção (`_sandbox_mode`), de modo que erros que só apareceriam no daemon aparecem antes.
- **Entrega faltando não é erro de execução.** Quando o pedido cita análise ou insights, mas o código não chama `ctx.analisar`, o relatório sairia sem o que foi pedido, sem nenhum erro. O nó `verificar_analise` detecta e corrige essa situação.
- **Não deixar tarefas pela metade.** A tarefa nasce como `draft`; se o pipeline quebrar ou o usuário cancelar, o rascunho é apagado, e um varredor do daemon remove também os drafts órfãos com mais de 30 minutos.
- **Alertas confiáveis e interpretação explícita.** A notificação só sai na transição, e `ctx.alert(False, ...)` representa “sem dado”. Um monitor cujo pedido não cita período usa a janela `last_90d`, a mesma dos painéis padrão, para que o alerta bata com o número que o usuário vê no gráfico. E o bloco “Como interpretei o pedido” permite que o usuário corrija uma premissa errada antes de descobri-la pelo alerta.
- **Reconhecer a continuação de um pedido de agendamento.** Os botões “Agendar nova tarefa via chat” e “Editar via chat” abrem uma sessão nova já com um turno inicial gravado direto no checkpoint, sem chamar o LLM, o que ajuda o roteamento a reconhecer a continuação como pedido de agendamento mesmo sem palavras-chave.
- **Versionar e auditar.** Cada gravação arquiva a versão anterior em `task_code_versions` (com rollback por `restore_task_code_version`), e o painel de auditoria lista os erros de geração de código.

### Arquitetura

**Tick do daemon (5 s) → Tarefas `active` vencidas → Condição (monitor simples) → Sandbox (`task_code`) → Artefatos, alerta e relatório → `task_runs` → Próxima execução**

O `task_code` roda em um sandbox em que o objeto `ctx` é a única porta para fora: consulta de dados (`ctx.sql` ou `ctx.graphql_*`, conforme o modo), geração de artefatos (`ctx.save_chart`, `ctx.generate_pdf`, `ctx.generate_excel`), análise narrativa (`ctx.analisar`) e alertas (`ctx.notify`, `ctx.alert`).

### Resultados

- Relatórios recorrentes e monitores criados e editados por conversa, com o código validado antes de ser salvo.
- Execução sem LLM no caminho, com retry e backoff, versionamento do código e auditoria dos erros de geração.
- Alertas de threshold, simples ou compostos, exibidos no dashboard.

### Evidências

**Figura 24.** Pipeline de criação de tarefa em execução no chat, com o preview do artefato e o selo de salvo.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: tela do chat]**

**Figura 25.** Card da tarefa na Agenda (executar agora, pausar, ver código, histórico de execuções).  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: painel da Agenda]**

**Figura 26.** Alerta de threshold no sino do dashboard.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: sino de alertas]**

## Atividade 12: Painéis do dashboard

### Descrição

O dashboard é uma grade de painéis Chart.js, de dois tipos. Os **painéis padrão do domínio** ficam na lista `WIDGETS` do arquivo `dashboard_default.py` do pack, versionada junto a ele, visível a todos e não removível. Os **painéis customizados** ficam na tabela `dashboard_widgets` do `mfg.db`, são criados pelo usuário ao pedir ao agente que fixe um gráfico e são visíveis ao dono e, se compartilhados, a todos os usuários do domínio.

Em ambos, o que existe é código-fonte — uma função `run(from_date, to_date, ctx)` que devolve a configuração do gráfico —, e não imagens nem HTML gerado pelo agente: o `<canvas>` já está fixo no componente Angular. São aceitos os tipos `bar`, `line`, `pie`, `doughnut`, `radar`, `polarArea`, `scatter`, `bubble` e `matrix` (heatmap).

**Criação por conversa (categoria F).** O orquestrador confirma a intenção (“Vou criar e fixar um painel… Confirma?”), descobre os dados reais via `consultar_analista(para_agendamento=True)`, valida o código com `test_widget_code` e o grava com `add_chart_to_dashboard`, que coloca o painel na primeira posição da ordem do usuário e emite o evento `widget_added`. A configuração do gráfico é gerada sob demanda, a cada refresh (`GET /dashboard-widgets/{id}/data`): o código executa no sandbox contra o banco do domínio naquele instante, e o resultado é efêmero, nunca gravado de volta.

### Justificativa

Permitir que o usuário monte painéis por conversa dá autonomia sem exigir um desenvolvedor. Guardar o código, e não o gráfico renderizado, mantém o painel sempre atualizado com os dados do momento. E restringir o que o agente produz a uma configuração Chart.js validada, renderizada por um componente fixo, reduz a superfície de risco em relação a HTML gerado.

### Principais desafios

- **Período como dado, não como convenção de prompt.** Antes, `from_date` e `to_date` eram sempre “hoje menos 7 dias”, e o único jeito de um painel mostrar outro período era o próprio código recalcular a data, ignorando os parâmetros: uma convenção sustentada apenas por instrução de prompt. Hoje o período é guardado como dado estruturado (`range_unit` e `range_value`), o backend calcula as datas a partir dele a cada execução (`compute_widget_range`), e o código apenas usa o que recebeu.
- **Atualizar o dashboard sem F5.** As instâncias do Chart.js ficam centralizadas no `WidgetsService`, e não nos componentes. Os eventos SSE `widget_*` chegam ao `ChatService`, que aciona o serviço para recarregar a lista, buscar os dados de novo e redesenhar ou destruir o gráfico correspondente.
- **Sandbox próprio para painéis.** `from_date` e `to_date` chegam como strings ISO (nas tarefas são objetos `date`), e o conjunto de variáveis é menor, sem `plt`, `sns` nem `openpyxl`.
- **Manter a ordem estável quando o conjunto muda.** A ordem é salva no servidor, por usuário e domínio, e reconciliada na leitura: ids que sumiram são descartados e painéis novos entram no fim, exceto um painel criado via chat, que entra na primeira posição.
- **Heatmap.** O tipo `matrix` tem payload próprio, porque a cor de cada célula depende do mínimo e do máximo do conjunto inteiro, o que só o cliente calcula.

### Arquitetura

**Criação: Pedido → Orquestrador (categoria F) → `consultar_analista` → LLM escreve `run()` → `test_widget_code` → `add_chart_to_dashboard` → `dashboard_widgets` + evento `widget_added`**

**Refresh: `GET /dashboard-widgets/{id}/data` → `compute_widget_range` → Sandbox (`run_widget_code`) → JSON Chart.js (efêmero) → `WidgetsService` → Chart.js no `<canvas>`**

### Resultados

- Painéis customizados criados, editados, reordenados e compartilhados por conversa e por drag-and-drop.
- Painéis padrão por domínio, versionados junto ao pack.
- Dados sempre atualizados a cada refresh, sem armazenar o resultado.

### Evidências

**Figura 27.** Dashboard com painéis padrão e customizados.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: grade de painéis do dashboard]**

**Figura 28.** Painel criado por conversa.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: chat e painel fixado no dashboard]**

**Figura 29.** Código `run()` de um painel.  
**[INSERIR PRINT DO CÓDIGO: função run() de um painel, em dashboard_default.py ou no código salvo do painel]**

## Atividade 13: Interação por voz e recortes de tela

### Descrição

**Voz.** O usuário grava o comando no próprio chat (MediaRecorder, webm/opus), ouve a gravação e decide se a descarta ou a envia. Ao enviar, o áudio segue para `POST /chat/transcribe`, onde o Gemini (Vertex AI) faz uma transcrição literal, sem interpretação. A transcrição aparece como mensagem do usuário e é enviada ao agente (`GET /chat/stream`), que a processa normalmente. A transcrição usa o mesmo modelo do projeto (`MODEL_NAME`, com raciocínio no nível mínimo aceito), sem dependência de um serviço de speech externo. Depois do envio, não há etapa de edição do texto transcrito.

**Recortes de tela.** O usuário pode selecionar uma área da tela (até 3 recortes, capturados no browser com `html-to-image`) e anexá-la à mensagem. Antes de cada envio, o frontend posta em `POST /chart-snapshot` um snapshot do estado do dashboard — alertas, artefatos, tarefas e as imagens —, guardado por `session_id`. Se há imagens, a mensagem é montada de forma multimodal; o orquestrador as trata como categoria E e pode complementar com `get_dashboard_charts()`, que lê o restante do snapshot.

### Justificativa

A interação por voz reduz o esforço de digitação, e a transcrição literal, que o usuário pode conferir ouvindo a gravação antes do envio, evita que o sistema interprete ou reescreva o que foi dito. Os recortes permitem perguntar sobre o que está na tela, como “por que esse pico?”, sem descrever o gráfico.

### Principais desafios

- **Transcrever sem interpretar.** O prompt de transcrição pede a transcrição literal, e o usuário ouve a gravação antes de enviá-la, o que dá a chance de descartá-la.
- **Montar a mensagem multimodal.** Quando há recortes, `_build_human_message` combina o texto e as imagens, e o orquestrador as trata como a categoria E do roteamento.
- **Dar ao agente o estado do dashboard.** O snapshot de alertas, artefatos e tarefas é guardado por sessão e lido por `get_dashboard_charts()`. Os campos `charts` e `kpis` existem no formato, mas o frontend hoje os envia vazios: os painéis do dashboard ainda não alimentam o snapshot.

### Arquitetura

**Gravar → MediaRecorder (webm/opus) → Player (ouvir, descartar ou enviar) → `POST /chat/transcribe` → Gemini → Mensagem do usuário → `GET /chat/stream`**

**Recortes (`html-to-image`) + snapshot do dashboard → `POST /chart-snapshot` → Mensagem multimodal → Orquestrador (categoria E)**

### Resultados

- Comandos por voz e perguntas sobre o que está na tela são tratados pelo mesmo fluxo do chat.

### Evidências

**Figura 30.** Gravação de áudio com player de revisão antes do envio.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: chat com a gravação]**

**Figura 31.** Pergunta sobre um gráfico da tela com recorte anexado.  
**[INSERIR PRINT DO FRONT-END EM FUNCIONAMENTO: chat com o recorte anexado]**

## Limitações conhecidas da PoC

- **Processo único e estado em memória.** Os namespaces de DataFrames, as filas de eventos SSE, os runs ativos e o snapshot do dashboard vivem em dicts do processo, e o daemon roda dentro do processo web. Escalar horizontalmente exige externalizar esse estado (por exemplo, para um Redis) e dar um único dono ao daemon.
- **SQLite como banco local.** O `mfg.db` aceita um único escritor por vez; sob alta concorrência de escrita, isso se torna o gargalo.
- **Daemon sequencial.** As tarefas vencidas são executadas em sequência, e uma execução lenta (até o timeout de 10 minutos) atrasa as seguintes.
- **Diretório reports/ fora do volume de dados.** Em um redeploy que recrie o container, os `metadata.json` com `status: pending_send` somem, a menos que esse caminho também seja montado como volume. Os PDFs em si ficam no `mfg.db`.
- **Envio de e-mail não implementado.** O sistema prepara o metadado do relatório, mas o envio é responsabilidade de um serviço externo.
- **Segurança.** O sistema não implementa login: foi pensado para ser embutido em um app host que já resolveu a autenticação e informa o `user_id` na URL. Esse identificador não é assinado, nenhuma rota valida o chamador e algumas rotas, que operam por identificador de artefato, de sessão ou do domínio inteiro, não aplicam o filtro por usuário. O sandbox é um filtro, e não um isolamento, e o tráfego dentro do container é HTTP puro, com TLS na camada à frente. Esses pontos devem ser tratados antes de qualquer exposição além de um ambiente controlado.
- **Snapshot do dashboard.** Os painéis do dashboard ainda não alimentam o snapshot lido pelo agente (`charts` e `kpis` vazios).
