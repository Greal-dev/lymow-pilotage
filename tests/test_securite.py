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


@pytest.mark.parametrize("statut", [0, 1, 3, 5, 6, 7, 12])
def test_sec01_statuts_lames_a_l_arret_acceptes_dont_pause_et_erreur(statut):
    securite.verifier_lames_arret(statut, 10.0, True)
    securite.verifier_presence_et_statut(statut, True)  # même liste pendant le mouvement


@pytest.mark.parametrize("statut", [-1, 2, 4, 8, 9, 10, 11, 13, 14, 15, 16, 17, 99])
def test_sec01_tonte_reprise_zone_retour_urgence_et_inconnu_refuses(statut):
    with pytest.raises(RefusSecurite, match="SEC-01"):
        securite.verifier_lames_arret(statut, 10.0, True)
    with pytest.raises(RefusSecurite, match="SEC-01"):
        securite.verifier_presence_et_statut(statut, True)


def test_sec01_liste_blanche_exacte_et_sans_tonte_ni_retour_ni_arret_d_urgence():
    assert securite.STATUTS_LAMES_ARRET == {0, 1, 3, 5, 6, 7, 12}
    assert not securite.STATUTS_LAMES_ARRET & socle.WORK_STATUS_MOWING_GROUP
    assert not securite.STATUTS_LAMES_ARRET & socle.WORK_STATUS_RETURNING_GROUP
    assert socle.WORK_STATUS_EMERGENCY_STOP not in securite.STATUTS_LAMES_ARRET


@pytest.mark.parametrize("age", [None, 30.1, 90.0, 3600.0, -1.0, math.nan])
def test_sec01_etat_absent_ou_perime_refuse(age):
    with pytest.raises(RefusSecurite, match="SEC-01"):
        securite.verifier_lames_arret(1, age, True)


def test_sec01_etat_de_trente_secondes_au_plus_accepte():
    securite.verifier_lames_arret(1, 30.0, True)


def test_sec01_statut_absent_ou_robot_hors_ligne_refuse():
    with pytest.raises(RefusSecurite):
        securite.verifier_lames_arret(None, 1.0, True)
    with pytest.raises(RefusSecurite, match="hors ligne"):
        securite.verifier_lames_arret(1, 1.0, False)


def test_sec01_presence_inconnue_refusee_il_faut_un_vrai_explicite():
    with pytest.raises(RefusSecurite, match="non confirmée"):
        securite.verifier_lames_arret(1, 1.0, None)


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


@pytest.mark.parametrize(("metres", "facteur"), [(2.0, 0.5), (1.5, 0.5), (1.01, 0.5), (-1.5, 0.5), (2.0, 0.99)])
def test_sec02_deplacement_commande_plafonne_quel_que_soit_le_facteur(metres, facteur):
    # facteur 0.5 + « avancer 1.5 » commanderait 3 m (12 s à 0,25 m/s) : refus
    with pytest.raises(RefusSecurite, match="commandé.*SEC-02"):
        securite.consigne_translation(metres, facteur_distance=facteur)


@pytest.mark.parametrize("degres", [180.0, 120.0, -100.0])
def test_sec02_rotation_commandee_plafonnee_quel_que_soit_le_facteur(degres):
    with pytest.raises(RefusSecurite, match="commandé"):
        securite.consigne_rotation(degres, facteur_angle=0.5)


@pytest.mark.parametrize("facteur", [0.5, 1.0, 2.0])
def test_sec02_vitesse_fois_duree_ne_depasse_jamais_les_plafonds(facteur):
    for metres in (0.1, 0.5, 1.0, 2.0):
        try:
            c = securite.consigne_translation(metres, facteur)
        except RefusSecurite:
            continue
        assert abs(c.lineaire) * c.duree <= securite.DISTANCE_MAX_M + 1e-9
        assert c.duree <= securite.DUREE_MAX_S
    for degres in (10.0, 90.0, 180.0):
        try:
            c = securite.consigne_rotation(degres, facteur)
        except RefusSecurite:
            continue
        assert math.degrees(abs(c.angulaire) * c.duree) <= securite.ANGLE_MAX_DEG + 1e-6


def test_sec02_facteur_dans_les_bornes_allonge_la_duree_sans_depasser():
    assert securite.consigne_translation(1.0, facteur_distance=0.5).duree == 8.0  # 2 m commandés


@pytest.mark.parametrize("facteur", [0.0, -1.0, math.nan, math.inf])
def test_sec02_facteur_invalide_refuse(facteur):
    with pytest.raises(RefusSecurite):
        securite.consigne_translation(1.0, facteur_distance=facteur)


def test_sec02_mouvement_qui_ne_donnerait_aucune_emission_refuse():
    with pytest.raises(RefusSecurite, match="trop court"):
        securite.consigne_rotation(0.5)  # 0,025 s : moins d'une période
    with pytest.raises(RefusSecurite, match="trop court"):
        securite.consigne_translation(0.01)
    assert securite.consigne_translation(0.03).duree >= 1 / securite.CADENCE_HZ


@pytest.mark.parametrize("millimetres", [20, 40, 79, 101])
def test_sec02_plateau_haut_borne_au_haut_de_la_course(millimetres):
    with pytest.raises(RefusSecurite):
        protocole.trame_hauteur(millimetres, Calibration())


def test_sec02_plateau_haut_accepte_de_80_a_100():
    assert protocole.trame_hauteur(80, Calibration()).champs["hauteur_coupe_mm"] == 80
    assert protocole.trame_hauteur(100, Calibration()).champs["hauteur_coupe_mm"] == 100


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
