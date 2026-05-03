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

    # Step 1: Add enum values (MUST run outside transaction in PostgreSQL)
    enum_migrations = [
        "ALTER TYPE statutrdvenum ADD VALUE IF NOT EXISTS 'paiement_requis'",
        "ALTER TYPE statutrdvenum ADD VALUE IF NOT EXISTS 'paiement_effectue'",
        "ALTER TYPE statutrdvenum ADD VALUE IF NOT EXISTS 'propose_autre_moment'",
    ]
    try:
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            for sql in enum_migrations:
                try:
                    conn.execute(text(sql))
                    print(f"✓ Enum: {sql}")
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        print(f"Enum warning: {e}")
    except Exception as e:
        print(f"Enum migration error: {e}")

    # Step 2: Add columns (can run in transaction)
    column_migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_image TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par INTEGER",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par_role VARCHAR(20)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS autre_moment_propose VARCHAR(50)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS autre_moment_message VARCHAR(500)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS medecin_id INTEGER",
        "ALTER TABLE dossiers_patients ADD COLUMN IF NOT EXISTS rdv_id INTEGER",
    ]
    try:
        with engine.begin() as conn:
            for sql in column_migrations:
                try:
                    conn.execute(text(sql))
                    print(f"✓ Column: {sql[:60]}")
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        print(f"Column warning: {e}")
    except Exception as e:
        print(f"Column migration error: {e}")

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
