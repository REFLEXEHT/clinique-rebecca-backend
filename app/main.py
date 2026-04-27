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
    Le mot de passe par défaut est lu depuis la variable d'environnement
    ADMIN_DEFAULT_PASSWORD — ne jamais coder en dur dans le source.
    """
    from app.database import SessionLocal
    import app.models as models
    from passlib.context import CryptContext

    default_password = os.environ.get("ADMIN_DEFAULT_PASSWORD", "")
    if not default_password:
        logger.warning(
            "ADMIN_DEFAULT_PASSWORD non défini — "
            "le compte admin ne sera pas créé automatiquement."
        )
        return

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter(
            models.User.email == settings.ADMIN_EMAIL
        ).first()
        if not admin:
            db.add(models.User(
                email=settings.ADMIN_EMAIL,
                nom="Administrateur",
                hashed_password=pwd_context.hash(default_password),
                role="admin",
                is_active=True,
            ))
            db.commit()
            logger.info("Admin créé : %s", settings.ADMIN_EMAIL)
        else:
            # Met à jour uniquement si le mot de passe a changé
            if not pwd_context.verify(default_password, admin.hashed_password):
                admin.hashed_password = pwd_context.hash(default_password)
                admin.role = "admin"
                admin.is_active = True
                db.commit()
                logger.info("Admin mis à jour : %s", settings.ADMIN_EMAIL)
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
    docs_url="/docs" if os.environ.get("ENVIRONMENT") != "production" else None,
    redoc_url=None,
)

# CORS — n'autoriser que les origines déclarées dans CORS_ORIGINS
allowed_origins = settings.cors_origins_list
logger.info("CORS origins autorisées : %s", allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,   # False car on utilise Bearer token, pas cookies
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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
