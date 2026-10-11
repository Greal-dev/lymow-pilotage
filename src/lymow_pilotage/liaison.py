"""Liaison réelle : coffre, jetons de courte durée, MQTT AWS IoT.

Rôle : seul module qui ouvre le réseau vers le nuage Lymow. Jamais appelé en
simulation (SEC-05). Aucun secret ne sort d'ici : tout est déclaré au filtre.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from . import auth, coffre
from .config import Calibration
from .journal import Journal, declarer_secret
from .protocole import Categorie
from .session import EchecOrdre, Session
from .vendor.ha_lymow.auth import LymowAuth
from .vendor.ha_lymow.const import REGION_CONFIG
from .vendor.ha_lymow.mqtt import LymowMqttClient

DELAI_HTTP = aiohttp.ClientTimeout(total=20)


@dataclass(frozen=True)
class Identifiants:
    region: str
    appareil: str
    methode: str
    jeton: str = field(repr=False)


@dataclass(frozen=True)
class Acces:
    identite: str = field(repr=False)
    jeton_acces: str = field(repr=False)
    cles: dict[str, Any] = field(repr=False)


def lire_identifiants() -> Identifiants:
    valeurs = {cle: coffre.lire(cle) for cle in (coffre.REGION, coffre.APPAREIL, coffre.METHODE, coffre.JETON)}
    if not all(valeurs.values()) or valeurs[coffre.REGION] not in REGION_CONFIG:
        raise auth.ErreurAuth("aucune connexion enregistrée : lancez d'abord « lymow connexion »")
    return Identifiants(
        valeurs[coffre.REGION], valeurs[coffre.APPAREIL], valeurs[coffre.METHODE], valeurs[coffre.JETON]
    )


async def obtenir_acces(http: aiohttp.ClientSession, ident: Identifiants) -> Acces:
    jetons = await auth.rafraichir(http, ident.jeton, ident.region, ident.methode)
    nouveau = jetons.get("RefreshToken")
    if isinstance(nouveau, str) and nouveau and nouveau != ident.jeton:
        coffre.ecrire(coffre.JETON, nouveau)  # rotation du jeton côté Cognito
    aws = await LymowAuth(http).get_aws_credentials(jetons["IdToken"], ident.region)
    cles = aws["credentials"]
    for secret in (aws["identity_id"], cles.get("AccessKeyId"), cles.get("SecretKey"), cles.get("SessionToken")):
        declarer_secret(secret)
    return Acces(aws["identity_id"], jetons["AccessToken"], cles)


class LienMqtt:
    """Adapte le client du socle à la forme attendue par `Session`."""

    def __init__(self, client: LymowMqttClient, ident: Identifiants, acces: Acces) -> None:
        self._client = client
        self._ident = ident
        self._acces = acces

    @property
    def connecte(self) -> bool:
        return self._client.is_connected

    async def connecter(self, abonner: bool = True) -> None:
        cles = self._acces.cles
        try:
            await self._client.connect(
                [self._ident.appareil], cles["AccessKeyId"], cles["SecretKey"], cles.get("SessionToken"),
                subscribe=abonner,
            )
        except asyncio.CancelledError:
            raise
        except Exception as erreur:  # noqa: BLE001 — le texte d'origine peut citer l'URL présignée
            raise EchecOrdre(f"connexion MQTT impossible ({type(erreur).__name__})") from None

    async def publier(self, sujet: str, charge: str, *, delai: float) -> None:
        await self._client.async_publish(sujet, charge, timeout=delai)

    async def reconnecter(self) -> None:
        """SEC-03 : voie de secours de l'arrêt — sans abonnements, pour publier au plus vite."""
        await self._client.disconnect()
        await self.connecter(abonner=False)

    async def fermer(self) -> None:
        await self._client.disconnect()


@asynccontextmanager
async def ouvrir_session(
    journal: Journal,
    cal: Calibration,
    autorisees: frozenset[Categorie],
    ident: Identifiants | None = None,
    acces: Acces | None = None,
) -> AsyncIterator[Session]:
    ident = ident or lire_identifiants()
    if acces is None:
        async with aiohttp.ClientSession(timeout=DELAI_HTTP) as http:
            acces = await obtenir_acces(http, ident)
    receveur: list[Session] = []
    client = LymowMqttClient(
        REGION_CONFIG[ident.region]["iot_host"],
        ident.region,
        on_state=lambda _appareil, fragment, retenu: receveur[0].recevoir_etat(fragment, retenu),
        on_online=lambda _appareil, en_ligne, retenu: receveur[0].recevoir_presence(en_ligne, retenu),
    )
    lien = LienMqtt(client, ident, acces)
    receveur.append(Session(lien, ident.appareil, journal, cal, autorisees))
    await lien.connecter()
    try:
        yield receveur[0]
    finally:
        await lien.fermer()
