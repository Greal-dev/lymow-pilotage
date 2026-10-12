"""Carte du robot : décodage du contenu brut reçu, lecture seule.

Rôle : transformer les octets d'une réponse de carte en zones, zones interdites,
passages et station, EN GARDANT les octets bruts de chaque élément — ce sont eux
qui servent à la sauvegarde (SEC-09) et à la comparaison après écriture (SEC-11).
N'émet rien, n'écrit rien. Numéros de champs : ceux du socle ha-lymow (PbMap).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import geometrie, protocole
from .geometrie import Point
from .session import EchecOrdre, Session
from .vendor.ha_lymow import protocol as pb

CHAMP_ZONES, CHAMP_INTERDITES, CHAMP_PASSAGES, CHAMP_STATION = 1, 2, 3, 4
# Champs qui ne doivent pas bouger quand on retouche une zone (SEC-11). Les champs 6
# (boîte englobante de la carte) et 9 (zones modifiées) peuvent légitimement changer.
CHAMPS_STABLES = (CHAMP_INTERDITES, CHAMP_PASSAGES, CHAMP_STATION, 7, 8, 11, 12, 13)


class CarteIllisible(ValueError):
    """Les octets reçus ne sont pas une carte (réponse de trajet, charge tronquée…)."""


@dataclass(frozen=True)
class Zone:
    identifiant: str
    nom: str
    contour: list[Point]
    brut: bytes = field(repr=False)
    parent: str = ""  # zone interdite : zone de tonte de rattachement

    @property
    def libelle(self) -> str:
        return self.nom or self.identifiant

    def hors_contour(self) -> list[Any]:
        """Tout ce que porte la zone sauf son polygone et ses propriétés dérivées (surface, bornes)."""
        champs = pb._decode_fields(self.brut)
        infos = pb._first(champs, 1)
        internes = [c for c in pb._decode_fields(infos) if c[0] != 5] if isinstance(infos, bytes) else []
        return [internes] + [c for c in champs if c[0] not in (1, 3)]


@dataclass(frozen=True)
class Passage:
    identifiant: str
    zone1: str
    zone2: str
    station: bool
    trace: list[Point]
    brut: bytes = field(repr=False)


@dataclass(frozen=True)
class Carte:
    octets: bytes = field(repr=False)
    zones: list[Zone]
    interdites: list[Zone]
    passages: list[Passage]
    station: tuple[float, float, float] | None
    recue: float = 0.0  # horloge monotone à la réception

    def zone(self, cle: str) -> Zone:
        """Par identifiant exact, sinon par nom (sans tenir compte de la casse) ; jamais d'à-peu-près."""
        exactes = [z for z in self.zones if z.identifiant == cle]
        trouvees = exactes or [z for z in self.zones if z.nom and z.nom.casefold() == cle.casefold()]
        if len(trouvees) != 1:
            connues = ", ".join(f"{z.libelle} ({z.identifiant})" for z in self.zones) or "aucune"
            raise CarteIllisible(
                f"zone « {cle} » {'ambiguë' if trouvees else 'introuvable'} ; zones de la carte : {connues}"
            )
        return trouvees[0]

    def stables(self) -> list[tuple[int, int, Any]]:
        return [c for c in pb._decode_fields(self.octets) if c[0] in CHAMPS_STABLES]


def _texte(brut: Any) -> str:
    return brut.decode("utf-8", errors="replace") if isinstance(brut, bytes) else ""


def _points(brut: Any) -> list[Point]:
    if not isinstance(brut, bytes):
        return []
    points = [(p["x"], p["y"]) for p in pb._decode_map_polygon(brut)]
    if not all(math.isfinite(x) and math.isfinite(y) for x, y in points):
        raise CarteIllisible("coordonnée non finie dans la carte")
    return points


def _zone(brut: bytes) -> Zone:
    champs = pb._decode_fields(brut)
    infos_brutes = pb._first(champs, 1)
    if not isinstance(infos_brutes, bytes):
        raise CarteIllisible("zone sans description : ce n'est pas une carte")
    infos = pb._decode_fields(infos_brutes)
    return Zone(
        _texte(pb._first(infos, 3)), _texte(pb._first(infos, 2)), _points(pb._first(infos, 5)), brut,
        _texte(pb._first(champs, 4)),
    )


def _passage(brut: bytes) -> Passage:
    champs = pb._decode_fields(brut)
    return Passage(
        _texte(pb._first(champs, 1)), _texte(pb._first(champs, 2)), _texte(pb._first(champs, 3)),
        bool(pb._first(champs, 6, 0)), _points(pb._first(champs, 5)), brut,
    )


def decoder(octets: bytes, recue: float = 0.0) -> Carte:
    """Contenu brut d'une carte (PbMap) → `Carte`. Lève `CarteIllisible` sur tout autre contenu."""
    try:
        champs = pb._decode_fields(octets)
        if any(numero in (CHAMP_ZONES, CHAMP_INTERDITES, CHAMP_PASSAGES) and type_ != 2 for numero, type_, _v in champs):
            raise CarteIllisible("structure inattendue : ce n'est pas une carte")
        zones = [_zone(z) for z in pb._all(champs, CHAMP_ZONES)]
        interdites = [_zone(z) for z in pb._all(champs, CHAMP_INTERDITES)]
        passages = [_passage(p) for p in pb._all(champs, CHAMP_PASSAGES)]
        station = None
        brute = pb._first(champs, CHAMP_STATION)
        if isinstance(brute, bytes):
            s = pb._decode_fields(brute)
            x, y, cap = (pb._first(s, n) for n in (1, 2, 3))
            if isinstance(x, int) and isinstance(y, int):
                station = (pb._decode_f32(x), pb._decode_f32(y), pb._decode_f32(cap) if isinstance(cap, int) else 0.0)
    except (IndexError, TypeError, KeyError, UnicodeError) as erreur:
        raise CarteIllisible(f"carte illisible ({type(erreur).__name__})") from None
    if not zones:
        raise CarteIllisible("aucune zone de tonte : carte vide ou réponse d'une autre nature")
    return Carte(octets, zones, interdites, passages, station, recue)


def _contour_json(points: list[Point]) -> list[list[float]]:
    return [[round(x, 4), round(y, 4)] for x, y in points]


def vers_json(carte: Carte) -> dict[str, Any]:
    """Forme lisible : affichage, sortie `--json`, et moitié JSON de la sauvegarde."""
    return {
        "zones": [
            {
                "identifiant": z.identifiant, "nom": z.nom, "sommets": len(z.contour),
                "surface_m2": round(abs(geometrie.aire_signee(z.contour)), 2) if len(z.contour) >= 3 else 0.0,
                "contour": _contour_json(z.contour),
            }
            for z in carte.zones
        ],
        "zones_interdites": [
            {"identifiant": z.identifiant, "nom": z.nom, "zone": z.parent, "contour": _contour_json(z.contour)}
            for z in carte.interdites
        ],
        "passages": [
            {
                "identifiant": p.identifiant, "zone1": p.zone1, "zone2": p.zone2,
                "passage_de_station": p.station, "trace": _contour_json(p.trace),
            }
            for p in carte.passages
        ],
        "station": None if carte.station is None else {
            "est_m": round(carte.station[0], 4), "nord_m": round(carte.station[1], 4),
            "cap_deg": round(math.degrees(carte.station[2]), 1),
        },
    }


def en_memoire(session: Session) -> Carte | None:
    """Dernière carte reçue par la session, décodée ; rien si aucune ou si illisible."""
    recu = session.carte_brute
    if recu is None:
        return None
    try:
        return decoder(recu[0], recu[1])
    except CarteIllisible:
        return None


async def demander(session: Session, attente_s: float) -> Carte:
    """Requête pure « carte entière », puis attente d'une carte reçue APRÈS la demande."""
    instant = session.horloge()
    await session.publier(protocole.trame_demande_carte(session.cal), 3.0)
    fin = instant + attente_s
    while True:
        carte = en_memoire(session)
        if carte is not None and carte.recue > instant:
            return carte
        if session.horloge() >= fin or not session.lien.connecte:
            raise EchecOrdre("carte non reçue du robot dans le délai : réessayez")
        await session.dormir(0.2)
