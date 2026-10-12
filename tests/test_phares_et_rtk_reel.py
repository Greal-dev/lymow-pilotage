"""Phares du robot (signaux 10 et 11, attestés par capture dans le socle) et réponses RÉELLES de diagnostic RTK.

Les sous-messages RTK ci-dessous sont un échantillon des 169 réponses reçues le 2026-10-12 (20:41-20:53) :
ils ne contiennent AUCUNE coordonnée — c'est justement le constat : le robot n'y transmet pas sa position
brute, et l'écart « position satellite / position calculée » ne peut donc pas se calculer.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from conftest import est_consigne, fragment
from test_demo import serveur_demo
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import cli, mouvement, protocole, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.etat import Etat
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.protocole import est_requete_pure
from lymow_pilotage.vendor.ha_lymow import protocol as pb
from lymow_pilotage.vendor.ha_lymow.protocol import decode_pboutput

NODE = shutil.which("node")
ALLUMER, ETEINDRE = bytes.fromhex("10 31 6a 02 40 0a"), bytes.fromhex("10 31 6a 02 40 0b")
SANS_POSE = {"robotState": 3, "workStatus": 2, "rtkEastM": 0.03}
# Réponses réelles, sous-messages 35 (niveau 1) et 36 (niveau 2) seuls.
REELLES_L1 = (
    "08011507f0d63d181c201d280b301038234021482050005d00000000",
    "080115ed0d3e3d1818201c280c300b38234020482150005d00000000",
    "0801156a4df33c1819201d280c300b38244020482150005d00000000",
)
REELLES_L2 = (
    "0d0000004010e90118d20320ed062db072683f35c1ca813f3db072e83f400948065000583160316835",
    "0d0000004010ee0118d80320bb072db072683f352506813f3db072e83f400948075000583b60316835",
    "0d0000004010e40118bd0320d5062d79e9663f35c1ca813f3db072e83f400e48085005583a60316834",
)


# --- phares --------------------------------------------------------------------------------------------


def test_phares_octets_attestes_et_rien_d_autre():
    assert protocole.trame_phares(True, Calibration()).octets == ALLUMER  # capture du socle : 10 31 6a 02 40 0a
    assert protocole.trame_phares(False, Calibration()).octets == ETEINDRE
    assert protocole.SIGNAUX_PHARES == {True: 10, False: 11} and not est_requete_pure(ALLUMER)
    for autre in (1, 0, "allumer", None):
        with pytest.raises(ValueError):
            protocole.trame_phares(autre, Calibration())
    assert mouvement.texte_phares(True, False) == "allumés (d'après la configuration du robot)"
    assert mouvement.texte_phares(None, True) == "dernier ordre envoyé : allumés (état réel non transmis)"
    assert mouvement.texte_phares(None, None) == "état inconnu (non transmis)"


def test_etat_des_phares_lu_dans_la_configuration_du_robot():
    # Configuration réelle (2026-10-12) : champ 7 absent — un booléen faux n'est pas transmis : phares éteints.
    sans = decode_pboutput(pb._field_bytes(17, pb._field_i32(6, 80) + pb._field_i32(9, 1)))
    avec = decode_pboutput(pb._field_bytes(17, pb._field_i32(6, 80) + pb._field_i32(7, 1)))
    assert sans["isOpenLed"] is False and avec["isOpenLed"] is True
    etat = Etat()
    assert etat.phares is None  # aucune configuration reçue : inconnu
    etat.appliquer(avec, 1.0)
    assert etat.phares is True and etat.resume(1.0)["phares"] is True
    assert "isOpenLed" not in decode_pboutput(pb._field_bytes(5, pb._field_i32(1, 3)))


def test_phares_un_clic_sans_previsualisation_journalise_et_refuse_main_rendue(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(SANS_POSE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            assert serveur.instantane()["phares"]["texte"] == "Phares : état inconnu (non transmis)"
            for corps in ({}, {"allumer": "oui"}, {"allumer": True, "signal": 28}):
                statut, _reponse, _recus = await poster(http, serveur, "/api/phares", corps)
                assert statut == 400 and lien.ordres == []
            statut, reponse, _recus = await poster(http, serveur, "/api/phares", {"allumer": True})
            assert statut == 202 and reponse["en_attente"] is False
            await attendre(lambda: not serveur.occupe, delai=10.0)
            assert lien.ordres == [ALLUMER]
            phares = serveur.instantane()["phares"]
            assert phares["allumes"] is True and phares["texte"] == "Phares : dernier ordre envoyé : allumés (état réel non transmis)"
            # Le robot annonce ensuite sa configuration : c'est elle qui est affichée.
            lien.robot = dict(SANS_POSE) | {"isOpenLed": True}
            await poster(http, serveur, "/api/actualiser")
            assert serveur.instantane()["phares"] == {
                "allumes": True, "transmis": True, "en_attente": None, "texte": "Phares : allumés (d'après la configuration du robot)",
            }
            await poster(http, serveur, "/api/phares", {"allumer": False})
            await attendre(lambda: not serveur.occupe, delai=10.0)
            assert lien.ordres == [ALLUMER, ETEINDRE]
            journal = [json.loads(ligne) for f in tmp_path.glob("journaux/*.jsonl") for ligne in f.read_text(encoding="utf-8").splitlines()]
            assert [ligne["ordre"] for ligne in journal if ligne.get("evenement") == "ordre-emis"] == ["phares : allumer", "phares : éteindre"]
            # Main rendue : refusé.
            await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            statut, reponse, _recus = await poster(http, serveur, "/api/phares", {"allumer": True})
            assert statut == 409 and "main" in reponse["erreur"] and lien.ordres == [ALLUMER, ETEINDRE]

    executer(scenario())


def test_phares_pendant_un_pas_mis_en_attente_sans_retarder_les_consignes_ni_le_stop(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(SANS_POSE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            await poster(http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "haut", "pas": 0.5})
            await attendre(lambda: len(lien.consignes) >= 3)
            statut, reponse, _recus = await poster(http, serveur, "/api/phares", {"allumer": True})
            assert statut == 202 and reponse["en_attente"] is True
            assert "allumage en attente de la fin du pas" in serveur.instantane()["phares"]["texte"]
            await attendre(lambda: ALLUMER in lien.trames, delai=20.0)
            trames = lien.trames
            rang = trames.index(ALLUMER)
            # Les consignes du pas sont toutes parties AVANT l'ordre des phares, à leur cadence : aucune après lui.
            assert sum(est_consigne(t) for t in trames[:rang]) == 20 and not any(est_consigne(t) for t in trames[rang:])
            await attendre(lambda: not serveur.occupe, delai=20.0)
            assert serveur.direct.arme is True and serveur.instantane()["phares"]["en_attente"] is None

            # STOP annule une demande en attente : rien ne part après lui.
            await poster(http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "haut", "pas": 2})
            await attendre(lambda: serveur.occupe)
            await poster(http, serveur, "/api/phares", {"allumer": False})
            lien.publies.clear()
            await poster(http, serveur, "/api/stop")
            await attendre(lambda: not serveur.occupe, delai=20.0)
            await asyncio.sleep(0.3)
            assert ETEINDRE not in lien.trames and serveur.instantane()["phares"]["en_attente"] is None

    executer(scenario())


def test_phares_simulation_commande_et_page(capsys, tmp_path, monkeypatch):
    assert cli.main(["phares", "allumer", "--json", "--donnees", str(tmp_path)]) == 0  # simulation par défaut
    sortie = capsys.readouterr().out
    assert "phares-allumer" in sortie and '"signal": 10' in sortie
    assert cli.main(["phares", "eteindre", "--json", "--donnees", str(tmp_path)]) == 0
    assert '"signal": 11' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["phares", "clignoter", "--donnees", str(tmp_path)])
    assert mouvement.simuler(Ordre("phares-allumer"), Calibration(), None)["trame"]["champs"]["robotConfig"] == {"signal": 10}

    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            statut, _reponse, _recus = await poster(http, serveur, "/api/phares", {"allumer": True})
            assert statut in (200, 202)

    executer(scenario())
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    grille = page[page.index('<p class="actions-robot">') :]
    assert 'id="phares-bouton"' in grille[: grille.index("</p>")]  # dans la grille d'actions du pilotage
    if NODE is not None:
        script = ("const M = require(process.argv[1]); process.stdout.write(JSON.stringify(["
                  "M.boutonPhares(null).libelle, M.boutonPhares({allumes:true}).libelle, M.boutonPhares({allumes:false,en_attente:true}).libelle]));")
        sortie = subprocess.run([NODE, "-e", script, str(ui.STATIQUE / "ordres.js")], capture_output=True, text=True, timeout=20, check=True, encoding="utf-8")
        assert json.loads(sortie.stdout) == ["Phares : allumer", "Phares : éteindre", "Phares : éteindre"]


# --- diagnostic RTK : réponses réelles rejouées --------------------------------------------------------


def test_reponses_reelles_de_diagnostic_rtk_rejouees_age_des_corrections_et_pas_de_position_brute():
    etat = Etat(en_ligne=True, origine=(46.5, -0.5))
    etat.appliquer(fragment(statut=6, tache=2, est=3.0, nord=2.0, precision=0.05), 1.0)
    ages, precisions, satellites = [], [], []
    for rang, (l1, l2) in enumerate(zip(REELLES_L1, REELLES_L2)):
        niveau1 = decode_pboutput(pb._field_bytes(35, bytes.fromhex(l1)))
        niveau2 = decode_pboutput(pb._field_bytes(36, bytes.fromhex(l2)))
        etat.appliquer(niveau1, 2.0 + rang)
        etat.appliquer(niveau2, 2.5 + rang)
        ages.append(etat.age_corrections_s)
        precisions.append(niveau1["rtkL1"]["locationPrecisionM"])
        satellites.append(niveau1["rtkL1"]["gnssSatellites"])
        assert niveau1["rtkL1"]["baseStationStatus"] == "online" and niveau1["rtkL1"]["dataErrorRatePct"] == 0.0
        # Aucune coordonnée dans ces réponses : ni position brute, ni écart calculable.
        assert "llaCoords" not in niveau1 and "llaCoords" not in niveau2
    assert ages == [2.0, 2.0, 2.0]  # corrections de la base reçues il y a 2 s
    assert all(0.02 < p < 0.11 for p in precisions) and all(23 <= s <= 29 for s in satellites)
    assert etat.detail_localisation() == "corrections de la base reçues il y a 2 s"
    assert etat.position_brute is None and etat.ecart_brut_m is None and etat.ecart_pour_la_garde is None
    assert etat.resume(5.0)["ecart_brut_m"] is None and etat.resume(5.0)["age_corrections_s"] == 2.0


def test_diagnostic_affiche_dit_tout_ce_que_la_reponse_reelle_contient(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=3, tache=2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/diagnostic-rtk", {"actif": True})
            serveur.session.recevoir_etat(decode_pboutput(pb._field_bytes(35, bytes.fromhex(REELLES_L1[0]))))
            serveur.session.recevoir_etat(decode_pboutput(pb._field_bytes(36, bytes.fromhex(REELLES_L2[0]))))
            assert serveur.instantane()["diagnostic_texte"] == (
                "corrections de la base reçues il y a 2 s ; base en ligne ; lien radio de la base : 0 % d'erreur ; 28 satellites ; "
                "écart position satellite / position calculée : non mesurable (position brute non transmise)"
            )

    executer(scenario())
