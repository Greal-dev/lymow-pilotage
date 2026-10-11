"""Logique pure de la signalisation vidéo (JavaScript de la page), exécutée hors navigateur.

Demande Node.js, seul moteur capable d'exécuter le fichier servi à la page ;
sans lui ces tests sont sautés et la logique reste « non vérifiée ».
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess

import pytest

from lymow_pilotage import ui

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js absent : signalisation vidéo non testée")
MODULE = ui.STATIQUE / "signalisation.js"

SDP_AVEC_OPTIONS = "v=0\r\na=ice-ufrag:abcd\r\na=ice-pwd:secret\r\na=ice-options:trickle\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
SDP_SANS_OPTIONS = "v=0\na=ice-ufrag:abcd\na=ice-pwd:secret\nm=video 9 UDP/TLS/RTP/SAVPF 96\n"


def js(expression: str, *arguments: object) -> object:
    """Évalue `expression` avec `S` = le module et `a` = les arguments, rend le résultat JSON."""
    script = (
        "const S = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(MODULE), json.dumps(arguments)], capture_output=True, text=True, timeout=20, check=True
    )
    return json.loads(sortie.stdout)


def charge_de(message: str) -> tuple[dict, dict]:
    enveloppe = json.loads(message)
    return enveloppe, json.loads(base64.b64decode(enveloppe["messagePayload"]).decode("utf-8"))


def test_base64_aller_retour_y_compris_hors_ascii():
    texte = "état prêt — 100 % ✓"
    encode = js("S.encoderBase64(a[0])", texte)
    assert encode == base64.b64encode(texte.encode("utf-8")).decode()
    assert js("S.decoderBase64(a[0])", encode) == texte


def test_sdp_ligne_ice_options_remplacee():
    sortie = js("S.ajusterSdp(a[0])", SDP_AVEC_OPTIONS)
    assert sortie.count("a=ice-options:") == 1
    assert "a=ice-options:trickle renomination\r\n" in sortie and "a=ice-options:trickle\r\n" not in sortie
    assert sortie.replace("a=ice-options:trickle renomination", "a=ice-options:trickle") == SDP_AVEC_OPTIONS


def test_sdp_ligne_ice_options_ajoutee_apres_ice_pwd_si_absente():
    sortie = js("S.ajusterSdp(a[0])", SDP_SANS_OPTIONS)
    lignes = sortie.split("\r\n")
    assert lignes[lignes.index("a=ice-pwd:secret") + 1] == "a=ice-options:trickle renomination"
    assert sortie.count("a=ice-options:") == 1


def test_sdp_deja_conforme_inchange_sur_le_fond():
    conforme = js("S.ajusterSdp(a[0])", SDP_AVEC_OPTIONS)
    assert js("S.ajusterSdp(a[0])", conforme) == conforme


def test_message_d_offre():
    enveloppe, charge = charge_de(js("S.messageOffre(a[0])", SDP_AVEC_OPTIONS))
    assert set(enveloppe) == {"action", "messagePayload"} and enveloppe["action"] == "SDP_OFFER"
    assert charge["type"] == "offer" and "a=ice-options:trickle renomination" in charge["sdp"]


def test_message_de_candidat():
    candidat = {"candidate": "candidate:1 1 udp 2122260223 192.168.1.2 54321 typ host", "sdpMid": "0", "sdpMLineIndex": 0}
    enveloppe, charge = charge_de(js("S.messageCandidat(a[0])", candidat))
    assert enveloppe["action"] == "ICE_CANDIDATE" and charge == candidat


def _recu(type_, charge, cle="messageType"):
    return json.dumps({cle: type_, "messagePayload": base64.b64encode(json.dumps(charge).encode()).decode()})


def test_decodage_de_la_reponse_et_des_candidats_du_robot():
    reponse = js("S.decoderMessage(a[0])", _recu("SDP_ANSWER", {"type": "answer", "sdp": "v=0\r\n", "intrus": 1}))
    assert reponse == {"type": "SDP_ANSWER", "charge": {"type": "answer", "sdp": "v=0\r\n"}}
    candidat = {"candidate": "candidate:1 1 udp 1 10.0.0.1 1 typ host", "sdpMid": "0"}
    assert js("S.decoderMessage(a[0])", _recu("ICE_CANDIDATE", candidat)) == {"type": "ICE_CANDIDATE", "charge": candidat}
    assert js("S.decoderMessage(a[0])", _recu("ICE_CANDIDATE", candidat, cle="action"))["type"] == "ICE_CANDIDATE"


@pytest.mark.parametrize(
    "message",
    [
        "", "ping", "{}", json.dumps({"messageType": "SDP_ANSWER"}),
        json.dumps({"messageType": "SDP_ANSWER", "messagePayload": "###"}),
        _recu("SDP_ANSWER", {"type": "answer"}), _recu("SDP_ANSWER", "texte"), _recu("SDP_ANSWER", None),
        _recu("ICE_CANDIDATE", {"sdpMid": "0"}), _recu("SDP_OFFER", {"type": "offer", "sdp": "v=0"}),
        _recu("STATUS_RESPONSE", {"statusCode": "400"}), _recu("GO_AWAY", {}),
    ],
)
def test_message_inattendu_ou_malforme_ignore(message):
    assert js("S.decoderMessage(a[0])", message) is None
