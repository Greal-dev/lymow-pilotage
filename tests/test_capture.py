"""Commande `lymow capture` : enregistrement brut des messages reçus, hors ligne avec un faux lien."""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from conftest import DEMANDE_CARTE, lignes_journal

from lymow_pilotage import capture, cli, journal, liaison
from lymow_pilotage.protocole import est_requete_pure
from lymow_pilotage.vendor.ha_lymow import mqtt as socle_mqtt

SUJET_ETAT = "/device/robot-test/pboutput"


def varint(n: int) -> bytes:
    sortie = b""
    while True:
        n, octet = n >> 7, n & 0x7F
        sortie += bytes([octet | (0x80 if n else 0)])
        if not n:
            return sortie


def champ(numero: int, contenu: bytes) -> bytes:
    return varint(numero << 3 | 2) + varint(len(contenu)) + contenu


def enveloppe(protobuf: bytes) -> bytes:
    return json.dumps({"message": base64.b64encode(protobuf).decode()}).encode()


POINT = b"\x0d\x00\x00\x80\x3f\x15\x00\x00\x00\x40"  # deux fixe32
ZONE = champ(1, b"zone-a") + b"".join(champ(3, POINT) for _ in range(40))
ENTIER = b"\x10\x31" + champ(23, champ(2, champ(1, ZONE) + b"\x28\x01"))
COUPE = ENTIER[:-25]  # tranche de taille fixe : le dernier champ annonce plus qu'il n'en reste


@pytest.fixture
def liaison_capture(monkeypatch, session):
    """Faux lien : à la demande de carte, le « robot » répond par deux messages bruts."""
    ouvertures = []

    @asynccontextmanager
    async def ouvrir(_journal, _cal, autorisees, *_args, brut=None):
        ouvertures.append(autorisees)
        session.autorisees = autorisees

        def repondre(_lien, octets):
            if octets == DEMANDE_CARTE:
                brut(SUJET_ETAT, enveloppe(COUPE), False)
                brut(SUJET_ETAT, enveloppe(ENTIER), False)
            else:
                brut("/device/robot-test/notify-app", '{"robotState":"online"}', True)

        session.lien.sur_publication = repondre
        yield session

    monkeypatch.setattr(liaison, "ouvrir_session", ouvrir)
    return ouvertures


def lancer(capsys, tmp_path, *arguments):
    code = cli.main(["capture", *arguments, "--json", "--donnees", str(tmp_path)])
    sortie = capsys.readouterr()
    return code, (json.loads(sortie.out) if sortie.out.strip() else None), sortie.err


def test_capture_enregistre_chaque_message_tel_quel_avec_index_et_vidage(capsys, tmp_path, liaison_capture, lien):
    code, bilan, _err = lancer(capsys, tmp_path, "--carte", "--secondes", "5")
    assert code == 0 and bilan["messages"] == 4 and bilan["par_sujet"] == {"notify-app": 2, "pboutput": 2}
    assert bilan["champs_de_premier_niveau"] == {"2": 2, "23": 2} and bilan["protobuf_incomplets"] == 1
    assert bilan["tailles"][2:] == [len(enveloppe(COUPE)), len(enveloppe(ENTIER))] and bilan["carte_demandee"] is True
    dossier = next((tmp_path / "captures").iterdir())
    assert str(dossier) == bilan["dossier"]
    fiche = json.loads((dossier / "0003.json").read_text(encoding="utf-8"))
    assert base64.b64decode(fiche["charge_b64"]) == enveloppe(COUPE)  # tel quel, à l'octet près
    assert fiche["sujet"] == SUJET_ETAT and fiche["taille"] == len(enveloppe(COUPE)) and fiche["horodatage"][:2] == "20"
    assert json.loads((dossier / "0001.json").read_text(encoding="utf-8"))["retenu"] is True
    index = [json.loads(ligne) for ligne in (dossier / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [ligne["numero"] for ligne in index] == [1, 2, 3, 4] and "charge_b64" not in index[2]
    assert index[2]["lecture_complete"] is False and index[3]["lecture_complete"] is True
    assert index[3]["champs"] == [[2, "varint", 2], [23, "LEN", len(ENTIER) - 6]]
    assert not (dossier / "0001.vidage.txt").exists()  # présence : pas du protobuf
    assert "capture" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_capture_n_emet_que_des_requetes_de_la_liste_blanche(capsys, tmp_path, liaison_capture, lien):
    lancer(capsys, tmp_path, "--secondes", "2")
    sans_carte = list(lien.trames)
    lancer(capsys, tmp_path, "--carte", "--secondes", "2", "--dossier", str(tmp_path / "ailleurs"))
    assert len(sans_carte) == 2 and DEMANDE_CARTE not in sans_carte
    assert lien.trames[2:].count(DEMANDE_CARTE) == 1 and len(lien.trames) == 5
    assert all(est_requete_pure(trame) for trame in lien.trames) and lien.ordres == []
    assert all(autorisees == cli._LECTURE for autorisees in liaison_capture)
    assert len(list((tmp_path / "ailleurs").iterdir())) == 1


def test_capture_ecoute_la_duree_demandee_bornee(capsys, tmp_path, liaison_capture, horloge):
    depart = horloge.t
    code, bilan, _err = lancer(capsys, tmp_path)
    assert code == 0 and bilan["secondes"] == 30 and 30 <= horloge.t - depart < 32
    for trop in ("0", "121"):
        code, _bilan, err = lancer(capsys, tmp_path, "--secondes", trop)
        assert code == cli.CODE_REFUS and "de 1 à 120" in err


def test_capture_sans_reel_possible_et_sans_message_code_1(capsys, tmp_path, liaison_capture, session):
    session.lien.connecte = False  # rien n'est émis, rien n'arrive
    code, bilan, _err = lancer(capsys, tmp_path, "--secondes", "3")
    assert code == 1 and bilan["messages"] == 0 and "liaison perdue" in bilan["note"]


def test_vidage_structurel_generique_sans_interpretation():
    lignes = capture.vider(ENTIER)
    assert lignes[0] == "2 varint = 49" and lignes[1] == f"23 LEN {len(ENTIER) - 6}"
    assert any("3 LEN ×40 · longueurs 10 à 10 · total 400 (seul le premier est détaillé" in ligne for ligne in lignes)
    assert "        2 fixe32" in lignes and "    5 varint = 1" in lignes
    assert not any(mot in " ".join(lignes).lower() for mot in ("zone", "carte", "map", "polygon"))


def test_vidage_dit_ou_un_message_coupe_s_arrete():
    lignes = capture.vider(COUPE)
    assert "TRONQUÉ" in lignes[1] and f"{len(ENTIER) - 6} octets annoncés, {len(COUPE) - 6} présents" in lignes[1]
    assert sum("TRONQUÉ" in ligne for ligne in lignes) >= 3  # chaque niveau traversé par la coupure
    assert capture.vider(b"\x0b\x01")[-1].startswith("!! lecture arrêtée, octet 0 sur 2")
    assert capture.decouper(b"\x08\xff\xff")[1] == "octet 0 sur 3 : varint coupé"
    assert capture.decouper(b"\x0d\x00\x00")[1] == "octet 0 sur 3 : fixe32 coupé"


@pytest.mark.parametrize(
    "protobuf",
    [
        champ(4, b"https://exemple.invalid/carte?X-Amz-Signature=abcdef0123"),
        champ(4, b"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eHh4In0.signature"),
        champ(4, b"JETON-DE-RAFRAICHISSEMENT-123456"),
    ],
)
def test_capture_n_ecrit_ni_jeton_ni_url_signee(tmp_path, protobuf):
    journal.declarer_secret("JETON-DE-RAFRAICHISSEMENT-123456")
    ecoute = capture.Enregistreur(tmp_path / "c")
    ecoute.recevoir(SUJET_ETAT, enveloppe(protobuf))
    ecoute.recevoir(SUJET_ETAT, enveloppe(ENTIER))
    ecrit = b"".join(fichier.read_bytes() for fichier in (tmp_path / "c").iterdir())
    for interdit in (b"X-Amz", b"exemple.invalid", b"eyJhbGci", b"JETON-DE", base64.b64encode(protobuf)[:24]):
        assert interdit not in ecrit
    assert ecoute.resume()["charges_ecartees"] == 1 and "ecartee" in json.loads((tmp_path / "c" / "0001.json").read_text("utf-8"))
    assert not (tmp_path / "c" / "0001.vidage.txt").exists() and (tmp_path / "c" / "0002.vidage.txt").exists()


def test_charge_binaire_ordinaire_jamais_ecartee_par_hasard():
    import random

    alea = random.Random(7)
    assert not any(journal.contient_acces(alea.randbytes(81920)) for _ in range(20))


def test_capture_bornee_en_nombre_de_fichiers(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MESSAGES_MAX", 3)
    ecoute = capture.Enregistreur(tmp_path / "c")
    for _ in range(5):
        ecoute.recevoir(SUJET_ETAT, enveloppe(ENTIER))
    assert len(list((tmp_path / "c").glob("0*.json"))) == 3 and ecoute.resume()["non_ecrits"] == 2
    assert ecoute.resume()["messages"] == 5


def test_socle_remet_chaque_message_brut_avant_decodage_sans_tuer_l_ecoute():
    etats, bruts = [], []
    client = socle_mqtt.LymowMqttClient("hote", "eu-west-1", lambda a, f, r: etats.append(f), lambda *_a: None)
    client._things = ["robot-test"]
    client.on_raw = lambda sujet, charge, retenu: bruts.append((sujet, charge, retenu))
    client._dispatch(SimpleNamespace(topic=SUJET_ETAT, payload=enveloppe(ENTIER), retain=True))
    client._dispatch(SimpleNamespace(topic="/device/autre/pboutput", payload=b"pas du json"))
    assert bruts == [(SUJET_ETAT, enveloppe(ENTIER), True), ("/device/autre/pboutput", b"pas du json", False)]
    client.on_raw = lambda *_a: 1 / 0
    client._dispatch(SimpleNamespace(topic=SUJET_ETAT, payload=enveloppe(ENTIER)))
    assert len(etats) == 2  # l'état reste décodé même si la capture échoue
