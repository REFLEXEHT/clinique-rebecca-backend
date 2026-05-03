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
    """Add new columns to existing tables without dropping data."""
    from sqlalchemy import text
    from app.database import SessionLocal
    migrations = [
        # Users table new columns
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_image TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_updated_at TIMESTAMP WITH TIME ZONE",
        # RDV table new columns
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par INTEGER",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS confirme_par_role VARCHAR(20)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS autre_moment_propose VARCHAR(50)",
        "ALTER TABLE rendez_vous ADD COLUMN IF NOT EXISTS autre_moment_message VARCHAR(500)",
        # Add paiement_requis/paiement_effectue/propose_autre_moment to StatutRDVEnum if needed
        "ALTER TYPE statutrdvenum ADD VALUE IF NOT EXISTS 'paiement_requis'",
        "ALTER TYPE statutrdvenum ADD VALUE IF NOT EXISTS 'paiement_effectue'",
        "ALTER TYPE statutrdvenum ADD VALUE IF NOT EXISTS 'propose_autre_moment'",
    ]
    db = SessionLocal()
    try:
        for sql in migrations:
            try:
                db.execute(text(sql))
                db.commit()
            except Exception as e:
                db.rollback()
                # Ignore errors for columns that already exist or enum values that exist
                if 'already exists' not in str(e).lower() and 'duplicate' not in str(e).lower():
                    print(f"Migration warning: {e}")
    finally:
        db.close()

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
