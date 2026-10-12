"""Faux client MQTT, faux robot, horloge virtuelle : aucun test ne touche le réseau ni le coffre."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from lymow_pilotage import coffre, journal
from lymow_pilotage.config import Calibration
from lymow_pilotage.journal import Journal
from lymow_pilotage.protocole import Categorie, est_requete_pure
from lymow_pilotage.session import Session

APPAREIL = "robot-test"
SUJET = "/device/robot-test/pbinput"
ARRET = bytes.fromhex("10 31 52 0a 0d 00 00 00 00 15 00 00 00 00")
DEMANDE_CONFIGURATION = bytes.fromhex("10 31 4a 02 50 01")
DEMANDE_CARTE = bytes.fromhex("10 31 28 13 ba 01 04 08 00 20 01")
ENTETE_ECRITURE_CARTE = bytes.fromhex("10 31 28 09")


class Horloge:
    """Temps virtuel : dormir avance l'horloge et rend la main à la boucle."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    async def dormir(self, secondes: float) -> None:
        self.t += secondes
        await asyncio.sleep(0)


def fragment(statut: int = 1, est: float = 0.0, nord: float = 0.0, theta: float = 0.0) -> dict:
    return {"workStatus": statut, "poseEastM": est, "poseNorthM": nord, "poseThetaRad": theta, "battery": 80}


def est_consigne(octets: bytes) -> bool:
    """Trame de pilotage non nulle (champ 10 présent, hors arrêt)."""
    return b"\x52\x0a" in octets and octets != ARRET


class FauxLien:
    """Client MQTT factice doublé d'un faux robot.

    - `sur_publication(lien, octets)` : appelée avant l'envoi ; peut lever ou couper la liaison ;
    - `retard` : acquittement (PUBACK) lent, en secondes réelles ;
    - `muet` : liaison à moitié morte, qui n'acquitte jamais et n'échoue jamais ;
    - `robot` : état renvoyé à chaque demande de configuration ; `None` = robot silencieux.
    """

    def __init__(self, horloge: Horloge) -> None:
        self.horloge = horloge
        self.session: Session | None = None
        self.connecte = True
        self.publies: list[tuple[str, bytes]] = []
        self.sur_publication = None
        self.retard = 0.0
        self.muet = False
        self.robot: dict | None = fragment()
        self.delai_robot = 0.0
        self.carte: bytes | None = None  # contenu brut renvoyé à chaque demande de carte
        self.carte_apres: bytes | None = None  # carte du faux robot après une écriture (None : il l'ignore)
        self.ecritures: list[bytes] = []
        self.en_vol = 0
        self.max_en_vol = 0

    async def publier(self, sujet: str, charge: str, *, delai: float) -> None:
        octets = base64.b64decode(json.loads(charge)["message"])
        if self.sur_publication:
            self.sur_publication(self, octets)
        self.en_vol += 1
        self.max_en_vol = max(self.max_en_vol, self.en_vol)
        try:
            if self.muet:
                await asyncio.Event().wait()
            if self.retard:
                await asyncio.sleep(self.retard)
        finally:
            self.en_vol -= 1
        self.publies.append((sujet, octets))
        if octets == DEMANDE_CARTE and self.carte is not None and self.session is not None:
            if self.delai_robot:
                await asyncio.sleep(self.delai_robot)
            self.horloge.t += 0.01
            self.session.recevoir_etat({"mapRaw": self.carte})
        if octets.startswith(ENTETE_ECRITURE_CARTE):  # userCtrl 9 : le faux robot applique, ou non
            self.ecritures.append(octets)
            if self.carte_apres is not None:
                self.carte = self.carte_apres
        if octets == DEMANDE_CONFIGURATION and self.robot is not None and self.session is not None:
            if self.delai_robot:  # horloge réelle : la réponse doit être datée après la demande
                await asyncio.sleep(self.delai_robot)
            self.horloge.t += 0.01
            self.session.recevoir_etat(dict(self.robot))

    @property
    def trames(self) -> list[bytes]:
        return [octets for _sujet, octets in self.publies]

    @property
    def ordres(self) -> list[bytes]:
        """Tout ce qui n'est pas une requête pure."""
        return [o for o in self.trames if not est_requete_pure(o)]

    @property
    def consignes(self) -> list[bytes]:
        return [o for o in self.trames if est_consigne(o)]

    @property
    def arrets(self) -> list[bytes]:
        return [o for o in self.trames if o == ARRET]


@pytest.fixture(autouse=True)
def _isolement(monkeypatch):
    def interdit(*_args, **_kwargs):
        raise AssertionError("accès au coffre interdit pendant les tests")

    monkeypatch.setattr(coffre, "lire", lambda _cle: None)
    monkeypatch.setattr(coffre, "ecrire", interdit)
    monkeypatch.delenv("LYMOW_DONNEES", raising=False)
    journal._secrets_connus.clear()
    yield
    journal._secrets_connus.clear()


@pytest.fixture
def horloge() -> Horloge:
    return Horloge()


@pytest.fixture
def lien(horloge) -> FauxLien:
    return FauxLien(horloge)


@pytest.fixture
def messages() -> list[str]:
    return []


@pytest.fixture
def session(tmp_path, horloge, lien, messages) -> Session:
    """Session vierge ; le faux robot (en attente, à l'origine) répond aux sollicitations."""
    s = Session(
        lien, APPAREIL, Journal(tmp_path), Calibration(), frozenset(Categorie),
        horloge=horloge, dormir=horloge.dormir, signaler=messages.append,
    )
    lien.session = s
    return s


@pytest.fixture
def session_reelle(tmp_path, lien, messages) -> Session:
    """Session à l'horloge réelle (tests du serveur web) ; le faux robot répond après 30 ms."""
    s = Session(lien, APPAREIL, Journal(tmp_path), Calibration(), frozenset(Categorie), signaler=messages.append)
    lien.session = s
    lien.delai_robot = 0.03
    return s


def lignes_journal(dossier) -> list[dict]:
    fichiers = sorted((dossier / "journaux").glob("*.jsonl"))
    return [json.loads(ligne) for f in fichiers for ligne in f.read_text(encoding="utf-8").splitlines()]
