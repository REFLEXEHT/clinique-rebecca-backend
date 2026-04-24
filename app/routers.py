from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import app.models as models
import app.schemas as schemas
from app.database import get_db
from app.auth import (get_current_user, require_admin,
                      verify_password, get_password_hash, create_access_token)
from app.services.notifications import notify_rdv_confirmed
from app.services.ai import chat_with_rebecca
import asyncio

router = APIRouter()

# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auth/login", response_model=schemas.Token, tags=["Auth"])
async def login(data: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Identifiants incorrects")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Compte inactif — en attente de validation")
    token = create_access_token({"sub": str(user.id)})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id, "nom": user.nom, "email": user.email,
            "role": user.role, "specialite": user.specialite,
        },
    }


@router.post("/auth/register", tags=["Auth"])
async def register(data: schemas.UserCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == data.email).first():
        raise HTTPException(400, "Email déjà utilisé")
    is_active = data.role == models.RoleEnum.patient
    user = models.User(
        email=data.email, nom=data.nom,
        hashed_password=get_password_hash(data.password),
        role=data.role, telephone=data.telephone,
        specialite=data.specialite,
        type_medecin=data.type_medecin,
        is_active=is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Si médecin → créer automatiquement le profil comptable
    if data.role == models.RoleEnum.medecin and data.type_medecin:
        profil = models.ProfilMedecin(
            user_id=user.id, nom=user.nom,
            specialite=data.specialite,
            type_medecin=data.type_medecin,
        )
        db.add(profil)
        db.commit()

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
# SERVICES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/services", response_model=List[schemas.ServiceOut], tags=["Services"])
async def list_services(db: Session = Depends(get_db)):
    return db.query(models.Service).filter(models.Service.actif == True).order_by(models.Service.ordre).all()

@router.post("/admin/services", response_model=schemas.ServiceOut, tags=["Admin"])
async def create_service(data: schemas.ServiceCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    svc = models.Service(**data.model_dump())
    db.add(svc); db.commit(); db.refresh(svc)
    return svc

@router.put("/admin/services/{sid}", response_model=schemas.ServiceOut, tags=["Admin"])
async def update_service(sid: int, data: schemas.ServiceUpdate, db: Session = Depends(get_db), _=Depends(require_admin)):
    svc = db.query(models.Service).filter(models.Service.id == sid).first()
    if not svc: raise HTTPException(404, "Service introuvable")
    for k, v in data.model_dump(exclude_none=True).items(): setattr(svc, k, v)
    db.commit(); db.refresh(svc)
    return svc

@router.delete("/admin/services/{sid}", tags=["Admin"])
async def delete_service(sid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    svc = db.query(models.Service).filter(models.Service.id == sid).first()
    if not svc: raise HTTPException(404, "Service introuvable")
    svc.actif = False; db.commit()
    return {"message": "Supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# SPECIALISTES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/specialistes", response_model=List[schemas.SpecialisteOut], tags=["Spécialistes"])
async def list_specialistes(categorie: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.Specialiste).filter(models.Specialiste.actif == True)
    if categorie and categorie != "tous":
        q = q.filter(models.Specialiste.categorie.in_([categorie, "tous"]))
    return q.order_by(models.Specialiste.ordre).all()

@router.get("/specialistes/{spec_id}", response_model=schemas.SpecialisteOut, tags=["Spécialistes"])
async def get_specialiste(spec_id: int, db: Session = Depends(get_db)):
    spec = db.query(models.Specialiste).filter(models.Specialiste.id == spec_id).first()
    if not spec: raise HTTPException(404, "Spécialiste introuvable")
    return spec

@router.post("/admin/specialistes", response_model=schemas.SpecialisteOut, tags=["Admin"])
async def create_specialiste(data: schemas.SpecialisteCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    spec = models.Specialiste(**data.model_dump())
    db.add(spec); db.commit(); db.refresh(spec)
    return spec

@router.put("/admin/specialistes/{spec_id}", response_model=schemas.SpecialisteOut, tags=["Admin"])
async def update_specialiste(spec_id: int, data: schemas.SpecialisteUpdate, db: Session = Depends(get_db), _=Depends(require_admin)):
    spec = db.query(models.Specialiste).filter(models.Specialiste.id == spec_id).first()
    if not spec: raise HTTPException(404, "Spécialiste introuvable")
    for k, v in data.model_dump(exclude_none=True).items(): setattr(spec, k, v)
    db.commit(); db.refresh(spec)
    return spec

@router.delete("/admin/specialistes/{spec_id}", tags=["Admin"])
async def delete_specialiste(spec_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    spec = db.query(models.Specialiste).filter(models.Specialiste.id == spec_id).first()
    if not spec: raise HTTPException(404, "Spécialiste introuvable")
    spec.actif = False; db.commit()
    return {"message": "Supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# HORAIRES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/horaires", response_model=List[schemas.HoraireOut], tags=["Horaires"])
async def get_horaires(db: Session = Depends(get_db)):
    return db.query(models.Horaire).order_by(models.Horaire.id).all()

@router.put("/admin/horaires/{jour}", response_model=schemas.HoraireOut, tags=["Admin"])
async def update_horaire(jour: str, data: schemas.HoraireUpdate, db: Session = Depends(get_db), _=Depends(require_admin)):
    h = db.query(models.Horaire).filter(models.Horaire.jour == jour).first()
    if not h: raise HTTPException(404, "Jour introuvable")
    h.ouvert = data.ouvert; h.heure_ouverture = data.heure_ouverture; h.heure_fermeture = data.heure_fermeture
    db.commit(); db.refresh(h)
    return h


# ══════════════════════════════════════════════════════════════════════════════
# RENDEZ-VOUS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/rendez-vous", response_model=schemas.RendezVousOut, status_code=201, tags=["RDV"])
async def create_rdv(data: schemas.RendezVousCreate, db: Session = Depends(get_db)):
    rdv = models.RendezVous(**data.model_dump())
    db.add(rdv); db.commit(); db.refresh(rdv)
    rdv_data = {k: getattr(rdv, k) for k in ["patient_nom","patient_telephone","patient_email","specialite","date_rdv","type_rdv","motif"]}
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
    for k, v in data.model_dump(exclude_none=True).items(): setattr(rdv, k, v)
    db.commit(); db.refresh(rdv)
    return rdv

@router.delete("/admin/rendez-vous/{rdv_id}", tags=["Admin"])
async def cancel_rdv(rdv_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv: raise HTTPException(404, "RDV introuvable")
    rdv.statut = "annule"; db.commit()
    return {"message": "RDV annulé"}

@router.get("/medecin/rendez-vous", response_model=List[schemas.RendezVousOut], tags=["Médecin"])
async def medecin_rdv(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
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
async def encaisser(rdv_id: int, data: dict, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv: raise HTTPException(404, "RDV introuvable")
    mouvement = models.Mouvement(
        type=models.TypeMouvementEnum.recette,
        categorie=data.get("categorie", "Consultations"),
        description=f"Encaissement RDV #{rdv_id} — {rdv.patient_nom} — {rdv.specialite}",
        montant=float(data.get("montant", 0)),
        date_mouvement=datetime.now(timezone.utc),
        mode_paiement=data.get("mode_paiement", "especes"),
        created_by=current_user.id,
    )
    db.add(mouvement)
    rdv.statut = "confirme"
    db.commit()
    return {"message": "Encaissement enregistré", "mouvement_id": mouvement.id}


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
    patient = models.Patient(
        numero=f"#RB-{str(count + 1).zfill(4)}",
        **data.model_dump(),
        created_by=current_user.id,
    )
    db.add(patient); db.commit(); db.refresh(patient)
    return patient

@router.get("/patients/search", tags=["Patients"])
async def search_patients(q: str = "", db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Patient).filter(
        models.Patient.nom.ilike(f"%{q}%") | models.Patient.numero.ilike(f"%{q}%") | models.Patient.telephone.ilike(f"%{q}%")
    ).limit(20).all()

@router.get("/patients/{pid}", tags=["Patients"])
async def get_patient(pid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    p = db.query(models.Patient).filter(models.Patient.id == pid).first()
    if not p: raise HTTPException(404, "Patient non trouvé")
    return p


# ══════════════════════════════════════════════════════════════════════════════
# PROFILS MÉDECINS (comptabilité)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/profils-medecins", response_model=List[schemas.ProfilMedecinOut], tags=["Admin - Compta"])
async def list_profils(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.ProfilMedecin).filter(models.ProfilMedecin.actif == True).all()

@router.put("/admin/profils-medecins/{pid}", tags=["Admin - Compta"])
async def update_profil(pid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.query(models.ProfilMedecin).filter(models.ProfilMedecin.id == pid).first()
    if not p: raise HTTPException(404, "Profil introuvable")
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
    if not r: raise HTTPException(404, "Règle introuvable")
    r.pct_medecin = pct_medecin; r.pct_clinique = 100 - pct_medecin
    db.commit(); return r


# ══════════════════════════════════════════════════════════════════════════════
# ACTES FACTURABLES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/actes-facturables", response_model=List[schemas.ActeOut], tags=["Admin - Compta"])
async def list_actes(mois: Optional[int] = None, annee: Optional[int] = None, db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(models.ActeFacturable)
    if annee: q = q.filter(extract("year", models.ActeFacturable.date_acte) == annee)
    if mois:  q = q.filter(extract("month", models.ActeFacturable.date_acte) == mois)
    return q.order_by(models.ActeFacturable.date_acte.desc()).all()

@router.post("/actes-facturables", response_model=schemas.ActeOut, status_code=201, tags=["Admin - Compta"])
async def create_acte(data: schemas.ActeCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    medecin = None
    if data.medecin_id:
        medecin = db.query(models.ProfilMedecin).filter(models.ProfilMedecin.id == data.medecin_id).first()

    # Calcul automatique selon type médecin et type acte
    if data.montant_medecin_manuel is not None and data.montant_clinique_manuel is not None:
        montant_medecin  = data.montant_medecin_manuel
        montant_clinique = data.montant_clinique_manuel
        pct_medecin      = round(montant_medecin / data.montant_total * 100, 1) if data.montant_total else 0
    elif medecin:
        regle = db.query(models.ReglePartage).filter(
            models.ReglePartage.type_medecin == medecin.type_medecin,
            models.ReglePartage.type_acte == data.type_acte
        ).first()
        if regle:
            pct_medecin      = regle.pct_medecin
            montant_medecin  = round(data.montant_total * pct_medecin / 100, 2)
            montant_clinique = data.montant_total - montant_medecin
        else:
            # Règles par défaut si aucune règle configurée
            DEFAUTS = {
                "investisseur": {"consultation": 70, "geste": 80, "chirurgie": 0},
                "affilie":      {"consultation": 60, "geste": 70, "chirurgie": 0},
                "exploitant":   {"consultation": 100, "geste": 100, "chirurgie": 100},
                "investisseur_exploitant": {"consultation": 100, "geste": 100, "chirurgie": 100},
            }
            type_m = str(medecin.type_medecin.value) if medecin.type_medecin else "affilie"
            type_a = data.type_acte if data.type_acte in ["consultation","geste","chirurgie"] else "consultation"
            pct_medecin      = DEFAUTS.get(type_m, {}).get(type_a, 60)
            montant_medecin  = round(data.montant_total * pct_medecin / 100, 2)
            montant_clinique = data.montant_total - montant_medecin
    else:
        pct_medecin = 0; montant_medecin = 0; montant_clinique = data.montant_total

    acte = models.ActeFacturable(
        medecin_id=data.medecin_id,
        medecin_nom=medecin.nom if medecin else None,
        patient_nom=data.patient_nom,
        type_acte=data.type_acte,
        specialite=data.specialite,
        description=data.description,
        montant_total=data.montant_total,
        montant_medecin=montant_medecin,
        montant_clinique=montant_clinique,
        pct_medecin=pct_medecin,
        mode_paiement=data.mode_paiement,
        created_by=current_user.id,
    )
    db.add(acte)
    # Enregistrer la part clinique en mouvement comptable
    cat_map = {"consultation": "Consultations", "geste": "Gestes médicaux",
               "chirurgie": "Chirurgies", "hospit": "Hospitalisations", "observation": "Hospitalisations"}
    mouvement = models.Mouvement(
        type=models.TypeMouvementEnum.recette,
        categorie=cat_map.get(data.type_acte, "Consultations"),
        description=f"{data.type_acte.capitalize()} — {data.patient_nom}" + (f" (Dr {medecin.nom})" if medecin else ""),
        montant=montant_clinique,
        date_mouvement=datetime.now(timezone.utc),
        mode_paiement=data.mode_paiement,
        created_by=current_user.id,
    )
    db.add(mouvement)
    db.commit(); db.refresh(acte)
    return acte


# ══════════════════════════════════════════════════════════════════════════════
# DÉCAISSEMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/decaissements", response_model=List[schemas.DecaissementOut], tags=["Admin - Compta"])
async def list_decaissements(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Decaissement).order_by(models.Decaissement.date_decaissement.desc()).all()

@router.post("/admin/decaissements", status_code=201, tags=["Admin - Compta"])
async def create_decaissement(data: schemas.DecaissementCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # Récupérer nom médecin si pas fourni
    if not data.medecin_nom:
        profil = db.query(models.ProfilMedecin).filter(models.ProfilMedecin.id == data.medecin_id).first()
        nom = profil.nom if profil else "Inconnu"
    else:
        nom = data.medecin_nom

    dec = models.Decaissement(
        medecin_id=data.medecin_id, medecin_nom=nom,
        montant=data.montant, motif=data.motif,
        mode_paiement=data.mode_paiement, created_by=current_user.id,
    )
    db.add(dec)
    mouvement = models.Mouvement(
        type=models.TypeMouvementEnum.depense,
        categorie="Décaissements médecins",
        description=f"Décaissement Dr {nom} — {data.motif}",
        montant=data.montant, date_mouvement=datetime.now(timezone.utc),
        mode_paiement=data.mode_paiement, created_by=current_user.id,
    )
    db.add(mouvement)
    # Marquer les actes de ce médecin comme décaissés
    db.query(models.ActeFacturable).filter(
        models.ActeFacturable.medecin_id == data.medecin_id,
        models.ActeFacturable.statut_decaissement == "en_attente"
    ).update({"statut_decaissement": "decaisse"})
    db.commit(); db.refresh(dec)
    return dec


# ══════════════════════════════════════════════════════════════════════════════
# MOUVEMENTS COMPTABLES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/mouvements", response_model=List[schemas.MouvementOut], tags=["Admin - Compta"])
async def list_mouvements(type: Optional[str] = None, mois: Optional[int] = None,
    annee: Optional[int] = None, db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(models.Mouvement)
    if type:  q = q.filter(models.Mouvement.type == type)
    if annee: q = q.filter(extract("year",  models.Mouvement.date_mouvement) == annee)
    if mois:  q = q.filter(extract("month", models.Mouvement.date_mouvement) == mois)
    return q.order_by(models.Mouvement.date_mouvement.desc()).all()

@router.post("/admin/mouvements", response_model=schemas.MouvementOut, status_code=201, tags=["Admin - Compta"])
async def create_mouvement(data: schemas.MouvementCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    m = models.Mouvement(**data.model_dump(), created_by=current_user.id)
    db.add(m); db.commit(); db.refresh(m)
    return m

@router.delete("/admin/mouvements/{mid}", tags=["Admin - Compta"])
async def delete_mouvement(mid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    m = db.query(models.Mouvement).filter(models.Mouvement.id == mid).first()
    if not m: raise HTTPException(404, "Mouvement introuvable")
    db.delete(m); db.commit()
    return {"message": "Supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# BILANS MENSUELS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/bilans", tags=["Admin - Compta"])
async def list_bilans(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.BilanMensuel).order_by(models.BilanMensuel.annee.desc(), models.BilanMensuel.mois.desc()).all()

@router.post("/admin/generer-bilan", tags=["Admin - Compta"])
async def generer_bilan(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    mois  = data.get("mois")
    annee = data.get("annee")

    def sum_cat(cat, type_mouv="recette"):
        return db.query(func.sum(models.Mouvement.montant)).filter(
            models.Mouvement.type == type_mouv,
            models.Mouvement.categorie.ilike(f"%{cat}%"),
            extract("month", models.Mouvement.date_mouvement) == mois,
            extract("year",  models.Mouvement.date_mouvement) == annee,
        ).scalar() or 0.0

    def sum_all(type_mouv):
        return db.query(func.sum(models.Mouvement.montant)).filter(
            models.Mouvement.type == type_mouv,
            extract("month", models.Mouvement.date_mouvement) == mois,
            extract("year",  models.Mouvement.date_mouvement) == annee,
        ).scalar() or 0.0

    tot_cons  = sum_cat("Consultations")
    tot_gest  = sum_cat("Gestes")
    tot_chir  = sum_cat("Chirurgie")
    tot_hosp  = sum_cat("Hospitalisation")
    tot_labo  = sum_cat("Laboratoire")
    tot_phar  = sum_cat("Pharmacie")
    tot_loyer = sum_cat("Loyer") + sum_cat("Exploitant") + sum_cat("Optométrie")
    tot_autres_prod = sum_all("recette") - tot_cons - tot_gest - tot_chir - tot_hosp - tot_labo - tot_phar - tot_loyer
    total_produits  = sum_all("recette")

    tot_dec   = sum_cat("Décaissements", "depense")
    tot_sal   = sum_cat("Salaires", "depense") + sum_cat("RH", "depense")
    tot_phar_ach = sum_cat("Pharmacie achats", "depense")
    tot_infra = sum_cat("Infrastructure", "depense") + sum_cat("Énergie", "depense")
    tot_autres_ch = sum_all("depense") - tot_dec - tot_sal - tot_phar_ach - tot_infra
    total_charges  = sum_all("depense")

    # Upsert bilan
    bilan = db.query(models.BilanMensuel).filter(
        models.BilanMensuel.mois == mois, models.BilanMensuel.annee == annee
    ).first()
    if not bilan:
        bilan = models.BilanMensuel(mois=mois, annee=annee)
        db.add(bilan)

    bilan.total_consultations = tot_cons
    bilan.total_gestes = tot_gest
    bilan.total_chirurgies = tot_chir
    bilan.total_hospitalisations = tot_hosp
    bilan.total_laboratoire = tot_labo
    bilan.total_pharmacie = tot_phar
    bilan.total_loyers_recus = tot_loyer
    bilan.total_autres_produits = max(tot_autres_prod, 0)
    bilan.total_produits = total_produits
    bilan.total_decaissements_medecins = tot_dec
    bilan.total_salaires = tot_sal
    bilan.total_pharmacie_achats = tot_phar_ach
    bilan.total_infrastructure = tot_infra
    bilan.total_autres_charges = max(tot_autres_ch, 0)
    bilan.total_charges = total_charges
    bilan.resultat_net = total_produits - total_charges
    bilan.statut = "brouillon"
    db.commit(); db.refresh(bilan)
    return bilan

@router.put("/admin/bilans/{bid}/valider", tags=["Admin - Compta"])
async def valider_bilan(bid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    b = db.query(models.BilanMensuel).filter(models.BilanMensuel.id == bid).first()
    if not b: raise HTTPException(404, "Bilan introuvable")
    b.statut = "valide"; db.commit()
    return b

@router.get("/admin/rapport-cumul", tags=["Admin - Compta"])
async def rapport_cumul(mois_debut: int, annee_debut: int, mois_fin: int, annee_fin: int,
    db: Session = Depends(get_db), _=Depends(require_admin)):
    bilans = db.query(models.BilanMensuel).all()
    bilans_filtre = []
    for b in bilans:
        if (b.annee > annee_debut or (b.annee == annee_debut and b.mois >= mois_debut)) and \
           (b.annee < annee_fin  or (b.annee == annee_fin  and b.mois <= mois_fin)):
            bilans_filtre.append(b)

    total_produits = sum(b.total_produits for b in bilans_filtre)
    total_charges  = sum(b.total_charges  for b in bilans_filtre)
    return {
        "periode": f"{mois_debut}/{annee_debut} — {mois_fin}/{annee_fin}",
        "nb_mois": len(bilans_filtre),
        "total_produits": total_produits,
        "total_charges": total_charges,
        "resultat_net": total_produits - total_charges,
        "detail_produits": {
            "consultations": sum(b.total_consultations for b in bilans_filtre),
            "gestes": sum(b.total_gestes for b in bilans_filtre),
            "chirurgies": sum(b.total_chirurgies for b in bilans_filtre),
            "laboratoire": sum(b.total_laboratoire for b in bilans_filtre),
            "pharmacie": sum(b.total_pharmacie for b in bilans_filtre),
            "loyers": sum(b.total_loyers_recus for b in bilans_filtre),
            "autres": sum(b.total_autres_produits for b in bilans_filtre),
        },
        "detail_charges": {
            "decaissements_medecins": sum(b.total_decaissements_medecins for b in bilans_filtre),
            "salaires": sum(b.total_salaires for b in bilans_filtre),
            "pharmacie_achats": sum(b.total_pharmacie_achats for b in bilans_filtre),
            "infrastructure": sum(b.total_infrastructure for b in bilans_filtre),
            "autres": sum(b.total_autres_charges for b in bilans_filtre),
        },
        "bilans_mensuels": [{"mois": b.mois, "annee": b.annee,
            "produits": b.total_produits, "charges": b.total_charges,
            "resultat": b.resultat_net, "statut": b.statut} for b in bilans_filtre],
    }


# ══════════════════════════════════════════════════════════════════════════════
# TARIFS CLINIC
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/tarifs-clinic", tags=["Admin - Compta"])
async def list_tarifs(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.TarifClinic).all()

@router.put("/admin/tarifs-clinic/{code}", tags=["Admin - Compta"])
async def update_tarif(code: str, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.query(models.TarifClinic).filter(models.TarifClinic.code == code).first()
    if not t: raise HTTPException(404, "Tarif introuvable")
    t.montant = data.get("montant", t.montant); db.commit()
    return t


# ══════════════════════════════════════════════════════════════════════════════
# STOCKS PHARMACIE
# ══════════════════════════════════════════════════════════════════════════════

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
    db.add(item); db.commit(); db.refresh(item)
    return item

@router.put("/admin/stocks/{sid}", tags=["Admin"])
async def update_stock(sid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = db.query(models.StockItem).filter(models.StockItem.id == sid).first()
    if not s: raise HTTPException(404, "Stock introuvable")
    for k, v in data.items(): setattr(s, k, v)
    db.commit(); return s

@router.delete("/admin/stocks/{sid}", tags=["Admin"])
async def delete_stock(sid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = db.query(models.StockItem).filter(models.StockItem.id == sid).first()
    if not s: raise HTTPException(404)
    db.delete(s); db.commit()
    return {"message": "Supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# LABORATOIRE
# ══════════════════════════════════════════════════════════════════════════════

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
    db.add(r); db.commit(); db.refresh(r)
    return r

@router.put("/labo/analyses/{aid}", tags=["Labo"])
async def update_analyse(aid: int, data: dict, db: Session = Depends(get_db), _=Depends(get_current_user)):
    r = db.query(models.ResultatLabo).filter(models.ResultatLabo.id == aid).first()
    if not r: raise HTTPException(404)
    for k, v in data.items(): setattr(r, k, v)
    db.commit(); return r

@router.get("/patient/resultats-labo/{patient_id}", tags=["Patient"])
async def patient_resultats(patient_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.ResultatLabo).filter(models.ResultatLabo.patient_id == patient_id).all()


# ══════════════════════════════════════════════════════════════════════════════
# PAIEMENTS EXPLOITANTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/caissier/paiement-exploitant", status_code=201, tags=["Exploitants"])
async def paiement_exploitant(data: dict, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    paiement = models.PaiementExploitant(
        medecin_id=data.get("medecin_id"), medecin_nom=data.get("medecin_nom", ""),
        patient_nom=data.get("patient_nom", ""), montant=float(data.get("montant", 0)),
        mode_paiement=data.get("mode_paiement", "especes"),
        flux_direct=data.get("flux_direct", False),
        description=data.get("description", ""), created_by=current_user.id,
    )
    db.add(paiement)
    mouvement = models.Mouvement(
        type=models.TypeMouvementEnum.recette, categorie="Exploitant",
        description=f"{data.get('medecin_nom','')} — {data.get('patient_nom','')} — {data.get('description','')}",
        montant=float(data.get("montant", 0)),
        date_mouvement=datetime.now(timezone.utc),
        mode_paiement=data.get("mode_paiement", "especes"),
        notes=f"Flux direct: {data.get('flux_direct', False)}",
        created_by=current_user.id,
    )
    db.add(mouvement); db.commit(); db.refresh(paiement)
    return paiement


# ══════════════════════════════════════════════════════════════════════════════
# OPTOMÉTRIE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/contrat-optometrie", tags=["Optométrie"])
async def get_contrat(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.ContratOptometrie).first()

@router.put("/admin/contrat-optometrie", tags=["Optométrie"])
async def update_contrat(data: dict, db: Session = Depends(get_db), current_user=Depends(require_admin)):
    c = db.query(models.ContratOptometrie).first()
    if not c:
        c = models.ContratOptometrie(); db.add(c)
    for k, v in data.items(): setattr(c, k, v)
    c.updated_by = current_user.id
    db.commit(); return c

@router.post("/admin/calculer-optometrie", tags=["Optométrie"])
async def calculer_optomet(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    c = db.query(models.ContratOptometrie).first()
    if not c: raise HTTPException(404, "Contrat non configuré")
    total_consul  = float(data.get("total_consultations", 0))
    total_montures = float(data.get("total_montures", 0))
    part_consul   = round(total_consul   * c.pct_consultation / 100, 2)
    part_montures = round(total_montures * c.pct_montures     / 100, 2)
    total_part    = part_consul + part_montures
    minimum_htg   = round(c.minimum_mensuel_usd * c.taux_usd_htg, 2)
    montant_final = max(total_part, minimum_htg)
    difference    = total_part - minimum_htg
    bilan = models.BilanOptometrieMensuel(
        mois=data.get("mois"), annee=data.get("annee"),
        total_consultations=total_consul, total_montures=total_montures,
        part_clinique_consultations=part_consul, part_clinique_montures=part_montures,
        total_part_clinique=total_part, minimum_applicable_htg=minimum_htg,
        montant_final_clinique=montant_final, difference=difference,
    )
    db.add(bilan); db.commit()
    return {
        "mois": data.get("mois"), "annee": data.get("annee"),
        "part_clinique_consultations": part_consul,
        "part_clinique_montures": part_montures,
        "total_part_clinique": total_part,
        "minimum_mensuel_usd": c.minimum_mensuel_usd,
        "minimum_htg": minimum_htg,
        "montant_final_clinique": montant_final,
        "difference": difference,
        "verdict": "OK — % supérieur au minimum" if difference >= 0 else f"COMPLÉMENT REQUIS: {abs(difference):,.0f} HTG",
    }


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — UTILISATEURS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/users", tags=["Admin - Users"])
async def list_users(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.User).order_by(models.User.created_at.desc()).all()

@router.put("/admin/users/{uid}", tags=["Admin - Users"])
async def update_user(uid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    u = db.query(models.User).filter(models.User.id == uid).first()
    if not u: raise HTTPException(404, "Utilisateur introuvable")
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
    db.delete(u); db.commit()
    return {"message": "Supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# STATS DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/stats/dashboard", response_model=schemas.DashboardStats, tags=["Stats"])
async def dashboard_stats(db: Session = Depends(get_db), _=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rdv_today    = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.date_rdv >= today_start).scalar()
    rdv_month    = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.date_rdv >= month_start).scalar()
    recettes_day = db.query(func.sum(models.Mouvement.montant)).filter(models.Mouvement.type == "recette", models.Mouvement.date_mouvement >= today_start).scalar() or 0.0
    recettes_month = db.query(func.sum(models.Mouvement.montant)).filter(models.Mouvement.type == "recette", models.Mouvement.date_mouvement >= month_start).scalar() or 0.0
    rdv_en_attente = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.statut == "en_attente").scalar()
    rdv_total    = db.query(func.count(models.RendezVous.id)).scalar() or 1
    rdv_ok       = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.statut.in_(["confirme","termine"])).scalar()
    return {"rdv_today": rdv_today, "rdv_month": rdv_month, "patients_month": rdv_month,
            "recettes_day": recettes_day, "recettes_month": recettes_month,
            "rdv_en_attente": rdv_en_attente, "taux_presence": round(rdv_ok/rdv_total*100, 1)}

@router.get("/admin/stats/rdv-par-jour", tags=["Stats"])
async def rdv_par_jour(jours: int = 7, db: Session = Depends(get_db), _=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    result = []
    for i in range(jours - 1, -1, -1):
        day = now - timedelta(days=i)
        ds = day.replace(hour=0, minute=0, second=0); de = day.replace(hour=23, minute=59, second=59)
        count = db.query(func.count(models.RendezVous.id)).filter(models.RendezVous.date_rdv.between(ds, de)).scalar()
        result.append({"date": day.strftime("%d/%m"), "count": count})
    return result

@router.get("/admin/stats/recettes-par-jour", tags=["Stats"])
async def recettes_par_jour(jours: int = 7, db: Session = Depends(get_db), _=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    result = []
    for i in range(jours - 1, -1, -1):
        day = now - timedelta(days=i)
        ds = day.replace(hour=0, minute=0, second=0); de = day.replace(hour=23, minute=59, second=59)
        total = db.query(func.sum(models.Mouvement.montant)).filter(
            models.Mouvement.type == "recette", models.Mouvement.date_mouvement.between(ds, de)
        ).scalar() or 0
        result.append({"date": day.strftime("%d/%m"), "total": float(total)})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# AI CHAT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/chat", tags=["IA"])
async def chat(data: schemas.ChatMessage):
    response = await chat_with_rebecca(data.message, data.historique)
    return {"response": response}


# ══════════════════════════════════════════════════════════════════════════════
# SETUP ADMIN (à supprimer après premier déploiement)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/setup-admin-init")
def setup_admin(db: Session = Depends(get_db)):
    try:
        existing = db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first()
        if existing:
            existing.hashed_password = get_password_hash("rebecca2026")
            existing.role = "admin"; existing.is_active = True
            db.commit()
            return {"status": "Admin mis à jour", "email": "admin@cliniquerebecca.ht"}
        admin = models.User(email="admin@cliniquerebecca.ht", nom="Administrateur",
            hashed_password=get_password_hash("rebecca2026"), role="admin", is_active=True)
        db.add(admin); db.commit()
        # Seed règles de partage par défaut
        _seed_regles(db)
        return {"status": "Admin créé", "email": "admin@cliniquerebecca.ht", "password": "rebecca2026"}
    except Exception as e:
        db.rollback(); raise HTTPException(500, str(e))


def _seed_regles(db: Session):
    """Crée les règles de répartition par défaut si elles n'existent pas."""
    if db.query(models.ReglePartage).count() > 0:
        return
    regles = [
        # Investisseur
        ("investisseur", "consultation", 70, 30),
        ("investisseur", "geste",        80, 20),
        ("investisseur", "chirurgie",     0, 100),  # Manuel
        ("investisseur", "hospit",        70, 30),
        # Affilié
        ("affilie", "consultation", 60, 40),
        ("affilie", "geste",        70, 30),
        ("affilie", "chirurgie",     0, 100),
        ("affilie", "hospit",        60, 40),
        # Exploitant — 100% médecin
        ("exploitant", "consultation", 100, 0),
        ("exploitant", "geste",        100, 0),
        ("exploitant", "chirurgie",    100, 0),
        # Investisseur-Exploitant — 100% médecin
        ("investisseur_exploitant", "consultation", 100, 0),
        ("investisseur_exploitant", "geste",        100, 0),
        ("investisseur_exploitant", "chirurgie",    100, 0),
    ]
    for type_m, type_a, pct_med, pct_clin in regles:
        db.add(models.ReglePartage(type_medecin=type_m, type_acte=type_a,
                                    pct_medecin=pct_med, pct_clinique=pct_clin))
    db.commit()
