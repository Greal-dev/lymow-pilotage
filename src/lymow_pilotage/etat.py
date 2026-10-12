"""État du robot reconstitué à partir des trames reçues (lecture seule).

Rôle : fusionner les fragments décodés, dater chaque information à sa
réception, fournir libellés et résumé affichable. N'émet rien.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from . import securite
from .vendor.ha_lymow import const as socle

CLE_STATUT, CLE_TACHE = "robotState", "workStatus"
# Au-delà, les corrections de la base sont tenues pour perdues (elles arrivent chaque seconde en temps normal).
AGE_CORRECTIONS_MAX_S = 10.0
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
    # Origine de la carte (latitude, longitude), posée par l'appelant quand une carte est connue : sans
    # elle, la position satellite brute ne se compare pas à la pose.
    origine: tuple[float, float] | None = None
    recu_rtk: float | None = None

    def appliquer(self, fragment: dict[str, Any], instant: float) -> None:
        """Fusionne un fragment décodé ; `instant` = horloge monotone à la réception."""
        # Constat du 2026-10-12 (trames réelles, captures d'ordres) : l'état VIVANT du robot est
        # `robotStatus`, champ 1 de robotInfo (clé `robotState` du socle). `workStatus` (champ 6)
        # reste à « tonte » pendant toute la tâche, pause comprise : ce n'est que la tâche en cours.
        # Une trame d'état sans champ 1 laisse le statut INCONNU — jamais de repli sur le champ 6.
        if isinstance(fragment.get(CLE_STATUT), int) and not isinstance(fragment.get(CLE_STATUT), bool):
            self.recu_statut = instant
        elif CLE_TACHE in fragment:
            self.donnees.pop(CLE_STATUT, None)
            fragment = {c: v for c, v in fragment.items() if c != CLE_STATUT}
        if CLE_TACHE not in fragment:
            # Une réponse partielle ne doit pas effacer les codes déjà connus.
            fragment = {c: v for c, v in fragment.items() if c not in ("errorCodes", "errorCode", "warningCodes")}
        if any(c in fragment for c in ("rtkL1", "rtkL2", "rtkDiagnostic")):
            self.recu_rtk = instant
        if all(isinstance(fragment.get(c), float) and math.isfinite(fragment[c]) for c in _CLES_POSE):
            self.recu_pose = instant
        else:
            fragment = {c: v for c, v in fragment.items() if c not in _CLES_POSE}
        self.donnees.update(fragment)

    @property
    def statut(self) -> int | None:
        """Statut vivant du robot (robotStatus, f1) : le seul que SEC-01 juge."""
        valeur = self.donnees.get(CLE_STATUT)
        return valeur if isinstance(valeur, int) and not isinstance(valeur, bool) else None

    @property
    def tache(self) -> int | None:
        """Tâche en cours (workStatus, f6) : information, jamais un critère de sécurité."""
        valeur = self.donnees.get(CLE_TACHE)
        return valeur if isinstance(valeur, int) and not isinstance(valeur, bool) and valeur >= 0 else None

    def age_statut(self, instant: float) -> float | None:
        return None if self.recu_statut is None else instant - self.recu_statut

    @property
    def pose(self) -> Pose | None:
        if self.recu_pose is None:
            return None
        est, nord, theta = (self.donnees[c] for c in _CLES_POSE)
        return Pose(round(est, 3), round(nord, 3), round(math.degrees(theta), 1))

    @property
    def precision_m(self) -> float | None:
        """Précision horizontale annoncée par le robot, en mètres (clé « rtkEastM » du socle, mal nommée)."""
        valeur = self.donnees.get("rtkEastM")
        return valeur if isinstance(valeur, float) and math.isfinite(valeur) and 0 <= valeur < 1000 else None

    @property
    def age_corrections_s(self) -> float | None:
        """Âge des corrections de la base, tel que le robot l'annonce (diagnostic RTK)."""
        for bloc, cle in (("rtkL2", "differentialAgeSec"), ("rtkDiagnostic", "diffAgeS")):
            valeur = (self.donnees.get(bloc) or {}).get(cle)
            if isinstance(valeur, (int, float)) and not isinstance(valeur, bool) and math.isfinite(valeur) and valeur >= 0:
                return float(valeur)
        return None

    @property
    def position_brute(self) -> tuple[float, float, bool] | None:
        """Position satellite brute dans le repère de la carte (est, nord) ; le troisième terme dit si
        elle est assez fine pour être comparée à la pose (coordonnées en double précision)."""
        lla = self.donnees.get("llaCoords")
        if not isinstance(lla, dict) or self.origine is None:
            return None
        lat, lon = lla.get("lat"), lla.get("lon")
        if not all(isinstance(v, float) and math.isfinite(v) for v in (lat, lon)):
            return None
        rayon = 6_378_137.0
        nord = math.radians(lat - self.origine[0]) * rayon
        est = math.radians(lon - self.origine[1]) * rayon * math.cos(math.radians(self.origine[0]))
        return est, nord, bool(lla.get("double"))

    @property
    def ecart_brut_m(self) -> tuple[float, bool] | None:
        """Écart entre position satellite brute et position calculée par le robot, et s'il est fiable."""
        brute, pose = self.position_brute, self.pose
        if brute is None or pose is None:
            return None
        return math.hypot(brute[0] - pose.est_m, brute[1] - pose.nord_m), brute[2]

    @property
    def ecart_pour_la_garde(self) -> float | None:
        """L'écart, seulement s'il est mesurable assez finement pour juger (SEC-12)."""
        ecart = self.ecart_brut_m
        return ecart[0] if ecart is not None and ecart[1] else None

    def detail_localisation(self) -> str:
        """Ce qui distingue un lien de base perdu d'un ciel masqué : âge des corrections, base, écart."""
        morceaux = []
        age = self.age_corrections_s
        if age is not None:
            morceaux.append(
                f"corrections de la base reçues il y a {age:.0f} s" if age <= AGE_CORRECTIONS_MAX_S
                else f"plus de corrections depuis {age:.0f} s"
            )
        base = (self.donnees.get("rtkL1") or {}).get("baseStationStatus")
        if base == "offline":
            morceaux.append("base hors ligne")
        erreur = (self.donnees.get("rtkDiagnostic") or {}).get("primaryError")
        if isinstance(erreur, str) and erreur:
            morceaux.append(f"motif annoncé : {erreur}")
        ecart = self.ecart_brut_m
        if ecart is not None:
            texte = f"écart position satellite / position calculée : {ecart[0]:.2f} m".replace(".", ",")
            morceaux.append(texte if ecart[1] else texte + " (indicatif : coordonnées en simple précision)")
        return " ; ".join(morceaux)

    def _eclairage(self, cle: str) -> bool | None:
        """Niveau d'éclairage annoncé par le robot : 1 à 3 = allumé, 4 = éteint ; sinon inconnu."""
        niveau = self.donnees.get(cle)
        if niveau in (1, 2, 3):
            return True
        return False if niveau == 4 else None

    @property
    def phares(self) -> bool | None:
        """Phares (éclairage de la caméra), d'après la configuration du robot."""
        return self._eclairage("camLedStatus")

    @property
    def voyants(self) -> bool | None:
        """Voyants LED du robot, d'après sa configuration."""
        return self._eclairage("vehLedStatus")

    @property
    def plage_des_phares(self) -> str | None:
        """Plage d'allumage des phares, en heure locale (« 20:30–21:30 ») ; chaîne vide si aucune ; rien si inconnue."""
        debut, fin = self.donnees.get("openLedTime"), self.donnees.get("closeLedTime")
        if not (isinstance(debut, list) and isinstance(fin, list)):
            return None
        if debut == fin:
            return ""
        base = datetime.now(timezone.utc)
        locales = [base.replace(hour=h, minute=m).astimezone().strftime("%H:%M") for h, m in (debut, fin)]
        return "–".join(locales)

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
            # La tâche n'est dite que si elle diffère du statut vivant (« en pause — tâche : tonte en cours »).
            "tache": self.tache,
            "tache_libelle": _libelle_statut(self.tache) if self.tache not in (None, self.statut) else None,
            "erreurs": _libelles(d.get("errorCodes"), socle.ERROR_DESCRIPTIONS),
            "avertissements": _libelles(d.get("warningCodes"), socle.WARNING_DESCRIPTIONS),
            "position_est_m": pose.est_m if pose else None,
            "position_nord_m": pose.nord_m if pose else None,
            "cap_deg": pose.cap_deg if pose else None,
            "age_position_s": None if self.recu_pose is None else round(instant - self.recu_pose, 1),
            "rtk": _libelle_rtk(rtk, d.get("rtkEastM")),
            "rtk_satellites": d.get("rtkSatellites"),
            # SEC-12 : la qualité de localisation se lit sur la précision en mètres, à chaque trame.
            "precision_m": None if self.precision_m is None else round(self.precision_m, 2),
            "localisation": securite.qualite_position(self.precision_m),
            "localisation_detail": self.detail_localisation(),
            "age_corrections_s": self.age_corrections_s,
            "ecart_brut_m": None if self.ecart_brut_m is None else round(self.ecart_brut_m[0], 2),
            "position_brute": (
                None if self.position_brute is None else [round(self.position_brute[0], 2), round(self.position_brute[1], 2)]
            ),
            "localisation_texte": (
                f"position : {securite.texte_precision(self.precision_m)} — {securite.qualite_position(self.precision_m)}"
            ),
            "signal_4g": d.get("lteSignalQuality"),
            "signal_wifi": d.get("wifiSignalQuality"),
            "wifi_rssi_dbm": d.get("wifiRssiDbm"),
            "hauteur_coupe_mm": self.hauteur_coupe_mm,
            "phares": self.phares,
            "voyants": self.voyants,
            "micrologiciel": d.get("softwareVersion") or d.get("fwVersion"),
            "age_etat_s": None if age is None else round(age, 1),
        }


def _libelle_rtk(qualite: Any, precision: Any) -> str | None:
    """Précision horizontale en mètres d'abord ; le libellé de qualité n'est qu'une indication.

    Constat du 2026-10-12 : le sous-champ 2 de la localisation, que le socle nomme « est », vaut
    quelques centimètres robot à sa station, loin de l'origine : c'est la précision horizontale
    (schéma de Lymow-HA). Pour la qualité, les sources se contredisent (1 = point seul, ou flottant)."""
    morceaux = []
    if isinstance(precision, float) and math.isfinite(precision) and 0 <= precision < 1000:
        morceaux.append(f"précision horizontale {precision:.2f} m")
    if isinstance(qualite, int) and not isinstance(qualite, bool):
        morceaux.append(f"{socle.RTK_SIGNAL_QUALITY.get(qualite, f'qualité {qualite}')} (libellé non confirmé)")
    return " — ".join(morceaux) or None


def _libelle_statut(statut: int | None) -> str:
    if statut is None:
        return "aucun état reçu"
    return LIBELLES_STATUT.get(statut, f"statut inconnu ({statut})")


def _libelles(codes: Any, table: dict[int, str]) -> list[str]:
    if not isinstance(codes, list):
        return []
    return [f"{code} : {table.get(code, 'code inconnu')}" for code in codes if code]
