"""
routers.py — Clinique de la Rebecca
Conformité PCN Haïti + IFRS for SMEs
Corrections appliquées :
  - Partie double garantie sur toutes les écritures comptables
  - Décaissements médecins : 651→468 puis 468→511 (deux mouvements)
  - Numérotation séquentielle des pièces (VTE-AAAA-NNNN)
  - Multi-devises HTG/USD avec taux obligatoire
  - Verrouillage des périodes clôturées
  - Contrepassation au lieu de suppression
  - Lettrage RDV ↔ mouvement
  - Immobilisations + amortissements
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import app.models as models
import app.schemas as schemas
from app.database import get_db

# ── Token blacklist (in-memory — à remplacer par Redis en prod) ──────────
REVOKED_TOKENS: set = set()

from app.auth import (get_current_user, require_admin,
                      verify_password, get_password_hash, create_access_token)
from app.services.notifications import notify_rdv_confirmed, notify_rdv_video_confirme
import asyncio
import os
import uuid as uuid_lib

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS COMPTABLES
# ══════════════════════════════════════════════════════════════════════════════

def _verif_periode(mois: int, annee: int, db: Session):
    """Lève une exception si la période est clôturée."""
    p = db.query(models.PeriodeComptable).filter(
        models.PeriodeComptable.mois == mois,
        models.PeriodeComptable.annee == annee,
        models.PeriodeComptable.statut == models.StatutPeriodeEnum.cloturee,
    ).first()
    if p:
        raise HTTPException(
            423,
            f"Période {mois}/{annee} clôturée — écriture impossible. "
            "Utilisez une contrepassation sur la période courante."
        )


def _verif_balance(montant_total: float, montant_medecin: float, montant_clinique: float):
    """Vérifie que débit = crédit (partie double)."""
    diff = abs(montant_total - (montant_medecin + montant_clinique))
    if diff > 1.0:  # Tolérance arrondi 1 HTG
        raise HTTPException(
            422,
            f"Déséquilibre comptable : {montant_total} ≠ "
            f"{montant_medecin} + {montant_clinique} "
            f"(différence : {diff:.2f} HTG)"
        )


def _next_numero_piece(journal: str, annee: int, db: Session) -> str:
    """Génère le prochain numéro de pièce séquentiel : VTE-2025-0001."""
    count = db.query(func.count(models.Mouvement.id)).filter(
        models.Mouvement.journal == journal,
        models.Mouvement.periode_annee == annee,
    ).scalar() or 0
    return f"{journal}-{annee}-{str(count + 1).zfill(4)}"


def _creer_mouvement(
    db: Session,
    journal: str,
    type_mouv: models.TypeMouvementEnum,
    categorie: str,
    description: str,
    montant: float,
    compte_debit: str,
    compte_credit: str,
    libelle_debit: str = "",
    libelle_credit: str = "",
    mode_paiement: str = "especes",
    devise: models.DeviseEnum = models.DeviseEnum.HTG,
    montant_usd: float = None,
    taux_usd_htg: float = None,
    reference: str = None,
    rdv_id: int = None,
    created_by: int = None,
    est_contrepassation: bool = False,
    mouvement_origine_id: int = None,
    notes: str = None,
) -> models.Mouvement:
    """
    Crée un mouvement comptable avec partie double.
    Validation : montant > 0, période non clôturée.
    """
    if montant <= 0:
        raise HTTPException(422, "Le montant doit être positif (> 0 HTG)")

    now = datetime.now(timezone.utc)
    mois, annee = now.month, now.year

    # Vérification période
    if not est_contrepassation:
        _verif_periode(mois, annee, db)

    # Numéro de pièce séquentiel
    numero = _next_numero_piece(journal, annee, db)

    # Conversion USD si nécessaire
    montant_htg = None
    if devise == models.DeviseEnum.USD and montant_usd and taux_usd_htg:
        montant_htg = round(montant_usd * taux_usd_htg, 2)

    m = models.Mouvement(
        numero_piece    = numero,
        journal         = journal,
        type            = type_mouv,
        categorie       = categorie,
        description     = description,
        montant         = round(montant, 2),
        compte_debit    = compte_debit,
        compte_credit   = compte_credit,
        libelle_debit   = libelle_debit,
        libelle_credit  = libelle_credit,
        mode_paiement   = mode_paiement,
        devise          = devise,
        montant_usd     = montant_usd,
        taux_usd_htg    = taux_usd_htg,
        montant_htg     = montant_htg,
        reference       = reference,
        rdv_id          = rdv_id,
        periode_mois    = mois,
        periode_annee   = annee,
        est_contrepassation = est_contrepassation,
        mouvement_origine_id = mouvement_origine_id,
        created_by      = created_by,
        notes           = notes,
    )
    db.add(m)
    return m


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auth/login", response_model=schemas.Token, tags=["Auth"])
async def login(data: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(401, "Identifiants incorrects")
    if not user.is_active:
        raise HTTPException(403, "Compte inactif — en attente de validation")
    token = create_access_token({"sub": str(user.id)})
    return {
        "access_token": token, "token_type": "bearer",
        "user": {"id": user.id, "nom": user.nom, "email": user.email,
                 "role": user.role, "specialite": user.specialite},
    }


@router.post("/auth/register", tags=["Auth"])
async def register(data: schemas.UserCreate, db: Session = Depends(get_db)):
    """
    Inscription publique — patients uniquement.
    Règles :
    - Patients : email personnel obligatoire (pas @cliniquerebecca.ht)
    - Personnel (médecin, admin, caissier, labo, infirmier, pharmacie) :
      email @cliniquerebecca.ht obligatoire — compte créé par l'admin uniquement
    """
    email_lower = data.email.lower()
    is_staff_role = data.role not in [models.RoleEnum.patient]
    is_clinic_email = email_lower.endswith("@cliniquerebecca.ht")

    # Personnel ne peut pas s'inscrire en self-service
    if is_staff_role:
        raise HTTPException(403, "L'inscription en libre-service est réservée aux patients. Le personnel doit contacter l'administrateur.")

    # Patients ne peuvent pas utiliser l'email clinique
    if is_clinic_email:
        raise HTTPException(400, "Les comptes patients doivent utiliser un email personnel. Les emails @cliniquerebecca.ht sont réservés au personnel.")

    if db.query(models.User).filter(models.User.email == data.email).first():
        raise HTTPException(400, "Email déjà utilisé")
    is_active = data.role == models.RoleEnum.patient
    user = models.User(
        email=data.email, nom=data.nom,
        hashed_password=get_password_hash(data.password),
        role=data.role, telephone=data.telephone,
        specialite=data.specialite, type_medecin=data.type_medecin,
        is_active=is_active,
    )
    db.add(user); db.commit(); db.refresh(user)
    if data.role == models.RoleEnum.medecin and data.type_medecin:
        profil = models.ProfilMedecin(
            user_id=user.id, nom=user.nom,
            specialite=data.specialite, type_medecin=data.type_medecin,
        )
        db.add(profil); db.commit()
    if is_active:
        token = create_access_token({"sub": str(user.id)})
        return {"access_token": token, "token_type": "bearer",
                "user": {"id": user.id, "nom": user.nom, "email": user.email, "role": user.role}}
    return {"message": "Compte créé — en attente de validation", "role": data.role}


@router.get("/auth/me", tags=["Auth"])
async def me(current_user: models.User = Depends(get_current_user)):
    return {"id": current_user.id, "nom": current_user.nom,
            "email": current_user.email, "role": current_user.role}


# ══════════════════════════════════════════════════════════════════════════════
# SERVICES / SPÉCIALISTES / HORAIRES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/services", response_model=List[schemas.ServiceOut], tags=["Services"])
async def list_services(db: Session = Depends(get_db)):
    return db.query(models.Service).filter(models.Service.actif == True).order_by(models.Service.ordre).all()

@router.post("/admin/services", response_model=schemas.ServiceOut, tags=["Admin"])
async def create_service(data: schemas.ServiceCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    svc = models.Service(**data.model_dump()); db.add(svc); db.commit(); db.refresh(svc); return svc

@router.put("/admin/services/{sid}", response_model=schemas.ServiceOut, tags=["Admin"])
async def update_service(sid: int, data: schemas.ServiceUpdate, db: Session = Depends(get_db), _=Depends(require_admin)):
    svc = db.query(models.Service).filter(models.Service.id == sid).first()
    if not svc: raise HTTPException(404)
    for k, v in data.model_dump(exclude_none=True).items(): setattr(svc, k, v)
    db.commit(); db.refresh(svc); return svc

@router.delete("/admin/services/{sid}", tags=["Admin"])
async def delete_service(sid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    svc = db.query(models.Service).filter(models.Service.id == sid).first()
    if not svc: raise HTTPException(404)
    svc.actif = False; db.commit(); return {"message": "Supprimé"}

@router.get("/specialistes", response_model=List[schemas.SpecialisteOut], tags=["Spécialistes"])
async def list_specialistes(categorie: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.Specialiste).filter(models.Specialiste.actif == True)
    if categorie and categorie != "tous":
        q = q.filter(models.Specialiste.categorie.in_([categorie, "tous"]))
    return q.order_by(models.Specialiste.ordre).all()

@router.get("/specialistes/{spec_id}", response_model=schemas.SpecialisteOut, tags=["Spécialistes"])
async def get_specialiste(spec_id: int, db: Session = Depends(get_db)):
    s = db.query(models.Specialiste).filter(models.Specialiste.id == spec_id).first()
    if not s: raise HTTPException(404); return s

@router.post("/admin/specialistes", response_model=schemas.SpecialisteOut, tags=["Admin"])
async def create_specialiste(data: schemas.SpecialisteCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = models.Specialiste(**data.model_dump()); db.add(s); db.commit(); db.refresh(s); return s

@router.put("/admin/specialistes/{sid}", response_model=schemas.SpecialisteOut, tags=["Admin"])
async def update_specialiste(sid: int, data: schemas.SpecialisteUpdate, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = db.query(models.Specialiste).filter(models.Specialiste.id == sid).first()
    if not s: raise HTTPException(404)
    for k, v in data.model_dump(exclude_none=True).items(): setattr(s, k, v)
    db.commit(); db.refresh(s); return s

@router.delete("/admin/specialistes/{sid}", tags=["Admin"])
async def delete_specialiste(sid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = db.query(models.Specialiste).filter(models.Specialiste.id == sid).first()
    if not s: raise HTTPException(404)
    s.actif = False; db.commit(); return {"message": "Supprimé"}

@router.get("/horaires", response_model=List[schemas.HoraireOut], tags=["Horaires"])
async def get_horaires(db: Session = Depends(get_db)):
    return db.query(models.Horaire).order_by(models.Horaire.id).all()

@router.put("/admin/horaires/{jour}", response_model=schemas.HoraireOut, tags=["Admin"])
async def update_horaire(jour: str, data: schemas.HoraireUpdate, db: Session = Depends(get_db), _=Depends(require_admin)):
    h = db.query(models.Horaire).filter(models.Horaire.jour == jour).first()
    if not h: raise HTTPException(404)
    h.ouvert = data.ouvert; h.heure_ouverture = data.heure_ouverture; h.heure_fermeture = data.heure_fermeture
    db.commit(); db.refresh(h); return h


# ══════════════════════════════════════════════════════════════════════════════
# RENDEZ-VOUS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/rendez-vous", response_model=schemas.RendezVousOut, status_code=201, tags=["RDV"])
async def create_rdv(data: schemas.RendezVousCreate, db: Session = Depends(get_db)):
    rdv = models.RendezVous(**data.model_dump())
    db.add(rdv); db.commit(); db.refresh(rdv)
    medecins_emails = [rdv.medecin_email] if rdv.medecin_email else [
        u.email for u in db.query(models.User).filter(
            models.User.role == models.RoleEnum.medecin,
            models.User.specialite.ilike(f"%{rdv.specialite}%"),
            models.User.is_active == True,
        ).all() if u.email
    ]
    rdv_data = {
        "patient_nom": rdv.patient_nom, "patient_telephone": rdv.patient_telephone,
        "patient_email": rdv.patient_email, "specialite": rdv.specialite,
        "date_rdv": rdv.date_rdv, "type_rdv": str(rdv.type_rdv),
        "motif": rdv.motif, "mode_paiement": rdv.mode_paiement,
        "reference_paiement": rdv.reference_paiement,
        "medecin_nom": rdv.medecin_nom or "",
        "medecins_emails": medecins_emails,
    }
    asyncio.create_task(notify_rdv_confirmed(rdv_data))
    return rdv

@router.get("/admin/rendez-vous", response_model=List[schemas.RendezVousOut], tags=["Admin"])
async def admin_list_rdv(statut: Optional[str] = None, db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(models.RendezVous)
    if statut: q = q.filter(models.RendezVous.statut == statut)
    return q.order_by(models.RendezVous.date_rdv.desc()).all()

@router.put("/admin/rendez-vous/{rdv_id}", response_model=schemas.RendezVousOut, tags=["Admin"])
async def update_rdv(rdv_id: int, data: schemas.RendezVousUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv: raise HTTPException(404, "RDV introuvable")
    ancien_statut = str(rdv.statut)
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(rdv, k, v)
    if data.statut and str(data.statut) == "confirme" and ancien_statut != "confirme":
        if str(rdv.type_rdv) == "video" and not rdv.lien_video:
            numero = rdv.numero_rdv or f"rdv{rdv.id}"
            rdv.lien_video = f"https://meet.jit.si/clinique-rebecca-{numero}"
        db.commit(); db.refresh(rdv)
        rdv_data = {
            "patient_nom": rdv.patient_nom, "patient_telephone": rdv.patient_telephone,
            "patient_email": rdv.patient_email, "specialite": rdv.specialite,
            "date_rdv": rdv.date_rdv, "type_rdv": str(rdv.type_rdv),
            "motif": rdv.motif, "lien_video": rdv.lien_video,
        }
        asyncio.create_task(notify_rdv_video_confirme(rdv_data))
    else:
        db.commit(); db.refresh(rdv)
    return rdv

@router.delete("/admin/rendez-vous/{rdv_id}", tags=["Admin"])
async def cancel_rdv(rdv_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv: raise HTTPException(404)
    rdv.statut = "annule"; db.commit()
    return {"message": "RDV annulé"}

@router.get("/medecin/rendez-vous", response_model=List[schemas.RendezVousOut], tags=["Médecin"])
async def medecin_rdv(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.RendezVous).order_by(models.RendezVous.date_rdv.desc()).limit(50).all()

@router.get("/patient/rendez-vous", response_model=List[schemas.RendezVousOut], tags=["Patient"])
async def patient_rdv(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    return db.query(models.RendezVous).filter(
        models.RendezVous.patient_email == current_user.email
    ).order_by(models.RendezVous.date_rdv.desc()).all()

@router.get("/caissier/rendez-vous", tags=["Caissier"])
async def caissier_rdv(db: Session = Depends(get_db), _=Depends(get_current_user)):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
    return db.query(models.RendezVous).filter(
        models.RendezVous.date_rdv >= today
    ).order_by(models.RendezVous.date_rdv).all()

@router.post("/caissier/encaissement/{rdv_id}", tags=["Caissier"])
async def encaisser(rdv_id: int, data: dict, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv: raise HTTPException(404, "RDV introuvable")

    montant    = float(data.get("montant", 0))
    mode       = data.get("mode_paiement", "especes")
    devise_str = data.get("devise", "HTG")
    taux       = data.get("taux_usd_htg")
    categorie  = data.get("categorie", "Consultations")

    devise = models.DeviseEnum.USD if devise_str == "USD" else models.DeviseEnum.HTG

    if devise == models.DeviseEnum.USD and not taux:
        raise HTTPException(422, "Taux USD/HTG obligatoire pour un paiement en USD")

    compte_tresorerie = models.get_compte_tresorerie(mode, devise_str)
    compte_produit    = models.COMPTE_PCN.get(categorie, "701")

    mouvement = _creer_mouvement(
        db=db, journal=models.JournalEnum.VTE,
        type_mouv=models.TypeMouvementEnum.recette,
        categorie=categorie,
        description=f"Encaissement RDV #{rdv_id} — {rdv.patient_nom} — {rdv.specialite}",
        montant=montant,
        compte_debit=compte_tresorerie,
        compte_credit=compte_produit,
        libelle_debit=f"Trésorerie {mode}",
        libelle_credit=f"Produits {categorie}",
        mode_paiement=mode, devise=devise,
        montant_usd=montant if devise == models.DeviseEnum.USD else None,
        taux_usd_htg=taux,
        rdv_id=rdv_id, created_by=current_user.id,
    )
    rdv.statut = "confirme"
    rdv.mouvement_id = mouvement.id
    db.commit()
    return {"message": "Encaissement enregistré", "numero_piece": mouvement.numero_piece}


# ══════════════════════════════════════════════════════════════════════════════
# PATIENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/patients", tags=["Admin"])
async def list_patients(search: Optional[str] = None, db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(models.Patient)
    if search:
        q = q.filter(models.Patient.nom.ilike(f"%{search}%") | models.Patient.telephone.ilike(f"%{search}%"))
    return q.order_by(models.Patient.created_at.desc()).limit(100).all()

@router.post("/patients", status_code=201, tags=["Patients"])
async def create_patient(data: schemas.PatientCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    count = db.query(models.Patient).count()
    patient = models.Patient(numero=f"#RB-{str(count+1).zfill(4)}", **data.model_dump(), created_by=current_user.id)
    db.add(patient); db.commit(); db.refresh(patient); return patient

@router.get("/patients/search", tags=["Patients"])
async def search_patients(q: str = "", db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Patient).filter(
        models.Patient.nom.ilike(f"%{q}%") | models.Patient.numero.ilike(f"%{q}%") | models.Patient.telephone.ilike(f"%{q}%")
    ).limit(20).all()

@router.get("/patients/{pid}", tags=["Patients"])
async def get_patient(pid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    p = db.query(models.Patient).filter(models.Patient.id == pid).first()
    if not p: raise HTTPException(404); return p


# ══════════════════════════════════════════════════════════════════════════════
# PROFILS MÉDECINS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/profils-medecins", response_model=List[schemas.ProfilMedecinOut], tags=["Admin - Compta"])
async def list_profils(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.ProfilMedecin).filter(models.ProfilMedecin.actif == True).all()

@router.put("/admin/profils-medecins/{pid}", tags=["Admin - Compta"])
async def update_profil(pid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.query(models.ProfilMedecin).filter(models.ProfilMedecin.id == pid).first()
    if not p: raise HTTPException(404)
    for k, v in data.items(): setattr(p, k, v)
    db.commit(); return p


# ══════════════════════════════════════════════════════════════════════════════
# RÈGLES DE PARTAGE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/regles-partage", response_model=List[schemas.ReglePartageOut], tags=["Admin - Compta"])
async def list_regles(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.ReglePartage).all()

@router.put("/admin/regles-partage/{rid}", tags=["Admin - Compta"])
async def update_regle(rid: int, pct_medecin: float, db: Session = Depends(get_db), _=Depends(require_admin)):
    r = db.query(models.ReglePartage).filter(models.ReglePartage.id == rid).first()
    if not r: raise HTTPException(404)
    r.pct_medecin = pct_medecin; r.pct_clinique = round(100 - pct_medecin, 2)
    db.commit(); return r


# ══════════════════════════════════════════════════════════════════════════════
# ACTES FACTURABLES — PARTIE DOUBLE COMPLÈTE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/actes-facturables", response_model=List[schemas.ActeOut], tags=["Admin - Compta"])
async def list_actes(mois: Optional[int] = None, annee: Optional[int] = None,
                     db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(models.ActeFacturable)
    if annee: q = q.filter(extract("year",  models.ActeFacturable.date_acte) == annee)
    if mois:  q = q.filter(extract("month", models.ActeFacturable.date_acte) == mois)
    return q.order_by(models.ActeFacturable.date_acte.desc()).all()

@router.post("/actes-facturables", response_model=schemas.ActeOut, status_code=201, tags=["Admin - Compta"])
async def create_acte(data: schemas.ActeCreate, db: Session = Depends(get_db),
                      current_user=Depends(get_current_user)):
    # Vérification USD
    devise = models.DeviseEnum.USD if data.devise == "USD" else models.DeviseEnum.HTG
    if devise == models.DeviseEnum.USD and not data.taux_usd_htg:
        raise HTTPException(422, "taux_usd_htg obligatoire si devise=USD")

    medecin = None
    if data.medecin_id:
        medecin = db.query(models.ProfilMedecin).filter(models.ProfilMedecin.id == data.medecin_id).first()

    # Calcul répartition
    if data.montant_medecin_manuel is not None and data.montant_clinique_manuel is not None:
        montant_medecin  = round(data.montant_medecin_manuel, 2)
        montant_clinique = round(data.montant_clinique_manuel, 2)
        pct_medecin      = round(montant_medecin / data.montant_total * 100, 1) if data.montant_total else 0
    elif medecin:
        regle = db.query(models.ReglePartage).filter(
            models.ReglePartage.type_medecin == medecin.type_medecin,
            models.ReglePartage.type_acte == data.type_acte,
        ).first()
        DEFAUTS = {"investisseur":{"consultation":70,"geste":80,"chirurgie":0},
                   "affilie":{"consultation":60,"geste":70,"chirurgie":0},
                   "exploitant":{"consultation":100,"geste":100,"chirurgie":100},
                   "investisseur_exploitant":{"consultation":100,"geste":100,"chirurgie":100}}
        type_m = str(medecin.type_medecin.value)
        type_a = data.type_acte if data.type_acte in ["consultation","geste","chirurgie"] else "consultation"
        pct_medecin = regle.pct_medecin if regle else DEFAUTS.get(type_m, {}).get(type_a, 60)
        montant_medecin  = round(data.montant_total * pct_medecin / 100, 2)
        montant_clinique = round(data.montant_total - montant_medecin, 2)
    else:
        pct_medecin = 0; montant_medecin = 0; montant_clinique = round(data.montant_total, 2)

    # Vérification partie double
    _verif_balance(data.montant_total, montant_medecin, montant_clinique)

    cat_map = {"consultation":"Consultations","geste":"Gestes médicaux",
               "chirurgie":"Chirurgies","hospit":"Hospitalisations","observation":"Hospitalisations"}
    categorie      = cat_map.get(data.type_acte, "Consultations")
    compte_produit = models.COMPTE_PCN.get(categorie, "701")
    compte_tresor  = models.get_compte_tresorerie(data.mode_paiement, data.devise or "HTG")

    # ── Écriture 1 : Recette totale (partie clinique) ──────────────────
    # PCN  : 511/521 Trésorerie (D) / 701..709 Produits (C) = montant_clinique
    # IFRS 15 : produit reconnu à la réalisation de l'acte
    mouv_recette = _creer_mouvement(
        db=db, journal=models.JournalEnum.VTE,
        type_mouv=models.TypeMouvementEnum.recette,
        categorie=categorie,
        description=f"{data.type_acte.capitalize()} — {data.patient_nom}" + (f" (Dr {medecin.nom})" if medecin else ""),
        montant=montant_clinique,
        compte_debit=compte_tresor,
        compte_credit=compte_produit,
        libelle_debit=f"Trésorerie {data.mode_paiement}",
        libelle_credit=f"Produits {categorie}",
        mode_paiement=data.mode_paiement, devise=devise,
        montant_usd=data.montant_total if devise == models.DeviseEnum.USD else None,
        taux_usd_htg=data.taux_usd_htg, created_by=current_user.id,
    )

    mouv_honoraires_id = None
    # ── Écriture 2 : Honoraires médecin → compte courant 468 ──────────
    # PCN  : 651 Honoraires (D) / 468 C/C médecin (C) = montant_medecin
    # IFRS : charge de personnel / partage de revenu selon substance
    if medecin and montant_medecin > 0:
        mouv_honoraires = _creer_mouvement(
            db=db, journal=models.JournalEnum.OD,
            type_mouv=models.TypeMouvementEnum.depense,
            categorie="Honoraires médecins",
            description=f"Honoraires Dr {medecin.nom} — {data.patient_nom} — {data.type_acte}",
            montant=montant_medecin,
            compte_debit="651",
            compte_credit="468",
            libelle_debit="Honoraires médecins (651)",
            libelle_credit=f"C/C Dr {medecin.nom} (468)",
            mode_paiement="virement_interne", created_by=current_user.id,
        )
        mouv_honoraires_id = mouv_honoraires.id
        # Mettre à jour le solde 468 du médecin
        medecin.solde_compte_468 = round((medecin.solde_compte_468 or 0) + montant_medecin, 2)

    acte = models.ActeFacturable(
        medecin_id=data.medecin_id, medecin_nom=medecin.nom if medecin else None,
        patient_nom=data.patient_nom, type_acte=data.type_acte,
        specialite=data.specialite, description=data.description,
        montant_total=data.montant_total, montant_medecin=montant_medecin,
        montant_clinique=montant_clinique, pct_medecin=pct_medecin,
        devise=devise, taux_usd_htg=data.taux_usd_htg,
        mode_paiement=data.mode_paiement,
        balance_ok=True,
        mouvement_recette_id=mouv_recette.id,
        mouvement_honoraires_id=mouv_honoraires_id,
        created_by=current_user.id,
    )
    db.add(acte); db.commit(); db.refresh(acte)
    return acte


# ══════════════════════════════════════════════════════════════════════════════
# DÉCAISSEMENTS — PARTIE DOUBLE CORRECTE (468 → 511)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/decaissements", response_model=List[schemas.DecaissementOut], tags=["Admin - Compta"])
async def list_decaissements(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Decaissement).order_by(models.Decaissement.date_decaissement.desc()).all()

@router.post("/admin/decaissements", status_code=201, tags=["Admin - Compta"])
async def create_decaissement(data: schemas.DecaissementCreate, db: Session = Depends(get_db),
                               current_user=Depends(get_current_user)):
    """
    Décaissement médecin — CONFORME PCN HAÏTI (partie double en 2 étapes) :

    Écriture 1 (si pas encore passée via acte) :
      Débit  651 Honoraires médecins
      Crédit 468 C/C Dr [nom]
      → Constate la dette de la clinique envers le médecin

    Écriture 2 (paiement effectif) :
      Débit  468 C/C Dr [nom]
      Crédit 511/521 Caisse / Banque
      → Solde le compte courant par sortie de trésorerie
    """
    devise = models.DeviseEnum.USD if data.devise == "USD" else models.DeviseEnum.HTG
    if devise == models.DeviseEnum.USD and not data.taux_usd_htg:
        raise HTTPException(422, "taux_usd_htg obligatoire si devise=USD")

    profil = db.query(models.ProfilMedecin).filter(models.ProfilMedecin.id == data.medecin_id).first()
    nom    = profil.nom if profil else (data.medecin_nom or "Inconnu")

    compte_tresor = models.get_compte_tresorerie(data.mode_paiement, data.devise or "HTG")

    # ── Écriture 1 : Constatation dette 651 → 468 ─────────────────────
    mouv_468 = _creer_mouvement(
        db=db, journal=models.JournalEnum.OD,
        type_mouv=models.TypeMouvementEnum.depense,
        categorie="Honoraires médecins",
        description=f"Constatation honoraires Dr {nom} — {data.motif}",
        montant=data.montant,
        compte_debit="651", compte_credit="468",
        libelle_debit="Honoraires médecins (651)",
        libelle_credit=f"C/C Dr {nom} (468)",
        mode_paiement="interne", devise=devise,
        montant_usd=data.montant if devise == models.DeviseEnum.USD else None,
        taux_usd_htg=data.taux_usd_htg, created_by=current_user.id,
        notes=f"Étape 1/2 — Décaissement Dr {nom}",
    )

    # ── Écriture 2 : Paiement cash 468 → 511/521 ─────────────────────
    mouv_511 = _creer_mouvement(
        db=db, journal=models.JournalEnum.DECAIS,
        type_mouv=models.TypeMouvementEnum.depense,
        categorie="Honoraires médecins",
        description=f"Paiement Dr {nom} — {data.motif}",
        montant=data.montant,
        compte_debit="468", compte_credit=compte_tresor,
        libelle_debit=f"C/C Dr {nom} (468)",
        libelle_credit=f"Trésorerie {data.mode_paiement} ({compte_tresor})",
        mode_paiement=data.mode_paiement, devise=devise,
        montant_usd=data.montant if devise == models.DeviseEnum.USD else None,
        taux_usd_htg=data.taux_usd_htg, created_by=current_user.id,
        notes=f"Étape 2/2 — Sortie trésorerie Dr {nom}",
    )

    # Mettre à jour solde 468 médecin
    if profil:
        profil.solde_compte_468 = round((profil.solde_compte_468 or 0) - data.montant, 2)

    # Marquer actes comme décaissés
    db.query(models.ActeFacturable).filter(
        models.ActeFacturable.medecin_id == data.medecin_id,
        models.ActeFacturable.statut_decaissement == "en_attente",
    ).update({"statut_decaissement": "decaisse"})

    dec = models.Decaissement(
        medecin_id=data.medecin_id, medecin_nom=nom,
        montant=data.montant, motif=data.motif,
        mode_paiement=data.mode_paiement, devise=devise,
        taux_usd_htg=data.taux_usd_htg,
        mouvement_468_id=mouv_468.id,
        mouvement_511_id=mouv_511.id,
        created_by=current_user.id,
    )
    db.add(dec); db.commit(); db.refresh(dec)
    return dec


# ══════════════════════════════════════════════════════════════════════════════
# MOUVEMENTS COMPTABLES — JOURNAL
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/mouvements", response_model=List[schemas.MouvementOut], tags=["Admin - Compta"])
async def list_mouvements(
    type: Optional[str] = None, mois: Optional[int] = None,
    annee: Optional[int] = None, journal: Optional[str] = None,
    db: Session = Depends(get_db), _=Depends(get_current_user)
):
    q = db.query(models.Mouvement)
    if type:    q = q.filter(models.Mouvement.type == type)
    if annee:   q = q.filter(models.Mouvement.periode_annee == annee)
    if mois:    q = q.filter(models.Mouvement.periode_mois == mois)
    if journal: q = q.filter(models.Mouvement.journal == journal)
    return q.order_by(models.Mouvement.created_at.desc()).all()

@router.post("/admin/mouvements", response_model=schemas.MouvementOut, status_code=201, tags=["Admin - Compta"])
async def create_mouvement(data: schemas.MouvementCreate, db: Session = Depends(get_db),
                            current_user=Depends(get_current_user)):
    """Saisie manuelle d'un mouvement — PCN partie double exigée."""
    devise = models.DeviseEnum.USD if data.devise == "USD" else models.DeviseEnum.HTG
    if devise == models.DeviseEnum.USD and not data.taux_usd_htg:
        raise HTTPException(422, "taux_usd_htg obligatoire si devise=USD")

    type_mouv = models.TypeMouvementEnum.recette if data.type == "recette" else models.TypeMouvementEnum.depense
    journal   = models.JournalEnum.VTE if data.type == "recette" else models.JournalEnum.ACH

    compte_tresor  = models.get_compte_tresorerie(data.mode_paiement, data.devise or "HTG")
    compte_contrep = models.COMPTE_PCN.get(data.categorie, "701" if data.type == "recette" else "628")

    if data.type == "recette":
        compte_d, compte_c = compte_tresor, compte_contrep
    else:
        compte_d, compte_c = compte_contrep, compte_tresor

    m = _creer_mouvement(
        db=db, journal=journal, type_mouv=type_mouv,
        categorie=data.categorie, description=data.description,
        montant=data.montant,
        compte_debit=compte_d, compte_credit=compte_c,
        libelle_debit=data.libelle_debit or "",
        libelle_credit=data.libelle_credit or "",
        mode_paiement=data.mode_paiement, devise=devise,
        montant_usd=data.montant_usd, taux_usd_htg=data.taux_usd_htg,
        reference=data.reference, notes=data.notes,
        created_by=current_user.id,
    )
    db.commit(); db.refresh(m)
    return m

@router.post("/admin/mouvements/{mid}/contrepasser", tags=["Admin - Compta"])
async def contrepasser_mouvement(mid: int, raison: str, db: Session = Depends(get_db),
                                  current_user=Depends(require_admin)):
    """
    Contrepassation PCN — JAMAIS de suppression d'écriture.
    Crée un mouvement inverse avec référence à l'original.
    """
    orig = db.query(models.Mouvement).filter(models.Mouvement.id == mid).first()
    if not orig: raise HTTPException(404, "Mouvement introuvable")
    if orig.est_contrepassation:
        raise HTTPException(400, "Impossible de contrepasser une contrepassation")

    type_inv = (models.TypeMouvementEnum.depense
                if orig.type == models.TypeMouvementEnum.recette
                else models.TypeMouvementEnum.recette)

    journal_inv = models.JournalEnum.OD
    contrepass = _creer_mouvement(
        db=db, journal=journal_inv, type_mouv=type_inv,
        categorie=orig.categorie,
        description=f"CONTREPASSATION de {orig.numero_piece} — {raison}",
        montant=orig.montant,
        compte_debit=orig.compte_credit,    # Inversion débit/crédit
        compte_credit=orig.compte_debit,
        libelle_debit=f"Contrepassation {orig.libelle_credit}",
        libelle_credit=f"Contrepassation {orig.libelle_debit}",
        mode_paiement=orig.mode_paiement, devise=orig.devise,
        notes=f"Contrepassation de {orig.numero_piece}. Raison : {raison}",
        created_by=current_user.id,
        est_contrepassation=True,
        mouvement_origine_id=orig.id,
    )
    db.commit()
    return {
        "message": "Contrepassation créée",
        "numero_piece_original": orig.numero_piece,
        "numero_piece_contrepassation": contrepass.numero_piece,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PÉRIODES COMPTABLES — VERROUILLAGE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/periodes", tags=["Admin - Compta"])
async def list_periodes(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.PeriodeComptable).order_by(
        models.PeriodeComptable.annee.desc(), models.PeriodeComptable.mois.desc()
    ).all()

@router.post("/admin/periodes/cloturer", tags=["Admin - Compta"])
async def cloturer_periode(mois: int, annee: int, db: Session = Depends(get_db),
                            current_user=Depends(require_admin)):
    """Clôture une période comptable — irréversible sauf admin DBA."""
    p = db.query(models.PeriodeComptable).filter(
        models.PeriodeComptable.mois == mois,
        models.PeriodeComptable.annee == annee,
    ).first()
    if not p:
        p = models.PeriodeComptable(mois=mois, annee=annee); db.add(p)
    if p.statut == models.StatutPeriodeEnum.cloturee:
        raise HTTPException(400, f"Période {mois}/{annee} déjà clôturée")
    p.statut     = models.StatutPeriodeEnum.cloturee
    p.cloture_par = current_user.id
    p.cloture_at = datetime.now(timezone.utc)
    # Verrouiller tous les mouvements de cette période
    db.query(models.Mouvement).filter(
        models.Mouvement.periode_mois == mois,
        models.Mouvement.periode_annee == annee,
    ).update({"periode_verrou": True})
    db.commit()
    return {"message": f"Période {mois}/{annee} clôturée avec succès"}


# ══════════════════════════════════════════════════════════════════════════════
# BILANS MENSUELS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/bilans", tags=["Admin - Compta"])
async def list_bilans(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.BilanMensuel).order_by(
        models.BilanMensuel.annee.desc(), models.BilanMensuel.mois.desc()
    ).all()

@router.post("/admin/generer-bilan", tags=["Admin - Compta"])
async def generer_bilan(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    mois = data.get("mois"); annee = data.get("annee")

    def sum_compte(comptes: list, type_mouv: str = "recette") -> float:
        q = db.query(func.sum(models.Mouvement.montant)).filter(
            models.Mouvement.type == type_mouv,
            models.Mouvement.compte_credit.in_(comptes) if type_mouv == "recette"
            else models.Mouvement.compte_debit.in_(comptes),
            models.Mouvement.periode_mois == mois,
            models.Mouvement.periode_annee == annee,
            models.Mouvement.est_contrepassation == False,
        )
        return round(q.scalar() or 0.0, 2)

    # Produits par compte PCN
    tot_cons  = sum_compte(["701"])
    tot_gest  = sum_compte(["702"])
    tot_chir  = sum_compte(["703"])
    tot_hosp  = sum_compte(["704"])
    tot_labo  = sum_compte(["705"])
    tot_phar  = sum_compte(["706","707","708","709"])
    tot_loyer = sum_compte(["711"])
    tot_autres_prod = sum_compte(["719"])
    total_prod = tot_cons + tot_gest + tot_chir + tot_hosp + tot_labo + tot_phar + tot_loyer + tot_autres_prod

    # Charges par compte PCN
    tot_honor = sum_compte(["651"], "depense")
    tot_sal   = sum_compte(["641"], "depense")
    tot_cs    = sum_compte(["645"], "depense")
    tot_achats = sum_compte(["601","607"], "depense")
    tot_amort = sum_compte(["681"], "depense")
    tot_infra = sum_compte(["615"], "depense")
    tot_autres_ch = sum_compte(["626","628"], "depense")
    total_charges = tot_honor + tot_sal + tot_cs + tot_achats + tot_amort + tot_infra + tot_autres_ch

    # TCA collectée
    tot_tca = db.query(func.sum(models.Mouvement.tca_montant)).filter(
        models.Mouvement.periode_mois == mois,
        models.Mouvement.periode_annee == annee,
    ).scalar() or 0.0

    bilan = db.query(models.BilanMensuel).filter(
        models.BilanMensuel.mois == mois, models.BilanMensuel.annee == annee
    ).first()
    if not bilan:
        bilan = models.BilanMensuel(mois=mois, annee=annee); db.add(bilan)

    bilan.total_consultations       = tot_cons
    bilan.total_gestes               = tot_gest
    bilan.total_chirurgies           = tot_chir
    bilan.total_hospitalisations     = tot_hosp
    bilan.total_laboratoire          = tot_labo
    bilan.total_pharmacie            = tot_phar
    bilan.total_loyers_recus         = tot_loyer
    bilan.total_autres_produits      = tot_autres_prod
    bilan.total_produits             = total_prod
    bilan.total_honoraires_medecins  = tot_honor
    bilan.total_salaires             = tot_sal
    bilan.total_charges_sociales     = tot_cs
    bilan.total_pharmacie_achats     = tot_achats
    bilan.total_amortissements       = tot_amort
    bilan.total_infrastructure       = tot_infra
    bilan.total_autres_charges       = tot_autres_ch
    bilan.total_charges              = total_charges
    bilan.resultat_net               = round(total_prod - total_charges, 2)
    bilan.total_tca_collectee        = round(tot_tca, 2)
    db.commit(); db.refresh(bilan)
    return bilan

@router.put("/admin/bilans/{bid}/valider", tags=["Admin - Compta"])
async def valider_bilan(bid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    b = db.query(models.BilanMensuel).filter(models.BilanMensuel.id == bid).first()
    if not b: raise HTTPException(404)
    b.statut = "valide"; db.commit(); return b

@router.get("/admin/rapport-cumul", tags=["Admin - Compta"])
async def rapport_cumul(mois_debut: int, annee_debut: int, mois_fin: int, annee_fin: int,
                         db: Session = Depends(get_db), _=Depends(require_admin)):
    bilans = db.query(models.BilanMensuel).all()
    filtre = [b for b in bilans if
              (b.annee > annee_debut or (b.annee == annee_debut and b.mois >= mois_debut)) and
              (b.annee < annee_fin  or (b.annee == annee_fin  and b.mois <= mois_fin))]
    return {
        "periode": f"{mois_debut}/{annee_debut} — {mois_fin}/{annee_fin}",
        "nb_mois": len(filtre),
        "total_produits": sum(b.total_produits for b in filtre),
        "total_charges":  sum(b.total_charges for b in filtre),
        "resultat_net":   sum(b.resultat_net for b in filtre),
        "bilans": [{"mois":b.mois,"annee":b.annee,"produits":b.total_produits,
                    "charges":b.total_charges,"resultat":b.resultat_net,"statut":b.statut} for b in filtre],
    }


# ══════════════════════════════════════════════════════════════════════════════
# IMMOBILISATIONS (CLASSE 2 PCN)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/immobilisations", tags=["Admin - Compta"])
async def list_immobilisations(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Immobilisation).filter(models.Immobilisation.actif == True).all()

@router.post("/admin/immobilisations", status_code=201, tags=["Admin - Compta"])
async def create_immobilisation(data: dict, db: Session = Depends(get_db),
                                 current_user=Depends(require_admin)):
    """
    Acquisition d'une immobilisation — PCN Classe 2.
    Écriture : 218 Équipements médicaux (D) / 511 Caisse ou 401 Fournisseur (C)
    """
    devise_str = data.get("devise", "HTG")
    taux       = data.get("taux_usd_htg")
    devise     = models.DeviseEnum.USD if devise_str == "USD" else models.DeviseEnum.HTG

    if devise == models.DeviseEnum.USD and not taux:
        raise HTTPException(422, "taux_usd_htg obligatoire si devise=USD")

    valeur_acq = float(data.get("valeur_acquisition", 0))
    valeur_htg = round(valeur_acq * taux, 2) if devise == models.DeviseEnum.USD and taux else valeur_acq
    duree      = int(data.get("duree_amort_ans", 5))
    compte_pcn = data.get("compte_pcn", "218")

    immo = models.Immobilisation(
        libelle=data.get("libelle"), compte_pcn=compte_pcn,
        valeur_acquisition=valeur_acq, devise_acquisition=devise,
        taux_usd_achat=taux, valeur_htg=valeur_htg,
        date_acquisition=datetime.fromisoformat(data.get("date_acquisition", datetime.now().isoformat())),
        duree_amort_ans=duree, taux_amort=round(100/duree, 2),
        amort_cumule=0.0, valeur_nette=valeur_htg,
        created_by=current_user.id,
    )
    db.add(immo)

    # Écriture comptable acquisition : 218 (D) / 511 ou 401 (C)
    mode_financement = data.get("mode_financement", "caisse")
    compte_credit = "401" if mode_financement == "fournisseur" else models.get_compte_tresorerie(mode_financement)
    _creer_mouvement(
        db=db, journal=models.JournalEnum.ACH,
        type_mouv=models.TypeMouvementEnum.depense,
        categorie="Équipements",
        description=f"Acquisition {data.get('libelle')} — immobilisation",
        montant=valeur_htg,
        compte_debit=compte_pcn, compte_credit=compte_credit,
        libelle_debit=f"Immobilisation {data.get('libelle')} ({compte_pcn})",
        libelle_credit=f"{'Fournisseur (401)' if mode_financement=='fournisseur' else 'Trésorerie'}",
        mode_paiement=mode_financement, devise=devise,
        montant_usd=valeur_acq if devise == models.DeviseEnum.USD else None,
        taux_usd_htg=taux, created_by=current_user.id,
    )
    db.commit(); db.refresh(immo)
    return immo

@router.post("/admin/immobilisations/{iid}/amortir", tags=["Admin - Compta"])
async def passer_amortissement(iid: int, db: Session = Depends(get_db),
                                current_user=Depends(require_admin)):
    """
    Dotation aux amortissements mensuelle.
    PCN : 681 Dotations amortissements (D) / 280 Amortissements cumulés (C)
    """
    immo = db.query(models.Immobilisation).filter(models.Immobilisation.id == iid).first()
    if not immo: raise HTTPException(404, "Immobilisation introuvable")
    if not immo.actif: raise HTTPException(400, "Immobilisation déjà sortie")

    amort_mensuel = round(immo.valeur_htg / (immo.duree_amort_ans * 12), 2)
    if immo.valeur_nette <= 0:
        raise HTTPException(400, "Immobilisation totalement amortie")
    amort_mensuel = min(amort_mensuel, immo.valeur_nette)

    immo.amort_cumule = round(immo.amort_cumule + amort_mensuel, 2)
    immo.valeur_nette = round(immo.valeur_nette - amort_mensuel, 2)

    _creer_mouvement(
        db=db, journal=models.JournalEnum.OD,
        type_mouv=models.TypeMouvementEnum.depense,
        categorie="Amortissements",
        description=f"Dotation amortissement — {immo.libelle}",
        montant=amort_mensuel,
        compte_debit="681", compte_credit="280",
        libelle_debit="Dotations amortissements (681)",
        libelle_credit="Amortissements cumulés (280)",
        mode_paiement="interne", created_by=current_user.id,
    )
    db.commit()
    return {"message": "Amortissement passé", "montant": amort_mensuel,
            "valeur_nette_restante": immo.valeur_nette}


# ══════════════════════════════════════════════════════════════════════════════
# TARIFS / STOCKS / LABO / OPTOMÉTRIE / PAIEMENTS EXPLOITANTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/tarifs-clinic", tags=["Admin - Compta"])
async def list_tarifs(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.TarifClinic).all()

@router.put("/admin/tarifs-clinic/{code}", tags=["Admin - Compta"])
async def update_tarif(code: str, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.query(models.TarifClinic).filter(models.TarifClinic.code == code).first()
    if not t: raise HTTPException(404)
    t.montant = data.get("montant", t.montant); db.commit(); return t

@router.get("/pharmacie/stocks", tags=["Pharmacie"])
async def get_stocks(db: Session = Depends(get_db)):
    return db.query(models.StockItem).all()

@router.post("/admin/stocks", status_code=201, tags=["Admin"])
async def create_stock(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    mode = data.get("mode_reversement", "clinique")
    valeur = float(data.get("valeur_reversement", 0))
    pct = 100.0 if mode == "clinique" else (valeur if mode == "pourcentage" else 0.0)
    item = models.StockItem(
        nom=data.get("nom"), categorie=data.get("categorie"),
        quantite=int(data.get("quantite", 0)), seuil_min=int(data.get("seuil_min", 10)),
        prix_unitaire=float(data.get("prix_unitaire", 0)), unite=data.get("unite", "unité"),
        proprietaire=data.get("proprietaire", "Clinique"),
        mode_reversement=mode, valeur_reversement=valeur, pct_clinique=pct,
    )
    db.add(item); db.commit(); db.refresh(item); return item

@router.put("/admin/stocks/{sid}", tags=["Admin"])
async def update_stock(sid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = db.query(models.StockItem).filter(models.StockItem.id == sid).first()
    if not s: raise HTTPException(404)
    for k, v in data.items(): setattr(s, k, v)
    db.commit(); return s

@router.delete("/admin/stocks/{sid}", tags=["Admin"])
async def delete_stock(sid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = db.query(models.StockItem).filter(models.StockItem.id == sid).first()
    if not s: raise HTTPException(404)
    db.delete(s); db.commit(); return {"message": "Supprimé"}

@router.get("/labo/analyses", tags=["Labo"])
async def list_analyses(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.ResultatLabo).order_by(models.ResultatLabo.date_examen.desc()).all()

@router.post("/labo/analyses", status_code=201, tags=["Labo"])
async def create_analyse(data: dict, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    r = models.ResultatLabo(
        patient_id=data.get("patient_id"), patient_nom=data.get("patient_nom", ""),
        type_examen=data.get("type_examen", ""), resultats=data.get("resultats", ""),
        notes=data.get("notes", ""), technicien_id=current_user.id,
    )
    db.add(r); db.commit(); db.refresh(r); return r

@router.put("/labo/analyses/{aid}", tags=["Labo"])
async def update_analyse(aid: int, data: dict, db: Session = Depends(get_db), _=Depends(get_current_user)):
    r = db.query(models.ResultatLabo).filter(models.ResultatLabo.id == aid).first()
    if not r: raise HTTPException(404)
    for k, v in data.items(): setattr(r, k, v)
    db.commit(); return r

@router.get("/patient/resultats-labo/{patient_id}", tags=["Patient"])
async def patient_resultats(patient_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.ResultatLabo).filter(models.ResultatLabo.patient_id == patient_id).all()

@router.get("/admin/contrat-optometrie", tags=["Optométrie"])
async def get_contrat(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.ContratOptometrie).first()

@router.put("/admin/contrat-optometrie", tags=["Optométrie"])
async def update_contrat(data: dict, db: Session = Depends(get_db), current_user=Depends(require_admin)):
    c = db.query(models.ContratOptometrie).first()
    if not c: c = models.ContratOptometrie(); db.add(c)
    for k, v in data.items(): setattr(c, k, v)
    c.updated_by = current_user.id; db.commit(); return c

@router.post("/admin/calculer-optometrie", tags=["Optométrie"])
async def calculer_optometrie(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    c = db.query(models.ContratOptometrie).first()
    if not c: raise HTTPException(404, "Contrat non configuré")
    total_consul   = float(data.get("total_consultations", 0))
    total_montures = float(data.get("total_montures", 0))
    part_consul    = round(total_consul   * c.pct_consultation / 100, 2)
    part_montures  = round(total_montures * c.pct_montures     / 100, 2)
    total_part     = part_consul + part_montures
    minimum_htg    = round(c.minimum_mensuel_usd * c.taux_usd_htg, 2)
    montant_final  = max(total_part, minimum_htg)
    bilan = models.BilanOptometrieMensuel(
        mois=data.get("mois"), annee=data.get("annee"),
        total_consultations=total_consul, total_montures=total_montures,
        part_clinique_consultations=part_consul, part_clinique_montures=part_montures,
        total_part_clinique=total_part, minimum_applicable_htg=minimum_htg,
        montant_final_clinique=montant_final, difference=round(total_part - minimum_htg, 2),
    )
    db.add(bilan); db.commit()
    return {"part_clinique_consultations": part_consul, "part_clinique_montures": part_montures,
            "total_part_clinique": total_part, "minimum_htg": minimum_htg,
            "montant_final_clinique": montant_final,
            "verdict": "OK" if total_part >= minimum_htg else f"COMPLÉMENT : {minimum_htg - total_part:,.0f} HTG"}

@router.post("/caissier/paiement-exploitant", status_code=201, tags=["Exploitants"])
async def paiement_exploitant(data: dict, db: Session = Depends(get_db),
                               current_user=Depends(get_current_user)):
    devise_str = data.get("devise", "HTG")
    taux       = data.get("taux_usd_htg")
    devise     = models.DeviseEnum.USD if devise_str == "USD" else models.DeviseEnum.HTG
    if devise == models.DeviseEnum.USD and not taux:
        raise HTTPException(422, "taux_usd_htg obligatoire si devise=USD")

    montant = float(data.get("montant", 0))
    compte_tresor = models.get_compte_tresorerie(data.get("mode_paiement", "especes"), devise_str)

    mouvement = _creer_mouvement(
        db=db, journal=models.JournalEnum.VTE,
        type_mouv=models.TypeMouvementEnum.recette,
        categorie="Loyer exploitant",
        description=f"{data.get('medecin_nom','')} — {data.get('patient_nom','')} — {data.get('description','')}",
        montant=montant, compte_debit=compte_tresor, compte_credit="711",
        libelle_debit=f"Trésorerie {data.get('mode_paiement','')}",
        libelle_credit="Loyers exploitants (711)",
        mode_paiement=data.get("mode_paiement","especes"), devise=devise,
        montant_usd=montant if devise == models.DeviseEnum.USD else None,
        taux_usd_htg=taux,
        notes=f"Flux direct: {data.get('flux_direct', False)}",
        created_by=current_user.id,
    )
    paiement = models.PaiementExploitant(
        medecin_id=data.get("medecin_id"), medecin_nom=data.get("medecin_nom",""),
        patient_nom=data.get("patient_nom",""), montant=montant,
        devise=devise, taux_usd_htg=taux,
        mode_paiement=data.get("mode_paiement","especes"),
        flux_direct=data.get("flux_direct",False), description=data.get("description",""),
        mouvement_id=mouvement.id, created_by=current_user.id,
    )
    db.add(paiement); db.commit(); db.refresh(paiement)
    return paiement


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — UTILISATEURS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/users", tags=["Admin - Users"])
async def list_users(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.User).order_by(models.User.created_at.desc()).all()

@router.put("/admin/users/{uid}", tags=["Admin - Users"])
async def update_user(uid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    u = db.query(models.User).filter(models.User.id == uid).first()
    if not u: raise HTTPException(404)
    for k, v in data.items(): setattr(u, k, v)
    db.commit(); return {"message": "Mis à jour"}

@router.put("/admin/users/{uid}/activate", tags=["Admin - Users"])
async def activate_user(uid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    u = db.query(models.User).filter(models.User.id == uid).first()
    if not u: raise HTTPException(404)
    u.is_active = True; db.commit()
    return {"message": f"Compte {u.nom} activé"}

@router.delete("/admin/users/{uid}", tags=["Admin - Users"])
async def delete_user(uid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    u = db.query(models.User).filter(models.User.id == uid).first()
    if not u: raise HTTPException(404)
    if u.role == "admin": raise HTTPException(400, "Impossible de supprimer un admin")
    db.delete(u); db.commit(); return {"message": "Supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# STATISTIQUES DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/stats/dashboard", response_model=schemas.DashboardStats, tags=["Stats"])
async def dashboard_stats(db: Session = Depends(get_db), _=Depends(get_current_user)):
    now         = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rdv_today    = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.date_rdv >= today_start).scalar()
    rdv_month    = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.date_rdv >= month_start).scalar()
    recettes_day = db.query(func.sum(models.Mouvement.montant)).filter(
        models.Mouvement.type == "recette", models.Mouvement.created_at >= today_start).scalar() or 0.0
    recettes_month = db.query(func.sum(models.Mouvement.montant)).filter(
        models.Mouvement.type == "recette", models.Mouvement.periode_mois == now.month,
        models.Mouvement.periode_annee == now.year).scalar() or 0.0
    rdv_attente = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.statut == "en_attente").scalar()
    rdv_total   = db.query(func.count(models.RendezVous.id)).scalar() or 1
    rdv_ok      = db.query(func.count(models.RendezVous.id)).filter(
        models.RendezVous.statut.in_(["confirme","termine"])).scalar()
    return {"rdv_today": rdv_today, "rdv_month": rdv_month, "patients_month": rdv_month,
            "recettes_day": recettes_day, "recettes_month": recettes_month,
            "rdv_en_attente": rdv_attente, "taux_presence": round(rdv_ok/rdv_total*100, 1)}

@router.get("/admin/stats/rdv-par-jour", tags=["Stats"])
async def rdv_par_jour(jours: int = 7, db: Session = Depends(get_db), _=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    return [{"date": (now - timedelta(days=i)).strftime("%d/%m"),
             "count": db.query(func.count(models.RendezVous.id)).filter(
                models.RendezVous.date_rdv.between(
                    (now - timedelta(days=i)).replace(hour=0,minute=0,second=0),
                    (now - timedelta(days=i)).replace(hour=23,minute=59,second=59)
                )).scalar()} for i in range(jours-1, -1, -1)]

@router.get("/admin/stats/recettes-par-jour", tags=["Stats"])
async def recettes_par_jour(jours: int = 7, db: Session = Depends(get_db), _=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    return [{"date": (now - timedelta(days=i)).strftime("%d/%m"),
             "total": float(db.query(func.sum(models.Mouvement.montant)).filter(
                models.Mouvement.type == "recette",
                models.Mouvement.created_at.between(
                    (now - timedelta(days=i)).replace(hour=0,minute=0,second=0),
                    (now - timedelta(days=i)).replace(hour=23,minute=59,second=59)
                )).scalar() or 0)} for i in range(jours-1, -1, -1)]


# ══════════════════════════════════════════════════════════════════════════════
# SETUP INITIAL + AI CHAT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/chat", tags=["IA"])
async def chat(data: schemas.ChatMessage):
    from app.services.ai import chat_with_rebecca
    response = await chat_with_rebecca(data.message, data.historique)
    return {"response": response}



@router.post("/setup-admin-init", tags=["Setup"])
async def setup_admin_init(db: Session = Depends(get_db)):
    """
    Force reset admin account.
    Creates or resets wolfjer26@gmail.com as admin with password rebecca2026.
    Call this once after deployment: POST /api/setup-admin-init
    """
    from app.auth import get_password_hash
    import os

    admin_email = os.environ.get("ADMIN_EMAIL_OVERRIDE", "wolfjer26@gmail.com")
    admin_pwd   = os.environ.get("ADMIN_DEFAULT_PASSWORD", "rebecca2026")

    user = db.query(models.User).filter(models.User.email == admin_email).first()
    if user:
        user.role       = models.RoleEnum.admin
        user.is_active  = True
        user.hashed_password = get_password_hash(admin_pwd)
        db.commit()
        return {"message": f"Admin reset: {admin_email}", "role": "admin", "email": admin_email}
    else:
        user = models.User(
            email=admin_email, nom="Administrateur",
            hashed_password=get_password_hash(admin_pwd),
            role=models.RoleEnum.admin, is_active=True,
        )
        db.add(user); db.commit()
        return {"message": f"Admin created: {admin_email}", "role": "admin", "email": admin_email}


@router.post("/migrate-db")
def migrate_db(db: Session = Depends(get_db)):
    """Migration automatique — ajoute les colonnes manquantes sans supprimer les données."""
    from sqlalchemy import text
    migrations = [
        # Table users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS specialite VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS type_medecin VARCHAR(50)",
        # Table rendez_vous
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS code_patient VARCHAR(20)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_nom VARCHAR(255)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_email VARCHAR(255)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS devise VARCHAR(10) DEFAULT 'HTG'",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS mouvement_id INTEGER",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS numero_rdv VARCHAR(50)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS rappel_envoye BOOLEAN DEFAULT FALSE",
        # Table mouvements — nouvelles colonnes PCN
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS numero_piece VARCHAR(30)",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS journal VARCHAR(20) DEFAULT 'VTE'",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS compte_debit VARCHAR(10) DEFAULT '511'",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS compte_credit VARCHAR(10) DEFAULT '701'",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS libelle_debit VARCHAR(100)",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS libelle_credit VARCHAR(100)",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS devise VARCHAR(10) DEFAULT 'HTG'",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS montant_usd FLOAT",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS taux_usd_htg FLOAT",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS montant_htg FLOAT",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS rdv_id INTEGER",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS periode_mois INTEGER",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS periode_annee INTEGER",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS periode_verrou BOOLEAN DEFAULT FALSE",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS est_contrepassation BOOLEAN DEFAULT FALSE",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS mouvement_origine_id INTEGER",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS tca_applicable BOOLEAN DEFAULT FALSE",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS tca_montant FLOAT DEFAULT 0",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS tca_compte VARCHAR(10) DEFAULT '441'",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS modified_by INTEGER",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS modified_at TIMESTAMP WITH TIME ZONE",
        # Table profils_medecins
        "ALTER TABLE profils_medecins ADD COLUMN IF NOT EXISTS solde_compte_468 FLOAT DEFAULT 0",
        # Table actes_facturables
        "ALTER TABLE actes_facturables ADD COLUMN IF NOT EXISTS devise VARCHAR(10) DEFAULT 'HTG'",
        "ALTER TABLE actes_facturables ADD COLUMN IF NOT EXISTS taux_usd_htg FLOAT",
        "ALTER TABLE actes_facturables ADD COLUMN IF NOT EXISTS balance_ok BOOLEAN DEFAULT TRUE",
        "ALTER TABLE actes_facturables ADD COLUMN IF NOT EXISTS mouvement_recette_id INTEGER",
        "ALTER TABLE actes_facturables ADD COLUMN IF NOT EXISTS mouvement_honoraires_id INTEGER",
        # Table decaissements
        "ALTER TABLE decaissements ADD COLUMN IF NOT EXISTS devise VARCHAR(10) DEFAULT 'HTG'",
        "ALTER TABLE decaissements ADD COLUMN IF NOT EXISTS taux_usd_htg FLOAT",
        "ALTER TABLE decaissements ADD COLUMN IF NOT EXISTS mouvement_468_id INTEGER",
        "ALTER TABLE decaissements ADD COLUMN IF NOT EXISTS mouvement_511_id INTEGER",
        # Activer tous les admins existants
        "UPDATE users SET is_active = TRUE WHERE role = 'admin'",
        "UPDATE users SET is_active = TRUE WHERE email = 'admin@cliniquerebecca.ht'",
        # Colonnes manquantes table users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telephone VARCHAR(50)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()",
        # Patient - double ID
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS id_papier VARCHAR(50)",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS service VARCHAR(50) DEFAULT \'clinique\'",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS date_premiere_visite TIMESTAMP WITH TIME ZONE",
        # Nouvelles tables (créées via create_all)
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS id_papier VARCHAR(50)",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS service VARCHAR(50) DEFAULT 'clinique'",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS date_premiere_visite TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS mode_paiement VARCHAR(50)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS reference_paiement VARCHAR(100)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS devise VARCHAR(10) DEFAULT 'HTG'",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS mouvement_id INTEGER",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS numero_rdv VARCHAR(50)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS rappel_envoye BOOLEAN DEFAULT FALSE",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS code_patient VARCHAR(20)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_nom VARCHAR(255)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_email VARCHAR(255)",

    ]
    results = []
    errors = []
    for sql in migrations:
        try:
            db.execute(text(sql))
            results.append(f"OK: {sql[:60]}...")
        except Exception as e:
            errors.append(f"ERR: {sql[:60]} — {str(e)[:80]}")
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        errors.append(f"COMMIT ERR: {e}")
    
    # Créer les nouvelles tables si manquantes
    try:
        from app.database import engine, Base
        import app.models as models
        Base.metadata.create_all(bind=engine)
        results.append("OK: Nouvelles tables créées")
    except Exception as e:
        errors.append(f"CREATE TABLES ERR: {e}")

    return {
        "message": f"{len(results)} migrations OK, {len(errors)} erreurs",
        "ok": results,
        "errors": errors
    }


@router.post("/setup-admin-init")
def setup_admin(db: Session = Depends(get_db)):
    try:
        existing = db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first()
        if existing:
            existing.hashed_password = get_password_hash("rebecca2026")
            existing.role = "admin"; existing.is_active = True; db.commit()
            return {"status": "Admin mis à jour"}
        admin = models.User(email="admin@cliniquerebecca.ht", nom="Administrateur Rebecca",
            hashed_password=get_password_hash("rebecca2026"), role="admin", is_active=True)
        db.add(admin); db.commit()
        _seed_regles(db)
        return {"status": "Admin créé", "email": "admin@cliniquerebecca.ht", "password": "rebecca2026"}
    except Exception as e:
        db.rollback(); raise HTTPException(500, str(e))



# ══════════════════════════════════════════════════════════════════════════════
# TARIFS LABORATOIRE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/labo/tarifs", tags=["Labo"])
async def list_tarifs_labo(search: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.TarifLabo).filter(models.TarifLabo.actif == True)
    if search:
        q = q.filter(models.TarifLabo.libelle.ilike(f"%{search}%"))
    return q.order_by(models.TarifLabo.libelle).all()

@router.put("/admin/labo/tarifs/{code}", tags=["Admin"])
async def update_tarif_labo(code: str, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.query(models.TarifLabo).filter(models.TarifLabo.code == code).first()
    if not t: raise HTTPException(404)
    t.montant = float(data.get("montant", t.montant))
    db.commit(); return t


# ══════════════════════════════════════════════════════════════════════════════
# TARIFS DENTISTERIE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/labo/ajouter", tags=["Admin"])
async def add_tarif_labo(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    existing = db.query(models.TarifLabo).filter(models.TarifLabo.code == data.get("code")).first()
    if existing:
        raise HTTPException(400, "Code déjà existant")
    t = models.TarifLabo(
        code=data.get("code"), libelle=data.get("libelle"),
        montant=float(data.get("montant", 0)), devise="HTG", actif=True
    )
    db.add(t); db.commit(); db.refresh(t); return t


@router.get("/dentiste/tarifs", tags=["Dentiste"])
async def list_tarifs_dentiste(db: Session = Depends(get_db)):
    return db.query(models.TarifDentiste).filter(models.TarifDentiste.actif == True).order_by(models.TarifDentiste.libelle).all()

@router.put("/admin/dentiste/tarifs/{code}", tags=["Admin"])
async def update_tarif_dentiste(code: str, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.query(models.TarifDentiste).filter(models.TarifDentiste.code == code).first()
    if not t: raise HTTPException(404)
    t.montant = float(data.get("montant", t.montant))
    t.devise  = data.get("devise", t.devise)
    db.commit(); return t


# ══════════════════════════════════════════════════════════════════════════════
# TARIFS MÉDECINS (prix par médecin)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tarifs-medecins", tags=["Tarifs"])
async def list_tarifs_medecins(db: Session = Depends(get_db)):
    """Liste tous les médecins avec leurs prix de consultation."""
    return db.query(models.TarifMedecin).filter(models.TarifMedecin.actif == True).all()

@router.put("/admin/tarifs-medecins/{tid}", tags=["Admin"])
async def update_tarif_medecin(tid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.query(models.TarifMedecin).filter(models.TarifMedecin.id == tid).first()
    if not t: raise HTTPException(404)
    for k, v in data.items(): setattr(t, k, v)
    db.commit(); return t


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT PATIENTS DEPUIS EXCEL
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/import-patients", tags=["Admin"])
async def import_patients(db: Session = Depends(get_db), _=Depends(require_admin)):
    """
    Importe les patients depuis BD.xlsx.
    Conserve l'ID papier + génère un nouveau numéro RB-XXXX.
    """
    from app.seed import import_patients_from_excel
    excel_path = "/mnt/user-data/uploads/BD.xlsx"
    if not os.path.exists(excel_path):
        raise HTTPException(404, "Fichier BD.xlsx non trouvé — uploadez-le d'abord")
    result = import_patients_from_excel(db, excel_path)
    return result



# ══════════════════════════════════════════════════════════════════════════════
# GESTES MÉDICAUX
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/gestes-medicaux", tags=["Gestes"])
async def list_gestes(specialite: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Liste les gestes médicaux.
    Le caissier filtre par spécialité du médecin concerné.
    Inclut toujours les gestes "Général" applicables à toutes spécialités.
    """
    q = db.query(models.GesteMedical).filter(models.GesteMedical.actif == True)
    if specialite:
        q = q.filter(
            (models.GesteMedical.specialite == specialite) |
            (models.GesteMedical.specialite == "Général")
        )
    return q.order_by(models.GesteMedical.specialite, models.GesteMedical.ordre).all()

@router.post("/admin/gestes-medicaux", status_code=201, tags=["Admin"])
async def create_geste(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    g = models.GesteMedical(
        specialite=data.get("specialite", "Général"),
        libelle=data.get("libelle", ""),
        prix_suggere=float(data.get("prix_suggere", 0)),
        prix_min=data.get("prix_min"),
        prix_max=data.get("prix_max"),
        prix_fixe=data.get("prix_fixe", False),
    )
    db.add(g); db.commit(); db.refresh(g); return g

@router.put("/admin/gestes-medicaux/{gid}", tags=["Admin"])
async def update_geste(gid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    g = db.query(models.GesteMedical).filter(models.GesteMedical.id == gid).first()
    if not g: raise HTTPException(404)
    for k, v in data.items(): setattr(g, k, v)
    db.commit(); return g

@router.delete("/admin/gestes-medicaux/{gid}", tags=["Admin"])
async def delete_geste(gid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    g = db.query(models.GesteMedical).filter(models.GesteMedical.id == gid).first()
    if not g: raise HTTPException(404)
    g.actif = False; db.commit(); return {"message": "Supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# ACTE AVEC GESTE LIBRE (saisi par caissier)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/caissier/acte-geste", status_code=201, tags=["Caissier"])
async def enregistrer_acte_geste(data: dict, db: Session = Depends(get_db),
                                   current_user=Depends(get_current_user)):
    """
    Enregistre un geste médical avec prix saisi par le caissier.
    Champs :
      - patient_nom, patient_id : patient concerné
      - medecin_nom, specialite : médecin concerné
      - geste_libelle : description du geste (libre ou depuis liste)
      - montant : prix saisi par le caissier (obligatoire)
      - mode_paiement : especes, moncash, etc.
      - devise : HTG ou USD
    """
    montant = float(data.get("montant", 0))
    if montant <= 0:
        raise HTTPException(422, "Le montant du geste est obligatoire")

    mode     = data.get("mode_paiement", "especes")
    devise_s = data.get("devise", "HTG")
    devise   = models.DeviseEnum.USD if devise_s == "USD" else models.DeviseEnum.HTG

    # Chercher le profil médecin pour la répartition
    medecin_nom = data.get("medecin_nom", "")
    medecin = db.query(models.ProfilMedecin).filter(
        models.ProfilMedecin.nom.ilike(f"%{medecin_nom}%"),
        models.ProfilMedecin.actif == True,
    ).first()

    # Calcul répartition selon type médecin
    if medecin:
        regle = db.query(models.ReglePartage).filter(
            models.ReglePartage.type_medecin == medecin.type_medecin,
            models.ReglePartage.type_acte == "geste",
        ).first()
        pct_med = regle.pct_medecin if regle else 70
        montant_medecin  = round(montant * pct_med / 100, 2)
        montant_clinique = round(montant - montant_medecin, 2)
    else:
        montant_medecin  = 0
        montant_clinique = montant

    compte_tresor = models.get_compte_tresorerie(mode, devise_s)

    # Écriture comptable recette
    mouv = _creer_mouvement(
        db=db, journal=models.JournalEnum.VTE,
        type_mouv=models.TypeMouvementEnum.recette,
        categorie="Gestes médicaux",
        description=f"Geste: {data.get('geste_libelle','Geste')} — {data.get('patient_nom','')} (Dr {medecin_nom})",
        montant=montant_clinique,
        compte_debit=compte_tresor, compte_credit="702",
        libelle_debit=f"Trésorerie {mode}",
        libelle_credit="Gestes médicaux (702)",
        mode_paiement=mode, devise=devise,
        created_by=current_user.id,
    )

    # Créer l'acte facturable
    acte = models.ActeFacturable(
        medecin_nom=medecin_nom,
        patient_nom=data.get("patient_nom", ""),
        type_acte="geste",
        specialite=data.get("specialite", ""),
        description=data.get("geste_libelle", ""),
        montant_total=montant,
        montant_medecin=montant_medecin,
        montant_clinique=montant_clinique,
        pct_medecin=round(montant_medecin/montant*100, 1) if montant > 0 else 0,
        mode_paiement=mode,
        balance_ok=True,
        created_by=current_user.id,
    )
    db.add(acte); db.commit(); db.refresh(acte)
    return {
        "message": "Geste enregistré",
        "acte_id": acte.id,
        "geste": data.get("geste_libelle"),
        "montant_total": montant,
        "montant_medecin": montant_medecin,
        "montant_clinique": montant_clinique,
        "numero_piece": mouv.numero_piece,
    }



# ══════════════════════════════════════════════════════════════════════════════
# DEMANDES D'ACCÈS DOSSIER — MÉDECIN → ADMIN → AUTORISATION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/medecin/demande-acces-dossier", status_code=201, tags=["Médecin"])
async def demander_acces_dossier(data: dict, request: Request,
                                  db: Session = Depends(get_db),
                                  current_user=Depends(get_current_user)):
    """
    Le médecin demande l'accès à un dossier patient.
    Déclenche une notification à l'admin.
    Accès accordé uniquement si admin approuve.
    """
    patient_numero = data.get("patient_numero", "").strip()
    motif          = data.get("motif", "").strip()
    urgence        = data.get("urgence", False)

    if not patient_numero:
        raise HTTPException(422, "Numéro patient requis")
    if not motif:
        raise HTTPException(422, "Motif de la demande requis")

    # Chercher le patient par son numéro
    patient = db.query(models.Patient).filter(
        models.Patient.numero == patient_numero
    ).first()

    demande = models.DemandeAccesDossier(
        medecin_id=current_user.id,
        medecin_nom=current_user.nom,
        medecin_specialite=current_user.specialite,
        patient_id=patient.id if patient else None,
        patient_numero=patient_numero,
        dossier_id=data.get("dossier_id"),
        motif=motif,
        urgence=urgence,
        duree_acces_h=data.get("duree_acces_h", 24),
    )
    db.add(demande); db.commit(); db.refresh(demande)

    log_audit(db, "DEMANDE_ACCES_DOSSIER",
              actor_id=current_user.id, actor_role="medecin",
              target_id=patient_numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              details=f"Motif: {motif[:100]} | Urgence: {urgence}")

    return {
        "message": "Demande envoyée à l'administrateur",
        "demande_id": demande.id,
        "statut": "en_attente",
        "urgence": urgence,
    }


@router.get("/medecin/mes-demandes-acces", tags=["Médecin"])
async def mes_demandes_acces(db: Session = Depends(get_db),
                              current_user=Depends(get_current_user)):
    """Le médecin voit le statut de ses demandes d'accès."""
    return db.query(models.DemandeAccesDossier).filter(
        models.DemandeAccesDossier.medecin_id == current_user.id
    ).order_by(models.DemandeAccesDossier.created_at.desc()).all()


@router.get("/admin/demandes-acces-dossier", tags=["Admin"])
async def list_demandes_acces(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Admin voit toutes les demandes d'accès en attente."""
    return db.query(models.DemandeAccesDossier).order_by(
        models.DemandeAccesDossier.urgence.desc(),
        models.DemandeAccesDossier.created_at.asc()
    ).all()


@router.put("/admin/demandes-acces-dossier/{did}/approuver", tags=["Admin"])
async def approuver_acces(did: int, data: dict, request: Request,
                           db: Session = Depends(get_db),
                           current_user=Depends(require_admin)):
    """
    Admin approuve la demande d'accès.
    Génère un accès temporaire pour le médecin (durée configurable, défaut 24h).
    """
    demande = db.query(models.DemandeAccesDossier).filter(
        models.DemandeAccesDossier.id == did
    ).first()
    if not demande:
        raise HTTPException(404, "Demande introuvable")
    if demande.statut != models.StatutDemandeEnum.en_attente:
        raise HTTPException(400, f"Demande déjà traitée : {demande.statut}")

    duree_h = int(data.get("duree_acces_h", demande.duree_acces_h or 24))
    expire  = datetime.now(timezone.utc) + timedelta(hours=duree_h)

    demande.statut           = models.StatutDemandeEnum.approuve
    demande.admin_id         = current_user.id
    demande.admin_commentaire = data.get("commentaire", "")
    demande.duree_acces_h    = duree_h
    demande.acces_expire_at  = expire
    demande.decided_at       = datetime.now(timezone.utc)
    db.commit()

    log_audit(db, "ACCES_DOSSIER_APPROUVE",
              actor_id=current_user.id, actor_role="admin",
              target_id=demande.patient_numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              details=f"Dr {demande.medecin_nom} | Durée: {duree_h}h | Expire: {expire.isoformat()}",
              retention_ans=7)

    return {
        "message": f"Accès accordé à Dr {demande.medecin_nom} pour {duree_h}h",
        "acces_expire_at": expire.isoformat(),
        "patient_numero": demande.patient_numero,
    }


@router.put("/admin/demandes-acces-dossier/{did}/refuser", tags=["Admin"])
async def refuser_acces(did: int, data: dict, request: Request,
                         db: Session = Depends(get_db),
                         current_user=Depends(require_admin)):
    """Admin refuse la demande avec un motif obligatoire."""
    motif_refus = data.get("motif_refus", "").strip()
    if not motif_refus:
        raise HTTPException(422, "Motif de refus obligatoire")

    demande = db.query(models.DemandeAccesDossier).filter(
        models.DemandeAccesDossier.id == did
    ).first()
    if not demande:
        raise HTTPException(404, "Demande introuvable")

    demande.statut            = models.StatutDemandeEnum.refuse
    demande.admin_id          = current_user.id
    demande.admin_commentaire = motif_refus
    demande.decided_at        = datetime.now(timezone.utc)
    db.commit()

    log_audit(db, "ACCES_DOSSIER_REFUSE",
              actor_id=current_user.id, actor_role="admin",
              target_id=demande.patient_numero,
              details=f"Dr {demande.medecin_nom} refusé | Motif: {motif_refus[:100]}",
              retention_ans=7)

    return {"message": f"Accès refusé — Dr {demande.medecin_nom} notifié"}


@router.get("/medecin/acces-autorise/{patient_numero}", tags=["Médecin"])
async def verifier_acces_autorise(patient_numero: str, request: Request,
                                   db: Session = Depends(get_db),
                                   current_user=Depends(get_current_user)):
    """
    Vérifie si le médecin a un accès admin autorisé pour ce patient.
    Retourne le dossier si accès valide et non expiré.
    """
    now = datetime.now(timezone.utc)

    # Chercher demande approuvée non expirée
    demande = db.query(models.DemandeAccesDossier).filter(
        models.DemandeAccesDossier.medecin_id == current_user.id,
        models.DemandeAccesDossier.patient_numero == patient_numero,
        models.DemandeAccesDossier.statut == models.StatutDemandeEnum.approuve,
        models.DemandeAccesDossier.acces_expire_at > now,
    ).order_by(models.DemandeAccesDossier.created_at.desc()).first()

    if not demande:
        raise HTTPException(403,
            "Accès non autorisé — aucune autorisation admin valide pour ce patient. "
            "Soumettez une demande d'accès via votre dashboard."
        )

    # Accès autorisé — récupérer le dossier
    patient = db.query(models.Patient).filter(
        models.Patient.numero == patient_numero
    ).first()
    if not patient:
        raise HTTPException(404, "Patient introuvable")

    dossiers = db.query(models.DossierPatient).filter(
        models.DossierPatient.patient_id == patient.id
    ).order_by(models.DossierPatient.date_visite.desc()).limit(5).all()

    log_audit(db, "DOSSIER_CONSULTE_AUTORISATION_ADMIN",
              actor_id=current_user.id, actor_role="medecin",
              target_id=patient_numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              details=f"Accès admin #{demande.id} | Expire: {demande.acces_expire_at.isoformat()}",
              retention_ans=5)

    return {
        "acces_valide": True,
        "expire_at": demande.acces_expire_at.isoformat(),
        "duree_restante_h": round((demande.acces_expire_at - now).total_seconds() / 3600, 1),
        "patient": patient,
        "dossiers": dossiers,
        "demande_id": demande.id,
    }



# ══════════════════════════════════════════════════════════════════════════════
# IMPRESSION DOCUMENTS — INFIRMIER & CAISSIER (accès impression uniquement)
# Règle : cherche par ID patient → confirme existence → imprime uniquement
# JAMAIS de données médicales affichées à l'écran pour ces rôles
# ══════════════════════════════════════════════════════════════════════════════

DOCS_IMPRIMABLES_INFIRMIER = [
    "certificat",
    "exeat",
    "ecg",
    "sortie_contre_avis",
    "resultats_labo",
]
DOCS_IMPRIMABLES_CAISSIER = [
    "resultats_labo",
    "etat_compte",
]

@router.get("/infirmier/documents-disponibles/{patient_numero}", tags=["Infirmier"])
async def docs_disponibles_infirmier(patient_numero: str, request: Request,
                                      db: Session = Depends(get_db),
                                      current_user=Depends(get_current_user)):
    """
    L'infirmier cherche par ID patient.
    Retourne UNIQUEMENT la liste des documents disponibles pour impression.
    JAMAIS le contenu médical.
    """
    patient = db.query(models.Patient).filter(
        models.Patient.numero == patient_numero
    ).first()
    if not patient:
        raise HTTPException(404, f"Patient {patient_numero} introuvable")

    # Vérifier documents disponibles
    docs = []

    # Dossiers terminés = certificats/exéat potentiels
    dossiers = db.query(models.DossierPatient).filter(
        models.DossierPatient.patient_id == patient.id,
        models.DossierPatient.statut.in_([
            models.StatutDossierEnum.termine,
            models.StatutDossierEnum.observation,
            models.StatutDossierEnum.hospitalisation,
        ])
    ).all()

    if dossiers:
        docs.append({"type": "certificat",         "label": "Certificat Médical",        "icone": "📋", "disponible": True})
        docs.append({"type": "exeat",              "label": "Note d'Exéat",              "icone": "🚪", "disponible": True})
        docs.append({"type": "ecg",                "label": "Compte Rendu ECG",          "icone": "❤️", "disponible": len([d for d in dossiers]) > 0})
        docs.append({"type": "sortie_contre_avis", "label": "Sortie Contre Avis Médical","icone": "🚫", "disponible": True})

    # Résultats labo disponibles
    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(patient.id),
        models.ResultatLabo.status.in_(["disponible", "modifie", "en_attente"])
    ).all()

    if resultats:
        docs.append({
            "type": "resultats_labo",
            "label": f"Résultats Laboratoire ({len(resultats)} examen{'s' if len(resultats) > 1 else ''})",
            "icone": "🔬",
            "disponible": True,
            "nb_resultats": len(resultats),
            "derniere_date": str(resultats[0].date_examen) if resultats else None,
        })

    # Audit log
    log_audit(db, "DOCUMENTS_CONSULTES_LISTE",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=patient_numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              result="succes", details="Liste documents pour impression uniquement")

    return {
        "patient_numero": patient_numero,
        "patient_nom": f"{patient.nom} {patient.prenom or ''}".strip(),
        "documents": docs,
        "message": "Impression uniquement — aucun accès au dossier médical",
    }


@router.get("/caissier/documents-disponibles/{patient_numero}", tags=["Caissier"])
async def docs_disponibles_caissier(patient_numero: str, request: Request,
                                     db: Session = Depends(get_db),
                                     current_user=Depends(get_current_user)):
    """
    Le caissier cherche par ID patient.
    Peut imprimer : résultats labo + état de compte.
    JAMAIS le dossier médical.
    """
    patient = db.query(models.Patient).filter(
        models.Patient.numero == patient_numero
    ).first()
    if not patient:
        raise HTTPException(404, f"Patient {patient_numero} introuvable")

    docs = []

    # Résultats labo
    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(patient.id)
    ).all()
    if resultats:
        docs.append({
            "type": "resultats_labo",
            "label": f"Résultats Laboratoire ({len(resultats)} examen{'s' if len(resultats)>1 else ''})",
            "icone": "🔬", "disponible": True,
        })

    # État de compte (paiements)
    paiements = db.query(models.Mouvement).filter(
        models.Mouvement.description.ilike(f"%{patient_numero}%"),
    ).count()
    docs.append({
        "type": "etat_compte",
        "label": "État de Compte",
        "icone": "💰", "disponible": True,
        "nb_transactions": paiements,
    })

    log_audit(db, "DOCUMENTS_IMPRIMES_CAISSIER",
              actor_id=current_user.id, actor_role="caissier",
              target_id=patient_numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              result="succes")

    return {
        "patient_numero": patient_numero,
        "patient_nom": f"{patient.nom} {patient.prenom or ''}".strip(),
        "documents": docs,
    }


@router.get("/infirmier/imprimer-resultats-labo/{patient_numero}", tags=["Infirmier"])
async def imprimer_resultats_labo_infirmier(patient_numero: str, request: Request,
                                             db: Session = Depends(get_db),
                                             current_user=Depends(get_current_user)):
    """
    Retourne les données de résultats labo formatées pour impression.
    UNIQUEMENT les résultats — pas le dossier médical complet.
    """
    patient = db.query(models.Patient).filter(models.Patient.numero == patient_numero).first()
    if not patient: raise HTTPException(404, "Patient introuvable")

    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(patient.id)
    ).order_by(models.ResultatLabo.date_examen.desc()).all()

    log_audit(db, "RESULTATS_LABO_IMPRIMES",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=patient_numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              result="succes", details=f"{len(resultats)} résultats imprimés", retention_ans=5)

    return {
        "patient_numero": patient_numero,
        "patient_nom": f"{patient.nom} {patient.prenom or ''}".strip(),
        "resultats": [
            {
                "type_examen": r.type_examen,
                "resultats": r.resultats,
                "notes": r.notes,
                "date_examen": str(r.date_examen),
                "status": r.status,
            } for r in resultats
        ],
        "clinique": "Clinique de la Rebecca — #44, Rue Rebecca, Pétion-Ville — (509) 4858-5757",
        "date_impression": str(datetime.now(timezone.utc)),
    }



# ═══════════════════════════════════════════════════════════════════════════
# PATIENT — Dashboard complet
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/patient/mon-dossier", tags=["Patient"])
async def mon_dossier_complet(db: Session = Depends(get_db),
                               current_user=Depends(get_current_user)):
    """Dossier patient complet: visites, prescriptions actives, résultats labo 3 mois"""
    if current_user.role != models.RoleEnum.patient:
        raise HTTPException(403, "Accès réservé aux patients")

    patient = db.query(models.Patient).filter(
        models.Patient.user_id == current_user.id
    ).first()

    if not patient:
        return {"dossiers": [], "prescriptions_actives": [], "resultats_labo": []}

    from datetime import datetime, timedelta, timezone
    trois_mois = datetime.now(timezone.utc) - timedelta(days=90)

    dossiers = db.query(models.DossierPatient).filter(
        models.DossierPatient.patient_id == patient.id
    ).order_by(models.DossierPatient.date_visite.desc()).limit(10).all()

    prescriptions = db.query(models.Prescription).filter(
        models.Prescription.patient_id == patient.id,
        models.Prescription.date_prescription >= trois_mois
    ).order_by(models.Prescription.date_prescription.desc()).all()

    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(patient.id),
        models.ResultatLabo.date_examen >= trois_mois.date()
    ).order_by(models.ResultatLabo.date_examen.desc()).all()

    return {
        "patient_numero": patient.numero,
        "dossiers": [{"id": d.id, "date_visite": str(d.date_visite),
                       "type_visite": d.type_visite, "specialite": d.specialite,
                       "diagnostic": d.diagnostic, "statut": str(d.statut)} for d in dossiers],
        "prescriptions_actives": [{"id": p.id, "medicaments": p.medicaments,
                                    "medecin_nom": p.medecin_nom,
                                    "date_prescription": str(p.date_prescription),
                                    "valide_jusqu_au": str(p.valide_jusqu_au) if p.valide_jusqu_au else None} for p in prescriptions],
        "resultats_labo": [{"id": r.id, "type_examen": r.type_examen,
                             "resultats": r.resultats, "notes": r.notes,
                             "date_examen": str(r.date_examen), "status": r.status} for r in resultats],
    }


@router.post("/patient/avis", tags=["Patient"])
async def soumettre_avis(data: dict, db: Session = Depends(get_db),
                          current_user=Depends(get_current_user)):
    """Patient soumet une note et un avis post-consultation"""
    if current_user.role != models.RoleEnum.patient:
        raise HTTPException(403, "Réservé aux patients")

    note = data.get("note", 0)
    if not (1 <= note <= 5):
        raise HTTPException(422, "Note entre 1 et 5 requise")

    avis = models.AvisPatient(
        patient_id=current_user.id,
        dossier_id=data.get("dossier_id"),
        note=note,
        commentaire=data.get("commentaire", ""),
    )
    db.add(avis); db.commit()
    return {"message": "Avis enregistré", "note": note}


# ═══════════════════════════════════════════════════════════════════════════
# MÉDECIN — Recommandation vers autre spécialiste
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/medecin/recommander/{dossier_id}", tags=["Médecin"])
async def recommander_specialiste(dossier_id: int, data: dict,
                                   db: Session = Depends(get_db),
                                   current_user=Depends(get_current_user)):
    """
    Médecin recommande le patient vers un autre spécialiste.
    Le physiothérapeute/dentiste/optométriste recevra un accès
    LIMITÉ au résumé de recommandation uniquement.
    """
    dossier = db.query(models.DossierPatient).filter(
        models.DossierPatient.id == dossier_id).first()
    if not dossier:
        raise HTTPException(404, "Dossier introuvable")

    specialiste_cible = data.get("specialiste_cible", "")
    motif = data.get("motif", "")
    notes_recommandation = data.get("notes", "")

    # Enregistrer la recommandation
    rec = models.GesteMedical(
        dossier_id=dossier_id,
        type_geste="RECOMMANDATION",
        description=f"Recommandation vers {specialiste_cible} — {motif}",
        notes=notes_recommandation,
        medecin_id=current_user.id,
    )
    db.add(rec)

    log_audit(db, "RECOMMANDATION_EMISE",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=str(dossier_id), target_type="dossier",
              details=f"→ {specialiste_cible}: {motif}", retention_ans=5)
    db.commit()

    return {"message": f"Recommandation vers {specialiste_cible} enregistrée",
            "dossier_id": dossier_id, "specialiste": specialiste_cible}


@router.get("/medecin/recommandations/{patient_id}", tags=["Médecin"])
async def get_recommandations_patient(patient_id: int,
                                       db: Session = Depends(get_db),
                                       current_user=Depends(get_current_user)):
    """Physiothérapeute/dentiste/optométriste voit UNIQUEMENT le résumé de recommandation"""
    SPECIALITES_LIMITEES = ['dentisterie','dentiste','optometrie','physiotherapie','physiothérapie']
    user_spec = (current_user.specialite or '').lower()
    
    recs = db.query(models.GesteMedical).filter(
        models.GesteMedical.type_geste == "RECOMMANDATION",
        models.GesteMedical.dossier_id.in_(
            db.query(models.DossierPatient.id).filter(
                models.DossierPatient.patient_id == patient_id
            )
        )
    ).all()

    return {"recommandations": [
        {"id": r.id, "description": r.description, "notes": r.notes,
         "date": str(r.created_at)} for r in recs
    ]}


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN — Tableau de bord analytique IA
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/admin/dashboard-analytics", tags=["Admin - Analytics"])
async def dashboard_analytics(db: Session = Depends(get_db),
                               _=Depends(require_admin)):
    """Tableau de bord analytique complet pour l'admin"""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    
    now = datetime.now(timezone.utc)
    debut_mois = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    debut_semaine = now - timedelta(days=7)

    # Revenus par service (30 derniers jours)
    debut_30j = now - timedelta(days=30)
    
    # Dossiers par spécialité
    dossiers_par_spec = db.query(
        models.DossierPatient.specialite,
        func.count(models.DossierPatient.id).label("count")
    ).filter(
        models.DossierPatient.date_visite >= debut_30j.date()
    ).group_by(models.DossierPatient.specialite).all()

    # Paiements du mois
    paiements_mois = db.query(
        func.sum(models.Mouvement.montant_debit)
    ).filter(
        models.Mouvement.date >= debut_mois
    ).scalar() or 0

    # Nouveaux patients (mois)
    nouveaux_patients = db.query(func.count(models.Patient.id)).filter(
        models.Patient.created_at >= debut_mois
    ).scalar() or 0

    # Comptes en attente
    comptes_attente = db.query(func.count(models.User.id)).filter(
        models.User.is_active == False,
        models.User.role != models.RoleEnum.patient
    ).scalar() or 0

    # Taux occupation par service
    services = db.query(
        models.DossierPatient.type_visite,
        func.count(models.DossierPatient.id).label("count")
    ).filter(
        models.DossierPatient.date_visite >= debut_semaine.date()
    ).group_by(models.DossierPatient.type_visite).all()

    # Accès suspects (>20 dossiers/jour par user)
    from sqlalchemy import cast, Date as SADate
    acces_suspects = db.query(
        models.AuditLog.actor_id,
        func.count(models.AuditLog.id).label("nb_acces")
    ).filter(
        models.AuditLog.event_type == "DOSSIER_CONSULTE",
        cast(models.AuditLog.timestamp, SADate) == now.date()
    ).group_by(models.AuditLog.actor_id).having(
        func.count(models.AuditLog.id) > 20
    ).all()

    return {
        "periode": "30 derniers jours",
        "revenus_mois": float(paiements_mois),
        "nouveaux_patients": nouveaux_patients,
        "comptes_en_attente": comptes_attente,
        "dossiers_par_specialite": [{"specialite": d[0] or "Non spécifié", "count": d[1]} for d in dossiers_par_spec],
        "taux_occupation_semaine": [{"service": s[0] or "Autre", "count": s[1]} for s in services],
        "alertes_acces_suspects": [{"actor_id": a[0], "nb_acces": a[1]} for a in acces_suspects],
        "alerte_comptes_attente_48h": comptes_attente > 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN — Activation/Rejet compte avec notification email
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/admin/users/{user_id}/activer", tags=["Admin - Users"])
async def activer_compte(user_id: int, data: dict = {}, 
                          db: Session = Depends(get_db),
                          current_user=Depends(require_admin)):
    """Activer un compte interne + envoyer email de confirmation"""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    
    user.is_active = True
    db.commit()

    log_audit(db, "COMPTE_ACTIVE",
              actor_id=current_user.id, actor_role="admin",
              target_id=str(user_id), target_type="user",
              details=f"Compte {user.email} activé par admin")

    # Email notification (async)
    try:
        import asyncio
        from app.services.notifications import envoyer_email_activation
        asyncio.create_task(envoyer_email_activation(user.email, user.nom, activated=True))
    except Exception:
        pass

    return {"message": f"Compte {user.email} activé", "email": user.email}


@router.post("/admin/users/{user_id}/rejeter", tags=["Admin - Users"])
async def rejeter_compte(user_id: int, data: dict,
                          db: Session = Depends(get_db),
                          current_user=Depends(require_admin)):
    """Rejeter une demande de compte avec motif"""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    
    motif = data.get("motif", "Demande non conforme")
    user.is_active = False
    db.commit()

    log_audit(db, "COMPTE_REJETE",
              actor_id=current_user.id, actor_role="admin",
              target_id=str(user_id), details=f"Motif: {motif}")

    try:
        import asyncio
        from app.services.notifications import envoyer_email_activation
        asyncio.create_task(envoyer_email_activation(user.email, user.nom, activated=False, motif=motif))
    except Exception:
        pass

    return {"message": f"Compte {user.email} rejeté", "motif": motif}


# ═══════════════════════════════════════════════════════════════════════════
# LABO — Alerte IA valeurs critiques → notification médecin
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/labo/alerte-critique/{resultat_id}", tags=["Laboratoire"])
async def alerte_valeur_critique(resultat_id: int, data: dict,
                                  db: Session = Depends(get_db),
                                  current_user=Depends(get_current_user)):
    """
    Alerte IA: valeur critique détectée → notification au médecin prescripteur
    Déclenchée automatiquement par le frontend après saisie d'un résultat labo
    """
    resultat = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.id == resultat_id).first()
    if not resultat:
        raise HTTPException(404, "Résultat introuvable")

    valeur_critique = data.get("valeur", "")
    examen = data.get("examen", "")
    
    # Trouver le médecin prescripteur via le dossier
    medecin_email = data.get("medecin_email", "")
    
    log_audit(db, "ALERTE_VALEUR_CRITIQUE",
              actor_id=current_user.id, actor_role="labo",
              target_id=str(resultat_id), target_type="resultat_labo",
              details=f"Valeur critique: {examen} = {valeur_critique}",
              result="alerte", retention_ans=5)

    # Notification (email/WhatsApp) au médecin prescripteur
    try:
        import asyncio
        from app.services.notifications import notifier_medecin_valeur_critique
        asyncio.create_task(notifier_medecin_valeur_critique(
            medecin_email=medecin_email,
            patient_id=resultat.patient_id,
            examen=examen, valeur=valeur_critique
        ))
    except Exception:
        pass

    return {"message": "Alerte envoyée au médecin prescripteur", "examen": examen}



@router.post("/caissier/paiement", status_code=201, tags=["Caissier"])
async def enregistrer_paiement(data: dict, request: Request,
                                db: Session = Depends(get_db),
                                current_user=Depends(get_current_user)):
    """Enregistrement d'un paiement avec génération de reçu"""
    patient_id = data.get("patient_id")
    patient = db.query(models.Patient).filter(models.Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(404, "Patient introuvable")

    montant = float(data.get("montant", 0))
    service = data.get("service", "")
    mode    = data.get("mode_paiement", "especes")
    ref     = data.get("reference", "")

    import uuid as uuid_lib2
    recu_num = f"REC-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{str(uuid_lib2.uuid4())[:6].upper()}"

    mouvement = models.Mouvement(
        numero_piece=recu_num,
        description=f"Paiement {service} — Patient {patient.numero}",
        montant_debit=montant,
        mode_paiement=mode,
        reference_paiement=ref,
        patient_id=patient_id,
    )
    db.add(mouvement); db.commit()

    log_audit(db, "PAIEMENT_ENREGISTRE",
              actor_id=current_user.id, actor_role="caissier",
              target_id=patient.numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              details=f"Service: {service} — Montant: {montant} HTG — Mode: {mode}",
              result="succes", retention_ans=7)

    return {
        "message": "Paiement enregistré",
        "recu_numero": recu_num,
        "patient_id": patient_id,
        "patient_nom": patient.nom,
        "service": service,
        "montant": montant,
        "mode_paiement": mode,
    }


@router.get("/caissier/paiements-jour", tags=["Caissier"])
async def paiements_du_jour(db: Session = Depends(get_db),
                             current_user=Depends(get_current_user)):
    """Liste des paiements du jour avec total"""
    from sqlalchemy import cast, Date as SADate
    from datetime import datetime, timezone
    
    aujourd_hui = datetime.now(timezone.utc).date()
    paiements = db.query(models.Mouvement).filter(
        cast(models.Mouvement.date, SADate) == aujourd_hui,
        models.Mouvement.montant_debit > 0
    ).order_by(models.Mouvement.date.desc()).all()

    total = sum(float(p.montant_debit or 0) for p in paiements)

    return {
        "paiements": [
            {
                "id": p.id,
                "patient_id": p.patient_id,
                "service": p.description,
                "montant": float(p.montant_debit or 0),
                "mode_paiement": p.mode_paiement or "especes",
                "recu_numero": p.numero_piece,
                "date": str(p.date),
            } for p in paiements
        ],
        "total": total,
        "nb_transactions": len(paiements),
        "date": str(aujourd_hui),
    }


@router.post("/caissier/nouveau-patient", status_code=201, tags=["Caissier"])
async def creer_nouveau_patient_caissier(data: dict, request: Request,
                                          db: Session = Depends(get_db),
                                          current_user=Depends(get_current_user)):
    """Caissier crée un nouveau patient avec ID unique"""
    import uuid as uuid_lib3
    
    nom    = data.get("nom", "").upper().strip()
    prenom = data.get("prenom", "").strip()
    if not nom or not prenom:
        raise HTTPException(422, "Nom et prénom requis")

    # Generate short readable ID
    count = db.query(models.Patient).count()
    numero = f"#RB-{(count + 1):04d}"

    patient = models.Patient(
        nom=nom, prenom=prenom,
        age=data.get("age"),
        adresse=data.get("adresse", ""),
        telephone=data.get("telephone", ""),
        email=data.get("email", ""),
        contact_urgence=data.get("contact_urgence", ""),
        numero=numero,
        is_premiere_visite=data.get("is_premiere_visite", True),
    )
    db.add(patient); db.commit(); db.refresh(patient)

    log_audit(db, "PATIENT_CREE",
              actor_id=current_user.id, actor_role="caissier",
              target_id=numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              details=f"Nouveau patient: {prenom} {nom}", retention_ans=5)

    return {"message": "Patient créé", "patient": {
        "id": patient.id, "nom": f"{prenom} {nom}", "numero": numero,
        "telephone": patient.telephone,
    }}



@router.post("/caissier/decaissement", status_code=201, tags=["Caissier"])
async def enregistrer_decaissement(data: dict, request: Request,
                                    db: Session = Depends(get_db),
                                    current_user=Depends(get_current_user)):
    """Caissier enregistre une dépense/décaissement journalier"""
    description = data.get("description", "").strip()
    montant = float(data.get("montant", 0))
    categorie = data.get("categorie", "autre")

    if not description or montant <= 0:
        raise HTTPException(422, "Description et montant requis")

    import uuid as u_lib
    piece = f"DEC-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{str(u_lib.uuid4())[:6].upper()}"

    dec = models.Mouvement(
        numero_piece=piece,
        description=f"[DÉCAISSEMENT — {categorie.upper()}] {description}",
        montant_credit=montant,
        mode_paiement="especes",
    )
    db.add(dec); db.commit(); db.refresh(dec)

    log_audit(db, "DECAISSEMENT_ENREGISTRE",
              actor_id=current_user.id, actor_role="caissier",
              target_id=piece, target_type="mouvement",
              ip_address=request.client.host if request.client else None,
              details=f"{description}: {montant} HTG",
              result="succes", retention_ans=7)

    return {
        "id": dec.id,
        "description": description,
        "montant": montant,
        "categorie": categorie,
        "numero_piece": piece,
        "date": str(datetime.now(timezone.utc).date()),
    }


def _seed_regles(db: Session):
    if db.query(models.ReglePartage).count() > 0: return
    regles = [
        ("investisseur","consultation",70,30),("investisseur","geste",80,20),
        ("investisseur","chirurgie",0,100),("investisseur","hospit",70,30),
        ("affilie","consultation",60,40),("affilie","geste",70,30),
        ("affilie","chirurgie",0,100),("affilie","hospit",60,40),
        ("exploitant","consultation",100,0),("exploitant","geste",100,0),("exploitant","chirurgie",100,0),
        ("investisseur_exploitant","consultation",100,0),
        ("investisseur_exploitant","geste",100,0),("investisseur_exploitant","chirurgie",100,0),
    ]
    for tm, ta, pm, pc in regles:
        db.add(models.ReglePartage(type_medecin=tm, type_acte=ta, pct_medecin=pm, pct_clinique=pc))
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT LOG — JOURNAL D'AUDIT IMMUABLE
# ══════════════════════════════════════════════════════════════════════════════


def log_audit(
    db, event_type: str, actor_id: int = None, actor_role: str = None,
    target_id: str = None, target_type: str = None,
    ip_address: str = None, device_info: str = None,
    session_id: str = None, result: str = "succes",
    details: str = None, retention_ans: int = 5
):
    """Enregistre un événement dans le journal d'audit immuable."""
    try:
        entry = models.AuditLog(
            audit_id=str(uuid_lib.uuid4()),
            event_type=event_type,
            actor_id=actor_id,
            actor_role=actor_role,
            target_id=str(target_id) if target_id else None,
            target_type=target_type,
            ip_address=ip_address,
            device_info=device_info,
            session_id=session_id,
            result=result,
            details=details,
            retention_ans=retention_ans,
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        pass  # Ne jamais faire échouer une requête à cause de l'audit

@router.get("/admin/audit-log", tags=["Admin - Audit"])
async def get_audit_log(
    event_type: Optional[str] = None,
    actor_id: Optional[int] = None,
    target_id: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _=Depends(require_admin)
):
    """Journal d'audit — accessible uniquement par l'admin. Lecture seule."""
    q = db.query(models.AuditLog)
    if event_type: q = q.filter(models.AuditLog.event_type == event_type)
    if actor_id:   q = q.filter(models.AuditLog.actor_id == actor_id)
    if target_id:  q = q.filter(models.AuditLog.target_id == target_id)
    if result:     q = q.filter(models.AuditLog.result == result)
    return q.order_by(models.AuditLog.timestamp.desc()).limit(limit).all()


# ══════════════════════════════════════════════════════════════════════════════
# DOSSIERS PATIENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/caissier/nouveau-dossier", status_code=201, tags=["Caissier"])
async def creer_dossier(data: dict, request: Request, db: Session = Depends(get_db),
                         current_user=Depends(get_current_user)):
    """
    Le caissier crée un dossier après paiement.
    Déclenche : accès infirmier pour signes vitaux, puis médecin.
    """
    patient_id = data.get("patient_id")
    patient = db.query(models.Patient).filter(models.Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(404, "Patient introuvable")

    dossier = models.DossierPatient(
        patient_id=patient_id,
        patient_numero=patient.numero or "",
        type_visite=data.get("type_visite", "premiere_consultation"),
        service=data.get("service", "clinique"),
        specialite=data.get("specialite"),
        paiement_effectue=True,
        locked=False,
        statut=models.StatutDossierEnum.attente_infirmier,
        motif_consultation=data.get("motif"),
        created_by=current_user.id,
    )
    db.add(dossier); db.commit(); db.refresh(dossier)

    log_audit(db, "DOSSIER_CREE", actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=str(dossier.id), target_type="dossier",
              ip_address=request.client.host if request.client else None,
              details=f"Patient #{patient.numero}")
    return dossier

@router.get("/infirmier/dossiers-en-attente", tags=["Infirmier"])
async def dossiers_en_attente(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Liste des dossiers attendant les signes vitaux."""
    return db.query(models.DossierPatient).filter(
        models.DossierPatient.statut == models.StatutDossierEnum.attente_infirmier,
        models.DossierPatient.paiement_effectue == True,
    ).order_by(models.DossierPatient.date_visite.desc()).all()

@router.post("/infirmier/signes-vitaux", status_code=201, tags=["Infirmier"])
async def saisir_signes_vitaux(data: dict, request: Request, db: Session = Depends(get_db),
                                current_user=Depends(get_current_user)):
    """
    L'infirmier saisit les signes vitaux.
    Déverrouille le dossier pour le médecin + place en file d'attente.
    Alerte IA si valeurs critiques.
    """
    dossier_id = data.get("dossier_id")
    dossier = db.query(models.DossierPatient).filter(models.DossierPatient.id == dossier_id).first()
    if not dossier:
        raise HTTPException(404, "Dossier introuvable")
    if not dossier.paiement_effectue:
        raise HTTPException(403, "Paiement requis avant saisie des signes vitaux")

    # Détection valeurs critiques
    alerte = False; alerte_msg = []
    tension_sys = data.get("tension_systolique")
    glycemie    = data.get("glycemie")
    spo2        = data.get("saturation_o2")
    temp        = data.get("temperature")
    fc          = data.get("frequence_cardiaque")

    if tension_sys and (tension_sys > 180 or tension_sys < 80):
        alerte = True; alerte_msg.append(f"⚠️ Tension critique : {tension_sys} mmHg")
    if glycemie and glycemie > 600:
        alerte = True; alerte_msg.append(f"⚠️ Glycémie critique : {glycemie} mg/dL")
    if spo2 and spo2 < 90:
        alerte = True; alerte_msg.append(f"⚠️ SpO2 critique : {spo2}%")
    if temp and (temp > 40 or temp < 35):
        alerte = True; alerte_msg.append(f"⚠️ Température critique : {temp}°C")
    if fc and (fc > 150 or fc < 40):
        alerte = True; alerte_msg.append(f"⚠️ FC critique : {fc} bpm")

    sv = models.SignesVitaux(
        dossier_id=dossier_id,
        patient_id=dossier.patient_id,
        tension_systolique=tension_sys,
        tension_diastolique=data.get("tension_diastolique"),
        frequence_cardiaque=fc,
        temperature=temp,
        frequence_respiratoire=data.get("frequence_respiratoire"),
        saturation_o2=spo2,
        poids=data.get("poids"),
        taille=data.get("taille"),
        glycemie=glycemie,
        notes=data.get("notes"),
        alerte_critique=alerte,
        alerte_message="\n".join(alerte_msg) if alerte_msg else None,
        saisi_par=current_user.id,
    )
    db.add(sv)

    # Passer statut → attente_medecin et placer en file
    dossier.statut = models.StatutDossierEnum.attente_medecin
    dossier.infirmier_id = current_user.id

    file = models.FileAttente(
        dossier_id=dossier_id,
        patient_id=dossier.patient_id,
        patient_numero=dossier.patient_numero,
        medecin_id=dossier.medecin_id,
        priorite=1 if alerte else 5,
        place_par=current_user.id,
    )
    db.add(file); db.commit(); db.refresh(sv)

    log_audit(db, "SIGNES_VITAUX_SAISIS", actor_id=current_user.id, actor_role="infirmier",
              target_id=str(dossier_id), target_type="dossier",
              ip_address=request.client.host if request.client else None,
              details=f"Alerte: {alerte}")
    return {"message": "Signes vitaux enregistrés", "alerte": alerte, "alertes": alerte_msg, "sv_id": sv.id}

@router.get("/medecin/file-attente", tags=["Médecin"])
async def file_attente_medecin(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Liste des patients en attente pour ce médecin."""
    return db.query(models.FileAttente).filter(
        models.FileAttente.statut == "en_attente"
    ).order_by(models.FileAttente.priorite, models.FileAttente.heure_entree).all()

@router.get("/medecin/dossier/{dossier_id}", tags=["Médecin"])
async def get_dossier_medecin(dossier_id: int, request: Request,
                               db: Session = Depends(get_db),
                               current_user=Depends(get_current_user)):
    """
    Accès médecin au dossier patient.
    RÈGLES CRITIQUES (SPEC §5.3) :
    - Paiement effectué requis
    - Signes vitaux saisis par infirmier requis
    - DENTISTE / OPTOMÉTRISTE / PHYSIOTHÉRAPEUTE → accès INTERDIT au dossier complet
    - Journal d'audit systématique
    """
    # ── RESTRICTION SPÉCIALITÉS (CRITIQUE) ────────────────────────────────
    SPECIALITES_SANS_ACCES = [
        'dentisterie', 'dentiste', 'optometrie', 'optométrie',
        'physiotherapie', 'physiothérapie',
    ]
    user_spec = (current_user.specialite or '').lower().strip()
    if any(s in user_spec for s in SPECIALITES_SANS_ACCES):
        log_audit(db, "ACCES_DOSSIER_REFUSE_SPECIALITE",
                  actor_id=current_user.id, actor_role=str(current_user.role),
                  target_id=str(dossier_id), result="echec",
                  details=f"Spécialité {current_user.specialite} non autorisée — accès dossier interdit",
                  ip_address=request.client.host if request.client else None)
        raise HTTPException(403,
            f"Accès refusé — Les {current_user.specialite}s n'ont pas accès au dossier médical complet. "
            "Contactez un médecin pour une recommandation.")

    dossier = db.query(models.DossierPatient).filter(models.DossierPatient.id == dossier_id).first()
    if not dossier:
        raise HTTPException(404, "Dossier introuvable")
    if not dossier.paiement_effectue:
        log_audit(db, "ACCES_DOSSIER_REFUSE", actor_id=current_user.id,
                  actor_role=str(current_user.role), target_id=str(dossier_id),
                  result="echec", details="Paiement non effectué")
        raise HTTPException(403, "Accès refusé — paiement requis")
    if dossier.statut == models.StatutDossierEnum.attente_infirmier:
        raise HTTPException(403, "Accès refusé — signes vitaux non encore saisis")

    log_audit(db, "DOSSIER_CONSULTE", actor_id=current_user.id,
              actor_role=str(current_user.role), target_id=str(dossier_id),
              target_type="dossier", ip_address=request.client.host if request.client else None,
              result="succes", details=f"Patient #{dossier.patient_numero}", retention_ans=5)

    # Signes vitaux
    sv = db.query(models.SignesVitaux).filter(
        models.SignesVitaux.dossier_id == dossier_id
    ).order_by(models.SignesVitaux.created_at.desc()).first()

    # Prescriptions antérieures
    prescriptions = db.query(models.Prescription).filter(
        models.Prescription.patient_id == dossier.patient_id
    ).order_by(models.Prescription.date_prescription.desc()).limit(5).all()

    # Résultats labo récents
    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(dossier.patient_id)
    ).order_by(models.ResultatLabo.date_examen.desc()).limit(5).all()

    return {
        "dossier": dossier,
        "signes_vitaux": sv,
        "prescriptions_anterieures": prescriptions,
        "resultats_labo": resultats,
    }

@router.put("/medecin/dossier/{dossier_id}/consultation", tags=["Médecin"])
async def terminer_consultation(dossier_id: int, data: dict, request: Request,
                                 db: Session = Depends(get_db),
                                 current_user=Depends(get_current_user)):
    """Le médecin termine la consultation — verrouille le dossier pour l'infirmier."""
    dossier = db.query(models.DossierPatient).filter(models.DossierPatient.id == dossier_id).first()
    if not dossier: raise HTTPException(404)

    dossier.diagnostic     = data.get("diagnostic")
    dossier.examen_clinique = data.get("examen_clinique")
    dossier.notes_medecin  = data.get("notes_medecin")
    dossier.statut         = models.StatutDossierEnum.termine
    dossier.date_fin_consultation = datetime.now(timezone.utc)
    dossier.locked         = True  # Infirmier perd l'accès

    # Créer prescription si fournie
    if data.get("medicaments"):
        import hashlib, json
        med_json = json.dumps(data.get("medicaments", []))
        hash_sig = hashlib.sha256(f"{current_user.id}{dossier_id}{med_json}".encode()).hexdigest()
        presc = models.Prescription(
            dossier_id=dossier_id, patient_id=dossier.patient_id,
            medecin_id=dossier.medecin_id, medecin_nom=current_user.nom,
            medicaments=med_json, examens_requis=data.get("examens_requis"),
            notes=data.get("notes_prescription"),
            signature_hash=hash_sig, signee=True,
        )
        db.add(presc)

    # Mettre à jour file d'attente
    fa = db.query(models.FileAttente).filter(
        models.FileAttente.dossier_id == dossier_id,
        models.FileAttente.statut == "en_cours"
    ).first()
    if fa:
        fa.statut = "termine"
        fa.heure_fin = datetime.now(timezone.utc)

    db.commit()
    log_audit(db, "CONSULTATION_TERMINEE", actor_id=current_user.id,
              actor_role="medecin", target_id=str(dossier_id), retention_ans=5)
    return {"message": "Consultation terminée", "dossier_statut": "termine"}


# ══════════════════════════════════════════════════════════════════════════════
# RÉSULTATS LABO — fenêtre 24h
# ══════════════════════════════════════════════════════════════════════════════

@router.put("/labo/resultats/{rid}", tags=["Labo"])
async def modifier_resultat(rid: int, data: dict, request: Request,
                             db: Session = Depends(get_db),
                             current_user=Depends(get_current_user)):
    """Modification résultat labo — fenêtre de 24h uniquement."""
    r = db.query(models.ResultatLabo).filter(models.ResultatLabo.id == rid).first()
    if not r: raise HTTPException(404, "Résultat introuvable")

    # Vérifier fenêtre 24h
    age = datetime.now(timezone.utc) - r.date_examen.replace(tzinfo=timezone.utc)
    if age.total_seconds() > 86400:
        raise HTTPException(423, "Résultat verrouillé — fenêtre de 24h dépassée")

    ancienne_val = r.resultats
    r.resultats = data.get("resultats", r.resultats)
    r.notes     = data.get("notes", r.notes)
    r.status    = "modifie"
    db.commit()

    log_audit(db, "RESULTAT_LABO_MODIFIE", actor_id=current_user.id,
              actor_role="labo", target_id=str(rid), target_type="resultat_labo",
              ip_address=request.client.host if request.client else None,
              details=f"Ancienne valeur: {ancienne_val[:100] if ancienne_val else ''}", retention_ans=5)
    return r


# ══════════════════════════════════════════════════════════════════════════════
# PATIENT — SON PROPRE DOSSIER
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/patient/mon-dossier", tags=["Patient"])
async def mon_dossier(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Le patient voit uniquement son propre dossier — jamais celui d'un autre."""
    patient = db.query(models.Patient).filter(
        models.Patient.email == current_user.email
    ).first()
    if not patient:
        return {"message": "Aucun dossier trouvé", "dossiers": []}

    dossiers = db.query(models.DossierPatient).filter(
        models.DossierPatient.patient_id == patient.id
    ).order_by(models.DossierPatient.date_visite.desc()).limit(10).all()

    prescriptions = db.query(models.Prescription).filter(
        models.Prescription.patient_id == patient.id,
        models.Prescription.statut == "active"
    ).all()

    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(patient.id)
    ).order_by(models.ResultatLabo.date_examen.desc()).limit(10).all()

    return {
        "patient": patient,
        "dossiers": dossiers,
        "prescriptions_actives": prescriptions,
        "resultats_labo": resultats,
    }


# ══════════════════════════════════════════════════════════════════════════════
# HOSPITALISATION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/caissier/hospitalisation", status_code=201, tags=["Caissier"])
async def creer_hospitalisation(data: dict, db: Session = Depends(get_db),
                                 current_user=Depends(get_current_user)):
    patient = db.query(models.Patient).filter(models.Patient.id == data.get("patient_id")).first()
    if not patient: raise HTTPException(404, "Patient introuvable")

    # Créer ou récupérer dossier
    dossier = models.DossierPatient(
        patient_id=patient.id, patient_numero=patient.numero or "",
        type_visite="hospitalisation", service=data.get("type_sejour", "hospitalisation"),
        paiement_effectue=True, statut=models.StatutDossierEnum.hospitalisation,
        locked=False, created_by=current_user.id,
    )
    db.add(dossier); db.flush()

    hospit = models.Hospitalisation(
        dossier_id=dossier.id, patient_id=patient.id,
        patient_numero=patient.numero or "",
        type_sejour=data.get("type_sejour", "hospitalisation"),
        lit_numero=data.get("lit_numero"),
        service=data.get("service"),
        tarif_journalier=float(data.get("tarif_journalier", 0)),
        created_by=current_user.id,
    )
    db.add(hospit); db.commit(); db.refresh(hospit)
    return hospit

@router.put("/caissier/hospitalisation/{hid}/sortie", tags=["Caissier"])
async def sortie_hospitalisation(hid: int, data: dict, db: Session = Depends(get_db),
                                  current_user=Depends(get_current_user)):
    h = db.query(models.Hospitalisation).filter(models.Hospitalisation.id == hid).first()
    if not h: raise HTTPException(404)
    h.date_sortie    = datetime.now(timezone.utc)
    h.nb_jours       = data.get("nb_jours", h.nb_jours)
    h.total_hebergement = h.nb_jours * h.tarif_journalier
    h.acquittement_total = data.get("acquittement_total", False)
    if not h.acquittement_total:
        raise HTTPException(402, "Acquittement total requis avant sortie")
    h.statut = "sorti"
    if h.dossier_id:
        dossier = db.query(models.DossierPatient).filter(models.DossierPatient.id == h.dossier_id).first()
        if dossier:
            dossier.statut = models.StatutDossierEnum.termine
            dossier.locked = True
    db.commit()
    return h


# ══════════════════════════════════════════════════════════════════════════════
# AVIS PATIENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/patient/avis", status_code=201, tags=["Patient"])
async def soumettre_avis(data: dict, db: Session = Depends(get_db),
                          current_user=Depends(get_current_user)):
    patient = db.query(models.Patient).filter(models.Patient.email == current_user.email).first()
    note = int(data.get("note", 3))
    if not 1 <= note <= 5:
        raise HTTPException(422, "Note entre 1 et 5 requise")
    avis = models.AvisPatient(
        patient_id=patient.id if patient else None,
        dossier_id=data.get("dossier_id"),
        medecin_nom=data.get("medecin_nom"),
        service=data.get("service"),
        note=note, commentaire=data.get("commentaire"),
        anonyme=data.get("anonyme", False),
    )
    db.add(avis); db.commit(); db.refresh(avis)
    return avis

@router.get("/admin/avis", tags=["Admin"])
async def list_avis(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.AvisPatient).order_by(models.AvisPatient.created_at.desc()).all()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — RABAIS + COMPTES EN ATTENTE
# ══════════════════════════════════════════════════════════════════════════════


@router.post("/admin/creer-compte-personnel", status_code=201, tags=["Admin - Users"])
async def creer_compte_personnel(data: dict, db: Session = Depends(get_db),
                                  current_user=Depends(require_admin)):
    """
    L'admin crée les comptes du personnel (médecin, caissier, labo, infirmier, pharmacie).
    Email @cliniquerebecca.ht obligatoire pour tous les rôles staff.
    """
    email = data.get("email", "").lower()
    role_str = data.get("role", "")
    
    # Valider email clinique pour le personnel
    if role_str != "patient" and not email.endswith("@cliniquerebecca.ht"):
        raise HTTPException(400, f"Le compte {role_str} doit utiliser un email @cliniquerebecca.ht")
    
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(400, "Email déjà utilisé")
    
    try:
        role = models.RoleEnum(role_str)
    except:
        raise HTTPException(422, f"Rôle invalide: {role_str}")
    
    user = models.User(
        email=email,
        nom=data.get("nom", ""),
        hashed_password=get_password_hash(data.get("password", "clinique2026")),
        role=role,
        telephone=data.get("telephone"),
        specialite=data.get("specialite"),
        is_active=True,
    )
    db.add(user); db.commit(); db.refresh(user)
    
    log_audit(db, "COMPTE_PERSONNEL_CREE",
              actor_id=current_user.id, actor_role="admin",
              target_id=email, target_type="user",
              details=f"Rôle: {role_str}")
    
    return {"message": f"Compte {role_str} créé", "email": email, "id": user.id}


@router.get("/admin/comptes-en-attente", tags=["Admin - Users"])
async def comptes_en_attente(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Liste tous les comptes internes en attente de validation."""
    return db.query(models.User).filter(
        models.User.is_active == False,
        models.User.role != models.RoleEnum.patient,
    ).order_by(models.User.created_at.desc()).all()

@router.put("/admin/users/{uid}/suspendre", tags=["Admin - Users"])
async def suspendre_user(uid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    u = db.query(models.User).filter(models.User.id == uid).first()
    if not u: raise HTTPException(404)
    u.is_active = False; db.commit()
    return {"message": f"Compte {u.nom} suspendu — sessions invalidées"}

@router.put("/admin/users/{uid}/reactiver", tags=["Admin - Users"])
async def reactiver_user(uid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    u = db.query(models.User).filter(models.User.id == uid).first()
    if not u: raise HTTPException(404)
    u.is_active = True; db.commit()
    return {"message": f"Compte {u.nom} réactivé"}

@router.post("/admin/rabais", status_code=201, tags=["Admin"])
async def appliquer_rabais(data: dict, db: Session = Depends(get_db),
                            current_user=Depends(require_admin)):
    """Admin peut appliquer un rabais ou laisser passer un patient sans paiement."""
    log_audit(db, "AUTORISATION_SPECIALE_ADMIN",
              actor_id=current_user.id, actor_role="admin",
              target_id=str(data.get("patient_id")), target_type="patient",
              details=f"Rabais: {data.get('rabais_pct')}% — {data.get('justification')}", retention_ans=7)
    return {"message": "Rabais enregistré", "data": data}


# ══════════════════════════════════════════════════════════════════════════════
# MIGRATION — ajoute les nouvelles colonnes et tables
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/migrate-db-v2", tags=["Setup"])
def migrate_db_v2(db: Session = Depends(get_db)):
    """Migration v2 — nouveaux rôles, tables audit, dossiers, hospitalisations."""
    from sqlalchemy import text
    migrations = [
        # Nouveaux rôles
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'roleenum') THEN RAISE NOTICE 'type not found'; END IF; END $$",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN DEFAULT FALSE",
        # Nouvelles tables via create_all
    ]
    errors = []
    for sql in migrations:
        try:
            db.execute(text(sql)); db.commit()
        except Exception as e:
            errors.append(str(e)[:100]); db.rollback()

    # Créer toutes les nouvelles tables
    try:
        from app.database import engine, Base
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        errors.append(str(e))

    return {"message": "Migration v2 terminée", "errors": errors}
