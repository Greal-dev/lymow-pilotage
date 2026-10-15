"""Position de nettoyage posée par l'outil, sortie par « redescendre », essai de pas plateau levé (SEC-01c).

Constat réel du 2026-10-13 : la levée de nettoyage (signal 8) a levé le plateau et mis le robot en statut 13
« arrêt d'urgence » ; « redescendre » (signal 9) a alors été refusé par SEC-01 — l'outil ne savait plus sortir
le robot de l'état où il l'avait mis. Tout est rejoué ici hors ligne, sur un faux robot.
"""

from __future__ import annotations

import json

import pytest
from conftest import APPAREIL, ARRET, fragment, lignes_journal
from test_attestation import attester
from test_demo import serveur_demo
from test_fleches import armer, fleche
from test_mouvement import lancer
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import mouvement, nettoyage, protocole, securite, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.nettoyage import PositionDeNettoyage
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.session import EchecOrdre

LEVER, DESCENDRE = bytes.fromhex("10 31 6a 02 40 08"), bytes.fromhex("10 31 6a 02 40 09")
CONFIRMER = {"confirmer": True}


def robot_a_plateau(sortie: int | None = 1):
    """Le faux robot se comporte comme le vrai : signal 8 → statut 13 ; signal 9 → `sortie` (None : il reste en 13)."""

    def sur_publication(lien, octets):
        if octets == LEVER:
            lien.robot = fragment(statut=13)
        elif octets == DESCENDRE and sortie is not None:
            lien.robot = fragment(statut=sortie)

    return sur_publication


def munir(session, tmp_path) -> PositionDeNettoyage:
    session.nettoyage = PositionDeNettoyage(tmp_path, session.horloge, session.journaliser)
    return session.nettoyage


def lever(session, lien, tmp_path, sortie: int | None = 1) -> PositionDeNettoyage:
    position = munir(session, tmp_path)
    lien.sur_publication = robot_a_plateau(sortie)
    lancer(mouvement.executer_signal_plateau(session, "nettoyage"))
    assert session.etat.statut == 13 and position.posee is not None
    return position


# --- 1. le bogue bloquant ----------------------------------------------------------


def test_redescendre_le_plateau_n_est_plus_refuse_en_arret_d_urgence_apres_la_levee_de_nettoyage(session, lien, tmp_path):
    lever(session, lien, tmp_path)
    rapport = lancer(mouvement.executer_signal_plateau(session, "redescendre"))
    assert lien.ordres == [LEVER, DESCENDRE]
    assert rapport["observation"].startswith("le robot est sorti de l'arrêt d'urgence : prêt")
    emis = [ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "ordre-emis"]
    assert [ligne["ordre"] for ligne in emis] == [protocole.LIBELLES_SIGNAUX_PLATEAU[n] for n in ("nettoyage", "redescendre")]


def test_redescendre_dit_quand_le_robot_reste_en_arret_d_urgence(session, lien, tmp_path):
    lever(session, lien, tmp_path, sortie=None)
    rapport = lancer(mouvement.executer_signal_plateau(session, "redescendre"))
    assert lien.ordres == [LEVER, DESCENDRE] and rapport["observation"].startswith("toujours en arrêt d'urgence")


def test_redescendre_admis_en_arret_d_urgence_d_origine_inconnue_la_levee_et_la_tonte_restent_refusees(session, lien):
    lien.robot = fragment(statut=13)
    with pytest.raises(RefusSecurite, match=r"arrêt d'urgence enclenché : levez-le sur le robot ou depuis l'application Lymow"):
        lancer(mouvement.executer_signal_plateau(session, "nettoyage"))
    assert lien.ordres == []
    lancer(mouvement.executer_signal_plateau(session, "redescendre"))
    assert lien.ordres == [DESCENDRE]
    lien.robot = fragment(statut=2)  # en tonte : SEC-01 strict, comme avant
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)$"):
        lancer(mouvement.executer_signal_plateau(session, "redescendre"))
    assert lien.ordres == [DESCENDRE]


# --- 2. « posée par l'outil » : mémorisation, persistance, effacement ----------------


def test_position_de_nettoyage_memorisee_avec_l_heure_et_l_appareil_puis_relue_au_redemarrage(session, lien, tmp_path, horloge):
    position = lever(session, lien, tmp_path)
    ecrit = json.loads((tmp_path / nettoyage.FICHIER).read_text(encoding="utf-8"))
    assert ecrit == position.posee and ecrit["appareil"] == APPAREIL and ecrit["horodatage"][:2] == "20"
    assert "nettoyage-pose" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]
    relue = PositionDeNettoyage(tmp_path, horloge)  # redémarrage de l'interface
    assert relue.posee == ecrit
    relue.observer(13, APPAREIL)
    assert relue.posee == ecrit  # toujours en arrêt d'urgence : toujours posée
    relue.observer(1, APPAREIL)
    assert relue.posee is None and not (tmp_path / nettoyage.FICHIER).exists()  # effacée dès que le statut quitte 13
    assert PositionDeNettoyage(tmp_path, horloge).posee is None


def test_un_arret_d_urgence_n_est_pose_par_l_outil_que_dans_les_dix_secondes_apres_une_levee_partie_d_un_autre_statut(tmp_path, horloge):
    position = PositionDeNettoyage(tmp_path, horloge)
    position.observer(13, APPAREIL)  # aucun ordre de l'outil : origine inconnue
    assert position.posee is None
    position.noter_emission(13)  # levée émise alors que le robot était déjà en arrêt d'urgence
    position.observer(13, APPAREIL)
    assert position.posee is None
    position.noter_emission(1)
    horloge.t += securite.DELAI_CONSTAT_NETTOYAGE_S + 0.5  # trop tard : rien ne dit que c'est l'outil
    position.observer(13, APPAREIL)
    assert position.posee is None
    position.noter_emission(1)
    position.observer(1, APPAREIL)  # un état « prêt » arrive encore avant le 13
    horloge.t += securite.DELAI_CONSTAT_NETTOYAGE_S - 0.5
    position.observer(13, APPAREIL)
    assert position.posee is not None and position.posee["appareil"] == APPAREIL
    position.observer(13, "autre-robot")  # mémorisée pour un autre appareil : origine inconnue pour celui-ci
    assert position.posee is None


# --- 3. essai de pas plateau levé (SEC-01c) -----------------------------------------


def test_sec01c_arret_d_urgence_d_origine_inconnue_toujours_refuse_attestation_ou_non(session, lien, tmp_path):
    lien.robot = fragment(statut=13)
    position = munir(session, tmp_path)
    attester(session)
    with pytest.raises(RefusSecurite, match=r"n'a pas été posé par l'outil \(SEC-01c\)"):
        position.autoriser_essai()
    assert position.essai_actif is False
    with pytest.raises(RefusSecurite, match="SEC-01"):
        lancer(mouvement.executer_mouvement(session, Ordre("avancer", 0.25)))
    assert lien.ordres == []


def test_sec01c_pose_par_l_outil_mais_sans_autorisation_le_pas_reste_refuse(session, lien, tmp_path):
    lever(session, lien, tmp_path)
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)$"):
        lancer(mouvement.executer_mouvement(session, Ordre("avancer", 0.25)))
    assert lien.ordres == [LEVER]


def test_sec01c_pas_autorise_emis_a_allure_lente_arret_garanti_journalise_et_constat_ignore(session, lien, tmp_path):
    position = lever(session, lien, tmp_path)
    position.autoriser_essai()
    try:
        constat = lancer(mouvement.executer_mouvement(session, Ordre("avancer", 0.5, vitesse=0.25)))["essai_plateau_leve"]
    except EchecOrdre as echec:  # SEC-14 a pu arrêter le pas avant sa fin : le constat dit la même chose
        constat = str(echec)
    assert "IGNORÉ" in constat
    lente = protocole.trame_consigne(securite.VITESSE_PLATEAU_LEVE_M_S, 0.0, Calibration()).octets
    assert lien.consignes and set(lien.consignes) == {lente} and len(lien.consignes) <= 50
    assert lien.ordres[-3:] == [ARRET] * 3  # SEC-03
    journal = lignes_journal(tmp_path)
    essai = next(ligne for ligne in journal if ligne["evenement"] == "essai-plateau-leve")
    assert essai["regle"] == "SEC-01c" and "décision du propriétaire, 2026-10-13" in essai["mention"]
    assert "essai-plateau-leve-autorise" in [ligne["evenement"] for ligne in journal]


def test_sec01c_le_constat_dit_quand_le_robot_a_bouge(session, lien, tmp_path):
    position = lever(session, lien, tmp_path)
    position.autoriser_essai()
    interne = lien.sur_publication

    def avance(lien_, octets):
        interne(lien_, octets)
        if octets == ARRET:
            lien_.robot = fragment(statut=13, est=0.5)

    lien.sur_publication = avance
    rapport = lancer(mouvement.executer_mouvement(session, Ordre("avancer", 0.5)))
    assert "BOUGÉ" in rapport["essai_plateau_leve"] and rapport["plateau_leve"] == securite.MENTION_PLATEAU_LEVE


@pytest.mark.parametrize(
    "ordre",
    [Ordre("avancer", 1.0), Ordre("reculer", 0.51), Ordre("tourner", 90.0), Ordre("tourner", -46.0),
     Ordre("arc", 0.5, angle=90.0), Ordre("arc", 1.0, angle=15.0), Ordre("avancer", None, duree=2.0)],
)
def test_sec01c_plafonds_du_pas_plateau_leve(session, lien, tmp_path, ordre):
    position = lever(session, lien, tmp_path)
    position.autoriser_essai()
    with pytest.raises(RefusSecurite, match=r"\(SEC-01c\)$"):
        lancer(mouvement.executer_mouvement(session, ordre))
    assert lien.ordres == [LEVER]
    for nom, valeur, angle in (("avancer", 0.5, None), ("tourner", -45.0, None), ("arc", -0.5, 45.0)):
        securite.verifier_pas_plateau_leve(nom, valeur, angle)


def test_sec01c_l_essai_expire_a_deux_minutes_se_retire_et_tombe_quand_le_statut_quitte_13(session, lien, tmp_path, horloge):
    position = lever(session, lien, tmp_path)
    position.autoriser_essai()
    assert position.essai_actif and position.essai_reste_s == 120
    horloge.t += securite.DUREE_ESSAI_PLATEAU_LEVE_S
    assert position.essai_actif is False
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)$"):
        lancer(mouvement.executer_mouvement(session, Ordre("avancer", 0.25)))
    assert lien.ordres == [LEVER]
    position.autoriser_essai()
    assert position.retirer_essai("STOP") is True and position.essai_actif is False
    position.autoriser_essai()
    position.observer(1, APPAREIL)  # le robot a quitté l'arrêt d'urgence
    assert position.essai_actif is False and position.posee is None
    evenements = [ligne["evenement"] for ligne in lignes_journal(tmp_path)]
    for attendu in ("essai-plateau-leve-expire", "essai-plateau-leve-retire", "nettoyage-efface"):
        assert attendu in evenements


# --- la page et ses routes ------------------------------------------------------------


def test_route_pas_plateau_leve_refusee_pour_un_arret_d_urgence_d_origine_inconnue(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=13)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.session is not None and serveur.session.etat.statut == 13)
            assert serveur.instantane()["nettoyage"]["posee"] is False
            assert "ou depuis l'application Lymow" in serveur.instantane()["ordres"]["mouvement_refuse"]
            for corps in ({}, {"confirmer": "oui"}, {"confirmer": True, "duree": 600}):
                assert (await poster(http, serveur, "/api/plateau/pas-leve", corps))[0] == 400
            statut, reponse, _recus = await poster(http, serveur, "/api/plateau/pas-leve", CONFIRMER)
            assert statut == 409 and "SEC-01c" in reponse["erreur"] and serveur.nettoyage.essai_actif is False
            assert lien.ordres == []
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            assert (await poster(http, serveur, "/api/plateau/pas-leve", CONFIRMER))[0] == 403

    executer(scenario())


def test_demonstration_levee_puis_arret_d_urgence_pas_essaye_un_a_la_fois_puis_redescente(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            robot = serveur.session.lien
            await poster(http, serveur, "/api/plateau/essai", {"ordre": "nettoyage", "confirmer": True})
            await attendre(lambda: not serveur.occupe and serveur.nettoyage.posee is not None, delai=30.0)
            image = serveur.instantane()
            assert robot.statut == 13 and image["nettoyage"]["posee"] is True
            assert image["nettoyage"]["texte"] == "Plateau en position de nettoyage (le robot se déclare en arrêt d'urgence)"
            assert image["ordres"]["mouvement_refuse"].endswith("redescendez le plateau.")
            assert not list(tmp_path.rglob(nettoyage.FICHIER))  # le jardin fictif n'écrit rien sur disque

            statut, reponse, _recus = await poster(http, serveur, "/api/plateau/pas-leve", CONFIRMER)
            assert statut == 200 and reponse["nettoyage"]["essai"] is True and reponse["nettoyage"]["essai_reste_s"] > 110
            assert serveur.instantane()["ordres"]["mouvement_refuse"] is None
            jeton = await armer(http, serveur)
            for direction, pas in (("haut", 1.0), ("gauche", 90.0)):  # plafonds : dits, rien n'est émis, la main est gardée
                statut, reponse, _recus = await fleche(http, serveur, jeton, direction, pas)
                assert statut == 409 and "SEC-01c" in reponse["erreur"] and serveur.direct.arme
            assert "un seul pas à la fois" in serveur._refus_plateau_leve([Ordre("avancer", 0.25), Ordre("avancer", 0.25)])
            avant = (robot.est, robot.nord)
            statut, reponse, _recus = await fleche(http, serveur, jeton, "haut", 0.25)
            assert statut == 202
            await attendre(lambda: not serveur.occupe and serveur.direct.faites, delai=40.0)
            assert "IGNORÉ" in json.dumps(serveur.direct.faites[-1]["rapport"], ensure_ascii=False)
            assert (robot.est, robot.nord) == avant and serveur.direct.arme

            await poster(http, serveur, "/api/plateau/essai", {"ordre": "redescendre", "confirmer": True})
            await attendre(lambda: not serveur.occupe and robot.statut == 1, delai=40.0)
            assert serveur.resultat["verdict"].startswith("le robot est sorti de l'arrêt d'urgence : prêt")
            image = serveur.instantane()
            assert image["nettoyage"] == image["nettoyage"] | {"posee": False, "essai": False, "texte": ""}
            assert serveur.nettoyage.essai_actif is False

    executer(scenario())


def test_stop_retire_l_essai_de_pas_plateau_leve(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            await poster(http, serveur, "/api/plateau/essai", {"ordre": "nettoyage", "confirmer": True})
            await attendre(lambda: not serveur.occupe and serveur.nettoyage.posee is not None, delai=30.0)
            await poster(http, serveur, "/api/plateau/pas-leve", CONFIRMER)
            assert serveur.nettoyage.essai_actif is True
            await poster(http, serveur, "/api/stop")
            await attendre(lambda: not serveur.occupe, delai=30.0)
            assert serveur.nettoyage.essai_actif is False and serveur.nettoyage.posee is not None

    executer(scenario())


def test_page_le_bloc_pilotage_porte_redescendre_et_l_essai_de_pas_avec_sa_confirmation():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    bloc = page[page.index('id="nettoyage"') :][:1400]
    assert page.index('id="nettoyage"') < page.index('data-volet="calibration"')  # dans Pilotage, pas seulement Calibration
    assert 'data-plateau-essai="redescendre"' in bloc and ">Redescendre le plateau</button>" in bloc
    assert ">Essayer un pas plateau levé</button>" in bloc
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    clic = app[app.index('$("nettoyage-essai").addEventListener("click"') :][:400]
    assert 'deuxClics("nettoyage"' in clic and "window.confirm(" not in clic and '"/api/plateau/pas-leve", { confirmer: true }' in clic
    assert nettoyage.CONFIRMATION_ESSAI == (
        "Le robot se déclare en arrêt d'urgence parce que le plateau est en position de nettoyage. "
        "Personne ne doit se trouver près du robot. Il ignorera peut-être l'ordre."
    )
