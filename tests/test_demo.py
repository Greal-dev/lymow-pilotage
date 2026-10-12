"""Mode démonstration : robot et jardin fictifs, aucun réseau, aucun coffre, aucun compte."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from conftest import lignes_journal
from test_ui import attendre, executer, lire, poster

from lymow_pilotage import carte as cartes
from lymow_pilotage import cli, coffre, demo, geometrie, liaison, retouche, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.journal import Journal
from lymow_pilotage.mouvement import ErreurSaisie
from lymow_pilotage.securite import RefusSecurite


@asynccontextmanager
async def serveur_demo(tmp_path, monkeypatch):
    """Serveur de démonstration ; toute tentative d'atteindre coffre, compte ou nuage fait échouer le test."""

    def interdit(*_args, **_kwargs):
        raise AssertionError("coffre, compte ou réseau sollicité en démonstration")

    for cible, nom in ((liaison, "lire_identifiants"), (liaison, "ouvrir_session"), (liaison, "obtenir_acces"),
                       (liaison, "demarrer_video"), (coffre, "lire"), (coffre, "ecrire"), (coffre, "appareil_connu")):
        monkeypatch.setattr(cible, nom, interdit)
    serveur = ui.ServeurUI(Calibration(), Journal(tmp_path), reel=False, port=0, demonstration=True)
    serveur.ouvrir_http = interdit
    await serveur.demarrer()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            yield serveur, http
    finally:
        await serveur.arreter()


def test_jardin_fictif_plausible_et_complet():
    jardin = cartes.decoder(demo.jardin())
    assert jardin.complete and [z.nom for z in jardin.zones] == ["Parterre sud", "Parterre nord", "Allée en équerre"]
    assert all(geometrie.est_simple(z.contour) and len(z.contour) >= 10 for z in jardin.zones)
    equerre = jardin.zone("allee-equerre").contour
    xs, ys = [p[0] for p in equerre], [p[1] for p in equerre]
    assert abs(geometrie.aire_signee(equerre)) < (max(xs) - min(xs)) * (max(ys) - min(ys)) * 0.8  # concave
    assert [z.nom for z in jardin.interdites] == ["Bassin", "Massif de buis"]
    for interdite in jardin.interdites:
        assert geometrie.strictement_dans(interdite.contour, jardin.zone(interdite.parent).contour)
    assert [p.station for p in jardin.passages] == [False, True]
    assert all(geometrie.touche(jardin.zone("parterre-sud").contour, p.trace) for p in jardin.passages)
    assert geometrie.contient(jardin.zone("parterre-sud").contour, jardin.station[:2])


def test_demo_et_reel_incompatibles(capsys, tmp_path):
    assert cli.main(["ui", "--demo", "--reel", "--donnees", str(tmp_path)]) == 2
    assert "incompatibles" in capsys.readouterr().err
    with pytest.raises(ErreurSaisie, match="incompatibles"):
        ui.ServeurUI(Calibration(), Journal(tmp_path), reel=True, port=0, demonstration=True)


def test_commande_ui_demo_transmet_le_mode(tmp_path, monkeypatch):
    appels = []

    async def servir(_cal, _journal, **options):
        appels.append((options["reel"], options["demonstration"]))

    monkeypatch.setattr(ui, "servir", servir)
    assert cli.main(["ui", "--demo", "--donnees", str(tmp_path)]) == 0
    assert cli.main(["ui", "--donnees", str(tmp_path)]) == 0
    assert appels == [(False, True), (False, False)]


def test_demonstration_de_bout_en_bout_sans_reseau_ni_coffre(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            _statut, texte, _recus = await lire(http, serveur, "/api/etat")
            image = json.loads(texte)
            assert (image["mode"], image["connecte"], image["appareil"]) == ("démonstration", True, "robot-fictif")
            assert image["liaison"] == "DÉMONSTRATION — robot fictif" and image["compte"]["connecte"] is False
            await attendre(lambda: serveur.session.etat.statut is not None)
            depart = serveur.session.etat.pose

            # Un ordre « part » vers le robot fictif, qui bouge.
            statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 0.2}]})
            assert statut == 200 and corps["identifiant"]
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=8.0)
            assert serveur.resultat["mode"] == "démonstration"  # jamais « réel » à l'écran
            etape = serveur.resultat["etapes"][0]
            assert etape["mode"] == "démonstration"
            assert etape["constat"] == "déplacement constaté" and etape["deplacement_m"] == pytest.approx(0.2, abs=0.03)
            assert serveur.session.etat.pose.est_m == pytest.approx(depart.est_m + 0.2, abs=0.03)

            # STOP et pause : même chemin que le vrai.
            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=8.0)
            assert serveur.resultat["etapes"][0]["ordre"] == "stop" and serveur.resultat["etapes"][0]["arrets_acquittes"] == 3

            # Carte, puis retouche appliquée par le robot fictif.
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 200 and corps["carte"]["complete"] is True and len(corps["carte"]["zones"]) == 3
            avant = corps["carte"]["zones"][1]["contour"]
            ajustement = {"zone": "parterre-nord", "de": 0, "a": 4, "cm": 20, "cote": "court", "raccord": 0.5}
            statut, corps, _recus = await poster(http, serveur, "/api/carte/previsualiser", ajustement)
            assert statut == 200 and corps["identifiant"] and corps["resume"]["variation_m2"] > 0
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=12.0)
            assert serveur.resultat["verdict"] == "appliqué" and serveur.resultat["code_sortie"] == 0
            assert serveur.resultat["mode"] == "démonstration"
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            apres = corps["carte"]["zones"][1]["contour"]
            assert apres != avant and apres[2] == pytest.approx([8.0, 11.8], abs=0.001)
            assert corps["carte"]["zones"][0]["contour"] == json.loads(json.dumps(cartes.vers_json(cartes.decoder(demo.jardin()))["zones"][0]["contour"]))

            # Ce qui n'existe pas en démonstration.
            statut, erreur, _recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 403 and "démonstration" in erreur["erreur"]
            for chemin in ("/api/compte/commencer", "/api/compte/valider", "/api/compte/choisir", "/api/compte/deconnecter", "/api/compte/un-clic"):
                statut, erreur, _recus = await poster(http, serveur, chemin, {"region": "eu-west-1", "activer": True})
                assert statut == 403 and "démonstration" in erreur["erreur"], chemin
            statut, _corps, _recus = await poster(http, serveur, "/api/actualiser")
            assert statut == 200
            return serveur.resultat["sauvegarde"]

    sauvegarde = Path(executer(scenario()))
    # Sauvegarde dans un sous-dossier distinct, inutilisable pour une restauration réelle.
    assert sauvegarde.parent == tmp_path / "sauvegardes-carte" / "demonstration"
    assert list((tmp_path / "sauvegardes-carte").glob("*.pb")) == []
    assert json.loads(sauvegarde.with_suffix(".json").read_text(encoding="utf-8"))["demonstration"] is True
    with pytest.raises(RefusSecurite, match="démonstration"):
        retouche.charger_sauvegarde(sauvegarde, demo.APPAREIL)
    assert retouche.reference_zone(tmp_path, demo.APPAREIL, "parterre-nord") is None  # jamais référence d'un vrai robot
    assert "carte-ecriture" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_rappel_et_reouverture_en_demonstration(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            serveur.ouvrir_navigateur = lambda _adresse: True
            adresse = serveur.adresse.rstrip("/")
            async with http.post(adresse + "/api/instance/rappel", json={"adresse": "myapp://callback/?code=a&state=b"}) as r:
                assert r.status == 409  # aucune connexion de compte en démonstration
            async with http.post(adresse + ui.ROUTE_REOUVERTURE, json={"mode": "démonstration"}) as r:
                assert r.status == 200 and (await r.json())["mode"] == "démonstration"
            with pytest.raises(ui.EchecDemarrage, match="une interface en mode démonstration tourne déjà"):
                await ui.rejoindre(serveur.port, False)

    executer(scenario())


def test_robot_fictif_applique_la_retouche_sans_toucher_au_reste():
    jardin = cartes.decoder(demo.jardin())
    operation = retouche.preparer_ajustement(jardin, "allee-equerre", 0, 2, -15, "court", 0)

    async def scenario():
        from lymow_pilotage.protocole import Categorie, trame_contour_zone
        from lymow_pilotage.session import Session

        robot = demo.RobotFictif()
        session = Session(robot, demo.APPAREIL, Journal(Path("inutilise")), Calibration(), frozenset(Categorie))
        session.journal.ecrire = lambda *_a, **_k: None
        robot.session = session
        await session.publier(trame_contour_zone("allee-equerre", operation.apres, Calibration()), 3.0)
        return cartes.decoder(robot.carte), session.etat.statut

    relue, statut = asyncio.run(scenario())
    assert retouche.comparer(jardin, relue, operation) == ("appliqué", [])
    assert statut == 1 and relue.complete


def test_page_porte_le_bandeau_de_demonstration():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    script = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert 'id="bandeau-demo"' in page and "DÉMONSTRATION — robot fictif" in page and demo.MENTION in page
    assert '$("bandeau-demo").hidden = !demonstration' in script and '$("video-demarrer").hidden = demonstration' in script


def test_robot_fictif_publie_son_etat_periodiquement_l_affichage_ne_se_grise_pas(tmp_path, monkeypatch):
    assert 1 <= demo.PERIODE_ETAT_S <= 10  # bien en deçà des 30 s de fraîcheur
    monkeypatch.setattr(demo, "PERIODE_ETAT_S", 0.05)

    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, _http):
            await attendre(lambda: serveur.session.etat.recu_statut is not None)
            premier = serveur.session.etat.recu_statut
            recues = len(serveur.session.lien.recues)
            await asyncio.sleep(0.4)  # aucune demande de la page pendant ce temps
            assert len(serveur.session.lien.recues) == recues
            assert serveur.session.etat.recu_statut > premier + 0.2
            image = serveur.instantane()
            assert image["etat"]["perime"] is False and image["etat"]["age_etat_s"] < 0.3
            battement = serveur._battement
        assert battement.done()  # arrêté avec le serveur

    executer(scenario())
