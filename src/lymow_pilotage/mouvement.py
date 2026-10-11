"""Ordres : analyse, simulation, exécution réelle avec arrêt garanti et constat.

Rôle : porter SEC-03 (arrêt dans un `finally`, homme mort), SEC-04 (constat
après ordre), SEC-07 (séquence) et appeler SEC-01/02 avant toute émission.
Ne sait rien du transport : tout passe par `Session.publier`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from . import protocole, securite
from .config import Calibration
from .etat import LIBELLES_STATUT, Pose
from .securite import Consigne, RefusSecurite
from .session import EchecOrdre, Session
from .vendor.ha_lymow import const as socle

# Le robot ne parle spontanément que toutes les 60 s environ.
ATTENTE_ETAT_S = 75.0

MOUVEMENTS = ("tourner", "avancer", "reculer")
TACHES = {
    "reprendre": socle.USER_CTRL_RESUME,
    "base": socle.USER_CTRL_RECHARGE_DOCK,
    "annuler": socle.USER_CTRL_FORCE_REINIT,
}
ETAPES_SEQUENCE = (*MOUVEMENTS, "plateau-haut", "stop")
_STATUTS_REPRISE = (socle.WORK_STATUS_PAUSE, socle.WORK_STATUS_REMOTE_CONTROL)


class ArretNonConfirme(EchecOrdre):
    """Les trois arrêts acquittés exigés par SEC-03 n'ont pas été obtenus."""


class ErreurSaisie(Exception):
    """Ordre mal écrit : rien n'est simulé ni émis."""


@dataclass(frozen=True)
class Ordre:
    nom: str
    valeur: float | None = None

    def __str__(self) -> str:
        return self.nom if self.valeur is None else f"{self.nom} {self.valeur:g}"


def analyser_etape(texte: str) -> Ordre:
    mots = texte.split()
    if not mots or mots[0] not in ETAPES_SEQUENCE:
        raise ErreurSaisie(f"étape inconnue « {texte.strip()} » ; étapes possibles : {', '.join(ETAPES_SEQUENCE)}")
    attendu = 2 if mots[0] in MOUVEMENTS else 1
    if len(mots) != attendu:
        raise ErreurSaisie(f"« {mots[0]} » attend {attendu - 1} valeur(s)")
    if attendu == 1:
        return Ordre(mots[0])
    try:
        return Ordre(mots[0], float(mots[1].replace(",", ".")))
    except ValueError:
        raise ErreurSaisie(f"valeur illisible dans « {texte.strip()} »") from None


def analyser_sequence(texte: str) -> list[Ordre]:
    """SEC-07 : toute la séquence est analysée avant la première étape."""
    etapes = [morceau for morceau in texte.split(";") if morceau.strip()]
    if not etapes:
        raise ErreurSaisie("séquence vide")
    return [analyser_etape(etape) for etape in etapes]


def consigne_de(ordre: Ordre, cal: Calibration) -> Consigne:
    """SEC-02 : plafonds appliqués ici, en simulation comme en réel."""
    if ordre.nom == "tourner":
        return securite.consigne_rotation(ordre.valeur, cal.facteur_angle, cal.signe_angulaire)
    if ordre.valeur is None or not ordre.valeur > 0:  # « not > » refuse aussi NaN
        raise RefusSecurite("distance refusée : valeur strictement positive exigée (SEC-02)")
    metres = ordre.valeur if ordre.nom == "avancer" else -ordre.valeur
    return securite.consigne_translation(metres, cal.facteur_distance)


def simuler(ordre: Ordre, cal: Calibration, appareil: str | None) -> dict[str, Any]:
    """SEC-05 : décrit ce qui serait émis, sans liaison ni publication."""
    sujet = protocole.sujet_ordres(appareil) if appareil else "/device/<appareil>/pbinput"
    resultat: dict[str, Any] = {"mode": "simulation", "ordre": str(ordre), "sujet": sujet, "qos": 1}
    arret = protocole.trame_arret(cal)
    if ordre.nom in MOUVEMENTS:
        consigne = consigne_de(ordre, cal)
        trame = protocole.trame_consigne(consigne.lineaire, consigne.angulaire, cal)
        resultat |= {
            "duree_s": round(consigne.duree, 2),
            "cadence_hz": securite.CADENCE_HZ,
            "emissions_prevues": math.ceil(consigne.duree * securite.CADENCE_HZ),
            "arret_final": _decrire(arret) | {"repetitions": securite.REPETITIONS_ARRET},
        }
    elif ordre.nom == "plateau-haut":
        trame = protocole.trame_hauteur(cal.plateau_haut_mm, cal)
    elif ordre.nom == "stop":
        trame = arret
        resultat["repetitions"] = securite.REPETITIONS_ARRET
    else:
        trame = protocole.trame_tache(TACHES[ordre.nom], cal)
        if ordre.nom != "annuler":
            sortie = protocole.trame_tache(socle.USER_CTRL_EXIT_REMOTE, cal)
            resultat["precede_si_telecommande"] = _decrire(sortie)
    resultat["trame"] = _decrire(trame)
    resultat["note"] = "simulation : aucune connexion ouverte, rien n'a été émis ; ajoutez --reel pour émettre"
    return resultat


def _decrire(trame: protocole.Trame) -> dict[str, Any]:
    return trame.description() | {"enveloppe": trame.charge()}


async def arreter(session: Session) -> int:
    """SEC-03 : arrêt publié 3 fois en QoS 1, chaque acquittement vérifié."""
    trame = protocole.trame_arret(session.cal)
    acquittes = await _serie_arrets(session, trame)
    if acquittes < securite.REPETITIONS_ARRET and hasattr(session.lien, "reconnecter"):
        # Liaison tombée robot en marche : une reconnexion est la seule voie d'arrêt.
        try:
            await session.lien.reconnecter()
            acquittes = await _serie_arrets(session, trame)
        except Exception:  # noqa: BLE001 — l'échec est rapporté juste dessous
            pass
    session.journal.ecrire(
        "arret-emis", sujet=session.sujet, trame=trame.description(), acquittes=acquittes
    )
    if acquittes < securite.REPETITIONS_ARRET:
        raise ArretNonConfirme(
            f"ARRÊT NON CONFIRMÉ ({acquittes}/{securite.REPETITIONS_ARRET} acquittés) : "
            "arrêtez le robot par son bouton STOP ou par l'application"
        )
    session.signaler(f"Arrêt émis et acquitté {acquittes}/{securite.REPETITIONS_ARRET}.")
    return acquittes


async def _serie_arrets(session: Session, trame: protocole.Trame) -> int:
    acquittes = 0
    for _ in range(securite.REPETITIONS_ARRET):
        try:
            await session.publier(trame, securite.DELAI_ARRET_S)
            acquittes += 1
        except EchecOrdre:
            continue
    return acquittes


async def _exiger_lames_arret(session: Session) -> None:
    """SEC-01 : état frais exigé, sollicité si besoin, puis liste blanche."""
    etat = session.etat
    age = etat.age_statut(session.horloge())
    if age is None or age > securite.AGE_MAX_ETAT_S:
        await session.solliciter()
        await session.attendre("statut", etat.recu_statut, ATTENTE_ETAT_S)
    securite.verifier_lames_arret(etat.statut, etat.age_statut(session.horloge()), etat.en_ligne)


async def executer_mouvement(session: Session, ordre: Ordre) -> dict[str, Any]:
    consigne = consigne_de(ordre, session.cal)  # SEC-02
    await _exiger_lames_arret(session)  # SEC-01
    trame = protocole.trame_consigne(consigne.lineaire, consigne.angulaire, session.cal)
    avant = session.etat.pose
    session.journal.ecrire(  # SEC-06 : sans trace possible, pas d'ordre
        "ordre-emis", strict=True, ordre=str(ordre), sujet=session.sujet,
        trame=trame.description(), duree_s=round(consigne.duree, 2), cadence_hz=securite.CADENCE_HZ,
    )
    periode = 1.0 / securite.CADENCE_HZ
    emissions = 0
    debut = session.horloge()
    try:
        # SEC-03 : consigne republiée à cadence fixe ; toute publication manquée
        # ou liaison tombée lève (homme mort) et passe par l'arrêt ci-dessous.
        while consigne.duree - (session.horloge() - debut) > 0.001:
            await session.publier(trame, securite.DELAI_CONSIGNE_S)
            emissions += 1
            attente = min(debut + emissions * periode, debut + consigne.duree) - session.horloge()
            if attente > 0:
                await session.dormir(attente)
    finally:
        # SEC-03 : arrêt garanti, y compris sur erreur, annulation ou Ctrl+C.
        instant_arret = session.horloge()
        session.journal.ecrire("ordre-termine", ordre=str(ordre), emissions=emissions)
        await arreter(session)
    return {"mode": "réel", "ordre": str(ordre), "emissions": emissions} | await constater(
        session, avant, instant_arret, consigne.attendu
    )


async def executer_arret(session: Session) -> dict[str, Any]:
    """`stop --reel` : aucune précondition, pas même un état."""
    avant = session.etat.pose
    instant = session.horloge()
    acquittes = await arreter(session)
    return {"mode": "réel", "ordre": "stop", "arrets_acquittes": acquittes} | await constater(
        session, avant, instant, None
    )


async def executer_plateau(session: Session) -> dict[str, Any]:
    await _exiger_lames_arret(session)  # SEC-01 : le plateau est un organe mobile
    trame = protocole.trame_hauteur(session.cal.plateau_haut_mm, session.cal)
    avant, hauteur_avant = session.etat.pose, session.etat.hauteur_coupe_mm
    session.journal.ecrire(
        "ordre-emis", strict=True, ordre="plateau-haut", sujet=session.sujet, trame=trame.description()
    )
    instant = session.horloge()
    await session.publier(trame, securite.DELAI_ARRET_S)
    rapport = await constater(session, avant, instant, None)
    return {
        "mode": "réel",
        "ordre": "plateau-haut",
        "hauteur_avant_mm": hauteur_avant,
        "hauteur_apres_mm": session.etat.hauteur_coupe_mm if rapport["etat_rafraichi"] else None,
    } | rapport


async def executer_tache(session: Session, nom: str) -> dict[str, Any]:
    etat = session.etat
    codes = [TACHES[nom]]
    if nom != "annuler":
        age = etat.age_statut(session.horloge())
        if age is None or age > securite.AGE_MAX_ETAT_S:
            await session.solliciter()
            await session.attendre("statut", etat.recu_statut, ATTENTE_ETAT_S)
        age = etat.age_statut(session.horloge())
        if etat.statut is None or age is None or age > securite.AGE_MAX_ETAT_S:
            raise RefusSecurite(f"« {nom} » refusé : aucun état frais du robot")
        if nom == "reprendre" and etat.statut not in _STATUTS_REPRISE:
            libelle = LIBELLES_STATUT.get(etat.statut, str(etat.statut))
            raise RefusSecurite(f"« reprendre » refusé : rien à reprendre (statut : {libelle})")
        if etat.statut == socle.WORK_STATUS_REMOTE_CONTROL:
            codes.insert(0, socle.USER_CTRL_EXIT_REMOTE)
    avant = etat.pose
    instant = session.horloge()
    for code in codes:
        trame = protocole.trame_tache(code, session.cal)
        session.journal.ecrire(
            "ordre-emis", strict=True, ordre=nom, sujet=session.sujet, trame=trame.description()
        )
        await session.publier(trame, securite.DELAI_ARRET_S)
    return {"mode": "réel", "ordre": nom, "codes_emis": codes} | await constater(session, avant, instant, None)


async def constater(
    session: Session, avant: Pose | None, instant: float, attendu: str | None
) -> dict[str, Any]:
    """SEC-04 : relit l'état ; n'affirme un déplacement que sur une pose reçue après l'ordre."""
    await session.solliciter()
    rafraichi = await session.attendre("pose", instant, ATTENTE_ETAT_S)
    apres = session.etat.pose
    rapport: dict[str, Any] = {
        "etat_rafraichi": rafraichi,
        "avant": _pose(avant),
        "apres": _pose(apres) if rafraichi else None,
        "statut_apres": session.etat.resume(session.horloge())["statut_libelle"],
    }
    if not rafraichi or avant is None or apres is None:
        rapport["constat"] = "état non rafraîchi depuis l'ordre : déplacement non vérifié"
        return rapport
    distance, rotation = avant.vers(apres)
    rapport |= {"deplacement_m": round(distance, 3), "rotation_deg": round(rotation, 1)}
    immobile = (attendu == "translation" and distance < securite.SEUIL_DEPLACEMENT_M) or (
        attendu == "rotation" and abs(rotation) < securite.SEUIL_ROTATION_DEG
    )
    if immobile:
        rapport["constat"] = "le robot n'a pas bougé (patinage ou ordre ignoré)"
    else:
        rapport["constat"] = "déplacement constaté" if attendu else "état relu après l'ordre"
    return rapport


def _pose(pose: Pose | None) -> dict[str, float] | None:
    return None if pose is None else {"est_m": pose.est_m, "nord_m": pose.nord_m, "cap_deg": pose.cap_deg}


async def executer(session: Session, ordre: Ordre) -> dict[str, Any]:
    if ordre.nom in MOUVEMENTS:
        return await executer_mouvement(session, ordre)
    if ordre.nom == "plateau-haut":
        return await executer_plateau(session)
    if ordre.nom == "stop":
        return await executer_arret(session)
    return await executer_tache(session, ordre.nom)


async def executer_sequence(session: Session, ordres: list[Ordre]) -> dict[str, Any]:
    """SEC-07 : mêmes règles par étape ; arrêt au premier refus ou échec."""
    etapes: list[dict[str, Any]] = []
    for ordre in ordres:
        try:
            etapes.append(await executer(session, ordre))
        except (RefusSecurite, EchecOrdre, OSError) as erreur:
            etapes.append({"ordre": str(ordre), "echec": str(erreur)})
            return {"mode": "réel", "etapes": etapes, "interrompue": True}
    return {"mode": "réel", "etapes": etapes, "interrompue": False}
