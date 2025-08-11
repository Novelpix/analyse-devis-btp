# main.py — API "prod lite+" : SIRET/SIREN + Annuaire + Pappers (opt) + Google (opt) + RGE ADEME (opt) + Score pondéré
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
import os, httpx, re, io, datetime as dt, difflib

APP_NAME = "Analyse Devis API – prod lite+"
ANN_API = "https://recherche-entreprises.api.gouv.fr/search"
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "8"))

# ⬇️ RGE via Data-Fair (mettre la variable d'env RGE_QUERY_URL sur Render)
# Exemple :
# https://data.ademe.fr/data-fair/api/v1/datasets/liste-des-entreprises-rge-2/lines?format=json&q_mode=simple&size=50&qs=siret:%SIRET%
RGE_QUERY_URL = os.getenv("RGE_QUERY_URL", "").strip()

app = FastAPI(title=APP_NAME)

# CORS (ouvert pour démarrer ; restreindre aux domaines GHL ensuite)
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
CP_RE = re.compile(r"\b\d{5}\b")
LEGAL_SUFFIX = {"sarl","sas","sasu","eurl","sa","sci","snc","scea","ei","eirl","sarl.","sas.","sasu.","sa."}

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

# ---------------- Scoring helpers ----------------
def _norm(s: Optional[str]) -> str:
    if not s: return ""
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE).lower()
    toks = [t for t in s.split() if t and t not in LEGAL_SUFFIX]
    return " ".join(toks)

def _sim_name(a: Optional[str], b: Optional[str]) -> float:
    a1, b1 = _norm(a), _norm(b)
    if not a1 or not b1: return 0.0
    return difflib.SequenceMatcher(None, a1, b1).ratio()  # 0..1

def _postal(text: Optional[str]) -> Optional[str]:
    if not text: return None
    m = CP_RE.search(text)
    return m.group(0) if m else None

def _naf_is_construction(naf: Optional[str]) -> bool:
    if not naf: return False
    naf = str(naf)
    return naf.startswith(("41","42","43"))  # Construction

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
    """Lit 'siege' OU 'etablissement_siege' et reconstruit l’adresse."""
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
                params={"query": q, "key": key, "language": "fr", "region": "fr"},
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
                params={"place_id": pid, "key": key, "fields": "rating,user_ratings_total", "language": "fr"},
            )
            r2.raise_for_status()
            d = (r2.json() or {}).get("result") or {}
            return {"note": d.get("rating"), "nbAvis": d.get("user_ratings_total")}
    except Exception:
        return None

# ---------------- RGE (ADEME Data-Fair) – optionnel ----------------
def _pick(d: dict, names):
    for n in names:
        if n in d and d[n]:
            return str(d[n]).strip()
    return None

async def fetch_rge_by_siret(siret: Optional[str]) -> Optional[list]:
    """Interroge le dataset ADEME via Data-Fair par SIRET si RGE_QUERY_URL est défini."""
    if not siret or not RGE_QUERY_URL:
        return None
    url = RGE_QUERY_URL.replace("%SIRET%", siret)
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json() or {}
        rows = data.get("results") or data.get("data") or []
        out = []
        for row in rows:
            rec = row.get("fields", row)  # Data-Fair met souvent sous "fields"
            item = {
                "domaine":  _pick(rec, ["domaine_travaux", "domaine", "libelle_domaine", "qualification"]),
                "organisme":_pick(rec, ["organisme", "certificateur", "organisme_certification"]),
                "debut":    _pick(rec, ["date_debut", "date_debut_validite", "date_obtention"]),
                "fin":      _pick(rec, ["date_fin", "date_fin_validite", "date_validite"]),
            }
            if any(item.values()):
                out.append(item)
        return out or None
    except Exception:
        return None

# ---------------- Endpoints ----------------
@app.get("/")
def root():
    return {
        "ok": True,
        "service": APP_NAME,
        "endpoints": ["/health", "/api/verify"],
    }

@app.get("/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}

@app.post("/api/verify")
async def verify(files: List[UploadFile] = File(...)):
    """
    Reçoit 1..n fichiers sous la clé 'files' (pluriel).
    Retourne une synthèse par fichier : identité + NAF + adresse (+ options) + score pondéré.
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

            # RGE (si RGE_QUERY_URL présent)
            siret_final = ids.get("siret") or mapped.get("siret")
            rge_items = await fetch_rge_by_siret(siret_final)

            # ---- Score d'identification pondéré (0..100)
            pts = 0
            siret_match = bool(siret_final and mapped.get("raisonSociale"))
            if siret_match:
                pts += 60  # SIRET trouvé + match Annuaire

            first_line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
            sim = _sim_name(first_line, mapped.get("raisonSociale"))
            name_similarity_pct = int(round(sim * 100))
            pts += int(round(sim * 15))  # similarité nom (≤15)

            cp_text = _postal(text)
            cp_addr = _postal(mapped.get("adresse"))
            address_match = bool(cp_text and cp_addr and cp_text == cp_addr)
            if address_match:
                pts += 15  # CP concordant

            naf_coherent = _naf_is_construction(mapped.get("naf"))
            if naf_coherent:
                pts += 10  # NAF 41/42/43

            identification_pct = max(0, min(100, pts))

            out = {
                "file": f.filename,
                "raisonSociale": mapped.get("raisonSociale"),
                "siret": siret_final,
                "siren": ids.get("siren") or mapped.get("siren"),
                "naf": mapped.get("naf"),
                "adresse": mapped.get("adresse"),
                "statut": mapped.get("statut"),
                "dateCreation": mapped.get("dateCreation"),
                "categorie": mapped.get("categorie"),
                "rge": bool(rge_items),
                "rge_details": rge_items,
                "qualibat": None,
                "qualifelec": None,
                "qualitenr": None,
                "ca": fin.get("ca"),
                "resultat": fin.get("resultat"),
                "exercice": fin.get("exercice"),
                "note": g.get("note"),
                "nbAvis": g.get("nbAvis"),
                "scores": {
                    "identification_pct": identification_pct,
                    "siret_match": siret_match,
                    "name_similarity_pct": name_similarity_pct,
                    "address_match": address_match,
                    "naf_coherent": naf_coherent
                },
                "drapeaux_rouges": [] if (siret_final or (ids.get("siren") or mapped.get("siren"))) else ["SIRET/SIREN introuvable"],
            }
            results.append(out)
        except Exception as e:
            results.append({"file": f.filename, "error": str(e)})

    return {"ok": True, "results": results}
