"""Retouche du contour d'une zone de tonte : calcul, bornes, sauvegarde, écriture, vérification.

Rôle : porter SEC-08 (quand écrire), SEC-09 (carte complète, lue deux fois,
sauvegardée avant écriture), SEC-10 (bornes de la retouche, dérive cumulée) et
SEC-11 (relectures après écriture). Une zone par opération, par le seul encodeur
`protocole.trame_contour_zone`. Jamais d'envoi de carte entière. La géométrie
pure vit dans `geometrie`, le décodage dans `carte`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import struct
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
DOSSIER_DEMONSTRATION = "demonstration"  # sous-dossier : jamais mêlé aux sauvegardes d'un vrai robot
DOSSIER_APERCUS = "apercus-carte"
APPLIQUE = "appliqué"
NON_CONFIRME = (
    "non confirmé (le robot n'a pas encore renvoyé le nouveau contour — ne relancez pas, relisez la carte)"
)
DIFFERENCE = "DIFFÉRENCE INATTENDUE"
NON_VERIFIE = "non vérifié"
_NOMS_CHAMPS = {2: "zones interdites", 3: "passages", 4: "station de charge", 7: "origine", 8: "réglages de tâche",
                11: "réglages de tonte", 12: "réglages de passage", 13: "réglages en cours"}
# Identifiants venus du nuage : jeu de caractères fermé AVANT tout usage dans un nom de fichier.
_IDENTIFIANT = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class Retouche:
    zone: str
    nom: str
    avant: list[Point] = field(repr=False)
    apres: list[Point] = field(repr=False)
    ecart_m: float | None  # None : restauration d'un contour sauvegardé
    description: str
    resume: dict[str, Any]
    carte: str = ""  # empreinte de la carte sur laquelle la retouche a été calculée
    nouvelle_reference: bool = False

    @property
    def empreinte(self) -> str:
        """Courte, à recopier dans `--confirmer` : carte lue, zone, paramètres et nouveau contour."""
        condense = hashlib.sha256(f"{self.carte}|{self.zone}|{self.description}|{self.nouvelle_reference}|".encode())
        for x, y in self.apres:
            condense.update(struct.pack("<ff", x, y))
        return condense.hexdigest()[:12]


def _refus(motif: str) -> RefusSecurite:
    return RefusSecurite(f"retouche refusée : {motif} (SEC-10)")


def identifiant_sur(identifiant: str) -> str:
    if not _IDENTIFIANT.fullmatch(identifiant):
        raise RefusSecurite("identifiant de zone ou d'appareil hors du jeu de caractères admis : opération refusée")
    return identifiant


def exiger_complete(carte: Carte) -> None:
    """SEC-09 : une carte partielle s'affiche, mais ne sert jamais de base à une écriture."""
    if not carte.complete:
        raise RefusSecurite(f"carte incomplète ({' ; '.join(carte.manques)}) : aucune retouche possible (SEC-09)")


def controler(carte: Carte, zone: Zone, apres: list[Point], ecart_m: float | None) -> dict[str, Any]:
    """SEC-10 : bornes dures. Un seul manquement lève, avec le motif exact. Rend les mesures."""
    exiger_complete(carte)
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
        if not (rattachee or geometrie.strictement_dans(interdite.contour, avant)):
            continue
        if not geometrie.strictement_dans(interdite.contour, apres):
            raise _refus(f"la zone interdite « {interdite.libelle} » ne resterait pas strictement à l'intérieur")
        marge = geometrie.distance_contours(interdite.contour, apres)
        if marge < securite.DEGAGEMENT_INTERDITE_M:
            raise _refus(
                f"la zone interdite « {interdite.libelle} » serait à {marge * 100:.1f} cm du contour "
                f"(dégagement minimal : {securite.DEGAGEMENT_INTERDITE_M * 100:g} cm)"
            )
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
    carte: Carte, cle: str, de: int, a: int, cm: float, cote: str = "court", raccord_m: float | None = None,
    nouvelle_reference: bool = False,
) -> Retouche:
    """Élargit (cm > 0) ou rogne (cm < 0) une bande le long d'une portion du contour d'UNE zone."""
    exiger_complete(carte)
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
    return Retouche(
        zone.identifiant, zone.libelle, list(zone.contour), apres, ecart, description, resume,
        carte.empreinte(), nouvelle_reference,
    )


# --- sauvegardes locales (SEC-09) et référence cumulée (SEC-10) --------------------


def _repertoire(dossier: Path, demonstration: bool) -> Path:
    base = dossier / DOSSIER_SAUVEGARDES
    return base / DOSSIER_DEMONSTRATION if demonstration else base


def sauvegarder(
    carte: Carte, dossier: Path, zone: Zone, instant: datetime, appareil: str,
    *, reference: bool = False, demonstration: bool = False,
) -> Path:
    """SEC-09 : carte complète telle que reçue, octets bruts et JSON lisible ; lève si l'écriture échoue."""
    identifiant_sur(zone.identifiant)
    repertoire = _repertoire(dossier, demonstration)
    repertoire.mkdir(parents=True, exist_ok=True)
    base = f"{instant:%Y-%m-%d_%H%M%S}_{zone.identifiant}"
    chemin, rang = repertoire / f"{base}.pb", 1
    while chemin.exists():
        rang += 1
        chemin = repertoire / f"{base}_{rang}.pb"
    chemin.write_bytes(carte.octets)
    lisible = {
        "date": instant.isoformat(timespec="microseconds"), "appareil": appareil, "zone_retouchee": zone.identifiant,
        "reference": reference, "demonstration": demonstration,
    } | cartes.vers_json(carte)
    chemin.with_suffix(".json").write_text(json.dumps(lisible, ensure_ascii=False, indent=1), encoding="utf-8")
    if chemin.read_bytes() != carte.octets:
        raise OSError("sauvegarde relue différente de la carte reçue")
    return chemin


def charger_sauvegarde(chemin: Path, appareil: str, *, demonstration: bool = False) -> Carte:
    """Sauvegarde validée : même appareil, structure cohérente, horodatage lisible, même nature (réelle ou démo)."""
    brut, lisible = chemin.with_suffix(".pb"), chemin.with_suffix(".json")
    try:
        meta = json.loads(lisible.read_text(encoding="utf-8"))
        carte = cartes.decoder(brut.read_bytes())
        datetime.fromisoformat(meta["date"])
        zones = [z["identifiant"] for z in meta["zones"]]
    except (OSError, ValueError, KeyError, TypeError) as erreur:
        raise ErreurSaisie(f"sauvegarde illisible ou incomplète ({type(erreur).__name__}) : {brut.name}") from None
    if meta.get("appareil") != appareil:
        raise RefusSecurite("cette sauvegarde vient d'un autre appareil : restauration refusée")
    if bool(meta.get("demonstration")) != demonstration:
        raise RefusSecurite("sauvegarde de démonstration et robot réel ne se mélangent pas : restauration refusée")
    if zones != [z.identifiant for z in carte.zones] or not carte.complete:
        raise RefusSecurite("le fichier .pb et sa description .json ne correspondent pas : restauration refusée")
    return carte


def reference_zone(dossier: Path, appareil: str, identifiant: str, *, demonstration: bool = False) -> list[Point] | None:
    """Contour de référence de la zone : dernière référence déclarée, sinon la plus ancienne sauvegarde."""
    candidats: list[tuple[bool, str, Path]] = []
    for lisible in _repertoire(dossier, demonstration).glob("*.json"):
        try:
            meta = json.loads(lisible.read_text(encoding="utf-8"))
            if meta.get("appareil") == appareil and bool(meta.get("demonstration")) == demonstration:
                candidats.append((bool(meta.get("reference")), str(meta["date"]), lisible))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    references = sorted(c for c in candidats if c[0])
    ordre = [references[-1]] if references else []
    ordre += sorted(candidats, key=lambda c: c[1])
    for _reference, _date, lisible in ordre:
        try:
            zone = cartes.decoder(lisible.with_suffix(".pb").read_bytes()).zone_exacte(identifiant)
        except (OSError, cartes.CarteIllisible):
            continue
        if zone is not None:
            return zone.contour
    return None


def controler_cumul(dossier: Path, appareil: str, retouche: Retouche, *, demonstration: bool = False) -> None:
    """SEC-10 : aucun sommet ne s'éloigne de plus de 1 m de la référence locale de sa zone."""
    if retouche.nouvelle_reference:
        return  # la carte actuelle du robot devient la référence, à l'émission
    reference = reference_zone(dossier, appareil, retouche.zone, demonstration=demonstration)
    if reference is None:
        return  # première retouche : la sauvegarde qui la précède fera référence
    conseil = "pour repartir de la carte actuelle du robot, ajoutez --nouvelle-reference"
    if len(reference) != len(retouche.apres):
        raise _refus(f"le contour n'a plus le même nombre de sommets que la référence locale ; {conseil}")
    derive = max(geometrie.longueur(a, b) for a, b in zip(reference, retouche.apres))
    if derive > securite.CUMUL_MAX_M + securite.TOLERANCE_CONTOUR_M:
        raise _refus(
            f"dérive cumulée de {derive * 100:.0f} cm par rapport à la référence locale "
            f"(maximum {securite.CUMUL_MAX_M * 100:g} cm) ; {conseil}"
        )


def preparer_restauration(carte: Carte, sauvegarde: Carte, identifiant: str | None, origine: str) -> Retouche:
    """Remet l'ancien contour d'UNE zone, désignée par son identifiant exact, sous les mêmes bornes."""
    exiger_complete(carte)
    if identifiant is None:
        actuels = {z.identifiant: z.contour for z in carte.zones}
        differentes = [z for z in sauvegarde.zones if z.identifiant in actuels and z.contour != actuels[z.identifiant]]
        if len(differentes) != 1:
            raise ErreurSaisie("précisez la zone à restaurer avec --zone <identifiant> (aucune ou plusieurs zones diffèrent)")
        identifiant = differentes[0].identifiant
    ancienne, zone = sauvegarde.zone_exacte(identifiant), carte.zone_exacte(identifiant)
    if ancienne is None or zone is None:  # jamais de repli par nom pour une restauration
        raise ErreurSaisie(f"identifiant de zone exact attendu : « {identifiant} » est absent de la sauvegarde ou de la carte")
    apres = geometrie.en_f32(ancienne.contour)
    mesures = controler(carte, zone, apres, None)
    resume = {"zone": zone.libelle, "identifiant": zone.identifiant, "restauration": origine} | mesures
    return Retouche(
        zone.identifiant, zone.libelle, list(zone.contour), apres, None,
        f"restaurer-zone {origine} --zone {zone.identifiant}", resume, carte.empreinte(),
    )


def ecrire_apercu(retouche: Retouche, dossier: Path, instant: datetime) -> Path:
    """Aperçu de simulation : ancien contour (gris) et nouveau (rouge), nord en haut."""
    identifiant_sur(retouche.zone)
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


# --- écriture réelle ---------------------------------------------------------------


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
    return (APPLIQUE if applique else NON_CONFIRME), []


async def _lire_deux_fois(session: Session, retouche: Retouche) -> Carte:
    """SEC-09 : deux lectures consécutives, complètes, identiques, et égales à la carte prévisualisée."""
    premiere = await cartes.demander(session, securite.ATTENTE_CARTE_S)
    seconde = await cartes.demander(session, securite.ATTENTE_CARTE_S)
    exiger_complete(premiere)
    exiger_complete(seconde)
    if premiere.socle() != seconde.socle():
        raise RefusSecurite("deux lectures consécutives de la carte diffèrent : écriture refusée (SEC-09)")
    if seconde.empreinte() != retouche.carte:
        raise RefusSecurite("la carte a changé depuis la prévisualisation : prévisualisez de nouveau (SEC-09)")
    return seconde


async def _verdict(session: Session, carte: Carte, retouche: Retouche) -> tuple[str, list[str]]:
    """SEC-11 : deux relectures (≈ 1 s puis ≈ 5 s) ; « appliqué » dès que le contour envoyé revient."""
    verdict, differences = NON_VERIFIE, ["la carte n'a pas pu être relue"]
    for attente in securite.ATTENTES_VERDICT_S:
        await session.dormir(attente)
        try:
            relue = await cartes.demander(session, securite.ATTENTE_CARTE_S)
        except EchecOrdre:
            continue
        verdict, differences = comparer(carte, relue, retouche)
        if verdict != NON_CONFIRME:
            break
    return verdict, differences


async def emettre(
    session: Session, retouche: Retouche, dossier: Path, *, demonstration: bool = False
) -> tuple[dict[str, Any], int]:
    """Écriture réelle d'une retouche. L'appelant garantit qu'aucun autre ordre n'est en cours (SEC-08)."""
    appareil = session.appareil
    await _exiger_repos(session)  # SEC-08
    carte = await _lire_deux_fois(session, retouche)  # SEC-09
    zone = carte.zone_exacte(retouche.zone)
    if zone is None:
        raise RefusSecurite("la zone a disparu de la carte : écriture refusée (SEC-09)")
    controler(carte, zone, retouche.apres, retouche.ecart_m)  # SEC-10, de nouveau, sur la carte fraîche
    try:
        if retouche.nouvelle_reference:  # la carte actuelle du robot devient la référence locale
            sauvegarder(carte, dossier, zone, datetime.now(), appareil, reference=True, demonstration=demonstration)
        controler_cumul(dossier, appareil, retouche, demonstration=demonstration)  # SEC-10 cumulatif
        sauvegarde = sauvegarder(carte, dossier, zone, datetime.now(), appareil, demonstration=demonstration)
    except RefusSecurite:
        raise
    except Exception as erreur:  # noqa: BLE001 — sans sauvegarde relue, aucune écriture
        raise RefusSecurite(
            f"sauvegarde de la carte impossible ({type(erreur).__name__}) : écriture refusée (SEC-09)"
        ) from None
    await _exiger_repos(session)  # SEC-08 : revérifié au dernier moment, le statut a pu changer
    trame = protocole.trame_contour_zone(zone.identifiant, retouche.apres, session.cal)
    restauration = f'lymow restaurer-zone "{sauvegarde}" --zone {zone.identifiant}'
    session.journal.ecrire(  # SEC-06 : sans trace possible, pas d'écriture
        "carte-ecriture", strict=True, ordre=retouche.description, zone=zone.identifiant,
        resume=retouche.resume, sauvegarde=str(sauvegarde), trame=trame.champs,
    )
    publication = "acquittée"
    try:
        try:
            await session.publier(trame, securite.DELAI_ARRET_S)
        except EchecOrdre as erreur:  # peut-être reçue quand même : on relit avant de conclure
            publication = f"non acquittée ({erreur})"
        verdict, differences = await _verdict(session, carte, retouche)
    except asyncio.CancelledError:
        # STOP ou Ctrl+C en pleine retouche : on ne sait pas ce que le robot a retenu.
        session.journal.ecrire("carte-verdict", zone=zone.identifiant, verdict=f"{NON_VERIFIE} : interrompu")
        session.signaler(f"Retouche de « {zone.libelle} » interrompue : {NON_VERIFIE}. Relisez la carte.")
        session.signaler(f"Sauvegarde : {sauvegarde} — pour revenir en arrière : {restauration}")
        raise
    session.journal.ecrire(
        "carte-verdict", zone=zone.identifiant, verdict=verdict, differences=differences, publication=publication
    )
    if verdict != APPLIQUE or publication != "acquittée":
        session.signaler(f"{verdict} après l'écriture de la zone « {zone.libelle} ». Sauvegarde : {sauvegarde}")
        session.signaler(f"Pour revenir en arrière : {restauration}")
    bilan = {"mode": "réel", "ordre": retouche.description, "verdict": verdict, "differences": differences,
             "publication": publication, "sauvegarde": str(sauvegarde), "restauration": restauration} | retouche.resume
    return bilan, 0 if verdict == APPLIQUE else 1
