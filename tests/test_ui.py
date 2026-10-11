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


@asynccontextmanager
async def serveur_ui(tmp_path, monkeypatch, *, reel, session=None):
    """Serveur démarré sur un port libre ; `session=None` = aucun compte enregistré."""
    ouvertures = []

    def sans_compte():
        raise auth.ErreurAuth("aucune connexion enregistrée")

    @asynccontextmanager
    async def ouvrir(_journal, _cal, autorisees, *_reste):
        if session is None:
            raise AssertionError("liaison au nuage ouverte sans compte")
        ouvertures.append(autorisees)
        session.autorisees = autorisees
        yield session

    monkeypatch.setattr(liaison, "lire_identifiants", sans_compte if session is None else lambda: IDENT)
    monkeypatch.setattr(liaison, "ouvrir_session", ouvrir)
    serveur = ui.ServeurUI(Calibration(), Journal(tmp_path), reel=reel, port=0)
    serveur.ouvertures = ouvertures
    await serveur.demarrer()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            yield serveur, http
    finally:
        await serveur.arreter()


def entetes(serveur, **remplace):
    base = {"X-Lymow-Jeton": serveur.jeton, "Origin": f"http://127.0.0.1:{serveur.port}"}
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


def test_refus_sans_jeton_sur_toutes_les_routes(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True) as (serveur, http):
            for chemin in ("/", "/api/etat", "/api/flux", "/statique/app.js", "/?jeton=faux", "/inconnu"):
                statut, _texte, _entetes = await lire(http, serveur, chemin, **{"X-Lymow-Jeton": None})
                assert statut == 403, chemin
            for chemin in ("/api/previsualiser", "/api/emettre", "/api/stop", "/api/actualiser", "/api/video/demarrer"):
                for jeton in (None, "faux", serveur.jeton[:-1]):
                    statut, _corps, _entetes = await poster(http, serveur, chemin, AVANCER_1, **{"X-Lymow-Jeton": jeton})
                    assert statut == 403, chemin

    executer(scenario())


def test_echange_initial_du_jeton_contre_un_temoin_puis_page_servie(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            statut, _texte, recus = await lire(http, serveur, f"/?jeton={serveur.jeton}", **{"X-Lymow-Jeton": None})
            temoin = recus["Set-Cookie"]
            assert statut == 303 and recus["Location"] == "/"
            assert "HttpOnly" in temoin and "SameSite=Strict" in temoin
            statut, page, recus = await lire(http, serveur, "/", **{"X-Lymow-Jeton": None})  # témoin seul
            assert statut == 200 and "<title>Lymow" in page and serveur.jeton not in page
            assert recus["Cache-Control"] == "no-store" and recus["X-Content-Type-Options"] == "nosniff"
            assert recus["Referrer-Policy"] == "no-referrer" and "Access-Control-Allow-Origin" not in recus
            regles = recus["Content-Security-Policy"]
            assert "default-src 'self'" in regles and "connect-src 'self';" in regles  # sans compte : aucun hôte externe
            assert "unsafe" not in regles and "*" not in regles
            statut, _corps, _recus = await poster(http, serveur, "/api/previsualiser", AVANCER_1, **{"X-Lymow-Jeton": None})
            assert statut == 200  # le témoin suffit ensuite
            return serveur.jeton

    jeton = executer(scenario())
    assert jeton not in json.dumps(lignes_journal(tmp_path))


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
            for chemin in ("/api/previsualiser", "/api/emettre", "/api/stop", "/api/actualiser"):
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
            assert image["liaison"] == "non connecté — lancez « lymow connexion »"
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
            assert 10 <= serveur.resultat["etapes"][0]["emissions"] <= 12 and serveur.resultat["code"] == 0

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

EXTERNE = re.compile(r"(?:https?:|wss?:)?//[A-Za-z0-9]|@import|url\(", re.IGNORECASE)


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
    for interdit in ("innerHTML =", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "localStorage"):
        assert interdit not in script
    assert "history.replaceState" in script
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    assert 'type="password"' not in page and "autoplay" not in page
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
    ],
)
def test_demarrer_video_refuse_une_adresse_hors_kinesis_video_de_la_region(monkeypatch, points):
    class Detourne(FauxApiVideo):
        async def get_signaling_channel_endpoint(self, *_args, **_kwargs):
            return points

    monkeypatch.setattr(liaison, "LymowApiClient", Detourne)
    with pytest.raises(liaison.EchecOrdre, match="inattendue"):
        asyncio.run(liaison.demarrer_video(None, IDENT, liaison.Acces("identite", "acces", {})))
