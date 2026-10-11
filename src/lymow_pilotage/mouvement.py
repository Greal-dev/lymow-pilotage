"""Ordres : analyse, simulation, exécution réelle avec arrêt garanti et constat.

Rôle : porter SEC-03 (arrêt dans un `finally`, homme mort, arrêt à l'abri des
interruptions), SEC-04 (constat après ordre), SEC-07 (séquence) et appeler
SEC-01/02 avant toute émission. Ne sait rien du transport : tout passe par
`Session.publier`.
"""

from __future__ import annotations

import asyncio
import math
import re
import signal
from collections.abc import Coroutine
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
# Chiffres ASCII seulement : `float` accepterait « 1_0 » ou des chiffres d'autres écritures.
_NOMBRE = re.compile(r"[+-]?[0-9]+(?:[.,][0-9]+)?")


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


def lire_nombre(texte: str) -> float:
    if not _NOMBRE.fullmatch(texte):
        raise ValueError(f"nombre décimal simple attendu, reçu « {texte} »")
    return float(texte.replace(",", "."))


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
        return Ordre(mots[0], lire_nombre(mots[1]))
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


def valider(ordre: Ordre, cal: Calibration) -> None:
    """SEC-02 / SEC-07 : tout ce qui peut être refusé sans parler au robot."""
    if ordre.nom in MOUVEMENTS:
        consigne_de(ordre, cal)
    elif ordre.nom == "plateau-haut":
        securite.verifier_plateau_haut(cal.plateau_haut_mm)


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
        resultat["puis_pause"] = _decrire(protocole.trame_tache(code_pause(None), cal)) | {
            "variante": f"userCtrl {socle.USER_CTRL_PAUSE_DOCK} si le dernier état connu indique un retour à la base"
        }
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


# --- SEC-03 : arrêt -----------------------------------------------------------


def _dire(session: Session, message: str) -> None:
    try:
        session.signaler(message)
    except Exception:  # noqa: BLE001 — un affichage en panne ne doit rien empêcher
        pass


class _Garde:
    """SEC-03 : Ctrl+C et Ctrl+Break mènent à l'arrêt, puis sont ignorés tant qu'il dure.

    Hors phase d'arrêt, le signal annule la tâche (donc passe par le `finally`
    du mouvement) ; sans cela Ctrl+Break tuerait le processus robot en marche.
    """

    def __init__(self, session: Session, en_arret: bool) -> None:
        self._session = session
        self.en_arret = en_arret
        self._anciens: dict[int, Any] = {}

    def __enter__(self) -> _Garde:
        self._tache = asyncio.current_task()
        self._boucle = asyncio.get_running_loop()
        for nom in ("SIGINT", "SIGBREAK"):
            numero = getattr(signal, nom, None)
            if numero is None:
                continue
            try:
                self._anciens[numero] = signal.signal(numero, self._recu)
            except (ValueError, OSError):
                pass  # hors du fil principal : aucun signal n'y est livré
        return self

    def __exit__(self, *_exc: object) -> None:
        for numero, ancien in self._anciens.items():
            signal.signal(numero, signal.SIG_DFL if ancien is None else ancien)

    def _recu(self, *_args: object) -> None:
        if self.en_arret:
            _dire(self._session, "Arrêt en cours : interruption ignorée jusqu'à la fin de l'arrêt.")
        elif self._tache is not None:
            self._tache.cancel()
            self._boucle.call_soon_threadsafe(lambda: None)  # réveille la boucle


async def _a_l_abri(travail: Coroutine[Any, Any, int]) -> tuple[int, bool]:
    """SEC-03 : mène `travail` à son terme même si l'appelant est annulé entre-temps."""
    tache = asyncio.ensure_future(travail)
    annule = False
    while not tache.done():
        try:
            await asyncio.shield(tache)
        except asyncio.CancelledError:
            annule = True
        except Exception:  # noqa: BLE001 — relevée par result() ci-dessous
            break
    return tache.result(), annule


async def _un_arret(session: Session, trame: protocole.Trame, compte: list[int]) -> None:
    try:
        await session.publier(trame, securite.DELAI_ARRET_S)
    except EchecOrdre:
        return
    compte[0] += 1


async def _salve(session: Session, trame: protocole.Trame, compte: list[int]) -> None:
    manquants = securite.REPETITIONS_ARRET - compte[0]
    await asyncio.gather(*(_un_arret(session, trame, compte) for _ in range(manquants)))


async def _arret_borne(session: Session) -> int:
    """Arrêts en parallèle, puis reconnexion et nouveaux essais, sous échéance fixe."""
    trame = protocole.trame_arret(session.cal)
    compte = [0]
    reconnecter = getattr(session.lien, "reconnecter", None)
    try:
        async with asyncio.timeout(securite.ECHEANCE_ARRET_S):
            await _salve(session, trame, compte)
            # Liaison tombée robot en marche : une reconnexion est la seule voie d'arrêt.
            while compte[0] < securite.REPETITIONS_ARRET and reconnecter is not None:
                try:
                    await reconnecter()
                except Exception:  # noqa: BLE001 — nouvel essai jusqu'à l'échéance
                    pass
                await _salve(session, trame, compte)
                if compte[0] < securite.REPETITIONS_ARRET:
                    await asyncio.sleep(securite.PAUSE_REPRISE_ARRET_S)
    except TimeoutError:
        pass
    return compte[0]


async def arreter(session: Session) -> int:
    """SEC-03 : arrêt publié 3 fois en QoS 1, acquittements vérifiés, ininterruptible et borné."""
    session.arret_en_cours = True  # plus aucune écriture de journal avant la fin de l'arrêt
    try:
        with _Garde(session, en_arret=True):
            acquittes, annule = await _a_l_abri(_arret_borne(session))
    finally:
        session.fin_arret()
    session.journal.ecrire(
        "arret-emis", sujet=session.sujet, trame=protocole.trame_arret(session.cal).description(),
        acquittes=acquittes,
    )
    if acquittes < securite.REPETITIONS_ARRET:
        raise ArretNonConfirme(
            f"ARRÊT NON CONFIRMÉ ({acquittes}/{securite.REPETITIONS_ARRET} acquittés) : "
            "arrêtez le robot par son bouton STOP ou par l'application"
        )
    _dire(session, f"Arrêt émis et acquitté {acquittes}/{securite.REPETITIONS_ARRET}.")
    if annule:
        raise asyncio.CancelledError
    return acquittes


# --- SEC-01 : état frais --------------------------------------------------------


async def _exiger_lames_arret(session: Session) -> Pose | None:
    """SEC-01 : état reçu dans la session, après sollicitation, puis liste blanche.

    Rend la pose de départ si elle est aussi fraîche que le statut (SEC-04).
    """
    if not session.lien.connecte:
        raise EchecOrdre("MQTT non connecté : rien n'a été émis")
    etat = session.etat
    instant = await session.solliciter()
    if instant is None:
        raise RefusSecurite("sollicitation d'état impossible : mouvement refusé (SEC-01)")
    await session.attendre("statut", instant, ATTENTE_ETAT_S)
    maintenant = session.horloge()
    frais = etat.recu_statut is not None and etat.recu_statut > instant
    securite.verifier_lames_arret(
        etat.statut if frais else None, etat.age_statut(maintenant) if frais else None, etat.en_ligne
    )
    pose_fraiche = (
        etat.recu_pose is not None
        and etat.recu_pose > instant
        and maintenant - etat.recu_pose <= securite.AGE_MAX_ETAT_S
    )
    return etat.pose if pose_fraiche else None


def _surveiller(session: Session) -> None:
    """SEC-01 : en cours de mouvement, un état qui sort de la liste blanche arrête tout."""
    try:
        securite.verifier_presence_et_statut(session.etat.statut, session.etat.en_ligne)
    except RefusSecurite as refus:
        raise EchecOrdre(f"mouvement interrompu en cours de route : {refus}") from None


# --- exécution ------------------------------------------------------------------


async def executer_mouvement(session: Session, ordre: Ordre) -> dict[str, Any]:
    consigne = consigne_de(ordre, session.cal)  # SEC-02
    avant = await _exiger_lames_arret(session)  # SEC-01
    trame = protocole.trame_consigne(consigne.lineaire, consigne.angulaire, session.cal)
    session.journal.ecrire(  # SEC-06 : sans trace possible, pas d'ordre
        "ordre-emis", strict=True, ordre=str(ordre), sujet=session.sujet,
        trame=trame.description(), duree_s=round(consigne.duree, 2), cadence_hz=securite.CADENCE_HZ,
    )
    periode = 1.0 / securite.CADENCE_HZ
    emissions = 0
    with _Garde(session, en_arret=False) as garde:
        debut = session.horloge()
        try:
            # SEC-03 : consigne republiée à cadence fixe ; toute publication manquée
            # ou liaison tombée lève (homme mort) et passe par l'arrêt ci-dessous.
            while consigne.duree - (session.horloge() - debut) > 0.001:
                _surveiller(session)
                await session.publier(trame, securite.DELAI_CONSIGNE_S)
                emissions += 1
                attente = min(debut + emissions * periode, debut + consigne.duree) - session.horloge()
                if attente > 0:
                    await session.dormir(attente)
        finally:
            # SEC-03 : arrêt garanti, y compris sur erreur, annulation, Ctrl+C ou Ctrl+Break.
            garde.en_arret = True
            instant_arret = session.horloge()
            try:
                await arreter(session)
            finally:
                session.journal.ecrire("ordre-termine", ordre=str(ordre), emissions=emissions)
    return {"mode": "réel", "ordre": str(ordre), "emissions": emissions} | await constater(
        session, avant, instant_arret, consigne
    )


def code_pause(statut: int | None) -> int:
    """Logique du socle : 21 pendant un retour à la base, 3 sinon (y compris sans état)."""
    return socle.USER_CTRL_PAUSE_DOCK if statut == socle.WORK_STATUS_DOCKING else socle.USER_CTRL_PAUSE


async def _publier_pause(session: Session, trame: protocole.Trame) -> int:
    try:
        await session.publier(trame, securite.DELAI_ARRET_S)
    except EchecOrdre:
        return 0
    return 1


async def executer_arret(session: Session) -> dict[str, Any]:
    """`stop --reel` : consignes nulles d'abord, sans précondition, puis pause de la tâche.

    La pause part même si l'arrêt n'est pas confirmé ; son échec est signalé à
    part et ne remplace jamais le résultat de l'arrêt.
    """
    avant = session.etat.pose
    instant = session.horloge()
    echec: ArretNonConfirme | None = None
    interrompu = False
    acquittes = 0
    try:
        acquittes = await arreter(session)
    except ArretNonConfirme as erreur:
        echec = erreur
    except asyncio.CancelledError:
        interrompu, acquittes = True, securite.REPETITIONS_ARRET  # levée seulement après 3 acquittements
    code = code_pause(session.etat.statut)
    trame = protocole.trame_tache(code, session.cal)
    with _Garde(session, en_arret=True):
        pause, annule = await _a_l_abri(_publier_pause(session, trame))
    session.journal.ecrire(
        "ordre-emis", ordre="stop (pause)", sujet=session.sujet, trame=trame.description(), acquitte=bool(pause)
    )
    if not pause:
        _dire(session, f"Mise en pause de la tâche (userCtrl {code}) non acquittée.")
    if echec:
        raise echec
    if interrompu or annule:
        raise asyncio.CancelledError
    return {
        "mode": "réel", "ordre": "stop", "arrets_acquittes": acquittes,
        "pause": {"userCtrl": code, "acquittee": bool(pause)},
    } | await constater(session, avant, instant, None)


async def executer_plateau(session: Session) -> dict[str, Any]:
    trame = protocole.trame_hauteur(session.cal.plateau_haut_mm, session.cal)  # SEC-02
    avant = await _exiger_lames_arret(session)  # SEC-01 : le plateau est un organe mobile
    hauteur_avant = session.etat.hauteur_coupe_mm
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
            precedent = etat.recu_statut  # relevé avant la demande : la réponse peut être immédiate
            await session.solliciter()
            await session.attendre("statut", precedent, ATTENTE_ETAT_S)
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


# --- SEC-04 : constat -------------------------------------------------------------


def _juger_translation(avant: Pose, apres: Pose, ordre_m: float) -> tuple[dict[str, Any], str | None]:
    distance, _rotation = avant.vers(apres)
    avance = avant.avance(apres)
    mesures = {"deplacement_m": round(distance, 3), "avance_m": round(avance, 3)}
    if distance < securite.SEUIL_DEPLACEMENT_M:
        return mesures, "le robot n'a pas bougé (patinage ou ordre ignoré)"
    if avance * ordre_m < 0:
        return mesures, f"déplacement dans le mauvais sens ({avance:+.2f} m mesurés pour {ordre_m:+g} m ordonnés)"
    if distance > securite.EXCES_FACTEUR * abs(ordre_m) + securite.EXCES_MARGE_M:
        return mesures, f"déplacement nettement supérieur à l'ordre ({distance:.2f} m pour {abs(ordre_m):g} m)"
    return mesures, None


def _juger_rotation(avant: Pose, apres: Pose, ordre_deg: float) -> tuple[dict[str, Any], str | None]:
    _distance, rotation = avant.vers(apres)
    mesures = {"rotation_deg": round(rotation, 1)}
    if abs(rotation) < securite.SEUIL_ROTATION_DEG:
        return mesures, "le robot n'a pas bougé (patinage ou ordre ignoré)"

    def ecart(cible: float) -> float:
        return abs((rotation - cible + 180.0) % 360.0 - 180.0)

    # Plus proche de l'ordre inverse que de l'ordre : mauvais sens (indécidable à 180°).
    if ecart(-ordre_deg) + securite.SENS_MARGE_DEG < ecart(ordre_deg):
        return mesures, f"rotation dans le mauvais sens ({rotation:+.0f}° mesurés pour {ordre_deg:+g}° ordonnés)"
    if abs(rotation) > securite.EXCES_FACTEUR * abs(ordre_deg) + securite.EXCES_MARGE_DEG:
        return mesures, f"rotation nettement supérieure à l'ordre ({abs(rotation):.0f}° pour {abs(ordre_deg):g}°)"
    return mesures, None


async def constater(
    session: Session, avant: Pose | None, instant: float, consigne: Consigne | None
) -> dict[str, Any]:
    """SEC-04 : relit l'état ; n'affirme un déplacement que sur des poses fraîches avant et après.

    `verifie` est faux dès que le constat ne confirme pas l'ordre (SEC-07 s'y appuie).
    """
    await session.solliciter()
    rafraichi = await session.attendre("pose", instant, ATTENTE_ETAT_S)
    apres = session.etat.pose if rafraichi else None
    rapport: dict[str, Any] = {
        "etat_rafraichi": rafraichi,
        "avant": _pose(avant),
        "apres": _pose(apres),
        "statut_apres": session.etat.resume(session.horloge())["statut_libelle"],
        "verifie": False,
    }
    if apres is None:
        rapport["constat"] = "état non rafraîchi depuis l'ordre : déplacement non vérifié"
    elif consigne is None:
        rapport |= {"verifie": True, "constat": "état relu après l'ordre"}
    elif avant is None:
        rapport["constat"] = "constat impossible : aucune position fraîche reçue juste avant l'ordre"
    else:
        juger = _juger_translation if consigne.attendu == "translation" else _juger_rotation
        mesures, anomalie = juger(avant, apres, consigne.valeur)
        rapport |= mesures | {"verifie": anomalie is None, "constat": anomalie or "déplacement constaté"}
    return rapport


def _pose(pose: Pose | None) -> dict[str, float] | None:
    return None if pose is None else {"est_m": pose.est_m, "nord_m": pose.nord_m, "cap_deg": pose.cap_deg}


# --- SEC-07 : séquence --------------------------------------------------------------


async def executer(session: Session, ordre: Ordre) -> dict[str, Any]:
    if ordre.nom in MOUVEMENTS:
        return await executer_mouvement(session, ordre)
    if ordre.nom == "plateau-haut":
        return await executer_plateau(session)
    if ordre.nom == "stop":
        return await executer_arret(session)
    return await executer_tache(session, ordre.nom)


def refus_prealable(ordres: list[Ordre], cal: Calibration) -> dict[str, Any] | None:
    """SEC-07 : toutes les étapes sont validées avant la première émission."""
    for ordre in ordres:
        try:
            valider(ordre, cal)
        except RefusSecurite as refus:
            return {
                "interrompue": True,
                "motif": f"« {ordre} » refusé avant toute émission : {refus}",
                "non_executees": [str(o) for o in ordres],
            }
    return None


async def executer_sequence(session: Session, ordres: list[Ordre]) -> dict[str, Any]:
    """SEC-07 : mêmes règles par étape ; arrêt au premier refus, échec ou constat non confirmé."""
    bilan: dict[str, Any] = {"mode": "réel", "etapes": [], "interrompue": False}
    refus = refus_prealable(ordres, session.cal)
    if refus:
        return bilan | refus
    for rang, ordre in enumerate(ordres):
        reste = [str(o) for o in ordres[rang + 1 :]]
        try:
            rapport = await executer(session, ordre)
        except (RefusSecurite, EchecOrdre, OSError) as erreur:
            bilan["etapes"].append({"ordre": str(ordre), "echec": str(erreur)})
            return bilan | {
                "interrompue": True,
                "motif": f"« {ordre} » a échoué : {erreur}",
                "non_executees": reste,
                "arret_non_confirme": isinstance(erreur, ArretNonConfirme),
            }
        bilan["etapes"].append(rapport)
        if reste and not rapport["verifie"]:
            return bilan | {
                "interrompue": True,
                "motif": f"séquence interrompue après « {ordre} » : {rapport['constat']}",
                "non_executees": reste,
            }
    return bilan
