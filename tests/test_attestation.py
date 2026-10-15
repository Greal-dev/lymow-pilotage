"""SEC-01b : attestation « lames à l'arrêt constatées par l'opérateur » (décision du 2026-10-12).

Symptôme d'origine : robot en pause, lames arrêtées à vue, mais statut 2 publié — « statut de
travail 2 hors de la liste "lames à l'arrêt" : mouvement refusé (SEC-01) ».
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest
from conftest import ARRET, DEMANDE_CONFIGURATION, fragment, lignes_journal
from test_demo import serveur_demo
from test_fleches import armer, fleche
from test_mouvement import AVANCER_1, CONSIGNE_AVANT, a_la_consigne, lancer, robot_ensuite
from test_ordres_page import js, sans_node
from test_ui import AVANCER_1 as PAGE_AVANCER_1
from test_ui import attendre, executer, lire, poster, serveur_ui

import lymow_pilotage
from lymow_pilotage import mouvement, securite, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.etat import LIBELLES_STATUT
from lymow_pilotage.journal import Journal
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.securite import Attestation, RefusSecurite
from lymow_pilotage.session import EchecOrdre

EN_TONTE = {"robotState": 2, "workStatus": 2, "battery": 80}  # statut VIVANT (f1) « tonte » : le cas que l'attestation couvre
RECULER = {"nom": "reculer", "duree": 0.3, "vitesse": 0.1}
POSER, RETIRER = ui.ROUTE_ATTESTATION_POSER, ui.ROUTE_ATTESTATION_RETIRER
CONFIRMER = {"confirmer": True}


def attester(session) -> Attestation:
    session.attestation = Attestation(session.horloge, session.journaliser)
    session.attestation.poser("test")
    return session.attestation


def evenements(dossier) -> list[str]:
    return [ligne["evenement"] for ligne in lignes_journal(dossier)]


def test_les_tests_portent_sur_le_code_de_ce_depot():
    assert Path(lymow_pilotage.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]


# --- règle pure -------------------------------------------------------------------


def test_sec01b_listes_disjointes_et_tout_statut_connu_est_classe():
    assert securite.STATUTS_ATTESTABLES == {2, 8, 9, 4, 10, 14}
    assert securite.STATUTS_TOUJOURS_REFUSES == {13, 11, 15, 16, -1}
    listes = (securite.STATUTS_LAMES_ARRET, securite.STATUTS_ATTESTABLES, securite.STATUTS_TOUJOURS_REFUSES)
    assert sum(len(liste) for liste in listes) == len(frozenset().union(*listes))  # disjointes deux à deux
    assert frozenset().union(*listes) == set(LIBELLES_STATUT)
    assert securite.DUREE_ATTESTATION_S == 600.0


def test_sec01b_statut_2_refuse_sans_attestation_admis_avec():
    with pytest.raises(RefusSecurite, match=r"le robot tond : .*\(SEC-01\)$"):
        securite.verifier_lames_arret(2, 1.0, True)
    securite.verifier_lames_arret(2, 1.0, True, attestee=True)
    securite.verifier_presence_et_statut(2, True, attestee=True)  # même règle pendant le mouvement


@pytest.mark.parametrize("statut", [13, 11, 15, 16, -1, None, 99])
def test_sec01b_statuts_toujours_refuses_meme_avec_attestation(statut):
    with pytest.raises(RefusSecurite, match="SEC-01"):
        securite.verifier_lames_arret(statut, 1.0, True, attestee=True)
    if statut is not None:
        with pytest.raises(RefusSecurite, match="SEC-01b"):
            securite.verifier_presence_et_statut(statut, True, attestee=True)


@pytest.mark.parametrize(("age", "en_ligne", "motif"), [(31.0, True, "périmé"), (1.0, False, "hors ligne"), (1.0, None, "non confirmée"), (None, True, "aucun état")])
def test_sec01b_l_attestation_ne_dispense_ni_de_l_etat_frais_ni_de_la_presence(age, en_ligne, motif):
    with pytest.raises(RefusSecurite, match=motif):
        securite.verifier_lames_arret(2, age, en_ligne, attestee=True)


def test_sec01b_attestation_inactive_par_defaut_expire_a_dix_minutes_et_trace_tout(horloge):
    traces = []
    attestation = Attestation(horloge, lambda evenement, **champs: traces.append((evenement, champs)))
    assert attestation.active is False and attestation.reste_s == 0 and attestation.retirer("rien") is False
    assert traces == []
    attestation.poser("test")
    assert attestation.active is True and attestation.reste_s == 600
    horloge.t += 599.5
    assert attestation.active is True and attestation.reste_s == 1
    horloge.t += 0.5
    assert attestation.active is False and attestation.reste_s == 0
    attestation.poser("test")
    assert attestation.retirer("à la demande") is True and attestation.active is False
    assert [evenement for evenement, _champs in traces] == [
        "attestation-posee", "attestation-expiree", "attestation-posee", "attestation-retiree",
    ]
    assert traces[0][1]["duree_s"] == 600.0 and traces[3][1] == {"motif": "à la demande"}


@pytest.mark.parametrize(("statut", "en_ligne", "reste"), [(2, True, True), (3, True, True), (13, True, False), (11, True, False), (15, True, False), (99, True, False), (2, False, False)])
def test_sec01b_attestation_tombe_des_qu_un_statut_toujours_refuse_arrive(horloge, statut, en_ligne, reste):
    attestation = Attestation(horloge)
    attestation.poser("test")
    attestation.controler(statut, en_ligne)
    assert attestation.active is reste


def test_sec01b_rien_ne_pose_l_attestation_hors_de_l_operateur(tmp_path, monkeypatch):
    for nom in ("LYMOW_LAMES_ARRETEES_CONSTATEES", "LAMES_ARRETEES_CONSTATEES", "LYMOW_ATTESTATION"):
        monkeypatch.setenv(nom, "1")
    assert not [champ.name for champ in dataclasses.fields(Calibration) if "lame" in champ.name or "attest" in champ.name]
    serveur = ui.ServeurUI(Calibration(), Journal(tmp_path), reel=True, port=0)
    assert serveur.attestation.active is False and serveur.instantane()["attestation"]["active"] is False
    assert not list(tmp_path.iterdir())  # rien sur disque : ni état, ni journal


# --- mouvement, de bout en bout sur le faux robot ------------------------------------


def test_sec01b_statut_2_sans_attestation_refus_inchange(session, lien, horloge):
    lien.robot = fragment(statut=2)
    session.attestation = Attestation(horloge, session.journaliser)  # présente, jamais posée
    with pytest.raises(RefusSecurite, match=r"le robot tond : .*\(SEC-01\)$"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == []


def test_sec01b_statut_2_avec_attestation_mouvement_emis_arret_garanti_et_mention_au_journal(session, lien, tmp_path):
    lien.robot = fragment(statut=2)
    lien.sur_publication = robot_ensuite(fragment(statut=2, est=1.0))
    attester(session)
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [CONSIGNE_AVANT] * 40 + [ARRET] * 3  # SEC-02 et SEC-03 inchangés
    assert rapport["attestation"] == securite.MENTION_ATTESTATION and rapport["statut_declare"] == 2
    journal = lignes_journal(tmp_path)
    emis = next(ligne for ligne in journal if ligne["evenement"] == "ordre-emis")
    assert emis["attestation"] == securite.MENTION_ATTESTATION and emis["statut_declare"] == 2
    assert next(ligne for ligne in journal if ligne["evenement"] == "attestation-posee")["duree_s"] == 600.0
    assert session.attestation.active is True  # un statut 2 ne la retire pas


def test_sec01b_sans_attestation_le_journal_ne_porte_aucune_mention(session, lien, tmp_path):
    lancer(mouvement.executer_mouvement(session, AVANCER_1))
    emis = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "ordre-emis")
    assert "attestation" not in emis and "statut_declare" not in emis


@pytest.mark.parametrize("statut", [13, 11, 15, 16, 99])
def test_sec01b_statut_toujours_refuse_avec_attestation_aucune_consigne_et_attestation_retiree(session, lien, statut, tmp_path):
    lien.robot = fragment(statut=statut)
    attester(session)
    with pytest.raises(RefusSecurite, match="SEC-01"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [] and session.attestation.active is False
    retrait = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "attestation-retiree")
    assert str(statut) in retrait["motif"]


def test_sec01b_hors_ligne_avec_attestation_refuse(session, lien, horloge):
    lien.robot = None

    def etat_puis_hors_ligne(_lien, octets):
        if octets == DEMANDE_CONFIGURATION:
            horloge.t += 0.01
            session.recevoir_etat(fragment(statut=2))
            session.recevoir_presence(False)

    lien.sur_publication = etat_puis_hors_ligne
    attester(session)
    with pytest.raises(RefusSecurite, match="hors ligne"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [] and session.attestation.active is False


def test_sec01b_statut_absent_avec_attestation_refuse(session, lien):
    lien.robot = None
    attester(session)
    with pytest.raises(RefusSecurite, match="aucun état"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == []


def test_sec01b_etat_perime_avec_attestation_refuse(session, lien, horloge):
    lien.robot = fragment(statut=2)
    attester(session)
    attendre_etat = session.attendre

    async def puis_le_temps_passe(*arguments):
        recu = await attendre_etat(*arguments)
        horloge.t += securite.AGE_MAX_ETAT_S + 1
        return recu

    session.attendre = puis_le_temps_passe
    with pytest.raises(RefusSecurite, match="périmé"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == []


def test_sec01b_attestation_expiree_apres_dix_minutes_refus_de_nouveau(session, lien, horloge, tmp_path):
    lien.robot = fragment(statut=2)
    attester(session)
    horloge.t += securite.DUREE_ATTESTATION_S
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [] and "attestation-expiree" in evenements(tmp_path)


def test_sec01b_statut_2_recu_pendant_le_mouvement_sous_attestation_n_interrompt_pas(session, lien):
    lien.robot = fragment(statut=2)
    lien.sur_publication = a_la_consigne(5, lambda _lien: session.recevoir_etat(fragment(statut=2)))
    attester(session)
    lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 40 and lien.arrets == [ARRET] * 3


@pytest.mark.parametrize("statut", [13, 11])
def test_sec01b_passage_en_statut_toujours_refuse_pendant_le_mouvement_arret_immediat(session, lien, statut):
    lien.robot = fragment(statut=2)
    lien.sur_publication = a_la_consigne(5, lambda _lien: session.recevoir_etat(fragment(statut=statut)))
    attester(session)
    with pytest.raises(EchecOrdre, match="interrompu en cours de route"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [CONSIGNE_AVANT] * 5 + [ARRET] * 3
    assert session.attestation.active is False


def test_sec01b_passage_hors_ligne_pendant_le_mouvement_sous_attestation_arret_immediat(session, lien):
    lien.robot = fragment(statut=2)
    lien.sur_publication = a_la_consigne(5, lambda _lien: session.recevoir_presence(False))
    attester(session)
    with pytest.raises(EchecOrdre, match="hors ligne"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [CONSIGNE_AVANT] * 5 + [ARRET] * 3


def test_sec01b_attestation_retiree_pendant_le_mouvement_arret_immediat(session, lien):
    lien.robot = fragment(statut=2)
    lien.sur_publication = a_la_consigne(5, lambda _lien: session.attestation.retirer("à la demande"))
    attester(session)
    with pytest.raises(EchecOrdre, match=r"interrompu en cours de route.*SEC-01"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [CONSIGNE_AVANT] * 5 + [ARRET] * 3


def test_sec01b_plateau_et_essai_d_arret_restent_sur_la_liste_blanche(session, lien):
    lien.robot = fragment(statut=2)
    attester(session)
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)"):
        lancer(mouvement.executer_plateau(session))
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)"):
        lancer(mouvement.executer_essai(session, Ordre(mouvement.ESSAI)))
    assert lien.ordres == []


def test_sec01b_stop_retire_l_attestation(session, lien, tmp_path):
    lien.robot = fragment(statut=2)
    attester(session)
    lancer(mouvement.executer_arret(session))
    assert session.attestation.active is False and lien.trames[:3] == [ARRET] * 3  # les arrêts d'abord
    retrait = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "attestation-retiree")
    assert retrait["motif"] == "STOP"
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))


def test_sec01b_sequence_sous_attestation_non_interrompue_par_le_statut_2(session, lien):
    lien.robot = dict(EN_TONTE)
    attester(session)
    bilan = lancer(mouvement.executer_sequence(session, [Ordre("avancer", 0.5), Ordre("tourner", 45.0)]))
    assert bilan["interrompue"] is False and len(bilan["etapes"]) == 2
    assert all(etape["statut_declare"] == 2 for etape in bilan["etapes"]) and len(lien.arrets) == 6


# --- interface web --------------------------------------------------------------------


def test_route_attestation_refusee_en_simulation(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(EN_TONTE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            statut, corps, _recus = await poster(http, serveur, POSER, CONFIRMER)
            assert statut == 403 and "simulation" in corps["erreur"]
            image = serveur.instantane()["attestation"]
            assert image["active"] is False and image["proposable"] is False

    executer(scenario())
    assert "attestation-posee" not in evenements(tmp_path)


def test_route_attestation_protegee_comme_les_autres_routes_modifiantes(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(EN_TONTE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["attestation"]["proposable"])
            for chemin in (POSER, RETIRER):
                for temoin in (None, "lymow_ui=faux", f"lymow_ui={serveur.jeton}"):
                    statut, _corps, _recus = await poster(http, serveur, chemin, CONFIRMER, Cookie=temoin)
                    assert statut == 403, chemin
                for origine in (None, "http://evil.example", "null", f"https://127.0.0.1:{serveur.port}"):
                    statut, _corps, _recus = await poster(http, serveur, chemin, CONFIRMER, Origin=origine)
                    assert statut == 403, chemin
                statut, _corps, _recus = await poster(http, serveur, chemin, CONFIRMER, Host="evil.example")
                assert statut == 403, chemin
                statut, _texte, _recus = await lire(http, serveur, chemin)
                assert statut == 405, chemin
            for corps in ({}, {"confirmer": "oui"}, {"confirmer": 1}, [True]):
                statut, _corps, _recus = await poster(http, serveur, POSER, corps)
                assert statut == 400
            assert serveur.attestation.active is False

    executer(scenario())
    assert "attestation-posee" not in evenements(tmp_path)


def test_route_attestation_refusee_pendant_un_mouvement(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", PAGE_AVANCER_1)
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            await attendre(lambda: len(lien.consignes) >= 3)
            lien.robot = dict(EN_TONTE)
            statut, corps, _recus = await poster(http, serveur, POSER, CONFIRMER)
            assert statut == 409 and "en cours" in corps["erreur"] and serveur.attestation.active is False
            await poster(http, serveur, "/api/stop")

    executer(scenario())


@pytest.mark.parametrize("statut", [1, 3, 13, 11, 15, 99])
def test_route_attestation_refusee_hors_statut_attestable(tmp_path, monkeypatch, session_reelle, lien, statut):
    lien.robot = {"robotState": statut, "workStatus": statut, "battery": 80}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["etat"]["statut"] == statut)
            assert serveur.instantane()["attestation"]["proposable"] is False
            statut_http, corps, _recus = await poster(http, serveur, POSER, CONFIRMER)
            assert statut_http == 409 and "SEC-01b" in corps["erreur"] and serveur.attestation.active is False

    executer(scenario())


def test_page_statut_2_refuse_puis_attestation_ordre_emis_et_stop_la_retire(tmp_path, monkeypatch, session_reelle, lien):
    """Le scénario du 2026-10-12 : en pause, statut 2 publié, pilotage refusé — puis débloqué sur attestation."""
    lien.robot = dict(EN_TONTE)

    async def emettre(http, serveur):
        _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [RECULER]})
        statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
        assert statut == 202
        await attendre(lambda: not serveur.occupe, delai=4.0)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["attestation"]["proposable"])
            image = serveur.instantane()
            assert image["attestation"] == {
                "active": False, "reste_s": 0, "duree_s": 600.0, "proposable": True, "statut": "tonte en cours",
            }
            assert image["ordres"]["mouvement_refuse"] == "Le robot tond : mettez la tâche en pause pour prendre la main."
            assert image["ordres"]["action"] == "pause"  # la pause reste le geste proposé en premier
            await emettre(http, serveur)
            assert "SEC-01" in serveur.resultat["motif"] and lien.ordres == []  # sans attestation : refus inchangé

            statut, corps, _recus = await poster(http, serveur, POSER, CONFIRMER)
            assert statut == 200 and corps["attestation"]["active"] is True
            image = serveur.instantane()
            assert image["attestation"]["proposable"] is False and 598 <= image["attestation"]["reste_s"] <= 600
            assert (image["ordres"]["statut"], image["ordres"]["mouvement_refuse"], image["ordres"]["action"]) == (
                "tonte en cours", None, None,
            )
            statut, _corps, _recus = await poster(http, serveur, POSER, CONFIRMER)
            assert statut == 409  # déjà active : pas de prolongation silencieuse

            await emettre(http, serveur)  # ordre classique : même règle, sans chemin particulier
            etape = serveur.resultat["etapes"][0]
            assert serveur.resultat["code_sortie"] == 0 and etape["attestation"] == securite.MENTION_ATTESTATION
            assert lien.consignes and lien.arrets == [ARRET] * 3

            jeton = await armer(http, serveur)  # pilotage direct par flèches : même règle
            statut, _corps, _recus = await fleche(http, serveur, jeton, "haut", 0.25)
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=6.0)
            assert serveur.resultat["code_sortie"] == 0 and serveur.resultat["etapes"][0]["statut_declare"] == 2

            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202 and serveur.instantane()["attestation"]["active"] is False
            await attendre(lambda: not serveur.occupe, delai=6.0)
            ordres = len(lien.consignes)
            await emettre(http, serveur)
            assert "SEC-01" in serveur.resultat["motif"] and len(lien.consignes) == ordres  # de nouveau refusé
            return serveur.temoin

    temoin = executer(scenario())
    journal = lignes_journal(tmp_path)
    posee = next(ligne for ligne in journal if ligne["evenement"] == "attestation-posee")
    assert posee["origine"] == "page" and posee["mode"] == "réel" and posee["duree_s"] == 600.0
    assert [ligne["motif"] for ligne in journal if ligne["evenement"] == "attestation-retiree"] == ["STOP"]
    emis = [ligne for ligne in journal if ligne["evenement"] == "ordre-emis" and "attestation" in ligne]
    assert [ligne["ordre"] for ligne in emis] == ["reculer --duree 0.3 --vitesse 0.1", "avancer 0.25"]
    assert all(ligne["statut_declare"] == 2 for ligne in emis)
    assert temoin not in str(journal)  # évènements sans secret


def test_page_attestation_expire_apres_dix_minutes(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(EN_TONTE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["attestation"]["proposable"])
            await poster(http, serveur, POSER, CONFIRMER)
            pose = serveur.horloge()
            serveur.horloge = lambda: pose + securite.DUREE_ATTESTATION_S - 1
            assert serveur.instantane()["attestation"]["active"] is True
            serveur.horloge = lambda: pose + securite.DUREE_ATTESTATION_S + 1
            image = serveur.instantane()
            assert image["attestation"]["active"] is False and image["ordres"]["mouvement_refuse"] is not None
            assert any("expirée" in ligne["texte"] for ligne in serveur.fil)
            serveur.horloge = time.monotonic

    executer(scenario())
    assert "attestation-expiree" in evenements(tmp_path)


def test_page_attestation_retiree_a_la_demande_par_un_statut_toujours_refuse_et_a_l_arret_du_serveur(
    tmp_path, monkeypatch, session_reelle, lien
):
    lien.robot = dict(EN_TONTE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["attestation"]["proposable"])
            await poster(http, serveur, POSER, CONFIRMER)
            statut, corps, _recus = await poster(http, serveur, RETIRER)
            assert statut == 200 and corps["attestation"]["active"] is False

            await poster(http, serveur, POSER, CONFIRMER)
            session_reelle.recevoir_etat({"robotState": 13, "workStatus": 13, "battery": 80})  # arrêt d'urgence, hors de tout ordre
            image = serveur.instantane()["attestation"]
            assert image["active"] is False and image["proposable"] is False

            session_reelle.recevoir_etat(dict(EN_TONTE))
            statut, _corps, _recus = await poster(http, serveur, POSER, CONFIRMER)
            assert statut == 200 and serveur.attestation.active is True
        assert serveur.attestation.active is False  # serveur arrêté

    executer(scenario())
    motifs = [ligne["motif"] for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "attestation-retiree"]
    assert motifs == ["à la demande de l'opérateur", "arrêt d'urgence enclenché (statut 13) : toujours refusé", "arrêt du serveur"]


def test_attestation_acceptee_en_demonstration(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            serveur.session.lien.statut = 2  # le robot fictif se déclare en tonte
            await serveur.session.solliciter()
            await attendre(lambda: serveur.instantane()["attestation"]["proposable"])
            statut, corps, _recus = await poster(http, serveur, POSER, CONFIRMER)
            assert statut == 200 and corps["attestation"]["active"] is True
            assert serveur.instantane()["ordres"]["mouvement_refuse"] is None

    executer(scenario())
    posee = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "attestation-posee")
    assert posee["mode"] == "démonstration"


# --- page ---------------------------------------------------------------------------


def test_page_porte_le_message_le_bouton_la_confirmation_et_le_bandeau_d_attestation():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    script = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    logique = (ui.STATIQUE / "ordres.js").read_text(encoding="utf-8")
    bouton = "Je vois le robot, lames à l'arrêt : autoriser le pilotage 10 min"
    # Interface simplifiée : UNE proposition, dans le bloc Pilotage, sur la ligne de statut sous la barre
    # de commande — après le raccourci « Mettre en pause » (la pause d'abord, l'attestation ensuite).
    assert page.count(bouton) == 1
    assert page.index('id="bloc-fleches"') < page.index('id="ordres-etat"') < page.index('id="pause-raccourci"')
    assert page.index('id="pause-raccourci"') < page.index(bouton) < page.index('class="ligne-sequence"')
    haut = page[page.index('<div class="haut">') : page.index('<main>')]
    assert 'id="bandeau-attestation"' in haut and 'id="stop"' in haut  # avec les autres bandeaux : STOP jamais masqué
    assert "position: fixed" not in (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
    assert 'id="bandeau-attestation"' in page and ">Retirer</button>" in page
    assert "Vous attestez que les lames sont à l'arrêt. L'outil ne peut pas le vérifier." in script
    assert 'deuxClics("attestation"' in script and "window.confirm(" not in script
    assert ui.ROUTE_ATTESTATION_POSER in script and ui.ROUTE_ATTESTATION_RETIRER in script
    assert "Le robot se déclare : " in logique
    assert "Pilotage autorisé sur attestation de l'opérateur — expire dans " in logique
    assert "bandeau-attestation" in (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
    assert securite.DUREE_ATTESTATION_S == 10 * 60  # le « 10 min » de la page


@sans_node
def test_page_message_du_statut_declare_et_bandeau_en_minutes_secondes():
    proposable = {"active": False, "reste_s": 0, "proposable": True, "statut": "tonte en cours"}
    assert js("O.messageStatutDeclare(a[0])", proposable) == "Le robot se déclare : tonte en cours."
    assert js("O.messageStatutDeclare(a[0])", proposable | {"proposable": False, "statut": None}) == ""
    assert js("O.messageStatutDeclare(a[0])", None) == "" and js("O.bandeauAttestation(a[0])", proposable) == ""
    active = {"active": True, "reste_s": 600, "proposable": False, "statut": None}
    debut = "Pilotage autorisé sur attestation de l'opérateur — expire dans "
    assert js("O.bandeauAttestation(a[0])", active) == debut + "10:00"
    assert js("O.bandeauAttestation(a[0])", active | {"reste_s": 599}) == debut + "09:59"
    assert js("O.bandeauAttestation(a[0])", active | {"reste_s": 5}) == debut + "00:05"
    assert js("O.messageStatutDeclare(a[0])", active) == ""
