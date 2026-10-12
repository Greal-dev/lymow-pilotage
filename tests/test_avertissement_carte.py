"""SEC-13 devenu avertissement par défaut (décision du propriétaire, 2026-10-12).

Essai réel : « avancer 2 » refusé cinq fois (« bord de zone devant à 0,35 m »), pilotage direct désarmé à
chaque refus, alors que le propriétaire voyait le robot ailleurs que là où la carte le plaçait.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from conftest import fragment
from test_carte import contenu
from test_ui import attendre, executer, poster, serveur_ui

from lymow_pilotage import carte as cartes
from lymow_pilotage import config, garde_carte, mouvement, ui
from lymow_pilotage.mouvement import Ordre

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


def au_bord(precision: float = 0.02, **plus) -> dict:
    """Le cas réel : la carte place le robot à 0,35 m du bord, cap vers le bord."""
    return fragment(statut=3, tache=2, est=9.62, nord=5.0, precision=precision) | plus


def test_sec13_par_defaut_le_pas_part_en_entier_la_carte_avertit_seulement(session, lien):
    assert session.arret_aux_bords is False  # défaut : avertissement
    session.carte_reference = CARTE
    for precision in (0.02, 0.41, 4.68):  # fiable ou non : jamais raccourci, jamais refusé
        lien.robot = au_bord(precision)
        bilan = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 2.0)]))
        etape = bilan["etapes"][0]
        assert "echec" not in etape and etape["ordre"].startswith("avancer 2") and bilan["interrompue"] is False
        assert etape["garde_carte"] == "d'après la carte : bord de zone devant à 0,35 m" and etape["garde_carte_alerte"] is True
        assert etape["garde_carte_apres"].startswith("d'après la carte : bord de zone devant à ")  # redit après le pas
    loin = asyncio.run(mouvement.executer_sequence(session, [Ordre("reculer", 0.5)]))["etapes"][0]
    assert loin["garde_carte"] == "d'après la carte : bord de zone derrière à 9,6 m" and loin["garde_carte_alerte"] is False
    assert garde_carte.constat(garde_carte.juger_pas(None, (1.0, 1.0), 0.0, 1.0, 0.02), 1.0) == (
        "distance au bord inconnue : aucune carte de référence", False,
    )


def test_sec13_case_cochee_retablit_raccourcir_ou_refuser(session, lien):
    session.carte_reference = CARTE
    session.arret_aux_bords = True
    lien.robot = au_bord()
    bilan = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 2.0)]))
    assert "pas refusé : bord de zone devant à 0,35 m" in bilan["etapes"][0]["echec"] and lien.consignes == []
    assert bilan["etapes"][0]["refus_avant_emission"] is True


def test_allure_lente_pres_d_un_bord_seulement_si_la_position_est_fiable(session, lien):
    session.carte_reference = CARTE
    lien.robot = fragment(statut=3, tache=2, est=8.72, nord=5.0, precision=0.02)  # bord à 1,25 m, position fiable
    fiable = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5)]))["etapes"][0]
    assert fiable["ordre"] == "avancer 0.5 --vitesse 0.1" and "moins de 1 m d'un bord de zone" in fiable["allure"]
    for precision in (0.41, 1.68, 4.68):  # dégradée ou perdue : la carte n'est pas crédible, allure normale
        lien.robot = fragment(statut=3, tache=2, est=8.72, nord=5.0, precision=precision)
        etape = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5)]))["etapes"][0]
        assert etape["ordre"] == "avancer 0.5" and "allure" not in etape and etape["emissions"] == 20


def test_previsualisation_annonce_l_avertissement_sans_rien_raccourcir():
    etapes = [("avancer", 2.0), ("tourner", 90.0), ("avancer", 0.5)]
    assert garde_carte.annoncer(CARTE, (9.62, 5.0), 0.0, 0.02, 1.0, etapes) == [
        "étape 1 : d'après la carte : bord de zone devant à 0,35 m (pas demandé : 2 m) — le pas partira en entier",
        "distance au bord inconnue : le robot est hors des zones de la carte",  # la suite part de la position prévue
    ]
    assert garde_carte.annoncer(CARTE, (7.02, 5.0), 0.0, 0.02, 1.0, [("avancer", 1.0)]) == []
    assert garde_carte.annoncer(CARTE, (9.62, 5.0), 0.0, 0.02, 1.0, etapes, True)[0].startswith("étape 1 : pas refusé")


def test_aucun_refus_de_garde_carte_ne_desarme_et_la_case_est_memorisee(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = au_bord()
    lien.carte = contenu()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await attendre(lambda: serveur.instantane()["carte_connue"])
            await poster(http, serveur, "/api/actualiser")
            image = serveur.instantane()
            assert image["arret_aux_bords"] is False
            assert image["ordres"]["bord"]["texte"] == "d'après la carte : bord de zone devant à 0,35 m ; derrière à 9,6 m"
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "avancer", "valeur": 2}]})
            assert corps["garde_carte"] == [
                "étape 1 : d'après la carte : bord de zone devant à 0,35 m (pas demandé : 2 m) — le pas partira en entier"
            ] and corps["identifiant"]

            # Case cochée : le refus revient — mais il ne désarme plus le pilotage direct (cinq fois de suite, ici).
            for corps_faux in ({}, {"arreter": "oui"}, {"arreter": True, "x": 1}):
                statut, _reponse, _recus = await poster(http, serveur, "/api/garde-carte", corps_faux)
                assert statut == 400
            statut, reponse, _recus = await poster(http, serveur, "/api/garde-carte", {"arreter": True})
            assert statut == 200 and reponse == {"arreter": True, "memorisee": True}
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            for _essai in range(5):
                statut, _reponse, _recus = await poster(
                    http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "haut", "pas": 2}
                )
                assert statut == 202
                await attendre(lambda: not serveur.occupe, delai=20.0)
                rapport = serveur.direct.faites[-1]["rapport"]
                assert "SEC-13" in rapport["echec"] and serveur.direct.arme is True
            assert lien.consignes == []
            journal = "".join(f.read_text(encoding="utf-8") for f in tmp_path.rglob("*.jsonl"))
            assert "direct-desarme" not in journal

            # Un refus de statut (SEC-01), lui, désarme toujours.
            lien.robot = fragment(statut=2, tache=2, est=5.0, nord=5.0)  # tonte en cours
            await poster(http, serveur, "/api/direct/fleche", {"jeton": serveur.direct._jeton, "direction": "haut", "pas": 0.25})
            await attendre(lambda: not serveur.occupe, delai=20.0)
            assert "SEC-01" in serveur.direct.faites[-1]["rapport"]["echec"] and serveur.direct.arme is False

        # La case est mémorisée sur ce poste : un nouveau serveur la retrouve cochée.
        assert config.arret_aux_bords(tmp_path) is True
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            assert serveur.instantane()["arret_aux_bords"] is True
            await poster(http, serveur, "/api/garde-carte", {"arreter": False})
        assert config.arret_aux_bords(tmp_path) is False

    executer(scenario())
    (tmp_path / config.FICHIER_PREFERENCES).write_text("pas du json", encoding="utf-8")
    assert config.arret_aux_bords(tmp_path) is False and config.arret_aux_bords(None) is False  # illisible : défaut


@sans_node
def test_page_bord_en_orange_quand_il_est_avant_la_fin_du_pas_et_forcer_masque_par_defaut():
    alerte = lambda devant, derriere, pas: js("ordres.js", "M.alerteBord(a[0], a[1])", {"devant_m": devant, "derriere_m": derriere}, pas)  # noqa: E731
    assert alerte(0.35, 9.6, 2) is True and alerte(0.35, 9.6, 0.25) is False and alerte(5, 0.4, 0.5) is True
    assert alerte(None, None, 2) is False and js("ordres.js", "M.alerteBord(null, 2)") is False
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    assert '<input id="garde-carte" type="checkbox"> Arrêter aux bords de zone (d\'après la carte)</label>' in page  # décochée
    assert '<label id="pave-forcer-libelle" hidden' in page  # « Forcer ce pas » : seulement case cochée
    pave = (ui.STATIQUE / "pave.js").read_text(encoding="utf-8")
    assert '$("pave-forcer-libelle").hidden = !image.arret_aux_bords;' in pave and 'classList.toggle("alerte-carte"' in pave


# --- « rien ne s'ajuste sur la carte » : la partie testable ---------------------------------------------


@sans_node
def test_page_le_marqueur_du_robot_suit_chaque_nouvelle_pose():
    position = lambda est, nord, age=0.6: js(  # noqa: E731
        "ordres.js", "M.positionAffichee(a[0], a[1])",
        {"position_est_m": est, "position_nord_m": nord, "cap_deg": 30.0, "precision_m": 0.02, "age_position_s": age},
        {"est_m": 0.0, "nord_m": 0.0},
    )
    premiere, seconde = position(3.0, 2.0), position(4.5, 2.5)
    assert (premiere["est"], premiere["nord"], premiere["source"]) == (3.0, 2.0, "pose")
    assert (seconde["est"], seconde["nord"]) == (4.5, 2.5)  # nouvelle pose : marqueur déplacé, même repère que la carte
    assert premiere["note"] == "" and "position reçue il y a 42 s" in position(4.5, 2.5, 42.0)["note"]  # ancienne : dit
    assert "il y a 5 min" in position(4.5, 2.5, 300.0)["note"]
    carte_js = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    robot = carte_js[carte_js.index("    robot: function (etat, bord) {") :][:900]
    # Chaque état reçu replace le robot, recentre la vue si « Suivre le robot » est coché, puis redessine.
    assert robot.index("placerLeRobot();") < robot.index('$("carte-suivre").checked') < robot.index("dessiner();")
    assert "vue.x = robot.est - vue.l / 2;" in robot and "vue.y = -robot.nord - vue.h / 2;" in robot
    assert "window.LymowCarte.robot(image.etat" in (ui.STATIQUE / "app.js").read_text(encoding="utf-8")


def test_diagnostic_de_localisation_dit_quand_le_robot_ne_repond_pas(tmp_path, monkeypatch, session_reelle, lien):
    from lymow_pilotage import protocole
    from lymow_pilotage.vendor.ha_lymow import protocol as pb
    from lymow_pilotage.vendor.ha_lymow.protocol import decode_pboutput

    monkeypatch.setattr(protocole, "PERIODE_DIAGNOSTIC_RTK_S", 0.05)
    monkeypatch.setattr(ui, "ATTENTE_DIAGNOSTIC_S", 0.4)
    lien.robot = fragment(statut=3, tache=2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            assert serveur.instantane()["diagnostic_texte"] == ""
            await poster(http, serveur, "/api/diagnostic-rtk", {"actif": True})
            assert serveur.instantane()["diagnostic_texte"] == "demande envoyée : réponse attendue…"
            await attendre(lambda: serveur.instantane()["diagnostic_texte"] == "le robot ne répond pas à la demande de diagnostic")
            # Une réponse arrive (trame construite selon le schéma du socle) : elle est affichée, écart compris.
            serveur.session.recevoir_etat(decode_pboutput(pb._field_bytes(36, pb._field_f32(1, 2.0))))
            texte = serveur.instantane()["diagnostic_texte"]
            assert texte.startswith("corrections de la base reçues il y a 2 s") and "non mesurable (position brute non transmise)" in texte

    executer(scenario())
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    gauche = page[page.index('class="colonne-commandes"') : page.index('id="fleches-bascule"')]
    assert 'id="diagnostic-rtk"' in gauche and 'id="diagnostic-texte"' in gauche  # dans le bloc état, bien visible
