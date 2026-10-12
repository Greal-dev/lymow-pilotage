"""Trames de pilotage : encodeurs, sujets MQTT, catégories et listes blanches.

Rôle : fabriquer les seuls messages que l'outil sait émettre. Ne publie rien.
Les dispositions de trame viennent de Lymow-HA (commit f38a4da, MIT) :
`encode_remote_control`, `encode_remote_stop`, `encode_set_cut_height`,
réécrites ici sans dépendance protobuf. Aucun encodeur de démarrage de tonte,
de réglage de débogage (`exec_cmd`) ni de Bluetooth n'existe dans ce module.
"""

from __future__ import annotations

import base64
import enum
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from . import securite
from .config import Calibration
from .vendor.ha_lymow import const as socle
from .vendor.ha_lymow import protocol as pb

# Champs de PbInput (protobuf proto2, relevés dans les deux dépôts sources).
CHAMP_VERSION = 2
CHAMP_USER_CTRL = 5
CHAMP_APP_CONNECT = 7
CHAMP_BASE_INPUT = 9
CHAMP_REMOTE_CONTROL = 10
CHAMP_MAP = 12
CHAMP_UUID = 27
CHAMP_MAP_RUN_TIME_CONFIG = 13
CHAMP_BT_MAP = 23
VERSIONS = (49, 40)

_APPAREIL_VALIDE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_ZONE_VALIDE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Categorie(enum.Enum):
    REQUETE_PURE = "requête pure"
    MOUVEMENT = "mouvement"
    ARRET = "arrêt"
    TACHE = "tâche"
    REGLAGE = "réglage"
    CARTE = "écriture de carte"


# SEC-01 : seuls codes userCtrl émissibles. Aucun ne démarre une tonte neuve ;
# `reprendre` (4) est le seul qui puisse relancer les lames, sur demande explicite.
USER_CTRL_AUTORISES: dict[int, str] = {
    socle.USER_CTRL_RESUME: "reprendre la tâche",
    socle.USER_CTRL_RECHARGE_DOCK: "retour à la base, tâche conservée",
    socle.USER_CTRL_FORCE_REINIT: "annuler la tâche, arrêt sur place",
    socle.USER_CTRL_EXIT_REMOTE: "quitter le mode télécommande",
    socle.USER_CTRL_PAUSE: "mettre la tâche en pause",
    socle.USER_CTRL_PAUSE_DOCK: "mettre le retour à la base en pause",
}


@dataclass(frozen=True)
class Trame:
    nom: str
    categorie: Categorie
    octets: bytes
    champs: dict[str, Any] = field(default_factory=dict)

    def charge(self) -> str:
        """Enveloppe JSON `{"message": base64}` publiée telle quelle."""
        return pb.wrap_envelope(self.octets)

    def description(self) -> dict[str, Any]:
        return {
            "nom": self.nom,
            "categorie": self.categorie.value,
            "champs": self.champs,
            "hexadecimal": self.octets.hex(" "),
            "base64": base64.b64encode(self.octets).decode(),
        }


def _sujet(appareil: str, suffixe: str) -> str:
    # L'identifiant vient du nuage : un « / », « # » ou « + » détournerait le sujet.
    if not _APPAREIL_VALIDE.fullmatch(appareil):
        raise ValueError("identifiant d'appareil invalide")
    return f"/device/{appareil}/{suffixe}"


def sujet_ordres(appareil: str) -> str:
    return _sujet(appareil, "pbinput")


def sujet_etat(appareil: str) -> str:
    return _sujet(appareil, "pboutput")


def sujet_presence(appareil: str) -> str:
    return _sujet(appareil, "notify-app")


def _version(version: int) -> bytes:
    if version not in VERSIONS:
        raise ValueError("version de protocole inconnue")
    return pb._field_i32(CHAMP_VERSION, version)


def encode_remote_control(
    lineaire: float, angulaire: float, *, variante: str = "mqtt", version: int = 49
) -> bytes:
    """PbInput{version, remoteControl(10){linearSpeed(1), angularSpeed(2)}}.

    Variante `ble` : disposition de la trame Bluetooth du socle, champ 7 = 2 en plus.
    """
    securite.verifier_vitesses(lineaire, angulaire)  # SEC-02
    corps = pb._field_f32(1, lineaire) + pb._field_f32(2, angulaire)
    entete = _version(version)
    if variante == "ble":
        entete += pb._field_i32(CHAMP_APP_CONNECT, 2)
    elif variante != "mqtt":
        raise ValueError("variante de trame inconnue")
    return entete + pb._field_bytes(CHAMP_REMOTE_CONTROL, corps)


def encode_remote_stop(*, variante: str = "mqtt", version: int = 49) -> bytes:
    """Consigne nulle explicite (les deux vitesses à 0.0 sont bien sérialisées)."""
    return encode_remote_control(0.0, 0.0, variante=variante, version=version)


def encode_set_cut_height(millimetres: int, *, variante: str = "socle", version: int = 49) -> bytes:
    """PbInput{version, [userCtrl=50], map(12){runTimeConfig(13){cutHeight(1)}}}.

    `socle` : avec userCtrl 50 (ha-lymow). `lymow-ha` : sans userCtrl (Lymow-HA).
    """
    securite.verifier_hauteur(millimetres)  # SEC-02
    carte = pb._field_bytes(CHAMP_MAP_RUN_TIME_CONFIG, pb._field_i32(1, millimetres))
    entete = _version(version)
    if variante == "socle":
        entete += pb._field_i32(CHAMP_USER_CTRL, socle.USER_CTRL_SET_RUN_TIME_CONFIG)
    elif variante != "lymow-ha":
        raise ValueError("variante de hauteur inconnue")
    return entete + pb._field_bytes(CHAMP_MAP, carte)


def encode_ordre_tache(code: int, *, version: int = 49) -> bytes:
    # SEC-01 : liste blanche ; démarrer une tonte (1, 5, 46…) est impossible par construction.
    if code not in USER_CTRL_AUTORISES:
        raise securite.RefusSecurite(f"code userCtrl {code} interdit (SEC-01)")
    return _version(version) + pb._field_i32(CHAMP_USER_CTRL, code)


def trame_consigne(lineaire: float, angulaire: float, cal: Calibration) -> Trame:
    octets = encode_remote_control(
        lineaire, angulaire, variante=cal.variante_trame, version=cal.version_protocole
    )
    nul = lineaire == 0 and angulaire == 0
    return Trame(
        "arrêt télécommande" if nul else "consigne télécommande",
        Categorie.ARRET if nul else Categorie.MOUVEMENT,
        octets,
        {
            "version": cal.version_protocole,
            "variante": cal.variante_trame,
            "vitesse_lineaire_m_s": _f32(lineaire),
            "vitesse_angulaire_rad_s": _f32(angulaire),
        },
    )


def trame_arret(cal: Calibration) -> Trame:
    return trame_consigne(0.0, 0.0, cal)


def trame_hauteur(millimetres: int, cal: Calibration) -> Trame:
    octets = encode_set_cut_height(
        millimetres, variante=cal.variante_hauteur, version=cal.version_protocole
    )
    champs = {
        "version": cal.version_protocole,
        "variante": cal.variante_hauteur,
        "hauteur_coupe_mm": millimetres,
    }
    return Trame("hauteur de coupe", Categorie.REGLAGE, octets, champs)


def trame_tache(code: int, cal: Calibration) -> Trame:
    octets = encode_ordre_tache(code, version=cal.version_protocole)
    champs = {"version": cal.version_protocole, "userCtrl": code}
    return Trame(USER_CTRL_AUTORISES[code], Categorie.TACHE, octets, champs)


def trame_presence(identifiant_session: str) -> Trame:
    """Battement « application connectée » : le robot diffuse son état à un client présent."""
    octets = pb.encode_app_connect_heartbeat(identifiant_session)
    return Trame("présence application", Categorie.REQUETE_PURE, octets, {"appConnect": 2})


def trame_demande_configuration(cal: Calibration) -> Trame:
    octets = pb.encode_query_robot_config(cal.version_protocole)
    champs = {"version": cal.version_protocole, "demande": "configuration du robot"}
    return Trame("demande de configuration", Categorie.REQUETE_PURE, octets, champs)


# Constat du 2026-10-12 (première lecture réelle, capture hors dépôt) : le robot répond à la
# demande de carte par UN message dont le contenu est coupé net à 81 920 octets (0x14000),
# au milieu d'un champ. Une grande carte tient donc en plusieurs tranches de cette taille.
TAILLE_TRANCHE_CARTE = 81920
# Hypothèse NON vérifiée en réel : la tranche suivante se demande par l'index de la requête
# (`queryIndex`, 0 = première). Borne : huit tranches, soit 640 ko de carte.
TRANCHES_CARTE_MAX = 8


def trame_demande_carte(cal: Calibration, index: int = 0) -> Trame:
    """Demande d'une tranche de la carte (userCtrl 19) : une lecture, rien n'est modifié."""
    if not 0 <= index < TRANCHES_CARTE_MAX:
        raise ValueError("index de tranche hors bornes")
    octets = pb.encode_query_map(index, cal.version_protocole)
    champs = {
        "version": cal.version_protocole, "userCtrl": socle.USER_CTRL_QUERY_MAP,
        "demande": "carte entière" if index == 0 else f"carte, tranche {index + 1}",
    }
    return Trame("demande de carte", Categorie.REQUETE_PURE, octets, champs)


def trame_contour_zone(identifiant: str, contour: list[tuple[float, float]], cal: Calibration) -> Trame:
    """Retouche du contour d'UNE zone de tonte : userCtrl 9 + zone{identifiant, polygone}.

    Seule écriture de carte de l'outil ; c'est la seule que le socle dit vérifiée en réel.
    L'envoi de carte entière (que le robot ignore) n'existe pas ici. Bornes : SEC-10, dans `retouche`.
    """
    if not _ZONE_VALIDE.fullmatch(identifiant):
        raise ValueError("identifiant de zone invalide")
    if not 3 <= len(contour) <= securite.SOMMETS_MAX:
        raise securite.RefusSecurite("contour refusé : nombre de sommets hors bornes (SEC-10)")
    points = []
    for x, y in contour:
        securite._nombre_fini(x, "coordonnée")
        securite._nombre_fini(y, "coordonnée")
        points.append({"x": x, "y": y})
    octets = pb.encode_set_zone_polygon(identifiant, points, cal.version_protocole)
    champs = {
        "version": cal.version_protocole, "userCtrl": socle.USER_CTRL_MODIFY_ZONE_INFO,
        "zone": identifiant, "sommets": len(contour),
    }
    return Trame("retouche du contour d'une zone", Categorie.CARTE, octets, champs)


def est_requete_pure(octets: bytes) -> bool:
    """Liste blanche structurelle des messages émissibles en lecture seule.

    Trois formes exactes, rien d'autre :
    - demande de carte : {version(2), userCtrl(5)=19, champ 23={1: index de tranche, 4: 1}},
      index de 0 à TRANCHES_CARTE_MAX - 1 (lecture d'une grande carte, constat du 2026-10-12) ;
    - présence : {appConnect(7)=2, uuid(27)} ;
    - demande de configuration : {version(2), baseInput(9)={champ 10 = 1}}.
    """
    try:
        champs = pb._decode_fields(octets)
        numeros = sorted(numero for numero, _type, _valeur in champs)
        if numeros == [CHAMP_APP_CONNECT, CHAMP_UUID]:
            return pb._first(champs, CHAMP_APP_CONNECT) == 2
        if numeros == [CHAMP_VERSION, CHAMP_USER_CTRL, CHAMP_BT_MAP]:
            demande = pb._first(champs, CHAMP_BT_MAP)
            return (
                pb._first(champs, CHAMP_VERSION) in VERSIONS
                and pb._first(champs, CHAMP_USER_CTRL) == socle.USER_CTRL_QUERY_MAP
                and isinstance(demande, bytes)
                and any(
                    octets == pb.encode_query_map(index, pb._first(champs, CHAMP_VERSION))
                    for index in range(TRANCHES_CARTE_MAX)
                )
            )
        if numeros == [CHAMP_VERSION, CHAMP_BASE_INPUT]:
            interne = pb._first(champs, CHAMP_BASE_INPUT)
            return (
                pb._first(champs, CHAMP_VERSION) in VERSIONS
                and isinstance(interne, bytes)
                and pb._decode_fields(interne) == [(10, 0, 1)]
            )
    except (IndexError, struct.error):
        pass
    return False


def _f32(valeur: float) -> float:
    """Valeur réellement portée par la trame (arrondi simple précision)."""
    return round(struct.unpack("<f", struct.pack("<f", valeur))[0], 6)
