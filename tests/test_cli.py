"""SEC-05 : simulation par défaut, `--reel` explicite ; aiguillage de la commande."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from conftest import ARRET, est_consigne, fragment, lignes_journal

from lymow_pilotage import cli, liaison
from lymow_pilotage.protocole import est_requete_pure


@pytest.fixture
def reseau_interdit(monkeypatch):
    """Toute tentative d'ouvrir une liaison ou une session HTTP fait échouer le test."""

    def interdit(*_args, **_kwargs):
        raise AssertionError("ouverture de connexion en simulation")

    monkeypatch.setattr(liaison, "ouvrir_session", interdit)
    monkeypatch.setattr(aiohttp, "ClientSession", interdit)


@pytest.fixture
def liaison_factice(monkeypatch, session):
    ouvertures = []

    @asynccontextmanager
    async def ouvrir(journal, cal, autorisees, *_args):
        ouvertures.append(autorisees)
        session.autorisees = autorisees
        yield session

    monkeypatch.setattr(liaison, "ouvrir_session", ouvrir)
    return ouvertures


def lancer(capsys, tmp_path, *arguments: str) -> tuple[int, dict]:
    code = cli.main([*arguments, "--json", "--donnees", str(tmp_path)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    "arguments",
    [
        ("tourner", "10"), ("avancer", "1"), ("reculer", "0.5"), ("plateau-haut",),
        ("stop",), ("reprendre",), ("base",), ("annuler",),
    ],
)
def test_sec05_sans_reel_aucune_connexion_et_zero_publication(capsys, tmp_path, reseau_interdit, lien, arguments):
    code, sortie = lancer(capsys, tmp_path, *arguments)
    assert code == 0
    assert sortie["mode"] == "simulation"
    assert sortie["sujet"] == "/device/<appareil>/pbinput"
    assert sortie["trame"]["hexadecimal"] and sortie["trame"]["base64"]
    assert lien.publies == []
    assert [ligne["evenement"] for ligne in lignes_journal(tmp_path)] == ["ordre-simule"]  # SEC-06


def test_sec05_aucune_variable_d_environnement_ne_vaut_reel(capsys, tmp_path, reseau_interdit, monkeypatch):
    for nom in ("LYMOW_REEL", "REEL", "LYMOW_MODE"):
        monkeypatch.setenv(nom, "1")
    code, sortie = lancer(capsys, tmp_path, "avancer", "1")
    assert code == 0 and sortie["mode"] == "simulation"


def test_simulation_affiche_duree_cadence_et_arret_final(capsys, tmp_path, reseau_interdit):
    _code, sortie = lancer(capsys, tmp_path, "avancer", "1")
    assert sortie["duree_s"] == 4.0
    assert sortie["cadence_hz"] == 10
    assert sortie["emissions_prevues"] == 40
    assert sortie["trame"]["hexadecimal"] == "10 31 52 0a 0d 00 00 80 3e 15 00 00 00 00"
    assert sortie["trame"]["champs"]["vitesse_lineaire_m_s"] == 0.25
    assert sortie["arret_final"]["repetitions"] == 3
    assert sortie["arret_final"]["hexadecimal"] == ARRET.hex(" ")


def test_simulation_de_stop_affiche_les_deux_trames(capsys, tmp_path, reseau_interdit):
    _code, sortie = lancer(capsys, tmp_path, "stop")
    assert sortie["trame"]["hexadecimal"] == ARRET.hex(" ") and sortie["repetitions"] == 3
    assert sortie["puis_pause"]["hexadecimal"] == "10 31 28 03"
    assert "userCtrl 21" in sortie["puis_pause"]["variante"]


def test_simulation_sequence(capsys, tmp_path, reseau_interdit):
    code, sortie = lancer(capsys, tmp_path, "sequence", "plateau-haut; tourner 90; avancer 1")
    assert code == 0 and sortie["interrompue"] is False
    assert [etape["ordre"] for etape in sortie["etapes"]] == ["plateau-haut", "tourner 90", "avancer 1"]
    assert len(lignes_journal(tmp_path)) == 3


def test_sec07_sequence_simulee_s_arrete_au_premier_refus(capsys, tmp_path, reseau_interdit):
    code, sortie = lancer(capsys, tmp_path, "sequence", "tourner 90; avancer 9; avancer 1")
    assert code == 2 and sortie["interrompue"] is True
    assert sortie["etapes"] == []  # aucune étape n'est même simulée
    assert "avancer 9" in sortie["motif"] and "SEC-02" in sortie["motif"]
    assert sortie["non_executees"] == ["tourner 90", "avancer 9", "avancer 1"]
    assert [ligne["evenement"] for ligne in lignes_journal(tmp_path)] == ["ordre-refuse"]


def test_sec07_sequence_reelle_refusee_avant_toute_connexion(capsys, tmp_path, reseau_interdit):
    code, sortie = lancer(capsys, tmp_path, "sequence", "avancer 1; tourner 200", "--reel")
    assert code == 2 and sortie["mode"] == "réel" and sortie["etapes"] == []
    assert "SEC-02" in sortie["motif"]


def test_sec07_sequence_reelle_interrompue_dit_pourquoi_et_ce_qui_reste(capsys, tmp_path, liaison_factice, lien):
    code, sortie = lancer(capsys, tmp_path, "sequence", "avancer 1; tourner 90", "--reel")
    assert code == 1 and sortie["interrompue"] is True
    assert sortie["motif"].startswith("séquence interrompue après « avancer 1 » : le robot n'a pas bougé")
    assert sortie["non_executees"] == ["tourner 90"]
    assert len(lien.consignes) == 40


def test_arret_non_confirme_code_de_sortie_dedie_et_alerte_en_tete(capsys, tmp_path, liaison_factice, lien):
    def coupure(lien_, octets):
        if est_consigne(octets):
            lien_.connecte = False

    lien.sur_publication = coupure
    code = cli.main(["avancer", "1", "--reel", "--json", "--donnees", str(tmp_path)])
    sortie = capsys.readouterr()
    assert code == 3 and code != cli.CODE_REFUS
    assert sortie.err.startswith("ARRÊT NON CONFIRMÉ")
    assert "ARRÊT NON CONFIRMÉ (0/3 acquittés)" in json.loads(sortie.out)["echec"]


@pytest.mark.parametrize(
    "arguments",
    [("avancer", "1", "--re"), ("--ree", "avancer", "1"), ("stop", "--r"), ("avancer", "1", "--js")],
)
def test_aucune_abreviation_d_option_n_est_acceptee(capsys, tmp_path, reseau_interdit, arguments):
    with pytest.raises(SystemExit) as sortie:
        cli.main([*arguments, "--donnees", str(tmp_path)])
    assert sortie.value.code == 2


@pytest.mark.parametrize("valeur", ["1_0", "١", "1e1", "inf", "nan"])
def test_nombre_exotique_refuse_des_la_ligne_de_commande(capsys, tmp_path, reseau_interdit, valeur):
    with pytest.raises(SystemExit) as sortie:
        cli.main(["avancer", valeur, "--donnees", str(tmp_path)])
    assert sortie.value.code == 2


def test_sequence_mal_ecrite_refusee_en_bloc(capsys, tmp_path, reseau_interdit):
    code = cli.main(["sequence", "tourner 90; voler 3", "--donnees", str(tmp_path)])
    assert code == 2
    assert "étape inconnue" in capsys.readouterr().err
    assert not (tmp_path / "journaux").exists()


def test_plafond_depasse_en_simulation(capsys, tmp_path, reseau_interdit):
    code = cli.main(["tourner", "270", "--donnees", str(tmp_path)])
    assert code == 2
    assert "SEC-02" in capsys.readouterr().out
    assert [ligne["evenement"] for ligne in lignes_journal(tmp_path)] == ["ordre-refuse"]


def test_sortie_lisible_en_francais(capsys, tmp_path, reseau_interdit):
    assert cli.main(["tourner", "-45", "--donnees", str(tmp_path)]) == 0
    texte = capsys.readouterr().out
    assert "mode : simulation" in texte
    assert "durée (s) :" in texte and "vitesse angulaire (rad/s) : -0.35" in texte


def test_reel_explicite_ouvre_la_liaison_et_stop_part_sans_precondition(
    capsys, tmp_path, liaison_factice, lien, session
):
    lien.robot = None  # robot muet : aucun état, et l'arrêt part quand même
    code, sortie = lancer(capsys, tmp_path, "stop", "--reel")
    assert code == 0 and sortie["mode"] == "réel"
    assert len(liaison_factice) == 1
    assert lien.trames[:3] == [ARRET] * 3


def test_reel_accepte_avant_la_commande(capsys, tmp_path, liaison_factice, lien):
    code, sortie = lancer(capsys, tmp_path, "--reel", "avancer", "0.5")
    assert code == 0 and sortie["emissions"] == 20
    assert len(lien.consignes) == 20 and len(lien.arrets) == 3


def test_reel_refuse_par_sec01_sort_en_erreur(capsys, tmp_path, liaison_factice, lien, session):
    lien.robot = fragment(statut=2)
    code, sortie = lancer(capsys, tmp_path, "avancer", "1", "--reel")
    assert code == 1 and "SEC-01" in sortie["echec"]
    assert lien.ordres == []


class FauxHttp:
    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FauxApi:
    def __init__(self, *_args) -> None:
        pass

    async def get_devices(self):
        return [{"deviceThingName": "robot-test", "deviceName": "Tondeuse", "sn": "ne-doit-pas-sortir"}]

    async def get_device_info(self, _appareil):
        return {"softwareVersion": "v2.1.48"}


def test_diagnostic_sans_jeton_s_arrete_sans_reseau(capsys, tmp_path, reseau_interdit):
    code, sortie = lancer(capsys, tmp_path, "diagnostic")
    assert code == 1 and sortie["diagnostic"] == "incomplet"
    assert sortie["etapes"] == [
        {"etape": "jeton de rafraîchissement présent", "reussi": False,
         "detail": "aucune connexion enregistrée : lancez d'abord « lymow connexion »"}
    ]


def test_diagnostic_complet_ne_publie_rien(capsys, tmp_path, monkeypatch, liaison_factice, lien, session):
    session.recevoir_etat(fragment())  # battement spontané du robot, reçu en écoute passive

    async def acces(_http, _ident):
        return liaison.Acces("identite", "jeton-acces-123", {})

    ident = liaison.Identifiants("eu-west-1", "robot-test", "google", "raf-secret-555")
    monkeypatch.setattr(liaison, "lire_identifiants", lambda: ident)
    monkeypatch.setattr(liaison, "obtenir_acces", acces)
    monkeypatch.setattr(cli, "LymowApiClient", FauxApi)
    monkeypatch.setattr(aiohttp, "ClientSession", FauxHttp)
    code, sortie = lancer(capsys, tmp_path, "diagnostic")
    assert code == 0 and sortie["diagnostic"] == "complet"
    assert [etape["etape"] for etape in sortie["etapes"]] == [
        "jeton de rafraîchissement présent", "rafraîchissement du jeton", "liste des appareils",
        "connexion MQTT", "réception d'un état", "version du micrologiciel",
    ]
    assert sortie["etapes"][2]["detail"] == [{"nom": "Tondeuse", "appareil": "robot-test"}]
    assert sortie["etapes"][5]["detail"] == "v2.1.48"
    assert lien.publies == [] and liaison_factice == [frozenset()]
    assert "raf-secret-555" not in json.dumps(sortie)


def test_connexion_google_ne_stocke_que_le_jeton_de_rafraichissement(capsys, tmp_path, monkeypatch):
    from lymow_pilotage import auth, coffre
    from lymow_pilotage.vendor.ha_lymow.auth import LymowAuth

    stocke: dict[str, str] = {}
    invites: list[str] = []

    async def echange(_http, demande, saisie):
        assert saisie == "adresse-de-rappel-collee"
        return {"AccessToken": "acces-court-111", "IdToken": "identite-courte-222", "RefreshToken": "raf-long-333"}

    async def cles_aws(_self, _id_token, _region):
        return {"identity_id": "identite-aws-444", "credentials": {}}

    def saisie_masquee(invite):
        invites.append(invite)
        return "adresse-de-rappel-collee"

    monkeypatch.setattr("builtins.input", lambda _invite: "")  # région par défaut
    monkeypatch.setattr(cli.getpass, "getpass", saisie_masquee)
    monkeypatch.setattr(auth, "echanger_code", echange)
    monkeypatch.setattr(LymowAuth, "get_aws_credentials", cles_aws)
    monkeypatch.setattr(cli, "LymowApiClient", FauxApi)
    monkeypatch.setattr(aiohttp, "ClientSession", FauxHttp)
    monkeypatch.setattr(coffre, "ecrire", stocke.__setitem__)

    assert cli.main(["connexion", "--donnees", str(tmp_path)]) == 0
    assert stocke == {
        "jeton-rafraichissement": "raf-long-333", "region": "eu-west-1",
        "appareil": "robot-test", "methode": "google",
    }
    sortie = capsys.readouterr()
    assert "https://eu-auth.lymow.com/oauth2/authorize?" in sortie.out and "Preserve log" in sortie.out
    assert len(invites) == 1  # l'adresse de rappel passe par getpass, donc sans écho
    for secret in ("raf-long-333", "acces-court-111", "identite-courte-222", "identite-aws-444"):
        assert secret not in sortie.out + sortie.err
    assert not any(tmp_path.iterdir())  # aucun fichier, donc aucun secret sur disque


def test_etat_n_emet_que_des_requetes_pures_et_passif_rien(capsys, tmp_path, liaison_factice, lien):
    code, sortie = lancer(capsys, tmp_path, "etat")
    assert code == 0
    assert sortie["statut_libelle"] == "prêt, en attente d'ordre" and sortie["batterie_pct"] == 80
    assert len(lien.trames) == 2 and all(est_requete_pure(octets) for octets in lien.trames)

    lien.publies.clear()
    code, _sortie = lancer(capsys, tmp_path, "etat", "--passif")
    assert code == 0 and lien.publies == []
    assert liaison_factice[1] == frozenset()
