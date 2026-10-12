"""Décalage entre la carte du robot et ses positions : mesure à la station, recalage d'affichage par l'opérateur.

Rôle :
- mesurer, à chaque arrivée à la station de charge, l'écart entre la position annoncée par le robot et la
  station de la carte (un écart stable et non nul = carte décalée) ;
- tenir le décalage d'AFFICHAGE choisi par l'opérateur (« Le robot est ici ») et l'appliquer aux positions
  que l'outil place sur la carte ; le mémoriser, daté, dans le dossier de données.
Frontières : rien n'est envoyé au robot, qui navigue toujours avec sa propre carte ; aucune règle de
sécurité ne vit ici. Constat du propriétaire (2026-10-12) : la position affichée était décalée d'environ
un mètre par rapport au robot réel.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .vendor.ha_lymow import const as socle

FICHIER = "recalage.json"
DECALAGE_MAX_M = 20.0  # au-delà, ce n'est plus un recalage : la demande est refusée
SEUIL_M = 0.05  # en dessous, une composante n'est pas dite
POSE_RECENTE_S = 60.0  # le robot cesse d'annoncer sa position une fois posé : la dernière pose récente compte
EN_CHARGE = frozenset({socle.WORK_STATUS_CHARGING, socle.WORK_STATUS_CHARGING_FULL})

Vecteur = tuple[float, float]


def _nombre(valeur: float) -> str:
    return f"{abs(valeur):.2f}".rstrip("0").rstrip(".").replace(".", ",") + " m"


def texte(decalage: Vecteur) -> str:
    """« 1,2 m vers l'est, 0,4 m vers le nord » ; « aucun » si tout est sous 5 cm."""
    est, nord = decalage
    morceaux = []
    if abs(est) >= SEUIL_M:
        morceaux.append(_nombre(est) + (" vers l'est" if est > 0 else " vers l'ouest"))
    if abs(nord) >= SEUIL_M:
        morceaux.append(f"{_nombre(nord)} vers le {'nord' if nord > 0 else 'sud'}")
    return ", ".join(morceaux) or "aucun"


def appliquer(point: Vecteur | None, decalage: Vecteur | None) -> Vecteur | None:
    """La position annoncée, déplacée du décalage d'affichage ; inchangée sans décalage."""
    if point is None or decalage is None:
        return point
    return point[0] + decalage[0], point[1] + decalage[1]


def decalage_vers(annoncee: Vecteur, indiquee: Vecteur) -> Vecteur:
    """« Le robot est ici » : ce qu'il faut ajouter à la position annoncée pour tomber sur l'endroit indiqué."""
    est, nord = indiquee[0] - annoncee[0], indiquee[1] - annoncee[1]
    if not (math.isfinite(est) and math.isfinite(nord)) or math.hypot(est, nord) > DECALAGE_MAX_M:
        raise ValueError(f"décalage refusé : {DECALAGE_MAX_M:g} m au plus entre la position annoncée et l'endroit indiqué")
    return round(est, 2), round(nord, 2)


def _date(horodatage: Any) -> str:
    try:
        instant = datetime.fromisoformat(str(horodatage))
    except ValueError:
        return "date inconnue"
    return f"le {instant:%d/%m} à {instant:%H:%M}"


class SuiviStation:
    """Mesure de l'écart carte / robot à chaque arrivée à la station (statut vivant « en charge »)."""

    def __init__(self) -> None:
        self._precedent: int | None = None
        self._pose: tuple[float, float, float, float | None] | None = None  # est, nord, instant, précision

    def observer(
        self, statut: int | None, pose: Vecteur | None, instant_pose: float | None, precision_m: float | None,
        station: Vecteur | None, maintenant: float,
    ) -> dict[str, Any] | None:
        """Une mesure à l'instant où le robot passe « en charge », sur sa dernière position récente ; sinon rien."""
        if pose is not None and instant_pose is not None and (self._pose is None or instant_pose > self._pose[2]):
            self._pose = (pose[0], pose[1], instant_pose, precision_m)
        arrivee = statut in EN_CHARGE and self._precedent is not None and self._precedent not in EN_CHARGE
        if statut is not None:
            self._precedent = statut
        if not arrivee or station is None or self._pose is None or maintenant - self._pose[2] > POSE_RECENTE_S:
            return None
        est, nord = round(self._pose[0] - station[0], 2), round(self._pose[1] - station[1], 2)
        return {
            "est_m": est, "nord_m": nord, "norme_m": round(math.hypot(est, nord), 2),
            "precision_m": None if self._pose[3] is None else round(self._pose[3], 2),
            "horodatage": datetime.now().isoformat(timespec="seconds"),
        }


def texte_mesure(mesure: dict[str, Any] | None) -> str:
    """« écart carte/robot mesuré à la station : 0,9 m vers l'est, le 12/10 à 20:40 »."""
    if not mesure:
        return ""
    return f"écart carte/robot mesuré à la station : {texte((mesure['est_m'], mesure['nord_m']))}, {_date(mesure.get('horodatage'))}"


class Memoire:
    """Décalage d'affichage et dernière mesure à la station, mémorisés dans le dossier de données."""

    def __init__(self, dossier: Path | None) -> None:
        self.chemin = None if dossier is None else dossier / FICHIER
        self.decalage: Vecteur | None = None
        self.date: str | None = None
        self.source: str | None = None
        self.mesure: dict[str, Any] | None = None
        self._lire()

    def _lire(self) -> None:
        try:
            lu = json.loads(self.chemin.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(lu, dict):
            return
        d = lu.get("decalage")
        if isinstance(d, dict) and all(isinstance(d.get(c), (int, float)) and not isinstance(d.get(c), bool) for c in ("est_m", "nord_m")):
            if math.hypot(d["est_m"], d["nord_m"]) <= DECALAGE_MAX_M:
                self.decalage, self.date, self.source = (float(d["est_m"]), float(d["nord_m"])), str(d.get("horodatage")), str(d.get("source"))
        m = lu.get("mesure_station")
        if isinstance(m, dict) and all(isinstance(m.get(c), (int, float)) and not isinstance(m.get(c), bool) for c in ("est_m", "nord_m")):
            self.mesure = m

    def _ecrire(self) -> bool:
        contenu = {
            "decalage": None if self.decalage is None else {
                "est_m": self.decalage[0], "nord_m": self.decalage[1], "horodatage": self.date, "source": self.source,
            },
            "mesure_station": self.mesure,
        }
        try:
            self.chemin.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            provisoire = self.chemin.with_suffix(".json.tmp")  # type: ignore[union-attr]
            provisoire.write_text(json.dumps(contenu, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            provisoire.replace(self.chemin)  # type: ignore[arg-type]
        except (OSError, AttributeError):
            return False
        return True

    def poser(self, decalage: Vecteur, source: str) -> bool:
        self.decalage, self.source = decalage, source
        self.date = datetime.now().isoformat(timespec="seconds")
        return self._ecrire()

    def annuler(self) -> bool:
        self.decalage = self.date = self.source = None
        return self._ecrire()

    def noter_mesure(self, mesure: dict[str, Any]) -> bool:
        self.mesure = mesure
        return self._ecrire()

    def decalage_de_la_mesure(self) -> Vecteur | None:
        """Le robot s'annonce à (station + écart) quand il est SUR la station : l'affichage se corrige de −écart."""
        if not self.mesure:
            return None
        return -float(self.mesure["est_m"]), -float(self.mesure["nord_m"])

    def resume(self) -> dict[str, Any]:
        return {
            "actif": self.decalage is not None,
            "est_m": None if self.decalage is None else self.decalage[0],
            "nord_m": None if self.decalage is None else self.decalage[1],
            "texte": "" if self.decalage is None else (
                f"carte recalée par l'opérateur — décalage appliqué : {texte(self.decalage)} ({_date(self.date)})"
            ),
            "mesure_station": texte_mesure(self.mesure),
        }
