"""Grande carte, livrée par le robot en tranches de 81 920 octets (constat du 2026-10-12).

Jeu d'essai : `jardin_etendu`, entièrement fictif, de la forme de la carte réelle. La capture
réelle, elle, n'est jamais dans le dépôt : le dernier test ne tourne que si elle est présente.
"""

from __future__ import annotations

import asyncio
import base64
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
        "contours de zone non simples": sum(not geometrie.est_simple(geometrie.sans_doublons(c)) for c in zones.values()),
        "zones à surface négative": sum(geometrie.aire_signee(c) <= 0 for c in zones.values()),
        "zones interdites non simples": sum(
            not geometrie.est_simple(geometrie.sans_doublons(z.contour)) for z in carte.interdites
        ),
        "zones interdites sans zone de la carte": sum(z.parent not in zones for z in carte.interdites),
        "zones interdites qui ne touchent pas leur zone": sum(
            z.parent in zones and not geometrie.touche(zones[z.parent], z.contour) for z in carte.interdites
        ),
        "passages qui ne touchent pas leurs zones": sum(
            not all(i in zones and geometrie.touche(zones[i], p.trace) for i in (p.zone1, p.zone2))
            for p in carte.passages
        ),
    }
    assert {motif: n for motif, n in fautes.items() if n} == {}


def test_jeu_d_essai_de_la_forme_de_la_carte_reelle():
    assert TAILLE_TRANCHE_CARTE < len(CONTENU) < 2 * TAILLE_TRANCHE_CARTE
    carte = cartes.decoder(CONTENU)
    assert (len(carte.zones), len(carte.interdites), len(carte.passages)) == (55, 19, 54) and carte.complete
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


def test_retouche_d_un_contour_a_sommets_repetes_refusee_avec_le_motif():
    carte = cartes.decoder(CONTENU)
    fermee = carte.zone_exacte(jardin_etendu.identifiant_zone(0))
    with pytest.raises(RefusSecurite, match="répète des sommets"):
        retouche.preparer_ajustement(carte, fermee.identifiant, 2, 6, 10.0)
    ouverte = carte.zone_exacte(jardin_etendu.identifiant_zone(5))  # ni fermée ni doublée
    operation = retouche.preparer_ajustement(carte, ouverte.identifiant, 2, 6, 10.0)
    assert operation.resume["sommets_deplaces"] >= 1


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
