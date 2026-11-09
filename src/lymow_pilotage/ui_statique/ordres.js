/* Ordres et séquence — logique pure de la page, sans réseau ni document.
 *
 * Constat du 2026-10-12 (premier ordre réel) : trois étapes composées, une seule émise. Le
 * bouton « Prévisualiser » d'un formulaire ne prévisualise QUE son ordre, et rien ne le disait ;
 * « Émettre » restait grisé sans explication. Ici : ce que la page annonce avant d'émettre.
 * Fichier testé hors navigateur (voir tests/test_ordres_page.py).
 */
(function (racine) {
  "use strict";

  // Textes dans la langue choisie ; hors navigateur (tests Node), en français.
  var I18n = racine.LymowI18n || (typeof require === "function" ? require("./i18n.js") : null);
  var t = I18n.t;

  function texteOrdre(ordre) {
    var mots = [ordre.nom];
    if (ordre.valeur !== null && ordre.valeur !== undefined) { mots.push(String(ordre.valeur)); }
    if (ordre.duree !== null && ordre.duree !== undefined) { mots.push("durée " + ordre.duree + " s"); }
    if (ordre.vitesse !== null && ordre.vitesse !== undefined) { mots.push("vitesse " + ordre.vitesse); }
    return mots.join(" ");
  }

  /* Intitulé du bouton d'émission : il dit combien d'étapes partiront. */
  function libelleEmission(etapes) {
    var nombre = (etapes || []).length;
    return nombre > 1 ? t("emission.sequence", { n: nombre }) : t("emission.ordre");
  }

  /* Un ordre seul prévisualisé alors qu'une séquence est composée : le dire, toujours. */
  function avertissementOrdreSeul(etapesPrevisualisees, tailleSequence) {
    if (!tailleSequence || (etapesPrevisualisees || []).length === tailleSequence) { return ""; }
    return t("sequence.ordre-seul", { etapes: I18n.compte("mot.etape", tailleSequence) });
  }

  /* Pourquoi « Émettre » est indisponible ; chaîne vide s'il est disponible. Jamais de bouton grisé muet. */
  function motifEmissionIndisponible(contexte) {
    if (contexte.occupe) { return t("emission.occupe"); }
    if (contexte.previsualisationValide) { return ""; }
    if (!contexte.emission) { return t("emission.simulation"); }
    return contexte.note || t("emission.prevoir");
  }

  function libelleReprise(reprise) {
    var nombre = reprise.ordres.length;
    return t("reprise.libelle", { etape: reprise.etape, restantes: I18n.compte("mot.restante", nombre) });
  }

  /* Statut du robot en clair, et le refus de mouvement dit AVANT toute prévisualisation. */
  function texteEtatDesOrdres(ordres) {
    if (!ordres) { return ""; }
    if (ordres.suspendue) { return I18n.francais() ? ordres.texte : t("resume.suspendue"); }  // état voulu : dit tel quel, sans alarme
    var texte = ordres.statut ? t("ordres.robot", { statut: I18n.statut(ordres.statut) }) : t("ordres.robot-inconnu");
    if (ordres.tache) { texte += t("resume.tache", { tache: I18n.statut(ordres.tache) }); }
    // ponytail: le motif de refus vient du serveur, en français ; à coder côté serveur pour le traduire.
    return ordres.mouvement_refuse ? texte + " — " + ordres.mouvement_refuse : texte;
  }

  /* En-tête : l'état du robot en une ligne (en ligne, statut, batterie, RTK). */
  function resumeEtat(etat, suspendue) {
    if (suspendue) { return t("resume.suspendue"); }
    if (!etat) { return t("resume.inconnu"); }
    var morceaux = [t(etat.en_ligne === true ? "resume.en-ligne" : etat.en_ligne === false ? "resume.hors-ligne" : "resume.presence")];
    morceaux.push((I18n.statut(etat.statut_libelle, etat.statut) || t("resume.statut-inconnu"))
      + (etat.tache_libelle ? t("resume.tache", { tache: I18n.statut(etat.tache_libelle, etat.tache) }) : ""));
    if (typeof etat.batterie_pct === "number") { morceaux.push(t("resume.batterie", { pct: etat.batterie_pct })); }
    // La précision en mètres, pas un libellé RTK non confirmé.
    if (etat.localisation_texte) { morceaux.push(texteLocalisation(etat)); }
    return morceaux.join(" · ") + (etat.perime ? t("resume.ancien") : "");
  }

  /* « position : ±0,02 m — fiable » : le texte du serveur en français, recomposé dans les autres langues. */
  function texteLocalisation(etat) {
    if (I18n.francais() || !etat) { return etat ? etat.localisation_texte : ""; }
    var precision = typeof etat.precision_m === "number" ? "±" + I18n.decimal(etat.precision_m.toFixed(2)) + " m" : t("loc.inconnue");
    return t("loc.texte", { precision: precision, qualite: t("loc." + etat.localisation) });
  }

  /* Sections repliables : fermées par défaut, puis ce que le poste a mémorisé ; « compte » ouverte
   * d'office si aucun compte n'est connecté. Une mémoire abîmée vaut « rien de mémorisé ». */
  function voletsOuverts(memoire, noms, compteConnecte) {
    var ouverts = {};
    var valide = memoire && typeof memoire === "object" && !Array.isArray(memoire);
    noms.forEach(function (nom) { ouverts[nom] = Boolean(valide && memoire[nom] === true); });
    if (!compteConnecte && noms.indexOf("compte") >= 0) { ouverts.compte = true; }
    return ouverts;
  }

  /* Pourquoi les flèches n'agissent pas maintenant ; chaîne vide si elles agissent. Après une pause
   * demandée, elles attendent le statut « pause » frais — et rien ne part tout seul ensuite. */
  function blocageDesFleches(ordres) {
    if (ordres && ordres.pause && ordres.pause.etat === "en attente") {
      return t("fleches.pause-attente");
    }
    return "";
  }

  /* Ce que la page propose à côté d'un refus : le geste utile, s'il existe. */
  function gestesProposes(ordres, occupe) {
    return {
      pause: Boolean(ordres && ordres.action === "pause" && !occupe),
      reprendre: Boolean(ordres && ordres.reprise_possible && !occupe),
      // SEC-12 : position imprécise — la reprise simple serait refusée ; « quand même » reste possible.
      avertissementPosition: ordres && ordres.reprise_possible && ordres.avertissement_position
        ? ordres.avertissement_position : "",
      reprendreQuandMeme: Boolean(ordres && ordres.reprise_possible && ordres.avertissement_position && !occupe),
      texte: ordres && ordres.pause ? ordres.pause.texte : ""
    };
  }

  /* SEC-01b — attestation « lames à l'arrêt » de l'opérateur : ce que la page en dit. Le serveur seul
   * décide si elle est proposable (statut attestable) ; rien n'est déduit ici. */
  function messageStatutDeclare(attestation) {
    if (!attestation || !attestation.proposable || attestation.active) { return ""; }
    return t("attestation.statut", { statut: I18n.statut(attestation.statut) });
  }

  function bandeauAttestation(attestation) {
    if (!attestation || !attestation.active) { return ""; }
    var reste = Math.max(0, Math.floor(attestation.reste_s || 0));
    var secondes = reste % 60;
    return t("attestation.bandeau", { reste: (reste < 600 ? "0" : "") + Math.floor(reste / 60) + ":" + (secondes < 10 ? "0" : "") + secondes });
  }

  function libelleLiaison(suspendue) {
    return suspendue ? "Reprendre la liaison" : "Rendre la main à l'application";
  }

  /* Où dessiner le robot sur la carte : sa pose dès qu'elle est connue, avec son cercle d'incertitude ;
   * au repos sans pose, sur sa station, en le disant. */
  function positionAffichee(etat, station) {
    var rayon = etat && typeof etat.precision_m === "number" ? etat.precision_m : null;
    if (etat && typeof etat.position_est_m === "number" && typeof etat.position_nord_m === "number") {
      // Position ancienne : le marqueur reste où il était, et c'est dit — le robot ne la transmet plus.
      var age = typeof etat.age_position_s === "number" ? etat.age_position_s : 0;
      var note = age > 5
        ? t("carte.position-ancienne", { duree: age < 90 ? Math.round(age) + " s" : Math.round(age / 60) + " min" })
        : "";
      return { est: etat.position_est_m, nord: etat.position_nord_m, cap: etat.cap_deg || 0, rayon: rayon, source: "pose", note: note };
    }
    if (station) {
      return {
        est: station.est_m, nord: station.nord_m, cap: station.cap_deg || 0, rayon: null, source: "station",
        note: t("carte.position-station")
      };
    }
    return null;
  }

  /* Bon signal le plus proche : ce que la page en dit, en une ligne. */
  function texteSignal(proposition) {
    if (!proposition) { return ""; }
    var texte = "";
    if (proposition.cible) {
      texte = t("signal.texte", {
        m: proposition.cible.distance_m, cap: proposition.cible.cap_deg, releves: I18n.compte("mot.releve", proposition.cible.points)
      });
      if (proposition.ordres) { texte += t("signal.trajet", { n: proposition.ordres.length }); }
    }
    // ponytail: motif et mention viennent du serveur, en français.
    if (proposition.motif) { texte += (texte ? " — " : "") + proposition.motif; }
    if (proposition.mention) { texte += " — " + proposition.mention; }
    return texte;
  }

  /* Robot en pause : la phrase qui accompagne « Reprendre ». Signal retrouvé : la reprise est conseillée. */
  function conseilDeReprise(ordres) {
    return t(ordres && ordres.reprise_conseillee ? "reprise.conseillee" : "reprise.pause");
  }

  /* Recalage assisté : un aller-retour en ligne droite, chargé dans la séquence (jamais exécuté seul).
   * Pratique décrite par la communauté pour qu'un robot guidé par satellite retrouve position et cap ;
   * effet non garanti, jamais essayé sur ce robot. */
  function sequenceDeRecalage(pas) {
    var etape = function (nom) { return { nom: nom, valeur: pas, vitesse: null, duree: null }; };
    return [etape("avancer"), etape("avancer"), etape("reculer"), etape("reculer")];
  }

  /* Après le recalage : la situation s'est-elle améliorée ? (précision et écart, avant / après) */
  function bilanDeRecalage(avant, apres) {
    if (!avant || !apres) { return ""; }
    var texte = t("recalage.bilan", { avant: avant.precision_m, apres: apres.precision_m });
    if (typeof avant.ecart_brut_m === "number" && typeof apres.ecart_brut_m === "number") {
      texte += t("recalage.bilan-ecart", { avant: avant.ecart_brut_m, apres: apres.ecart_brut_m });
      texte += t(apres.ecart_brut_m < avant.ecart_brut_m ? "recalage.ameliore" : "recalage.sans-amelioration");
    } else {
      texte += t(apres.precision_m <= avant.precision_m ? "recalage.stable" : "recalage.degrade");
    }
    return texte;
  }

  /* SEC-13, avertissement : le bord (d'après la carte) est-il avant la fin du pas demandé, devant ou derrière ? */
  function alerteBord(bord, pas) {
    if (!bord || typeof pas !== "number") { return false; }
    return (typeof bord.devant_m === "number" && bord.devant_m < pas) || (typeof bord.derriere_m === "number" && bord.derriere_m < pas);
  }

  /* Recalage d'affichage : ce que la page en dit, et ce qu'elle propose. */
  function etatDuRecalage(recalage, attenteDuClic) {
    var r = recalage || {};
    return {
      texte: attenteDuClic ? t("recalage.bandeau") : (r.texte || ""),  // ponytail: r.texte vient du serveur, en français
      annuler: Boolean(r.actif),
      mesure: r.mesure_station || "",
      utiliserLaMesure: Boolean(r.mesure_station) && !r.actif
    };
  }

  /* Hauteur du plateau : une CONSIGNE enregistrée par le robot (lue dans la carte), jamais une mesure — le
   * robot ne transmet aucune hauteur mesurée. Inconnue : dit tel quel. */
  function textePlateau(plateau) {
    var p = plateau || {};
    var suite = p.suite ? " — " + p.suite : "";
    if (typeof p.mm !== "number") {
      return { texte: t("plateau.inconnu", { suite: suite }), jauge: null };
    }
    var valeur = p.au_maximum ? t("plateau.au-max", { mm: p.mm }) : t("plateau.valeur", { mm: p.mm, max: p.maximum_mm });
    // ponytail: nature et suite viennent du serveur, en français.
    var nature = t("plateau.nature", { nature: p.nature || t("plateau.reglage"), date: p.date ? ", " + p.date : "" });
    return { texte: t("plateau.texte", { valeur: valeur, nature: nature, suite: suite }), jauge: { min: p.minimum_mm, max: p.maximum_mm, valeur: Math.min(p.mm, p.maximum_mm) } };
  }

  var CONFIRMATION_ESSAI_PLATEAU = "Ordre jamais essayé par le cloud sur ce robot. À faire robot à vue, mains éloignées du plateau.";

  /* Essais de plateau : proposés en mode réel seulement, hors de tout ordre en cours. */
  /* « Retour base » : l'info-bulle dit l'ordre que le clic émettra (avec ou sans tâche à conserver). */
  function bulleRetourBase(libelle) {
    if (libelle === "déjà sur la base") { return t("base.deja"); }
    // ponytail: le libellé de l'ordre vient du serveur, en français.
    return t("base.emet", { libelle: libelle }) + (libelle === "retour à la base" ? t("base.sans-tache") : ".");
  }

  function essaiPlateauPossible(c) {
    if (c.mode === "simulation") { return { possible: false, motif: t("essai.reel-seulement") }; }
    if (c.occupe) { return { possible: false, motif: t("commun.ordre-en-cours") }; }
    return { possible: true, motif: "" };
  }

  /* Fil d'activité : trois lignes, la plus récente en tête ; un message identique au dernier n'est pas redit. */
  function filAjouter(lignes, ligne) {
    if (!ligne || !ligne.texte) { return lignes; }
    if (lignes.length && lignes[0].texte === ligne.texte) { return lignes; }
    return [ligne].concat(lignes).slice(0, 3);
  }

  function graviteDe(texte) {
    var t = String(texte).toLowerCase();
    if (/arrêt non confirmé|sec-01|liaison perdue|injoignable|en échec/.test(t)) { return "erreur"; }
    if (/refus|échou|non abouti|désarmé|interrompu|patinage|liaison lente|d'après la carte|ignor|persiste|aucun changement|n'a pas changé d'état|vidéo : échec|non transmis/.test(t)) { return "alerte"; }
    return "info";
  }

  function heureCourte(date) {
    return ("0" + date.getHours()).slice(-2) + ":" + ("0" + date.getMinutes()).slice(-2);
  }

  /* Notes du serveur pas encore reprises dans le fil (repère : la dernière note déjà vue). */
  function notesNouvelles(journal, derniereVue) {
    if (!derniereVue) { return journal.slice(-1); }
    for (var i = journal.length - 1; i >= 0; i -= 1) {
      if (journal[i].heure + journal[i].texte === derniereVue) { return journal.slice(i + 1); }
    }
    return journal.slice(-3);
  }

  /* Touches 1 à 4 : choix du pas des flèches ↑ ↓ — flèches actives, hors champ de saisie. Rang, ou null. */
  function raccourciDistance(e) {
    var balise = String(e.balise || "").toUpperCase();
    if (!e.actives || balise === "INPUT" || balise === "SELECT" || balise === "TEXTAREA" || e.editable) { return null; }
    if (String(e.code || "").indexOf("Numpad") === 0) { return null; }  // pavé numérique : les diagonales
    var rang = ["1", "2", "3", "4", "5", "6"].indexOf(e.key);
    return rang >= 0 && rang < e.nombre ? rang : null;
  }

  /* Inactivité du pilotage direct : le temps restant n'est dit que dans la dernière minute. */
  function texteInactivite(arme, reste) {
    return arme && typeof reste === "number" && reste <= 60 ? t("fleches.inactivite", { s: reste }) : "";
  }

  /* « Reprendre » relance les lames : deux clics, le second dans les 5 s (pas de boîte de dialogue). */
  function secondClic(premier, maintenant) {
    return premier > 0 && maintenant - premier <= 5000;
  }

  /* État du robot en pastilles compactes : statut · batterie · position · bord · plateau · liaison (· erreur). */
  function pastilles(image) {
    var ordres = image.ordres || {}, etat = image.etat || null, liste = [];
    if (enErreur(ordres)) {
      var libelles = (ordres.erreur.libelles || []).map(function (l) { return I18n.erreur(l); });
      var codes = libelles.map(function (l) { return l.split(" : ")[0]; }).join(", ");
      liste.push({ texte: t("pastille.erreur", { codes: codes }), classe: "erreur", action: "erreur", titre: t("pastille.erreur.titre", { libelles: libelles.join(" ; ") }) });
    }
    liste.push({
      texte: ordres.suspendue ? t("pastille.suspendue") : (I18n.statut(ordres.statut, etat ? etat.statut : null) || t("pastille.inconnu")),
      classe: ordres.mouvement_refuse ? "alerte" : "", titre: texteEtatDesOrdres(ordres)
    });
    if (etat && typeof etat.batterie_pct === "number") { liste.push({ texte: "🔋 " + etat.batterie_pct + " %", classe: etat.batterie_pct < 20 ? "alerte" : "" }); }
    if (etat && typeof etat.precision_m === "number") {
      // ponytail: le détail de localisation vient du serveur, en français.
      liste.push({ texte: "±" + I18n.decimal(etat.precision_m) + " m", classe: etat.localisation,
        titre: t("pastille.position", { qualite: I18n.francais() ? etat.localisation : t("loc." + etat.localisation) })
          + (etat.localisation_detail ? " — " + etat.localisation_detail : "") });
    } else if (etat) {
      liste.push({ texte: t("pastille.position-inconnue"), classe: "perdue", titre: t("pastille.position-inconnue.titre") });
    }
    if (ordres.bord && typeof ordres.bord.devant_m === "number") {
      // ponytail: le texte du bord (info-bulle) vient du serveur, en français.
      liste.push({ texte: t("pastille.bord", { m: I18n.decimal(ordres.bord.devant_m) }), classe: image.alerteBord ? "alerte" : "", titre: ordres.bord.texte });
    }
    var plateau = textePlateau(image.plateau);
    liste.push({ texte: image.plateau && typeof image.plateau.mm === "number"
      ? t("pastille.plateau", { mm: image.plateau.mm }) + (image.plateau.au_maximum ? t("pastille.plateau-max") : "") : t("pastille.plateau-inconnu"), titre: plateau.texte });
    // Phares : lecture seule (niveau annoncé par le robot, plage réglée dans l'application).
    if (image.phares && typeof image.phares.allumes === "boolean") {
      liste.push({ texte: t(image.phares.allumes ? "pastille.phares-allumes" : "pastille.phares-eteints"), titre: image.phares.texte });
    }
    if (image.recalage && image.recalage.actif) { liste.push({ texte: t("pastille.recalee"), classe: "alerte", titre: image.recalage.texte }); }
    // ponytail: image.liaison (info-bulle) vient du serveur, en français.
    liste.push({ texte: t(image.connecte ? "pastille.liaison-ok" : "pastille.liaison-ko"), classe: image.connecte ? "ok" : (ordres.suspendue ? "" : "erreur"), titre: image.liaison || "" });
    return liste;
  }

  /* Longueur du décalage entre position annoncée et position recalée : « 1,2 m ». */
  function distanceDuDecalage(annoncee, recalee) {
    var d = Math.hypot(recalee[0] - annoncee[0], recalee[1] - annoncee[1]);
    return String(Math.round(d * 100) / 100).replace(".", ",") + " m";
  }

  /* Bouton à bascule des phares : il propose le contraire du niveau annoncé par le robot (inconnu : allumer). */
  function boutonPhares(phares) {
    var allumer = !(phares && phares.allumes === true);
    return { allumer: allumer, libelle: t(allumer ? "phares.allumer" : "phares.eteindre") };
  }

  function enErreur(ordres) {
    return Boolean(ordres && ordres.erreur && !ordres.erreur.resolue);
  }

  /* Encadré « robot en erreur » : libellés, phrase, bouton d'annulation (masqué pendant l'attente et quand
   * l'erreur a persisté — rien ne boucle), puis la suite. Rien si le robot n'est pas en erreur. */
  function diagnosticErreur(erreur) {
    if (!erreur) { return null; }
    // Hors français, les messages du serveur sont redits d'après l'état de l'annulation (texte du serveur sinon).
    var suite = I18n.francais() || !erreur.message ? erreur.message || ""
      : erreur.resolue ? t("erreur.resolue") : erreur.annulation === "en attente" ? t("erreur.attente")
        : erreur.annulation === "persistante" ? t("erreur.persiste") : erreur.message;
    if (erreur.resolue) { return { titre: "", phrase: "", bouton: false, suite: suite }; }
    var libelles = erreur.libelles && erreur.libelles.length ? erreur.libelles.map(function (l) { return I18n.erreur(l); }).join(" ; ") : t("erreur.sans-code");
    return {
      titre: t("erreur.titre", { libelles: libelles }),
      phrase: I18n.francais() || !erreur.phrase ? erreur.phrase || "" : t("erreur.phrase"),
      bouton: !erreur.annulation,
      suite: suite
    };
  }

  /* Qui a la main ? L'application quand la liaison est suspendue ; l'outil quand il l'a prise et que la
   * liaison est active (STOP ne la retire pas) ; sinon personne en particulier. */
  function quiALaMain(c) {
    if (c.suspendue) { return "application"; }
    return c.mainPrise || c.flechesActives ? "outil" : "personne";
  }

  function texteDeLaMain(qui) {
    if (qui === "outil") { return t("main.vous"); }
    return qui === "application" ? t("main.application") : "";
  }

  /* « Prendre la main » : ce que le geste va faire, dit dans SA confirmation unique. */
  function annoncePrendreLaMain(c) {
    var lignes = [];
    if (c.suspendue) { lignes.push(t("prise.liaison")); }
    if (enErreur(c.ordres)) {
      lignes.push(t("prise.erreur"));
    } else if (c.ordres && c.ordres.action === "pause") {
      var motif = I18n.francais() ? c.ordres.mouvement_refuse.replace(/ : .*$/, "") : t("ordres.robot", { statut: I18n.statut(c.ordres.statut) });
      lignes.push(t("prise.pause", { motif: motif }));
    }
    lignes.push(c.composer ? t("prise.composer") : t("prise.direct") + (c.simulation ? t("prise.simulation") : "."));
    lignes.push(t("prise.stop"));
    return t("prise.titre") + "\n\n" + lignes.join("\n");
  }

  /* « Prendre la main », pas à pas : la prochaine action, au vu de l'état que le serveur vient d'annoncer.
   * Une seule à la fois ; rien n'est enchaîné à l'aveugle (la pause doit être CONFIRMÉE par le robot). */
  function suitePrendreLaMain(c) {
    if (c.suspendue) { return "liaison"; }  // d'abord la liaison et l'état : la confirmation dira vrai
    var ordres = c.ordres || {};
    if (c.simulation && !c.connecte) {  // simulation sans robot : rien à lire, rien à mettre en pause
      if (!c.confirme) { return "confirmer"; }
      return c.flechesActives ? "fait" : "fleches";
    }
    if (!c.connecte || c.occupe) { return "attendre"; }
    if (!c.confirme) { return ordres.statut || ordres.mouvement_refuse ? "confirmer" : "attendre"; }
    var pause = ordres.pause ? ordres.pause.etat : null;
    if (pause === "en attente") { return "attendre"; }
    // Robot en erreur : aucune pause n'est tentée ; le diagnostic et son geste sont affichés tels quels.
    if (ordres.action === "pause" && !enErreur(ordres)) {
      if (!c.pauseDemandee) { return "pause"; }
      return pause === "non confirmée" ? "abandon" : "attendre";
    }
    if (ordres.mouvement_refuse) { return "bloque"; }  // statut non pilotable : l'attestation est alors proposée
    return c.flechesActives ? "fait" : "fleches";
  }

  /* « Rendre la main » : refusé pendant un mouvement (STOP d'abord) ; attendre la fin de la télécommande. */
  function etapeRendreLaMain(c) {
    if (c.suspendue) { return "deja"; }
    if (c.occupe) { return "refus"; }
    if (c.ordres && c.ordres.telecommande && !c.sansAttendre) { return "attendre"; }
    return "choisir";
  }

  /* « Rendre la main » et sa boîte de choix : un clic de souris, toujours ; au clavier (Entrée, Espace sur
   * le bouton qui a le focus), seulement flèches inactives — flèches actives, Espace = STOP et rien d'autre. */
  function clicRendreAccepte(c) {
    return !(c.clavier && c.flechesActives);
  }

  var api = {
    libelleLiaison: libelleLiaison,
    quiALaMain: quiALaMain,
    clicRendreAccepte: clicRendreAccepte,
    diagnosticErreur: diagnosticErreur,
    alerteBord: alerteBord,
    etatDuRecalage: etatDuRecalage,
    distanceDuDecalage: distanceDuDecalage,
    textePlateau: textePlateau,
    filAjouter: filAjouter,
    boutonPhares: boutonPhares,
    graviteDe: graviteDe,
    heureCourte: heureCourte,
    notesNouvelles: notesNouvelles,
    raccourciDistance: raccourciDistance,
    texteInactivite: texteInactivite,
    secondClic: secondClic,
    pastilles: pastilles,
    essaiPlateauPossible: essaiPlateauPossible,
    CONFIRMATION_ESSAI_PLATEAU: CONFIRMATION_ESSAI_PLATEAU,
    DELAI_VIDEO_MS: 75000,
    bulleRetourBase: bulleRetourBase,
    texteDeLaMain: texteDeLaMain,
    annoncePrendreLaMain: annoncePrendreLaMain,
    suitePrendreLaMain: suitePrendreLaMain,
    etapeRendreLaMain: etapeRendreLaMain,
    sequenceDeRecalage: sequenceDeRecalage,
    bilanDeRecalage: bilanDeRecalage,
    texteSignal: texteSignal,
    conseilDeReprise: conseilDeReprise,
    positionAffichee: positionAffichee,
    messageStatutDeclare: messageStatutDeclare,
    bandeauAttestation: bandeauAttestation,
    texteOrdre: texteOrdre,
    blocageDesFleches: blocageDesFleches,
    gestesProposes: gestesProposes,
    resumeEtat: resumeEtat,
    voletsOuverts: voletsOuverts,
    libelleEmission: libelleEmission,
    avertissementOrdreSeul: avertissementOrdreSeul,
    motifEmissionIndisponible: motifEmissionIndisponible,
    libelleReprise: libelleReprise,
    texteEtatDesOrdres: texteEtatDesOrdres
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    racine.LymowOrdres = api;
  }
})(typeof self !== "undefined" ? self : this);
