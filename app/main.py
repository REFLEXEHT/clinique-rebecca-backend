from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import engine, Base
from app.routers import router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.seed import seed_database
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def ensure_admin():
    from app.database import SessionLocal
    import app.models as models
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first()
        if not admin:
            db.add(models.User(
                email="admin@cliniquerebecca.ht",
                nom="Administrateur",
                hashed_password=pwd_context.hash("rebecca2026"),
                role="admin"
            ))
            db.commit()
            logger.info("✅ Admin créé")
        else:
            # Reset le mot de passe admin au cas où
            admin.hashed_password = pwd_context.hash("rebecca2026")
            admin.role = "admin"
            db.commit()
            logger.info("✅ Admin mot de passe réinitialisé")
    except Exception as e:
        logger.error("Erreur ensure_admin: %s", e)
        db.rollback()
    finally:
        db.close()

def migrate_add_missing_columns():
    """Add new columns and enum values safely without losing data."""
    from sqlalchemy import text
    from app.database import engine

    # ── 1. Convertir colonnes native-enum → VARCHAR (fix critique) ──────────────
    # Le schéma original utilisait Enum(RoleEnum) natif PostgreSQL.
    # Le code actuel utilise native_enum=False (VARCHAR). Cette migration convertit
    # les colonnes existantes pour que les INSERTs fonctionnent sans erreur de type.
    enum_to_varchar = [
        "ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(50) USING role::text",
        "ALTER TABLE users ALTER COLUMN type_medecin TYPE VARCHAR(50) USING type_medecin::text",
        "ALTER TABLE rendez_vous ALTER COLUMN statut TYPE VARCHAR(50) USING statut::text",
        "ALTER TABLE rendez_vous ALTER COLUMN type_rdv TYPE VARCHAR(20) USING type_rdv::text",
        "ALTER TABLE rendez_vous ALTER COLUMN devise TYPE VARCHAR(10) USING devise::text",
        "ALTER TABLE dossiers_patients ALTER COLUMN statut TYPE VARCHAR(50) USING statut::text",
        "ALTER TABLE profils_medecins ALTER COLUMN type_medecin TYPE VARCHAR(50) USING type_medecin::text",
        "ALTER TABLE mouvements ALTER COLUMN type TYPE VARCHAR(30) USING type::text",
        "ALTER TABLE mouvements ALTER COLUMN devise TYPE VARCHAR(10) USING devise::text",
        "ALTER TABLE mouvements ALTER COLUMN journal TYPE VARCHAR(10) USING journal::text",
        "ALTER TABLE encaissements ALTER COLUMN devise TYPE VARCHAR(10) USING devise::text",
        "ALTER TABLE decaissements ALTER COLUMN devise TYPE VARCHAR(10) USING devise::text",
        "ALTER TABLE periodes_comptables ALTER COLUMN statut TYPE VARCHAR(20) USING statut::text",
        "ALTER TABLE stocks ALTER COLUMN devise_acquisition TYPE VARCHAR(10) USING devise_acquisition::text",
        "ALTER TABLE tarifs_medecins ALTER COLUMN type_medecin TYPE VARCHAR(50) USING type_medecin::text",
        "ALTER TABLE tarifs_config ALTER COLUMN type_medecin TYPE VARCHAR(50) USING type_medecin::text",
        "ALTER TABLE demandes_acces_dossier ALTER COLUMN statut TYPE VARCHAR(30) USING statut::text",
        "ALTER TABLE labo_analyses ALTER COLUMN type_medecin TYPE VARCHAR(50) USING type_medecin::text",
    ]
    for sql in enum_to_varchar:
        try:
            with engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
            print(f"✓ enum→varchar: {sql[35:75]}")
        except Exception as e:
            msg = str(e).lower()
            # "cannot be cast" means it's already varchar — safe to ignore
            if "already exists" not in msg and "cannot be cast" not in msg and "does not exist" not in msg:
                print(f"Enum migration note [{sql[35:55]}]: {e}")

    # ── 2. Ajouter colonnes manquantes ───────────────────────────────────────
    column_migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_image TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par INTEGER",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par_role VARCHAR(20)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS autre_moment_propose VARCHAR(50)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS autre_moment_message VARCHAR(500)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_id INTEGER",
        "ALTER TABLE dossiers_patients ADD COLUMN IF NOT EXISTS rdv_id INTEGER",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS categorie VARCHAR(255)",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS prix_usd FLOAT DEFAULT 0",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS prix_usd_min FLOAT",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS prix_usd_max FLOAT",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS prix_clinique_usd FLOAT",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS source_bareme VARCHAR(50)",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS prix_htg_ref FLOAT",
        "ALTER TABLE gestes_medicaux ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE tarifs_labo ADD COLUMN IF NOT EXISTS montant_usd FLOAT",
        "ALTER TABLE tarifs_dentiste ADD COLUMN IF NOT EXISTS montant_usd FLOAT",
        "ALTER TABLE tarifs_dentiste ADD COLUMN IF NOT EXISTS categorie VARCHAR(255)",
        "CREATE TABLE IF NOT EXISTS taux_change (id SERIAL PRIMARY KEY, taux_htg FLOAT NOT NULL, date TIMESTAMP WITH TIME ZONE DEFAULT NOW(), saisi_par INTEGER REFERENCES users(id))",
        "CREATE TABLE IF NOT EXISTS autorisations_paiement (id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES patients(id), patient_nom VARCHAR(255), patient_numero VARCHAR(20), motif VARCHAR(255), service VARCHAR(255), date_validite TIMESTAMP WITH TIME ZONE, actif BOOLEAN DEFAULT TRUE, created_by INTEGER REFERENCES users(id), created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW())",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS age INTEGER",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS date_naissance DATE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS photo_profil TEXT",
        "ALTER TABLE specialistes ADD COLUMN IF NOT EXISTS photo_profil TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS contact_urgence VARCHAR(255)",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS is_premiere_visite BOOLEAN DEFAULT TRUE",
        "ALTER TABLE specialistes ADD COLUMN IF NOT EXISTS titre VARCHAR(20) DEFAULT \'Dr\'",
        # Tiers comptable (bénéficiaire/payeur)
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS tiers_nom VARCHAR(255)",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS tiers_type VARCHAR(50)",
        "ALTER TABLE decaissements ADD COLUMN IF NOT EXISTS tiers_nom VARCHAR(255)",
        "ALTER TABLE tarifs_medecins ADD COLUMN IF NOT EXISTS solde_compte_468 FLOAT DEFAULT 0",
        # Décaissements planifiés
        "ALTER TABLE decaissements ADD COLUMN IF NOT EXISTS date_prevue TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE decaissements ADD COLUMN IF NOT EXISTS statut VARCHAR(20) DEFAULT \'effectue\'",
        # Mouvements enrichis
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS date_mouvement TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE mouvements ADD COLUMN IF NOT EXISTS reference VARCHAR(100)",
        # RendezVous — colonnes critiques pour l'enregistrement caissier
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS code_patient VARCHAR(20)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_nom VARCHAR(255)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_email VARCHAR(255)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par INTEGER",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par_role VARCHAR(20)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS mouvement_id INTEGER",
        # Autres colonnes critiques
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_image TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS service VARCHAR(100)",
        "ALTER TABLE specialistes ADD COLUMN IF NOT EXISTS categorie VARCHAR(100) DEFAULT \'tous\'",
        "ALTER TABLE tarifs_medecins ADD COLUMN IF NOT EXISTS type_medecin VARCHAR(50)",
        "ALTER TABLE tarifs_medecins ADD COLUMN IF NOT EXISTS prix_consultation FLOAT DEFAULT 0",
        "ALTER TABLE tarifs_medecins ADD COLUMN IF NOT EXISTS prix_rdv FLOAT DEFAULT 0",
    ]
    # Each migration in its own connection to avoid transaction interference
    for sql in column_migrations:
        try:
            with engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
            print(f"✓ {sql[:70]}")
        except Exception as e:
            msg = str(e).lower()
            if "already exists" not in msg and "duplicate" not in msg:
                print(f"Migration warning [{sql[:40]}]: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_add_missing_columns()
    seed_database()
    ensure_admin()
    start_scheduler()
    yield
    stop_scheduler()

app = FastAPI(title="Clinique de la Rebecca API", version="1.0.0", lifespan=lifespan, docs_url="/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "healthy"}
