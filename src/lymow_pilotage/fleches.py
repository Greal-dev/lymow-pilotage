"""Pilotage par flèches : les quatre seuls ordres du mode direct, et son armement.

Rôle : traduire une flèche et un pas en UN ordre borné (avancer, reculer, tourner à
gauche, tourner à droite), et tenir l'armement du mode direct : jeton court, renouvelé
à chaque action, file d'attente de cinq étapes au plus.
Frontières : ce module n'émet rien et ne valide aucun invariant de sécurité — chaque
ordre produit ici passe ensuite par les mêmes fonctions que tout autre ordre (SEC-01,
SEC-02, SEC-03). Rien d'autre que ces quatre ordres, aux pas des listes ci-dessous, ne
peut sortir d'ici : ni plateau, ni tâche, ni vitesse, ni durée, ni séquence libre.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from typing import Any

from .mouvement import ErreurSaisie, Ordre

DISTANCES_M = (0.25, 0.5, 1.0, 2.0)
ANGLES_DEG = (15.0, 45.0, 90.0)
DISTANCE_DEFAUT_M, ANGLE_DEFAUT_DEG = 1.0, 45.0
# Flèche → (ordre, signe). Tourner : positif = gauche, comme partout dans l'outil.
DIRECTIONS: dict[str, tuple[str, float]] = {
    "haut": ("avancer", 1.0), "bas": ("reculer", 1.0), "gauche": ("tourner", 1.0), "droite": ("tourner", -1.0),
}
FILE_MAX = 5
DUREE_ARMEMENT_S = 60.0


class RefusDirect(Exception):
    """Mode direct non armé, expiré, ou jeton d'armement invalide."""


def ordre(direction: object, pas: object) -> Ordre:
    """La flèche et son pas → l'ordre. Tout ce qui n'est pas exactement dans les listes est refusé."""
    if not isinstance(direction, str) or direction not in DIRECTIONS:
        raise ErreurSaisie("flèche inconnue : haut, bas, gauche ou droite")
    if isinstance(pas, bool) or not isinstance(pas, (int, float)):
        raise ErreurSaisie("pas numérique attendu")
    nom, signe = DIRECTIONS[direction]
    permis = ANGLES_DEG if nom == "tourner" else DISTANCES_M
    if pas not in permis:  # égalité exacte avec une valeur de la liste : aucune valeur intermédiaire
        liste = ", ".join(f"{valeur:g}" for valeur in permis)
        raise ErreurSaisie(f"pas hors liste pour « {nom} » : {liste}")
    return Ordre(nom, signe * float(pas))


class Armement:
    """État du mode direct pour UNE interface : armé ou non, jeton, file d'attente, étapes faites."""

    def __init__(self, horloge: Callable[[], float]) -> None:
        self.horloge = horloge
        self._jeton: str | None = None
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
        self.file.clear()
        self.faites.clear()
        return self.renouveler()

    def renouveler(self) -> str:
        """Nouveau jeton à chaque action : celui d'une page restée ouverte ailleurs ne vaut plus rien."""
        self._jeton = secrets.token_urlsafe(16)
        self.prolonger()
        return self._jeton

    def prolonger(self) -> None:
        self._expire = self.horloge() + DUREE_ARMEMENT_S

    def verifier(self, jeton: object) -> None:
        if not self.arme:
            raise RefusDirect("pilotage direct non armé (ou désarmé après 60 s sans action) : armez-le de nouveau")
        if not isinstance(jeton, str) or not hmac.compare_digest(jeton.encode(), (self._jeton or "").encode()):
            raise RefusDirect("jeton d'armement invalide : pilotage direct désarmé")

    def desarmer(self) -> None:
        self._jeton = None
        self.file.clear()
