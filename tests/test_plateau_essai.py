"""Essais de levée de nettoyage du plateau : deux ordres expérimentaux, jamais émis par le nuage sur ce robot.

Attesté par les sources : l'enveloppe (phares du robot, capturée : 10 31 6a 02 40 0a) et les numéros 8 / 9.
Supposé : que le robot les accepte par le nuage. Ces tests ne prouvent que ce que l'outil émet et refuse.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import fragment
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import protocole, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.protocole import Categorie, est_requete_pure

NODE = shutil.which("node")
LEVER, DESCENDRE = bytes.fromhex("10 31 6a 02 40 08"), bytes.fromhex("10 31 6a 02 40 09")
CONFIRMATION = "Ordre jamais essayé par le cloud sur ce robot. À faire robot à vue, mains éloignées du plateau."


def test_seuls_les_deux_signaux_de_plateau_sortent_octet_pour_octet():
    lever, descendre = (protocole.trame_signal_plateau(nom, Calibration()) for nom in ("nettoyage", "redescendre"))
    assert (lever.octets, descendre.octets) == (LEVER, DESCENDRE)  # l'enveloppe des phares, seul le numéro change
    assert lever.categorie is Categorie.REGLAGE and not est_requete_pure(LEVER) and not est_requete_pure(DESCENDRE)
    assert protocole.SIGNAUX_PLATEAU == {"nettoyage": 8, "redescendre": 9}
    # La même énumération porte l'extinction (1, 28), le frein (2, 25), l'arrêt (3) : aucun autre numéro ne sort.
    for autre in ("extinction", "frein", "1", "28", 8, None, ""):
        with pytest.raises(ValueError):
            protocole.trame_signal_plateau(autre, Calibration())


def test_essai_de_plateau_reel_seulement_sur_confirmation_sec01_strict_et_constat(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=3, tache=2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            for corps in ({}, {"ordre": "nettoyage"}, {"ordre": "nettoyage", "confirmer": "oui"}, {"ordre": "extinction", "confirmer": True},
                          {"ordre": "nettoyage", "confirmer": True, "signal": 1}, {"ordre": 8, "confirmer": True}):
                statut, _reponse, _recus = await poster(http, serveur, "/api/plateau/essai", corps)
                assert statut == 400 and lien.ordres == []
            statut, _reponse, _recus = await poster(http, serveur, "/api/plateau/essai", {"ordre": "nettoyage", "confirmer": True})
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=30.0)
            assert lien.ordres == [LEVER]  # cet ordre, seul
            etape = serveur.resultat["etapes"][0]
            assert etape["essai"] is True and etape["ordre"] == "plateau : position de nettoyage (essai)"
            assert etape["observation"].startswith("aucun changement observable par l'outil")
            # Le robot annonce une erreur après la redescente : l'outil dit ce qu'il voit.
            lien.sur_publication = lambda lien_, octets: setattr(
                lien_, "robot", fragment(statut=7, tache=2) | {"errorCodes": [7]}
            ) if octets == DESCENDRE else None
            await poster(http, serveur, "/api/plateau/essai", {"ordre": "redescendre", "confirmer": True})
            await attendre(lambda: not serveur.occupe, delai=30.0)
            assert lien.ordres == [LEVER, DESCENDRE]
            assert "changement observé — statut : en erreur ; erreurs : 7 : " in serveur.resultat["etapes"][0]["observation"]
            journal = [json.loads(ligne) for f in tmp_path.rglob("*.jsonl") for ligne in f.read_text(encoding="utf-8").splitlines()]
            emis = [ligne for ligne in journal if ligne.get("evenement") == "ordre-emis"]  # le relevé de précision est aussi un .jsonl
            assert len(emis) == 2 and all(ligne["essai"] is True for ligne in emis)

            # SEC-01 strict : en tonte, refusé — même avec l'attestation de l'opérateur posée.
            lien.sur_publication = None
            lien.robot = fragment(statut=2, tache=2)
            await poster(http, serveur, "/api/actualiser")
            await poster(http, serveur, ui.ROUTE_ATTESTATION_POSER, {"confirmer": True})
            assert serveur.attestation.active is True
            await poster(http, serveur, "/api/plateau/essai", {"ordre": "nettoyage", "confirmer": True})
            await attendre(lambda: not serveur.occupe, delai=30.0)
            assert "SEC-01" in serveur.resultat["motif"] and lien.ordres == [LEVER, DESCENDRE]

    executer(scenario())


def test_essai_de_plateau_refuse_hors_mode_reel(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            statut, reponse, _recus = await poster(http, serveur, "/api/plateau/essai", {"ordre": "nettoyage", "confirmer": True})
            assert statut == 403 and "mode réel seulement" in reponse["erreur"] and lien.ordres == []
        # La démonstration, elle, rejoue le constat réel du 2026-10-13 : voir tests/test_nettoyage.py.

    executer(scenario())


def test_page_essais_ranges_dans_calibration_avec_leur_confirmation_et_hors_des_sequences():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    calibration = page[page.index('data-volet="calibration"') :]
    calibration = calibration[: calibration.index("</details>")]
    assert ">Plateau : position de nettoyage (essai)</button>" in calibration and ">Plateau : redescendre (essai)</button>" in calibration
    # Hors de Calibration : le seul « Redescendre le plateau » du bloc Pilotage, affiché en position de nettoyage.
    ailleurs = page.replace(calibration, "")
    assert ailleurs.count("data-plateau-essai") == 1 and 'id="nettoyage-redescendre"' in ailleurs
    assert "jamais essayée par le nuage sur ce robot" in calibration
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    clic = app[app.index('document.querySelectorAll("[data-plateau-essai]").forEach(function (bouton) {\n    bouton.addEventListener') :][:600]
    assert "window.confirm(" in clic and "Ordres.CONFIRMATION_ESSAI_PLATEAU" in clic
    assert f'"{CONFIRMATION}"' in (ui.STATIQUE / "ordres.js").read_text(encoding="utf-8")
    # Ni la grammaire des ordres, ni les séquences, ni les flèches ne connaissent ces essais.
    from lymow_pilotage import mouvement

    assert not any("nettoyage" in nom or "redescendre" in nom for nom in (*mouvement.ETAPES_SEQUENCE, *mouvement.TACHES, *ui.NOMS_ORDRES))
    if NODE is not None:
        script = "const M = require(process.argv[1]); process.stdout.write(JSON.stringify([M.essaiPlateauPossible({mode:'réel',occupe:false}).possible, M.essaiPlateauPossible({mode:'simulation',occupe:false}).possible, M.essaiPlateauPossible({mode:'réel',occupe:true}).possible]));"
        sortie = subprocess.run([NODE, "-e", script, str(ui.STATIQUE / "ordres.js")], capture_output=True, text=True, timeout=20, check=True, encoding="utf-8")
        assert json.loads(sortie.stdout) == [True, False, False]
