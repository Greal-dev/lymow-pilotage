"""Le statut du robot est `robotStatus` (f1 de robotInfo) ; `workStatus` (f6) n'est que la tâche.

Constat du 2026-10-12, captures réelles d'ordres : f6 reste à 2 (tonte) pendant toute la tâche,
pause comprise. Juger f6 a fait refuser à tort le pilotage d'un robot en pause. Les trames de
ces tests ont la forme réelle : PbOutput → champ 5 (robotInfo) → champs 1 et 6.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import ARRET, fragment, lignes_journal

from lymow_pilotage import mouvement, securite
from lymow_pilotage.etat import Etat
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.session import EchecOrdre
from lymow_pilotage.vendor.ha_lymow import protocol as pb
from lymow_pilotage.vendor.ha_lymow.protocol import decode_pboutput

AVANCER = Ordre("avancer", 0.5)


def trame(f1: int | None, f6: int | None, batterie: int = 80) -> bytes:
    """PbOutput de la forme réelle : robotInfo {robotStatus(1), batterie(2), workStatus(6)}."""
    info = b"" if f1 is None else pb._field_i32(1, f1)
    info += pb._field_i32(2, batterie)
    info += b"" if f6 is None else pb._field_i32(6, f6)
    return pb._field_bytes(5, info)


def etat_decode(f1: int | None, f6: int | None) -> dict:
    return decode_pboutput(trame(f1, f6))


def lancer(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    ("f1", "f6", "statut", "libelle", "tache"),
    [
        (5, 1, 5, "en charge", "prêt, en attente d'ordre"),  # en charge
        (1, 1, 1, "prêt, en attente d'ordre", None),  # au repos : tâche identique, non répétée
        (3, 2, 3, "tonte en pause", "tonte en cours"),  # tâche en pause
        (6, 2, 6, "télécommande manuelle", "tonte en cours"),  # ~30 s après une trame de pilotage
        (2, 2, 2, "tonte en cours", None),
    ],
)
def test_statut_vivant_lu_dans_f1_et_tache_dans_f6(f1, f6, statut, libelle, tache):
    etat = Etat(en_ligne=True)
    etat.appliquer(etat_decode(f1, f6), 10.0)
    resume = etat.resume(10.0)
    assert (etat.statut, etat.tache, etat.recu_statut) == (statut, f6, 10.0)
    assert (resume["statut"], resume["statut_libelle"], resume["tache_libelle"]) == (statut, libelle, tache)


def test_f1_absent_statut_inconnu_jamais_de_repli_sur_f6():
    etat = Etat(en_ligne=True)
    etat.appliquer(etat_decode(None, 1), 10.0)
    assert etat.statut is None and etat.tache == 1 and etat.recu_statut is None
    assert etat.resume(10.0)["statut_libelle"] == "aucun état reçu"
    with pytest.raises(RefusSecurite, match=r"\(SEC-01\)$"):
        securite.verifier_lames_arret(etat.statut, etat.age_statut(10.0), etat.en_ligne)
    # Un statut connu, puis une trame sans f1 : le statut redevient inconnu, sa fraîcheur n'avance pas.
    etat.appliquer(etat_decode(3, 2), 20.0)
    etat.appliquer(etat_decode(None, 2), 30.0)
    assert etat.statut is None and etat.recu_statut == 20.0 and etat.tache == 2
    # Une trame sans robotInfo (configuration seule) ne touche pas au statut.
    etat.appliquer(etat_decode(3, 2), 40.0)
    etat.appliquer({"rcCutHeight": 60}, 50.0)
    assert etat.statut == 3 and etat.recu_statut == 40.0


@pytest.mark.parametrize(("f1", "f6"), [(3, 2), (6, 2), (1, 1), (5, 1)])
def test_sec01_mouvement_accepte_sur_le_statut_vivant_meme_si_la_tache_est_tonte(session, lien, f1, f6):
    lien.robot = etat_decode(f1, f6)
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER))
    assert rapport["emissions"] == 20 and lien.arrets == [ARRET] * 3
    assert rapport["verification"] == "non vérifiable"  # pas de pose dans ces trames


@pytest.mark.parametrize(
    ("f1", "f6", "motif"),
    [
        (2, 2, "le robot tond"),
        (4, 2, "le robot rentre à sa base"),
        (2, 1, "le robot tond"),  # f6 « prêt » ne rachète pas un f1 « tonte »
        (None, 1, "aucun état reçu"),  # f1 absent : refus, même avec f6 dans la liste blanche
        (None, 3, "aucun état reçu"),
        (13, 2, "arrêt d'urgence"),
    ],
)
def test_sec01_mouvement_refuse_sur_le_statut_vivant(session, lien, f1, f6, motif):
    lien.robot = etat_decode(f1, f6)
    with pytest.raises(RefusSecurite, match=motif) as refus:
        lancer(mouvement.executer_mouvement(session, AVANCER))
    assert str(refus.value).endswith("(SEC-01)") and lien.ordres == []


def test_sec01_surveillance_f1_passe_de_6_a_2_pendant_le_mouvement_arret(session, lien, tmp_path):
    lien.robot = etat_decode(6, 2)
    compte = []

    def sur_publication(lien_, octets):
        if octets != ARRET and b"\x52\x0a" in octets:
            compte.append(octets)
            if len(compte) == 5:  # la tonte reprend en plein mouvement : f1 passe à 2, f6 n'a jamais bougé
                lien_.session.recevoir_etat(etat_decode(2, 2))

    lien.sur_publication = sur_publication
    with pytest.raises(EchecOrdre, match="mouvement interrompu en cours de route : le robot tond"):
        lancer(mouvement.executer_mouvement(session, Ordre("avancer", 2.0)))
    assert len(compte) == 5 and lien.arrets == [ARRET] * 3  # arrêt immédiat, garanti
    assert "arret-emis" in [ligne["evenement"] for ligne in lignes_journal(tmp_path)]


def test_sec01_surveillance_ne_s_arrete_pas_quand_seul_f6_dit_tonte(session, lien):
    lien.robot = etat_decode(3, 2)

    def sur_publication(lien_, octets):
        if octets != ARRET and b"\x52\x0a" in octets:
            lien_.session.recevoir_etat(etat_decode(6, 2))  # télécommande, tâche de tonte toujours « en cours »

    lien.sur_publication = sur_publication
    rapport = lancer(mouvement.executer_mouvement(session, AVANCER))
    assert rapport["emissions"] == 20


def test_liste_blanche_inchangee_et_appliquee_au_statut_vivant():
    assert sorted(securite.STATUTS_LAMES_ARRET) == [0, 1, 3, 5, 6, 7, 12]
    assert fragment(statut=3, tache=2)["robotState"] == 3 and fragment(statut=3, tache=2)["workStatus"] == 2
    assert fragment(statut=1)["robotState"] == fragment(statut=1)["workStatus"] == 1


def test_reprendre_et_pause_jugent_aussi_le_statut_vivant(session, lien):
    lien.robot = etat_decode(3, 2)  # en pause, tâche de tonte : « reprendre » a quelque chose à reprendre
    assert lancer(mouvement.executer(session, Ordre("reprendre")))["codes_emis"] == [4]
    lien.robot = etat_decode(2, 2)
    lancer(session.solliciter())  # le robot annonce son nouvel état
    with pytest.raises(RefusSecurite, match="rien à reprendre"):
        lancer(mouvement.executer(session, Ordre("reprendre")))
    lien.robot = etat_decode(4, 2)  # retour à la base : la pause est le code 21
    lancer(session.solliciter())
    assert lancer(mouvement.executer(session, Ordre("pause")))["codes_emis"] == [21]


def test_precision_horizontale_affichee_et_libelle_de_qualite_non_confirme():
    etat = Etat(en_ligne=True)
    localisation = pb._field_i32(1, 10) + pb._field_f32(2, 0.014) + pb._field_f32(3, 0.02) + pb._field_i32(4, 1)
    etat.appliquer(decode_pboutput(trame(1, 1) + pb._field_bytes(6, localisation)), 1.0)
    assert etat.resume(1.0)["rtk"] == "précision horizontale 0.01 m — SINGLE_POINT (libellé non confirmé)"
    etat.appliquer(decode_pboutput(trame(1, 1) + pb._field_bytes(6, pb._field_i32(4, 3))), 2.0)
    assert "(libellé non confirmé)" in etat.resume(2.0)["rtk"]
    assert Etat().resume(0.0)["rtk"] is None
