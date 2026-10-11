"""Session de dialogue avec un robot : point de passage unique des publications.

Rôle : filtrer ce qui a le droit de partir (catégories, requêtes pures),
refuser d'émettre sans liaison, tenir l'état à jour. Indépendant du transport :
la liaison réelle (`liaison`) et les faux clients de test offrent la même forme
(`connecte`, `publier(sujet, charge, delai=…)`, et si possible `reconnecter()`).
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from . import protocole
from .config import Calibration
from .etat import Etat
from .journal import Journal
from .protocole import Categorie, Trame
from .securite import RefusSecurite

PAS_ATTENTE_S = 0.2


class EchecOrdre(Exception):
    """Un ordre réel n'a pas pu être mené à bien (liaison, acquittement)."""


def _avertir(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Session:
    def __init__(
        self,
        lien: Any,
        appareil: str,
        journal: Journal,
        cal: Calibration,
        autorisees: frozenset[Categorie],
        *,
        horloge: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        signaler: Callable[[str], None] = _avertir,
    ) -> None:
        self.lien = lien
        self.sujet = protocole.sujet_ordres(appareil)
        self.journal = journal
        self.cal = cal
        self.autorisees = autorisees
        self.horloge = horloge
        self.dormir = dormir
        self.signaler = signaler
        self.etat = Etat()
        self._identifiant = uuid.uuid4().hex

    def recevoir_etat(self, fragment: dict[str, Any]) -> None:
        instant = self.horloge()
        self.etat.appliquer(fragment, instant)
        self.journal.ecrire("etat-recu", **self.etat.resume(instant))  # SEC-06

    def recevoir_presence(self, en_ligne: bool) -> None:
        self.etat.en_ligne = en_ligne
        self.journal.ecrire("presence-recue", en_ligne=en_ligne)

    async def publier(self, trame: Trame, delai: float) -> None:
        """Publie en QoS 1 et attend l'acquittement ; lève plutôt que d'abandonner en silence."""
        if trame.categorie not in self.autorisees:
            raise RefusSecurite(f"émission « {trame.categorie.value} » interdite dans cette commande")
        if trame.categorie is Categorie.REQUETE_PURE and not protocole.est_requete_pure(trame.octets):
            raise RefusSecurite("message hors de la liste blanche des requêtes pures")
        # SEC-03 : refus d'émettre si MQTT n'est pas connecté.
        if not self.lien.connecte:
            raise EchecOrdre("MQTT non connecté : rien n'a été émis")
        try:
            await self.lien.publier(self.sujet, trame.charge(), delai=delai)
        except asyncio.CancelledError:
            raise
        except Exception as erreur:  # noqa: BLE001 — toute panne de liaison compte (SEC-03)
            raise EchecOrdre(f"publication non acquittée ({type(erreur).__name__})") from erreur

    async def solliciter(self) -> None:
        """Demande un état frais par requêtes pures, si la commande y est autorisée."""
        if Categorie.REQUETE_PURE not in self.autorisees:
            return
        for trame in (
            protocole.trame_presence(self._identifiant),
            protocole.trame_demande_configuration(self.cal),
        ):
            try:
                await self.publier(trame, 3.0)
                self.journal.ecrire("requete-emise", sujet=self.sujet, trame=trame.description())
            except EchecOrdre:
                return  # au mieux : l'écoute passive continue

    async def attendre(self, quoi: str, apres: float | None, delai: float) -> bool:
        """Vrai si un `statut` ou une `pose` est arrivé après l'instant `apres`."""
        fin = self.horloge() + delai
        while True:
            recu = self.etat.recu_pose if quoi == "pose" else self.etat.recu_statut
            if recu is not None and (apres is None or recu > apres):
                return True
            if self.horloge() >= fin or not self.lien.connecte:
                return False
            await self.dormir(PAS_ATTENTE_S)
