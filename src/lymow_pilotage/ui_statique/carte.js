/* Panneau « Carte » : dessin SVG par l'API DOM (createElementNS), zoom, déplacement, choix
 * de la portion de contour. Repère local en mètres : est vers la droite, nord vers le haut.
 * Étiquettes et symboles gardent une taille constante À L'ÉCRAN, quel que soit le zoom.
 * Aucun calcul de retouche ici : le serveur local calcule, borne et valide. */
(function () {
  "use strict";

  var page = window.LymowPage;
  var dessin = document.getElementById("carte-dessin");
  var NS = dessin.namespaceURI; // espace de noms SVG, lu sur l'élément de la page : aucune adresse écrite ici
  var POLICE_PX = 12; // hauteur des étiquettes, en pixels d'écran
  var SOMMET_PX = 4; // rayon d'un sommet
  var SYMBOLE_PX = 7; // demi-taille de la station et du robot
  var ESPACEMENT_MIN_PX = 9; // en dessous, les sommets d'un contour ne sont plus distinguables
  var PROCHE_PX = 10; // distance maximale d'un clic à un sommet
  var carte = null;
  var robot = null;
  var etatRobot = null;
  var dejaCadree = false;
  var CADRE_ROBOT_M = 40; // cadrage initial : le robot et une quarantaine de mètres autour
  var apercu = null;
  var vue = { x: -10, y: -10, l: 20, h: 15 };
  var glisse = null;
  var releve = []; // précision relevée : [est, nord, précision, qualité]

  function $(id) { return document.getElementById(id); }

  function svg(nom, classe, attributs) {
    var noeud = document.createElementNS(NS, nom);
    if (classe) { noeud.setAttribute("class", classe); }
    Object.keys(attributs || {}).forEach(function (cle) { noeud.setAttribute(cle, String(attributs[cle])); });
    return noeud;
  }

  function titre(noeud, texte) {
    var bulle = svg("title");
    bulle.textContent = texte;
    noeud.appendChild(bulle);
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

  /* Mètres par pixel d'écran (preserveAspectRatio par défaut : même échelle sur les deux axes). */
  function echelle() {
    var cadre = dessin.getBoundingClientRect();
    return Math.max(vue.l / (cadre.width || 600), vue.h / (cadre.height || 400));
  }

  function mesurer(contour) {
    var xs = contour.map(function (p) { return p[0]; }), ys = contour.map(function (p) { return p[1]; });
    var perimetre = 0;
    contour.forEach(function (p, i) {
      var q = contour[(i + 1) % contour.length];
      perimetre += Math.hypot(q[0] - p[0], q[1] - p[1]);
    });
    return {
      minX: Math.min.apply(null, xs), maxX: Math.max.apply(null, xs),
      minY: Math.min.apply(null, ys), maxY: Math.max.apply(null, ys), perimetre: perimetre
    };
  }

  function sommetsVisibles(zone, parPixel) {
    return zone.contour.length > 0 && zone.mesure.perimetre / zone.contour.length / parPixel >= ESPACEMENT_MIN_PX;
  }

  function etiquette(zone, estChoisie, parPixel) {
    if (!$("carte-noms").checked) { return null; }
    // Une zone sans nom n'affiche rien ; son identifiant paraît au survol, et quand elle est choisie.
    var texte = zone.nom || (estChoisie ? zone.identifiant : "");
    if (!texte || !zone.contour.length) { return null; }
    var m = zone.mesure;
    var tient = texte.length * POLICE_PX * 0.6 <= (m.maxX - m.minX) / parPixel && POLICE_PX * 1.3 <= (m.maxY - m.minY) / parPixel;
    if (!tient && !estChoisie) { return null; } // elle réapparaît en zoomant
    var nom = svg("text", "carte-nom", { x: (m.minX + m.maxX) / 2, y: -(m.minY + m.maxY) / 2, "font-size": POLICE_PX * parPixel });
    nom.textContent = texte;
    return nom;
  }

  function dessiner() {
    page.vider(dessin);
    dessin.setAttribute("viewBox", [vue.x, vue.y, vue.l, vue.h].join(" "));
    var parPixel = echelle();
    var choisie = zoneChoisie();
    var noms = [];
    if (carte) {
      carte.zones.forEach(function (zone) {
        var estChoisie = Boolean(choisie) && zone.identifiant === choisie.identifiant;
        var contour = svg("polygon", "carte-zone" + (estChoisie ? " choisie" : ""), { points: points(zone.contour) });
        dessin.appendChild(titre(contour, zone.nom ? zone.nom + " (" + zone.identifiant + ")" : zone.identifiant));
        var nom = etiquette(zone, estChoisie, parPixel);
        if (nom) { noms.push(nom); }
      });
      carte.zones_interdites.forEach(function (zone) {
        dessin.appendChild(svg("polygon", "carte-interdite", { points: points(zone.contour) }));
      });
      carte.passages.forEach(function (passage) {
        var classe = "carte-passage" + (passage.passage_de_station ? " station" : "");
        dessin.appendChild(svg("polyline", classe, { points: points(passage.trace) }));
      });
      if (carte.station) {
        var demi = SYMBOLE_PX * parPixel;
        dessin.appendChild(titre(svg("rect", "carte-station", {
          x: carte.station.est_m - demi, y: -carte.station.nord_m - demi, width: 2 * demi, height: 2 * demi
        }), "station de charge"));
      }
    }
    if ($("carte-precision").checked) {  // couche optionnelle : où le signal décroche, où il revient
      releve.forEach(function (point) {
        dessin.appendChild(titre(svg("circle", "releve " + point[3], {
          cx: point[0], cy: -point[1], r: 4 * parPixel
        }), "précision relevée : ±" + point[2] + " m (" + point[3] + ")"));
      });
    }
    if (apercu) {
      dessin.appendChild(svg("polygon", "carte-ancien", { points: points(apercu.avant) }));
      dessin.appendChild(svg("polygon", "carte-nouveau", { points: points(apercu.apres) }));
    }
    noms.forEach(function (nom) { dessin.appendChild(nom); }); // au-dessus des contours
    if (choisie) {
      var de = entier("carte-de"), a = entier("carte-a");
      var tous = sommetsVisibles(choisie, parPixel); // sinon : simple surlignage du contour
      choisie.contour.forEach(function (p, indice) {
        var borne = indice === de || indice === a;
        if (!tous && !borne) { return; }
        var rayon = SOMMET_PX * parPixel * (borne ? 1.6 : 1);
        dessin.appendChild(titre(svg("circle", "carte-sommet" + (borne ? " borne" : ""), { cx: p[0], cy: -p[1], r: rayon }), "sommet " + indice));
      });
    }
    if (robot && robot.rayon) {  // cercle d'incertitude, au rayon annoncé par le robot
      dessin.appendChild(titre(svg("circle", "carte-incertitude", {
        cx: robot.est, cy: -robot.nord, r: robot.rayon
      }), "incertitude de position : ±" + robot.rayon + " m"));
    }
    if (robot) {
      var unite = SYMBOLE_PX * parPixel;
      dessin.appendChild(titre(svg("polygon", "carte-robot", {
        points: [[1.8, 0], [-1.2, 1.1], [-1.2, -1.1]].map(function (p) { return p[0] * unite + "," + p[1] * unite; }).join(" "),
        // Cap mesuré depuis l'est, sens trigonométrique ; l'axe vertical du dessin est inversé.
        transform: "translate(" + robot.est + " " + (-robot.nord) + ") rotate(" + (-robot.cap) + ")"
      }), robot.source === "station" ? "robot (sur sa station : position non transmise au repos)" : "robot"));
    }
    afficherEchelle();
  }

  function afficherEchelle() {
    var metresParPixel = echelle();
    var choix = [0.5, 1, 2, 5, 10, 20, 50, 100, 200];
    var longueur = choix.filter(function (m) { return m / metresParPixel >= 60; })[0] || 500;
    $("carte-barre").style.width = Math.round(longueur / metresParPixel) + "px";
    $("carte-echelle").textContent = longueur + " m";
  }

  /* Cadre la vue sur des points (repère carte), avec une marge. */
  function cadrer(tous, margeMin) {
    if (!tous.length) { return; }
    var xs = tous.map(function (p) { return p[0]; }), ys = tous.map(function (p) { return -p[1]; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    var marge = Math.max(Math.max(maxX - minX, maxY - minY) * 0.08, margeMin || 1);
    vue = { x: minX - marge, y: minY - marge, l: maxX - minX + 2 * marge, h: maxY - minY + 2 * marge };
    dessiner();
  }

  /* Le robot apparaît dès qu'une pose est connue ; au repos sans pose, sur sa station, en le disant. */
  function placerLeRobot() {
    robot = window.LymowOrdres.positionAffichee(etatRobot, carte ? carte.station : null);
    var note = robot ? robot.note : (etatRobot ? "position du robot inconnue" : "");
    $("carte-position").textContent = note;
  }

  function toutVoir() {
    var tous = [];
    if (carte) {
      carte.zones.concat(carte.zones_interdites).forEach(function (z) { tous = tous.concat(z.contour); });
      carte.passages.forEach(function (p) { tous = tous.concat(p.trace); });
      if (carte.station) { tous.push([carte.station.est_m, carte.station.nord_m]); }
    }
    if (robot) { tous.push([robot.est, robot.nord]); }
    cadrer(tous);
  }

  function cadrerSurLeRobot() {
    if (robot) { cadrer([[robot.est, robot.nord]], CADRE_ROBOT_M); }
  }

  function zoomer(facteur, centreX, centreY) {
    var cx = centreX === undefined ? vue.x + vue.l / 2 : centreX;
    var cy = centreY === undefined ? vue.y + vue.h / 2 : centreY;
    var l = Math.min(Math.max(vue.l * facteur, 1), 5000);
    var rapport = l / vue.l;
    vue = { x: cx - (cx - vue.x) * rapport, y: cy - (cy - vue.y) * rapport, l: l, h: vue.h * rapport };
    dessiner();
  }

  function versCarte(evenement) {
    var cadre = dessin.getBoundingClientRect();
    var parPixel = echelle();
    return {
      x: vue.x + vue.l / 2 + (evenement.clientX - cadre.left - cadre.width / 2) * parPixel,
      y: vue.y + vue.h / 2 + (evenement.clientY - cadre.top - cadre.height / 2) * parPixel,
      echelle: parPixel
    };
  }

  /* Un clic ne choisit un sommet que s'il est affiché et tout proche du curseur. */
  function choisirSommet(position) {
    var zone = zoneChoisie();
    if (!zone || !sommetsVisibles(zone, position.echelle)) { return; }
    var meilleur = -1, distance = PROCHE_PX * position.echelle;
    zone.contour.forEach(function (p, indice) {
      var d = Math.hypot(p[0] - position.x, -p[1] - position.y);
      if (d < distance) { distance = d; meilleur = indice; }
    });
    if (meilleur < 0) { return; }
    // Premier clic : « Du sommet » ; second : « au sommet » ; le suivant recommence.
    if (entier("carte-de") === null || entier("carte-a") !== null) {
      $("carte-de").value = String(meilleur);
      $("carte-a").value = "";
    } else {
      $("carte-a").value = String(meilleur);
    }
    apercu = null;
    dessiner();
  }

  function contient(contour, x, y) {
    var dedans = false;
    for (var i = 0, j = contour.length - 1; i < contour.length; j = i++) {
      var a = contour[i], b = contour[j];
      if ((a[1] > y) !== (b[1] > y) && x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]) { dedans = !dedans; }
    }
    return dedans;
  }

  /* Double-clic sur une zone : la sélectionner et cadrer dessus (la plus petite, si plusieurs). */
  function choisirZone(position) {
    if (!carte) { return; }
    var dessous = carte.zones.filter(function (zone) { return contient(zone.contour, position.x, -position.y); });
    dessous.sort(function (a, b) { return a.surface_m2 - b.surface_m2; });
    if (!dessous.length) { return; }
    $("carte-zone").value = dessous[0].identifiant;
    zoneChangee();
    cadrer(dessous[0].contour);
  }

  function ligneChoisie() {
    var identifiant = $("carte-zone").value;
    return carte ? carte.liste.filter(function (ligne) { return ligne.identifiant === identifiant; })[0] : null;
  }

  /* Dit, pour la zone choisie, si la retouche est possible, et sinon pourquoi. */
  function afficherPossibilite() {
    var ligne = ligneChoisie();
    var texte = "";
    if (ligne && ligne.retouche_possible) {
      texte = "Retouche possible sur cette zone";
      if (ligne.sommets_confondus) {
        texte += " — " + ligne.sommets_confondus + " sommet(s) confondu(s) avec leur voisin : ils seront déplacés ensemble";
      }
      texte += ". La prévisualisation vérifie encore les zones interdites, les passages et les bornes.";
    } else if (ligne) {
      texte = "Retouche impossible sur cette zone : " + ligne.motif + ".";
    }
    $("carte-possible").textContent = texte;
    $("carte-previsualiser").disabled = !ligne || !ligne.retouche_possible;
  }

  function zoneChangee() {
    $("carte-de").value = "";
    $("carte-a").value = "";
    apercu = null;
    afficherPossibilite();
    dessiner();
  }

  function recevoir(reponse) {
    carte = reponse.carte;
    apercu = null;
    var choix = $("carte-zone");
    var precedent = choix.value;
    page.vider(choix);
    if (carte) {
      carte.zones.concat(carte.zones_interdites).forEach(function (zone) { zone.mesure = mesurer(zone.contour); });
      // Liste triée par le serveur : zones nommées d'abord, puis les autres par surface décroissante.
      carte.liste.forEach(function (ligne) {
        var option = page.element("option", ligne.libelle);
        option.value = ligne.identifiant;
        option.selected = ligne.identifiant === precedent;
        choix.appendChild(option);
      });
    }
    var incomplete = carte && carte.complete === false
      ? "CARTE INCOMPLÈTE (" + carte.manques.join(" ; ") + ") : affichage seul, aucune retouche possible — "
      : "";
    // Carte de référence (dernière carte complète, gardée sur disque) : dite comme telle, avec sa date.
    var origine = reponse.reference
      ? reponse.reference
      : "carte reçue à " + (reponse.heure || "?") + (typeof reponse.age_s === "number" ? " (il y a " + Math.round(reponse.age_s) + " s)" : "");
    $("carte-etat").textContent = carte ? incomplete + origine : "aucune carte : cliquez sur « Actualiser la carte »";
    $("carte-note").textContent = reponse.note || "";
    afficherPossibilite();
    placerLeRobot();
    // Premier affichage : le robot et ~40 m autour, pas tout le jardin ; « Tout voir » reste là.
    if (carte && !dejaCadree) { dejaCadree = true; if (robot) { cadrerSurLeRobot(); } else { toutVoir(); } }
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
  $("carte-recadrer").addEventListener("click", toutVoir);
  $("carte-robot-cadrer").addEventListener("click", cadrerSurLeRobot);
  $("carte-noms").addEventListener("change", dessiner);
  function lireReleve() {
    if (!$("carte-precision").checked) { dessiner(); return; }
    fetch("/api/releve", { credentials: "same-origin", cache: "no-store" })
      .then(function (reponse) { return reponse.json(); })
      .then(function (corps) { releve = corps.points || []; dessiner(); })
      .catch(function () { return undefined; });
  }
  $("carte-precision").addEventListener("change", lireReleve);
  window.setInterval(function () { if ($("carte-precision").checked) { lireReleve(); } }, 15000);
  $("carte-zone").addEventListener("change", zoneChangee);
  ["carte-de", "carte-a"].forEach(function (id) { $(id).addEventListener("input", function () { apercu = null; dessiner(); }); });
  $("carte-ajustement").addEventListener("submit", previsualiserAjustement);
  dessin.addEventListener("wheel", function (evenement) {
    evenement.preventDefault();
    var position = versCarte(evenement); // le point sous le curseur reste sous le curseur
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
    var parPixel = echelle();
    vue.x = glisse.vx - dx * parPixel;
    vue.y = glisse.vy - dy * parPixel;
    dessiner();
  });
  dessin.addEventListener("pointerup", function (evenement) {
    var etait = glisse;
    glisse = null;
    if (etait && !etait.bouge) { choisirSommet(versCarte(evenement)); } // un clic sans glisser, sur un sommet
  });
  dessin.addEventListener("dblclick", function (evenement) {
    evenement.preventDefault();
    choisirZone(versCarte(evenement));
  });
  window.addEventListener("resize", dessiner);

  window.LymowCarte = {
    robot: function (etat) {
      etatRobot = etat;
      placerLeRobot();
      $("carte-rtk").textContent = etat && etat.localisation_texte ? "— " + etat.localisation_texte : "";
      $("carte-robot-cadrer").disabled = !robot;
      $("carte-robot-cadrer").title = robot ? "" : "position du robot inconnue";
      // « Suivre le robot » : la vue reste centrée sur lui, sans changer le zoom.
      if (robot && robot.source === "pose" && $("carte-suivre").checked && !glisse) {
        vue.x = robot.est - vue.l / 2;
        vue.y = -robot.nord - vue.h / 2;
      }
      dessiner();
    }
  };

  fetch("/api/carte", { credentials: "same-origin", cache: "no-store" })
    .then(function (reponse) { return reponse.json(); })
    .then(recevoir)
    .catch(function () { return undefined; });
})();
