"""
Données initiales : insérées une seule fois au démarrage
"""
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.auth import get_password_hash
import app.models as models
import logging

logger = logging.getLogger(__name__)


def seed_database():
    db: Session = SessionLocal()
    try:
        # ─── Admin user ───────────────────────────────────────────────────────
        if not db.query(models.User).first():
            admin = models.User(
                email="admin@cliniquerebecca.ht",
                nom="Administrateur",
                hashed_password=get_password_hash("rebecca2026"),
                role="admin",
            )
            db.add(admin)
            logger.info("Admin créé : admin@cliniquerebecca.ht / rebecca2026")

        # ─── Services ─────────────────────────────────────────────────────────
        if not db.query(models.Service).first():
            services = [
                {"nom": "Clinique Externe", "icone": "fa-stethoscope", "couleur": "#1a4fc4",
                 "description": "12 spécialistes : chirurgie, neurologie, pédiatrie, gynécologie, ORL et plus.", "ordre": 1},
                {"nom": "Dentisterie", "icone": "fa-tooth", "couleur": "#5aaa28",
                 "description": "Soins dentaires préventifs et curatifs, orthodontie, chirurgie buccale.", "ordre": 2},
                {"nom": "Physiothérapie", "icone": "fa-person-walking", "couleur": "#e07a00",
                 "description": "Rééducation fonctionnelle et traitement des douleurs chroniques.", "ordre": 3},
                {"nom": "Laboratoire", "icone": "fa-flask-vial", "couleur": "#1a4fc4",
                 "description": "Analyses complètes avec résultats envoyés par WhatsApp et email.", "ordre": 4},
                {"nom": "Pharmacie", "icone": "fa-pills", "couleur": "#5aaa28",
                 "description": "Médicaments génériques et de marque, conseils pharmaceutiques.", "ordre": 5},
                {"nom": "Optométrie", "icone": "fa-glasses", "couleur": "#be185d",
                 "description": "Examen de la vue, prescription de lunettes et dépistage oculaire.", "ordre": 6},
                {"nom": "Salle d'Opération", "icone": "fa-scalpel", "couleur": "#1a4fc4",
                 "description": "Bloc opératoire pour chirurgies programmées.", "ordre": 7},
                {"nom": "Salle d'Accouchement", "icone": "fa-baby", "couleur": "#be185d",
                 "description": "Maternité sécurisée, suivi pré et postnatal.", "ordre": 8},
                {"nom": "Gestes Médicaux", "icone": "fa-syringe", "couleur": "#e07a00",
                 "description": "Injections, pansements, perfusions, ECG et actes techniques.", "ordre": 9},
            ]
            for s in services:
                db.add(models.Service(**s))

        # ─── Spécialistes ─────────────────────────────────────────────────────
        if not db.query(models.Specialiste).first():
            specs = [
                {"nom": "Dr. Michel Dubois", "specialite": "Chirurgie générale", "emoji": "🔬", "categorie": "chir",
                 "description": "Chirurgien senior, 15 ans d'expérience"},
                {"nom": "Dr. Anne-Marie Pierre", "specialite": "Neurochirurgie", "emoji": "🧠", "categorie": "neuro",
                 "description": "Spécialiste neurochirurgie pédiatrique"},
                {"nom": "Dr. Jean-Claude Étienne", "specialite": "Neurologie", "emoji": "🧬", "categorie": "neuro",
                 "description": "Épilepsie, AVC, sclérose en plaques"},
                {"nom": "Dr. Sophie Lamour", "specialite": "Orthopédie", "emoji": "🦴", "categorie": "chir",
                 "description": "Traumatologie, chirurgie osseuse"},
                {"nom": "Dr. Paul Désir", "specialite": "Pédiatrie", "emoji": "👶", "categorie": "ped",
                 "description": "Soins nouveau-nés et enfants"},
                {"nom": "Dr. Isabelle François", "specialite": "Dermatologie", "emoji": "🌸", "categorie": "tous",
                 "description": "Maladies de peau, cosmétologie médicale"},
                {"nom": "Dr. Henri Nazaire", "specialite": "Urologie", "emoji": "💊", "categorie": "tous",
                 "description": "Système urinaire, prostate"},
                {"nom": "Dr. Marie-Rose Cajuste", "specialite": "ORL", "emoji": "👂", "categorie": "tous",
                 "description": "Oreille, nez, gorge, chirurgie ORL"},
                {"nom": "Dr. Claudette Joseph", "specialite": "Gynécologie", "emoji": "🌺", "categorie": "gyn",
                 "description": "Suivi grossesse, santé féminine"},
                {"nom": "Dr. Patrick Dorival", "specialite": "Chirurgie pédiatrique", "emoji": "🏥", "categorie": "ped",
                 "description": "Chirurgie des nourrissons"},
                {"nom": "Dr. Réginald Louis", "specialite": "Médecine interne", "emoji": "❤️", "categorie": "tous",
                 "description": "Diabète, hypertension, maladies chroniques"},
                {"nom": "Dr. Nathalie Vincent", "specialite": "Ophtalmologie", "emoji": "👁️", "categorie": "tous",
                 "description": "Chirurgie oculaire, glaucome, cataracte"},
            ]
            for i, s in enumerate(specs):
                db.add(models.Specialiste(**s, ordre=i))

        # ─── Horaires ─────────────────────────────────────────────────────────
        if not db.query(models.Horaire).first():
            jours = [
                {"jour": "Lundi", "ouvert": True, "heure_ouverture": "07:00", "heure_fermeture": "17:00"},
                {"jour": "Mardi", "ouvert": True, "heure_ouverture": "07:00", "heure_fermeture": "17:00"},
                {"jour": "Mercredi", "ouvert": True, "heure_ouverture": "07:00", "heure_fermeture": "17:00"},
                {"jour": "Jeudi", "ouvert": True, "heure_ouverture": "07:00", "heure_fermeture": "17:00"},
                {"jour": "Vendredi", "ouvert": True, "heure_ouverture": "07:00", "heure_fermeture": "17:00"},
                {"jour": "Samedi", "ouvert": True, "heure_ouverture": "07:00", "heure_fermeture": "17:00"},
                {"jour": "Dimanche", "ouvert": True, "heure_ouverture": "07:00", "heure_fermeture": "15:00"},
            ]
            for j in jours:
                db.add(models.Horaire(**j))

        db.commit()
        logger.info("Base de données initialisée avec succès")

    except Exception as e:
        logger.error("Erreur seed: %s", e)
        db.rollback()
    finally:
        db.close()

def seed_optometrie():
    from app.database import SessionLocal
    import app.models as models
    db = SessionLocal()
    try:
        if not db.query(models.ContratOptometrie).first():
            db.add(models.ContratOptometrie(
                pct_consultation=35.0,
                pct_montures=13.0,
                minimum_mensuel_usd=300.0,
                taux_usd_htg=130.0,
            ))
            db.commit()
            print("Contrat optométrie initialisé")
    except Exception as e:
        print(f"Erreur seed_optometrie: {e}")
        db.rollback()
    finally:
        db.close()
