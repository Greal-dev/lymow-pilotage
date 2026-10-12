"""Grand jardin entièrement FICTIF, de la FORME de la carte réelle lue le 2026-10-12.

Aucune coordonnée, aucun nom, aucun identifiant réel : tout est fabriqué ici, sur une
grille. Seule la structure observée est reprise (compteurs et ordres de grandeur) :
- 55 zones de tonte, de 22 à 599 sommets, 5 seulement nommées ; 25 contours répètent leur
  premier point à la fin, 3 doublent un sommet ; description {1, [2], 3, 4, 5, [8], 9},
  puis champ 3 opaque, et parfois un champ 2 vide ;
- 19 zones interdites, de 15 à 45 sommets, 14 fermées sur leur premier point ; chacune porte
  de 5 à 12 identifiants (champ 4), dont un à trois seulement sont des zones de la carte, pas
  toujours en tête ; quatre ne sont pas strictement à l'intérieur d'une zone ;
- des passages {1, 2, 3, 5} de 5 à 9 points, qui touchent leurs deux zones ;
- un contenu de plus de 81 920 octets : le robot le livre coupé à cette taille.
La station, l'origine (valeur fictive) et les réglages finaux n'ont PAS été vus en réel
(ils se trouveraient au-delà de la coupure) : leur présence ici est une hypothèse.
"""

from __future__ import annotations

import math
import random

from lymow_pilotage.vendor.ha_lymow import protocol as pb

COLONNES, LIGNES = 11, 5
LARGEUR, HAUTEUR = 50.0, 25.0
NOMMEES = {3: "Zone fictive A", 17: "Zone fictive B", 28: "Zone fictive C", 41: "Zone fictive D", 50: "Zone E"}
DOUBLEES = (7, 19, 40)
NOMBRE_INTERDITES = 19
RATTACHEMENTS = [6] * 6 + [12] * 5 + [11] * 4 + [10] * 2 + [9, 5]


def _point(x: float, y: float) -> bytes:
    return pb._field_f32(1, x) + pb._field_f32(2, y)


def _polygone(points: list[tuple[float, float]]) -> bytes:
    return b"".join(pb._field_bytes(1, _point(x, y)) for x, y in points)


def centre(k: int) -> tuple[float, float]:
    return (LARGEUR * (k % COLONNES + 0.5), HAUTEUR * (k // COLONNES + 0.5))


def _ovale(milieu: tuple[float, float], rx: float, ry: float, n: int, phase: float) -> list[tuple[float, float]]:
    """Contour étoilé autour de son centre, sens trigonométrique, sans recoupement."""
    points = []
    for i in range(n):
        angle = 2 * math.pi * i / n
        relief = 1 + 0.12 * math.sin(3 * angle + phase)
        points.append((milieu[0] + rx * relief * math.cos(angle), milieu[1] + ry * relief * math.sin(angle)))
    return points


def identifiant_zone(k: int) -> str:
    return f"zn{k:02d}fict"


def est_fermee(k: int) -> bool:
    return k % COLONNES < 5


def contour_zone(k: int, alea: random.Random) -> list[tuple[float, float]]:
    sommets = {0: 22, 1: 599, 12: 250, 23: 260, 34: 210, 45: 190}.get(k) or alea.randint(40, 140)
    points = _ovale(centre(k), 22.0, 10.0, sommets, k * 0.7)
    if k in DOUBLEES:
        points.insert(5, points[5])
    return points + points[:1] if est_fermee(k) else points


def _zone(k: int, alea: random.Random) -> bytes:
    infos = pb._field_i32(1, 1)
    if k in NOMMEES:
        infos += pb._field_str(2, NOMMEES[k])
    infos += pb._field_str(3, identifiant_zone(k)) + pb._field_i32(4, 1) + pb._field_bytes(5, _polygone(contour_zone(k, alea)))
    if k == 9:
        infos += pb._field_i32(8, 1)
    infos += pb._field_bytes(9, b"")
    brut = pb._field_bytes(1, infos)
    if k >= 51:
        brut += pb._field_bytes(2, b"")
    milieu = centre(k)
    opaque = pb._field_bytes(1, _point(*milieu)) + pb._field_bytes(2, _point(*milieu)) + pb._field_i32(3, 132)
    return brut + pb._field_bytes(3, opaque + pb._field_bytes(5, _point(*milieu)))


def zone_d_une_interdite(j: int) -> int:
    return (3 * j) % (COLONNES * LIGNES)


def _interdite(j: int) -> bytes:
    k = zone_d_une_interdite(j)
    milieu = centre(k)
    if j >= 15:  # à cheval sur le bord de sa zone, comme quatre zones interdites réelles
        milieu = (milieu[0] + 22.0, milieu[1])
    points = _ovale(milieu, 3.0, 2.0, 15 + (j * 7) % 31, j * 0.3)
    if j < 14:
        points = points + points[:1]
    infos = pb._field_i32(1, 2) + pb._field_str(3, f"ng{j:02d}fict") + pb._field_i32(4, 1)
    infos += pb._field_bytes(5, _polygone(points))
    opaque = pb._field_bytes(1, _point(*milieu)) + pb._field_bytes(2, _point(*milieu)) + pb._field_i32(3, 1)
    brut = pb._field_bytes(1, infos) + pb._field_bytes(3, opaque + pb._field_bytes(5, _point(*milieu)))
    connus = [identifiant_zone(k)] + [identifiant_zone((k + d) % 55) for d in range(1, 1 + j % 3)]
    inconnus = [f"xx{j:02d}{rang:02d}zz" for rang in range(RATTACHEMENTS[j] - len(connus))]
    if j % 4 == 0:
        connus.reverse()  # la zone qui contient la zone interdite n'est pas la première connue
    liste = inconnus[:1] + connus + inconnus[1:] if j % 2 else connus + inconnus
    return brut + b"".join(pb._field_str(4, identifiant) for identifiant in liste)


def _passage(numero: int, a: int, b: int) -> bytes:
    (xa, ya), (xb, yb) = centre(a), centre(b)
    n = 5 + numero % 5
    trace = [(xa + (xb - xa) * i / (n - 1), ya + (yb - ya) * i / (n - 1)) for i in range(n)]
    brut = pb._field_str(1, f"ps{numero:02d}fict") + pb._field_str(2, identifiant_zone(a)) + pb._field_str(3, identifiant_zone(b))
    return brut + pb._field_bytes(5, _polygone(trace))


def voisins() -> list[tuple[int, int]]:
    horizontaux = [(k, k + 1) for k in range(COLONNES * LIGNES) if k % COLONNES < COLONNES - 1]
    return horizontaux + [(k, k + COLONNES) for k in range(0, COLONNES * (LIGNES - 1), COLONNES)]


def contenu() -> bytes:
    """Contenu brut de la carte (PbMap), dans l'ordre observé : zones, zones interdites, passages…"""
    alea = random.Random(20261012)
    brut = b"".join(pb._field_bytes(1, _zone(k, alea)) for k in range(COLONNES * LIGNES))
    brut += b"".join(pb._field_bytes(2, _interdite(j)) for j in range(NOMBRE_INTERDITES))
    brut += b"".join(pb._field_bytes(3, _passage(numero, a, b)) for numero, (a, b) in enumerate(voisins()))
    station = pb._field_f32(1, 2.0) + pb._field_f32(2, 12.5) + pb._field_f32(3, 0.0)
    brut += pb._field_bytes(4, station)
    brut += pb._field_bytes(7, pb._field_f32(1, 10.0) + pb._field_f32(2, 20.0))  # origine fictive
    return brut + pb._field_bytes(8, pb._field_i32(2, 1)) + pb._field_bytes(13, pb._field_i32(1, 60))
