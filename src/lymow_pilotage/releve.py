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
from datetime import datetime
from pathlib import Path
from typing import Any

FICHIER = "releve-precision.jsonl"
PAS_M = 2.0
PAS_S = 30.0
POINTS_MAX = 1500


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
            with self.chemin.open("a", encoding="utf-8", newline="\n") as fichier:
                fichier.write(json.dumps(point, ensure_ascii=False) + "\n")
        except OSError:
            return False  # jamais bloquant : sans disque, pas de relevé
        self._dernier = (est, nord, instant)
        return True


def lire(dossier: Path, maximum: int = POINTS_MAX) -> list[dict[str, Any]]:
    """Les derniers points relevés ; une ligne illisible est ignorée."""
    try:
        lignes = (dossier / FICHIER).read_text(encoding="utf-8").splitlines()[-maximum:]
    except OSError:
        return []
    points = []
    for ligne in lignes:
        try:
            point = json.loads(ligne)
        except ValueError:
            continue
        if isinstance(point, dict) and all(isinstance(point.get(c), (int, float)) for c in ("est_m", "nord_m", "precision_m")):
            points.append(point)
    return points
