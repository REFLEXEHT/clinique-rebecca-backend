from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime
from enum import Enum


# ─── Enums ───────────────────────────────────────────────────────────────────
class RoleEnum(str, Enum):
    admin     = "admin"
    medecin   = "medecin"
    patient   = "patient"
    caissier  = "caissier"
    labo      = "labo"
    pharmacie = "pharmacie"


class StatutRDVEnum(str, Enum):
    en_attente = "en_attente"
    confirme   = "confirme"
    annule     = "annule"
    termine    = "termine"


class TypeRDVEnum(str, Enum):
    presentiel = "presentiel"
    video      = "video"


class TypeMouvementEnum(str, Enum):
    recette = "recette"
    depense = "depense"


class TypeMedecinEnum(str, Enum):
    investisseur            = "investisseur"
    affilie                 = "affilie"
    exploitant              = "exploitant"
    investisseur_exploitant = "investisseur_exploitant"


# ─── Auth ─────────────────────────────────────────────────────────────────────
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
    telephone: Optional[str] = None
    role: RoleEnum = RoleEnum.patient
    specialite: Optional[str] = None
    type_medecin: Optional[TypeMedecinEnum] = None


class UserOut(BaseModel):
    id: int
    email: str
    nom: str
    role: RoleEnum
    specialite: Optional[str]
    type_medecin: Optional[TypeMedecinEnum]
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
    date_naissance: Optional[str] = None
    sexe: Optional[str] = None
    groupe_sanguin: Optional[str] = None
    allergies: Optional[str] = None
    antecedents: Optional[str] = None
    notes: Optional[str] = None


class PatientOut(BaseModel):
    id: int
    numero: Optional[str]
    nom: str
    prenom: Optional[str]
    telephone: Optional[str]
    email: Optional[str]
    sexe: Optional[str]
    date_naissance: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Rendez-vous ─────────────────────────────────────────────────────────────
class RendezVousCreate(BaseModel):
    patient_nom: str
    patient_telephone: str
    patient_email: Optional[str] = None
    code_patient: Optional[str] = None
    specialite: str
    specialiste_id: Optional[int] = None   # ID du médecin choisi
    medecin_nom: Optional[str] = None      # Nom affiché du médecin choisi
    medecin_email: Optional[str] = None    # Email direct du médecin choisi
    date_rdv: datetime
    type_rdv: TypeRDVEnum = TypeRDVEnum.presentiel
    motif: Optional[str] = None
    mode_paiement: Optional[str] = None
    reference_paiement: Optional[str] = None
    lien_video: Optional[str] = None
    numero_rdv: Optional[str] = None


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
    code_patient: Optional[str] = None
    specialite: str
    medecin_nom: Optional[str] = None
    date_rdv: datetime
    type_rdv: TypeRDVEnum
    statut: StatutRDVEnum
    motif: Optional[str]
    notes_admin: Optional[str]
    mode_paiement: Optional[str]
    lien_video: Optional[str]
    numero_rdv: Optional[str]
    rappel_envoye: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Profil médecin ───────────────────────────────────────────────────────────
class ProfilMedecinOut(BaseModel):
    id: int
    nom: str
    specialite: Optional[str]
    type_medecin: TypeMedecinEnum
    loyer_mensuel_htg: float
    actif: bool

    class Config:
        from_attributes = True


# ─── Acte facturable ─────────────────────────────────────────────────────────
class ActeCreate(BaseModel):
    medecin_id: Optional[int] = None
    patient_nom: str
    type_acte: str
    specialite: Optional[str] = None
    description: Optional[str] = None
    montant_total: float
    mode_paiement: str = "especes"
    devise: Optional[str] = "HTG"
    taux_usd_htg: Optional[float] = None
    montant_medecin_manuel: Optional[float] = None
    montant_clinique_manuel: Optional[float] = None


class ActeOut(BaseModel):
    id: int
    medecin_nom: Optional[str]
    patient_nom: str
    type_acte: str
    montant_total: float
    montant_medecin: float
    montant_clinique: float
    pct_medecin: float
    statut_decaissement: str
    date_acte: datetime

    class Config:
        from_attributes = True


# ─── Décaissement ─────────────────────────────────────────────────────────────
class DecaissementCreate(BaseModel):
    medecin_id: int
    medecin_nom: Optional[str] = None
    montant: float
    motif: str
    mode_paiement: str = "especes"
    devise: Optional[str] = "HTG"
    taux_usd_htg: Optional[float] = None


class DecaissementOut(BaseModel):
    id: int
    medecin_nom: Optional[str]
    montant: float
    motif: Optional[str]
    mode_paiement: str
    date_decaissement: datetime

    class Config:
        from_attributes = True


# ─── Mouvements comptables ───────────────────────────────────────────────────
class MouvementCreate(BaseModel):
    type: str   # "recette" ou "depense"
    categorie: str
    description: str
    montant: float
    date_mouvement: Optional[datetime] = None
    mode_paiement: str = "especes"
    devise: Optional[str] = "HTG"
    montant_usd: Optional[float] = None
    taux_usd_htg: Optional[float] = None
    libelle_debit: Optional[str] = None
    libelle_credit: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


class MouvementOut(BaseModel):
    id: int
    numero_piece: Optional[str]
    journal: Optional[str]
    type: Optional[str]
    categorie: str
    description: str
    montant: float
    compte_debit: Optional[str]
    compte_credit: Optional[str]
    mode_paiement: str
    devise: Optional[str]
    taux_usd_htg: Optional[float]
    est_contrepassation: bool = False
    notes: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Règle partage ────────────────────────────────────────────────────────────
class ReglePartageOut(BaseModel):
    id: int
    type_medecin: TypeMedecinEnum
    type_acte: str
    pct_medecin: float
    pct_clinique: float

    class Config:
        from_attributes = True


# ─── Tarif clinic ─────────────────────────────────────────────────────────────
class TarifOut(BaseModel):
    id: int
    code: str
    libelle: str
    montant: float
    unite: str

    class Config:
        from_attributes = True


# ─── Stock ────────────────────────────────────────────────────────────────────
class StockOut(BaseModel):
    id: int
    nom: str
    categorie: Optional[str]
    quantite: int
    seuil_min: int
    prix_unitaire: float
    unite: str
    proprietaire: str
    mode_reversement: str
    pct_clinique: float

    class Config:
        from_attributes = True


# ─── Résultat labo ────────────────────────────────────────────────────────────
class ResultatLaboOut(BaseModel):
    id: int
    patient_id: Optional[str]
    patient_nom: str
    type_examen: str
    resultats: Optional[str]
    notes: Optional[str]
    date_examen: datetime
    status: str

    class Config:
        from_attributes = True


# ─── Stats ────────────────────────────────────────────────────────────────────
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


# ─── Résultat labo (création) ─────────────────────────────────────────────────
class ResultatLaboCreate(BaseModel):
    patient_id: str
    patient_nom: str
    patient_telephone: Optional[str] = None
    patient_email: Optional[str] = None
    type_examen: str
    resultats: str
    valeurs_normales: Optional[str] = None
    interpretation: Optional[str] = None
    notes: Optional[str] = None
    date_examen: Optional[datetime] = None

