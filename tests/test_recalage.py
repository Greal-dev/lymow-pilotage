"""Écart carte / robot mesuré à la station, recalage d'affichage par l'opérateur, liaison lente.

Constat du propriétaire sur place (2026-10-12) : « l'affichage est décalé ». Données construites : la carte
d'essai a sa station en (1 ; 1), une pelouse de 10 × 6 m.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from conftest import ARRET, est_consigne, fragment
from test_carte import contenu
from test_ui import attendre, executer, lire, poster, serveur_ui

from lymow_pilotage import mouvement, recalage, releve, ui
from lymow_pilotage.recalage import Memoire, SuiviStation

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
STATION = (1.0, 1.0)
SANS_POSE = {"robotState": 3, "workStatus": 2, "rtkEastM": 0.03}


def js(fichier: str, expression: str, *arguments):
    script = (
        "const M = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(ui.STATIQUE / fichier), json.dumps(arguments)],
        capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
    )
    return json.loads(sortie.stdout)


def journal(tmp_path) -> list[dict]:
    lignes = []
    for fichier in tmp_path.rglob("*.jsonl"):
        if fichier.name != releve.FICHIER:
            lignes += [json.loads(ligne) for ligne in fichier.read_text(encoding="utf-8").splitlines()]
    return lignes


# --- 1. mesure à la station ---------------------------------------------------------------------------


def test_ecart_mesure_a_chaque_arrivee_a_la_station_sur_la_derniere_pose_recente():
    suivi = SuiviStation()
    assert suivi.observer(4, (5.0, 1.0), 100.0, 0.02, STATION, 100.0) is None  # retour à la base, en route
    assert suivi.observer(4, (1.9, 1.1), 130.0, 0.03, STATION, 130.0) is None  # dernière position annoncée
    mesure = suivi.observer(5, None, None, None, STATION, 140.0)  # « en charge » : le robot n'annonce plus de position
    assert (mesure["est_m"], mesure["nord_m"], mesure["norme_m"], mesure["precision_m"]) == (0.9, 0.1, 0.91, 0.03)
    assert suivi.observer(5, None, None, None, STATION, 150.0) is None  # une mesure par arrivée
    assert suivi.observer(12, None, None, None, STATION, 160.0) is None  # charge terminée : toujours la même arrivée
    assert suivi.observer(2, (3.0, 3.0), 200.0, 0.02, STATION, 200.0) is None  # il repart
    assert suivi.observer(5, (1.5, 0.6), 300.0, 0.02, STATION, 300.0)["nord_m"] == -0.4  # nouvelle arrivée, nouvelle mesure
    # Pose trop ancienne, carte sans station, premier état déjà « en charge » : aucune mesure, plutôt qu'une fausse.
    vieux = SuiviStation()
    vieux.observer(4, (1.9, 1.1), 100.0, 0.03, STATION, 100.0)
    assert vieux.observer(5, None, None, None, STATION, 100.0 + recalage.POSE_RECENTE_S + 1) is None
    sans = SuiviStation()
    sans.observer(4, (1.9, 1.1), 100.0, 0.03, None, 100.0)
    assert sans.observer(5, None, None, None, None, 101.0) is None
    assert SuiviStation().observer(5, (1.9, 1.1), 100.0, 0.03, STATION, 100.0) is None


def test_textes_du_decalage():
    assert recalage.texte((1.2, 0.4)) == "1,2 m vers l'est, 0,4 m vers le nord"
    assert recalage.texte((-0.46, 0.04)) == "0,46 m vers l'ouest" and recalage.texte((0.0, -2.0)) == "2 m vers le sud"
    assert recalage.texte((0.04, -0.04)) == "aucun"
    mesure = {"est_m": 0.9, "nord_m": 0.0, "horodatage": "2026-10-12T20:40:11"}
    assert recalage.texte_mesure(mesure) == "écart carte/robot mesuré à la station : 0,9 m vers l'est, le 12/10 à 20:40"
    assert recalage.texte_mesure(None) == ""


def test_arrivee_a_la_station_ecart_journalise_affiche_et_utilisable_en_un_clic(tmp_path, monkeypatch, session_reelle, lien):
    lien.carte = contenu()
    lien.robot = fragment(statut=4, est=1.9, nord=1.0, precision=0.03)  # retour à la base, presque arrivé

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])
            await poster(http, serveur, "/api/actualiser")
            assert serveur.instantane()["recalage"] == {"actif": False, "est_m": None, "nord_m": None, "texte": "", "mesure_station": ""}
            statut, reponse, _recus = await poster(http, serveur, "/api/recalage/station")
            assert statut == 409 and "aucun écart" in reponse["erreur"]
            lien.robot = {"robotState": 5, "workStatus": 5, "rtkEastM": 0.03}  # en charge, sans position
            await poster(http, serveur, "/api/actualiser")
            mesures = [ligne for ligne in journal(tmp_path) if ligne["evenement"] == "ecart-station"]
            assert len(mesures) == 1 and (mesures[0]["est_m"], mesures[0]["nord_m"], mesures[0]["precision_m"]) == (0.9, 0.0, 0.03)
            assert serveur.instantane()["recalage"]["mesure_station"].startswith("écart carte/robot mesuré à la station : 0,9 m vers l'est, le ")
            # En un clic : le robot s'annonce 0,9 m trop à l'est quand il est sur la station → affichage décalé de 0,9 m vers l'ouest.
            publiees = len(lien.trames)
            statut, reponse, _recus = await poster(http, serveur, "/api/recalage/station")
            assert statut == 200 and (reponse["est_m"], reponse["nord_m"]) == (-0.9, -0.0) and "0,9 m vers l'ouest" in reponse["texte"]
            assert len(lien.trames) == publiees  # rien n'est envoyé au robot

    executer(scenario())


# --- 2. recalage d'affichage par l'opérateur -----------------------------------------------------------


def test_logique_du_decalage_et_memoire_datee(tmp_path):
    assert recalage.decalage_vers((9.62, 5.0), (7.62, 5.4)) == (-2.0, 0.4)
    assert recalage.appliquer((9.62, 5.0), (-2.0, 0.4)) == pytest.approx((7.62, 5.4))
    assert recalage.appliquer((9.62, 5.0), None) == (9.62, 5.0) and recalage.appliquer(None, (1.0, 1.0)) is None
    for loin in ((60.0, 5.0), (float("nan"), 5.0), (9.62, float("inf"))):
        with pytest.raises(ValueError, match="20 m au plus"):
            recalage.decalage_vers((9.62, 5.0), loin)
    memoire = Memoire(tmp_path)
    assert memoire.decalage is None and memoire.resume()["actif"] is False
    assert memoire.poser((1.2, 0.4), "indiqué par l'opérateur") is True
    relue = Memoire(tmp_path)
    assert relue.decalage == (1.2, 0.4) and relue.source == "indiqué par l'opérateur"
    assert relue.resume()["texte"].startswith("carte recalée par l'opérateur — décalage appliqué : 1,2 m vers l'est, 0,4 m vers le nord (le ")
    assert relue.annuler() is True and Memoire(tmp_path).decalage is None
    (tmp_path / recalage.FICHIER).write_text('{"decalage": {"est_m": 500, "nord_m": 0}}', encoding="utf-8")
    assert Memoire(tmp_path).decalage is None  # valeur absurde sur le disque : ignorée
    (tmp_path / recalage.FICHIER).write_text("pas du json", encoding="utf-8")
    assert Memoire(tmp_path).decalage is None and Memoire(None).poser((1.0, 0.0), "x") is False


def test_le_robot_est_ici_decalage_applique_partout_ou_l_outil_place_le_robot(tmp_path, monkeypatch, session_reelle, lien):
    lien.carte = contenu()
    lien.robot = fragment(statut=3, tache=2, est=9.62, nord=5.0, precision=0.02)  # annoncé à 0,35 m du bord

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])
            await poster(http, serveur, "/api/actualiser")
            assert serveur.instantane()["ordres"]["bord"]["devant_m"] == 0.35
            for corps in ({}, {"est": 7.62}, {"est": "x", "nord": 5.0}, {"est": 7.62, "nord": 5.0, "cap": 3}):
                statut, _reponse, _recus = await poster(http, serveur, "/api/recalage", corps)
                assert statut == 400
            statut, reponse, _recus = await poster(http, serveur, "/api/recalage", {"est": 80.0, "nord": 5.0})
            assert statut == 400 and "20 m au plus" in reponse["erreur"]
            publiees = len(lien.trames)
            # L'opérateur, qui voit le robot, le place 2 m plus à l'ouest.
            statut, reponse, _recus = await poster(http, serveur, "/api/recalage", {"est": 7.62, "nord": 5.0})
            assert statut == 200 and reponse["actif"] is True and (reponse["est_m"], reponse["nord_m"]) == (-2.0, 0.0)
            assert "carte recalée par l'opérateur — décalage appliqué : 2 m vers l'ouest" in reponse["texte"]
            assert len(lien.trames) == publiees  # côté outil seulement : RIEN n'est envoyé au robot
            image = serveur.instantane()
            # Marqueur (et son cercle, centré sur lui) : à l'endroit indiqué ; la valeur annoncée reste lisible.
            assert (image["etat"]["position_est_m"], image["etat"]["position_nord_m"]) == (7.62, 5.0)
            assert image["etat"]["position_annoncee"] == [9.62, 5.0]
            # Avertissement de bord de zone : jugé depuis la position recalée.
            assert image["ordres"]["bord"]["devant_m"] == 2.35 and image["recalage"]["actif"] is True
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 2}]})
            assert corps["garde_carte"] == []  # 2 m devant, bord à 2,35 m : plus d'alerte
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            await attendre(lambda: not serveur.occupe, delai=30.0)
            assert serveur.resultat["etapes"][0]["garde_carte"] == "d'après la carte : bord de zone devant à 2,35 m"
            # Trajet « bon signal » et relevé affiché : mêmes positions recalées ; le disque garde les valeurs brutes.
            assert serveur._releve.noter(4.0, 2.0, 0.03, 20, 1.0e9, serveur._recalage.decalage)  # loin du point précédent
            _statut, texte, _recus = await lire(http, serveur, "/api/releve")
            assert json.loads(texte)["points"][-1][:2] == [2.0, 2.0]
            brut = json.loads((tmp_path / releve.FICHIER).read_text(encoding="utf-8").splitlines()[-1])
            assert (brut["est_m"], brut["nord_m"], brut["decalage_est_m"], brut["decalage_nord_m"]) == (4.0, 2.0, -2.0, 0.0)
            evenements = [ligne["evenement"] for ligne in journal(tmp_path)]
            assert "recalage-affichage" in evenements

        # Mémorisé, daté : un nouveau serveur le retrouve ; « Annuler le décalage » revient à la position annoncée.
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            assert serveur.instantane()["recalage"]["actif"] is True and serveur.instantane()["etat"]["position_est_m"] == 7.62
            statut, reponse, _recus = await poster(http, serveur, "/api/recalage/annuler")
            assert statut == 200 and reponse["actif"] is False and reponse["texte"] == ""
            image = serveur.instantane()
            assert image["etat"]["position_est_m"] == 9.62 and "position_annoncee" not in image["etat"]
            # Une position ancienne sert encore de repère (robot immobile : il n'en renvoie pas forcément une autre).
            serveur.session.etat.recu_pose -= 3600.0
            statut, reponse, _recus = await poster(http, serveur, "/api/recalage", {"est": 7.62, "nord": 5.0})
            assert statut == 200 and reponse["actif"] is True
            # Aucune position jamais annoncée : pas de décalage mesurable, et c'est dit.
            serveur.session.etat.recu_pose = None
            statut, reponse, _recus = await poster(http, serveur, "/api/recalage", {"est": 7.62, "nord": 5.0})
            assert statut == 409 and "aucune position" in reponse["erreur"]

    executer(scenario())


@sans_node
def test_page_recalage_mention_permanente_et_boutons():
    actif = {"actif": True, "texte": "carte recalée par l'opérateur — décalage appliqué : 1,2 m vers l'est", "mesure_station": "écart mesuré"}
    assert js("ordres.js", "M.etatDuRecalage(a[0], false)", actif) == {
        "texte": actif["texte"], "annuler": True, "mesure": "écart mesuré", "utiliserLaMesure": False,
    }
    sans = js("ordres.js", "M.etatDuRecalage(a[0], false)", {"actif": False, "texte": "", "mesure_station": "écart mesuré"})
    assert sans["annuler"] is False and sans["utiliserLaMesure"] is True  # la mesure à la station se propose en un clic
    assert js("ordres.js", "M.etatDuRecalage(null, true)")["texte"] == "Cliquez l'endroit réel du robot sur la carte (Échap pour annuler)"
    assert js("ordres.js", "M.distanceDuDecalage(a[0], a[1])", [9.62, 5.0], [7.62, 5.0]) == "2 m"
    assert js("ordres.js", "M.distanceDuDecalage(a[0], a[1])", [0, 0], [1.2, 0.5]) == "1,3 m"
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    for texte in (">Le robot est ici<", ">Annuler le décalage<", ">Utiliser l'écart mesuré à la station<", 'id="recalage-mention"'):
        assert texte in page
    carte_js = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    assert 'recaler("/api/recalage", { est: position.x, nord: -position.y });' in carte_js  # axe vertical de l'écran inversé
    # Défaut constaté en réel (2026-10-12) : un clic à la main tremblante était pris pour un glisser et ignoré.
    # En pointage, le relâchement désigne l'endroit, et la carte ne glisse pas.
    assert "if (attenteIci) { indiquerLeRobot(versCarte(evenement)); return; }" in carte_js
    assert "if (!glisse || attenteIci) { return; }" in carte_js
    assert 'id="recalage-bandeau" class="bandeau-pointage" role="status" hidden>Cliquez l\'endroit réel du robot sur la carte (Échap pour annuler)<' in page
    assert 'svg("line", "carte-decalage"' in carte_js and ".cadre-carte svg.attente-clic { cursor: crosshair; }" in (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
    assert 'if (evenement.key === "Escape" && attenteIci) { attenteIci = false; afficherRecalage(); }' in carte_js
    assert 'image.recalage && image.recalage.actif ? image.recalage.texte : ""' in (ui.STATIQUE / "app.js").read_text(encoding="utf-8")


# --- 4. liaison lente ---------------------------------------------------------------------------------------


async def _fleche(http, serveur, direction="haut"):
    return await poster(http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": direction, "pas": 0.25})


def _muet_pendant_le_pas(lien):
    """Le robot cesse de répondre dès la première consigne : aucune trame fraîche après le pas."""
    lien.sur_publication = lambda lien_, octets: setattr(lien_, "robot", None) if est_consigne(octets) else None


def test_liaison_lente_la_main_est_gardee_et_le_pas_suivant_attend_une_trame(tmp_path, monkeypatch, session_reelle, lien):
    assert ui.SANS_TRAME_MAX_S == 45.0  # valeur réelle : 45 s sans aucune trame avant de retirer la main
    monkeypatch.setattr(mouvement, "ATTENTE_CONSTAT_S", 0.4)
    monkeypatch.setattr(ui, "SANS_TRAME_MAX_S", 30.0)
    lien.robot = dict(SANS_POSE)
    _muet_pendant_le_pas(lien)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            statut, _corps, _recus = await _fleche(http, serveur)
            assert statut == 202
            await attendre(lambda: serveur.instantane()["ordres"]["liaison_lente"] is True, delai=15.0)
            assert "aucune trame d'état fraîche" in serveur.direct.faites[-1]["rapport"]["constat"]
            assert serveur.direct.arme is True and serveur.occupe  # la main est gardée ; le pas suivant attend
            consignes = len(lien.consignes)
            statut, corps, _recus = await poster(
                http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "gauche", "pas": 15}
            )
            assert statut == 202 and corps["file"] == ["tourner 15"]  # l'appui reste en file
            await asyncio.sleep(0.6)
            assert len(lien.consignes) == consignes and serveur.direct.arme is True  # rien n'est émis sans trame fraîche
            lien.sur_publication = None
            lien.robot = dict(SANS_POSE)  # le robot répond de nouveau
            await attendre(lambda: len(serveur.direct.faites) == 2 and not serveur.occupe, delai=20.0)
            assert serveur.direct.faites[-1]["rapport"]["ordre"] == "tourner 15" and len(lien.consignes) > consignes
            assert serveur.direct.arme is True and serveur.instantane()["ordres"]["liaison_lente"] is False
            assert not any(ligne["evenement"] == "direct-desarme" for ligne in journal(tmp_path))

    executer(scenario())


def test_liaison_lente_desarme_apres_le_delai_sans_aucune_trame_et_stop_reste_immediat(tmp_path, monkeypatch, session_reelle, lien):
    monkeypatch.setattr(mouvement, "ATTENTE_CONSTAT_S", 0.4)
    monkeypatch.setattr(ui, "SANS_TRAME_MAX_S", 1.5)
    assert ui.SANS_TRAME_MAX_S == 1.5
    lien.robot = dict(SANS_POSE)
    _muet_pendant_le_pas(lien)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            await _fleche(http, serveur)
            await attendre(lambda: not serveur.occupe, delai=20.0)  # délai écoulé sans aucune trame
            assert serveur.direct.arme is False
            motifs = [ligne["motif"] for ligne in journal(tmp_path) if ligne["evenement"] == "direct-desarme"]
            assert motifs == ["1.5 s sans aucune trame du robot : liaison perdue"]

            # STOP pendant l'attente : immédiat.
            monkeypatch.setattr(ui, "SANS_TRAME_MAX_S", 30.0)
            lien.robot = dict(SANS_POSE)
            await poster(http, serveur, "/api/actualiser")
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            await _fleche(http, serveur)
            await attendre(lambda: serveur.instantane()["ordres"]["liaison_lente"] is True, delai=15.0)
            lien.publies.clear()
            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202
            await attendre(lambda: lien.arrets[-3:] == [ARRET] * 3, delai=3.0)

    executer(scenario())
    pave = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    assert "Liaison lente : en attente d'une nouvelle trame du robot…" in pave
