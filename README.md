# UruTracker - Radar de Emenda Pix Parada


🔗 **Acesse:** https://uru-tracker.vercel.app/

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

### Criterio de "emenda parada" (versao de producao)

O dashboard web (`api/index.py`, funcao `build_dataframe`) usa o criterio final, com 3 condicoes (A | B | C). Essa e a versao evoluida do criterio - o notebook documenta a versao inicial, mais simples, do prototipo (ver secao "Notebook de Analise" abaixo).

- **A** - `situacao_plano_trabalho == APROVADO`: recurso aprovado, execucao nao concluida.
- **B** - `paralisada`: obra com indicativo de paralisacao/prorrogacao, derivado da coluna `ind_justificativa_prorrogacao_paralizacao_pt` (convertida para booleano).
- **C** - `data_fim_execucao_plano_trabalho < hoje + 90 dias` E situacao fora de `{CONCLUIDO, CONCLUIDO_NT_TCU, CONCLUIDO_PRESTACAO_CONTAS}`.

Emendas sem `data_fim_execucao_plano_trabalho` sao excluidas da base - toda a classificacao de urgencia depende dessa data.

> `situacao_plano_acao` representa apenas o fluxo administrativo da transferencia e nao deve ser usado para avaliar o andamento fisico da obra.

#### Faixas de urgencia

A partir de `dias_para_prazo` (`data_fim_execucao_plano_trabalho - hoje`), o dashboard classifica cada emenda em 6 faixas, nao mais 2 como na versao inicial do prototipo:

| Urgencia (interno) | Criterio (dias_para_prazo) | Rotulo no dashboard |
|---|---|---|
| `ANDAMENTO` | maior que 180 dias | Projeto em andamento |
| `POSSIVEL` | 91 a 180 dias | Possivel oportunidade |
| `CRITICO` | 0 a 90 dias | Prazo critico |
| `OPORTUNIDADE` | -90 a -1 dias (venceu ha ate 90 dias) | Oportunidade |
| `ESTAGNADO` | -180 a -91 dias (venceu ha 91 a 180 dias) | Recem estagnada |
| `ABANDONADO` | menor que -180 dias (venceu ha mais de 180 dias) | Dormente |

A faixa `ABANDONADO` mantem esse nome internamente no codigo, mas aparece como "Dormente" na interface. O grupo comercial "Oportunidade" (constante `GRUPO_OPORTUNIDADE` no codigo) soma as faixas OPORTUNIDADE + ESTAGNADO.

## Extracao de dados

A funcao `fetch_view` em `extrair_dados.py` realiza o download completo de qualquer view com paginacao automatica via `Content-Range`, retry automatico e rate limiting passivo (100ms entre lotes).

## Persistencia em Parquet

A funcao `salvar_parquet` (antes `salvar_csv`) grava cada DataFrame em `data/<nome>.parquet`, usando o engine `pyarrow` com compressao `gzip`.

```bash
python data_extraction/extrair_dados.py
```

Os arquivos `data/*.parquet` (plano_acao, plano_trabalho, executor, finalidade) estao versionados no repositorio - antes eram `*.csv` e ficavam fora do git, listados no `.gitignore`. Isso significa que o dashboard funciona direto apos o clone ou o deploy, sem precisar rodar a extracao primeiro. Para atualizar os dados com informacoes mais recentes da API, basta rodar o comando acima novamente.

## Dashboard web (producao)

A partir da extracao e do prototipo do notebook, o projeto evoluiu para um dashboard web de producao: `api/index.py`, um app Flask single-file (cerca de 13 mil linhas) que concentra logica de dados, API REST, CSS, JS e HTML embutidos - sem banco de dados e sem build step. Tudo e recalculado em memoria com pandas a partir dos arquivos parquet.

Estrutura interna do arquivo, em ordem:

1. Config e logica de dados em Python (`build_dataframe`, `aplicar_filtros`, `kpis`, `agg_uf`, `agg_setor`, `agg_municipio`, `agg_urgencia`, `agg_prospeccao_uf`, `agg_prospeccao_mun`, `cards`, `meta`).
2. CSS embutido como string Python: design system proprio, dark mode institucional azul-escuro + ciano, tipografia mono para dados, regra explicita de zero cantos arredondados.
3. JS embutido como string Python: toda a interatividade do front-end.
4. HTML embutido como string Python.
5. Definicao das rotas Flask e `main()`.
6. Duas tabelas estaticas grandes: `_MUNI_IBGE` (cerca de 5500 municipios, mapeia "NOME|UF" para codigo IBGE de 7 digitos + nome de exibicao) e `_MUNI_CENTROIDE` (cerca de 4400 entradas, codigo IBGE para `[lon, lat]`). Usadas para casar os beneficiarios com o GeoJSON municipal e posicionar pins no mapa sem geocoding externo em runtime.

### API REST

| Endpoint | Descricao |
|---|---|
| `GET /` | Serve o dashboard (HTML/CSS/JS embutidos) |
| `GET /api/meta` | Metadados de boot: UFs, setores, faixas de urgencia disponiveis, totais |
| `GET /api/kpis` | Os 9 KPIs agregados, respeitando os filtros ativos |
| `GET /api/leads` | Tabela de leads paginada (`page`/`page_size`) e ordenavel (urgencia, prazo, valor, municipio, UF), com ordenacao especial "oportunidades primeiro" |
| `GET /api/agg/uf` | Dados agregados por UF, para grafico |
| `GET /api/agg/setor` | Top 15 setores, para grafico |
| `GET /api/agg/municipio` | Dados agregados por municipio |
| `GET /api/agg/urgencia` | Distribuicao por faixa de urgencia, para grafico |
| `GET /api/prospeccao/uf` | Componentes do "calor" (oportunidade/estagnado/dormente/critico) por UF, para colorir o mapa |
| `GET /api/prospeccao/mun` | Mesmos componentes por municipio + centroide, para posicionar pins |
| `GET /api/cards` | Ao clicar num municipio no mapa, ate 60 casos acionaveis daquele municipio, ordenados por faixa e prazo |
| `POST /api/reload` | Reprocessa os parquet sem reiniciar o servidor |
| `GET /vendor/<nome>` | Proxy com cache em memoria para assets de terceiros via CDN: chart.js UMD e o GeoJSON dos estados do Brasil (`brazil.geojson`) |
| `GET /vendor/mun/<uf>` | Proxy com cache em memoria para o GeoJSON municipal de uma UF (malha tbrugz/geodata-br), usado no drill-down do mapa |

Os endpoints `/vendor/*` evitam depender de CDN externo diretamente no navegador do usuario e evitam a necessidade de bundler ou build step no projeto.

### Front-end (dashboard servido em `/`)

Vanilla HTML/CSS/JS embutido no mesmo arquivo Python, sem framework e sem build step:

- Grid com 8 KPIs: emendas paradas (total), valor total parado (R$), valor de oportunidade parado (R$), oportunidades + recem estagnadas (split em 2 numeros), municipios com oportunidade, projetos dormentes, em prazo critico, possiveis + em andamento.
- Filtros: UF, setor, faixa de urgencia (dropdown com os 6 rotulos), busca textual livre (municipio, objeto da obra, parlamentar).
- Mapa do Brasil em SVG, interativo, com 3 modos de visualizacao (PROSPECCAO/calor de oportunidade, QTD, VALOR), drill-down de UF para malha municipal, toggle de pins (oportunidade e prazo critico), tooltip e legenda.
- 3 graficos via Chart.js: emendas paradas por UF, distribuicao por faixa de urgencia, top 15 setores com emendas paradas.
- Painel de "casos criticos": cards deslizantes que aparecem ao clicar num municipio no mapa.
- Tabela de leads paginada, ordenavel, com opcao "mostrar oportunidades primeiro".
- Auto-refresh (toggle) e relogio/status "LIVE" no cabecalho.
- Tela de erro dedicada quando os parquet nao sao encontrados, orientando rodar `python data_extraction/extrair_dados.py`.

## Deploy na Vercel

O projeto roda como Vercel Serverless Function (Python), apontando para `api/index.py`.

- `vercel.json` usa o formato `functions`, com `maxDuration` de 30 segundos. O formato antigo (`builds`/`routes`) foi substituido por esse.
- `requirements.txt` (`Flask>=3.0`, `pandas>=2.0`, `numpy>=1.24`, `pyarrow>=14.0.1`) e instalado automaticamente pela Vercel no build.
- Localmente, o proprio `api/index.py` se autoinstala as dependencias que faltarem via pip (funcao `_garantir_dependencias`), mas pula essa autoinstalacao quando a variavel de ambiente `VERCEL` esta definida - um ambiente serverless nao deve tentar instalar pacotes em runtime.
- Fix de carregamento de dados: `build_dataframe()` passou a rodar direto no import do modulo (nivel de modulo, fora de `main()`). Antes, os dados so eram carregados dentro de `main()`, que nunca e chamada no cold start de uma funcao serverless (a Vercel apenas importa o modulo) - isso fazia o app subir sem dados em producao. Com a mudanca, tanto o `python api/index.py` local quanto o cold start na Vercel garantem os dados ja carregados.

### URL de producao

**https://uru-tracker.vercel.app/**


## Como rodar localmente

```bash
pip install -r requirements.txt
python api/index.py
```

Abre automaticamente `http://127.0.0.1:5000` no navegador. A variavel de ambiente `URU_NO_BROWSER` desativa a abertura automatica.

Os arquivos parquet ja vem versionados no repositorio, entao o dashboard funciona direto apos o clone. Para atualizar os dados, rodar `python data_extraction/extrair_dados.py` antes.

## Notebook de Analise (prototipo exploratorio)

O notebook `notebooks/urutracker_pandas.ipynb` foi o prototipo exploratorio em Jupyter/pandas que guiou a integracao, classificacao e visualizacao de dados. A logica desenvolvida aqui foi depois incorporada e expandida no dashboard de producao (`api/index.py`) - inclusive o criterio de "emenda parada" evoluiu de 2 condicoes (A | C) para 3 (A | B | C) e as faixas de urgencia, de 2 para 6. Ver a secao "Criterio de emenda parada (versao de producao)" acima.

> Nota sobre numeracao: o script `data_extraction/extrair_dados.py` tambem tem comentarios internos "Parte 1/2/3" (extracao / fetch com retry / persistencia), atribuidos a Vinicius/Theo/Belarmino. Essa numeracao e PARALELA e INDEPENDENTE das partes do notebook descritas abaixo (que tambem sao atribuidas a Vinicius/Theo/Belarmino, mas tratam de carga/classificacao/visualizacao). Sao duas divisoes de trabalho diferentes, em arquivos diferentes - nao confundir uma com a outra.

### Parte 1 - Carga e Integracao (Vinicius)

Carrega os 4 CSVs, converte datas, calcula `valor_total`, agrega setores por executor e integra as 4 tabelas via `id_plano_acao`.

### Parte 2 - Classificacao e KPIs (Theo)

Aplica o criterio inicial de "parado" (cond_A | cond_C), classifica urgencia em 2 faixas e calcula KPIs. Esta e a versao do PROTOTIPO - a tabela abaixo e mantida como registro historico.

| Condicao | Logica |
|----------|--------|
| A | `situacao_plano_trabalho == APROVADO` |
| C | `data_fim < HOJE + 90 dias` AND status != CONCLUIDO |

| Urgencia | Criterio |
|----------|----------|
| `PRAZO_CRITICO` | prazo vencendo em menos de 90 dias |
| `APROVADO_PENDENTE` | demais casos |

### Parte 3 - Analises e Visualizacoes (Belarmino)

Agrega por UF e setor, gera graficos com matplotlib e consolida a tabela de leads ranqueada por urgencia.

| Visualizacao | Tipo |
|---|---|
| Emendas paradas por UF | Barras |
| Top 15 setores | Barras horizontais |
| Distribuicao por urgencia | Barras + pizza |
| Tabela de leads | DataFrame por `urgencia` e `dias_para_prazo` |

## Proximos passos

- [x] Extracao de dados via API publica do Transferegov
- [x] Persistencia dos dados em Parquet (`data/*.parquet`, versionados no repositorio)
- [x] Notebook - Parte 1: carga e integracao
- [x] Notebook - Parte 2: classificacao e KPIs
- [x] Notebook - Parte 3: analises e visualizacoes
- [x] Dashboard web interativo em producao (Flask single-file + API REST + front-end vanilla)
- [x] Deploy em producao na Vercel (Serverless Function Python)
