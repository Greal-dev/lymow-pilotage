"""Pilotage par flèches : route directe du serveur, et logique pure de la page (fleches.js)."""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess

import pytest
from conftest import ARRET, est_consigne, lignes_journal
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import fleches, mouvement, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.mouvement import ErreurSaisie
from lymow_pilotage.protocole import est_requete_pure

SANS_POSE = {"robotState": 1, "workStatus": 1, "battery": 80}


async def armer(http, serveur) -> str:
    statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
    assert statut == 200 and corps["arme"] is True and serveur.instantane()["direct"]["arme"] is True
    return corps["jeton"]


async def fleche(http, serveur, jeton, direction="haut", pas=0.25, **en_plus):
    return await poster(http, serveur, "/api/direct/fleche", {"jeton": jeton, "direction": direction, "pas": pas} | en_plus)


# --- les quatre ordres, et rien d'autre ------------------------------------------


def test_les_quatre_fleches_ne_produisent_que_des_ordres_bornes():
    ordres = [fleches.ordre(direction, pas) for direction in ("haut", "bas") for pas in fleches.DISTANCES_M]
    ordres += [fleches.ordre(direction, pas) for direction in ("gauche", "droite") for pas in fleches.ANGLES_DEG]
    assert {o.nom for o in ordres} == {"avancer", "reculer", "tourner"} and len(ordres) == 14
    assert all(o.vitesse is None and o.duree is None for o in ordres)  # vitesses par défaut de l'outil
    assert sorted({o.valeur for o in ordres if o.nom == "tourner"}) == [-90.0, -45.0, -15.0, 15.0, 45.0, 90.0]
    assert sorted({o.valeur for o in ordres if o.nom != "tourner"}) == [0.25, 0.5, 1.0, 2.0]
    for ordre in ordres:
        mouvement.valider(ordre, Calibration())  # SEC-02 : tous sous les plafonds
    assert fleches.ordre("droite", 45).valeur == -45.0 and fleches.ordre("gauche", 45).valeur == 45.0


@pytest.mark.parametrize(
    ("direction", "pas"),
    [
        ("plateau-haut", 1), ("stop", 1), ("reprendre", 1), ("avancer", 1), ("", 1), (None, 1), (["haut"], 1),
        ("haut", 3), ("haut", 0.3), ("haut", -1), ("haut", 45), ("gauche", 1), ("gauche", 180), ("droite", -45),
        ("haut", "1"), ("haut", True), ("haut", None), ("haut", float("nan")), ("haut", [1]),
    ],
)
def test_fleche_ou_pas_hors_liste_refuse(direction, pas):
    with pytest.raises(ErreurSaisie):
        fleches.ordre(direction, pas)


def test_la_route_directe_ne_fabrique_ses_ordres_que_par_les_fleches():
    source = inspect.getsource(ui.ServeurUI._direct_fleche) + inspect.getsource(ui.ServeurUI._derouler_file)
    assert "fleches.ordre(" in source and "Ordre(" not in source.replace("fleches.ordre(", "")
    assert "ordres_depuis" not in source and "conduite.emettre_ordres(self.session, [ordre], self.journal)" in source
    assert "self.direct.verifier(" in source  # jamais sans armement vérifié


# --- route directe ---------------------------------------------------------------


def test_direct_refuse_si_non_arme_et_armement_sur_confirmation_explicite(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            statut, corps, _recus = await fleche(http, serveur, "invente")
            assert statut == 409 and "non armé" in corps["erreur"]
            for corps_faux in ({}, {"confirmer": "oui"}, {"confirmer": 1}, {"confirmer": False}):
                statut, _corps, _recus = await poster(http, serveur, "/api/direct/armer", corps_faux)
                assert statut == 400 and serveur.instantane()["direct"]["arme"] is False
            jeton = await armer(http, serveur)
            statut, corps, _recus = await fleche(http, serveur, "un-autre-jeton")
            assert statut == 409 and serveur.instantane()["direct"]["arme"] is False  # mauvais jeton : désarmé
            statut, _corps, _recus = await fleche(http, serveur, jeton)
            assert statut == 409  # l'ancien jeton ne vaut plus rien
            assert lien.ordres == []
            nouveau = await armer(http, serveur)
            assert nouveau not in json.dumps(serveur.instantane(complet=True))  # le jeton ne passe jamais par le flux d'état

    executer(scenario())
    evenements = [ligne["evenement"] for ligne in lignes_journal(tmp_path)]
    assert "direct-arme" in evenements and "direct-desarme" in evenements


def test_direct_armement_expire_apres_cinq_minutes_sans_action_et_toute_action_le_renouvelle(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            jeton = await armer(http, serveur)
            assert fleches.DUREE_ARMEMENT_S == 300.0 and serveur.instantane()["direct"]["reste_s"] == 300
            depart = serveur.horloge()
            instant = [depart + 299.0]
            serveur.direct.horloge = serveur.horloge = lambda: instant[0]
            assert serveur.instantane()["direct"]["arme"] is True
            # Toute action sur la page renouvelle le délai, sans changer de jeton.
            statut, corps, _recus = await poster(http, serveur, "/api/direct/activite")
            assert statut == 200 and corps == {"arme": True, "reste_s": 300}
            instant[0] = depart + 299.0 + 299.0
            assert serveur.instantane()["direct"]["arme"] is True
            instant[0] = depart + 299.0 + 301.0
            assert serveur.instantane()["direct"]["arme"] is False
            statut, corps, _recus = await poster(http, serveur, "/api/direct/activite")
            assert corps == {"arme": False, "reste_s": 0}  # l'activité ne réarme pas
            statut, corps, _recus = await fleche(http, serveur, jeton)
            assert statut == 409 and "5 min sans action" in corps["erreur"] and lien.ordres == []

    executer(scenario())


@pytest.mark.parametrize(
    "demande",
    [
        {"direction": "plateau-haut", "pas": 1}, {"direction": "stop", "pas": 1}, {"direction": "haut", "pas": 3},
        {"direction": "gauche", "pas": 180}, {"direction": "haut", "pas": "1"}, {"direction": "haut", "pas": 1, "vitesse": 0.25},
        {"direction": "haut", "pas": 1, "ordres": [{"nom": "reprendre"}]}, {"direction": "haut", "pas": 1, "duree": 9},
    ],
)
def test_direct_ordre_ou_pas_hors_liste_refuse_sans_desarmer(tmp_path, monkeypatch, session_reelle, lien, demande):
    # Revue du 2026-10-12 : un refus de validation ne désarme pas (seuls un refus de sécurité ou un échec d'émission).
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            jeton = await armer(http, serveur)
            statut, _corps, _recus = await poster(http, serveur, "/api/direct/fleche", {"jeton": jeton} | demande)
            assert statut == 400 and serveur.instantane()["direct"]["arme"] is True
            assert lien.ordres == [] and serveur.tache is None

    executer(scenario())


def test_direct_en_simulation_meme_parcours_et_zero_publication(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            jeton = await armer(http, serveur)
            for direction, pas in (("haut", 1), ("gauche", 45), ("bas", 2), ("droite", 90)):
                statut, corps, _recus = await fleche(http, serveur, jeton, direction, pas)
                assert statut == 200 and corps["simulation"] is True and corps["bilan"]["mode"] == "simulation"
                jeton = corps["jeton"]
            assert serveur.tache is None and len(serveur.instantane()["direct"]["faites"]) == 4
            assert lien.ordres == [] and all(est_requete_pure(trame) for trame in lien.trames)

    executer(scenario())
    simules = [ligne["ordre"] for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "ordre-simule"]
    assert simules == ["avancer 1", "tourner 45", "reculer 2", "tourner -90"]


def test_direct_emet_chaque_fleche_par_le_chemin_des_ordres_et_enchaine_la_file(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(SANS_POSE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            jeton = await armer(http, serveur)
            statut, corps, _recus = await fleche(http, serveur, jeton, "haut", 0.25)
            assert statut == 202 and corps["jeton"] != jeton
            statut, corps, _recus = await fleche(http, serveur, corps["jeton"], "gauche", 15)  # pendant le mouvement
            assert statut == 202 and corps["file"] == ["tourner 15"]
            assert serveur.instantane()["direct"]["file"] == [{"nom": "tourner", "valeur": 15.0, "vitesse": None, "duree": None, "force": False, "angle": None}]
            await attendre(lambda: not serveur.occupe, delai=8.0)
            direct = serveur.instantane()["direct"]
            assert direct["arme"] is True and direct["file"] == []  # toujours armé après deux pas réussis
            assert [f["ordre"]["nom"] for f in direct["faites"]] == ["avancer", "tourner"]
            assert all(f["rapport"]["issue"] == "émise, non vérifiable" for f in direct["faites"])
            assert serveur.resultat["code_sortie"] == 0 and len(serveur.resultat["etapes"]) == 2

    executer(scenario())
    journal = lignes_journal(tmp_path)
    assert [ligne["ordres"] for ligne in journal if ligne["evenement"] == "session-reelle"] == [["avancer 0.25"], ["tourner 15"]]
    assert [ligne["evenement"] for ligne in journal].count("arret-emis") == 2  # SEC-03 : un arrêt garanti par pas
    assert sum(est_consigne(trame) for trame in lien.trames) > 10 and lien.arrets == [ARRET] * 6


def test_direct_file_limitee_a_huit_et_stop_vide_la_file_et_desarme(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(SANS_POSE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            jeton = await armer(http, serveur)
            statut, corps, _recus = await fleche(http, serveur, jeton, "haut", 2)  # 8 s de mouvement
            assert statut == 202
            for _rang in range(8):
                statut, corps, _recus = await fleche(http, serveur, corps["jeton"], "gauche", 45)
                assert statut == 202
            jeton = corps["jeton"]
            statut, refus, _recus = await fleche(http, serveur, jeton, "gauche", 45)
            assert statut == 429 and "file d'attente pleine (8)" in refus["erreur"]
            direct = serveur.instantane()["direct"]
            assert len(direct["file"]) == 8 and direct["arme"] is True  # appui ignoré, toujours armé
            statut, _corps, _recus = await fleche(http, serveur, jeton, "gauche", 45)
            assert statut == 429  # le jeton n'a pas changé : la page reste armée

            statut, _corps, _recus = await poster(http, serveur, "/api/stop")
            assert statut == 202
            direct = serveur.instantane()["direct"]
            assert direct["file"] == [] and direct["arme"] is False
            await attendre(lambda: not serveur.occupe, delai=6.0)
            statut, _corps, _recus = await fleche(http, serveur, jeton, "gauche", 45)
            assert statut == 409  # désarmé : plus rien ne part sans réarmer

    executer(scenario())
    journal = lignes_journal(tmp_path)
    assert [ligne["ordres"] for ligne in journal if ligne["evenement"] == "session-reelle"][0] == ["avancer 2"]
    assert not any(ligne.get("ordres") == ["tourner 45"] for ligne in journal)  # aucune étape de la file n'est partie
    assert {"motif": "STOP"}.items() <= next(l for l in journal if l["evenement"] == "direct-desarme").items()


def test_direct_refus_sec01_desarme_et_vide_la_file(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = {"robotState": 4, "workStatus": 4, "battery": 80}  # retour à la base : hors liste blanche

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            jeton = await armer(http, serveur)
            statut, corps, _recus = await fleche(http, serveur, jeton, "haut", 1)
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=4.0)
            direct = serveur.instantane()["direct"]
            assert direct["arme"] is False and direct["file"] == []
            assert "SEC-01" in direct["faites"][0]["rapport"]["echec"] and "SEC-01" in serveur.resultat["motif"]
            assert serveur.resultat["code_sortie"] == 1
            statut, _corps, _recus = await fleche(http, serveur, corps["jeton"], "haut", 1)
            assert statut == 409
            assert lien.ordres == []  # rien n'a bougé

    executer(scenario())


def test_direct_refuse_pendant_un_autre_ordre_et_arme_refuse_si_occupe(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(SANS_POSE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            jeton = await armer(http, serveur)
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 0.5}]})
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            statut, _corps, _recus = await fleche(http, serveur, jeton, "haut", 1)
            # Un seul mouvement à la fois ; refus avant émission : dit, sans désarmer (décision du 2026-10-12).
            assert statut == 409 and serveur.instantane()["direct"]["arme"] is True
            await poster(http, serveur, "/api/direct/desarmer")
            await attendre(lambda: serveur.session is not None)
            statut, _corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            assert statut == 409
            await attendre(lambda: not serveur.occupe, delai=6.0)
            assert [ligne["ordres"] for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "session-reelle"] == [["avancer 0.5"]]

    executer(scenario())


def test_direct_en_demonstration_robot_fictif(tmp_path, monkeypatch):
    from test_demo import serveur_demo

    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            jeton = await armer(http, serveur)
            statut, _corps, _recus = await fleche(http, serveur, jeton, "haut", 0.25)
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=6.0)
            assert serveur.resultat["mode"] == "démonstration" and serveur.instantane()["direct"]["arme"] is True

    executer(scenario())


def test_page_du_pave_etat_complet_et_garde_fous_visibles(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, _http):
            return serveur.instantane(complet=True)["fleches"]

    assert executer(scenario()) == {
        "distances_m": [0.25, 0.5, 1.0, 2.0], "angles_deg": [15.0, 45.0, 90.0],
        "distance_defaut_m": 1.0, "angle_defaut_deg": 45.0, "file_max": 8, "armement_s": 300.0,
        "allures": {"auto": None, "lente": 0.10, "normale": 0.25}, "voie_m": 0.45,
    }
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    script = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    assert "PILOTAGE DIRECT ARMÉ" in page and 'id="bandeau-direct"' in page and "Échap ou Espace" in page
    # Commandes à gauche, puis la caméra à droite, flèches en surimpression de la caméra.
    assert page.index('id="bloc-pilotage"') < page.index('id="bloc-fleches"') < page.index('id="video"') < page.rindex('data-fleche=')
    for identifiant in ("pave-miniature", "pave-total", "pave-retours", "pave-file", "composer", "fleches-bascule"):
        assert f'id="{identifiant}"' in page and f'"{identifiant}"' in script
    assert page.count("data-fleche=") == 16  # deux pavés 3×3 : à gauche ET en surimpression de la caméra
    # Mode file : la flèche écrit dans LA séquence de la page ; aucune émission sans les deux temps.
    assert "page.ajouterEtape(etape)" in script and "/api/emettre" not in script and "/api/previsualiser" not in script
    assert "window.confirm(" in script and '"/api/direct/armer", { confirmer: true }' in script
    # Clavier : capté au niveau du document, en phase de capture ; la décision est dans fleches.js.
    assert "Fleches.interception({" in script and "repeat: evenement.repeat" in script
    assert 'document.addEventListener("keydown", function (evenement) {' in script and "}, true);" in script
    assert 'panneau.addEventListener("keydown"' not in script and "focusout" not in script and ".focus()" not in script
    assert 'navigator.sendBeacon("/api/direct/desarmer")' in script


# --- logique pure de la page (fleches.js), exécutée hors navigateur -----------------

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique du pavé non testée")


def js(expression: str, *arguments: object) -> object:
    script = (
        "const F = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(ui.STATIQUE / "fleches.js"), json.dumps(arguments)],
        capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
    )
    return json.loads(sortie.stdout)


@sans_node
def test_page_fleche_vers_etape_et_symbole():
    etapes = [js("F.ordreDe(a[0], 1, 45)", d) for d in ("haut", "bas", "gauche", "droite")]
    assert [(e["nom"], e["valeur"]) for e in etapes] == [("avancer", 1), ("reculer", 1), ("tourner", 45), ("tourner", -45)]
    assert all(e["vitesse"] is None and e["duree"] is None for e in etapes)
    assert js("F.ordreDe(a[0], 1, 45)", "plateau") is None
    assert [js("F.symbole(a[0])", e) for e in etapes] == ["↑ 1 m", "↓ 1 m", "← 45°", "→ 45°"]
    assert js("F.symbole(a[0])", {"nom": "plateau-haut"}) == "plateau-haut"


@sans_node
def test_page_trace_cumule_et_totaux():
    etapes = [
        {"nom": "avancer", "valeur": 1}, {"nom": "tourner", "valeur": 90}, {"nom": "avancer", "valeur": 2},
        {"nom": "tourner", "valeur": -45}, {"nom": "reculer", "valeur": 1}, {"nom": "plateau-haut"},
    ]
    trace = js("F.trace(a[0], null, a[1])", etapes, {"lineaire": 0.25, "angulaire": 0.35})
    points = [(round(p["x"], 3), round(p["y"], 3), p["cap"]) for p in trace["points"]]
    # Sans position connue : départ en (0, 0), cap vers le haut ; gauche = sens trigonométrique.
    assert points[:4] == [(0, 0, 90), (0, 1, 90), (0, 1, 180), (-2, 1, 180)]
    assert points[4] == (-2, 1, 135) and points[5] == (-1.293, 0.293, 135) and points[6] == points[5]
    assert trace["distance_m"] == 4 and trace["rotation_deg"] == 135
    assert trace["duree_s"] == pytest.approx(4 / 0.25 + 2.356 / 0.35, abs=0.1)
    depart = js("F.trace(a[0], a[1], null)", etapes[:1], {"x": 5, "y": 5, "cap": 0})
    assert depart["points"][1] == {"x": 6, "y": 5, "cap": 0} and depart["duree_s"] == 0
    assert js("F.trace([], null, null).points.length") == 1
    assert js("F.texteTotal(a[0], 5)", trace) == "5 étapes — 4 m parcourus, 135° de rotation, environ 23 s de mouvement"


@sans_node
def test_page_file_d_attente_limitee_et_annulation():
    file = []
    for rang in range(7):
        resultat = js("F.empiler(a[0], a[1], 5)", file, {"nom": "avancer", "valeur": rang})
        file = resultat["file"]
        assert resultat["accepte"] is (rang < 5)
    assert len(file) == 5 and [e["valeur"] for e in file] == [0, 1, 2, 3, 4]
    assert js("F.annulerDerniere(a[0])", file) == file[:4] and js("F.annulerDerniere(a[0])", []) == []


@sans_node
def test_page_clavier_une_touche_maintenue_vaut_un_seul_appui_et_seulement_dans_le_pave():
    base = {"key": "ArrowUp", "repeat": False, "balise": "SECTION", "dansLePave": True}
    assert js("F.actionClavier(a[0])", base) == {"type": "fleche", "direction": "haut"}
    assert js("F.actionClavier(a[0])", base | {"repeat": True}) is None  # event.repeat ignoré
    assert js("F.actionClavier(a[0])", base | {"dansLePave": False}) is None  # jamais hors du pavé
    for balise in ("INPUT", "select", "TEXTAREA"):
        assert js("F.actionClavier(a[0])", base | {"balise": balise}) is None  # jamais pendant une saisie
    directions = [js("F.actionClavier(a[0])", base | {"key": k})["direction"] for k in ("ArrowDown", "ArrowLeft", "ArrowRight")]
    assert directions == ["bas", "gauche", "droite"]
    assert js("F.actionClavier(a[0])", base | {"key": "Enter"}) == {"type": "previsualiser"}
    assert js("F.actionClavier(a[0])", base | {"key": "Backspace"}) == {"type": "annuler"}
    assert js("F.actionClavier(a[0])", base | {"key": "a"}) is None
    # Les flèches ne font pas défiler la page quand le pavé a le focus, même maintenues.
    assert js("F.retientLaTouche(a[0])", base | {"repeat": True}) is True
    assert js("F.retientLaTouche(a[0])", base | {"dansLePave": False}) is False
    assert js("F.retientLaTouche(a[0])", base | {"balise": "SELECT"}) is False


@sans_node
def test_page_retour_court_apres_chaque_etape():
    avancer = {"nom": "avancer", "valeur": 1}
    non_verifiable = {"verification": "non vérifiable", "constat": mouvement.POSITION_NON_FOURNIE}
    assert js("F.ligneRetour(a[0], a[1])", avancer, non_verifiable) == "↑ 1 m : émise, arrêt acquitté — position non fournie"
    refus = {"echec": "statut de travail 4 hors de la liste : mouvement refusé (SEC-01)"}
    assert js("F.ligneRetour(a[0], a[1])", avancer, refus) == "↑ 1 m : refusée : " + refus["echec"]
    assert js("F.ligneRetour(a[0], a[1])", avancer, {"verification": "vérifiée"}) == "↑ 1 m : émise, arrêt acquitté — déplacement constaté"
    anomalie = {"verification": "anomalie", "constat": "le robot n'a pas bougé (patinage ou ordre ignoré)"}
    assert "n'a pas bougé" in js("F.ligneRetour(a[0], a[1])", avancer, anomalie)
    assert js("F.ligneRetour(a[0], a[1])", avancer, {"simulee": True}) == "↑ 1 m : simulée, rien n'est émis"
    assert js("F.ligneRetour(a[0], a[1])", {"nom": "tourner", "valeur": -45}, None) == "→ 45° : en attente"
    # Mode file : l'étape d'un bilan de séquence porte son ordre en clair.
    assert js("F.ligneRetour(a[0], a[1])", "reculer 2", non_verifiable) == "reculer 2 : émise, arrêt acquitté — position non fournie"
