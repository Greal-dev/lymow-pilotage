"""Tableau de bord compact (retour du propriétaire, 2026-10-12) : moins de légendes, plus de boutons, moins de boîtes.

La logique d'affichage vit dans ordres.js (fonctions pures, testées par Node) ; la mise en forme dans tableau.js.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from conftest import fragment
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import ui

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
PAGE = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
STYLE = (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
GAUCHE = PAGE[PAGE.index('class="colonne-commandes"') : PAGE.index('<div class="colonne-vue">')]


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


# --- A. moins de texte, plus de boutons ---------------------------------------------------------------


def test_colonne_de_gauche_sans_paragraphe_d_explication_permanent():
    # Tout texte permanent de la colonne est une valeur, un libellé de bouton ou de case : aucune phrase d'explication.
    visible = re.sub(r"<[^>]+>", "\n", GAUCHE)  # les attributs (dont les info-bulles) disparaissent avec les balises
    phrases = [ligne.strip() for ligne in visible.splitlines() if len(ligne.strip()) > 70]
    assert phrases == [], phrases
    for explication in ("Activées, elles font bouger le robot", "Pour savoir si les corrections", "Prévisualisez un ordre ou la séquence"):
        assert explication not in GAUCHE
    # Le « pourquoi / comment » est en info-bulle.
    assert GAUCHE.count('title="') >= 20
    assert ".vers-pastilles, .vers-fil { display: none !important; }" in STYLE


def test_pave_de_fleches_a_gauche_avec_stop_au_centre_et_segments_au_lieu_des_listes():
    croix = GAUCHE[GAUCHE.index('<div class="croix"') :]
    croix = croix[: croix.index("</div>")]
    assert [m for m in re.findall(r'data-fleche="(\w+)"', croix)] == ["haut", "gauche", "droite", "bas"]
    assert 'id="stop-centre"' in croix and ">STOP<" in croix
    tableau = (ui.STATIQUE / "tableau.js").read_text(encoding="utf-8")
    assert '$("stop-centre").addEventListener("click", function () { $("stop").click(); });' in tableau  # le même STOP
    for groupe, liste in (("segments-distance", "pave-distance"), ("segments-angle", "pave-angle"), ("segments-allure", "pave-allure")):
        assert f'id="{groupe}" class="segments" role="group"' in GAUCHE
        assert f'<select id="{liste}" class="masque"' in GAUCHE  # la liste reste la source, masquée
        assert f'segments("{liste}", "{groupe}");' in tableau
    assert "Clavier : touches 1 à 4" in GAUCHE


def test_actions_en_grille_de_boutons_et_cibles_d_au_moins_quarante_pixels():
    grille = GAUCHE[GAUCHE.index('<p class="actions-robot">') :]
    grille = grille[: grille.index("</p>")]
    libelles = [re.sub(r"<[^>]+>", "", b).strip() for b in re.findall(r"<button[^>]*>(.*?)</button>", grille)]
    assert [re.sub(r"^\S+\s", "", libelle) for libelle in libelles] == [
        "Pause", "Reprendre", "Retour base", "Annuler tâche", "Plateau haut", "Phares : allumer", "Recalage assisté", "Bon signal",
    ]
    assert "Plateau bas" not in PAGE  # ambigu : n'existe pas
    assert ".actions-robot { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));" in STYLE
    # Tailles de cible : 2,5 rem = 40 px au moins pour les boutons du tableau.
    for regle in (".segments button { flex: 1; min-height: 2.5rem; min-width: 2.5rem;", ".actions-robot button { min-height: 2.6rem;",
                  ".ligne-bascule .bascule { flex: 1; min-height: 2.6rem; }", "grid-template-columns: repeat(3, 3.3rem); grid-template-rows: repeat(3, 3.3rem);"):
        assert regle in STYLE
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    # Bouton indisponible : grisé, sa raison en info-bulle.
    assert "bouton.disabled = Boolean(raison);" in app and "bouton.title = raison || titresDOrigine[id];" in app
    assert 'griser("reprendre-bouton", image.ordres && image.ordres.reprise_possible ? "" : "Le robot n\'est pas en pause : rien à reprendre.");' in app


def test_sequence_et_previsualisation_n_occupent_de_place_qu_en_composition():
    assert "#bloc-pilotage:not(.composition) .ligne-sequence, #bloc-pilotage:not(.composition) #volet-details { display: none; }" in STYLE
    tableau = (ui.STATIQUE / "tableau.js").read_text(encoding="utf-8")
    assert 'var montrer = $("composer").checked || page.sequence().length > 0;' in tableau
    assert '$("bloc-pilotage").classList.toggle("composition", montrer);' in tableau


@sans_node
def test_pastilles_d_etat_compactes():
    image = {
        "connecte": True, "liaison": "connecté",
        "ordres": {"statut": "tonte en pause", "mouvement_refuse": None, "bord": {"devant_m": 2.4, "texte": "d'après la carte : bord de zone devant à 2,4 m"}},
        "etat": {"batterie_pct": 87, "precision_m": 0.02, "localisation": "fiable", "localisation_detail": ""},
        "plateau": {"mm": 96, "maximum_mm": 100, "minimum_mm": 20, "au_maximum": False, "nature": "réglage global de tonte", "date": None, "suite": None},
    }
    assert [p["texte"] for p in js("M.pastilles(a[0])", image)] == ["tonte en pause", "🔋 87 %", "±0,02 m", "bord 2,4 m", "plateau 96 mm", "liaison ✓"]
    erreur = {"resolue": False, "libelles": ["44 : pare-chocs enfoncé (Bumper stuck)"], "annulation": None}
    en_erreur = js("M.pastilles(a[0])", image | {"ordres": image["ordres"] | {"statut": "en erreur", "erreur": erreur}})
    assert en_erreur[0] == {
        "texte": "⚠ erreur 44", "classe": "erreur", "action": "erreur",
        "titre": "Cliquez : diagnostic et geste proposé — 44 : pare-chocs enfoncé (Bumper stuck)",
    }
    perdue = js("M.pastilles(a[0])", image | {"etat": image["etat"] | {"precision_m": 4.68, "localisation": "perdue"}, "alerteBord": True})
    assert [(p["texte"], p.get("classe")) for p in perdue][2:4] == [("±4,68 m", "perdue"), ("bord 2,4 m", "alerte")]
    suspendue = js("M.pastilles(a[0])", {"connecte": False, "liaison": "Liaison suspendue", "ordres": {"suspendue": True, "texte": "Liaison suspendue : l'application Lymow a la main"}, "etat": None})
    assert suspendue[0]["texte"] == "liaison suspendue" and suspendue[-1] == {"texte": "liaison ✗", "classe": "", "titre": "Liaison suspendue"}  # sans alarme
    tableau = (ui.STATIQUE / "tableau.js").read_text(encoding="utf-8")
    assert 'erreurOuverte = !erreurOuverte; $("erreur-robot").hidden = !erreurOuverte;' in tableau  # la pastille ouvre le diagnostic


@sans_node
def test_fil_d_activite_trois_lignes_la_plus_recente_en_haut_et_gravite():
    lignes = []
    for texte in ("un", "deux", "deux", "trois", "quatre"):
        lignes = js("M.filAjouter(a[0], a[1])", lignes, {"texte": texte, "gravite": "info", "heure": "21:40"})
    assert [ligne["texte"] for ligne in lignes] == ["quatre", "trois", "deux"]  # trois lignes, sans redite
    assert js("M.filAjouter(a[0], a[1])", lignes, {"texte": ""}) == lignes
    gravite = lambda texte: js("M.graviteDe(a[0])", texte)  # noqa: E731
    assert gravite("ordre terminé : déplacement constaté") == "info"
    assert gravite("Pas d'avance : patinage ? (2 de suite)") == "alerte" and gravite("Liaison lente : en attente d'une nouvelle trame") == "alerte"
    assert gravite("étape 1 : d'après la carte : bord de zone devant à 0,35 m") == "alerte"
    assert gravite("ARRÊT NON CONFIRMÉ : arrêtez le robot") == "erreur" and gravite("mouvement refusé (SEC-01)") == "erreur"
    notes = [{"heure": "21:40:01", "texte": "a"}, {"heure": "21:40:02", "texte": "b"}, {"heure": "21:40:03", "texte": "c"}]
    assert js("M.notesNouvelles(a[0], a[1])", notes, "21:40:01a") == notes[1:]
    assert js("M.notesNouvelles(a[0], a[1])", notes, "21:40:03c") == [] and js("M.notesNouvelles(a[0], a[1])", notes, "") == notes[-1:]
    tableau = (ui.STATIQUE / "tableau.js").read_text(encoding="utf-8")
    assert 'document.querySelectorAll(".vers-fil").forEach' in tableau and "window.setTimeout(function () { toast.hidden = true; }, 4000);" in tableau
    assert 'id="fil" class="fil"' in GAUCHE and 'id="toast" class="toast" role="status" hidden' in PAGE
    assert "position: fixed" not in STYLE  # le rappel ne recouvre jamais STOP


@sans_node
def test_raccourcis_inactivite_et_reprendre_en_deux_clics():
    touche = lambda **e: js("M.raccourciDistance(a[0])", {"key": "2", "balise": "BODY", "actives": True, "nombre": 4} | e)  # noqa: E731
    assert touche() == 1 and touche(key="4") == 3 and touche(key="5") is None  # quatre pas : touches 1 à 4
    assert touche(actives=False) is None and touche(balise="INPUT") is None and touche(key="a") is None
    # Inactivité : le temps restant n'est affiché que dans la dernière minute.
    assert js("M.texteInactivite(true, 300)") == "" and js("M.texteInactivite(true, 61)") == ""
    assert js("M.texteInactivite(true, 45)") == "Flèches désactivées dans 45 s sans action." and js("M.texteInactivite(false, 45)") == ""
    # « Reprendre » relance les lames : deux clics, le second dans les 5 s.
    assert js("M.secondClic(0, 1000)") is False and js("M.secondClic(1000, 4000)") is True and js("M.secondClic(1000, 7000)") is False


def test_une_seule_confirmation_de_prise_de_main_par_session_et_boites_natives_limitees():
    pave = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    assert "var dejaConfirme = false;" in pave and pave.count("dejaConfirme = true;") == 1
    assert "if (!dejaConfirme && !window.confirm(Ordres.annoncePrendreLaMain(contexte()))) {" in pave
    assert pave.count("dejaConfirme = false") == 1  # jamais remis à zéro : STOP puis réarmement = un clic, sans boîte
    # Boîtes natives : seulement prise de main (1re fois), annuler la tâche, attestation, essais, reprendre quand même —
    # et, hors tableau de bord, le compte (effacement, connexion en un clic) et l'écriture de carte.
    permis = ("annoncePrendreLaMain", "CONFIRMATION_ATTESTATION", "CONFIRMATION_ESSAI_PLATEAU", "Annuler la tâche en cours", "dernierNettoyage.confirmation",
              "nom === \"annuler\" || nom === \"reprendre-quand-meme\"", "EXPLICATION_UN_CLIC", "Effacer de ce poste", "courante.tache")
    total = 0
    for fichier in sorted(ui.STATIQUE.glob("*.js")):
        source = fichier.read_text(encoding="utf-8")
        assert "alert(" not in source.replace("role=\"alert\"", ""), fichier.name
        for rang in [m.start() for m in re.finditer(r"window\.confirm\(", source)]:
            total += 1
            contexte = source[max(0, rang - 160) : rang + 120]
            assert any(mot in contexte for mot in permis), (fichier.name, contexte)
    assert total <= 9  # 9 depuis le 2026-10-13 : « Essayer un pas plateau levé » (SEC-01c) exige sa confirmation


# --- tâches d'un clic ------------------------------------------------------------------------------------


def test_taches_d_un_clic_sans_previsualisation_memes_gardes_qu_a_l_emission(tmp_path, monkeypatch, session_reelle, lien):
    assert ui.TACHES_IMMEDIATES == ("pause", "reprendre", "base", "annuler", "plateau-haut", "reprendre-quand-meme")
    lien.robot = fragment(statut=3, tache=2, precision=1.32)  # en pause, position perdue

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            for corps in ({}, {"nom": "base"}, {"nom": "avancer", "confirmer": True}, {"nom": "stop", "confirmer": True},
                          {"nom": "base", "confirmer": "oui"}, {"nom": "base", "confirmer": True, "valeur": 2}):
                statut, _reponse, _recus = await poster(http, serveur, "/api/tache", corps)
                assert statut == 400 and lien.ordres == []  # jamais un mouvement, jamais sans confirmation explicite
            # La garde de localisation (SEC-12) juge toujours « reprendre ».
            statut, _reponse, _recus = await poster(http, serveur, "/api/tache", {"nom": "reprendre", "confirmer": True})
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=20.0)
            assert "SEC-12" in serveur.resultat["motif"] and lien.ordres == []
            statut, _reponse, _recus = await poster(http, serveur, "/api/tache", {"nom": "base", "confirmer": True})
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=20.0)
            assert len(lien.ordres) >= 1 and serveur.resultat["code_sortie"] == 0
            # Pendant un ordre : refusé, sans rien mettre en file.
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            await poster(http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "gauche", "pas": 90})
            statut, reponse, _recus = await poster(http, serveur, "/api/tache", {"nom": "plateau-haut", "confirmer": True})
            assert statut == 409 and "en cours" in reponse["erreur"]
            await poster(http, serveur, "/api/stop")
            await attendre(lambda: not serveur.occupe, delai=20.0)
            # Main rendue : refusé.
            await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            statut, _reponse, _recus = await poster(http, serveur, "/api/tache", {"nom": "base", "confirmer": True})
            assert statut == 409

    executer(scenario())
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert 'await appeler("/api/tache", { nom: nom, confirmer: true });' in app
    assert 'if (nom === "reprendre" && !Ordres.secondClic(reprendreArme, Date.now())) {' in app


def test_tache_d_un_clic_en_simulation_rien_n_est_emis(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            statut, reponse, _recus = await poster(http, serveur, "/api/tache", {"nom": "base", "confirmer": True})
            assert statut == 200 and reponse["simulation"] is True and lien.ordres == []

    executer(scenario())
