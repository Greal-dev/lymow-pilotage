"""Position de nettoyage du plateau « posée par l'outil », et essai de pas plateau levé (SEC-01c).

Constat réel du 2026-10-13 : la levée de nettoyage (signal 8) lève le plateau et met le robot en statut 13
« arrêt d'urgence ». Ce module retient que CET arrêt d'urgence vient de l'outil — en mémoire et dans le
dossier de données, pour survivre à un redémarrage de l'interface — et porte l'autorisation temporaire
d'essayer un pas plateau levé (décision du propriétaire, 2026-10-13).

Frontières : il ne décide d'aucun mouvement (`securite.statut_admis` porte la règle, `mouvement` les
plafonds) et n'émet rien. Un arrêt d'urgence d'origine inconnue n'est jamais « posé par l'outil ».
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import securite
from .securite import RefusSecurite

FICHIER = "nettoyage.json"
TEXTE = "Plateau en position de nettoyage (le robot se déclare en arrêt d'urgence)"
CONFIRMATION_ESSAI = (
    "Le robot se déclare en arrêt d'urgence parce que le plateau est en position de nettoyage. "
    "Personne ne doit se trouver près du robot. Il ignorera peut-être l'ordre."
)


class PositionDeNettoyage:
    """`tracer(evenement, **champs)` reçoit chaque pose, effacement, autorisation, retrait et expiration."""

    def __init__(
        self, dossier: Path | None, horloge: Callable[[], float], tracer: Callable[..., None] | None = None
    ) -> None:
        self.chemin = None if dossier is None else dossier / FICHIER
        self._horloge = horloge
        self._tracer = tracer or (lambda _evenement, **_champs: None)
        self._emis: float | None = None  # signal 8 émis depuis un statut autre que 13 : instant
        self._essai_expire: float | None = None
        self.posee: dict[str, Any] | None = None  # {"horodatage", "appareil"}
        self._lire()

    def _lire(self) -> None:
        try:
            lu = json.loads(self.chemin.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, ValueError, AttributeError):
            return
        if isinstance(lu, dict) and isinstance(lu.get("appareil"), str) and isinstance(lu.get("horodatage"), str):
            self.posee = {"horodatage": lu["horodatage"], "appareil": lu["appareil"]}

    def _ecrire(self) -> None:
        if self.chemin is None:
            return
        try:
            if self.posee is None:
                self.chemin.unlink(missing_ok=True)
                return
            self.chemin.parent.mkdir(parents=True, exist_ok=True)
            provisoire = self.chemin.with_suffix(".json.tmp")
            provisoire.write_text(json.dumps(self.posee, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            provisoire.replace(self.chemin)
        except OSError:  # disque indisponible : la mémoire du processus fait foi
            return

    def noter_emission(self, statut_avant: int | None) -> None:
        """La levée de nettoyage vient d'être émise : un statut 13 dans les 10 s viendra de l'outil."""
        if statut_avant != securite.STATUT_PLATEAU_LEVE:
            self._emis = self._horloge()

    def observer(self, statut: int | None, appareil: str | None) -> None:
        """À chaque état reçu : pose (13 dans les 10 s après l'émission) ou effacement (le statut a quitté 13)."""
        if statut is None:
            return
        if statut != securite.STATUT_PLATEAU_LEVE:
            if self.posee is not None:
                self.posee = None
                self._ecrire()
                self.retirer_essai("le robot a quitté l'arrêt d'urgence")
                self._tracer("nettoyage-efface", statut=statut)
            return
        if self.posee is not None and appareil is not None and self.posee["appareil"] != appareil:
            self.posee = None  # mémorisée pour un autre robot : origine inconnue pour celui-ci
            self._ecrire()
        emis, self._emis = self._emis, None
        recent = emis is not None and self._horloge() - emis <= securite.DELAI_CONSTAT_NETTOYAGE_S
        if self.posee is None and recent and appareil is not None:
            self.posee = {"horodatage": datetime.now().isoformat(timespec="seconds"), "appareil": appareil}
            self._ecrire()
            self._tracer("nettoyage-pose", **self.posee)

    # --- essai de pas plateau levé (SEC-01c) --------------------------------------

    @property
    def essai_actif(self) -> bool:
        if self._essai_expire is not None and self._horloge() >= self._essai_expire:
            self._essai_expire = None
            self._tracer("essai-plateau-leve-expire", regle="SEC-01c", duree_s=securite.DUREE_ESSAI_PLATEAU_LEVE_S)
        return self._essai_expire is not None and self.posee is not None

    @property
    def essai_reste_s(self) -> int:
        return max(0, math.ceil(self._essai_expire - self._horloge())) if self.essai_actif and self._essai_expire else 0

    def autoriser_essai(self) -> None:
        """SEC-01c : jamais pour un arrêt d'urgence d'origine inconnue."""
        if self.posee is None:
            raise RefusSecurite(
                "essai de pas plateau levé refusé : cet arrêt d'urgence n'a pas été posé par l'outil (SEC-01c)"
            )
        self._essai_expire = self._horloge() + securite.DUREE_ESSAI_PLATEAU_LEVE_S
        self._tracer(
            "essai-plateau-leve-autorise", regle="SEC-01c", duree_s=securite.DUREE_ESSAI_PLATEAU_LEVE_S,
            mention=securite.MENTION_PLATEAU_LEVE,
        )

    def retirer_essai(self, motif: str) -> bool:
        if not self.essai_actif:
            self._essai_expire = None
            return False
        self._essai_expire = None
        self._tracer("essai-plateau-leve-retire", regle="SEC-01c", motif=motif)
        return True
