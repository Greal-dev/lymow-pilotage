"""Qualité de localisation (SEC-12), reprise gardée, main rendue à l'application, relevé de précision, constat de calibration.

Constat réel du 2026-10-12 (15 h 32 – 15 h 39) : la pose suit les manœuvres, mais la précision
annoncée reste de 1,1 à 1,5 m ; la tonte reprise dans cet état « fait n'importe quoi ».
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from conftest import fragment, lignes_journal
from test_ui import attendre, executer, lire, poster, serveur_ui

from lymow_pilotage import calibrage, cli, mouvement, releve, securite, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.etat import Etat
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.securite import RefusSecurite

REPRENDRE = bytes.fromhex("10 31 28 04")
MESSAGE = (
    "Position imprécise (±1,32 m) : la tonte reprendrait au mauvais endroit. "
    "Conduisez le robot vers un endroit dégagé jusqu'à ±0,10 m, ou renvoyez-le à la base."
)


def lancer(coroutine):
    return asyncio.run(coroutine)


def en_pause(precision: float | None) -> dict:
    return fragment(statut=3, tache=2, precision=precision)


# --- B : indicateur à trois états, fondé sur la précision en mètres --------------------


@pytest.mark.parametrize(
    ("precision", "qualite", "texte"),
    [
        (0.03, "fiable", "position : ±0,03 m — fiable"), (0.10, "fiable", "position : ±0,10 m — fiable"),
        (0.11, "dégradée", "position : ±0,11 m — dégradée"), (0.50, "dégradée", "position : ±0,50 m — dégradée"),
        (0.51, "perdue", "position : ±0,51 m — perdue"), (1.32, "perdue", "position : ±1,32 m — perdue"),
        (None, "perdue", "position : précision inconnue — perdue"),
    ],
)
def test_localisation_jugee_sur_la_precision_en_metres(precision, qualite, texte):
    etat = Etat(en_ligne=True)
    etat.appliquer(fragment(precision=precision), 1.0)
    resume = etat.resume(1.0)
    assert (resume["localisation"], resume["localisation_texte"]) == (qualite, texte)
    assert resume["precision_m"] == precision and securite.qualite_position(precision) == qualite


def test_seuils_de_precision_en_constantes_et_valeurs_absurdes_perdues():
    assert (securite.PRECISION_FIABLE_M, securite.PRECISION_DEGRADEE_M) == (0.10, 0.50)
    for absurde in (float("nan"), float("inf"), -1.0):
        assert securite.qualite_position(absurde) == "perdue"
    etat = Etat()
    etat.appliquer({"robotState": 1, "workStatus": 1, "rtkEastM": float("nan")}, 1.0)
    assert etat.precision_m is None


# --- C : garde de reprise (SEC-12) ------------------------------------------------------


def test_sec12_reprendre_refuse_quand_la_position_est_imprecise_avec_un_message_actionnable(session, lien):
    lien.robot = en_pause(1.32)
    with pytest.raises(RefusSecurite) as refus:
        lancer(mouvement.executer(session, Ordre("reprendre")))
    assert str(refus.value) == MESSAGE + " (SEC-12)" and lien.ordres == []
    for precision in (0.3, None):  # dégradée, ou inconnue : même garde
        lien.robot = en_pause(precision)
        lancer(session.solliciter())
        with pytest.raises(RefusSecurite, match=r"\(SEC-12\)$"):
            lancer(mouvement.executer(session, Ordre("reprendre")))
    assert lien.ordres == []


def test_sec12_reprendre_passe_quand_la_position_est_fiable(session, lien):
    lien.robot = en_pause(0.08)
    rapport = lancer(mouvement.executer(session, Ordre("reprendre")))
    assert rapport["codes_emis"] == [4] and lien.ordres == [REPRENDRE] and "avertissement" not in rapport


def test_sec12_reprendre_quand_meme_possible_et_journalise(session, lien, tmp_path):
    lien.robot = en_pause(1.32)
    rapport = lancer(mouvement.executer(session, Ordre(mouvement.REPRISE_FORCEE)))
    assert rapport["codes_emis"] == [4] and lien.ordres == [REPRENDRE]
    assert rapport["avertissement"] == "position imprécise (±1,32 m) : le trajet automatique peut être erratique"
    force = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "reprise-forcee")
    assert force["precision_m"] == 1.32 and force["localisation"] == "perdue"
    lien.publies.clear()
    lien.robot = fragment(statut=2, precision=1.32)  # rien à reprendre : la garde levée ne lève rien d'autre
    lancer(session.solliciter())
    with pytest.raises(RefusSecurite, match="rien à reprendre"):
        lancer(mouvement.executer(session, Ordre(mouvement.REPRISE_FORCEE)))


def test_sec12_retour_a_la_base_averti_sans_blocage(session, lien):
    lien.robot = en_pause(1.32)
    rapport = lancer(mouvement.executer(session, Ordre("base")))
    assert rapport["codes_emis"] == [33] and "position imprécise (±1,32 m)" in rapport["avertissement"]
    lien.robot = en_pause(0.05)
    lancer(session.solliciter())
    assert "avertissement" not in lancer(mouvement.executer(session, Ordre("base")))


def test_sec12_ligne_de_commande_option_explicite(capsys, tmp_path):
    analyseur = cli.construire_analyseur()
    assert cli._ordres(analyseur.parse_args(["reprendre"])) == [Ordre("reprendre")]
    assert cli._ordres(analyseur.parse_args(["reprendre", "--position-imprecise-acceptee"])) == [Ordre(mouvement.REPRISE_FORCEE)]
    assert cli.main(["reprendre", "--json", "--donnees", str(tmp_path)]) == 0  # simulation
    assert "SEC-12" in json.loads(capsys.readouterr().out)["garde"]


def test_sec12_la_page_le_dit_avant_et_propose_reprendre_quand_meme(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = en_pause(1.32)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.instantane()["ordres"]["reprise_possible"])
            image = serveur.instantane()
            assert image["ordres"]["avertissement_position"] == MESSAGE and image["ordres"]["mouvement_refuse"] is None
            assert image["etat"]["localisation_texte"] == "position : ±1,32 m — perdue"
            # « Reprendre » : refusé, avec le message ; « Reprendre quand même » : émis.
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "reprendre"}]})
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            await attendre(lambda: not serveur.occupe)
            assert MESSAGE in serveur.resultat["motif"] and lien.ordres == []
            _statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": [{"nom": "reprendre-quand-meme"}]})
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            await attendre(lambda: not serveur.occupe)
            assert lien.ordres == [REPRENDRE] and serveur.resultat["code_sortie"] == 0
            lien.robot = en_pause(0.04)
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.instantane()["ordres"]["avertissement_position"] is None)

    executer(scenario())
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    assert 'id="reprendre-quand-meme" type="button" data-tache="reprendre-quand-meme" hidden>Reprendre quand même' in page
    assert 'id="reprise-avertissement"' in page and 'id="localisation"' in page


# --- A : rendre la main à l'application -------------------------------------------------


def test_rendre_la_main_ferme_la_liaison_suspend_la_reconnexion_et_n_emet_rien(tmp_path, monkeypatch, session_reelle, lien):
    monkeypatch.setattr(ui, "PERIODE_VEILLE_S", 0.05)
    lien.robot = fragment(statut=2)  # attestable : de quoi poser une attestation à retirer

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.instantane()["attestation"]["proposable"])
            await poster(http, serveur, ui.ROUTE_ATTESTATION_POSER, {"confirmer": True})
            lien.robot = fragment(statut=3, tache=2)
            await poster(http, serveur, "/api/actualiser")
            await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            assert serveur.attestation.active and serveur.direct.arme
            emis = list(lien.trames)

            # Pendant un mouvement : refusé.
            serveur.tache = asyncio.ensure_future(asyncio.sleep(30))
            statut, corps, _recus = await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            assert statut == 409 and "mouvement est en cours" in corps["erreur"] and serveur.session is not None
            serveur.tache.cancel()
            await asyncio.gather(serveur.tache, return_exceptions=True)

            statut, corps, _recus = await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            assert statut == 200 and corps["suspendue"] is True and corps["message"] == ui.LIAISON_SUSPENDUE
            image = serveur.instantane()
            assert image["liaison_suspendue"] is True and image["liaison"] == "Liaison suspendue : l'application Lymow a la main"
            assert serveur.session is None and not serveur.attestation.active and not serveur.direct.arme
            assert lien.trames == emis  # RIEN n'a été émis vers le robot
            ouvertures = len(serveur.ouvertures)
            await asyncio.sleep(0.4)  # la veille ne rouvre pas la liaison
            assert len(serveur.ouvertures) == ouvertures and serveur.session is None
            statut, corps, _recus = await poster(http, serveur, "/api/actualiser")
            assert statut == 409 and "Reprendre la liaison" in corps["erreur"] and len(serveur.ouvertures) == ouvertures
            statut, _texte, _recus = await lire(http, serveur, "/api/etat")
            assert statut == 200  # la page, elle, reste ouverte

            statut, corps, _recus = await poster(http, serveur, "/api/liaison/reprendre")
            assert statut == 200 and corps["suspendue"] is False and serveur.session is not None
            assert len(serveur.ouvertures) == ouvertures + 1 and serveur.instantane()["liaison_suspendue"] is False
            assert not serveur.attestation.active  # elle ne revient pas avec la liaison

    executer(scenario())
    evenements = [ligne["evenement"] for ligne in lignes_journal(tmp_path)]
    assert "liaison-suspendue" in evenements and "liaison-reprise" in evenements


def test_rendre_la_main_en_telecommande_propose_d_attendre_et_n_emet_rien(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = fragment(statut=6, tache=2)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.session.etat.statut == 6)
            emis = list(lien.trames)
            statut, corps, _recus = await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})
            assert statut == 200 and corps["attendre"] is True and corps["suspendue"] is False
            assert "télécommande" in corps["message"] and serveur.session is not None and not serveur.suspendue
            statut, corps, _recus = await poster(http, serveur, "/api/liaison/rendre", {"sans_attendre": True, "choix": "pause"})
            assert corps["suspendue"] is True and serveur.session is None
            assert lien.trames == emis and lien.ordres == []  # ni sortie de télécommande, ni rien d'autre

    executer(scenario())


# --- D : précision après chaque pas, relevé par position ---------------------------------


def test_chaque_pas_rapporte_la_precision_avant_et_apres(session, lien):
    lien.robot = fragment(statut=3, tache=2, precision=1.32)

    def apres_l_arret(lien_, octets):
        if octets == bytes.fromhex("10 31 52 0a 0d 00 00 00 00 15 00 00 00 00"):
            lien_.robot = fragment(statut=6, tache=2, est=1.0, precision=0.84)

    lien.sur_publication = apres_l_arret
    rapport = lancer(mouvement.executer_mouvement(session, Ordre("avancer", 1.0)))
    assert (rapport["precision_avant_m"], rapport["precision_m"]) == (1.32, 0.84)


def test_releve_un_point_au_plus_tous_les_deux_metres_ou_trente_secondes(tmp_path):
    carnet = releve.Releve(tmp_path)
    ecrits = [
        carnet.noter(0.0, 0.0, 1.32, 10, 0.0),  # premier point
        carnet.noter(0.5, 0.0, 1.30, 10, 1.0),  # trop près, trop tôt
        carnet.noter(1.9, 0.0, 1.10, 10, 5.0),
        carnet.noter(2.0, 0.0, 0.84, 10, 6.0),  # 2 m
        carnet.noter(2.1, 0.0, 0.40, 10, 20.0),
        carnet.noter(2.2, 0.0, 0.09, 12, 36.0),  # 30 s
        carnet.noter(float("nan"), 0.0, 0.09, 12, 99.0),
    ]
    assert ecrits == [True, False, False, True, False, True, False]
    points = releve.lire(tmp_path)
    assert [(p["est_m"], p["precision_m"], p["satellites"]) for p in points] == [(0.0, 1.32, 10), (2.0, 0.84, 10), (2.2, 0.09, 12)]
    assert all(p["horodatage"][:2] == "20" and "nord_m" in p for p in points)
    (tmp_path / releve.FICHIER).write_text("pas du json\n" + json.dumps({"est_m": 1, "nord_m": 2, "precision_m": 0.3}) + "\n", "utf-8")
    assert len(releve.lire(tmp_path)) == 1 and releve.lire(tmp_path / "absent") == []
    impossible = tmp_path / "fichier"
    impossible.write_text("x", encoding="utf-8")
    assert releve.Releve(impossible).noter(0.0, 0.0, 1.0, 10, 0.0) is False  # jamais bloquant


def test_page_releve_la_precision_par_position_et_la_sert_a_la_carte(tmp_path, monkeypatch, session_reelle, lien):
    lien.robot = None  # seules les trames du test arrivent

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            for est, precision in ((0.0, 1.32), (0.3, 1.30), (3.0, 0.30), (6.0, 0.05)):
                session_reelle.recevoir_etat(fragment(statut=6, tache=2, est=est, precision=precision))
                await asyncio.sleep(0.03)  # des trames distinctes pour l'horloge
            session_reelle.arret_en_cours = True  # SEC-03 : rien n'est écrit pendant un arrêt
            session_reelle.recevoir_etat(fragment(statut=6, tache=2, est=20.0, precision=0.05))
            session_reelle.arret_en_cours = False
            session_reelle.fin_arret()
            await asyncio.sleep(0.03)
            session_reelle.recevoir_etat({"robotState": 3, "workStatus": 2, "rtkEastM": 0.02})  # sans pose : pas de point
            statut, texte, _recus = await lire(http, serveur, "/api/releve")
            assert statut == 200
            return json.loads(texte)["points"]

    points = executer(scenario())
    assert points == [[0.0, 0.0, 1.32, "perdue"], [3.0, 0.0, 0.3, "dégradée"], [6.0, 0.0, 0.05, "fiable"]]
    assert (tmp_path / releve.FICHIER).is_file()
    ignore = (ui.STATIQUE.parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert releve.FICHIER in ignore
    carte = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    assert '"/api/releve"' in carte and '$("carte-precision").checked' in carte
    assert 'id="carte-precision"' in (ui.STATIQUE / "index.html").read_text(encoding="utf-8")


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
def test_page_retour_de_pas_avec_la_precision_et_sa_tendance():
    avancer = {"nom": "avancer", "valeur": 1}
    mieux = {"verification": "vérifiée", "precision_avant_m": 1.32, "precision_m": 0.84}
    assert js("fleches.js", "M.ligneRetour(a[0], a[1])", avancer, mieux) == (
        "↑ 1 m : émise, arrêt acquitté — déplacement constaté — position ±0,84 m ↘ en amélioration"
    )
    assert js("fleches.js", "M.tendance(0.84, 1.2)") == "↗ en dégradation"
    assert js("fleches.js", "M.tendance(1.32, 1.30)") == "→ stable"
    assert js("fleches.js", "M.tendance(null, 1.3)") == ""
    sans = {"verification": "non vérifiable", "precision_m": None}
    assert js("fleches.js", "M.ligneRetour(a[0], a[1])", avancer, sans) == "↑ 1 m : émise, arrêt acquitté — position non fournie"


@sans_node
def test_page_reprise_avertie_et_libelle_de_la_liaison():
    ordres = {"action": None, "reprise_possible": True, "avertissement_position": MESSAGE, "pause": None}
    gestes = js("ordres.js", "M.gestesProposes(a[0], false)", ordres)
    assert gestes["reprendre"] is True and gestes["reprendreQuandMeme"] is True and gestes["avertissementPosition"] == MESSAGE
    fiable = js("ordres.js", "M.gestesProposes(a[0], false)", ordres | {"avertissement_position": None})
    assert fiable["reprendreQuandMeme"] is False and fiable["avertissementPosition"] == ""
    assert js("ordres.js", "M.libelleLiaison(false)") == "Rendre la main à l'application"
    assert js("ordres.js", "M.libelleLiaison(true)") == "Reprendre la liaison"
    etat = {"en_ligne": True, "statut_libelle": "tonte en pause", "batterie_pct": 80, "localisation_texte": "position : ±1,32 m — perdue",
            "rtk": "SINGLE_POINT (libellé non confirmé)"}
    resume = js("ordres.js", "M.resumeEtat(a[0])", etat)
    assert resume.endswith("position : ±1,32 m — perdue") and "non confirmé" not in resume


# --- E : constat de calibration, à partir du journal local -------------------------------


def journal_d_essai(dossier, constats):
    (dossier / "journaux").mkdir(exist_ok=True)
    lignes = [json.dumps({"horodatage": "2026-10-12T15:32:00.000", "evenement": "ordre-constate", "mode": "réel"} | c) for c in constats]
    (dossier / "journaux" / "2026-10-12.jsonl").write_text("\n".join(lignes + ["ligne abîmée"]) + "\n", encoding="utf-8")


def test_constat_de_calibration_rapport_mesure_sur_commande_par_vitesse(tmp_path, capsys):
    verifie = {"verification": "vérifiée"}
    journal_d_essai(tmp_path, [
        {"ordre": "tourner 45", "rotation_deg": 36.0} | verifie, {"ordre": "tourner -45", "rotation_deg": -34.0} | verifie,
        {"ordre": "tourner 45", "rotation_deg": 35.3} | verifie, {"ordre": "tourner 45", "rotation_deg": 36.3} | verifie,
        {"ordre": "tourner 90 --vitesse 0.2", "rotation_deg": 81.0} | verifie,
        {"ordre": "avancer 1", "avance_m": 0.97} | verifie, {"ordre": "reculer 1", "avance_m": -0.95} | verifie,
        {"ordre": "avancer 2", "avance_m": 1.98} | verifie,
        {"ordre": "avancer 1", "avance_m": 0.2, "verification": "anomalie"},  # non vérifié : écarté
        {"ordre": "avancer 1", "avance_m": -0.9} | verifie,  # mauvais sens : une anomalie, pas une échelle
        {"ordre": "avancer --duree 2 --vitesse 0.1", "avance_m": 0.2} | verifie,  # à la durée : rien à comparer
        {"ordre": "avancer 1", "avance_m": 0.5, "mode": "simulation"} | verifie,
        {"ordre": "pause"} | verifie, {"ordre": "inconnu 3"} | verifie,
    ])
    resultat = calibrage.constat(tmp_path, Calibration())
    lent, angle, distance = resultat["constats"]  # triés par grandeur, puis par vitesse
    assert (angle["grandeur"], angle["vitesse"], angle["mesures"]) == ("angle", "0.35 rad/s", 4)
    assert angle["rapport_mesure_sur_commande"] == pytest.approx(0.787, abs=0.001) and angle["facteur_angle_suggere"] == 0.787
    assert angle["etendue"] == [0.756, 0.807] and angle["dispersion"] == pytest.approx(0.022, abs=0.002)
    assert (lent["vitesse"], lent["mesures"]) == ("0.2 rad/s", 1) and "facteur_angle_suggere" not in lent and "aucune" in lent["suggestion"]
    assert (distance["grandeur"], distance["mesures"]) == ("distance", 3) and distance["facteur_distance_suggere"] == 0.97
    assert "calibration.toml n'est pas modifié" in resultat["note"]
    # Le facteur est le rapport réel / consigne : un rapport constaté AVEC un facteur le corrige par produit.
    deja = calibrage.constat(tmp_path, Calibration(facteur_angle=0.9))
    assert deja["constats"][1]["facteur_angle_suggere"] == pytest.approx(0.9 * 0.787, abs=0.002)

    assert cli.main(["calibration-constat", "--json", "--donnees", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["constats"][1]["facteur_angle_suggere"] == 0.787
    vide = tmp_path / "vide"
    vide.mkdir()
    assert cli.main(["calibration-constat", "--json", "--donnees", str(vide)]) == 1
    assert "aucun constat" in json.loads(capsys.readouterr().out)["note"]


# --- calibration de rotation appliquée (décision du propriétaire, 2026-10-12) -----------------


def test_calibration_du_depot_facteur_d_angle_applique_et_distance_inchangee():
    from lymow_pilotage import config

    cal = config.charger_calibration(ui.STATIQUE.parents[2])
    assert (cal.facteur_angle, cal.facteur_distance) == (0.79, 1.0)
    texte = (ui.STATIQUE.parents[2] / "calibration.toml").read_text(encoding="utf-8")
    assert "2026-10-12" in texte and "calibration-constat" in texte and "NON appliqué" in texte


def test_tourner_45_commande_plus_longtemps_et_le_plafond_sec02_tient():
    sans, avec = securite.consigne_rotation(45.0, 1.0), securite.consigne_rotation(45.0, 0.79)
    assert avec.duree == pytest.approx(sans.duree / 0.79) and avec.duree > sans.duree  # 2,84 s au lieu de 2,24 s
    import math

    assert math.degrees(abs(avec.angulaire) * avec.duree) == pytest.approx(45 / 0.79)  # ≈ 57° commandés pour 45° réels
    for degres in (15.0, 45.0, 90.0, 142.0, -142.0):  # les pas des flèches, et la borne
        consigne = securite.consigne_rotation(degres, 0.79)
        assert math.degrees(abs(consigne.angulaire) * consigne.duree) <= 180.0 + 1e-6  # SEC-02 : jamais plus de 180° commandés
    for degres in (180.0, 143.0, -150.0):
        with pytest.raises(RefusSecurite) as refus:
            securite.consigne_rotation(degres, 0.79)
        assert "avec le facteur 0.79, le maximum demandable par ordre est 142°" in str(refus.value)
        assert str(refus.value).endswith("(SEC-02)")
    with pytest.raises(RefusSecurite, match="le maximum demandable par ordre est 1.6 m"):
        securite.consigne_translation(2.0, facteur_distance=0.8)
    securite.consigne_rotation(180.0, 1.0)  # sans facteur, 180° reste admis
