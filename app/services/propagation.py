"""
propagation.py — Service de propagation des modifications en cascade
Clinique de la Rebecca v2.1

Principe :
  - Toute modification d'une entité maîtresse (médecin, service, tarif, utilisateur)
    déclenche automatiquement la mise à jour de toutes les entités liées.
  - Les écritures comptables PASSÉES (Mouvement) sont immutables (règle PCN).
  - Seuls les RDV non terminés et les données d'affichage sont propagés.
  - Un journal d'audit trace chaque propagation (what changed, by whom, how many rows).
"""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
import app.models as models
import logging

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _log_propagation(entity: str, field: str, old_val, new_val, rows_affected: int, by: str = "admin"):
    logger.info(
        "PROPAGATION [%s.%s] '%s' → '%s' | %d lignes mises à jour | par %s",
        entity, field, old_val, new_val, rows_affected, by
    )


def _rdv_non_termines(db: Session):
    """Retourne la query de base des RDV qui peuvent encore être modifiés."""
    return db.query(models.RendezVous).filter(
        models.RendezVous.statut.in_([
            models.StatutRDVEnum.en_attente,
            models.StatutRDVEnum.confirme,
        ])
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. PROPAGATION CHANGEMENT NOM MÉDECIN
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_nom_medecin(
    db: Session,
    ancien_nom: str,
    nouveau_nom: str,
    user_id: Optional[int] = None,
    profil_medecin_id: Optional[int] = None,
    specialiste_id: Optional[int] = None,
    modifie_par: str = "admin",
) -> dict:
    """
    Propage un changement de nom médecin vers toutes les entités liées.

    Tables mises à jour :
      - RendezVous.medecin_nom       (RDV non terminés uniquement)
      - RendezVous.medecin_email     (si user.email change aussi)
      - ActeFacturable.medecin_nom   (affichage comptable)
      - Decaissement.medecin_nom     (historique décaissements)
      - ProfilMedecin.nom            (si changement vient de User ou Specialiste)
      - TarifMedecin.medecin_nom     (tarifs affichage)

    Tables NON modifiées (immutables PCN) :
      - Mouvement.description        (écriture comptable — jamais altérée)
    """
    if not ancien_nom or not nouveau_nom or ancien_nom == nouveau_nom:
        return {"changed": 0, "message": "Aucun changement de nom"}

    counts = {}

    # 1. RDV non terminés
    rdvs = _rdv_non_termines(db).filter(
        models.RendezVous.medecin_nom.ilike(f"%{ancien_nom}%")
    ).all()
    for rdv in rdvs:
        rdv.medecin_nom = rdv.medecin_nom.replace(ancien_nom, nouveau_nom) if rdv.medecin_nom else nouveau_nom
    counts["rendez_vous"] = len(rdvs)

    # 2. Actes facturables — mise à jour affichage (pas les montants)
    actes = db.query(models.ActeFacturable).filter(
        models.ActeFacturable.medecin_nom.ilike(f"%{ancien_nom}%")
    ).all()
    for a in actes:
        a.medecin_nom = a.medecin_nom.replace(ancien_nom, nouveau_nom) if a.medecin_nom else nouveau_nom
    counts["actes_facturables"] = len(actes)

    # 3. Décaissements
    decs = db.query(models.Decaissement).filter(
        models.Decaissement.medecin_nom.ilike(f"%{ancien_nom}%")
    ).all()
    for d in decs:
        d.medecin_nom = d.medecin_nom.replace(ancien_nom, nouveau_nom) if d.medecin_nom else nouveau_nom
    counts["decaissements"] = len(decs)

    # 4. ProfilMedecin (si changement vient de User ou Specialiste)
    if user_id:
        profil = db.query(models.ProfilMedecin).filter(
            models.ProfilMedecin.user_id == user_id
        ).first()
        if profil:
            profil.nom = nouveau_nom
            counts["profil_medecin"] = 1

    elif profil_medecin_id:
        # Déjà mis à jour en amont
        counts["profil_medecin"] = 0

    elif ancien_nom:
        # Fallback : chercher par nom
        profils = db.query(models.ProfilMedecin).filter(
            models.ProfilMedecin.nom.ilike(f"%{ancien_nom}%")
        ).all()
        for p in profils:
            p.nom = p.nom.replace(ancien_nom, nouveau_nom)
        counts["profil_medecin"] = len(profils)

    # 5. TarifMedecin
    tarifs = db.query(models.TarifMedecin).filter(
        models.TarifMedecin.medecin_nom.ilike(f"%{ancien_nom}%")
    ).all()
    for t in tarifs:
        t.medecin_nom = t.medecin_nom.replace(ancien_nom, nouveau_nom) if t.medecin_nom else nouveau_nom
    counts["tarifs_medecin"] = len(tarifs)

    # 6. Specialiste (si changement vient de User uniquement)
    if user_id and not specialiste_id:
        specs = db.query(models.Specialiste).filter(
            models.Specialiste.nom.ilike(f"%{ancien_nom}%")
        ).all()
        for sp in specs:
            sp.nom = sp.nom.replace(ancien_nom, nouveau_nom)
        counts["specialiste"] = len(specs)

    db.commit()

    total = sum(counts.values())
    _log_propagation("Medecin", "nom", ancien_nom, nouveau_nom, total, modifie_par)

    return {
        "changed": total,
        "detail": counts,
        "message": f"Nom '{ancien_nom}' → '{nouveau_nom}' propagé sur {total} enregistrements",
        "note_comptable": "Les écritures comptables passées (Mouvement) sont immutables — conforme PCN Haïti",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. PROPAGATION CHANGEMENT TYPE MÉDECIN (affilié → investisseur, etc.)
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_type_medecin(
    db: Session,
    medecin_nom: str,
    ancien_type: str,
    nouveau_type: str,
    profil_medecin_id: Optional[int] = None,
    user_id: Optional[int] = None,
    modifie_par: str = "admin",
) -> dict:
    """
    Propage un changement de type médecin (affilie → investisseur, etc.).

    Impact :
      - ProfilMedecin.type_medecin  → mis à jour
      - User.type_medecin           → mis à jour
      - ActeFacturable futurs       → utiliseront automatiquement le nouveau type
                                       (car ils lisent toujours ProfilMedecin.type_medecin)
      - ActeFacturable PASSÉS       → IMMUTABLES (PCN)
      - Règles de partage futures   → recalculées automatiquement

    IMPORTANT : Les actes passés conservent leurs anciens pourcentages.
    C'est intentionnel : la comptabilité PCN ne permet pas de modifier les écritures passées.
    """
    if ancien_type == nouveau_type:
        return {"changed": 0, "message": "Aucun changement de type"}

    counts = {}

    # 1. ProfilMedecin
    if profil_medecin_id:
        profil = db.query(models.ProfilMedecin).filter(
            models.ProfilMedecin.id == profil_medecin_id
        ).first()
    else:
        profil = db.query(models.ProfilMedecin).filter(
            models.ProfilMedecin.nom.ilike(f"%{medecin_nom}%"),
            models.ProfilMedecin.actif == True,
        ).first()

    if profil:
        profil.type_medecin = nouveau_type
        counts["profil_medecin"] = 1
    else:
        counts["profil_medecin"] = 0

    # 2. User.type_medecin
    if user_id:
        user = db.query(models.User).filter(models.User.id == user_id).first()
    else:
        user = db.query(models.User).filter(
            models.User.nom.ilike(f"%{medecin_nom}%"),
            models.User.role == models.RoleEnum.medecin,
        ).first()

    if user:
        user.type_medecin = nouveau_type
        counts["user"] = 1
    else:
        counts["user"] = 0

    db.commit()

    # Calculer les nouvelles règles de partage pour info
    regles_nouvelles = db.query(models.ReglePartage).filter(
        models.ReglePartage.type_medecin == nouveau_type
    ).all()
    regles_info = {r.type_acte: f"{r.pct_medecin}% médecin / {r.pct_clinique}% clinique" for r in regles_nouvelles}

    total = sum(counts.values())
    _log_propagation("Medecin", "type_medecin", ancien_type, nouveau_type, total, modifie_par)

    return {
        "changed": total,
        "detail": counts,
        "message": f"Type '{ancien_type}' → '{nouveau_type}' propagé",
        "nouvelles_regles_partage": regles_info,
        "note_actes_passes": (
            f"Les actes facturables antérieurs conservent leurs pourcentages originaux "
            f"(règle PCN — immuabilité des écritures). "
            f"Les nouveaux actes appliqueront automatiquement les règles {nouveau_type}."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. PROPAGATION CHANGEMENT SPÉCIALITÉ MÉDECIN
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_specialite_medecin(
    db: Session,
    medecin_nom: str,
    ancienne_specialite: str,
    nouvelle_specialite: str,
    user_id: Optional[int] = None,
    modifie_par: str = "admin",
) -> dict:
    """
    Propage un changement de spécialité médecin.

    Impact :
      - User.specialite             → mis à jour (pour filtrage RDV côté médecin)
      - ProfilMedecin.specialite    → mis à jour
      - TarifMedecin.specialite     → mis à jour

    RDV passés : conservent l'ancienne spécialité (historique intact).
    RDV futurs confirmés : mis à jour si le médecin est explicitement assigné.
    """
    if ancienne_specialite == nouvelle_specialite:
        return {"changed": 0}

    counts = {}

    # User
    if user_id:
        user = db.query(models.User).filter(models.User.id == user_id).first()
    else:
        user = db.query(models.User).filter(
            models.User.nom.ilike(f"%{medecin_nom}%"),
            models.User.role == models.RoleEnum.medecin
        ).first()
    if user:
        user.specialite = nouvelle_specialite
        counts["user"] = 1

    # ProfilMedecin
    profil = db.query(models.ProfilMedecin).filter(
        models.ProfilMedecin.nom.ilike(f"%{medecin_nom}%")
    ).first()
    if profil:
        profil.specialite = nouvelle_specialite
        counts["profil_medecin"] = 1

    # TarifMedecin
    tarifs = db.query(models.TarifMedecin).filter(
        models.TarifMedecin.medecin_nom.ilike(f"%{medecin_nom}%")
    ).all()
    for t in tarifs:
        t.specialite = nouvelle_specialite
    counts["tarifs_medecin"] = len(tarifs)

    # Specialiste table
    spec = db.query(models.Specialiste).filter(
        models.Specialiste.nom.ilike(f"%{medecin_nom}%")
    ).first()
    if spec:
        spec.specialite = nouvelle_specialite
        counts["specialiste"] = 1

    db.commit()
    total = sum(counts.values())
    _log_propagation("Medecin", "specialite", ancienne_specialite, nouvelle_specialite, total, modifie_par)

    return {
        "changed": total,
        "detail": counts,
        "message": f"Spécialité '{ancienne_specialite}' → '{nouvelle_specialite}' propagée",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. PROPAGATION CHANGEMENT EMAIL / TÉLÉPHONE MÉDECIN
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_contact_medecin(
    db: Session,
    medecin_nom: str,
    ancien_email: Optional[str],
    nouveau_email: Optional[str],
    modifie_par: str = "admin",
) -> dict:
    """
    Propage un changement d'email médecin vers les RDV non terminés.
    Les futures notifications utiliseront le nouvel email.
    """
    if not nouveau_email or ancien_email == nouveau_email:
        return {"changed": 0}

    rdvs = _rdv_non_termines(db).filter(
        models.RendezVous.medecin_email == ancien_email
    ).all()
    for rdv in rdvs:
        rdv.medecin_email = nouveau_email
    db.commit()

    _log_propagation("Medecin", "email", ancien_email, nouveau_email, len(rdvs), modifie_par)
    return {"changed": len(rdvs), "rendez_vous_updated": len(rdvs)}


# ══════════════════════════════════════════════════════════════════════════════
# 5. PROPAGATION CHANGEMENT TARIF / PRIX
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_tarif(
    db: Session,
    code_tarif: str,
    ancien_prix: float,
    nouveau_prix: float,
    modifie_par: str = "admin",
) -> dict:
    """
    Un changement de tarif :
      - Met à jour GesteMedical.prix_suggere si lié (même code/libelle)
      - N'affecte PAS les transactions passées (PCN — immuabilité)
      - Retourne un avertissement pour que l'admin puisse notifier la caisse
    """
    counts = {}

    # Chercher les gestes liés par code
    gestes = db.query(models.GesteMedical).filter(
        models.GesteMedical.libelle.ilike(f"%{code_tarif}%"),
        models.GesteMedical.prix_fixe == False,  # prix_fixe = ne pas écraser
    ).all()
    for g in gestes:
        g.prix_suggere = nouveau_prix
    counts["gestes_medicaux"] = len(gestes)

    db.commit()
    _log_propagation("Tarif", "prix", ancien_prix, nouveau_prix, sum(counts.values()), modifie_par)

    return {
        "changed": sum(counts.values()),
        "detail": counts,
        "message": f"Prix {code_tarif} : {ancien_prix} → {nouveau_prix} HTG",
        "avertissement": (
            "Le nouveau prix s'applique aux prochaines transactions. "
            "Les transactions passées sont immutables (règle PCN). "
            "Informez la caisse du changement de tarif."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6. PROPAGATION CHANGEMENT NOM SERVICE
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_nom_service(
    db: Session,
    ancien_nom: str,
    nouveau_nom: str,
    modifie_par: str = "admin",
) -> dict:
    """
    Un changement de nom de service propage vers :
      - Mouvements futurs : la catégorie des prochains mouvements utilisera le nouveau nom
      - Aucune modification des mouvements passés (PCN immuable)
    """
    # Pas de propagation sur les mouvements passés — immutables PCN
    # On retourne juste une confirmation que le changement est enregistré
    _log_propagation("Service", "nom", ancien_nom, nouveau_nom, 0, modifie_par)
    return {
        "changed": 0,
        "message": f"Nom service '{ancien_nom}' → '{nouveau_nom}' enregistré",
        "note": "Les mouvements comptables passés conservent l'ancienne catégorie (PCN immuable). "
                "Les nouveaux mouvements utiliseront automatiquement le nouveau nom.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 7. PROPAGATION CHANGEMENT CONTACT PATIENT (email / téléphone)
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_contact_patient(
    db: Session,
    patient_email_ancien: Optional[str],
    patient_email_nouveau: Optional[str],
    patient_tel_ancien: Optional[str],
    patient_tel_nouveau: Optional[str],
    modifie_par: str = "admin",
) -> dict:
    """
    Propage les changements de contact patient vers les RDV non terminés.
    """
    counts = {}

    if patient_email_ancien and patient_email_nouveau and patient_email_ancien != patient_email_nouveau:
        rdvs = _rdv_non_termines(db).filter(
            models.RendezVous.patient_email == patient_email_ancien
        ).all()
        for rdv in rdvs:
            rdv.patient_email = patient_email_nouveau
        counts["rdv_email"] = len(rdvs)

    if patient_tel_ancien and patient_tel_nouveau and patient_tel_ancien != patient_tel_nouveau:
        rdvs = _rdv_non_termines(db).filter(
            models.RendezVous.patient_telephone == patient_tel_ancien
        ).all()
        for rdv in rdvs:
            rdv.patient_telephone = patient_tel_nouveau
        counts["rdv_telephone"] = len(rdvs)

    db.commit()
    total = sum(counts.values())
    logger.info("PROPAGATION Patient contact → %d RDV mis à jour", total)
    return {"changed": total, "detail": counts}


# ══════════════════════════════════════════════════════════════════════════════
# 8. PROPAGATION RÈGLES DE PARTAGE (changement pourcentages)
# ══════════════════════════════════════════════════════════════════════════════

def propager_changement_regles_partage(
    db: Session,
    type_medecin: str,
    type_acte: str,
    ancien_pct: float,
    nouveau_pct: float,
    modifie_par: str = "admin",
) -> dict:
    """
    Un changement de règle de partage s'applique uniquement aux futurs actes.
    Les actes passés gardent leurs anciens pourcentages (PCN immuable).
    Retourne un résumé des médecins impactés.
    """
    # Compter les médecins concernés par ce type
    medecins_concernes = db.query(models.ProfilMedecin).filter(
        models.ProfilMedecin.type_medecin == type_medecin,
        models.ProfilMedecin.actif == True,
    ).count()

    _log_propagation(
        "ReglePartage", f"{type_medecin}/{type_acte}",
        f"{ancien_pct}%", f"{nouveau_pct}%",
        medecins_concernes, modifie_par
    )

    return {
        "changed": 0,  # Pas de modification des actes passés
        "medecins_concernes": medecins_concernes,
        "message": (
            f"Règle {type_medecin}/{type_acte} : {ancien_pct}% → {nouveau_pct}% médecin. "
            f"{medecins_concernes} médecin(s) concerné(s). "
            f"S'applique aux prochains actes uniquement."
        ),
        "note_pcn": "Actes passés immutables — conforme PCN Haïti",
    }
