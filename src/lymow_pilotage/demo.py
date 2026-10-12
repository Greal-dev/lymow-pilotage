"""Mode démonstration : un robot et un jardin FICTIFS, entièrement en mémoire.

Rôle : tenir lieu de liaison (`connecte`, `publier`) pour une `Session` ordinaire,
afin d'essayer l'interface de bout en bout sans compte, sans coffre et sans
réseau. Le robot fictif lit les mêmes trames que le vrai, se déplace quand on
le pilote et applique la retouche de contour. Rien d'ici ne parle au nuage.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from . import carte as cartes
from .securite import CADENCE_HZ
from .vendor.ha_lymow import protocol as pb

APPAREIL = "robot-fictif"
MENTION = "DÉMONSTRATION — robot fictif"
# Délai de réponse du robot fictif : une réponse doit être datée APRÈS la demande.
DELAI_REPONSE_S = 0.03
# Comme le vrai, le robot fictif donne de ses nouvelles sans qu'on le lui demande.
PERIODE_ETAT_S = 5.0


# --- construction d'une carte brute (sert aussi de jeu d'essai aux tests) ------------


def polygone_brut(points: Any) -> bytes:
    return b"".join(pb._field_bytes(1, pb._field_f32(1, x) + pb._field_f32(2, y)) for x, y in points)


def _aire(points: Any) -> float:
    points = list(points)
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1])) / 2)


def zone_brute(identifiant: str, nom: str, points: Any, parent: str | None = None, reglages: bytes = b"\x08\x28\x48\x0a") -> bytes:
    infos = pb._field_i32(1, 1) + pb._field_str(2, nom) + pb._field_str(3, identifiant) + pb._field_i32(4, 1)
    infos += pb._field_bytes(5, polygone_brut(points))
    brut = pb._field_bytes(1, infos) + pb._field_bytes(2, reglages) + pb._field_bytes(3, pb._field_f32(3, _aire(points)))
    return brut + (pb._field_str(4, parent) if parent else b"")


def passage_brut(identifiant: str, zone1: str, zone2: str, points: Any, station: bool = False) -> bytes:
    brut = pb._field_str(1, identifiant) + pb._field_str(2, zone1) + pb._field_str(3, zone2) + pb._field_i32(4, 1)
    return brut + pb._field_bytes(5, polygone_brut(points)) + pb._field_i32(6, 1 if station else 0)


def _cote(a: tuple[float, float], b: tuple[float, float], pas: float = 4.0) -> list[tuple[float, float]]:
    """Points de `a` (inclus) vers `b` (exclu), tous les `pas` mètres : de quoi choisir une portion."""
    n = max(1, round(math.hypot(b[0] - a[0], b[1] - a[1]) / pas))
    return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]


def _contour(*coins: tuple[float, float]) -> list[tuple[float, float]]:
    return [p for a, b in zip(coins, coins[1:] + coins[:1]) for p in _cote(a, b)]


def jardin() -> bytes:
    """Jardin à la française : deux parterres, une allée en équerre, un bassin, un massif."""
    sud = _contour((0, 0), (16, 0), (16, 8), (0, 8))
    nord = _contour((0, 12), (16, 12), (16, 20), (0, 20))
    equerre = _contour((18, 0), (26, 0), (26, 20), (22, 20), (22, 6), (18, 6))  # concave
    bassin = [(8 + 1.5 * math.cos(math.radians(a)), 16 + 1.5 * math.sin(math.radians(a))) for a in range(0, 360, 45)]
    brut = pb._field_bytes(1, zone_brute("parterre-sud", "Parterre sud", sud))
    brut += pb._field_bytes(1, zone_brute("parterre-nord", "Parterre nord", nord))
    brut += pb._field_bytes(1, zone_brute("allee-equerre", "Allée en équerre", equerre))
    brut += pb._field_bytes(2, zone_brute("bassin", "Bassin", bassin, parent="parterre-nord"))
    brut += pb._field_bytes(2, zone_brute("massif-buis", "Massif de buis", [(6, 3), (9, 3), (9, 5), (6, 5)], parent="parterre-sud"))
    brut += pb._field_bytes(3, passage_brut("passage-parterres", "parterre-sud", "parterre-nord", [(8, 7.5), (8, 12.5)]))
    brut += pb._field_bytes(3, passage_brut("passage-station", "parterre-sud", "", [(1, 1), (2.5, 2.5)], station=True))
    brut += pb._field_bytes(4, pb._field_f32(1, 1.0) + pb._field_f32(2, 1.0) + pb._field_f32(3, 0.0))  # station
    brut += pb._field_bytes(7, pb._field_f32(1, 48.8) + pb._field_f32(2, 2.1))  # origine du repère
    return brut + pb._field_bytes(11, b"\x08\x32")


def _encoder(champs: list[cartes.Champ]) -> bytes:
    """Inverse de `carte.champs_stricts` : mêmes champs, mêmes octets."""
    brut = b""
    for numero, type_, valeur in champs:
        brut += pb._encode_varint((numero << 3) | type_)
        if type_ == 0:
            brut += pb._encode_varint(valeur)
        elif type_ == 2:
            brut += pb._encode_varint(len(valeur)) + valeur
        else:
            brut += valeur.to_bytes(8 if type_ == 1 else 4, "little")
    return brut


def _avec(champs: list[cartes.Champ], numero: int, valeur: bytes) -> list[cartes.Champ]:
    return [(n, t, valeur if n == numero and t == 2 else v) for n, t, v in champs]


class RobotFictif:
    """Liaison de démonstration : chaque trame publiée est lue et suivie d'effet, en mémoire."""

    def __init__(self) -> None:
        self.connecte = True
        self.session: Any = None
        self.carte = jardin()
        self.est, self.nord, self.cap = 2.5, 2.5, 0.0  # mètres, mètres, radians
        self.hauteur_mm = 60
        self.statut = 1  # prêt, en attente d'ordre
        self.recues: list[bytes] = []

    def etat(self) -> dict[str, Any]:
        return {
            # Comme le vrai : statut vivant (f1) et tâche (f6), ici identiques — le robot fictif n'a pas de tâche.
            "robotState": self.statut, "workStatus": self.statut, "battery": 87, "poseEastM": self.est, "poseNorthM": self.nord,
            "poseThetaRad": self.cap, "rtkStatus": 3, "rtkSatellites": 24, "lteSignalQuality": 4,
            "wifiSignalQuality": 3, "rcCutHeight": self.hauteur_mm, "softwareVersion": "démonstration",
        }

    async def battre(self) -> None:
        """État publié périodiquement : sans cela l'affichage se griserait au bout de 30 s."""
        while True:
            await asyncio.sleep(PERIODE_ETAT_S)
            self.session.recevoir_etat(self.etat())

    async def publier(self, _sujet: str, charge: str, *, delai: float) -> None:
        octets = pb.unwrap_envelope(charge)
        self.recues.append(octets)
        champs = cartes.champs_stricts(octets)
        commande = next((v for n, t, v in champs if n == 5 and t == 0), None)
        pilotage = next((v for n, t, v in champs if n == 10 and t == 2), None)
        if pilotage is not None:
            self._piloter(cartes.champs_stricts(pilotage))
            return
        await asyncio.sleep(DELAI_REPONSE_S)
        if commande == 19:  # demande de carte
            self.session.recevoir_etat({"mapRaw": self.carte})
            return
        if commande == 9:
            self._retoucher(champs)
        elif commande == 50:
            self._regler_hauteur(champs)
        elif commande == 33:  # retour à la base : le robot fictif s'y retrouve aussitôt, en charge
            self.est, self.nord, self.cap, self.statut = 1.0, 1.0, 0.0, 5
        elif commande in (28, 32):
            self.statut = 1
        self.session.recevoir_etat(self.etat())

    def _piloter(self, consigne: list[cartes.Champ]) -> None:
        """Une consigne = un pas de 1/10 s ; la consigne nulle renvoie l'état (comme après un arrêt)."""
        lineaire = cartes._f32(next((v for n, t, v in consigne if n == 1 and t == 5), 0))
        angulaire = cartes._f32(next((v for n, t, v in consigne if n == 2 and t == 5), 0))
        if lineaire == 0 and angulaire == 0:
            self.session.recevoir_etat(self.etat())
            return
        pas = 1.0 / CADENCE_HZ
        self.cap = (self.cap + angulaire * pas + math.pi) % (2 * math.pi) - math.pi  # positif = gauche
        self.est += lineaire * math.cos(self.cap) * pas
        self.nord += lineaire * math.sin(self.cap) * pas

    def _retoucher(self, champs: list[cartes.Champ]) -> None:
        """Retouche de contour (userCtrl 9) : seul le polygone de la zone visée change."""
        try:
            recue = cartes.champs_stricts(next(v for n, t, v in champs if n == 12 and t == 2))
            infos = cartes.champs_stricts(cartes.champs_stricts(next(v for n, t, v in recue if n == 1))[0][2])
            identifiant = next(v for n, t, v in infos if n == 3 and t == 2)
            polygone = next(v for n, t, v in infos if n == 5 and t == 2)
        except (StopIteration, IndexError, cartes.CarteIllisible):
            return
        contenu = []
        for numero, type_, valeur in cartes.champs_stricts(self.carte):
            if numero == 1 and type_ == 2:
                zone = cartes.champs_stricts(valeur)
                anciennes = cartes.champs_stricts(zone[0][2])
                if next((v for n, t, v in anciennes if n == 3 and t == 2), None) == identifiant:
                    valeur = _encoder(_avec(zone, 1, _encoder(_avec(anciennes, 5, polygone))))
            contenu.append((numero, type_, valeur))
        self.carte = _encoder(contenu)

    def _regler_hauteur(self, champs: list[cartes.Champ]) -> None:
        try:
            reglage = cartes.champs_stricts(cartes.champs_stricts(next(v for n, t, v in champs if n == 12 and t == 2))[0][2])
            self.hauteur_mm = int(next(v for n, t, v in reglage if n == 1 and t == 0))
        except (StopIteration, IndexError, cartes.CarteIllisible):
            return
