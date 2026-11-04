# Instruções para gerar o documento Word do RDA

## Tarefa

Converter `RDA_AI_Capex_v4_com_figuras.md` em um arquivo `.docx`, mantendo o texto exatamente como está e inserindo as imagens da pasta `figuras_rda/` nos locais já indicados no Markdown.

## O que vem no pacote

| Item | Para que serve |
| --- | --- |
| `RDA_AI_Capex_v4_com_figuras.md` | Documento-fonte (português). Cada figura já está referenciada como `![Figura N — ...](figuras_rda/figura_NN.png)`, logo abaixo da legenda `**Figura N.** ...` |
| `figuras_rda/figura_NN.png` | 31 imagens PNG, numeradas de 01 a 31, todas em resolução 2x |
| `figuras_rda/manifesto_figuras.json` | Para cada figura: legenda, seção (Atividade) do RDA, tipo, origem, tamanho em pixels e **largura sugerida em cm** |

Os caminhos das imagens são relativos: o `.md` e a pasta `figuras_rda/` precisam ficar lado a lado.

**Integridade:** todas as imagens são PNG reais, com as dimensões do manifesto (campos `largura_px`, `altura_px`, `bytes` e `sha256`). Se as imagens que você recebeu forem menores que o manifesto ou estiverem em JPEG, elas foram recomprimidas no upload: peça o `.zip` do pacote em vez de usar imagens avulsas.

## Regras

1. **Não reescreva, traduza nem resuma o texto.** O Markdown é a fonte da verdade. Só a formatação muda.
2. **Estrutura:** `#` = título do documento; `##` = Atividade (ou "Limitações conhecidas da PoC"); `###` = subseções (Descrição, Justificativa, Principais desafios, Arquitetura, Resultados, Evidências). Use os estilos de título do Word (Título 1/2/3), de modo que o sumário automático funcione.
3. **Tabelas Markdown viram tabelas do Word**, com linha de cabeçalho em negrito e bordas. Trechos entre crases (`assim`) ficam em fonte monoespaçada. As linhas em negrito com setas (`→`) são diagramas de fluxo em texto: mantenha-as como parágrafos em negrito.
4. **Figuras:**
   - Cada figura é composta por duas partes consecutivas: o parágrafo da legenda (`**Figura N.** ...`) e, logo abaixo, a imagem. Mantenha as duas juntas, sem quebra de página entre elas ("manter com o próximo").
   - Centralize a imagem e preserve a proporção.
   - Largura: use `largura_sugerida_cm` do manifesto (nunca mais que 16 cm de largura nem 15 cm de altura em A4 com margens de 2,5 cm). Não recorte, não gire e não altere as imagens.
5. **Legendas das Figuras 8 e 9:** foram ajustadas no `.md` para descrever o que as imagens mostram. Use sempre a legenda do `.md` atual, não a de versões anteriores do documento.
6. Idioma do documento: português do Brasil. Não adicione capa, índice de figuras ou texto que não esteja no `.md`, a menos que o usuário peça.

## Como fazer (sugestão)

- `pandoc RDA_AI_Capex_v4_com_figuras.md -o RDA_AI_Capex_v4.docx --resource-path=.` resolve a maior parte; depois ajuste a largura das imagens conforme o manifesto. Alternativas: `python-docx` ou `docx` (JS), lendo o `.md` e o manifesto.
- Para o sumário, insira um campo de sumário do Word (títulos 1 a 3).

## Checklist antes de entregar

- [ ] 31 imagens inseridas, cada uma logo abaixo da sua legenda `Figura N.`
- [ ] 31 legendas presentes, na ordem 1 a 31
- [ ] Nenhum texto literal `![Figura` ou `figuras_rda/` aparecendo no documento
- [ ] Tabelas, listas e títulos conforme o `.md`; nenhuma imagem cortada ou além da margem
- [ ] Texto idêntico ao do Markdown (nenhuma frase alterada)
