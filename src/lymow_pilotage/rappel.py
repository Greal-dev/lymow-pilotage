"""Connexion Google en un clic : gestionnaire Windows du protocole `myapp://`.

Rôle : (1) inscrire ou retirer, dans le registre de l'utilisateur courant, la
commande qui reçoit l'adresse de rappel ; (2) valider cette adresse, qui vient
d'un navigateur donc d'une source non fiable, et la remettre à l'interface
locale en cours. Ne termine pas la connexion (c'est `ui`), n'affiche ni ne
journalise jamais l'adresse.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import aiohttp

from .mouvement import ErreurSaisie

HOTE = "127.0.0.1"
PORT_DEFAUT = 8377
ROUTE = "/api/instance/rappel"
FICHIER_PORTS = "ui-ports.txt"
# L'argument vient d'un navigateur : forme exacte, jeu de caractères fermé, longueur bornée.
ADRESSE = re.compile(r"myapp://callback/\?[A-Za-z0-9=&%._~\-]{1,2048}")
CLE = r"Software\Classes\myapp"
SOUS_CLES = (CLE + r"\shell\open\command", CLE + r"\shell\open", CLE + r"\shell", CLE)
LIBELLE = "URL:Lymow, retour de connexion"


class EchecRappel(Exception):
    """Inscription impossible ou refusée ; le message dit pourquoi, sans rien avoir modifié."""


def valider(arguments: list[str]) -> str:
    """Un seul argument, aucune option, forme stricte. Le message d'erreur ne cite jamais l'argument."""
    if len(arguments) != 1 or not isinstance(arguments[0], str) or not ADRESSE.fullmatch(arguments[0]):
        raise ErreurSaisie("adresse de rappel refusée : un seul argument de la forme myapp, callback, code et state")
    return arguments[0]


# --- registre ------------------------------------------------------------------------


def _registre_reel() -> Any:
    if sys.platform != "win32":
        raise EchecRappel("connexion en un clic non prise en charge hors de Windows")
    import winreg

    return winreg


def executable_lymow() -> Path:
    chemin = Path(sys.executable).resolve().with_name("lymow.exe")
    if not chemin.is_file():
        raise EchecRappel("lymow.exe introuvable à côté de l'interpréteur : installation à refaire")
    return chemin


def commande_attendue(executable: Path) -> str:
    # L'adresse arrive entre guillemets, en dernier et seul argument : `rappel` refuse tout le reste.
    return f'"{executable}" rappel "%1"'


def _lire(registre: Any, racine: Any, chemin: str) -> str | None:
    """Valeur par défaut d'une clé ; None si la clé n'existe pas, « » si elle est vide."""
    try:
        with registre.OpenKey(racine, chemin) as cle:
            try:
                return str(registre.QueryValueEx(cle, "")[0])
            except OSError:
                return ""
    except OSError:
        return None


def etat(registre: Any = None, executable: Path | None = None) -> dict[str, Any]:
    """Lecture seule. `actif` : notre commande, à l'identique. `perime` : notre clé, autre exécutable.

    `etranger` : une clé `myapp` qui n'est pas la nôtre, dans le registre de l'utilisateur ou,
    vue par HKEY_CLASSES_ROOT, dans celui de la machine. Elle n'est jamais écrasée.
    """
    constat: dict[str, Any] = {"pris_en_charge": False, "actif": False, "perime": False, "etranger": None}
    try:
        registre = registre or _registre_reel()
    except EchecRappel:
        return constat
    constat["pris_en_charge"] = True
    libelle = _lire(registre, registre.HKEY_CURRENT_USER, CLE)
    commande = _lire(registre, registre.HKEY_CURRENT_USER, SOUS_CLES[0])
    if libelle is None:  # rien chez l'utilisateur : la vue fusionnée montre une éventuelle clé machine
        machine = _lire(registre, registre.HKEY_CLASSES_ROOT, "myapp")
        if machine is not None:
            en_place = _lire(registre, registre.HKEY_CLASSES_ROOT, r"myapp\shell\open\command")
            constat["etranger"] = f"clé de la machine : {en_place or machine or 'myapp sans commande'}"
        return constat
    if libelle != LIBELLE:  # seul notre libellé désigne notre clé
        constat["etranger"] = commande or "(clé myapp sans commande)"
        return constat
    try:
        attendue = commande_attendue(executable or executable_lymow())
    except EchecRappel:
        attendue = None
    constat["actif"] = commande is not None and commande == attendue  # égalité exacte, pas un suffixe
    constat["perime"] = not constat["actif"]
    return constat


def installer(registre: Any = None, executable: Path | None = None) -> str:
    """Écrit (ou répare) HKCU\\Software\\Classes\\myapp ; refuse d'écraser une clé qui n'est pas la nôtre."""
    registre = registre or _registre_reel()
    executable = executable or executable_lymow()
    constat = etat(registre, executable)
    if constat["etranger"] is not None:
        raise EchecRappel(
            f"une clé « myapp » existe déjà et ne pointe pas vers cet outil ({constat['etranger']}) : rien n'a été modifié"
        )
    commande = commande_attendue(executable)
    with registre.CreateKey(registre.HKEY_CURRENT_USER, CLE) as cle:
        registre.SetValueEx(cle, "", 0, registre.REG_SZ, LIBELLE)
        registre.SetValueEx(cle, "URL Protocol", 0, registre.REG_SZ, "")
    with registre.CreateKey(registre.HKEY_CURRENT_USER, SOUS_CLES[0]) as cle:
        registre.SetValueEx(cle, "", 0, registre.REG_SZ, commande)
    action = "réparée" if constat["perime"] else "activée"
    return f"connexion en un clic {action} : HKCU\\{CLE} lance désormais {commande}"


def desinstaller(registre: Any = None) -> str:
    """Ne retire que la clé qui porte NOTRE libellé (active ou périmée) ; rien d'une clé étrangère."""
    registre = registre or _registre_reel()
    constat = etat(registre)
    if constat["etranger"] is not None:
        raise EchecRappel(f"la clé « myapp » présente n'est pas la nôtre ({constat['etranger']}) : rien n'a été retiré")
    if not (constat["actif"] or constat["perime"]):
        return "connexion en un clic déjà inactive : rien à retirer"
    for chemin in SOUS_CLES:  # de la plus profonde à la racine : jamais de clé `myapp` vide laissée derrière
        try:
            registre.DeleteKey(registre.HKEY_CURRENT_USER, chemin)
        except FileNotFoundError:
            continue  # clé périmée incomplète : on poursuit vers la racine
        except OSError as erreur:
            raise EchecRappel(
                f"retrait incomplet ({type(erreur).__name__}) : la clé HKCU\\{CLE} contient autre chose que notre commande"
            ) from None
    return f"connexion en un clic désactivée : HKCU\\{CLE} retirée"


# --- remise de l'adresse à l'interface locale -----------------------------------------


def dossier_ports() -> Path:
    """Dossier fixe, indépendant du répertoire courant et de `--donnees` : l'interface et
    `lymow rappel` (lancé par Windows depuis un autre répertoire) doivent lire le même fichier."""
    depart = Path(__file__).resolve().parent
    for dossier in (depart, *depart.parents):
        if (dossier / "pyproject.toml").is_file():
            return dossier
    return Path.home()


def ports_connus(dossier: Path | None) -> list[int]:
    ports = [PORT_DEFAUT]
    try:
        lignes = (dossier / FICHIER_PORTS).read_text(encoding="ascii").split() if dossier else []
    except (OSError, ValueError):
        lignes = []
    for ligne in lignes[-8:]:
        if ligne.isdigit() and 1024 <= int(ligne) <= 65535 and int(ligne) not in ports:
            ports.append(int(ligne))
    return ports


def noter_port(dossier: Path | None, port: int, present: bool) -> None:
    """Tient la liste des ports où une interface tourne ; au mieux, jamais bloquant."""
    if dossier is None:
        return
    try:
        ports = [p for p in ports_connus(dossier)[1:] if p != port] + ([port] if present and port != PORT_DEFAUT else [])
        (dossier / FICHIER_PORTS).write_text("\n".join(str(p) for p in ports), encoding="ascii")
    except OSError:
        pass


REUSSIE, REFUSEE, AUCUNE = "réussie", "refusée", "aucune"


async def transmettre(adresse: str, ports: list[int]) -> str:
    """POST local, sans Origin ni Referer. Rend l'issue réelle : réussie, refusée, ou aucune interface."""
    delai = aiohttp.ClientTimeout(total=30)
    for port in ports:
        try:
            async with aiohttp.ClientSession(timeout=delai) as http:
                async with http.post(f"http://{HOTE}:{port}{ROUTE}", json={"adresse": adresse}, allow_redirects=False) as r:
                    corps = await r.json(content_type=None)
        except Exception:  # noqa: BLE001 — port fermé ou autre programme : on essaie le suivant
            continue
        if not isinstance(corps, dict) or corps.get("interface") != "lymow":
            continue
        if corps.get("ok") is True:
            return REUSSIE
        if corps.get("motif") == "connexion-refusee":  # la bonne interface a tenté l'échange, en vain
            return REFUSEE
    return AUCUNE
