# main.py — API minimale pour Render + GoHighLevel
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import datetime as dt

app = FastAPI(title="Analyse Devis API (min)")

# CORS ouvert pour tests (tu pourras restreindre plus tard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ex: ["https://*.gohighlevel.com", "https://*.gohighlevel.app"]
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}

@app.post("/api/verify")
async def verify(files: List[UploadFile] = File(...)):
    """
    Reçoit 1..n fichiers sous le champ 'file' (multiples autorisés).
    Retourne un JSON simple par fichier (maquette fonctionnelle).
    """
    results = []
    for f in files:
        # On ne lit pas vraiment le contenu ici: objectif = valider le flux bout-à-bout
        results.append({
            "file": f.filename,
            "raisonSociale": "Démo BTP",
            "siret": "12345678900011",
            "siren": "123456789",
            "naf": "43.39Z",
            "adresse": "10 Rue des Travaux, 75000 Paris",
            "rge": "À vérifier",
            "qualibat": "À vérifier",
            "qualifelec": "—",
            "ca": 450000,
            "resultat": 23000,
            "exercice": "2023-12-31",
            "note": 4.5,
            "nbAvis": 37,
            "scores": {"identification_pct": 100},
            "drapeaux_rouges": [],
        })
    return {"ok": True, "results": results}
