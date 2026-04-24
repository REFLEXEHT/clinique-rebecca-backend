from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    Float, ForeignKey, Enum
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum


# ─── Enums ───────────────────────────────────────────────────────────────────
class RoleEnum(str, enum.Enum):
    admin     = "admin"
    medecin   = "medecin"
    patient   = "patient"
    caissier  = "caissier"
    labo      = "labo"
    pharmacie = "pharmacie"


class StatutRDVEnum(str, enum.Enum):
    en_attente = "en_attente"
    confirme   = "confirme"
    annule     = "annule"
    termine    = "termine"


class TypeRDVEnum(str, enum.Enum):
    presentiel = "presentiel"
    video      = "video"


class TypeMouvementEnum(str, enum.Enum):
    recette = "recette"
    depense = "depense"


class TypeMedecinEnum(str, enum.Enum):
    investisseur          = "investisseur"
    affilie               = "affilie"
    exploitant            = "exploitant"
    investisseur_exploitant = "investisseur_exploitant"


# ─── Users ───────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(255), unique=True, index=True, nullable=False)
    nom             = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role            = Column(Enum(RoleEnum), default=RoleEnum.patient)
    telephone       = Column(String(50))
    # Champs médecin
    specialite      = Column(String(255))
    type_medecin    = Column(Enum(TypeMedecinEnum), nullable=True)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


# ─── Services ────────────────────────────────────────────────────────────────
class Service(Base):
    __tablename__ = "services"

    id          = Column(Integer, primary_key=True, index=True)
    nom         = Column(String(255), nullable=False)
    description = Column(Text)
    icone       = Column(String(100), default="fa-stethoscope")
    couleur     = Column(String(20), default="#1a4fc4")
    ordre       = Column(Integer, default=0)
    actif       = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


# ─── Specialistes ────────────────────────────────────────────────────────────
class Specialiste(Base):
    __tablename__ = "specialistes"

    id          = Column(Integer, primary_key=True, index=True)
    nom         = Column(String(255), nullable=False)
    specialite  = Column(String(255), nullable=False)
    description = Column(Text)
    emoji       = Column(String(10), default="👨‍⚕️")
    categorie   = Column(String(50), default="tous")
    email       = Column(String(255))
    telephone   = Column(String(50))
    actif       = Column(Boolean, default=True)
    ordre       = Column(Integer, default=0)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    rendez_vous = relationship("RendezVous", back_populates="specialiste")


# ─── Horaires ────────────────────────────────────────────────────────────────
class Horaire(Base):
    __tablename__ = "horaires"

    id               = Column(Integer, primary_key=True, index=True)
    jour             = Column(String(20), nullable=False, unique=True)
    ouvert           = Column(Boolean, default=True)
    heure_ouverture  = Column(String(5), default="07:00")
    heure_fermeture  = Column(String(5), default="17:00")
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())


# ─── Patients ────────────────────────────────────────────────────────────────
class Patient(Base):
    __tablename__ = "patients"

    id              = Column(Integer, primary_key=True, index=True)
    numero          = Column(String(20), unique=True, index=True)   # #RB-001
    nom             = Column(String(255), nullable=False)
    prenom          = Column(String(255))
    date_naissance  = Column(String(20))
    sexe            = Column(String(10))
    telephone       = Column(String(50))
    email           = Column(String(255))
    adresse         = Column(String(500))
    groupe_sanguin  = Column(String(10))
    allergies       = Column(Text)
    antecedents     = Column(Text)
    notes           = Column(Text)
    created_by      = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    rendez_vous = relationship("RendezVous", back_populates="patient")


# ─── Rendez-vous ─────────────────────────────────────────────────────────────
class RendezVous(Base):
    __tablename__ = "rendez_vous"

    id                  = Column(Integer, primary_key=True, index=True)
    patient_id          = Column(Integer, ForeignKey("patients.id"), nullable=True)
    specialiste_id      = Column(Integer, ForeignKey("specialistes.id"), nullable=True)
    patient_nom         = Column(String(255), nullable=False)
    patient_telephone   = Column(String(50), nullable=False)
    patient_email       = Column(String(255))
    code_patient        = Column(String(20))
    specialite          = Column(String(255), nullable=False)
    date_rdv            = Column(DateTime(timezone=True), nullable=False)
    type_rdv            = Column(Enum(TypeRDVEnum), default=TypeRDVEnum.presentiel)
    statut              = Column(Enum(StatutRDVEnum), default=StatutRDVEnum.en_attente)
    motif               = Column(Text)
    notes_admin         = Column(Text)
    mode_paiement       = Column(String(50))
    reference_paiement  = Column(String(100))
    lien_video          = Column(String(500))
    numero_rdv          = Column(String(50))
    rappel_envoye       = Column(Boolean, default=False)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())

    patient     = relationship("Patient", back_populates="rendez_vous")
    specialiste = relationship("Specialiste", back_populates="rendez_vous")


# ─── Profil médecin (comptabilité) ───────────────────────────────────────────
class ProfilMedecin(Base):
    __tablename__ = "profils_medecins"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    nom          = Column(String(255), nullable=False)
    specialite   = Column(String(255))
    type_medecin = Column(Enum(TypeMedecinEnum), nullable=False)
    # Loyer mensuel (pour exploitants et investisseur_exploitant)
    loyer_mensuel_htg = Column(Float, default=0.0)
    actif        = Column(Boolean, default=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    actes        = relationship("ActeFacturable", back_populates="medecin")
    decaissements = relationship("Decaissement", back_populates="medecin")


# ─── Règles de répartition ───────────────────────────────────────────────────
class ReglePartage(Base):
    __tablename__ = "regles_partage"

    id           = Column(Integer, primary_key=True, index=True)
    type_medecin = Column(Enum(TypeMedecinEnum), nullable=False)
    type_acte    = Column(String(50), nullable=False)  # consultation, geste, chirurgie
    pct_medecin  = Column(Float, nullable=False)       # % pour le médecin
    pct_clinique = Column(Float, nullable=False)       # % pour la clinique
    updated_at   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ─── Actes facturables ───────────────────────────────────────────────────────
class ActeFacturable(Base):
    __tablename__ = "actes_facturables"

    id                    = Column(Integer, primary_key=True, index=True)
    medecin_id            = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom           = Column(String(255))
    patient_nom           = Column(String(255))
    type_acte             = Column(String(50))  # consultation, geste, chirurgie, hospit, observation
    specialite            = Column(String(255))
    description           = Column(String(500))
    montant_total         = Column(Float, nullable=False)
    montant_medecin       = Column(Float, default=0)
    montant_clinique      = Column(Float, default=0)
    pct_medecin           = Column(Float, default=0)
    mode_paiement         = Column(String(50), default="especes")
    statut_decaissement   = Column(String(20), default="en_attente")  # en_attente, decaisse
    date_acte             = Column(DateTime(timezone=True), server_default=func.now())
    created_by            = Column(Integer, ForeignKey("users.id"), nullable=True)

    medecin = relationship("ProfilMedecin", back_populates="actes")


# ─── Décaissements médecins ──────────────────────────────────────────────────
class Decaissement(Base):
    __tablename__ = "decaissements"

    id             = Column(Integer, primary_key=True, index=True)
    medecin_id     = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom    = Column(String(255))
    montant        = Column(Float, nullable=False)
    motif          = Column(String(500))
    mode_paiement  = Column(String(50), default="especes")
    date_decaissement = Column(DateTime(timezone=True), server_default=func.now())
    created_by     = Column(Integer, ForeignKey("users.id"), nullable=True)

    medecin = relationship("ProfilMedecin", back_populates="decaissements")


# ─── Bilan mensuel ───────────────────────────────────────────────────────────
class BilanMensuel(Base):
    __tablename__ = "bilans_mensuels"

    id                          = Column(Integer, primary_key=True, index=True)
    mois                        = Column(Integer, nullable=False)
    annee                       = Column(Integer, nullable=False)
    # Produits
    total_consultations         = Column(Float, default=0)
    total_gestes                = Column(Float, default=0)
    total_chirurgies            = Column(Float, default=0)
    total_hospitalisations      = Column(Float, default=0)
    total_laboratoire           = Column(Float, default=0)
    total_pharmacie             = Column(Float, default=0)
    total_loyers_recus          = Column(Float, default=0)
    total_autres_produits       = Column(Float, default=0)
    total_produits              = Column(Float, default=0)
    # Charges
    total_decaissements_medecins = Column(Float, default=0)
    total_salaires              = Column(Float, default=0)
    total_pharmacie_achats      = Column(Float, default=0)
    total_infrastructure        = Column(Float, default=0)
    total_autres_charges        = Column(Float, default=0)
    total_charges               = Column(Float, default=0)
    # Résultat
    resultat_net                = Column(Float, default=0)
    statut                      = Column(String(20), default="brouillon")  # brouillon, valide
    created_at                  = Column(DateTime(timezone=True), server_default=func.now())


# ─── Mouvements comptables ───────────────────────────────────────────────────
class Mouvement(Base):
    __tablename__ = "mouvements"

    id             = Column(Integer, primary_key=True, index=True)
    type           = Column(Enum(TypeMouvementEnum), nullable=False)
    categorie      = Column(String(100), nullable=False)
    description    = Column(String(500), nullable=False)
    montant        = Column(Float, nullable=False)
    date_mouvement = Column(DateTime(timezone=True), nullable=False)
    mode_paiement  = Column(String(50), default="especes")
    reference      = Column(String(100))
    notes          = Column(Text)
    created_by     = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())


# ─── Tarifs configurables ────────────────────────────────────────────────────
class TarifClinic(Base):
    __tablename__ = "tarifs_clinic"

    id      = Column(Integer, primary_key=True, index=True)
    code    = Column(String(50), unique=True, nullable=False)
    libelle = Column(String(255), nullable=False)
    montant = Column(Float, default=0)
    unite   = Column(String(20), default="HTG")  # HTG, pct, mois, jour


# ─── Paiements exploitants (flux direct) ─────────────────────────────────────
class PaiementExploitant(Base):
    __tablename__ = "paiements_exploitants"

    id            = Column(Integer, primary_key=True)
    medecin_id    = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom   = Column(String(255))
    patient_nom   = Column(String(255))
    montant       = Column(Float)
    mode_paiement = Column(String(50))
    flux_direct   = Column(Boolean, default=False)
    description   = Column(String(500))
    date_paiement = Column(DateTime(timezone=True), server_default=func.now())
    created_by    = Column(Integer, ForeignKey("users.id"), nullable=True)


# ─── Stock pharmacie ─────────────────────────────────────────────────────────
class StockItem(Base):
    __tablename__ = "stocks"

    id                 = Column(Integer, primary_key=True)
    nom                = Column(String(255), nullable=False)
    categorie          = Column(String(100))
    quantite           = Column(Integer, default=0)
    seuil_min          = Column(Integer, default=10)
    prix_unitaire      = Column(Float, default=0)
    unite              = Column(String(50), default="unité")
    proprietaire       = Column(String(255), default="Clinique")
    mode_reversement   = Column(String(20), default="clinique")   # clinique, pourcentage, forfait
    valeur_reversement = Column(Float, default=0)
    pct_clinique       = Column(Float, default=100)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())


# ─── Ventes pharmacie ────────────────────────────────────────────────────────
class VentePharmacie(Base):
    __tablename__ = "ventes_pharmacie"

    id                   = Column(Integer, primary_key=True)
    stock_id             = Column(Integer, ForeignKey("stocks.id"), nullable=True)
    produit_nom          = Column(String(255))
    quantite             = Column(Integer)
    prix_unitaire        = Column(Float)
    montant_total        = Column(Float)
    montant_clinique     = Column(Float)
    montant_investisseur = Column(Float, default=0)
    proprietaire         = Column(String(255), default="Clinique")
    mode_reversement     = Column(String(20), default="clinique")
    patient_nom          = Column(String(255))
    mode_paiement        = Column(String(50), default="especes")
    date_vente           = Column(DateTime(timezone=True), server_default=func.now())
    created_by           = Column(Integer, ForeignKey("users.id"), nullable=True)


# ─── Résultats laboratoire ───────────────────────────────────────────────────
class ResultatLabo(Base):
    __tablename__ = "resultats_labo"

    id          = Column(Integer, primary_key=True, index=True)
    patient_id  = Column(String(50))
    patient_nom = Column(String(255))
    type_examen = Column(String(255))
    resultats   = Column(Text)
    notes       = Column(Text)
    date_examen = Column(DateTime(timezone=True), server_default=func.now())
    technicien_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status      = Column(String(20), default="en_attente")  # en_attente, disponible, envoye


# ─── Contrat optométrie ──────────────────────────────────────────────────────
class ContratOptometrie(Base):
    __tablename__ = "contrat_optometrie"

    id                   = Column(Integer, primary_key=True)
    pct_consultation     = Column(Float, default=35.0)
    pct_montures         = Column(Float, default=13.0)
    minimum_mensuel_usd  = Column(Float, default=300.0)
    taux_usd_htg         = Column(Float, default=130.0)
    updated_at           = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by           = Column(Integer, ForeignKey("users.id"), nullable=True)


# ─── Bilan optométrie mensuel ────────────────────────────────────────────────
class BilanOptometrieMensuel(Base):
    __tablename__ = "bilans_optometrie"

    id                          = Column(Integer, primary_key=True)
    mois                        = Column(Integer)
    annee                       = Column(Integer)
    total_consultations         = Column(Float, default=0)
    total_montures              = Column(Float, default=0)
    part_clinique_consultations = Column(Float, default=0)
    part_clinique_montures      = Column(Float, default=0)
    total_part_clinique         = Column(Float, default=0)
    minimum_applicable_htg      = Column(Float, default=0)
    montant_final_clinique      = Column(Float, default=0)
    difference                  = Column(Float, default=0)
    statut                      = Column(String(20), default="en_attente")
    created_at                  = Column(DateTime(timezone=True), server_default=func.now())
