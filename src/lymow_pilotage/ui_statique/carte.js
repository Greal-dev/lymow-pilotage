/* Panneau « Carte » : dessin SVG par l'API DOM (createElementNS), zoom, déplacement, choix
 * de la portion de contour. Repère local en mètres : est vers la droite, nord vers le haut.
 * Aucun calcul de retouche ici : le serveur local calcule, borne et valide. */
(function () {
  "use strict";

  var page = window.LymowPage;
  var dessin = document.getElementById("carte-dessin");
  var NS = dessin.namespaceURI; // espace de noms SVG, lu sur l'élément de la page : aucune adresse écrite ici
  var carte = null;
  var robot = null;
  var apercu = null;
  var vue = { x: -10, y: -10, l: 20, h: 15 };
  var glisse = null;

  function $(id) { return document.getElementById(id); }

  function svg(nom, classe, attributs) {
    var noeud = document.createElementNS(NS, nom);
    if (classe) { noeud.setAttribute("class", classe); }
    Object.keys(attributs || {}).forEach(function (cle) { noeud.setAttribute(cle, String(attributs[cle])); });
    return noeud;
  }

  function points(contour) {
    return contour.map(function (p) { return p[0] + "," + (-p[1]); }).join(" ");
  }

  function zoneChoisie() {
    var identifiant = $("carte-zone").value;
    return carte ? carte.zones.filter(function (z) { return z.identifiant === identifiant; })[0] : null;
  }

  function entier(id) {
    var texte = $(id).value.trim();
    return /^[0-9]+$/.test(texte) ? Number(texte) : null;
  }

  function dessiner() {
    page.vider(dessin);
    dessin.setAttribute("viewBox", [vue.x, vue.y, vue.l, vue.h].join(" "));
    var unite = vue.l / 100; // taille des repères, constante à l'écran
    var choisie = zoneChoisie();
    if (carte) {
      carte.zones.forEach(function (zone) {
        var estChoisie = choisie && zone.identifiant === choisie.identifiant;
        dessin.appendChild(svg("polygon", "carte-zone" + (estChoisie ? " choisie" : ""), { points: points(zone.contour) }));
        if (zone.contour.length) {
          var cx = 0, cy = 0;
          zone.contour.forEach(function (p) { cx += p[0]; cy += p[1]; });
          var nom = svg("text", "carte-nom", { x: cx / zone.contour.length, y: -cy / zone.contour.length, "font-size": unite * 2.2 });
          nom.textContent = zone.nom || zone.identifiant;
          dessin.appendChild(nom);
        }
      });
      carte.zones_interdites.forEach(function (zone) {
        dessin.appendChild(svg("polygon", "carte-interdite", { points: points(zone.contour) }));
      });
      carte.passages.forEach(function (passage) {
        var classe = "carte-passage" + (passage.passage_de_station ? " station" : "");
        dessin.appendChild(svg("polyline", classe, { points: points(passage.trace) }));
      });
      if (carte.station) {
        dessin.appendChild(svg("rect", "carte-station", {
          x: carte.station.est_m - unite, y: -carte.station.nord_m - unite, width: 2 * unite, height: 2 * unite
        }));
      }
    }
    if (apercu) {
      dessin.appendChild(svg("polygon", "carte-ancien", { points: points(apercu.avant) }));
      dessin.appendChild(svg("polygon", "carte-nouveau", { points: points(apercu.apres) }));
    }
    if (choisie) {
      var de = entier("carte-de"), a = entier("carte-a");
      choisie.contour.forEach(function (p, indice) {
        var borne = indice === de || indice === a;
        var sommet = svg("circle", "carte-sommet" + (borne ? " borne" : ""), { cx: p[0], cy: -p[1], r: unite * (borne ? 1.1 : 0.7) });
        var titre = svg("title");
        titre.textContent = "sommet " + indice;
        sommet.appendChild(titre);
        dessin.appendChild(sommet);
      });
    }
    if (robot) {
      var pointe = svg("polygon", "carte-robot", {
        points: [[1.8, 0], [-1.2, 1.1], [-1.2, -1.1]].map(function (p) { return p[0] * unite + "," + p[1] * unite; }).join(" "),
        // Cap mesuré depuis l'est, sens trigonométrique ; l'axe vertical du dessin est inversé.
        transform: "translate(" + robot.est + " " + (-robot.nord) + ") rotate(" + (-robot.cap) + ")"
      });
      dessin.appendChild(pointe);
    }
    afficherEchelle();
  }

  function afficherEchelle() {
    var largeur = dessin.clientWidth || 600;
    var metresParPixel = vue.l / largeur;
    var choix = [0.5, 1, 2, 5, 10, 20, 50, 100, 200];
    var longueur = choix.filter(function (m) { return m / metresParPixel >= 60; })[0] || 500;
    $("carte-barre").style.width = Math.round(longueur / metresParPixel) + "px";
    $("carte-echelle").textContent = longueur + " m";
  }

  function recadrer() {
    var tous = [];
    if (carte) {
      carte.zones.concat(carte.zones_interdites).forEach(function (z) { tous = tous.concat(z.contour); });
      carte.passages.forEach(function (p) { tous = tous.concat(p.trace); });
      if (carte.station) { tous.push([carte.station.est_m, carte.station.nord_m]); }
    }
    if (robot) { tous.push([robot.est, robot.nord]); }
    if (!tous.length) { return; }
    var xs = tous.map(function (p) { return p[0]; }), ys = tous.map(function (p) { return -p[1]; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    var marge = Math.max(maxX - minX, maxY - minY, 2) * 0.08;
    vue = { x: minX - marge, y: minY - marge, l: maxX - minX + 2 * marge, h: maxY - minY + 2 * marge };
    dessiner();
  }

  function zoomer(facteur, centreX, centreY) {
    var cx = centreX === undefined ? vue.x + vue.l / 2 : centreX;
    var cy = centreY === undefined ? vue.y + vue.h / 2 : centreY;
    var l = Math.min(Math.max(vue.l * facteur, 1), 2000);
    var rapport = l / vue.l;
    vue = { x: cx - (cx - vue.x) * rapport, y: cy - (cy - vue.y) * rapport, l: l, h: vue.h * rapport };
    dessiner();
  }

  function versCarte(evenement) {
    var cadre = dessin.getBoundingClientRect();
    // preserveAspectRatio par défaut : même échelle sur les deux axes, dessin centré.
    var echelle = Math.max(vue.l / cadre.width, vue.h / cadre.height);
    return {
      x: vue.x + vue.l / 2 + (evenement.clientX - cadre.left - cadre.width / 2) * echelle,
      y: vue.y + vue.h / 2 + (evenement.clientY - cadre.top - cadre.height / 2) * echelle,
      echelle: echelle
    };
  }

  function choisirSommet(position) {
    var zone = zoneChoisie();
    if (!zone) { return; }
    var meilleur = -1, distance = Infinity;
    zone.contour.forEach(function (p, indice) {
      var d = Math.hypot(p[0] - position.x, -p[1] - position.y);
      if (d < distance) { distance = d; meilleur = indice; }
    });
    if (meilleur < 0) { return; }
    // Premier clic : début de la portion ; second : fin ; le suivant recommence.
    if (entier("carte-de") === null || entier("carte-a") !== null) {
      $("carte-de").value = String(meilleur);
      $("carte-a").value = "";
    } else {
      $("carte-a").value = String(meilleur);
    }
    apercu = null;
    dessiner();
  }

  function recevoir(reponse) {
    carte = reponse.carte;
    apercu = null;
    var choix = $("carte-zone");
    var precedent = choix.value;
    page.vider(choix);
    (carte ? carte.zones : []).forEach(function (zone) {
      var option = page.element("option", (zone.nom || zone.identifiant) + " — " + zone.surface_m2 + " m², " + zone.sommets + " sommets");
      option.value = zone.identifiant;
      option.selected = zone.identifiant === precedent;
      choix.appendChild(option);
    });
    $("carte-etat").textContent = carte
      ? "carte reçue à " + (reponse.heure || "?") + " (il y a " + Math.round(reponse.age_s) + " s)"
      : "aucune carte : cliquez sur « Actualiser la carte »";
    recadrer();
    dessiner();
  }

  function nombre(id) {
    var texte = $(id).value.trim();
    if (texte === "") { return null; }
    if (!/^[+-]?[0-9]+([.,][0-9]+)?$/.test(texte)) { throw new Error("nombre invalide : « " + texte + " »"); }
    return Number(texte.replace(",", "."));
  }

  async function previsualiserAjustement(evenement) {
    evenement.preventDefault();
    var reponse;
    try {
      reponse = await page.appeler("/api/carte/previsualiser", {
        zone: $("carte-zone").value, de: entier("carte-de"), a: entier("carte-a"),
        cm: nombre("carte-cm"), cote: $("carte-cote").value, raccord: nombre("carte-raccord")
      });
    } catch (erreur) {
      page.signalerErreur(erreur);
      return;
    }
    if (reponse.refus) {
      apercu = null;
      page.signalerErreur(new Error(reponse.refus));
    } else {
      apercu = { avant: reponse.avant, apres: reponse.apres };
      page.proposer(reponse.ordres, reponse.resume, reponse.identifiant, true);
    }
    dessiner();
  }

  // --- branchements ---------------------------------------------------------

  $("carte-actualiser").addEventListener("click", function () {
    $("carte-etat").textContent = "demande de la carte au robot…";
    page.appeler("/api/carte/actualiser").then(recevoir).catch(function (erreur) { $("carte-etat").textContent = erreur.message; });
  });
  $("carte-zoom-plus").addEventListener("click", function () { zoomer(0.7); });
  $("carte-zoom-moins").addEventListener("click", function () { zoomer(1 / 0.7); });
  $("carte-recadrer").addEventListener("click", recadrer);
  $("carte-zone").addEventListener("change", function () { $("carte-de").value = ""; $("carte-a").value = ""; apercu = null; dessiner(); });
  ["carte-de", "carte-a"].forEach(function (id) { $(id).addEventListener("input", function () { apercu = null; dessiner(); }); });
  $("carte-ajustement").addEventListener("submit", previsualiserAjustement);
  dessin.addEventListener("wheel", function (evenement) {
    evenement.preventDefault();
    var position = versCarte(evenement);
    zoomer(evenement.deltaY < 0 ? 0.8 : 1.25, position.x, position.y);
  }, { passive: false });
  dessin.addEventListener("pointerdown", function (evenement) {
    glisse = { x: evenement.clientX, y: evenement.clientY, vx: vue.x, vy: vue.y, bouge: false };
    dessin.setPointerCapture(evenement.pointerId);
  });
  dessin.addEventListener("pointermove", function (evenement) {
    if (!glisse) { return; }
    var dx = evenement.clientX - glisse.x, dy = evenement.clientY - glisse.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) { glisse.bouge = true; }
    if (!glisse.bouge) { return; }
    var echelle = versCarte(evenement).echelle;
    vue.x = glisse.vx - dx * echelle;
    vue.y = glisse.vy - dy * echelle;
    dessiner();
  });
  dessin.addEventListener("pointerup", function (evenement) {
    var etait = glisse;
    glisse = null;
    if (etait && !etait.bouge) { choisirSommet(versCarte(evenement)); } // un clic sans glisser choisit un sommet
  });
  window.addEventListener("resize", afficherEchelle);

  window.LymowCarte = {
    robot: function (etat) {
      var connu = etat && typeof etat.position_est_m === "number" && typeof etat.position_nord_m === "number";
      robot = connu ? { est: etat.position_est_m, nord: etat.position_nord_m, cap: etat.cap_deg || 0 } : null;
      $("carte-rtk").textContent = etat && etat.rtk ? "— RTK : " + etat.rtk : "";
      dessiner();
    }
  };

  fetch("/api/carte", { credentials: "same-origin", cache: "no-store" })
    .then(function (reponse) { return reponse.json(); })
    .then(recevoir)
    .catch(function () { return undefined; });
})();
