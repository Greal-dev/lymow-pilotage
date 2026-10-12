/* Ordres et séquence — logique pure de la page, sans réseau ni document.
 *
 * Constat du 2026-10-12 (premier ordre réel) : trois étapes composées, une seule émise. Le
 * bouton « Prévisualiser » d'un formulaire ne prévisualise QUE son ordre, et rien ne le disait ;
 * « Émettre » restait grisé sans explication. Ici : ce que la page annonce avant d'émettre.
 * Fichier testé hors navigateur (voir tests/test_ordres_page.py).
 */
(function (racine) {
  "use strict";

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
    return nombre > 1 ? "Émettre la séquence (" + nombre + " étapes)" : "Émettre l'ordre";
  }

  /* Un ordre seul prévisualisé alors qu'une séquence est composée : le dire, toujours. */
  function avertissementOrdreSeul(etapesPrevisualisees, tailleSequence) {
    if (!tailleSequence || (etapesPrevisualisees || []).length === tailleSequence) { return ""; }
    return "Attention : cette prévisualisation ne contient PAS la séquence composée (" + tailleSequence
      + " étape" + (tailleSequence > 1 ? "s" : "") + "). Pour l'émettre, utilisez « Prévisualiser la séquence ».";
  }

  /* Pourquoi « Émettre » est indisponible ; chaîne vide s'il est disponible. Jamais de bouton grisé muet. */
  function motifEmissionIndisponible(contexte) {
    if (contexte.occupe) { return "Ordre en cours : attente de sa fin et du constat (12 s au plus). STOP reste disponible."; }
    if (contexte.previsualisationValide) { return ""; }
    if (!contexte.emission) { return "Simulation : le serveur n'émet rien (relancez « lymow ui --reel » pour émettre)."; }
    return contexte.note || "Prévisualisez un ordre ou la séquence : « Émettre » s'active ensuite.";
  }

  function libelleReprise(reprise) {
    var nombre = reprise.ordres.length;
    return "Reprendre à l'étape " + reprise.etape + " (" + nombre + " étape" + (nombre > 1 ? "s" : "") + " restante"
      + (nombre > 1 ? "s" : "") + ")";
  }

  /* Statut du robot en clair, et le refus de mouvement dit AVANT toute prévisualisation. */
  function texteEtatDesOrdres(ordres) {
    if (!ordres) { return ""; }
    if (ordres.suspendue) { return ordres.texte; }  // état voulu : dit tel quel, sans alarme
    var texte = ordres.statut ? "Robot : " + ordres.statut : "Robot : état inconnu";
    if (ordres.tache) { texte += " (tâche : " + ordres.tache + ")"; }
    return ordres.mouvement_refuse ? texte + " — " + ordres.mouvement_refuse : texte;
  }

  /* En-tête : l'état du robot en une ligne (en ligne, statut, batterie, RTK). */
  function resumeEtat(etat, suspendue) {
    if (suspendue) { return "liaison suspendue : l'application Lymow a la main"; }
    if (!etat) { return "état du robot inconnu"; }
    var morceaux = [etat.en_ligne === true ? "en ligne" : etat.en_ligne === false ? "HORS LIGNE" : "présence inconnue"];
    morceaux.push((etat.statut_libelle || "statut inconnu") + (etat.tache_libelle ? " (tâche : " + etat.tache_libelle + ")" : ""));
    if (typeof etat.batterie_pct === "number") { morceaux.push("batterie " + etat.batterie_pct + " %"); }
    // La précision en mètres, pas un libellé RTK non confirmé.
    if (etat.localisation_texte) { morceaux.push(etat.localisation_texte); }
    return morceaux.join(" · ") + (etat.perime ? " (état ancien)" : "");
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
      return "Pause en attente de confirmation : les flèches reprendront ensuite.";
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
    return "Le robot se déclare : " + attestation.statut + ".";
  }

  function bandeauAttestation(attestation) {
    if (!attestation || !attestation.active) { return ""; }
    var reste = Math.max(0, Math.floor(attestation.reste_s || 0));
    var secondes = reste % 60;
    return "Pilotage autorisé sur attestation de l'opérateur — expire dans "
      + (reste < 600 ? "0" : "") + Math.floor(reste / 60) + ":" + (secondes < 10 ? "0" : "") + secondes;
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
      var age = typeof etat.age_pose_s === "number" ? etat.age_pose_s : 0;
      var note = age > 5
        ? "position reçue il y a " + (age < 90 ? Math.round(age) + " s" : Math.round(age / 60) + " min") + " : le robot ne la transmet plus, le marqueur n'est plus à jour"
        : "";
      return { est: etat.position_est_m, nord: etat.position_nord_m, cap: etat.cap_deg || 0, rayon: rayon, source: "pose", note: note };
    }
    if (station) {
      return {
        est: station.est_m, nord: station.nord_m, cap: station.cap_deg || 0, rayon: null, source: "station",
        note: "position non transmise au repos : robot placé sur sa station"
      };
    }
    return null;
  }

  /* Bon signal le plus proche : ce que la page en dit, en une ligne. */
  function texteSignal(proposition) {
    if (!proposition) { return ""; }
    var texte = "";
    if (proposition.cible) {
      texte = "Bon signal à " + proposition.cible.distance_m + " m, cap " + proposition.cible.cap_deg + "° ("
        + proposition.cible.points + " relevé" + (proposition.cible.points > 1 ? "s" : "") + ")";
      if (proposition.ordres) { texte += " — trajet de " + proposition.ordres.length + " pas"; }
    }
    if (proposition.motif) { texte += (texte ? " — " : "") + proposition.motif; }
    if (proposition.mention) { texte += " — " + proposition.mention; }
    return texte;
  }

  /* Robot en pause : la phrase qui accompagne « Reprendre ». Signal retrouvé : la reprise est conseillée. */
  function conseilDeReprise(ordres) {
    return ordres && ordres.reprise_conseillee
      ? "Signal retrouvé, position fiable : vous pouvez « Reprendre » la tâche — les lames redémarrent."
      : "Robot en pause : « Reprendre » relance la tâche — les lames peuvent redémarrer.";
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
    var texte = "Recalage : précision " + avant.precision_m + " m → " + apres.precision_m + " m";
    if (typeof avant.ecart_brut_m === "number" && typeof apres.ecart_brut_m === "number") {
      texte += ", écart " + avant.ecart_brut_m + " m → " + apres.ecart_brut_m + " m";
      texte += apres.ecart_brut_m < avant.ecart_brut_m ? " : amélioré." : " : pas d'amélioration (retour à la base, ou redémarrage du robot).";
    } else {
      texte += apres.precision_m <= avant.precision_m ? " : stable ou amélioré." : " : dégradé.";
    }
    return texte;
  }

  /* SEC-13, avertissement : le bord (d'après la carte) est-il avant la fin du pas demandé, devant ou derrière ? */
  function alerteBord(bord, pas) {
    if (!bord || typeof pas !== "number") { return false; }
    return (typeof bord.devant_m === "number" && bord.devant_m < pas) || (typeof bord.derriere_m === "number" && bord.derriere_m < pas);
  }

  function enErreur(ordres) {
    return Boolean(ordres && ordres.erreur && !ordres.erreur.resolue);
  }

  /* Encadré « robot en erreur » : libellés, phrase, bouton d'annulation (masqué pendant l'attente et quand
   * l'erreur a persisté — rien ne boucle), puis la suite. Rien si le robot n'est pas en erreur. */
  function diagnosticErreur(erreur) {
    if (!erreur) { return null; }
    if (erreur.resolue) { return { titre: "", phrase: "", bouton: false, suite: erreur.message || "" }; }
    return {
      titre: "Robot en erreur — " + (erreur.libelles && erreur.libelles.length ? erreur.libelles.join(" ; ") : "code d'erreur non transmis"),
      phrase: erreur.phrase || "",
      bouton: !erreur.annulation,
      suite: erreur.message || ""
    };
  }

  /* Qui a la main ? L'application quand la liaison est suspendue ; l'outil quand il l'a prise et que la
   * liaison est active (STOP ne la retire pas) ; sinon personne en particulier. */
  function quiALaMain(c) {
    if (c.suspendue) { return "application"; }
    return c.mainPrise || c.flechesActives ? "outil" : "personne";
  }

  function texteDeLaMain(qui) {
    if (qui === "outil") { return "Vous avez la main"; }
    return qui === "application" ? "L'application Lymow a la main — liaison suspendue" : "";
  }

  /* « Prendre la main » : ce que le geste va faire, dit dans SA confirmation unique. */
  function annoncePrendreLaMain(c) {
    var lignes = [];
    if (c.suspendue) { lignes.push("La liaison avec le robot sera rétablie."); }
    if (enErreur(c.ordres)) {
      lignes.push("Le robot est EN ERREUR : il ignorera probablement les flèches. Voyez l'encadré rouge (annuler la tâche).");
    } else if (c.ordres && c.ordres.action === "pause") {
      lignes.push(c.ordres.mouvement_refuse.replace(/ : .*$/, "") + " : la tâche sera mise en pause.");
    }
    lignes.push(c.composer
      ? "Les flèches composeront une séquence (rien ne part sans « Émettre »)."
      : "Les flèches feront ensuite bouger le robot IMMÉDIATEMENT" + (c.simulation ? " (simulation : rien n'est émis)." : "."));
    lignes.push("STOP : Échap ou Espace.");
    return "Prendre la main ?\n\n" + lignes.join("\n");
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
