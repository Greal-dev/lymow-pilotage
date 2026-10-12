"""Garde-fous de déplacement fondés sur ce que l'outil VOIT : la carte, la pose, les codes du robot.

Rôle :
- SEC-13 (carte) — AVERTISSEMENT par défaut depuis le 2026-10-12 (décision du propriétaire, après un
  essai réel où la position annoncée était fausse et le garde-fou bloquait à tort) ; il ne raccourcit et
  ne refuse que si l'opérateur a coché « Arrêter aux bords de zone (d'après la carte) » :
  avant une avance ou un recul, projeter le pas sur la carte de référence et dire
  s'il reste dans les zones de tonte et les passages, hors zones interdites, en gardant devant lui
  une marge de sécurité (0,30 m + l'incertitude de position annoncée) ; sinon proposer la plus
  grande distance sûre, ou le refus ;
- SEC-14 (indices de choc) : dire, pendant un mouvement, si un code du robot ou l'absence de
  progression de la pose indique un obstacle ou un patinage ;
- allure : dire quand un pas doit se faire lentement.
Frontières : logique pure — rien n'est émis ni refusé ici ; `mouvement` applique. L'outil ne voit
ni les obstacles hors carte, ni les personnes, ni les animaux : on ignore si le robot garde sa
propre détection en pilotage manuel (aucun code de proximité dans 550 trames réelles de pilotage,
2026-10-12).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from . import bon_signal, geometrie, securite
from .carte import Carte
from .geometrie import Point

MARGE_FIXE_M = 0.30
PAS_MIN_M = 0.25  # en dessous, un pas raccourci n'a plus de sens : refus
AGE_POSE_MAX_S = 5.0  # une pose plus ancienne ne dit plus où est le robot : pas de garde-fou, et c'est dit
ECHANTILLON_M = 0.05
PORTEE_BORD_M = 15.0  # au-delà, le bord « devant » n'est plus cherché
ALLURE_LENTE_M_S = 0.10
ALLURE_NORMALE_M_S = securite.VITESSE_LINEAIRE_M_S
BORD_PROCHE_M = 1.0  # un pas qui finit à moins d'un mètre d'un bord se fait lentement
INCHANGE, RACCOURCI, REFUS, INCONNU = "inchangé", "raccourci", "refus", "inconnu"

# SEC-14 : codes du robot qui signalent un choc, un pare-chocs, un capteur à ultrasons, un soulèvement,
# un blocage ou un patinage (libellés du socle ha-lymow). Seule leur APPARITION pendant un mouvement
# arrête : un code déjà présent au départ ne doit pas empêcher de dégager le robot.
ERREURS_DE_CHOC = frozenset({7, 8, 17, 19, 21, 44, 59, 60, 66, 74, 75, 76, 77, 83, 84})
AVERTISSEMENTS_DE_CHOC = frozenset({5, 6, 7, 8, 24, 30})
FENETRE_PROGRESSION_S = 1.5
PROGRESSION_MIN = 0.20  # part du déplacement attendu en dessous de laquelle le robot est tenu pour bloqué
ETABLISSEMENT_S = 1.0  # la vitesse n'est jugée établie qu'après ce délai
CADENCE_POSE_MAX_S = 1.0  # au-delà de cet intervalle entre deux poses, la progression n'est pas jugeable


def marge_de_securite(precision_m: float | None) -> float:
    """0,30 m, plus l'incertitude de position annoncée (inconnue : comptée comme « dégradée »)."""
    connue = precision_m is not None and math.isfinite(precision_m) and precision_m >= 0
    return MARGE_FIXE_M + (precision_m if connue else securite.PRECISION_DEGRADEE_M)


def _sur(terrain: Any, p: Point) -> bool:
    """Le point est-il dans une zone de tonte ou sur un passage, et hors de toute zone interdite ?"""
    x, y = p
    for contour, x0, x1, y0, y1 in terrain.interdites:
        if len(contour) >= 3 and x0 <= x <= x1 and y0 <= y <= y1 and geometrie.contient(contour, p):
            return False
    for contour, x0, x1, y0, y1 in terrain.zones:
        if x0 <= x <= x1 and y0 <= y <= y1 and len(contour) >= 3 and geometrie.contient(contour, p):
            return True
    return any(
        x0 <= x <= x1 and y0 <= y <= y1 and bon_signal._distance_au_trace(trace, p) <= bon_signal.LARGEUR_PASSAGE_M
        for trace, x0, x1, y0, y1 in terrain.passages
    )


def _direction(cap_deg: float, sens: float) -> Point:
    return sens * math.cos(math.radians(cap_deg)), sens * math.sin(math.radians(cap_deg))


def bord_devant(carte: Carte | None, depart: Point | None, cap_deg: float | None, sens: float = 1.0) -> float | None:
    """Distance, dans l'axe du robot, au premier bord : sortie des zones et passages, ou entrée en zone interdite.

    Rien si elle est inconnue (pas de carte, pas de pose, robot hors des zones) ; plafonnée à 15 m."""
    if carte is None or depart is None or cap_deg is None:
        return None
    terrain = bon_signal._terrain(carte)
    if not _sur(terrain, depart):
        return None
    dx, dy = _direction(cap_deg, sens)
    sure = 0.0
    for i in range(1, math.ceil(PORTEE_BORD_M / ECHANTILLON_M) + 1):
        d = i * ECHANTILLON_M
        if not _sur(terrain, (depart[0] + dx * d, depart[1] + dy * d)):
            break
        sure = d
    return round(sure, 2)


@dataclass(frozen=True)
class Jugement:
    decision: str  # inchangé, raccourci, refus, inconnu
    distance_m: float  # distance retenue (celle demandée si rien ne change)
    motif: str
    bord_m: float | None = None
    arrivee_au_bord_m: float | None = None  # distance au bord, dans l'axe, depuis le point d'arrivée

    @property
    def bloque(self) -> bool:
        return self.decision == REFUS


def metres(valeur: float) -> str:
    return (f"{valeur:.2f}".rstrip("0").rstrip(".") + " m").replace(".", ",")


def juger_pas(
    carte: Carte | None, depart: Point | None, cap_deg: float | None, distance_m: float, precision_m: float | None,
    age_pose_s: float | None = 0.0,
) -> Jugement:
    """SEC-13 : `distance_m` signée (positive = avance). Sans pose ou sans carte : aucun garde-fou, dit clairement."""
    demandee = abs(distance_m)
    if carte is None:
        return Jugement(INCONNU, demandee, "distance au bord inconnue : aucune carte de référence")
    if depart is None or cap_deg is None:
        return Jugement(INCONNU, demandee, "distance au bord inconnue : position du robot non fournie")
    if age_pose_s is None or not age_pose_s <= AGE_POSE_MAX_S:
        return Jugement(INCONNU, demandee, "distance au bord inconnue : position du robot trop ancienne")
    sens = 1.0 if distance_m >= 0 else -1.0
    bord = bord_devant(carte, depart, cap_deg, sens)
    if bord is None:
        return Jugement(INCONNU, demandee, "distance au bord inconnue : le robot est hors des zones de la carte")
    # La marge se compte DANS L'AXE du pas : pas sûr = distance au bord − marge. Un bord latéral proche n'est
    # pas jugé (le pas ne rapproche pas de lui) : limite connue, dite au README.
    marge = marge_de_securite(precision_m)
    sure = bord - marge
    devant = "devant" if sens > 0 else "derrière"
    if sure >= demandee - 1e-6:
        return Jugement(INCHANGE, demandee, f"bord de zone {devant} : {metres(bord)}", bord, round(bord - demandee, 2))
    detail = f"bord de zone {devant} à {metres(bord)} (marge de sécurité {metres(marge)})"
    reduite = math.floor(sure / ECHANTILLON_M + 1e-6) * ECHANTILLON_M
    if reduite >= PAS_MIN_M:
        return Jugement(RACCOURCI, round(reduite, 2), f"pas réduit à {metres(reduite)} : {detail}", bord, round(bord - reduite, 2))
    return Jugement(REFUS, 0.0, f"pas refusé : {detail}", bord, None)


def constat(jugement: Jugement, distance_m: float) -> tuple[str, bool]:
    """Mode avertissement : ce que dit la carte, sans rien décider — (texte, alerte si le bord est avant la fin du pas)."""
    if jugement.decision == INCONNU or jugement.bord_m is None:
        return jugement.motif, False
    devant = "devant" if distance_m >= 0 else "derrière"
    return f"d'après la carte : bord de zone {devant} à {metres(jugement.bord_m)}", jugement.bord_m < abs(distance_m)


def annoncer(
    carte: Carte | None, depart: Point | None, cap_deg: float | None, precision_m: float | None,
    age_pose_s: float | None, etapes: list[tuple[str, float | None]], arreter: bool = False,
) -> list[str]:
    """SEC-13, dès la prévisualisation : ce que la carte dit de chaque avance ou recul de la séquence, la pose étant
    propagée d'étape en étape (rotations comprises). `arreter` : le garde-fou raccourcira ou refusera ; sinon ce
    n'est qu'un avertissement, et le pas partira en entier. Indicatif : l'émission rejuge chaque pas."""
    annonces: list[str] = []
    position, cap = depart, cap_deg
    for numero, (nom, valeur) in enumerate(etapes, 1):
        if nom not in ("avancer", "reculer", "tourner"):
            continue
        if valeur is None:
            break  # étape tenue à la durée : la suite n'est plus prévisible
        if nom == "tourner":
            cap = None if cap is None else cap + valeur
            continue
        sens = 1.0 if nom == "avancer" else -1.0
        jugement = juger_pas(carte, position, cap, sens * valeur, precision_m, age_pose_s)
        if jugement.decision == INCONNU:
            annonces.append(jugement.motif)
            break
        parcouru = jugement.distance_m
        if not arreter:
            texte, alerte = constat(jugement, sens * valeur)
            if alerte:
                annonces.append(f"étape {numero} : {texte} (pas demandé : {metres(valeur)}) — le pas partira en entier")
            parcouru = valeur
        elif jugement.decision != INCHANGE:
            annonces.append(f"étape {numero} : {jugement.motif}")
            if jugement.bloque:
                break
        dx, dy = _direction(cap, sens)
        position = (position[0] + dx * parcouru, position[1] + dy * parcouru)
    return annonces


def vitesse_lente(distance_m: float, facteur_distance: float = 1.0) -> float:
    """Vitesse de l'allure lente pour ce pas : 0,10 m/s, ou la plus faible qui tient dans la durée maximale
    d'un ordre (SEC-02) — le pas garde sa longueur, il n'est jamais refusé pour cela."""
    plancher = math.ceil(abs(distance_m) / (securite.DUREE_MAX_S * facteur_distance) * 100 - 1e-9) / 100
    return min(max(ALLURE_LENTE_M_S, plancher), ALLURE_NORMALE_M_S)


def annonce_vitesse(vitesse: float, distance_m: float) -> str:
    if vitesse <= ALLURE_LENTE_M_S:
        return ""
    return f"{vitesse:g} m/s pour ce pas de {abs(distance_m):g} m ({securite.DUREE_MAX_S:g} s au plus par ordre)"


def allure(vitesse_demandee: float | None, jugement: Jugement, precision_m: float | None) -> tuple[float | None, str | None]:
    """Allure du pas : (vitesse imposée ou None, annonce). Lente d'office près d'un bord de zone, seulement quand la
    position est fiable — sinon la carte n'est pas crédible et l'allure reste normale (décision du propriétaire,
    2026-10-12). Jamais si l'opérateur a choisi sa vitesse."""
    if vitesse_demandee is not None:
        return None, None
    if precision_m is None or securite.qualite_position(precision_m) != securite.FIABLE:
        return None, None
    if jugement.arrivee_au_bord_m is not None and jugement.arrivee_au_bord_m < BORD_PROCHE_M:
        return ALLURE_LENTE_M_S, f"allure lente : l'arrivée est à moins de {metres(BORD_PROCHE_M)} d'un bord de zone"
    return None, None


def codes_de_choc(etat: dict[str, Any]) -> frozenset[tuple[str, int]]:
    erreurs = {("erreur", c) for c in etat.get("errorCodes") or [] if c in ERREURS_DE_CHOC}
    avertissements = {("avertissement", c) for c in etat.get("warningCodes") or [] if c in AVERTISSEMENTS_DE_CHOC}
    return frozenset(erreurs | avertissements)


class SuiviDeProgression:
    """SEC-14 (c) : la pose avance-t-elle pendant que les consignes continuent ?

    Jugée seulement à vitesse établie et quand la pose arrive assez souvent ; sinon inactive, et dite telle.
    """

    def __init__(self, vitesse_m_s: float, debut: float) -> None:
        self.vitesse = abs(vitesse_m_s)
        self.debut = debut
        self.poses: list[tuple[float, float, float]] = []  # instant, est, nord
        self.active = False

    def noter(self, instant: float, est: float, nord: float) -> None:
        if not self.poses or instant > self.poses[-1][0]:
            self.poses.append((instant, est, nord))

    def bloque(self, maintenant: float) -> bool:
        """Vrai si, sur la dernière fenêtre, la progression est inférieure à 20 % de l'attendu."""
        depuis = maintenant - FENETRE_PROGRESSION_S
        if depuis < self.debut + ETABLISSEMENT_S:
            return False
        fenetre = [p for p in self.poses if p[0] >= depuis - 1e-9]
        if len(fenetre) < 3 or fenetre[0][0] > depuis + CADENCE_POSE_MAX_S or maintenant - fenetre[-1][0] > CADENCE_POSE_MAX_S:
            return False
        if any(b[0] - a[0] > CADENCE_POSE_MAX_S for a, b in zip(fenetre, fenetre[1:])):
            return False
        self.active = True
        duree = fenetre[-1][0] - fenetre[0][0]
        parcouru = math.hypot(fenetre[-1][1] - fenetre[0][1], fenetre[-1][2] - fenetre[0][2])
        return duree > 0 and parcouru < PROGRESSION_MIN * self.vitesse * duree
