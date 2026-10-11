"""Décodage d'un état construit à la main, calibration, et correctifs apportés au socle."""

from __future__ import annotations

import asyncio
import json
import base64
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from lymow_pilotage import config, liaison
from lymow_pilotage.etat import Etat, Pose
from lymow_pilotage.session import EchecOrdre
from lymow_pilotage.vendor.ha_lymow import mqtt as socle_mqtt
from lymow_pilotage.vendor.ha_lymow.protocol import decode_pboutput

# PbOutput écrit à la main :
#   champ 3 (1a) erreurs compactées [19]
#   champ 5 (2a) PbRobotInfo {batterie(2)=80, wifi(3)=4, 4G(4)=3, workStatus(6)=1}
#   champ 6 (32) RTK {satellites(1)=22, statut(4)=3}
#   champ 10 (52) profil {softwareVersion(3)="v2.1"}
#   champ 14 (72) pose {est=1.0, nord=2.0, theta=pi/2 -> db 0f c9 3f}
#   champ 17 (8a 01) configuration {rcCutHeight(3)=60}
PBOUTPUT = bytes.fromhex(
    "1a 01 13"
    "2a 08 10 50 18 04 20 03 30 01"
    "32 04 08 16 20 03"
    "52 06 1a 04 76 32 2e 31"
    "72 0f 0d 00 00 80 3f 15 00 00 00 40 1d db 0f c9 3f"
    "8a 01 02 18 3c"
)


def test_etat_decode_et_resume():
    etat = Etat(en_ligne=True)
    etat.appliquer(decode_pboutput(PBOUTPUT), 100.0)
    resume = etat.resume(112.5)
    assert resume["batterie_pct"] == 80
    assert resume["statut"] == 1 and resume["statut_libelle"] == "prêt, en attente d'ordre"
    assert resume["erreurs"] == ["19 : Robot wheel slip"] and resume["avertissements"] == []
    assert (resume["position_est_m"], resume["position_nord_m"], resume["cap_deg"]) == (1.0, 2.0, 90.0)
    assert resume["rtk"] == "FIXED" and resume["rtk_satellites"] == 22
    assert (resume["signal_wifi"], resume["signal_4g"]) == (4, 3)
    assert resume["hauteur_coupe_mm"] == 60 and resume["micrologiciel"] == "v2.1"
    assert resume["age_etat_s"] == 12.5 and resume["en_ligne"] is True


def test_reponse_partielle_ne_rafraichit_ni_le_statut_ni_les_erreurs():
    etat = Etat()
    etat.appliquer(decode_pboutput(PBOUTPUT), 100.0)
    etat.appliquer(decode_pboutput(bytes.fromhex("8a 01 02 18 64")), 150.0)  # configuration seule
    assert etat.recu_statut == 100.0 and etat.recu_pose == 100.0
    assert etat.hauteur_coupe_mm == 100
    assert etat.resume(150.0)["erreurs"] == ["19 : Robot wheel slip"]


def test_pose_non_finie_ignoree():
    etat = Etat()
    etat.appliquer({"workStatus": 1, "poseEastM": math.nan, "poseNorthM": 0.0, "poseThetaRad": 0.0}, 1.0)
    assert etat.pose is None


def test_ecart_de_cap_passe_par_le_plus_court():
    assert Pose(0, 0, 170.0).vers(Pose(0, 0, -170.0)) == (0.0, 20.0)
    assert Pose(0, 0, 0.0).vers(Pose(3, 4, -90.0)) == (5.0, -90.0)


# --- calibration ------------------------------------------------------------


def test_calibration_du_depot_egale_les_valeurs_par_defaut():
    racine = Path(__file__).resolve().parents[1]
    assert config.charger_calibration(racine) == config.Calibration()


def test_calibration_absente_valeurs_par_defaut(tmp_path):
    cal = config.charger_calibration(tmp_path)
    assert (cal.signe_angulaire, cal.variante_trame, cal.version_protocole, cal.plateau_haut_mm) == (1, "mqtt", 49, 100)


@pytest.mark.parametrize(
    "contenu",
    [
        "facteur_distance = 0.1", "facteur_angle = 5.0", "signe_angulaire = 0", "version_protocole = 12",
        'variante_trame = "usb"', "vitesse = 3.0", 'facteur_distance = "1"', "signe_angulaire = true", "= cassé",
    ],
)
def test_calibration_hors_borne_ou_inconnue_refusee(tmp_path, contenu):
    (tmp_path / "calibration.toml").write_text(contenu, encoding="utf-8")
    with pytest.raises(config.ErreurConfiguration):
        config.charger_calibration(tmp_path)


def test_dossier_de_donnees_option_puis_variable_puis_racine(tmp_path, monkeypatch):
    assert config.dossier_donnees(str(tmp_path)) == tmp_path.resolve()
    monkeypatch.setenv("LYMOW_DONNEES", str(tmp_path))
    assert config.dossier_donnees() == tmp_path.resolve()
    monkeypatch.delenv("LYMOW_DONNEES")
    assert (config.dossier_donnees() / "pyproject.toml").is_file()


# --- correctifs du socle ----------------------------------------------------


def _client(etats: list, presences: list) -> socle_mqtt.LymowMqttClient:
    client = socle_mqtt.LymowMqttClient(
        "hote", "eu-west-1", lambda a, f: etats.append((a, f)), lambda a, e: presences.append((a, e))
    )
    client._things = ["robot-test"]
    return client


def test_socle_reception_decode_l_etat_et_la_presence():
    etats, presences = [], []
    client = _client(etats, presences)
    enveloppe = json.dumps({"message": base64.b64encode(PBOUTPUT).decode()})
    client._dispatch(SimpleNamespace(topic="/device/robot-test/pboutput", payload=enveloppe.encode()))
    client._dispatch(SimpleNamespace(topic="/device/robot-test/notify-app", payload=b'{"robotState":"online"}'))
    client._dispatch(SimpleNamespace(topic="/device/robot-test/pboutput", payload=b"pas du json"))
    client._dispatch(SimpleNamespace(topic="/device/autre/pboutput", payload=enveloppe.encode()))
    assert len(etats) == 1 and etats[0][1]["workStatus"] == 1
    assert presences == [("robot-test", True)]


def test_socle_publication_sans_connexion_leve_au_lieu_d_abandonner():
    client = _client([], [])
    with pytest.raises(socle_mqtt.MqttNotConnectedError):
        asyncio.run(client.async_publish("/device/robot-test/pbinput", "{}", timeout=1.0))


def test_socle_liaison_perdue_quand_l_ecoute_s_interrompt():
    class Messages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionError("liaison coupée")

    client = _client([], [])
    client._client = SimpleNamespace(messages=Messages())
    assert client.is_connected
    asyncio.run(client._listen_loop())
    assert not client.is_connected


def test_echec_de_connexion_mqtt_sans_url_presignee_dans_l_erreur():
    class ClientEnPanne:
        is_connected = False

        async def connect(self, *_args):
            raise ConnectionError("wss://hote/mqtt?X-Amz-Signature=deadbeef&X-Amz-Security-Token=zzz")

    ident = liaison.Identifiants("eu-west-1", "robot-test", "google", "raf-secret-555")
    acces = liaison.Acces("identite", "acces", {"AccessKeyId": "AKIAEXEMPLE", "SecretKey": "cle-secrete"})
    lien = liaison.LienMqtt(ClientEnPanne(), ident, acces)
    with pytest.raises(EchecOrdre) as erreur:
        asyncio.run(lien.connecter())
    assert "X-Amz" not in str(erreur.value) and erreur.value.__cause__ is None
    assert "raf-secret-555" not in repr(ident) and "cle-secrete" not in repr(acces)
