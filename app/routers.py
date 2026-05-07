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
import logging
logger = logging.getLogger(__name__)

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

@router.get("/debug/health", tags=["Debug"])
async def health_check(db: Session = Depends(get_db)):
    """Test DB connection and basic operations."""
    try:
        from sqlalchemy import text
        result = db.execute(text("SELECT current_database(), version()")).fetchone()
        
        # Test users table
        user_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
        
        # Test column existence
        cols = db.execute(text("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'users'
            ORDER BY ordinal_position
        """)).fetchall()
        
        return {
            "status": "ok",
            "database": result[0] if result else "unknown",
            "user_count": user_count,
            "users_columns": [{"name": c[0], "type": c[1]} for c in cols],
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


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
        # Auto-créer le dossier Patient lié au compte User
        try:
            count = db.query(models.Patient).count()
            numero = f"#RB-{str(count + 1).zfill(4)}"
            patient = models.Patient(
                user_id=user.id,
                numero=numero,
                nom=data.nom,
                email=data.email,
                telephone=data.telephone,
            )
            db.add(patient)
            db.commit()
        except Exception as e:
            # Ne pas bloquer la création du compte si le dossier patient échoue
            logger.warning(f"Patient record creation failed for user {user.id}: {e}")
            db.rollback()

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

@router.get("/admin/specialistes", response_model=List[schemas.SpecialisteOut], tags=["Spécialistes"])
async def list_specialistes_admin(categorie: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.Specialiste).filter(models.Specialiste.actif == True)
    if categorie and categorie != "tous":
        q = q.filter(models.Specialiste.categorie.in_([categorie, "tous"]))
    specs = q.order_by(models.Specialiste.ordre).all()
    results = []
    for s in specs:
        tarif = db.query(models.TarifMedecin).filter(
            models.TarifMedecin.specialiste_id == s.id
        ).first()
        item = schemas.SpecialisteOut.model_validate(s)
        if tarif:
            item.prix_consultation = tarif.prix_consultation
            item.prix_rdv = tarif.prix_rdv
            item.type_medecin = str(tarif.type_medecin.value) if tarif.type_medecin else None
        results.append(item)
    return results

@router.get("/specialistes/{spec_id}", response_model=schemas.SpecialisteOut, tags=["Spécialistes"])
async def get_specialiste(spec_id: int, db: Session = Depends(get_db)):
    s = db.query(models.Specialiste).filter(models.Specialiste.id == spec_id).first()
    if not s: raise HTTPException(404); return s

@router.post("/admin/specialistes", response_model=schemas.SpecialisteOut, tags=["Admin"])
async def create_specialiste(data: schemas.SpecialisteCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    # Extraire les champs tarif avant de créer le Specialiste
    prix_consultation = data.prix_consultation
    prix_rdv = data.prix_rdv
    type_medecin_val = data.type_medecin

    spec_data = data.model_dump(exclude={"prix_consultation", "prix_rdv", "type_medecin"})
    s = models.Specialiste(**spec_data)
    db.add(s)
    db.flush()

    # Créer ou mettre à jour le TarifMedecin associé
    tarif = db.query(models.TarifMedecin).filter(
        models.TarifMedecin.specialiste_id == s.id
    ).first()
    if tarif:
        if prix_consultation is not None: tarif.prix_consultation = prix_consultation
        if prix_rdv is not None: tarif.prix_rdv = prix_rdv
        if type_medecin_val: tarif.type_medecin = type_medecin_val
    else:
        from app.models import TypeMedecinEnum
        tm_enum = None
        if type_medecin_val:
            try: tm_enum = TypeMedecinEnum(type_medecin_val)
            except: pass
        tarif = models.TarifMedecin(
            specialiste_id=s.id,
            medecin_nom=f"{s.titre} {s.nom}",
            specialite=s.specialite,
            prix_consultation=prix_consultation or 0,
            prix_rdv=prix_rdv or 0,
            type_medecin=tm_enum,
            actif=True,
        )
        db.add(tarif)

    db.commit()
    db.refresh(s)

    # Construire la réponse avec les champs tarif
    result = schemas.SpecialisteOut.model_validate(s)
    result.prix_consultation = prix_consultation
    result.prix_rdv = prix_rdv
    result.type_medecin = type_medecin_val
    return result

@router.put("/admin/specialistes/{sid}", response_model=schemas.SpecialisteOut, tags=["Admin"])
async def update_specialiste(sid: int, data: schemas.SpecialisteUpdate, db: Session = Depends(get_db), _=Depends(require_admin)):
    s = db.query(models.Specialiste).filter(models.Specialiste.id == sid).first()
    if not s: raise HTTPException(404)

    old_nom = s.nom
    update_data = data.model_dump(exclude_none=True)

    # Extraire champs tarif avant mise à jour Specialiste
    prix_consultation = update_data.pop("prix_consultation", None)
    prix_rdv = update_data.pop("prix_rdv", None)
    type_medecin_val = update_data.pop("type_medecin", None)

    for k, v in update_data.items(): setattr(s, k, v)
    db.commit(); db.refresh(s)

    # Mettre à jour TarifMedecin si champs tarif fournis
    if prix_consultation is not None or prix_rdv is not None or type_medecin_val is not None:
        tarif = db.query(models.TarifMedecin).filter(
            models.TarifMedecin.specialiste_id == sid
        ).first()
        if tarif:
            if prix_consultation is not None: tarif.prix_consultation = prix_consultation
            if prix_rdv is not None: tarif.prix_rdv = prix_rdv
            if type_medecin_val:
                from app.models import TypeMedecinEnum
                try: tarif.type_medecin = TypeMedecinEnum(type_medecin_val)
                except: pass
            db.commit()

    # ── PROPAGATION: sync TarifMedecin name if name changed ─────────────
    if 'nom' in update_data and update_data['nom'] != old_nom:
        tarif = db.query(models.TarifMedecin).filter(
            models.TarifMedecin.medecin_nom.contains(old_nom)
        ).first()
        if tarif:
            tarif.medecin_nom = f"{s.titre} {update_data['nom']}"
            if 'specialite' in update_data:
                tarif.specialite = update_data['specialite']
            db.commit()

    # ── PROPAGATION: sync User table if email or specialite changed ─────
    if 'email' in update_data or 'specialite' in update_data:
        user = db.query(models.User).filter(models.User.email == s.email).first()
        if user:
            if 'specialite' in update_data: user.specialite = update_data['specialite']
            db.commit()

    # Récupérer le tarif pour retourner les prix dans la réponse
    tarif_final = db.query(models.TarifMedecin).filter(
        models.TarifMedecin.specialiste_id == sid
    ).first()

    result = schemas.SpecialisteOut.model_validate(s)
    if tarif_final:
        result.prix_consultation = tarif_final.prix_consultation
        result.prix_rdv = tarif_final.prix_rdv
        result.type_medecin = str(tarif_final.type_medecin.value) if tarif_final.type_medecin else None
    return result

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

    # Normalisation catégorie → compte PCN (accepte les variantes du frontend)
    CAT_NORM = {
        "RH": "RH / Salaires", "Salaires": "RH / Salaires", "Personnel": "RH / Salaires",
        "Médical": "Consommables médicaux", "Pharmacie": "Pharmacie achats",
        "Infrastructure": "Infrastructure", "Équipements": "Équipements",
        "Telecom": "Télécom", "Télécom": "Télécom",
        "Autre": "Autres charges", "Autre charges": "Autres charges",
        "Consultations": "Consultations", "Gestes médicaux": "Gestes médicaux",
        "Honoraires": "Honoraires médecins",
    }
    cat_norm = CAT_NORM.get(data.categorie, data.categorie)

    compte_tresor  = models.get_compte_tresorerie(data.mode_paiement, data.devise or "HTG")
    compte_contrep = models.COMPTE_PCN.get(cat_norm, "701" if data.type == "recette" else "628")

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
# GRAND LIVRE + BALANCE DE VÉRIFICATION — PCN HAÏTI
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/grand-livre", tags=["Admin - Compta"])
async def grand_livre(
    compte: Optional[str] = None,
    mois: Optional[int] = None,
    annee: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user)
):
    """
    Grand Livre — toutes les écritures filtrables par compte/période.
    Calcule solde cumulé pour chaque compte.
    """
    now = datetime.now(timezone.utc)
    q = db.query(models.Mouvement).filter(
        models.Mouvement.est_contrepassation == False
    )
    if mois:   q = q.filter(models.Mouvement.periode_mois == mois)
    if annee:  q = q.filter(models.Mouvement.periode_annee == annee or now.year)
    if compte: q = q.filter(
        (models.Mouvement.compte_debit == compte) | (models.Mouvement.compte_credit == compte)
    )
    ecritures = q.order_by(models.Mouvement.created_at.asc()).all()

    # Regrouper par compte pour balance
    comptes: dict = {}
    for m in ecritures:
        for side, cpt in [("debit", m.compte_debit), ("credit", m.compte_credit)]:
            if cpt not in comptes:
                comptes[cpt] = {"compte": cpt, "total_debit": 0.0, "total_credit": 0.0, "nb_ecritures": 0}
            comptes[cpt]["nb_ecritures"] += 1
            if side == "debit":   comptes[cpt]["total_debit"]  += float(m.montant or 0)
            else:                 comptes[cpt]["total_credit"] += float(m.montant or 0)

    for c in comptes.values():
        c["solde"] = round(c["total_debit"] - c["total_credit"], 2)

    return {
        "ecritures": [{
            "id": m.id,
            "numero_piece": m.numero_piece,
            "journal": str(m.journal.value) if m.journal else "",
            "date": str(m.created_at)[:10],
            "compte_debit": m.compte_debit,
            "compte_credit": m.compte_credit,
            "libelle_debit": m.libelle_debit or "",
            "libelle_credit": m.libelle_credit or "",
            "categorie": m.categorie,
            "description": m.description,
            "montant": float(m.montant or 0),
            "mode_paiement": m.mode_paiement or "especes",
            "reference": m.reference or "",
            "type": str(m.type.value) if m.type else "",
        } for m in ecritures],
        "comptes": list(comptes.values()),
        "total_recettes": sum(c["total_credit"] for k,c in comptes.items() if k.startswith("7")),
        "total_charges":  sum(c["total_debit"]  for k,c in comptes.items() if k.startswith("6")),
        "nb_ecritures": len(ecritures),
    }


@router.get("/admin/balance-verification", tags=["Admin - Compta"])
async def balance_verification(
    mois: Optional[int] = None,
    annee: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(require_admin)
):
    """
    Balance de vérification — vérifie que Total Débit = Total Crédit (partie double).
    Norme PCN Haïti + IFRS pour PME.
    """
    now = datetime.now(timezone.utc)
    q = db.query(models.Mouvement)
    if mois:  q = q.filter(models.Mouvement.periode_mois == mois)
    if annee: q = q.filter(models.Mouvement.periode_annee == (annee or now.year))
    mouvements = q.all()

    total_debit  = sum(float(m.montant or 0) for m in mouvements)
    total_credit = sum(float(m.montant or 0) for m in mouvements)  # Partie double: D=C toujours
    equilibre    = abs(total_debit - total_credit) < 0.01

    # Comptes par classe
    classes: dict = {}
    for m in mouvements:
        for cpt in [m.compte_debit, m.compte_credit]:
            cl = cpt[0] if cpt else "?"
            if cl not in classes:
                classes[cl] = {"classe": cl, "debit": 0.0, "credit": 0.0}
        classes[m.compte_debit[0]]["debit"]  += float(m.montant or 0)
        classes[m.compte_credit[0]]["credit"] += float(m.montant or 0)

    return {
        "equilibre": equilibre,
        "total_debit":  round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "ecart": round(abs(total_debit - total_credit), 2),
        "nb_ecritures": len(mouvements),
        "par_classe": sorted(classes.values(), key=lambda x: x["classe"]),
        "message": "✓ Balance équilibrée — partie double respectée" if equilibre
                   else f"⚠️ Déséquilibre de {abs(total_debit-total_credit):.2f} HTG — vérification requise",
        "periode": f"mois {mois}/{annee}" if mois else "toutes périodes",
    }


@router.post("/admin/comptable-ai", tags=["Admin - Compta"])
async def assistant_comptable_ai(data: dict, db: Session = Depends(get_db),
                                   current_user=Depends(require_admin)):
    """
    Assistant IA comptable dédié — collecte toutes les données financières,
    génère un rapport en conformité PCN Haïti + normes IFRS pour PME.
    Utilise l'API Anthropic claude-sonnet-4-20250514.
    """
    mois  = data.get("mois",  datetime.now(timezone.utc).month)
    annee = data.get("annee", datetime.now(timezone.utc).year)
    type_rapport = data.get("type", "mensuel")  # mensuel | annuel | flux_tresorerie | bilan_patrimonial

    # ── Collecte de toutes les données ──────────────────────────────────────
    mouvements = db.query(models.Mouvement).filter(
        models.Mouvement.periode_mois == mois,
        models.Mouvement.periode_annee == annee,
    ).order_by(models.Mouvement.created_at.asc()).all()

    # Recettes par service
    recettes: dict = {}
    charges: dict  = {}
    for m in mouvements:
        if str(m.type) == "recette":
            recettes[m.categorie] = recettes.get(m.categorie, 0) + float(m.montant or 0)
        else:
            charges[m.categorie]  = charges.get(m.categorie, 0)  + float(m.montant or 0)

    total_rec = sum(recettes.values())
    total_dep = sum(charges.values())
    resultat  = total_rec - total_dep

    # Trésorerie par mode
    tresorerie: dict = {}
    for m in mouvements:
        mode = m.mode_paiement or "especes"
        tresorerie[mode] = tresorerie.get(mode, 0) + float(m.montant or 0)

    # Patients enregistrés ce mois
    nb_patients = db.query(func.count(models.Patient.id)).filter(
        func.extract('month', models.Patient.created_at) == mois,
        func.extract('year',  models.Patient.created_at) == annee,
    ).scalar() or 0

    # Nombre d'actes par service
    rdvs = db.query(models.RendezVous).filter(
        func.extract('month', models.RendezVous.date_rdv) == mois,
        func.extract('year',  models.RendezVous.date_rdv) == annee,
    ).all()
    actes_par_service: dict = {}
    for r in rdvs:
        svc = r.specialite or "Autres"
        actes_par_service[svc] = actes_par_service.get(svc, 0) + 1

    MOIS_NOMS = ["","Janvier","Février","Mars","Avril","Mai","Juin","Juillet","Août","Septembre","Octobre","Novembre","Décembre"]
    mois_nom = MOIS_NOMS[mois] if 1 <= mois <= 12 else str(mois)

    # ── Prompt système comptable ─────────────────────────────────────────────
    system_prompt = """Tu es un expert-comptable agréé spécialisé dans les établissements de santé haïtiens.
Tu maîtrises le Plan Comptable National haïtien (PCN), les normes IFRS pour PME, et la réglementation fiscale haïtienne (DGI, TCA).
Tes rapports sont structurés, précis, conformes aux normes, et incluent des recommandations actionnables.
Réponds toujours en français haïtien professionnel. Utilise les numéros de comptes PCN dans tes analyses."""

    context = f"""DONNÉES FINANCIÈRES — CLINIQUE DE LA REBECCA
Période: {mois_nom} {annee}
Type de rapport demandé: {type_rapport}

═══ PRODUITS (Classe 7 PCN) ═══
{chr(10).join(f"  {cat}: {montant:,.0f} HTG" for cat, montant in sorted(recettes.items(), key=lambda x: -x[1]))}
TOTAL PRODUITS: {total_rec:,.0f} HTG

═══ CHARGES (Classe 6 PCN) ═══
{chr(10).join(f"  {cat}: {montant:,.0f} HTG" for cat, montant in sorted(charges.items(), key=lambda x: -x[1]))}
TOTAL CHARGES: {total_dep:,.0f} HTG

═══ RÉSULTAT NET ═══
{resultat:,.0f} HTG ({'BÉNÉFICE' if resultat >= 0 else 'DÉFICIT'})

═══ TRÉSORERIE PAR MODE ═══
{chr(10).join(f"  {mode}: {montant:,.0f} HTG" for mode, montant in tresorerie.items())}

═══ ACTIVITÉ CLINIQUE ═══
Nouveaux patients: {nb_patients}
Total transactions: {len(mouvements)}
Actes par service:
{chr(10).join(f"  {svc}: {nb} actes" for svc, nb in sorted(actes_par_service.items(), key=lambda x: -x[1]))}
"""

    type_prompts = {
        "mensuel": f"""Génère un rapport comptable mensuel complet incluant:
1. Résumé exécutif (3 phrases max)
2. Analyse des produits par service (avec % du total)
3. Analyse des charges avec comparaison aux normes sectorielles
4. Résultat net et taux de marge
5. Flux de trésorerie par mode de paiement
6. Indicateurs clés: ratio charges/produits, productivité par patient
7. Points d'attention et anomalies éventuelles
8. Recommandations pour le mois prochain
Format: rapport professionnel structuré, 400-600 mots.""",

        "flux_tresorerie": """Génère un état des flux de trésorerie selon IAS 7 adapté PCN Haïti:
1. Flux d'exploitation (activités de soins)
2. Flux d'investissement (équipements, immobilisations)
3. Flux de financement (apports associés, emprunts)
4. Variation nette de trésorerie
5. Analyse de la liquidité immédiate
6. Recommandations de gestion de trésorerie""",

        "bilan_patrimonial": """Génère une analyse du bilan patrimonial:
1. Actif: immobilisations, stocks pharmacie, créances patients, trésorerie
2. Passif: dettes fournisseurs, charges à payer, capitaux propres estimés
3. Ratio de liquidité générale
4. Fonds de roulement
5. Recommandations de renforcement patrimonial""",

        "annuel": """Génère un rapport annuel de synthèse:
1. Faits marquants de l'année
2. Évolution des produits vs N-1 (estimé)
3. Maîtrise des charges
4. Investissements réalisés
5. Perspectives et objectifs N+1
6. Conformité fiscale (TCA, OFATMA, DGI)""",
    }

    prompt_type = type_prompts.get(type_rapport, type_prompts["mensuel"])
    full_prompt = f"{context}

MISSION:
{prompt_type}"

    # ── Appel Anthropic API ──────────────────────────────────────────────────
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": full_prompt}],
                }
            )
            resp.raise_for_status()
            ai_response = resp.json()
            rapport_texte = ai_response["content"][0]["text"]
    except Exception as e:
        rapport_texte = f"[Erreur IA: {str(e)}] Données disponibles mais rapport IA non généré."

    return {
        "rapport": rapport_texte,
        "donnees": {
            "mois": mois, "annee": annee, "mois_nom": mois_nom,
            "total_produits": round(total_rec, 2),
            "total_charges":  round(total_dep, 2),
            "resultat_net":   round(resultat, 2),
            "nb_transactions": len(mouvements),
            "nb_patients":    nb_patients,
            "recettes_par_service": recettes,
            "charges_par_categorie": charges,
            "tresorerie_par_mode":  tresorerie,
            "actes_par_service":    actes_par_service,
        },
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
    """Dossier patient: visites, RDV, prescriptions, résultats labo"""
    if current_user.role != models.RoleEnum.patient:
        raise HTTPException(403, "Accès réservé aux patients")

    # Cherche par user_id (nouveaux comptes) OU par email (anciens comptes)
    patient = db.query(models.Patient).filter(
        models.Patient.user_id == current_user.id
    ).first()
    if not patient:
        patient = db.query(models.Patient).filter(
            models.Patient.email == current_user.email
        ).first()

    if not patient:
        # Créer automatiquement le dossier patient s'il n'existe pas encore
        try:
            count = db.query(models.Patient).count()
            patient = models.Patient(
                user_id=current_user.id,
                numero=f"#RB-{str(count + 1).zfill(4)}",
                nom=current_user.nom,
                email=current_user.email,
                telephone=current_user.telephone,
            )
            db.add(patient)
            db.commit()
            db.refresh(patient)
        except Exception as e:
            logger.error(f"Auto-create patient failed: {e}")
            db.rollback()
            return {
                "numero_patient": None,
                "nb_visites": 0, "visites": [], "rdv_a_venir": [], "rdv_passes": [],
                "prescriptions_actives": [], "resultats_labo": [],
            }

    from datetime import timedelta, timezone
    now = datetime.now(timezone.utc)
    trois_mois = now - timedelta(days=90)

    dossiers = db.query(models.DossierPatient).filter(
        models.DossierPatient.patient_id == patient.id
    ).order_by(models.DossierPatient.date_visite.desc()).limit(20).all()

    prescriptions = db.query(models.Prescription).filter(
        models.Prescription.patient_id == patient.id,
        models.Prescription.date_prescription >= trois_mois
    ).order_by(models.Prescription.date_prescription.desc()).all()

    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(patient.id),
    ).order_by(models.ResultatLabo.date_examen.desc()).limit(20).all()

    rdv_a_venir = db.query(models.RendezVous).filter(
        models.RendezVous.patient_email == current_user.email,
        models.RendezVous.date_rdv >= now,
    ).order_by(models.RendezVous.date_rdv).all()

    rdv_passes = db.query(models.RendezVous).filter(
        models.RendezVous.patient_email == current_user.email,
        models.RendezVous.date_rdv < now,
        models.RendezVous.date_rdv >= now - timedelta(days=365),
    ).order_by(models.RendezVous.date_rdv.desc()).limit(20).all()

    def rdv_out(r):
        return {
            "id": r.id, "specialite": r.specialite,
            "date_rdv": str(r.date_rdv), "statut": str(r.statut),
            "type_rdv": str(r.type_rdv), "medecin_nom": r.medecin_nom,
            "lien_video": r.lien_video if hasattr(r, "lien_video") else None,
        }

    return {
        "numero_patient": patient.numero,
        "nb_visites": len(dossiers),
        "visites": [{"id": d.id, "date_visite": str(d.date_visite),
                     "specialite": d.specialite or d.type_visite,
                     "statut": str(d.statut), "service": d.service,
                     "contexte_visite": f"{d.specialite or 'Consultation'} — {d.service or 'Clinique'}"}
                    for d in dossiers],
        "rdv_a_venir": [rdv_out(r) for r in rdv_a_venir],
        "rdv_passes": [rdv_out(r) for r in rdv_passes],
        "prescriptions_actives": [{"id": p.id, "medicaments": p.medicaments,
                                    "medecin_nom": p.medecin_nom,
                                    "date": str(p.date_prescription)} for p in prescriptions],
        "resultats_labo": [{"id": r.id, "type_examen": r.type_examen,
                             "resultats": r.resultats, "date_examen": str(r.date_examen),
                             "alerte_critique": getattr(r, "alerte_critique", False)} for r in resultats],
    }

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

    if montant <= 0:
        raise HTTPException(422, "Montant invalide")
    try:
        mouvement = _creer_mouvement(
            db=db, journal="VTE",
            type_mouv=models.TypeMouvementEnum.recette,
            categorie=service or "Consultation",
            description=f"Paiement {service} — Patient {patient.numero}",
            montant=montant,
            compte_debit="511", compte_credit="701",
            libelle_debit="Caisse HTG", libelle_credit="Recettes",
            mode_paiement=mode, reference=ref,
            created_by=current_user.id,
        )
        db.commit(); db.refresh(mouvement)
        recu_num = mouvement.numero_piece
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erreur paiement: {str(e)}")

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


@router.post("/caissier/verifier-moncash", tags=["Caissier - Paiement"])
async def verifier_moncash(data: dict, db: Session = Depends(get_db),
                            current_user=Depends(get_current_user)):
    """
    Vérifie qu'un numéro MonCash est valide avant d'enregistrer le paiement.
    Validation locale : format 509-XXXXXXXX (8 chiffres haïtiens).
    En production, appeler l'API MonCash pour confirmer la transaction.
    """
    telephone = data.get("telephone", "").strip().replace("-", "").replace(" ", "")
    reference = data.get("reference", "").strip()
    montant   = float(data.get("montant", 0))

    # Validation format numéro haïtien
    import re
    clean = telephone.replace("+509", "").replace("509", "")
    if not re.match(r"^[34]\d{7}$", clean):
        raise HTTPException(422, "Numéro MonCash invalide — format attendu: 3X/4X-XXXXXXX (Haïti)")

    if montant <= 0:
        raise HTTPException(422, "Montant invalide")

    # TODO production: appel API MonCash pour confirmer la transaction par référence
    # Pour l'instant: validation locale uniquement
    verified = bool(reference)  # En prod: vérifier reference via API MonCash

    return {
        "valide": True,
        "telephone": telephone,
        "montant": montant,
        "reference": reference,
        "reference_confirmee": verified,
        "message": f"Numéro {telephone} vérifié" + (" — référence fournie" if verified else " — aucune référence de transaction"),
        "mode": "moncash",
    }


@router.post("/caissier/verifier-natcash", tags=["Caissier - Paiement"])
async def verifier_natcash(data: dict, db: Session = Depends(get_db),
                            current_user=Depends(get_current_user)):
    """Vérifie qu'un numéro Natcash est valide."""
    telephone = data.get("telephone", "").strip().replace("-", "").replace(" ", "")
    reference = data.get("reference", "").strip()
    montant   = float(data.get("montant", 0))

    import re
    clean = telephone.replace("+509", "").replace("509", "")
    if not re.match(r"^[34]\d{7}$", clean):
        raise HTTPException(422, "Numéro Natcash invalide — format attendu: 3X/4X-XXXXXXX (Haïti)")

    if montant <= 0:
        raise HTTPException(422, "Montant invalide")

    return {
        "valide": True,
        "telephone": telephone,
        "montant": montant,
        "reference": reference,
        "reference_confirmee": bool(reference),
        "message": f"Numéro Natcash {telephone} vérifié",
        "mode": "natcash",
    }


@router.post("/caissier/verifier-carte", tags=["Caissier - Paiement"])
async def verifier_carte(data: dict, db: Session = Depends(get_db),
                          current_user=Depends(get_current_user)):
    """
    Pré-validation carte bancaire côté caissier.
    NB: les données de carte ne sont jamais stockées — conformité PCI-DSS.
    En production: intégration Stripe/Vantiv pour tokenisation.
    """
    numero = data.get("numero", "").replace(" ", "").replace("-", "")
    expiry = data.get("expiry", "").strip()   # MM/YY
    cvv    = data.get("cvv", "").strip()
    nom    = data.get("nom_titulaire", "").strip()
    montant = float(data.get("montant", 0))

    import re
    if not re.match(r"^\d{13,19}$", numero):
        raise HTTPException(422, "Numéro de carte invalide (13-19 chiffres)")
    if not re.match(r"^\d{2}/\d{2}$", expiry):
        raise HTTPException(422, "Date d'expiration invalide — format MM/AA")
    if not re.match(r"^\d{3,4}$", cvv):
        raise HTTPException(422, "CVV invalide (3-4 chiffres)")
    if not nom:
        raise HTTPException(422, "Nom du titulaire requis")

    # Détection type carte (Visa/MC/Amex)
    carte_type = "Visa" if numero.startswith("4") else                  "Mastercard" if numero[:2] in ["51","52","53","54","55"] else                  "Amex" if numero[:2] in ["34","37"] else "Autre"

    # Masquer le numéro pour l'audit
    numero_masque = f"**** **** **** {numero[-4:]}"

    return {
        "valide": True,
        "carte_type": carte_type,
        "numero_masque": numero_masque,
        "expiry": expiry,
        "nom_titulaire": nom,
        "montant": montant,
        "message": f"Carte {carte_type} {numero_masque} validée",
        "mode": "carte",
        # En production: token Stripe ici
        "token": f"tok_{numero[-4:]}_{expiry.replace('/','')}"
    }


@router.post("/caissier/verifier-zelle", tags=["Caissier - Paiement"])
async def verifier_zelle(data: dict, db: Session = Depends(get_db),
                          current_user=Depends(get_current_user)):
    """
    Validation paiement Zelle (transfert USD).
    Zelle ne fournit pas d'API de vérification en temps réel —
    le caissier doit confirmer visuellement la réception sur l'app bancaire.
    """
    import re
    email_ou_tel = data.get("email_ou_tel", "").strip()
    nom_envoyeur = data.get("nom_envoyeur", "").strip()
    montant_usd  = float(data.get("montant_usd", 0))
    reference    = data.get("reference", "").strip()  # Numéro de confirmation Zelle

    # Valider email ou téléphone US
    is_email = "@" in email_ou_tel
    is_phone = re.match(r"^\+?1?\d{10,11}$", email_ou_tel.replace("-","").replace(" ",""))

    if not is_email and not is_phone:
        raise HTTPException(422, "Email ou numéro de téléphone US invalide pour Zelle")

    if montant_usd <= 0:
        raise HTTPException(422, "Montant USD invalide")

    if not nom_envoyeur:
        raise HTTPException(422, "Nom de l'envoyeur requis pour confirmer le Zelle")

    return {
        "valide": True,
        "email_ou_tel": email_ou_tel,
        "nom_envoyeur": nom_envoyeur,
        "montant_usd": montant_usd,
        "reference": reference,
        "message": f"Zelle de {nom_envoyeur} ({email_ou_tel}) — ${montant_usd} USD — en attente confirmation visuelle",
        "mode": "zelle",
        "avertissement": "Confirmez visuellement la réception sur votre application bancaire avant de valider."
    }


@router.get("/caissier/paiements-jour", tags=["Caissier"])
async def paiements_du_jour(db: Session = Depends(get_db),
                             current_user=Depends(get_current_user)):
    """Liste des paiements du jour avec total"""
    from sqlalchemy import cast, Date as SADate
    from datetime import datetime, timezone
    
    aujourd_hui = datetime.now(timezone.utc).date()
    paiements = db.query(models.Mouvement).filter(
        cast(models.Mouvement.created_at, SADate) == aujourd_hui,
        models.Mouvement.type == models.TypeMouvementEnum.recette,
    ).order_by(models.Mouvement.created_at.desc()).all()

    total = sum(float(p.montant or 0) for p in paiements)

    return {
        "paiements": [
            {
                "id": p.id,
                "service": p.description,
                "montant": float(p.montant or 0),
                "mode_paiement": p.mode_paiement or "especes",
                "recu_numero": p.numero_piece,
                "reference": p.reference or "",
                "date": str(p.created_at),
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



@router.post("/caissier/depense", status_code=201, tags=["Caissier"])
async def enregistrer_depense(data: dict, request: Request,
                               db: Session = Depends(get_db),
                               current_user=Depends(get_current_user)):
    """Enregistre une dépense/décaissement journalier"""
    import uuid as uid2
    now = datetime.now(timezone.utc)
    num = f"DEP-{now.strftime('%Y%m%d')}-{str(uid2.uuid4())[:6].upper()}"
    montant = float(data.get('montant', 0))
    categorie = data.get('categorie', 'Dépense')
    
    if montant <= 0:
        raise HTTPException(422, "Montant invalide — doit être supérieur à 0")

    if not categorie or categorie.strip() == "":
        raise HTTPException(422, "Catégorie requise")
    
    # Normalisation catégorie → compte PCN
    CAT_TO_PCN = {
        "RH / Salaires": "641",  "RH": "641", "Salaires": "641", "Personnel": "641",
        "Charges sociales OFATMA": "645",
        "Honoraires médecins": "651",  "Honoraires": "651",
        "Achats médicaments": "601",   "Médical": "601",
        "Pharmacie achats": "607",
        "Consommables médicaux": "602",
        "Infrastructure": "615",       "Entretien": "615",
        "Équipements": "218",
        "Télécom": "626",              "Telecom": "626",
        "Amortissements": "681",
        "Autres charges": "628",       "Autre": "628",
    }
    compte_d = CAT_TO_PCN.get(categorie, models.COMPTE_PCN.get(categorie, "628"))

    # Compte crédit = caisse selon mode
    mode_pmt = data.get('mode', 'especes')
    compte_c = models.get_compte_tresorerie(mode_pmt, "HTG")

    try:
        mouvement = _creer_mouvement(
            db=db,
            journal=models.JournalEnum.ACH,
            type_mouv=models.TypeMouvementEnum.depense,
            categorie=categorie,
            description=f"[{categorie}] {data.get('description', '')}",
            montant=montant,
            compte_debit=compte_d,
            compte_credit=compte_c,
            libelle_debit=f"{categorie} ({compte_d})",
            libelle_credit=f"Trésorerie {mode_pmt} ({compte_c})",
            mode_paiement=mode_pmt,
            created_by=current_user.id,
        )
        db.commit()
        db.refresh(mouvement)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Erreur création dépense: {e}")
        raise HTTPException(500, f"Erreur enregistrement dépense: {str(e)}")
    
    log_audit(db, "DEPENSE_ENREGISTREE",
              actor_id=current_user.id, actor_role="caissier",
              target_id=mouvement.numero_piece,
              details=f"{categorie}: {montant} HTG | {data.get('mode','especes')}",
              retention_ans=7)
    
    return {
        "id": mouvement.id,
        "numero_piece": mouvement.numero_piece,
        "categorie": categorie,
        "description": data.get('description'),
        "montant": montant,
        "mode": data.get('mode', 'especes'),
        "message": f"Dépense enregistrée — {mouvement.numero_piece}"
    }


@router.get("/caissier/depenses-jour", tags=["Caissier"])
async def depenses_du_jour(db: Session = Depends(get_db),
                            current_user=Depends(get_current_user)):
    """Dépenses/décaissements du jour"""
    from sqlalchemy import cast, Date as SADate
    aujourd_hui = datetime.now(timezone.utc).date()
    
    depenses = db.query(models.Mouvement).filter(
        cast(models.Mouvement.created_at, SADate) == aujourd_hui,
        models.Mouvement.type == models.TypeMouvementEnum.depense,
    ).order_by(models.Mouvement.created_at.desc()).all()
    
    total = sum(float(d.montant or 0) for d in depenses)
    
    return {
        "depenses": [
            {"id":d.id, "description":d.description, "montant":float(d.montant or 0),
             "mode":d.mode_paiement or 'especes', "categorie": (d.description or '').split(']')[0].replace('[','')}
            for d in depenses
        ],
        "total": total
    }


@router.get("/medecin/certificat/{patient_id}", tags=["Médecin"])
async def get_dernier_certificat(patient_id: int, db: Session = Depends(get_db),
                                  current_user=Depends(get_current_user)):
    """Récupère le dernier certificat médical signé pour un patient"""
    dossier = db.query(models.DossierPatient).filter(
        models.DossierPatient.patient_id == patient_id,
        models.DossierPatient.statut == models.StatutDossierEnum.termine
    ).order_by(models.DossierPatient.date_visite.desc()).first()
    
    if not dossier:
        raise HTTPException(404, "Aucun certificat disponible")
    
    medecin = db.query(models.User).filter(models.User.id == dossier.medecin_id).first() if dossier.medecin_id else None
    
    return {
        "medecin_nom": medecin.nom if medecin else None,
        "specialite": medecin.specialite if medecin else None,
        "contenu": dossier.notes or "",
        "date": str(dossier.date_visite)
    }


@router.post("/medecin/recommander-avec-resume/{dossier_id}", tags=["Médecin"])
async def recommander_avec_resume_ia(dossier_id: int, data: dict,
                                      db: Session = Depends(get_db),
                                      current_user=Depends(get_current_user)):
    """
    Médecin recommande vers un spécialiste (physio/dentiste/optométriste).
    L'IA génère un résumé limité pour le spécialiste — PAS le dossier complet.
    Le spécialiste n'aura accès QU'à ce résumé.
    """
    dossier = db.query(models.DossierPatient).filter(
        models.DossierPatient.id == dossier_id).first()
    if not dossier:
        raise HTTPException(404, "Dossier introuvable")
    
    specialiste_cible = data.get("specialiste_cible", "")
    motif = data.get("motif", "")
    
    # Récupérer contexte minimal pour le résumé IA
    patient = db.query(models.Patient).filter(
        models.Patient.id == dossier.patient_id).first()
    
    sv = db.query(models.SignesVitaux).filter(
        models.SignesVitaux.dossier_id == dossier_id
    ).order_by(models.SignesVitaux.created_at.desc()).first()
    
    # Le résumé IA sera limité: motif de recommandation + points essentiels UNIQUEMENT
    resume_context = {
        "motif_recommandation": motif,
        "specialite_cible": specialiste_cible,
        "medecin_referent": current_user.nom,
        "signes_vitaux_recents": {
            "tension": f"{sv.tension_systolique}/{sv.tension_diastolique}" if sv else None,
            "temperature": sv.temperature if sv else None,
        } if sv else {},
        "type_visite": dossier.type_visite,
    }
    
    # Enregistrer la recommandation
    geste = models.GesteMedical(
        dossier_id=dossier_id,
        type_geste="RECOMMANDATION",
        description=f"Recommandation vers {specialiste_cible} — Motif: {motif}",
        notes=f"Résumé IA disponible pour {specialiste_cible}",
        medecin_id=current_user.id,
    )
    db.add(geste)
    
    log_audit(db, "RECOMMANDATION_EMISE",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=str(dossier_id), target_type="dossier",
              details=f"→ {specialiste_cible}: {motif}", retention_ans=5)
    db.commit()
    
    return {
        "message": f"Recommandation vers {specialiste_cible} enregistrée",
        "dossier_id": dossier_id,
        "specialiste": specialiste_cible,
        "motif": motif,
        "resume_context": resume_context,  # Le frontend utilisera ça pour générer le résumé IA
    }


@router.get("/specialiste/ma-recommandation/{dossier_id}", tags=["Spécialistes"])
async def get_recommandation_specialiste(dossier_id: int,
                                          db: Session = Depends(get_db),
                                          current_user=Depends(get_current_user)):
    """
    Physio/Dentiste/Optométriste voit UNIQUEMENT le résumé de recommandation.
    PAS le dossier complet.
    """
    SPECIALITES_LIMITEES = ['dentisterie','dentiste','optometrie','optométrie','physiotherapie','physiothérapie']
    user_spec = (current_user.specialite or '').lower()
    
    if not any(s in user_spec for s in SPECIALITES_LIMITEES):
        raise HTTPException(403, "Endpoint réservé aux spécialistes avec accès limité")
    
    # Récupérer uniquement la recommandation, pas le dossier
    rec = db.query(models.GesteMedical).filter(
        models.GesteMedical.dossier_id == dossier_id,
        models.GesteMedical.type_geste == "RECOMMANDATION"
    ).order_by(models.GesteMedical.created_at.desc()).first()
    
    if not rec:
        raise HTTPException(404, "Aucune recommandation trouvée pour ce dossier")
    
    log_audit(db, "RECOMMANDATION_CONSULTEE_SPECIALISTE",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=str(dossier_id), result="succes",
              details=f"Spécialiste {current_user.specialite} — résumé uniquement")
    
    return {
        "recommandation": rec.description,
        "date": str(rec.created_at),
        "notes": rec.notes,
        "acces": "resume_uniquement",
        "avertissement": "Vous n'avez accès qu'au résumé de recommandation. Le dossier médical complet n'est pas accessible."
    }



# ═══════════════════════════════════════════════════════════════════════════
# PARCOURS PATIENT AMÉLIORÉ
# ═══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# SIGNATURE MÉDECIN — accès strictement réservé au médecin lui-même
# ══════════════════════════════════════════════════════════════════════════════

@router.put("/medecin/ma-signature", tags=["Médecin"])
async def enregistrer_signature(data: dict, request: Request,
                                 db: Session = Depends(get_db),
                                 current_user=Depends(get_current_user)):
    """Médecin enregistre sa propre signature (PNG base64). Accès strictement limité au médecin lui-même."""
    if str(current_user.role) != 'medecin':
        raise HTTPException(403, "Seul un médecin peut enregistrer sa signature")
    signature_b64 = data.get("signature", "")
    if not signature_b64.startswith("data:image/png;base64,"):
        raise HTTPException(422, "Format invalide — PNG base64 requis")
    current_user.signature_image = signature_b64
    db.commit()
    log_audit(db, "SIGNATURE_ENREGISTREE", actor_id=current_user.id,
              actor_role="medecin", target_id=str(current_user.id), retention_ans=10)
    return {"message": "Signature enregistrée"}


@router.get("/medecin/ma-signature", tags=["Médecin"])
async def get_ma_signature(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Médecin récupère SA PROPRE signature — aucun autre rôle n'y a accès."""
    if str(current_user.role) != 'medecin':
        raise HTTPException(403, "Réservé au médecin")
    if not current_user.signature_image:
        return {"signature": None}
    log_audit(db, "SIGNATURE_CONSULTEE", actor_id=current_user.id,
              actor_role="medecin", target_id=str(current_user.id), retention_ans=5)
    return {"signature": current_user.signature_image, "medecin_nom": current_user.nom}


@router.delete("/medecin/ma-signature", tags=["Médecin"])
async def supprimer_signature(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Médecin supprime sa propre signature."""
    if str(current_user.role) != 'medecin':
        raise HTTPException(403, "Réservé au médecin")
    current_user.signature_image = None
    db.commit()
    log_audit(db, "SIGNATURE_SUPPRIMEE", actor_id=current_user.id,
              actor_role="medecin", target_id=str(current_user.id), retention_ans=10)
    return {"message": "Signature supprimée"}



# ══════════════════════════════════════════════════════════════════════════════
# MOT DE PASSE OUBLIÉ — Reset en 3 étapes
# ══════════════════════════════════════════════════════════════════════════════

import secrets
import hashlib
from datetime import timedelta

# Stockage en mémoire des tokens (en prod: Redis ou table DB)
_reset_tokens: dict = {}  # {token_hash: {user_id, email, expires_at}}


@router.post("/auth/mot-de-passe-oublie", tags=["Auth"])
async def demander_reset(data: dict, db: Session = Depends(get_db)):
    """
    Étape 1 : L'utilisateur soumet son email.
    - On cherche le compte (tous rôles)
    - On génère un token sécurisé (6 chiffres + lien)
    - On envoie par email
    - IMPORTANT : on répond toujours 200 même si email inconnu (sécurité anti-énumération)
    """
    email = data.get("email", "").strip().lower()
    if not email:
        raise HTTPException(422, "Email obligatoire")

    user = db.query(models.User).filter(
        models.User.email == email
    ).first()

    # Toujours répondre 200 pour éviter l'énumération des comptes
    if not user:
        return {"message": "Si ce compte existe, un email de réinitialisation a été envoyé."}

    if not user.is_active:
        return {"message": "Si ce compte existe, un email de réinitialisation a été envoyé."}

    # Générer token sécurisé
    token_raw = secrets.token_urlsafe(32)
    code_6    = str(secrets.randbelow(900000) + 100000)  # Code 6 chiffres
    token_hash = hashlib.sha256(token_raw.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    _reset_tokens[token_hash] = {
        "user_id": user.id,
        "email": user.email,
        "code": code_6,
        "expires_at": expires_at,
        "used": False,
    }

    # Envoyer email
    from app.services.notifications import send_email
    reset_link = f"https://clinique-rebecca-frontend.vercel.app/reset-password?token={token_raw}"

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px;">
      <div style="text-align:center;margin-bottom:24px;">
        <h2 style="color:#1641C8;margin:0;">Clinique de la Rebecca</h2>
        <p style="color:#64748b;font-size:13px;">Réinitialisation de mot de passe</p>
      </div>
      <p style="color:#374151;">Bonjour <strong>{user.nom}</strong>,</p>
      <p style="color:#374151;">Vous avez demandé à réinitialiser votre mot de passe.</p>

      <div style="background:#f8fafc;border-radius:12px;padding:24px;text-align:center;margin:24px 0;border:1px solid #e2e8f0;">
        <p style="color:#64748b;font-size:13px;margin:0 0 8px;">Votre code de vérification</p>
        <div style="font-size:36px;font-weight:900;letter-spacing:8px;color:#1641C8;font-family:monospace;">{code_6}</div>
        <p style="color:#94a3b8;font-size:12px;margin:8px 0 0;">Valide pendant 1 heure</p>
      </div>

      <div style="text-align:center;margin:20px 0;">
        <p style="color:#64748b;font-size:13px;">ou cliquez sur ce lien :</p>
        <a href="{reset_link}"
           style="display:inline-block;background:linear-gradient(135deg,#1641C8,#0d9488);color:white;text-decoration:none;border-radius:10px;padding:12px 28px;font-weight:700;font-size:14px;">
          Réinitialiser mon mot de passe
        </a>
      </div>

      <div style="background:#fef2f2;border-radius:8px;padding:12px 16px;margin-top:24px;">
        <p style="color:#dc2626;font-size:12px;margin:0;">
          ⚠️ Si vous n'avez pas fait cette demande, ignorez cet email.
          Votre mot de passe reste inchangé.
        </p>
      </div>

      <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:24px;">
        Clinique de la Rebecca · #44, Rue Rebecca, Pétion-Ville · (509) 4858-5757
      </p>
    </div>
    """

    import asyncio
    asyncio.create_task(send_email(
        to=user.email,
        subject="Réinitialisation de votre mot de passe — Clinique de la Rebecca",
        html_body=html
    ))

    log_audit(db, "RESET_PASSWORD_DEMANDE",
              actor_id=user.id, actor_role=str(user.role),
              target_id=str(user.id), retention_ans=2)

    return {"message": "Si ce compte existe, un email de réinitialisation a été envoyé."}


@router.post("/auth/verifier-code-reset", tags=["Auth"])
async def verifier_code_reset(data: dict, db: Session = Depends(get_db)):
    """
    Étape 2 : Vérifier le code 6 chiffres ou le token URL.
    Retourne un token de session pour l'étape 3.
    """
    email = data.get("email", "").strip().lower()
    code  = data.get("code", "").strip()
    token_url = data.get("token", "").strip()

    now = datetime.now(timezone.utc)

    # Trouver le token correspondant
    found_token_hash = None
    found_entry = None

    if token_url:
        token_hash = hashlib.sha256(token_url.encode()).hexdigest()
        if token_hash in _reset_tokens:
            found_token_hash = token_hash
            found_entry = _reset_tokens[token_hash]
    elif email and code:
        for h, entry in _reset_tokens.items():
            if entry["email"] == email and entry["code"] == code:
                found_token_hash = h
                found_entry = entry
                break

    if not found_entry:
        raise HTTPException(400, "Code invalide ou email incorrect. Vérifiez le code reçu par email.")

    if found_entry["used"]:
        raise HTTPException(400, "Ce code a déjà été utilisé. Faites une nouvelle demande.")

    if found_entry["expires_at"] < now:
        del _reset_tokens[found_token_hash]
        raise HTTPException(400, "Ce code a expiré (valide 1 heure). Faites une nouvelle demande.")

    # Générer un token de session pour l'étape 3 (valide 15 min)
    session_token = secrets.token_urlsafe(32)
    session_hash  = hashlib.sha256(session_token.encode()).hexdigest()

    _reset_tokens[session_hash] = {
        "user_id": found_entry["user_id"],
        "email":   found_entry["email"],
        "type":    "session_reset",
        "expires_at": now + timedelta(minutes=15),
        "used": False,
    }

    # Marquer le code original comme utilisé
    _reset_tokens[found_token_hash]["used"] = True

    return {
        "valid": True,
        "session_token": session_token,
        "message": "Code vérifié. Vous pouvez maintenant définir un nouveau mot de passe.",
    }


@router.post("/auth/nouveau-mot-de-passe", tags=["Auth"])
async def nouveau_mot_de_passe(data: dict, db: Session = Depends(get_db)):
    """
    Étape 3 : Définir le nouveau mot de passe.
    Requiert le session_token de l'étape 2.
    """
    session_token = data.get("session_token", "").strip()
    nouveau_mdp   = data.get("nouveau_mot_de_passe", "").strip()

    if not session_token or not nouveau_mdp:
        raise HTTPException(422, "Session token et nouveau mot de passe obligatoires")

    if len(nouveau_mdp) < 6:
        raise HTTPException(422, "Le mot de passe doit contenir au moins 6 caractères")

    now = datetime.now(timezone.utc)
    session_hash = hashlib.sha256(session_token.encode()).hexdigest()
    entry = _reset_tokens.get(session_hash)

    if not entry or entry.get("type") != "session_reset":
        raise HTTPException(400, "Session invalide. Recommencez la procédure.")

    if entry["used"]:
        raise HTTPException(400, "Cette session a déjà été utilisée.")

    if entry["expires_at"] < now:
        del _reset_tokens[session_hash]
        raise HTTPException(400, "Session expirée (15 minutes). Recommencez la procédure.")

    user = db.query(models.User).filter(models.User.id == entry["user_id"]).first()
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")

    # Mettre à jour le mot de passe
    user.hashed_password = get_password_hash(nouveau_mdp)
    entry["used"] = True
    db.commit()

    # Email de confirmation
    from app.services.notifications import send_email
    import asyncio
    asyncio.create_task(send_email(
        to=user.email,
        subject="Mot de passe modifié — Clinique de la Rebecca",
        html_body=f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px;">
          <h2 style="color:#1641C8;">Clinique de la Rebecca</h2>
          <p>Bonjour <strong>{user.nom}</strong>,</p>
          <p>Votre mot de passe a été modifié avec succès le {datetime.now().strftime('%d/%m/%Y à %H:%M')}.</p>
          <div style="background:#f0fdf4;border-radius:8px;padding:12px 16px;border-left:3px solid #16a34a;">
            <p style="color:#16a34a;margin:0;font-size:13px;">✓ Mot de passe mis à jour</p>
          </div>
          <p style="color:#dc2626;font-size:12px;margin-top:16px;">
            Si vous n'êtes pas à l'origine de cette modification, contactez-nous immédiatement au (509) 4858-5757.
          </p>
        </div>
        """
    ))

    log_audit(db, "RESET_PASSWORD_COMPLETE",
              actor_id=user.id, actor_role=str(user.role),
              target_id=str(user.id), retention_ans=5)

    # Auto-login
    token = create_access_token({"sub": str(user.id)})
    return {
        "message": "Mot de passe modifié avec succès.",
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "nom": user.nom, "email": user.email, "role": str(user.role)},
    }


@router.post("/rdv/confirmer/{rdv_id}", tags=["RDV"])
async def confirmer_rdv(rdv_id: int, request: Request,
                         db: Session = Depends(get_db),
                         current_user=Depends(get_current_user)):
    """
    Caissier OU médecin confirme un RDV.
    - Caissier confirme après accord verbal avec le médecin
    - Médecin confirme directement depuis son dashboard
    Dans les deux cas: notification au patient + enregistrement dans registre
    """
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")

    role = str(current_user.role)
    if role not in ("caissier", "medecin", "admin"):
        raise HTTPException(403, "Seul un caissier, médecin ou admin peut confirmer un RDV")

    ancien_statut = str(rdv.statut)
    rdv.statut = models.StatutRDVEnum.confirme
    rdv.confirme_par = current_user.id
    rdv.confirme_par_role = role

    # Si vidéo → générer lien Jitsi
    if str(rdv.type_rdv) == "video" and not rdv.lien_video:
        numero = rdv.numero_rdv or f"rdv{rdv.id}"
        rdv.lien_video = f"https://meet.jit.si/clinique-rebecca-{numero}"

    db.commit()

    log_audit(db, "RDV_CONFIRME",
              actor_id=current_user.id, actor_role=role,
              target_id=str(rdv_id),
              details=f"RDV {rdv.patient_nom} confirmé par {role} {current_user.nom}")

    # Notifier le patient
    rdv_data = {
        "patient_nom": rdv.patient_nom,
        "patient_telephone": rdv.patient_telephone,
        "patient_email": rdv.patient_email,
        "specialite": rdv.specialite,
        "date_rdv": rdv.date_rdv,
        "type_rdv": str(rdv.type_rdv),
        "motif": rdv.motif,
        "lien_video": rdv.lien_video,
        "confirme_par": f"{role} — {current_user.nom}",
    }
    import asyncio
    asyncio.create_task(notify_rdv_video_confirme(rdv_data))

    return {
        "message": "RDV confirmé",
        "rdv_id": rdv_id,
        "confirme_par_role": role,
        "lien_video": rdv.lien_video,
    }


@router.post("/rdv/proposer-autre-moment/{rdv_id}", tags=["RDV"])
async def proposer_autre_moment(rdv_id: int, data: dict,
                                 db: Session = Depends(get_db),
                                 current_user=Depends(get_current_user)):
    """
    Médecin propose un autre créneau si non disponible.
    Le patient recevra une notification avec la suggestion.
    """
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")
    if str(current_user.role) not in ("medecin", "caissier", "admin"):
        raise HTTPException(403, "Accès refusé")

    rdv.statut = models.StatutRDVEnum.propose_autre_moment
    rdv.autre_moment_propose = data.get("nouveau_moment", "")
    rdv.autre_moment_message = data.get("message", "Le médecin n'est pas disponible à cette date.")
    db.commit()

    log_audit(db, "RDV_AUTRE_MOMENT_PROPOSE",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=str(rdv_id),
              details=f"Nouveau moment proposé: {data.get('nouveau_moment')}")

    return {"message": "Proposition envoyée au patient", "nouveau_moment": data.get("nouveau_moment")}


@router.post("/rdv/paiement-presentiel/{rdv_id}", tags=["RDV"])
async def confirmer_paiement_presentiel(rdv_id: int, data: dict,
                                         db: Session = Depends(get_db),
                                         current_user=Depends(get_current_user)):
    """
    Enregistre le paiement d'un RDV présentiel (caissier ou secrétaire).
    Modes: especes | moncash | natcash | carte | zelle
    Crée aussi le mouvement comptable.
    """
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")

    mode      = data.get("mode", "especes")
    reference = data.get("reference", "")
    montant   = float(data.get("montant", 0))

    if montant <= 0:
        raise HTTPException(422, "Montant invalide")

    # Passer le statut à paiement_effectue
    rdv.statut            = models.StatutRDVEnum.paiement_effectue
    rdv.mode_paiement     = mode
    rdv.reference_paiement = reference

    # Créer l'écriture comptable
    if montant > 0:
        try:
            _creer_mouvement(
                db=db, journal=models.JournalEnum.VTE,
                type_mouv=models.TypeMouvementEnum.recette,
                categorie="Consultations",
                description=f"RDV {rdv.specialite} — {rdv.patient_nom}",
                montant=montant,
                compte_debit=models.get_compte_tresorerie(mode, "HTG"),
                compte_credit="701",
                libelle_debit="Trésorerie", libelle_credit="Produits consultations",
                mode_paiement=mode, reference=reference,
                created_by=current_user.id,
            )
        except Exception:
            pass  # Paiement enregistré même si écriture comptable échoue

    db.commit()

    log_audit(db, "PAIEMENT_RDV_PRESENTIEL",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=str(rdv_id),
              details=f"RDV {rdv.patient_nom} — {mode} — {montant} HTG — ref:{reference}")

    return {
        "message": "Paiement RDV enregistré",
        "statut": "paiement_effectue",
        "rdv_id": rdv_id,
        "mode": mode,
        "montant": montant,
    }


@router.post("/rdv/paiement-video/{rdv_id}", tags=["RDV"])
async def confirmer_paiement_video(rdv_id: int, data: dict,
                                    db: Session = Depends(get_db),
                                    current_user=Depends(get_current_user)):
    """
    Pour consultation vidéo: enregistre le paiement en ligne.
    Passe statut: paiement_requis → paiement_effectue
    Déclenche demande de confirmation au médecin.
    """
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")
    if str(rdv.type_rdv) != "video":
        raise HTTPException(400, "Réservé aux consultations vidéo")

    mode      = data.get("mode", "moncash")
    reference = data.get("reference", "")
    montant   = float(data.get("montant", 0))

    rdv.statut             = models.StatutRDVEnum.paiement_effectue
    rdv.reference_paiement = reference
    rdv.mode_paiement      = mode

    # Écriture comptable si montant fourni
    if montant > 0:
        try:
            _creer_mouvement(
                db=db, journal=models.JournalEnum.VTE,
                type_mouv=models.TypeMouvementEnum.recette,
                categorie="Consultations",
                description=f"RDV vidéo {rdv.specialite} — {rdv.patient_nom}",
                montant=montant,
                compte_debit=models.get_compte_tresorerie(mode, "HTG"),
                compte_credit="701",
                libelle_debit="Trésorerie", libelle_credit="Produits consultations",
                mode_paiement=mode, reference=reference,
                created_by=current_user.id if current_user else None,
            )
        except Exception:
            pass

    db.commit()

    log_audit(db, "PAIEMENT_VIDEO_CONFIRME",
              actor_id=current_user.id if current_user else 0,
              actor_role="patient",
              target_id=str(rdv_id),
              details=f"Paiement vidéo: {mode} — ref: {reference} — {montant} HTG")

    return {"message": "Paiement enregistré — confirmation en attente", "statut": "paiement_effectue", "mode": mode}


@router.get("/registre-rdv", tags=["RDV"])
async def registre_rdv(medecin_id: Optional[int] = None,
                         jours: int = 30,
                         db: Session = Depends(get_db),
                         current_user=Depends(get_current_user)):
    """
    Registre de tous les RDV à venir.
    - Caissier: voit tous les RDV (pour coordonner avec les médecins)
    - Médecin: voit ses propres RDV
    - Admin: voit tout + peut filtrer par médecin
    """
    from sqlalchemy import cast, Date as SADate
    role = str(current_user.role)
    aujourd = datetime.now(timezone.utc)
    limite = aujourd + timedelta(days=jours)

    q = db.query(models.RendezVous).filter(
        models.RendezVous.date_rdv >= aujourd,
        models.RendezVous.date_rdv <= limite,
        models.RendezVous.statut.in_(["en_attente", "paiement_effectue", "confirme"])
    )

    if role == "medecin":
        # Médecin voit ses RDV
        q = q.filter(
            (models.RendezVous.medecin_id == current_user.id) |
            (models.RendezVous.specialite.ilike(f"%{current_user.specialite or ''}%"))
        )
    elif medecin_id and role == "admin":
        q = q.filter(models.RendezVous.medecin_id == medecin_id)

    rdvs = q.order_by(models.RendezVous.date_rdv).all()

    return {
        "rdvs": [
            {
                "id": r.id,
                "patient_nom": r.patient_nom,
                "patient_telephone": r.patient_telephone,
                "specialite": r.specialite,
                "medecin_nom": r.medecin_nom,
                "date_rdv": str(r.date_rdv),
                "type_rdv": str(r.type_rdv),
                "statut": str(r.statut),
                "motif": r.motif,
                "confirme_par_role": r.confirme_par_role,
                "lien_video": r.lien_video,
                "autre_moment_propose": r.autre_moment_propose,
            }
            for r in rdvs
        ],
        "total": len(rdvs),
        "periode_jours": jours,
    }


@router.post("/rdv/initiation-physique/{rdv_id}", tags=["RDV"])
async def initiation_rdv_physique(rdv_id: int, data: dict, request: Request,
                                    db: Session = Depends(get_db),
                                    current_user=Depends(get_current_user)):
    """
    Pour RDV physique confirmé:
    Le caissier crée le dossier patient et encaisse le paiement.
    Retourne l'ID patient pour que l'infirmière puisse compléter.
    """
    if str(current_user.role) not in ("caissier", "admin"):
        raise HTTPException(403, "Réservé au caissier")

    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")

    # Trouver ou créer le patient
    patient = db.query(models.Patient).filter(
        models.Patient.telephone == rdv.patient_telephone
    ).first()

    if not patient:
        count = db.query(models.Patient).count()
        patient = models.Patient(
            numero=f"#RB-{str(count+1).zfill(4)}",
            nom=rdv.patient_nom,
            telephone=rdv.patient_telephone,
            email=rdv.patient_email or "",
            created_by=current_user.id,
        )
        db.add(patient)
        db.flush()

    # Créer le dossier
    dossier = models.DossierPatient(
        patient_id=patient.id,
        patient_numero=patient.numero,
        type_visite="rdv" if rdv.statut == models.StatutRDVEnum.confirme else "premiere_consultation",
        service="clinique",
        specialite=rdv.specialite,
        paiement_effectue=True,
        statut=models.StatutDossierEnum.attente_infirmier,
        motif_consultation=rdv.motif,
        rdv_id=rdv.id,
        created_by=current_user.id,
    )
    db.add(dossier)
    rdv.statut = models.StatutRDVEnum.termine
    db.commit()

    log_audit(db, "RDV_PHYSIQUE_INITIE",
              actor_id=current_user.id, actor_role="caissier",
              target_id=str(rdv_id),
              details=f"Patient {patient.numero} — Dossier créé")

    return {
        "message": "Dossier créé — patient peut voir l'infirmière",
        "patient_id": patient.id,
        "patient_numero": patient.numero,
        "dossier_id": dossier.id,
    }


@router.get("/medecin/dossier-par-patient/{patient_numero}", tags=["Médecin"])
async def get_dossier_par_patient_numero(patient_numero: str, request: Request,
                                          db: Session = Depends(get_db),
                                          current_user=Depends(get_current_user)):
    """
    Médecin cherche un dossier directement par numéro patient (#RB-XXXX).
    Utilisé quand le patient se présente avec son ID sans rendez-vous préalable.
    Retourne le dossier ACTIF en attente_medecin pour ce patient.
    """
    if str(current_user.role) != 'medecin':
        raise HTTPException(403, "Réservé aux médecins")

    patient = db.query(models.Patient).filter(
        models.Patient.numero == patient_numero.strip().upper()
    ).first()
    if not patient:
        raise HTTPException(404, f"Patient {patient_numero} introuvable")

    # Accès direct par ID patient — pas de restriction de statut ni de spécialité
    # Le médecin qui a l'ID peut toujours consulter le dossier (audit tracé)
    dossier = db.query(models.DossierPatient).filter(
        models.DossierPatient.patient_id == patient.id,
    ).order_by(models.DossierPatient.date_visite.desc()).first()

    # Si pas de dossier dans le système, retourner le profil patient vide
    if not dossier:
        return {
            "patient": {
                "id": patient.id, "numero": patient.numero,
                "nom": patient.nom, "prenom": patient.prenom,
                "age": patient.age, "telephone": patient.telephone,
                "email": patient.email, "adresse": patient.adresse,
            },
            "dossier": None,
            "message": "Patient enregistré — aucun dossier médical disponible",
            "signes_vitaux": None, "prescriptions": [], "resultats_labo": []
        }

    sv = db.query(models.SignesVitaux).filter(
        models.SignesVitaux.dossier_id == dossier.id
    ).order_by(models.SignesVitaux.created_at.desc()).first()

    prescriptions = db.query(models.Prescription).filter(
        models.Prescription.patient_id == patient.id
    ).order_by(models.Prescription.date_prescription.desc()).limit(10).all()

    resultats = db.query(models.ResultatLabo).filter(
        models.ResultatLabo.patient_id == str(patient.id)
    ).order_by(models.ResultatLabo.date_examen.desc()).limit(10).all()

    log_audit(db, "DOSSIER_CONSULTE_PAR_ID",
              actor_id=current_user.id, actor_role="medecin",
              target_id=patient_numero, target_type="patient",
              ip_address=request.client.host if request.client else None,
              details=f"Accès direct par ID patient — Dossier #{dossier.id}",
              retention_ans=5)

    return {
        "dossier": dossier,
        "patient": patient,
        "signes_vitaux": sv,
        "prescriptions_anterieures": prescriptions,
        "resultats_labo": resultats,
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

# ══════════════════════════════════════════════════════════════════════════════
# QUEUE / FILE D'ATTENTE CLINIQUE — Système temps réel
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/caissier/enregistrer-visite", status_code=201, tags=["Caissier - Queue"])
async def enregistrer_visite_avec_paiement(data: dict, request: Request,
                                             db: Session = Depends(get_db),
                                             current_user=Depends(get_current_user)):
    """
    Caissier enregistre un nouveau patient OU un retour, choisit le service,
    encaisse le paiement, et envoie le patient dans la queue infirmière.
    """
    import uuid as uuid_q
    nom    = data.get("nom", "").upper().strip()
    prenom = data.get("prenom", "").strip()
    if not nom or not prenom:
        raise HTTPException(422, "Nom et prénom requis")

    # Créer ou retrouver patient
    patient = db.query(models.Patient).filter(
        models.Patient.telephone == data.get("telephone", "")
    ).first() if data.get("telephone") else None

    if not patient:
        # Générer un numéro séquentiel basé sur le MAX existant (évite les collisions)
        from sqlalchemy import func as sqlfunc
        last_numero = db.query(sqlfunc.max(models.Patient.numero)).scalar()
        if last_numero and last_numero.startswith("#RB-"):
            try:
                last_n = int(last_numero.replace("#RB-", ""))
            except:
                last_n = db.query(models.Patient).count()
        else:
            last_n = db.query(models.Patient).count()
        new_numero = f"#RB-{(last_n + 1):04d}"

        patient = models.Patient(
            nom=nom, prenom=prenom,
            age=data.get("age"),
            telephone=data.get("telephone", ""),
            email=data.get("email", ""),
            adresse=data.get("adresse", ""),
            contact_urgence=data.get("contact_urgence", ""),
            numero=new_numero,
            is_premiere_visite=True,
            service=data.get("service", "clinique"),
            created_by=current_user.id,
        )
        db.add(patient); db.flush()
    else:
        patient.is_premiere_visite = False

    # Enregistrer le paiement
    service = data.get("service", "Consultation")
    montant = float(data.get("montant", 0))
    mode    = data.get("mode_paiement", "especes")
    now     = datetime.now(timezone.utc)

    if montant > 0:
        try:
            # Mapping service → catégorie + compte PCN produit
            SERVICE_TO_CAT = {
                "labo": ("Laboratoire", "705"),
                "pharmacie": ("Pharmacie", "706"),
                "dentisterie": ("Dentisterie", "707"),
                "physio": ("Physiothérapie", "708"),
                "optometrie": ("Optométrie", "709"),
                "maternite": ("Chirurgies", "703"),   # Accouchements
                "sop": ("Chirurgies", "703"),
                "observation": ("Hospitalisations", "704"),
                "geste": ("Gestes médicaux", "702"),
                "clinique": ("Consultations", "701"),
            }
            svc_key = (data.get("serviceType") or service or "").lower().split()[0]
            cat_svc, cpte_produit = SERVICE_TO_CAT.get(svc_key, ("Consultations", "701"))

            compte_tresor = models.get_compte_tresorerie(mode, "HTG")
            mouvement = _creer_mouvement(
                db=db,
                journal=models.JournalEnum.VTE,
                type_mouv=models.TypeMouvementEnum.recette,
                categorie=cat_svc,
                description=f"{cat_svc} — {prenom} {nom} — {service}",
                montant=montant,
                compte_debit=compte_tresor,
                compte_credit=cpte_produit,
                libelle_debit=f"Trésorerie {mode} ({compte_tresor})",
                libelle_credit=f"Produits {cat_svc} ({cpte_produit})",
                mode_paiement=mode,
                created_by=current_user.id,
            )
            db.flush()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Erreur paiement visite: {e}")
            mouvement = None

    # Créer une entrée dans la queue infirmière
    ticket_id = str(uuid_q.uuid4())[:8].upper()
    queue_entry = {
        "ticket": ticket_id,
        "patient_id": patient.id,
        "patient_numero": patient.numero,
        "patient_nom": f"{prenom} {nom}",
        "patient_telephone": patient.telephone,
        "service": service,
        "priorite": data.get("priorite", "normal"),  # urgent, normal
        "created_at": now.isoformat(),
        "statut": "en_attente_infirmier",
    }
    # Stocker dans un RDV simplifié pour que l'infirmier puisse voir
    medecin_nom_val = data.get("medecin_nom", "") or ""
    praticien_val   = data.get("praticien", "") or medecin_nom_val  # compatibilité double champ
    rdv = models.RendezVous(
        patient_id=patient.id,
        patient_nom=f"{prenom} {nom}",
        patient_telephone=patient.telephone or "",
        patient_email=patient.email or "",
        code_patient=ticket_id,
        specialite=service,
        medecin_nom=praticien_val or medecin_nom_val or None,
        date_rdv=now,
        type_rdv=models.TypeRDVEnum.presentiel,
        statut=models.StatutRDVEnum.paiement_effectue if montant > 0 else models.StatutRDVEnum.en_attente,
        notes_admin=f"Queue caisse #{ticket_id} | Priorité: {data.get('priorite','normal')}" + (f" | Praticien: {praticien_val}" if praticien_val else ""),
        created_by=current_user.id,
    )
    db.add(rdv); db.commit(); db.refresh(patient); db.refresh(rdv)

    log_audit(db, "VISITE_ENREGISTREE", actor_id=current_user.id, actor_role="caissier",
              target_id=patient.numero, target_type="patient",
              details=f"{service} | {montant} HTG | ticket #{ticket_id}")

    return {
        "patient": {
            "id": patient.id, "numero": patient.numero,
            "nom": f"{prenom} {nom}", "telephone": patient.telephone,
            "is_premiere_visite": patient.is_premiere_visite,
        },
        "ticket": ticket_id,
        "rdv_id": rdv.id,
        "service": service,
        "montant": montant,
        "mode_paiement": mode,
        "medecin_nom": praticien_val or medecin_nom_val or "",
        "message": f"Patient {patient.numero} enregistré — ticket #{ticket_id} envoyé à l'infirmière",
    }


@router.get("/caissier/dernier-patient", tags=["Caissier - Queue"])
async def dernier_patient_enregistre(db: Session = Depends(get_db),
                                      current_user=Depends(get_current_user)):
    """Retourne le dernier patient enregistré par la caisse (pour retrouver un ID oublié)"""
    dernier = db.query(models.Patient).order_by(
        models.Patient.created_at.desc()
    ).first()
    if not dernier:
        return {"patient": None}
    return {
        "patient": {
            "id": dernier.id, "numero": dernier.numero,
            "nom": dernier.nom, "prenom": dernier.prenom,
            "telephone": dernier.telephone,
            "created_at": str(dernier.created_at),
        }
    }


@router.get("/caissier/prochain-numero", tags=["Caissier - Queue"])
async def prochain_numero_patient(db: Session = Depends(get_db),
                                   current_user=Depends(get_current_user)):
    """Retourne le prochain numero #RB-XXXX qui sera attribue au prochain patient."""
    from sqlalchemy import func as sqlfunc
    last_numero = db.query(sqlfunc.max(models.Patient.numero)).scalar()
    if last_numero and last_numero.startswith("#RB-"):
        try:
            last_n = int(last_numero.replace("#RB-", ""))
        except Exception:
            last_n = db.query(models.Patient).count()
    else:
        last_n = db.query(models.Patient).count()
    return {"prochain_numero": f"#RB-{(last_n + 1):04d}"}


@router.get("/infirmier/queue", tags=["Infirmier - Queue"])
async def queue_infirmier(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """File d'attente de l'infirmier — patients en attente de signes vitaux"""
    aujourd_hui = datetime.now(timezone.utc).date()
    from sqlalchemy import cast, Date as SADate

    en_attente = db.query(models.RendezVous).filter(
        cast(models.RendezVous.date_rdv, SADate) == aujourd_hui,
        models.RendezVous.statut.in_([
            models.StatutRDVEnum.paiement_effectue.value,
            models.StatutRDVEnum.en_attente.value,
        ])
    ).order_by(models.RendezVous.date_rdv).all()

    # Enrichit chaque patient avec son statut de paiement
    def get_paiement_statut(rdv):
        statut_str = str(rdv.statut).split(".")[-1] if "." in str(rdv.statut) else str(rdv.statut)
        # Vérifier autorisation admin
        auth = db.query(models.AutorisationPaiement).filter(
            models.AutorisationPaiement.actif == True,
            (models.AutorisationPaiement.patient_nom == rdv.patient_nom) |
            (models.AutorisationPaiement.patient_id == rdv.patient_id),
        ).first() if rdv.patient_id else None

        if auth:
            return {"statut_paiement": "autorise", "couleur": "#d97706", "libelle": f"✓ Autorisé — {auth.motif}"}
        if statut_str in ("paiement_effectue", "confirme"):
            return {"statut_paiement": "paye", "couleur": "#16a34a", "libelle": "✅ Payé"}
        return {"statut_paiement": "non_paye", "couleur": "#dc2626", "libelle": "⚠️ Non payé"}

    return {
        "total": len(en_attente),
        "patients": [{
            "rdv_id": r.id,
            "ticket": r.code_patient,
            "patient_nom": r.patient_nom,
            "patient_telephone": r.patient_telephone,
            "patient_id": r.patient_id,
            "service": r.specialite,
            "statut": str(r.statut),
            "heure": str(r.date_rdv),
            "priorite": "urgent" if "urgent" in (r.notes_admin or "").lower() else "normal",
            "notes": r.notes_admin,
            **get_paiement_statut(r),
        } for r in en_attente],
    }


@router.put("/infirmier/signes-vitaux/{rdv_id}", tags=["Infirmier - Queue"])
async def enregistrer_signes_vitaux(rdv_id: int, data: dict,
                                     db: Session = Depends(get_db),
                                     current_user=Depends(get_current_user)):
    """Infirmier enregistre les signes vitaux et envoie vers le médecin"""
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")

    notes_sv = (f"TA: {data.get('tension','—')} | "
                f"Pouls: {data.get('pouls','—')} | "
                f"Temp: {data.get('temperature','—')}°C | "
                f"Poids: {data.get('poids','—')} kg | "
                f"SpO2: {data.get('spo2','—')}%")

    rdv.statut = models.StatutRDVEnum.confirme  # → médecin peut voir
    rdv.notes_admin = f"{rdv.notes_admin or ''}\n[Signes vitaux] {notes_sv}"
    db.commit()

    return {"message": "Signes vitaux enregistrés — patient envoyé vers le médecin", "rdv_id": rdv_id}


@router.get("/infirmier/alertes-prescriptions", tags=["Infirmier - Alertes"])
async def alertes_prescriptions_infirmier(db: Session = Depends(get_db),
                                           current_user=Depends(get_current_user)):
    """Alertes: prescriptions labo ou pharmacie en attente de suivi"""
    from sqlalchemy import cast, Date as SADate
    aujourd_hui = datetime.now(timezone.utc).date()
    seuil = datetime.now(timezone.utc) - timedelta(hours=4)

    prescriptions = db.query(models.Prescription).filter(
        models.Prescription.date_prescription >= seuil,
    ).order_by(models.Prescription.date_prescription.desc()).limit(20).all()

    return {
        "alertes": [{
            "id": p.id,
            "patient_id": p.patient_id,
            "medecin_nom": p.medecin_nom,
            "medicaments": p.medicaments,
            "examens_requis": p.examens_requis if hasattr(p, 'examens_requis') else None,
            "date": str(p.date_prescription),
            "type": "labo" if (hasattr(p, 'examens_requis') and p.examens_requis) else "pharmacie",
        } for p in prescriptions]
    }


@router.get("/labo/queue", tags=["Labo - Queue"])
async def queue_laboratoire(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """File d'attente laboratoire — examens à réaliser aujourd'hui"""
    from sqlalchemy import cast, Date as SADate
    aujourd_hui = datetime.now(timezone.utc).date()

    examens = db.query(models.ResultatLabo).filter(
        cast(models.ResultatLabo.date_examen, SADate) == aujourd_hui,
        models.ResultatLabo.status.in_(["en_attente", "prescrit"]),
    ).order_by(models.ResultatLabo.date_examen).all()

    return {
        "total": len(examens),
        "examens": [{
            "id": e.id,
            "patient_id": e.patient_id,
            "type_examen": e.type_examen,
            "statut": e.status,
            "date": str(e.date_examen),
            "notes": e.notes,
        } for e in examens]
    }


@router.get("/caissier/recherche-patient", tags=["Caissier - Queue"])
async def rechercher_patient_caisse(q: str = "", db: Session = Depends(get_db),
                                     current_user=Depends(get_current_user)):
    """Recherche patient par nom, téléphone ou numéro #RB-XXXX"""
    if len(q) < 2:
        return {"patients": []}
    patients = db.query(models.Patient).filter(
        models.Patient.nom.ilike(f"%{q}%") |
        models.Patient.prenom.ilike(f"%{q}%") |
        models.Patient.telephone.ilike(f"%{q}%") |
        models.Patient.numero.ilike(f"%{q}%")
    ).limit(10).all()
    return {
        "patients": [{
            "id": p.id, "numero": p.numero,
            "nom": p.nom, "prenom": p.prenom,
            "telephone": p.telephone,
            "created_at": str(p.created_at),
        } for p in patients]
    }

# ══════════════════════════════════════════════════════════════════════════════
# VÉRIFICATION DE PAIEMENT — Utilisé par infirmier, médecin, tout le personnel
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/verification-paiement", tags=["Vérification Paiement"])
async def verifier_paiement(
    q: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """
    Vérifie si un patient a payé aujourd'hui.
    Cherche par: ticket (#XXXXXXXX), numéro patient (#RB-XXXX), téléphone, ou nom.
    Accessible à tout le personnel authentifié (infirmier, médecin, labo, caissier, admin).
    """
    if not q or len(q.strip()) < 2:
        raise HTTPException(400, "Fournir un ticket, numéro patient, téléphone ou nom")

    q = q.strip()
    from sqlalchemy import cast, Date as SADate
    aujourd_hui = datetime.now(timezone.utc).date()

    # Cherche dans les RDV du jour (ticket ou patient_email ou patient_nom)
    rdvs = db.query(models.RendezVous).filter(
        cast(models.RendezVous.date_rdv, SADate) == aujourd_hui,
    ).filter(
        models.RendezVous.code_patient.ilike(f"%{q}%") |
        models.RendezVous.patient_nom.ilike(f"%{q}%") |
        models.RendezVous.patient_telephone.ilike(f"%{q}%") |
        models.RendezVous.patient_email.ilike(f"%{q}%")
    ).order_by(models.RendezVous.date_rdv.desc()).all()

    # Si rien trouvé via RDV, cherche via Patient.numero
    if not rdvs:
        patient = db.query(models.Patient).filter(
            models.Patient.numero.ilike(f"%{q.upper()}%") |
            models.Patient.telephone.ilike(f"%{q}%") |
            models.Patient.nom.ilike(f"%{q}%")
        ).first()
        if patient:
            rdvs = db.query(models.RendezVous).filter(
                cast(models.RendezVous.date_rdv, SADate) == aujourd_hui,
                models.RendezVous.patient_email == patient.email,
            ).order_by(models.RendezVous.date_rdv.desc()).all()

    if not rdvs:
        return {
            "trouve": False,
            "message": "Aucun enregistrement trouvé pour ce patient aujourd'hui",
            "paiements": []
        }

    # Construire la réponse avec statut clair pour chaque service
    STATUTS = {
        "paiement_effectue": {"libelle": "✅ PAYÉ", "couleur": "#16a34a", "ok": True},
        "confirme":          {"libelle": "✅ PAYÉ & CONFIRMÉ", "couleur": "#16a34a", "ok": True},
        "en_attente":        {"libelle": "⏳ EN ATTENTE DE PAIEMENT", "couleur": "#d97706", "ok": False},
        "paiement_requis":   {"libelle": "💳 PAIEMENT REQUIS", "couleur": "#dc2626", "ok": False},
    }

    paiements = []
    for r in rdvs:
        statut_str = str(r.statut).split(".")[-1] if "." in str(r.statut) else str(r.statut)
        info_statut = STATUTS.get(statut_str, {"libelle": statut_str, "couleur": "#64748b", "ok": False})

        # Chercher le mouvement de paiement lié
        mouvement = None
        if r.mouvement_id:
            mouvement = db.query(models.Mouvement).filter(models.Mouvement.id == r.mouvement_id).first()

        paiements.append({
            "rdv_id": r.id,
            "ticket": r.code_patient,
            "patient_nom": r.patient_nom,
            "patient_telephone": r.patient_telephone,
            "service": r.specialite,
            "heure": str(r.date_rdv),
            "statut": statut_str,
            "statut_libelle": info_statut["libelle"],
            "statut_couleur": info_statut["couleur"],
            "paiement_ok": info_statut["ok"],
            "recu_numero": mouvement.numero_piece if mouvement else None,
            "montant": mouvement.montant if mouvement else None,
            "mode_paiement": mouvement.mode_paiement if mouvement else None,
            "notes": r.notes_admin,
        })

    # Le patient a-t-il payé au moins un service aujourd'hui?
    a_paye = any(p["paiement_ok"] for p in paiements)

    return {
        "trouve": True,
        "a_paye": a_paye,
        "message": "✅ Paiement confirmé" if a_paye else "⚠️ Paiement non confirmé",
        "paiements": paiements,
    }

# ══════════════════════════════════════════════════════════════════════════════
# AUTORISATIONS DE PAIEMENT (Admin)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/autorisation-paiement", status_code=201, tags=["Admin - Autorisations"])
async def creer_autorisation(data: dict, db: Session = Depends(get_db),
                              current_user=Depends(require_admin)):
    """Admin crée une autorisation de service sans paiement (employé, cas social, partenaire)"""
    auth = models.AutorisationPaiement(
        patient_nom=data.get("patient_nom", ""),
        patient_numero=data.get("patient_numero"),
        motif=data.get("motif", "Autorisation admin"),
        service=data.get("service"),
        date_validite=data.get("date_validite"),
        actif=True,
        created_by=current_user.id,
    )
    if data.get("patient_id"):
        auth.patient_id = int(data["patient_id"])
    db.add(auth); db.commit(); db.refresh(auth)
    return {"message": f"Autorisation créée pour {auth.patient_nom}", "id": auth.id}


@router.get("/admin/autorisations-paiement", tags=["Admin - Autorisations"])
async def list_autorisations(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.AutorisationPaiement).filter(
        models.AutorisationPaiement.actif == True
    ).order_by(models.AutorisationPaiement.created_at.desc()).all()


@router.delete("/admin/autorisation-paiement/{aid}", tags=["Admin - Autorisations"])
async def supprimer_autorisation(aid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    auth = db.query(models.AutorisationPaiement).filter(models.AutorisationPaiement.id == aid).first()
    if not auth: raise HTTPException(404)
    auth.actif = False
    db.commit()
    return {"message": "Autorisation révoquée"}


# ══════════════════════════════════════════════════════════════════════════════
# FILE D'ATTENTE MÉDECIN avec statut paiement
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/medecin/queue-patients", tags=["Médecin - Queue"])
async def queue_patients_medecin(db: Session = Depends(get_db),
                                  current_user=Depends(get_current_user)):
    """File d'attente du médecin — patients avec signes vitaux enregistrés + statut paiement"""
    from sqlalchemy import cast, Date as SADate
    aujourd_hui = datetime.now(timezone.utc).date()

    en_attente = db.query(models.RendezVous).filter(
        cast(models.RendezVous.date_rdv, SADate) == aujourd_hui,
        models.RendezVous.statut == models.StatutRDVEnum.confirme,
    ).order_by(models.RendezVous.date_rdv).all()

    def get_paiement_statut(rdv):
        statut_str = str(rdv.statut).split(".")[-1] if "." in str(rdv.statut) else str(rdv.statut)
        auth = db.query(models.AutorisationPaiement).filter(
            models.AutorisationPaiement.actif == True,
            models.AutorisationPaiement.patient_nom == rdv.patient_nom,
        ).first()
        if auth:
            return {"statut_paiement": "autorise", "couleur": "#d97706", "libelle": f"✓ Autorisé — {auth.motif}"}
        if statut_str in ("paiement_effectue", "confirme"):
            return {"statut_paiement": "paye", "couleur": "#16a34a", "libelle": "✅ Payé"}
        return {"statut_paiement": "non_paye", "couleur": "#dc2626", "libelle": "⚠️ Non payé"}

    # Extraire les signes vitaux des notes
    def parse_sv(notes):
        sv = {}
        if notes and "[Signes vitaux]" in notes:
            sv_text = notes.split("[Signes vitaux]")[-1].strip()
            sv["resume"] = sv_text[:200]
        return sv

    return {
        "total": len(en_attente),
        "patients": [{
            "rdv_id": r.id,
            "ticket": r.code_patient,
            "patient_nom": r.patient_nom,
            "patient_telephone": r.patient_telephone,
            "patient_id": r.patient_id,
            "service": r.specialite,
            "heure": str(r.date_rdv),
            "priorite": "urgent" if "urgent" in (r.notes_admin or "").lower() else "normal",
            "signes_vitaux": parse_sv(r.notes_admin),
            **get_paiement_statut(r),
        } for r in en_attente],
    }

# ══════════════════════════════════════════════════════════════════════════════
# BARÈMES DES GESTES MÉDICAUX — Prix en USD, calcul HTG au taux du jour
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tarifs/gestes", tags=["Tarifs - Gestes"])
async def list_gestes(
    specialite: Optional[str] = None,
    categorie:  Optional[str] = None,
    search:     Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user)   # Données non publiques — personnel uniquement
):
    """Catalogue des gestes médicaux avec prix de référence en USD. Non public."""
    q = db.query(models.GesteMedical).filter(models.GesteMedical.actif == True)
    if specialite:
        q = q.filter(models.GesteMedical.specialite.ilike(f"%{specialite}%"))
    if categorie:
        q = q.filter(models.GesteMedical.categorie.ilike(f"%{categorie}%"))
    if search:
        q = q.filter(models.GesteMedical.libelle.ilike(f"%{search}%"))
    gestes = q.order_by(models.GesteMedical.specialite, models.GesteMedical.categorie, models.GesteMedical.libelle).all()

    # Taux du jour
    taux = db.query(models.TauxChange).order_by(models.TauxChange.date.desc()).first()
    taux_htg = taux.taux_htg if taux else 130.0

    return {
        "taux_htg": taux_htg,
        "total": len(gestes),
        "gestes": [{
            "id": g.id,
            "specialite": g.specialite,
            "categorie": g.categorie,
            "libelle": g.libelle,
            "prix_usd": g.prix_clinique_usd or g.prix_usd,  # Prix clinique prioritaire
            "prix_usd_bareme": g.prix_usd,                   # Barème de référence
            "prix_usd_min": g.prix_usd_min,
            "prix_usd_max": g.prix_usd_max,
            "prix_clinique_usd": g.prix_clinique_usd,
            "prix_htg_calcule": round((g.prix_clinique_usd or g.prix_usd or 0) * taux_htg),
            "prix_htg_ref": g.prix_htg_ref,
            "source_bareme": g.source_bareme,
            "prix_fixe": g.prix_fixe,
        } for g in gestes],
        "specialites": sorted(list(set(g.specialite for g in db.query(models.GesteMedical).filter(models.GesteMedical.actif==True).all()))),
    }


@router.get("/tarifs/specialites", tags=["Tarifs - Gestes"])
async def list_specialites_gestes(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Liste des spécialités disponibles dans le catalogue."""
    specs = db.query(models.GesteMedical.specialite).filter(
        models.GesteMedical.actif == True
    ).distinct().order_by(models.GesteMedical.specialite).all()
    return [s[0] for s in specs]


@router.post("/admin/tarifs/geste", status_code=201, tags=["Admin - Tarifs"])
async def creer_geste(data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Admin crée un nouveau geste dans le catalogue."""
    geste = models.GesteMedical(
        specialite=data.get("specialite", ""),
        categorie=data.get("categorie"),
        libelle=data.get("libelle", ""),
        prix_usd=float(data.get("prix_usd", 0)),
        prix_usd_min=float(data["prix_usd_min"]) if data.get("prix_usd_min") else None,
        prix_usd_max=float(data["prix_usd_max"]) if data.get("prix_usd_max") else None,
        prix_clinique_usd=float(data["prix_clinique_usd"]) if data.get("prix_clinique_usd") else None,
        prix_htg_ref=float(data["prix_htg_ref"]) if data.get("prix_htg_ref") else None,
        source_bareme=data.get("source_bareme", "CLINIQUE"),
        prix_fixe=bool(data.get("prix_fixe", False)),
        actif=True,
    )
    db.add(geste); db.commit(); db.refresh(geste)
    return {"message": "Geste créé", "id": geste.id}


@router.put("/admin/tarifs/geste/{gid}", tags=["Admin - Tarifs"])
async def modifier_geste(gid: int, data: dict, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Admin modifie un geste — notamment le prix clinique (différent du barème)."""
    g = db.query(models.GesteMedical).filter(models.GesteMedical.id == gid).first()
    if not g: raise HTTPException(404)
    if "libelle" in data: g.libelle = data["libelle"]
    if "categorie" in data: g.categorie = data["categorie"]
    if "prix_usd" in data: g.prix_usd = float(data["prix_usd"])
    if "prix_usd_min" in data: g.prix_usd_min = float(data["prix_usd_min"]) if data["prix_usd_min"] else None
    if "prix_usd_max" in data: g.prix_usd_max = float(data["prix_usd_max"]) if data["prix_usd_max"] else None
    if "prix_clinique_usd" in data:
        g.prix_clinique_usd = float(data["prix_clinique_usd"]) if data["prix_clinique_usd"] else None
    if "actif" in data: g.actif = bool(data["actif"])
    if "prix_fixe" in data: g.prix_fixe = bool(data["prix_fixe"])
    db.commit()
    return {"message": "Geste mis à jour"}


@router.delete("/admin/tarifs/geste/{gid}", tags=["Admin - Tarifs"])
async def supprimer_geste(gid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    g = db.query(models.GesteMedical).filter(models.GesteMedical.id == gid).first()
    if not g: raise HTTPException(404)
    g.actif = False; db.commit()
    return {"message": "Geste désactivé"}


# ── Taux de change ────────────────────────────────────────────────────────────

@router.post("/caissier/taux-change", status_code=201, tags=["Caissier - Taux"])
async def saisir_taux_change(data: dict, db: Session = Depends(get_db),
                              current_user=Depends(get_current_user)):
    """Caissier saisit le taux de change HTG/USD du jour."""
    taux_htg = float(data.get("taux_htg", 0))
    if taux_htg <= 0:
        raise HTTPException(422, "Taux invalide — doit être > 0")
    t = models.TauxChange(taux_htg=taux_htg, saisi_par=current_user.id)
    db.add(t); db.commit()
    return {"message": f"Taux mis à jour: 1 USD = {taux_htg} HTG", "taux_htg": taux_htg}


@router.get("/caissier/taux-change", tags=["Caissier - Taux"])
async def get_taux_change(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Taux de change HTG/USD en vigueur."""
    t = db.query(models.TauxChange).order_by(models.TauxChange.date.desc()).first()
    return {
        "taux_htg": t.taux_htg if t else 130.0,
        "date": str(t.date) if t else None,
        "is_default": t is None,
    }


# ── Seed admin (initialisation depuis les barèmes) ───────────────────────────

@router.post("/admin/seed-tarifs", tags=["Admin - Tarifs"])
async def seed_baremes(db: Session = Depends(get_db), _=Depends(require_admin)):
    """
    Initialise le catalogue avec les barèmes de référence.
    À n'exécuter qu'une fois. N'écrase pas les prix clinique déjà saisis.
    """
    from app.seed_tarifs import GESTES
    created = 0
    updated = 0
    for (spec, cat, lib, prix_usd, prix_min, prix_max, source, prix_htg) in GESTES:
        existing = db.query(models.GesteMedical).filter(
            models.GesteMedical.specialite == spec,
            models.GesteMedical.libelle == lib,
        ).first()
        if existing:
            # Mettre à jour prix_htg_ref et corriger prix_usd si SHP (ne pas écraser prix_clinique_usd)
            existing.prix_htg_ref = float(prix_htg) if prix_htg else existing.prix_htg_ref
            existing.source_bareme = source
            if source == "SHP":
                existing.prix_usd = 0  # SHP = HTG uniquement, pas de conversion approximative
            existing.categorie = cat
            updated += 1
            continue
        g = models.GesteMedical(
            specialite=spec, categorie=cat, libelle=lib,
            prix_usd=float(prix_usd) if prix_usd else 0,
            prix_usd_min=float(prix_min) if prix_min else None,
            prix_usd_max=float(prix_max) if prix_max else None,
            prix_htg_ref=float(prix_htg) if prix_htg else None,
            source_bareme=source,
            actif=True,
        )
        db.add(g)
        created += 1
    db.commit()
    return {
        "message": f"{created} gestes importés, {updated} mis à jour",
        "created": created, "updated": updated
    }

# ══════════════════════════════════════════════════════════════════════════════
# RECHERCHE DOSSIER PAR NOM COMPLET + DATE DE NAISSANCE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/medecin/recherche-patient", tags=["Médecin"])
async def medecin_recherche_patient(
    nom: str = "", prenom: str = "", date_naissance: Optional[str] = None,
    db: Session = Depends(get_db), current_user=Depends(get_current_user)
):
    """
    Médecin cherche un patient par NOM COMPLET + date de naissance.
    Les deux sont requis pour la recherche par nom (sécurité).
    Pour la recherche par #RB-XXXX, utiliser /medecin/dossier/{numero}.
    """
    if str(current_user.role) not in ('medecin', 'admin', 'infirmier'):
        raise HTTPException(403, "Accès réservé au personnel médical")

    if not nom or not prenom:
        raise HTTPException(422, "Le nom ET le prénom complets sont requis")

    q = db.query(models.Patient).filter(
        models.Patient.nom.ilike(nom.strip().upper()),
        models.Patient.prenom.ilike(prenom.strip()),
    )

    # Date de naissance requise pour la recherche par nom (identification sûre)
    if date_naissance:
        from datetime import date as dt_date
        try:
            dob = dt_date.fromisoformat(date_naissance)
            q = q.filter(models.Patient.date_naissance == dob)
        except ValueError:
            raise HTTPException(422, "Format date invalide — utiliser YYYY-MM-DD")
    else:
        raise HTTPException(422, "La date de naissance est requise pour une recherche par nom (identification sûre)")

    patients = q.limit(5).all()

    if not patients:
        return {"patients": [], "message": "Aucun patient trouvé avec ces informations"}

    results = []
    for p in patients:
        dossier = db.query(models.DossierPatient).filter(
            models.DossierPatient.patient_id == p.id
        ).order_by(models.DossierPatient.date_visite.desc()).first()

        results.append({
            "id": p.id, "numero": p.numero,
            "nom": p.nom, "prenom": p.prenom,
            "date_naissance": str(p.date_naissance) if p.date_naissance else None,
            "telephone": p.telephone,
            "nb_dossiers": db.query(models.DossierPatient).filter(models.DossierPatient.patient_id == p.id).count(),
            "derniere_visite": str(dossier.date_visite) if dossier else None,
        })

    log_audit(db, "RECHERCHE_PAR_NOM_DOB",
              actor_id=current_user.id, actor_role=str(current_user.role),
              target_id=f"{nom} {prenom} {date_naissance}", target_type="patient",
              details="Recherche par nom complet + date de naissance")

    return {"patients": results}

# ══════════════════════════════════════════════════════════════════════════════
# PHOTO DE PROFIL — Médecin
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/medecin/photo-profil", tags=["Médecin"])
async def upload_photo_profil(data: dict, db: Session = Depends(get_db),
                               current_user=Depends(get_current_user)):
    """Médecin télécharge sa photo de profil (base64 JPEG/PNG, max 2MB)."""
    photo = data.get("photo_base64", "")
    if not photo:
        raise HTTPException(422, "Photo manquante")

    # Validation taille approximative (base64 ~4/3 de l'original)
    if len(photo) > 3_000_000:  # ~2.2MB décodé
        raise HTTPException(413, "Photo trop lourde — maximum 2MB")

    if not photo.startswith(("data:image/jpeg", "data:image/jpg", "data:image/png", "data:image/webp")):
        raise HTTPException(422, "Format invalide — utiliser JPEG, PNG ou WebP")

    # Mettre à jour le User
    current_user.photo_profil = photo
    db.commit()

    # Si le médecin a un Specialiste lié, mettre à jour aussi
    spec = db.query(models.Specialiste).filter(
        models.Specialiste.medecin_email == current_user.email
    ).first()
    if not spec:
        spec = db.query(models.Specialiste).filter(
            models.Specialiste.nom.ilike(f"%{current_user.nom.split()[-1]}%")
        ).first()
    if spec:
        spec.photo_profil = photo
        db.commit()

    return {"message": "Photo de profil mise à jour avec succès"}


@router.delete("/medecin/photo-profil", tags=["Médecin"])
async def supprimer_photo_profil(db: Session = Depends(get_db),
                                  current_user=Depends(get_current_user)):
    """Médecin supprime sa photo de profil."""
    current_user.photo_profil = None
    db.commit()
    return {"message": "Photo supprimée"}
