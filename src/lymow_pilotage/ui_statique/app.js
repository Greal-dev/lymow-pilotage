/* Page de pilotage Lymow. Aucune règle de sécurité ne vit ici : le serveur local valide tout.
 * Toute donnée affichée passe par textContent : aucune insertion de HTML, sous aucune forme. */
(function () {
  "use strict";

  var libelles = {};
  var bornes = {};
  var sequence = [];
  var previsualisation = null;
  var Ordres = window.LymowOrdres;
  var occupe = false;
  var liaisonSuspendue = false;
  var objectifSequence = null;
  var noteEmission = "";
  var reprise = null;
  var modeReel = false;
  var emission = false; // réel, ou démonstration (robot fictif)
  var dernierResultat = "";
  var video = { pc: null, ws: null };
  var videoOuverte = false;
  var unClicActif = false;
  var CLES_ETAT = [
    "en_ligne", "statut_libelle", "tache_libelle", "batterie_pct", "erreurs", "avertissements", "rtk", "rtk_satellites",
    "signal_4g", "signal_wifi", "wifi_rssi_dbm", "position_est_m", "position_nord_m", "cap_deg",
    "hauteur_coupe_mm", "micrologiciel", "age_etat_s"
  ];

  function $(id) { return document.getElementById(id); }

  // Le jeton de lancement ne reste pas dans l'adresse (le serveur a déjà redirigé ; ceinture et bretelles).
  if (window.location.search) {
    window.history.replaceState(null, "", window.location.pathname);
  }

  function vider(noeud) {
    while (noeud.firstChild) { noeud.removeChild(noeud.firstChild); }
  }

  function element(nom, texte, classe) {
    var noeud = document.createElement(nom);
    if (texte !== undefined) { noeud.textContent = texte; }
    if (classe) { noeud.className = classe; }
    return noeud;
  }

  function libelle(cle) {
    return libelles[cle] || String(cle).replace(/_/g, " ");
  }

  function texteValeur(valeur) {
    if (valeur === null || valeur === undefined) { return "inconnu"; }
    if (valeur === true) { return "oui"; }
    if (valeur === false) { return "non"; }
    if (Array.isArray(valeur) && valeur.length === 0) { return "aucun"; }
    return String(valeur);
  }

  function estCompose(valeur) {
    return valeur !== null && typeof valeur === "object" && Object.keys(valeur).length > 0;
  }

  /* Rend un objet JSON en listes imbriquées, uniquement par textContent. */
  function rendre(objet, conteneur) {
    if (Array.isArray(objet)) {
      var liste = element("ul");
      objet.forEach(function (item) {
        var ligne = element("li");
        if (estCompose(item)) { rendre(item, ligne); } else { ligne.textContent = texteValeur(item); }
        liste.appendChild(ligne);
      });
      conteneur.appendChild(liste);
      return;
    }
    var definitions = element("dl", undefined, "grille");
    Object.keys(objet).forEach(function (cle) {
      definitions.appendChild(element("dt", libelle(cle)));
      var valeur = element("dd");
      if (estCompose(objet[cle])) { rendre(objet[cle], valeur); } else { valeur.textContent = texteValeur(objet[cle]); }
      definitions.appendChild(valeur);
    });
    conteneur.appendChild(definitions);
  }

  async function appeler(chemin, corps) {
    var reponse = await fetch(chemin, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corps || {}),
      credentials: "same-origin",
      cache: "no-store"
    });
    var donnees = null;
    try { donnees = await reponse.json(); } catch (erreur) { donnees = null; }
    if (!reponse.ok) {
      throw new Error(donnees && donnees.erreur ? donnees.erreur : "erreur " + reponse.status);
    }
    return donnees;
  }

  function signalerErreur(erreur) {
    $("previsualisation-titre").textContent = "Refusé : " + erreur.message;
    vider($("previsualisation-detail"));
    afficherEtapes([], "");
    oublierPrevisualisation("Prévisualisation refusée : " + erreur.message);
  }

  // --- état -----------------------------------------------------------------

  function appliquer(image) {
    modeReel = image.mode === "réel";
    var demonstration = image.mode === "démonstration";
    emission = modeReel || demonstration;
    $("bandeau-reel").hidden = !modeReel;
    $("bandeau-demo").hidden = !demonstration;
    $("bandeau-arret").hidden = true; // le serveur répond
    if (image.serveur) {
      $("version-serveur").textContent = "version " + image.serveur.empreinte + ", démarrée à " + image.serveur.demarre;
    }
    $("video-demarrer").hidden = demonstration;
    $("mode").textContent = modeReel ? "MODE RÉEL" : image.mode;
    $("mode").className = "pastille" + (modeReel ? " reel" : "");
    $("liaison").textContent = image.liaison + (image.occupe ? " — ordre en cours" : "");
    // Statut du robot en clair près des ordres, et le refus de mouvement dit avant toute prévisualisation.
    $("ordres-etat").textContent = Ordres.texteEtatDesOrdres(image.ordres);
    $("ordres-etat").classList.toggle("refuse", Boolean(image.ordres && image.ordres.mouvement_refuse));
    occupe = Boolean(image.occupe);
    // Refus actionnable : le geste utile est à côté du message (pause), et la reprise reste proposée.
    var gestes = Ordres.gestesProposes(image.ordres, occupe);
    $("pause-raccourci").hidden = !gestes.pause;
    $("reprendre-rappel").hidden = !gestes.reprendre;
    $("pause-etat").textContent = gestes.texte;
    $("reprise-avertissement").textContent = gestes.avertissementPosition;
    $("reprise-conseil").textContent = Ordres.conseilDeReprise(image.ordres);
    $("reprendre-quand-meme").hidden = !gestes.reprendreQuandMeme;
    // Qualité de localisation : la précision en mètres, à chaque trame.
    $("localisation").textContent = image.etat ? image.etat.localisation_texte : "";
    $("localisation").className = "localisation " + (image.etat ? image.etat.localisation : "perdue");
    liaisonSuspendue = Boolean(image.liaison_suspendue);
    $("rendre-main").textContent = Ordres.libelleLiaison(liaisonSuspendue);
    afficherAttestation(image.attestation);
    afficherEmission();
    $("appareil").textContent = image.appareil || "";
    $("etat-resume").textContent = Ordres.resumeEtat(image.etat);
    // Aucun compte connecté (hors démonstration) : la section « Compte » s'ouvre d'elle-même.
    if (window.LymowVolets && image.compte && !demonstration) { window.LymowVolets.compte(image.compte.connecte); }
    afficherCompte(image.compte);
    if (window.LymowCarte) { window.LymowCarte.robot(image.etat); }
    if (window.LymowPave) { window.LymowPave.appliquer(image); }
    var bloc = $("bloc-etat");
    var liste = $("etat-liste");
    vider(liste);
    if (!image.etat) {
      $("etat-absent").textContent = image.liaison;
      bloc.classList.remove("perime");
    } else {
      $("etat-absent").textContent = image.etat.perime ? "État périmé ou jamais reçu : valeurs grisées." : "";
      bloc.classList.toggle("perime", Boolean(image.etat.perime));
      CLES_ETAT.forEach(function (cle) {
        liste.appendChild(element("dt", libelle(cle)));
        var valeur = element("dd");
        if (estCompose(image.etat[cle])) { rendre(image.etat[cle], valeur); } else { valeur.textContent = texteValeur(image.etat[cle]); }
        liste.appendChild(valeur);
      });
    }
    var resultat = JSON.stringify(image.resultat);
    if (resultat !== dernierResultat) {
      dernierResultat = resultat;
      vider($("resultat-detail"));
      $("resultat-vide").hidden = Boolean(image.resultat);
      if (image.resultat) { rendre(image.resultat, $("resultat-detail")); }
      // Séquence interrompue : les étapes restantes se reprennent en un clic (via la prévisualisation).
      reprise = image.resultat && image.resultat.reprise && image.resultat.reprise.ordres ? image.resultat.reprise : null;
      $("reprise-bloc").hidden = !reprise;
      if (reprise) { $("reprise").textContent = Ordres.libelleReprise(reprise); }
      if (image.resultat && !occupe && !previsualisation) { noteEmission = "Ordre terminé (résultat plus bas) : prévisualisez le suivant."; afficherEmission(); }
    }
    var journal = $("journal");
    vider(journal);
    (image.journal || []).slice().reverse().forEach(function (ligne) {
      journal.appendChild(element("li", ligne.heure + " — " + ligne.texte));
    });
  }

  // --- attestation « lames à l'arrêt » de l'opérateur (SEC-01b) ---------------
  // La page propose et affiche ; le serveur seul décide (statut attestable, durée, retrait).

  var CONFIRMATION_ATTESTATION = "Vous attestez que les lames sont à l'arrêt. L'outil ne peut pas le vérifier.\n\n"
    + "Le robot se déclare pourtant dans un statut où ses lames peuvent tourner. L'attestation autorise le "
    + "pilotage manuel pendant 10 minutes au plus ; STOP, ou le bouton « Retirer », la retire aussitôt.";

  function afficherAttestation(attestation) {
    var message = Ordres.messageStatutDeclare(attestation);
    document.querySelectorAll(".attestation-proposition").forEach(function (bloc) {
      bloc.hidden = message === "";
      bloc.querySelector('[data-attestation="message"]').textContent = message;
      bloc.querySelector('[data-attestation="poser"]').disabled = occupe;
    });
    var bandeau = Ordres.bandeauAttestation(attestation);
    $("bandeau-attestation").hidden = bandeau === "";
    $("bandeau-attestation-texte").textContent = bandeau;
    if (bandeau !== "") { $("attestation-erreur").textContent = ""; }
  }

  async function poserAttestation() {
    if (!window.confirm(CONFIRMATION_ATTESTATION)) { return; }
    try {
      await appeler("/api/attestation/poser", { confirmer: true });
      $("attestation-erreur").textContent = "";
    } catch (erreur) {
      $("attestation-erreur").textContent = "Attestation refusée : " + erreur.message;
    }
  }

  function retirerAttestation() {
    appeler("/api/attestation/retirer").catch(function (erreur) {
      $("attestation-erreur").textContent = "Retrait refusé : " + erreur.message + " — utilisez STOP.";
    });
  }

  function afficherBornes() {
    var textes = {
      "angle": "jusqu'à " + bornes.angle_max_deg + "°",
      "distance": "jusqu'à " + bornes.distance_max_m + " m",
      "duree": "jusqu'à " + bornes.duree_max_s + " s",
      "vitesse-angulaire": bornes.vitesse_angulaire_min + " à " + bornes.vitesse_angulaire_max + " (défaut " + bornes.vitesse_angulaire_max + ")",
      "vitesse-lineaire": bornes.vitesse_lineaire_min + " à " + bornes.vitesse_lineaire_max + " (défaut " + bornes.vitesse_lineaire_max + ")",
      "hauteur": "entier de " + bornes.hauteur_min_mm + " à " + bornes.hauteur_max_mm,
      "essai-vitesse": bornes.vitesse_lineaire_min + " à " + bornes.essai_vitesse_max + " (défaut " + bornes.essai_vitesse + ")",
      "essai-fenetre": bornes.essai_observation_min + " à " + bornes.essai_observation_max + " (défaut " + bornes.essai_observation + ")"
    };
    document.querySelectorAll("[data-borne]").forEach(function (noeud) {
      noeud.textContent = textes[noeud.dataset.borne] || "";
    });
    $("exposition-essai").textContent = "Exposition : " + bornes.exposition_essai + ".";
  }

  // --- compte ---------------------------------------------------------------
  // Aucun secret n'est gardé ici : le vérificateur et le « state » restent dans le serveur local.

  function messageCompte(texte) { $("compte-message").textContent = texte; }

  function afficherCompte(compte) {
    if (!compte) { return; }
    $("bloc-compte").classList.toggle("mise-en-avant", !compte.connecte);
    $("compte-etat").textContent = compte.connecte
      ? "Connecté — région " + compte.region + ", appareil " + compte.appareil + "."
      : "Non connecté : aucun compte enregistré sur ce poste.";
    $("compte-deconnecter").hidden = !compte.connecte;
    $("compte-connecter").textContent = compte.connecte ? "Changer de compte Google" : "Se connecter avec Google";
    if (!compte.tentative) { $("compte-marche").hidden = true; $("un-clic-attente").hidden = true; }
    var unClic = compte.un_clic || {};
    unClicActif = Boolean(unClic.actif);
    $("un-clic-etat").textContent = !unClic.pris_en_charge
      ? "connexion en un clic : non prise en charge sur ce système"
      : "connexion en un clic : " + (unClic.actif ? "active" : unClic.perime
        ? "à réparer (elle pointe vers un autre emplacement de lymow.exe)" : "inactive")
        + (unClic.etranger ? " (un autre programme occupe déjà le protocole myapp)" : "");
    $("un-clic-activer").hidden = !unClic.pris_en_charge || unClic.actif || Boolean(unClic.etranger);
    $("un-clic-activer").textContent = unClic.perime ? "Réparer la connexion en un clic" : "Activer la connexion en un clic";
    $("un-clic-desactiver").hidden = !(unClic.actif || unClic.perime);
    var choix = $("compte-choix");
    choix.hidden = !compte.choix;
    var liste = $("compte-appareils");
    vider(liste);
    (compte.choix || []).forEach(function (propose) {
      var ligne = element("li");
      var bouton = element("button", propose.nom + " (" + propose.appareil + ")");
      bouton.type = "button";
      bouton.addEventListener("click", function () {
        appeler("/api/compte/choisir", { appareil: propose.appareil })
          .then(function () { messageCompte("Connexion réussie."); })
          .catch(function (erreur) { messageCompte(erreur.message); });
      });
      ligne.appendChild(bouton);
      liste.appendChild(ligne);
    });
  }

  function remplirRegions(regions, defaut) {
    var choix = $("compte-region");
    vider(choix);
    (regions || []).forEach(function (region) {
      var option = element("option", region);
      option.value = region;
      option.selected = region === defaut;
      choix.appendChild(option);
    });
  }

  async function commencerConnexion() {
    messageCompte("");
    var reponse;
    try {
      reponse = await appeler("/api/compte/commencer", { region: $("compte-region").value });
    } catch (erreur) {
      messageCompte(erreur.message);
      return;
    }
    if (!/^https:/.test(reponse.url)) {
      messageCompte("Adresse de connexion inattendue : rien n'a été ouvert.");
      return;
    }
    $("compte-lien").href = reponse.url;
    $("compte-adresse").value = "";
    $("compte-marche").hidden = false;
    $("un-clic-attente").hidden = !unClicActif;
    // Navigation dans un nouvel onglet, sans lien de retour vers cette page.
    window.open(reponse.url, "_blank", "noopener,noreferrer");
    messageCompte("Si aucun onglet ne s'est ouvert, utilisez le lien ci-dessous. Tentative valable 10 minutes.");
  }

  async function validerConnexion(evenement) {
    evenement.preventDefault();
    var champ = $("compte-adresse");
    var adresse = champ.value;
    champ.value = ""; // l'adresse ne reste jamais dans la page, succès ou non
    messageCompte("Vérification en cours…");
    try {
      var reponse = await appeler("/api/compte/valider", { adresse: adresse });
      adresse = "";
      $("compte-marche").hidden = true;
      messageCompte(reponse.appareils ? "Choisissez l'appareil à piloter." : "Connexion réussie.");
    } catch (erreur) {
      adresse = "";
      $("compte-marche").hidden = true;
      messageCompte(erreur.message + " Recommencez avec « Se connecter avec Google ».");
    }
  }

  var EXPLICATION_UN_CLIC = "Activer la connexion en un clic ?\n\n"
    + "Lymow écrit une clé dans le registre de votre session Windows "
    + "(HKCU\\Software\\Classes\\myapp) pour que l'adresse de retour de Google soit remise "
    + "directement à lymow.exe. Rien d'autre n'est modifié. Pour annuler : bouton « Désactiver », "
    + "ou la commande « lymow rappel-desinstaller ».";

  async function reglerUnClic(activer) {
    if (activer && !window.confirm(EXPLICATION_UN_CLIC)) { return; }
    try {
      var reponse = await appeler("/api/compte/un-clic", { activer: activer });
      messageCompte(reponse.message);
    } catch (erreur) {
      messageCompte(erreur.message);
    }
  }

  async function deconnecter() {
    if (!window.confirm("Effacer de ce poste l'accès au compte ? Il faudra se reconnecter avec Google.")) { return; }
    try {
      await appeler("/api/compte/deconnecter");
      messageCompte("Compte effacé de ce poste.");
    } catch (erreur) {
      messageCompte(erreur.message);
    }
  }

  // --- ordres ---------------------------------------------------------------

  function nombre(champ, entier) {
    var texte = champ.value.trim();
    if (texte === "") { return null; }
    var motif = entier ? /^[0-9]+$/ : /^[+-]?[0-9]+([.,][0-9]+)?$/;
    if (!motif.test(texte)) { throw new Error("nombre invalide : « " + texte + " »"); }
    return Number(texte.replace(",", "."));
  }

  function ordreDe(formulaire) {
    var type = formulaire.dataset.ordre;
    var champs = formulaire.elements;
    if (type === "plateau") {
      return { nom: "plateau", valeur: nombre(champs.valeur, true) };
    }
    var vitesse = nombre(champs.vitesse);
    var duree = nombre(champs.duree);
    if (type === "essai-arret-auto") {
      return { nom: type, vitesse: vitesse, duree: duree };
    }
    var valeur = nombre(champs.valeur);
    if (type === "translation") {
      return { nom: champs.nom.value, valeur: valeur, vitesse: vitesse, duree: duree };
    }
    // Tourner : le sens porte sur l'angle, ou sur la vitesse quand une durée est donnée.
    var sens = Number(champs.sens.value) < 0 ? -1 : 1;
    if (valeur !== null) { valeur = Math.abs(valeur) * sens; }
    if (vitesse !== null) { vitesse = Math.abs(vitesse); }
    if (duree !== null) {
      vitesse = (vitesse === null ? bornes.vitesse_angulaire_max : vitesse) * sens;
    }
    return { nom: "tourner", valeur: valeur, vitesse: vitesse, duree: duree };
  }

  var texteOrdre = Ordres.texteOrdre;

  /* « Émettre » : actif ou non, et TOUJOURS la raison à côté quand il ne l'est pas. */
  function afficherEmission() {
    var motif = Ordres.motifEmissionIndisponible({
      occupe: occupe, previsualisationValide: Boolean(previsualisation), emission: emission, note: noteEmission
    });
    $("emettre").disabled = motif !== "";
    $("emettre-note").textContent = motif || noteEmission;
  }

  function oublierPrevisualisation(note) {
    if (previsualisation && previsualisation.minuterie) { window.clearTimeout(previsualisation.minuterie); }
    previsualisation = null;
    noteEmission = note;
    $("emettre").textContent = Ordres.libelleEmission([]);
    afficherEmission();
  }

  function afficherEtapes(etapes, avertissement) {
    var liste = $("previsualisation-etapes");
    vider(liste);
    (etapes || []).forEach(function (etape) { liste.appendChild(element("li", etape)); });
    $("previsualisation-avertissement").textContent = avertissement || "";
    $("previsualisation-avertissement").hidden = !avertissement;
  }

  async function previsualiser(ordres, estTache, objectif) {
    oublierPrevisualisation("");
    var reponse;
    try {
      reponse = await appeler("/api/previsualiser", objectif ? { ordres: ordres, objectif: objectif } : { ordres: ordres });
    } catch (erreur) {
      signalerErreur(erreur);
      return;
    }
    proposer(reponse.ordres, reponse.bilan, reponse.identifiant, estTache, reponse.etapes);
  }

  /* Affiche une prévisualisation et arme « Émettre » si le serveur a fourni un identifiant. */
  function proposer(titre, detail, identifiant, estTache, etapes) {
    oublierPrevisualisation("");
    var reponse = { ordres: titre, identifiant: identifiant };
    etapes = etapes && etapes.length ? etapes : [titre];
    $("previsualisation-titre").textContent = etapes.length > 1
      ? "Séquence de " + etapes.length + " étapes, émises dans cet ordre :" : "Un ordre sera émis :";
    // La liste vient du serveur : c'est exactement ce qui partira, étape par étape.
    afficherEtapes(etapes, Ordres.avertissementOrdreSeul(etapes, estTache ? 0 : sequence.length));
    vider($("previsualisation-detail"));
    rendre(detail, $("previsualisation-detail"));
    if (!reponse.identifiant) {
      var motif = detail && detail.motif ? detail.motif : "voir le détail ci-dessus";
      noteEmission = emission ? "Prévisualisation refusée, rien à émettre : " + motif : "";
      afficherEmission();
      return;
    }
    previsualisation = {
      identifiant: reponse.identifiant,
      texte: reponse.ordres,
      tache: Boolean(estTache),
      minuterie: window.setTimeout(function () {
        oublierPrevisualisation("Prévisualisation expirée : prévisualisez de nouveau.");
      }, bornes.previsualisation_s * 1000)
    };
    $("emettre").textContent = Ordres.libelleEmission(etapes);
    noteEmission = "Valable " + bornes.previsualisation_s + " s, pour "
      + (etapes.length > 1 ? "ces " + etapes.length + " étapes" : "cet ordre") + " seulement.";
    afficherEmission();
  }

  async function emettre() {
    if (!previsualisation) { return; }
    var courante = previsualisation;
    if (courante.tache && !window.confirm("Émettre « " + courante.texte + " » vers le robot ?")) { return; }
    oublierPrevisualisation("Émission demandée…");
    try {
      await appeler("/api/emettre", { identifiant: courante.identifiant });
      noteEmission = "Ordre accepté : le résultat s'affichera plus bas. Prévisualisez ensuite le suivant.";
    } catch (erreur) {
      noteEmission = "Émission refusée : " + erreur.message + " — prévisualisez de nouveau.";
    }
    afficherEmission();
  }

  async function stop() {
    oublierPrevisualisation("");
    try {
      var reponse = await appeler("/api/stop");
      if (reponse && reponse.bilan) {
        afficherEtapes([], "");
        $("previsualisation-titre").textContent = "STOP (simulation)";
        vider($("previsualisation-detail"));
        rendre(reponse.bilan, $("previsualisation-detail"));
      } else {
        noteEmission = "STOP envoyé. Prévisualisez un ordre pour reprendre.";
      }
    } catch (erreur) {
      noteEmission = "STOP refusé : " + erreur.message;
    }
    afficherEmission();
  }

  // --- séquence -------------------------------------------------------------

  function afficherSequence() {
    var liste = $("sequence-liste");
    vider(liste);
    sequence.forEach(function (ordre, rang) {
      var ligne = element("li", texteOrdre(ordre) + " ");
      [["Monter", -1], ["Descendre", 1], ["Retirer", 0]].forEach(function (action) {
        var bouton = element("button", action[0]);
        bouton.type = "button";
        bouton.addEventListener("click", function () {
          var cible = rang + action[1];
          sequence.splice(rang, 1);
          if (action[1] !== 0 && cible >= 0 && cible <= sequence.length) { sequence.splice(cible, 0, ordre); }
          else if (action[1] !== 0) { sequence.splice(rang, 0, ordre); }
          afficherSequence();
        });
        ligne.appendChild(bouton);
      });
      liste.appendChild(ligne);
    });
    $("sequence-vide").hidden = sequence.length > 0;
    $("sequence-previsualiser").disabled = sequence.length === 0;
    $("sequence-vider").disabled = sequence.length === 0;
    if (window.LymowPave) { window.LymowPave.sequenceChangee(); } // le pavé de flèches montre la même liste
  }

  // --- vidéo (non vérifié en réel : voir le README) -------------------------

  function etatVideo(texte) { $("video-etat").textContent = texte; }

  function arreterVideo(texte) {
    if (video.ws) { video.ws.onclose = null; video.ws.close(); }
    if (video.pc) { video.pc.close(); }
    video = { pc: null, ws: null };
    if (videoOuverte) {
      // Le serveur refuse un second démarrage tant que la page n'a pas rendu la session.
      videoOuverte = false;
      appeler("/api/video/arreter").catch(function () { return undefined; });
    }
    $("video").srcObject = null;
    $("video-demarrer").disabled = false;
    $("video-arreter").disabled = true;
    etatVideo(texte || "arrêtée");
  }

  async function demarrerVideo() {
    $("video-demarrer").disabled = true;
    etatVideo("ouverture de la session…");
    var reglages;
    try {
      reglages = await appeler("/api/video/demarrer");
    } catch (erreur) {
      arreterVideo("indisponible : " + erreur.message);
      return;
    }
    videoOuverte = true;
    $("video-arreter").disabled = false;
    var pc = new RTCPeerConnection({ iceServers: reglages.iceServers || [], bundlePolicy: "max-bundle" });
    var ws = new WebSocket(reglages.wss);
    var enAttente = [];
    reglages = null; // secrets temporaires : rien n'est gardé au-delà de l'ouverture
    video = { pc: pc, ws: ws };
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.ontrack = function (evenement) {
      $("video").srcObject = evenement.streams[0] || new MediaStream([evenement.track]);
      $("video").play().catch(function () { return undefined; });
    };
    pc.onconnectionstatechange = function () { etatVideo("liaison vidéo : " + pc.connectionState); };
    pc.onicecandidate = function (evenement) {
      if (evenement.candidate && ws.readyState === WebSocket.OPEN) {
        ws.send(Signalisation.messageCandidat(evenement.candidate.toJSON()));
      }
    };
    ws.onopen = async function () {
      etatVideo("signalisation ouverte, offre envoyée…");
      try {
        var offre = await pc.createOffer();
        await pc.setLocalDescription(offre);
        ws.send(Signalisation.messageOffre(offre.sdp));
      } catch (erreur) {
        arreterVideo("échec de l'offre vidéo");
      }
    };
    ws.onmessage = async function (evenement) {
      var message = Signalisation.decoderMessage(evenement.data);
      if (!message) { return; }
      try {
        if (message.type === "SDP_ANSWER") {
          await pc.setRemoteDescription(message.charge);
          while (enAttente.length) { await pc.addIceCandidate(enAttente.shift()); }
        } else if (!pc.remoteDescription) {
          enAttente.push(message.charge);
        } else {
          await pc.addIceCandidate(message.charge);
        }
      } catch (erreur) {
        etatVideo("message de signalisation refusé par le navigateur");
      }
    };
    ws.onerror = function () { etatVideo("échec de la signalisation"); };
    ws.onclose = function () { if (video.ws === ws && pc.connectionState !== "connected") { etatVideo("signalisation fermée"); } };
  }

  // --- branchements ---------------------------------------------------------

  document.querySelectorAll("form.ordre").forEach(function (formulaire) {
    formulaire.addEventListener("submit", function (evenement) {
      evenement.preventDefault();
      try { previsualiser([ordreDe(formulaire)], false); } catch (erreur) { signalerErreur(erreur); }
    });
    formulaire.querySelectorAll("[data-action]").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        try {
          if (bouton.dataset.action === "plateau-haut") { previsualiser([{ nom: "plateau-haut" }], false); return; }
          sequence.push(ordreDe(formulaire));
          afficherSequence();
        } catch (erreur) { signalerErreur(erreur); }
      });
    });
  });
  document.querySelectorAll("[data-tache]").forEach(function (bouton) {
    bouton.addEventListener("click", function () { previsualiser([{ nom: bouton.dataset.tache }], true); });
  });
  $("sequence-previsualiser").addEventListener("click", function () { previsualiser(sequence.slice(), false, objectifSequence); });
  $("sequence-vider").addEventListener("click", function () { sequence = []; afficherSequence(); });
  $("emettre").addEventListener("click", emettre);
  // Cadre de droite : deux onglets, « Caméra » et « Carte ».
  function montrerOnglet(nom) {
    ["camera", "carte"].forEach(function (onglet) {
      $("vue-" + onglet).hidden = onglet !== nom;
      $("onglet-" + onglet).setAttribute("aria-selected", String(onglet === nom));
    });
    window.dispatchEvent(new Event("resize")); // la carte se dessine à sa taille réelle
  }
  $("onglet-camera").addEventListener("click", function () { montrerOnglet("camera"); });
  $("onglet-carte").addEventListener("click", function () { montrerOnglet("carte"); });
  $("rendre-main").addEventListener("click", async function () {
    // Rendre la main : la liaison est fermée et ne se rouvre plus seule ; rien n'est émis vers le robot.
    try {
      if (liaisonSuspendue) { await appeler("/api/liaison/reprendre"); return; }
      var reponse = await appeler("/api/liaison/rendre", {});
      if (reponse.attendre && window.confirm(reponse.message + "\n\nRendre la main sans attendre ?")) {
        reponse = await appeler("/api/liaison/rendre", { sans_attendre: true });
      }
      $("pause-etat").textContent = reponse.message || "";
    } catch (erreur) {
      $("pause-etat").textContent = "Refusé : " + erreur.message;
    }
  });
  $("pause-raccourci").addEventListener("click", async function () {
    // Une confirmation, puis la pause seule. Aucun mouvement n'est enchaîné : c'est à vous de rappuyer.
    if (!window.confirm("Mettre la tâche du robot en pause, pour prendre la main ?")) { return; }
    try {
      var reponse = await appeler("/api/pause", { confirmer: true });
      $("pause-etat").textContent = reponse.simulation ? "Simulation : pause parcourue, rien n'est émis." : "Pause demandée…";
    } catch (erreur) {
      $("pause-etat").textContent = "Pause refusée : " + erreur.message;
    }
  });
  $("reprise").addEventListener("click", function () {
    if (!reprise) { return; }
    sequence = reprise.ordres.slice();
    afficherSequence();
    previsualiser(sequence.slice(), false); // jamais d'émission directe : Prévisualiser, puis Émettre
  });
  $("stop").addEventListener("click", stop);
  document.querySelectorAll('[data-attestation="poser"]').forEach(function (bouton) {
    bouton.addEventListener("click", poserAttestation);
  });
  $("attestation-retirer").addEventListener("click", retirerAttestation);
  $("actualiser").addEventListener("click", function () {
    appeler("/api/actualiser").then(appliquer).catch(function (erreur) { $("liaison").textContent = erreur.message; });
  });
  $("video-demarrer").addEventListener("click", demarrerVideo);
  $("compte-connecter").addEventListener("click", commencerConnexion);
  $("compte-formulaire").addEventListener("submit", validerConnexion);
  $("compte-deconnecter").addEventListener("click", deconnecter);
  $("un-clic-activer").addEventListener("click", function () { reglerUnClic(true); });
  $("un-clic-desactiver").addEventListener("click", function () { reglerUnClic(false); });
  $("video-arreter").addEventListener("click", function () { arreterVideo(); });

  window.addEventListener("pagehide", function () {
    if (videoOuverte && navigator.sendBeacon) { navigator.sendBeacon("/api/video/arreter"); }
  });

  // Échap et Espace = STOP, partout : Espace n'active donc jamais un autre bouton.
  function estToucheStop(evenement) {
    return evenement.key === "Escape" || evenement.key === " " || evenement.key === "Spacebar";
  }
  document.addEventListener("keydown", function (evenement) {
    if (!estToucheStop(evenement)) { return; }
    // Échap : STOP prioritaire ; il retire seulement le focus des flèches si rien ne bouge ni n'est armé.
    if (evenement.key === "Escape" && !evenement.repeat && window.LymowPave && window.LymowPave.echap(occupe)) {
      evenement.preventDefault();
      evenement.stopPropagation();
      return;
    }
    evenement.preventDefault();
    evenement.stopPropagation();
    if (!evenement.repeat) { stop(); }
  }, true);
  document.addEventListener("keyup", function (evenement) {
    if (estToucheStop(evenement)) { evenement.preventDefault(); evenement.stopPropagation(); }
  }, true);

  fetch("/api/etat", { credentials: "same-origin", cache: "no-store" })
    .then(function (reponse) { return reponse.json(); })
    .then(function (image) {
      libelles = image.libelles || {};
      bornes = image.bornes || {};
      afficherBornes();
      if (window.LymowPave) { window.LymowPave.configurer(image.fleches, bornes); }
      remplirRegions(image.regions, image.region_defaut);
      appliquer(image);
      var flux = new EventSource("/api/flux");
      flux.onmessage = function (evenement) { appliquer(JSON.parse(evenement.data)); };
      flux.onerror = function () {
        // Flux rompu : le serveur est arrêté (ou remplacé). Plus de page figée qui semble vivante.
        $("bandeau-arret").hidden = false;
        $("liaison").textContent = "page déconnectée du serveur local";
      };
    })
    .catch(function () { $("liaison").textContent = "serveur local injoignable"; });
  afficherSequence();
  // Outils partagés avec le panneau « Carte » (carte.js), sans variable globale de plus.
  window.LymowPage = {
    appeler: appeler, element: element, vider: vider, proposer: proposer, signalerErreur: signalerErreur,
    // Pavé de flèches (pave.js) : il écrit dans LA séquence de cette page, jamais dans une seconde liste.
    sequence: function () { return sequence.slice(); },
    ajouterEtape: function (ordre) { sequence.push(ordre); afficherSequence(); },
    annulerDerniere: function () { sequence.pop(); afficherSequence(); },
    viderSequence: function () { sequence = []; objectifSequence = null; afficherSequence(); },
    previsualiserSequence: function () { if (sequence.length) { previsualiser(sequence.slice(), false, objectifSequence); } },
    // Trajet vers le bon signal : chargé dans LA séquence, avec son objectif (arrêt dès que la précision est fiable).
    chargerSequence: function (ordres, objectif) { sequence = ordres.slice(); objectifSequence = objectif || null; afficherSequence(); },
    previsualiser: function (ordres) { previsualiser(ordres, false); }
  };
})();
