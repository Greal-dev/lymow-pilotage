"""Grande carte, livrée par le robot en tranches de 81 920 octets (constat du 2026-10-12).

Jeu d'essai : `jardin_etendu`, entièrement fictif, de la forme de la carte réelle. La capture
réelle, elle, n'est jamais dans le dépôt : le dernier test ne tourne que si elle est présente.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import random
from pathlib import Path

import jardin_etendu
import pytest
from conftest import index_demande_carte

from lymow_pilotage import capture, geometrie, protocole, retouche
from lymow_pilotage import carte as cartes
from lymow_pilotage.protocole import TAILLE_TRANCHE_CARTE, est_requete_pure
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.session import EchecOrdre
from lymow_pilotage.vendor.ha_lymow import protocol as pb

CONTENU = jardin_etendu.contenu()


def lancer(coroutine):
    return asyncio.run(coroutine)


def verifier_invariants(carte: cartes.Carte) -> None:
    """Ce que la carte réelle vérifie (2026-10-12) et que le décodage doit donc accepter.

    Des compteurs seulement : un échec ne doit afficher aucune coordonnée du jardin."""
    zones = {zone.identifiant: zone.contour for zone in carte.zones}
    fautes = {
        "identifiants de zone en double": len(carte.zones) - len(zones),
        "contours de zone non simples": sum(not geometrie.est_simple(geometrie.reduire(c)[0]) for c in zones.values()),
        "zones à surface négative": sum(geometrie.aire_signee(c) <= 0 for c in zones.values()),
        "zones interdites non simples": sum(
            not geometrie.est_simple(geometrie.reduire(z.contour)[0]) for z in carte.interdites
        ),
        "zones interdites sans zone de la carte": sum(z.parent not in zones for z in carte.interdites),
        "zones interdites qui ne touchent pas leur zone": sum(
            z.parent in zones and not geometrie.touche(zones[z.parent], z.contour) for z in carte.interdites
        ),
        "zones dont la retouche serait refusée d'emblée": sum(
            retouche.empechement(carte, zone) is not None for zone in carte.zones if carte.complete
        ),
        "passages qui ne touchent pas leurs zones": sum(
            # Le passage de station ne relie qu'UNE zone : son autre extrémité n'en est pas une.
            not all(i in zones and geometrie.touche(zones[i], p.trace) for i in (p.zone1, p.zone2)[: 1 if p.station else 2])
            for p in carte.passages
        ),
    }
    assert {motif: n for motif, n in fautes.items() if n} == {}


def test_jeu_d_essai_de_la_forme_de_la_carte_reelle():
    assert TAILLE_TRANCHE_CARTE < len(CONTENU) < 2 * TAILLE_TRANCHE_CARTE
    carte = cartes.decoder(CONTENU)
    assert (len(carte.zones), len(carte.interdites), len(carte.passages)) == (55, 19, 55) and carte.complete
    assert [p.station for p in carte.passages].count(True) == 1
    assert not any(geometrie.touche(zone.contour, [carte.station[:2]]) for zone in carte.zones)
    sommets = sorted(len(zone.contour) for zone in carte.zones)
    assert sommets[0] == 23 and sommets[-1] == 600 and sum(bool(zone.nom) for zone in carte.zones) == 5
    assert sum(zone.contour[0] == zone.contour[-1] for zone in carte.zones) == 25
    assert sorted({len(z.rattachements) for z in carte.interdites}) == [5, 6, 9, 10, 11, 12]
    attendues = [jardin_etendu.identifiant_zone(jardin_etendu.zone_d_une_interdite(j)) for j in range(19)]
    assert [z.parent for z in carte.interdites] == attendues  # la zone touchée, pas la première de la liste
    assert sum(z.parent != z.rattachements[0] for z in carte.interdites) >= 9
    dedans = [any(geometrie.strictement_dans(z.contour, zone.contour) for zone in carte.zones) for z in carte.interdites]
    assert dedans.count(False) == 4
    verifier_invariants(carte)


def test_premiere_tranche_seule_refusee_par_le_decodage_strict(session):
    tranche = CONTENU[:TAILLE_TRANCHE_CARTE]
    with pytest.raises(cartes.CarteIllisible, match="tranche plus courte que la longueur annoncée"):
        cartes.decoder(tranche)
    session.recevoir_etat({"mapRaw": tranche})
    assert session.tranche_carte[0] == tranche and session.carte_brute is None  # une tranche n'est pas une carte
    assert cartes.en_memoire(session) is None


def test_grande_carte_lue_tranche_apres_tranche_puis_decodee_strictement(session, lien):
    lien.carte = CONTENU
    carte = lancer(cartes.demander(session, 20.0))
    assert carte.octets == CONTENU and carte.complete and session.carte_brute[0] == CONTENU
    assert [index_demande_carte(trame) for trame in lien.trames] == [0, 1]
    assert all(est_requete_pure(trame) for trame in lien.trames) and lien.ordres == []
    assert cartes.en_memoire(session).empreinte() == carte.empreinte()
    verifier_invariants(carte)


def test_carte_d_un_seul_message_une_seule_demande(session, lien):
    lien.carte = pb._field_bytes(1, jardin_etendu._zone(0, random.Random(1)))
    carte = lancer(cartes.demander(session, 20.0))
    assert len(carte.zones) == 1 and not carte.complete and [index_demande_carte(t) for t in lien.trames] == [0]


def test_suite_jamais_recue_carte_refusee_vite_avec_le_motif(session, lien, horloge):
    lien.carte, lien.tranches_servies = CONTENU, 1  # le robot ne répond qu'à l'index 0
    with pytest.raises(cartes.CarteIllisible, match="carte reçue mais refusée : 1 tranche.* de 81920 octets.*carte incomplète"):
        lancer(cartes.demander(session, 20.0))
    assert session.carte_brute is None and cartes.en_memoire(session) is None  # rien qui puisse servir à écrire


def test_robot_qui_ignore_l_index_carte_refusee(session, lien, horloge):
    lien.carte, lien.index_ignore = CONTENU, True
    depart = horloge.t
    with pytest.raises(cartes.CarteIllisible, match="renvoie la même première tranche"):
        lancer(cartes.demander(session, 20.0))
    assert horloge.t - depart < 1 and session.carte_brute is None  # motif immédiat, pas d'attente du délai


def test_carte_tronquee_d_un_seul_message_refusee_aussitot(session, lien, horloge):
    lien.carte = CONTENU[:70000]
    depart = horloge.t
    with pytest.raises(cartes.CarteIllisible, match="carte reçue mais refusée : .*70000 octets en 1 tranche"):
        lancer(cartes.demander(session, 20.0))
    assert horloge.t - depart < 1


def test_aucune_reponse_reste_un_delai_depasse(session, lien, horloge):
    lien.carte = None
    with pytest.raises(EchecOrdre, match="carte non reçue du robot dans le délai"):
        lancer(cartes.demander(session, 20.0))


def test_demande_de_tranche_requete_pure_et_bornee():
    from lymow_pilotage.config import Calibration

    for index in range(protocole.TRANCHES_CARTE_MAX):
        trame = protocole.trame_demande_carte(Calibration(), index)
        assert index_demande_carte(trame.octets) == index and est_requete_pure(trame.octets)
    with pytest.raises(ValueError):
        protocole.trame_demande_carte(Calibration(), protocole.TRANCHES_CARTE_MAX)
    assert not est_requete_pure(pb.encode_query_map(protocole.TRANCHES_CARTE_MAX, 49))


def test_reduction_ne_reunit_que_les_sommets_exactement_confondus_et_consecutifs():
    a, b, c, d = (0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)
    assert geometrie.reduire([a, b, c, d]) == ([a, b, c, d], [0, 1, 2, 3])
    assert geometrie.reduire([a, b, c, d, a]) == ([a, b, c, d], [0, 1, 2, 3, 0])  # fermé sur son premier point
    assert geometrie.reduire([a, b, b, b, c, d]) == ([a, b, c, d], [0, 1, 1, 1, 2, 3])  # doublons consécutifs
    assert geometrie.reduire([a, a, b, c, d, a, a]) == ([a, b, c, d], [0, 0, 1, 2, 3, 0, 0])
    presque = (4.0, 0.00001)
    assert geometrie.reduire([a, b, presque, c, d])[1] == [0, 1, 2, 3, 4]  # proche n'est pas confondu
    repris, _rangs = geometrie.reduire([a, b, c, a, d])  # point repris plus loin : distinct, donc non simple
    assert len(repris) == 5 and not geometrie.est_simple(repris)


@pytest.mark.parametrize("k", [0, 1, 7, 19, 40, 5])  # fermées, doublées, et une zone sans répétition
def test_retouche_deplace_ensemble_les_sommets_confondus_et_garde_la_structure(k):
    carte = cartes.decoder(CONTENU)
    zone = carte.zone_exacte(jardin_etendu.identifiant_zone(k))
    reduit, rangs = geometrie.reduire(zone.contour)
    operation = retouche.preparer_ajustement(carte, zone.identifiant, 2, 9, 10.0)
    avant, apres = operation.avant, operation.apres
    assert avant == zone.contour and len(apres) == len(avant)  # même nombre de points renvoyé au robot
    assert [i for i in range(len(avant)) if avant[i] == avant[i - 1]] == [i for i in range(len(apres)) if apres[i] == apres[i - 1]]
    assert geometrie.reduire(apres)[1] == rangs  # mêmes doublons, aux mêmes rangs
    deplaces = [i for i in range(len(avant)) if avant[i] != apres[i]]
    assert deplaces and all(2 <= i <= 9 for i in deplaces)
    assert operation.resume["sommets_confondus"] == len(avant) - len(reduit)
    assert ("déplacés ensemble" in " ".join(operation.resume["avertissements"])) == (len(avant) != len(reduit))


def test_portion_qui_passe_par_la_fermeture_deplace_les_deux_copies_du_premier_point():
    carte = cartes.decoder(CONTENU)
    zone = carte.zone_exacte(jardin_etendu.identifiant_zone(0))  # fermée : dernier sommet = premier
    dernier = len(zone.contour) - 1
    operation = retouche.preparer_ajustement(carte, zone.identifiant, dernier - 3, 3, 10.0, raccord_m=0.0)
    assert operation.apres[0] == operation.apres[dernier] != zone.contour[0]
    # Désigner le premier point par l'un ou l'autre de ses deux rangs revient au même.
    par_le_dernier = retouche.preparer_ajustement(carte, zone.identifiant, dernier, 4, 10.0)
    par_le_premier = retouche.preparer_ajustement(carte, zone.identifiant, 0, 4, 10.0)
    assert par_le_dernier.apres == par_le_premier.apres


def test_controle_refuse_de_desunir_des_sommets_confondus_et_tout_autre_cas():
    carte = cartes.decoder(CONTENU)
    zone = carte.zone_exacte(jardin_etendu.identifiant_zone(0))
    desuni = list(zone.contour)
    x, y = desuni[-1]
    desuni[-1] = (x + 0.05, y)
    with pytest.raises(RefusSecurite, match="sommets confondus du contour seraient désunis"):
        retouche.controler(carte, zone, desuni, None)
    with pytest.raises(RefusSecurite, match="nombre de sommets modifié"):
        retouche.controler(carte, zone, zone.contour[:-1], None)
    croise = dataclasses.replace(zone, contour=[(0.0, 0.0), (4.0, 3.0), (4.0, 0.0), (0.0, 3.0)])
    assert retouche.empechement(carte, croise) == "le contour actuel de la zone n'est pas un polygone simple"
    with pytest.raises(RefusSecurite, match="n'est pas un polygone simple"):
        retouche.controler(carte, croise, croise.contour, None)
    partielle = dataclasses.replace(carte, manques=("origine absente",))
    assert retouche.empechement(partielle, zone) == "carte incomplète (origine absente)"


def test_ecriture_d_une_zone_fermee_renvoie_la_meme_structure_au_robot():
    from lymow_pilotage.config import Calibration

    carte = cartes.decoder(CONTENU)
    zone = carte.zone_exacte(jardin_etendu.identifiant_zone(1))  # 599 sommets + la fermeture
    operation = retouche.preparer_ajustement(carte, zone.identifiant, 10, 30, 10.0)
    trame = protocole.trame_contour_zone(zone.identifiant, operation.apres, Calibration())
    envoyee = cartes.decoder(cartes.champs_stricts(trame.octets)[-1][2] + pb._field_bytes(4, b"")).zones[0]
    assert len(envoyee.contour) == 600 and envoyee.contour == operation.apres and envoyee.contour[0] == envoyee.contour[-1]


def test_liste_des_zones_nommees_d_abord_puis_par_surface_avec_la_possibilite_de_retouche():
    from lymow_pilotage import ui

    carte = cartes.decoder(CONTENU)
    liste = ui.liste_zones(carte)
    assert len(liste) == 55 and [ligne["nommee"] for ligne in liste] == [True] * 5 + [False] * 50
    noms = [ligne["libelle"].split(" — ")[0] for ligne in liste[:5]]
    assert noms == ["Zone E", "Zone fictive A", "Zone fictive B", "Zone fictive C", "Zone fictive D"]
    surfaces = [ligne["surface_m2"] for ligne in liste[5:]]
    assert surfaces == sorted(surfaces, reverse=True)
    assert all(" m², " in ligne["libelle"] and ligne["libelle"].endswith(" sommets") for ligne in liste)
    assert all(ligne["retouche_possible"] and ligne["motif"] is None for ligne in liste)
    assert sum(ligne["sommets_confondus"] > 0 for ligne in liste) == 28  # 25 fermées + 3 doublées
    partielle = dataclasses.replace(carte, manques=("station de charge absente",))
    assert {ligne["motif"] for ligne in ui.liste_zones(partielle)} == {"carte incomplète (station de charge absente)"}


def test_page_carte_donne_la_liste_triee_et_le_motif_par_zone(tmp_path, monkeypatch, session_reelle, lien):
    from test_ui import executer, poster, serveur_ui

    lien.carte = CONTENU

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            liste = corps["carte"]["liste"]
            assert statut == 200 and len(liste) == 55 and liste[0]["nommee"] and liste[0]["retouche_possible"] is True
            assert {ligne["identifiant"] for ligne in liste} == {zone["identifiant"] for zone in corps["carte"]["zones"]}
            fermee = jardin_etendu.identifiant_zone(0)
            statut, corps, _recus = await poster(
                http, serveur, "/api/carte/previsualiser", {"zone": fermee, "de": 2, "a": 9, "cm": 10, "cote": "court", "raccord": 0.5},
            )
            assert statut == 200 and "refus" not in corps and corps["resume"]["sommets_confondus"] == 1
            assert corps["apres"][0] == corps["apres"][-1]

    executer(scenario())


def test_page_carte_lisible_sur_un_grand_terrain():
    from lymow_pilotage import ui

    script = (ui.STATIQUE / "carte.js").read_text(encoding="utf-8")
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    for identifiant in ("carte-noms", "carte-robot-cadrer", "carte-recadrer", "carte-possible"):
        assert f'id="{identifiant}"' in page and f'"{identifiant}"' in script
    assert "Tout voir" in page and "Cadrer sur le robot" in page
    assert "dblclick" in script and "POLICE_PX" in script and "SOMMET_PX" in script and "carte.liste" in script
    assert "unite * 2.2" not in script  # plus de taille d'étiquette en unités de carte


def test_page_carte_refusee_repond_vite_avec_le_motif_exact(tmp_path, monkeypatch, session_reelle, lien):
    from test_ui import executer, poster, serveur_ui

    lien.carte, lien.index_ignore = CONTENU, True

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            debut = asyncio.get_running_loop().time()
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 422 and corps["erreur"].startswith("carte reçue mais refusée : ")
            assert asyncio.get_running_loop().time() - debut < 5
            lien.index_ignore = False
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 200 and len(corps["carte"]["zones"]) == 55 and corps["carte"]["complete"] is True

    executer(scenario())


def test_capture_signale_une_coupure_profonde(tmp_path):
    reponse = pb._field_bytes(23, pb._field_bytes(2, pb._field_i32(1, 2) + pb._field_bytes(3, CONTENU[:TAILLE_TRANCHE_CARTE])))
    assert capture.est_coupe(reponse) and not capture.est_coupe(pb._field_bytes(23, pb._field_bytes(2, pb._field_bytes(3, CONTENU))))
    ecoute = capture.Enregistreur(tmp_path / "c")
    ecoute.recevoir("/device/robot-test/pboutput", json.dumps({"message": base64.b64encode(reponse).decode()}))
    assert ecoute.resume()["protobuf_incomplets"] == 1
    assert "TRONQUÉ" in (tmp_path / "c" / "0001.vidage.txt").read_text(encoding="utf-8")


# --- capture réelle : locale, jamais dans le dépôt ---------------------------


def _captures_reelles() -> list[Path]:
    for parent in list(Path(__file__).resolve().parents)[1:6]:
        if (parent / "captures").is_dir():
            return sorted(fiche for fiche in (parent / "captures").glob("*/*.json"))
    return []


def test_capture_reelle_locale_tranches_et_invariants():
    tranches = []
    for fiche in _captures_reelles():
        donnees = json.loads(fiche.read_text(encoding="utf-8"))
        if "charge_b64" not in donnees:
            continue
        brut = pb.extract_raw_map_content(pb.unwrap_envelope(base64.b64decode(donnees["charge_b64"])))
        if brut:
            tranches.append((fiche.parent, brut))
    if not tranches:
        pytest.skip("aucune capture réelle sur ce poste (dossier captures/ absent)")
    for dossier in sorted({dossier for dossier, _brut in tranches}):
        morceaux = [brut for d, brut in tranches if d == dossier]
        assert all(len(morceau) == TAILLE_TRANCHE_CARTE for morceau in morceaux[:-1])
        octets = b"".join(morceaux)
        try:
            carte = cartes.decoder(octets)
        except cartes.CarteIllisible:
            # Tranche(s) sans la suite : coupée net, donc refusée ; les éléments entiers restent sains.
            assert len(morceaux[-1]) == TAILLE_TRANCHE_CARTE
            champs, arret = capture.decouper(octets)
            assert arret is None and champs[-1].annonce is not None and all(c.annonce is None for c in champs[:-1])
            entiers = b"".join(pb._field_bytes(c.numero, c.contenu) for c in champs[:-1])
            assert octets.startswith(entiers)
            carte = cartes.decoder(entiers)
            assert not carte.complete
        verifier_invariants(carte)
