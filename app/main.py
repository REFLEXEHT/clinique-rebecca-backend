"""
main.py — Version Render/Production
Ajouts par rapport au dev local :
 - Gestion du PORT via variable d'environnement Render
 - Headers sécurité pour production
 - Logs structurés
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from contextlib import asynccontextmanager
from app.database import engine, Base
from app.routers import router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.config import settings
from app.seed import seed_database
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Démarrage Clinique de la Rebecca API...")
    Base.metadata.create_all(bind=engine)
    seed_database()
    start_scheduler()
    logger.info("✅ API prête — http://0.0.0.0:%s", os.getenv("PORT", "8000"))
    yield
    stop_scheduler()
    logger.info("👋 Arrêt de l'API")


app = FastAPI(
    title="Clinique de la Rebecca — API",
    description="Backend FastAPI · PostgreSQL · Render",
    version="1.0.0",
    lifespan=lifespan,
    # Désactiver docs en production si souhaité
    docs_url="/docs" if os.getenv("ENVIRONMENT") != "production" else None,
    redoc_url=None,
)

# ─── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list + [
        "https://clinique-rebecca.vercel.app",
        "https://*.vercel.app",   # Preview deployments Vercel
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/", tags=["Health"])
async def root():
    return {
        "status": "ok",
        "app": "Clinique de la Rebecca API",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "development"),
    }


@app.get("/health", tags=["Health"])
async def health():
    """Endpoint santé pour Render health check"""
    return {"status": "healthy"}
