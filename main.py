# main.py — API "prod lite" : extrait SIRET/SIREN + interroge Annuaire des Entreprises
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
import os, httpx, re, io, datetime as dt

APP_NAME = "Analyse Devis API – prod lite"
ANN_API = "https://recherche-entreprises.api.gouv.fr/search"
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "8"))

app = FastAPI(title=APP_NAME)

# CORS ouvert pour démarrer (tu pourras restreindre à ton domaine GHL plus tard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ex.: ["https://*.gohighlevel.com","https://*.gohighlevel.app"]
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Helpers extraction ----------------
SIRET_RE = re.compile(r"\b(?:\d[\s\.-]?){14}\b")
SIREN_RE = re.compile(r"\b(?:\d[\s\.-]?){9}\b")
DIGITS = re.compile(r"\D")

def _digits(s: str) -> str:
    return DIGITS.sub("", s)

def extract_ids(text: str) -> Dict[str, Optional[str]]:
    siret = None
    siren = None
    if m := SIRET_RE.search(text or ""):
        siret = _digits(m.group(0))[:14]
        siren = siret[:9]
    if not siren and (m := SIREN_RE.search(text or "")):
        siren = _digits(m.group(0))[:9]
    return {"siret": siret, "siren": siren}

async def read_text(file: UploadFile) -> str:
    """Lit le contenu en texte (TXT direct, PDF via pdfminer, pas d’OCR dans cette v1)."""
    name = (file.filename or "").lower()
    kind = (file.content_type or "").lower()
    raw = await file.read()

    # TXT
    if kind.startswith("text/") or name.endswith(".txt"):
        return raw.decode("utf-8", errors="ignore")

    # PDF
    if kind == "application/pdf" or name.endswith(".pdf"):
        try:
            from pdfminer.high_level import extract_text
            return extract_text(io.BytesIO(raw)) or ""
        except Exception:
            return ""

    # Images (JPG/PNG) : OCR non activé ici
    return ""

# ---------------- Annuaire des Entreprises ----------------
async def search_annuaire(query: str) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            r = await client.get(ANN_API, params={"q": query})
            r.raise_for_status()
            hits = (r.json() or {}).get("results") or []
            return hits[0] if hits else None
    except Exception:
        return None

def map_annuaire(hit: Dict[str, Any]) -> Dict[str, Any]:
    """Corrigé : lit 'siege' OU 'etablissement_siege' et reconstruit l’adresse."""
    if not hit:
        return {}
    siege = hit.get("siege") or hit.get("etablissement_siege") or {}
    adr = siege.get("adresse") or {}
    if isinstance(adr, dict):
        adr_txt = " ".join(
            str(x) for x in [
                adr.get("numero_voie"), adr.get("type_voie"), adr.get("nom_voie"),
                adr.get("code_postal"), adr.get("commune")
            ] if x
        )
    else:
        adr_txt = str(adr) if adr else None

    return {
        "raisonSociale": hit.get("nom_complet") or hit.get("nom_raison_sociale"),
        "siren": hit.get("siren"),
        "siret": siege.get("siret") or hit.get("siret"),
        "naf": hit.get("activite_principale") or siege.get("activite_principale"),
        "adresse": adr_txt,
        "statut": hit.get("statut_juridique") or hit.get("categorie_entreprise"),
        "categorie": hit.get("categorie_entreprise"),
        "dateCreation": hit.get("date_creation") or hit.get("date_immatriculation"),
    }

# ---------------- Finances (Pappers) – optionnel ----------------
async def fetch_pappers_finance(siren: str) -> Optional[Dict[str, Any]]:
    token = os.getenv("PAPPERS_API_KEY")
    if not token or not siren:
        return None
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            r = await client.get(
                "https://api.pappers.fr/v2/entreprise",
                params={"siren": siren, "api_token": token, "champs": "comptes"},
            )
            r.raise_for_status()
            d = r.json() or {}
            comptes = d.get("comptes") or []
            if not comptes:
                return None
            last = sorted(comptes, key=lambda x: x.get("date_cloture_exercice") or "")[-1]
            return {
                "ca": last.get("chiffre_affaires"),
                "resultat": last.get("resultat_net"),
                "exercice": last.get("date_cloture_exercice"),
            }
    except Exception:
        return None

# ---------------- Avis Google – optionnel ----------------
async def fetch_google_reviews(name: Optional[str], address: Optional[str]) -> Optional[Dict[str, Any]]:
    key = os.getenv("GOOGLE_API_KEY")
    if not key or not name:
        return None
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            q = f"{name} {address}" if address else name
            r1 = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": q, "key": key},
            )
            r1.raise_for_status()
            res = (r1.json() or {}).get("results") or []
            if not res:
                return None
            pid = res[0].get("place_id")
            if not pid:
                return None
            r2 = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={"place_id": pid, "key": key, "fields": "rating,user_ratings_total"},
            )
            r2.raise_for_status()
            d = (r2.json() or {}).get("result") or {}
            return {"note": d.get("rating"), "nbAvis": d.get("user_ratings_total")}
    except Exception:
        return None

# ---------------- Endpoints ----------------
@app.get("/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}

@app.post("/api/verify")
async def verify(files: List[UploadFile] = File(...)):
    """
    Reçoit 1..n fichiers sous la clé 'files' (pluriel).
    Retourne une synthèse par fichier : identité + NAF + adresse (+ options).
    """
    results = []
    for f in files:
        try:
            text = await read_text(f)
            ids = extract_ids(text)
            ann_hit = None

            # Recherche par SIRET/SIREN, puis fallback sur 1ère ligne non vide
            if ids.get("siret"):
                ann_hit = await search_annuaire(ids["siret"])
            if not ann_hit and ids.get("siren"):
                ann_hit = await search_annuaire(ids["siren"])
            if not ann_hit:
                first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
                if len(first) > 3:
                    ann_hit = await search_annuaire(first)

            mapped = map_annuaire(ann_hit) if ann_hit else {}

            # Optionnels (pas d’erreur si pas de clés API)
            fin = await fetch_pappers_finance(mapped.get("siren") or ids.get("siren")) or {}
            g = await fetch_google_reviews(mapped.get("raisonSociale"), mapped.get("adresse")) or {}

            out = {
                "file": f.filename,
                "raisonSociale": mapped.get("raisonSociale"),
                "siret": ids.get("siret") or mapped.get("siret"),
                "siren": ids.get("siren") or mapped.get("siren"),
                "naf": mapped.get("naf"),
                "adresse": mapped.get("adresse"),
                "statut": mapped.get("statut"),
                "dateCreation": mapped.get("dateCreation"),
                "categorie": mapped.get("categorie"),
                "rge": None,
                "qualibat": None,
                "qualifelec": None,
                "qualitenr": None,
                "ca": fin.get("ca"),
                "resultat": fin.get("resultat"),
                "exercice": fin.get("exercice"),
                "note": g.get("note"),
                "nbAvis": g.get("nbAvis"),
                "scores": {"identification_pct": 100 if (ids.get("siret") or ids.get("siren")) else 0},
                "drapeaux_rouges": [] if (ids.get("siret") or ids.get("siren")) else ["SIRET/SIREN introuvable"],
            }
            results.append(out)
        except Exception as e:
            results.append({"file": f.filename, "error": str(e)})

    return {"ok": True, "results": results}
