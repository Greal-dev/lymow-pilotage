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
    var texte = ordres.statut ? "Robot : " + ordres.statut : "Robot : état inconnu";
    if (ordres.tache) { texte += " (tâche : " + ordres.tache + ")"; }
    return ordres.mouvement_refuse ? texte + " — " + ordres.mouvement_refuse : texte;
  }

  /* En-tête : l'état du robot en une ligne (en ligne, statut, batterie, RTK). */
  function resumeEtat(etat) {
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
      return { est: etat.position_est_m, nord: etat.position_nord_m, cap: etat.cap_deg || 0, rayon: rayon, source: "pose", note: "" };
    }
    if (station) {
      return {
        est: station.est_m, nord: station.nord_m, cap: station.cap_deg || 0, rayon: null, source: "station",
        note: "position non transmise au repos : robot placé sur sa station"
      };
    }
    return null;
  }

  var api = {
    libelleLiaison: libelleLiaison,
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
