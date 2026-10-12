"""Interface web locale `lymow ui` : serveur HTTP sur la boucle locale uniquement.

Rôle : exposer à une page servie localement les MÊMES chemins que la ligne de
commande (`conduite`). Aucune émission propre à l'interface. Frontières : écoute
sur 127.0.0.1 sans option pour en changer, jeton par lancement sur toutes les
routes, contrôle de Host et d'Origin, aucune identité saisie dans la page.
Le mouvement vit ici, côté serveur : la page peut disparaître, l'arrêt part.
"""

from __future__ import annotations

import asyncio
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

from . import auth, compte, conduite, demo, liaison, mouvement, rappel, retouche, securite
from . import carte as cartes
from .config import Calibration
from .journal import Journal, assainir, declarer_secret, masquer
from .mouvement import ErreurSaisie, Ordre
from .securite import RefusSecurite
from .protocole import Categorie
from .session import Session

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
}
NOMS_ORDRES = (*mouvement.ETAPES_SEQUENCE, mouvement.ESSAI, *mouvement.TACHES)
NON_CONNECTE = "non connecté — utilisez le panneau « Compte » de cette page"
ROUTE_REOUVERTURE = "/api/instance/rouvrir"
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
                web.post("/api/video/demarrer", self._video_demarrer),
                web.post("/api/video/arreter", self._video_arreter),
                web.get("/favicon.ico", self._sans_contenu),
                web.post(ROUTE_REOUVERTURE, self._rouvrir),
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
        await self._interrompre()
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
                liaison.ouvrir_session(self.journal, self.cal, autorisees, self.ident)
            )
        except Exception as erreur:  # noqa: BLE001
            self.session = None
            self.etat_liaison = f"connexion impossible : {masquer(str(erreur)) or type(erreur).__name__}"
            return
        self.session.signaler = self._signaler
        self.etat_liaison = "connecté"
        await self.session.solliciter()

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
        if requete.path in (ROUTE_REOUVERTURE, rappel.ROUTE):
            # Réservées à un second processus lymow de ce poste : un navigateur joint toujours
            # Origin ou Referer à un POST, donc aucun site web ne peut les déclencher.
            if requete.method != "POST":
                return _refus(405, "méthode refusée")
            if "Origin" in requete.headers or "Referer" in requete.headers:
                return _refus(403, "origine refusée")
            return await (self._rouvrir(requete) if requete.path == ROUTE_REOUVERTURE else self._rappel(requete))
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
        return web.Response(text=(STATIQUE / "index.html").read_text(encoding="utf-8"), content_type="text/html")

    async def _statique(self, requete: web.Request) -> web.StreamResponse:
        nom = requete.match_info["nom"]
        if nom not in FICHIERS:
            return _refus(404, "fichier inconnu")
        return web.Response(text=(STATIQUE / nom).read_text(encoding="utf-8"), content_type=FICHIERS[nom])

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
            "connecte": session is not None and bool(session.lien.connecte),
            "liaison": self.etat_liaison,
            "appareil": demo.APPAREIL if self.demonstration else self.ident.appareil if self.ident else None,
            "occupe": self.occupe,
            "compte": self._compte(),
            "etat": etat,
            "resultat": self.resultat,
            "journal": self.fil[-100:],
        }
        if complet:
            image |= {
                "bornes": _bornes(), "libelles": conduite.LIBELLES,
                "regions": list(auth.REGIONS), "region_defaut": auth.REGION_DEFAUT,
            }
        return image

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
        return web.json_response({"bilan": bilan, "identifiant": identifiant, "ordres": texte})

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

    def _lancer_travail(self, texte: str, travail: Any) -> None:
        # Jamais deux tâches : l'appelant a vérifié, ceci interdit toute tâche orpheline.
        if self.occupe:
            raise web.HTTPConflict(reason="un ordre est déjà en cours")
        self._noter(f"émission : {texte}")
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

    async def _stop(self, _requete: web.Request) -> web.StreamResponse:
        """STOP : sans prévisualisation, prioritaire sur tout ordre en cours."""
        async with self._verrou_stop:
            if not self.emet:
                bilan, _code = conduite.simuler_ordres([Ordre("stop")], self.cal, self.journal)
                self._noter("STOP (simulation)")
                return web.json_response({"bilan": bilan})
            if self.session is None:
                return _refus(503, f"robot injoignable : {self.etat_liaison}")
            self._noter("STOP demandé")
            self.previsualisation = None
            self._stop_demande = True
            try:
                await self._interrompre()  # l'arrêt de fin de mouvement part d'abord
                self._lancer([Ordre("stop")])  # puis le `stop` de la commande : consignes nulles ×3, pause
            finally:
                self._stop_demande = False
            return web.json_response({"accepte": True}, status=202)

    # --- carte : lecture, puis retouche par le même chemin que la commande -----

    def _carte_json(self) -> dict[str, Any]:
        carte = cartes.en_memoire(self.session) if self.session is not None else None
        if carte is None:
            return {"carte": None}
        return {
            "carte": cartes.vers_json(carte), "heure": self.carte_heure,
            "age_s": round(self.session.horloge() - carte.recue, 1),
        }

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

    async def _rouvrir_liaison(self) -> None:
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


def _refus(statut: int, message: str) -> web.Response:
    return web.json_response({"erreur": message}, status=statut)


async def rejoindre(port: int, reel: bool, demonstration: bool = False) -> str:
    """Port occupé : si c'est une interface lymow du même mode, lui faire rouvrir sa page."""
    mode = MODES[2] if demonstration else MODES[1] if reel else MODES[0]
    corps: Any = None
    statut = 0
    try:
        delai = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=delai) as http:
            adresse = f"http://{HOTE}:{port}{ROUTE_REOUVERTURE}"
            async with http.post(adresse, json={"mode": mode}, allow_redirects=False) as reponse:
                statut = reponse.status
                corps = await reponse.json(content_type=None)
    except Exception:  # noqa: BLE001 — tout autre programme répond autrement, ou pas du tout
        corps = None
    if not isinstance(corps, dict) or corps.get("interface") != "lymow" or corps.get("mode") not in MODES:
        raise EchecDemarrage(
            f"le port {port} est occupé par un autre programme : fermez-le, ou choisissez un autre port "
            f"avec « lymow ui --port N »"
        )
    if corps["mode"] != mode:
        raise EchecDemarrage(
            f"une interface en mode {corps['mode']} tourne déjà ; fermez sa fenêtre avant de lancer le mode {mode}"
        )
    if corps.get("ok") is True:
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
    serveur = ServeurUI(cal, journal, reel=reel, port=port, dossier=dossier, demonstration=demonstration)
    if not navigateur:
        serveur.ouvrir_navigateur = lambda _adresse: False
    try:
        await serveur.demarrer()
    except OSError:  # port déjà pris (WinError 10048) : une interface tourne peut-être déjà
        await serveur.arreter()
        return await rejoindre(port, reel, demonstration)
    rappel.noter_port(rappel.dossier_ports(), serveur.port, True)
    try:
        print(f"Interface en mode {serveur.mode.upper() if reel else serveur.mode} sur {serveur.adresse} — Ctrl+C pour arrêter.")
        serveur.ouvrir_page()  # en cas d'échec : adresse affichée, et la fenêtre reste ouverte
        await serveur.fin.wait()
    finally:
        rappel.noter_port(rappel.dossier_ports(), serveur.port, False)
        await serveur.arreter()
    return "interface arrêtée"
