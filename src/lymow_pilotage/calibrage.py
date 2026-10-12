"""Constat de calibration : ce que le journal local dit du rapport mesuré / commandé.

Rôle : relire les constats « vérifiés » des ordres réels (journaux du dossier de données) et
calculer, par type d'ordre et par vitesse, le rapport moyen entre le déplacement mesuré par le
robot et le déplacement commandé, sa dispersion, et le facteur de calibration que cela suggère.
Frontières : lecture seule, hors ligne ; ne modifie jamais `calibration.toml` — la décision
revient au propriétaire. Hypothèse dite dans le résultat : les ordres relus ont été émis avec
les facteurs actuels. La mesure est celle du robot : elle vaut ce que vaut sa position.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from . import mouvement, securite
from .config import Calibration

MESURES_MIN = 3  # en dessous, aucune suggestion : trop peu de constats


def _constats(dossier: Path) -> list[dict[str, Any]]:
    lignes: list[dict[str, Any]] = []
    for fichier in sorted((dossier / "journaux").glob("*.jsonl")):
        try:
            texte = fichier.read_text(encoding="utf-8")
        except OSError:
            continue
        for ligne in texte.splitlines():
            try:
                evenement = json.loads(ligne)
            except ValueError:
                continue
            if isinstance(evenement, dict) and evenement.get("evenement") == "ordre-constate":
                lignes.append(evenement)
    return lignes


def _mesure(constat: dict[str, Any]) -> tuple[str, float, float] | None:
    """(grandeur, vitesse, rapport mesuré/commandé) d'un constat vérifié en réel ; rien sinon."""
    if constat.get("mode") != "réel" or constat.get("verification") != mouvement.VERIFIEE:
        return None
    try:
        ordre = mouvement.analyser_etape(str(constat.get("ordre", "")))
    except (ValueError, mouvement.ErreurSaisie, securite.RefusSecurite):
        return None
    if ordre.nom not in mouvement.MOUVEMENTS or not ordre.valeur:
        return None  # ordre à la durée : pas de grandeur commandée à comparer
    if ordre.nom == "tourner":
        mesure, vitesse = constat.get("rotation_deg"), abs(ordre.vitesse or securite.VITESSE_ANGULAIRE_RAD_S)
        rapport = mesure / ordre.valeur if isinstance(mesure, (int, float)) else None
        grandeur = "angle"
    else:
        mesure, vitesse = constat.get("avance_m"), abs(ordre.vitesse or securite.VITESSE_LINEAIRE_M_S)
        signe = 1.0 if ordre.nom == "avancer" else -1.0
        rapport = signe * mesure / abs(ordre.valeur) if isinstance(mesure, (int, float)) else None
        grandeur = "distance"
    if rapport is None or rapport <= 0:
        return None  # mauvais sens : une anomalie, pas une échelle
    return grandeur, round(vitesse, 3), rapport


def constat(dossier: Path, cal: Calibration) -> dict[str, Any]:
    groupes: dict[tuple[str, float], list[float]] = {}
    for ligne in _constats(dossier):
        mesure = _mesure(ligne)
        if mesure is not None:
            groupes.setdefault(mesure[:2], []).append(mesure[2])
    actuels = {"distance": cal.facteur_distance, "angle": cal.facteur_angle}
    unites = {"distance": "m/s", "angle": "rad/s"}
    lignes = []
    for (grandeur, vitesse), rapports in sorted(groupes.items()):
        moyenne = statistics.fmean(rapports)
        ligne: dict[str, Any] = {
            "grandeur": grandeur, "vitesse": f"{vitesse:g} {unites[grandeur]}", "mesures": len(rapports),
            "rapport_mesure_sur_commande": round(moyenne, 3),
            "dispersion": round(statistics.stdev(rapports), 3) if len(rapports) > 1 else None,
            "etendue": [round(min(rapports), 3), round(max(rapports), 3)],
            "facteur_actuel": actuels[grandeur],
        }
        if len(rapports) >= MESURES_MIN:
            ligne[f"facteur_{grandeur}_suggere"] = round(actuels[grandeur] / moyenne, 3)
        else:
            ligne["suggestion"] = f"aucune : moins de {MESURES_MIN} constats"
        lignes.append(ligne)
    return {
        "constats": lignes,
        "note": (
            "lecture seule : calibration.toml n'est pas modifié. Hypothèse : ces ordres ont été émis avec les facteurs "
            "actuels. La mesure est la position annoncée par le robot ; elle ne vaut que si sa précision était bonne."
        ) if lignes else "aucun constat vérifié d'ordre réel dans les journaux de ce dossier",
    }
