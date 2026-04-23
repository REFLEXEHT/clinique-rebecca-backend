from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import app.models as models
import app.schemas as schemas
from app.database import get_db
from app.auth import get_current_user, require_admin, verify_password, get_password_hash, create_access_token
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
        raise HTTPException(status_code=403, detail="Compte désactivé")

    token = create_access_token({"sub": str(user.id)})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "nom": user.nom, "email": user.email, "role": user.role},
    }


@router.get("/auth/me", tags=["Auth"])
async def me(current_user: models.User = Depends(get_current_user)):
    return {"id": current_user.id, "nom": current_user.nom, "email": current_user.email, "role": current_user.role}


# ══════════════════════════════════════════════════════════════════════════════
# SERVICES (public + admin)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/services", response_model=List[schemas.ServiceOut], tags=["Services"])
async def list_services(db: Session = Depends(get_db)):
    return db.query(models.Service).filter(models.Service.actif == True).order_by(models.Service.ordre).all()


@router.post("/admin/services", response_model=schemas.ServiceOut, tags=["Admin - Services"])
async def create_service(
    data: schemas.ServiceCreate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    svc = models.Service(**data.model_dump())
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


@router.put("/admin/services/{service_id}", response_model=schemas.ServiceOut, tags=["Admin - Services"])
async def update_service(
    service_id: int,
    data: schemas.ServiceUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    svc = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not svc:
        raise HTTPException(404, "Service introuvable")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(svc, k, v)
    db.commit()
    db.refresh(svc)
    return svc


@router.delete("/admin/services/{service_id}", tags=["Admin - Services"])
async def delete_service(
    service_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    svc = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not svc:
        raise HTTPException(404, "Service introuvable")
    svc.actif = False
    db.commit()
    return {"message": "Service supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# SPECIALISTES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/specialistes", response_model=List[schemas.SpecialisteOut], tags=["Spécialistes"])
async def list_specialistes(
    categorie: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Specialiste).filter(models.Specialiste.actif == True)
    if categorie and categorie != "tous":
        q = q.filter(models.Specialiste.categorie.in_([categorie, "tous"]))
    return q.order_by(models.Specialiste.ordre).all()


@router.post("/admin/specialistes", response_model=schemas.SpecialisteOut, tags=["Admin - Spécialistes"])
async def create_specialiste(
    data: schemas.SpecialisteCreate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    spec = models.Specialiste(**data.model_dump())
    db.add(spec)
    db.commit()
    db.refresh(spec)
    return spec


@router.put("/admin/specialistes/{spec_id}", response_model=schemas.SpecialisteOut, tags=["Admin - Spécialistes"])
async def update_specialiste(
    spec_id: int,
    data: schemas.SpecialisteUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    spec = db.query(models.Specialiste).filter(models.Specialiste.id == spec_id).first()
    if not spec:
        raise HTTPException(404, "Spécialiste introuvable")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(spec, k, v)
    db.commit()
    db.refresh(spec)
    return spec


@router.delete("/admin/specialistes/{spec_id}", tags=["Admin - Spécialistes"])
async def delete_specialiste(
    spec_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    spec = db.query(models.Specialiste).filter(models.Specialiste.id == spec_id).first()
    if not spec:
        raise HTTPException(404, "Spécialiste introuvable")
    spec.actif = False
    db.commit()
    return {"message": "Spécialiste supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# HORAIRES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/horaires", response_model=List[schemas.HoraireOut], tags=["Horaires"])
async def get_horaires(db: Session = Depends(get_db)):
    return db.query(models.Horaire).order_by(models.Horaire.id).all()


@router.put("/admin/horaires/{jour}", response_model=schemas.HoraireOut, tags=["Admin - Horaires"])
async def update_horaire(
    jour: str,
    data: schemas.HoraireUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    h = db.query(models.Horaire).filter(models.Horaire.jour == jour).first()
    if not h:
        raise HTTPException(404, "Jour introuvable")
    h.ouvert = data.ouvert
    h.heure_ouverture = data.heure_ouverture
    h.heure_fermeture = data.heure_fermeture
    db.commit()
    db.refresh(h)
    return h


# ══════════════════════════════════════════════════════════════════════════════
# RENDEZ-VOUS (public)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/rendez-vous", response_model=schemas.RendezVousOut, status_code=201, tags=["Rendez-vous"])
async def create_rdv(data: schemas.RendezVousCreate, db: Session = Depends(get_db)):
    rdv = models.RendezVous(**data.model_dump())
    db.add(rdv)
    db.commit()
    db.refresh(rdv)

    # Notifier en background
    rdv_data = {
        "patient_nom": rdv.patient_nom,
        "patient_telephone": rdv.patient_telephone,
        "patient_email": rdv.patient_email,
        "specialite": rdv.specialite,
        "date_rdv": rdv.date_rdv,
        "type_rdv": rdv.type_rdv,
        "motif": rdv.motif,
    }
    asyncio.create_task(notify_rdv_confirmed(rdv_data))

    return rdv


# ══════════════════════════════════════════════════════════════════════════════
# RENDEZ-VOUS (admin)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/rendez-vous", response_model=List[schemas.RendezVousOut], tags=["Admin - RDV"])
async def admin_list_rdv(
    statut: Optional[str] = None,
    date_debut: Optional[datetime] = None,
    date_fin: Optional[datetime] = None,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    q = db.query(models.RendezVous)
    if statut:
        q = q.filter(models.RendezVous.statut == statut)
    if date_debut:
        q = q.filter(models.RendezVous.date_rdv >= date_debut)
    if date_fin:
        q = q.filter(models.RendezVous.date_rdv <= date_fin)
    return q.order_by(models.RendezVous.date_rdv.desc()).all()


@router.put("/admin/rendez-vous/{rdv_id}", response_model=schemas.RendezVousOut, tags=["Admin - RDV"])
async def update_rdv(
    rdv_id: int,
    data: schemas.RendezVousUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(rdv, k, v)
    db.commit()
    db.refresh(rdv)
    return rdv


@router.delete("/admin/rendez-vous/{rdv_id}", tags=["Admin - RDV"])
async def cancel_rdv(
    rdv_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    rdv = db.query(models.RendezVous).filter(models.RendezVous.id == rdv_id).first()
    if not rdv:
        raise HTTPException(404, "RDV introuvable")
    rdv.statut = "annule"
    db.commit()
    return {"message": "RDV annulé"}


# ══════════════════════════════════════════════════════════════════════════════
# COMPTABILITÉ (admin)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/mouvements", response_model=List[schemas.MouvementOut], tags=["Admin - Compta"])
async def list_mouvements(
    type: Optional[str] = None,
    mois: Optional[int] = None,
    annee: Optional[int] = None,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    q = db.query(models.Mouvement)
    if type:
        q = q.filter(models.Mouvement.type == type)
    if annee:
        q = q.filter(extract("year", models.Mouvement.date_mouvement) == annee)
    if mois:
        q = q.filter(extract("month", models.Mouvement.date_mouvement) == mois)
    return q.order_by(models.Mouvement.date_mouvement.desc()).all()


@router.post("/admin/mouvements", response_model=schemas.MouvementOut, status_code=201, tags=["Admin - Compta"])
async def create_mouvement(
    data: schemas.MouvementCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    m = models.Mouvement(**data.model_dump(), created_by=current_user.id)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


@router.delete("/admin/mouvements/{mouvement_id}", tags=["Admin - Compta"])
async def delete_mouvement(
    mouvement_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    m = db.query(models.Mouvement).filter(models.Mouvement.id == mouvement_id).first()
    if not m:
        raise HTTPException(404, "Mouvement introuvable")
    db.delete(m)
    db.commit()
    return {"message": "Mouvement supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD & STATS (admin)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/stats/dashboard", response_model=schemas.DashboardStats, tags=["Admin - Stats"])
async def dashboard_stats(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0)

    rdv_today = db.query(func.count(models.RendezVous.id)).filter(
        models.RendezVous.date_rdv >= today_start
    ).scalar()

    rdv_month = db.query(func.count(models.RendezVous.id)).filter(
        models.RendezVous.date_rdv >= month_start
    ).scalar()

    patients_month = rdv_month  # Simplifié

    recettes_day = db.query(func.sum(models.Mouvement.montant)).filter(
        models.Mouvement.type == "recette",
        models.Mouvement.date_mouvement >= today_start,
    ).scalar() or 0.0

    recettes_month = db.query(func.sum(models.Mouvement.montant)).filter(
        models.Mouvement.type == "recette",
        models.Mouvement.date_mouvement >= month_start,
    ).scalar() or 0.0

    rdv_en_attente = db.query(func.count(models.RendezVous.id)).filter(
        models.RendezVous.statut == "en_attente"
    ).scalar()

    rdv_confirmes = db.query(func.count(models.RendezVous.id)).filter(
        models.RendezVous.statut.in_(["confirme", "termine"])
    ).scalar()
    rdv_total = db.query(func.count(models.RendezVous.id)).scalar() or 1
    taux = round((rdv_confirmes / rdv_total) * 100, 1)

    return {
        "rdv_today": rdv_today,
        "rdv_month": rdv_month,
        "patients_month": patients_month,
        "recettes_day": recettes_day,
        "recettes_month": recettes_month,
        "rdv_en_attente": rdv_en_attente,
        "taux_presence": taux,
    }


@router.get("/admin/stats/rdv-par-jour", tags=["Admin - Stats"])
async def rdv_par_jour(
    jours: int = 7,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    result = []
    now = datetime.now(timezone.utc)
    for i in range(jours - 1, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0)
        day_end = day.replace(hour=23, minute=59, second=59)
        count = db.query(func.count(models.RendezVous.id)).filter(
            models.RendezVous.date_rdv >= day_start,
            models.RendezVous.date_rdv <= day_end,
        ).scalar()
        result.append({"date": day.strftime("%d/%m"), "count": count})
    return result


@router.get("/admin/stats/recettes-par-jour", tags=["Admin - Stats"])
async def recettes_par_jour(
    jours: int = 7,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    result = []
    now = datetime.now(timezone.utc)
    for i in range(jours - 1, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0)
        day_end = day.replace(hour=23, minute=59, second=59)
        total = db.query(func.sum(models.Mouvement.montant)).filter(
            models.Mouvement.type == "recette",
            models.Mouvement.date_mouvement >= day_start,
            models.Mouvement.date_mouvement <= day_end,
        ).scalar() or 0
        result.append({"date": day.strftime("%d/%m"), "total": float(total)})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# PATIENTS (admin)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/patients", tags=["Admin - Patients"])
async def list_patients(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    q = db.query(models.Patient)
    if search:
        q = q.filter(
            models.Patient.nom.ilike(f"%{search}%") |
            models.Patient.telephone.ilike(f"%{search}%")
        )
    return q.order_by(models.Patient.created_at.desc()).limit(100).all()


# ══════════════════════════════════════════════════════════════════════════════
# AI CHAT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/chat", tags=["IA"])
async def chat(data: schemas.ChatMessage):
    response = await chat_with_rebecca(data.message, data.historique)
    return {"response": response}

# ══════════════════════════════════════════════════════════════════════════════
# SETUP TEMPORAIRE — À SUPPRIMER APRÈS UTILISATION
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/setup-admin-temp-xk92")
def setup_admin(db: Session = Depends(get_db)):
    """Endpoint temporaire pour créer l'admin — supprimer après usage"""
    try:
        existing = db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first()
        if existing:
            existing.hashed_password = get_password_hash("rebecca2026")
            existing.role = "admin"
            db.commit()
            return {"status": "Admin mis à jour", "email": "admin@cliniquerebecca.ht"}
        else:
            admin = models.User(
                email="admin@cliniquerebecca.ht",
                nom="Administrateur",
                hashed_password=get_password_hash("rebecca2026"),
                role="admin"
            )
            db.add(admin)
            db.commit()
            return {"status": "Admin créé", "email": "admin@cliniquerebecca.ht", "password": "rebecca2026"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/setup-admin-temp-xk93")
def setup_admin_v2(db: Session = Depends(get_db)):
    try:
        pwd = "rebecca2026"[:72]
        hashed = get_password_hash(pwd)
        existing = db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first()
        if existing:
            existing.hashed_password = hashed
            existing.role = "admin"
            db.commit()
            return {"status": "Admin mis à jour"}
        else:
            admin = models.User(
                email="admin@cliniquerebecca.ht",
                nom="Administrateur",
                hashed_password=hashed,
                role="admin"
            )
            db.add(admin)
            db.commit()
            return {"status": "Admin créé", "email": "admin@cliniquerebecca.ht"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/setup-admin-temp-xk94")
def setup_admin_v3(db: Session = Depends(get_db)):
    try:
        import bcrypt
        pwd = b"Admin2026"
        hashed = bcrypt.hashpw(pwd, bcrypt.gensalt()).decode("utf-8")
        existing = db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first()
        if existing:
            existing.hashed_password = hashed
            existing.role = "admin"
            db.commit()
            return {"status": "ok", "email": "admin@cliniquerebecca.ht", "password": "Admin2026"}
        else:
            admin = models.User(
                email="admin@cliniquerebecca.ht",
                nom="Administrateur",
                hashed_password=hashed,
                role="admin"
            )
            db.add(admin)
            db.commit()
            return {"status": "created", "email": "admin@cliniquerebecca.ht", "password": "Admin2026"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
