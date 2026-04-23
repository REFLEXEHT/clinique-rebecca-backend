from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    Float, ForeignKey, Enum, Time
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum


class RoleEnum(str, enum.Enum):
    admin = "admin"
    medecin = "medecin"
    staff = "staff"


class StatutRDVEnum(str, enum.Enum):
    en_attente = "en_attente"
    confirme = "confirme"
    annule = "annule"
    termine = "termine"


class TypeRDVEnum(str, enum.Enum):
    presentiel = "presentiel"
    video = "video"


class TypeMouvementEnum(str, enum.Enum):
    recette = "recette"
    depense = "depense"


# ─── Users ───────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    nom = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(Enum(RoleEnum), default=RoleEnum.staff)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ─── Services ────────────────────────────────────────────────────────────────
class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String(255), nullable=False)
    description = Column(Text)
    icone = Column(String(100), default="fa-stethoscope")
    couleur = Column(String(20), default="#1a4fc4")
    ordre = Column(Integer, default=0)
    actif = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ─── Specialistes ────────────────────────────────────────────────────────────
class Specialiste(Base):
    __tablename__ = "specialistes"

    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String(255), nullable=False)
    specialite = Column(String(255), nullable=False)
    description = Column(Text)
    emoji = Column(String(10), default="👨‍⚕️")
    categorie = Column(String(50), default="tous")
    email = Column(String(255))
    telephone = Column(String(50))
    actif = Column(Boolean, default=True)
    ordre = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    rendez_vous = relationship("RendezVous", back_populates="specialiste")


# ─── Horaires ────────────────────────────────────────────────────────────────
class Horaire(Base):
    __tablename__ = "horaires"

    id = Column(Integer, primary_key=True, index=True)
    jour = Column(String(20), nullable=False, unique=True)
    ouvert = Column(Boolean, default=True)
    heure_ouverture = Column(String(5), default="07:00")
    heure_fermeture = Column(String(5), default="17:00")
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ─── Patients ────────────────────────────────────────────────────────────────
class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True)
    nom = Column(String(255), nullable=False)
    prenom = Column(String(255))
    telephone = Column(String(50))
    email = Column(String(255))
    date_naissance = Column(DateTime)
    adresse = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    rendez_vous = relationship("RendezVous", back_populates="patient")


# ─── Rendez-vous ─────────────────────────────────────────────────────────────
class RendezVous(Base):
    __tablename__ = "rendez_vous"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True)
    specialiste_id = Column(Integer, ForeignKey("specialistes.id"), nullable=True)
    patient_nom = Column(String(255), nullable=False)
    patient_telephone = Column(String(50), nullable=False)
    patient_email = Column(String(255))
    specialite = Column(String(255), nullable=False)
    date_rdv = Column(DateTime(timezone=True), nullable=False)
    type_rdv = Column(Enum(TypeRDVEnum), default=TypeRDVEnum.presentiel)
    statut = Column(Enum(StatutRDVEnum), default=StatutRDVEnum.en_attente)
    motif = Column(Text)
    notes_admin = Column(Text)
    rappel_envoye = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    patient = relationship("Patient", back_populates="rendez_vous")
    specialiste = relationship("Specialiste", back_populates="rendez_vous")


# ─── Comptabilité ────────────────────────────────────────────────────────────
class Mouvement(Base):
    __tablename__ = "mouvements"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(Enum(TypeMouvementEnum), nullable=False)
    categorie = Column(String(100), nullable=False)
    description = Column(String(500), nullable=False)
    montant = Column(Float, nullable=False)
    date_mouvement = Column(DateTime(timezone=True), nullable=False)
    mode_paiement = Column(String(50), default="especes")
    reference = Column(String(100))
    notes = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# ─── Patient (fiche clinique) ─────────────────────────────────────────────────
class Patient(Base):
    __tablename__ = "patients"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True)  # #RB-001
    nom = Column(String(255), nullable=False)
    prenom = Column(String(255))
    date_naissance = Column(String(20))
    sexe = Column(String(10))
    telephone = Column(String(50))
    email = Column(String(255))
    adresse = Column(String(500))
    groupe_sanguin = Column(String(10))
    allergies = Column(Text)
    antecedents = Column(Text)
    notes = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
