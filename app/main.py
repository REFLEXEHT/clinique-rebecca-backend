from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import engine, Base
from app.routers import router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.config import settings
from app.seed import seed_database
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Démarrage Clinique de la Rebecca API...")
    Base.metadata.create_all(bind=engine)
    seed_database()
    ensure_admin()
    start_scheduler()
    yield
    stop_scheduler()

def ensure_admin():
    """Garantit qu'un admin existe toujours"""
    from app.database import SessionLocal
    from app.auth import get_password_hash
    import app.models as models
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter(models.User.email == "admin@cliniquerebecca.ht").first()
        if not admin:
            db.add(models.User(
                email="admin@cliniquerebecca.ht",
                nom="Administrateur",
                hashed_password=get_password_hash("rebecca2026"),
                role="admin"
            ))
            db.commit()
            logger.info("Admin créé automatiquement")
        else:
            logger.info("Admin existant trouvé")
    except Exception as e:
        logger.error("Erreur ensure_admin: %s", e)
        db.rollback()
    finally:
        db.close()

app = FastAPI(
    title="Clinique de la Rebecca — API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

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
    return {"status": "ok", "app": "Clinique de la Rebecca API"}

@app.get("/health")
async def health():
    return {"status": "healthy"}
