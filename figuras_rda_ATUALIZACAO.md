# Atualização das figuras — o que mudou e o que fazer

Se o `.docx` já foi montado, **substitua estas 7 imagens** (mesmo nome, mesma posição) e **atualize as legendas das Figuras 8 e 9** com o texto do `RDA_AI_Capex_v4_com_figuras.md` que veio neste pacote. O restante do texto do RDA não muda.

| Figura | Arquivo | O que mudou |
| --- | --- | --- |
| 1 | `figura_01.png` | Passou a mostrar só o registro de tools, `bind_tools`, `ToolNode`, o roteamento e `compile(checkpointer)` (corpo do nó omitido). Ficou mais baixa. |
| 5 | `figura_05.png` | Sem as linhas comentadas do Posh e sem o bloco `idioma_default`. Mantém `modo: sql`. |
| 6 | `figura_06.png` | Barra de título só com `dominio.md` (sem pasta do pack nem idioma). |
| 8 | `figura_08.png` | **Nova.** Trecho do `dominio.md` (seção Domínio de negócio, L437–470). Substitui o print do RDA anterior. |
| 9 | `figura_09.png` | **Nova.** Trecho do `dominio.md` (tabela de colunas com semântica especial, L274–284). Substitui o print do RDA anterior. |
| 20 | `figura_20.png` | Título só com `dominio.md`. |
| 29 | `figura_29.png` | Título só com `dashboard_default.py`. |

## Legendas novas (Figuras 8 e 9)

- **Figura 8.** Seção Domínio de negócio do `dominio.md`: descrição do sistema, fluxo de alocação em etapas (waterfall) e faixas de WOI.
- **Figura 9.** Seção Modelo de dados do `dominio.md`: colunas de semântica especial e inconsistências registradas explicitamente (por exemplo, `year_month` corrompido na origem e `req_qty` com semântica diferente do nome).

Legendas anteriores (não usar mais): Figura 8, "Seção do arquivo Markdown sobre o gerenciamento das ordens e itens de compra vinculados ao CAPEX."; Figura 9, "Seção do arquivo Markdown sobre Assets e as inconsistências identificadas em relação ao conteúdo de Gestão de Compras."

## Manifesto e integridade

O `manifesto_figuras.json` foi atualizado: cada figura tem `formato`, `bytes` e `sha256`, e as Figuras 8 e 9 têm `legenda_original` além da legenda nova. Todos os arquivos são PNG reais, nas dimensões do manifesto. Se as imagens chegarem menores ou em JPEG, foram recomprimidas no upload: use o `.zip` em vez de anexar imagens avulsas e compare o `sha256`.
