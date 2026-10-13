"""Vidéo : la panne se voit (fil et journal), et une session restée levée ne bloque plus rien.

Constat réel du 2026-10-13 : « Je n'arrive plus à accéder au flux vidéo du robot », le nuage répondant bien, et
AUCUNE ligne vidéo dans le journal sur disque. Tout est rejoué hors ligne : `fournir_video` est un double.
"""

from __future__ import annotations

import json

import pytest
from conftest import lignes_journal
from test_ui import attendre, executer, lire, poster, serveur_ui

from lymow_pilotage import ui

REGLAGES = {
    "wss": "wss://m-abc.kinesisvideo.eu-west-1.amazonaws.com/?X-Amz-Signature=SIGNATURE-SECRETE-123",
    "iceServers": [{"urls": ["turn:1.2.3.4:443"], "username": "utilisateur-secret-456", "credential": "mdp-secret-789"}] * 2,
}


async def fournir():
    return REGLAGES


def etapes(dossier) -> list[str]:
    return [ligne["etape"] for ligne in lignes_journal(dossier) if ligne["evenement"] == "video-etape"]


def test_video_session_restee_levee_ne_refuse_plus_le_demarrage_suivant(tmp_path, monkeypatch, session_reelle):
    """Symptôme : après un échec côté navigateur, « une session vidéo est déjà ouverte » (409) à chaque clic."""

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            serveur.fournir_video = fournir
            for _ in range(3):  # la page n'a jamais dit « arrêtée » : chaque nouveau démarrage libère le précédent
                statut, corps, _recus = await poster(http, serveur, "/api/video/demarrer")
                assert statut == 200 and corps == REGLAGES and serveur.video_ouverte is True
            assert any("session précédente libérée" in note["texte"] for note in serveur.fil)

    executer(scenario())
    assert etapes(tmp_path).count("reglages-obtenus") == 3 and etapes(tmp_path).count("session-liberee") == 2


def test_video_sans_flux_la_session_se_libere_seule_et_c_est_ecrit(tmp_path, monkeypatch, session_reelle):
    monkeypatch.setattr(ui, "VIDEO_SANS_FLUX_S", 0.2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            serveur.fournir_video = fournir
            await poster(http, serveur, "/api/video/demarrer")
            await attendre(lambda: serveur.video_ouverte is False)
            assert any("aucun flux" in note["texte"] and "Réessayer" in note["texte"] for note in serveur.fil)
            # Avec « connexion établie » rapportée par la page, la session n'est pas libérée.
            await poster(http, serveur, "/api/video/demarrer")
            statut, _corps, _recus = await poster(http, serveur, "/api/video/etape", {"etape": "connexion-etablie"})
            assert statut == 200
            await lire(http, serveur, "/api/etat")
            import asyncio

            await asyncio.sleep(0.4)
            assert serveur.video_ouverte is True

    executer(scenario())
    assert etapes(tmp_path).count("sans-flux") == 1


def test_video_chaque_etape_au_fil_et_au_journal_sans_aucun_secret(tmp_path, monkeypatch, session_reelle):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            serveur.fournir_video = fournir
            await poster(http, serveur, "/api/video/demarrer")
            for etape in ("signalisation-ouverte", "offre-envoyee", "reponse-recue", "connexion-etablie"):
                assert (await poster(http, serveur, "/api/video/etape", {"etape": etape}))[0] == 200
            motif = "signalisation fermée (code 1006) à l'étape : offre envoyée"
            assert (await poster(http, serveur, "/api/video/etape", {"etape": "echec", "motif": motif}))[0] == 200
            assert serveur.video_ouverte is False  # un échec rapporté libère la session
            # Un motif qui porterait une adresse est retiré ; une étape inconnue, un champ en trop : refusés.
            fuite = "wss://m-abc.kinesisvideo.eu-west-1.amazonaws.com/?X-Amz-Signature=SIGNATURE-SECRETE-123"
            assert (await poster(http, serveur, "/api/video/etape", {"etape": "echec", "motif": fuite}))[0] == 200
            for corps in ({}, {"etape": "autre"}, {"etape": "echec", "motif": 3}, {"etape": "echec", "wss": "x"}):
                assert (await poster(http, serveur, "/api/video/etape", corps))[0] == 400
            return [note["texte"] for note in serveur.fil if note["texte"].startswith("vidéo")]

    fil = executer(scenario())
    assert fil[:7] == [
        "vidéo : demande des réglages au cloud Lymow…", "vidéo : session ouverte, réglages obtenus (2 serveur(s) de relais)",
        "vidéo : signalisation ouverte", "vidéo : offre envoyée", "vidéo : réponse reçue", "vidéo : connexion établie",
        "vidéo : ÉCHEC — signalisation fermée (code 1006) à l'étape : offre envoyée",
    ]
    assert fil[7] == "vidéo : ÉCHEC — motif retiré (adresse)"
    assert etapes(tmp_path) == [
        "demande", "reglages-obtenus", "signalisation-ouverte", "offre-envoyee", "reponse-recue", "connexion-etablie", "echec", "echec",
    ]
    disque = json.dumps(lignes_journal(tmp_path), ensure_ascii=False) + json.dumps(fil, ensure_ascii=False)
    for secret in ("SIGNATURE-SECRETE-123", "utilisateur-secret-456", "mdp-secret-789", "X-Amz-Signature", "kinesisvideo"):
        assert secret not in disque


def test_video_refus_de_la_route_ecrit_avec_son_code(tmp_path, monkeypatch, session_reelle):
    async def panne():
        raise ConnectionError("wss://hote/?X-Amz-Signature=fuite")

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            serveur.fournir_video = panne
            statut, corps, _recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 502 and "fuite" not in corps["erreur"] and serveur.video_ouverte is False
            serveur.suspendue = True  # main rendue : refus dit, avec le geste à faire
            statut, corps, _recus = await poster(http, serveur, "/api/video/demarrer")
            assert statut == 409 and "Prendre la main" in corps["erreur"]
            serveur.suspendue, serveur.ident = False, None
            assert (await poster(http, serveur, "/api/video/demarrer"))[0] == 503

    executer(scenario())
    refus = [ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "video-etape" and "code_http" in ligne]
    assert [ligne["code_http"] for ligne in refus] == [502, 409, 503] and "fuite" not in json.dumps(refus)


def test_page_video_bouton_etapes_delai_et_reessayer():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    onglets = page[page.index('class="onglets"') :][:1200]
    for identifiant in ("video-demarrer", "video-arreter", "video-reessayer"):
        assert f'id="{identifiant}"' in onglets  # hors des panneaux d'onglet : jamais masqués avec la caméra
    assert '<video id="video" muted playsinline></video>' in page
    assert page.index('id="vue-camera"') < page.index('id="video"') < page.index('id="vue-carte"')
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    for attendu in (
        '$("video-demarrer").addEventListener("click", demarrerVideo);', '$("video-reessayer").addEventListener("click", demarrerVideo);',
        'etapeVideo("signalisation-ouverte")', 'etapeVideo("offre-envoyee")', 'etapeVideo("reponse-recue")', 'etapeVideo("connexion-etablie")',
        '"signalisation fermée (code " + evenement.code', '"délai dépassé (75 s) à l\'étape : "', "Ordres.DELAI_VIDEO_MS",
        'pc.iceConnectionState === "failed"', 'pc.iceConnectionState === "disconnected"', '"securitypolicyviolation"',
    ):
        assert attendu in app, attendu
    assert "DELAI_VIDEO_MS: 75000" in (ui.STATIQUE / "ordres.js").read_text(encoding="utf-8")
    assert set(ui.ETAPES_VIDEO) == {"signalisation-ouverte", "offre-envoyee", "reponse-recue", "connexion-etablie", "echec"}


@pytest.mark.parametrize("nom", ["index.html", "app.js", "signalisation.js"])
def test_statiques_video_servis_depuis_la_memoire(nom):
    assert ui.charger_statiques()[nom] == (ui.STATIQUE / nom).read_text(encoding="utf-8")
