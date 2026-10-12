"""Ordre « arc » : avancer ou reculer en tournant, d'UNE consigne tenue (vitesse linéaire ET angulaire).

Demande du propriétaire (2026-10-12) : des flèches diagonales, « les deux chaînes dans le même sens mais à
vitesse différente, pour un virage en douceur ». La voie (0,45 m) est une estimation, à calibrer.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import struct
import subprocess

import pytest
from conftest import est_consigne, fragment
from test_demo import serveur_demo
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import cli, fleches, mouvement, protocole, securite, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.mouvement import ErreurSaisie, Ordre
from lymow_pilotage.securite import RefusSecurite

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
VOIE = 0.45


def js(expression: str, *arguments):
    script = (
        "const M = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(ui.STATIQUE / "fleches.js"), json.dumps(arguments)],
        capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
    )
    return json.loads(sortie.stdout)


def chenilles(consigne) -> tuple[float, float]:
    """Vitesses des deux chenilles (intérieure, extérieure), en valeur absolue."""
    v, w = abs(consigne.lineaire), abs(consigne.angulaire)
    return v - w * VOIE / 2, v + w * VOIE / 2


# --- calcul ------------------------------------------------------------------------------------------


def test_arc_calcul_a_la_main_un_metre_quarante_cinq_degres():
    # 1 m et 45° (0,785398 rad) sur la même durée. Chenille extérieure : v·(1 + 0,785398 × 0,225) ≤ 0,25 m/s
    # → v = 0,25 / 1,176715 = 0,212456 m/s ; durée = 1 / v = 4,70686 s ; w = 0,785398 / 4,70686 = 0,166862 rad/s.
    consigne = securite.consigne_arc(1.0, 45.0)
    assert consigne.lineaire == pytest.approx(0.212456, abs=1e-6) and consigne.angulaire == pytest.approx(0.166862, abs=1e-6)
    assert consigne.duree == pytest.approx(4.70686, abs=1e-5) and (consigne.attendu, consigne.valeur, consigne.angle) == ("arc", 1.0, 45.0)
    assert consigne.annonce is None  # allure calculée par l'outil : rien à annoncer
    assert securite.consigne_arc(1.0, 45.0, vitesse=0.25).annonce == "allure réduite à 0.21 m/s pour tenir les plafonds de vitesse"
    # La trame porte les DEUX champs, non nuls — comparée octet pour octet à cette référence.
    reference = bytes.fromhex("10 31 52 0a 0d") + struct.pack("<f", 0.2124559369043065) + b"\x15" + struct.pack("<f", 0.1668625026475265)
    trame = protocole.trame_consigne(consigne.lineaire, consigne.angulaire, Calibration())
    assert trame.octets == reference == bytes.fromhex("10 31 52 0a 0d 0d 8e 59 3e 15 01 de 2a 3e")
    assert est_consigne(trame.octets) and trame.categorie is protocole.Categorie.MOUVEMENT


@pytest.mark.parametrize("metres", [0.25, 0.5, 1.0, 2.0, -0.25, -1.0])
@pytest.mark.parametrize("degres", [15.0, 45.0, 90.0, -15.0, -90.0, 180.0])
def test_arc_plafonds_tenus_et_chenille_interieure_jamais_arretee_ni_inversee(metres, degres):
    try:
        consigne = securite.consigne_arc(metres, degres, voie_m=VOIE)
    except RefusSecurite as refus:
        assert "arc refusé" in str(refus) and "plus petit" in str(refus)  # refus clair, avec quoi changer
        return
    interieure, exterieure = chenilles(consigne)
    v = abs(consigne.lineaire)
    assert securite.VITESSE_LINEAIRE_MIN_M_S - 1e-9 <= v <= securite.VITESSE_LINEAIRE_M_S + 1e-9
    assert abs(consigne.angulaire) <= securite.VITESSE_ANGULAIRE_RAD_S + 1e-9
    assert exterieure <= securite.VITESSE_LINEAIRE_M_S + 1e-9  # pas plus vite qu'en ligne droite
    assert interieure >= securite.PART_CHENILLE_INTERIEURE_MIN * v - 1e-9 and interieure > 0  # même sens, jamais à l'arrêt
    assert consigne.duree <= securite.DUREE_MAX_S and v * consigne.duree == pytest.approx(abs(metres))  # SEC-02
    assert math.degrees(abs(consigne.angulaire) * consigne.duree) == pytest.approx(abs(consigne.angle), abs=1e-6)
    assert abs(consigne.angle) <= abs(degres) + 1e-9 and math.copysign(1, consigne.lineaire) == math.copysign(1, metres)
    securite.verifier_vitesses(consigne.lineaire, consigne.angulaire)  # dernière barrière de l'encodeur


def test_arc_virage_adouci_annonce_plutot_que_de_pivoter_sur_place():
    serre = securite.consigne_arc(0.25, 90.0)  # 90° sur 25 cm : la chenille intérieure s'inverserait
    assert serre.angle == pytest.approx(50.93, abs=0.01)
    assert serre.annonce == "virage adouci : 51° au lieu de 90° (les deux chenilles restent dans le même sens)"
    interieure, _exterieure = chenilles(serre)
    assert interieure == pytest.approx(0.2 * abs(serre.lineaire))  # juste à la limite : 20 % de la vitesse de l'ordre
    assert securite.consigne_arc(1.0, 15.0).annonce is None  # virage doux à l'allure demandée : rien à annoncer
    assert securite.consigne_arc(0.5, 15.0, vitesse=0.1).lineaire == pytest.approx(0.1)
    assert fleches.ordre("haut-gauche", 1.0, "lente", False, 1.0, 15.0).vitesse == 0.12  # lente : tient dans les 9 s
    mouvement.consigne_de(fleches.ordre("haut-gauche", 2.0, "lente", False, 1.0, 15.0), Calibration())
    # Une voie plus grande adoucit davantage ; les facteurs de calibration s'appliquent comme aux ordres simples.
    assert abs(securite.consigne_arc(0.25, 90.0, voie_m=0.6).angle) < abs(serre.angle)
    calibre = securite.consigne_arc(1.0, 45.0, facteur_angle=0.79)
    assert math.degrees(abs(calibre.angulaire) * calibre.duree) == pytest.approx(45.0 / 0.79, abs=1e-6)


@pytest.mark.parametrize("metres, degres", [(0, 45), (2.5, 45), (1, 0), (1, 181), (float("nan"), 45), (1, float("inf")), (-3, 10)])
def test_arc_sec02_bornes(metres, degres):
    with pytest.raises(RefusSecurite, match="SEC-02|invalide"):
        securite.consigne_arc(metres, degres)


def test_arc_trop_long_refuse_avec_ce_qu_il_faut_changer():
    with pytest.raises(RefusSecurite, match=r"arc refusé : il durerait 9\.4 s, plus que les 9 s permises par ordre — choisissez une distance plus courte"):
        securite.consigne_arc(2.0, 90.0)
    with pytest.raises(RefusSecurite, match="facteur de calibration"):
        securite.consigne_arc(1.5, 45.0, facteur_distance=0.5)  # 3 m commandés
    with pytest.raises(RefusSecurite):
        mouvement.consigne_de(Ordre("arc", 1.0, duree=3.0, angle=45.0), Calibration())  # pas de --duree pour un arc
    with pytest.raises(RefusSecurite):
        mouvement.consigne_de(Ordre("avancer", 1.0, angle=45.0), Calibration())  # un angle ne vaut que pour un arc


# --- sens des quatre diagonales, par la trajectoire --------------------------------------------------------


def trajectoire(consigne) -> tuple[float, float, float]:
    """Intègre la consigne à 10 Hz depuis l'origine, cap vers l'est : (est, nord, cap en degrés)."""
    x = y = cap = 0.0
    pas = 0.1
    for _ in range(round(consigne.duree / pas)):
        cap += consigne.angulaire * pas / 2
        x += consigne.lineaire * math.cos(cap) * pas
        y += consigne.lineaire * math.sin(cap) * pas
        cap += consigne.angulaire * pas / 2
    return x, y, math.degrees(cap)


@pytest.mark.parametrize(
    "direction, avant, a_gauche, cap_a_gauche",
    [
        ("haut-gauche", True, True, True),   # ↖ avance en virant à gauche
        ("haut-droite", True, False, False),  # ↗ avance en virant à droite
        ("bas-gauche", False, True, False),   # ↙ recule, l'arrière part à gauche : comme une voiture, le cap tourne à droite
        ("bas-droite", False, False, True),   # ↘ recule, l'arrière part à droite : le cap tourne à gauche
    ],
)
def test_sens_des_quatre_diagonales_par_la_trajectoire_calculee(direction, avant, a_gauche, cap_a_gauche):
    ordre = fleches.ordre(direction, 1.0, angle=45.0)
    assert ordre.nom == "arc" and abs(ordre.valeur) == 1.0 and abs(ordre.angle) == 45.0
    x, y, cap = trajectoire(mouvement.consigne_de(ordre, Calibration()))
    assert (x > 0.8) is avant and (x < -0.8) is (not avant)  # le robot fait face à l'est : x = vers l'avant
    assert (y > 0.3) is a_gauche and (y < -0.3) is (not a_gauche)  # y = vers la gauche du robot
    assert (cap > 40) is cap_a_gauche and (cap < -40) is (not cap_a_gauche)
    # Le point d'arrivée calculé (avertissement de carte) est celui de la trajectoire.
    devant, gauche = securite.arrivee_arc(ordre.valeur, ordre.angle)
    assert (devant, gauche) == (pytest.approx(x, abs=0.03), pytest.approx(y, abs=0.03))
    if NODE is not None:  # la page annonce la même flèche pour cet ordre
        etape = js("M.ordreDe(a[0], 1, 45)", direction)
        assert (etape["nom"], etape["valeur"], etape["angle"]) == ("arc", ordre.valeur, ordre.angle)
        assert js("M.directionDe(a[0])", etape) == direction


# --- flèches, séquence, commande ---------------------------------------------------------------------------


def test_fleches_diagonales_bornees_aux_listes_et_sequence():
    assert fleches.DIAGONALES == {"haut-gauche": (1.0, 1.0), "haut-droite": (1.0, -1.0), "bas-gauche": (-1.0, -1.0), "bas-droite": (-1.0, 1.0)}
    assert fleches.ordre("haut-droite", 0.5, "lente", False, 1.0, 15.0) == Ordre("arc", 0.5, vitesse=0.10, angle=-15.0)
    for pas, angle in ((3, 45.0), (1.0, 30.0), (1.0, None), ("1", 45.0), (1.0, True)):
        with pytest.raises(ErreurSaisie):
            fleches.ordre("haut-gauche", pas, angle=angle)
    with pytest.raises(ErreurSaisie):
        fleches.ordre("haut", 1.0, angle=45.0)  # un angle ne vaut que pour une diagonale
    with pytest.raises(ErreurSaisie):
        fleches.ordre("haut-gauche", 1.0, "auto", True, 1.0, 45.0)  # pas de « forcer » pour un arc
    assert "arc" in mouvement.MOUVEMENTS and "arc" in mouvement.ETAPES_SEQUENCE
    assert mouvement.analyser_sequence("arc 1 45; arc -0.5 -15 --vitesse 0.1; tourner 15") == [
        Ordre("arc", 1.0, angle=45.0), Ordre("arc", -0.5, vitesse=0.1, angle=-15.0), Ordre("tourner", 15.0),
    ]
    assert str(Ordre("arc", -0.5, vitesse=0.1, angle=-15.0)) == "arc -0.5 -15 --vitesse 0.1"
    for faux in ("arc 1", "arc 1 45 10", "arc 1 45 --duree 2 --reel", "arc x 45"):
        with pytest.raises(ErreurSaisie):
            mouvement.analyser_sequence(faux)
    assert ui.ordres_depuis({"ordres": [{"nom": "arc", "valeur": 1, "angle": -45}]}) == [Ordre("arc", 1.0, angle=-45.0)]
    with pytest.raises(ErreurSaisie):
        ui.ordres_depuis({"ordres": [{"nom": "avancer", "valeur": 1, "angle": 45}]})


def test_commande_arc_simulation_et_refus(capsys, tmp_path):
    assert cli.main(["arc", "1", "45", "--json", "--donnees", str(tmp_path)]) == 0  # simulation par défaut
    etape = json.loads(capsys.readouterr().out)
    assert etape["ordre"] == "arc 1 45" and etape["vitesse_m_s"] == 0.212 and etape["vitesse_rad_s"] == 0.167
    assert etape["deplacement_commande_m"] == 1.0 and etape["deplacement_commande_deg"] == 45.0 and etape["emissions_prevues"] == 48
    assert etape["trame"]["hexadecimal"] == "10 31 52 0a 0d 0d 8e 59 3e 15 01 de 2a 3e"
    assert cli.main(["arc", "-1", "45", "--json", "--donnees", str(tmp_path)]) == 0  # mètres négatifs : recul
    assert json.loads(capsys.readouterr().out)["trame"]["champs"]["vitesse_lineaire_m_s"] < 0
    assert cli.main(["arc", "0.25", "90", "--json", "--donnees", str(tmp_path)]) == 0
    assert "virage adouci : 51° au lieu de 90°" in json.loads(capsys.readouterr().out)["arc"]
    assert cli.main(["arc", "2", "90", "--json", "--donnees", str(tmp_path)]) != 0  # refusé avant toute émission
    assert "arc refusé" in capsys.readouterr().out
    assert cli.main(["sequence", "arc 1 45; arc -1 -45", "--json", "--donnees", str(tmp_path)]) == 0
    assert len(json.loads(capsys.readouterr().out)["etapes"]) == 2


# --- émission réelle (faux lien), constat, avertissement de carte ------------------------------------------


def test_arc_reel_une_consigne_tenue_arret_garanti_et_constat_distance_et_rotation(session, lien):
    from test_carte import contenu

    from lymow_pilotage import carte as cartes

    session.carte_reference = cartes.decoder(contenu())
    lien.robot = fragment(statut=3, tache=2, est=2.0, nord=1.0, theta=0.0, precision=0.02)
    consigne = mouvement.consigne_de(Ordre("arc", 1.0, angle=45.0), session.cal)
    devant, gauche = securite.arrivee_arc(1.0, 45.0)

    def arrive(lien_, octets):  # le faux robot décrit l'arc : position ET cap changent
        if est_consigne(octets) and len(lien_.consignes) == 40:
            lien_.robot = fragment(statut=6, tache=2, est=2.0 + devant, nord=1.0 + gauche, theta=math.radians(45.0), precision=0.02)

    lien.sur_publication = arrive
    etape = asyncio.run(mouvement.executer_sequence(session, [Ordre("arc", 1.0, angle=45.0)]))["etapes"][0]
    assert etape["ordre"] == "arc 1 45" and etape["emissions"] == 48  # 4,71 s à 10 Hz
    assert set(lien.consignes) == {protocole.trame_consigne(consigne.lineaire, consigne.angulaire, session.cal).octets}  # UNE consigne, tenue
    assert len(lien.arrets) == 3  # SEC-03
    assert etape["verification"] == mouvement.VERIFIEE and etape["rotation_deg"] == 45.0 and etape["rotation_attendue_deg"] == 45.0
    assert etape["deplacement_m"] == pytest.approx(etape["corde_attendue_m"], abs=0.01) == pytest.approx(0.974, abs=0.01)
    assert "arc" not in etape  # rien n'a dû être adouci ni réduit par rapport à une demande
    # Avertissement de carte sur le point d'arrivée de l'arc — jamais un refus, même case cochée.
    assert etape["garde_carte"].startswith("d'après la carte : bord de zone vers l'arrivée de l'arc à ") and etape["garde_carte_alerte"] is False
    session.arret_aux_bords = True
    lien.sur_publication = None
    lien.robot = fragment(statut=3, tache=2, est=9.62, nord=5.0, theta=0.0, precision=0.02)  # à 0,35 m du bord
    proche = asyncio.run(mouvement.executer_sequence(session, [Ordre("arc", 1.0, angle=15.0)]))["etapes"][0]
    assert "echec" not in proche and proche["garde_carte_alerte"] is True and proche["emissions"] > 0


def test_arc_constat_immobile_mauvais_sens_et_statut_non_pilotable(session, lien):
    lien.robot = fragment(statut=3, tache=2, est=2.0, nord=1.0, precision=0.02)
    immobile = asyncio.run(mouvement.executer_sequence(session, [Ordre("arc", 0.5, angle=15.0)]))["etapes"][0]
    assert immobile["constat"] == mouvement.IMMOBILE and immobile["cause_probable"] == "patinage possible"
    assert mouvement._juger_arc(
        mouvement.Pose(0, 0, 0), mouvement.Pose(0.9, -0.37, -45.0), 1.0, 45.0)[1].startswith("rotation dans le mauvais sens")
    assert mouvement._juger_arc(mouvement.Pose(0, 0, 0), mouvement.Pose(-0.9, 0.37, 45.0), 1.0, 45.0)[1].startswith("déplacement dans le mauvais sens")
    assert mouvement._juger_arc(mouvement.Pose(0, 0, 0), mouvement.Pose(0.9, 0.37, 45.0), 1.0, 45.0)[1] is None
    lien.robot = fragment(statut=2, tache=2)  # tonte en cours : SEC-01, comme tout mouvement
    refuse = asyncio.run(mouvement.executer_sequence(session, [Ordre("arc", 0.5, angle=15.0)]))
    assert "SEC-01" in refuse["etapes"][0]["echec"]


def test_arc_en_pilotage_direct_et_en_file(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = {"robotState": 3, "workStatus": 2, "rtkEastM": 0.03}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            statut, corps, _recus = await poster(
                http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "haut-droite", "pas": 0.25, "angle": 15}
            )
            assert statut == 202
            statut, corps, _recus = await poster(  # empilé pendant le premier : la file garde l'arc tel quel
                http, serveur, "/api/direct/fleche", {"jeton": corps["jeton"], "direction": "bas-gauche", "pas": 0.25, "angle": 15}
            )
            assert statut == 202 and corps["file"] == ["arc -0.25 -15"]
            await attendre(lambda: not serveur.occupe and len(serveur.direct.faites) == 2, delai=40.0)
            assert [f["rapport"]["ordre"] for f in serveur.direct.faites] == ["arc 0.25 -15", "arc -0.25 -15"]
            assert serveur.direct.arme is True
            # Refus de validation : dit, sans désarmer.
            statut, _corps, _recus = await poster(
                http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "haut-gauche", "pas": 0.25, "angle": 60}
            )
            assert statut == 400 and serveur.direct.arme is True

    executer(scenario())


def test_robot_fictif_decrit_un_arc_position_et_cap_changent(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            robot = serveur.session.lien
            depart = (robot.est, robot.nord, robot.cap)
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            statut, _corps, _recus = await poster(
                http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "haut-gauche", "pas": 0.5, "angle": 45}
            )
            assert statut == 202
            await attendre(lambda: not serveur.occupe and len(serveur.direct.faites) == 1, delai=40.0)
            rapport = serveur.direct.faites[-1]["rapport"]
            assert math.hypot(robot.est - depart[0], robot.nord - depart[1]) > 0.35  # il a avancé…
            assert math.degrees(robot.cap - depart[2]) == pytest.approx(45.0, abs=6.0)  # … en tournant à gauche
            assert rapport["verification"] == mouvement.VERIFIEE and "echec" not in rapport

    executer(scenario())


# --- page : symboles, tracé en arc, combinaisons de touches ------------------------------------------------


@sans_node
def test_page_pastille_trace_en_arc_et_totaux():
    arc = {"nom": "arc", "valeur": 1, "angle": -45, "vitesse": None, "duree": None}
    assert js("M.symbole(a[0])", arc) == "↗ 1 m / 45°"
    assert js("M.symbole(a[0])", {"nom": "arc", "valeur": -0.5, "angle": -15}) == "↙ 0.5 m / 15°"
    trace = js("M.trace(a[0], {x: 0, y: 0, cap: 0}, {lineaire: 0.25, angulaire: 0.35})", [arc])
    fin = trace["points"][-1]
    devant, gauche = securite.arrivee_arc(1.0, -45.0)
    assert (fin["x"], fin["y"], fin["cap"]) == (pytest.approx(devant, abs=0.01), pytest.approx(gauche, abs=0.01), pytest.approx(-45.0))
    assert len(fin["courbe"]) == 11 and len(trace["points"]) == 2  # tracé courbe, une étape = un point
    assert (trace["distance_m"], trace["rotation_deg"]) == (1, 45)
    pave = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    assert "points[i].courbe || []" in pave  # la miniature suit la courbe


@sans_node
def test_page_combinaisons_de_touches_sans_pas_droit_avant_la_diagonale():
    vide = {"attente": None, "depuis": 0, "tenues": []}
    assert js("M.diagonale('haut', 'gauche')") == "haut-gauche" and js("M.diagonale('droite', 'bas')") == "bas-droite"
    assert js("M.diagonale('haut', 'bas')") is None and js("M.diagonale('gauche', 'gauche')") is None
    # Une flèche seule : rien ne part tout de suite, puis elle part à l'échéance (120 ms).
    seule = js("M.combiner(a[0], a[1])", vide, {"type": "appui", "direction": "haut", "t": 1000})
    assert seule["emettre"] == [] and seule["attendreMs"] == 120 and seule["etat"]["attente"] == "haut"
    assert js("M.combiner(a[0], a[1])", seule["etat"], {"type": "echeance", "t": 1120})["emettre"] == ["haut"]
    # ↑ puis ← dans les 150 ms : la diagonale, et AUCUN pas droit.
    ensemble = js("M.combiner(a[0], a[1])", seule["etat"], {"type": "appui", "direction": "gauche", "t": 1100})
    assert ensemble["emettre"] == ["haut-gauche"] and ensemble["etat"]["attente"] is None and ensemble["attendreMs"] == 0
    # Trop tard pour combiner avec l'attente, mais ↑ est encore tenue : la diagonale quand même.
    parti = js("M.combiner(a[0], a[1])", seule["etat"], {"type": "echeance", "t": 1120})
    tenue = js("M.combiner(a[0], a[1])", parti["etat"], {"type": "appui", "direction": "droite", "t": 1900})
    assert tenue["emettre"] == ["haut-droite"]
    # ↑ relâchée, puis ← : un pas droit, pas une diagonale.
    relache = js("M.combiner(a[0], a[1])", parti["etat"], {"type": "relache", "direction": "haut", "t": 1500})
    apres = js("M.combiner(a[0], a[1])", relache["etat"], {"type": "appui", "direction": "gauche", "t": 1900})
    assert apres["emettre"] == [] and apres["etat"]["attente"] == "gauche"
    # Deux flèches opposées ne se combinent pas : la première part, la seconde attend son tour.
    opposees = js("M.combiner(a[0], a[1])", seule["etat"], {"type": "appui", "direction": "bas", "t": 1050})
    assert opposees["emettre"] == ["haut"] and opposees["etat"]["attente"] == "bas"
    # Pavé numérique 7-9-1-3 : les diagonales d'une touche ; `repeat` toujours ignoré ; 1 à 4 du haut du clavier inchangés.
    touche = lambda code, **e: js("M.interception(a[0])", {"key": code[-1], "code": code, "actives": True, "balise": "BODY"} | e)  # noqa: E731
    assert [touche(c)["action"]["direction"] for c in ("Numpad7", "Numpad9", "Numpad1", "Numpad3")] == [
        "haut-gauche", "haut-droite", "bas-gauche", "bas-droite",
    ]
    assert touche("Numpad7", repeat=True) == {"intercepter": True, "action": None}
    assert touche("Digit1")["intercepter"] is False and touche("Numpad7", actives=False)["intercepter"] is False
    ordres = (ui.STATIQUE / "ordres.js")
    script = "const M = require(process.argv[1]); process.stdout.write(JSON.stringify([M.raccourciDistance({key:'1',code:'Digit1',actives:true,nombre:4}), M.raccourciDistance({key:'1',code:'Numpad1',actives:true,nombre:4})]));"
    sortie = subprocess.run([NODE, "-e", script, str(ordres)], capture_output=True, text=True, timeout=20, check=True, encoding="utf-8")
    assert json.loads(sortie.stdout) == [0, None]


def test_page_deux_paves_trois_par_trois_avec_raccourcis_en_info_bulle():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    for conteneur in ('<div class="croix"', '<div class="pave"'):
        bloc = page[page.index(conteneur) :]
        bloc = bloc[: bloc.index("</div>")]
        import re

        directions = re.findall(r'data-fleche="([\w-]+)"', bloc)
        assert directions == ["haut-gauche", "haut", "haut-droite", "gauche", "droite", "bas-gauche", "bas", "bas-droite"]
        for raccourci in ("pavé numérique 7, ou ↑ et ← ensemble", "pavé numérique 9", "pavé numérique 1", "pavé numérique 3, ou ↓ et → ensemble"):
            assert raccourci in bloc
        assert "Reculer, l'arrière partant à gauche" in bloc
    assert 'id="stop-centre"' in page[page.index('<div class="croix"') :][:1500]
    style = (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
    assert ".fleche.haut-gauche { grid-column: 1; grid-row: 1; }" in style and "grid-template-rows: repeat(3, 2.9rem)" in style
    assert Calibration().voie_m == 0.45  # estimation, à calibrer (calibration.toml)
