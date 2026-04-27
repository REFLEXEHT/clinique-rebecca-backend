"""
Planificateur de tâches : rappels automatiques 6h avant chaque RDV
FIX v2: type_rdv et lien_video inclus dans rdv_data (étaient manquants)
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.services.notifications import notify_rdv_rappel
import app.models as models
import logging

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="America/Port-au-Prince")


async def check_and_send_reminders():
    """Vérifie chaque minute les RDVs à rappeler dans ~6h."""
    db: Session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        window_start = now + timedelta(hours=5, minutes=55)
        window_end   = now + timedelta(hours=6, minutes=5)

        rdvs = (
            db.query(models.RendezVous)
            .filter(
                models.RendezVous.statut == models.StatutRDVEnum.confirme,
                models.RendezVous.rappel_envoye == False,
                models.RendezVous.date_rdv >= window_start,
                models.RendezVous.date_rdv <= window_end,
            )
            .all()
        )

        for rdv in rdvs:
            rdv_data = {
                "patient_nom":       rdv.patient_nom,
                "patient_telephone": rdv.patient_telephone,
                "patient_email":     rdv.patient_email,
                "specialite":        rdv.specialite,
                "date_rdv":          rdv.date_rdv,
                "type_rdv":          str(rdv.type_rdv),      # FIX: manquait
                "lien_video":        rdv.lien_video or "",    # FIX: manquait
                "medecin_nom":       rdv.medecin_nom or "",
            }
            await notify_rdv_rappel(rdv_data)
            rdv.rappel_envoye = True
            db.commit()
            logger.info("Rappel envoyé pour RDV #%d — %s", rdv.id, rdv.patient_nom)

    except Exception as e:
        logger.error("Erreur scheduler rappels: %s", e)
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(
        check_and_send_reminders,
        trigger=IntervalTrigger(minutes=1),
        id="rdv_reminders",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler démarré — vérification rappels toutes les minutes")


def stop_scheduler():
    try:
        scheduler.shutdown()
    except Exception:
        pass
