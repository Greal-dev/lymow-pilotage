"""Carte du robot : décodage STRICT du contenu brut reçu, lecture seule.

Rôle : transformer les octets d'une réponse de carte en zones, zones interdites,
passages et station, EN GARDANT les octets bruts de chaque élément — ce sont eux
qui servent à la sauvegarde (SEC-09) et à la comparaison après écriture (SEC-11).
Dit aussi si la carte est complète : une carte partielle s'affiche, mais ne sert
jamais de base à une écriture. N'émet rien, n'écrit rien. Numéros de champs :
ceux du socle ha-lymow (PbMap). Le décodeur d'ici est plus strict que celui du
socle (qui tolère les charges tronquées) et ne sert qu'à la carte.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Any

from . import geometrie, protocole
from .geometrie import Point
from .session import EchecOrdre, Session

CHAMP_ZONES, CHAMP_INTERDITES, CHAMP_PASSAGES, CHAMP_STATION = 1, 2, 3, 4
CHAMP_INCOMPLETE, CHAMP_ORIGINE = 5, 7
# Ce qui définit la carte pour une écriture : deux lectures doivent y être identiques (SEC-09).
CHAMPS_SOCLE = (CHAMP_ZONES, CHAMP_INTERDITES, CHAMP_PASSAGES, CHAMP_STATION)
# Champs qui ne doivent pas bouger quand on retouche une zone (SEC-11). Les champs 6
# (boîte englobante de la carte) et 9 (zones modifiées) peuvent légitimement changer.
CHAMPS_STABLES = (CHAMP_INTERDITES, CHAMP_PASSAGES, CHAMP_STATION, 7, 8, 11, 12, 13)
Champ = tuple[int, int, Any]


class CarteIllisible(ValueError):
    """Les octets reçus ne sont pas une carte (réponse de trajet, charge tronquée…)."""


def _varint(octets: bytes, position: int) -> tuple[int, int]:
    valeur = 0
    for rang in range(10):
        if position >= len(octets):
            raise CarteIllisible("charge tronquée au milieu d'un nombre")
        octet = octets[position]
        position += 1
        valeur |= (octet & 0x7F) << (7 * rang)
        if not octet & 0x80:
            return valeur, position
    raise CarteIllisible("nombre trop long")


def champs_stricts(octets: bytes) -> list[Champ]:
    """Décodage protobuf sans tolérance : toute longueur annoncée doit être entièrement lue."""
    position, champs = 0, []
    tailles_fixes = {1: 8, 5: 4}
    while position < len(octets):
        etiquette, position = _varint(octets, position)
        numero, type_ = etiquette >> 3, etiquette & 7
        if numero == 0:
            raise CarteIllisible("numéro de champ nul")
        if type_ == 0:
            valeur, position = _varint(octets, position)
        elif type_ == 2:
            taille, position = _varint(octets, position)
            if position + taille > len(octets):
                raise CarteIllisible("tranche plus courte que la longueur annoncée")
            valeur, position = octets[position : position + taille], position + taille
        elif type_ in tailles_fixes:
            taille = tailles_fixes[type_]
            if position + taille > len(octets):
                raise CarteIllisible("valeur fixe tronquée")
            valeur, position = int.from_bytes(octets[position : position + taille], "little"), position + taille
        else:
            raise CarteIllisible(f"type de fil inconnu ({type_})")
        champs.append((numero, type_, valeur))
    return champs


def _premier(champs: list[Champ], numero: int, type_: int | None = None) -> Any:
    for n, t, valeur in champs:
        if n == numero and (type_ is None or t == type_):
            return valeur
    return None


def _f32(brut: int) -> float:
    return struct.unpack("<f", struct.pack("<I", brut & 0xFFFFFFFF))[0]


@dataclass(frozen=True)
class Zone:
    identifiant: str
    nom: str
    contour: list[Point]
    brut: bytes = field(repr=False)
    parent: str = ""  # zone interdite : zone de tonte de rattachement
    # Constat du 2026-10-12 : une zone interdite réelle porte de 5 à 12 identifiants (champ 4),
    # dont un à trois seulement sont des zones de la carte ; tous sont gardés ici.
    rattachements: tuple[str, ...] = ()

    @property
    def libelle(self) -> str:
        return self.nom or self.identifiant

    def hors_contour(self) -> list[Any]:
        """Tout ce que porte la zone sauf son polygone et ses propriétés dérivées (surface, bornes)."""
        champs = champs_stricts(self.brut)
        infos = _premier(champs, 1, 2)
        internes = [c for c in champs_stricts(infos) if c[0] != 5] if isinstance(infos, bytes) else []
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
    manques: tuple[str, ...] = ()  # pourquoi la carte est incomplète ; vide = complète

    @property
    def complete(self) -> bool:
        return not self.manques

    def zone(self, cle: str) -> Zone:
        """Par identifiant exact, sinon par nom (sans tenir compte de la casse) ; jamais d'à-peu-près."""
        trouvees = self.zone_exacte(cle)
        trouvees = [trouvees] if trouvees else [z for z in self.zones if z.nom and z.nom.casefold() == cle.casefold()]
        if len(trouvees) != 1:
            connues = ", ".join(f"{z.libelle} ({z.identifiant})" for z in self.zones) or "aucune"
            raise CarteIllisible(
                f"zone « {cle} » {'ambiguë' if trouvees else 'introuvable'} ; zones de la carte : {connues}"
            )
        return trouvees[0]

    def zone_exacte(self, identifiant: str) -> Zone | None:
        exactes = [z for z in self.zones if z.identifiant == identifiant]
        return exactes[0] if len(exactes) == 1 else None

    def stables(self) -> list[Champ]:
        return [c for c in champs_stricts(self.octets) if c[0] in CHAMPS_STABLES]

    def socle(self) -> list[Champ]:
        """Zones, zones interdites, passages et station, octet pour octet."""
        return [c for c in champs_stricts(self.octets) if c[0] in CHAMPS_SOCLE]

    def empreinte(self) -> str:
        condense = hashlib.sha256()
        for numero, _type, valeur in self.socle():
            condense.update(bytes([numero]) + (valeur if isinstance(valeur, bytes) else str(valeur).encode()))
        return condense.hexdigest()


def _texte(brut: Any) -> str:
    return brut.decode("utf-8", errors="replace") if isinstance(brut, bytes) else ""


def _points(brut: Any) -> list[Point]:
    if not isinstance(brut, bytes):
        return []
    points = []
    for numero, type_, valeur in champs_stricts(brut):
        if numero != 1 or type_ != 2:
            raise CarteIllisible("polygone mal formé")
        point = champs_stricts(valeur)
        x, y = _premier(point, 1, 5), _premier(point, 2, 5)
        if x is None or y is None:
            raise CarteIllisible("point sans x ou sans y")
        x, y = _f32(x), _f32(y)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise CarteIllisible("coordonnée non finie dans la carte")
        points.append((x, y))
    return points


def _zone(brut: bytes) -> Zone:
    champs = champs_stricts(brut)
    infos_brutes = _premier(champs, 1, 2)
    if not isinstance(infos_brutes, bytes):
        raise CarteIllisible("zone sans description : ce n'est pas une carte")
    infos = champs_stricts(infos_brutes)
    return Zone(
        _texte(_premier(infos, 3, 2)), _texte(_premier(infos, 2, 2)), _points(_premier(infos, 5, 2)), brut,
        rattachements=tuple(_texte(v) for n, t, v in champs if n == 4 and t == 2),
    )


def _passage(brut: bytes) -> Passage:
    champs = champs_stricts(brut)
    return Passage(
        _texte(_premier(champs, 1, 2)), _texte(_premier(champs, 2, 2)), _texte(_premier(champs, 3, 2)),
        bool(_premier(champs, 6, 0)), _points(_premier(champs, 5, 2)), brut,
    )


def _zone_de_rattachement(interdite: Zone, contours: dict[str, list[Point]]) -> str:
    """Parmi les identifiants du champ 4 qui désignent une zone de la carte, celle que la zone
    interdite touche ; à défaut la première. Constat du 2026-10-12 : la zone qui contient la
    zone interdite figure toujours dans la liste, mais ni seule ni forcément en tête."""
    candidates = [i for i in interdite.rattachements if i in contours]
    touchees = [i for i in candidates if geometrie.touche(contours[i], interdite.contour)]
    return (touchees or candidates or [""])[0]


def decoder(octets: bytes, recue: float = 0.0) -> Carte:
    """Contenu brut d'une carte (PbMap) → `Carte`. Lève `CarteIllisible` sur tout autre contenu."""
    try:
        champs = champs_stricts(octets)
        if any(numero in CHAMPS_SOCLE and type_ != 2 for numero, type_, _v in champs):
            raise CarteIllisible("structure inattendue : ce n'est pas une carte")
        zones = [_zone(v) for n, _t, v in champs if n == CHAMP_ZONES]
        contours = {zone.identifiant: zone.contour for zone in zones}
        interdites = [
            dataclasses.replace(z, parent=_zone_de_rattachement(z, contours))
            for z in (_zone(v) for n, _t, v in champs if n == CHAMP_INTERDITES)
        ]
        passages = [_passage(v) for n, _t, v in champs if n == CHAMP_PASSAGES]
        station = None
        brute = _premier(champs, CHAMP_STATION, 2)
        if isinstance(brute, bytes):
            s = champs_stricts(brute)
            x, y, cap = _premier(s, 1, 5), _premier(s, 2, 5), _premier(s, 3, 5)
            if x is not None and y is not None:
                station = (_f32(x), _f32(y), _f32(cap) if cap is not None else 0.0)
    except (IndexError, TypeError, KeyError, UnicodeError, struct.error, OverflowError) as erreur:
        raise CarteIllisible(f"carte illisible ({type(erreur).__name__})") from None
    if not zones:
        raise CarteIllisible("aucune zone de tonte : carte vide ou réponse d'une autre nature")
    # Les réponses partielles existent (zones sans passages ni origine) : on les nomme.
    manques = []
    if _premier(champs, CHAMP_INCOMPLETE):
        manques.append("le robot la signale incomplète")
    if station is None:
        manques.append("station de charge absente")
    if not isinstance(_premier(champs, CHAMP_ORIGINE, 2), bytes):
        manques.append("origine absente")
    return Carte(octets, zones, interdites, passages, station, recue, tuple(manques))


def origine(carte: Carte) -> tuple[float, float] | None:
    """Origine du repère (latitude, longitude) : champ 7 de la carte. Jamais affichée ni journalisée."""
    brut = _premier(champs_stricts(carte.octets), CHAMP_ORIGINE, 2)
    if not isinstance(brut, bytes):
        return None
    champs = champs_stricts(brut)
    lat, lon = _premier(champs, 1, 5), _premier(champs, 2, 5)
    return None if lat is None or lon is None else (_f32(lat), _f32(lon))


def _contour_json(points: list[Point]) -> list[list[float]]:
    return [[round(x, 4), round(y, 4)] for x, y in points]


def vers_json(carte: Carte) -> dict[str, Any]:
    """Forme lisible : affichage, sortie `--json`, et moitié JSON de la sauvegarde."""
    return {
        "complete": carte.complete,
        "manques": list(carte.manques),
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
    """Requêtes pures « carte », tranche après tranche, puis décodage strict de l'ensemble.

    Constat du 2026-10-12 : une réponse de 81 920 octets est une tranche coupée net ; la suite se
    demande à l'index suivant (hypothèse non vérifiée en réel). Rien n'est affaibli : l'assemblage
    passe par le même décodage strict, et une carte restée tronquée est refusée avec son motif,
    aussitôt — plus d'attente muette jusqu'au délai.
    """
    fin = session.horloge() + attente_s
    tranches: list[bytes] = []
    recu: tuple[bytes, float] = (b"", 0.0)
    for index in range(protocole.TRANCHES_CARTE_MAX):
        instant = session.horloge()
        trame = protocole.trame_demande_carte(session.cal, index)
        await session.publier(trame, 3.0)
        session.journaliser("requete-emise", sujet=session.sujet, trame=trame.description())
        while True:
            recu = session.tranche_carte or recu
            if recu[1] > instant:
                break
            if session.horloge() >= fin or not session.lien.connecte:
                if not tranches:
                    raise EchecOrdre("carte non reçue du robot dans le délai : réessayez")
                raise CarteIllisible(
                    f"carte reçue mais refusée : {len(tranches)} tranche(s) de {protocole.TAILLE_TRANCHE_CARTE} octets "
                    f"reçue(s), la suivante n'est pas arrivée dans le délai — carte incomplète"
                )
            await session.dormir(0.2)
        if tranches and recu[0] == tranches[0]:
            raise CarteIllisible(
                "carte reçue mais refusée : le robot renvoie la même première tranche de "
                f"{protocole.TAILLE_TRANCHE_CARTE} octets quel que soit l'index demandé — carte tronquée"
            )
        tranches.append(recu[0])
        if len(recu[0]) < protocole.TAILLE_TRANCHE_CARTE:
            break
    else:
        raise CarteIllisible(f"carte reçue mais refusée : plus de {protocole.TRANCHES_CARTE_MAX} tranches")
    octets = b"".join(tranches)
    try:
        carte = decoder(octets, recu[1])
    except CarteIllisible as erreur:
        raise CarteIllisible(
            f"carte reçue mais refusée : {erreur} ({len(octets)} octets en {len(tranches)} tranche(s))"
        ) from None
    session.carte_brute = (octets, recu[1])
    session.journaliser("carte-assemblee", octets=len(octets), tranches=len(tranches), complete=carte.complete)
    return carte
