"""Ordres depuis la page, au vu du premier ordre réel (2026-10-12).

Symptômes rapportés : « seule la première demande a fonctionné » (trois étapes composées, une
seule émise), « le bouton reste grisé et incliquable », 75 s d'attente après l'ordre.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess

import pytest
from conftest import lignes_journal
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import capture, mouvement, ui

SANS_POSE = {"workStatus": 1, "battery": 80}  # le robot réel : un état, jamais de position
EN_RETOUR = {"workStatus": 4, "battery": 80}
RECULER = {"nom": "reculer", "duree": 0.3, "vitesse": 0.1}
TROIS_ETAPES = [RECULER, {"nom": "tourner", "duree": 0.3, "vitesse": 0.2}, {"nom": "avancer", "duree": 0.3, "vitesse": 0.1}]
TRAME = json.dumps({"message": base64.b64encode(b"\x2a\x04\x08\x05\x30\x01").decode()})


async def emettre(http, serveur, ordres):
    statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": ordres})
    assert statut == 200 and corps["identifiant"], corps
    statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
    return statut, corps


def test_bouton_emettre_ne_reste_pas_grise_apres_un_ordre_sans_position(tmp_path, monkeypatch, session_reelle, lien):
    """Régression : après un ordre réel sans pose, tout restait bloqué 75 s, puis refusé sans explication."""
    lien.robot = dict(SANS_POSE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            statut, _corps = await emettre(http, serveur, [RECULER])
            assert statut == 202 and serveur.instantane()["occupe"] is True
            await attendre(lambda: not serveur.occupe, delai=4.0)  # et non 75 s
            etape = serveur.resultat["etapes"][0]
            assert etape["verification"] == "non vérifiable" and etape["constat"] == mouvement.POSITION_NON_FOURNIE
            assert serveur.resultat["code_sortie"] == 0 and serveur.resultat["interrompue"] is False
            assert serveur.instantane()["ordres"] == {"statut": "prêt, en attente d'ordre", "mouvement_refuse": None}
            # La page est de nouveau utilisable : un second ordre se prévisualise et s'émet.
            statut, _corps = await emettre(http, serveur, [RECULER])
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=4.0)

            # Le robot part en « retour à la base » : dit en clair AVANT toute prévisualisation.
            lien.robot = dict(EN_RETOUR)
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.instantane()["ordres"]["mouvement_refuse"] is not None)
            assert serveur.instantane()["ordres"] == {
                "statut": "retour à la base", "mouvement_refuse": "mouvement refusé tant que le robot est en : retour à la base",
            }
            # Un ordre refusé rend la main lui aussi, avec son motif et la reprise proposée.
            statut, _corps = await emettre(http, serveur, [RECULER, RECULER])
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=4.0)
            assert "SEC-01" in serveur.resultat["motif"] and serveur.resultat["reprise"]["etape"] == 1
            statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [RECULER]})
            assert statut == 200 and corps["identifiant"]  # toujours utilisable

    executer(scenario())


def test_trois_etapes_composees_trois_ordres_dans_la_previsualisation_et_dans_la_session_emise(
    tmp_path, monkeypatch, session_reelle, lien
):
    lien.robot = dict(SANS_POSE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            statut, corps = await emettre(http, serveur, TROIS_ETAPES)
            assert statut == 202 and len(corps["etapes"]) == 3 and len(corps["bilan"]["etapes"]) == 3
            assert [etape.split()[0] for etape in corps["etapes"]] == ["reculer", "tourner", "avancer"]
            await attendre(lambda: not serveur.occupe, delai=8.0)
            assert [etape["issue"] for etape in serveur.resultat["etapes"]] == ["émise, non vérifiable"] * 3
            assert serveur.resultat["interrompue"] is False and "reprise" not in serveur.resultat
            return corps["etapes"]

    etapes = executer(scenario())
    session = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "session-reelle")
    assert session["ordres"] == etapes and len(session["ordres"]) == 3
    assert [ligne["evenement"] for ligne in lignes_journal(tmp_path)].count("ordre-emis") == 3


def test_sequence_interrompue_par_le_statut_reprise_proposee_a_la_page(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(SANS_POSE)

    def puis_retour(lien_, octets):
        if octets == bytes.fromhex("10 31 52 0a 0d 00 00 00 00 15 00 00 00 00"):  # arrêt : fin de la première étape
            lien_.robot = dict(EN_RETOUR)

    lien.sur_publication = puis_retour

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await emettre(http, serveur, TROIS_ETAPES)
            await attendre(lambda: not serveur.occupe, delai=6.0)
            resultat = serveur.resultat
            assert resultat["interrompue"] is True and "retour à la base" in resultat["motif"] and len(resultat["etapes"]) == 1
            assert resultat["reprise"]["etape"] == 2 and [o["nom"] for o in resultat["reprise"]["ordres"]] == ["tourner", "avancer"]
            # « Reprendre à l'étape 2 » : la page renvoie ces ordres tels quels à la prévisualisation.
            statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": resultat["reprise"]["ordres"]})
            assert statut == 200 and len(corps["etapes"]) == 2 and corps["identifiant"]

    executer(scenario())


# --- enregistrement de diagnostic pendant les ordres réels ---------------------


def test_trames_brutes_enregistrees_pendant_l_ordre_reel_et_un_peu_apres(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = dict(SANS_POSE)
    monkeypatch.setattr(ui, "SUITE_CAPTURE_S", 0.3)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            session_reelle.brut("/device/robot-test/pboutput", TRAME, False)  # hors de tout ordre : rien
            assert not (tmp_path / "captures").exists()
            await emettre(http, serveur, [RECULER])
            session_reelle.brut("/device/robot-test/pboutput", TRAME, False)  # pendant l'ordre
            await attendre(lambda: not serveur.occupe, delai=4.0)
            session_reelle.brut("/device/robot-test/pboutput", TRAME, False)  # dans les secondes qui suivent
            await attendre(lambda: serveur._ecoute is None, delai=3.0)
            session_reelle.brut("/device/robot-test/pboutput", TRAME, False)  # après : plus rien

    executer(scenario())
    (dossier,) = (tmp_path / "captures").iterdir()
    assert dossier.name.startswith("ordre-")
    index = [json.loads(ligne) for ligne in (dossier / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(index) == 2 and index[0]["champs"] == [[5, "LEN", 4]]
    assert base64.b64decode(json.loads((dossier / "0001.json").read_text("utf-8"))["charge_b64"]).decode() == TRAME
    assert "capture-ordre" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_enregistrement_jamais_bloquant_ni_en_simulation(tmp_path, monkeypatch, session_reelle, lien):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, _http):
            serveur._ouvrir_ecoute()
            assert serveur._ecoute is None  # simulation : aucun ordre réel, aucun enregistrement

            class Casse:
                def recevoir(self, *_arguments):
                    raise OSError("disque plein")

            serveur._ecoute = Casse()
            serveur._brut("/device/robot-test/pboutput", TRAME, False)  # ne lève pas
            assert serveur._ecoute is None

    executer(scenario())
    fichier = tmp_path / "pas-un-dossier"
    fichier.write_text("x", encoding="utf-8")
    assert capture.pour_un_ordre(fichier) is None  # dossier impossible : pas d'enregistrement, pas d'erreur


def test_sec03_enregistrement_differe_pendant_un_arret(tmp_path):
    en_arret = [True]
    ecoute = capture.Enregistreur(tmp_path / "c", attendre=lambda: en_arret[0])
    ecoute.recevoir("/device/robot-test/pboutput", TRAME)
    ecoute.recevoir("/device/robot-test/pboutput", TRAME)
    assert list((tmp_path / "c").iterdir()) == []  # rien sur disque pendant l'arrêt
    en_arret[0] = False
    ecoute.recevoir("/device/robot-test/pboutput", TRAME)
    index = [json.loads(ligne) for ligne in (tmp_path / "c" / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [ligne["numero"] for ligne in index] == [1, 2, 3] and index[0]["t_s"] <= index[2]["t_s"]


def test_ordre_reel_par_la_commande_enregistre_aussi_les_trames(capsys, tmp_path, monkeypatch, session, lien):
    from contextlib import asynccontextmanager

    from lymow_pilotage import cli, liaison

    lien.robot = dict(SANS_POSE)

    @asynccontextmanager
    async def ouvrir(_journal, _cal, autorisees, *_reste, brut=None):
        session.autorisees = autorisees
        brut("/device/robot-test/pboutput", TRAME, False)
        yield session

    monkeypatch.setattr(liaison, "ouvrir_session", ouvrir)
    assert cli.main(["reculer", "1", "--reel", "--json", "--donnees", str(tmp_path)]) == 0
    bilan = json.loads(capsys.readouterr().out)
    assert bilan["verification"] == "non vérifiable" and bilan["issue"] == "émise, non vérifiable"
    (dossier,) = (tmp_path / "captures").iterdir()
    assert dossier.name.startswith("ordre-") and (dossier / "0001.json").exists()


# --- liaison coupée (autre application) ----------------------------------------


def test_liaison_coupee_dite_en_clair_et_retablie_sans_page_figee(tmp_path, monkeypatch, session_reelle, lien):
    monkeypatch.setattr(ui, "PERIODE_VEILLE_S", 0.05)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, _http):
            assert serveur.etat_liaison == "connecté" and len(serveur.ouvertures) == 1
            lien.connecte = False  # le courtier a fermé notre liaison
            await attendre(lambda: any(ui.LIAISON_COUPEE in ligne["texte"] for ligne in serveur.fil))
            assert "une autre application" in ui.LIAISON_COUPEE
            await attendre(lambda: len(serveur.ouvertures) >= 2)  # reconnexion sans aucun clic
            lien.connecte = True
            await attendre(lambda: any(ligne["texte"] == "liaison rétablie" for ligne in serveur.fil))
            await attendre(lambda: serveur.etat_liaison == "connecté" and serveur.session is not None)
            assert serveur.instantane()["ordres"]["mouvement_refuse"] is None

            # Pendant un ordre, la veille ne touche à rien : le mouvement gère lui-même la coupure (SEC-03).
            ouvertures = len(serveur.ouvertures)
            serveur.tache = asyncio.ensure_future(asyncio.sleep(30))
            lien.connecte = False
            await asyncio.sleep(0.3)
            assert len(serveur.ouvertures) == ouvertures
            assert serveur.instantane()["ordres"]["mouvement_refuse"].startswith("robot injoignable")
            serveur.tache.cancel()
            await asyncio.gather(serveur.tache, return_exceptions=True)
            lien.connecte = True

    executer(scenario())


# --- logique de la page (ordres.js), exécutée hors navigateur -------------------

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")


def js(expression: str, *arguments: object) -> object:
    script = (
        "const O = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
        f"process.stdout.write(JSON.stringify({expression} ?? null));"
    )
    sortie = subprocess.run(
        [NODE, "-e", script, str(ui.STATIQUE / "ordres.js"), json.dumps(arguments)],
        capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
    )
    return json.loads(sortie.stdout)


@sans_node
def test_page_le_bouton_d_emission_dit_combien_d_etapes_partiront():
    assert js("O.libelleEmission(a[0])", ["reculer 2", "tourner 90", "avancer 1"]) == "Émettre la séquence (3 étapes)"
    assert js("O.libelleEmission(a[0])", ["reculer 2"]) == "Émettre l'ordre"
    assert js("O.libelleEmission(a[0])", []) == "Émettre l'ordre"


@sans_node
def test_page_un_ordre_seul_previsualise_avec_une_sequence_composee_est_signale():
    """Le piège du 2026-10-12 : trois étapes dans la séquence, « Prévisualiser » d'un seul formulaire."""
    alerte = js("O.avertissementOrdreSeul(a[0], a[1])", ["reculer 2"], 3)
    assert "ne contient PAS la séquence composée (3 étapes)" in alerte and "Prévisualiser la séquence" in alerte
    assert js("O.avertissementOrdreSeul(a[0], a[1])", ["reculer 2", "tourner 90", "avancer 1"], 3) == ""
    assert js("O.avertissementOrdreSeul(a[0], a[1])", ["reculer 2"], 0) == ""


@sans_node
def test_page_un_bouton_emettre_indisponible_dit_toujours_pourquoi():
    cas = [
        {"occupe": True, "previsualisationValide": True, "emission": True, "note": ""},
        {"occupe": False, "previsualisationValide": False, "emission": True, "note": ""},
        {"occupe": False, "previsualisationValide": False, "emission": False, "note": ""},
        {"occupe": False, "previsualisationValide": False, "emission": True, "note": "Prévisualisation refusée : SEC-01"},
    ]
    motifs = [js("O.motifEmissionIndisponible(a[0])", c) for c in cas]
    assert all(motifs) and "STOP reste disponible" in motifs[0] and "Prévisualisez" in motifs[1]
    assert "Simulation" in motifs[2] and motifs[3] == "Prévisualisation refusée : SEC-01"
    disponible = {"occupe": False, "previsualisationValide": True, "emission": True, "note": "Valable 60 s"}
    assert js("O.motifEmissionIndisponible(a[0])", disponible) == ""


@sans_node
def test_page_reprise_et_statut_en_clair():
    reprise = {"etape": 2, "ordres": [{"nom": "tourner", "valeur": 90}, {"nom": "avancer", "valeur": 1}]}
    assert js("O.libelleReprise(a[0])", reprise) == "Reprendre à l'étape 2 (2 étapes restantes)"
    refus = {"statut": "retour à la base", "mouvement_refuse": "mouvement refusé tant que le robot est en : retour à la base"}
    assert js("O.texteEtatDesOrdres(a[0])", refus) == "Robot : retour à la base — " + refus["mouvement_refuse"]
    assert js("O.texteEtatDesOrdres(a[0])", {"statut": "en charge", "mouvement_refuse": None}) == "Robot : en charge"
    assert js("O.texteOrdre(a[0])", {"nom": "reculer", "valeur": 2, "vitesse": None, "duree": None}) == "reculer 2"


def test_page_liste_les_etapes_et_n_a_plus_de_bouton_grise_muet():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    script = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    for identifiant in ("previsualisation-etapes", "previsualisation-avertissement", "ordres-etat", "reprise", "emettre-note"):
        assert f'id="{identifiant}"' in page and f'"{identifiant}"' in script
    assert page.index('src="/statique/ordres.js"') < page.index('src="/statique/app.js"')
    assert "reponse.etapes" in script and "Ordres.libelleEmission(etapes)" in script
    assert "Ordres.avertissementOrdreSeul(etapes" in script and "Ordres.motifEmissionIndisponible" in script
    # « Émettre » n'est désactivé qu'en un seul endroit, qui affiche toujours le motif à côté.
    assert script.count('$("emettre").disabled') == 1 and 'oublierPrevisualisation("")' in script
    assert "le bouton « Prévisualiser » d'un formulaire ne prévisualise que SON ordre" in page
