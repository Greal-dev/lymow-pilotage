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
  var SYMBOLES = { haut: "↑", bas: "↓", gauche: "←", droite: "→" };

  /* Flèche et pas → l'étape, sous la forme des ordres de la page ; null si la flèche est inconnue. */
  /* `vitesse` : allure choisie (null = auto, l'outil choisit) ; `force` : garde-fou carte levé pour ce pas. */
  function ordreDe(direction, distance, angle, vitesse, force) {
    var pas = function (nom) {
      var etape = { nom: nom, valeur: distance, vitesse: vitesse === undefined ? null : vitesse, duree: null };
      if (force) { etape.force = true; }
      return etape;
    };
    if (direction === "haut") { return pas("avancer"); }
    if (direction === "bas") { return pas("reculer"); }
    if (direction === "gauche") { return { nom: "tourner", valeur: angle, vitesse: null, duree: null }; }
    if (direction === "droite") { return { nom: "tourner", valeur: -angle, vitesse: null, duree: null }; }
    return null;
  }

  function directionDe(ordre) {
    if (ordre.nom === "avancer") { return "haut"; }
    if (ordre.nom === "reculer") { return "bas"; }
    if (ordre.nom === "tourner") { return ordre.valeur < 0 ? "droite" : "gauche"; }
    return null;
  }

  /* « ↑ 1 m », « ← 45° » ; une étape qui n'est pas une flèche garde son nom. */
  function symbole(ordre) {
    if (typeof ordre === "string") { return ordre; } // étape d'un bilan : déjà en clair (« reculer 2 »)
    var direction = directionDe(ordre);
    if (!direction || typeof ordre.valeur !== "number") { return ordre.nom; }
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
    if (rapport.verification === "non vérifiable") { suite = "position non fournie"; }
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

  /* Échap : STOP dès qu'un mouvement est en cours. Hors mouvement, il désactive seulement les flèches. */
  function effetEchap(etat) {
    if (etat.occupe) { return "stop"; }
    return etat.flechesActives ? "desactiver" : "stop";
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
    if (DIRECTIONS[evenement.key]) { action = { type: "fleche", direction: DIRECTIONS[evenement.key] }; }
    else if (evenement.key === "Enter") { action = { type: "previsualiser" }; }
    else if (evenement.key === "Backspace") { action = { type: "annuler" }; }
    else if (evenement.key !== " " && evenement.key !== "Spacebar") { return { intercepter: false, action: null }; }
    return { intercepter: true, action: evenement.repeat ? null : action };
  }

  /* Une seule activation : pilotage direct par défaut, composition de séquence si la case est cochée. */
  function modeDesFleches(composer) { return composer ? "file" : "direct"; }

  var api = {
    ordreDe: ordreDe,
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
