# RDA — Preparação da base de dados New Age (DL Cost) e camada de acesso GraphQL para a PoC

## Contexto

A PoC de análise de custo de mão de obra direta (DL cost, de *Direct Labor*) pago a fábricas contratadas (CMs) precisa de uma base de dados confiável antes de qualquer camada de inteligência ser construída sobre ela. Este documento registra o trabalho feito nessa primeira fase: a modelagem da base, a sua população, a validação e o aprimoramento dos dados, e a criação de uma camada de acesso em GraphQL.

Dois pontos orientam todo o texto.

1. **A base atual não contém dados reais de produção.** Ela está no ambiente de desenvolvimento (non-prod) e é composta por dados fictícios e de teste, criados apenas para viabilizar esta fase de implementação e preparação. Os dados reais começarão a entrar na próxima etapa do projeto.
2. **Os dados foram preparados pensando no consumidor seguinte.** Na próxima etapa, agentes de análise vão consultar esta base em linguagem natural. Um agente não vê a tela nem conhece o negócio: ele só enxerga as tabelas e as colunas. Por isso cada decisão aqui (nomes, relacionamentos, regras de vigência, tratamento de vazios, documentação do significado dos campos) foi tomada para que a base possa ser interpretada sem ambiguidade, e não apenas para que ela exista.

Todos os números citados abaixo foram obtidos por consultas reais à base, feitas em 14/10/2026.

---

## 1. Implementação da funcionalidade

### 1.1 Visão geral das etapas

| Etapa | O que foi feito | Resultado |
| --- | --- | --- |
| 1 | Modelagem das tabelas (dimensões, fatos e tabela ponte) | 10 tabelas relacionadas por chaves |
| 2 | População da base com dados fictícios e de teste | Cerca de 7 mil registros de fatos e 6 mil de cadastros |
| 3 | Criação da camada de acesso GraphQL | Três operações de leitura, com filtro, junção, agregação e paginação |
| 4 | Profiling e validação da integridade dos dados | Testes de chaves, de cálculos e de cobertura |
| 5 | Aprimoramento: dar sentido aos dados e documentar as regras | Base documentada, com armadilhas conhecidas e tratadas |
| 6 | Preparação para o consumo pelos agentes | Regras de interpretação e limites de uso escritos de forma explícita |

### 1.2 Etapa 1 — Modelagem das tabelas

A base segue o desenho clássico de análise de dados, com **tabelas dimensão** (o "quem, onde e quando") e **tabelas fato** (os números que serão analisados). Esse desenho foi escolhido por permitir responder perguntas de negócio do tipo "quanto custou, por site, por produto, por trimestre" com consultas simples.

**Dimensões (cadastros)**

| Tabela | O que representa | Linhas |
| --- | --- | --- |
| `dim_calendar` | Meses de 01/2024 a 12/2026, com o trimestre **fiscal** de cada um | 36 |
| `dim_site` | Sites produtivos das fábricas contratadas (a "Operação" no vocabulário do negócio) | 10 |
| `dim_product_family` | Famílias de produto (A Series, Moto G, Edge, Razr, Gaming, Tablet etc.) | 20 |
| `dim_product` | Produtos (celulares, tablets e outros), cada um ligado a uma família | 222 |
| `dim_sales_model` | Variantes comerciais de cada produto, por mercado ou região | 5.665 |

**Fatos (os números)**

| Tabela | O que registra | Linhas |
| --- | --- | --- |
| `fact_hourly_rate` | Taxa horária de mão de obra, por site, produto, área e mês | 2.527 |
| `fact_standard_time` | Tempo padrão de produção e produtividade, por área e estágio | 1.969 |
| `fact_dl_discount` | Descontos negociados com a fábrica (automação, acordo de longo prazo, trimestre a trimestre, especial) | 2.529 |
| `fact_final_dl_cost` | **Resultado consolidado**: custo final de DL por site, produto e trimestre, aberto por área e convertido para dólar | 22 |

**Tabela ponte**

| Tabela | O que representa | Linhas |
| --- | --- | --- |
| `bridge_final_dl_cost_sales_model` | Liga cada custo final aos Sales Models associados (relação muitos para muitos) | 380 |

As quatro áreas do processo produtivo aparecem de forma idêntica em três fatos e nas colunas de custo: `BACKEND`, `CFC`, `SMT` (montagem de componentes em placa) e `L5` (estágio final de montagem e teste). Manter o mesmo vocabulário em todas as tabelas foi uma escolha deliberada: quem consulta pode cruzar taxa, tempo e desconto da mesma área sem precisar traduzir termos.

**Fluxo lógico dos dados.** Os três fatos de insumo (taxa horária, tempo padrão e desconto) alimentam o cálculo do fato final. O custo final é, portanto, um *resultado*, e não um dado digitado. É por isso que ele tem poucas linhas (22) e é a melhor porta de entrada para perguntas de "quanto custou".

### 1.3 Etapa 2 — População da base

Como ainda não há dados de produção, a base foi povoada em ambiente de desenvolvimento com cadastros e fatos de teste. Algumas características da população merecem registro, porque determinam o que é possível analisar hoje:

- **Cadastros com aparência de produção.** Os sites trazem fábricas e países reais do setor (BPL/HUAQUIN e WUH/LENOVO CN na China, BAT/PTSN na Indonésia, GEM/NEOLYNC e NOI/DIXON na Índia, JAG e MAN/FLEX no Brasil, TDF/NEWSAN na Argentina). Os produtos usam codinomes no padrão comercial e estão distribuídos pelas 20 famílias (207 celulares, 14 tablets e 1 outro). O catálogo de Sales Models tem 5.665 itens, com nome e descrição. Existe ainda um site claramente de teste, o `Test` (empresa `CM-Alert`, país `System`), além do `APEX` (empresa `APEX`, Índia), que não aparece em nenhum fato.
- **Fatos carregados em ondas, pelo fluxo de aprovação.** Os registros de taxa horária carregam a data de aprovação, e ela mostra que a carga aconteceu em lotes ao longo de cinco meses: 388 aprovações em maio/2026, 21 em junho, 796 em julho, 5 em agosto, 786 em setembro e 196 em outubro. Cada lote substituiu valores anteriores, o que gerou o histórico de versões (1.509 taxas `EXPIRED` contra 1.018 `APPROVED`).
- **Valores de teste, sem significado econômico.** A taxa horária varia de 1 a 99, com mediana 5 e valores inteiros pequenos. O custo final mais alto da base é de US$ 700, e há linhas com custo de US$ 1 a 2. Esses números servem para testar relacionamentos e cálculos, e não para tirar conclusões sobre a operação.
- **Cobertura desigual, de propósito ou por acaso.** O site WUH concentra 2.154 das 2.527 taxas horárias (85%); GEM tem 4; MAN só começa em setembro/2026; quatro dos dez sites (TDF, NOI, APEX e Test) não têm nenhuma taxa. O custo final cobre apenas 11 dos 222 produtos. Essa irregularidade se tornou, na verdade, útil: obrigou a preparação a lidar com os casos difíceis que a produção também terá.

### 1.4 Etapa 3 — Camada de acesso em GraphQL

A partir do banco, foi construída uma camada de acesso em GraphQL. Em vez de expor o banco diretamente (com consultas escritas à mão para cada pergunta), a base passou a ser lida por um único endereço, com uma linguagem de consulta que o próprio servidor entende e valida. O acesso é feito com chave de API guardada em variável de ambiente, e todas as operações são **somente leitura**.

A camada oferece três operações:

| Operação | Para que serve |
| --- | --- |
| `reportTable` | Devolve linhas de uma tabela, com filtro, ordenação, paginação e junção com uma tabela relacionada |
| `reportAggregate` | Soma, conta, tira média, mínimo ou máximo **já agrupados no servidor**, sem trazer a tabela inteira |
| `reportSchema` | Devolve a estrutura real da base: tabelas, colunas, tipos e relacionamentos |

**O que o GraphQL facilita.** Foi uma escolha por benefícios concretos, todos verificados na prática:

1. **Endereço único e linguagem uniforme.** Todas as 10 tabelas são lidas do mesmo jeito, sem escrever uma consulta diferente por tabela.
2. **Estrutura consultável.** O `reportSchema` permite conferir a qualquer momento se a documentação ainda bate com a base. Foi assim que se descobriu que as tabelas `dim_sales_model` e `bridge_final_dl_cost_sales_model` tinham sido adicionadas ao servidor depois da primeira verificação.
3. **Filtros tipados.** Doze operadores (igual, diferente, maior, menor, está em, contém, é nulo etc.), combinados entre si, evitam trazer dados desnecessários.
4. **Junção em um salto (`expand`).** Uma única chamada traz, por exemplo, a taxa horária já com o nome e a empresa do site, sem exigir uma segunda consulta e um cruzamento manual.
5. **Agregação no servidor.** "Custo total por site" é calculado onde os dados estão, e devolve poucas linhas em vez de milhares.
6. **Seleção de colunas.** Só viaja o que foi pedido, o que reduz tempo e volume de dados.
7. **Limites de proteção.** O servidor recusa pedidos com mais de 1.000 linhas e limita as agregações a 500 grupos. A recusa é explícita (erro), e não silenciosa, o que evita análises feitas sobre uma tabela cortada sem aviso.
8. **Proteção de campos sensíveis.** As colunas de custo de `fact_final_dl_cost` são criptografadas pelo servidor quando acessadas por junção a partir de outra tabela, e só voltam em claro quando lidas diretamente. É um controle de segurança do próprio GraphQL, que exigiu tratamento (ver seção 1.6).

**Como o agente usará a camada (visão breve).** Na próxima etapa, um agente analista fará essas consultas a partir de uma pergunta em linguagem natural, escolhendo entre linhas cruas (`reportTable`) e valores agregados (`reportAggregate`) e consultando o `reportSchema` apenas em caso de dúvida. A camada foi desenhada para que essa escolha seja simples e segura.

### 1.5 Etapa 4 — Profiling e validação dos dados

Antes de qualquer análise, a base foi submetida a um conjunto de verificações. O objetivo era responder duas perguntas: *os dados são consistentes entre si?* e *onde eles NÃO devem ser usados?*

**Testes de integridade — todos aprovados**

| Verificação | Resultado |
| --- | --- |
| Chaves primárias duplicadas nas 9 tabelas com `id` | 0 |
| Registros órfãos (chave estrangeira sem correspondente) em 11 relacionamentos | 0 em todos |
| Soma das 4 áreas (`backend` + `cfc` + `smt` + `l5`) igual ao custo total | 22 de 22 corretos |
| Custo em dólar igual ao custo em moeda local dividido pelo câmbio | 22 de 22 corretos |
| Rótulo do trimestre (ex.: `Q2,2026`) coerente com a janela de datas da linha | 22 de 22 corretos |
| Trimestre fiscal do `dim_calendar` correto para os 36 meses | 36 de 36 corretos |
| Janelas de desconto com data final anterior à inicial | 0 |
| Descontos percentuais acima de 100% | 0 |
| Relação entre `upph` e o inverso do tempo padrão | 100% coerente |

**Achados que exigiram tratamento — o que realmente tornou a base útil**

| Achado | Dimensão do problema | Por que importa para a análise |
| --- | --- | --- |
| `month_id` do custo final fora do trimestre da própria linha | 16 de 22 linhas | Filtrar por `month_id` devolveria o período errado; o filtro correto usa `start_month` e `end_month` |
| Custo final repetido ao ligar com a ponte de Sales Models | 22 linhas viram 380, e a soma sobe de US$ 1.487 para US$ 13.289 (8,9 vezes) | Somar custo "por Sales Model" infla o total; o custo é do grupo produto + site + trimestre |
| Colunas inteiras vazias em `fact_standard_time` | `site_id`, `product_id` e `sales_model_id` 100% nulos em 1.969 linhas | Tempo padrão não pode ser cortado por site ou produto |
| Colunas vazias em `fact_final_dl_cost` | Desconto LTA, configuração QoQ, tempo padrão de referência, taxa horária e data de aprovação 100% nulos | Não é possível reconstruir o cálculo a partir do resultado |
| Descontos trimestrais sem tipo de valor | 593 (todos do tipo QOQ) | Não se sabe se o número é percentual ou valor absoluto |
| Descontos com janela de zero dia | 272 de 2.529 | Parecem lançamentos pontuais; só 28 estão vigentes |
| Tempo padrão igual a zero | 90 linhas | Zero é ausência de dado, e não "produção instantânea" |
| Versões de taxa para a mesma combinação site + produto + área + mês | 87 combinações | Exigem regra para escolher a versão certa |
| Trimestres futuros misturados a realizados | 7 das 22 linhas são do Q3/2026 (out a dez), que ainda não começou | Sem coluna de previsão na tabela, só a data revela que não é realizado |

### 1.6 Etapa 5 — Aprimoramento: dar sentido aos dados fictícios

Povoar uma base com valores de teste não basta: sem entender o que cada coluna representa, as análises seriam "surreais", isto é, formalmente corretas e sem sentido de negócio. O trabalho de aprimoramento consistiu em **dar semântica aos dados**, em quatro frentes.

**a) Descobrir o que cada campo significa.** Cada coluna foi confrontada com o vocabulário do negócio, e o resultado foi escrito em um documento de domínio (cerca de 840 linhas) que descreve cada tabela, cada coluna e cada valor possível de campos de classificação. Exemplos: o que são `RFQ`, `THIRTY_DAYS_REVIEW` e `POST_30DAYS` (estágios de validação do tempo padrão, da cotação inicial ao pós 30 dias); o que distingue `LTA`, `QOQ`, `AUTOMATION` e `SPECIAL` (tipos de desconto); o que significa `EXPIRED` (versão substituída, mantida por histórico). Termos que não puderam ser confirmados com o negócio, como a unidade do tempo padrão e o significado de `sa_date`, foram marcados como **não confirmados**, em vez de preenchidos por suposição.

**b) Comprovar as relações matemáticas.** Os cálculos foram verificados contra os dados, e não apenas descritos. Foi assim que se confirmou que o custo em dólar é uma **conversão de câmbio** (por exemplo, 14 dividido por 2 dá 7), e não uma subtração de desconto. Essa distinção evita uma explicação inventada: o texto de domínio registra um caso real em que uma resposta apresentou um "desconto" que não existia na tabela.

**c) Tratar o tempo corretamente.** O calendário fiscal é o ponto que mais distorce análises. O ano fiscal vai de abril a março (abr a jun é o Q1, jan a mar é o Q4) e é nomeado pelo ano em que **começa**. Assim, janeiro de 2026 pertence ao FY2025 Q4, o oposto do que a coluna `year` sugere à primeira vista. A regra foi escrita e testada contra os 36 meses do calendário. Também foi definido que "último trimestre" significa o **último trimestre fechado**, e nunca o que está em andamento.

**d) Reconstruir a taxa horária vigente.** A taxa horária segue a regra de vigência de uma dimensão que muda ao longo do tempo: um novo valor aprovado para a mesma combinação site + produto + área expira o anterior, mas só vale a partir do seu mês de referência. Portanto, saber "qual a taxa vigente em um mês" exige olhar o valor mais recente com mês igual ou anterior, incluindo os registros `EXPIRED`. O efeito de ignorar essa regra foi medido com dados reais:

| Site | Média ingênua (só `APPROVED`) | Taxa vigente em ago/2026 (reconstruída) |
| --- | --- | --- |
| JAG | US$ 73,25 | US$ 73,25 |
| BPL | US$ 47,91 | US$ 77,00 |

Pela média ingênua, o JAG parece mais caro que o BPL. Reconstruindo o vigente mês a mês, o BPL é mais caro. **A conclusão se inverte**, e é exatamente o tipo de erro que uma base fictícia mal compreendida produziria sem que ninguém percebesse.

Outras decisões de aprimoramento seguiram a mesma lógica:

- **Granularidade e Sales Model.** Ficou registrado que dá para dizer *quais* Sales Models pertencem a um custo, mas não *quanto custou cada um*, e que perguntas sobre "Sales Model mais barato" não têm resposta nesta base.
- **Empresa versus site.** Uma mesma fábrica pode ter mais de um site (a FLEX tem JAG e MAN, ambos no Brasil), e "custo da FLEX" é diferente de "custo do JAG". Também ficou escrito que a média de uma empresa deve ser calculada sobre as linhas cruas, e não como média das médias por site.
- **Colunas cuja ausência precisa ser dita.** Para cada coluna sempre vazia, o documento diz isso de forma explícita, para que um consumidor não tome o vazio por zero.

### 1.7 Etapa 6 — Preparação para o uso pelos agentes

A etapa final transforma o que foi aprendido em **regras de uso**, escritas para quem vai consultar a base sem conhecê-la. Os principais pontos:

- **Ordem de consulta recomendada**: começar pelo custo final (pequeno e consolidado), usar agregação no servidor para totais e linhas cruas só quando for preciso combinar colunas.
- **Limites da camada** documentados: teto de 1.000 linhas por chamada, necessidade de paginar as três tabelas maiores, datas devolvidas com hora, valores numéricos que podem chegar como texto.
- **Avisos antecipados** ao analista sobre as duas limitações mais comuns (custo por Sales Model e tempo padrão por site ou produto), para evitar idas e voltas.
- **Período padrão**: quando o usuário não informa período, vale o último trimestre fechado.
- **Alertas sobre quando não concluir**: comparações que misturam trimestre fechado e em andamento, e rankings com um único candidato, não devem gerar conclusão.
- **Conferência da própria documentação**: o `reportSchema` serve para detectar quando o texto de domínio ficou desatualizado em relação ao servidor.

---

## 2. Justificativa da atividade

**Os resultados de uma análise nunca são melhores que os dados em que ela se apoia.** Como a PoC vai entregar respostas em linguagem natural, qualquer ambiguidade na base se transforma em resposta errada com aparência de certeza. Preparar a base antes de construir a camada de análise reduz esse risco na origem.

**Por que dados fictícios nesta fase.** Usar dados de teste permite construir e validar o desenho, os relacionamentos, os cálculos e as regras de interpretação sem expor informação sensível de custo e de negociação com fábricas contratadas. Também separa dois riscos: o de a base estar mal desenhada e o de os dados reais virem com problemas próprios.

**Por que não bastava popular.** Uma base fictícia sem entendimento do significado de cada campo produz análises sem sentido de negócio. O trabalho de profiling mostrou que os problemas mais graves não eram erros de cálculo (todos os testes de cálculo passaram), e sim **armadilhas de interpretação**: um calendário fiscal que contraria a coluna de ano, uma taxa cuja vigência não é o seu status, um custo que se multiplica ao ser cruzado com a ponte. Nenhuma delas é detectável por quem apenas olha a tabela.

**Por que GraphQL.**

- Evita escrever e manter uma consulta por pergunta de negócio.
- Valida a consulta no servidor, o que reduz erros de formato.
- Permite agregar e juntar no servidor, trazendo menos dados e tornando as respostas mais rápidas.
- Expõe a estrutura da base de forma consultável, o que ajuda a manter a documentação em dia.
- Permite impor limites e proteger campos sensíveis em um único ponto.
- Isola o consumidor do banco: a consulta passa por um contrato controlado, somente leitura, e não por acesso direto às tabelas.

**Por que preparar já para os agentes.** Um agente consulta o banco como um analista novo, que só conhece o que está escrito. Quanto mais regras de negócio estiverem documentadas e quanto menos armadilhas estiverem escondidas, menor a chance de resposta inventada. Deixar essa preparação para depois obrigaria a refazer a validação já com dados reais, em um momento em que o custo de errar é maior.

---

## 3. Resultado obtido da atividade

**Base modelada, populada e validada.** São 10 tabelas relacionadas, com cerca de 7 mil registros de fatos e 6 mil de cadastros, sem chaves duplicadas e sem registros órfãos em todos os relacionamentos verificados. Os cálculos do custo final fecham em 100% das linhas (soma das áreas e conversão para dólar).

**Camada GraphQL funcionando.** A conexão foi testada em 14/10/2026: a chave foi aceita, o `reportSchema` devolveu as 10 tabelas e uma consulta de dados devolveu registros reais do ambiente de desenvolvimento. As três operações (linhas, agregação e estrutura) estão disponíveis, com limites de proteção e leitura somente.

**Problemas conhecidos mapeados e documentados.** Nove achados de qualidade foram identificados, medidos e tratados com uma regra de uso. Entre eles, as quatro armadilhas de maior impacto: o filtro de período do custo final, o custo que se multiplica na ponte de Sales Models, o calendário fiscal e a vigência da taxa horária. Esta última foi comprovada com dados reais, pois a média ingênua inverte o ranking entre dois sites.

**Documento de domínio completo.** A base conta com um documento de cerca de 840 linhas que reúne o glossário de negócio, o dicionário de cada tabela e coluna, o grafo de relacionamentos, as regras de período e vigência e os padrões de pergunta validados. Os pontos não confirmados com o negócio estão marcados como tal.

**Limites assumidos com transparência.**

| Limitação | Efeito |
| --- | --- |
| Os valores são de teste e sem significado econômico | Nenhum número atual deve ser usado como conclusão de negócio |
| `fact_standard_time` sem site, produto ou Sales Model | Perguntas de tempo padrão por site ou produto não têm resposta |
| Custo final cobre 11 de 222 produtos e 22 linhas | Séries temporais por produto são raras |
| Custo não é dividido por Sales Model | "Quanto custou o Sales Model X" não tem resposta exata |
| Cobertura de taxa desigual entre sites (WUH com 85% das linhas) | Comparações entre sites exigem verificar a profundidade de histórico |
| Algumas colunas do custo final sem preenchimento | O cálculo não pode ser refeito a partir do resultado |

**Preparação para a próxima etapa.** Com a base desenhada, validada e documentada, a entrada dos dados reais passa a ser um processo de **repetir as verificações já definidas** (integridade de chaves, fechamento dos cálculos, cobertura por site e produto, preenchimento das colunas hoje vazias) e de **confirmar com o negócio os pontos ainda em aberto** (unidade do tempo padrão, significado de `sa_date`, tipo de valor dos descontos QoQ). O resultado esperado é que as consultas dos agentes, que dependem de uma base interpretável, já nasçam sobre dados que o time sabe ler.
