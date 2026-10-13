"""Invariants de sécurité codés en dur (SEC-01, SEC-01b, SEC-02) et constantes de SEC-03/04.

Rôle : bornes, liste blanche des statuts « lames à l'arrêt », attestation de
l'opérateur (SEC-01b, en mémoire seulement), calcul de la consigne d'un mouvement. Ne publie rien, ne lit aucun réglage. Les facteurs de
calibration ne changent que la durée, et le déplacement commandé (vitesse ×
durée) reste plafonné ici : aucune option, variable d'environnement ou clé de
calibration ne peut dépasser ces bornes.
"""

from __future__ import annotations

import math
from collections.abc import Callable
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
# jour (11), essais d'usine (15, 16), inconnu, hors ligne (-1) — refuse, sauf SEC-01b ci-dessous.
# Constat du 2026-10-12 (captures réelles d'ordres) : cette liste s'applique au statut VIVANT du
# robot, `robotStatus` (champ 1 de robotInfo), et à lui seul. `workStatus` (champ 6) reste à 2
# (tonte) pendant toute la tâche, pause comprise — le juger refusait à tort un robot en pause.
# Valeurs réelles (f1/f6) : en charge 5/1, au repos 1/1, tâche en pause 3/2, et 6/2 pendant une
# trentaine de secondes après toute trame de pilotage manuel. Le contenu de la liste ne change pas.
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

# SEC-01b (décision du propriétaire, 2026-10-12) : une attestation explicite de l'opérateur
# (« lames à l'arrêt constatées »), temporaire et journalisée, admet un mouvement manuel dans les
# statuts ci-dessous. Comme tout SEC-01, elle porte sur le statut VIVANT du robot (robotStatus,
# champ 1) : le « statut 2 en pause » qui l'a motivée venait du champ 6, qui n'est plus jugé.
# L'outil ne peut PAS vérifier ce que l'opérateur atteste.
STATUTS_ATTESTABLES = frozenset(
    {
        socle.WORK_STATUS_MOWING,
        socle.WORK_STATUS_RESUME,
        socle.WORK_STATUS_ZONE_PARTITION,
        socle.WORK_STATUS_DOCKING,
        socle.WORK_STATUS_PAUSE_DOCKING,
        socle.WORK_STATUS_ESCAPING,
    }
)
# SEC-01b : aucune attestation ne lève ces refus ; un statut absent ou inconnu refuse aussi,
# puisqu'il n'est dans aucune des deux listes admises.
STATUTS_TOUJOURS_REFUSES = frozenset(
    {
        socle.WORK_STATUS_EMERGENCY_STOP,
        socle.WORK_STATUS_UPDATING,
        socle.WORK_STATUS_RTT,
        socle.WORK_STATUS_AGING_TEST,
        socle.WORK_STATUS_OFFLINE,
    }
)
# SEC-01b : durée de vie d'une attestation ; aucune option, variable d'environnement ni clé de
# calibration ne la change, ne la pose ni ne la prolonge.
DUREE_ATTESTATION_S = 600.0
MENTION_ATTESTATION = "lames à l'arrêt constatées par l'opérateur, non vérifiées par l'outil (SEC-01b)"


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
    attendu: str  # "translation", "rotation" ou "arc"
    valeur: float  # ordre signé : mètres (positif = avant) ou degrés (positif = gauche)
    angle: float = 0.0  # arc seulement : rotation commandée, en degrés signés (après adoucissement éventuel)
    annonce: str | None = None  # arc seulement : ce que le calcul a dû changer (virage adouci, allure réduite)


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


def _duree(
    consigne: float, vitesse: float, facteur: float, plafond: float, affichage: tuple[float, str, int] | None = None
) -> float:
    """Durée d'émission ; `consigne` et `plafond` dans la même unité (m ou rad)."""
    facteur = _nombre_fini(facteur, "facteur de calibration")
    if facteur <= 0:
        raise RefusSecurite("facteur de calibration invalide")
    # SEC-02 : ce que le robot reçoit réellement (vitesse × durée) est plafonné aussi.
    commande = consigne / facteur
    if commande > plafond * (1 + 1e-9):
        raise RefusSecurite(
            "déplacement commandé au-dessus du plafond une fois le facteur de calibration appliqué"
            + _maximum_demandable(facteur, affichage)
            + " (SEC-02)"
        )
    return _duree_bornee(commande / vitesse)


def _maximum_demandable(facteur: float, affichage: tuple[float, str, int] | None) -> str:
    """« … le maximum demandable par ordre est 142° » : plafond × facteur, arrondi vers le bas."""
    if affichage is None:
        return ""
    plafond, unite, pas_par_unite = affichage
    valeur = math.floor(plafond * facteur * pas_par_unite + 1e-9) / pas_par_unite
    return f" : avec le facteur {facteur:g}, le maximum demandable par ordre est {valeur:g}{unite}"


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
    tenue = _duree(
        abs(metres), v, facteur_distance, DISTANCE_MAX_M,
        (DISTANCE_MAX_M, " m", 100),
    )
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
    tenue = _duree(
        math.radians(abs(degres)), v, facteur_angle, math.radians(ANGLE_MAX_DEG),
        (ANGLE_MAX_DEG, "°", 1),
    )
    return Consigne(0.0, math.copysign(v, degres) * signe, tenue, "rotation", degres)


# Arc : la chenille intérieure garde au moins cette part de la vitesse de l'ordre — elle ne s'arrête ni ne
# s'inverse jamais (demande du propriétaire, 2026-10-12 : virer en douceur, sans patiner).
PART_CHENILLE_INTERIEURE_MIN = 0.20


def consigne_arc(
    metres: float, degres: float, facteur_distance: float = 1.0, facteur_angle: float = 1.0, signe: int = 1,
    voie_m: float = 0.45, *, vitesse: float | None = None,
) -> Consigne:
    """SEC-02 : UNE consigne tenue, vitesse linéaire ET angulaire. `metres` signé (négatif = recul, 2 m au
    plus), `degres` signé (positif = le cap tourne à gauche, 180° au plus), parcourus sur la même durée
    (9 s au plus). Les deux chenilles vont dans le même sens : sinon le virage est adouci, et c'est annoncé.
    Vitesses sous les plafonds linéaire, angulaire, et de chenille (celle d'une ligne droite)."""
    metres, degres = _nombre_fini(metres, "distance"), _nombre_fini(degres, "angle")
    if metres == 0 or abs(metres) > DISTANCE_MAX_M:
        raise RefusSecurite(f"distance refusée : entre 0 (exclu) et {DISTANCE_MAX_M:g} m par ordre (SEC-02)")
    if degres == 0 or abs(degres) > ANGLE_MAX_DEG:
        raise RefusSecurite(f"angle refusé : entre 0 (exclu) et {ANGLE_MAX_DEG:g}° par ordre (SEC-02)")
    for facteur in (facteur_distance, facteur_angle, voie_m):
        if not _nombre_fini(facteur, "facteur de calibration") > 0:
            raise RefusSecurite("facteur de calibration invalide")
    distance, rotation = abs(metres) / facteur_distance, math.radians(abs(degres)) / facteur_angle  # commandés
    if distance > DISTANCE_MAX_M * (1 + 1e-9) or rotation > math.radians(ANGLE_MAX_DEG) * (1 + 1e-9):
        raise RefusSecurite("déplacement commandé au-dessus du plafond une fois le facteur de calibration appliqué (SEC-02)")
    annonces = []
    # Chenille intérieure : v − w·voie/2 ≥ 20 % de v. Ce rapport ne dépend que de la géométrie (angle / distance).
    courbure_max = (1 - PART_CHENILLE_INTERIEURE_MIN) * 2 / voie_m
    obtenu = abs(degres)
    if rotation / distance > courbure_max * (1 + 1e-9):
        rotation = courbure_max * distance
        obtenu = math.degrees(rotation * facteur_angle)
        annonces.append(f"virage adouci : {obtenu:.0f}° au lieu de {abs(degres):g}° (les deux chenilles restent dans le même sens)")
    demandee = _vitesse(vitesse, VITESSE_LINEAIRE_M_S, VITESSE_LINEAIRE_MIN_M_S, "vitesse linéaire")
    exterieure = 1 + rotation / distance * voie_m / 2  # vitesse de la chenille extérieure, rapportée à v
    v = min(demandee, VITESSE_LINEAIRE_M_S / exterieure, VITESSE_ANGULAIRE_RAD_S * distance / rotation)
    if v < VITESSE_LINEAIRE_MIN_M_S * (1 - 1e-9):
        raise RefusSecurite(
            f"arc refusé : pour tenir les plafonds de vitesse il faudrait descendre sous {VITESSE_LINEAIRE_MIN_M_S:g} m/s — "
            "choisissez un angle plus petit ou une distance plus grande (SEC-02)"
        )
    if vitesse is not None and v < demandee * (1 - 1e-9):  # dit seulement si l'opérateur avait choisi sa vitesse
        annonces.append(f"allure réduite à {v:.2f} m/s pour tenir les plafonds de vitesse")
    duree = distance / v
    if duree > DUREE_MAX_S:
        raise RefusSecurite(
            f"arc refusé : il durerait {duree:.1f} s, plus que les {DUREE_MAX_S:g} s permises par ordre — "
            "choisissez une distance plus courte ou un angle plus petit (SEC-02)"
        )
    duree = _duree_bornee(duree)
    w = rotation / duree
    return Consigne(
        math.copysign(v, metres), math.copysign(w, degres) * signe, duree, "arc", metres,
        angle=math.copysign(obtenu, degres), annonce=" ; ".join(annonces) or None,
    )


def arrivee_arc(metres: float, degres: float) -> tuple[float, float]:
    """Où mène un arc, dans le repère du robot au départ : (vers l'avant, vers la gauche), en mètres."""
    angle = math.radians(degres)
    if abs(angle) < 1e-9:
        return metres, 0.0
    rayon = metres / angle
    return rayon * math.sin(angle), rayon * (1 - math.cos(angle))


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



# SEC-12 — qualité de localisation, jugée sur la PRÉCISION HORIZONTALE en mètres annoncée par le robot
# (sous-champ 2 de la localisation). Constat du 2026-10-12 : 0,03 m le matin sur la station ; 1,14 à
# 1,55 m pendant un pilotage après perte du signal, et la tonte reprise ensuite « fait n'importe quoi ».
# Fiable jusqu'à 0,10 m ; dégradée jusqu'à 0,50 m ; perdue au-delà, ou inconnue.
PRECISION_FIABLE_M = 0.10
PRECISION_DEGRADEE_M = 0.50
FIABLE, DEGRADEE, PERDUE = "fiable", "dégradée", "perdue"
# Écart toléré entre la position satellite brute et la position calculée par le robot, quand il est
# mesurable (coordonnées en double précision) : au-delà, la précision annoncée ne suffit pas.
ECART_BRUT_MAX_M = 0.30
ECART_BRUT_AFFICHE_M = 0.20  # au-delà, la carte relie les deux positions par un trait


def qualite_position(precision_m: float | None) -> str:
    if precision_m is None or not math.isfinite(precision_m) or precision_m < 0:
        return PERDUE
    return FIABLE if precision_m <= PRECISION_FIABLE_M else DEGRADEE if precision_m <= PRECISION_DEGRADEE_M else PERDUE


def texte_precision(precision_m: float | None) -> str:
    """« ±1,32 m », ou « précision inconnue » ; virgule décimale, comme le reste de la page."""
    if precision_m is None or not math.isfinite(precision_m) or precision_m < 0:
        return "précision inconnue"
    return f"±{precision_m:.2f} m".replace(".", ",")


def avertissement_position(precision_m: float | None, ecart_m: float | None = None) -> str | None:
    """SEC-12 : ce que risque une tâche automatique lancée dans cet état ; rien si la position est fiable.

    Fiable : précision de 0,10 m ou mieux ET, si l'écart entre position satellite brute et position
    calculée est connu, écart de 0,30 m au plus (constat du 2026-10-12 : la précision est revenue de
    1,32 m à 0,01 m, robot immobile, sans que la pose bouge — on ne sait pas si le filtre s'est recalé)."""
    if qualite_position(precision_m) == FIABLE:
        if ecart_m is not None and math.isfinite(ecart_m) and ecart_m > ECART_BRUT_MAX_M:
            ecart = f"{ecart_m:.2f} m".replace(".", ",")
            return (
                f"Le signal est revenu mais la position calculée par le robot s'écarte de {ecart} de sa "
                "position satellite : recalage conseillé."
            )
        return None
    cible = f"±{PRECISION_FIABLE_M:.2f} m".replace(".", ",")
    return (
        f"Position imprécise ({texte_precision(precision_m)}) : la tonte reprendrait au mauvais endroit. "
        f"Conduisez le robot vers un endroit dégagé jusqu'à {cible}, ou renvoyez-le à la base."
    )


def verifier_precision_pour_reprise(precision_m: float | None, ecart_m: float | None = None) -> None:
    """SEC-12 : pas de reprise à l'aveugle. Une aide, pas un verrou : « reprendre quand même » existe."""
    avertissement = avertissement_position(precision_m, ecart_m)
    if avertissement:
        raise RefusSecurite(f"{avertissement} (SEC-12)")


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
        socle.WORK_STATUS_EMERGENCY_STOP: (
            "arrêt d'urgence enclenché", "levez-le sur le robot ou depuis l'application Lymow",
        ),
        socle.WORK_STATUS_OFFLINE: ("robot inconnu ou hors ligne", "attendez son retour en ligne"),
        None: ("aucun statut reçu du robot", "cliquez sur « Actualiser »"),
    }
    return connus.get(statut, (f"statut {statut} non reconnu comme « lames à l'arrêt »", None))


class Attestation:
    """SEC-01b : « lames à l'arrêt constatées par l'opérateur », en mémoire seulement.

    Jamais active par défaut, jamais écrite sur disque, limitée à `DUREE_ATTESTATION_S` et au
    processus qui la porte. `tracer(evenement, **champs)` reçoit chaque pose, retrait et
    expiration (journal). Ne décide de rien : `statut_admis` porte la règle.
    """

    def __init__(self, horloge: Callable[[], float], tracer: Callable[..., None] | None = None) -> None:
        self._horloge = horloge
        self._tracer = tracer or (lambda _evenement, **_champs: None)
        self._expire: float | None = None

    @property
    def active(self) -> bool:
        if self._expire is not None and self._horloge() >= self._expire:
            self._expire = None
            self._tracer("attestation-expiree", duree_s=DUREE_ATTESTATION_S)
        return self._expire is not None

    @property
    def reste_s(self) -> int:
        return max(0, math.ceil(self._expire - self._horloge())) if self.active and self._expire is not None else 0

    def poser(self, origine: str) -> None:
        self._expire = self._horloge() + DUREE_ATTESTATION_S
        self._tracer("attestation-posee", origine=origine, duree_s=DUREE_ATTESTATION_S, mention=MENTION_ATTESTATION)

    def retirer(self, motif: str) -> bool:
        """Vrai si une attestation était active ; un retrait sans attestation ne trace rien."""
        if not self.active:
            return False
        self._expire = None
        self._tracer("attestation-retiree", motif=motif)
        return True

    def controler(self, statut: int | None, en_ligne: bool | None) -> None:
        """Le robot passe hors ligne ou dans un statut toujours refusé : l'attestation tombe."""
        if en_ligne is False:
            self.retirer("robot hors ligne")
        elif statut is not None and not statut_admis(statut, attestee=True):
            self.retirer(f"{explication_statut(statut)[0]} (statut {statut}) : toujours refusé")


# SEC-01c — décision du propriétaire, 2026-10-13. Constat réel du même jour : la levée de nettoyage du
# plateau (signal 8) met le robot en statut 13 « arrêt d'urgence ». Ce statut est admis dans deux cas
# seulement : l'ordre de sortie « redescendre le plateau », et un pas d'essai « plateau levé » quand cet
# arrêt d'urgence a été posé par l'outil lui-même (jamais pour un arrêt d'urgence d'origine inconnue).
STATUT_PLATEAU_LEVE = socle.WORK_STATUS_EMERGENCY_STOP
DUREE_ESSAI_PLATEAU_LEVE_S = 120.0
PAS_MAX_PLATEAU_LEVE_M = 0.5
ANGLE_MAX_PLATEAU_LEVE_DEG = 45.0
VITESSE_PLATEAU_LEVE_M_S = 0.10
VITESSE_ANGULAIRE_PLATEAU_LEVE_RAD_S = 0.15
DELAI_CONSTAT_NETTOYAGE_S = 10.0
MENTION_PLATEAU_LEVE = "essai de pas plateau levé (SEC-01c, décision du propriétaire, 2026-10-13)"


def verifier_pas_plateau_leve(nom: str, valeur: float | None, angle: float | None) -> None:
    """SEC-01c : plateau levé, seuls un pas droit ou un arc ≤ 0,5 m et une rotation ≤ 45° sont permis."""
    if nom not in ("tourner", "avancer", "reculer", "arc") or valeur is None or not math.isfinite(valeur):
        raise RefusSecurite("plateau levé : seuls un pas droit, un arc ou une rotation, donnés en mètres ou en degrés (SEC-01c)")
    if nom == "tourner":
        if abs(valeur) > ANGLE_MAX_PLATEAU_LEVE_DEG:
            raise RefusSecurite(f"plateau levé : rotation de {ANGLE_MAX_PLATEAU_LEVE_DEG:g}° au plus (SEC-01c)")
        return
    if abs(valeur) > PAS_MAX_PLATEAU_LEVE_M:
        raise RefusSecurite(f"plateau levé : pas de {PAS_MAX_PLATEAU_LEVE_M:g} m au plus (SEC-01c)")
    if angle is not None and not abs(angle) <= ANGLE_MAX_PLATEAU_LEVE_DEG:
        raise RefusSecurite(f"plateau levé : virage de {ANGLE_MAX_PLATEAU_LEVE_DEG:g}° au plus (SEC-01c)")


def statut_admis(statut: int | None, attestee: bool = False, arret_urgence_admis: bool = False) -> bool:
    """SEC-01 : liste blanche ; SEC-01b : ou attestation active ET statut attestable ;
    SEC-01c : ou statut 13 quand l'appelant l'admet (sortie de nettoyage, essai plateau levé)."""
    if arret_urgence_admis and statut == STATUT_PLATEAU_LEVE:
        return True
    return statut in STATUTS_LAMES_ARRET or (attestee and statut in STATUTS_ATTESTABLES)


def verifier_presence_et_statut(
    statut: int | None, en_ligne: bool | None, attestee: bool = False, arret_urgence_admis: bool = False
) -> None:
    """SEC-01 : présence explicitement confirmée et statut dans la liste blanche (ou SEC-01b, SEC-01c)."""
    if en_ligne is False:
        raise RefusSecurite("robot hors ligne : attendez son retour en ligne — mouvement refusé (SEC-01)")
    if en_ligne is not True:
        raise RefusSecurite(
            "présence en ligne du robot non confirmée : cliquez sur « Actualiser » — mouvement refusé (SEC-01)"
        )
    if statut_admis(statut, attestee, arret_urgence_admis):
        return
    constat, conseil = explication_statut(statut)
    if attestee:
        raise RefusSecurite(
            f"{constat} : statut toujours refusé, même sur attestation de l'opérateur — mouvement refusé (SEC-01b)"
        )
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


def verifier_lames_arret(
    statut: int | None, age_s: float | None, en_ligne: bool | None, attestee: bool = False,
    arret_urgence_admis: bool = False,
) -> None:
    """SEC-01 : refuse tout mouvement sans preuve fraîche de lames à l'arrêt.

    SEC-01b : l'attestation ne dispense ni de l'état frais, ni de la présence en ligne."""
    if statut is None or age_s is None:
        raise RefusSecurite(
            "aucun état reçu du robot depuis la sollicitation : cliquez sur « Actualiser » — mouvement refusé (SEC-01)"
        )
    if not 0 <= age_s <= AGE_MAX_ETAT_S:
        raise RefusSecurite(
            f"état périmé ({age_s:.0f} s, maximum {AGE_MAX_ETAT_S:.0f} s) : cliquez sur « Actualiser » — "
            "mouvement refusé (SEC-01)"
        )
    verifier_presence_et_statut(statut, en_ligne, attestee, arret_urgence_admis)
