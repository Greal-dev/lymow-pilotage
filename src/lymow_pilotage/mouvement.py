"""Ordres : analyse, simulation, exécution réelle avec arrêt garanti et constat.

Rôle : porter SEC-03 (arrêt dans un `finally`, homme mort, arrêt à l'abri des
interruptions), SEC-04 (constat après ordre), SEC-07 (séquence) et appeler
SEC-01/02 avant toute émission (SEC-01b : attestation de l'opérateur lue sur la session). Ne sait rien du transport : tout passe par
`Session.publier`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import re
import signal
import sys
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from . import diagnostic, garde_carte, protocole, recalage, securite
from .config import Calibration
from .etat import LIBELLES_STATUT, Pose
from .securite import Consigne, RefusSecurite
from .session import EchecOrdre, Session
from .vendor.ha_lymow import const as socle

# Le robot ne parle spontanément que toutes les 60 s environ.
ATTENTE_ETAT_S = 75.0
# Constat après un ordre : une trame d'état fraîche, pas davantage (voir `constater`).
ATTENTE_CONSTAT_S = 12.0
IMMOBILE = "le robot n'a pas bougé (patinage ou ordre ignoré)"
VERIFIEE, NON_VERIFIABLE, ANOMALIE, AUCUN_ETAT = "vérifiée", "non vérifiable", "anomalie", "aucun état"
POSITION_NON_FOURNIE = (
    "position non fournie par le robot : déplacement non vérifiable par l'outil (utilisez la vidéo)"
)

MOUVEMENTS = ("tourner", "avancer", "reculer", "arc")
ESSAI = "essai-arret-auto"
EXPOSITION_ESSAI = (
    f"au plus {securite.ESSAI_VITESSE_MAX_M_S:g} m/s × {securite.ESSAI_OBSERVATION_MAX_S:g} s = "
    f"{securite.ESSAI_VITESSE_MAX_M_S * securite.ESSAI_OBSERVATION_MAX_S:g} m si le robot conserve la consigne, "
    f"plus le délai d'arrêt (jusqu'à {securite.ECHEANCE_ARRET_S:g} s d'essais si la liaison tombe)"
)
RAPPEL_ESSAI = "l'observation visuelle fait foi : cette lecture n'est qu'un indice"
PLATEAUX = ("plateau", "plateau-haut")
PHARES = {"phares-allumer": True, "phares-eteindre": False}  # éclairage de la caméra : ordres sans mouvement
VOYANTS = {"voyants-allumer": True, "voyants-eteindre": False}  # ordres sans mouvement
TACHES = {
    "reprendre": socle.USER_CTRL_RESUME,
    "base": socle.USER_CTRL_RECHARGE_DOCK,
    "annuler": socle.USER_CTRL_FORCE_REINIT,
    # Mise en pause de la tâche, seule : la même pause que celle de `stop`, sans les consignes nulles.
    "pause": socle.USER_CTRL_PAUSE,
    # SEC-12 : la reprise malgré une position imprécise — même ordre, garde levée explicitement.
    "reprendre-quand-meme": socle.USER_CTRL_RESUME,
}
REPRISE_FORCEE = "reprendre-quand-meme"
ETAPES_SEQUENCE = (*MOUVEMENTS, *PLATEAUX, "stop")
OPTIONS_MOUVEMENT = ("--vitesse", "--duree")
_STATUTS_REPRISE = (socle.WORK_STATUS_PAUSE, socle.WORK_STATUS_REMOTE_CONTROL)
# Chiffres ASCII seulement : `float` accepterait « 1_0 » ou des chiffres d'autres écritures.
_NOMBRE = re.compile(r"[+-]?[0-9]+(?:[.,][0-9]+)?")
_ENTIER = re.compile(r"[0-9]{1,4}")


class ArretNonConfirme(EchecOrdre):
    """Les trois arrêts acquittés exigés par SEC-03 n'ont pas été obtenus."""


class ErreurSaisie(Exception):
    """Ordre mal écrit : rien n'est simulé ni émis."""


@dataclass(frozen=True)
class Ordre:
    """`valeur` : degrés, mètres ou millimètres. `duree` : secondes tenues, ou fenêtre de l'essai."""

    nom: str
    valeur: float | None = None
    vitesse: float | None = None
    duree: float | None = None
    force: bool = False  # SEC-13 : « Forcer ce pas » — le garde-fou carte est levé pour cet ordre, et journalisé
    angle: float | None = None  # arc seulement : rotation en degrés (positif = le cap tourne à gauche)

    def __str__(self) -> str:
        mots = [self.nom]
        if self.valeur is not None:
            mots.append(f"{self.valeur:g}")
        if self.angle is not None:
            mots.append(f"{self.angle:g}")
        if self.duree is not None:
            mots.append(f"{'--observation' if self.nom == ESSAI else '--duree'} {self.duree:g}")
        if self.vitesse is not None:
            mots.append(f"--vitesse {self.vitesse:g}")
        if self.force:
            mots.append("--forcer")
        return " ".join(mots)


def lire_nombre(texte: str) -> float:
    if not _NOMBRE.fullmatch(texte):
        raise ValueError(f"nombre décimal simple attendu, reçu « {texte} »")
    return float(texte.replace(",", "."))


def lire_entier(texte: str) -> int:
    if not _ENTIER.fullmatch(texte):
        raise ValueError(f"entier positif attendu, reçu « {texte} »")
    return int(texte)


def analyser_etape(texte: str) -> Ordre:
    """Une étape de séquence : nom, valeur éventuelle, puis `--vitesse` / `--duree` pour un mouvement."""
    mots = texte.split()
    nom = mots[0] if mots else ""
    if nom == ESSAI:
        raise ErreurSaisie(f"« {ESSAI} » est interdite dans une séquence : à lancer seule, robot à vue")
    if nom not in ETAPES_SEQUENCE:
        raise ErreurSaisie(f"étape inconnue « {texte.strip()} » ; étapes possibles : {', '.join(ETAPES_SEQUENCE)}")
    positionnels: list[str] = []
    options: dict[str, float] = {}
    reste = iter(mots[1:])
    try:
        for mot in reste:
            if mot in OPTIONS_MOUVEMENT and nom in MOUVEMENTS and mot not in options:
                options[mot] = lire_nombre(next(reste, ""))
            elif mot.startswith("--"):
                # SEC-05 : `--reel` ne se glisse jamais dans la chaîne d'une séquence.
                raise ErreurSaisie(f"option « {mot} » refusée dans l'étape « {texte.strip()} »")
            else:
                positionnels.append(mot)
        attendus = (2,) if nom == "arc" else (0, 1) if nom in MOUVEMENTS else (1,) if nom == "plateau" else (0,)
        if len(positionnels) not in attendus:
            raise ErreurSaisie(f"nombre de valeurs inattendu dans « {texte.strip()} »")
        lire = lire_entier if nom == "plateau" else lire_nombre
        valeur = lire(positionnels[0]) if positionnels else None
        angle = lire_nombre(positionnels[1]) if nom == "arc" else None
    except ValueError:
        raise ErreurSaisie(f"valeur illisible dans « {texte.strip()} »") from None
    return Ordre(nom, valeur, options.get("--vitesse"), options.get("--duree"), angle=angle)


def analyser_sequence(texte: str) -> list[Ordre]:
    """SEC-07 : toute la séquence est analysée avant la première étape."""
    etapes = [morceau for morceau in texte.split(";") if morceau.strip()]
    if not etapes:
        raise ErreurSaisie("séquence vide")
    return [analyser_etape(etape) for etape in etapes]


def consigne_de(ordre: Ordre, cal: Calibration) -> Consigne:
    """SEC-02 : plafonds appliqués ici, en simulation comme en réel."""
    if ordre.nom == ESSAI:
        return securite.consigne_essai(ordre.vitesse, ordre.duree)
    if ordre.nom == "arc":
        if ordre.valeur is None or ordre.angle is None or ordre.duree is not None:
            raise RefusSecurite("arc : une distance et un angle sont exigés, sans --duree (SEC-02)")
        return securite.consigne_arc(
            ordre.valeur, ordre.angle, cal.facteur_distance, cal.facteur_angle, cal.signe_angulaire, cal.voie_m,
            vitesse=ordre.vitesse,
        )
    if ordre.angle is not None:
        raise RefusSecurite(f"« {ordre.nom} » n'accepte pas d'angle (SEC-02)")
    if ordre.nom == "tourner":
        return securite.consigne_rotation(
            ordre.valeur, cal.facteur_angle, cal.signe_angulaire, vitesse=ordre.vitesse, duree=ordre.duree
        )
    sens = 1 if ordre.nom == "avancer" else -1
    metres = None
    if ordre.valeur is not None:
        if not ordre.valeur > 0:  # « not > » refuse aussi NaN
            raise RefusSecurite("distance refusée : valeur strictement positive exigée (SEC-02)")
        metres = sens * ordre.valeur
    return securite.consigne_translation(
        metres, cal.facteur_distance, vitesse=ordre.vitesse, duree=ordre.duree, sens=sens
    )


def hauteur_de(ordre: Ordre, cal: Calibration) -> int:
    """SEC-02 : `plateau <mm>` sur toute la course, `plateau-haut` dans le haut seulement."""
    if ordre.nom == "plateau-haut":
        return securite.verifier_plateau_haut(cal.plateau_haut_mm)
    return securite.verifier_hauteur(ordre.valeur)  # type: ignore[arg-type]


def valider(ordre: Ordre, cal: Calibration) -> None:
    """SEC-02 / SEC-07 : tout ce qui peut être refusé sans parler au robot."""
    if ordre.nom in MOUVEMENTS or ordre.nom == ESSAI:
        consigne_de(ordre, cal)
    elif ordre.nom in PLATEAUX:
        hauteur_de(ordre, cal)


def _grandeurs(consigne: Consigne) -> dict[str, Any]:
    """Vitesse et déplacement commandé (vitesse × durée), toujours affichés."""
    if consigne.attendu == "arc":
        return {
            "vitesse_m_s": round(abs(consigne.lineaire), 3), "vitesse_rad_s": round(abs(consigne.angulaire), 3),
            "deplacement_commande_m": round(abs(consigne.lineaire) * consigne.duree, 3),
            "deplacement_commande_deg": round(math.degrees(abs(consigne.angulaire) * consigne.duree), 1),
            "rotation_visee_deg": round(consigne.angle, 1),
        } | ({"arc": consigne.annonce} if consigne.annonce else {})
    if consigne.attendu == "rotation":
        return {
            "vitesse_rad_s": abs(consigne.angulaire),
            "deplacement_commande_deg": round(math.degrees(abs(consigne.angulaire) * consigne.duree), 1),
        }
    return {
        "vitesse_m_s": abs(consigne.lineaire),
        "deplacement_commande_m": round(abs(consigne.lineaire) * consigne.duree, 3),
    }


def simuler(ordre: Ordre, cal: Calibration, appareil: str | None) -> dict[str, Any]:
    """SEC-05 : décrit ce qui serait émis, sans liaison ni publication."""
    sujet = protocole.sujet_ordres(appareil) if appareil else "/device/<appareil>/pbinput"
    resultat: dict[str, Any] = {"mode": "simulation", "ordre": str(ordre), "sujet": sujet, "qos": 1}
    arret = protocole.trame_arret(cal)
    arret_final = _decrire(arret) | {"repetitions": securite.REPETITIONS_ARRET}
    if ordre.nom in MOUVEMENTS:
        consigne = consigne_de(ordre, cal)
        trame = protocole.trame_consigne(consigne.lineaire, consigne.angulaire, cal)
        resultat |= _grandeurs(consigne) | {
            "duree_s": round(consigne.duree, 2),
            "cadence_hz": securite.CADENCE_HZ,
            "emissions_prevues": math.ceil(round(consigne.duree * securite.CADENCE_HZ, 6)),
            "arret_final": arret_final,
        }
    elif ordre.nom == ESSAI:
        consigne = consigne_de(ordre, cal)
        trame = protocole.trame_consigne(consigne.lineaire, 0.0, cal)
        resultat |= {
            "vitesse_m_s": consigne.lineaire,
            "emissions_prevues": 1,
            "observation_s": consigne.duree,
            "deplacement_si_consigne_conservee_m": round(consigne.valeur, 3),
            "arret_final": arret_final,
            "exposition": EXPOSITION_ESSAI,
        }
    elif ordre.nom in PLATEAUX:
        trame = protocole.trame_hauteur(hauteur_de(ordre, cal), cal)
    elif ordre.nom in VOYANTS:
        trame = protocole.trame_voyants(VOYANTS[ordre.nom], cal)
    elif ordre.nom in PHARES:
        trame = protocole.trame_phares(PHARES[ordre.nom], cal)
    elif ordre.nom == "stop":
        trame = arret
        resultat["repetitions"] = securite.REPETITIONS_ARRET
        resultat["puis_pause"] = _decrire(protocole.trame_tache(code_pause(None), cal)) | {
            "variante": f"userCtrl {socle.USER_CTRL_PAUSE_DOCK} si le dernier état connu indique un retour à la base"
        }
    else:
        trame = protocole.trame_tache(TACHES[ordre.nom], cal)
        if ordre.nom == "reprendre":
            resultat["garde"] = "refusé en réel si la position du robot est imprécise (SEC-12)"
        if ordre.nom == "pause":
            resultat["variante"] = f"userCtrl {socle.USER_CTRL_PAUSE_DOCK} si le robot rentre à sa base"
        elif ordre.nom != "annuler":
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


# CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT : Windows laisse environ 5 s.
EVENEMENTS_FERMETURE_CONSOLE = (2, 5, 6)
DELAI_FERMETURE_CONSOLE_S = 4.5


def _noyau_windows() -> Any:
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


class _Garde:
    """SEC-03 : Ctrl+C et Ctrl+Break mènent à l'arrêt, puis sont ignorés tant qu'il dure.

    Sous Windows, la fermeture de la console, de la session ou de la machine mène
    aussi à l'arrêt, dans le délai que le système accorde.

    Hors phase d'arrêt, le signal annule la tâche (donc passe par le `finally`
    du mouvement) ; sans cela Ctrl+Break tuerait le processus robot en marche.
    """

    def __init__(self, session: Session, en_arret: bool) -> None:
        self._session = session
        self.en_arret = en_arret
        self._anciens: dict[int, Any] = {}
        self._termine = threading.Event()
        self._rappel_console: Any = None

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
        self._poser_rappel_console()
        return self

    def __exit__(self, *_exc: object) -> None:
        for numero, ancien in self._anciens.items():
            signal.signal(numero, signal.SIG_DFL if ancien is None else ancien)
        if self._rappel_console is not None:
            try:
                _noyau_windows().SetConsoleCtrlHandler(self._rappel_console, 0)
            except Exception:  # noqa: BLE001
                pass
        self._termine.set()  # libère le fil de fermeture de console : l'arrêt est parti

    def _poser_rappel_console(self) -> None:
        """Windows : fermer la fenêtre, fermer la session ou éteindre tue le processus sans signal.

        Le système accorde quelques secondes au rappel ci-dessous ; hors Windows, rien à poser.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes

            prototype = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)  # BOOL HandlerRoutine(DWORD)
            rappel = prototype(self._console_fermee)
            if _noyau_windows().SetConsoleCtrlHandler(rappel, 1):
                self._rappel_console = rappel  # référence gardée : le système l'appellera plus tard
        except Exception:  # noqa: BLE001 — sans console ni ctypes, la garde par signaux reste en place
            self._rappel_console = None

    def _console_fermee(self, evenement: int) -> int:
        """Appelé par Windows sur un autre fil ; au retour, le processus est détruit."""
        if evenement not in EVENEMENTS_FERMETURE_CONSOLE:
            return 0  # Ctrl+C et Ctrl+Break suivent la voie des signaux
        if not self.en_arret and self._tache is not None:
            self._boucle.call_soon_threadsafe(self._tache.cancel)  # passe par le `finally` du mouvement
        self._termine.wait(DELAI_FERMETURE_CONSOLE_S)
        return 1

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


def _attestee(session: Session) -> bool:
    """SEC-01b : une attestation de l'opérateur est-elle active sur cette session ?"""
    return session.attestation is not None and session.attestation.active


async def _exiger_lames_arret(session: Session, attestable: bool = False) -> Pose | None:
    """SEC-01 : état reçu dans la session, après sollicitation, puis liste blanche.

    Rend la pose de départ si elle est aussi fraîche que le statut (SEC-04).
    SEC-01b : `attestable` n'est vrai que pour tourner, avancer, reculer ; le plateau et
    l'essai d'arrêt automatique restent sur la seule liste blanche.
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
        etat.statut if frais else None, etat.age_statut(maintenant) if frais else None, etat.en_ligne,
        attestee=attestable and _attestee(session),
    )
    pose_fraiche = (
        etat.recu_pose is not None
        and etat.recu_pose > instant
        and maintenant - etat.recu_pose <= securite.AGE_MAX_ETAT_S
    )
    return etat.pose if pose_fraiche else None


def _surveiller(session: Session, attestable: bool = False) -> None:
    """SEC-01 : en cours de mouvement, un état qui sort de la liste blanche arrête tout.

    SEC-01b : même règle qu'au départ ; une attestation retirée ou expirée en route arrête aussi.
    """
    try:
        securite.verifier_presence_et_statut(
            session.etat.statut, session.etat.en_ligne, attestable and _attestee(session)
        )
    except RefusSecurite as refus:
        raise EchecOrdre(f"mouvement interrompu en cours de route : {refus}") from None


def _sur_la_carte(session: Session, pose: Pose | None) -> tuple[float, float] | None:
    """La pose annoncée, située sur la carte : avec le recalage d'affichage de l'opérateur s'il y en a un."""
    return None if pose is None else recalage.appliquer((pose.est_m, pose.nord_m), session.decalage_carte)


def _age_de_la_pose(session: Session) -> float | None:
    recue = session.etat.recu_pose
    return None if recue is None else session.horloge() - recue


def _avertir_de_l_arc(session: Session, ordre: Ordre) -> dict[str, Any]:
    """SEC-13 pour un arc : toujours un simple avertissement, calculé sur son point d'arrivée (la corde)."""
    consigne = consigne_de(ordre, session.cal)
    devant, gauche = securite.arrivee_arc(consigne.valeur, consigne.angle)
    pose = session.etat.pose
    corde = math.hypot(devant, gauche)
    jugement = garde_carte.juger_pas(
        session.carte_reference, _sur_la_carte(session, pose),
        None if pose is None else pose.cap_deg + math.degrees(math.atan2(gauche, devant)), corde,
        session.etat.precision_m, _age_de_la_pose(session),
    )
    texte, alerte = garde_carte.constat(jugement, corde)
    texte = texte.replace(" devant à ", " vers l'arrivée de l'arc à ").replace(" derrière à ", " vers l'arrivée de l'arc à ")
    return {"garde_carte": texte, "garde_carte_alerte": alerte}


def _garder_le_pas(session: Session, ordre: Ordre) -> tuple[Ordre, dict[str, Any]]:
    """SEC-13 : une avance ou un recul est projeté sur la carte de référence avant d'être émis.

    Par défaut, simple AVERTISSEMENT (voir plus bas) ; le comportement décrit ici ne vaut que si l'opérateur a
    coché « Arrêter aux bords de zone (d'après la carte) ».
    Hors des zones et passages, ou dans une zone interdite (marge 0,30 m + incertitude annoncée) : le pas
    est raccourci à la plus grande distance sûre, ou refusé. « Forcer ce pas » passe outre (la carte peut
    être fausse, et il faut pouvoir sortir d'une zone) : c'est journalisé. Sans pose ou sans carte, aucun
    garde-fou n'est possible : dit au bilan, sans bloquer. L'allure lente est choisie près d'un bord ou
    quand la position n'est pas fiable, sauf vitesse choisie par l'opérateur."""
    if ordre.nom == "arc":
        return ordre, _avertir_de_l_arc(session, ordre)
    if ordre.nom not in ("avancer", "reculer") or ordre.valeur is None:
        return ordre, {}
    pose, precision = session.etat.pose, session.etat.precision_m
    sens = 1.0 if ordre.nom == "avancer" else -1.0
    jugement = garde_carte.juger_pas(
        session.carte_reference, _sur_la_carte(session, pose),
        None if pose is None else pose.cap_deg, sens * ordre.valeur, precision, _age_de_la_pose(session),
    )
    infos: dict[str, Any] = {"garde_carte": jugement.motif}
    if not session.arret_aux_bords:
        # Décision du propriétaire (2026-10-12, constat réel : position affichée fausse, pas refusés à tort) :
        # par défaut la carte AVERTIT seulement — le pas part en entier, jamais raccourci ni refusé.
        texte, alerte = garde_carte.constat(jugement, sens * ordre.valeur)
        infos = {"garde_carte": texte, "garde_carte_alerte": alerte}
    elif jugement.decision in (garde_carte.RACCOURCI, garde_carte.REFUS):
        if ordre.force:
            session.journal.ecrire("garde-carte-forcee", strict=True, ordre=str(ordre), motif=jugement.motif)
            infos["garde_carte"] = f"pas forcé par l'opérateur malgré : {jugement.motif}"
        elif jugement.bloque:
            raise RefusSecurite(f"{jugement.motif} — « Forcer ce pas » passe outre (SEC-13)")
        else:
            ordre = dataclasses.replace(ordre, valeur=jugement.distance_m)
    vitesse, annonce = garde_carte.allure(ordre.vitesse, jugement, precision)
    if vitesse is not None:
        # SEC-02 borne la durée d'un ordre : un pas trop long pour l'allure lente garde sa longueur (la suite d'une
        # séquence en dépend) et prend la plus faible vitesse qui tient dans cette durée — c'est annoncé.
        vitesse = garde_carte.vitesse_lente(ordre.valeur, session.cal.facteur_distance)
        retenue = garde_carte.annonce_vitesse(vitesse, ordre.valeur)
        annonce = f"{annonce} — {retenue}" if retenue else annonce
        ordre = dataclasses.replace(ordre, vitesse=vitesse)
        infos["allure"] = annonce
    return ordre, infos


def _surveiller_choc(session: Session, chocs_au_depart: frozenset[tuple[str, int]], suivi: Any) -> None:
    """SEC-14 : un indice de choc apparu pendant le mouvement arrête tout (l'arrêt suit, garanti par SEC-03).

    (a) code d'erreur ou d'avertissement de choc, pare-chocs, ultrasons, soulèvement ou patinage, absent au
    départ ; (b) statut passé à erreur ou arrêt d'urgence : déjà couvert par `_surveiller` (SEC-01) ;
    (c) pose qui n'avance plus alors que les consignes continuent."""
    nouveaux = garde_carte.codes_de_choc(session.etat.donnees) - chocs_au_depart
    if nouveaux:
        codes = ", ".join(f"{nature} {code}" for nature, code in sorted(nouveaux))
        raise EchecOrdre(f"indice de choc annoncé par le robot ({codes}) : arrêt (SEC-14)")
    pose = session.etat.pose
    if suivi is not None and pose is not None and session.etat.recu_pose is not None:
        suivi.noter(session.etat.recu_pose, pose.est_m, pose.nord_m)
        if suivi.bloque(session.horloge()):
            raise EchecOrdre("obstacle ou patinage : arrêt — la position n'avance plus (SEC-14)")


# --- exécution ------------------------------------------------------------------


async def executer_mouvement(session: Session, ordre: Ordre) -> dict[str, Any]:
    consigne = consigne_de(ordre, session.cal)  # SEC-02
    avant = await _exiger_lames_arret(session, attestable=True)  # SEC-01, SEC-01b
    precision_avant = session.etat.precision_m
    ordre, garde_fou = _garder_le_pas(session, ordre)  # SEC-13 : pas raccourci, refusé, ou forcé
    if garde_fou:
        consigne = consigne_de(ordre, session.cal)  # SEC-02, sur le pas retenu
    chocs_au_depart = garde_carte.codes_de_choc(session.etat.donnees)
    suivi = None
    # SEC-01b : tout ordre émis pendant qu'une attestation est active le dit, au journal et au bilan.
    sous_attestation: dict[str, Any] = {}
    if _attestee(session):
        sous_attestation = {"attestation": securite.MENTION_ATTESTATION, "statut_declare": session.etat.statut}
    trame = protocole.trame_consigne(consigne.lineaire, consigne.angulaire, session.cal)
    session.journal.ecrire(  # SEC-06 : sans trace possible, pas d'ordre
        "ordre-emis", strict=True, ordre=str(ordre), sujet=session.sujet,
        trame=trame.description(), duree_s=round(consigne.duree, 2), cadence_hz=securite.CADENCE_HZ,
        **sous_attestation, **garde_fou,
    )
    periode = 1.0 / securite.CADENCE_HZ
    emissions = 0
    with _Garde(session, en_arret=False) as garde:
        debut = session.horloge()
        if consigne.lineaire:
            suivi = garde_carte.SuiviDeProgression(consigne.lineaire, debut)
        try:
            # SEC-03 : consigne republiée à cadence fixe ; toute publication manquée
            # ou liaison tombée lève (homme mort) et passe par l'arrêt ci-dessous.
            # Double borne : le temps écoulé, et le nombre d'émissions (un réveil en avance
            # de l'horloge ne doit pas ajouter une consigne).
            maximum = math.ceil(round(consigne.duree * securite.CADENCE_HZ, 6))
            while emissions < maximum and consigne.duree - (session.horloge() - debut) > 0.001:
                _surveiller(session, attestable=True)
                _surveiller_choc(session, chocs_au_depart, suivi)  # SEC-14
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
    depart = {"precision_avant_m": None if precision_avant is None else round(precision_avant, 2)}
    # SEC-14 (c) n'opère que si la pose arrive assez souvent (télécommande : ~0,6 s) : sinon c'est dit.
    if suivi is not None:
        garde_fou["surveillance_progression"] = "active" if suivi.active else "inactive : position reçue trop rarement"
    rapport = {"mode": "réel", "ordre": str(ordre), "emissions": emissions} | depart | sous_attestation | garde_fou | await constater(
        session, avant, instant_arret, consigne
    )
    if consigne.annonce:
        rapport["arc"] = consigne.annonce
    if "garde_carte" in garde_fou and not session.arret_aux_bords and consigne.attendu != "arc":  # la carte, après le pas aussi
        apres = session.etat.pose
        suite = garde_carte.juger_pas(
            session.carte_reference, _sur_la_carte(session, apres),
            None if apres is None else apres.cap_deg, math.copysign(1.0, consigne.lineaire), session.etat.precision_m,
            _age_de_la_pose(session),
        )
        rapport["garde_carte_apres"] = garde_carte.constat(suite, math.copysign(1.0, consigne.lineaire))[0]
    return rapport


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
    if session.attestation is not None:
        session.attestation.retirer("STOP")  # SEC-01b : après les arrêts, jamais avant (rien ne les retarde)
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


async def executer_essai(session: Session, ordre: Ordre) -> dict[str, Any]:
    """Essai d'arrêt automatique : UNE consigne, puis silence, puis l'arrêt garanti habituel."""
    consigne = consigne_de(ordre, session.cal)  # SEC-02
    avant = await _exiger_lames_arret(session)  # SEC-01
    trame = protocole.trame_consigne(consigne.lineaire, 0.0, session.cal)
    session.journal.ecrire(
        "ordre-emis", strict=True, ordre=str(ordre), sujet=session.sujet,
        trame=trame.description(), observation_s=consigne.duree, emissions=1,
    )
    with _Garde(session, en_arret=False) as garde:
        instant_arret = session.horloge()
        try:
            _surveiller(session)
            await session.publier(trame, securite.DELAI_CONSIGNE_S)  # la seule consigne de l'essai
            debut = session.horloge()
            while (restant := consigne.duree - (session.horloge() - debut)) > 0.001:
                _surveiller(session)  # SEC-01 : la surveillance continue pendant le silence
                if not session.lien.connecte:
                    raise EchecOrdre("connexion MQTT perdue pendant la fenêtre d'observation")
                await session.dormir(min(restant, 1.0 / securite.CADENCE_HZ))
        finally:
            # SEC-03 : même arrêt garanti qu'un mouvement, y compris sur Ctrl+C.
            garde.en_arret = True
            instant_arret = session.horloge()
            try:
                await arreter(session)
            finally:
                session.journal.ecrire("ordre-termine", ordre=str(ordre), emissions=1)
    rapport = await constater(session, avant, instant_arret, None)
    apres = session.etat.pose if rapport["etat_rafraichi"] else None
    resultat: dict[str, Any] = {
        "mode": "réel", "ordre": str(ordre), "emissions": 1,
        "vitesse_m_s": consigne.lineaire, "observation_s": consigne.duree,
        "deplacement_si_consigne_conservee_m": round(consigne.valeur, 3),
    } | {cle: rapport[cle] for cle in ("etat_rafraichi", "avant", "apres", "statut_apres")}
    if avant is None or apres is None:
        resultat["lecture"] = "constat instrumental impossible, fiez-vous à l'observation visuelle"
    else:
        distance = avant.vers(apres)[0]
        resultat["deplacement_m"] = round(distance, 3)
        if distance >= securite.ESSAI_PART_CONSERVEE * consigne.valeur:
            resultat["lecture"] = "le robot semble conserver la dernière consigne"
        elif distance <= securite.ESSAI_PART_ARRET * consigne.valeur:
            resultat["lecture"] = "le robot semble s'arrêter seul"
        else:
            resultat["lecture"] = "lecture indécise : déplacement intermédiaire"
    return resultat | {"verifie": rapport["verifie"], "rappel": RAPPEL_ESSAI}


async def executer_plateau(session: Session, ordre: Ordre = Ordre("plateau-haut")) -> dict[str, Any]:
    trame = protocole.trame_hauteur(hauteur_de(ordre, session.cal), session.cal)  # SEC-02
    avant = await _exiger_lames_arret(session)  # SEC-01 : le plateau est un organe mobile
    hauteur_avant = session.etat.hauteur_coupe_mm
    session.journal.ecrire(
        "ordre-emis", strict=True, ordre=str(ordre), sujet=session.sujet, trame=trame.description()
    )
    instant = session.horloge()
    await session.publier(trame, securite.DELAI_ARRET_S)
    rapport = await constater(session, avant, instant, None)
    return {
        "mode": "réel",
        "ordre": str(ordre),
        "hauteur_avant_mm": hauteur_avant,
        "hauteur_apres_mm": session.etat.hauteur_coupe_mm if rapport["etat_rafraichi"] else None,
    } | rapport


def texte_voyants(transmis: bool | None, dernier: bool | None) -> str:
    """L'état des voyants LED tel qu'il est connu : transmis par le robot, sinon le dernier ordre envoyé."""
    if transmis is not None:
        return "allumés (d'après la configuration du robot)" if transmis else "éteints (d'après la configuration du robot)"
    if dernier is not None:
        return f"dernier ordre envoyé : {'allumés' if dernier else 'éteints'} (état réel non transmis)"
    return "état inconnu (non transmis)"


ATTENTE_PHARES_S = 4.0  # le robot annonce le nouveau niveau dans sa configuration, relue après l'ordre


def constat_phares(demande: bool, avant: bool | None, apres: bool | None) -> str:
    """Le robot a-t-il obéi ? D'après le niveau d'éclairage qu'il annonce lui-même."""
    if apres is None:
        return "phares : état non transmis après l'ordre — regardez le robot"
    etat = "allumés" if apres else "éteints"
    if apres == demande:
        return f"phares : {etat} d'après le robot"
    return f"phares : aucun changement ({etat} d'après le robot)"


async def executer_phares(session: Session, allumer: bool) -> dict[str, Any]:
    """Phares (éclairage de la caméra) : ordre sans mouvement, sans précondition de statut ; puis la
    configuration du robot est relue (requête pure) pour dire s'il a obéi."""
    trame = protocole.trame_phares(allumer, session.cal)
    avant = session.etat.phares
    session.journal.ecrire("ordre-emis", strict=True, ordre=trame.nom, sujet=session.sujet, trame=trame.description())
    instant = session.horloge()
    await session.publier(trame, securite.DELAI_ARRET_S)
    await session.solliciter()
    relu = await session.attendre("statut", instant, ATTENTE_PHARES_S)
    while relu and session.etat.phares != allumer and session.horloge() - instant < ATTENTE_PHARES_S:
        await session.dormir(0.4)  # la configuration peut suivre la trame d'état de peu
    apres = session.etat.phares if relu else None
    return {"mode": "réel", "ordre": trame.nom, "emis": True, "etat_rafraichi": relu, "constat": constat_phares(allumer, avant, apres)}


async def executer_voyants(session: Session, allumer: bool, relire: bool = True) -> dict[str, Any]:
    """Voyants LED du robot : un ordre sans mouvement, sans précondition de statut. `relire` : la configuration du
    robot est redemandée ensuite (requête pure) pour dire l'état qu'il annonce ; sans relecture, un seul message."""
    trame = protocole.trame_voyants(allumer, session.cal)
    session.journal.ecrire("ordre-emis", strict=True, ordre=trame.nom, sujet=session.sujet, trame=trame.description())
    instant = session.horloge()
    await session.publier(trame, securite.DELAI_ARRET_S)
    rapport: dict[str, Any] = {"mode": "réel", "ordre": trame.nom, "emis": True}
    if relire:
        await session.solliciter()
        relu = await session.attendre("statut", instant, 3.0)
        transmis = session.etat.voyants if relu else None
        rapport |= {"etat_rafraichi": relu, "voyants": texte_voyants(transmis, allumer)}
        if relu and transmis is not None and transmis != allumer:
            rapport["constat"] = "la configuration relue n'a pas (encore) changé : regardez le robot"
    return rapport


async def executer_signal_plateau(session: Session, nom: str) -> dict[str, Any]:
    """ESSAI : levée de nettoyage du plateau, ou redescente (voir `protocole.trame_signal_plateau`).

    SEC-01 strict : état frais, liste blanche, AUCUNE attestation (le plateau est un organe mobile). Après
    l'ordre, l'état est relu et l'outil dit ce qu'il a pu observer — le plus souvent rien : il ne reçoit
    aucune hauteur mesurée."""
    trame = protocole.trame_signal_plateau(nom, session.cal)
    avant = await _exiger_lames_arret(session)  # SEC-01, sans attestation
    etat = session.etat
    vu_avant = (etat.statut, tuple(etat.donnees.get("errorCodes") or ()), tuple(etat.donnees.get("warningCodes") or ()))
    session.journal.ecrire(
        "ordre-emis", strict=True, ordre=trame.nom, sujet=session.sujet, trame=trame.description(), essai=True,
    )
    instant = session.horloge()
    await session.publier(trame, securite.DELAI_ARRET_S)
    rapport = await constater(session, avant, instant, None)
    vu_apres = (etat.statut, tuple(etat.donnees.get("errorCodes") or ()), tuple(etat.donnees.get("warningCodes") or ()))
    if not rapport["etat_rafraichi"]:
        observe = "aucun état relu après l'ordre : effet inconnu"
    elif vu_apres == vu_avant:
        observe = "aucun changement observable par l'outil (il ne reçoit aucune hauteur mesurée) : regardez le plateau"
    else:
        resume = etat.resume(session.horloge())
        observe = (
            f"changement observé — statut : {resume['statut_libelle']} ; erreurs : {', '.join(resume['erreurs']) or 'aucune'} ; "
            f"avertissements : {', '.join(resume['avertissements']) or 'aucun'}"
        )
    return {"mode": "réel", "ordre": trame.nom, "essai": True, "observation": observe} | rapport


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
        if nom in ("reprendre", REPRISE_FORCEE) and etat.statut not in _STATUTS_REPRISE:
            libelle = LIBELLES_STATUT.get(etat.statut, str(etat.statut))
            raise RefusSecurite(f"« reprendre » refusé : rien à reprendre (statut : {libelle})")
        if nom == "reprendre":
            securite.verifier_precision_pour_reprise(etat.precision_m, etat.ecart_pour_la_garde)  # SEC-12
        elif nom == REPRISE_FORCEE:
            session.journal.ecrire(  # SEC-12 : la garde levée par l'opérateur laisse une trace
                "reprise-forcee", strict=True, precision_m=etat.precision_m,
                localisation=securite.qualite_position(etat.precision_m),
            )
        if nom == "pause":
            codes = [code_pause(etat.statut)]  # 21 pendant un retour à la base, 3 sinon : comme `stop`
        elif etat.statut == socle.WORK_STATUS_REMOTE_CONTROL:
            codes.insert(0, socle.USER_CTRL_EXIT_REMOTE)
    avant = etat.pose
    instant = session.horloge()
    for code in codes:
        trame = protocole.trame_tache(code, session.cal)
        session.journal.ecrire(
            "ordre-emis", strict=True, ordre=nom, sujet=session.sujet, trame=trame.description()
        )
        await session.publier(trame, securite.DELAI_ARRET_S)
    rapport: dict[str, Any] = {"mode": "réel", "ordre": nom, "codes_emis": codes}
    if nom in ("base", REPRISE_FORCEE) and securite.avertissement_position(etat.precision_m):
        # SEC-12 : avertissement sans blocage — le retour à la base aussi part d'une position incertaine.
        rapport["avertissement"] = (
            f"position imprécise ({securite.texte_precision(etat.precision_m)}) : le trajet automatique peut être erratique"
        )
    return rapport | await constater(session, avant, instant, None)


# --- SEC-04 : constat -------------------------------------------------------------


def _juger_translation(avant: Pose, apres: Pose, ordre_m: float) -> tuple[dict[str, Any], str | None]:
    distance, _rotation = avant.vers(apres)
    avance = avant.avance(apres)
    mesures = {"deplacement_m": round(distance, 3), "avance_m": round(avance, 3)}
    if distance < securite.SEUIL_DEPLACEMENT_M:
        return mesures, IMMOBILE
    if avance * ordre_m < 0:
        return mesures, f"déplacement dans le mauvais sens ({avance:+.2f} m mesurés pour {ordre_m:+g} m ordonnés)"
    if distance > securite.EXCES_FACTEUR * abs(ordre_m) + securite.EXCES_MARGE_M:
        return mesures, f"déplacement nettement supérieur à l'ordre ({distance:.2f} m pour {abs(ordre_m):g} m)"
    return mesures, None


def _juger_rotation(avant: Pose, apres: Pose, ordre_deg: float) -> tuple[dict[str, Any], str | None]:
    _distance, rotation = avant.vers(apres)
    mesures = {"rotation_deg": round(rotation, 1)}
    if abs(rotation) < securite.SEUIL_ROTATION_DEG:
        return mesures, IMMOBILE

    def ecart(cible: float) -> float:
        return abs((rotation - cible + 180.0) % 360.0 - 180.0)

    # Plus proche de l'ordre inverse que de l'ordre : mauvais sens (indécidable à 180°).
    if ecart(-ordre_deg) + securite.SENS_MARGE_DEG < ecart(ordre_deg):
        return mesures, f"rotation dans le mauvais sens ({rotation:+.0f}° mesurés pour {ordre_deg:+g}° ordonnés)"
    if abs(rotation) > securite.EXCES_FACTEUR * abs(ordre_deg) + securite.EXCES_MARGE_DEG:
        return mesures, f"rotation nettement supérieure à l'ordre ({abs(rotation):.0f}° pour {abs(ordre_deg):g}°)"
    return mesures, None


def _juger_arc(avant: Pose, apres: Pose, ordre_m: float, ordre_deg: float) -> tuple[dict[str, Any], str | None]:
    """Arc : distance ET rotation mesurées contre commandées (la distance mesurée est la corde de l'arc)."""
    distance, rotation = avant.vers(apres)
    avance = avant.avance(apres)
    corde = math.hypot(*securite.arrivee_arc(ordre_m, ordre_deg))
    mesures = {
        "deplacement_m": round(distance, 3), "avance_m": round(avance, 3), "rotation_deg": round(rotation, 1),
        "corde_attendue_m": round(corde, 3), "rotation_attendue_deg": round(ordre_deg, 1),
    }
    if distance < securite.SEUIL_DEPLACEMENT_M and abs(rotation) < securite.SEUIL_ROTATION_DEG:
        return mesures, IMMOBILE
    if avance * ordre_m < 0 and distance >= securite.SEUIL_DEPLACEMENT_M:
        return mesures, f"déplacement dans le mauvais sens ({avance:+.2f} m mesurés pour {ordre_m:+g} m ordonnés)"
    if abs(rotation) >= securite.SENS_MARGE_DEG and rotation * ordre_deg < 0:
        return mesures, f"rotation dans le mauvais sens ({rotation:+.0f}° mesurés pour {ordre_deg:+.0f}° ordonnés)"
    if distance > securite.EXCES_FACTEUR * corde + securite.EXCES_MARGE_M:
        return mesures, f"déplacement nettement supérieur à l'ordre ({distance:.2f} m pour {corde:.2f} m)"
    if abs(rotation) > securite.EXCES_FACTEUR * abs(ordre_deg) + securite.EXCES_MARGE_DEG:
        return mesures, f"rotation nettement supérieure à l'ordre ({abs(rotation):.0f}° pour {abs(ordre_deg):.0f}°)"
    return mesures, None


async def constater(
    session: Session, avant: Pose | None, instant: float, consigne: Consigne | None
) -> dict[str, Any]:
    """SEC-04 : relit l'état ; n'affirme un déplacement que sur des poses fraîches avant et après.

    Constat du 2026-10-12 (premier ordre réel) : le robot envoie son état toutes les demi-secondes
    après un ordre mais AUCUNE position ; attendre une pose bloquait tout 75 s. L'attente porte donc
    sur une trame d'état fraîche, 12 s au plus ; la pose n'est attendue que si le robot en donnait une
    juste avant l'ordre. `verification` : « vérifiée », « non vérifiable » (constat normal, la séquence
    continue), « anomalie » ou « aucun état » (SEC-07 : la séquence s'arrête).
    """
    await session.solliciter()
    rafraichi = await session.attendre("statut", instant, ATTENTE_CONSTAT_S)
    if rafraichi and avant is not None:
        reste = ATTENTE_CONSTAT_S - (session.horloge() - instant)
        await session.attendre("pose", instant, max(0.0, reste))
    recu_pose = session.etat.recu_pose
    apres = session.etat.pose if recu_pose is not None and recu_pose > instant else None
    precision = session.etat.precision_m if rafraichi else None
    rapport: dict[str, Any] = {
        # Aide à retrouver le signal : la précision de position après chaque pas.
        "precision_m": None if precision is None else round(precision, 2),
        "etat_rafraichi": rafraichi,
        "avant": _pose(avant),
        "apres": _pose(apres),
        "statut_apres": session.etat.resume(session.horloge())["statut_libelle"],
        "verifie": False,
        "verification": NON_VERIFIABLE,
    }
    if not rafraichi:
        rapport |= {
            "verification": AUCUN_ETAT,
            "constat": f"aucune trame d'état fraîche reçue en {ATTENTE_CONSTAT_S:g} s après l'ordre : liaison douteuse",
        }
    elif consigne is None:
        rapport |= {"verifie": True, "verification": VERIFIEE, "constat": "état relu après l'ordre"}
    elif apres is None or avant is None:
        rapport |= {"constat": POSITION_NON_FOURNIE, "cause_probable": diagnostic.CAUSE_POSE_RARE}
    else:
        if consigne.attendu == "arc":
            mesures, anomalie = _juger_arc(avant, apres, consigne.valeur, consigne.angle)
        else:
            juger = _juger_translation if consigne.attendu == "translation" else _juger_rotation
            mesures, anomalie = juger(avant, apres, consigne.valeur)
        rapport |= mesures | {
            "verifie": anomalie is None, "verification": ANOMALIE if anomalie else VERIFIEE,
            "constat": anomalie or "déplacement constaté",
        }
        if anomalie == IMMOBILE:  # la cause probable, selon le statut : en erreur, le robot ignore l'ordre
            rapport["cause_probable"] = diagnostic.cause_d_immobilite(session.etat.statut)
    return rapport


def _pose(pose: Pose | None) -> dict[str, float] | None:
    return None if pose is None else {"est_m": pose.est_m, "nord_m": pose.nord_m, "cap_deg": pose.cap_deg}


# --- SEC-07 : séquence --------------------------------------------------------------


async def executer(session: Session, ordre: Ordre) -> dict[str, Any]:
    if ordre.nom in MOUVEMENTS:
        return await executer_mouvement(session, ordre)
    if ordre.nom == ESSAI:
        return await executer_essai(session, ordre)
    if ordre.nom in PLATEAUX:
        return await executer_plateau(session, ordre)
    if ordre.nom == "stop":
        return await executer_arret(session)
    if ordre.nom in VOYANTS:
        return await executer_voyants(session, VOYANTS[ordre.nom])
    if ordre.nom in PHARES:
        return await executer_phares(session, PHARES[ordre.nom])
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


def _reprise(ordres: list[Ordre], rang: int) -> dict[str, Any]:
    """Étapes restantes, sous la forme que la page renvoie à la prévisualisation (« Reprendre à l'étape N »)."""
    return {
        "non_executees": [str(o) for o in ordres[rang:]],
        "reprise": {"etape": rang + 1, "ordres": [dataclasses.asdict(o) for o in ordres[rang:]]} if ordres[rang:] else None,
    }


async def executer_sequence(session: Session, ordres: list[Ordre], objectif_signal: bool = False) -> dict[str, Any]:
    """SEC-07 (révisé le 2026-10-12, décision du pilote au vu du réel) : mêmes règles par étape.

    La séquence s'arrête si (a) un ordre est refusé ou échoue, (b) l'arrêt n'est pas confirmé,
    (c) le statut reçu sort de la liste blanche, (d) aucune trame d'état fraîche n'arrive dans le
    délai, (e) une pose EST fournie et contredit l'ordre. Elle ne s'arrête plus parce que le robot
    ne fournit pas sa position. Chaque étape repasse par SEC-01, qui exige un état frais.
    """
    bilan: dict[str, Any] = {"mode": "réel", "etapes": [], "interrompue": False}
    refus = refus_prealable(ordres, session.cal)
    if refus:
        return bilan | refus
    for rang, ordre in enumerate(ordres):
        try:
            rapport = await executer(session, ordre)
        except (RefusSecurite, EchecOrdre, OSError) as erreur:
            etape = {"ordre": str(ordre), "issue": "non émise ou échouée", "echec": str(erreur)}
            if isinstance(erreur, RefusSecurite):
                etape["refus_avant_emission"] = True  # rien n'est parti : l'appelant peut garder la main
            bilan["etapes"].append(etape)
            non_confirme = isinstance(erreur, ArretNonConfirme)
            return bilan | {
                "interrompue": True,
                "motif": f"« {ordre} » a échoué : {erreur}",
                "arret_non_confirme": non_confirme,
                "non_executees": [str(o) for o in ordres[rang + 1 :]],
                # Reprise à l'étape en échec (elle repasse par la prévisualisation) ; aucune après un
                # arrêt non confirmé : le robot doit d'abord être arrêté à la main.
                "reprise": None if non_confirme else _reprise(ordres, rang)["reprise"],
            }
        rapport["issue"] = f"émise, {rapport.get('verification', NON_VERIFIABLE)}"
        bilan["etapes"].append(rapport)
        if not ordres[rang + 1 :]:
            break
        if objectif_signal and securite.qualite_position(session.etat.precision_m) == securite.FIABLE:
            # Trajet « vers le bon signal » : la précision est redevenue fiable — arrêt NORMAL, annoncé.
            reste = [str(o) for o in ordres[rang + 1 :]]
            return bilan | {
                "objectif_atteint": True, "non_executees": reste,
                "motif": f"précision retrouvée ({securite.texte_precision(session.etat.precision_m)}) : objectif atteint, "
                         f"séquence arrêtée — {len(reste)} étape(s) non exécutée(s)",
            }
        motif = None
        if rapport.get("verification") in (ANOMALIE, AUCUN_ETAT):
            motif = rapport["constat"]
        else:
            try:  # (c) : dit ici, avec le statut en clair, plutôt qu'au refus de l'étape suivante
                securite.verifier_presence_et_statut(
                    session.etat.statut, session.etat.en_ligne, _attestee(session)  # SEC-01b
                )
            except RefusSecurite:
                motif = f"le robot est passé en « {rapport['statut_apres']} », hors des statuts où un mouvement est permis"
        if motif:
            return bilan | {"interrompue": True, "motif": f"séquence interrompue après « {ordre} » : {motif}"} | _reprise(ordres, rang + 1)
    return bilan
