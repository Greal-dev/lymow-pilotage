/* Panneau « Pilotage par flèches » : écrire vite une suite d'étapes, pas piloter à vue.
 * Mode file (défaut) : chaque flèche AJOUTE une étape à LA séquence de la page (une seule liste) ;
 * Entrée la prévisualise, « Émettre » l'émet — les deux temps habituels, sans contournement.
 * Mode direct (armé explicitement) : chaque flèche part tout de suite, par une route du serveur
 * qui n'accepte que ces quatre ordres bornés. Aucune règle de sécurité ne vit ici. */
(function () {
  "use strict";

  var page = window.LymowPage;
  var Fleches = window.LymowFleches;
  var panneau = document.getElementById("bloc-fleches");
  var miniature = document.getElementById("pave-miniature");
  var NS = miniature.namespaceURI;
  var config = { distances_m: [1], angles_deg: [45], distance_defaut_m: 1, angle_defaut_deg: 45, file_max: 5, armement_s: 60 };
  var vitesses = { lineaire: 0.25, angulaire: 0.35 };
  var jeton = null; // armement du mode direct, renouvelé par le serveur à chaque action
  var direct = { arme: false, reste_s: 0, file: [], faites: [] };
  var resultat = null;
  var dernierResultat = "";
  var faitesEnFile = 0; // étapes de la séquence déjà émises (marquées dans la miniature)
  var cap = null;
  var ordresDuRobot = null;
  var actives = false; // LA bascule : boutons sur la caméra et clavier, ensemble
  var modeSimulation = false;
  var dernierBord = null;
  function afficherAlerteBord() {
    $("bord-zone").classList.toggle("alerte-carte", Ordres.alerteBord(dernierBord, Number($("pave-distance").value)));
  }
  var allures = { auto: null, lente: 0.10, normale: 0.25 };
  var Ordres = window.LymowOrdres;
  var prise = null; // « Prendre la main » en cours
  var mainPrise = false;
  var suspendue = false;
  var connecte = false;
  var occupeCourant = false;

  function $(id) { return document.getElementById(id); }

  function signaler(texte) { $("pave-signal").textContent = texte; }

  function remplir(liste, valeurs, defaut, unite) {
    page.vider(liste);
    valeurs.forEach(function (valeur) {
      var option = page.element("option", String(valeur).replace(".", ",") + unite);
      option.value = String(valeur);
      option.selected = valeur === defaut;
      liste.appendChild(option);
    });
  }

  function etapeDe(direction) {
    var distance = Number($("pave-distance").value);
    var vitesse = allures[$("pave-allure").value];
    // Allure lente : la plus faible vitesse qui tient dans les 9 s d'un ordre (le serveur fait de même).
    if ($("pave-allure").value === "lente") { vitesse = Fleches.vitesseLente(distance, vitesse); }
    return Fleches.ordreDe(direction, distance, Number($("pave-angle").value), vitesse, forcerCePas(direction));
  }

  // --- miniature : trajet cumulé, étapes faites puis étapes à venir --------------

  function trait(classe, a, b) {
    var ligne = document.createElementNS(NS, "line");
    ligne.setAttribute("class", classe);
    ligne.setAttribute("x1", a.x); ligne.setAttribute("y1", -a.y);
    ligne.setAttribute("x2", b.x); ligne.setAttribute("y2", -b.y);
    return ligne;
  }

  function dessiner() {
    var faites = direct.arme || direct.faites.length ? direct.faites.map(function (f) { return f.ordre; }) : [];
    var aVenir = direct.arme ? direct.file : page.sequence();
    var dejaFaites = direct.arme ? faites.length : faitesEnFile;
    var toutes = direct.arme ? faites.concat(aVenir) : aVenir;
    var depart = { x: 0, y: 0, cap: cap === null ? 90 : cap };
    var total = Fleches.trace(toutes, depart, vitesses);
    var points = total.points;
    page.vider(miniature);
    var xs = points.map(function (p) { return p.x; }), ys = points.map(function (p) { return -p.y; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    var marge = Math.max(maxX - minX, maxY - minY, 1) * 0.15 + 0.3;
    miniature.setAttribute("viewBox", [minX - marge, minY - marge, maxX - minX + 2 * marge, maxY - minY + 2 * marge].join(" "));
    afficherPastilles(
      direct.arme ? faites : aVenir.slice(0, dejaFaites), direct.arme ? aVenir : aVenir.slice(dejaFaites)
    );
    for (var i = 1; i < points.length; i += 1) {
      miniature.appendChild(trait(i <= dejaFaites ? "fait" : "prevu", points[i - 1], points[i]));
    }
    var taille = marge * 0.35;
    var origine = document.createElementNS(NS, "circle");
    origine.setAttribute("class", "depart");
    origine.setAttribute("cx", points[0].x); origine.setAttribute("cy", -points[0].y); origine.setAttribute("r", taille * 0.6);
    miniature.appendChild(origine);
    var fin = points[points.length - 1];
    var pointe = document.createElementNS(NS, "polygon");
    pointe.setAttribute("class", "arrivee");
    pointe.setAttribute("points", [[1.6, 0], [-1, 0.9], [-1, -0.9]].map(function (p) { return p[0] * taille + "," + p[1] * taille; }).join(" "));
    pointe.setAttribute("transform", "translate(" + fin.x + " " + (-fin.y) + ") rotate(" + (-fin.cap) + ")");
    miniature.appendChild(pointe);
    var enTete = cap === null ? "Départ : repère local (cap du robot inconnu, tracé vers le haut). " : "Départ : cap courant du robot. ";
    $("pave-total").textContent = Fleches.texteTotal(Fleches.trace(aVenir, depart, vitesses), aVenir.length)
      + (direct.arme ? " en attente" : "");
    $("pave-total").title = enTete;
  }

  function enDirect() { return Fleches.modeDesFleches($("composer").checked) === "direct"; }

  /* Bascule unique : un seul état pour les boutons sur la caméra ET le clavier, visible (liseré, libellé). */
  function regler(nouveau) {
    actives = nouveau;
    panneau.classList.toggle("actif", actives);
    document.getElementById("vue-camera").classList.toggle("actif", actives);
    document.querySelectorAll("[data-fleche]").forEach(function (bouton) { bouton.disabled = !actives; });
    $("fleches-bascule").textContent = Fleches.libelleBascule(actives);
    $("fleches-bascule").setAttribute("aria-pressed", String(actives));
    $("pave-focus").textContent = !actives
      ? "Flèches inactives. Activées, elles font bouger le robot tout de suite (ou composent une séquence, case cochée)."
      : enDirect()
        ? "Flèches ACTIVES — pilotage direct : ↑ ↓ ← → font bouger le robot. Échap ou Espace : STOP."
        : "Flèches ACTIVES — composition : ↑ ↓ ← → ajoutent une étape, Entrée prévisualise, Retour arrière annule.";
  }

  async function desactiver(motif) {
    var armees = Boolean(jeton);
    jeton = null;
    regler(false);
    if (motif) { signaler(motif); }
    if (armees) { try { await page.appeler("/api/direct/desarmer"); } catch (erreur) { signaler(erreur.message); } }
  }

  /* Active les flèches (arme le pilotage direct, sauf en composition). La confirmation a déjà été donnée
   * par « Prendre la main » : une seule pour tout le geste. */
  async function activer() {
    if (!enDirect()) { regler(true); signaler(""); return; }
    try {
      jeton = (await page.appeler("/api/direct/armer", { confirmer: true })).jeton;
      regler(true);
      signaler(modeSimulation ? "Simulation : les flèches parcourent le même chemin, rien n'est émis vers le robot." : "");
    } catch (erreur) {
      jeton = null;
      signaler("Activation refusée : " + erreur.message);
    }
  }

  function contexte() {
    return {
      suspendue: suspendue, connecte: connecte, occupe: occupeCourant, ordres: ordresDuRobot, flechesActives: actives,
      composer: $("composer").checked, simulation: modeSimulation, pauseDemandee: Boolean(prise && prise.pauseDemandee),
      confirme: Boolean(prise && prise.confirme)
    };
  }

  /* « Prendre la main » : UN geste, UNE confirmation — liaison rétablie, robot mis en pause s'il tond ou
   * rentre (et pause attendue), puis flèches actives. Chaque pas attend l'état annoncé par le serveur. */
  function prendreLaMain() {
    if (prise) { return; }
    if (occupeCourant) { signaler("Un ordre est en cours : attendez sa fin, ou STOP."); return; }
    // Le focus ne reste pas sur les boutons du haut : une touche n'y activerait rien par mégarde.
    if (document.activeElement && document.activeElement.blur) { document.activeElement.blur(); }
    prise = { confirme: false, pauseDemandee: false, enCours: false, depuisSuspension: suspendue, echeance: Date.now() + 60000 };
    poursuivrePrise();
  }

  /* Main rendue : la liaison est rétablie et l'état relu AVANT la confirmation, qui dit alors ce qui va vraiment
   * se passer (« la tâche sera mise en pause »). Sinon la confirmation vient tout de suite, dans le même geste. */
  async function poursuivrePrise() {
    if (!prise || prise.enCours) { return; }
    var suite = Ordres.suitePrendreLaMain(contexte());
    if (suite === "attendre" && Date.now() < prise.echeance) { return; }
    var courante = prise;
    if (suite === "confirmer") {
      if (!window.confirm(Ordres.annoncePrendreLaMain(contexte()))) {
        prise = null;
        signaler(courante.depuisSuspension ? "Liaison rétablie ; les flèches ne sont pas activées." : "");
        return;
      }
      courante.confirme = true;
      mainPrise = true;
      suite = Ordres.suitePrendreLaMain(contexte());
      if (suite === "attendre") { return; }
    }
    courante.enCours = true;
    try {
      if (suite === "liaison") {
        signaler("Liaison en cours de rétablissement…");
        await page.appeler("/api/liaison/reprendre");
        suspendue = false;
      } else if (suite === "pause") {
        courante.pauseDemandee = true;
        await page.appeler("/api/pause", { confirmer: true });
      } else if (suite === "fleches") {
        await activer();
        prise = null;
      } else if (suite === "fait") {
        prise = null;
      } else {  // abandon, blocage ou délai dépassé : dit en clair, rien d'autre n'est tenté
        prise = null;
        signaler(suite === "bloque"
          ? "Le robot n'est pas pilotable dans cet état : " + ((ordresDuRobot && ordresDuRobot.mouvement_refuse) || "statut inconnu")
          : suite === "abandon" ? "Pause non confirmée par le robot : les flèches ne sont pas activées."
            : "Prise en main interrompue : le robot ne répond pas comme attendu.");
      }
    } catch (erreur) {
      prise = null;
      signaler("Prise en main interrompue : " + erreur.message);
    } finally {
      courante.enCours = false;
    }
  }

  function afficherPastilles(faites, aVenir) {
    var liste = $("pave-pastilles");
    page.vider(liste);
    faites.forEach(function (ordre) { liste.appendChild(page.element("li", Fleches.symbole(ordre), "faite")); });
    aVenir.forEach(function (ordre) { liste.appendChild(page.element("li", Fleches.symbole(ordre))); });
    if (!faites.length && !aVenir.length) { liste.appendChild(page.element("li", "séquence vide", "vide")); }
  }

  function afficherRetours() {
    var liste = $("pave-retours");
    page.vider(liste);
    var lignes = [];
    if (direct.arme || direct.faites.length) {
      lignes = direct.faites.map(function (f) { return Fleches.ligneRetour(f.ordre, f.rapport); });
    } else if (resultat && resultat.etapes) {
      lignes = resultat.etapes.map(function (etape) { return Fleches.ligneRetour(etape.ordre, etape); });
    }
    lignes.slice(-2).forEach(function (ligne) { liste.appendChild(page.element("li", ligne)); });
    $("pave-file").textContent = direct.arme
      ? "File d'attente (" + direct.file.length + "/" + config.file_max + ") : "
        + (direct.file.map(Fleches.symbole).join(", ") || "vide")
      : "";
  }

  function afficherDirect() {
    $("bandeau-direct").hidden = !direct.arme;
    $("direct-etat").textContent = direct.arme
      ? "Pilotage direct : désactivation automatique dans " + direct.reste_s + " s sans action."
      : "";
    var composition = !enDirect();
    var vide = page.sequence().length === 0;
    // La séquence ne se manie qu'en composition ; hors composition, ces boutons disent pourquoi ils sont inactifs.
    $("pave-previsualiser").disabled = !composition || vide;
    $("pave-annuler").disabled = !composition || vide;
    $("pave-vider").disabled = vide;
    $("pave-boutons-note").textContent = composition || vide
      ? ""
      : "Séquence en attente : cochez « Composer une séquence » pour la prévisualiser.";
  }

  function rafraichir() {
    afficherDirect();
    afficherRetours();
    dessiner();
  }

  // --- actions -------------------------------------------------------------------

  /* « Forcer ce pas » : une confirmation, pour UN pas d'avance ou de recul ; la case se décoche aussitôt. */
  function forcerCePas(direction) {
    if ((direction !== "haut" && direction !== "bas") || !$("pave-forcer").checked) { return false; }
    $("pave-forcer").checked = false;
    return window.confirm("Forcer ce pas ? Le garde-fou carte ne le raccourcira pas et ne le refusera pas, même s'il sort "
      + "de la zone ou entre dans une zone interdite. À faire robot à vue.");
  }

  async function flecheDirecte(direction) {
    var lineaire = direction === "haut" || direction === "bas";
    var pas = lineaire ? Number($("pave-distance").value) : Number($("pave-angle").value);
    var demande = { jeton: jeton, direction: direction, pas: pas };
    if (lineaire) { demande.allure = $("pave-allure").value; demande.forcer = forcerCePas(direction); }
    try {
      var reponse = await page.appeler("/api/direct/fleche", demande);
      jeton = reponse.jeton;
      signaler([reponse.allure, reponse.simulation ? "Simulation : étape parcourue, rien n'est émis." : ""].filter(Boolean).join(" — "));
    } catch (erreur) {
      signaler(erreur.message); // file pleine : appui ignoré ; tout autre refus désarme côté serveur
    }
  }

  function fleche(direction) {
    if (!actives) { return; }
    var blocage = window.LymowOrdres.blocageDesFleches(ordresDuRobot);
    if (blocage) { signaler(blocage); return; }
    if (enDirect()) {
      if (jeton) { flecheDirecte(direction); }
      return;
    }
    var etape = etapeDe(direction);
    if (!etape) { return; }
    page.ajouterEtape(etape);
    signaler("");
  }

  // --- branchements --------------------------------------------------------------

  document.querySelectorAll("[data-fleche]").forEach(function (bouton) {
    bouton.addEventListener("click", function () { fleche(bouton.dataset.fleche); });
  });
  $("pave-plateau").addEventListener("click", function () {
    // En composition : une étape de plus ; sinon : prévisualisé, puis « Émettre », comme tout ordre.
    if (enDirect()) { page.previsualiser([{ nom: "plateau-haut" }]); } else { page.ajouterEtape({ nom: "plateau-haut" }); }
  });
  $("pave-annuler").addEventListener("click", page.annulerDerniere);
  $("pave-vider").addEventListener("click", page.viderSequence);
  $("pave-previsualiser").addEventListener("click", page.previsualiserSequence);
  // « Flèches : activer » est le reflet de « Prendre la main » : le même geste.
  $("fleches-bascule").addEventListener("click", function () { if (actives) { desactiver(""); } else { prendreLaMain(); } });
  $("prendre-main").addEventListener("click", prendreLaMain);
  $("pave-distance").addEventListener("change", afficherAlerteBord);  // l'alerte dépend du pas choisi
  document.getElementById("vue-camera").addEventListener("click", function (evenement) {
    if (!actives && !evenement.target.closest("button")) { prendreLaMain(); }
  });
  $("composer").addEventListener("change", function () {
    if (actives) { desactiver("Mode changé : réactivez les flèches."); }
    rafraichir();
  });

  // Clavier : capté au niveau du document, en phase de capture, quel que soit l'élément qui a le focus.
  // Flèches actives, ↑ ↓ ← → Entrée et Retour arrière ne font JAMAIS défiler ni agir la page.
  document.addEventListener("keydown", function (evenement) {
    var cible = evenement.target || {};
    var decision = Fleches.interception({
      key: evenement.key, repeat: evenement.repeat, balise: cible.tagName, editable: cible.isContentEditable, actives: actives
    });
    if (!decision.intercepter) { return; }
    if (evenement.key === " " || evenement.key === "Spacebar") { return; } // STOP : traité par la page, qui retient aussi la touche
    evenement.preventDefault();
    evenement.stopPropagation();
    var action = decision.action;
    if (!action) { return; }
    if (action.type === "fleche") { fleche(action.direction); }
    if (action.type === "annuler" && !enDirect()) { page.annulerDerniere(); }
    if (action.type === "previsualiser" && !enDirect()) { page.previsualiserSequence(); }
  }, true);
  window.addEventListener("pagehide", function () {
    if (direct.arme && navigator.sendBeacon) { navigator.sendBeacon("/api/direct/desarmer"); }
  });

  window.LymowPave = {
    configurer: function (fleches, bornes) {
      if (fleches) { config = fleches; }
      if (bornes && bornes.vitesse_lineaire_max) {
        vitesses = { lineaire: bornes.vitesse_lineaire_max, angulaire: bornes.vitesse_angulaire_max };
      }
      remplir($("pave-distance"), config.distances_m, config.distance_defaut_m, " m");
      remplir($("pave-angle"), config.angles_deg, config.angle_defaut_deg, "°");
      if (config.allures) { allures = config.allures; }
      rafraichir();
    },
    appliquer: function (image) {
      direct = image.direct || direct;
      ordresDuRobot = image.ordres || null;
      modeSimulation = image.mode === "simulation";
      suspendue = Boolean(image.liaison_suspendue);
      $("bord-zone").textContent = image.ordres && image.ordres.bord ? image.ordres.bord.texte : "";  // SEC-13
      dernierBord = image.ordres ? image.ordres.bord : null;
      afficherAlerteBord();
      $("garde-carte").checked = Boolean(image.arret_aux_bords);
      $("pave-forcer-libelle").hidden = !image.arret_aux_bords;  // « Forcer ce pas » n'a de sens que garde-fou actif
      if (!image.arret_aux_bords) { $("pave-forcer").checked = false; }
      // Robot en erreur : dit AVANT le pas ; pas sans déplacement : dit, la main est gardée.
      $("pave-avertissement").textContent = [
        image.ordres && image.ordres.avertissement_pas,
        image.ordres && image.ordres.immobiles ? image.ordres.immobiles + " pas de suite sans déplacement constaté (désarmement au troisième)." : ""
      ].filter(Boolean).join(" ");
      connecte = Boolean(image.connecte);
      occupeCourant = Boolean(image.occupe);
      if (suspendue) { mainPrise = false; }
      var qui = Ordres.quiALaMain({ suspendue: suspendue, mainPrise: mainPrise, flechesActives: actives });
      $("etat-main").textContent = Ordres.texteDeLaMain(qui);
      $("prendre-main").classList.toggle("en-avant", qui !== "outil");
      poursuivrePrise();
      if (!direct.arme && jeton) {  // expiré, STOP, refus ou échec : le serveur a désarmé — l'activation tombe
        jeton = null;
        regler(false);
        signaler("Flèches désactivées : pilotage direct désarmé (60 s sans action, STOP ou refus). Réactivez-les.");
      }
      cap = image.etat && typeof image.etat.cap_deg === "number" ? image.etat.cap_deg : null;
      var texte = JSON.stringify(image.resultat);
      if (texte !== dernierResultat) {
        dernierResultat = texte;
        resultat = image.resultat;
        var etapes = resultat && resultat.etapes ? resultat.etapes : [];
        var restantes = resultat && resultat.reprise && resultat.reprise.ordres ? resultat.reprise.ordres.length : 0;
        var emises = etapes.filter(function (etape) { return !etape.echec; }).length;
        // Le résultat est celui de la séquence affichée si les comptes concordent.
        faitesEnFile = etapes.length && emises + restantes === page.sequence().length ? emises : 0;
      }
      rafraichir();
    },
    /* « Rendre la main » et STOP : les flèches tombent ; « la main » n'est retirée que par « Rendre la main ». */
    flechesActives: function () { return actives; },
    desactiverFleches: function (rendue) {
      prise = null;
      if (rendue) { mainPrise = false; }
      return actives ? desactiver("") : Promise.resolve();
    },
    sequenceChangee: function () {
      faitesEnFile = 0;
      rafraichir();
    }
  };
  regler(false);
})();
