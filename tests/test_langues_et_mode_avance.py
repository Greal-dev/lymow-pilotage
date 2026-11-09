"""Interface commercialisable (2026-11-09) : quatre langues, mode avancé, compte en tête, STOP toujours visible.

La présentation seule change : aucune règle de sécurité, aucune route d'ordre. Ces tests gardent le contrat
de la page (clés de traduction, ce qui est masqué, ce qui ne l'est jamais) ; l'essai en vrai navigateur est
dans `outils/essai_navigateur.py` (scénario « interface »).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from lymow_pilotage import diagnostic, etat, ui

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
PAGE = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
STYLE = (ui.STATIQUE / "style.css").read_text(encoding="utf-8")
HAUT = PAGE[PAGE.index('<div class="haut">') : PAGE.index("<main>")]
VIDES = {"br", "input", "meta", "link", "img", "meter"}
# Ce qu'un client doit toujours pouvoir faire, quel que soit le mode : jamais derrière « Mode avancé ».
ESSENTIELS = (
    "stop", "stop-centre", "prendre-main", "rendre-main", "base-bouton", "annuler-bouton", "pause-bouton", "erreur-annuler",
    "fleches-bascule", "bandeau-attestation", "attestation-retirer", "compte-entete", "langue", "mode-avance", "video", "carte-dessin",
    "pastilles", "fil", "nettoyage-redescendre",
)


class Arbre(HTMLParser):
    """Chaque élément : ses attributs, ses ancêtres, et son texte direct (sans celui des enfants)."""

    def __init__(self) -> None:
        super().__init__()
        self.pile: list[dict] = []
        self.elements: list[dict] = []

    def handle_starttag(self, balise, attributs):
        noeud = {"balise": balise, "attrs": dict(attributs), "ancetres": list(self.pile), "textes": []}
        self.elements.append(noeud)
        if balise not in VIDES:
            self.pile.append(noeud)

    def handle_endtag(self, balise):
        if balise in VIDES:
            return
        while self.pile:
            if self.pile.pop()["balise"] == balise:
                break

    def handle_data(self, donnees):
        if self.pile:
            self.pile[-1]["textes"].append(donnees)


ARBRE = Arbre()
ARBRE.feed(PAGE)


def element(identifiant: str) -> dict:
    return next(e for e in ARBRE.elements if e["attrs"].get("id") == identifiant)


def classes(noeud: dict) -> set[str]:
    return set((noeud["attrs"].get("class") or "").split())


def js(fichier: str, expression: str, *arguments, langue: str = "fr"):
    script = (
        f"const I = require({json.dumps(str(ui.STATIQUE / 'i18n.js'))}); I.choisir({json.dumps(langue)});"
        "const M = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(ui.STATIQUE / fichier), json.dumps(arguments)],
        capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
    )
    return json.loads(sortie.stdout)


def dictionnaires() -> dict[str, dict[str, str]]:
    return js("i18n.js", "M.DICTIONNAIRES")


# --- traductions -------------------------------------------------------------------------------------------


@sans_node
def test_les_quatre_dictionnaires_ont_exactement_les_memes_cles_toutes_remplies():
    textes = dictionnaires()
    assert sorted(textes) == ["de", "en", "es", "fr"]
    for langue in ("en", "de", "es"):
        assert set(textes[langue]) == set(textes["fr"]), langue
    for langue, dictionnaire in textes.items():
        vides = [cle for cle, texte in dictionnaire.items() if not isinstance(texte, str) or not texte.strip()]
        assert vides == [], (langue, vides)
    # Mêmes paramètres « {nom} » dans chaque langue : aucun texte ne perd une valeur.
    for cle, texte in textes["fr"].items():
        attendus = sorted(re.findall(r"\{[\w-]+\}", texte))
        for langue in ("en", "de", "es"):
            assert sorted(re.findall(r"\{[\w-]+\}", textes[langue][cle])) == attendus, (langue, cle)


@sans_node
def test_toute_cle_de_la_page_existe_et_le_francais_est_le_texte_d_origine():
    francais = dictionnaires()["fr"]
    cles = re.findall(r'data-i18n(?:-title|-aria)?="([^"]+)"', PAGE)
    assert len(cles) > 150
    assert sorted({c for c in cles if c not in francais}) == []
    blanc = lambda texte: re.sub(r"\s+", " ", texte).strip()  # noqa: E731
    for noeud in ARBRE.elements:
        attrs = noeud["attrs"]
        if "data-i18n-title" in attrs:
            assert attrs.get("title") == francais[attrs["data-i18n-title"]], attrs["data-i18n-title"]
        if "data-i18n-aria" in attrs:
            assert attrs.get("aria-label") == francais[attrs["data-i18n-aria"]], attrs["data-i18n-aria"]
        if "data-i18n" in attrs:
            # Le texte remplacé est le dernier texte direct non vide (un libellé peut contenir un champ ou une icône).
            textes = [blanc(t) for t in noeud["textes"] if t.strip()]
            assert textes and textes[-1] == blanc(francais[attrs["data-i18n"]]), attrs["data-i18n"]


@sans_node
def test_statuts_et_erreurs_traduits_depuis_le_code_meme_francais_que_le_serveur():
    francais = dictionnaires()["fr"]
    assert {int(c[7:]): v for c, v in francais.items() if re.fullmatch(r"statut\.-?\d+", c)} == etat.LIBELLES_STATUT
    assert francais["statut.aucun"] == etat._libelle_statut(None)
    assert {int(c[7:]): v for c, v in francais.items() if re.fullmatch(r"erreur\.\d+", c)} == diagnostic.ERREURS_FR
    # Le code d'abord ; à défaut, le libellé français du serveur ; inconnu : tel quel.
    assert js("i18n.js", "M.statut('tonte en cours', 2)", langue="en") == "mowing"
    assert js("i18n.js", "M.statut('libellé du serveur', 3)", langue="de") == "Mähen pausiert"
    assert js("i18n.js", "M.statut('en charge')", langue="es") == "cargando"
    assert js("i18n.js", "M.statut('statut inconnu (99)')", langue="en") == "statut inconnu (99)"
    assert js("i18n.js", "M.statut('en charge', 5)") == "en charge"  # français : le texte du serveur, tel quel
    assert js("i18n.js", "M.erreur('44 : pare-chocs enfoncé (Bumper stuck)')", langue="en") == "44 : bumper pressed"
    assert js("i18n.js", "M.erreur('23 : « Map missing »')", langue="en") == "23 : Map missing"
    assert js("i18n.js", "M.erreur('23 : « Map missing »')", langue="de") == "23 : « Map missing »"


@sans_node
def test_textes_generes_par_la_page_suivent_la_langue_et_restent_identiques_en_francais():
    image = {
        "connecte": True, "liaison": "connecté",
        "ordres": {"statut": "tonte en pause", "mouvement_refuse": None,
                   "erreur": {"resolue": False, "libelles": ["44 : pare-chocs enfoncé (Bumper stuck)"], "annulation": None}},
        "etat": {"statut": 3, "batterie_pct": 87, "precision_m": 0.02, "localisation": "fiable", "localisation_detail": ""},
        "plateau": {"mm": 96, "maximum_mm": 100, "minimum_mm": 20, "au_maximum": False},
    }
    textes = lambda langue: [p["texte"] for p in js("ordres.js", "M.pastilles(a[0])", image, langue=langue)]  # noqa: E731
    assert textes("fr") == ["⚠ erreur 44", "tonte en pause", "🔋 87 %", "±0,02 m", "plateau 96 mm", "liaison ✓"]
    assert textes("en") == ["⚠ error 44", "mowing paused", "🔋 87 %", "±0.02 m", "deck 96 mm", "link ✓"]
    assert textes("de")[1] == "Mähen pausiert" and textes("es")[1] == "corte en pausa"
    resume = {"en_ligne": True, "statut": 2, "statut_libelle": "tonte en cours", "batterie_pct": 80, "precision_m": 0.02,
              "localisation": "fiable", "localisation_texte": "position : ±0,02 m — fiable"}
    assert js("ordres.js", "M.resumeEtat(a[0], false)", resume) == "en ligne · tonte en cours · batterie 80 % · position : ±0,02 m — fiable"
    assert js("ordres.js", "M.resumeEtat(a[0], false)", resume, langue="en") == "online · mowing · battery 80% · position: ±0.02 m — reliable"
    assert js("ordres.js", "M.texteDeLaMain('outil')", langue="es") == "Usted tiene el control"
    diagnostic_en = js("ordres.js", "M.diagnosticErreur(a[0])", image["ordres"]["erreur"] | {"phrase": "En erreur…"}, langue="en")
    assert diagnostic_en["titre"] == "Robot in error — 44 : bumper pressed" and "Cancel the task" in diagnostic_en["phrase"]
    assert js("fleches.js", "M.libelleBascule(false)", langue="de") == "Pfeile: aktivieren"
    assert js("fleches.js", "M.libelleBascule(false)") == "Flèches : activer"


# --- sélecteur de langue, mode avancé, compte ----------------------------------------------------------------


def test_en_tete_porte_langue_mode_avance_et_compte():
    assert 'id="langue"' in HAUT and 'id="mode-avance" type="checkbox" role="switch"' in HAUT and 'id="compte-entete"' in HAUT
    langue = HAUT[HAUT.index('<select id="langue"') : HAUT.index("</select>")]
    assert re.findall(r'<option value="(\w+)"', langue) == ["fr", "en", "de", "es"]
    assert '<html lang="fr">' in PAGE  # français par défaut ; i18n.js met à jour l'attribut
    scripts = re.findall(r'<script src="/statique/([\w.]+)"></script>', PAGE)
    assert scripts[0] == "i18n.js" and scripts[-1] == "avance.js"  # les textes d'abord, la bascule en dernier
    assert {"i18n.js", "avance.js"} <= set(ui.FICHIERS)
    i18n = (ui.STATIQUE / "i18n.js").read_text(encoding="utf-8")
    assert 'var CLE = "lymow-langue";' in i18n and "document.documentElement.lang = langue;" in i18n
    assert 'return LANGUES.indexOf(proposee) >= 0 ? proposee : "fr";' in i18n  # langue du navigateur si l'une des 4
    avance = (ui.STATIQUE / "avance.js").read_text(encoding="utf-8")
    assert 'var CLE = "lymow-mode-avance";' in avance and 'appliquer(memoire === "1");' in avance  # désactivé par défaut
    for source in (i18n, avance):  # jamais d'exception si le stockage est refusé
        for ligne in source.splitlines():
            if "localStorage" in ligne:
                assert "try {" in ligne and "catch" in ligne, ligne
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    # Bouton du compte : mêmes routes et même flux que le volet ; déconnexion en deux clics, sans boîte.
    compte = app[app.index("async function compteEnTete()") :]
    compte = compte[: compte.index("\n  }\n")]
    assert "await deconnecter();" in compte and "await commencerConnexion();" in compte and "window.confirm(" not in app
    assert 'deuxClics("deconnecter"' in app and '$("compte-entete").addEventListener("click", compteEnTete);' in app


def test_stop_et_l_essentiel_ne_sont_jamais_derriere_le_mode_avance():
    assert "body:not(.mode-avance) .avance { display: none !important; }" in STYLE
    for identifiant in ESSENTIELS:
        noeud = element(identifiant)
        chaine = [noeud, *noeud["ancetres"]]
        assert not any("avance" in classes(n) for n in chaine), identifiant
    assert 'id="stop"' in HAUT and ".haut { position: sticky" in STYLE  # STOP en tête, collant
    # Ce qui passe derrière le mode avancé : masqué seulement, toujours dans la page.
    for identifiant in ("volet-ordres", "volet-calibration", "volet-carte", "volet-journal", "volet-details", "phares-bouton",
                        "recalage", "bon-signal-bouton", "pave-plateau", "recalage-ici", "nettoyage-essai", "version-serveur"):
        noeud = element(identifiant)
        assert any("avance" in classes(n) for n in [noeud, *noeud["ancetres"]]), identifiant
    attestation = next(e for e in ARBRE.elements if e["attrs"].get("data-attestation") == "poser")
    assert any("avance" in classes(n) for n in attestation["ancetres"])  # la proposition ; le bandeau actif, jamais


def test_robot_en_erreur_l_encadre_et_annuler_la_tache_s_ouvrent_d_eux_memes():
    tableau = (ui.STATIQUE / "tableau.js").read_text(encoding="utf-8")
    assert "if (enErreur && !erreurVue) { erreurOuverte = true; }" in tableau
    assert "#erreur-annuler { background: var(--danger);" in STYLE
