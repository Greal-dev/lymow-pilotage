"""Enregistrement du compte sur ce poste : étapes communes à `lymow connexion` et à l'interface.

Rôle : après l'échange du code (dans `auth`), lister les appareils, n'écrire au
coffre que le jeton de rafraîchissement et trois repères, ou tout effacer.
Ne lit aucune saisie, n'affiche rien, ne journalise rien.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from . import auth, coffre
from .journal import declarer_secret
from .vendor.ha_lymow.api import LymowApiClient
from .vendor.ha_lymow.auth import LymowAuth


def appareils_lisibles(brut: Any) -> list[dict[str, str]]:
    """Réponse du nuage non fiable : on n'en garde que deux chaînes bornées."""
    liste = brut if isinstance(brut, list) else []
    return [
        {"nom": str(a.get("deviceName") or "sans nom")[:60], "appareil": str(a["deviceThingName"])[:128]}
        for a in liste
        if isinstance(a, dict) and a.get("deviceThingName")
    ]


def jeton_durable(jetons: dict[str, Any]) -> str:
    jeton = jetons.get("RefreshToken")
    if not isinstance(jeton, str) or not jeton:
        raise auth.ErreurAuth("le service n'a pas fourni de jeton de rafraîchissement")
    return jeton


async def appareils_du_compte(http: aiohttp.ClientSession, jetons: dict[str, Any], region: str) -> list[dict[str, str]]:
    aws = await LymowAuth(http).get_aws_credentials(jetons["IdToken"], region)
    declarer_secret(aws["identity_id"])
    client = LymowApiClient(http, jetons["AccessToken"], region, aws["identity_id"])
    appareils = appareils_lisibles(await client.get_devices())
    if not appareils:
        raise auth.ErreurAuth("aucun appareil sur ce compte")
    return appareils


def enregistrer(jeton: str, region: str, appareil: str, methode: str) -> None:
    """Seul le jeton de rafraîchissement est conservé ; les jetons courts sont jetés."""
    for cle, valeur in (
        (coffre.JETON, jeton), (coffre.REGION, region), (coffre.APPAREIL, appareil), (coffre.METHODE, methode),
    ):
        coffre.ecrire(cle, valeur)


def oublier() -> None:
    """Déconnexion de ce poste : efface les quatre entrées du coffre, rien d'autre."""
    coffre.effacer()
