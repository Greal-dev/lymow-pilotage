"""Stockage dans le Gestionnaire d'identification Windows, via `keyring`.

Rôle : seul endroit où un secret est conservé. Aucun fichier. Seul le jeton de
rafraîchissement est secret ; région, appareil et méthode sont des repères.
"""

from __future__ import annotations

from .journal import declarer_secret

SERVICE = "lymow-pilotage"
JETON = "jeton-rafraichissement"
REGION = "region"
APPAREIL = "appareil"
METHODE = "methode"  # "google" ou "mot-de-passe" : décide du guichet de rafraîchissement
_CLES = (JETON, REGION, APPAREIL, METHODE)


# Windows refuse un secret de plus de 1280 caractères (CredWrite, erreur 1783) et le jeton
# de rafraîchissement Cognito en fait davantage (constaté le 2026-10-12 : la connexion
# réussissait puis échouait à l'enregistrement). Une valeur longue est donc répartie sur
# plusieurs entrées « clé#1 », « clé#2 »…, l'entrée principale ne portant que leur nombre.
TAILLE_MORCEAU = 1000
MORCEAUX_MAX = 16
_MARQUE = "#morceaux:"


def _nombre_de_morceaux(valeur: str | None) -> int:
    if not valeur or not valeur.startswith(_MARQUE):
        return 0
    reste = valeur[len(_MARQUE):]
    nombre = int(reste) if reste.isascii() and reste.isdigit() else 0
    return nombre if 1 <= nombre <= MORCEAUX_MAX else 0


def _supprimer(cle: str) -> None:
    import keyring
    import keyring.errors

    try:
        keyring.delete_password(SERVICE, cle)
    except keyring.errors.PasswordDeleteError:
        pass  # entrée déjà absente


def lire(cle: str) -> str | None:
    import keyring  # import tardif : la simulation ne doit pas dépendre du coffre

    valeur = keyring.get_password(SERVICE, cle)
    nombre = _nombre_de_morceaux(valeur)
    if nombre:
        morceaux = [keyring.get_password(SERVICE, f"{cle}#{rang}") for rang in range(1, nombre + 1)]
        valeur = None if any(not morceau for morceau in morceaux) else "".join(morceaux)
    if cle == JETON:
        declarer_secret(valeur)
    return valeur or None


def ecrire(cle: str, valeur: str) -> None:
    import keyring

    if cle not in _CLES:
        raise ValueError("clé de coffre inconnue")
    if len(valeur) > TAILLE_MORCEAU * MORCEAUX_MAX:
        raise ValueError("valeur trop longue pour le coffre")
    ancien = _nombre_de_morceaux(keyring.get_password(SERVICE, cle))
    if len(valeur) <= TAILLE_MORCEAU and not valeur.startswith(_MARQUE):
        morceaux: list[str] = []
        keyring.set_password(SERVICE, cle, valeur)
    else:
        morceaux = [valeur[debut:debut + TAILLE_MORCEAU] for debut in range(0, len(valeur), TAILLE_MORCEAU)]
        for rang, morceau in enumerate(morceaux, start=1):
            keyring.set_password(SERVICE, f"{cle}#{rang}", morceau)
        keyring.set_password(SERVICE, cle, f"{_MARQUE}{len(morceaux)}")
    for rang in range(len(morceaux) + 1, ancien + 1):
        _supprimer(f"{cle}#{rang}")  # morceaux d'une ancienne valeur plus longue


def effacer() -> None:
    import keyring

    for cle in _CLES:
        for rang in range(1, _nombre_de_morceaux(keyring.get_password(SERVICE, cle)) + 1):
            _supprimer(f"{cle}#{rang}")
        _supprimer(cle)


def appareil_connu() -> str | None:
    """Identifiant d'appareil pour l'affichage en simulation ; jamais bloquant."""
    try:
        return lire(APPAREIL)
    except Exception:  # noqa: BLE001 — coffre absent ou verrouillé : la simulation continue
        return None
