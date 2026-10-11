"""Dossier de données et calibration (réglages non secrets, incertitudes du protocole).

Rôle : résoudre où vivent `journaux/` et `calibration.toml`, lire et valider la
calibration. Aucun secret ici ; aucune clé ne touche aux invariants SEC-*.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

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


@dataclass(frozen=True)
class Calibration:
    signe_angulaire: int = 1
    facteur_distance: float = 1.0
    facteur_angle: float = 1.0
    variante_trame: str = "mqtt"
    version_protocole: int = 49
    plateau_haut_mm: int = 100
    variante_hauteur: str = "socle"


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
