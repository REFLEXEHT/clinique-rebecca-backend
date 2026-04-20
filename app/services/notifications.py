"""
Service de notifications : Email SMTP + WhatsApp (lien wa.me)
Pour WhatsApp Business API officielle, remplacer les fonctions wa_* par l'API Meta.
"""
import asyncio
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from app.config import settings
import logging

logger = logging.getLogger(__name__)


def format_date_fr(dt: datetime) -> str:
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin",
            "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[dt.weekday()]} {dt.day} {mois[dt.month-1]} {dt.year} à {dt.strftime('%H:%M')}"


# ─── Email ───────────────────────────────────────────────────────────────────
async def send_email(to: str, subject: str, html_body: str):
    """Envoyer un email via SMTP"""
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning("SMTP non configuré, email simulé vers: %s", to)
        logger.info("Sujet: %s", subject)
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Clinique de la Rebecca <{settings.SMTP_USER}>"
        msg["To"] = to
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info("Email envoyé à %s", to)
        return True
    except Exception as e:
        logger.error("Erreur email: %s", e)
        return False


def get_whatsapp_link(phone: str, message: str) -> str:
    """Génère un lien WhatsApp (pour intégration webhook ou affichage)"""
    import urllib.parse
    clean_phone = phone.replace("+", "").replace(" ", "").replace("-", "")
    if not clean_phone.startswith("509"):
        clean_phone = "509" + clean_phone
    encoded = urllib.parse.quote(message)
    return f"https://wa.me/{clean_phone}?text={encoded}"


# ─── Templates email ─────────────────────────────────────────────────────────
def email_confirmation_patient(nom: str, specialite: str, date_rdv: datetime, type_rdv: str) -> str:
    date_str = format_date_fr(date_rdv)
    type_label = "Vidéo (en ligne)" if type_rdv == "video" else "En personne à la clinique"
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      <div style="background:#1a2a4a;padding:24px;border-radius:12px 12px 0 0;text-align:center;">
        <h1 style="color:#fff;margin:0;font-size:22px;">Clinique de la Rebecca</h1>
        <p style="color:rgba(255,255,255,0.7);margin:4px 0 0;">Confirmation de rendez-vous</p>
      </div>
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#4a5a78;">Votre rendez-vous a bien été enregistré :</p>
        <div style="background:#f4f7fd;border-left:4px solid #5aaa28;padding:16px;border-radius:0 8px 8px 0;margin:20px 0;">
          <p style="margin:0 0 8px;"><strong>📅 Date :</strong> {date_str}</p>
          <p style="margin:0 0 8px;"><strong>🏥 Service :</strong> {specialite}</p>
          <p style="margin:0;"><strong>📍 Type :</strong> {type_label}</p>
        </div>
        <p style="color:#4a5a78;">Un rappel vous sera envoyé <strong>6 heures avant</strong> votre consultation.</p>
        <p style="color:#4a5a78;">Pour modifier ou annuler, appelez-nous au <strong>+509 3888-0000</strong>.</p>
        <div style="margin-top:24px;padding-top:20px;border-top:1px solid #e2e8f5;color:#8a9ab8;font-size:13px;text-align:center;">
          Clinique de la Rebecca · Haïti · contact@cliniquerebecca.ht
        </div>
      </div>
    </div>
    """


def email_confirmation_medecin(specialite: str, patient_nom: str, date_rdv: datetime, type_rdv: str, motif: str = "") -> str:
    date_str = format_date_fr(date_rdv)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      <div style="background:#1a2a4a;padding:24px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;margin:0;font-size:20px;">Nouveau rendez-vous</h1>
        <p style="color:rgba(255,255,255,0.7);margin:4px 0 0;">{specialite}</p>
      </div>
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:15px;">Un nouveau rendez-vous a été enregistré :</p>
        <div style="background:#f4f7fd;border-left:4px solid #1a4fc4;padding:16px;border-radius:0 8px 8px 0;margin:20px 0;">
          <p style="margin:0 0 8px;"><strong>👤 Patient :</strong> {patient_nom}</p>
          <p style="margin:0 0 8px;"><strong>📅 Date :</strong> {date_str}</p>
          <p style="margin:0 0 8px;"><strong>🏥 Service :</strong> {specialite}</p>
          <p style="margin:0 0 8px;"><strong>📍 Type :</strong> {"Vidéo" if type_rdv == "video" else "Présentiel"}</p>
          {f'<p style="margin:0;"><strong>📝 Motif :</strong> {motif}</p>' if motif else ''}
        </div>
        <p style="color:#4a5a78;">Connectez-vous au tableau de bord admin pour gérer ce rendez-vous.</p>
      </div>
    </div>
    """


def email_rappel(nom: str, specialite: str, date_rdv: datetime) -> str:
    date_str = format_date_fr(date_rdv)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      <div style="background:#e07a00;padding:24px;border-radius:12px 12px 0 0;text-align:center;">
        <h1 style="color:#fff;margin:0;font-size:20px;">⏰ Rappel de rendez-vous</h1>
      </div>
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#4a5a78;">Votre rendez-vous est dans <strong>6 heures</strong> :</p>
        <div style="background:#fff8f0;border-left:4px solid #e07a00;padding:16px;border-radius:0 8px 8px 0;margin:20px 0;">
          <p style="margin:0 0 8px;font-size:18px;font-weight:bold;">📅 {date_str}</p>
          <p style="margin:0;"><strong>🏥 {specialite}</strong></p>
        </div>
        <p style="color:#4a5a78;">Pour annuler : <strong>+509 3888-0000</strong></p>
      </div>
    </div>
    """


# ─── Fonction principale envoi RDV ───────────────────────────────────────────
async def notify_rdv_confirmed(rdv_data: dict):
    """Envoyer toutes les notifications à la confirmation d'un RDV"""
    tasks = []

    # Email patient
    if rdv_data.get("patient_email"):
        html = email_confirmation_patient(
            rdv_data["patient_nom"],
            rdv_data["specialite"],
            rdv_data["date_rdv"],
            rdv_data.get("type_rdv", "presentiel"),
        )
        tasks.append(send_email(
            rdv_data["patient_email"],
            f"✅ Confirmation RDV — {rdv_data['specialite']}",
            html,
        ))

    # Email médecin / admin
    html_med = email_confirmation_medecin(
        rdv_data["specialite"],
        rdv_data["patient_nom"],
        rdv_data["date_rdv"],
        rdv_data.get("type_rdv", "presentiel"),
        rdv_data.get("motif", ""),
    )
    tasks.append(send_email(
        settings.MEDECIN_EMAIL,
        f"📋 Nouveau RDV — {rdv_data['patient_nom']} ({rdv_data['specialite']})",
        html_med,
    ))
    tasks.append(send_email(
        settings.ADMIN_EMAIL,
        f"📋 Nouveau RDV — {rdv_data['patient_nom']}",
        html_med,
    ))

    await asyncio.gather(*tasks, return_exceptions=True)

    # Lien WhatsApp (log pour intégration future)
    wa_msg = (
        f"✅ Bonjour {rdv_data['patient_nom']}, votre RDV à la Clinique de la Rebecca "
        f"est confirmé : {rdv_data['specialite']} le {format_date_fr(rdv_data['date_rdv'])}. "
        f"Pour toute question : +509 3888-0000"
    )
    wa_link = get_whatsapp_link(rdv_data["patient_telephone"], wa_msg)
    logger.info("WhatsApp lien confirmation: %s", wa_link)
    return wa_link


async def notify_rdv_rappel(rdv_data: dict):
    """Rappel 6h avant le RDV"""
    tasks = []

    if rdv_data.get("patient_email"):
        html = email_rappel(
            rdv_data["patient_nom"],
            rdv_data["specialite"],
            rdv_data["date_rdv"],
        )
        tasks.append(send_email(
            rdv_data["patient_email"],
            f"⏰ Rappel : votre RDV dans 6h — {rdv_data['specialite']}",
            html,
        ))

    # Rappel médecin
    html_med = email_rappel(
        f"Patient : {rdv_data['patient_nom']}",
        rdv_data["specialite"],
        rdv_data["date_rdv"],
    )
    tasks.append(send_email(
        settings.MEDECIN_EMAIL,
        f"⏰ Rappel RDV dans 6h — {rdv_data['patient_nom']}",
        html_med,
    ))

    await asyncio.gather(*tasks, return_exceptions=True)

    wa_msg = (
        f"⏰ Rappel Clinique de la Rebecca : votre RDV ({rdv_data['specialite']}) "
        f"est dans 6 heures — {format_date_fr(rdv_data['date_rdv'])}. "
        f"Pour annuler : +509 3888-0000"
    )
    wa_link = get_whatsapp_link(rdv_data["patient_telephone"], wa_msg)
    logger.info("WhatsApp lien rappel: %s", wa_link)
