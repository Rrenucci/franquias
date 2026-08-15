"""
import_cnpj_lifecycle.py
Popula a tabela `company_lifecycle` (mortalidade/sobrevida de empresas por
cidade + segmento) a partir dos Dados Abertos de CNPJ da Receita Federal.

É o dado que sustenta score_survival_risk e score_market_gap — cruza
abertura/baixa de CNPJ por CNAE e município pra responder "empresas desse
tipo já tentaram aqui antes, e deram certo?".

── Fonte (verificado em 2026-08-14 via WebDAV — a URL antiga não existe mais) ─
O portal em arquivos.receitafederal.gov.br agora roda em Nextcloud. Acesso via
WebDAV (basic auth com o token do share como usuário, sem senha):
  https://<token>:@arquivos.receitafederal.gov.br/public.php/webdav/<AAAA-MM>/<arquivo>
Confirmado no mês 2026-08 (mais recente disponível): 10 arquivos
Estabelecimentos0.zip..9.zip, ~4.97GB no total. Empresas/Sócios/Simples não
são baixados (não precisamos de razão social/sócios pra sobrevida).

── DuckDB NÃO lê .zip diretamente ────────────────────────────────────────
Só suporta gzip/zstd em read_csv, não arquivos .zip (que são um formato de
arquivo-contêiner, não um stream de compressão único). Por isso cada
Estabelecimentos*.zip é baixado, extraído pra CSV, agregado com DuckDB, e
o CSV extraído é apagado antes do próximo — processando um arquivo por vez
pra não precisar de ~16GB de disco livre de uma vez (a extração expande
~3.3x: um zip de 2GB vira ~6-7GB de CSV).

── Layout de colunas (VALIDADO em 2026-08-14 contra um arquivo real) ─────
Baixei Estabelecimentos8.zip (mês 2026-08) e inspecionei linha a linha —
os índices abaixo são os que o DuckDB usa (`column00`..`column29`,
0-indexados, sem header):
  column00 cnpj_basico          column01 cnpj_ordem       column02 cnpj_dv
  column03 identificador_matriz_filial (1=matriz, 2=filial)
  column04 nome_fantasia
  column05 situacao_cadastral   (02=Ativa, 08=Baixada — confirmado nos dados)
  column06 data_situacao_cadastral  (AAAAMMDD)
  column07 motivo_situacao_cadastral
  column08 nome_cidade_exterior column09 pais
  column10 data_inicio_atividade    (AAAAMMDD)
  column11 cnae_fiscal_principal
  column12 cnae_fiscal_secundaria
  column13..18 endereço (logradouro, número, complemento, bairro, cep)
  column19 uf
  column20 municipio   (código TOM da Receita — usar load_municipio_crosswalk())
  column21..29 ddd/telefones/email/situação especial
Numa versão anterior deste script esses índices estavam ERRADOS (situação
cadastral em column06 em vez de column05, e data_inicio_atividade era um
placeholder inválido "column11_2") — só foi pego rodando contra dado real,
não teria aparecido só lendo o código.

── Crosswalk de município (RESOLVIDO) ────────────────────────────────────
O código de município da Receita Federal (`municipio` acima) NÃO é o
ibge_code de 7 dígitos. Resolvido com gov.br/receitafederal/dados/municipios.csv
(TOM — Tabela de Órgãos e Municípios), 5.571 municípios, bate exato com a
tabela `cities`. Ver load_municipio_crosswalk() — é um download pequeno
(~270KB), separado do dump grande, sem necessidade de confirmação.

── ATENÇÃO ─────────────────────────────────────────────────────────────────
Este script só baixa os ~5GB de Estabelecimentos com --confirm-big-download.
Isso deve ter sido combinado explicitamente com o usuário antes de usar essa
flag — ela é só uma trava adicional, não substitui a conversa.

Uso:
  python scripts/import_cnpj_lifecycle.py --periodo 2026-08 --dry-run
  python scripts/import_cnpj_lifecycle.py --periodo 2026-08 --confirm-big-download
"""

import argparse
import csv
import io
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime

import requests
from dotenv import load_dotenv
from supabase import create_client

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

try:
    import duckdb
except ImportError:
    duckdb = None

SHARE_TOKEN = "YggdBLfdninEJX9"
WEBDAV_BASE = f"https://arquivos.receitafederal.gov.br/public.php/webdav"

CYAN, GREEN, YELLOW, RED, RESET, BOLD = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m", "\033[0m", "\033[1m"
)


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
              "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")

# ─── CNAE → segmento de franquia ──────────────────────────────────────────
# Alinhado com a taxonomia real de scripts/import_franchises.py (os 11
# segmentos do Guia de Franquias Associadas ABF). Cada código foi conferido
# contra a API oficial do IBGE (servicodados.ibge.gov.br/api/v2/cnae/subclasses)
# em 2026-08-14. Lista curada/parcial, não exaustiva.
CNAE_SEGMENT_MAP = {
    "5611203": "alimentacao", "5611201": "alimentacao", "4721102": "alimentacao",
    "4744099": "casa_e_construcao", "4330404": "casa_e_construcao",
    "8593700": "educacao",
    "6209100": "comunicacao_e_tecnologia", "4751201": "comunicacao_e_tecnologia",
    "8230001": "entretenimento_e_lazer",
    "5510801": "hotelaria_e_turismo", "7911200": "hotelaria_e_turismo",
    "8121400": "limpeza_e_conservacao",
    "4781400": "moda",
    "9602501": "saude_beleza_bem_estar", "9602502": "saude_beleza_bem_estar",
    "9313100": "saude_beleza_bem_estar", "8650004": "saude_beleza_bem_estar",
    "4520001": "servicos_automotivos", "4512901": "servicos_automotivos",
    "9609208": "servicos_e_outros_negocios", "7500100": "servicos_e_outros_negocios",
    "9609299": "servicos_e_outros_negocios",
}

# ─── Crosswalk município Receita Federal → ibge_code ─────────────────────

MUNICIPIOS_CSV_URL = "https://www.gov.br/receitafederal/dados/municipios.csv"


def load_municipio_crosswalk() -> dict[str, str]:
    """Retorna {codigo_municipio_tom: ibge_code} a partir do municipios.csv oficial."""
    r = requests.get(MUNICIPIOS_CSV_URL, timeout=60)
    r.raise_for_status()
    text = r.content.decode("latin-1")

    reader = csv.reader(io.StringIO(text), delimiter=";")
    next(reader)  # header

    crosswalk = {}
    for row in reader:
        if len(row) < 2:
            continue
        tom_code, ibge_code = row[0].strip(), row[1].strip()
        crosswalk[tom_code] = ibge_code
    return crosswalk

# ─── Download + extração + agregação, um arquivo por vez ────────────────

def build_file_url(periodo: str, filename: str) -> str:
    return f"{WEBDAV_BASE}/{periodo}/{filename}"


def download_with_retry(url: str, dest_path: str, max_attempts: int = 4) -> None:
    """
    Downloads de vários GB caem no meio por instabilidade de rede (visto na
    prática: IncompleteRead a 68% do último arquivo, depois de processar os
    outros 9 com sucesso). Retry com backoff em vez de perder tudo.
    """
    import time as _time

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, auth=(SHARE_TOKEN, ""), stream=True, timeout=300)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            filename = os.path.basename(dest_path)
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(f"\r    baixando {filename}: {pct:5.1f}% ({downloaded/1e6:.0f}/{total/1e6:.0f}MB)", end="", flush=True)
            print()
            return
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            print(f"\n    tentativa {attempt}/{max_attempts} falhou ({e.__class__.__name__}), tentando de novo...")
            if attempt == max_attempts:
                raise
            _time.sleep(5 * attempt)


def download_and_extract(periodo: str, filename: str, workdir: str) -> str:
    zip_path = os.path.join(workdir, filename)
    url = build_file_url(periodo, filename)
    download_with_retry(url, zip_path)

    with zipfile.ZipFile(zip_path) as z:
        members = z.namelist()
        extracted_name = members[0]
        z.extract(extracted_name, workdir)
    os.remove(zip_path)

    # Os arquivos da Receita Federal vêm em latin-1, não UTF-8 — o DuckDB
    # rejeita byte a byte na primeira linha com acento fora do plano ASCII.
    # Descoberto rodando de verdade (só apareceu na linha 37 de um arquivo
    # de 2.2GB, não numa amostra pequena de teste). Reconverte pra UTF-8 em
    # streaming, sem carregar o arquivo inteiro em memória.
    raw_path = os.path.join(workdir, extracted_name)
    utf8_path = raw_path + ".utf8.csv"
    with open(raw_path, "r", encoding="latin-1", errors="replace") as src, \
         open(utf8_path, "w", encoding="utf-8") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(raw_path)
    return utf8_path


def aggregate_file(con: "duckdb.DuckDBPyConnection", csv_path: str) -> None:
    """Agrega um arquivo extraído na tabela acumuladora `lifecycle_acc` (já criada em con)."""
    cnaes = ", ".join(f"'{c}'" for c in CNAE_SEGMENT_MAP)
    con.execute(f"""
        INSERT INTO lifecycle_acc
        SELECT
            column20 AS municipio_rf,
            column11 AS cnae,
            column05 AS situacao,
            column06 AS data_situacao,
            column10 AS data_inicio
        FROM read_csv('{csv_path}', delim=';', header=false, all_varchar=true, quote='"')
        WHERE column11 IN ({cnaes})
    """)


def finalize_stats(con: "duckdb.DuckDBPyConnection") -> list[dict]:
    """Calcula opened/active/closed/sobrevida/mortalidade a partir do acumulador.

    Usa TRY_STRPTIME (não STRPTIME) porque o DuckDB é vetorizado: uma
    função que lança erro dentro de um CASE WHEN pode ser avaliada pra
    todas as linhas do batch antes do filtro ser aplicado, quebrando em
    datas inválidas ("0", "00000000" etc.) mesmo em linhas que o WHEN
    excluiria. TRY_STRPTIME devolve NULL em vez de estourar.
    """
    rows = con.execute("""
        WITH parsed AS (
            SELECT
                municipio_rf,
                cnae,
                situacao,
                TRY_STRPTIME(data_inicio, '%Y%m%d')::DATE AS dt_inicio,
                TRY_STRPTIME(data_situacao, '%Y%m%d')::DATE AS dt_situacao
            FROM lifecycle_acc
        ),
        with_survival AS (
            SELECT
                *,
                CASE WHEN situacao = '08' AND dt_inicio IS NOT NULL AND dt_situacao IS NOT NULL
                     THEN DATE_DIFF('day', dt_inicio, dt_situacao) END AS survival_days
            FROM parsed
        )
        SELECT
            municipio_rf,
            cnae,
            COUNT(*) AS opened_count,
            COUNT(*) FILTER (WHERE situacao = '02') AS active_count,
            COUNT(*) FILTER (WHERE situacao = '08') AS closed_count,
            AVG(survival_days) / 30.44 AS avg_survival_months,
            (COUNT(*) FILTER (WHERE survival_days IS NOT NULL AND survival_days <= 365)::FLOAT
                / NULLIF(COUNT(*) FILTER (WHERE situacao = '08'), 0)) * 100 AS mortality_rate_1y,
            (COUNT(*) FILTER (WHERE survival_days IS NOT NULL AND survival_days <= 1095)::FLOAT
                / NULLIF(COUNT(*) FILTER (WHERE situacao = '08'), 0)) * 100 AS mortality_rate_3y
        FROM with_survival
        GROUP BY municipio_rf, cnae
    """).fetchall()

    cols = ["municipio_rf", "cnae", "opened_count", "active_count", "closed_count",
            "avg_survival_months", "mortality_rate_1y", "mortality_rate_3y"]
    return [dict(zip(cols, r)) for r in rows]

# ─── Gravação no Supabase ─────────────────────────────────────────────────

def upsert_lifecycle(supabase, stats: list[dict], crosswalk: dict[str, str]) -> tuple[int, int, int]:
    header("Gravando company_lifecycle no Supabase")

    # city_id por ibge_code (pra não fazer 1 query por linha)
    all_ibge_codes = {crosswalk.get(s["municipio_rf"]) for s in stats}
    all_ibge_codes.discard(None)

    city_id_by_ibge: dict[str, str] = {}
    codes = list(all_ibge_codes)
    for i in range(0, len(codes), 500):
        batch = codes[i:i + 500]
        rows = supabase.table("cities").select("id, ibge_code").in_("ibge_code", batch).execute().data
        for r in rows:
            city_id_by_ibge[r["ibge_code"]] = r["id"]

    payload = []
    skipped_no_crosswalk, skipped_no_city = 0, 0
    for s in stats:
        ibge_code = crosswalk.get(s["municipio_rf"])
        if not ibge_code:
            skipped_no_crosswalk += 1
            continue
        city_id = city_id_by_ibge.get(ibge_code)
        if not city_id:
            skipped_no_city += 1
            continue
        payload.append({
            "city_id": city_id,
            "cnae_code": s["cnae"],
            "segment": CNAE_SEGMENT_MAP.get(s["cnae"]),
            "opened_count": s["opened_count"],
            "active_count": s["active_count"],
            "closed_count": s["closed_count"],
            "avg_survival_months": round(s["avg_survival_months"], 1) if s["avg_survival_months"] is not None else None,
            "mortality_rate_1y": round(s["mortality_rate_1y"], 2) if s["mortality_rate_1y"] is not None else None,
            "mortality_rate_3y": round(s["mortality_rate_3y"], 2) if s["mortality_rate_3y"] is not None else None,
            "data_reference_date": datetime.now().strftime("%Y-%m-%d"),
        })

    inserted, errors = 0, 0
    for i in range(0, len(payload), 200):
        batch = payload[i:i + 200]
        try:
            supabase.table("company_lifecycle").upsert(batch, on_conflict="city_id,cnae_code").execute()
            inserted += len(batch)
        except Exception as e:
            errors += len(batch)
            log(f"Erro no lote {i}: {e}", "ERR")

    log(f"{inserted} gravados | {errors} com erro | {skipped_no_crosswalk} sem crosswalk | {skipped_no_city} sem city_id", "OK")
    return inserted, errors, skipped_no_crosswalk + skipped_no_city

# ─── Entry point ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Importa mortalidade/sobrevida de empresas (CNPJ) por cidade+CNAE")
    p.add_argument("--periodo", type=str, required=True, help="Mês do dump, ex: 2026-08")
    p.add_argument("--dry-run", action="store_true", help="Só testa o crosswalk e mostra o plano, não baixa Estabelecimentos")
    p.add_argument("--confirm-big-download", action="store_true",
                   help="Trava de segurança — sem essa flag o script nunca baixa os ~5GB de Estabelecimentos, "
                        "mesmo fora de --dry-run. Só usar depois de combinar isso explicitamente com o usuário.")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if duckdb is None:
        log("duckdb não instalado. Rode: pip install -r requirements.txt", "ERR")
        sys.exit(1)

    header("Crosswalk município Receita Federal -> IBGE")
    crosswalk = load_municipio_crosswalk()
    log(f"{len(crosswalk)} municípios carregados (esperado: 5570-5571)", "OK")

    if args.dry_run:
        header(f"Plano (período {args.periodo})")
        log(f"{len(CNAE_SEGMENT_MAP)} CNAEs filtrados, cobrindo 11 segmentos", "INFO")
        for i in range(10):
            log(build_file_url(args.periodo, f"Estabelecimentos{i}.zip"), "INFO")
        return

    if not args.confirm_big_download:
        log("BLOQUEADO: faltou --confirm-big-download (baixa ~5GB da Receita Federal).", "ERR")
        sys.exit(1)

    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Banco em disco (não :memory:) — se o processo cair no meio (rede,
    # energia, etc.), o que já foi agregado não se perde. `processed_files`
    # deixa rodar de novo pulando os arquivos já feitos.
    db_path = os.path.join(os.path.dirname(__file__), f".cnpj_progress_{args.periodo}.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS lifecycle_acc (municipio_rf TEXT, cnae TEXT, situacao TEXT, data_situacao TEXT, data_inicio TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS processed_files (filename TEXT PRIMARY KEY)")
    already_done = {r[0] for r in con.execute("SELECT filename FROM processed_files").fetchall()}
    if already_done:
        log(f"Retomando: {len(already_done)} arquivo(s) já processado(s) numa execução anterior ({db_path})", "WARN")

    workdir = tempfile.mkdtemp(prefix="rf_cnpj_")
    header(f"Processando Estabelecimentos (período {args.periodo})")
    try:
        for i in range(10):
            filename = f"Estabelecimentos{i}.zip"
            if filename in already_done:
                log(f"[{i+1}/10] {filename} já processado, pulando", "INFO")
                continue
            log(f"[{i+1}/10] Baixando e extraindo {filename}...")
            csv_path = download_and_extract(args.periodo, filename, workdir)
            log(f"[{i+1}/10] Agregando...")
            aggregate_file(con, csv_path)
            con.execute("INSERT INTO processed_files VALUES (?)", [filename])
            os.remove(csv_path)
            log(f"[{i+1}/10] Concluído", "OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    header("Calculando estatísticas finais")
    stats = finalize_stats(con)
    log(f"{len(stats)} combinações cidade+CNAE agregadas", "OK")

    upsert_lifecycle(supabase, stats, crosswalk)
    con.close()
    os.remove(db_path)
    log("Progresso local limpo (importação concluída com sucesso)", "OK")


if __name__ == "__main__":
    main()
