# main.py — API "prod lite" : extrait SIRET/SIREN + interroge Annuaire des Entreprises
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
import httpx, re, io, datetime as dt

# --- App & CORS (ouvert pour démarrer ; on pourra restreindre aux domaines GHL ensuite)
app = FastAPI(title="Analyse Devis API – prod lite")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ex: ["https://*.gohighlevel.com", "https://*.gohighlevel.app"]
    allow_methods=["*"],
    allow_headers=["*"],
)

ANN_API = "https://recherche-entreprises.api.gouv.fr/search"
API_TIMEOUT = 8.0

# --- Regex & helpers
SIRET_RE = re.compile(r"\b(?:\d[\s\.-]?){14}\b")
SIREN_RE = re.compile(r"\b(?:\d[\s\.-]?){9}\b")
DIGITS = re.compile(r"\D")

def _digits(s: str) -> str:
    return DIGITS.sub("", s)

def extract_ids(text: str) -> Dict[str, Optional[str]]:
    siret, siren = None, None
    m = SIRET_RE.search(text or "")
    if m:
        siret = _digits(m.group(0))[:14]
        siren = siret[:9]
    if not siren:
        m2 = SIREN_RE.search(text or "")
        if m2:
            siren = _digits(m2.group(0))[:9]
    return {"siret": siret, "siren": siren}

async def read_text(file: UploadFile) -> str:
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

    # Images : pas d'OCR dans cette version "lite"
    return ""

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
    if not hit:
        return {}
    siege = hit.get("etablissement_siege") or {}
    adr = siege.get("adresse") or {}
    if isinstance(adr, dict):
        adr_txt = " ".join(
            str(x) for x in [
                adr.get("numero_voie"), adr.get("type_voie"), adr.get("nom_voie"),
                adr.get("code_postal"), adr.get("commune")
            ] if x
        )
    else:
        adr_txt = str(adr)
    return {
        "raisonSociale": hit.get("nom_complet") or hit.get("nom_raison_sociale"),
        "siren": hit.get("siren"),
        "siret": siege.get("siret") or hit.get("siret"),
        "naf": hit.get("activite_principale") or siege.get("activite_principale"),
        "adresse": adr_txt or None,
        "statut": hit.get("statut_juridique") or hit.get("categorie_entreprise"),
        "categorie": hit.get("categorie_entreprise"),
        "dateCreation": hit.get("date_creation") or hit.get("date_immatriculation"),
    }

@app.get("/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}

@app.post("/api/verify")
async def verify(files: List[UploadFile] = File(...)):
    """
    Reçoit 1..n fichiers sous la clé 'files' (pluriel).
    Retourne une synthèse par fichier avec tentative d'identification + infos Annuaire.
    """
    results = []

    for f in files:
        try:
            text = await read_text(f)
            ids = extract_ids(text)
            ann_hit = None

            if ids.get("siret"):
                ann_hit = await search_annuaire(ids["siret"])
            if not ann_hit and ids.get("siren"):
                ann_hit = await search_annuaire(ids["siren"])
            if not ann_hit:
                # Heuristique simple : première ligne non vide comme requête
                first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
                if len(first) > 3:
                    ann_hit = await search_annuaire(first)

            mapped = map_annuaire(ann_hit) if ann_hit else {}

            # Structure de sortie cohérente avec l'UI
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
                # Champs non traités dans la v1 lite :
                "rge": None, "qualibat": None, "qualifelec": None, "qualitenr": None,
                "ca": None, "resultat": None, "exercice": None,
                "note": None, "nbAvis": None,
                "scores": {"identification_pct": 100 if (ids.get("siret") or ids.get("siren")) else 0},
                "drapeaux_rouges": [] if (ids.get("siret") or ids.get("siren")) else ["SIRET/SIREN introuvable"],
            }
            results.append(out)

        except Exception as e:
            results.append({"file": f.filename, "error": str(e)})

    return {"ok": True, "results": results}
