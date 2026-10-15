/* Tableau de bord compact : pastilles d'état, pas et allure en boutons, fil d'activité, file en pastilles.
 * Rien ici ne décide ni n'émet : ce fichier met en forme ce que le serveur annonce et ce que les autres
 * fichiers de la page disent (retour du propriétaire, 2026-10-12 : « trop de légendes, pas assez de boutons »). */
(function () {
  "use strict";

  var Ordres = window.LymowOrdres;
  var Fleches = window.LymowFleches;
  var page = window.LymowPage;
  var lignes = [];            // fil d'activité, la plus récente en tête
  var dernierJournal = "";    // dernière note du serveur déjà reprise
  var dernierRetour = "";     // dernier retour de pas déjà repris
  var erreurOuverte = false;  // la pastille d'erreur ouvre et ferme le diagnostic
  var annulationVue = false;
  var derniereActivite = 0;
  var arme = false;

  function $(id) { return document.getElementById(id); }

  // --- fil d'activité : une seule zone, trois lignes ---------------------------------------------

  function afficherFil() {
    var liste = $("fil");
    page.vider(liste);
    lignes.forEach(function (ligne) {
      var item = page.element("li", ligne.heure + " " + ligne.texte, ligne.gravite);
      item.title = ligne.texte;
      liste.appendChild(item);
    });
  }

  function dire(texte, gravite, heure) {
    var nouvelle = Ordres.filAjouter(lignes, { texte: texte, gravite: gravite || Ordres.graviteDe(texte), heure: heure || Ordres.heureCourte(new Date()) });
    if (nouvelle === lignes) { return; }
    lignes = nouvelle;
    afficherFil();
  }

  // Les messages que les autres fichiers écrivent dans leurs zones (masquées) passent tous par le fil.
  var observateur = new MutationObserver(function (mutations) {
    var vus = [];
    mutations.forEach(function (mutation) {
      var cible = mutation.target.nodeType === 1 ? mutation.target : mutation.target.parentElement;
      var zone = cible && cible.closest ? cible.closest(".vers-fil") : null;
      if (!zone || vus.indexOf(zone) !== -1) { return; }
      vus.push(zone);
      var texte = zone.textContent.trim();
      if (texte) { dire(texte, zone.classList.contains("avertissement") ? "alerte" : undefined); }
    });
  });
  document.querySelectorAll(".vers-fil").forEach(function (zone) {
    observateur.observe(zone, { childList: true, characterData: true, subtree: true });
  });

  // --- pas et allure en groupes de boutons -------------------------------------------------------

  function segments(idListe, idGroupe) {
    var liste = $(idListe), groupe = $(idGroupe);
    var signature = Array.prototype.map.call(liste.options, function (o) { return o.value; }).join("|");
    if (groupe.dataset.signature !== signature) {
      groupe.dataset.signature = signature;
      page.vider(groupe);
      Array.prototype.forEach.call(liste.options, function (option, rang) {
        var bouton = page.element("button", option.textContent.replace(/ m$/, ""));  // l'unité est dans le titre du groupe
        bouton.type = "button";
        bouton.dataset.valeur = option.value;
        if (idListe === "pave-distance") { bouton.title = "Touche " + (rang + 1); }
        bouton.addEventListener("click", function () {
          liste.value = option.value;
          liste.dispatchEvent(new Event("change"));
          segments(idListe, idGroupe);
        });
        groupe.appendChild(bouton);
      });
    }
    Array.prototype.forEach.call(groupe.children, function (bouton) {
      bouton.setAttribute("aria-pressed", String(bouton.dataset.valeur === liste.value));
    });
  }

  function tousLesSegments() {
    segments("pave-distance", "segments-distance");
    segments("pave-angle", "segments-angle");
    segments("pave-allure", "segments-allure");
  }

  // Touches 1 à 4 : pas des flèches ↑ ↓, quand les flèches sont actives et hors de tout champ de saisie.
  document.addEventListener("keydown", function (evenement) {
    var cible = evenement.target || {};
    var rang = Ordres.raccourciDistance({
      key: evenement.key, code: evenement.code, balise: cible.tagName, editable: cible.isContentEditable,
      actives: Boolean(window.LymowPave && window.LymowPave.flechesActives()), nombre: $("pave-distance").options.length
    });
    if (rang === null) { return; }
    evenement.preventDefault();
    $("pave-distance").selectedIndex = rang;
    $("pave-distance").dispatchEvent(new Event("change"));
    tousLesSegments();
  });

  // --- inactivité : toute action sur la page renouvelle le délai du pilotage direct --------------

  function activite() {
    var maintenant = Date.now();
    if (!arme || maintenant - derniereActivite < 20000) { return; }
    derniereActivite = maintenant;
    page.appeler("/api/direct/activite").catch(function () { return undefined; });
  }
  document.addEventListener("pointerdown", activite, true);
  document.addEventListener("keydown", activite, true);

  // --- boutons du tableau ------------------------------------------------------------------------

  $("stop-centre").addEventListener("click", function () { $("stop").click(); });
  $("bon-signal-bouton").addEventListener("click", function () {
    $("onglet-carte").click();
    $("signal-chercher").click();
  });
  $("composer").addEventListener("change", function () { composition(); });

  function composition() {
    // La séquence et la prévisualisation détaillée n'occupent de place que si on compose (ou si une séquence existe).
    var montrer = $("composer").checked || page.sequence().length > 0;
    $("bloc-pilotage").classList.toggle("composition", montrer);
  }

  function afficherPastilles(image) {
    var conteneur = $("pastilles");
    page.vider(conteneur);
    Ordres.pastilles(image).forEach(function (pastille) {
      var element = page.element(pastille.action ? "button" : "span", pastille.texte, "pastille-etat " + (pastille.classe || ""));
      if (pastille.titre) { element.title = pastille.titre; }
      if (pastille.action === "erreur") {
        element.type = "button";
        element.addEventListener("click", function () { erreurOuverte = !erreurOuverte; $("erreur-robot").hidden = !erreurOuverte; });
      }
      conteneur.appendChild(element);
    });
  }

  window.LymowTableau = {
    dire: dire,
    afficher: function (image) {
      arme = Boolean(image.direct && image.direct.arme);
      afficherPastilles(image);
      tousLesSegments();
      composition();
      // Diagnostic d'erreur : derrière sa pastille ; ouvert de lui-même pendant une annulation et à son issue.
      var erreur = image.ordres ? image.ordres.erreur : null;
      var suivi = Boolean(erreur && (erreur.annulation || erreur.resolue));
      if (suivi && !annulationVue) { erreurOuverte = true; }
      annulationVue = suivi;
      if (!erreur) { erreurOuverte = false; }
      $("erreur-robot").hidden = !(erreur && erreurOuverte);
      // File du pilotage direct : visible en pastilles.
      var file = $("file-pastilles");
      page.vider(file);
      ((image.direct && image.direct.file) || []).forEach(function (ordre) {
        file.appendChild(page.element("li", Fleches.symbole(ordre)));
      });
      // Notes du serveur (émission, ordre terminé, désarmement, carte…) : reprises dans le fil, une fois.
      var journal = image.journal || [];
      var nouvelles = Ordres.notesNouvelles(journal, dernierJournal);
      if (journal.length) { dernierJournal = journal[journal.length - 1].heure + journal[journal.length - 1].texte; }
      nouvelles.slice(-3).forEach(function (note) { dire(note.texte, undefined, note.heure.slice(0, 5)); });
      // Retour du dernier pas direct (« ↑ 1 m : émise — pas d'avance : patinage ? »).
      var faites = (image.direct && image.direct.faites) || [];
      if (faites.length) {
        var dernier = faites[faites.length - 1];
        var retour = Fleches.ligneRetour(dernier.ordre, dernier.rapport);
        var cle = faites.length + retour;
        if (cle !== dernierRetour) {
          dernierRetour = cle;
          dire(retour, dernier.rapport && (dernier.rapport.echec || dernier.rapport.cause_probable) ? "alerte" : "info");
        }
      }
    }
  };
})();
