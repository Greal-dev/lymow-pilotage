"""Connexion au compte Lymow : Google par l'interface Cognito hébergée, ou mot de passe.

Rôle : PKCE S256 et `state` (obligatoires, vérifiés), échange du code,
rafraîchissement. Ne stocke rien (voir `coffre`), n'affiche rien, ne journalise rien.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from .journal import declarer_secret
from .vendor.ha_lymow.auth import LymowAuth
from .vendor.ha_lymow.const import COGNITO_DOMAINS

REDIRECTION = "myapp://callback/"
REGION_DEFAUT = "eu-west-1"
REGIONS = tuple(COGNITO_DOMAINS)
METHODE_GOOGLE = "google"
METHODE_MOT_DE_PASSE = "mot-de-passe"


class ErreurAuth(Exception):
    """Connexion refusée ou réponse inexploitable ; le message ne porte aucun secret."""


@dataclass(frozen=True)
class DemandeOAuth:
    region: str
    url: str
    etat: str = field(repr=False)
    verificateur: str = field(repr=False)


def preparer_connexion(region: str) -> DemandeOAuth:
    if region not in REGIONS:
        raise ErreurAuth(f"région inconnue ; régions possibles : {', '.join(REGIONS)}")
    etat = secrets.token_urlsafe(32)
    verificateur = secrets.token_urlsafe(64)
    declarer_secret(verificateur)
    defi = base64.urlsafe_b64encode(hashlib.sha256(verificateur.encode()).digest()).rstrip(b"=").decode()
    # La construction de l'URL n'utilise pas la session HTTP.
    url = LymowAuth(None).get_oauth_authorize_url(  # type: ignore[arg-type]
        region=region, redirect_uri=REDIRECTION, state=etat, code_challenge=defi
    )
    return DemandeOAuth(region, url, etat, verificateur)


def extraire_code(saisie: str, etat_attendu: str) -> str:
    """Exige l'URL de rappel complète : sans `state` vérifiable, pas d'échange."""
    saisie = saisie.strip()
    declarer_secret(saisie)
    if not saisie.startswith(REDIRECTION):
        raise ErreurAuth(
            "collez l'adresse de rappel complète (elle commence par myapp et porte code et state) : "
            "un code seul ne permet pas de vérifier le paramètre state"
        )
    parametres = parse_qs(urlparse(saisie).query)
    if "error" in parametres:
        raise ErreurAuth("le service de connexion a renvoyé une erreur au lieu d'un code")
    etat_recu = parametres.get("state", [""])[0]
    code = parametres.get("code", [""])[0]
    # Sur des octets : `compare_digest` refuse les chaînes non ASCII.
    if not etat_recu or not hmac.compare_digest(etat_recu.encode(), etat_attendu.encode()):
        raise ErreurAuth("paramètre state absent ou différent : connexion abandonnée")
    if not code:
        raise ErreurAuth("aucun code dans l'URL de rappel")
    declarer_secret(code)
    return code


def _declarer(jetons: dict[str, Any]) -> dict[str, Any]:
    for cle in ("AccessToken", "IdToken", "RefreshToken"):
        declarer_secret(jetons.get(cle))
    return jetons


async def echanger_code(http: aiohttp.ClientSession, demande: DemandeOAuth, saisie: str) -> dict[str, Any]:
    code = extraire_code(saisie, demande.etat)
    jetons = await LymowAuth(http).exchange_oauth_code(
        region=demande.region, code=code, redirect_uri=REDIRECTION, code_verifier=demande.verificateur
    )
    return _declarer(jetons)


async def connexion_mot_de_passe(
    http: aiohttp.ClientSession, courriel: str, mot_de_passe: str, region: str
) -> dict[str, Any]:
    declarer_secret(mot_de_passe)
    declarer_secret(courriel)
    try:
        return _declarer(await LymowAuth(http).login_region(courriel, mot_de_passe, region))
    except KeyError as erreur:
        raise ErreurAuth("réponse de connexion inattendue (défi non pris en charge ?)") from erreur


async def rafraichir(http: aiohttp.ClientSession, jeton: str, region: str, methode: str) -> dict[str, Any]:
    """Jetons de courte durée à partir du jeton de rafraîchissement stocké."""
    declarer_secret(jeton)
    client = LymowAuth(http)
    if methode == METHODE_GOOGLE:
        jetons = await client.refresh_oauth_tokens(refresh_token=jeton, region=region)
    else:
        jetons = await client.refresh_tokens(jeton, region)
    return _declarer(jetons)
