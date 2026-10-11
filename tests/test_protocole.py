"""Encodeurs comparés octet pour octet à des références calculées à la main.

Rappel protobuf : étiquette = (champ << 3) | type ; type 0 = varint,
2 = longueur + octets, 5 = 32 bits petit-boutiste.
  version      champ 2  varint -> 10 ; 49 = 31, 40 = 28
  userCtrl     champ 5  varint -> 28
  champ 7               varint -> 38
  baseInput    champ 9  octets -> 4a
  remoteControl champ 10 octets -> 52 ; linearSpeed -> 0d, angularSpeed -> 15
  map          champ 12 octets -> 62 ; runTimeConfig champ 13 -> 6a ; cutHeight -> 08
  uuid         champ 27 octets -> da 01
Flottants simple précision : 0.25 = 3e800000, 0.35 = 3eb33333 (octets inversés sur le fil).
"""

from __future__ import annotations

import base64
import json

import pytest

from lymow_pilotage import protocole as p
from lymow_pilotage.config import Calibration
from lymow_pilotage.securite import RefusSecurite


def h(texte: str) -> bytes:
    return bytes.fromhex(texte)


@pytest.mark.parametrize(
    ("lineaire", "angulaire", "reference"),
    [
        (0.25, 0.0, "10 31 52 0a 0d 00 00 80 3e 15 00 00 00 00"),
        (-0.25, 0.0, "10 31 52 0a 0d 00 00 80 be 15 00 00 00 00"),
        (0.0, 0.35, "10 31 52 0a 0d 00 00 00 00 15 33 33 b3 3e"),
        (0.0, -0.35, "10 31 52 0a 0d 00 00 00 00 15 33 33 b3 be"),
    ],
)
def test_encode_remote_control_variante_mqtt_version_49(lineaire, angulaire, reference):
    assert p.encode_remote_control(lineaire, angulaire) == h(reference)


def test_encode_remote_control_variante_ble_ajoute_le_champ_7():
    attendu = h("10 31 38 02 52 0a 0d 00 00 80 3e 15 00 00 00 00")
    assert p.encode_remote_control(0.25, 0.0, variante="ble") == attendu


def test_encode_remote_control_version_40():
    attendu = h("10 28 52 0a 0d 00 00 80 3e 15 00 00 00 00")
    assert p.encode_remote_control(0.25, 0.0, version=40) == attendu


def test_encode_remote_stop_serialise_les_deux_vitesses_nulles():
    assert p.encode_remote_stop() == h("10 31 52 0a 0d 00 00 00 00 15 00 00 00 00")


def test_encode_set_cut_height_variante_socle_avec_userctrl_50():
    assert p.encode_set_cut_height(100) == h("10 31 28 32 62 04 6a 02 08 64")


def test_encode_set_cut_height_variante_lymow_ha_sans_userctrl():
    assert p.encode_set_cut_height(100, variante="lymow-ha", version=40) == h("10 28 62 04 6a 02 08 64")


@pytest.mark.parametrize("millimetres", [19, 101, 1000, -5, True, 50.0])
def test_encode_set_cut_height_refuse_hors_bornes(millimetres):
    with pytest.raises(RefusSecurite):
        p.encode_set_cut_height(millimetres)


@pytest.mark.parametrize(
    ("code", "reference"),
    [(4, "10 31 28 04"), (33, "10 31 28 21"), (28, "10 31 28 1c"), (32, "10 31 28 20")],
)
def test_encode_ordre_tache_codes_autorises(code, reference):
    assert p.encode_ordre_tache(code) == h(reference)


@pytest.mark.parametrize("code", [1, 2, 3, 5, 26, 37, 46, 50, 0, -1, 99])
def test_encode_ordre_tache_refuse_tout_autre_code_dont_le_demarrage_de_tonte(code):
    # SEC-01
    with pytest.raises(RefusSecurite):
        p.encode_ordre_tache(code)


def test_trames_de_requete_pure():
    cal = Calibration()
    assert p.trame_presence("abc").octets == h("38 02 da 01 03 61 62 63")
    assert p.trame_demande_configuration(cal).octets == h("10 31 4a 02 50 01")


def test_enveloppe_json_base64():
    trame = p.trame_arret(Calibration())
    reference = h("10 31 52 0a 0d 00 00 00 00 15 00 00 00 00")
    assert json.loads(trame.charge()) == {"message": base64.b64encode(reference).decode()}
    assert trame.categorie is p.Categorie.ARRET


def test_sujets():
    assert p.sujet_ordres("robot-1") == "/device/robot-1/pbinput"
    assert p.sujet_etat("robot-1") == "/device/robot-1/pboutput"
    assert p.sujet_presence("robot-1") == "/device/robot-1/notify-app"


@pytest.mark.parametrize("appareil", ["", "a/b", "#", "robot/+", "x" * 200, "robot 1"])
def test_sujet_refuse_un_identifiant_qui_detournerait_le_sujet(appareil):
    with pytest.raises(ValueError):
        p.sujet_ordres(appareil)


def test_liste_blanche_des_requetes_pures():
    cal = Calibration()
    assert p.est_requete_pure(p.trame_presence("session").octets)
    assert p.est_requete_pure(p.trame_demande_configuration(cal).octets)
    for interdit in (
        p.trame_arret(cal).octets,
        p.trame_consigne(0.25, 0.0, cal).octets,
        p.trame_hauteur(100, cal).octets,
        p.trame_tache(4, cal).octets,
        h("38 01 da 01 03 61 62 63"),  # présence avec une autre valeur
        h("10 31 4a 02 50 02"),  # champ 9 avec un autre contenu
        h("10 31 28 13"),  # userCtrl 19 : demande de carte, hors liste
        b"",
        b"\xff\xff\xff",
    ):
        assert not p.est_requete_pure(interdit)
