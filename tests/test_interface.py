"""Écran unique de pilotage, sections repliées, bascule unique des flèches, pause et refus actionnables.

Retours du propriétaire (2026-10-12) : « je dois pouvoir activer le contrôle par les flèches au même
endroit que la vidéo », « je ne vois pas le bouton » ; et, robot en tonte : un refus qui ne disait
pas quoi faire.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from conftest import ARRET, est_consigne, lignes_journal
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import cli, mouvement, securite, ui
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.securite import RefusSecurite

PAUSE = bytes.fromhex("10 31 28 03")
PAUSE_RETOUR = bytes.fromhex("10 31 28 15")
EN_TONTE = {"robotState": 2, "workStatus": 2, "battery": 80}
EN_PAUSE = {"robotState": 3, "workStatus": 2, "battery": 80}
PAGE = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")


def robot_obeit_a_la_pause(lien, nouveau=EN_PAUSE):
    def sur_publication(lien_, octets):
        if octets in (PAUSE, PAUSE_RETOUR):
            lien_.robot = dict(nouveau)

    lien.sur_publication = sur_publication


# --- page : un écran, le reste replié ------------------------------------------------


def test_sections_repliees_par_defaut_et_ecran_principal_dans_un_seul_bloc():
    volets = re.findall(r'<details id="volet-([a-z]+)" class="([^"]+)" data-volet="([a-z]+)"( open)?>\s*<summary>([^<]+)</summary>', PAGE)
    assert [(nom, titre) for nom, _classe, _cle, _ouvert, titre in volets] == [
        ("details", "Détails de la prévisualisation"), ("carte", "Ajustement de zone"),
        ("ordres", "Ordres détaillés"), ("calibration", "Calibration"), ("compte", "Compte"),
        ("journal", "Journal et résultat détaillé"),
    ]
    assert all(ouvert == "" and nom == cle for nom, _classe, cle, ouvert, _titre in volets)  # FERMÉES par défaut
    assert "<details open" not in PAGE and " open>" not in PAGE
    # Chaque fonction est toujours là, rangée dans sa section.
    for volet, identifiants in {
        "carte": ("carte-ajustement", "carte-zone"), "compte": ("compte-connecter", "un-clic-activer"),
        "journal": ("journal", "resultat-detail", "etat-liste", "actualiser"), "ordres": ("sequence-liste", "sequence-previsualiser"),
    }.items():
        debut = PAGE.index(f'id="volet-{volet}"')
        fin = PAGE.index("</details>", debut)
        for identifiant in identifiants:
            assert debut < PAGE.index(f'id="{identifiant}"') < fin, (volet, identifiant)
    calibration = PAGE[PAGE.index('id="volet-calibration"') : PAGE.index('id="volet-compte"')]
    assert 'data-ordre="essai-arret-auto"' in calibration
    ordres = PAGE[PAGE.index('id="volet-ordres"') : PAGE.index('id="volet-calibration"')]
    assert all(f'data-ordre="{nom}"' in ordres for nom in ("tourner", "translation", "plateau"))
    assert all(f'data-tache="{nom}"' in ordres for nom in ("pause", "reprendre", "base", "annuler"))
    # L'écran principal : deux colonnes — commandes à GAUCHE (dans l'ordre demandé), caméra ou carte à DROITE.
    principal = PAGE[PAGE.index('id="bloc-pilotage"') : PAGE.index('id="volet-details"')]
    gauche = principal[principal.index('class="colonne-commandes"') : principal.index('class="colonne-vue"')]
    droite = principal[principal.index('class="colonne-vue"') :]
    ordre_voulu = [
        "ordres-etat", "localisation", "fleches-bascule", "composer", "pave-distance", "pave-angle", "pause-bouton",
        "reprendre-bouton", "base-bouton", "pave-plateau", "pause-raccourci", "pave-pastilles", "emettre", "pave-retours",
    ]
    positions = [gauche.index(f'id="{identifiant}"') for identifiant in ordre_voulu]
    assert positions == sorted(positions)
    for identifiant in ("onglet-camera", "onglet-carte", "video", "vue-camera", "vue-carte", "carte-dessin", "carte-suivre", "carte-precision"):
        assert f'id="{identifiant}"' in droite and f'id="{identifiant}"' not in gauche
    assert droite.count("data-fleche=") == 4 and 'id="vue-carte" class="vue-carte" role="tabpanel" hidden' in droite
    style = (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
    assert "grid-template-columns: minmax(0, 2fr) minmax(0, 3fr)" in style  # ≈ 40 % / 60 %
    assert "@media (max-width: 1099px)" in style and ".colonne-vue { order: -1; }" in style  # étroit : caméra d'abord
    for identifiant in ("pave-miniature", "pave-previsualiser", "pave-annuler", "pave-vider"):
        assert f'id="{identifiant}"' in principal
    assert 'id="previsualisation-detail"' not in principal  # trames et hexadécimal : dans le volet « détails »
    assert len(re.findall(r'id="([^"]+)"', PAGE)) == len(set(re.findall(r'id="([^"]+)"', PAGE)))


def test_une_seule_bascule_pour_les_fleches_et_plus_de_bouton_clavier_separe():
    script = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    assert PAGE.count('id="fleches-bascule"') == 1 and "Flèches : activer" in PAGE and 'aria-pressed="false"' in PAGE
    assert "pave-clavier" not in PAGE + script and "Activer le clavier" not in PAGE
    assert PAGE.count("data-fleche=") == 4 and PAGE.count('data-fleche="haut" aria-label="Avancer" disabled') == 1
    cadre = PAGE[PAGE.index('id="vue-camera"') : PAGE.index('id="vue-carte"')]
    assert cadre.count("data-fleche=") == 4 and 'id="video"' in cadre  # flèches en surimpression de la caméra
    assert "bouton.disabled = !actives" in script and "if (!actives) { return; }" in script and "actives: actives" in script
    # UN geste : la bascule arme aussi le pilotage direct ; plus de second interrupteur.
    assert "direct-interrupteur" not in PAGE + script and 'id="composer" type="checkbox"' in PAGE
    assert "Composer une séquence (ne pas émettre tout de suite)" in PAGE
    activer = script[script.index("async function activer()") : script.index("function afficherPastilles")]
    assert "window.confirm(" in activer and '"/api/direct/armer", { confirmer: true }' in activer and "regler(true)" in activer
    assert "if (!enDirect()) { regler(true);" in activer  # composition : aucune émission, donc aucun armement
    assert "Simulation : les flèches parcourent le même chemin, rien n'est émis" in activer
    style = (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
    assert ".cadre-video.actif { border-color" in style  # liseré visible
    assert ".haut { position: sticky" in style and "bandeau-demo {\n  position: sticky" not in style  # STOP jamais recouvert
    haut = PAGE[PAGE.index('<div class="haut">') : PAGE.index("<main>")]
    assert all(f'id="{i}"' in haut for i in ("bandeau-demo", "bandeau-reel", "bandeau-direct", "stop", "etat-resume"))


def test_memoire_des_sections_seul_usage_du_stockage_du_navigateur():
    for fichier in ui.STATIQUE.iterdir():
        texte = fichier.read_text(encoding="utf-8")
        if fichier.name != "volets.js":
            assert "localStorage" not in texte and "sessionStorage" not in texte, fichier.name
    volets = (ui.STATIQUE / "volets.js").read_text(encoding="utf-8")
    assert volets.count("localStorage") == 2 and 'var CLE = "lymow-volets"' in volets and "sessionStorage" not in volets
    assert "etat[volet.dataset.volet] = volet.open" in volets  # seulement ouvert / fermé, par nom de section
    assert "volets.js" in ui.FICHIERS and PAGE.index('src="/statique/volets.js"') > PAGE.index('src="/statique/carte.js"')
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert "window.LymowVolets.compte(image.compte.connecte)" in app and "!demonstration" in app


# --- logique pure (Node) -------------------------------------------------------------

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")


def js(module: str, expression: str, *arguments: object) -> object:
    script = (
        "const M = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(ui.STATIQUE / module), json.dumps(arguments)],
        capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
    )
    return json.loads(sortie.stdout)


@sans_node
def test_page_bascule_unique_des_fleches():
    assert js("fleches.js", "M.basculer(false, 'bouton')") is True and js("fleches.js", "M.basculer(true, 'bouton')") is False
    assert js("fleches.js", "M.basculer(false, 'clic-video')") is True and js("fleches.js", "M.basculer(true, 'clic-video')") is True
    assert js("fleches.js", "M.basculer(true, 'sortie')") is False and js("fleches.js", "M.basculer(true, 'echap')") is False
    assert js("fleches.js", "M.basculer(false, 'armement')") is True and js("fleches.js", "M.basculer(true, 'autre')") is True
    assert js("fleches.js", "M.libelleBascule(false)") == "Flèches : activer"
    assert js("fleches.js", "M.libelleBascule(true)") == "Flèches : ACTIVES — désactiver"
    assert js("fleches.js", "M.modeDesFleches(false)") == "direct" and js("fleches.js", "M.modeDesFleches(true)") == "file"
    # Désactivées : ni le clavier ni les boutons n'agissent, même avec le focus.
    touche = {"key": "ArrowUp", "repeat": False, "balise": "DIV", "dansLePave": True, "actives": False}
    assert js("fleches.js", "M.actionClavier(a[0])", touche) is None
    assert js("fleches.js", "M.retientLaTouche(a[0])", touche) is False
    assert js("fleches.js", "M.actionClavier(a[0])", touche | {"actives": True}) == {"type": "fleche", "direction": "haut"}


@sans_node
def test_page_echap_reste_stop_des_qu_un_mouvement_est_en_cours():
    cas = lambda **etat: js("fleches.js", "M.effetEchap(a[0])", {"occupe": False, "directArme": False, "flechesActives": False} | etat)  # noqa: E731
    assert cas(occupe=True, flechesActives=True) == "stop"  # STOP prioritaire
    assert cas(flechesActives=True) == "desactiver"  # hors mouvement : Échap désactive seulement les flèches
    assert cas(directArme=True, flechesActives=True) == "desactiver"  # même en pilotage direct, sans mouvement
    assert cas() == "stop"  # comme avant


@sans_node
def test_page_sections_fermees_par_defaut_et_compte_ouverte_si_non_connecte():
    noms = ["carte", "ordres", "calibration", "compte", "journal"]
    fermees = dict.fromkeys(noms, False)
    assert js("ordres.js", "M.voletsOuverts(null, a[0], true)", noms) == fermees
    assert js("ordres.js", "M.voletsOuverts(null, a[0], false)", noms) == fermees | {"compte": True}
    memoire = {"carte": True, "journal": False, "inconnue": True, "ordres": "oui"}
    assert js("ordres.js", "M.voletsOuverts(a[1], a[0], true)", noms, memoire) == fermees | {"carte": True}
    for abimee in ("texte", [True], 3):
        assert js("ordres.js", "M.voletsOuverts(a[1], a[0], true)", noms, abimee) == fermees


@sans_node
def test_page_etat_du_robot_en_une_ligne_et_gestes_proposes():
    etat = {"en_ligne": True, "statut_libelle": "tonte en pause", "tache_libelle": "tonte en cours", "batterie_pct": 80,
            "localisation_texte": "position : ±0,02 m — fiable"}
    assert js("ordres.js", "M.resumeEtat(a[0])", etat) == (
        "en ligne · tonte en pause (tâche : tonte en cours) · batterie 80 % · position : ±0,02 m — fiable"
    )
    pres = {"statut": "tonte en pause", "tache": "tonte en cours", "mouvement_refuse": None}
    assert js("ordres.js", "M.texteEtatDesOrdres(a[0])", pres) == "Robot : tonte en pause (tâche : tonte en cours)"
    assert js("ordres.js", "M.resumeEtat(a[0])", etat | {"en_ligne": False, "perime": True}).startswith("HORS LIGNE")
    assert js("ordres.js", "M.resumeEtat(null)") == "état du robot inconnu"
    tonte = {"action": "pause", "reprise_possible": False, "pause": None}
    gestes = js("ordres.js", "M.gestesProposes(a[0], false)", tonte)
    assert (gestes["pause"], gestes["reprendre"], gestes["texte"], gestes["reprendreQuandMeme"]) == (True, False, "", False)
    assert js("ordres.js", "M.gestesProposes(a[0], true)", tonte)["pause"] is False  # pas pendant un ordre
    attente = {"action": None, "reprise_possible": False, "pause": {"etat": "en attente", "texte": "Pause demandée…"}}
    assert "attente de confirmation" in js("ordres.js", "M.blocageDesFleches(a[0])", attente)
    confirmee = {"action": None, "reprise_possible": True, "pause": {"etat": "confirmée", "texte": "Pause confirmée : flèches disponibles."}}
    assert js("ordres.js", "M.blocageDesFleches(a[0])", confirmee) == ""
    gestes = js("ordres.js", "M.gestesProposes(a[0], false)", confirmee)
    assert (gestes["pause"], gestes["reprendre"], gestes["texte"]) == (False, True, "Pause confirmée : flèches disponibles.")


# --- refus en clair et actionnables (SEC-01 inchangé, seul l'énoncé change) -----------


@pytest.mark.parametrize(
    ("statut", "attendu"),
    [
        (2, "le robot tond : mettez la tâche en pause pour prendre la main — mouvement refusé (SEC-01)"),
        (8, "le robot tond : mettez la tâche en pause pour prendre la main — mouvement refusé (SEC-01)"),
        (9, "le robot tond : mettez la tâche en pause pour prendre la main — mouvement refusé (SEC-01)"),
        (4, "le robot rentre à sa base : mettez le retour en pause pour prendre la main — mouvement refusé (SEC-01)"),
        (14, "le robot se dégage d'un obstacle : mettez la tâche en pause pour prendre la main — mouvement refusé (SEC-01)"),
        (11, "le robot se met à jour : attendez la fin de la mise à jour — mouvement refusé (SEC-01)"),
        (13, "arrêt d'urgence enclenché : levez-le sur le robot — mouvement refusé (SEC-01)"),
    ],
)
def test_refus_sec01_dit_le_statut_en_francais_et_quoi_faire(statut, attendu):
    with pytest.raises(RefusSecurite) as refus:
        securite.verifier_presence_et_statut(statut, True)
    assert str(refus.value) == attendu and not re.search(r"statut de travail \d", str(refus.value))


def test_refus_sec01_les_autres_cas_disent_aussi_quoi_faire_et_la_liste_blanche_ne_bouge_pas():
    assert sorted(securite.STATUTS_LAMES_ARRET) == [0, 1, 3, 5, 6, 7, 12]
    assert sorted(securite.STATUTS_PAUSE_UTILE) == [2, 4, 8, 9, 14]
    for statut in securite.STATUTS_LAMES_ARRET:
        securite.verifier_presence_et_statut(statut, True)  # en pause, en erreur… : rien à faire, pas de refus
    for statut in (2, 4, 8, 9, 10, 11, 13, 14, 15, 16, -1, 99, None):
        with pytest.raises(RefusSecurite, match=r"\(SEC-01\)$"):
            securite.verifier_presence_et_statut(statut, True)
    with pytest.raises(RefusSecurite, match="hors ligne : attendez son retour en ligne — mouvement refusé"):
        securite.verifier_presence_et_statut(1, False)
    with pytest.raises(RefusSecurite, match="état périmé .* : cliquez sur « Actualiser » — mouvement refusé"):
        securite.verifier_lames_arret(1, 45.0, True)
    with pytest.raises(RefusSecurite, match="aucun état reçu .* cliquez sur « Actualiser »"):
        securite.verifier_lames_arret(None, None, True)
    with pytest.raises(RefusSecurite, match="retour à la base est en pause : aucun mouvement n'est permis"):
        securite.verifier_presence_et_statut(10, True)


def test_refus_en_tonte_message_et_action_mettre_en_pause(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(EN_TONTE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.instantane()["ordres"]["action"] == "pause")
            ordres = serveur.instantane()["ordres"]  # dit AVANT, près des flèches
            assert ordres["mouvement_refuse"] == "Le robot tond : mettez la tâche en pause pour prendre la main."
            assert ordres["statut"] == "tonte en cours" and ordres["pause"] is None and ordres["reprise_possible"] is False
            # Et le refus lui-même, flèche en mode direct : même phrase, code SEC à la fin.
            _statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            await poster(http, serveur, "/api/direct/fleche", {"jeton": corps["jeton"], "direction": "droite", "pas": 45})
            await attendre(lambda: not serveur.occupe)
            echec = serveur.instantane()["direct"]["faites"][0]["rapport"]["echec"]
            assert echec == "le robot tond : mettez la tâche en pause pour prendre la main — mouvement refusé (SEC-01)"
            assert lien.ordres == []

    executer(scenario())


def test_pause_emise_fleches_reactivees_seulement_apres_statut_pause_frais_et_aucun_mouvement_automatique(
    tmp_path, monkeypatch, session_reelle, lien
):
    lien.robot = dict(EN_TONTE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            for corps_faux in ({}, {"confirmer": "oui"}, {"confirmer": True, "ordres": [{"nom": "avancer"}]}):
                statut, _corps, _recus = await poster(http, serveur, "/api/pause", corps_faux)
                assert statut == 400 and lien.ordres == []  # une confirmation explicite, et rien d'autre
            statut, _corps, _recus = await poster(http, serveur, "/api/pause", {"confirmer": True})
            assert statut == 202
            ordres = serveur.instantane()["ordres"]
            assert ordres["pause"]["etat"] == "en attente" and ordres["action"] is None
            assert ordres["mouvement_refuse"] == "Pause demandée : attente de la confirmation du robot."
            lien.sur_publication = None
            robot_obeit_a_la_pause(lien)  # le robot n'annonce « pause » qu'à la sollicitation suivante
            lien.robot = dict(EN_PAUSE)
            await attendre(lambda: not serveur.occupe)
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.instantane()["ordres"]["pause"]["etat"] == "confirmée")
            ordres = serveur.instantane()["ordres"]
            assert ordres["pause"]["texte"] == "Pause confirmée : flèches disponibles." and ordres["mouvement_refuse"] is None
            assert ordres["reprise_possible"] is True  # « Reprendre » reste proposé (les lames peuvent redémarrer)
            # Aucun mouvement n'est parti tout seul : la pause, et rien d'autre.
            assert lien.ordres == [PAUSE] and not any(est_consigne(trame) for trame in lien.trames) and ARRET not in lien.trames
            assert serveur.instantane()["direct"]["file"] == [] and serveur.tache.done()

    executer(scenario())
    journal = lignes_journal(tmp_path)
    assert [ligne["ordres"] for ligne in journal if ligne["evenement"] == "session-reelle"] == [["pause"]]


def test_pause_non_confirmee_dite_et_fleches_toujours_refusees(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(EN_TONTE)  # le robot continue de tondre malgré la pause
    monkeypatch.setattr(ui, "DELAI_PAUSE_S", 0.3)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            await poster(http, serveur, "/api/pause", {"confirmer": True})
            await attendre(lambda: not serveur.occupe)
            await attendre(lambda: serveur.instantane()["ordres"]["pause"]["etat"] == "non confirmée", delai=3.0)
            ordres = serveur.instantane()["ordres"]
            assert ordres["pause"]["texte"] == "Pause non confirmée : le robot annonce « tonte en cours »."
            assert ordres["action"] == "pause" and "Le robot tond" in ordres["mouvement_refuse"]
            assert not any(est_consigne(trame) for trame in lien.trames)

    executer(scenario())


def test_pause_par_previsualiser_puis_emettre_et_en_simulation_rien_n_est_emis(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = {"robotState": 4, "workStatus": 4, "battery": 80}  # retour à la base : la pause est le code 21

    async def reel():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "pause"}]})
            assert statut == 200 and corps["etapes"] == ["pause"] and corps["identifiant"]
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            await attendre(lambda: not serveur.occupe)
            assert lien.ordres == [PAUSE_RETOUR] and serveur.resultat["etapes"][0]["codes_emis"] == [21]
            assert serveur.instantane()["ordres"]["pause"] is not None  # suivie comme la pause du raccourci

    executer(reel())
    lien.publies.clear()

    async def simulation():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            statut, corps, _recus = await poster(http, serveur, "/api/pause", {"confirmer": True})
            assert statut == 200 and corps["simulation"] is True and corps["bilan"]["mode"] == "simulation"
            assert lien.ordres == [] and serveur.tache is None

    executer(simulation())


def test_ordre_pause_meme_code_que_la_pause_de_stop_sans_consignes_nulles(session, lien, capsys, tmp_path):
    lien.robot = dict(EN_TONTE)
    rapport = asyncio_run(mouvement.executer(session, Ordre("pause")))
    assert rapport["codes_emis"] == [3] and lien.ordres == [PAUSE] and lien.arrets == []
    lien.publies.clear()
    lien.robot = {"robotState": 6, "workStatus": 2, "battery": 80}  # télécommande : la pause part seule, sans « quitter la télécommande »
    assert asyncio_run(mouvement.executer(session, Ordre("pause")))["codes_emis"] == [3]
    lien.robot = None
    session.etat.recu_statut = None
    with pytest.raises(RefusSecurite, match="aucun état frais"):
        asyncio_run(mouvement.executer(session, Ordre("pause")))
    assert cli.main(["pause", "--json", "--donnees", str(tmp_path)]) == 0  # simulation par défaut
    sortie = json.loads(capsys.readouterr().out)
    assert sortie["mode"] == "simulation" and "userCtrl 21" in sortie["variante"]


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_page_propose_pause_et_reprise_au_bon_endroit():
    principal = PAGE[PAGE.index('id="bloc-pilotage"') : PAGE.index('id="volet-details"')]
    for identifiant in ("pause-bouton", "pause-raccourci", "pause-etat", "reprendre-rappel", "ordres-etat"):
        assert f'id="{identifiant}"' in principal
    assert 'id="pause-bouton" type="button" data-tache="pause"' in principal  # Prévisualiser → Émettre, comme toute tâche
    assert 'id="pause-raccourci" type="button" class="raccourci" hidden>Mettre en pause' in principal
    assert "les lames peuvent redémarrer" in principal and 'id="reprendre-bouton" type="button" data-tache="reprendre"' in principal
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    raccourci = app[app.index('$("pause-raccourci").addEventListener') :][:700]
    assert "window.confirm(" in raccourci and '"/api/pause", { confirmer: true }' in raccourci
    assert "/api/emettre" not in raccourci and "previsualiser(" not in raccourci and "fleche" not in raccourci  # rien n'est enchaîné
    pave = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    assert "blocageDesFleches(ordresDuRobot)" in pave


# --- dix-neuvième avenant : le clavier fait bouger le robot, pas la page --------------------


@sans_node
def test_page_fleche_pressee_ne_fait_jamais_defiler_la_page_quel_que_soit_le_focus():
    """Régression (2026-10-12) : flèches activées, les touches fléchées faisaient défiler la page."""
    for balise in ("BODY", "BUTTON", "DIV", "SECTION", "A", "SUMMARY", "VIDEO", ""):
        for touche, direction in (("ArrowUp", "haut"), ("ArrowDown", "bas"), ("ArrowLeft", "gauche"), ("ArrowRight", "droite")):
            decision = js("fleches.js", "M.interception(a[0])", {"key": touche, "repeat": False, "balise": balise, "actives": True})
            assert decision == {"intercepter": True, "action": {"type": "fleche", "direction": direction}}, (balise, touche)
    # Touche maintenue : toujours retenue (la page ne défile pas), jamais comptée.
    maintenue = js("fleches.js", "M.interception(a[0])", {"key": "ArrowDown", "repeat": True, "balise": "BODY", "actives": True})
    assert maintenue == {"intercepter": True, "action": None}
    for touche, action in (("Enter", {"type": "previsualiser"}), ("Backspace", {"type": "annuler"}), (" ", None)):
        decision = js("fleches.js", "M.interception(a[0])", {"key": touche, "repeat": False, "balise": "BUTTON", "actives": True})
        assert decision == {"intercepter": True, "action": action}, touche


@sans_node
def test_page_clavier_jamais_intercepte_dans_une_saisie_ni_fleches_inactives():
    for balise in ("INPUT", "input", "SELECT", "TEXTAREA"):
        decision = js("fleches.js", "M.interception(a[0])", {"key": "ArrowUp", "repeat": False, "balise": balise, "actives": True})
        assert decision == {"intercepter": False, "action": None}, balise
    editable = {"key": "Backspace", "repeat": False, "balise": "DIV", "editable": True, "actives": True}
    assert js("fleches.js", "M.interception(a[0])", editable) == {"intercepter": False, "action": None}
    inactives = {"key": "ArrowUp", "repeat": False, "balise": "BODY", "actives": False}
    assert js("fleches.js", "M.interception(a[0])", inactives) == {"intercepter": False, "action": None}
    autre = {"key": "a", "repeat": False, "balise": "BODY", "actives": True}
    assert js("fleches.js", "M.interception(a[0])", autre) == {"intercepter": False, "action": None}


@sans_node
def test_page_robot_sur_la_carte_des_qu_une_pose_est_connue_sinon_sur_la_station():
    station = {"est_m": 2.0, "nord_m": 3.0, "cap_deg": 90.0}
    pose = {"position_est_m": 10.0, "position_nord_m": -4.0, "cap_deg": 45.0, "precision_m": 1.32}
    assert js("ordres.js", "M.positionAffichee(a[0], a[1])", pose, station) == {
        "est": 10.0, "nord": -4.0, "cap": 45.0, "rayon": 1.32, "source": "pose", "note": "",
    }
    repos = js("ordres.js", "M.positionAffichee(a[0], a[1])", {"position_est_m": None, "precision_m": 0.03}, station)
    assert (repos["est"], repos["nord"], repos["source"], repos["rayon"]) == (2.0, 3.0, "station", None)
    assert repos["note"] == "position non transmise au repos : robot placé sur sa station"
    assert js("ordres.js", "M.positionAffichee(a[0], a[1])", {"position_est_m": None}, None) is None
    assert js("ordres.js", "M.positionAffichee(null, null)") is None
    carte = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    assert "positionAffichee(etatRobot, carte ? carte.station : null)" in carte and '"carte-incertitude"' in carte
    assert '$("carte-suivre").checked' in carte and "CADRE_ROBOT_M = 40" in carte and "reponse.reference" in carte


# --- trois boutons en tête : « Prendre la main », « Rendre la main », « STOP » ----------------


def test_page_trois_boutons_toujours_visibles_en_tete_dans_l_ordre():
    haut = PAGE[PAGE.index('<div class="haut">') : PAGE.index("<main>")]
    positions = [haut.index(f'id="{identifiant}"') for identifiant in ("prendre-main", "rendre-main", "stop")]
    assert positions == sorted(positions) and PAGE.count('id="rendre-main"') == 1  # « Rendre la main » : seulement en haut
    assert ">Prendre la main<" in haut and ">Rendre la main<" in haut and ">STOP <small>Échap ou Espace</small><" in haut
    for identifiant in ("rendre-choix", "rendre-pause", "rendre-reprendre", "rendre-annuler", "etat-main"):
        assert f'id="{identifiant}"' in haut
    assert ">Laisser le robot en pause<" in haut and ">Reprendre la tâche<" in haut
    pave = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    # « Flèches : activer » est le reflet de « Prendre la main » : le même geste, UNE confirmation.
    assert '$("prendre-main").addEventListener("click", prendreLaMain)' in pave
    assert "if (actives) { desactiver(\"\"); } else { prendreLaMain(); }" in pave and pave.count("window.confirm(") == 2  # prendre la main ; forcer ce pas
    stop = app[app.index("  async function stop() {") :][:700]
    assert "sequence = [];" in stop and "oublierPrevisualisation" in stop and "desactiverFleches(false)" in stop
    assert '"/api/liaison/rendre", { sans_attendre: true, reprendre: reprendre }' in app


@sans_node
def test_page_qui_a_la_main():
    qui = lambda **c: js("ordres.js", "M.quiALaMain(a[0])", {"suspendue": False, "mainPrise": False, "flechesActives": False} | c)  # noqa: E731
    assert qui() == "personne" and qui(mainPrise=True) == "outil" and qui(flechesActives=True) == "outil"
    assert qui(suspendue=True, mainPrise=True, flechesActives=True) == "application"  # liaison suspendue : l'application
    assert js("ordres.js", "M.texteDeLaMain('outil')") == "Vous avez la main"
    assert js("ordres.js", "M.texteDeLaMain('application')") == "L'application Lymow a la main — liaison suspendue"
    assert js("ordres.js", "M.texteDeLaMain('personne')") == ""
    # STOP ne retire pas la main : flèches tombées, la main reste à l'outil.
    assert qui(mainPrise=True, flechesActives=False) == "outil"


@sans_node
def test_page_prendre_la_main_pas_a_pas_sans_rien_enchainer_a_l_aveugle():
    def suite(**c):
        base = {"suspendue": False, "connecte": True, "occupe": False, "ordres": {}, "flechesActives": False, "pauseDemandee": False}
        return js("ordres.js", "M.suitePrendreLaMain(a[0])", base | c)

    tonte = {"action": "pause", "mouvement_refuse": "Le robot tond : mettez la tâche en pause pour prendre la main."}
    assert suite(suspendue=True) == "liaison"  # (a) rétablir la liaison d'abord
    assert suite(connecte=False) == "attendre" and suite(occupe=True) == "attendre"
    assert suite(ordres=tonte) == "pause"  # (b) robot en tonte ou en retour : pause
    assert suite(ordres=tonte, pauseDemandee=True) == "attendre"  # … et on ATTEND le statut vivant « pause »
    assert suite(ordres=tonte | {"pause": {"etat": "en attente"}}, pauseDemandee=True) == "attendre"
    assert suite(ordres=tonte | {"pause": {"etat": "non confirmée"}}, pauseDemandee=True) == "abandon"
    confirmee = {"action": None, "mouvement_refuse": None, "pause": {"etat": "confirmée"}}
    assert suite(ordres=confirmee, pauseDemandee=True) == "fleches"  # (c) flèches seulement une fois la pause confirmée
    assert suite(ordres=confirmee, flechesActives=True) == "fait"
    urgence = {"action": None, "mouvement_refuse": "Arrêt d'urgence enclenché : levez-le sur le robot."}
    assert suite(ordres=urgence) == "bloque"  # non pilotable : rien n'est activé (l'attestation est alors proposée)
    assert suite() == "fleches"

    annonce = js("ordres.js", "M.annoncePrendreLaMain(a[0])", {"suspendue": True, "ordres": tonte, "composer": False, "simulation": False})
    assert annonce.startswith("Prendre la main ?") and "liaison avec le robot sera rétablie" in annonce
    assert "Le robot tond : il sera mis en pause." in annonce and "IMMÉDIATEMENT" in annonce and "STOP : Échap ou Espace." in annonce
    simulation = js("ordres.js", "M.annoncePrendreLaMain(a[0])", {"suspendue": False, "ordres": {}, "composer": False, "simulation": True})
    assert "simulation : rien n'est émis" in simulation and "mis en pause" not in simulation
    composition = js("ordres.js", "M.annoncePrendreLaMain(a[0])", {"suspendue": False, "ordres": {}, "composer": True, "simulation": False})
    assert "composeront une séquence" in composition and "IMMÉDIATEMENT" not in composition


@sans_node
def test_page_rendre_la_main_refuse_pendant_un_mouvement_et_attend_la_telecommande():
    def etape(**c):
        base = {"suspendue": False, "occupe": False, "ordres": {"telecommande": False}, "sansAttendre": False}
        return js("ordres.js", "M.etapeRendreLaMain(a[0])", base | c)

    assert etape(occupe=True) == "refus"  # STOP d'abord
    assert etape(ordres={"telecommande": True}) == "attendre"
    assert etape(ordres={"telecommande": True}, sansAttendre=True) == "choisir"
    assert etape() == "choisir" and etape(suspendue=True) == "deja"


def test_rendre_la_main_avec_reprise_gardee_par_sec12_puis_liaison_suspendue(tmp_path, monkeypatch, session_reelle, lien):
    from conftest import fragment

    lien.robot = fragment(statut=3, tache=2, precision=1.32)  # en pause, position perdue

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            # « Reprendre la tâche » refusé par la garde de localisation : la main n'est PAS rendue.
            statut, corps, _recus = await poster(http, serveur, "/api/liaison/rendre", {"sans_attendre": True, "reprendre": True})
            assert statut == 200 and corps["suspendue"] is False and "Position imprécise (±1,32 m)" in corps["refus"]
            assert serveur.session is not None and not serveur.suspendue and lien.ordres == []
            # Position fiable : la tâche reprend, PUIS la liaison est suspendue.
            lien.robot = fragment(statut=3, tache=2, precision=0.04)
            await poster(http, serveur, "/api/actualiser")
            statut, corps, _recus = await poster(http, serveur, "/api/liaison/rendre", {"sans_attendre": True, "reprendre": True})
            assert corps["suspendue"] is True and serveur.session is None
            assert lien.ordres == [bytes.fromhex("10 31 28 04")]  # reprendre, et rien d'autre
            assert serveur.instantane()["liaison"] == "Liaison suspendue : l'application Lymow a la main"

            # STOP reste disponible main rendue : il rétablit la liaison pour émettre, et ne la suspend pas.
            lien.publies.clear()
            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202 and serveur.suspendue is False and serveur.session is not None
            await attendre(lambda: not serveur.occupe)
            assert lien.arrets == [ARRET] * 3 and bytes.fromhex("10 31 28 03") in lien.ordres  # consignes nulles ×3, puis pause

    executer(scenario())


def test_stop_annule_tout_depuis_chaque_etat_sans_rendre_la_main(tmp_path, monkeypatch, session_reelle, lien):
    from conftest import fragment

    lien.robot = fragment(statut=3, tache=2, precision=0.04)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            # État 1 : rien en cours.
            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202
            await attendre(lambda: not serveur.occupe)
            # État 2 : prévisualisation en attente, direct armé, attestation impossible ici — tout tombe.
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 1}]})
            identifiant = corps["identifiant"]
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            await poster(http, serveur, "/api/stop")
            assert serveur.previsualisation is None and serveur.direct.arme is False
            await attendre(lambda: not serveur.occupe)
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": identifiant})
            assert statut == 409  # la prévisualisation annulée ne vaut plus rien
            # État 3 : mouvement direct en cours, file pleine — interrompu, file vidée, liaison gardée.
            _statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            _statut, corps, _recus = await poster(http, serveur, "/api/direct/fleche", {"jeton": corps["jeton"], "direction": "haut", "pas": 2})
            await poster(http, serveur, "/api/direct/fleche", {"jeton": corps["jeton"], "direction": "gauche", "pas": 45})
            await poster(http, serveur, "/api/stop")
            image = serveur.instantane()
            assert image["direct"]["file"] == [] and image["direct"]["arme"] is False and image["liaison_suspendue"] is False
            await attendre(lambda: not serveur.occupe, delai=6.0)
            assert serveur.session is not None and serveur.etat_liaison == "connecté"  # la main n'est pas rendue

    executer(scenario())
