"""Carte de référence : la dernière carte COMPLÈTE et valide reçue du robot, gardée sur disque.

Constat du 2026-10-12 : pendant une tâche, le robot diffuse des réponses de trajet (4 728 octets,
394 points) par le même chemin que la carte ; elles chassaient la carte de la mémoire et la page
n'affichait plus rien. L'affichage s'appuie donc sur cette référence, par appareil, horodatée.
Frontières : affichage seulement. Une écriture de carte garde ses exigences (SEC-09 : deux
lectures complètes, identiques et fraîches) et ne lit jamais ce fichier. Hors dépôt : il décrit
le jardin (contours, origine).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import carte as cartes
from .retouche import identifiant_sur
from .securite import RefusSecurite

DOSSIER = "carte-reference"


def _chemins(dossier: Path, appareil: str) -> tuple[Path, Path]:
    base = dossier / DOSSIER / identifiant_sur(appareil)  # jeu de caractères fermé avant tout nom de fichier
    return base.with_suffix(".pb"), base.with_suffix(".json")


def enregistrer(dossier: Path, appareil: str, carte: cartes.Carte) -> dict[str, Any] | None:
    """Garde `carte` si elle est complète ; rend ses métadonnées, ou rien (partielle, ou disque indisponible)."""
    if not carte.complete:
        return None
    meta = {
        "horodatage": datetime.now().isoformat(timespec="seconds"), "octets": len(carte.octets),
        "sha256": hashlib.sha256(carte.octets).hexdigest(),
    }
    try:
        brut, fiche = _chemins(dossier, appareil)
        brut.parent.mkdir(parents=True, exist_ok=True)
        brut.write_bytes(carte.octets)
        fiche.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    except (OSError, RefusSecurite):
        return None  # jamais bloquant : la carte reste affichée depuis la mémoire
    return meta


def charger(dossier: Path, appareil: str) -> tuple[cartes.Carte, dict[str, Any]] | None:
    """La référence de cet appareil, revalidée : empreinte du fichier, décodage strict, carte complète."""
    try:
        brut, fiche = _chemins(dossier, appareil)
        octets = brut.read_bytes()
        meta = json.loads(fiche.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("sha256") != hashlib.sha256(octets).hexdigest():
            return None
        carte = cartes.decoder(octets)
    except (OSError, ValueError, RefusSecurite):  # absente, abîmée, illisible, identifiant refusé : pas de référence
        return None
    return (carte, meta) if carte.complete else None


def libelle(meta: dict[str, Any]) -> str:
    """« carte complète du 12/10 à 10:50 »."""
    try:
        instant = datetime.fromisoformat(str(meta.get("horodatage")))
    except ValueError:
        return "carte complète de référence"
    return f"carte complète du {instant:%d/%m} à {instant:%H:%M}"
