"""
Service de notifications : Email SMTP + WhatsApp
Flux :
  1. Patient prend RDV → admin + caisse + médecin notifiés
  2. Admin/caisse/médecin confirme → patient reçoit confirmation + lien vidéo si vidéo
  3. Rappel automatique 6h avant le RDV
"""
import asyncio
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from app.config import settings
import logging

logger = logging.getLogger(__name__)

# Les emails des médecins sont récupérés dynamiquement depuis la base de données
# via le champ `specialite` du modèle User. Pas de config manuelle nécessaire.


def format_date_fr(dt: datetime) -> str:
    jours = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
    mois  = ["janvier","février","mars","avril","mai","juin",
             "juillet","août","septembre","octobre","novembre","décembre"]
    return f"{jours[dt.weekday()]} {dt.day} {mois[dt.month-1]} {dt.year} à {dt.strftime('%H:%M')}"


# ─── Envoi email ─────────────────────────────────────────────────────────────
async def send_email(to: str, subject: str, html_body: str) -> bool:
    if not to:
        return False
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning("SMTP non configuré — email simulé → %s | %s", to, subject)
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
        logger.info("Email envoyé → %s", to)
        return True
    except Exception as e:
        logger.error("Erreur email → %s : %s", to, e)
        return False


# ─── WhatsApp link ────────────────────────────────────────────────────────────
def get_whatsapp_link(phone: str, message: str) -> str:
    import urllib.parse
    clean = phone.replace("+","").replace(" ","").replace("-","")
    if not clean.startswith("509"):
        clean = "509" + clean
    return f"https://wa.me/{clean}?text={urllib.parse.quote(message)}"


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATES EMAIL
# ══════════════════════════════════════════════════════════════════════════════

def _header(couleur: str, titre: str, sous_titre: str = "") -> str:
    return f"""
    <div style="background:{couleur};padding:28px 32px;border-radius:12px 12px 0 0;text-align:center;">
      <h1 style="color:#fff;margin:0;font-size:20px;">{titre}</h1>
      {f'<p style="color:rgba(255,255,255,0.75);margin:6px 0 0;font-size:14px;">{sous_titre}</p>' if sous_titre else ''}
    </div>"""

def _footer() -> str:
    return f"""
    <div style="margin-top:24px;padding-top:16px;border-top:1px solid #e2e8f5;
      color:#94a3b8;font-size:12px;text-align:center;">
      Clinique de la Rebecca · Pétion-Ville, Haïti · {settings.CLINIQUE_TELEPHONE}
    </div>"""

def _bloc_rdv(date_str: str, specialite: str, type_rdv: str,
              patient_nom: str = "", motif: str = "",
              mode_paiement: str = "", lien_video: str = "") -> str:
    type_label = "💻 Vidéo (en ligne)" if type_rdv == "video" else "🏥 En personne à la clinique"
    rows = [
        f"<p style='margin:0 0 8px;'><strong>📅 Date :</strong> {date_str}</p>",
        f"<p style='margin:0 0 8px;'><strong>🏥 Spécialité :</strong> {specialite}</p>",
        f"<p style='margin:0 0 8px;'><strong>📍 Type :</strong> {type_label}</p>",
    ]
    if patient_nom:
        rows.insert(0, f"<p style='margin:0 0 8px;'><strong>👤 Patient :</strong> {patient_nom}</p>")
    if medecin_nom:
        rows.append(f"<p style='margin:0 0 8px;'><strong>👨‍⚕️ Médecin choisi :</strong> {medecin_nom}</p>")
    if mode_paiement:
        rows.append(f"<p style='margin:0 0 8px;'><strong>💳 Paiement :</strong> {mode_paiement}</p>")
    if motif:
        rows.append(f"<p style='margin:0;'><strong>📝 Motif :</strong> {motif}</p>")
    bloc_video = ""
    if lien_video:
        bloc_video = f"""
        <div style="background:#1641C8;border-radius:10px;padding:16px;text-align:center;margin-top:16px;">
          <p style="color:rgba(255,255,255,0.8);margin:0 0 10px;font-size:13px;">Lien de la consultation vidéo :</p>
          <a href="{lien_video}" style="display:inline-block;background:#0d9488;color:#fff;
            text-decoration:none;padding:10px 24px;border-radius:8px;font-weight:bold;font-size:14px;">
            📹 Rejoindre la consultation
          </a>
          <p style="color:rgba(255,255,255,0.55);margin:10px 0 0;font-size:11px;">
            Aucune installation requise · Fonctionne sur Chrome et Firefox
          </p>
        </div>"""
    return f"""
    <div style="background:#f8fafc;border-left:4px solid #0d9488;
      padding:16px 18px;border-radius:0 10px 10px 0;margin:18px 0;">
      {''.join(rows)}
    </div>{bloc_video}"""


# ── 1. Email au PATIENT — confirmation prise de RDV ──────────────────────────
def email_patient_rdv_recu(nom: str, specialite: str, date_rdv: datetime,
                            type_rdv: str, mode_paiement: str = "") -> str:
    date_str   = format_date_fr(date_rdv)
    type_label = "vidéo" if type_rdv == "video" else "en cabinet"
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header('linear-gradient(135deg,#1641C8,#0d9488)',
               '✅ Demande de RDV reçue',
               'Clinique de la Rebecca')}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#475569;">
          Votre demande de rendez-vous <strong>{type_label}</strong> a bien été enregistrée.
          Notre équipe va vérifier la disponibilité de votre médecin et confirmer votre RDV très prochainement.
        </p>
        {_bloc_rdv(date_str, specialite, type_rdv, mode_paiement=mode_paiement)}
        <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;
          padding:14px;margin-top:16px;">
          <p style="margin:0;color:#92400e;font-size:13px;">
            ⏳ <strong>Prochaine étape :</strong> Vous recevrez une confirmation avec
            {'le lien vidéo ' if type_rdv == 'video' else 'les détails '}
            dès que votre médecin et la caisse auront validé votre rendez-vous.
          </p>
        </div>
        <p style="color:#64748b;font-size:13px;margin-top:20px;">
          Questions ? Appelez-nous au <strong>{settings.CLINIQUE_TELEPHONE}</strong>
        </p>
        {_footer()}
      </div>
    </div>"""


# ── 2. Email à ADMIN + CAISSE + MÉDECIN — nouveau RDV à traiter ─────────────
def email_interne_nouveau_rdv(patient_nom: str, patient_telephone: str,
                               patient_email: str, specialite: str,
                               date_rdv: datetime, type_rdv: str,
                               motif: str = "", mode_paiement: str = "",
                               reference_paiement: str = "",
                               medecin_nom: str = "",
                               destinataire: str = "admin") -> str:
    date_str = format_date_fr(date_rdv)
    labels = {
        "admin":   ("👨‍💼 Nouveau rendez-vous", "#0f1e3d", "Action requise : confirmer ou réassigner"),
        "caisse":  ("💳 Nouveau RDV — Vérifier paiement", "#d97706", "Vérifiez le paiement puis confirmez le RDV"),
        "medecin": ("📋 Nouveau patient — Confirmer disponibilité", "#1641C8", "Vérifiez votre disponibilité et confirmez"),
    }
    titre, couleur, sous = labels.get(destinataire, labels["admin"])

    paiement_info = ""
    if mode_paiement and mode_paiement.lower() not in ["à la clinique", "especes"]:
        paiement_info = f"""
        <div style="background:#fef9c3;border:1px solid #fde047;border-radius:10px;padding:14px;margin-bottom:16px;">
          <p style="margin:0;color:#713f12;font-size:13px;">
            💳 <strong>Paiement mobile déclaré :</strong> {mode_paiement}
            {f'<br/>🔑 Référence : <strong>{reference_paiement}</strong>' if reference_paiement else ''}
            <br/>➡️ <strong>Vérifiez ce paiement avant de confirmer le RDV.</strong>
          </p>
        </div>"""

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header(couleur, titre, sous)}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:15px;">
          Un nouveau rendez-vous vient d'être soumis et nécessite votre action.
        </p>
        {_bloc_rdv(date_str, specialite, type_rdv, patient_nom=patient_nom,
                   motif=motif, mode_paiement=mode_paiement)}
        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;
          padding:14px;margin-bottom:16px;">
          <p style="margin:0 0 6px;color:#0369a1;font-size:13px;">
            <strong>📞 Contact patient</strong>
          </p>
          <p style="margin:0;color:#0369a1;font-size:13px;">
            Téléphone : <strong>{patient_telephone}</strong>
            {f'<br/>Email : {patient_email}' if patient_email else ''}
          </p>
        </div>
        {paiement_info}
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;
          padding:14px;margin-bottom:20px;">
          <p style="margin:0;color:#14532d;font-size:13px;">
            ✅ <strong>Action requise :</strong> Connectez-vous au tableau de bord,
            vérifiez le paiement{' avec la caisse' if destinataire == 'medecin' else ''},
            {'confirmez votre disponibilité ' if destinataire == 'medecin' else 'puis '}
            et changez le statut du RDV en <strong>"Confirmé"</strong>.
            <br/>Le patient recevra automatiquement sa confirmation
            {'avec le lien vidéo ' if type_rdv == 'video' else ''}.
          </p>
        </div>
        {_footer()}
      </div>
    </div>"""


# ── 3. Email au PATIENT — RDV confirmé (+ lien vidéo si vidéo) ───────────────
def email_patient_rdv_confirme(nom: str, specialite: str, date_rdv: datetime,
                                type_rdv: str, lien_video: str = "") -> str:
    date_str = format_date_fr(date_rdv)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header('linear-gradient(135deg,#0d9488,#059669)',
               '✅ Rendez-vous confirmé !',
               'Clinique de la Rebecca')}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#475569;">
          Votre médecin a confirmé sa disponibilité et votre paiement a été vérifié.
          Votre rendez-vous est <strong>officiellement confirmé</strong>.
        </p>
        {_bloc_rdv(date_str, specialite, type_rdv, lien_video=lien_video)}
        {'<div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;padding:14px;margin-top:16px;"><p style="margin:0;color:#92400e;font-size:13px;">💡 <strong>Conseil :</strong> Testez votre caméra et microphone 5 minutes avant. Utilisez Chrome ou Firefox.</p></div>' if lien_video else ''}
        <p style="color:#64748b;font-size:13px;margin-top:20px;">
          Pour modifier ou annuler : <strong>{settings.CLINIQUE_TELEPHONE}</strong>
        </p>
        {_footer()}
      </div>
    </div>"""


# ── 4. Email rappel 6h avant ──────────────────────────────────────────────────
def email_rappel(nom: str, specialite: str, date_rdv: datetime,
                 type_rdv: str = "presentiel", lien_video: str = "") -> str:
    date_str = format_date_fr(date_rdv)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#f4f7fd;padding:20px;">
      {_header('#d97706', '⏰ Rappel — Votre RDV dans 6 heures', 'Clinique de la Rebecca')}
      <div style="background:#fff;padding:28px;border-radius:0 0 12px 12px;">
        <p style="color:#0f1e3d;font-size:16px;">Bonjour <strong>{nom}</strong>,</p>
        <p style="color:#475569;">Votre rendez-vous est dans <strong>6 heures</strong>. Ne l'oubliez pas !</p>
        {_bloc_rdv(date_str, specialite, type_rdv, lien_video=lien_video)}
        <p style="color:#64748b;font-size:13px;margin-top:20px;">
          Pour annuler : <strong>{settings.CLINIQUE_TELEPHONE}</strong>
        </p>
        {_footer()}
      </div>
    </div>"""


# ══════════════════════════════════════════════════════════════════════════════
# FONCTIONS D'ENVOI PRINCIPALES
# ══════════════════════════════════════════════════════════════════════════════

async def notify_rdv_confirmed(rdv_data: dict):
    """
    Appelé à la CRÉATION d'un RDV.
    Envoie :
      - Au patient : accusé de réception
      - À l'admin : nouveau RDV à traiter
      - À la caisse : vérifier le paiement
      - Au médecin concerné : confirmer disponibilité
    """
    patient_nom       = rdv_data.get("patient_nom", "")
    patient_telephone = rdv_data.get("patient_telephone", "")
    patient_email     = rdv_data.get("patient_email", "")
    specialite        = rdv_data.get("specialite", "")
    date_rdv          = rdv_data.get("date_rdv")
    type_rdv          = rdv_data.get("type_rdv", "presentiel")
    motif             = rdv_data.get("motif", "")
    mode_paiement     = rdv_data.get("mode_paiement", "")
    reference         = rdv_data.get("reference_paiement", "")

    tasks = []

    # 1. Accusé de réception au patient
    if patient_email:
        tasks.append(send_email(
            patient_email,
            f"📋 Demande de RDV reçue — {specialite}",
            email_patient_rdv_recu(patient_nom, specialite, date_rdv,
                                   type_rdv, mode_paiement),
        ))

    medecin_nom_rdv = rdv_data.get("medecin_nom", "")

    # 2. Notification admin
    tasks.append(send_email(
        settings.ADMIN_EMAIL,
        f"📋 Nouveau RDV — {patient_nom} ({specialite})",
        email_interne_nouveau_rdv(
            patient_nom, patient_telephone, patient_email,
            specialite, date_rdv, type_rdv, motif, mode_paiement, reference,
            medecin_nom=medecin_nom_rdv,
            destinataire="admin"
        ),
    ))

    # 3. Notification caisse — vérifier le paiement
    tasks.append(send_email(
        settings.CAISSE_EMAIL,
        f"💳 Paiement à vérifier — {patient_nom} ({mode_paiement or 'À la clinique'})",
        email_interne_nouveau_rdv(
            patient_nom, patient_telephone, patient_email,
            specialite, date_rdv, type_rdv, motif, mode_paiement, reference,
            medecin_nom=medecin_nom_rdv,
            destinataire="caisse"
        ),
    ))

    # 4. Notification à TOUS les médecins ayant cette spécialité (depuis la DB)
    medecins_emails = rdv_data.get("medecins_emails", [])
    if not medecins_emails:
        # Fallback si aucun médecin trouvé en DB
        medecins_emails = [settings.MEDECIN_EMAIL]

    medecin_nom_rdv = rdv_data.get("medecin_nom", "")

    html_medecin = email_interne_nouveau_rdv(
        patient_nom, patient_telephone, patient_email,
        specialite, date_rdv, type_rdv, motif, mode_paiement, reference,
        medecin_nom=medecin_nom_rdv,
        destinataire="medecin"
    )
    for med_email in medecins_emails:
        tasks.append(send_email(
            med_email,
            f"📋 Nouveau patient — {patient_nom} · {specialite}",
            html_medecin,
        ))

    await asyncio.gather(*tasks, return_exceptions=True)

    # WhatsApp accusé de réception au patient
    type_label = "vidéo" if type_rdv == "video" else "en cabinet"
    wa_msg = (
        f"✅ Bonjour {patient_nom}, votre demande de RDV {type_label} "
        f"({specialite}) à la Clinique de la Rebecca a bien été reçue. "
        f"Vous recevrez une confirmation sous peu. "
        f"Questions : {settings.CLINIQUE_TELEPHONE}"
    )
    wa_link = get_whatsapp_link(patient_telephone, wa_msg)
    logger.info("WhatsApp accusé patient: %s", wa_link)


async def notify_rdv_video_confirme(rdv_data: dict):
    """
    Appelé quand le statut d'un RDV passe à 'confirmé'.
    Envoie au patient :
      - Si vidéo : confirmation + lien Jitsi Meet
      - Si présentiel : confirmation standard
    """
    patient_nom       = rdv_data.get("patient_nom", "")
    patient_telephone = rdv_data.get("patient_telephone", "")
    patient_email     = rdv_data.get("patient_email", "")
    specialite        = rdv_data.get("specialite", "")
    date_rdv          = rdv_data.get("date_rdv")
    type_rdv          = rdv_data.get("type_rdv", "presentiel")
    lien_video        = rdv_data.get("lien_video", "")

    tasks = []

    # Email de confirmation au patient (avec lien vidéo si applicable)
    if patient_email:
        tasks.append(send_email(
            patient_email,
            f"✅ RDV confirmé — {specialite}" + (" · Votre lien vidéo" if lien_video else ""),
            email_patient_rdv_confirme(patient_nom, specialite, date_rdv,
                                       type_rdv, lien_video),
        ))

    await asyncio.gather(*tasks, return_exceptions=True)

    # WhatsApp au patient
    if lien_video:
        wa_msg = (
            f"✅ Bonjour {patient_nom}, votre consultation vidéo ({specialite}) "
            f"est confirmée !\n\n"
            f"📅 {format_date_fr(date_rdv)}\n\n"
            f"🔗 Votre lien vidéo :\n{lien_video}\n\n"
            f"Cliquez à l'heure du RDV. Aucune installation requise.\n"
            f"Questions : {settings.CLINIQUE_TELEPHONE}"
        )
    else:
        wa_msg = (
            f"✅ Bonjour {patient_nom}, votre RDV ({specialite}) "
            f"à la Clinique de la Rebecca est confirmé : "
            f"{format_date_fr(date_rdv)}. "
            f"Questions : {settings.CLINIQUE_TELEPHONE}"
        )

    wa_link = get_whatsapp_link(patient_telephone, wa_msg)
    logger.info("WhatsApp confirmation patient: %s", wa_link)


async def notify_rdv_rappel(rdv_data: dict):
    """Rappel automatique 6h avant le RDV."""
    tasks = []
    if rdv_data.get("patient_email"):
        tasks.append(send_email(
            rdv_data["patient_email"],
            f"⏰ Rappel RDV dans 6h — {rdv_data['specialite']}",
            email_rappel(
                rdv_data["patient_nom"], rdv_data["specialite"],
                rdv_data["date_rdv"], rdv_data.get("type_rdv", "presentiel"),
                rdv_data.get("lien_video", ""),
            ),
        ))
    await asyncio.gather(*tasks, return_exceptions=True)
    wa_msg = (
        f"⏰ Rappel Clinique de la Rebecca : votre RDV ({rdv_data['specialite']}) "
        f"est dans 6 heures — {format_date_fr(rdv_data['date_rdv'])}. "
        f"Pour annuler : {settings.CLINIQUE_TELEPHONE}"
    )
    logger.info("WhatsApp rappel: %s",
                get_whatsapp_link(rdv_data["patient_telephone"], wa_msg))


async def envoyer_email_activation(email: str, nom: str, activated: bool, motif: str = ""):
    """Email de confirmation d'activation ou de rejet de compte"""
    import aiosmtplib
    from email.mime.text import MIMEText
    from app.config import settings
    import os

    if activated:
        sujet = "✅ Votre compte Clinique de la Rebecca est activé"
        corps = f"""
Bonjour {nom},

Votre compte a été activé par l'administrateur de la Clinique de la Rebecca.
Vous pouvez maintenant vous connecter à : https://clinique-rebecca-frontend.vercel.app/login

Cordialement,
L'équipe administrative — Clinique de la Rebecca
(509) 4858-5757
"""
    else:
        sujet = "❌ Demande de compte refusée — Clinique de la Rebecca"
        corps = f"""
Bonjour {nom},

Votre demande de compte a été refusée.
Motif : {motif or 'Non conforme aux critères d\'admission'}

Pour toute question, contactez l'administration :
(509) 4858-5757 | admin@cliniquerebecca.ht

Cordialement,
L'équipe administrative — Clinique de la Rebecca
"""

    try:
        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        
        if not smtp_user:
            return  # SMTP not configured
            
        msg = MIMEText(corps, "plain", "utf-8")
        msg["From"] = f"Clinique de la Rebecca <{smtp_user}>"
        msg["To"] = email
        msg["Subject"] = sujet

        async with aiosmtplib.SMTP(hostname=smtp_host, port=smtp_port) as smtp:
            await smtp.login(smtp_user, smtp_pass)
            await smtp.send_message(msg)
    except Exception as e:
        print(f"Email notification error: {e}")


async def notifier_medecin_valeur_critique(medecin_email: str, patient_id: str,
                                            examen: str, valeur: str):
    """Alerte email au médecin prescripteur pour valeur critique"""
    import aiosmtplib
    from email.mime.text import MIMEText
    import os

    sujet = f"🚨 ALERTE — Valeur critique détectée : {examen}"
    corps = f"""
ALERTE MÉDICALE URGENTE

Un résultat de laboratoire avec une valeur critique a été détecté.

Patient ID : {patient_id}
Examen     : {examen}
Valeur     : {valeur}

Veuillez consulter le dossier patient immédiatement.

Clinique de la Rebecca — Système d'alertes automatiques
"""
    try:
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        if not smtp_user or not medecin_email:
            return
            
        msg = MIMEText(corps, "plain", "utf-8")
        msg["From"] = f"Alertes Clinique Rebecca <{smtp_user}>"
        msg["To"] = medecin_email
        msg["Subject"] = sujet

        async with aiosmtplib.SMTP(hostname="smtp.gmail.com", port=587) as smtp:
            await smtp.login(smtp_user, smtp_pass)
            await smtp.send_message(msg)
    except Exception as e:
        print(f"Alert email error: {e}")

