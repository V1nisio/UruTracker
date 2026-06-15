# UruTracker - Radar de Emenda Pix Parada

Dashboard de prospeccao para construtoras e consultores de engenharia que ajuda a identificar municipios com recursos de Emenda Pix aprovados, mas com obras ainda nao concluidas.

## O que e

O UruTracker reune dados publicos do Transferegov para destacar prefeituras que receberam recursos de Emenda Pix para investimentos, mas que ainda possuem obras em andamento ou proximas do vencimento do prazo de execucao.

A proposta e facilitar a identificacao de oportunidades para construtoras de pequeno e medio porte e para consultores de engenharia que atuam junto a municipios.

## Fonte de dados

API publica PostgREST do Transferegov referente as Transferencias Especiais (Emenda Pix), acessivel sem autenticacao:

```
https://api.transferegov.dth.api.gov.br/transferenciasespeciais/
```

### Views mapeadas

| View | Conteudo |
|---|---|
| `plano_acao_especial` | Municipio, UF, parlamentar, valores e situacao administrativa |
| `plano_trabalho_especial` | Situacao da execucao, datas de inicio e termino |
| `executor_especial` | Descricao textual do objeto da obra |
| `finalidade_especial` | Classificacao setorial padronizada |

As informacoes sao integradas via `id_plano_acao` no pandas - a API nao suporta JOIN nativo.

### Filtro de periodo

`ano_plano_acao >= 2024` aplicado apenas em `plano_acao_especial`. As demais views sao carregadas integralmente.

### Criterio de "emenda parada"

- `situacao_plano_trabalho = APROVADO` - recurso aprovado, execucao nao concluida
- `data_fim_execucao_plano_trabalho < hoje + 90 dias` - prazo vencendo

> `situacao_plano_acao` representa apenas o fluxo administrativo da transferencia e nao deve ser usado para avaliar o andamento fisico da obra.

## Extracao de dados

A funcao `fetch_view` em `extrair_dados.py` realiza o download completo de qualquer view com paginacao automatica via `Content-Range`, retry automatico e rate limiting passivo (100ms entre lotes).

## Persistencia em CSV

A funcao `salvar_csv` grava cada DataFrame em `data_extraction/<nome>.csv` com encoding utf-8-sig.

```bash
python data_extraction/extrair_dados.py
```
## Notebook de Analise

### Parte 1 - Carga e Integracao (Vinicius)

Carrega os 4 CSVs, converte datas, calcula `valor_total`, agrega setores por executor e integra as 4 tabelas via `id_plano_acao`.

### Parte 2 - Classificacao e KPIs (Theo)

Aplica o criterio de "parado" (cond_A | cond_C), classifica urgencia e calcula KPIs.

 Condicao | Logica 
| A | `situacao_plano_trabalho == APROVADO` |
| C | `data_fim < HOJE + 90 dias` AND status != CONCLUIDO |

 Urgencia | Criterio 
 `PRAZO_CRITICO` | prazo vencendo em menos de 90 dias 
 `APROVADO_PENDENTE` | demais casos 

## Proximos passos

- [x] Extracao e persistencia de dados
- [x] Notebook - Parte 1: carga e integracao
- [x] Notebook - Parte 2: classificacao e KPIs
- [ ] Notebook - Parte 3: analises e visualizacoes