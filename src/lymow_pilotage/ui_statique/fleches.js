/* Pilotage par flèches — logique pure, sans réseau ni document.
 *
 * Quatre flèches, quatre ordres : avancer, reculer, tourner à gauche, tourner à droite, aux pas
 * choisis dans deux listes. Ici : la traduction flèche → ordre, le tracé cumulé d'une suite
 * d'étapes (miniature), les totaux, la file d'attente du mode direct et le filtre du clavier.
 * Aucune règle de sécurité ne vit ici : le serveur local revalide tout.
 * Fichier testé hors navigateur (voir tests/test_fleches.py).
 */
(function (racine) {
  "use strict";

  var DIRECTIONS = { ArrowUp: "haut", ArrowDown: "bas", ArrowLeft: "gauche", ArrowRight: "droite" };
  var SYMBOLES = {
    haut: "↑", bas: "↓", gauche: "←", droite: "→",
    "haut-gauche": "↖", "haut-droite": "↗", "bas-gauche": "↙", "bas-droite": "↘"
  };
  /* Diagonales : avancer ou reculer en tournant (ordre « arc »). [signe des mètres, signe des degrés] — en
   * marche arrière, comme une voiture : « bas-gauche » envoie l'arrière à gauche, le cap tourne donc à droite. */
  var DIAGONALES = { "haut-gauche": [1, 1], "haut-droite": [1, -1], "bas-gauche": [-1, -1], "bas-droite": [-1, 1] };
  var PAVE_NUMERIQUE = { Numpad7: "haut-gauche", Numpad9: "haut-droite", Numpad1: "bas-gauche", Numpad3: "bas-droite" };

  /* Flèche et pas → l'étape, sous la forme des ordres de la page ; null si la flèche est inconnue. */
  /* `vitesse` : allure choisie (null = auto, l'outil choisit) ; `force` : garde-fou carte levé pour ce pas. */
  function ordreDe(direction, distance, angle, vitesse, force) {
    var pas = function (nom) {
      var etape = { nom: nom, valeur: distance, vitesse: vitesse === undefined ? null : vitesse, duree: null };
      if (force) { etape.force = true; }
      return etape;
    };
    if (DIAGONALES[direction]) {
      return {
        nom: "arc", valeur: DIAGONALES[direction][0] * distance, angle: DIAGONALES[direction][1] * angle,
        vitesse: vitesse === undefined ? null : vitesse, duree: null
      };
    }
    if (direction === "haut") { return pas("avancer"); }
    if (direction === "bas") { return pas("reculer"); }
    if (direction === "gauche") { return { nom: "tourner", valeur: angle, vitesse: null, duree: null }; }
    if (direction === "droite") { return { nom: "tourner", valeur: -angle, vitesse: null, duree: null }; }
    return null;
  }

  /* Allure lente : 0,10 m/s, ou la plus faible vitesse qui tient dans les 9 s d'un ordre (SEC-02). */
  function vitesseLente(distance, lente) {
    return Math.min(0.25, Math.max(lente, Math.ceil(distance / 9 * 100 - 1e-9) / 100));
  }

  function directionDe(ordre) {
    if (ordre.nom === "avancer") { return "haut"; }
    if (ordre.nom === "reculer") { return "bas"; }
    if (ordre.nom === "tourner") { return ordre.valeur < 0 ? "droite" : "gauche"; }
    if (ordre.nom === "arc" && typeof ordre.angle === "number") {
      var avant = ordre.valeur >= 0;
      // En marche arrière, l'arrière part du côté opposé au sens de rotation du cap.
      return (avant ? "haut-" : "bas-") + ((ordre.angle >= 0) === avant ? "gauche" : "droite");
    }
    return null;
  }

  /* « ↑ 1 m », « ← 45° » ; une étape qui n'est pas une flèche garde son nom. */
  function symbole(ordre) {
    if (typeof ordre === "string") { return ordre; } // étape d'un bilan : déjà en clair (« reculer 2 »)
    var direction = directionDe(ordre);
    if (!direction || typeof ordre.valeur !== "number") { return ordre.nom; }
    if (ordre.nom === "arc") { return SYMBOLES[direction] + " " + Math.abs(ordre.valeur) + " m / " + Math.abs(ordre.angle) + "°"; }
    return SYMBOLES[direction] + " " + Math.abs(ordre.valeur) + (ordre.nom === "tourner" ? "°" : " m");
  }

  /* Trajet cumulé : repère local, x vers la droite, y vers le haut, cap en degrés depuis l'axe x,
   * sens trigonométrique. Sans position connue : départ en (0, 0), cap vers le haut (90°).
   * Les étapes sans distance ni angle connus (durée seule, plateau…) ne déplacent pas le tracé. */
  function trace(etapes, depart, vitesses) {
    var x = depart ? depart.x : 0, y = depart ? depart.y : 0, cap = depart ? depart.cap : 90;
    var points = [{ x: x, y: y, cap: cap }];
    var distance = 0, rotation = 0, duree = 0;
    (etapes || []).forEach(function (ordre) {
      var valeur = typeof ordre.valeur === "number" ? ordre.valeur : null;
      if (valeur !== null && (ordre.nom === "avancer" || ordre.nom === "reculer")) {
        var sens = ordre.nom === "avancer" ? 1 : -1;
        x += sens * valeur * Math.cos(cap * Math.PI / 180);
        y += sens * valeur * Math.sin(cap * Math.PI / 180);
        distance += Math.abs(valeur);
        if (vitesses && vitesses.lineaire) { duree += Math.abs(valeur) / vitesses.lineaire; }
      } else if (valeur !== null && ordre.nom === "arc" && typeof ordre.angle === "number") {
        // Arc de cercle : parcouru par petits morceaux, pour que la miniature le trace courbe.
        var morceaux = 12, courbe = [];
        for (var m = 0; m < morceaux; m += 1) {
          cap += ordre.angle / morceaux / 2;
          x += valeur / morceaux * Math.cos(cap * Math.PI / 180);
          y += valeur / morceaux * Math.sin(cap * Math.PI / 180);
          cap += ordre.angle / morceaux / 2;
          if (m < morceaux - 1) { courbe.push({ x: x, y: y }); }
        }
        distance += Math.abs(valeur);
        rotation += Math.abs(ordre.angle);
        if (vitesses && vitesses.lineaire) { duree += Math.abs(valeur) / vitesses.lineaire; }
        points.push({ x: x, y: y, cap: cap, courbe: courbe });
        return;
      } else if (valeur !== null && ordre.nom === "tourner") {
        cap += valeur;
        rotation += Math.abs(valeur);
        if (vitesses && vitesses.angulaire) { duree += Math.abs(valeur) * Math.PI / 180 / vitesses.angulaire; }
      } else if (typeof ordre.duree === "number") {
        duree += ordre.duree;
      }
      points.push({ x: x, y: y, cap: cap });
    });
    return {
      points: points,
      distance_m: Math.round(distance * 100) / 100,
      rotation_deg: Math.round(rotation * 10) / 10,
      duree_s: Math.round(duree * 10) / 10
    };
  }

  function texteTotal(total, nombre) {
    return nombre + " étape" + (nombre > 1 ? "s" : "") + " — " + total.distance_m + " m parcourus, "
      + total.rotation_deg + "° de rotation, environ " + Math.round(total.duree_s) + " s de mouvement";
  }

  /* File d'attente du mode direct : cinq au plus, l'appui en trop est ignoré (et signalé). */
  function empiler(file, ordre, maximum) {
    if (file.length >= maximum) { return { file: file.slice(), accepte: false }; }
    return { file: file.concat([ordre]), accepte: true };
  }

  function annulerDerniere(etapes) {
    return etapes.slice(0, Math.max(0, etapes.length - 1));
  }

  /* Clavier : ce que vaut un appui. Rien si le pavé n'a pas le focus, si la touche est maintenue
   * (`repeat`), ou pendant la saisie dans un champ ou une liste. */
  function actionClavier(evenement) {
    if (!evenement.dansLePave || evenement.actives === false || evenement.repeat) { return null; }
    var balise = String(evenement.balise || "").toUpperCase();
    if (balise === "INPUT" || balise === "SELECT" || balise === "TEXTAREA") { return null; }
    if (DIRECTIONS[evenement.key]) { return { type: "fleche", direction: DIRECTIONS[evenement.key] }; }
    if (evenement.key === "Enter") { return { type: "previsualiser" }; }
    if (evenement.key === "Backspace") { return { type: "annuler" }; }
    return null;
  }

  /* Les flèches ne font pas défiler la page quand le pavé a le focus, même maintenues. */
  function retientLaTouche(evenement) {
    var balise = String(evenement.balise || "").toUpperCase();
    if (!evenement.dansLePave || evenement.actives === false || balise === "INPUT" || balise === "SELECT" || balise === "TEXTAREA") { return false; }
    return Boolean(DIRECTIONS[evenement.key]) || evenement.key === "Backspace";
  }

  function metres(valeur) { return "±" + valeur.toFixed(2).replace(".", ",") + " m"; }

  /* Aide à retrouver le signal : la précision de position évolue-t-elle dans le bon sens ? */
  function tendance(avant, apres) {
    if (typeof avant !== "number" || typeof apres !== "number") { return ""; }
    if (Math.abs(apres - avant) < 0.05) { return "→ stable"; }
    return apres < avant ? "↘ en amélioration" : "↗ en dégradation";
  }

  function textePrecision(rapport) {
    if (!rapport || typeof rapport.precision_m !== "number") { return ""; }
    var sens = tendance(rapport.precision_avant_m, rapport.precision_m);
    return " — position " + metres(rapport.precision_m) + (sens ? " " + sens : "");
  }

  /* Retour court après une étape : « ↑ 1 m : émise, arrêt acquitté — position non fournie ». */
  function ligneRetour(ordre, rapport) {
    var debut = symbole(ordre) + " : ";
    if (!rapport) { return debut + "en attente"; }
    if (rapport.echec) { return debut + "refusée : " + rapport.echec; }
    if (rapport.simulee) { return debut + "simulée, rien n'est émis"; }
    var suite = rapport.constat || "";
    if (rapport.verification === "non vérifiable") { suite = rapport.cause_probable || "position non fournie"; }
    // Pose inchangée : la cause probable selon le statut (« en erreur : ordre ignoré », « patinage possible »).
    else if (rapport.cause_probable) { suite = "le robot n'a pas bougé — " + rapport.cause_probable; }
    if (rapport.verification === "vérifiée") { suite = "déplacement constaté"; }
    return debut + "émise, arrêt acquitté" + (suite ? " — " + suite : "") + textePrecision(rapport);
  }

  /* Bascule unique des flèches : un seul état pour les boutons sur la vidéo ET le clavier. */
  function basculer(actives, evenement) {
    if (evenement === "bouton") { return !actives; }
    if (evenement === "clic-video" || evenement === "armement") { return true; }
    if (evenement === "sortie" || evenement === "echap") { return false; }
    return actives;
  }

  function libelleBascule(actives) {
    return actives ? "Flèches : ACTIVES — désactiver" : "Flèches : activer";
  }

  /* Échap = STOP complet, toujours, comme Espace (revue du 2026-10-12 : la page peut croire qu'aucun ordre
   * n'est en cours alors qu'un pas vient de partir). Retirer seulement les flèches passe par leur bouton. */
  function effetEchap() {
    return "stop";
  }

  /* Deux flèches → la diagonale, ou rien si elles ne se combinent pas (haut + bas, gauche + droite). */
  function diagonale(a, b) {
    var vertical = a === "haut" || a === "bas" ? a : (b === "haut" || b === "bas" ? b : null);
    var lateral = a === "gauche" || a === "droite" ? a : (b === "gauche" || b === "droite" ? b : null);
    return vertical && lateral && a !== b ? vertical + "-" + lateral : null;
  }

  /* Combinaisons de flèches au clavier. Une flèche seule attend 120 ms avant de partir : si une seconde,
   * combinable, arrive dans les 150 ms, c'est la diagonale qui part — jamais un pas droit d'abord. Une
   * flèche pressée pendant qu'une autre est tenue donne aussi la diagonale. Rend le nouvel état, ce qu'il
   * faut émettre, et le délai à armer (0 : aucun). */
  var DELAI_FLECHE_SEULE_MS = 120, FENETRE_COMBINAISON_MS = 150;
  function combiner(etat, e) {
    var tenues = (etat.tenues || []).slice(), attente = etat.attente || null, depuis = etat.depuis || 0, emettre = [];
    if (e.type === "relache") {
      tenues = tenues.filter(function (d) { return d !== e.direction; });
      return { etat: { attente: attente, depuis: depuis, tenues: tenues }, emettre: [], attendreMs: 0 };
    }
    if (e.type === "echeance") {
      if (attente) { emettre.push(attente); }
      return { etat: { attente: null, depuis: 0, tenues: tenues }, emettre: emettre, attendreMs: 0 };
    }
    var autres = tenues.filter(function (d) { return d !== e.direction; });
    if (tenues.indexOf(e.direction) === -1) { tenues.push(e.direction); }
    var combinee = attente && e.t - depuis <= FENETRE_COMBINAISON_MS ? diagonale(attente, e.direction) : null;
    if (combinee) { return { etat: { attente: null, depuis: 0, tenues: tenues }, emettre: [combinee], attendreMs: 0 }; }
    if (attente) { emettre.push(attente); }  // elle ne se combine pas : elle part, telle quelle
    var avecTenue = attente ? null : autres.map(function (d) { return diagonale(d, e.direction); }).filter(Boolean)[0];
    if (avecTenue) { return { etat: { attente: null, depuis: 0, tenues: tenues }, emettre: emettre.concat([avecTenue]), attendreMs: 0 }; }
    return { etat: { attente: e.direction, depuis: e.t, tenues: tenues }, emettre: emettre, attendreMs: DELAI_FLECHE_SEULE_MS };
  }

  /* Clavier, flèches actives : la touche fait bouger le robot, PAS la page (constat du 2026-10-12 : la
   * page défilait). L'interception vaut quel que soit l'élément qui a le focus — `body`, un bouton — et
   * jamais dans un champ de saisie, une liste déroulante ou une zone éditable. Une touche maintenue
   * (`repeat`) est retenue mais ne compte pas. Espace reste STOP, traité ailleurs. */
  function interception(evenement) {
    var balise = String(evenement.balise || "").toUpperCase();
    var saisie = balise === "INPUT" || balise === "SELECT" || balise === "TEXTAREA" || Boolean(evenement.editable);
    if (!evenement.actives || saisie) { return { intercepter: false, action: null }; }
    var action = null;
    // Pavé numérique 7, 9, 1, 3 : les quatre diagonales, d'une touche (reconnues par leur position, pas par leur chiffre).
    if (PAVE_NUMERIQUE[evenement.code]) { action = { type: "fleche", direction: PAVE_NUMERIQUE[evenement.code], diagonale: true }; }
    else if (DIRECTIONS[evenement.key]) { action = { type: "fleche", direction: DIRECTIONS[evenement.key] }; }
    else if (evenement.key === "Enter") { action = { type: "previsualiser" }; }
    else if (evenement.key === "Backspace") { action = { type: "annuler" }; }
    else if (evenement.key !== " " && evenement.key !== "Spacebar") { return { intercepter: false, action: null }; }
    return { intercepter: true, action: evenement.repeat ? null : action };
  }

  /* Une seule activation : pilotage direct par défaut, composition de séquence si la case est cochée. */
  function modeDesFleches(composer) { return composer ? "file" : "direct"; }

  var api = {
    ordreDe: ordreDe,
    diagonale: diagonale,
    combiner: combiner,
    DIRECTIONS: DIRECTIONS,
    vitesseLente: vitesseLente,
    basculer: basculer,
    libelleBascule: libelleBascule,
    effetEchap: effetEchap,
    interception: interception,
    modeDesFleches: modeDesFleches,
    directionDe: directionDe,
    symbole: symbole,
    trace: trace,
    texteTotal: texteTotal,
    empiler: empiler,
    annulerDerniere: annulerDerniere,
    actionClavier: actionClavier,
    retientLaTouche: retientLaTouche,
    ligneRetour: ligneRetour,
    tendance: tendance
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    racine.LymowFleches = api;
  }
})(typeof self !== "undefined" ? self : this);
