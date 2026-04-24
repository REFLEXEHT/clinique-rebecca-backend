"""
Seed initial — appelé automatiquement au démarrage via main.py
"""
from app.database import SessionLocal
from app.auth import get_password_hash
import app.models as models


def seed_database():
    """Nom attendu par main.py — initialise les données de base."""
    db = SessionLocal()
    try:
        # ── Horaires ───────────────────────────────────────────────────────
        jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
        for j in jours:
            if not db.query(models.Horaire).filter(models.Horaire.jour == j).first():
                hf = "15:00" if j == "Dimanche" else "17:00"
                db.add(models.Horaire(jour=j, ouvert=True, heure_ouverture="07:00", heure_fermeture=hf))

        # ── Règles de répartition par défaut ──────────────────────────────
        if db.query(models.ReglePartage).count() == 0:
            regles = [
                # Investisseur : 30% clinique sur consultations, 20% sur gestes
                ("investisseur", "consultation", 70, 30),
                ("investisseur", "geste",        80, 20),
                ("investisseur", "chirurgie",     0, 100),  # Montant manuel
                ("investisseur", "hospit",        70, 30),
                # Affilié : 40% clinique sur consultations, 30% sur gestes
                ("affilie", "consultation", 60, 40),
                ("affilie", "geste",        70, 30),
                ("affilie", "chirurgie",     0, 100),
                ("affilie", "hospit",        60, 40),
                # Exploitant : 100% médecin (loyer fixe séparé)
                ("exploitant", "consultation", 100, 0),
                ("exploitant", "geste",        100, 0),
                ("exploitant", "chirurgie",    100, 0),
                # Investisseur-Exploitant : 100% médecin (loyer fixe séparé)
                ("investisseur_exploitant", "consultation", 100, 0),
                ("investisseur_exploitant", "geste",        100, 0),
                ("investisseur_exploitant", "chirurgie",    100, 0),
            ]
            for tm, ta, pm, pc in regles:
                db.add(models.ReglePartage(
                    type_medecin=tm, type_acte=ta,
                    pct_medecin=pm, pct_clinique=pc
                ))

        # ── Contrat optométrie (valeurs par défaut) ────────────────────────
        if not db.query(models.ContratOptometrie).first():
            db.add(models.ContratOptometrie(
                pct_consultation=35.0,
                pct_montures=13.0,
                minimum_mensuel_usd=300.0,
                taux_usd_htg=130.0,
            ))

        # ── Tarifs configurables ───────────────────────────────────────────
        tarifs_defaut = [
            ("loyer_dentisterie", "Loyer Dentisterie (mensuel)",       0, "mois"),
            ("loyer_laboratoire", "Loyer Laboratoire (mensuel)",       0, "mois"),
            ("loyer_physio",      "Loyer Physiothérapie (mensuel)",    0, "mois"),
            ("loyer_optometrie",  "Loyer min. Optométrie (USD/mois)",  0, "mois"),
            ("forfait_hospit_jr", "Chambre hospitalisation (par jour)", 0, "jour"),
            ("frais_dossier",     "Frais dossier patient",             0, "HTG"),
        ]
        for code, libelle, montant, unite in tarifs_defaut:
            if not db.query(models.TarifClinic).filter(models.TarifClinic.code == code).first():
                db.add(models.TarifClinic(code=code, libelle=libelle, montant=montant, unite=unite))

        db.commit()

    except Exception as e:
        db.rollback()
        import logging
        logging.getLogger(__name__).error("Erreur seed: %s", e)
    finally:
        db.close()


# Alias pour compatibilité
run_seed = seed_database


if __name__ == "__main__":
    seed_database()
    print("✅ Seed terminé")
