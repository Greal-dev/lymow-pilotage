"""Pilotage par flèches : les quatre seuls ordres du mode direct, et son armement.

Rôle : traduire une flèche et un pas en UN ordre borné (avancer, reculer, tourner à
gauche, tourner à droite), et tenir l'armement du mode direct : jeton court, renouvelé
à chaque action, file d'attente de cinq étapes au plus.
Frontières : ce module n'émet rien et ne valide aucun invariant de sécurité — chaque
ordre produit ici passe ensuite par les mêmes fonctions que tout autre ordre (SEC-01,
SEC-02, SEC-03). Rien d'autre que ces quatre ordres, aux pas des listes ci-dessous, ne
peut sortir d'ici : ni plateau, ni tâche, ni durée, ni séquence libre ; pour la vitesse,
seulement l'une des allures de la liste (avance et recul), et « forcer ce pas » (SEC-13).
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from typing import Any

from . import garde_carte
from .mouvement import ErreurSaisie, Ordre

DISTANCES_M = (0.25, 0.5, 1.0, 2.0)
ANGLES_DEG = (15.0, 45.0, 90.0)
DISTANCE_DEFAUT_M, ANGLE_DEFAUT_DEG = 1.0, 45.0
# Flèche → (ordre, signe). Tourner : positif = gauche, comme partout dans l'outil.
DIRECTIONS: dict[str, tuple[str, float]] = {
    "haut": ("avancer", 1.0), "bas": ("reculer", 1.0), "gauche": ("tourner", 1.0), "droite": ("tourner", -1.0),
}
# Allure d'une avance ou d'un recul : « auto » laisse l'outil choisir (lente près d'un bord, SEC-13).
ALLURES: dict[str, float | None] = {"auto": None, "lente": 0.10, "normale": 0.25}
# Diagonales : avancer ou reculer en tournant (ordre « arc »). (signe des mètres, signe des degrés) — en marche
# arrière, comme une voiture : « bas-gauche » envoie l'arrière à gauche, donc le cap tourne à DROITE.
DIAGONALES: dict[str, tuple[float, float]] = {
    "haut-gauche": (1.0, 1.0), "haut-droite": (1.0, -1.0), "bas-gauche": (-1.0, -1.0), "bas-droite": (-1.0, 1.0),
}
FILE_MAX = 8  # appuis empilés pendant un pas (5 jusqu'au 2026-10-12)
# Inactivité : cinq minutes sans action sur la page (60 s jusqu'au 2026-10-12 : la main se perdait trop vite).
DUREE_ARMEMENT_S = 300.0
# Deux flèches plus rapides que l'aller-retour avec le serveur : la seconde porte encore le jeton précédent.
JETON_PRECEDENT_S = 2.0


class RefusDirect(Exception):
    """Mode direct non armé, expiré, ou jeton d'armement invalide."""


def ordre(
    direction: object, pas: object, allure: object = "auto", forcer: object = False, facteur_distance: float = 1.0,
    angle: object = None,
) -> Ordre:
    """La flèche et son pas → l'ordre. Tout ce qui n'est pas exactement dans les listes est refusé."""
    if isinstance(direction, str) and direction in DIAGONALES:
        for valeur, permis, nom in ((pas, DISTANCES_M, "distance"), (angle, ANGLES_DEG, "angle")):
            if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or valeur not in permis:
                raise ErreurSaisie(f"{nom} hors liste pour une diagonale : " + ", ".join(f"{v:g}" for v in permis))
        if not isinstance(allure, str) or allure not in ALLURES or forcer is not False:
            raise ErreurSaisie("allure inconnue : auto, lente ou normale")
        signe_m, signe_deg = DIAGONALES[direction]
        vitesse = ALLURES[allure]
        if allure == "lente":  # comme un pas droit : la plus faible vitesse qui tient dans la durée d'un ordre
            vitesse = garde_carte.vitesse_lente(float(pas), facteur_distance)
        return Ordre("arc", signe_m * float(pas), vitesse=vitesse, angle=signe_deg * float(angle))
    if angle is not None:
        raise ErreurSaisie("un angle ne vaut que pour une diagonale")
    if not isinstance(direction, str) or direction not in DIRECTIONS:
        raise ErreurSaisie("flèche inconnue : haut, bas, gauche, droite, ou une diagonale")
    if isinstance(pas, bool) or not isinstance(pas, (int, float)):
        raise ErreurSaisie("pas numérique attendu")
    nom, signe = DIRECTIONS[direction]
    permis = ANGLES_DEG if nom == "tourner" else DISTANCES_M
    if pas not in permis:  # égalité exacte avec une valeur de la liste : aucune valeur intermédiaire
        liste = ", ".join(f"{valeur:g}" for valeur in permis)
        raise ErreurSaisie(f"pas hors liste pour « {nom} » : {liste}")
    if not isinstance(allure, str) or allure not in ALLURES or not isinstance(forcer, bool):
        raise ErreurSaisie("allure inconnue : auto, lente ou normale")
    if nom == "tourner":  # rotation inchangée : ni allure, ni garde-fou carte
        return Ordre(nom, signe * float(pas))
    vitesse = ALLURES[allure]
    if allure == "lente":  # jamais refusée pour sa durée : la plus faible vitesse qui tient (SEC-02)
        vitesse = garde_carte.vitesse_lente(float(pas), facteur_distance)
    return Ordre(nom, signe * float(pas), vitesse=vitesse, force=forcer)


class Armement:
    """État du mode direct pour UNE interface : armé ou non, jeton, file d'attente, étapes faites."""

    def __init__(self, horloge: Callable[[], float]) -> None:
        self.horloge = horloge
        self._jeton: str | None = None
        self._precedent: tuple[str, float] | None = None
        self._expire = 0.0
        self.file: list[Ordre] = []
        self.faites: list[dict[str, Any]] = []

    @property
    def arme(self) -> bool:
        if self._jeton is not None and self.horloge() > self._expire:
            self.desarmer()  # 60 s sans action : désarmé de lui-même
        return self._jeton is not None

    @property
    def reste_s(self) -> int:
        return max(0, round(self._expire - self.horloge())) if self.arme else 0

    def armer(self) -> str:
        self._jeton = self._precedent = None
        self.file.clear()
        self.faites.clear()
        return self.renouveler()

    def renouveler(self) -> str:
        """Nouveau jeton à chaque action : celui d'une page restée ouverte ailleurs ne vaut plus rien."""
        if self._jeton is not None:
            self._precedent = (self._jeton, self.horloge() + JETON_PRECEDENT_S)
        self._jeton = secrets.token_urlsafe(16)
        self.prolonger()
        return self._jeton

    def prolonger(self) -> None:
        self._expire = self.horloge() + DUREE_ARMEMENT_S

    def verifier(self, jeton: object) -> None:
        if not self.arme:
            raise RefusDirect("pilotage direct non armé (ou désarmé après 5 min sans action) : armez-le de nouveau")
        admis = [self._jeton or ""]
        if self._precedent is not None and self.horloge() <= self._precedent[1]:
            admis.append(self._precedent[0])
        if not isinstance(jeton, str) or not any(hmac.compare_digest(jeton.encode(), a.encode()) for a in admis):
            raise RefusDirect("jeton d'armement invalide : pilotage direct désarmé")

    def desarmer(self) -> None:
        self._jeton = self._precedent = None
        self.file.clear()
