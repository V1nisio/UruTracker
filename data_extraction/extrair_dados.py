import time
import requests
import pandas as pd
from pathlib import Path

# --- Parte 1: Vinicius ---

BASE_URL = "https://api.transferegov.dth.api.gov.br/transferenciasespeciais"
OUT_DIR = Path(__file__).parent
ANO_MINIMO = 2024

VIEWS = {
    "plano_acao_especial": (
        "id_plano_acao,ano_plano_acao,nome_beneficiario_plano_acao,"
        "cnpj_beneficiario_plano_acao,uf_beneficiario_plano_acao,"
        "nome_parlamentar_emenda_plano_acao,valor_custeio_plano_acao,"
        "valor_investimento_plano_acao,situacao_plano_acao"
    ),
    "plano_trabalho_especial": (
        "id_plano_acao,situacao_plano_trabalho,"
        "ind_justificativa_prorrogacao_paralizacao_pt,ind_justificativa_prorrogacao_atraso_pt,"
        "data_inicio_execucao_plano_trabalho,data_fim_execucao_plano_trabalho"
    ),
    "executor_especial": "id_plano_acao,id_executor,objeto_executor",
    "finalidade_especial": "id_executor,area_politica_publica_pt",
}


# --- Parte 2: Theo ---

def fetch_view(view: str, select: str, filtro: dict | None = None, batch_size: int = 1000, max_retries: int = 3) -> pd.DataFrame:
    headers = {"Prefer": "count=exact"}
    params = {"select": select, "limit": batch_size, "offset": 0}
    if filtro:
        params.update(filtro)

    def get(p):
        for attempt in range(max_retries):
            try:
                resp = requests.get(f"{BASE_URL}/{view}", params=p, headers=headers, timeout=30)
                resp.raise_for_status()
                return resp
            except requests.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2)

    resp = get(params)
    total = int(resp.headers["Content-Range"].split("/")[-1])
    rows = resp.json()

    offset = batch_size
    while offset < total:
        params["offset"] = offset
        resp = get(params)
        rows.extend(resp.json())
        offset += batch_size
        print(f"  {view}: {min(offset, total)}/{total}")
        time.sleep(0.1)

    return pd.DataFrame(rows)
