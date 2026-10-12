"""Session de dialogue avec un robot : point de passage unique des publications.

Rôle : filtrer ce qui a le droit de partir (catégories, requêtes pures),
refuser d'émettre sans liaison, borner chaque publication, tenir l'état à jour.
Indépendant du transport : la liaison réelle (`liaison`) et les faux clients de
test offrent la même forme (`connecte`, `publier(sujet, charge, delai=…)`, et
si possible `reconnecter()`).
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
DELAI_REQUETE_S = 3.0


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
        self.carte_brute: tuple[bytes, float] | None = None
        self.arret_en_cours = False
        self._differes: list[tuple[str, dict[str, Any]]] = []
        self._identifiant = uuid.uuid4().hex

    def journaliser(self, evenement: str, **champs: Any) -> None:
        """SEC-03 : pendant un arrêt, rien n'est écrit sur disque ; les lignes attendent."""
        if self.arret_en_cours:
            self._differes.append((evenement, champs | {"ecrit_apres_arret": True}))
        else:
            self.journal.ecrire(evenement, **champs)

    def fin_arret(self) -> None:
        self.arret_en_cours = False
        differes, self._differes = self._differes, []
        for evenement, champs in differes:
            self.journal.ecrire(evenement, **champs)

    def recevoir_etat(self, fragment: dict[str, Any], retenu: bool = False) -> None:
        if retenu:
            # SEC-01 : un message conservé par le serveur n'a pas d'âge connu.
            self.journaliser("etat-retenu-ignore")
            return
        instant = self.horloge()
        fragment = dict(fragment)
        carte = fragment.pop("mapRaw", None)
        if isinstance(carte, bytes):
            # Octets gardés tels quels : la sauvegarde (SEC-09) et la comparaison (SEC-11) en dépendent.
            self.carte_brute = (carte, instant)
            self.journaliser("carte-recue", octets=len(carte))
        self.etat.appliquer(fragment, instant)
        # Un état émis à l'instant par le robot vaut preuve de présence.
        self.etat.en_ligne = True
        self.journaliser("etat-recu", **self.etat.resume(instant))  # SEC-06

    def recevoir_presence(self, en_ligne: bool, retenu: bool = False) -> None:
        if not retenu:
            self.etat.en_ligne = en_ligne
        self.journaliser("presence-recue", en_ligne=en_ligne, retenu=retenu)

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
            # SEC-03 : le délai est tenu ici aussi, au cas où la liaison ne rendrait jamais la main.
            await asyncio.wait_for(self.lien.publier(self.sujet, trame.charge(), delai=delai), delai)
        except asyncio.CancelledError:
            raise
        except Exception as erreur:  # noqa: BLE001 — toute panne de liaison compte (SEC-03)
            raise EchecOrdre(f"publication non acquittée ({type(erreur).__name__})") from erreur

    async def solliciter(self) -> float | None:
        """Demande un état frais par requêtes pures ; rend l'instant de la demande, ou rien."""
        if Categorie.REQUETE_PURE not in self.autorisees:
            return None
        instant = self.horloge()
        emises = 0
        for trame in (
            protocole.trame_presence(self._identifiant),
            protocole.trame_demande_configuration(self.cal),
        ):
            try:
                await self.publier(trame, DELAI_REQUETE_S)
            except EchecOrdre:
                break  # au mieux : l'écoute passive continue
            emises += 1
            self.journaliser("requete-emise", sujet=self.sujet, trame=trame.description())
        return instant if emises else None

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
