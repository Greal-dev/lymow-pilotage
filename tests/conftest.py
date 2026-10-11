"""Faux client MQTT, horloge virtuelle et garde-fous : aucun test ne touche le réseau ni le coffre."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from lymow_pilotage import coffre, journal
from lymow_pilotage.config import Calibration
from lymow_pilotage.journal import Journal
from lymow_pilotage.protocole import Categorie
from lymow_pilotage.session import Session

APPAREIL = "robot-test"
SUJET = "/device/robot-test/pbinput"
ARRET = bytes.fromhex("10 31 52 0a 0d 00 00 00 00 15 00 00 00 00")


class Horloge:
    """Temps virtuel : dormir avance l'horloge et rend la main à la boucle."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    async def dormir(self, secondes: float) -> None:
        self.t += secondes
        await asyncio.sleep(0)


class FauxLien:
    """Client MQTT factice : enregistre les publications, peut échouer sur commande."""

    def __init__(self) -> None:
        self.connecte = True
        self.publies: list[tuple[str, bytes]] = []
        self.sur_publication = None  # appelée avec (lien, octets) avant l'enregistrement

    async def publier(self, sujet: str, charge: str, *, delai: float) -> None:
        octets = base64.b64decode(json.loads(charge)["message"])
        if self.sur_publication:
            self.sur_publication(self, octets)
        self.publies.append((sujet, octets))

    @property
    def trames(self) -> list[bytes]:
        return [octets for _sujet, octets in self.publies]

    @property
    def consignes(self) -> list[bytes]:
        """Trames de pilotage non nulles (champ 10 présent, hors arrêt)."""
        return [o for o in self.trames if b"\x52\x0a" in o and o != ARRET]

    @property
    def arrets(self) -> list[bytes]:
        return [o for o in self.trames if o == ARRET]


def fragment(statut: int = 1, est: float = 0.0, nord: float = 0.0, theta: float = 0.0) -> dict:
    return {"workStatus": statut, "poseEastM": est, "poseNorthM": nord, "poseThetaRad": theta, "battery": 80}


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
def lien() -> FauxLien:
    return FauxLien()


@pytest.fixture
def messages() -> list[str]:
    return []


@pytest.fixture
def session(tmp_path, horloge, lien, messages) -> Session:
    """Session prête : robot en attente (statut 1), pose connue à l'origine."""
    s = Session(
        lien, APPAREIL, Journal(tmp_path), Calibration(), frozenset(Categorie),
        horloge=horloge, dormir=horloge.dormir, signaler=messages.append,
    )
    s.recevoir_etat(fragment())
    return s


def lignes_journal(dossier) -> list[dict]:
    fichiers = sorted((dossier / "journaux").glob("*.jsonl"))
    return [json.loads(ligne) for f in fichiers for ligne in f.read_text(encoding="utf-8").splitlines()]
