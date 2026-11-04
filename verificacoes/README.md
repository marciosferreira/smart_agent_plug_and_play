# verificacoes/

Verificações da base de dados de um domínio, para repetir sempre que a base mudar (por exemplo,
quando os dados reais substituírem os mockados). Nada aqui é importado ou chamado pelo app
(`main.py`, `agent_multi.py`, `scheduler/`). Ao contrário de `scripts_manuais/`, esta pasta é
versionada: as verificações fazem parte do que foi entregue.

Os scripts são só leitura e usam o cliente GraphQL do próprio app (`graphql_config.py`), com o
endpoint de `config/settings.yml` e a chave do `.env`. Rodam a partir da raiz do projeto.

| Arquivo | O que faz |
| --- | --- |
| `verificar_new_age.py` | Integridade e cálculos (OK/FALHA, sai com código 1 se algum teste falhar), medição das armadilhas de interpretação, teste de hipóteses concorrentes, calendário fiscal e comparação de métodos de cálculo da taxa horária — base New Age (DL Cost) |
| `demonstrar_graphql_new_age.py` | Demonstra a junção em uma chamada (`expand`), a criptografia dos campos de custo lidos por junção e a recusa de pedidos acima de 1.000 linhas |
