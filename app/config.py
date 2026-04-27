from pydantic_settings import BaseSettings
from typing import List
import secrets


class Settings(BaseSettings):
    # Base de données — obligatoire en production
    DATABASE_URL: str = "postgresql://rebecca:rebecca2026@localhost:5432/clinique_rebecca"

    # Sécurité JWT — à définir via variable d'environnement en production
    # Ne jamais laisser cette valeur par défaut en production
    SECRET_KEY: str = secrets.token_urlsafe(32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24h

    # API IA (optionnel)
    ANTHROPIC_API_KEY: str = ""

    # SMTP (optionnel)
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""

    # Emails internes
    ADMIN_EMAIL: str = "admin@cliniquerebecca.ht"
    CAISSE_EMAIL: str = "caisse@cliniquerebecca.ht"
    MEDECIN_EMAIL: str = "medecin@cliniquerebecca.ht"

    # Contacts clinique
    WHATSAPP_PHONE: str = "50938880000"
    CLINIQUE_TELEPHONE: str = "+509 3888-0000"

    # Environnement
    ENVIRONMENT: str = "development"  # "production" en prod

    # CORS — liste d'origines séparées par virgule
    # Exemple : https://clinique-rebecca.vercel.app,https://cliniquerebecca.ht
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        origins = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        # Ajouter automatiquement les domaines Vercel courants si ENVIRONMENT=production
        if self.ENVIRONMENT == "production":
            # Autoriser le domaine principal Vercel du frontend
            pass
        return origins

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
