"""Robot en erreur : la main n'est plus perdue au premier pas sans effet, et le diagnostic propose un geste.

Essai réel du 2026-10-12 (19:25) : statut « en erreur », erreur 44 (pare-chocs enfoncé), tâche « retour à la
base ». Cinq « reculer 1 » émis et acquittés, pose inchangée — et le pilotage direct désarmé à chaque fois.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from conftest import est_consigne, fragment
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import diagnostic, mouvement, ui
from lymow_pilotage.vendor.ha_lymow import const as socle

NODE = shutil.which("node")
sans_node = pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
ANNULER = bytes.fromhex("10 31 28 1c")
PAUSES = (bytes.fromhex("10 31 28 03"), bytes.fromhex("10 31 28 15"))


def en_erreur(**plus) -> dict:
    """L'état relevé pendant l'essai réel : en erreur, pare-chocs enfoncé, retour à la base, position perdue."""
    return fragment(statut=7, tache=4, est=3.0, nord=2.0, precision=4.68) | {"errorCodes": [44]} | plus


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


async def pas_direct(http, serveur, direction="bas"):
    statut, corps, _recus = await poster(
        http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": direction, "pas": 0.25}
    )
    assert statut == 202, corps
    await attendre(lambda: not serveur.occupe, delai=30.0)
    return serveur.direct.faites[-1]["rapport"]


def test_libelles_d_erreur_en_francais_avec_le_libelle_d_origine():
    assert diagnostic.libelle_erreur(44) == "44 : pare-chocs enfoncé (Bumper stuck)"
    assert diagnostic.libelle_erreur(23) == "23 : « Map missing »"  # non traduit : libellé d'origine entre guillemets
    assert diagnostic.libelle_erreur(9999) == "9999 : code inconnu"
    assert diagnostic.libelles_erreurs([44, 0, 19, "x", True]) == ["44 : pare-chocs enfoncé (Bumper stuck)", "19 : roues qui patinent (Robot wheel slip)"]
    assert diagnostic.libelles_erreurs(None) == []
    # Chaque traduction correspond bien au libellé d'origine du socle (garde contre un décalage de table).
    mots = {"roue": "wheel", "batterie": "batter", "soulèvement": "lift", "pare-chocs": "bumper", "ultrasons": "ultrasonic",
            "pente": ("incline", "slope"), "bloqué": "stuck", "obstacle": "obstacle", "vide": "cliff", "patin": "slip", "bordure": "edge", "entraînement": "drive"}
    for code, traduit in diagnostic.ERREURS_FR.items():
        origine = socle.ERROR_DESCRIPTIONS[code].lower()
        attendus = [m for cle, m in mots.items() if cle in traduit]
        assert attendus, (code, traduit)
        assert any(any(v in origine for v in ([m] if isinstance(m, str) else m)) for m in attendus), (code, traduit, origine)
    assert diagnostic.cause_d_immobilite(7) == "en erreur : ordre ignoré" and diagnostic.cause_d_immobilite(13) == "en erreur : ordre ignoré"
    assert diagnostic.cause_d_immobilite(6) == "patinage possible"


def test_patinage_repete_ne_desarme_jamais_le_pilotage_direct(tmp_path, monkeypatch, session_reelle, lien):
    # Décision du propriétaire (2026-10-12) : c'est précisément quand le robot patine qu'on a besoin d'insister.
    lien.robot = en_erreur()  # la pose ne change jamais : le robot ignore les ordres

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            for numero in range(1, 6):  # cinq pas de suite sans déplacement
                rapport = await pas_direct(http, serveur)
                assert rapport["constat"] == mouvement.IMMOBILE and rapport["cause_probable"] == "en erreur : ordre ignoré"
                image = serveur.instantane()
                assert image["direct"]["arme"] is True and image["ordres"]["immobiles"] == numero
            # Un pas qui réussit remet le compteur à zéro.
            avance = []
            lien.sur_publication = lambda lien_, octets: (avance.append(1), setattr(
                lien_, "robot", en_erreur(poseEastM=3.0 + 0.01 * len(avance))
            )) if est_consigne(octets) else None
            rapport = await pas_direct(http, serveur, "haut")
            assert rapport["verification"] == mouvement.VERIFIEE and serveur.instantane()["ordres"]["immobiles"] == 0
            # Un déplacement dans le mauvais sens est dit, sans retirer la main non plus.
            recule = []
            lien.sur_publication = lambda lien_, octets: (recule.append(1), setattr(
                lien_, "robot", en_erreur(poseEastM=3.10 - 0.02 * len(recule))
            )) if est_consigne(octets) else None
            rapport = await pas_direct(http, serveur, "haut")
            assert "mauvais sens" in rapport["constat"] and serveur.direct.arme is True
            journal = "".join(f.read_text(encoding="utf-8") for f in tmp_path.rglob("*.jsonl"))
            assert "direct-desarme" not in journal
            # Un statut non pilotable (SEC-01), lui, retire toujours la main.
            lien.sur_publication = None
            lien.robot = fragment(statut=2, tache=2)
            await pas_direct(http, serveur)
            assert serveur.direct.arme is False and "SEC-01" in serveur.resultat["motif"]

    executer(scenario())


def test_statut_en_erreur_diagnostic_actionnable_et_avertissement_avant_le_pas(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = en_erreur()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            ordres = serveur.instantane()["ordres"]
            assert ordres["erreur"] == {
                "resolue": False, "libelles": ["44 : pare-chocs enfoncé (Bumper stuck)"],
                "phrase": "En erreur, le robot ignore le pilotage manuel.", "annulation": None, "message": None,
            }
            assert ordres["avertissement_pas"] == "Robot en erreur : il ignorera probablement cet ordre."
            # Le pas reste possible (le propriétaire décide), et aucune pause n'est proposée : rien à mettre en pause.
            assert ordres["mouvement_refuse"] is None and ordres["action"] is None
            for corps in ({}, {"confirmer": "oui"}, {"confirmer": True, "autre": 1}):
                statut, _reponse, _recus = await poster(http, serveur, "/api/erreur/annuler", corps)
                assert statut == 400 and lien.ordres == []
            lien.robot = fragment(statut=1)
            await poster(http, serveur, "/api/actualiser")
            image = serveur.instantane()["ordres"]
            assert image["erreur"] is None and image["avertissement_pas"] is None
            statut, reponse, _recus = await poster(http, serveur, "/api/erreur/annuler", {"confirmer": True})
            assert statut == 409 and "n'est pas en erreur" in reponse["erreur"] and lien.ordres == []

    executer(scenario())


def test_annulation_puis_statut_pret_les_fleches_sont_de_nouveau_utilisables(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = en_erreur()
    # Le faux robot se remet en attente quand il reçoit l'ordre 28, comme l'application le constate.
    lien.sur_publication = lambda lien_, octets: setattr(lien_, "robot", fragment(statut=1, est=3.0, nord=2.0)) if octets == ANNULER else None

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            statut, _reponse, _recus = await poster(http, serveur, "/api/erreur/annuler", {"confirmer": True})
            assert statut == 202
            await attendre(lambda: not serveur.occupe, delai=20.0)
            assert lien.ordres == [ANNULER]  # le seul ordre « annuler la tâche », rien d'autre
            await attendre(lambda: (serveur.instantane()["ordres"]["erreur"] or {}).get("resolue") is True)
            erreur = serveur.instantane()["ordres"]["erreur"]
            assert erreur["annulation"] == "réussie" and erreur["message"] == (
                "Le robot est de nouveau en attente : les flèches sont de nouveau utilisables — reculez pour dégager le pare-chocs."
            )
            assert serveur.instantane()["ordres"]["avertissement_pas"] is None
            # La main n'a pas été perdue : la flèche suivante part, sans réarmer.
            assert serveur.direct.arme is True
            avance = []
            lien.sur_publication = lambda lien_, octets: (avance.append(1), setattr(
                lien_, "robot", fragment(statut=6, est=3.0 - 0.025 * len(avance), nord=2.0)
            )) if est_consigne(octets) else None
            rapport = await pas_direct(http, serveur)
            assert rapport["verification"] == mouvement.VERIFIEE and serveur.direct.arme is True
            assert serveur.instantane()["ordres"]["erreur"] is None  # le message de réussite ne reste pas

    executer(scenario())


def test_erreur_persistante_apres_annulation_dite_une_fois_sans_boucler(tmp_path, monkeypatch, session_reelle, lien):
    monkeypatch.setattr(ui, "DELAI_ANNULATION_S", 0.3)
    lien.robot = en_erreur()  # l'annulation n'y change rien

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/erreur/annuler", {"confirmer": True})
            assert serveur.instantane()["ordres"]["erreur"]["annulation"] == "en attente"
            await attendre(lambda: serveur.instantane()["ordres"]["erreur"]["annulation"] == "persistante", delai=20.0)
            erreur = serveur.instantane()["ordres"]["erreur"]
            assert erreur["message"] == "L'erreur persiste : intervention sur place nécessaire, ou essayez l'application Lymow."
            assert erreur["libelles"] == ["44 : pare-chocs enfoncé (Bumper stuck)"]
            await asyncio.sleep(1.0)
            assert lien.ordres == [ANNULER]  # rien n'est retenté de lui-même

    executer(scenario())


@sans_node
def test_page_prendre_la_main_en_erreur_n_emet_pas_de_pause_et_affiche_le_diagnostic():
    erreur = {"resolue": False, "libelles": ["44 : pare-chocs enfoncé (Bumper stuck)"], "phrase": diagnostic.PHRASE_ERREUR,
              "annulation": None, "message": None}
    base = {"suspendue": False, "connecte": True, "occupe": False, "flechesActives": False, "pauseDemandee": False, "confirme": True}
    # Même si une pause était proposée, elle n'est pas tentée sur un robot en erreur.
    ordres = {"statut": "en erreur", "action": "pause", "mouvement_refuse": None, "erreur": erreur}
    assert js("ordres.js", "M.suitePrendreLaMain(a[0])", base | {"ordres": ordres}) == "fleches"
    assert js("ordres.js", "M.suitePrendreLaMain(a[0])", base | {"ordres": ordres | {"erreur": None}}) == "pause"
    annonce = js("ordres.js", "M.annoncePrendreLaMain(a[0])", {"suspendue": False, "ordres": ordres, "composer": False, "simulation": False})
    assert "EN ERREUR" in annonce and "mise en pause" not in annonce
    assert js("ordres.js", "M.diagnosticErreur(a[0])", erreur) == {
        "titre": "Robot en erreur — 44 : pare-chocs enfoncé (Bumper stuck)", "phrase": "En erreur, le robot ignore le pilotage manuel.",
        "bouton": True, "suite": "",
    }
    attente = js("ordres.js", "M.diagnosticErreur(a[0])", erreur | {"annulation": "en attente", "message": "Annulation demandée…"})
    assert attente["bouton"] is False and attente["suite"] == "Annulation demandée…"
    persistante = js("ordres.js", "M.diagnosticErreur(a[0])", erreur | {"annulation": "persistante", "message": "L'erreur persiste"})
    assert persistante["bouton"] is False  # rien ne boucle : le bouton ne revient pas
    assert js("ordres.js", "M.diagnosticErreur(a[0])", {"resolue": True, "message": "de nouveau utilisables"})["suite"] == "de nouveau utilisables"
    assert js("ordres.js", "M.diagnosticErreur(null)") is None
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    assert ">Annuler la tâche et remettre le robot en attente<" in page and 'id="pave-avertissement"' in page
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    clic = app[app.index('$("erreur-annuler").addEventListener') :][:600]
    assert "deuxClics(" in clic and "window.confirm(" not in clic and "ABANDONNÉE" in clic and '"/api/erreur/annuler", { confirmer: true }' in clic


def test_route_prendre_la_main_en_erreur_aucune_pause_n_est_emise(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = en_erreur()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            # Ce que fait « Prendre la main » quand aucune pause n'est proposée : armer les flèches, rien d'autre.
            assert serveur.instantane()["ordres"]["action"] is None
            statut, _corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            assert statut == 200 and not any(p in lien.trames for p in PAUSES) and lien.ordres == []

    executer(scenario())


@sans_node
def test_page_ligne_de_retour_dit_la_cause_probable_quand_la_pose_n_a_pas_change():
    ordre = {"nom": "reculer", "valeur": 1}
    ligne = lambda **rapport: js("fleches.js", "M.ligneRetour(a[0], a[1])", ordre, rapport)  # noqa: E731
    assert "le robot n'a pas bougé — en erreur : ordre ignoré" in ligne(verification="anomalie", constat=mouvement.IMMOBILE, cause_probable="en erreur : ordre ignoré")
    assert "le robot n'a pas bougé — patinage possible" in ligne(verification="anomalie", constat=mouvement.IMMOBILE, cause_probable="patinage possible")
    assert "position transmise trop rarement : non vérifiable" in ligne(verification="non vérifiable", cause_probable=diagnostic.CAUSE_POSE_RARE)
    assert "position non fournie" in ligne(verification="non vérifiable")  # rapport ancien, sans cause
    assert "déplacement constaté" in ligne(verification="vérifiée", constat="déplacement constaté")
