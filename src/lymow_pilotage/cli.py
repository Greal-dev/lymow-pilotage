"""Commande `lymow` : analyse des arguments, simulation par défaut, affichage.

Rôle : aiguiller. Aucune règle de sécurité ne vit ici, sauf SEC-05 : sans
`--reel` explicite sur la ligne de commande, aucun ordre n'ouvre de liaison.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from dataclasses import dataclass
from typing import Any

import aiohttp

from . import auth, coffre, config, liaison, mouvement
from .journal import Journal, declarer_secret, masquer
from .mouvement import ErreurSaisie, Ordre
from .protocole import Categorie
from .securite import RefusSecurite
from .vendor.ha_lymow.api import LymowApiClient
from .vendor.ha_lymow.auth import LymowAuth

_ORDRES_SIMPLES = ("plateau-haut", "stop", "reprendre", "base", "annuler")
_AUTORISEES_ORDRES = frozenset(Categorie)
_LECTURE = frozenset({Categorie.REQUETE_PURE})
# Codes de sortie : 0 fait, 1 échec ou séquence interrompue, 2 refus ou saisie,
# 3 arrêt non confirmé (le robot peut encore bouger), 130 interruption clavier.
CODE_REFUS = 2
CODE_ARRET_NON_CONFIRME = 3
_ALERTE_ARRET = "ARRÊT NON CONFIRMÉ : arrêtez le robot par son bouton STOP ou par l'application."

_MODE_D_EMPLOI = """\
1. Ouvrez un navigateur, puis ses outils de développement (F12), onglet Réseau,
   et cochez « Preserve log » (conserver le journal).
2. Ouvrez l'adresse ci-dessous et connectez-vous avec le compte Google du robot.
3. À la fin, la page ne s'ouvre pas : c'est normal. Dans l'onglet Réseau, repérez
   la requête qui commence par myapp://callback/?code=…&state=… et copiez son
   adresse complète.
4. Collez-la à l'invite (la saisie reste invisible), puis validez.
"""


@dataclass(frozen=True)
class Contexte:
    cal: config.Calibration
    journal: Journal
    attente: float


def _communs(defauts: bool) -> argparse.ArgumentParser:
    """Options acceptées avant ou après la commande (`lymow stop --reel`)."""
    absent: Any = None if defauts else argparse.SUPPRESS
    faux: Any = False if defauts else argparse.SUPPRESS
    analyseur = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    analyseur.add_argument("--reel", action="store_true", default=faux, help="émettre réellement (sinon : simulation)")
    analyseur.add_argument("--json", action="store_true", default=faux, help="sortie JSON")
    analyseur.add_argument("--donnees", default=absent, metavar="DOSSIER", help="dossier des journaux et de calibration.toml")
    return analyseur


def construire_analyseur() -> argparse.ArgumentParser:
    analyseur = argparse.ArgumentParser(
        prog="lymow",
        description="Pilotage prudent d'un robot Lymow One Plus. Simulation par défaut.",
        parents=[_communs(True)],
        allow_abbrev=False,  # « --re » ne doit jamais valoir « --reel »
    )
    commandes = analyseur.add_subparsers(dest="commande", required=True, metavar="commande")
    fils = [_communs(False)]

    def ajouter(nom: str, aide: str) -> argparse.ArgumentParser:
        return commandes.add_parser(nom, parents=fils, help=aide, allow_abbrev=False)

    connexion = ajouter("connexion", "enregistrer l'accès au compte (Google par défaut)")
    connexion.add_argument("--mot-de-passe", action="store_true", help="compte Lymow à adresse et mot de passe")
    diagnostic = ajouter("diagnostic", "vérifier l'accès de bout en bout, sans rien publier")
    etat = ajouter("etat", "afficher l'état du robot (lecture seule)")
    etat.add_argument("--passif", action="store_true", help="écoute seule : aucune requête émise")
    for lecture in (etat, diagnostic):
        lecture.add_argument(
            "--attente", type=mouvement.lire_nombre, default=75.0, metavar="S", help="attente maximale d'un état"
        )

    nombre = mouvement.lire_nombre
    ajouter("tourner", "pivoter (positif = gauche)").add_argument("valeur", type=nombre, metavar="degrés")
    ajouter("avancer", "avancer").add_argument("valeur", type=nombre, metavar="mètres")
    ajouter("reculer", "reculer").add_argument("valeur", type=nombre, metavar="mètres")
    aides = {
        "plateau-haut": "lever le plateau de coupe au maximum",
        "stop": "arrêt du pilotage manuel (sans précondition avec --reel)",
        "reprendre": "reprendre la tâche en pause (les lames peuvent redémarrer)",
        "base": "retour à la base en conservant la tâche",
        "annuler": "annuler la tâche, arrêt sur place",
    }
    for nom in _ORDRES_SIMPLES:
        ajouter(nom, aides[nom])
    sequence = ajouter("sequence", 'enchaîner : "plateau-haut; tourner 90; avancer 1"')
    sequence.add_argument("etapes", metavar='"étape; étape; …"')
    return analyseur


def _ordres(args: argparse.Namespace) -> list[Ordre]:
    if args.commande == "sequence":
        return mouvement.analyser_sequence(args.etapes)
    return [Ordre(args.commande, getattr(args, "valeur", None))]


def _refuser(ordres: list[Ordre], ctx: Contexte, mode: str) -> dict[str, Any] | None:
    """SEC-07 : un seul plafond dépassé et rien n'est simulé, ouvert ni émis."""
    refus = mouvement.refus_prealable(ordres, ctx.cal)
    if refus is None:
        return None
    ctx.journal.ecrire("ordre-refuse", mode=mode, **refus)
    return {"mode": mode, "etapes": []} | refus


def _simuler(ordres: list[Ordre], ctx: Contexte) -> tuple[dict[str, Any], int]:
    # SEC-05 : ce chemin n'ouvre aucune liaison et ne publie rien.
    refus = _refuser(ordres, ctx, "simulation")
    if refus:
        return refus, CODE_REFUS
    appareil = coffre.appareil_connu()
    etapes = [mouvement.simuler(ordre, ctx.cal, appareil) for ordre in ordres]
    for etape in etapes:
        ctx.journal.ecrire("ordre-simule", **etape)  # SEC-06
    return {"mode": "simulation", "etapes": etapes, "interrompue": False}, 0


async def _emettre(ordres: list[Ordre], ctx: Contexte) -> tuple[dict[str, Any], int]:
    refus = _refuser(ordres, ctx, "réel")
    if refus:
        return refus, CODE_REFUS
    ctx.journal.ecrire("session-reelle", strict=True, ordres=[str(ordre) for ordre in ordres])
    async with liaison.ouvrir_session(ctx.journal, ctx.cal, _AUTORISEES_ORDRES) as session:
        bilan = await mouvement.executer_sequence(session, ordres)
    for etape in bilan["etapes"]:
        ctx.journal.ecrire("ordre-refuse" if "echec" in etape else "ordre-constate", **etape)
    if bilan.get("arret_non_confirme"):
        return bilan, CODE_ARRET_NON_CONFIRME
    return bilan, 1 if bilan["interrompue"] else 0


async def _commande_ordres(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    ordres = _ordres(args)
    bilan, code = await _emettre(ordres, ctx) if args.reel else _simuler(ordres, ctx)
    if args.commande == "sequence":
        return {"sequence": args.etapes} | bilan, code
    if bilan["etapes"]:
        return bilan["etapes"][0], code
    return {"mode": bilan["mode"], "ordre": str(ordres[0]), "echec": bilan["motif"]}, code


async def _commande_etat(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    autorisees = frozenset() if args.passif else _LECTURE
    async with liaison.ouvrir_session(ctx.journal, ctx.cal, autorisees) as session:
        await session.solliciter()
        recu = await session.attendre("statut", None, ctx.attente)
        resume = session.etat.resume(session.horloge())
    if not recu:
        resume["note"] = f"aucun état reçu en {ctx.attente:g} s : valeurs inconnues"
    return resume, 0 if recu else 1


async def _commande_diagnostic(_args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    etapes: list[dict[str, Any]] = []

    def noter(nom: str, reussi: bool, detail: Any = None) -> None:
        etapes.append({"etape": nom, "reussi": reussi} | ({} if detail is None else {"detail": detail}))

    en_cours = "jeton de rafraîchissement présent"
    try:
        ident = liaison.lire_identifiants()
        noter(en_cours, True, {"region": ident.region, "appareil": ident.appareil})
        async with aiohttp.ClientSession(timeout=liaison.DELAI_HTTP) as http:
            en_cours = "rafraîchissement du jeton"
            acces = await liaison.obtenir_acces(http, ident)
            noter(en_cours, True)
            en_cours = "liste des appareils"
            client = LymowApiClient(http, acces.jeton_acces, ident.region, acces.identite)
            appareils = _appareils(await client.get_devices())
            noter(en_cours, bool(appareils), appareils)
            en_cours = "connexion MQTT"
            # Diagnostic : aucune catégorie autorisée, donc aucune publication possible.
            async with liaison.ouvrir_session(ctx.journal, ctx.cal, frozenset(), ident, acces) as session:
                noter(en_cours, True)
                en_cours = "réception d'un état"
                recu = await session.attendre("statut", None, ctx.attente)
                resume = session.etat.resume(session.horloge())
                noter(en_cours, recu, resume["statut_libelle"] if recu else f"rien en {ctx.attente:g} s")
            en_cours = "version du micrologiciel"
            version = resume["micrologiciel"]
            if not version:
                infos = await client.get_device_info(ident.appareil)
                version = infos.get("softwareVersion") if isinstance(infos, dict) else None
            noter(en_cours, bool(version), str(version)[:40] if version else "indisponible")
    except Exception as erreur:  # noqa: BLE001 — un diagnostic rapporte l'échec, il ne s'arrête pas dessus
        noter(en_cours, False, masquer(str(erreur)) or type(erreur).__name__)
    reussi = all(etape["reussi"] for etape in etapes)
    return {"diagnostic": "complet" if reussi else "incomplet", "etapes": etapes}, 0 if reussi else 1


def _appareils(brut: Any) -> list[dict[str, str]]:
    """Réponse du nuage non fiable : on n'en garde que deux chaînes bornées."""
    liste = brut if isinstance(brut, list) else []
    return [
        {"nom": str(a.get("deviceName") or "sans nom")[:60], "appareil": str(a["deviceThingName"])[:128]}
        for a in liste
        if isinstance(a, dict) and a.get("deviceThingName")
    ]


async def _commande_connexion(args: argparse.Namespace, _ctx: Contexte) -> tuple[Any, int]:
    region = input(f"Région du compte [{auth.REGION_DEFAUT}] : ").strip() or auth.REGION_DEFAUT
    async with aiohttp.ClientSession(timeout=liaison.DELAI_HTTP) as http:
        if args.mot_de_passe:
            if region not in auth.REGIONS:
                raise auth.ErreurAuth(f"région inconnue ; régions possibles : {', '.join(auth.REGIONS)}")
            courriel = input("Adresse du compte Lymow : ").strip()
            secret = getpass.getpass("Mot de passe (saisie invisible) : ")
            jetons = await auth.connexion_mot_de_passe(http, courriel, secret, region)
            methode = auth.METHODE_MOT_DE_PASSE
        else:
            demande = auth.preparer_connexion(region)
            print(_MODE_D_EMPLOI)
            print(demande.url, end="\n\n", flush=True)
            saisie = getpass.getpass("Adresse de rappel myapp://callback/… (saisie invisible) : ")
            jetons = await auth.echanger_code(http, demande, saisie)
            methode = auth.METHODE_GOOGLE
        jeton = jetons.get("RefreshToken")
        if not isinstance(jeton, str) or not jeton:
            raise auth.ErreurAuth("le service n'a pas fourni de jeton de rafraîchissement")
        aws = await LymowAuth(http).get_aws_credentials(jetons["IdToken"], region)
        declarer_secret(aws["identity_id"])
        client = LymowApiClient(http, jetons["AccessToken"], region, aws["identity_id"])
        appareils = _appareils(await client.get_devices())
    if not appareils:
        raise auth.ErreurAuth("aucun appareil sur ce compte")
    choix = 0
    if len(appareils) > 1:
        for rang, appareil in enumerate(appareils, 1):
            print(f"  {rang}. {appareil['nom']} ({appareil['appareil']})")
        choix = int(input("Numéro de l'appareil à piloter : ")) - 1
        if not 0 <= choix < len(appareils):
            raise ErreurSaisie("numéro d'appareil hors liste")
    # Seul le jeton de rafraîchissement est conservé ; les jetons courts sont jetés.
    for cle, valeur in (
        (coffre.JETON, jeton), (coffre.REGION, region),
        (coffre.APPAREIL, appareils[choix]["appareil"]), (coffre.METHODE, methode),
    ):
        coffre.ecrire(cle, valeur)
    return {"connexion": "enregistrée dans le Gestionnaire d'identification Windows", "region": region,
            "methode": methode, **appareils[choix]}, 0


_COMMANDES = {"connexion": _commande_connexion, "diagnostic": _commande_diagnostic, "etat": _commande_etat}

# Sortie lisible : les clés JSON restent en ASCII, l'affichage porte les accents.
_LIBELLES = {
    "duree_s": "durée (s)", "cadence_hz": "cadence (Hz)", "emissions_prevues": "émissions prévues",
    "arret_final": "arrêt final", "categorie": "catégorie", "hexadecimal": "hexadécimal",
    "vitesse_lineaire_m_s": "vitesse linéaire (m/s)", "vitesse_angulaire_rad_s": "vitesse angulaire (rad/s)",
    "repetitions": "répétitions", "precede_si_telecommande": "précédé, si le robot est en télécommande, de",
    "hauteur_coupe_mm": "hauteur de coupe (mm)", "hauteur_avant_mm": "hauteur avant (mm)",
    "hauteur_apres_mm": "hauteur après (mm)", "emissions": "émissions", "etat_rafraichi": "état rafraîchi",
    "apres": "après", "statut_apres": "statut après", "deplacement_m": "déplacement (m)",
    "rotation_deg": "rotation (°)", "est_m": "est (m)", "nord_m": "nord (m)", "cap_deg": "cap (°)",
    "arrets_acquittes": "arrêts acquittés", "codes_emis": "codes émis", "echec": "échec", "etapes": "étapes",
    "sequence": "séquence", "en_ligne": "en ligne", "batterie_pct": "batterie (%)",
    "statut_libelle": "statut (libellé)", "position_est_m": "position est (m)",
    "position_nord_m": "position nord (m)", "age_position_s": "âge de la position (s)",
    "rtk_satellites": "satellites RTK", "signal_4g": "signal 4G", "signal_wifi": "signal Wi-Fi",
    "wifi_rssi_dbm": "Wi-Fi RSSI (dBm)", "age_etat_s": "âge de l'état (s)", "etape": "étape",
    "reussi": "réussi", "detail": "détail", "region": "région", "methode": "méthode",
    "puis_pause": "puis mise en pause de la tâche", "acquittee": "acquittée",
    "non_executees": "étapes non exécutées", "arret_non_confirme": "arrêt non confirmé",
    "avance_m": "avance le long du cap (m)", "verifie": "ordre vérifié par le constat",
}


def _lignes(objet: Any, retrait: int = 0) -> list[str]:
    marge = "  " * retrait
    if isinstance(objet, dict):
        lignes: list[str] = []
        for cle, valeur in objet.items():
            titre = _LIBELLES.get(cle) or str(cle).replace("_", " ")
            if isinstance(valeur, (dict, list)) and valeur:
                lignes += [f"{marge}{titre} :", *_lignes(valeur, retrait + 1)]
            else:
                lignes.append(f"{marge}{titre} : {_valeur(valeur)}")
        return lignes
    if isinstance(objet, list):
        lignes = []
        for element in objet:
            bloc = _lignes(element, retrait + 1)
            lignes += [f"{marge}- {bloc[0].lstrip()}", *bloc[1:]] if bloc else []
        return lignes
    return [f"{marge}{_valeur(objet)}"]


def _valeur(valeur: Any) -> str:
    if valeur is None or valeur == [] or valeur == {}:
        return "inconnu" if valeur is None else "aucun"
    if isinstance(valeur, bool):
        return "oui" if valeur else "non"
    return str(valeur)


def main(argv: list[str] | None = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(errors="replace")
    args = construire_analyseur().parse_args(argv)
    # aiomqtt exige une boucle à sélecteur ; la boucle par défaut de Windows n'en est pas une.
    fabrique = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        dossier = config.dossier_donnees(args.donnees)
        ctx = Contexte(config.charger_calibration(dossier), Journal(dossier), getattr(args, "attente", 75.0))
        commande = _COMMANDES.get(args.commande, _commande_ordres)
        resultat, code = asyncio.run(commande(args, ctx), loop_factory=fabrique)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Interrompu.", file=sys.stderr)
        return 130
    except Exception as erreur:  # noqa: BLE001 — sortie unique, filtrée (SEC-06)
        print(f"Erreur : {masquer(str(erreur)) or type(erreur).__name__}", file=sys.stderr)
        if isinstance(erreur, mouvement.ArretNonConfirme):
            return CODE_ARRET_NON_CONFIRME
        refus = (RefusSecurite, ErreurSaisie, config.ErreurConfiguration)
        return CODE_REFUS if isinstance(erreur, refus) else 1
    if code == CODE_ARRET_NON_CONFIRME:
        print(_ALERTE_ARRET, file=sys.stderr, flush=True)  # en tête, avant tout compte rendu
    if args.json:
        print(json.dumps(resultat, ensure_ascii=False, indent=2))
    else:
        print("\n".join(_lignes(resultat)))
    return code


if __name__ == "__main__":
    sys.exit(main())
