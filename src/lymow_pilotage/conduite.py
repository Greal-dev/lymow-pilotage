"""Chemin unique des ordres, partagé par la ligne de commande et l'interface web.

Rôle : valider, simuler ou émettre une liste d'ordres, et journaliser. Aucune
autre voie ne mène à `mouvement.executer_sequence` : l'interface ne peut donc
relâcher aucun invariant que la commande respecte.
"""

from __future__ import annotations

from typing import Any

from . import coffre, mouvement
from .config import Calibration
from .journal import Journal
from .mouvement import Ordre
from .session import Session

# Codes de sortie : 0 fait, 1 échec ou séquence interrompue, 2 refus ou saisie,
# 3 arrêt non confirmé (le robot peut encore bouger), 130 interruption clavier.
CODE_REFUS = 2
CODE_ARRET_NON_CONFIRME = 3

# Sortie lisible : les clés JSON restent en ASCII, l'affichage porte les accents.
LIBELLES = {
    "duree_s": "durée (s)", "cadence_hz": "cadence (Hz)", "emissions_prevues": "émissions prévues",
    "arret_final": "arrêt final", "categorie": "catégorie", "hexadecimal": "hexadécimal",
    "vitesse_lineaire_m_s": "vitesse linéaire (m/s)", "vitesse_angulaire_rad_s": "vitesse angulaire (rad/s)",
    "repetitions": "répétitions", "precede_si_telecommande": "précédé, si le robot est en télécommande, de",
    "hauteur_coupe_mm": "hauteur de coupe (mm)", "hauteur_avant_mm": "hauteur avant (mm)",
    "hauteur_apres_mm": "hauteur après (mm)", "emissions": "émissions", "etat_rafraichi": "état rafraîchi",
    "apres": "après", "statut_apres": "statut après", "deplacement_m": "déplacement (m)",
    "rotation_deg": "rotation (°)", "est_m": "est (m)", "nord_m": "nord (m)", "cap_deg": "cap (°)",
    "arrets_acquittes": "arrêts acquittés", "codes_emis": "codes émis", "echec": "échec", "etapes": "étapes",
    "sequence": "séquence", "en_ligne": "en ligne", "batterie_pct": "batterie (%)",
    "statut_libelle": "statut (libellé)", "position_est_m": "position est (m)",
    "position_nord_m": "position nord (m)", "age_position_s": "âge de la position (s)",
    "rtk_satellites": "satellites RTK", "signal_4g": "signal 4G", "signal_wifi": "signal Wi-Fi",
    "wifi_rssi_dbm": "Wi-Fi RSSI (dBm)", "age_etat_s": "âge de l'état (s)", "etape": "étape",
    "reussi": "réussi", "detail": "détail", "region": "région", "methode": "méthode",
    "vitesse_m_s": "vitesse (m/s)", "vitesse_rad_s": "vitesse (rad/s)",
    "deplacement_commande_m": "déplacement commandé (m)", "deplacement_commande_deg": "déplacement commandé (°)",
    "observation_s": "fenêtre d'observation (s)",
    "deplacement_si_consigne_conservee_m": "déplacement si la consigne est conservée (m)",
    "puis_pause": "puis mise en pause de la tâche", "acquittee": "acquittée",
    "non_executees": "étapes non exécutées", "arret_non_confirme": "arrêt non confirmé",
    "avance_m": "avance le long du cap (m)", "verifie": "ordre vérifié par le constat",
    "lecture": "lecture de l'essai", "exposition": "exposition maximale",
    "etat_recu": "état reçu", "perime": "état périmé", "code_sortie": "code de sortie",
    "ecart_cm": "écart (cm)", "raccord_m": "raccord (m)", "sommets_deplaces": "sommets déplacés",
    "deplacement_max_cm": "déplacement maximal (cm)", "surface_avant_m2": "surface avant (m²)",
    "surface_apres_m2": "surface après (m²)", "variation_m2": "variation de surface (m²)",
    "surface_m2": "surface (m²)", "zones_interdites": "zones interdites", "passage_de_station": "passage de station",
    "complete": "carte complète", "manques": "ce qui manque", "empreinte": "empreinte à confirmer",
    "connexion_en_un_clic": "connexion en un clic",
    "verification": "vérification par l'outil", "issue": "issue de l'étape", "reprise": "reprise proposée",
    "cote": "côté", "apercu": "aperçu", "differences": "différences", "horodatage": "horodatage",
}


def refuser(ordres: list[Ordre], cal: Calibration, journal: Journal, mode: str) -> dict[str, Any] | None:
    """SEC-07 : un seul plafond dépassé et rien n'est simulé, ouvert ni émis."""
    refus = mouvement.refus_prealable(ordres, cal)
    if refus is None:
        return None
    journal.ecrire("ordre-refuse", mode=mode, **refus)
    return {"mode": mode, "etapes": []} | refus


def simuler_ordres(
    ordres: list[Ordre], cal: Calibration, journal: Journal, appareil: str | None = None
) -> tuple[dict[str, Any], int]:
    """SEC-05 : ce chemin n'ouvre aucune liaison et ne publie rien."""
    refus = refuser(ordres, cal, journal, "simulation")
    if refus:
        return refus, CODE_REFUS
    appareil = appareil or coffre.appareil_connu()  # appareil fourni (démonstration) : coffre jamais lu
    etapes = [mouvement.simuler(ordre, cal, appareil) for ordre in ordres]
    for etape in etapes:
        journal.ecrire("ordre-simule", **etape)  # SEC-06
    return {"mode": "simulation", "etapes": etapes, "interrompue": False}, 0


async def emettre_ordres(session: Session, ordres: list[Ordre], journal: Journal) -> tuple[dict[str, Any], int]:
    """Émission réelle sur une session ouverte ; l'appelant a déjà passé `refuser`."""
    journal.ecrire("session-reelle", strict=True, ordres=[str(ordre) for ordre in ordres])
    try:
        bilan = await mouvement.executer_sequence(session, ordres)
    except BaseException:
        journal.ecrire("ordre-interrompu", ordres=[str(ordre) for ordre in ordres])
        raise
    for etape in bilan["etapes"]:
        journal.ecrire("ordre-refuse" if "echec" in etape else "ordre-constate", **etape)
    if bilan.get("arret_non_confirme"):
        return bilan, CODE_ARRET_NON_CONFIRME
    return bilan, 1 if bilan["interrompue"] else 0
