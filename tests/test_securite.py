"""SEC-01 (lames) et SEC-02 (plafonds), au niveau des règles pures."""

from __future__ import annotations

import math

import pytest

from lymow_pilotage import protocole, securite
from lymow_pilotage.config import Calibration
from lymow_pilotage.mouvement import Ordre, consigne_de
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.vendor.ha_lymow import const as socle

PAS_FINIS = [math.nan, math.inf, -math.inf]


# --- SEC-01 -----------------------------------------------------------------


@pytest.mark.parametrize("statut", [0, 1, 5, 6, 12])
def test_sec01_statuts_lames_a_l_arret_acceptes(statut):
    securite.verifier_lames_arret(statut, 10.0, True)


@pytest.mark.parametrize("statut", [-1, 2, 3, 4, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 99])
def test_sec01_tonte_bordure_et_statut_inconnu_refuses(statut):
    with pytest.raises(RefusSecurite, match="SEC-01"):
        securite.verifier_lames_arret(statut, 10.0, True)


def test_sec01_liste_blanche_exclut_tout_le_groupe_de_tonte_du_socle():
    assert not securite.STATUTS_LAMES_ARRET & socle.WORK_STATUS_MOWING_GROUP
    assert not securite.STATUTS_LAMES_ARRET & socle.WORK_STATUS_RETURNING_GROUP
    assert not securite.STATUTS_LAMES_ARRET & socle.WORK_STATUS_ERROR_GROUP


@pytest.mark.parametrize("age", [None, 90.1, 3600.0, -1.0, math.nan])
def test_sec01_etat_absent_ou_perime_refuse(age):
    with pytest.raises(RefusSecurite, match="SEC-01"):
        securite.verifier_lames_arret(1, age, True)


def test_sec01_statut_absent_ou_robot_hors_ligne_refuse():
    with pytest.raises(RefusSecurite):
        securite.verifier_lames_arret(None, 1.0, True)
    with pytest.raises(RefusSecurite, match="hors ligne"):
        securite.verifier_lames_arret(1, 1.0, False)


# --- SEC-02 -----------------------------------------------------------------


def test_sec02_vitesses_sous_les_plafonds_du_contrat_et_du_socle():
    assert securite.VITESSE_LINEAIRE_M_S <= 0.25 < socle.BLE_DRIVE_LINEAR_MAX
    assert securite.VITESSE_ANGULAIRE_RAD_S <= 0.35 < socle.BLE_DRIVE_ANGULAR_MAX
    assert 5 <= securite.CADENCE_HZ <= 10
    assert securite.REPETITIONS_ARRET == 3


def test_sec02_translation_au_plafond():
    consigne = securite.consigne_translation(2.0)
    assert (consigne.lineaire, consigne.angulaire, consigne.duree) == (0.25, 0.0, 8.0)
    assert securite.consigne_translation(-1.0).lineaire == -0.25


@pytest.mark.parametrize("metres", [2.01, -2.01, 50.0, 0.0, *PAS_FINIS])
def test_sec02_translation_refusee(metres):
    with pytest.raises(RefusSecurite):
        securite.consigne_translation(metres)


def test_sec02_rotation_au_plafond_et_sens():
    gauche = securite.consigne_rotation(180.0)
    assert gauche.angulaire == 0.35 and gauche.lineaire == 0.0
    assert gauche.duree == pytest.approx(math.pi / 0.35)
    assert gauche.duree <= securite.DUREE_MAX_S
    assert securite.consigne_rotation(-90.0).angulaire == -0.35
    assert securite.consigne_rotation(90.0, signe=-1).angulaire == -0.35


@pytest.mark.parametrize("degres", [180.1, -181.0, 720.0, 0.0, *PAS_FINIS])
def test_sec02_rotation_refusee(degres):
    with pytest.raises(RefusSecurite):
        securite.consigne_rotation(degres)


def test_sec02_duree_bornee_meme_avec_un_facteur_de_calibration():
    with pytest.raises(RefusSecurite, match="durée"):
        securite.consigne_translation(2.0, facteur_distance=0.5)  # 16 s > 12 s


@pytest.mark.parametrize("valeur", PAS_FINIS)
def test_sec02_encodeur_refuse_nan_et_infini(valeur):
    with pytest.raises(RefusSecurite):
        protocole.encode_remote_control(valeur, 0.0)
    with pytest.raises(RefusSecurite):
        protocole.encode_remote_control(0.0, valeur)


@pytest.mark.parametrize(("lineaire", "angulaire"), [(0.26, 0.0), (-0.5, 0.0), (0.0, 0.36), (0.0, -0.6)])
def test_sec02_encodeur_refuse_une_vitesse_au_dessus_du_plafond(lineaire, angulaire):
    with pytest.raises(RefusSecurite):
        protocole.encode_remote_control(lineaire, angulaire)


@pytest.mark.parametrize("ordre", [Ordre("avancer", -1.0), Ordre("reculer", -1.0), Ordre("avancer", math.nan)])
def test_sec02_avancer_et_reculer_exigent_une_valeur_positive(ordre):
    with pytest.raises(RefusSecurite):
        consigne_de(ordre, Calibration())


def test_reculer_donne_une_vitesse_negative():
    assert consigne_de(Ordre("reculer", 1.0), Calibration()).lineaire == -0.25
