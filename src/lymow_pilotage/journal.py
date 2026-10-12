"""Journal horodaté et filtre anti-secret (SEC-06).

Rôle : point de passage unique de tout ce qui est écrit sur disque ou affiché
en cas d'erreur. Un fichier JSON Lines par jour dans `journaux/`.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

MASQUE = "[SECRET]"

# SEC-06 : valeurs secrètes connues du processus, masquées où qu'elles apparaissent.
_secrets_connus: set[str] = set()

_CLE_SENSIBLE = re.compile(
    r"token|jeton|secret|password|passe|authorization|credential|signature|verifier|verificateur"
    r"|session|identity|courriel|email|username|^code$|url",
    re.IGNORECASE,
)
_MOTIFS = (
    re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*"),  # jeton JWT
    re.compile(r"\b(?:wss?|https?|myapp)://\S+"),  # URL : présignée, de rappel ou d'API
    re.compile(r"X-Amz-[\w-]+=[^&\s\"']+", re.IGNORECASE),
    re.compile(r"\b(?:code|state|refresh_token|access_token|id_token|code_verifier)=[^&\s\"']+"),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # adresse électronique
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b"),  # identifiant de clé AWS
)


_ACCES = re.compile(
    rb"eyJ[\w-]{8,}\.[\w-]{8,}\.|(?:wss?|https?|myapp)://|X-Amz-|(?:AKIA|ASIA)[A-Z0-9]{12,}", re.ASCII
)


def contient_acces(octets: bytes) -> bool:
    """SEC-06, pour des octets bruts : un secret connu, un jeton ou une URL y figure-t-il ?

    Motifs stricts, en ASCII : un contenu binaire ne doit pas être écarté par hasard."""
    return bool(_ACCES.search(octets)) or any(secret.encode() in octets for secret in _secrets_connus)


def declarer_secret(valeur: object) -> None:
    """Enregistre une valeur à ne jamais laisser sortir (jeton, clé, mot de passe)."""
    if isinstance(valeur, str) and len(valeur) >= 6:
        _secrets_connus.add(valeur)


def masquer(texte: str) -> str:
    for secret in sorted(_secrets_connus, key=len, reverse=True):
        texte = texte.replace(secret, MASQUE)
    for motif in _MOTIFS:
        texte = motif.sub(MASQUE, texte)
    return texte


def assainir(objet: Any) -> Any:
    """Copie sans secret : clés sensibles remplacées, chaînes passées au filtre."""
    if isinstance(objet, dict):
        return {
            masquer(str(cle)): MASQUE if _CLE_SENSIBLE.search(str(cle)) else assainir(valeur)
            for cle, valeur in objet.items()
        }
    if isinstance(objet, (list, tuple, set, frozenset)):
        return [assainir(element) for element in objet]
    if isinstance(objet, str):
        return masquer(objet)
    if isinstance(objet, (bool, int, float)) or objet is None:
        return objet
    return masquer(str(objet))


class Journal:
    def __init__(self, dossier: Path, horloge: Callable[[], datetime] = datetime.now) -> None:
        self._dossier = dossier / "journaux"
        self._horloge = horloge

    def ecrire(self, evenement: str, *, strict: bool = False, **champs: Any) -> None:
        """Ajoute une ligne. `strict` : lève si l'écriture échoue (avant un ordre réel)."""
        try:
            instant = self._horloge()
            ligne = {"horodatage": instant.isoformat(timespec="milliseconds"), "evenement": evenement}
            ligne.update(assainir(champs))
            self._dossier.mkdir(parents=True, exist_ok=True)
            with (self._dossier / f"{instant:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as fichier:
                fichier.write(json.dumps(ligne, ensure_ascii=False) + "\n")
        except Exception as erreur:  # noqa: BLE001 — garde large voulue (SEC-03)
            if strict:
                raise
            # Un journal en panne, quelle qu'en soit la cause, ne doit jamais gêner un arrêt.
            try:
                print(f"Journal indisponible ({type(erreur).__name__}).", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass
