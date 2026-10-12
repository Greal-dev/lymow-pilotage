"""Retouche du contour d'une zone de tonte : calcul, bornes, sauvegarde, écriture, vérification.

Rôle : porter SEC-08 (quand écrire), SEC-09 (sauvegarde avant écriture), SEC-10
(bornes de la retouche) et SEC-11 (relecture après écriture). Une zone par
opération, par le seul encodeur `protocole.trame_contour_zone`. Jamais d'envoi
de carte entière. La géométrie pure vit dans `geometrie`, le décodage dans `carte`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import carte as cartes
from . import geometrie, protocole, securite
from .carte import Carte, Zone
from .geometrie import Point
from .mouvement import ErreurSaisie
from .securite import RefusSecurite
from .session import EchecOrdre, Session

DOSSIER_SAUVEGARDES = "sauvegardes-carte"
DOSSIER_APERCUS = "apercus-carte"
APPLIQUE = "appliqué"
IGNORE = "ignoré par le robot"
DIFFERENCE = "DIFFÉRENCE INATTENDUE"
NON_VERIFIE = "non vérifié : la carte n'a pas pu être relue"
_NOMS_CHAMPS = {2: "zones interdites", 3: "passages", 4: "station de charge", 7: "origine", 8: "réglages de tâche",
                11: "réglages de tonte", 12: "réglages de passage", 13: "réglages en cours"}


@dataclass(frozen=True)
class Retouche:
    zone: str
    nom: str
    avant: list[Point] = field(repr=False)
    apres: list[Point] = field(repr=False)
    ecart_m: float | None  # None : restauration d'un contour sauvegardé
    description: str
    resume: dict[str, Any]


def _refus(motif: str) -> RefusSecurite:
    return RefusSecurite(f"retouche refusée : {motif} (SEC-10)")


def controler(carte: Carte, zone: Zone, apres: list[Point], ecart_m: float | None) -> dict[str, Any]:
    """SEC-10 : bornes dures. Un seul manquement lève, avec le motif exact. Rend les mesures."""
    avant = zone.contour
    borne = securite.AJUSTEMENT_MAX_M
    if ecart_m is not None:
        securite._nombre_fini(ecart_m, "écart")
        if ecart_m == 0 or abs(ecart_m) > borne:
            raise _refus(f"écart de {ecart_m * 100:g} cm hors bornes (jusqu'à {borne * 100:g} cm, non nul)")
    if len(avant) < 3 or not geometrie.est_simple(avant):
        raise _refus("le contour actuel de la zone n'est pas un polygone simple")
    if len(apres) != len(avant):
        raise _refus(f"nombre de sommets modifié ({len(avant)} avant, {len(apres)} après)")
    if any(geometrie.longueur(a, b) < 1e-4 for a, b in zip(apres, apres[1:] + apres[:1])):
        raise _refus("deux sommets consécutifs confondus")
    if not geometrie.est_simple(apres):
        raise _refus("le nouveau contour se recoupe lui-même")
    aire_avant, aire_apres = geometrie.aire_signee(avant), geometrie.aire_signee(apres)
    if abs(aire_apres) < 1e-6 or (aire_avant > 0) != (aire_apres > 0):
        raise _refus("sens de parcours inversé ou surface nulle")
    ecarts = [geometrie.longueur(a, b) for a, b in zip(avant, apres)]
    deplaces = sum(1 for e in ecarts if e > 1e-6)
    if deplaces == 0:
        raise _refus("aucun sommet déplacé (portion trop courte pour les raccords ?)")
    limite = (borne if ecart_m is None else abs(ecart_m)) + securite.TOLERANCE_CONTOUR_M
    if max(ecarts) > limite:
        raise _refus(f"un sommet se déplace de {max(ecarts) * 100:.1f} cm, plus que la borne de {limite * 100:.1f} cm")
    variation = abs(aire_apres) - abs(aire_avant)
    if ecart_m is not None and variation * ecart_m <= 0:
        raise _refus("la surface ne varie pas dans le sens de l'écart demandé")
    for interdite in carte.interdites:
        rattachee = interdite.parent == zone.identifiant
        if (rattachee or geometrie.strictement_dans(interdite.contour, avant)) and not geometrie.strictement_dans(
            interdite.contour, apres
        ):
            raise _refus(f"la zone interdite « {interdite.libelle} » ne resterait pas strictement à l'intérieur")
    for passage in carte.passages:
        lie = zone.identifiant in (passage.zone1, passage.zone2) or geometrie.touche(avant, passage.trace)
        if lie and passage.trace and not geometrie.touche(apres, passage.trace):
            raise _refus(f"le passage « {passage.identifiant} » ne toucherait plus la zone")
    if carte.station is not None:
        point = [carte.station[:2]]
        if geometrie.touche(avant, point) and not geometrie.touche(apres, point):
            raise _refus("la station de charge ne serait plus dans la zone")
    return {
        "sommets": len(avant), "sommets_deplaces": deplaces,
        "deplacement_max_cm": round(max(ecarts) * 100, 1),
        "surface_avant_m2": round(abs(aire_avant), 2), "surface_apres_m2": round(abs(aire_apres), 2),
        "variation_m2": round(variation, 2),
    }


def preparer_ajustement(
    carte: Carte, cle: str, de: int, a: int, cm: float, cote: str = "court", raccord_m: float | None = None
) -> Retouche:
    """Élargit (cm > 0) ou rogne (cm < 0) une bande le long d'une portion du contour d'UNE zone."""
    zone = carte.zone(cle)
    raccord = securite.RACCORD_DEFAUT_M if raccord_m is None else securite._nombre_fini(raccord_m, "raccord")
    if not 0 <= raccord <= securite.RACCORD_MAX_M:
        raise _refus(f"raccord hors bornes (0 à {securite.RACCORD_MAX_M:g} m)")
    ecart = securite._nombre_fini(cm, "écart") / 100.0
    if ecart == 0 or abs(ecart) > securite.AJUSTEMENT_MAX_M:
        raise _refus(f"écart de {cm:g} cm hors bornes (jusqu'à {securite.AJUSTEMENT_MAX_M * 100:g} cm, non nul)")
    try:
        indices = geometrie.portion(zone.contour, de, a, cote)
    except ValueError as erreur:
        raise ErreurSaisie(str(erreur)) from None
    apres = geometrie.en_f32(geometrie.ajuster(zone.contour, indices, ecart, raccord))
    mesures = controler(carte, zone, apres, ecart)
    avertissements = ["la bande suit les sommets existants : aux angles elle est plus mince que l'écart demandé"]
    if mesures["deplacement_max_cm"] < abs(cm) * 0.99:
        avertissements.append("raccords plus longs que la portion : le déplacement maximal reste sous l'écart demandé")
    sens = "élargir" if cm > 0 else "rogner"
    resume = {
        "zone": zone.libelle, "identifiant": zone.identifiant, "ecart_cm": cm, "action": sens,
        "portion": {"de": de, "a": a, "cote": cote, "sommets": len(indices)}, "raccord_m": raccord,
    } | mesures | {"avertissements": avertissements}
    description = f"ajuster-zone {zone.identifiant} --de {de} --a {a} --cm {cm:g} --cote {cote} --raccord {raccord:g}"
    return Retouche(zone.identifiant, zone.libelle, list(zone.contour), apres, ecart, description, resume)


def charger_sauvegarde(chemin: Path) -> Carte:
    """Fichier `.pb` d'une sauvegarde (ou son `.json` voisin) → carte telle qu'elle était."""
    brut = chemin.with_suffix(".pb")
    try:
        return cartes.decoder(brut.read_bytes())
    except OSError as erreur:
        raise ErreurSaisie(f"sauvegarde illisible ({type(erreur).__name__}) : {brut.name}") from None


def preparer_restauration(carte: Carte, sauvegarde: Carte, cle: str | None, origine: str) -> Retouche:
    """Remet l'ancien contour d'UNE zone, par le même encodeur et sous les mêmes bornes."""
    if cle is None:
        actuels = {z.identifiant: z.contour for z in carte.zones}
        differentes = [z for z in sauvegarde.zones if z.identifiant in actuels and z.contour != actuels[z.identifiant]]
        if len(differentes) != 1:
            raise ErreurSaisie("précisez la zone à restaurer avec --zone (aucune ou plusieurs zones diffèrent)")
        ancienne = differentes[0]
    else:
        ancienne = sauvegarde.zone(cle)
    zone = carte.zone(ancienne.identifiant)
    apres = geometrie.en_f32(ancienne.contour)
    mesures = controler(carte, zone, apres, None)
    resume = {"zone": zone.libelle, "identifiant": zone.identifiant, "restauration": origine} | mesures
    return Retouche(
        zone.identifiant, zone.libelle, list(zone.contour), apres, None,
        f"restaurer-zone {origine} --zone {zone.identifiant}", resume,
    )


def sauvegarder(carte: Carte, dossier: Path, zone: Zone, instant: datetime) -> Path:
    """SEC-09 : carte complète telle que reçue, octets bruts et JSON lisible ; lève si l'écriture échoue."""
    repertoire = dossier / DOSSIER_SAUVEGARDES
    repertoire.mkdir(parents=True, exist_ok=True)
    base = f"{instant:%Y-%m-%d_%H%M%S}_{zone.identifiant}"
    chemin, rang = repertoire / f"{base}.pb", 1
    while chemin.exists():
        rang += 1
        chemin = repertoire / f"{base}_{rang}.pb"
    chemin.write_bytes(carte.octets)
    lisible = {"date": instant.isoformat(timespec="seconds"), "zone_retouchee": zone.identifiant} | cartes.vers_json(carte)
    chemin.with_suffix(".json").write_text(json.dumps(lisible, ensure_ascii=False, indent=1), encoding="utf-8")
    if chemin.read_bytes() != carte.octets:
        raise OSError("sauvegarde relue différente de la carte reçue")
    return chemin


def ecrire_apercu(retouche: Retouche, dossier: Path, instant: datetime) -> Path:
    """Aperçu de simulation : ancien contour (gris) et nouveau (rouge), nord en haut."""
    points = retouche.avant + retouche.apres
    xs, ys = [p[0] for p in points], [-p[1] for p in points]
    marge = 1.0
    boite = f"{min(xs) - marge:.2f} {min(ys) - marge:.2f} {max(xs) - min(xs) + 2 * marge:.2f} {max(ys) - min(ys) + 2 * marge:.2f}"

    def trace(contour: list[Point], couleur: str) -> str:
        sommets = " ".join(f"{x:.3f},{-y:.3f}" for x, y in contour)
        return f'<polygon points="{sommets}" fill="none" stroke="{couleur}" stroke-width="0.04"/>'

    repertoire = dossier / DOSSIER_APERCUS
    repertoire.mkdir(parents=True, exist_ok=True)
    chemin = repertoire / f"{instant:%Y-%m-%d_%H%M%S}_{retouche.zone}.svg"
    chemin.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{boite}">'
        f'{trace(retouche.avant, "#888888")}{trace(retouche.apres, "#cc2222")}</svg>\n',
        encoding="utf-8",
    )
    return chemin


async def _exiger_repos(session: Session) -> None:
    """SEC-08 : état reçu dans la session après sollicitation, robot en ligne, au repos ou en charge."""
    if not session.lien.connecte:
        raise EchecOrdre("MQTT non connecté : rien n'a été émis")
    instant = await session.solliciter()
    if instant is None:
        raise RefusSecurite("sollicitation d'état impossible : écriture de carte refusée (SEC-08)")
    await session.attendre("statut", instant, 75.0)
    etat, maintenant = session.etat, session.horloge()
    frais = etat.recu_statut is not None and etat.recu_statut > instant
    securite.verifier_repos_pour_carte(
        etat.statut if frais else None, etat.age_statut(maintenant) if frais else None, etat.en_ligne
    )


def _memes(a: list[Point], b: list[Point]) -> bool:
    return len(a) == len(b) and all(geometrie.longueur(p, q) <= securite.TOLERANCE_CONTOUR_M for p, q in zip(a, b))


def comparer(avant: Carte, apres: Carte, retouche: Retouche) -> tuple[str, list[str]]:
    """SEC-11 : le contour relu est-il celui envoyé, et tout le reste est-il intact ?"""
    differences: list[str] = []
    anciennes = {z.identifiant: z for z in avant.zones}
    nouvelles = {z.identifiant: z for z in apres.zones}
    relue = nouvelles.get(retouche.zone)
    if relue is None:
        return DIFFERENCE, ["la zone retouchée a disparu de la carte"]
    applique, inchange = _memes(relue.contour, retouche.apres), _memes(relue.contour, retouche.avant)
    if not applique and not inchange:
        differences.append("le contour relu n'est ni l'ancien ni celui envoyé")
    if relue.hors_contour() != anciennes[retouche.zone].hors_contour():
        differences.append("le nom ou les réglages de la zone ont changé")
    for identifiant in sorted(set(anciennes) | set(nouvelles)):
        if identifiant == retouche.zone:
            continue
        ancienne, nouvelle = anciennes.get(identifiant), nouvelles.get(identifiant)
        if ancienne is None or nouvelle is None or ancienne.brut != nouvelle.brut:
            differences.append(f"une autre zone a changé : {identifiant}")
    stables_avant, stables_apres = avant.stables(), apres.stables()
    for numero in cartes.CHAMPS_STABLES:
        if [c for c in stables_avant if c[0] == numero] != [c for c in stables_apres if c[0] == numero]:
            differences.append(f"changement inattendu : {_NOMS_CHAMPS.get(numero, f'champ {numero}')}")
    if differences:
        return DIFFERENCE, differences
    return (APPLIQUE if applique else IGNORE), []


async def emettre(session: Session, retouche: Retouche, dossier: Path) -> tuple[dict[str, Any], int]:
    """Écriture réelle d'une retouche. L'appelant garantit qu'aucun autre ordre n'est en cours (SEC-08)."""
    await _exiger_repos(session)  # SEC-08
    # SEC-09 : carte de moins de 60 s, sinon redemandée ; sans carte ni sauvegarde, pas d'écriture.
    carte = cartes.en_memoire(session)
    if carte is None or session.horloge() - carte.recue > securite.AGE_MAX_CARTE_S:
        carte = await cartes.demander(session, securite.ATTENTE_CARTE_S)
    zone = carte.zone(retouche.zone)
    if geometrie.en_f32(zone.contour) != geometrie.en_f32(retouche.avant):
        raise RefusSecurite("la carte a changé depuis la prévisualisation : prévisualisez de nouveau (SEC-09)")
    controler(carte, zone, retouche.apres, retouche.ecart_m)  # SEC-10, de nouveau, sur la carte fraîche
    try:
        sauvegarde = sauvegarder(carte, dossier, zone, datetime.now())
    except Exception as erreur:  # noqa: BLE001 — sans sauvegarde relue, aucune écriture
        raise RefusSecurite(
            f"sauvegarde de la carte impossible ({type(erreur).__name__}) : écriture refusée (SEC-09)"
        ) from None
    trame = protocole.trame_contour_zone(zone.identifiant, retouche.apres, session.cal)
    restauration = f'lymow restaurer-zone "{sauvegarde}" --zone {zone.identifiant} --reel'
    session.journal.ecrire(  # SEC-06 : sans trace possible, pas d'écriture
        "carte-ecriture", strict=True, ordre=retouche.description, zone=zone.identifiant,
        resume=retouche.resume, sauvegarde=str(sauvegarde), trame=trame.champs,
    )
    await session.publier(trame, securite.DELAI_ARRET_S)
    # SEC-11 : relecture et comparaison.
    try:
        await session.dormir(1.0)
        verdict, differences = comparer(carte, await cartes.demander(session, securite.ATTENTE_CARTE_S), retouche)
    except EchecOrdre:
        verdict, differences = NON_VERIFIE, []
    session.journal.ecrire("carte-verdict", zone=zone.identifiant, verdict=verdict, differences=differences)
    if verdict in (DIFFERENCE, NON_VERIFIE):
        session.signaler(f"{verdict} après l'écriture de la zone « {zone.libelle} ». Sauvegarde : {sauvegarde}")
        session.signaler(f"Pour revenir en arrière : {restauration}")
    bilan = {"mode": "réel", "ordre": retouche.description, "verdict": verdict, "differences": differences,
             "sauvegarde": str(sauvegarde), "restauration": restauration} | retouche.resume
    return bilan, 0 if verdict == APPLIQUE else 1
