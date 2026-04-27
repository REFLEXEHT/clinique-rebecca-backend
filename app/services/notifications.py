"""
Service de notifications : Email SMTP + WhatsApp
Corrections v2 :
  - Bug medecin_nom corrigé dans _bloc_rdv (variable non définie)
  - Templates sans emoji pour design moderne
  - Lien vidéo inclus dans les rappels
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
    jours = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
    mois  = ["janvier","février","mars","avril","mai","juin",
             "juillet","août","septembre","octobre","novembre","décembre"]
    return f"{jours[dt.weekday()]} {dt.day} {mois[dt.month-1]} {dt.year} à {dt.strftime('%H:%M')}"


async def send_email(to: str, subject: str, html_body: str) -> bool:
    if not to:
        return False
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.info("SMTP non configuré — email simulé vers %s | Sujet: %s", to, subject)
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Clinique de la Rebecca <{settings.SMTP_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info("Email envoyé vers %s", to)
        return True
    except Exception as e:
        logger.error("Erreur email vers %s : %s", to, e)
        return False


def get_whatsapp_link(phone: str, message: str) -> str:
    import urllib.parse
    clean = phone.replace("+","").replace(" ","").replace("-","")
    if not clean.startswith("509"):
        clean = "509" + clean
    return f"https://wa.me/{clean}?text={urllib.parse.quote(message)}"


# ── Bloc HTML commun ──────────────────────────────────────────────────────────
def _header(couleur: str, titre: str, sous_titre: str = "") -> str:
    return f"""
    <div style="background:{couleur};padding:28px 32px;border-radius:12px 12px 0 0;text-align:center;">
      <h1 style="color:#fff;margin:0;font-size:20px;font-family:Arial,sans-serif;">{titre}</h1>
      {f'<p style="color:rgba(255,255,255,0.8);margin:6px 0 0;font-size:14px;">{sous_titre}</p>' if sous_titre else ''}
    </div>"""


def _footer() -> str:
    return f"""
    <div style="margin-top:24px;padding-top:16px;border-top:1px solid #e2e8f5;
      color:#94a3b8;font-size:12px;text-align:center;font-family:Arial,sans-serif;">
      Clinique de la Rebecca &middot; Haïti &middot; {settings.CLINIQUE_TELEPHONE}
    </div>"""


def _bloc_rdv(
    date_str: str,
    specialite: str,
    type_rdv: str,
    patient_nom: str = "",
    medecin_nom: str = "",   # FIX: paramètre explicite
    motif: str = "",
    mode_paiement: str = "",
    lien_video: str = "",
) -> str:
    """Bloc récapitulatif RDV — medecin_nom maintenant paramètre explicite (bug corrigé)."""
    type_label = "Par vidéo (en ligne)" if type_rdv == "video" else "En personne à la clinique"
    rows = [
        f"<p style='margin:0 0 8px;font-family:Arial,sans-serif;'><strong>Date :</strong> {date_str}</p>",
        f"<p style='margin:0 0 8px;font-family:Arial,sans-serif;'><strong>Spécialité :</strong> {specialite}</p>",
        f"<p style='margin:0 0 8px;font-family:Arial,sans-serif;'><strong>Type :</strong> {type_label}</p>",
    ]
    if patient_nom:
        rows.insert(0, f"<p style='margin:0 0 8px;font-family:Arial,sans-serif;'><strong>Patient :</strong> {patient_nom}</p>")
    if medecin_nom:
        rows.append(f"<p style='margin:0 0 8px;font-family:Arial,sans-serif;'><strong>Médecin :</strong> {medecin_nom}</p>")
    if mode_paiement:
        rows.append(f"<p style='margin:0 0 8px;font-family:Arial,sans-serif;'><strong>Paiement :</strong> {mode_paiement}</p>")
    if motif:
        rows.append(f"<p style='margin:0;font-family:Arial,sans-serif;'><strong>Motif :</strong> {motif}</p>")

    bloc_video = ""
    if lien_video:
        bloc_video = f"""
        <div style="background:#1641C8;border-radius:10px;padding:16px;text-align:center;margin-top:16px;">
          <p style="color:rgba(255,255,255,0.85);margin:0 0 10px;font-size:13px;font-family:Arial,sans-serif;">
            Lien de votre consultation vidéo :
          </p>
          <a href="{lien_video}" style="display:inline-block;background:#0d9488;color:#fff;
            text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:bold;font-size:15px;font-family:Arial,sans-serif;">
            Rejoindre la consultation
          </a>
          <p style="color:rgba(255,255,255,0.6);margin:10px 0 0;font-size:11px;font-family:Arial,sans-serif;">
            Aucune installation requise · Compatible Chrome et Firefox
          </p>
        </div>"""

    return f"""
    <div style="background:#f8fafc;border-left:4px solid #0d9488;
      padding:16px 18px;border-radius:0 10px 10px 0;margin:18px 0;font-family:Arial,sans-serif;">
      {'\n'.join(rows)}
    </div>{bloc_video}"""


# ── Templates ─────────────────────────────────────────────────────────────────

def email_patient_rdv_recu(
    nom: str, specialite: str, date_rdv: datetime,
    type_rdv: str, mode_paiement: str = "", medecin_nom: str = "",
) -> str:
    date_str   = format_date_fr(date_rdv)
    type_label = "vidéo" if type_rdv == "video" else "en cabinet"
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header('linear-gradient(135deg,#1641C8,#0d9488)',
               'Demande de rendez-vous reçue',
               'Clinique de la Rebecca')}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#475569;line-height:1.6;">
          Votre demande de rendez-vous <strong>{type_label}</strong> a bien été enregistrée.
          Notre équipe vérifiera la disponibilité et vous confirmera votre rendez-vous sous peu.
        </p>
        {_bloc_rdv(date_str, specialite, type_rdv, medecin_nom=medecin_nom, mode_paiement=mode_paiement)}
        <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;padding:14px;margin-top:16px;">
          <p style="margin:0;color:#92400e;font-size:13px;">
            <strong>Prochaine étape :</strong> Vous recevrez une confirmation avec
            {'le lien vidéo ' if type_rdv == 'video' else 'les détails '}
            dès validation par notre équipe.
          </p>
        </div>
        <p style="color:#64748b;font-size:13px;margin-top:20px;">
          Questions ? Appelez-nous au <strong>{settings.CLINIQUE_TELEPHONE}</strong>
        </p>
        {_footer()}
      </div>
    </div>"""


def email_interne_nouveau_rdv(
    patient_nom: str, patient_telephone: str, patient_email: str,
    specialite: str, date_rdv: datetime, type_rdv: str,
    motif: str = "", mode_paiement: str = "", reference_paiement: str = "",
    medecin_nom: str = "", destinataire: str = "admin",
) -> str:
    date_str = format_date_fr(date_rdv)
    labels = {
        "admin":   ("Nouveau rendez-vous", "#0f1e3d", "Action requise : confirmer ou réassigner"),
        "caisse":  ("Nouveau RDV — Vérifier paiement", "#d97706", "Vérifiez le paiement avant confirmation"),
        "medecin": ("Nouveau patient", "#1641C8", "Confirmez votre disponibilité"),
    }
    titre, couleur, sous = labels.get(destinataire, labels["admin"])

    paiement_info = ""
    if mode_paiement and mode_paiement.lower() not in ["à la clinique", "especes", "espèces"]:
        paiement_info = f"""
        <div style="background:#fef9c3;border:1px solid #fde047;border-radius:10px;padding:14px;margin-bottom:16px;">
          <p style="margin:0;color:#713f12;font-size:13px;">
            <strong>Paiement mobile déclaré :</strong> {mode_paiement}
            {f'<br/>Référence : <strong>{reference_paiement}</strong>' if reference_paiement else ''}
            <br/>Vérifiez ce paiement avant de confirmer le RDV.
          </p>
        </div>"""

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header(couleur, titre, sous)}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:15px;">Un nouveau rendez-vous vient d'être soumis.</p>
        {_bloc_rdv(date_str, specialite, type_rdv, patient_nom=patient_nom,
                   medecin_nom=medecin_nom, motif=motif, mode_paiement=mode_paiement)}
        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:14px;margin-bottom:16px;">
          <p style="margin:0;color:#0369a1;font-size:13px;">
            <strong>Contact patient</strong><br/>
            Téléphone : <strong>{patient_telephone}</strong>
            {f'<br/>Email : {patient_email}' if patient_email else ''}
          </p>
        </div>
        {paiement_info}
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:14px;">
          <p style="margin:0;color:#14532d;font-size:13px;">
            <strong>Action requise :</strong> Connectez-vous au tableau de bord et passez
            le statut du RDV en <strong>Confirmé</strong>.
            Le patient recevra automatiquement la confirmation.
          </p>
        </div>
        {_footer()}
      </div>
    </div>"""


def email_patient_rdv_confirme(
    nom: str, specialite: str, date_rdv: datetime,
    type_rdv: str, lien_video: str = "", medecin_nom: str = "",
) -> str:
    date_str = format_date_fr(date_rdv)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header('linear-gradient(135deg,#0d9488,#059669)',
               'Rendez-vous confirmé',
               'Clinique de la Rebecca')}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#475569;line-height:1.6;">
          Votre rendez-vous est <strong>officiellement confirmé</strong>.
          Votre médecin a validé sa disponibilité.
        </p>
        {_bloc_rdv(date_str, specialite, type_rdv, medecin_nom=medecin_nom, lien_video=lien_video)}
        {'<div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;padding:14px;margin-top:16px;"><p style="margin:0;color:#92400e;font-size:13px;">Conseil : Testez votre caméra et microphone 5 minutes avant. Utilisez Chrome ou Firefox.</p></div>' if lien_video else ''}
        <p style="color:#64748b;font-size:13px;margin-top:20px;">
          Pour modifier ou annuler : <strong>{settings.CLINIQUE_TELEPHONE}</strong>
        </p>
        {_footer()}
      </div>
    </div>"""


def email_rappel(
    nom: str, specialite: str, date_rdv: datetime,
    type_rdv: str = "presentiel", lien_video: str = "", medecin_nom: str = "",
) -> str:
    date_str = format_date_fr(date_rdv)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header('#d97706', 'Rappel — Votre rendez-vous dans 6 heures', 'Clinique de la Rebecca')}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#475569;">Votre rendez-vous est dans <strong>6 heures</strong>.</p>
        {_bloc_rdv(date_str, specialite, type_rdv, medecin_nom=medecin_nom, lien_video=lien_video)}
        <p style="color:#64748b;font-size:13px;margin-top:20px;">
          Pour annuler : <strong>{settings.CLINIQUE_TELEPHONE}</strong>
        </p>
        {_footer()}
      </div>
    </div>"""


def email_resultat_labo_disponible(
    patient_nom: str, type_examen: str,
    caisse_email: str = "", admin_email: str = "",
) -> str:
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header('#0d9488', 'Résultat disponible', 'Laboratoire — Clinique de la Rebecca')}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:15px;">
          Le résultat pour <strong>{type_examen}</strong> est disponible pour le patient
          <strong>{patient_nom}</strong>.
        </p>
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:14px;margin:16px 0;">
          <p style="margin:0;color:#14532d;font-size:13px;">
            <strong>Action requise :</strong> Le caissier peut maintenant imprimer,
            envoyer par email ou WhatsApp le résultat au patient.
          </p>
        </div>
        {_footer()}
      </div>
    </div>"""


# ══════════════════════════════════════════════════════════════════════════════
# FONCTIONS D'ENVOI
# ══════════════════════════════════════════════════════════════════════════════

async def notify_rdv_confirmed(rdv_data: dict):
    """Appelé à la CRÉATION d'un RDV."""
    patient_nom       = rdv_data.get("patient_nom", "")
    patient_telephone = rdv_data.get("patient_telephone", "")
    patient_email     = rdv_data.get("patient_email", "")
    specialite        = rdv_data.get("specialite", "")
    date_rdv          = rdv_data.get("date_rdv")
    type_rdv          = rdv_data.get("type_rdv", "presentiel")
    motif             = rdv_data.get("motif", "")
    mode_paiement     = rdv_data.get("mode_paiement", "")
    reference         = rdv_data.get("reference_paiement", "")
    medecin_nom       = rdv_data.get("medecin_nom", "")

    tasks = []

    if patient_email:
        tasks.append(send_email(
            patient_email,
            f"Demande de RDV reçue — {specialite}",
            email_patient_rdv_recu(patient_nom, specialite, date_rdv, type_rdv,
                                   mode_paiement, medecin_nom),
        ))

    tasks.append(send_email(
        settings.ADMIN_EMAIL,
        f"Nouveau RDV — {patient_nom} ({specialite})",
        email_interne_nouveau_rdv(
            patient_nom, patient_telephone, patient_email, specialite,
            date_rdv, type_rdv, motif, mode_paiement, reference,
            medecin_nom=medecin_nom, destinataire="admin"
        ),
    ))

    tasks.append(send_email(
        settings.CAISSE_EMAIL,
        f"Paiement à vérifier — {patient_nom} ({mode_paiement or 'À la clinique'})",
        email_interne_nouveau_rdv(
            patient_nom, patient_telephone, patient_email, specialite,
            date_rdv, type_rdv, motif, mode_paiement, reference,
            medecin_nom=medecin_nom, destinataire="caisse"
        ),
    ))

    medecins_emails = rdv_data.get("medecins_emails", [])
    if not medecins_emails:
        medecins_emails = [settings.MEDECIN_EMAIL]

    html_medecin = email_interne_nouveau_rdv(
        patient_nom, patient_telephone, patient_email, specialite,
        date_rdv, type_rdv, motif, mode_paiement, reference,
        medecin_nom=medecin_nom, destinataire="medecin"
    )
    for med_email in medecins_emails:
        tasks.append(send_email(
            med_email,
            f"Nouveau patient — {patient_nom} · {specialite}",
            html_medecin,
        ))

    await asyncio.gather(*tasks, return_exceptions=True)

    type_label = "vidéo" if type_rdv == "video" else "en cabinet"
    wa_msg = (
        f"Bonjour {patient_nom}, votre demande de RDV {type_label} "
        f"({specialite}) à la Clinique de la Rebecca a bien été reçue. "
        f"Vous recevrez une confirmation sous peu. "
        f"Questions : {settings.CLINIQUE_TELEPHONE}"
    )
    wa_link = get_whatsapp_link(patient_telephone, wa_msg)
    logger.info("WhatsApp patient: %s", wa_link)


async def notify_rdv_video_confirme(rdv_data: dict):
    """Appelé quand statut passe à 'confirmé'."""
    patient_nom       = rdv_data.get("patient_nom", "")
    patient_telephone = rdv_data.get("patient_telephone", "")
    patient_email     = rdv_data.get("patient_email", "")
    specialite        = rdv_data.get("specialite", "")
    date_rdv          = rdv_data.get("date_rdv")
    type_rdv          = rdv_data.get("type_rdv", "presentiel")
    lien_video        = rdv_data.get("lien_video", "")
    medecin_nom       = rdv_data.get("medecin_nom", "")

    tasks = []
    if patient_email:
        tasks.append(send_email(
            patient_email,
            f"RDV confirmé — {specialite}" + (" · Votre lien vidéo" if lien_video else ""),
            email_patient_rdv_confirme(patient_nom, specialite, date_rdv,
                                       type_rdv, lien_video, medecin_nom),
        ))
    await asyncio.gather(*tasks, return_exceptions=True)

    if lien_video:
        wa_msg = (
            f"Bonjour {patient_nom}, votre consultation vidéo ({specialite}) "
            f"est confirmée !\n\n"
            f"Date : {format_date_fr(date_rdv)}\n\n"
            f"Lien vidéo :\n{lien_video}\n\n"
            f"Cliquez à l'heure du RDV. Questions : {settings.CLINIQUE_TELEPHONE}"
        )
    else:
        wa_msg = (
            f"Bonjour {patient_nom}, votre RDV ({specialite}) "
            f"à la Clinique de la Rebecca est confirmé : "
            f"{format_date_fr(date_rdv)}. "
            f"Questions : {settings.CLINIQUE_TELEPHONE}"
        )
    wa_link = get_whatsapp_link(patient_telephone, wa_msg)
    logger.info("WhatsApp confirmation: %s", wa_link)


async def notify_rdv_rappel(rdv_data: dict):
    """Rappel automatique 6h avant le RDV — FIX: inclut type_rdv et lien_video."""
    tasks = []
    if rdv_data.get("patient_email"):
        tasks.append(send_email(
            rdv_data["patient_email"],
            f"Rappel RDV dans 6h — {rdv_data['specialite']}",
            email_rappel(
                rdv_data["patient_nom"],
                rdv_data["specialite"],
                rdv_data["date_rdv"],
                rdv_data.get("type_rdv", "presentiel"),
                rdv_data.get("lien_video", ""),
                rdv_data.get("medecin_nom", ""),
            ),
        ))
    await asyncio.gather(*tasks, return_exceptions=True)
    wa_msg = (
        f"Rappel Clinique de la Rebecca : votre RDV ({rdv_data['specialite']}) "
        f"est dans 6 heures — {format_date_fr(rdv_data['date_rdv'])}. "
        f"Pour annuler : {settings.CLINIQUE_TELEPHONE}"
    )
    logger.info("WhatsApp rappel: %s", get_whatsapp_link(rdv_data["patient_telephone"], wa_msg))


async def notify_resultat_labo(patient_nom: str, type_examen: str):
    """Notification résultat labo disponible — envoi admin + caissier."""
    tasks = [
        send_email(
            settings.ADMIN_EMAIL,
            f"Résultat labo disponible — {patient_nom} ({type_examen})",
            email_resultat_labo_disponible(patient_nom, type_examen),
        ),
        send_email(
            settings.CAISSE_EMAIL,
            f"Résultat labo à transmettre — {patient_nom}",
            email_resultat_labo_disponible(patient_nom, type_examen),
        ),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Notifications résultat labo envoyées pour %s", patient_nom)
