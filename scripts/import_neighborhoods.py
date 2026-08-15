"""
import_neighborhoods.py
Importa renda por bairro (nomeado) do Censo 2022 — produto separado do
IBGE ("Agregados por Setores Censitários - Rendimento do Responsável"),
descoberto ao pesquisar como responder "que bairro dessa cidade combina
com o público da franquia?".

Diferente do que se imaginava a princípio, não precisa de malha
geográfica/shapefile pra isso — o arquivo já vem com nome de bairro e
código do município (os 7 primeiros dígitos de CD_BAIRRO = ibge_code,
o mesmo código que a tabela `cities` já usa).

Isso NÃO resolve geocodificação de um endereço específico (não sabemos
em que bairro fica "Rua X, 123") — só permite rankear os bairros de uma
cidade por renda mediana do responsável pelo domicílio.

Fonte (verificada ao vivo em 2026-08-15):
  https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios_Rendimento_do_Responsavel/Agregados_por_bairros_renda_responsavel_BR_20260508_csv.zip
  491KB zipado, 17.379 bairros, CSV com ; e latin-1.

Uso:
  python scripts/import_neighborhoods.py
"""

import csv
import io
import os
import sys
import zipfile

import requests
from dotenv import load_dotenv
from supabase import create_client

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

CSV_URL = (
    "https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/"
    "Agregados_por_Setores_Censitarios_Rendimento_do_Responsavel/"
    "Agregados_por_bairros_renda_responsavel_BR_20260508_csv.zip"
)

CYAN, GREEN, YELLOW, RED, RESET, BOLD = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m", "\033[0m", "\033[1m"
)


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
              "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")


def parse_number(s: str) -> float | None:
    """
    A maioria das colunas numéricas desse CSV usa vírgula decimal (formato
    BR: ponto = milhar). Mas a coluna de mediana (V06006) vem inconsistente
    — às vezes inteiro puro, às vezes com PONTO decimal ("3107.5"), nunca
    com vírgula. Achado rodando de verdade: tratar todo "." como milhar
    cegamente transformava 3107.5 em 31075 (10x inflado). Corrigido: só
    aplica a conversão BR quando há vírgula; senão usa o número como está.
    """
    s = (s or "").strip()
    if not s or s in ("X", "-", "..", "..."):
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fetch_all_cities(supabase) -> dict[str, str]:
    """{ibge_code: city_id} — pagina pra passar do limite de 1000 do PostgREST."""
    rows, start, page_size = [], 0, 1000
    while True:
        batch = supabase.table("cities").select("id, ibge_code").range(start, start + page_size - 1).execute().data
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return {r["ibge_code"]: r["id"] for r in rows}


def main() -> None:
    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    header("Baixando renda por bairro (IBGE Censo 2022)")
    r = requests.get(CSV_URL, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
        raw = z.read(csv_name).decode("latin-1")
    log(f"{len(raw.splitlines())} linhas baixadas", "OK")

    header("Carregando cidades (crosswalk por ibge_code)")
    city_id_by_ibge = fetch_all_cities(supabase)
    log(f"{len(city_id_by_ibge)} cidades carregadas", "OK")

    header("Processando bairros")
    reader = csv.DictReader(io.StringIO(raw), delimiter=";")
    payload = []
    skipped_no_city = 0
    for row in reader:
        cd_bairro = row["CD_BAIRRO"].strip()
        ibge_code = cd_bairro[:7]
        city_id = city_id_by_ibge.get(ibge_code)
        if not city_id:
            skipped_no_city += 1
            continue
        payload.append({
            "city_id": city_id,
            "cd_bairro": cd_bairro,
            "name": row["NM_BAIRRO"].strip(),
            "households_count": int(parse_number(row["V06001"]) or 0) or None,
            "residents_count": int(parse_number(row["V06002"]) or 0) or None,
            "avg_income": parse_number(row["V06004"]),
            "median_income": parse_number(row["V06006"]),
        })

    # A própria fonte do IBGE tem pelo menos 1 CD_BAIRRO duplicado (visto em
    # 2026-08-15: "São Toquarto"/"São Torquato" com o mesmo código em Vila
    # Velha/ES — provável erro de digitação no dado original). Sem isso o
    # upsert falha ("ON CONFLICT DO UPDATE command cannot affect row a
    # second time"). Mantém o último (maior nº de moradores = mais provável
    # de ser o nome correto), descarta o resto.
    by_cd: dict[str, dict] = {}
    for row in payload:
        prev = by_cd.get(row["cd_bairro"])
        if prev is None or (row["residents_count"] or 0) >= (prev["residents_count"] or 0):
            by_cd[row["cd_bairro"]] = row
    duplicates = len(payload) - len(by_cd)
    payload = list(by_cd.values())
    if duplicates:
        log(f"{duplicates} CD_BAIRRO duplicado(s) na fonte do IBGE — mantido o de maior população", "WARN")

    log(f"{len(payload)} bairros prontos pra gravar ({skipped_no_city} sem cidade correspondente)", "INFO")

    header("Gravando no Supabase")
    inserted = 0
    for i in range(0, len(payload), 500):
        batch = payload[i:i + 500]
        supabase.table("neighborhoods").upsert(batch, on_conflict="cd_bairro").execute()
        inserted += len(batch)
        print(f"\r  {inserted}/{len(payload)}", end="", flush=True)
    print()

    log(f"{inserted} bairros gravados", "OK")


if __name__ == "__main__":
    main()
