from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime
from enum import Enum


# ─── Enums ───────────────────────────────────────────────────────────────────
class RoleEnum(str, Enum):
    admin = "admin"
    medecin = "medecin"
    staff = "staff"


class StatutRDVEnum(str, Enum):
    en_attente = "en_attente"
    confirme = "confirme"
    annule = "annule"
    termine = "termine"


class TypeRDVEnum(str, Enum):
    presentiel = "presentiel"
    video = "video"


class TypeMouvementEnum(str, Enum):
    recette = "recette"
    depense = "depense"


# ─── Auth ────────────────────────────────────────────────────────────────────
class UserLogin(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str
    user: dict


class UserCreate(BaseModel):
    email: EmailStr
    nom: str
    password: str
    role: RoleEnum = RoleEnum.staff


class UserOut(BaseModel):
    id: int
    email: str
    nom: str
    role: RoleEnum
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Services ────────────────────────────────────────────────────────────────
class ServiceCreate(BaseModel):
    nom: str
    description: Optional[str] = None
    icone: str = "fa-stethoscope"
    couleur: str = "#1a4fc4"
    ordre: int = 0


class ServiceUpdate(BaseModel):
    nom: Optional[str] = None
    description: Optional[str] = None
    icone: Optional[str] = None
    couleur: Optional[str] = None
    ordre: Optional[int] = None
    actif: Optional[bool] = None


class ServiceOut(BaseModel):
    id: int
    nom: str
    description: Optional[str]
    icone: str
    couleur: str
    ordre: int
    actif: bool

    class Config:
        from_attributes = True


# ─── Specialistes ────────────────────────────────────────────────────────────
class SpecialisteCreate(BaseModel):
    nom: str
    specialite: str
    description: Optional[str] = None
    emoji: str = "👨‍⚕️"
    categorie: str = "tous"
    email: Optional[str] = None
    telephone: Optional[str] = None
    ordre: int = 0


class SpecialisteUpdate(BaseModel):
    nom: Optional[str] = None
    specialite: Optional[str] = None
    description: Optional[str] = None
    emoji: Optional[str] = None
    categorie: Optional[str] = None
    email: Optional[str] = None
    telephone: Optional[str] = None
    actif: Optional[bool] = None
    ordre: Optional[int] = None


class SpecialisteOut(BaseModel):
    id: int
    nom: str
    specialite: str
    description: Optional[str]
    emoji: str
    categorie: str
    email: Optional[str]
    telephone: Optional[str]
    actif: bool

    class Config:
        from_attributes = True


# ─── Horaires ────────────────────────────────────────────────────────────────
class HoraireUpdate(BaseModel):
    ouvert: bool
    heure_ouverture: str
    heure_fermeture: str


class HoraireOut(BaseModel):
    id: int
    jour: str
    ouvert: bool
    heure_ouverture: str
    heure_fermeture: str

    class Config:
        from_attributes = True


# ─── Patients ────────────────────────────────────────────────────────────────
class PatientCreate(BaseModel):
    nom: str
    prenom: Optional[str] = None
    telephone: Optional[str] = None
    email: Optional[str] = None
    adresse: Optional[str] = None


class PatientOut(BaseModel):
    id: int
    numero: str
    nom: str
    prenom: Optional[str]
    telephone: Optional[str]
    email: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Rendez-vous ─────────────────────────────────────────────────────────────
class RendezVousCreate(BaseModel):
    patient_nom: str
    patient_telephone: str
    patient_email: Optional[str] = None
    specialite: str
    date_rdv: datetime
    type_rdv: TypeRDVEnum = TypeRDVEnum.presentiel
    motif: Optional[str] = None


class RendezVousUpdate(BaseModel):
    statut: Optional[StatutRDVEnum] = None
    date_rdv: Optional[datetime] = None
    notes_admin: Optional[str] = None
    specialiste_id: Optional[int] = None


class RendezVousOut(BaseModel):
    id: int
    patient_nom: str
    patient_telephone: str
    patient_email: Optional[str]
    specialite: str
    date_rdv: datetime
    type_rdv: TypeRDVEnum
    statut: StatutRDVEnum
    motif: Optional[str]
    notes_admin: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Comptabilité ────────────────────────────────────────────────────────────
class MouvementCreate(BaseModel):
    type: TypeMouvementEnum
    categorie: str
    description: str
    montant: float
    date_mouvement: datetime
    mode_paiement: str = "especes"
    reference: Optional[str] = None
    notes: Optional[str] = None


class MouvementOut(BaseModel):
    id: int
    type: TypeMouvementEnum
    categorie: str
    description: str
    montant: float
    date_mouvement: datetime
    mode_paiement: str
    reference: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Stats ───────────────────────────────────────────────────────────────────
class DashboardStats(BaseModel):
    rdv_today: int
    rdv_month: int
    patients_month: int
    recettes_day: float
    recettes_month: float
    rdv_en_attente: int
    taux_presence: float


# ─── AI Chat ─────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    message: str
    historique: Optional[List[dict]] = []
