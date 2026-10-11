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
from typing import Any

import aiohttp
from aiohttp import web

from . import auth, conduite, liaison, mouvement, securite
from .config import Calibration
from .journal import Journal, assainir, declarer_secret, masquer
from .mouvement import ErreurSaisie, Ordre
from .protocole import Categorie

HOTE = "127.0.0.1"  # jamais 0.0.0.0 : aucune option ne change cette valeur
PORT_DEFAUT = 8377
COOKIE = "lymow_ui"
DUREE_PREVISUALISATION_S = 60.0
ETAPES_MAX = 20
NOMBRE_MAX = 1_000_000  # au-delà, une valeur de la page est refusée sans calcul
STATIQUE = Path(__file__).resolve().parent / "ui_statique"
FICHIERS = {
    "app.js": "text/javascript",
    "signalisation.js": "text/javascript",
    "style.css": "text/css",
}
NOMS_ORDRES = (*mouvement.ETAPES_SEQUENCE, mouvement.ESSAI, *mouvement.TACHES)
NON_CONNECTE = "non connecté — lancez « lymow connexion »"


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
    def __init__(self, cal: Calibration, journal: Journal, *, reel: bool, port: int = PORT_DEFAUT) -> None:
        self.cal, self.journal, self.reel, self.port = cal, journal, reel, port
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
        self.previsualisation: tuple[str, list[Ordre], float] | None = None
        self.resultat: dict[str, Any] | None = None
        self.fil: list[dict[str, str]] = []
        self.version = 0
        self.ferme = False
        self.fin = asyncio.Event()
        self.fournir_video = self._video
        self.video_ouverte = False
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
            ]
        )
        # Pas de journal d'accès : la première requête porte le jeton dans son adresse.
        self._lanceur = web.AppRunner(application, access_log=None, shutdown_timeout=1.0)
        await self._lanceur.setup()
        site = web.TCPSite(self._lanceur, HOTE, self.port)
        await site.start()
        self.port = self.adresses_ecoute()[0][1]
        await self._connecter()
        self._noter(f"interface démarrée en mode {'RÉEL' if self.reel else 'simulation'}")

    def adresses_ecoute(self) -> list[tuple[str, int]]:
        assert self._lanceur is not None
        return [prise.getsockname()[:2] for site in self._lanceur.sites for prise in site._server.sockets]  # type: ignore[union-attr]

    @property
    def adresse(self) -> str:
        return f"http://{HOTE}:{self.port}/"

    async def arreter(self) -> None:
        """SEC-03 : un mouvement en cours reçoit son arrêt AVANT la fermeture de la liaison."""
        if self.ferme:
            return
        self.ferme = True
        self._stop_demande = True
        await self._interrompre()
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
            "mode": "réel" if self.reel else "simulation",
            "connecte": session is not None and bool(session.lien.connecte),
            "liaison": self.etat_liaison,
            "appareil": self.ident.appareil if self.ident else None,
            "occupe": self.occupe,
            "etat": etat,
            "resultat": self.resultat,
            "journal": self.fil[-100:],
        }
        if complet:
            image |= {"bornes": _bornes(), "libelles": conduite.LIBELLES}
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
                await self._pile.aclose()
                self._pile, self.session = AsyncExitStack(), None
                await self._connecter_sans_verrou()
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
        bilan, code = conduite.simuler_ordres(ordres, self.cal, self.journal)
        identifiant = None
        self.previsualisation = None  # une nouvelle prévisualisation annule toujours la précédente
        if self.reel and code == 0:
            identifiant = secrets.token_urlsafe(16)
            self.previsualisation = (identifiant, ordres, self.horloge() + DUREE_PREVISUALISATION_S)
        texte = " ; ".join(str(ordre) for ordre in ordres)
        self._noter(f"prévisualisation : {texte}" + ("" if code == 0 else f" — refusée : {bilan.get('motif')}"))
        return web.json_response({"bilan": bilan, "identifiant": identifiant, "ordres": texte})

    async def _emettre(self, requete: web.Request) -> web.StreamResponse:
        # SEC-05 : serveur lancé sans --reel, aucun ordre réel, quoi que demande la page.
        if not self.reel:
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
        self._lancer(attendue[1])
        return web.json_response({"accepte": True}, status=202)

    def _lancer(self, ordres: list[Ordre]) -> None:
        # Jamais deux tâches : l'appelant a vérifié, ceci interdit toute tâche orpheline.
        if self.occupe:
            raise web.HTTPConflict(reason="un ordre est déjà en cours")
        self._noter("émission : " + " ; ".join(str(ordre) for ordre in ordres))
        self.tache = asyncio.create_task(self._conduire(ordres))

    async def _conduire(self, ordres: list[Ordre]) -> None:
        """Le mouvement vit ici : aucune requête de la page n'est nécessaire pour qu'il s'arrête."""
        try:
            bilan, code = await conduite.emettre_ordres(self.session, ordres, self.journal)
            self.resultat = assainir(bilan | {"code_sortie": code})  # filtré comme le journal
            if code == conduite.CODE_ARRET_NON_CONFIRME:
                self._signaler("ARRÊT NON CONFIRMÉ : arrêtez le robot par son bouton STOP ou par l'application.")
            self._noter("ordre terminé" if code == 0 else f"ordre non abouti : {bilan.get('motif', 'voir le résultat')}")
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
            if not self.reel:
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

    # --- vidéo ----------------------------------------------------------------

    async def _video(self) -> dict[str, Any]:
        async with self._verrou_acces, aiohttp.ClientSession(timeout=liaison.DELAI_HTTP) as http:
            # Relu à chaque fois : `obtenir_acces` fait tourner le jeton dans le coffre, et
            # présenter l'ancien pourrait révoquer la connexion du compte.
            self.ident = liaison.lire_identifiants()
            acces = await liaison.obtenir_acces(http, self.ident)
            return await liaison.demarrer_video(http, self.ident, acces)

    async def _video_demarrer(self, _requete: web.Request) -> web.StreamResponse:
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


def _refus(statut: int, message: str) -> web.Response:
    return web.json_response({"erreur": message}, status=statut)


async def servir(cal: Calibration, journal: Journal, *, reel: bool, port: int, navigateur: bool = True) -> None:
    serveur = ServeurUI(cal, journal, reel=reel, port=port)
    await serveur.demarrer()
    try:
        print(f"Interface en mode {'RÉEL' if reel else 'simulation'} sur {serveur.adresse} — Ctrl+C pour arrêter.")
        adresse = f"{serveur.adresse}?jeton={serveur.jeton}"
        if not (navigateur and webbrowser.open(adresse)):
            print(f"Ouvrez cette adresse (valable pour ce lancement seulement) : {adresse}")
        await serveur.fin.wait()
    finally:
        await serveur.arreter()
