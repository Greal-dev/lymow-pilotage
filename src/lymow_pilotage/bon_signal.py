"""Retour vers le bon signal : cible la plus proche, trajet proposé, découpage en pas.

Rôle : à partir du relevé local de précision et de la carte, proposer l'endroit de bon signal
le plus proche du robot, un trajet qui reste dans les zones de tonte et les passages (hors
zones interdites), et sa traduction en pas bornés (SEC-02). Logique pure : rien n'est émis ici.
Frontières : mode ASSISTÉ — le trajet se charge dans la séquence, que le propriétaire
prévisualise et émet lui-même. Limites dites à l'utilisateur : quand le signal est perdu la
position du robot est incertaine, donc le trajet indicatif ; les obstacles non cartographiés
sont ignorés ; jamais essayé en réel.
"""

from __future__ import annotations

import heapq
import math
import time
from datetime import datetime, timedelta
from typing import Any

from . import geometrie, securite
from .carte import Carte
from .geometrie import Point

MARGE_M = 0.5  # distance tenue aux contours des zones et aux zones interdites
LARGEUR_PASSAGE_M = 0.5  # de part et d'autre du tracé d'un passage
RAYON_GROUPE_M = 3.0
PAS_ECHANTILLON_M = 0.3
ESPACEMENT_NOEUDS_M = 1.5
NOEUDS_MAX = 80
RECHERCHE_MAX_S = 3.0
COULOIR_M = 25.0  # autour de la ligne droite : seuls ces contours servent au contournement
MENTION_INCERTAINE = "position incertaine : trajet indicatif, surveillez la caméra"


def points_fiables(points: list[dict[str, Any]], jours: int | None = None, maintenant: datetime | None = None) -> list[Point]:
    """Points du relevé à précision fiable ; `jours` écarte les relevés plus anciens."""
    limite = None if jours is None else (maintenant or datetime.now()) - timedelta(days=jours)
    gardes = []
    for point in points:
        if securite.qualite_position(float(point["precision_m"])) != securite.FIABLE:
            continue
        if limite is not None:
            try:
                if datetime.fromisoformat(str(point.get("horodatage"))) < limite:
                    continue
            except ValueError:
                continue  # sans date lisible, un point ne peut pas être dit récent
        gardes.append((float(point["est_m"]), float(point["nord_m"])))
    return gardes


def regrouper(points: list[Point], rayon: float = RAYON_GROUPE_M) -> list[tuple[Point, int]]:
    """Groupes de points voisins : (centre, nombre de points). Simple, sans interpolation."""
    groupes: list[list[Point]] = []
    for point in points:
        for groupe in groupes:
            cx = sum(p[0] for p in groupe) / len(groupe)
            cy = sum(p[1] for p in groupe) / len(groupe)
            if math.hypot(point[0] - cx, point[1] - cy) <= rayon:
                groupe.append(point)
                break
        else:
            groupes.append([point])
    return [((sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g)), len(g)) for g in groupes]


def _distance_au_contour(contour: list[Point], p: Point) -> float:
    return min(geometrie.distance_au_segment(p, a, b) for a, b in zip(contour, contour[1:] + contour[:1]))


def _distance_au_trace(trace: list[Point], p: Point) -> float:
    if len(trace) < 2:
        return math.hypot(p[0] - trace[0][0], p[1] - trace[0][1]) if trace else math.inf
    return min(geometrie.distance_au_segment(p, a, b) for a, b in zip(trace, trace[1:]))


class _Terrain:
    """Contours réduits et boîtes englobantes, calculés une fois par carte : un point ne s'essaie
    que contre les zones dont la boîte le contient (la carte réelle compte 55 zones)."""

    def __init__(self, carte: Carte) -> None:
        self.zones = [self._boite(geometrie.reduire(z.contour)[0], 0.0) for z in carte.zones]
        self.interdites = [self._boite(geometrie.reduire(z.contour)[0], MARGE_M + 1.0) for z in carte.interdites]
        self.passages = [self._boite(list(p.trace), LARGEUR_PASSAGE_M) for p in carte.passages if p.trace]

    @staticmethod
    def _boite(points: list[Point], marge: float) -> tuple[list[Point], float, float, float, float]:
        if not points:
            return points, math.inf, -math.inf, math.inf, -math.inf
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return points, min(xs) - marge, max(xs) + marge, min(ys) - marge, max(ys) + marge

    def libre(self, p: Point, marge: float) -> bool:
        x, y = p
        for contour, x0, x1, y0, y1 in self.interdites:
            if x0 <= x <= x1 and y0 <= y <= y1 and len(contour) >= 3:
                if geometrie.contient(contour, p) or _distance_au_contour(contour, p) < marge:
                    return False
        for contour, x0, x1, y0, y1 in self.zones:
            if x0 <= x <= x1 and y0 <= y <= y1 and len(contour) >= 3 and geometrie.contient(contour, p):
                if marge <= 0 or _distance_au_contour(contour, p) >= marge:
                    return True
        return any(
            x0 <= x <= x1 and y0 <= y <= y1 and _distance_au_trace(trace, p) <= LARGEUR_PASSAGE_M
            for trace, x0, x1, y0, y1 in self.passages
        )


_terrains: dict[bytes, _Terrain] = {}


def _terrain(carte: Carte) -> _Terrain:
    if carte.octets not in _terrains:
        if len(_terrains) >= 4:
            _terrains.clear()
        _terrains[carte.octets] = _Terrain(carte)
    return _terrains[carte.octets]


def libre(carte: Carte, p: Point, marge: float = 0.0) -> bool:
    """Dans une zone de tonte (à `marge` de son contour) ou sur un passage, et hors de toute zone interdite."""
    return _terrain(carte).libre(p, marge)


def cible_la_plus_proche(robot: Point, groupes: list[tuple[Point, int]], carte: Carte, marge: float = MARGE_M) -> dict[str, Any] | None:
    """Le groupe de bon signal le plus proche du robot qui soit atteignable en principe : dans une zone ou un passage."""
    admissibles = [(centre, n) for centre, n in groupes if libre(carte, centre, marge)]
    if not admissibles:
        return None
    centre, nombre = min(admissibles, key=lambda g: math.hypot(g[0][0] - robot[0], g[0][1] - robot[1]))
    return {
        "est_m": round(centre[0], 2), "nord_m": round(centre[1], 2), "points": nombre,
        "distance_m": round(math.hypot(centre[0] - robot[0], centre[1] - robot[1]), 1),
        "cap_deg": round(math.degrees(math.atan2(centre[1] - robot[1], centre[0] - robot[0])), 0),
    }


def segment_libre(carte: Carte, a: Point, b: Point, marge: float = 0.0) -> bool:
    terrain = _terrain(carte)
    longueur = math.hypot(b[0] - a[0], b[1] - a[1])
    pas = max(1, math.ceil(longueur / PAS_ECHANTILLON_M))
    return all(
        terrain.libre((a[0] + (b[0] - a[0]) * i / pas, a[1] + (b[1] - a[1]) * i / pas), marge) for i in range(pas + 1)
    )


def _noeuds(carte: Carte, depart: Point, cible: Point, marge: float) -> list[Point]:
    """Sommets utiles au contournement, proches de la ligne droite : d'abord le tour des zones
    interdites (ce qu'il faut contourner), puis les extrémités des passages (ce qui relie deux
    zones), puis les contours des zones (là où la ligne droite sort de la pelouse)."""
    def proche(p: Point) -> bool:
        return geometrie.distance_au_segment(p, depart, cible) <= COULOIR_M

    def ecart(p: Point) -> float:
        return geometrie.distance_au_segment(p, depart, cible)

    def tour(zones: list[Any], sens: float) -> list[Point]:
        sommets: list[Point] = []
        decalage = marge + 0.3
        for zone in zones:
            contour = geometrie.reduire(zone.contour)[0]
            if len(contour) < 3 or not any(proche(s) for s in contour[:: max(1, len(contour) // 8)]):
                continue
            orientation = 1.0 if geometrie.aire_signee(contour) > 0 else -1.0
            dernier: Point | None = None
            for i, sommet in enumerate(contour):
                if not proche(sommet) or (dernier and math.hypot(sommet[0] - dernier[0], sommet[1] - dernier[1]) < ESPACEMENT_NOEUDS_M):
                    continue
                nx, ny = geometrie._normale_exterieure(contour, i, orientation)
                sommets.append((sommet[0] + sens * decalage * nx, sommet[1] + sens * decalage * ny))
                dernier = sommet
        return sorted((p for p in sommets if libre(carte, p, 0.0)), key=ecart)

    autour_des_interdites = tour(carte.interdites, 1.0)[: NOEUDS_MAX // 2]
    passages = sorted(
        (p for passage in carte.passages if passage.trace for p in (passage.trace[0], passage.trace[-1]) if proche(p)), key=ecart
    )[: NOEUDS_MAX // 4]
    reste = NOEUDS_MAX - len(autour_des_interdites) - len(passages)
    return autour_des_interdites + passages + tour(carte.zones, -1.0)[:reste]


def trajet(carte: Carte, depart: Point, cible: Point, marge: float = MARGE_M) -> tuple[list[Point] | None, str | None]:
    """Trajet du départ à la cible dans zones + passages, hors zones interdites ; (rien, motif) s'il n'y en a pas."""
    if not libre(carte, depart, 0.0):
        return None, "le robot est hors de toute zone de tonte et de tout passage de la carte : seul le cap est proposé"
    if not libre(carte, cible, 0.0):
        return None, "la cible est hors des zones de tonte et des passages : seul le cap est proposé"
    if segment_libre(carte, depart, cible, 0.0):  # la ligne droite y tient : pas de détour
        return [depart, cible], None
    noeuds = [depart, cible] + _noeuds(carte, depart, cible, marge)
    visibles: dict[tuple[int, int], bool] = {}

    def visible(i: int, j: int) -> bool:
        cle = (min(i, j), max(i, j))
        if cle not in visibles:
            visibles[cle] = segment_libre(carte, noeuds[i], noeuds[j], 0.0)
        return visibles[cle]

    def cout(i: int, j: int) -> float:
        return math.hypot(noeuds[i][0] - noeuds[j][0], noeuds[i][1] - noeuds[j][1])

    distances = {0: 0.0}
    precedents: dict[int, int] = {}
    file = [(cout(0, 1), 0.0, 0)]
    vus: set[int] = set()
    echeance = time.monotonic() + RECHERCHE_MAX_S
    while file:
        if time.monotonic() > echeance:  # grande carte, détour compliqué : on le dit plutôt que de bloquer la page
            return None, "aucun trajet trouvé dans le temps imparti : seul le cap est proposé"
        _estime, parcouru, i = heapq.heappop(file)
        if i in vus:
            continue
        vus.add(i)
        if i == 1:
            chemin = [1]
            while chemin[-1] != 0:
                chemin.append(precedents[chemin[-1]])
            return [noeuds[k] for k in reversed(chemin)], None
        for j in range(len(noeuds)):
            if j in vus or j == i:
                continue
            nouveau = parcouru + cout(i, j)
            if time.monotonic() > echeance:
                return None, "aucun trajet trouvé dans le temps imparti : seul le cap est proposé"
            if nouveau < distances.get(j, math.inf) and visible(i, j):
                distances[j] = nouveau
                precedents[j] = i
                heapq.heappush(file, (nouveau + cout(j, 1), nouveau, j))
    return None, "aucun trajet trouvé dans les zones et les passages de la carte : seul le cap est proposé"


def decouper(
    points: list[Point], cap_deg: float | None, facteur_angle: float = 1.0, facteur_distance: float = 1.0
) -> list[dict[str, Any]] | None:
    """Trajet → pas compatibles avec SEC-02 : rotations bornées, avances de 2 m au plus. Rien si le cap est inconnu."""
    if cap_deg is None or len(points) < 2:
        return None
    angle_max = math.floor(securite.ANGLE_MAX_DEG * min(1.0, facteur_angle) + 1e-9)  # maximum demandable par ordre
    ordres: list[dict[str, Any]] = []
    cap = cap_deg
    for a, b in zip(points, points[1:]):
        distance = math.hypot(b[0] - a[0], b[1] - a[1])
        if distance < 0.05:
            continue
        vise = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        virage = (vise - cap + 180.0) % 360.0 - 180.0  # positif = à gauche
        if abs(virage) >= 1.0:
            morceaux = math.ceil(abs(virage) / angle_max)
            ordres.extend({"nom": "tourner", "valeur": round(virage / morceaux, 1), "vitesse": None, "duree": None} for _ in range(morceaux))
        cap = vise
        # Plus long pas demandable : le plafond de distance, et la durée maximale d'un ordre une fois le facteur
        # de distance appliqué (un facteur inférieur à 1 allonge la durée).
        plus_long = min(
            securite.DISTANCE_MAX_M,
            math.floor(securite.VITESSE_LINEAIRE_M_S * securite.DUREE_MAX_S * facteur_distance * 100 + 1e-9) / 100,
        )
        pas = math.ceil(distance / plus_long - 1e-9)
        ordres.extend({"nom": "avancer", "valeur": round(distance / pas, 2), "vitesse": None, "duree": None} for _ in range(pas))
    return ordres


def proposer(
    carte: Carte, releve: list[dict[str, Any]], robot: Point | None, cap_deg: float | None, precision_m: float | None,
    facteur_angle: float = 1.0, jours: int | None = None, facteur_distance: float = 1.0,
) -> dict[str, Any]:
    """Tout ce que la page affiche : cible, distance, cap, trajet, pas — ou pourquoi il n'y en a pas."""
    fiables = points_fiables(releve, jours)
    resultat: dict[str, Any] = {"points_fiables": len(fiables), "cible": None, "trajet": None, "ordres": None, "motif": None, "mention": None}
    if robot is None:
        return resultat | {"motif": "position du robot inconnue : aucune cible ne peut être proposée"}
    if securite.qualite_position(precision_m) == securite.FIABLE:
        return resultat | {"motif": "la position du robot est déjà fiable : rien à rejoindre"}
    if not fiables:
        return resultat | {"motif": "aucun point de bon signal dans le relevé : conduisez le robot vers un endroit dégagé"}
    cible = cible_la_plus_proche(robot, regrouper(fiables), carte)
    if cible is None:
        return resultat | {"motif": "aucun point de bon signal relevé à l'intérieur d'une zone ou d'un passage de la carte"}
    resultat["cible"] = cible
    if securite.qualite_position(precision_m) == securite.PERDUE:
        resultat["mention"] = MENTION_INCERTAINE
    chemin, motif = trajet(carte, robot, (cible["est_m"], cible["nord_m"]))
    if chemin is None:
        return resultat | {"motif": motif}
    resultat["trajet"] = [[round(x, 2), round(y, 2)] for x, y in chemin]
    resultat["ordres"] = decouper(chemin, cap_deg, facteur_angle, facteur_distance)
    if resultat["ordres"] is None:
        resultat["motif"] = "cap du robot inconnu : le trajet est tracé, mais ses pas ne peuvent pas être calculés"
    return resultat
