"""Essai de la page dans un VRAI navigateur, sans affichage, contre l'interface de DÉMONSTRATION.

Rôle : jouer les gestes d'un opérateur (clics de souris réels, touches, boîtes de dialogue) sur la page servie
par `ServeurUI` en démonstration — robot fictif, aucun compte, aucun réseau, rien n'est émis vers un vrai
robot — et rendre un compte rendu JSON, des captures d'écran et les erreurs de console.
Frontières : outil de mise au point, lancé à la main ; il n'est PAS exécuté par pytest et n'ajoute aucune
dépendance (aiohttp, déjà requis, parle au navigateur par son protocole de débogage). Il pilote Microsoft Edge
ou Chrome s'il en trouve un ; sinon il le dit et s'arrête.

Usage :
    python outils/essai_navigateur.py <dossier-de-sortie> [recalage] [boutons] [diagonales] [--navigateur CHEMIN]
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))
sys.path.insert(0, str(RACINE / "tests"))

from conftest import FauxRegistre  # noqa: E402 — le registre réel de Windows n'est jamais lu

from lymow_pilotage import ui  # noqa: E402
from lymow_pilotage.config import Calibration  # noqa: E402
from lymow_pilotage.journal import Journal  # noqa: E402

NAVIGATEURS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
PORT_DEBOGAGE = 9341
TOUCHES = {"ArrowUp": 38, "ArrowLeft": 37, "ArrowDown": 40, "ArrowRight": 39, "Escape": 27, "1": 49, "2": 50, "3": 51, "4": 52}


class Page:
    """Une page pilotée par le protocole de débogage : évaluation, souris, clavier, captures, boîtes de dialogue."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, sortie: Path) -> None:
        self.ws, self.sortie = ws, sortie
        self.compteur = 0
        self.dialogues: list[str] = []
        self.erreurs: list[str] = []

    async def appel(self, methode: str, **params: Any) -> dict[str, Any]:
        self.compteur += 1
        numero = self.compteur
        await self.ws.send_json({"id": numero, "method": methode, "params": params})
        while True:
            message = json.loads((await self.ws.receive()).data)
            if message.get("method") == "Page.javascriptDialogOpening":
                self.dialogues.append(message["params"]["message"].split("\n")[0])
                self.compteur += 1
                await self.ws.send_json({"id": self.compteur, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}})
            elif message.get("method") == "Runtime.exceptionThrown":
                details = message["params"]["exceptionDetails"]
                self.erreurs.append((details.get("exception", {}).get("description") or details.get("text", "?"))[:300])
            elif message.get("method") == "Log.entryAdded" and message["params"]["entry"]["level"] == "error":
                self.erreurs.append(message["params"]["entry"]["text"][:200])
            if message.get("id") == numero:
                return message.get("result", {})

    async def js(self, expression: str) -> Any:
        resultat = await self.appel("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return resultat.get("result", {}).get("value")

    async def attendre(self, secondes: float) -> None:
        fin = time.monotonic() + secondes
        while time.monotonic() < fin:
            await self.js("1")
            await asyncio.sleep(0.2)

    async def jusqu_a(self, expression: str, delai: float = 15.0) -> bool:
        fin = time.monotonic() + delai
        while time.monotonic() < fin:
            if await self.js(expression):
                return True
            await asyncio.sleep(0.2)
        return False

    async def touche(self, nom: str) -> None:
        for genre in ("rawKeyDown", "keyUp"):
            await self.appel("Input.dispatchKeyEvent", type=genre, key=nom, code=nom, windowsVirtualKeyCode=TOUCHES[nom])

    async def appuyer(self, nom: str) -> None:
        await self.appel("Input.dispatchKeyEvent", type="rawKeyDown", key=nom, code=nom, windowsVirtualKeyCode=TOUCHES[nom])

    async def relacher(self, nom: str) -> None:
        await self.appel("Input.dispatchKeyEvent", type="keyUp", key=nom, code=nom, windowsVirtualKeyCode=TOUCHES[nom])

    async def clic(self, x: float, y: float) -> None:
        """Un clic de souris RÉEL (pressé puis relâché), aux coordonnées de la fenêtre."""
        await self.appel("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        for genre in ("mousePressed", "mouseReleased"):
            await self.appel("Input.dispatchMouseEvent", type=genre, x=x, y=y, button="left", clickCount=1)

    async def centre(self, selecteur: str) -> tuple[float, float] | None:
        """Centre visible d'un élément, après l'avoir fait défiler dans la fenêtre ; rien s'il est masqué."""
        boite = await self.js(
            "(function () { var e = document.querySelector(%s); if (!e) { return null; } e.scrollIntoView({block: 'center'});"
            " var r = e.getBoundingClientRect(); return r.width && r.height ? [r.left + r.width / 2, r.top + r.height / 2] : null; })()"
            % json.dumps(selecteur)
        )
        return None if boite is None else (boite[0], boite[1])

    async def cliquer(self, selecteur: str) -> bool:
        point = await self.centre(selecteur)
        if point is None:
            return False
        await self.clic(*point)
        return True

    async def capture(self, nom: str) -> None:
        image = await self.appel("Page.captureScreenshot", format="png")
        (self.sortie / nom).write_bytes(base64.b64decode(image["data"]))


def evenements(dossier: Path) -> list[dict[str, Any]]:
    lignes = []
    for fichier in sorted(dossier.rglob("*.jsonl")):
        for ligne in fichier.read_text(encoding="utf-8").splitlines():
            try:
                lu = json.loads(ligne)
            except ValueError:
                continue
            if isinstance(lu, dict) and "evenement" in lu:
                lignes.append(lu)
    return lignes


async def scenario_recalage(page: Page, serveur: Any, donnees: Path) -> dict[str, Any]:
    """Onglet Carte → « Le robot est ici » → clic réel à ~3 m du robot → décalage affiché → « Annuler le décalage »."""
    bilan: dict[str, Any] = {}
    await page.cliquer("#onglet-carte")
    bilan["carte_dessinee"] = await page.jusqu_a("document.querySelectorAll('#carte-dessin polygon').length > 0")
    await page.attendre(1.0)
    avant = serveur.instantane()["etat"]
    bilan["bouton_visible"] = await page.cliquer("#recalage-ici")
    await page.attendre(0.5)
    bilan["bandeau_pointage"] = await page.js(
        "document.getElementById('recalage-bandeau').hidden ? '' : document.getElementById('recalage-bandeau').textContent")
    bilan["curseur"] = await page.js("getComputedStyle(document.getElementById('carte-dessin')).cursor")
    await page.capture("recalage-1-pointage.png")
    # Le point visé : à 3 m à l'est du robot, d'après l'échelle de la vue.
    cible = await page.js(
        "(function () { var d = document.getElementById('carte-dessin'), r = d.getBoundingClientRect(), v = d.viewBox.baseVal;"
        " var k = Math.max(v.width / r.width, v.height / r.height); var m = d.querySelector('.carte-robot');"
        " var b = m ? m.getBoundingClientRect() : r; return [b.left + b.width / 2 + 3 / k, b.top + b.height / 2, k]; })()"
    )
    # Ce que le clic doit désigner, d'après la matrice du dessin lui-même (indépendante du code de la page).
    attendu = await page.js(
        "(function () { var d = document.getElementById('carte-dessin'); var p = d.createSVGPoint(); p.x = %f; p.y = %f;"
        " var q = p.matrixTransform(d.getScreenCTM().inverse()); return [q.x, -q.y]; })()" % (cible[0], cible[1])
    )
    # Un clic « tremblé » : relâché 9 pixels plus loin, comme une main sur un pavé tactile.
    await page.appel("Input.dispatchMouseEvent", type="mouseMoved", x=cible[0] - 9, y=cible[1])
    await page.appel("Input.dispatchMouseEvent", type="mousePressed", x=cible[0] - 9, y=cible[1], button="left", clickCount=1)
    await page.appel("Input.dispatchMouseEvent", type="mouseMoved", x=cible[0], y=cible[1], button="left")
    await page.appel("Input.dispatchMouseEvent", type="mouseReleased", x=cible[0], y=cible[1], button="left", clickCount=1)
    bilan["decalage_enregistre"] = await page.jusqu_a("document.getElementById('recalage-texte').textContent.indexOf('décalage appliqué') !== -1", 8.0)
    bilan["texte_decalage"] = await page.js("document.getElementById('recalage-texte').textContent")
    await page.jusqu_a("document.querySelectorAll('#carte-dessin .carte-decalage').length > 0", 6.0)  # l'état suivant est arrivé
    apres = serveur.instantane()
    bilan["decalage_serveur_m"] = [apres["recalage"]["est_m"], apres["recalage"]["nord_m"]]
    bilan["marqueur_deplace_m"] = None if avant is None or apres["etat"] is None else [
        round(apres["etat"]["position_est_m"] - avant["position_est_m"], 2), round(apres["etat"]["position_nord_m"] - avant["position_nord_m"], 2),
    ]
    recale = apres["etat"]
    bilan["erreur_de_conversion_m"] = None if recale is None else round(
        ((recale["position_est_m"] - attendu[0]) ** 2 + (recale["position_nord_m"] - attendu[1]) ** 2) ** 0.5, 3)
    bilan["valeur_sur_le_trait"] = await page.js("(document.querySelector('#carte-dessin .carte-decalage-texte') || {}).textContent || ''")
    bilan["trait_de_decalage"] = await page.js("document.querySelectorAll('#carte-dessin .carte-decalage').length")
    bilan["mention_permanente"] = await page.js("document.getElementById('pastilles').textContent.indexOf('carte recalée') !== -1")
    await page.capture("recalage-2-applique.png")
    bilan["evenements"] = [e["evenement"] for e in evenements(donnees) if e["evenement"].startswith("recalage")]
    await page.cliquer("#recalage-annuler")
    bilan["annule"] = await page.jusqu_a("document.getElementById('recalage-texte').textContent.indexOf('décalage appliqué') === -1", 8.0)
    bilan["evenements_apres_annulation"] = [e["evenement"] for e in evenements(donnees) if e["evenement"].startswith("recalage")]
    # Échap en mode pointage : annule le pointage (et reste un STOP, comme toujours).
    await page.cliquer("#recalage-ici")
    await page.touche("Escape")
    await page.attendre(0.5)
    bilan["echap_annule_le_pointage"] = await page.js("document.getElementById('recalage-ici').textContent") == "Le robot est ici"
    await page.cliquer("#onglet-camera")
    return bilan


async def scenario_boutons(page: Page, serveur: Any, donnees: Path) -> dict[str, Any]:
    """Clique réellement chaque bouton de la grille d'actions et des groupes de pas / allure ; relève ce qui en sort."""
    bilan: dict[str, Any] = {"segments": {}, "actions": {}}
    for groupe, liste in (("segments-distance", "pave-distance"), ("segments-angle", "pave-angle"), ("segments-allure", "pave-allure")):
        valeurs = await page.js(f"Array.prototype.map.call(document.getElementById('{liste}').options, function (o) {{ return o.value; }})")
        obtenues = []
        for rang in range(len(valeurs)):
            await page.cliquer(f"#{groupe} button:nth-child({rang + 1})")
            obtenues.append(await page.js(f"document.getElementById('{liste}').value"))
        bilan["segments"][groupe] = {"attendu": valeurs, "obtenu": obtenues, "ok": valeurs == obtenues}
    # Prise de main, puis une flèche du pavé de gauche, les touches 1 à 4, STOP au centre.
    await page.cliquer("#prendre-main")
    bilan["main_prise"] = await page.jusqu_a("document.getElementById('etat-main').textContent.indexOf('Vous avez la main') !== -1")
    await page.touche("1")
    bilan["touche_1_pas"] = await page.js("document.getElementById('pave-distance').value")
    await page.cliquer(".croix [data-fleche='haut']")
    bilan["fleche_du_pave"] = await page.jusqu_a("document.getElementById('fil').textContent.indexOf('↑') !== -1", 20.0)
    await page.jusqu_a("!document.getElementById('stop').disabled", 1.0)
    await page.attendre(4.0)
    await page.cliquer("#stop-centre")
    await page.attendre(4.0)
    bilan["stop_centre"] = any(e["evenement"] == "arret-emis" for e in evenements(donnees))
    # Grille d'actions : chaque bouton cliqué réellement ; on relève s'il était grisé, et ce que le journal en dit.
    attendus = {
        "pause-bouton": "pause", "reprendre-bouton": "reprendre", "base-bouton": "base", "pave-plateau": "plateau-haut",
        "phares-bouton": "phares", "annuler-bouton": "annuler", "recalage": None, "bon-signal-bouton": None,
    }
    for identifiant, mot in attendus.items():
        await page.jusqu_a("!document.body.classList.contains('occupe') && true", 0.2)
        for _ in range(60):  # attendre la fin de l'ordre précédent
            if not serveur.occupe:
                break
            await page.attendre(0.3)
        await page.attendre(0.6)
        grise = await page.js(f"document.getElementById('{identifiant}').disabled")
        raison = await page.js(f"document.getElementById('{identifiant}').title")
        nombre = len(evenements(donnees))
        clique = False if grise else await page.cliquer(f"#{identifiant}")
        await page.attendre(1.5)
        nouveaux = [e for e in evenements(donnees)[nombre:] if e["evenement"] in ("ordre-emis", "ordre-simule", "session-reelle", "ordre-refuse")]
        textes = json.dumps(nouveaux, ensure_ascii=False)
        bilan["actions"][identifiant] = {
            "grise": bool(grise), "raison": raison[:90] if grise else "", "clique": clique,
            "journal": sorted({e["evenement"] for e in nouveaux}), "ordre_attendu_vu": None if mot is None or grise else mot in textes,
        }
    # Voyants LED : dans la section repliée « Ordres détaillés » (ce ne sont pas les phares).
    await page.js("document.getElementById('volet-ordres').open = true")
    for sens in ("allumer", "eteindre"):
        for _ in range(60):
            if not serveur.occupe:
                break
            await page.attendre(0.3)
        nombre = len(evenements(donnees))
        clique = await page.cliquer(f"[data-voyants='{sens}']")
        await page.attendre(1.5)
        textes = json.dumps([e for e in evenements(donnees)[nombre:] if e["evenement"] == "ordre-emis"], ensure_ascii=False)
        bilan["actions"][f"voyants-{sens}"] = {"clique": clique, "ordre_attendu_vu": f"voyants LED : {'allumer' if sens == 'allumer' else 'éteindre'}" in textes}
    await page.js("document.getElementById('volet-ordres').open = false; window.scrollTo(0, 0)")
    bilan["fil_apres_les_phares"] = [n["texte"] for n in serveur.fil if n["texte"].startswith("phares")]
    bilan["onglet_carte_apres_bon_signal"] = await page.js("!document.getElementById('vue-carte').hidden")
    await page.capture("boutons.png")
    return bilan


async def scenario_nettoyage(page: Page, serveur: Any, donnees: Path) -> dict[str, Any]:
    """Clique réellement : levée de nettoyage (Calibration), « Essayer un pas plateau levé », une flèche, puis
    « Redescendre le plateau » du bloc Pilotage. Le robot fictif rejoue le constat réel du 2026-10-13."""
    bilan: dict[str, Any] = {}
    robot = serveur.session.lien

    async def fin(condition: Any, tours: int = 150) -> bool:
        for _ in range(tours):
            if not serveur.occupe and condition():
                return True
            await page.attendre(0.2)
        return False

    visible = "!document.getElementById('nettoyage').hidden"
    bilan["bloc_masque_au_depart"] = not await page.js(visible)
    await page.js("document.querySelector(\"[data-volet='calibration']\").open = true")
    bilan["clic_levee"] = await page.cliquer("#plateau-essais [data-plateau-essai='nettoyage']")
    bilan["statut_13_pose_par_l_outil"] = await fin(lambda: robot.statut == 13 and serveur.nettoyage.posee is not None)
    await page.js("document.querySelector(\"[data-volet='calibration']\").open = false; window.scrollTo(0, 0)")
    bilan["bloc_affiche"] = await page.jusqu_a(visible, 5.0)
    bilan["texte"] = await page.js("document.getElementById('nettoyage-texte').textContent")
    await page.capture("nettoyage.png")
    bilan["clic_essai"] = await page.cliquer("#nettoyage-essai")
    bilan["essai_autorise"] = await fin(lambda: serveur.nettoyage.essai_actif, 30)
    bilan["page_a_jour"] = await page.jusqu_a("document.getElementById('nettoyage-essai').disabled", 5.0)  # sinon la prise de main juge un état périmé
    await page.cliquer("#segments-distance button:nth-child(1)")  # 0,25 m
    if not await page.js("document.getElementById('etat-main').textContent.indexOf('Vous avez la main') !== -1"):
        await page.cliquer("#prendre-main")
    await page.jusqu_a("!document.querySelector('.croix [data-fleche=\\'haut\\']').disabled")
    nombre, avant = len(serveur.direct.faites), (robot.est, robot.nord)
    bilan["clic_fleche"] = await page.cliquer(".croix [data-fleche='haut']")
    bilan["pas_termine"] = await fin(lambda: len(serveur.direct.faites) > nombre)
    bilan["main"] = await page.js("document.getElementById('etat-main').textContent + ' / ' + document.getElementById('direct-etat').textContent + ' / ' + document.getElementById('fil').textContent")
    rapport = serveur.direct.faites[-1]["rapport"] if len(serveur.direct.faites) > nombre else {}
    bilan["constat_du_pas"] = rapport.get("essai_plateau_leve") or rapport.get("echec")
    bilan["robot_fictif_immobile"] = (robot.est, robot.nord) == avant
    bilan["clic_redescendre"] = await page.cliquer("#nettoyage-redescendre")
    bilan["sorti_de_l_arret_d_urgence"] = await fin(lambda: robot.statut == 1)
    bilan["verdict"] = (serveur.resultat or {}).get("verdict")
    bilan["bloc_masque_a_la_fin"] = await page.jusqu_a("document.getElementById('nettoyage').hidden", 5.0)
    bilan["fil"] = [n["texte"] for n in serveur.fil[-8:]]
    bilan["journal"] = sorted({e["evenement"] for e in evenements(donnees) if "nettoyage" in e["evenement"] or "plateau-leve" in e["evenement"]})
    return bilan


async def scenario_diagonales(page: Page, serveur: Any, donnees: Path) -> dict[str, Any]:
    """Clique réellement les quatre diagonales du pavé de gauche : le robot fictif doit décrire un arc (position ET
    cap changent). Puis ↑ et ← pressées ensemble : une diagonale, sans pas droit avant."""
    import math

    bilan: dict[str, Any] = {"clics": {}}
    robot = serveur.session.lien
    await page.cliquer("#onglet-camera")
    await page.cliquer("#segments-distance button:nth-child(2)")  # 0,5 m
    await page.cliquer("#segments-angle button:nth-child(2)")  # 45°
    if not await page.js("document.getElementById('etat-main').textContent.indexOf('Vous avez la main') !== -1"):
        await page.cliquer("#prendre-main")
    await page.jusqu_a("!document.querySelector('.croix [data-fleche=\\'haut-gauche\\']').disabled")

    async def fin_du_pas(nombre: int) -> None:
        for _ in range(200):
            if not serveur.occupe and len(serveur.direct.faites) > nombre:
                return
            await page.attendre(0.2)

    for direction in ("haut-gauche", "haut-droite", "bas-gauche", "bas-droite"):
        avant = (robot.est, robot.nord, robot.cap)
        nombre = len(serveur.direct.faites)
        clique = await page.cliquer(f".croix [data-fleche='{direction}']")
        await fin_du_pas(nombre)
        rapport = serveur.direct.faites[-1]["rapport"] if len(serveur.direct.faites) > nombre else {}
        dx, dy = robot.est - avant[0], robot.nord - avant[1]
        devant = dx * math.cos(avant[2]) + dy * math.sin(avant[2])
        gauche = -dx * math.sin(avant[2]) + dy * math.cos(avant[2])
        tourne = math.degrees((robot.cap - avant[2] + math.pi) % (2 * math.pi) - math.pi)
        bilan["clics"][direction] = {
            "clique": clique, "ordre": rapport.get("ordre"), "verification": rapport.get("verification"),
            "vers_l_avant_m": round(devant, 2), "vers_la_gauche_m": round(gauche, 2), "cap_deg": round(tourne, 1),
        }
    bilan["pastilles"] = await page.js("document.getElementById('fil').textContent")
    # Clavier : ↑ puis ← aussitôt (tenues ensemble) → UNE diagonale, aucun pas droit.
    nombre = len(serveur.direct.faites)
    await page.appuyer("ArrowUp")
    await page.appuyer("ArrowLeft")
    await page.relacher("ArrowUp")
    await page.relacher("ArrowLeft")
    await fin_du_pas(nombre)
    await page.attendre(1.0)
    bilan["clavier_haut_et_gauche"] = [f["rapport"].get("ordre") for f in serveur.direct.faites[nombre:]]
    # Une flèche seule part toujours, après sa courte temporisation.
    nombre = len(serveur.direct.faites)
    await page.touche("ArrowRight")
    await fin_du_pas(nombre)
    bilan["clavier_droite_seule"] = [f["rapport"].get("ordre") for f in serveur.direct.faites[nombre:]]
    await page.capture("diagonales.png")
    return bilan


async def principal(sortie: Path, scenarios: list[str], navigateur: str | None) -> int:
    chemin = navigateur or next((n for n in NAVIGATEURS if Path(n).exists()), None)
    if chemin is None:
        print("aucun navigateur trouvé (Edge ou Chrome) : indiquez --navigateur CHEMIN")
        return 2
    sortie.mkdir(parents=True, exist_ok=True)
    donnees = sortie / f"donnees-{int(time.time())}"  # un dossier neuf par essai : journal et préférences vierges
    serveur = ui.ServeurUI(Calibration(), Journal(donnees), reel=False, port=0, demonstration=True)
    serveur.registre = FauxRegistre()
    await serveur.demarrer()
    edge = subprocess.Popen([
        chemin, "--headless=new", "--disable-gpu", "--no-first-run", f"--remote-debugging-port={PORT_DEBOGAGE}",
        f"--user-data-dir={sortie / 'profil'}", "--window-size=1366,768", "about:blank",
    ])
    compte_rendu: dict[str, Any] = {}
    try:
        async with aiohttp.ClientSession() as http:
            cible = None
            for _ in range(40):
                try:
                    async with http.get(f"http://127.0.0.1:{PORT_DEBOGAGE}/json") as reponse:
                        cible = next(c for c in await reponse.json() if c["type"] == "page")
                    break
                except Exception:  # noqa: BLE001 — le navigateur démarre
                    await asyncio.sleep(0.5)
            if cible is None:
                print("le navigateur ne répond pas sur son port de débogage")
                return 2
            async with http.ws_connect(cible["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                page = Page(ws, sortie)
                for domaine in ("Page", "Runtime", "Log"):
                    await page.appel(f"{domaine}.enable")
                await page.appel("Page.navigate", url=f"{serveur.adresse}?jeton={serveur.jeton}")
                await page.jusqu_a("document.getElementById('pastilles') && document.getElementById('pastilles').children.length > 0")
                await page.attendre(2.0)
                await page.capture("ouverture.png")
                if "recalage" in scenarios:
                    compte_rendu["recalage"] = await scenario_recalage(page, serveur, donnees)
                if "diagonales" in scenarios:
                    compte_rendu["diagonales"] = await scenario_diagonales(page, serveur, donnees)
                if "nettoyage" in scenarios:
                    compte_rendu["nettoyage"] = await scenario_nettoyage(page, serveur, donnees)
                if "boutons" in scenarios:
                    compte_rendu["boutons"] = await scenario_boutons(page, serveur, donnees)
                compte_rendu["boites_de_dialogue"] = page.dialogues
                compte_rendu["erreurs_de_console"] = page.erreurs
    finally:
        edge.terminate()
        await serveur.arreter()
    (sortie / "compte-rendu.json").write_text(json.dumps(compte_rendu, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(compte_rendu, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    navigateur = None
    if "--navigateur" in arguments:
        rang = arguments.index("--navigateur")
        navigateur = arguments[rang + 1]
        del arguments[rang : rang + 2]
    if not arguments:
        raise SystemExit(__doc__)
    raise SystemExit(asyncio.run(principal(Path(arguments[0]).resolve(), arguments[1:] or ["recalage", "diagonales", "boutons"], navigateur)))
