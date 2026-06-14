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

def fetch_view(view, select, filtro=None):
    params = {"select": select, "limit": 1000}
    if filtro:
        params.update(filtro)
    for tentativa in range(3):
        try:
            resp = requests.get(f"{BASE_URL}/{view}", params=params, timeout=30)
            resp.raise_for_status()
            return pd.DataFrame(resp.json())
        except requests.RequestException:
            if tentativa == 2:
                raise
            print(f"  Tentativa {tentativa + 1} falhou, aguardando...")
            time.sleep(2)
