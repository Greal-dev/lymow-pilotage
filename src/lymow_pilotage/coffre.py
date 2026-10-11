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


def lire(cle: str) -> str | None:
    import keyring  # import tardif : la simulation ne doit pas dépendre du coffre

    valeur = keyring.get_password(SERVICE, cle)
    if cle == JETON:
        declarer_secret(valeur)
    return valeur or None


def ecrire(cle: str, valeur: str) -> None:
    import keyring

    if cle not in _CLES:
        raise ValueError("clé de coffre inconnue")
    keyring.set_password(SERVICE, cle, valeur)


def effacer() -> None:
    import keyring
    import keyring.errors

    for cle in _CLES:
        try:
            keyring.delete_password(SERVICE, cle)
        except keyring.errors.PasswordDeleteError:
            pass  # entrée déjà absente


def appareil_connu() -> str | None:
    """Identifiant d'appareil pour l'affichage en simulation ; jamais bloquant."""
    try:
        return lire(APPAREIL)
    except Exception:  # noqa: BLE001 — coffre absent ou verrouillé : la simulation continue
        return None
