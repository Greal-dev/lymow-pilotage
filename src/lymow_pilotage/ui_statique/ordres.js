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

  var api = {
    texteOrdre: texteOrdre,
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
