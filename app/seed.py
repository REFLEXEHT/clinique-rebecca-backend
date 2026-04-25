"""
seed.py — Données initiales Clinique de la Rebecca
Inclut : patients réels, médecins, tarifs labo, dentisterie, pharmacie
"""
from app.database import SessionLocal
from app.auth import get_password_hash
import app.models as models
import logging
import pandas as pd
import os

logger = logging.getLogger(__name__)


def seed_database():
    """Appelé au démarrage via main.py lifespan."""
    db = SessionLocal()
    try:
        _seed_horaires(db)
        _seed_regles_partage(db)
        _seed_contrat_optometrie(db)
        _seed_tarifs_config(db)
        _seed_medecins_specialistes(db)
        _seed_tarifs_labo(db)
        _seed_tarifs_dentiste(db)
        _seed_gestes_medicaux(db)
        _seed_tarifs_medecins(db)
        db.commit()
        logger.info("✅ Seed terminé")
    except Exception as e:
        db.rollback()
        logger.error("❌ Erreur seed: %s", e)
    finally:
        db.close()


# ─── Horaires ─────────────────────────────────────────────────────────────────
def _seed_horaires(db):
    jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    for j in jours:
        if not db.query(models.Horaire).filter(models.Horaire.jour == j).first():
            hf = "15:00" if j == "Dimanche" else "17:00"
            db.add(models.Horaire(jour=j, ouvert=True, heure_ouverture="07:00", heure_fermeture=hf))


# ─── Règles de répartition ────────────────────────────────────────────────────
def _seed_regles_partage(db):
    if db.query(models.ReglePartage).count() > 0:
        return
    regles = [
        ("investisseur","consultation",70,30),
        ("investisseur","geste",80,20),
        ("investisseur","chirurgie",0,100),
        ("investisseur","hospit",70,30),
        ("affilie","consultation",60,40),
        ("affilie","geste",70,30),
        ("affilie","chirurgie",0,100),
        ("affilie","hospit",60,40),
        ("exploitant","consultation",100,0),
        ("exploitant","geste",100,0),
        ("exploitant","chirurgie",100,0),
        ("investisseur_exploitant","consultation",100,0),
        ("investisseur_exploitant","geste",100,0),
        ("investisseur_exploitant","chirurgie",100,0),
    ]
    for tm, ta, pm, pc in regles:
        db.add(models.ReglePartage(
            type_medecin=tm, type_acte=ta,
            pct_medecin=pm, pct_clinique=pc
        ))


# ─── Contrat optométrie ────────────────────────────────────────────────────────
def _seed_contrat_optometrie(db):
    if not db.query(models.ContratOptometrie).first():
        db.add(models.ContratOptometrie(
            pct_consultation=35.0,
            pct_montures=13.0,
            minimum_mensuel_usd=300.0,
            taux_usd_htg=130.0,
        ))


# ─── Tarifs configurables (loyers, etc.) ──────────────────────────────────────
def _seed_tarifs_config(db):
    tarifs = [
        ("loyer_dentisterie",  "Loyer Dentisterie (mensuel)",        0, "mois"),
        ("loyer_laboratoire",  "Loyer Laboratoire (mensuel)",        0, "mois"),
        ("loyer_physio",       "Loyer Physiothérapie (mensuel)",     0, "mois"),
        ("loyer_optometrie",   "Loyer min. Optométrie (USD/mois)",   300, "mois"),
        ("forfait_hospit_jr",  "Chambre hospitalisation (par jour)", 0, "jour"),
        ("frais_dossier",      "Frais dossier patient",              0, "HTG"),
    ]
    for code, libelle, montant, unite in tarifs:
        if not db.query(models.TarifClinic).filter(models.TarifClinic.code == code).first():
            db.add(models.TarifClinic(code=code, libelle=libelle, montant=montant, unite=unite))


# ─── Médecins → Spécialistes visibles sur le site ─────────────────────────────
def _seed_medecins_specialistes(db):
    from app.seed_data import MEDECINS

    for idx, med in enumerate(MEDECINS):
        # Vérifier si le spécialiste existe déjà
        existing = db.query(models.Specialiste).filter(
            models.Specialiste.nom == med["nom"]
        ).first()
        if existing:
            continue

        spec = models.Specialiste(
            nom=med["nom"],
            specialite=med["specialite"],
            telephone=med.get("telephone", ""),
            emoji="👨‍⚕️",
            categorie="tous",
            actif=True,
            ordre=idx,
            description=f"Consultation : {med['prix_consultation']:,} HTG | RDV : {med['prix_rdv']:,} HTG",
        )
        db.add(spec)

        # Créer le profil médecin pour la comptabilité
        existing_profil = db.query(models.ProfilMedecin).filter(
            models.ProfilMedecin.nom == med["nom"]
        ).first()
        if not existing_profil:
            type_med = med.get("type_medecin", "affilie")
            try:
                type_enum = models.TypeMedecinEnum(type_med)
            except:
                type_enum = models.TypeMedecinEnum.affilie
            db.add(models.ProfilMedecin(
                nom=med["nom"],
                specialite=med["specialite"],
                type_medecin=type_enum,
                loyer_mensuel_htg=0,
            ))


# ─── Tarifs laboratoire ───────────────────────────────────────────────────────
def _seed_tarifs_labo(db):
    from app.seed_data import TARIFS_LABO

    for tarif in TARIFS_LABO:
        existing = db.query(models.TarifLabo).filter(
            models.TarifLabo.code == tarif["code"]
        ).first()
        if not existing:
            db.add(models.TarifLabo(
                code=tarif["code"],
                libelle=tarif["libelle"],
                montant=tarif["montant"],
                devise="HTG",
                actif=True,
            ))


# ─── Tarifs dentisterie ───────────────────────────────────────────────────────
def _seed_tarifs_dentiste(db):
    from app.seed_data import TARIFS_DENTISTE

    for tarif in TARIFS_DENTISTE:
        existing = db.query(models.TarifDentiste).filter(
            models.TarifDentiste.code == tarif["code"]
        ).first()
        if not existing:
            db.add(models.TarifDentiste(
                code=tarif["code"],
                libelle=tarif["libelle"],
                montant=tarif["montant"],
                devise=tarif.get("devise", "HTG"),
                actif=True,
            ))


# ─── Tarifs médecins (prix individuels) ──────────────────────────────────────
def _seed_tarifs_medecins(db):
    from app.seed_data import MEDECINS
    if db.query(models.TarifMedecin).count() > 0:
        return
    for med in MEDECINS:
        db.add(models.TarifMedecin(
            medecin_nom=med["nom"],
            specialite=med["specialite"],
            prix_consultation=med.get("prix_consultation", 0),
            prix_rdv=med.get("prix_rdv", 0),
            type_medecin=models.TypeMedecinEnum(med.get("type_medecin","affilie")),
            actif=True,
        ))


# ─── Gestes médicaux ─────────────────────────────────────────────────────────
def _seed_gestes_medicaux(db):
    from app.seed_data import GESTES_PAR_SPECIALITE
    if db.query(models.GesteMedical).count() > 0:
        return
    for idx, g in enumerate(GESTES_PAR_SPECIALITE):
        db.add(models.GesteMedical(
            specialite=g["specialite"],
            libelle=g["libelle"],
            prix_suggere=g.get("prix_suggere", 0),
            prix_min=g.get("prix_min"),
            prix_max=g.get("prix_max"),
            prix_fixe=g.get("prix_fixe", False),
            ordre=idx,
        ))


# ─── Import patients depuis Excel (appelé manuellement via endpoint) ──────────
def import_patients_from_excel(db, excel_path: str):
    """
    Importe les patients depuis BD.xlsx.
    Conserve l'ID papier original + crée un nouveau numéro RB-XXXX.
    """
    try:
        bd = pd.read_excel(excel_path, sheet_name='Sheet1', header=None)
    except Exception as e:
        return {"error": str(e)}

    imported = 0
    skipped  = 0
    from datetime import datetime, timedelta

    for i, row in bd.iterrows():
        if i == 0:
            continue
        nom = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        if not nom or nom in ["Noms", ""]:
            continue

        prenom   = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ""
        id_papier = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else ""
        telephone = str(row.iloc[4]).strip() if pd.notna(row.iloc[4]) else ""

        # Convertir date Excel
        date_val = row.iloc[0]
        date_visite = None
        try:
            if isinstance(date_val, (int, float)) and not pd.isna(date_val):
                date_visite = datetime(1899, 12, 30) + timedelta(days=int(date_val))
        except:
            pass

        # Vérifier si patient existe déjà (par ID papier)
        existing = db.query(models.Patient).filter(
            models.Patient.id_papier == id_papier,
            models.Patient.service == "clinique"
        ).first() if id_papier else None

        if existing:
            skipped += 1
            continue

        # Générer nouveau numéro RB-XXXX
        count = db.query(models.Patient).count()
        nouveau_numero = f"#RB-{str(count + 1).zfill(4)}"

        patient = models.Patient(
            numero=nouveau_numero,
            id_papier=id_papier,
            nom=nom,
            prenom=prenom,
            telephone=telephone,
            service="clinique",
            date_premiere_visite=date_visite,
        )
        db.add(patient)
        imported += 1

        if imported % 50 == 0:
            db.commit()

    db.commit()
    return {"imported": imported, "skipped": skipped}


# Alias
run_seed = seed_database

if __name__ == "__main__":
    seed_database()
    print("✅ Seed terminé")
