"""Commande `lymow` : analyse des arguments, simulation par défaut, affichage.

Rôle : aiguiller. Aucune règle de sécurité ne vit ici, sauf SEC-05 : sans
`--reel` explicite sur la ligne de commande, aucun ordre n'ouvre de liaison.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hmac
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from . import auth, carte, compte, conduite, config, journal, liaison, mouvement, rappel, retouche, securite
from .conduite import CODE_ARRET_NON_CONFIRME, CODE_REFUS
from .journal import Journal, masquer
from .mouvement import ErreurSaisie, Ordre
from .protocole import Categorie
from .securite import RefusSecurite
from .vendor.ha_lymow.api import LymowApiClient

_ORDRES_SIMPLES = ("plateau-haut", "stop", "reprendre", "base", "annuler")
_AUTORISEES_ORDRES = frozenset(Categorie)
_LECTURE = frozenset({Categorie.REQUETE_PURE})
PORT_UI = 8377  # même valeur que ui.PORT_DEFAUT, sans charger le serveur web pour les autres commandes
_ALERTE_ARRET ="ARRÊT NON CONFIRMÉ : arrêtez le robot par son bouton STOP ou par l'application."

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
    dossier: Path = Path(".")


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

    connexion = ajouter("connexion", "enregistrer l'accès au compte (Google par défaut ; possible aussi depuis « lymow ui »)")
    connexion.add_argument("--mot-de-passe", action="store_true", help="compte Lymow à adresse et mot de passe")
    ajouter("deconnexion", "effacer de ce poste l'accès au compte (rien n'est supprimé côté Lymow)")
    diagnostic = ajouter("diagnostic", "vérifier l'accès de bout en bout, sans rien publier")
    etat = ajouter("etat", "afficher l'état du robot (lecture seule)")
    etat.add_argument("--passif", action="store_true", help="écoute seule : aucune requête émise")
    for lecture in (etat, diagnostic):
        lecture.add_argument(
            "--attente", type=mouvement.lire_nombre, default=75.0, metavar="S", help="attente maximale d'un état"
        )

    nombre = mouvement.lire_nombre
    mouvements = (
        ("tourner", "pivoter (positif = gauche)", "degrés", "rad/s", securite.VITESSE_ANGULAIRE_RAD_S),
        ("avancer", "avancer", "mètres", "m/s", securite.VITESSE_LINEAIRE_M_S),
        ("reculer", "reculer", "mètres", "m/s", securite.VITESSE_LINEAIRE_M_S),
    )
    for nom, aide, unite, unite_vitesse, plafond in mouvements:
        fils_mouvement = ajouter(nom, f"{aide} : <{unite}> ou --duree S")
        fils_mouvement.add_argument("valeur", type=nombre, nargs="?", default=None, metavar=unite)
        fils_mouvement.add_argument(
            "--vitesse", type=nombre, default=None, metavar="V",
            help=f"vitesse en {unite_vitesse}, de 0.05 à {plafond:g} (défaut : {plafond:g})"
            + (" ; avec --duree, une valeur négative tourne à droite" if nom == "tourner" else ""),
        )
        fils_mouvement.add_argument(
            "--duree", type=nombre, default=None, metavar="S",
            help=f"tenir la consigne S secondes (au plus {securite.DUREE_MAX_S:g}) au lieu de donner des {unite}",
        )
    plateau = ajouter("plateau", f"régler la hauteur du plateau de coupe ({securite.HAUTEUR_MIN_MM} à {securite.HAUTEUR_MAX_MM} mm)")
    plateau.add_argument("valeur", type=mouvement.lire_entier, metavar="mm")
    essai = ajouter(
        mouvement.ESSAI,
        "calibration, robot à vue : le robot s'arrête-t-il seul quand les consignes cessent ? "
        f"Une seule consigne avant, puis silence, puis arrêt. Exposition : {mouvement.EXPOSITION_ESSAI}.",
    )
    essai.add_argument(
        "--vitesse", type=nombre, default=None, metavar="V",
        help=f"m/s, de 0.05 à {securite.ESSAI_VITESSE_MAX_M_S:g} (défaut : {securite.ESSAI_VITESSE_M_S:g})",
    )
    essai.add_argument(
        "--observation", type=nombre, default=None, metavar="S", dest="duree",
        help=f"fenêtre sans émission, de {securite.ESSAI_OBSERVATION_MIN_S:g} à "
        f"{securite.ESSAI_OBSERVATION_MAX_S:g} s (défaut : {securite.ESSAI_OBSERVATION_S:g})",
    )
    aides = {
        "plateau-haut": "lever le plateau de coupe au maximum",
        "stop": "arrêt du pilotage manuel (sans précondition avec --reel)",
        "reprendre": "reprendre la tâche en pause (les lames peuvent redémarrer)",
        "base": "retour à la base en conservant la tâche",
        "annuler": "annuler la tâche, arrêt sur place",
    }
    for nom in _ORDRES_SIMPLES:
        ajouter(nom, aides[nom])
    sequence = ajouter(
        "sequence", 'enchaîner : "plateau 90; tourner 45 --vitesse 0.2; reculer --duree 2 --vitesse 0.1"'
    )
    sequence.add_argument("etapes", metavar='"étape; étape; …"')
    ajouter("carte", "afficher la carte du robot : zones, zones interdites, passages, station (lecture seule)")
    ajuster = ajouter(
        "ajuster-zone",
        "élargir (cm > 0) ou rogner (cm < 0) une bande le long d'une portion du contour d'une zone ; "
        "simulation sans --reel",
    )
    ajuster.add_argument("zone", metavar="nom|identifiant")
    ajuster.add_argument("--de", type=mouvement.lire_entier, required=True, metavar="I", help="indice du premier sommet")
    ajuster.add_argument("--a", type=mouvement.lire_entier, required=True, metavar="J", help="indice du dernier sommet")
    ajuster.add_argument(
        "--cm", type=nombre, required=True, metavar="X",
        help=f"largeur de la bande en cm, de -{securite.AJUSTEMENT_MAX_M * 100:g} à {securite.AJUSTEMENT_MAX_M * 100:g}",
    )
    ajuster.add_argument("--cote", choices=("court", "long", "tout"), default="court", help="côté de la boucle (défaut : court)")
    ajuster.add_argument(
        "--raccord", type=nombre, default=None, metavar="M",
        help=f"longueur des raccords progressifs en m (défaut : {securite.RACCORD_DEFAUT_M:g})",
    )
    restaurer = ajouter("restaurer-zone", "remettre le contour d'une zone depuis une sauvegarde ; simulation sans --reel")
    restaurer.add_argument("fichier", metavar="sauvegarde.pb")
    restaurer.add_argument("--zone", default=None, metavar="identifiant", help="identifiant exact de la zone")
    ajuster.add_argument(
        "--nouvelle-reference", action="store_true",
        help="prendre la carte actuelle du robot comme nouvelle référence de dérive cumulée",
    )
    for ecriture in (ajuster, restaurer):
        ecriture.add_argument(
            "--confirmer", default=None, metavar="EMPREINTE",
            help="exigé avec --reel : l'empreinte affichée par la même commande en simulation",
        )
    ajouter("rappel-installer", "activer la connexion Google en un clic (gestionnaire Windows du protocole myapp)")
    ajouter("rappel-desinstaller", "retirer la connexion en un clic (ne retire que la clé de cet outil)")
    interface = ajouter(
        "ui", "interface web locale (127.0.0.1 seulement) ; simulation sans --reel, quoi que demande la page ; "
        "la connexion au compte Google peut s'y faire"
    )
    interface.add_argument(
        "--port", type=_lire_port, default=PORT_UI, metavar="N", help=f"port local (défaut : {PORT_UI})"
    )
    return analyseur


def _lire_port(texte: str) -> int:
    # `lire_entier` s'arrête à quatre chiffres (millimètres) : un port en compte jusqu'à cinq.
    if not (texte.isascii() and texte.isdigit() and len(texte) <= 5):
        raise ValueError("numéro de port attendu")
    return int(texte)


async def _commande_ui(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    from . import ui  # import tardif : le serveur web ne sert qu'à cette commande

    if not 1024 <= args.port <= 65535:
        raise ErreurSaisie("port attendu entre 1024 et 65535")
    return {"interface": await ui.servir(ctx.cal, ctx.journal, reel=args.reel, port=args.port, dossier=ctx.dossier)}, 0


def _ordres(args: argparse.Namespace) -> list[Ordre]:
    if args.commande == "sequence":
        return mouvement.analyser_sequence(args.etapes)
    lire = lambda nom: getattr(args, nom, None)  # noqa: E731
    return [Ordre(args.commande, lire("valeur"), lire("vitesse"), lire("duree"))]


async def _emettre(ordres: list[Ordre], ctx: Contexte) -> tuple[dict[str, Any], int]:
    refus = conduite.refuser(ordres, ctx.cal, ctx.journal, "réel")
    if refus:
        return refus, CODE_REFUS
    async with liaison.ouvrir_session(ctx.journal, ctx.cal, _AUTORISEES_ORDRES) as session:
        return await conduite.emettre_ordres(session, ordres, ctx.journal)


async def _commande_ordres(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    ordres = _ordres(args)
    bilan, code = await _emettre(ordres, ctx) if args.reel else conduite.simuler_ordres(ordres, ctx.cal, ctx.journal)
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
            appareils = compte.appareils_lisibles(await client.get_devices())
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
        jeton = compte.jeton_durable(jetons)
        appareils = await compte.appareils_du_compte(http, jetons, region)
    choix = 0
    if len(appareils) > 1:
        for rang, appareil in enumerate(appareils, 1):
            print(f"  {rang}. {appareil['nom']} ({appareil['appareil']})")
        choix = int(input("Numéro de l'appareil à piloter : ")) - 1
        if not 0 <= choix < len(appareils):
            raise ErreurSaisie("numéro d'appareil hors liste")
    compte.enregistrer(jeton, region, appareils[choix]["appareil"], methode)
    return {"connexion": "enregistrée dans le Gestionnaire d'identification Windows", "region": region,
            "methode": methode, **appareils[choix]}, 0


async def _commande_deconnexion(_args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    compte.oublier()
    ctx.journal.ecrire("compte-oublie")
    return {"connexion": "effacée de ce poste (Gestionnaire d'identification Windows)"}, 0


def _sans_contours(objet: Any) -> Any:
    """Sortie lisible : les listes de points ne sont utiles qu'en JSON."""
    if isinstance(objet, dict):
        return {cle: _sans_contours(valeur) for cle, valeur in objet.items() if cle not in ("contour", "trace")}
    if isinstance(objet, list):
        return [_sans_contours(element) for element in objet]
    return objet


async def _commande_carte(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    async with liaison.ouvrir_session(ctx.journal, ctx.cal, _LECTURE) as session:
        await session.solliciter()
        recue = await carte.demander(session, securite.ATTENTE_CARTE_S)
        await session.attendre("pose", None, 5.0)
        etat = session.etat.resume(session.horloge())
    robot = {cle: etat[cle] for cle in ("position_est_m", "position_nord_m", "cap_deg", "rtk", "age_position_s")}
    resultat = {"horodatage": datetime.now().isoformat(timespec="seconds")} | carte.vers_json(recue) | {"robot": robot}
    return (resultat if args.json else _sans_contours(resultat)), 0


async def _retoucher(args: argparse.Namespace, ctx: Contexte, preparer: Any) -> tuple[Any, int]:
    """Simulation : lit la carte (comme `etat`), calcule, écrit un aperçu. Réel : SEC-08 à SEC-11."""
    autorisees = _AUTORISEES_ORDRES if args.reel else _LECTURE
    async with liaison.ouvrir_session(ctx.journal, ctx.cal, autorisees) as session:
        operation = preparer(await carte.demander(session, securite.ATTENTE_CARTE_S), session)
        retouche.controler_cumul(ctx.dossier, session.appareil, operation)  # SEC-10 cumulatif, dès l'aperçu
        if args.reel:
            # L'empreinte lie l'émission à CE que la simulation a montré : carte, zone, contour.
            fournie = (args.confirmer or "").encode()
            if not hmac.compare_digest(fournie, operation.empreinte.encode()):
                raise RefusSecurite(
                    "--confirmer absent ou différent : relancez la même commande sans --reel, "
                    "vérifiez l'aperçu, puis recopiez l'empreinte affichée"
                )
            return await retouche.emettre(session, operation, ctx.dossier)
    apercu = retouche.ecrire_apercu(operation, ctx.dossier, datetime.now())
    ctx.journal.ecrire("carte-simulee", ordre=operation.description, resume=operation.resume, apercu=str(apercu))
    note = (
        "simulation : la carte a été lue, rien n'a été écrit sur le robot ; pour émettre, relancez avec "
        f"--reel --confirmer {operation.empreinte}"
    )
    return {"mode": "simulation", "ordre": operation.description} | operation.resume | {
        "apercu": str(apercu), "empreinte": operation.empreinte, "note": note,
    }, 0


async def _commande_ajuster(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    return await _retoucher(
        args, ctx,
        lambda recue, _session: retouche.preparer_ajustement(
            recue, args.zone, args.de, args.a, args.cm, args.cote, args.raccord, args.nouvelle_reference
        ),
    )


async def _commande_restaurer(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    return await _retoucher(
        args, ctx,
        lambda recue, session: retouche.preparer_restauration(
            recue, retouche.charger_sauvegarde(Path(args.fichier), session.appareil), args.zone, args.fichier
        ),
    )


async def _commande_un_clic(args: argparse.Namespace, ctx: Contexte) -> tuple[Any, int]:
    message = rappel.installer() if args.commande == "rappel-installer" else rappel.desinstaller()
    ctx.journal.ecrire("un-clic", commande=args.commande)
    return {"connexion_en_un_clic": message}, 0


def _recevoir_rappel(arguments: list[str]) -> int:
    """`lymow rappel <adresse>` : appelé par Windows avec une adresse venue du navigateur.

    Aucune option n'existe ici (ni --reel ni rien) : un seul argument, de forme stricte.
    L'adresse n'est ni affichée ni journalisée, même en cas d'erreur.
    """
    try:
        adresse = rappel.valider(arguments)
        journal.declarer_secret(adresse)
        fabrique = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        remise = asyncio.run(rappel.transmettre(adresse, rappel.ports_connus(config.dossier_donnees())), loop_factory=fabrique)
    except ErreurSaisie:
        print("Adresse de rappel refusée : rien n'a été transmis.", file=sys.stderr)
        _retenir_la_fenetre()
        return CODE_REFUS
    except Exception as erreur:  # noqa: BLE001 — jamais le texte d'origine : il pourrait citer l'adresse
        print(f"Transmission impossible ({type(erreur).__name__}).", file=sys.stderr)
        _retenir_la_fenetre()
        return 1
    if remise:
        print("Connexion transmise à l'interface Lymow : cette fenêtre peut se fermer.")
        return 0
    print(
        "Aucune interface Lymow n'attend cette connexion. Lancez « lymow ui », cliquez sur "
        "« Se connecter avec Google », puis recommencez.",
        file=sys.stderr,
    )
    _retenir_la_fenetre()
    return 1


_COMMANDES = {
    "rappel-installer": _commande_un_clic, "rappel-desinstaller": _commande_un_clic,
    "carte": _commande_carte, "ajuster-zone": _commande_ajuster, "restaurer-zone": _commande_restaurer,
    "connexion": _commande_connexion, "deconnexion": _commande_deconnexion,
    "diagnostic": _commande_diagnostic, "etat": _commande_etat,
    "ui": _commande_ui,
}

def _lignes(objet: Any, retrait: int = 0) -> list[str]:
    marge = "  " * retrait
    if isinstance(objet, dict):
        lignes: list[str] = []
        for cle, valeur in objet.items():
            titre = conduite.LIBELLES.get(cle) or str(cle).replace("_", " ")
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


def _retenir_la_fenetre() -> None:
    """Double-clic : sans cette pause, la fenêtre se ferme avant qu'on ait lu l'erreur."""
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            input("Appuyez sur Entrée pour fermer.")
    except (EOFError, OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(errors="replace")
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] == ["rappel"]:  # avant tout analyseur : cette commande n'accepte aucune option
        return _recevoir_rappel(arguments[1:])
    args = construire_analyseur().parse_args(arguments)
    # aiomqtt exige une boucle à sélecteur ; la boucle par défaut de Windows n'en est pas une.
    fabrique = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        dossier = config.dossier_donnees(args.donnees)
        ctx = Contexte(config.charger_calibration(dossier), Journal(dossier), getattr(args, "attente", 75.0), dossier)
        commande = _COMMANDES.get(args.commande, _commande_ordres)
        resultat, code = asyncio.run(commande(args, ctx), loop_factory=fabrique)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Interrompu.", file=sys.stderr)
        return 130
    except Exception as erreur:  # noqa: BLE001 — sortie unique, filtrée (SEC-06)
        print(f"Erreur : {masquer(str(erreur)) or type(erreur).__name__}", file=sys.stderr)
        if isinstance(erreur, mouvement.ArretNonConfirme):
            return CODE_ARRET_NON_CONFIRME
        if args.commande == "ui":
            _retenir_la_fenetre()
        refus = (RefusSecurite, ErreurSaisie, config.ErreurConfiguration, carte.CarteIllisible, rappel.EchecRappel)
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
