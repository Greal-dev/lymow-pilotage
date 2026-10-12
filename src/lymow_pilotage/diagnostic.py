"""Erreurs annoncées par le robot, dites en français, et ce qu'elles impliquent pour le pilotage.

Rôle : traduire les codes d'erreur de choc, pare-chocs, soulèvement, inclinaison, roues et batterie
(libellés d'origine du socle ha-lymow), et dire la cause probable d'un pas sans déplacement.
Frontières : aucun ordre, aucune règle de sécurité ; un code non traduit garde son libellé d'origine.
Constat du 2026-10-12 (essai réel) : en statut « en erreur » (pare-chocs enfoncé), cinq « reculer 1 »
émis et acquittés n'ont produit aucun déplacement — le robot en erreur ignore le pilotage manuel.
"""

from __future__ import annotations

from typing import Any

from .vendor.ha_lymow import const as socle

ERREURS_FR: dict[int, str] = {
    1: "défaut d'entraînement d'une roue", 2: "roue en surchauffe", 3: "liaison avec une roue perdue",
    4: "température de la batterie", 5: "défaut de charge de la batterie", 6: "tension de la batterie",
    7: "premier capteur de soulèvement bloqué", 8: "second capteur de soulèvement bloqué",
    17: "robot au bord d'un vide", 18: "pente trop forte", 19: "roues qui patinent", 21: "robot bloqué",
    31: "batterie faible", 44: "pare-chocs enfoncé", 59: "capteur à ultrasons avant en défaut",
    60: "capteur à ultrasons arrière en défaut", 66: "robot bloqué (coincé)", 74: "pare-chocs déclenché dans un passage",
    75: "obstacle dans un passage", 76: "obstacle en suivi de bordure", 77: "bordure infranchissable",
    83: "patinage dans un passage", 84: "patinage en pente",
}
PHRASE_ERREUR = "En erreur, le robot ignore le pilotage manuel."
AVERTISSEMENT_PAS = "Robot en erreur : il ignorera probablement cet ordre."
CAUSE_ERREUR = "en erreur : ordre ignoré"
CAUSE_PATINAGE = "patinage possible"
CAUSE_POSE_RARE = "position transmise trop rarement : non vérifiable"


def libelle_erreur(code: int) -> str:
    """« 44 : pare-chocs enfoncé (Bumper stuck) » ; sans traduction : « 23 : « Map missing » »."""
    origine = socle.ERROR_DESCRIPTIONS.get(code)
    if origine is None:
        return f"{code} : code inconnu"
    traduit = ERREURS_FR.get(code)
    return f"{code} : {traduit} ({origine})" if traduit else f"{code} : « {origine} »"


def libelles_erreurs(codes: Any) -> list[str]:
    if not isinstance(codes, list):
        return []
    return [libelle_erreur(code) for code in codes if isinstance(code, int) and not isinstance(code, bool) and code]


def en_erreur(statut: int | None) -> bool:
    return statut == socle.WORK_STATUS_ERROR


def cause_d_immobilite(statut: int | None) -> str:
    """Un pas émis, acquitté, et une pose inchangée : la cause probable, selon le statut du robot."""
    return CAUSE_ERREUR if statut in socle.WORK_STATUS_ERROR_GROUP else CAUSE_PATINAGE
