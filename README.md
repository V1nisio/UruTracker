# UruTracker — Radar de Emenda Pix Parada

Dashboard de prospecção para construtoras e consultores de engenharia que ajuda a identificar municípios com recursos de Emenda Pix aprovados, mas com obras ainda não concluídas.

## Status

Projeto em desenvolvimento. A etapa de mapeamento das fontes de dados foi concluída. As fases de extração, tratamento e visualização ainda serão implementadas.

## O que é

O UruTracker reúne dados públicos do Transferegov para destacar prefeituras que receberam recursos de Emenda Pix para investimentos, mas que ainda possuem obras em andamento, paralisadas ou próximas do vencimento do prazo de execução.

A proposta é facilitar a identificação de oportunidades para construtoras de pequeno e médio porte e para consultores de engenharia que atuam junto a municípios.

A versão final será disponibilizada como um dashboard web atualizado semanalmente, com filtros por estado, área de atuação e prazo de execução, além da possibilidade de exportação dos dados em CSV.

## Fonte de dados

API pública PostgREST do Transferegov referente às Transferências Especiais (Emenda Pix), acessível sem autenticação:

```text
https://api.transferegov.dth.api.gov.br/transferenciasespeciais/
```
**Referencia de API e biblioteca de dados seja implementado no notebook**

### Views mapeadas

| View | Conteúdo |
|---|---|
| `plano_acao_especial` | Município, UF, parlamentar responsável, valores de custeio e investimento e situação administrativa |
| `plano_trabalho_especial` | Situação da execução, indicadores de paralisação e datas de início e término |
| `executor_especial` | Descrição textual do objeto da obra |
| `finalidade_especial` | Classificação setorial padronizada (`area_politica_publica_pt`) |

As informações são integradas por meio do campo `id_plano_acao` utilizando pandas, já que a API não oferece suporte a operações JOIN.

### Filtro de período

O filtro `ano_plano_acao >= 2024` é aplicado apenas na view `plano_acao_especial`.

As demais views não possuem um campo próprio de ano e, por isso, são carregadas integralmente.

### Critério de "emenda parada"

Um município é considerado relevante para monitoramento quando atende a pelo menos um dos critérios abaixo:

- `situacao_plano_trabalho = APROVADO` (recurso aprovado, mas sem indicação de conclusão da execução)
- `ind_justificativa_prorrogacao_paralizacao_pt = True` (obra oficialmente paralisada — prioridade máxima)
- `data_fim_execucao_plano_trabalho < hoje + 90 dias` (prazo de execução próximo do vencimento)

> **Importante:** o campo `situacao_plano_acao` representa apenas o fluxo administrativo da transferência (ex.: CIENTE ou IMPEDIDO) e não deve ser utilizado para avaliar o andamento físico da obra.

## Estrutura atual do projeto

```text
UruTracker/
└── data_extraction/
    └── extrair_dados.py    ← constantes e mapeamento dos endpoints (Parte 1 concluída)
```

## Próximos passos

- [ ] Implementar a extração dos dados via API com paginação e mecanismo de retry (Parte 2 — Theo)
- [ ] Implementar persistência em CSV e orquestração dos downloads (Parte 3 — Belarmino)