"""Géométrie plane en Python pur pour la retouche de contour (repère local, mètres).

Rôle : polygones simples, appartenance, portion de contour, déplacement de
sommets le long de la normale extérieure. Aucune dépendance, aucune entrée-sortie,
aucune règle de sécurité (elles vivent dans `retouche`).
"""

from __future__ import annotations

import math
import struct

Point = tuple[float, float]
EPSILON = 1e-9


def f32(valeur: float) -> float:
    """Valeur telle qu'elle voyage sur le fil (flottant simple précision)."""
    return struct.unpack("<f", struct.pack("<f", valeur))[0]


def en_f32(contour: list[Point]) -> list[Point]:
    return [(f32(x), f32(y)) for x, y in contour]


def aire_signee(contour: list[Point]) -> float:
    """Positive si le contour tourne dans le sens trigonométrique."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(contour, contour[1:] + contour[:1]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def longueur(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _croix(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _dans_la_boite(a: Point, b: Point, p: Point) -> bool:
    return (
        min(a[0], b[0]) - EPSILON <= p[0] <= max(a[0], b[0]) + EPSILON
        and min(a[1], b[1]) - EPSILON <= p[1] <= max(a[1], b[1]) + EPSILON
    )


def segments_se_touchent(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Vrai si [ab] et [cd] se coupent ou se touchent, alignements compris."""
    d1, d2, d3, d4 = _croix(c, d, a), _croix(c, d, b), _croix(a, b, c), _croix(a, b, d)
    if ((d1 > EPSILON and d2 < -EPSILON) or (d1 < -EPSILON and d2 > EPSILON)) and (
        (d3 > EPSILON and d4 < -EPSILON) or (d3 < -EPSILON and d4 > EPSILON)
    ):
        return True
    return (
        (abs(d1) <= EPSILON and _dans_la_boite(c, d, a))
        or (abs(d2) <= EPSILON and _dans_la_boite(c, d, b))
        or (abs(d3) <= EPSILON and _dans_la_boite(a, b, c))
        or (abs(d4) <= EPSILON and _dans_la_boite(a, b, d))
    )


def distance_au_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    carre = dx * dx + dy * dy
    if carre <= EPSILON:
        return longueur(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / carre))
    return longueur(p, (a[0] + t * dx, a[1] + t * dy))


def _aretes(contour: list[Point], ferme: bool = True) -> list[tuple[Point, Point]]:
    suivants = contour[1:] + contour[:1] if ferme else contour[1:]
    return list(zip(contour, suivants))


def reduire(contour: list[Point]) -> tuple[list[Point], list[int]]:
    """Contour sans ses arêtes nulles, et pour chaque sommet d'origine le rang de son représentant.

    Seuls les sommets EXACTEMENT confondus et consécutifs (fermeture comprise) sont réunis : ce
    sont les deux seules répétitions vues sur la carte réelle (2026-10-12). Un point repris plus
    loin dans le contour reste distinct, et le contour réduit n'est alors pas simple."""
    reduit: list[Point] = []
    rangs: list[int] = []
    for i, point in enumerate(contour):
        if not (i and point == contour[i - 1]):
            reduit.append(point)
        rangs.append(len(reduit) - 1)
    if len(reduit) > 1 and reduit[-1] == reduit[0]:
        dernier = len(reduit) - 1
        reduit.pop()
        rangs = [0 if rang == dernier else rang for rang in rangs]
    return reduit, rangs


def est_simple(contour: list[Point]) -> bool:
    """Ni auto-intersection, ni arête repliée sur la précédente, ni arête nulle."""
    n = len(contour)
    if n < 3:
        return False
    aretes = _aretes(contour)
    for i, (a, b) in enumerate(aretes):
        if longueur(a, b) <= EPSILON:
            return False
        c = aretes[(i + 1) % n][1]
        # Arête suivante repliée sur celle-ci : alignée et de sens opposé.
        if abs(_croix(a, b, c)) <= EPSILON and (a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1]) > 0:
            return False
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # arêtes voisines par la fermeture du contour
            if segments_se_touchent(a, b, *aretes[j]):
                return False
    return True


def sur_le_bord(contour: list[Point], p: Point, tolerance: float = 1e-7) -> bool:
    return any(distance_au_segment(p, a, b) <= tolerance for a, b in _aretes(contour))


def contient(contour: list[Point], p: Point) -> bool:
    """Strictement à l'intérieur (un point du bord n'est pas dedans)."""
    if sur_le_bord(contour, p):
        return False
    dedans = False
    for (x1, y1), (x2, y2) in _aretes(contour):
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            dedans = not dedans
    return dedans


def strictement_dans(interieur: list[Point], exterieur: list[Point]) -> bool:
    """Tous les sommets dedans et aucune arête ne touche le contour extérieur."""
    if not interieur or not all(contient(exterieur, p) for p in interieur):
        return False
    return not any(
        segments_se_touchent(a, b, c, d) for a, b in _aretes(interieur) for c, d in _aretes(exterieur)
    )


def touche(contour: list[Point], trace: list[Point]) -> bool:
    """Le tracé (ligne ouverte, ou point seul) coupe, touche ou pénètre le contour."""
    if not trace:
        return False
    if any(contient(contour, p) or sur_le_bord(contour, p) for p in trace):
        return True
    return any(
        segments_se_touchent(a, b, c, d) for a, b in _aretes(trace, ferme=False) for c, d in _aretes(contour)
    )


def distance_contours(a: list[Point], b: list[Point]) -> float:
    """Plus petite distance entre deux contours fermés qui ne se coupent pas."""
    return min(
        [distance_au_segment(p, c, d) for p in a for c, d in _aretes(b)]
        + [distance_au_segment(p, c, d) for p in b for c, d in _aretes(a)]
    )


def portion(contour: list[Point], de: int, a: int, cote: str) -> list[int]:
    """Indices des sommets de `de` à `a`, par le côté court ou long de la boucle, ou tous."""
    n = len(contour)
    if not (0 <= de < n and 0 <= a < n):
        raise ValueError(f"indices de sommet attendus entre 0 et {n - 1}")
    if cote == "tout":
        return list(range(n))
    avant = [(de + k) % n for k in range((a - de) % n + 1)]
    arriere = [(de - k) % n for k in range((de - a) % n + 1)]

    def parcours(indices: list[int]) -> float:
        return sum(longueur(contour[i], contour[j]) for i, j in zip(indices, indices[1:]))

    court, long_ = sorted((avant, arriere), key=parcours)
    if cote == "court":
        return court
    if cote == "long":
        return long_
    raise ValueError("côté attendu : court, long ou tout")


def _normale_exterieure(contour: list[Point], i: int, sens: float) -> Point:
    """Moyenne normalisée des normales extérieures des deux arêtes du sommet `i`."""
    n = len(contour)
    somme = [0.0, 0.0]
    for a, b in ((contour[i - 1], contour[i]), (contour[i], contour[(i + 1) % n])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        norme = math.hypot(dx, dy) or 1.0
        somme[0] += sens * dy / norme
        somme[1] += -sens * dx / norme
    norme = math.hypot(*somme)
    if norme <= 1e-6:  # pointe à 180° : la normale de l'arête entrante fait foi
        a, b = contour[i - 1], contour[i]
        dx, dy = b[0] - a[0], b[1] - a[1]
        norme_arete = math.hypot(dx, dy) or 1.0
        return (sens * dy / norme_arete, -sens * dx / norme_arete)
    return (somme[0] / norme, somme[1] / norme)


def ajuster(contour: list[Point], indices: list[int], ecart_m: float, raccord_m: float) -> list[Point]:
    """Déplace les sommets `indices` de `ecart_m` le long de la normale extérieure.

    Positif = vers l'extérieur. Sur une portion ouverte, l'écart croît de 0 à sa
    pleine valeur sur `raccord_m` à chaque extrémité (pas de marche brutale) ; un
    raccord nul déplace toute la portion d'un bloc. Aucun sommet n'est ajouté ni
    retiré, et aucun ne bouge de plus de |ecart_m|.
    """
    sens = 1.0 if aire_signee(contour) > 0 else -1.0
    tout = len(indices) == len(contour) and len(set(indices)) == len(contour)
    abscisses = [0.0]
    for i, j in zip(indices, indices[1:]):
        abscisses.append(abscisses[-1] + longueur(contour[i], contour[j]))
    total = abscisses[-1]
    resultat = list(contour)
    for rang, i in enumerate(indices):
        poids = 1.0
        if not tout and raccord_m > 0:
            poids = min(1.0, min(abscisses[rang], total - abscisses[rang]) / raccord_m)
        nx, ny = _normale_exterieure(contour, i, sens)
        resultat[i] = (contour[i][0] + ecart_m * poids * nx, contour[i][1] + ecart_m * poids * ny)
    return resultat
