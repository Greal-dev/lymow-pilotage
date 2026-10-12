"""Coffre : un jeton plus long que la limite de Windows doit être enregistré et relu intact."""

from __future__ import annotations

import sys
import types

import pytest

from lymow_pilotage import coffre
from lymow_pilotage.coffre import ecrire, effacer, lire  # fonctions réelles, avant les doubles de conftest

LIMITE_WINDOWS = 1280


class _ErreurSuppression(Exception):
    pass


@pytest.fixture
def faux_keyring(monkeypatch):
    """Gestionnaire d'identification simulé, avec la limite de taille réelle de Windows."""
    entrees: dict[tuple[str, str], str] = {}

    def set_password(service: str, cle: str, valeur: str) -> None:
        if len(valeur) > LIMITE_WINDOWS:
            raise OSError(1783, "CredWrite")
        entrees[(service, cle)] = valeur

    def delete_password(service: str, cle: str) -> None:
        if (service, cle) not in entrees:
            raise _ErreurSuppression(cle)
        del entrees[(service, cle)]

    module = types.ModuleType("keyring")
    module.set_password = set_password
    module.get_password = lambda service, cle: entrees.get((service, cle))
    module.delete_password = delete_password
    erreurs = types.ModuleType("keyring.errors")
    erreurs.PasswordDeleteError = _ErreurSuppression
    module.errors = erreurs
    monkeypatch.setitem(sys.modules, "keyring", module)
    monkeypatch.setitem(sys.modules, "keyring.errors", erreurs)
    return entrees


def test_jeton_plus_long_que_la_limite_windows_est_enregistre_et_relu(faux_keyring):
    jeton = "".join(chr(97 + rang % 26) for rang in range(2345))
    ecrire(coffre.JETON, jeton)
    assert lire(coffre.JETON) == jeton
    assert all(len(valeur) <= LIMITE_WINDOWS for valeur in faux_keyring.values())


def test_valeur_courte_reste_dans_une_seule_entree(faux_keyring):
    ecrire(coffre.REGION, "eu-west-1")
    assert faux_keyring == {(coffre.SERVICE, coffre.REGION): "eu-west-1"}
    assert lire(coffre.REGION) == "eu-west-1"


def test_reecriture_plus_courte_ne_laisse_aucun_ancien_morceau(faux_keyring):
    ecrire(coffre.JETON, "a" * 3500)
    ecrire(coffre.JETON, "b" * 1500)
    assert lire(coffre.JETON) == "b" * 1500
    assert len(faux_keyring) == 3  # entrée principale + 2 morceaux
    ecrire(coffre.JETON, "c" * 10)
    assert faux_keyring == {(coffre.SERVICE, coffre.JETON): "c" * 10}


def test_morceau_manquant_donne_aucun_jeton_plutot_qu_un_jeton_tronque(faux_keyring):
    ecrire(coffre.JETON, "a" * 2500)
    del faux_keyring[(coffre.SERVICE, f"{coffre.JETON}#2")]
    assert lire(coffre.JETON) is None


def test_effacer_retire_aussi_les_morceaux(faux_keyring):
    ecrire(coffre.JETON, "a" * 2500)
    ecrire(coffre.REGION, "eu-west-1")
    effacer()
    assert faux_keyring == {}
