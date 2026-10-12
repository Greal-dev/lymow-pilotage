"""Carte et retouche de contour : décodage, géométrie, SEC-08 à SEC-11, commande et page.

Le socle ne fournit aucune réponse de carte capturée (ses tests la construisent) :
la carte d'essai est donc écrite ici champ par champ. Sa seule charge réelle,
une réponse de trajet, sert à vérifier qu'elle n'est pas prise pour une carte.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import math
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEMANDE_CARTE, ENTETE_ECRITURE_CARTE, fragment, lignes_journal

from lymow_pilotage import carte as cartes
from lymow_pilotage import cli, geometrie, liaison, protocole, retouche
from lymow_pilotage.config import Calibration
from lymow_pilotage.mouvement import ErreurSaisie
from lymow_pilotage.protocole import Categorie, est_requete_pure
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.session import EchecOrdre
from lymow_pilotage.vendor.ha_lymow import mqtt as socle_mqtt
from lymow_pilotage.vendor.ha_lymow import protocol as pb

PELOUSE = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 6.0), (5.0, 6.0), (0.0, 6.0)]  # sens trigonométrique
MASSIF = [(4.0, 2.0), (6.0, 2.0), (6.0, 4.0), (4.0, 4.0)]
VERGER = [(14.0, 0.0), (20.0, 0.0), (20.0, 6.0), (14.0, 6.0)]


def _polygone(points) -> bytes:
    return b"".join(pb._field_bytes(1, pb._field_f32(1, x) + pb._field_f32(2, y)) for x, y in points)


def _zone(identifiant, nom, points, parent=None, reglages=b"\x08\x28\x48\x0a") -> bytes:
    infos = pb._field_i32(1, 1) + pb._field_str(2, nom) + pb._field_str(3, identifiant) + pb._field_i32(4, 1)
    infos += pb._field_bytes(5, _polygone(points))
    surface = abs(geometrie.aire_signee(list(points)))
    brut = pb._field_bytes(1, infos) + pb._field_bytes(2, reglages) + pb._field_bytes(3, pb._field_f32(3, surface))
    return brut + (pb._field_str(4, parent) if parent else b"")


def _passage(identifiant, zone1, zone2, points, station=False) -> bytes:
    brut = pb._field_str(1, identifiant) + pb._field_str(2, zone1) + pb._field_str(3, zone2) + pb._field_i32(4, 1)
    return brut + pb._field_bytes(5, _polygone(points)) + pb._field_i32(6, 1 if station else 0)


def contenu(pelouse=PELOUSE, nom="Pelouse", massif=MASSIF, passage=((9.5, 3.0), (14.5, 3.0)), station=(1.0, 1.0)) -> bytes:
    """Contenu brut d'une carte (PbMap) : deux zones, une zone interdite, deux passages, une station."""
    brut = pb._field_bytes(1, _zone("zoneA1", nom, pelouse)) + pb._field_bytes(1, _zone("zoneB1", "Verger", VERGER))
    if massif:
        brut += pb._field_bytes(2, _zone("nogoA1", "Massif", massif, parent="zoneA1"))
    brut += pb._field_bytes(3, _passage("passAB", "zoneA1", "zoneB1", passage))
    brut += pb._field_bytes(3, _passage("passSt", "zoneA1", "", [(1.0, 1.0), (2.0, 2.0)], station=True))
    brut += pb._field_bytes(4, pb._field_f32(1, station[0]) + pb._field_f32(2, station[1]) + pb._field_f32(3, 0.0))
    return brut + pb._field_bytes(11, b"\x08\x32")  # réglages globaux de tonte


def reponse(brut: bytes) -> bytes:
    """Réponse du robot : PbOutput.f23 → f2 → f3 = contenu de la carte."""
    return pb._field_bytes(23, pb._field_bytes(2, pb._field_bytes(3, brut)))


def lancer(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def carte() -> cartes.Carte:
    return cartes.decoder(contenu())


# --- lecture ----------------------------------------------------------------


def test_decodage_de_la_carte_zones_interdites_passages_station(carte):
    assert [(z.identifiant, z.nom) for z in carte.zones] == [("zoneA1", "Pelouse"), ("zoneB1", "Verger")]
    assert carte.zones[0].contour == PELOUSE
    assert [(z.nom, z.parent) for z in carte.interdites] == [("Massif", "zoneA1")]
    assert [(p.identifiant, p.station) for p in carte.passages] == [("passAB", False), ("passSt", True)]
    assert carte.station == (1.0, 1.0, 0.0)
    lisible = cartes.vers_json(carte)
    assert lisible["zones"][0] | {"contour": None} == {
        "identifiant": "zoneA1", "nom": "Pelouse", "sommets": 6, "surface_m2": 60.0, "contour": None,
    }
    assert lisible["passages"][1]["passage_de_station"] is True and lisible["station"]["est_m"] == 1.0
    json.dumps(lisible)


def test_zone_par_identifiant_ou_par_nom_jamais_d_a_peu_pres(carte):
    assert carte.zone("zoneB1").nom == "Verger" and carte.zone("pelouse").identifiant == "zoneA1"
    for cle in ("Pel", "zone", ""):
        with pytest.raises(cartes.CarteIllisible, match="introuvable"):
            carte.zone(cle)


def test_reponse_de_trajet_capturee_par_le_socle_n_est_pas_prise_pour_une_carte(session):
    capture = base64.b64decode((Path(__file__).parent / "donnees" / "reponse_trajet_ha_lymow.b64").read_text().strip())
    brut = pb.extract_raw_map_content(capture)
    assert isinstance(brut, bytes) and len(brut) > 1000  # même enveloppe f23 → f2 → f3 qu'une carte
    with pytest.raises(cartes.CarteIllisible):
        cartes.decoder(brut)
    session.recevoir_etat({"mapRaw": brut})
    assert cartes.en_memoire(session) is None


@pytest.mark.parametrize("brut", [b"", b"\xff\xff", b"\x0a\x02\x08\x01", pb._field_i32(1, 3), pb._field_bytes(2, b"\x08\x01")])
def test_contenu_qui_n_est_pas_une_carte_refuse(brut):
    with pytest.raises(cartes.CarteIllisible):
        cartes.decoder(brut)


def test_socle_transmet_le_contenu_brut_de_la_carte():
    recus = []
    client = socle_mqtt.LymowMqttClient("hote", "eu-west-1", lambda a, f, r: recus.append(f), lambda a, e, r: None)
    client._things = ["robot-test"]
    enveloppe = json.dumps({"message": base64.b64encode(reponse(contenu())).decode()})
    client._dispatch(SimpleNamespace(topic="/device/robot-test/pboutput", payload=enveloppe))
    assert recus[0]["mapRaw"] == contenu()


def test_demande_de_carte_requete_pure_octet_pour_octet():
    # version(2)=49 → 10 31 ; userCtrl(5)=19 → 28 13 ; champ 23 (ba 01), 4 octets : {1: 0, 4: 1} → 08 00 20 01
    trame = protocole.trame_demande_carte(Calibration())
    assert trame.octets == bytes.fromhex("10 31 28 13 ba 01 04 08 00 20 01") == DEMANDE_CARTE
    assert trame.categorie is Categorie.REQUETE_PURE and est_requete_pure(trame.octets)
    assert est_requete_pure(protocole.trame_demande_carte(dataclasses.replace(Calibration(), version_protocole=40)).octets)


@pytest.mark.parametrize(
    "octets",
    [
        "10 31 28 13 ba 01 04 08 01 20 01",  # autre index
        "10 31 28 13 ba 01 04 08 00 20 00",  # drapeau à zéro
        "10 31 28 13 ba 01 06 08 00 20 01 28 01",  # champ en plus dans la demande
        "10 31 28 19 ba 01 04 08 00 20 01",  # userCtrl 25 : envoi de carte entière
        "10 31 28 09 ba 01 04 08 00 20 01",  # userCtrl 9 : écriture
        "10 31 28 17 ba 01 04 08 00 18 01",  # userCtrl 23 : trajet, non retenu
        "10 0c 28 13 ba 01 04 08 00 20 01",  # version inconnue
        "10 31 28 13 ba 01 04 08 00 20 01 62 00",  # carte jointe à la demande
        "10 31 28 13",  # sans le champ de demande
        "10 31 28 13 ba 01 05 08 80 00 20 01",  # même valeur, autre codage des octets
    ],
)
def test_variantes_de_la_demande_de_carte_refusees(octets):
    assert not est_requete_pure(bytes.fromhex(octets))


# --- géométrie --------------------------------------------------------------


def test_portion_cote_court_long_et_tout():
    assert geometrie.portion(PELOUSE, 0, 2, "court") == [0, 1, 2]
    assert geometrie.portion(PELOUSE, 0, 2, "long") == [0, 5, 4, 3, 2]
    assert geometrie.portion(PELOUSE, 2, 0, "court") == [2, 1, 0]
    assert geometrie.portion(PELOUSE, 3, 3, "tout") == [0, 1, 2, 3, 4, 5]
    for de, a, cote in ((0, 6, "court"), (-1, 2, "court"), (0, 2, "milieu")):
        with pytest.raises(ValueError):
            geometrie.portion(PELOUSE, de, a, cote)


def test_polygone_simple_appartenance_et_contact():
    assert geometrie.est_simple(PELOUSE) and geometrie.aire_signee(PELOUSE) == 60.0
    assert not geometrie.est_simple([(0, 0), (4, 4), (4, 0), (0, 4)])  # nœud papillon
    assert not geometrie.est_simple([(0, 0), (4, 0), (2, 0), (2, 3)])  # arête repliée
    assert not geometrie.est_simple([(0, 0), (4, 0), (4, 0), (0, 3)])  # sommet doublé
    assert geometrie.contient(PELOUSE, (5, 3)) and not geometrie.contient(PELOUSE, (5, 0)) and not geometrie.contient(PELOUSE, (11, 3))
    assert geometrie.strictement_dans(MASSIF, PELOUSE)
    assert not geometrie.strictement_dans([(4, 0), (6, 0), (6, 1), (4, 1)], PELOUSE)  # touche le bord
    assert geometrie.touche(PELOUSE, [(9.5, 3), (14.5, 3)]) and geometrie.touche(PELOUSE, [(10, 3), (14, 3)])
    assert not geometrie.touche(PELOUSE, [(10.2, 3), (14, 3)]) and not geometrie.touche(PELOUSE, [])


def test_rectangle_elargi_d_un_cote_entier(carte):
    operation = retouche.preparer_ajustement(carte, "Pelouse", 0, 2, 20, "court", 0)
    apres = operation.apres
    assert apres[1] == pytest.approx((5.0, -0.2)) and apres[3:] == PELOUSE[3:]
    assert apres[0] == pytest.approx((-0.2 / math.sqrt(2), -0.2 / math.sqrt(2)), abs=1e-6)  # angle : le long de la bissectrice
    r = operation.resume
    assert (r["sommets"], r["sommets_deplaces"], r["deplacement_max_cm"], r["action"]) == (6, 3, 20.0, "élargir")
    # Bande de 10 m × 0,2 m (2 m²) plus les deux coins tirés le long de la bissectrice.
    assert r["surface_avant_m2"] == 60.0 and r["variation_m2"] == pytest.approx(2.58, abs=0.01)
    assert operation.description == "ajuster-zone zoneA1 --de 0 --a 2 --cm 20 --cote court --raccord 0"


def test_portion_partielle_avec_raccords_progressifs(carte):
    elargie = retouche.preparer_ajustement(carte, "zoneA1", 0, 2, 20, "court", 0.5)
    assert elargie.apres[0] == PELOUSE[0] and elargie.apres[2] == PELOUSE[2]  # extrémités : écart nul
    assert elargie.apres[1] == pytest.approx((5.0, -0.2))
    assert elargie.resume["variation_m2"] == 1.0 and elargie.resume["sommets_deplaces"] == 1
    rognee = retouche.preparer_ajustement(carte, "zoneA1", 0, 2, -20, "court", 0.5)
    assert rognee.apres[1] == pytest.approx((5.0, 0.2)) and rognee.resume["variation_m2"] == -1.0
    douce = retouche.preparer_ajustement(carte, "zoneA1", 0, 2, 20, "court", 10 / 3)  # raccord de 3,33 m sur 5 m
    assert douce.apres[1] == pytest.approx((5.0, -0.2)) and douce.resume["raccord_m"] == pytest.approx(10 / 3)
    courte = retouche.preparer_ajustement(carte, "zoneA1", 0, 1, 20, "court", 0)
    assert courte.resume["portion"] == {"de": 0, "a": 1, "cote": "court", "sommets": 2}
    with pytest.raises(RefusSecurite, match="raccord hors bornes"):
        retouche.preparer_ajustement(carte, "zoneA1", 0, 2, 20, "court", 6)
    with pytest.raises(RefusSecurite, match="aucun sommet déplacé"):
        retouche.preparer_ajustement(carte, "zoneA1", 0, 1, 20, "court", 0.5)  # deux extrémités, rien entre


@pytest.mark.parametrize("cm", [10, -10])
def test_sens_horaire_et_trigonometrique_meme_resultat(cm):
    directe = cartes.decoder(contenu())
    inverse = cartes.decoder(contenu(pelouse=list(reversed(PELOUSE))))
    a = retouche.preparer_ajustement(directe, "zoneA1", 0, 0, cm, "tout", 0)
    b = retouche.preparer_ajustement(inverse, "zoneA1", 0, 0, cm, "tout", 0)
    assert a.resume["surface_apres_m2"] == b.resume["surface_apres_m2"]
    assert (a.resume["variation_m2"] > 0) == (cm > 0) and a.resume["sommets_deplaces"] == 6
    assert (geometrie.aire_signee(a.apres) > 0) and (geometrie.aire_signee(b.apres) < 0)  # sens conservé


def test_contour_concave(carte):
    equerre = [(0.0, 0.0), (6.0, 0.0), (6.0, 2.0), (2.0, 2.0), (2.0, 6.0), (0.0, 6.0)]
    concave = cartes.decoder(contenu(pelouse=equerre, massif=None, passage=((5.5, 1.0), (14.5, 3.0)), station=(1.0, 1.0)))
    rognee = retouche.preparer_ajustement(concave, "zoneA1", 0, 0, -30, "tout", 0)
    assert geometrie.est_simple(rognee.apres) and rognee.resume["variation_m2"] < 0
    assert rognee.apres[3] == pytest.approx((2 - 0.3 / math.sqrt(2), 2 - 0.3 / math.sqrt(2)), abs=1e-6)  # angle rentrant


def test_sec10_auto_intersection_provoquee_refusee():
    bande = [(0.0, 0.0), (10.0, 0.0), (10.0, 0.3), (5.0, 0.3), (0.0, 0.3)]
    etroite = cartes.decoder(contenu(pelouse=bande, massif=None, passage=((9.5, 0.1), (14.5, 3.0)), station=(1.0, 0.1)))
    with pytest.raises(RefusSecurite, match=r"se recoupe lui-même \(SEC-10\)"):
        retouche.preparer_ajustement(etroite, "zoneA1", 2, 4, -40, "court", 0.5)


def test_sec10_zone_interdite_qui_ne_resterait_pas_dedans_refusee():
    pres_du_bord = cartes.decoder(contenu(massif=[(4.0, 0.1), (6.0, 0.1), (6.0, 1.0), (4.0, 1.0)]))
    with pytest.raises(RefusSecurite, match=r"zone interdite « Massif » ne resterait pas strictement à l'intérieur \(SEC-10\)"):
        retouche.preparer_ajustement(pres_du_bord, "zoneA1", 0, 2, -20, "court", 0.5)
    retouche.preparer_ajustement(pres_du_bord, "zoneA1", 0, 2, 20, "court", 0.5)  # élargir reste permis


def test_sec10_passage_qui_ne_toucherait_plus_la_zone_refuse():
    au_bord = cartes.decoder(contenu(passage=((10.0, 3.0), (14.5, 3.0))))
    with pytest.raises(RefusSecurite, match=r"passage « passAB » ne toucherait plus la zone \(SEC-10\)"):
        retouche.preparer_ajustement(au_bord, "zoneA1", 2, 3, -20, "court", 0)
    retouche.preparer_ajustement(au_bord, "zoneA1", 2, 3, 20, "court", 0)


def test_sec10_station_qui_sortirait_de_la_zone_refusee():
    pres_du_bas = cartes.decoder(contenu(station=(5.0, 0.1)))
    with pytest.raises(RefusSecurite, match=r"station de charge ne serait plus dans la zone \(SEC-10\)"):
        retouche.preparer_ajustement(pres_du_bas, "zoneA1", 0, 2, -20, "court", 0.5)


@pytest.mark.parametrize("cm", [50.1, 51, -50.5, 200, 0, math.nan, math.inf])
def test_sec10_ecart_hors_bornes_refuse(carte, cm):
    with pytest.raises(RefusSecurite):
        retouche.preparer_ajustement(carte, "zoneA1", 0, 2, cm, "court", 0)


def test_sec10_bornes_au_plafond_acceptees(carte):
    assert retouche.preparer_ajustement(carte, "zoneA1", 0, 2, 50, "court", 0).resume["deplacement_max_cm"] == 50.0
    assert retouche.preparer_ajustement(carte, "zoneA1", 0, 2, -50, "court", 0).resume["variation_m2"] < 0


def test_sec10_controle_direct_de_chaque_borne(carte):
    zone = carte.zones[0]
    cas = {
        "nombre de sommets modifié": PELOUSE[:-1],
        "deux sommets consécutifs confondus": [PELOUSE[0], PELOUSE[0], *PELOUSE[2:]],
        "sens de parcours inversé": [(x + 0.01, y) for x, y in reversed(PELOUSE)],
        "se déplace de": [(x, y - 0.4) if i == 1 else (x, y) for i, (x, y) in enumerate(PELOUSE)],
        "surface ne varie pas dans le sens": [(x, y + 0.2) if i == 1 else (x, y) for i, (x, y) in enumerate(PELOUSE)],
        "aucun sommet déplacé": list(PELOUSE),
    }
    for motif, apres in cas.items():
        with pytest.raises(RefusSecurite, match=motif):
            retouche.controler(carte, zone, apres, 0.2)


def test_indices_ou_zone_invalides(carte):
    with pytest.raises(ErreurSaisie):
        retouche.preparer_ajustement(carte, "zoneA1", 0, 9, 10, "court", 0)
    with pytest.raises(cartes.CarteIllisible):
        retouche.preparer_ajustement(carte, "Potager", 0, 2, 10, "court", 0)


# --- encodage de la retouche ------------------------------------------------


def test_retouche_de_contour_octet_pour_octet():
    # Calcul à la main. Point = 0d <x f32> 15 <y f32> (10 octets) ; 1.0 = 0000803f, 2.0 = 00000040,
    # 3.0 = 00004040, 4.0 = 00008040, 5.0 = 0000a040, 6.0 = 0000c040.
    # polygone (champ 5) = 3 × (0a 0a + point) = 36 octets (24) ; infos = 1a 02 "ab" + 2a 24 + polygone = 42 (2a) ;
    # zone = 0a 2a + infos = 44 (2c) ; carte (champ 12) = 0a 2c + zone = 46 (2e) ; version 49, userCtrl 9.
    reference = bytes.fromhex(
        "10 31 28 09 62 2e 0a 2c 0a 2a 1a 02 61 62 2a 24"
        "0a 0a 0d 00 00 80 3f 15 00 00 00 40"
        "0a 0a 0d 00 00 40 40 15 00 00 80 40"
        "0a 0a 0d 00 00 a0 40 15 00 00 c0 40"
    )
    trame = protocole.trame_contour_zone("ab", [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)], Calibration())
    assert trame.octets == reference and trame.categorie is Categorie.CARTE
    assert not est_requete_pure(trame.octets) and trame.octets.startswith(ENTETE_ECRITURE_CARTE)


@pytest.mark.parametrize(
    ("identifiant", "contour"),
    [("", PELOUSE), ("a b", PELOUSE), ("x" * 65, PELOUSE), ("zoneA1", PELOUSE[:2]), ("zoneA1", [(0.0, math.nan), (1.0, 0.0), (0.0, 1.0)])],
)
def test_retouche_refuse_identifiant_ou_contour_invalide(identifiant, contour):
    with pytest.raises((ValueError, RefusSecurite)):
        protocole.trame_contour_zone(identifiant, contour, Calibration())


def test_aucun_envoi_de_carte_entiere_dans_le_projet():
    import lymow_pilotage

    source = "".join(f.read_text(encoding="utf-8") for f in Path(lymow_pilotage.__file__).parent.rglob("*.py"))
    assert "def encode_sync_map" not in source and "USER_CTRL_SYNC_MAP" not in source
    assert "userCtrl 9" not in str(protocole.USER_CTRL_AUTORISES) and 9 not in protocole.USER_CTRL_AUTORISES


# --- SEC-08 à SEC-11 : écriture réelle sur faux robot -----------------------


def _operation(carte_, cm=20):
    return retouche.preparer_ajustement(carte_, "zoneA1", 0, 2, cm, "court", 0.5)


def _contenu_applique(operation, **autres) -> bytes:
    return contenu(pelouse=operation.apres, **autres)


@pytest.mark.parametrize("statut", [0, 1, 5, 12])
def test_sec08_ecriture_permise_au_repos_ou_en_charge(session, lien, tmp_path, carte, statut):
    lien.carte, lien.robot = contenu(), fragment(statut=statut)
    lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert len(lien.ecritures) == 1


@pytest.mark.parametrize("statut", [2, 3, 4, 6, 7, 8, 9, 10, 11, 13, 14, 99, -1])
def test_sec08_ecriture_refusee_en_tonte_pause_erreur_telecommande_retour(session, lien, tmp_path, carte, statut):
    lien.carte, lien.robot = contenu(), fragment(statut=statut)
    with pytest.raises(RefusSecurite, match="SEC-08"):
        lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert lien.ecritures == [] and not (tmp_path / "sauvegardes-carte").exists()


def test_sec08_robot_silencieux_ou_hors_ligne_refuse(session, lien, tmp_path, carte, horloge):
    lien.carte, lien.robot = contenu(), None
    with pytest.raises(RefusSecurite, match="SEC-08"):
        lancer(retouche.emettre(session, _operation(carte), tmp_path))

    def hors_ligne(_lien, octets):
        if octets == bytes.fromhex("10 31 4a 02 50 01"):
            horloge.t += 0.01
            session.recevoir_etat(fragment())
            session.recevoir_presence(False)

    lien.sur_publication = hors_ligne
    with pytest.raises(RefusSecurite, match="hors ligne"):
        lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert lien.ecritures == []


def test_sec08_session_de_simulation_incapable_d_ecrire(session, lien, tmp_path, carte):
    lien.carte = contenu()
    session.autorisees = frozenset({Categorie.REQUETE_PURE})
    with pytest.raises(RefusSecurite, match="interdite dans cette commande"):
        lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert lien.ecritures == []


def test_sec09_sauvegarde_complete_ecrite_avant_la_publication(session, lien, tmp_path, carte):
    lien.carte = contenu()
    vus = []

    def a_l_ecriture(_lien, octets):
        if octets.startswith(ENTETE_ECRITURE_CARTE):
            vus.extend(sorted(f.name for f in (tmp_path / "sauvegardes-carte").iterdir()))
            assert next((tmp_path / "sauvegardes-carte").glob("*.pb")).read_bytes() == contenu()

    lien.sur_publication = a_l_ecriture
    bilan, _code = lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert len(vus) == 2 and vus[0].endswith("_zoneA1.json") and vus[1].endswith("_zoneA1.pb")
    sauvegarde = Path(bilan["sauvegarde"])
    assert sauvegarde.read_bytes() == contenu()  # octets bruts, tels que reçus
    lisible = json.loads(sauvegarde.with_suffix(".json").read_text(encoding="utf-8"))
    assert lisible["zone_retouchee"] == "zoneA1" and lisible["zones"][0]["contour"][2] == [10.0, 0.0]
    assert len(lisible["zones_interdites"]) == 1 and len(lisible["passages"]) == 2
    ecriture = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "carte-ecriture")
    assert ecriture["sauvegarde"] == str(sauvegarde) and ecriture["zone"] == "zoneA1"
    assert ecriture["resume"]["ecart_cm"] == 20 and ecriture["resume"]["portion"]["de"] == 0
    assert "carte-verdict" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_sec09_sauvegarde_impossible_aucune_ecriture(session, lien, tmp_path, carte):
    lien.carte = contenu()
    (tmp_path / "sauvegardes-carte").write_text("pas un dossier", encoding="utf-8")
    with pytest.raises(RefusSecurite, match=r"sauvegarde de la carte impossible .* \(SEC-09\)"):
        lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert lien.ecritures == []


def test_sec09_carte_de_plus_de_60_s_redemandee_carte_fraiche_reutilisee(session, lien, tmp_path, carte, horloge):
    lien.carte = contenu()
    session.recevoir_etat({"mapRaw": contenu()})
    horloge.t += 61.0
    lancer(retouche.emettre(session, _operation(carte), tmp_path))
    avant_ecriture = lien.trames[: lien.trames.index(lien.ecritures[0])]
    assert avant_ecriture.count(DEMANDE_CARTE) == 1  # périmée : redemandée

    lien.publies.clear()
    lien.ecritures.clear()
    horloge.t += 5.0  # la carte relue après l'écriture précédente a moins de 60 s
    lancer(retouche.emettre(session, _operation(carte), tmp_path))
    avant_ecriture = lien.trames[: lien.trames.index(lien.ecritures[0])]
    assert avant_ecriture.count(DEMANDE_CARTE) == 0


def test_sec09_sans_carte_ou_carte_changee_depuis_la_previsualisation_refus(session, lien, tmp_path, carte):
    lien.carte = None
    with pytest.raises(EchecOrdre, match="carte non reçue"):
        lancer(retouche.emettre(session, _operation(carte), tmp_path))
    lien.carte = contenu(pelouse=[(x, y + 0.05) for x, y in PELOUSE])
    with pytest.raises(RefusSecurite, match=r"la carte a changé depuis la prévisualisation .* \(SEC-09\)"):
        lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert lien.ecritures == []


def test_sec10_reverifie_sur_la_carte_fraiche_avant_d_ecrire(session, lien, tmp_path, carte):
    operation = _operation(carte, cm=-20)
    lien.carte = contenu(massif=[(4.0, 0.1), (6.0, 0.1), (6.0, 1.0), (4.0, 1.0)])  # le massif a été déplacé entre-temps
    with pytest.raises(RefusSecurite, match="SEC-10"):
        lancer(retouche.emettre(session, operation, tmp_path))
    assert lien.ecritures == []


def test_sec11_verdict_applique(session, lien, tmp_path, carte, messages):
    operation = _operation(carte)
    lien.carte, lien.carte_apres = contenu(), _contenu_applique(operation)
    bilan, code = lancer(retouche.emettre(session, operation, tmp_path))
    assert (bilan["verdict"], bilan["differences"], code) == ("appliqué", [], 0)
    assert lien.ecritures == [protocole.trame_contour_zone("zoneA1", operation.apres, Calibration()).octets]
    assert messages == []


def test_sec11_verdict_ignore_par_le_robot(session, lien, tmp_path, carte):
    lien.carte = contenu()
    bilan, code = lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert (bilan["verdict"], code) == ("ignoré par le robot", 1) and len(lien.ecritures) == 1


@pytest.mark.parametrize(
    ("autres", "difference"),
    [
        ({"nom": "Renommée"}, "le nom ou les réglages de la zone ont changé"),
        ({"massif": None}, "changement inattendu : zones interdites"),
        ({"passage": ((9.0, 3.0), (14.5, 3.0))}, "changement inattendu : passages"),
        ({"station": (1.5, 1.0)}, "changement inattendu : station de charge"),
    ],
)
def test_sec11_difference_inattendue_message_en_tete_sauvegarde_et_restauration(
    session, lien, tmp_path, carte, messages, autres, difference
):
    operation = _operation(carte)
    lien.carte, lien.carte_apres = contenu(), _contenu_applique(operation, **autres)
    bilan, code = lancer(retouche.emettre(session, operation, tmp_path))
    assert bilan["verdict"] == "DIFFÉRENCE INATTENDUE" and code == 1 and difference in bilan["differences"]
    assert messages[0].startswith("DIFFÉRENCE INATTENDUE") and bilan["sauvegarde"] in messages[0]
    assert messages[1] == f"Pour revenir en arrière : {bilan['restauration']}"
    assert bilan["restauration"] == f'lymow restaurer-zone "{bilan["sauvegarde"]}" --zone zoneA1 --reel'
    verdict = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "carte-verdict")
    assert verdict["verdict"] == "DIFFÉRENCE INATTENDUE" and difference in verdict["differences"]


def test_sec11_contour_relu_ni_ancien_ni_envoye_et_autre_zone_modifiee(carte):
    operation = _operation(carte)
    decale = cartes.decoder(contenu(pelouse=[(x, y - 0.1) if i == 1 else (x, y) for i, (x, y) in enumerate(PELOUSE)]))
    assert retouche.comparer(carte, decale, operation) == (
        "DIFFÉRENCE INATTENDUE", ["le contour relu n'est ni l'ancien ni celui envoyé"],
    )
    sans_zone = cartes.decoder(pb._field_bytes(1, _zone("zoneB1", "Verger", VERGER)))
    assert retouche.comparer(carte, sans_zone, operation)[1] == ["la zone retouchée a disparu de la carte"]
    autre = contenu(pelouse=operation.apres).replace(_zone("zoneB1", "Verger", VERGER), _zone("zoneB1", "Verger", VERGER[:3] + [(14.0, 7.0)]))
    assert retouche.comparer(carte, cartes.decoder(autre), operation)[1] == ["une autre zone a changé : zoneB1"]


def test_sec11_carte_non_relue_dit_non_verifie(session, lien, tmp_path, carte, messages):
    lien.carte = contenu()

    def robot_muet_ensuite(lien_, octets):
        if octets.startswith(ENTETE_ECRITURE_CARTE):
            lien_.carte = None

    lien.sur_publication = robot_muet_ensuite
    bilan, code = lancer(retouche.emettre(session, _operation(carte), tmp_path))
    assert bilan["verdict"].startswith("non vérifié") and code == 1 and "restaurer-zone" in messages[1]


def test_restauration_renvoie_l_ancien_contour_par_le_meme_encodeur(session, lien, tmp_path, carte):
    operation = _operation(carte)
    lien.carte, lien.carte_apres = contenu(), _contenu_applique(operation)
    bilan, _code = lancer(retouche.emettre(session, operation, tmp_path))
    sauvegarde = retouche.charger_sauvegarde(Path(bilan["sauvegarde"]).with_suffix(".json"))  # .pb ou .json voisin
    actuelle = cartes.decoder(lien.carte)
    retour = retouche.preparer_restauration(actuelle, sauvegarde, None, bilan["sauvegarde"])
    assert retour.ecart_m is None and retour.apres == PELOUSE and retour.resume["sommets_deplaces"] == 1
    lien.carte_apres = contenu()
    bilan2, code = lancer(retouche.emettre(session, retour, tmp_path))
    assert (bilan2["verdict"], code) == ("appliqué", 0)
    assert lien.ecritures[-1] == protocole.trame_contour_zone("zoneA1", PELOUSE, Calibration()).octets
    assert len(list((tmp_path / "sauvegardes-carte").glob("*.pb"))) == 2  # SEC-09 : la restauration sauvegarde aussi


def test_restauration_memes_bornes_et_zone_a_preciser(carte, tmp_path):
    loin = cartes.decoder(contenu(pelouse=[(x, y - 0.8) if i == 1 else (x, y) for i, (x, y) in enumerate(PELOUSE)]))
    with pytest.raises(RefusSecurite, match="SEC-10"):
        retouche.preparer_restauration(loin, carte, "zoneA1", "s.pb")  # 80 cm : plus qu'une opération
    with pytest.raises(ErreurSaisie, match="--zone"):
        retouche.preparer_restauration(carte, carte, None, "s.pb")  # aucune zone ne diffère
    with pytest.raises(RefusSecurite, match="aucun sommet déplacé"):
        retouche.preparer_restauration(carte, carte, "Pelouse", "s.pb")
    with pytest.raises(ErreurSaisie, match="illisible"):
        retouche.charger_sauvegarde(tmp_path / "absente.pb")


# --- ligne de commande ------------------------------------------------------


@pytest.fixture
def liaison_factice(monkeypatch, session):
    ouvertures = []

    @asynccontextmanager
    async def ouvrir(_journal, _cal, autorisees, *_reste):
        ouvertures.append(autorisees)
        session.autorisees = autorisees
        yield session

    monkeypatch.setattr(liaison, "ouvrir_session", ouvrir)
    return ouvertures


def commande(capsys, tmp_path, *arguments):
    code = cli.main([*arguments, "--json", "--donnees", str(tmp_path)])
    sortie = capsys.readouterr()
    return code, (json.loads(sortie.out) if sortie.out.strip() else None), sortie.err


def test_commande_carte_lecture_seule(capsys, tmp_path, liaison_factice, lien):
    lien.carte = contenu()
    code, sortie, _err = commande(capsys, tmp_path, "carte")
    assert code == 0 and liaison_factice == [frozenset({Categorie.REQUETE_PURE})]
    assert sortie["zones"][0]["contour"][3] == [10.0, 6.0] and sortie["robot"]["position_est_m"] == 0.0
    assert lien.ordres == [] and DEMANDE_CARTE in lien.trames
    assert cli.main(["carte", "--donnees", str(tmp_path)]) == 0
    texte = capsys.readouterr().out
    assert "Pelouse" in texte and "surface (m²) : 60.0" in texte and "contour" not in texte


def test_commande_ajuster_zone_simulation_resume_et_apercu_sans_ecriture(capsys, tmp_path, liaison_factice, lien):
    lien.carte = contenu()
    code, sortie, _err = commande(capsys, tmp_path, "ajuster-zone", "Pelouse", "--de", "0", "--a", "2", "--cm", "-20")
    assert code == 0 and sortie["mode"] == "simulation" and liaison_factice == [frozenset({Categorie.REQUETE_PURE})]
    assert (sortie["ecart_cm"], sortie["action"], sortie["variation_m2"], sortie["raccord_m"]) == (-20, "rogner", -1.0, 0.5)
    apercu = Path(sortie["apercu"])
    assert apercu.parent == tmp_path / "apercus-carte" and "<polygon" in apercu.read_text(encoding="utf-8")
    assert lien.ecritures == [] and lien.ordres == [] and not (tmp_path / "sauvegardes-carte").exists()
    assert "carte-simulee" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_commande_ajuster_zone_reelle_et_restauration(capsys, tmp_path, liaison_factice, lien, carte):
    operation = retouche.preparer_ajustement(carte, "Pelouse", 0, 2, 5, "court", None)
    lien.carte, lien.carte_apres = contenu(), _contenu_applique(operation)
    code, sortie, _err = commande(capsys, tmp_path, "ajuster-zone", "zoneA1", "--de", "0", "--a", "2", "--cm", "5", "--reel")
    assert code == 0 and sortie["verdict"] == "appliqué" and len(lien.ecritures) == 1
    assert liaison_factice == [frozenset(Categorie)]
    code, simulee, _err = commande(capsys, tmp_path, "restaurer-zone", sortie["sauvegarde"])
    assert code == 0 and simulee["mode"] == "simulation" and len(lien.ecritures) == 1
    lien.carte_apres = contenu()
    code, restauree, _err = commande(capsys, tmp_path, "restaurer-zone", sortie["sauvegarde"], "--zone", "Pelouse", "--reel")
    assert code == 0 and restauree["verdict"] == "appliqué" and len(lien.ecritures) == 2


@pytest.mark.parametrize(
    ("arguments", "motif"),
    [
        (("ajuster-zone", "Pelouse", "--de", "0", "--a", "2", "--cm", "60"), "SEC-10"),
        (("ajuster-zone", "Pelouse", "--de", "0", "--a", "2", "--cm", "60", "--reel"), "SEC-10"),
        (("ajuster-zone", "Potager", "--de", "0", "--a", "2", "--cm", "5"), "introuvable"),
        (("ajuster-zone", "Pelouse", "--de", "0", "--a", "42", "--cm", "5"), "indices"),
        (("restaurer-zone", "absente.pb"), "illisible"),
    ],
)
def test_commandes_de_carte_refusees_sans_ecriture(capsys, tmp_path, liaison_factice, lien, arguments, motif):
    lien.carte = contenu()
    code, _sortie, err = commande(capsys, tmp_path, *arguments)
    assert code == 2 and motif in err and lien.ecritures == []


def test_commande_ajuster_zone_difference_inattendue_code_non_nul_et_message_en_tete(capsys, tmp_path, liaison_factice, lien, carte):
    operation = retouche.preparer_ajustement(carte, "Pelouse", 0, 2, 5, "court", None)
    lien.carte, lien.carte_apres = contenu(), _contenu_applique(operation, massif=None)
    code = cli.main(["ajuster-zone", "Pelouse", "--de", "0", "--a", "2", "--cm", "5", "--reel", "--donnees", str(tmp_path)])
    assert code == 1 and "DIFFÉRENCE INATTENDUE" in capsys.readouterr().out


# --- routes de la page ------------------------------------------------------

AJUSTEMENT = {"zone": "zoneA1", "de": 0, "a": 2, "cm": 20, "cote": "court", "raccord": 0.5}


def test_page_carte_en_simulation_lecture_et_previsualisation_sans_emission(tmp_path, monkeypatch, session_reelle, lien):
    from test_ui import executer, lire, poster, serveur_ui

    lien.carte = contenu()

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            statut, texte, _recus = await lire(http, serveur, "/api/carte")
            assert statut == 200 and json.loads(texte) == {"carte": None}
            statut, corps, _recus = await poster(http, serveur, "/api/carte/previsualiser", AJUSTEMENT)
            assert statut == 409  # pas de carte en mémoire
            statut, corps, recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 200 and corps["carte"]["zones"][0]["nom"] == "Pelouse" and corps["heure"]
            assert recus["Cache-Control"] == "no-store"
            statut, corps, _recus = await poster(http, serveur, "/api/carte/previsualiser", AJUSTEMENT)
            assert statut == 200 and corps["identifiant"] is None and corps["resume"]["variation_m2"] == 1.0
            assert corps["avant"][1] == [5.0, 0.0] and corps["apres"][1] == pytest.approx([5.0, -0.2])
            statut, erreur, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": "x"})
            assert statut == 403 and "SEC-05" in erreur["erreur"]
            for chemin in ("/api/carte/actualiser", "/api/carte/previsualiser"):
                statut, _corps, _recus = await poster(http, serveur, chemin, AJUSTEMENT, Cookie=None)
                assert statut == 403
                statut, _corps, _recus = await poster(http, serveur, chemin, AJUSTEMENT, Origin="http://evil.example")
                assert statut == 403
            statut, _texte, _recus = await lire(http, serveur, "/api/carte", Cookie=None)
            assert statut == 403

    executer(scenario())
    assert lien.ecritures == [] and all(est_requete_pure(octets) for octets in lien.trames)


def test_page_carte_en_reel_previsualisation_a_usage_unique_puis_ecriture(tmp_path, monkeypatch, session_reelle, lien, carte):
    from test_ui import attendre, executer, poster, serveur_ui

    operation = _operation(carte)
    lien.carte, lien.carte_apres = contenu(), _contenu_applique(operation)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            await poster(http, serveur, "/api/carte/actualiser")
            statut, refus, _recus = await poster(http, serveur, "/api/carte/previsualiser", AJUSTEMENT | {"cm": 60})
            assert statut == 200 and "SEC-10" in refus["refus"] and refus["identifiant"] is None
            for incomplet in ({"zone": "zoneA1"}, AJUSTEMENT | {"cote": "milieu"}, AJUSTEMENT | {"de": 0.5}, AJUSTEMENT | {"cm": "20"}):
                statut, _corps, _recus = await poster(http, serveur, "/api/carte/previsualiser", incomplet)
                assert statut == 400
            statut, corps, _recus = await poster(http, serveur, "/api/carte/previsualiser", AJUSTEMENT)
            assert statut == 200 and corps["identifiant"]
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 202
            statut, _corps, _recus = await poster(http, serveur, "/api/emettre", {"identifiant": corps["identifiant"]})
            assert statut == 409  # identifiant rejoué
            statut, _corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 409  # SEC-08 : rien d'autre pendant l'écriture
            await attendre(lambda: not serveur.occupe)
            return serveur.resultat, serveur.dossier

    resultat, dossier = executer(scenario())
    assert resultat["verdict"] == "appliqué" and resultat["code_sortie"] == 0 and dossier == tmp_path
    assert len(lien.ecritures) == 1 and len(list((tmp_path / "sauvegardes-carte").glob("*.pb"))) == 1


def test_page_carte_dessinee_par_l_api_dom_et_avertissement_affiche():
    from lymow_pilotage import ui

    script = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    assert "createElementNS" in script and "/api/carte/previsualiser" in script
    assert "Jamais essayé sur ce robot" in page and "+5 cm" in page and 'id="carte-dessin"' in page
