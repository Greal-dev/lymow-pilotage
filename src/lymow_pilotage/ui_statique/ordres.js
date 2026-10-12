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
    return ordres.mouvement_refuse ? texte + " — " + ordres.mouvement_refuse : texte;
  }

  /* En-tête : l'état du robot en une ligne (en ligne, statut, batterie, RTK). */
  function resumeEtat(etat) {
    if (!etat) { return "état du robot inconnu"; }
    var morceaux = [etat.en_ligne === true ? "en ligne" : etat.en_ligne === false ? "HORS LIGNE" : "présence inconnue"];
    morceaux.push(etat.statut_libelle || "statut inconnu");
    if (typeof etat.batterie_pct === "number") { morceaux.push("batterie " + etat.batterie_pct + " %"); }
    if (etat.rtk) { morceaux.push("RTK " + etat.rtk); }
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
      texte: ordres && ordres.pause ? ordres.pause.texte : ""
    };
  }

  var api = {
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
