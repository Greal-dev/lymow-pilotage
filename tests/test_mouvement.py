"""SEC-01, SEC-03, SEC-04, SEC-07 de bout en bout sur un faux client MQTT et un faux robot."""

from __future__ import annotations

import asyncio
import math
import signal
import time

import pytest
from conftest import ARRET, DEMANDE_CONFIGURATION, SUJET, est_consigne, fragment, lignes_journal

from lymow_pilotage import mouvement, securite
from lymow_pilotage.mouvement import ArretNonConfirme, ErreurSaisie, Ordre
from lymow_pilotage.protocole import Categorie, est_requete_pure
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.session import EchecOrdre

AVANCER_1 = Ordre("avancer", 1.0)
CONSIGNE_AVANT = bytes.fromhex("10 31 52 0a 0d 00 00 80 3e 15 00 00 00 00")
PAUSE = bytes.fromhex("10 31 28 03")
IMMOBILE = "le robot n'a pas bougé (patinage ou ordre ignoré)"
NON_RAFRAICHI = "état non rafraîchi depuis l'ordre : déplacement non vérifié"


def au_premier_arret(action):
    """Déclenche `action(lien)` quand le premier arrêt est publié."""
    deja = []

    def sur_publication(lien, octets):
        if octets == ARRET and not deja:
            deja.append(True)
            action(lien)

    return sur_publication


def robot_ensuite(etat):
    """Après l'arrêt, le faux robot rapporte `etat` (ou se tait si `None`)."""

    def action(lien):
        lien.robot = etat

    return au_premier_arret(action)


def a_la_consigne(n, action):
    """Déclenche `action(lien)` à la n-ième consigne de mouvement."""
    compte = []

    def sur_publication(lien, octets):
        if est_consigne(octets):
            compte.append(octets)
            if len(compte) == n:
                action(lien)

    return sur_publication


def _echec(_lien):
    raise ConnectionError("wss://hote/mqtt?X-Amz-Signature=abcdef")


def _coupure(lien):
    lien.connecte = False


def lancer(coroutine):
    return asyncio.run(coroutine)


# --- nominal ----------------------------------------------------------------


def test_mouvement_nominal_cadence_arret_et_constat(session, lien):
    lien.sur_publication = robot_ensuite(fragment(est=1.0))
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER_1))

    assert all(sujet == SUJET for sujet, _octets in lien.publies)
    assert lien.ordres == [CONSIGNE_AVANT] * 40 + [ARRET] * 3  # 4 s à 10 Hz, puis SEC-03
    assert rapport["emissions"] == 40
    assert rapport["etat_rafraichi"] is True and rapport["verifie"] is True
    assert rapport["avant"] == {"est_m": 0.0, "nord_m": 0.0, "cap_deg": 0.0}
    assert rapport["apres"]["est_m"] == 1.0
    assert (rapport["deplacement_m"], rapport["avance_m"]) == (1.0, 1.0)
    assert rapport["constat"] == "déplacement constaté"


def test_rotation_gauche_constatee(session, lien):
    lien.sur_publication = robot_ensuite(fragment(theta=math.radians(90)))
    rapport = lancer(mouvement.executer_mouvement(session, Ordre("tourner", 90.0)))
    assert set(lien.consignes) == {bytes.fromhex("10 31 52 0a 0d 00 00 00 00 15 33 33 b3 3e")}
    assert rapport["rotation_deg"] == 90.0 and rapport["verifie"] is True


# --- SEC-01 -----------------------------------------------------------------


@pytest.mark.parametrize("statut", [2, 8, 9, 4, 13, 99])
def test_sec01_tonte_reprise_zone_retour_urgence_aucune_consigne_emise(session, lien, statut):
    lien.robot = fragment(statut=statut)
    with pytest.raises(RefusSecurite, match="SEC-01"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == []


@pytest.mark.parametrize("statut", [3, 7])
def test_sec01_pause_et_erreur_autorisent_le_mouvement_manuel(session, lien, statut):
    lien.robot = fragment(statut=statut)
    lien.sur_publication = robot_ensuite(fragment(statut=statut, est=1.0))
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [CONSIGNE_AVANT] * 40 + [ARRET] * 3
    assert rapport["verifie"] is True


def test_sec01_passage_en_pause_pendant_le_mouvement_ne_l_interrompt_pas(session, lien):
    lien.sur_publication = a_la_consigne(5, lambda _lien: session.recevoir_etat(fragment(statut=3)))
    lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 40


def test_sec01_robot_silencieux_refuse_et_seules_des_requetes_pures_sont_parties(session, lien):
    lien.robot = None
    with pytest.raises(RefusSecurite, match="aucun état"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == []
    assert lien.trames and all(est_requete_pure(octets) for octets in lien.trames)


def test_sec01_etat_non_sollicite_recu_avant_la_demande_ne_suffit_pas(session, lien):
    session.recevoir_etat(fragment())  # état valable, mais antérieur à la sollicitation
    lien.robot = None
    with pytest.raises(RefusSecurite, match="aucun état"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == []


def test_sec01_etat_retenu_par_le_serveur_ignore(session, lien, horloge):
    lien.robot = None

    def message_retenu(_lien, octets):
        if octets == DEMANDE_CONFIGURATION:
            horloge.t += 0.01
            session.recevoir_etat(fragment(), retenu=True)

    lien.sur_publication = message_retenu
    with pytest.raises(RefusSecurite, match="aucun état"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert session.etat.statut is None and session.etat.en_ligne is None
    assert lien.ordres == []


def test_sec01_presence_retenue_ne_vaut_pas_presence(session):
    session.recevoir_presence(True, retenu=True)
    assert session.etat.en_ligne is None
    session.recevoir_presence(False)
    assert session.etat.en_ligne is False


def test_sec01_robot_annonce_hors_ligne_refuse(session, lien, horloge):
    lien.robot = None

    def etat_puis_hors_ligne(_lien, octets):
        if octets == DEMANDE_CONFIGURATION:
            horloge.t += 0.01
            session.recevoir_etat(fragment())
            session.recevoir_presence(False)

    lien.sur_publication = etat_puis_hors_ligne
    with pytest.raises(RefusSecurite, match="hors ligne"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == []


def test_sec01_sans_droit_de_solliciter_pas_de_mouvement(session, lien):
    session.autorisees = frozenset(Categorie) - {Categorie.REQUETE_PURE}
    with pytest.raises(RefusSecurite, match="SEC-01"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.publies == []


def test_sec01_plateau_haut_refuse_pendant_la_tonte(session, lien):
    lien.robot = fragment(statut=2)
    with pytest.raises(RefusSecurite):
        lancer(mouvement.executer_plateau(session))
    assert lien.ordres == []


def test_sec01_statut_qui_passe_en_tonte_pendant_le_mouvement_arrete_tout(session, lien):
    lien.sur_publication = a_la_consigne(5, lambda _lien: session.recevoir_etat(fragment(statut=2)))
    with pytest.raises(EchecOrdre, match="interrompu en cours de route.*SEC-01"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.ordres == [CONSIGNE_AVANT] * 5 + [ARRET] * 3


def test_sec01_passage_hors_ligne_pendant_le_mouvement_arrete_tout(session, lien):
    lien.sur_publication = a_la_consigne(5, lambda _lien: session.recevoir_presence(False))
    with pytest.raises(EchecOrdre, match="hors ligne"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 5 and lien.arrets == [ARRET] * 3


# --- SEC-03 -----------------------------------------------------------------


def test_sec03_publication_qui_echoue_arret_tente_et_erreur(session, lien):
    lien.sur_publication = a_la_consigne(5, _echec)
    with pytest.raises(EchecOrdre, match="non acquittée") as erreur:
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 4
    assert lien.arrets == [ARRET] * 3
    assert "X-Amz" not in str(erreur.value)


def test_sec03_deconnexion_en_cours_de_mouvement_arret_non_confirme(session, lien):
    lien.sur_publication = a_la_consigne(5, _coupure)
    with pytest.raises(ArretNonConfirme, match="ARRÊT NON CONFIRMÉ"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 5  # homme mort : plus rien après la coupure
    assert lien.arrets == []


def test_sec03_deconnexion_puis_reconnexion_pour_arreter(session, lien):
    async def reconnecter():
        lien.connecte = True

    lien.reconnecter = reconnecter
    lien.sur_publication = a_la_consigne(5, _coupure)
    with pytest.raises(EchecOrdre, match="non connecté"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert len(lien.consignes) == 5
    assert lien.arrets == [ARRET] * 3


def test_sec03_refus_d_emettre_si_mqtt_non_connecte(session, lien):
    lien.connecte = False
    with pytest.raises(EchecOrdre, match="non connecté"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.publies == []


def test_sec03_un_arret_non_acquitte_sur_trois_est_une_erreur(session, lien):
    def refus(_lien):
        raise TimeoutError

    lien.sur_publication = au_premier_arret(refus)
    with pytest.raises(ArretNonConfirme, match="2/3"):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))


def test_sec03_un_arret_manque_est_reemis_apres_reconnexion(session, lien, messages):
    def refus(_lien):
        raise TimeoutError

    reconnexions = []

    async def reconnecter():
        reconnexions.append(True)

    lien.reconnecter = reconnecter
    lien.sur_publication = au_premier_arret(refus)
    assert lancer(mouvement.arreter(session)) == 3
    assert lien.arrets == [ARRET] * 3 and reconnexions == [True]
    assert messages == ["Arrêt émis et acquitté 3/3."]


def test_sec03_les_trois_arrets_partent_en_parallele(session, lien):
    lien.retard = 0.05  # acquittement lent : en série, un seul arrêt serait en vol à la fois
    assert lancer(mouvement.arreter(session)) == 3
    assert lien.max_en_vol == 3


@pytest.mark.parametrize("panne", ["muet", "lent"])
def test_sec03_echeance_globale_de_l_arret_tenue_sur_lien_a_moitie_mort(session, lien, monkeypatch, panne):
    monkeypatch.setattr(securite, "ECHEANCE_ARRET_S", 0.4)
    monkeypatch.setattr(securite, "DELAI_ARRET_S", 0.15)
    monkeypatch.setattr(securite, "PAUSE_REPRISE_ARRET_S", 0.02)
    reconnexions = []

    async def reconnecter():
        reconnexions.append(True)

    lien.reconnecter = reconnecter
    lien.muet, lien.retard = (True, 0.0) if panne == "muet" else (False, 30.0)
    debut = time.monotonic()
    with pytest.raises(ArretNonConfirme, match="0/3"):
        lancer(mouvement.arreter(session))
    duree = time.monotonic() - debut
    assert 0.35 <= duree < 1.0  # ni abandon précoce, ni attente sans fin
    assert reconnexions  # la reconnexion a bien été tentée entre deux salves
    assert lien.arrets == []


def test_sec03_reconnexion_qui_ne_rend_jamais_la_main_reste_bornee(session, lien, monkeypatch):
    monkeypatch.setattr(securite, "ECHEANCE_ARRET_S", 0.3)

    async def reconnecter():
        await asyncio.Event().wait()

    lien.connecte = False
    lien.reconnecter = reconnecter
    debut = time.monotonic()
    with pytest.raises(ArretNonConfirme):
        lancer(mouvement.arreter(session))
    assert time.monotonic() - debut < 1.0


def test_sec03_interruption_clavier_envoie_l_arret(session, lien):
    async def scenario():
        tache = asyncio.ensure_future(mouvement.executer_mouvement(session, AVANCER_1))
        lien.sur_publication = a_la_consigne(5, lambda _lien: tache.cancel())  # effet de Ctrl+C
        with pytest.raises(asyncio.CancelledError):
            await tache

    lancer(scenario())
    assert len(lien.consignes) == 5
    assert lien.arrets == [ARRET] * 3


def test_sec03_signal_pendant_le_mouvement_mene_a_l_arret_pas_a_une_sortie_seche(session, lien):
    avant = signal.getsignal(signal.SIGINT)

    async def scenario():
        tache = asyncio.ensure_future(mouvement.executer_mouvement(session, AVANCER_1))
        lien.sur_publication = a_la_consigne(5, lambda _lien: signal.raise_signal(signal.SIGINT))
        with pytest.raises(asyncio.CancelledError):
            await tache

    lancer(scenario())
    assert 5 <= len(lien.consignes) <= 6
    assert lien.arrets == [ARRET] * 3
    assert signal.getsignal(signal.SIGINT) is avant  # gestionnaire d'origine rétabli


def test_sec03_seconde_interruption_pendant_l_arret_les_arrets_partent_quand_meme(session, lien, messages):
    signaux = [getattr(signal, nom) for nom in ("SIGINT", "SIGBREAK") if hasattr(signal, nom)]
    non_proteges = []

    async def scenario():
        tache = asyncio.ensure_future(mouvement.executer_mouvement(session, AVANCER_1))

        def pendant_l_arret(lien_):
            lien_.retard = 0.05  # l'arrêt est encore en vol quand l'interruption arrive
            tache.cancel()
            for numero in signaux:
                if signal.getsignal(numero) in (signal.SIG_DFL, signal.default_int_handler):
                    non_proteges.append(numero)  # le lever tuerait le processus de test
                else:
                    signal.raise_signal(numero)

        premiere = a_la_consigne(5, lambda _lien: tache.cancel())
        seconde = au_premier_arret(pendant_l_arret)

        def sur_publication(lien_, octets):
            premiere(lien_, octets)
            seconde(lien_, octets)

        lien.sur_publication = sur_publication
        with pytest.raises(asyncio.CancelledError):
            await tache

    lancer(scenario())
    assert non_proteges == []
    assert lien.arrets == [ARRET] * 3
    ignorees = [m for m in messages if m.startswith("Arrêt en cours")]
    assert len(ignorees) == len(signaux)
    assert messages[-1] == "Arrêt émis et acquitté 3/3."


def test_sec03_journal_en_panne_ou_etat_recu_pendant_l_arret_ne_le_retardent_pas(session, lien, tmp_path):
    differes = []

    def pendant_l_arret(lien_):
        lien_.retard = 0.01
        session.recevoir_etat(fragment(est=1.0))
        differes.extend(session._differes)  # rien n'est écrit pendant l'arrêt
        session.journal._dossier = tmp_path / "calibration-absente" / "\0"  # journal cassé ensuite

    lien.sur_publication = au_premier_arret(pendant_l_arret)
    lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert lien.arrets == [ARRET] * 3
    assert [evenement for evenement, _champs in differes] == ["etat-recu"]
    assert "arret-emis" not in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_sec03_stop_reel_sans_aucune_precondition(session, lien, messages):
    lien.robot = None
    rapport = lancer(mouvement.executer_arret(session))
    assert lien.trames[:4] == [ARRET] * 3 + [PAUSE]  # consignes nulles d'abord, pause ensuite
    assert rapport["arrets_acquittes"] == 3
    assert rapport["pause"] == {"userCtrl": 3, "acquittee": True}  # sans état connu : 3
    assert rapport["etat_rafraichi"] is False
    assert messages == ["Arrêt émis et acquitté 3/3."]


@pytest.mark.parametrize(("statut", "trame"), [(4, "10 31 28 15"), (2, "10 31 28 03"), (10, "10 31 28 03")])
def test_stop_choisit_la_pause_selon_le_dernier_etat_connu(session, lien, statut, trame):
    session.recevoir_etat(fragment(statut=statut))  # 4 = retour à la base : pause du retour (21)
    lancer(mouvement.executer_arret(session))
    assert lien.ordres == [ARRET] * 3 + [bytes.fromhex(trame)]


def test_stop_pause_emise_meme_si_l_arret_n_est_pas_confirme(session, lien, messages):
    def refuse_les_arrets(_lien, octets):
        if octets == ARRET:
            raise TimeoutError

    lien.sur_publication = refuse_les_arrets
    with pytest.raises(ArretNonConfirme, match="0/3"):
        lancer(mouvement.executer_arret(session))
    assert lien.ordres == [PAUSE]
    assert messages == []  # ni « acquitté » mensonger, ni alerte de pause


def test_stop_echec_de_la_pause_signale_sans_masquer_l_arret(session, lien, messages):
    def refuse_la_pause(_lien, octets):
        if octets == PAUSE:
            raise TimeoutError

    lien.sur_publication = refuse_la_pause
    rapport = lancer(mouvement.executer_arret(session))
    assert rapport["arrets_acquittes"] == 3 and rapport["pause"] == {"userCtrl": 3, "acquittee": False}
    assert messages == ["Arrêt émis et acquitté 3/3.", "Mise en pause de la tâche (userCtrl 3) non acquittée."]


def test_arret_de_fin_de_mouvement_sans_pause(session, lien):
    lien.sur_publication = a_la_consigne(5, _echec)
    with pytest.raises(EchecOrdre):
        lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert PAUSE not in lien.trames and lien.arrets == [ARRET] * 3


def test_session_refuse_une_categorie_non_autorisee(session, lien):
    session.autorisees = frozenset({Categorie.REQUETE_PURE})
    with pytest.raises(RefusSecurite):
        lancer(mouvement.arreter(session))
    assert lien.publies == []


# --- SEC-04 -----------------------------------------------------------------


def test_sec04_robot_immobile_signale(session, lien):
    lien.sur_publication = robot_ensuite(fragment(est=0.01))
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert rapport["constat"] == IMMOBILE and rapport["verifie"] is False
    assert rapport["deplacement_m"] == 0.01


def test_sec04_etat_non_rafraichi_dit_plutot_qu_affirme(session, lien):
    lien.sur_publication = robot_ensuite(None)
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert rapport["etat_rafraichi"] is False and rapport["verifie"] is False
    assert rapport["apres"] is None
    assert "deplacement_m" not in rapport
    assert rapport["constat"] == NON_RAFRAICHI


def test_sec04_position_de_depart_non_fraiche_constat_impossible(session, lien):
    session.recevoir_etat(fragment(est=5.0))  # vieille pose, antérieure à la sollicitation
    lien.robot = {"workStatus": 1, "battery": 80}  # le robot répond sans position
    lien.sur_publication = robot_ensuite(fragment(est=6.0))
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER_1))
    assert rapport["avant"] is None and rapport["verifie"] is False
    assert rapport["constat"].startswith("constat impossible")
    assert "deplacement_m" not in rapport


@pytest.mark.parametrize(
    ("ordre", "ensuite", "constat"),
    [
        (AVANCER_1, fragment(est=-1.0), "déplacement dans le mauvais sens"),
        (Ordre("reculer", 1.0), fragment(est=1.0), "déplacement dans le mauvais sens"),
        (AVANCER_1, fragment(est=1.7), "déplacement nettement supérieur à l'ordre"),
        (Ordre("tourner", 90.0), fragment(theta=math.radians(-90)), "rotation dans le mauvais sens"),
        (Ordre("tourner", 20.0), fragment(theta=math.radians(60)), "rotation nettement supérieure à l'ordre"),
    ],
)
def test_sec04_mauvais_sens_ou_exces_signales(session, lien, ordre, ensuite, constat):
    lien.sur_publication = robot_ensuite(ensuite)
    rapport = lancer(mouvement.executer_mouvement(session, ordre))
    assert rapport["constat"].startswith(constat) and rapport["verifie"] is False


@pytest.mark.parametrize(
    ("ordre", "ensuite"),
    [
        (Ordre("reculer", 1.0), fragment(est=-0.9)),
        (Ordre("tourner", 180.0), fragment(theta=math.radians(-175))),  # demi-tour : sens indécidable
        (Ordre("tourner", -45.0), fragment(theta=math.radians(-50))),
    ],
)
def test_sec04_deplacement_conforme_verifie(session, lien, ordre, ensuite):
    lien.sur_publication = robot_ensuite(ensuite)
    rapport = lancer(mouvement.executer_mouvement(session, ordre))
    assert rapport["constat"] == "déplacement constaté" and rapport["verifie"] is True


# --- tâches -----------------------------------------------------------------


def test_reprendre_quitte_d_abord_la_telecommande(session, lien):
    lien.robot = fragment(statut=6)
    rapport = lancer(mouvement.executer_tache(session, "reprendre"))
    assert lien.ordres == [bytes.fromhex("10 31 28 20"), bytes.fromhex("10 31 28 04")]
    assert rapport["codes_emis"] == [32, 4]


def test_reprendre_refuse_s_il_n_y_a_rien_a_reprendre(session, lien):
    with pytest.raises(RefusSecurite, match="rien à reprendre"):
        lancer(mouvement.executer_tache(session, "reprendre"))
    assert lien.ordres == []


def test_base_et_annuler(session, lien):
    lancer(mouvement.executer_tache(session, "base"))
    lancer(mouvement.executer_tache(session, "annuler"))
    assert lien.ordres == [bytes.fromhex("10 31 28 21"), bytes.fromhex("10 31 28 1c")]


def test_plateau_haut_emet_la_hauteur_maximale(session, lien):
    rapport = lancer(mouvement.executer_plateau(session))
    assert lien.ordres == [bytes.fromhex("10 31 28 32 62 04 6a 02 08 64")]
    assert rapport["verifie"] is True


# --- SEC-07 et analyse ------------------------------------------------------


def test_analyse_de_sequence():
    ordres = mouvement.analyser_sequence("plateau-haut; tourner 90; avancer 1")
    assert ordres == [Ordre("plateau-haut"), Ordre("tourner", 90.0), Ordre("avancer", 1.0)]
    assert mouvement.analyser_sequence(" reculer 0,5 ;; stop ;") == [Ordre("reculer", 0.5), Ordre("stop")]
    assert mouvement.analyser_sequence("tourner -45.5") == [Ordre("tourner", -45.5)]


@pytest.mark.parametrize(
    "texte", ["", " ; ", "voler 3", "tourner", "avancer un", "stop 3", "tourner 90 10", "reprendre", "tondre"]
)
def test_analyse_de_sequence_refuse_avant_toute_etape(texte):
    with pytest.raises(ErreurSaisie):
        mouvement.analyser_sequence(texte)


@pytest.mark.parametrize("nombre", ["1_0", "١", "１", "1e1", "inf", "nan", "0x10", "1.", ".5", "1 0", "--1", ""])
def test_nombres_exotiques_refuses(nombre):
    with pytest.raises(ValueError):
        mouvement.lire_nombre(nombre)
    with pytest.raises(ErreurSaisie):
        mouvement.analyser_sequence(f"avancer {nombre}")


def test_sec07_une_etape_au_dessus_d_un_plafond_et_rien_n_est_emis(session, lien):
    ordres = [Ordre("tourner", 90.0), Ordre("avancer", 5.0), AVANCER_1]
    bilan = lancer(mouvement.executer_sequence(session, ordres))
    assert lien.publies == []  # pas même une requête pure
    assert bilan["interrompue"] is True and bilan["etapes"] == []
    assert "avancer 5" in bilan["motif"] and "SEC-02" in bilan["motif"]
    assert bilan["non_executees"] == ["tourner 90", "avancer 5", "avancer 1"]


def test_sec07_plateau_hors_borne_refuse_toute_la_sequence(session, lien):
    import dataclasses

    session.cal = dataclasses.replace(session.cal, plateau_haut_mm=40)
    bilan = lancer(mouvement.executer_sequence(session, [AVANCER_1, Ordre("plateau-haut")]))
    assert lien.publies == [] and "plateau-haut" in bilan["motif"]


def test_sec07_sequence_s_arrete_au_premier_echec(session, lien):
    lien.sur_publication = a_la_consigne(3, _echec)
    bilan = lancer(mouvement.executer_sequence(session, [AVANCER_1, Ordre("tourner", 90.0)]))
    assert bilan["interrompue"] is True and bilan["arret_non_confirme"] is False
    assert len(bilan["etapes"]) == 1 and "echec" in bilan["etapes"][0]
    assert bilan["non_executees"] == ["tourner 90"]
    assert lien.arrets == [ARRET] * 3


def test_sec07_arret_non_confirme_remonte_dans_le_bilan(session, lien):
    lien.sur_publication = a_la_consigne(3, _coupure)
    bilan = lancer(mouvement.executer_sequence(session, [AVANCER_1, AVANCER_1]))
    assert bilan["arret_non_confirme"] is True
    assert "ARRÊT NON CONFIRMÉ" in bilan["motif"] and bilan["non_executees"] == ["avancer 1"]


def test_sec07_chaque_etape_reverifie_les_lames(session, lien):
    lien.sur_publication = robot_ensuite(fragment(statut=2, est=1.0))
    bilan = lancer(mouvement.executer_sequence(session, [AVANCER_1, AVANCER_1]))
    assert bilan["interrompue"] is True
    assert bilan["etapes"][0]["verifie"] is True
    assert "SEC-01" in bilan["etapes"][1]["echec"]
    assert len(lien.consignes) == 40


@pytest.mark.parametrize(("ensuite", "constat"), [(fragment(), IMMOBILE), (None, NON_RAFRAICHI)])
def test_sec07_sequence_interrompue_si_le_constat_ne_confirme_pas(session, lien, ensuite, constat):
    lien.sur_publication = robot_ensuite(ensuite)
    bilan = lancer(mouvement.executer_sequence(session, [AVANCER_1, Ordre("tourner", 90.0), AVANCER_1]))
    assert bilan["interrompue"] is True
    assert bilan["motif"] == f"séquence interrompue après « avancer 1 » : {constat}"
    assert bilan["non_executees"] == ["tourner 90", "avancer 1"]
    assert len(bilan["etapes"]) == 1 and len(lien.consignes) == 40


def test_sec07_constat_negatif_sur_la_derniere_etape_n_interrompt_rien(session, lien):
    bilan = lancer(mouvement.executer_sequence(session, [AVANCER_1]))
    assert bilan["interrompue"] is False and "non_executees" not in bilan
    assert bilan["etapes"][0]["constat"] == IMMOBILE
