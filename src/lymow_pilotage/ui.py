"""Interface web locale `lymow ui` : serveur HTTP sur la boucle locale uniquement.

Rôle : exposer à une page servie localement les MÊMES chemins que la ligne de
commande (`conduite`). Aucune émission propre à l'interface. Frontières : écoute
sur 127.0.0.1 sans option pour en changer, jeton par lancement sur toutes les
routes, contrôle de Host et d'Origin, aucune identité saisie dans la page.
Le mouvement vit ici, côté serveur : la page peut disparaître, l'arrêt part.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import math
import secrets
import sys
import time
import webbrowser
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

import aiohttp
from aiohttp import web

from . import auth, capture, compte, conduite, demo, fleches, geometrie, liaison, releve, mouvement, rappel, retouche, securite
from . import carte as cartes
from .config import Calibration
from .journal import Journal, assainir, declarer_secret, masquer
from .mouvement import ErreurSaisie, Ordre
from .securite import RefusSecurite
from .protocole import Categorie
from .session import Session
from .vendor.ha_lymow import const as socle

HOTE = "127.0.0.1"  # jamais 0.0.0.0 : aucune option ne change cette valeur
PORT_DEFAUT = 8377
COOKIE = "lymow_ui"
DUREE_PREVISUALISATION_S = 60.0
DUREE_TENTATIVE_S = 600.0  # connexion au compte : une tentative à la fois, dix minutes
ETAPES_MAX = 20
NOMBRE_MAX = 1_000_000  # au-delà, une valeur de la page est refusée sans calcul
STATIQUE = Path(__file__).resolve().parent / "ui_statique"
FICHIERS = {
    "app.js": "text/javascript",
    "signalisation.js": "text/javascript",
    "style.css": "text/css",
    "carte.js": "text/javascript",
    "ordres.js": "text/javascript",
    "fleches.js": "text/javascript",
    "pave.js": "text/javascript",
    "volets.js": "text/javascript",
}
NOMS_ORDRES = (*mouvement.ETAPES_SEQUENCE, mouvement.ESSAI, *mouvement.TACHES)
NON_CONNECTE = "non connecté — utilisez le panneau « Compte » de cette page"
ROUTE_REOUVERTURE = "/api/instance/rouvrir"
ROUTE_IDENTITE = "/api/instance/identite"
ROUTE_ARRET = "/api/instance/arreter"
ROUTES_LOCALES = (ROUTE_REOUVERTURE, ROUTE_IDENTITE, ROUTE_ARRET, rappel.ROUTE)
ROUTE_ATTESTATION_POSER = "/api/attestation/poser"
ROUTE_ATTESTATION_RETIRER = "/api/attestation/retirer"
REMPLACEE = "ancienne version remplacée"
DELAI_LIBERATION_S = 10.0
# Après un ordre réel, les trames reçues sont encore enregistrées ce temps-là (diagnostic).
SUITE_CAPTURE_S = 20.0
# Après « Mettre en pause » : temps laissé au robot pour annoncer son statut « pause ».
DELAI_PAUSE_S = 15.0
# Veille de la liaison : une coupure est vue et réparée sans attendre un clic.
PERIODE_VEILLE_S = 2.0
LIAISON_SUSPENDUE = "Liaison suspendue : l'application Lymow a la main"
LIAISON_COUPEE = (
    "liaison coupée par le service — peut-être reprise par une autre application "
    "(l'application officielle ?) : reconnexion automatique"
)


def charger_statiques() -> dict[str, str]:
    """Page et fichiers statiques, lus UNE fois : la page servie reste celle du code en mémoire,
    même si les fichiers changent sur le disque pendant que le serveur tourne (panne du 2026-10-12)."""
    return {nom: (STATIQUE / nom).read_text(encoding="utf-8") for nom in ("index.html", *FICHIERS)}


def empreinte_du_code() -> str:
    """Condensat court des fichiers .py et statiques du paquet : deux lancements du même code le partagent."""
    condense = hashlib.sha256()
    paquet = STATIQUE.parent
    fichiers = [f for f in paquet.rglob("*") if f.is_file() and (f.suffix == ".py" or f.parent == STATIQUE)]
    for fichier in sorted(fichiers):
        condense.update(fichier.relative_to(paquet).as_posix().encode() + b"\0" + fichier.read_bytes() + b"\0")
    return condense.hexdigest()[:12]
DELAI_REOUVERTURE_S = 3.0  # au plus une réouverture de page toutes les trois secondes
DELAI_RAPPEL_S = 1.0  # au plus une remise d'adresse de rappel par seconde
MODES = ("simulation", "réel", "démonstration")


class EchecDemarrage(Exception):
    """L'interface n'a pas pu démarrer ; le message dit quoi faire."""


def _bornes() -> dict[str, Any]:
    """Bornes affichées dans la page : lues dans `securite`, jamais recopiées à la main."""
    s = securite
    return {
        "angle_max_deg": s.ANGLE_MAX_DEG, "distance_max_m": s.DISTANCE_MAX_M, "duree_max_s": s.DUREE_MAX_S,
        "vitesse_lineaire_min": s.VITESSE_LINEAIRE_MIN_M_S, "vitesse_lineaire_max": s.VITESSE_LINEAIRE_M_S,
        "vitesse_angulaire_min": s.VITESSE_ANGULAIRE_MIN_RAD_S, "vitesse_angulaire_max": s.VITESSE_ANGULAIRE_RAD_S,
        "hauteur_min_mm": s.HAUTEUR_MIN_MM, "hauteur_max_mm": s.HAUTEUR_MAX_MM,
        "essai_vitesse": s.ESSAI_VITESSE_M_S, "essai_vitesse_max": s.ESSAI_VITESSE_MAX_M_S,
        "essai_observation": s.ESSAI_OBSERVATION_S, "essai_observation_min": s.ESSAI_OBSERVATION_MIN_S,
        "essai_observation_max": s.ESSAI_OBSERVATION_MAX_S, "age_max_etat_s": s.AGE_MAX_ETAT_S,
        "previsualisation_s": DUREE_PREVISUALISATION_S, "exposition_essai": mouvement.EXPOSITION_ESSAI,
    }


def _nombre(valeur: Any, entier: bool = False) -> float | int | None:
    if valeur is None:
        return None
    if isinstance(valeur, bool) or not isinstance(valeur, int if entier else (int, float)):
        raise ErreurSaisie("valeur numérique attendue")
    if isinstance(valeur, int) and abs(valeur) > NOMBRE_MAX:  # 10**400 ferait déborder isfinite
        raise ErreurSaisie("valeur numérique démesurée")
    if not math.isfinite(valeur) or abs(valeur) > NOMBRE_MAX:
        raise ErreurSaisie("valeur numérique finie attendue")
    return valeur


def ordres_depuis(corps: Any) -> list[Ordre]:
    """Données de la page non fiables : chaque champ est revérifié, puis `conduite` valide."""
    liste = corps.get("ordres") if isinstance(corps, dict) else None
    if not isinstance(liste, list) or not 1 <= len(liste) <= ETAPES_MAX:
        raise ErreurSaisie(f"liste de 1 à {ETAPES_MAX} ordres attendue")
    ordres = []
    for entree in liste:
        nom = entree.get("nom") if isinstance(entree, dict) else None
        if nom not in NOMS_ORDRES:
            raise ErreurSaisie("ordre inconnu")
        if set(entree) - {"nom", "valeur", "vitesse", "duree"}:
            raise ErreurSaisie("champ inattendu dans un ordre")
        mobile = nom in mouvement.MOUVEMENTS or nom == mouvement.ESSAI
        if not mobile and (entree.get("vitesse") is not None or entree.get("duree") is not None):
            raise ErreurSaisie(f"« {nom} » n'accepte ni vitesse ni durée")
        if nom not in (*mouvement.MOUVEMENTS, "plateau") and entree.get("valeur") is not None:
            raise ErreurSaisie(f"« {nom} » n'accepte pas de valeur")
        ordres.append(
            Ordre(
                nom, _nombre(entree.get("valeur"), entier=nom == "plateau"),
                _nombre(entree.get("vitesse")), _nombre(entree.get("duree")),
            )
        )
    if len(ordres) > 1 and any(ordre.nom not in mouvement.ETAPES_SEQUENCE for ordre in ordres):
        raise ErreurSaisie("une séquence n'accepte que : " + ", ".join(mouvement.ETAPES_SEQUENCE))
    return ordres


class ServeurUI:
    def __init__(
        self, cal: Calibration, journal: Journal, *, reel: bool, port: int = PORT_DEFAUT, dossier: Path | None = None,
        demonstration: bool = False,
    ) -> None:
        if reel and demonstration:
            raise ErreurSaisie("--demo et --reel sont incompatibles : la démonstration ne parle jamais au vrai robot")
        self.cal, self.journal, self.reel, self.port = cal, journal, reel, port
        self.demonstration = demonstration
        # En démonstration les ordres « partent » vers un robot fictif en mémoire : aucun invariant
        # n'est relâché, c'est la liaison qui est fausse.
        self.emet = reel or demonstration
        self.dossier = dossier if dossier is not None else journal._dossier.parent  # sauvegardes de carte
        self.carte_heure: str | None = None
        # Version : figée au lancement, avec la page qui va avec.
        self.statiques = charger_statiques()
        self.empreinte = empreinte_du_code()
        self.demarre = datetime.now().strftime("%H:%M")
        self._liste: tuple[bytes, list[dict[str, Any]]] = (b"", [])
        # Deux secrets de 256 bits, neufs à chaque lancement : le jeton d'adresse sert UNE fois,
        # puis seul le témoin (valeur distincte, jamais dans une adresse) authentifie la page.
        self.jeton: str | None = secrets.token_urlsafe(32)
        self.temoin = secrets.token_urlsafe(32)
        declarer_secret(self.jeton)
        declarer_secret(self.temoin)
        self.horloge = time.monotonic
        self.session: Any = None
        self.ident: liaison.Identifiants | None = None
        self.etat_liaison = NON_CONNECTE
        self.tache: asyncio.Task[None] | None = None
        self.previsualisation: tuple[str, Any, float] | None = None  # ordres, ou retouche de carte
        self.resultat: dict[str, Any] | None = None
        self.fil: list[dict[str, str]] = []
        self.version = 0
        self.ferme = False
        self.fin = asyncio.Event()
        self.fournir_video = self._video
        self.ouvrir_navigateur = webbrowser.open
        self._derniere_reouverture: float | None = None
        self._dernier_rappel: float | None = None
        self.registre: Any = None  # None : registre Windows réel ; les tests y mettent un double
        self.un_clic: dict[str, Any] = {"pris_en_charge": False, "actif": False, "perime": False, "etranger": None}
        self._battement: asyncio.Task[None] | None = None  # démonstration : état périodique du robot fictif
        self._veille: asyncio.Task[None] | None = None
        # Pilotage direct par flèches : désarmé par défaut, armé explicitement, 60 s sans action au plus.
        self.direct = fleches.Armement(lambda: self.horloge())
        # Pause demandée : (instant de la demande, échéance, statut attendu) ; jamais suivie d'un mouvement automatique.
        self._pause: tuple[float, float, int] | None = None
        self._direct_en_cours = False
        # « Rendre la main à l'application » : liaison fermée, reconnexion automatique suspendue.
        self.suspendue = False
        self._releve = releve.Releve(self.dossier)
        # SEC-01b : attestation « lames à l'arrêt » de l'opérateur ; en mémoire, jamais active au lancement.
        self.attestation = securite.Attestation(lambda: self.horloge(), self._tracer_attestation)
        self._ecoute: capture.Enregistreur | None = None  # trames brutes pendant un ordre réel
        self._fin_ecoute: asyncio.TimerHandle | None = None
        self.video_ouverte = False
        # Connexion au compte : vérificateur PKCE et `state` ne quittent jamais la mémoire du serveur.
        self.tentative: tuple[auth.DemandeOAuth, float] | None = None
        self.choix: dict[str, Any] | None = None
        self.ouvrir_http = lambda: aiohttp.ClientSession(timeout=liaison.DELAI_HTTP)
        self._stop_demande = False
        self._verrou_stop = asyncio.Lock()
        # Un seul rafraîchissement de jeton à la fois : Cognito fait tourner le jeton stocké.
        self._verrou_acces = asyncio.Lock()
        self._pile = AsyncExitStack()
        self._lanceur: web.AppRunner | None = None

    # --- cycle de vie -------------------------------------------------------

    async def demarrer(self) -> None:
        application = web.Application(middlewares=[self._garde], client_max_size=64 * 1024)
        application.add_routes(
            [
                web.get("/", self._page),
                web.get("/statique/{nom}", self._statique),
                web.get("/api/etat", self._etat),
                web.get("/api/flux", self._flux),
                web.post("/api/actualiser", self._actualiser),
                web.post("/api/previsualiser", self._previsualiser),
                web.post("/api/emettre", self._emettre),
                web.post("/api/stop", self._stop),
                web.post("/api/pause", self._pause_raccourci),
                web.post("/api/liaison/rendre", self._liaison_rendre),
                web.post("/api/liaison/reprendre", self._liaison_reprendre),
                web.get("/api/releve", self._releve_lire),
                web.post("/api/direct/armer", self._direct_armer),
                web.post("/api/direct/desarmer", self._direct_desarmer),
                web.post("/api/direct/fleche", self._direct_fleche),
                web.post(ROUTE_ATTESTATION_POSER, self._attestation_poser),
                web.post(ROUTE_ATTESTATION_RETIRER, self._attestation_retirer),
                web.post("/api/video/demarrer", self._video_demarrer),
                web.post("/api/video/arreter", self._video_arreter),
                web.get("/favicon.ico", self._sans_contenu),
                web.post(ROUTE_REOUVERTURE, self._rouvrir),
                web.post(ROUTE_IDENTITE, self._identite),
                web.post(ROUTE_ARRET, self._arret_demande),
                web.post(rappel.ROUTE, self._rappel),
                web.post("/api/compte/un-clic", self._compte_un_clic),
                web.get("/api/carte", self._carte),
                web.post("/api/carte/actualiser", self._carte_actualiser),
                web.post("/api/carte/previsualiser", self._carte_previsualiser),
                web.post("/api/compte/commencer", self._compte_commencer),
                web.post("/api/compte/valider", self._compte_valider),
                web.post("/api/compte/choisir", self._compte_choisir),
                web.post("/api/compte/deconnecter", self._compte_deconnecter),
            ]
        )
        # Pas de journal d'accès : la première requête porte le jeton dans son adresse.
        self._lanceur = web.AppRunner(application, access_log=None, shutdown_timeout=1.0)
        await self._lanceur.setup()
        site = web.TCPSite(self._lanceur, HOTE, self.port)
        await site.start()
        self.port = self.adresses_ecoute()[0][1]
        self.un_clic = rappel.etat(self.registre)
        await self._connecter()
        self._veille = asyncio.create_task(self._veiller())
        self._noter(f"interface démarrée en mode {self.mode}")

    def adresses_ecoute(self) -> list[tuple[str, int]]:
        assert self._lanceur is not None
        return [prise.getsockname()[:2] for site in self._lanceur.sites for prise in site._server.sockets]  # type: ignore[union-attr]

    @property
    def adresse(self) -> str:
        return f"http://{HOTE}:{self.port}/"

    @property
    def mode(self) -> str:
        return MODES[2] if self.demonstration else MODES[1] if self.reel else MODES[0]

    def ouvrir_page(self) -> bool:
        """Nouveau jeton d'adresse à usage unique, donné au navigateur par CE processus seulement."""
        self.jeton = secrets.token_urlsafe(32)
        declarer_secret(self.jeton)
        adresse = f"{self.adresse}?jeton={self.jeton}"
        try:
            ouvert = bool(self.ouvrir_navigateur(adresse))
        except Exception:  # noqa: BLE001 — aucun navigateur utilisable
            ouvert = False
        if not ouvert:
            print("Le navigateur n'a pas pu être ouvert automatiquement.")
            print(f"Ouvrez cette adresse (elle ne sert qu'une fois) : {adresse}", flush=True)
        return ouvert

    async def arreter(self) -> None:
        """SEC-03 : un mouvement en cours reçoit son arrêt AVANT la fermeture de la liaison."""
        if self.ferme:
            return
        self.ferme = True
        self._stop_demande = True
        self.direct.desarmer()
        await self._interrompre()
        self.attestation.retirer("arrêt du serveur")  # SEC-01b ; après l'arrêt du mouvement, jamais avant
        self._fermer_ecoute()
        if self._veille is not None:
            self._veille.cancel()
            await asyncio.gather(self._veille, return_exceptions=True)
        if self._battement is not None:
            self._battement.cancel()
            await asyncio.gather(self._battement, return_exceptions=True)
        await self._pile.aclose()
        self.session = None
        if self._lanceur is not None:
            await self._lanceur.cleanup()

    async def _interrompre(self) -> None:
        if self.tache is not None and not self.tache.done():
            self.tache.cancel()  # le `finally` du mouvement émet l'arrêt, à l'abri de l'annulation
            await asyncio.gather(self.tache, return_exceptions=True)

    async def _connecter(self) -> None:
        async with self._verrou_acces:
            await self._connecter_sans_verrou()

    async def _connecter_sans_verrou(self) -> None:
        if self.demonstration:  # ni coffre, ni compte, ni nuage : un robot fictif en mémoire
            robot = demo.RobotFictif()
            self.session = Session(robot, demo.APPAREIL, self.journal, self.cal, frozenset(Categorie))
            robot.session = self.session
            self.session.signaler = self._signaler
            self.session.attestation = self.attestation
            self.ident, self.etat_liaison = None, demo.MENTION
            if self._battement is not None:
                self._battement.cancel()
            self._battement = asyncio.create_task(robot.battre())  # l'état ne se grise pas tout seul
            await self.session.solliciter()
            return
        # Identifiants relus dans le coffre à chaque connexion : le jeton stocké a pu tourner.
        try:
            self.ident = liaison.lire_identifiants()
        except auth.ErreurAuth:
            self.ident, self.etat_liaison = None, NON_CONNECTE
            return
        except Exception as erreur:  # noqa: BLE001 — coffre indisponible : la page reste utilisable
            self.ident, self.etat_liaison = None, f"coffre illisible ({type(erreur).__name__})"
            return
        # SEC-05 : en simulation, la session elle-même ne sait émettre que des requêtes pures.
        autorisees = frozenset(Categorie) if self.reel else frozenset({Categorie.REQUETE_PURE})
        try:
            self.session = await self._pile.enter_async_context(
                liaison.ouvrir_session(self.journal, self.cal, autorisees, self.ident, brut=self._brut)
            )
        except Exception as erreur:  # noqa: BLE001
            self.session = None
            self.etat_liaison = f"connexion impossible : {masquer(str(erreur)) or type(erreur).__name__}"
            return
        self.session.signaler = self._signaler
        self.session.attestation = self.attestation  # SEC-01b : la même règle pour tous les chemins d'ordre
        self.session.sur_etat = self._relever  # relevé local de la précision, par position
        self.etat_liaison = "connecté"
        await self.session.solliciter()

    async def _veiller(self) -> None:
        """Liaison perdue hors de tout ordre : le dire en clair et se reconnecter, sans page figée.

        Pendant un ordre, c'est le mouvement lui-même qui traite la coupure (SEC-03)."""
        while True:
            await asyncio.sleep(PERIODE_VEILLE_S)
            session = self.session
            if self.suspendue or self.demonstration or session is None or session.lien.connecte or self.occupe or self.ferme:
                continue
            self.etat_liaison = LIAISON_COUPEE
            self._noter(LIAISON_COUPEE)
            try:
                async with self._verrou_acces:
                    if not self.occupe and self.session is session:
                        await self._rouvrir_liaison()
                        if self.session is not None:
                            self._noter("liaison rétablie")
            except Exception as erreur:  # noqa: BLE001 — la veille ne meurt jamais ; nouvel essai au tour suivant
                self.etat_liaison = f"{LIAISON_COUPEE} — échec ({type(erreur).__name__}), nouvel essai"
            if self.session is None and self.ident is not None:
                # Reconnexion manquée : on réessaie, sans marteler le service.
                await asyncio.sleep(4 * PERIODE_VEILLE_S)
                async with self._verrou_acces:
                    if self.session is None and not self.ferme:
                        await self._connecter_sans_verrou()

    # --- enregistrement de diagnostic pendant un ordre réel --------------------

    def _brut(self, sujet: str, charge: bytes | str, retenu: bool) -> None:
        if self._ecoute is not None:
            try:
                self._ecoute.recevoir(sujet, charge, retenu)
            except Exception:  # noqa: BLE001 — jamais bloquant : le diagnostic ne gêne ni l'écoute ni l'ordre
                self._ecoute = None

    def _ouvrir_ecoute(self) -> None:
        if self._fin_ecoute is not None:
            self._fin_ecoute.cancel()  # nouvel ordre pendant la suite du précédent : même dossier
            self._fin_ecoute = None
        if self._ecoute is None and self.reel:
            session = self.session
            self._ecoute = capture.pour_un_ordre(self.dossier, lambda: bool(session and session.arret_en_cours))

    def _fermer_ecoute(self) -> None:
        if self._fin_ecoute is not None:
            self._fin_ecoute.cancel()
        ecoute, self._ecoute, self._fin_ecoute = self._ecoute, None, None
        if ecoute is not None:
            try:
                ecoute.vider_attente()
                self.journal.ecrire("capture-ordre", messages=ecoute.messages, octets=ecoute.octets)
            except Exception:  # noqa: BLE001
                pass

    # --- garde commune à toutes les routes -----------------------------------

    def _entetes(self) -> dict[str, str]:
        connexions = "'self'"
        if self.ident is not None:  # signalisation vidéo : seul hôte externe jamais joint par la page
            connexions += f" wss://*.kinesisvideo.{self.ident.region}.amazonaws.com"
        return {
            "Content-Security-Policy": (
                f"default-src 'self'; connect-src {connexions}; media-src 'self' blob: mediastream:; "
                "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        }

    @staticmethod
    def _egaux(propose: str | None, attendu: str | None) -> bool:
        if not propose or not attendu or not propose.isascii():
            return False
        return hmac.compare_digest(propose.encode(), attendu.encode())

    @web.middleware
    async def _garde(self, request: web.Request, handler: Any) -> web.StreamResponse:  # noms imposés par aiohttp
        reponse = await self._filtrer(request, handler)
        if not reponse.prepared:
            reponse.headers.update(self._entetes())
        return reponse

    async def _filtrer(self, requete: web.Request, suite: Any) -> web.StreamResponse:
        for nom in ("Host", "Origin", "Cookie"):
            if not requete.headers.get(nom, "").isascii():
                return _refus(400, "en-tête non ASCII refusé")
        # Anti « DNS rebinding » : un nom étranger pointant sur 127.0.0.1 est refusé.
        if requete.headers.get("Host") not in (f"{HOTE}:{self.port}", f"localhost:{self.port}"):
            return _refus(403, "hôte refusé")
        if requete.method not in ("GET", "POST"):
            return _refus(405, "méthode refusée")
        if requete.path in ROUTES_LOCALES:
            # Réservées à un second processus lymow de ce poste : un navigateur joint toujours
            # Origin ou Referer à un POST, donc aucun site web ne peut les déclencher.
            if requete.method != "POST":
                return _refus(405, "méthode refusée")
            if "Origin" in requete.headers or "Referer" in requete.headers:
                return _refus(403, "origine refusée")
            locales = {
                ROUTE_REOUVERTURE: self._rouvrir, ROUTE_IDENTITE: self._identite,
                ROUTE_ARRET: self._arret_demande, rappel.ROUTE: self._rappel,
            }
            return await locales[requete.path](requete)
        origines = (f"http://{HOTE}:{self.port}", f"http://localhost:{self.port}")
        if requete.method == "POST" and requete.headers.get("Origin") not in origines:
            return _refus(403, "origine refusée")
        echange_initial = requete.path == "/" and "jeton" in requete.query
        if not echange_initial and not self._egaux(requete.cookies.get(COOKIE), self.temoin):
            return _refus(403, "jeton absent ou invalide : ouvrez l'adresse affichée par « lymow ui »")
        try:
            return await suite(requete)
        except web.HTTPException as exception:
            return _refus(exception.status, exception.reason or "refusé")
        except ErreurSaisie as erreur:
            return _refus(400, str(erreur))
        except Exception as erreur:  # noqa: BLE001 — jamais de réponse brute sans nos en-têtes
            return _refus(500, f"erreur interne ({type(erreur).__name__})")

    async def _rouvrir(self, requete: web.Request) -> web.StreamResponse:
        """Un second lancement demande de rouvrir la page ; la réponse ne porte ni jeton ni adresse."""
        try:
            corps = await requete.json()
        except (ValueError, RecursionError):
            corps = None
        demande = corps.get("mode") if isinstance(corps, dict) else None
        reponse = {"interface": "lymow", "mode": self.mode, "ok": False}
        if demande != self.mode:
            return web.json_response(reponse, status=409)
        maintenant = self.horloge()
        if self._derniere_reouverture is not None and maintenant - self._derniere_reouverture < DELAI_REOUVERTURE_S:
            return web.json_response(reponse, status=429)
        self._derniere_reouverture = maintenant
        # Le témoin de session ne change pas : un mouvement en cours et sa page restent valides.
        reponse["ok"] = self.ouvrir_page()
        self._noter("page rouverte à la demande d'un second lancement")
        return web.json_response(reponse)

    async def _identite(self, _requete: web.Request) -> web.StreamResponse:
        """Qui tourne ici : mode et empreinte du code, rien d'autre ; aucun effet."""
        return web.json_response({"interface": "lymow", "mode": self.mode, "empreinte": self.empreinte})

    async def _arret_demande(self, requete: web.Request) -> web.StreamResponse:
        """Un lancement d'une AUTRE version demande la place. Refusé pendant un mouvement ou une
        écriture de carte : on ne coupe jamais un ordre en cours pour une mise à jour."""
        try:
            corps = await requete.json()
        except (ValueError, RecursionError):
            corps = None
        reponse = {"interface": "lymow", "mode": self.mode, "ok": False}
        if not isinstance(corps, dict) or corps.get("mode") != self.mode or self.occupe:
            return web.json_response(reponse, status=409)
        self._noter("arrêt demandé par le lancement d'une autre version")
        asyncio.get_running_loop().call_later(0.05, self.fin.set)  # après l'envoi de la réponse
        return web.json_response(reponse | {"ok": True})

    # --- page et état ---------------------------------------------------------

    async def _page(self, requete: web.Request) -> web.StreamResponse:
        if "jeton" in requete.query:
            if not self._egaux(requete.query.get("jeton"), self.jeton):
                return _refus(403, "jeton invalide ou déjà utilisé : relancez « lymow ui »")
            self.jeton = None  # usage unique : une adresse recopiée ou rejouée ne vaut plus rien
            # Le jeton quitte l'adresse aussitôt : redirection, puis témoin inaccessible aux scripts.
            reponse = web.Response(status=303, headers={"Location": "/"})
            reponse.set_cookie(COOKIE, self.temoin, httponly=True, samesite="Strict", path="/")
            return reponse
        self.video_ouverte = False  # page (re)chargée : l'ancienne session vidéo n'existe plus
        return web.Response(text=self.statiques["index.html"], content_type="text/html")

    async def _statique(self, requete: web.Request) -> web.StreamResponse:
        nom = requete.match_info["nom"]
        if nom not in FICHIERS:
            return _refus(404, "fichier inconnu")
        return web.Response(text=self.statiques[nom], content_type=FICHIERS[nom])

    async def _sans_contenu(self, _requete: web.Request) -> web.StreamResponse:
        return web.Response(status=204)

    @property
    def occupe(self) -> bool:
        return self.tache is not None and not self.tache.done()

    def instantane(self, complet: bool = False) -> dict[str, Any]:
        session = self.session
        etat = None
        if session is not None:
            etat = session.etat.resume(session.horloge())
            age = etat["age_etat_s"]
            etat["perime"] = age is None or age > securite.AGE_MAX_ETAT_S
        image: dict[str, Any] = {
            "mode": self.mode,
            "serveur": {"empreinte": self.empreinte, "demarre": self.demarre},
            "connecte": session is not None and bool(session.lien.connecte),
            "liaison": self.etat_liaison,
            "appareil": demo.APPAREIL if self.demonstration else self.ident.appareil if self.ident else None,
            "occupe": self.occupe,
            "ordres": self._etat_des_ordres(),
            "direct": {
                "arme": self.direct.arme, "reste_s": self.direct.reste_s,
                "file": [dataclasses.asdict(ordre) for ordre in self.direct.file], "faites": self.direct.faites[-12:],
            },
            "attestation": self._etat_attestation(),
            "compte": self._compte(),
            "etat": etat,
            "resultat": self.resultat,
            "journal": self.fil[-100:],
            "liaison_suspendue": self.suspendue,
        }
        if complet:
            image |= {
                "bornes": _bornes() | {
                    "precision_fiable_m": securite.PRECISION_FIABLE_M, "precision_degradee_m": securite.PRECISION_DEGRADEE_M,
                },
                "libelles": conduite.LIBELLES,
                "fleches": {
                    "distances_m": list(fleches.DISTANCES_M), "angles_deg": list(fleches.ANGLES_DEG),
                    "distance_defaut_m": fleches.DISTANCE_DEFAUT_M, "angle_defaut_deg": fleches.ANGLE_DEFAUT_DEG,
                    "file_max": fleches.FILE_MAX, "armement_s": fleches.DUREE_ARMEMENT_S,
                },
                "regions": list(auth.REGIONS), "region_defaut": auth.REGION_DEFAUT,
            }
        return image

    def _etat_des_ordres(self) -> dict[str, Any]:
        """Statut du robot en clair, et ce qui empêcherait un mouvement, dit AVANT la prévisualisation."""
        session = self.session
        if session is None or not session.lien.connecte:
            return {"statut": None, "mouvement_refuse": f"robot injoignable ({self.etat_liaison})"}
        etat = session.etat
        libelle = etat.resume(session.horloge())["statut_libelle"]
        refus, action = None, None
        if etat.en_ligne is False:
            refus = "Le robot est hors ligne : attendez son retour en ligne."
        elif etat.statut is not None and not securite.statut_admis(etat.statut, self.attestation.active):
            constat, conseil = securite.explication_statut(etat.statut)
            refus = constat[0].upper() + constat[1:] + (f" : {conseil}." if conseil else " : mouvement refusé.")
            action = "pause" if etat.statut in securite.STATUTS_PAUSE_UTILE else None
        pause = self._etat_de_la_pause(etat, session.horloge())
        if pause and pause["etat"] == "en attente":
            refus, action = "Pause demandée : attente de la confirmation du robot.", None
        return {
            "statut": libelle, "mouvement_refuse": refus, "action": action, "pause": pause,
            "tache": etat.resume(session.horloge())["tache_libelle"],  # dite seulement si elle diffère du statut
            # Après un dégagement, la reprise reste proposée clairement (les lames peuvent redémarrer).
            "reprise_possible": etat.statut == socle.WORK_STATUS_PAUSE,
            # SEC-12 : dit AVANT de reprendre ou de renvoyer à la base ; rien si la position est fiable.
            "avertissement_position": securite.avertissement_position(etat.precision_m),
        }

    def _etat_de_la_pause(self, etat: Any, maintenant: float) -> dict[str, str] | None:
        """Suite d'une pause demandée : en attente, confirmée par un statut FRAIS, ou non confirmée."""
        if self._pause is None:
            return None
        instant, echeance, attendu = self._pause
        frais = etat.recu_statut is not None and etat.recu_statut > instant
        if frais and etat.statut == attendu:
            if attendu == socle.WORK_STATUS_PAUSE:
                return {"etat": "confirmée", "texte": "Pause confirmée : flèches disponibles."}
            return {"etat": "confirmée", "texte": "Retour en pause confirmé : aucun mouvement n'est permis dans ce statut."}
        if self.occupe or maintenant <= echeance:
            return {"etat": "en attente", "texte": "Pause demandée : attente de la confirmation du robot…"}
        libelle = etat.resume(maintenant)["statut_libelle"]
        return {"etat": "non confirmée", "texte": f"Pause non confirmée : le robot annonce « {libelle} »."}

    def _etat_attestation(self) -> dict[str, Any]:
        """SEC-01b : active ou non, temps restant, et si la page peut la proposer (statut attestable seulement)."""
        session = self.session
        active = self.attestation.active
        libelle = None
        if self.emet and not active and session is not None and session.lien.connecte:
            etat = session.etat
            if etat.en_ligne is True and etat.statut in securite.STATUTS_ATTESTABLES:
                libelle = etat.resume(session.horloge())["statut_libelle"]
        return {
            "active": active, "reste_s": self.attestation.reste_s, "duree_s": securite.DUREE_ATTESTATION_S,
            "proposable": libelle is not None, "statut": libelle,
        }

    def _tracer_attestation(self, evenement: str, **champs: Any) -> None:
        """Chaque pose, retrait ou expiration : au journal (différé pendant un arrêt, SEC-03) et au fil de la page."""
        session = self.session
        (session.journaliser if session is not None else self.journal.ecrire)(evenement, mode=self.mode, **champs)
        textes = {
            "attestation-posee": "PILOTAGE AUTORISÉ SUR ATTESTATION DE L'OPÉRATEUR (lames à l'arrêt constatées), 10 min au plus",
            "attestation-retiree": f"attestation de l'opérateur retirée : {champs.get('motif')}",
            "attestation-expiree": "attestation de l'opérateur expirée",
        }
        self._noter(textes[evenement])

    async def _etat(self, _requete: web.Request) -> web.StreamResponse:
        return web.json_response(self.instantane(complet=True))

    async def _flux(self, requete: web.Request) -> web.StreamResponse:
        """État poussé à la page (Server-Sent Events) : à chaque changement, et chaque seconde."""
        reponse = web.StreamResponse(headers={"Content-Type": "text/event-stream"} | self._entetes())
        await reponse.prepare(requete)
        vue, dernier = -1, 0.0
        try:
            while not self.ferme:
                if self.version != vue or time.monotonic() - dernier >= 1.0:
                    vue, dernier = self.version, time.monotonic()
                    donnees = json.dumps(self.instantane(), ensure_ascii=False)
                    await reponse.write(f"data: {donnees}\n\n".encode())
                await asyncio.sleep(0.2)
        except (ConnectionError, RuntimeError):
            pass  # page fermée : rien à faire, le mouvement éventuel vit dans sa propre tâche
        return reponse

    def _noter(self, texte: str) -> None:
        self.fil.append({"heure": datetime.now().strftime("%H:%M:%S"), "texte": masquer(texte)})
        del self.fil[:-200]
        self.version += 1

    def _signaler(self, texte: str) -> None:
        print(masquer(texte), file=sys.stderr, flush=True)
        self._noter(texte)

    async def _actualiser(self, _requete: web.Request) -> web.StreamResponse:
        if self.occupe:
            return _refus(409, "un ordre est en cours")
        if self.suspendue:
            return _refus(409, f"{LIAISON_SUSPENDUE} — cliquez sur « Reprendre la liaison »")
        async with self._verrou_acces:  # deux actualisations ne rouvrent jamais deux liaisons
            if self.session is None or not self.session.lien.connecte:
                await self._rouvrir_liaison()
            else:
                await self.session.solliciter()
        self._noter(f"actualisation : {self.etat_liaison}")
        return web.json_response(self.instantane())

    # --- ordres ---------------------------------------------------------------

    async def _corps(self, requete: web.Request) -> Any:
        try:
            return await requete.json()
        except (ValueError, RecursionError):
            raise ErreurSaisie("corps JSON attendu") from None

    async def _previsualiser(self, requete: web.Request) -> web.StreamResponse:
        ordres = ordres_depuis(await self._corps(requete))
        bilan, code = conduite.simuler_ordres(ordres, self.cal, self.journal, self._appareil_fictif())
        identifiant = None
        self.previsualisation = None  # une nouvelle prévisualisation annule toujours la précédente
        if self.emet and code == 0:
            identifiant = secrets.token_urlsafe(16)
            self.previsualisation = (identifiant, ordres, self.horloge() + DUREE_PREVISUALISATION_S)
        texte = " ; ".join(str(ordre) for ordre in ordres)
        self._noter(f"prévisualisation : {texte}" + ("" if code == 0 else f" — refusée : {bilan.get('motif')}"))
        # `etapes` : ce que CE serveur émettra, étape par étape — la page l'affiche tel quel, numéroté.
        return web.json_response({
            "bilan": bilan, "identifiant": identifiant, "ordres": texte, "etapes": [str(ordre) for ordre in ordres],
        })

    async def _emettre(self, requete: web.Request) -> web.StreamResponse:
        # SEC-05 : serveur lancé sans --reel, aucun ordre réel, quoi que demande la page.
        if not self.emet:
            return _refus(403, "serveur lancé en simulation : aucun ordre réel n'est accepté (SEC-05)")
        corps = await self._corps(requete)
        propose = corps.get("identifiant") if isinstance(corps, dict) else None
        attendue, self.previsualisation = self.previsualisation, None  # usage unique, même en cas de refus
        if (
            attendue is None
            or not isinstance(propose, str)
            or not hmac.compare_digest(propose.encode(), attendue[0].encode())
        ):
            return _refus(409, "aucune prévisualisation valide : prévisualisez de nouveau")
        if self.horloge() > attendue[2]:
            return _refus(409, "prévisualisation expirée : prévisualisez de nouveau")
        if self.session is None or not self.session.lien.connecte:
            return _refus(503, f"robot injoignable : {self.etat_liaison}")
        if self.occupe or self._stop_demande:
            return _refus(409, "un ordre ou un STOP est déjà en cours : seul STOP est accepté")
        if isinstance(attendue[1], retouche.Retouche):
            operation = attendue[1]  # SEC-08 : aucun autre ordre en cours, vérifié juste au-dessus
            self._lancer_travail(
                operation.description,
                lambda: retouche.emettre(self.session, operation, self.dossier, demonstration=self.demonstration),
            )
        else:
            self._lancer(attendue[1])
        return web.json_response({"accepte": True}, status=202)

    def _lancer(self, ordres: list[Ordre]) -> None:
        self._lancer_travail(
            " ; ".join(str(ordre) for ordre in ordres),
            lambda: conduite.emettre_ordres(self.session, ordres, self.journal),
        )
        if ordres == [Ordre("pause")]:  # la page attendra le statut « pause » frais avant de rendre les flèches
            instant = self.session.horloge()
            retour = self.session.etat.statut == socle.WORK_STATUS_DOCKING
            attendu = socle.WORK_STATUS_PAUSE_DOCKING if retour else socle.WORK_STATUS_PAUSE
            self._pause = (instant, instant + DELAI_PAUSE_S, attendu)

    def _lancer_travail(self, texte: str, travail: Any) -> None:
        # Jamais deux tâches : l'appelant a vérifié, ceci interdit toute tâche orpheline.
        if self.occupe:
            raise web.HTTPConflict(reason="un ordre est déjà en cours")
        self._noter(f"émission : {texte}")
        self._pause = None
        self._ouvrir_ecoute()
        self.tache = asyncio.create_task(self._conduire(travail))

    async def _conduire(self, travail: Any) -> None:
        """Le mouvement vit ici : aucune requête de la page n'est nécessaire pour qu'il s'arrête."""
        try:
            bilan, code = await travail()
            if self.demonstration:  # « émis » vers le robot fictif : ne jamais afficher « réel »
                bilan = _en_demonstration(bilan)
            self.resultat = assainir(bilan | {"code_sortie": code})  # filtré comme le journal
            if code == conduite.CODE_ARRET_NON_CONFIRME:
                self._signaler("ARRÊT NON CONFIRMÉ : arrêtez le robot par son bouton STOP ou par l'application.")
            suite = bilan.get("motif") or bilan.get("verdict") or "voir le résultat"
            self._noter(f"ordre terminé : {bilan['verdict']}" if code == 0 and "verdict" in bilan else
                        "ordre terminé" if code == 0 else f"ordre non abouti : {suite}")
        except asyncio.CancelledError:
            self.resultat = {"echec": "ordre interrompu ; l'arrêt a été émis", "code_sortie": 130}
            self._noter("ordre interrompu : arrêt émis")
            if not self._stop_demande:
                self.fin.set()  # Ctrl+C pendant un mouvement : robot arrêté, puis serveur arrêté
        except Exception as erreur:  # noqa: BLE001 — une tâche de fond ne doit jamais mourir en silence
            self.resultat = {"echec": masquer(str(erreur)) or type(erreur).__name__, "code_sortie": 1}
            self._noter(f"ordre en échec : {self.resultat['echec']}")
        finally:
            self.version += 1
            if self._ecoute is not None and not self.ferme:
                self._fin_ecoute = asyncio.get_running_loop().call_later(SUITE_CAPTURE_S, self._fermer_ecoute)

    async def _stop(self, _requete: web.Request) -> web.StreamResponse:
        """STOP : sans prévisualisation, prioritaire sur tout ordre en cours."""
        async with self._verrou_stop:
            if not self.emet:
                self._desarmer("STOP")
                bilan, _code = conduite.simuler_ordres([Ordre("stop")], self.cal, self.journal)
                self._noter("STOP (simulation)")
                return web.json_response({"bilan": bilan})
            if self.session is None:
                return _refus(503, f"robot injoignable : {self.etat_liaison}")
            self._noter("STOP demandé")
            self._desarmer("STOP")  # la file du pilotage direct est vidée avant tout
            self.previsualisation = None
            self._stop_demande = True
            try:
                await self._interrompre()  # l'arrêt de fin de mouvement part d'abord
                self.attestation.retirer("STOP")  # SEC-01b : après l'arrêt, rien ne le retarde
                self._lancer([Ordre("stop")])  # puis le `stop` de la commande : consignes nulles ×3, pause
            finally:
                self._stop_demande = False
            return web.json_response({"accepte": True}, status=202)

    async def _pause_raccourci(self, requete: web.Request) -> web.StreamResponse:
        """« Mettre en pause » : UNE confirmation de la page, puis le seul ordre « pause » — rien d'autre,
        et jamais de mouvement à sa suite : c'est le propriétaire qui rappuie."""
        corps = await self._corps(requete)
        if not isinstance(corps, dict) or corps.get("confirmer") is not True or set(corps) != {"confirmer"}:
            raise ErreurSaisie("confirmation explicite attendue pour mettre en pause")
        if not self.emet:  # SEC-05
            bilan, _code = conduite.simuler_ordres([Ordre("pause")], self.cal, self.journal, self._appareil_fictif())
            self._noter("pause (simulation)")
            return web.json_response({"accepte": True, "simulation": True, "bilan": bilan})
        if self.session is None or not self.session.lien.connecte:
            return _refus(503, f"robot injoignable : {self.etat_liaison}")
        if self.occupe or self._stop_demande:
            return _refus(409, "un ordre ou un STOP est en cours : la pause attendra sa fin (STOP met aussi en pause)")
        self.direct.file.clear()
        self.previsualisation = None
        self._lancer([Ordre("pause")])
        return web.json_response({"accepte": True}, status=202)

    # --- attestation « lames à l'arrêt » de l'opérateur (SEC-01b) -------------
    # Ces routes ne font que poser ou retirer l'attestation : aucun ordre n'en part, et la règle
    # elle-même vit dans `securite` — flèches, pilotage direct et ordres classiques la lisent pareil.

    async def _attestation_poser(self, requete: web.Request) -> web.StreamResponse:
        if not self.emet:
            return _refus(403, "serveur lancé en simulation : rien n'est émis, aucune attestation n'y a de sens (SEC-01b)")
        corps = await self._corps(requete)
        if not isinstance(corps, dict) or corps.get("confirmer") is not True:
            raise ErreurSaisie("confirmation explicite attendue : vous attestez que les lames sont à l'arrêt")
        if self.occupe or self._stop_demande:
            return _refus(409, "un ordre ou un STOP est en cours : attestez ensuite, robot à l'arrêt")
        if self.session is None or not self.session.lien.connecte:
            return _refus(503, f"robot injoignable : {self.etat_liaison}")
        if self.attestation.active:
            return _refus(409, "une attestation est déjà active : retirez-la, ou attendez son expiration")
        if not self._etat_attestation()["proposable"]:
            return _refus(
                409,
                "attestation refusée : elle ne vaut que si le robot se déclare en tonte ou en retour ; jamais en "
                "arrêt d'urgence, mise à jour, essai d'usine, hors ligne ou sans état connu (SEC-01b)",
            )
        self.session.attestation = self.attestation
        self.attestation.poser("page")
        return web.json_response({"attestation": self._etat_attestation()})

    async def _attestation_retirer(self, _requete: web.Request) -> web.StreamResponse:
        self.attestation.retirer("à la demande de l'opérateur")  # pendant un mouvement : SEC-01 l'arrête
        return web.json_response({"attestation": self._etat_attestation()})

    # --- pilotage direct par flèches ------------------------------------------
    # Seul chemin sans prévisualisation. Il n'accepte que les quatre ordres bornés de `fleches`,
    # armement préalable, et chaque ordre passe par `conduite.emettre_ordres` comme tout autre :
    # SEC-01 (statut, état frais), SEC-02 (plafonds), SEC-03 (arrêt garanti), un mouvement à la fois.

    def _desarmer(self, motif: str) -> None:
        if self.direct.arme or self.direct.file:
            self.direct.desarmer()
            self.journal.ecrire("direct-desarme", motif=motif)
            self._noter(f"pilotage direct désarmé : {motif}")

    async def _direct_armer(self, requete: web.Request) -> web.StreamResponse:
        corps = await self._corps(requete)
        if not isinstance(corps, dict) or corps.get("confirmer") is not True:
            raise ErreurSaisie("confirmation explicite attendue pour armer le pilotage direct")
        if self.occupe or self._stop_demande:
            return _refus(409, "un ordre est en cours : armez le pilotage direct ensuite")
        if self.emet and (self.session is None or not self.session.lien.connecte):
            return _refus(503, f"robot injoignable : {self.etat_liaison}")
        jeton = self.direct.armer()
        self.journal.ecrire("direct-arme", mode=self.mode)
        self._noter("PILOTAGE DIRECT ARMÉ : chaque flèche émet son étape sans prévisualisation")
        return web.json_response({"arme": True, "jeton": jeton, "valable_s": fleches.DUREE_ARMEMENT_S})

    async def _direct_desarmer(self, _requete: web.Request) -> web.StreamResponse:
        self._desarmer("à la demande de la page")
        return web.json_response({"arme": False})

    async def _direct_fleche(self, requete: web.Request) -> web.StreamResponse:
        corps = await self._corps(requete)
        corps = corps if isinstance(corps, dict) else {}
        try:
            self.direct.verifier(corps.get("jeton"))
        except fleches.RefusDirect as refus:
            self._desarmer(str(refus))
            return _refus(409, str(refus))
        try:
            if set(corps) - {"jeton", "direction", "pas"}:
                raise ErreurSaisie("champ inattendu : le pilotage direct n'accepte qu'une flèche et son pas")
            ordre = fleches.ordre(corps.get("direction"), corps.get("pas"))
        except ErreurSaisie as erreur:
            self._desarmer(f"demande refusée ({erreur})")
            return _refus(400, str(erreur))
        if not self.emet:  # SEC-05 : simulation — même parcours, rien n'est émis
            bilan, _code = conduite.simuler_ordres([ordre], self.cal, self.journal, self._appareil_fictif())
            self.direct.faites.append({
                "ordre": dataclasses.asdict(ordre), "rapport": {"ordre": str(ordre), "simulee": True},
            })
            self.version += 1
            return web.json_response({"accepte": True, "simulation": True, "jeton": self.direct.renouveler(), "bilan": bilan})
        if self.session is None or not self.session.lien.connecte:
            self._desarmer("robot injoignable")
            return _refus(503, f"robot injoignable : {self.etat_liaison}")
        if self._stop_demande or (self.occupe and not self._direct_en_cours):
            self._desarmer("un autre ordre ou un STOP est en cours")
            return _refus(409, "un autre ordre ou un STOP est en cours : pilotage direct désarmé")
        if self.occupe and len(self.direct.file) >= fleches.FILE_MAX:
            self.direct.prolonger()  # appui ignoré, jeton inchangé : la page reste armée
            return _refus(429, f"file d'attente pleine ({fleches.FILE_MAX}) : appui ignoré")
        jeton = self.direct.renouveler()
        if self.occupe:  # un pas est en mouvement : celui-ci attend son tour, cinq au plus
            self.direct.file.append(ordre)
        else:
            self.direct.file.append(ordre)
            self._direct_en_cours = True
            try:
                self._lancer_travail("pilotage direct", self._derouler_file)
            except BaseException:
                self._direct_en_cours = False
                raise
        self.version += 1
        return web.json_response(
            {"accepte": True, "jeton": jeton, "file": [str(o) for o in self.direct.file]}, status=202
        )

    async def _derouler_file(self) -> tuple[dict[str, Any], int]:
        """Émet la file, un pas après l'autre ; le premier refus, échec ou constat contraire l'arrête et désarme."""
        etapes: list[dict[str, Any]] = []
        motif: str | None = "pilotage direct interrompu"
        code = 0
        try:
            while self.direct.file:
                ordre = self.direct.file.pop(0)
                self.version += 1
                bilan, code = await conduite.emettre_ordres(self.session, [ordre], self.journal)
                rapport = bilan["etapes"][0] if bilan["etapes"] else {"ordre": str(ordre), "echec": bilan.get("motif")}
                etapes.append(rapport)
                self.direct.faites.append({"ordre": dataclasses.asdict(ordre), "rapport": assainir(rapport)})
                del self.direct.faites[:-20]
                self.version += 1
                contraire = rapport.get("verification") in (mouvement.ANOMALIE, mouvement.AUCUN_ETAT)
                if code != 0 or contraire:
                    motif = bilan.get("motif") or rapport.get("echec") or rapport.get("constat") or "échec"
                    code = code or 1
                    break
                self.direct.prolonger()
            else:
                motif = None
        finally:
            self._direct_en_cours = False
            if motif is not None:  # refus, échec, constat contraire, ou annulation (STOP, fermeture)
                self._desarmer(motif)
        bilan = {"mode": "réel", "etapes": etapes, "interrompue": motif is not None}
        return bilan | ({"motif": f"pilotage direct arrêté : {motif}"} if motif else {}), code

    # --- carte : lecture, puis retouche par le même chemin que la commande -----

    def _carte_json(self) -> dict[str, Any]:
        carte = cartes.en_memoire(self.session) if self.session is not None else None
        if carte is None:
            return {"carte": None}
        return {
            "carte": cartes.vers_json(carte) | {"liste": self._liste_zones(carte)}, "heure": self.carte_heure,
            "age_s": round(self.session.horloge() - carte.recue, 1),
        }

    def _liste_zones(self, carte: cartes.Carte) -> list[dict[str, Any]]:
        """Zones pour la liste déroulante, triées, avec la possibilité de retouche de chacune."""
        if self._liste[0] != carte.octets:  # le diagnostic des contours coûte : une fois par carte
            self._liste = (carte.octets, liste_zones(carte))
        return self._liste[1]

    async def _carte(self, _requete: web.Request) -> web.StreamResponse:
        return web.json_response(self._carte_json())

    async def _carte_actualiser(self, _requete: web.Request) -> web.StreamResponse:
        """Demande de carte : une requête pure, permise en simulation dès qu'un compte est connecté."""
        self._refuser_si_occupe()
        if self.session is None or not self.session.lien.connecte:
            return _refus(503, f"robot injoignable : {self.etat_liaison}")
        try:
            await cartes.demander(self.session, securite.ATTENTE_CARTE_S)
        except cartes.CarteIllisible as erreur:  # reçue, mais refusée : le motif exact, tout de suite
            self._noter(str(erreur))
            return _refus(422, masquer(str(erreur)))
        except Exception as erreur:  # noqa: BLE001
            return _refus(504, masquer(str(erreur)) or type(erreur).__name__)
        self.carte_heure = datetime.now().strftime("%H:%M:%S")
        self._noter("carte reçue")
        return web.json_response(self._carte_json())

    async def _carte_previsualiser(self, requete: web.Request) -> web.StreamResponse:
        corps = await self._corps(requete)
        if not isinstance(corps, dict) or not isinstance(corps.get("zone"), str):
            raise ErreurSaisie("zone attendue")
        if corps.get("cote") not in ("court", "long", "tout"):
            raise ErreurSaisie("côté attendu : court, long ou tout")
        de, a, cm = _nombre(corps.get("de"), entier=True), _nombre(corps.get("a"), entier=True), _nombre(corps.get("cm"))
        if de is None or a is None or cm is None:
            raise ErreurSaisie("sommets de début et de fin, et largeur en cm, attendus")
        carte = cartes.en_memoire(self.session) if self.session is not None else None
        if carte is None:
            return _refus(409, "aucune carte en mémoire : actualisez d'abord la carte")
        self.previsualisation = None  # une nouvelle prévisualisation annule toujours la précédente
        try:
            operation = retouche.preparer_ajustement(
                carte, corps["zone"], int(de), int(a), cm, corps["cote"], _nombre(corps.get("raccord"))
            )
            retouche.controler_cumul(
                self.dossier, self.session.appareil, operation, demonstration=self.demonstration
            )
        except RefusSecurite as refus:
            self._noter(f"retouche refusée : {refus}")
            return web.json_response({"refus": str(refus), "identifiant": None})
        except cartes.CarteIllisible as erreur:
            raise ErreurSaisie(str(erreur)) from None
        identifiant = None
        if self.emet:  # SEC-08 : pas d'identifiant d'émission en simulation
            identifiant = secrets.token_urlsafe(16)
            self.previsualisation = (identifiant, operation, self.horloge() + DUREE_PREVISUALISATION_S)
        self.journal.ecrire("carte-simulee", ordre=operation.description, resume=operation.resume)
        self._noter(f"prévisualisation : {operation.description}")
        return web.json_response({
            "resume": operation.resume, "ordres": operation.description, "identifiant": identifiant,
            "avant": [list(p) for p in operation.avant], "apres": [list(p) for p in operation.apres],
        })

    # --- compte : mêmes fonctions que `lymow connexion`, Google seulement ------

    def _compte(self) -> dict[str, Any]:
        """Jamais de jeton ici : seulement ce que la page a le droit d'afficher."""
        ident = self.ident
        return {
            "connecte": ident is not None,
            "region": ident.region if ident else None,
            "appareil": ident.appareil if ident else None,
            "tentative": self.tentative is not None and self.horloge() <= self.tentative[1],
            "choix": self.choix["appareils"] if self.choix else None,
            "un_clic": self.un_clic,
        }

    def _appareil_fictif(self) -> str | None:
        return demo.APPAREIL if self.demonstration else None

    def _refuser_en_demonstration(self) -> None:
        if self.demonstration:
            raise web.HTTPForbidden(reason="indisponible en démonstration : aucun compte, aucun robot réel")

    def _refuser_si_occupe(self) -> None:
        if self.occupe or self._stop_demande:
            raise web.HTTPConflict(reason="un ordre est en cours : attendez sa fin ou faites STOP")

    def _connexion_refusee(self, motif: str) -> web.Response:
        motif = masquer(motif)
        self._noter(f"connexion refusée : {motif}")
        return _refus(400, f"connexion refusée : {motif}")

    async def _compte_commencer(self, requete: web.Request) -> web.StreamResponse:
        self._refuser_en_demonstration()
        self._refuser_si_occupe()
        corps = await self._corps(requete)
        region = corps.get("region") if isinstance(corps, dict) else None
        if region not in auth.REGIONS:
            raise ErreurSaisie("région inconnue")
        demande = auth.preparer_connexion(region)
        # Une seule tentative active : la nouvelle rend caduque la précédente.
        self.tentative, self.choix = (demande, self.horloge() + DUREE_TENTATIVE_S), None
        self._noter("connexion : tentative ouverte")  # l'adresse d'autorisation n'est pas journalisée
        return web.json_response({"url": demande.url, "expire_s": DUREE_TENTATIVE_S})

    async def _compte_valider(self, requete: web.Request) -> web.StreamResponse:
        self._refuser_en_demonstration()
        self._refuser_si_occupe()
        corps = await self._corps(requete)
        adresse = corps.get("adresse") if isinstance(corps, dict) else None
        return await self._terminer_connexion(adresse)

    async def _terminer_connexion(self, adresse: Any) -> web.Response:
        """Fin de connexion, que l'adresse ait été collée dans la page ou remise par `lymow rappel`."""
        if isinstance(adresse, str):
            declarer_secret(adresse.strip())  # avant tout : aucun refus ne doit la faire fuiter
        tentative, self.tentative = self.tentative, None  # usage unique, même en cas de refus
        if tentative is None:
            return self._connexion_refusee("aucune tentative en cours : cliquez de nouveau sur « Se connecter avec Google »")
        if self.horloge() > tentative[1]:
            return self._connexion_refusee("tentative expirée : recommencez")
        if not isinstance(adresse, str) or len(adresse) > 4096:
            return self._connexion_refusee("adresse de rappel attendue")
        demande = tentative[0]
        try:
            async with self._verrou_acces, self.ouvrir_http() as http:
                jetons = await auth.echanger_code(http, demande, adresse)  # vérifie `state`, comme la commande
                jeton = compte.jeton_durable(jetons)
                appareils = await compte.appareils_du_compte(http, jetons, demande.region)
        except auth.ErreurAuth as erreur:
            return self._connexion_refusee(str(erreur))  # messages écrits par nous, sans secret
        except Exception:  # noqa: BLE001 — jamais le texte d'origine : il peut citer une adresse
            return self._connexion_refusee("échange refusé par le service (code expiré ou déjà utilisé ?) : recommencez")
        if len(appareils) > 1:
            self.choix = {
                "jeton": jeton, "region": demande.region, "appareils": appareils,
                "expire": self.horloge() + DUREE_TENTATIVE_S,
            }
            self._noter("connexion : plusieurs appareils, choix demandé")
            return web.json_response({"appareils": appareils})
        return await self._compte_enregistrer(jeton, demande.region, appareils[0]["appareil"])

    async def _rappel(self, requete: web.Request) -> web.StreamResponse:
        """Adresse de rappel remise par `lymow rappel` (gestionnaire du protocole myapp).

        Acceptée seulement si une tentative est active et que son `state` correspond ;
        un `state` étranger ne consomme pas la tentative. La réponse ne dit que « ok » ou non.
        """
        reponse: dict[str, Any] = {"interface": "lymow", "ok": False}
        if self.demonstration:
            return web.json_response(reponse, status=409)
        maintenant = self.horloge()
        try:
            corps = await requete.json()
        except (ValueError, RecursionError):
            corps = None
        adresse = corps.get("adresse") if isinstance(corps, dict) else None
        if not isinstance(adresse, str) or not rappel.ADRESSE.fullmatch(adresse):
            return web.json_response(reponse, status=400)
        declarer_secret(adresse)
        tentative = self.tentative
        if tentative is None or maintenant > tentative[1] or self.occupe or self._stop_demande:
            return web.json_response(reponse, status=409)
        etat_recu = parse_qs(urlparse(adresse).query).get("state", [""])[0]
        if not self._egaux(etat_recu, tentative[0].etat):  # comparaison en temps constant
            # La fréquence n'est limitée qu'APRÈS un `state` refusé : une adresse fausse ne doit
            # jamais faire perdre le vrai rappel, qui passe sans attendre.
            if self._dernier_rappel is not None and maintenant - self._dernier_rappel < DELAI_RAPPEL_S:
                return web.json_response(reponse, status=429)
            self._dernier_rappel = maintenant
            self._noter("connexion en un clic : adresse refusée")
            return web.json_response(reponse, status=403)
        resultat = await self._terminer_connexion(adresse)
        reponse["ok"] = resultat.status == 200
        if not reponse["ok"]:
            reponse["motif"] = "connexion-refusee"
        return web.json_response(reponse, status=200 if reponse["ok"] else 400)

    async def _compte_un_clic(self, requete: web.Request) -> web.StreamResponse:
        """Active ou retire le gestionnaire `myapp` : mêmes fonctions que `lymow rappel-installer`."""
        self._refuser_en_demonstration()
        corps = await self._corps(requete)
        activer = corps.get("activer") if isinstance(corps, dict) else None
        if not isinstance(activer, bool):
            raise ErreurSaisie("« activer » attendu : vrai ou faux")
        try:
            message = rappel.installer(self.registre) if activer else rappel.desinstaller(self.registre)
        except rappel.EchecRappel as erreur:
            return _refus(409, str(erreur))
        finally:
            self.un_clic = rappel.etat(self.registre)
        self.journal.ecrire("un-clic", actif=self.un_clic["actif"])
        self._noter(message)
        return web.json_response({"un_clic": self.un_clic, "message": message})

    async def _compte_choisir(self, requete: web.Request) -> web.StreamResponse:
        self._refuser_en_demonstration()
        self._refuser_si_occupe()
        corps = await self._corps(requete)
        appareil = corps.get("appareil") if isinstance(corps, dict) else None
        choix = self.choix
        if choix is None or self.horloge() > choix["expire"]:
            self.choix = None
            return self._connexion_refusee("aucun choix d'appareil en attente : recommencez")
        if appareil not in [propose["appareil"] for propose in choix["appareils"]]:
            raise ErreurSaisie("appareil hors de la liste proposée")
        self.choix = None
        return await self._compte_enregistrer(choix["jeton"], choix["region"], appareil)

    # --- rendre la main à l'application officielle ------------------------------

    async def _liaison_rendre(self, requete: web.Request) -> web.StreamResponse:
        """Ferme la liaison et suspend la reconnexion automatique ; n'émet RIEN vers le robot."""
        corps = await self._corps(requete)
        corps = corps if isinstance(corps, dict) else {}
        if self.occupe or self._stop_demande:
            return _refus(409, "un mouvement est en cours : attendez sa fin (ou STOP), puis rendez la main")
        session = self.session
        en_telecommande = session is not None and session.etat.statut == socle.WORK_STATUS_REMOTE_CONTROL
        if en_telecommande and corps.get("sans_attendre") is not True:
            # Rien n'est émis pour l'en sortir : le robot revient seul en pause après une trentaine de secondes.
            return web.json_response({
                "suspendue": False, "attendre": True,
                "message": "Le robot est encore en télécommande : il revient seul en pause après une trentaine de "
                           "secondes. Attendez, ou rendez la main sans attendre.",
            })
        self.attestation.retirer("main rendue à l'application")
        self._desarmer("main rendue à l'application")
        self.previsualisation = None
        async with self._verrou_acces:
            self.suspendue = True
            await self._pile.aclose()
            self._pile, self.session = AsyncExitStack(), None
            self.etat_liaison = LIAISON_SUSPENDUE
        self.journal.ecrire("liaison-suspendue", mode=self.mode)
        self._noter(LIAISON_SUSPENDUE)
        return web.json_response({"suspendue": True, "attendre": False, "message": LIAISON_SUSPENDUE})

    async def _liaison_reprendre(self, _requete: web.Request) -> web.StreamResponse:
        async with self._verrou_acces:
            if self.suspendue:
                self.suspendue = False
                self.journal.ecrire("liaison-reprise", mode=self.mode)
                await self._rouvrir_liaison()
                self._noter(f"liaison reprise : {self.etat_liaison}")
        return web.json_response({"suspendue": self.suspendue, "liaison": self.etat_liaison})

    # --- relevé local de la précision de position --------------------------------

    def _relever(self, etat: Any, instant: float) -> None:
        pose, precision = etat.pose, etat.precision_m
        if pose is not None and precision is not None and etat.recu_pose == instant and not self.demonstration:
            self._releve.noter(pose.est_m, pose.nord_m, precision, etat.donnees.get("rtkSatellites"), instant)

    async def _releve_lire(self, _requete: web.Request) -> web.StreamResponse:
        points = [
            [p["est_m"], p["nord_m"], p["precision_m"], securite.qualite_position(float(p["precision_m"]))]
            for p in releve.lire(self.dossier)
        ]
        return web.json_response({"points": points})

    async def _rouvrir_liaison(self) -> None:
        # SEC-01b : nouvelle liaison, état du robot à relire — l'attestation ne lui survit pas.
        self.attestation.retirer("liaison rouverte")
        await self._pile.aclose()
        self._pile, self.session = AsyncExitStack(), None
        await self._connecter_sans_verrou()

    async def _compte_enregistrer(self, jeton: str, region: str, appareil: str) -> web.StreamResponse:
        async with self._verrou_acces:
            compte.enregistrer(jeton, region, appareil, auth.METHODE_GOOGLE)
            await self._rouvrir_liaison()  # mêmes règles qu'au démarrage : session bridée en simulation
        self.journal.ecrire("compte-enregistre", region=region)
        self._noter("connexion réussie")
        return web.json_response({"connecte": self.ident is not None, "liaison": self.etat_liaison})

    async def _compte_deconnecter(self, _requete: web.Request) -> web.StreamResponse:
        self._refuser_en_demonstration()
        self._refuser_si_occupe()
        async with self._verrou_acces:
            self.attestation.retirer("compte effacé de ce poste")
            await self._pile.aclose()
            self._pile, self.session = AsyncExitStack(), None
            compte.oublier()
            self.ident, self.etat_liaison = None, NON_CONNECTE
        self.tentative = self.choix = None
        self.video_ouverte = False
        self.journal.ecrire("compte-oublie")
        self._noter("compte effacé de ce poste")
        return web.json_response({"connecte": False})

    # --- vidéo ----------------------------------------------------------------

    async def _video(self) -> dict[str, Any]:
        async with self._verrou_acces, self.ouvrir_http() as http:
            # Relu à chaque fois : `obtenir_acces` fait tourner le jeton dans le coffre, et
            # présenter l'ancien pourrait révoquer la connexion du compte.
            self.ident = liaison.lire_identifiants()
            acces = await liaison.obtenir_acces(http, self.ident)
            return await liaison.demarrer_video(http, self.ident, acces)

    async def _video_demarrer(self, _requete: web.Request) -> web.StreamResponse:
        if self.demonstration:
            return _refus(403, "vidéo désactivée en démonstration : le robot fictif n'a pas de caméra")
        if self.ident is None:
            return _refus(503, NON_CONNECTE)
        if self.video_ouverte:
            return _refus(409, "une session vidéo est déjà ouverte : arrêtez-la d'abord")
        self.video_ouverte = True  # posé avant l'attente : deux démarrages simultanés n'en font qu'un
        try:
            reglages = await self.fournir_video()
        except Exception as erreur:  # noqa: BLE001
            self.video_ouverte = False
            return _refus(502, f"vidéo indisponible : {masquer(str(erreur)) or type(erreur).__name__}")
        self._noter("vidéo : session ouverte")  # jamais les valeurs : ce sont des secrets temporaires
        return web.json_response(reglages)

    async def _video_arreter(self, _requete: web.Request) -> web.StreamResponse:
        """La page a fermé sa session : aucun arrêt de canal n'est connu côté service."""
        if self.video_ouverte:
            self._noter("vidéo : session fermée par la page")
        self.video_ouverte = False
        return web.json_response({"video": "arrêtée"})


def _en_demonstration(objet: Any) -> Any:
    if isinstance(objet, dict):
        return {c: MODES[2] if c == "mode" and v == MODES[1] else _en_demonstration(v) for c, v in objet.items()}
    if isinstance(objet, list):
        return [_en_demonstration(element) for element in objet]
    return objet


def liste_zones(carte: cartes.Carte) -> list[dict[str, Any]]:
    """Nommées d'abord (ordre alphabétique), puis les autres par surface décroissante."""
    lignes = []
    for zone in carte.zones:
        surface = round(abs(geometrie.aire_signee(zone.contour)), 2) if len(zone.contour) >= 3 else 0.0
        motif = retouche.empechement(carte, zone)
        lignes.append({
            "identifiant": zone.identifiant,
            "libelle": f"{zone.libelle} — {surface:g} m², {len(zone.contour)} sommets",
            "nommee": bool(zone.nom), "surface_m2": surface,
            "retouche_possible": motif is None, "motif": motif,
            "sommets_confondus": len(zone.contour) - len(geometrie.reduire(zone.contour)[0]),
        })
    nommees = sorted((ligne for ligne in lignes if ligne["nommee"]), key=lambda ligne: ligne["libelle"].casefold())
    autres = sorted((ligne for ligne in lignes if not ligne["nommee"]), key=lambda ligne: -ligne["surface_m2"])
    return nommees + autres


def _refus(statut: int, message: str) -> web.Response:
    return web.json_response({"erreur": message}, status=statut)


async def _demander(http: aiohttp.ClientSession, port: int, route: str, corps: dict[str, Any]) -> tuple[int, Any]:
    """POST local, sans Origin ni Referer ; tout autre programme répond autrement, ou pas du tout."""
    try:
        async with http.post(f"http://{HOTE}:{port}{route}", json=corps, allow_redirects=False) as reponse:
            return reponse.status, await reponse.json(content_type=None)
    except Exception:  # noqa: BLE001
        return 0, None


def _est_lymow(corps: Any) -> bool:
    return isinstance(corps, dict) and corps.get("interface") == "lymow" and corps.get("mode") in MODES


async def _port_libere(port: int, delai_s: float) -> bool:
    fin = time.monotonic() + delai_s
    while time.monotonic() < fin:
        try:
            _lecteur, ecrivain = await asyncio.open_connection(HOTE, port)
        except OSError:
            return True
        ecrivain.close()
        await asyncio.sleep(0.1)
    return False


async def rejoindre(port: int, reel: bool, demonstration: bool = False, empreinte: str | None = None) -> str:
    """Port occupé par une interface lymow du même mode : même version, elle rouvre sa page ;
    autre version, elle s'arrête proprement et rend `REMPLACEE` pour que la nouvelle démarre."""
    mode = MODES[2] if demonstration else MODES[1] if reel else MODES[0]
    empreinte = empreinte or empreinte_du_code()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
        _statut, identite = await _demander(http, port, ROUTE_IDENTITE, {})
        if not (_est_lymow(identite) and isinstance(identite.get("empreinte"), str)):
            # Pas d'identité : un autre programme, ou une interface d'avant ces routes (constat du
            # 2026-10-12). Un mode volontairement inconnu la fait répondre sans rouvrir sa page.
            _statut, ancienne = await _demander(http, port, ROUTE_REOUVERTURE, {"mode": "identification"})
            if _est_lymow(ancienne):
                raise EchecDemarrage(
                    "une ancienne version de l'interface tourne encore : fermez sa fenêtre noire puis relancez"
                )
            raise EchecDemarrage(
                f"le port {port} est occupé par un autre programme : fermez-le, ou choisissez un autre port "
                f"avec « lymow ui --port N »"
            )
        if identite["mode"] != mode:
            raise EchecDemarrage(
                f"une interface en mode {identite['mode']} tourne déjà ; fermez sa fenêtre avant de lancer le mode {mode}"
            )
        if identite["empreinte"] != empreinte:
            statut, corps = await _demander(http, port, ROUTE_ARRET, {"mode": mode})
            if not (statut == 200 and _est_lymow(corps) and corps.get("ok") is True):
                raise EchecDemarrage("une ancienne version est en cours d'utilisation, fermez sa fenêtre")
            if not await _port_libere(port, DELAI_LIBERATION_S):
                raise EchecDemarrage(
                    f"l'ancienne version n'a pas libéré le port {port} en {DELAI_LIBERATION_S:g} s : fermez sa fenêtre puis relancez"
                )
            return REMPLACEE
        statut, corps = await _demander(http, port, ROUTE_REOUVERTURE, {"mode": mode})
    if _est_lymow(corps) and corps.get("ok") is True:
        return "interface déjà ouverte : page rouverte dans le navigateur"
    if statut == 429:
        raise EchecDemarrage("interface déjà ouverte, page rouverte à l'instant : réessayez dans quelques secondes")
    raise EchecDemarrage(
        "interface déjà ouverte, mais son navigateur n'a pas pu être lancé : l'adresse à ouvrir est dans sa fenêtre"
    )


async def servir(
    cal: Calibration, journal: Journal, *, reel: bool, port: int, navigateur: bool = True, dossier: Path | None = None,
    demonstration: bool = False,
) -> str:
    remplacee = False
    while True:
        serveur = ServeurUI(cal, journal, reel=reel, port=port, dossier=dossier, demonstration=demonstration)
        if not navigateur:
            serveur.ouvrir_navigateur = lambda _adresse: False
        try:
            await serveur.demarrer()
            break
        except OSError:  # port déjà pris (WinError 10048) : une interface tourne peut-être déjà
            await serveur.arreter()
            issue = await rejoindre(port, reel, demonstration, serveur.empreinte)
            if issue != REMPLACEE:
                return issue
            if remplacee:
                raise EchecDemarrage(f"le port {port} reste occupé après l'arrêt de l'ancienne version") from None
            remplacee = True
    rappel.noter_port(rappel.dossier_ports(), serveur.port, True)
    try:
        if remplacee:
            print("Ancienne version remplacée : l'interface à jour démarre.")
        print(f"Interface en mode {serveur.mode.upper() if reel else serveur.mode} sur {serveur.adresse} — Ctrl+C pour arrêter.")
        serveur.ouvrir_page()  # en cas d'échec : adresse affichée, et la fenêtre reste ouverte
        await serveur.fin.wait()
    finally:
        rappel.noter_port(rappel.dossier_ports(), serveur.port, False)
        await serveur.arreter()
    return "interface arrêtée"
