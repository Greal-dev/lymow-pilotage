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
# SEC-02 : planchers de `--vitesse` (en dessous, le robot risque de ne pas décoller).
VITESSE_LINEAIRE_MIN_M_S = 0.05
VITESSE_ANGULAIRE_MIN_RAD_S = 0.05
# SEC-02 : essai d'arrêt automatique — une seule consigne, lente, fenêtre courte.
# Exposition : 0,15 m/s × 4 s = 0,6 m, plus le délai d'arrêt.
ESSAI_VITESSE_M_S = 0.10
ESSAI_VITESSE_MAX_M_S = 0.15
ESSAI_OBSERVATION_S = 3.0
ESSAI_OBSERVATION_MIN_S = 1.0
ESSAI_OBSERVATION_MAX_S = 4.0
# Lecture de l'essai : part du déplacement « consigne conservée » réellement mesurée.
ESSAI_PART_CONSERVEE = 0.7
ESSAI_PART_ARRET = 0.4
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
# statut — tonte (2, 8, 9), retour (4, 10, 14), arrêt d'urgence (13), mise à
# jour (11), essais d'usine (15, 16), inconnu, hors ligne (-1) — refuse.
STATUTS_LAMES_ARRET = frozenset(
    {
        socle.WORK_STATUS_NONE,
        socle.WORK_STATUS_WAITING,
        socle.WORK_STATUS_CHARGING,
        socle.WORK_STATUS_CHARGING_FULL,
        socle.WORK_STATUS_REMOTE_CONTROL,
        # PAUSE (3) et ERROR (7) : ajoutés sur décision du propriétaire le 2026-10-11 ;
        # arrêt des lames dans ces statuts à constater robot à vue lors de la calibration.
        socle.WORK_STATUS_PAUSE,
        socle.WORK_STATUS_ERROR,
    }
)


# SEC-08 : une carte ne s'écrit que robot au repos ou en charge — ni tonte, ni pause,
# ni erreur, ni télécommande, ni retour.
STATUTS_ECRITURE_CARTE = frozenset(
    {
        socle.WORK_STATUS_NONE,
        socle.WORK_STATUS_WAITING,
        socle.WORK_STATUS_CHARGING,
        socle.WORK_STATUS_CHARGING_FULL,
    }
)
# SEC-09 : au-delà, la carte en mémoire est redemandée avant toute écriture.
AGE_MAX_CARTE_S = 60.0
ATTENTE_CARTE_S = 20.0
# SEC-10 : bornes dures d'une retouche de contour.
AJUSTEMENT_MAX_M = 0.50
TOLERANCE_CONTOUR_M = 0.001  # un flottant simple précision à 100 m se lit à 0,01 mm près
RACCORD_DEFAUT_M = 0.5
RACCORD_MAX_M = 5.0
# SEC-10 : dérive cumulée d'un sommet par rapport à la référence locale de sa zone.
CUMUL_MAX_M = 1.00
# SEC-10 : une zone interdite garde au moins cette marge avec le nouveau contour.
DEGAGEMENT_INTERDITE_M = 0.05
# SEC-11 : deux relectures après l'écriture, à 1 s puis 5 s.
ATTENTES_VERDICT_S = (1.0, 4.0)
SOMMETS_MAX = 2000


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


def _vitesse(demande: float | None, plafond: float, plancher: float, nom: str, signee: bool = False) -> float:
    """SEC-02 : vitesse choisie par ordre, entre plancher et plafond ; défaut = plafond."""
    if demande is None:
        return plafond
    demande = _nombre_fini(demande, nom)
    if demande < 0 and not signee:
        raise RefusSecurite(f"{nom} refusée : valeur positive exigée (SEC-02)")
    if not plancher <= abs(demande) <= plafond:
        raise RefusSecurite(f"{nom} refusée : de {plancher:g} à {plafond:g} en valeur absolue (SEC-02)")
    return demande


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
    return _duree_bornee(commande / vitesse)


def _duree_bornee(duree: float) -> float:
    if duree > DUREE_MAX_S:
        raise RefusSecurite(
            f"durée de mouvement hors borne (maximum {DUREE_MAX_S:g} s) : "
            "réduisez l'ordre ou augmentez la vitesse (SEC-02)"
        )
    if duree < 1.0 / CADENCE_HZ:
        raise RefusSecurite("mouvement trop court : il ne donnerait lieu à aucune émission utile (SEC-02)")
    return duree


def _duree_directe(duree: float, vitesse: float, plafond: float) -> float:
    """SEC-02 : `--duree` tient la consigne telle quelle ; vitesse × durée reste plafonné."""
    duree = _duree_bornee(_nombre_fini(duree, "durée"))
    if abs(vitesse) * duree > plafond * (1 + 1e-9):
        raise RefusSecurite("déplacement commandé (vitesse × durée) au-dessus du plafond (SEC-02)")
    return duree


def _un_seul(quantite: float | None, duree: float | None, nom: str) -> None:
    if (quantite is None) == (duree is None):
        raise RefusSecurite(f"indiquez soit {nom}, soit --duree : exactement l'un des deux")


def consigne_translation(
    metres: float | None,
    facteur_distance: float = 1.0,
    *,
    vitesse: float | None = None,
    duree: float | None = None,
    sens: int = 1,
) -> Consigne:
    """SEC-02 : `metres` signé (positif = avant), plafonné à 2 m ; ou `duree` dans le sens `sens`."""
    _un_seul(metres, duree, "une distance")
    v = _vitesse(vitesse, VITESSE_LINEAIRE_M_S, VITESSE_LINEAIRE_MIN_M_S, "vitesse linéaire")
    if duree is not None:
        tenue = _duree_directe(duree, v, DISTANCE_MAX_M)
        return Consigne(sens * v, 0.0, tenue, "translation", sens * v * tenue)
    metres = _nombre_fini(metres, "distance")
    if metres == 0 or abs(metres) > DISTANCE_MAX_M:
        raise RefusSecurite(f"distance refusée : entre 0 (exclu) et {DISTANCE_MAX_M:g} m par ordre (SEC-02)")
    tenue = _duree(abs(metres), v, facteur_distance, DISTANCE_MAX_M)
    return Consigne(math.copysign(v, metres), 0.0, tenue, "translation", metres)


def consigne_rotation(
    degres: float | None,
    facteur_angle: float = 1.0,
    signe: int = 1,
    *,
    vitesse: float | None = None,
    duree: float | None = None,
) -> Consigne:
    """SEC-02 : `degres` signé (positif = gauche), plafonné à 180°.

    Avec `duree`, c'est le signe de la vitesse qui donne le sens (positif = gauche).
    `signe` est le réglage de calibration appliqué à la vitesse envoyée.
    """
    _un_seul(degres, duree, "un angle")
    v = _vitesse(
        vitesse, VITESSE_ANGULAIRE_RAD_S, VITESSE_ANGULAIRE_MIN_RAD_S, "vitesse angulaire",
        signee=duree is not None,
    )
    if duree is not None:
        tenue = _duree_directe(duree, v, math.radians(ANGLE_MAX_DEG))
        return Consigne(0.0, v * signe, tenue, "rotation", math.degrees(v * tenue))
    degres = _nombre_fini(degres, "angle")
    if degres == 0 or abs(degres) > ANGLE_MAX_DEG:
        raise RefusSecurite(f"angle refusé : entre 0 (exclu) et {ANGLE_MAX_DEG:g}° par ordre (SEC-02)")
    tenue = _duree(math.radians(abs(degres)), v, facteur_angle, math.radians(ANGLE_MAX_DEG))
    return Consigne(0.0, math.copysign(v, degres) * signe, tenue, "rotation", degres)


def consigne_essai(vitesse: float | None, observation: float | None) -> Consigne:
    """Essai d'arrêt automatique : une consigne avant, lente, puis silence pendant `observation`."""
    v = ESSAI_VITESSE_M_S if vitesse is None else _nombre_fini(vitesse, "vitesse d'essai")
    if not VITESSE_LINEAIRE_MIN_M_S <= v <= ESSAI_VITESSE_MAX_M_S:
        raise RefusSecurite(
            f"vitesse d'essai refusée : de {VITESSE_LINEAIRE_MIN_M_S:g} à {ESSAI_VITESSE_MAX_M_S:g} m/s (SEC-02)"
        )
    fenetre = ESSAI_OBSERVATION_S if observation is None else _nombre_fini(observation, "fenêtre d'observation")
    if not ESSAI_OBSERVATION_MIN_S <= fenetre <= ESSAI_OBSERVATION_MAX_S:
        raise RefusSecurite(
            f"fenêtre d'observation refusée : de {ESSAI_OBSERVATION_MIN_S:g} à {ESSAI_OBSERVATION_MAX_S:g} s (SEC-02)"
        )
    return Consigne(v, 0.0, fenetre, "translation", v * fenetre)



# Statuts où la mise en pause rend la main : tonte (2, 8, 9), retour à la base (4), dégagement (14).
# Le retour déjà en pause (10) n'en fait pas partie : une seconde pause n'y changerait rien.
STATUTS_PAUSE_UTILE = socle.WORK_STATUS_MOWING_GROUP | {socle.WORK_STATUS_DOCKING, socle.WORK_STATUS_ESCAPING}


def explication_statut(statut: int | None) -> tuple[str, str | None]:
    """Pourquoi un mouvement est refusé dans ce statut, et que faire — en clair, sans numéro.

    Constat du 2026-10-12 : « statut de travail 2 hors de la liste » ne disait ni l'un ni l'autre.
    La règle (liste blanche de SEC-01) ne change pas : seul son énoncé est humain."""
    if statut in socle.WORK_STATUS_MOWING_GROUP:
        return "le robot tond", "mettez la tâche en pause pour prendre la main"
    connus = {
        socle.WORK_STATUS_DOCKING: ("le robot rentre à sa base", "mettez le retour en pause pour prendre la main"),
        socle.WORK_STATUS_ESCAPING: ("le robot se dégage d'un obstacle", "mettez la tâche en pause pour prendre la main"),
        socle.WORK_STATUS_PAUSE_DOCKING: (
            "le retour à la base est en pause",
            "aucun mouvement n'est permis dans ce statut ; seul « Annuler la tâche » peut rendre la main",
        ),
        socle.WORK_STATUS_UPDATING: ("le robot se met à jour", "attendez la fin de la mise à jour"),
        socle.WORK_STATUS_EMERGENCY_STOP: ("arrêt d'urgence enclenché", "levez-le sur le robot"),
        socle.WORK_STATUS_OFFLINE: ("robot inconnu ou hors ligne", "attendez son retour en ligne"),
        None: ("aucun statut reçu du robot", "cliquez sur « Actualiser »"),
    }
    return connus.get(statut, (f"statut {statut} non reconnu comme « lames à l'arrêt »", None))


def verifier_presence_et_statut(statut: int | None, en_ligne: bool | None) -> None:
    """SEC-01 : présence explicitement confirmée et statut dans la liste blanche."""
    if en_ligne is False:
        raise RefusSecurite("robot hors ligne : attendez son retour en ligne — mouvement refusé (SEC-01)")
    if en_ligne is not True:
        raise RefusSecurite(
            "présence en ligne du robot non confirmée : cliquez sur « Actualiser » — mouvement refusé (SEC-01)"
        )
    if statut not in STATUTS_LAMES_ARRET:
        constat, conseil = explication_statut(statut)
        raise RefusSecurite(f"{constat} : {conseil + ' — ' if conseil else ''}mouvement refusé (SEC-01)")


def verifier_repos_pour_carte(statut: int | None, age_s: float | None, en_ligne: bool | None) -> None:
    """SEC-08 : écriture de carte seulement robot en ligne, état frais, au repos ou en charge."""
    if statut is None or age_s is None:
        raise RefusSecurite("aucun état reçu du robot depuis la sollicitation : écriture de carte refusée (SEC-08)")
    if not 0 <= age_s <= AGE_MAX_ETAT_S:
        raise RefusSecurite(f"état périmé ({age_s:.0f} s) : écriture de carte refusée (SEC-08)")
    if en_ligne is not True:
        raise RefusSecurite("robot hors ligne ou présence non confirmée : écriture de carte refusée (SEC-08)")
    if statut not in STATUTS_ECRITURE_CARTE:
        raise RefusSecurite(
            f"statut de travail {statut} : une carte ne s'écrit que robot au repos ou en charge (SEC-08)"
        )


def verifier_lames_arret(statut: int | None, age_s: float | None, en_ligne: bool | None) -> None:
    """SEC-01 : refuse tout mouvement sans preuve fraîche de lames à l'arrêt."""
    if statut is None or age_s is None:
        raise RefusSecurite(
            "aucun état reçu du robot depuis la sollicitation : cliquez sur « Actualiser » — mouvement refusé (SEC-01)"
        )
    if not 0 <= age_s <= AGE_MAX_ETAT_S:
        raise RefusSecurite(
            f"état périmé ({age_s:.0f} s, maximum {AGE_MAX_ETAT_S:.0f} s) : cliquez sur « Actualiser » — "
            "mouvement refusé (SEC-01)"
        )
    verifier_presence_et_statut(statut, en_ligne)
