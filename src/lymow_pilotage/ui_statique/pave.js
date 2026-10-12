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
    return Fleches.ordreDe(direction, Number($("pave-distance").value), Number($("pave-angle").value));
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
    $("pave-total").textContent = enTete + Fleches.texteTotal(Fleches.trace(aVenir, depart, vitesses), aVenir.length)
      + (direct.arme ? " en attente" : " dans la séquence");
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
    lignes.slice(-6).forEach(function (ligne) { liste.appendChild(page.element("li", ligne)); });
    $("pave-file").textContent = direct.arme
      ? "File d'attente (" + direct.file.length + "/" + config.file_max + ") : "
        + (direct.file.map(Fleches.symbole).join(", ") || "vide")
      : "";
  }

  function afficherDirect() {
    $("bandeau-direct").hidden = !direct.arme;
    $("direct-interrupteur").checked = direct.arme;
    $("direct-etat").textContent = direct.arme
      ? "ARMÉ : chaque flèche émet tout de suite. Désarmement automatique dans " + direct.reste_s + " s sans action."
      : "désarmé : les flèches ajoutent des étapes à la séquence.";
    // Hors des quatre flèches, rien ne part en direct : ces boutons le disent quand ils sont inactifs.
    $("pave-plateau").disabled = direct.arme;
    $("pave-previsualiser").disabled = direct.arme || page.sequence().length === 0;
    $("pave-annuler").disabled = direct.arme || page.sequence().length === 0;
    $("pave-vider").disabled = direct.arme || page.sequence().length === 0;
    $("pave-boutons-note").textContent = direct.arme
      ? "Pilotage direct armé : seules les quatre flèches agissent (ni plateau, ni séquence)."
      : (page.sequence().length === 0 ? "Séquence vide : ajoutez une étape avec une flèche." : "");
  }

  function rafraichir() {
    afficherDirect();
    afficherRetours();
    dessiner();
  }

  // --- actions -------------------------------------------------------------------

  async function flecheDirecte(direction) {
    var pas = direction === "haut" || direction === "bas" ? Number($("pave-distance").value) : Number($("pave-angle").value);
    try {
      var reponse = await page.appeler("/api/direct/fleche", { jeton: jeton, direction: direction, pas: pas });
      jeton = reponse.jeton;
      signaler(reponse.simulation ? "Simulation : étape parcourue, rien n'est émis." : "");
    } catch (erreur) {
      signaler(erreur.message); // file pleine : appui ignoré ; tout autre refus désarme côté serveur
    }
  }

  function fleche(direction) {
    if (direct.arme && jeton) { flecheDirecte(direction); return; }
    var etape = etapeDe(direction);
    if (!etape) { return; }
    page.ajouterEtape(etape);
    signaler("");
  }

  async function basculerDirect() {
    if (!$("direct-interrupteur").checked) {
      jeton = null;
      try { await page.appeler("/api/direct/desarmer"); } catch (erreur) { signaler(erreur.message); }
      return;
    }
    var accord = window.confirm(
      "Armer le pilotage direct ?\n\nChaque flèche émettra IMMÉDIATEMENT son étape, sans prévisualisation. "
      + "STOP : Échap ou Espace. Désarmement automatique après " + config.armement_s + " s sans action."
    );
    if (!accord) { $("direct-interrupteur").checked = false; return; }
    try {
      jeton = (await page.appeler("/api/direct/armer", { confirmer: true })).jeton;
      panneau.focus();
    } catch (erreur) {
      jeton = null;
      $("direct-interrupteur").checked = false;
      signaler("Armement refusé : " + erreur.message);
    }
  }

  // --- branchements --------------------------------------------------------------

  panneau.querySelectorAll("[data-fleche]").forEach(function (bouton) {
    bouton.addEventListener("click", function () { fleche(bouton.dataset.fleche); });
  });
  $("pave-plateau").addEventListener("click", function () { page.ajouterEtape({ nom: "plateau-haut" }); });
  $("pave-annuler").addEventListener("click", page.annulerDerniere);
  $("pave-vider").addEventListener("click", page.viderSequence);
  $("pave-previsualiser").addEventListener("click", page.previsualiserSequence);
  $("pave-clavier").addEventListener("click", function () { panneau.focus(); });
  $("direct-interrupteur").addEventListener("change", basculerDirect);
  ["pave-distance", "pave-angle"].forEach(function (id) { $(id).addEventListener("change", function () { panneau.focus(); }); });

  // Le clavier n'agit que lorsque le panneau a le focus ; un clic dans le panneau le lui donne.
  panneau.addEventListener("click", function (evenement) {
    if (!/^(INPUT|SELECT|BUTTON|LABEL)$/.test(evenement.target.tagName)) { panneau.focus(); }
  });
  panneau.addEventListener("keydown", function (evenement) {
    var touche = { key: evenement.key, repeat: evenement.repeat, balise: evenement.target.tagName, dansLePave: true };
    if (Fleches.retientLaTouche(touche)) { evenement.preventDefault(); } // la page ne défile pas
    var action = Fleches.actionClavier(touche);
    if (!action) { return; }
    evenement.preventDefault();
    if (action.type === "fleche") { fleche(action.direction); }
    if (action.type === "annuler" && !direct.arme) { page.annulerDerniere(); }
    if (action.type === "previsualiser" && !direct.arme) { page.previsualiserSequence(); }
  });
  function focusChange() {
    var actif = panneau.contains(document.activeElement);
    $("pave-focus").textContent = actif
      ? "Clavier ACTIF : ↑ ↓ ← →, Entrée = prévisualiser, Retour arrière = annuler la dernière."
      : "Clavier inactif : cliquez dans ce panneau ou sur « Activer le clavier ».";
  }
  panneau.addEventListener("focusin", focusChange);
  panneau.addEventListener("focusout", function () { window.setTimeout(focusChange, 0); });
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
      rafraichir();
    },
    appliquer: function (image) {
      direct = image.direct || direct;
      if (!direct.arme) { jeton = null; } // expiré, STOP, refus ou échec : le serveur a désarmé
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
    sequenceChangee: function () {
      faitesEnFile = 0;
      rafraichir();
    }
  };
  focusChange();
})();
