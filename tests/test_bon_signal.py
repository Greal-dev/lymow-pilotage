"""Retour vers le bon signal : regroupement, cible la plus proche, trajet, découpage en pas (logique pure)."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime

import pytest
from conftest import est_consigne, fragment
from test_carte import contenu

from lymow_pilotage import bon_signal, mouvement, securite
from lymow_pilotage import carte as cartes
from lymow_pilotage.config import Calibration
from lymow_pilotage.demo import passage_brut, zone_brute
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.vendor.ha_lymow import protocol as pb

# Pelouse 10 × 6 m (zoneA1) avec un massif interdit de 4 à 6 m × 2 à 4 m ; verger 14 à 20 m ; passage à y = 3.
CARTE = cartes.decoder(contenu())


def point(est, nord, precision, date="2026-10-12T15:40:00"):
    return {"est_m": est, "nord_m": nord, "precision_m": precision, "horodatage": date}


def test_points_fiables_filtres_par_precision_et_par_date():
    releve = [point(1, 1, 0.05), point(2, 2, 0.10), point(3, 3, 0.11), point(4, 4, 1.32), point(5, 5, 0.03, "2026-08-01T10:00:00"),
              point(6, 6, 0.02, "pas une date")]
    assert bon_signal.points_fiables(releve) == [(1.0, 1.0), (2.0, 2.0), (5.0, 5.0), (6.0, 6.0)]
    recents = bon_signal.points_fiables(releve, jours=30, maintenant=datetime(2026, 10, 13))
    assert recents == [(1.0, 1.0), (2.0, 2.0)]  # relevés anciens, ou sans date lisible, écartés


def test_regroupement_simple_des_points_voisins():
    groupes = bon_signal.regrouper([(1.0, 1.0), (1.5, 1.2), (2.0, 1.0), (8.0, 5.0), (8.4, 5.0)])
    assert [(round(c[0], 2), round(c[1], 2), n) for c, n in groupes] == [(1.5, 1.07, 3), (8.2, 5.0, 2)]
    assert bon_signal.regrouper([]) == []


def test_cible_la_plus_proche_a_l_interieur_d_une_zone_avec_marge_et_hors_zone_interdite():
    groupes = [((5.0, 3.0), 9), ((0.2, 0.2), 4), ((30.0, 30.0), 6), ((8.0, 1.0), 2), ((17.0, 3.0), 5)]
    # (5, 3) est dans le massif interdit ; (0.2, 0.2) à moins de 0,5 m du contour ; (30, 30) hors de la carte.
    cible = bon_signal.cible_la_plus_proche((9.0, 5.0), groupes, CARTE)
    assert (cible["est_m"], cible["nord_m"], cible["points"]) == (8.0, 1.0, 2)
    assert cible["distance_m"] == pytest.approx(4.1, abs=0.05) and cible["cap_deg"] == pytest.approx(-104.0, abs=1)
    assert bon_signal.cible_la_plus_proche((19.0, 3.0), groupes, CARTE)["est_m"] == 17.0  # la plus proche, dans le verger
    assert bon_signal.cible_la_plus_proche((1.0, 1.0), [((5.0, 3.0), 9), ((30.0, 30.0), 1)], CARTE) is None
    assert bon_signal.libre(CARTE, (12.0, 3.0)) and not bon_signal.libre(CARTE, (12.0, 5.0))  # sur le passage ; hors de tout


def test_trajet_en_ligne_droite_quand_elle_tient_dans_la_zone():
    chemin, motif = bon_signal.trajet(CARTE, (1.0, 1.0), (8.0, 1.0))
    assert chemin == [(1.0, 1.0), (8.0, 1.0)] and motif is None


def test_trajet_contourne_la_zone_interdite():
    depart, cible = (1.0, 3.0), (9.0, 3.0)  # le massif (4 à 6 m) barre la ligne droite
    assert not bon_signal.segment_libre(CARTE, depart, cible)
    chemin, motif = bon_signal.trajet(CARTE, depart, cible)
    assert motif is None and chemin[0] == depart and chemin[-1] == cible and len(chemin) >= 3
    for a, b in zip(chemin, chemin[1:]):
        assert bon_signal.segment_libre(CARTE, a, b)  # dans la pelouse, hors du massif, de bout en bout
    longueur = sum(math.dist(a, b) for a, b in zip(chemin, chemin[1:]))
    assert 8.0 < longueur < 12.0  # un détour, pas un tour du jardin


def test_trajet_d_une_zone_a_l_autre_par_le_passage():
    chemin, motif = bon_signal.trajet(CARTE, (8.0, 1.0), (17.0, 5.0))
    assert motif is None and len(chemin) >= 3
    assert all(bon_signal.segment_libre(CARTE, a, b) for a, b in zip(chemin, chemin[1:]))
    suivis = [
        (a[0] + (b[0] - a[0]) * i / 50, a[1] + (b[1] - a[1]) * i / 50) for a, b in zip(chemin, chemin[1:]) for i in range(51)
    ]
    assert any(11.0 < x < 13.0 and abs(y - 3.0) < 0.6 for x, y in suivis)  # il emprunte le passage, seul lien entre les zones


def test_cible_inatteignable_ou_robot_hors_de_toute_zone_seul_le_cap_est_propose():
    ile = pb._field_bytes(1, zone_brute("zoneA1", "Pelouse", [(0, 0), (10, 0), (10, 6), (0, 6)]))
    ile += pb._field_bytes(1, zone_brute("zoneB1", "Île", [(20, 0), (26, 0), (26, 6), (20, 6)]))  # aucun passage entre elles
    ile += pb._field_bytes(3, passage_brut("passSt", "zoneA1", "", [(1.0, 1.0), (2.0, 2.0)], station=True))
    ile += pb._field_bytes(4, pb._field_f32(1, 1.0) + pb._field_f32(2, 1.0) + pb._field_f32(3, 0.0))
    ile += pb._field_bytes(7, pb._field_f32(1, 10.0) + pb._field_f32(2, 20.0))
    carte = cartes.decoder(ile)
    chemin, motif = bon_signal.trajet(carte, (5.0, 3.0), (23.0, 3.0))
    assert chemin is None and "aucun trajet trouvé" in motif and "seul le cap est proposé" in motif
    chemin, motif = bon_signal.trajet(CARTE, (12.0, 5.5), (8.0, 1.0))  # robot hors de toute zone
    assert chemin is None and "hors de toute zone" in motif
    chemin, motif = bon_signal.trajet(CARTE, (1.0, 1.0), (5.0, 3.0))  # cible dans la zone interdite
    assert chemin is None and "cible est hors" in motif


def test_decoupage_en_pas_compatibles_avec_sec02():
    cal = Calibration()
    ordres = bon_signal.decouper([(0.0, 0.0), (5.0, 0.0), (5.0, 3.0)], cap_deg=90.0)
    assert [(o["nom"], o["valeur"]) for o in ordres] == [
        ("tourner", -90.0), ("avancer", 1.67), ("avancer", 1.67), ("avancer", 1.67), ("tourner", 90.0), ("avancer", 1.5), ("avancer", 1.5),
    ]
    for ordre in ordres:
        mouvement.valider(Ordre(ordre["nom"], ordre["valeur"]), cal)  # SEC-02 : chaque pas passe
    assert all(o["vitesse"] is None and o["duree"] is None for o in ordres)
    # Demi-tour : découpé pour rester sous l'angle maximal demandable (142° avec le facteur 0,79).
    demi_tour = bon_signal.decouper([(0.0, 0.0), (-4.0, 0.0)], cap_deg=0.0, facteur_angle=0.79)
    rotations = [o["valeur"] for o in demi_tour if o["nom"] == "tourner"]
    assert len(rotations) == 2 and sum(abs(r) for r in rotations) == pytest.approx(180.0) and max(abs(r) for r in rotations) <= 142
    for ordre in demi_tour:
        mouvement.valider(Ordre(ordre["nom"], ordre["valeur"]), Calibration(facteur_angle=0.79))
    assert bon_signal.decouper([(0.0, 0.0), (1.0, 0.0)], cap_deg=None) is None  # cap inconnu : pas de pas
    assert bon_signal.decouper([(0.0, 0.0), (1.0, 0.0)], cap_deg=0.5) == [{"nom": "avancer", "valeur": 1.0, "vitesse": None, "duree": None}]  # sans « force » : jamais proposé d'office


def test_proposition_complete_et_ses_refus():
    releve = [point(8.0, 1.0, 0.05), point(8.2, 1.1, 0.04), point(5.0, 3.0, 0.03), point(2.0, 5.0, 1.2)]
    proposition = bon_signal.proposer(CARTE, releve, (1.0, 1.0), 0.0, 1.32)
    assert proposition["cible"]["est_m"] == 8.1 and proposition["points_fiables"] == 3 and proposition["motif"] is None
    assert proposition["trajet"] == [[1.0, 1.0], [8.1, 1.05]]
    assert [o["nom"] for o in proposition["ordres"]] == ["avancer"] * 4
    assert proposition["mention"] == "position incertaine : trajet indicatif, surveillez la caméra"
    assert bon_signal.proposer(CARTE, releve, (1.0, 1.0), 0.0, 0.3)["mention"] is None  # dégradée : pas « perdue »
    assert "déjà fiable" in bon_signal.proposer(CARTE, releve, (1.0, 1.0), 0.0, 0.05)["motif"]
    assert "position du robot inconnue" in bon_signal.proposer(CARTE, releve, None, None, 1.3)["motif"]
    assert "aucun point de bon signal dans le relevé" in bon_signal.proposer(CARTE, [point(1, 1, 0.9)], (1.0, 1.0), 0.0, 1.3)["motif"]
    sans_cap = bon_signal.proposer(CARTE, releve, (1.0, 1.0), None, 1.32)
    assert sans_cap["trajet"] is not None and sans_cap["ordres"] is None and "cap du robot inconnu" in sans_cap["motif"]
    hors = bon_signal.proposer(CARTE, releve, (12.0, 5.5), 0.0, 1.32)
    assert hors["cible"] is not None and hors["trajet"] is None and "hors de toute zone" in hors["motif"]


def test_sequence_vers_le_signal_s_arrete_d_elle_meme_quand_la_precision_redevient_fiable(session, lien):
    # Trames sans pose : le constat de chaque pas est « non vérifiable », la séquence continue (SEC-07).
    lien.robot = {"robotState": 3, "workStatus": 2, "rtkEastM": 1.32}
    compte = []

    def signal_retrouve(lien_, octets):
        if est_consigne(octets):
            compte.append(1)
            # Position perdue : la carte n'est pas crédible, l'allure reste normale (décision du 2026-10-12).
            if len(compte) == 80:  # dernière consigne de la deuxième étape (40 par mètre à 0,25 m/s)
                lien_.robot = {"robotState": 6, "workStatus": 2, "rtkEastM": 0.06}

    lien.sur_publication = signal_retrouve
    ordres = [Ordre("avancer", 1.0)] * 4
    bilan = asyncio.run(mouvement.executer_sequence(session, ordres, objectif_signal=True))
    assert len(bilan["etapes"]) == 2 and bilan["interrompue"] is False and bilan["objectif_atteint"] is True
    assert bilan["motif"] == "précision retrouvée (±0,06 m) : objectif atteint, séquence arrêtée — 2 étape(s) non exécutée(s)"
    assert bilan["non_executees"] == ["avancer 1", "avancer 1"] and "reprise" not in bilan
    # Sans cet objectif, la même séquence va au bout.
    lien.robot = {"robotState": 3, "workStatus": 2, "rtkEastM": 0.06}
    lien.sur_publication = None
    complet = asyncio.run(mouvement.executer_sequence(session, [Ordre("avancer", 0.5)] * 2))
    assert len(complet["etapes"]) == 2 and "objectif_atteint" not in complet
    assert securite.qualite_position(0.06) == "fiable"


# --- routes et page ---------------------------------------------------------------------------


def test_route_de_proposition_et_sequence_chargee_avec_son_objectif(tmp_path, monkeypatch, session_reelle, lien):
    import json

    from test_ui import attendre, executer, lire, poster, serveur_ui

    from lymow_pilotage import releve, ui

    # La demande de carte d'office (testée ailleurs) est neutralisée : ce test part d'un serveur sans carte,
    # et ne doit pas dépendre de qui arrive le premier (échec intermittent constaté le 2026-10-12).
    monkeypatch.setattr(ui.ServeurUI, "_demander_la_carte_d_office", lambda self: None)
    carnet = releve.Releve(tmp_path)
    carnet.noter(8.0, 1.0, 0.05, 20, 0.0)
    carnet.noter(8.0, 1.2, 0.04, 20, 100.0)
    lien.carte = contenu()
    lien.robot = fragment(statut=3, tache=2, est=1.0, nord=1.0, theta=0.0, precision=1.32)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            statut, texte, _recus = await lire(http, serveur, "/api/signal")
            assert statut == 200 and "aucune carte" in json.loads(texte)["motif"]
            await poster(http, serveur, "/api/carte/actualiser")
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.session.etat.pose is not None)
            statut, texte, _recus = await lire(http, serveur, "/api/signal")
            proposition = json.loads(texte)
            assert proposition["cible"]["distance_m"] == pytest.approx(7.0, abs=0.1) and len(proposition["ordres"]) == 4
            assert proposition["mention"] == bon_signal.MENTION_INCERTAINE and proposition["trajet"][0] == [1.0, 1.0]
            assert lien.ordres == []  # proposer n'émet rien

            # Mode assisté : la séquence chargée se prévisualise et s'émet comme toute séquence, avec son objectif.
            avance = []  # le robot fictif avance vraiment : sinon SEC-14 l'arrête pour « obstacle ou patinage »
            lien.sur_publication = lambda lien_, octets: (avance.append(1), setattr(
                lien_, "robot", fragment(statut=6, tache=2, est=3.0 + 0.012 * len(avance), nord=1.0, precision=0.06)
            )) if est_consigne(octets) else None
            statut, corps, _recus = await poster(http, serveur, "/api/previsualiser", {"ordres": proposition["ordres"], "objectif": "signal"})
            assert statut == 200 and len(corps["etapes"]) == 4
            await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            await attendre(lambda: not serveur.occupe, delai=20.0)
            resultat = serveur.resultat
            assert "objectif_atteint" in resultat, {c: v for c, v in resultat.items() if c in ("motif", "echec", "code_sortie")}
            assert resultat["objectif_atteint"] is True and resultat["code_sortie"] == 0 and len(resultat["etapes"]) == 1
            assert "précision retrouvée (±0,06 m)" in resultat["motif"]
            # Signal retrouvé, robot en pause : la reprise est proposée, la garde SEC-12 passe.
            lien.sur_publication = None
            lien.robot = fragment(statut=3, tache=2, est=3.0, nord=1.0, precision=0.06)
            await poster(http, serveur, "/api/actualiser")
            await attendre(lambda: serveur.instantane()["ordres"]["reprise_conseillee"] is True)
            assert serveur.instantane()["ordres"]["avertissement_position"] is None
            statut, texte, _recus = await lire(http, serveur, "/api/signal?jours=30")
            assert "déjà fiable" in json.loads(texte)["motif"]

    executer(scenario())
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    carte_js = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    assert 'id="signal-chercher"' in page and "Charger le trajet dans la séquence" in page and 'id="signal-recent"' in page
    assert 'page.chargerSequence(signal.ordres, "signal")' in carte_js and '"/api/signal"' in carte_js
    assert "/api/emettre" not in carte_js  # jamais d'exécution automatique : le trajet passe par la séquence
