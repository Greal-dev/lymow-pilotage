"""Pilotage fluide (décision du propriétaire, 2026-10-12) : la main n'est retirée que pour la sécurité ou sur demande.

Voir aussi : test_erreur_robot (patinage répété), test_fleches (inactivité de 5 min, file de 8), test_recalage
(liaison lente), test_avertissement_carte (refus de carte).
"""

from __future__ import annotations

import time

from conftest import ARRET, est_consigne
from test_demo import serveur_demo
from test_ui import attendre, executer, poster

from lymow_pilotage import fleches


def test_constantes_du_pilotage_fluide():
    assert fleches.DUREE_ARMEMENT_S == 300.0  # inactivité : cinq minutes
    assert fleches.FILE_MAX == 8  # appuis empilés pendant un pas


def test_robot_fictif_moins_d_une_seconde_et_demie_entre_deux_pas_de_la_file(tmp_path, monkeypatch):
    async def scenario():
        async with serveur_demo(tmp_path, monkeypatch) as (serveur, http):
            robot = serveur.session.lien
            instants: list[tuple[float, str]] = []
            publier = robot.publier

            async def espion(sujet, charge, *, delai):
                avant = len(robot.recues)
                await publier(sujet, charge, delai=delai)
                for octets in robot.recues[avant:]:
                    if est_consigne(octets) or octets == ARRET:
                        instants.append((time.monotonic(), "arret" if octets == ARRET else "consigne"))

            robot.publier = espion
            _statut, corps, _recus = await poster(http, serveur, "/api/direct/armer", {"confirmer": True})
            jeton = corps["jeton"]
            for direction, pas in (("haut", 0.25), ("gauche", 15), ("bas", 0.25), ("droite", 15)):  # empilés d'un coup
                statut, corps, _recus = await poster(http, serveur, "/api/direct/fleche", {"jeton": jeton, "direction": direction, "pas": pas})
                assert statut == 202
                jeton = corps["jeton"]
            await attendre(lambda: not serveur.occupe and len(serveur.direct.faites) == 4, delai=60.0)
            morts, dernier_arret = [], None
            for instant, nature in instants:
                if nature == "arret":
                    dernier_arret = instant
                elif dernier_arret is not None:
                    morts.append(instant - dernier_arret)
                    dernier_arret = None
            # Le pas suivant part dès l'arrêt acquitté et la première trame fraîche : aucune attente fixe.
            assert len(morts) == 3 and max(morts) < 1.5, [round(m, 2) for m in morts]
            assert serveur.direct.arme is True

    executer(scenario())
