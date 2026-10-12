"""Version de l'interface : empreinte du code, page figée en mémoire, remplacement d'une ancienne instance.

Panne du 2026-10-12 : une interface lancée avant une mise à jour servait la nouvelle page du
disque avec l'ancien code en mémoire, et relancer le raccourci ne faisait que la rouvrir.
"""

from __future__ import annotations

import asyncio
import shutil

import aiohttp
import pytest
from aiohttp import web
from test_ui import attendre, executer, lire, serveur_ui

from lymow_pilotage import ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.journal import Journal


async def locale(http, port, route, corps=None, **entetes):
    """Comme un second processus lymow : POST sans Origin ni Referer."""
    async with http.post(f"http://127.0.0.1:{port}{route}", json=corps or {}, headers=entetes) as reponse:
        return reponse.status, await reponse.json(content_type=None)


def test_identite_donne_le_mode_et_l_empreinte_du_code_sans_effet(tmp_path, monkeypatch):
    ouvertes = []

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            serveur.ouvrir_navigateur = lambda adresse: ouvertes.append(adresse) or True
            statut, corps = await locale(http, serveur.port, ui.ROUTE_IDENTITE)
            assert statut == 200 and corps == {"interface": "lymow", "mode": "simulation", "empreinte": ui.empreinte_du_code()}
            assert len(corps["empreinte"]) == 12 and serveur.instantane()["serveur"]["empreinte"] == corps["empreinte"]
            assert ouvertes == [] and not serveur.fin.is_set()

    executer(scenario())


def test_empreinte_change_avec_le_code_et_la_page_reste_celle_du_demarrage(tmp_path, monkeypatch):
    copie = tmp_path / "paquet" / "ui_statique"
    shutil.copytree(ui.STATIQUE, copie)
    (copie.parent / "module.py").write_text("VERSION = 1\n", encoding="utf-8")
    monkeypatch.setattr(ui, "STATIQUE", copie)
    avant = ui.empreinte_du_code()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            d_origine = (copie / "app.js").read_text(encoding="utf-8")
            # Mise à jour du code sur le disque pendant que le serveur tourne.
            (copie / "app.js").write_text("nouvelleVersion();\n", encoding="utf-8")
            (copie / "index.html").write_text("<p>nouvelle page</p>", encoding="utf-8")
            (copie / "carte.js").unlink()
            statut, texte, _recus = await lire(http, serveur, "/statique/app.js")
            assert statut == 200 and texte == d_origine  # servi depuis la mémoire
            statut, texte, _recus = await lire(http, serveur, "/")
            assert statut == 200 and "nouvelle page" not in texte and 'id="carte-dessin"' in texte
            statut, _texte, _recus = await lire(http, serveur, "/statique/carte.js")
            assert statut == 200
            assert serveur.empreinte == avant  # figée au lancement

    executer(scenario())
    (copie / "carte.js").write_text("", encoding="utf-8")
    apres = ui.empreinte_du_code()
    assert apres != avant
    (copie.parent / "module.py").write_text("VERSION = 2\n", encoding="utf-8")
    assert ui.empreinte_du_code() != apres  # un fichier .py compte aussi


def test_lancement_d_une_autre_version_remplace_l_ancienne_instance(tmp_path, monkeypatch, capsys):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (ancienne, http):
            ancienne.empreinte = "ancienne0000"
            port = ancienne.port

            async def fenetre_de_l_ancienne():  # ce que fait `servir` quand `fin` est levé
                await ancienne.fin.wait()
                await ancienne.arreter()

            veille = asyncio.ensure_future(fenetre_de_l_ancienne())
            nouvelle = asyncio.ensure_future(ui.servir(Calibration(), Journal(tmp_path), reel=False, port=port, navigateur=False))

            async def identite():
                return (await locale(http, port, ui.ROUTE_IDENTITE))[1]

            courante = ui.empreinte_du_code()
            fin = asyncio.get_running_loop().time() + 15
            while True:
                assert asyncio.get_running_loop().time() < fin and not nouvelle.done(), "remplacement jamais fait"
                try:
                    if (await identite()).get("empreinte") == courante:
                        break
                except (aiohttp.ClientError, OSError):
                    pass  # entre l'arrêt de l'ancienne et l'écoute de la nouvelle
                await asyncio.sleep(0.05)
            assert ancienne.ferme and veille.done()
            statut, corps = await locale(http, port, ui.ROUTE_ARRET, {"mode": "simulation"})
            assert statut == 200 and corps["ok"] is True
            assert await asyncio.wait_for(nouvelle, 10) == "interface arrêtée"

    executer(scenario())
    assert "Ancienne version remplacée" in capsys.readouterr().out


def test_remplacement_refuse_pendant_un_mouvement_ou_une_ecriture(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (ancienne, http):
            ancienne.empreinte = "ancienne0000"
            ancienne.tache = asyncio.ensure_future(asyncio.sleep(30))  # un ordre est en cours
            try:
                with pytest.raises(ui.EchecDemarrage) as erreur:
                    await ui.servir(Calibration(), Journal(tmp_path), reel=False, port=ancienne.port, navigateur=False)
                assert str(erreur.value) == "une ancienne version est en cours d'utilisation, fermez sa fenêtre"
                statut, corps = await locale(http, ancienne.port, ui.ROUTE_ARRET, {"mode": "simulation"})
                assert statut == 409 and corps["ok"] is False
                await asyncio.sleep(0.2)
                assert not ancienne.fin.is_set() and not ancienne.ferme  # l'ordre en cours n'est pas coupé
            finally:
                ancienne.tache.cancel()
                await asyncio.gather(ancienne.tache, return_exceptions=True)

    executer(scenario())


def test_route_d_arret_fermee_aux_sites_web(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            base = f"http://127.0.0.1:{serveur.port}"
            for route in (ui.ROUTE_ARRET, ui.ROUTE_IDENTITE):
                for en_plus in ({"Origin": base}, {"Origin": "http://evil.example"}, {"Referer": base + "/"}, {"Origin": "null"}):
                    statut, _corps = await locale(http, serveur.port, route, {"mode": "simulation"}, **en_plus)
                    assert statut == 403, (route, en_plus)  # un navigateur joint toujours l'un des deux à un POST
                statut, _corps = await locale(http, serveur.port, route, {"mode": "simulation"}, Host="evil.example")
                assert statut == 403
                async with http.get(base + route) as reponse:
                    assert reponse.status == 405
            for corps in ({"mode": "réel"}, {}, {"mode": 1}):
                statut, reponse = await locale(http, serveur.port, ui.ROUTE_ARRET, corps)
                assert statut == 409 and reponse["ok"] is False  # autre mode : jamais arrêtée
            await asyncio.sleep(0.2)
            assert not serveur.fin.is_set()
            statut, reponse = await locale(http, serveur.port, ui.ROUTE_ARRET, {"mode": "simulation"})
            assert statut == 200 and reponse == {"interface": "lymow", "mode": "simulation", "ok": True}
            await attendre(serveur.fin.is_set)

    executer(scenario())


def test_instance_trop_ancienne_pour_ces_routes_message_explicite(tmp_path):
    """Forme des réponses d'une interface d'avant le 2026-10-12 : ni identité, ni arrêt."""
    appels = []

    async def ancienne(requete):
        appels.append(requete.path)
        if requete.path == ui.ROUTE_REOUVERTURE:
            corps = await requete.json()
            ouverte = corps.get("mode") == "simulation"
            return web.json_response({"interface": "lymow", "mode": "simulation", "ok": ouverte}, status=200 if ouverte else 409)
        return web.json_response({"erreur": "origine refusée"}, status=403)

    async def scenario():
        application = web.Application()
        application.router.add_route("*", "/{reste:.*}", ancienne)
        lanceur = web.AppRunner(application, access_log=None)
        await lanceur.setup()
        site = web.TCPSite(lanceur, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(ui.EchecDemarrage) as erreur:
                await ui.servir(Calibration(), Journal(tmp_path), reel=False, port=port, navigateur=False)
        finally:
            await lanceur.cleanup()
        return str(erreur.value)

    message = executer(scenario())
    assert message == "une ancienne version de l'interface tourne encore : fermez sa fenêtre noire puis relancez"
    assert appels == [ui.ROUTE_IDENTITE, ui.ROUTE_REOUVERTURE]  # et sa page n'a pas été rouverte pour rien


def test_page_affiche_la_version_et_signale_l_arret_du_serveur():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    script = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert 'id="bandeau-arret"' in page and "Interface arrêtée — relancez le raccourci." in page
    assert 'id="version-serveur"' in page and "image.serveur.empreinte" in script and "image.serveur.demarre" in script
    assert '$("bandeau-arret").hidden = false' in script and "flux.onerror" in script
