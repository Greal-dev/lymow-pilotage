/* Sections repliables de la page : fermées par défaut, état mémorisé sur ce poste.
 * Seule donnée gardée par le navigateur : quelles sections sont ouvertes (nom → vrai/faux),
 * sous la clé « lymow-volets ». Jamais d'identité, de jeton ni d'état du robot. */
(function () {
  "use strict";

  var CLE = "lymow-volets";
  var Ordres = window.LymowOrdres;
  var volets = Array.prototype.slice.call(document.querySelectorAll("details[data-volet]"));
  var noms = volets.map(function (volet) { return volet.dataset.volet; });

  function lire() {
    try { return JSON.parse(window.localStorage.getItem(CLE)); } catch (erreur) { return null; }
  }

  function garder() {
    var etat = {};
    volets.forEach(function (volet) { etat[volet.dataset.volet] = volet.open; });
    try { window.localStorage.setItem(CLE, JSON.stringify(etat)); } catch (erreur) { /* stockage refusé : sans gravité */ }
  }

  var ouverts = Ordres.voletsOuverts(lire(), noms, true);
  volets.forEach(function (volet) {
    volet.open = ouverts[volet.dataset.volet];
    volet.addEventListener("toggle", function () {
      garder();
      window.dispatchEvent(new Event("resize")); // la carte se redessine à sa taille réelle
    });
  });

  window.LymowVolets = {
    /* Aucun compte connecté : la section « Compte » s'ouvre d'elle-même (sans l'enregistrer comme un choix). */
    compte: function (connecte) {
      var volet = document.getElementById("volet-compte");
      if (!connecte && volet && !volet.open) { volet.open = true; }
    }
  };
})();
