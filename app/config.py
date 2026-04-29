from pydantic_settings import BaseSettings
from typing import List
import secrets


class Settings(BaseSettings):
    # Base de données
    DATABASE_URL: str = "postgresql://rebecca:rebecca2026@localhost:5432/clinique_rebecca"

    # JWT
    SECRET_KEY: str = secrets.token_urlsafe(32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24h

    # IA
    ANTHROPIC_API_KEY: str = ""

    # SMTP
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
    ENVIRONMENT: str = "development"

    # CORS — séparées par virgule
    # Ex: https://clinique-rebecca-frontend.vercel.app,https://cliniquerebecca.ht
    CORS_ORIGINS: str = "http://localhost:3000,https://clinique-rebecca-frontend.vercel.app"

    @property
    def cors_origins_list(self) -> List[str]:
        origins = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        # Toujours inclure localhost pour développement local
        if "http://localhost:3000" not in origins:
            origins.append("http://localhost:3000")
        return origins

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
