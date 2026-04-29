from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import engine, Base
from app.routers import router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.seed import seed_database
from app.config import settings
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def ensure_admin():
    """
    Crée ou met à jour le compte admin au démarrage.
    Priorité : variable ADMIN_DEFAULT_PASSWORD → valeur par défaut 'rebecca2026'
    """
    from app.database import SessionLocal
    import app.models as models
    from passlib.context import CryptContext

    # Mot de passe : variable d'env en priorité, sinon valeur par défaut
    default_password = os.environ.get("ADMIN_DEFAULT_PASSWORD", "rebecca2026")
    admin_email      = os.environ.get("ADMIN_EMAIL_OVERRIDE", settings.ADMIN_EMAIL)

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter(
            models.User.email == admin_email
        ).first()
        if not admin:
            db.add(models.User(
                email=admin_email,
                nom="Administrateur",
                hashed_password=pwd_context.hash(default_password),
                role="admin",
                is_active=True,
            ))
            db.commit()
            logger.info("✅ Admin créé : %s", admin_email)
        else:
            admin.hashed_password = pwd_context.hash(default_password)
            admin.role = "admin"
            admin.is_active = True
            db.commit()
            logger.info("✅ Admin réinitialisé : %s", admin_email)
    except Exception as e:
        logger.error("Erreur ensure_admin: %s", e)
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    try:
        seed_database()
    except Exception as e:
        logger.warning("Seed skipped: %s", e)
    ensure_admin()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="Clinique de la Rebecca API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",   # Toujours actif pour faciliter le débogage
    redoc_url=None,
)

# CORS — autoriser les origines déclarées dans la config
allowed_origins = settings.cors_origins_list
logger.info("CORS origins autorisées : %s", allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Temporaire — sera restreint quand domaine définitif connu
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    max_age=600,
)

app.include_router(router, prefix="/api")


@app.get("/")
async def root():
    return {"status": "ok", "app": "Clinique de la Rebecca API v2.0"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
