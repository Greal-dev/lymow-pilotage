"""Dossier de données et calibration (réglages non secrets, incertitudes du protocole).

Rôle : résoudre où vivent `journaux/` et `calibration.toml`, lire et valider la
calibration. Aucun secret ici ; aucune clé ne touche aux invariants SEC-*.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from . import securite

NOM_CALIBRATION = "calibration.toml"
VARIABLE_DOSSIER = "LYMOW_DONNEES"


class ErreurConfiguration(Exception):
    """Calibration illisible ou hors bornes."""


def _racine_projet() -> Path:
    for depart in (Path.cwd(), Path(__file__).resolve().parent):
        for dossier in (depart, *depart.parents):
            if (dossier / "pyproject.toml").is_file():
                return dossier
    return Path.cwd()


def dossier_donnees(option: str | None = None) -> Path:
    """Option `--donnees`, sinon variable LYMOW_DONNEES, sinon racine du projet."""
    choisi = option or os.environ.get(VARIABLE_DOSSIER)
    return Path(choisi).expanduser().resolve() if choisi else _racine_projet()


FICHIER_PREFERENCES = "preferences.json"


def arret_aux_bords(dossier: Path | None) -> bool:
    """Préférence « Arrêter aux bords de zone (d'après la carte) » : fausse par défaut, et si elle est illisible."""
    try:
        lue = json.loads((dossier / FICHIER_PREFERENCES).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(lue, dict) and lue.get("arret_aux_bords") is True


def memoriser_arret_aux_bords(dossier: Path, actif: bool) -> bool:
    """Écriture atomique ; faux si le disque refuse (la préférence vaut alors pour cette session seulement)."""
    try:
        dossier.mkdir(parents=True, exist_ok=True)
        provisoire = dossier / (FICHIER_PREFERENCES + ".tmp")
        provisoire.write_text(json.dumps({"arret_aux_bords": bool(actif)}) + "\n", encoding="utf-8", newline="\n")
        provisoire.replace(dossier / FICHIER_PREFERENCES)
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class Calibration:
    signe_angulaire: int = 1
    facteur_distance: float = 1.0
    facteur_angle: float = 1.0
    variante_trame: str = "mqtt"
    version_protocole: int = 49
    plateau_haut_mm: int = 100
    variante_hauteur: str = "socle"
    voie_m: float = 0.45  # écart entre les chenilles : estimation, à calibrer


_CHOIX = {
    "signe_angulaire": (1, -1),
    "variante_trame": ("mqtt", "ble"),
    "version_protocole": (49, 40),
    "variante_hauteur": ("socle", "lymow-ha"),
}
_FACTEUR_MIN, _FACTEUR_MAX = 0.5, 2.0


def _valider(nom: str, valeur: object, defaut: object) -> None:
    if type(valeur) is not type(defaut) and not (isinstance(defaut, float) and type(valeur) is int):
        raise ErreurConfiguration(f"{NOM_CALIBRATION} : type inattendu pour « {nom} »")
    if nom in _CHOIX and valeur not in _CHOIX[nom]:
        raise ErreurConfiguration(f"{NOM_CALIBRATION} : « {nom} » doit valoir l'un de {_CHOIX[nom]}")
    if nom.startswith("facteur_") and not _FACTEUR_MIN <= valeur <= _FACTEUR_MAX:
        raise ErreurConfiguration(f"{NOM_CALIBRATION} : « {nom} » doit rester entre {_FACTEUR_MIN} et {_FACTEUR_MAX}")
    if nom == "voie_m" and not 0.2 <= valeur <= 1.0:
        raise ErreurConfiguration(f"{NOM_CALIBRATION} : « {nom} » doit rester entre 0.2 et 1.0 m")
    if nom == "plateau_haut_mm" and not securite.PLATEAU_HAUT_MIN_MM <= valeur <= securite.HAUTEUR_MAX_MM:
        raise ErreurConfiguration(
            f"{NOM_CALIBRATION} : « {nom} » doit rester entre "
            f"{securite.PLATEAU_HAUT_MIN_MM} et {securite.HAUTEUR_MAX_MM} mm"
        )


def charger_calibration(dossier: Path) -> Calibration:
    """Fichier absent = valeurs par défaut ; clé inconnue ou hors borne = erreur."""
    try:
        brut = tomllib.loads((dossier / NOM_CALIBRATION).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Calibration()
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as erreur:
        raise ErreurConfiguration(f"{NOM_CALIBRATION} illisible ({type(erreur).__name__})") from erreur
    defauts = {champ.name: champ.default for champ in fields(Calibration)}
    for nom, valeur in brut.items():
        if nom not in defauts:
            raise ErreurConfiguration(f"{NOM_CALIBRATION} : clé inconnue « {nom} »")
        _valider(nom, valeur, defauts[nom])
    return Calibration(**brut)
