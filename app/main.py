from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import engine, Base
from app.routers import router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.seed import seed_database
import logging

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
                role="admin",
                is_active=True,
            ))
            db.commit()
            logger.info("Admin créé : admin@cliniquerebecca.ht / rebecca2026")
        else:
            admin.hashed_password = pwd_context.hash("rebecca2026")
            admin.role = "admin"
            admin.is_active = True
            db.commit()
            logger.info("Admin mot de passe réinitialisé")
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
    return {"status": "ok", "app": "Clinique de la Rebecca API v2.0"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
