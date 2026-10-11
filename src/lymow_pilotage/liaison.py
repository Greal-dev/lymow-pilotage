"""Liaison réelle : coffre, jetons de courte durée, MQTT AWS IoT.

Rôle : seul module qui ouvre le réseau vers le nuage Lymow. Un ORDRE simulé ne
passe jamais par ici (SEC-05). Les lectures, elles, l'ouvrent sans `--reel` :
`etat`, `diagnostic`, et l'interface web quand un compte est enregistré — avec
une session qui ne sait émettre que les deux requêtes pures (ou rien du tout),
plus la vidéo sur demande explicite. Aucun secret ne sort d'ici : tout est
déclaré au filtre. Chaque appel à `obtenir_acces` peut faire tourner le jeton
stocké : l'appelant relit `lire_identifiants()` avant chaque appel et ne lance
jamais deux obtentions en parallèle.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import re
from typing import Any

import aiohttp

from . import auth, coffre
from .config import Calibration
from .journal import Journal, declarer_secret
from .protocole import Categorie
from .session import EchecOrdre, Session
from .vendor.ha_lymow.api import LymowApiClient
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


def _hote_video(url: object, schema: str, region: str) -> bool:
    """Réponse du nuage non fiable : seul un hôte Kinesis Video de la région est accepté."""
    if not isinstance(url, str) or region not in REGION_CONFIG:
        return False
    # Expression ancrée, caractères autorisés seulement : ni « \ », ni « @ », ni requête, ni fragment.
    motif = (
        rf"{schema}://(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+"
        rf"kinesisvideo\.{re.escape(region)}\.amazonaws\.com(?::443)?(?:/[A-Za-z0-9._/-]*)?"
    )
    return re.fullmatch(motif, url) is not None


async def demarrer_video(http: aiohttp.ClientSession, ident: Identifiants, acces: Acces) -> dict[str, Any]:
    """Ouvre une session vidéo (spectateur) : URL de signalisation présignée et serveurs ICE.

    Tout ce qui est rendu est un secret temporaire : déclaré au filtre, jamais journalisé.
    Enchaînement repris du socle (`coordinator.async_start_video_session`).
    """
    client = LymowApiClient(http, acces.jeton_acces, ident.region, acces.identite)
    session = await client.start_video_session(ident.appareil)
    canal = session.get("channelARN") if isinstance(session, dict) else None
    cles = session.get("credentials") if isinstance(session, dict) else None
    noms = ("accessKeyId", "secretAccessKey", "sessionToken")
    if not isinstance(canal, str) or not isinstance(cles, dict) or not all(isinstance(cles.get(n), str) for n in noms):
        raise EchecOrdre("réponse inattendue du service vidéo")
    for nom in noms:
        declarer_secret(cles[nom])
    points = await client.get_signaling_channel_endpoint(canal, cles, region=ident.region)
    wss, https = points.get("WSS"), points.get("HTTPS")
    # Les clés signent la requête suivante : elles ne partent que vers Kinesis Video.
    if not _hote_video(wss, "wss", ident.region) or not _hote_video(https, "https", ident.region):
        raise EchecOrdre("adresse de signalisation vidéo inattendue")
    glace = await client.get_ice_server_config(canal, https, cles, region=ident.region)
    url = client.presign_signaling_url(wss, canal, client.viewer_client_id(), cles, region=ident.region)
    declarer_secret(url)
    serveurs = []
    for entree in glace if isinstance(glace, list) else []:
        if not isinstance(entree, dict) or not isinstance(entree.get("Uris"), list):
            continue
        adresses = [a for a in entree["Uris"] if isinstance(a, str) and a.startswith(("turn:", "turns:", "stun:"))]
        identifiant, secret = entree.get("Username"), entree.get("Password")
        if adresses and isinstance(identifiant, str) and isinstance(secret, str):
            declarer_secret(identifiant)
            declarer_secret(secret)
            serveurs.append({"urls": adresses, "username": identifiant, "credential": secret})
    return {"wss": url, "iceServers": serveurs}


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
