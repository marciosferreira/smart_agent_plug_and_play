# RDA — Atividade 6: preparação e transformação dos dados do módulo Direct Labor (DL Cost) e camada de acesso GraphQL

## Contexto

Este capítulo registra trabalho executado no âmbito da **Atividade 6 – Modelagem, Treinamento e Validação de Algoritmos de Inteligência Artificial** do Plano de Trabalho do projeto **New Age – Plataforma para Gestão Integrada de Custos Globais**. A Atividade 6 conduz pesquisas aplicadas com diferentes abordagens de Inteligência Artificial, com foco na análise preditiva e na otimização de custos, e o seu processo experimental começa pela **preparação e transformação dos dados**. É essa primeira etapa que este capítulo descreve. O trabalho também atende ao objetivo específico 2026/2027 de *"desenvolver estrutura de dados complexa para persistência, digitalização de dados e ingestão, preparando a estrutura de dados para algoritmos de inteligência artificial"*.

O escopo é o módulo **Direct Labor**, que trata do custo de mão de obra direta pago às fábricas contratadas e cujos submódulos (Standard Time, Hourly Rate, DL Discount e Final DL Cost) foram digitalizados em 2025. A atividade prepara a base desse módulo para uma prova de conceito (PoC) em que agentes de inteligência artificial respondem perguntas de negócio em linguagem natural. Antes de construir essa camada de inteligência, era preciso uma base confiável e interpretável.

Dois pontos orientam todo o texto.

1. **A base atual não contém dados reais de operação.** Ela está no ambiente de validação experimental e é composta por dados mockados, isto é, dados fictícios criados para simular a operação. A carga de dados reais está prevista para as próximas etapas.
2. **Os dados foram preparados pensando em quem vai consumi-los.** O agente de IA não usa a tela da plataforma: ele trabalha com duas fontes, a própria base de dados e um arquivo de configuração que lhe transmite o conhecimento do negócio (o documento de domínio, descrito na seção 1.6). Por isso as decisões desta etapa foram tomadas para que essas duas fontes, juntas, permitam interpretar os dados sem ambiguidade.

**Relação com o processo experimental da Atividade 6.** A tabela abaixo mostra quais itens do processo previsto no Plano de Trabalho foram tratados nesta etapa e quais ficam para as etapas seguintes da atividade, que se estende até 2028.

| Item previsto no Plano de Trabalho | Situação nesta etapa |
| --- | --- |
| Preparação e transformação de dados em larga escala | **Executado**: modelagem, população, validação e camada de acesso aos dados. A estrutura e as verificações foram desenhadas para serem repetidas sobre o volume dos dados reais |
| Engenharia de atributos | **Iniciado**: definição de informações derivadas que os modelos vão usar, como o valor vigente em cada mês e o período fiscal de cada registro |
| Validação e análise estatística | **Iniciado sobre os dados**: testes de consistência e medição dos problemas encontrados. A validação dos modelos fica para as etapas seguintes |
| Análise de sensibilidade | **Iniciado sobre os dados**: medição de quanto uma conclusão muda conforme o método de cálculo |
| Comparação entre diferentes abordagens | **Iniciado**: comparação entre hipóteses de interpretação, entre métodos de cálculo e entre arquiteturas de acesso aos dados |
| Treinamento de múltiplos modelos de Machine Learning | Próximas etapas, com os dados reais |
| Ajuste de hiperparâmetros | Próximas etapas |
| Métricas de erro médio, precisão e capacidade de generalização | Próximas etapas, para os modelos. Nesta etapa, as métricas aplicadas foram de qualidade dos dados |

Entre as diferentes abordagens de IA previstas na atividade, a primeira a ser investigada é a de **agentes de análise em linguagem natural**. A mesma base preparada aqui serve também aos modelos preditivos de custo das etapas seguintes.

Como o projeto é de Pesquisa e Desenvolvimento Experimental, a atividade foi conduzida por ciclos de experimentação: formulação de hipóteses sobre o significado e o comportamento dos dados, teste dessas hipóteses contra a base, comparação entre abordagens alternativas e registro das incertezas que permaneceram em aberto.

---

## 1. Implementação da funcionalidade

### 1.1 Visão geral das etapas

| Etapa | O que foi feito | Resultado |
| --- | --- | --- |
| 1 | Modelagem da base de dados | Estrutura que organiza os cadastros e os valores de custo do módulo Direct Labor |
| 2 | População com dados mockados | Base completa para testes, com cerca de 13 mil registros |
| 3 | Criação da camada de acesso aos dados | Ponto único e seguro de consulta, preparado para uso pela IA |
| 4 | Validação dos dados | Consistência confirmada e problemas de interpretação identificados |
| 5 | Aprimoramento e documentação | Regras de interpretação definidas e registradas no documento de domínio |
| 6 | Preparação para o uso pelos agentes | Regras de consulta e limites de uso definidos |

### 1.2 Etapa 1 — Modelagem da base

A base foi organizada em dois grupos de informação: os **cadastros** (sites produtivos das fábricas contratadas, produtos, famílias de produto, variantes comerciais e calendário) e os **valores de custo**, que correspondem aos submódulos do Direct Labor: taxa horária de mão de obra (Hourly Rate), tempo padrão de produção (Standard Time), descontos negociados com as fábricas (DL Discount) e o custo final consolidado (Final DL Cost), também convertido para dólar. O custo final é calculado a partir dos três primeiros, e por isso é a principal fonte para perguntas sobre "quanto custou". Esse desenho permite responder perguntas de negócio, como o custo por site, por produto ou por trimestre, com consultas simples.

No Plano de Trabalho, esta etapa corresponde ao item "preparação e transformação de dados em larga escala" da Atividade 6 e ao objetivo específico de desenvolver estrutura de dados complexa preparada para algoritmos de IA. A organização adotada é a que os modelos preditivos e os agentes vão consumir.

### 1.3 Etapa 2 — População da base

Como ainda não há dados reais de operação, a base foi povoada com dados mockados que reproduzem a estrutura da operação: sites nos países em que o projeto atua, produtos organizados em famílias e um histórico de versões dos valores, gerado pela carga em lotes ao longo de 2026. Os valores numéricos não têm significado econômico e servem apenas para testar relacionamentos e cálculos. A cobertura dos dados é propositalmente desigual entre sites e produtos, o que obrigou a preparação a lidar com os casos difíceis que a operação real também terá.

A população da base também faz parte da preparação e transformação de dados prevista na Atividade 6. O uso de dados mockados permitiu conduzir a parte experimental da atividade sem expor dados sensíveis de custo.

### 1.4 Etapa 3 — Camada de acesso aos dados

O Plano de Trabalho prevê APIs no padrão REST para a comunicação da plataforma web, e esse continua sendo o padrão da aplicação. Para o consumo pela IA, porém, foram comparadas duas abordagens: um conjunto de consultas fixas, uma para cada tipo de pergunta, ou uma camada de consulta flexível em GraphQL. A hipótese era que um agente, que formula perguntas variadas e imprevisíveis, seria mal atendido por consultas fixas, já que cada pergunta nova exigiria uma consulta nova. A camada GraphQL foi adotada porque oferece um ponto único de consulta, somente leitura e validado, no qual a própria base faz cruzamentos e totais sem que o agente precise trazer todos os dados.

A camada também concentra os controles de segurança: os valores de custo mais sensíveis ficam criptografados no próprio banco e só são decifrados pelo servidor na leitura direta, apenas para as linhas devolvidas; acessados de forma indireta, por junção a partir de outra tabela, continuam criptografados. Isso está alinhado ao objetivo do projeto de desenvolver mecanismos de segurança da informação com criptografia de dados e controle de acesso.

A avaliação das duas arquiteturas atende ao item "comparação entre diferentes abordagens" do processo experimental previsto no Plano de Trabalho. A camada também prepara o acesso das diferentes abordagens de IA que a Atividade 6 vai investigar.

### 1.5 Etapa 4 — Validação dos dados

A base foi submetida a um conjunto de verificações, cada uma formulada como hipótese de consistência e testada contra todos os registros. As verificações de estrutura e de cálculo foram todas aprovadas: não há registros duplicados nem referências quebradas, e os totais calculados fecham com as suas partes em todos os casos.

Os problemas mais relevantes não eram erros de cálculo, e sim **armadilhas de interpretação**, situações em que uma leitura aparentemente correta dos dados leva a uma conclusão errada. Foram identificados nove casos desse tipo. Entre eles: um campo de data que não corresponde ao período real do registro; um custo que se multiplica artificialmente quando cruzado com a lista de variantes comerciais; informações ausentes que poderiam ser confundidas com valor zero; e registros de um trimestre ainda em andamento misturados aos trimestres já fechados. Cada caso foi medido e recebeu uma regra de tratamento.

Este conjunto de verificações aplica aos dados o item "validação e análise estatística" da Atividade 6. A validação dos modelos fica para as etapas seguintes da atividade.

### 1.6 Etapa 5 — Aprimoramento e documentação

Uma base com dados fictícios, sem o entendimento do que cada informação representa, produz análises formalmente corretas, porém sem sentido de negócio. O aprimoramento consistiu em dar significado aos dados, em quatro frentes.

**Documentação do significado.** Cada informação da base foi confrontada com o vocabulário do negócio, e o resultado foi registrado em um documento de domínio, que é o arquivo de configuração pelo qual o agente recebe o conhecimento do negócio. Pontos que não puderam ser confirmados com a área de negócio foram marcados como tal, em vez de preenchidos por suposição, e ficam registrados como incertezas da atividade.

**Teste de hipóteses concorrentes.** Em alguns casos, uma mesma diferença nos dados admitia mais de uma explicação. Por exemplo, a diferença entre dois valores de custo podia ser um desconto ou uma conversão de moeda. As hipóteses foram testadas contra os dados, e apenas a conversão de moeda se confirmou. Isso evita que a IA apresente ao usuário um desconto que não existe.

**Tratamento do tempo.** O ano fiscal usado pelo negócio não coincide com o ano civil, o que leva facilmente a atribuir um registro ao período errado. A regra de conversão foi definida e testada, e ficou estabelecido que, quando a pergunta não informa período, vale o último trimestre já fechado, e não o que está em andamento.

**Escolha do método de cálculo.** Os valores de taxa horária mudam ao longo do tempo, e um valor novo só passa a valer a partir de uma data. Foram comparados dois métodos para obter a taxa de um período: uma média simples dos valores aprovados e a reconstrução do valor efetivamente vigente em cada mês. Na comparação entre dois sites, os dois métodos levaram a conclusões opostas sobre qual deles era mais caro. O segundo método foi adotado, e o resultado mostrou como uma interpretação ingênua dos dados pode inverter uma conclusão sem que ninguém perceba.

Em relação ao Plano de Trabalho, esta etapa cobre três itens do processo experimental da Atividade 6. A definição de informações derivadas, como o valor vigente em cada mês e o período fiscal, é o início da engenharia de atributos. A medição de quanto a conclusão muda conforme o método de cálculo é uma análise de sensibilidade. E o teste de hipóteses concorrentes, com escolha baseada em evidência, é uma comparação entre abordagens. Os pontos não confirmados registram a incerteza tecnológica que, segundo o plano, caracteriza a atividade.

### 1.7 Etapa 6 — Preparação para o uso pelos agentes

A etapa final transformou o que foi aprendido em regras de uso para quem vai consultar a base sem conhecê-la: por onde começar uma consulta, qual período adotar quando a pergunta não informa, quais perguntas a base não consegue responder e em que situações uma comparação não deve gerar conclusão.

Com isso, fica preparada a primeira das diferentes abordagens de IA previstas na Atividade 6, os agentes de análise em linguagem natural, e ficam definidas as regras que servirão para validar as respostas desses agentes nas etapas seguintes.

---

## 2. Justificativa da atividade

**Se os dados estiverem errados ou forem mal interpretados, a análise também estará, por melhor que seja o método.** Como a PoC vai entregar respostas em linguagem natural, qualquer ambiguidade na base se transforma em resposta errada com aparência de certeza. Por isso o Plano de Trabalho coloca a preparação e a transformação dos dados como o primeiro passo do processo experimental da Atividade 6: não é possível treinar um modelo, nem validar a resposta de um agente, sobre uma base cujas regras de interpretação não estão definidas.

**Por que dados mockados nesta fase.** Usar dados fictícios permite construir e validar a estrutura, os cálculos e as regras de interpretação sem expor informação sensível de custo e de negociação com as fábricas contratadas. Também separa dois riscos: o de a base estar mal desenhada e o de os dados reais virem com problemas próprios.

**Por que não bastava popular a base.** A validação mostrou que os problemas mais graves não eram erros de cálculo, e sim armadilhas de interpretação que não são detectáveis por quem apenas olha os dados. Identificá-las exigiu formular hipóteses, testá-las e comparar métodos alternativos, o que caracteriza a incerteza técnica tratada nesta atividade.

**Por que preparar já para os agentes.** O agente conhece o negócio apenas pelo que está escrito no arquivo de configuração e pelo que a própria base revela. Quanto mais regras estiverem documentadas e quanto menos armadilhas estiverem escondidas, menor a chance de resposta inventada. Deixar essa preparação para depois obrigaria a refazer a validação já com dados reais, quando o custo de errar é maior.

Essa justificativa acompanha a que o Plano de Trabalho dá para a Atividade 6: a atividade envolve incerteza tecnológica e experimentação iterativa, com testes sucessivos e análise comparativa entre abordagens. Nesta etapa, a experimentação se deu sobre os dados; nas seguintes, se dará sobre os modelos.

---

## 3. Resultado obtido da atividade

**Entregáveis**

- Base de dados do módulo Direct Labor modelada e populada com dados mockados, no ambiente de validação experimental.
- Camada de acesso aos dados em GraphQL, somente leitura, com controles de segurança e criptografia dos valores sensíveis.
- Conjunto de verificações de consistência, reaplicável quando os dados reais forem carregados.
- Documento de domínio, usado como arquivo de configuração do agente, com o significado dos dados e as regras de interpretação. O detalhamento técnico da base (tabelas, campos e regras) está registrado nesse documento.
- Regras de uso da base para os agentes de análise da PoC.

**Problemas conhecidos mapeados.** Nove armadilhas de interpretação foram identificadas, medidas e tratadas com uma regra de uso. A mais significativa, a forma de obter a taxa vigente, foi comprovada pela comparação entre dois métodos de cálculo, que levaram a conclusões opostas.

**Limites assumidos.** Os valores atuais são fictícios e não devem ser usados como conclusão de negócio. Algumas perguntas não têm resposta nesta base, por exemplo o custo de cada variante comercial isoladamente, porque a informação não existe nesse nível de detalhe. A cobertura é desigual entre sites e produtos, o que exige cuidado em comparações.

**Preparação para a próxima etapa.** Com a base desenhada, validada e documentada, a entrada dos dados reais passa a ser um processo de repetir as verificações já definidas e de confirmar com a área de negócio os pontos ainda em aberto. Sobre essa base seguem as próximas etapas da Atividade 6: a validação dos agentes de análise e o treinamento, a validação cruzada e a comparação de modelos preditivos de custo, com as métricas previstas no Plano de Trabalho (erro médio, precisão e capacidade de generalização).
