"""
Script de seed initial — exécuter une seule fois après création de la base.
"""
from app.database import SessionLocal
from app.auth import get_password_hash
import app.models as models


def run_seed():
    db = SessionLocal()
    try:
        # ── Admin ──────────────────────────────────────────────────────────
        if not db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first():
            db.add(models.User(
                email="admin@cliniquerebecca.ht", nom="Administrateur Rebecca",
                hashed_password=get_password_hash("rebecca2026"),
                role=models.RoleEnum.admin, is_active=True,
            ))

        # ── Horaires ───────────────────────────────────────────────────────
        jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
        for j in jours:
            if not db.query(models.Horaire).filter(models.Horaire.jour == j).first():
                hf = "15:00" if j == "Dimanche" else "17:00"
                db.add(models.Horaire(jour=j, ouvert=True, heure_ouverture="07:00", heure_fermeture=hf))

        # ── Règles de répartition ──────────────────────────────────────────
        if db.query(models.ReglePartage).count() == 0:
            regles = [
                ("investisseur", "consultation", 70, 30),
                ("investisseur", "geste",        80, 20),
                ("investisseur", "chirurgie",     0, 100),
                ("investisseur", "hospit",        70, 30),
                ("affilie",      "consultation", 60, 40),
                ("affilie",      "geste",        70, 30),
                ("affilie",      "chirurgie",     0, 100),
                ("affilie",      "hospit",        60, 40),
                ("exploitant",   "consultation", 100, 0),
                ("exploitant",   "geste",        100, 0),
                ("exploitant",   "chirurgie",    100, 0),
                ("investisseur_exploitant", "consultation", 100, 0),
                ("investisseur_exploitant", "geste",        100, 0),
                ("investisseur_exploitant", "chirurgie",    100, 0),
            ]
            for tm, ta, pm, pc in regles:
                db.add(models.ReglePartage(type_medecin=tm, type_acte=ta, pct_medecin=pm, pct_clinique=pc))

        # ── Contrat optométrie (valeurs par défaut) ────────────────────────
        if not db.query(models.ContratOptometrie).first():
            db.add(models.ContratOptometrie(
                pct_consultation=35.0, pct_montures=13.0,
                minimum_mensuel_usd=300.0, taux_usd_htg=130.0,
            ))

        # ── Tarifs configurables ───────────────────────────────────────────
        tarifs_defaut = [
            ("loyer_dentisterie",  "Loyer Dentisterie (mensuel)",  0, "mois"),
            ("loyer_laboratoire",  "Loyer Laboratoire (mensuel)",  0, "mois"),
            ("loyer_physio",       "Loyer Physiothérapie (mensuel)", 0, "mois"),
            ("loyer_optometrie",   "Loyer min. Optométrie (mensuel — USD)", 0, "mois"),
            ("forfait_hospit_jr",  "Chambre hospitalisation (par jour)", 0, "jour"),
            ("frais_dossier",      "Frais dossier patient", 0, "HTG"),
        ]
        for code, libelle, montant, unite in tarifs_defaut:
            if not db.query(models.TarifClinic).filter(models.TarifClinic.code == code).first():
                db.add(models.TarifClinic(code=code, libelle=libelle, montant=montant, unite=unite))

        db.commit()
        print("✅ Seed terminé avec succès")
    except Exception as e:
        db.rollback()
        print(f"❌ Erreur seed: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    run_seed()
