"""SEC-13 (garde-fou carte), SEC-14 (arrêt sur indice de choc), allure : logique pure, puis par le chemin des ordres.

Carte d'essai (test_carte.contenu) : pelouse 10 × 6 m, massif interdit [4 ; 6] × [2 ; 4], passage de (9,5 ; 3)
à (14,5 ; 3), verger [14 ; 20] × [0 ; 6].
"""

from __future__ import annotations

import asyncio
import math

import pytest
from conftest import est_consigne, fragment
from test_carte import contenu

from lymow_pilotage import carte as cartes
from lymow_pilotage import cli, fleches, garde_carte, mouvement, securite, ui
from lymow_pilotage.garde_carte import INCHANGE, INCONNU, RACCOURCI, REFUS, SuiviDeProgression, juger_pas
from lymow_pilotage.mouvement import ErreurSaisie, Ordre

CARTE = cartes.decoder(contenu())


# --- SEC-13 : logique pure ---------------------------------------------------------------------


def test_sec13_bord_droit_devant_pas_inchange_raccourci_ou_refuse():
    assert (garde_carte.MARGE_FIXE_M, garde_carte.PAS_MIN_M) == (0.30, 0.25)
    assert garde_carte.bord_devant(CARTE, (7.02, 5.0), 0.0) == 2.95 and garde_carte.bord_devant(CARTE, (7.02, 5.0), 0.0, -1.0) == 7.0
    loin = juger_pas(CARTE, (7.02, 5.0), 0.0, 1.0, 0.02)
    assert (loin.decision, loin.distance_m, loin.motif, loin.arrivee_au_bord_m) == (INCHANGE, 1.0, "bord de zone devant : 2,95 m", 1.95)
    # L'exemple du propriétaire, à l'échantillonnage près (5 cm) : bord à 0,85 m, marge 0,30 m → pas de 0,55 m.
    proche = juger_pas(CARTE, (9.12, 5.0), 0.0, 1.0, 0.0)
    assert (proche.decision, proche.distance_m) == (RACCOURCI, 0.55)
    assert proche.motif == "pas réduit à 0,55 m : bord de zone devant à 0,85 m (marge de sécurité 0,3 m)"
    colle = juger_pas(CARTE, (9.62, 5.0), 0.0, 0.5, 0.0)  # bord à 0,35 m : il ne reste que 5 cm sûrs
    assert colle.decision == REFUS and colle.bloque and colle.motif.startswith("pas refusé : bord de zone devant à 0,35 m")
    recul = juger_pas(CARTE, (9.12, 5.0), 180.0, -1.0, 0.0)  # reculer, dos au bord
    assert recul.decision == RACCOURCI and "bord de zone derrière à 0,85 m" in recul.motif


def test_sec13_zone_interdite_sur_le_trajet():
    assert garde_carte.bord_devant(CARTE, (1.02, 3.0), 0.0) == 2.95  # le massif commence à x = 4
    assert juger_pas(CARTE, (1.02, 3.0), 0.0, 2.0, 0.02).decision == INCHANGE
    devant_le_massif = juger_pas(CARTE, (2.52, 3.0), 0.0, 2.0, 0.02)
    assert (devant_le_massif.decision, devant_le_massif.distance_m) == (RACCOURCI, 1.1)
    assert garde_carte.bord_devant(CARTE, (5.0, 3.0), 0.0) is None  # robot DANS la zone interdite : rien à mesurer
    assert juger_pas(CARTE, (5.0, 3.0), 0.0, 1.0, 0.02).decision == INCONNU


def test_sec13_passage_etroit_suivi_dans_l_axe_refuse_en_travers():
    dans_l_axe = juger_pas(CARTE, (12.0, 3.0), 0.0, 2.0, 0.02)  # le long du passage, vers le verger
    assert dans_l_axe.decision == INCHANGE and dans_l_axe.bord_m > 7.5
    en_travers = juger_pas(CARTE, (12.0, 3.0), 90.0, 0.5, 0.02)  # 0,5 m de part et d'autre du tracé
    assert en_travers.decision == REFUS and en_travers.bord_m == pytest.approx(0.5, abs=0.06)


def test_sec13_sans_carte_sans_pose_ou_robot_hors_zone_rien_n_est_bloque_et_c_est_dit():
    cas = {
        "distance au bord inconnue : aucune carte de référence": juger_pas(None, (1.0, 1.0), 0.0, 1.0, 0.02),
        "distance au bord inconnue : position du robot non fournie": juger_pas(CARTE, None, None, 1.0, 0.02),
        "distance au bord inconnue : le robot est hors des zones de la carte": juger_pas(CARTE, (12.0, 5.5), 0.0, 1.0, 0.02),
    }
    for motif, jugement in cas.items():
        assert (jugement.decision, jugement.motif, jugement.bloque, jugement.distance_m) == (INCONNU, motif, False, 1.0)


def test_sec13_incertitude_qui_absorbe_toute_la_marge():
    assert garde_carte.marge_de_securite(0.02) == pytest.approx(0.32) and garde_carte.marge_de_securite(1.32) == pytest.approx(1.62)
    assert garde_carte.marge_de_securite(None) == pytest.approx(0.80)  # incertitude non annoncée : comptée « dégradée »
    assert garde_carte.marge_de_securite(math.nan) == pytest.approx(0.80)
    assert juger_pas(CARTE, (7.02, 5.0), 0.0, 1.0, 0.02).decision == INCHANGE
    assert juger_pas(CARTE, (7.02, 5.0), 0.0, 1.0, 1.32).decision == INCHANGE  # 2,95 − 1,62 : il reste 1,33 m
    assert juger_pas(CARTE, (7.02, 5.0), 0.0, 2.0, 1.32).distance_m == 1.3
    perdue = juger_pas(CARTE, (7.02, 5.0), 0.0, 1.0, 2.9)  # ±2,9 m : le robot est peut-être déjà au bord
    assert perdue.decision == REFUS and "marge de sécurité 3,2 m" in perdue.motif


def test_allure_lente_pres_d_un_bord_ou_position_non_fiable_sauf_choix_de_l_operateur():
    assert (garde_carte.ALLURE_LENTE_M_S, garde_carte.ALLURE_NORMALE_M_S, garde_carte.BORD_PROCHE_M) == (0.10, 0.25, 1.0)
    loin = juger_pas(CARTE, (7.02, 5.0), 0.0, 1.0, 0.02)  # arrivée à 1,95 m du bord
    pres = juger_pas(CARTE, (7.02, 5.0), 0.0, 2.0, 0.02)  # arrivée à 0,95 m du bord
    assert garde_carte.allure(None, loin, 0.02) == (None, None)
    assert garde_carte.allure(None, pres, 0.02) == (0.10, "allure lente : l'arrivée est à moins de 1 m d'un bord de zone")
    # Position non fiable : la carte n'est pas crédible, l'allure reste normale (décision du 2026-10-12).
    for precision in (0.3, 1.32, 4.68):
        assert garde_carte.allure(None, loin, precision) == (None, None) and garde_carte.allure(None, pres, precision) == (None, None)
    assert garde_carte.allure(0.25, pres, 1.32) == (None, None)  # allure choisie par l'opérateur : respectée
    assert garde_carte.allure(None, loin, None) == (None, None)  # précision non annoncée : rien n'est changé


# --- SEC-14 : logique pure -----------------------------------------------------------------------


def test_sec14_codes_de_choc_reconnus():
    assert garde_carte.codes_de_choc({"errorCodes": [74, 3], "warningCodes": [24, 17]}) == {("erreur", 74), ("avertissement", 24)}
    assert garde_carte.codes_de_choc({}) == frozenset() and garde_carte.codes_de_choc({"errorCodes": None}) == frozenset()
    for code in (7, 8, 17, 44, 59, 60, 74, 75, 76, 77):  # soulèvement, vide, pare-chocs, ultrasons, obstacle
        assert code in garde_carte.ERREURS_DE_CHOC


def test_sec14_non_progression_jugee_seulement_a_vitesse_etablie_et_pose_frequente():
    def suivi(vitesse_reelle: float, periode: float = 0.6, jusqu_a: float = 4.0) -> tuple[SuiviDeProgression, list[bool]]:
        s, verdicts, t = SuiviDeProgression(0.25, 100.0), [], 100.0
        while t <= 100.0 + jusqu_a:
            s.noter(t, vitesse_reelle * (t - 100.0), 0.0)
            verdicts.append(s.bloque(t))
            t += periode
        return s, verdicts

    libre, verdicts = suivi(0.25)
    assert not any(verdicts) and libre.active is True  # le robot avance comme attendu
    lent, verdicts = suivi(0.06)  # 24 % de l'attendu : au-dessus du seuil de 20 %
    assert not any(verdicts)
    bloque, verdicts = suivi(0.0)
    assert verdicts[:4] == [False] * 4 and verdicts[-1] is True  # rien avant 2,5 s (1 s d'établissement + 1,5 s de fenêtre)
    patine, verdicts = suivi(0.04)  # 16 % de l'attendu
    assert verdicts[-1] is True
    rare, verdicts = suivi(0.0, periode=2.0, jusqu_a=8.0)  # pose toutes les 2 s : non jugeable
    assert not any(verdicts) and rare.active is False
    assert (garde_carte.FENETRE_PROGRESSION_S, garde_carte.PROGRESSION_MIN) == (1.5, 0.20)


# --- par le chemin des ordres ----------------------------------------------------------------------


def _robot(est: float, nord: float = 5.0, precision: float = 0.01, **plus) -> dict:
    return fragment(statut=3, tache=2, est=est, nord=nord, precision=precision) | plus


def test_sec13_avance_reelle_raccourcie_annoncee_et_lente(session, lien):
    session.carte_reference = CARTE
    session.arret_aux_bords = True  # case « Arrêter aux bords de zone » cochée
    lien.robot = _robot(9.12)  # bord à 0,85 m, marge 0,31 m
    bilan = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 1.0)]))
    etape = bilan["etapes"][0]
    assert etape["ordre"] == "avancer 0.5 --vitesse 0.1" and etape["emissions"] == 50  # 0,5 m à 0,10 m/s, 10 Hz
    assert etape["garde_carte"] == "pas réduit à 0,5 m : bord de zone devant à 0,85 m (marge de sécurité 0,31 m)"
    assert etape["allure"] == "allure lente : l'arrivée est à moins de 1 m d'un bord de zone"
    assert etape["surveillance_progression"] == "inactive : position reçue trop rarement"


def test_sec13_avance_reelle_refusee_rien_n_est_emis_puis_forcee_et_journalisee(session, lien, tmp_path):
    session.carte_reference = CARTE
    session.arret_aux_bords = True  # case « Arrêter aux bords de zone » cochée
    lien.robot = _robot(9.82)  # bord à 0,15 m
    bilan = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5)]))
    assert "pas refusé : bord de zone devant à 0,15 m" in bilan["etapes"][0]["echec"] and "(SEC-13)" in bilan["etapes"][0]["echec"]
    assert lien.consignes == []
    force = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5, vitesse=0.25, force=True)]))
    etape = force["etapes"][0]
    assert etape["ordre"] == "avancer 0.5 --vitesse 0.25 --forcer" and etape["emissions"] == 20
    assert etape["garde_carte"].startswith("pas forcé par l'opérateur malgré : pas refusé")
    journal = "".join(f.read_text(encoding="utf-8") for f in tmp_path.rglob("*.jsonl"))
    assert journal.count('"garde-carte-forcee"') == 1


def test_sec13_sans_carte_de_reference_le_pas_part_et_le_bilan_le_dit(session, lien):
    lien.robot = _robot(9.82)
    etape = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5)]))["etapes"][0]
    assert etape["garde_carte"] == "distance au bord inconnue : aucune carte de référence" and etape["emissions"] == 20
    tourne = asyncio.run(mouvement.executer_sequence(session, [Ordre("tourner", 15.0)]))["etapes"][0]
    assert "garde_carte" not in tourne and "allure" not in tourne  # rotation inchangée


def test_position_degradee_allure_normale_et_pas_long_pres_d_un_bord_plancher_de_vitesse(session, lien):
    lien.robot = _robot(2.0, precision=0.3)
    normal = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5)]))["etapes"][0]
    assert normal["ordre"] == "avancer 0.5" and "allure" not in normal  # position dégradée : allure normale
    session.carte_reference = CARTE
    lien.robot = _robot(8.72, precision=0.02)  # position fiable, bord à 1,25 m : arrivée à moins de 1 m
    etape = [asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", pas)]))["etapes"][0] for pas in (0.5, 2.0)]
    assert etape[0]["ordre"] == "avancer 0.5 --vitesse 0.1" and "moins de 1 m d'un bord de zone" in etape[0]["allure"]
    # Un pas de 2 m ne tient pas en 9 s à 0,10 m/s (SEC-02) : il garde sa longueur, à la plus faible vitesse qui tient.
    assert etape[1]["ordre"] == "avancer 2 --vitesse 0.23" and "0.23 m/s pour ce pas de 2 m (9 s au plus par ordre)" in etape[1]["allure"]


def test_sec14_code_de_choc_apparu_pendant_le_mouvement_arret_immediat(session, lien):
    lien.robot = _robot(2.0, errorCodes=[45])  # un code sans rapport, présent au départ

    def choc(lien_, octets):
        if est_consigne(octets) and len(lien_.consignes) == 6:
            session.recevoir_etat(_robot(2.0, errorCodes=[45, 74]))  # « Channel bumper triggered »

    lien.sur_publication = choc
    bilan = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 1.0)]))
    assert "indice de choc annoncé par le robot (erreur 74) : arrêt (SEC-14)" in bilan["etapes"][0]["echec"]
    assert len(lien.consignes) <= 8 and len(lien.arrets) == 3  # arrêt garanti (SEC-03)


def test_sec14_code_de_choc_deja_present_au_depart_n_empeche_pas_de_degager_le_robot(session, lien):
    lien.robot = _robot(2.0, errorCodes=[44], warningCodes=[24])
    etape = asyncio.run(mouvement.executer_sequence(session, [Ordre("reculer", 0.5)]))["etapes"][0]
    assert "echec" not in etape and etape["emissions"] == 20


def test_sec14_pose_qui_n_avance_plus_obstacle_ou_patinage_arret(session, lien):
    lien.robot = _robot(2.0)
    lien.sur_publication = lambda lien_, octets: session.recevoir_etat(_robot(2.0)) if est_consigne(octets) else None
    bilan = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 2.0)]))
    assert "obstacle ou patinage : arrêt" in bilan["etapes"][0]["echec"] and "(SEC-14)" in bilan["etapes"][0]["echec"]
    assert 25 <= len(lien.consignes) <= 32 and len(lien.arrets) == 3  # jugé après 2,5 s, bien avant les 8 s du pas


def test_sec14_pose_qui_avance_le_pas_va_au_bout_et_la_surveillance_est_dite_active(session, lien):
    lien.robot = _robot(2.0)
    lien.sur_publication = lambda lien_, octets: (
        session.recevoir_etat(_robot(2.0 + 0.025 * len(lien_.consignes))) if est_consigne(octets) else None
    )
    etape = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 2.0)]))["etapes"][0]
    assert "echec" not in etape and etape["emissions"] == 80 and etape["surveillance_progression"] == "active"


# --- flèches, page, commande ---------------------------------------------------------------------------


def test_fleches_allure_et_forcer_bornes_aux_listes():
    assert fleches.ALLURES == {"auto": None, "lente": 0.10, "normale": 0.25}
    assert fleches.ordre("haut", 1.0) == Ordre("avancer", 1.0)
    assert fleches.ordre("haut", 0.5, "lente", True) == Ordre("avancer", 0.5, vitesse=0.10, force=True)
    assert fleches.ordre("bas", 0.5, "normale") == Ordre("reculer", 0.5, vitesse=0.25)
    assert fleches.ordre("gauche", 45.0, "lente", True) == Ordre("tourner", 45.0)  # rotation inchangée
    for allure, forcer in (("rapide", False), (0.3, False), ("lente", "oui"), (None, False)):
        with pytest.raises(ErreurSaisie):
            fleches.ordre("haut", 1.0, allure, forcer)
    for vitesse in fleches.ALLURES.values():
        assert vitesse is None or vitesse <= securite.VITESSE_LINEAIRE_M_S  # SEC-02
    assert ui.ordres_depuis({"ordres": [{"nom": "avancer", "valeur": 1, "force": True}]}) == [Ordre("avancer", 1.0, force=True)]
    for refuse in ({"nom": "tourner", "valeur": 15, "force": True}, {"nom": "avancer", "valeur": 1, "force": "oui"}):
        with pytest.raises(ErreurSaisie):
            ui.ordres_depuis({"ordres": [refuse]})


def test_page_allure_forcer_et_distance_au_bord(tmp_path, monkeypatch, session_reelle, lien):
    from test_ui import attendre, executer, poster, serveur_ui

    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    for identifiant in ("pave-allure", "pave-forcer", "bord-zone"):
        assert f'id="{identifiant}"' in page
    assert "Lente : 0,10 m/s" in page and "Normale : 0,25 m/s" in page and "Forcer ce pas" in page  # en info-bulle
    pave = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    forcer = pave[pave.index("function forcerCePas") :][:500]
    assert 'checked = false' in forcer and "window.confirm(" not in forcer  # un pas : la case cochée est la demande
    lien.robot = _robot(7.02)
    lien.carte = contenu()  # demandée d'office à l'ouverture

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.session.etat.pose is not None)
            await attendre(lambda: serveur.instantane()["ordres"]["bord"]["devant_m"] is not None)
            assert serveur.instantane()["ordres"]["bord"] == {
                "devant_m": 2.95, "derriere_m": 7.0, "texte": "d'après la carte : bord de zone devant à 2,95 m ; derrière à 7 m",
            }
            # Une flèche réelle passe par le garde-fou : la carte connue de la page est celle de la session.
            _statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            statut, _corps, _recus = await poster(
                http, serveur, "/api/direct/fleche", {"jeton": corps["jeton"], "direction": "haut", "pas": 0.25, "allure": "lente"}
            )
            assert statut == 202 and serveur.session.carte_reference is not None
            await attendre(lambda: not serveur.occupe, delai=10.0)
            rapport = serveur.instantane()["direct"]["faites"][-1]["rapport"]
            assert rapport["ordre"] == "avancer 0.25 --vitesse 0.1" and rapport["garde_carte"] == "d'après la carte : bord de zone devant à 2,95 m"

    executer(scenario())


def test_commande_avancer_forcer_acceptee_et_dite(capsys, tmp_path):
    assert cli.main(["avancer", "0.5", "--forcer", "--json", "--donnees", str(tmp_path)]) == 0  # simulation
    assert "avancer 0.5 --forcer" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["tourner", "15", "--forcer", "--json", "--donnees", str(tmp_path)])
