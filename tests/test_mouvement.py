"""SEC-01, SEC-03, SEC-04, SEC-07 de bout en bout sur un faux client MQTT."""

from __future__ import annotations

import asyncio
import math

import pytest
from conftest import ARRET, SUJET, fragment

from lymow_pilotage import mouvement
from lymow_pilotage.mouvement import ArretNonConfirme, ErreurSaisie, Ordre
from lymow_pilotage.protocole import Categorie, est_requete_pure
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.session import EchecOrdre

AVANCER_1 = Ordre("avancer", 1.0)
CONSIGNE_AVANT = bytes.fromhex("10 31 52 0a 0d 00 00 80 3e 15 00 00 00 00")


def robot_repond(session, horloge, **pose):
    """Le faux robot publie un nouvel état juste après le premier arrêt reçu."""
    deja = []

    def sur_publication(_lien, octets):
        if octets == ARRET and not deja:
            deja.append(True)
            horloge.t += 0.01
            session.recevoir_etat(fragment(**pose))

    return sur_publication


def casse_a_la(n, action):
    compte = []

    def sur_publication(lien, octets):
        compte.append(octets)
        if len(compte) == n:
            action(lien)

    return sur_publication


def _echec(_lien):
    raise ConnectionError("wss://hote/mqtt?X-Amz-Signature=abcdef")


# --- nominal ----------------------------------------------------------------


def test_mouvement_nominal_cadence_arret_et_constat(session, lien, horloge):
    lien.sur_publication = robot_repond(session, horloge, est=1.0)
    rapport = asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))

    assert all(sujet == SUJET for sujet, _octets in lien.publies)
    assert len(lien.consignes) == 40  # 4 s à 10 Hz
    assert set(lien.consignes) == {CONSIGNE_AVANT}
    assert lien.trames[40:43] == [ARRET] * 3  # SEC-03 : l'arrêt suit immédiatement
    assert rapport["emissions"] == 40
    assert rapport["etat_rafraichi"] is True
    assert rapport["avant"] == {"est_m": 0.0, "nord_m": 0.0, "cap_deg": 0.0}
    assert rapport["apres"]["est_m"] == 1.0
    assert rapport["deplacement_m"] == 1.0
    assert rapport["constat"] == "déplacement constaté"


def test_rotation_gauche_constatee(session, lien, horloge):
    lien.sur_publication = robot_repond(session, horloge, theta=math.radians(90))
    rapport = asyncio.run(mouvement.executer_mouvement(session, Ordre("tourner", 90.0)))
    assert set(lien.consignes) == {bytes.fromhex("10 31 52 0a 0d 00 00 00 00 15 33 33 b3 3e")}
    assert rapport["rotation_deg"] == 90.0
    assert rapport["constat"] == "déplacement constaté"


# --- SEC-01 -----------------------------------------------------------------


@pytest.mark.parametrize("statut", [2, 9, 3, 99])
def test_sec01_statut_de_tonte_ou_inconnu_aucune_consigne_emise(session, lien, statut):
    session.recevoir_etat(fragment(statut=statut))
    with pytest.raises(RefusSecurite, match="SEC-01"):
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.publies == []


def test_sec01_etat_perime_refuse_apres_sollicitation_par_requetes_pures(session, lien, horloge):
    horloge.t += 200.0
    with pytest.raises(RefusSecurite, match="périmé"):
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.consignes == []
    assert lien.trames and all(est_requete_pure(octets) for octets in lien.trames)


def test_sec01_etat_absent_refuse(session, lien):
    session.etat.donnees.clear()
    session.etat.recu_statut = session.etat.recu_pose = None
    with pytest.raises(RefusSecurite, match="aucun état"):
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.consignes == []


def test_sec01_plateau_haut_refuse_pendant_la_tonte(session, lien):
    session.recevoir_etat(fragment(statut=2))
    with pytest.raises(RefusSecurite):
        asyncio.run(mouvement.executer_plateau(session))
    assert lien.publies == []


# --- SEC-03 -----------------------------------------------------------------


def test_sec03_publication_qui_echoue_arret_tente_et_erreur(session, lien):
    lien.sur_publication = casse_a_la(5, _echec)
    with pytest.raises(EchecOrdre, match="non acquittée") as erreur:
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 4
    assert lien.arrets == [ARRET] * 3
    assert "X-Amz" not in str(erreur.value)


def test_sec03_deconnexion_en_cours_de_mouvement_arret_non_confirme(session, lien):
    def coupure(lien_):
        lien_.connecte = False

    lien.sur_publication = casse_a_la(5, coupure)
    with pytest.raises(ArretNonConfirme, match="ARRÊT NON CONFIRMÉ"):
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 5  # homme mort : plus rien après la coupure
    assert lien.arrets == []


def test_sec03_deconnexion_puis_reconnexion_pour_arreter(session, lien):
    def coupure(lien_):
        lien_.connecte = False

    async def reconnecter():
        lien.connecte = True

    lien.reconnecter = reconnecter
    lien.sur_publication = casse_a_la(5, coupure)
    with pytest.raises(EchecOrdre, match="non connecté"):
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 5
    assert lien.arrets == [ARRET] * 3


def test_sec03_refus_d_emettre_si_mqtt_non_connecte(session, lien):
    lien.connecte = False
    with pytest.raises(EchecOrdre):
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.publies == []


def test_sec03_un_arret_non_acquitte_sur_trois_est_une_erreur(session, lien):
    deja = []

    def sur_publication(_lien, octets):
        if octets == ARRET and not deja:
            deja.append(True)
            raise TimeoutError

    lien.sur_publication = sur_publication
    with pytest.raises(ArretNonConfirme, match="2/3"):
        asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))


def test_sec03_interruption_clavier_envoie_l_arret(session, lien):
    async def scenario():
        tache = asyncio.ensure_future(mouvement.executer_mouvement(session, AVANCER_1))
        lien.sur_publication = casse_a_la(5, lambda _lien: tache.cancel())  # effet de Ctrl+C
        with pytest.raises(asyncio.CancelledError):
            await tache

    asyncio.run(scenario())
    assert len(lien.consignes) == 5
    assert lien.arrets == [ARRET] * 3


def test_sec03_stop_reel_sans_aucune_precondition(session, lien, messages):
    session.etat.donnees.clear()
    session.etat.recu_statut = session.etat.recu_pose = None
    rapport = asyncio.run(mouvement.executer_arret(session))
    assert lien.trames[:3] == [ARRET] * 3
    assert rapport["arrets_acquittes"] == 3
    assert rapport["etat_rafraichi"] is False
    assert messages == ["Arrêt émis et acquitté 3/3."]


def test_session_refuse_une_categorie_non_autorisee(session, lien):
    session.autorisees = frozenset({Categorie.REQUETE_PURE})
    with pytest.raises(RefusSecurite):
        asyncio.run(mouvement.arreter(session))
    assert lien.publies == []


# --- SEC-04 -----------------------------------------------------------------


def test_sec04_robot_immobile_signale(session, lien, horloge):
    lien.sur_publication = robot_repond(session, horloge, est=0.01)
    rapport = asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert rapport["constat"] == "le robot n'a pas bougé (patinage ou ordre ignoré)"
    assert rapport["deplacement_m"] == 0.01


def test_sec04_etat_non_rafraichi_dit_plutot_qu_affirme(session, lien):
    rapport = asyncio.run(mouvement.executer_mouvement(session, AVANCER_1))
    assert rapport["etat_rafraichi"] is False
    assert rapport["apres"] is None
    assert "deplacement_m" not in rapport
    assert rapport["constat"] == "état non rafraîchi depuis l'ordre : déplacement non vérifié"


# --- tâches -----------------------------------------------------------------


def test_reprendre_quitte_d_abord_la_telecommande(session, lien):
    session.recevoir_etat(fragment(statut=6))
    rapport = asyncio.run(mouvement.executer_tache(session, "reprendre"))
    ordres = [o for o in lien.trames if not est_requete_pure(o)]
    assert ordres == [bytes.fromhex("10 31 28 20"), bytes.fromhex("10 31 28 04")]
    assert rapport["codes_emis"] == [32, 4]


def test_reprendre_refuse_s_il_n_y_a_rien_a_reprendre(session, lien):
    with pytest.raises(RefusSecurite, match="rien à reprendre"):
        asyncio.run(mouvement.executer_tache(session, "reprendre"))
    assert lien.publies == []


def test_base_et_annuler(session, lien):
    asyncio.run(mouvement.executer_tache(session, "base"))
    asyncio.run(mouvement.executer_tache(session, "annuler"))
    ordres = [o for o in lien.trames if not est_requete_pure(o)]
    assert ordres == [bytes.fromhex("10 31 28 21"), bytes.fromhex("10 31 28 1c")]


def test_plateau_haut_emet_la_hauteur_maximale(session, lien):
    asyncio.run(mouvement.executer_plateau(session))
    assert lien.trames[0] == bytes.fromhex("10 31 28 32 62 04 6a 02 08 64")


# --- SEC-07 et analyse ------------------------------------------------------


def test_analyse_de_sequence():
    ordres = mouvement.analyser_sequence("plateau-haut; tourner 90; avancer 1")
    assert ordres == [Ordre("plateau-haut"), Ordre("tourner", 90.0), Ordre("avancer", 1.0)]
    assert mouvement.analyser_sequence(" reculer 0,5 ;; stop ;") == [Ordre("reculer", 0.5), Ordre("stop")]


@pytest.mark.parametrize(
    "texte", ["", " ; ", "voler 3", "tourner", "avancer un", "stop 3", "tourner 90 10", "reprendre", "tondre"]
)
def test_analyse_de_sequence_refuse_avant_toute_etape(texte):
    with pytest.raises(ErreurSaisie):
        mouvement.analyser_sequence(texte)


def test_sec07_sequence_s_arrete_au_premier_refus(session, lien):
    ordres = [Ordre("tourner", 90.0), Ordre("avancer", 5.0), Ordre("avancer", 1.0)]
    bilan = asyncio.run(mouvement.executer_sequence(session, ordres))
    assert bilan["interrompue"] is True
    assert [etape["ordre"] for etape in bilan["etapes"]] == ["tourner 90", "avancer 5"]
    assert "SEC-02" in bilan["etapes"][1]["echec"]
    assert CONSIGNE_AVANT not in lien.trames  # la troisième étape n'a jamais été émise


def test_sec07_sequence_s_arrete_au_premier_echec(session, lien):
    lien.sur_publication = casse_a_la(3, _echec)
    bilan = asyncio.run(mouvement.executer_sequence(session, [AVANCER_1, Ordre("tourner", 90.0)]))
    assert bilan["interrompue"] is True
    assert len(bilan["etapes"]) == 1 and "echec" in bilan["etapes"][0]
    assert lien.arrets == [ARRET] * 3


def test_sec07_chaque_etape_reverifie_les_lames(session, lien, horloge):
    def passe_en_tonte(_lien, octets):
        if octets == ARRET and len(lien.arrets) == 0:
            horloge.t += 0.01
            session.recevoir_etat(fragment(statut=2, est=1.0))

    lien.sur_publication = passe_en_tonte
    bilan = asyncio.run(mouvement.executer_sequence(session, [AVANCER_1, AVANCER_1]))
    assert bilan["interrompue"] is True
    assert "SEC-01" in bilan["etapes"][1]["echec"]
    assert len(lien.consignes) == 40
