"""
detect_brand_presence.py
Responde a pergunta que motivou o audit de consultoria: "essa franquia
específica JÁ opera nessa cidade?" — não o segmento inteiro (isso já existe
em score_market_gap), a MARCA.

── Por que isso não existia ────────────────────────────────────────────────
A página de detalhe da ABF não expõe localizador de lojas publicamente
(investigado e descartado — ver sql/05 e o audit). O único jeito real de
saber é cruzar o `nome_fantasia` dos estabelecimentos da Receita Federal
contra o nome das 187 franquias, por cidade.

── Reaproveita a infraestrutura de import_cnpj_lifecycle.py ────────────────
Mesmos 10 arquivos de Estabelecimentos (~5GB), mesmo WebDAV, mesmo
crosswalk de município. A diferença: em vez de só agregar contagem por
CNAE, também guarda `nome_fantasia` (filtrado pelas mesmas CNAEs dos 11
segmentos) pra casar contra o catálogo depois.

── Limitações honestas (documentar, não esconder) ─────────────────────────
- `nome_fantasia` vem em branco numa fatia relevante dos estabelecimentos
  — ausência de match NÃO prova ausência da franquia, só que não achamos
  evidência. Por isso a tabela de saída nunca grava "não existe", só grava
  quando ACHA um match.
- Nomes de marca muito curtos/genéricos (< 5 caracteres sem espaço) são
  pulados — o risco de falso positivo (bater com qualquer nome comercial
  parecido) é maior que o valor do dado. Ficam listados no log como
  "não verificado".
- Matching é por regex com borda de palavra sobre o nome normalizado (sem
  acento, maiúsculo, sem sufixo societário tipo LTDA/ME/EIRELI) — variações
  tipo nome_fantasia sem espaço ("CACAUSHOW") podem não bater. Falso
  negativo é o modo de falha aceitável aqui, falso positivo não.

Uso:
  python scripts/detect_brand_presence.py --periodo 2026-08 --dry-run
  python scripts/detect_brand_presence.py --periodo 2026-08 --confirm-big-download
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from datetime import datetime

from dotenv import load_dotenv
from supabase import create_client

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(__file__))
import import_cnpj_lifecycle as base  # reaproveita download/crosswalk/CNAE map

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

try:
    import duckdb
except ImportError:
    duckdb = None

CYAN, GREEN, YELLOW, RED, RESET, BOLD = base.CYAN, base.GREEN, base.YELLOW, base.RED, base.RESET, base.BOLD
log, header = base.log, base.header

MIN_NAME_LEN = 5  # sem espaço — abaixo disso, risco de falso positivo é alto demais

CORPORATE_SUFFIXES = {"LTDA", "ME", "EIRELI", "EPP", "SA", "MEI", "COMERCIO", "COM", "IND", "SERVICOS"}


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    tokens = [t for t in s.split() if t not in CORPORATE_SUFFIXES]
    return " ".join(tokens).strip()

# ─── Acumulador (nome_fantasia, além do que import_cnpj_lifecycle.py já guarda) ─

def aggregate_file_with_names(con: "duckdb.DuckDBPyConnection", csv_path: str) -> None:
    cnaes = ", ".join(f"'{c}'" for c in base.CNAE_SEGMENT_MAP)
    con.execute(f"""
        INSERT INTO brand_acc
        SELECT
            column20 AS municipio_rf,
            column11 AS cnae,
            column05 AS situacao,
            UPPER(regexp_replace(
                regexp_replace(column04, '[^A-Za-z0-9 ]', ' ', 'g'),
                '\\s+', ' ', 'g'
            )) AS nome_fantasia_norm
        FROM read_csv('{csv_path}', delim=';', header=false, all_varchar=true, quote='"')
        WHERE column11 IN ({cnaes}) AND column04 IS NOT NULL AND TRIM(column04) != ''
    """)

# ─── Matching ──────────────────────────────────────────────────────────────

def load_franchises(supabase) -> list[dict]:
    rows = supabase.table("franchises").select("id, name, segment").eq("active", True).execute().data
    out = []
    for r in rows:
        norm = normalize_name(r["name"])
        compact = norm.replace(" ", "")
        if len(compact) < MIN_NAME_LEN:
            log(f"'{r['name']}' — nome curto demais ({compact}), pulando pra evitar falso positivo", "WARN")
            continue
        out.append({**r, "norm": norm})
    return out


def match_franchises(con: "duckdb.DuckDBPyConnection", franchises: list[dict]) -> list[dict]:
    """Pra cada franquia, casa contra brand_acc restrito ao segmento dela.
    Retorna [{franchise_id, municipio_rf, active_count, closed_count, sample}]."""
    results = []
    for i, f in enumerate(franchises, 1):
        segment_cnaes = [c for c, s in base.CNAE_SEGMENT_MAP.items() if s == f["segment"]]
        if not segment_cnaes:
            continue
        cnae_list = ", ".join(f"'{c}'" for c in segment_cnaes)
        pattern = r"(^|[^A-Z0-9])" + re.escape(f["norm"]) + r"([^A-Z0-9]|$)"

        rows = con.execute(f"""
            SELECT municipio_rf, situacao, COUNT(*) AS cnt, ANY_VALUE(nome_fantasia_norm) AS sample
            FROM brand_acc
            WHERE cnae IN ({cnae_list}) AND regexp_matches(nome_fantasia_norm, ?)
            GROUP BY municipio_rf, situacao
        """, [pattern]).fetchall()

        if rows:
            log(f"[{i}/{len(franchises)}] {f['name']}: {len(rows)} combinação(ões) cidade+situação encontradas "
                f"(ex: \"{rows[0][3]}\")", "OK")
        else:
            log(f"[{i}/{len(franchises)}] {f['name']}: nenhuma evidência encontrada", "INFO")

        by_city: dict[str, dict] = {}
        for municipio_rf, situacao, cnt, sample in rows:
            entry = by_city.setdefault(municipio_rf, {"active": 0, "closed": 0, "sample": sample})
            if situacao == "02":
                entry["active"] += cnt
            elif situacao == "08":
                entry["closed"] += cnt

        for municipio_rf, counts in by_city.items():
            results.append({
                "franchise_id": f["id"],
                "municipio_rf": municipio_rf,
                "active_count": counts["active"],
                "closed_count": counts["closed"],
                "sample": counts["sample"],
            })

    return results

# ─── Gravação ──────────────────────────────────────────────────────────────

def upsert_presence(supabase, matches: list[dict], crosswalk: dict[str, str]) -> None:
    header("Gravando franchise_city_presence")

    all_ibge = {crosswalk.get(m["municipio_rf"]) for m in matches}
    all_ibge.discard(None)
    city_id_by_ibge: dict[str, str] = {}
    codes = list(all_ibge)
    for i in range(0, len(codes), 500):
        batch = codes[i:i + 500]
        rows = supabase.table("cities").select("id, ibge_code").in_("ibge_code", batch).execute().data
        for r in rows:
            city_id_by_ibge[r["ibge_code"]] = r["id"]

    payload = []
    skipped = 0
    for m in matches:
        ibge_code = crosswalk.get(m["municipio_rf"])
        city_id = city_id_by_ibge.get(ibge_code) if ibge_code else None
        if not city_id:
            skipped += 1
            continue
        payload.append({
            "franchise_id": m["franchise_id"],
            "city_id": city_id,
            "active_units_matched": m["active_count"],
            "closed_units_matched": m["closed_count"],
            "sample_nome_fantasia": m["sample"],
            "data_reference_date": datetime.now().strftime("%Y-%m-%d"),
        })

    inserted = 0
    for i in range(0, len(payload), 200):
        batch = payload[i:i + 200]
        supabase.table("franchise_city_presence").upsert(batch, on_conflict="franchise_id,city_id").execute()
        inserted += len(batch)

    log(f"{inserted} gravados | {skipped} sem crosswalk/city_id", "OK")

# ─── Entry point ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Detecta presença de marca (franquia específica) por cidade via nome_fantasia da Receita Federal")
    p.add_argument("--periodo", type=str, required=True, help="Mês do dump, ex: 2026-08")
    p.add_argument("--dry-run", action="store_true", help="Só valida franquias/crosswalk, não baixa Estabelecimentos")
    p.add_argument("--confirm-big-download", action="store_true", help="Trava de segurança — baixa ~5GB da Receita Federal")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if duckdb is None:
        log("duckdb não instalado. Rode: pip install -r requirements.txt", "ERR")
        sys.exit(1)

    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    header("Carregando franquias e normalizando nomes")
    franchises = load_franchises(supabase)
    log(f"{len(franchises)} franquias prontas pra matching", "OK")

    header("Crosswalk município Receita Federal -> IBGE")
    crosswalk = base.load_municipio_crosswalk()
    log(f"{len(crosswalk)} municípios carregados", "OK")

    if args.dry_run:
        return

    if not args.confirm_big_download:
        log("BLOQUEADO: faltou --confirm-big-download (baixa ~5GB da Receita Federal).", "ERR")
        sys.exit(1)

    db_path = os.path.join(os.path.dirname(__file__), f".brand_progress_{args.periodo}.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS brand_acc (municipio_rf TEXT, cnae TEXT, situacao TEXT, nome_fantasia_norm TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS processed_files (filename TEXT PRIMARY KEY)")
    already_done = {r[0] for r in con.execute("SELECT filename FROM processed_files").fetchall()}
    if already_done:
        log(f"Retomando: {len(already_done)} arquivo(s) já processado(s)", "WARN")

    workdir = tempfile.mkdtemp(prefix="rf_brand_")
    header(f"Baixando e agregando Estabelecimentos (período {args.periodo})")
    try:
        for i in range(10):
            filename = f"Estabelecimentos{i}.zip"
            if filename in already_done:
                log(f"[{i+1}/10] {filename} já processado, pulando", "INFO")
                continue
            log(f"[{i+1}/10] Baixando e extraindo {filename}...")
            csv_path = base.download_and_extract(args.periodo, filename, workdir)
            log(f"[{i+1}/10] Agregando (com nome_fantasia)...")
            aggregate_file_with_names(con, csv_path)
            con.execute("INSERT INTO processed_files VALUES (?)", [filename])
            os.remove(csv_path)
            log(f"[{i+1}/10] Concluído", "OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    header("Casando nomes de franquia contra nome_fantasia")
    matches = match_franchises(con, franchises)
    log(f"{len(matches)} combinações franquia+cidade com evidência de presença", "OK")

    upsert_presence(supabase, matches, crosswalk)
    con.close()
    os.remove(db_path)
    log("Progresso local limpo (concluído com sucesso)", "OK")


if __name__ == "__main__":
    main()
