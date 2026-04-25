from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://rebecca:rebecca2026@localhost:5432/clinique_rebecca"
    SECRET_KEY: str = "changez-cette-cle-en-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    ANTHROPIC_API_KEY: str = ""

    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""

    # Emails internes — à configurer dans les variables Render
    ADMIN_EMAIL: str = "admin@cliniquerebecca.ht"
    CAISSE_EMAIL: str = "caisse@cliniquerebecca.ht"
    MEDECIN_EMAIL: str = "medecin@cliniquerebecca.ht"  # fallback si spécialité inconnue

    # Numéro WhatsApp de la clinique (pour notifications entrantes)
    WHATSAPP_PHONE: str = "50938880000"
    CLINIQUE_TELEPHONE: str = "+509 3888-0000"

    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    class Config:
        env_file = ".env"


settings = Settings()
