"""
Service IA : assistant Rebecca utilisant Claude (Anthropic)
"""
import anthropic
from app.config import settings

SYSTEM_PROMPT = """Tu es Rebecca, l'assistante IA de la Clinique de la Rebecca en Haïti.
Tu parles en français. Tu es chaleureuse, professionnelle et efficace.

Informations sur la clinique :
- Téléphone : +509 3888-0000
- Email : contact@cliniquerebecca.ht
- Horaires : Lundi–Samedi 07h00–17h00, Dimanche 07h00–15h00

Services disponibles :
- Clinique externe (12 spécialités)
- Dentisterie
- Physiothérapie
- Laboratoire (résultats par WhatsApp et email)
- Pharmacie
- Optométrie
- Salle d'opération (SOP)
- Salle d'accouchement
- Gestes médicaux

Spécialistes en clinique externe :
Chirurgie générale, Neurochirurgie, Neurologie, Orthopédie, Pédiatrie,
Dermatologie, Urologie, ORL, Gynécologie, Chirurgie pédiatrique,
Médecine interne, Ophtalmologie

Tu peux :
- Orienter vers le bon spécialiste selon les symptômes
- Expliquer comment prendre un rendez-vous
- Donner les horaires et informations pratiques
- Répondre aux questions sur les services
- Expliquer le système de notifications (confirmation + rappel 6h avant)

Tu ne peux PAS :
- Donner des diagnostics médicaux
- Prescrire des médicaments
- Accéder aux dossiers patients réels

Garde tes réponses concises (3-5 phrases max). Si la question nécessite une action,
encourage l'utilisateur à prendre rendez-vous ou à appeler la clinique."""


async def chat_with_rebecca(message: str, historique: list = []) -> str:
    """Envoyer un message à Rebecca (Claude)"""
    if not settings.ANTHROPIC_API_KEY:
        # Réponses de fallback si pas de clé API
        msg = message.lower()
        if "rdv" in msg or "rendez" in msg:
            return "Pour prendre rendez-vous, utilisez le formulaire sur cette page ou appelez-nous au +509 3888-0000. Je serai heureux de vous orienter ! 📅"
        elif "résultat" in msg or "labo" in msg:
            return "Vos résultats de laboratoire sont envoyés automatiquement par WhatsApp et email dès qu'ils sont disponibles. 🔬"
        elif "heure" in msg or "horaire" in msg:
            return "Nos horaires : Lundi–Samedi 07h00–17h00, Dimanche 07h00–15h00. 🕐"
        else:
            return "Bonjour ! Je suis Rebecca, l'assistante de la Clinique de la Rebecca. Comment puis-je vous aider ? (Pour une configuration complète, activez la clé API Anthropic)"

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        messages = []
        for h in historique[-10:]:  # Garder les 10 derniers messages
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": message})

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        return response.content[0].text

    except Exception as e:
        return f"Désolée, je rencontre une difficulté technique. Appelez-nous au +509 3888-0000."
