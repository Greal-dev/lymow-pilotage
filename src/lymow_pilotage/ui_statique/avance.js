/* Interrupteur « Mode avancé » : désactivé par défaut, choix mémorisé sur ce poste (clé « lymow-mode-avance »).
 * Il ne fait que montrer ou masquer (classe « avance ») ; rien n'est retiré de la page, aucune règle ne change.
 * STOP, Prendre / Rendre la main, Retour base, Annuler la tâche ne portent jamais cette classe. */
(function () {
  "use strict";

  var CLE = "lymow-mode-avance";
  var interrupteur = document.getElementById("mode-avance");

  function appliquer(actif) {
    document.body.classList.toggle("mode-avance", actif);
    interrupteur.checked = actif;
    window.dispatchEvent(new Event("resize"));  // la carte se redessine à sa taille réelle
  }

  var memoire = null;
  try { memoire = window.localStorage.getItem(CLE); } catch (erreur) { memoire = null; }
  appliquer(memoire === "1");
  interrupteur.addEventListener("change", function () {
    appliquer(interrupteur.checked);
    try { window.localStorage.setItem(CLE, interrupteur.checked ? "1" : "0"); } catch (erreur) { /* stockage refusé : sans gravité */ }
  });
})();
