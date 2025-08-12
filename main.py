# main.py — API Analyse Devis (v3, score global + verdict)
from __future__ import annotations
import io, re, os, json, datetime
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pdfminer.high_level import extract_text
from rapidfuzz import fuzz
from PIL import Image

try:
    import pytesseract
    HAS_TESS = True
except Exception:
    HAS_TESS = False

ALLOWED_ORIGINS = os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",")
app = FastAPI(title="Analyse Devis BTP – v3 (score global + verdict)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CURRENCY_RE = r"(?:€|EUR)"
NUM_RE = r"[0-9][0-9\.\s ,]*"
PCT_RE = r"(?:[0-9]{1,3}(?:[\.,]\d{1,2})?)\s*%"
DATE_RE = r"(?:\d{1,2}[\/\.\-]\d{1,2}[\/\.\-]\d{2,4})"

def load_text_from_file(file: UploadFile) -> str:
    name = (file.filename or "").lower()
    content = file.file.read()
    file.file.seek(0)
    if name.endswith(".txt"):
        return content.decode(errors="ignore")
    if name.endswith(".pdf"):
        with io.BytesIO(content) as buf:
            return extract_text(buf) or ""
    if any(name.endswith(x) for x in (".jpg",".jpeg",".png",".webp")):
        if not HAS_TESS:
            return ""
        try:
            img = Image.open(io.BytesIO(content))
            return pytesseract.image_to_string(img, lang="fra+eng")
        except Exception:
            return ""
    return ""

def norm(s: Optional[str]) -> str:
    return (s or "").strip()

def to_float(raw: str) -> Optional[float]:
    if not raw: return None
    r = raw.replace(" "," ").replace(" ", "").replace(".", "")
    r = r.replace(",", ".")
    try:
        return float(r)
    except Exception:
        return None

def find_total_ttc(text: str) -> Optional[float]:
    patt = re.compile(r"(TOTAL(?:\s+GÉNÉRAL)?\s+TTC|MONTANT\s+TTC|TTC)\D{0,35}("+NUM_RE+")", re.I)
    best = None
    for m in patt.finditer(text):
        raw = m.group(2)
        val = to_float(raw)
        if val is not None:
            best = max(best or 0, val)
    return best

def find_acompte_pct(text: str) -> Optional[float]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"\bacompte\b|\bà la commande\b", line, re.I):
            window = line + " " + (lines[i+1] if i+1 < len(lines) else "")
            m = re.search(PCT_RE, window, re.I)
            if m:
                return to_float(m.group(0).replace("%",""))
    m = re.search(r"(acompte|à la commande)\D{0,30}("+PCT_RE+")", text, re.I)
    if m:
        return to_float(m.group(2).replace("%",""))
    return None

def find_surface_m2(text: str) -> Optional[float]:
    m = re.search(r"("+NUM_RE+")\s*(?:m2|m²|m\^2)\b", text, re.I)
    if not m: return None
    return to_float(m.group(1))

def find_timeline_days(text: str) -> Optional[int]:
    m = re.search(r"(\d{1,3})\s*(jour|jours|semaine|semaines|mois)\b", text, re.I)
    if not m: return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("jour"): return n
    if unit.startswith("sem"): return n*7
    return n*30

def find_decennale(text: str) -> Dict[str, Optional[Any]]:
    present = bool(re.search(r"d[ée]cennale|assurance\s+d[ée]cennale|RC\s*Pro", text, re.I))
    expiry = None
    for m in re.finditer(r"(d[ée]cennale|RC\s*Pro)[\s\S]{0,80}?("+DATE_RE+")", text, re.I):
        expiry = m.group(2)
    months_left = None
    if expiry:
        try:
            parts = re.split(r"[\/\.\-]", expiry)
            if len(parts[2]) == 2:
                parts[2] = "20"+parts[2]
            d = datetime.date(int(parts[2]), int(parts[1]), int(parts[0]))
            today = datetime.date.today()
            months_left = (d.year - today.year)*12 + (d.month - today.month)
        except Exception:
            pass
    return {"present": present, "valid_until": expiry, "months_left": months_left}

def find_iban_country(text: str) -> Dict[str, Optional[Any]]:
    m = re.search(r"\b([A-Z]{2})(\d{2}[A-Z0-9]{10,30})\b", text.replace(" ", ""), re.I)
    if not m: return {"country": None, "is_foreign": None}
    country = m.group(1).upper()
    return {"country": country, "is_foreign": country != "FR"}

def strict_siret_match(found_siret: Optional[str], mapped_siret: Optional[str]) -> bool:
    if not found_siret or not mapped_siret: return False
    return re.sub(r"\D","",found_siret) == re.sub(r"\D","",mapped_siret)

def similarity(a: str, b: str) -> int:
    if not a or not b: return 0
    return int(fuzz.token_sort_ratio(a, b))

def compute_verdict(scores: Dict[str, Any], flags: List[str], info: Dict[str, Any]) -> Dict[str, Any]:
    score = 100
    acompte = scores.get("acompte_pct")
    if acompte is None:
        score -= 5
    elif acompte > 60:
        score -= 25
    elif acompte > 40:
        score -= 15
    elif acompte > 30:
        score -= 5
    if not info.get("decennale_present"):
        score -= 30
    else:
        ml = info.get("decennale_months_left")
        if isinstance(ml, (int, float)) and ml < 6:
            score -= 10
    if info.get("iban_is_foreign") is True:
        score -= 15
    if scores.get("identification_pct",0) < 100:
        score -= 10
    if scores.get("price_per_m2") is None and info.get("surface_m2") is not None:
        score -= 5
    if "Total TTC non détecté" in flags:
        score -= 10
    score = max(0, min(100, int(round(score))))
    if score >= 80:
        risk = "faible"; color = "green"
    elif score >= 60:
        risk = "modéré"; color = "orange"
    else:
        risk = "élevé"; color = "red"
    diag = []
    if acompte is not None and acompte > 40: diag.append(f"Acompte élevé ({int(round(acompte))}%)")
    if not info.get("decennale_present"): diag.append("Décennale non détectée")
    elif isinstance(info.get("decennale_months_left"), (int,float)) and info["decennale_months_left"] < 6: diag.append("Décennale proche d'expiration")
    if info.get("iban_is_foreign") is True: diag.append("IBAN étranger")
    if scores.get("identification_pct",0) < 100: diag.append("Identité incomplète")
    if len(diag) < 3 and scores.get("price_per_m2") is None and info.get("surface_m2") is not None: diag.append("€/m² non calculé")
    diag = diag[:3]
    return {"global_score": score, "risk_level": risk, "color": color, "diagnostics": diag}

class VerifyResponse(BaseModel):
    results: List[Dict[str, Any]]

@app.post("/api/verify", response_model=VerifyResponse)
async def verify(files: List[UploadFile] = File(...)):
    out = []
    for f in files:
        txt = load_text_from_file(f)
        text = txt if isinstance(txt, str) else ""

        total_ttc = find_total_ttc(text)
        acompte_pct = find_acompte_pct(text)
        surface_m2 = find_surface_m2(text)
        price_per_m2 = None
        if total_ttc and surface_m2 and surface_m2 > 0:
            price_per_m2 = round(total_ttc / surface_m2, 2)
        timeline_days = find_timeline_days(text)
        ib = find_iban_country(text)
        dec = find_decennale(text)

        found_siret = None
        m_siret = re.search(r"\b(\d{14})\b", text)
        if m_siret:
            found_siret = m_siret.group(1)
        mapped = {
            "siret": found_siret,
            "siren": found_siret[:-5] if found_siret else None,
            "raisonSociale": None,
            "naf": None,
            "adresse": None,
            "code_postal": None,
            "ville": None,
            "date_creation": None,
        }

        company_age_years = None

        scores = {
            "identification_pct": 100 if found_siret else 0,
            "siret_match": strict_siret_match(found_siret, mapped.get("siret")),
            "name_similarity_pct": 0,
            "address_match": False,
            "naf_coherent": True,
            "acompte_pct": acompte_pct,
            "price_per_m2": price_per_m2
        }

        flags = []
        actions = []
        if ib["is_foreign"]:
            flags.append(f"IBAN étranger détecté ({ib['country']})")
        if acompte_pct is not None and acompte_pct > 40:
            flags.append(f"Acompte élevé ({acompte_pct:.0f} %)")
        if total_ttc is None:
            flags.append("Total TTC non détecté")
        if not dec["present"]:
            flags.append("Attestation d'assurance décennale/RC Pro non détectée")
        else:
            if dec["months_left"] is not None and dec["months_left"] < 6:
                flags.append("Assurance proche d'expiration (< 6 mois)")

        if not dec["present"]:
            actions.append("Fournir attestation d'assurance décennale et RC Pro")
        if acompte_pct is None:
            actions.append("Préciser le pourcentage et les modalités d'acompte")
        if surface_m2 is None:
            actions.append("Préciser la surface (m²) pour normaliser le prix")
        if ib["country"] and ib["is_foreign"]:
            actions.append("Justifier le RIB/IBAN utilisé (pays différent de FR)")

        indicateurs = {
            "company_age_years": company_age_years,
            "decennale_present": dec["present"],
            "decennale_valid_until": dec["valid_until"],
            "decennale_months_left": dec["months_left"],
            "iban_country": ib["country"],
            "iban_is_foreign": ib["is_foreign"],
            "surface_m2": surface_m2,
            "timeline_days": timeline_days,
            "total_ttc": total_ttc,
        }

        verdict = compute_verdict(scores, flags, indicateurs)

        result = {
            "file": f.filename,
            "total_ttc": total_ttc,
            "surface_m2": surface_m2,
            "timeline_days": timeline_days,
            "acompte_pct": acompte_pct,
            "price_per_m2": price_per_m2,
            "siret": mapped["siret"],
            "siren": mapped["siren"],
            "raisonSociale": mapped["raisonSociale"],
            "naf": mapped["naf"],
            "adresse": mapped["adresse"],
            "code_postal": mapped["code_postal"],
            "ville": mapped["ville"],
            "rge": False,
            "rge_details": [],
            "qualibat": None,
            "qualifelec": None,
            "qualitenr": None,
            "ca": None,
            "resultat": None,
            "exercice": None,
            "note": None,
            "nbAvis": None,
            "scores": scores,
            "drapeaux_rouges": flags,
            "actions_suggerees": actions,
            "indicateurs": indicateurs,
            "verdict": verdict
        }
        out.append(result)

    return {"results": out}

@app.get("/health")
def health():
    return {"status":"ok","time":datetime.datetime.utcnow().isoformat()+"Z"}
