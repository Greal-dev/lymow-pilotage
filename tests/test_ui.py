"""Interface web locale : serveur réel sur un port libre, liaison factice, aucun accès au nuage."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager

import aiohttp
import pytest
from conftest import ARRET, lignes_journal

from lymow_pilotage import auth, cli, liaison, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.journal import Journal
from lymow_pilotage.mouvement import ErreurSaisie
from lymow_pilotage.protocole import Categorie, est_requete_pure

IDENT = liaison.Identifiants("eu-west-1", "robot-test", "google", "raf-secret-555")
PAUSE = bytes.fromhex("10 31 28 03")
AVANCER_1 = {"ordres": [{"nom": "avancer", "valeur": 1}]}
COMPTE = ("/api/compte/commencer", "/api/compte/valider", "/api/compte/choisir", "/api/compte/deconnecter")


@asynccontextmanager
async def serveur_ui(tmp_path, monkeypatch, *, reel, session=None, coffre_factice=None):
    """Serveur démarré sur un port libre ; `session=None` = aucun compte enregistré."""
    ouvertures = []
    simultanees = [0, 0]  # ouvertes maintenant, maximum observé

    def sans_compte():
        raise auth.ErreurAuth("aucune connexion enregistrée")

    @asynccontextmanager
    async def ouvrir(_journal, _cal, autorisees, *_reste):
        if session is None:
            raise AssertionError("liaison au nuage ouverte sans compte")
        ouvertures.append(autorisees)
        session.autorisees = autorisees
        simultanees[0] += 1
        simultanees[1] = max(simultanees)
        await asyncio.sleep(0.02)  # laisse à une ouverture concurrente le temps de se montrer
        try:
            yield session
        finally:
            simultanees[0] -= 1

    if coffre_factice is None:
        monkeypatch.setattr(liaison, "lire_identifiants", sans_compte if session is None else lambda: IDENT)
    else:  # le vrai `lire_identifiants`, sur un coffre en mémoire
        from lymow_pilotage import coffre

        monkeypatch.setattr(coffre, "lire", coffre_factice.get)
        monkeypatch.setattr(coffre, "ecrire", coffre_factice.__setitem__)
        monkeypatch.setattr(coffre, "effacer", coffre_factice.clear)
    monkeypatch.setattr(liaison, "ouvrir_session", ouvrir)
    serveur = ui.ServeurUI(Calibration(), Journal(tmp_path), reel=reel, port=0)
    serveur.ouvertures = ouvertures
    serveur.simultanees = simultanees
    await serveur.demarrer()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            yield serveur, http
    finally:
        await serveur.arreter()


def entetes(serveur, **remplace):
    base = {"Cookie": f"lymow_ui={serveur.temoin}", "Origin": f"http://127.0.0.1:{serveur.port}"}
    base.update(remplace)
    return {cle: valeur for cle, valeur in base.items() if valeur is not None}


async def poster(http, serveur, chemin, corps=None, **remplace):
    async with http.post(serveur.adresse.rstrip("/") + chemin, json=corps or {}, headers=entetes(serveur, **remplace)) as r:
        return r.status, await r.json(), r.headers


async def lire(http, serveur, chemin, **remplace):
    adresse = serveur.adresse.rstrip("/") + chemin
    async with http.get(adresse, headers=entetes(serveur, Origin=None, **remplace), allow_redirects=False) as r:
        return r.status, await r.text(), r.headers


async def attendre(condition, delai=4.0):
    fin = time.monotonic() + delai
    while not condition():
        assert time.monotonic() < fin, "condition jamais atteinte"
        await asyncio.sleep(0.02)


def executer(scenario):
    return asyncio.run(scenario)


# --- écoute et cycle de vie -------------------------------------------------


def test_ecoute_sur_la_boucle_locale_uniquement_et_demarrage_arret(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            assert [hote for hote, _port in serveur.adresses_ecoute()] == ["127.0.0.1"]
            statut, _corps, _entetes = await lire(http, serveur, "/api/etat")
            assert statut == 200
            adresse = serveur.adresse
        with pytest.raises(aiohttp.ClientConnectionError):
            async with aiohttp.ClientSession() as http, http.get(adresse):
                pass

    executer(scenario())
    assert ui.HOTE == "127.0.0.1" and cli.PORT_UI == ui.PORT_DEFAUT


@pytest.mark.parametrize("arguments", [("ui", "--hote", "0.0.0.0"), ("ui", "--host", "0.0.0.0"), ("ui", "--port", "80")])
def test_aucune_option_ne_change_l_hote_ni_n_ouvre_un_port_reserve(tmp_path, arguments):
    try:
        code = cli.main([*arguments, "--donnees", str(tmp_path)])
    except SystemExit as sortie:
        code = sortie.code
    assert code == 2


def test_commande_ui_simulation_par_defaut_reel_explicite(tmp_path, monkeypatch, capsys):
    appels = []

    async def servir(_cal, _journal, *, reel, port):
        appels.append((reel, port))

    monkeypatch.setattr(ui, "servir", servir)
    assert cli.main(["ui", "--donnees", str(tmp_path)]) == 0
    assert cli.main(["ui", "--reel", "--port", "9000", "--donnees", str(tmp_path)]) == 0
    assert appels == [(False, ui.PORT_DEFAUT), (True, 9000)]


def test_servir_demarre_annonce_l_adresse_et_s_arrete_proprement(tmp_path, monkeypatch, capsys):
    def sans_compte():
        raise auth.ErreurAuth("aucune connexion enregistrée")

    monkeypatch.setattr(liaison, "lire_identifiants", sans_compte)

    async def scenario():
        tache = asyncio.ensure_future(ui.servir(Calibration(), Journal(tmp_path), reel=False, port=0, navigateur=False))
        adresse = None
        for _ in range(100):
            await asyncio.sleep(0.02)
            trouve = re.search(r"(http://127\.0\.0\.1:\d+/\?jeton=\S+)", capsys.readouterr().out)
            if trouve:
                adresse = trouve.group(1)
                break
        assert adresse, "adresse jamais annoncée"
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            async with http.get(adresse) as reponse:  # suit la redirection, témoin posé
                assert reponse.status == 200 and "jeton" not in str(reponse.url)
                assert "<title>Lymow" in await reponse.text()
        tache.cancel()  # effet de Ctrl+C sur le serveur au repos
        with pytest.raises(asyncio.CancelledError):
            await tache
        with pytest.raises(aiohttp.ClientConnectionError):
            async with aiohttp.ClientSession() as http, http.get(adresse):
                pass

    executer(scenario())


# --- jeton, Host, Origin, en-têtes ------------------------------------------


def test_refus_sans_temoin_sur_toutes_les_routes(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True) as (serveur, http):
            for chemin in ("/", "/api/etat", "/api/flux", "/statique/app.js", "/?jeton=faux", "/inconnu", "/favicon.ico"):
                statut, _texte, recus = await lire(http, serveur, chemin, Cookie=None)
                assert statut == 403, chemin
                assert "default-src 'self'" in recus["Content-Security-Policy"]  # en-têtes aussi sur un refus
                assert recus["X-Content-Type-Options"] == "nosniff" and recus["Cache-Control"] == "no-store"
            mauvais = (None, "lymow_ui=faux", f"lymow_ui={serveur.temoin[:-1]}", f"lymow_ui={serveur.jeton}")
            for chemin in ("/api/previsualiser", "/api/emettre", "/api/stop", "/api/actualiser", "/api/video/demarrer", *COMPTE):
                for temoin in mauvais:  # le jeton d'adresse lui-même n'authentifie aucune route
                    statut, _corps, _recus = await poster(http, serveur, chemin, AVANCER_1, Cookie=temoin)
                    assert statut == 403, chemin
            statut, _corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1, **{"X-Lymow-Jeton": serveur.temoin, "Cookie": None})
            assert statut == 403  # aucun en-tête ne remplace le témoin

    executer(scenario())


def test_jeton_d_adresse_a_usage_unique_echange_contre_un_temoin_distinct(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            jeton = serveur.jeton
            assert jeton and jeton != serveur.temoin and len(jeton) >= 43 and len(serveur.temoin) >= 43
            statut, _texte, recus = await lire(http, serveur, f"/?jeton={jeton}", Cookie=None)
            temoin = recus["Set-Cookie"]
            assert statut == 303 and recus["Location"] == "/"
            assert "HttpOnly" in temoin and "SameSite=Strict" in temoin
            assert f"lymow_ui={serveur.temoin}" in temoin and jeton not in temoin
            assert serveur.jeton is None
            async with aiohttp.ClientSession() as autre:  # adresse rejouée, par exemple depuis l'historique
                statut, _texte, _recus = await lire(autre, serveur, f"/?jeton={jeton}", Cookie=None)
                assert statut == 403
            statut, page, recus = await lire(http, serveur, "/", Cookie=None)  # témoin du navigateur seul
            assert statut == 200 and "<title>Lymow" in page and jeton not in page and serveur.temoin not in page
            assert recus["Cache-Control"] == "no-store" and recus["X-Content-Type-Options"] == "nosniff"
            assert recus["Referrer-Policy"] == "no-referrer" and "Access-Control-Allow-Origin" not in recus
            regles = recus["Content-Security-Policy"]
            assert "default-src 'self'" in regles and "connect-src 'self';" in regles  # sans compte : aucun hôte externe
            assert "unsafe" not in regles and "*" not in regles
            statut, _corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1, Cookie=None)
            assert statut == 200  # le témoin suffit ensuite
            statut, _texte, _recus = await lire(http, serveur, "/favicon.ico", Cookie=None)
            assert statut == 204
            return jeton, serveur.temoin

    jeton, temoin = executer(scenario())
    disque = json.dumps(lignes_journal(tmp_path))
    assert jeton not in disque and temoin not in disque


def test_csp_n_ouvre_que_la_signalisation_video_de_la_region(tmp_path, monkeypatch, session_reelle):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            _statut, _texte, recus = await lire(http, serveur, "/api/etat")
            return recus["Content-Security-Policy"]

    regles = executer(scenario())
    assert "connect-src 'self' wss://*.kinesisvideo.eu-west-1.amazonaws.com;" in regles


@pytest.mark.parametrize("hote", ["evil.example", "evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "192.168.1.10:{port}", "[::1]:{port}"])
def test_refus_d_un_host_etranger(tmp_path, monkeypatch, hote):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            statut, _texte, _recus = await lire(http, serveur, "/api/etat", Host=hote.format(port=serveur.port))
            assert statut == 403
            statut, _texte, _recus = await lire(http, serveur, "/api/etat", Host=f"localhost:{serveur.port}")
            assert statut == 200

    executer(scenario())


@pytest.mark.parametrize("origine", [None, "http://evil.example", "null", "http://127.0.0.1", "https://127.0.0.1:{port}", "http://127.0.0.1:{port}.evil.example"])
def test_refus_d_une_origine_etrangere_sur_toute_requete_modifiante(tmp_path, monkeypatch, origine):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True) as (serveur, http):
            valeur = origine.format(port=serveur.port) if origine else None
            for chemin in ("/api/previsualiser", "/api/emettre", "/api/stop", "/api/actualiser", *COMPTE):
                statut, _corps, _recus = await poster(http, serveur, chemin, AVANCER_1, Origin=valeur)
                assert statut == 403, chemin

    executer(scenario())


def test_actions_refusees_en_get_et_methodes_exotiques_refusees(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True) as (serveur, http):
            for chemin in ("/api/stop", "/api/emettre", "/api/previsualiser"):
                statut, _texte, _recus = await lire(http, serveur, chemin)
                assert statut == 405
            async with http.delete(serveur.adresse + "api/etat", headers=entetes(serveur)) as reponse:
                assert reponse.status == 405

    executer(scenario())


# --- SEC-05 et prévisualisation ---------------------------------------------


def test_sec05_serveur_en_simulation_refuse_tout_ordre_reel(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            assert serveur.ouvertures == [frozenset({Categorie.REQUETE_PURE})]  # la session elle-même est bridée
            statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1)
            assert statut == 200 and corps["identifiant"] is None and corps["bilan"]["mode"] == "simulation"
            for identifiant in ("n-importe-quoi", None, ""):
                statut, corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": identifiant, "reel": True})
                assert statut == 403 and "SEC-05" in corps["erreur"]
            statut, corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 200 and corps["bilan"]["etapes"][0]["mode"] == "simulation"
            assert serveur.tache is None

    executer(scenario())
    assert all(est_requete_pure(octets) for octets in lien.trames)


def test_simulation_sans_compte_n_ouvre_jamais_la_liaison(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            statut, texte, _recus = await lire(http, serveur, "/api/etat")
            image = json.loads(texte)
            assert image["connecte"] is False and image["etat"] is None
            assert image["liaison"] == ui.NON_CONNECTE and "Compte" in ui.NON_CONNECTE
            assert image["bornes"]["distance_max_m"] == 2.0 and image["libelles"]["duree_s"] == "durée (s)"
            statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1)
            assert statut == 200 and corps["bilan"]["etapes"][0]["deplacement_commande_m"] == 1.0
            statut, _corps, _recus = await poster(http, serveur, "/api/actualiser")
            assert statut == 200
            statut, corps, _recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 503
            assert serveur.ouvertures == []

    executer(scenario())
    assert "ordre-simule" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_emission_refusee_sans_previsualisation_valide_rejouee_ou_expiree(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            statut, corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": "invente"})
            assert statut == 409  # jamais prévisualisé

            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1)
            bon = corps["identifiant"]
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": "mauvais"})
            assert statut == 409
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": bon})
            assert statut == 409  # un essai manqué consomme la prévisualisation

            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1)
            ancien = corps["identifiant"]
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1)
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": ancien})
            assert statut == 409  # seule la dernière prévisualisation compte

            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1)
            maintenant = serveur.horloge()
            serveur.horloge = lambda: maintenant + ui.DUREE_PREVISUALISATION_S + 1
            statut, erreur, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 409 and "expirée" in erreur["erreur"]
            serveur.horloge = time.monotonic

            _statut, refuse, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 5}]})
            assert refuse["identifiant"] is None and "SEC-02" in refuse["bilan"]["motif"]
            assert lien.ordres == [] and serveur.tache is None

            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 0.3}]})
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 202
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 409  # identifiant rejoué
            await attendre(lambda: not serveur.occupe)
            assert 10 <= serveur.resultat["etapes"][0]["emissions"] <= 12 and serveur.resultat["code_sortie"] == 0

    executer(scenario())
    assert 10 <= len(lien.consignes) <= 12 and lien.arrets == [ARRET] * 3  # horloge réelle


# --- mouvement côté serveur --------------------------------------------------


async def _lancer_avancer(http, serveur, lien):
    _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1)
    statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
    assert statut == 202
    await attendre(lambda: len(lien.consignes) >= 3)


def test_stop_pendant_un_mouvement_arret_emis_et_mouvement_termine(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await _lancer_avancer(http, serveur, lien)
            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202
            en_vol = len(lien.consignes)
            assert lien.arrets == [ARRET] * 3  # l'arrêt de fin de mouvement est déjà parti
            await attendre(lambda: not serveur.occupe)
            assert len(lien.consignes) == en_vol < 40  # plus aucune consigne après STOP
            assert serveur.resultat["etapes"][0]["ordre"] == "stop"

    executer(scenario())
    assert lien.ordres[-4:] == [ARRET] * 3 + [PAUSE]  # puis le `stop` de la commande, pause comprise
    assert len(lien.arrets) == 6


def test_second_ordre_pendant_un_mouvement_refuse_sauf_stop(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await _lancer_avancer(http, serveur, lien)
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "tourner", "valeur": 90}]})
            statut, erreur, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 409 and "déjà en cours" in erreur["erreur"]
            statut, _corps, _recus = await poster(http, serveur, "/api/actualiser")
            assert statut == 409
            assert serveur.occupe
            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202

    executer(scenario())
    assert not [o for o in lien.consignes if o != lien.consignes[0]]  # aucune consigne de rotation


def test_arret_du_serveur_pendant_un_mouvement_arret_emis_avant_fermeture(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await _lancer_avancer(http, serveur, lien)
            await serveur.arreter()
            assert lien.arrets == [ARRET] * 3 and len(lien.consignes) < 40
            assert serveur.session is None and not serveur.occupe

    executer(scenario())


def test_page_fermee_le_mouvement_se_termine_seul_avec_son_arret(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 0.3}]})
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            await http.close()  # la page disparaît
            await attendre(lambda: not serveur.occupe)

    executer(scenario())
    assert 10 <= len(lien.consignes) <= 12 and lien.arrets == [ARRET] * 3  # horloge réelle


def test_interruption_clavier_pendant_un_mouvement_arrete_robot_puis_serveur(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await _lancer_avancer(http, serveur, lien)
            serveur.tache.cancel()  # effet de Ctrl+C capté par la garde du mouvement
            await asyncio.wait_for(serveur.fin.wait(), 3.0)

    executer(scenario())
    assert lien.arrets == [ARRET] * 3


# --- flux, vidéo, saisie ----------------------------------------------------


def test_flux_pousse_l_etat_a_la_page(tmp_path, monkeypatch, session_reelle):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            await attendre(lambda: session_reelle.etat.statut is not None)
            async with http.get(serveur.adresse + "api/flux", headers=entetes(serveur, Origin=None)) as reponse:
                assert reponse.headers["Content-Type"].startswith("text/event-stream")
                assert reponse.headers["Cache-Control"] == "no-store"
                ligne = await asyncio.wait_for(reponse.content.readline(), 3.0)
            return json.loads(ligne.decode().removeprefix("data: "))

    image = executer(scenario())
    assert image["mode"] == "simulation" and image["connecte"] is True and image["appareil"] == "robot-test"
    assert image["etat"]["statut_libelle"] == "prêt, en attente d'ordre" and image["etat"]["perime"] is False


def test_secrets_video_rendus_a_la_page_mais_absents_des_journaux(tmp_path, monkeypatch, session_reelle):
    secrets_video = {
        "wss": "wss://m-abc.kinesisvideo.eu-west-1.amazonaws.com/?X-Amz-Signature=SIGNATURE-SECRETE-123",
        "iceServers": [{"urls": ["turn:1.2.3.4:443"], "username": "utilisateur-secret-456", "credential": "mdp-secret-789"}],
    }

    async def fournir():
        return secrets_video

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            serveur.fournir_video = fournir
            statut, corps, recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 200 and corps == secrets_video and recus["Cache-Control"] == "no-store"
            return json.dumps(serveur.instantane(), ensure_ascii=False)

    ecran = executer(scenario())
    disque = json.dumps(lignes_journal(tmp_path), ensure_ascii=False)
    for secret in ("SIGNATURE-SECRETE-123", "utilisateur-secret-456", "mdp-secret-789", "X-Amz-Signature"):
        assert secret not in ecran and secret not in disque
    assert "vidéo : session ouverte" in ecran


@pytest.mark.parametrize(
    "corps",
    [
        None, [], {}, {"ordres": []}, {"ordres": "avancer 1"}, {"ordres": [{"nom": "tondre"}]},
        {"ordres": [{"nom": "avancer", "valeur": "1"}]}, {"ordres": [{"nom": "avancer", "valeur": True}]},
        {"ordres": [{"nom": "avancer", "valeur": 1, "reel": True}]}, {"ordres": [{"nom": "plateau", "valeur": 90.5}]},
        {"ordres": [{"nom": "stop", "valeur": 1}]}, {"ordres": [{"nom": "plateau", "valeur": 90, "vitesse": 0.1}]},
        {"ordres": [{"nom": "avancer", "valeur": 1}, {"nom": "essai-arret-auto"}]},
        {"ordres": [{"nom": "avancer", "valeur": 1}, {"nom": "reprendre"}]},
        {"ordres": [{"nom": "avancer", "valeur": 1}] * 21},
    ],
)
def test_ordres_de_la_page_revérifies_champ_par_champ(corps):
    with pytest.raises(ErreurSaisie):
        ui.ordres_depuis(corps)


def test_ordres_de_la_page_acceptes_et_convertis():
    ordres = ui.ordres_depuis(
        {"ordres": [{"nom": "plateau", "valeur": 90}, {"nom": "tourner", "valeur": None, "vitesse": -0.2, "duree": 2}]}
    )
    assert [str(o) for o in ordres] == ["plateau 90", "tourner --duree 2 --vitesse -0.2"]
    assert str(ui.ordres_depuis({"ordres": [{"nom": "essai-arret-auto", "vitesse": 0.1, "duree": 2}]})[0]) == (
        "essai-arret-auto --observation 2 --vitesse 0.1"
    )


def test_corps_non_json_refuse_proprement(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            adresse = serveur.adresse + "api/previsualiser"
            async with http.post(adresse, data=b"pas du json", headers=entetes(serveur)) as reponse:
                assert reponse.status == 400

    executer(scenario())


# --- fichiers statiques -----------------------------------------------------

# Ressource externe : adresse absolue, ou relative au protocole. « myapp://callback » n'en est pas une :
# c'est le texte de la marche à suivre, aucun navigateur ne sait le charger.
EXTERNE = re.compile(r"(?:https?:|wss?:)//|(?<!:)//[A-Za-z0-9]|@import|url\(", re.IGNORECASE)
# Toute forme d'insertion de HTML ou d'exécution de texte : affectation, « += », accès par crochets…
HTML_DYNAMIQUE = re.compile(
    r"innerHTML|outerHTML|insertAdjacentHTML|document\s*\.\s*write|\beval\s*\(|new\s+Function|srcdoc"
    r"|DOMParser|createContextualFragment|setTimeout\s*\(\s*[\"']|javascript:",
    re.IGNORECASE,
)


def test_la_page_ne_reference_aucune_ressource_externe():
    fichiers = sorted(ui.STATIQUE.iterdir())
    assert {f.name for f in fichiers} == {"index.html", *ui.FICHIERS}
    for fichier in fichiers:
        texte = fichier.read_text(encoding="utf-8")
        assert not EXTERNE.search(texte), fichier.name
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    liens = re.findall(r'(?:src|href)="([^"]*)"', page)
    assert liens and all(lien.startswith("/statique/") for lien in liens)
    assert "<script>" not in page and "onclick" not in page and " style=" not in page  # compatible avec la CSP


def test_la_page_n_insere_jamais_de_html_ni_ne_demande_d_identifiants():
    script = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    for fichier in ui.STATIQUE.iterdir():
        assert not HTML_DYNAMIQUE.search(fichier.read_text(encoding="utf-8")), fichier.name
    for forme in ('x.innerHTML += y', 'x["innerHTML"] = y', "x['outerHTML']", 'x.insertAdjacentHTML(a, b)', 'x.innerHTML=y'):
        assert HTML_DYNAMIQUE.search(forme), forme  # le détecteur voit bien toutes les formes
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "history.replaceState" in script
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    # Un seul champ masqué : l'adresse de rappel. Ni mot de passe ni adresse de compte dans la page.
    assert page.count('type="password"') == 1 and 'id="compte-adresse" type="password" autocomplete="off"' in page
    assert "mot de passe" not in page.lower() and 'type="email"' not in page and "autoplay" not in page
    assert 'target="_blank" rel="noopener noreferrer"' in page
    assert 'window.open(reponse.url, "_blank", "noopener,noreferrer")' in script
    assert "muted playsinline" in page


# --- vidéo : filtrage des réponses du nuage ---------------------------------


class FauxApiVideo:
    points = {"WSS": "wss://m-abc.kinesisvideo.eu-west-1.amazonaws.com", "HTTPS": "https://r-abc.kinesisvideo.eu-west-1.amazonaws.com"}

    def __init__(self, *_args) -> None:
        pass

    async def start_video_session(self, _appareil):
        cles = {"accessKeyId": "ASIAEXEMPLE123456", "secretAccessKey": "cle-secrete-aws", "sessionToken": "jeton-session-aws"}
        return {"channelARN": "arn:aws:kinesisvideo:eu-west-1:1:channel/x/1", "credentials": cles, "region": "eu-west-1"}

    async def get_signaling_channel_endpoint(self, *_args, **_kwargs):
        return self.points

    async def get_ice_server_config(self, *_args, **_kwargs):
        return [
            {"Uris": ["turn:1.2.3.4:443?transport=udp", "http://evil.example"], "Username": "u-ice-secret", "Password": "p-ice-secret"},
            {"Uris": "pas une liste"}, "pas un objet",
        ]

    def viewer_client_id(self):
        return "ha-lymow_abcd_userId_sub"

    def presign_signaling_url(self, wss, *_args, **_kwargs):
        return wss + "/?X-Amz-Signature=signature-presignee"


def test_demarrer_video_filtre_et_declare_les_secrets(monkeypatch):
    from lymow_pilotage import journal

    monkeypatch.setattr(liaison, "LymowApiClient", FauxApiVideo)
    reglages = asyncio.run(liaison.demarrer_video(None, IDENT, liaison.Acces("identite", "acces", {})))
    assert reglages["wss"].startswith("wss://m-abc.kinesisvideo.eu-west-1.amazonaws.com/?X-Amz-Signature=")
    assert reglages["iceServers"] == [
        {"urls": ["turn:1.2.3.4:443?transport=udp"], "username": "u-ice-secret", "credential": "p-ice-secret"}
    ]
    masque = journal.masquer(json.dumps(reglages) + " cle-secrete-aws jeton-session-aws")
    for secret in ("signature-presignee", "u-ice-secret", "p-ice-secret", "cle-secrete-aws", "jeton-session-aws"):
        assert secret not in masque


@pytest.mark.parametrize(
    "points",
    [
        {"WSS": "wss://evil.example/kinesisvideo.eu-west-1.amazonaws.com", "HTTPS": FauxApiVideo.points["HTTPS"]},
        {"WSS": FauxApiVideo.points["WSS"], "HTTPS": "https://kinesisvideo.eu-west-1.amazonaws.com.evil.example"},
        {"WSS": "ws://m-abc.kinesisvideo.eu-west-1.amazonaws.com", "HTTPS": FauxApiVideo.points["HTTPS"]},
        {"WSS": "wss://m-abc.kinesisvideo.us-east-2.amazonaws.com", "HTTPS": FauxApiVideo.points["HTTPS"]},
        {"WSS": "wss://u@m-abc.kinesisvideo.eu-west-1.amazonaws.com", "HTTPS": FauxApiVideo.points["HTTPS"]},
        {"HTTPS": FauxApiVideo.points["HTTPS"]},
        {"WSS": "wss://evil.com\\.kinesisvideo.eu-west-1.amazonaws.com", "HTTPS": FauxApiVideo.points["HTTPS"]},
        {"WSS": FauxApiVideo.points["WSS"], "HTTPS": "https://evil.com\\.kinesisvideo.eu-west-1.amazonaws.com"},
        {"WSS": FauxApiVideo.points["WSS"] + "/?x=1", "HTTPS": FauxApiVideo.points["HTTPS"]},
        {"WSS": "wss://evil.com#.kinesisvideo.eu-west-1.amazonaws.com", "HTTPS": FauxApiVideo.points["HTTPS"]},
        {"WSS": "wss://M-ABC.kinesisvideo.eu-west-1.amazonaws.com:8443", "HTTPS": FauxApiVideo.points["HTTPS"]},
    ],
)
def test_demarrer_video_refuse_une_adresse_hors_kinesis_video_de_la_region(monkeypatch, points):
    class Detourne(FauxApiVideo):
        async def get_signaling_channel_endpoint(self, *_args, **_kwargs):
            return points

    monkeypatch.setattr(liaison, "LymowApiClient", Detourne)
    with pytest.raises(liaison.EchecOrdre, match="inattendue"):
        asyncio.run(liaison.demarrer_video(None, IDENT, liaison.Acces("identite", "acces", {})))


@pytest.mark.parametrize(
    "adresse",
    [
        "wss://evil.com\\.kinesisvideo.eu-west-1.amazonaws.com", "wss://kinesisvideo.eu-west-1.amazonaws.com",
        "wss://a..kinesisvideo.eu-west-1.amazonaws.com", "wss://-a.kinesisvideo.eu-west-1.amazonaws.com",
        "wss://a.kinesisvideo.eu-west-1.amazonaws.com.", "wss://a.kinesisvideo.eu-west-1.amazonaws.com\n",
        "wss://a b.kinesisvideo.eu-west-1.amazonaws.com", "wss://é.kinesisvideo.eu-west-1.amazonaws.com",
        " wss://a.kinesisvideo.eu-west-1.amazonaws.com", "wss://a.kinesisvideo.eu-west-1xamazonaws.com", None, 12,
    ],
)
def test_hote_video_expression_stricte(adresse):
    assert liaison._hote_video(adresse, "wss", "eu-west-1") is False


def test_hote_video_accepte_les_formes_attendues():
    for adresse in ("wss://m-abc123.kinesisvideo.eu-west-1.amazonaws.com", "wss://a.b.kinesisvideo.eu-west-1.amazonaws.com:443/"):
        assert liaison._hote_video(adresse, "wss", "eu-west-1") is True
    assert liaison._hote_video("wss://a.kinesisvideo.mars-1.amazonaws.com", "wss", "mars-1") is False  # région inconnue


# --- cinquième avenant : jeton tournant, concurrence, entrées hostiles -------


class FauxHttp:
    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def test_video_relit_le_coffre_a_chaque_acces_et_ne_rafraichit_jamais_en_parallele(tmp_path, monkeypatch):
    from lymow_pilotage import coffre
    from lymow_pilotage.vendor.ha_lymow.auth import LymowAuth

    coffre_factice = {"jeton-rafraichissement": "jeton-tour-1", "region": "eu-west-1", "appareil": "robot-test", "methode": "google"}
    presentes, vus, en_cours = [], [], [0, 0]

    async def rafraichir(_http, jeton, _region, _methode):
        en_cours[0] += 1
        en_cours[1] = max(en_cours)
        presentes.append(jeton)
        await asyncio.sleep(0.03)
        en_cours[0] -= 1
        return {"AccessToken": "acces-court-1", "IdToken": "identite-courte-1", "RefreshToken": f"jeton-tour-{len(presentes) + 1}"}

    async def cles_aws(_self, _id_token, _region):
        return {"identity_id": "identite-aws-1", "credentials": {"AccessKeyId": "ASIAEXEMPLE1234", "SecretKey": "cle-1", "SessionToken": "s-1"}}

    async def video(_http, ident, _acces):
        vus.append(ident.jeton)
        return {"wss": "wss://m.kinesisvideo.eu-west-1.amazonaws.com", "iceServers": []}

    monkeypatch.setattr(coffre, "lire", coffre_factice.get)
    monkeypatch.setattr(coffre, "ecrire", coffre_factice.__setitem__)
    monkeypatch.setattr(auth, "rafraichir", rafraichir)
    monkeypatch.setattr(LymowAuth, "get_aws_credentials", cles_aws)
    monkeypatch.setattr(liaison, "demarrer_video", video)
    serveur = ui.ServeurUI(Calibration(), Journal(tmp_path), reel=False, port=0)
    serveur.ouvrir_http = FauxHttp

    async def scenario():
        await serveur._video()  # le vrai chemin : coffre, rafraîchissement, vidéo
        await asyncio.gather(serveur._video(), serveur._video())

    executer(scenario())
    assert presentes == ["jeton-tour-1", "jeton-tour-2", "jeton-tour-3"]  # jamais deux fois le même jeton
    assert vus == presentes and en_cours[1] == 1
    assert coffre_factice["jeton-rafraichissement"] == "jeton-tour-4"


def test_video_second_demarrage_refuse_tant_que_la_page_tient_la_session(tmp_path, monkeypatch, session_reelle):
    appels = []

    async def fournir():
        appels.append(True)
        await asyncio.sleep(0.02)
        return {"wss": "wss://m.kinesisvideo.eu-west-1.amazonaws.com", "iceServers": []}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            serveur.fournir_video = fournir
            premiers = await asyncio.gather(*(poster(http, serveur, "/api/video/demarrer") for _ in range(2)))
            assert sorted(statut for statut, _corps, _recus in premiers) == [200, 409]
            statut, corps, _recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 409 and "déjà ouverte" in corps["erreur"]
            statut, _corps, _recus = await poster(http, serveur, "/api/video/arreter")
            assert statut == 200
            statut, _corps, _recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 200
            await lire(http, serveur, "/")  # page rechargée : l'ancienne session n'existe plus
            statut, _corps, _recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 200

    executer(scenario())
    assert len(appels) == 3  # le cloud n'a été contacté qu'une fois par démarrage accepté


def test_video_echec_ne_bloque_pas_le_demarrage_suivant(tmp_path, monkeypatch, session_reelle):
    async def panne():
        raise ConnectionError("wss://hote/?X-Amz-Signature=fuite")

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            serveur.fournir_video = panne
            for _ in range(2):
                statut, corps, recus = await poster(http, serveur, "/api/video/demarrer")
                assert statut == 502 and "X-Amz" not in corps["erreur"]
                assert "default-src 'self'" in recus["Content-Security-Policy"]

    executer(scenario())


def test_stop_contre_emission_simultanee_une_seule_tache_et_pas_de_second_mouvement(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await _lancer_avancer(http, serveur, lien)
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "tourner", "valeur": 90}]})
            stop, emission = await asyncio.gather(
                poster(http, serveur, "/api/stop"),
                poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]}),
            )
            assert stop[0] == 202 and emission[0] == 409
            await attendre(lambda: not serveur.occupe)
            assert serveur.resultat["etapes"][0]["ordre"] == "stop"
            taches = [t for t in asyncio.all_tasks() if t.get_coro().__qualname__.endswith("_conduire")]
            assert taches == []  # aucune tâche orpheline

    executer(scenario())
    assert len(set(lien.consignes)) == 1 and len(lien.arrets) == 6  # aucune consigne de rotation


def test_double_emission_simultanee_un_seul_ordre_part(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 0.3}]})
            reponses = await asyncio.gather(
                *(poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]}) for _ in range(3))
            )
            assert sorted(statut for statut, _corps, _recus in reponses) == [202, 409, 409]
            await attendre(lambda: not serveur.occupe)

    executer(scenario())
    assert 10 <= len(lien.consignes) <= 12 and lien.arrets == [ARRET] * 3  # horloge réelle


def test_double_actualisation_ne_rouvre_jamais_deux_liaisons_a_la_fois(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            lien.connecte = False  # liaison tombée : chaque actualisation tente une reconnexion
            reponses = await asyncio.gather(*(poster(http, serveur, "/api/actualiser") for _ in range(3)))
            assert [statut for statut, _corps, _recus in reponses] == [200, 200, 200]
            assert len(serveur.ouvertures) == 4 and serveur.simultanees[1] == 1

    executer(scenario())


def test_lancer_refuse_une_seconde_tache(tmp_path, monkeypatch, session_reelle, lien):
    from aiohttp import web

    from lymow_pilotage.mouvement import Ordre

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await _lancer_avancer(http, serveur, lien)
            premiere = serveur.tache
            with pytest.raises(web.HTTPConflict):
                serveur._lancer([Ordre("tourner", 90.0)])
            assert serveur.tache is premiere

    executer(scenario())


@pytest.mark.parametrize(
    "corps",
    [
        '{"ordres":[{"nom":"avancer","valeur":1' + "0" * 400 + "}]}",
        '{"ordres":[{"nom":"avancer","valeur":' + "9" * 5000 + "}]}",
        '{"ordres":[{"nom":"avancer","valeur":1e400}]}',
        '{"ordres":[{"nom":"avancer","valeur":-1e999}]}',
        '{"ordres":[{"nom":"avancer","duree":1' + "0" * 400 + "}]}",
        '{"ordres":[{"nom":"plateau","valeur":9' + "0" * 400 + "}]}",
        '{"ordres":[{"nom":"avancer","valeur":NaN}]}',
        "[" * 5000 + "]" * 5000,
    ],
)
def test_entrees_numeriques_demesurees_refusees_proprement(tmp_path, monkeypatch, corps):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            adresse = serveur.adresse + "api/previsualiser"
            async with http.post(adresse, data=corps.encode(), headers=entetes(serveur)) as reponse:
                assert reponse.status == 400, await reponse.text()
                assert "erreur" in await reponse.json()
                assert "default-src 'self'" in reponse.headers["Content-Security-Policy"]

    executer(scenario())


@pytest.mark.parametrize("nom", ["Host", "Origin", "Cookie"])
def test_entete_non_ascii_refuse_proprement(tmp_path, monkeypatch, nom):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            statut, corps, recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1, **{nom: "éèà-ü"})
            assert statut == 400 and "erreur" in corps
            assert recus["X-Content-Type-Options"] == "nosniff"

    executer(scenario())


def test_resultat_et_signalements_filtres_comme_le_journal(tmp_path, monkeypatch, session_reelle, capsys):
    from lymow_pilotage import conduite, journal
    from lymow_pilotage.mouvement import Ordre

    journal.declarer_secret("valeur-secrete-424242")

    async def emettre(_session, _ordres, _journal):
        return {"etapes": [{"ordre": "avancer 1", "constat": "fuite valeur-secrete-424242"}], "interrompue": False}, 0

    monkeypatch.setattr(conduite, "emettre_ordres", emettre)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, _http):
            serveur._lancer([Ordre("avancer", 1.0)])
            await attendre(lambda: not serveur.occupe)
            serveur._signaler("alerte avec valeur-secrete-424242")
            return json.dumps(serveur.instantane(), ensure_ascii=False)

    ecran = executer(scenario())
    assert "valeur-secrete-424242" not in ecran and "valeur-secrete-424242" not in capsys.readouterr().err
    assert "[SECRET]" in ecran


# --- sixième avenant : connexion au compte depuis la page --------------------

JETONS = {"access_token": "acces-court-aaa111", "id_token": "identite-courte-bbb222", "refresh_token": "raf-durable-ccc333"}


class ReponseOAuth:
    def __init__(self, statut=200, donnees=None):
        self.status, self.ok, self._donnees = statut, statut < 400, JETONS if donnees is None else donnees

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def json(self, content_type=None):
        return self._donnees

    async def read(self):
        return b'{"error":"invalid_grant","detail":"fuite-corps-ddd444"}'


class HttpOAuth(FauxHttp):
    """Double du service de jetons : enregistre ce que le serveur local lui envoie."""

    envois: list = []
    statut = 200

    def post(self, adresse, **kwargs):
        HttpOAuth.envois.append((adresse, kwargs))
        return ReponseOAuth(HttpOAuth.statut)


class ApiAppareils:
    appareils = [{"deviceThingName": "robot-test", "deviceName": "Tondeuse"}]

    def __init__(self, *_args):
        pass

    async def get_devices(self):
        return ApiAppareils.appareils


@pytest.fixture
def service_compte(monkeypatch):
    """Doubles de tout ce qui parlerait au nuage pendant une connexion."""
    from lymow_pilotage import compte
    from lymow_pilotage.vendor.ha_lymow.auth import LymowAuth

    async def cles_aws(_self, _id_token, _region):
        return {"identity_id": "identite-aws-eee555", "credentials": {}}

    HttpOAuth.envois, HttpOAuth.statut = [], 200
    ApiAppareils.appareils = [{"deviceThingName": "robot-test", "deviceName": "Tondeuse"}]
    monkeypatch.setattr(LymowAuth, "get_aws_credentials", cles_aws)
    monkeypatch.setattr(compte, "LymowApiClient", ApiAppareils)
    return HttpOAuth


def rappel(serveur, code="CODE-OAUTH-fff666", etat=None):
    etat = serveur.tentative[0].etat if etat is None else etat
    return {"adresse": f"myapp://callback/?code={code}&state={etat}"}


def test_compte_tentative_pkce_et_state_gardes_dans_le_serveur(tmp_path, monkeypatch, capsys):
    from urllib.parse import parse_qs, urlparse

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, coffre_factice={}) as (serveur, http):
            statut, texte, _recus = await lire(http, serveur, "/api/etat")
            image = json.loads(texte)
            assert image["compte"] == {"connecte": False, "region": None, "appareil": None, "tentative": False, "choix": None}
            assert image["regions"] == list(auth.REGIONS) and image["region_defaut"] == "eu-west-1"
            statut, erreur, _recus = await poster(http, serveur, "/api/compte/commencer", {"region": "mars-1"})
            assert statut == 400 and serveur.tentative is None
            statut, corps, recus = await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            assert statut == 200 and recus["Cache-Control"] == "no-store"
            demande = serveur.tentative[0]
            q = {cle: v[0] for cle, v in parse_qs(urlparse(corps["url"]).query).items()}
            assert corps["url"].startswith("https://eu-auth.lymow.com/oauth2/authorize?")
            assert q["code_challenge_method"] == "S256" and q["state"] == demande.etat and len(q["state"]) >= 32
            assert q["redirect_uri"] == "myapp://callback/" and q["identity_provider"] == "Google"
            assert demande.verificateur not in json.dumps(corps)
            premier = demande.etat
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            assert serveur.tentative[0].etat != premier  # une seule tentative active
            image = serveur.instantane()
            assert image["compte"]["tentative"] is True
            return corps["url"], demande.verificateur, json.dumps(image, ensure_ascii=False)

    url, verificateur, ecran = executer(scenario())
    disque = json.dumps(lignes_journal(tmp_path), ensure_ascii=False)
    sortie = capsys.readouterr()
    for secret in (url, verificateur, "oauth2/authorize"):
        assert secret not in disque and secret not in ecran and secret not in sortie.out + sortie.err


@pytest.mark.parametrize(
    ("adresse", "motif"),
    [
        ("myapp://callback/?code=CODE-OAUTH-fff666&state=un-autre-etat", "state"),
        ("myapp://callback/?code=CODE-OAUTH-fff666", "state"),
        ("CODE-OAUTH-fff666", "adresse de rappel complète"),
        ("https://evil.example/?code=CODE-OAUTH-fff666&state=x", "adresse de rappel complète"),
        ("myapp://callback/?error=access_denied&state=x", "erreur"),
        (12, "adresse de rappel attendue"),
    ],
)
def test_compte_adresse_au_state_different_ou_absent_refusee_et_tentative_consommee(
    tmp_path, monkeypatch, service_compte, adresse, motif
):
    coffre_factice = {}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, coffre_factice=coffre_factice) as (serveur, http):
            serveur.ouvrir_http = service_compte
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            bonne = rappel(serveur)
            statut, corps, _recus = await poster(http, serveur, "/api/compte/valider", {"adresse": adresse})
            assert statut == 400 and corps["erreur"].startswith("connexion refusée") and motif in corps["erreur"]
            assert "CODE-OAUTH" not in corps["erreur"] and serveur.tentative is None
            statut, corps, _recus = await poster(http, serveur, "/api/compte/valider", bonne)
            assert statut == 400 and "aucune tentative" in corps["erreur"]  # usage unique, même après un refus
            return json.dumps(serveur.instantane(), ensure_ascii=False)

    ecran = executer(scenario())
    assert coffre_factice == {} and service_compte.envois == []  # rien écrit, service jamais contacté
    assert "CODE-OAUTH" not in ecran and "CODE-OAUTH" not in json.dumps(lignes_journal(tmp_path))


def test_compte_refus_sans_tentative_ou_tentative_expiree(tmp_path, monkeypatch, service_compte):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, coffre_factice={}) as (serveur, http):
            serveur.ouvrir_http = service_compte
            statut, corps, _recus = await poster(http, serveur, "/api/compte/valider", {"adresse": "myapp://callback/?code=a&state=b"})
            assert statut == 400 and "aucune tentative" in corps["erreur"]
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            bonne = rappel(serveur)
            maintenant = serveur.horloge()
            serveur.horloge = lambda: maintenant + ui.DUREE_TENTATIVE_S + 1
            assert serveur.instantane()["compte"]["tentative"] is False
            statut, corps, _recus = await poster(http, serveur, "/api/compte/valider", bonne)
            assert statut == 400 and "expirée" in corps["erreur"]
            statut, corps, _recus = await poster(http, serveur, "/api/compte/choisir", {"appareil": "robot-test"})
            assert statut == 400 and "aucun choix" in corps["erreur"]

    executer(scenario())
    assert service_compte.envois == []


def test_compte_echange_reussi_coffre_ecrit_liaison_rouverte_et_aucun_secret_ne_sort(
    tmp_path, monkeypatch, service_compte, session_reelle, capsys
):
    coffre_factice = {}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle, coffre_factice=coffre_factice) as (serveur, http):
            serveur.ouvrir_http = service_compte
            assert serveur.ouvertures == [] and serveur.ident is None  # démarré sans compte : aucune liaison
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            demande = serveur.tentative[0]
            envoi = rappel(serveur)
            statut, corps, recus = await poster(http, serveur, "/api/compte/valider", envoi)
            assert statut == 200 and corps == {"connecte": True, "liaison": "connecté"}
            assert recus["Cache-Control"] == "no-store"
            assert serveur.tentative is None and serveur.choix is None
            assert serveur.ouvertures == [frozenset({Categorie.REQUETE_PURE})]  # liaison rouverte, bridée (simulation)
            image = serveur.instantane()
            assert image["compte"]["connecte"] is True and image["compte"]["appareil"] == "robot-test"
            assert image["connecte"] is True
            statut, corps2, _recus = await poster(http, serveur, "/api/compte/valider", envoi)
            assert statut == 400  # adresse rejouée
            return demande, envoi["adresse"], json.dumps([corps, corps2, image], ensure_ascii=False)

    demande, adresse, reponses = executer(scenario())
    assert coffre_factice == {
        "jeton-rafraichissement": "raf-durable-ccc333", "region": "eu-west-1", "appareil": "robot-test", "methode": "google",
    }
    (envoi,) = service_compte.envois  # un seul échange, avec le vérificateur gardé par le serveur
    assert envoi[0] == "https://eu-auth.lymow.com/oauth2/token"
    assert envoi[1]["data"]["code"] == "CODE-OAUTH-fff666" and envoi[1]["data"]["code_verifier"] == demande.verificateur
    disque = json.dumps(lignes_journal(tmp_path), ensure_ascii=False)
    sortie = capsys.readouterr()
    secrets_ = (
        adresse, "CODE-OAUTH-fff666", demande.verificateur, demande.etat, "acces-court-aaa111",
        "identite-courte-bbb222", "raf-durable-ccc333", "identite-aws-eee555",
    )
    for secret in secrets_:
        assert secret not in disque and secret not in reponses and secret not in sortie.out + sortie.err
    assert "connexion réussie" in reponses and "compte-enregistre" in disque


def test_compte_echec_de_l_echange_message_generique_et_nouvelle_tentative_possible(tmp_path, monkeypatch, service_compte):
    coffre_factice = {}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, coffre_factice=coffre_factice) as (serveur, http):
            serveur.ouvrir_http = service_compte
            service_compte.statut = 400  # code expiré côté service
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            statut, corps, _recus = await poster(http, serveur, "/api/compte/valider", rappel(serveur))
            assert statut == 400 and "recommencez" in corps["erreur"]
            assert "fuite-corps" not in corps["erreur"] and "CODE-OAUTH" not in corps["erreur"]
            statut, _corps, _recus = await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            assert statut == 200 and serveur.tentative is not None
            return json.dumps(serveur.instantane(), ensure_ascii=False)

    ecran = executer(scenario())
    assert coffre_factice == {} and "connexion refusée" in ecran and "fuite-corps" not in ecran


def test_compte_plusieurs_appareils_meme_liste_que_la_commande(tmp_path, monkeypatch, service_compte, session_reelle):
    coffre_factice = {}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle, coffre_factice=coffre_factice) as (serveur, http):
            serveur.ouvrir_http = service_compte
            ApiAppareils.appareils = [
                {"deviceThingName": "robot-jardin", "deviceName": "Jardin"},
                {"deviceThingName": "robot-test", "deviceName": "Verger", "sn": "ne-sort-pas"},
            ]
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            statut, corps, _recus = await poster(http, serveur, "/api/compte/valider", rappel(serveur))
            attendus = [{"nom": "Jardin", "appareil": "robot-jardin"}, {"nom": "Verger", "appareil": "robot-test"}]
            assert statut == 200 and corps == {"appareils": attendus}
            assert coffre_factice == {} and serveur.instantane()["compte"]["choix"] == attendus
            assert "raf-durable" not in json.dumps(serveur.instantane())
            statut, _corps, _recus = await poster(http, serveur, "/api/compte/choisir", {"appareil": "robot-inconnu"})
            assert statut == 400 and coffre_factice == {}
            statut, corps, _recus = await poster(http, serveur, "/api/compte/choisir", {"appareil": "robot-test"})
            assert statut == 200 and corps["connecte"] is True and serveur.choix is None

    executer(scenario())
    assert coffre_factice["appareil"] == "robot-test" and coffre_factice["jeton-rafraichissement"] == "raf-durable-ccc333"


def test_compte_deconnexion_efface_le_coffre_et_ferme_la_liaison(tmp_path, monkeypatch, session_reelle):
    coffre_factice = {"jeton-rafraichissement": "raf-durable-ccc333", "region": "eu-west-1", "appareil": "robot-test", "methode": "google"}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle, coffre_factice=coffre_factice) as (serveur, http):
            assert serveur.instantane()["compte"]["connecte"] is True and len(serveur.ouvertures) == 1
            statut, corps, _recus = await poster(http, serveur, "/api/compte/deconnecter")
            assert statut == 200 and corps == {"connecte": False}
            image = serveur.instantane()
            assert image["compte"]["connecte"] is False and image["connecte"] is False and image["etat"] is None
            assert image["liaison"] == ui.NON_CONNECTE and serveur.simultanees[0] == 0  # liaison fermée

    executer(scenario())
    assert coffre_factice == {}
    assert "compte-oublie" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_compte_routes_refusees_pendant_un_mouvement(tmp_path, monkeypatch, session_reelle, lien, service_compte):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            serveur.ouvrir_http = service_compte
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            envoi = rappel(serveur)
            await _lancer_avancer(http, serveur, lien)
            for chemin, corps in (
                ("/api/compte/commencer", {"region": "eu-west-1"}), ("/api/compte/valider", envoi),
                ("/api/compte/choisir", {"appareil": "robot-test"}), ("/api/compte/deconnecter", {}),
            ):
                statut, erreur, _recus = await poster(http, serveur, chemin, corps)
                assert statut == 409 and "ordre est en cours" in erreur["erreur"], chemin
            assert serveur.tentative is not None and serveur.occupe  # tentative intacte, mouvement intact

    executer(scenario())
    assert service_compte.envois == []


def test_csp_inchangee_par_la_connexion_et_sans_entrave_a_un_nouvel_onglet(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, coffre_factice={}) as (serveur, http):
            _statut, _corps, recus = await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            return recus["Content-Security-Policy"]

    regles = executer(scenario())
    assert "connect-src 'self';" in regles and "lymow.com" not in regles and "google" not in regles.lower()
    # Rien qui bloquerait l'ouverture d'un onglet : ni bac à sable, ni restriction de navigation.
    assert "sandbox" not in regles and "navigate-to" not in regles


# --- septième avenant : instance unique et réouverture de la page ------------


async def rouvrir(http, serveur, mode="simulation", **entetes_en_plus):
    adresse = serveur.adresse.rstrip("/") + ui.ROUTE_REOUVERTURE
    async with http.post(adresse, json={"mode": mode}, headers=entetes_en_plus) as reponse:
        return reponse.status, await reponse.json(), reponse.headers


def test_second_lancement_du_meme_mode_fait_rouvrir_la_page_par_l_instance_en_place(tmp_path, monkeypatch, capsys):
    ouvertes = []

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            serveur.ouvrir_navigateur = lambda adresse: ouvertes.append(adresse) or True
            ancien_jeton, temoin = serveur.jeton, serveur.temoin
            message = await ui.servir(Calibration(), Journal(tmp_path), reel=False, port=serveur.port, navigateur=False)
            assert message == "interface déjà ouverte : page rouverte dans le navigateur"
            (adresse,) = ouvertes  # c'est l'instance en place qui a appelé le navigateur
            assert adresse.startswith(serveur.adresse + "?jeton=") and ancien_jeton not in adresse
            assert serveur.temoin == temoin  # la session existante reste valable
            statut, _texte, recus = await lire(http, serveur, adresse.removeprefix(serveur.adresse.rstrip("/")), Cookie=None)
            assert statut == 303 and f"lymow_ui={temoin}" in recus["Set-Cookie"]  # le nouveau jeton ouvre bien la page
            statut, _texte, _recus = await lire(http, serveur, "/api/etat")
            assert statut == 200
            return adresse

    adresse = executer(scenario())
    sortie = capsys.readouterr()
    assert adresse not in sortie.out + sortie.err  # le jeton ne passe ni par la réponse ni par la console
    assert cli.main(["ui", "--donnees", str(tmp_path), "--port", "1"]) == 2  # garde de port intacte


def test_second_lancement_par_la_commande_code_zero(tmp_path, monkeypatch, capsys):
    ouvertes = []

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, _http):
            serveur.ouvrir_navigateur = lambda adresse: ouvertes.append(adresse) or True
            from lymow_pilotage import config

            cal = config.charger_calibration(tmp_path)
            return await cli._commande_ui(
                cli.construire_analyseur().parse_args(["ui", "--port", str(serveur.port)]),
                cli.Contexte(cal, Journal(tmp_path), 75.0),
            )

    resultat, code = executer(scenario())
    assert code == 0 and resultat == {"interface": "interface déjà ouverte : page rouverte dans le navigateur"}
    assert len(ouvertes) == 1


@pytest.mark.parametrize(("en_place", "demande", "noms"), [(True, False, ("réel", "simulation")), (False, True, ("simulation", "réel"))])
def test_second_lancement_d_un_autre_mode_refuse_sans_rouvrir(tmp_path, monkeypatch, en_place, demande, noms):
    ouvertes = []

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=en_place) as (serveur, _http):
            serveur.ouvrir_navigateur = lambda adresse: ouvertes.append(adresse) or True
            with pytest.raises(ui.EchecDemarrage) as erreur:
                await ui.servir(Calibration(), Journal(tmp_path), reel=demande, port=serveur.port, navigateur=False)
            return str(erreur.value)

    message = executer(scenario())
    assert message == f"une interface en mode {noms[0]} tourne déjà ; fermez sa fenêtre avant de lancer le mode {noms[1]}"
    assert ouvertes == []


def test_port_occupe_par_un_autre_programme_message_dedie(tmp_path, monkeypatch):
    from aiohttp import web

    async def tiers(_requete):
        return web.json_response({"interface": "autre chose", "mode": "simulation", "ok": True})

    async def scenario():
        application = web.Application()
        application.router.add_route("*", "/{reste:.*}", tiers)
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
        return port, str(erreur.value)

    port, message = executer(scenario())
    assert f"le port {port} est occupé par un autre programme" in message and "--port" in message


def test_route_de_reouverture_fermee_aux_sites_web_et_limitee_en_frequence(tmp_path, monkeypatch):
    ouvertes = []

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            serveur.ouvrir_navigateur = lambda adresse: ouvertes.append(adresse) or True
            base = f"http://127.0.0.1:{serveur.port}"
            for en_plus in ({"Origin": base}, {"Origin": "http://evil.example"}, {"Referer": base + "/"}, {"Origin": "null"}):
                statut, _corps, recus = await rouvrir(http, serveur, **en_plus)
                assert statut == 403, en_plus  # un navigateur joint toujours l'un des deux à un POST
                assert "default-src 'self'" in recus["Content-Security-Policy"]
            for hote in ("evil.example", f"evil.example:{serveur.port}", "127.0.0.1:1"):
                statut, _corps, _recus = await rouvrir(http, serveur, Host=hote)
                assert statut == 403
            async with http.get(serveur.adresse.rstrip("/") + ui.ROUTE_REOUVERTURE) as reponse:
                assert reponse.status == 405
            assert ouvertes == []

            statut, corps, _recus = await rouvrir(http, serveur, mode="réel")
            assert statut == 409 and corps == {"interface": "lymow", "mode": "simulation", "ok": False}
            for inattendu in (None, 12, "RÉEL", ["simulation"]):
                statut, _corps, _recus = await rouvrir(http, serveur, mode=inattendu)
                assert statut == 409
            assert ouvertes == []

            statut, corps, _recus = await rouvrir(http, serveur)
            assert statut == 200 and corps == {"interface": "lymow", "mode": "simulation", "ok": True}  # rien de sensible
            statut, corps, _recus = await rouvrir(http, serveur)
            assert statut == 429 and corps["ok"] is False and len(ouvertes) == 1  # moins de 3 s après
            with pytest.raises(ui.EchecDemarrage, match="réessayez"):
                await ui.rejoindre(serveur.port, False)
            maintenant = serveur.horloge()
            serveur.horloge = lambda: maintenant + ui.DELAI_REOUVERTURE_S + 0.1
            statut, _corps, _recus = await rouvrir(http, serveur)
            assert statut == 200 and len(ouvertes) == 2 and ouvertes[0] != ouvertes[1]
            jeton_perime = ouvertes[0].split("jeton=")[1]
            statut, _texte, _recus = await lire(http, serveur, f"/?jeton={jeton_perime}", Cookie=None)
            assert statut == 403  # seul le dernier jeton d'adresse vaut, et une seule fois

    executer(scenario())
    assert ui.DELAI_REOUVERTURE_S >= 3


def test_reouverture_pendant_un_mouvement_ne_le_touche_pas(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            serveur.ouvrir_navigateur = lambda _adresse: True
            await _lancer_avancer(http, serveur, lien)
            tache, temoin = serveur.tache, serveur.temoin
            statut, corps, _recus = await rouvrir(http, serveur, mode="réel")
            assert statut == 200 and corps["ok"] is True
            assert serveur.tache is tache and serveur.occupe and serveur.temoin == temoin
            statut, _corps, _recus = await poster(http, serveur, "/api/stop")  # l'ancienne page pilote toujours
            assert statut == 202

    executer(scenario())


def test_navigateur_introuvable_adresse_affichee_et_serveur_maintenu(tmp_path, monkeypatch, capsys):
    def sans_navigateur(_adresse):
        raise OSError("aucun navigateur")

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            serveur.ouvrir_navigateur = sans_navigateur
            assert serveur.ouvrir_page() is False
            statut, _texte, _recus = await lire(http, serveur, "/api/etat")
            assert statut == 200  # le serveur tourne toujours
            with pytest.raises(ui.EchecDemarrage, match="navigateur n'a pas pu être lancé"):
                await ui.rejoindre(serveur.port, False)
            return serveur.jeton

    jeton = executer(scenario())
    sortie = capsys.readouterr().out
    assert "Le navigateur n'a pas pu être ouvert automatiquement." in sortie and f"?jeton={jeton}" in sortie


@pytest.mark.parametrize(("interactif", "attendu"), [(True, 1), (False, 0)])
def test_erreur_de_demarrage_fenetre_retenue_seulement_en_console_interactive(tmp_path, monkeypatch, capsys, interactif, attendu):
    import sys

    invites = []

    async def servir(_cal, _journal, *, reel, port):
        raise ui.EchecDemarrage("le port 8377 est occupé par un autre programme")

    monkeypatch.setattr(ui, "servir", servir)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: interactif, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: interactif, raising=False)
    monkeypatch.setattr("builtins.input", lambda invite="": invites.append(invite) or "")
    assert cli.main(["ui", "--donnees", str(tmp_path)]) == 1
    assert "le port 8377 est occupé" in capsys.readouterr().err
    assert len(invites) == attendu and (not invites or invites[0] == "Appuyez sur Entrée pour fermer.")


def test_aucune_pause_pour_les_autres_commandes_meme_en_console_interactive(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda invite="": (_ for _ in ()).throw(AssertionError("pause inattendue")))
    assert cli.main(["sequence", "voler 3", "--donnees", str(tmp_path)]) == 2


def test_la_page_renvoie_au_panneau_compte_et_donne_l_astuce_de_la_console():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    script = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert "lymow connexion" not in page + script
    assert "onglet « Console »" in page and "Failed to launch" in page
