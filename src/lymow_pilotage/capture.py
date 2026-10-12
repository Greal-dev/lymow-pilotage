"""Capture de diagnostic : enregistre tels quels les messages reçus du robot.

Rôle : garder, pour une analyse hors ligne, chaque message reçu (sujet, horodatage,
charge brute en base64, taille) et un vidage structurel générique du protobuf
(numéro de champ, type de fil, longueur) qui n'interprète rien.
Frontières : ce module n'émet rien et ne décode ni état ni carte. Ses fichiers
contiennent l'origine GPS du jardin : ils restent hors du dépôt (`captures/`).
SEC-06 : une charge où figure une valeur d'accès (jeton, URL) n'est pas écrite.
"""

from __future__ import annotations

import base64
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Any

from . import journal
from .vendor.ha_lymow.protocol import unwrap_envelope

DOSSIER = "captures"
SECONDES_DEFAUT, SECONDES_MAX = 30, 120
# Garde-fou de disque : au-delà, les messages sont comptés mais plus écrits.
MESSAGES_MAX = 2000
TYPES = {0: "varint", 1: "fixe64", 2: "LEN", 5: "fixe32"}
PROFONDEUR_MAX = 8
# Au-delà, une série de champs identiques (points d'un contour) tient en une ligne.
SERIE_MAX = 16


@dataclass(frozen=True)
class Champ:
    numero: int
    type: int
    longueur: int
    valeur: int | None = None  # varint seulement
    contenu: bytes = b""  # LEN seulement
    annonce: int | None = None  # LEN tronqué : longueur annoncée, supérieure au reste


def _varint(octets: bytes, i: int) -> tuple[int, int]:
    valeur = 0
    for rang in range(10):
        if i >= len(octets):
            raise ValueError("varint coupé")
        octet = octets[i]
        i += 1
        valeur |= (octet & 0x7F) << (7 * rang)
        if not octet & 0x80:
            return valeur, i
    raise ValueError("varint trop long")


def decouper(octets: bytes) -> tuple[list[Champ], str | None]:
    """Champs d'un niveau, et le motif d'arrêt si la lecture ne va pas au bout."""
    champs: list[Champ] = []
    i = 0
    while i < len(octets):
        debut = i
        try:
            cle, i = _varint(octets, i)
            numero, type_fil = cle >> 3, cle & 7
            if numero == 0 or type_fil not in TYPES:
                raise ValueError(f"étiquette invalide (champ {numero}, type {type_fil})")
            if type_fil == 0:
                valeur, i = _varint(octets, i)
                champs.append(Champ(numero, 0, i - debut, valeur=valeur))
            elif type_fil == 2:
                annonce, i = _varint(octets, i)
                contenu = octets[i : i + annonce]
                coupe = len(contenu) < annonce
                champs.append(Champ(numero, 2, len(contenu), contenu=contenu, annonce=annonce if coupe else None))
                i += len(contenu)
            else:
                largeur = 8 if type_fil == 1 else 4
                if i + largeur > len(octets):
                    raise ValueError(f"{TYPES[type_fil]} coupé")
                champs.append(Champ(numero, type_fil, largeur))
                i += largeur
        except ValueError as erreur:
            return champs, f"octet {debut} sur {len(octets)} : {erreur}"
    return champs, None


def _est_message(octets: bytes) -> bool:
    champs, arret = decouper(octets)
    return bool(champs) and arret is None and all(champ.annonce is None for champ in champs)


def _ligne(champ: Champ) -> str:
    texte = f"{champ.numero} {TYPES[champ.type]}"
    if champ.type == 0:
        return f"{texte} = {champ.valeur}"
    if champ.annonce is not None:
        return f"{texte} {champ.longueur} TRONQUÉ : {champ.annonce} octets annoncés, {champ.longueur} présents"
    return f"{texte} {champ.longueur}" if champ.type == 2 else texte


def vider(octets: bytes, profondeur: int = 0) -> list[str]:
    """Structure seule : numéro de champ, type de fil, longueur. Aucun nom, aucun sens prêté."""
    marge = "  " * profondeur
    champs, arret = decouper(octets)
    lignes: list[str] = []
    for (numero, type_fil), groupe in groupby(champs, key=lambda champ: (champ.numero, champ.type)):
        serie = list(groupe)
        if len(serie) > SERIE_MAX:
            longueurs = [champ.longueur for champ in serie]
            lignes.append(
                f"{marge}{numero} {TYPES[type_fil]} ×{len(serie)} · longueurs {min(longueurs)} à {max(longueurs)}"
                f" · total {sum(longueurs)} (seul le premier est détaillé)"
            )
            serie = serie[:1]
        for champ in serie:
            lignes.append(marge + _ligne(champ))
            descendre = champ.annonce is not None or _est_message(champ.contenu)
            if champ.type == 2 and descendre and profondeur < PROFONDEUR_MAX:
                lignes.extend(vider(champ.contenu, profondeur + 1))
    if arret:
        lignes.append(f"{marge}!! lecture arrêtée, {arret}")
    return lignes


class Enregistreur:
    """Reçoit chaque message brut de la liaison et l'écrit aussitôt, sans rien décoder d'utile."""

    def __init__(self, dossier: Path, horloge: Callable[[], float] = time.monotonic) -> None:
        dossier.mkdir(parents=True, exist_ok=False)
        self.dossier = dossier
        self.horloge = horloge
        self._debut = horloge()
        self.messages = 0
        self.octets = 0
        self.ecartes = 0
        self.non_ecrits = 0
        self.erreurs = 0
        self.par_sujet: Counter[str] = Counter()
        self.premier_niveau: Counter[int] = Counter()
        self.tailles: list[int] = []
        self.tronques = 0

    def recevoir(self, sujet: str, charge: bytes | str, retenu: bool = False) -> None:
        brut = charge.encode() if isinstance(charge, str) else bytes(charge)
        self.messages += 1
        self.octets += len(brut)
        self.par_sujet[sujet.rsplit("/", 1)[-1]] += 1
        self.tailles.append(len(brut))
        if self.messages > MESSAGES_MAX:
            self.non_ecrits += 1
            return
        fiche: dict[str, Any] = {
            "numero": self.messages,
            "sujet": sujet,
            "horodatage": datetime.now().isoformat(timespec="milliseconds"),
            "t_s": round(self.horloge() - self._debut, 3),
            "retenu": retenu,
            "taille": len(brut),
        }
        try:
            protobuf = unwrap_envelope(brut)
        except Exception:  # noqa: BLE001 — charge non fiable : tout ce qui n'est pas une enveloppe protobuf
            protobuf = None
        # SEC-06 : ni jeton ni URL sur disque, même venus du robot ; la charge est alors écartée.
        if journal.contient_acces(brut) or (protobuf is not None and journal.contient_acces(protobuf)):
            self.ecartes += 1
            fiche["ecartee"] = "valeur d'accès possible (jeton ou URL) : charge non enregistrée"
            protobuf = None
        else:
            fiche["charge_b64"] = base64.b64encode(brut).decode()
        nom = f"{self.messages:04d}"
        resume = dict(fiche)
        resume.pop("charge_b64", None)
        try:
            if protobuf is not None:
                champs, arret = decouper(protobuf)
                tronque = arret is not None or any(champ.annonce is not None for champ in champs)
                self.tronques += tronque
                self.premier_niveau.update(champ.numero for champ in champs)
                resume |= {
                    "protobuf_octets": len(protobuf),
                    "champs": [[champ.numero, TYPES[champ.type], champ.longueur] for champ in champs[:64]],
                    "lecture_complete": not tronque,
                }
                entete = f"message {nom} · {sujet} · {len(protobuf)} octets de protobuf"
                self._ecrire(f"{nom}.vidage.txt", "\n".join([entete, *vider(protobuf)]) + "\n")
            self._ecrire(f"{nom}.json", json.dumps(fiche, ensure_ascii=False) + "\n")
            with (self.dossier / "index.jsonl").open("a", encoding="utf-8", newline="\n") as index:
                index.write(json.dumps(resume, ensure_ascii=False) + "\n")
        except OSError:
            self.erreurs += 1

    def _ecrire(self, nom: str, texte: str) -> None:
        (self.dossier / nom).write_text(texte, encoding="utf-8", newline="\n")

    def resume(self) -> dict[str, Any]:
        bilan: dict[str, Any] = {
            "dossier": str(self.dossier),
            "messages": self.messages,
            "octets": self.octets,
            "par_sujet": dict(self.par_sujet),
            "tailles": self.tailles[:50],
            "champs_de_premier_niveau": {str(numero): n for numero, n in sorted(self.premier_niveau.items())},
            "protobuf_incomplets": self.tronques,
        }
        for cle, valeur in (("charges_ecartees", self.ecartes), ("non_ecrits", self.non_ecrits), ("erreurs_d_ecriture", self.erreurs)):
            if valeur:
                bilan[cle] = valeur
        return bilan
