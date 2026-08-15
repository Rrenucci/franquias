"""
api/main.py
API HTTP do Franchise Intelligence — expõe o motor de recomendação
(scripts/recommend.py) e serve o frontend estático.

Uso:
  python -m uvicorn api.main:app --reload --port 8000
Depois abra http://localhost:8000
"""

import os
import sys

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from supabase import create_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import recommend as rec  # noqa: E402  (path precisa ser ajustado antes do import)

SUPABASE_URL = rec.SUPABASE_URL
SUPABASE_KEY = rec.SUPABASE_KEY

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY ausentes no .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Franchise Intelligence API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Rótulo legível pra cada segmento (os slugs vêm de scripts/import_franchises.py)
SEGMENT_LABELS = {
    "alimentacao": "Alimentação",
    "casa_e_construcao": "Casa e Construção",
    "educacao": "Educação",
    "comunicacao_e_tecnologia": "Comunicação, Informática e Eletrônicos",
    "entretenimento_e_lazer": "Entretenimento e Lazer",
    "hotelaria_e_turismo": "Hotelaria e Turismo",
    "limpeza_e_conservacao": "Limpeza e Conservação",
    "moda": "Moda",
    "saude_beleza_bem_estar": "Saúde, Beleza e Bem-Estar",
    "servicos_automotivos": "Serviços Automotivos",
    "servicos_e_outros_negocios": "Serviços e Outros Negócios",
}


class RecommendRequest(BaseModel):
    city_id: str
    investment: int
    segment: str | None = None
    top: int = 10
    save: bool = True


@app.get("/api/segments")
def list_segments():
    return [{"value": v, "label": l} for v, l in SEGMENT_LABELS.items()]


@app.get("/api/cities")
def search_cities(q: str):
    if len(q.strip()) < 2:
        return []
    rows = (
        supabase.table("cities")
        .select("id, name, state_code, population")
        .ilike("name", f"%{q.strip()}%")
        .order("population", desc=True)
        .limit(8)
        .execute()
        .data
    )
    return rows


@app.post("/api/recommend")
def recommend_endpoint(req: RecommendRequest):
    city_row = supabase.table("cities").select("*").eq("id", req.city_id).limit(1).execute().data
    if not city_row:
        raise HTTPException(status_code=404, detail="Cidade não encontrada")
    city = city_row[0]

    if req.investment <= 0:
        raise HTTPException(status_code=400, detail="Investimento precisa ser maior que zero")

    results = rec.recommend(supabase, city, req.investment, req.segment, min(req.top, 50))

    if req.save:
        rec.persist_search(supabase, city, req.investment, req.segment, results)

    return {
        "city": {
            "id": city["id"],
            "name": city["name"],
            "state_code": city["state_code"],
            "population": city.get("population"),
        },
        "investment": req.investment,
        "results": [{
            "name": r["name"],
            "slug": r["slug"],
            "segment": r["segment"],
            "segment_label": SEGMENT_LABELS.get(r["segment"], r["segment"]),
            "logo_url": r.get("logo_url"),
            "brand_url": r.get("brand_url"),
            "investment_min": r["investment_min"],
            "investment_max": r["investment_max"],
            "score_total": r["score_total"],
            "score_investment_fit": r["score_investment_fit"],
            "score_city_demand": r["score_city_demand"],
            "score_purchasing_power": r["score_purchasing_power"],
            "score_survival_risk": r["score_survival_risk"],
        } for r in results],
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
