"""Vingt-troisième avenant : la main n'est pas rendue sans clic explicite, et les constats de la revue du lot.

Bogue constaté dans un navigateur (2026-10-12) : après « Prendre la main », le premier pas s'exécutait puis la
page rendait la main toute seule — une seconde fonction `rendre` (rendre la main) masquait celle qui affiche
un résultat, et chaque résultat affiché appelait la route « rendre la main ».
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess

import pytest
from conftest import ARRET, est_consigne, fragment
from test_carte import contenu
from test_demo import serveur_demo
from test_ui import attendre, executer, lire, poster, serveur_ui

from lymow_pilotage import bon_signal, capture, fleches, garde_carte, protocole, reference, releve, ui
from lymow_pilotage import carte as cartes
from lymow_pilotage.fleches import Armement, RefusDirect

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
CARTE = cartes.decoder(contenu())


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


def journal(tmp_path) -> str:
    return "".join(f.read_text(encoding="utf-8") for f in tmp_path.rglob("*.jsonl"))


# --- bogue 1 : la main n'est pas rendue sans clic explicite ------------------------------------------


def test_page_la_main_n_est_pas_rendue_sans_clic_explicite():
    for fichier in sorted(ui.STATIQUE.glob("*.js")):
        noms = re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", fichier.read_text(encoding="utf-8"), re.M)
        doubles = sorted({nom for nom in noms if noms.count(nom) > 1})
        # Deux fonctions du même nom dans un fichier : la seconde remplace la première en silence (cause du bogue).
        assert doubles == [], f"{fichier.name} : fonctions déclarées deux fois : {doubles}"
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert app.count('"/api/liaison/rendre"') == 1  # un seul appel, dans « rendreLaMain »
    corps = app[app.index("async function rendreLaMain(choix)") :][:400]
    assert '"/api/liaison/rendre", { sans_attendre: true, choix: choix }' in corps
    # « rendreLaMain » n'est appelée que par les deux boutons de la boîte de choix, sur un clic explicite.
    appels = re.findall(r"^.*rendreLaMain\((?!choix\)).*$", app, re.M)
    assert len(appels) == 2 and all("clicExplicite(evenement)" in ligne and 'addEventListener("click"' in ligne for ligne in appels)
    ouverture = app[app.index('$("rendre-main").addEventListener("click"') :][:300]
    assert "if (!clicExplicite(evenement)) { return; }" in ouverture
    for autre in ("pave.js", "carte.js", "ordres.js", "fleches.js", "volets.js"):
        assert "liaison/rendre" not in (ui.STATIQUE / autre).read_text(encoding="utf-8")


@sans_node
def test_page_rendre_la_main_jamais_au_clavier_quand_les_fleches_sont_actives():
    accepte = lambda **c: js("ordres.js", "M.clicRendreAccepte(a[0])", c)  # noqa: E731
    assert accepte(clavier=False, flechesActives=True) is True  # clic de souris : toujours
    assert accepte(clavier=True, flechesActives=True) is False  # Entrée ou Espace sur le bouton : jamais, flèches actives
    assert accepte(clavier=True, flechesActives=False) is True  # accessibilité : au clavier, flèches inactives
    assert js("fleches.js", "M.effetEchap(a[0])", {"occupe": False, "flechesActives": True}) == "stop"  # Échap = STOP, toujours


def test_route_la_main_n_est_pas_rendue_sans_clic_explicite(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=3, tache=2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            # Ce que la page envoyait par erreur (un résultat d'ordre à la place du choix), et toute demande sans choix.
            bilan = {"mode": "réel", "etapes": [{"ordre": "avancer 1"}], "code_sortie": 0}
            for corps in ({}, {"sans_attendre": True}, {"sans_attendre": True, "reprendre": bilan}, {"choix": "autre"},
                          {"choix": True}, {"choix": "pause", "reprendre": True}):
                statut, reponse, _recus = await poster(http, serveur, "/api/liaison/rendre", corps)
                assert statut == 400 and "choix explicite" in reponse["erreur"]
                assert serveur.suspendue is False and serveur.session is not None
            assert "liaison-suspendue" not in journal(tmp_path)
            statut, reponse, _recus = await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            assert statut == 200 and reponse["suspendue"] is True and journal(tmp_path).count('"liaison-suspendue"') == 1

    executer(scenario())


# --- bogues 2 à 4 et bout en bout, robot fictif --------------------------------------------------------


def test_robot_fictif_prendre_la_main_puis_trois_fleches_donnent_trois_pas_dans_l_ordre(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])  # bogue 3 : la carte vient d'elle-même
            _statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            jeton = corps["jeton"]
            for direction, pas in (("haut", 0.25), ("gauche", 15), ("bas", 0.25)):  # à la suite, sans attendre
                statut, corps, _recus = await poster(http, serveur, "/api/direct/fleche", {"jeton": jeton, "direction": direction, "pas": pas})
                assert statut == 202, corps
                jeton = corps["jeton"]
            await attendre(lambda: not serveur.occupe and len(serveur.direct.faites) == 3, delai=60.0)
            image = serveur.instantane()
            faites = image["direct"]["faites"]
            assert [f["rapport"]["ordre"].split(" --")[0] for f in faites] == ["avancer 0.25", "tourner 15", "reculer 0.25"]
            assert not any("echec" in f["rapport"] for f in faites)
            # La main n'est pas rendue : liaison active, flèches toujours armées.
            assert image["liaison_suspendue"] is False and image["direct"]["arme"] is True
            assert "liaison-suspendue" not in journal(tmp_path) and "main rendue" not in journal(tmp_path)

    executer(scenario())


def test_robot_fictif_echap_pendant_un_pas_arret_immediat_et_desarmer_interrompt_aussi(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            robot = serveur.session.lien
            for route in ("/api/stop", "/api/direct/desarmer"):  # Échap = STOP ; le bouton des flèches désarme
                _statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
                statut, corps, _recus = await poster(http, serveur, "/api/direct/fleche", {"jeton": corps["jeton"], "direction": "haut", "pas": 2})
                assert statut == 202
                await attendre(lambda: sum(est_consigne(t) for t in robot.recues) >= 3)
                robot.recues.clear()
                await poster(http, serveur, route)
                await attendre(lambda: ARRET in robot.recues, delai=2.0)  # arrêt immédiat, bien avant les 8 s du pas
                await attendre(lambda: not serveur.occupe, delai=20.0)
                assert sum(est_consigne(t) for t in robot.recues) <= 2 and robot.recues.count(ARRET) >= 3
                assert serveur.direct.arme is False and serveur.direct.file == []
                assert not serveur.fin.is_set()  # le serveur, lui, ne s'arrête pas

    executer(scenario())


def test_robot_fictif_onglet_carte_jamais_vide_quand_une_carte_est_connue(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])  # aucun clic sur « Actualiser la carte »
            _statut, texte, _recus = await lire(http, serveur, "/api/carte")
            assert len(json.loads(texte)["carte"]["zones"]) == 3
            assert serveur.session.lien.recues.count(protocole.trame_demande_carte(serveur.cal).octets) == 1  # une seule demande

    executer(scenario())
    carte_js = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert "connue: function (oui) { if (oui && !carte) { charger(); } }" in carte_js and "montrer: function ()" in carte_js
    assert 'if (nom === "carte" && window.LymowCarte) { window.LymowCarte.montrer(); }' in app  # redessinée à l'affichage
    assert "window.LymowCarte.connue(Boolean(image.carte_connue));" in app


def test_carte_demandee_d_office_seulement_sans_carte_de_reference(tmp_path, monkeypatch, session_reelle, lien):
    lien.carte = contenu()
    demande = protocole.trame_demande_carte(session_reelle.cal).octets

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])
            assert lien.trames.count(demande) == 1 and lien.ordres == []  # requête pure, permise en simulation
        premieres = lien.trames.count(demande)
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            await asyncio.sleep(0.3)
            assert serveur.instantane()["carte_connue"] and lien.trames.count(demande) == premieres  # la référence suffit

    executer(scenario())


@sans_node
def test_liaison_suspendue_dite_sans_alarme(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=3, tache=2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            return serveur.instantane()["ordres"]

    ordres = executer(scenario())
    assert ordres == {"statut": None, "mouvement_refuse": None, "suspendue": True, "texte": "Liaison suspendue : l'application Lymow a la main"}
    assert js("ordres.js", "M.texteEtatDesOrdres(a[0])", ordres) == "Liaison suspendue : l'application Lymow a la main"
    assert js("ordres.js", "M.resumeEtat(null, true)") == "liaison suspendue : l'application Lymow a la main"
    assert js("ordres.js", "M.resumeEtat(null, false)") == "état du robot inconnu"
    assert "!image.ordres.suspendue" in (ui.STATIQUE / "app.js").read_text(encoding="utf-8")  # pas de rouge


# --- revue : main rendue, plus rien ne sort (a, i, j) ---------------------------------------------------


def test_main_rendue_aucune_ouverture_ni_publication_par_aucun_chemin(tmp_path, monkeypatch, session_reelle, lien):
    # Cadences accélérées : 1,5 s d'attente couvrent 75 tours de veille et de sonde (30 s réelles en comptent 6 à 20).
    monkeypatch.setattr(ui, "PERIODE_VEILLE_S", 0.02)
    monkeypatch.setattr(protocole, "PERIODE_DIAGNOSTIC_RTK_S", 0.02)
    lien.robot = fragment(statut=3, tache=2)
    lien.carte = contenu()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/diagnostic-rtk", {"actif": True})
            await attendre(lambda: bytes.fromhex("10 31 28 39") in lien.trames)
            await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            publiees, ouvertures = len(lien.trames), len(serveur.ouvertures)
            # Tous les chemins qui ouvriraient la liaison ou publieraient : refusés en clair, jamais d'erreur 500.
            for route, corps in (
                ("/api/actualiser", {}), ("/api/carte/actualiser", {}), ("/api/video/demarrer", {}),
                ("/api/compte/commencer", {"region": "eu"}), ("/api/compte/valider", {"adresse": "x"}),
                ("/api/compte/choisir", {"appareil": "x"}), ("/api/compte/deconnecter", {}),
                ("/api/diagnostic-rtk", {"actif": True}), ("/api/direct/armer", {"confirmer": True}),
                ("/api/pause", {"confirmer": True}), ("/api/emettre", {"identifiant": "x"}),
            ):
                statut, reponse, _recus = await poster(http, serveur, route, corps)
                assert statut == 409, (route, statut, reponse)
                assert "main" in reponse["erreur"] or "suspendue" in reponse["erreur"].lower(), (route, reponse)
            for route, corps in (("/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 1}]}), ("/api/direct/desarmer", {}),
                                 ("/api/direct/fleche", {"jeton": "x", "direction": "haut", "pas": 1}),
                                 ("/api/carte/previsualiser", {}), ("/api/attestation/poser", {})):
                statut, _reponse, _recus = await poster(http, serveur, route, corps)
                assert statut < 500, (route, statut)
            for route in ("/api/etat", "/api/carte", "/api/signal", "/api/releve"):
                statut, _texte, _recus = await lire(http, serveur, route)
                assert statut < 500, (route, statut)
            # La veille elle-même, et tout appelant, passent par le même point : rien ne s'ouvre.
            await serveur._connecter_sans_verrou()
            await serveur._rouvrir_liaison()
            await asyncio.sleep(1.5)
            assert serveur.session is None and serveur.suspendue is True
            assert len(lien.trames) == publiees and len(serveur.ouvertures) == ouvertures

            # STOP reste disponible : il rétablit la liaison et émet l'arrêt.
            statut, _reponse, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202
            await attendre(lambda: not serveur.occupe)
            assert lien.arrets[-3:] == [ARRET] * 3 and serveur.suspendue is False

    executer(scenario())


def test_rendre_la_main_reverifie_sous_le_verrou_qu_aucun_mouvement_n_a_commence(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=3, tache=2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await serveur._verrou_acces.acquire()  # une opération d'accès est en cours
            demande = asyncio.ensure_future(poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"}))
            await asyncio.sleep(0.2)
            serveur.tache = asyncio.ensure_future(asyncio.sleep(30))  # un mouvement a commencé pendant l'attente
            serveur._verrou_acces.release()
            statut, reponse, _recus = await demande
            assert statut == 409 and "mouvement est en cours" in reponse["erreur"]
            assert serveur.suspendue is False and serveur.session is not None  # la liaison n'est pas fermée sous lui
            serveur.tache.cancel()
            await asyncio.gather(serveur.tache, return_exceptions=True)

    executer(scenario())


def test_stop_ne_depend_ni_du_disque_ni_de_la_carte_ni_du_verrou_d_acces(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=3, tache=2)

    def en_panne(*_args, **_kwargs):
        raise OSError("disque indisponible")

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            monkeypatch.setattr(reference, "charger", en_panne)
            monkeypatch.setattr(capture, "pour_un_ordre", en_panne)
            monkeypatch.setattr(serveur, "_poser_origine", en_panne)  # STOP ne doit même pas l'appeler
            await serveur._verrou_acces.acquire()  # vidéo ou compte en cours : STOP n'attend pas derrière
            try:
                statut, _reponse, _recus = await asyncio.wait_for(poster(http, serveur, "/api/stop"), 2.0)
                assert statut == 202
                await attendre(lambda: not serveur.occupe)
                assert lien.arrets == [ARRET] * 3 and bytes.fromhex("10 31 28 03") in lien.ordres  # arrêt ×3, puis pause
            finally:
                serveur._verrou_acces.release()
            # Une avance, elle, se passe de garde-fou si la carte est illisible : dit au bilan, sans retenir l'ordre.
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 0.25}]})
            assert corps["identifiant"], corps
            statut, _reponse, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=20.0)
            assert serveur.resultat["etapes"][0]["garde_carte"] == "distance au bord inconnue : aucune carte de référence"

    executer(scenario())


# --- revue : allure lente, jeton, validation (e, P3) ----------------------------------------------------


def test_allure_lente_choisie_jamais_refusee_pour_sa_duree_vitesse_retenue_annoncee(tmp_path, monkeypatch, session_reelle, lien):
    assert fleches.ordre("haut", 1.0, "lente").vitesse == 0.12 and fleches.ordre("haut", 2.0, "lente").vitesse == 0.23
    assert fleches.ordre("haut", 0.5, "lente").vitesse == 0.10 and fleches.ordre("bas", 2.0, "lente", False, 0.8).vitesse == 0.25
    assert (garde_carte.vitesse_lente(0.9), garde_carte.vitesse_lente(1.0), garde_carte.annonce_vitesse(0.10, 0.5)) == (0.10, 0.12, "")
    lien.robot = {"robotState": 3, "workStatus": 2, "rtkEastM": 0.03}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            _statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            jeton = corps["jeton"]
            # Un refus de validation ne désarme pas : la flèche suivante part, avec le même jeton.
            statut, _corps, _recus = await poster(http, serveur, "/api/direct/fleche", {"jeton": jeton, "direction": "haut", "pas": 7})
            assert statut == 400 and serveur.direct.arme is True and lien.ordres == []
            statut, corps, _recus = await poster(
                http, serveur, "/api/direct/fleche", {"jeton": jeton, "direction": "haut", "pas": 1, "allure": "lente"}
            )
            assert statut == 202 and corps["allure"] == "allure lente : 0.12 m/s pour ce pas de 1 m (9 s au plus par ordre)"
            await attendre(lambda: not serveur.occupe, delai=20.0)
            rapport = serveur.instantane()["direct"]["faites"][-1]["rapport"]
            assert rapport["ordre"] == "avancer 1 --vitesse 0.12" and "echec" not in rapport

    executer(scenario())


@sans_node
def test_page_allure_lente_en_composition_meme_plancher_que_le_serveur():
    for pas in (0.25, 0.5, 1.0, 2.0):
        assert js("fleches.js", "M.vitesseLente(a[0], 0.10)", pas) == fleches.ordre("haut", pas, "lente").vitesse


def test_deux_fleches_plus_rapides_que_l_aller_retour_ne_desarment_pas():
    instant = [100.0]
    armement = Armement(lambda: instant[0])
    premier = armement.armer()
    second = armement.renouveler()  # la réponse à la première flèche n'est pas encore arrivée à la page
    armement.verifier(premier)  # la deuxième flèche porte encore le jeton précédent : acceptée
    armement.verifier(second)
    instant[0] += fleches.JETON_PRECEDENT_S + 0.1
    with pytest.raises(RefusDirect):
        armement.verifier(premier)  # deux secondes plus tard, il ne vaut plus rien
    armement.verifier(second)
    troisieme = armement.renouveler()
    armement.desarmer()
    for jeton in (premier, second, troisieme, "", None):
        with pytest.raises(RefusDirect):
            armement.verifier(jeton)
    rearme = armement.armer()  # un nouvel armement n'accepte aucun jeton d'avant
    with pytest.raises(RefusDirect):
        armement.verifier(troisieme)
    armement.verifier(rearme)


# --- revue : SEC-13, âge de la pose et annonce dès la prévisualisation (f) --------------------------------


def test_sec13_pose_plus_vieille_que_cinq_secondes_distance_au_bord_inconnue():
    assert garde_carte.AGE_POSE_MAX_S == 5.0
    assert garde_carte.juger_pas(CARTE, (9.12, 5.0), 0.0, 1.0, 0.0, 4.9).decision == garde_carte.RACCOURCI
    for age in (5.1, 60.0, None, float("nan")):
        vieux = garde_carte.juger_pas(CARTE, (9.12, 5.0), 0.0, 1.0, 0.0, age)
        assert (vieux.decision, vieux.bloque, vieux.distance_m) == (garde_carte.INCONNU, False, 1.0)
        assert vieux.motif == "distance au bord inconnue : position du robot trop ancienne"


def test_sec13_pose_ancienne_le_pas_part_entier_et_le_bilan_le_dit(session, lien, horloge):
    import asyncio as aio

    from lymow_pilotage import mouvement
    from lymow_pilotage.mouvement import Ordre

    session.carte_reference = CARTE
    session.recevoir_etat(fragment(statut=3, tache=2, est=9.82, nord=5.0, precision=0.01))  # pose reçue…
    horloge.t += 30.0  # … il y a 30 s
    lien.robot = {"robotState": 3, "workStatus": 2, "rtkEastM": 0.01}  # l'état frais ne porte pas de pose
    etape = aio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5)]))["etapes"][0]
    assert etape["garde_carte"] == "distance au bord inconnue : position du robot trop ancienne" and etape["emissions"] == 20


def test_sec13_raccourcissement_annonce_des_la_previsualisation(tmp_path, monkeypatch, session_reelle, lien):
    etapes = [("avancer", 1.0), ("tourner", 180.0), ("avancer", 1.0), ("plateau", 60.0), ("reculer", 2.0)]
    assert garde_carte.annoncer(CARTE, (9.12, 5.0), 0.0, 0.0, 1.0, etapes) == [
        "étape 1 : pas réduit à 0,55 m : bord de zone devant à 0,85 m (marge de sécurité 0,3 m)",
        "étape 5 : pas réduit à 1 m : bord de zone derrière à 1,3 m (marge de sécurité 0,3 m)",
    ]
    assert garde_carte.annoncer(CARTE, (7.02, 5.0), 0.0, 0.02, 1.0, [("avancer", 1.0), ("tourner", 15.0)]) == []
    assert garde_carte.annoncer(None, (7.02, 5.0), 0.0, 0.02, 1.0, [("avancer", 1.0)]) == ["distance au bord inconnue : aucune carte de référence"]
    assert garde_carte.annoncer(CARTE, (7.02, 5.0), 0.0, 0.02, 9.0, [("avancer", 1.0)]) == [
        "distance au bord inconnue : position du robot trop ancienne"
    ]
    lien.robot = fragment(statut=3, tache=2, est=9.12, nord=5.0, precision=0.0)
    lien.carte = contenu()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])
            await poster(http, serveur, "/api/actualiser")
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 1}]})
            assert corps["garde_carte"] == ["étape 1 : pas réduit à 0,55 m : bord de zone devant à 0,85 m (marge de sécurité 0,3 m)"]
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 1, "force": True}]})
            assert corps["garde_carte"] == []  # pas forcé : rien ne sera raccourci
            assert lien.ordres == []

    executer(scenario())
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert "reponse.garde_carte" in app and '(gardeCarte || []).join(" ; ")' in app  # affiché avant « Émettre »


# --- revue : bon signal, relevé, référence (h, P3) ------------------------------------------------------


def test_trajet_vers_le_bon_signal_refuse_pendant_un_mouvement_et_calcule_hors_de_la_boucle(tmp_path, monkeypatch, session_reelle, lien):
    import inspect

    lien.carte = contenu()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])
            statut, _texte, _recus = await lire(http, serveur, "/api/signal")
            assert statut == 200
            serveur.tache = asyncio.ensure_future(asyncio.sleep(30))
            statut, _texte, _recus = await lire(http, serveur, "/api/signal")
            assert statut == 409
            serveur.tache.cancel()
            await asyncio.gather(serveur.tache, return_exceptions=True)

    executer(scenario())
    assert "await asyncio.to_thread(\n            bon_signal.proposer," in inspect.getsource(ui.ServeurUI._signal_proposer)


def test_releve_de_precision_borne_et_lu_de_facon_incrementale(tmp_path, monkeypatch):
    journal_releve = releve.Releve(tmp_path)
    lecteur = releve.Lecteur(tmp_path)
    assert lecteur.points() == []
    for i in range(3):
        assert journal_releve.noter(10.0 * i, 0.0, 0.05, 20, 100.0 * i)
    assert [p["est_m"] for p in lecteur.points()] == [0.0, 10.0, 20.0]
    position = lecteur._position
    lus = []
    monkeypatch.setattr(releve, "_point", lambda ligne, _vrai=releve._point: lus.append(ligne) or _vrai(ligne))
    assert len(lecteur.points()) == 3 and lus == []  # rien de neuf : rien n'est relu
    journal_releve.noter(30.0, 0.0, 0.05, 20, 300.0)
    with (tmp_path / releve.FICHIER).open("a", encoding="utf-8") as fichier:
        fichier.write('{"est_m": 40.0, "nord_m"')  # ligne en cours d'écriture : attend d'être entière
    assert [p["est_m"] for p in lecteur.points()] == [0.0, 10.0, 20.0, 30.0] and len(lus) == 1 and lecteur._position > position
    assert [p["est_m"] for p in releve.lire(tmp_path, maximum=2)] == [20.0, 30.0]
    # Fichier borné : au-delà de la taille maximale, il tourne (un seul ancien gardé) et le lecteur repart.
    monkeypatch.setattr(releve, "TAILLE_MAX_OCTETS", 200)
    (tmp_path / releve.FICHIER).write_text("\n".join(['{"est_m": 1, "nord_m": 1, "precision_m": 1}'] * 10) + "\n", encoding="utf-8")
    assert journal_releve.noter(500.0, 0.0, 0.05, 20, 5000.0)
    assert (tmp_path / (releve.FICHIER + ".1")).exists() and (tmp_path / releve.FICHIER).stat().st_size < 200
    assert [p["est_m"] for p in lecteur.points()] == [500.0]


def test_carte_de_reference_ecriture_atomique_et_refusee_pour_un_autre_appareil(tmp_path, monkeypatch):
    meta = reference.enregistrer(tmp_path, "robot-A", CARTE)
    assert meta["appareil"] == "robot-A" and reference.charger(tmp_path, "robot-A")[0].octets == CARTE.octets
    dossier = tmp_path / reference.DOSSIER
    assert sorted(f.suffix for f in dossier.iterdir()) == [".json", ".pb"]  # aucun fichier provisoire ne reste
    # La carte d'un appareil, recopiée sous le nom d'un autre : refusée.
    for suffixe in (".pb", ".json"):
        shutil.copy(dossier / f"robot-A{suffixe}", dossier / f"robot-B{suffixe}")
    assert reference.charger(tmp_path, "robot-B") is None and reference.charger(tmp_path, "robot-A") is not None
    # Coupure pendant l'écriture : l'ancienne référence reste entière et lisible.
    autre = cartes.decoder(contenu(nom="Autre"))
    vrai_remplacer = type(dossier).replace

    def coupure(self, cible):
        if str(cible).endswith(".pb"):
            raise OSError("coupure")
        return vrai_remplacer(self, cible)

    monkeypatch.setattr(type(dossier), "replace", coupure)
    assert reference.enregistrer(tmp_path, "robot-A", autre) is None
    monkeypatch.undo()
    assert reference.charger(tmp_path, "robot-A")[0].octets == CARTE.octets


def test_decouper_tient_compte_du_facteur_de_distance():
    from lymow_pilotage import mouvement
    from lymow_pilotage.config import Calibration
    from lymow_pilotage.mouvement import Ordre

    trajet = [(0.0, 0.0), (6.0, 0.0)]
    assert [o["valeur"] for o in bon_signal.decouper(trajet, 0.0)] == [2.0, 2.0, 2.0]
    courts = bon_signal.decouper(trajet, 0.0, 1.0, 0.8)  # facteur 0,8 : 2 m dureraient 10 s, plus que les 9 s permises
    assert [o["valeur"] for o in courts] == [1.5, 1.5, 1.5, 1.5]
    for cal, ordres in ((Calibration(), bon_signal.decouper(trajet, 0.0)), (Calibration(facteur_distance=0.8), courts)):
        for o in ordres:
            mouvement.valider(Ordre(o["nom"], o["valeur"]), cal)  # SEC-02 : chaque pas proposé est émissible
