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

# ══════════════════════════════════════════════════════════════════════════════
# REGISTER — Inscription avec rôle (en attente de validation admin)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/auth/register", tags=["Auth"])
async def register(data: schemas.UserCreate, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email déjà utilisé")
    # Les patients sont actifs directement, les autres rôles attendent validation
    is_active = data.role == models.RoleEnum.patient
    user = models.User(
        email=data.email,
        nom=data.nom,
        hashed_password=get_password_hash(data.password),
        role=data.role,
        is_active=is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    if is_active:
        token = create_access_token({"sub": str(user.id)})
        return {"access_token": token, "token_type": "bearer", "user": {"id": user.id, "nom": user.nom, "email": user.email, "role": user.role}}
    return {"message": f"Compte créé — en attente de validation par l'administrateur", "role": data.role}

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Gestion des utilisateurs (validation, liste, suppression)
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/admin/users", tags=["Admin - Users"])
async def list_users(db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    users = db.query(models.User).order_by(models.User.created_at.desc()).all()
    return [{"id": u.id, "nom": u.nom, "email": u.email, "role": u.role, "is_active": u.is_active, "created_at": u.created_at} for u in users]

@router.put("/admin/users/{user_id}/activate", tags=["Admin - Users"])
async def activate_user(user_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    user.is_active = True
    db.commit()
    return {"message": f"Compte de {user.nom} activé", "user_id": user_id}

@router.put("/admin/users/{user_id}/deactivate", tags=["Admin - Users"])
async def deactivate_user(user_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    user.is_active = False
    db.commit()
    return {"message": f"Compte de {user.nom} désactivé"}

@router.delete("/admin/users/{user_id}", tags=["Admin - Users"])
async def delete_user(user_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Impossible de supprimer un admin")
    db.delete(user)
    db.commit()
    return {"message": f"Compte supprimé"}

@router.put("/admin/users/{user_id}/role", tags=["Admin - Users"])
async def change_role(user_id: int, role: str, db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    user.role = role
    db.commit()
    return {"message": f"Rôle changé en {role}"}

# ══════════════════════════════════════════════════════════════════════════════
# COMPTABILITÉ — Rapport financier par période
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/admin/rapport-financier", tags=["Admin - Compta"])
async def rapport_financier(
    debut: str, fin: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    from datetime import datetime
    try:
        date_debut = datetime.fromisoformat(debut)
        date_fin = datetime.fromisoformat(fin)
    except:
        raise HTTPException(status_code=400, detail="Format de date invalide (YYYY-MM-DD)")
    
    mouvements = db.query(models.Mouvement).filter(
        models.Mouvement.date_mouvement >= date_debut,
        models.Mouvement.date_mouvement <= date_fin
    ).all()
    
    recettes = [m for m in mouvements if m.type == "recette"]
    depenses = [m for m in mouvements if m.type == "depense"]
    
    total_rec = sum(m.montant for m in recettes)
    total_dep = sum(m.montant for m in depenses)
    
    # Grouper par catégorie
    par_cat_rec = {}
    for m in recettes:
        par_cat_rec[m.categorie] = par_cat_rec.get(m.categorie, 0) + m.montant
    
    par_cat_dep = {}
    for m in depenses:
        par_cat_dep[m.categorie] = par_cat_dep.get(m.categorie, 0) + m.montant

    return {
        "periode": {"debut": debut, "fin": fin},
        "recettes": {"total": total_rec, "par_categorie": par_cat_rec, "transactions": len(recettes)},
        "depenses": {"total": total_dep, "par_categorie": par_cat_dep, "transactions": len(depenses)},
        "resultat_net": total_rec - total_dep,
        "mouvements": [{"id": m.id, "date": m.date_mouvement, "type": m.type, "categorie": m.categorie, "description": m.description, "montant": m.montant, "mode_paiement": m.mode_paiement} for m in mouvements]
    }

# ══════════════════════════════════════════════════════════════════════════════
# PATIENTS — Création et gestion (accessible à tous les rôles staff)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/patients", status_code=201, tags=["Patients"])
async def create_patient(data: dict, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    # Générer code unique #RB-XXX
    count = db.query(models.Patient).count()
    code = f"#RB-{str(count + 1).zfill(3)}"
    patient = models.Patient(
        code=code,
        nom=data.get("nom", ""),
        prenom=data.get("prenom", ""),
        date_naissance=data.get("date_naissance", ""),
        sexe=data.get("sexe", ""),
        telephone=data.get("telephone", ""),
        email=data.get("email", ""),
        adresse=data.get("adresse", ""),
        groupe_sanguin=data.get("groupe_sanguin", ""),
        allergies=data.get("allergies", ""),
        antecedents=data.get("antecedents", ""),
        notes=data.get("notes", ""),
        created_by=current_user.id,
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return {"id": patient.id, "code": patient.code, "nom": patient.nom, "message": f"Patient {code} créé avec succès"}

@router.get("/patients/search", tags=["Patients"])
async def search_patients(q: str = "", db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    patients = db.query(models.Patient).filter(
        (models.Patient.nom.ilike(f"%{q}%")) |
        (models.Patient.prenom.ilike(f"%{q}%")) |
        (models.Patient.code.ilike(f"%{q}%")) |
        (models.Patient.telephone.ilike(f"%{q}%"))
    ).limit(20).all()
    return [{"id": p.id, "code": p.code, "nom": p.nom, "prenom": p.prenom, "telephone": p.telephone, "email": p.email, "date_naissance": p.date_naissance, "sexe": p.sexe, "groupe_sanguin": p.groupe_sanguin} for p in patients]

@router.get("/patients/{patient_id}", tags=["Patients"])
async def get_patient(patient_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    patient = db.query(models.Patient).filter(models.Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient non trouvé")
    return patient

@router.get("/admin/patients-list", tags=["Admin - Patients"])
async def list_patients(db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    patients = db.query(models.Patient).order_by(models.Patient.created_at.desc()).all()
    return [{"id": p.id, "code": p.code, "nom": p.nom, "prenom": p.prenom, "telephone": p.telephone, "email": p.email, "created_at": p.created_at} for p in patients]

# ══════════════════════════════════════════════════════════════════════════════
# CAISSIER — Endpoints accessibles au caissier
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/caissier/mouvements", status_code=201, tags=["Caissier"])
async def caissier_create_mouvement(data: schemas.MouvementCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    m = models.Mouvement(**data.model_dump(), created_by=current_user.id)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m

@router.get("/caissier/mouvements", tags=["Caissier"])
async def caissier_list_mouvements(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    debut = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mouvements = db.query(models.Mouvement).filter(models.Mouvement.date_mouvement >= debut).order_by(models.Mouvement.date_mouvement.desc()).all()
    return mouvements

# ══════════════════════════════════════════════════════════════════════════════
# EXPLOITANTS — Paiements directs (chèque/virement)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/caissier/paiement-exploitant", status_code=201, tags=["Exploitants"])
async def enregistrer_paiement_exploitant(data: dict, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    paiement = models.PaiementExploitant(
        medecin_id=data.get("medecin_id"),
        medecin_nom=data.get("medecin_nom", ""),
        patient_nom=data.get("patient_nom", ""),
        montant=float(data.get("montant", 0)),
        mode_paiement=data.get("mode_paiement", "especes"),
        flux_direct=data.get("flux_direct", False),
        description=data.get("description", ""),
        created_by=current_user.id,
    )
    db.add(paiement)
    # Toujours enregistrer en statistiques même si flux direct
    mouvement = models.Mouvement(
        type=models.TypeMouvementEnum.recette,
        categorie="Exploitant",
        description=f"{data.get('medecin_nom','')} — {data.get('patient_nom','')} — {data.get('description','')}",
        montant=float(data.get("montant", 0)),
        date_mouvement=func.now(),
        mode_paiement=data.get("mode_paiement", "especes"),
        notes=f"Flux direct: {data.get('flux_direct', False)} — Reversement total prévu",
        created_by=current_user.id,
    )
    db.add(mouvement)
    db.commit()
    db.refresh(paiement)
    return paiement

@router.get("/admin/paiements-exploitants", tags=["Exploitants"])
async def list_paiements_exploitants(db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    return db.query(models.PaiementExploitant).order_by(models.PaiementExploitant.date_paiement.desc()).all()

# ══════════════════════════════════════════════════════════════════════════════
# STOCK V2 — Avec propriétaire investisseur
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/pharmacie/stocks-v2", tags=["Pharmacie V2"])
async def get_stocks_v2(db: Session = Depends(get_db)):
    return db.query(models.StockItemV2).all()

@router.post("/admin/stocks-v2", status_code=201, tags=["Pharmacie V2"])
async def create_stock_v2(data: dict, db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    # Calculer pct_clinique selon mode
    mode = data.get("mode_reversement", "clinique")
    valeur = float(data.get("valeur_reversement", 0))
    if mode == "clinique":
        pct = 100.0
    elif mode == "pourcentage":
        pct = valeur  # valeur = % clinique directement
    else:  # forfait — pct calculé dynamiquement à la vente
        pct = 0.0
    item = models.StockItemV2(
        nom=data.get("nom"), categorie=data.get("categorie"),
        quantite=int(data.get("quantite", 0)), seuil_min=int(data.get("seuil_min", 10)),
        prix_unitaire=float(data.get("prix_unitaire", 0)), unite=data.get("unite", "unité"),
        proprietaire=data.get("proprietaire", "Clinique"),
        mode_reversement=mode, valeur_reversement=valeur, pct_clinique=pct,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item

@router.post("/pharmacie/vente-v2", status_code=201, tags=["Pharmacie V2"])
async def vente_pharmacie_v2(data: dict, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    stock = db.query(models.StockItemV2).filter(models.StockItemV2.id == data.get("stock_id")).first()
    qte = int(data.get("quantite", 1))
    montant_total = float(data.get("montant_total", 0))
    
    if stock:
        if stock.quantite < qte:
            raise HTTPException(400, "Stock insuffisant")
        stock.quantite -= qte
        # Calculer reversement
        if stock.mode_reversement == "clinique":
            montant_clinique = montant_total
            montant_invest = 0
        elif stock.mode_reversement == "pourcentage":
            montant_clinique = round(montant_total * stock.pct_clinique / 100, 2)
            montant_invest = montant_total - montant_clinique
        else:  # forfait
            montant_invest = round(stock.valeur_reversement * qte, 2)
            montant_clinique = montant_total - montant_invest
        proprietaire = stock.proprietaire
        mode_rev = stock.mode_reversement
    else:
        montant_clinique = montant_total
        montant_invest = 0
        proprietaire = "Clinique"
        mode_rev = "clinique"

    vente = models.VentePharmacie(
        stock_id=data.get("stock_id"), produit_nom=data.get("produit_nom", ""),
        quantite=qte, prix_unitaire=float(data.get("prix_unitaire", 0)),
        montant_total=montant_total, montant_clinique=montant_clinique,
        montant_investisseur=montant_invest, proprietaire=proprietaire,
        mode_reversement=mode_rev, patient_nom=data.get("patient_nom", ""),
        mode_paiement=data.get("mode_paiement", "especes"),
        created_by=current_user.id,
    )
    db.add(vente)
    # Enregistrer part clinique comme recette
    mouvement = models.Mouvement(
        type=models.TypeMouvementEnum.recette, categorie="Pharmacie",
        description=f"Vente {data.get('produit_nom','')} — {data.get('patient_nom','')}",
        montant=montant_clinique, date_mouvement=func.now(),
        mode_paiement=data.get("mode_paiement", "especes"), created_by=current_user.id,
    )
    db.add(mouvement)
    db.commit()
    return {"vente": vente.id, "montant_clinique": montant_clinique, "montant_investisseur": montant_invest, "proprietaire": proprietaire}

# ══════════════════════════════════════════════════════════════════════════════
# OPTOMÉTRIE — Contrat et calcul mensuel
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/admin/contrat-optometrie", tags=["Optométrie"])
async def get_contrat_optomet(db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    return db.query(models.ContratOptometrie).first()

@router.put("/admin/contrat-optometrie", tags=["Optométrie"])
async def update_contrat_optomet(data: dict, db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    contrat = db.query(models.ContratOptometrie).first()
    if not contrat:
        contrat = models.ContratOptometrie()
        db.add(contrat)
    for k, v in data.items():
        setattr(contrat, k, v)
    contrat.updated_by = current_user.id
    db.commit()
    return contrat

@router.post("/admin/calculer-optometrie", tags=["Optométrie"])
async def calculer_optometrie(data: dict, db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    contrat = db.query(models.ContratOptometrie).first()
    if not contrat:
        raise HTTPException(404, "Contrat optométrie non configuré")
    mois = data.get("mois")
    annee = data.get("annee")
    total_consultations = float(data.get("total_consultations", 0))
    total_montures = float(data.get("total_montures", 0))
    
    part_consul = round(total_consultations * contrat.pct_consultation / 100, 2)
    part_montures = round(total_montures * contrat.pct_montures / 100, 2)
    total_part = part_consul + part_montures
    minimum_htg = round(contrat.minimum_mensuel_usd * contrat.taux_usd_htg, 2)
    montant_final = max(total_part, minimum_htg)
    difference = total_part - minimum_htg  # négatif = doit payer le minimum
    
    bilan = models.BilanOptometrieMensuel(
        mois=mois, annee=annee,
        total_consultations=total_consultations, total_montures=total_montures,
        part_clinique_consultations=part_consul, part_clinique_montures=part_montures,
        total_part_clinique=total_part, minimum_applicable_htg=minimum_htg,
        montant_final_clinique=montant_final, difference=difference,
    )
    db.add(bilan)
    db.commit()
    return {
        "mois": mois, "annee": annee,
        "total_consultations": total_consultations,
        "total_montures": total_montures,
        "part_clinique_consultations": part_consul,
        "part_clinique_montures": part_montures,
        "total_part_clinique": total_part,
        "minimum_mensuel_usd": contrat.minimum_mensuel_usd,
        "minimum_htg": minimum_htg,
        "montant_final_clinique": montant_final,
        "difference": difference,
        "verdict": "OK — % supérieur au minimum" if difference >= 0 else f"COMPLÉMENT REQUIS: {abs(difference):,.0f} HTG",
    }
