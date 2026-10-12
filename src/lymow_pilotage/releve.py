"""Relevé local de la précision de position, pour voir où le signal décroche et où il revient.

Rôle : garder, au fil des trames d'état, la précision horizontale annoncée par le robot à
l'endroit où il se trouve — un point au plus tous les 2 m ou toutes les 30 s — dans un
fichier JSON Lines du dossier de données (hors dépôt : il décrit le terrain).
Frontières : aucune interpolation, aucun jugement ; jamais bloquant, jamais d'écriture
pendant un arrêt (SEC-03, à la charge de l'appelant). Les seuils vivent dans `securite`.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

FICHIER = "releve-precision.jsonl"
PAS_M = 2.0
PAS_S = 30.0
POINTS_MAX = 1500
TAILLE_MAX_OCTETS = 1_000_000  # au-delà, le fichier tourne (un seul ancien fichier gardé)


class Releve:
    def __init__(self, dossier: Path) -> None:
        self.chemin = dossier / FICHIER
        self._dernier: tuple[float, float, float] | None = None  # est, nord, instant

    def noter(self, est: float, nord: float, precision_m: float, satellites: Any, instant: float) -> bool:
        """Écrit un point s'il est assez loin, ou assez tard, du précédent. Vrai s'il a été écrit."""
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (est, nord, precision_m)):
            return False
        if self._dernier is not None:
            proche = math.hypot(est - self._dernier[0], nord - self._dernier[1]) < PAS_M
            if proche and instant - self._dernier[2] < PAS_S:
                return False
        point = {
            "horodatage": datetime.now().isoformat(timespec="seconds"), "est_m": round(est, 2), "nord_m": round(nord, 2),
            "precision_m": round(precision_m, 2), "satellites": satellites if isinstance(satellites, int) else None,
        }
        try:
            self.chemin.parent.mkdir(parents=True, exist_ok=True)
            if self.chemin.exists() and self.chemin.stat().st_size > TAILLE_MAX_OCTETS:
                self.chemin.replace(self.chemin.with_name(FICHIER + ".1"))  # fichier borné
            with self.chemin.open("a", encoding="utf-8", newline="\n") as fichier:
                fichier.write(json.dumps(point, ensure_ascii=False) + "\n")
        except OSError:
            return False  # jamais bloquant : sans disque, pas de relevé
        self._dernier = (est, nord, instant)
        return True


def _point(ligne: bytes) -> dict[str, Any] | None:
    try:
        point = json.loads(ligne)
    except ValueError:
        return None
    if isinstance(point, dict) and all(isinstance(point.get(c), (int, float)) for c in ("est_m", "nord_m", "precision_m")):
        return point
    return None


def _recent(point: dict[str, Any], limite: datetime | None) -> bool:
    if limite is None:
        return True
    try:
        return datetime.fromisoformat(str(point.get("horodatage"))) >= limite
    except ValueError:
        return False


class Lecteur:
    """Lecture incrémentale du relevé : seuls les octets ajoutés depuis le dernier appel sont relus."""

    def __init__(self, dossier: Path) -> None:
        self.chemin = dossier / FICHIER
        self._position = 0
        self._points: list[dict[str, Any]] = []

    def points(self, maximum: int = POINTS_MAX, jours: int | None = None) -> list[dict[str, Any]]:
        try:
            taille = self.chemin.stat().st_size
            if taille < self._position:  # fichier tourné ou remplacé : on repart du début
                self._position, self._points = 0, []
            if taille > self._position:
                with self.chemin.open("rb") as fichier:
                    fichier.seek(self._position)
                    bloc = fichier.read(TAILLE_MAX_OCTETS)
                fin = bloc.rfind(b"\n") + 1  # lignes entières seulement : une ligne en cours d'écriture attend
                self._points.extend(p for p in map(_point, bloc[:fin].splitlines()) if p is not None)
                self._position += fin
                del self._points[:-POINTS_MAX]
        except OSError:
            self._position, self._points = 0, []
        limite = None if jours is None else datetime.now() - timedelta(days=jours)
        return [p for p in self._points if _recent(p, limite)][-maximum:]


def lire(dossier: Path, maximum: int = POINTS_MAX, jours: int | None = None) -> list[dict[str, Any]]:
    """Les derniers points relevés ; une ligne illisible est ignorée. `jours` écarte les relevés plus anciens."""
    return Lecteur(dossier).points(maximum, jours)
