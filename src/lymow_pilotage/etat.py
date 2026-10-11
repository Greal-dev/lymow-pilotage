"""État du robot reconstitué à partir des trames reçues (lecture seule).

Rôle : fusionner les fragments décodés, dater chaque information à sa
réception, fournir libellés et résumé affichable. N'émet rien.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .vendor.ha_lymow import const as socle

LIBELLES_STATUT: dict[int, str] = {
    socle.WORK_STATUS_OFFLINE: "inconnu ou hors ligne",
    socle.WORK_STATUS_NONE: "au repos à la station",
    socle.WORK_STATUS_WAITING: "prêt, en attente d'ordre",
    socle.WORK_STATUS_MOWING: "tonte en cours",
    socle.WORK_STATUS_PAUSE: "tonte en pause",
    socle.WORK_STATUS_DOCKING: "retour à la base",
    socle.WORK_STATUS_CHARGING: "en charge",
    socle.WORK_STATUS_REMOTE_CONTROL: "télécommande manuelle",
    socle.WORK_STATUS_ERROR: "en erreur",
    socle.WORK_STATUS_RESUME: "reprise de tonte",
    socle.WORK_STATUS_ZONE_PARTITION: "tonte d'une zone",
    socle.WORK_STATUS_PAUSE_DOCKING: "retour à la base en pause",
    socle.WORK_STATUS_UPDATING: "mise à jour du micrologiciel",
    socle.WORK_STATUS_CHARGING_FULL: "charge terminée",
    socle.WORK_STATUS_EMERGENCY_STOP: "arrêt d'urgence",
    socle.WORK_STATUS_ESCAPING: "dégagement d'obstacle",
    socle.WORK_STATUS_RTT: "essai d'usine",
    socle.WORK_STATUS_AGING_TEST: "essai d'usine (vieillissement)",
}

_CLES_POSE = ("poseEastM", "poseNorthM", "poseThetaRad")


@dataclass(frozen=True)
class Pose:
    est_m: float
    nord_m: float
    cap_deg: float

    def vers(self, autre: Pose) -> tuple[float, float]:
        """Distance parcourue (m) et rotation signée (degrés, positif = gauche)."""
        distance = math.hypot(autre.est_m - self.est_m, autre.nord_m - self.nord_m)
        rotation = (autre.cap_deg - self.cap_deg + 180.0) % 360.0 - 180.0
        return distance, rotation

    def avance(self, autre: Pose) -> float:
        """Déplacement signé le long du cap de départ (positif = vers l'avant).

        Suppose le cap mesuré depuis l'est, sens trigonométrique (repère ENU du socle).
        """
        cap = math.radians(self.cap_deg)
        return (autre.est_m - self.est_m) * math.cos(cap) + (autre.nord_m - self.nord_m) * math.sin(cap)


@dataclass
class Etat:
    donnees: dict[str, Any] = field(default_factory=dict)
    en_ligne: bool | None = None
    recu_statut: float | None = None
    recu_pose: float | None = None

    def appliquer(self, fragment: dict[str, Any], instant: float) -> None:
        """Fusionne un fragment décodé ; `instant` = horloge monotone à la réception."""
        if "workStatus" in fragment:
            self.recu_statut = instant
        else:
            # Une réponse partielle ne doit pas effacer les codes déjà connus.
            fragment = {c: v for c, v in fragment.items() if c not in ("errorCodes", "errorCode", "warningCodes")}
        if all(isinstance(fragment.get(c), float) and math.isfinite(fragment[c]) for c in _CLES_POSE):
            self.recu_pose = instant
        else:
            fragment = {c: v for c, v in fragment.items() if c not in _CLES_POSE}
        self.donnees.update(fragment)

    @property
    def statut(self) -> int | None:
        valeur = self.donnees.get("workStatus")
        return valeur if isinstance(valeur, int) else None

    def age_statut(self, instant: float) -> float | None:
        return None if self.recu_statut is None else instant - self.recu_statut

    @property
    def pose(self) -> Pose | None:
        if self.recu_pose is None:
            return None
        est, nord, theta = (self.donnees[c] for c in _CLES_POSE)
        return Pose(round(est, 3), round(nord, 3), round(math.degrees(theta), 1))

    @property
    def hauteur_coupe_mm(self) -> int | None:
        return self.donnees.get("rcCutHeight")

    def resume(self, instant: float) -> dict[str, Any]:
        d = self.donnees
        pose = self.pose
        age = self.age_statut(instant)
        rtk = d.get("rtkStatus")
        return {
            "en_ligne": self.en_ligne,
            "batterie_pct": d.get("battery"),
            "statut": self.statut,
            "statut_libelle": _libelle_statut(self.statut),
            "erreurs": _libelles(d.get("errorCodes"), socle.ERROR_DESCRIPTIONS),
            "avertissements": _libelles(d.get("warningCodes"), socle.WARNING_DESCRIPTIONS),
            "position_est_m": pose.est_m if pose else None,
            "position_nord_m": pose.nord_m if pose else None,
            "cap_deg": pose.cap_deg if pose else None,
            "age_position_s": None if self.recu_pose is None else round(instant - self.recu_pose, 1),
            "rtk": None if rtk is None else socle.RTK_SIGNAL_QUALITY.get(rtk, f"inconnu ({rtk})"),
            "rtk_satellites": d.get("rtkSatellites"),
            "signal_4g": d.get("lteSignalQuality"),
            "signal_wifi": d.get("wifiSignalQuality"),
            "wifi_rssi_dbm": d.get("wifiRssiDbm"),
            "hauteur_coupe_mm": self.hauteur_coupe_mm,
            "micrologiciel": d.get("softwareVersion") or d.get("fwVersion"),
            "age_etat_s": None if age is None else round(age, 1),
        }


def _libelle_statut(statut: int | None) -> str:
    if statut is None:
        return "aucun état reçu"
    return LIBELLES_STATUT.get(statut, f"statut inconnu ({statut})")


def _libelles(codes: Any, table: dict[int, str]) -> list[str]:
    if not isinstance(codes, list):
        return []
    return [f"{code} : {table.get(code, 'code inconnu')}" for code in codes if code]
