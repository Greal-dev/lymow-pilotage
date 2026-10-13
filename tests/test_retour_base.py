"""« Retour base » selon la tâche, et constat honnête des ordres de tâche.

Constat réel du 2026-10-13 (09:10-09:11) : « base » émis trois fois (ordre 33, tâche conservée) alors que le robot
n'avait aucune tâche — statut resté « prêt », et pourtant « émise, vérifiée ». Rejoué ici hors ligne.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from conftest import fragment
from test_demo import serveur_demo
from test_mouvement import lancer
from test_ui import attendre, executer, poster

from lymow_pilotage import demo, mouvement, protocole, ui
from lymow_pilotage.etat import LIBELLES_STATUT
from lymow_pilotage.mouvement import Ordre

NODE = shutil.which("node")
DOCK, RECHARGE, SORTIE_TELECOMMANDE = (bytes.fromhex(h) for h in ("10 31 28 02", "10 31 28 21", "10 31 28 20"))
PAUSE, REPRISE, ANNULATION = (bytes.fromhex(h) for h in ("10 31 28 03", "10 31 28 04", "10 31 28 1c"))


def robot_devient(code: bytes, etat: dict):
    def sur_publication(lien, octets):
        if octets == code:
            lien.robot = etat

    return sur_publication


@pytest.mark.parametrize(
    ("tache", "statut", "code"),
    [(1, 1, 2), (0, 5, 2), (None, 1, 2), (None, None, 2), (1, 6, 2),  # aucune tâche : ordre 2
     (2, 3, 33), (3, 3, 33), (9, 2, 33), (2, 6, 33), (4, 10, 33), (None, 2, 33)],  # tâche en cours ou en pause : 33
)
def test_retour_base_choisit_l_ordre_selon_la_tache(tache, statut, code):
    assert mouvement.code_base(tache, statut) == code
    assert protocole.USER_CTRL_AUTORISES[2] == "retour à la base"
    assert protocole.USER_CTRL_AUTORISES[33] == "retour à la base, tâche conservée"


def test_retour_base_sans_effet_n_est_plus_dit_verifie(session, lien, horloge):
    debut = horloge()
    rapport = lancer(mouvement.executer_tache(session, "base"))  # le faux robot reste « prêt »
    assert lien.ordres == [DOCK] and rapport["codes_emis"] == [2]  # aucune tâche : plus jamais le 33 sans effet
    assert rapport["verifie"] is False and rapport["verification"] == mouvement.ANOMALIE
    assert rapport["constat"] == f"retour à la base : ordre émis, mais le robot n'a pas changé d'état : {LIBELLES_STATUT[1]}"
    assert 10.0 <= horloge() - debut < 16.0  # guetté dix secondes, pas davantage


def test_retour_base_avec_tache_en_pause_emet_le_33_et_constate_le_retour(session, lien):
    lien.robot = fragment(statut=3, tache=2)
    lien.sur_publication = robot_devient(RECHARGE, fragment(statut=4, tache=2))
    bilan = lancer(mouvement.executer_sequence(session, [Ordre("base")]))
    rapport = bilan["etapes"][0]
    assert lien.ordres == [RECHARGE] and rapport["verification"] == mouvement.VERIFIEE and rapport["verifie"] is True
    assert rapport["constat"] == f"retour à la base, tâche conservée : le robot annonce « {LIBELLES_STATUT[4]} »"
    assert bilan["verdict"] == rapport["constat"] and rapport["issue"] == "émise, vérifiée"


@pytest.mark.parametrize(
    ("nom", "depart", "code", "arrivee", "atteint"),
    [("pause", 2, PAUSE, 3, True), ("pause", 2, PAUSE, 2, False), ("reprendre", 3, REPRISE, 8, True),
     ("reprendre", 3, REPRISE, 3, False), ("annuler", 3, ANNULATION, 1, True), ("annuler", 7, ANNULATION, 7, False),
     ("base", 1, DOCK, 4, True)],
)
def test_constat_des_ordres_de_tache_jamais_verifie_sans_changement(session, lien, nom, depart, code, arrivee, atteint):
    lien.robot = fragment(statut=depart)
    lien.sur_publication = robot_devient(code, fragment(statut=arrivee))
    rapport = lancer(mouvement.executer_tache(session, nom))
    assert lien.ordres == [code]
    assert rapport["verification"] == (mouvement.VERIFIEE if atteint else mouvement.ANOMALIE) and rapport["verifie"] is atteint
    assert ("n'a pas changé d'état : " + LIBELLES_STATUT[arrivee] in rapport["constat"]) is not atteint


@pytest.mark.parametrize(("tache", "ordre"), [(1, DOCK), (2, RECHARGE)])
def test_retour_base_en_telecommande_sort_d_abord_de_la_telecommande(session, lien, tache, ordre):
    lien.robot = fragment(statut=6, tache=tache)
    lien.sur_publication = robot_devient(ordre, fragment(statut=4, tache=tache))
    rapport = lancer(mouvement.executer_tache(session, "base"))
    assert lien.ordres == [SORTIE_TELECOMMANDE, ordre] and rapport["verifie"] is True


def test_robot_fictif_ignore_le_33_sans_tache_et_rentre_sur_l_ordre_2():
    async def scenario():
        robot = demo.RobotFictif()
        robot.session = type("S", (), {"recevoir_etat": lambda self, etat: None})()
        for octets, attendu in ((RECHARGE, 1), (DOCK, 4)):
            await robot.publier("sujet", json.dumps({"message": __import__("base64").b64encode(octets).decode()}), delai=1.0)
            assert robot.statut == attendu

    asyncio.run(scenario())


def test_demonstration_le_bouton_retour_base_envoie_l_ordre_2_et_le_fil_dit_ce_que_fait_le_robot(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            robot = serveur.session.lien
            await attendre(lambda: serveur.session.etat.statut == 1, delai=10.0)
            assert serveur.instantane()["ordres"]["retour_base"] == "retour à la base"
            statut, _reponse, _recus = await poster(http, serveur, "/api/tache", {"nom": "base", "confirmer": True})
            assert statut == 202
            await attendre(lambda: not serveur.occupe and serveur.resultat is not None, delai=30.0)
            assert DOCK in robot.recues and RECHARGE not in robot.recues
            assert serveur.resultat["verdict"].startswith("retour à la base : le robot annonce « ")
            assert any(note["texte"].startswith("ordre terminé : retour à la base : le robot annonce") for note in serveur.fil)

    executer(scenario())


def test_page_info_bulle_du_retour_base_et_fil_orange_quand_le_robot_n_a_pas_change_d_etat():
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert '$("base-bouton").title = Ordres.bulleRetourBase(image.ordres.retour_base);' in app
    if NODE is None:
        pytest.skip("node absent")
    script = (
        "const M = require(process.argv[1]); process.stdout.write(JSON.stringify(["
        "M.bulleRetourBase('retour à la base'), M.bulleRetourBase('retour à la base, tâche conservée'),"
        "M.graviteDe(\"ordre terminé : retour à la base : ordre émis, mais le robot n'a pas changé d'état : prêt\"),"
        "M.graviteDe('ordre terminé : retour à la base : le robot annonce « retour à la base »')]));"
    )
    sortie = subprocess.run([NODE, "-e", script, str(ui.STATIQUE / "ordres.js")], capture_output=True, text=True, timeout=20, check=True, encoding="utf-8")
    assert json.loads(sortie.stdout) == [
        "Émet, tout de suite : retour à la base (aucune tâche en cours).",
        "Émet, tout de suite : retour à la base, tâche conservée.", "alerte", "info",
    ]
