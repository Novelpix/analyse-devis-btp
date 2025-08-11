# main.py — API "prod lite++" : Identité + Pappers/Google/RGE (option) + EXTRACT auto (prix, m², délais, acompte, etc.) + Score
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
import os, httpx, re, io, datetime as dt, difflib

APP_NAME = "Analyse Devis API – prod lite++"
ANN_API = "https://recherche-entreprises.api.gouv.fr/search"
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "8"))
ID_STRICT = os.getenv("ID_STRICT", "1") == "1"

# RGE (ADEME Data-Fair) — mettre RGE_QUERY_URL sur Render :
# https://data.ademe.fr/data-fair/api/v1/datasets/liste-des-entreprises-rge-2/lines?format=json&q_mode=simple&size=50&qs=siret:%SIRET%
RGE_QUERY_URL = os.getenv("RGE_QUERY_URL", "").strip()

app = FastAPI(title=APP_NAME)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Extraction bas niveau ----------------
SIRET_RE = re.compile(r"\b(?:\d[\s\.-]?){14}\b")
SIREN_RE = re.compile(r"\b(?:\d[\s\.-]?){9}\b")
DIGITS = re.compile(r"\D")
CP_RE = re.compile(r"\b\d{5}\b")
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}[0-9A-Z]{11,30})\b")
AMOUNT_RE = re.compile(r"(?<!\w)(\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2})?|\d+)\s*(?:€|eur|euro[s]?)?", re.I)
PCT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s?%")
M2_RE = re.compile(r"(\d{1,5}(?:[ \u00A0]\d{3})?(?:[.,]\d{1,2})?)\s?(?:m2|m²|m\^2|metre[s]?\s*carr[ée]s?)", re.I)

LEGAL_SUFFIX = {"sarl","sas","sasu","eurl","sa","sci","snc","scea","ei","eirl","sarl.","sas.","sasu.","sa."}

def _digits(s: str) -> str:
    return DIGITS.sub("", s)

def _fr2float(s: Optional[str]) -> Optional[float]:
    if not s: return None
    s = s.replace("\u00A0"," ").replace(" ", "")
    s = re.sub(r"[^\d,\.]", "", s)
    if s.count(",") > 1 and "." in s:  # cas bizarre -> nettoyage
        s = s.replace(",", "")
    elif s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def extract_ids(text: str) -> Dict[str, Optional[str]]:
    siret = None; siren = None
    if m := SIRET_RE.search(text or ""):
        siret = _digits(m.group(0))[:14]; siren = siret[:9]
    if not siren and (m := SIREN_RE.search(text or "")):
        siren = _digits(m.group(0))[:9]
    return {"siret": siret, "siren": siren}

async def read_text(file: UploadFile) -> str:
    name = (file.filename or "").lower()
    kind = (file.content_type or "").lower()
    raw = await file.read()

    # TXT
    if kind.startswith("text/") or name.endswith(".txt"):
        return raw.decode("utf-8", errors="ignore")

    # PDF (texte)
    if kind == "application/pdf" or name.endswith(".pdf"):
        try:
            from pdfminer.high_level import extract_text
            return extract_text(io.BytesIO(raw)) or ""
        except Exception:
            return ""

    # Images (JPG/PNG) : OCR à ajouter plus tard (Docker/Tesseract)
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
    return difflib.SequenceMatcher(None, a1, b1).ratio()

def _postal(text: Optional[str]) -> Optional[str]:
    if not text: return None
    m = CP_RE.search(text); return m.group(0) if m else None

def _naf_is_construction(naf: Optional[str]) -> bool:
    if not naf: return False
    naf = str(naf); return naf.startswith(("41","42","43"))

# ---------------- Annuaire ----------------
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
    if not hit: return {}
    siege = hit.get("siege") or hit.get("etablissement_siege") or {}
    adr = siege.get("adresse") or {}
    if isinstance(adr, dict):
        adr_txt = " ".join(str(x) for x in [adr.get("numero_voie"), adr.get("type_voie"),
                                            adr.get("nom_voie"), adr.get("code_postal"), adr.get("commune")] if x)
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

# ---------------- Pappers (option) ----------------
async def fetch_pappers_finance(siren: str) -> Optional[Dict[str, Any]]:
    token = os.getenv("PAPPERS_API_KEY")
    if not token or not siren: return None
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            r = await client.get("https://api.pappers.fr/v2/entreprise",
                                 params={"siren": siren, "api_token": token, "champs": "comptes"})
            r.raise_for_status()
            d = r.json() or {}
            comptes = d.get("comptes") or []
            if not comptes: return None
            last = sorted(comptes, key=lambda x: x.get("date_cloture_exercice") or "")[-1]
            return {"ca": last.get("chiffre_affaires"),
                    "resultat": last.get("resultat_net"),
                    "exercice": last.get("date_cloture_exercice")}
    except Exception:
        return None

# ---------------- Google (option) ----------------
async def fetch_google_reviews(name: Optional[str], address: Optional[str]) -> Optional[Dict[str, Any]]:
    key = os.getenv("GOOGLE_API_KEY")
    if not key or not name: return None
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            q = f"{name} {address}" if address else name
            r1 = await client.get("https://maps.googleapis.com/maps/api/place/textsearch/json",
                                  params={"query": q, "key": key, "language": "fr", "region": "fr"})
            r1.raise_for_status()
            res = (r1.json() or {}).get("results") or []
            if not res: return None
            pid = res[0].get("place_id")
            if not pid: return None
            r2 = await client.get("https://maps.googleapis.com/maps/api/place/details/json",
                                  params={"place_id": pid, "key": key, "fields": "rating,user_ratings_total", "language": "fr"})
            r2.raise_for_status()
            d = (r2.json() or {}).get("result") or {}
            return {"note": d.get("rating"), "nbAvis": d.get("user_ratings_total")}
    except Exception:
        return None

# ---------------- RGE (ADEME) ----------------
def _pick(d: dict, names):
    for n in names:
        if n in d and d[n]:
            return str(d[n]).strip()
    return None

async def fetch_rge_by_siret(siret: Optional[str]) -> Optional[list]:
    if not siret or not RGE_QUERY_URL: return None
    url = RGE_QUERY_URL.replace("%SIRET%", siret)
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            r = await client.get(url); r.raise_for_status()
            data = r.json() or {}
        rows = data.get("results") or data.get("data") or []
        out = []
        for row in rows:
            rec = row.get("fields", row)
            item = {
                "domaine":  _pick(rec, ["domaine_travaux", "domaine", "libelle_domaine", "qualification"]),
                "organisme":_pick(rec, ["organisme", "certificateur", "organisme_certification"]),
                "debut":    _pick(rec, ["date_debut", "date_debut_validite", "date_obtention"]),
                "fin":      _pick(rec, ["date_fin", "date_fin_validite", "date_validite"]),
            }
            if any(item.values()): out.append(item)
        return out or None
    except Exception:
        return None

# ---------------- EXTRACT "devis" (prix, m², délais, acompte, etc.) ----------
def _line_iter(text: str):
    for raw in (text or "").splitlines():
        yield raw.strip()

def _find_amount_near(text: str, keywords: List[str]) -> Optional[float]:
    lines = list(_line_iter(text))
    vals = []
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(k in low for k in keywords):
            # cherche montants sur la ligne et la suivante
            scope = ln + " " + (lines[i+1] if i+1 < len(lines) else "")
            for m in AMOUNT_RE.finditer(scope):
                v = _fr2float(m.group(1))
                if v is not None: vals.append(v)
    if vals:
        # on prend le plus grand (souvent le total)
        return max(vals)
    return None

def _find_pct_near(text: str, keywords: List[str]) -> Optional[float]:
    for ln in _line_iter(text):
        low = ln.lower()
        if any(k in low for k in keywords):
            m = PCT_RE.search(ln)
            if m:
                return _fr2float(m.group(1))
    return None

def _find_surface(text: str) -> Optional[float]:
    cands = []
    for m in M2_RE.finditer(text):
        v = _fr2float(m.group(1))
        if v is not None and 1 <= v <= 100000:
            cands.append(v)
    return max(cands) if cands else None

def _find_delai_jours(text: str) -> Optional[int]:
    # "délai 10 jours", "sous 3 semaines", "durée : 2 mois"
    pat = re.compile(r"(?:d[ée]lai[s]?|dur[ée]e|intervention|r[ée]alisation).{0,40}?(\d{1,3})\s?(jours?|j|semaines?|mois)", re.I)
    best = None
    for m in pat.finditer(text):
        n = int(m.group(1)); unit = m.group(2).lower()
        if unit.startswith("jour") or unit == "j": days = n
        elif unit.startswith("semaine"): days = n*7
        else: days = n*30
        best = max(best or 0, days)
    return best

def _find_acompte_pct(text: str) -> Optional[float]:
    # "acompte 30%", "à la commande : 40 %", "avance 20 %"
    kw = ("acompte","à la commande","avance")
    for ln in _line_iter(text):
        low = ln.lower()
        if any(k in low for k in kw):
            m = PCT_RE.search(ln)
            if m: return _fr2float(m.group(1))
    return None

def _find_decennale(text: str) -> bool:
    return bool(re.search(r"d[ée]cennale|assurance\s+d[ée]cennale", text, re.I))

def _inclusion(text: str, word: str) -> Optional[bool]:
    # vrai si mot présent sans "non compris/exclu" proche
    if not re.search(word, text, re.I): return None
    # regarde si "non compris" à proximité (±50 caractères)
    pat = re.compile(r"(non\s+compris|exclu|hors\s+prix)", re.I)
    for m in re.finditer(word, text, re.I):
        start = max(0, m.start()-50); end = m.end()+50
        if pat.search(text[start:end]): return False
    return True

def extract_devis_info(text: str) -> Dict[str, Any]:
    ttc = _find_amount_near(text, ["ttc","t.t.c","toutes taxes comprises","montant total","total général"])
    ht = _find_amount_near(text, ["ht","h.t","hors taxes"])
    tva_pct = _find_pct_near(text, ["tva","taxe sur la valeur ajout", "taux tva"])
    surface = _find_surface(text)
    delai = _find_delai_jours(text)
    acompte = _find_acompte_pct(text)
    iban = None; iban_country = None
    m = IBAN_RE.search(text)
    if m:
        iban = m.group(1)
        iban_country = iban[:2]
    decennale = _find_decennale(text)
    inc_depose = _inclusion(text, r"d[ée]pose|[ée]vacuation|gravats")
    inc_echaf  = _inclusion(text, r"[ée]chafaudage")
    inc_finit  = _inclusion(text, r"finition[s]?")
    inc_sav    = _inclusion(text, r"\bSAV\b|service\s+apr[èe]s\s+vente|garantie")
    return {
        "total_ttc": ttc,
        "total_ht": ht,
        "tva_pct": tva_pct,
        "surface_m2": surface,
        "delai_jours": delai,
        "acompte_pct": acompte,
        "iban": iban,
        "iban_country": iban_country,
        "decennale": bool(decennale),
        "inclusions": {
            "depose_evac": inc_depose,
            "echafaudage": inc_echaf,
            "finitions": inc_finit,
            "sav": inc_sav,
        }
    }

# ---------------- Endpoints ----------------
@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME, "endpoints": ["/health", "/api/verify"]}

@app.get("/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}

@app.post("/api/verify")
async def verify(files: List[UploadFile] = File(...)):
    results = []
    for f in files:
        try:
            text = await read_text(f)
            ids = extract_ids(text)
            ann_hit = None

            if ids.get("siret"): ann_hit = await search_annuaire(ids["siret"])
            if not ann_hit and ids.get("siren"): ann_hit = await search_annuaire(ids["siren"])
            if not ann_hit:
                first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
                if len(first) > 3: ann_hit = await search_annuaire(first)

            mapped = map_annuaire(ann_hit) if ann_hit else {}

            # Optionnels
            fin = await fetch_pappers_finance(mapped.get("siren") or ids.get("siren")) or {}
            g = await fetch_google_reviews(mapped.get("raisonSociale"), mapped.get("adresse")) or {}
            siret_final = ids.get("siret") or mapped.get("siret")
            rge_items = await fetch_rge_by_siret(siret_final)

            # ---- Score d'identification
            pts = 0
            siret_match = bool(siret_final and mapped.get("raisonSociale"))
            first_line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
            sim = _sim_name(first_line, mapped.get("raisonSociale"))
            name_similarity_pct = int(round(sim * 100))
            cp_text = _postal(text); cp_addr = _postal(mapped.get("adresse"))
            address_match = bool(cp_text and cp_addr and cp_text == cp_addr)
            naf_coherent = _naf_is_construction(mapped.get("naf"))
            if ID_STRICT and siret_match:
                identification_pct = 100
            else:
                if siret_match: pts += 60
                pts += int(round(sim * 15))
                if address_match: pts += 15
                if naf_coherent: pts += 10
                identification_pct = max(0, min(100, pts))

            # ---- Extract devis
            extr = extract_devis_info(text)
            # calcul €/m2 côté API aussi (pratique)
            eurm2 = None
            if extr.get("total_ttc") and extr.get("surface_m2"):
                try:
                    eurm2 = round(float(extr["total_ttc"]) / max(1.0, float(extr["surface_m2"])), 2)
                except Exception:
                    eurm2 = None

            out = {
                "file": f.filename,
                # Identité
                "raisonSociale": mapped.get("raisonSociale"),
                "siret": siret_final,
                "siren": ids.get("siren") or mapped.get("siren"),
                "naf": mapped.get("naf"),
                "adresse": mapped.get("adresse"),
                "statut": mapped.get("statut"),
                "dateCreation": mapped.get("dateCreation"),
                "categorie": mapped.get("categorie"),
                # RGE
                "rge": bool(rge_items),
                "rge_details": rge_items,
                # Finances publiques
                "ca": fin.get("ca"),
                "resultat": fin.get("resultat"),
                "exercice": fin.get("exercice"),
                # Avis
                "note": g.get("note"),
                "nbAvis": g.get("nbAvis"),
                # EXTRACT auto devis
                "total_ttc": extr.get("total_ttc"),
                "total_ht": extr.get("total_ht"),
                "tva_pct": extr.get("tva_pct"),
                "surface_m2": extr.get("surface_m2"),
                "prix_m2": eurm2,
                "delai_jours": extr.get("delai_jours"),
                "acompte_pct": extr.get("acompte_pct"),
                "iban_country": extr.get("iban_country"),
                "decennale": extr.get("decennale"),
                "inclusions": extr.get("inclusions"),
                # Scores + Alertes
                "scores": {
                    "identification_pct": identification_pct,
                    "siret_match": siret_match,
                    "name_similarity_pct": name_similarity_pct,
                    "address_match": address_match,
                    "naf_coherent": naf_coherent
                },
                "drapeaux_rouges": [] if (siret_final or (ids.get("siren") or mapped.get("siren"))) else ["SIRET/SIREN introuvable"],
            }
            # Drapeaux additionnels utiles
            if extr.get("iban_country") and extr["iban_country"] != "FR":
                out["drapeaux_rouges"].append(f"IBAN non FR ({extr['iban_country']})")
            if out.get("total_ttc") is None:
                out["drapeaux_rouges"].append("Total TTC non détecté")
            results.append(out)
        except Exception as e:
            results.append({"file": f.filename, "error": str(e)})

    return {"ok": True, "results": results}
