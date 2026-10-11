"""Invariants de sécurité codés en dur (SEC-01, SEC-02) et constantes de SEC-03/04.

Rôle : bornes, liste blanche des statuts « lames à l'arrêt », calcul de la
consigne d'un mouvement. Ne publie rien, ne lit aucun réglage. Les facteurs de
calibration ne changent que la durée, et le déplacement commandé (vitesse ×
durée) reste plafonné ici : aucune option, variable d'environnement ou clé de
calibration ne peut dépasser ces bornes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .vendor.ha_lymow import const as socle

# SEC-02 : vitesses faibles, fixes, sous les bornes relevées par le socle (±0,5 m/s, ±0,6 rad/s).
VITESSE_LINEAIRE_M_S = 0.25
VITESSE_ANGULAIRE_RAD_S = 0.35
# SEC-02 : plafonds par ordre, appliqués à l'ordre ET au déplacement commandé.
DISTANCE_MAX_M = 2.0
ANGLE_MAX_DEG = 180.0
# SEC-02 : borne dure de durée (180° à 0,35 rad/s = 8,98 s ; 2 m à 0,25 m/s = 8 s).
DUREE_MAX_S = 9.0
# SEC-02 : hauteur de coupe admise (bornes du socle et de Lymow-HA : 20-100 mm).
HAUTEUR_MIN_MM = 20
HAUTEUR_MAX_MM = 100
# SEC-02 : « plateau-haut » ne peut pas servir à descendre le plateau.
PLATEAU_HAUT_MIN_MM = 80

# SEC-03 : cadence de republication de la consigne, arrêt répété, délais d'acquittement.
CADENCE_HZ = 10
REPETITIONS_ARRET = 3
DELAI_CONSIGNE_S = 1.0
DELAI_ARRET_S = 3.0
# SEC-03 : échéance globale de la phase d'arrêt, reconnexions comprises.
ECHEANCE_ARRET_S = 20.0
PAUSE_REPRISE_ARRET_S = 0.5

# SEC-01 : âge maximal de l'état, lui-même reçu après une sollicitation de la session.
AGE_MAX_ETAT_S = 30.0
# SEC-04 : seuils sous lesquels on déclare que le robot n'a pas bougé.
SEUIL_DEPLACEMENT_M = 0.05
SEUIL_ROTATION_DEG = 3.0
# SEC-04 : au-delà de 1,5 fois l'ordre plus une marge, le déplacement est anormal.
EXCES_FACTEUR = 1.5
EXCES_MARGE_M = 0.10
EXCES_MARGE_DEG = 5.0
SENS_MARGE_DEG = 10.0

# SEC-01 : seuls statuts où les lames sont tenues pour arrêtées. Tout autre
# statut — tonte (2, 8, 9), pause (3), retour (4, 10, 14), erreur (7, 13),
# mise à jour (11), essais d'usine (15, 16), inconnu, hors ligne (-1) — refuse.
STATUTS_LAMES_ARRET = frozenset(
    {
        socle.WORK_STATUS_NONE,
        socle.WORK_STATUS_WAITING,
        socle.WORK_STATUS_CHARGING,
        socle.WORK_STATUS_CHARGING_FULL,
        socle.WORK_STATUS_REMOTE_CONTROL,
    }
)


class RefusSecurite(Exception):
    """Ordre refusé avant toute émission par un invariant de sécurité."""


@dataclass(frozen=True)
class Consigne:
    """Vitesses à tenir pendant une durée ; `attendu` et `valeur` servent au constat SEC-04."""

    lineaire: float
    angulaire: float
    duree: float
    attendu: str  # "translation" ou "rotation"
    valeur: float  # ordre signé : mètres (positif = avant) ou degrés (positif = gauche)


def _nombre_fini(valeur: float, nom: str) -> float:
    # SEC-02 : NaN et infini passeraient toutes les comparaisons de borne.
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or not math.isfinite(valeur):
        raise RefusSecurite(f"{nom} invalide : un nombre fini est exigé")
    return float(valeur)


def verifier_vitesses(lineaire: float, angulaire: float) -> None:
    """SEC-02 : dernière barrière, appelée par l'encodeur lui-même."""
    lineaire = _nombre_fini(lineaire, "vitesse linéaire")
    angulaire = _nombre_fini(angulaire, "vitesse angulaire")
    if abs(lineaire) > VITESSE_LINEAIRE_M_S or abs(angulaire) > VITESSE_ANGULAIRE_RAD_S:
        raise RefusSecurite("vitesse hors plafond (SEC-02)")


def verifier_hauteur(millimetres: int) -> int:
    if isinstance(millimetres, bool) or not isinstance(millimetres, int):
        raise RefusSecurite("hauteur de coupe invalide : un entier en millimètres est exigé")
    if not HAUTEUR_MIN_MM <= millimetres <= HAUTEUR_MAX_MM:
        raise RefusSecurite(f"hauteur de coupe hors bornes ({HAUTEUR_MIN_MM}-{HAUTEUR_MAX_MM} mm)")
    return millimetres


def verifier_plateau_haut(millimetres: int) -> int:
    """SEC-02 : la hauteur de « plateau-haut » reste dans le haut de la course."""
    if verifier_hauteur(millimetres) < PLATEAU_HAUT_MIN_MM:
        raise RefusSecurite(
            f"« plateau-haut » exige {PLATEAU_HAUT_MIN_MM} à {HAUTEUR_MAX_MM} mm (SEC-02)"
        )
    return millimetres


def _duree(consigne: float, vitesse: float, facteur: float, plafond: float) -> float:
    """Durée d'émission ; `consigne` et `plafond` dans la même unité (m ou rad)."""
    facteur = _nombre_fini(facteur, "facteur de calibration")
    if facteur <= 0:
        raise RefusSecurite("facteur de calibration invalide")
    # SEC-02 : ce que le robot reçoit réellement (vitesse × durée) est plafonné aussi.
    commande = consigne / facteur
    if commande > plafond * (1 + 1e-9):
        raise RefusSecurite(
            "déplacement commandé au-dessus du plafond une fois le facteur de calibration appliqué (SEC-02)"
        )
    duree = commande / vitesse
    if duree > DUREE_MAX_S:
        raise RefusSecurite(f"durée de mouvement hors borne (maximum {DUREE_MAX_S:g} s) (SEC-02)")
    if duree < 1.0 / CADENCE_HZ:
        raise RefusSecurite("mouvement trop court : il ne donnerait lieu à aucune émission utile (SEC-02)")
    return duree


def consigne_translation(metres: float, facteur_distance: float = 1.0) -> Consigne:
    """SEC-02 : `metres` signé (positif = avant), plafonné à 2 m."""
    metres = _nombre_fini(metres, "distance")
    if metres == 0 or abs(metres) > DISTANCE_MAX_M:
        raise RefusSecurite(f"distance refusée : entre 0 (exclu) et {DISTANCE_MAX_M:g} m par ordre (SEC-02)")
    duree = _duree(abs(metres), VITESSE_LINEAIRE_M_S, facteur_distance, DISTANCE_MAX_M)
    return Consigne(math.copysign(VITESSE_LINEAIRE_M_S, metres), 0.0, duree, "translation", metres)


def consigne_rotation(degres: float, facteur_angle: float = 1.0, signe: int = 1) -> Consigne:
    """SEC-02 : `degres` signé (positif = gauche), plafonné à 180°."""
    degres = _nombre_fini(degres, "angle")
    if degres == 0 or abs(degres) > ANGLE_MAX_DEG:
        raise RefusSecurite(f"angle refusé : entre 0 (exclu) et {ANGLE_MAX_DEG:g}° par ordre (SEC-02)")
    duree = _duree(
        math.radians(abs(degres)), VITESSE_ANGULAIRE_RAD_S, facteur_angle, math.radians(ANGLE_MAX_DEG)
    )
    vitesse = math.copysign(VITESSE_ANGULAIRE_RAD_S, degres) * signe
    return Consigne(0.0, vitesse, duree, "rotation", degres)


def verifier_presence_et_statut(statut: int | None, en_ligne: bool | None) -> None:
    """SEC-01 : présence explicitement confirmée et statut dans la liste blanche."""
    if en_ligne is False:
        raise RefusSecurite("robot hors ligne : mouvement refusé (SEC-01)")
    if en_ligne is not True:
        raise RefusSecurite("présence en ligne du robot non confirmée : mouvement refusé (SEC-01)")
    if statut not in STATUTS_LAMES_ARRET:
        raise RefusSecurite(
            f"statut de travail {statut} hors de la liste « lames à l'arrêt » : mouvement refusé (SEC-01)"
        )


def verifier_lames_arret(statut: int | None, age_s: float | None, en_ligne: bool | None) -> None:
    """SEC-01 : refuse tout mouvement sans preuve fraîche de lames à l'arrêt."""
    if statut is None or age_s is None:
        raise RefusSecurite("aucun état reçu du robot depuis la sollicitation : mouvement refusé (SEC-01)")
    if not 0 <= age_s <= AGE_MAX_ETAT_S:
        raise RefusSecurite(
            f"état périmé ({age_s:.0f} s, maximum {AGE_MAX_ETAT_S:.0f} s) : mouvement refusé (SEC-01)"
        )
    verifier_presence_et_statut(statut, en_ligne)
