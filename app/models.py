"""
models.py — Clinique de la Rebecca
Conformité : PCN Haïti (Plan Comptable National) + IFRS for SMEs
Corrections appliquées :
  1. Numéros de comptes PCN sur chaque mouvement
  2. Décaissements médecins via compte de tiers 468 (partie double)
  3. Multi-devises HTG/USD avec taux de change obligatoire
  4. Numérotation séquentielle des pièces comptables
  5. Immobilisations + amortissements (classe 2 PCN)
  6. Verrouillage des périodes clôturées
  7. Audit trail complet (created_by, modified_by, modified_at)
  8. Contrepassation (pas de suppression comptable)
  9. Lettrage RDV ↔ paiement
 10. TCA/TVA Haïti (exonération médicale tracée)
"""
from sqlalchemy import (
    Date,
    Column, Integer, String, Text, Boolean, DateTime,
    Float, ForeignKey, Enum, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum


# ══════════════════════════════════════════════════════════════════════════════
# ENUMS
# ══════════════════════════════════════════════════════════════════════════════

class RoleEnum(str, enum.Enum):
    admin      = "admin"
    medecin    = "medecin"
    patient    = "patient"
    caissier   = "caissier"
    labo       = "labo"
    pharmacie  = "pharmacie"
    infirmier  = "infirmier"
    dentiste   = "dentiste"
    physio     = "physio"
    optometrie = "optometrie"


class StatutRDVEnum(str, enum.Enum):
    en_attente           = "en_attente"
    paiement_requis      = "paiement_requis"
    paiement_effectue    = "paiement_effectue"
    confirme             = "confirme"
    propose_autre_moment = "propose_autre_moment"
    annule               = "annule"
    termine              = "termine"


class TypeRDVEnum(str, enum.Enum):
    presentiel = "presentiel"
    video      = "video"


class TypeMouvementEnum(str, enum.Enum):
    recette      = "recette"       # Classe 7 PCN
    depense      = "depense"       # Classe 6 PCN
    contrepassation = "contrepassation"  # Écriture inverse (jamais supprimer)


class DeviseEnum(str, enum.Enum):
    HTG = "HTG"
    USD = "USD"


class TypeMedecinEnum(str, enum.Enum):
    investisseur            = "investisseur"
    affilie                 = "affilie"
    exploitant              = "exploitant"
    investisseur_exploitant = "investisseur_exploitant"


class JournalEnum(str, enum.Enum):
    VTE  = "VTE"   # Ventes / recettes
    ACH  = "ACH"   # Achats / dépenses
    BQ   = "BQ"    # Banque
    CAISSE = "CAISSE"  # Caisse
    OD   = "OD"    # Opérations diverses (salaires, amortissements)
    DECAIS = "DECAIS"  # Décaissements médecins


class StatutPeriodeEnum(str, enum.Enum):
    ouverte  = "ouverte"
    cloturee = "cloturee"   # Verrouillée — aucune écriture possible


# ══════════════════════════════════════════════════════════════════════════════
# PLAN DE COMPTES PCN HAÏTI — Référentiel
# ══════════════════════════════════════════════════════════════════════════════

# Mapping catégorie → numéro de compte PCN
COMPTE_PCN: dict[str, str] = {
    # Classe 5 — Trésorerie
    "especes":        "511",   # Caisse HTG
    "especes_usd":    "512",   # Caisse USD
    "banque":         "521",   # Banque HTG
    "banque_usd":     "522",   # Banque USD
    "moncash":        "531",   # Mobile Money MonCash
    "natcash":        "532",   # NatCash
    "carte":          "521",   # Carte → banque
    # Classe 7 — Produits
    "Consultations":  "701",
    "Gestes médicaux":"702",
    "Chirurgies":     "703",
    "Hospitalisations":"704",
    "Laboratoire":    "705",
    "Pharmacie":      "706",
    "Dentisterie":    "707",
    "Physiothérapie": "708",
    "Optométrie":     "709",
    "Loyer exploitant":"711",
    "Autres produits":"719",
    # Classe 6 — Charges
    "RH / Salaires":          "641",
    "Charges sociales OFATMA":"645",
    "Honoraires médecins":    "651",   # ← décaissements médecins
    "Achats médicaments":     "601",
    "Pharmacie achats":       "607",
    "Consommables médicaux":  "602",
    "Infrastructure":         "615",
    "Équipements":            "218",   # Immobilisation si > seuil
    "Télécom":                "626",
    "Amortissements":         "681",
    "Autres charges":         "628",
    # Classe 4 — Tiers
    "compte_medecin":         "468",   # Compte courant médecin
    "compte_patient":         "411",   # Créances patients
    "fournisseurs":           "401",
    "dgi_tca":                "441",   # DGI — TCA à reverser
}


def get_compte_tresorerie(mode_paiement: str, devise: str = "HTG") -> str:
    """Retourne le numéro de compte de trésorerie selon le mode et la devise."""
    mode = (mode_paiement or "").lower()
    if "moncash" in mode:                    return "531"
    if "natcash" in mode:                    return "532"
    if "zelle" in mode:                      return "522"  # Compte USD (Zelle = transfert USD)
    if "carte" in mode:                      return "521"
    if "virement" in mode or "banque" in mode:
        return "522" if devise == "USD" else "521"
    if "usd" in mode:                        return "512"
    return "512" if devise == "USD" else "511"


# ══════════════════════════════════════════════════════════════════════════════
# UTILISATEURS
# ══════════════════════════════════════════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(255), unique=True, index=True, nullable=False)
    nom             = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role            = Column(String(50), default="patient")  # VARCHAR — supporte tous les rôles
    telephone       = Column(String(50))
    specialite      = Column(String(255))
    type_medecin    = Column(Enum(TypeMedecinEnum, native_enum=False), nullable=True)
    is_active       = Column(Boolean, default=True)
    signature_image = Column(Text, nullable=True)  # base64 PNG — médecin uniquement
    photo_profil    = Column(Text, nullable=True)  # base64 JPEG — photo de profil
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)


# ══════════════════════════════════════════════════════════════════════════════
# SERVICES / SPÉCIALISTES / HORAIRES
# ══════════════════════════════════════════════════════════════════════════════

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


class Specialiste(Base):
    __tablename__ = "specialistes"
    id          = Column(Integer, primary_key=True, index=True)
    nom         = Column(String(255), nullable=False)
    titre       = Column(String(20), default="Dr")   # Dr, Mr, Mme, etc.
    specialite  = Column(String(255), nullable=False)
    photo_profil = Column(Text, nullable=True)        # base64 JPEG
    description = Column(Text)
    emoji       = Column(String(10), default="👨‍⚕️")
    categorie   = Column(String(50), default="tous")
    email       = Column(String(255))
    telephone   = Column(String(50))
    actif       = Column(Boolean, default=True)
    ordre       = Column(Integer, default=0)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    rendez_vous = relationship("RendezVous", back_populates="specialiste")


class Horaire(Base):
    __tablename__ = "horaires"
    id              = Column(Integer, primary_key=True, index=True)
    jour            = Column(String(20), nullable=False, unique=True)
    ouvert          = Column(Boolean, default=True)
    heure_ouverture = Column(String(5), default="07:00")
    heure_fermeture = Column(String(5), default="17:00")
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())


# ══════════════════════════════════════════════════════════════════════════════
# PATIENTS ET RENDEZ-VOUS
# ══════════════════════════════════════════════════════════════════════════════

class Patient(Base):
    __tablename__ = "patients"
    id             = Column(Integer, primary_key=True, index=True)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=True, unique=True, index=True)
    numero         = Column(String(20), unique=True, index=True)
    nom            = Column(String(255), nullable=False)
    prenom         = Column(String(255))
    sexe           = Column(String(10))
    telephone      = Column(String(50))
    email          = Column(String(255))
    adresse        = Column(String(500))
    groupe_sanguin = Column(String(10))
    allergies      = Column(Text)
    antecedents    = Column(Text)
    notes          = Column(Text)
    age            = Column(Integer, nullable=True)
    date_naissance = Column(Date, nullable=True)
    contact_urgence = Column(String(255), nullable=True)
    is_premiere_visite = Column(Boolean, default=True)
    created_by     = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    # Deux identifiants distincts
    id_papier      = Column(String(50), nullable=True, index=True)   # Ex: 0001, CR127 — dossier papier CONSERVÉ
    service              = Column(String(50), default="clinique")           # clinique, dentiste, physio, optometrie
    date_premiere_visite = Column(DateTime(timezone=True), nullable=True)
    rendez_vous    = relationship("RendezVous", back_populates="patient")


class RendezVous(Base):
    __tablename__ = "rendez_vous"
    id                 = Column(Integer, primary_key=True, index=True)
    patient_id         = Column(Integer, ForeignKey("patients.id"), nullable=True)
    specialiste_id     = Column(Integer, ForeignKey("specialistes.id"), nullable=True)
    patient_nom        = Column(String(255), nullable=False)
    patient_telephone  = Column(String(50), nullable=False)
    patient_email      = Column(String(255))
    code_patient       = Column(String(20))
    specialite         = Column(String(255), nullable=False)
    medecin_nom        = Column(String(255))
    medecin_email      = Column(String(255))
    date_rdv           = Column(DateTime(timezone=True), nullable=False)
    type_rdv           = Column(Enum(TypeRDVEnum, native_enum=False), default=TypeRDVEnum.presentiel)
    statut               = Column(Enum(StatutRDVEnum, native_enum=False), default=StatutRDVEnum.en_attente)
    confirme_par         = Column(Integer, nullable=True)
    confirme_par_role    = Column(String(20), nullable=True)
    autre_moment_propose = Column(String(50), nullable=True)
    autre_moment_message = Column(String(500), nullable=True)
    motif              = Column(Text)
    notes_admin        = Column(Text)
    mode_paiement      = Column(String(50))
    devise             = Column(Enum(DeviseEnum, native_enum=False), default=DeviseEnum.HTG)
    reference_paiement = Column(String(100))
    # Lettrage comptable : lien vers le mouvement de paiement
    mouvement_id       = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    lien_video         = Column(String(500))
    numero_rdv         = Column(String(50))
    rappel_envoye      = Column(Boolean, default=False)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())
    patient            = relationship("Patient", back_populates="rendez_vous")
    specialiste        = relationship("Specialiste", back_populates="rendez_vous")


# ══════════════════════════════════════════════════════════════════════════════
# COMPTABILITÉ — MOUVEMENTS (JOURNAL PCN)
# ══════════════════════════════════════════════════════════════════════════════

class Mouvement(Base):
    """
    Journal comptable principal — PCN Haïti.
    Chaque ligne = une écriture avec compte débit ET crédit.
    Principe de la partie double garanti par validation backend.
    JAMAIS supprimé — contrepassation si erreur.
    """
    __tablename__ = "mouvements"

    id              = Column(Integer, primary_key=True, index=True)
    # Numérotation séquentielle PCN : VTE-2025-0001, ACH-2025-0001
    numero_piece    = Column(String(30), unique=True, index=True)
    journal         = Column(Enum(JournalEnum, native_enum=False), nullable=False, default=JournalEnum.VTE)

    # Comptes PCN (partie double)
    compte_debit    = Column(String(10), nullable=False)   # ex: "511"
    compte_credit   = Column(String(10), nullable=False)   # ex: "701"
    libelle_debit   = Column(String(100))                  # ex: "Caisse HTG"
    libelle_credit  = Column(String(100))                  # ex: "Produits consultations"

    # Classification
    type            = Column(Enum(TypeMouvementEnum, native_enum=False), nullable=False)
    categorie       = Column(String(100), nullable=False)
    description     = Column(String(500), nullable=False)

    # Montants — HTG principal, USD optionnel
    montant         = Column(Float, nullable=False)         # Toujours > 0
    devise          = Column(Enum(DeviseEnum, native_enum=False), default=DeviseEnum.HTG)
    montant_usd     = Column(Float, nullable=True)          # Si paiement en USD
    taux_usd_htg    = Column(Float, nullable=True)          # Taux du jour si USD
    montant_htg     = Column(Float, nullable=True)          # montant_usd × taux

    # Paiement
    mode_paiement   = Column(String(50), default="especes")
    reference       = Column(String(100))                   # Ref MonCash, NatCash...

    # Lettrage et période
    rdv_id          = Column(Integer, ForeignKey("rendez_vous.id"), nullable=True)
    periode_mois    = Column(Integer)                       # Mois de rattachement
    periode_annee   = Column(Integer)                       # Année de rattachement
    periode_verrou  = Column(Boolean, default=False)        # True = période clôturée

    # Contrepassation
    est_contrepassation = Column(Boolean, default=False)
    mouvement_origine_id = Column(Integer, ForeignKey("mouvements.id"), nullable=True)

    # TCA Haïti
    tca_applicable  = Column(Boolean, default=False)        # Faux pour soins médicaux
    tca_montant     = Column(Float, default=0.0)            # Montant TCA si applicable
    tca_compte      = Column(String(10), default="441")     # 441 = DGI TCA

    notes           = Column(Text)

    # Tiers comptable — payeur (recette) ou bénéficiaire/fournisseur (dépense)
    tiers_nom       = Column(String(255), nullable=True)   # Nom du fournisseur, client, médecin...
    tiers_type      = Column(String(50),  nullable=True)   # fournisseur | medecin | patient | autre

    # Date effective du mouvement (peut différer de created_at)
    date_mouvement  = Column(DateTime(timezone=True), nullable=True)

    # Audit trail complet
    created_by      = Column(Integer, ForeignKey("users.id"), nullable=True)
    modified_by     = Column(Integer, ForeignKey("users.id"), nullable=True)
    modified_at     = Column(DateTime(timezone=True), onupdate=func.now())
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)


# ══════════════════════════════════════════════════════════════════════════════
# COMPTABILITÉ — PROFILS MÉDECINS ET RÉPARTITION
# ══════════════════════════════════════════════════════════════════════════════

class ProfilMedecin(Base):
    __tablename__ = "profils_medecins"
    id                = Column(Integer, primary_key=True, index=True)
    user_id           = Column(Integer, ForeignKey("users.id"), nullable=True)
    nom               = Column(String(255), nullable=False)
    specialite        = Column(String(255))
    type_medecin      = Column(Enum(TypeMedecinEnum, native_enum=False), nullable=False)
    loyer_mensuel_htg = Column(Float, default=0.0)
    # Solde compte courant 468 (créances médecin envers la clinique)
    solde_compte_468  = Column(Float, default=0.0)
    actif             = Column(Boolean, default=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    actes             = relationship("ActeFacturable", back_populates="medecin")
    decaissements     = relationship("Decaissement", back_populates="medecin")


class ReglePartage(Base):
    __tablename__ = "regles_partage"
    id           = Column(Integer, primary_key=True, index=True)
    type_medecin = Column(Enum(TypeMedecinEnum, native_enum=False), nullable=False)
    type_acte    = Column(String(50), nullable=False)
    pct_medecin  = Column(Float, nullable=False)
    pct_clinique = Column(Float, nullable=False)
    updated_at   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ActeFacturable(Base):
    """
    Acte médical facturé.
    PCN : génère 2 écritures :
      (1) 511/521 → 701..709  (recette clinique = montant_clinique)
      (2) 651 → 468_medecin   (charge honoraires = montant_medecin)
    IFRS 15 : produit reconnu à la réalisation de l'acte.
    """
    __tablename__ = "actes_facturables"
    id                  = Column(Integer, primary_key=True, index=True)
    medecin_id          = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom         = Column(String(255))
    patient_nom         = Column(String(255))
    type_acte           = Column(String(50))
    specialite          = Column(String(255))
    description         = Column(String(500))
    # Montants
    montant_total       = Column(Float, nullable=False)
    montant_medecin     = Column(Float, default=0)
    montant_clinique    = Column(Float, default=0)
    pct_medecin         = Column(Float, default=0)
    devise              = Column(Enum(DeviseEnum, native_enum=False), default=DeviseEnum.HTG)
    taux_usd_htg        = Column(Float, nullable=True)
    # Contrôle : montant_medecin + montant_clinique doit = montant_total
    balance_ok          = Column(Boolean, default=True)
    mode_paiement       = Column(String(50), default="especes")
    statut_decaissement = Column(String(20), default="en_attente")
    # Liens comptables
    mouvement_recette_id  = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    mouvement_honoraires_id = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    date_acte           = Column(DateTime(timezone=True), server_default=func.now())
    created_by          = Column(Integer, ForeignKey("users.id"), nullable=True)
    medecin             = relationship("ProfilMedecin", back_populates="actes")


class Decaissement(Base):
    """
    Décaissement médecin — PCN CORRECT :
      Étape 1 (lors de l'acte) : 651 Honoraires / 468 C/C médecin
      Étape 2 (cash) :           468 C/C médecin / 511 Caisse
    Les deux mouvements sont liés par medecin_id + date.
    """
    __tablename__ = "decaissements"
    id                = Column(Integer, primary_key=True, index=True)
    medecin_id        = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom       = Column(String(255))
    montant           = Column(Float, nullable=False)
    motif             = Column(String(500))
    mode_paiement     = Column(String(50), default="especes")
    devise            = Column(Enum(DeviseEnum, native_enum=False), default=DeviseEnum.HTG)
    taux_usd_htg      = Column(Float, nullable=True)
    # Liens PCN : 2 mouvements générés
    mouvement_468_id  = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    mouvement_511_id  = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    date_decaissement = Column(DateTime(timezone=True), server_default=func.now())
    tiers_nom         = Column(String(255), nullable=True)  # Nom du bénéficiaire si différent du médecin
    date_prevue       = Column(DateTime(timezone=True), nullable=True)   # Si paiement planifié futur
    statut            = Column(String(20), default="effectue")            # effectue | planifie
    created_by        = Column(Integer, ForeignKey("users.id"), nullable=True)
    medecin           = relationship("ProfilMedecin", back_populates="decaissements")


# ══════════════════════════════════════════════════════════════════════════════
# BILAN MENSUEL + VERROUILLAGE PÉRIODE
# ══════════════════════════════════════════════════════════════════════════════

class PeriodeComptable(Base):
    """
    Gestion des périodes comptables — verrouillage après clôture.
    Une période clôturée interdit toute écriture sur ses mouvements.
    """
    __tablename__ = "periodes_comptables"
    __table_args__ = (UniqueConstraint("mois", "annee", name="uq_periode"),)

    id      = Column(Integer, primary_key=True)
    mois    = Column(Integer, nullable=False)
    annee   = Column(Integer, nullable=False)
    statut  = Column(Enum(StatutPeriodeEnum, native_enum=False), default=StatutPeriodeEnum.ouverte)
    cloture_par  = Column(Integer, ForeignKey("users.id"), nullable=True)
    cloture_at   = Column(DateTime(timezone=True), nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())


class BilanMensuel(Base):
    __tablename__ = "bilans_mensuels"
    id                           = Column(Integer, primary_key=True, index=True)
    mois                         = Column(Integer, nullable=False)
    annee                        = Column(Integer, nullable=False)
    # Produits (Classe 7)
    total_consultations          = Column(Float, default=0)   # 701
    total_gestes                 = Column(Float, default=0)   # 702
    total_chirurgies             = Column(Float, default=0)   # 703
    total_hospitalisations       = Column(Float, default=0)   # 704
    total_laboratoire            = Column(Float, default=0)   # 705
    total_pharmacie              = Column(Float, default=0)   # 706
    total_loyers_recus           = Column(Float, default=0)   # 711
    total_autres_produits        = Column(Float, default=0)   # 719
    total_produits               = Column(Float, default=0)
    # Charges (Classe 6)
    total_honoraires_medecins    = Column(Float, default=0)   # 651 (renommé)
    total_salaires               = Column(Float, default=0)   # 641
    total_charges_sociales       = Column(Float, default=0)   # 645
    total_pharmacie_achats       = Column(Float, default=0)   # 607
    total_amortissements         = Column(Float, default=0)   # 681
    total_infrastructure         = Column(Float, default=0)   # 615
    total_autres_charges         = Column(Float, default=0)   # 628
    total_charges                = Column(Float, default=0)
    # Résultat
    resultat_net                 = Column(Float, default=0)
    # TCA Haïti
    total_tca_collectee          = Column(Float, default=0)   # 441
    # Devises
    total_produits_usd           = Column(Float, default=0)   # Produits en USD (converti)
    taux_usd_moyen               = Column(Float, default=0)   # Taux moyen du mois
    statut                       = Column(String(20), default="brouillon")
    created_at                   = Column(DateTime(timezone=True), server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
# IMMOBILISATIONS (Classe 2 PCN — ABSENT AVANT)
# ══════════════════════════════════════════════════════════════════════════════

class Immobilisation(Base):
    """
    PCN Haïti Classe 2 — Immobilisations corporelles.
    IAS 16 : coût historique ou réévaluation.
    Amortissement linéaire obligatoire (PCN) ou par composants (IAS 16).
    """
    __tablename__ = "immobilisations"

    id               = Column(Integer, primary_key=True)
    libelle          = Column(String(255), nullable=False)  # "Échographe Samsung"
    compte_pcn       = Column(String(10), default="218")    # 218 = équipements médicaux
    # Valeurs
    valeur_acquisition  = Column(Float, nullable=False)
    devise_acquisition  = Column(Enum(DeviseEnum, native_enum=False), default=DeviseEnum.HTG)
    taux_usd_achat      = Column(Float, nullable=True)
    valeur_htg          = Column(Float, nullable=False)
    # Amortissement
    date_acquisition    = Column(DateTime(timezone=True), nullable=False)
    duree_amort_ans     = Column(Integer, default=5)        # Durée en années
    taux_amort          = Column(Float, default=20.0)       # % annuel
    amort_cumule        = Column(Float, default=0.0)
    valeur_nette        = Column(Float, nullable=False)     # Valeur nette comptable
    # Statut
    actif               = Column(Boolean, default=True)
    date_sortie         = Column(DateTime(timezone=True), nullable=True)
    motif_sortie        = Column(String(255), nullable=True)
    # Audit
    created_by          = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
# TARIFS / STOCKS / PHARMACIE / LABO / OPTOMÉTRIE
# ══════════════════════════════════════════════════════════════════════════════

class TarifClinic(Base):
    __tablename__ = "tarifs_clinic"
    id      = Column(Integer, primary_key=True, index=True)
    code    = Column(String(50), unique=True, nullable=False)
    libelle = Column(String(255), nullable=False)
    montant = Column(Float, default=0)
    unite   = Column(String(20), default="HTG")


class PaiementExploitant(Base):
    __tablename__ = "paiements_exploitants"
    id            = Column(Integer, primary_key=True)
    medecin_id    = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom   = Column(String(255))
    patient_nom   = Column(String(255))
    montant       = Column(Float)
    devise        = Column(Enum(DeviseEnum, native_enum=False), default=DeviseEnum.HTG)
    taux_usd_htg  = Column(Float, nullable=True)
    mode_paiement = Column(String(50))
    flux_direct   = Column(Boolean, default=False)
    description   = Column(String(500))
    mouvement_id  = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    date_paiement = Column(DateTime(timezone=True), server_default=func.now())
    created_by    = Column(Integer, ForeignKey("users.id"), nullable=True)


class StockItem(Base):
    __tablename__ = "stocks"
    id                 = Column(Integer, primary_key=True)
    nom                = Column(String(255), nullable=False)
    categorie          = Column(String(100))
    quantite           = Column(Integer, default=0)
    seuil_min          = Column(Integer, default=10)
    prix_unitaire      = Column(Float, default=0)
    devise             = Column(Enum(DeviseEnum, native_enum=False), default=DeviseEnum.HTG)
    unite              = Column(String(50), default="unité")
    proprietaire       = Column(String(255), default="Clinique")
    mode_reversement   = Column(String(20), default="clinique")
    valeur_reversement = Column(Float, default=0)
    pct_clinique       = Column(Float, default=100)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())


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
    # TCA pharmacie (non exonérée)
    tca_applicable       = Column(Boolean, default=False)
    tca_montant          = Column(Float, default=0.0)
    date_vente           = Column(DateTime(timezone=True), server_default=func.now())
    created_by           = Column(Integer, ForeignKey("users.id"), nullable=True)


class ResultatLabo(Base):
    __tablename__ = "resultats_labo"
    id            = Column(Integer, primary_key=True, index=True)
    patient_id    = Column(String(50))
    patient_nom   = Column(String(255))
    type_examen   = Column(String(255))
    resultats     = Column(Text)
    notes         = Column(Text)
    date_examen   = Column(DateTime(timezone=True), server_default=func.now())
    technicien_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status        = Column(String(20), default="en_attente")


class ContratOptometrie(Base):
    __tablename__ = "contrat_optometrie"
    id                  = Column(Integer, primary_key=True)
    pct_consultation    = Column(Float, default=35.0)
    pct_montures        = Column(Float, default=13.0)
    minimum_mensuel_usd = Column(Float, default=300.0)
    taux_usd_htg        = Column(Float, default=130.0)
    updated_at          = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by          = Column(Integer, ForeignKey("users.id"), nullable=True)


# ─── Tarifs Laboratoire ──────────────────────────────────────────────────────
class TarifLabo(Base):
    __tablename__ = "tarifs_labo"
    id          = Column(Integer, primary_key=True)
    code        = Column(String(50), unique=True, nullable=False)
    libelle     = Column(String(500), nullable=False)
    montant     = Column(Float, default=0)       # HTG (legacy)
    montant_usd = Column(Float, nullable=True)   # USD (référence)
    devise      = Column(String(10), default="USD")
    actif       = Column(Boolean, default=True)


# ─── Tarifs Dentisterie ──────────────────────────────────────────────────────
class TarifDentiste(Base):
    __tablename__ = "tarifs_dentiste"
    id          = Column(Integer, primary_key=True)
    code        = Column(String(50), unique=True, nullable=False)
    libelle     = Column(String(500), nullable=False)
    montant     = Column(Float, default=0)       # HTG (legacy)
    montant_usd = Column(Float, nullable=True)   # USD (référence)
    categorie   = Column(String(255), nullable=True)
    devise      = Column(String(10), default="USD")
    actif       = Column(Boolean, default=True)


# ─── Tarifs Médecin (prix par médecin) ───────────────────────────────────────
class TarifMedecin(Base):
    __tablename__ = "tarifs_medecins"
    id                      = Column(Integer, primary_key=True)
    specialiste_id          = Column(Integer, ForeignKey("specialistes.id"), nullable=True)
    medecin_nom             = Column(String(255))
    specialite              = Column(String(255))
    prix_consultation       = Column(Float, default=0)
    prix_rdv                = Column(Float, default=0)
    prix_hospitalisation_jr = Column(Float, default=0)
    prix_geste_base         = Column(Float, default=0)
    type_medecin            = Column(Enum(TypeMedecinEnum, native_enum=False), nullable=True)
    actif                   = Column(Boolean, default=True)



# ─── Gestes Médicaux par Spécialité ─────────────────────────────────────────
class GesteMedical(Base):
    """
    Catalogue des gestes médicaux par spécialité — prix de référence en USD.
    Sources: AHC (Chirurgie), SHOG (Gynécologie), SHP (Pédiatrie), SHA (Anesthésie).
    Le prix affiché est en USD. Le caissier entre le taux du jour → calcul HTG automatique.
    Le médecin/admin peut saisir un prix différent du barème si nécessaire.
    Ces données ne sont pas publiques.
    """
    __tablename__ = "gestes_medicaux"
    
    id              = Column(Integer, primary_key=True)
    specialite      = Column(String(255), nullable=False, index=True)
    categorie       = Column(String(255), nullable=True)   # Ex: "Foie", "Abdomen", "Urgences"
    libelle         = Column(String(500), nullable=False)
    # Prix en USD — référence principale
    prix_usd        = Column(Float, default=0)
    prix_usd_min    = Column(Float, nullable=True)         # Fourchette basse si applicable
    prix_usd_max    = Column(Float, nullable=True)         # Fourchette haute
    # Prix clinique (peut différer du barème)
    prix_clinique_usd = Column(Float, nullable=True)       # Prix appliqué par la clinique
    # Source du barème
    source_bareme   = Column(String(50), nullable=True)    # "AHC", "SHOG", "SHP", "SHA", "CLINIQUE"
    # Prix HTG de référence (du barème original)
    prix_htg_ref    = Column(Float, nullable=True)
    # Si True = prix fixe non négociable, si False = prix libre saisi à l'acte
    prix_fixe       = Column(Boolean, default=False)
    actif           = Column(Boolean, default=True)
    ordre           = Column(Integer, default=0)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())


class TauxChange(Base):
    """Taux de change HTG/USD du jour — saisi par la caisse."""
    __tablename__ = "taux_change"
    id          = Column(Integer, primary_key=True)
    taux_htg    = Column(Float, nullable=False)   # Ex: 130.0 signifie 1 USD = 130 HTG
    date        = Column(DateTime(timezone=True), server_default=func.now())
    saisi_par   = Column(Integer, ForeignKey("users.id"), nullable=True)

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


# ══════════════════════════════════════════════════════════════════════════════
# RÔLES MANQUANTS
# ══════════════════════════════════════════════════════════════════════════════
# Ajout infirmier et pharmacie dans RoleEnum — fait via migration SQL
# (RoleEnum déjà défini en haut, modifié via ALTER TYPE)



# ══════════════════════════════════════════════════════════════════════════════
# DEMANDE D'ACCÈS DOSSIER (médecin → admin → autorisation)
# ══════════════════════════════════════════════════════════════════════════════

class StatutDemandeEnum(str, enum.Enum):
    en_attente = "en_attente"
    approuve   = "approuve"
    refuse     = "refuse"
    expire     = "expire"


class DemandeAccesDossier(Base):
    """
    Système d'autorisation admin pour accès médecin à un dossier.
    Flux : médecin demande → admin approuve/refuse → accès temporaire 24h.
    Tout est journalisé dans audit_logs.
    """
    __tablename__ = "demandes_acces_dossier"

    id              = Column(Integer, primary_key=True)
    # Demandeur
    medecin_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    medecin_nom     = Column(String(255), nullable=False)
    medecin_specialite = Column(String(255), nullable=True)
    # Patient concerné
    patient_id      = Column(Integer, ForeignKey("patients.id"), nullable=True)
    patient_numero  = Column(String(20), nullable=False)
    dossier_id      = Column(Integer, ForeignKey("dossiers_patients.id"), nullable=True)
    # Motif de la demande
    motif           = Column(Text, nullable=False)
    urgence         = Column(Boolean, default=False)
    # Décision admin
    statut          = Column(Enum(StatutDemandeEnum, native_enum=False), default=StatutDemandeEnum.en_attente)
    admin_id        = Column(Integer, ForeignKey("users.id"), nullable=True)
    admin_commentaire = Column(Text, nullable=True)
    # Durée d'accès si approuvé (heures)
    duree_acces_h   = Column(Integer, default=24)
    acces_expire_at = Column(DateTime(timezone=True), nullable=True)
    # Timestamps
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)
    decided_at      = Column(DateTime(timezone=True), nullable=True)

# ══════════════════════════════════════════════════════════════════════════════
# JOURNAL D'AUDIT IMMUABLE
# ══════════════════════════════════════════════════════════════════════════════

class AuditLog(Base):
    """
    Journal d'audit immuable — HIPAA / RGPD / OPS-OMS.
    JAMAIS de UPDATE ou DELETE sur cette table.
    Timestamp toujours généré côté serveur UTC.
    """
    __tablename__ = "audit_logs"

    id          = Column(Integer, primary_key=True)
    audit_id    = Column(String(36), unique=True, nullable=False)  # UUID
    event_type  = Column(String(50), nullable=False)   # DOSSIER_CONSULTE, CONNEXION, etc.
    actor_id    = Column(Integer, nullable=True)
    actor_role  = Column(String(30), nullable=True)
    target_id   = Column(String(100), nullable=True)   # ID patient, dossier, transaction
    target_type = Column(String(50), nullable=True)    # patient, dossier, paiement
    timestamp   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ip_address  = Column(String(45), nullable=True)
    device_info = Column(String(500), nullable=True)
    session_id  = Column(String(100), nullable=True)
    result      = Column(String(10), default="succes") # succes / echec
    details     = Column(Text, nullable=True)
    # Rétention selon type (en années)
    retention_ans = Column(Integer, default=5)




# ─── Autorisation de paiement (admin) ────────────────────────────────────────
class AutorisationPaiement(Base):
    """
    L'admin peut autoriser un patient à recevoir un service sans payer
    (employé, cas social, partenaire). Le statut s'affiche en jaune pour
    l'infirmier et le médecin.
    """
    __tablename__ = "autorisations_paiement"
    id            = Column(Integer, primary_key=True)
    patient_id    = Column(Integer, ForeignKey("patients.id"), nullable=True)
    patient_nom   = Column(String(255))
    patient_numero = Column(String(20))
    motif         = Column(String(255))  # "Employé", "Cas social", "Partenaire", etc.
    service       = Column(String(255), nullable=True)  # None = tous les services
    date_validite = Column(DateTime(timezone=True), nullable=True)  # None = indéfini
    actif         = Column(Boolean, default=True)
    created_by    = Column(Integer, ForeignKey("users.id"))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

# ══════════════════════════════════════════════════════════════════════════════
# DOSSIER PATIENT MÉDICAL
# ══════════════════════════════════════════════════════════════════════════════

class StatutDossierEnum(str, enum.Enum):
    attente_infirmier  = "attente_infirmier"
    attente_medecin    = "attente_medecin"
    en_consultation    = "en_consultation"
    observation        = "observation"
    hospitalisation    = "hospitalisation"
    maternite          = "maternite"
    salle_sop          = "salle_sop"
    termine            = "termine"
    archive            = "archive"


class DossierPatient(Base):
    """
    Dossier médical d'une visite.
    Chaque visite crée un nouveau dossier (pas de fusion).
    Accès contrôlé par statut + rôle.
    """
    __tablename__ = "dossiers_patients"

    id              = Column(Integer, primary_key=True)
    # Liens
    patient_id      = Column(Integer, ForeignKey("patients.id"), nullable=False)
    patient_numero  = Column(String(20), nullable=False)  # #RB-0001
    medecin_id      = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    infirmier_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Type de visite
    type_visite     = Column(String(50), default="premiere_consultation")  # premiere_consultation, rendez_vous, urgence
    service         = Column(String(50), default="clinique")
    specialite      = Column(String(100), nullable=True)
    # Statut
    statut          = Column(Enum(StatutDossierEnum, native_enum=False), default=StatutDossierEnum.attente_infirmier)
    # Paiement requis avant accès médecin
    paiement_effectue = Column(Boolean, default=False)
    mouvement_paiement_id = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    # Verrouillage
    locked          = Column(Boolean, default=True)   # True = accès restreint
    unlock_par      = Column(Integer, ForeignKey("users.id"), nullable=True)
    unlock_at       = Column(DateTime(timezone=True), nullable=True)
    # Contenu médical
    motif_consultation = Column(Text, nullable=True)
    anamnese        = Column(Text, nullable=True)       # Histoire de la maladie
    examen_clinique = Column(Text, nullable=True)       # Examen physique
    diagnostic      = Column(Text, nullable=True)
    notes_medecin   = Column(Text, nullable=True)
    synthese_ia     = Column(Text, nullable=True)       # Résumé généré par IA
    # Dates
    date_visite     = Column(DateTime(timezone=True), server_default=func.now())
    date_fin_consultation = Column(DateTime(timezone=True), nullable=True)
    created_by      = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)


# ══════════════════════════════════════════════════════════════════════════════
# SIGNES VITAUX (Infirmier)
# ══════════════════════════════════════════════════════════════════════════════

class SignesVitaux(Base):
    __tablename__ = "signes_vitaux"

    id              = Column(Integer, primary_key=True)
    dossier_id      = Column(Integer, ForeignKey("dossiers_patients.id"), nullable=False)
    patient_id      = Column(Integer, ForeignKey("patients.id"), nullable=False)
    # Mesures
    tension_systolique  = Column(Float, nullable=True)   # mmHg
    tension_diastolique = Column(Float, nullable=True)   # mmHg
    frequence_cardiaque = Column(Integer, nullable=True) # bpm
    temperature         = Column(Float, nullable=True)   # °C
    frequence_respiratoire = Column(Integer, nullable=True)  # /min
    saturation_o2       = Column(Float, nullable=True)   # %
    poids               = Column(Float, nullable=True)   # kg
    taille              = Column(Float, nullable=True)   # cm
    glycemie            = Column(Float, nullable=True)   # mg/dL
    notes               = Column(Text, nullable=True)
    # Alertes IA
    alerte_critique     = Column(Boolean, default=False)
    alerte_message      = Column(Text, nullable=True)
    # Audit
    saisi_par       = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)


# ══════════════════════════════════════════════════════════════════════════════
# PRESCRIPTIONS / ORDONNANCES
# ══════════════════════════════════════════════════════════════════════════════

class Prescription(Base):
    __tablename__ = "prescriptions"

    id              = Column(Integer, primary_key=True)
    dossier_id      = Column(Integer, ForeignKey("dossiers_patients.id"), nullable=False)
    patient_id      = Column(Integer, ForeignKey("patients.id"), nullable=False)
    medecin_id      = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom     = Column(String(255), nullable=True)
    # Médicaments (JSON-like en texte)
    medicaments     = Column(Text, nullable=False)  # JSON: [{nom, dose, duree, instructions}]
    examens_requis  = Column(Text, nullable=True)   # Examens prescrits
    notes           = Column(Text, nullable=True)
    # Signature numérique (hash)
    signature_hash  = Column(String(64), nullable=True)
    signee          = Column(Boolean, default=False)
    date_prescription = Column(DateTime(timezone=True), server_default=func.now())
    valide_jusqu_au = Column(DateTime(timezone=True), nullable=True)
    statut          = Column(String(20), default="active")  # active, executee, expiree


# ══════════════════════════════════════════════════════════════════════════════
# FILE D'ATTENTE
# ══════════════════════════════════════════════════════════════════════════════

class FileAttente(Base):
    __tablename__ = "file_attente"

    id              = Column(Integer, primary_key=True)
    dossier_id      = Column(Integer, ForeignKey("dossiers_patients.id"), nullable=False)
    patient_id      = Column(Integer, ForeignKey("patients.id"), nullable=False)
    patient_numero  = Column(String(20), nullable=False)
    medecin_id      = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    medecin_nom     = Column(String(255), nullable=True)
    priorite        = Column(Integer, default=5)     # 1=urgence, 5=normal
    statut          = Column(String(20), default="en_attente")  # en_attente, en_cours, termine
    heure_entree    = Column(DateTime(timezone=True), server_default=func.now())
    heure_appel     = Column(DateTime(timezone=True), nullable=True)
    heure_fin       = Column(DateTime(timezone=True), nullable=True)
    place_par       = Column(Integer, ForeignKey("users.id"), nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
# HOSPITALISATION / OBSERVATION
# ══════════════════════════════════════════════════════════════════════════════

class Hospitalisation(Base):
    __tablename__ = "hospitalisations"

    id              = Column(Integer, primary_key=True)
    dossier_id      = Column(Integer, ForeignKey("dossiers_patients.id"), nullable=False)
    patient_id      = Column(Integer, ForeignKey("patients.id"), nullable=False)
    patient_numero  = Column(String(20), nullable=False)
    type_sejour     = Column(String(30), default="hospitalisation")  # hospitalisation, observation, maternite, sop
    medecin_id      = Column(Integer, ForeignKey("profils_medecins.id"), nullable=True)
    lit_numero      = Column(String(20), nullable=True)
    service         = Column(String(100), nullable=True)
    date_admission  = Column(DateTime(timezone=True), server_default=func.now())
    date_sortie     = Column(DateTime(timezone=True), nullable=True)
    # Facturation
    nb_jours        = Column(Integer, default=0)
    tarif_journalier = Column(Float, default=0)
    total_hebergement = Column(Float, default=0)
    acquittement_total = Column(Boolean, default=False)
    mouvement_id    = Column(Integer, ForeignKey("mouvements.id"), nullable=True)
    statut          = Column(String(20), default="actif")  # actif, sorti
    created_by      = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)


# ══════════════════════════════════════════════════════════════════════════════
# NOTATION / AVIS PATIENT
# ══════════════════════════════════════════════════════════════════════════════

class AvisPatient(Base):
    __tablename__ = "avis_patients"

    id              = Column(Integer, primary_key=True)
    patient_id      = Column(Integer, ForeignKey("patients.id"), nullable=True)
    dossier_id      = Column(Integer, ForeignKey("dossiers_patients.id"), nullable=True)
    medecin_nom     = Column(String(255), nullable=True)
    service         = Column(String(100), nullable=True)
    note            = Column(Integer, nullable=False)   # 1-5
    commentaire     = Column(Text, nullable=True)
    sentiment_ia    = Column(String(20), nullable=True) # positif, neutre, negatif
    anonyme         = Column(Boolean, default=False)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)


# ══════════════════════════════════════════════════════════════════════════════
# LIEN VIDÉO SÉCURISÉ (JWT 2h)
# ══════════════════════════════════════════════════════════════════════════════

class LienVideoRdv(Base):
    __tablename__ = "liens_video_rdv"

    id              = Column(Integer, primary_key=True)
    rdv_id          = Column(Integer, ForeignKey("rendez_vous.id"), nullable=False)
    token_jwt       = Column(String(500), unique=True, nullable=False)
    lien_video      = Column(String(500), nullable=False)
    expire_at       = Column(DateTime(timezone=True), nullable=False)  # +2h à partir connexion
    utilise         = Column(Boolean, default=False)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Boolean, default=False)
