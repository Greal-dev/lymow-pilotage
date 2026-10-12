"""Retour sur la hauteur du plateau : une consigne lue dans la carte, jamais une mesure.

Constat du 2026-10-12 : aucune trame d'état du robot ne porte de hauteur (`hauteur_coupe_mm` toujours vide) ;
la carte reçue porte, elle, les réglages de tonte (champ 11, hauteur en champ 1). Aucune demande de lecture
nouvelle n'est ajoutée : la réponse à la demande 51 du socle n'est attestée par aucune capture.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import fragment
from test_carte import contenu
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import carte as cartes
from lymow_pilotage import ui
from lymow_pilotage.protocole import est_requete_pure
from lymow_pilotage.vendor.ha_lymow import protocol as pb

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
A_100_MM = contenu() + pb._field_bytes(13, pb._field_i32(1, 100))  # réglages de la tâche en cours : 100 mm


def js(expression: str, *arguments):
    script = (
        "const M = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(ui.STATIQUE / "ordres.js"), json.dumps(arguments)],
        capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
    )
    return json.loads(sortie.stdout)


def test_aucune_demande_de_lecture_non_attestee_n_est_ajoutee():
    # 51 (réglages en cours), 35 (configuration), 24 (tâche en cours) : nus, ils restent refusés.
    for octets in ("10 31 28 33", "10 31 28 23", "10 31 28 18"):
        assert not est_requete_pure(bytes.fromhex(octets))


def test_hauteur_de_coupe_decodee_dans_la_carte_trame_construite_selon_le_schema():
    assert cartes.hauteur_de_coupe(cartes.decoder(contenu())) == (50, "réglage global de tonte")  # champ 11, champ 1
    assert cartes.hauteur_de_coupe(cartes.decoder(A_100_MM)) == (100, "consigne en cours")  # champ 13 s'il existe
    sans_reglages = contenu()[:-4]  # la carte d'essai finit par son champ 11
    assert cartes.hauteur_de_coupe(cartes.decoder(sans_reglages)) is None
    absurde = sans_reglages + pb._field_bytes(11, pb._field_i32(1, 9000))
    assert cartes.hauteur_de_coupe(cartes.decoder(absurde)) is None  # valeur hors de toute course : ignorée
    vide = sans_reglages + pb._field_bytes(11, pb._field_i32(4, 1))  # réglages sans hauteur
    assert cartes.hauteur_de_coupe(cartes.decoder(vide)) is None


def test_suite_d_un_ordre_de_plateau():
    globale, en_cours = (96, "réglage global de tonte"), (100, "consigne en cours")
    assert ui.suite_du_plateau(globale, en_cours) == "consigne passée de 96 à 100 mm"
    assert ui.suite_du_plateau(globale, globale) == (
        "valeur relue inchangée (96 mm) : l'ordre n'a pas été pris en compte, ou cette lecture ne reflète pas la position du plateau"
    )
    assert ui.suite_du_plateau(None, en_cours) == "consigne relue après l'ordre : 100 mm"
    assert ui.suite_du_plateau(globale, None) == "hauteur inconnue après l'ordre : le robot ne l'a pas transmise"


@sans_node
def test_page_texte_du_plateau_inconnue_consigne_maximum_et_changement():
    inconnue = js("M.textePlateau(a[0])", {"mm": None, "maximum_mm": 100, "minimum_mm": 20, "suite": None})
    assert inconnue == {"texte": "Plateau : hauteur inconnue : le robot ne l'a pas transmise", "jauge": None}
    assert js("M.textePlateau(null)")["jauge"] is None
    consigne = js("M.textePlateau(a[0])", {
        "mm": 60, "maximum_mm": 100, "minimum_mm": 20, "au_maximum": False, "nature": "réglage global de tonte",
        "date": "carte complète du 12/10 à 19:38", "suite": None,
    })
    assert consigne["texte"] == (
        "Plateau : 60 mm (maximum 100) — consigne enregistrée par le robot (réglage global de tonte, "
        "carte complète du 12/10 à 19:38), pas une hauteur mesurée"
    )
    assert consigne["jauge"] == {"min": 20, "max": 100, "valeur": 60}
    maximum = js("M.textePlateau(a[0])", {
        "mm": 100, "maximum_mm": 100, "minimum_mm": 20, "au_maximum": True, "nature": "consigne en cours", "date": None,
        "suite": "consigne passée de 60 à 100 mm",
    })
    assert maximum["texte"].startswith("Plateau : 100 mm — au maximum — consigne enregistrée par le robot (consigne en cours)")
    assert maximum["texte"].endswith(" — consigne passée de 60 à 100 mm") and "mesurée" in maximum["texte"]
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    assert 'id="plateau-etat"' in page and '<meter id="plateau-jauge"' in page
    assert page.index('id="plateau-etat"') < page.index('id="pave-plateau"')  # dans le bloc de gauche, près de « Plateau haut »


def test_apres_un_ordre_de_plateau_la_carte_est_relue_et_le_changement_est_dit(tmp_path, monkeypatch, session_reelle, lien):
    lien.carte = contenu()
    lien.robot = fragment(statut=3, tache=2)
    demande_carte = bytes.fromhex("10 31 28 13")

    async def plateau_haut(http, serveur):
        _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "plateau-haut"}]})
        statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
        assert statut == 202
        await attendre(lambda: not serveur.occupe, delai=30.0)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])
            plateau = serveur.instantane()["plateau"]
            assert (plateau["mm"], plateau["maximum_mm"], plateau["au_maximum"], plateau["nature"]) == (50, 100, False, "réglage global de tonte")
            assert plateau["suite"] is None and serveur.instantane()["etat"]["hauteur_coupe_mm"] is None  # aucune mesure

            # L'ordre n'a rien changé à ce que le robot enregistre : dit tel quel.
            demandes = sum(t.startswith(demande_carte) for t in lien.trames)
            await plateau_haut(http, serveur)
            assert sum(t.startswith(demande_carte) for t in lien.trames) > demandes  # relecture, après l'ordre
            assert serveur.instantane()["plateau"]["suite"].startswith("valeur relue inchangée (50 mm)")
            assert serveur.resultat["plateau_relu"] == serveur.instantane()["plateau"]["suite"]

            # Le faux robot applique l'ordre : la relecture le montre, et « au maximum » est dit.
            def applique(lien_, octets):
                if b"\x62" in octets[:8] and octets not in (demande_carte,) and not octets.startswith(demande_carte):
                    lien_.carte = A_100_MM

            lien.sur_publication = applique
            await plateau_haut(http, serveur)
            plateau = serveur.instantane()["plateau"]
            assert (plateau["mm"], plateau["au_maximum"], plateau["nature"]) == (100, True, "consigne en cours")
            assert plateau["suite"] == "consigne passée de 50 à 100 mm"
            relus = [json.loads(ligne) for f in tmp_path.rglob("*.jsonl") for ligne in f.read_text(encoding="utf-8").splitlines()
                     if '"plateau-relu"' in ligne]
            assert [(r["avant"], r["apres"]) for r in relus] == [(50, 50), (50, 100)]

    executer(scenario())


def test_sans_carte_la_hauteur_est_dite_inconnue(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=3, tache=2)
    monkeypatch.setattr(ui.ServeurUI, "_demander_la_carte_d_office", lambda self: None)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, _http):
            assert serveur.instantane()["plateau"] == {
                "mm": None, "maximum_mm": 100, "minimum_mm": 20, "au_maximum": False, "nature": None, "date": None, "suite": None,
            }

    executer(scenario())
