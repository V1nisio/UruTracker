"""
urutracker.py — UruTracker (Radar de Emenda Pix Parada), aplicativo unico.

TUDO vive neste unico arquivo: servidor Flask + HTML + CSS + JS. Ao rodar:

    python ui_extension/urutracker.py

ele sobe um servidor local, abre o navegador padrao e exibe o dashboard.

Os dados NAO sao embutidos: vem dos CSVs gerados por
    python data_extraction/extrair_dados.py
Sem esses CSVs o app sobe e mostra a tela de "dados ausentes" (nao funciona
sem os arquivos de data_extraction/).

Os dois assets de terceiros (Chart.js e o GeoJSON do Brasil) sao buscados em
tempo de execucao via CDN e servidos pela rota /vendor/<...> — por isso nenhum
arquivo binario precisa existir ao lado deste .py. Requer internet na 1a carga.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap de dependencias (instala automaticamente se faltarem)
# ---------------------------------------------------------------------------

REQUISITOS = {
    "flask": "Flask>=3.0",
    "pandas": "pandas>=2.0",
    "numpy": "numpy>=1.24",
}


def _garantir_dependencias() -> None:
    faltando = []
    for modulo, pacote in REQUISITOS.items():
        try:
            importlib.import_module(modulo)
        except ImportError:
            faltando.append(pacote)
    if not faltando:
        return
    print(f"[setup] Instalando dependencias: {', '.join(faltando)} ...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *faltando]
    )
    importlib.invalidate_caches()


_garantir_dependencias()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from flask import Flask, jsonify, request  # noqa: E402

# ===========================================================================
# DATA LOADER  (carga e processamento dos CSVs de data_extraction/)
# ===========================================================================

# ui_extension/  ->  raiz do projeto  ->  data_extraction/
DATA_DIR = Path(__file__).resolve().parent.parent / "data_extraction"

# Logomarca hospedada (PNG transparente). Fica online para nao precisar
# versionar nenhum arquivo de imagem junto do .py.
LOGO_URL = "https://logusautomacao.com/wp-content/uploads/2026/06/logopng.png"

ARQUIVOS = {
    "plano_acao": "plano_acao_especial.csv",
    "plano_trabalho": "plano_trabalho_especial.csv",
    "executor": "executor_especial.csv",
    "finalidade": "finalidade_especial.csv",
}

# Situacoes que indicam obra concluida (nao entram no radar)
CONCLUIDOS = {"CONCLUIDO", "CONCLUIDO_NT_TCU", "CONCLUIDO_PRESTACAO_CONTAS"}

# Janela (em dias) para considerar o prazo "critico"
JANELA_PRAZO_DIAS = 90

# Sigla da UF -> codigo IBGE de 2 digitos (nomes dos GeoJSON municipais tbrugz)
UF_COD = {
    "RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17",
    "MA": "21", "PI": "22", "CE": "23", "RN": "24", "PB": "25", "PE": "26",
    "AL": "27", "SE": "28", "BA": "29",
    "MG": "31", "ES": "32", "RJ": "33", "SP": "35",
    "PR": "41", "SC": "42", "RS": "43",
    "MS": "50", "MT": "51", "GO": "52", "DF": "53",
}

# _MUNI_IBGE (mapeamento "NOME|UF" -> (codigo_ibge, nome_exibicao)) fica
# definido no rodape deste arquivo, logo antes do bloco __main__.

# Faixas de classificacao por prazo (data_fim relativa a hoje), na ordem da
# tabela. O rank define a ordenacao padrao da tabela de leads.
URGENCIA_ORDEM = ["ANDAMENTO", "POSSIVEL", "CRITICO", "OPORTUNIDADE",
                  "ESTAGNADO", "ABANDONADO"]
URGENCIA_RANK = {k: i for i, k in enumerate(URGENCIA_ORDEM)}

# "Mostrar oportunidades primeiro": ordem de prospeccao (padrao da tabela).
OPORT_RANK = {k: i for i, k in enumerate(
    ["OPORTUNIDADE", "ESTAGNADO", "CRITICO", "POSSIVEL", "ANDAMENTO", "ABANDONADO"]
)}

# Faixas "verdes" (acionaveis): oportunidade + recem estagnada.
GRUPO_OPORTUNIDADE = ["OPORTUNIDADE", "ESTAGNADO"]

# Faixas exibidas nos cards do municipio, na ordem pedida.
CARDS_ORDEM = ["OPORTUNIDADE", "ESTAGNADO", "CRITICO", "ABANDONADO"]
CARDS_RANK = {k: i for i, k in enumerate(CARDS_ORDEM)}

# Familia tematica do tipo de obra (area_politica_publica_pt) -> icone no card.
# Os 79 tipos da base sao agrupados em 16 familias por palavra-chave (ordem
# importa: regra mais especifica primeiro). O frontend mapeia familia -> SVG.
FAMILIA_RULES = [
    ("saude", ["saude", "hospitalar", "ambulatorial", "atencao basica", "vigilancia sanitaria",
               "vigilancia epidemiolog", "epidemiolog", "profilatico", "terapeutico",
               "alimentacao e nutricao"]),
    ("educacao", ["ensino", "educa", "formacao de recursos"]),
    ("esporte", ["desporto", "lazer"]),
    ("cultura", ["cultural", "patrimonio"]),
    ("ciencia", ["cientific", "tecnolog", "engenharia", "meteorologia", "conhecimento cient"]),
    ("saneamento", ["saneamento", "hidric", "irriga"]),
    ("ambiente", ["ambiental", "preservacao e conservacao", "areas degradadas"]),
    ("energia", ["energia", "combustive", "mineral"]),
    ("transporte", ["transporte", "rodoviario", "ferroviario", "hidroviario", "aereo",
                    "coletivos urbanos"]),
    ("agro", ["agropecuaria", "agraria", "extensao rural", "colonizacao", "abastecimento"]),
    ("seguranca", ["policiamento", "defesa civil", "inteligencia", "custodia", "reintegracao"]),
    ("direitos", ["direitos"]),
    ("social", ["assistencia", "socioassist", "crianca e ao adolescente", "idoso", "deficiencia",
                "povos indigenas", "seguranca de renda"]),
    ("turismo", ["turismo"]),
    ("infra_urbana", ["infraestrutura urbana", "servicos urbanos", "habita", "ordenamento territorial"]),
    ("trabalho", ["trabalho", "empregabilidade", "trabalhador", "financeiro", "comercial",
                  "industrial", "comercio", "propriedade", "normalizacao", "mineracao", "producao",
                  "relacoes de trabalho", "exterior"]),
]


def _norm_txt(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def familia_obra(setor_nome: str) -> str:
    """Classifica um tipo de obra numa das 16 familias tematicas (ou 'generic')."""
    n = _norm_txt(setor_nome)
    for fam, kws in FAMILIA_RULES:
        if any(k in n for k in kws):
            return fam
    return "generic"

# Colunas expostas na tabela de leads (na ordem de exibicao)
COLUNAS_LEAD = [
    "id_plano_acao",
    "urgencia",
    "nome_beneficiario_plano_acao",
    "uf_beneficiario_plano_acao",
    "setor",
    "objeto_executor",
    "valor_total",
    "nome_parlamentar_emenda_plano_acao",
    "dias_para_prazo",
]

# Whitelist de ordenacao aceita pela API (protege contra valores arbitrarios)
SORT_WHITELIST = {
    "urgencia": ("rank", True),
    "prazo": ("dias_para_prazo", True),
    "valor": ("valor_total", False),
    "municipio": ("nome_beneficiario_plano_acao", True),
    "uf": ("uf_beneficiario_plano_acao", True),
}


class DataStore:
    """Mantem o DataFrame processado e os metadados da carga."""

    def __init__(self) -> None:
        self.df: pd.DataFrame | None = None
        self.data_ok: bool = False
        self.erro: str | None = None
        self.gerado_em: str | None = None
        self.hoje: pd.Timestamp | None = None
        self.faltando: list[str] = []
        self.lista_setores: list[str] = []

    @property
    def ufs(self) -> list[str]:
        if self.df is None:
            return []
        return sorted(self.df["uf_beneficiario_plano_acao"].dropna().unique().tolist())

    @property
    def setores(self) -> list[str]:
        return self.lista_setores

    @property
    def urgencias(self) -> list[str]:
        return list(URGENCIA_ORDEM)


STORE = DataStore()


def _to_bool(serie: pd.Series) -> pd.Series:
    """Normaliza colunas booleanas que podem vir como bool/str/NaN."""
    if serie.dtype == bool:
        return serie.fillna(False)
    return (
        serie.astype("string")
        .str.strip()
        .str.lower()
        .isin(["true", "t", "1", "sim", "verdadeiro"])
        .fillna(False)
    )


def build_dataframe() -> None:
    """Le os CSVs, integra as tabelas e classifica as emendas paradas.

    Atualiza o ``STORE`` global. Nunca levanta excecao para o servidor:
    em caso de erro, marca ``data_ok=False`` e guarda a mensagem.
    """
    STORE.df = None
    STORE.data_ok = False
    STORE.erro = None
    STORE.faltando = []

    faltando = [
        nome for chave, nome in ARQUIVOS.items()
        if not (DATA_DIR / nome).exists()
    ]
    if faltando:
        STORE.faltando = faltando
        STORE.erro = (
            "CSVs nao encontrados em data_extraction/. "
            "Rode primeiro: python data_extraction/extrair_dados.py"
        )
        return

    try:
        plano_acao = pd.read_csv(DATA_DIR / ARQUIVOS["plano_acao"], encoding="utf-8-sig")
        plano_trabalho = pd.read_csv(DATA_DIR / ARQUIVOS["plano_trabalho"], encoding="utf-8-sig")
        executor = pd.read_csv(DATA_DIR / ARQUIVOS["executor"], encoding="utf-8-sig")
        finalidade = pd.read_csv(DATA_DIR / ARQUIVOS["finalidade"], encoding="utf-8-sig")

        hoje = pd.Timestamp(date.today())

        # -- datas e valores ------------------------------------------------
        plano_trabalho["data_fim_execucao_plano_trabalho"] = pd.to_datetime(
            plano_trabalho["data_fim_execucao_plano_trabalho"], errors="coerce"
        )
        plano_trabalho["paralisada"] = _to_bool(
            plano_trabalho["ind_justificativa_prorrogacao_paralizacao_pt"]
        )

        plano_acao["valor_total"] = (
            plano_acao["valor_custeio_plano_acao"].fillna(0)
            + plano_acao["valor_investimento_plano_acao"].fillna(0)
        )

        # -- setor (agrega area_politica_publica por executor) --------------
        finalidade_lst = (
            finalidade.groupby("id_executor")["area_politica_publica_pt"]
            .apply(lambda x: sorted(x.dropna().unique()))
            .reset_index()
            .rename(columns={"area_politica_publica_pt": "setores_list"})
        )
        finalidade_lst["setor"] = finalidade_lst["setores_list"].apply(lambda L: " | ".join(L))
        finalidade_lst["setor_key"] = finalidade_lst["setores_list"].apply(
            lambda L: "|" + "|".join(s.lower() for s in L) + "|" if L else ""
        )
        finalidade_agg = finalidade_lst[["id_executor", "setores_list", "setor", "setor_key"]]
        executor_primeiro = executor.drop_duplicates("id_plano_acao", keep="first")

        # -- integracao das 4 tabelas via id_plano_acao / id_executor -------
        df = plano_acao.merge(plano_trabalho, on="id_plano_acao", how="left")
        df = df.merge(
            executor_primeiro[["id_plano_acao", "id_executor", "objeto_executor"]],
            on="id_plano_acao", how="left",
        )
        df = df.merge(finalidade_agg, on="id_executor", how="left")

        # -- criterio de "parado" (A | B | C) -------------------------------
        prazo_limite = hoje + pd.Timedelta(days=JANELA_PRAZO_DIAS)
        cond_a = df["situacao_plano_trabalho"] == "APROVADO"
        cond_b = df["paralisada"].fillna(False)
        cond_c = (
            (df["data_fim_execucao_plano_trabalho"] < prazo_limite)
            & (~df["situacao_plano_trabalho"].isin(CONCLUIDOS))
        )
        df["parado"] = cond_a | cond_b | cond_c
        df = df[df["parado"]].copy()

        # emendas sem data_fim sao excluidas: toda a classificacao depende dela.
        df = df[df["data_fim_execucao_plano_trabalho"].notna()].copy()

        # -- classificacao por faixa de prazo (6 faixas, sem sobreposicao) ----
        df["dias_para_prazo"] = (
            df["data_fim_execucao_plano_trabalho"] - hoje
        ).dt.days
        d = df["dias_para_prazo"]
        df["urgencia"] = np.select(
            [d > 180, d > 90, d >= 0, d >= -90, d >= -180],
            ["ANDAMENTO", "POSSIVEL", "CRITICO", "OPORTUNIDADE", "ESTAGNADO"],
            default="ABANDONADO",
        )
        df["rank"] = df["urgencia"].map(URGENCIA_RANK).astype(int)

        # ranqueia: faixa (ordem da tabela), depois prazo mais curto primeiro
        df = df.sort_values(["rank", "dias_para_prazo"], na_position="last").reset_index(drop=True)

        # -- codigo IBGE + nome de exibicao do municipio --------------------
        # chave "NOME DO BENEFICIARIO|UF" -> (cod_ibge, nome_exibicao).
        chave = (
            df["nome_beneficiario_plano_acao"].fillna("").astype(str).str.upper().str.strip()
            + "|"
            + df["uf_beneficiario_plano_acao"].fillna("").astype(str).str.upper().str.strip()
        )
        mapeado = chave.map(_MUNI_IBGE)
        df["cod_ibge"] = mapeado.apply(
            lambda t: t[0] if isinstance(t, tuple) else ""
        )
        df["municipio_exib"] = mapeado.apply(
            lambda t: t[1] if isinstance(t, tuple) else None
        )
        df["municipio_exib"] = df["municipio_exib"].fillna(
            df["nome_beneficiario_plano_acao"]
        )

        # normaliza colunas de setor para linhas sem finalidade
        df["setor"] = df["setor"].fillna("")
        df["setor_key"] = df["setor_key"].fillna("")
        df["setores_list"] = df["setores_list"].apply(
            lambda v: v if isinstance(v, list) else []
        )

        # coluna auxiliar para busca textual rapida
        df["busca"] = (
            df["nome_beneficiario_plano_acao"].fillna("").astype(str) + " "
            + df["uf_beneficiario_plano_acao"].fillna("").astype(str) + " "
            + df["setor"].astype(str) + " "
            + df["objeto_executor"].fillna("").astype(str) + " "
            + df["nome_parlamentar_emenda_plano_acao"].fillna("").astype(str)
        ).str.lower()

        STORE.df = df
        STORE.hoje = hoje
        STORE.gerado_em = hoje.strftime("%d/%m/%Y")
        STORE.lista_setores = sorted(
            finalidade["area_politica_publica_pt"].dropna().unique().tolist()
        )
        STORE.data_ok = True
    except Exception as exc:  # pragma: no cover - defensivo
        STORE.df = None
        STORE.data_ok = False
        STORE.erro = f"Falha ao processar os CSVs: {exc}"


def aplicar_filtros(args) -> pd.DataFrame:
    """Aplica os filtros de UF, setor, urgencia e busca textual."""
    df = STORE.df
    if df is None:
        return pd.DataFrame()

    mask = pd.Series(True, index=df.index)

    uf = (args.get("uf") or "").strip().upper()
    if uf:
        mask &= df["uf_beneficiario_plano_acao"].str.upper() == uf

    setor = (args.get("setor") or "").strip()
    if setor:
        alvo = "|" + setor.lower() + "|"
        mask &= df["setor_key"].str.contains(alvo, regex=False, na=False)

    urgencia = (args.get("urgencia") or "").strip().upper()
    if urgencia:
        mask &= df["urgencia"] == urgencia

    mun = (args.get("mun") or "").strip()
    if mun:
        mask &= df["cod_ibge"].astype(str) == mun

    q = (args.get("q") or "").strip().lower()
    if q:
        mask &= df["busca"].str.contains(q, regex=False, na=False)

    return df[mask]


def _nan_to_none(value):
    if value is None:
        return None
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    return value


def leads_pagina(args) -> dict:
    """Retorna uma pagina de leads ja filtrada e ordenada (server-side).

    Leads sem prazo (sem data de fim de execucao) nao entram na tabela.
    """
    df = aplicar_filtros(args)
    df = df[df["dias_para_prazo"].notna()]

    sort_key = (args.get("sort") or "").strip().lower()
    if sort_key == "oport":
        # "Mostrar oportunidades primeiro": ordem custom de faixas + prazo
        df = df.assign(_o=df["urgencia"].map(OPORT_RANK)).sort_values(
            ["_o", "dias_para_prazo"], na_position="last"
        )
    elif sort_key in SORT_WHITELIST:
        col, asc = SORT_WHITELIST[sort_key]
        df = df.sort_values(col, ascending=asc, na_position="last")

    total = len(df)
    try:
        page = max(1, int(args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(args.get("page_size", 50))
    except (TypeError, ValueError):
        page_size = 50
    page_size = max(10, min(page_size, 200))

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size

    bloco = df.iloc[start:start + page_size][COLUNAS_LEAD]

    rows = []
    for rec in bloco.to_dict(orient="records"):
        rows.append({k: _nan_to_none(v) for k, v in rec.items()})

    return {
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


KPI_KEYS = [
    "emendas_paradas", "valor_total", "valor_oportunidade",
    "qtd_oportunidade", "qtd_estagnado", "municipios_oport",
    "abandonado", "prazo_critico", "possivel_andamento",
]


def kpis(args) -> dict:
    """KPIs agregados respeitando os filtros ativos."""
    df = aplicar_filtros(args)
    if df.empty:
        return {k: 0.0 if k.startswith("valor") else 0 for k in KPI_KEYS}
    g = df["urgencia"]
    is_opp = g.isin(GRUPO_OPORTUNIDADE)
    return {
        "emendas_paradas": int(len(df)),
        "valor_total": float(df["valor_total"].sum()),
        # oportunidade + recem estagnada (vencidas ha ate 180 dias)
        "valor_oportunidade": float(df.loc[is_opp, "valor_total"].sum()),
        "qtd_oportunidade": int((g == "OPORTUNIDADE").sum()),
        "qtd_estagnado": int((g == "ESTAGNADO").sum()),
        "municipios_oport": int(df.loc[is_opp, "nome_beneficiario_plano_acao"].nunique()),
        "abandonado": int((g == "ABANDONADO").sum()),  # "Dormentes" no front
        "prazo_critico": int((g == "CRITICO").sum()),
        "possivel_andamento": int(g.isin(["POSSIVEL", "ANDAMENTO"]).sum()),
    }


def agg_uf(args) -> dict:
    df = aplicar_filtros(args)
    if df.empty:
        return {"labels": [], "qtd": [], "valor": []}
    g = df.groupby("uf_beneficiario_plano_acao").agg(
        qtd=("id_plano_acao", "count"),
        valor=("valor_total", "sum"),
    )
    g = g.sort_values("qtd", ascending=False)
    return {
        "labels": g.index.tolist(),
        "qtd": [int(x) for x in g["qtd"]],
        "valor": [round(float(x) / 1e6, 1) for x in g["valor"]],  # R$ MM
    }


def agg_setor(args, top: int = 15) -> dict:
    df = aplicar_filtros(args)
    if df.empty:
        return {"labels": [], "values": []}
    expl = df[["id_plano_acao", "setores_list"]].explode("setores_list")
    expl = expl.dropna(subset=["setores_list"])
    expl = expl[expl["setores_list"] != ""]
    if expl.empty:
        return {"labels": [], "values": []}
    g = expl.groupby("setores_list")["id_plano_acao"].count().sort_values(ascending=False).head(top)
    return {"labels": g.index.tolist(), "values": [int(x) for x in g]}


def agg_municipio(args) -> dict:
    """Distribuicao por municipio (beneficiario), usada no drill-down do mapa.

    Retorna TODOS os municipios do recorte filtrado (ordenados por qtd), com o
    codigo IBGE para casar com o GeoJSON municipal. O frontend usa o topo da
    lista no grafico de barras e o conjunto todo para colorir o mapa.
    """
    df = aplicar_filtros(args)
    # so municipios de verdade: descarta beneficiarios sem codigo IBGE
    # (governo estadual "ESTADO DO X", consorcios etc.) — coerente com o mapa.
    df = df[df["cod_ibge"].astype(str) != ""]
    if df.empty:
        return {"labels": [], "codes": [], "qtd": [], "valor": []}
    g = (
        df.groupby(["cod_ibge", "municipio_exib"])
        .agg(qtd=("id_plano_acao", "count"), valor=("valor_total", "sum"))
        .reset_index()
        .sort_values("qtd", ascending=False)
    )
    return {
        "labels": g["municipio_exib"].tolist(),
        "codes": [str(c) for c in g["cod_ibge"]],
        "qtd": [int(x) for x in g["qtd"]],
        "valor": [round(float(x) / 1e6, 1) for x in g["valor"]],
    }


def agg_urgencia(args) -> dict:
    df = aplicar_filtros(args)
    counts = df["urgencia"].value_counts() if not df.empty else {}
    return {
        "labels": list(URGENCIA_ORDEM),
        "values": [int(counts.get(u, 0)) for u in URGENCIA_ORDEM],
    }


# Categorias relevantes para a visao "Prospeccao" (componentes do calor).
PROSP_CATS = ["OPORTUNIDADE", "ESTAGNADO", "ABANDONADO", "CRITICO"]


def _prospeccao_df(args) -> pd.DataFrame:
    """DataFrame para a visao Prospeccao: respeita UF/setor/busca, ignora
    urgencia e municipio (o mapa mostra o recorte inteiro, nao 1 municipio)."""
    base = _Args({
        "uf": args.get("uf"),
        "setor": args.get("setor"),
        "q": args.get("q"),
    })
    return aplicar_filtros(base)


def _contagem_por(df: pd.DataFrame, group):
    """Pivot de contagem de emendas por <group> x faixa, com as 4 categorias."""
    piv = df.pivot_table(
        index=group, columns="urgencia", values="id_plano_acao",
        aggfunc="count", fill_value=0,
    )
    for c in PROSP_CATS:
        if c not in piv.columns:
            piv[c] = 0
    return piv


def agg_prospeccao_uf(args) -> dict:
    """Componentes do 'calor' por UF (oportunidade/estagnado/dormente/critico)."""
    df = _prospeccao_df(args)
    df = df[df["urgencia"].isin(PROSP_CATS)]
    vazio = {"labels": [], "oportunidade": [], "estagnado": [],
             "dormente": [], "critico": [], "valor_oport": []}
    if df.empty:
        return vazio
    piv = _contagem_por(df, "uf_beneficiario_plano_acao")
    valop = (
        df[df["urgencia"].isin(GRUPO_OPORTUNIDADE)]
        .groupby("uf_beneficiario_plano_acao")["valor_total"].sum()
    )
    ufs = piv.index.tolist()
    return {
        "labels": ufs,
        "oportunidade": [int(piv.loc[u, "OPORTUNIDADE"]) for u in ufs],
        "estagnado": [int(piv.loc[u, "ESTAGNADO"]) for u in ufs],
        "dormente": [int(piv.loc[u, "ABANDONADO"]) for u in ufs],
        "critico": [int(piv.loc[u, "CRITICO"]) for u in ufs],
        "valor_oport": [round(float(valop.get(u, 0.0)) / 1e6, 1) for u in ufs],
    }


def agg_prospeccao_mun(args) -> dict:
    """Componentes do calor por municipio + centroide (para heat, contorno e pins).

    Sem filtro de UF -> nacional (pins na visao Brasil). Com UF -> so a UF.
    """
    df = _prospeccao_df(args)
    df = df[(df["cod_ibge"].astype(str) != "") & df["urgencia"].isin(PROSP_CATS)]
    vazio = {"codes": [], "nomes": [], "oportunidade": [], "estagnado": [],
             "dormente": [], "critico": [], "valor_oport": [], "lon": [], "lat": []}
    if df.empty:
        return vazio
    piv = _contagem_por(df, ["cod_ibge", "municipio_exib"])
    valop = (
        df[df["urgencia"].isin(GRUPO_OPORTUNIDADE)]
        .groupby(["cod_ibge", "municipio_exib"])["valor_total"].sum()
    )
    out = {k: [] for k in vazio}
    for (cod, nome), row in piv.iterrows():
        cen = _MUNI_CENTROIDE.get(str(cod))
        out["codes"].append(str(cod))
        out["nomes"].append(nome)
        out["oportunidade"].append(int(row["OPORTUNIDADE"]))
        out["estagnado"].append(int(row["ESTAGNADO"]))
        out["dormente"].append(int(row["ABANDONADO"]))
        out["critico"].append(int(row["CRITICO"]))
        out["valor_oport"].append(round(float(valop.get((cod, nome), 0.0)) / 1e6, 1))
        out["lon"].append(cen[0] if cen else None)
        out["lat"].append(cen[1] if cen else None)
    return out


class _Args:
    """Args simples a partir de um dict (para chamadas internas com filtros)."""

    def __init__(self, d):
        self._d = d

    def get(self, key, default=None):
        return self._d.get(key, default)


CARDS_CAP = 60  # teto de cards por municipio (evita render gigante)


def cards(args) -> dict:
    """Cards das emendas acionaveis de um municipio (ao clicar nele no mapa).

    Mostra as 4 faixas na ordem: Oportunidade -> Recem estagnada -> Prazo
    critico -> Dormente. Respeita UF/setor/busca; ignora o filtro de urgencia.
    """
    cod = (args.get("mun") or "").strip()
    if not cod:
        return {"rows": [], "total": 0, "capped": False, "municipio": ""}

    base = _Args({
        "uf": args.get("uf"),
        "setor": args.get("setor"),
        "q": args.get("q"),
        "mun": cod,
    })
    df = aplicar_filtros(base)
    # nome do municipio antes de filtrar (para exibir mesmo com 0 casos)
    municipio = str(df["municipio_exib"].iloc[0]) if not df.empty else ""

    df = df[df["urgencia"].isin(CARDS_ORDEM) & df["dias_para_prazo"].notna()]
    total = len(df)
    if total == 0:
        return {"rows": [], "total": 0, "capped": False, "municipio": municipio}

    # ordena por faixa (ordem pedida) e, dentro dela, prazo mais critico primeiro
    df = df.assign(_o=df["urgencia"].map(CARDS_RANK)).sort_values(
        ["_o", "dias_para_prazo"], na_position="last"
    )

    rows = []
    for _, r in df.head(CARDS_CAP).iterrows():
        lst = r["setores_list"] if isinstance(r["setores_list"], list) else []
        principal = lst[0] if lst else (r["setor"] or "")
        dt = r["data_fim_execucao_plano_trabalho"]
        data_fim = dt.strftime("%d/%m/%Y") if pd.notna(dt) else None
        rows.append({
            "objeto": _nan_to_none(r["objeto_executor"]),
            "setor": _nan_to_none(r["setor"]) or principal,
            "setor_principal": principal,
            "familia": familia_obra(principal),
            "urgencia": r["urgencia"],
            "valor_total": _nan_to_none(float(r["valor_total"])),
            "dias_para_prazo": int(r["dias_para_prazo"]),
            "data_fim": data_fim,
            "parlamentar": _nan_to_none(r["nome_parlamentar_emenda_plano_acao"]),
            "municipio": municipio,
        })

    return {
        "rows": rows,
        "total": int(total),
        "capped": total > CARDS_CAP,
        "municipio": municipio,
    }


def meta() -> dict:
    """Metadados globais para o boot do frontend."""
    base = {
        "data_ok": STORE.data_ok,
        "erro": STORE.erro,
        "faltando": STORE.faltando,
        "gerado_em": STORE.gerado_em,
        "comando_extracao": "python data_extraction/extrair_dados.py",
    }
    if not STORE.data_ok or STORE.df is None:
        base.update({"ufs": [], "setores": [], "urgencias": [], "totais": {}})
        return base

    base.update({
        "ufs": STORE.ufs,
        "setores": STORE.setores,
        "urgencias": STORE.urgencias,
        "totais": kpis(_EmptyArgs()),
    })
    return base


class _EmptyArgs:
    """Args vazio para chamadas internas sem filtros."""

    def get(self, key, default=None):
        return default


# ===========================================================================
# FRONTEND  (CSS + JS inline; HTML servido como uma unica pagina)
# ===========================================================================

_CSS = r"""
/* ============================================================
   UruTracker - Design System
   Institucional + tecnologico. Azul escuro + ciano.
   REGRA INVIOLAVEL: zero cantos arredondados.
   ============================================================ */

:root {
  --bg-900: #06121F;
  --bg-850: #081826;
  --bg-800: #0A1929;
  --bg-700: #0F2438;
  --panel:  #102A42;
  --panel-2:#16324F;
  --line:   #1E3A5F;
  --line-2: #27496E;

  --cyan:      #00E5FF;
  --cyan-600:  #00B8D4;
  --cyan-700:  #0091A7;
  --cyan-glow: rgba(0, 229, 255, .35);
  --cyan-dim:  rgba(0, 229, 255, .12);

  --ink:    #E6F1F7;
  --ink-2:  #B9D2E4;
  --muted:  #7FA8C9;
  --faint:  #4F718E;

  --alert: #FF4D5E;
  --warn:  #FFB020;
  --info:  #3DA9FC;
  --ok:    #19E39A;
  --dorm:  #3B6FE0;   /* "Dormente" (ex-Abandonado) */

  --alert-dim: rgba(255, 77, 94, .14);
  --warn-dim:  rgba(255, 176, 32, .14);
  --info-dim:  rgba(61, 169, 252, .14);

  --font-ui:   "Segoe UI", system-ui, -apple-system, Roboto, sans-serif;
  --font-mono: ui-monospace, "JetBrains Mono", "Cascadia Mono", "Consolas", monospace;

  --gap: 1px;
}

*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
  border-radius: 0 !important;
}

[hidden] { display: none !important; }

html, body {
  height: 100%;
  background: var(--bg-900);
  color: var(--ink);
  font-family: var(--font-ui);
  font-size: 17px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}

body {
  background-image:
    linear-gradient(var(--line) 1px, transparent 1px),
    linear-gradient(90deg, var(--line) 1px, transparent 1px);
  background-size: 48px 48px, 48px 48px;
  background-position: -1px -1px;
  background-attachment: fixed;
  background-blend-mode: soft-light;
}

.mono { font-family: var(--font-mono); letter-spacing: .04em; }

::selection { background: var(--cyan); color: var(--bg-900); }

* { scrollbar-width: thin; scrollbar-color: var(--cyan-700) var(--bg-800); }
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-track { background: var(--bg-800); }
*::-webkit-scrollbar-thumb { background: var(--line-2); border: 2px solid var(--bg-800); }
*::-webkit-scrollbar-thumb:hover { background: var(--cyan-700); }

.btn {
  font-family: var(--font-mono);
  font-size: 15px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--bg-900);
  background: var(--cyan);
  border: 1px solid var(--cyan);
  padding: 8px 14px;
  cursor: pointer;
  transition: background .12s, box-shadow .12s, color .12s;
}
.btn:hover { box-shadow: 0 0 0 1px var(--cyan), 0 0 14px var(--cyan-glow); }
.btn:active { transform: translateY(1px); }

.btn-ghost {
  background: transparent;
  color: var(--ink-2);
  border: 1px solid var(--line-2);
}
.btn-ghost:hover { color: var(--cyan); border-color: var(--cyan); box-shadow: none; }

.btn-toggle {
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--line-2);
}
.btn-toggle[aria-pressed="true"] {
  color: var(--cyan);
  border-color: var(--cyan);
  background: var(--cyan-dim);
  box-shadow: inset 0 0 12px var(--cyan-dim);
}

select, input[type="text"], input[type="number"] {
  font-family: var(--font-mono);
  font-size: 16px;
  color: var(--ink);
  background: var(--bg-800);
  border: 1px solid var(--line-2);
  padding: 7px 10px;
  width: 100%;
  outline: none;
  transition: border-color .12s, box-shadow .12s;
}
select:focus, input:focus {
  border-color: var(--cyan);
  box-shadow: 0 0 0 1px var(--cyan-glow);
}
select option { background: var(--bg-800); color: var(--ink); }

@keyframes pulse-dot {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 var(--cyan-glow); }
  50%      { opacity: .55; box-shadow: 0 0 0 6px transparent; }
}
@keyframes shimmer {
  0%   { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}
@keyframes flash-cell {
  0%   { background: var(--cyan-dim); }
  100% { background: transparent; }
}

.skeleton {
  background: linear-gradient(90deg, var(--bg-700) 25%, var(--panel-2) 37%, var(--bg-700) 63%);
  background-size: 200% 100%;
  animation: shimmer 1.3s linear infinite;
  color: transparent !important;
}

/* ============================================================
   Layout do dashboard
   ============================================================ */

.app { padding: 0 16px 24px; max-width: 1680px; margin: 0 auto; }

.app-header {
  position: relative;
  overflow: hidden;
  background: linear-gradient(180deg, var(--bg-850), var(--bg-900));
  border-bottom: 1px solid var(--cyan-700);
  box-shadow: 0 1px 0 var(--cyan-dim), 0 6px 24px rgba(0,0,0,.4);
}
.radar-bg {
  position: absolute; inset: 0;
  width: 100%; height: 100%;
  opacity: .55; pointer-events: none;
}
.header-grid {
  position: absolute; inset: 0;
  background-image:
    linear-gradient(90deg, var(--cyan-dim) 1px, transparent 1px);
  background-size: 32px 100%;
  opacity: .25; pointer-events: none;
  mask-image: linear-gradient(90deg, transparent, #000 30%, #000 70%, transparent);
}
.header-content {
  position: relative;
  display: flex; align-items: center; justify-content: space-between;
  gap: 24px; flex-wrap: wrap;
  padding: 18px 24px;
  max-width: 1680px; margin: 0 auto;
}
.brand { display: flex; align-items: center; gap: 16px; }
.brand-logo {
  width: 84px; height: 84px; flex: none; object-fit: contain;
  /* PNG transparente (urubu preto): invert -> urubu branco no header escuro. */
  filter: invert(1);
}
.brand-mark {
  position: relative; width: 40px; height: 40px; flex: none;
  border: 1px solid var(--cyan); background: var(--bg-800);
  display: grid; place-items: center;
}
.mark-ring {
  position: absolute; inset: 6px;
  border: 1px solid var(--cyan-600);
  clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%);
  animation: pulse-dot 2.4s ease-in-out infinite;
}
.mark-core { width: 8px; height: 8px; background: var(--cyan); box-shadow: 0 0 10px var(--cyan); }
.brand-text h1 {
  font-family: var(--font-mono);
  font-size: 30px; letter-spacing: .14em; font-weight: 700;
  color: var(--ink);
}
.brand-text h1 .accent { color: var(--cyan); }
.subtitle { font-family: var(--font-mono); font-size: 13px; color: var(--muted); letter-spacing: .14em; margin-top: 3px; }

.header-status { text-align: right; display: flex; flex-direction: column; gap: 8px; align-items: flex-end; }
.status-line { display: flex; align-items: center; gap: 8px; }
.live-dot {
  width: 9px; height: 9px; background: var(--ok);
  box-shadow: 0 0 10px var(--ok);
  animation: pulse-dot 1.4s ease-in-out infinite;
}
.live-dot.paused { background: var(--faint); box-shadow: none; animation: none; }
#live-label { font-size: 14px; color: var(--ok); letter-spacing: .18em; }
#live-label.paused { color: var(--faint); }
.status-meta { display: flex; flex-direction: column; gap: 2px; font-size: 13px; color: var(--faint); }
#clock { color: var(--cyan); font-size: 16px; letter-spacing: .12em; }

.error-screen { display: grid; place-items: center; min-height: 70vh; padding: 24px; }
.error-box {
  background: var(--bg-800); border: 1px solid var(--alert);
  box-shadow: 0 0 30px var(--alert-dim);
  padding: 32px 36px; max-width: 560px; text-align: left;
  position: relative;
}
.error-box::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--alert);
}
.error-tag { display: inline-block; font-size: 13px; color: var(--alert); border: 1px solid var(--alert); padding: 3px 8px; letter-spacing: .18em; margin-bottom: 14px; }
.error-box h2 { font-size: 25px; margin-bottom: 10px; }
.error-box p { color: var(--ink-2); margin-bottom: 10px; }
.error-hint { color: var(--muted); font-size: 16px; }
.error-cmd { display: block; background: var(--bg-900); border: 1px solid var(--line-2); color: var(--cyan); padding: 12px 14px; margin: 8px 0 20px; font-size: 16px; }
#btn-retry { width: 100%; }

.toolbar {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  background: var(--bg-800); border: 1px solid var(--line);
  padding: 8px; margin: 16px 0; position: relative;
}
.tb-item { position: relative; }
.tb-badge {
  display: inline-block; min-width: 16px; text-align: center;
  background: var(--cyan); color: var(--bg-900); font-size: 11px; font-weight: 700;
  padding: 0 4px; margin: 0 2px;
}
.tb-dot { display: inline-block; width: 7px; height: 7px; background: var(--cyan); margin: 0 2px; }
/* popover de filtros / busca */
.popover {
  position: absolute; top: calc(100% + 5px); left: 0; z-index: 20;
  background: var(--bg-800); border: 1px solid var(--cyan-700);
  box-shadow: 0 8px 28px rgba(0,0,0,.55), 0 0 18px var(--cyan-dim);
  padding: var(--gap); display: flex; flex-direction: column; gap: var(--gap);
  min-width: 320px;
}
.popover-busca { min-width: 380px; }
.field { background: var(--bg-700); padding: 8px 10px; display: flex; flex-direction: column; gap: 5px; }
.field label { font-size: 13px; color: var(--muted); letter-spacing: .12em; }
.active-filters { display: flex; gap: 6px; flex-wrap: wrap; padding: 0 4px; }
.active-filters:empty { display: none; }
.chip {
  font-family: var(--font-mono); font-size: 14px; letter-spacing: .04em;
  background: var(--cyan-dim); color: var(--cyan); border: 1px solid var(--cyan-700);
  padding: 3px 8px; display: inline-flex; gap: 6px; align-items: center; cursor: pointer;
}
.chip:hover { background: var(--cyan); color: var(--bg-900); }
.chip .x { font-weight: 700; }

.kpi-grid {
  display: grid; grid-template-columns: repeat(8, 1fr); gap: var(--gap);
  background: var(--line); border: 1px solid var(--line); margin-bottom: 16px;
}
.kpi {
  position: relative; background: var(--bg-800); padding: 14px 12px 12px;
  overflow: hidden; transition: background .15s; min-width: 0;
}
.kpi::after {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--line-2);
}
.kpi-alert::after { background: var(--alert); }
.kpi-warn::after  { background: var(--warn); }
.kpi-info::after  { background: var(--info); }
.kpi-cyan::after  { background: var(--cyan); }
.kpi-green::after { background: var(--ok); }
.kpi-gray::after  { background: var(--faint); }
.kpi-dorm::after  { background: var(--dorm); }
.kpi:hover { background: var(--bg-700); }
.kpi-head { display: flex; align-items: center; justify-content: space-between; }
.kpi-label { font-size: 11px; color: var(--muted); letter-spacing: .06em; }
.kpi-dot { width: 7px; height: 7px; background: var(--line-2); }
.kpi-alert .kpi-dot { background: var(--alert); box-shadow: 0 0 8px var(--alert); animation: pulse-dot 1.8s infinite; }
.kpi-warn .kpi-dot  { background: var(--warn); box-shadow: 0 0 8px var(--warn); }
.kpi-info .kpi-dot  { background: var(--info); box-shadow: 0 0 8px var(--info); }
.kpi-cyan .kpi-dot  { background: var(--cyan); box-shadow: 0 0 8px var(--cyan); }
.kpi-green .kpi-dot { background: var(--ok); box-shadow: 0 0 8px var(--ok); animation: pulse-dot 1.8s infinite; }
.kpi-gray .kpi-dot  { background: var(--faint); }
.kpi-dorm .kpi-dot  { background: var(--dorm); box-shadow: 0 0 8px var(--dorm); }
.kpi-value { font-size: 26px; font-weight: 700; margin: 7px 0 7px; color: var(--ink); line-height: 1; }
.kpi-cyan .kpi-value { color: var(--cyan); }
.kpi-valor .kpi-value { color: var(--alert); }
.kpi-green .kpi-value { color: var(--ok); }
.kpi-dorm .kpi-value { color: var(--dorm); }
.kpi-money .kpi-value { font-size: 21px; }   /* evita "bi"/"mi" quebrar linha */
.kpi-desc { font-size: 11px; color: var(--muted); line-height: 1.3; margin-bottom: 8px; min-height: 2.1em; }
.kpi-split { display: flex; gap: 12px; margin: 7px 0; }
.kpi-split > div { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.kpi-split-v { font-size: 22px; font-weight: 700; line-height: 1; color: var(--ok); }
.kpi-split-k { font-size: 9px; color: var(--muted); letter-spacing: .06em; }
.kpi-spark { display: flex; align-items: flex-end; gap: 2px; height: 22px; }
.kpi-spark span { flex: 1; background: var(--line-2); min-height: 2px; transition: height .5s, background .3s; }
.kpi-alert .kpi-spark span { background: var(--alert-dim); }
.kpi-spark span.lit { background: currentColor; }
.kpi-alert .kpi-spark { color: var(--alert); }
.kpi-warn .kpi-spark  { color: var(--warn); }
.kpi-info .kpi-spark  { color: var(--info); }
.kpi-cyan .kpi-spark  { color: var(--cyan); }
.kpi-green .kpi-spark { color: var(--ok); }
.kpi-gray .kpi-spark  { color: var(--faint); }
.kpi-dorm .kpi-spark  { color: var(--dorm); }

.grid-main {
  display: grid;
  grid-template-columns: 1.25fr 2fr;
  gap: var(--gap);
  background: var(--line); border: 1px solid var(--line);
  margin-bottom: 16px;
}
.panel { background: var(--bg-800); display: flex; flex-direction: column; min-width: 0; }
/* area dos graficos (col 2 do grid-main): grid interno + ancora p/ o overlay */
.charts-wrap {
  position: relative;
  display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap);
  background: var(--line); min-width: 0;
}
.panel-wide { grid-column: 1 / 3; }
.panel-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 11px 14px; border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, var(--panel), var(--bg-800));
}
.panel-head h3 { font-size: 15px; font-weight: 600; color: var(--ink-2); letter-spacing: .08em; }
.panel-head h3 span { color: var(--cyan); }
.panel-body { padding: 14px; flex: 1; min-height: 0; position: relative; }
.panel-chart .panel-body { height: 240px; }
.panel-chart canvas { width: 100% !important; height: 100% !important; }

.map-head-tools { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
#map-reset { padding: 4px 10px; }
.map-toggle { display: flex; gap: var(--gap); border: 1px solid var(--line-2); }
.map-toggle button {
  font-family: var(--font-mono); font-size: 14px; letter-spacing: .08em;
  background: var(--bg-800); color: var(--muted); border: none; padding: 4px 12px; cursor: pointer;
}
.map-toggle button.active { background: var(--cyan-dim); color: var(--cyan); }
/* PROSPECCAO: botao visualmente distinto (verde, destacado) */
.map-toggle button.prosp-mode {
  color: var(--ok); border: 1px solid var(--ok); background: rgba(25,227,154,.08);
  font-weight: 700;
}
.map-toggle button.prosp-mode.active {
  background: var(--ok); color: var(--bg-900); box-shadow: 0 0 12px rgba(25,227,154,.5);
}
/* toggles da visao prospeccao */
.prosp-tools { display: flex; gap: var(--gap); }
.prosp-tools button {
  font-family: var(--font-mono); font-size: 13px; letter-spacing: .06em;
  background: var(--bg-800); border: 1px solid var(--line-2); padding: 4px 10px; cursor: pointer;
  color: var(--muted);
}
.prosp-pin[aria-pressed="true"] { color: var(--ok); border-color: var(--ok); background: rgba(25,227,154,.1); }
.prosp-crit[aria-pressed="true"] { color: var(--warn); border-color: var(--warn); background: var(--warn-dim); }
.map-body { display: flex; flex-direction: column; gap: 10px; }
.map-wrap { position: relative; flex: 1; min-height: 420px; }
#brazil-map { width: 100%; height: 100%; display: block; }
#brazil-map path {
  stroke: var(--bg-900); stroke-width: .25;
  vector-effect: non-scaling-stroke;
  cursor: pointer; transition: fill .2s, stroke .12s, filter .12s;
}
#brazil-map path:hover { stroke: var(--cyan); stroke-width: .6; filter: drop-shadow(0 0 4px var(--cyan-glow)); }
#brazil-map path.selected { stroke: var(--cyan); stroke-width: .8; }
#brazil-map path.mun { stroke: var(--bg-900); stroke-width: .12; }
#brazil-map path.mun:hover { stroke: var(--cyan); stroke-width: .5; filter: drop-shadow(0 0 3px var(--cyan-glow)); }
/* contorno ciano: municipios com oportunidade/recem est. (+critico) — persiste */
#brazil-map path.mun.has-opp { stroke: var(--cyan); stroke-width: .45; }
#brazil-map path.mun.selected { stroke: var(--cyan); stroke-width: .8; filter: drop-shadow(0 0 3px var(--cyan-glow)); }
#brazil-map text { transition: opacity .3s; }
/* pins de prospeccao */
#brazil-map circle.pin { stroke: var(--bg-900); stroke-width: .12; vector-effect: non-scaling-stroke; }
#brazil-map circle.pin-opp { fill: #1FFFAE; filter: drop-shadow(0 0 2.5px rgba(31,255,174,.9)); }
#brazil-map circle.pin-est { fill: #6FB89A; opacity: .9; }
#brazil-map circle.pin-crit { fill: var(--warn); filter: drop-shadow(0 0 2px rgba(255,176,32,.8)); }
.map-tooltip {
  position: absolute; pointer-events: none; z-index: 6;
  background: var(--bg-900); border: 1px solid var(--cyan-700);
  box-shadow: 0 0 14px var(--cyan-dim);
  padding: 7px 10px; font-size: 14px; color: var(--ink); white-space: nowrap;
  transform: translate(-50%, -120%);
}
.map-tooltip b { color: var(--cyan); }
.map-legend { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--muted); flex-wrap: wrap; }
.legend-scale { display: flex; height: 10px; flex: 1; min-width: 120px; border: 1px solid var(--line-2); }
.legend-scale span { flex: 1; }

.panel-table { margin-bottom: 16px; }
.opp-toggle {
  display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
  font-size: 13px; color: var(--ok); letter-spacing: .06em; user-select: none;
}
.opp-toggle input { width: auto; accent-color: var(--ok); cursor: pointer; }
.pager { display: flex; align-items: center; gap: 6px; font-size: 15px; color: var(--muted); }
.pager button {
  font-family: var(--font-mono); background: var(--bg-700); color: var(--ink-2);
  border: 1px solid var(--line-2); padding: 3px 9px; cursor: pointer; font-size: 16px;
}
.pager button:hover:not(:disabled) { border-color: var(--cyan); color: var(--cyan); }
.pager button:disabled { opacity: .3; cursor: default; }
.pager #pg-info { color: var(--ink-2); min-width: 96px; text-align: center; }
.pg-sep { color: var(--faint); }
.pager select { width: auto; padding: 3px 6px; display: inline; }
.pager label { display: inline-flex; gap: 6px; align-items: center; font-size: 14px; }

.table-body { padding: 0; }
.table-scroll { max-height: 560px; overflow: auto; }
table.leads { width: 100%; border-collapse: collapse; font-size: 16px; }
table.leads thead th {
  position: sticky; top: 0; z-index: 2;
  background: var(--panel); color: var(--muted);
  font-family: var(--font-mono); font-size: 13px; letter-spacing: .1em; text-transform: uppercase;
  text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--cyan-700);
  white-space: nowrap;
}
table.leads thead th[data-sort] { cursor: pointer; }
table.leads thead th[data-sort]:hover { color: var(--cyan); }
table.leads thead th.sorted::after { content: " \25BE"; color: var(--cyan); }
table.leads td { padding: 8px 12px; border-bottom: 1px solid var(--line); vertical-align: top; color: var(--ink-2); }
table.leads tbody tr { transition: background .1s; }
table.leads tbody tr:hover td { background: var(--cyan-dim); }
table.leads tbody tr.flash td { animation: flash-cell .8s ease-out; }
.right { text-align: right; }
.col-obj { max-width: 240px; color: var(--muted); font-size: 14px; }
.col-val { font-family: var(--font-mono); color: var(--ink); white-space: nowrap; }
.col-muni { color: var(--ink); font-weight: 600; }

.badge {
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--font-mono); font-size: 13px; font-weight: 700; letter-spacing: .06em;
  padding: 3px 8px; border: 1px solid currentColor; white-space: nowrap;
}
.badge::before { content: ""; width: 6px; height: 6px; background: currentColor; }
/* cores das faixas vem inline (CAT) via JS — ver const CATS. */

.prazo-wrap { display: flex; flex-direction: column; gap: 3px; align-items: flex-end; min-width: 96px; }
.prazo-txt { font-family: var(--font-mono); font-size: 14px; white-space: nowrap; }
.prazo-txt.vencido { color: var(--alert); }
.prazo-txt.critico { color: var(--warn); }
.prazo-txt.ok { color: var(--ok); }
.prazo-bar { width: 90px; height: 4px; background: var(--bg-700); border: 1px solid var(--line-2); }
.prazo-bar i { display: block; height: 100%; }
.prazo-bar i.vencido { background: var(--alert); }
.prazo-bar i.critico { background: var(--warn); }
.prazo-bar i.ok { background: var(--ok); }
.muted-cell { color: var(--faint); }

/* ============================ CARDS (casos criticos do municipio) ====== */
/* slide-over: overlay absoluto sobre a area dos graficos (charts-wrap).
   Fechado nao ocupa espaco no layout; desliza da direita ao abrir. */
.cards-section {
  position: absolute; inset: 0; z-index: 8;
  display: flex; flex-direction: column;
  background: var(--bg-900); border: 1px solid var(--warn);
  box-shadow: -10px 0 30px rgba(0,0,0,.55), 0 0 24px var(--warn-dim);
  overflow: hidden;
  transform: translateX(102%); opacity: 0; pointer-events: none;
  transition: transform .3s ease, opacity .3s ease;
}
.cards-section.open { transform: none; opacity: 1; pointer-events: auto; }
.cards-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 11px 14px; border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, var(--panel), var(--bg-800));
}
.cards-head h3 { font-size: 15px; font-weight: 600; color: var(--warn); letter-spacing: .08em; }
.cards-head h3 span { color: var(--ink); }
#cards-close { padding: 4px 10px; }
.cards-grid {
  flex: 1; overflow-y: auto; align-content: start;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--gap); background: var(--line); padding: var(--gap);
}
.cards-empty { grid-column: 1 / -1; padding: 28px; text-align: center; color: var(--muted); }
.card {
  position: relative; background: var(--bg-800); padding: 14px 16px 12px;
  border-left: 3px solid var(--warn); display: flex; flex-direction: column; gap: 10px;
  transition: background .12s;
}
.card:hover { background: var(--bg-700); }
.card-top { display: flex; align-items: center; gap: 10px; }
.card-icon {
  width: 40px; height: 40px; flex: none; display: grid; place-items: center;
  border: 1px solid var(--cyan-700); background: var(--bg-900); color: var(--cyan);
  box-shadow: inset 0 0 10px var(--cyan-dim);
}
.card-icon svg { width: 24px; height: 24px; }
.card-setor {
  flex: 1; min-width: 0; font-family: var(--font-mono); font-size: 13px;
  color: var(--ink); letter-spacing: .04em; line-height: 1.2;
}
.card-obj {
  font-size: 14px; color: var(--ink-2); line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.card-meta {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px;
  border-top: 1px solid var(--line); padding-top: 10px;
}
.card-meta > div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.card-k { font-family: var(--font-mono); font-size: 11px; color: var(--faint); letter-spacing: .1em; }
.card-v { font-size: 14px; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-v.prazo-txt { font-family: var(--font-mono); }

.app-footer { text-align: center; color: var(--faint); font-size: 13px; letter-spacing: .14em; padding: 18px 0 6px; border-top: 1px solid var(--line); margin-top: 8px; }

@media (max-width: 1180px) {
  .grid-main { grid-template-columns: 1fr; }
  .kpi-grid { grid-template-columns: repeat(4, 1fr); }
  .map-wrap { min-height: 360px; }
}
@media (max-width: 860px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .charts-wrap { grid-template-columns: 1fr; }
  .panel-wide { grid-column: 1; }
  .header-status { text-align: left; align-items: flex-start; }
}
"""

_JS = r"""
/* api.js - wrapper de acesso a API JSON do backend. */
const API = (() => {
  async function get(path, params) {
    const qs = params ? '?' + new URLSearchParams(clean(params)).toString() : '';
    const res = await fetch('/api/' + path + qs);
    if (!res.ok) throw new Error('API ' + path + ' -> ' + res.status);
    return res.json();
  }

  function clean(obj) {
    const out = {};
    for (const [k, v] of Object.entries(obj || {})) {
      if (v !== '' && v != null) out[k] = v;
    }
    return out;
  }

  return {
    meta:         ()    => get('meta'),
    kpis:         (f)   => get('kpis', f),
    leads:        (f)   => get('leads', f),
    aggUf:        (f)   => get('agg/uf', f),
    aggMunicipio: (f)   => get('agg/municipio', f),
    aggSetor:     (f)   => get('agg/setor', f),
    aggUrgencia:  (f)   => get('agg/urgencia', f),
    prospUf:      (f)   => get('prospeccao/uf', f),
    prospMun:     (f)   => get('prospeccao/mun', f),
    cards:        (f)   => get('cards', f),
    reload:       ()    => fetch('/api/reload', { method: 'POST' }).then(r => r.json()),
  };
})();

/* classificacao por faixa de prazo: fonte unica de chave/label/cor.
   Ordem = ordem da tabela (Projeto em andamento -> Abandonado). */
const CATS = [
  { key: 'ANDAMENTO',    label: 'Projeto em andamento', color: '#7A8B99', dim: 'rgba(122,139,153,.16)' },
  { key: 'POSSIVEL',     label: 'Possivel oportunidade', color: '#3DA9FC', dim: 'rgba(61,169,252,.16)' },
  { key: 'CRITICO',      label: 'Prazo critico',         color: '#FFB020', dim: 'rgba(255,176,32,.16)' },
  { key: 'OPORTUNIDADE', label: 'Oportunidade',          color: '#19E39A', dim: 'rgba(25,227,154,.16)' },
  { key: 'ESTAGNADO',    label: 'Recem estagnada',       color: '#8FBF9F', dim: 'rgba(143,191,159,.16)' },
  { key: 'ABANDONADO',   label: 'Dormente',              color: '#3B6FE0', dim: 'rgba(59,111,224,.16)' },
];
const CAT = Object.fromEntries(CATS.map((c) => [c.key, c]));

/* counters.js - contadores animados (count-up) e sparklines dos KPIs. */
const Counters = (() => {
  const fmtInt = (n) => Math.round(n).toLocaleString('pt-BR');

  function fmtBRL(n) {
    if (n >= 1e9) return 'R$ ' + (n / 1e9).toFixed(2) + ' bi';
    if (n >= 1e6) return 'R$ ' + (n / 1e6).toFixed(1) + ' mi';
    return 'R$ ' + fmtInt(n);
  }

  function animate(el, to, format) {
    const from = Number(el.dataset.current || 0);
    el.dataset.current = to;
    const dur = 700;
    const t0 = performance.now();
    const fmt = format === 'brl' ? fmtBRL : fmtInt;
    function step(now) {
      const p = Math.min(1, (now - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(from + (to - from) * eased);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function spark(container, seed) {
    const N = 16;
    let bars = container._bars;
    if (!bars) {
      bars = [];
      for (let i = 0; i < N; i++) {
        const s = document.createElement('span');
        container.appendChild(s);
        bars.push(s);
      }
      container._bars = bars;
    }
    let x = Math.abs(Math.sin(seed * 0.9) * 1000);
    bars.forEach((b, i) => {
      x = (x * 9301 + 49297) % 233280;
      const r = x / 233280;
      const h = 18 + r * 78 + (i === N - 1 ? 6 : 0);
      b.style.height = h + '%';
      b.classList.toggle('lit', r > 0.62);
    });
  }

  function updateKpis(data) {
    document.querySelectorAll('.kpi').forEach((card) => {
      // card "split": multiplos [data-count], cada um com seu data-key
      const multi = card.querySelectorAll('[data-count][data-key]');
      if (multi.length) {
        let seed = 0;
        multi.forEach((el) => {
          const v = data[el.dataset.key] ?? 0;
          animate(el, v, el.dataset.format);
          seed += v;
        });
        spark(card.querySelector('.kpi-spark'), seed + card.dataset.kpi.length);
        return;
      }
      const key = card.dataset.kpi;
      const val = data[key] ?? 0;
      animate(card.querySelector('[data-count]'), val, card.dataset.format);
      spark(card.querySelector('.kpi-spark'), val + key.length);
    });
  }

  return { updateKpis, fmtBRL, fmtInt };
})();

/* radar.js - varredura de radar animada no header (canvas). */
const Radar = (() => {
  let canvas, ctx, raf, blips = [];

  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr;
    canvas.height = r.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function seedBlips() {
    blips = [];
    const n = 14;
    for (let i = 0; i < n; i++) {
      blips.push({ a: Math.random() * Math.PI * 2, r: 0.25 + Math.random() * 0.72 });
    }
  }

  function draw(t) {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.clearRect(0, 0, w, h);

    const cx = w * 0.5, cy = h * 0.5;
    const R = Math.max(w, h) * 0.62;

    ctx.strokeStyle = 'rgba(0,229,255,0.10)';
    ctx.lineWidth = 1;
    for (let i = 1; i <= 4; i++) {
      ctx.beginPath();
      ctx.ellipse(cx, cy, (R * i) / 4, (R * i) / 4 * 0.42, 0, 0, Math.PI * 2);
      ctx.stroke();
    }
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R * 0.42);
      ctx.stroke();
    }

    const sweep = (t * 0.0007) % (Math.PI * 2);
    ctx.save();
    ctx.translate(cx, cy);
    ctx.scale(1, 0.42);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.arc(0, 0, R, sweep - 0.5, sweep);
    ctx.closePath();
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, R);
    g.addColorStop(0, 'rgba(0,229,255,0.22)');
    g.addColorStop(1, 'rgba(0,229,255,0)');
    ctx.fillStyle = g;
    ctx.fill();
    ctx.restore();

    ctx.strokeStyle = 'rgba(0,229,255,0.5)';
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(sweep) * R, cy + Math.sin(sweep) * R * 0.42);
    ctx.stroke();

    blips.forEach((b) => {
      let diff = ((sweep - b.a) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
      const glow = Math.max(0, 1 - diff / 0.9);
      if (glow <= 0.02) return;
      const x = cx + Math.cos(b.a) * R * b.r;
      const y = cy + Math.sin(b.a) * R * 0.42 * b.r;
      ctx.fillStyle = `rgba(25,227,154,${glow})`;
      ctx.fillRect(x - 2, y - 2, 4, 4);
    });

    raf = requestAnimationFrame(draw);
  }

  function start() {
    canvas = document.getElementById('radar-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    resize();
    seedBlips();
    window.addEventListener('resize', resize);
    raf = requestAnimationFrame(draw);
  }

  return { start };
})();

/* map.js - mapa coropletico do Brasil (SVG inline) por UF, com zoom/drill-down. */
const BrMap = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = () => document.getElementById('brazil-map');
  let paths = {};
  let centroids = {};
  let mode = 'prospeccao';   // 'prospeccao' (default) | 'qtd' | 'valor'
  let metric = 'qtd';        // usado so quando mode e 'qtd'/'valor'
  let values = {};
  let maxVal = 1;
  let selected = '';
  let onSelect = () => {};
  // --- prospeccao ---
  let criticoOn = false;     // inclui prazo critico no calor/hover/pins
  let pinsOppOn = true;      // pins de oportunidade + recem estagnada
  let propUf = {};           // sigla -> {opp,est,dorm,crit,valor}
  let propMun = {};          // cod -> {opp,est,dorm,crit,valor,lon,lat,nome}
  let pinLayer = null;
  let bboxes = {};        // sigla -> {minX,minY,maxX,maxY} no espaco do viewBox
  let labelEls = [];      // <text> das siglas, escondidos durante o zoom
  let curVB = [0, 0, 100, 100];
  let vbRaf = null;
  let natBbox = null;     // bbox nacional (mesma projecao p/ a malha municipal)
  let munLayer = null;    // <g> com os poligonos dos municipios da UF em zoom
  let munPaths = {};      // cod_ibge -> <path>
  let munValues = {};     // cod_ibge -> {qtd, valor, nome}
  let munSelected = '';
  let munMax = 1;
  let munReq = 0;         // token p/ descartar fetch de UF da qual ja saimos
  let onMunSelect = () => {};

  function colorScale(v, mx) {
    if (!v) return '#0C2031';
    const t = Math.pow(v / mx, 0.6);
    const c1 = [16, 42, 66];
    const c2 = [0, 229, 255];
    const mix = c1.map((a, i) => Math.round(a + (c2[i] - a) * t));
    return `rgb(${mix[0]},${mix[1]},${mix[2]})`;
  }
  function color(v) { return colorScale(v, maxVal); }

  function project(lon, lat, bbox) {
    const { minX, maxX, minY, maxY } = bbox;
    const w = maxX - minX, h = maxY - minY;
    const scale = 96 / Math.max(w, h);
    const ox = (100 - w * scale) / 2;
    const oy = (100 - h * scale) / 2;
    return [ox + (lon - minX) * scale, oy + (maxY - lat) * scale];
  }

  function ringToPath(ring, bbox) {
    let d = '';
    ring.forEach((pt, i) => {
      const [x, y] = project(pt[0], pt[1], bbox);
      d += (i ? 'L' : 'M') + x.toFixed(2) + ' ' + y.toFixed(2) + ' ';
    });
    return d + 'Z';
  }

  function bounds(geo) {
    let minX = 180, maxX = -180, minY = 90, maxY = -90;
    geo.features.forEach((f) => eachRing(f.geometry, (ring) => {
      ring.forEach((p) => {
        if (p[0] < minX) minX = p[0]; if (p[0] > maxX) maxX = p[0];
        if (p[1] < minY) minY = p[1]; if (p[1] > maxY) maxY = p[1];
      });
    }));
    return { minX, maxX, minY, maxY };
  }

  function eachRing(geom, fn) {
    if (geom.type === 'Polygon') geom.coordinates.forEach(fn);
    else if (geom.type === 'MultiPolygon') geom.coordinates.forEach((p) => p.forEach(fn));
  }

  async function build() {
    let geo;
    try {
      geo = await fetch('/vendor/brazil.geojson').then((r) => r.json());
    } catch (e) {
      svg().innerHTML = '<text x="50" y="50" fill="#7FA8C9" font-size="4" text-anchor="middle">MAPA INDISPONIVEL</text>';
      return;
    }
    const bbox = bounds(geo);
    natBbox = bbox;
    const tip = document.getElementById('map-tooltip');

    // fundo clicavel (o "mar"): clicar fora dos estados volta pro Brasil.
    const bg = document.createElementNS(NS, 'rect');
    bg.setAttribute('x', '-1000'); bg.setAttribute('y', '-1000');
    bg.setAttribute('width', '3000'); bg.setAttribute('height', '3000');
    bg.setAttribute('fill', 'transparent');
    bg.setAttribute('pointer-events', 'all');
    bg.addEventListener('click', () => onSelect(''));
    svg().appendChild(bg);

    const frag = document.createDocumentFragment();

    geo.features.forEach((f) => {
      const sigla = f.properties.sigla || f.properties.SIGLA;
      const nome = f.properties.name || f.properties.Estado || sigla;
      let d = '';
      let sx = 0, sy = 0, sn = 0;
      let bxMin = 1e9, byMin = 1e9, bxMax = -1e9, byMax = -1e9;
      eachRing(f.geometry, (ring) => {
        d += ringToPath(ring, bbox);
        ring.forEach((p) => {
          const [x, y] = project(p[0], p[1], bbox);
          sx += x; sy += y; sn++;
          if (x < bxMin) bxMin = x; if (x > bxMax) bxMax = x;
          if (y < byMin) byMin = y; if (y > byMax) byMax = y;
        });
      });
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', d);
      path.setAttribute('fill', '#0C2031');
      path.dataset.uf = sigla;
      path.dataset.nome = nome;
      path.addEventListener('mousemove', (ev) => showTip(ev, sigla, nome, tip));
      path.addEventListener('mouseleave', () => { tip.hidden = true; });
      path.addEventListener('click', () => onSelect(selected === sigla ? '' : sigla));
      frag.appendChild(path);
      paths[sigla] = path;
      centroids[sigla] = { x: sx / sn, y: sy / sn };
      bboxes[sigla] = { minX: bxMin, minY: byMin, maxX: bxMax, maxY: byMax };
    });

    svg().appendChild(frag);

    Object.entries(centroids).forEach(([sigla, c]) => {
      const t = document.createElementNS(NS, 'text');
      t.setAttribute('x', c.x.toFixed(1));
      t.setAttribute('y', (c.y + 0.6).toFixed(1));
      t.setAttribute('text-anchor', 'middle');
      t.setAttribute('font-size', '2.6');
      t.setAttribute('font-family', 'ui-monospace, monospace');
      t.setAttribute('fill', 'rgba(230,241,247,0.75)');
      t.setAttribute('pointer-events', 'none');
      t.dataset.uf = sigla;
      t.textContent = sigla;
      svg().appendChild(t);
      labelEls.push(t);
    });

    setupWheelZoom();
  }

  // ---- zoom (tween do viewBox) ----
  function tweenVB(target, ms) {
    if (vbRaf) cancelAnimationFrame(vbRaf);
    const start = curVB.slice();
    const t0 = performance.now();
    function step(now) {
      const p = Math.min(1, (now - t0) / ms);
      const e = 1 - Math.pow(1 - p, 3);
      const vb = start.map((s, i) => s + (target[i] - s) * e);
      svg().setAttribute('viewBox', vb.join(' '));
      curVB = vb;
      rescalePins();
      if (p < 1) vbRaf = requestAnimationFrame(step);
    }
    vbRaf = requestAnimationFrame(step);
  }

  function zoomTo(sigla) {
    const b = bboxes[sigla];
    if (!b) return;
    const w = b.maxX - b.minX, h = b.maxY - b.minY;
    const pad = Math.max(w, h) * 0.16 + 2;
    tweenVB([b.minX - pad, b.minY - pad, w + pad * 2, h + pad * 2], 480);
    labelEls.forEach((t) => { t.style.opacity = '0'; });
  }

  function zoomReset() {
    tweenVB([0, 0, 100, 100], 480);
    labelEls.forEach((t) => { t.style.opacity = '1'; });
  }

  // ---- zoom por scroll do mouse (ancorado no cursor), em qualquer visao ----
  function setupWheelZoom() {
    const wrap = document.getElementById('map-wrap');
    if (!wrap) return;
    wrap.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      if (vbRaf) { cancelAnimationFrame(vbRaf); vbRaf = null; }
      // posicao do cursor em coordenadas do viewBox (considera letterbox)
      const pt = svg().createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      const ctm = svg().getScreenCTM();
      if (!ctm) return;
      const loc = pt.matrixTransform(ctm.inverse());
      const [vx, vy, vw, vh] = curVB;
      const factor = ev.deltaY < 0 ? 0.85 : 1 / 0.85;   // scroll p/ cima = zoom in
      const MIN = 4, MAX = 100;
      let nw = Math.min(MAX, Math.max(MIN, vw * factor));
      let nh = Math.min(MAX, Math.max(MIN, vh * factor));
      // mantem o ponto sob o cursor fixo
      let nx = loc.x - (loc.x - vx) * (nw / vw);
      let ny = loc.y - (loc.y - vy) * (nh / vh);
      curVB = [nx, ny, nw, nh];
      svg().setAttribute('viewBox', curVB.join(' '));
      rescalePins();
    }, { passive: false });
  }

  // componentes do calor para o tooltip (prospeccao)
  function prospLines(o) {
    o = o || {};
    let s = `OPORT.: <b>${(o.opp || 0)}</b> &middot; REC.EST.: <b>${(o.est || 0)}</b>` +
            `<br>DORMENTES: <b>${(o.dorm || 0)}</b>`;
    if (criticoOn) s += ` &middot; CRITICOS: <b>${(o.crit || 0)}</b>`;
    return s;
  }

  function showTip(ev, sigla, nome, tip) {
    const wrap = document.getElementById('map-wrap').getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = (ev.clientX - wrap.left) + 'px';
    tip.style.top = (ev.clientY - wrap.top) + 'px';
    if (mode === 'prospeccao') {
      // em zoom de UF, os demais estados ficam zerados pelo filtro -> so o nome
      if (selected && sigla !== selected) {
        tip.innerHTML = `<b>${sigla}</b> &middot; ${nome}`;
      } else {
        tip.innerHTML = `<b>${sigla}</b> &middot; ${nome}<br>` + prospLines(propUf[sigla]);
      }
      return;
    }
    const v = values[sigla] || { qtd: 0, valor: 0 };
    if (selected && sigla !== selected) {
      tip.innerHTML = `<b>${sigla}</b> &middot; ${nome}`;
    } else {
      tip.innerHTML =
        `<b>${sigla}</b> &middot; ${nome}<br>` +
        `${v.qtd.toLocaleString('pt-BR')} paradas &middot; ` +
        `R$ ${v.valor.toFixed(1)} mi`;
    }
  }

  // calor = oportunidade + recem estagnada + dormente (+ critico se ligado)
  function heat(o) {
    if (!o) return 0;
    return (o.opp || 0) + (o.est || 0) + (o.dorm || 0) + (criticoOn ? (o.crit || 0) : 0);
  }

  function recolor() {
    if (mode === 'prospeccao') {
      const vals = Object.values(propUf).map(heat);
      maxVal = Math.max(1, ...vals);
      Object.entries(paths).forEach(([sigla, p]) => {
        p.setAttribute('fill', colorScale(heat(propUf[sigla]), maxVal));
        p.classList.toggle('selected', sigla === selected);
      });
    } else {
      const vals = Object.values(values).map((v) => v[metric]);
      maxVal = Math.max(1, ...vals);
      Object.entries(paths).forEach(([sigla, p]) => {
        const v = (values[sigla] || {})[metric] || 0;
        p.setAttribute('fill', color(v));
        p.classList.toggle('selected', sigla === selected);
      });
    }
    drawLegend();
  }

  function drawLegend() {
    const leg = document.getElementById('map-legend');
    let unit, maxTxt;
    if (mode === 'prospeccao') {
      unit = 'prospeccao' + (criticoOn ? ' + criticos' : '');
      maxTxt = maxVal.toLocaleString('pt-BR');
    } else {
      unit = metric === 'qtd' ? 'emendas' : 'R$ mi';
      maxTxt = metric === 'qtd' ? maxVal.toLocaleString('pt-BR') : maxVal.toFixed(0);
    }
    leg.innerHTML =
      `<span>0</span><div class="legend-scale" id="lg"></div>` +
      `<span>${maxTxt}</span>` +
      `<span style="margin-left:8px">CALOR &middot; ${unit}</span>`;
    const sc = document.getElementById('lg');
    for (let i = 0; i < 8; i++) {
      const s = document.createElement('span');
      s.style.background = colorScale((maxVal * (i + 1)) / 8, maxVal);
      sc.appendChild(s);
    }
  }

  function update(agg) {
    values = {};
    (agg.labels || []).forEach((uf, i) => {
      values[uf] = { qtd: agg.qtd[i] || 0, valor: agg.valor[i] || 0 };
    });
    recolor();
  }

  // ---- prospeccao: dados por UF e por municipio ----
  function updateProspUf(agg) {
    propUf = {};
    (agg.labels || []).forEach((uf, i) => {
      propUf[uf] = {
        opp: agg.oportunidade[i] || 0, est: agg.estagnado[i] || 0,
        dorm: agg.dormente[i] || 0, crit: agg.critico[i] || 0,
        valor: agg.valor_oport[i] || 0,
      };
    });
    if (mode === 'prospeccao') recolor();
  }

  function updateProspMun(agg) {
    propMun = {};
    (agg.codes || []).forEach((c, i) => {
      propMun[String(c)] = {
        nome: agg.nomes[i], opp: agg.oportunidade[i] || 0, est: agg.estagnado[i] || 0,
        dorm: agg.dormente[i] || 0, crit: agg.critico[i] || 0,
        valor: agg.valor_oport[i] || 0,
        lon: agg.lon[i], lat: agg.lat[i],
      };
    });
    if (mode === 'prospeccao') { recolorMun(); renderPins(); }
  }

  // ---- pins de prospeccao (oportunidade/estagnada/critico) ----
  function pinScale() { return curVB[2] / 100; }   // mantem tamanho ~constante na tela

  function makePin(cx, cy, r, cls) {
    const c = document.createElementNS(NS, 'circle');
    c.setAttribute('cx', cx.toFixed(2));
    c.setAttribute('cy', cy.toFixed(2));
    c.setAttribute('r', (r * pinScale()).toFixed(3));
    c.setAttribute('class', 'pin ' + cls);
    c.dataset.br = r;   // raio base (em unidades Brasil) p/ rescale no zoom
    return c;
  }

  function renderPins() {
    if (pinLayer) { pinLayer.remove(); pinLayer = null; }
    if (mode !== 'prospeccao' || !natBbox) return;
    const g = document.createElementNS(NS, 'g');
    g.setAttribute('id', 'pin-layer');
    g.setAttribute('pointer-events', 'none');
    Object.values(propMun).forEach((m) => {
      if (m.lon == null || m.lat == null) return;
      const [x, y] = project(m.lon, m.lat, natBbox);
      // verde: oportunidade (brilhante, tamanho ~ valor) ou recem estagnada (fixo)
      if (pinsOppOn) {
        if (m.opp > 0) {
          const r = 0.7 + Math.min(2.1, Math.sqrt(Math.max(0, m.valor)) * 0.42);
          g.appendChild(makePin(x, y, r, 'pin-opp'));
        } else if (m.est > 0) {
          g.appendChild(makePin(x - 0.0, y, 0.85, 'pin-est'));
        }
      }
      // amarelo: prazo critico (fixo), so com o seletor ligado
      if (criticoOn && m.crit > 0) {
        g.appendChild(makePin(x + (m.opp > 0 || m.est > 0 ? 1.4 : 0), y, 0.85, 'pin-crit'));
      }
    });
    svg().appendChild(g);   // sempre por cima (apos estados/municipios)
    pinLayer = g;
  }

  function rescalePins() {
    if (!pinLayer) return;
    const s = pinScale();
    pinLayer.querySelectorAll('circle.pin').forEach((c) => {
      c.setAttribute('r', (parseFloat(c.dataset.br) * s).toFixed(3));
    });
  }

  // ---- camada de municipios (drill-down da UF em zoom) ----
  async function showMunicipios(uf) {
    clearMunicipios();
    if (!uf || !natBbox) return;
    const myReq = ++munReq;
    showLoading(uf);
    let geo;
    try { geo = await fetch('/vendor/mun/' + uf).then((r) => r.json()); }
    catch (e) { if (myReq === munReq) hideLoading(); return; }
    if (myReq !== munReq) return;   // saimos do estado antes de carregar: descarta
    const tip = document.getElementById('map-tooltip');
    const g = document.createElementNS(NS, 'g');
    g.setAttribute('id', 'mun-layer');
    geo.features.forEach((f) => {
      const code = String(f.properties.id);
      const nome = f.properties.name || code;
      let d = '';
      eachRing(f.geometry, (ring) => { d += ringToPath(ring, natBbox); });
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', d);
      path.setAttribute('fill', '#0C2031');
      path.classList.add('mun');
      path.dataset.code = code;
      path.dataset.nome = nome;
      path.addEventListener('mousemove', (ev) => showMunTip(ev, code, nome, tip));
      path.addEventListener('mouseleave', () => { tip.hidden = true; });
      path.addEventListener('click', () => onMunSelect(munSelected === code ? '' : code));
      g.appendChild(path);
      munPaths[code] = path;
    });
    if (myReq !== munReq) return;   // checagem final antes de inserir no DOM
    svg().appendChild(g);
    munLayer = g;
    hideLoading();
    recolorMun();
    renderPins();   // pins por cima da malha municipal recem inserida
  }

  function clearMunicipios() {
    munReq++;                       // invalida qualquer fetch de municipio em voo
    hideLoading();
    if (munLayer) { munLayer.remove(); munLayer = null; }
    munPaths = {};
  }

  // spinner de carregamento centrado no estado selecionado (SMIL, sem CSS).
  function showLoading(uf) {
    hideLoading();
    const c = centroids[uf], b = bboxes[uf];
    if (!c || !b) return;
    const r = Math.max(b.maxX - b.minX, b.maxY - b.minY) * 0.10 + 1;
    const sw = r * 0.20;
    const circ = 2 * Math.PI * r;
    const g = document.createElementNS(NS, 'g');
    g.setAttribute('id', 'mun-loading');
    g.setAttribute('pointer-events', 'none');
    const ring = document.createElementNS(NS, 'circle');
    ring.setAttribute('cx', c.x); ring.setAttribute('cy', c.y); ring.setAttribute('r', r);
    ring.setAttribute('fill', 'none');
    ring.setAttribute('stroke', 'rgba(0,229,255,0.18)');
    ring.setAttribute('stroke-width', sw);
    const arc = document.createElementNS(NS, 'circle');
    arc.setAttribute('cx', c.x); arc.setAttribute('cy', c.y); arc.setAttribute('r', r);
    arc.setAttribute('fill', 'none');
    arc.setAttribute('stroke', '#00E5FF');
    arc.setAttribute('stroke-width', sw);
    arc.setAttribute('stroke-linecap', 'round');
    arc.setAttribute('stroke-dasharray', (circ * 0.28) + ' ' + (circ * 0.72));
    const at = document.createElementNS(NS, 'animateTransform');
    at.setAttribute('attributeName', 'transform');
    at.setAttribute('type', 'rotate');
    at.setAttribute('from', '0 ' + c.x + ' ' + c.y);
    at.setAttribute('to', '360 ' + c.x + ' ' + c.y);
    at.setAttribute('dur', '0.9s');
    at.setAttribute('repeatCount', 'indefinite');
    arc.appendChild(at);
    g.appendChild(ring); g.appendChild(arc);
    svg().appendChild(g);
  }

  function hideLoading() {
    const e = document.getElementById('mun-loading');
    if (e) e.remove();
  }

  function updateMun(agg) {
    munValues = {};
    (agg.codes || []).forEach((c, i) => {
      munValues[String(c)] = {
        qtd: agg.qtd[i] || 0, valor: agg.valor[i] || 0, nome: agg.labels[i],
      };
    });
    recolorMun();
  }

  // municipio com oportunidade/recem estagnada (+critico se ligado) -> contorno
  function munHasOpp(o) {
    if (!o) return false;
    return (o.opp || 0) > 0 || (o.est || 0) > 0 || (criticoOn && (o.crit || 0) > 0);
  }

  function recolorMun() {
    if (!munLayer) return;
    if (mode === 'prospeccao') {
      const vals = Object.values(propMun).map(heat);
      munMax = Math.max(1, ...vals);
      Object.entries(munPaths).forEach(([code, p]) => {
        p.setAttribute('fill', colorScale(heat(propMun[code]), munMax));
        p.classList.toggle('has-opp', munHasOpp(propMun[code]));  // contorno persiste
        p.classList.toggle('selected', code === munSelected);
      });
    } else {
      const vals = Object.values(munValues).map((v) => v[metric]);
      munMax = Math.max(1, ...vals);
      Object.entries(munPaths).forEach(([code, p]) => {
        const v = (munValues[code] || {})[metric] || 0;
        p.setAttribute('fill', colorScale(v, munMax));
        p.classList.remove('has-opp');
        p.classList.toggle('selected', code === munSelected);
      });
    }
  }

  function showMunTip(ev, code, nome, tip) {
    const wrap = document.getElementById('map-wrap').getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = (ev.clientX - wrap.left) + 'px';
    tip.style.top = (ev.clientY - wrap.top) + 'px';
    if (mode === 'prospeccao') {
      // prospeccao ignora o filtro de municipio -> sempre mostra os componentes
      tip.innerHTML = `<b>${nome}</b><br>` + prospLines(propMun[code]);
      return;
    }
    const v = munValues[code] || { qtd: 0, valor: 0 };
    // com um municipio selecionado, os demais ficam zerados pelo filtro -> so nome
    if (munSelected && code !== munSelected) {
      tip.innerHTML = `<b>${nome}</b>`;
    } else {
      tip.innerHTML =
        `<b>${nome}</b><br>` +
        `${v.qtd.toLocaleString('pt-BR')} paradas &middot; ` +
        `R$ ${v.valor.toFixed(1)} mi`;
    }
  }

  function munNome(code) { return (munValues[String(code)] || {}).nome || ''; }

  function setMode(m) {
    mode = m;
    if (m === 'qtd' || m === 'valor') metric = m;
    recolor(); recolorMun(); renderPins();
  }
  function setCritico(on) { criticoOn = !!on; recolor(); recolorMun(); renderPins(); }
  function setPinsOpp(on) { pinsOppOn = !!on; renderPins(); }
  function setSelected(uf) { selected = uf || ''; recolor(); }
  function setMunSelected(code) { munSelected = code || ''; recolorMun(); }
  function onSelectHandler(fn) { onSelect = fn; }
  function onMunSelectHandler(fn) { onMunSelect = fn; }

  return {
    build, update, updateProspUf, updateProspMun,
    setMode, setCritico, setPinsOpp, setSelected, zoomTo, zoomReset,
    showMunicipios, clearMunicipios, updateMun, setMunSelected, munNome,
    onSelect: onSelectHandler, onMunSelect: onMunSelectHandler,
  };
})();

/* charts.js - graficos Chart.js no tema azul-escuro/ciano. */
const Charts = (() => {
  const C = {
    cyan: '#00E5FF', alert: '#FF4D5E', warn: '#FFB020', info: '#3DA9FC',
    grid: 'rgba(30,58,95,0.6)', ink: '#B9D2E4', muted: '#7FA8C9',
  };
  let chUf, chSetor, chUrg;

  function baseOpts(extra = {}) {
    return Object.assign({
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600 },
      plugins: { legend: { display: false }, tooltip: {
        backgroundColor: '#06121F', borderColor: C.cyan, borderWidth: 1,
        titleColor: C.cyan, bodyColor: C.ink, displayColors: false, padding: 8,
      } },
    }, extra);
  }

  function axis() {
    return {
      x: { grid: { color: C.grid, drawBorder: false }, ticks: { color: C.muted, font: { size: 13, family: 'ui-monospace' } } },
      y: { grid: { color: C.grid, drawBorder: false }, beginAtZero: true, ticks: { color: C.muted, font: { size: 13, family: 'ui-monospace' } } },
    };
  }

  function setup() {
    Chart.defaults.font.family = 'ui-monospace, monospace';

    chUf = new Chart(document.getElementById('chart-uf'), {
      type: 'bar',
      data: { labels: [], datasets: [{ data: [], backgroundColor: C.cyan, hoverBackgroundColor: '#7FF3FF', barThickness: 'flex' }] },
      options: baseOpts({ scales: axis() }),
    });

    chSetor = new Chart(document.getElementById('chart-setor'), {
      type: 'bar',
      data: { labels: [], datasets: [{ data: [], backgroundColor: '#0FA8C8', hoverBackgroundColor: C.cyan }] },
      options: baseOpts({ indexAxis: 'y', scales: {
        x: { grid: { color: C.grid }, beginAtZero: true, ticks: { color: C.muted, font: { size: 13 } } },
        y: { grid: { display: false }, ticks: { color: C.ink, font: { size: 13 } } },
      } }),
    });

    chUrg = new Chart(document.getElementById('chart-urgencia'), {
      type: 'doughnut',
      data: { labels: CATS.map((c) => c.label.toUpperCase()),
        datasets: [{ data: CATS.map(() => 0), backgroundColor: CATS.map((c) => c.color),
          borderColor: '#06121F', borderWidth: 2 }] },
      options: baseOpts({ cutout: '58%', plugins: {
        legend: { display: true, position: 'bottom', labels: { color: C.muted, font: { size: 12, family: 'ui-monospace' }, boxWidth: 10, padding: 8 } },
        tooltip: { backgroundColor: '#06121F', borderColor: C.cyan, borderWidth: 1, titleColor: C.cyan, bodyColor: C.ink },
      } }),
    });
  }

  function updateUf(agg, metric) {
    const vals = metric === 'valor' ? agg.valor : agg.qtd;
    chUf.data.labels = agg.labels.map((s) => s.length > 16 ? s.slice(0, 15) + '…' : s);
    chUf.data.datasets[0].data = vals;
    chUf.update();
  }
  function updateSetor(agg) {
    chSetor.data.labels = agg.labels.map((s) => s.length > 28 ? s.slice(0, 27) + '…' : s);
    chSetor.data.datasets[0].data = agg.values;
    chSetor.update();
  }
  function updateUrg(agg) {
    chUrg.data.datasets[0].data = agg.values;
    chUrg.update();
  }

  return { setup, updateUf, updateSetor, updateUrg };
})();

/* table.js - tabela de leads paginada (server-side). */
const Leads = (() => {
  let page = 1, pageSize = 50, totalPages = 1, sort = '';
  let oportFirst = true;   // "Mostrar oportunidades primeiro" (padrao ligado)
  let getFilters = () => ({});

  const fmtBRL = (v) => v == null ? '—' : 'R$ ' + Math.round(v).toLocaleString('pt-BR');

  function badge(u) {
    const c = CAT[u];
    if (!c) return `<span class="badge">${u}</span>`;
    return `<span class="badge" style="color:${c.color};background:${c.dim};border-color:${c.color}">` +
      `${c.label.toUpperCase()}</span>`;
  }

  function prazo(d) {
    if (d == null) return '<span class="muted-cell">—</span>';
    let cls, txt, frac;
    if (d < 0) { cls = 'vencido'; txt = Math.abs(d) + 'd VENCIDO'; frac = 1; }
    else if (d <= 90) { cls = 'critico'; txt = 'vence em ' + d + 'd'; frac = 1 - d / 90; }
    else { cls = 'ok'; txt = 'em ' + d + 'd'; frac = Math.max(0.08, 1 - d / 365); }
    return `<div class="prazo-wrap">
      <span class="prazo-txt ${cls}">${txt}</span>
      <span class="prazo-bar"><i class="${cls}" style="width:${Math.round(frac * 100)}%"></i></span>
    </div>`;
  }

  function esc(s) {
    return s == null ? '' : String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  }

  function skeleton() {
    const tb = document.getElementById('leads-tbody');
    let html = '';
    for (let i = 0; i < 8; i++) {
      html += '<tr>' + '<td><span class="skeleton">&nbsp;</span></td>'.repeat(8) + '</tr>';
    }
    tb.innerHTML = html;
  }

  function render(data) {
    page = data.page; totalPages = data.total_pages;
    const tb = document.getElementById('leads-tbody');
    if (!data.rows.length) {
      tb.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:32px;color:var(--muted)">NENHUM LEAD PARA OS FILTROS ATUAIS</td></tr>';
    } else {
      tb.innerHTML = data.rows.map((r) => `
        <tr class="flash">
          <td>${badge(r.urgencia)}</td>
          <td class="col-muni">${esc(r.nome_beneficiario_plano_acao) || '—'}</td>
          <td class="mono">${esc(r.uf_beneficiario_plano_acao) || '—'}</td>
          <td>${esc(r.setor) || '<span class="muted-cell">—</span>'}</td>
          <td class="col-obj">${esc((r.objeto_executor || '').slice(0, 90)) || '<span class="muted-cell">—</span>'}</td>
          <td class="right col-val">${fmtBRL(r.valor_total)}</td>
          <td>${esc(r.nome_parlamentar_emenda_plano_acao) || '—'}</td>
          <td class="right">${prazo(r.dias_para_prazo)}</td>
        </tr>`).join('');
    }
    document.getElementById('leads-total').textContent = data.total.toLocaleString('pt-BR');
    document.getElementById('pg-info').textContent = `${data.page} / ${data.total_pages}`;
    document.getElementById('pg-first').disabled = page <= 1;
    document.getElementById('pg-prev').disabled = page <= 1;
    document.getElementById('pg-next').disabled = page >= totalPages;
    document.getElementById('pg-last').disabled = page >= totalPages;
  }

  async function load() {
    skeleton();
    const ord = oportFirst ? 'oport' : sort;   // toggle vence a ordenacao por coluna
    const f = Object.assign({}, getFilters(), { page, page_size: pageSize, sort: ord });
    try {
      render(await API.leads(f));
    } catch (e) {
      document.getElementById('leads-tbody').innerHTML =
        '<tr><td colspan="8" style="text-align:center;color:var(--alert);padding:24px">FALHA AO CARREGAR LEADS</td></tr>';
    }
  }

  function goto(p) { page = Math.max(1, Math.min(p, totalPages)); load(); }
  function reset() { page = 1; }

  function init(filtersGetter) {
    getFilters = filtersGetter;
    document.getElementById('pg-first').onclick = () => goto(1);
    document.getElementById('pg-prev').onclick = () => goto(page - 1);
    document.getElementById('pg-next').onclick = () => goto(page + 1);
    document.getElementById('pg-last').onclick = () => goto(totalPages);
    document.getElementById('pg-size').onchange = (e) => { pageSize = +e.target.value; page = 1; load(); };

    const cb = document.getElementById('opp-first');
    if (cb) cb.onchange = () => {
      oportFirst = cb.checked;
      if (oportFirst) document.querySelectorAll('th[data-sort]').forEach((o) => o.classList.remove('sorted'));
      page = 1; load();
    };

    document.querySelectorAll('th[data-sort]').forEach((th) => {
      th.onclick = () => {
        // ordenar por coluna desliga "oportunidades primeiro"
        oportFirst = false;
        if (cb) cb.checked = false;
        document.querySelectorAll('th[data-sort]').forEach((o) => o.classList.remove('sorted'));
        th.classList.add('sorted');
        sort = th.dataset.sort; page = 1; load();
      };
    });
  }

  return { init, load, reset };
})();

/* filters.js - estado reativo de filtros (UF, setor, urgencia, busca). */
const Filters = (() => {
  // mun = codigo IBGE (vai p/ a API); munNome = nome de exibicao (so p/ o chip)
  const state = { uf: '', setor: '', urgencia: '', q: '', mun: '', munNome: '' };
  let onChange = () => {};
  let debounceTimer;

  const LABELS = {
    uf: 'UF', setor: 'SETOR', urgencia: 'FAIXA', q: 'BUSCA', mun: 'MUNICIPIO',
  };

  function get() { return Object.assign({}, state); }

  function syncUrl() {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(state)) if (v && k !== 'munNome') p.set(k, v);
    const qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
  }

  function renderChips() {
    const box = document.getElementById('active-filters');
    box.innerHTML = '';
    for (const [k, v] of Object.entries(state)) {
      if (!v || !(k in LABELS)) continue;  // munNome nao vira chip
      const chip = document.createElement('span');
      chip.className = 'chip';
      let disp = v;
      if (k === 'mun') disp = state.munNome || v;
      else if (k === 'urgencia') disp = (CAT[v] && CAT[v].label) || v;
      chip.innerHTML = `${LABELS[k]}: ${disp} <span class="x">×</span>`;
      chip.onclick = () => set(k, '');
      box.appendChild(chip);
    }
  }

  function emit() {
    syncUrl();
    renderChips();
    onChange(get());
  }

  function set(key, val, disp) {
    state[key] = val;
    if (key === 'uf') { state.mun = ''; state.munNome = ''; }   // trocar UF reseta municipio
    if (key === 'mun') { state.munNome = disp || ''; }
    const el = document.getElementById('f-' + key);
    if (el && el.value !== val) el.value = val;
    emit();
  }

  function fromUrl() {
    const p = new URLSearchParams(location.search);
    for (const k of Object.keys(state)) {
      const v = p.get(k);
      if (v) state[k] = v;
    }
  }

  function populate(meta) {
    fromUrl();
    const ufSel = document.getElementById('f-uf');
    meta.ufs.forEach((uf) => ufSel.add(new Option(uf, uf)));
    const setSel = document.getElementById('f-setor');
    meta.setores.forEach((s) => setSel.add(new Option(s.length > 42 ? s.slice(0, 41) + '…' : s, s)));
    for (const k of Object.keys(state)) {
      const el = document.getElementById('f-' + k);
      if (el) el.value = state[k];
    }
    renderChips();
  }

  function init(changeHandler) {
    onChange = changeHandler;
    fromUrl();

    document.getElementById('f-uf').onchange = (e) => set('uf', e.target.value);
    document.getElementById('f-setor').onchange = (e) => set('setor', e.target.value);
    document.getElementById('f-urgencia').onchange = (e) => set('urgencia', e.target.value);
    document.getElementById('f-q').oninput = (e) => {
      clearTimeout(debounceTimer);
      const v = e.target.value;
      debounceTimer = setTimeout(() => { state.q = v; emit(); }, 320);
    };
    document.getElementById('btn-reset').onclick = () => {
      Object.keys(state).forEach((k) => state[k] = '');
      document.querySelectorAll('#f-uf,#f-setor,#f-urgencia,#f-q').forEach((el) => el.value = '');
      emit();
    };
  }

  return { init, get, set, populate };
})();

/* cards.js - cards dos casos criticos do municipio selecionado, com icone
   representativo do tipo de obra (familia). */
const CardsUI = (() => {
  // 16 familias + generic. SVG inline, traco ciano, estetica quadrada.
  const ICONS = {
    saude: '<path d="M12 5v14M5 12h14"/>',
    educacao: '<path d="M3 9l9-4 9 4-9 4z"/><path d="M7 11v5c0 1.2 5 3 5 3s5-1.8 5-3v-5"/>',
    esporte: '<path d="M7 4h10v4a5 5 0 0 1-10 0z"/><path d="M7 6H4v1a3 3 0 0 0 3 3M17 6h3v1a3 3 0 0 1-3 3M10 16h4M10 16v3M14 16v3M8 21h8"/>',
    cultura: '<path d="M12 3 4 7h16zM5 9v8M9 9v8M15 9v8M19 9v8M3 21h18M4 9h16"/>',
    ciencia: '<path d="M9 3h6M10 3v6l-5 9a1 1 0 0 0 1 1.5h12a1 1 0 0 0 1-1.5l-5-9V3"/>',
    saneamento: '<path d="M12 3s6 6 6 10a6 6 0 0 1-12 0c0-4 6-10 6-10z"/>',
    ambiente: '<path d="M5 19c0-8 6-13 14-13 0 8-5 14-13 14a6 6 0 0 1-1-1z"/><path d="M5 19c3-4 7-6 10-7"/>',
    energia: '<path d="M13 2 4 14h6l-1 8 9-12h-6z"/>',
    transporte: '<path d="M3 6h11v9H3zM14 9h4l3 3v3h-7z"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/>',
    agro: '<path d="M12 21v-9M12 12c0-3 2-5 5-5 0 3-2 5-5 5zM12 12c0-3-2-5-5-5 0 3 2 5 5 5z"/>',
    seguranca: '<path d="M12 3 5 6v5c0 4 3 7 7 8 4-1 7-4 7-8V6z"/>',
    direitos: '<path d="M12 3v18M7 21h10M5 7h14M12 5 5 7l-2 6h4zM12 5l7 2 2 6h-4"/>',
    social: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 5.5A3 3 0 0 1 17 11M21 20a6 6 0 0 0-4-5.7"/>',
    turismo: '<path d="M12 21s7-7 7-12a7 7 0 0 0-14 0c0 5 7 12 7 12z"/><circle cx="12" cy="9" r="2.5"/>',
    infra_urbana: '<path d="M3 21V8l6-3v16M9 21V3l8 3v15M3 21h18M12 9h2M12 13h2M12 17h2"/>',
    trabalho: '<path d="M3 8h18v11H3zM8 8V5h8v3M3 13h18"/>',
    generic: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
  };

  function icon(fam) {
    const inner = ICONS[fam] || ICONS.generic;
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="square" stroke-linejoin="miter">${inner}</svg>`;
  }

  const esc = (s) => s == null ? '' : String(s).replace(/[&<>]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  const fmtBRL = (v) => v == null ? '—' : 'R$ ' + Math.round(v).toLocaleString('pt-BR');

  function prazoTxt(d) {
    if (d == null) return ['—', ''];
    if (d < 0) return [Math.abs(d) + 'd VENCIDO', 'vencido'];
    return ['vence em ' + d + 'd', 'critico'];
  }

  function cardHtml(r) {
    const [pz, cls] = prazoTxt(r.dias_para_prazo);
    const cat = CAT[r.urgencia] || CAT.CRITICO;
    return `<article class="card" style="border-left-color:${cat.color}">
      <div class="card-top">
        <span class="card-icon">${icon(r.familia)}</span>
        <span class="card-setor">${esc(r.setor_principal || r.setor)}</span>
        <span class="badge" style="color:${cat.color};background:${cat.dim};border-color:${cat.color}">${cat.label.toUpperCase()}</span>
      </div>
      <p class="card-obj">${esc(r.objeto || '—')}</p>
      <div class="card-meta">
        <div><span class="card-k">VALOR</span><span class="card-v">${fmtBRL(r.valor_total)}</span></div>
        <div><span class="card-k">PRAZO</span><span class="card-v prazo-txt ${cls}">${pz}</span></div>
        <div><span class="card-k">FIM PREVISTO</span><span class="card-v">${esc(r.data_fim || '—')}</span></div>
        <div><span class="card-k">PARLAMENTAR</span><span class="card-v">${esc(r.parlamentar || '—')}</span></div>
      </div>
    </article>`;
  }

  function render(data) {
    const sec = document.getElementById('cards-section');
    const grid = document.getElementById('cards-grid');
    const title = document.getElementById('cards-title');
    sec.classList.add('open');
    title.innerHTML = `CASOS &middot; <span>${esc(data.municipio)}</span> &middot; ` +
      `${data.total.toLocaleString('pt-BR')} ${data.total === 1 ? 'CASO' : 'CASOS'}` +
      (data.capped ? ` (mostrando ${data.rows.length})` : '');
    if (!data.rows.length) {
      grid.innerHTML = '<div class="cards-empty mono">NENHUM CASO ACIONAVEL NESTE MUNICIPIO</div>';
    } else {
      grid.innerHTML = data.rows.map(cardHtml).join('');
    }
  }

  function hide() {
    const sec = document.getElementById('cards-section');
    if (sec) sec.classList.remove('open');
  }

  async function load(f) {
    if (!f.mun) { hide(); return; }
    try { render(await API.cards(f)); }
    catch (e) { hide(); }
  }

  function init(onClose) {
    const btn = document.getElementById('cards-close');
    if (btn) btn.onclick = onClose;
  }

  return { load, hide, init };
})();

/* main.js - orquestracao: boot, refresh reativo, auto-refresh e relogio. */
(() => {
  let mapMode = 'prospeccao';   // 'prospeccao' (default) | 'qtd' | 'valor'
  let autoTimer = null;

  function tickClock() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    document.getElementById('clock').textContent =
      `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  // badge no botao FILTROS (qtd de filtros ativos) + ponto no botao BUSCA
  function updateFilterBadges(f) {
    const n = ['uf', 'setor', 'urgencia'].filter((k) => f[k]).length;
    const badge = document.getElementById('filtros-count');
    if (badge) { badge.hidden = n === 0; badge.textContent = n; }
    const dot = document.getElementById('busca-dot');
    if (dot) dot.hidden = !f.q;
  }

  // popovers de FILTROS e BUSCA: abrem abaixo do botao, fecham fora/Esc
  function setupPopovers() {
    const items = [['btn-filtros', 'pop-filtros'], ['btn-busca', 'pop-busca']];
    function closeAll(except) {
      items.forEach(([bid, pid]) => {
        if (pid === except) return;
        const pop = document.getElementById(pid), btn = document.getElementById(bid);
        if (pop) pop.hidden = true;
        if (btn) btn.setAttribute('aria-expanded', 'false');
      });
    }
    items.forEach(([bid, pid]) => {
      const btn = document.getElementById(bid), pop = document.getElementById(pid);
      if (!btn || !pop) return;
      btn.onclick = (e) => {
        e.stopPropagation();
        const willOpen = pop.hidden;
        closeAll(pid);
        pop.hidden = !willOpen;
        btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        if (willOpen && pid === 'pop-busca') {
          const inp = document.getElementById('f-q'); if (inp) inp.focus();
        }
      };
      pop.addEventListener('click', (e) => e.stopPropagation());
    });
    document.addEventListener('click', () => closeAll(null));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAll(null); });
  }

  // zoom do mapa + camada de municipios + botao de retorno conforme a UF.
  // so (re)desenha os municipios quando a UF realmente muda (clicar em um
  // municipio nao redesenha a malha, so recolore via refresh).
  let drawnUf = '';
  function aplicarZoom(uf) {
    const btn = document.getElementById('map-reset');
    if (uf) {
      if (uf !== drawnUf) { BrMap.zoomTo(uf); BrMap.showMunicipios(uf); }
      if (btn) btn.hidden = false;
    } else {
      BrMap.zoomReset();
      BrMap.clearMunicipios();   // sempre limpa ao voltar pro Brasil
      if (btn) btn.hidden = true;
    }
    drawnUf = uf;
  }

  async function refresh() {
    const f = Filters.get();
    try {
      const [k, aUf, aSetor, aUrg] = await Promise.all([
        API.kpis(f), API.aggUf(f), API.aggSetor(f), API.aggUrgencia(f),
      ]);
      Counters.updateKpis(k);
      Charts.updateSetor(aSetor);
      Charts.updateUrg(aUrg);

      const aMun = f.uf ? await API.aggMunicipio(f) : null;

      // ----- coloracao do mapa conforme o modo -----
      if (mapMode === 'prospeccao') {
        const [pu, pm] = await Promise.all([API.prospUf(f), API.prospMun(f)]);
        BrMap.updateProspUf(pu);
        BrMap.updateProspMun(pm);
      } else {
        BrMap.update(aUf);
        if (aMun) BrMap.updateMun(aMun);
      }

      // ----- grafico de barras (independe do modo do mapa) -----
      const tituloUf = document.getElementById('titulo-uf');
      if (f.uf && aMun) {
        const top = {
          labels: aMun.labels.slice(0, 12),
          qtd: aMun.qtd.slice(0, 12),
          valor: aMun.valor.slice(0, 12),
        };
        Charts.updateUf(top, 'qtd');
        if (tituloUf) tituloUf.textContent = 'MUNICIPIOS · ' + f.uf;
      } else {
        Charts.updateUf(aUf, 'qtd');
        if (tituloUf) tituloUf.textContent = 'EMENDAS PARADAS POR UF';
      }
    } catch (e) {
      console.error('refresh falhou', e);
    }
    CardsUI.load(f);   // cards do municipio selecionado (ou esconde)
    Leads.reset();
    Leads.load();
  }

  function setAuto(on) {
    const btn = document.getElementById('btn-auto');
    const dot = document.getElementById('live-dot');
    const lbl = document.getElementById('live-label');
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (on) {
      dot.classList.remove('paused'); lbl.classList.remove('paused'); lbl.textContent = 'LIVE';
      autoTimer = setInterval(refresh, 20000);
    } else {
      dot.classList.add('paused'); lbl.classList.add('paused'); lbl.textContent = 'PAUSED';
      clearInterval(autoTimer); autoTimer = null;
    }
  }

  function showError(meta) {
    document.getElementById('app').hidden = true;
    const scr = document.getElementById('error-screen');
    scr.hidden = false;
    document.getElementById('error-msg').textContent =
      meta.erro || 'Os CSVs ainda nao foram gerados.';
    if (meta.comando_extracao)
      document.getElementById('error-cmd').textContent = meta.comando_extracao;
  }

  async function boot() {
    Radar.start();
    tickClock();
    setInterval(tickClock, 1000);

    let meta;
    try {
      meta = await API.meta();
    } catch (e) {
      meta = { data_ok: false, erro: 'Servidor nao respondeu /api/meta.' };
    }

    if (!meta.data_ok) {
      showError(meta);
      document.getElementById('btn-retry').onclick = async () => {
        const m = await API.reload();
        if (m.data_ok) location.reload(); else showError(m);
      };
      return;
    }

    document.getElementById('error-screen').hidden = true;
    document.getElementById('app').hidden = false;
    if (meta.gerado_em)
      document.getElementById('load-stamp').textContent = 'CARGA: ' + meta.gerado_em;

    Charts.setup();
    await BrMap.build();
    Filters.populate(meta);
    aplicarZoom(Filters.get().uf);  // zoom inicial se veio UF pela URL

    BrMap.onSelect((uf) => { BrMap.setSelected(uf); Filters.set('uf', uf); });
    BrMap.onMunSelect((code) => {
      BrMap.setMunSelected(code);
      Filters.set('mun', code, code ? BrMap.munNome(code) : '');
    });

    const mapReset = document.getElementById('map-reset');
    if (mapReset) mapReset.onclick = () => Filters.set('uf', '');

    const prospTools = document.getElementById('prosp-tools');
    document.querySelectorAll('.map-toggle button').forEach((b) => {
      b.onclick = () => {
        document.querySelectorAll('.map-toggle button').forEach((o) => o.classList.remove('active'));
        b.classList.add('active');
        mapMode = b.dataset.metric;
        if (prospTools) prospTools.hidden = (mapMode !== 'prospeccao');
        BrMap.setMode(mapMode);
        refresh();
      };
    });

    // toggles da visao prospeccao
    const btnPinsOpp = document.getElementById('btn-pins-opp');
    if (btnPinsOpp) btnPinsOpp.onclick = () => {
      const on = btnPinsOpp.getAttribute('aria-pressed') !== 'true';
      btnPinsOpp.setAttribute('aria-pressed', on ? 'true' : 'false');
      BrMap.setPinsOpp(on);
    };
    const btnCritico = document.getElementById('btn-critico');
    if (btnCritico) btnCritico.onclick = () => {
      const on = btnCritico.getAttribute('aria-pressed') !== 'true';
      btnCritico.setAttribute('aria-pressed', on ? 'true' : 'false');
      BrMap.setCritico(on);
    };

    Filters.init((f) => {
      BrMap.setSelected(f.uf);
      BrMap.setMunSelected(f.mun);
      aplicarZoom(f.uf);
      updateFilterBadges(f);
      refresh();
    });
    CardsUI.init(() => { BrMap.setMunSelected(''); Filters.set('mun', ''); });
    Leads.init(Filters.get);
    setupPopovers();
    updateFilterBadges(Filters.get());
    document.getElementById('btn-auto').onclick = () =>
      setAuto(document.getElementById('btn-auto').getAttribute('aria-pressed') !== 'true');

    await refresh();
  }

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
"""

_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UruTracker &middot; Radar de Emenda Pix Parada</title>
<link rel="icon" href="data:image/svg+xml,&lt;svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'&gt;&lt;rect width='16' height='16' fill='%2306121F'/&gt;&lt;rect x='6' y='6' width='4' height='4' fill='%2300E5FF'/&gt;&lt;rect x='2' y='2' width='12' height='12' fill='none' stroke='%2300B8D4'/&gt;&lt;/svg&gt;">
<style>__CSS__</style>
</head>
<body>

<header class="app-header">
  <canvas id="radar-canvas" class="radar-bg" aria-hidden="true"></canvas>
  <div class="header-grid"></div>
  <div class="header-content">
    <div class="brand">
      <img class="brand-logo" src="__LOGO__" alt="UruTracker"
           onerror="this.style.display='none';document.getElementById('brand-mark').style.display='grid'">
      <div class="brand-mark" id="brand-mark" aria-hidden="true" style="display:none">
        <span class="mark-ring"></span><span class="mark-core"></span>
      </div>
      <div class="brand-text">
        <h1>URU<span class="accent">TRACKER</span></h1>
        <p class="subtitle">RADAR DE EMENDA PIX PARADA &middot; PROSPECCAO DE OBRAS PUBLICAS</p>
      </div>
    </div>
    <div class="header-status">
      <div class="status-line">
        <span id="live-dot" class="live-dot"></span>
        <span id="live-label" class="mono">LIVE</span>
      </div>
      <div class="status-meta mono">
        <span>FONTE: TRANSFEREGOV &middot; TRANSFERENCIAS ESPECIAIS</span>
        <span id="load-stamp">CARGA: &mdash;</span>
        <span id="clock">--:--:--</span>
      </div>
    </div>
  </div>
</header>

<div id="error-screen" class="error-screen" hidden>
  <div class="error-box">
    <div class="error-tag mono">DADOS AUSENTES</div>
    <h2>Nenhum dado carregado</h2>
    <p id="error-msg">Os CSVs ainda nao foram gerados.</p>
    <p class="error-hint">Rode o extrator na raiz do projeto e recarregue:</p>
    <code class="error-cmd mono" id="error-cmd">python data_extraction/extrair_dados.py</code>
    <button id="btn-retry" class="btn">RECARREGAR DADOS</button>
  </div>
</div>

<main id="app" class="app" hidden>

  <section class="toolbar">
    <div class="tb-item" data-pop>
      <button id="btn-filtros" class="btn btn-ghost" aria-expanded="false">FILTROS <span id="filtros-count" class="tb-badge" hidden>0</span> &#9662;</button>
      <div id="pop-filtros" class="popover" hidden>
        <div class="field">
          <label class="mono" for="f-uf">UF</label>
          <select id="f-uf"><option value="">TODAS</option></select>
        </div>
        <div class="field">
          <label class="mono" for="f-setor">SETOR</label>
          <select id="f-setor"><option value="">TODOS OS SETORES</option></select>
        </div>
        <div class="field">
          <label class="mono" for="f-urgencia">FAIXA</label>
          <select id="f-urgencia">
            <option value="">TODAS</option>
            <option value="ANDAMENTO">PROJETO EM ANDAMENTO</option>
            <option value="POSSIVEL">POSSIVEL OPORTUNIDADE</option>
            <option value="CRITICO">PRAZO CRITICO</option>
            <option value="OPORTUNIDADE">OPORTUNIDADE</option>
            <option value="ESTAGNADO">RECEM ESTAGNADA</option>
            <option value="ABANDONADO">DORMENTE</option>
          </select>
        </div>
      </div>
    </div>
    <div class="tb-item" data-pop>
      <button id="btn-busca" class="btn btn-ghost" aria-expanded="false">BUSCA <span id="busca-dot" class="tb-dot" hidden></span> &#9662;</button>
      <div id="pop-busca" class="popover popover-busca" hidden>
        <div class="field">
          <label class="mono" for="f-q">BUSCA</label>
          <input id="f-q" type="text" placeholder="municipio, objeto, parlamentar..." autocomplete="off">
        </div>
      </div>
    </div>
    <button id="btn-reset" class="btn btn-ghost">LIMPAR</button>
    <button id="btn-auto" class="btn btn-toggle" aria-pressed="false">AUTO-REFRESH</button>
    <div id="active-filters" class="active-filters"></div>
  </section>

  <section class="kpi-grid" id="kpi-grid">
    <article class="kpi kpi-alert kpi-valor" data-kpi="emendas_paradas">
      <div class="kpi-head"><span class="kpi-label mono">EMENDAS PARADAS</span><span class="kpi-dot"></span></div>
      <div class="kpi-value mono" data-count>0</div>
      <div class="kpi-desc mono">Total em todas as faixas paradas</div>
      <div class="kpi-spark"></div>
    </article>
    <article class="kpi kpi-alert kpi-valor kpi-money" data-kpi="valor_total" data-format="brl">
      <div class="kpi-head"><span class="kpi-label mono">VALOR TOTAL PARADO</span><span class="kpi-dot"></span></div>
      <div class="kpi-value mono" data-count>0</div>
      <div class="kpi-desc mono">Soma de todas as faixas paradas</div>
      <div class="kpi-spark"></div>
    </article>
    <article class="kpi kpi-green kpi-money" data-kpi="valor_oportunidade" data-format="brl">
      <div class="kpi-head"><span class="kpi-label mono">VALOR DE OPORTUNIDADE PARADO</span><span class="kpi-dot"></span></div>
      <div class="kpi-value mono" data-count>0</div>
      <div class="kpi-desc mono">Oportunidade + recem estagnadas</div>
      <div class="kpi-spark"></div>
    </article>
    <article class="kpi kpi-green" data-kpi="oport_split">
      <div class="kpi-head"><span class="kpi-label mono">OPORTUNIDADES &amp; RECEM ESTAGNADAS</span><span class="kpi-dot"></span></div>
      <div class="kpi-split">
        <div><span class="kpi-split-v mono" data-count data-key="qtd_oportunidade">0</span><span class="kpi-split-k mono">OPORTUNIDADES</span></div>
        <div><span class="kpi-split-v mono" data-count data-key="qtd_estagnado">0</span><span class="kpi-split-k mono">RECEM ESTAGNADAS</span></div>
      </div>
      <div class="kpi-desc mono">Vencidas ha ate 90d / 90 a 180d</div>
      <div class="kpi-spark"></div>
    </article>
    <article class="kpi kpi-green" data-kpi="municipios_oport">
      <div class="kpi-head"><span class="kpi-label mono">MUNICIPIOS C/ OPORTUNIDADE</span><span class="kpi-dot"></span></div>
      <div class="kpi-value mono" data-count>0</div>
      <div class="kpi-desc mono">Com oportunidade ou recem estagnada</div>
      <div class="kpi-spark"></div>
    </article>
    <article class="kpi kpi-dorm" data-kpi="abandonado">
      <div class="kpi-head"><span class="kpi-label mono">PROJETOS DORMENTES</span><span class="kpi-dot"></span></div>
      <div class="kpi-value mono" data-count>0</div>
      <div class="kpi-desc mono">Vencidas ha mais de 180 dias</div>
      <div class="kpi-spark"></div>
    </article>
    <article class="kpi kpi-warn" data-kpi="prazo_critico">
      <div class="kpi-head"><span class="kpi-label mono">EM PRAZO CRITICO</span><span class="kpi-dot"></span></div>
      <div class="kpi-value mono" data-count>0</div>
      <div class="kpi-desc mono">Vencem em ate 90 dias</div>
      <div class="kpi-spark"></div>
    </article>
    <article class="kpi kpi-info" data-kpi="possivel_andamento">
      <div class="kpi-head"><span class="kpi-label mono">POSSIVEIS + EM ANDAMENTO</span><span class="kpi-dot"></span></div>
      <div class="kpi-value mono" data-count>0</div>
      <div class="kpi-desc mono">Vencem em mais de 90 dias</div>
      <div class="kpi-spark"></div>
    </article>
  </section>

  <section class="grid-main">
    <div class="panel panel-map">
      <div class="panel-head">
        <h3 class="mono" id="titulo-mapa">VARREDURA GEOGRAFICA &middot; BRASIL</h3>
        <div class="map-head-tools">
          <button id="map-reset" class="btn btn-ghost" hidden>&larr; BRASIL</button>
          <div id="prosp-tools" class="prosp-tools mono">
            <button id="btn-pins-opp" class="prosp-pin" aria-pressed="true" title="Pins de oportunidade + recem estagnada">&#9679; PINS OPORT.</button>
            <button id="btn-critico" class="prosp-crit" aria-pressed="false" title="Inclui prazo critico no calor, hover e pins">&#9679; PRAZO CRITICO</button>
          </div>
          <div class="map-toggle mono">
            <button data-metric="prospeccao" class="prosp-mode active">PROSPECCAO</button>
            <button data-metric="qtd">QTD</button>
            <button data-metric="valor">VALOR</button>
          </div>
        </div>
      </div>
      <div class="panel-body map-body">
        <div id="map-wrap" class="map-wrap">
          <svg id="brazil-map" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet"></svg>
          <div id="map-tooltip" class="map-tooltip mono" hidden></div>
        </div>
        <div class="map-legend mono" id="map-legend"></div>
      </div>
    </div>

    <div class="charts-wrap">
      <div class="panel panel-chart">
        <div class="panel-head"><h3 class="mono" id="titulo-uf">EMENDAS PARADAS POR UF</h3></div>
        <div class="panel-body"><canvas id="chart-uf"></canvas></div>
      </div>

      <div class="panel panel-chart">
        <div class="panel-head"><h3 class="mono">DISTRIBUICAO POR FAIXA</h3></div>
        <div class="panel-body"><canvas id="chart-urgencia"></canvas></div>
      </div>

      <div class="panel panel-chart panel-wide">
        <div class="panel-head"><h3 class="mono">TOP 15 SETORES COM EMENDAS PARADAS</h3></div>
        <div class="panel-body"><canvas id="chart-setor"></canvas></div>
      </div>

      <!-- cards dos casos criticos: slide-over absoluto sobre os graficos -->
      <section id="cards-section" class="cards-section">
        <div class="cards-head">
          <h3 class="mono" id="cards-title">CASOS CRITICOS</h3>
          <button id="cards-close" class="btn btn-ghost" title="Fechar">FECHAR &times;</button>
        </div>
        <div id="cards-grid" class="cards-grid"></div>
      </section>
    </div>
  </section>

  <section class="panel panel-table">
    <div class="panel-head">
      <h3 class="mono">TABELA DE LEADS &middot; <span id="leads-total">0</span> OPORTUNIDADES</h3>
      <label class="opp-toggle mono">
        <input type="checkbox" id="opp-first" checked> MOSTRAR OPORTUNIDADES PRIMEIRO
      </label>
      <div class="pager mono">
        <button id="pg-first" title="Primeira">&#171;</button>
        <button id="pg-prev" title="Anterior">&#8249;</button>
        <span id="pg-info">&mdash; / &mdash;</span>
        <button id="pg-next" title="Proxima">&#8250;</button>
        <button id="pg-last" title="Ultima">&#187;</button>
        <span class="pg-sep">|</span>
        <label>LINHAS
          <select id="pg-size">
            <option>50</option><option>100</option><option>200</option>
          </select>
        </label>
      </div>
    </div>
    <div class="panel-body table-body">
      <div class="table-scroll">
        <table class="leads">
          <thead>
            <tr>
              <th data-sort="urgencia">URGENCIA</th>
              <th data-sort="municipio">MUNICIPIO</th>
              <th data-sort="uf">UF</th>
              <th>SETOR</th>
              <th>OBJETO</th>
              <th class="right" data-sort="valor">VALOR</th>
              <th>PARLAMENTAR</th>
              <th class="right" data-sort="prazo">PRAZO</th>
            </tr>
          </thead>
          <tbody id="leads-tbody"></tbody>
        </table>
      </div>
    </div>
  </section>

  <footer class="app-footer mono">
    URUTRACKER &middot; DADOS PUBLICOS TRANSFEREGOV &middot; USO LOCAL &mdash; RODANDO COM DADOS DE data_extraction/
  </footer>
</main>

<script src="/vendor/chart.js"></script>
<script>__JS__</script>
</body>
</html>"""

PAGE = _HTML.replace("__CSS__", _CSS).replace("__JS__", _JS).replace("__LOGO__", LOGO_URL)

# ===========================================================================
# SERVIDOR
# ===========================================================================

HOST = "127.0.0.1"
PORT = 5000

# Assets de terceiros buscados via CDN e servidos por /vendor/<nome>.
# ponytail: cache em memoria, baixa 1x por execucao. Cache em disco so se a
# latencia de boot incomodar.
_VENDOR = {
    "chart.js": (
        "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js",
        "application/javascript",
    ),
    "brazil.geojson": (
        "https://cdn.jsdelivr.net/gh/codeforgermany/click_that_hood@main/public/data/brazil-states.geojson",
        "application/json",
    ),
}
_vendor_cache: dict[str, bytes] = {}

app = Flask(__name__)
app.json.ensure_ascii = False  # preserva acentos no JSON (utf-8)


@app.get("/")
def index():
    return PAGE


@app.get("/vendor/<path:name>")
def vendor(name):
    if name not in _VENDOR:
        return ("vendor desconhecido", 404)
    if name not in _vendor_cache:
        url, _ctype = _VENDOR[name]
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                _vendor_cache[name] = resp.read()
        except Exception as exc:  # offline / CDN fora do ar
            return (f"falha ao buscar vendor '{name}': {exc}", 502)
    return app.response_class(_vendor_cache[name], mimetype=_VENDOR[name][1])


_mun_cache: dict[str, bytes] = {}


@app.get("/vendor/mun/<uf>")
def vendor_mun(uf):
    """GeoJSON municipal de uma UF (malha tbrugz/geodata-br via CDN, cacheado)."""
    sigla = uf.upper().replace(".JSON", "").replace(".GEOJSON", "")
    cod = UF_COD.get(sigla)
    if not cod:
        return ("UF desconhecida", 404)
    if sigla not in _mun_cache:
        url = (
            "https://cdn.jsdelivr.net/gh/tbrugz/geodata-br@master/"
            f"geojson/geojs-{cod}-mun.json"
        )
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                _mun_cache[sigla] = resp.read()
        except Exception as exc:  # offline / CDN fora do ar
            return (f"falha ao buscar municipios de {sigla}: {exc}", 502)
    return app.response_class(_mun_cache[sigla], mimetype="application/json")


@app.get("/api/meta")
def api_meta():
    return jsonify(meta())


@app.get("/api/kpis")
def api_kpis():
    return jsonify(kpis(request.args))


@app.get("/api/leads")
def api_leads():
    return jsonify(leads_pagina(request.args))


@app.get("/api/agg/uf")
def api_agg_uf():
    return jsonify(agg_uf(request.args))


@app.get("/api/agg/setor")
def api_agg_setor():
    return jsonify(agg_setor(request.args))


@app.get("/api/agg/municipio")
def api_agg_municipio():
    return jsonify(agg_municipio(request.args))


@app.get("/api/prospeccao/uf")
def api_prospeccao_uf():
    return jsonify(agg_prospeccao_uf(request.args))


@app.get("/api/prospeccao/mun")
def api_prospeccao_mun():
    return jsonify(agg_prospeccao_mun(request.args))


@app.get("/api/cards")
def api_cards():
    return jsonify(cards(request.args))


@app.get("/api/agg/urgencia")
def api_agg_urgencia():
    return jsonify(agg_urgencia(request.args))


@app.post("/api/reload")
def api_reload():
    """Recarrega os CSVs sem reiniciar o servidor."""
    build_dataframe()
    return jsonify(meta())


def _abrir_navegador() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


def main() -> None:
    print("[uru] Carregando dados de data_extraction/ ...")
    build_dataframe()
    if STORE.data_ok:
        n = len(STORE.df)
        print(f"[uru] OK - {n:,} emendas paradas carregadas.".replace(",", "."))
    else:
        print(f"[uru] AVISO - {STORE.erro}")
        print("[uru] O app vai subir e mostrar instrucoes na tela.")

    if not os.environ.get("URU_NO_BROWSER"):
        threading.Timer(1.0, _abrir_navegador).start()

    print(f"[uru] Servindo em http://{HOST}:{PORT}  (Ctrl+C para encerrar)")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


# ===========================================================================
# MAPEAMENTO DE MUNICIPIOS  (gerado de municipios.csv + municipios_matching.csv)
# ---------------------------------------------------------------------------
# Tabela estatica e volumosa, mantida no rodape para nao poluir o resto do
# codigo. Chave: "NOME DO BENEFICIARIO|UF" (maiusculas).  Valor: (codigo IBGE
# de 7 digitos, nome de exibicao). Usado em build_dataframe() para anexar
# cod_ibge/municipio_exib a cada emenda — e o que casa os dados com o GeoJSON
# municipal desenhado no mapa.
# ===========================================================================

_MUNI_IBGE = {
    "ABREU E LIMA|PE": ("2600054", "Abreu e Lima"),
    "ACARAPE|CE": ("2300150", "Acarape"),
    "ADOLFO|SP": ("3500204", "Adolfo"),
    "AGRESTINA|PE": ("2600302", "Agrestina"),
    "AGUIAR|PB": ("2500205", "Aguiar"),
    "ALAGOINHA DO PIAUÍ|PI": ("2200251", "Alagoinha do Piauí"),
    "ALDEIAS ALTAS|MA": ("2100303", "Aldeias Altas"),
    "ALFREDO MARCONDES|SP": ("3500808", "Alfredo Marcondes"),
    "ALTAIR|SP": ("3500907", "Altair"),
    "ALTO ALEGRE DO MARANHÃO|MA": ("2100436", "Alto Alegre do Maranhão"),
    "ALTO ALEGRE|RR": ("1400050", "Alto Alegre"),
    "ALTO ALEGRE|RS": ("4300554", "Alto Alegre"),
    "ALTO ALEGRE|SP": ("3501103", "Alto Alegre"),
    "ALTO GARÇAS|MT": ("5100409", "Alto Garças"),
    "ALTO PARNAÍBA|MA": ("2100501", "Alto Parnaíba"),
    "ALVINLÂNDIA|SP": ("3501509", "Alvinlândia"),
    "ALVORADA DO NORTE|GO": ("5200803", "Alvorada do Norte"),
    "ALVORADA DO SUL|PR": ("4100806", "Alvorada do Sul"),
    "AMARAJI|PE": ("2600906", "Amaraji"),
    "AMARAL FERRADOR|RS": ("4300638", "Amaral Ferrador"),
    "AMONTADA|CE": ("2300754", "Amontada"),
    "AMPARO|PB": ("2500734", "Amparo"),
    "AMPARO|SP": ("3501905", "Amparo"),
    "AMÉLIA RODRIGUES|BA": ("2901106", "Amélia Rodrigues"),
    "ANAMÃ|AM": ("1300086", "Anamã"),
    "ANAPURUS|MA": ("2100808", "Anapurus"),
    "ANGRA DOS REIS|RJ": ("3300100", "Angra dos Reis"),
    "ANTA GORDA|RS": ("4300703", "Anta Gorda"),
    "ANTONINA DO NORTE|CE": ("2300804", "Antonina do Norte"),
    "ANTÔNIO ALMEIDA|PI": ("2200806", "Antônio Almeida"),
    "ANTÔNIO GONÇALVES|BA": ("2901809", "Antônio Gonçalves"),
    "ANTÔNIO MARTINS|RN": ("2400901", "Antônio Martins"),
    "APORÉ|GO": ("5201504", "Aporé"),
    "APUAREMA|BA": ("2901957", "Apuarema"),
    "APUÍ|AM": ("1300144", "Apuí"),
    "ARACITABA|MG": ("3103306", "Aracitaba"),
    "ARAME|MA": ("2100956", "Arame"),
    "ARAPIRACA|AL": ("2700300", "Arapiraca"),
    "ARARUAMA|RJ": ("3300209", "Araruama"),
    "ARATUÍPE|BA": ("2902302", "Aratuípe"),
    "ARAÇAS|BA": ("2902054", "Araçás"),
    "ARAÇU|GO": ("5201603", "Araçu"),
    "AREALVA|SP": ("3503406", "Arealva"),
    "AREIAL|PB": ("2501203", "Areial"),
    "ARENÓPOLIS|GO": ("5202353", "Arenópolis"),
    "ARIPUANÃ|MT": ("5101407", "Aripuanã"),
    "ARNEIROZ|CE": ("2301505", "Arneiroz"),
    "ATALAIA DO NORTE|AM": ("1300201", "Atalaia do Norte"),
    "ATALAIA|AL": ("2700409", "Atalaia"),
    "ATALAIA|PR": ("4102208", "Atalaia"),
    "AURORA|CE": ("2301703", "Aurora"),
    "AURORA|SC": ("4201901", "Aurora"),
    "BAIXIO|CE": ("2301802", "Baixio"),
    "BALIZA|GO": ("5203104", "Baliza"),
    "BALSAS|MA": ("2101400", "Balsas"),
    "BARAÚNA|PB": ("2501534", "Baraúna"),
    "BARAÚNA|RN": ("2401453", "Baraúna"),
    "BARRA DA ESTIVA|BA": ("2902807", "Barra da Estiva"),
    "BARRA DE GUABIRABA|PE": ("2601300", "Barra de Guabiraba"),
    "BARRA DE SANTA ROSA|PB": ("2501609", "Barra de Santa Rosa"),
    "BARRA DE SÃO MIGUEL|AL": ("2700607", "Barra de São Miguel"),
    "BARRA DE SÃO MIGUEL|PB": ("2501708", "Barra de São Miguel"),
    "BARRACÃO|PR": ("4102604", "Barracão"),
    "BARRACÃO|RS": ("4301800", "Barracão"),
    "BARREIRAS DO PIAUÍ|PI": ("2201309", "Barreiras do Piauí"),
    "BARREIRA|CE": ("2301950", "Barreira"),
    "BARREIRINHA|AM": ("1300508", "Barreirinha"),
    "BARROQUINHA|CE": ("2302057", "Barroquinha"),
    "BARRO|CE": ("2302008", "Barro"),
    "BATALHA|AL": ("2700706", "Batalha"),
    "BATALHA|PI": ("2201507", "Batalha"),
    "BAÍA DA TRAIÇÃO|PB": ("2501401", "Baía da Traição"),
    "BELA VISTA DO PIAUÍ|PI": ("2201556", "Bela Vista do Piauí"),
    "BELFORD ROXO|RJ": ("3300456", "Belford Roxo"),
    "BELMONTE|BA": ("2903409", "Belmonte"),
    "BELMONTE|SC": ("4202156", "Belmonte"),
    "BELO JARDIM|PE": ("2601706", "Belo Jardim"),
    "BELÉM|AL": ("2700805", "Belém"),
    "BELÉM|PA": ("1501402", "Belém"),
    "BELÉM|PB": ("2501906", "Belém"),
    "BENEDITINOS|PI": ("2201606", "Beneditinos"),
    "BENEDITO LEITE|MA": ("2101806", "Benedito Leite"),
    "BENTO DE ABREU|SP": ("3506201", "Bento de Abreu"),
    "BERURI|AM": ("1300631", "Beruri"),
    "BOA NOVA|BA": ("2903706", "Boa Nova"),
    "BOA VENTURA|PB": ("2502102", "Boa Ventura"),
    "BOA VISTA DO GURUPI|MA": ("2101970", "Boa Vista do Gurupi"),
    "BOCA DA MATA|AL": ("2701001", "Boca da Mata"),
    "BOCA DO ACRE|AM": ("1300706", "Boca do Acre"),
    "BODÓ|RN": ("2401651", "Bodó"),
    "BOM PRINCÍPIO DO PIAUÍ|PI": ("2201919", "Bom Princípio do Piauí"),
    "BOQUEIRÃO DO LEÃO|RS": ("4302451", "Boqueirão do Leão"),
    "BOQUEIRÃO DO PIAUÍ|PI": ("2201945", "Boqueirão do Piauí"),
    "BORBOREMA|PB": ("2502706", "Borborema"),
    "BORBOREMA|SP": ("3507407", "Borborema"),
    "BOREBI|SP": ("3507456", "Borebi"),
    "BRASILÂNDIA DO SUL|PR": ("4103370", "Brasilândia do Sul"),
    "BRASNORTE|MT": ("5101902", "Brasnorte"),
    "BREJO DO PIAUÍ|PI": ("2201988", "Brejo do Piauí"),
    "BRODOWSKI|SP": ("3507803", "Brodowski"),
    "BRUSQUE|SC": ("4202909", "Brusque"),
    "BUERAREMA|BA": ("2904704", "Buerarema"),
    "BURITAMA|SP": ("3508108", "Buritama"),
    "BURITI BRAVO|MA": ("2102309", "Buriti Bravo"),
    "BURITIRAMA|BA": ("2904753", "Buritirama"),
    "CAAPIRANGA|AM": ("1300839", "Caapiranga"),
    "CAAPORÃ|PB": ("2503001", "Caaporã"),
    "CACHOEIRA ALTA|GO": ("5204102", "Cachoeira Alta"),
    "CACHOEIRA DA PRATA|MG": ("3109600", "Cachoeira da Prata"),
    "CACHOEIRA DO ARARI|PA": ("1502004", "Cachoeira do Arari"),
    "CACHOEIRINHA|PE": ("2603108", "Cachoeirinha"),
    "CACHOEIRINHA|RS": ("4303103", "Cachoeirinha"),
    "CACHOEIRINHA|TO": ("1703826", "Cachoeirinha"),
    "CACIMBA DE AREIA|PB": ("2503407", "Cacimba de Areia"),
    "CACONDE|SP": ("3508702", "Caconde"),
    "CAFARNAUM|BA": ("2905305", "Cafarnaum"),
    "CAJATI|SP": ("3509254", "Cajati"),
    "CAJAZEIRINHAS|PB": ("2503753", "Cajazeirinhas"),
    "CAJUEIRO|AL": ("2701308", "Cajueiro"),
    "CALDAS BRANDÃO|PB": ("2503803", "Caldas Brandão"),
    "CALDEIRÃO GRANDE DO PIAUÍ|PI": ("2202091", "Caldeirão Grande do Piauí"),
    "CAMAÇARI|BA": ("2905701", "Camaçari"),
    "CAMBARÁ DO SUL|RS": ("4303608", "Cambará do Sul"),
    "CAMBIRA|PR": ("4103800", "Cambira"),
    "CAMPINA DO MONTE ALEGRE|SP": ("3509452", "Campina do Monte Alegre"),
    "CAMPO ALEGRE DO FIDALGO|PI": ("2202117", "Campo Alegre do Fidalgo"),
    "CAMPO ALEGRE|AL": ("2701407", "Campo Alegre"),
    "CAMPO ALEGRE|SC": ("4203303", "Campo Alegre"),
    "CAMPO BELO|MG": ("3111200", "Campo Belo"),
    "CAMUTANGA|PE": ("2603603", "Camutanga"),
    "CANABRAVA DO NORTE|MT": ("5102694", "Canabrava do Norte"),
    "CANAVIEIRA|PI": ("2202251", "Canavieira"),
    "CANAÃ DOS CARAJÁS|PA": ("1502152", "Canaã dos Carajás"),
    "CANTAGALO|MG": ("3112059", "Cantagalo"),
    "CANTAGALO|PR": ("4104451", "Cantagalo"),
    "CANTAGALO|RJ": ("3301108", "Cantagalo"),
    "CANTO DO BURITI|PI": ("2202307", "Canto do Buriti"),
    "CANUTAMA|AM": ("1300904", "Canutama"),
    "CANÁPOLIS|BA": ("2906105", "Canápolis"),
    "CANÁPOLIS|MG": ("3111804", "Canápolis"),
    "CAPETINGA|MG": ("3112406", "Capetinga"),
    "CAPISTRANO|CE": ("2302909", "Capistrano"),
    "CAPOEIRAS|PE": ("2603801", "Capoeiras"),
    "CAPUTIRA|MG": ("3112901", "Caputira"),
    "CAPÃO BONITO DO SUL|RS": ("4304622", "Capão Bonito do Sul"),
    "CARAÚBAS DO PIAUÍ|PI": ("2202539", "Caraúbas do Piauí"),
    "CARIDADE|CE": ("2303006", "Caridade"),
    "CARIÚS|CE": ("2303303", "Cariús"),
    "CARLOS CHAGAS|MG": ("3113701", "Carlos Chagas"),
    "CARNAÚBA DOS DANTAS|RN": ("2402402", "Carnaúba dos Dantas"),
    "CARRAPATEIRA|PB": ("2504108", "Carrapateira"),
    "CASA GRANDE|MG": ("3114907", "Casa Grande"),
    "CASCALHO RICO|MG": ("3115003", "Cascalho Rico"),
    "CASSERENGUE|PB": ("2504157", "Casserengue"),
    "CASTANHEIRA|MT": ("5102850", "Castanheira"),
    "CATOLÉ DO ROCHA|PB": ("2504306", "Catolé do Rocha"),
    "CAUCAIA|CE": ("2303709", "Caucaia"),
    "CAXIAS|MA": ("2103000", "Caxias"),
    "CAXINGÓ|PI": ("2202653", "Caxingó"),
    "CEDRAL|MA": ("2103109", "Cedral"),
    "CEDRAL|SP": ("3511300", "Cedral"),
    "CEDRO|CE": ("2303808", "Cedro"),
    "CEDRO|PE": ("2604304", "Cedro"),
    "CESÁRIO LANGE|SP": ("3511607", "Cesário Lange"),
    "CHAPADA DOS GUIMARÃES|MT": ("5103007", "Chapada dos Guimarães"),
    "CHARQUEADA|SP": ("3511706", "Charqueada"),
    "CHAVAL|CE": ("2303907", "Chaval"),
    "CHIAPETTA|RS": ("4305405", "Chiapetta"),
    "CHORÓ|CE": ("2303931", "Choró"),
    "COARI|AM": ("1301209", "Coari"),
    "COCAL DOS ALVES|PI": ("2202729", "Cocal dos Alves"),
    "COCALINHO|MT": ("5103106", "Cocalinho"),
    "COITÉ DO NÓIA|AL": ("2702009", "Coité do Nóia"),
    "COLÍDER|MT": ("5103205", "Colíder"),
    "COLÔNIA DO GURGUEIA|PI": ("2202752", "Colônia do Gurguéia"),
    "COLÔNIA LEOPOLDINA|AL": ("2702108", "Colônia Leopoldina"),
    "COMENDADOR LEVY GASPARIAN|RJ": ("3300951", "Comendador Levy Gasparian"),
    "CONCEIÇÃO DA FEIRA|BA": ("2908200", "Conceição da Feira"),
    "CONCEIÇÃO DO CANINDÉ|PI": ("2202802", "Conceição do Canindé"),
    "CONCEIÇÃO DO LAGO-AÇU|MA": ("2103554", "Conceição do Lago-Açu"),
    "CONCÓRDIA|SC": ("4204301", "Concórdia"),
    "CONDADO|PB": ("2504504", "Condado"),
    "CONDADO|PE": ("2604601", "Condado"),
    "CONGONHAS|MG": ("3118007", "Congonhas"),
    "COQUEIRO SECO|AL": ("2702207", "Coqueiro Seco"),
    "COREMAS|PB": ("2504801", "Coremas"),
    "CORONEL DOMINGOS SOARES|PR": ("4106456", "Coronel Domingos Soares"),
    "CORONEL MARTINS|SC": ("4204459", "Coronel Martins"),
    "CORURIPE|AL": ("2702306", "Coruripe"),
    "COSTA RICA|MS": ("5003256", "Costa Rica"),
    "COXIXOLA|PB": ("2504850", "Coxixola"),
    "CRAÍBAS|AL": ("2702355", "Craíbas"),
    "CRUZMALTINA|PR": ("4106852", "Cruzmaltina"),
    "CRUZ|CE": ("2304251", "Cruz"),
    "CUMARU|PE": ("2604908", "Cumaru"),
    "CURRAL VELHO|PB": ("2505303", "Curral Velho"),
    "CURUÁ|PA": ("1502855", "Curuá"),
    "CURVELÂNDIA|MT": ("5103437", "Curvelândia"),
    "CÁSSIA DOS COQUEIROS|SP": ("3510906", "Cássia dos Coqueiros"),
    "CÁSSIA|MG": ("3115102", "Cássia"),
    "CÂNDIDO MENDES|MA": ("2102606", "Cândido Mendes"),
    "DAVID CANABARRO|RS": ("4306304", "David Canabarro"),
    "DEODÁPOLIS|MS": ("5003454", "Deodápolis"),
    "DESTERRO|PB": ("2505402", "Desterro"),
    "DIAMANTE|PB": ("2505600", "Diamante"),
    "DIVINOLÂNDIA DE MINAS|MG": ("3122207", "Divinolândia de Minas"),
    "DIVINÉSIA|MG": ("3121902", "Divinésia"),
    "DOIS IRMÃOS DAS MISSÕES|RS": ("4306429", "Dois Irmãos das Missões"),
    "DOIS IRMÃOS|RS": ("4306403", "Dois Irmãos"),
    "DOM AQUINO|MT": ("5103601", "Dom Aquino"),
    "DOM CAVATI|MG": ("3122504", "Dom Cavati"),
    "DOM MACEDO COSTA|BA": ("2910206", "Dom Macedo Costa"),
    "DOMINGOS MOURÃO|PI": ("2203420", "Domingos Mourão"),
    "DONA FRANCISCA|RS": ("4306700", "Dona Francisca"),
    "DOUTOR SEVERIANO|RN": ("2403202", "Doutor Severiano"),
    "DUQUE BACELAR|MA": ("2103901", "Duque Bacelar"),
    "ECHAPORÃ|SP": ("3514700", "Echaporã"),
    "ECOPORANGA|ES": ("3202108", "Ecoporanga"),
    "ELÍSIO MEDRADO|BA": ("2910305", "Elísio Medrado"),
    "EMAS|PB": ("2505907", "Emas"),
    "EMILIANÓPOLIS|SP": ("3515129", "Emilianópolis"),
    "ENCANTO|RN": ("2403301", "Encanto"),
    "ENTRE RIOS DO OESTE|PR": ("4107538", "Entre Rios do Oeste"),
    "ENVIRA|AM": ("1301506", "Envira"),
    "EREBANGO|RS": ("4306973", "Erebango"),
    "ERERÊ|CE": ("2304277", "Ereré"),
    "ESMERALDAS|MG": ("3124104", "Esmeraldas"),
    "ESPERANÇA DO SUL|RS": ("4307450", "Esperança do Sul"),
    "ESPERANÇA NOVA|PR": ("4107520", "Esperança Nova"),
    "ESTIVA GERBI|SP": ("3557303", "Estiva Gerbi"),
    "ESTREITO|MA": ("2104057", "Estreito"),
    "ESTRELA DE ALAGOAS|AL": ("2702553", "Estrela de Alagoas"),
    "ESTRELA VELHA|RS": ("4307815", "Estrela Velha"),
    "FAGUNDES|PB": ("2506103", "Fagundes"),
    "FARIAS BRITO|CE": ("2304301", "Farias Brito"),
    "FAZENDA VILANOVA|RS": ("4308078", "Fazenda Vilanova"),
    "FEIRA GRANDE|AL": ("2702603", "Feira Grande"),
    "FERVEDOURO|MG": ("3125952", "Fervedouro"),
    "FIGUEIRÓPOLIS D'OESTE|MT": ("5103809", "Figueirópolis D'Oeste"),
    "FLEXEIRAS|AL": ("2702801", "Flexeiras"),
    "FLOREAL|SP": ("3515905", "Floreal"),
    "FLORES DE GOIÁS|GO": ("5207907", "Flores de Goiás"),
    "FLORIANÓPOLIS|SC": ("4205407", "Florianópolis"),
    "FRANCISCÓPOLIS|MG": ("3126752", "Franciscópolis"),
    "GADO BRAVO|PB": ("2506251", "Gado Bravo"),
    "GAMELEIRA|PE": ("2605905", "Gameleira"),
    "GAVIÃO PEIXOTO|SP": ("3516853", "Gavião Peixoto"),
    "GENERAL MAYNARD|SE": ("2802502", "General Maynard"),
    "GENERAL SALGADO|SP": ("3516903", "General Salgado"),
    "GENERAL SAMPAIO|CE": ("2304608", "General Sampaio"),
    "GENTIL|RS": ("4308854", "Gentil"),
    "GENTIO DO OURO|BA": ("2911303", "Gentio do Ouro"),
    "GOIANINHA|RN": ("2404200", "Goianinha"),
    "GONÇALVES|MG": ("3127404", "Gonçalves"),
    "GOVERNADOR NEWTON BELLO|MA": ("2104651", "Governador Newton Bello"),
    "GOVERNADOR NUNES FREIRE|MA": ("2104677", "Governador Nunes Freire"),
    "GRUPIARA|MG": ("3127909", "Grupiara"),
    "GUABIJU|RS": ("4309258", "Guabiju"),
    "GUAIÚBA|CE": ("2304954", "Guaiúba"),
    "GUARANTÃ|SP": ("3518107", "Guarantã"),
    "GUARAREMA|SP": ("3518305", "Guararema"),
    "GUIA LOPES DA LAGUNA|MS": ("5004106", "Guia Lopes da Laguna"),
    "GURUPÁ|PA": ("1503101", "Gurupá"),
    "HIDROLÂNDIA|CE": ("2305209", "Hidrolândia"),
    "HIDROLÂNDIA|GO": ("5209705", "Hidrolândia"),
    "HUGO NAPOLEÃO|PI": ("2204600", "Hugo Napoleão"),
    "IBARETAMA|CE": ("2305266", "Ibaretama"),
    "IBATEGUARA|AL": ("2703007", "Ibateguara"),
    "IBATÉ|SP": ("3519303", "Ibaté"),
    "IBERTIOGA|MG": ("3129400", "Ibertioga"),
    "IBIARA|PB": ("2506608", "Ibiara"),
    "IBICOARA|BA": ("2912202", "Ibicoara"),
    "IBIRAÇU|ES": ("3202504", "Ibiraçu"),
    "ICAPUÍ|CE": ("2305357", "Icapuí"),
    "IELMO MARINHO|RN": ("2404606", "Ielmo Marinho"),
    "IGARACY|PB": ("2502607", "Igaracy"),
    "IGARAPÉ DO MEIO|MA": ("2105153", "Igarapé do Meio"),
    "IGARAPÉ GRANDE|MA": ("2105203", "Igarapé Grande"),
    "IGREJA NOVA|AL": ("2703205", "Igreja Nova"),
    "IGUAÍ|BA": ("2913507", "Iguaí"),
    "ILHABELA|SP": ("3520400", "Ilhabela"),
    "IPIRANGA DO SUL|RS": ("4310462", "Ipiranga do Sul"),
    "IPUEIRAS|CE": ("2305902", "Ipueiras"),
    "IPUEIRAS|TO": ("1709807", "Ipueiras"),
    "IRAPURU|SP": ("3521606", "Irapuru"),
    "ITAARA|RS": ("4310538", "Itaara"),
    "ITABAIANA|PB": ("2506905", "Itabaiana"),
    "ITABAIANA|SE": ("2802908", "Itabaiana"),
    "ITAGUARI|GO": ("5210562", "Itaguari"),
    "ITAJU|SP": ("3522000", "Itaju"),
    "ITANHANGÁ|MT": ("5104542", "Itanhangá"),
    "ITAPERUNA|RJ": ("3302205", "Itaperuna"),
    "ITAPIRANGA|AM": ("1302009", "Itapiranga"),
    "ITAPIRANGA|SC": ("4208401", "Itapiranga"),
    "ITAPORANGA D'AJUDA|SE": ("2803203", "Itaporanga d'Ajuda"),
    "ITAPORANGA|PB": ("2507002", "Itaporanga"),
    "ITAPORANGA|SP": ("3522802", "Itaporanga"),
    "ITAPUCA|RS": ("4310579", "Itapuca"),
    "ITARANTIM|BA": ("2916807", "Itarantim"),
    "ITATIAIA|RJ": ("3302254", "Itatiaia"),
    "ITATIAIUÇU|MG": ("3133709", "Itatiaiuçu"),
    "ITAÍBA|PE": ("2607505", "Itaíba"),
    "ITIRAPUÃ|SP": ("3523701", "Itirapuã"),
    "IUIÚ|BA": ("2917334", "Iuiu"),
    "JABOTICABA|RS": ("4310850", "Jaboticaba"),
    "JACARACI|BA": ("2917409", "Jacaraci"),
    "JACARAÚ|PB": ("2507309", "Jacaraú"),
    "JACARÉ DOS HOMENS|AL": ("2703403", "Jacaré dos Homens"),
    "JACI|SP": ("3524501", "Jaci"),
    "JAPARATUBA|SE": ("2803302", "Japaratuba"),
    "JAPARAÍBA|MG": ("3135308", "Japaraíba"),
    "JAPIRA|PR": ("4112306", "Japira"),
    "JAPOATÃ|SE": ("2803401", "Japoatã"),
    "JARAGUARI|MS": ("5004908", "Jaraguari"),
    "JARAMATAIA|AL": ("2703700", "Jaramataia"),
    "JARDIM DE ANGICOS|RN": ("2405504", "Jardim de Angicos"),
    "JARINU|SP": ("3525201", "Jarinu"),
    "JAUPACI|GO": ("5212006", "Jaupaci"),
    "JAURU|MT": ("5105002", "Jauru"),
    "JENIPAPO DOS VIEIRAS|MA": ("2105476", "Jenipapo dos Vieiras"),
    "JIJOCA DE JERICOACOARA|CE": ("2307254", "Jijoca de Jericoacoara"),
    "JOAQUIM NABUCO|PE": ("2608206", "Joaquim Nabuco"),
    "JOAQUIM PIRES|PI": ("2205409", "Joaquim Pires"),
    "JOAQUIM TÁVORA|PR": ("4112801", "Joaquim Távora"),
    "JOCA CLAUDINO|PB": ("2513653", "Joca Claudino"),
    "JOSELÂNDIA|MA": ("2105609", "Joselândia"),
    "JOSÉ BONIFÁCIO|SP": ("3525706", "José Bonifácio"),
    "JOSÉ DA PENHA|RN": ("2406007", "José da Penha"),
    "JOSÉ GONÇALVES DE MINAS|MG": ("3136520", "José Gonçalves de Minas"),
    "JUARA|MT": ("5105101", "Juara"),
    "JUAZEIRO DO PIAUÍ|PI": ("2205516", "Juazeiro do Piauí"),
    "JUCATI|PE": ("2608255", "Jucati"),
    "JUNCO DO MARANHÃO|MA": ("2105658", "Junco do Maranhão"),
    "JUNCO DO SERIDÓ|PB": ("2507804", "Junco do Seridó"),
    "JUNDIÁ|AL": ("2703908", "Jundiá"),
    "JUNDIÁ|RN": ("2406155", "Jundiá"),
    "JUPI|PE": ("2608305", "Jupi"),
    "JURU|PB": ("2508000", "Juru"),
    "JUSSARA|BA": ("2918506", "Jussara"),
    "JUSSARA|GO": ("5212204", "Jussara"),
    "JUSSARA|PR": ("4113007", "Jussara"),
    "LAGO VERDE|MA": ("2105906", "Lago Verde"),
    "LAGOA DA CANOA|AL": ("2704104", "Lagoa da Canoa"),
    "LAGOA DE DENTRO|PB": ("2508208", "Lagoa de Dentro"),
    "LAGOA DE ITAENGA|PE": ("2608503", "Lagoa de Itaenga"),
    "LAGOA DE PEDRAS|RN": ("2406304", "Lagoa de Pedras"),
    "LAGOA DE SÃO FRANCISCO|PI": ("2205573", "Lagoa de São Francisco"),
    "LAGOA DO BARRO DO PIAUÍ|PI": ("2205565", "Lagoa do Barro do Piauí"),
    "LAGOA DO MATO|MA": ("2105922", "Lagoa do Mato"),
    "LAGOA DOS GATOS|PE": ("2608701", "Lagoa dos Gatos"),
    "LAGOA NOVA|RN": ("2406502", "Lagoa Nova"),
    "LAGOÃO|RS": ("4311254", "Lagoão"),
    "LAGUNA CARAPÃ|MS": ("5005251", "Laguna Carapã"),
    "LAJE DO MURIAÉ|RJ": ("3302304", "Laje do Muriaé"),
    "LAJEADO GRANDE|SC": ("4209458", "Lajeado Grande"),
    "LAJEADO NOVO|MA": ("2105989", "Lajeado Novo"),
    "LASTRO|PB": ("2508406", "Lastro"),
    "LIBERATO SALZANO|RS": ("4311601", "Liberato Salzano"),
    "LIMOEIRO DE ANADIA|AL": ("2704203", "Limoeiro de Anadia"),
    "LIMOEIRO DO AJURU|PA": ("1504000", "Limoeiro do Ajuru"),
    "LINDÓIA|SP": ("3527009", "Lindóia"),
    "LINHA NOVA|RS": ("4311643", "Linha Nova"),
    "LINHARES|ES": ("3203205", "Linhares"),
    "LIVRAMENTO|PB": ("2508505", "Livramento"),
    "LORETO|MA": ("2106102", "Loreto"),
    "LOURDES|SP": ("3527256", "Lourdes"),
    "LUCAS DO RIO VERDE|MT": ("5105259", "Lucas do Rio Verde"),
    "LUTÉCIA|SP": ("3527900", "Lutécia"),
    "LUZILÂNDIA|PI": ("2205805", "Luzilândia"),
    "LUÍS DOMINGUES|MA": ("2106201", "Luís Domingues"),
    "MACUCO|RJ": ("3302452", "Macuco"),
    "MAFRA|SC": ("4210100", "Mafra"),
    "MAGALHÃES BARATA|PA": ("1504109", "Magalhães Barata"),
    "MAIRIPOTABA|GO": ("5212600", "Mairipotaba"),
    "MAJOR ISIDORO|AL": ("2704401", "Major Isidoro"),
    "MALACACHETA|MG": ("3139201", "Malacacheta"),
    "MAMANGUAPE|PB": ("2508901", "Mamanguape"),
    "MANAQUIRI|AM": ("1302553", "Manaquiri"),
    "MANSIDÃO|BA": ("2920452", "Mansidão"),
    "MAQUINÉ|RS": ("4311775", "Maquiné"),
    "MARACAÇUMÉ|MA": ("2106326", "Maracaçumé"),
    "MARATAÍZES|ES": ("3203320", "Marataízes"),
    "MARCELINO VIEIRA|RN": ("2407302", "Marcelino Vieira"),
    "MARCIONÍLIO SOUZA|BA": ("2920809", "Marcionílio Souza"),
    "MARCOS PARENTE|PI": ("2206001", "Marcos Parente"),
    "MARIANA PIMENTEL|RS": ("4311981", "Mariana Pimentel"),
    "MARIBONDO|AL": ("2704807", "Maribondo"),
    "MARILÂNDIA DO SUL|PR": ("4114906", "Marilândia do Sul"),
    "MARINÓPOLIS|SP": ("3529104", "Marinópolis"),
    "MARIPÁ DE MINAS|MG": ("3140209", "Maripá de Minas"),
    "MARI|PB": ("2509107", "Mari"),
    "MATA|RS": ("4312104", "Mata"),
    "MATINHA|MA": ("2106508", "Matinha"),
    "MATO LEITÃO|RS": ("4312153", "Mato Leitão"),
    "MATÕES|MA": ("2106607", "Matões"),
    "MAXIMILIANO DE ALMEIDA|RS": ("4312203", "Maximiliano de Almeida"),
    "MESQUITA|MG": ("3141702", "Mesquita"),
    "MESQUITA|RJ": ("3302858", "Mesquita"),
    "MILAGRES DO MARANHÃO|MA": ("2106672", "Milagres do Maranhão"),
    "MINADOR DO NEGRÃO|AL": ("2705309", "Minador do Negrão"),
    "MIRACATU|SP": ("3529906", "Miracatu"),
    "MIRADOR|MA": ("2106706", "Mirador"),
    "MIRADOR|PR": ("4115903", "Mirador"),
    "MOCAJUBA|PA": ("1504604", "Mocajuba"),
    "MOGEIRO|PB": ("2509404", "Mogeiro"),
    "MONTAURI|RS": ("4312351", "Montauri"),
    "MONTE FORMOSO|MG": ("3143153", "Monte Formoso"),
    "MONTE HOREBE|PB": ("2509602", "Monte Horebe"),
    "MONÇÃO|MA": ("2106904", "Monção"),
    "MORRO DA GARÇA|MG": ("3143609", "Morro da Garça"),
    "MORRO DO CHAPÉU|BA": ("2921708", "Morro do Chapéu"),
    "MORRO GRANDE|SC": ("4211256", "Morro Grande"),
    "MORROS|MA": ("2107100", "Morros"),
    "MORTUGABA|BA": ("2921807", "Mortugaba"),
    "MULUNGU DO MORRO|BA": ("2922052", "Mulungu do Morro"),
    "MULUNGU|CE": ("2309102", "Mulungu"),
    "MULUNGU|PB": ("2509800", "Mulungu"),
    "MUNICIPIO DA ALIANCA|PE": ("2600708", "Aliança"),
    "MUNICIPIO DA ESTANCIA TURISTICA DE IBITINGA|SP": ("3519600", "Ibitinga"),
    "MUNICIPIO DA ESTANCIA TURISTICA DE OLIMPIA|SP": ("3533908", "Olímpia"),
    "MUNICIPIO DA LAPA|PR": ("4113205", "Lapa"),
    "MUNICIPIO DA SERRA|ES": ("3205002", "Serra"),
    "MUNICIPIO DE ABADIA DE GOIAS|GO": ("5200050", "Abadia de Goiás"),
    "MUNICIPIO DE ABADIA DOS DOURADOS|MG": ("3100104", "Abadia dos Dourados"),
    "MUNICIPIO DE ABADIANIA|GO": ("5200100", "Abadiânia"),
    "MUNICIPIO DE ABAETETUBA|PA": ("1500107", "Abaetetuba"),
    "MUNICIPIO DE ABAETE|MG": ("3100203", "Abaeté"),
    "MUNICIPIO DE ABAIARA|CE": ("2300101", "Abaiara"),
    "MUNICIPIO DE ABAIRA|BA": ("2900108", "Abaíra"),
    "MUNICIPIO DE ABARE|BA": ("2900207", "Abaré"),
    "MUNICIPIO DE ABDON BATISTA|SC": ("4200051", "Abdon Batista"),
    "MUNICIPIO DE ABELARDO LUZ|SC": ("4200101", "Abelardo Luz"),
    "MUNICIPIO DE ABRE CAMPO|MG": ("3100302", "Abre Campo"),
    "MUNICIPIO DE ABREULANDIA|TO": ("1700251", "Abreulândia"),
    "MUNICIPIO DE ACAIACA|MG": ("3100401", "Acaiaca"),
    "MUNICIPIO DE ACAILANDIA|MA": ("2100055", "Açailândia"),
    "MUNICIPIO DE ACAJUTIBA|BA": ("2900306", "Acajutiba"),
    "MUNICIPIO DE ACARAU|CE": ("2300200", "Acaraú"),
    "MUNICIPIO DE ACARA|PA": ("1500206", "Acará"),
    "MUNICIPIO DE ACARI|RN": ("2400109", "Acari"),
    "MUNICIPIO DE ACAUA|PI": ("2200053", "Acauã"),
    "MUNICIPIO DE ACEGUA|RS": ("4300034", "Aceguá"),
    "MUNICIPIO DE ACOPIARA|CE": ("2300309", "Acopiara"),
    "MUNICIPIO DE ACORIZAL|MT": ("5100102", "Acorizal"),
    "MUNICIPIO DE ACRELANDIA|AC": ("1200013", "Acrelândia"),
    "MUNICIPIO DE ACREUNA|GO": ("5200134", "Acreúna"),
    "MUNICIPIO DE ACUCENA|MG": ("3100500", "Açucena"),
    "MUNICIPIO DE ADELANDIA|GO": ("5200159", "Adelândia"),
    "MUNICIPIO DE ADRIANOPOLIS|PR": ("4100202", "Adrianópolis"),
    "MUNICIPIO DE ADUSTINA|BA": ("2900355", "Adustina"),
    "MUNICIPIO DE AFOGADOS DA INGAZEIRA|PE": ("2600104", "Afogados da Ingazeira"),
    "MUNICIPIO DE AFONSO BEZERRA|RN": ("2400307", "Afonso Bezerra"),
    "MUNICIPIO DE AFONSO CLAUDIO|ES": ("3200102", "Afonso Cláudio"),
    "MUNICIPIO DE AFONSO CUNHA|MA": ("2100105", "Afonso Cunha"),
    "MUNICIPIO DE AFRANIO|PE": ("2600203", "Afrânio"),
    "MUNICIPIO DE AFUA|PA": ("1500305", "Afuá"),
    "MUNICIPIO DE AGRICOLANDIA|PI": ("2200103", "Agricolândia"),
    "MUNICIPIO DE AGROLANDIA|SC": ("4200200", "Agrolândia"),
    "MUNICIPIO DE AGRONOMICA|SC": ("4200309", "Agronômica"),
    "MUNICIPIO DE AGUA AZUL DO NORTE|PA": ("1500347", "Água Azul do Norte"),
    "MUNICIPIO DE AGUA BOA|MG": ("3100609", "Água Boa"),
    "MUNICIPIO DE AGUA BOA|MT": ("5100201", "Água Boa"),
    "MUNICIPIO DE AGUA BRANCA|AL": ("2700102", "Água Branca"),
    "MUNICIPIO DE AGUA BRANCA|PB": ("2500106", "Água Branca"),
    "MUNICIPIO DE AGUA BRANCA|PI": ("2200202", "Água Branca"),
    "MUNICIPIO DE AGUA CLARA|MS": ("5000203", "Água Clara"),
    "MUNICIPIO DE AGUA COMPRIDA|MG": ("3100708", "Água Comprida"),
    "MUNICIPIO DE AGUA DOCE DO MARANHAO|MA": ("2100154", "Água Doce do Maranhão"),
    "MUNICIPIO DE AGUA DOCE DO NORTE|ES": ("3200169", "Água Doce do Norte"),
    "MUNICIPIO DE AGUA DOCE|SC": ("4200408", "Água Doce"),
    "MUNICIPIO DE AGUA FRIA DE GOIAS|GO": ("5200175", "Água Fria de Goiás"),
    "MUNICIPIO DE AGUA LIMPA|GO": ("5200209", "Água Limpa"),
    "MUNICIPIO DE AGUA NOVA|RN": ("2400406", "Água Nova"),
    "MUNICIPIO DE AGUA PRETA|PE": ("2600401", "Água Preta"),
    "MUNICIPIO DE AGUA SANTA|RS": ("4300059", "Água Santa"),
    "MUNICIPIO DE AGUAI|SP": ("3500303", "Aguaí"),
    "MUNICIPIO DE AGUANIL|MG": ("3100807", "Aguanil"),
    "MUNICIPIO DE AGUAS BELAS|PE": ("2600500", "Águas Belas"),
    "MUNICIPIO DE AGUAS DA PRATA|SP": ("3500402", "Águas da Prata"),
    "MUNICIPIO DE AGUAS DE CHAPECO|SC": ("4200507", "Águas de Chapecó"),
    "MUNICIPIO DE AGUAS DE LINDOIA|SP": ("3500501", "Águas de Lindóia"),
    "MUNICIPIO DE AGUAS DE SAO PEDRO|SP": ("3500600", "Águas de São Pedro"),
    "MUNICIPIO DE AGUAS FORMOSAS|MG": ("3100906", "Águas Formosas"),
    "MUNICIPIO DE AGUAS FRIAS|SC": ("4200556", "Águas Frias"),
    "MUNICIPIO DE AGUAS LINDAS DE GOIAS|GO": ("5200258", "Águas Lindas de Goiás"),
    "MUNICIPIO DE AGUAS MORNAS|SC": ("4200606", "Águas Mornas"),
    "MUNICIPIO DE AGUAS VERMELHAS|MG": ("3101003", "Águas Vermelhas"),
    "MUNICIPIO DE AGUDOS DO SUL|PR": ("4100301", "Agudos do Sul"),
    "MUNICIPIO DE AGUDOS|SP": ("3500709", "Agudos"),
    "MUNICIPIO DE AGUDO|RS": ("4300109", "Agudo"),
    "MUNICIPIO DE AGUIA BRANCA|ES": ("3200136", "Águia Branca"),
    "MUNICIPIO DE AGUIARNOPOLIS|TO": ("1700301", "Aguiarnópolis"),
    "MUNICIPIO DE AIMORES|MG": ("3101102", "Aimorés"),
    "MUNICIPIO DE AIQUARA|BA": ("2900603", "Aiquara"),
    "MUNICIPIO DE AIUABA|CE": ("2300408", "Aiuaba"),
    "MUNICIPIO DE AIURUOCA|MG": ("3101201", "Aiuruoca"),
    "MUNICIPIO DE AJURICABA|RS": ("4300208", "Ajuricaba"),
    "MUNICIPIO DE ALAGOA GRANDE|PB": ("2500304", "Alagoa Grande"),
    "MUNICIPIO DE ALAGOA NOVA|PB": ("2500403", "Alagoa Nova"),
    "MUNICIPIO DE ALAGOA|MG": ("3101300", "Alagoa"),
    "MUNICIPIO DE ALAGOINHAS|BA": ("2900702", "Alagoinhas"),
    "MUNICIPIO DE ALAGOINHA|PB": ("2500502", "Alagoinha"),
    "MUNICIPIO DE ALAGOINHA|PE": ("2600609", "Alagoinha"),
    "MUNICIPIO DE ALAMBARI|SP": ("3500758", "Alambari"),
    "MUNICIPIO DE ALBERTINA|MG": ("3101409", "Albertina"),
    "MUNICIPIO DE ALCANTARAS|CE": ("2300507", "Alcântaras"),
    "MUNICIPIO DE ALCANTARA|MA": ("2100204", "Alcântara"),
    "MUNICIPIO DE ALCANTIL|PB": ("2500536", "Alcantil"),
    "MUNICIPIO DE ALCINOPOLIS|MS": ("5000252", "Alcinópolis"),
    "MUNICIPIO DE ALCOBACA|BA": ("2900801", "Alcobaça"),
    "MUNICIPIO DE ALECRIM|RS": ("4300307", "Alecrim"),
    "MUNICIPIO DE ALEGRETE DO PIAUI|PI": ("2200277", "Alegrete do Piauí"),
    "MUNICIPIO DE ALEGRETE|RS": ("4300406", "Alegrete"),
    "MUNICIPIO DE ALEGRE|ES": ("3200201", "Alegre"),
    "MUNICIPIO DE ALEGRIA|RS": ("4300455", "Alegria"),
    "MUNICIPIO DE ALEM PARAIBA|MG": ("3101508", "Além Paraíba"),
    "MUNICIPIO DE ALENQUER|PA": ("1500404", "Alenquer"),
    "MUNICIPIO DE ALEXANDRIA|RN": ("2400505", "Alexandria"),
    "MUNICIPIO DE ALEXANIA|GO": ("5200308", "Alexânia"),
    "MUNICIPIO DE ALFENAS|MG": ("3101607", "Alfenas"),
    "MUNICIPIO DE ALFREDO CHAVES|ES": ("3200300", "Alfredo Chaves"),
    "MUNICIPIO DE ALFREDO VASCONCELOS|MG": ("3101631", "Alfredo Vasconcelos"),
    "MUNICIPIO DE ALFREDO WAGNER|SC": ("4200705", "Alfredo Wagner"),
    "MUNICIPIO DE ALGODAO DE JANDAIRA|PB": ("2500577", "Algodão de Jandaíra"),
    "MUNICIPIO DE ALHANDRA|PB": ("2500601", "Alhandra"),
    "MUNICIPIO DE ALIANCA DO TOCANTINS|TO": ("1700350", "Aliança do Tocantins"),
    "MUNICIPIO DE ALMADINA|BA": ("2900900", "Almadina"),
    "MUNICIPIO DE ALMAS|TO": ("1700400", "Almas"),
    "MUNICIPIO DE ALMEIRIM|PA": ("1500503", "Almeirim"),
    "MUNICIPIO DE ALMENARA|MG": ("3101706", "Almenara"),
    "MUNICIPIO DE ALMINO AFONSO|RN": ("2400604", "Almino Afonso"),
    "MUNICIPIO DE ALMIRANTE TAMANDARE DO SUL|RS": ("4300471", "Almirante Tamandaré do Sul"),
    "MUNICIPIO DE ALMIRANTE TAMANDARE|PR": ("4100400", "Almirante Tamandaré"),
    "MUNICIPIO DE ALOANDIA|GO": ("5200506", "Aloândia"),
    "MUNICIPIO DE ALPERCATA|MG": ("3101805", "Alpercata"),
    "MUNICIPIO DE ALPESTRE|RS": ("4300505", "Alpestre"),
    "MUNICIPIO DE ALPINOPOLIS|MG": ("3101904", "Alpinópolis"),
    "MUNICIPIO DE ALTA FLORESTA D'OESTE|RO": ("1100015", "Alta Floresta D'Oeste"),
    "MUNICIPIO DE ALTA FLORESTA|MT": ("5100250", "Alta Floresta"),
    "MUNICIPIO DE ALTAMIRA DO MARANHAO|MA": ("2100402", "Altamira do Maranhão"),
    "MUNICIPIO DE ALTAMIRA DO PARANA|PR": ("4100459", "Altamira do Paraná"),
    "MUNICIPIO DE ALTAMIRA|PA": ("1500602", "Altamira"),
    "MUNICIPIO DE ALTANEIRA|CE": ("2300606", "Altaneira"),
    "MUNICIPIO DE ALTEROSA|MG": ("3102001", "Alterosa"),
    "MUNICIPIO DE ALTINHO|PE": ("2600807", "Altinho"),
    "MUNICIPIO DE ALTINOPOLIS|SP": ("3501004", "Altinópolis"),
    "MUNICIPIO DE ALTO ALEGRE DO PINDARE|MA": ("2100477", "Alto Alegre do Pindaré"),
    "MUNICIPIO DE ALTO ALEGRE DOS PARECIS|RO": ("1100379", "Alto Alegre dos Parecis"),
    "MUNICIPIO DE ALTO ALEGRE|RR": ("1400050", "Alto Alegre"),
    "MUNICIPIO DE ALTO ALEGRE|RS": ("4300554", "Alto Alegre"),
    "MUNICIPIO DE ALTO ALEGRE|SP": ("3501103", "Alto Alegre"),
    "MUNICIPIO DE ALTO ARAGUAIA|MT": ("5100300", "Alto Araguaia"),
    "MUNICIPIO DE ALTO BELA VISTA|SC": ("4200754", "Alto Bela Vista"),
    "MUNICIPIO DE ALTO BOA VISTA|MT": ("5100359", "Alto Boa Vista"),
    "MUNICIPIO DE ALTO CAPARAO|MG": ("3102050", "Alto Caparaó"),
    "MUNICIPIO DE ALTO DO RODRIGUES|RN": ("2400703", "Alto do Rodrigues"),
    "MUNICIPIO DE ALTO FELIZ|RS": ("4300570", "Alto Feliz"),
    "MUNICIPIO DE ALTO HORIZONTE|GO": ("5200555", "Alto Horizonte"),
    "MUNICIPIO DE ALTO JEQUITIBA|MG": ("3153509", "Alto Jequitibá"),
    "MUNICIPIO DE ALTO LONGA|PI": ("2200301", "Alto Longá"),
    "MUNICIPIO DE ALTO PARAGUAI|MT": ("5100508", "Alto Paraguai"),
    "MUNICIPIO DE ALTO PARAISO DE GOIAS|GO": ("5200605", "Alto Paraíso de Goiás"),
    "MUNICIPIO DE ALTO PARAISO|PR": ("4128625", "Alto Paraíso"),
    "MUNICIPIO DE ALTO PARAISO|RO": ("1100403", "Alto Paraíso"),
    "MUNICIPIO DE ALTO PARANA|PR": ("4100608", "Alto Paraná"),
    "MUNICIPIO DE ALTO PIQUIRI|PR": ("4100707", "Alto Piquiri"),
    "MUNICIPIO DE ALTO RIO DOCE|MG": ("3102100", "Alto Rio Doce"),
    "MUNICIPIO DE ALTO RIO NOVO|ES": ("3200359", "Alto Rio Novo"),
    "MUNICIPIO DE ALTO SANTO|CE": ("2300705", "Alto Santo"),
    "MUNICIPIO DE ALTONIA|PR": ("4100509", "Altônia"),
    "MUNICIPIO DE ALTOS|PI": ("2200400", "Altos"),
    "MUNICIPIO DE ALUMINIO|SP": ("3501152", "Alumínio"),
    "MUNICIPIO DE ALVARAES|AM": ("1300029", "Alvarães"),
    "MUNICIPIO DE ALVARENGA|MG": ("3102209", "Alvarenga"),
    "MUNICIPIO DE ALVARES MACHADO|SP": ("3501301", "Álvares Machado"),
    "MUNICIPIO DE ALVARO DE CARVALHO|SP": ("3501400", "Álvaro de Carvalho"),
    "MUNICIPIO DE ALVINOPOLIS|MG": ("3102308", "Alvinópolis"),
    "MUNICIPIO DE ALVORADA D:OESTE|RO": ("1100346", "Alvorada D'Oeste"),
    "MUNICIPIO DE ALVORADA DE MINAS|MG": ("3102407", "Alvorada de Minas"),
    "MUNICIPIO DE ALVORADA DO GURGUEIA|PI": ("2200459", "Alvorada do Gurguéia"),
    "MUNICIPIO DE ALVORADA|RS": ("4300604", "Alvorada"),
    "MUNICIPIO DE ALVORADA|TO": ("1700707", "Alvorada"),
    "MUNICIPIO DE AMAJARI|RR": ("1400027", "Amajari"),
    "MUNICIPIO DE AMAMBAI|MS": ("5000609", "Amambai"),
    "MUNICIPIO DE AMAPA DO MARANHAO|MA": ("2100550", "Amapá do Maranhão"),
    "MUNICIPIO DE AMAPA|AP": ("1600105", "Amapá"),
    "MUNICIPIO DE AMAPORA|PR": ("4100905", "Amaporã"),
    "MUNICIPIO DE AMARALINA|GO": ("5200829", "Amaralina"),
    "MUNICIPIO DE AMARANTE DO MARANHAO|MA": ("2100600", "Amarante do Maranhão"),
    "MUNICIPIO DE AMARANTE|PI": ("2200509", "Amarante"),
    "MUNICIPIO DE AMARGOSA|BA": ("2901007", "Amargosa"),
    "MUNICIPIO DE AMATURA|AM": ("1300060", "Amaturá"),
    "MUNICIPIO DE AMERICA DOURADA|BA": ("2901155", "América Dourada"),
    "MUNICIPIO DE AMERICANA|SP": ("3501608", "Americana"),
    "MUNICIPIO DE AMERICANO DO BRASIL|GO": ("5200852", "Americano do Brasil"),
    "MUNICIPIO DE AMERICO BRASILIENSE|SP": ("3501707", "Américo Brasiliense"),
    "MUNICIPIO DE AMERICO DE CAMPOS|SP": ("3501806", "Américo de Campos"),
    "MUNICIPIO DE AMETISTA DO SUL|RS": ("4300646", "Ametista do Sul"),
    "MUNICIPIO DE AMORINOPOLIS|GO": ("5200902", "Amorinópolis"),
    "MUNICIPIO DE AMPARO DE SAO FRANCISCO|SE": ("2800100", "Amparo do São Francisco"),
    "MUNICIPIO DE AMPARO DO SERRA|MG": ("3102506", "Amparo do Serra"),
    "MUNICIPIO DE AMPARO|PB": ("2500734", "Amparo"),
    "MUNICIPIO DE AMPARO|SP": ("3501905", "Amparo"),
    "MUNICIPIO DE AMPERE|PR": ("4101002", "Ampére"),
    "MUNICIPIO DE ANAGE|BA": ("2901205", "Anagé"),
    "MUNICIPIO DE ANAHY|PR": ("4101051", "Anahy"),
    "MUNICIPIO DE ANAJAS|PA": ("1500701", "Anajás"),
    "MUNICIPIO DE ANAJATUBA|MA": ("2100709", "Anajatuba"),
    "MUNICIPIO DE ANANAS|TO": ("1701002", "Ananás"),
    "MUNICIPIO DE ANANINDEUA|PA": ("1500800", "Ananindeua"),
    "MUNICIPIO DE ANAPOLIS|GO": ("5201108", "Anápolis"),
    "MUNICIPIO DE ANAPU|PA": ("1500859", "Anapu"),
    "MUNICIPIO DE ANASTACIO|MS": ("5000708", "Anastácio"),
    "MUNICIPIO DE ANAURILANDIA|MS": ("5000807", "Anaurilândia"),
    "MUNICIPIO DE ANCHIETA|ES": ("3200409", "Anchieta"),
    "MUNICIPIO DE ANCHIETA|SC": ("4200804", "Anchieta"),
    "MUNICIPIO DE ANDARAI|BA": ("2901304", "Andaraí"),
    "MUNICIPIO DE ANDIRA|PR": ("4101101", "Andirá"),
    "MUNICIPIO DE ANDORINHA|BA": ("2901353", "Andorinha"),
    "MUNICIPIO DE ANDRADAS|MG": ("3102605", "Andradas"),
    "MUNICIPIO DE ANDRADINA|SP": ("3502101", "Andradina"),
    "MUNICIPIO DE ANDRE DA ROCHA|RS": ("4300661", "André da Rocha"),
    "MUNICIPIO DE ANDRELANDIA|MG": ("3102803", "Andrelândia"),
    "MUNICIPIO DE ANGATUBA|SP": ("3502200", "Angatuba"),
    "MUNICIPIO DE ANGELANDIA|MG": ("3102852", "Angelândia"),
    "MUNICIPIO DE ANGELICA|MS": ("5000856", "Angélica"),
    "MUNICIPIO DE ANGELIM|PE": ("2601003", "Angelim"),
    "MUNICIPIO DE ANGELINA|SC": ("4200903", "Angelina"),
    "MUNICIPIO DE ANGICAL DO PIAUI|PI": ("2200608", "Angical do Piauí"),
    "MUNICIPIO DE ANGICAL|BA": ("2901403", "Angical"),
    "MUNICIPIO DE ANGICOS|RN": ("2400802", "Angicos"),
    "MUNICIPIO DE ANGICO|TO": ("1701051", "Angico"),
    "MUNICIPIO DE ANGUERA|BA": ("2901502", "Anguera"),
    "MUNICIPIO DE ANGULO|PR": ("4101150", "Ângulo"),
    "MUNICIPIO DE ANHANGUERA|GO": ("5201207", "Anhanguera"),
    "MUNICIPIO DE ANHEMBI|SP": ("3502309", "Anhembi"),
    "MUNICIPIO DE ANHUMAS|SP": ("3502408", "Anhumas"),
    "MUNICIPIO DE ANICUNS|GO": ("5201306", "Anicuns"),
    "MUNICIPIO DE ANISIO DE ABREU|PI": ("2200707", "Anísio de Abreu"),
    "MUNICIPIO DE ANITA GARIBALDI|SC": ("4201000", "Anita Garibaldi"),
    "MUNICIPIO DE ANITAPOLIS|SC": ("4201109", "Anitápolis"),
    "MUNICIPIO DE ANORI|AM": ("1300102", "Anori"),
    "MUNICIPIO DE ANTAS|BA": ("2901601", "Antas"),
    "MUNICIPIO DE ANTONINA|PR": ("4101200", "Antonina"),
    "MUNICIPIO DE ANTONIO CARDOSO|BA": ("2901700", "Antônio Cardoso"),
    "MUNICIPIO DE ANTONIO CARLOS|MG": ("3102902", "Antônio Carlos"),
    "MUNICIPIO DE ANTONIO CARLOS|SC": ("4201208", "Antônio Carlos"),
    "MUNICIPIO DE ANTONIO DIAS|MG": ("3103009", "Antônio Dias"),
    "MUNICIPIO DE ANTONIO JOAO|MS": ("5000906", "Antônio João"),
    "MUNICIPIO DE ANTONIO OLINTO|PR": ("4101309", "Antônio Olinto"),
    "MUNICIPIO DE ANTONIO PRADO DE MINAS|MG": ("3103108", "Antônio Prado de Minas"),
    "MUNICIPIO DE ANTONIO PRADO|RS": ("4300802", "Antônio Prado"),
    "MUNICIPIO DE APARECIDA D'OESTE|SP": ("3502606", "Aparecida d'Oeste"),
    "MUNICIPIO DE APARECIDA DE GOIANIA|GO": ("5201405", "Aparecida de Goiânia"),
    "MUNICIPIO DE APARECIDA DO RIO DOCE|GO": ("5201454", "Aparecida do Rio Doce"),
    "MUNICIPIO DE APARECIDA DO RIO NEGRO|TO": ("1701101", "Aparecida do Rio Negro"),
    "MUNICIPIO DE APARECIDA DO TABOADO|MS": ("5001003", "Aparecida do Taboado"),
    "MUNICIPIO DE APARECIDA|PB": ("2500775", "Aparecida"),
    "MUNICIPIO DE APARECIDA|SP": ("3502507", "Aparecida"),
    "MUNICIPIO DE APERIBE|RJ": ("3300159", "Aperibé"),
    "MUNICIPIO DE APIACA|ES": ("3200508", "Apiacá"),
    "MUNICIPIO DE APIAI|SP": ("3502705", "Apiaí"),
    "MUNICIPIO DE APICUM-ACU|MA": ("2100832", "Apicum-Açu"),
    "MUNICIPIO DE APIUNA|SC": ("4201257", "Apiúna"),
    "MUNICIPIO DE APODI|RN": ("2401008", "Apodi"),
    "MUNICIPIO DE APORA|BA": ("2901908", "Aporá"),
    "MUNICIPIO DE APUCARANA|PR": ("4101408", "Apucarana"),
    "MUNICIPIO DE APUIARES|CE": ("2300903", "Apuiarés"),
    "MUNICIPIO DE AQUIDABA|SE": ("2800209", "Aquidabã"),
    "MUNICIPIO DE AQUIDAUANA|MS": ("5001102", "Aquidauana"),
    "MUNICIPIO DE AQUIRAZ|CE": ("2301000", "Aquiraz"),
    "MUNICIPIO DE ARABUTA|SC": ("4201273", "Arabutã"),
    "MUNICIPIO DE ARACAGI|PB": ("2500809", "Araçagi"),
    "MUNICIPIO DE ARACAI|MG": ("3103207", "Araçaí"),
    "MUNICIPIO DE ARACAJU|SE": ("2800308", "Aracaju"),
    "MUNICIPIO DE ARACARIGUAMA|SP": ("3502754", "Araçariguama"),
    "MUNICIPIO DE ARACATI|CE": ("2301109", "Aracati"),
    "MUNICIPIO DE ARACATUBA|SP": ("3502804", "Araçatuba"),
    "MUNICIPIO DE ARACATU|BA": ("2902005", "Aracatu"),
    "MUNICIPIO DE ARACI|BA": ("2902104", "Araci"),
    "MUNICIPIO DE ARACOIABA DA SERRA|SP": ("3502903", "Araçoiaba da Serra"),
    "MUNICIPIO DE ARACOIABA|CE": ("2301208", "Aracoiaba"),
    "MUNICIPIO DE ARACRUZ|ES": ("3200607", "Aracruz"),
    "MUNICIPIO DE ARACUAI|MG": ("3103405", "Araçuaí"),
    "MUNICIPIO DE ARAGARCAS|GO": ("5201702", "Aragarças"),
    "MUNICIPIO DE ARAGOIANIA|GO": ("5201801", "Aragoiânia"),
    "MUNICIPIO DE ARAGOMINAS|TO": ("1701309", "Aragominas"),
    "MUNICIPIO DE ARAGUACEMA|TO": ("1701903", "Araguacema"),
    "MUNICIPIO DE ARAGUACU|TO": ("1702000", "Araguaçu"),
    "MUNICIPIO DE ARAGUAIANA|MT": ("5101001", "Araguaiana"),
    "MUNICIPIO DE ARAGUAINA|TO": ("1702109", "Araguaína"),
    "MUNICIPIO DE ARAGUAINHA|MT": ("5101209", "Araguainha"),
    "MUNICIPIO DE ARAGUANA|MA": ("2100873", "Araguanã"),
    "MUNICIPIO DE ARAGUANA|TO": ("1702158", "Araguanã"),
    "MUNICIPIO DE ARAGUAPAZ|GO": ("5202155", "Araguapaz"),
    "MUNICIPIO DE ARAGUARI|MG": ("3103504", "Araguari"),
    "MUNICIPIO DE ARAGUATINS|TO": ("1702208", "Araguatins"),
    "MUNICIPIO DE ARAIOSES|MA": ("2100907", "Araioses"),
    "MUNICIPIO DE ARAL MOREIRA|MS": ("5001243", "Aral Moreira"),
    "MUNICIPIO DE ARAMARI|BA": ("2902203", "Aramari"),
    "MUNICIPIO DE ARAMBARE|RS": ("4300851", "Arambaré"),
    "MUNICIPIO DE ARAMINA|SP": ("3503000", "Aramina"),
    "MUNICIPIO DE ARANDU|SP": ("3503109", "Arandu"),
    "MUNICIPIO DE ARANTINA|MG": ("3103603", "Arantina"),
    "MUNICIPIO DE ARAPEI|SP": ("3503158", "Arapeí"),
    "MUNICIPIO DE ARAPOEMA|TO": ("1702307", "Arapoema"),
    "MUNICIPIO DE ARAPONGAS|PR": ("4101507", "Arapongas"),
    "MUNICIPIO DE ARAPONGA|MG": ("3103702", "Araponga"),
    "MUNICIPIO DE ARAPORA|MG": ("3103751", "Araporã"),
    "MUNICIPIO DE ARAPOTI|PR": ("4101606", "Arapoti"),
    "MUNICIPIO DE ARAPUA|MG": ("3103801", "Arapuá"),
    "MUNICIPIO DE ARAPUTANGA|MT": ("5101258", "Araputanga"),
    "MUNICIPIO DE ARAQUARI|SC": ("4201307", "Araquari"),
    "MUNICIPIO DE ARARANGUA|SC": ("4201406", "Araranguá"),
    "MUNICIPIO DE ARARAQUARA|SP": ("3503208", "Araraquara"),
    "MUNICIPIO DE ARARAS|SP": ("3503307", "Araras"),
    "MUNICIPIO DE ARARA|PB": ("2500908", "Arara"),
    "MUNICIPIO DE ARARICA|RS": ("4300877", "Araricá"),
    "MUNICIPIO DE ARARIPE|CE": ("2301307", "Araripe"),
    "MUNICIPIO DE ARARIPINA|PE": ("2601102", "Araripina"),
    "MUNICIPIO DE ARARI|MA": ("2101004", "Arari"),
    "MUNICIPIO DE ARARUNA|PB": ("2501005", "Araruna"),
    "MUNICIPIO DE ARARUNA|PR": ("4101705", "Araruna"),
    "MUNICIPIO DE ARATACA|BA": ("2902252", "Arataca"),
    "MUNICIPIO DE ARATIBA|RS": ("4300901", "Aratiba"),
    "MUNICIPIO DE ARATUBA|CE": ("2301406", "Aratuba"),
    "MUNICIPIO DE ARAUA|SE": ("2800407", "Arauá"),
    "MUNICIPIO DE ARAUJOS|MG": ("3103900", "Araújos"),
    "MUNICIPIO DE ARAXA|MG": ("3104007", "Araxá"),
    "MUNICIPIO DE ARCEBURGO|MG": ("3104106", "Arceburgo"),
    "MUNICIPIO DE ARCOS|MG": ("3104205", "Arcos"),
    "MUNICIPIO DE ARCOVERDE|PE": ("2601201", "Arcoverde"),
    "MUNICIPIO DE AREADO|MG": ("3104304", "Areado"),
    "MUNICIPIO DE AREIA BRANCA|RN": ("2401107", "Areia Branca"),
    "MUNICIPIO DE AREIA BRANCA|SE": ("2800506", "Areia Branca"),
    "MUNICIPIO DE AREIA DE BARAUNAS|PB": ("2501153", "Areia de Baraúnas"),
    "MUNICIPIO DE AREIAS|SP": ("3503505", "Areias"),
    "MUNICIPIO DE AREIA|PB": ("2501104", "Areia"),
    "MUNICIPIO DE AREIOPOLIS|SP": ("3503604", "Areiópolis"),
    "MUNICIPIO DE ARENAPOLIS|MT": ("5101308", "Arenápolis"),
    "MUNICIPIO DE ARES|RN": ("2401206", "Arês"),
    "MUNICIPIO DE ARGIRITA|MG": ("3104403", "Argirita"),
    "MUNICIPIO DE ARICANDUVA|MG": ("3104452", "Aricanduva"),
    "MUNICIPIO DE ARINOS|MG": ("3104502", "Arinos"),
    "MUNICIPIO DE ARIQUEMES|RO": ("1100023", "Ariquemes"),
    "MUNICIPIO DE ARIRANHA DO IVAI|PR": ("4101853", "Ariranha do Ivaí"),
    "MUNICIPIO DE ARIRANHA|SP": ("3503703", "Ariranha"),
    "MUNICIPIO DE ARMACAO DE BUZIOS|RJ": ("3300233", "Armação dos Búzios"),
    "MUNICIPIO DE ARMAZEM|SC": ("4201505", "Armazém"),
    "MUNICIPIO DE AROAZES|PI": ("2200905", "Aroazes"),
    "MUNICIPIO DE AROEIRAS DO ITAIM|PI": ("2200954", "Aroeiras do Itaim"),
    "MUNICIPIO DE AROEIRAS|PB": ("2501302", "Aroeiras"),
    "MUNICIPIO DE ARRAIAL DO CABO|RJ": ("3300258", "Arraial do Cabo"),
    "MUNICIPIO DE ARRAIAL|PI": ("2201002", "Arraial"),
    "MUNICIPIO DE ARRAIAS|TO": ("1702406", "Arraias"),
    "MUNICIPIO DE ARROIO DO MEIO|RS": ("4301008", "Arroio do Meio"),
    "MUNICIPIO DE ARROIO DO PADRE|RS": ("4301073", "Arroio do Padre"),
    "MUNICIPIO DE ARROIO DO SAL|RS": ("4301057", "Arroio do Sal"),
    "MUNICIPIO DE ARROIO DO TIGRE|RS": ("4301206", "Arroio do Tigre"),
    "MUNICIPIO DE ARROIO DOS RATOS|RS": ("4301107", "Arroio dos Ratos"),
    "MUNICIPIO DE ARROIO GRANDE|RS": ("4301305", "Arroio Grande"),
    "MUNICIPIO DE ARROIO TRINTA|SC": ("4201604", "Arroio Trinta"),
    "MUNICIPIO DE ARTUR NOGUEIRA|SP": ("3503802", "Artur Nogueira"),
    "MUNICIPIO DE ARUANA|GO": ("5202502", "Aruanã"),
    "MUNICIPIO DE ARUJA|SP": ("3503901", "Arujá"),
    "MUNICIPIO DE ARVOREDO|SC": ("4201653", "Arvoredo"),
    "MUNICIPIO DE ARVOREZINHA|RS": ("4301404", "Arvorezinha"),
    "MUNICIPIO DE ASCURRA|SC": ("4201703", "Ascurra"),
    "MUNICIPIO DE ASPASIA|SP": ("3503950", "Aspásia"),
    "MUNICIPIO DE ASSAI|PR": ("4101903", "Assaí"),
    "MUNICIPIO DE ASSARE|CE": ("2301604", "Assaré"),
    "MUNICIPIO DE ASSIS BRASIL|AC": ("1200054", "Assis Brasil"),
    "MUNICIPIO DE ASSIS CHATEAUBRIAND|PR": ("4102000", "Assis Chateaubriand"),
    "MUNICIPIO DE ASSIS|SP": ("3504008", "Assis"),
    "MUNICIPIO DE ASSUNCAO DO PIAUI|PI": ("2201051", "Assunção do Piauí"),
    "MUNICIPIO DE ASSUNCAO|PB": ("2501351", "Assunção"),
    "MUNICIPIO DE ASSU|RN": ("2400208", "Açu"),
    "MUNICIPIO DE ASTOLFO DUTRA|MG": ("3104601", "Astolfo Dutra"),
    "MUNICIPIO DE ASTORGA|PR": ("4102109", "Astorga"),
    "MUNICIPIO DE ATALAIA|AL": ("2700409", "Atalaia"),
    "MUNICIPIO DE ATALAIA|PR": ("4102208", "Atalaia"),
    "MUNICIPIO DE ATALANTA|SC": ("4201802", "Atalanta"),
    "MUNICIPIO DE ATALEIA|MG": ("3104700", "Ataléia"),
    "MUNICIPIO DE ATIBAIA|SP": ("3504107", "Atibaia"),
    "MUNICIPIO DE ATILIO VIVACQUA|ES": ("3200706", "Atílio Vivácqua"),
    "MUNICIPIO DE AUGUSTINOPOLIS|TO": ("1702554", "Augustinópolis"),
    "MUNICIPIO DE AUGUSTO CORREA|PA": ("1500909", "Augusto Corrêa"),
    "MUNICIPIO DE AUGUSTO DE LIMA|MG": ("3104809", "Augusto de Lima"),
    "MUNICIPIO DE AUGUSTO PESTANA|RS": ("4301503", "Augusto Pestana"),
    "MUNICIPIO DE AUREA|RS": ("4301552", "Áurea"),
    "MUNICIPIO DE AURELINO LEAL|BA": ("2902401", "Aurelino Leal"),
    "MUNICIPIO DE AURIFLAMA|SP": ("3504206", "Auriflama"),
    "MUNICIPIO DE AURORA DO PARA|PA": ("1500958", "Aurora do Pará"),
    "MUNICIPIO DE AURORA DO TOCANTINS|TO": ("1702703", "Aurora do Tocantins"),
    "MUNICIPIO DE AURORA|CE": ("2301703", "Aurora"),
    "MUNICIPIO DE AURORA|SC": ("4201901", "Aurora"),
    "MUNICIPIO DE AUTAZES|AM": ("1300300", "Autazes"),
    "MUNICIPIO DE AVAI|SP": ("3504305", "Avaí"),
    "MUNICIPIO DE AVANHANDAVA|SP": ("3504404", "Avanhandava"),
    "MUNICIPIO DE AVARE|SP": ("3504503", "Avaré"),
    "MUNICIPIO DE AVEIRO|PA": ("1501006", "Aveiro"),
    "MUNICIPIO DE AVELINO LOPES|PI": ("2201101", "Avelino Lopes"),
    "MUNICIPIO DE AVELINOPOLIS|GO": ("5202809", "Avelinópolis"),
    "MUNICIPIO DE AXIXA DO TOCANTINS|TO": ("1702901", "Axixá do Tocantins"),
    "MUNICIPIO DE AXIXA|MA": ("2101103", "Axixá"),
    "MUNICIPIO DE BABACULANDIA|TO": ("1703008", "Babaçulândia"),
    "MUNICIPIO DE BACABAL|MA": ("2101202", "Bacabal"),
    "MUNICIPIO DE BACURI|MA": ("2101301", "Bacuri"),
    "MUNICIPIO DE BADY BASSITT|SP": ("3504602", "Bady Bassitt"),
    "MUNICIPIO DE BAEPENDI|MG": ("3104908", "Baependi"),
    "MUNICIPIO DE BAGE|RS": ("4301602", "Bagé"),
    "MUNICIPIO DE BAGRE|PA": ("1501105", "Bagre"),
    "MUNICIPIO DE BAIA FORMOSA|RN": ("2401404", "Baía Formosa"),
    "MUNICIPIO DE BAIANOPOLIS|BA": ("2902500", "Baianópolis"),
    "MUNICIPIO DE BAIAO|PA": ("1501204", "Baião"),
    "MUNICIPIO DE BAIXA GRANDE DO RIBEIRO|PI": ("2201150", "Baixa Grande do Ribeiro"),
    "MUNICIPIO DE BAIXA GRANDE|BA": ("2902609", "Baixa Grande"),
    "MUNICIPIO DE BAIXO GUANDU|ES": ("3200805", "Baixo Guandu"),
    "MUNICIPIO DE BALBINOS|SP": ("3504701", "Balbinos"),
    "MUNICIPIO DE BALDIM|MG": ("3105004", "Baldim"),
    "MUNICIPIO DE BALNEARIO ARROIO DO SILVA|SC": ("4201950", "Balneário Arroio do Silva"),
    "MUNICIPIO DE BALNEARIO BARRA DO SUL|SC": ("4202057", "Balneário Barra do Sul"),
    "MUNICIPIO DE BALNEARIO CAMBORIU|SC": ("4202008", "Balneário Camboriú"),
    "MUNICIPIO DE BALNEARIO DE PICARRAS|SC": ("4212809", "Balneário Piçarras"),
    "MUNICIPIO DE BALNEARIO GAIVOTA|SC": ("4202073", "Balneário Gaivota"),
    "MUNICIPIO DE BALNEARIO PINHAL|RS": ("4301636", "Balneário Pinhal"),
    "MUNICIPIO DE BALNEARIO RINCAO|SC": ("4220000", "Balneário Rincão"),
    "MUNICIPIO DE BALSA NOVA|PR": ("4102307", "Balsa Nova"),
    "MUNICIPIO DE BALSAMO|SP": ("3504800", "Bálsamo"),
    "MUNICIPIO DE BAMBUI|MG": ("3105103", "Bambuí"),
    "MUNICIPIO DE BANABUIU|CE": ("2301851", "Banabuiú"),
    "MUNICIPIO DE BANANAL|SP": ("3504909", "Bananal"),
    "MUNICIPIO DE BANANEIRAS|PB": ("2501500", "Bananeiras"),
    "MUNICIPIO DE BANDEIRA DO SUL|MG": ("3105301", "Bandeira do Sul"),
    "MUNICIPIO DE BANDEIRANTES DO TOCANTINS|TO": ("1703057", "Bandeirantes do Tocantins"),
    "MUNICIPIO DE BANDEIRANTES|MS": ("5001508", "Bandeirantes"),
    "MUNICIPIO DE BANDEIRANTES|PR": ("4102406", "Bandeirantes"),
    "MUNICIPIO DE BANDEIRANTE|SC": ("4202081", "Bandeirante"),
    "MUNICIPIO DE BANDEIRA|MG": ("3105202", "Bandeira"),
    "MUNICIPIO DE BANNACH|PA": ("1501253", "Bannach"),
    "MUNICIPIO DE BANZAE|BA": ("2902658", "Banzaê"),
    "MUNICIPIO DE BARAO DE ANTONINA|SP": ("3505005", "Barão de Antonina"),
    "MUNICIPIO DE BARAO DE COCAIS|MG": ("3105400", "Barão de Cocais"),
    "MUNICIPIO DE BARAO DE COTEGIPE|RS": ("4301701", "Barão de Cotegipe"),
    "MUNICIPIO DE BARAO DE MELGACO|MT": ("5101605", "Barão de Melgaço"),
    "MUNICIPIO DE BARAO DO MONTE ALTO|MG": ("3105509", "Barão de Monte Alto"),
    "MUNICIPIO DE BARAO DO TRIUNFO|RS": ("4301750", "Barão do Triunfo"),
    "MUNICIPIO DE BARAO|RS": ("4301651", "Barão"),
    "MUNICIPIO DE BARAUNA|PB": ("2501534", "Baraúna"),
    "MUNICIPIO DE BARAUNA|RN": ("2401453", "Baraúna"),
    "MUNICIPIO DE BARBACENA|MG": ("3105608", "Barbacena"),
    "MUNICIPIO DE BARBALHA|CE": ("2301901", "Barbalha"),
    "MUNICIPIO DE BARBOSA FERRAZ|PR": ("4102505", "Barbosa Ferraz"),
    "MUNICIPIO DE BARCARENA|PA": ("1501303", "Barcarena"),
    "MUNICIPIO DE BARCELONA|RN": ("2401503", "Barcelona"),
    "MUNICIPIO DE BARCELOS|AM": ("1300409", "Barcelos"),
    "MUNICIPIO DE BARIRI|SP": ("3505203", "Bariri"),
    "MUNICIPIO DE BARRA BONITA|SC": ("4202099", "Barra Bonita"),
    "MUNICIPIO DE BARRA BONITA|SP": ("3505302", "Barra Bonita"),
    "MUNICIPIO DE BARRA D'ALCANTARA|PI": ("2201176", "Barra D'Alcântara"),
    "MUNICIPIO DE BARRA DE SANTANA|PB": ("2501575", "Barra de Santana"),
    "MUNICIPIO DE BARRA DE SANTO ANTONIO|AL": ("2700508", "Barra de Santo Antônio"),
    "MUNICIPIO DE BARRA DE SAO FRANCISCO|ES": ("3200904", "Barra de São Francisco"),
    "MUNICIPIO DE BARRA DO BUGRES|MT": ("5101704", "Barra do Bugres"),
    "MUNICIPIO DE BARRA DO CHOCA|BA": ("2902906", "Barra do Choça"),
    "MUNICIPIO DE BARRA DO CORDA|MA": ("2101608", "Barra do Corda"),
    "MUNICIPIO DE BARRA DO GARCAS|MT": ("5101803", "Barra do Garças"),
    "MUNICIPIO DE BARRA DO GUARITA|RS": ("4301859", "Barra do Guarita"),
    "MUNICIPIO DE BARRA DO JACARE|PR": ("4102703", "Barra do Jacaré"),
    "MUNICIPIO DE BARRA DO MENDES|BA": ("2903003", "Barra do Mendes"),
    "MUNICIPIO DE BARRA DO OURO|TO": ("1703073", "Barra do Ouro"),
    "MUNICIPIO DE BARRA DO PIRAI|RJ": ("3300308", "Barra do Piraí"),
    "MUNICIPIO DE BARRA DO QUARAI|RS": ("4301875", "Barra do Quaraí"),
    "MUNICIPIO DE BARRA DO RIBEIRO|RS": ("4301909", "Barra do Ribeiro"),
    "MUNICIPIO DE BARRA DO RIO AZUL|RS": ("4301925", "Barra do Rio Azul"),
    "MUNICIPIO DE BARRA DO ROCHA|BA": ("2903102", "Barra do Rocha"),
    "MUNICIPIO DE BARRA DO TURVO|SP": ("3505401", "Barra do Turvo"),
    "MUNICIPIO DE BARRA DOS COQUEIROS|SE": ("2800605", "Barra dos Coqueiros"),
    "MUNICIPIO DE BARRA FUNDA|RS": ("4301958", "Barra Funda"),
    "MUNICIPIO DE BARRA LONGA|MG": ("3105707", "Barra Longa"),
    "MUNICIPIO DE BARRA MANSA|RJ": ("3300407", "Barra Mansa"),
    "MUNICIPIO DE BARRA VELHA|SC": ("4202107", "Barra Velha"),
    "MUNICIPIO DE BARRACAO|PR": ("4102604", "Barracão"),
    "MUNICIPIO DE BARRACAO|RS": ("4301800", "Barracão"),
    "MUNICIPIO DE BARRAS|PI": ("2201200", "Barras"),
    "MUNICIPIO DE BARRA|BA": ("2902708", "Barra"),
    "MUNICIPIO DE BARREIRINHAS|MA": ("2101707", "Barreirinhas"),
    "MUNICIPIO DE BARRETOS|SP": ("3505500", "Barretos"),
    "MUNICIPIO DE BARRINHA|SP": ("3505609", "Barrinha"),
    "MUNICIPIO DE BARRO ALTO|BA": ("2903235", "Barro Alto"),
    "MUNICIPIO DE BARRO ALTO|GO": ("5203203", "Barro Alto"),
    "MUNICIPIO DE BARRO DURO|PI": ("2201408", "Barro Duro"),
    "MUNICIPIO DE BARRO PRETO|BA": ("2903300", "Barro Preto"),
    "MUNICIPIO DE BARROCAS|BA": ("2903276", "Barrocas"),
    "MUNICIPIO DE BARROLANDIA|TO": ("1703107", "Barrolândia"),
    "MUNICIPIO DE BARROS CASSAL|RS": ("4302006", "Barros Cassal"),
    "MUNICIPIO DE BARROSO|MG": ("3105905", "Barroso"),
    "MUNICIPIO DE BARUERI|SP": ("3505708", "Barueri"),
    "MUNICIPIO DE BASTOS|SP": ("3505807", "Bastos"),
    "MUNICIPIO DE BATAGUASSU|MS": ("5001904", "Bataguassu"),
    "MUNICIPIO DE BATALHA|AL": ("2700706", "Batalha"),
    "MUNICIPIO DE BATALHA|PI": ("2201507", "Batalha"),
    "MUNICIPIO DE BATATAIS|SP": ("3505906", "Batatais"),
    "MUNICIPIO DE BATAYPORA|MS": ("5002001", "Batayporã"),
    "MUNICIPIO DE BATURITE|CE": ("2302107", "Baturité"),
    "MUNICIPIO DE BAURU|SP": ("3506003", "Bauru"),
    "MUNICIPIO DE BAYEUX|PB": ("2501807", "Bayeux"),
    "MUNICIPIO DE BEBEDOURO|SP": ("3506102", "Bebedouro"),
    "MUNICIPIO DE BEBERIBE|CE": ("2302206", "Beberibe"),
    "MUNICIPIO DE BELA CRUZ|CE": ("2302305", "Bela Cruz"),
    "MUNICIPIO DE BELA VISTA DE GOIAS|GO": ("5203302", "Bela Vista de Goiás"),
    "MUNICIPIO DE BELA VISTA DE MINAS|MG": ("3106002", "Bela Vista de Minas"),
    "MUNICIPIO DE BELA VISTA DO CAROBA|PR": ("4102752", "Bela Vista da Caroba"),
    "MUNICIPIO DE BELA VISTA DO MARANHAO|MA": ("2101772", "Bela Vista do Maranhão"),
    "MUNICIPIO DE BELA VISTA DO PARAISO|PR": ("4102802", "Bela Vista do Paraíso"),
    "MUNICIPIO DE BELA VISTA DO TOLDO|SC": ("4202131", "Bela Vista do Toldo"),
    "MUNICIPIO DE BELA VISTA|MS": ("5002100", "Bela Vista"),
    "MUNICIPIO DE BELAGUA|MA": ("2101731", "Belágua"),
    "MUNICIPIO DE BELEM DE SAO FRANCISCO|PE": ("2601607", "Belém do São Francisco"),
    "MUNICIPIO DE BELEM DO BREJO DO CRUZ|PB": ("2502003", "Belém do Brejo do Cruz"),
    "MUNICIPIO DE BELEM DO PIAUI|PI": ("2201572", "Belém do Piauí"),
    "MUNICIPIO DE BELEM|AL": ("2700805", "Belém"),
    "MUNICIPIO DE BELEM|PA": ("1501402", "Belém"),
    "MUNICIPIO DE BELEM|PB": ("2501906", "Belém"),
    "MUNICIPIO DE BELMIRO BRAGA|MG": ("3106101", "Belmiro Braga"),
    "MUNICIPIO DE BELO CAMPO|BA": ("2903508", "Belo Campo"),
    "MUNICIPIO DE BELO HORIZONTE|MG": ("3106200", "Belo Horizonte"),
    "MUNICIPIO DE BELO MONTE|AL": ("2700904", "Belo Monte"),
    "MUNICIPIO DE BELO ORIENTE|MG": ("3106309", "Belo Oriente"),
    "MUNICIPIO DE BELO VALE|MG": ("3106408", "Belo Vale"),
    "MUNICIPIO DE BELTERRA|PA": ("1501451", "Belterra"),
    "MUNICIPIO DE BENEDITO NOVO|SC": ("4202206", "Benedito Novo"),
    "MUNICIPIO DE BENEVIDES|PA": ("1501501", "Benevides"),
    "MUNICIPIO DE BENJAMIN CONSTANT DO SUL|RS": ("4302055", "Benjamin Constant do Sul"),
    "MUNICIPIO DE BENJAMIN CONSTANT|AM": ("1300607", "Benjamin Constant"),
    "MUNICIPIO DE BENTO FERNANDES|RN": ("2401602", "Bento Fernandes"),
    "MUNICIPIO DE BENTO GONCALVES|RS": ("4302105", "Bento Gonçalves"),
    "MUNICIPIO DE BERILO|MG": ("3106507", "Berilo"),
    "MUNICIPIO DE BERIZAL|MG": ("3106655", "Berizal"),
    "MUNICIPIO DE BERNARDINO BATISTA|PB": ("2502052", "Bernardino Batista"),
    "MUNICIPIO DE BERNARDINO DE CAMPOS|SP": ("3506300", "Bernardino de Campos"),
    "MUNICIPIO DE BERNARDO SAYAO|TO": ("1703206", "Bernardo Sayão"),
    "MUNICIPIO DE BERTIOGA|SP": ("3506359", "Bertioga"),
    "MUNICIPIO DE BERTOLINIA|PI": ("2201705", "Bertolínia"),
    "MUNICIPIO DE BERTOPOLIS|MG": ("3106606", "Bertópolis"),
    "MUNICIPIO DE BETANIA DO PIAUI|PI": ("2201739", "Betânia do Piauí"),
    "MUNICIPIO DE BETANIA|PE": ("2601805", "Betânia"),
    "MUNICIPIO DE BETIM|MG": ("3106705", "Betim"),
    "MUNICIPIO DE BEZERROS|PE": ("2601904", "Bezerros"),
    "MUNICIPIO DE BIAS FORTES|MG": ("3106804", "Bias Fortes"),
    "MUNICIPIO DE BICAS|MG": ("3106903", "Bicas"),
    "MUNICIPIO DE BIGUACU|SC": ("4202305", "Biguaçu"),
    "MUNICIPIO DE BILAC|SP": ("3506409", "Bilac"),
    "MUNICIPIO DE BIQUINHAS|MG": ("3107000", "Biquinhas"),
    "MUNICIPIO DE BIRIGUI|SP": ("3506508", "Birigui"),
    "MUNICIPIO DE BIRITIBA-MIRIM|SP": ("3506607", "Biritiba Mirim"),
    "MUNICIPIO DE BIRITINGA|BA": ("2903607", "Biritinga"),
    "MUNICIPIO DE BITURUNA|PR": ("4102901", "Bituruna"),
    "MUNICIPIO DE BLUMENAU|SC": ("4202404", "Blumenau"),
    "MUNICIPIO DE BOA ESPERANCA DO IGUACU|PR": ("4103024", "Boa Esperança do Iguaçu"),
    "MUNICIPIO DE BOA ESPERANCA DO SUL|SP": ("3506706", "Boa Esperança do Sul"),
    "MUNICIPIO DE BOA ESPERANCA|ES": ("3201001", "Boa Esperança"),
    "MUNICIPIO DE BOA ESPERANCA|MG": ("3107109", "Boa Esperança"),
    "MUNICIPIO DE BOA ESPERANCA|PR": ("4103008", "Boa Esperança"),
    "MUNICIPIO DE BOA HORA|PI": ("2201770", "Boa Hora"),
    "MUNICIPIO DE BOA SAUDE|RN": ("2405306", "Januário Cicco"),
    "MUNICIPIO DE BOA VENTURA DE SAO ROQUE|PR": ("4103040", "Boa Ventura de São Roque"),
    "MUNICIPIO DE BOA VIAGEM|CE": ("2302404", "Boa Viagem"),
    "MUNICIPIO DE BOA VISTA DA APARECIDA|PR": ("4103057", "Boa Vista da Aparecida"),
    "MUNICIPIO DE BOA VISTA DAS MISSOES|RS": ("4302154", "Boa Vista das Missões"),
    "MUNICIPIO DE BOA VISTA DO BURICA|RS": ("4302204", "Boa Vista do Buricá"),
    "MUNICIPIO DE BOA VISTA DO CADEADO|RS": ("4302220", "Boa Vista do Cadeado"),
    "MUNICIPIO DE BOA VISTA DO INCRA|RS": ("4302238", "Boa Vista do Incra"),
    "MUNICIPIO DE BOA VISTA DO RAMOS|AM": ("1300680", "Boa Vista do Ramos"),
    "MUNICIPIO DE BOA VISTA DO SUL|RS": ("4302253", "Boa Vista do Sul"),
    "MUNICIPIO DE BOA VISTA DO TUPIM|BA": ("2903805", "Boa Vista do Tupim"),
    "MUNICIPIO DE BOA VISTA|PB": ("2502151", "Boa Vista"),
    "MUNICIPIO DE BOA VISTA|RR": ("1400100", "Boa Vista"),
    "MUNICIPIO DE BOCAINA DE MINAS|MG": ("3107208", "Bocaina de Minas"),
    "MUNICIPIO DE BOCAINA DO SUL|SC": ("4202438", "Bocaina do Sul"),
    "MUNICIPIO DE BOCAINA|PI": ("2201804", "Bocaina"),
    "MUNICIPIO DE BOCAINA|SP": ("3506805", "Bocaina"),
    "MUNICIPIO DE BOCAIUVA DO SUL|PR": ("4103107", "Bocaiúva do Sul"),
    "MUNICIPIO DE BOCAIUVA|MG": ("3107307", "Bocaiúva"),
    "MUNICIPIO DE BODOCO|PE": ("2602001", "Bodocó"),
    "MUNICIPIO DE BODOQUENA|MS": ("5002159", "Bodoquena"),
    "MUNICIPIO DE BOFETE|SP": ("3506904", "Bofete"),
    "MUNICIPIO DE BOITUVA|SP": ("3507001", "Boituva"),
    "MUNICIPIO DE BOM CONSELHO|PE": ("2602100", "Bom Conselho"),
    "MUNICIPIO DE BOM DESPACHO|MG": ("3107406", "Bom Despacho"),
    "MUNICIPIO DE BOM JARDIM DA SERRA|SC": ("4202503", "Bom Jardim da Serra"),
    "MUNICIPIO DE BOM JARDIM DE GOIAS|GO": ("5203401", "Bom Jardim de Goiás"),
    "MUNICIPIO DE BOM JARDIM DE MINAS|MG": ("3107505", "Bom Jardim de Minas"),
    "MUNICIPIO DE BOM JARDIM|MA": ("2102002", "Bom Jardim"),
    "MUNICIPIO DE BOM JARDIM|PE": ("2602209", "Bom Jardim"),
    "MUNICIPIO DE BOM JARDIM|RJ": ("3300506", "Bom Jardim"),
    "MUNICIPIO DE BOM JESUS DA LAPA|BA": ("2903904", "Bom Jesus da Lapa"),
    "MUNICIPIO DE BOM JESUS DA PENHA|MG": ("3107604", "Bom Jesus da Penha"),
    "MUNICIPIO DE BOM JESUS DA SERRA|BA": ("2903953", "Bom Jesus da Serra"),
    "MUNICIPIO DE BOM JESUS DAS SELVAS|MA": ("2102036", "Bom Jesus das Selvas"),
    "MUNICIPIO DE BOM JESUS DO AMPARO|MG": ("3107703", "Bom Jesus do Amparo"),
    "MUNICIPIO DE BOM JESUS DO ARAGUAIA|MT": ("5101852", "Bom Jesus do Araguaia"),
    "MUNICIPIO DE BOM JESUS DO GALHO|MG": ("3107802", "Bom Jesus do Galho"),
    "MUNICIPIO DE BOM JESUS DO ITABAPOANA|RJ": ("3300605", "Bom Jesus do Itabapoana"),
    "MUNICIPIO DE BOM JESUS DO NORTE|ES": ("3201100", "Bom Jesus do Norte"),
    "MUNICIPIO DE BOM JESUS DO OESTE|SC": ("4202578", "Bom Jesus do Oeste"),
    "MUNICIPIO DE BOM JESUS DO SUL|PR": ("4103156", "Bom Jesus do Sul"),
    "MUNICIPIO DE BOM JESUS DO TOCANTINS|PA": ("1501576", "Bom Jesus do Tocantins"),
    "MUNICIPIO DE BOM JESUS DO TOCANTINS|TO": ("1703305", "Bom Jesus do Tocantins"),
    "MUNICIPIO DE BOM JESUS DOS PERDOES|SP": ("3507100", "Bom Jesus dos Perdões"),
    "MUNICIPIO DE BOM JESUS|GO": ("5203500", "Bom Jesus de Goiás"),
    "MUNICIPIO DE BOM LUGAR|MA": ("2102077", "Bom Lugar"),
    "MUNICIPIO DE BOM PRINCIPIO|RS": ("4302352", "Bom Princípio"),
    "MUNICIPIO DE BOM PROGRESSO|RS": ("4302378", "Bom Progresso"),
    "MUNICIPIO DE BOM REPOUSO|MG": ("3107901", "Bom Repouso"),
    "MUNICIPIO DE BOM RETIRO DO SUL|RS": ("4302402", "Bom Retiro do Sul"),
    "MUNICIPIO DE BOM RETIRO|SC": ("4202602", "Bom Retiro"),
    "MUNICIPIO DE BOM SUCESSO DE ITARARE|SP": ("3507159", "Bom Sucesso de Itararé"),
    "MUNICIPIO DE BOM SUCESSO DO SUL|PR": ("4103222", "Bom Sucesso do Sul"),
    "MUNICIPIO DE BOM SUCESSO|MG": ("3108008", "Bom Sucesso"),
    "MUNICIPIO DE BOM SUCESSO|PB": ("2502300", "Bom Sucesso"),
    "MUNICIPIO DE BOM SUCESSO|PR": ("4103206", "Bom Sucesso"),
    "MUNICIPIO DE BOMBINHAS|SC": ("4202453", "Bombinhas"),
    "MUNICIPIO DE BONFIM DO PIAUI|PI": ("2201929", "Bonfim do Piauí"),
    "MUNICIPIO DE BONFIM|MG": ("3108107", "Bonfim"),
    "MUNICIPIO DE BONFIM|RR": ("1400159", "Bonfim"),
    "MUNICIPIO DE BONFINOPOLIS DE MINAS|MG": ("3108206", "Bonfinópolis de Minas"),
    "MUNICIPIO DE BONFINOPOLIS|GO": ("5203559", "Bonfinópolis"),
    "MUNICIPIO DE BONINAL|BA": ("2904001", "Boninal"),
    "MUNICIPIO DE BONITO DE MINAS|MG": ("3108255", "Bonito de Minas"),
    "MUNICIPIO DE BONITO DE SANTA FE|PB": ("2502409", "Bonito de Santa Fé"),
    "MUNICIPIO DE BONITO|BA": ("2904050", "Bonito"),
    "MUNICIPIO DE BONITO|MS": ("5002209", "Bonito"),
    "MUNICIPIO DE BONITO|PA": ("1501600", "Bonito"),
    "MUNICIPIO DE BONITO|PE": ("2602308", "Bonito"),
    "MUNICIPIO DE BONOPOLIS|GO": ("5203575", "Bonópolis"),
    "MUNICIPIO DE BOQUEIRAO|PB": ("2502508", "Boqueirão"),
    "MUNICIPIO DE BOQUIM|SE": ("2800670", "Boquim"),
    "MUNICIPIO DE BOQUIRA|BA": ("2904100", "Boquira"),
    "MUNICIPIO DE BORACEIA|SP": ("3507308", "Boracéia"),
    "MUNICIPIO DE BORA|SP": ("3507209", "Borá"),
    "MUNICIPIO DE BORBA|AM": ("1300805", "Borba"),
    "MUNICIPIO DE BORBOREMA|PB": ("2502706", "Borborema"),
    "MUNICIPIO DE BORBOREMA|SP": ("3507407", "Borborema"),
    "MUNICIPIO DE BORDA DA MATA|MG": ("3108305", "Borda da Mata"),
    "MUNICIPIO DE BORRAZOPOLIS|PR": ("4103305", "Borrazópolis"),
    "MUNICIPIO DE BOSSOROCA|RS": ("4302501", "Bossoroca"),
    "MUNICIPIO DE BOTELHOS|MG": ("3108404", "Botelhos"),
    "MUNICIPIO DE BOTUCATU|SP": ("3507506", "Botucatu"),
    "MUNICIPIO DE BOTUMIRIM|MG": ("3108503", "Botumirim"),
    "MUNICIPIO DE BOTUPORA|BA": ("2904209", "Botuporã"),
    "MUNICIPIO DE BOTUVERA|SC": ("4202701", "Botuverá"),
    "MUNICIPIO DE BOZANO|RS": ("4302584", "Bozano"),
    "MUNICIPIO DE BRACO DO NORTE|SC": ("4202800", "Braço do Norte"),
    "MUNICIPIO DE BRACO DO TROMBUDO|SC": ("4202859", "Braço do Trombudo"),
    "MUNICIPIO DE BRAGANCA PAULISTA|SP": ("3507605", "Bragança Paulista"),
    "MUNICIPIO DE BRAGANCA|PA": ("1501709", "Bragança"),
    "MUNICIPIO DE BRAGANEY|PR": ("4103354", "Braganey"),
    "MUNICIPIO DE BRAGA|RS": ("4302600", "Braga"),
    "MUNICIPIO DE BRANQUINHA|AL": ("2701100", "Branquinha"),
    "MUNICIPIO DE BRAS PIRES|MG": ("3108701", "Brás Pires"),
    "MUNICIPIO DE BRASIL NOVO|PA": ("1501725", "Brasil Novo"),
    "MUNICIPIO DE BRASILANDIA DE MINAS|MG": ("3108552", "Brasilândia de Minas"),
    "MUNICIPIO DE BRASILANDIA DO TOCANTINS|TO": ("1703602", "Brasilândia do Tocantins"),
    "MUNICIPIO DE BRASILANDIA|MS": ("5002308", "Brasilândia"),
    "MUNICIPIO DE BRASILEIA|AC": ("1200104", "Brasiléia"),
    "MUNICIPIO DE BRASILEIRA|PI": ("2201960", "Brasileira"),
    "MUNICIPIO DE BRASILIA DE MINAS|MG": ("3108602", "Brasília de Minas"),
    "MUNICIPIO DE BRASOPOLIS|MG": ("3108909", "Brazópolis"),
    "MUNICIPIO DE BRAUNAS|MG": ("3108800", "Braúnas"),
    "MUNICIPIO DE BRAZABRANTES|GO": ("5203609", "Brazabrantes"),
    "MUNICIPIO DE BREJAO|PE": ("2602407", "Brejão"),
    "MUNICIPIO DE BREJETUBA|ES": ("3201159", "Brejetuba"),
    "MUNICIPIO DE BREJINHO DE NAZARE|TO": ("1703701", "Brejinho de Nazaré"),
    "MUNICIPIO DE BREJINHO|PE": ("2602506", "Brejinho"),
    "MUNICIPIO DE BREJINHO|RN": ("2401800", "Brejinho"),
    "MUNICIPIO DE BREJO DE AREIA|MA": ("2102150", "Brejo de Areia"),
    "MUNICIPIO DE BREJO DO CRUZ|PB": ("2502805", "Brejo do Cruz"),
    "MUNICIPIO DE BREJO DOS SANTOS|PB": ("2502904", "Brejo dos Santos"),
    "MUNICIPIO DE BREJO GRANDE DO ARAGUAIA|PA": ("1501758", "Brejo Grande do Araguaia"),
    "MUNICIPIO DE BREJO GRANDE|SE": ("2800704", "Brejo Grande"),
    "MUNICIPIO DE BREJO SANTO|CE": ("2302503", "Brejo Santo"),
    "MUNICIPIO DE BREJOES|BA": ("2904308", "Brejões"),
    "MUNICIPIO DE BREJO|MA": ("2102101", "Brejo"),
    "MUNICIPIO DE BREU BRANCO|PA": ("1501782", "Breu Branco"),
    "MUNICIPIO DE BREVES|PA": ("1501808", "Breves"),
    "MUNICIPIO DE BRITANIA|GO": ("5203807", "Britânia"),
    "MUNICIPIO DE BROCHIER|RS": ("4302659", "Brochier"),
    "MUNICIPIO DE BROTAS|SP": ("3507902", "Brotas"),
    "MUNICIPIO DE BRUMADINHO|MG": ("3109006", "Brumadinho"),
    "MUNICIPIO DE BRUMADO|BA": ("2904605", "Brumado"),
    "MUNICIPIO DE BRUNOPOLIS|SC": ("4202875", "Brunópolis"),
    "MUNICIPIO DE BUENO BRANDAO|MG": ("3109105", "Bueno Brandão"),
    "MUNICIPIO DE BUENOPOLIS|MG": ("3109204", "Buenópolis"),
    "MUNICIPIO DE BUENOS AIRES|PE": ("2602704", "Buenos Aires"),
    "MUNICIPIO DE BUGRE|MG": ("3109253", "Bugre"),
    "MUNICIPIO DE BUIQUE|PE": ("2602803", "Buíque"),
    "MUNICIPIO DE BUJARI|AC": ("1200138", "Bujari"),
    "MUNICIPIO DE BUJARU|PA": ("1501907", "Bujaru"),
    "MUNICIPIO DE BURITI ALEGRE|GO": ("5203906", "Buriti Alegre"),
    "MUNICIPIO DE BURITI DE GOIAS|GO": ("5203939", "Buriti de Goiás"),
    "MUNICIPIO DE BURITI DO TOCANTINS|TO": ("1703800", "Buriti do Tocantins"),
    "MUNICIPIO DE BURITI DOS LOPES|PI": ("2202000", "Buriti dos Lopes"),
    "MUNICIPIO DE BURITI DOS MONTES|PI": ("2202026", "Buriti dos Montes"),
    "MUNICIPIO DE BURITICUPU|MA": ("2102325", "Buriticupu"),
    "MUNICIPIO DE BURITINOPOLIS|GO": ("5203962", "Buritinópolis"),
    "MUNICIPIO DE BURITIRANA|MA": ("2102358", "Buritirana"),
    "MUNICIPIO DE BURITIS|MG": ("3109303", "Buritis"),
    "MUNICIPIO DE BURITIS|RO": ("1100452", "Buritis"),
    "MUNICIPIO DE BURITIZEIRO|MG": ("3109402", "Buritizeiro"),
    "MUNICIPIO DE BURITI|MA": ("2102200", "Buriti"),
    "MUNICIPIO DE BUTIA|RS": ("4302709", "Butiá"),
    "MUNICIPIO DE CAARAPO|MS": ("5002407", "Caarapó"),
    "MUNICIPIO DE CAATIBA|BA": ("2904803", "Caatiba"),
    "MUNICIPIO DE CABACEIRAS DO PARAGUACU|BA": ("2904852", "Cabaceiras do Paraguaçu"),
    "MUNICIPIO DE CABACEIRAS|PB": ("2503100", "Cabaceiras"),
    "MUNICIPIO DE CABECEIRA GRANDE|MG": ("3109451", "Cabeceira Grande"),
    "MUNICIPIO DE CABECEIRAS DO PIAUI|PI": ("2202059", "Cabeceiras do Piauí"),
    "MUNICIPIO DE CABECEIRAS|GO": ("5204003", "Cabeceiras"),
    "MUNICIPIO DE CABEDELO|PB": ("2503209", "Cabedelo"),
    "MUNICIPIO DE CABIXI|RO": ("1100031", "Cabixi"),
    "MUNICIPIO DE CABO FRIO|RJ": ("3300704", "Cabo Frio"),
    "MUNICIPIO DE CABO VERDE|MG": ("3109501", "Cabo Verde"),
    "MUNICIPIO DE CABRALIA PAULISTA|SP": ("3508306", "Cabrália Paulista"),
    "MUNICIPIO DE CABREUVA|SP": ("3508405", "Cabreúva"),
    "MUNICIPIO DE CABROBO|PE": ("2603009", "Cabrobó"),
    "MUNICIPIO DE CACADOR|SC": ("4203006", "Caçador"),
    "MUNICIPIO DE CACAPAVA DO SUL|RS": ("4302808", "Caçapava do Sul"),
    "MUNICIPIO DE CACAPAVA|SP": ("3508504", "Caçapava"),
    "MUNICIPIO DE CACAULANDIA|RO": ("1100601", "Cacaulândia"),
    "MUNICIPIO DE CACEQUI|RS": ("4302907", "Cacequi"),
    "MUNICIPIO DE CACERES|MT": ("5102504", "Cáceres"),
    "MUNICIPIO DE CACHOEIRA DE GOIAS|GO": ("5204201", "Cachoeira de Goiás"),
    "MUNICIPIO DE CACHOEIRA DE MINAS|MG": ("3109709", "Cachoeira de Minas"),
    "MUNICIPIO DE CACHOEIRA DE PAJEU|MG": ("3102704", "Cachoeira de Pajeú"),
    "MUNICIPIO DE CACHOEIRA DO PIRIA|PA": ("1501956", "Cachoeira do Piriá"),
    "MUNICIPIO DE CACHOEIRA DO SUL|RS": ("4303004", "Cachoeira do Sul"),
    "MUNICIPIO DE CACHOEIRA DOS INDIOS|PB": ("2503308", "Cachoeira dos Índios"),
    "MUNICIPIO DE CACHOEIRA DOURADA|GO": ("5204250", "Cachoeira Dourada"),
    "MUNICIPIO DE CACHOEIRA DOURADA|MG": ("3109808", "Cachoeira Dourada"),
    "MUNICIPIO DE CACHOEIRA GRANDE|MA": ("2102374", "Cachoeira Grande"),
    "MUNICIPIO DE CACHOEIRA PAULISTA|SP": ("3508603", "Cachoeira Paulista"),
    "MUNICIPIO DE CACHOEIRAS DE MACACU|RJ": ("3300803", "Cachoeiras de Macacu"),
    "MUNICIPIO DE CACHOEIRINHA|PE": ("2603108", "Cachoeirinha"),
    "MUNICIPIO DE CACHOEIRINHA|RS": ("4303103", "Cachoeirinha"),
    "MUNICIPIO DE CACHOEIRINHA|TO": ("1703826", "Cachoeirinha"),
    "MUNICIPIO DE CACHOEIRO DE ITAPEMIRIM|ES": ("3201209", "Cachoeiro de Itapemirim"),
    "MUNICIPIO DE CACIMBA DE DENTRO|PB": ("2503506", "Cacimba de Dentro"),
    "MUNICIPIO DE CACIMBAS|PB": ("2503555", "Cacimbas"),
    "MUNICIPIO DE CACIMBINHAS|AL": ("2701209", "Cacimbinhas"),
    "MUNICIPIO DE CACIQUE DOBLE|RS": ("4303202", "Cacique Doble"),
    "MUNICIPIO DE CACOAL|RO": ("1100049", "Cacoal"),
    "MUNICIPIO DE CACULE|BA": ("2905008", "Caculé"),
    "MUNICIPIO DE CACU|GO": ("5204300", "Caçu"),
    "MUNICIPIO DE CAEM|BA": ("2905107", "Caém"),
    "MUNICIPIO DE CAETANOPOLIS|MG": ("3109907", "Caetanópolis"),
    "MUNICIPIO DE CAETANOS|BA": ("2905156", "Caetanos"),
    "MUNICIPIO DE CAETES|PE": ("2603207", "Caetés"),
    "MUNICIPIO DE CAETE|MG": ("3110004", "Caeté"),
    "MUNICIPIO DE CAETITE|BA": ("2905206", "Caetité"),
    "MUNICIPIO DE CAFEARA|PR": ("4103404", "Cafeara"),
    "MUNICIPIO DE CAFELANDIA|PR": ("4103453", "Cafelândia"),
    "MUNICIPIO DE CAFELANDIA|SP": ("3508801", "Cafelândia"),
    "MUNICIPIO DE CAFEZAL DO SUL|PR": ("4103479", "Cafezal do Sul"),
    "MUNICIPIO DE CAIABU|SP": ("3508900", "Caiabu"),
    "MUNICIPIO DE CAIANA|MG": ("3110103", "Caiana"),
    "MUNICIPIO DE CAIAPONIA|GO": ("5204409", "Caiapônia"),
    "MUNICIPIO DE CAIBATE|RS": ("4303301", "Caibaté"),
    "MUNICIPIO DE CAIBI|SC": ("4203105", "Caibi"),
    "MUNICIPIO DE CAICARA DO NORTE|RN": ("2401859", "Caiçara do Norte"),
    "MUNICIPIO DE CAICARA DO RIO DO VENTO|RN": ("2401909", "Caiçara do Rio do Vento"),
    "MUNICIPIO DE CAICARA|PB": ("2503605", "Caiçara"),
    "MUNICIPIO DE CAICARA|RS": ("4303400", "Caiçara"),
    "MUNICIPIO DE CAICO|RN": ("2402006", "Caicó"),
    "MUNICIPIO DE CAIEIRAS|SP": ("3509007", "Caieiras"),
    "MUNICIPIO DE CAIRU|BA": ("2905404", "Cairu"),
    "MUNICIPIO DE CAIUA|SP": ("3509106", "Caiuá"),
    "MUNICIPIO DE CAJAMAR|SP": ("3509205", "Cajamar"),
    "MUNICIPIO DE CAJARI|MA": ("2102507", "Cajari"),
    "MUNICIPIO DE CAJAZEIRAS DO PIAUI|PI": ("2202075", "Cajazeiras do Piauí"),
    "MUNICIPIO DE CAJAZEIRAS|PB": ("2503704", "Cajazeiras"),
    "MUNICIPIO DE CAJOBI|SP": ("3509304", "Cajobi"),
    "MUNICIPIO DE CAJUEIRO DA PRAIA|PI": ("2202083", "Cajueiro da Praia"),
    "MUNICIPIO DE CAJURI|MG": ("3110202", "Cajuri"),
    "MUNICIPIO DE CAJURU|SP": ("3509403", "Cajuru"),
    "MUNICIPIO DE CALCADO|PE": ("2603306", "Calçado"),
    "MUNICIPIO DE CALCOENE|AP": ("1600204", "Calçoene"),
    "MUNICIPIO DE CALDAS NOVAS|GO": ("5204508", "Caldas Novas"),
    "MUNICIPIO DE CALDAS|MG": ("3110301", "Caldas"),
    "MUNICIPIO DE CALDAZINHA|GO": ("5204557", "Caldazinha"),
    "MUNICIPIO DE CALDEIRAO GRANDE|BA": ("2905503", "Caldeirão Grande"),
    "MUNICIPIO DE CALIFORNIA|PR": ("4103503", "Califórnia"),
    "MUNICIPIO DE CALMON|SC": ("4203154", "Calmon"),
    "MUNICIPIO DE CALUMBI|PE": ("2603405", "Calumbi"),
    "MUNICIPIO DE CAMACAN|BA": ("2905602", "Camacan"),
    "MUNICIPIO DE CAMACHO|MG": ("3110400", "Camacho"),
    "MUNICIPIO DE CAMALAU|PB": ("2503902", "Camalaú"),
    "MUNICIPIO DE CAMAMU|BA": ("2905800", "Camamu"),
    "MUNICIPIO DE CAMANDUCAIA|MG": ("3110509", "Camanducaia"),
    "MUNICIPIO DE CAMAPUA|MS": ("5002605", "Camapuã"),
    "MUNICIPIO DE CAMAQUA|RS": ("4303509", "Camaquã"),
    "MUNICIPIO DE CAMARAGIBE|PE": ("2603454", "Camaragibe"),
    "MUNICIPIO DE CAMARGO|RS": ("4303558", "Camargo"),
    "MUNICIPIO DE CAMBARA|PR": ("4103602", "Cambará"),
    "MUNICIPIO DE CAMBE|PR": ("4103701", "Cambé"),
    "MUNICIPIO DE CAMBORIU|SC": ("4203204", "Camboriú"),
    "MUNICIPIO DE CAMBUCI|RJ": ("3300902", "Cambuci"),
    "MUNICIPIO DE CAMBUI|MG": ("3110608", "Cambuí"),
    "MUNICIPIO DE CAMBUQUIRA|MG": ("3110707", "Cambuquira"),
    "MUNICIPIO DE CAMETA|PA": ("1502103", "Cametá"),
    "MUNICIPIO DE CAMOCIM DE SAO FELIX|PE": ("2603504", "Camocim de São Félix"),
    "MUNICIPIO DE CAMOCIM|CE": ("2302602", "Camocim"),
    "MUNICIPIO DE CAMPANARIO|MG": ("3110806", "Campanário"),
    "MUNICIPIO DE CAMPANHA|MG": ("3110905", "Campanha"),
    "MUNICIPIO DE CAMPESTRE DA SERRA|RS": ("4303673", "Campestre da Serra"),
    "MUNICIPIO DE CAMPESTRE DE GOIAS|GO": ("5204607", "Campestre de Goiás"),
    "MUNICIPIO DE CAMPESTRE DO MARANHAO|MA": ("2102556", "Campestre do Maranhão"),
    "MUNICIPIO DE CAMPESTRE|AL": ("2701357", "Campestre"),
    "MUNICIPIO DE CAMPESTRE|MG": ("3111002", "Campestre"),
    "MUNICIPIO DE CAMPINA DA LAGOA|PR": ("4103909", "Campina da Lagoa"),
    "MUNICIPIO DE CAMPINA DAS MISSOES|RS": ("4303707", "Campina das Missões"),
    "MUNICIPIO DE CAMPINA DO SIMAO|PR": ("4103958", "Campina do Simão"),
    "MUNICIPIO DE CAMPINA GRANDE DO SUL|PR": ("4104006", "Campina Grande do Sul"),
    "MUNICIPIO DE CAMPINA GRANDE|PB": ("2504009", "Campina Grande"),
    "MUNICIPIO DE CAMPINA VERDE|MG": ("3111101", "Campina Verde"),
    "MUNICIPIO DE CAMPINACU|GO": ("5204656", "Campinaçu"),
    "MUNICIPIO DE CAMPINAPOLIS|MT": ("5102603", "Campinápolis"),
    "MUNICIPIO DE CAMPINAS DO PIAUI|PI": ("2202109", "Campinas do Piauí"),
    "MUNICIPIO DE CAMPINAS DO SUL|RS": ("4303806", "Campinas do Sul"),
    "MUNICIPIO DE CAMPINAS|SP": ("3509502", "Campinas"),
    "MUNICIPIO DE CAMPINORTE|GO": ("5204706", "Campinorte"),
    "MUNICIPIO DE CAMPO ALEGRE DE GOIAS|GO": ("5204805", "Campo Alegre de Goiás"),
    "MUNICIPIO DE CAMPO ALEGRE DE LOURDES|BA": ("2905909", "Campo Alegre de Lourdes"),
    "MUNICIPIO DE CAMPO ALEGRE|AL": ("2701407", "Campo Alegre"),
    "MUNICIPIO DE CAMPO ALEGRE|SC": ("4203303", "Campo Alegre"),
    "MUNICIPIO DE CAMPO AZUL|MG": ("3111150", "Campo Azul"),
    "MUNICIPIO DE CAMPO BOM|RS": ("4303905", "Campo Bom"),
    "MUNICIPIO DE CAMPO BONITO|PR": ("4104055", "Campo Bonito"),
    "MUNICIPIO DE CAMPO DO BRITO|SE": ("2801009", "Campo do Brito"),
    "MUNICIPIO DE CAMPO DO MEIO|MG": ("3111309", "Campo do Meio"),
    "MUNICIPIO DE CAMPO ERE|SC": ("4203501", "Campo Erê"),
    "MUNICIPIO DE CAMPO FLORIDO|MG": ("3111408", "Campo Florido"),
    "MUNICIPIO DE CAMPO FORMOSO|BA": ("2906006", "Campo Formoso"),
    "MUNICIPIO DE CAMPO GRANDE DO PIAUI|PI": ("2202133", "Campo Grande do Piauí"),
    "MUNICIPIO DE CAMPO GRANDE|AL": ("2701506", "Campo Grande"),
    "MUNICIPIO DE CAMPO GRANDE|MS": ("5002704", "Campo Grande"),
    "MUNICIPIO DE CAMPO GRANDE|RN": ("2401305", "Campo Grande"),
    "MUNICIPIO DE CAMPO LARGO DO PIAUI|PI": ("2202174", "Campo Largo do Piauí"),
    "MUNICIPIO DE CAMPO LARGO|PR": ("4104204", "Campo Largo"),
    "MUNICIPIO DE CAMPO LIMPO DE GOIAS|GO": ("5204854", "Campo Limpo de Goiás"),
    "MUNICIPIO DE CAMPO LIMPO PAULISTA|SP": ("3509601", "Campo Limpo Paulista"),
    "MUNICIPIO DE CAMPO MAGRO|PR": ("4104253", "Campo Magro"),
    "MUNICIPIO DE CAMPO MAIOR|PI": ("2202208", "Campo Maior"),
    "MUNICIPIO DE CAMPO MOURAO|PR": ("4104303", "Campo Mourão"),
    "MUNICIPIO DE CAMPO NOVO DE RONDONIA|RO": ("1100700", "Campo Novo de Rondônia"),
    "MUNICIPIO DE CAMPO NOVO DO PARECIS|MT": ("5102637", "Campo Novo do Parecis"),
    "MUNICIPIO DE CAMPO NOVO|RS": ("4304002", "Campo Novo"),
    "MUNICIPIO DE CAMPO REDONDO|RN": ("2402105", "Campo Redondo"),
    "MUNICIPIO DE CAMPO VERDE|MT": ("5102678", "Campo Verde"),
    "MUNICIPIO DE CAMPOS ALTOS|MG": ("3111507", "Campos Altos"),
    "MUNICIPIO DE CAMPOS BELOS|GO": ("5204904", "Campos Belos"),
    "MUNICIPIO DE CAMPOS BORGES|RS": ("4304101", "Campos Borges"),
    "MUNICIPIO DE CAMPOS DO JORDAO|SP": ("3509700", "Campos do Jordão"),
    "MUNICIPIO DE CAMPOS DOS GOYTACAZES|RJ": ("3301009", "Campos dos Goytacazes"),
    "MUNICIPIO DE CAMPOS GERAIS|MG": ("3111606", "Campos Gerais"),
    "MUNICIPIO DE CAMPOS LINDOS|TO": ("1703842", "Campos Lindos"),
    "MUNICIPIO DE CAMPOS NOVOS PAULISTA|SP": ("3509809", "Campos Novos Paulista"),
    "MUNICIPIO DE CAMPOS NOVOS|SC": ("4203600", "Campos Novos"),
    "MUNICIPIO DE CAMPOS SALES|CE": ("2302701", "Campos Sales"),
    "MUNICIPIO DE CAMPOS VERDES|GO": ("5204953", "Campos Verdes"),
    "MUNICIPIO DE CANA VERDE|MG": ("3111903", "Cana Verde"),
    "MUNICIPIO DE CANAA|MG": ("3111705", "Canaã"),
    "MUNICIPIO DE CANAPI|AL": ("2701605", "Canapi"),
    "MUNICIPIO DE CANAPOLIS|BA": ("2906105", "Canápolis"),
    "MUNICIPIO DE CANAPOLIS|MG": ("3111804", "Canápolis"),
    "MUNICIPIO DE CANARANA|BA": ("2906204", "Canarana"),
    "MUNICIPIO DE CANARANA|MT": ("5102702", "Canarana"),
    "MUNICIPIO DE CANAS|SP": ("3509957", "Canas"),
    "MUNICIPIO DE CANAVIEIRAS|BA": ("2906303", "Canavieiras"),
    "MUNICIPIO DE CANDEAL|BA": ("2906402", "Candeal"),
    "MUNICIPIO DE CANDEIAS DO JAMARI|RO": ("1100809", "Candeias do Jamari"),
    "MUNICIPIO DE CANDEIAS|BA": ("2906501", "Candeias"),
    "MUNICIPIO DE CANDEIAS|MG": ("3112000", "Candeias"),
    "MUNICIPIO DE CANDELARIA|RS": ("4304200", "Candelária"),
    "MUNICIPIO DE CANDIBA|BA": ("2906600", "Candiba"),
    "MUNICIPIO DE CANDIDO DE ABREU|PR": ("4104402", "Cândido de Abreu"),
    "MUNICIPIO DE CANDIDO GODOI|RS": ("4304309", "Cândido Godói"),
    "MUNICIPIO DE CANDIDO MOTA|SP": ("3510005", "Cândido Mota"),
    "MUNICIPIO DE CANDIDO RODRIGUES|SP": ("3510104", "Cândido Rodrigues"),
    "MUNICIPIO DE CANDIDO SALES|BA": ("2906709", "Cândido Sales"),
    "MUNICIPIO DE CANDIOTA|RS": ("4304358", "Candiota"),
    "MUNICIPIO DE CANDOI|PR": ("4104428", "Candói"),
    "MUNICIPIO DE CANELA|RS": ("4304408", "Canela"),
    "MUNICIPIO DE CANELINHA|SC": ("4203709", "Canelinha"),
    "MUNICIPIO DE CANGUARETAMA|RN": ("2402204", "Canguaretama"),
    "MUNICIPIO DE CANGUCU|RS": ("4304507", "Canguçu"),
    "MUNICIPIO DE CANHOBA|SE": ("2801108", "Canhoba"),
    "MUNICIPIO DE CANINDE DE SAO FRANCISCO|SE": ("2801207", "Canindé de São Francisco"),
    "MUNICIPIO DE CANINDE|CE": ("2302800", "Canindé"),
    "MUNICIPIO DE CANITAR|SP": ("3510153", "Canitar"),
    "MUNICIPIO DE CANOAS|RS": ("4304606", "Canoas"),
    "MUNICIPIO DE CANOINHAS|SC": ("4203808", "Canoinhas"),
    "MUNICIPIO DE CANSANCAO|BA": ("2906808", "Cansanção"),
    "MUNICIPIO DE CANTAGALO|MG": ("3112059", "Cantagalo"),
    "MUNICIPIO DE CANTAGALO|PR": ("4104451", "Cantagalo"),
    "MUNICIPIO DE CANTAGALO|RJ": ("3301108", "Cantagalo"),
    "MUNICIPIO DE CANTANHEDE|MA": ("2102705", "Cantanhede"),
    "MUNICIPIO DE CANTA|RR": ("1400175", "Cantá"),
    "MUNICIPIO DE CANUDOS DO VALE|RS": ("4304614", "Canudos do Vale"),
    "MUNICIPIO DE CANUDOS|BA": ("2906824", "Canudos"),
    "MUNICIPIO DE CAPANEMA|PA": ("1502202", "Capanema"),
    "MUNICIPIO DE CAPANEMA|PR": ("4104501", "Capanema"),
    "MUNICIPIO DE CAPAO ALTO|SC": ("4203253", "Capão Alto"),
    "MUNICIPIO DE CAPAO BONITO|SP": ("3510203", "Capão Bonito"),
    "MUNICIPIO DE CAPAO DA CANOA|RS": ("4304630", "Capão da Canoa"),
    "MUNICIPIO DE CAPAO DO CIPO|RS": ("4304655", "Capão do Cipó"),
    "MUNICIPIO DE CAPAO DO LEAO|RS": ("4304663", "Capão do Leão"),
    "MUNICIPIO DE CAPARAO|MG": ("3112109", "Caparaó"),
    "MUNICIPIO DE CAPELA DE SANTANA|RS": ("4304689", "Capela de Santana"),
    "MUNICIPIO DE CAPELA DO ALTO ALEGRE|BA": ("2906857", "Capela do Alto Alegre"),
    "MUNICIPIO DE CAPELA DO ALTO|SP": ("3510302", "Capela do Alto"),
    "MUNICIPIO DE CAPELA NOVA|MG": ("3112208", "Capela Nova"),
    "MUNICIPIO DE CAPELA|AL": ("2701704", "Capela"),
    "MUNICIPIO DE CAPELA|SE": ("2801306", "Capela"),
    "MUNICIPIO DE CAPELINHA|MG": ("3112307", "Capelinha"),
    "MUNICIPIO DE CAPIM BRANCO|MG": ("3112505", "Capim Branco"),
    "MUNICIPIO DE CAPIM GROSSO|BA": ("2906873", "Capim Grosso"),
    "MUNICIPIO DE CAPIM|PB": ("2504033", "Capim"),
    "MUNICIPIO DE CAPINOPOLIS|MG": ("3112604", "Capinópolis"),
    "MUNICIPIO DE CAPINZAL|SC": ("4203907", "Capinzal"),
    "MUNICIPIO DE CAPITAO ANDRADE|MG": ("3112653", "Capitão Andrade"),
    "MUNICIPIO DE CAPITAO DE CAMPOS|PI": ("2202406", "Capitão de Campos"),
    "MUNICIPIO DE CAPITAO ENEAS|MG": ("3112703", "Capitão Enéas"),
    "MUNICIPIO DE CAPITAO GERVASIO OLIVEIRA|PI": ("2202455", "Capitão Gervásio Oliveira"),
    "MUNICIPIO DE CAPITAO LEONIDAS MARQUES|PR": ("4104600", "Capitão Leônidas Marques"),
    "MUNICIPIO DE CAPITAO POCO|PA": ("1502301", "Capitão Poço"),
    "MUNICIPIO DE CAPITAO|RS": ("4304697", "Capitão"),
    "MUNICIPIO DE CAPITOLIO|MG": ("3112802", "Capitólio"),
    "MUNICIPIO DE CAPIVARI DE BAIXO|SC": ("4203956", "Capivari de Baixo"),
    "MUNICIPIO DE CAPIVARI DO SUL|RS": ("4304671", "Capivari do Sul"),
    "MUNICIPIO DE CAPIVARI|SP": ("3510401", "Capivari"),
    "MUNICIPIO DE CAPIXABA|AC": ("1200179", "Capixaba"),
    "MUNICIPIO DE CARAA|RS": ("4304713", "Caraá"),
    "MUNICIPIO DE CARACARAI|RR": ("1400209", "Caracaraí"),
    "MUNICIPIO DE CARACOL|MS": ("5002803", "Caracol"),
    "MUNICIPIO DE CARACOL|PI": ("2202505", "Caracol"),
    "MUNICIPIO DE CARAGUATATUBA|SP": ("3510500", "Caraguatatuba"),
    "MUNICIPIO DE CARAIBAS|BA": ("2906899", "Caraíbas"),
    "MUNICIPIO DE CARAI|MG": ("3113008", "Caraí"),
    "MUNICIPIO DE CARAMBEI|PR": ("4104659", "Carambeí"),
    "MUNICIPIO DE CARANAIBA|MG": ("3113107", "Caranaíba"),
    "MUNICIPIO DE CARANDAI|MG": ("3113206", "Carandaí"),
    "MUNICIPIO DE CARANGOLA|MG": ("3113305", "Carangola"),
    "MUNICIPIO DE CARAPEBUS|RJ": ("3300936", "Carapebus"),
    "MUNICIPIO DE CARAPICUIBA|SP": ("3510609", "Carapicuíba"),
    "MUNICIPIO DE CARATINGA|MG": ("3113404", "Caratinga"),
    "MUNICIPIO DE CARAUARI|AM": ("1301001", "Carauari"),
    "MUNICIPIO DE CARAUBAS|PB": ("2504074", "Caraúbas"),
    "MUNICIPIO DE CARAUBAS|RN": ("2402303", "Caraúbas"),
    "MUNICIPIO DE CARAVELAS|BA": ("2906907", "Caravelas"),
    "MUNICIPIO DE CARAZINHO|RS": ("4304705", "Carazinho"),
    "MUNICIPIO DE CARBONITA|MG": ("3113503", "Carbonita"),
    "MUNICIPIO DE CARDEAL DA SILVA|BA": ("2907004", "Cardeal da Silva"),
    "MUNICIPIO DE CARDOSO MOREIRA|RJ": ("3301157", "Cardoso Moreira"),
    "MUNICIPIO DE CARDOSO|SP": ("3510708", "Cardoso"),
    "MUNICIPIO DE CAREACU|MG": ("3113602", "Careaçu"),
    "MUNICIPIO DE CAREIRO DA VARZEA|AM": ("1301159", "Careiro da Várzea"),
    "MUNICIPIO DE CAREIRO|AM": ("1301100", "Careiro"),
    "MUNICIPIO DE CARIACICA|ES": ("3201308", "Cariacica"),
    "MUNICIPIO DE CARIDADE DO PIAUI|PI": ("2202554", "Caridade do Piauí"),
    "MUNICIPIO DE CARINHANHA|BA": ("2907103", "Carinhanha"),
    "MUNICIPIO DE CARIRA|SE": ("2801405", "Carira"),
    "MUNICIPIO DE CARIRE|CE": ("2303105", "Cariré"),
    "MUNICIPIO DE CARIRI DO TOCANTINS|TO": ("1703867", "Cariri do Tocantins"),
    "MUNICIPIO DE CARIRIACU|CE": ("2303204", "Caririaçu"),
    "MUNICIPIO DE CARLINDA|MT": ("5102793", "Carlinda"),
    "MUNICIPIO DE CARLOPOLIS|PR": ("4104709", "Carlópolis"),
    "MUNICIPIO DE CARLOS BARBOSA|RS": ("4304804", "Carlos Barbosa"),
    "MUNICIPIO DE CARLOS GOMES|RS": ("4304853", "Carlos Gomes"),
    "MUNICIPIO DE CARMESIA|MG": ("3113800", "Carmésia"),
    "MUNICIPIO DE CARMO DA CACHOEIRA|MG": ("3113909", "Carmo da Cachoeira"),
    "MUNICIPIO DE CARMO DA MATA|MG": ("3114006", "Carmo da Mata"),
    "MUNICIPIO DE CARMO DE MINAS|MG": ("3114105", "Carmo de Minas"),
    "MUNICIPIO DE CARMO DO CAJURU|MG": ("3114204", "Carmo do Cajuru"),
    "MUNICIPIO DE CARMO DO PARANAIBA|MG": ("3114303", "Carmo do Paranaíba"),
    "MUNICIPIO DE CARMO DO RIO CLARO|MG": ("3114402", "Carmo do Rio Claro"),
    "MUNICIPIO DE CARMO DO RIO VERDE|GO": ("5205000", "Carmo do Rio Verde"),
    "MUNICIPIO DE CARMOLANDIA|TO": ("1703883", "Carmolândia"),
    "MUNICIPIO DE CARMOPOLIS DE MINAS|MG": ("3114501", "Carmópolis de Minas"),
    "MUNICIPIO DE CARMOPOLIS|SE": ("2801504", "Carmópolis"),
    "MUNICIPIO DE CARMO|RJ": ("3301207", "Carmo"),
    "MUNICIPIO DE CARNAIBA|PE": ("2603900", "Carnaíba"),
    "MUNICIPIO DE CARNAUBAIS|RN": ("2402501", "Carnaubais"),
    "MUNICIPIO DE CARNAUBAL|CE": ("2303402", "Carnaubal"),
    "MUNICIPIO DE CARNAUBEIRA DA PENHA|PE": ("2603926", "Carnaubeira da Penha"),
    "MUNICIPIO DE CARNEIRINHO|MG": ("3114550", "Carneirinho"),
    "MUNICIPIO DE CARNEIROS|AL": ("2701803", "Carneiros"),
    "MUNICIPIO DE CAROEBE|RR": ("1400233", "Caroebe"),
    "MUNICIPIO DE CAROLINA|MA": ("2102804", "Carolina"),
    "MUNICIPIO DE CARPINA|PE": ("2604007", "Carpina"),
    "MUNICIPIO DE CARRANCAS|MG": ("3114600", "Carrancas"),
    "MUNICIPIO DE CARRASCO BONITO|TO": ("1703891", "Carrasco Bonito"),
    "MUNICIPIO DE CARUARU|PE": ("2604106", "Caruaru"),
    "MUNICIPIO DE CARUTAPERA|MA": ("2102903", "Carutapera"),
    "MUNICIPIO DE CARVALHOPOLIS|MG": ("3114709", "Carvalhópolis"),
    "MUNICIPIO DE CARVALHOS|MG": ("3114808", "Carvalhos"),
    "MUNICIPIO DE CASA BRANCA|SP": ("3510807", "Casa Branca"),
    "MUNICIPIO DE CASA NOVA|BA": ("2907202", "Casa Nova"),
    "MUNICIPIO DE CASCAVEL|CE": ("2303501", "Cascavel"),
    "MUNICIPIO DE CASCAVEL|PR": ("4104808", "Cascavel"),
    "MUNICIPIO DE CASCA|RS": ("4304903", "Casca"),
    "MUNICIPIO DE CASEARA|TO": ("1703909", "Caseara"),
    "MUNICIPIO DE CASEIROS|RS": ("4304952", "Caseiros"),
    "MUNICIPIO DE CASIMIRO DE ABREU|RJ": ("3301306", "Casimiro de Abreu"),
    "MUNICIPIO DE CASINHAS|PE": ("2604155", "Casinhas"),
    "MUNICIPIO DE CASSILANDIA|MS": ("5002902", "Cassilândia"),
    "MUNICIPIO DE CASTANHAL|PA": ("1502400", "Castanhal"),
    "MUNICIPIO DE CASTANHEIRAS|RO": ("1100908", "Castanheiras"),
    "MUNICIPIO DE CASTELANDIA|GO": ("5205059", "Castelândia"),
    "MUNICIPIO DE CASTELO DO PIAUI|PI": ("2202604", "Castelo do Piauí"),
    "MUNICIPIO DE CASTELO|ES": ("3201407", "Castelo"),
    "MUNICIPIO DE CASTILHO|SP": ("3511003", "Castilho"),
    "MUNICIPIO DE CASTRO ALVES|BA": ("2907301", "Castro Alves"),
    "MUNICIPIO DE CASTRO|PR": ("4104907", "Castro"),
    "MUNICIPIO DE CATAGUASES|MG": ("3115300", "Cataguases"),
    "MUNICIPIO DE CATALAO|GO": ("5205109", "Catalão"),
    "MUNICIPIO DE CATANDUVAS|PR": ("4105003", "Catanduvas"),
    "MUNICIPIO DE CATANDUVAS|SC": ("4204004", "Catanduvas"),
    "MUNICIPIO DE CATANDUVA|SP": ("3511102", "Catanduva"),
    "MUNICIPIO DE CATARINA|CE": ("2303600", "Catarina"),
    "MUNICIPIO DE CATAS ALTAS DA NORUEGA|MG": ("3115409", "Catas Altas da Noruega"),
    "MUNICIPIO DE CATAS ALTAS|MG": ("3115359", "Catas Altas"),
    "MUNICIPIO DE CATENDE|PE": ("2604205", "Catende"),
    "MUNICIPIO DE CATIGUA|SP": ("3511201", "Catiguá"),
    "MUNICIPIO DE CATINGUEIRA|PB": ("2504207", "Catingueira"),
    "MUNICIPIO DE CATUIPE|RS": ("4305009", "Catuípe"),
    "MUNICIPIO DE CATUJI|MG": ("3115458", "Catuji"),
    "MUNICIPIO DE CATUNDA|CE": ("2303659", "Catunda"),
    "MUNICIPIO DE CATURAI|GO": ("5205208", "Caturaí"),
    "MUNICIPIO DE CATURITE|PB": ("2504355", "Caturité"),
    "MUNICIPIO DE CATUTI|MG": ("3115474", "Catuti"),
    "MUNICIPIO DE CATU|BA": ("2907509", "Catu"),
    "MUNICIPIO DE CAVALCANTE|GO": ("5205307", "Cavalcante"),
    "MUNICIPIO DE CAXAMBU DO SUL|SC": ("4204103", "Caxambu do Sul"),
    "MUNICIPIO DE CAXAMBU|MG": ("3115508", "Caxambu"),
    "MUNICIPIO DE CAXIAS DO SUL|RS": ("4305108", "Caxias do Sul"),
    "MUNICIPIO DE CEARA-MIRIM|RN": ("2402600", "Ceará-Mirim"),
    "MUNICIPIO DE CEDRAL|MA": ("2103109", "Cedral"),
    "MUNICIPIO DE CEDRAL|SP": ("3511300", "Cedral"),
    "MUNICIPIO DE CEDRO DE SAO JOAO|SE": ("2801603", "Cedro de São João"),
    "MUNICIPIO DE CEDRO DO ABAETE|MG": ("3115607", "Cedro do Abaeté"),
    "MUNICIPIO DE CELSO RAMOS|SC": ("4204152", "Celso Ramos"),
    "MUNICIPIO DE CENTENARIO DO SUL|PR": ("4105102", "Centenário do Sul"),
    "MUNICIPIO DE CENTENARIO|RS": ("4305116", "Centenário"),
    "MUNICIPIO DE CENTENARIO|TO": ("1704105", "Centenário"),
    "MUNICIPIO DE CENTRAL DE MINAS|MG": ("3115706", "Central de Minas"),
    "MUNICIPIO DE CENTRAL DO MARANHAO|MA": ("2103125", "Central do Maranhão"),
    "MUNICIPIO DE CENTRALINA|MG": ("3115805", "Centralina"),
    "MUNICIPIO DE CENTRAL|BA": ("2907608", "Central"),
    "MUNICIPIO DE CENTRO DO GUILHERME|MA": ("2103158", "Centro do Guilherme"),
    "MUNICIPIO DE CENTRO NOVO DO MARANHAO|MA": ("2103174", "Centro Novo do Maranhão"),
    "MUNICIPIO DE CEREJEIRAS|RO": ("1100056", "Cerejeiras"),
    "MUNICIPIO DE CERES|GO": ("5205406", "Ceres"),
    "MUNICIPIO DE CERQUEIRA CESAR|SP": ("3511409", "Cerqueira César"),
    "MUNICIPIO DE CERQUILHO|SP": ("3511508", "Cerquilho"),
    "MUNICIPIO DE CERRITO|RS": ("4305124", "Cerrito"),
    "MUNICIPIO DE CERRO AZUL|PR": ("4105201", "Cerro Azul"),
    "MUNICIPIO DE CERRO BRANCO|RS": ("4305132", "Cerro Branco"),
    "MUNICIPIO DE CERRO GRANDE DO SUL|RS": ("4305173", "Cerro Grande do Sul"),
    "MUNICIPIO DE CERRO GRANDE|RS": ("4305157", "Cerro Grande"),
    "MUNICIPIO DE CERRO LARGO|RS": ("4305207", "Cerro Largo"),
    "MUNICIPIO DE CERRO NEGRO|SC": ("4204178", "Cerro Negro"),
    "MUNICIPIO DE CERRO-CORA|RN": ("2402709", "Cerro Corá"),
    "MUNICIPIO DE CEU AZUL|PR": ("4105300", "Céu Azul"),
    "MUNICIPIO DE CEZARINA|GO": ("5205455", "Cezarina"),
    "MUNICIPIO DE CHA DE ALEGRIA|PE": ("2604403", "Chã de Alegria"),
    "MUNICIPIO DE CHA GRANDE|PE": ("2604502", "Chã Grande"),
    "MUNICIPIO DE CHA PRETA|AL": ("2701902", "Chã Preta"),
    "MUNICIPIO DE CHACARA|MG": ("3115904", "Chácara"),
    "MUNICIPIO DE CHALE|MG": ("3116001", "Chalé"),
    "MUNICIPIO DE CHAPADA DA NATIVIDADE|TO": ("1705102", "Chapada da Natividade"),
    "MUNICIPIO DE CHAPADA DE AREIA|TO": ("1704600", "Chapada de Areia"),
    "MUNICIPIO DE CHAPADA DO NORTE|MG": ("3116100", "Chapada do Norte"),
    "MUNICIPIO DE CHAPADA GAUCHA|MG": ("3116159", "Chapada Gaúcha"),
    "MUNICIPIO DE CHAPADAO DO CEU|GO": ("5205471", "Chapadão do Céu"),
    "MUNICIPIO DE CHAPADAO DO LAGEADO|SC": ("4204194", "Chapadão do Lageado"),
    "MUNICIPIO DE CHAPADAO DO SUL|MS": ("5002951", "Chapadão do Sul"),
    "MUNICIPIO DE CHAPADA|RS": ("4305306", "Chapada"),
    "MUNICIPIO DE CHAPADINHA|MA": ("2103208", "Chapadinha"),
    "MUNICIPIO DE CHAPECO|SC": ("4204202", "Chapecó"),
    "MUNICIPIO DE CHARQUEADAS|RS": ("4305355", "Charqueadas"),
    "MUNICIPIO DE CHARRUA|RS": ("4305371", "Charrua"),
    "MUNICIPIO DE CHAVES|PA": ("1502509", "Chaves"),
    "MUNICIPIO DE CHIADOR|MG": ("3116209", "Chiador"),
    "MUNICIPIO DE CHOPINZINHO|PR": ("4105409", "Chopinzinho"),
    "MUNICIPIO DE CHOROZINHO|CE": ("2303956", "Chorozinho"),
    "MUNICIPIO DE CHORROCHO|BA": ("2907707", "Chorrochó"),
    "MUNICIPIO DE CHUI|RS": ("4305439", "Chuí"),
    "MUNICIPIO DE CHUPINGUAIA|RO": ("1100924", "Chupinguaia"),
    "MUNICIPIO DE CHUVISCA|RS": ("4305447", "Chuvisca"),
    "MUNICIPIO DE CIANORTE|PR": ("4105508", "Cianorte"),
    "MUNICIPIO DE CICERO DANTAS|BA": ("2907806", "Cícero Dantas"),
    "MUNICIPIO DE CIDADE GAUCHA|PR": ("4105607", "Cidade Gaúcha"),
    "MUNICIPIO DE CIDADE OCIDENTAL|GO": ("5205497", "Cidade Ocidental"),
    "MUNICIPIO DE CIDELANDIA|MA": ("2103257", "Cidelândia"),
    "MUNICIPIO DE CIDREIRA|RS": ("4305454", "Cidreira"),
    "MUNICIPIO DE CIPOTANEA|MG": ("3116308", "Cipotânea"),
    "MUNICIPIO DE CIPO|BA": ("2907905", "Cipó"),
    "MUNICIPIO DE CIRIACO|RS": ("4305504", "Ciríaco"),
    "MUNICIPIO DE CLARAVAL|MG": ("3116407", "Claraval"),
    "MUNICIPIO DE CLARO DOS POCOES|MG": ("3116506", "Claro dos Poções"),
    "MUNICIPIO DE CLAUDIO|MG": ("3116605", "Cláudio"),
    "MUNICIPIO DE CLEMENTINA|SP": ("3511904", "Clementina"),
    "MUNICIPIO DE CLEVELANDIA|PR": ("4105706", "Clevelândia"),
    "MUNICIPIO DE COARACI|BA": ("2908002", "Coaraci"),
    "MUNICIPIO DE COCAL DE TELHA|PI": ("2202711", "Cocal de Telha"),
    "MUNICIPIO DE COCAL DO SUL|SC": ("4204251", "Cocal do Sul"),
    "MUNICIPIO DE COCALZINHO DE GOIAS|GO": ("5205513", "Cocalzinho de Goiás"),
    "MUNICIPIO DE COCAL|PI": ("2202703", "Cocal"),
    "MUNICIPIO DE COCOS|BA": ("2908101", "Cocos"),
    "MUNICIPIO DE CODAJAS|AM": ("1301308", "Codajás"),
    "MUNICIPIO DE CODO|MA": ("2103307", "Codó"),
    "MUNICIPIO DE COELHO NETO|MA": ("2103406", "Coelho Neto"),
    "MUNICIPIO DE COIMBRA|MG": ("3116704", "Coimbra"),
    "MUNICIPIO DE COIVARAS|PI": ("2202737", "Coivaras"),
    "MUNICIPIO DE COLARES|PA": ("1502608", "Colares"),
    "MUNICIPIO DE COLATINA|ES": ("3201506", "Colatina"),
    "MUNICIPIO DE COLINAS DO SUL|GO": ("5205521", "Colinas do Sul"),
    "MUNICIPIO DE COLINAS|MA": ("2103505", "Colinas"),
    "MUNICIPIO DE COLINAS|RS": ("4305587", "Colinas"),
    "MUNICIPIO DE COLINA|SP": ("3512001", "Colina"),
    "MUNICIPIO DE COLMEIA|TO": ("1716703", "Colméia"),
    "MUNICIPIO DE COLNIZA|MT": ("5103254", "Colniza"),
    "MUNICIPIO DE COLOMBIA|SP": ("3512100", "Colômbia"),
    "MUNICIPIO DE COLOMBO|PR": ("4105805", "Colombo"),
    "MUNICIPIO DE COLONIA DO PIAUI|PI": ("2202778", "Colônia do Piauí"),
    "MUNICIPIO DE COLORADO DO OESTE|RO": ("1100064", "Colorado do Oeste"),
    "MUNICIPIO DE COLORADO|PR": ("4105904", "Colorado"),
    "MUNICIPIO DE COLORADO|RS": ("4305603", "Colorado"),
    "MUNICIPIO DE COLUNA|MG": ("3116803", "Coluna"),
    "MUNICIPIO DE COMBINADO|TO": ("1705557", "Combinado"),
    "MUNICIPIO DE COMENDADOR GOMES|MG": ("3116902", "Comendador Gomes"),
    "MUNICIPIO DE COMERCINHO|MG": ("3117009", "Comercinho"),
    "MUNICIPIO DE COMODORO|MT": ("5103304", "Comodoro"),
    "MUNICIPIO DE CONCEICAO DA APARECIDA|MG": ("3117108", "Conceição da Aparecida"),
    "MUNICIPIO DE CONCEICAO DA BARRA DE MINAS|MG": ("3115201", "Conceição da Barra de Minas"),
    "MUNICIPIO DE CONCEICAO DA BARRA|ES": ("3201605", "Conceição da Barra"),
    "MUNICIPIO DE CONCEICAO DAS ALAGOAS|MG": ("3117306", "Conceição das Alagoas"),
    "MUNICIPIO DE CONCEICAO DAS PEDRAS|MG": ("3117207", "Conceição das Pedras"),
    "MUNICIPIO DE CONCEICAO DE IPANEMA|MG": ("3117405", "Conceição de Ipanema"),
    "MUNICIPIO DE CONCEICAO DE MACABU|RJ": ("3301405", "Conceição de Macabu"),
    "MUNICIPIO DE CONCEICAO DO ALMEIDA|BA": ("2908309", "Conceição do Almeida"),
    "MUNICIPIO DE CONCEICAO DO ARAGUAIA|PA": ("1502707", "Conceição do Araguaia"),
    "MUNICIPIO DE CONCEICAO DO CASTELO|ES": ("3201704", "Conceição do Castelo"),
    "MUNICIPIO DE CONCEICAO DO COITE|BA": ("2908408", "Conceição do Coité"),
    "MUNICIPIO DE CONCEICAO DO JACUIPE|BA": ("2908507", "Conceição do Jacuípe"),
    "MUNICIPIO DE CONCEICAO DO PARA|MG": ("3117603", "Conceição do Pará"),
    "MUNICIPIO DE CONCEICAO DO RIO VERDE|MG": ("3117702", "Conceição do Rio Verde"),
    "MUNICIPIO DE CONCEICAO DO TOCANTINS|TO": ("1705607", "Conceição do Tocantins"),
    "MUNICIPIO DE CONCEICAO DOS OUROS|MG": ("3117801", "Conceição dos Ouros"),
    "MUNICIPIO DE CONCEICAO|PB": ("2504405", "Conceição"),
    "MUNICIPIO DE CONCHAL|SP": ("3512209", "Conchal"),
    "MUNICIPIO DE CONCHAS|SP": ("3512308", "Conchas"),
    "MUNICIPIO DE CONCORDIA DO PARA|PA": ("1502756", "Concórdia do Pará"),
    "MUNICIPIO DE CONDEUBA|BA": ("2908705", "Condeúba"),
    "MUNICIPIO DE CONDE|BA": ("2908606", "Conde"),
    "MUNICIPIO DE CONDE|PB": ("2504603", "Conde"),
    "MUNICIPIO DE CONDOR|RS": ("4305702", "Condor"),
    "MUNICIPIO DE CONEGO MARINHO|MG": ("3117836", "Cônego Marinho"),
    "MUNICIPIO DE CONFINS|MG": ("3117876", "Confins"),
    "MUNICIPIO DE CONFRESA|MT": ("5103353", "Confresa"),
    "MUNICIPIO DE CONGONHAL|MG": ("3117900", "Congonhal"),
    "MUNICIPIO DE CONGONHAS DO NORTE|MG": ("3118106", "Congonhas do Norte"),
    "MUNICIPIO DE CONGONHINHAS|PR": ("4106001", "Congonhinhas"),
    "MUNICIPIO DE CONGO|PB": ("2504702", "Congo"),
    "MUNICIPIO DE CONQUISTA D'OESTE|MT": ("5103361", "Conquista D'Oeste"),
    "MUNICIPIO DE CONQUISTA|MG": ("3118205", "Conquista"),
    "MUNICIPIO DE CONSELHEIRO LAFAIETE|MG": ("3118304", "Conselheiro Lafaiete"),
    "MUNICIPIO DE CONSELHEIRO PENA|MG": ("3118403", "Conselheiro Pena"),
    "MUNICIPIO DE CONSOLACAO|MG": ("3118502", "Consolação"),
    "MUNICIPIO DE CONSTANTINA|RS": ("4305801", "Constantina"),
    "MUNICIPIO DE CONTAGEM|MG": ("3118601", "Contagem"),
    "MUNICIPIO DE CONTENDA|PR": ("4106209", "Contenda"),
    "MUNICIPIO DE COQUEIRAL|MG": ("3118700", "Coqueiral"),
    "MUNICIPIO DE COQUEIRO BAIXO|RS": ("4305835", "Coqueiro Baixo"),
    "MUNICIPIO DE CORACAO DE JESUS|MG": ("3118809", "Coração de Jesus"),
    "MUNICIPIO DE CORACAO DE MARIA|BA": ("2908903", "Coração de Maria"),
    "MUNICIPIO DE CORBELIA|PR": ("4106308", "Corbélia"),
    "MUNICIPIO DE CORDEIROPOLIS|SP": ("3512407", "Cordeirópolis"),
    "MUNICIPIO DE CORDEIROS|BA": ("2909000", "Cordeiros"),
    "MUNICIPIO DE CORDEIRO|RJ": ("3301504", "Cordeiro"),
    "MUNICIPIO DE CORDILHEIRA ALTA|SC": ("4204350", "Cordilheira Alta"),
    "MUNICIPIO DE CORDISBURGO|MG": ("3118908", "Cordisburgo"),
    "MUNICIPIO DE CORDISLANDIA|MG": ("3119005", "Cordislândia"),
    "MUNICIPIO DE COREAU|CE": ("2304004", "Coreaú"),
    "MUNICIPIO DE CORGUINHO|MS": ("5003108", "Corguinho"),
    "MUNICIPIO DE CORIBE|BA": ("2909109", "Coribe"),
    "MUNICIPIO DE CORINTO|MG": ("3119104", "Corinto"),
    "MUNICIPIO DE CORNELIO PROCOPIO|PR": ("4106407", "Cornélio Procópio"),
    "MUNICIPIO DE COROACI|MG": ("3119203", "Coroaci"),
    "MUNICIPIO DE COROADOS|SP": ("3512506", "Coroados"),
    "MUNICIPIO DE COROATA|MA": ("2103604", "Coroatá"),
    "MUNICIPIO DE COROMANDEL|MG": ("3119302", "Coromandel"),
    "MUNICIPIO DE CORONEL BARROS|RS": ("4305871", "Coronel Barros"),
    "MUNICIPIO DE CORONEL BICACO|RS": ("4305900", "Coronel Bicaco"),
    "MUNICIPIO DE CORONEL EZEQUIEL|RN": ("2402808", "Coronel Ezequiel"),
    "MUNICIPIO DE CORONEL FABRICIANO|MG": ("3119401", "Coronel Fabriciano"),
    "MUNICIPIO DE CORONEL FREITAS|SC": ("4204400", "Coronel Freitas"),
    "MUNICIPIO DE CORONEL JOAO PESSOA|RN": ("2402907", "Coronel João Pessoa"),
    "MUNICIPIO DE CORONEL JOAO SA|BA": ("2909208", "Coronel João Sá"),
    "MUNICIPIO DE CORONEL JOSE DIAS|PI": ("2202851", "Coronel José Dias"),
    "MUNICIPIO DE CORONEL MACEDO|SP": ("3512605", "Coronel Macedo"),
    "MUNICIPIO DE CORONEL MURTA|MG": ("3119500", "Coronel Murta"),
    "MUNICIPIO DE CORONEL PACHECO|MG": ("3119609", "Coronel Pacheco"),
    "MUNICIPIO DE CORONEL PILAR|RS": ("4305934", "Coronel Pilar"),
    "MUNICIPIO DE CORONEL SAPUCAIA|MS": ("5003488", "Coronel Sapucaia"),
    "MUNICIPIO DE CORONEL VIVIDA|PR": ("4106506", "Coronel Vivida"),
    "MUNICIPIO DE CORONEL XAVIER CHAVES|MG": ("3119708", "Coronel Xavier Chaves"),
    "MUNICIPIO DE CORREGO DANTA|MG": ("3119807", "Córrego Danta"),
    "MUNICIPIO DE CORREGO DO BOM JESUS|MG": ("3119906", "Córrego do Bom Jesus"),
    "MUNICIPIO DE CORREGO FUNDO|MG": ("3119955", "Córrego Fundo"),
    "MUNICIPIO DE CORREGO NOVO|MG": ("3120003", "Córrego Novo"),
    "MUNICIPIO DE CORREIA PINTO|SC": ("4204558", "Correia Pinto"),
    "MUNICIPIO DE CORRENTES|PE": ("2604700", "Correntes"),
    "MUNICIPIO DE CORRENTE|PI": ("2202901", "Corrente"),
    "MUNICIPIO DE CORRENTINA|BA": ("2909307", "Correntina"),
    "MUNICIPIO DE CORTES|PE": ("2604809", "Cortês"),
    "MUNICIPIO DE CORUMBA DE GOIAS|GO": ("5205802", "Corumbá de Goiás"),
    "MUNICIPIO DE CORUMBAIBA|GO": ("5205901", "Corumbaíba"),
    "MUNICIPIO DE CORUMBATAI DO SUL|PR": ("4106555", "Corumbataí do Sul"),
    "MUNICIPIO DE CORUMBATAI|SP": ("3512704", "Corumbataí"),
    "MUNICIPIO DE CORUMBA|MS": ("5003207", "Corumbá"),
    "MUNICIPIO DE CORUMBIARA|RO": ("1100072", "Corumbiara"),
    "MUNICIPIO DE CORUPA|SC": ("4204509", "Corupá"),
    "MUNICIPIO DE COSMOPOLIS|SP": ("3512803", "Cosmópolis"),
    "MUNICIPIO DE COSMORAMA|SP": ("3512902", "Cosmorama"),
    "MUNICIPIO DE COSTA MARQUES|RO": ("1100080", "Costa Marques"),
    "MUNICIPIO DE COTEGIPE|BA": ("2909406", "Cotegipe"),
    "MUNICIPIO DE COTIA|SP": ("3513009", "Cotia"),
    "MUNICIPIO DE COTIPORA|RS": ("4305959", "Cotiporã"),
    "MUNICIPIO DE COUTO DE MAGALHAES DE MINAS|MG": ("3120102", "Couto de Magalhães de Minas"),
    "MUNICIPIO DE COUTO DE MAGALHAES|TO": ("1706001", "Couto Magalhães"),
    "MUNICIPIO DE COXILHA|RS": ("4305975", "Coxilha"),
    "MUNICIPIO DE COXIM|MS": ("5003306", "Coxim"),
    "MUNICIPIO DE CRATEUS|CE": ("2304103", "Crateús"),
    "MUNICIPIO DE CRATO|CE": ("2304202", "Crato"),
    "MUNICIPIO DE CRAVINHOS|SP": ("3513108", "Cravinhos"),
    "MUNICIPIO DE CRAVOLANDIA|BA": ("2909505", "Cravolândia"),
    "MUNICIPIO DE CRICIUMA|SC": ("4204608", "Criciúma"),
    "MUNICIPIO DE CRISOLITA|MG": ("3120151", "Crisólita"),
    "MUNICIPIO DE CRISOPOLIS|BA": ("2909604", "Crisópolis"),
    "MUNICIPIO DE CRISSIUMAL|RS": ("4306007", "Crissiumal"),
    "MUNICIPIO DE CRISTAIS|MG": ("3120201", "Cristais"),
    "MUNICIPIO DE CRISTAL DO SUL|RS": ("4306072", "Cristal do Sul"),
    "MUNICIPIO DE CRISTALANDIA DO PIAUI|PI": ("2203008", "Cristalândia do Piauí"),
    "MUNICIPIO DE CRISTALANDIA|TO": ("1706100", "Cristalândia"),
    "MUNICIPIO DE CRISTALIA|MG": ("3120300", "Cristália"),
    "MUNICIPIO DE CRISTALINA|GO": ("5206206", "Cristalina"),
    "MUNICIPIO DE CRISTAL|RS": ("4306056", "Cristal"),
    "MUNICIPIO DE CRISTIANO OTONI|MG": ("3120409", "Cristiano Otoni"),
    "MUNICIPIO DE CRISTIANOPOLIS|GO": ("5206305", "Cristianópolis"),
    "MUNICIPIO DE CRISTINAPOLIS|SE": ("2801702", "Cristinápolis"),
    "MUNICIPIO DE CRISTINA|MG": ("3120508", "Cristina"),
    "MUNICIPIO DE CRISTINO CASTRO|PI": ("2203107", "Cristino Castro"),
    "MUNICIPIO DE CRISTOPOLIS|BA": ("2909703", "Cristópolis"),
    "MUNICIPIO DE CRIXAS DO TOCANTINS|TO": ("1706258", "Crixás do Tocantins"),
    "MUNICIPIO DE CRIXAS|GO": ("5206404", "Crixás"),
    "MUNICIPIO DE CROMINIA|GO": ("5206503", "Cromínia"),
    "MUNICIPIO DE CRUCILANDIA|MG": ("3120607", "Crucilândia"),
    "MUNICIPIO DE CRUZ ALTA|RS": ("4306106", "Cruz Alta"),
    "MUNICIPIO DE CRUZ DAS ALMAS|BA": ("2909802", "Cruz das Almas"),
    "MUNICIPIO DE CRUZ DO ESPIRITO SANTO|PB": ("2504900", "Cruz do Espírito Santo"),
    "MUNICIPIO DE CRUZ MACHADO|PR": ("4106803", "Cruz Machado"),
    "MUNICIPIO DE CRUZALIA|SP": ("3513306", "Cruzália"),
    "MUNICIPIO DE CRUZALTENSE|RS": ("4306130", "Cruzaltense"),
    "MUNICIPIO DE CRUZEIRO DA FORTALEZA|MG": ("3120706", "Cruzeiro da Fortaleza"),
    "MUNICIPIO DE CRUZEIRO DO IGUACU|PR": ("4106571", "Cruzeiro do Iguaçu"),
    "MUNICIPIO DE CRUZEIRO DO OESTE|PR": ("4106605", "Cruzeiro do Oeste"),
    "MUNICIPIO DE CRUZEIRO DO SUL|AC": ("1200203", "Cruzeiro do Sul"),
    "MUNICIPIO DE CRUZEIRO DO SUL|PR": ("4106704", "Cruzeiro do Sul"),
    "MUNICIPIO DE CRUZEIRO DO SUL|RS": ("4306205", "Cruzeiro do Sul"),
    "MUNICIPIO DE CRUZEIRO|SP": ("3513405", "Cruzeiro"),
    "MUNICIPIO DE CRUZETA|RN": ("2403004", "Cruzeta"),
    "MUNICIPIO DE CRUZILIA|MG": ("3120805", "Cruzília"),
    "MUNICIPIO DE CUBATAO|SP": ("3513504", "Cubatão"),
    "MUNICIPIO DE CUBATI|PB": ("2505006", "Cubati"),
    "MUNICIPIO DE CUIABA|MT": ("5103403", "Cuiabá"),
    "MUNICIPIO DE CUITE DE MAMANGUAPE|PB": ("2505238", "Cuité de Mamanguape"),
    "MUNICIPIO DE CUITEGI|PB": ("2505204", "Cuitegi"),
    "MUNICIPIO DE CUITE|PB": ("2505105", "Cuité"),
    "MUNICIPIO DE CUJUBIM|RO": ("1100940", "Cujubim"),
    "MUNICIPIO DE CUMARI|GO": ("5206602", "Cumari"),
    "MUNICIPIO DE CUMARU DO NORTE|PA": ("1502764", "Cumaru do Norte"),
    "MUNICIPIO DE CUMBE|SE": ("2801900", "Cumbe"),
    "MUNICIPIO DE CUNHA PORA|SC": ("4204707", "Cunha Porã"),
    "MUNICIPIO DE CUNHATAI|SC": ("4204756", "Cunhataí"),
    "MUNICIPIO DE CUNHA|SP": ("3513603", "Cunha"),
    "MUNICIPIO DE CUPARAQUE|MG": ("3120839", "Cuparaque"),
    "MUNICIPIO DE CUPIRA|PE": ("2605004", "Cupira"),
    "MUNICIPIO DE CURACA|BA": ("2909901", "Curaçá"),
    "MUNICIPIO DE CURIMATA|PI": ("2203206", "Curimatá"),
    "MUNICIPIO DE CURITIBANOS|SC": ("4204806", "Curitibanos"),
    "MUNICIPIO DE CURITIBA|PR": ("4106902", "Curitiba"),
    "MUNICIPIO DE CURIUVA|PR": ("4107009", "Curiúva"),
    "MUNICIPIO DE CURRAIS NOVOS|RN": ("2403103", "Currais Novos"),
    "MUNICIPIO DE CURRAIS|PI": ("2203230", "Currais"),
    "MUNICIPIO DE CURRAL DE CIMA|PB": ("2505279", "Curral de Cima"),
    "MUNICIPIO DE CURRAL DE DENTRO|MG": ("3120870", "Curral de Dentro"),
    "MUNICIPIO DE CURRAL NOVO DO PIAUI|PI": ("2203271", "Curral Novo do Piauí"),
    "MUNICIPIO DE CURRALINHOS|PI": ("2203255", "Curralinhos"),
    "MUNICIPIO DE CURRALINHO|PA": ("1502806", "Curralinho"),
    "MUNICIPIO DE CURUCA|PA": ("1502905", "Curuçá"),
    "MUNICIPIO DE CURURUPU|MA": ("2103703", "Cururupu"),
    "MUNICIPIO DE CURVELO|MG": ("3120904", "Curvelo"),
    "MUNICIPIO DE CUSTODIA|PE": ("2605103", "Custódia"),
    "MUNICIPIO DE CUTIAS|AP": ("1600212", "Cutias"),
    "MUNICIPIO DE DAMIANOPOLIS|GO": ("5206701", "Damianópolis"),
    "MUNICIPIO DE DAMIAO|PB": ("2505352", "Damião"),
    "MUNICIPIO DE DAMOLANDIA|GO": ("5206800", "Damolândia"),
    "MUNICIPIO DE DARCINOPOLIS|TO": ("1706506", "Darcinópolis"),
    "MUNICIPIO DE DARIO MEIRA|BA": ("2910008", "Dário Meira"),
    "MUNICIPIO DE DATAS|MG": ("3121001", "Datas"),
    "MUNICIPIO DE DAVINOPOLIS|GO": ("5206909", "Davinópolis"),
    "MUNICIPIO DE DAVINOPOLIS|MA": ("2103752", "Davinópolis"),
    "MUNICIPIO DE DELFIM MOREIRA|MG": ("3121100", "Delfim Moreira"),
    "MUNICIPIO DE DELFINOPOLIS|MG": ("3121209", "Delfinópolis"),
    "MUNICIPIO DE DELMIRO GOUVEIA|AL": ("2702405", "Delmiro Gouveia"),
    "MUNICIPIO DE DELTA|MG": ("3121258", "Delta"),
    "MUNICIPIO DE DEMERVAL LOBAO|PI": ("2203305", "Demerval Lobão"),
    "MUNICIPIO DE DENISE|MT": ("5103452", "Denise"),
    "MUNICIPIO DE DEPUTADO IRAPUAN PINHEIRO|CE": ("2304269", "Deputado Irapuan Pinheiro"),
    "MUNICIPIO DE DERRUBADAS|RS": ("4306320", "Derrubadas"),
    "MUNICIPIO DE DESCALVADO|SP": ("3513702", "Descalvado"),
    "MUNICIPIO DE DESCANSO|SC": ("4204905", "Descanso"),
    "MUNICIPIO DE DESCOBERTO|MG": ("3121308", "Descoberto"),
    "MUNICIPIO DE DESTERRO DE ENTRE RIOS|MG": ("3121407", "Desterro de Entre Rios"),
    "MUNICIPIO DE DESTERRO DO MELO|MG": ("3121506", "Desterro do Melo"),
    "MUNICIPIO DE DEZESSEIS DE NOVEMBRO|RS": ("4306353", "Dezesseis de Novembro"),
    "MUNICIPIO DE DIADEMA|SP": ("3513801", "Diadema"),
    "MUNICIPIO DE DIAMANTE D'OESTE|PR": ("4107157", "Diamante D'Oeste"),
    "MUNICIPIO DE DIAMANTE DO NORTE|PR": ("4107108", "Diamante do Norte"),
    "MUNICIPIO DE DIAMANTE DO SUL|PR": ("4107124", "Diamante do Sul"),
    "MUNICIPIO DE DIAMANTINA|MG": ("3121605", "Diamantina"),
    "MUNICIPIO DE DIAMANTINO|MT": ("5103502", "Diamantino"),
    "MUNICIPIO DE DIANOPOLIS|TO": ("1707009", "Dianópolis"),
    "MUNICIPIO DE DIAS D'AVILA|BA": ("2910057", "Dias d'Ávila"),
    "MUNICIPIO DE DILERMANDO DE AGUIAR|RS": ("4306379", "Dilermando de Aguiar"),
    "MUNICIPIO DE DIOGO DE VASCONCELOS|MG": ("3121704", "Diogo de Vasconcelos"),
    "MUNICIPIO DE DIONISIO CERQUEIRA|SC": ("4205001", "Dionísio Cerqueira"),
    "MUNICIPIO DE DIONISIO|MG": ("3121803", "Dionísio"),
    "MUNICIPIO DE DIORAMA|GO": ("5207105", "Diorama"),
    "MUNICIPIO DE DIRCEU ARCOVERDE|PI": ("2203354", "Dirceu Arcoverde"),
    "MUNICIPIO DE DIVINA PASTORA|SE": ("2802007", "Divina Pastora"),
    "MUNICIPIO DE DIVINO DAS LARANJEIRAS|MG": ("3122108", "Divino das Laranjeiras"),
    "MUNICIPIO DE DIVINO DE SAO LOURENCO|ES": ("3201803", "Divino de São Lourenço"),
    "MUNICIPIO DE DIVINOLANDIA|SP": ("3513900", "Divinolândia"),
    "MUNICIPIO DE DIVINOPOLIS DE GOIAS|GO": ("5208301", "Divinópolis de Goiás"),
    "MUNICIPIO DE DIVINOPOLIS DO TOCANTINS|TO": ("1707108", "Divinópolis do Tocantins"),
    "MUNICIPIO DE DIVINOPOLIS|MG": ("3122306", "Divinópolis"),
    "MUNICIPIO DE DIVINO|MG": ("3122009", "Divino"),
    "MUNICIPIO DE DIVISA ALEGRE|MG": ("3122355", "Divisa Alegre"),
    "MUNICIPIO DE DIVISA NOVA|MG": ("3122405", "Divisa Nova"),
    "MUNICIPIO DE DIVISOPOLIS|MG": ("3122454", "Divisópolis"),
    "MUNICIPIO DE DOBRADA|SP": ("3514007", "Dobrada"),
    "MUNICIPIO DE DOIS CORREGOS|SP": ("3514106", "Dois Córregos"),
    "MUNICIPIO DE DOIS IRMAOS DO BURITI|MS": ("5003488", "Dois Irmãos do Buriti"),
    "MUNICIPIO DE DOIS IRMAOS DO TOCANTINS|TO": ("1707207", "Dois Irmãos do Tocantins"),
    "MUNICIPIO DE DOIS LAJEADOS|RS": ("4306452", "Dois Lajeados"),
    "MUNICIPIO DE DOIS RIACHOS|AL": ("2702504", "Dois Riachos"),
    "MUNICIPIO DE DOIS VIZINHOS|PR": ("4107207", "Dois Vizinhos"),
    "MUNICIPIO DE DOLCINOPOLIS|SP": ("3514205", "Dolcinópolis"),
    "MUNICIPIO DE DOM BASILIO|BA": ("2910107", "Dom Basílio"),
    "MUNICIPIO DE DOM BOSCO|MG": ("3122470", "Dom Bosco"),
    "MUNICIPIO DE DOM EXPEDITO LOPES|PI": ("2203404", "Dom Expedito Lopes"),
    "MUNICIPIO DE DOM FELICIANO|RS": ("4306502", "Dom Feliciano"),
    "MUNICIPIO DE DOM INOCENCIO|PI": ("2203453", "Dom Inocêncio"),
    "MUNICIPIO DE DOM JOAQUIM|MG": ("3122603", "Dom Joaquim"),
    "MUNICIPIO DE DOM PEDRITO|RS": ("4306601", "Dom Pedrito"),
    "MUNICIPIO DE DOM PEDRO DE ALCANTARA|RS": ("4306551", "Dom Pedro de Alcântara"),
    "MUNICIPIO DE DOM PEDRO|MA": ("2103802", "Dom Pedro"),
    "MUNICIPIO DE DOM SILVERIO|MG": ("3122702", "Dom Silvério"),
    "MUNICIPIO DE DOM VICOSO|MG": ("3122801", "Dom Viçoso"),
    "MUNICIPIO DE DOMINGOS MARTINS|ES": ("3201902", "Domingos Martins"),
    "MUNICIPIO DE DONA EMMA|SC": ("4205100", "Dona Emma"),
    "MUNICIPIO DE DONA EUZEBIA|MG": ("3122900", "Dona Euzébia"),
    "MUNICIPIO DE DONA INES|PB": ("2505709", "Dona Inês"),
    "MUNICIPIO DE DORES DE CAMPOS|MG": ("3123007", "Dores de Campos"),
    "MUNICIPIO DE DORES DE GUANHAES|MG": ("3123106", "Dores de Guanhães"),
    "MUNICIPIO DE DORES DO INDAIA|MG": ("3123205", "Dores do Indaiá"),
    "MUNICIPIO DE DORES DO RIO PRETO|ES": ("3202009", "Dores do Rio Preto"),
    "MUNICIPIO DE DORES DO TURVO|MG": ("3123304", "Dores do Turvo"),
    "MUNICIPIO DE DORESOPOLIS|MG": ("3123403", "Doresópolis"),
    "MUNICIPIO DE DORMENTES|PE": ("2605152", "Dormentes"),
    "MUNICIPIO DE DOURADINA|MS": ("5003504", "Douradina"),
    "MUNICIPIO DE DOURADINA|PR": ("4107256", "Douradina"),
    "MUNICIPIO DE DOURADOQUARA|MG": ("3123502", "Douradoquara"),
    "MUNICIPIO DE DOURADOS|MS": ("5003702", "Dourados"),
    "MUNICIPIO DE DOURADO|SP": ("3514304", "Dourado"),
    "MUNICIPIO DE DOUTOR CAMARGO|PR": ("4107306", "Doutor Camargo"),
    "MUNICIPIO DE DOUTOR MAURICIO CARDOSO|RS": ("4306734", "Doutor Maurício Cardoso"),
    "MUNICIPIO DE DOUTOR PEDRINHO|SC": ("4205159", "Doutor Pedrinho"),
    "MUNICIPIO DE DOUTOR RICARDO|RS": ("4306759", "Doutor Ricardo"),
    "MUNICIPIO DE DOVERLANDIA|GO": ("5207253", "Doverlândia"),
    "MUNICIPIO DE DRACENA|SP": ("3514403", "Dracena"),
    "MUNICIPIO DE DUARTINA|SP": ("3514502", "Duartina"),
    "MUNICIPIO DE DUAS BARRAS|RJ": ("3301603", "Duas Barras"),
    "MUNICIPIO DE DUAS ESTRADAS|PB": ("2505808", "Duas Estradas"),
    "MUNICIPIO DE DUERE|TO": ("1707306", "Dueré"),
    "MUNICIPIO DE DUMONT|SP": ("3514601", "Dumont"),
    "MUNICIPIO DE DUQUE DE CAXIAS|RJ": ("3301702", "Duque de Caxias"),
    "MUNICIPIO DE DURANDE|MG": ("3123528", "Durandé"),
    "MUNICIPIO DE EDEALINA|GO": ("5207352", "Edealina"),
    "MUNICIPIO DE EDEIA|GO": ("5207402", "Edéia"),
    "MUNICIPIO DE EIRUNEPE|AM": ("1301407", "Eirunepé"),
    "MUNICIPIO DE ELDORADO DO SUL|RS": ("4306767", "Eldorado do Sul"),
    "MUNICIPIO DE ELDORADO DOS CARAJAS|PA": ("1502954", "Eldorado do Carajás"),
    "MUNICIPIO DE ELDORADO|MS": ("5003751", "Eldorado"),
    "MUNICIPIO DE ELDORADO|SP": ("3514809", "Eldorado"),
    "MUNICIPIO DE ELESBAO VELOSO|PI": ("2203503", "Elesbão Veloso"),
    "MUNICIPIO DE ELIAS FAUSTO|SP": ("3514908", "Elias Fausto"),
    "MUNICIPIO DE ELISEU MARTINS|PI": ("2203602", "Eliseu Martins"),
    "MUNICIPIO DE ELOI MENDES|MG": ("3123601", "Elói Mendes"),
    "MUNICIPIO DE EMBU DAS ARTES|SP": ("3515004", "Embu das Artes"),
    "MUNICIPIO DE EMBU-GUACU|SP": ("3515103", "Embu-Guaçu"),
    "MUNICIPIO DE ENCANTADO|RS": ("4306809", "Encantado"),
    "MUNICIPIO DE ENCRUZILHADA DO SUL|RS": ("4306908", "Encruzilhada do Sul"),
    "MUNICIPIO DE ENCRUZILHADA|BA": ("2910404", "Encruzilhada"),
    "MUNICIPIO DE ENEAS MARQUES|PR": ("4107405", "Enéas Marques"),
    "MUNICIPIO DE ENGENHEIRO BELTRAO|PR": ("4107504", "Engenheiro Beltrão"),
    "MUNICIPIO DE ENGENHEIRO CALDAS|MG": ("3123700", "Engenheiro Caldas"),
    "MUNICIPIO DE ENGENHEIRO COELHO|SP": ("3515152", "Engenheiro Coelho"),
    "MUNICIPIO DE ENGENHEIRO NAVARRO|MG": ("3123809", "Engenheiro Navarro"),
    "MUNICIPIO DE ENGENHO VELHO|RS": ("4306924", "Engenho Velho"),
    "MUNICIPIO DE ENTRE FOLHAS|MG": ("3123858", "Entre Folhas"),
    "MUNICIPIO DE ENTRE IJUIS|RS": ("4306932", "Entre-Ijuís"),
    "MUNICIPIO DE ENTRE RIOS DE MINAS|MG": ("3123908", "Entre Rios de Minas"),
    "MUNICIPIO DE ENTRE RIOS DO SUL|RS": ("4306957", "Entre Rios do Sul"),
    "MUNICIPIO DE ENTRE RIOS|BA": ("2910503", "Entre Rios"),
    "MUNICIPIO DE ENTRE RIOS|SC": ("4205175", "Entre Rios"),
    "MUNICIPIO DE EPITACIOLANDIA|AC": ("1200252", "Epitaciolândia"),
    "MUNICIPIO DE EQUADOR|RN": ("2403400", "Equador"),
    "MUNICIPIO DE ERECHIM|RS": ("4307005", "Erechim"),
    "MUNICIPIO DE ERICO CARDOSO|BA": ("2900504", "Érico Cardoso"),
    "MUNICIPIO DE ERMO|SC": ("4205191", "Ermo"),
    "MUNICIPIO DE ERNESTINA|RS": ("4307054", "Ernestina"),
    "MUNICIPIO DE ERVAL GRANDE|RS": ("4307203", "Erval Grande"),
    "MUNICIPIO DE ERVAL SECO|RS": ("4307302", "Erval Seco"),
    "MUNICIPIO DE ERVAL VELHO|SC": ("4205209", "Erval Velho"),
    "MUNICIPIO DE ERVALIA|MG": ("3124005", "Ervália"),
    "MUNICIPIO DE ESCADA|PE": ("2605202", "Escada"),
    "MUNICIPIO DE ESMERALDA|RS": ("4307401", "Esmeralda"),
    "MUNICIPIO DE ESPERA FELIZ|MG": ("3124203", "Espera Feliz"),
    "MUNICIPIO DE ESPERANCA|PB": ("2506004", "Esperança"),
    "MUNICIPIO DE ESPERANTINA|PI": ("2203701", "Esperantina"),
    "MUNICIPIO DE ESPERANTINA|TO": ("1707405", "Esperantina"),
    "MUNICIPIO DE ESPERANTINOPOLIS|MA": ("2104008", "Esperantinópolis"),
    "MUNICIPIO DE ESPIGAO ALTO DO IGUACU|PR": ("4107546", "Espigão Alto do Iguaçu"),
    "MUNICIPIO DE ESPIGAO D'OESTE|RO": ("1100098", "Espigão D'Oeste"),
    "MUNICIPIO DE ESPINOSA|MG": ("3124302", "Espinosa"),
    "MUNICIPIO DE ESPIRITO SANTO DO DOURADO|MG": ("3124401", "Espírito Santo do Dourado"),
    "MUNICIPIO DE ESPIRITO SANTO DO PINHAL|SP": ("3515186", "Espírito Santo do Pinhal"),
    "MUNICIPIO DE ESPIRITO SANTO DO TURVO|SP": ("3515194", "Espírito Santo do Turvo"),
    "MUNICIPIO DE ESPIRITO SANTO|RN": ("2403509", "Espírito Santo"),
    "MUNICIPIO DE ESPLANADA|BA": ("2910602", "Esplanada"),
    "MUNICIPIO DE ESPUMOSO|RS": ("4307500", "Espumoso"),
    "MUNICIPIO DE ESTACAO|RS": ("4307559", "Estação"),
    "MUNICIPIO DE ESTANCIA VELHA|RS": ("4307609", "Estância Velha"),
    "MUNICIPIO DE ESTANCIA|SE": ("2802106", "Estância"),
    "MUNICIPIO DE ESTEIO|RS": ("4307708", "Esteio"),
    "MUNICIPIO DE ESTIVA|MG": ("3124500", "Estiva"),
    "MUNICIPIO DE ESTRELA D'OESTE|SP": ("3515202", "Estrela d'Oeste"),
    "MUNICIPIO DE ESTRELA DALVA|MG": ("3124609", "Estrela Dalva"),
    "MUNICIPIO DE ESTRELA DO NORTE|GO": ("5207501", "Estrela do Norte"),
    "MUNICIPIO DE ESTRELA DO NORTE|SP": ("3515301", "Estrela do Norte"),
    "MUNICIPIO DE ESTRELA DO SUL|MG": ("3124807", "Estrela do Sul"),
    "MUNICIPIO DE ESTRELA|RS": ("4307807", "Estrela"),
    "MUNICIPIO DE EUCLIDES DA CUNHA PAULISTA|SP": ("3515350", "Euclides da Cunha Paulista"),
    "MUNICIPIO DE EUCLIDES DA CUNHA|BA": ("2910701", "Euclides da Cunha"),
    "MUNICIPIO DE EUGENIO DE CASTRO|RS": ("4307831", "Eugênio de Castro"),
    "MUNICIPIO DE EUGENOPOLIS|MG": ("3124906", "Eugenópolis"),
    "MUNICIPIO DE EUNAPOLIS|BA": ("2910727", "Eunápolis"),
    "MUNICIPIO DE EUSEBIO|CE": ("2304285", "Eusébio"),
    "MUNICIPIO DE EWBANK DA CAMARA|MG": ("3125002", "Ewbank da Câmara"),
    "MUNICIPIO DE EXTREMA|MG": ("3125101", "Extrema"),
    "MUNICIPIO DE EXTREMOZ|RN": ("2403608", "Extremoz"),
    "MUNICIPIO DE EXU|PE": ("2605301", "Exu"),
    "MUNICIPIO DE FAGUNDES VARELA|RS": ("4307864", "Fagundes Varela"),
    "MUNICIPIO DE FAINA|GO": ("5207535", "Faina"),
    "MUNICIPIO DE FARIA LEMOS|MG": ("3125309", "Faria Lemos"),
    "MUNICIPIO DE FAROL|PR": ("4107553", "Farol"),
    "MUNICIPIO DE FARO|PA": ("1503002", "Faro"),
    "MUNICIPIO DE FARROUPILHA|RS": ("4307906", "Farroupilha"),
    "MUNICIPIO DE FARTURA DO PIAUI|PI": ("2203750", "Fartura do Piauí"),
    "MUNICIPIO DE FARTURA|SP": ("3515400", "Fartura"),
    "MUNICIPIO DE FATIMA DO SUL|MS": ("5003801", "Fátima do Sul"),
    "MUNICIPIO DE FATIMA|BA": ("2910750", "Fátima"),
    "MUNICIPIO DE FATIMA|TO": ("1707553", "Fátima"),
    "MUNICIPIO DE FAXINAL DO SOTURNO|RS": ("4308003", "Faxinal do Soturno"),
    "MUNICIPIO DE FAXINAL DOS GUEDES|SC": ("4205308", "Faxinal dos Guedes"),
    "MUNICIPIO DE FAXINALZINHO|RS": ("4308052", "Faxinalzinho"),
    "MUNICIPIO DE FAXINAL|PR": ("4107603", "Faxinal"),
    "MUNICIPIO DE FAZENDA NOVA|GO": ("5207600", "Fazenda Nova"),
    "MUNICIPIO DE FAZENDA RIO GRANDE|PR": ("4107652", "Fazenda Rio Grande"),
    "MUNICIPIO DE FEIJO|AC": ("1200302", "Feijó"),
    "MUNICIPIO DE FEIRA DA MATA|BA": ("2910776", "Feira da Mata"),
    "MUNICIPIO DE FEIRA DE SANTANA|BA": ("2910800", "Feira de Santana"),
    "MUNICIPIO DE FEIRA NOVA DO MARANHAO|MA": ("2104073", "Feira Nova do Maranhão"),
    "MUNICIPIO DE FEIRA NOVA|PE": ("2605400", "Feira Nova"),
    "MUNICIPIO DE FEIRA NOVA|SE": ("2802205", "Feira Nova"),
    "MUNICIPIO DE FELICIO DOS SANTOS|MG": ("3125408", "Felício dos Santos"),
    "MUNICIPIO DE FELIPE GUERRA|RN": ("2403707", "Felipe Guerra"),
    "MUNICIPIO DE FELISBURGO|MG": ("3125606", "Felisburgo"),
    "MUNICIPIO DE FELIXLANDIA|MG": ("3125705", "Felixlândia"),
    "MUNICIPIO DE FELIZ DESERTO|AL": ("2702702", "Feliz Deserto"),
    "MUNICIPIO DE FELIZ NATAL|MT": ("5103700", "Feliz Natal"),
    "MUNICIPIO DE FELIZ|RS": ("4308102", "Feliz"),
    "MUNICIPIO DE FENIX|PR": ("4107702", "Fênix"),
    "MUNICIPIO DE FERNANDES PINHEIRO|PR": ("4107736", "Fernandes Pinheiro"),
    "MUNICIPIO DE FERNANDO FALCAO|MA": ("2104081", "Fernando Falcão"),
    "MUNICIPIO DE FERNANDO PEDROZA|RN": ("2403756", "Fernando Pedroza"),
    "MUNICIPIO DE FERNANDOPOLIS|SP": ("3515509", "Fernandópolis"),
    "MUNICIPIO DE FERRAZ DE VASCONCELOS|SP": ("3515707", "Ferraz de Vasconcelos"),
    "MUNICIPIO DE FERREIRA GOMES|AP": ("1600238", "Ferreira Gomes"),
    "MUNICIPIO DE FERROS|MG": ("3125903", "Ferros"),
    "MUNICIPIO DE FIGUEIRAO|MS": ("5003900", "Figueirão"),
    "MUNICIPIO DE FIGUEIRA|PR": ("4107751", "Figueira"),
    "MUNICIPIO DE FIGUEIROPOLIS|TO": ("1707652", "Figueirópolis"),
    "MUNICIPIO DE FILADELFIA|BA": ("2910859", "Filadélfia"),
    "MUNICIPIO DE FILADELFIA|TO": ("1707702", "Filadélfia"),
    "MUNICIPIO DE FIRMINO ALVES|BA": ("2910909", "Firmino Alves"),
    "MUNICIPIO DE FIRMINOPOLIS|GO": ("5207808", "Firminópolis"),
    "MUNICIPIO DE FLOR DA SERRA DO SUL|PR": ("4107850", "Flor da Serra do Sul"),
    "MUNICIPIO DE FLOR DO SERTAO|SC": ("4205357", "Flor do Sertão"),
    "MUNICIPIO DE FLORA RICA|SP": ("3515806", "Flora Rica"),
    "MUNICIPIO DE FLORAI|PR": ("4107801", "Floraí"),
    "MUNICIPIO DE FLORANIA|RN": ("2403806", "Florânia"),
    "MUNICIPIO DE FLORES DA CUNHA|RS": ("4308201", "Flores da Cunha"),
    "MUNICIPIO DE FLORES DO PIAUI|PI": ("2203800", "Flores do Piauí"),
    "MUNICIPIO DE FLORESTA AZUL|BA": ("2911006", "Floresta Azul"),
    "MUNICIPIO DE FLORESTA DO ARAGUAIA|PA": ("1503044", "Floresta do Araguaia"),
    "MUNICIPIO DE FLORESTA DO PIAUI|PI": ("2203859", "Floresta do Piauí"),
    "MUNICIPIO DE FLORESTAL|MG": ("3126000", "Florestal"),
    "MUNICIPIO DE FLORESTA|PE": ("2605707", "Floresta"),
    "MUNICIPIO DE FLORESTA|PR": ("4107900", "Floresta"),
    "MUNICIPIO DE FLORESTOPOLIS|PR": ("4108007", "Florestópolis"),
    "MUNICIPIO DE FLORES|PE": ("2605608", "Flores"),
    "MUNICIPIO DE FLORIANO PEIXOTO|RS": ("4308250", "Floriano Peixoto"),
    "MUNICIPIO DE FLORIANO|PI": ("2203909", "Floriano"),
    "MUNICIPIO DE FLORIDA PAULISTA|SP": ("3516002", "Flórida Paulista"),
    "MUNICIPIO DE FLORIDA|PR": ("4108106", "Flórida"),
    "MUNICIPIO DE FLORINEA|SP": ("3516101", "Florínea"),
    "MUNICIPIO DE FONTE BOA|AM": ("1301605", "Fonte Boa"),
    "MUNICIPIO DE FONTOURA XAVIER|RS": ("4308300", "Fontoura Xavier"),
    "MUNICIPIO DE FORMIGA|MG": ("3126109", "Formiga"),
    "MUNICIPIO DE FORMIGUEIRO|RS": ("4308409", "Formigueiro"),
    "MUNICIPIO DE FORMOSA DA SERRA NEGRA|MA": ("2104099", "Formosa da Serra Negra"),
    "MUNICIPIO DE FORMOSA DO OESTE|PR": ("4108205", "Formosa do Oeste"),
    "MUNICIPIO DE FORMOSA DO RIO PRETO|BA": ("2911105", "Formosa do Rio Preto"),
    "MUNICIPIO DE FORMOSA DO SUL|SC": ("4205431", "Formosa do Sul"),
    "MUNICIPIO DE FORMOSA|GO": ("5208004", "Formosa"),
    "MUNICIPIO DE FORMOSO DO ARAGUAIA|TO": ("1708205", "Formoso do Araguaia"),
    "MUNICIPIO DE FORMOSO|GO": ("5208103", "Formoso"),
    "MUNICIPIO DE FORMOSO|MG": ("3126208", "Formoso"),
    "MUNICIPIO DE FORQUETINHA|RS": ("4308433", "Forquetinha"),
    "MUNICIPIO DE FORQUILHA|CE": ("2304350", "Forquilha"),
    "MUNICIPIO DE FORQUILHINHA|SC": ("4205456", "Forquilhinha"),
    "MUNICIPIO DE FORTALEZA DE MINAS|MG": ("3126307", "Fortaleza de Minas"),
    "MUNICIPIO DE FORTALEZA DO TABOCAO|TO": ("1708254", "Tabocão"),
    "MUNICIPIO DE FORTALEZA DOS NOGUEIRAS|MA": ("2104107", "Fortaleza dos Nogueiras"),
    "MUNICIPIO DE FORTALEZA DOS VALOS|RS": ("4308458", "Fortaleza dos Valos"),
    "MUNICIPIO DE FORTALEZA|CE": ("2304400", "Fortaleza"),
    "MUNICIPIO DE FORTIM|CE": ("2304459", "Fortim"),
    "MUNICIPIO DE FORTUNA DE MINAS|MG": ("3126406", "Fortuna de Minas"),
    "MUNICIPIO DE FORTUNA|MA": ("2104206", "Fortuna"),
    "MUNICIPIO DE FOZ DO IGUACU|PR": ("4108304", "Foz do Iguaçu"),
    "MUNICIPIO DE FOZ DO JORDAO|PR": ("4108452", "Foz do Jordão"),
    "MUNICIPIO DE FRAIBURGO|SC": ("4205506", "Fraiburgo"),
    "MUNICIPIO DE FRANCA|SP": ("3516200", "Franca"),
    "MUNICIPIO DE FRANCINOPOLIS|PI": ("2204006", "Francinópolis"),
    "MUNICIPIO DE FRANCISCO ALVES|PR": ("4108320", "Francisco Alves"),
    "MUNICIPIO DE FRANCISCO AYRES|PI": ("2204105", "Francisco Ayres"),
    "MUNICIPIO DE FRANCISCO BADARO|MG": ("3126505", "Francisco Badaró"),
    "MUNICIPIO DE FRANCISCO BELTRAO|PR": ("4108403", "Francisco Beltrão"),
    "MUNICIPIO DE FRANCISCO DANTAS|RN": ("2403905", "Francisco Dantas"),
    "MUNICIPIO DE FRANCISCO DUMONT|MG": ("3126604", "Francisco Dumont"),
    "MUNICIPIO DE FRANCISCO MACEDO|PI": ("2204154", "Francisco Macedo"),
    "MUNICIPIO DE FRANCISCO MORATO|SP": ("3516309", "Francisco Morato"),
    "MUNICIPIO DE FRANCISCO SANTOS|PI": ("2204204", "Francisco Santos"),
    "MUNICIPIO DE FRANCISCO SA|MG": ("3126703", "Francisco Sá"),
    "MUNICIPIO DE FRANCO DA ROCHA|SP": ("3516408", "Franco da Rocha"),
    "MUNICIPIO DE FRECHEIRINHA|CE": ("2304509", "Frecheirinha"),
    "MUNICIPIO DE FREDERICO WESTPHALEN|RS": ("4308508", "Frederico Westphalen"),
    "MUNICIPIO DE FREI GASPAR|MG": ("3126802", "Frei Gaspar"),
    "MUNICIPIO DE FREI INOCENCIO|MG": ("3126901", "Frei Inocêncio"),
    "MUNICIPIO DE FREI LAGONEGRO|MG": ("3126950", "Frei Lagonegro"),
    "MUNICIPIO DE FREI MARTINHO|PB": ("2506202", "Frei Martinho"),
    "MUNICIPIO DE FREI MIGUELINHO|PE": ("2605806", "Frei Miguelinho"),
    "MUNICIPIO DE FREI PAULO|SE": ("2802304", "Frei Paulo"),
    "MUNICIPIO DE FREI ROGERIO|SC": ("4205555", "Frei Rogério"),
    "MUNICIPIO DE FRONTEIRA DOS VALES|MG": ("3127057", "Fronteira dos Vales"),
    "MUNICIPIO DE FRONTEIRAS|PI": ("2204303", "Fronteiras"),
    "MUNICIPIO DE FRONTEIRA|MG": ("3127008", "Fronteira"),
    "MUNICIPIO DE FRUTA DE LEITE|MG": ("3127073", "Fruta de Leite"),
    "MUNICIPIO DE FRUTAL|MG": ("3127107", "Frutal"),
    "MUNICIPIO DE FUNDAO|ES": ("3202207", "Fundão"),
    "MUNICIPIO DE FUNILANDIA|MG": ("3127206", "Funilândia"),
    "MUNICIPIO DE GABRIEL MONTEIRO|SP": ("3516507", "Gabriel Monteiro"),
    "MUNICIPIO DE GALILEIA|MG": ("3127305", "Galiléia"),
    "MUNICIPIO DE GALINHOS|RN": ("2404101", "Galinhos"),
    "MUNICIPIO DE GALVAO|SC": ("4205605", "Galvão"),
    "MUNICIPIO DE GAMELEIRA DE GOIAS|GO": ("5208152", "Gameleira de Goiás"),
    "MUNICIPIO DE GAMELEIRAS|MG": ("3127339", "Gameleiras"),
    "MUNICIPIO DE GANDU|BA": ("2911204", "Gandu"),
    "MUNICIPIO DE GARANHUNS|PE": ("2606002", "Garanhuns"),
    "MUNICIPIO DE GARARU|SE": ("2802403", "Gararu"),
    "MUNICIPIO DE GARCA|SP": ("3516705", "Garça"),
    "MUNICIPIO DE GARIBALDI|RS": ("4308607", "Garibaldi"),
    "MUNICIPIO DE GAROPABA|SC": ("4205704", "Garopaba"),
    "MUNICIPIO DE GARRUCHOS|RS": ("4308656", "Garruchos"),
    "MUNICIPIO DE GARUVA|SC": ("4205803", "Garuva"),
    "MUNICIPIO DE GASPAR|SC": ("4205902", "Gaspar"),
    "MUNICIPIO DE GAUCHA DO NORTE|MT": ("5103858", "Gaúcha do Norte"),
    "MUNICIPIO DE GAURAMA|RS": ("4308706", "Gaurama"),
    "MUNICIPIO DE GAVIAO|BA": ("2911253", "Gavião"),
    "MUNICIPIO DE GEMINIANO|PI": ("2204352", "Geminiano"),
    "MUNICIPIO DE GENERAL CAMARA|RS": ("4308805", "General Câmara"),
    "MUNICIPIO DE GENERAL CARNEIRO|MT": ("5103908", "General Carneiro"),
    "MUNICIPIO DE GENERAL CARNEIRO|PR": ("4108502", "General Carneiro"),
    "MUNICIPIO DE GETULINA|SP": ("3517000", "Getulina"),
    "MUNICIPIO DE GETULIO VARGAS|RS": ("4308904", "Getúlio Vargas"),
    "MUNICIPIO DE GILBUES|PI": ("2204402", "Gilbués"),
    "MUNICIPIO DE GIRAU DO PONCIANO|AL": ("2702900", "Girau do Ponciano"),
    "MUNICIPIO DE GIRUA|RS": ("4309001", "Giruá"),
    "MUNICIPIO DE GLAUCILANDIA|MG": ("3127354", "Glaucilândia"),
    "MUNICIPIO DE GLORIA D'OESTE|MT": ("5103957", "Glória D'Oeste"),
    "MUNICIPIO DE GLORIA DE DOURADOS|MS": ("5004007", "Glória de Dourados"),
    "MUNICIPIO DE GLORIA DO GOITA|PE": ("2606101", "Glória do Goitá"),
    "MUNICIPIO DE GLORIA|BA": ("2911402", "Glória"),
    "MUNICIPIO DE GLORINHA|RS": ("4309050", "Glorinha"),
    "MUNICIPIO DE GODOY MOREIRA|PR": ("4108551", "Godoy Moreira"),
    "MUNICIPIO DE GOIABEIRA|MG": ("3127370", "Goiabeira"),
    "MUNICIPIO DE GOIANAPOLIS|GO": ("5208400", "Goianápolis"),
    "MUNICIPIO DE GOIANA|PE": ("2606200", "Goiana"),
    "MUNICIPIO DE GOIANDIRA|GO": ("5208509", "Goiandira"),
    "MUNICIPIO DE GOIANESIA DO PARA|PA": ("1503093", "Goianésia do Pará"),
    "MUNICIPIO DE GOIANESIA|GO": ("5208608", "Goianésia"),
    "MUNICIPIO DE GOIANIA|GO": ("5208707", "Goiânia"),
    "MUNICIPIO DE GOIANIRA|GO": ("5208806", "Goianira"),
    "MUNICIPIO DE GOIANORTE|TO": ("1708304", "Goianorte"),
    "MUNICIPIO DE GOIAS|GO": ("5208905", "Goiás"),
    "MUNICIPIO DE GOIATINS|TO": ("1709005", "Goiatins"),
    "MUNICIPIO DE GOIATUBA|GO": ("5209101", "Goiatuba"),
    "MUNICIPIO DE GOIOERE|PR": ("4108601", "Goioerê"),
    "MUNICIPIO DE GOIOXIM|PR": ("4108650", "Goioxim"),
    "MUNICIPIO DE GONCALVES DIAS|MA": ("2104404", "Gonçalves Dias"),
    "MUNICIPIO DE GONGOGI|BA": ("2911501", "Gongogi"),
    "MUNICIPIO DE GONZAGA|MG": ("3127503", "Gonzaga"),
    "MUNICIPIO DE GOUVEA|MG": ("3127602", "Gouveia"),
    "MUNICIPIO DE GOUVELANDIA|GO": ("5209150", "Gouvelândia"),
    "MUNICIPIO DE GOVERNADOR ARCHER|MA": ("2104503", "Governador Archer"),
    "MUNICIPIO DE GOVERNADOR CELSO RAMOS|SC": ("4206009", "Governador Celso Ramos"),
    "MUNICIPIO DE GOVERNADOR DIX-SEPT ROSADO|RN": ("2404309", "Governador Dix-Sept Rosado"),
    "MUNICIPIO DE GOVERNADOR EDISON LOBAO|MA": ("2104552", "Governador Edison Lobão"),
    "MUNICIPIO DE GOVERNADOR EUGENIO BARROS|MA": ("2104602", "Governador Eugênio Barros"),
    "MUNICIPIO DE GOVERNADOR JORGE TEIXEIRA|RO": ("1101005", "Governador Jorge Teixeira"),
    "MUNICIPIO DE GOVERNADOR LINDENBERG|ES": ("3202256", "Governador Lindenberg"),
    "MUNICIPIO DE GOVERNADOR MANGABEIRA|BA": ("2911600", "Governador Mangabeira"),
    "MUNICIPIO DE GOVERNADOR VALADARES|MG": ("3127701", "Governador Valadares"),
    "MUNICIPIO DE GRACA|CE": ("2304657", "Graça"),
    "MUNICIPIO DE GRACCHO CARDOSO|SE": ("2802601", "Gracho Cardoso"),
    "MUNICIPIO DE GRAJAU|MA": ("2104800", "Grajaú"),
    "MUNICIPIO DE GRAMADO DOS LOUREIROS|RS": ("4309126", "Gramado dos Loureiros"),
    "MUNICIPIO DE GRAMADO XAVIER|RS": ("4309159", "Gramado Xavier"),
    "MUNICIPIO DE GRAMADO|RS": ("4309100", "Gramado"),
    "MUNICIPIO DE GRANDES RIOS|PR": ("4108700", "Grandes Rios"),
    "MUNICIPIO DE GRANITO|PE": ("2606309", "Granito"),
    "MUNICIPIO DE GRANJA|CE": ("2304707", "Granja"),
    "MUNICIPIO DE GRANJEIRO|CE": ("2304806", "Granjeiro"),
    "MUNICIPIO DE GRAO MOGOL|MG": ("3127800", "Grão Mogol"),
    "MUNICIPIO DE GRAO PARA|SC": ("4206108", "Grão-Pará"),
    "MUNICIPIO DE GRAVATAI|RS": ("4309209", "Gravataí"),
    "MUNICIPIO DE GRAVATAL|SC": ("4206207", "Gravatal"),
    "MUNICIPIO DE GRAVATA|PE": ("2606408", "Gravatá"),
    "MUNICIPIO DE GROAIRAS|CE": ("2304905", "Groaíras"),
    "MUNICIPIO DE GROSSOS|RN": ("2404408", "Grossos"),
    "MUNICIPIO DE GUABIRUBA|SC": ("4206306", "Guabiruba"),
    "MUNICIPIO DE GUACUI|ES": ("3202306", "Guaçuí"),
    "MUNICIPIO DE GUADALUPE|PI": ("2204501", "Guadalupe"),
    "MUNICIPIO DE GUAIBA|RS": ("4309308", "Guaíba"),
    "MUNICIPIO DE GUAICARA|SP": ("3517208", "Guaiçara"),
    "MUNICIPIO DE GUAIMBE|SP": ("3517307", "Guaimbê"),
    "MUNICIPIO DE GUAIRACA|PR": ("4108908", "Guairaçá"),
    "MUNICIPIO DE GUAIRA|PR": ("4108809", "Guaíra"),
    "MUNICIPIO DE GUAIRA|SP": ("3517406", "Guaíra"),
    "MUNICIPIO DE GUAJARA-MIRIM|RO": ("1100106", "Guajará-Mirim"),
    "MUNICIPIO DE GUAJERU|BA": ("2911659", "Guajeru"),
    "MUNICIPIO DE GUAMIRANGA|PR": ("4108957", "Guamiranga"),
    "MUNICIPIO DE GUANAMBI|BA": ("2911709", "Guanambi"),
    "MUNICIPIO DE GUANHAES|MG": ("3128006", "Guanhães"),
    "MUNICIPIO DE GUAPIACU|SP": ("3517505", "Guapiaçu"),
    "MUNICIPIO DE GUAPIARA|SP": ("3517604", "Guapiara"),
    "MUNICIPIO DE GUAPIMIRIM|RJ": ("3301850", "Guapimirim"),
    "MUNICIPIO DE GUAPIRAMA|PR": ("4109005", "Guapirama"),
    "MUNICIPIO DE GUAPOREMA|PR": ("4109104", "Guaporema"),
    "MUNICIPIO DE GUAPORE|RS": ("4309407", "Guaporé"),
    "MUNICIPIO DE GUAPO|GO": ("5209200", "Guapó"),
    "MUNICIPIO DE GUARABIRA|PB": ("2506301", "Guarabira"),
    "MUNICIPIO DE GUARACIABA DO NORTE|CE": ("2305001", "Guaraciaba do Norte"),
    "MUNICIPIO DE GUARACIABA|MG": ("3128204", "Guaraciaba"),
    "MUNICIPIO DE GUARACIABA|SC": ("4206405", "Guaraciaba"),
    "MUNICIPIO DE GUARACIAMA|MG": ("3128253", "Guaraciama"),
    "MUNICIPIO DE GUARACI|PR": ("4109203", "Guaraci"),
    "MUNICIPIO DE GUARACI|SP": ("3517901", "Guaraci"),
    "MUNICIPIO DE GUARAITA|GO": ("5209291", "Guaraíta"),
    "MUNICIPIO DE GUARAI|TO": ("1709302", "Guaraí"),
    "MUNICIPIO DE GUARAMIRANGA|CE": ("2305100", "Guaramiranga"),
    "MUNICIPIO DE GUARAMIRIM|SC": ("4206504", "Guaramirim"),
    "MUNICIPIO DE GUARANESIA|MG": ("3128303", "Guaranésia"),
    "MUNICIPIO DE GUARANI D:OESTE|SP": ("3518008", "Guarani d'Oeste"),
    "MUNICIPIO DE GUARANI DAS MISSOES|RS": ("4309506", "Guarani das Missões"),
    "MUNICIPIO DE GUARANI DE GOIAS|GO": ("5209408", "Guarani de Goiás"),
    "MUNICIPIO DE GUARANIACU|PR": ("4109302", "Guaraniaçu"),
    "MUNICIPIO DE GUARANI|MG": ("3128402", "Guarani"),
    "MUNICIPIO DE GUARANTA DO NORTE|MT": ("5104104", "Guarantã do Norte"),
    "MUNICIPIO DE GUARAPARI|ES": ("3202405", "Guarapari"),
    "MUNICIPIO DE GUARAPUAVA|PR": ("4109401", "Guarapuava"),
    "MUNICIPIO DE GUARAQUECABA|PR": ("4109500", "Guaraqueçaba"),
    "MUNICIPIO DE GUARARAPES|SP": ("3518206", "Guararapes"),
    "MUNICIPIO DE GUARARA|MG": ("3128501", "Guarará"),
    "MUNICIPIO DE GUARATINGA|BA": ("2911808", "Guaratinga"),
    "MUNICIPIO DE GUARATINGUETA|SP": ("3518404", "Guaratinguetá"),
    "MUNICIPIO DE GUARATUBA|PR": ("4109609", "Guaratuba"),
    "MUNICIPIO DE GUARA|SP": ("3517703", "Guará"),
    "MUNICIPIO DE GUARDA-MOR|MG": ("3128600", "Guarda-Mor"),
    "MUNICIPIO DE GUAREI|SP": ("3518503", "Guareí"),
    "MUNICIPIO DE GUARIBAS|PI": ("2204550", "Guaribas"),
    "MUNICIPIO DE GUARIBA|SP": ("3518602", "Guariba"),
    "MUNICIPIO DE GUARINOS|GO": ("5209457", "Guarinos"),
    "MUNICIPIO DE GUARUJA DO SUL|SC": ("4206603", "Guarujá do Sul"),
    "MUNICIPIO DE GUARUJA|SP": ("3518701", "Guarujá"),
    "MUNICIPIO DE GUARULHOS|SP": ("3518800", "Guarulhos"),
    "MUNICIPIO DE GUATAMBU|SC": ("4206652", "Guatambú"),
    "MUNICIPIO DE GUATAPARA|SP": ("3518859", "Guatapará"),
    "MUNICIPIO DE GUAXUPE|MG": ("3128709", "Guaxupé"),
    "MUNICIPIO DE GUIDOVAL|MG": ("3128808", "Guidoval"),
    "MUNICIPIO DE GUIMARAES|MA": ("2104909", "Guimarães"),
    "MUNICIPIO DE GUIMARANIA|MG": ("3128907", "Guimarânia"),
    "MUNICIPIO DE GUIRATINGA|MT": ("5104203", "Guiratinga"),
    "MUNICIPIO DE GUIRICEMA|MG": ("3129004", "Guiricema"),
    "MUNICIPIO DE GURINHATA|MG": ("3129103", "Gurinhatã"),
    "MUNICIPIO DE GURINHEM|PB": ("2506400", "Gurinhém"),
    "MUNICIPIO DE GURJAO|PB": ("2506509", "Gurjão"),
    "MUNICIPIO DE GURUPI|TO": ("1709500", "Gurupi"),
    "MUNICIPIO DE GUZOLANDIA|SP": ("3518909", "Guzolândia"),
    "MUNICIPIO DE HARMONIA|RS": ("4309555", "Harmonia"),
    "MUNICIPIO DE HEITORAI|GO": ("5209606", "Heitoraí"),
    "MUNICIPIO DE HELIODORA|MG": ("3129202", "Heliodora"),
    "MUNICIPIO DE HELIOPOLIS|BA": ("2911857", "Heliópolis"),
    "MUNICIPIO DE HERCULANDIA|SP": ("3519006", "Herculândia"),
    "MUNICIPIO DE HERVAL D'OESTE|SC": ("4206702", "Herval d'Oeste"),
    "MUNICIPIO DE HERVAL|RS": ("4307104", "Herval"),
    "MUNICIPIO DE HERVEIRAS|RS": ("4309571", "Herveiras"),
    "MUNICIPIO DE HIDROLANDIA|CE": ("2305209", "Hidrolândia"),
    "MUNICIPIO DE HIDROLANDIA|GO": ("5209705", "Hidrolândia"),
    "MUNICIPIO DE HIDROLINA|GO": ("5209804", "Hidrolina"),
    "MUNICIPIO DE HOLAMBRA|SP": ("3519055", "Holambra"),
    "MUNICIPIO DE HONORIO SERPA|PR": ("4109658", "Honório Serpa"),
    "MUNICIPIO DE HORIZONTE|CE": ("2305233", "Horizonte"),
    "MUNICIPIO DE HORIZONTINA|RS": ("4309605", "Horizontina"),
    "MUNICIPIO DE HORTOLANDIA|SP": ("3519071", "Hortolândia"),
    "MUNICIPIO DE HULHA NEGRA|RS": ("4309654", "Hulha Negra"),
    "MUNICIPIO DE HUMAITA|AM": ("1301704", "Humaitá"),
    "MUNICIPIO DE HUMAITA|RS": ("4309704", "Humaitá"),
    "MUNICIPIO DE IACANGA|SP": ("3519105", "Iacanga"),
    "MUNICIPIO DE IACIARA|GO": ("5209903", "Iaciara"),
    "MUNICIPIO DE IACRI|SP": ("3519204", "Iacri"),
    "MUNICIPIO DE IACU|BA": ("2911907", "Iaçu"),
    "MUNICIPIO DE IAPU|MG": ("3129301", "Iapu"),
    "MUNICIPIO DE IARAS|SP": ("3519253", "Iaras"),
    "MUNICIPIO DE IATI|PE": ("2606507", "Iati"),
    "MUNICIPIO DE IBAITI|PR": ("4109708", "Ibaiti"),
    "MUNICIPIO DE IBARAMA|RS": ("4309753", "Ibarama"),
    "MUNICIPIO DE IBATIBA|ES": ("3202454", "Ibatiba"),
    "MUNICIPIO DE IBEMA|PR": ("4109757", "Ibema"),
    "MUNICIPIO DE IBIACA|RS": ("4309803", "Ibiaçá"),
    "MUNICIPIO DE IBIAI|MG": ("3129608", "Ibiaí"),
    "MUNICIPIO DE IBIAM|SC": ("4206751", "Ibiam"),
    "MUNICIPIO DE IBIAPINA|CE": ("2305308", "Ibiapina"),
    "MUNICIPIO DE IBIASSUCE|BA": ("2912004", "Ibiassucê"),
    "MUNICIPIO DE IBIA|MG": ("3129509", "Ibiá"),
    "MUNICIPIO DE IBICARAI|BA": ("2912103", "Ibicaraí"),
    "MUNICIPIO DE IBICARE|SC": ("4206801", "Ibicaré"),
    "MUNICIPIO DE IBICUITINGA|CE": ("2305332", "Ibicuitinga"),
    "MUNICIPIO DE IBIMIRIM|PE": ("2606606", "Ibimirim"),
    "MUNICIPIO DE IBIPEBA|BA": ("2912400", "Ibipeba"),
    "MUNICIPIO DE IBIPITANGA|BA": ("2912509", "Ibipitanga"),
    "MUNICIPIO DE IBIPORA|PR": ("4109807", "Ibiporã"),
    "MUNICIPIO DE IBIQUERA|BA": ("2912608", "Ibiquera"),
    "MUNICIPIO DE IBIRACATU|MG": ("3129657", "Ibiracatu"),
    "MUNICIPIO DE IBIRACI|MG": ("3129707", "Ibiraci"),
    "MUNICIPIO DE IBIRAIARAS|RS": ("4309902", "Ibiraiaras"),
    "MUNICIPIO DE IBIRAJUBA|PE": ("2606705", "Ibirajuba"),
    "MUNICIPIO DE IBIRAMA|SC": ("4206900", "Ibirama"),
    "MUNICIPIO DE IBIRAPITANGA|BA": ("2912707", "Ibirapitanga"),
    "MUNICIPIO DE IBIRAPUITA|RS": ("4309951", "Ibirapuitã"),
    "MUNICIPIO DE IBIRAREMA|SP": ("3519501", "Ibirarema"),
    "MUNICIPIO DE IBIRATAIA|BA": ("2912905", "Ibirataia"),
    "MUNICIPIO DE IBIRA|SP": ("3519402", "Ibirá"),
    "MUNICIPIO DE IBIRITE|MG": ("3129806", "Ibirité"),
    "MUNICIPIO DE IBIRUBA|RS": ("4310009", "Ibirubá"),
    "MUNICIPIO DE IBITIARA|BA": ("2913002", "Ibitiara"),
    "MUNICIPIO DE IBITIRAMA|ES": ("3202553", "Ibitirama"),
    "MUNICIPIO DE IBITITA|BA": ("2913101", "Ibititá"),
    "MUNICIPIO DE IBITIURA DE MINAS|MG": ("3129905", "Ibitiúra de Minas"),
    "MUNICIPIO DE IBITURUNA|MG": ("3130002", "Ibituruna"),
    "MUNICIPIO DE IBIUNA|SP": ("3519709", "Ibiúna"),
    "MUNICIPIO DE IBOTIRAMA|BA": ("2913200", "Ibotirama"),
    "MUNICIPIO DE ICARAI DE MINAS|MG": ("3130051", "Icaraí de Minas"),
    "MUNICIPIO DE ICARAIMA|PR": ("4109906", "Icaraíma"),
    "MUNICIPIO DE ICARA|SC": ("4207007", "Içara"),
    "MUNICIPIO DE ICATU|MA": ("2105104", "Icatu"),
    "MUNICIPIO DE ICEM|SP": ("3519808", "Icém"),
    "MUNICIPIO DE ICHU|BA": ("2913309", "Ichu"),
    "MUNICIPIO DE ICONHA|ES": ("3202603", "Iconha"),
    "MUNICIPIO DE ICO|CE": ("2305407", "Icó"),
    "MUNICIPIO DE IEPE|SP": ("3519907", "Iepê"),
    "MUNICIPIO DE IGACI|AL": ("2703106", "Igaci"),
    "MUNICIPIO DE IGAPORA|BA": ("2913408", "Igaporã"),
    "MUNICIPIO DE IGARACU DO TIETE|SP": ("3520004", "Igaraçu do Tietê"),
    "MUNICIPIO DE IGARAPAVA|SP": ("3520103", "Igarapava"),
    "MUNICIPIO DE IGARAPE-ACU|PA": ("1503200", "Igarapé-Açu"),
    "MUNICIPIO DE IGARAPE-MIRI|PA": ("1503309", "Igarapé-Miri"),
    "MUNICIPIO DE IGARAPE|MG": ("3130101", "Igarapé"),
    "MUNICIPIO DE IGARASSU|PE": ("2606804", "Igarassu"),
    "MUNICIPIO DE IGARATA|SP": ("3520202", "Igaratá"),
    "MUNICIPIO DE IGARATINGA|MG": ("3130200", "Igaratinga"),
    "MUNICIPIO DE IGRAPIUNA|BA": ("2913457", "Igrapiúna"),
    "MUNICIPIO DE IGREJINHA|RS": ("4310108", "Igrejinha"),
    "MUNICIPIO DE IGUABA GRANDE|RJ": ("3301876", "Iguaba Grande"),
    "MUNICIPIO DE IGUAPE|SP": ("3520301", "Iguape"),
    "MUNICIPIO DE IGUARACI|PE": ("2606903", "Iguaracy"),
    "MUNICIPIO DE IGUARACU|PR": ("4110003", "Iguaraçu"),
    "MUNICIPIO DE IGUATAMA|MG": ("3130309", "Iguatama"),
    "MUNICIPIO DE IGUATEMI|MS": ("5004304", "Iguatemi"),
    "MUNICIPIO DE IGUATU|CE": ("2305506", "Iguatu"),
    "MUNICIPIO DE IGUATU|PR": ("4110052", "Iguatu"),
    "MUNICIPIO DE IJACI|MG": ("3130408", "Ijaci"),
    "MUNICIPIO DE IJUI|RS": ("4310207", "Ijuí"),
    "MUNICIPIO DE ILHA COMPRIDA|SP": ("3520426", "Ilha Comprida"),
    "MUNICIPIO DE ILHA DAS FLORES|SE": ("2802700", "Ilha das Flores"),
    "MUNICIPIO DE ILHA DE ITAMARACA|PE": ("2607604", "Ilha de Itamaracá"),
    "MUNICIPIO DE ILHA GRANDE|PI": ("2204659", "Ilha Grande"),
    "MUNICIPIO DE ILHA SOLTEIRA|SP": ("3520442", "Ilha Solteira"),
    "MUNICIPIO DE ILHEUS|BA": ("2913606", "Ilhéus"),
    "MUNICIPIO DE ILHOTA|SC": ("4207106", "Ilhota"),
    "MUNICIPIO DE ILICINEA|MG": ("3130507", "Ilicínea"),
    "MUNICIPIO DE ILOPOLIS|RS": ("4310306", "Ilópolis"),
    "MUNICIPIO DE IMACULADA|PB": ("2506707", "Imaculada"),
    "MUNICIPIO DE IMARUI|SC": ("4207205", "Imaruí"),
    "MUNICIPIO DE IMBAU|PR": ("4110078", "Imbaú"),
    "MUNICIPIO DE IMBE DE MINAS|MG": ("3130556", "Imbé de Minas"),
    "MUNICIPIO DE IMBE|RS": ("4310330", "Imbé"),
    "MUNICIPIO DE IMBITUBA|SC": ("4207304", "Imbituba"),
    "MUNICIPIO DE IMBITUVA|PR": ("4110102", "Imbituva"),
    "MUNICIPIO DE IMBUIA|SC": ("4207403", "Imbuia"),
    "MUNICIPIO DE IMIGRANTE|RS": ("4310363", "Imigrante"),
    "MUNICIPIO DE IMPERATRIZ|MA": ("2105302", "Imperatriz"),
    "MUNICIPIO DE INACIO MARTINS|PR": ("4110201", "Inácio Martins"),
    "MUNICIPIO DE INACIOLANDIA|GO": ("5209937", "Inaciolândia"),
    "MUNICIPIO DE INAJA|PE": ("2607000", "Inajá"),
    "MUNICIPIO DE INAJA|PR": ("4110300", "Inajá"),
    "MUNICIPIO DE INCONFIDENTES|MG": ("3130606", "Inconfidentes"),
    "MUNICIPIO DE INDAIABIRA|MG": ("3130655", "Indaiabira"),
    "MUNICIPIO DE INDAIAL|SC": ("4207502", "Indaial"),
    "MUNICIPIO DE INDAIATUBA|SP": ("3520509", "Indaiatuba"),
    "MUNICIPIO DE INDEPENDENCIA|CE": ("2305605", "Independência"),
    "MUNICIPIO DE INDEPENDENCIA|RS": ("4310405", "Independência"),
    "MUNICIPIO DE INDIANA|SP": ("3520608", "Indiana"),
    "MUNICIPIO DE INDIANOPOLIS|MG": ("3130705", "Indianópolis"),
    "MUNICIPIO DE INDIANOPOLIS|PR": ("4110409", "Indianópolis"),
    "MUNICIPIO DE INDIAPORA|SP": ("3520707", "Indiaporã"),
    "MUNICIPIO DE INDIARA|GO": ("5209952", "Indiara"),
    "MUNICIPIO DE INDIAROBA|SE": ("2802809", "Indiaroba"),
    "MUNICIPIO DE INDIAVAI|MT": ("5104500", "Indiavaí"),
    "MUNICIPIO DE INGAI|MG": ("3130804", "Ingaí"),
    "MUNICIPIO DE INGAZEIRA|PE": ("2607109", "Ingazeira"),
    "MUNICIPIO DE INGA|PB": ("2506806", "Ingá"),
    "MUNICIPIO DE INHACORA|RS": ("4310413", "Inhacorá"),
    "MUNICIPIO DE INHAMBUPE|BA": ("2913705", "Inhambupe"),
    "MUNICIPIO DE INHANGAPI|PA": ("1503408", "Inhangapi"),
    "MUNICIPIO DE INHAPIM|MG": ("3130903", "Inhapim"),
    "MUNICIPIO DE INHAPI|AL": ("2703304", "Inhapi"),
    "MUNICIPIO DE INHAUMA|MG": ("3131000", "Inhaúma"),
    "MUNICIPIO DE INHUMAS|GO": ("5210000", "Inhumas"),
    "MUNICIPIO DE INHUMA|PI": ("2204709", "Inhuma"),
    "MUNICIPIO DE INIMUTABA|MG": ("3131109", "Inimutaba"),
    "MUNICIPIO DE INOCENCIA|MS": ("5004403", "Inocência"),
    "MUNICIPIO DE INUBIA PAULISTA|SP": ("3520806", "Inúbia Paulista"),
    "MUNICIPIO DE IOMERE|SC": ("4207577", "Iomerê"),
    "MUNICIPIO DE IPABA|MG": ("3131158", "Ipaba"),
    "MUNICIPIO DE IPAMERI|GO": ("5210109", "Ipameri"),
    "MUNICIPIO DE IPANEMA|MG": ("3131208", "Ipanema"),
    "MUNICIPIO DE IPANGUACU|RN": ("2404705", "Ipanguaçu"),
    "MUNICIPIO DE IPATINGA|MG": ("3131307", "Ipatinga"),
    "MUNICIPIO DE IPAUMIRIM|CE": ("2305704", "Ipaumirim"),
    "MUNICIPIO DE IPAUSSU|SP": ("3520905", "Ipaussu"),
    "MUNICIPIO DE IPECAETA|BA": ("2913804", "Ipecaetá"),
    "MUNICIPIO DE IPERO|SP": ("3521002", "Iperó"),
    "MUNICIPIO DE IPE|RS": ("4310439", "Ipê"),
    "MUNICIPIO DE IPIACU|MG": ("3131406", "Ipiaçu"),
    "MUNICIPIO DE IPIAU|BA": ("2913903", "Ipiaú"),
    "MUNICIPIO DE IPIRANGA DE GOIAS|GO": ("5210158", "Ipiranga de Goiás"),
    "MUNICIPIO DE IPIRANGA DO PIAUI|PI": ("2204808", "Ipiranga do Piauí"),
    "MUNICIPIO DE IPIRANGA|PR": ("4110508", "Ipiranga"),
    "MUNICIPIO DE IPIRA|SC": ("4207601", "Ipira"),
    "MUNICIPIO DE IPIXUNA DO PARA|PA": ("1503457", "Ipixuna do Pará"),
    "MUNICIPIO DE IPIXUNA|AM": ("1301803", "Ipixuna"),
    "MUNICIPIO DE IPOJUCA|PE": ("2607208", "Ipojuca"),
    "MUNICIPIO DE IPORA DO OESTE|SC": ("4207650", "Iporã do Oeste"),
    "MUNICIPIO DE IPORANGA|SP": ("3521200", "Iporanga"),
    "MUNICIPIO DE IPORA|GO": ("5210208", "Iporá"),
    "MUNICIPIO DE IPUACU|SC": ("4207684", "Ipuaçu"),
    "MUNICIPIO DE IPUA|SP": ("3521309", "Ipuã"),
    "MUNICIPIO DE IPUBI|PE": ("2607307", "Ipubi"),
    "MUNICIPIO DE IPUEIRAS|CE": ("2305902", "Ipueiras"),
    "MUNICIPIO DE IPUEIRAS|TO": ("1709807", "Ipueiras"),
    "MUNICIPIO DE IPUEIRA|RN": ("2404804", "Ipueira"),
    "MUNICIPIO DE IPUIUNA|MG": ("3131505", "Ipuiúna"),
    "MUNICIPIO DE IPUMIRIM|SC": ("4207700", "Ipumirim"),
    "MUNICIPIO DE IPU|CE": ("2305803", "Ipu"),
    "MUNICIPIO DE IRACEMA DO OESTE|PR": ("4110656", "Iracema do Oeste"),
    "MUNICIPIO DE IRACEMAPOLIS|SP": ("3521408", "Iracemápolis"),
    "MUNICIPIO DE IRACEMA|CE": ("2306009", "Iracema"),
    "MUNICIPIO DE IRACEMA|RR": ("1400282", "Iracema"),
    "MUNICIPIO DE IRACEMINHA|SC": ("4207759", "Iraceminha"),
    "MUNICIPIO DE IRAI DE MINAS|MG": ("3131604", "Iraí de Minas"),
    "MUNICIPIO DE IRAI|RS": ("4310504", "Iraí"),
    "MUNICIPIO DE IRAJUBA|BA": ("2914208", "Irajuba"),
    "MUNICIPIO DE IRAMAIA|BA": ("2914307", "Iramaia"),
    "MUNICIPIO DE IRANDUBA|AM": ("1301852", "Iranduba"),
    "MUNICIPIO DE IRANI|SC": ("4207809", "Irani"),
    "MUNICIPIO DE IRAPUA|SP": ("3521507", "Irapuã"),
    "MUNICIPIO DE IRAQUARA|BA": ("2914406", "Iraquara"),
    "MUNICIPIO DE IRARA|BA": ("2914505", "Irará"),
    "MUNICIPIO DE IRATI|PR": ("4110706", "Irati"),
    "MUNICIPIO DE IRATI|SC": ("4207858", "Irati"),
    "MUNICIPIO DE IRAUCUBA|CE": ("2306108", "Irauçuba"),
    "MUNICIPIO DE IRECE|BA": ("2914604", "Irecê"),
    "MUNICIPIO DE IRETAMA|PR": ("4110805", "Iretama"),
    "MUNICIPIO DE IRINEOPOLIS|SC": ("4207908", "Irineópolis"),
    "MUNICIPIO DE IRITUIA|PA": ("1503507", "Irituia"),
    "MUNICIPIO DE IRUPI|ES": ("3202652", "Irupi"),
    "MUNICIPIO DE ISAIAS COELHO|PI": ("2204907", "Isaías Coelho"),
    "MUNICIPIO DE ISRAELANDIA|GO": ("5210307", "Israelândia"),
    "MUNICIPIO DE ITABAIANINHA|SE": ("2803005", "Itabaianinha"),
    "MUNICIPIO DE ITABERABA|BA": ("2914703", "Itaberaba"),
    "MUNICIPIO DE ITABERAI|GO": ("5210406", "Itaberaí"),
    "MUNICIPIO DE ITABERA|SP": ("3521705", "Itaberá"),
    "MUNICIPIO DE ITABIRA|MG": ("3131703", "Itabira"),
    "MUNICIPIO DE ITABIRINHA|MG": ("3131802", "Itabirinha"),
    "MUNICIPIO DE ITABIRITO|MG": ("3131901", "Itabirito"),
    "MUNICIPIO DE ITABI|SE": ("2803104", "Itabi"),
    "MUNICIPIO DE ITABORAI|RJ": ("3301900", "Itaboraí"),
    "MUNICIPIO DE ITABUNA|BA": ("2914802", "Itabuna"),
    "MUNICIPIO DE ITACAJA|TO": ("1710508", "Itacajá"),
    "MUNICIPIO DE ITACAMBIRA|MG": ("3132008", "Itacambira"),
    "MUNICIPIO DE ITACARAMBI|MG": ("3132107", "Itacarambi"),
    "MUNICIPIO DE ITACARE|BA": ("2914901", "Itacaré"),
    "MUNICIPIO DE ITACOATIARA|AM": ("1301902", "Itacoatiara"),
    "MUNICIPIO DE ITACURUBI|RS": ("4310553", "Itacurubi"),
    "MUNICIPIO DE ITAETE|BA": ("2915007", "Itaeté"),
    "MUNICIPIO DE ITAGIBA|BA": ("2915205", "Itagibá"),
    "MUNICIPIO DE ITAGIMIRIM|BA": ("2915304", "Itagimirim"),
    "MUNICIPIO DE ITAGI|BA": ("2915106", "Itagi"),
    "MUNICIPIO DE ITAGUACU DA BAHIA|BA": ("2915353", "Itaguaçu da Bahia"),
    "MUNICIPIO DE ITAGUACU|ES": ("3202702", "Itaguaçu"),
    "MUNICIPIO DE ITAGUAJE|PR": ("4110904", "Itaguajé"),
    "MUNICIPIO DE ITAGUARA|MG": ("3132206", "Itaguara"),
    "MUNICIPIO DE ITAGUARU|GO": ("5210604", "Itaguaru"),
    "MUNICIPIO DE ITAGUATINS|TO": ("1710706", "Itaguatins"),
    "MUNICIPIO DE ITAICABA|CE": ("2306207", "Itaiçaba"),
    "MUNICIPIO DE ITAINOPOLIS|PI": ("2205003", "Itainópolis"),
    "MUNICIPIO DE ITAIOPOLIS|SC": ("4208104", "Itaiópolis"),
    "MUNICIPIO DE ITAIPAVA DO GRAJAU|MA": ("2105351", "Itaipava do Grajaú"),
    "MUNICIPIO DE ITAIPE|MG": ("3132305", "Itaipé"),
    "MUNICIPIO DE ITAIPULANDIA|PR": ("4110953", "Itaipulândia"),
    "MUNICIPIO DE ITAITINGA|CE": ("2306256", "Itaitinga"),
    "MUNICIPIO DE ITAITUBA|PA": ("1503606", "Itaituba"),
    "MUNICIPIO DE ITAI|SP": ("3521804", "Itaí"),
    "MUNICIPIO DE ITAJAI|SC": ("4208203", "Itajaí"),
    "MUNICIPIO DE ITAJA|GO": ("5210802", "Itajá"),
    "MUNICIPIO DE ITAJA|RN": ("2404853", "Itajá"),
    "MUNICIPIO DE ITAJOBI|SP": ("3521903", "Itajobi"),
    "MUNICIPIO DE ITAJU DO COLONIA|BA": ("2915403", "Itaju do Colônia"),
    "MUNICIPIO DE ITAJUBA|MG": ("3132404", "Itajubá"),
    "MUNICIPIO DE ITAJUIPE|BA": ("2915502", "Itajuípe"),
    "MUNICIPIO DE ITALVA|RJ": ("3302056", "Italva"),
    "MUNICIPIO DE ITAMARAJU|BA": ("2915601", "Itamaraju"),
    "MUNICIPIO DE ITAMARANDIBA|MG": ("3132503", "Itamarandiba"),
    "MUNICIPIO DE ITAMARATI DE MINAS|MG": ("3132602", "Itamarati de Minas"),
    "MUNICIPIO DE ITAMARATI|AM": ("1301951", "Itamarati"),
    "MUNICIPIO DE ITAMARI|BA": ("2915700", "Itamari"),
    "MUNICIPIO DE ITAMBACURI|MG": ("3132701", "Itambacuri"),
    "MUNICIPIO DE ITAMBARACA|PR": ("4111001", "Itambaracá"),
    "MUNICIPIO DE ITAMBE DO MATO DENTRO|MG": ("3132800", "Itambé do Mato Dentro"),
    "MUNICIPIO DE ITAMBE|BA": ("2915809", "Itambé"),
    "MUNICIPIO DE ITAMBE|PE": ("2607653", "Itambé"),
    "MUNICIPIO DE ITAMBE|PR": ("4111100", "Itambé"),
    "MUNICIPIO DE ITAMOGI|MG": ("3132909", "Itamogi"),
    "MUNICIPIO DE ITAMONTE|MG": ("3133006", "Itamonte"),
    "MUNICIPIO DE ITANAGRA|BA": ("2915908", "Itanagra"),
    "MUNICIPIO DE ITANHAEM|SP": ("3522109", "Itanhaém"),
    "MUNICIPIO DE ITANHANDU|MG": ("3133105", "Itanhandu"),
    "MUNICIPIO DE ITANHEM|BA": ("2916005", "Itanhém"),
    "MUNICIPIO DE ITANHOMI|MG": ("3133204", "Itanhomi"),
    "MUNICIPIO DE ITAOBIM|MG": ("3133303", "Itaobim"),
    "MUNICIPIO DE ITAOCARA|RJ": ("3302106", "Itaocara"),
    "MUNICIPIO DE ITAOCA|SP": ("3522158", "Itaoca"),
    "MUNICIPIO DE ITAPACI|GO": ("5210901", "Itapaci"),
    "MUNICIPIO DE ITAPAGIPE|MG": ("3133402", "Itapagipe"),
    "MUNICIPIO DE ITAPAJE|CE": ("2306306", "Itapajé"),
    "MUNICIPIO DE ITAPARICA|BA": ("2916104", "Itaparica"),
    "MUNICIPIO DE ITAPEBI|BA": ("2916302", "Itapebi"),
    "MUNICIPIO DE ITAPECERICA DA SERRA|SP": ("3522208", "Itapecerica da Serra"),
    "MUNICIPIO DE ITAPECERICA|MG": ("3133501", "Itapecerica"),
    "MUNICIPIO DE ITAPECURU MIRIM|MA": ("2105401", "Itapecuru Mirim"),
    "MUNICIPIO DE ITAPEJARA D:OESTE|PR": ("4111209", "Itapejara d'Oeste"),
    "MUNICIPIO DE ITAPEMA|SC": ("4208302", "Itapema"),
    "MUNICIPIO DE ITAPEMIRIM|ES": ("3202801", "Itapemirim"),
    "MUNICIPIO DE ITAPERUCU|PR": ("4111258", "Itaperuçu"),
    "MUNICIPIO DE ITAPETIM|PE": ("2607703", "Itapetim"),
    "MUNICIPIO DE ITAPETINGA|BA": ("2916401", "Itapetinga"),
    "MUNICIPIO DE ITAPETININGA|SP": ("3522307", "Itapetininga"),
    "MUNICIPIO DE ITAPEVA|MG": ("3133600", "Itapeva"),
    "MUNICIPIO DE ITAPEVA|SP": ("3522406", "Itapeva"),
    "MUNICIPIO DE ITAPEVI|SP": ("3522505", "Itapevi"),
    "MUNICIPIO DE ITAPE|BA": ("2916203", "Itapé"),
    "MUNICIPIO DE ITAPICURU|BA": ("2916500", "Itapicuru"),
    "MUNICIPIO DE ITAPIPOCA|CE": ("2306405", "Itapipoca"),
    "MUNICIPIO DE ITAPIRANGA|AM": ("1302009", "Itapiranga"),
    "MUNICIPIO DE ITAPIRANGA|SC": ("4208401", "Itapiranga"),
    "MUNICIPIO DE ITAPIRAPUA PAULISTA|SP": ("3522653", "Itapirapuã Paulista"),
    "MUNICIPIO DE ITAPIRATINS|TO": ("1710904", "Itapiratins"),
    "MUNICIPIO DE ITAPIRA|SP": ("3522604", "Itapira"),
    "MUNICIPIO DE ITAPISSUMA|PE": ("2607752", "Itapissuma"),
    "MUNICIPIO DE ITAPITANGA|BA": ("2916609", "Itapitanga"),
    "MUNICIPIO DE ITAPIUNA|CE": ("2306504", "Itapiúna"),
    "MUNICIPIO DE ITAPOA|SC": ("4208450", "Itapoá"),
    "MUNICIPIO DE ITAPOLIS|SP": ("3522703", "Itápolis"),
    "MUNICIPIO DE ITAPORA DO TOCANTINS|TO": ("1711100", "Itaporã do Tocantins"),
    "MUNICIPIO DE ITAPORANGA|PB": ("2507002", "Itaporanga"),
    "MUNICIPIO DE ITAPORANGA|SP": ("3522802", "Itaporanga"),
    "MUNICIPIO DE ITAPORA|MS": ("5004502", "Itaporã"),
    "MUNICIPIO DE ITAPOROROCA|PB": ("2507101", "Itapororoca"),
    "MUNICIPIO DE ITAPUA DO OESTE|RO": ("1101104", "Itapuã do Oeste"),
    "MUNICIPIO DE ITAPUI|SP": ("3522901", "Itapuí"),
    "MUNICIPIO DE ITAPURANGA|GO": ("5211206", "Itapuranga"),
    "MUNICIPIO DE ITAPURA|SP": ("3523008", "Itapura"),
    "MUNICIPIO DE ITAQUAQUECETUBA|SP": ("3523107", "Itaquaquecetuba"),
    "MUNICIPIO DE ITAQUIRAI|MS": ("5004601", "Itaquiraí"),
    "MUNICIPIO DE ITAQUITINGA|PE": ("2607802", "Itaquitinga"),
    "MUNICIPIO DE ITAQUI|RS": ("4310603", "Itaqui"),
    "MUNICIPIO DE ITARANA|ES": ("3202900", "Itarana"),
    "MUNICIPIO DE ITARARE|SP": ("3523206", "Itararé"),
    "MUNICIPIO DE ITAREMA|CE": ("2306553", "Itarema"),
    "MUNICIPIO DE ITARIRI|SP": ("3523305", "Itariri"),
    "MUNICIPIO DE ITARUMA|GO": ("5211305", "Itarumã"),
    "MUNICIPIO DE ITATIBA DO SUL|RS": ("4310702", "Itatiba do Sul"),
    "MUNICIPIO DE ITATIBA|SP": ("3523404", "Itatiba"),
    "MUNICIPIO DE ITATIM|BA": ("2916856", "Itatim"),
    "MUNICIPIO DE ITATINGA|SP": ("3523503", "Itatinga"),
    "MUNICIPIO DE ITATIRA|CE": ("2306603", "Itatira"),
    "MUNICIPIO DE ITATI|RS": ("4310652", "Itati"),
    "MUNICIPIO DE ITATUBA|PB": ("2507200", "Itatuba"),
    "MUNICIPIO DE ITAU DE MINAS|MG": ("3133758", "Itaú de Minas"),
    "MUNICIPIO DE ITAUBAL|AP": ("1600253", "Itaubal"),
    "MUNICIPIO DE ITAUBA|MT": ("5104559", "Itaúba"),
    "MUNICIPIO DE ITAUCU|GO": ("5211404", "Itauçu"),
    "MUNICIPIO DE ITAUEIRA|PI": ("2205102", "Itaueira"),
    "MUNICIPIO DE ITAUNA DO SUL|PR": ("4111308", "Itaúna do Sul"),
    "MUNICIPIO DE ITAUNA|MG": ("3133808", "Itaúna"),
    "MUNICIPIO DE ITAU|RN": ("2404903", "Itaú"),
    "MUNICIPIO DE ITAVERAVA|MG": ("3133907", "Itaverava"),
    "MUNICIPIO DE ITA|SC": ("4208005", "Itá"),
    "MUNICIPIO DE ITINGA DO MARANHAO|MA": ("2105427", "Itinga do Maranhão"),
    "MUNICIPIO DE ITINGA|MG": ("3134004", "Itinga"),
    "MUNICIPIO DE ITIRAPINA|SP": ("3523602", "Itirapina"),
    "MUNICIPIO DE ITIRUCU|BA": ("2916906", "Itiruçu"),
    "MUNICIPIO DE ITIUBA|BA": ("2917003", "Itiúba"),
    "MUNICIPIO DE ITOBI|SP": ("3523800", "Itobi"),
    "MUNICIPIO DE ITORORO|BA": ("2917102", "Itororó"),
    "MUNICIPIO DE ITUACU|BA": ("2917201", "Ituaçu"),
    "MUNICIPIO DE ITUBERA|BA": ("2917300", "Ituberá"),
    "MUNICIPIO DE ITUETA|MG": ("3134103", "Itueta"),
    "MUNICIPIO DE ITUIUTABA|MG": ("3134202", "Ituiutaba"),
    "MUNICIPIO DE ITUMBIARA|GO": ("5211503", "Itumbiara"),
    "MUNICIPIO DE ITUMIRIM|MG": ("3134301", "Itumirim"),
    "MUNICIPIO DE ITUPEVA|SP": ("3524006", "Itupeva"),
    "MUNICIPIO DE ITUPIRANGA|PA": ("1503705", "Itupiranga"),
    "MUNICIPIO DE ITUPORANGA|SC": ("4208500", "Ituporanga"),
    "MUNICIPIO DE ITURAMA|MG": ("3134400", "Iturama"),
    "MUNICIPIO DE ITUTINGA|MG": ("3134509", "Itutinga"),
    "MUNICIPIO DE ITUVERAVA|SP": ("3524105", "Ituverava"),
    "MUNICIPIO DE ITU|SP": ("3523909", "Itu"),
    "MUNICIPIO DE IUNA|ES": ("3203007", "Iúna"),
    "MUNICIPIO DE IVAIPORA|PR": ("4111506", "Ivaiporã"),
    "MUNICIPIO DE IVAI|PR": ("4111407", "Ivaí"),
    "MUNICIPIO DE IVATE|PR": ("4111555", "Ivaté"),
    "MUNICIPIO DE IVATUBA|PR": ("4111605", "Ivatuba"),
    "MUNICIPIO DE IVINHEMA|MS": ("5004700", "Ivinhema"),
    "MUNICIPIO DE IVOLANDIA|GO": ("5211602", "Ivolândia"),
    "MUNICIPIO DE IVORA|RS": ("4310751", "Ivorá"),
    "MUNICIPIO DE IVOTI|RS": ("4310801", "Ivoti"),
    "MUNICIPIO DE JABOATAO DOS GUARARAPES|PE": ("2607901", "Jaboatão dos Guararapes"),
    "MUNICIPIO DE JABORANDI|BA": ("2917359", "Jaborandi"),
    "MUNICIPIO DE JABORANDI|SP": ("3524204", "Jaborandi"),
    "MUNICIPIO DE JABORA|SC": ("4208609", "Jaborá"),
    "MUNICIPIO DE JABOTICABAL|SP": ("3524303", "Jaboticabal"),
    "MUNICIPIO DE JABOTICATUBAS|MG": ("3134608", "Jaboticatubas"),
    "MUNICIPIO DE JABOTI|PR": ("4111704", "Jaboti"),
    "MUNICIPIO DE JACANA|RN": ("2405009", "Jaçanã"),
    "MUNICIPIO DE JACAREACANGA|PA": ("1503754", "Jacareacanga"),
    "MUNICIPIO DE JACAREI|SP": ("3524402", "Jacareí"),
    "MUNICIPIO DE JACAREZINHO|PR": ("4111803", "Jacarezinho"),
    "MUNICIPIO DE JACIARA|MT": ("5104807", "Jaciara"),
    "MUNICIPIO DE JACINTO MACHADO|SC": ("4208708", "Jacinto Machado"),
    "MUNICIPIO DE JACINTO|MG": ("3134707", "Jacinto"),
    "MUNICIPIO DE JACOBINA DO PIAUI|PI": ("2205151", "Jacobina do Piauí"),
    "MUNICIPIO DE JACOBINA|BA": ("2917508", "Jacobina"),
    "MUNICIPIO DE JACUIPE|AL": ("2703502", "Jacuípe"),
    "MUNICIPIO DE JACUIZINHO|RS": ("4310876", "Jacuizinho"),
    "MUNICIPIO DE JACUI|MG": ("3134806", "Jacuí"),
    "MUNICIPIO DE JACUNDA|PA": ("1503804", "Jacundá"),
    "MUNICIPIO DE JACUPIRANGA|SP": ("3524600", "Jacupiranga"),
    "MUNICIPIO DE JACUTINGA|MG": ("3134905", "Jacutinga"),
    "MUNICIPIO DE JACUTINGA|RS": ("4310900", "Jacutinga"),
    "MUNICIPIO DE JAGUAPITA|PR": ("4111902", "Jaguapitã"),
    "MUNICIPIO DE JAGUAQUARA|BA": ("2917607", "Jaguaquara"),
    "MUNICIPIO DE JAGUARACU|MG": ("3135001", "Jaguaraçu"),
    "MUNICIPIO DE JAGUARAO|RS": ("4311007", "Jaguarão"),
    "MUNICIPIO DE JAGUARARI|BA": ("2917706", "Jaguarari"),
    "MUNICIPIO DE JAGUARETAMA|CE": ("2306702", "Jaguaretama"),
    "MUNICIPIO DE JAGUARE|ES": ("3203056", "Jaguaré"),
    "MUNICIPIO DE JAGUARIAIVA|PR": ("4112009", "Jaguariaíva"),
    "MUNICIPIO DE JAGUARIBARA|CE": ("2306801", "Jaguaribara"),
    "MUNICIPIO DE JAGUARIBE|CE": ("2306900", "Jaguaribe"),
    "MUNICIPIO DE JAGUARIPE|BA": ("2917805", "Jaguaripe"),
    "MUNICIPIO DE JAGUARIUNA|SP": ("3524709", "Jaguariúna"),
    "MUNICIPIO DE JAGUARI|RS": ("4311106", "Jaguari"),
    "MUNICIPIO DE JAGUARUANA|CE": ("2307007", "Jaguaruana"),
    "MUNICIPIO DE JAGUARUNA|SC": ("4208807", "Jaguaruna"),
    "MUNICIPIO DE JAHU|SP": ("3525300", "Jaú"),
    "MUNICIPIO DE JAIBA|MG": ("3135050", "Jaíba"),
    "MUNICIPIO DE JAICOS|PI": ("2205201", "Jaicós"),
    "MUNICIPIO DE JALES|SP": ("3524808", "Jales"),
    "MUNICIPIO DE JAMBEIRO|SP": ("3524907", "Jambeiro"),
    "MUNICIPIO DE JAMPRUCA|MG": ("3135076", "Jampruca"),
    "MUNICIPIO DE JANAUBA|MG": ("3135100", "Janaúba"),
    "MUNICIPIO DE JANDAIA DO SUL|PR": ("4112108", "Jandaia do Sul"),
    "MUNICIPIO DE JANDAIA|GO": ("5211701", "Jandaia"),
    "MUNICIPIO DE JANDAIRA|BA": ("2917904", "Jandaíra"),
    "MUNICIPIO DE JANDAIRA|RN": ("2405108", "Jandaíra"),
    "MUNICIPIO DE JANDIRA|SP": ("3525003", "Jandira"),
    "MUNICIPIO DE JANDUIS|RN": ("2405207", "Janduís"),
    "MUNICIPIO DE JANGADA|MT": ("5104906", "Jangada"),
    "MUNICIPIO DE JANIOPOLIS|PR": ("4112207", "Janiópolis"),
    "MUNICIPIO DE JANUARIA|MG": ("3135209", "Januária"),
    "MUNICIPIO DE JAPARATINGA|AL": ("2703601", "Japaratinga"),
    "MUNICIPIO DE JAPERI|RJ": ("3302270", "Japeri"),
    "MUNICIPIO DE JAPI|RN": ("2405405", "Japi"),
    "MUNICIPIO DE JAPONVAR|MG": ("3135357", "Japonvar"),
    "MUNICIPIO DE JAPORA|MS": ("5004809", "Japorã"),
    "MUNICIPIO DE JAPURA|AM": ("1302108", "Japurá"),
    "MUNICIPIO DE JAPURA|PR": ("4112405", "Japurá"),
    "MUNICIPIO DE JAQUEIRA|PE": ("2607950", "Jaqueira"),
    "MUNICIPIO DE JAQUIRANA|RS": ("4311122", "Jaquirana"),
    "MUNICIPIO DE JARAGUA DO SUL|SC": ("4208906", "Jaraguá do Sul"),
    "MUNICIPIO DE JARAGUA|GO": ("5211800", "Jaraguá"),
    "MUNICIPIO DE JARDIM ALEGRE|PR": ("4112504", "Jardim Alegre"),
    "MUNICIPIO DE JARDIM DE PIRANHAS|RN": ("2405603", "Jardim de Piranhas"),
    "MUNICIPIO DE JARDIM DO MULATO|PI": ("2205250", "Jardim do Mulato"),
    "MUNICIPIO DE JARDIM DO SERIDO|RN": ("2405702", "Jardim do Seridó"),
    "MUNICIPIO DE JARDIM OLINDA|PR": ("4112603", "Jardim Olinda"),
    "MUNICIPIO DE JARDIM|CE": ("2307106", "Jardim"),
    "MUNICIPIO DE JARDIM|MS": ("5005004", "Jardim"),
    "MUNICIPIO DE JARDINOPOLIS|SC": ("4208955", "Jardinópolis"),
    "MUNICIPIO DE JARDINOPOLIS|SP": ("3525102", "Jardinópolis"),
    "MUNICIPIO DE JARI|RS": ("4311130", "Jari"),
    "MUNICIPIO DE JARU|RO": ("1100114", "Jaru"),
    "MUNICIPIO DE JATAIZINHO|PR": ("4112702", "Jataizinho"),
    "MUNICIPIO DE JATAI|GO": ("5211909", "Jataí"),
    "MUNICIPIO DE JATAUBA|PE": ("2608008", "Jataúba"),
    "MUNICIPIO DE JATEI|MS": ("5005103", "Jateí"),
    "MUNICIPIO DE JATOBA DO PIAUI|PI": ("2205276", "Jatobá do Piauí"),
    "MUNICIPIO DE JATOBA|MA": ("2105450", "Jatobá"),
    "MUNICIPIO DE JATOBA|PE": ("2608057", "Jatobá"),
    "MUNICIPIO DE JAU DO TOCANTINS|TO": ("1711506", "Jaú do Tocantins"),
    "MUNICIPIO DE JECEABA|MG": ("3135407", "Jeceaba"),
    "MUNICIPIO DE JENIPAPO DE MINAS|MG": ("3135456", "Jenipapo de Minas"),
    "MUNICIPIO DE JEQUERI|MG": ("3135506", "Jequeri"),
    "MUNICIPIO DE JEQUIA DA PRAIA|AL": ("2703759", "Jequiá da Praia"),
    "MUNICIPIO DE JEQUIE|BA": ("2918001", "Jequié"),
    "MUNICIPIO DE JEQUITAI|MG": ("3135605", "Jequitaí"),
    "MUNICIPIO DE JEQUITIBA|MG": ("3135704", "Jequitibá"),
    "MUNICIPIO DE JEQUITINHONHA|MG": ("3135803", "Jequitinhonha"),
    "MUNICIPIO DE JEREMOABO|BA": ("2918100", "Jeremoabo"),
    "MUNICIPIO DE JERICO|PB": ("2507408", "Jericó"),
    "MUNICIPIO DE JERONIMO MONTEIRO|ES": ("3203106", "Jerônimo Monteiro"),
    "MUNICIPIO DE JERUMENHA|PI": ("2205300", "Jerumenha"),
    "MUNICIPIO DE JESUANIA|MG": ("3135902", "Jesuânia"),
    "MUNICIPIO DE JESUITAS|PR": ("4112751", "Jesuítas"),
    "MUNICIPIO DE JESUPOLIS|GO": ("5212055", "Jesúpolis"),
    "MUNICIPIO DE JI-PARANA|RO": ("1100122", "Ji-Paraná"),
    "MUNICIPIO DE JIQUIRICA|BA": ("2918209", "Jiquiriçá"),
    "MUNICIPIO DE JITAUNA|BA": ("2918308", "Jitaúna"),
    "MUNICIPIO DE JOACABA|SC": ("4209003", "Joaçaba"),
    "MUNICIPIO DE JOAIMA|MG": ("3136009", "Joaíma"),
    "MUNICIPIO DE JOANESIA|MG": ("3136108", "Joanésia"),
    "MUNICIPIO DE JOANOPOLIS|SP": ("3525508", "Joanópolis"),
    "MUNICIPIO DE JOAO ALFREDO|PE": ("2608107", "João Alfredo"),
    "MUNICIPIO DE JOAO CAMARA|RN": ("2405801", "João Câmara"),
    "MUNICIPIO DE JOAO COSTA|PI": ("2205359", "João Costa"),
    "MUNICIPIO DE JOAO DIAS|RN": ("2405900", "João Dias"),
    "MUNICIPIO DE JOAO DOURADO|BA": ("2918357", "João Dourado"),
    "MUNICIPIO DE JOAO LISBOA|MA": ("2105500", "João Lisboa"),
    "MUNICIPIO DE JOAO MONLEVADE|MG": ("3136207", "João Monlevade"),
    "MUNICIPIO DE JOAO NEIVA|ES": ("3203130", "João Neiva"),
    "MUNICIPIO DE JOAO PESSOA|PB": ("2507507", "João Pessoa"),
    "MUNICIPIO DE JOAO PINHEIRO|MG": ("3136306", "João Pinheiro"),
    "MUNICIPIO DE JOAQUIM FELICIO|MG": ("3136405", "Joaquim Felício"),
    "MUNICIPIO DE JOAQUIM GOMES|AL": ("2703809", "Joaquim Gomes"),
    "MUNICIPIO DE JOCA MARQUES|PI": ("2205458", "Joca Marques"),
    "MUNICIPIO DE JOIA|RS": ("4311155", "Jóia"),
    "MUNICIPIO DE JOINVILLE|SC": ("4209102", "Joinville"),
    "MUNICIPIO DE JORDANIA|MG": ("3136504", "Jordânia"),
    "MUNICIPIO DE JORDAO|AC": ("1200328", "Jordão"),
    "MUNICIPIO DE JOSE BOITEUX|SC": ("4209151", "José Boiteux"),
    "MUNICIPIO DE JOSE DE FREITAS|PI": ("2205508", "José de Freitas"),
    "MUNICIPIO DE JOSE RAYDAN|MG": ("3136553", "José Raydan"),
    "MUNICIPIO DE JOSENOPOLIS|MG": ("3136579", "Josenópolis"),
    "MUNICIPIO DE JOVIANIA|GO": ("5212105", "Joviânia"),
    "MUNICIPIO DE JUAREZ TAVORA|PB": ("2507606", "Juarez Távora"),
    "MUNICIPIO DE JUARINA|TO": ("1711803", "Juarina"),
    "MUNICIPIO DE JUATUBA|MG": ("3136652", "Juatuba"),
    "MUNICIPIO DE JUAZEIRINHO|PB": ("2507705", "Juazeirinho"),
    "MUNICIPIO DE JUAZEIRO DO NORTE|CE": ("2307304", "Juazeiro do Norte"),
    "MUNICIPIO DE JUAZEIRO|BA": ("2918407", "Juazeiro"),
    "MUNICIPIO DE JUCAS|CE": ("2307403", "Jucás"),
    "MUNICIPIO DE JUCURUCU|BA": ("2918456", "Jucuruçu"),
    "MUNICIPIO DE JUCURUTU|RN": ("2406106", "Jucurutu"),
    "MUNICIPIO DE JUINA|MT": ("5105150", "Juína"),
    "MUNICIPIO DE JUIZ DE FORA|MG": ("3136702", "Juiz de Fora"),
    "MUNICIPIO DE JULIO BORGES|PI": ("2205524", "Júlio Borges"),
    "MUNICIPIO DE JULIO DE CASTILHOS|RS": ("4311205", "Júlio de Castilhos"),
    "MUNICIPIO DE JUNDIAI DO SUL|PR": ("4112900", "Jundiaí do Sul"),
    "MUNICIPIO DE JUNDIAI|SP": ("3525904", "Jundiaí"),
    "MUNICIPIO DE JUNDIA|AL": ("2703908", "Jundiá"),
    "MUNICIPIO DE JUNDIA|RN": ("2406155", "Jundiá"),
    "MUNICIPIO DE JUNQUEIROPOLIS|SP": ("3526001", "Junqueirópolis"),
    "MUNICIPIO DE JUNQUEIRO|AL": ("2704005", "Junqueiro"),
    "MUNICIPIO DE JUPIA|SC": ("4209177", "Jupiá"),
    "MUNICIPIO DE JUQUIA|SP": ("3526100", "Juquiá"),
    "MUNICIPIO DE JUQUITIBA|SP": ("3526209", "Juquitiba"),
    "MUNICIPIO DE JURANDA|PR": ("4112959", "Juranda"),
    "MUNICIPIO DE JUREMA|PE": ("2608404", "Jurema"),
    "MUNICIPIO DE JUREMA|PI": ("2205532", "Jurema"),
    "MUNICIPIO DE JURIPIRANGA|PB": ("2507903", "Juripiranga"),
    "MUNICIPIO DE JURUAIA|MG": ("3136900", "Juruaia"),
    "MUNICIPIO DE JURUA|AM": ("1302207", "Juruá"),
    "MUNICIPIO DE JURUENA|MT": ("5105176", "Juruena"),
    "MUNICIPIO DE JURUTI|PA": ("1503903", "Juruti"),
    "MUNICIPIO DE JUSCIMEIRA|MT": ("5105200", "Juscimeira"),
    "MUNICIPIO DE JUSSARA|BA": ("2918506", "Jussara"),
    "MUNICIPIO DE JUSSARA|GO": ("5212204", "Jussara"),
    "MUNICIPIO DE JUSSARA|PR": ("4113007", "Jussara"),
    "MUNICIPIO DE JUSSARI|BA": ("2918555", "Jussari"),
    "MUNICIPIO DE JUSSIAPE|BA": ("2918605", "Jussiape"),
    "MUNICIPIO DE JUTAI|AM": ("1302306", "Jutaí"),
    "MUNICIPIO DE JUTI|MS": ("5005152", "Juti"),
    "MUNICIPIO DE JUVENILIA|MG": ("3136959", "Juvenília"),
    "MUNICIPIO DE KALORE|PR": ("4113106", "Kaloré"),
    "MUNICIPIO DE LABREA|AM": ("1302405", "Lábrea"),
    "MUNICIPIO DE LACERDOPOLIS|SC": ("4209201", "Lacerdópolis"),
    "MUNICIPIO DE LADAINHA|MG": ("3137007", "Ladainha"),
    "MUNICIPIO DE LADARIO|MS": ("5005202", "Ladário"),
    "MUNICIPIO DE LAFAIETE COUTINHO|BA": ("2918704", "Lafaiete Coutinho"),
    "MUNICIPIO DE LAGAMAR|MG": ("3137106", "Lagamar"),
    "MUNICIPIO DE LAGARTO|SE": ("2803500", "Lagarto"),
    "MUNICIPIO DE LAGEDO DO TABOCAL|BA": ("2919058", "Lajedo do Tabocal"),
    "MUNICIPIO DE LAGES|SC": ("4209300", "Lages"),
    "MUNICIPIO DE LAGO DA PEDRA|MA": ("2105708", "Lago da Pedra"),
    "MUNICIPIO DE LAGO DO JUNCO|MA": ("2105807", "Lago do Junco"),
    "MUNICIPIO DE LAGO DOS RODRIGUES|MA": ("2105948", "Lago dos Rodrigues"),
    "MUNICIPIO DE LAGOA ALEGRE|PI": ("2205557", "Lagoa Alegre"),
    "MUNICIPIO DE LAGOA BONITA DO SUL|RS": ("4311239", "Lagoa Bonita do Sul"),
    "MUNICIPIO DE LAGOA DA CONFUSAO|TO": ("1711902", "Lagoa da Confusão"),
    "MUNICIPIO DE LAGOA DA PRATA|MG": ("3137205", "Lagoa da Prata"),
    "MUNICIPIO DE LAGOA DANTA|RN": ("2406205", "Lagoa d'Anta"),
    "MUNICIPIO DE LAGOA DE VELHOS|RN": ("2406403", "Lagoa de Velhos"),
    "MUNICIPIO DE LAGOA DO CARRO|PE": ("2608453", "Lagoa do Carro"),
    "MUNICIPIO DE LAGOA DO OURO|PE": ("2608602", "Lagoa do Ouro"),
    "MUNICIPIO DE LAGOA DO PIAUI|PI": ("2205581", "Lagoa do Piauí"),
    "MUNICIPIO DE LAGOA DO SITIO|PI": ("2205599", "Lagoa do Sítio"),
    "MUNICIPIO DE LAGOA DO TOCANTINS|TO": ("1711951", "Lagoa do Tocantins"),
    "MUNICIPIO DE LAGOA DOS PATOS|MG": ("3137304", "Lagoa dos Patos"),
    "MUNICIPIO DE LAGOA DOS TRES CANTOS|RS": ("4311270", "Lagoa dos Três Cantos"),
    "MUNICIPIO DE LAGOA DOURADA|MG": ("3137403", "Lagoa Dourada"),
    "MUNICIPIO DE LAGOA FORMOSA|MG": ("3137502", "Lagoa Formosa"),
    "MUNICIPIO DE LAGOA GRANDE DO MARANHAO|MA": ("2105963", "Lagoa Grande do Maranhão"),
    "MUNICIPIO DE LAGOA GRANDE|MG": ("3137536", "Lagoa Grande"),
    "MUNICIPIO DE LAGOA GRANDE|PE": ("2608750", "Lagoa Grande"),
    "MUNICIPIO DE LAGOA REAL|BA": ("2918753", "Lagoa Real"),
    "MUNICIPIO DE LAGOA SALGADA|RN": ("2406601", "Lagoa Salgada"),
    "MUNICIPIO DE LAGOA SANTA|GO": ("5212253", "Lagoa Santa"),
    "MUNICIPIO DE LAGOA SANTA|MG": ("3137601", "Lagoa Santa"),
    "MUNICIPIO DE LAGOA SECA|PB": ("2508307", "Lagoa Seca"),
    "MUNICIPIO DE LAGOA VERMELHA|RS": ("4311304", "Lagoa Vermelha"),
    "MUNICIPIO DE LAGOA|PB": ("2508109", "Lagoa"),
    "MUNICIPIO DE LAGOINHA DO PIAUI|PI": ("2205540", "Lagoinha do Piauí"),
    "MUNICIPIO DE LAGOINHA|SP": ("3526308", "Lagoinha"),
    "MUNICIPIO DE LAGUNA|SC": ("4209409", "Laguna"),
    "MUNICIPIO DE LAJEADO DO BUGRE|RS": ("4311429", "Lajeado do Bugre"),
    "MUNICIPIO DE LAJEADO|RS": ("4311403", "Lajeado"),
    "MUNICIPIO DE LAJEADO|TO": ("1712009", "Lajeado"),
    "MUNICIPIO DE LAJEDAO|BA": ("2918902", "Lajedão"),
    "MUNICIPIO DE LAJEDINHO|BA": ("2919009", "Lajedinho"),
    "MUNICIPIO DE LAJEDO|PE": ("2608800", "Lajedo"),
    "MUNICIPIO DE LAJES PINTADAS|RN": ("2406809", "Lajes Pintadas"),
    "MUNICIPIO DE LAJES|RN": ("2406700", "Lajes"),
    "MUNICIPIO DE LAJE|BA": ("2918803", "Laje"),
    "MUNICIPIO DE LAJINHA|MG": ("3137700", "Lajinha"),
    "MUNICIPIO DE LAMBARI D'OESTE|MT": ("5105234", "Lambari D'Oeste"),
    "MUNICIPIO DE LAMBARI|MG": ("3137809", "Lambari"),
    "MUNICIPIO DE LAMIM|MG": ("3137908", "Lamim"),
    "MUNICIPIO DE LANDRI SALES|PI": ("2205607", "Landri Sales"),
    "MUNICIPIO DE LAPAO|BA": ("2919157", "Lapão"),
    "MUNICIPIO DE LARANJA DA TERRA|ES": ("3203163", "Laranja da Terra"),
    "MUNICIPIO DE LARANJAL DO JARI|AP": ("1600279", "Laranjal do Jari"),
    "MUNICIPIO DE LARANJAL PAULISTA|SP": ("3526407", "Laranjal Paulista"),
    "MUNICIPIO DE LARANJAL|MG": ("3138005", "Laranjal"),
    "MUNICIPIO DE LARANJAL|PR": ("4113254", "Laranjal"),
    "MUNICIPIO DE LARANJEIRAS DO SUL|PR": ("4113304", "Laranjeiras do Sul"),
    "MUNICIPIO DE LARANJEIRAS|SE": ("2803609", "Laranjeiras"),
    "MUNICIPIO DE LASSANCE|MG": ("3138104", "Lassance"),
    "MUNICIPIO DE LAURENTINO|SC": ("4209508", "Laurentino"),
    "MUNICIPIO DE LAURO DE FREITAS|BA": ("2919207", "Lauro de Freitas"),
    "MUNICIPIO DE LAURO MULLER|SC": ("4209607", "Lauro Müller"),
    "MUNICIPIO DE LAVANDEIRA|TO": ("1712157", "Lavandeira"),
    "MUNICIPIO DE LAVRAS DA MANGABEIRA|CE": ("2307502", "Lavras da Mangabeira"),
    "MUNICIPIO DE LAVRAS DO SUL|RS": ("4311502", "Lavras do Sul"),
    "MUNICIPIO DE LAVRAS|MG": ("3138203", "Lavras"),
    "MUNICIPIO DE LAVRINHAS|SP": ("3526605", "Lavrinhas"),
    "MUNICIPIO DE LEANDRO FERREIRA|MG": ("3138302", "Leandro Ferreira"),
    "MUNICIPIO DE LEBON REGIS|SC": ("4209706", "Lebon Régis"),
    "MUNICIPIO DE LEME DO PRADO|MG": ("3138351", "Leme do Prado"),
    "MUNICIPIO DE LEME|SP": ("3526704", "Leme"),
    "MUNICIPIO DE LENCOIS PAULISTA|SP": ("3526803", "Lençóis Paulista"),
    "MUNICIPIO DE LENCOIS|BA": ("2919306", "Lençóis"),
    "MUNICIPIO DE LEOBERTO LEAL|SC": ("4209805", "Leoberto Leal"),
    "MUNICIPIO DE LEOPOLDINA|MG": ("3138401", "Leopoldina"),
    "MUNICIPIO DE LEOPOLDO DE BULHOES|GO": ("5212303", "Leopoldo de Bulhões"),
    "MUNICIPIO DE LEOPOLIS|PR": ("4113403", "Leópolis"),
    "MUNICIPIO DE LIBERDADE|MG": ("3138500", "Liberdade"),
    "MUNICIPIO DE LICINIO DE ALMEIDA|BA": ("2919405", "Licínio de Almeida"),
    "MUNICIPIO DE LIDIANOPOLIS|PR": ("4113429", "Lidianópolis"),
    "MUNICIPIO DE LIMA CAMPOS|MA": ("2106003", "Lima Campos"),
    "MUNICIPIO DE LIMA DUARTE|MG": ("3138609", "Lima Duarte"),
    "MUNICIPIO DE LIMEIRA DO OESTE|MG": ("3138625", "Limeira do Oeste"),
    "MUNICIPIO DE LIMEIRA|SP": ("3526902", "Limeira"),
    "MUNICIPIO DE LIMOEIRO DO NORTE|CE": ("2307601", "Limoeiro do Norte"),
    "MUNICIPIO DE LINDOESTE|PR": ("4113452", "Lindoeste"),
    "MUNICIPIO DE LINDOIA DO SUL|SC": ("4209854", "Lindóia do Sul"),
    "MUNICIPIO DE LINDOLFO COLLOR|RS": ("4311627", "Lindolfo Collor"),
    "MUNICIPIO DE LINS|SP": ("3527108", "Lins"),
    "MUNICIPIO DE LIVRAMENTO DE NOSSA SENHORA|BA": ("2919504", "Livramento de Nossa Senhora"),
    "MUNICIPIO DE LIZARDA|TO": ("1712405", "Lizarda"),
    "MUNICIPIO DE LOANDA|PR": ("4113502", "Loanda"),
    "MUNICIPIO DE LOBATO|PR": ("4113601", "Lobato"),
    "MUNICIPIO DE LOGRADOURO|PB": ("2508554", "Logradouro"),
    "MUNICIPIO DE LONDRINA|PR": ("4113700", "Londrina"),
    "MUNICIPIO DE LONTRAS|SC": ("4209904", "Lontras"),
    "MUNICIPIO DE LONTRA|MG": ("3138658", "Lontra"),
    "MUNICIPIO DE LORENA|SP": ("3527207", "Lorena"),
    "MUNICIPIO DE LOUVEIRA|SP": ("3527306", "Louveira"),
    "MUNICIPIO DE LUCELIA|SP": ("3527405", "Lucélia"),
    "MUNICIPIO DE LUCENA|PB": ("2508604", "Lucena"),
    "MUNICIPIO DE LUCIANOPOLIS|SP": ("3527504", "Lucianópolis"),
    "MUNICIPIO DE LUCIARA|MT": ("5105309", "Luciara"),
    "MUNICIPIO DE LUCRECIA|RN": ("2406908", "Lucrécia"),
    "MUNICIPIO DE LUIS ANTONIO|SP": ("3527603", "Luís Antônio"),
    "MUNICIPIO DE LUIS CORREIA|PI": ("2205706", "Luís Correia"),
    "MUNICIPIO DE LUIS EDUARDO MAGALHAES|BA": ("2919553", "Luís Eduardo Magalhães"),
    "MUNICIPIO DE LUIS GOMES|RN": ("2407005", "Luís Gomes"),
    "MUNICIPIO DE LUISBURGO|MG": ("3138674", "Luisburgo"),
    "MUNICIPIO DE LUISLANDIA|MG": ("3138682", "Luislândia"),
    "MUNICIPIO DE LUIZ ALVES|SC": ("4210001", "Luiz Alves"),
    "MUNICIPIO DE LUIZIANA|PR": ("4113734", "Luiziana"),
    "MUNICIPIO DE LUIZIANIA|SP": ("3527702", "Luiziânia"),
    "MUNICIPIO DE LUMINARIAS|MG": ("3138708", "Luminárias"),
    "MUNICIPIO DE LUNARDELLI|PR": ("4113759", "Lunardelli"),
    "MUNICIPIO DE LUPIONOPOLIS|PR": ("4113809", "Lupionópolis"),
    "MUNICIPIO DE LUZERNA|SC": ("4210035", "Luzerna"),
    "MUNICIPIO DE LUZIANIA|GO": ("5212501", "Luziânia"),
    "MUNICIPIO DE LUZINOPOLIS|TO": ("1712454", "Luzinópolis"),
    "MUNICIPIO DE LUZ|MG": ("3138807", "Luz"),
    "MUNICIPIO DE MACAE|RJ": ("3302403", "Macaé"),
    "MUNICIPIO DE MACAIBA|RN": ("2407104", "Macaíba"),
    "MUNICIPIO DE MACAJUBA|BA": ("2919603", "Macajuba"),
    "MUNICIPIO DE MACAMBARA|RS": ("4311718", "Maçambará"),
    "MUNICIPIO DE MACAMBIRA|SE": ("2803708", "Macambira"),
    "MUNICIPIO DE MACAPARANA|PE": ("2609006", "Macaparana"),
    "MUNICIPIO DE MACAPA|AP": ("1600303", "Macapá"),
    "MUNICIPIO DE MACARANI|BA": ("2919702", "Macarani"),
    "MUNICIPIO DE MACATUBA|SP": ("3528007", "Macatuba"),
    "MUNICIPIO DE MACAU|RN": ("2407203", "Macau"),
    "MUNICIPIO DE MACEDONIA|SP": ("3528205", "Macedônia"),
    "MUNICIPIO DE MACEIO|AL": ("2704302", "Maceió"),
    "MUNICIPIO DE MACHACALIS|MG": ("3138906", "Machacalis"),
    "MUNICIPIO DE MACHADINHO D'OESTE|RO": ("1100130", "Machadinho D'Oeste"),
    "MUNICIPIO DE MACHADINHO|RS": ("4311700", "Machadinho"),
    "MUNICIPIO DE MACHADOS|PE": ("2609105", "Machados"),
    "MUNICIPIO DE MACHADO|MG": ("3139003", "Machado"),
    "MUNICIPIO DE MACIEIRA|SC": ("4210050", "Macieira"),
    "MUNICIPIO DE MACURURE|BA": ("2919900", "Macururé"),
    "MUNICIPIO DE MADALENA|CE": ("2307635", "Madalena"),
    "MUNICIPIO DE MADEIRO|PI": ("2205854", "Madeiro"),
    "MUNICIPIO DE MADRE DE DEUS DE MINAS|MG": ("3139102", "Madre de Deus de Minas"),
    "MUNICIPIO DE MADRE DE DEUS|BA": ("2919926", "Madre de Deus"),
    "MUNICIPIO DE MAE D'AGUA|PB": ("2508703", "Mãe d'Água"),
    "MUNICIPIO DE MAE DO RIO|PA": ("1504059", "Mãe do Rio"),
    "MUNICIPIO DE MAETINGA|BA": ("2919959", "Maetinga"),
    "MUNICIPIO DE MAGALHAES DE ALMEIDA|MA": ("2106300", "Magalhães de Almeida"),
    "MUNICIPIO DE MAGDA|SP": ("3528304", "Magda"),
    "MUNICIPIO DE MAGE|RJ": ("3302502", "Magé"),
    "MUNICIPIO DE MAIQUINIQUE|BA": ("2920007", "Maiquinique"),
    "MUNICIPIO DE MAIRINQUE|SP": ("3528403", "Mairinque"),
    "MUNICIPIO DE MAIRIPORA|SP": ("3528502", "Mairiporã"),
    "MUNICIPIO DE MAIRI|BA": ("2920106", "Mairi"),
    "MUNICIPIO DE MAJOR GERCINO|SC": ("4210209", "Major Gercino"),
    "MUNICIPIO DE MAJOR SALES|RN": ("2407252", "Major Sales"),
    "MUNICIPIO DE MAJOR VIEIRA|SC": ("4210308", "Major Vieira"),
    "MUNICIPIO DE MALHADA DE PEDRAS|BA": ("2920304", "Malhada de Pedras"),
    "MUNICIPIO DE MALHADA DOS BOIS|SE": ("2803807", "Malhada dos Bois"),
    "MUNICIPIO DE MALHADA|BA": ("2920205", "Malhada"),
    "MUNICIPIO DE MALHADOR|SE": ("2803906", "Malhador"),
    "MUNICIPIO DE MALLET|PR": ("4113908", "Mallet"),
    "MUNICIPIO DE MALTA|PB": ("2508802", "Malta"),
    "MUNICIPIO DE MAMBAI|GO": ("5212709", "Mambaí"),
    "MUNICIPIO DE MAMBORE|PR": ("4114005", "Mamborê"),
    "MUNICIPIO DE MAMONAS|MG": ("3139250", "Mamonas"),
    "MUNICIPIO DE MAMPITUBA|RS": ("4311734", "Mampituba"),
    "MUNICIPIO DE MANACAPURU|AM": ("1302504", "Manacapuru"),
    "MUNICIPIO DE MANAIRA|PB": ("2509008", "Manaíra"),
    "MUNICIPIO DE MANAUS|AM": ("1302603", "Manaus"),
    "MUNICIPIO DE MANCIO LIMA|AC": ("1200336", "Mâncio Lima"),
    "MUNICIPIO DE MANDAGUACU|PR": ("4114104", "Mandaguaçu"),
    "MUNICIPIO DE MANDAGUARI|PR": ("4114203", "Mandaguari"),
    "MUNICIPIO DE MANDIRITUBA|PR": ("4114302", "Mandirituba"),
    "MUNICIPIO DE MANFRINOPOLIS|PR": ("4114351", "Manfrinópolis"),
    "MUNICIPIO DE MANGARATIBA|RJ": ("3302601", "Mangaratiba"),
    "MUNICIPIO DE MANGA|MG": ("3139300", "Manga"),
    "MUNICIPIO DE MANGUEIRINHA|PR": ("4114401", "Mangueirinha"),
    "MUNICIPIO DE MANHUACU|MG": ("3139409", "Manhuaçu"),
    "MUNICIPIO DE MANHUMIRIM|MG": ("3139508", "Manhumirim"),
    "MUNICIPIO DE MANICORE|AM": ("1302702", "Manicoré"),
    "MUNICIPIO DE MANOEL EMIDIO|PI": ("2205904", "Manoel Emídio"),
    "MUNICIPIO DE MANOEL RIBAS|PR": ("4114500", "Manoel Ribas"),
    "MUNICIPIO DE MANOEL URBANO|AC": ("1200344", "Manoel Urbano"),
    "MUNICIPIO DE MANOEL VIANA|RS": ("4311759", "Manoel Viana"),
    "MUNICIPIO DE MANOEL VITORINO|BA": ("2920403", "Manoel Vitorino"),
    "MUNICIPIO DE MANTENA|MG": ("3139607", "Mantena"),
    "MUNICIPIO DE MANTENOPOLIS|ES": ("3203304", "Mantenópolis"),
    "MUNICIPIO DE MAR DE ESPANHA|MG": ("3139805", "Mar de Espanha"),
    "MUNICIPIO DE MAR VERMELHO|AL": ("2704906", "Mar Vermelho"),
    "MUNICIPIO DE MARA ROSA|GO": ("5212808", "Mara Rosa"),
    "MUNICIPIO DE MARAA|AM": ("1302801", "Maraã"),
    "MUNICIPIO DE MARABA PAULISTA|SP": ("3528700", "Marabá Paulista"),
    "MUNICIPIO DE MARABA|PA": ("1504208", "Marabá"),
    "MUNICIPIO DE MARACAI|SP": ("3528809", "Maracaí"),
    "MUNICIPIO DE MARACAJA|SC": ("4210407", "Maracajá"),
    "MUNICIPIO DE MARACAJU|MS": ("5005400", "Maracaju"),
    "MUNICIPIO DE MARACANAU|CE": ("2307650", "Maracanaú"),
    "MUNICIPIO DE MARACANA|PA": ("1504307", "Maracanã"),
    "MUNICIPIO DE MARACAS|BA": ("2920502", "Maracás"),
    "MUNICIPIO DE MARAGOGIPE|BA": ("2920601", "Maragogipe"),
    "MUNICIPIO DE MARAGOGI|AL": ("2704500", "Maragogi"),
    "MUNICIPIO DE MARAIAL|PE": ("2609204", "Maraial"),
    "MUNICIPIO DE MARAJA DO SENA|MA": ("2106359", "Marajá do Sena"),
    "MUNICIPIO DE MARANGUAPE|CE": ("2307700", "Maranguape"),
    "MUNICIPIO DE MARANHAOZINHO|MA": ("2106375", "Maranhãozinho"),
    "MUNICIPIO DE MARAPANIM|PA": ("1504406", "Marapanim"),
    "MUNICIPIO DE MARAPOAMA|SP": ("3528858", "Marapoama"),
    "MUNICIPIO DE MARATA|RS": ("4311791", "Maratá"),
    "MUNICIPIO DE MARAU|RS": ("4311809", "Marau"),
    "MUNICIPIO DE MARAVILHAS|MG": ("3139706", "Maravilhas"),
    "MUNICIPIO DE MARAVILHA|AL": ("2704609", "Maravilha"),
    "MUNICIPIO DE MARAVILHA|SC": ("4210506", "Maravilha"),
    "MUNICIPIO DE MARCACAO|PB": ("2509057", "Marcação"),
    "MUNICIPIO DE MARCELANDIA|MT": ("5105580", "Marcelândia"),
    "MUNICIPIO DE MARCELINO RAMOS|RS": ("4311908", "Marcelino Ramos"),
    "MUNICIPIO DE MARCOLANDIA|PI": ("2205953", "Marcolândia"),
    "MUNICIPIO DE MARCO|CE": ("2307809", "Marco"),
    "MUNICIPIO DE MARECHAL CANDIDO RONDON|PR": ("4114609", "Marechal Cândido Rondon"),
    "MUNICIPIO DE MARECHAL DEODORO|AL": ("2704708", "Marechal Deodoro"),
    "MUNICIPIO DE MARECHAL FLORIANO|ES": ("3203346", "Marechal Floriano"),
    "MUNICIPIO DE MARECHAL THAUMATURGO|AC": ("1200351", "Marechal Thaumaturgo"),
    "MUNICIPIO DE MAREMA|SC": ("4210555", "Marema"),
    "MUNICIPIO DE MARIA DA FE|MG": ("3139904", "Maria da Fé"),
    "MUNICIPIO DE MARIA HELENA|PR": ("4114708", "Maria Helena"),
    "MUNICIPIO DE MARIALVA|PR": ("4114807", "Marialva"),
    "MUNICIPIO DE MARIANA|MG": ("3140001", "Mariana"),
    "MUNICIPIO DE MARIANO MORO|RS": ("4312005", "Mariano Moro"),
    "MUNICIPIO DE MARIANOPOLIS DO TOCANTINS|TO": ("1712504", "Marianópolis do Tocantins"),
    "MUNICIPIO DE MARIAPOLIS|SP": ("3528908", "Mariápolis"),
    "MUNICIPIO DE MARILAC|MG": ("3140100", "Marilac"),
    "MUNICIPIO DE MARILANDIA|ES": ("3203353", "Marilândia"),
    "MUNICIPIO DE MARILENA|PR": ("4115002", "Marilena"),
    "MUNICIPIO DE MARILIA|SP": ("3529005", "Marília"),
    "MUNICIPIO DE MARILUZ|PR": ("4115101", "Mariluz"),
    "MUNICIPIO DE MARINGA|PR": ("4115200", "Maringá"),
    "MUNICIPIO DE MARIO CAMPOS|MG": ("3140159", "Mário Campos"),
    "MUNICIPIO DE MARIOPOLIS|PR": ("4115309", "Mariópolis"),
    "MUNICIPIO DE MARIPA|PR": ("4115358", "Maripá"),
    "MUNICIPIO DE MARITUBA|PA": ("1504422", "Marituba"),
    "MUNICIPIO DE MARIZOPOLIS|PB": ("2509156", "Marizópolis"),
    "MUNICIPIO DE MARLIERIA|MG": ("3140308", "Marliéria"),
    "MUNICIPIO DE MARMELEIRO|PR": ("4115408", "Marmeleiro"),
    "MUNICIPIO DE MARMELOPOLIS|MG": ("3140407", "Marmelópolis"),
    "MUNICIPIO DE MARQUES DE SOUZA|RS": ("4312054", "Marques de Souza"),
    "MUNICIPIO DE MARQUINHO|PR": ("4115457", "Marquinho"),
    "MUNICIPIO DE MARTINHO CAMPOS|MG": ("3140506", "Martinho Campos"),
    "MUNICIPIO DE MARTINOPOLE|CE": ("2307908", "Martinópole"),
    "MUNICIPIO DE MARTINOPOLIS|SP": ("3529203", "Martinópolis"),
    "MUNICIPIO DE MARTINS SOARES|MG": ("3140530", "Martins Soares"),
    "MUNICIPIO DE MARTINS|RN": ("2407401", "Martins"),
    "MUNICIPIO DE MARUIM|SE": ("2804003", "Maruim"),
    "MUNICIPIO DE MARUMBI|PR": ("4115507", "Marumbi"),
    "MUNICIPIO DE MARZAGAO|GO": ("5212907", "Marzagão"),
    "MUNICIPIO DE MASSAPE DO PIAUI|PI": ("2206050", "Massapê do Piauí"),
    "MUNICIPIO DE MASSAPE|CE": ("2308005", "Massapê"),
    "MUNICIPIO DE MASSARANDUBA|PB": ("2509206", "Massaranduba"),
    "MUNICIPIO DE MASSARANDUBA|SC": ("4210605", "Massaranduba"),
    "MUNICIPIO DE MATA DE SAO JOAO|BA": ("2921005", "Mata de São João"),
    "MUNICIPIO DE MATA GRANDE|AL": ("2705002", "Mata Grande"),
    "MUNICIPIO DE MATA ROMA|MA": ("2106409", "Mata Roma"),
    "MUNICIPIO DE MATA VERDE|MG": ("3140555", "Mata Verde"),
    "MUNICIPIO DE MATAO|SP": ("3529302", "Matão"),
    "MUNICIPIO DE MATARACA|PB": ("2509305", "Mataraca"),
    "MUNICIPIO DE MATEIROS|TO": ("1712702", "Mateiros"),
    "MUNICIPIO DE MATELANDIA|PR": ("4115606", "Matelândia"),
    "MUNICIPIO DE MATERLANDIA|MG": ("3140605", "Materlândia"),
    "MUNICIPIO DE MATEUS LEME|MG": ("3140704", "Mateus Leme"),
    "MUNICIPIO DE MATHIAS LOBATO|MG": ("3171501", "Mathias Lobato"),
    "MUNICIPIO DE MATIAS BARBOSA|MG": ("3140803", "Matias Barbosa"),
    "MUNICIPIO DE MATIAS CARDOSO|MG": ("3140852", "Matias Cardoso"),
    "MUNICIPIO DE MATIAS OLIMPIO|PI": ("2206100", "Matias Olímpio"),
    "MUNICIPIO DE MATINA|BA": ("2921054", "Matina"),
    "MUNICIPIO DE MATINHAS|PB": ("2509339", "Matinhas"),
    "MUNICIPIO DE MATINHOS|PR": ("4115705", "Matinhos"),
    "MUNICIPIO DE MATIPO|MG": ("3140902", "Matipó"),
    "MUNICIPIO DE MATO CASTELHANO|RS": ("4312138", "Mato Castelhano"),
    "MUNICIPIO DE MATO GROSSO|PB": ("2509370", "Mato Grosso"),
    "MUNICIPIO DE MATO QUEIMADO|RS": ("4312179", "Mato Queimado"),
    "MUNICIPIO DE MATO RICO|PR": ("4115739", "Mato Rico"),
    "MUNICIPIO DE MATO VERDE|MG": ("3141009", "Mato Verde"),
    "MUNICIPIO DE MATOES DO NORTE|MA": ("2106631", "Matões do Norte"),
    "MUNICIPIO DE MATOS COSTA|SC": ("4210704", "Matos Costa"),
    "MUNICIPIO DE MATOZINHOS|MG": ("3141108", "Matozinhos"),
    "MUNICIPIO DE MATRINCHA|GO": ("5212956", "Matrinchã"),
    "MUNICIPIO DE MATRIZ DE CAMARAGIBE|AL": ("2705101", "Matriz de Camaragibe"),
    "MUNICIPIO DE MATUPA|MT": ("5105606", "Matupá"),
    "MUNICIPIO DE MATUREIA|PB": ("2509396", "Maturéia"),
    "MUNICIPIO DE MATUTINA|MG": ("3141207", "Matutina"),
    "MUNICIPIO DE MAUA DA SERRA|PR": ("4115754", "Mauá da Serra"),
    "MUNICIPIO DE MAUA|SP": ("3529401", "Mauá"),
    "MUNICIPIO DE MAUES|AM": ("1302900", "Maués"),
    "MUNICIPIO DE MAURILANDIA DO TOCANTINS|TO": ("1712801", "Maurilândia do Tocantins"),
    "MUNICIPIO DE MAURILANDIA|GO": ("5213004", "Maurilândia"),
    "MUNICIPIO DE MAURITI|CE": ("2308104", "Mauriti"),
    "MUNICIPIO DE MAXARANGUAPE|RN": ("2407500", "Maxaranguape"),
    "MUNICIPIO DE MAZAGAO|AP": ("1600402", "Mazagão"),
    "MUNICIPIO DE MEDEIROS NETO|BA": ("2921104", "Medeiros Neto"),
    "MUNICIPIO DE MEDEIROS|MG": ("3141306", "Medeiros"),
    "MUNICIPIO DE MEDIANEIRA|PR": ("4115804", "Medianeira"),
    "MUNICIPIO DE MEDICILANDIA|PA": ("1504455", "Medicilândia"),
    "MUNICIPIO DE MEDINA|MG": ("3141405", "Medina"),
    "MUNICIPIO DE MELEIRO|SC": ("4210803", "Meleiro"),
    "MUNICIPIO DE MELGACO|PA": ("1504505", "Melgaço"),
    "MUNICIPIO DE MENDES PIMENTEL|MG": ("3141504", "Mendes Pimentel"),
    "MUNICIPIO DE MENDES|RJ": ("3302809", "Mendes"),
    "MUNICIPIO DE MENDONCA|SP": ("3529500", "Mendonça"),
    "MUNICIPIO DE MERCEDES|PR": ("4115853", "Mercedes"),
    "MUNICIPIO DE MERCES|MG": ("3141603", "Mercês"),
    "MUNICIPIO DE MERIDIANO|SP": ("3529609", "Meridiano"),
    "MUNICIPIO DE MERUOCA|CE": ("2308203", "Meruoca"),
    "MUNICIPIO DE MESQUITA|MG": ("3141702", "Mesquita"),
    "MUNICIPIO DE MESQUITA|RJ": ("3302858", "Mesquita"),
    "MUNICIPIO DE MESSIAS TARGINO|RN": ("2407609", "Messias Targino"),
    "MUNICIPIO DE MESSIAS|AL": ("2705200", "Messias"),
    "MUNICIPIO DE MIGUEL ALVES|PI": ("2206209", "Miguel Alves"),
    "MUNICIPIO DE MIGUEL CALMON|BA": ("2921203", "Miguel Calmon"),
    "MUNICIPIO DE MIGUEL LEAO|PI": ("2206308", "Miguel Leão"),
    "MUNICIPIO DE MIGUEL PEREIRA|RJ": ("3302908", "Miguel Pereira"),
    "MUNICIPIO DE MIGUELOPOLIS|SP": ("3529708", "Miguelópolis"),
    "MUNICIPIO DE MILAGRES|BA": ("2921302", "Milagres"),
    "MUNICIPIO DE MILAGRES|CE": ("2308302", "Milagres"),
    "MUNICIPIO DE MILHA|CE": ("2308351", "Milhã"),
    "MUNICIPIO DE MILTON BRANDAO|PI": ("2206357", "Milton Brandão"),
    "MUNICIPIO DE MIMOSO DE GOIAS|GO": ("5213053", "Mimoso de Goiás"),
    "MUNICIPIO DE MIMOSO DO SUL|ES": ("3203403", "Mimoso do Sul"),
    "MUNICIPIO DE MINACU|GO": ("5213087", "Minaçu"),
    "MUNICIPIO DE MINAS DO LEAO|RS": ("4312252", "Minas do Leão"),
    "MUNICIPIO DE MINAS NOVAS|MG": ("3141801", "Minas Novas"),
    "MUNICIPIO DE MINDURI|MG": ("3141900", "Minduri"),
    "MUNICIPIO DE MINEIROS DO TIETE|SP": ("3529807", "Mineiros do Tietê"),
    "MUNICIPIO DE MINEIROS|GO": ("5213103", "Mineiros"),
    "MUNICIPIO DE MINISTRO ANDREAZZA|RO": ("1101203", "Ministro Andreazza"),
    "MUNICIPIO DE MIRA ESTRELA|SP": ("3530003", "Mira Estrela"),
    "MUNICIPIO DE MIRABELA|MG": ("3142007", "Mirabela"),
    "MUNICIPIO DE MIRACEMA DO TOCANTINS|TO": ("1713205", "Miracema do Tocantins"),
    "MUNICIPIO DE MIRACEMA|RJ": ("3303005", "Miracema"),
    "MUNICIPIO DE MIRADOR|MA": ("2106706", "Mirador"),
    "MUNICIPIO DE MIRADOR|PR": ("4115903", "Mirador"),
    "MUNICIPIO DE MIRADOURO|MG": ("3142106", "Miradouro"),
    "MUNICIPIO DE MIRAGUAI|RS": ("4312302", "Miraguaí"),
    "MUNICIPIO DE MIRAIMA|CE": ("2308377", "Miraíma"),
    "MUNICIPIO DE MIRAI|MG": ("3142205", "Miraí"),
    "MUNICIPIO DE MIRANDA DO NORTE|MA": ("2106755", "Miranda do Norte"),
    "MUNICIPIO DE MIRANDA|MS": ("5005608", "Miranda"),
    "MUNICIPIO DE MIRANDIBA|PE": ("2609303", "Mirandiba"),
    "MUNICIPIO DE MIRANDOPOLIS|SP": ("3530102", "Mirandópolis"),
    "MUNICIPIO DE MIRANGABA|BA": ("2921401", "Mirangaba"),
    "MUNICIPIO DE MIRANORTE|TO": ("1713304", "Miranorte"),
    "MUNICIPIO DE MIRANTE DA SERRA|RO": ("1101302", "Mirante da Serra"),
    "MUNICIPIO DE MIRANTE DO PARANAPANEMA|SP": ("3530201", "Mirante do Paranapanema"),
    "MUNICIPIO DE MIRANTE|BA": ("2921450", "Mirante"),
    "MUNICIPIO DE MIRASELVA|PR": ("4116000", "Miraselva"),
    "MUNICIPIO DE MIRASSOL D:OESTE|MT": ("5105622", "Mirassol d'Oeste"),
    "MUNICIPIO DE MIRASSOLANDIA|SP": ("3530409", "Mirassolândia"),
    "MUNICIPIO DE MIRASSOL|SP": ("3530300", "Mirassol"),
    "MUNICIPIO DE MIRAVANIA|MG": ("3142254", "Miravânia"),
    "MUNICIPIO DE MIRIM DOCE|SC": ("4210852", "Mirim Doce"),
    "MUNICIPIO DE MISSAL|PR": ("4116059", "Missal"),
    "MUNICIPIO DE MISSAO VELHA|CE": ("2308401", "Missão Velha"),
    "MUNICIPIO DE MOCOCA|SP": ("3530508", "Mococa"),
    "MUNICIPIO DE MODELO|SC": ("4210902", "Modelo"),
    "MUNICIPIO DE MOEDA|MG": ("3142304", "Moeda"),
    "MUNICIPIO DE MOEMA|MG": ("3142403", "Moema"),
    "MUNICIPIO DE MOGI DAS CRUZES|SP": ("3530607", "Mogi das Cruzes"),
    "MUNICIPIO DE MOGI-GUACU|SP": ("3530706", "Mogi Guaçu"),
    "MUNICIPIO DE MOGI-MIRIM|SP": ("3530805", "Mogi Mirim"),
    "MUNICIPIO DE MOIPORA|GO": ("5213400", "Moiporá"),
    "MUNICIPIO DE MOITA BONITA|SE": ("2804102", "Moita Bonita"),
    "MUNICIPIO DE MOJUI DOS CAMPOS|PA": ("1504752", "Mojuí dos Campos"),
    "MUNICIPIO DE MOJU|PA": ("1504703", "Moju"),
    "MUNICIPIO DE MOMBACA|CE": ("2308500", "Mombaça"),
    "MUNICIPIO DE MOMBUCA|SP": ("3530904", "Mombuca"),
    "MUNICIPIO DE MONDAI|SC": ("4211009", "Mondaí"),
    "MUNICIPIO DE MONJOLOS|MG": ("3142502", "Monjolos"),
    "MUNICIPIO DE MONSENHOR GIL|PI": ("2206407", "Monsenhor Gil"),
    "MUNICIPIO DE MONSENHOR HIPOLITO|PI": ("2206506", "Monsenhor Hipólito"),
    "MUNICIPIO DE MONSENHOR PAULO|MG": ("3142601", "Monsenhor Paulo"),
    "MUNICIPIO DE MONSENHOR TABOSA|CE": ("2308609", "Monsenhor Tabosa"),
    "MUNICIPIO DE MONTADAS|PB": ("2509503", "Montadas"),
    "MUNICIPIO DE MONTALVANIA|MG": ("3142700", "Montalvânia"),
    "MUNICIPIO DE MONTANHAS|RN": ("2407708", "Montanhas"),
    "MUNICIPIO DE MONTANHA|ES": ("3203502", "Montanha"),
    "MUNICIPIO DE MONTE ALEGRE DE GOIAS|GO": ("5213509", "Monte Alegre de Goiás"),
    "MUNICIPIO DE MONTE ALEGRE DE MINAS|MG": ("3142809", "Monte Alegre de Minas"),
    "MUNICIPIO DE MONTE ALEGRE DE SERGIPE|SE": ("2804201", "Monte Alegre de Sergipe"),
    "MUNICIPIO DE MONTE ALEGRE DO PIAUI|PI": ("2206605", "Monte Alegre do Piauí"),
    "MUNICIPIO DE MONTE ALEGRE DO SUL|SP": ("3531209", "Monte Alegre do Sul"),
    "MUNICIPIO DE MONTE ALEGRE DOS CAMPOS|RS": ("4312377", "Monte Alegre dos Campos"),
    "MUNICIPIO DE MONTE ALEGRE|PA": ("1504802", "Monte Alegre"),
    "MUNICIPIO DE MONTE ALEGRE|RN": ("2407807", "Monte Alegre"),
    "MUNICIPIO DE MONTE ALTO|SP": ("3531308", "Monte Alto"),
    "MUNICIPIO DE MONTE APRAZIVEL|SP": ("3531407", "Monte Aprazível"),
    "MUNICIPIO DE MONTE AZUL PAULISTA|SP": ("3531506", "Monte Azul Paulista"),
    "MUNICIPIO DE MONTE AZUL|MG": ("3142908", "Monte Azul"),
    "MUNICIPIO DE MONTE BELO DO SUL|RS": ("4312385", "Monte Belo do Sul"),
    "MUNICIPIO DE MONTE BELO|MG": ("3143005", "Monte Belo"),
    "MUNICIPIO DE MONTE CARLO|SC": ("4211058", "Monte Carlo"),
    "MUNICIPIO DE MONTE CARMELO|MG": ("3143104", "Monte Carmelo"),
    "MUNICIPIO DE MONTE CASTELO|SC": ("4211108", "Monte Castelo"),
    "MUNICIPIO DE MONTE CASTELO|SP": ("3531605", "Monte Castelo"),
    "MUNICIPIO DE MONTE DAS GAMELEIRAS|RN": ("2407906", "Monte das Gameleiras"),
    "MUNICIPIO DE MONTE DO CARMO|TO": ("1713601", "Monte do Carmo"),
    "MUNICIPIO DE MONTE MOR|SP": ("3531803", "Monte Mor"),
    "MUNICIPIO DE MONTE NEGRO|RO": ("1101401", "Monte Negro"),
    "MUNICIPIO DE MONTE SANTO DE MINAS|MG": ("3143203", "Monte Santo de Minas"),
    "MUNICIPIO DE MONTE SANTO DO TOCANTINS|TO": ("1713700", "Monte Santo do Tocantins"),
    "MUNICIPIO DE MONTE SANTO|BA": ("2921500", "Monte Santo"),
    "MUNICIPIO DE MONTE SIAO|MG": ("3143401", "Monte Sião"),
    "MUNICIPIO DE MONTEIRO LOBATO|SP": ("3531704", "Monteiro Lobato"),
    "MUNICIPIO DE MONTEIROPOLIS|AL": ("2705408", "Monteirópolis"),
    "MUNICIPIO DE MONTEIRO|PB": ("2509701", "Monteiro"),
    "MUNICIPIO DE MONTENEGRO|RS": ("4312401", "Montenegro"),
    "MUNICIPIO DE MONTES ALTOS|MA": ("2107001", "Montes Altos"),
    "MUNICIPIO DE MONTES CLAROS DE GOIAS|GO": ("5213707", "Montes Claros de Goiás"),
    "MUNICIPIO DE MONTES CLAROS|MG": ("3143302", "Montes Claros"),
    "MUNICIPIO DE MONTEZUMA|MG": ("3143450", "Montezuma"),
    "MUNICIPIO DE MONTIVIDIU DO NORTE|GO": ("5213772", "Montividiu do Norte"),
    "MUNICIPIO DE MONTIVIDIU|GO": ("5213756", "Montividiu"),
    "MUNICIPIO DE MORADA NOVA DE MINAS|MG": ("3143500", "Morada Nova de Minas"),
    "MUNICIPIO DE MORADA NOVA|CE": ("2308708", "Morada Nova"),
    "MUNICIPIO DE MORAUJO|CE": ("2308807", "Moraújo"),
    "MUNICIPIO DE MOREILANDIA|PE": ("2614303", "Moreilândia"),
    "MUNICIPIO DE MOREIRA SALES|PR": ("4116109", "Moreira Sales"),
    "MUNICIPIO DE MORENO|PE": ("2609402", "Moreno"),
    "MUNICIPIO DE MORMACO|RS": ("4312427", "Mormaço"),
    "MUNICIPIO DE MORPARA|BA": ("2921609", "Morpará"),
    "MUNICIPIO DE MORRETES|PR": ("4116208", "Morretes"),
    "MUNICIPIO DE MORRINHOS DO SUL|RS": ("4312443", "Morrinhos do Sul"),
    "MUNICIPIO DE MORRINHOS|CE": ("2308906", "Morrinhos"),
    "MUNICIPIO DE MORRINHOS|GO": ("5213806", "Morrinhos"),
    "MUNICIPIO DE MORRO AGUDO DE GOIAS|GO": ("5213855", "Morro Agudo de Goiás"),
    "MUNICIPIO DE MORRO AGUDO|SP": ("3531902", "Morro Agudo"),
    "MUNICIPIO DE MORRO CABECA NO TEMPO|PI": ("2206654", "Morro Cabeça no Tempo"),
    "MUNICIPIO DE MORRO DA FUMACA|SC": ("4211207", "Morro da Fumaça"),
    "MUNICIPIO DE MORRO DO CHAPEU DO PIAUI|PI": ("2206670", "Morro do Chapéu do Piauí"),
    "MUNICIPIO DE MORRO DO PILAR|MG": ("3143708", "Morro do Pilar"),
    "MUNICIPIO DE MORRO REDONDO|RS": ("4312450", "Morro Redondo"),
    "MUNICIPIO DE MORRO REUTER|RS": ("4312476", "Morro Reuter"),
    "MUNICIPIO DE MORUNGABA|SP": ("3532009", "Morungaba"),
    "MUNICIPIO DE MOSSAMEDES|GO": ("5213905", "Mossâmedes"),
    "MUNICIPIO DE MOSSORO|RN": ("2408003", "Mossoró"),
    "MUNICIPIO DE MOSTARDAS|RS": ("4312500", "Mostardas"),
    "MUNICIPIO DE MOTUCA|SP": ("3532058", "Motuca"),
    "MUNICIPIO DE MOZARLANDIA|GO": ("5214002", "Mozarlândia"),
    "MUNICIPIO DE MUANA|PA": ("1504901", "Muaná"),
    "MUNICIPIO DE MUCAJAI|RR": ("1400308", "Mucajaí"),
    "MUNICIPIO DE MUCUGE|BA": ("2921906", "Mucugê"),
    "MUNICIPIO DE MUCUM|RS": ("4312609", "Muçum"),
    "MUNICIPIO DE MUCURICI|ES": ("3203601", "Mucurici"),
    "MUNICIPIO DE MUITOS CAPOES|RS": ("4312617", "Muitos Capões"),
    "MUNICIPIO DE MULITERNO|RS": ("4312625", "Muliterno"),
    "MUNICIPIO DE MULUNGU PREFEITURA MUNICIPAL|CE": ("2309102", "Mulungu"),
    "MUNICIPIO DE MULUNGU PREFEITURA MUNICIPAL|PB": ("2509800", "Mulungu"),
    "MUNICIPIO DE MUNDO NOVO|BA": ("2922102", "Mundo Novo"),
    "MUNICIPIO DE MUNDO NOVO|GO": ("5214051", "Mundo Novo"),
    "MUNICIPIO DE MUNDO NOVO|MS": ("5005681", "Mundo Novo"),
    "MUNICIPIO DE MUNHOZ DE MELLO|PR": ("4116307", "Munhoz de Melo"),
    "MUNICIPIO DE MUNHOZ|MG": ("3143807", "Munhoz"),
    "MUNICIPIO DE MUNIZ FERREIRA|BA": ("2922201", "Muniz Ferreira"),
    "MUNICIPIO DE MUNIZ FREIRE|ES": ("3203700", "Muniz Freire"),
    "MUNICIPIO DE MUQUEM DO SAO FRANCISCO|BA": ("2922250", "Muquém do São Francisco"),
    "MUNICIPIO DE MUQUI|ES": ("3203809", "Muqui"),
    "MUNICIPIO DE MURIAE|MG": ("3143906", "Muriaé"),
    "MUNICIPIO DE MURIBECA|SE": ("2804300", "Muribeca"),
    "MUNICIPIO DE MURICI DOS PORTELAS|PI": ("2206696", "Murici dos Portelas"),
    "MUNICIPIO DE MURICI|AL": ("2705507", "Murici"),
    "MUNICIPIO DE MURUTINGA DO SUL|SP": ("3532108", "Murutinga do Sul"),
    "MUNICIPIO DE MUTUIPE|BA": ("2922409", "Mutuípe"),
    "MUNICIPIO DE MUTUM|MG": ("3144003", "Mutum"),
    "MUNICIPIO DE MUZAMBINHO|MG": ("3144102", "Muzambinho"),
    "MUNICIPIO DE NACIP RAYDAN|MG": ("3144201", "Nacip Raydan"),
    "MUNICIPIO DE NANTES|SP": ("3532157", "Nantes"),
    "MUNICIPIO DE NANUQUE|MG": ("3144300", "Nanuque"),
    "MUNICIPIO DE NAO-ME-TOQUE|RS": ("4312658", "Não-Me-Toque"),
    "MUNICIPIO DE NAQUE|MG": ("3144359", "Naque"),
    "MUNICIPIO DE NARANDIBA|SP": ("3532207", "Narandiba"),
    "MUNICIPIO DE NATALANDIA|MG": ("3144375", "Natalândia"),
    "MUNICIPIO DE NATAL|RN": ("2408102", "Natal"),
    "MUNICIPIO DE NATERCIA|MG": ("3144409", "Natércia"),
    "MUNICIPIO DE NATIVIDADE DA SERRA|SP": ("3532306", "Natividade da Serra"),
    "MUNICIPIO DE NATIVIDADE|RJ": ("3303104", "Natividade"),
    "MUNICIPIO DE NATIVIDADE|TO": ("1714203", "Natividade"),
    "MUNICIPIO DE NAVIRAI|MS": ("5005707", "Naviraí"),
    "MUNICIPIO DE NAZARE DA MATA|PE": ("2609501", "Nazaré da Mata"),
    "MUNICIPIO DE NAZARE PAULISTA|SP": ("3532405", "Nazaré Paulista"),
    "MUNICIPIO DE NAZARENO|MG": ("3144508", "Nazareno"),
    "MUNICIPIO DE NAZAREZINHO|PB": ("2510006", "Nazarezinho"),
    "MUNICIPIO DE NAZARE|BA": ("2922508", "Nazaré"),
    "MUNICIPIO DE NAZARE|TO": ("1714302", "Nazaré"),
    "MUNICIPIO DE NAZARIO|GO": ("5214408", "Nazário"),
    "MUNICIPIO DE NEOPOLIS|SE": ("2804409", "Neópolis"),
    "MUNICIPIO DE NEPOMUCENO|MG": ("3144607", "Nepomuceno"),
    "MUNICIPIO DE NHAMUNDA|AM": ("1303007", "Nhamundá"),
    "MUNICIPIO DE NICOLAU VERGUEIRO|RS": ("4312674", "Nicolau Vergueiro"),
    "MUNICIPIO DE NILO PECANHA|BA": ("2922607", "Nilo Peçanha"),
    "MUNICIPIO DE NILOPOLIS|RJ": ("3303203", "Nilópolis"),
    "MUNICIPIO DE NINHEIRA|MG": ("3144656", "Ninheira"),
    "MUNICIPIO DE NIOAQUE|MS": ("5005806", "Nioaque"),
    "MUNICIPIO DE NISIA FLORESTA|RN": ("2408201", "Nísia Floresta"),
    "MUNICIPIO DE NITEROI|RJ": ("3303302", "Niterói"),
    "MUNICIPIO DE NORDESTINA|BA": ("2922656", "Nordestina"),
    "MUNICIPIO DE NORMANDIA|RR": ("1400407", "Normandia"),
    "MUNICIPIO DE NORTELANDIA|MT": ("5106000", "Nortelândia"),
    "MUNICIPIO DE NOSSA SENHORA APARECIDA|SE": ("2804458", "Nossa Senhora Aparecida"),
    "MUNICIPIO DE NOSSA SENHORA DA GLORIA|SE": ("2804508", "Nossa Senhora da Glória"),
    "MUNICIPIO DE NOSSA SENHORA DAS DORES|SE": ("2804607", "Nossa Senhora das Dores"),
    "MUNICIPIO DE NOSSA SENHORA DAS GRACAS|PR": ("4116406", "Nossa Senhora das Graças"),
    "MUNICIPIO DE NOSSA SENHORA DE NAZARE|PI": ("2206753", "Nossa Senhora de Nazaré"),
    "MUNICIPIO DE NOSSA SENHORA DO LIVRAMENTO|MT": ("5106109", "Nossa Senhora do Livramento"),
    "MUNICIPIO DE NOSSA SENHORA DO SOCORRO|SE": ("2804805", "Nossa Senhora do Socorro"),
    "MUNICIPIO DE NOSSA SENHORA DOS REMEDIOS|PI": ("2206803", "Nossa Senhora dos Remédios"),
    "MUNICIPIO DE NOVA ALIANCA|SP": ("3532801", "Nova Aliança"),
    "MUNICIPIO DE NOVA ALVORADA DO SUL|MS": ("5006002", "Nova Alvorada do Sul"),
    "MUNICIPIO DE NOVA AMERICA|GO": ("5214705", "Nova América"),
    "MUNICIPIO DE NOVA ANDRADINA|MS": ("5006200", "Nova Andradina"),
    "MUNICIPIO DE NOVA ARACA|RS": ("4312807", "Nova Araçá"),
    "MUNICIPIO DE NOVA AURORA|GO": ("5214804", "Nova Aurora"),
    "MUNICIPIO DE NOVA AURORA|PR": ("4116703", "Nova Aurora"),
    "MUNICIPIO DE NOVA BANDEIRANTES|MT": ("5106158", "Nova Bandeirantes"),
    "MUNICIPIO DE NOVA BASSANO|RS": ("4312906", "Nova Bassano"),
    "MUNICIPIO DE NOVA BELEM|MG": ("3144672", "Nova Belém"),
    "MUNICIPIO DE NOVA BOA VISTA|RS": ("4312955", "Nova Boa Vista"),
    "MUNICIPIO DE NOVA BRASILANDIA D:OESTE|RO": ("1100148", "Nova Brasilândia D'Oeste"),
    "MUNICIPIO DE NOVA BRASILANDIA|MT": ("5106208", "Nova Brasilândia"),
    "MUNICIPIO DE NOVA BRESCIA|RS": ("4313003", "Nova Bréscia"),
    "MUNICIPIO DE NOVA CAMPINA|SP": ("3532827", "Nova Campina"),
    "MUNICIPIO DE NOVA CANAA DO NORTE|MT": ("5106216", "Nova Canaã do Norte"),
    "MUNICIPIO DE NOVA CANDELARIA|RS": ("4313011", "Nova Candelária"),
    "MUNICIPIO DE NOVA CANTU|PR": ("4116802", "Nova Cantu"),
    "MUNICIPIO DE NOVA COLINAS|MA": ("2107258", "Nova Colinas"),
    "MUNICIPIO DE NOVA CRIXAS|GO": ("5214838", "Nova Crixás"),
    "MUNICIPIO DE NOVA CRUZ|RN": ("2408300", "Nova Cruz"),
    "MUNICIPIO DE NOVA ERA|MG": ("3144706", "Nova Era"),
    "MUNICIPIO DE NOVA ERECHIM|SC": ("4211405", "Nova Erechim"),
    "MUNICIPIO DE NOVA ESPERANCA DO PIRIA|PA": ("1504950", "Nova Esperança do Piriá"),
    "MUNICIPIO DE NOVA ESPERANCA DO SUDOESTE|PR": ("4116950", "Nova Esperança do Sudoeste"),
    "MUNICIPIO DE NOVA ESPERANCA|PR": ("4116901", "Nova Esperança"),
    "MUNICIPIO DE NOVA EUROPA|SP": ("3532900", "Nova Europa"),
    "MUNICIPIO DE NOVA FATIMA|BA": ("2922730", "Nova Fátima"),
    "MUNICIPIO DE NOVA FATIMA|PR": ("4117008", "Nova Fátima"),
    "MUNICIPIO DE NOVA FLORESTA|PB": ("2510105", "Nova Floresta"),
    "MUNICIPIO DE NOVA FRIBURGO|RJ": ("3303401", "Nova Friburgo"),
    "MUNICIPIO DE NOVA GLORIA|GO": ("5214861", "Nova Glória"),
    "MUNICIPIO DE NOVA GRANADA|SP": ("3533007", "Nova Granada"),
    "MUNICIPIO DE NOVA HARTZ|RS": ("4313060", "Nova Hartz"),
    "MUNICIPIO DE NOVA IGUACU DE GOIAS|GO": ("5214879", "Nova Iguaçu de Goiás"),
    "MUNICIPIO DE NOVA IGUACU|RJ": ("3303500", "Nova Iguaçu"),
    "MUNICIPIO DE NOVA IORQUE|MA": ("2107308", "Nova Iorque"),
    "MUNICIPIO DE NOVA IPIXUNA|PA": ("1504976", "Nova Ipixuna"),
    "MUNICIPIO DE NOVA ITABERABA|SC": ("4211454", "Nova Itaberaba"),
    "MUNICIPIO DE NOVA ITARANA|BA": ("2922805", "Nova Itarana"),
    "MUNICIPIO DE NOVA LARANJEIRAS|PR": ("4117057", "Nova Laranjeiras"),
    "MUNICIPIO DE NOVA LIMA|MG": ("3144805", "Nova Lima"),
    "MUNICIPIO DE NOVA LONDRINA|PR": ("4117107", "Nova Londrina"),
    "MUNICIPIO DE NOVA MAMORE|RO": ("1100338", "Nova Mamoré"),
    "MUNICIPIO DE NOVA MODICA|MG": ("3144904", "Nova Módica"),
    "MUNICIPIO DE NOVA MONTE VERDE|MT": ("5108956", "Nova Monte Verde"),
    "MUNICIPIO DE NOVA NAZARE|MT": ("5106174", "Nova Nazaré"),
    "MUNICIPIO DE NOVA ODESSA|SP": ("3533403", "Nova Odessa"),
    "MUNICIPIO DE NOVA OLIMPIA|MT": ("5106232", "Nova Olímpia"),
    "MUNICIPIO DE NOVA OLIMPIA|PR": ("4117206", "Nova Olímpia"),
    "MUNICIPIO DE NOVA OLINDA DO MARANHAO|MA": ("2107357", "Nova Olinda do Maranhão"),
    "MUNICIPIO DE NOVA OLINDA DO NORTE|AM": ("1303106", "Nova Olinda do Norte"),
    "MUNICIPIO DE NOVA OLINDA|CE": ("2309201", "Nova Olinda"),
    "MUNICIPIO DE NOVA OLINDA|PB": ("2510204", "Nova Olinda"),
    "MUNICIPIO DE NOVA OLINDA|TO": ("1714880", "Nova Olinda"),
    "MUNICIPIO DE NOVA PADUA|RS": ("4313086", "Nova Pádua"),
    "MUNICIPIO DE NOVA PALMEIRA|PB": ("2510303", "Nova Palmeira"),
    "MUNICIPIO DE NOVA PETROPOLIS|RS": ("4313201", "Nova Petrópolis"),
    "MUNICIPIO DE NOVA PONTE|MG": ("3145000", "Nova Ponte"),
    "MUNICIPIO DE NOVA PRATA DO IGUACU|PR": ("4117255", "Nova Prata do Iguaçu"),
    "MUNICIPIO DE NOVA PRATA|RS": ("4313300", "Nova Prata"),
    "MUNICIPIO DE NOVA RAMADA|RS": ("4313334", "Nova Ramada"),
    "MUNICIPIO DE NOVA RESENDE|MG": ("3145109", "Nova Resende"),
    "MUNICIPIO DE NOVA ROMA DO SUL|RS": ("4313359", "Nova Roma do Sul"),
    "MUNICIPIO DE NOVA ROSALANDIA|TO": ("1715002", "Nova Rosalândia"),
    "MUNICIPIO DE NOVA RUSSAS|CE": ("2309300", "Nova Russas"),
    "MUNICIPIO DE NOVA SANTA BARBARA|PR": ("4117214", "Nova Santa Bárbara"),
    "MUNICIPIO DE NOVA SANTA RITA|PI": ("2207959", "Nova Santa Rita"),
    "MUNICIPIO DE NOVA SANTA RITA|RS": ("4313375", "Nova Santa Rita"),
    "MUNICIPIO DE NOVA SANTA ROSA|PR": ("4117222", "Nova Santa Rosa"),
    "MUNICIPIO DE NOVA SERRANA|MG": ("3145208", "Nova Serrana"),
    "MUNICIPIO DE NOVA SOURE|BA": ("2922904", "Nova Soure"),
    "MUNICIPIO DE NOVA TIMBOTEUA|PA": ("1505007", "Nova Timboteua"),
    "MUNICIPIO DE NOVA TRENTO|SC": ("4211504", "Nova Trento"),
    "MUNICIPIO DE NOVA UBIRATA|MT": ("5106240", "Nova Ubiratã"),
    "MUNICIPIO DE NOVA UNIAO|MG": ("3136603", "Nova União"),
    "MUNICIPIO DE NOVA UNIAO|RO": ("1101435", "Nova União"),
    "MUNICIPIO DE NOVA VENECIA|ES": ("3203908", "Nova Venécia"),
    "MUNICIPIO DE NOVA VENEZA|GO": ("5215009", "Nova Veneza"),
    "MUNICIPIO DE NOVA VENEZA|SC": ("4211603", "Nova Veneza"),
    "MUNICIPIO DE NOVA XAVANTINA|MT": ("5106257", "Nova Xavantina"),
    "MUNICIPIO DE NOVAIS|SP": ("3533254", "Novais"),
    "MUNICIPIO DE NOVO ACORDO|TO": ("1715101", "Novo Acordo"),
    "MUNICIPIO DE NOVO AIRAO|AM": ("1303205", "Novo Airão"),
    "MUNICIPIO DE NOVO ALEGRE|TO": ("1715150", "Novo Alegre"),
    "MUNICIPIO DE NOVO ARIPUANA|AM": ("1303304", "Novo Aripuanã"),
    "MUNICIPIO DE NOVO BARREIRO|RS": ("4313490", "Novo Barreiro"),
    "MUNICIPIO DE NOVO BRASIL|GO": ("5215207", "Novo Brasil"),
    "MUNICIPIO DE NOVO CABRAIS|RS": ("4313391", "Novo Cabrais"),
    "MUNICIPIO DE NOVO CRUZEIRO|MG": ("3145307", "Novo Cruzeiro"),
    "MUNICIPIO DE NOVO GAMA|GO": ("5215231", "Novo Gama"),
    "MUNICIPIO DE NOVO HAMBURGO|RS": ("4313409", "Novo Hamburgo"),
    "MUNICIPIO DE NOVO HORIZONTE DO NORTE|MT": ("5106273", "Novo Horizonte do Norte"),
    "MUNICIPIO DE NOVO HORIZONTE DO OESTE|RO": ("1100502", "Novo Horizonte do Oeste"),
    "MUNICIPIO DE NOVO HORIZONTE DO SUL|MS": ("5006259", "Novo Horizonte do Sul"),
    "MUNICIPIO DE NOVO HORIZONTE|BA": ("2923035", "Novo Horizonte"),
    "MUNICIPIO DE NOVO HORIZONTE|SC": ("4211652", "Novo Horizonte"),
    "MUNICIPIO DE NOVO HORIZONTE|SP": ("3533502", "Novo Horizonte"),
    "MUNICIPIO DE NOVO ITACOLOMI|PR": ("4117297", "Novo Itacolomi"),
    "MUNICIPIO DE NOVO LINO|AL": ("2705606", "Novo Lino"),
    "MUNICIPIO DE NOVO MUNDO|MT": ("5106265", "Novo Mundo"),
    "MUNICIPIO DE NOVO ORIENTE DE MINAS|MG": ("3145356", "Novo Oriente de Minas"),
    "MUNICIPIO DE NOVO ORIENTE DO PIAUI|PI": ("2206902", "Novo Oriente do Piauí"),
    "MUNICIPIO DE NOVO ORIENTE|CE": ("2309409", "Novo Oriente"),
    "MUNICIPIO DE NOVO PLANALTO|GO": ("5215256", "Novo Planalto"),
    "MUNICIPIO DE NOVO PROGRESSO|PA": ("1505031", "Novo Progresso"),
    "MUNICIPIO DE NOVO REPARTIMENTO|PA": ("1505064", "Novo Repartimento"),
    "MUNICIPIO DE NOVO SANTO ANTONIO|MT": ("5106315", "Novo Santo Antônio"),
    "MUNICIPIO DE NOVO SANTO ANTONIO|PI": ("2206951", "Novo Santo Antônio"),
    "MUNICIPIO DE NOVO TIRADENTES|RS": ("4313441", "Novo Tiradentes"),
    "MUNICIPIO DE NOVO TRIUNFO|BA": ("2923050", "Novo Triunfo"),
    "MUNICIPIO DE NOVO XINGU|RS": ("4313466", "Novo Xingu"),
    "MUNICIPIO DE NOVORIZONTE|MG": ("3145372", "Novorizonte"),
    "MUNICIPIO DE OBIDOS|PA": ("1505106", "Óbidos"),
    "MUNICIPIO DE OCARA|CE": ("2309458", "Ocara"),
    "MUNICIPIO DE OCAUCU|SP": ("3533700", "Ocauçu"),
    "MUNICIPIO DE OEIRAS DO PARA|PA": ("1505205", "Oeiras do Pará"),
    "MUNICIPIO DE OEIRAS|PI": ("2207009", "Oeiras"),
    "MUNICIPIO DE OIAPOQUE|AP": ("1600501", "Oiapoque"),
    "MUNICIPIO DE OLARIA|MG": ("3145406", "Olaria"),
    "MUNICIPIO DE OLHO D'AGUA DAS CUNHAS|MA": ("2107407", "Olho d'Água das Cunhãs"),
    "MUNICIPIO DE OLHO D'AGUA DO BORGES|RN": ("2408409", "Olho d'Água do Borges"),
    "MUNICIPIO DE OLHO D'AGUA GRANDE|AL": ("2705903", "Olho d'Água Grande"),
    "MUNICIPIO DE OLHO D'AGUA|PB": ("2510402", "Olho d'Água"),
    "MUNICIPIO DE OLHO D:AGUA DO CASADO|AL": ("2705804", "Olho d'Água do Casado"),
    "MUNICIPIO DE OLHOS-D:AGUA|MG": ("3145455", "Olhos-d'Água"),
    "MUNICIPIO DE OLIMPIO NORONHA|MG": ("3145505", "Olímpio Noronha"),
    "MUNICIPIO DE OLINDA|PE": ("2609600", "Olinda"),
    "MUNICIPIO DE OLINDINA|BA": ("2923100", "Olindina"),
    "MUNICIPIO DE OLIVEDOS|PB": ("2510501", "Olivedos"),
    "MUNICIPIO DE OLIVEIRA DOS BREJINHOS|BA": ("2923209", "Oliveira dos Brejinhos"),
    "MUNICIPIO DE OLIVEIRA FORTES|MG": ("3145703", "Oliveira Fortes"),
    "MUNICIPIO DE OLIVEIRA|MG": ("3145604", "Oliveira"),
    "MUNICIPIO DE OLIVENCA|AL": ("2706000", "Olivença"),
    "MUNICIPIO DE ONCA DE PITANGUI|MG": ("3145802", "Onça de Pitangui"),
    "MUNICIPIO DE ORATORIOS|MG": ("3145851", "Oratórios"),
    "MUNICIPIO DE ORINDIUVA|SP": ("3534203", "Orindiúva"),
    "MUNICIPIO DE ORIXIMINA|PA": ("1505304", "Oriximiná"),
    "MUNICIPIO DE ORIZANIA|MG": ("3145877", "Orizânia"),
    "MUNICIPIO DE ORIZONA|GO": ("5215306", "Orizona"),
    "MUNICIPIO DE ORLANDIA|SP": ("3534302", "Orlândia"),
    "MUNICIPIO DE ORLEANS|SC": ("4211702", "Orleans"),
    "MUNICIPIO DE OROBO|PE": ("2609709", "Orobó"),
    "MUNICIPIO DE OROCO|PE": ("2609808", "Orocó"),
    "MUNICIPIO DE OSASCO|SP": ("3534401", "Osasco"),
    "MUNICIPIO DE OSORIO|RS": ("4313508", "Osório"),
    "MUNICIPIO DE OSVALDO CRUZ|SP": ("3534609", "Osvaldo Cruz"),
    "MUNICIPIO DE OTACILIO COSTA|SC": ("4211751", "Otacílio Costa"),
    "MUNICIPIO DE OUREM|PA": ("1505403", "Ourém"),
    "MUNICIPIO DE OURICURI|PE": ("2609907", "Ouricuri"),
    "MUNICIPIO DE OURILANDIA DO NORTE|PA": ("1505437", "Ourilândia do Norte"),
    "MUNICIPIO DE OURINHOS|SP": ("3534708", "Ourinhos"),
    "MUNICIPIO DE OURIZONA|PR": ("4117404", "Ourizona"),
    "MUNICIPIO DE OURO BRANCO|AL": ("2706109", "Ouro Branco"),
    "MUNICIPIO DE OURO BRANCO|MG": ("3145901", "Ouro Branco"),
    "MUNICIPIO DE OURO BRANCO|RN": ("2408508", "Ouro Branco"),
    "MUNICIPIO DE OURO FINO|MG": ("3146008", "Ouro Fino"),
    "MUNICIPIO DE OURO PRETO DO OESTE|RO": ("1100155", "Ouro Preto do Oeste"),
    "MUNICIPIO DE OURO PRETO|MG": ("3146107", "Ouro Preto"),
    "MUNICIPIO DE OURO VELHO|PB": ("2510600", "Ouro Velho"),
    "MUNICIPIO DE OURO VERDE DE GOIAS|GO": ("5215405", "Ouro Verde de Goiás"),
    "MUNICIPIO DE OURO VERDE DE MINAS|MG": ("3146206", "Ouro Verde de Minas"),
    "MUNICIPIO DE OURO VERDE DO OESTE|PR": ("4117453", "Ouro Verde do Oeste"),
    "MUNICIPIO DE OURO VERDE|SC": ("4211850", "Ouro Verde"),
    "MUNICIPIO DE OURO VERDE|SP": ("3534807", "Ouro Verde"),
    "MUNICIPIO DE OUROESTE|SP": ("3534757", "Ouroeste"),
    "MUNICIPIO DE OURO|SC": ("4211801", "Ouro"),
    "MUNICIPIO DE PACAEMBU|SP": ("3534906", "Pacaembu"),
    "MUNICIPIO DE PACAJA|PA": ("1505486", "Pacajá"),
    "MUNICIPIO DE PACAJUS|CE": ("2309607", "Pacajus"),
    "MUNICIPIO DE PACARAIMA|RR": ("1400456", "Pacaraima"),
    "MUNICIPIO DE PACATUBA|CE": ("2309706", "Pacatuba"),
    "MUNICIPIO DE PACATUBA|SE": ("2804904", "Pacatuba"),
    "MUNICIPIO DE PACO DO LUMIAR|MA": ("2107506", "Paço do Lumiar"),
    "MUNICIPIO DE PACUJA|CE": ("2309904", "Pacujá"),
    "MUNICIPIO DE PADRE BERNARDO|GO": ("5215603", "Padre Bernardo"),
    "MUNICIPIO DE PADRE CARVALHO|MG": ("3146255", "Padre Carvalho"),
    "MUNICIPIO DE PADRE PARAISO|MG": ("3146305", "Padre Paraíso"),
    "MUNICIPIO DE PAES LANDIM|PI": ("2207306", "Paes Landim"),
    "MUNICIPIO DE PAIAL|SC": ("4211876", "Paial"),
    "MUNICIPIO DE PAICANDU|PR": ("4117503", "Paiçandu"),
    "MUNICIPIO DE PAIM FILHO|RS": ("4313607", "Paim Filho"),
    "MUNICIPIO DE PAINEIRAS|MG": ("3146404", "Paineiras"),
    "MUNICIPIO DE PAINEL|SC": ("4211892", "Painel"),
    "MUNICIPIO DE PAIVA|MG": ("3146602", "Paiva"),
    "MUNICIPIO DE PAJEU DO PIAUI|PI": ("2207355", "Pajeú do Piauí"),
    "MUNICIPIO DE PALESTINA DO PARA|PA": ("1505494", "Palestina do Pará"),
    "MUNICIPIO DE PALESTINA|AL": ("2706208", "Palestina"),
    "MUNICIPIO DE PALESTINA|SP": ("3535002", "Palestina"),
    "MUNICIPIO DE PALHOCA|SC": ("4211900", "Palhoça"),
    "MUNICIPIO DE PALMA SOLA|SC": ("4212007", "Palma Sola"),
    "MUNICIPIO DE PALMARES DO SUL|RS": ("4313656", "Palmares do Sul"),
    "MUNICIPIO DE PALMAS DE MONTE ALTO|BA": ("2923407", "Palmas de Monte Alto"),
    "MUNICIPIO DE PALMAS|PR": ("4117602", "Palmas"),
    "MUNICIPIO DE PALMAS|TO": ("1721000", "Palmas"),
    "MUNICIPIO DE PALMA|MG": ("3146701", "Palma"),
    "MUNICIPIO DE PALMEIRA D'OESTE|SP": ("3535200", "Palmeira d'Oeste"),
    "MUNICIPIO DE PALMEIRA DAS MISSOES|RS": ("4313706", "Palmeira das Missões"),
    "MUNICIPIO DE PALMEIRA DO PIAUI|PI": ("2207405", "Palmeira do Piauí"),
    "MUNICIPIO DE PALMEIRA DOS INDIOS|AL": ("2706307", "Palmeira dos Índios"),
    "MUNICIPIO DE PALMEIRAIS|PI": ("2207504", "Palmeirais"),
    "MUNICIPIO DE PALMEIRANTE|TO": ("1715705", "Palmeirante"),
    "MUNICIPIO DE PALMEIRAS DE GOIAS|GO": ("5215702", "Palmeiras de Goiás"),
    "MUNICIPIO DE PALMEIRAS DO TOCANTINS|TO": ("1713809", "Palmeiras do Tocantins"),
    "MUNICIPIO DE PALMEIRAS|BA": ("2923506", "Palmeiras"),
    "MUNICIPIO DE PALMEIRA|PR": ("4117701", "Palmeira"),
    "MUNICIPIO DE PALMEIRA|SC": ("4212056", "Palmeira"),
    "MUNICIPIO DE PALMEIROPOLIS|TO": ("1715754", "Palmeirópolis"),
    "MUNICIPIO DE PALMELO|GO": ("5215801", "Palmelo"),
    "MUNICIPIO DE PALMINOPOLIS|GO": ("5215900", "Palminópolis"),
    "MUNICIPIO DE PALMITAL|PR": ("4117800", "Palmital"),
    "MUNICIPIO DE PALMITAL|SP": ("3535309", "Palmital"),
    "MUNICIPIO DE PALMITOS|SC": ("4212106", "Palmitos"),
    "MUNICIPIO DE PALMOPOLIS|MG": ("3146750", "Palmópolis"),
    "MUNICIPIO DE PALOTINA|PR": ("4117909", "Palotina"),
    "MUNICIPIO DE PANAMA|GO": ("5216007", "Panamá"),
    "MUNICIPIO DE PANAMBI|RS": ("4313904", "Panambi"),
    "MUNICIPIO DE PANCAS|ES": ("3204005", "Pancas"),
    "MUNICIPIO DE PANELAS|PE": ("2610202", "Panelas"),
    "MUNICIPIO DE PANTANO GRANDE|RS": ("4313953", "Pantano Grande"),
    "MUNICIPIO DE PAPAGAIOS|MG": ("3146909", "Papagaios"),
    "MUNICIPIO DE PAPANDUVA|SC": ("4212205", "Papanduva"),
    "MUNICIPIO DE PAQUETA|PI": ("2207553", "Paquetá"),
    "MUNICIPIO DE PARA DE MINAS|MG": ("3147105", "Pará de Minas"),
    "MUNICIPIO DE PARACAMBI|RJ": ("3303609", "Paracambi"),
    "MUNICIPIO DE PARACATU|MG": ("3147006", "Paracatu"),
    "MUNICIPIO DE PARACURU|CE": ("2310209", "Paracuru"),
    "MUNICIPIO DE PARAGOMINAS|PA": ("1505502", "Paragominas"),
    "MUNICIPIO DE PARAGUACU PAULISTA|SP": ("3535507", "Paraguaçu Paulista"),
    "MUNICIPIO DE PARAGUACU|MG": ("3147204", "Paraguaçu"),
    "MUNICIPIO DE PARAIBA DO SUL|RJ": ("3303708", "Paraíba do Sul"),
    "MUNICIPIO DE PARAIBANO|MA": ("2107704", "Paraibano"),
    "MUNICIPIO DE PARAIBUNA|SP": ("3535606", "Paraibuna"),
    "MUNICIPIO DE PARAIPABA|CE": ("2310258", "Paraipaba"),
    "MUNICIPIO DE PARAISO DAS AGUAS|MS": ("5006275", "Paraíso das Águas"),
    "MUNICIPIO DE PARAISO DO NORTE|PR": ("4118006", "Paraíso do Norte"),
    "MUNICIPIO DE PARAISO DO SUL|RS": ("4314027", "Paraíso do Sul"),
    "MUNICIPIO DE PARAISO DO TOCANTINS|TO": ("1716109", "Paraíso do Tocantins"),
    "MUNICIPIO DE PARAISOPOLIS|MG": ("3147303", "Paraisópolis"),
    "MUNICIPIO DE PARAISO|SC": ("4212239", "Paraíso"),
    "MUNICIPIO DE PARAISO|SP": ("3535705", "Paraíso"),
    "MUNICIPIO DE PARAI|RS": ("4314001", "Paraí"),
    "MUNICIPIO DE PARAMBU|CE": ("2310308", "Parambu"),
    "MUNICIPIO DE PARAMOTI|CE": ("2310407", "Paramoti"),
    "MUNICIPIO DE PARANACITY|PR": ("4118105", "Paranacity"),
    "MUNICIPIO DE PARANAGUA|PR": ("4118204", "Paranaguá"),
    "MUNICIPIO DE PARANAIBA|MS": ("5006309", "Paranaíba"),
    "MUNICIPIO DE PARANAIGUARA|GO": ("5216304", "Paranaiguara"),
    "MUNICIPIO DE PARANAITA|MT": ("5106299", "Paranaíta"),
    "MUNICIPIO DE PARANATAMA|PE": ("2610301", "Paranatama"),
    "MUNICIPIO DE PARANATINGA|MT": ("5106307", "Paranatinga"),
    "MUNICIPIO DE PARANAVAI|PR": ("4118402", "Paranavaí"),
    "MUNICIPIO DE PARANA|TO": ("1716208", "Paranã"),
    "MUNICIPIO DE PARANHOS|MS": ("5006358", "Paranhos"),
    "MUNICIPIO DE PARAOPEBA|MG": ("3147402", "Paraopeba"),
    "MUNICIPIO DE PARATINGA|BA": ("2923704", "Paratinga"),
    "MUNICIPIO DE PARAUNA|GO": ("5216403", "Paraúna"),
    "MUNICIPIO DE PARAU|RN": ("2408706", "Paraú"),
    "MUNICIPIO DE PARAZINHO|RN": ("2408805", "Parazinho"),
    "MUNICIPIO DE PARDINHO|SP": ("3536109", "Pardinho"),
    "MUNICIPIO DE PARECI NOVO|RS": ("4314035", "Pareci Novo"),
    "MUNICIPIO DE PARECIS|RO": ("1101450", "Parecis"),
    "MUNICIPIO DE PARELHAS|RN": ("2408904", "Parelhas"),
    "MUNICIPIO DE PARICONHA|AL": ("2706422", "Pariconha"),
    "MUNICIPIO DE PARINTINS|AM": ("1303403", "Parintins"),
    "MUNICIPIO DE PARIPIRANGA|BA": ("2923803", "Paripiranga"),
    "MUNICIPIO DE PARIPUEIRA|AL": ("2706448", "Paripueira"),
    "MUNICIPIO DE PARIQUERA-ACU|SP": ("3536208", "Pariquera-Açu"),
    "MUNICIPIO DE PARISI|SP": ("3536257", "Parisi"),
    "MUNICIPIO DE PARNAGUA|PI": ("2207603", "Parnaguá"),
    "MUNICIPIO DE PARNAIBA|PI": ("2207702", "Parnaíba"),
    "MUNICIPIO DE PARNAMIRIM|PE": ("2610400", "Parnamirim"),
    "MUNICIPIO DE PARNAMIRIM|RN": ("2403251", "Parnamirim"),
    "MUNICIPIO DE PAROBE|RS": ("4314050", "Parobé"),
    "MUNICIPIO DE PASSA E FICA|RN": ("2409100", "Passa e Fica"),
    "MUNICIPIO DE PASSA QUATRO|MG": ("3147600", "Passa Quatro"),
    "MUNICIPIO DE PASSA SETE|RS": ("4314068", "Passa Sete"),
    "MUNICIPIO DE PASSA TEMPO|MG": ("3147709", "Passa Tempo"),
    "MUNICIPIO DE PASSA VINTE|MG": ("3147808", "Passa Vinte"),
    "MUNICIPIO DE PASSAGEM FRANCA DO PIAUI|PI": ("2207751", "Passagem Franca do Piauí"),
    "MUNICIPIO DE PASSIRA|PE": ("2610509", "Passira"),
    "MUNICIPIO DE PASSO DE CAMARAGIBE|AL": ("2706505", "Passo de Camaragibe"),
    "MUNICIPIO DE PASSO DE TORRES|SC": ("4212254", "Passo de Torres"),
    "MUNICIPIO DE PASSO DO SOBRADO|RS": ("4314076", "Passo do Sobrado"),
    "MUNICIPIO DE PASSO FUNDO|RS": ("4314100", "Passo Fundo"),
    "MUNICIPIO DE PASSOS MAIA|SC": ("4212270", "Passos Maia"),
    "MUNICIPIO DE PASSOS|MG": ("3147907", "Passos"),
    "MUNICIPIO DE PASTOS BONS|MA": ("2108009", "Pastos Bons"),
    "MUNICIPIO DE PATIS|MG": ("3147956", "Patis"),
    "MUNICIPIO DE PATO BRANCO|PR": ("4118501", "Pato Branco"),
    "MUNICIPIO DE PATOS DE MINAS|MG": ("3148004", "Patos de Minas"),
    "MUNICIPIO DE PATOS DO PIAUI|PI": ("2207777", "Patos do Piauí"),
    "MUNICIPIO DE PATOS|PB": ("2510808", "Patos"),
    "MUNICIPIO DE PATROCINIO DO MURIAE|MG": ("3148202", "Patrocínio do Muriaé"),
    "MUNICIPIO DE PATROCINIO PAULISTA|SP": ("3536307", "Patrocínio Paulista"),
    "MUNICIPIO DE PATROCINIO|MG": ("3148103", "Patrocínio"),
    "MUNICIPIO DE PATU|RN": ("2409308", "Patu"),
    "MUNICIPIO DE PATY DO ALFERES|RJ": ("3303856", "Paty do Alferes"),
    "MUNICIPIO DE PAU BRASIL|BA": ("2923902", "Pau Brasil"),
    "MUNICIPIO DE PAU D'ARCO|PA": ("1505551", "Pau D'Arco"),
    "MUNICIPIO DE PAU D'ARCO|TO": ("1716307", "Pau D'Arco"),
    "MUNICIPIO DE PAU D:ARCO DO PIAUI|PI": ("2207793", "Pau D'Arco do Piauí"),
    "MUNICIPIO DE PAU D:ARCO|PA": ("1505551", "Pau D'Arco"),
    "MUNICIPIO DE PAU D:ARCO|TO": ("1716307", "Pau D'Arco"),
    "MUNICIPIO DE PAU DOS FERROS|RN": ("2409407", "Pau dos Ferros"),
    "MUNICIPIO DE PAUDALHO|PE": ("2610608", "Paudalho"),
    "MUNICIPIO DE PAUINI|AM": ("1303502", "Pauini"),
    "MUNICIPIO DE PAULA CANDIDO|MG": ("3148301", "Paula Cândido"),
    "MUNICIPIO DE PAULICEIA|SP": ("3536406", "Paulicéia"),
    "MUNICIPIO DE PAULINO NEVES|MA": ("2108058", "Paulino Neves"),
    "MUNICIPIO DE PAULISTANA|PI": ("2207801", "Paulistana"),
    "MUNICIPIO DE PAULISTAS|MG": ("3148400", "Paulistas"),
    "MUNICIPIO DE PAULISTA|PB": ("2510907", "Paulista"),
    "MUNICIPIO DE PAULISTA|PE": ("2610707", "Paulista"),
    "MUNICIPIO DE PAULO AFONSO|BA": ("2924009", "Paulo Afonso"),
    "MUNICIPIO DE PAULO BENTO|RS": ("4314134", "Paulo Bento"),
    "MUNICIPIO DE PAULO FRONTIN|PR": ("4118709", "Paulo Frontin"),
    "MUNICIPIO DE PAULO JACINTO|AL": ("2706604", "Paulo Jacinto"),
    "MUNICIPIO DE PAULO LOPES|SC": ("4212304", "Paulo Lopes"),
    "MUNICIPIO DE PAULO RAMOS|MA": ("2108108", "Paulo Ramos"),
    "MUNICIPIO DE PAVAO|MG": ("3148509", "Pavão"),
    "MUNICIPIO DE PAVERAMA|RS": ("4314159", "Paverama"),
    "MUNICIPIO DE PAVUSSU|PI": ("2207850", "Pavussu"),
    "MUNICIPIO DE PE DE SERRA|BA": ("2924058", "Pé de Serra"),
    "MUNICIPIO DE PEABIRU|PR": ("4118808", "Peabiru"),
    "MUNICIPIO DE PECANHA|MG": ("3148608", "Peçanha"),
    "MUNICIPIO DE PEDERNEIRAS|SP": ("3536703", "Pederneiras"),
    "MUNICIPIO DE PEDRA AZUL|MG": ("3148707", "Pedra Azul"),
    "MUNICIPIO DE PEDRA BELA|SP": ("3536802", "Pedra Bela"),
    "MUNICIPIO DE PEDRA BONITA|MG": ("3148756", "Pedra Bonita"),
    "MUNICIPIO DE PEDRA BRANCA DO AMAPARI|AP": ("1600154", "Pedra Branca do Amapari"),
    "MUNICIPIO DE PEDRA BRANCA|CE": ("2310506", "Pedra Branca"),
    "MUNICIPIO DE PEDRA BRANCA|PB": ("2511004", "Pedra Branca"),
    "MUNICIPIO DE PEDRA DO ANTA|MG": ("3148806", "Pedra do Anta"),
    "MUNICIPIO DE PEDRA DO INDAIA|MG": ("3148905", "Pedra do Indaiá"),
    "MUNICIPIO DE PEDRA DOURADA|MG": ("3149002", "Pedra Dourada"),
    "MUNICIPIO DE PEDRA GRANDE|RN": ("2409506", "Pedra Grande"),
    "MUNICIPIO DE PEDRA LAVRADA|PB": ("2511103", "Pedra Lavrada"),
    "MUNICIPIO DE PEDRA MOLE|SE": ("2805000", "Pedra Mole"),
    "MUNICIPIO DE PEDRA PRETA|MT": ("5106372", "Pedra Preta"),
    "MUNICIPIO DE PEDRA PRETA|RN": ("2409605", "Pedra Preta"),
    "MUNICIPIO DE PEDRALVA|MG": ("3149101", "Pedralva"),
    "MUNICIPIO DE PEDRANOPOLIS|SP": ("3536901", "Pedranópolis"),
    "MUNICIPIO DE PEDRAS ALTAS|RS": ("4314175", "Pedras Altas"),
    "MUNICIPIO DE PEDRAS DE MARIA DA CRUZ|MG": ("3149150", "Pedras de Maria da Cruz"),
    "MUNICIPIO DE PEDRAS GRANDES|SC": ("4212403", "Pedras Grandes"),
    "MUNICIPIO DE PEDRA|PE": ("2610806", "Pedra"),
    "MUNICIPIO DE PEDREIRA|SP": ("3537107", "Pedreira"),
    "MUNICIPIO DE PEDRINHAS PAULISTA|SP": ("3537156", "Pedrinhas Paulista"),
    "MUNICIPIO DE PEDRINHAS|SE": ("2805109", "Pedrinhas"),
    "MUNICIPIO DE PEDRINOPOLIS|MG": ("3149200", "Pedrinópolis"),
    "MUNICIPIO DE PEDRO AFONSO|TO": ("1716505", "Pedro Afonso"),
    "MUNICIPIO DE PEDRO ALEXANDRE|BA": ("2924207", "Pedro Alexandre"),
    "MUNICIPIO DE PEDRO AVELINO|RN": ("2409704", "Pedro Avelino"),
    "MUNICIPIO DE PEDRO CANARIO|ES": ("3204054", "Pedro Canário"),
    "MUNICIPIO DE PEDRO DE TOLEDO|SP": ("3537206", "Pedro de Toledo"),
    "MUNICIPIO DE PEDRO DO ROSARIO|MA": ("2108256", "Pedro do Rosário"),
    "MUNICIPIO DE PEDRO GOMES|MS": ("5006408", "Pedro Gomes"),
    "MUNICIPIO DE PEDRO LAURENTINO|PI": ("2207934", "Pedro Laurentino"),
    "MUNICIPIO DE PEDRO LEOPOLDO|MG": ("3149309", "Pedro Leopoldo"),
    "MUNICIPIO DE PEDRO OSORIO|RS": ("4314209", "Pedro Osório"),
    "MUNICIPIO DE PEDRO REGIS|PB": ("2512721", "Pedro Régis"),
    "MUNICIPIO DE PEDRO TEIXEIRA|MG": ("3149408", "Pedro Teixeira"),
    "MUNICIPIO DE PEIXE|TO": ("1716604", "Peixe"),
    "MUNICIPIO DE PEIXOTO DE AZEVEDO|MT": ("5106422", "Peixoto de Azevedo"),
    "MUNICIPIO DE PELOTAS|RS": ("4314407", "Pelotas"),
    "MUNICIPIO DE PENAFORTE|CE": ("2310605", "Penaforte"),
    "MUNICIPIO DE PENALVA|MA": ("2108306", "Penalva"),
    "MUNICIPIO DE PENAPOLIS|SP": ("3537305", "Penápolis"),
    "MUNICIPIO DE PENDENCIAS|RN": ("2409902", "Pendências"),
    "MUNICIPIO DE PENEDO|AL": ("2706703", "Penedo"),
    "MUNICIPIO DE PENHA|SC": ("4212502", "Penha"),
    "MUNICIPIO DE PENTECOSTE|CE": ("2310704", "Pentecoste"),
    "MUNICIPIO DE PEQUERI|MG": ("3149507", "Pequeri"),
    "MUNICIPIO DE PEQUIZEIRO|TO": ("1716653", "Pequizeiro"),
    "MUNICIPIO DE PEQUI|MG": ("3149606", "Pequi"),
    "MUNICIPIO DE PERDIGAO|MG": ("3149705", "Perdigão"),
    "MUNICIPIO DE PERDIZES|MG": ("3149804", "Perdizes"),
    "MUNICIPIO DE PERDOES|MG": ("3149903", "Perdões"),
    "MUNICIPIO DE PEREIRA BARRETO|SP": ("3537404", "Pereira Barreto"),
    "MUNICIPIO DE PEREIRAS|SP": ("3537503", "Pereiras"),
    "MUNICIPIO DE PERIQUITO|MG": ("3149952", "Periquito"),
    "MUNICIPIO DE PERITIBA|SC": ("4212601", "Peritiba"),
    "MUNICIPIO DE PERITORO|MA": ("2108454", "Peritoró"),
    "MUNICIPIO DE PEROBAL|PR": ("4118857", "Perobal"),
    "MUNICIPIO DE PEROLA D'OESTE|PR": ("4119004", "Pérola d'Oeste"),
    "MUNICIPIO DE PEROLA|PR": ("4118907", "Pérola"),
    "MUNICIPIO DE PERUIBE|SP": ("3537602", "Peruíbe"),
    "MUNICIPIO DE PESCADOR|MG": ("3150000", "Pescador"),
    "MUNICIPIO DE PESCARIA BRAVA|SC": ("4212650", "Pescaria Brava"),
    "MUNICIPIO DE PESQUEIRA|PE": ("2610905", "Pesqueira"),
    "MUNICIPIO DE PETROLANDIA|PE": ("2611002", "Petrolândia"),
    "MUNICIPIO DE PETROLANDIA|SC": ("4212700", "Petrolândia"),
    "MUNICIPIO DE PETROLINA DE GOIAS|GO": ("5216809", "Petrolina de Goiás"),
    "MUNICIPIO DE PETROLINA|PE": ("2611101", "Petrolina"),
    "MUNICIPIO DE PIANCO|PB": ("2511301", "Piancó"),
    "MUNICIPIO DE PIATA|BA": ("2924306", "Piatã"),
    "MUNICIPIO DE PIAU|MG": ("3150109", "Piau"),
    "MUNICIPIO DE PICADA CAFE|RS": ("4314423", "Picada Café"),
    "MUNICIPIO DE PICARRA|PA": ("1505635", "Piçarra"),
    "MUNICIPIO DE PICOS|PI": ("2208007", "Picos"),
    "MUNICIPIO DE PICUI|PB": ("2511400", "Picuí"),
    "MUNICIPIO DE PIEDADE DE CARATINGA|MG": ("3150158", "Piedade de Caratinga"),
    "MUNICIPIO DE PIEDADE DE PONTE NOVA|MG": ("3150208", "Piedade de Ponte Nova"),
    "MUNICIPIO DE PIEDADE DOS GERAIS|MG": ("3150406", "Piedade dos Gerais"),
    "MUNICIPIO DE PIEDADE|SP": ("3537800", "Piedade"),
    "MUNICIPIO DE PIEN|PR": ("4119103", "Piên"),
    "MUNICIPIO DE PILAR DE GOIAS|GO": ("5216908", "Pilar de Goiás"),
    "MUNICIPIO DE PILAR DO SUL|SP": ("3537909", "Pilar do Sul"),
    "MUNICIPIO DE PILAR|AL": ("2706901", "Pilar"),
    "MUNICIPIO DE PILAR|PB": ("2511509", "Pilar"),
    "MUNICIPIO DE PILOES|PB": ("2511608", "Pilões"),
    "MUNICIPIO DE PILOES|RN": ("2410009", "Pilões"),
    "MUNICIPIO DE PILOEZINHOS|PB": ("2511707", "Pilõezinhos"),
    "MUNICIPIO DE PIMENTA BUENO|RO": ("1100189", "Pimenta Bueno"),
    "MUNICIPIO DE PIMENTA|MG": ("3150505", "Pimenta"),
    "MUNICIPIO DE PIMENTEIRAS DO OESTE|RO": ("1101468", "Pimenteiras do Oeste"),
    "MUNICIPIO DE PIMENTEIRAS|PI": ("2208106", "Pimenteiras"),
    "MUNICIPIO DE PINDAI|BA": ("2924504", "Pindaí"),
    "MUNICIPIO DE PINDAMONHANGABA|SP": ("3538006", "Pindamonhangaba"),
    "MUNICIPIO DE PINDARE MIRIM|MA": ("2108504", "Pindaré-Mirim"),
    "MUNICIPIO DE PINDOBA|AL": ("2707008", "Pindoba"),
    "MUNICIPIO DE PINDORAMA|SP": ("3538105", "Pindorama"),
    "MUNICIPIO DE PINDORETAMA|CE": ("2310852", "Pindoretama"),
    "MUNICIPIO DE PINGO D'AGUA|MG": ("3150539", "Pingo-d'Água"),
    "MUNICIPIO DE PINHAIS|PR": ("4119152", "Pinhais"),
    "MUNICIPIO DE PINHAL DA SERRA|RS": ("4314464", "Pinhal da Serra"),
    "MUNICIPIO DE PINHAL DO SAO BENTO|PR": ("4119251", "Pinhal de São Bento"),
    "MUNICIPIO DE PINHAL GRANDE|RS": ("4314472", "Pinhal Grande"),
    "MUNICIPIO DE PINHALAO|PR": ("4119202", "Pinhalão"),
    "MUNICIPIO DE PINHALZINHO|SC": ("4212908", "Pinhalzinho"),
    "MUNICIPIO DE PINHALZINHO|SP": ("3538204", "Pinhalzinho"),
    "MUNICIPIO DE PINHAL|RS": ("4314456", "Pinhal"),
    "MUNICIPIO DE PINHAO|PR": ("4119301", "Pinhão"),
    "MUNICIPIO DE PINHAO|SE": ("2805208", "Pinhão"),
    "MUNICIPIO DE PINHEIRAL|RJ": ("3303955", "Pinheiral"),
    "MUNICIPIO DE PINHEIRO MACHADO|RS": ("4314506", "Pinheiro Machado"),
    "MUNICIPIO DE PINHEIRO PRETO|SC": ("4213005", "Pinheiro Preto"),
    "MUNICIPIO DE PINHEIROS|ES": ("3204104", "Pinheiros"),
    "MUNICIPIO DE PINHEIRO|MA": ("2108603", "Pinheiro"),
    "MUNICIPIO DE PINTO BANDEIRA|RS": ("4314548", "Pinto Bandeira"),
    "MUNICIPIO DE PINTOPOLIS|MG": ("3150570", "Pintópolis"),
    "MUNICIPIO DE PIO IX|PI": ("2208205", "Pio IX"),
    "MUNICIPIO DE PIO XII|MA": ("2108702", "Pio XII"),
    "MUNICIPIO DE PIQUEROBI|SP": ("3538303", "Piquerobi"),
    "MUNICIPIO DE PIQUET CARNEIRO|CE": ("2310902", "Piquet Carneiro"),
    "MUNICIPIO DE PIQUETE|SP": ("3538501", "Piquete"),
    "MUNICIPIO DE PIRACAIA|SP": ("3538600", "Piracaia"),
    "MUNICIPIO DE PIRACANJUBA|GO": ("5217104", "Piracanjuba"),
    "MUNICIPIO DE PIRACEMA|MG": ("3150604", "Piracema"),
    "MUNICIPIO DE PIRACICABA|SP": ("3538709", "Piracicaba"),
    "MUNICIPIO DE PIRAI DO NORTE|BA": ("2924678", "Piraí do Norte"),
    "MUNICIPIO DE PIRAI DO SUL|PR": ("4119400", "Piraí do Sul"),
    "MUNICIPIO DE PIRAI|RJ": ("3304003", "Piraí"),
    "MUNICIPIO DE PIRAJUBA|MG": ("3150703", "Pirajuba"),
    "MUNICIPIO DE PIRAJUI|SP": ("3538907", "Pirajuí"),
    "MUNICIPIO DE PIRAJU|SP": ("3538808", "Piraju"),
    "MUNICIPIO DE PIRANGA|MG": ("3150802", "Piranga"),
    "MUNICIPIO DE PIRANGI|SP": ("3539004", "Pirangi"),
    "MUNICIPIO DE PIRANGUCU|MG": ("3150901", "Piranguçu"),
    "MUNICIPIO DE PIRANGUINHO|MG": ("3151008", "Piranguinho"),
    "MUNICIPIO DE PIRANHAS|AL": ("2707107", "Piranhas"),
    "MUNICIPIO DE PIRANHAS|GO": ("5217203", "Piranhas"),
    "MUNICIPIO DE PIRAPEMAS|MA": ("2108801", "Pirapemas"),
    "MUNICIPIO DE PIRAPETINGA|MG": ("3151107", "Pirapetinga"),
    "MUNICIPIO DE PIRAPORA|MG": ("3151206", "Pirapora"),
    "MUNICIPIO DE PIRAPOZINHO|SP": ("3539202", "Pirapozinho"),
    "MUNICIPIO DE PIRAQUARA|PR": ("4119509", "Piraquara"),
    "MUNICIPIO DE PIRAQUE|TO": ("1717206", "Piraquê"),
    "MUNICIPIO DE PIRASSUNUNGA|SP": ("3539301", "Pirassununga"),
    "MUNICIPIO DE PIRATININGA|SP": ("3539400", "Piratininga"),
    "MUNICIPIO DE PIRATINI|RS": ("4314605", "Piratini"),
    "MUNICIPIO DE PIRATUBA|SC": ("4213104", "Piratuba"),
    "MUNICIPIO DE PIRAUBA|MG": ("3151305", "Piraúba"),
    "MUNICIPIO DE PIRENOPOLIS|GO": ("5217302", "Pirenópolis"),
    "MUNICIPIO DE PIRES DO RIO|GO": ("5217401", "Pires do Rio"),
    "MUNICIPIO DE PIRES FERREIRA|CE": ("2310951", "Pires Ferreira"),
    "MUNICIPIO DE PIRIPA|BA": ("2924702", "Piripá"),
    "MUNICIPIO DE PIRIPIRI|PI": ("2208403", "Piripiri"),
    "MUNICIPIO DE PITANGA|PR": ("4119608", "Pitanga"),
    "MUNICIPIO DE PITANGUEIRAS|PR": ("4119657", "Pitangueiras"),
    "MUNICIPIO DE PITANGUEIRAS|SP": ("3539509", "Pitangueiras"),
    "MUNICIPIO DE PITANGUI|MG": ("3151404", "Pitangui"),
    "MUNICIPIO DE PIUMA|ES": ("3204203", "Piúma"),
    "MUNICIPIO DE PIUMHI|MG": ("3151503", "Piumhi"),
    "MUNICIPIO DE PIUM|TO": ("1717503", "Pium"),
    "MUNICIPIO DE PLACAS|PA": ("1505650", "Placas"),
    "MUNICIPIO DE PLACIDO DE CASTRO|AC": ("1200385", "Plácido de Castro"),
    "MUNICIPIO DE PLANALTINA DO PARANA|PR": ("4119707", "Planaltina do Paraná"),
    "MUNICIPIO DE PLANALTINA|GO": ("5217609", "Planaltina"),
    "MUNICIPIO DE PLANALTO ALEGRE|SC": ("4213153", "Planalto Alegre"),
    "MUNICIPIO DE PLANALTO|BA": ("2925006", "Planalto"),
    "MUNICIPIO DE PLANALTO|PR": ("4119806", "Planalto"),
    "MUNICIPIO DE PLANALTO|RS": ("4314704", "Planalto"),
    "MUNICIPIO DE PLANALTO|SP": ("3539608", "Planalto"),
    "MUNICIPIO DE PLANURA|MG": ("3151602", "Planura"),
    "MUNICIPIO DE POA|SP": ("3539806", "Poá"),
    "MUNICIPIO DE POCAO DE PEDRAS|MA": ("2108900", "Poção de Pedras"),
    "MUNICIPIO DE POCAO|PE": ("2611200", "Poção"),
    "MUNICIPIO DE POCINHOS|PB": ("2512002", "Pocinhos"),
    "MUNICIPIO DE POCO BRANCO|RN": ("2410108", "Poço Branco"),
    "MUNICIPIO DE POCO DANTAS|PB": ("2512036", "Poço Dantas"),
    "MUNICIPIO DE POCO DAS ANTAS|RS": ("4314753", "Poço das Antas"),
    "MUNICIPIO DE POCO DAS TRINCHEIRAS|AL": ("2707206", "Poço das Trincheiras"),
    "MUNICIPIO DE POCO DE JOSE DE MOURA|PB": ("2512077", "Poço de José de Moura"),
    "MUNICIPIO DE POCO FUNDO|MG": ("3151701", "Poço Fundo"),
    "MUNICIPIO DE POCO REDONDO|SE": ("2805406", "Poço Redondo"),
    "MUNICIPIO DE POCO VERDE|SE": ("2805505", "Poço Verde"),
    "MUNICIPIO DE POCOES|BA": ("2925105", "Poções"),
    "MUNICIPIO DE POCRANE|MG": ("3151909", "Pocrane"),
    "MUNICIPIO DE POJUCA|BA": ("2925204", "Pojuca"),
    "MUNICIPIO DE POMBOS|PE": ("2611309", "Pombos"),
    "MUNICIPIO DE POMERODE|SC": ("4213203", "Pomerode"),
    "MUNICIPIO DE POMPEU|MG": ("3152006", "Pompéu"),
    "MUNICIPIO DE PONGAI|SP": ("3540101", "Pongaí"),
    "MUNICIPIO DE PONTA DE PEDRAS|PA": ("1505700", "Ponta de Pedras"),
    "MUNICIPIO DE PONTA GROSSA|PR": ("4119905", "Ponta Grossa"),
    "MUNICIPIO DE PONTA PORA|MS": ("5006606", "Ponta Porã"),
    "MUNICIPIO DE PONTAL DO ARAGUAIA|MT": ("5106653", "Pontal do Araguaia"),
    "MUNICIPIO DE PONTAL DO PARANA|PR": ("4119954", "Pontal do Paraná"),
    "MUNICIPIO DE PONTALINA|GO": ("5217708", "Pontalina"),
    "MUNICIPIO DE PONTAO|RS": ("4314779", "Pontão"),
    "MUNICIPIO DE PONTE ALTA DO BOM JESUS|TO": ("1717800", "Ponte Alta do Bom Jesus"),
    "MUNICIPIO DE PONTE ALTA DO NORTE|SC": ("4213351", "Ponte Alta do Norte"),
    "MUNICIPIO DE PONTE ALTA DO TOCANTINS|TO": ("1717909", "Ponte Alta do Tocantins"),
    "MUNICIPIO DE PONTE ALTA|SC": ("4213302", "Ponte Alta"),
    "MUNICIPIO DE PONTE BRANCA|MT": ("5106703", "Ponte Branca"),
    "MUNICIPIO DE PONTE NOVA|MG": ("3152105", "Ponte Nova"),
    "MUNICIPIO DE PONTE PRETA|RS": ("4314787", "Ponte Preta"),
    "MUNICIPIO DE PONTE SERRADA|SC": ("4213401", "Ponte Serrada"),
    "MUNICIPIO DE PONTES GESTAL|SP": ("3540309", "Pontes Gestal"),
    "MUNICIPIO DE PONTO BELO|ES": ("3204252", "Ponto Belo"),
    "MUNICIPIO DE PONTO CHIQUE|MG": ("3152131", "Ponto Chique"),
    "MUNICIPIO DE PONTO DOS VOLANTES|MG": ("3152170", "Ponto dos Volantes"),
    "MUNICIPIO DE PONTO NOVO|BA": ("2925253", "Ponto Novo"),
    "MUNICIPIO DE POPULINA|SP": ("3540408", "Populina"),
    "MUNICIPIO DE PORANGABA|SP": ("3540507", "Porangaba"),
    "MUNICIPIO DE PORCIUNCULA|RJ": ("3304102", "Porciúncula"),
    "MUNICIPIO DE PORECATU|PR": ("4120002", "Porecatu"),
    "MUNICIPIO DE PORTALEGRE|RN": ("2410207", "Portalegre"),
    "MUNICIPIO DE PORTAO|RS": ("4314803", "Portão"),
    "MUNICIPIO DE PORTEIRINHA|MG": ("3152204", "Porteirinha"),
    "MUNICIPIO DE PORTELANDIA|GO": ("5218102", "Portelândia"),
    "MUNICIPIO DE PORTO ACRE|AC": ("1200807", "Porto Acre"),
    "MUNICIPIO DE PORTO ALEGRE DO NORTE|MT": ("5106778", "Porto Alegre do Norte"),
    "MUNICIPIO DE PORTO ALEGRE DO PIAUI|PI": ("2208551", "Porto Alegre do Piauí"),
    "MUNICIPIO DE PORTO ALEGRE DO TOCANTINS|TO": ("1718006", "Porto Alegre do Tocantins"),
    "MUNICIPIO DE PORTO ALEGRE|RS": ("4314902", "Porto Alegre"),
    "MUNICIPIO DE PORTO AMAZONAS|PR": ("4120101", "Porto Amazonas"),
    "MUNICIPIO DE PORTO BARREIRO|PR": ("4120150", "Porto Barreiro"),
    "MUNICIPIO DE PORTO BELO|SC": ("4213500", "Porto Belo"),
    "MUNICIPIO DE PORTO CALVO|AL": ("2707305", "Porto Calvo"),
    "MUNICIPIO DE PORTO DA FOLHA|SE": ("2805604", "Porto da Folha"),
    "MUNICIPIO DE PORTO DE MOZ|PA": ("1505908", "Porto de Moz"),
    "MUNICIPIO DE PORTO DE PEDRAS|AL": ("2707404", "Porto de Pedras"),
    "MUNICIPIO DE PORTO DOS GAUCHOS|MT": ("5106802", "Porto dos Gaúchos"),
    "MUNICIPIO DE PORTO ESPERIDIAO|MT": ("5106828", "Porto Esperidião"),
    "MUNICIPIO DE PORTO ESTRELA|MT": ("5106851", "Porto Estrela"),
    "MUNICIPIO DE PORTO FELIZ|SP": ("3540606", "Porto Feliz"),
    "MUNICIPIO DE PORTO FERREIRA|SP": ("3540705", "Porto Ferreira"),
    "MUNICIPIO DE PORTO FIRME|MG": ("3152303", "Porto Firme"),
    "MUNICIPIO DE PORTO FRANCO|MA": ("2109007", "Porto Franco"),
    "MUNICIPIO DE PORTO GRANDE|AP": ("1600535", "Porto Grande"),
    "MUNICIPIO DE PORTO LUCENA|RS": ("4315008", "Porto Lucena"),
    "MUNICIPIO DE PORTO MAUA|RS": ("4315057", "Porto Mauá"),
    "MUNICIPIO DE PORTO MURTINHO|MS": ("5006903", "Porto Murtinho"),
    "MUNICIPIO DE PORTO NACIONAL|TO": ("1718204", "Porto Nacional"),
    "MUNICIPIO DE PORTO RICO|PR": ("4120200", "Porto Rico"),
    "MUNICIPIO DE PORTO SEGURO|BA": ("2925303", "Porto Seguro"),
    "MUNICIPIO DE PORTO UNIAO|SC": ("4213609", "Porto União"),
    "MUNICIPIO DE PORTO VELHO|RO": ("1100205", "Porto Velho"),
    "MUNICIPIO DE PORTO WALTER|AC": ("1200393", "Porto Walter"),
    "MUNICIPIO DE PORTO XAVIER|RS": ("4315107", "Porto Xavier"),
    "MUNICIPIO DE PORTO|PI": ("2208502", "Porto"),
    "MUNICIPIO DE POSSE|GO": ("5218300", "Posse"),
    "MUNICIPIO DE POTENGI|CE": ("2311207", "Potengi"),
    "MUNICIPIO DE POTE|MG": ("3152402", "Poté"),
    "MUNICIPIO DE POTIRAGUA|BA": ("2925402", "Potiraguá"),
    "MUNICIPIO DE POTIRENDABA|SP": ("3540804", "Potirendaba"),
    "MUNICIPIO DE POTIRETAMA|CE": ("2311231", "Potiretama"),
    "MUNICIPIO DE POUSO ALEGRE|MG": ("3152501", "Pouso Alegre"),
    "MUNICIPIO DE POUSO ALTO|MG": ("3152600", "Pouso Alto"),
    "MUNICIPIO DE POUSO NOVO|RS": ("4315131", "Pouso Novo"),
    "MUNICIPIO DE POUSO REDONDO|SC": ("4213708", "Pouso Redondo"),
    "MUNICIPIO DE POXOREO|MT": ("5107008", "Poxoréu"),
    "MUNICIPIO DE PRACUUBA|AP": ("1600550", "Pracuúba"),
    "MUNICIPIO DE PRADO FERREIRA|PR": ("4120333", "Prado Ferreira"),
    "MUNICIPIO DE PRADOPOLIS|SP": ("3540903", "Pradópolis"),
    "MUNICIPIO DE PRADOS|MG": ("3152709", "Prados"),
    "MUNICIPIO DE PRADO|BA": ("2925501", "Prado"),
    "MUNICIPIO DE PRAIA GRANDE|SC": ("4213807", "Praia Grande"),
    "MUNICIPIO DE PRAIA GRANDE|SP": ("3541000", "Praia Grande"),
    "MUNICIPIO DE PRAIA NORTE|TO": ("1718303", "Praia Norte"),
    "MUNICIPIO DE PRAINHA|PA": ("1506005", "Prainha"),
    "MUNICIPIO DE PRANCHITA|PR": ("4120358", "Pranchita"),
    "MUNICIPIO DE PRATA DO PIAUI|PI": ("2208601", "Prata do Piauí"),
    "MUNICIPIO DE PRATAPOLIS|MG": ("3152907", "Pratápolis"),
    "MUNICIPIO DE PRATA|MG": ("3152808", "Prata"),
    "MUNICIPIO DE PRATA|PB": ("2512200", "Prata"),
    "MUNICIPIO DE PRATINHA|MG": ("3153004", "Pratinha"),
    "MUNICIPIO DE PRESIDENTE ALVES|SP": ("3541109", "Presidente Alves"),
    "MUNICIPIO DE PRESIDENTE BERNARDES|MG": ("3153103", "Presidente Bernardes"),
    "MUNICIPIO DE PRESIDENTE BERNARDES|SP": ("3541208", "Presidente Bernardes"),
    "MUNICIPIO DE PRESIDENTE CASTELO BRANCO|PR": ("4120408", "Presidente Castelo Branco"),
    "MUNICIPIO DE PRESIDENTE DUTRA|BA": ("2925600", "Presidente Dutra"),
    "MUNICIPIO DE PRESIDENTE DUTRA|MA": ("2109106", "Presidente Dutra"),
    "MUNICIPIO DE PRESIDENTE EPITACIO|SP": ("3541307", "Presidente Epitácio"),
    "MUNICIPIO DE PRESIDENTE FIGUEIREDO|AM": ("1303536", "Presidente Figueiredo"),
    "MUNICIPIO DE PRESIDENTE GETULIO|SC": ("4214003", "Presidente Getúlio"),
    "MUNICIPIO DE PRESIDENTE JANIO QUADROS|BA": ("2925709", "Presidente Jânio Quadros"),
    "MUNICIPIO DE PRESIDENTE JUSCELINO|MA": ("2109205", "Presidente Juscelino"),
    "MUNICIPIO DE PRESIDENTE JUSCELINO|MG": ("3153202", "Presidente Juscelino"),
    "MUNICIPIO DE PRESIDENTE KENNEDY|ES": ("3204302", "Presidente Kennedy"),
    "MUNICIPIO DE PRESIDENTE KENNEDY|TO": ("1718402", "Presidente Kennedy"),
    "MUNICIPIO DE PRESIDENTE KUBITSCHEK|MG": ("3153301", "Presidente Kubitschek"),
    "MUNICIPIO DE PRESIDENTE LUCENA|RS": ("4315149", "Presidente Lucena"),
    "MUNICIPIO DE PRESIDENTE MEDICI|MA": ("2109239", "Presidente Médici"),
    "MUNICIPIO DE PRESIDENTE MEDICI|RO": ("1100254", "Presidente Médici"),
    "MUNICIPIO DE PRESIDENTE NEREU|SC": ("4214102", "Presidente Nereu"),
    "MUNICIPIO DE PRESIDENTE OLEGARIO|MG": ("3153400", "Presidente Olegário"),
    "MUNICIPIO DE PRESIDENTE PRUDENTE|SP": ("3541406", "Presidente Prudente"),
    "MUNICIPIO DE PRESIDENTE TANCREDO NEVES|BA": ("2925758", "Presidente Tancredo Neves"),
    "MUNICIPIO DE PRESIDENTE VARGAS|MA": ("2109304", "Presidente Vargas"),
    "MUNICIPIO DE PRESIDENTE VENCESLAU|SP": ("3541505", "Presidente Venceslau"),
    "MUNICIPIO DE PRIMAVERA DE RONDONIA|RO": ("1101476", "Primavera de Rondônia"),
    "MUNICIPIO DE PRIMAVERA DO LESTE|MT": ("5107040", "Primavera do Leste"),
    "MUNICIPIO DE PRIMAVERA|PA": ("1506104", "Primavera"),
    "MUNICIPIO DE PRIMAVERA|PE": ("2611408", "Primavera"),
    "MUNICIPIO DE PRIMEIRA CRUZ|MA": ("2109403", "Primeira Cruz"),
    "MUNICIPIO DE PRIMEIRO DE MAIO|PR": ("4120507", "Primeiro de Maio"),
    "MUNICIPIO DE PRINCESA|SC": ("4214151", "Princesa"),
    "MUNICIPIO DE PROFESSOR JAMIL|GO": ("5218391", "Professor Jamil"),
    "MUNICIPIO DE PROGRESSO|RS": ("4315156", "Progresso"),
    "MUNICIPIO DE PROMISSAO|SP": ("3541604", "Promissão"),
    "MUNICIPIO DE PROPRIA|SE": ("2805703", "Propriá"),
    "MUNICIPIO DE PROTASIO ALVES|RS": ("4315172", "Protásio Alves"),
    "MUNICIPIO DE PRUDENTE DE MORAIS|MG": ("3153608", "Prudente de Morais"),
    "MUNICIPIO DE PRUDENTOPOLIS|PR": ("4120606", "Prudentópolis"),
    "MUNICIPIO DE PUGMIL|TO": ("1718451", "Pugmil"),
    "MUNICIPIO DE PUREZA|RN": ("2410405", "Pureza"),
    "MUNICIPIO DE PUTINGA|RS": ("4315206", "Putinga"),
    "MUNICIPIO DE PUXINANA|PB": ("2512408", "Puxinanã"),
    "MUNICIPIO DE QUARAI|RS": ("4315305", "Quaraí"),
    "MUNICIPIO DE QUARTEL GERAL|MG": ("3153707", "Quartel Geral"),
    "MUNICIPIO DE QUARTO CENTENARIO|PR": ("4120655", "Quarto Centenário"),
    "MUNICIPIO DE QUATA|SP": ("3541703", "Quatá"),
    "MUNICIPIO DE QUATIGUA|PR": ("4120705", "Quatiguá"),
    "MUNICIPIO DE QUATIPURU|PA": ("1506112", "Quatipuru"),
    "MUNICIPIO DE QUATIS|RJ": ("3304128", "Quatis"),
    "MUNICIPIO DE QUATRO BARRAS|PR": ("4120804", "Quatro Barras"),
    "MUNICIPIO DE QUATRO PONTES|PR": ("4120853", "Quatro Pontes"),
    "MUNICIPIO DE QUEBRANGULO|AL": ("2707602", "Quebrangulo"),
    "MUNICIPIO DE QUEDAS DO IGUACU|PR": ("4120903", "Quedas do Iguaçu"),
    "MUNICIPIO DE QUEIMADA NOVA|PI": ("2208650", "Queimada Nova"),
    "MUNICIPIO DE QUEIMADAS|BA": ("2925808", "Queimadas"),
    "MUNICIPIO DE QUEIMADAS|PB": ("2512507", "Queimadas"),
    "MUNICIPIO DE QUEIMADOS|RJ": ("3304144", "Queimados"),
    "MUNICIPIO DE QUEIROZ|SP": ("3541802", "Queiroz"),
    "MUNICIPIO DE QUELUZITA|MG": ("3153806", "Queluzito"),
    "MUNICIPIO DE QUELUZ|SP": ("3541901", "Queluz"),
    "MUNICIPIO DE QUERENCIA DO NORTE|PR": ("4121000", "Querência do Norte"),
    "MUNICIPIO DE QUEVEDOS|RS": ("4315321", "Quevedos"),
    "MUNICIPIO DE QUIJINGUE|BA": ("2925907", "Quijingue"),
    "MUNICIPIO DE QUILOMBO|SC": ("4214201", "Quilombo"),
    "MUNICIPIO DE QUINTA DO SOL|PR": ("4121109", "Quinta do Sol"),
    "MUNICIPIO DE QUINTANA|SP": ("3542008", "Quintana"),
    "MUNICIPIO DE QUINZE DE NOVEMBRO|RS": ("4315354", "Quinze de Novembro"),
    "MUNICIPIO DE QUIPAPA|PE": ("2611507", "Quipapá"),
    "MUNICIPIO DE QUIRINOPOLIS|GO": ("5218508", "Quirinópolis"),
    "MUNICIPIO DE QUISSAMA|RJ": ("3304151", "Quissamã"),
    "MUNICIPIO DE QUITANDINHA|PR": ("4121208", "Quitandinha"),
    "MUNICIPIO DE QUITERIANOPOLIS|CE": ("2311264", "Quiterianópolis"),
    "MUNICIPIO DE QUIXABA|PB": ("2512606", "Quixaba"),
    "MUNICIPIO DE QUIXABA|PE": ("2611533", "Quixaba"),
    "MUNICIPIO DE QUIXABEIRA|BA": ("2925931", "Quixabeira"),
    "MUNICIPIO DE QUIXADA|CE": ("2311306", "Quixadá"),
    "MUNICIPIO DE QUIXELO|CE": ("2311355", "Quixelô"),
    "MUNICIPIO DE QUIXERAMOBIM|CE": ("2311405", "Quixeramobim"),
    "MUNICIPIO DE QUIXERE|CE": ("2311504", "Quixeré"),
    "MUNICIPIO DE RAFAEL FERNANDES|RN": ("2410504", "Rafael Fernandes"),
    "MUNICIPIO DE RAFAEL GODEIRO|RN": ("2410603", "Rafael Godeiro"),
    "MUNICIPIO DE RAFARD|SP": ("3542107", "Rafard"),
    "MUNICIPIO DE RAMILANDIA|PR": ("4121257", "Ramilândia"),
    "MUNICIPIO DE RANCHARIA|SP": ("3542206", "Rancharia"),
    "MUNICIPIO DE RANCHO ALEGRE D:OESTE|PR": ("4121356", "Rancho Alegre D'Oeste"),
    "MUNICIPIO DE RANCHO ALEGRE|PR": ("4121307", "Rancho Alegre"),
    "MUNICIPIO DE RANCHO QUEIMADO|SC": ("4214300", "Rancho Queimado"),
    "MUNICIPIO DE RAPOSA|MA": ("2109452", "Raposa"),
    "MUNICIPIO DE RAPOSOS|MG": ("3153905", "Raposos"),
    "MUNICIPIO DE RAUL SOARES|MG": ("3154002", "Raul Soares"),
    "MUNICIPIO DE REALEZA|PR": ("4121406", "Realeza"),
    "MUNICIPIO DE REBOUCAS|PR": ("4121505", "Rebouças"),
    "MUNICIPIO DE RECREIO|MG": ("3154101", "Recreio"),
    "MUNICIPIO DE RECURSOLANDIA|TO": ("1718501", "Recursolândia"),
    "MUNICIPIO DE REDENCAO DA SERRA|SP": ("3542305", "Redenção da Serra"),
    "MUNICIPIO DE REDENCAO DO GURGUEIA|PI": ("2208700", "Redenção do Gurguéia"),
    "MUNICIPIO DE REDENCAO|CE": ("2311603", "Redenção"),
    "MUNICIPIO DE REDENCAO|PA": ("1506138", "Redenção"),
    "MUNICIPIO DE REDUTO|MG": ("3154150", "Reduto"),
    "MUNICIPIO DE REGENERACAO|PI": ("2208809", "Regeneração"),
    "MUNICIPIO DE REGENTE FEIJO|SP": ("3542404", "Regente Feijó"),
    "MUNICIPIO DE REGINOPOLIS|SP": ("3542503", "Reginópolis"),
    "MUNICIPIO DE REGISTRO|SP": ("3542602", "Registro"),
    "MUNICIPIO DE RELVADO|RS": ("4315453", "Relvado"),
    "MUNICIPIO DE REMANSO|BA": ("2926004", "Remanso"),
    "MUNICIPIO DE REMIGIO|PB": ("2512705", "Remígio"),
    "MUNICIPIO DE RERIUTABA|CE": ("2311702", "Reriutaba"),
    "MUNICIPIO DE RESENDE COSTA|MG": ("3154200", "Resende Costa"),
    "MUNICIPIO DE RESERVA DO IGUACU|PR": ("4121752", "Reserva do Iguaçu"),
    "MUNICIPIO DE RESERVA|PR": ("4121703", "Reserva"),
    "MUNICIPIO DE RESPLENDOR|MG": ("3154309", "Resplendor"),
    "MUNICIPIO DE RESSAQUINHA|MG": ("3154408", "Ressaquinha"),
    "MUNICIPIO DE RESTINGA|SP": ("3542701", "Restinga"),
    "MUNICIPIO DE RETIROLANDIA|BA": ("2926103", "Retirolândia"),
    "MUNICIPIO DE RIACHAO DAS NEVES|BA": ("2926202", "Riachão das Neves"),
    "MUNICIPIO DE RIACHAO DO BACAMARTE|PB": ("2512754", "Riachão do Bacamarte"),
    "MUNICIPIO DE RIACHAO DO DANTAS|SE": ("2805802", "Riachão do Dantas"),
    "MUNICIPIO DE RIACHAO DO JACUIPE|BA": ("2926301", "Riachão do Jacuípe"),
    "MUNICIPIO DE RIACHAO|MA": ("2109502", "Riachão"),
    "MUNICIPIO DE RIACHAO|PB": ("2512747", "Riachão"),
    "MUNICIPIO DE RIACHINHO|MG": ("3154457", "Riachinho"),
    "MUNICIPIO DE RIACHINHO|TO": ("1718550", "Riachinho"),
    "MUNICIPIO DE RIACHO DA CRUZ|RN": ("2410702", "Riacho da Cruz"),
    "MUNICIPIO DE RIACHO DE SANTANA|BA": ("2926400", "Riacho de Santana"),
    "MUNICIPIO DE RIACHO DE SANTANA|RN": ("2410801", "Riacho de Santana"),
    "MUNICIPIO DE RIACHO DE SANTO ANTONIO|PB": ("2512788", "Riacho de Santo Antônio"),
    "MUNICIPIO DE RIACHO DOS CAVALOS|PB": ("2512804", "Riacho dos Cavalos"),
    "MUNICIPIO DE RIACHO DOS MACHADOS|MG": ("3154507", "Riacho dos Machados"),
    "MUNICIPIO DE RIACHO FRIO|PI": ("2208858", "Riacho Frio"),
    "MUNICIPIO DE RIACHUELO|RN": ("2410900", "Riachuelo"),
    "MUNICIPIO DE RIACHUELO|SE": ("2805901", "Riachuelo"),
    "MUNICIPIO DE RIALMA|GO": ("5218607", "Rialma"),
    "MUNICIPIO DE RIANAPOLIS|GO": ("5218706", "Rianápolis"),
    "MUNICIPIO DE RIBAMAR FIQUENE|MA": ("2109551", "Ribamar Fiquene"),
    "MUNICIPIO DE RIBAS DO RIO PARDO|MS": ("5007109", "Ribas do Rio Pardo"),
    "MUNICIPIO DE RIBEIRA DO AMPARO|BA": ("2926509", "Ribeira do Amparo"),
    "MUNICIPIO DE RIBEIRAO BONITO|SP": ("3542909", "Ribeirão Bonito"),
    "MUNICIPIO DE RIBEIRAO BRANCO|SP": ("3543006", "Ribeirão Branco"),
    "MUNICIPIO DE RIBEIRAO CASCALHEIRA|MT": ("5107180", "Ribeirão Cascalheira"),
    "MUNICIPIO DE RIBEIRAO CLARO|PR": ("4121802", "Ribeirão Claro"),
    "MUNICIPIO DE RIBEIRAO CORRENTE|SP": ("3543105", "Ribeirão Corrente"),
    "MUNICIPIO DE RIBEIRAO DAS NEVES|MG": ("3154606", "Ribeirão das Neves"),
    "MUNICIPIO DE RIBEIRAO DO LARGO|BA": ("2926657", "Ribeirão do Largo"),
    "MUNICIPIO DE RIBEIRAO DO PINHAL|PR": ("4121901", "Ribeirão do Pinhal"),
    "MUNICIPIO DE RIBEIRAO DO SUL|SP": ("3543204", "Ribeirão do Sul"),
    "MUNICIPIO DE RIBEIRAO GRANDE|SP": ("3543253", "Ribeirão Grande"),
    "MUNICIPIO DE RIBEIRAO PIRES|SP": ("3543303", "Ribeirão Pires"),
    "MUNICIPIO DE RIBEIRAO PRETO|SP": ("3543402", "Ribeirão Preto"),
    "MUNICIPIO DE RIBEIRAOZINHO|MT": ("5107198", "Ribeirãozinho"),
    "MUNICIPIO DE RIBEIRA|SP": ("3542800", "Ribeira"),
    "MUNICIPIO DE RIBEIRO GONCALVES|PI": ("2208908", "Ribeiro Gonçalves"),
    "MUNICIPIO DE RIBEIROPOLIS|SE": ("2806008", "Ribeirópolis"),
    "MUNICIPIO DE RIFAINA|SP": ("3543600", "Rifaina"),
    "MUNICIPIO DE RINCAO|SP": ("3543709", "Rincão"),
    "MUNICIPIO DE RIO ACIMA|MG": ("3154804", "Rio Acima"),
    "MUNICIPIO DE RIO AZUL|PR": ("4122008", "Rio Azul"),
    "MUNICIPIO DE RIO BANANAL|ES": ("3204351", "Rio Bananal"),
    "MUNICIPIO DE RIO BOM|PR": ("4122107", "Rio Bom"),
    "MUNICIPIO DE RIO BONITO DO IGUACU|PR": ("4122156", "Rio Bonito do Iguaçu"),
    "MUNICIPIO DE RIO BONITO|RJ": ("3304300", "Rio Bonito"),
    "MUNICIPIO DE RIO BRANCO DO IVAI|PR": ("4122172", "Rio Branco do Ivaí"),
    "MUNICIPIO DE RIO BRANCO DO SUL|PR": ("4122206", "Rio Branco do Sul"),
    "MUNICIPIO DE RIO BRANCO|AC": ("1200401", "Rio Branco"),
    "MUNICIPIO DE RIO BRANCO|MT": ("5107206", "Rio Branco"),
    "MUNICIPIO DE RIO BRILHANTE|MS": ("5007208", "Rio Brilhante"),
    "MUNICIPIO DE RIO CASCA|MG": ("3154903", "Rio Casca"),
    "MUNICIPIO DE RIO CLARO|RJ": ("3304409", "Rio Claro"),
    "MUNICIPIO DE RIO CLARO|SP": ("3543907", "Rio Claro"),
    "MUNICIPIO DE RIO CRESPO|RO": ("1100262", "Rio Crespo"),
    "MUNICIPIO DE RIO DA CONCEICAO|TO": ("1718659", "Rio da Conceição"),
    "MUNICIPIO DE RIO DAS ANTAS|SC": ("4214409", "Rio das Antas"),
    "MUNICIPIO DE RIO DAS FLORES|RJ": ("3304508", "Rio das Flores"),
    "MUNICIPIO DE RIO DAS OSTRAS|RJ": ("3304524", "Rio das Ostras"),
    "MUNICIPIO DE RIO DAS PEDRAS|SP": ("3544004", "Rio das Pedras"),
    "MUNICIPIO DE RIO DE CONTAS|BA": ("2926707", "Rio de Contas"),
    "MUNICIPIO DE RIO DE JANEIRO|RJ": ("3304557", "Rio de Janeiro"),
    "MUNICIPIO DE RIO DO ANTONIO|BA": ("2926806", "Rio do Antônio"),
    "MUNICIPIO DE RIO DO CAMPO|SC": ("4214508", "Rio do Campo"),
    "MUNICIPIO DE RIO DO OESTE|SC": ("4214607", "Rio do Oeste"),
    "MUNICIPIO DE RIO DO PIRES|BA": ("2926905", "Rio do Pires"),
    "MUNICIPIO DE RIO DO PRADO|MG": ("3155108", "Rio do Prado"),
    "MUNICIPIO DE RIO DO SUL|SC": ("4214805", "Rio do Sul"),
    "MUNICIPIO DE RIO DOS BOIS|TO": ("1718709", "Rio dos Bois"),
    "MUNICIPIO DE RIO DOS CEDROS|SC": ("4214706", "Rio dos Cedros"),
    "MUNICIPIO DE RIO ESPERA|MG": ("3155207", "Rio Espera"),
    "MUNICIPIO DE RIO FORMOSO|PE": ("2611903", "Rio Formoso"),
    "MUNICIPIO DE RIO FORTUNA|SC": ("4214904", "Rio Fortuna"),
    "MUNICIPIO DE RIO GRANDE DA SERRA|SP": ("3544103", "Rio Grande da Serra"),
    "MUNICIPIO DE RIO GRANDE DO PIAUI|PI": ("2209005", "Rio Grande do Piauí"),
    "MUNICIPIO DE RIO LARGO|AL": ("2707701", "Rio Largo"),
    "MUNICIPIO DE RIO MANSO|MG": ("3155306", "Rio Manso"),
    "MUNICIPIO DE RIO MARIA|PA": ("1506161", "Rio Maria"),
    "MUNICIPIO DE RIO NEGRINHO|SC": ("4215000", "Rio Negrinho"),
    "MUNICIPIO DE RIO NEGRO|MS": ("5007307", "Rio Negro"),
    "MUNICIPIO DE RIO NEGRO|PR": ("4122305", "Rio Negro"),
    "MUNICIPIO DE RIO NOVO DO SUL|ES": ("3204401", "Rio Novo do Sul"),
    "MUNICIPIO DE RIO NOVO|MG": ("3155405", "Rio Novo"),
    "MUNICIPIO DE RIO PARANAIBA|MG": ("3155504", "Rio Paranaíba"),
    "MUNICIPIO DE RIO PARDO DE MINAS|MG": ("3155603", "Rio Pardo de Minas"),
    "MUNICIPIO DE RIO PARDO|RS": ("4315701", "Rio Pardo"),
    "MUNICIPIO DE RIO POMBA|MG": ("3155801", "Rio Pomba"),
    "MUNICIPIO DE RIO PRETO DA EVA|AM": ("1303569", "Rio Preto da Eva"),
    "MUNICIPIO DE RIO PRETO|MG": ("3155900", "Rio Preto"),
    "MUNICIPIO DE RIO QUENTE|GO": ("5218789", "Rio Quente"),
    "MUNICIPIO DE RIO REAL|BA": ("2927002", "Rio Real"),
    "MUNICIPIO DE RIO RUFINO|SC": ("4215059", "Rio Rufino"),
    "MUNICIPIO DE RIO SONO|TO": ("1718758", "Rio Sono"),
    "MUNICIPIO DE RIO TINTO|PB": ("2512903", "Rio Tinto"),
    "MUNICIPIO DE RIO VERDE DE MATO GROSSO|MS": ("5007406", "Rio Verde de Mato Grosso"),
    "MUNICIPIO DE RIO VERMELHO|MG": ("3156007", "Rio Vermelho"),
    "MUNICIPIO DE RIOZINHO|RS": ("4315750", "Riozinho"),
    "MUNICIPIO DE RIQUEZA|SC": ("4215075", "Riqueza"),
    "MUNICIPIO DE RITAPOLIS|MG": ("3156106", "Ritápolis"),
    "MUNICIPIO DE ROCHEDO DE MINAS|MG": ("3156205", "Rochedo de Minas"),
    "MUNICIPIO DE ROCHEDO|MS": ("5007505", "Rochedo"),
    "MUNICIPIO DE RODEIO BONITO|RS": ("4315909", "Rodeio Bonito"),
    "MUNICIPIO DE RODEIO|SC": ("4215109", "Rodeio"),
    "MUNICIPIO DE RODEIRO|MG": ("3156304", "Rodeiro"),
    "MUNICIPIO DE RODELAS|BA": ("2927101", "Rodelas"),
    "MUNICIPIO DE RODOLFO FERNANDES|RN": ("2411007", "Rodolfo Fernandes"),
    "MUNICIPIO DE RODRIGUES ALVES|AC": ("1200427", "Rodrigues Alves"),
    "MUNICIPIO DE ROLANDIA|PR": ("4122404", "Rolândia"),
    "MUNICIPIO DE ROLANTE|RS": ("4316006", "Rolante"),
    "MUNICIPIO DE ROLIM DE MOURA|RO": ("1100288", "Rolim de Moura"),
    "MUNICIPIO DE ROMARIA|MG": ("3156403", "Romaria"),
    "MUNICIPIO DE ROMELANDIA|SC": ("4215208", "Romelândia"),
    "MUNICIPIO DE RONCADOR|PR": ("4122503", "Roncador"),
    "MUNICIPIO DE RONDA ALTA|RS": ("4316105", "Ronda Alta"),
    "MUNICIPIO DE RONDINHA|RS": ("4316204", "Rondinha"),
    "MUNICIPIO DE RONDOLANDIA|MT": ("5107578", "Rondolândia"),
    "MUNICIPIO DE RONDON DO PARA|PA": ("1506187", "Rondon do Pará"),
    "MUNICIPIO DE RONDONOPOLIS|MT": ("5107602", "Rondonópolis"),
    "MUNICIPIO DE RONDON|PR": ("4122602", "Rondon"),
    "MUNICIPIO DE ROQUE GONZALES|RS": ("4316303", "Roque Gonzales"),
    "MUNICIPIO DE RORAINOPOLIS|RR": ("1400472", "Rorainópolis"),
    "MUNICIPIO DE ROSANA|SP": ("3544251", "Rosana"),
    "MUNICIPIO DE ROSARIO DO CATETE|SE": ("2806107", "Rosário do Catete"),
    "MUNICIPIO DE ROSARIO DO IVAI|PR": ("4122651", "Rosário do Ivaí"),
    "MUNICIPIO DE ROSARIO DO SUL|RS": ("4316402", "Rosário do Sul"),
    "MUNICIPIO DE RUBELITA|MG": ("3156502", "Rubelita"),
    "MUNICIPIO DE RUBIACEA|SP": ("3544400", "Rubiácea"),
    "MUNICIPIO DE RUBIATABA|GO": ("5218904", "Rubiataba"),
    "MUNICIPIO DE RUBIM|MG": ("3156601", "Rubim"),
    "MUNICIPIO DE RUBINEIA|SP": ("3544509", "Rubinéia"),
    "MUNICIPIO DE RUROPOLIS|PA": ("1506195", "Rurópolis"),
    "MUNICIPIO DE RUSSAS|CE": ("2311801", "Russas"),
    "MUNICIPIO DE RUY BARBOSA|BA": ("2927200", "Ruy Barbosa"),
    "MUNICIPIO DE RUY BARBOSA|RN": ("2411106", "Ruy Barbosa"),
    "MUNICIPIO DE SABARA|MG": ("3156700", "Sabará"),
    "MUNICIPIO DE SABAUDIA|PR": ("4122701", "Sabáudia"),
    "MUNICIPIO DE SABINOPOLIS|MG": ("3156809", "Sabinópolis"),
    "MUNICIPIO DE SABINO|SP": ("3544608", "Sabino"),
    "MUNICIPIO DE SABOEIRO|CE": ("2311900", "Saboeiro"),
    "MUNICIPIO DE SACRAMENTO|MG": ("3156908", "Sacramento"),
    "MUNICIPIO DE SAGRADA FAMILIA|RS": ("4316428", "Sagrada Família"),
    "MUNICIPIO DE SAIRE|PE": ("2612000", "Sairé"),
    "MUNICIPIO DE SALES OLIVEIRA|SP": ("3544905", "Sales Oliveira"),
    "MUNICIPIO DE SALETE|SC": ("4215307", "Salete"),
    "MUNICIPIO DE SALGADINHO|PB": ("2513000", "Salgadinho"),
    "MUNICIPIO DE SALGADINHO|PE": ("2612109", "Salgadinho"),
    "MUNICIPIO DE SALGADO DE SAO FELIX|PB": ("2513109", "Salgado de São Félix"),
    "MUNICIPIO DE SALGADO FILHO|PR": ("4122800", "Salgado Filho"),
    "MUNICIPIO DE SALGADO|SE": ("2806206", "Salgado"),
    "MUNICIPIO DE SALGUEIRO|PE": ("2612208", "Salgueiro"),
    "MUNICIPIO DE SALINAS DA MARGARIDA|BA": ("2927309", "Salinas da Margarida"),
    "MUNICIPIO DE SALINAS|MG": ("3157005", "Salinas"),
    "MUNICIPIO DE SALINOPOLIS|PA": ("1506203", "Salinópolis"),
    "MUNICIPIO DE SALMOURAO|SP": ("3545100", "Salmourão"),
    "MUNICIPIO DE SALOA|PE": ("2612307", "Saloá"),
    "MUNICIPIO DE SALTINHO|SC": ("4215356", "Saltinho"),
    "MUNICIPIO DE SALTINHO|SP": ("3545159", "Saltinho"),
    "MUNICIPIO DE SALTO DA DIVISA|MG": ("3157104", "Salto da Divisa"),
    "MUNICIPIO DE SALTO DE PIRAPORA|SP": ("3545308", "Salto de Pirapora"),
    "MUNICIPIO DE SALTO DO ITARARE|PR": ("4122909", "Salto do Itararé"),
    "MUNICIPIO DE SALTO DO JACUI|RS": ("4316451", "Salto do Jacuí"),
    "MUNICIPIO DE SALTO DO LONTRA|PR": ("4123006", "Salto do Lontra"),
    "MUNICIPIO DE SALTO GRANDE|SP": ("3545407", "Salto Grande"),
    "MUNICIPIO DE SALTO VELOSO|SC": ("4215406", "Salto Veloso"),
    "MUNICIPIO DE SALTO|SP": ("3545209", "Salto"),
    "MUNICIPIO DE SALVADOR DAS MISSOES|RS": ("4316477", "Salvador das Missões"),
    "MUNICIPIO DE SALVADOR DO SUL|RS": ("4316501", "Salvador do Sul"),
    "MUNICIPIO DE SALVADOR|BA": ("2927408", "Salvador"),
    "MUNICIPIO DE SALVATERRA|PA": ("1506302", "Salvaterra"),
    "MUNICIPIO DE SAMBAIBA|MA": ("2109700", "Sambaíba"),
    "MUNICIPIO DE SAMPAIO|TO": ("1718808", "Sampaio"),
    "MUNICIPIO DE SANANDUVA|RS": ("4316600", "Sananduva"),
    "MUNICIPIO DE SANCLERLANDIA|GO": ("5219001", "Sanclerlândia"),
    "MUNICIPIO DE SANDOLANDIA|TO": ("1718840", "Sandolândia"),
    "MUNICIPIO DE SANDOVALINA|SP": ("3545506", "Sandovalina"),
    "MUNICIPIO DE SANGAO|SC": ("4215455", "Sangão"),
    "MUNICIPIO DE SANHARO|PE": ("2612406", "Sanharó"),
    "MUNICIPIO DE SANTA ADELIA|SP": ("3545605", "Santa Adélia"),
    "MUNICIPIO DE SANTA ALBERTINA|SP": ("3545704", "Santa Albertina"),
    "MUNICIPIO DE SANTA AMELIA|PR": ("4123105", "Santa Amélia"),
    "MUNICIPIO DE SANTA BARBARA D'OESTE|SP": ("3545803", "Santa Bárbara d'Oeste"),
    "MUNICIPIO DE SANTA BARBARA DE GOIAS|GO": ("5219100", "Santa Bárbara de Goiás"),
    "MUNICIPIO DE SANTA BARBARA DO LESTE|MG": ("3157252", "Santa Bárbara do Leste"),
    "MUNICIPIO DE SANTA BARBARA DO MONTE VERDE|MG": ("3157278", "Santa Bárbara do Monte Verde"),
    "MUNICIPIO DE SANTA BARBARA DO PARA|PA": ("1506351", "Santa Bárbara do Pará"),
    "MUNICIPIO DE SANTA BARBARA DO SUL|RS": ("4316709", "Santa Bárbara do Sul"),
    "MUNICIPIO DE SANTA BARBARA|BA": ("2927507", "Santa Bárbara"),
    "MUNICIPIO DE SANTA BARBARA|MG": ("3157203", "Santa Bárbara"),
    "MUNICIPIO DE SANTA BRIGIDA|BA": ("2927606", "Santa Brígida"),
    "MUNICIPIO DE SANTA CECILIA DO PAVAO|PR": ("4123204", "Santa Cecília do Pavão"),
    "MUNICIPIO DE SANTA CECILIA DO SUL|RS": ("4316733", "Santa Cecília do Sul"),
    "MUNICIPIO DE SANTA CECILIA|PB": ("2513158", "Santa Cecília"),
    "MUNICIPIO DE SANTA CECILIA|SC": ("4215505", "Santa Cecília"),
    "MUNICIPIO DE SANTA CLARA D'OESTE|SP": ("3546108", "Santa Clara d'Oeste"),
    "MUNICIPIO DE SANTA CRUZ CABRALIA|BA": ("2927705", "Santa Cruz Cabrália"),
    "MUNICIPIO DE SANTA CRUZ DA BAIXA VERDE|PE": ("2612471", "Santa Cruz da Baixa Verde"),
    "MUNICIPIO DE SANTA CRUZ DA CONCEICAO|SP": ("3546207", "Santa Cruz da Conceição"),
    "MUNICIPIO DE SANTA CRUZ DAS PALMEIRAS|SP": ("3546306", "Santa Cruz das Palmeiras"),
    "MUNICIPIO DE SANTA CRUZ DE GOIAS|GO": ("5219209", "Santa Cruz de Goiás"),
    "MUNICIPIO DE SANTA CRUZ DE MINAS|MG": ("3157336", "Santa Cruz de Minas"),
    "MUNICIPIO DE SANTA CRUZ DE SALINAS|MG": ("3157377", "Santa Cruz de Salinas"),
    "MUNICIPIO DE SANTA CRUZ DO ESCALVADO|MG": ("3157401", "Santa Cruz do Escalvado"),
    "MUNICIPIO DE SANTA CRUZ DO MONTE CASTELO|PR": ("4123303", "Santa Cruz de Monte Castelo"),
    "MUNICIPIO DE SANTA CRUZ DO PIAUI|PI": ("2209104", "Santa Cruz do Piauí"),
    "MUNICIPIO DE SANTA CRUZ DO RIO PARDO|SP": ("3546405", "Santa Cruz do Rio Pardo"),
    "MUNICIPIO DE SANTA CRUZ DO SUL|RS": ("4316808", "Santa Cruz do Sul"),
    "MUNICIPIO DE SANTA CRUZ DOS MILAGRES|PI": ("2209153", "Santa Cruz dos Milagres"),
    "MUNICIPIO DE SANTA CRUZ|PB": ("2513208", "Santa Cruz"),
    "MUNICIPIO DE SANTA CRUZ|PE": ("2612455", "Santa Cruz"),
    "MUNICIPIO DE SANTA CRUZ|RN": ("2411205", "Santa Cruz"),
    "MUNICIPIO DE SANTA EFIGENIA DE MINAS|MG": ("3157500", "Santa Efigênia de Minas"),
    "MUNICIPIO DE SANTA ERNESTINA|SP": ("3546504", "Santa Ernestina"),
    "MUNICIPIO DE SANTA FE DE GOIAS|GO": ("5219258", "Santa Fé de Goiás"),
    "MUNICIPIO DE SANTA FE DE MINAS|MG": ("3157609", "Santa Fé de Minas"),
    "MUNICIPIO DE SANTA FE DO ARAGUAIA|TO": ("1718865", "Santa Fé do Araguaia"),
    "MUNICIPIO DE SANTA FE DO SUL|SP": ("3546603", "Santa Fé do Sul"),
    "MUNICIPIO DE SANTA FE|PR": ("4123402", "Santa Fé"),
    "MUNICIPIO DE SANTA FILOMENA|PE": ("2612554", "Santa Filomena"),
    "MUNICIPIO DE SANTA FILOMENA|PI": ("2209203", "Santa Filomena"),
    "MUNICIPIO DE SANTA GERTRUDES|SP": ("3546702", "Santa Gertrudes"),
    "MUNICIPIO DE SANTA HELENA DE GOIAS|GO": ("5219308", "Santa Helena de Goiás"),
    "MUNICIPIO DE SANTA HELENA DE MINAS|MG": ("3157658", "Santa Helena de Minas"),
    "MUNICIPIO DE SANTA HELENA|MA": ("2109809", "Santa Helena"),
    "MUNICIPIO DE SANTA HELENA|PB": ("2513307", "Santa Helena"),
    "MUNICIPIO DE SANTA HELENA|PR": ("4123501", "Santa Helena"),
    "MUNICIPIO DE SANTA HELENA|SC": ("4215554", "Santa Helena"),
    "MUNICIPIO DE SANTA INES|BA": ("2927903", "Santa Inês"),
    "MUNICIPIO DE SANTA INES|MA": ("2109908", "Santa Inês"),
    "MUNICIPIO DE SANTA INES|PB": ("2513356", "Santa Inês"),
    "MUNICIPIO DE SANTA INES|PR": ("4123600", "Santa Inês"),
    "MUNICIPIO DE SANTA ISABEL DO IVAI|PR": ("4123709", "Santa Isabel do Ivaí"),
    "MUNICIPIO DE SANTA ISABEL|GO": ("5219357", "Santa Isabel"),
    "MUNICIPIO DE SANTA ISABEL|SP": ("3546801", "Santa Isabel"),
    "MUNICIPIO DE SANTA IZABEL DO OESTE|PR": ("4123808", "Santa Izabel do Oeste"),
    "MUNICIPIO DE SANTA JULIANA|MG": ("3157708", "Santa Juliana"),
    "MUNICIPIO DE SANTA LEOPOLDINA|ES": ("3204500", "Santa Leopoldina"),
    "MUNICIPIO DE SANTA LUCIA|PR": ("4123824", "Santa Lúcia"),
    "MUNICIPIO DE SANTA LUCIA|SP": ("3546900", "Santa Lúcia"),
    "MUNICIPIO DE SANTA LUZIA D'OESTE|RO": ("1100296", "Santa Luzia D'Oeste"),
    "MUNICIPIO DE SANTA LUZIA DO ITANHY|SE": ("2806305", "Santa Luzia do Itanhy"),
    "MUNICIPIO DE SANTA LUZIA DO PARA|PA": ("1506559", "Santa Luzia do Pará"),
    "MUNICIPIO DE SANTA LUZIA|BA": ("2928059", "Santa Luzia"),
    "MUNICIPIO DE SANTA LUZIA|MA": ("2110005", "Santa Luzia"),
    "MUNICIPIO DE SANTA LUZIA|MG": ("3157807", "Santa Luzia"),
    "MUNICIPIO DE SANTA LUZIA|PB": ("2513406", "Santa Luzia"),
    "MUNICIPIO DE SANTA LUZ|PI": ("2209302", "Santa Luz"),
    "MUNICIPIO DE SANTA MARGARIDA DO SUL|RS": ("4316972", "Santa Margarida do Sul"),
    "MUNICIPIO DE SANTA MARGARIDA|MG": ("3157906", "Santa Margarida"),
    "MUNICIPIO DE SANTA MARIA DA BOA VISTA|PE": ("2612604", "Santa Maria da Boa Vista"),
    "MUNICIPIO DE SANTA MARIA DA SERRA|SP": ("3547007", "Santa Maria da Serra"),
    "MUNICIPIO DE SANTA MARIA DA VITORIA|BA": ("2928109", "Santa Maria da Vitória"),
    "MUNICIPIO DE SANTA MARIA DAS BARREIRAS|PA": ("1506583", "Santa Maria das Barreiras"),
    "MUNICIPIO DE SANTA MARIA DE ITABIRA|MG": ("3158003", "Santa Maria de Itabira"),
    "MUNICIPIO DE SANTA MARIA DE JETIBA|ES": ("3204559", "Santa Maria de Jetibá"),
    "MUNICIPIO DE SANTA MARIA DO CAMBUCA|PE": ("2612703", "Santa Maria do Cambucá"),
    "MUNICIPIO DE SANTA MARIA DO HERVAL|RS": ("4316956", "Santa Maria do Herval"),
    "MUNICIPIO DE SANTA MARIA DO OESTE|PR": ("4123857", "Santa Maria do Oeste"),
    "MUNICIPIO DE SANTA MARIA DO PARA|PA": ("1506609", "Santa Maria do Pará"),
    "MUNICIPIO DE SANTA MARIA DO SALTO|MG": ("3158102", "Santa Maria do Salto"),
    "MUNICIPIO DE SANTA MARIA DO SUACUI|MG": ("3158201", "Santa Maria do Suaçuí"),
    "MUNICIPIO DE SANTA MARIA DO TOCANTINS|TO": ("1718881", "Santa Maria do Tocantins"),
    "MUNICIPIO DE SANTA MARIA MADALENA|RJ": ("3304607", "Santa Maria Madalena"),
    "MUNICIPIO DE SANTA MARIANA|PR": ("4123907", "Santa Mariana"),
    "MUNICIPIO DE SANTA MARIA|RN": ("2409332", "Santa Maria"),
    "MUNICIPIO DE SANTA MARIA|RS": ("4316907", "Santa Maria"),
    "MUNICIPIO DE SANTA QUITERIA DO MARANHAO|MA": ("2110104", "Santa Quitéria do Maranhão"),
    "MUNICIPIO DE SANTA QUITERIA|CE": ("2312205", "Santa Quitéria"),
    "MUNICIPIO DE SANTA RITA D'OESTE|SP": ("3547403", "Santa Rita d'Oeste"),
    "MUNICIPIO DE SANTA RITA DE CALDAS|MG": ("3159209", "Santa Rita de Caldas"),
    "MUNICIPIO DE SANTA RITA DE CASSIA|BA": ("2928406", "Santa Rita de Cássia"),
    "MUNICIPIO DE SANTA RITA DE IBITIPOCA|MG": ("3159407", "Santa Rita de Ibitipoca"),
    "MUNICIPIO DE SANTA RITA DE JACUTINGA|MG": ("3159308", "Santa Rita de Jacutinga"),
    "MUNICIPIO DE SANTA RITA DE MINAS|MG": ("3159357", "Santa Rita de Minas"),
    "MUNICIPIO DE SANTA RITA DO ARAGUAIA|GO": ("5219407", "Santa Rita do Araguaia"),
    "MUNICIPIO DE SANTA RITA DO NOVO DESTINO|GO": ("5219456", "Santa Rita do Novo Destino"),
    "MUNICIPIO DE SANTA RITA DO PARDO|MS": ("5007554", "Santa Rita do Pardo"),
    "MUNICIPIO DE SANTA RITA DO PASSA QUATRO|SP": ("3547502", "Santa Rita do Passa Quatro"),
    "MUNICIPIO DE SANTA RITA DO SAPUCAI|MG": ("3159605", "Santa Rita do Sapucaí"),
    "MUNICIPIO DE SANTA RITA DO TOCANTINS|TO": ("1718899", "Santa Rita do Tocantins"),
    "MUNICIPIO DE SANTA RITA|MA": ("2110203", "Santa Rita"),
    "MUNICIPIO DE SANTA RITA|PB": ("2513703", "Santa Rita"),
    "MUNICIPIO DE SANTA ROSA DA SERRA|MG": ("3159704", "Santa Rosa da Serra"),
    "MUNICIPIO DE SANTA ROSA DE GOIAS|GO": ("5219506", "Santa Rosa de Goiás"),
    "MUNICIPIO DE SANTA ROSA DE LIMA|SC": ("4215604", "Santa Rosa de Lima"),
    "MUNICIPIO DE SANTA ROSA DE LIMA|SE": ("2806503", "Santa Rosa de Lima"),
    "MUNICIPIO DE SANTA ROSA DE VITERBO|SP": ("3547601", "Santa Rosa de Viterbo"),
    "MUNICIPIO DE SANTA ROSA DO PURUS|AC": ("1200435", "Santa Rosa do Purus"),
    "MUNICIPIO DE SANTA ROSA DO SUL|SC": ("4215653", "Santa Rosa do Sul"),
    "MUNICIPIO DE SANTA ROSA DO TOCANTINS|TO": ("1718907", "Santa Rosa do Tocantins"),
    "MUNICIPIO DE SANTA ROSA|RS": ("4317202", "Santa Rosa"),
    "MUNICIPIO DE SANTA SALETE|SP": ("3547650", "Santa Salete"),
    "MUNICIPIO DE SANTA TERESINHA|BA": ("2928505", "Santa Terezinha"),
    "MUNICIPIO DE SANTA TERESINHA|MT": ("5107776", "Santa Terezinha"),
    "MUNICIPIO DE SANTA TERESINHA|PE": ("2612802", "Santa Terezinha"),
    "MUNICIPIO DE SANTA TERESINHA|SC": ("4215679", "Santa Terezinha"),
    "MUNICIPIO DE SANTA TEREZA DE GOIAS|GO": ("5219605", "Santa Tereza de Goiás"),
    "MUNICIPIO DE SANTA TEREZA DO TOCANTINS|TO": ("1719004", "Santa Tereza do Tocantins"),
    "MUNICIPIO DE SANTA TEREZA|RS": ("4317251", "Santa Tereza"),
    "MUNICIPIO DE SANTA TEREZINHA DE GOIAS|GO": ("5219704", "Santa Terezinha de Goiás"),
    "MUNICIPIO DE SANTA TEREZINHA DO PROGRESSO|SC": ("4215687", "Santa Terezinha do Progresso"),
    "MUNICIPIO DE SANTA TEREZINHA DO TOCANTINS|TO": ("1720002", "Santa Terezinha do Tocantins"),
    "MUNICIPIO DE SANTA TEREZINHA|BA": ("2928505", "Santa Terezinha"),
    "MUNICIPIO DE SANTA TEREZINHA|MT": ("5107776", "Santa Terezinha"),
    "MUNICIPIO DE SANTA TEREZINHA|PE": ("2612802", "Santa Terezinha"),
    "MUNICIPIO DE SANTA TEREZINHA|SC": ("4215679", "Santa Terezinha"),
    "MUNICIPIO DE SANTA VITORIA DO PALMAR|RS": ("4317301", "Santa Vitória do Palmar"),
    "MUNICIPIO DE SANTA VITORIA|MG": ("3159803", "Santa Vitória"),
    "MUNICIPIO DE SANTALUZ|BA": ("2928000", "Santaluz"),
    "MUNICIPIO DE SANTANA DA BOA VISTA|RS": ("4317004", "Santana da Boa Vista"),
    "MUNICIPIO DE SANTANA DA PONTE PENSA|SP": ("3547205", "Santana da Ponte Pensa"),
    "MUNICIPIO DE SANTANA DA VARGEM|MG": ("3158300", "Santana da Vargem"),
    "MUNICIPIO DE SANTANA DE CATAGUASES|MG": ("3158409", "Santana de Cataguases"),
    "MUNICIPIO DE SANTANA DE PARNAIBA|SP": ("3547304", "Santana de Parnaíba"),
    "MUNICIPIO DE SANTANA DE PIRAPAMA|MG": ("3158508", "Santana de Pirapama"),
    "MUNICIPIO DE SANTANA DO ACARAU|CE": ("2312007", "Santana do Acaraú"),
    "MUNICIPIO DE SANTANA DO ARAGUAIA|PA": ("1506708", "Santana do Araguaia"),
    "MUNICIPIO DE SANTANA DO DESERTO|MG": ("3158607", "Santana do Deserto"),
    "MUNICIPIO DE SANTANA DO GARAMBEU|MG": ("3158706", "Santana do Garambéu"),
    "MUNICIPIO DE SANTANA DO IPANEMA|AL": ("2708006", "Santana do Ipanema"),
    "MUNICIPIO DE SANTANA DO ITARARE|PR": ("4124004", "Santana do Itararé"),
    "MUNICIPIO DE SANTANA DO JACARE|MG": ("3158805", "Santana do Jacaré"),
    "MUNICIPIO DE SANTANA DO LIVRAMENTO|RS": ("4317103", "Sant'Ana do Livramento"),
    "MUNICIPIO DE SANTANA DO MANHUACU|MG": ("3158904", "Santana do Manhuaçu"),
    "MUNICIPIO DE SANTANA DO MARANHAO|MA": ("2110237", "Santana do Maranhão"),
    "MUNICIPIO DE SANTANA DO MATOS|RN": ("2411403", "Santana do Matos"),
    "MUNICIPIO DE SANTANA DO PARAISO|MG": ("3158953", "Santana do Paraíso"),
    "MUNICIPIO DE SANTANA DO PIAUI|PI": ("2209351", "Santana do Piauí"),
    "MUNICIPIO DE SANTANA DO RIACHO|MG": ("3159001", "Santana do Riacho"),
    "MUNICIPIO DE SANTANA DO SAO FRANCISCO|SE": ("2806404", "Santana do São Francisco"),
    "MUNICIPIO DE SANTANA DO SERIDO|RN": ("2411429", "Santana do Seridó"),
    "MUNICIPIO DE SANTANA DOS MONTES|MG": ("3159100", "Santana dos Montes"),
    "MUNICIPIO DE SANTANA|AP": ("1600600", "Santana"),
    "MUNICIPIO DE SANTANA|BA": ("2928208", "Santana"),
    "MUNICIPIO DE SANTANOPOLIS|BA": ("2928307", "Santanópolis"),
    "MUNICIPIO DE SANTAREM NOVO|PA": ("1506906", "Santarém Novo"),
    "MUNICIPIO DE SANTAREM|PA": ("1506807", "Santarém"),
    "MUNICIPIO DE SANTIAGO DO SUL|SC": ("4215695", "Santiago do Sul"),
    "MUNICIPIO DE SANTIAGO|RS": ("4317400", "Santiago"),
    "MUNICIPIO DE SANTO AMARO DA IMPERATRIZ|SC": ("4215703", "Santo Amaro da Imperatriz"),
    "MUNICIPIO DE SANTO AMARO DAS BROTAS|SE": ("2806602", "Santo Amaro das Brotas"),
    "MUNICIPIO DE SANTO AMARO|BA": ("2928604", "Santo Amaro"),
    "MUNICIPIO DE SANTO ANASTACIO|SP": ("3547700", "Santo Anastácio"),
    "MUNICIPIO DE SANTO ANDRE|PB": ("2513851", "Santo André"),
    "MUNICIPIO DE SANTO ANDRE|SP": ("3547809", "Santo André"),
    "MUNICIPIO DE SANTO ANGELO|RS": ("4317509", "Santo Ângelo"),
    "MUNICIPIO DE SANTO ANTONIO DA ALEGRIA|SP": ("3547908", "Santo Antônio da Alegria"),
    "MUNICIPIO DE SANTO ANTONIO DA BARRA|GO": ("5219712", "Santo Antônio da Barra"),
    "MUNICIPIO DE SANTO ANTONIO DA PATRULHA|RS": ("4317608", "Santo Antônio da Patrulha"),
    "MUNICIPIO DE SANTO ANTONIO DA PLATINA|PR": ("4124103", "Santo Antônio da Platina"),
    "MUNICIPIO DE SANTO ANTONIO DAS MISSOES|RS": ("4317707", "Santo Antônio das Missões"),
    "MUNICIPIO DE SANTO ANTONIO DE GOIAS|GO": ("5219738", "Santo Antônio de Goiás"),
    "MUNICIPIO DE SANTO ANTONIO DE JESUS|BA": ("2928703", "Santo Antônio de Jesus"),
    "MUNICIPIO DE SANTO ANTONIO DE PADUA|RJ": ("3304706", "Santo Antônio de Pádua"),
    "MUNICIPIO DE SANTO ANTONIO DE POSSE|SP": ("3548005", "Santo Antônio de Posse"),
    "MUNICIPIO DE SANTO ANTONIO DO AMPARO|MG": ("3159902", "Santo Antônio do Amparo"),
    "MUNICIPIO DE SANTO ANTONIO DO ARACANGUA|SP": ("3548054", "Santo Antônio do Aracanguá"),
    "MUNICIPIO DE SANTO ANTONIO DO AVENTUREIRO|MG": ("3160009", "Santo Antônio do Aventureiro"),
    "MUNICIPIO DE SANTO ANTONIO DO CAIUA|PR": ("4124202", "Santo Antônio do Caiuá"),
    "MUNICIPIO DE SANTO ANTONIO DO DESCOBERTO|GO": ("5219753", "Santo Antônio do Descoberto"),
    "MUNICIPIO DE SANTO ANTONIO DO GRAMA|MG": ("3160108", "Santo Antônio do Grama"),
    "MUNICIPIO DE SANTO ANTONIO DO ICA|AM": ("1303700", "Santo Antônio do Içá"),
    "MUNICIPIO DE SANTO ANTONIO DO JACINTO|MG": ("3160306", "Santo Antônio do Jacinto"),
    "MUNICIPIO DE SANTO ANTONIO DO JARDIM|SP": ("3548104", "Santo Antônio do Jardim"),
    "MUNICIPIO DE SANTO ANTONIO DO LESTE|MT": ("5107792", "Santo Antônio do Leste"),
    "MUNICIPIO DE SANTO ANTONIO DO LEVERGER|MT": ("5107800", "Santo Antônio do Leverger"),
    "MUNICIPIO DE SANTO ANTONIO DO MONTE|MG": ("3160405", "Santo Antônio do Monte"),
    "MUNICIPIO DE SANTO ANTONIO DO PALMA|RS": ("4317558", "Santo Antônio do Palma"),
    "MUNICIPIO DE SANTO ANTONIO DO PARAISO|PR": ("4124301", "Santo Antônio do Paraíso"),
    "MUNICIPIO DE SANTO ANTONIO DO PINHAL|SP": ("3548203", "Santo Antônio do Pinhal"),
    "MUNICIPIO DE SANTO ANTONIO DO PLANALTO|RS": ("4317756", "Santo Antônio do Planalto"),
    "MUNICIPIO DE SANTO ANTONIO DO RETIRO|MG": ("3160454", "Santo Antônio do Retiro"),
    "MUNICIPIO DE SANTO ANTONIO DO RIO ABAIXO|MG": ("3160504", "Santo Antônio do Rio Abaixo"),
    "MUNICIPIO DE SANTO ANTONIO DO SUDOESTE|PR": ("4124400", "Santo Antônio do Sudoeste"),
    "MUNICIPIO DE SANTO ANTONIO DO TAUA|PA": ("1507003", "Santo Antônio do Tauá"),
    "MUNICIPIO DE SANTO ANTONIO|RN": ("2411502", "Santo Antônio"),
    "MUNICIPIO DE SANTO AUGUSTO|RS": ("4317806", "Santo Augusto"),
    "MUNICIPIO DE SANTO CRISTO|RS": ("4317905", "Santo Cristo"),
    "MUNICIPIO DE SANTO EXPEDITO DO SUL|RS": ("4317954", "Santo Expedito do Sul"),
    "MUNICIPIO DE SANTO HIPOLITO|MG": ("3160603", "Santo Hipólito"),
    "MUNICIPIO DE SANTO INACIO DO PIAUI|PI": ("2209500", "Santo Inácio do Piauí"),
    "MUNICIPIO DE SANTO INACIO|PR": ("4124509", "Santo Inácio"),
    "MUNICIPIO DE SANTOPOLIS DO AGUAPEI|SP": ("3548401", "Santópolis do Aguapeí"),
    "MUNICIPIO DE SANTOS DUMONT|MG": ("3160702", "Santos Dumont"),
    "MUNICIPIO DE SANTOS|SP": ("3548500", "Santos"),
    "MUNICIPIO DE SAO BENEDITO DO SUL|PE": ("2612901", "São Benedito do Sul"),
    "MUNICIPIO DE SAO BENEDITO|CE": ("2312304", "São Benedito"),
    "MUNICIPIO DE SAO BENTINHO|PB": ("2513927", "São Bentinho"),
    "MUNICIPIO DE SAO BENTO ABADE|MG": ("3160801", "São Bento Abade"),
    "MUNICIPIO DE SAO BENTO DO SAPUCAI|SP": ("3548609", "São Bento do Sapucaí"),
    "MUNICIPIO DE SAO BENTO DO SUL|SC": ("4215802", "São Bento do Sul"),
    "MUNICIPIO DE SAO BENTO DO TOCANTINS|TO": ("1720101", "São Bento do Tocantins"),
    "MUNICIPIO DE SAO BENTO DO UNA|PE": ("2613008", "São Bento do Una"),
    "MUNICIPIO DE SAO BENTO|MA": ("2110500", "São Bento"),
    "MUNICIPIO DE SAO BENTO|PB": ("2513901", "São Bento"),
    "MUNICIPIO DE SAO BERNARDINO|SC": ("4215752", "São Bernardino"),
    "MUNICIPIO DE SAO BERNARDO DO CAMPO|SP": ("3548708", "São Bernardo do Campo"),
    "MUNICIPIO DE SAO BERNARDO|MA": ("2110609", "São Bernardo"),
    "MUNICIPIO DE SAO BONIFACIO|SC": ("4215901", "São Bonifácio"),
    "MUNICIPIO DE SAO BORJA|RS": ("4318002", "São Borja"),
    "MUNICIPIO DE SAO BRAS DO SUACUI|MG": ("3160900", "São Brás do Suaçuí"),
    "MUNICIPIO DE SAO BRAS|AL": ("2708204", "São Brás"),
    "MUNICIPIO DE SAO BRAZ DO PIAUI|PI": ("2209559", "São Braz do Piauí"),
    "MUNICIPIO DE SAO CAETANO DO SUL|SP": ("3548807", "São Caetano do Sul"),
    "MUNICIPIO DE SAO CAITANO|PE": ("2613107", "São Caitano"),
    "MUNICIPIO DE SAO CARLOS DO IVAI|PR": ("4124608", "São Carlos do Ivaí"),
    "MUNICIPIO DE SAO CARLOS|SC": ("4216008", "São Carlos"),
    "MUNICIPIO DE SAO CARLOS|SP": ("3548906", "São Carlos"),
    "MUNICIPIO DE SAO CRISTOVAO DO SUL|SC": ("4216057", "São Cristóvão do Sul"),
    "MUNICIPIO DE SAO CRISTOVAO|SE": ("2806701", "São Cristóvão"),
    "MUNICIPIO DE SAO DESIDERIO|BA": ("2928901", "São Desidério"),
    "MUNICIPIO DE SAO DOMINGOS DAS DORES|MG": ("3160959", "São Domingos das Dores"),
    "MUNICIPIO DE SAO DOMINGOS DO ARAGUAIA|PA": ("1507151", "São Domingos do Araguaia"),
    "MUNICIPIO DE SAO DOMINGOS DO CAPIM|PA": ("1507201", "São Domingos do Capim"),
    "MUNICIPIO DE SAO DOMINGOS DO MARANHAO|MA": ("2110708", "São Domingos do Maranhão"),
    "MUNICIPIO DE SAO DOMINGOS DO PRATA|MG": ("3161007", "São Domingos do Prata"),
    "MUNICIPIO DE SAO DOMINGOS|BA": ("2928950", "São Domingos"),
    "MUNICIPIO DE SAO DOMINGOS|GO": ("5219803", "São Domingos"),
    "MUNICIPIO DE SAO DOMINGOS|PB": ("2513968", "São Domingos"),
    "MUNICIPIO DE SAO DOMINGOS|SC": ("4216107", "São Domingos"),
    "MUNICIPIO DE SAO DOMINGOS|SE": ("2806800", "São Domingos"),
    "MUNICIPIO DE SAO FELIPE D'OESTE|RO": ("1101484", "São Felipe D'Oeste"),
    "MUNICIPIO DE SAO FELIPE|BA": ("2929107", "São Felipe"),
    "MUNICIPIO DE SAO FELIX DE BALSAS|MA": ("2110807", "São Félix de Balsas"),
    "MUNICIPIO DE SAO FELIX DE MINAS|MG": ("3161056", "São Félix de Minas"),
    "MUNICIPIO DE SAO FELIX DO ARAGUAIA|MT": ("5107859", "São Félix do Araguaia"),
    "MUNICIPIO DE SAO FELIX DO CORIBE|BA": ("2929057", "São Félix do Coribe"),
    "MUNICIPIO DE SAO FELIX DO PIAUI|PI": ("2209609", "São Félix do Piauí"),
    "MUNICIPIO DE SAO FELIX DO TOCANTINS|TO": ("1720150", "São Félix do Tocantins"),
    "MUNICIPIO DE SAO FELIX DO XINGU|PA": ("1507300", "São Félix do Xingu"),
    "MUNICIPIO DE SAO FELIX|BA": ("2929008", "São Félix"),
    "MUNICIPIO DE SAO FERNANDO|RN": ("2411809", "São Fernando"),
    "MUNICIPIO DE SAO FIDELIS|RJ": ("3304805", "São Fidélis"),
    "MUNICIPIO DE SAO FRANCISCO DE ASSIS DO PIAUI|PI": ("2209658", "São Francisco de Assis do Piauí"),
    "MUNICIPIO DE SAO FRANCISCO DE ASSIS|RS": ("4318101", "São Francisco de Assis"),
    "MUNICIPIO DE SAO FRANCISCO DE GOIAS|GO": ("5219902", "São Francisco de Goiás"),
    "MUNICIPIO DE SAO FRANCISCO DE PAULA|MG": ("3161205", "São Francisco de Paula"),
    "MUNICIPIO DE SAO FRANCISCO DE PAULA|RS": ("4318200", "São Francisco de Paula"),
    "MUNICIPIO DE SAO FRANCISCO DE SALES|MG": ("3161304", "São Francisco de Sales"),
    "MUNICIPIO DE SAO FRANCISCO DO GLORIA|MG": ("3161403", "São Francisco do Glória"),
    "MUNICIPIO DE SAO FRANCISCO DO GUAPORE|RO": ("1101492", "São Francisco do Guaporé"),
    "MUNICIPIO DE SAO FRANCISCO DO OESTE|RN": ("2411908", "São Francisco do Oeste"),
    "MUNICIPIO DE SAO FRANCISCO DO PIAUI|PI": ("2209708", "São Francisco do Piauí"),
    "MUNICIPIO DE SAO FRANCISCO DO SUL|SC": ("4216206", "São Francisco do Sul"),
    "MUNICIPIO DE SAO FRANCISCO|MG": ("3161106", "São Francisco"),
    "MUNICIPIO DE SAO FRANCISCO|PB": ("2513984", "São Francisco"),
    "MUNICIPIO DE SAO FRANCISCO|SE": ("2806909", "São Francisco"),
    "MUNICIPIO DE SAO FRANCISCO|SP": ("3549003", "São Francisco"),
    "MUNICIPIO DE SAO GABRIEL DA PALHA|ES": ("3204708", "São Gabriel da Palha"),
    "MUNICIPIO DE SAO GABRIEL DO OESTE|MS": ("5007695", "São Gabriel do Oeste"),
    "MUNICIPIO DE SAO GABRIEL|BA": ("2929255", "São Gabriel"),
    "MUNICIPIO DE SAO GABRIEL|RS": ("4318309", "São Gabriel"),
    "MUNICIPIO DE SAO GERALDO DA PIEDADE|MG": ("3161601", "São Geraldo da Piedade"),
    "MUNICIPIO DE SAO GERALDO DO ARAGUAIA|PA": ("1507458", "São Geraldo do Araguaia"),
    "MUNICIPIO DE SAO GERALDO DO BAIXIO|MG": ("3161650", "São Geraldo do Baixio"),
    "MUNICIPIO DE SAO GERALDO|MG": ("3161502", "São Geraldo"),
    "MUNICIPIO DE SAO GONCALO DO ABAETE|MG": ("3161700", "São Gonçalo do Abaeté"),
    "MUNICIPIO DE SAO GONCALO DO AMARANTE|CE": ("2312403", "São Gonçalo do Amarante"),
    "MUNICIPIO DE SAO GONCALO DO AMARANTE|RN": ("2412005", "São Gonçalo do Amarante"),
    "MUNICIPIO DE SAO GONCALO DO GURGUEIA|PI": ("2209757", "São Gonçalo do Gurguéia"),
    "MUNICIPIO DE SAO GONCALO DO PARA|MG": ("3161809", "São Gonçalo do Pará"),
    "MUNICIPIO DE SAO GONCALO DO PIAUI|PI": ("2209807", "São Gonçalo do Piauí"),
    "MUNICIPIO DE SAO GONCALO DO RIO ABAIXO|MG": ("3161908", "São Gonçalo do Rio Abaixo"),
    "MUNICIPIO DE SAO GONCALO DO RIO PRETO|MG": ("3125507", "São Gonçalo do Rio Preto"),
    "MUNICIPIO DE SAO GONCALO DO SAPUCAI|MG": ("3162005", "São Gonçalo do Sapucaí"),
    "MUNICIPIO DE SAO GONCALO DOS CAMPOS|BA": ("2929305", "São Gonçalo dos Campos"),
    "MUNICIPIO DE SAO GONCALO|RJ": ("3304904", "São Gonçalo"),
    "MUNICIPIO DE SAO GOTARDO|MG": ("3162104", "São Gotardo"),
    "MUNICIPIO DE SAO JERONIMO DA SERRA|PR": ("4124707", "São Jerônimo da Serra"),
    "MUNICIPIO DE SAO JERONIMO|RS": ("4318408", "São Jerônimo"),
    "MUNICIPIO DE SAO JOAO BATISTA|MA": ("2111003", "São João Batista"),
    "MUNICIPIO DE SAO JOAO BATISTA|SC": ("4216305", "São João Batista"),
    "MUNICIPIO DE SAO JOAO D'ALIANCA|GO": ("5220009", "São João d'Aliança"),
    "MUNICIPIO DE SAO JOAO DA BALIZA|RR": ("1400506", "São João da Baliza"),
    "MUNICIPIO DE SAO JOAO DA BARRA|RJ": ("3305000", "São João da Barra"),
    "MUNICIPIO DE SAO JOAO DA BOA VISTA|SP": ("3549102", "São João da Boa Vista"),
    "MUNICIPIO DE SAO JOAO DA CANABRAVA|PI": ("2209856", "São João da Canabrava"),
    "MUNICIPIO DE SAO JOAO DA FRONTEIRA|PI": ("2209872", "São João da Fronteira"),
    "MUNICIPIO DE SAO JOAO DA LAGOA|MG": ("3162252", "São João da Lagoa"),
    "MUNICIPIO DE SAO JOAO DA MATA|MG": ("3162302", "São João da Mata"),
    "MUNICIPIO DE SAO JOAO DA PARAUNA|GO": ("5220058", "São João da Paraúna"),
    "MUNICIPIO DE SAO JOAO DA PONTA|PA": ("1507466", "São João da Ponta"),
    "MUNICIPIO DE SAO JOAO DA PONTE|MG": ("3162401", "São João da Ponte"),
    "MUNICIPIO DE SAO JOAO DA URTIGA|RS": ("4318424", "São João da Urtiga"),
    "MUNICIPIO DE SAO JOAO DAS DUAS PONTES|SP": ("3549201", "São João das Duas Pontes"),
    "MUNICIPIO DE SAO JOAO DAS MISSOES|MG": ("3162450", "São João das Missões"),
    "MUNICIPIO DE SAO JOAO DE MERITI|RJ": ("3305109", "São João de Meriti"),
    "MUNICIPIO DE SAO JOAO DEL REI|MG": ("3162500", "São João del Rei"),
    "MUNICIPIO DE SAO JOAO DO ARAGUAIA|PA": ("1507508", "São João do Araguaia"),
    "MUNICIPIO DE SAO JOAO DO ARRAIAL|PI": ("2209971", "São João do Arraial"),
    "MUNICIPIO DE SAO JOAO DO CAIUA|PR": ("4124905", "São João do Caiuá"),
    "MUNICIPIO DE SAO JOAO DO CARU|MA": ("2111029", "São João do Carú"),
    "MUNICIPIO DE SAO JOAO DO ITAPERIU|SC": ("4216354", "São João do Itaperiú"),
    "MUNICIPIO DE SAO JOAO DO IVAI|PR": ("4125001", "São João do Ivaí"),
    "MUNICIPIO DE SAO JOAO DO MANHUACU|MG": ("3162559", "São João do Manhuaçu"),
    "MUNICIPIO DE SAO JOAO DO MANTENINHA|MG": ("3162575", "São João do Manteninha"),
    "MUNICIPIO DE SAO JOAO DO OESTE|SC": ("4216255", "São João do Oeste"),
    "MUNICIPIO DE SAO JOAO DO ORIENTE|MG": ("3162609", "São João do Oriente"),
    "MUNICIPIO DE SAO JOAO DO PACUI|MG": ("3162658", "São João do Pacuí"),
    "MUNICIPIO DE SAO JOAO DO PARAISO|MA": ("2111052", "São João do Paraíso"),
    "MUNICIPIO DE SAO JOAO DO PARAISO|MG": ("3162708", "São João do Paraíso"),
    "MUNICIPIO DE SAO JOAO DO PIAUI|PI": ("2210003", "São João do Piauí"),
    "MUNICIPIO DE SAO JOAO DO RIO DO PEIXE|PB": ("2500700", "São João do Rio do Peixe"),
    "MUNICIPIO DE SAO JOAO DO SABUGI|RN": ("2412104", "São João do Sabugi"),
    "MUNICIPIO DE SAO JOAO DO SOTER|MA": ("2111078", "São João do Soter"),
    "MUNICIPIO DE SAO JOAO DO SUL|SC": ("4216404", "São João do Sul"),
    "MUNICIPIO DE SAO JOAO DO TIGRE|PB": ("2514107", "São João do Tigre"),
    "MUNICIPIO DE SAO JOAO DO TRIUNFO|PR": ("4125100", "São João do Triunfo"),
    "MUNICIPIO DE SAO JOAO DOS PATOS|MA": ("2111102", "São João dos Patos"),
    "MUNICIPIO DE SAO JOAO EVANGELISTA|MG": ("3162807", "São João Evangelista"),
    "MUNICIPIO DE SAO JOAO NEPOMUCENO|MG": ("3162906", "São João Nepomuceno"),
    "MUNICIPIO DE SAO JOAO|PE": ("2613206", "São João"),
    "MUNICIPIO DE SAO JOAO|PR": ("4124806", "São João"),
    "MUNICIPIO DE SAO JOAQUIM DA BARRA|SP": ("3549409", "São Joaquim da Barra"),
    "MUNICIPIO DE SAO JOAQUIM DO MONTE|PE": ("2613305", "São Joaquim do Monte"),
    "MUNICIPIO DE SAO JOAQUIM|SC": ("4216503", "São Joaquim"),
    "MUNICIPIO DE SAO JORGE D:OESTE|PR": ("4125209", "São Jorge d'Oeste"),
    "MUNICIPIO DE SAO JORGE DO IVAI|PR": ("4125308", "São Jorge do Ivaí"),
    "MUNICIPIO DE SAO JORGE DO PATROCINIO|PR": ("4125357", "São Jorge do Patrocínio"),
    "MUNICIPIO DE SAO JOSE DA BARRA|MG": ("3162948", "São José da Barra"),
    "MUNICIPIO DE SAO JOSE DA BELA VISTA|SP": ("3549508", "São José da Bela Vista"),
    "MUNICIPIO DE SAO JOSE DA COROA GRANDE|PE": ("2613404", "São José da Coroa Grande"),
    "MUNICIPIO DE SAO JOSE DA LAJE|AL": ("2708303", "São José da Laje"),
    "MUNICIPIO DE SAO JOSE DA LAPA|MG": ("3162955", "São José da Lapa"),
    "MUNICIPIO DE SAO JOSE DA SAFIRA|MG": ("3163003", "São José da Safira"),
    "MUNICIPIO DE SAO JOSE DA TAPERA|AL": ("2708402", "São José da Tapera"),
    "MUNICIPIO DE SAO JOSE DA VARGINHA|MG": ("3163102", "São José da Varginha"),
    "MUNICIPIO DE SAO JOSE DA VITORIA|BA": ("2929354", "São José da Vitória"),
    "MUNICIPIO DE SAO JOSE DE ESPINHARAS|PB": ("2514404", "São José de Espinharas"),
    "MUNICIPIO DE SAO JOSE DE MIPIBU|RN": ("2412203", "São José de Mipibu"),
    "MUNICIPIO DE SAO JOSE DE PIRANHAS|PB": ("2514503", "São José de Piranhas"),
    "MUNICIPIO DE SAO JOSE DE PRINCESA|PB": ("2514552", "São José de Princesa"),
    "MUNICIPIO DE SAO JOSE DE RIBAMAR|MA": ("2111201", "São José de Ribamar"),
    "MUNICIPIO DE SAO JOSE DE UBA|RJ": ("3305133", "São José de Ubá"),
    "MUNICIPIO DE SAO JOSE DO ALEGRE|MG": ("3163201", "São José do Alegre"),
    "MUNICIPIO DE SAO JOSE DO BARREIRO|SP": ("3549607", "São José do Barreiro"),
    "MUNICIPIO DE SAO JOSE DO BELMONTE|PE": ("2613503", "São José do Belmonte"),
    "MUNICIPIO DE SAO JOSE DO BONFIM|PB": ("2514602", "São José do Bonfim"),
    "MUNICIPIO DE SAO JOSE DO CALCADO|ES": ("3204807", "São José do Calçado"),
    "MUNICIPIO DE SAO JOSE DO CEDRO|SC": ("4216701", "São José do Cedro"),
    "MUNICIPIO DE SAO JOSE DO CERRITO|SC": ("4216800", "São José do Cerrito"),
    "MUNICIPIO DE SAO JOSE DO DIVINO|MG": ("3163300", "São José do Divino"),
    "MUNICIPIO DE SAO JOSE DO DIVINO|PI": ("2210052", "São José do Divino"),
    "MUNICIPIO DE SAO JOSE DO EGITO|PE": ("2613602", "São José do Egito"),
    "MUNICIPIO DE SAO JOSE DO GOIABAL|MG": ("3163409", "São José do Goiabal"),
    "MUNICIPIO DE SAO JOSE DO HERVAL|RS": ("4318465", "São José do Herval"),
    "MUNICIPIO DE SAO JOSE DO HORTENCIO|RS": ("4318481", "São José do Hortêncio"),
    "MUNICIPIO DE SAO JOSE DO JACUIPE|BA": ("2929370", "São José do Jacuípe"),
    "MUNICIPIO DE SAO JOSE DO JACURI|MG": ("3163508", "São José do Jacuri"),
    "MUNICIPIO DE SAO JOSE DO MANTIMENTO|MG": ("3163607", "São José do Mantimento"),
    "MUNICIPIO DE SAO JOSE DO NORTE|RS": ("4318507", "São José do Norte"),
    "MUNICIPIO DE SAO JOSE DO PIAUI|PI": ("2210201", "São José do Piauí"),
    "MUNICIPIO DE SAO JOSE DO POVO|MT": ("5107297", "São José do Povo"),
    "MUNICIPIO DE SAO JOSE DO RIO CLARO|MT": ("5107305", "São José do Rio Claro"),
    "MUNICIPIO DE SAO JOSE DO RIO PARDO|SP": ("3549706", "São José do Rio Pardo"),
    "MUNICIPIO DE SAO JOSE DO RIO PRETO|SP": ("3549805", "São José do Rio Preto"),
    "MUNICIPIO DE SAO JOSE DO SABUGI|PB": ("2514701", "São José do Sabugi"),
    "MUNICIPIO DE SAO JOSE DO SERIDO|RN": ("2412401", "São José do Seridó"),
    "MUNICIPIO DE SAO JOSE DO SUL|RS": ("4318614", "São José do Sul"),
    "MUNICIPIO DE SAO JOSE DO VALE DO RIO PRETO|RJ": ("3305158", "São José do Vale do Rio Preto"),
    "MUNICIPIO DE SAO JOSE DO XINGU|MT": ("5107354", "São José do Xingu"),
    "MUNICIPIO DE SAO JOSE DOS CAMPOS|SP": ("3549904", "São José dos Campos"),
    "MUNICIPIO DE SAO JOSE DOS CORDEIROS|PB": ("2514800", "São José dos Cordeiros"),
    "MUNICIPIO DE SAO JOSE DOS PINHAIS|PR": ("4125506", "São José dos Pinhais"),
    "MUNICIPIO DE SAO JOSE DOS QUATRO MARCOS|MT": ("5107107", "São José dos Quatro Marcos"),
    "MUNICIPIO DE SAO JOSE DOS RAMOS|PB": ("2514453", "São José dos Ramos"),
    "MUNICIPIO DE SAO JOSE|SC": ("4216602", "São José"),
    "MUNICIPIO DE SAO JULIAO|PI": ("2210300", "São Julião"),
    "MUNICIPIO DE SAO LEOPOLDO|RS": ("4318705", "São Leopoldo"),
    "MUNICIPIO DE SAO LOURENCO D'OESTE|SC": ("4216909", "São Lourenço do Oeste"),
    "MUNICIPIO DE SAO LOURENCO DA MATA|PE": ("2613701", "São Lourenço da Mata"),
    "MUNICIPIO DE SAO LOURENCO DA SERRA|SP": ("3549953", "São Lourenço da Serra"),
    "MUNICIPIO DE SAO LOURENCO DO PIAUI|PI": ("2210359", "São Lourenço do Piauí"),
    "MUNICIPIO DE SAO LOURENCO DO SUL|RS": ("4318804", "São Lourenço do Sul"),
    "MUNICIPIO DE SAO LOURENCO|MG": ("3163706", "São Lourenço"),
    "MUNICIPIO DE SAO LUDGERO|SC": ("4217006", "São Ludgero"),
    "MUNICIPIO DE SAO LUIS DE MONTES BELOS|GO": ("5220108", "São Luís de Montes Belos"),
    "MUNICIPIO DE SAO LUIS DO CURU|CE": ("2312601", "São Luís do Curu"),
    "MUNICIPIO DE SAO LUIS DO PIAUI|PI": ("2210375", "São Luis do Piauí"),
    "MUNICIPIO DE SAO LUIS DO QUITUNDE|AL": ("2708501", "São Luís do Quitunde"),
    "MUNICIPIO DE SAO LUIS GONZAGA DO MARANHAO|MA": ("2111409", "São Luís Gonzaga do Maranhão"),
    "MUNICIPIO DE SAO LUIS|MA": ("2111300", "São Luís"),
    "MUNICIPIO DE SAO LUIZ DO NORTE|GO": ("5220157", "São Luiz do Norte"),
    "MUNICIPIO DE SAO LUIZ DO PARAITINGA|SP": ("3550001", "São Luiz do Paraitinga"),
    "MUNICIPIO DE SAO LUIZ|RR": ("1400605", "São Luiz"),
    "MUNICIPIO DE SAO MAMEDE|PB": ("2514909", "São Mamede"),
    "MUNICIPIO DE SAO MANOEL DO PARANA|PR": ("4125555", "São Manoel do Paraná"),
    "MUNICIPIO DE SAO MARCOS|RS": ("4319000", "São Marcos"),
    "MUNICIPIO DE SAO MARTINHO DA SERRA|RS": ("4319125", "São Martinho da Serra"),
    "MUNICIPIO DE SAO MARTINHO|RS": ("4319109", "São Martinho"),
    "MUNICIPIO DE SAO MARTINHO|SC": ("4217105", "São Martinho"),
    "MUNICIPIO DE SAO MATEUS DO MARANHAO|MA": ("2111508", "São Mateus do Maranhão"),
    "MUNICIPIO DE SAO MATEUS DO SUL|PR": ("4125605", "São Mateus do Sul"),
    "MUNICIPIO DE SAO MATEUS|ES": ("3204906", "São Mateus"),
    "MUNICIPIO DE SAO MIGUEL ARCANJO|SP": ("3550209", "São Miguel Arcanjo"),
    "MUNICIPIO DE SAO MIGUEL D'OESTE|SC": ("4217204", "São Miguel do Oeste"),
    "MUNICIPIO DE SAO MIGUEL DA BAIXA GRANDE|PI": ("2210383", "São Miguel da Baixa Grande"),
    "MUNICIPIO DE SAO MIGUEL DA BOA VISTA|SC": ("4217154", "São Miguel da Boa Vista"),
    "MUNICIPIO DE SAO MIGUEL DAS MATAS|BA": ("2929404", "São Miguel das Matas"),
    "MUNICIPIO DE SAO MIGUEL DAS MISSOES|RS": ("4319158", "São Miguel das Missões"),
    "MUNICIPIO DE SAO MIGUEL DE TAIPU|PB": ("2515005", "São Miguel de Taipu"),
    "MUNICIPIO DE SAO MIGUEL DO ALEIXO|SE": ("2807006", "São Miguel do Aleixo"),
    "MUNICIPIO DE SAO MIGUEL DO ANTA|MG": ("3163805", "São Miguel do Anta"),
    "MUNICIPIO DE SAO MIGUEL DO ARAGUAIA|GO": ("5220207", "São Miguel do Araguaia"),
    "MUNICIPIO DE SAO MIGUEL DO FIDALGO|PI": ("2210391", "São Miguel do Fidalgo"),
    "MUNICIPIO DE SAO MIGUEL DO GOSTOSO|RN": ("2412559", "São Miguel do Gostoso"),
    "MUNICIPIO DE SAO MIGUEL DO GUAMA|PA": ("1507607", "São Miguel do Guamá"),
    "MUNICIPIO DE SAO MIGUEL DO GUAPORE|RO": ("1100320", "São Miguel do Guaporé"),
    "MUNICIPIO DE SAO MIGUEL DO IGUACU|PR": ("4125704", "São Miguel do Iguaçu"),
    "MUNICIPIO DE SAO MIGUEL DO TAPUIO|PI": ("2210409", "São Miguel do Tapuio"),
    "MUNICIPIO DE SAO MIGUEL DO TOCANTINS|TO": ("1720200", "São Miguel do Tocantins"),
    "MUNICIPIO DE SAO MIGUEL DOS CAMPOS|AL": ("2708600", "São Miguel dos Campos"),
    "MUNICIPIO DE SAO MIGUEL DOS MILAGRES|AL": ("2708709", "São Miguel dos Milagres"),
    "MUNICIPIO DE SAO MIGUEL|RN": ("2412500", "São Miguel"),
    "MUNICIPIO DE SAO NICOLAU|RS": ("4319208", "São Nicolau"),
    "MUNICIPIO DE SAO PATRICIO|GO": ("5220280", "São Patrício"),
    "MUNICIPIO DE SAO PAULO DE OLIVENCA|AM": ("1303908", "São Paulo de Olivença"),
    "MUNICIPIO DE SAO PAULO DO POTENGI|RN": ("2412609", "São Paulo do Potengi"),
    "MUNICIPIO DE SAO PAULO|SP": ("3550308", "São Paulo"),
    "MUNICIPIO DE SAO PEDRO DA ALDEIA|RJ": ("3305208", "São Pedro da Aldeia"),
    "MUNICIPIO DE SAO PEDRO DA CIPA|MT": ("5107404", "São Pedro da Cipa"),
    "MUNICIPIO DE SAO PEDRO DA SERRA|RS": ("4319356", "São Pedro da Serra"),
    "MUNICIPIO DE SAO PEDRO DA UNIAO|MG": ("3163904", "São Pedro da União"),
    "MUNICIPIO DE SAO PEDRO DAS MISSOES|RS": ("4319364", "São Pedro das Missões"),
    "MUNICIPIO DE SAO PEDRO DE ALCANTARA|SC": ("4217253", "São Pedro de Alcântara"),
    "MUNICIPIO DE SAO PEDRO DO BUTIA|RS": ("4319372", "São Pedro do Butiá"),
    "MUNICIPIO DE SAO PEDRO DO IGUACU|PR": ("4125753", "São Pedro do Iguaçu"),
    "MUNICIPIO DE SAO PEDRO DO IVAI|PR": ("4125803", "São Pedro do Ivaí"),
    "MUNICIPIO DE SAO PEDRO DO PARANA|PR": ("4125902", "São Pedro do Paraná"),
    "MUNICIPIO DE SAO PEDRO DO PIAUI|PI": ("2210508", "São Pedro do Piauí"),
    "MUNICIPIO DE SAO PEDRO DO SUACUI|MG": ("3164100", "São Pedro do Suaçuí"),
    "MUNICIPIO DE SAO PEDRO DO SUL|RS": ("4319406", "São Pedro do Sul"),
    "MUNICIPIO DE SAO PEDRO DOS FERROS|MG": ("3164001", "São Pedro dos Ferros"),
    "MUNICIPIO DE SAO PEDRO|RN": ("2412708", "São Pedro"),
    "MUNICIPIO DE SAO PEDRO|SP": ("3550407", "São Pedro"),
    "MUNICIPIO DE SAO RAFAEL|RN": ("2412807", "São Rafael"),
    "MUNICIPIO DE SAO RAIMUNDO NONATO|PI": ("2210607", "São Raimundo Nonato"),
    "MUNICIPIO DE SAO ROBERTO|MA": ("2111672", "São Roberto"),
    "MUNICIPIO DE SAO ROMAO|MG": ("3164209", "São Romão"),
    "MUNICIPIO DE SAO ROQUE DE MINAS|MG": ("3164308", "São Roque de Minas"),
    "MUNICIPIO DE SAO ROQUE DO CANAA|ES": ("3204955", "São Roque do Canaã"),
    "MUNICIPIO DE SAO ROQUE|SP": ("3550605", "São Roque"),
    "MUNICIPIO DE SAO SALVADOR DO TOCANTINS|TO": ("1720259", "São Salvador do Tocantins"),
    "MUNICIPIO DE SAO SEBASTIAO DA AMOREIRA|PR": ("4126009", "São Sebastião da Amoreira"),
    "MUNICIPIO DE SAO SEBASTIAO DA BELA VISTA|MG": ("3164407", "São Sebastião da Bela Vista"),
    "MUNICIPIO DE SAO SEBASTIAO DA BOA VISTA|PA": ("1507706", "São Sebastião da Boa Vista"),
    "MUNICIPIO DE SAO SEBASTIAO DA GRAMA|SP": ("3550803", "São Sebastião da Grama"),
    "MUNICIPIO DE SAO SEBASTIAO DA VARGEM ALEGRE|MG": ("3164431", "São Sebastião da Vargem Alegre"),
    "MUNICIPIO DE SAO SEBASTIAO DE LAGOA DE ROCA|PB": ("2515104", "São Sebastião de Lagoa de Roça"),
    "MUNICIPIO DE SAO SEBASTIAO DO ANTA|MG": ("3164472", "São Sebastião do Anta"),
    "MUNICIPIO DE SAO SEBASTIAO DO CAI|RS": ("4319505", "São Sebastião do Caí"),
    "MUNICIPIO DE SAO SEBASTIAO DO MARANHAO|MG": ("3164506", "São Sebastião do Maranhão"),
    "MUNICIPIO DE SAO SEBASTIAO DO OESTE|MG": ("3164605", "São Sebastião do Oeste"),
    "MUNICIPIO DE SAO SEBASTIAO DO PARAISO|MG": ("3164704", "São Sebastião do Paraíso"),
    "MUNICIPIO DE SAO SEBASTIAO DO PASSE|BA": ("2929503", "São Sebastião do Passé"),
    "MUNICIPIO DE SAO SEBASTIAO DO RIO PRETO|MG": ("3164803", "São Sebastião do Rio Preto"),
    "MUNICIPIO DE SAO SEBASTIAO DO TOCANTINS|TO": ("1720309", "São Sebastião do Tocantins"),
    "MUNICIPIO DE SAO SEBASTIAO DO UATUMA|AM": ("1303957", "São Sebastião do Uatumã"),
    "MUNICIPIO DE SAO SEBASTIAO DO UMBUZEIRO|PB": ("2515203", "São Sebastião do Umbuzeiro"),
    "MUNICIPIO DE SAO SEBASTIAO|AL": ("2708808", "São Sebastião"),
    "MUNICIPIO DE SAO SEBASTIAO|SP": ("3550704", "São Sebastião"),
    "MUNICIPIO DE SAO SEPE|RS": ("4319604", "São Sepé"),
    "MUNICIPIO DE SAO SIMAO|GO": ("5220405", "São Simão"),
    "MUNICIPIO DE SAO SIMAO|SP": ("3550902", "São Simão"),
    "MUNICIPIO DE SAO TIAGO|MG": ("3165008", "São Tiago"),
    "MUNICIPIO DE SAO TOMAS DE AQUINO|MG": ("3165107", "São Tomás de Aquino"),
    "MUNICIPIO DE SAO TOME|PR": ("4126108", "São Tomé"),
    "MUNICIPIO DE SAO TOME|RN": ("2412906", "São Tomé"),
    "MUNICIPIO DE SAO VALENTIM DO SUL|RS": ("4319711", "São Valentim do Sul"),
    "MUNICIPIO DE SAO VALENTIM|RS": ("4319703", "São Valentim"),
    "MUNICIPIO DE SAO VALERIO DA NATIVIDADE|TO": ("1720499", "São Valério"),
    "MUNICIPIO DE SAO VALERIO DO SUL|RS": ("4319737", "São Valério do Sul"),
    "MUNICIPIO DE SAO VICENTE DE MINAS|MG": ("3165305", "São Vicente de Minas"),
    "MUNICIPIO DE SAO VICENTE DO SUL|RS": ("4319802", "São Vicente do Sul"),
    "MUNICIPIO DE SAO VICENTE|RN": ("2413003", "São Vicente"),
    "MUNICIPIO DE SAO VICENTE|SP": ("3551009", "São Vicente"),
    "MUNICIPIO DE SAPE|PB": ("2515302", "Sapé"),
    "MUNICIPIO DE SAPIRANGA|RS": ("4319901", "Sapiranga"),
    "MUNICIPIO DE SAPOPEMA|PR": ("4126207", "Sapopema"),
    "MUNICIPIO DE SAPUCAI-MIRIM|MG": ("3165404", "Sapucaí-Mirim"),
    "MUNICIPIO DE SAPUCAIA DO SUL|RS": ("4320008", "Sapucaia do Sul"),
    "MUNICIPIO DE SAPUCAIA|PA": ("1507755", "Sapucaia"),
    "MUNICIPIO DE SAPUCAIA|RJ": ("3305406", "Sapucaia"),
    "MUNICIPIO DE SARANDI|PR": ("4126256", "Sarandi"),
    "MUNICIPIO DE SARANDI|RS": ("4320107", "Sarandi"),
    "MUNICIPIO DE SARAPUI|SP": ("3551108", "Sarapuí"),
    "MUNICIPIO DE SARDOA|MG": ("3165503", "Sardoá"),
    "MUNICIPIO DE SARUTAIA|SP": ("3551207", "Sarutaiá"),
    "MUNICIPIO DE SARZEDO|MG": ("3165537", "Sarzedo"),
    "MUNICIPIO DE SATIRO DIAS|BA": ("2929701", "Sátiro Dias"),
    "MUNICIPIO DE SATUBINHA|MA": ("2111722", "Satubinha"),
    "MUNICIPIO DE SAUBARA|BA": ("2929750", "Saubara"),
    "MUNICIPIO DE SAUDADE DO IGUACU|PR": ("4126272", "Saudade do Iguaçu"),
    "MUNICIPIO DE SAUDADES|SC": ("4217303", "Saudades"),
    "MUNICIPIO DE SAUDE|BA": ("2929800", "Saúde"),
    "MUNICIPIO DE SCHROEDER|SC": ("4217402", "Schroeder"),
    "MUNICIPIO DE SEABRA|BA": ("2929909", "Seabra"),
    "MUNICIPIO DE SEARA|SC": ("4217501", "Seara"),
    "MUNICIPIO DE SEBASTIANOPOLIS DO SUL|SP": ("3551306", "Sebastianópolis do Sul"),
    "MUNICIPIO DE SEBASTIAO BARROS|PI": ("2210623", "Sebastião Barros"),
    "MUNICIPIO DE SEBASTIAO LARANJEIRAS|BA": ("2930006", "Sebastião Laranjeiras"),
    "MUNICIPIO DE SEBASTIAO LEAL|PI": ("2210631", "Sebastião Leal"),
    "MUNICIPIO DE SEBERI|RS": ("4320206", "Seberi"),
    "MUNICIPIO DE SEDE NOVA|RS": ("4320230", "Sede Nova"),
    "MUNICIPIO DE SEGREDO|RS": ("4320263", "Segredo"),
    "MUNICIPIO DE SELBACH|RS": ("4320305", "Selbach"),
    "MUNICIPIO DE SELVIRIA|MS": ("5007802", "Selvíria"),
    "MUNICIPIO DE SEM-PEIXE|MG": ("3165560", "Sem-Peixe"),
    "MUNICIPIO DE SENA MADUREIRA|AC": ("1200500", "Sena Madureira"),
    "MUNICIPIO DE SENADOR ALEXANDRE COSTA|MA": ("2111748", "Senador Alexandre Costa"),
    "MUNICIPIO DE SENADOR AMARAL|MG": ("3165578", "Senador Amaral"),
    "MUNICIPIO DE SENADOR CANEDO|GO": ("5220454", "Senador Canedo"),
    "MUNICIPIO DE SENADOR ELOI DE SOUZA|RN": ("2413102", "Senador Elói de Souza"),
    "MUNICIPIO DE SENADOR GUIOMARD|AC": ("1200450", "Senador Guiomard"),
    "MUNICIPIO DE SENADOR JOSE BENTO|MG": ("3165800", "Senador José Bento"),
    "MUNICIPIO DE SENADOR JOSE PORFIRIO|PA": ("1507805", "Senador José Porfírio"),
    "MUNICIPIO DE SENADOR LA ROCQUE|MA": ("2111763", "Senador La Rocque"),
    "MUNICIPIO DE SENADOR MODESTINO GONCALVES|MG": ("3165909", "Senador Modestino Gonçalves"),
    "MUNICIPIO DE SENADOR POMPEU|CE": ("2312700", "Senador Pompeu"),
    "MUNICIPIO DE SENADOR RUI PALMEIRA|AL": ("2708956", "Senador Rui Palmeira"),
    "MUNICIPIO DE SENADOR SALGADO FILHO|RS": ("4320321", "Senador Salgado Filho"),
    "MUNICIPIO DE SENADOR SA|CE": ("2312809", "Senador Sá"),
    "MUNICIPIO DE SENGES|PR": ("4126306", "Sengés"),
    "MUNICIPIO DE SENHOR DO BONFIM|BA": ("2930105", "Senhor do Bonfim"),
    "MUNICIPIO DE SENHORA DE OLIVEIRA|MG": ("3166006", "Senhora de Oliveira"),
    "MUNICIPIO DE SENHORA DO PORTO|MG": ("3166105", "Senhora do Porto"),
    "MUNICIPIO DE SENHORA DOS REMEDIOS|MG": ("3166204", "Senhora dos Remédios"),
    "MUNICIPIO DE SENTINELA DO SUL|RS": ("4320354", "Sentinela do Sul"),
    "MUNICIPIO DE SERAFINA CORREA|RS": ("4320404", "Serafina Corrêa"),
    "MUNICIPIO DE SERICITA|MG": ("3166303", "Sericita"),
    "MUNICIPIO DE SERINGUEIRAS|RO": ("1101500", "Seringueiras"),
    "MUNICIPIO DE SERIO|RS": ("4320453", "Sério"),
    "MUNICIPIO DE SEROPEDICA|RJ": ("3305554", "Seropédica"),
    "MUNICIPIO DE SERRA ALTA|SC": ("4217550", "Serra Alta"),
    "MUNICIPIO DE SERRA AZUL DE MINAS|MG": ("3166501", "Serra Azul de Minas"),
    "MUNICIPIO DE SERRA AZUL|SP": ("3551405", "Serra Azul"),
    "MUNICIPIO DE SERRA BRANCA|PB": ("2515500", "Serra Branca"),
    "MUNICIPIO DE SERRA DA RAIZ|PB": ("2515609", "Serra da Raiz"),
    "MUNICIPIO DE SERRA DE SAO BENTO|RN": ("2413300", "Serra de São Bento"),
    "MUNICIPIO DE SERRA DO NAVIO|AP": ("1600055", "Serra do Navio"),
    "MUNICIPIO DE SERRA DO RAMALHO|BA": ("2930154", "Serra do Ramalho"),
    "MUNICIPIO DE SERRA DO SALITRE|MG": ("3166808", "Serra do Salitre"),
    "MUNICIPIO DE SERRA DOS AIMORES|MG": ("3166709", "Serra dos Aimorés"),
    "MUNICIPIO DE SERRA DOURADA|BA": ("2930303", "Serra Dourada"),
    "MUNICIPIO DE SERRA NEGRA DO NORTE|RN": ("2413409", "Serra Negra do Norte"),
    "MUNICIPIO DE SERRA NEGRA|SP": ("3551603", "Serra Negra"),
    "MUNICIPIO DE SERRA PRETA|BA": ("2930402", "Serra Preta"),
    "MUNICIPIO DE SERRA TALHADA|PE": ("2613909", "Serra Talhada"),
    "MUNICIPIO DE SERRANA|SP": ("3551504", "Serrana"),
    "MUNICIPIO DE SERRANIA|MG": ("3166907", "Serrania"),
    "MUNICIPIO DE SERRANO DO MARANHAO|MA": ("2111789", "Serrano do Maranhão"),
    "MUNICIPIO DE SERRANOPOLIS DE MINAS|MG": ("3166956", "Serranópolis de Minas"),
    "MUNICIPIO DE SERRANOS|MG": ("3167004", "Serranos"),
    "MUNICIPIO DE SERRARIA|PB": ("2515906", "Serraria"),
    "MUNICIPIO DE SERRINHA DOS PINTOS|RN": ("2413557", "Serrinha dos Pintos"),
    "MUNICIPIO DE SERRINHA|BA": ("2930501", "Serrinha"),
    "MUNICIPIO DE SERRINHA|RN": ("2413508", "Serrinha"),
    "MUNICIPIO DE SERROLANDIA|BA": ("2930600", "Serrolândia"),
    "MUNICIPIO DE SERRO|MG": ("3167103", "Serro"),
    "MUNICIPIO DE SERTANEJA|PR": ("4126405", "Sertaneja"),
    "MUNICIPIO DE SERTANIA|PE": ("2614105", "Sertânia"),
    "MUNICIPIO DE SERTANOPOLIS|PR": ("4126504", "Sertanópolis"),
    "MUNICIPIO DE SERTAO SANTANA|RS": ("4320552", "Sertão Santana"),
    "MUNICIPIO DE SERTAOZINHO|PB": ("2515930", "Sertãozinho"),
    "MUNICIPIO DE SERTAOZINHO|SP": ("3551702", "Sertãozinho"),
    "MUNICIPIO DE SERTAO|RS": ("4320503", "Sertão"),
    "MUNICIPIO DE SETE BARRAS|SP": ("3551801", "Sete Barras"),
    "MUNICIPIO DE SETE DE SETEMBRO|RS": ("4320578", "Sete de Setembro"),
    "MUNICIPIO DE SETE LAGOAS|MG": ("3167202", "Sete Lagoas"),
    "MUNICIPIO DE SETE QUEDAS|MS": ("5007703", "Sete Quedas"),
    "MUNICIPIO DE SETUBINHA|MG": ("3165552", "Setubinha"),
    "MUNICIPIO DE SEVERIANO MELO|RN": ("2413607", "Severiano Melo"),
    "MUNICIPIO DE SEVERINIA|SP": ("3551900", "Severínia"),
    "MUNICIPIO DE SIDEROPOLIS|SC": ("4217600", "Siderópolis"),
    "MUNICIPIO DE SIDROLANDIA|MS": ("5007901", "Sidrolândia"),
    "MUNICIPIO DE SIGEFREDO PACHECO|PI": ("2210656", "Sigefredo Pacheco"),
    "MUNICIPIO DE SILVANIA|GO": ("5220603", "Silvânia"),
    "MUNICIPIO DE SILVANOPOLIS|TO": ("1720655", "Silvanópolis"),
    "MUNICIPIO DE SILVEIRA MARTINS|RS": ("4320651", "Silveira Martins"),
    "MUNICIPIO DE SILVEIRANIA|MG": ("3167301", "Silveirânia"),
    "MUNICIPIO DE SILVEIRAS|SP": ("3552007", "Silveiras"),
    "MUNICIPIO DE SILVES|AM": ("1304005", "Silves"),
    "MUNICIPIO DE SILVIANOPOLIS|MG": ("3167400", "Silvianópolis"),
    "MUNICIPIO DE SIMAO DIAS|SE": ("2807105", "Simão Dias"),
    "MUNICIPIO DE SIMAO PEREIRA|MG": ("3167509", "Simão Pereira"),
    "MUNICIPIO DE SIMOES|PI": ("2210706", "Simões"),
    "MUNICIPIO DE SIMOLANDIA|GO": ("5220686", "Simolândia"),
    "MUNICIPIO DE SIMONESIA|MG": ("3167608", "Simonésia"),
    "MUNICIPIO DE SIMPLICIO MENDES|PI": ("2210805", "Simplício Mendes"),
    "MUNICIPIO DE SINIMBU|RS": ("4320677", "Sinimbu"),
    "MUNICIPIO DE SINOP|MT": ("5107909", "Sinop"),
    "MUNICIPIO DE SIQUEIRA CAMPOS|PR": ("4126603", "Siqueira Campos"),
    "MUNICIPIO DE SIRINHAEM|PE": ("2614204", "Sirinhaém"),
    "MUNICIPIO DE SITIO DO MATO|BA": ("2930758", "Sítio do Mato"),
    "MUNICIPIO DE SITIO NOVO DO TOCANTINS|TO": ("1720804", "Sítio Novo do Tocantins"),
    "MUNICIPIO DE SITIO NOVO|MA": ("2111805", "Sítio Novo"),
    "MUNICIPIO DE SITIO NOVO|RN": ("2413706", "Sítio Novo"),
    "MUNICIPIO DE SOBRADINHO|BA": ("2930774", "Sobradinho"),
    "MUNICIPIO DE SOBRADINHO|RS": ("4320701", "Sobradinho"),
    "MUNICIPIO DE SOBRADO|PB": ("2515971", "Sobrado"),
    "MUNICIPIO DE SOBRALIA|MG": ("3167707", "Sobrália"),
    "MUNICIPIO DE SOBRAL|CE": ("2312908", "Sobral"),
    "MUNICIPIO DE SOCORRO DO PIAUI|PI": ("2210904", "Socorro do Piauí"),
    "MUNICIPIO DE SOCORRO|SP": ("3552106", "Socorro"),
    "MUNICIPIO DE SOLEDADE DE MINAS|MG": ("3167806", "Soledade de Minas"),
    "MUNICIPIO DE SOLEDADE|PB": ("2516102", "Soledade"),
    "MUNICIPIO DE SOLEDADE|RS": ("4320800", "Soledade"),
    "MUNICIPIO DE SOLIDAO|PE": ("2614402", "Solidão"),
    "MUNICIPIO DE SOMBRIO|SC": ("4217709", "Sombrio"),
    "MUNICIPIO DE SONORA|MS": ("5007935", "Sonora"),
    "MUNICIPIO DE SOORETAMA|ES": ("3205010", "Sooretama"),
    "MUNICIPIO DE SOROCABA|SP": ("3552205", "Sorocaba"),
    "MUNICIPIO DE SORRISO|MT": ("5107925", "Sorriso"),
    "MUNICIPIO DE SOSSEGO|PB": ("2516151", "Sossêgo"),
    "MUNICIPIO DE SOURE|PA": ("1507904", "Soure"),
    "MUNICIPIO DE SOUSA|PB": ("2516201", "Sousa"),
    "MUNICIPIO DE SOUTO SOARES|BA": ("2930808", "Souto Soares"),
    "MUNICIPIO DE SUCUPIRA DO NORTE|MA": ("2111904", "Sucupira do Norte"),
    "MUNICIPIO DE SUCUPIRA|TO": ("1720853", "Sucupira"),
    "MUNICIPIO DE SUD MENNUCCI|SP": ("3552304", "Sud Mennucci"),
    "MUNICIPIO DE SUL BRASIL|SC": ("4217758", "Sul Brasil"),
    "MUNICIPIO DE SULINA|PR": ("4126652", "Sulina"),
    "MUNICIPIO DE SUMARE|SP": ("3552403", "Sumaré"),
    "MUNICIPIO DE SURUBIM|PE": ("2614501", "Surubim"),
    "MUNICIPIO DE SUZANAPOLIS|SP": ("3552551", "Suzanápolis"),
    "MUNICIPIO DE SUZANO|SP": ("3552502", "Suzano"),
    "MUNICIPIO DE TABAI|RS": ("4320859", "Tabaí"),
    "MUNICIPIO DE TABAPUA|SP": ("3552601", "Tabapuã"),
    "MUNICIPIO DE TABATINGA|AM": ("1304062", "Tabatinga"),
    "MUNICIPIO DE TABATINGA|SP": ("3552700", "Tabatinga"),
    "MUNICIPIO DE TABIRA|PE": ("2614600", "Tabira"),
    "MUNICIPIO DE TABOAO DA SERRA|SP": ("3552809", "Taboão da Serra"),
    "MUNICIPIO DE TABOCAS DO BREJO VELHO|BA": ("2930907", "Tabocas do Brejo Velho"),
    "MUNICIPIO DE TABOLEIRO GRANDE|RN": ("2413805", "Taboleiro Grande"),
    "MUNICIPIO DE TABULEIRO DO NORTE|CE": ("2313104", "Tabuleiro do Norte"),
    "MUNICIPIO DE TACAIMBO|PE": ("2614709", "Tacaimbó"),
    "MUNICIPIO DE TACARATU|PE": ("2614808", "Tacaratu"),
    "MUNICIPIO DE TACIBA|SP": ("3552908", "Taciba"),
    "MUNICIPIO DE TACIMA|PB": ("2516409", "Tacima"),
    "MUNICIPIO DE TACURU|MS": ("5007950", "Tacuru"),
    "MUNICIPIO DE TAGUAI|SP": ("3553005", "Taguaí"),
    "MUNICIPIO DE TAGUATINGA|TO": ("1720903", "Taguatinga"),
    "MUNICIPIO DE TAIACU|SP": ("3553104", "Taiaçu"),
    "MUNICIPIO DE TAILANDIA|PA": ("1507953", "Tailândia"),
    "MUNICIPIO DE TAIOBEIRAS|MG": ("3168002", "Taiobeiras"),
    "MUNICIPIO DE TAIO|SC": ("4217808", "Taió"),
    "MUNICIPIO DE TAIPAS DO TOCANTINS|TO": ("1720937", "Taipas do Tocantins"),
    "MUNICIPIO DE TAIPU|RN": ("2413904", "Taipu"),
    "MUNICIPIO DE TALISMA|TO": ("1720978", "Talismã"),
    "MUNICIPIO DE TAMARANA|PR": ("4126678", "Tamarana"),
    "MUNICIPIO DE TAMBAU|SP": ("3553302", "Tambaú"),
    "MUNICIPIO DE TAMBOARA|PR": ("4126702", "Tamboara"),
    "MUNICIPIO DE TAMBORIL DO PIAUI|PI": ("2210953", "Tamboril do Piauí"),
    "MUNICIPIO DE TAMBORIL|CE": ("2313203", "Tamboril"),
    "MUNICIPIO DE TANABI|SP": ("3553401", "Tanabi"),
    "MUNICIPIO DE TANGARA DA SERRA|MT": ("5107958", "Tangará da Serra"),
    "MUNICIPIO DE TANGARA|RN": ("2414001", "Tangará"),
    "MUNICIPIO DE TANGARA|SC": ("4217907", "Tangará"),
    "MUNICIPIO DE TANGUA|RJ": ("3305752", "Tanguá"),
    "MUNICIPIO DE TANHACU|BA": ("2931004", "Tanhaçu"),
    "MUNICIPIO DE TANQUE D'ARCA|AL": ("2709004", "Tanque d'Arca"),
    "MUNICIPIO DE TANQUE DO PIAUI|PI": ("2210979", "Tanque do Piauí"),
    "MUNICIPIO DE TANQUE NOVO|BA": ("2931053", "Tanque Novo"),
    "MUNICIPIO DE TAPAUA|AM": ("1304104", "Tapauá"),
    "MUNICIPIO DE TAPEJARA|PR": ("4126801", "Tapejara"),
    "MUNICIPIO DE TAPEJARA|RS": ("4320909", "Tapejara"),
    "MUNICIPIO DE TAPEROA|BA": ("2931202", "Taperoá"),
    "MUNICIPIO DE TAPEROA|PB": ("2516508", "Taperoá"),
    "MUNICIPIO DE TAPES|RS": ("4321105", "Tapes"),
    "MUNICIPIO DE TAPIRAI|MG": ("3168200", "Tapiraí"),
    "MUNICIPIO DE TAPIRAI|SP": ("3553500", "Tapiraí"),
    "MUNICIPIO DE TAPIRAMUTA|BA": ("2931301", "Tapiramutá"),
    "MUNICIPIO DE TAPIRATIBA|SP": ("3553609", "Tapiratiba"),
    "MUNICIPIO DE TAPIRA|MG": ("3168101", "Tapira"),
    "MUNICIPIO DE TAPIRA|PR": ("4126900", "Tapira"),
    "MUNICIPIO DE TAQUARACU DE MINAS|MG": ("3168309", "Taquaraçu de Minas"),
    "MUNICIPIO DE TAQUARA|RS": ("4321204", "Taquara"),
    "MUNICIPIO DE TAQUARITINGA DO NORTE|PE": ("2615003", "Taquaritinga do Norte"),
    "MUNICIPIO DE TAQUARITINGA|SP": ("3553708", "Taquaritinga"),
    "MUNICIPIO DE TAQUARITUBA|SP": ("3553807", "Taquarituba"),
    "MUNICIPIO DE TAQUARIVAI|SP": ("3553856", "Taquarivaí"),
    "MUNICIPIO DE TAQUARI|RS": ("4321303", "Taquari"),
    "MUNICIPIO DE TAQUARUCU DO SUL|RS": ("4321329", "Taquaruçu do Sul"),
    "MUNICIPIO DE TARABAI|SP": ("3553906", "Tarabai"),
    "MUNICIPIO DE TARAUACA|AC": ("1200609", "Tarauacá"),
    "MUNICIPIO DE TARRAFAS|CE": ("2313252", "Tarrafas"),
    "MUNICIPIO DE TARTARUGALZINHO|AP": ("1600709", "Tartarugalzinho"),
    "MUNICIPIO DE TARUMIRIM|MG": ("3168408", "Tarumirim"),
    "MUNICIPIO DE TASSO FRAGOSO|MA": ("2112001", "Tasso Fragoso"),
    "MUNICIPIO DE TATUI|SP": ("3554003", "Tatuí"),
    "MUNICIPIO DE TAUA|CE": ("2313302", "Tauá"),
    "MUNICIPIO DE TAUBATE|SP": ("3554102", "Taubaté"),
    "MUNICIPIO DE TAVARES|PB": ("2516607", "Tavares"),
    "MUNICIPIO DE TAVARES|RS": ("4321352", "Tavares"),
    "MUNICIPIO DE TEFE|AM": ("1304203", "Tefé"),
    "MUNICIPIO DE TEIXEIRA SOARES|PR": ("4127007", "Teixeira Soares"),
    "MUNICIPIO DE TEIXEIRAS|MG": ("3168507", "Teixeiras"),
    "MUNICIPIO DE TEIXEIROPOLIS|RO": ("1101559", "Teixeirópolis"),
    "MUNICIPIO DE TEJUCUOCA|CE": ("2313351", "Tejuçuoca"),
    "MUNICIPIO DE TELEMACO BORBA|PR": ("4127106", "Telêmaco Borba"),
    "MUNICIPIO DE TELHA|SE": ("2807303", "Telha"),
    "MUNICIPIO DE TENENTE LAURENTINO CRUZ|RN": ("2414159", "Tenente Laurentino Cruz"),
    "MUNICIPIO DE TENENTE PORTELA|RS": ("4321402", "Tenente Portela"),
    "MUNICIPIO DE TENORIO|PB": ("2516755", "Tenório"),
    "MUNICIPIO DE TEODORO SAMPAIO|BA": ("2931400", "Teodoro Sampaio"),
    "MUNICIPIO DE TEODORO SAMPAIO|SP": ("3554300", "Teodoro Sampaio"),
    "MUNICIPIO DE TEOFILANDIA|BA": ("2931509", "Teofilândia"),
    "MUNICIPIO DE TEOFILO OTONI|MG": ("3168606", "Teófilo Otoni"),
    "MUNICIPIO DE TERESINA DE GOIAS|GO": ("5221080", "Teresina de Goiás"),
    "MUNICIPIO DE TERESINA|PI": ("2211001", "Teresina"),
    "MUNICIPIO DE TERESOPOLIS|RJ": ("3305802", "Teresópolis"),
    "MUNICIPIO DE TEREZINHA|PE": ("2615102", "Terezinha"),
    "MUNICIPIO DE TEREZOPOLIS DE GOIAS|GO": ("5221197", "Terezópolis de Goiás"),
    "MUNICIPIO DE TERRA ALTA|PA": ("1507961", "Terra Alta"),
    "MUNICIPIO DE TERRA BOA|PR": ("4127205", "Terra Boa"),
    "MUNICIPIO DE TERRA DE AREIA|RS": ("4321436", "Terra de Areia"),
    "MUNICIPIO DE TERRA NOVA DO NORTE|MT": ("5108055", "Terra Nova do Norte"),
    "MUNICIPIO DE TERRA NOVA|BA": ("2931707", "Terra Nova"),
    "MUNICIPIO DE TERRA NOVA|PE": ("2615201", "Terra Nova"),
    "MUNICIPIO DE TERRA RICA|PR": ("4127304", "Terra Rica"),
    "MUNICIPIO DE TERRA ROXA|PR": ("4127403", "Terra Roxa"),
    "MUNICIPIO DE TERRA ROXA|SP": ("3554409", "Terra Roxa"),
    "MUNICIPIO DE TERRA SANTA|PA": ("1507979", "Terra Santa"),
    "MUNICIPIO DE TEUTONIA|RS": ("4321451", "Teutônia"),
    "MUNICIPIO DE THEOBROMA|RO": ("1101609", "Theobroma"),
    "MUNICIPIO DE TIANGUA|CE": ("2313401", "Tianguá"),
    "MUNICIPIO DE TIBAGI|PR": ("4127502", "Tibagi"),
    "MUNICIPIO DE TIBAU DO SUL|RN": ("2414209", "Tibau do Sul"),
    "MUNICIPIO DE TIETE|SP": ("3554508", "Tietê"),
    "MUNICIPIO DE TIGRINHOS|SC": ("4217956", "Tigrinhos"),
    "MUNICIPIO DE TIJUCAS DO SUL|PR": ("4127601", "Tijucas do Sul"),
    "MUNICIPIO DE TIJUCAS|SC": ("4218004", "Tijucas"),
    "MUNICIPIO DE TIMBAUBA DOS BATISTAS|RN": ("2414308", "Timbaúba dos Batistas"),
    "MUNICIPIO DE TIMBAUBA|PE": ("2615300", "Timbaúba"),
    "MUNICIPIO DE TIMBE DO SUL|SC": ("4218103", "Timbé do Sul"),
    "MUNICIPIO DE TIMBIRAS|MA": ("2112100", "Timbiras"),
    "MUNICIPIO DE TIMBO GRANDE|SC": ("4218251", "Timbó Grande"),
    "MUNICIPIO DE TIMBO|SC": ("4218202", "Timbó"),
    "MUNICIPIO DE TIMON|MA": ("2112209", "Timon"),
    "MUNICIPIO DE TIMOTEO|MG": ("3168705", "Timóteo"),
    "MUNICIPIO DE TIO HUGO|RS": ("4321469", "Tio Hugo"),
    "MUNICIPIO DE TIRADENTES|MG": ("3168804", "Tiradentes"),
    "MUNICIPIO DE TIROS|MG": ("3168903", "Tiros"),
    "MUNICIPIO DE TOBIAS BARRETO|SE": ("2807402", "Tobias Barreto"),
    "MUNICIPIO DE TOCANTINIA|TO": ("1721109", "Tocantínia"),
    "MUNICIPIO DE TOCANTINOPOLIS|TO": ("1721208", "Tocantinópolis"),
    "MUNICIPIO DE TOCANTINS|MG": ("3169000", "Tocantins"),
    "MUNICIPIO DE TOCOS DO MOJI|MG": ("3169059", "Tocos do Moji"),
    "MUNICIPIO DE TOLEDO|MG": ("3169109", "Toledo"),
    "MUNICIPIO DE TOLEDO|PR": ("4127700", "Toledo"),
    "MUNICIPIO DE TOMAR DO GERU|SE": ("2807501", "Tomar do Geru"),
    "MUNICIPIO DE TOMAZINA|PR": ("4127809", "Tomazina"),
    "MUNICIPIO DE TOMBOS|MG": ("3169208", "Tombos"),
    "MUNICIPIO DE TOME-ACU|PA": ("1508001", "Tomé-Açu"),
    "MUNICIPIO DE TONANTINS|AM": ("1304237", "Tonantins"),
    "MUNICIPIO DE TORITAMA|PE": ("2615409", "Toritama"),
    "MUNICIPIO DE TORIXOREU|MT": ("5108204", "Torixoréu"),
    "MUNICIPIO DE TOROPI|RS": ("4321493", "Toropi"),
    "MUNICIPIO DE TORRES|RS": ("4321501", "Torres"),
    "MUNICIPIO DE TORRINHA|SP": ("3554706", "Torrinha"),
    "MUNICIPIO DE TOUROS|RN": ("2414407", "Touros"),
    "MUNICIPIO DE TRABIJU|SP": ("3554755", "Trabiju"),
    "MUNICIPIO DE TRACUATEUA|PA": ("1508035", "Tracuateua"),
    "MUNICIPIO DE TRACUNHAEM|PE": ("2615508", "Tracunhaém"),
    "MUNICIPIO DE TRAIRAO|PA": ("1508050", "Trairão"),
    "MUNICIPIO DE TRAIRI|CE": ("2313500", "Trairi"),
    "MUNICIPIO DE TRAMANDAI|RS": ("4321600", "Tramandaí"),
    "MUNICIPIO DE TRAVESSEIRO|RS": ("4321626", "Travesseiro"),
    "MUNICIPIO DE TREMEDAL|BA": ("2931806", "Tremedal"),
    "MUNICIPIO DE TREMEMBE|SP": ("3554805", "Tremembé"),
    "MUNICIPIO DE TRES BARRAS DO PARANA|PR": ("4127858", "Três Barras do Paraná"),
    "MUNICIPIO DE TRES CACHOEIRAS|RS": ("4321667", "Três Cachoeiras"),
    "MUNICIPIO DE TRES CORACOES|MG": ("3169307", "Três Corações"),
    "MUNICIPIO DE TRES COROAS|RS": ("4321709", "Três Coroas"),
    "MUNICIPIO DE TRES DE MAIO|RS": ("4321808", "Três de Maio"),
    "MUNICIPIO DE TRES FORQUILHAS|RS": ("4321832", "Três Forquilhas"),
    "MUNICIPIO DE TRES FRONTEIRAS|SP": ("3554904", "Três Fronteiras"),
    "MUNICIPIO DE TRES LAGOAS|MS": ("5008305", "Três Lagoas"),
    "MUNICIPIO DE TRES MARIAS|MG": ("3169356", "Três Marias"),
    "MUNICIPIO DE TRES PALMEIRAS|RS": ("4321857", "Três Palmeiras"),
    "MUNICIPIO DE TRES PASSOS|RS": ("4321907", "Três Passos"),
    "MUNICIPIO DE TRES PONTAS|MG": ("3169406", "Três Pontas"),
    "MUNICIPIO DE TRES RANCHOS|GO": ("5221304", "Três Ranchos"),
    "MUNICIPIO DE TRES RIOS|RJ": ("3306008", "Três Rios"),
    "MUNICIPIO DE TREVISO|SC": ("4218350", "Treviso"),
    "MUNICIPIO DE TREZE DE MAIO|SC": ("4218400", "Treze de Maio"),
    "MUNICIPIO DE TREZE TILIAS|SC": ("4218509", "Treze Tílias"),
    "MUNICIPIO DE TRINDADE DO SUL|RS": ("4321956", "Trindade do Sul"),
    "MUNICIPIO DE TRINDADE|GO": ("5221403", "Trindade"),
    "MUNICIPIO DE TRINDADE|PE": ("2615607", "Trindade"),
    "MUNICIPIO DE TRIUNFO POTIGUAR|RN": ("2414456", "Triunfo Potiguar"),
    "MUNICIPIO DE TRIUNFO|PB": ("2516805", "Triunfo"),
    "MUNICIPIO DE TRIUNFO|PE": ("2615706", "Triunfo"),
    "MUNICIPIO DE TRIUNFO|RS": ("4322004", "Triunfo"),
    "MUNICIPIO DE TROMBAS|GO": ("5221452", "Trombas"),
    "MUNICIPIO DE TROMBUDO CENTRAL|SC": ("4218608", "Trombudo Central"),
    "MUNICIPIO DE TUCUMA|PA": ("1508084", "Tucumã"),
    "MUNICIPIO DE TUCUNDUVA|RS": ("4322103", "Tucunduva"),
    "MUNICIPIO DE TUFILANDIA|MA": ("2112274", "Tufilândia"),
    "MUNICIPIO DE TUMIRITINGA|MG": ("3169505", "Tumiritinga"),
    "MUNICIPIO DE TUNAPOLIS|SC": ("4218756", "Tunápolis"),
    "MUNICIPIO DE TUNAS DO PARANA|PR": ("4127882", "Tunas do Paraná"),
    "MUNICIPIO DE TUNAS|RS": ("4322152", "Tunas"),
    "MUNICIPIO DE TUNEIRAS DO OESTE|PR": ("4127908", "Tuneiras do Oeste"),
    "MUNICIPIO DE TUNTUM|MA": ("2112308", "Tuntum"),
    "MUNICIPIO DE TUPACIGUARA|MG": ("3169604", "Tupaciguara"),
    "MUNICIPIO DE TUPANCI DO SUL|RS": ("4322186", "Tupanci do Sul"),
    "MUNICIPIO DE TUPANCIRETA|RS": ("4322202", "Tupanciretã"),
    "MUNICIPIO DE TUPANDI|RS": ("4322251", "Tupandi"),
    "MUNICIPIO DE TUPARETAMA|PE": ("2615904", "Tuparetama"),
    "MUNICIPIO DE TUPASSI|PR": ("4127957", "Tupãssi"),
    "MUNICIPIO DE TUPA|SP": ("3555000", "Tupã"),
    "MUNICIPIO DE TUPI PAULISTA|SP": ("3555109", "Tupi Paulista"),
    "MUNICIPIO DE TUPIRAMA|TO": ("1721257", "Tupirama"),
    "MUNICIPIO DE TUPIRATINS|TO": ("1721307", "Tupiratins"),
    "MUNICIPIO DE TURILANDIA|MA": ("2112456", "Turilândia"),
    "MUNICIPIO DE TURIUBA|SP": ("3555208", "Turiúba"),
    "MUNICIPIO DE TURMALINA|MG": ("3169703", "Turmalina"),
    "MUNICIPIO DE TURMALINA|SP": ("3555307", "Turmalina"),
    "MUNICIPIO DE TURUCU|RS": ("4322327", "Turuçu"),
    "MUNICIPIO DE TURURU|CE": ("2313559", "Tururu"),
    "MUNICIPIO DE TURVANIA|GO": ("5221502", "Turvânia"),
    "MUNICIPIO DE TURVELANDIA|GO": ("5221551", "Turvelândia"),
    "MUNICIPIO DE TURVOLANDIA|MG": ("3169802", "Turvolândia"),
    "MUNICIPIO DE TURVO|PR": ("4127965", "Turvo"),
    "MUNICIPIO DE TURVO|SC": ("4218806", "Turvo"),
    "MUNICIPIO DE TUTOIA|MA": ("2112506", "Tutóia"),
    "MUNICIPIO DE UARINI|AM": ("1304260", "Uarini"),
    "MUNICIPIO DE UAUA|BA": ("2932002", "Uauá"),
    "MUNICIPIO DE UBAIRA|BA": ("2932101", "Ubaíra"),
    "MUNICIPIO DE UBAITABA|BA": ("2932200", "Ubaitaba"),
    "MUNICIPIO DE UBAI|MG": ("3170008", "Ubaí"),
    "MUNICIPIO DE UBAPORANGA|MG": ("3170057", "Ubaporanga"),
    "MUNICIPIO DE UBATA|BA": ("2932309", "Ubatã"),
    "MUNICIPIO DE UBATUBA|SP": ("3555406", "Ubatuba"),
    "MUNICIPIO DE UBA|MG": ("3169901", "Ubá"),
    "MUNICIPIO DE UBERABA|MG": ("3170107", "Uberaba"),
    "MUNICIPIO DE UBERLANDIA|MG": ("3170206", "Uberlândia"),
    "MUNICIPIO DE UBIRAJARA|SP": ("3555505", "Ubirajara"),
    "MUNICIPIO DE UBIRATA|PR": ("4128005", "Ubiratã"),
    "MUNICIPIO DE UCHOA|SP": ("3555604", "Uchoa"),
    "MUNICIPIO DE UIBAI|BA": ("2932408", "Uibaí"),
    "MUNICIPIO DE UIRAMUTA|RR": ("1400704", "Uiramutã"),
    "MUNICIPIO DE UIRAPURU|GO": ("5221577", "Uirapuru"),
    "MUNICIPIO DE ULIANOPOLIS|PA": ("1508126", "Ulianópolis"),
    "MUNICIPIO DE UMARIZAL|RN": ("2414506", "Umarizal"),
    "MUNICIPIO DE UMBAUBA|SE": ("2807600", "Umbaúba"),
    "MUNICIPIO DE UMBURANAS|BA": ("2932457", "Umburanas"),
    "MUNICIPIO DE UMBURATIBA|MG": ("3170305", "Umburatiba"),
    "MUNICIPIO DE UMBUZEIRO|PB": ("2517001", "Umbuzeiro"),
    "MUNICIPIO DE UMUARAMA|PR": ("4128104", "Umuarama"),
    "MUNICIPIO DE UNAI|MG": ("3170404", "Unaí"),
    "MUNICIPIO DE UNA|BA": ("2932507", "Una"),
    "MUNICIPIO DE UNIAO DA SERRA|RS": ("4322350", "União da Serra"),
    "MUNICIPIO DE UNIAO DA VITORIA|PR": ("4128203", "União da Vitória"),
    "MUNICIPIO DE UNIAO DE MINAS|MG": ("3170438", "União de Minas"),
    "MUNICIPIO DE UNIAO DO OESTE|SC": ("4218855", "União do Oeste"),
    "MUNICIPIO DE UNIAO DO SUL|MT": ("5108303", "União do Sul"),
    "MUNICIPIO DE UNIAO|PI": ("2211100", "União"),
    "MUNICIPIO DE UNIFLOR|PR": ("4128302", "Uniflor"),
    "MUNICIPIO DE UNISTALDA|RS": ("4322376", "Unistalda"),
    "MUNICIPIO DE UPANEMA|RN": ("2414605", "Upanema"),
    "MUNICIPIO DE URAI|PR": ("4128401", "Uraí"),
    "MUNICIPIO DE URANDI|BA": ("2932606", "Urandi"),
    "MUNICIPIO DE URBANO SANTOS|MA": ("2112605", "Urbano Santos"),
    "MUNICIPIO DE URUACU|GO": ("5221601", "Uruaçu"),
    "MUNICIPIO DE URUANA DE MINAS|MG": ("3170479", "Uruana de Minas"),
    "MUNICIPIO DE URUANA|GO": ("5221700", "Uruana"),
    "MUNICIPIO DE URUARA|PA": ("1508159", "Uruará"),
    "MUNICIPIO DE URUBICI|SC": ("4218905", "Urubici"),
    "MUNICIPIO DE URUBURETAMA|CE": ("2313807", "Uruburetama"),
    "MUNICIPIO DE URUCANIA|MG": ("3170503", "Urucânia"),
    "MUNICIPIO DE URUCUCA|BA": ("2932705", "Uruçuca"),
    "MUNICIPIO DE URUCUIA|MG": ("3170529", "Urucuia"),
    "MUNICIPIO DE URUCURITUBA|AM": ("1304401", "Urucurituba"),
    "MUNICIPIO DE URUGUAIANA|RS": ("4322400", "Uruguaiana"),
    "MUNICIPIO DE URUOCA|CE": ("2313906", "Uruoca"),
    "MUNICIPIO DE URUPA|RO": ("1101708", "Urupá"),
    "MUNICIPIO DE URUPEMA|SC": ("4218954", "Urupema"),
    "MUNICIPIO DE URUSSANGA|SC": ("4219002", "Urussanga"),
    "MUNICIPIO DE UTINGA|BA": ("2932804", "Utinga"),
    "MUNICIPIO DE VACARIA|RS": ("4322509", "Vacaria"),
    "MUNICIPIO DE VALE DO ANARI|RO": ("1101757", "Vale do Anari"),
    "MUNICIPIO DE VALE DO PARAISO|RO": ("1101807", "Vale do Paraíso"),
    "MUNICIPIO DE VALE DO SOL|RS": ("4322533", "Vale do Sol"),
    "MUNICIPIO DE VALE REAL|RS": ("4322541", "Vale Real"),
    "MUNICIPIO DE VALE VERDE|RS": ("4322525", "Vale Verde"),
    "MUNICIPIO DE VALENCA DO PIAUI|PI": ("2211308", "Valença do Piauí"),
    "MUNICIPIO DE VALENTE|BA": ("2933000", "Valente"),
    "MUNICIPIO DE VALENTIM GENTIL|SP": ("3556107", "Valentim Gentil"),
    "MUNICIPIO DE VALINHOS|SP": ("3556206", "Valinhos"),
    "MUNICIPIO DE VALPARAISO DE GOIAS|GO": ("5221858", "Valparaíso de Goiás"),
    "MUNICIPIO DE VALPARAISO|SP": ("3556305", "Valparaíso"),
    "MUNICIPIO DE VANINI|RS": ("4322558", "Vanini"),
    "MUNICIPIO DE VARGEAO|SC": ("4219101", "Vargeão"),
    "MUNICIPIO DE VARGEM ALEGRE|MG": ("3170578", "Vargem Alegre"),
    "MUNICIPIO DE VARGEM ALTA|ES": ("3205036", "Vargem Alta"),
    "MUNICIPIO DE VARGEM BONITA|MG": ("3170602", "Vargem Bonita"),
    "MUNICIPIO DE VARGEM BONITA|SC": ("4219176", "Vargem Bonita"),
    "MUNICIPIO DE VARGEM GRANDE DO RIO PARDO|MG": ("3170651", "Vargem Grande do Rio Pardo"),
    "MUNICIPIO DE VARGEM GRANDE DO SUL|SP": ("3556404", "Vargem Grande do Sul"),
    "MUNICIPIO DE VARGEM|SC": ("4219150", "Vargem"),
    "MUNICIPIO DE VARGEM|SP": ("3556354", "Vargem"),
    "MUNICIPIO DE VARGINHA|MG": ("3170701", "Varginha"),
    "MUNICIPIO DE VARJAO DE MINAS|MG": ("3170750", "Varjão de Minas"),
    "MUNICIPIO DE VARJAO|GO": ("5221908", "Varjão"),
    "MUNICIPIO DE VARJOTA|CE": ("2313955", "Varjota"),
    "MUNICIPIO DE VARZEA BRANCA|PI": ("2211357", "Várzea Branca"),
    "MUNICIPIO DE VARZEA DA PALMA|MG": ("3170800", "Várzea da Palma"),
    "MUNICIPIO DE VARZEA DA ROCA|BA": ("2933059", "Várzea da Roça"),
    "MUNICIPIO DE VARZEA DO POCO|BA": ("2933109", "Várzea do Poço"),
    "MUNICIPIO DE VARZEA GRANDE|MT": ("5108402", "Várzea Grande"),
    "MUNICIPIO DE VARZEA GRANDE|PI": ("2211407", "Várzea Grande"),
    "MUNICIPIO DE VARZEA NOVA|BA": ("2933158", "Várzea Nova"),
    "MUNICIPIO DE VARZEA PAULISTA|SP": ("3556503", "Várzea Paulista"),
    "MUNICIPIO DE VARZEA|PB": ("2517100", "Várzea"),
    "MUNICIPIO DE VARZEA|RN": ("2414704", "Várzea"),
    "MUNICIPIO DE VARZEDO|BA": ("2933174", "Varzedo"),
    "MUNICIPIO DE VARZELANDIA|MG": ("3170909", "Varzelândia"),
    "MUNICIPIO DE VAZANTE|MG": ("3171006", "Vazante"),
    "MUNICIPIO DE VENANCIO AIRES|RS": ("4322608", "Venâncio Aires"),
    "MUNICIPIO DE VENDA NOVA DO IMIGRANTE|ES": ("3205069", "Venda Nova do Imigrante"),
    "MUNICIPIO DE VENHA-VER|RN": ("2414753", "Venha-Ver"),
    "MUNICIPIO DE VENTANIA|PR": ("4128534", "Ventania"),
    "MUNICIPIO DE VENTUROSA|PE": ("2616001", "Venturosa"),
    "MUNICIPIO DE VERA CRUZ DO OESTE|PR": ("4128559", "Vera Cruz do Oeste"),
    "MUNICIPIO DE VERA CRUZ|BA": ("2933208", "Vera Cruz"),
    "MUNICIPIO DE VERA CRUZ|RN": ("2414803", "Vera Cruz"),
    "MUNICIPIO DE VERA CRUZ|RS": ("4322707", "Vera Cruz"),
    "MUNICIPIO DE VERA CRUZ|SP": ("3556602", "Vera Cruz"),
    "MUNICIPIO DE VERANOPOLIS|RS": ("4322806", "Veranópolis"),
    "MUNICIPIO DE VERDEJANTE|PE": ("2616100", "Verdejante"),
    "MUNICIPIO DE VERDELANDIA|MG": ("3171030", "Verdelândia"),
    "MUNICIPIO DE VEREDA|BA": ("2933257", "Vereda"),
    "MUNICIPIO DE VERE|PR": ("4128609", "Verê"),
    "MUNICIPIO DE VERISSIMO|MG": ("3171105", "Veríssimo"),
    "MUNICIPIO DE VERMELHO NOVO|MG": ("3171154", "Vermelho Novo"),
    "MUNICIPIO DE VERTENTES|PE": ("2616209", "Vertentes"),
    "MUNICIPIO DE VESPASIANO|MG": ("3171204", "Vespasiano"),
    "MUNICIPIO DE VIADUTOS|RS": ("4322905", "Viadutos"),
    "MUNICIPIO DE VIAMAO|RS": ("4323002", "Viamão"),
    "MUNICIPIO DE VIANA|ES": ("3205101", "Viana"),
    "MUNICIPIO DE VIANA|MA": ("2112803", "Viana"),
    "MUNICIPIO DE VIANOPOLIS|GO": ("5222005", "Vianópolis"),
    "MUNICIPIO DE VICENCIA|PE": ("2616308", "Vicência"),
    "MUNICIPIO DE VICENTE DUTRA|RS": ("4323101", "Vicente Dutra"),
    "MUNICIPIO DE VICENTINA|MS": ("5008404", "Vicentina"),
    "MUNICIPIO DE VICENTINOPOLIS|GO": ("5222054", "Vicentinópolis"),
    "MUNICIPIO DE VICOSA|AL": ("2709400", "Viçosa"),
    "MUNICIPIO DE VICOSA|MG": ("3171303", "Viçosa"),
    "MUNICIPIO DE VICOSA|RN": ("2414902", "Viçosa"),
    "MUNICIPIO DE VICTOR GRAEFF|RS": ("4323200", "Victor Graeff"),
    "MUNICIPIO DE VIDAL RAMOS|SC": ("4219200", "Vidal Ramos"),
    "MUNICIPIO DE VIDEIRA|SC": ("4219309", "Videira"),
    "MUNICIPIO DE VIEIRAS|MG": ("3171402", "Vieiras"),
    "MUNICIPIO DE VIEIROPOLIS|PB": ("2517209", "Vieirópolis"),
    "MUNICIPIO DE VIGIA|PA": ("1508209", "Vigia"),
    "MUNICIPIO DE VILA BELA DA SANTISSIMA TRINDADE|MT": ("5105507", "Vila Bela da Santíssima Trindade"),
    "MUNICIPIO DE VILA BOA|GO": ("5222203", "Vila Boa"),
    "MUNICIPIO DE VILA FLORES|RS": ("4323309", "Vila Flores"),
    "MUNICIPIO DE VILA FLOR|RN": ("2415008", "Vila Flor"),
    "MUNICIPIO DE VILA MARIA|RS": ("4323408", "Vila Maria"),
    "MUNICIPIO DE VILA NOVA DO SUL|RS": ("4323457", "Vila Nova do Sul"),
    "MUNICIPIO DE VILA NOVA DOS MARTIRIOS|MA": ("2112852", "Vila Nova dos Martírios"),
    "MUNICIPIO DE VILA PAVAO|ES": ("3205150", "Vila Pavão"),
    "MUNICIPIO DE VILA PROPICIO|GO": ("5222302", "Vila Propício"),
    "MUNICIPIO DE VILA RICA|MT": ("5108600", "Vila Rica"),
    "MUNICIPIO DE VILA VALERIO|ES": ("3205176", "Vila Valério"),
    "MUNICIPIO DE VILA VELHA|ES": ("3205200", "Vila Velha"),
    "MUNICIPIO DE VILHENA|RO": ("1100304", "Vilhena"),
    "MUNICIPIO DE VINHEDO|SP": ("3556701", "Vinhedo"),
    "MUNICIPIO DE VIRADOURO|SP": ("3556800", "Viradouro"),
    "MUNICIPIO DE VIRGEM DA LAPA|MG": ("3171600", "Virgem da Lapa"),
    "MUNICIPIO DE VIRGINIA|MG": ("3171709", "Virgínia"),
    "MUNICIPIO DE VIRGINOPOLIS|MG": ("3171808", "Virginópolis"),
    "MUNICIPIO DE VIRMOND|PR": ("4128658", "Virmond"),
    "MUNICIPIO DE VISCONDE DO RIO BRANCO|MG": ("3172004", "Visconde do Rio Branco"),
    "MUNICIPIO DE VISTA ALEGRE DO PRATA|RS": ("4323606", "Vista Alegre do Prata"),
    "MUNICIPIO DE VISTA ALEGRE|RS": ("4323507", "Vista Alegre"),
    "MUNICIPIO DE VITOR MEIRELES|SC": ("4219358", "Vitor Meireles"),
    "MUNICIPIO DE VITORIA BRASIL|SP": ("3556958", "Vitória Brasil"),
    "MUNICIPIO DE VITORIA DA CONQUISTA|BA": ("2933307", "Vitória da Conquista"),
    "MUNICIPIO DE VITORIA DAS MISSOES|RS": ("4323754", "Vitória das Missões"),
    "MUNICIPIO DE VITORIA DE SANTO ANTAO|PE": ("2616407", "Vitória de Santo Antão"),
    "MUNICIPIO DE VITORIA DO JARI|AP": ("1600808", "Vitória do Jari"),
    "MUNICIPIO DE VITORIA DO MEARIM|MA": ("2112902", "Vitória do Mearim"),
    "MUNICIPIO DE VITORIA DO XINGU|PA": ("1508357", "Vitória do Xingu"),
    "MUNICIPIO DE VITORINO FREIRE|MA": ("2113009", "Vitorino Freire"),
    "MUNICIPIO DE VOLTA GRANDE|MG": ("3172103", "Volta Grande"),
    "MUNICIPIO DE VOLTA REDONDA|RJ": ("3306305", "Volta Redonda"),
    "MUNICIPIO DE VOTORANTIM|SP": ("3557006", "Votorantim"),
    "MUNICIPIO DE VOTUPORANGA|SP": ("3557105", "Votuporanga"),
    "MUNICIPIO DE WAGNER|BA": ("2933406", "Wagner"),
    "MUNICIPIO DE WANDERLANDIA|TO": ("1722081", "Wanderlândia"),
    "MUNICIPIO DE WENCESLAU BRAZ|MG": ("3172202", "Wenceslau Braz"),
    "MUNICIPIO DE WENCESLAU BRAZ|PR": ("4128500", "Wenceslau Braz"),
    "MUNICIPIO DE WENCESLAU GUIMARAES|BA": ("2933505", "Wenceslau Guimarães"),
    "MUNICIPIO DE WESTFALIA|RS": ("4323770", "Westfália"),
    "MUNICIPIO DE WITMARSUM|SC": ("4219408", "Witmarsum"),
    "MUNICIPIO DE XAMBIOA|TO": ("1722107", "Xambioá"),
    "MUNICIPIO DE XAMBRE|PR": ("4128807", "Xambrê"),
    "MUNICIPIO DE XANGRI-LA|RS": ("4323804", "Xangri-lá"),
    "MUNICIPIO DE XANXERE|SC": ("4219507", "Xanxerê"),
    "MUNICIPIO DE XAPURI|AC": ("1200708", "Xapuri"),
    "MUNICIPIO DE XAVANTINA|SC": ("4219606", "Xavantina"),
    "MUNICIPIO DE XAXIM|SC": ("4219705", "Xaxim"),
    "MUNICIPIO DE XEXEU|PE": ("2616506", "Xexéu"),
    "MUNICIPIO DE XINGUARA|PA": ("1508407", "Xinguara"),
    "MUNICIPIO DE XIQUE-XIQUE|BA": ("2933604", "Xique-Xique"),
    "MUNICIPIO DE ZABELE|PB": ("2517407", "Zabelê"),
    "MUNICIPIO DE ZE DOCA|MA": ("2114007", "Zé Doca"),
    "MUNICIPIO DE ZORTEA|SC": ("4219853", "Zortéa"),
    "MUNICIPIO DO BOM JARDIM|MA": ("2102002", "Bom Jardim"),
    "MUNICIPIO DO BOM JARDIM|PE": ("2602209", "Bom Jardim"),
    "MUNICIPIO DO BOM JARDIM|RJ": ("3300506", "Bom Jardim"),
    "MUNICIPIO DO BREJO DA MADRE DE DEUS|PE": ("2602605", "Brejo da Madre de Deus"),
    "MUNICIPIO DO CABO DE SANTO AGOSTINHO|PE": ("2602902", "Cabo de Santo Agostinho"),
    "MUNICIPIO DO CONDE|BA": ("2908606", "Conde"),
    "MUNICIPIO DO CONDE|PB": ("2504603", "Conde"),
    "MUNICIPIO DO CORREGO DO OURO|GO": ("5205703", "Córrego do Ouro"),
    "MUNICIPIO DO RECIFE|PE": ("2611606", "Recife"),
    "MUNICIPIO DO RIO GRANDE|RS": ("4315602", "Rio Grande"),
    "MUNICÍPIO DE ITABAIANA|PB": ("2506905", "Itabaiana"),
    "MUNICÍPIO DE ITABAIANA|SE": ("2802908", "Itabaiana"),
    "MURICILÂNDIA|TO": ("1713957", "Muricilândia"),
    "MUTUNÓPOLIS|GO": ("5214101", "Mutunópolis"),
    "NATUBA|PB": ("2509909", "Natuba"),
    "NAVEGANTES|SC": ("4211306", "Navegantes"),
    "NAZARÉ DO PIAUÍ|PI": ("2206704", "Nazaré do Piauí"),
    "NAZÁRIA|PI": ("2206720", "Nazária"),
    "NERÓPOLIS|GO": ("5214507", "Nerópolis"),
    "NINA RODRIGUES|MA": ("2107209", "Nina Rodrigues"),
    "NIQUELÂNDIA|GO": ("5214606", "Niquelândia"),
    "NOBRES|MT": ("5105903", "Nobres"),
    "NONOAI|RS": ("4312708", "Nonoai"),
    "NOSSA SENHORA DE LOURDES|SE": ("2804706", "Nossa Senhora de Lourdes"),
    "NOVA ALIANÇA DO IVAÍ|PR": ("4116505", "Nova Aliança do Ivaí"),
    "NOVA ALVORADA|RS": ("4312757", "Nova Alvorada"),
    "NOVA AMÉRICA DA COLINA|PR": ("4116604", "Nova América da Colina"),
    "NOVA CANAÃ|BA": ("2922706", "Nova Canaã"),
    "NOVA ESPERANÇA DO SUL|RS": ("4313037", "Nova Esperança do Sul"),
    "NOVA IBIÁ|BA": ("2922755", "Nova Ibiá"),
    "NOVA LACERDA|MT": ("5106182", "Nova Lacerda"),
    "NOVA MUTUM|MT": ("5106224", "Nova Mutum"),
    "NOVA OLÍMPIA|MT": ("5106232", "Nova Olímpia"),
    "NOVA OLÍMPIA|PR": ("4117206", "Nova Olímpia"),
    "NOVA PALMA|RS": ("4313102", "Nova Palma"),
    "NOVA PORTEIRINHA|MG": ("3145059", "Nova Porteirinha"),
    "NOVA REDENÇÃO|BA": ("2922854", "Nova Redenção"),
    "NOVA ROMA|GO": ("5214903", "Nova Roma"),
    "NOVA TEBAS|PR": ("4117271", "Nova Tebas"),
    "NOVA VIÇOSA|BA": ("2923001", "Nova Viçosa"),
    "NOVO HORIZONTE|BA": ("2923035", "Novo Horizonte"),
    "NOVO HORIZONTE|SC": ("4211652", "Novo Horizonte"),
    "NOVO HORIZONTE|SP": ("3533502", "Novo Horizonte"),
    "NOVO JARDIM|TO": ("1715259", "Novo Jardim"),
    "NOVO MACHADO|RS": ("4313425", "Novo Machado"),
    "NOVO SANTO ANTÔNIO|MT": ("5106315", "Novo Santo Antônio"),
    "NOVO SANTO ANTÔNIO|PI": ("2206951", "Novo Santo Antônio"),
    "OLHO D'ÁGUA DAS FLORES|AL": ("2705705", "Olho d'Água das Flores"),
    "OLHO D'ÁGUA DO PIAUÍ|PI": ("2207108", "Olho D'Água do Piauí"),
    "ORIENTE|SP": ("3534104", "Oriente"),
    "ORÓS|CE": ("2309508", "Orós"),
    "OSCAR BRESSANE|SP": ("3534500", "Oscar Bressane"),
    "OURO BRANCO|AL": ("2706109", "Ouro Branco"),
    "OURO BRANCO|MG": ("3145901", "Ouro Branco"),
    "OURO BRANCO|RN": ("2408508", "Ouro Branco"),
    "OUVIDOR|GO": ("5215504", "Ouvidor"),
    "PACATUBA|CE": ("2309706", "Pacatuba"),
    "PACATUBA|SE": ("2804904", "Pacatuba"),
    "PACOTI|CE": ("2309805", "Pacoti"),
    "PADRE MARCOS|PI": ("2207207", "Padre Marcos"),
    "PAINS|MG": ("3146503", "Pains"),
    "PALESTINA DE GOIÁS|GO": ("5215652", "Palestina de Goiás"),
    "PALESTINA|AL": ("2706208", "Palestina"),
    "PALESTINA|SP": ("3535002", "Palestina"),
    "PALMARES|PE": ("2610004", "Palmares"),
    "PALMAS|PR": ("4117602", "Palmas"),
    "PALMAS|TO": ("1721000", "Palmas"),
    "PALMEIRÂNDIA|MA": ("2107605", "Palmeirândia"),
    "PALMITINHO|RS": ("4313805", "Palmitinho"),
    "PALMÁCIA|CE": ("2310100", "Palmácia"),
    "PARARI|PB": ("2510659", "Parari"),
    "PARAUAPEBAS|PA": ("1505536", "Parauapebas"),
    "PARNARAMA|MA": ("2107803", "Parnarama"),
    "PASSABÉM|MG": ("3147501", "Passabém"),
    "PASSAGEM FRANCA|MA": ("2107902", "Passagem Franca"),
    "PASSAGEM|PB": ("2510709", "Passagem"),
    "PASSAGEM|RN": ("2409209", "Passagem"),
    "PATO BRAGADO|PR": ("4118451", "Pato Bragado"),
    "PAULA FREITAS|PR": ("4118600", "Paula Freitas"),
    "PAULISTA|PB": ("2510907", "Paulista"),
    "PAULISTA|PE": ("2610707", "Paulista"),
    "PAULISTÂNIA|SP": ("3536570", "Paulistânia"),
    "PEDRA BRANCA|CE": ("2310506", "Pedra Branca"),
    "PEDRA BRANCA|PB": ("2511004", "Pedra Branca"),
    "PEDRAS DE FOGO|PB": ("2511202", "Pedras de Fogo"),
    "PEDRO II|PI": ("2207900", "Pedro II"),
    "PEDRO VELHO|RN": ("2409803", "Pedro Velho"),
    "PEDRÃO|BA": ("2924108", "Pedrão"),
    "PEJUÇARA|RS": ("4314308", "Pejuçara"),
    "PEREIRO|CE": ("2310803", "Pereiro"),
    "PIACATU|SP": ("3537701", "Piacatu"),
    "PIAÇABUÇU|AL": ("2706802", "Piaçabuçu"),
    "PIEDADE DO RIO GRANDE|MG": ("3150307", "Piedade do Rio Grande"),
    "PILAR|AL": ("2706901", "Pilar"),
    "PILAR|PB": ("2511509", "Pilar"),
    "PINDOBAÇU|BA": ("2924603", "Pindobaçu"),
    "PINDORAMA DO TOCANTINS|TO": ("1717008", "Pindorama do Tocantins"),
    "PINHALZINHO|SC": ("4212908", "Pinhalzinho"),
    "PINHALZINHO|SP": ("3538204", "Pinhalzinho"),
    "PINHEIRINHO DO VALE|RS": ("4314498", "Pinheirinho do Vale"),
    "PINTADAS|BA": ("2924652", "Pintadas"),
    "PIRACURUCA|PI": ("2208304", "Piracuruca"),
    "PIRAPORA DO BOM JESUS|SP": ("3539103", "Pirapora do Bom Jesus"),
    "PIRAPÓ|RS": ("4314555", "Pirapó"),
    "PIRITIBA|BA": ("2924801", "Piritiba"),
    "PIRPIRITUBA|PB": ("2511806", "Pirpirituba"),
    "PITIMBU|PB": ("2511905", "Pitimbu"),
    "PLANALTINO|BA": ("2924900", "Planaltino"),
    "PLANALTO DA SERRA|MT": ("5106455", "Planalto da Serra"),
    "PLANALTO|BA": ("2925006", "Planalto"),
    "PLANALTO|PR": ("4119806", "Planalto"),
    "PLANALTO|RS": ("4314704", "Planalto"),
    "PLANALTO|SP": ("3539608", "Planalto"),
    "POCONÉ|MT": ("5106505", "Poconé"),
    "POMBAL|PB": ("2512101", "Pombal"),
    "PONTAL|SP": ("3540200", "Pontal"),
    "PONTES E LACERDA|MT": ("5106752", "Pontes e Lacerda"),
    "PORANGA|CE": ("2311009", "Poranga"),
    "PORTEIRÃO|GO": ("5218052", "Porteirão"),
    "PORTEL|PA": ("1505809", "Portel"),
    "PORTO REAL DO COLÉGIO|AL": ("2707503", "Porto Real do Colégio"),
    "PORTO RICO DO MARANHÃO|MA": ("2109056", "Porto Rico do Maranhão"),
    "PORTO VERA CRUZ|RS": ("4315073", "Porto Vera Cruz"),
    "PORTO VITÓRIA|PR": ("4120309", "Porto Vitória"),
    "PREFEITURA MUNICIPAL DE PARANAPANEMA|SP": ("3535804", "Paranapanema"),
    "PREFEITURA MUNICIPAL DE QUERENCIA|MT": ("5107065", "Querência"),
    "PRESIDENTE JUSCELINO|MA": ("2109205", "Presidente Juscelino"),
    "PRESIDENTE JUSCELINO|MG": ("3153202", "Presidente Juscelino"),
    "PRESIDENTE MÉDICI|MA": ("2109239", "Presidente Médici"),
    "PRESIDENTE MÉDICI|RO": ("1100254", "Presidente Médici"),
    "PRESIDENTE SARNEY|MA": ("2109270", "Presidente Sarney"),
    "PRINCESA ISABEL|PB": ("2512309", "Princesa Isabel"),
    "PÃO DE AÇÚCAR|AL": ("2706406", "Pão de Açúcar"),
    "QUADRA|SP": ("3541653", "Quadra"),
    "QUATRO IRMÃOS|RS": ("4315313", "Quatro Irmãos"),
    "QUEIMADAS|BA": ("2925808", "Queimadas"),
    "QUEIMADAS|PB": ("2512507", "Queimadas"),
    "QUIXABÁ|PB": ("2512606", "Quixaba"),
    "QUIXABÁ|PE": ("2611533", "Quixaba"),
    "RAFAEL JAMBEIRO|BA": ("2925956", "Rafael Jambeiro"),
    "REDENTORA|RS": ("4315404", "Redentora"),
    "RESENDE|RJ": ("3304201", "Resende"),
    "RESERVA DO CABAÇAL|MT": ("5107156", "Reserva do Cabaçal"),
    "RESTINGA SECA|RS": ("4315503", "Restinga Sêca"),
    "RIACHO DAS ALMAS|PE": ("2611705", "Riacho das Almas"),
    "RIACHÃO DO POÇO|PB": ("2512762", "Riachão do Poço"),
    "RIBEIRA DO PIAUÍ|PI": ("2208874", "Ribeira do Piauí"),
    "RIBEIRA DO POMBAL|BA": ("2926608", "Ribeira do Pombal"),
    "RIBEIRÃO VERMELHO|MG": ("3154705", "Ribeirão Vermelho"),
    "RIBEIRÃO|PE": ("2611804", "Ribeirão"),
    "RINÓPOLIS|SP": ("3543808", "Rinópolis"),
    "RIO DO FOGO|RN": ("2408953", "Rio do Fogo"),
    "RIO DOCE|MG": ("3155009", "Rio Doce"),
    "RIO DOS ÍNDIOS|RS": ("4315552", "Rio dos Índios"),
    "RIO NEGRO|MS": ("5007307", "Rio Negro"),
    "RIO NEGRO|PR": ("4122305", "Rio Negro"),
    "RIOLÂNDIA|SP": ("3544202", "Riolândia"),
    "ROCA SALES|RS": ("4315800", "Roca Sales"),
    "ROLADOR|RS": ("4315958", "Rolador"),
    "ROSEIRA|SP": ("3544301", "Roseira"),
    "ROSÁRIO DA LIMEIRA|MG": ("3156452", "Rosário da Limeira"),
    "ROSÁRIO|MA": ("2109601", "Rosário"),
    "ROTEIRO|AL": ("2707800", "Roteiro"),
    "RUY BARBOSA|BA": ("2927200", "Ruy Barbosa"),
    "RUY BARBOSA|RN": ("2411106", "Ruy Barbosa"),
    "SALDANHA MARINHO|RS": ("4316436", "Saldanha Marinho"),
    "SALGADINHO|PB": ("2513000", "Salgadinho"),
    "SALGADINHO|PE": ("2612109", "Salgadinho"),
    "SALITRE|CE": ("2311959", "Salitre"),
    "SALTINHO|SC": ("4215356", "Saltinho"),
    "SALTINHO|SP": ("3545159", "Saltinho"),
    "SALTO DO CÉU|MT": ("5107750", "Salto do Céu"),
    "SANTA CLARA DO SUL|RS": ("4316758", "Santa Clara do Sul"),
    "SANTA CRUZ DA VITÓRIA|BA": ("2927804", "Santa Cruz da Vitória"),
    "SANTA CRUZ DO ARARI|PA": ("1506401", "Santa Cruz do Arari"),
    "SANTA CRUZ DO CAPIBARIBE|PE": ("2612505", "Santa Cruz do Capibaribe"),
    "SANTA CRUZ|PB": ("2513208", "Santa Cruz"),
    "SANTA CRUZ|PE": ("2612455", "Santa Cruz"),
    "SANTA CRUZ|RN": ("2411205", "Santa Cruz"),
    "SANTA FILOMENA DO MARANHÃO|MA": ("2109759", "Santa Filomena do Maranhão"),
    "SANTA FILOMENA|PE": ("2612554", "Santa Filomena"),
    "SANTA FILOMENA|PI": ("2209203", "Santa Filomena"),
    "SANTA HELENA|MA": ("2109809", "Santa Helena"),
    "SANTA HELENA|PB": ("2513307", "Santa Helena"),
    "SANTA HELENA|PR": ("4123501", "Santa Helena"),
    "SANTA HELENA|SC": ("4215554", "Santa Helena"),
    "SANTA INÊS|BA": ("2927903", "Santa Inês"),
    "SANTA INÊS|MA": ("2109908", "Santa Inês"),
    "SANTA INÊS|PB": ("2513356", "Santa Inês"),
    "SANTA INÊS|PR": ("4123600", "Santa Inês"),
    "SANTA ISABEL DO PARÁ|PA": ("1506500", "Santa Izabel do Pará"),
    "SANTA LUZIA DO PARUÁ|MA": ("2110039", "Santa Luzia do Paruá"),
    "SANTA LUZIA|BA": ("2928059", "Santa Luzia"),
    "SANTA LUZIA|MA": ("2110005", "Santa Luzia"),
    "SANTA LUZIA|MG": ("3157807", "Santa Luzia"),
    "SANTA LUZIA|PB": ("2513406", "Santa Luzia"),
    "SANTA MERCEDES|SP": ("3547106", "Santa Mercedes"),
    "SANTA MÔNICA|PR": ("4123956", "Santa Mônica"),
    "SANTA RITA DO ITUETO|MG": ("3159506", "Santa Rita do Itueto"),
    "SANTA RITA DO TRIVELATO|MT": ("5107768", "Santa Rita do Trivelato"),
    "SANTA RITA|MA": ("2110203", "Santa Rita"),
    "SANTA RITA|PB": ("2513703", "Santa Rita"),
    "SANTA ROSA DE LIMA|SC": ("4215604", "Santa Rosa de Lima"),
    "SANTA ROSA DE LIMA|SE": ("2806503", "Santa Rosa de Lima"),
    "SANTA ROSA DO PIAUÍ|PI": ("2209377", "Santa Rosa do Piauí"),
    "SANTA TERESA|ES": ("3204609", "Santa Teresa"),
    "SANTA TEREZA DO OESTE|PR": ("4124020", "Santa Tereza do Oeste"),
    "SANTA TEREZINHA DE ITAIPU|PR": ("4124053", "Santa Terezinha de Itaipu"),
    "SANTANA DE MANGUEIRA|PB": ("2513505", "Santana de Mangueira"),
    "SANTANA DO MUNDAÚ|AL": ("2708105", "Santana do Mundaú"),
    "SANTANA DOS GARROTES|PB": ("2513604", "Santana dos Garrotes"),
    "SANTO AMARO DO MARANHÃO|MA": ("2110278", "Santo Amaro do Maranhão"),
    "SANTO ANTÔNIO DE LISBOA|PI": ("2209401", "Santo Antônio de Lisboa"),
    "SANTO ANTÔNIO DOS LOPES|MA": ("2110302", "Santo Antônio dos Lopes"),
    "SANTO ANTÔNIO DOS MILAGRES|PI": ("2209450", "Santo Antônio dos Milagres"),
    "SANTO ESTÊVÃO|BA": ("2928802", "Santo Estêvão"),
    "SAPEAÇU|BA": ("2929602", "Sapeaçu"),
    "SAPEZAL|MT": ("5107875", "Sapezal"),
    "SAQUAREMA|RJ": ("3305505", "Saquarema"),
    "SATUBA|AL": ("2708907", "Satuba"),
    "SENADOR CORTES|MG": ("3165602", "Senador Cortes"),
    "SENADOR FIRMINO|MG": ("3165701", "Senador Firmino"),
    "SENADOR GEORGINO AVELINO|RN": ("2413201", "Senador Georgino Avelino"),
    "SENTO SÉ|BA": ("2930204", "Sento Sé"),
    "SERITINGA|MG": ("3166402", "Seritinga"),
    "SERRA DO MEL|RN": ("2413359", "Serra do Mel"),
    "SERRA GRANDE|PB": ("2515708", "Serra Grande"),
    "SERRA REDONDA|PB": ("2515807", "Serra Redonda"),
    "SERRANÓPOLIS DO IGUAÇU|PR": ("4126355", "Serranópolis do Iguaçu"),
    "SERRANÓPOLIS|GO": ("5220504", "Serranópolis"),
    "SERRITA|PE": ("2614006", "Serrita"),
    "SEVERIANO DE ALMEIDA|RS": ("4320602", "Severiano de Almeida"),
    "SILVA JARDIM|RJ": ("3305604", "Silva Jardim"),
    "SIMÕES FILHO|BA": ("2930709", "Simões Filho"),
    "SIRIRI|SE": ("2807204", "Siriri"),
    "SOLONÓPOLE|CE": ("2313005", "Solonópole"),
    "SOLÂNEA|PB": ("2516003", "Solânea"),
    "SUMÉ|PB": ("2516300", "Sumé"),
    "SUSSUAPARA|PI": ("2210938", "Sussuapara"),
    "SÃO BENTO DO TRAIRÍ|RN": ("2411700", "São Bento do Trairí"),
    "SÃO BENTO|MA": ("2110500", "São Bento"),
    "SÃO BENTO|PB": ("2513901", "São Bento"),
    "SÃO DOMINGOS DO AZEITÃO|MA": ("2110658", "São Domingos do Azeitão"),
    "SÃO DOMINGOS DO CARIRI|PB": ("2513943", "São Domingos do Cariri"),
    "SÃO DOMINGOS DO NORTE|ES": ("3204658", "São Domingos do Norte"),
    "SÃO DOMINGOS DO SUL|RS": ("4318051", "São Domingos do Sul"),
    "SÃO DOMINGOS|BA": ("2928950", "São Domingos"),
    "SÃO DOMINGOS|GO": ("5219803", "São Domingos"),
    "SÃO DOMINGOS|PB": ("2513968", "São Domingos"),
    "SÃO DOMINGOS|SC": ("4216107", "São Domingos"),
    "SÃO DOMINGOS|SE": ("2806800", "São Domingos"),
    "SÃO FRANCISCO DE ITABAPOANA|RJ": ("3304755", "São Francisco de Itabapoana"),
    "SÃO FRANCISCO DO MARANHÃO|MA": ("2110906", "São Francisco do Maranhão"),
    "SÃO FRANCISCO DO PARÁ|PA": ("1507409", "São Francisco do Pará"),
    "SÃO GABRIEL DA CACHOEIRA|AM": ("1303809", "São Gabriel da Cachoeira"),
    "SÃO GABRIEL|BA": ("2929255", "São Gabriel"),
    "SÃO GABRIEL|RS": ("4318309", "São Gabriel"),
    "SÃO JORGE|RS": ("4318440", "São Jorge"),
    "SÃO JOSÉ DA BOA VISTA|PR": ("4125407", "São José da Boa Vista"),
    "SÃO JOSÉ DA LAGOA TAPADA|PB": ("2514206", "São José da Lagoa Tapada"),
    "SÃO JOSÉ DAS MISSÕES|RS": ("4318457", "São José das Missões"),
    "SÃO JOSÉ DAS PALMEIRAS|PR": ("4125456", "São José das Palmeiras"),
    "SÃO JOSÉ DE CAIANA|PB": ("2514305", "São José de Caiana"),
    "SÃO JOSÉ DO BREJO DO CRUZ|PB": ("2514651", "São José do Brejo do Cruz"),
    "SÃO JOSÉ DO INHACORÁ|RS": ("4318499", "São José do Inhacorá"),
    "SÃO JOSÉ DO OURO|RS": ("4318606", "São José do Ouro"),
    "SÃO JOSÉ DO PEIXE|PI": ("2210102", "São José do Peixe"),
    "SÃO JOSÉ DOS AUSENTES|RS": ("4318622", "São José dos Ausentes"),
    "SÃO JOSÉ DOS BASÍLIOS|MA": ("2111250", "São José dos Basílios"),
    "SÃO JOÃO BATISTA DO GLÓRIA|MG": ("3162203", "São João Batista do Glória"),
    "SÃO JOÃO DA SERRA|PI": ("2209906", "São João da Serra"),
    "SÃO JOÃO DA VARJOTA|PI": ("2209955", "São João da Varjota"),
    "SÃO JOÃO DE IRACEMA|SP": ("3549250", "São João de Iracema"),
    "SÃO JOÃO DO CARIRI|PB": ("2514008", "São João do Cariri"),
    "SÃO JOÃO DO JAGUARIBE|CE": ("2312502", "São João do Jaguaribe"),
    "SÃO JOÃO DO POLÊSINE|RS": ("4318432", "São João do Polêsine"),
    "SÃO LUIZ GONZAGA|RS": ("4318903", "São Luiz Gonzaga"),
    "SÃO MANUEL|SP": ("3550100", "São Manuel"),
    "SÃO MIGUEL DO PASSA QUATRO|GO": ("5220264", "São Miguel do Passa Quatro"),
    "SÃO PAULO DAS MISSÕES|RS": ("4319307", "São Paulo das Missões"),
    "SÃO PEDRO DA ÁGUA BRANCA|MA": ("2111532", "São Pedro da Água Branca"),
    "SÃO PEDRO DO TURVO|SP": ("3550506", "São Pedro do Turvo"),
    "SÃO PEDRO DOS CRENTES|MA": ("2111573", "São Pedro dos Crentes"),
    "SÃO PEDRO|RN": ("2412708", "São Pedro"),
    "SÃO PEDRO|SP": ("3550407", "São Pedro"),
    "SÃO SEBASTIÃO DO ALTO|RJ": ("3305307", "São Sebastião do Alto"),
    "SÃO THOMÉ DAS LETRAS|MG": ("3165206", "São Tomé das Letras"),
    "SÃO VENDELINO|RS": ("4319752", "São Vendelino"),
    "SÃO VICENTE DO SERIDÓ|PB": ("2515401", "São Vicente do Seridó"),
    "SÃO VICENTE FERRER|PE": ("2613800", "São Vicente Férrer"),
    "TABULEIRO|MG": ("3167905", "Tabuleiro"),
    "TAMANDARÉ|PE": ("2614857", "Tamandaré"),
    "TANQUINHO|BA": ("2931103", "Tanquinho"),
    "TAPARUBA|MG": ("3168051", "Taparuba"),
    "TAPERA|RS": ("4321006", "Tapera"),
    "TAPEROÁ|BA": ("2931202", "Taperoá"),
    "TAPEROÁ|PB": ("2516508", "Taperoá"),
    "TAPIRAÍ|MG": ("3168200", "Tapiraí"),
    "TAPIRAÍ|SP": ("3553500", "Tapiraí"),
    "TAPURAH|MT": ("5108006", "Tapurah"),
    "TAQUARAL DE GOIÁS|GO": ("5221007", "Taquaral de Goiás"),
    "TAQUARANA|AL": ("2709103", "Taquarana"),
    "TARUMÃ|SP": ("3553955", "Tarumã"),
    "TEIXEIRA DE FREITAS|BA": ("2931350", "Teixeira de Freitas"),
    "TEIXEIRA|PB": ("2516706", "Teixeira"),
    "TENENTE ANANIAS|RN": ("2414100", "Tenente Ananias"),
    "TEODORO SAMPAIO|BA": ("2931400", "Teodoro Sampaio"),
    "TEODORO SAMPAIO|SP": ("3554300", "Teodoro Sampaio"),
    "TEOLÂNDIA|BA": ("2931608", "Teolândia"),
    "TERENOS|MS": ("5008008", "Terenos"),
    "TESOURO|MT": ("5108105", "Tesouro"),
    "TIBAU|RN": ("2411056", "Tibau"),
    "TIRADENTES DO SUL|RS": ("4321477", "Tiradentes do Sul"),
    "TRAIPU|AL": ("2709202", "Traipu"),
    "TRIZIDELA DO VALE|MA": ("2112233", "Trizidela do Vale"),
    "TRÊS ARROIOS|RS": ("4321634", "Três Arroios"),
    "TRÊS BARRAS|SC": ("4218301", "Três Barras"),
    "TUBARÃO|SC": ("4218707", "Tubarão"),
    "TUCANO|BA": ("2931905", "Tucano"),
    "TUCURUÍ|PA": ("1508100", "Tucuruí"),
    "TUIUTI|SP": ("3554953", "Tuiuti"),
    "TUPANATINGA|PE": ("2615805", "Tupanatinga"),
    "TUPARENDI|RS": ("4322301", "Tuparendi"),
    "TURIAÇU|MA": ("2112407", "Turiaçu"),
    "TURMALINA|MG": ("3169703", "Turmalina"),
    "TURMALINA|SP": ("3555307", "Turmalina"),
    "TURVO|PR": ("4127965", "Turvo"),
    "TURVO|SC": ("4218806", "Turvo"),
    "UBARANA|SP": ("3555356", "Ubarana"),
    "UBIRETAMA|RS": ("4322343", "Ubiretama"),
    "UIRAÚNA|PB": ("2516904", "Uiraúna"),
    "UMARI|CE": ("2313708", "Umari"),
    "UMIRIM|CE": ("2313757", "Umirim"),
    "UNIÃO DOS PALMARES|AL": ("2709301", "União dos Palmares"),
    "URUCARÁ|AM": ("1304302", "Urucará"),
    "URUPÊS|SP": ("3556008", "Urupês"),
    "URUTAÍ|GO": ("5221809", "Urutaí"),
    "URUÇUÍ|PI": ("2211209", "Uruçuí"),
    "URÂNIA|SP": ("3555802", "Urânia"),
    "VALENÇA|BA": ("2932903", "Valença"),
    "VALENÇA|RJ": ("3306107", "Valença"),
    "VARGEM GRANDE PAULISTA|SP": ("3556453", "Vargem Grande Paulista"),
    "VARGEM GRANDE|MA": ("2112704", "Vargem Grande"),
    "VERA CRUZ|BA": ("2933208", "Vera Cruz"),
    "VERA CRUZ|RN": ("2414803", "Vera Cruz"),
    "VERA CRUZ|RS": ("4322707", "Vera Cruz"),
    "VERA CRUZ|SP": ("3556602", "Vera Cruz"),
    "VERA MENDES|PI": ("2211506", "Vera Mendes"),
    "VERA|MT": ("5108501", "Vera"),
    "VEREDINHA|MG": ("3171071", "Veredinha"),
    "VERTENTE DO LÉRIO|PE": ("2616183", "Vertente do Lério"),
    "VESPASIANO CORREA|RS": ("4322855", "Vespasiano Corrêa"),
    "VIANA|ES": ("3205101", "Viana"),
    "VIANA|MA": ("2112803", "Viana"),
    "VILA LÂNGARO|RS": ("4323358", "Vila Lângaro"),
    "VILA NOVA DO PIAUÍ|PI": ("2211605", "Vila Nova do Piauí"),
    "VIRGOLÂNDIA|MG": ("3171907", "Virgolândia"),
    "VISTA SERRANA|PB": ("2505501", "Vista Serrana"),
    "VITÓRIA|ES": ("3205309", "Vitória"),
    "VIÇOSA DO CEARÁ|CE": ("2314102", "Viçosa do Ceará"),
    "VIÇOSA|AL": ("2709400", "Viçosa"),
    "VIÇOSA|MG": ("3171303", "Viçosa"),
    "VIÇOSA|RN": ("2414902", "Viçosa"),
    "VÁRZEA ALEGRE|CE": ("2314003", "Várzea Alegre"),
    "VÁRZEA|PB": ("2517100", "Várzea"),
    "VÁRZEA|RN": ("2414704", "Várzea"),
    "WALL FERRAZ|PI": ("2211704", "Wall Ferraz"),
    "WANDERLEY|BA": ("2933455", "Wanderley"),
    "ÁGUA BRANCA|AL": ("2700102", "Água Branca"),
    "ÁGUA BRANCA|PB": ("2500106", "Água Branca"),
    "ÁGUA BRANCA|PI": ("2200202", "Água Branca"),
    "ÁGUA FRIA|BA": ("2900405", "Água Fria"),
    "ÁGUAS DE SANTA BÁRBARA|SP": ("3500550", "Águas de Santa Bárbara"),
    "ÁLVARES FLORENCE|SP": ("3501202", "Álvares Florence"),
    "ÓLEO|SP": ("3533809", "Óleo"),
}


# ===========================================================================
# CENTROIDES DE MUNICIPIOS  (lon,lat) por codigo IBGE de 7 digitos.
# Gerado das malhas municipais tbrugz/geodata-br. Usado para posicionar os
# pins de prospeccao sobre cada municipio (projetado pela bbox nacional).
# ===========================================================================

_MUNI_CENTROIDE = {
    "1100015": [-62.41, -12.5103],
    "1100023": [-62.9379, -9.9418],
    "1100031": [-60.6227, -13.5378],
    "1100049": [-61.4295, -11.4593],
    "1100056": [-61.4213, -13.1935],
    "1100064": [-60.505, -13.1231],
    "1100072": [-61.0952, -12.8846],
    "1100080": [-64.0772, -12.0832],
    "1100098": [-60.7297, -11.4446],
    "1100106": [-64.4489, -11.3455],
    "1100114": [-62.6682, -10.6459],
    "1100122": [-61.7602, -10.4461],
    "1100130": [-62.0864, -9.2077],
    "1100148": [-62.1869, -11.5244],
    "1100155": [-62.062, -10.4945],
    "1100189": [-60.8159, -11.7725],
    "1100205": [-64.3388, -9.1937],
    "1100254": [-61.9798, -11.1937],
    "1100262": [-62.7146, -9.6943],
    "1100288": [-61.776, -11.737],
    "1100296": [-61.757, -12.0581],
    "1100304": [-60.2667, -11.9716],
    "1100320": [-62.9754, -11.6571],
    "1100338": [-64.4949, -10.5221],
    "1100346": [-62.5697, -11.2756],
    "1100379": [-61.9956, -12.7395],
    "1100403": [-63.5563, -9.6522],
    "1100452": [-63.9795, -10.07],
    "1100502": [-62.0736, -11.6867],
    "1100601": [-63.0908, -10.3331],
    "1100700": [-63.8327, -10.4718],
    "1100809": [-63.3737, -8.9465],
    "1100908": [-61.8522, -11.4552],
    "1100924": [-60.9518, -12.641],
    "1100940": [-62.5292, -8.9986],
    "1101005": [-63.0825, -10.7776],
    "1101104": [-63.1463, -9.0307],
    "1101203": [-61.6219, -11.2124],
    "1101302": [-62.8447, -11.1189],
    "1101401": [-63.3447, -10.2735],
    "1101435": [-62.536, -10.9134],
    "1101450": [-61.3389, -12.2896],
    "1101468": [-61.5501, -13.2091],
    "1101476": [-61.3022, -11.9027],
    "1101484": [-61.4708, -11.898],
    "1101492": [-63.1034, -12.4125],
    "1101500": [-63.2193, -11.903],
    "1101559": [-62.29, -10.9566],
    "1101609": [-62.2717, -10.0935],
    "1101708": [-62.3811, -11.0889],
    "1101757": [-61.9403, -9.7157],
    "1101807": [-62.0759, -10.256],
    "1200013": [-66.8662, -10.0311],
    "1200054": [-69.963, -10.7711],
    "1200104": [-69.1099, -10.8102],
    "1200138": [-68.1583, -9.6119],
    "1200179": [-67.8407, -10.4641],
    "1200203": [-72.7585, -7.9818],
    "1200252": [-68.6221, -10.8552],
    "1200302": [-71.1448, -8.9392],
    "1200328": [-71.7969, -9.1233],
    "1200336": [-73.4571, -7.5634],
    "1200344": [-69.939, -9.36],
    "1200351": [-72.6873, -9.1679],
    "1200385": [-67.3422, -10.3114],
    "1200393": [-72.7815, -8.431],
    "1200401": [-68.2023, -10.0814],
    "1200427": [-73.1146, -7.841],
    "1200435": [-70.4026, -9.5109],
    "1200450": [-67.552, -10.0596],
    "1200500": [-69.645, -10.1292],
    "1200609": [-71.4018, -8.4495],
    "1200708": [-68.5159, -10.6135],
    "1200807": [-67.7266, -9.6626],
    "1300029": [-65.2673, -3.688],
    "1300060": [-68.244, -3.3661],
    "1300086": [-61.6944, -3.4875],
    "1300102": [-62.1367, -4.2031],
    "1300144": [-59.451, -7.7767],
    "1300300": [-59.6898, -3.8784],
    "1300680": [-57.6251, -3.1351],
    "1300706": [-67.9393, -8.5492],
    "1300805": [-59.2699, -5.0741],
    "1301001": [-67.3321, -5.1211],
    "1301100": [-60.197, -3.8231],
    "1301209": [-64.1191, -3.9678],
    "1301308": [-63.1442, -3.1887],
    "1301407": [-70.3336, -7.1015],
    "1301506": [-70.1035, -7.5091],
    "1301605": [-66.2443, -2.5189],
    "1301704": [-62.3511, -7.4238],
    "1301902": [-58.7606, -3.182],
    "1302009": [-58.5723, -2.5115],
    "1302207": [-66.2394, -3.2653],
    "1302405": [-66.0845, -8.1515],
    "1302504": [-61.0048, -3.2316],
    "1302603": [-60.1442, -2.6691],
    "1302702": [-61.5155, -7.0358],
    "1302801": [-64.8029, -2.6442],
    "1303007": [-57.883, -1.3784],
    "1303304": [-60.6054, -6.9797],
    "1303403": [-56.9009, -2.6546],
    "1303502": [-68.011, -7.6559],
    "1303536": [-59.9812, -1.2363],
    "1303569": [-59.6303, -2.4833],
    "1303700": [-68.7547, -3.0857],
    "1303809": [-68.0903, 0.3318],
    "1303908": [-69.5534, -4.5979],
    "1303957": [-58.7579, -1.9104],
    "1304005": [-58.6127, -2.7753],
    "1304104": [-65.7462, -6.2539],
    "1304203": [-65.632, -4.5364],
    "1304237": [-67.8195, -2.6592],
    "1304260": [-65.4085, -3.1246],
    "1304302": [-58.8231, -1.1639],
    "1304401": [-57.7433, -2.8444],
    "1400027": [-62.5529, 3.7769],
    "1400050": [-62.6675, 3.0779],
    "1400100": [-60.6357, 3.1706],
    "1400159": [-60.0568, 2.7253],
    "1400175": [-60.4534, 2.2304],
    "1400209": [-61.1113, 0.8341],
    "1400233": [-59.4891, 1.1958],
    "1400282": [-62.5738, 2.2512],
    "1400308": [-61.8994, 2.5907],
    "1400407": [-60.0543, 3.8691],
    "1400456": [-60.8481, 4.1746],
    "1400472": [-60.8939, -0.0126],
    "1400506": [-59.8692, 0.7239],
    "1400605": [-60.1361, 0.8446],
    "1400704": [-60.2361, 4.6597],
    "1500107": [-48.9139, -1.7308],
    "1500206": [-48.4523, -2.0748],
    "1500305": [-50.7312, -0.364],
    "1500347": [-50.5885, -6.6341],
    "1500404": [-54.9051, -1.0283],
    "1500503": [-53.943, 0.5791],
    "1500602": [-53.7162, -5.8869],
    "1500701": [-50.0479, -0.853],
    "1500800": [-48.3806, -1.3478],
    "1500859": [-51.3341, -3.8956],
    "1500909": [-46.4664, -1.0022],
    "1500958": [-47.7563, -2.2883],
    "1501105": [-50.1584, -2.4224],
    "1501204": [-49.7266, -3.2413],
    "1501402": [-48.4301, -1.3441],
    "1501451": [-55.0336, -3.4736],
    "1501501": [-48.2874, -1.3246],
    "1501576": [-48.8056, -4.9806],
    "1501600": [-47.3102, -1.4194],
    "1501725": [-52.7402, -3.3679],
    "1501758": [-48.4577, -5.7513],
    "1501782": [-49.2406, -3.7535],
    "1501907": [-48.0644, -1.647],
    "1501956": [-46.4312, -1.956],
    "1502103": [-49.5023, -2.2928],
    "1502152": [-50.0871, -6.4966],
    "1502202": [-47.0969, -1.1155],
    "1502301": [-47.1877, -1.9722],
    "1502400": [-47.8498, -1.2892],
    "1502707": [-49.5748, -8.1492],
    "1502764": [-51.2008, -8.4159],
    "1502855": [-55.1022, -1.7385],
    "1502905": [-47.8638, -0.7014],
    "1503002": [-57.8191, -1.1069],
    "1503044": [-49.5248, -7.5632],
    "1503093": [-48.8879, -3.9335],
    "1503101": [-51.5911, -1.1634],
    "1503309": [-49.147, -2.0655],
    "1503408": [-47.9419, -1.4672],
    "1503457": [-48.1024, -2.9616],
    "1503507": [-47.4272, -1.7552],
    "1503606": [-56.2173, -5.8292],
    "1503705": [-49.9332, -5.212],
    "1503754": [-57.1309, -7.2918],
    "1503804": [-49.2128, -4.5656],
    "1503903": [-56.0822, -2.3771],
    "1504000": [-49.4894, -1.8939],
    "1504059": [-47.5162, -1.9814],
    "1504109": [-47.6294, -0.8245],
    "1504208": [-50.0827, -5.6325],
    "1504307": [-47.4925, -0.7967],
    "1504406": [-47.7063, -0.7743],
    "1504422": [-48.3226, -1.4025],
    "1504455": [-53.2144, -3.1639],
    "1504604": [-49.4578, -2.5712],
    "1504802": [-54.4648, -1.0008],
    "1504901": [-49.3173, -1.274],
    "1504950": [-46.9457, -2.4286],
    "1504976": [-49.2055, -4.9806],
    "1505007": [-47.4164, -1.1281],
    "1505031": [-55.5013, -7.7593],
    "1505064": [-50.5002, -4.6635],
    "1505106": [-55.797, 0.1041],
    "1505205": [-49.8801, -2.2898],
    "1505304": [-56.8841, 0.5447],
    "1505403": [-47.1314, -1.5366],
    "1505437": [-51.4435, -7.5107],
    "1505486": [-50.672, -3.6463],
    "1505494": [-48.3689, -5.9495],
    "1505536": [-50.5457, -6.1815],
    "1505551": [-50.1366, -7.7295],
    "1505635": [-48.9654, -6.4905],
    "1505650": [-54.5153, -3.9759],
    "1505809": [-50.8136, -2.6823],
    "1505908": [-52.5487, -2.15],
    "1506005": [-53.5563, -2.0546],
    "1506104": [-47.1113, -0.9368],
    "1506112": [-47.0091, -0.8234],
    "1506138": [-50.2025, -8.0665],
    "1506161": [-49.8334, -7.3983],
    "1506187": [-48.5929, -4.493],
    "1506195": [-55.2153, -4.1917],
    "1506302": [-48.6359, -0.7752],
    "1506351": [-48.2566, -1.1936],
    "1506500": [-48.1263, -1.3728],
    "1506559": [-46.9463, -1.6637],
    "1506583": [-50.3062, -8.5935],
    "1506609": [-47.5183, -1.3542],
    "1506708": [-50.6953, -9.1697],
    "1506807": [-54.8441, -2.7014],
    "1506906": [-47.3558, -0.9005],
    "1507003": [-48.1833, -1.0953],
    "1507151": [-48.6907, -5.6961],
    "1507201": [-47.7907, -1.8894],
    "1507300": [-52.1975, -7.1954],
    "1507458": [-48.7219, -6.1825],
    "1507508": [-48.7159, -5.4426],
    "1507607": [-47.5421, -1.5792],
    "1507706": [-49.6611, -1.4349],
    "1507755": [-49.5122, -6.8342],
    "1507805": [-51.8059, -4.0089],
    "1507904": [-48.6393, -0.4958],
    "1507953": [-48.7505, -2.8767],
    "1507961": [-47.8483, -0.9946],
    "1508001": [-48.2378, -2.5812],
    "1508035": [-46.9368, -0.9897],
    "1508050": [-55.9589, -5.1334],
    "1508084": [-51.4566, -6.8477],
    "1508126": [-47.439, -3.8272],
    "1508159": [-53.7497, -3.655],
    "1508209": [-48.1391, -0.912],
    "1508357": [-51.9213, -3.1981],
    "1508407": [-49.4979, -6.8701],
    "1600055": [-52.271, 1.6356],
    "1600105": [-50.8424, 1.8604],
    "1600154": [-52.4188, 1.1621],
    "1600204": [-51.4589, 2.3799],
    "1600212": [-50.5211, 1.0011],
    "1600238": [-51.4028, 0.9226],
    "1600253": [-50.702, 0.5607],
    "1600279": [-53.3107, 1.0159],
    "1600303": [-50.7725, 0.5766],
    "1600402": [-52.1016, 0.0344],
    "1600501": [-52.145, 2.7222],
    "1600535": [-51.694, 0.5759],
    "1600550": [-51.2095, 1.6703],
    "1600600": [-51.3771, 0.1551],
    "1600709": [-51.0982, 1.2445],
    "1600808": [-52.0387, -0.9591],
    "1700251": [-49.3168, -9.4473],
    "1700301": [-47.5211, -6.4786],
    "1700350": [-48.9443, -11.3253],
    "1700400": [-47.2362, -11.4564],
    "1700707": [-49.103, -12.4032],
    "1701002": [-48.1672, -6.1773],
    "1701051": [-47.932, -6.4468],
    "1701101": [-47.976, -10.0419],
    "1701309": [-48.5857, -6.9622],
    "1701903": [-49.4194, -8.9017],
    "1702000": [-49.7544, -12.7256],
    "1702109": [-48.5387, -7.3217],
    "1702158": [-48.5273, -6.769],
    "1702208": [-48.1047, -5.6476],
    "1702307": [-49.0236, -7.7263],
    "1702406": [-47.0783, -12.8561],
    "1702554": [-47.9254, -5.5094],
    "1702703": [-46.4588, -12.6196],
    "1702901": [-47.7737, -5.6703],
    "1703008": [-47.8227, -7.2082],
    "1703057": [-48.6761, -8.0186],
    "1703073": [-47.588, -7.7516],
    "1703107": [-48.8309, -9.8608],
    "1703206": [-48.9743, -7.9671],
    "1703305": [-47.8963, -8.996],
    "1703602": [-48.4377, -8.2772],
    "1703701": [-48.6383, -11.0259],
    "1703800": [-48.1462, -5.3586],
    "1703826": [-47.873, -6.0943],
    "1703842": [-46.8387, -8.211],
    "1703867": [-49.221, -11.9329],
    "1703883": [-48.3698, -7.0414],
    "1703891": [-48.018, -5.3525],
    "1703909": [-49.834, -9.4534],
    "1704105": [-47.4233, -9.0968],
    "1704600": [-49.1808, -10.1718],
    "1705102": [-47.8929, -11.5403],
    "1705557": [-46.5187, -12.828],
    "1705607": [-47.3049, -12.1645],
    "1706001": [-49.1781, -8.4878],
    "1706100": [-49.3519, -10.6318],
    "1706258": [-49.0763, -11.1597],
    "1706506": [-47.8181, -6.7252],
    "1707009": [-46.7491, -11.7157],
    "1707108": [-49.3836, -9.6572],
    "1707207": [-49.1132, -9.2908],
    "1707306": [-49.4792, -11.3603],
    "1707405": [-48.5522, -5.3256],
    "1707553": [-48.8666, -10.802],
    "1707652": [-49.2886, -12.2683],
    "1707702": [-47.8491, -7.4831],
    "1708205": [-50.0696, -12.1496],
    "1708254": [-48.5614, -9.0957],
    "1708304": [-48.9841, -8.856],
    "1709005": [-47.4923, -8.0239],
    "1709302": [-48.4241, -8.7213],
    "1709500": [-48.8547, -11.6598],
    "1709807": [-48.3797, -11.1538],
    "1710508": [-47.6631, -8.5285],
    "1710706": [-47.5835, -5.821],
    "1710904": [-47.9962, -8.3478],
    "1711100": [-48.7486, -8.4961],
    "1711506": [-48.6388, -12.8152],
    "1711803": [-49.0984, -8.1176],
    "1711902": [-49.9605, -11.018],
    "1711951": [-47.4854, -10.3251],
    "1712009": [-48.2898, -9.8466],
    "1712157": [-46.4022, -12.8217],
    "1712405": [-46.9812, -9.5409],
    "1712454": [-47.8913, -6.2161],
    "1712504": [-49.737, -9.8196],
    "1712702": [-46.6721, -10.5569],
    "1712801": [-47.5727, -6.0006],
    "1713205": [-48.5778, -9.7617],
    "1713304": [-48.6629, -9.3864],
    "1713601": [-47.9987, -10.7125],
    "1713700": [-49.0858, -10.0109],
    "1713809": [-47.6619, -6.6146],
    "1713957": [-48.7658, -7.0063],
    "1714203": [-47.6499, -11.7798],
    "1714302": [-47.8011, -6.3128],
    "1714880": [-48.3628, -7.6459],
    "1715002": [-48.9744, -10.5608],
    "1715101": [-47.3907, -10.1778],
    "1715150": [-46.5663, -12.8782],
    "1715259": [-46.5438, -11.7695],
    "1715705": [-48.1779, -7.8639],
    "1715754": [-48.3798, -13.0533],
    "1716109": [-48.8611, -10.2271],
    "1716208": [-47.8175, -12.7037],
    "1716307": [-48.895, -7.5605],
    "1716505": [-48.0007, -9.2123],
    "1716604": [-48.5781, -12.0001],
    "1716653": [-48.9272, -8.3827],
    "1716703": [-48.7597, -8.8813],
    "1717008": [-47.5667, -11.1185],
    "1717206": [-48.2543, -6.688],
    "1717503": [-49.7646, -10.0464],
    "1717800": [-46.6486, -12.127],
    "1717909": [-47.252, -10.7669],
    "1718006": [-47.0366, -11.5163],
    "1718204": [-48.4763, -10.5174],
    "1718303": [-47.7929, -5.4882],
    "1718402": [-48.456, -8.4547],
    "1718451": [-48.8563, -10.4195],
    "1718501": [-47.0989, -8.7049],
    "1718550": [-48.1708, -6.4634],
    "1718659": [-46.7574, -11.3472],
    "1718709": [-48.4598, -9.2153],
    "1718758": [-47.4072, -9.6407],
    "1718808": [-47.9198, -5.3595],
    "1718840": [-49.8612, -12.3904],
    "1718865": [-48.9559, -7.0857],
    "1718881": [-47.8613, -8.8235],
    "1718899": [-49.3607, -10.985],
    "1718907": [-48.0994, -11.3911],
    "1719004": [-47.722, -10.3036],
    "1720002": [-47.7035, -6.4695],
    "1720101": [-47.9894, -5.9723],
    "1720150": [-46.7405, -10.0717],
    "1720200": [-47.6097, -5.5391],
    "1720259": [-48.3846, -12.5694],
    "1720309": [-48.3366, -5.2533],
    "1720499": [-48.1425, -11.845],
    "1720655": [-47.9814, -11.1279],
    "1720804": [-47.7227, -5.607],
    "1720853": [-48.8366, -12.097],
    "1720903": [-46.5896, -12.3781],
    "1720937": [-47.0089, -12.1615],
    "1720978": [-49.0408, -12.6683],
    "1721000": [-48.0799, -10.1645],
    "1721109": [-48.1592, -9.5525],
    "1721208": [-47.5333, -6.2444],
    "1721257": [-48.2564, -8.9006],
    "1721307": [-48.2293, -8.3644],
    "1722081": [-48.0091, -6.8371],
    "1722107": [-48.4375, -6.5905],
    "2100055": [-47.3005, -4.7336],
    "2100105": [-43.2539, -4.2029],
    "2100204": [-44.5121, -2.3338],
    "2100303": [-43.4426, -4.5593],
    "2100436": [-44.3775, -4.2321],
    "2100477": [-45.9885, -3.8433],
    "2100501": [-46.1489, -9.4673],
    "2100550": [-45.8829, -1.6741],
    "2100600": [-46.681, -5.4654],
    "2100709": [-44.5366, -3.2317],
    "2100832": [-45.0951, -1.4825],
    "2100873": [-45.783, -2.9994],
    "2100956": [-45.8751, -5.0632],
    "2101202": [-44.7277, -4.0464],
    "2101301": [-45.1317, -1.6558],
    "2101400": [-46.5049, -8.4678],
    "2101608": [-45.2319, -5.5162],
    "2101707": [-42.9237, -2.7559],
    "2101772": [-45.2601, -3.7747],
    "2102002": [-46.2302, -3.7962],
    "2102036": [-46.6693, -4.5164],
    "2102077": [-45.0532, -4.2857],
    "2102101": [-42.8412, -3.6848],
    "2102150": [-45.5429, -4.3445],
    "2102325": [-46.3501, -4.4037],
    "2102358": [-47.045, -5.4983],
    "2102374": [-43.9917, -3.0725],
    "2102507": [-45.0204, -3.3849],
    "2102606": [-45.6012, -1.4538],
    "2102705": [-44.2462, -3.6603],
    "2102804": [-47.1597, -7.4881],
    "2102903": [-45.9699, -1.2121],
    "2103000": [-43.4116, -4.8493],
    "2103109": [-44.5864, -1.9622],
    "2103125": [-44.8546, -2.2521],
    "2103158": [-46.0834, -2.3163],
    "2103174": [-46.6742, -3.1019],
    "2103208": [-43.4682, -3.7694],
    "2103257": [-47.8512, -5.0565],
    "2103307": [-43.9407, -4.7033],
    "2103406": [-43.1263, -4.198],
    "2103554": [-44.8033, -3.7417],
    "2103703": [-44.8212, -1.5815],
    "2103752": [-47.3207, -5.5702],
    "2103802": [-44.4148, -5.0421],
    "2103901": [-43.0121, -4.107],
    "2104008": [-44.8341, -4.9147],
    "2104057": [-47.1857, -6.7785],
    "2104073": [-46.6705, -6.9666],
    "2104081": [-45.2876, -6.3207],
    "2104107": [-46.0262, -6.8892],
    "2104206": [-43.998, -5.6473],
    "2104503": [-44.1783, -4.9469],
    "2104552": [-47.3233, -5.7401],
    "2104677": [-45.8636, -2.0318],
    "2104800": [-45.986, -5.7618],
    "2104909": [-44.6221, -2.125],
    "2105104": [-43.8303, -2.5843],
    "2105302": [-47.5617, -5.35],
    "2105351": [-45.6819, -5.1562],
    "2105401": [-44.317, -3.3453],
    "2105427": [-47.2217, -4.0983],
    "2105450": [-44.2905, -5.8702],
    "2105500": [-47.17, -5.2069],
    "2105708": [-45.2022, -4.7438],
    "2105807": [-44.9602, -4.5495],
    "2105948": [-44.9234, -4.5999],
    "2105989": [-46.9533, -6.1112],
    "2106003": [-44.4686, -4.5808],
    "2106102": [-45.2266, -7.1348],
    "2106201": [-45.8301, -1.2673],
    "2106326": [-45.972, -1.9919],
    "2106359": [-45.5257, -4.7384],
    "2106375": [-45.9646, -2.4287],
    "2106409": [-43.2428, -3.5937],
    "2106607": [-43.3638, -5.3691],
    "2106706": [-45.0799, -6.4655],
    "2106755": [-44.5061, -3.4997],
    "2106904": [-45.2957, -3.5413],
    "2107001": [-47.0786, -5.8578],
    "2107209": [-43.7731, -3.4506],
    "2107258": [-46.2755, -7.2166],
    "2107308": [-44.0821, -6.8196],
    "2107506": [-44.1213, -2.4988],
    "2107605": [-45.0519, -2.667],
    "2107704": [-43.8981, -6.3938],
    "2107803": [-43.5961, -5.5906],
    "2107902": [-43.7749, -6.1165],
    "2108009": [-44.1662, -6.6682],
    "2108058": [-42.5912, -2.8653],
    "2108256": [-45.4102, -2.8933],
    "2108306": [-45.2063, -3.2496],
    "2108454": [-44.2957, -4.3706],
    "2108504": [-45.3909, -3.6309],
    "2108603": [-45.1024, -2.5444],
    "2108801": [-44.2252, -3.7608],
    "2108900": [-44.7632, -4.7878],
    "2109007": [-47.095, -6.3479],
    "2109056": [-44.6242, -1.8596],
    "2109205": [-44.0717, -3.0838],
    "2109270": [-45.4111, -2.6021],
    "2109403": [-43.369, -2.6532],
    "2109452": [-44.0698, -2.4278],
    "2109502": [-46.7143, -7.7721],
    "2109551": [-47.2966, -5.9409],
    "2109809": [-45.4135, -2.4595],
    "2109908": [-45.408, -3.8542],
    "2110005": [-45.802, -4.292],
    "2110039": [-45.8425, -2.591],
    "2110104": [-42.8694, -3.3429],
    "2110237": [-42.6502, -3.1778],
    "2110278": [-43.1814, -2.6321],
    "2110500": [-45.0132, -2.801],
    "2110609": [-42.3975, -3.3755],
    "2110658": [-44.6231, -6.8273],
    "2110708": [-44.3708, -5.6339],
    "2110807": [-44.8476, -6.993],
    "2110906": [-43.0374, -6.2194],
    "2111029": [-46.3577, -3.5864],
    "2111052": [-46.9026, -6.4107],
    "2111201": [-44.1406, -2.5679],
    "2111250": [-44.6009, -5.0172],
    "2111409": [-44.6604, -4.3977],
    "2111508": [-44.5277, -3.983],
    "2111532": [-48.3872, -5.1682],
    "2111573": [-46.6755, -6.8448],
    "2111672": [-45.0255, -5.0267],
    "2111748": [-43.9509, -5.2797],
    "2111763": [-47.1065, -5.337],
    "2111789": [-45.0006, -1.8123],
    "2111805": [-46.664, -6.2191],
    "2111904": [-44.2982, -6.4851],
    "2112100": [-43.8128, -4.1758],
    "2112209": [-42.9225, -5.1749],
    "2112274": [-45.575, -3.7088],
    "2112308": [-44.7147, -5.465],
    "2112456": [-45.3497, -2.13],
    "2112506": [-42.3979, -2.8516],
    "2112704": [-43.8606, -3.6411],
    "2112803": [-45.0013, -3.1757],
    "2112852": [-48.074, -5.0792],
    "2113009": [-45.3411, -4.1469],
    "2114007": [-45.9469, -3.1714],
    "2200053": [-40.9089, -8.297],
    "2200103": [-42.6842, -5.7352],
    "2200202": [-42.625, -5.9081],
    "2200251": [-40.9189, -6.9472],
    "2200277": [-40.8189, -7.181],
    "2200301": [-42.1131, -5.369],
    "2200400": [-42.4681, -5.0905],
    "2200459": [-43.8924, -8.4092],
    "2200509": [-42.8297, -6.3776],
    "2200608": [-42.7373, -6.111],
    "2200707": [-43.04, -9.2199],
    "2200806": [-44.2415, -7.1575],
    "2200905": [-41.8604, -6.1741],
    "2200954": [-41.5238, -7.2359],
    "2201002": [-42.5137, -6.6662],
    "2201051": [-40.9977, -5.8681],
    "2201101": [-43.963, -10.1493],
    "2201150": [-45.1482, -8.6631],
    "2201176": [-42.1063, -6.5172],
    "2201200": [-42.2864, -4.1631],
    "2201408": [-42.4745, -5.8197],
    "2201507": [-42.0789, -4.0017],
    "2201556": [-41.884, -7.9384],
    "2201572": [-40.9859, -7.3859],
    "2201606": [-42.4109, -5.4626],
    "2201705": [-43.7993, -7.7554],
    "2201770": [-42.1294, -4.3592],
    "2201804": [-41.3151, -6.9068],
    "2201919": [-41.5977, -3.1699],
    "2201929": [-42.8852, -9.1939],
    "2201945": [-42.1251, -4.5561],
    "2201960": [-41.6554, -4.1275],
    "2201988": [-42.752, -8.3128],
    "2202000": [-41.8301, -3.2726],
    "2202026": [-41.2186, -5.0983],
    "2202059": [-42.3322, -4.4178],
    "2202075": [-42.4669, -6.775],
    "2202083": [-41.3669, -2.9843],
    "2202091": [-40.6187, -7.2948],
    "2202109": [-41.8591, -7.6621],
    "2202117": [-41.7836, -8.2586],
    "2202174": [-42.5767, -3.8495],
    "2202251": [-43.6382, -7.4821],
    "2202307": [-43.1602, -8.2618],
    "2202406": [-41.881, -4.4992],
    "2202455": [-41.9025, -8.5937],
    "2202505": [-43.3514, -9.1772],
    "2202539": [-41.8387, -3.5445],
    "2202554": [-40.9228, -7.6965],
    "2202604": [-41.5972, -5.2876],
    "2202653": [-41.8847, -3.3989],
    "2202703": [-41.5019, -3.4287],
    "2202711": [-41.9734, -4.6295],
    "2202737": [-42.2574, -5.1387],
    "2202752": [-43.7606, -8.1486],
    "2202778": [-42.213, -7.2542],
    "2202802": [-41.5695, -7.9588],
    "2202851": [-42.3157, -9.0389],
    "2202901": [-45.1117, -10.4396],
    "2203008": [-45.1111, -10.7838],
    "2203107": [-44.0433, -8.7171],
    "2203206": [-44.3323, -9.9113],
    "2203230": [-44.8023, -8.7449],
    "2203255": [-42.8437, -5.6002],
    "2203271": [-40.7053, -7.8991],
    "2203305": [-42.6697, -5.3333],
    "2203354": [-42.4429, -9.3356],
    "2203404": [-41.7061, -6.9559],
    "2203420": [-41.3212, -4.2213],
    "2203453": [-41.7893, -8.8454],
    "2203503": [-42.1656, -6.2123],
    "2203602": [-43.7577, -7.9138],
    "2203701": [-42.1696, -3.8401],
    "2203750": [-42.7809, -9.4896],
    "2203800": [-42.8627, -7.679],
    "2203859": [-41.8162, -7.4718],
    "2204006": [-42.2118, -6.4395],
    "2204105": [-42.6962, -6.671],
    "2204154": [-40.7905, -7.3438],
    "2204204": [-41.1518, -7.0628],
    "2204303": [-40.5681, -7.0242],
    "2204352": [-41.3413, -7.1843],
    "2204402": [-45.4762, -9.72],
    "2204501": [-43.7554, -6.8119],
    "2204550": [-43.6156, -9.088],
    "2204600": [-42.4848, -6.0276],
    "2204659": [-41.8163, -2.8434],
    "2204709": [-41.7099, -6.6743],
    "2204808": [-41.7861, -6.8178],
    "2204907": [-41.6445, -7.6397],
    "2205003": [-41.4764, -7.427],
    "2205102": [-43.0769, -7.5066],
    "2205151": [-41.2324, -7.8894],
    "2205201": [-41.2231, -7.4269],
    "2205250": [-42.5227, -6.1471],
    "2205276": [-41.8978, -4.8215],
    "2205300": [-43.525, -7.1032],
    "2205359": [-42.4456, -8.5612],
    "2205409": [-42.0742, -3.5308],
    "2205458": [-42.4282, -3.5366],
    "2205508": [-42.5547, -4.7135],
    "2205516": [-41.548, -5.006],
    "2205524": [-44.1541, -10.4415],
    "2205532": [-43.1466, -9.1974],
    "2205540": [-42.6259, -5.8153],
    "2205557": [-42.5486, -4.4671],
    "2205565": [-41.5732, -8.6212],
    "2205573": [-41.593, -4.3668],
    "2205599": [-41.4402, -6.4909],
    "2205607": [-43.8948, -7.2512],
    "2205706": [-41.4942, -3.0586],
    "2205805": [-42.3716, -3.5908],
    "2205854": [-42.5176, -3.5485],
    "2205904": [-43.8568, -8.164],
    "2205953": [-40.7347, -7.436],
    "2206001": [-43.8811, -7.0221],
    "2206050": [-41.063, -7.5334],
    "2206100": [-42.5761, -3.7188],
    "2206209": [-42.7892, -4.2128],
    "2206308": [-42.6902, -5.7163],
    "2206357": [-41.5266, -4.7297],
    "2206407": [-42.5866, -5.6165],
    "2206506": [-41.0298, -6.958],
    "2206605": [-45.0018, -9.7249],
    "2206654": [-43.8966, -9.8693],
    "2206670": [-42.2495, -3.7063],
    "2206696": [-42.0047, -3.3563],
    "2206704": [-42.6915, -7.0422],
    "2206753": [-42.1853, -4.6376],
    "2206803": [-42.6124, -4.0497],
    "2206902": [-42.0005, -6.5291],
    "2206951": [-41.9599, -5.3082],
    "2207009": [-42.1883, -6.9185],
    "2207108": [-42.5551, -5.8116],
    "2207207": [-40.8821, -7.4077],
    "2207306": [-42.2882, -7.8106],
    "2207355": [-42.8345, -7.9499],
    "2207405": [-44.3478, -8.5701],
    "2207504": [-42.9943, -5.8166],
    "2207553": [-41.6649, -7.1204],
    "2207603": [-44.5711, -10.1951],
    "2207751": [-42.4078, -5.8276],
    "2207777": [-41.2977, -7.6378],
    "2207793": [-42.4635, -5.2559],
    "2207850": [-43.3037, -7.8837],
    "2207900": [-41.41, -4.448],
    "2207934": [-42.2645, -8.0999],
    "2207959": [-41.9931, -8.1099],
    "2208007": [-41.5247, -7.0595],
    "2208106": [-41.2547, -6.3366],
    "2208205": [-40.649, -6.8569],
    "2208304": [-41.6573, -3.8374],
    "2208502": [-42.7047, -3.9612],
    "2208551": [-44.0696, -6.9196],
    "2208650": [-41.3143, -8.479],
    "2208700": [-44.538, -9.5829],
    "2208809": [-42.5053, -6.309],
    "2208858": [-44.7117, -9.8284],
    "2208874": [-42.5946, -8.0083],
    "2208908": [-45.4652, -8.0804],
    "2209005": [-43.1318, -7.799],
    "2209104": [-41.7718, -7.2454],
    "2209153": [-41.9433, -5.862],
    "2209203": [-45.6843, -8.9159],
    "2209302": [-44.2536, -8.9741],
    "2209351": [-41.4875, -6.94],
    "2209377": [-42.2202, -6.8193],
    "2209401": [-41.2137, -6.8819],
    "2209450": [-42.6985, -6.0492],
    "2209500": [-41.9438, -7.4556],
    "2209559": [-42.989, -8.9729],
    "2209609": [-42.1137, -5.8871],
    "2209708": [-42.5028, -7.1474],
    "2209757": [-45.4024, -10.0589],
    "2209807": [-42.6648, -6.0167],
    "2209856": [-41.3823, -6.7401],
    "2209872": [-41.2291, -4.0309],
    "2209906": [-41.8458, -5.4711],
    "2209955": [-41.9009, -6.9394],
    "2209971": [-42.4565, -3.7973],
    "2210052": [-41.8824, -3.7626],
    "2210102": [-42.547, -7.4701],
    "2210201": [-41.4932, -6.8271],
    "2210300": [-40.8371, -7.0752],
    "2210359": [-42.4654, -9.1503],
    "2210375": [-41.277, -6.7777],
    "2210409": [-41.5787, -5.7224],
    "2210508": [-42.7698, -5.8317],
    "2210623": [-44.8332, -10.6339],
    "2210631": [-44.0392, -7.5446],
    "2210656": [-41.8158, -4.9048],
    "2210706": [-40.7528, -7.6991],
    "2210805": [-41.9659, -7.8229],
    "2210904": [-42.5101, -7.8499],
    "2210938": [-41.393, -6.9858],
    "2210953": [-43.0812, -8.4418],
    "2210979": [-42.2679, -6.6775],
    "2211001": [-42.8069, -5.2476],
    "2211100": [-42.8308, -4.5689],
    "2211209": [-44.5891, -7.4338],
    "2211308": [-41.7978, -6.3069],
    "2211357": [-42.9285, -9.2974],
    "2211407": [-42.2019, -6.5625],
    "2211506": [-41.4991, -7.5832],
    "2211605": [-40.9265, -7.1949],
    "2211704": [-41.8431, -7.2915],
    "2300150": [-38.6688, -4.2179],
    "2300200": [-40.0863, -2.9763],
    "2300309": [-39.5258, -6.1105],
    "2300408": [-40.2588, -6.5981],
    "2300606": [-39.7004, -6.9857],
    "2300705": [-38.1989, -5.5108],
    "2300754": [-39.7886, -3.2407],
    "2300903": [-39.3067, -3.9577],
    "2301000": [-38.3921, -3.9548],
    "2301208": [-38.6808, -4.494],
    "2301406": [-39.0419, -4.4122],
    "2301505": [-40.0978, -6.2271],
    "2301604": [-39.8259, -6.9222],
    "2301851": [-38.9175, -5.2845],
    "2301950": [-38.6089, -4.3466],
    "2302057": [-41.1607, -2.9942],
    "2302107": [-38.8541, -4.3563],
    "2302206": [-38.0837, -4.4227],
    "2302305": [-40.2654, -3.0485],
    "2302404": [-39.8354, -5.0782],
    "2302602": [-40.8431, -2.9471],
    "2302701": [-40.2543, -6.9394],
    "2302800": [-39.4425, -4.3988],
    "2302909": [-38.9148, -4.4708],
    "2303204": [-39.2765, -7.0276],
    "2303303": [-39.4801, -6.672],
    "2303402": [-41.0045, -4.1317],
    "2303501": [-38.2935, -4.2567],
    "2303600": [-39.9257, -6.2588],
    "2303659": [-40.1804, -4.6043],
    "2303709": [-38.7984, -3.79],
    "2303808": [-39.1265, -6.5788],
    "2303907": [-41.2407, -3.0769],
    "2303956": [-38.4838, -4.2988],
    "2304202": [-39.4697, -7.2043],
    "2304251": [-40.3347, -2.8979],
    "2304269": [-39.2769, -5.8898],
    "2304277": [-38.3227, -5.9898],
    "2304285": [-38.4599, -3.8785],
    "2304350": [-40.234, -3.8149],
    "2304400": [-38.5188, -3.7795],
    "2304459": [-37.8633, -4.4496],
    "2304509": [-40.83, -3.7046],
    "2304608": [-39.4429, -4.0484],
    "2304657": [-40.7951, -4.0442],
    "2304707": [-40.9837, -3.2901],
    "2304806": [-39.2726, -6.9192],
    "2304905": [-40.3721, -3.9085],
    "2304954": [-38.6861, -4.1069],
    "2305001": [-40.8536, -4.2206],
    "2305100": [-38.9507, -4.2308],
    "2305233": [-38.4871, -4.0977],
    "2305308": [-41.0094, -3.9489],
    "2305332": [-38.5512, -4.9694],
    "2305357": [-37.414, -4.7275],
    "2305506": [-39.2805, -6.3524],
    "2305605": [-40.3184, -5.5028],
    "2305704": [-38.7396, -6.8046],
    "2306009": [-38.3557, -5.7907],
    "2306207": [-37.8069, -4.7153],
    "2306256": [-38.5442, -4.0084],
    "2306306": [-39.569, -3.7284],
    "2306405": [-39.5624, -3.3739],
    "2306504": [-38.9497, -4.5937],
    "2306553": [-39.8984, -3.0679],
    "2306603": [-39.5733, -4.5938],
    "2306702": [-38.752, -5.4548],
    "2306801": [-38.4963, -5.6342],
    "2306900": [-38.6833, -5.9897],
    "2307106": [-39.2312, -7.6334],
    "2307403": [-39.6116, -6.4453],
    "2307502": [-38.9818, -6.7805],
    "2307635": [-39.4944, -4.8638],
    "2307700": [-38.7797, -4.0096],
    "2307809": [-40.3061, -3.1837],
    "2308005": [-40.3924, -3.4992],
    "2308351": [-39.174, -5.6177],
    "2308377": [-39.93, -3.5703],
    "2308401": [-39.1291, -7.262],
    "2308500": [-39.7993, -5.8199],
    "2308609": [-40.0498, -4.9479],
    "2308708": [-38.4535, -4.9189],
    "2308807": [-40.6695, -3.4425],
    "2308906": [-40.147, -3.3001],
    "2309102": [-39.0074, -4.2946],
    "2309201": [-39.6657, -7.0731],
    "2309300": [-40.5468, -4.7014],
    "2309409": [-40.7419, -5.5872],
    "2309458": [-38.5085, -4.5282],
    "2309508": [-38.9542, -6.243],
    "2309607": [-38.503, -4.1902],
    "2309706": [-38.6128, -3.9581],
    "2309805": [-38.9127, -4.1931],
    "2310100": [-38.8581, -4.1252],
    "2310209": [-39.05, -3.483],
    "2310308": [-40.5856, -6.264],
    "2310407": [-39.3783, -4.1491],
    "2310704": [-39.2001, -3.8534],
    "2310803": [-38.495, -5.9989],
    "2310852": [-38.3037, -4.0585],
    "2310902": [-39.4407, -5.8722],
    "2310951": [-40.589, -4.2577],
    "2311009": [-41.0565, -4.7767],
    "2311207": [-40.0398, -7.048],
    "2311264": [-40.7307, -5.8856],
    "2311355": [-39.123, -6.1491],
    "2311405": [-39.3316, -5.1846],
    "2311504": [-37.8944, -5.0659],
    "2311702": [-40.6177, -4.1257],
    "2311801": [-38.1602, -4.8451],
    "2311900": [-39.8848, -6.4728],
    "2312007": [-40.1549, -3.4756],
    "2312205": [-39.9828, -4.3817],
    "2312403": [-39.0602, -3.5713],
    "2312601": [-39.2484, -3.649],
    "2312700": [-39.4838, -5.5633],
    "2312809": [-40.4685, -3.2832],
    "2312908": [-40.1914, -3.8272],
    "2313005": [-39.0022, -5.8559],
    "2313104": [-38.0933, -5.3039],
    "2313252": [-39.7194, -6.7261],
    "2313302": [-40.2586, -5.8968],
    "2313351": [-39.6053, -3.9323],
    "2313401": [-41.0367, -3.7095],
    "2313500": [-39.3782, -3.3401],
    "2313559": [-39.4086, -3.5451],
    "2313708": [-38.7226, -6.6171],
    "2313757": [-39.3955, -3.6871],
    "2313807": [-39.5193, -3.6231],
    "2313906": [-40.6885, -3.3437],
    "2313955": [-40.5061, -4.1661],
    "2314003": [-39.3233, -6.7478],
    "2400109": [-36.6526, -6.3976],
    "2400307": [-36.6588, -5.4664],
    "2400406": [-38.2953, -6.2052],
    "2400505": [-37.9934, -6.386],
    "2400604": [-37.7751, -6.1499],
    "2400703": [-36.7524, -5.3548],
    "2400802": [-36.5722, -5.6709],
    "2400901": [-37.9197, -6.1891],
    "2401008": [-37.8659, -5.6444],
    "2401107": [-37.0418, -4.9927],
    "2401206": [-35.1926, -6.1902],
    "2401305": [-37.3172, -5.9047],
    "2401404": [-35.055, -6.4242],
    "2401453": [-37.6124, -5.0498],
    "2401503": [-35.9233, -5.948],
    "2401651": [-36.4116, -5.9426],
    "2401800": [-35.3754, -6.2201],
    "2401859": [-36.0593, -5.1642],
    "2401909": [-35.9992, -5.7918],
    "2402006": [-37.0519, -6.4817],
    "2402105": [-36.2282, -6.2601],
    "2402204": [-35.1314, -6.3787],
    "2402303": [-37.5765, -5.7847],
    "2402402": [-36.5389, -6.5601],
    "2402501": [-36.8048, -5.2628],
    "2402600": [-35.3814, -5.5874],
    "2402709": [-36.346, -5.9842],
    "2402808": [-36.1992, -6.342],
    "2402907": [-38.4166, -6.255],
    "2403004": [-36.8324, -6.3429],
    "2403103": [-36.4948, -6.1844],
    "2403202": [-38.3926, -6.1074],
    "2403301": [-38.3088, -6.1282],
    "2403400": [-36.6558, -6.8684],
    "2403509": [-35.3068, -6.3238],
    "2403608": [-35.2636, -5.683],
    "2403707": [-37.6472, -5.5324],
    "2403756": [-36.4321, -5.7277],
    "2403806": [-36.8245, -6.1688],
    "2403905": [-38.101, -6.0392],
    "2404101": [-36.2422, -5.1387],
    "2404200": [-35.1953, -6.2749],
    "2404309": [-37.5277, -5.3994],
    "2404408": [-37.201, -4.9486],
    "2404606": [-35.5351, -5.7564],
    "2404705": [-36.8157, -5.5512],
    "2404804": [-37.1854, -6.7694],
    "2404853": [-36.7959, -5.7021],
    "2404903": [-37.9349, -5.8236],
    "2405009": [-36.2049, -6.4106],
    "2405108": [-36.1103, -5.3628],
    "2405207": [-37.481, -5.9593],
    "2405306": [-35.5961, -6.1453],
    "2405405": [-35.8734, -6.421],
    "2405603": [-37.2628, -6.2839],
    "2405702": [-36.8225, -6.5769],
    "2405801": [-35.8468, -5.5214],
    "2406106": [-37.0, -6.0527],
    "2406155": [-35.3409, -6.2523],
    "2406205": [-35.6295, -6.3749],
    "2406304": [-35.4681, -6.1802],
    "2406403": [-35.8434, -6.0148],
    "2406502": [-36.5111, -6.112],
    "2406700": [-36.1735, -5.718],
    "2406809": [-36.1391, -6.1264],
    "2406908": [-37.815, -6.1111],
    "2407005": [-38.3991, -6.3902],
    "2407104": [-35.42, -5.9096],
    "2407203": [-36.6081, -5.1589],
    "2407252": [-38.314, -6.3985],
    "2407302": [-38.1769, -6.2962],
    "2407401": [-37.9081, -6.1219],
    "2407500": [-35.3493, -5.4677],
    "2407609": [-37.4565, -6.0977],
    "2407708": [-35.2917, -6.4936],
    "2407807": [-35.4169, -6.0913],
    "2408102": [-35.2252, -5.8035],
    "2408201": [-35.1752, -6.0596],
    "2408300": [-35.4342, -6.4509],
    "2408409": [-37.7278, -6.0005],
    "2408508": [-36.9085, -6.6743],
    "2408706": [-37.1268, -5.7417],
    "2408805": [-35.946, -5.3026],
    "2408904": [-36.6241, -6.7214],
    "2408953": [-35.391, -5.3729],
    "2409100": [-35.59, -6.4586],
    "2409209": [-35.398, -6.275],
    "2409332": [-35.7139, -5.8242],
    "2409407": [-38.1846, -6.1151],
    "2409506": [-35.8539, -5.1115],
    "2409605": [-36.0949, -5.5471],
    "2409704": [-36.3147, -5.4755],
    "2409803": [-35.2346, -6.4561],
    "2409902": [-36.6274, -5.2576],
    "2410108": [-35.6922, -5.5744],
    "2410207": [-38.0298, -6.0203],
    "2410405": [-35.5737, -5.415],
    "2410504": [-38.227, -6.1969],
    "2410603": [-37.738, -6.0548],
    "2410702": [-37.9731, -5.9354],
    "2410801": [-38.3166, -6.27],
    "2410900": [-35.8656, -5.8028],
    "2411007": [-38.1037, -5.8817],
    "2411106": [-35.9279, -5.8617],
    "2411205": [-36.0052, -6.2222],
    "2411403": [-36.6655, -5.9153],
    "2411429": [-36.7595, -6.7129],
    "2411502": [-35.5137, -6.3289],
    "2411700": [-36.0474, -6.3926],
    "2411809": [-37.1385, -6.2904],
    "2411908": [-38.1678, -5.9816],
    "2412005": [-35.3439, -5.7755],
    "2412104": [-37.1566, -6.6909],
    "2412203": [-35.2976, -6.0653],
    "2412401": [-36.8374, -6.4862],
    "2412500": [-38.4628, -6.201],
    "2412559": [-35.7168, -5.1841],
    "2412609": [-35.7389, -5.8982],
    "2412708": [-35.6312, -5.8772],
    "2412807": [-36.8814, -5.8424],
    "2412906": [-36.1136, -5.9905],
    "2413003": [-36.6598, -6.2236],
    "2413201": [-35.126, -6.1535],
    "2413300": [-35.7112, -6.4377],
    "2413359": [-37.0247, -5.1115],
    "2413409": [-37.387, -6.5866],
    "2413508": [-35.5781, -6.2543],
    "2413557": [-37.989, -6.1534],
    "2413607": [-37.9702, -5.7839],
    "2413706": [-35.9517, -6.1152],
    "2414100": [-38.1503, -6.4441],
    "2414159": [-36.7162, -6.1495],
    "2414209": [-35.0961, -6.2415],
    "2414308": [-37.2285, -6.4977],
    "2414407": [-35.6175, -5.2609],
    "2414456": [-37.1339, -5.9323],
    "2414506": [-37.8145, -6.0093],
    "2414605": [-37.2706, -5.6731],
    "2414704": [-35.3587, -6.3431],
    "2414753": [-38.5211, -6.329],
    "2414803": [-35.4239, -6.0445],
    "2414902": [-37.9622, -5.9943],
    "2415008": [-35.0749, -6.2975],
    "2500106": [-37.6658, -7.4765],
    "2500205": [-38.2198, -7.0911],
    "2500304": [-35.6073, -7.0563],
    "2500403": [-35.7642, -7.0573],
    "2500502": [-35.5213, -6.9671],
    "2500536": [-36.0436, -7.6749],
    "2500577": [-35.9426, -6.8845],
    "2500601": [-34.9205, -7.3568],
    "2500700": [-38.4194, -6.7547],
    "2500734": [-37.036, -7.5583],
    "2500809": [-35.3593, -6.8568],
    "2500908": [-35.731, -6.856],
    "2501005": [-35.7344, -6.5263],
    "2501104": [-35.7127, -6.9509],
    "2501203": [-35.9409, -7.0426],
    "2501302": [-35.7004, -7.5198],
    "2501351": [-36.7011, -7.0762],
    "2501401": [-34.9797, -6.6537],
    "2501500": [-35.5953, -6.6886],
    "2501534": [-36.2664, -6.6238],
    "2501575": [-35.9722, -7.5691],
    "2501609": [-36.0217, -6.7489],
    "2501708": [-36.2697, -7.6852],
    "2501807": [-34.9195, -7.13],
    "2501906": [-35.5032, -6.7184],
    "2502003": [-37.3822, -6.1556],
    "2502052": [-38.5642, -6.4891],
    "2502102": [-38.1879, -7.4289],
    "2502151": [-36.1893, -7.2825],
    "2502300": [-37.9616, -6.4627],
    "2502409": [-38.479, -7.2902],
    "2502607": [-38.1166, -7.149],
    "2502706": [-35.5853, -6.7913],
    "2502805": [-37.4946, -6.322],
    "2502904": [-37.8572, -6.3939],
    "2503001": [-34.9177, -7.49],
    "2503100": [-36.2809, -7.4634],
    "2503209": [-34.85, -7.0231],
    "2503308": [-38.6979, -6.9423],
    "2503506": [-35.7996, -6.6303],
    "2503555": [-37.0879, -7.2102],
    "2503605": [-35.4052, -6.6055],
    "2503704": [-38.567, -6.9333],
    "2503753": [-37.8121, -6.9441],
    "2503803": [-35.3345, -7.1311],
    "2503902": [-36.7752, -7.8784],
    "2504009": [-35.9311, -7.2584],
    "2504033": [-35.1751, -6.8948],
    "2504074": [-36.5136, -7.7555],
    "2504108": [-38.3174, -7.0463],
    "2504157": [-35.8435, -6.7512],
    "2504207": [-37.5996, -7.1812],
    "2504306": [-37.7092, -6.3118],
    "2504355": [-36.0448, -7.4145],
    "2504405": [-38.5071, -7.4996],
    "2504504": [-37.625, -6.8652],
    "2504603": [-34.8521, -7.2819],
    "2504702": [-36.6458, -7.7909],
    "2504801": [-37.9768, -7.0696],
    "2504850": [-36.6329, -7.669],
    "2504900": [-35.1162, -7.158],
    "2505006": [-36.3291, -6.8511],
    "2505105": [-36.0743, -6.5333],
    "2505204": [-35.5083, -6.9023],
    "2505238": [-35.2505, -6.9068],
    "2505303": [-38.1872, -7.5395],
    "2505352": [-35.9365, -6.6666],
    "2505402": [-37.09, -7.2988],
    "2505501": [-37.5666, -6.7599],
    "2505600": [-38.3209, -7.4383],
    "2505709": [-35.6418, -6.6105],
    "2505808": [-35.3969, -6.7207],
    "2505907": [-37.7569, -7.0948],
    "2506004": [-35.8716, -7.0138],
    "2506103": [-35.7927, -7.4057],
    "2506202": [-36.4216, -6.4027],
    "2506251": [-35.8167, -7.5785],
    "2506301": [-35.4454, -6.8752],
    "2506400": [-35.4423, -7.1481],
    "2506509": [-36.4826, -7.27],
    "2506608": [-38.3954, -7.4943],
    "2506707": [-37.5598, -7.3755],
    "2506806": [-35.6092, -7.2536],
    "2506905": [-35.3566, -7.3589],
    "2507002": [-38.2078, -7.2879],
    "2507101": [-35.2531, -6.8169],
    "2507200": [-35.6395, -7.4252],
    "2507309": [-35.2661, -6.6081],
    "2507408": [-37.8118, -6.5169],
    "2507507": [-34.8714, -7.1662],
    "2507606": [-35.5566, -7.1702],
    "2507705": [-36.5805, -7.0414],
    "2507804": [-36.7368, -6.9836],
    "2507903": [-35.2214, -7.3488],
    "2508000": [-37.7979, -7.5152],
    "2508208": [-35.3616, -6.6703],
    "2508307": [-35.859, -7.1449],
    "2508406": [-38.1708, -6.5481],
    "2508505": [-36.9123, -7.3417],
    "2508554": [-35.4335, -6.5577],
    "2508604": [-34.8993, -6.932],
    "2508802": [-37.5144, -6.8928],
    "2508901": [-35.1486, -6.71],
    "2509008": [-38.1971, -7.7232],
    "2509057": [-34.9972, -6.7592],
    "2509107": [-35.3032, -7.048],
    "2509156": [-38.3392, -6.8303],
    "2509206": [-35.7512, -7.1989],
    "2509305": [-35.0482, -6.5508],
    "2509339": [-35.774, -7.118],
    "2509370": [-37.7283, -6.5467],
    "2509396": [-37.3566, -7.2786],
    "2509404": [-35.4875, -7.2779],
    "2509503": [-35.9417, -7.0929],
    "2509602": [-38.5261, -7.2082],
    "2509800": [-35.4256, -6.995],
    "2509909": [-35.5643, -7.5655],
    "2510006": [-38.3443, -6.9329],
    "2510105": [-36.2082, -6.4692],
    "2510303": [-36.4377, -6.6713],
    "2510501": [-36.2363, -6.9942],
    "2510600": [-37.1326, -7.6164],
    "2510659": [-36.6758, -7.2998],
    "2510709": [-37.0404, -7.1195],
    "2510808": [-37.3499, -7.0056],
    "2510907": [-37.6079, -6.5901],
    "2511004": [-38.0719, -7.4594],
    "2511103": [-36.4075, -6.7942],
    "2511202": [-35.0764, -7.3525],
    "2511301": [-37.9814, -7.1783],
    "2511400": [-36.3399, -6.4783],
    "2511509": [-35.2685, -7.2713],
    "2511707": [-35.5466, -6.8458],
    "2511806": [-35.4817, -6.7817],
    "2511905": [-34.8445, -7.4296],
    "2512002": [-36.0863, -7.0544],
    "2512036": [-38.5294, -6.3869],
    "2512077": [-38.51, -6.5881],
    "2512101": [-37.8641, -6.806],
    "2512200": [-37.1001, -7.6935],
    "2512309": [-37.9965, -7.6458],
    "2512408": [-35.9573, -7.1551],
    "2512507": [-35.9269, -7.4055],
    "2512606": [-37.1222, -7.0449],
    "2512705": [-35.8395, -6.9453],
    "2512721": [-35.291, -6.6707],
    "2512747": [-35.644, -6.5504],
    "2512754": [-35.6578, -7.2353],
    "2512762": [-35.2824, -7.1471],
    "2512804": [-37.6289, -6.4991],
    "2512903": [-35.0411, -6.7791],
    "2513109": [-35.4755, -7.4128],
    "2513158": [-35.9275, -7.7312],
    "2513307": [-38.5876, -6.7187],
    "2513356": [-38.5853, -7.6672],
    "2513406": [-36.9114, -6.9105],
    "2513505": [-38.3282, -7.6274],
    "2513604": [-37.9611, -7.3858],
    "2513653": [-38.4844, -6.4686],
    "2513703": [-34.984, -7.0818],
    "2513851": [-36.6224, -7.2363],
    "2513901": [-37.4804, -6.4536],
    "2513927": [-37.7036, -6.8853],
    "2513943": [-36.3859, -7.5818],
    "2513968": [-37.9434, -6.8058],
    "2513984": [-38.0498, -6.6362],
    "2514008": [-36.4872, -7.4588],
    "2514107": [-36.7859, -8.1166],
    "2514206": [-38.1237, -6.9325],
    "2514305": [-38.3252, -7.255],
    "2514404": [-37.3944, -6.8181],
    "2514453": [-35.3515, -7.2411],
    "2514503": [-38.503, -7.1086],
    "2514552": [-38.0938, -7.7009],
    "2514602": [-37.3192, -7.1416],
    "2514651": [-37.3546, -6.235],
    "2514701": [-36.821, -6.8424],
    "2514800": [-36.8703, -7.4194],
    "2514909": [-37.0707, -6.9348],
    "2515005": [-35.1962, -7.2364],
    "2515104": [-35.8482, -7.0893],
    "2515203": [-37.0099, -8.1457],
    "2515302": [-35.2085, -7.066],
    "2515401": [-36.452, -6.8737],
    "2515500": [-36.737, -7.5646],
    "2515609": [-35.448, -6.7054],
    "2515708": [-38.3769, -7.2215],
    "2515807": [-35.6652, -7.1693],
    "2515906": [-35.6404, -6.8317],
    "2515930": [-35.4208, -6.7422],
    "2515971": [-35.2507, -7.1754],
    "2516003": [-35.7243, -6.7407],
    "2516102": [-36.337, -7.1095],
    "2516151": [-36.2057, -6.7063],
    "2516300": [-36.9092, -7.6349],
    "2516409": [-35.5181, -6.5323],
    "2516508": [-36.8133, -7.2082],
    "2516607": [-37.8807, -7.6051],
    "2516706": [-37.2593, -7.2523],
    "2516755": [-36.6249, -6.9644],
    "2516805": [-38.56, -6.5982],
    "2516904": [-38.3866, -6.4842],
    "2517001": [-35.7331, -7.6772],
    "2517100": [-37.0309, -6.7899],
    "2517209": [-38.2746, -6.5614],
    "2517407": [-37.0948, -8.0794],
    "2600104": [-37.6241, -7.7175],
    "2600203": [-41.0348, -8.6238],
    "2600302": [-35.9336, -8.4498],
    "2600500": [-37.0249, -9.0838],
    "2600609": [-36.7643, -8.5192],
    "2600708": [-35.1822, -7.5978],
    "2600807": [-36.0872, -8.4784],
    "2600906": [-35.4723, -8.3992],
    "2601003": [-36.2796, -8.8569],
    "2601102": [-40.5201, -7.6378],
    "2601201": [-36.9986, -8.3933],
    "2601300": [-35.6261, -8.3929],
    "2601706": [-36.4365, -8.2737],
    "2601805": [-38.0104, -8.2739],
    "2601904": [-35.8092, -8.2583],
    "2602001": [-39.9626, -7.7326],
    "2602100": [-36.6464, -9.1998],
    "2602209": [-35.5637, -7.7712],
    "2602308": [-35.6737, -8.4983],
    "2602407": [-36.5537, -9.0278],
    "2602506": [-37.3255, -7.3438],
    "2602605": [-36.2395, -8.0656],
    "2602704": [-35.3589, -7.7416],
    "2602803": [-37.1459, -8.6656],
    "2602902": [-35.0844, -8.2626],
    "2603009": [-39.3202, -8.3907],
    "2603108": [-36.2981, -8.4785],
    "2603207": [-36.6663, -8.8066],
    "2603306": [-36.3285, -8.7492],
    "2603405": [-38.0686, -7.9909],
    "2603454": [-34.9976, -7.9817],
    "2603504": [-35.7453, -8.361],
    "2603603": [-35.2968, -7.4274],
    "2603801": [-36.5686, -8.6888],
    "2603926": [-38.7494, -8.4246],
    "2604007": [-35.3253, -7.826],
    "2604106": [-36.018, -8.1733],
    "2604155": [-35.7184, -7.7721],
    "2604205": [-35.7436, -8.6305],
    "2604403": [-35.2138, -7.9997],
    "2604502": [-35.4666, -8.2269],
    "2604700": [-36.3253, -9.1294],
    "2604809": [-35.5283, -8.4462],
    "2604908": [-35.7236, -8.0315],
    "2605004": [-35.9101, -8.5821],
    "2605103": [-37.6785, -8.1214],
    "2605202": [-35.2676, -8.3595],
    "2605301": [-39.693, -7.5534],
    "2605400": [-35.3998, -7.9314],
    "2605608": [-37.9042, -7.9291],
    "2605707": [-38.3076, -8.5632],
    "2605806": [-35.8831, -7.944],
    "2605905": [-35.3918, -8.6045],
    "2606002": [-36.528, -8.9234],
    "2606101": [-35.3398, -8.0146],
    "2606309": [-39.6654, -7.7462],
    "2606408": [-35.5457, -8.2448],
    "2606507": [-36.922, -9.1683],
    "2606606": [-37.7873, -8.559],
    "2606705": [-36.1844, -8.6129],
    "2606804": [-34.9481, -7.8039],
    "2606903": [-37.4258, -7.8454],
    "2607000": [-37.8126, -8.7982],
    "2607109": [-37.4279, -7.7074],
    "2607208": [-35.059, -8.4548],
    "2607505": [-37.3118, -8.9675],
    "2607604": [-34.8522, -7.7512],
    "2607653": [-35.1684, -7.4486],
    "2607703": [-37.1313, -7.3975],
    "2607752": [-34.9018, -7.7325],
    "2607802": [-35.061, -7.6755],
    "2607901": [-34.9921, -8.1608],
    "2607950": [-35.8034, -8.7321],
    "2608057": [-38.1935, -9.2004],
    "2608107": [-35.5689, -7.8486],
    "2608206": [-35.5301, -8.5133],
    "2608255": [-36.4652, -8.7372],
    "2608305": [-36.3844, -8.7407],
    "2608404": [-36.1408, -8.7623],
    "2608453": [-35.3381, -7.8468],
    "2608503": [-35.3075, -7.9009],
    "2608602": [-36.4733, -9.1738],
    "2608701": [-35.8875, -8.6862],
    "2608750": [-40.2418, -8.7851],
    "2608800": [-36.2861, -8.6712],
    "2609006": [-35.4476, -7.5379],
    "2609105": [-35.5045, -7.7082],
    "2609204": [-35.7845, -8.832],
    "2609303": [-38.7187, -8.1746],
    "2609402": [-35.1377, -8.1557],
    "2609501": [-35.1801, -7.7128],
    "2609600": [-34.8712, -7.9915],
    "2609709": [-35.6198, -7.7193],
    "2609808": [-39.5888, -8.4888],
    "2609907": [-40.1396, -8.0155],
    "2610004": [-35.6446, -8.5945],
    "2610202": [-36.0405, -8.662],
    "2610301": [-36.699, -8.8935],
    "2610400": [-39.7497, -8.1974],
    "2610509": [-35.5405, -7.9838],
    "2610608": [-35.1336, -7.9208],
    "2610707": [-34.9074, -7.926],
    "2610806": [-36.9054, -8.6209],
    "2610905": [-36.7324, -8.3936],
    "2611002": [-38.3774, -8.8132],
    "2611101": [-40.5719, -9.0657],
    "2611200": [-36.7031, -8.217],
    "2611309": [-35.4055, -8.1917],
    "2611408": [-35.3976, -8.306],
    "2611507": [-36.0343, -8.8276],
    "2611533": [-37.8517, -7.7081],
    "2611606": [-34.9507, -8.0208],
    "2611705": [-35.8533, -8.0639],
    "2611804": [-35.4237, -8.483],
    "2612000": [-35.7079, -8.3309],
    "2612109": [-35.5634, -7.9166],
    "2612208": [-39.0707, -8.0837],
    "2612307": [-36.7293, -9.0059],
    "2612406": [-36.5306, -8.3296],
    "2612455": [-40.3096, -8.2956],
    "2612471": [-38.1666, -7.8514],
    "2612505": [-36.3252, -7.9068],
    "2612554": [-40.5917, -8.2702],
    "2612604": [-39.9207, -8.6179],
    "2612802": [-37.4421, -7.4351],
    "2612901": [-35.907, -8.8047],
    "2613008": [-36.4459, -8.5244],
    "2613107": [-36.1508, -8.3458],
    "2613305": [-35.8309, -8.4591],
    "2613404": [-35.1875, -8.8705],
    "2613503": [-38.7694, -7.8267],
    "2613602": [-37.2682, -7.5378],
    "2613701": [-35.1016, -8.0175],
    "2613800": [-35.4912, -7.596],
    "2613909": [-38.3702, -8.057],
    "2614006": [-39.3988, -7.8336],
    "2614105": [-37.3336, -8.2119],
    "2614204": [-35.1498, -8.5843],
    "2614303": [-39.5284, -7.6311],
    "2614402": [-37.6559, -7.5975],
    "2614501": [-35.7524, -7.853],
    "2614600": [-37.5033, -7.6004],
    "2614709": [-36.25, -8.3395],
    "2614808": [-38.0219, -8.9754],
    "2614857": [-35.1935, -8.7507],
    "2615003": [-36.1076, -7.8806],
    "2615102": [-36.6074, -9.0807],
    "2615201": [-39.387, -8.1647],
    "2615300": [-35.317, -7.5335],
    "2615508": [-35.1484, -7.7401],
    "2615706": [-38.0596, -7.8505],
    "2615904": [-37.2509, -7.6976],
    "2616001": [-36.8132, -8.5789],
    "2616100": [-38.9953, -7.9782],
    "2616183": [-35.8079, -7.7842],
    "2616209": [-35.99, -7.8993],
    "2616308": [-35.3669, -7.6549],
    "2616407": [-35.2877, -8.1449],
    "2616506": [-35.6388, -8.8461],
    "2700102": [-37.9093, -9.272],
    "2700300": [-36.6193, -9.7633],
    "2700409": [-36.0785, -9.5137],
    "2700508": [-35.5592, -9.3886],
    "2700805": [-36.4885, -9.5499],
    "2700904": [-37.1932, -9.8099],
    "2701001": [-36.1482, -9.6593],
    "2701100": [-36.0658, -9.216],
    "2701209": [-36.9128, -9.4376],
    "2701308": [-36.1547, -9.3812],
    "2701357": [-35.5388, -8.8875],
    "2701506": [-36.7562, -9.9575],
    "2701605": [-37.5314, -9.1379],
    "2701704": [-36.0903, -9.3531],
    "2701803": [-37.3682, -9.4671],
    "2701902": [-36.323, -9.2342],
    "2702009": [-36.5908, -9.6261],
    "2702108": [-35.7409, -8.9135],
    "2702207": [-35.8075, -9.6467],
    "2702306": [-36.2419, -10.0991],
    "2702405": [-38.0405, -9.4258],
    "2702504": [-37.0631, -9.3721],
    "2702553": [-36.763, -9.4065],
    "2702603": [-36.666, -9.8998],
    "2702702": [-36.3488, -10.2881],
    "2702801": [-35.7744, -9.2302],
    "2702900": [-36.8489, -9.7945],
    "2703007": [-35.9181, -8.9488],
    "2703106": [-36.6685, -9.5648],
    "2703304": [-37.6691, -9.2739],
    "2703403": [-37.2208, -9.6592],
    "2703502": [-35.456, -8.8759],
    "2703601": [-35.2892, -9.098],
    "2703700": [-36.9666, -9.6544],
    "2703809": [-35.6975, -9.1238],
    "2703908": [-35.5403, -8.9588],
    "2704005": [-36.4517, -9.8713],
    "2704203": [-36.4725, -9.7355],
    "2704302": [-35.6991, -9.5603],
    "2704401": [-36.9953, -9.5175],
    "2704500": [-35.269, -8.9505],
    "2704708": [-35.9121, -9.7058],
    "2704807": [-36.313, -9.5507],
    "2704906": [-36.4032, -9.4787],
    "2705002": [-37.743, -9.0082],
    "2705101": [-35.5565, -9.1209],
    "2705200": [-35.8166, -9.3323],
    "2705408": [-37.2929, -9.6113],
    "2705507": [-35.9415, -9.3092],
    "2705606": [-35.6026, -8.95],
    "2705705": [-37.252, -9.538],
    "2705804": [-37.8268, -9.4446],
    "2705903": [-36.7946, -10.0526],
    "2706000": [-37.1592, -9.4751],
    "2706109": [-37.3757, -9.1373],
    "2706208": [-37.3227, -9.6789],
    "2706307": [-36.6186, -9.4111],
    "2706406": [-37.4536, -9.6869],
    "2706422": [-38.0341, -9.23],
    "2706448": [-35.5915, -9.4299],
    "2706505": [-35.4699, -9.278],
    "2706703": [-36.4822, -10.2524],
    "2706802": [-36.4265, -10.3829],
    "2706901": [-36.0388, -9.6204],
    "2707008": [-36.3012, -9.4712],
    "2707107": [-37.7378, -9.5399],
    "2707206": [-37.3265, -9.2962],
    "2707305": [-35.3882, -9.0573],
    "2707404": [-35.3979, -9.135],
    "2707503": [-36.7241, -10.1046],
    "2708006": [-37.2099, -9.3522],
    "2708105": [-36.2034, -9.1141],
    "2708204": [-36.8693, -10.1075],
    "2708303": [-36.0312, -8.9868],
    "2708402": [-37.5187, -9.5267],
    "2708501": [-35.6159, -9.2267],
    "2708600": [-36.0903, -9.7863],
    "2708709": [-35.4004, -9.2368],
    "2708956": [-37.528, -9.3943],
    "2709004": [-36.412, -9.5735],
    "2709103": [-36.4909, -9.6149],
    "2709202": [-36.9725, -9.8975],
    "2709301": [-36.0339, -9.1085],
    "2800100": [-36.9193, -10.1459],
    "2800209": [-37.089, -10.2954],
    "2800308": [-37.1051, -10.9795],
    "2800407": [-37.5744, -11.2456],
    "2800506": [-37.3308, -10.7775],
    "2800605": [-36.9688, -10.8402],
    "2800670": [-37.6099, -11.1305],
    "2800704": [-36.4589, -10.4701],
    "2801009": [-37.5048, -10.7734],
    "2801108": [-36.9987, -10.144],
    "2801207": [-37.9282, -9.7141],
    "2801306": [-37.0509, -10.4397],
    "2801405": [-37.7335, -10.3482],
    "2801504": [-36.9432, -10.6728],
    "2801603": [-36.8903, -10.2689],
    "2801702": [-37.7151, -11.4955],
    "2801900": [-37.1825, -10.349],
    "2802007": [-37.1675, -10.6805],
    "2802106": [-37.4207, -11.2355],
    "2802205": [-37.3307, -10.3169],
    "2802304": [-37.5751, -10.5334],
    "2802403": [-37.224, -10.0507],
    "2802502": [-36.9719, -10.6943],
    "2802601": [-37.2021, -10.2295],
    "2802700": [-36.5575, -10.4471],
    "2802809": [-37.5315, -11.4734],
    "2802908": [-37.4215, -10.6856],
    "2803005": [-37.7847, -11.2613],
    "2803104": [-37.1772, -10.0953],
    "2803203": [-37.3388, -11.0391],
    "2803401": [-36.808, -10.4072],
    "2803500": [-37.6558, -10.8933],
    "2803609": [-37.1666, -10.8044],
    "2803708": [-37.5967, -10.6805],
    "2803807": [-36.9339, -10.3355],
    "2803906": [-37.2824, -10.6824],
    "2804003": [-37.1113, -10.7323],
    "2804102": [-37.3357, -10.5965],
    "2804201": [-37.5979, -10.0733],
    "2804300": [-36.9637, -10.3849],
    "2804409": [-36.6695, -10.3548],
    "2804458": [-37.4968, -10.3084],
    "2804508": [-37.534, -10.1895],
    "2804607": [-37.2475, -10.444],
    "2804706": [-37.0209, -10.0876],
    "2804805": [-37.1619, -10.8648],
    "2804904": [-36.6369, -10.5104],
    "2805000": [-37.6835, -10.6501],
    "2805109": [-37.6558, -11.2035],
    "2805208": [-37.7743, -10.5764],
    "2805406": [-37.7315, -9.8816],
    "2805505": [-38.1322, -10.8074],
    "2805604": [-37.423, -9.9594],
    "2805703": [-36.7954, -10.2469],
    "2805802": [-37.7915, -11.0211],
    "2805901": [-37.2227, -10.7186],
    "2806008": [-37.4087, -10.5156],
    "2806107": [-37.0374, -10.6843],
    "2806206": [-37.4763, -11.0688],
    "2806305": [-37.5196, -11.3701],
    "2806404": [-36.6257, -10.2774],
    "2806503": [-37.2339, -10.6304],
    "2806602": [-36.9639, -10.7746],
    "2806800": [-37.5672, -10.7777],
    "2806909": [-36.8601, -10.3253],
    "2807006": [-37.3528, -10.3795],
    "2807105": [-37.7971, -10.7257],
    "2807204": [-37.119, -10.5776],
    "2807303": [-36.8719, -10.1895],
    "2807402": [-38.0197, -11.0935],
    "2807501": [-37.877, -11.384],
    "2807600": [-37.6697, -11.3994],
    "2900108": [-41.7086, -13.3077],
    "2900207": [-39.2844, -8.8163],
    "2900306": [-37.9856, -11.6445],
    "2900355": [-38.0026, -10.511],
    "2900405": [-38.7431, -11.802],
    "2900504": [-42.0869, -13.4473],
    "2900603": [-39.8802, -14.0952],
    "2900702": [-38.3932, -12.0682],
    "2900801": [-39.3968, -17.4662],
    "2900900": [-39.6771, -14.6997],
    "2901007": [-39.6197, -13.0451],
    "2901106": [-38.7062, -12.4262],
    "2901155": [-41.4803, -11.4319],
    "2901205": [-41.0508, -14.6412],
    "2901304": [-41.2479, -12.8402],
    "2901353": [-39.8871, -10.2431],
    "2901502": [-39.2013, -12.1817],
    "2901601": [-38.3002, -10.4204],
    "2901908": [-38.2038, -11.7346],
    "2901957": [-39.7375, -13.8252],
    "2902104": [-39.107, -11.1515],
    "2902203": [-38.5413, -12.0419],
    "2902252": [-39.4258, -15.2512],
    "2902302": [-39.0719, -13.0903],
    "2902401": [-39.5077, -14.3374],
    "2902500": [-44.4392, -12.5782],
    "2902658": [-38.6268, -10.6187],
    "2902807": [-41.0826, -13.7044],
    "2902906": [-40.608, -14.946],
    "2903102": [-39.6093, -14.064],
    "2903235": [-41.8646, -11.8092],
    "2903276": [-39.1049, -11.5485],
    "2903300": [-39.4139, -14.7531],
    "2903409": [-39.3072, -15.8973],
    "2903508": [-41.2267, -14.8947],
    "2903607": [-38.7869, -11.6271],
    "2903706": [-40.226, -14.3412],
    "2903805": [-40.6683, -12.8663],
    "2903904": [-43.3137, -13.273],
    "2903953": [-40.5726, -14.3846],
    "2904050": [-41.3249, -12.0015],
    "2904100": [-42.6645, -12.7395],
    "2904209": [-42.5511, -13.3419],
    "2904308": [-39.8304, -13.0826],
    "2904605": [-41.6246, -14.146],
    "2904704": [-39.2901, -14.9937],
    "2904753": [-43.6882, -10.5621],
    "2904803": [-40.2843, -14.9892],
    "2904852": [-39.184, -12.5644],
    "2905008": [-42.3453, -14.448],
    "2905107": [-40.2394, -11.1796],
    "2905156": [-40.9489, -14.2981],
    "2905206": [-42.5155, -13.9734],
    "2905305": [-41.4862, -11.7236],
    "2905404": [-38.9617, -13.5141],
    "2905503": [-40.247, -11.0352],
    "2905602": [-39.5033, -15.4314],
    "2905800": [-39.2196, -14.0156],
    "2905909": [-43.0319, -9.5088],
    "2906105": [-44.2186, -13.106],
    "2906204": [-41.72, -11.7046],
    "2906303": [-39.1752, -15.653],
    "2906402": [-39.2003, -11.8824],
    "2906501": [-38.4932, -12.6779],
    "2906709": [-41.2203, -15.4052],
    "2906808": [-39.4355, -10.7758],
    "2906824": [-39.0891, -9.9231],
    "2906857": [-39.8212, -11.6607],
    "2906873": [-39.9867, -11.2976],
    "2906899": [-41.2603, -14.6103],
    "2906907": [-39.4497, -17.7515],
    "2907004": [-37.9139, -12.0145],
    "2907103": [-43.8631, -14.1874],
    "2907202": [-41.1744, -9.1287],
    "2907509": [-38.4265, -12.3577],
    "2907608": [-42.0559, -11.204],
    "2907707": [-39.1478, -9.2901],
    "2907806": [-38.4246, -10.5549],
    "2907905": [-38.5008, -11.1158],
    "2908002": [-39.5934, -14.6412],
    "2908200": [-38.9811, -12.5076],
    "2908309": [-39.2419, -12.8557],
    "2908507": [-38.7414, -12.3254],
    "2908606": [-37.6637, -11.8079],
    "2908705": [-42.0341, -14.9451],
    "2908903": [-38.7869, -12.2344],
    "2909000": [-41.8983, -15.0137],
    "2909109": [-44.4039, -13.7212],
    "2909208": [-37.9735, -10.3196],
    "2909307": [-45.1308, -13.3362],
    "2909505": [-39.7731, -13.447],
    "2909604": [-38.1209, -11.5191],
    "2909703": [-44.2161, -12.194],
    "2909802": [-39.1169, -12.6848],
    "2910057": [-38.2851, -12.5929],
    "2910107": [-41.7157, -13.8028],
    "2910305": [-39.5252, -12.9344],
    "2910404": [-40.9265, -15.5712],
    "2910503": [-38.0207, -12.0838],
    "2910602": [-37.8938, -11.947],
    "2910701": [-38.927, -10.4591],
    "2910727": [-39.6469, -16.3048],
    "2910750": [-38.203, -10.5752],
    "2910800": [-39.0405, -12.2246],
    "2910859": [-40.1851, -10.7516],
    "2910909": [-39.9098, -14.9195],
    "2911006": [-39.7463, -14.8317],
    "2911204": [-39.4705, -13.7835],
    "2911253": [-39.7647, -11.4877],
    "2911303": [-42.5511, -11.3687],
    "2911402": [-38.3979, -9.1788],
    "2911600": [-39.0777, -12.5841],
    "2911659": [-42.003, -14.5844],
    "2911709": [-42.8019, -14.1802],
    "2911808": [-39.9718, -16.5685],
    "2911857": [-38.2855, -10.7289],
    "2911907": [-40.1587, -12.7828],
    "2912004": [-42.3043, -14.2321],
    "2912103": [-39.5693, -14.8653],
    "2912202": [-41.371, -13.3862],
    "2912400": [-42.179, -11.544],
    "2912608": [-40.8388, -12.5901],
    "2912707": [-39.4102, -14.0531],
    "2913002": [-42.35, -12.5592],
    "2913101": [-41.8693, -11.6398],
    "2913309": [-39.1734, -11.7181],
    "2913408": [-42.7581, -13.8707],
    "2913457": [-39.1868, -13.8567],
    "2913606": [-39.2164, -14.7292],
    "2913705": [-38.3732, -11.8053],
    "2913804": [-39.3337, -12.3254],
    "2913903": [-39.7284, -14.0546],
    "2914406": [-41.5721, -12.2879],
    "2914604": [-41.848, -11.3046],
    "2914703": [-40.2133, -12.4565],
    "2914802": [-39.351, -14.8576],
    "2914901": [-39.127, -14.3605],
    "2915007": [-40.9632, -13.0032],
    "2915106": [-40.0333, -14.1785],
    "2915205": [-39.8306, -14.2557],
    "2915304": [-39.7874, -16.1218],
    "2915353": [-42.2211, -10.6681],
    "2915403": [-39.7283, -15.1536],
    "2915502": [-39.426, -14.6881],
    "2915601": [-39.8146, -16.9469],
    "2915700": [-39.6678, -13.7702],
    "2915809": [-40.5267, -15.2039],
    "2915908": [-38.0945, -12.3151],
    "2916005": [-40.3372, -17.1006],
    "2916104": [-38.6697, -12.8859],
    "2916203": [-39.5133, -14.915],
    "2916302": [-39.686, -15.8903],
    "2916401": [-40.084, -15.3297],
    "2916500": [-38.1461, -11.1838],
    "2916609": [-39.6155, -14.4682],
    "2916807": [-40.0272, -15.7123],
    "2916856": [-39.7067, -12.6667],
    "2916906": [-40.1633, -13.4907],
    "2917003": [-39.8099, -10.7674],
    "2917102": [-40.0167, -15.0589],
    "2917300": [-39.156, -13.7777],
    "2917334": [-43.6324, -14.6113],
    "2917508": [-40.4424, -11.1722],
    "2917607": [-39.9279, -13.5826],
    "2917805": [-38.9977, -13.1102],
    "2917904": [-37.6197, -11.6031],
    "2918001": [-40.0774, -13.9309],
    "2918100": [-38.6733, -9.9035],
    "2918209": [-39.5824, -13.3056],
    "2918308": [-39.875, -13.9653],
    "2918357": [-41.5246, -11.2068],
    "2918407": [-40.2691, -9.4426],
    "2918456": [-40.0889, -16.8464],
    "2918506": [-41.9076, -11.018],
    "2918555": [-39.5132, -15.1561],
    "2918605": [-41.6161, -13.4737],
    "2918704": [-40.2846, -13.6241],
    "2918753": [-42.2241, -14.0917],
    "2918803": [-39.3228, -13.1801],
    "2918902": [-40.3093, -17.5813],
    "2919009": [-41.0762, -12.4197],
    "2919058": [-40.2454, -13.4121],
    "2919157": [-41.7525, -11.5483],
    "2919207": [-38.33, -12.8454],
    "2919306": [-41.311, -12.4655],
    "2919504": [-41.9881, -13.7987],
    "2919603": [-40.2887, -12.1325],
    "2919702": [-40.3862, -15.5523],
    "2919926": [-38.622, -12.7368],
    "2919959": [-41.4898, -14.6658],
    "2920007": [-40.2683, -15.697],
    "2920106": [-40.1977, -11.6662],
    "2920205": [-43.654, -14.1455],
    "2920304": [-41.8481, -14.3276],
    "2920403": [-40.4757, -14.0472],
    "2920452": [-44.1384, -11.2151],
    "2920502": [-40.6062, -13.612],
    "2920601": [-38.9319, -12.8338],
    "2920809": [-40.634, -13.1158],
    "2921054": [-42.9603, -13.8731],
    "2921104": [-40.2718, -17.3842],
    "2921401": [-40.6911, -10.8016],
    "2921450": [-40.7654, -14.1847],
    "2921500": [-39.5071, -10.311],
    "2921609": [-43.084, -11.7161],
    "2921708": [-41.3202, -11.2972],
    "2921807": [-42.3834, -14.9723],
    "2921906": [-41.4753, -13.1337],
    "2922052": [-41.4839, -12.0024],
    "2922102": [-40.5475, -11.9222],
    "2922201": [-39.1047, -12.9996],
    "2922409": [-39.5077, -13.3259],
    "2922508": [-38.9768, -12.9581],
    "2922607": [-39.2194, -13.647],
    "2922706": [-40.1889, -14.8451],
    "2922730": [-39.6198, -11.6029],
    "2922805": [-40.006, -13.047],
    "2922854": [-41.1119, -12.8654],
    "2922904": [-38.4742, -11.2986],
    "2923001": [-39.6709, -17.8733],
    "2923100": [-38.3571, -11.3774],
    "2923209": [-42.7078, -12.2917],
    "2923407": [-43.2218, -14.044],
    "2923506": [-41.5501, -12.5002],
    "2923803": [-37.8954, -10.5883],
    "2923902": [-39.6964, -15.4485],
    "2924009": [-38.2157, -9.5306],
    "2924058": [-39.6179, -11.8733],
    "2924108": [-38.6186, -12.1342],
    "2924207": [-37.9116, -10.062],
    "2924306": [-41.8763, -13.0761],
    "2924504": [-42.6725, -14.4972],
    "2924603": [-40.3261, -10.737],
    "2924702": [-41.7463, -14.9372],
    "2924900": [-40.2513, -13.2417],
    "2925006": [-40.4184, -14.7554],
    "2925105": [-40.3136, -14.5598],
    "2925253": [-40.1526, -10.8912],
    "2925303": [-39.2789, -16.6487],
    "2925402": [-39.757, -15.6998],
    "2925501": [-39.3525, -17.1279],
    "2925600": [-41.9916, -11.2747],
    "2925709": [-41.7415, -14.745],
    "2925758": [-39.4256, -13.4632],
    "2925808": [-39.7145, -10.9738],
    "2925907": [-39.0237, -10.765],
    "2925931": [-40.1282, -11.4054],
    "2925956": [-39.5077, -12.4825],
    "2926004": [-42.3455, -9.5584],
    "2926103": [-39.4042, -11.4782],
    "2926202": [-45.0727, -11.7637],
    "2926301": [-39.3963, -11.835],
    "2926400": [-43.1477, -13.8014],
    "2926509": [-38.3616, -10.9617],
    "2926608": [-38.4914, -10.697],
    "2926657": [-40.6593, -15.3865],
    "2926707": [-41.6928, -13.6416],
    "2926806": [-42.1081, -14.2882],
    "2927002": [-37.9033, -11.5248],
    "2927101": [-38.6744, -9.0425],
    "2927309": [-38.7664, -12.8892],
    "2927408": [-38.4933, -12.8431],
    "2927507": [-38.9893, -11.9206],
    "2927705": [-39.1897, -16.2698],
    "2927903": [-39.8414, -13.2798],
    "2928000": [-39.5028, -11.1674],
    "2928059": [-39.2806, -15.4559],
    "2928109": [-44.3833, -13.2673],
    "2928208": [-43.8702, -13.0724],
    "2928307": [-38.8665, -12.0227],
    "2928406": [-44.5894, -11.0175],
    "2928505": [-39.5517, -12.6902],
    "2928604": [-38.7791, -12.5636],
    "2928703": [-39.216, -13.0031],
    "2928802": [-39.2774, -12.4681],
    "2928901": [-45.5821, -12.8524],
    "2929057": [-44.0012, -13.4112],
    "2929107": [-39.0968, -12.8484],
    "2929255": [-41.63, -11.018],
    "2929305": [-38.9483, -12.4197],
    "2929354": [-39.3577, -15.0742],
    "2929404": [-39.4195, -13.0475],
    "2929602": [-39.2226, -12.7306],
    "2929701": [-38.5509, -11.6217],
    "2929750": [-38.8011, -12.7651],
    "2929909": [-41.8894, -12.4094],
    "2930105": [-40.1122, -10.4431],
    "2930154": [-43.7426, -13.3965],
    "2930204": [-41.5825, -10.195],
    "2930402": [-39.3514, -12.1014],
    "2930501": [-38.9797, -11.6242],
    "2930600": [-40.2243, -11.4442],
    "2930709": [-38.3921, -12.7824],
    "2930758": [-43.5224, -12.967],
    "2930774": [-40.851, -9.6787],
    "2930907": [-44.1009, -12.4569],
    "2931004": [-41.1319, -14.0964],
    "2931053": [-42.5391, -13.5681],
    "2931103": [-39.0903, -11.9429],
    "2931202": [-39.2117, -13.5694],
    "2931301": [-40.7691, -11.9243],
    "2931350": [-39.8967, -17.3395],
    "2931509": [-38.9535, -11.5109],
    "2931608": [-39.4715, -13.5634],
    "2931707": [-38.5891, -12.3952],
    "2931806": [-41.3663, -14.952],
    "2931905": [-38.8458, -10.9788],
    "2932002": [-39.3009, -9.866],
    "2932101": [-39.6869, -13.2722],
    "2932200": [-39.392, -14.2723],
    "2932309": [-39.5259, -14.0553],
    "2932408": [-42.1104, -11.3455],
    "2932457": [-41.1811, -10.5178],
    "2932507": [-39.2086, -15.1784],
    "2932606": [-42.6672, -14.739],
    "2932705": [-39.1873, -14.4955],
    "2933000": [-39.4677, -11.3973],
    "2933059": [-40.0708, -11.5627],
    "2933158": [-41.0697, -11.1033],
    "2933174": [-39.3753, -12.9865],
    "2933257": [-40.0433, -17.1519],
    "2933307": [-40.9195, -15.14],
    "2933406": [-41.1908, -12.239],
    "2933505": [-39.5993, -13.6313],
    "3100302": [-42.4295, -20.2636],
    "3100401": [-43.0985, -20.3993],
    "3100500": [-42.4329, -19.0485],
    "3100609": [-42.2573, -18.0464],
    "3100708": [-48.1062, -19.9815],
    "3100906": [-40.9815, -17.0378],
    "3101003": [-41.5538, -15.6752],
    "3101102": [-41.1996, -19.6098],
    "3101300": [-44.6589, -22.1816],
    "3101409": [-46.6242, -22.197],
    "3101508": [-42.7501, -21.8033],
    "3101706": [-40.7241, -16.1075],
    "3101805": [-42.0075, -18.9832],
    "3101904": [-46.3814, -20.8325],
    "3102001": [-46.1741, -21.2191],
    "3102050": [-41.882, -20.4617],
    "3102100": [-43.4074, -21.0155],
    "3102209": [-41.696, -19.422],
    "3102308": [-43.1368, -20.1157],
    "3102407": [-43.3769, -18.7838],
    "3102506": [-42.7942, -20.5198],
    "3102605": [-46.5644, -22.0701],
    "3102704": [-41.5094, -15.9804],
    "3102803": [-44.2869, -21.7051],
    "3102852": [-42.2873, -17.6988],
    "3102902": [-43.7688, -21.3989],
    "3103009": [-42.89, -19.5609],
    "3103108": [-42.1549, -21.0186],
    "3103306": [-43.4066, -21.3524],
    "3103504": [-48.2831, -18.5833],
    "3103603": [-44.2274, -21.9053],
    "3103702": [-42.5061, -20.6632],
    "3103801": [-46.099, -19.0337],
    "3103900": [-45.1615, -19.8752],
    "3104007": [-46.9775, -19.6158],
    "3104106": [-46.9369, -21.3588],
    "3104205": [-45.5703, -20.2389],
    "3104304": [-46.1586, -21.3411],
    "3104403": [-42.8266, -21.6384],
    "3104452": [-42.5998, -17.8622],
    "3104502": [-45.9766, -15.7864],
    "3104601": [-42.8812, -21.3109],
    "3104700": [-41.1624, -18.1811],
    "3104809": [-44.1823, -18.1082],
    "3104908": [-44.8458, -21.9784],
    "3105004": [-43.851, -19.2331],
    "3105103": [-45.9852, -20.1294],
    "3105202": [-40.5968, -15.8714],
    "3105301": [-46.3818, -21.7274],
    "3105400": [-43.4991, -19.881],
    "3105509": [-42.2773, -21.2616],
    "3105608": [-43.8299, -21.2462],
    "3105905": [-43.9524, -21.1811],
    "3106002": [-43.1009, -19.8037],
    "3106101": [-43.4609, -21.9653],
    "3106200": [-43.9596, -19.8876],
    "3106309": [-42.4372, -19.2549],
    "3106408": [-44.0681, -20.4036],
    "3106507": [-42.4909, -16.8703],
    "3106606": [-40.5648, -16.9469],
    "3106655": [-41.7731, -15.6808],
    "3106705": [-44.1941, -19.9574],
    "3106804": [-43.7585, -21.6355],
    "3106903": [-43.0933, -21.7357],
    "3107109": [-45.6425, -21.0667],
    "3107208": [-44.4992, -22.2379],
    "3107307": [-43.6689, -17.3092],
    "3107505": [-44.1331, -21.9446],
    "3107604": [-46.5468, -21.0005],
    "3107802": [-42.3587, -19.7331],
    "3107901": [-46.1847, -22.4464],
    "3108008": [-44.7924, -21.0334],
    "3108107": [-44.2092, -20.3287],
    "3108206": [-46.1534, -16.5358],
    "3108255": [-44.8826, -14.8986],
    "3108305": [-46.1598, -22.247],
    "3108503": [-43.0199, -16.904],
    "3108552": [-45.9281, -16.9329],
    "3108602": [-44.4617, -16.2645],
    "3108701": [-43.2311, -20.8743],
    "3108800": [-42.7209, -19.0149],
    "3108909": [-45.629, -22.4957],
    "3109105": [-46.354, -22.5028],
    "3109204": [-44.0258, -17.8667],
    "3109253": [-42.3065, -19.3648],
    "3109303": [-46.5491, -15.4178],
    "3109402": [-45.2818, -17.3704],
    "3109451": [-47.1585, -16.0813],
    "3109501": [-46.3877, -21.488],
    "3109600": [-44.4644, -19.5149],
    "3109709": [-45.7891, -22.3545],
    "3109808": [-49.4688, -18.595],
    "3109907": [-44.4089, -19.3347],
    "3110103": [-41.9, -20.7256],
    "3110202": [-42.7555, -20.7852],
    "3110301": [-46.3598, -21.884],
    "3110400": [-45.1304, -20.6411],
    "3110509": [-46.0729, -22.7818],
    "3110608": [-46.0682, -22.5834],
    "3110707": [-45.2653, -21.8519],
    "3110806": [-41.7449, -18.2782],
    "3110905": [-45.3944, -21.8061],
    "3111002": [-46.2337, -21.7329],
    "3111101": [-49.7085, -19.486],
    "3111150": [-44.7875, -16.5226],
    "3111200": [-45.2398, -20.9255],
    "3111507": [-46.1907, -19.6089],
    "3111606": [-45.7269, -21.2508],
    "3111705": [-42.637, -20.6681],
    "3111903": [-45.1941, -21.0253],
    "3112000": [-45.2724, -20.7502],
    "3112059": [-42.6494, -18.5119],
    "3112109": [-41.9464, -20.525],
    "3112208": [-43.6064, -20.9146],
    "3112307": [-42.4975, -17.6899],
    "3112505": [-44.1615, -19.5682],
    "3112604": [-49.58, -18.686],
    "3112653": [-41.8251, -19.0459],
    "3112703": [-43.6602, -16.045],
    "3112802": [-46.1523, -20.5946],
    "3113008": [-41.5596, -17.1754],
    "3113107": [-43.7212, -20.8913],
    "3113206": [-43.823, -20.9892],
    "3113305": [-42.1058, -20.7023],
    "3113404": [-42.1168, -19.7022],
    "3113503": [-43.0567, -17.4947],
    "3113602": [-45.6638, -22.0706],
    "3113701": [-40.8657, -17.6732],
    "3113800": [-43.1859, -19.069],
    "3113909": [-45.186, -21.446],
    "3114006": [-44.8813, -20.5676],
    "3114105": [-45.1558, -22.0876],
    "3114204": [-44.7104, -20.1907],
    "3114303": [-46.1344, -18.8646],
    "3114402": [-46.1141, -20.9864],
    "3114600": [-44.5938, -21.4951],
    "3114709": [-45.825, -21.7725],
    "3114808": [-44.479, -22.0207],
    "3114907": [-43.9391, -20.8316],
    "3115003": [-47.853, -18.5397],
    "3115201": [-44.4866, -21.1364],
    "3115300": [-42.6525, -21.3462],
    "3115409": [-43.5, -20.6755],
    "3115458": [-41.4936, -17.3685],
    "3115474": [-43.1046, -15.3275],
    "3115508": [-44.948, -21.9757],
    "3115607": [-45.7057, -19.1059],
    "3115706": [-41.2831, -18.7686],
    "3115805": [-49.1655, -18.607],
    "3115904": [-43.22, -21.677],
    "3116001": [-41.6764, -20.0322],
    "3116100": [-42.373, -17.1647],
    "3116159": [-45.4706, -15.4562],
    "3116209": [-43.017, -21.9801],
    "3116308": [-43.3575, -20.9278],
    "3116506": [-44.2307, -17.0983],
    "3116605": [-44.7855, -20.3891],
    "3116704": [-42.7904, -20.8453],
    "3117009": [-41.7671, -16.2804],
    "3117108": [-46.2296, -21.089],
    "3117207": [-45.4162, -22.1412],
    "3117306": [-48.319, -19.9473],
    "3117405": [-41.701, -19.9212],
    "3117603": [-44.8756, -19.7857],
    "3117702": [-45.0929, -21.9004],
    "3117801": [-45.7774, -22.4481],
    "3117836": [-44.6031, -15.0065],
    "3117876": [-43.9756, -19.6473],
    "3117900": [-46.0386, -22.1427],
    "3118106": [-43.6941, -18.8788],
    "3118205": [-47.6241, -19.8756],
    "3118304": [-43.7979, -20.6588],
    "3118403": [-41.442, -19.1647],
    "3118601": [-44.0802, -19.8852],
    "3118700": [-45.418, -21.1788],
    "3118809": [-44.4428, -16.6115],
    "3118908": [-44.1677, -19.091],
    "3119005": [-45.6713, -21.7803],
    "3119104": [-44.6187, -18.3528],
    "3119203": [-42.2586, -18.6112],
    "3119302": [-47.1568, -18.4022],
    "3119401": [-42.6835, -19.4559],
    "3119500": [-42.1941, -16.5792],
    "3119609": [-43.2921, -21.6045],
    "3119708": [-44.1957, -21.0294],
    "3119807": [-45.9669, -19.7929],
    "3119906": [-45.9868, -22.6271],
    "3119955": [-45.5402, -20.4408],
    "3120102": [-43.4315, -18.0918],
    "3120151": [-40.9759, -17.2446],
    "3120201": [-45.5366, -20.8046],
    "3120300": [-42.8176, -16.7067],
    "3120409": [-43.8322, -20.8384],
    "3120508": [-45.2913, -22.2116],
    "3120607": [-44.358, -20.3961],
    "3120706": [-46.6654, -18.971],
    "3120805": [-44.802, -21.7331],
    "3120839": [-41.1306, -18.9979],
    "3120870": [-41.7611, -15.8426],
    "3120904": [-44.4186, -18.8002],
    "3121001": [-43.6562, -18.4681],
    "3121100": [-45.2889, -22.5028],
    "3121209": [-46.7402, -20.3627],
    "3121258": [-47.8066, -19.9254],
    "3121308": [-42.9648, -21.4504],
    "3121407": [-44.2681, -20.6358],
    "3121605": [-43.5862, -17.959],
    "3121704": [-43.1846, -20.4745],
    "3121803": [-42.6988, -19.8375],
    "3122009": [-42.198, -20.5787],
    "3122108": [-41.4987, -18.7073],
    "3122207": [-42.5676, -18.7788],
    "3122306": [-44.9152, -20.1091],
    "3122355": [-41.3855, -15.6868],
    "3122454": [-40.9021, -15.7671],
    "3122470": [-46.3026, -16.7986],
    "3122504": [-42.0889, -19.3836],
    "3122603": [-43.2665, -18.9302],
    "3122702": [-42.9396, -20.1245],
    "3122801": [-45.153, -22.234],
    "3122900": [-42.8066, -21.3274],
    "3123007": [-43.9966, -21.1116],
    "3123106": [-42.9258, -19.0448],
    "3123205": [-45.535, -19.4703],
    "3123304": [-43.1623, -21.0249],
    "3123403": [-45.8951, -20.2896],
    "3123502": [-47.6117, -18.4399],
    "3123528": [-41.7862, -20.1446],
    "3123601": [-45.5965, -21.5948],
    "3123700": [-42.0183, -19.1135],
    "3123809": [-44.0339, -17.3025],
    "3123858": [-42.2413, -19.6534],
    "3123908": [-44.1224, -20.697],
    "3124005": [-42.6138, -20.8457],
    "3124104": [-44.3292, -19.7414],
    "3124203": [-41.9178, -20.5954],
    "3124302": [-42.9694, -14.8675],
    "3124401": [-45.9826, -22.0114],
    "3124500": [-46.0249, -22.4516],
    "3124609": [-42.4663, -21.7014],
    "3124807": [-47.6999, -18.7113],
    "3124906": [-42.22, -21.0175],
    "3125002": [-43.5511, -21.5683],
    "3125101": [-46.2905, -22.8189],
    "3125309": [-42.0279, -20.7804],
    "3125408": [-43.2419, -18.1531],
    "3125606": [-40.7228, -16.6588],
    "3125705": [-44.9342, -18.7053],
    "3125903": [-42.9696, -19.2316],
    "3126000": [-44.4358, -19.862],
    "3126109": [-45.4718, -20.5348],
    "3126208": [-46.1269, -15.1436],
    "3126307": [-46.7742, -20.8787],
    "3126505": [-42.2764, -16.9439],
    "3126604": [-44.2247, -17.3932],
    "3126703": [-43.4916, -16.4097],
    "3126752": [-41.9725, -17.9961],
    "3126802": [-41.5028, -18.1317],
    "3126901": [-41.8727, -18.5182],
    "3126950": [-42.7656, -18.139],
    "3127008": [-49.1674, -20.2141],
    "3127057": [-40.8298, -16.8895],
    "3127073": [-42.5361, -16.1573],
    "3127107": [-48.9626, -19.9951],
    "3127305": [-41.5168, -18.8715],
    "3127339": [-43.2925, -14.9784],
    "3127354": [-43.6549, -16.8882],
    "3127370": [-41.2249, -19.0284],
    "3127404": [-45.825, -22.6726],
    "3127503": [-42.5002, -18.8743],
    "3127602": [-43.8727, -18.5447],
    "3127701": [-41.9749, -18.7726],
    "3127800": [-42.9647, -16.4957],
    "3128006": [-42.8115, -18.8555],
    "3128204": [-43.0105, -20.5605],
    "3128253": [-43.61, -17.0773],
    "3128303": [-46.8166, -21.2814],
    "3128402": [-43.0549, -21.3462],
    "3128501": [-43.0247, -21.7515],
    "3128600": [-47.1346, -17.7489],
    "3128709": [-46.684, -21.288],
    "3128808": [-42.7922, -21.1715],
    "3129004": [-42.6978, -21.0063],
    "3129103": [-49.8822, -19.0407],
    "3129202": [-45.5285, -22.0419],
    "3129301": [-42.2366, -19.3535],
    "3129400": [-43.9452, -21.4492],
    "3129509": [-46.6088, -19.5368],
    "3129608": [-44.7922, -16.817],
    "3129657": [-44.1317, -15.6536],
    "3129707": [-47.1308, -20.3904],
    "3129905": [-46.4085, -22.0683],
    "3130002": [-44.781, -21.1635],
    "3130051": [-44.8689, -16.2218],
    "3130200": [-44.7183, -19.9644],
    "3130309": [-45.7488, -20.1356],
    "3130507": [-45.8138, -20.9294],
    "3130556": [-41.9712, -19.6331],
    "3130606": [-46.2796, -22.3347],
    "3130705": [-47.8892, -18.9762],
    "3130804": [-44.93, -21.4154],
    "3130903": [-41.9646, -19.5002],
    "3131000": [-44.4157, -19.4972],
    "3131109": [-44.2723, -18.7259],
    "3131158": [-42.3637, -19.405],
    "3131307": [-42.6037, -19.4345],
    "3131406": [-49.9337, -18.7089],
    "3131505": [-46.1347, -22.0233],
    "3131604": [-47.4536, -19.0645],
    "3131802": [-41.2537, -18.5314],
    "3131901": [-43.7844, -20.2476],
    "3132008": [-43.3088, -16.8795],
    "3132107": [-44.1503, -15.1625],
    "3132206": [-44.5369, -20.3696],
    "3132305": [-41.6576, -17.4106],
    "3132404": [-45.4184, -22.4273],
    "3132503": [-42.8821, -17.8573],
    "3132602": [-42.8382, -21.4191],
    "3132701": [-41.8723, -18.1744],
    "3132800": [-43.3502, -19.4074],
    "3132909": [-47.0503, -21.0806],
    "3133006": [-44.7625, -22.2845],
    "3133105": [-44.9095, -22.3217],
    "3133204": [-41.832, -19.155],
    "3133303": [-41.5309, -16.5593],
    "3133402": [-49.4478, -19.7495],
    "3133501": [-45.1031, -20.4554],
    "3133600": [-46.214, -22.702],
    "3133758": [-46.774, -20.73],
    "3133808": [-44.5837, -20.074],
    "3133907": [-43.6006, -20.6963],
    "3134004": [-41.8532, -16.5964],
    "3134103": [-41.0954, -19.3723],
    "3134202": [-49.5477, -19.0015],
    "3134301": [-44.8379, -21.2729],
    "3134400": [-50.3661, -19.6972],
    "3134509": [-44.7222, -21.3368],
    "3134608": [-43.7451, -19.436],
    "3134707": [-40.3283, -16.1836],
    "3134806": [-46.7243, -21.0171],
    "3134905": [-46.6007, -22.286],
    "3135001": [-42.7195, -19.6393],
    "3135050": [-43.6744, -15.2082],
    "3135076": [-41.7576, -18.4627],
    "3135100": [-43.3808, -15.8298],
    "3135209": [-44.872, -15.3188],
    "3135308": [-45.5277, -20.1316],
    "3135357": [-44.3391, -15.9366],
    "3135407": [-44.0336, -20.5602],
    "3135456": [-42.2096, -17.1869],
    "3135506": [-42.6261, -20.4716],
    "3135605": [-44.4625, -17.1906],
    "3135704": [-44.0243, -19.2188],
    "3135803": [-41.0539, -16.4],
    "3135902": [-45.279, -22.0089],
    "3136009": [-41.0166, -16.8041],
    "3136207": [-43.1597, -19.8374],
    "3136306": [-45.9811, -17.5431],
    "3136405": [-44.1045, -17.6386],
    "3136504": [-40.3047, -15.8766],
    "3136520": [-42.6588, -16.9112],
    "3136553": [-42.4689, -18.2576],
    "3136579": [-42.5234, -16.5429],
    "3136603": [-43.5749, -19.6422],
    "3136652": [-44.3462, -19.9597],
    "3136702": [-43.4429, -21.7642],
    "3136900": [-46.5156, -21.2232],
    "3136959": [-44.0867, -14.3516],
    "3137007": [-41.8327, -17.6293],
    "3137106": [-46.7086, -17.9931],
    "3137304": [-44.6794, -17.0149],
    "3137403": [-44.0644, -20.9062],
    "3137536": [-46.5298, -17.7293],
    "3137601": [-43.8855, -19.6208],
    "3137700": [-41.6014, -20.1229],
    "3137809": [-45.3683, -21.9898],
    "3137908": [-43.4734, -20.7743],
    "3138005": [-42.458, -21.3585],
    "3138104": [-44.6976, -17.8963],
    "3138203": [-45.0389, -21.2558],
    "3138302": [-45.0575, -19.6795],
    "3138351": [-42.7416, -17.0526],
    "3138401": [-42.6476, -21.5276],
    "3138500": [-44.3466, -22.0188],
    "3138609": [-43.8805, -21.7959],
    "3138625": [-50.6277, -19.3885],
    "3138658": [-44.2749, -15.8493],
    "3138674": [-42.0758, -20.4378],
    "3138682": [-44.5997, -16.1917],
    "3138708": [-44.9169, -21.5225],
    "3138807": [-45.6709, -19.8515],
    "3138906": [-40.7203, -17.0817],
    "3139003": [-45.8919, -21.6975],
    "3139102": [-44.3285, -21.4858],
    "3139201": [-42.1007, -17.8388],
    "3139250": [-42.9429, -15.0187],
    "3139300": [-44.1244, -14.6655],
    "3139409": [-42.0914, -20.1867],
    "3139508": [-41.9364, -20.3457],
    "3139607": [-41.0941, -18.6967],
    "3139706": [-44.6699, -19.5028],
    "3139805": [-43.0234, -21.874],
    "3139904": [-45.3085, -22.322],
    "3140100": [-42.0679, -18.4919],
    "3140209": [-42.9635, -21.6977],
    "3140308": [-42.6376, -19.6919],
    "3140407": [-45.1738, -22.4604],
    "3140506": [-45.199, -19.4075],
    "3140530": [-41.8433, -20.2616],
    "3140555": [-40.701, -15.7689],
    "3140605": [-43.018, -18.4472],
    "3140704": [-44.4347, -20.0193],
    "3140803": [-43.3089, -21.8718],
    "3140852": [-43.7084, -14.8521],
    "3140902": [-42.3197, -20.294],
    "3141009": [-42.8769, -15.4325],
    "3141108": [-44.0416, -19.5152],
    "3141207": [-45.9982, -19.1752],
    "3141306": [-46.365, -20.0064],
    "3141405": [-41.5367, -16.2916],
    "3141702": [-42.6237, -19.2532],
    "3141801": [-42.4253, -17.3471],
    "3142007": [-44.1621, -16.2745],
    "3142106": [-42.3927, -20.8437],
    "3142205": [-42.6067, -21.1484],
    "3142254": [-44.4183, -14.7376],
    "3142304": [-44.014, -20.3323],
    "3142403": [-45.4243, -19.8378],
    "3142601": [-45.477, -21.7195],
    "3142700": [-44.4914, -14.4314],
    "3142809": [-48.9405, -18.7964],
    "3142908": [-43.0046, -15.2121],
    "3143005": [-46.3279, -21.3183],
    "3143104": [-47.4884, -18.6429],
    "3143153": [-41.2699, -16.8756],
    "3143203": [-46.9499, -21.1983],
    "3143302": [-43.9099, -16.6067],
    "3143401": [-46.5301, -22.4207],
    "3143500": [-45.4506, -18.5729],
    "3143609": [-44.6353, -18.6214],
    "3143708": [-43.3994, -19.2286],
    "3143807": [-46.3113, -22.6243],
    "3143906": [-42.4375, -21.0665],
    "3144003": [-41.452, -19.9203],
    "3144102": [-46.5225, -21.3508],
    "3144300": [-40.5271, -17.7892],
    "3144359": [-42.3319, -19.1809],
    "3144375": [-46.5063, -16.5634],
    "3144409": [-45.4985, -22.1308],
    "3144508": [-44.616, -21.2061],
    "3144607": [-45.2496, -21.2148],
    "3144656": [-41.6543, -15.4155],
    "3144672": [-41.0858, -18.4935],
    "3144805": [-43.89, -20.0727],
    "3144904": [-41.5189, -18.4531],
    "3145000": [-47.7142, -19.2651],
    "3145059": [-43.2804, -15.7226],
    "3145109": [-46.4171, -21.0961],
    "3145208": [-44.9782, -19.8468],
    "3145307": [-41.9638, -17.3807],
    "3145356": [-41.2309, -17.243],
    "3145372": [-42.3998, -16.0035],
    "3145406": [-43.9575, -21.9032],
    "3145455": [-43.5646, -17.5095],
    "3145505": [-45.2757, -22.085],
    "3145604": [-44.7121, -20.7385],
    "3145703": [-43.5239, -21.3394],
    "3145802": [-44.7462, -19.7155],
    "3145851": [-42.7949, -20.4384],
    "3145877": [-42.2118, -20.5086],
    "3145901": [-43.6913, -20.5361],
    "3146008": [-46.3816, -22.2605],
    "3146107": [-43.6313, -20.3896],
    "3146206": [-41.2985, -18.0365],
    "3146255": [-42.5975, -16.2704],
    "3146305": [-41.5351, -17.0644],
    "3146404": [-45.5209, -18.9101],
    "3146503": [-45.7076, -20.3843],
    "3146602": [-43.4228, -21.2883],
    "3146701": [-42.325, -21.426],
    "3146750": [-40.3703, -16.7749],
    "3146909": [-44.6833, -19.3824],
    "3147105": [-44.6051, -19.8225],
    "3147204": [-45.7365, -21.5695],
    "3147303": [-45.8413, -22.5809],
    "3147402": [-44.4517, -19.2993],
    "3147501": [-43.1869, -19.3517],
    "3147600": [-44.9651, -22.4103],
    "3147808": [-44.2527, -22.1804],
    "3147907": [-46.6351, -20.7574],
    "3147956": [-44.1146, -16.0681],
    "3148202": [-42.2575, -21.1661],
    "3148301": [-42.9781, -20.8589],
    "3148400": [-42.8638, -18.4448],
    "3148509": [-41.0768, -17.4687],
    "3148608": [-42.5075, -18.5525],
    "3148707": [-41.2102, -15.9657],
    "3148756": [-42.3771, -20.4695],
    "3148806": [-42.7193, -20.5953],
    "3148905": [-45.2182, -20.2846],
    "3149002": [-42.155, -20.8308],
    "3149101": [-45.4446, -22.2463],
    "3149150": [-44.3156, -15.6505],
    "3149200": [-47.5069, -19.1826],
    "3149309": [-44.0313, -19.6297],
    "3149408": [-43.7214, -21.7275],
    "3149507": [-43.1372, -21.8168],
    "3149606": [-44.6326, -19.6008],
    "3149705": [-45.0715, -19.9292],
    "3149804": [-47.1648, -19.3582],
    "3149903": [-45.0752, -21.0751],
    "3149952": [-42.2238, -19.066],
    "3150000": [-41.5567, -18.3242],
    "3150109": [-43.3101, -21.5031],
    "3150158": [-42.0427, -19.773],
    "3150208": [-42.7113, -20.2396],
    "3150307": [-44.1706, -21.5026],
    "3150406": [-44.2415, -20.4715],
    "3150505": [-45.8355, -20.5037],
    "3150539": [-42.4269, -19.7379],
    "3150604": [-44.4246, -20.5194],
    "3150703": [-48.701, -19.9411],
    "3150802": [-43.2815, -20.6318],
    "3150901": [-45.5259, -22.5527],
    "3151008": [-45.5891, -22.3543],
    "3151107": [-42.3603, -21.67],
    "3151206": [-44.8682, -17.4062],
    "3151305": [-43.0199, -21.2623],
    "3151503": [-46.0702, -20.4289],
    "3151602": [-48.6752, -20.0734],
    "3151701": [-45.9914, -21.8066],
    "3151909": [-41.5579, -19.5723],
    "3152006": [-44.908, -19.1268],
    "3152131": [-44.9452, -16.6274],
    "3152170": [-41.4553, -16.8584],
    "3152204": [-43.1101, -15.727],
    "3152303": [-43.0757, -20.6624],
    "3152402": [-41.7571, -17.8054],
    "3152501": [-45.919, -22.2506],
    "3152600": [-44.9417, -22.1798],
    "3152709": [-44.0636, -21.1081],
    "3152808": [-48.9533, -19.3393],
    "3152907": [-46.8471, -20.7873],
    "3153004": [-46.403, -19.7576],
    "3153103": [-43.1634, -20.7723],
    "3153202": [-44.0808, -18.7312],
    "3153301": [-43.5804, -18.6444],
    "3153400": [-46.3971, -18.183],
    "3153509": [-41.9417, -20.4366],
    "3153608": [-44.1137, -19.4678],
    "3153707": [-45.6078, -19.2892],
    "3154002": [-42.3993, -20.014],
    "3154101": [-42.4435, -21.5219],
    "3154150": [-41.9415, -20.2377],
    "3154200": [-44.2892, -20.8393],
    "3154309": [-41.132, -19.1989],
    "3154408": [-43.7783, -21.0904],
    "3154457": [-45.8832, -16.2342],
    "3154507": [-42.9753, -16.0657],
    "3154606": [-44.0625, -19.7769],
    "3154705": [-45.0806, -21.1553],
    "3154804": [-43.7711, -20.1112],
    "3154903": [-42.6642, -20.1267],
    "3155108": [-40.5573, -16.6873],
    "3155207": [-43.5121, -20.8593],
    "3155306": [-44.3491, -20.2634],
    "3155405": [-43.1511, -21.471],
    "3155504": [-46.2871, -19.2159],
    "3155603": [-42.5622, -15.7323],
    "3155801": [-43.1659, -21.2455],
    "3155900": [-43.8688, -22.0483],
    "3156007": [-43.0624, -18.2606],
    "3156106": [-44.3582, -20.9962],
    "3156205": [-43.035, -21.6387],
    "3156304": [-42.8417, -21.2117],
    "3156403": [-47.5703, -18.8977],
    "3156452": [-42.5128, -20.9841],
    "3156502": [-42.2228, -16.3516],
    "3156601": [-40.4908, -16.4616],
    "3156700": [-43.7838, -19.8501],
    "3156809": [-43.0598, -18.6513],
    "3156908": [-47.2265, -19.8905],
    "3157005": [-42.1695, -16.1041],
    "3157104": [-40.0413, -16.08],
    "3157203": [-43.4336, -20.0188],
    "3157278": [-43.6872, -21.974],
    "3157336": [-44.2084, -21.1193],
    "3157401": [-42.8273, -20.2382],
    "3157500": [-42.4046, -18.8614],
    "3157609": [-45.6135, -16.6964],
    "3157658": [-40.6606, -16.9007],
    "3157708": [-47.5367, -19.3593],
    "3157807": [-43.8271, -19.7307],
    "3157906": [-42.269, -20.4404],
    "3158102": [-40.1135, -16.3004],
    "3158201": [-42.335, -18.2511],
    "3158300": [-45.4944, -21.2691],
    "3158508": [-43.9395, -18.9],
    "3158607": [-43.1787, -21.9491],
    "3158805": [-45.0781, -20.8822],
    "3158904": [-41.899, -20.0466],
    "3158953": [-42.5329, -19.3864],
    "3159001": [-43.683, -19.1748],
    "3159100": [-43.658, -20.7914],
    "3159209": [-46.2731, -22.0171],
    "3159357": [-42.1331, -19.8698],
    "3159407": [-43.9488, -21.5626],
    "3159506": [-41.4052, -19.4272],
    "3159605": [-45.6841, -22.2553],
    "3159803": [-50.2795, -19.0416],
    "3160009": [-42.8111, -21.7403],
    "3160108": [-42.6251, -20.3227],
    "3160306": [-40.2794, -16.5019],
    "3160405": [-45.3037, -20.0693],
    "3160454": [-42.6586, -15.2794],
    "3160603": [-44.1682, -18.393],
    "3160702": [-43.5201, -21.458],
    "3160801": [-45.0703, -21.5616],
    "3160900": [-43.9744, -20.6145],
    "3160959": [-42.0352, -19.5232],
    "3161007": [-42.8925, -19.8913],
    "3161056": [-41.4516, -18.5621],
    "3161106": [-44.8398, -15.8659],
    "3161205": [-45.0039, -20.7135],
    "3161304": [-49.7953, -19.7679],
    "3161403": [-42.285, -20.7855],
    "3161502": [-42.8277, -20.9103],
    "3161601": [-42.3038, -18.8974],
    "3161650": [-41.3617, -18.915],
    "3161700": [-45.6053, -18.2289],
    "3161809": [-44.8471, -19.9713],
    "3161908": [-43.3225, -19.8125],
    "3162005": [-45.6108, -21.9069],
    "3162104": [-45.9596, -19.3609],
    "3162252": [-44.3335, -16.8612],
    "3162302": [-45.9288, -21.9384],
    "3162401": [-43.8265, -15.8911],
    "3162500": [-44.2568, -21.2438],
    "3162559": [-42.153, -20.3774],
    "3162575": [-41.1599, -18.7436],
    "3162609": [-42.1719, -19.3444],
    "3162708": [-41.9748, -15.3594],
    "3162906": [-43.018, -21.5798],
    "3162948": [-46.2746, -20.7505],
    "3162955": [-43.993, -19.6943],
    "3163003": [-42.105, -18.3319],
    "3163102": [-44.5437, -19.6846],
    "3163201": [-45.5246, -22.3364],
    "3163300": [-41.3663, -18.4156],
    "3163409": [-42.6952, -19.9118],
    "3163607": [-41.7604, -20.0227],
    "3163706": [-45.0367, -22.1117],
    "3163805": [-42.6986, -20.7369],
    "3163904": [-46.6469, -21.1143],
    "3164001": [-42.5659, -20.0676],
    "3164100": [-42.5951, -18.3527],
    "3164209": [-45.4534, -16.3534],
    "3164308": [-46.4686, -20.1746],
    "3164407": [-45.7945, -22.1672],
    "3164431": [-42.5996, -21.0272],
    "3164472": [-41.942, -19.5223],
    "3164506": [-42.5407, -18.0577],
    "3164605": [-45.0391, -20.2475],
    "3164704": [-47.0075, -20.9174],
    "3164803": [-43.2283, -19.2956],
    "3165008": [-44.5697, -20.9459],
    "3165107": [-47.1261, -20.7891],
    "3165206": [-44.9704, -21.7413],
    "3165305": [-44.4572, -21.6704],
    "3165404": [-45.8499, -22.7977],
    "3165503": [-42.4029, -18.7734],
    "3165537": [-44.1269, -20.0494],
    "3165552": [-42.14, -17.6198],
    "3165560": [-42.8214, -20.0835],
    "3165578": [-46.2231, -22.5579],
    "3165701": [-43.1019, -20.9017],
    "3165800": [-46.1482, -22.1515],
    "3165909": [-43.2254, -17.8906],
    "3166006": [-43.3536, -20.8007],
    "3166105": [-43.0734, -18.8996],
    "3166204": [-43.6028, -21.0286],
    "3166303": [-42.4594, -20.4983],
    "3166402": [-44.4691, -21.9172],
    "3166501": [-43.2121, -18.3831],
    "3166709": [-40.3122, -17.7482],
    "3166907": [-46.0987, -21.5545],
    "3167004": [-44.5461, -21.8217],
    "3167103": [-43.3786, -18.5189],
    "3167202": [-44.2744, -19.4488],
    "3167301": [-43.1977, -21.142],
    "3167400": [-45.7995, -22.0412],
    "3167509": [-43.2927, -21.9627],
    "3167608": [-41.965, -19.997],
    "3167707": [-42.1559, -19.2097],
    "3167806": [-45.0411, -22.0278],
    "3167905": [-43.2511, -21.3606],
    "3168002": [-42.0773, -15.8284],
    "3168051": [-41.5994, -19.7237],
    "3168200": [-46.1413, -19.8772],
    "3168309": [-43.6876, -19.6295],
    "3168408": [-41.9263, -19.2921],
    "3168507": [-42.8582, -20.6282],
    "3168606": [-41.3369, -17.6842],
    "3168705": [-42.5935, -19.5521],
    "3168804": [-44.1638, -21.1101],
    "3168903": [-45.8281, -18.8517],
    "3169000": [-43.0273, -21.1792],
    "3169059": [-46.1519, -22.3569],
    "3169109": [-46.389, -22.7024],
    "3169208": [-42.0728, -20.883],
    "3169307": [-45.2058, -21.694],
    "3169356": [-45.0416, -18.2783],
    "3169406": [-45.5086, -21.4028],
    "3169604": [-48.708, -18.5331],
    "3169703": [-42.8067, -17.2598],
    "3169802": [-45.7935, -21.8768],
    "3169901": [-42.9727, -21.1054],
    "3170008": [-44.861, -16.3833],
    "3170057": [-42.0646, -19.6609],
    "3170107": [-47.9666, -19.5545],
    "3170206": [-48.3295, -18.9961],
    "3170305": [-40.6681, -17.2792],
    "3170404": [-46.7453, -16.552],
    "3170438": [-50.3646, -19.3928],
    "3170479": [-46.3371, -16.0744],
    "3170503": [-42.7129, -20.3176],
    "3170529": [-45.6079, -16.0281],
    "3170578": [-42.3157, -19.6014],
    "3170602": [-46.3179, -20.4074],
    "3170651": [-42.2864, -15.344],
    "3170701": [-45.4196, -21.5637],
    "3170750": [-45.9074, -18.46],
    "3170800": [-44.7438, -17.424],
    "3170909": [-43.9295, -15.6473],
    "3171006": [-46.7927, -17.786],
    "3171030": [-43.6832, -15.5491],
    "3171071": [-42.733, -17.499],
    "3171105": [-48.346, -19.6017],
    "3171154": [-42.2587, -20.0232],
    "3171204": [-43.9468, -19.7263],
    "3171303": [-42.8923, -20.7374],
    "3171402": [-42.2738, -20.9012],
    "3171501": [-41.9538, -18.5962],
    "3171600": [-42.3436, -16.7224],
    "3171709": [-45.1193, -22.3529],
    "3171808": [-42.6577, -18.8055],
    "3171907": [-42.3017, -18.4467],
    "3172004": [-42.8457, -21.0031],
    "3172103": [-42.5715, -21.7565],
    "3200102": [-41.1036, -20.1005],
    "3200136": [-40.7258, -18.982],
    "3200201": [-41.5164, -20.7119],
    "3200300": [-40.8161, -20.5371],
    "3200359": [-40.9867, -19.0266],
    "3200409": [-40.7058, -20.7167],
    "3200508": [-41.5551, -21.0739],
    "3200607": [-40.2042, -19.7868],
    "3200706": [-41.1846, -20.9566],
    "3200805": [-40.9703, -19.5339],
    "3200904": [-40.8165, -18.6389],
    "3201001": [-40.3308, -18.4922],
    "3201159": [-41.303, -20.1559],
    "3201308": [-40.4656, -20.2866],
    "3201407": [-41.2081, -20.5377],
    "3201506": [-40.6566, -19.4729],
    "3201605": [-39.8507, -18.4525],
    "3201704": [-41.2636, -20.3624],
    "3201902": [-40.8433, -20.3217],
    "3202108": [-40.8411, -18.2629],
    "3202207": [-40.3425, -19.9703],
    "3202256": [-40.4797, -19.2007],
    "3202306": [-41.7076, -20.7748],
    "3202405": [-40.5146, -20.6026],
    "3202454": [-41.5623, -20.2681],
    "3202504": [-40.4165, -19.8176],
    "3202553": [-41.6704, -20.5111],
    "3202603": [-40.8613, -20.7563],
    "3202652": [-41.6579, -20.3273],
    "3202801": [-40.9712, -20.9709],
    "3202900": [-40.8993, -19.961],
    "3203056": [-39.9991, -18.9789],
    "3203106": [-41.395, -20.8127],
    "3203163": [-41.0589, -19.8635],
    "3203205": [-40.1729, -19.3401],
    "3203320": [-40.8841, -21.078],
    "3203346": [-40.7594, -20.4301],
    "3203353": [-40.5223, -19.4256],
    "3203403": [-41.38, -21.1065],
    "3203502": [-40.2777, -18.1323],
    "3203700": [-41.4289, -20.4168],
    "3203809": [-41.3146, -20.9148],
    "3203908": [-40.565, -18.6746],
    "3204005": [-40.8201, -19.144],
    "3204054": [-40.0413, -18.1894],
    "3204104": [-40.2052, -18.3595],
    "3204203": [-40.7453, -20.8375],
    "3204252": [-40.5178, -18.2531],
    "3204351": [-40.3283, -19.2105],
    "3204401": [-40.9277, -20.7869],
    "3204500": [-40.5656, -20.1204],
    "3204559": [-40.8071, -20.0865],
    "3204609": [-40.6393, -19.8979],
    "3204658": [-40.5525, -19.1267],
    "3204708": [-40.5214, -18.968],
    "3204807": [-41.6621, -20.9791],
    "3204906": [-40.0783, -18.7469],
    "3204955": [-40.6636, -19.7239],
    "3205002": [-40.3087, -20.1117],
    "3205010": [-40.151, -19.0761],
    "3205036": [-41.0041, -20.6316],
    "3205069": [-41.1326, -20.3819],
    "3205101": [-40.5249, -20.3798],
    "3205150": [-40.6625, -18.6073],
    "3205176": [-40.3508, -18.9628],
    "3205200": [-40.3152, -20.3812],
    "3300159": [-42.1301, -21.6418],
    "3300209": [-42.2871, -22.7257],
    "3300233": [-41.913, -22.7699],
    "3300308": [-43.899, -22.4185],
    "3300407": [-44.1997, -22.4929],
    "3300456": [-43.3829, -22.7231],
    "3300605": [-41.6844, -21.1167],
    "3300902": [-41.9323, -21.5088],
    "3300951": [-43.2426, -22.0364],
    "3301009": [-41.4224, -21.6717],
    "3301157": [-41.483, -21.511],
    "3301207": [-42.5521, -21.9025],
    "3301306": [-42.1385, -22.4826],
    "3301603": [-42.5232, -22.0539],
    "3301850": [-42.9727, -22.582],
    "3301876": [-42.2149, -22.8309],
    "3301900": [-42.8587, -22.7527],
    "3302056": [-41.654, -21.4125],
    "3302106": [-42.0778, -21.7454],
    "3302205": [-41.923, -21.2183],
    "3302270": [-43.6065, -22.6603],
    "3302304": [-42.1344, -21.2387],
    "3302452": [-42.2712, -22.0183],
    "3302601": [-43.9971, -22.9773],
    "3302809": [-43.7597, -22.5383],
    "3302908": [-43.4595, -22.5078],
    "3303203": [-43.4287, -22.8223],
    "3303302": [-43.0706, -22.9227],
    "3303401": [-42.5009, -22.3232],
    "3303609": [-43.7257, -22.6223],
    "3303856": [-43.413, -22.3921],
    "3303955": [-44.006, -22.5363],
    "3304003": [-43.9037, -22.6591],
    "3304128": [-44.2344, -22.3586],
    "3304508": [-43.5733, -22.1624],
    "3304524": [-41.9501, -22.4485],
    "3304557": [-43.3369, -22.9571],
    "3304607": [-41.9268, -21.9629],
    "3304706": [-42.2087, -21.5695],
    "3304755": [-41.1571, -21.3784],
    "3304805": [-41.7835, -21.6918],
    "3305109": [-43.3752, -22.7878],
    "3305133": [-41.9521, -21.3755],
    "3305208": [-42.1343, -22.8049],
    "3305604": [-42.3702, -22.5503],
    "3305752": [-42.7291, -22.786],
    "3305802": [-42.8704, -22.3014],
    "3306008": [-43.0986, -22.1194],
    "3500204": [-49.6575, -21.2831],
    "3500303": [-47.0595, -22.0332],
    "3500402": [-46.7002, -21.921],
    "3500501": [-46.6001, -22.4769],
    "3500600": [-47.8766, -22.5993],
    "3500709": [-49.1395, -22.585],
    "3501004": [-47.3848, -21.0118],
    "3501152": [-47.2774, -23.5251],
    "3501301": [-51.5057, -22.1264],
    "3501608": [-47.2873, -22.7175],
    "3501707": [-48.0338, -21.7202],
    "3501806": [-49.7553, -20.2685],
    "3501905": [-46.7965, -22.7018],
    "3502408": [-51.4217, -22.3427],
    "3502606": [-50.9189, -20.4816],
    "3502705": [-48.8275, -24.3992],
    "3502804": [-50.5763, -21.1443],
    "3502903": [-47.6703, -23.5651],
    "3503000": [-47.8285, -20.1696],
    "3503109": [-49.0675, -23.1586],
    "3503208": [-48.191, -21.8182],
    "3503307": [-47.3041, -22.3187],
    "3503505": [-44.7163, -22.6737],
    "3503703": [-48.7815, -21.179],
    "3503802": [-47.1283, -22.553],
    "3503901": [-46.3115, -23.391],
    "3504206": [-50.5934, -20.6264],
    "3504305": [-49.3085, -22.192],
    "3504503": [-48.8926, -23.0504],
    "3504602": [-49.4324, -20.9245],
    "3504800": [-49.5506, -20.703],
    "3505005": [-49.5666, -23.5718],
    "3505203": [-48.7155, -22.067],
    "3505401": [-48.4127, -24.8794],
    "3505500": [-48.6608, -20.5244],
    "3505609": [-48.0971, -21.2195],
    "3505708": [-46.8737, -23.5004],
    "3505906": [-47.5592, -20.82],
    "3506003": [-49.1231, -22.2626],
    "3506300": [-49.4845, -23.026],
    "3506359": [-46.0468, -23.7539],
    "3506508": [-50.3438, -21.2484],
    "3506706": [-48.4821, -21.9075],
    "3506805": [-48.5169, -22.0955],
    "3506904": [-48.2865, -23.1239],
    "3507001": [-47.688, -23.3071],
    "3507100": [-46.4695, -23.1649],
    "3507159": [-49.1716, -24.3148],
    "3507308": [-48.7781, -22.1695],
    "3507407": [-49.037, -21.6262],
    "3507456": [-48.9879, -22.6664],
    "3507506": [-48.5154, -22.8781],
    "3507605": [-46.5594, -22.9429],
    "3508108": [-50.1816, -21.0461],
    "3508306": [-49.3828, -22.4896],
    "3508405": [-47.079, -23.3146],
    "3508504": [-45.718, -23.1056],
    "3508603": [-45.0023, -22.6914],
    "3508702": [-46.6276, -21.5431],
    "3508801": [-49.5581, -21.7386],
    "3508900": [-51.2374, -21.9458],
    "3509007": [-46.7388, -23.3797],
    "3509106": [-51.9712, -21.7722],
    "3509205": [-46.8745, -23.3493],
    "3509304": [-48.851, -20.8816],
    "3509502": [-47.0467, -22.8987],
    "3509601": [-46.7597, -23.2177],
    "3509700": [-45.5301, -22.6978],
    "3509809": [-50.0026, -22.6097],
    "3509957": [-45.0278, -22.7439],
    "3510005": [-50.3986, -22.8088],
    "3510203": [-48.2956, -24.0479],
    "3510302": [-47.747, -23.47],
    "3510401": [-47.4833, -22.9884],
    "3510500": [-45.4624, -23.6266],
    "3510609": [-46.8435, -23.5532],
    "3510708": [-49.9572, -20.0789],
    "3510807": [-47.0833, -21.7985],
    "3511102": [-48.9515, -21.1261],
    "3511201": [-49.0542, -21.0597],
    "3511904": [-50.4594, -21.5713],
    "3512001": [-48.5818, -20.7566],
    "3512407": [-47.4035, -22.4821],
    "3512506": [-50.3056, -21.3713],
    "3512704": [-47.6017, -22.2218],
    "3512803": [-47.1759, -22.6627],
    "3512902": [-49.7512, -20.4186],
    "3513009": [-46.956, -23.6721],
    "3513405": [-45.0066, -22.5596],
    "3513504": [-46.4122, -23.8568],
    "3513702": [-47.6535, -21.8723],
    "3513801": [-46.613, -23.69],
    "3513900": [-46.6977, -21.662],
    "3514106": [-48.3405, -22.3814],
    "3514205": [-50.5258, -20.1138],
    "3514304": [-48.3499, -22.1203],
    "3514502": [-49.431, -22.3954],
    "3514601": [-47.9859, -21.2415],
    "3514809": [-48.263, -24.5187],
    "3514908": [-47.3869, -23.0538],
    "3515004": [-46.85, -23.6464],
    "3515103": [-46.8371, -23.8476],
    "3515152": [-47.1745, -22.487],
    "3515186": [-46.7841, -22.19],
    "3515194": [-49.4221, -22.6645],
    "3515202": [-50.4086, -20.2728],
    "3515301": [-51.6859, -22.4829],
    "3515350": [-52.5697, -22.5064],
    "3515400": [-49.5289, -23.4011],
    "3515707": [-46.3753, -23.5626],
    "3515905": [-50.1558, -20.6554],
    "3516002": [-51.1716, -21.5453],
    "3516101": [-50.684, -22.8748],
    "3516200": [-47.3461, -20.5777],
    "3516309": [-46.7194, -23.2685],
    "3516408": [-46.7275, -23.3074],
    "3516853": [-48.461, -21.8087],
    "3516903": [-50.4325, -20.6186],
    "3517000": [-50.0651, -21.7597],
    "3517307": [-49.8537, -21.869],
    "3517505": [-49.1742, -20.7278],
    "3517703": [-47.7962, -20.4875],
    "3517901": [-48.9871, -20.4274],
    "3518008": [-50.3522, -20.0626],
    "3518107": [-49.5768, -21.9196],
    "3518305": [-46.0658, -23.4268],
    "3518404": [-45.2103, -22.8208],
    "3518503": [-48.2308, -23.3634],
    "3518602": [-48.2034, -21.3881],
    "3518701": [-46.2277, -23.9496],
    "3518800": [-46.4483, -23.3968],
    "3519105": [-49.0535, -21.9169],
    "3519303": [-48.0277, -21.9467],
    "3519402": [-49.2153, -21.0813],
    "3519501": [-50.0823, -22.8212],
    "3519709": [-47.2155, -23.8308],
    "3519808": [-49.2166, -20.382],
    "3519907": [-51.0171, -22.673],
    "3520004": [-48.585, -22.539],
    "3520103": [-47.6925, -20.0712],
    "3520400": [-45.255, -23.8412],
    "3520426": [-47.7453, -24.8781],
    "3520608": [-51.2593, -22.1131],
    "3520707": [-50.2474, -19.9532],
    "3520905": [-49.6069, -23.0787],
    "3521002": [-47.6421, -23.3859],
    "3521200": [-48.5505, -24.4976],
    "3521309": [-48.0547, -20.3946],
    "3521408": [-47.5245, -22.6099],
    "3521804": [-49.0775, -23.5136],
    "3522109": [-46.8359, -24.0511],
    "3522208": [-46.8548, -23.7439],
    "3522505": [-46.9774, -23.5545],
    "3522604": [-46.772, -22.422],
    "3522703": [-48.8261, -21.5618],
    "3522802": [-49.4756, -23.6683],
    "3523008": [-51.4225, -20.6037],
    "3523107": [-46.3323, -23.4592],
    "3523206": [-49.3311, -24.0698],
    "3523305": [-47.1336, -24.2858],
    "3523404": [-46.8082, -23.0111],
    "3523602": [-47.8359, -22.2944],
    "3523909": [-47.2839, -23.3093],
    "3524006": [-47.0595, -23.1401],
    "3524105": [-47.8268, -20.2983],
    "3524204": [-48.4061, -20.6475],
    "3524303": [-48.2947, -21.2055],
    "3524402": [-45.9748, -23.2911],
    "3524600": [-48.0428, -24.7757],
    "3524709": [-47.0191, -22.6849],
    "3524808": [-50.561, -20.2858],
    "3524907": [-45.7148, -23.28],
    "3525003": [-46.8977, -23.5406],
    "3525201": [-46.7309, -23.0994],
    "3525300": [-48.5326, -22.2764],
    "3525508": [-46.2193, -22.9374],
    "3525706": [-49.7629, -21.0809],
    "3525904": [-46.8933, -23.1862],
    "3526001": [-51.4357, -21.4111],
    "3526100": [-47.6473, -24.1978],
    "3526209": [-47.0258, -23.9587],
    "3526605": [-44.883, -22.5217],
    "3526704": [-47.3288, -22.1739],
    "3526902": [-47.3733, -22.6117],
    "3527009": [-46.6534, -22.5096],
    "3527108": [-49.6896, -21.6614],
    "3527207": [-45.066, -22.7767],
    "3527306": [-46.9318, -23.0818],
    "3527405": [-51.0027, -21.6419],
    "3527504": [-49.545, -22.4814],
    "3527603": [-47.8146, -21.5618],
    "3528007": [-48.7063, -22.4968],
    "3528403": [-47.2368, -23.5019],
    "3528502": [-46.5604, -23.3163],
    "3528700": [-52.0573, -22.1195],
    "3528858": [-49.147, -21.2627],
    "3529302": [-48.4531, -21.5918],
    "3529401": [-46.4501, -23.6538],
    "3529500": [-49.5726, -21.1985],
    "3529609": [-50.2011, -20.4157],
    "3529807": [-48.4424, -22.4536],
    "3530102": [-51.1553, -21.1242],
    "3530409": [-49.4919, -20.6039],
    "3530607": [-46.1908, -23.5598],
    "3530706": [-47.0105, -22.2551],
    "3530805": [-46.9987, -22.4378],
    "3531308": [-48.5542, -21.2513],
    "3531407": [-49.785, -20.7422],
    "3531506": [-48.6835, -20.9108],
    "3531605": [-51.5779, -21.219],
    "3531803": [-47.2938, -22.9541],
    "3531902": [-48.1914, -20.6579],
    "3532009": [-46.7839, -22.8855],
    "3532207": [-51.5111, -22.5438],
    "3532405": [-46.3682, -23.1857],
    "3532900": [-48.5644, -21.7884],
    "3533254": [-48.9113, -20.9838],
    "3533403": [-47.2615, -22.7805],
    "3533908": [-48.9935, -20.6903],
    "3534104": [-50.0964, -22.1374],
    "3534203": [-49.3669, -20.2298],
    "3534302": [-47.8982, -20.7018],
    "3534401": [-46.7864, -23.5308],
    "3534708": [-49.8566, -22.9549],
    "3534807": [-51.7516, -21.5362],
    "3535002": [-49.5251, -20.2986],
    "3535200": [-50.7486, -20.4629],
    "3535309": [-50.2334, -22.8265],
    "3535507": [-50.6454, -22.4586],
    "3535606": [-45.6491, -23.4806],
    "3536109": [-48.3827, -23.0941],
    "3536257": [-50.0388, -20.266],
    "3536406": [-51.758, -21.1605],
    "3536703": [-48.8762, -22.3006],
    "3536802": [-46.441, -22.7696],
    "3536901": [-50.0898, -20.1919],
    "3537156": [-50.8111, -22.8054],
    "3537206": [-47.1773, -24.1779],
    "3537305": [-50.1175, -21.382],
    "3537404": [-51.0995, -20.6462],
    "3537602": [-47.0082, -24.2983],
    "3537701": [-50.6637, -21.5715],
    "3537800": [-47.4354, -23.7976],
    "3537909": [-47.7272, -23.8612],
    "3538006": [-45.4579, -22.8862],
    "3538204": [-46.5746, -22.7782],
    "3538303": [-51.7479, -21.8405],
    "3538501": [-45.1653, -22.5997],
    "3538600": [-46.2973, -23.0527],
    "3538709": [-47.7793, -22.711],
    "3538808": [-49.3904, -23.1947],
    "3538907": [-49.3836, -21.9386],
    "3539103": [-46.9746, -23.3695],
    "3539202": [-51.5902, -22.4219],
    "3539400": [-49.187, -22.4224],
    "3539509": [-48.2537, -21.0183],
    "3539806": [-46.3505, -23.5344],
    "3540101": [-49.3629, -21.7283],
    "3540200": [-48.0711, -20.9713],
    "3540408": [-50.5114, -19.9142],
    "3540606": [-47.5232, -23.223],
    "3540705": [-47.4363, -21.8375],
    "3540903": [-48.076, -21.3495],
    "3541000": [-46.4958, -24.0035],
    "3541109": [-49.4117, -22.1087],
    "3541208": [-51.6324, -22.1239],
    "3541406": [-51.328, -21.9654],
    "3541505": [-51.8407, -21.7708],
    "3541604": [-49.8807, -21.5126],
    "3541703": [-50.6524, -22.2107],
    "3541802": [-50.2463, -21.7911],
    "3541901": [-44.78, -22.5148],
    "3542206": [-50.9149, -22.3379],
    "3542503": [-49.1856, -21.8906],
    "3542602": [-47.8639, -24.5191],
    "3542701": [-47.5195, -20.6755],
    "3542800": [-49.0278, -24.6186],
    "3542909": [-48.184, -22.0425],
    "3543006": [-48.7852, -24.2559],
    "3543253": [-48.3571, -24.1918],
    "3543303": [-46.3973, -23.7046],
    "3543402": [-47.8213, -21.2066],
    "3543808": [-50.7309, -21.6609],
    "3543907": [-47.6106, -22.402],
    "3544202": [-49.7246, -20.0464],
    "3544251": [-52.8487, -22.5043],
    "3544509": [-51.006, -20.2665],
    "3545159": [-47.7302, -22.8618],
    "3545209": [-47.3056, -23.17],
    "3545308": [-47.6028, -23.655],
    "3545407": [-49.9675, -22.8729],
    "3545605": [-48.8205, -21.3135],
    "3546108": [-50.9082, -20.0761],
    "3546306": [-47.2467, -21.8807],
    "3546405": [-49.5871, -22.7871],
    "3546504": [-48.3759, -21.4548],
    "3546603": [-50.9456, -20.251],
    "3546702": [-47.5285, -22.4715],
    "3546801": [-46.2362, -23.2947],
    "3546900": [-48.0751, -21.661],
    "3547007": [-48.1552, -22.5376],
    "3547205": [-50.7952, -20.2443],
    "3547304": [-46.9224, -23.449],
    "3547403": [-50.8135, -20.0917],
    "3547502": [-47.4952, -21.6963],
    "3547700": [-51.7074, -22.0356],
    "3547809": [-46.4399, -23.7297],
    "3548005": [-46.9443, -22.6078],
    "3548054": [-50.5256, -20.8604],
    "3548104": [-46.6804, -22.1303],
    "3548401": [-50.5176, -21.67],
    "3548609": [-45.6802, -22.6621],
    "3548906": [-47.8762, -21.9019],
    "3549102": [-46.7986, -21.97],
    "3549201": [-50.3891, -20.4198],
    "3549409": [-47.9294, -20.5346],
    "3549607": [-44.5859, -22.7361],
    "3549805": [-49.3651, -20.7979],
    "3549904": [-45.915, -23.0913],
    "3549953": [-46.932, -23.8436],
    "3550001": [-45.2303, -23.2585],
    "3550100": [-48.566, -22.6826],
    "3550308": [-46.631, -23.6305],
    "3550407": [-47.9027, -22.5621],
    "3550506": [-49.7494, -22.7045],
    "3550605": [-47.1194, -23.5414],
    "3550704": [-45.6077, -23.7659],
    "3550803": [-46.7548, -21.7496],
    "3550902": [-47.5721, -21.4529],
    "3551009": [-46.4955, -23.9644],
    "3551207": [-49.482, -23.2574],
    "3551405": [-47.5373, -21.2846],
    "3551504": [-47.6149, -21.2104],
    "3551603": [-46.6877, -22.5811],
    "3551702": [-48.0129, -21.1206],
    "3551900": [-48.7891, -20.7876],
    "3552007": [-44.8451, -22.74],
    "3552106": [-46.5155, -22.6067],
    "3552205": [-47.4478, -23.4705],
    "3552403": [-47.2497, -22.8376],
    "3552502": [-46.3075, -23.6016],
    "3552551": [-51.0752, -20.5038],
    "3552700": [-48.6385, -21.727],
    "3552809": [-46.7902, -23.6153],
    "3552908": [-51.3388, -22.5185],
    "3553104": [-48.538, -21.1256],
    "3553302": [-47.2283, -21.5892],
    "3553401": [-49.6345, -20.5032],
    "3553500": [-47.6352, -24.0089],
    "3553609": [-46.7367, -21.448],
    "3553708": [-48.5475, -21.4272],
    "3553807": [-49.224, -23.5258],
    "3553906": [-51.6241, -22.3513],
    "3553955": [-50.5881, -22.7609],
    "3554003": [-47.847, -23.3465],
    "3554102": [-45.5041, -23.0734],
    "3554300": [-52.3853, -22.4231],
    "3554409": [-48.3613, -20.7685],
    "3554508": [-47.712, -23.0476],
    "3554706": [-48.1541, -22.4747],
    "3554805": [-45.5997, -22.9356],
    "3555000": [-50.5185, -21.9432],
    "3555109": [-51.5753, -21.3797],
    "3555406": [-45.0301, -23.4314],
    "3555604": [-49.1465, -20.9291],
    "3556008": [-49.2822, -21.197],
    "3556107": [-50.1207, -20.4291],
    "3556206": [-46.9783, -22.974],
    "3556305": [-50.9377, -21.2092],
    "3556404": [-46.8842, -21.8638],
    "3556453": [-47.0155, -23.6203],
    "3556800": [-48.3061, -20.8752],
    "3557006": [-47.4121, -23.5775],
    "4100301": [-49.3131, -26.0425],
    "4100400": [-49.332, -25.2963],
    "4100509": [-53.9336, -23.8966],
    "4100608": [-52.3175, -23.0557],
    "4100707": [-53.3684, -24.1286],
    "4100806": [-51.2794, -22.8132],
    "4100905": [-52.8445, -23.1471],
    "4101002": [-53.5011, -25.9135],
    "4101101": [-50.2699, -23.0394],
    "4101150": [-51.9368, -23.2057],
    "4101309": [-50.1344, -25.9589],
    "4101408": [-51.4334, -23.5674],
    "4101507": [-51.45, -23.4403],
    "4101606": [-50.0271, -24.0605],
    "4101705": [-52.5858, -23.9577],
    "4101903": [-50.8809, -23.4124],
    "4102000": [-53.5425, -24.4143],
    "4102109": [-51.7154, -23.2603],
    "4102208": [-52.046, -23.1271],
    "4102406": [-50.3446, -23.1463],
    "4102505": [-52.0612, -24.0899],
    "4102703": [-50.1613, -23.1118],
    "4102752": [-53.6333, -25.8751],
    "4102802": [-51.272, -22.9807],
    "4103008": [-52.7391, -24.2447],
    "4103024": [-53.232, -25.6341],
    "4103040": [-51.5519, -24.8361],
    "4103057": [-53.4204, -25.4348],
    "4103107": [-48.8852, -25.1113],
    "4103206": [-51.8276, -23.6955],
    "4103222": [-52.846, -26.077],
    "4103305": [-51.6045, -23.9352],
    "4103354": [-53.0795, -24.7843],
    "4103370": [-53.5649, -24.199],
    "4103453": [-53.352, -24.6904],
    "4103479": [-53.5821, -23.9361],
    "4103503": [-51.3334, -23.6628],
    "4103701": [-51.298, -23.1827],
    "4103909": [-52.7988, -24.611],
    "4103958": [-51.7857, -25.087],
    "4104006": [-48.8225, -25.1602],
    "4104204": [-49.6042, -25.2728],
    "4104253": [-49.4764, -25.2693],
    "4104402": [-51.2596, -24.671],
    "4104428": [-52.041, -25.5286],
    "4104501": [-53.7775, -25.6192],
    "4104600": [-53.6066, -25.4548],
    "4104709": [-49.7033, -23.4573],
    "4104808": [-53.3885, -25.0463],
    "4105003": [-53.1614, -25.2634],
    "4105102": [-51.5464, -22.7984],
    "4105409": [-52.4585, -25.8018],
    "4105508": [-52.5788, -23.6899],
    "4105607": [-52.9732, -23.3648],
    "4105805": [-49.175, -25.293],
    "4105904": [-51.9834, -22.8441],
    "4106001": [-50.4908, -23.6236],
    "4106209": [-49.5023, -25.7082],
    "4106308": [-53.236, -24.7254],
    "4106407": [-50.6271, -23.2332],
    "4106456": [-51.9736, -26.1937],
    "4106506": [-52.6138, -25.9965],
    "4106555": [-52.1397, -24.1293],
    "4106571": [-53.1279, -25.5986],
    "4106605": [-53.0777, -23.8058],
    "4106704": [-52.1555, -22.9786],
    "4106803": [-51.2354, -25.9189],
    "4106902": [-49.2978, -25.5029],
    "4107108": [-52.8742, -22.6404],
    "4107157": [-54.1073, -24.9509],
    "4107207": [-53.0833, -25.7466],
    "4107256": [-53.2726, -23.3272],
    "4107306": [-52.2218, -23.5601],
    "4107405": [-53.1554, -25.8809],
    "4107504": [-52.2241, -23.7595],
    "4107520": [-53.7918, -23.711],
    "4107538": [-54.2118, -24.7054],
    "4107603": [-51.3035, -24.022],
    "4107652": [-49.3076, -25.6712],
    "4107702": [-52.0306, -23.9033],
    "4107736": [-50.5124, -25.4712],
    "4107850": [-53.31, -26.225],
    "4107900": [-52.0814, -23.6245],
    "4108106": [-51.9734, -23.1149],
    "4108205": [-53.3304, -24.2971],
    "4108304": [-54.4564, -25.4182],
    "4108320": [-53.8801, -24.0775],
    "4108403": [-53.1076, -26.0421],
    "4108452": [-52.0711, -25.6987],
    "4108551": [-51.9207, -24.1562],
    "4108601": [-53.072, -24.1913],
    "4108650": [-52.0161, -25.1112],
    "4108700": [-51.4447, -24.1812],
    "4108809": [-54.2144, -24.2268],
    "4108908": [-52.7428, -22.9124],
    "4108957": [-50.8492, -25.1683],
    "4109005": [-50.0975, -23.4634],
    "4109203": [-51.7004, -22.9658],
    "4109302": [-52.8792, -25.0658],
    "4109401": [-51.4932, -25.4079],
    "4109609": [-48.791, -25.8197],
    "4109708": [-50.3269, -23.7886],
    "4109807": [-51.0335, -23.2184],
    "4109906": [-53.5933, -23.3599],
    "4110003": [-51.8562, -23.2285],
    "4110201": [-51.2142, -25.6317],
    "4110300": [-52.2402, -22.7039],
    "4110508": [-50.5495, -25.0024],
    "4110706": [-50.8841, -25.5005],
    "4110805": [-52.1019, -24.3427],
    "4110904": [-51.9787, -22.648],
    "4111001": [-50.4249, -22.9842],
    "4111100": [-52.0143, -23.6866],
    "4111209": [-52.8291, -25.977],
    "4111258": [-49.4988, -25.1265],
    "4111407": [-50.8651, -24.9893],
    "4111506": [-51.6353, -24.2862],
    "4111555": [-53.4268, -23.3408],
    "4111605": [-52.189, -23.5984],
    "4111704": [-50.0735, -23.6956],
    "4111803": [-49.9533, -23.1829],
    "4112009": [-49.699, -24.3238],
    "4112108": [-51.688, -23.6271],
    "4112207": [-52.8093, -24.0928],
    "4112405": [-52.5566, -23.4177],
    "4112702": [-50.9166, -23.2652],
    "4112801": [-49.9011, -23.418],
    "4112959": [-52.8261, -24.4157],
    "4113106": [-51.6833, -23.865],
    "4113205": [-49.8926, -25.7824],
    "4113254": [-52.4962, -24.8958],
    "4113304": [-52.3696, -25.3152],
    "4113403": [-50.7174, -23.0264],
    "4113429": [-51.6488, -24.0712],
    "4113452": [-53.5712, -25.2619],
    "4113502": [-53.039, -22.9452],
    "4113601": [-52.0084, -22.9623],
    "4113700": [-51.1178, -23.5399],
    "4113734": [-52.2745, -24.3384],
    "4113809": [-51.6716, -22.7371],
    "4113908": [-50.8339, -25.8914],
    "4114005": [-52.6229, -24.4064],
    "4114104": [-52.0398, -23.2719],
    "4114203": [-51.7161, -23.4773],
    "4114302": [-49.3273, -25.8465],
    "4114401": [-52.2111, -26.0297],
    "4114500": [-51.6525, -24.5076],
    "4114609": [-54.1126, -24.5617],
    "4114708": [-53.2322, -23.5798],
    "4114807": [-51.8959, -23.5311],
    "4114906": [-51.2932, -23.7445],
    "4115002": [-53.0654, -22.7139],
    "4115101": [-53.2356, -24.0633],
    "4115200": [-51.9521, -23.3939],
    "4115309": [-52.5844, -26.3188],
    "4115358": [-53.8159, -24.4624],
    "4115408": [-53.1, -26.2322],
    "4115457": [-52.2541, -25.1025],
    "4115507": [-51.6663, -23.7695],
    "4115606": [-53.8858, -25.3682],
    "4115705": [-48.5685, -25.7661],
    "4115739": [-52.2356, -24.7082],
    "4115754": [-51.172, -23.9103],
    "4115804": [-54.1155, -25.2495],
    "4115853": [-54.1598, -24.4218],
    "4115903": [-52.7468, -23.2062],
    "4116059": [-54.2547, -25.1119],
    "4116109": [-52.971, -24.0128],
    "4116307": [-51.7417, -23.1212],
    "4116406": [-51.7949, -22.9226],
    "4116505": [-52.6235, -23.1789],
    "4116703": [-53.2983, -24.5117],
    "4116802": [-52.5776, -24.6469],
    "4116901": [-52.2454, -23.1789],
    "4117008": [-50.5417, -23.4038],
    "4117057": [-52.5856, -25.1992],
    "4117107": [-52.9573, -22.7603],
    "4117297": [-51.5346, -23.7745],
    "4117404": [-52.2404, -23.4788],
    "4117453": [-53.9476, -24.7912],
    "4117503": [-52.1386, -23.4678],
    "4118006": [-52.6339, -23.2658],
    "4118105": [-52.129, -22.8416],
    "4118204": [-48.5356, -25.555],
    "4118402": [-52.5246, -22.9352],
    "4118451": [-54.2312, -24.6408],
    "4118501": [-52.6637, -26.1678],
    "4118709": [-50.7419, -26.043],
    "4118808": [-52.2819, -23.9554],
    "4118857": [-53.337, -23.9598],
    "4118907": [-53.713, -23.8185],
    "4119004": [-53.7553, -25.8458],
    "4119103": [-49.4521, -26.0854],
    "4119152": [-49.1446, -25.4213],
    "4119202": [-50.0499, -23.9143],
    "4119301": [-51.6564, -25.7626],
    "4119400": [-49.9159, -24.4762],
    "4119608": [-51.8045, -24.6537],
    "4119657": [-51.5645, -23.2034],
    "4119707": [-52.9326, -23.1075],
    "4119806": [-53.7179, -25.7319],
    "4119905": [-50.0627, -25.132],
    "4120101": [-49.8957, -25.5479],
    "4120150": [-52.3854, -25.5819],
    "4120200": [-53.3156, -22.848],
    "4120333": [-51.3739, -23.0224],
    "4120358": [-53.6993, -25.9648],
    "4120507": [-51.07, -22.8754],
    "4120606": [-51.1148, -25.1667],
    "4120705": [-49.9064, -23.5546],
    "4120804": [-48.9842, -25.3779],
    "4120903": [-52.9743, -25.4347],
    "4121000": [-53.5463, -23.0901],
    "4121109": [-52.1583, -23.807],
    "4121208": [-49.4934, -25.8962],
    "4121257": [-54.0555, -25.0764],
    "4121307": [-50.9208, -23.0713],
    "4121356": [-52.975, -24.2941],
    "4121505": [-50.6387, -25.6833],
    "4121752": [-51.957, -25.8553],
    "4121802": [-49.7967, -23.2904],
    "4122008": [-50.7463, -25.7287],
    "4122107": [-51.4445, -23.7964],
    "4122156": [-52.6334, -25.5359],
    "4122305": [-49.7169, -26.0871],
    "4122404": [-51.4114, -23.2761],
    "4122503": [-52.2159, -24.558],
    "4122602": [-52.8016, -23.4485],
    "4122701": [-51.5979, -23.3673],
    "4122800": [-53.4339, -26.1209],
    "4122909": [-49.6825, -23.6189],
    "4123006": [-53.3007, -25.7724],
    "4123105": [-50.4165, -23.2592],
    "4123204": [-50.8206, -23.5454],
    "4123501": [-54.2809, -24.9046],
    "4123600": [-51.9073, -22.6978],
    "4123808": [-53.4109, -25.7688],
    "4123824": [-53.5425, -25.3956],
    "4123857": [-51.9737, -24.9109],
    "4123907": [-50.5287, -23.0475],
    "4123956": [-53.1185, -23.1634],
    "4124004": [-49.6218, -23.7425],
    "4124020": [-53.6091, -25.0442],
    "4124053": [-54.4216, -25.4175],
    "4124103": [-50.0915, -23.2649],
    "4124202": [-52.3152, -22.6925],
    "4124301": [-50.6232, -23.5575],
    "4124400": [-53.5957, -26.0451],
    "4124509": [-51.8014, -22.7187],
    "4124608": [-52.4991, -23.3479],
    "4124806": [-52.8012, -25.7708],
    "4124905": [-52.2985, -22.8289],
    "4125001": [-51.8744, -23.9796],
    "4125100": [-50.2641, -25.6926],
    "4125209": [-52.9514, -25.6613],
    "4125308": [-52.3016, -23.4481],
    "4125357": [-53.9123, -23.7458],
    "4125407": [-49.6494, -23.9615],
    "4125456": [-54.1176, -24.8326],
    "4125506": [-49.0624, -25.6894],
    "4125555": [-52.5993, -23.3834],
    "4125605": [-50.4702, -25.9241],
    "4125704": [-54.2367, -25.3722],
    "4125753": [-53.8816, -24.908],
    "4125902": [-53.1829, -22.794],
    "4126009": [-50.7123, -23.46],
    "4126108": [-52.5243, -23.5268],
    "4126207": [-50.5948, -23.883],
    "4126256": [-51.8708, -23.4742],
    "4126272": [-52.6132, -25.7105],
    "4126306": [-49.482, -24.1793],
    "4126355": [-54.0245, -25.4754],
    "4126405": [-50.8954, -22.9509],
    "4126504": [-51.0436, -23.0488],
    "4126603": [-49.7789, -23.6278],
    "4126652": [-52.694, -25.6793],
    "4126702": [-52.4771, -23.202],
    "4126801": [-52.906, -23.6284],
    "4126900": [-53.1428, -23.2992],
    "4127007": [-50.4282, -25.283],
    "4127205": [-52.3485, -23.6818],
    "4127304": [-52.6739, -22.7351],
    "4127502": [-50.5205, -24.6596],
    "4127601": [-49.117, -25.8984],
    "4127700": [-53.8128, -24.7363],
    "4127809": [-49.9818, -23.6902],
    "4127858": [-53.2299, -25.4224],
    "4127882": [-48.8941, -24.9676],
    "4127908": [-52.8496, -23.9296],
    "4127957": [-53.4906, -24.6308],
    "4127965": [-51.4888, -24.9689],
    "4128104": [-53.4002, -23.6895],
    "4128203": [-51.1161, -26.0998],
    "4128302": [-52.0935, -23.0582],
    "4128500": [-49.792, -23.8757],
    "4128559": [-53.9217, -24.9607],
    "4128658": [-52.244, -25.4359],
    "4128807": [-53.6086, -23.7362],
    "4200101": [-52.2335, -26.584],
    "4200200": [-49.8254, -27.4624],
    "4200309": [-49.7209, -27.3108],
    "4200507": [-52.9638, -27.065],
    "4200556": [-52.8539, -26.8553],
    "4200606": [-48.9239, -27.7399],
    "4200705": [-49.3513, -27.6957],
    "4200804": [-53.3489, -26.5213],
    "4200903": [-49.0757, -27.538],
    "4201000": [-51.0758, -27.7256],
    "4201109": [-49.1264, -27.8716],
    "4201208": [-48.8281, -27.4983],
    "4201257": [-49.3725, -27.107],
    "4201273": [-52.1733, -27.1451],
    "4201307": [-48.7822, -26.4599],
    "4201406": [-49.4835, -28.9356],
    "4201505": [-49.0057, -28.2337],
    "4201604": [-51.3436, -26.9128],
    "4201653": [-52.4443, -27.0649],
    "4201703": [-49.3855, -26.9788],
    "4201802": [-49.7496, -27.4394],
    "4201901": [-49.5941, -27.3283],
    "4201950": [-49.471, -29.0053],
    "4202008": [-48.6183, -27.0013],
    "4202057": [-48.6532, -26.4326],
    "4202099": [-53.4087, -26.675],
    "4202107": [-48.7388, -26.646],
    "4202131": [-50.4859, -26.4427],
    "4202156": [-53.6098, -26.8511],
    "4202206": [-49.4325, -26.7842],
    "4202404": [-49.0984, -26.8636],
    "4202453": [-48.5135, -27.1769],
    "4202503": [-49.6495, -28.3616],
    "4202578": [-53.095, -26.6851],
    "4202602": [-49.6101, -27.7686],
    "4202701": [-49.13, -27.2173],
    "4202800": [-49.1524, -28.2443],
    "4202859": [-49.9117, -27.3768],
    "4202875": [-50.8336, -27.3453],
    "4202909": [-48.9205, -27.1176],
    "4203006": [-51.1181, -26.7696],
    "4203154": [-51.0377, -26.6367],
    "4203204": [-48.7089, -27.0706],
    "4203501": [-53.1562, -26.4603],
    "4203600": [-51.2779, -27.3768],
    "4203709": [-48.7901, -27.241],
    "4203808": [-50.4946, -26.2559],
    "4203907": [-51.6383, -27.4112],
    "4203956": [-48.9444, -28.4634],
    "4204004": [-51.7217, -27.0555],
    "4204103": [-52.9191, -27.1393],
    "4204152": [-51.3204, -27.6463],
    "4204178": [-50.9425, -27.7677],
    "4204194": [-49.5612, -27.5936],
    "4204202": [-52.6609, -27.1161],
    "4204251": [-49.3209, -28.5967],
    "4204301": [-52.013, -27.2353],
    "4204350": [-52.6464, -26.973],
    "4204400": [-52.761, -26.8852],
    "4204509": [-49.3209, -26.4208],
    "4204558": [-50.392, -27.5779],
    "4204707": [-53.2111, -26.8678],
    "4204756": [-53.1055, -26.9764],
    "4204806": [-50.6152, -27.277],
    "4204905": [-53.4848, -26.8582],
    "4205001": [-53.5335, -26.3413],
    "4205100": [-49.7792, -26.9843],
    "4205159": [-49.5501, -26.7172],
    "4205175": [-52.5928, -26.728],
    "4205191": [-49.6519, -28.9804],
    "4205209": [-51.4185, -27.2978],
    "4205308": [-52.257, -26.8463],
    "4205407": [-48.4921, -27.6138],
    "4205431": [-52.7933, -26.6387],
    "4205456": [-49.4807, -28.7876],
    "4205506": [-50.8555, -27.0509],
    "4205605": [-52.6745, -26.4543],
    "4205704": [-48.6566, -28.0412],
    "4205803": [-48.8493, -26.0508],
    "4205902": [-48.9817, -26.9183],
    "4206009": [-48.5706, -27.3616],
    "4206108": [-49.3193, -28.1406],
    "4206207": [-49.0249, -28.3497],
    "4206306": [-49.0165, -27.107],
    "4206405": [-53.6126, -26.575],
    "4206504": [-48.9347, -26.4838],
    "4206603": [-53.4716, -26.4012],
    "4206702": [-51.4229, -27.1964],
    "4206751": [-51.2096, -27.2053],
    "4206801": [-51.3712, -27.1017],
    "4206900": [-49.5252, -27.0436],
    "4207007": [-49.252, -28.743],
    "4207106": [-48.8594, -26.8729],
    "4207205": [-48.8307, -28.193],
    "4207304": [-48.7039, -28.1701],
    "4207403": [-49.3949, -27.5085],
    "4207502": [-49.2227, -26.9931],
    "4207577": [-51.2595, -26.985],
    "4207601": [-51.7952, -27.3748],
    "4207650": [-53.4963, -27.0034],
    "4207700": [-52.135, -27.0476],
    "4207759": [-53.3164, -26.8518],
    "4207809": [-51.9116, -27.0312],
    "4207858": [-52.8973, -26.6249],
    "4207908": [-50.7777, -26.3316],
    "4208005": [-52.3097, -27.2346],
    "4208104": [-49.8846, -26.4835],
    "4208203": [-48.7486, -26.9583],
    "4208302": [-48.6229, -27.1035],
    "4208401": [-53.709, -27.1096],
    "4208450": [-48.6612, -26.0399],
    "4208500": [-49.513, -27.4719],
    "4208708": [-49.8672, -28.9946],
    "4208807": [-49.0713, -28.6555],
    "4208955": [-52.8847, -26.7203],
    "4209003": [-51.5867, -27.1519],
    "4209102": [-48.9957, -26.2728],
    "4209151": [-49.6546, -26.8455],
    "4209177": [-52.7292, -26.4049],
    "4209201": [-51.583, -27.2517],
    "4209300": [-50.3531, -27.96],
    "4209409": [-48.8387, -28.4774],
    "4209458": [-52.5475, -26.8541],
    "4209508": [-49.7369, -27.2045],
    "4209607": [-49.4544, -28.3781],
    "4209706": [-50.6937, -26.9033],
    "4209805": [-49.2525, -27.4868],
    "4209854": [-52.0484, -27.0309],
    "4209904": [-49.5083, -27.1742],
    "4210001": [-48.8949, -26.7318],
    "4210035": [-51.4936, -27.0808],
    "4210050": [-51.3453, -26.8102],
    "4210100": [-49.8779, -26.215],
    "4210209": [-49.0526, -27.431],
    "4210407": [-49.463, -28.8556],
    "4210506": [-53.1918, -26.7599],
    "4210555": [-52.6204, -26.8012],
    "4210605": [-48.9998, -26.6258],
    "4210803": [-49.5952, -28.8479],
    "4210902": [-53.0498, -26.7666],
    "4211009": [-53.4454, -27.0969],
    "4211058": [-50.9085, -27.2042],
    "4211108": [-50.2765, -26.6207],
    "4211207": [-49.262, -28.6381],
    "4211256": [-49.7477, -28.7069],
    "4211306": [-48.7296, -26.826],
    "4211405": [-52.8985, -26.9094],
    "4211454": [-52.8368, -26.9395],
    "4211504": [-49.0392, -27.3006],
    "4211603": [-49.5796, -28.6966],
    "4211702": [-49.3773, -28.295],
    "4211751": [-49.9763, -27.5073],
    "4211801": [-51.6809, -27.2929],
    "4211900": [-48.6583, -27.7878],
    "4212007": [-53.3322, -26.3981],
    "4212056": [-50.1576, -27.5577],
    "4212106": [-53.1839, -27.0806],
    "4212205": [-50.1746, -26.4912],
    "4212254": [-49.7298, -29.277],
    "4212304": [-48.752, -27.947],
    "4212403": [-49.211, -28.4738],
    "4212502": [-48.6434, -26.8074],
    "4212601": [-51.8858, -27.3529],
    "4212700": [-49.6831, -27.5528],
    "4212809": [-48.7467, -26.751],
    "4212908": [-52.9746, -26.8227],
    "4213005": [-51.2311, -27.0522],
    "4213104": [-51.7835, -27.4646],
    "4213153": [-52.8664, -27.0477],
    "4213203": [-49.1714, -26.723],
    "4213302": [-50.2905, -27.4231],
    "4213401": [-51.9132, -26.8598],
    "4213500": [-48.6036, -27.165],
    "4213708": [-49.9921, -27.3118],
    "4213807": [-50.0347, -29.2029],
    "4214003": [-49.7063, -27.046],
    "4214102": [-49.3177, -27.2535],
    "4214201": [-52.7038, -26.7263],
    "4214300": [-49.0807, -27.6828],
    "4214409": [-51.0428, -26.9014],
    "4214508": [-50.0998, -26.8824],
    "4214607": [-49.8306, -27.1534],
    "4214706": [-49.3672, -26.6225],
    "4214805": [-49.6321, -27.1973],
    "4214904": [-49.1963, -28.09],
    "4215000": [-49.5985, -26.3946],
    "4215059": [-49.7497, -27.9029],
    "4215075": [-53.3407, -26.9853],
    "4215109": [-49.3497, -26.8845],
    "4215406": [-51.4109, -26.8999],
    "4215455": [-49.1209, -28.66],
    "4215554": [-53.6036, -26.9233],
    "4215653": [-49.7328, -29.1104],
    "4215679": [-50.0156, -26.6594],
    "4215687": [-53.1821, -26.5922],
    "4215695": [-52.6832, -26.6299],
    "4215703": [-48.8059, -27.7557],
    "4215802": [-49.3834, -26.3048],
    "4216008": [-53.0313, -27.0328],
    "4216057": [-50.3635, -27.2748],
    "4216107": [-52.5661, -26.5678],
    "4216206": [-48.6334, -26.259],
    "4216305": [-48.8678, -27.3209],
    "4216354": [-48.7996, -26.5974],
    "4216404": [-49.8423, -29.211],
    "4216503": [-50.0118, -28.3027],
    "4216602": [-48.6632, -27.5745],
    "4216701": [-53.5556, -26.485],
    "4216909": [-52.8546, -26.4653],
    "4217006": [-49.171, -28.3463],
    "4217105": [-48.9779, -28.113],
    "4217154": [-53.2536, -26.6848],
    "4217204": [-53.5092, -26.7279],
    "4217253": [-48.8422, -27.5787],
    "4217402": [-49.0514, -26.3565],
    "4217501": [-52.3453, -27.1478],
    "4217550": [-53.0144, -26.6989],
    "4217709": [-49.6679, -29.0788],
    "4217808": [-50.0966, -27.0879],
    "4217907": [-51.1363, -27.137],
    "4218004": [-48.7154, -27.2412],
    "4218103": [-49.8955, -28.7909],
    "4218202": [-49.2738, -26.8043],
    "4218251": [-50.6275, -26.6353],
    "4218301": [-50.2737, -26.164],
    "4218509": [-51.4309, -26.9597],
    "4218608": [-49.8069, -27.3061],
    "4218707": [-49.0387, -28.4759],
    "4218756": [-53.651, -26.9954],
    "4218806": [-49.6869, -28.9091],
    "4218855": [-52.8728, -26.7919],
    "4218905": [-49.5633, -28.0487],
    "4218954": [-49.926, -28.048],
    "4219002": [-49.3125, -28.4758],
    "4219150": [-50.9395, -27.4627],
    "4219176": [-51.7511, -26.9243],
    "4219200": [-49.3409, -27.3903],
    "4219309": [-51.1313, -27.0076],
    "4219358": [-49.8515, -26.8347],
    "4219408": [-49.839, -26.9394],
    "4219507": [-52.4135, -26.8633],
    "4219705": [-52.5202, -26.9839],
    "4219853": [-51.5414, -27.4824],
    "4300034": [-54.0738, -31.6953],
    "4300059": [-52.0617, -28.2055],
    "4300109": [-53.2293, -29.6318],
    "4300208": [-53.7364, -28.219],
    "4300406": [-55.9191, -29.7203],
    "4300455": [-54.064, -27.8001],
    "4300570": [-51.296, -29.3609],
    "4300604": [-51.0366, -29.9887],
    "4300638": [-52.3001, -30.8125],
    "4300646": [-53.194, -27.3591],
    "4300703": [-51.9809, -28.9668],
    "4300802": [-51.3117, -28.8815],
    "4300851": [-51.5671, -30.9099],
    "4300877": [-50.9348, -29.6417],
    "4300901": [-52.2993, -27.3805],
    "4301008": [-51.9591, -29.3609],
    "4301057": [-49.8823, -29.5001],
    "4301073": [-52.3954, -31.4346],
    "4301107": [-51.7195, -30.1926],
    "4301206": [-53.0665, -29.2655],
    "4301305": [-52.8608, -32.1867],
    "4301404": [-52.2011, -28.8666],
    "4301503": [-54.0039, -28.5292],
    "4301552": [-52.0669, -27.6956],
    "4301602": [-53.9032, -31.1945],
    "4301636": [-50.2939, -30.2161],
    "4301651": [-51.5046, -29.3922],
    "4301701": [-52.4351, -27.5494],
    "4301750": [-51.7913, -30.3895],
    "4301800": [-51.4405, -27.7295],
    "4301859": [-53.7625, -27.2141],
    "4301875": [-57.3083, -30.1671],
    "4301909": [-51.3595, -30.3569],
    "4301925": [-52.4089, -27.3992],
    "4301958": [-53.0301, -27.917],
    "4302006": [-52.5786, -29.1325],
    "4302055": [-52.6613, -27.4811],
    "4302105": [-51.5593, -29.0962],
    "4302204": [-54.1126, -27.6791],
    "4302220": [-53.8477, -28.6544],
    "4302253": [-51.6791, -29.3435],
    "4302352": [-51.3603, -29.4709],
    "4302378": [-53.8323, -27.5412],
    "4302402": [-51.912, -29.6248],
    "4302451": [-52.4064, -29.3064],
    "4302501": [-54.9462, -28.6721],
    "4302584": [-53.7507, -28.3536],
    "4302659": [-51.6169, -29.5394],
    "4302709": [-52.0006, -30.1663],
    "4302808": [-53.4766, -30.6214],
    "4302907": [-54.8013, -29.91],
    "4303004": [-52.9783, -30.2443],
    "4303103": [-51.0955, -29.9203],
    "4303202": [-51.6932, -27.7953],
    "4303301": [-54.6443, -28.3307],
    "4303400": [-53.4722, -27.2427],
    "4303509": [-51.8529, -30.9143],
    "4303558": [-52.2168, -28.6114],
    "4303608": [-50.1252, -29.0821],
    "4303707": [-54.8413, -27.9746],
    "4303806": [-52.6538, -27.7316],
    "4303905": [-51.0452, -29.6696],
    "4304002": [-53.8161, -27.6783],
    "4304200": [-52.8044, -29.7048],
    "4304309": [-54.7394, -27.9062],
    "4304358": [-53.7575, -31.5976],
    "4304408": [-50.7787, -29.3517],
    "4304507": [-52.6606, -31.2013],
    "4304606": [-51.1851, -29.9152],
    "4304622": [-51.3879, -28.1579],
    "4304630": [-50.0055, -29.6718],
    "4304655": [-54.5996, -28.9032],
    "4304663": [-52.543, -31.8428],
    "4304671": [-50.4915, -30.1528],
    "4304689": [-51.3744, -29.7034],
    "4304697": [-51.9795, -29.2892],
    "4304705": [-52.8712, -28.2617],
    "4304713": [-50.3548, -29.7657],
    "4304804": [-51.5075, -29.3182],
    "4304853": [-51.911, -27.7047],
    "4304903": [-51.9456, -28.5793],
    "4304952": [-51.7635, -28.2383],
    "4305009": [-54.0589, -28.1995],
    "4305108": [-50.9935, -29.0721],
    "4305116": [-52.004, -27.7867],
    "4305124": [-52.785, -31.7374],
    "4305132": [-52.9991, -29.6288],
    "4305157": [-53.1602, -27.6238],
    "4305173": [-51.7503, -30.5848],
    "4305207": [-54.7344, -28.1372],
    "4305306": [-53.0952, -28.0889],
    "4305355": [-51.5624, -30.0062],
    "4305371": [-51.9917, -27.9452],
    "4305405": [-53.914, -27.9703],
    "4305439": [-53.4104, -33.645],
    "4305447": [-51.996, -30.7829],
    "4305454": [-50.2681, -30.1011],
    "4305504": [-51.9112, -28.3737],
    "4305587": [-51.875, -29.3908],
    "4305603": [-52.9823, -28.4841],
    "4305801": [-52.9949, -27.6877],
    "4305835": [-52.1244, -29.1674],
    "4305871": [-54.0557, -28.395],
    "4305900": [-53.6605, -27.7997],
    "4305934": [-51.7221, -29.2646],
    "4305959": [-51.6905, -29.0113],
    "4306007": [-54.1425, -27.4789],
    "4306056": [-52.0237, -31.0254],
    "4306072": [-53.2445, -27.4259],
    "4306106": [-53.5515, -28.7205],
    "4306205": [-52.0379, -29.5522],
    "4306304": [-51.8193, -28.4157],
    "4306320": [-53.8855, -27.2464],
    "4306379": [-54.1559, -29.8076],
    "4306403": [-51.0875, -29.6121],
    "4306429": [-53.513, -27.6725],
    "4306452": [-51.8463, -28.9755],
    "4306502": [-52.2275, -30.5947],
    "4306551": [-49.8655, -29.3541],
    "4306601": [-54.5642, -30.9804],
    "4306700": [-53.3438, -29.5734],
    "4306734": [-54.3678, -27.4914],
    "4306759": [-51.9801, -29.0967],
    "4306767": [-51.4808, -30.0716],
    "4306809": [-51.9308, -29.1994],
    "4306908": [-52.6778, -30.6108],
    "4306924": [-52.9113, -27.6778],
    "4306932": [-54.3058, -28.4552],
    "4306957": [-52.7244, -27.5125],
    "4306973": [-52.321, -27.8253],
    "4307005": [-52.2469, -27.6333],
    "4307054": [-52.5625, -28.4178],
    "4307104": [-53.3686, -32.0049],
    "4307203": [-52.5707, -27.3634],
    "4307302": [-53.5285, -27.497],
    "4307401": [-51.186, -28.0142],
    "4307500": [-52.8575, -28.8406],
    "4307559": [-52.2862, -27.9283],
    "4307609": [-51.1947, -29.6452],
    "4307708": [-51.1731, -29.8465],
    "4307807": [-51.9228, -29.5068],
    "4307815": [-53.1771, -29.2331],
    "4307864": [-51.7229, -28.8845],
    "4307906": [-51.3661, -29.1935],
    "4308003": [-53.4568, -29.5562],
    "4308052": [-52.6673, -27.3721],
    "4308078": [-51.8438, -29.5925],
    "4308102": [-51.2807, -29.4526],
    "4308201": [-51.2425, -29.0369],
    "4308250": [-52.0345, -27.8529],
    "4308300": [-52.3665, -29.0176],
    "4308409": [-53.4624, -29.9638],
    "4308433": [-52.1331, -29.3908],
    "4308458": [-53.3048, -28.9107],
    "4308508": [-53.3527, -27.3246],
    "4308607": [-51.5988, -29.2427],
    "4308656": [-55.5051, -28.237],
    "4308706": [-52.1082, -27.6119],
    "4308805": [-51.927, -29.8523],
    "4308904": [-52.1736, -27.8703],
    "4309001": [-54.3127, -28.0103],
    "4309050": [-50.7572, -29.8553],
    "4309100": [-50.9002, -29.3722],
    "4309159": [-52.61, -29.2806],
    "4309209": [-50.9403, -29.8848],
    "4309258": [-51.6515, -28.581],
    "4309308": [-51.4254, -30.1769],
    "4309407": [-51.8905, -28.8668],
    "4309506": [-54.5989, -28.1534],
    "4309555": [-51.424, -29.5482],
    "4309605": [-54.2954, -27.5729],
    "4309654": [-53.8991, -31.5053],
    "4309704": [-53.9957, -27.58],
    "4309753": [-53.1733, -29.4063],
    "4309803": [-51.8009, -28.0995],
    "4309902": [-51.638, -28.3831],
    "4310009": [-53.1825, -28.5882],
    "4310108": [-50.8031, -29.5548],
    "4310207": [-53.8991, -28.3299],
    "4310330": [-50.1259, -29.9258],
    "4310363": [-51.7605, -29.3375],
    "4310405": [-54.1958, -27.886],
    "4310413": [-54.0327, -27.9108],
    "4310439": [-51.2871, -28.7074],
    "4310462": [-52.4293, -27.9426],
    "4310504": [-53.238, -27.2541],
    "4310538": [-53.7543, -29.5679],
    "4310553": [-55.2726, -28.7826],
    "4310579": [-52.1928, -28.7743],
    "4310603": [-56.0684, -29.1748],
    "4310652": [-50.1787, -29.4218],
    "4310751": [-53.5808, -29.5063],
    "4310801": [-51.1567, -29.5843],
    "4310850": [-53.2663, -27.6209],
    "4310876": [-53.0069, -29.0494],
    "4310900": [-52.5468, -27.7781],
    "4311007": [-53.3332, -32.4168],
    "4311106": [-54.6402, -29.4582],
    "4311122": [-50.3705, -28.9264],
    "4311130": [-54.3153, -29.2969],
    "4311155": [-54.1162, -28.7232],
    "4311205": [-53.6294, -29.2689],
    "4311239": [-53.0373, -29.5024],
    "4311254": [-52.7722, -29.2528],
    "4311304": [-51.4804, -28.2277],
    "4311403": [-52.0269, -29.4378],
    "4311429": [-53.2054, -27.7005],
    "4311502": [-54.1617, -30.7685],
    "4311601": [-53.0735, -27.5415],
    "4311627": [-51.2238, -29.5805],
    "4311643": [-51.2183, -29.4576],
    "4311700": [-51.6934, -27.5894],
    "4311718": [-55.6314, -29.0667],
    "4311734": [-49.9987, -29.2569],
    "4311759": [-55.5733, -29.4098],
    "4311775": [-50.243, -29.5853],
    "4311809": [-52.2635, -28.4506],
    "4311908": [-51.9496, -27.4714],
    "4311981": [-51.5789, -30.3084],
    "4312005": [-52.1805, -27.3474],
    "4312104": [-54.4532, -29.5469],
    "4312138": [-52.1669, -28.2755],
    "4312179": [-54.6563, -28.2384],
    "4312203": [-51.7974, -27.5902],
    "4312252": [-52.0869, -30.0696],
    "4312302": [-53.7538, -27.4849],
    "4312351": [-52.056, -28.6611],
    "4312377": [-50.8228, -28.7224],
    "4312401": [-51.4847, -29.7102],
    "4312427": [-52.6664, -28.6844],
    "4312443": [-49.9712, -29.3488],
    "4312450": [-52.643, -31.6392],
    "4312476": [-51.0449, -29.5098],
    "4312500": [-50.8639, -30.9514],
    "4312609": [-51.8227, -29.139],
    "4312658": [-52.8062, -28.4743],
    "4312674": [-52.4423, -28.5266],
    "4312708": [-52.8877, -27.372],
    "4312757": [-52.1583, -28.7062],
    "4312807": [-51.776, -28.6519],
    "4312906": [-51.7915, -28.726],
    "4313003": [-52.0336, -29.2179],
    "4313037": [-54.8324, -29.3948],
    "4313060": [-50.8991, -29.5847],
    "4313086": [-51.3065, -29.0084],
    "4313201": [-51.0981, -29.3768],
    "4313300": [-51.5914, -28.7431],
    "4313334": [-53.6967, -28.0866],
    "4313359": [-51.4105, -28.9907],
    "4313375": [-51.2734, -29.8323],
    "4313409": [-51.0633, -29.7308],
    "4313425": [-54.5326, -27.5522],
    "4313441": [-53.161, -27.5596],
    "4313466": [-53.0563, -27.7465],
    "4313490": [-53.1104, -27.8923],
    "4313508": [-50.2239, -29.899],
    "4313607": [-51.7676, -27.7218],
    "4313656": [-50.5141, -30.3384],
    "4313706": [-53.3531, -27.9324],
    "4313805": [-53.5923, -27.3226],
    "4313904": [-53.5566, -28.3033],
    "4313953": [-52.3367, -30.2609],
    "4314001": [-51.8048, -28.6006],
    "4314027": [-53.1178, -29.701],
    "4314035": [-51.4244, -29.6088],
    "4314050": [-50.8517, -29.6657],
    "4314068": [-52.8569, -29.4225],
    "4314076": [-52.2446, -29.7524],
    "4314100": [-52.4521, -28.2915],
    "4314134": [-52.4081, -27.722],
    "4314159": [-51.7312, -29.5723],
    "4314175": [-53.7025, -31.8466],
    "4314209": [-52.8949, -31.951],
    "4314308": [-53.6101, -28.4469],
    "4314407": [-52.3349, -31.5386],
    "4314423": [-51.1057, -29.453],
    "4314456": [-53.2319, -27.5273],
    "4314464": [-51.2371, -27.8478],
    "4314472": [-53.3333, -29.2684],
    "4314498": [-53.6428, -27.2175],
    "4314506": [-53.4089, -31.3603],
    "4314555": [-55.2398, -28.0561],
    "4314605": [-53.098, -31.4069],
    "4314704": [-53.0932, -27.3424],
    "4314779": [-52.6305, -28.0407],
    "4314787": [-52.5082, -27.6661],
    "4314803": [-51.2458, -29.6911],
    "4314902": [-51.1731, -30.11],
    "4315008": [-54.9556, -27.8466],
    "4315057": [-54.6562, -27.5991],
    "4315073": [-54.8978, -27.7637],
    "4315107": [-55.1392, -27.9384],
    "4315131": [-52.2192, -29.1714],
    "4315149": [-51.1881, -29.523],
    "4315156": [-52.3114, -29.2236],
    "4315206": [-52.1559, -29.0357],
    "4315305": [-56.1578, -30.2986],
    "4315321": [-54.072, -29.322],
    "4315354": [-53.115, -28.7553],
    "4315404": [-53.6117, -27.5651],
    "4315453": [-52.0536, -29.115],
    "4315503": [-53.3392, -29.8229],
    "4315602": [-52.2602, -32.0432],
    "4315701": [-52.4334, -30.0515],
    "4315750": [-50.3879, -29.6113],
    "4315800": [-51.8299, -29.2319],
    "4315909": [-53.1692, -27.4627],
    "4315958": [-54.8441, -28.2414],
    "4316006": [-50.5392, -29.6396],
    "4316105": [-52.7338, -27.8126],
    "4316204": [-52.912, -27.83],
    "4316303": [-55.1176, -28.0551],
    "4316402": [-55.0872, -30.3237],
    "4316428": [-53.1262, -27.7083],
    "4316436": [-53.0971, -28.3801],
    "4316451": [-53.2352, -29.0728],
    "4316477": [-54.8308, -28.0819],
    "4316501": [-51.5344, -29.4622],
    "4316600": [-51.8241, -27.931],
    "4316709": [-53.2534, -28.3823],
    "4316808": [-52.4121, -29.6517],
    "4316907": [-53.817, -29.7705],
    "4316956": [-50.9742, -29.4802],
    "4316972": [-54.0738, -30.3645],
    "4317004": [-53.1843, -30.7732],
    "4317103": [-55.5363, -30.7607],
    "4317202": [-54.4825, -27.8358],
    "4317251": [-51.7112, -29.1556],
    "4317301": [-53.2213, -33.2388],
    "4317400": [-54.7594, -29.1398],
    "4317509": [-54.3139, -28.2653],
    "4317558": [-52.0067, -28.494],
    "4317608": [-50.5749, -29.8166],
    "4317707": [-55.4113, -28.4711],
    "4317756": [-52.661, -28.3825],
    "4317806": [-53.7243, -27.8984],
    "4317905": [-54.7045, -27.793],
    "4317954": [-51.6634, -27.9253],
    "4318002": [-55.7906, -28.7467],
    "4318051": [-51.8678, -28.5356],
    "4318101": [-55.1382, -29.4236],
    "4318200": [-50.4524, -29.2375],
    "4318309": [-54.3733, -30.3166],
    "4318408": [-51.9123, -30.2913],
    "4318424": [-51.8361, -27.7928],
    "4318432": [-53.4523, -29.6339],
    "4318440": [-51.7221, -28.4969],
    "4318457": [-53.1242, -27.7985],
    "4318465": [-52.2735, -29.0636],
    "4318499": [-54.1249, -27.7352],
    "4318507": [-51.7629, -31.7815],
    "4318606": [-51.5656, -27.7647],
    "4318614": [-51.4831, -29.5404],
    "4318705": [-51.1316, -29.7468],
    "4318804": [-52.0984, -31.2082],
    "4318903": [-54.9091, -28.4071],
    "4319000": [-51.0657, -28.9668],
    "4319109": [-53.9706, -27.7284],
    "4319125": [-53.8929, -29.4552],
    "4319158": [-54.5238, -28.7031],
    "4319208": [-55.2605, -28.2192],
    "4319307": [-54.9657, -27.9764],
    "4319356": [-51.5107, -29.4184],
    "4319372": [-54.8966, -28.1468],
    "4319406": [-54.2609, -29.6081],
    "4319505": [-51.3391, -29.5882],
    "4319604": [-53.6035, -30.1688],
    "4319711": [-51.7525, -29.0578],
    "4319737": [-53.9216, -27.8127],
    "4319802": [-54.7518, -29.7169],
    "4319901": [-50.9855, -29.6304],
    "4320008": [-51.1466, -29.8186],
    "4320107": [-52.9342, -27.9266],
    "4320206": [-53.3694, -27.5032],
    "4320230": [-53.9573, -27.6413],
    "4320263": [-52.9232, -29.2978],
    "4320305": [-52.9842, -28.6821],
    "4320321": [-54.5328, -28.0327],
    "4320404": [-51.9469, -28.6826],
    "4320453": [-52.2454, -29.406],
    "4320552": [-51.6512, -30.4822],
    "4320602": [-52.1101, -27.4154],
    "4320651": [-53.5646, -29.6293],
    "4320677": [-52.5969, -29.4236],
    "4320701": [-53.0155, -29.3905],
    "4320800": [-52.5181, -28.8436],
    "4320859": [-51.732, -29.6677],
    "4320909": [-52.0193, -28.0525],
    "4321006": [-52.8644, -28.6681],
    "4321105": [-51.4258, -30.695],
    "4321204": [-50.7814, -29.6693],
    "4321303": [-51.8132, -29.7269],
    "4321329": [-53.496, -27.4053],
    "4321352": [-51.0692, -31.2602],
    "4321402": [-53.7731, -27.3635],
    "4321436": [-50.0302, -29.5922],
    "4321451": [-51.7736, -29.4715],
    "4321501": [-49.8168, -29.304],
    "4321600": [-50.2236, -30.0306],
    "4321626": [-52.0995, -29.2778],
    "4321667": [-49.9767, -29.4651],
    "4321709": [-50.7694, -29.4696],
    "4321808": [-54.2563, -27.7349],
    "4321832": [-50.0721, -29.4425],
    "4321857": [-52.8535, -27.6104],
    "4321907": [-53.9201, -27.427],
    "4321956": [-52.9128, -27.5305],
    "4322004": [-51.5717, -29.8241],
    "4322103": [-54.4478, -27.6475],
    "4322152": [-52.8927, -29.1058],
    "4322186": [-51.5471, -27.9247],
    "4322202": [-53.9645, -29.0129],
    "4322251": [-51.4279, -29.4724],
    "4322301": [-54.5624, -27.6858],
    "4322327": [-52.1489, -31.4921],
    "4322343": [-54.6627, -28.0483],
    "4322350": [-52.0345, -28.7795],
    "4322376": [-55.1872, -29.0889],
    "4322400": [-56.6612, -29.8134],
    "4322509": [-50.9323, -28.4061],
    "4322525": [-52.1076, -29.8408],
    "4322533": [-52.6908, -29.5699],
    "4322541": [-51.245, -29.3626],
    "4322558": [-51.8362, -28.4899],
    "4322608": [-52.2099, -29.5419],
    "4322707": [-52.5249, -29.762],
    "4322806": [-51.5596, -28.9648],
    "4322855": [-51.8594, -29.065],
    "4322905": [-51.9878, -27.571],
    "4323002": [-50.9196, -30.1932],
    "4323200": [-52.6907, -28.5453],
    "4323309": [-51.5414, -28.8606],
    "4323358": [-52.1372, -28.1217],
    "4323408": [-52.162, -28.556],
    "4323457": [-53.8757, -30.3412],
    "4323507": [-53.5178, -27.3092],
    "4323606": [-51.7805, -28.8257],
    "4323754": [-54.471, -28.3551],
    "4323770": [-51.7498, -29.4071],
    "4323804": [-50.0877, -29.8067],
    "5000203": [-52.9326, -19.8864],
    "5000252": [-53.7831, -18.183],
    "5000609": [-54.9401, -23.0999],
    "5000708": [-55.7378, -20.7503],
    "5000807": [-52.7516, -22.1099],
    "5000856": [-53.8562, -22.0256],
    "5000906": [-55.9383, -22.1988],
    "5001003": [-51.3183, -20.0227],
    "5001102": [-55.8861, -19.8444],
    "5001243": [-55.3903, -22.9095],
    "5002001": [-53.1782, -22.4663],
    "5002100": [-56.5032, -22.0354],
    "5002159": [-56.6722, -20.4855],
    "5002308": [-52.4595, -21.0823],
    "5002407": [-54.8181, -22.6051],
    "5002605": [-53.839, -19.3231],
    "5002704": [-54.2232, -21.0041],
    "5002803": [-57.1244, -21.9702],
    "5002902": [-52.0888, -19.056],
    "5002951": [-52.727, -19.0928],
    "5003108": [-54.9885, -19.8077],
    "5003207": [-56.823, -18.6201],
    "5003256": [-53.1754, -18.6739],
    "5003306": [-54.6906, -18.2514],
    "5003454": [-54.1939, -22.1007],
    "5003488": [-55.3009, -20.631],
    "5003702": [-54.8096, -22.1592],
    "5003751": [-54.2191, -23.7831],
    "5003801": [-54.4372, -22.3288],
    "5004007": [-54.1439, -22.4524],
    "5004106": [-55.9299, -21.6101],
    "5004304": [-54.536, -23.424],
    "5004403": [-52.0387, -19.6495],
    "5004502": [-54.8134, -21.943],
    "5004601": [-54.0871, -23.3003],
    "5004700": [-53.7546, -22.3725],
    "5004809": [-54.5536, -23.8],
    "5004908": [-54.2432, -20.273],
    "5005004": [-56.242, -21.649],
    "5005103": [-53.8498, -22.7261],
    "5005152": [-54.5085, -22.8383],
    "5005202": [-57.574, -19.0929],
    "5005251": [-55.0799, -22.706],
    "5005400": [-55.5498, -21.4212],
    "5005608": [-56.5316, -20.2047],
    "5005681": [-54.2654, -23.9186],
    "5005707": [-54.0377, -23.1018],
    "5005806": [-55.7642, -21.1868],
    "5006002": [-54.1711, -21.4872],
    "5006259": [-53.7186, -22.63],
    "5006309": [-51.3975, -19.5038],
    "5006606": [-55.7227, -22.0224],
    "5006903": [-57.3475, -21.3067],
    "5007109": [-53.5615, -20.7202],
    "5007208": [-54.461, -21.7594],
    "5007307": [-54.9781, -19.4572],
    "5007406": [-54.9011, -18.8245],
    "5007505": [-54.7726, -19.9776],
    "5007554": [-52.7129, -21.3462],
    "5007695": [-54.4595, -19.1364],
    "5007802": [-51.8104, -20.2604],
    "5007901": [-55.0427, -20.9358],
    "5007935": [-54.4201, -17.6627],
    "5008008": [-55.098, -20.4198],
    "5008404": [-54.4392, -22.4867],
    "5100102": [-56.3135, -15.165],
    "5100201": [-52.436, -14.1305],
    "5100250": [-56.3347, -10.0442],
    "5100300": [-53.3879, -17.4495],
    "5100359": [-51.7118, -11.7948],
    "5100409": [-53.6109, -16.8213],
    "5101209": [-53.0663, -16.788],
    "5101258": [-58.4948, -15.1963],
    "5101308": [-56.8587, -14.4845],
    "5101407": [-59.693, -10.1831],
    "5101605": [-56.2207, -16.8487],
    "5101704": [-57.6604, -15.0777],
    "5101803": [-52.5589, -15.3543],
    "5102504": [-57.73, -16.5722],
    "5102603": [-53.1489, -14.2838],
    "5102637": [-57.9142, -13.5878],
    "5102678": [-54.9599, -15.4143],
    "5102702": [-52.4048, -13.1887],
    "5102793": [-55.8531, -10.0647],
    "5103007": [-55.5314, -15.0328],
    "5103106": [-51.1481, -13.7703],
    "5103205": [-55.4667, -10.5748],
    "5103254": [-60.1442, -9.5546],
    "5103304": [-59.8355, -13.3129],
    "5103353": [-51.7307, -10.3175],
    "5103361": [-59.2734, -14.5652],
    "5103437": [-57.8387, -15.6315],
    "5103452": [-56.9474, -14.7335],
    "5103502": [-56.7437, -14.0411],
    "5103700": [-54.1811, -11.9128],
    "5103809": [-58.7006, -15.4945],
    "5103858": [-53.4962, -12.9981],
    "5103908": [-53.2985, -15.5606],
    "5103957": [-58.315, -15.894],
    "5104104": [-54.7035, -9.8481],
    "5104203": [-53.5835, -16.3691],
    "5104500": [-58.6193, -15.3298],
    "5104542": [-56.7677, -12.1858],
    "5104559": [-55.5847, -11.0959],
    "5104807": [-55.0228, -15.8911],
    "5105002": [-58.8588, -15.3217],
    "5105101": [-57.5082, -11.1118],
    "5105200": [-54.8855, -16.2033],
    "5105234": [-57.8393, -15.4964],
    "5105309": [-50.9947, -10.8909],
    "5105507": [-60.0564, -14.8844],
    "5105622": [-58.0203, -15.637],
    "5105903": [-55.7808, -14.4055],
    "5106000": [-56.7353, -14.4162],
    "5106208": [-55.1342, -14.7759],
    "5106232": [-57.4191, -14.7788],
    "5106240": [-54.4517, -12.6515],
    "5106299": [-56.7968, -9.5557],
    "5106315": [-50.9066, -12.2828],
    "5106422": [-54.1405, -10.1967],
    "5106455": [-54.684, -14.5325],
    "5106505": [-57.0108, -16.8552],
    "5106653": [-52.7416, -15.9542],
    "5106828": [-58.9417, -15.8876],
    "5106851": [-57.2332, -15.5198],
    "5107040": [-54.212, -15.1797],
    "5107107": [-58.3169, -15.5345],
    "5107156": [-58.5056, -14.9221],
    "5107180": [-51.658, -12.8068],
    "5107198": [-52.7428, -16.4986],
    "5107206": [-58.1613, -15.2692],
    "5107305": [-56.805, -13.52],
    "5107404": [-54.7823, -15.9588],
    "5107578": [-61.0229, -10.2627],
    "5107602": [-54.6337, -16.4621],
    "5107750": [-58.082, -15.0351],
    "5107776": [-50.8093, -10.3824],
    "5107800": [-55.343, -16.6887],
    "5107859": [-52.0927, -11.4705],
    "5107925": [-55.6997, -12.6605],
    "5107958": [-58.1751, -14.5192],
    "5108204": [-52.8783, -16.2596],
    "5108303": [-54.2203, -11.4789],
    "5108402": [-56.2696, -15.5549],
    "5108501": [-55.3473, -12.4334],
    "5108956": [-57.2639, -9.9032],
    "5200050": [-49.4638, -16.7851],
    "5200100": [-48.6493, -16.2108],
    "5200134": [-50.2704, -17.4383],
    "5200159": [-50.1826, -16.3886],
    "5200175": [-47.8505, -14.9444],
    "5200209": [-48.8001, -18.0866],
    "5200258": [-48.2874, -15.7581],
    "5200308": [-48.4615, -16.1511],
    "5200506": [-49.4527, -17.6946],
    "5200555": [-49.4384, -14.2],
    "5200605": [-47.4733, -14.1964],
    "5200803": [-46.6834, -14.5287],
    "5200852": [-49.9883, -16.2688],
    "5200902": [-51.1081, -16.6455],
    "5201108": [-49.0105, -16.2811],
    "5201306": [-49.9565, -16.3966],
    "5201405": [-49.2566, -16.8257],
    "5201454": [-51.2519, -18.2301],
    "5201504": [-52.03, -18.7752],
    "5201603": [-49.7104, -16.3751],
    "5201702": [-52.0648, -15.9423],
    "5202353": [-51.5983, -16.3498],
    "5202502": [-50.9442, -14.8178],
    "5202809": [-49.7688, -16.4813],
    "5203104": [-52.4473, -16.3652],
    "5203203": [-48.8718, -14.8834],
    "5203302": [-48.9158, -16.9665],
    "5203401": [-52.0567, -16.2583],
    "5203500": [-49.9095, -18.1905],
    "5203559": [-49.0173, -16.6007],
    "5203609": [-49.3714, -16.377],
    "5203807": [-51.1521, -15.206],
    "5203906": [-49.001, -18.1299],
    "5203939": [-50.4323, -16.1698],
    "5203962": [-46.3282, -14.4189],
    "5204003": [-47.0238, -15.738],
    "5204102": [-50.9794, -18.5285],
    "5204201": [-50.6857, -16.7216],
    "5204250": [-49.6233, -18.4992],
    "5204300": [-51.0625, -18.7803],
    "5204508": [-48.6578, -17.7341],
    "5204607": [-49.7007, -16.788],
    "5204656": [-48.5696, -13.8755],
    "5204706": [-48.9322, -13.9623],
    "5204805": [-47.7511, -17.6497],
    "5204854": [-49.0882, -16.2855],
    "5204904": [-46.4853, -12.9928],
    "5204953": [-49.6637, -14.1942],
    "5205000": [-49.7338, -15.423],
    "5205059": [-50.3641, -18.1545],
    "5205109": [-47.7203, -17.9684],
    "5205307": [-47.7595, -13.6118],
    "5205406": [-49.6414, -15.2766],
    "5205455": [-49.73, -17.1097],
    "5205497": [-47.8048, -16.1511],
    "5205513": [-48.5845, -15.6375],
    "5205521": [-48.0501, -14.0019],
    "5205703": [-50.5718, -16.3897],
    "5205802": [-48.6512, -15.9361],
    "5205901": [-48.5504, -18.1756],
    "5206206": [-47.4755, -16.765],
    "5206305": [-48.7039, -17.2097],
    "5206404": [-50.033, -14.6657],
    "5206503": [-49.346, -17.2561],
    "5206602": [-48.1788, -18.3102],
    "5206701": [-46.1845, -14.5464],
    "5206909": [-47.5771, -18.1468],
    "5207105": [-51.3625, -16.1928],
    "5207253": [-52.5061, -16.8426],
    "5207352": [-49.7335, -17.4469],
    "5207402": [-50.0117, -17.5516],
    "5207501": [-49.0894, -13.7872],
    "5207535": [-50.3931, -15.4387],
    "5207600": [-50.9102, -16.1313],
    "5207808": [-50.3237, -16.6373],
    "5207907": [-46.8855, -14.5881],
    "5208004": [-47.2052, -15.281],
    "5208301": [-46.5443, -13.222],
    "5208400": [-49.0836, -16.5213],
    "5208608": [-49.1718, -15.3096],
    "5208806": [-49.4365, -16.4987],
    "5208905": [-50.2444, -15.8099],
    "5209200": [-49.5958, -16.9198],
    "5209291": [-50.0644, -15.6505],
    "5209408": [-46.5058, -13.8794],
    "5209457": [-49.7429, -14.6496],
    "5209606": [-49.8254, -15.7339],
    "5209705": [-49.2447, -17.0142],
    "5209804": [-49.3525, -14.7556],
    "5209903": [-46.7362, -14.0799],
    "5209937": [-49.9153, -18.5011],
    "5209952": [-49.9699, -17.2231],
    "5210000": [-49.5222, -16.3251],
    "5210109": [-48.0096, -17.4984],
    "5210158": [-49.6525, -15.1415],
    "5210208": [-51.1568, -16.4489],
    "5210307": [-50.8854, -16.3575],
    "5210406": [-49.8135, -16.1246],
    "5210562": [-49.6149, -15.9144],
    "5210604": [-49.6012, -15.766],
    "5210901": [-49.6815, -14.9227],
    "5211305": [-51.3132, -18.8823],
    "5211404": [-49.601, -16.208],
    "5211503": [-49.4554, -18.3382],
    "5211602": [-51.1231, -16.6497],
    "5211701": [-50.2068, -17.1402],
    "5211800": [-49.4164, -15.6876],
    "5211909": [-51.6851, -17.8742],
    "5212006": [-51.0788, -16.1727],
    "5212055": [-49.3996, -15.958],
    "5212105": [-49.5803, -17.7851],
    "5212253": [-51.2815, -19.2132],
    "5212303": [-48.8942, -16.586],
    "5212600": [-49.4419, -17.3159],
    "5212709": [-46.0436, -14.4354],
    "5212808": [-49.4247, -14.0114],
    "5212907": [-48.6775, -17.9913],
    "5212956": [-50.8212, -15.3313],
    "5213004": [-50.3428, -18.0403],
    "5213053": [-48.3564, -15.0518],
    "5213087": [-48.3794, -13.4466],
    "5213103": [-52.698, -17.4068],
    "5213400": [-50.7742, -16.4955],
    "5213509": [-46.8857, -13.2734],
    "5213707": [-51.5312, -15.9365],
    "5213772": [-48.7463, -13.0999],
    "5213806": [-49.095, -17.7955],
    "5213855": [-50.0072, -15.3215],
    "5213905": [-50.1789, -16.1592],
    "5214002": [-50.6204, -14.7641],
    "5214051": [-50.2167, -13.7131],
    "5214101": [-49.3297, -13.7066],
    "5214408": [-49.8609, -16.5794],
    "5214606": [-48.4089, -14.5021],
    "5214705": [-49.9283, -15.051],
    "5214804": [-48.2787, -18.0995],
    "5214838": [-50.5867, -14.2661],
    "5214861": [-49.4804, -15.058],
    "5214903": [-47.0049, -13.8229],
    "5215207": [-50.6528, -16.046],
    "5215231": [-48.0716, -16.1151],
    "5215256": [-49.75, -13.3578],
    "5215306": [-48.1641, -16.9911],
    "5215405": [-49.2201, -16.2343],
    "5215504": [-47.7108, -18.2105],
    "5215603": [-48.3293, -15.3338],
    "5215702": [-49.9059, -16.8563],
    "5215900": [-50.2226, -16.8275],
    "5216007": [-49.4016, -18.1922],
    "5216304": [-50.611, -18.7782],
    "5216809": [-49.2899, -16.1067],
    "5216908": [-49.5115, -14.5403],
    "5217104": [-49.0192, -17.3337],
    "5217203": [-51.8374, -16.3919],
    "5217302": [-49.013, -15.8075],
    "5217401": [-48.3866, -17.3396],
    "5217609": [-47.8756, -15.227],
    "5217708": [-49.5798, -17.5138],
    "5218052": [-50.161, -17.9003],
    "5218102": [-52.6928, -17.3414],
    "5218300": [-46.4679, -14.2511],
    "5218391": [-49.2558, -17.2973],
    "5218508": [-50.5298, -18.4537],
    "5218607": [-49.5338, -15.3275],
    "5218706": [-49.4539, -15.4728],
    "5218789": [-48.7972, -17.8205],
    "5218904": [-49.8769, -15.1699],
    "5219001": [-50.4213, -16.3204],
    "5219258": [-51.1389, -15.6464],
    "5219308": [-50.5477, -17.8063],
    "5219357": [-49.3813, -15.2619],
    "5219407": [-53.0786, -17.2083],
    "5219506": [-49.4872, -16.0608],
    "5219605": [-48.987, -13.5482],
    "5219704": [-49.7225, -14.2887],
    "5219712": [-50.6204, -17.5059],
    "5219753": [-48.2964, -16.0741],
    "5219803": [-46.5048, -13.5279],
    "5219902": [-49.2525, -15.9443],
    "5220009": [-47.4205, -14.4302],
    "5220058": [-50.3532, -16.8161],
    "5220108": [-50.3764, -16.4501],
    "5220157": [-49.2723, -14.9005],
    "5220207": [-50.2722, -12.9899],
    "5220264": [-48.6728, -16.9922],
    "5220280": [-49.8288, -15.3642],
    "5220454": [-49.1171, -16.7179],
    "5220504": [-52.2156, -18.2772],
    "5220603": [-48.5831, -16.5858],
    "5220686": [-46.5973, -14.4376],
    "5221007": [-49.5809, -16.0538],
    "5221080": [-47.2451, -13.7147],
    "5221197": [-49.0741, -16.4349],
    "5221304": [-47.7899, -18.3679],
    "5221403": [-49.5473, -16.654],
    "5221452": [-48.7653, -13.4066],
    "5221502": [-50.1666, -16.5611],
    "5221551": [-50.2995, -17.7954],
    "5221577": [-49.9225, -14.1324],
    "5221601": [-49.0561, -14.4054],
    "5221700": [-49.6427, -15.5694],
    "5221809": [-48.199, -17.4261],
    "5221858": [-47.9835, -16.1035],
    "5221908": [-49.619, -17.0747],
    "5222005": [-48.4551, -16.8436],
    "5222054": [-49.9147, -17.7012],
    "5222203": [-47.093, -14.9704],
}


if __name__ == "__main__":
    main()
