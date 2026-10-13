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
  var dernierPhares = { allumer: true };
  var dernierNettoyage = {};
  var dernieresOrdres = null;
  var dernierEtat = null;
  var recalageAvant = null;
  var recalageResultat = "";
  var objectifSequence = null;
  var noteEmission = "";
  var reprise = null;
  var modeReel = false;
  var emission = false; // réel, ou démonstration (robot fictif)
  var dernierResultat = "";
  var video = { pc: null, ws: null, minuterie: null };
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

  var titresDOrigine = {};
  function griser(id, raison) {
    var bouton = $(id);
    if (!(id in titresDOrigine)) { titresDOrigine[id] = bouton.title; }
    bouton.disabled = Boolean(raison);
    bouton.title = raison || titresDOrigine[id];
  }

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
    $("ordres-etat").classList.toggle("refuse", Boolean(image.ordres && image.ordres.mouvement_refuse && !image.ordres.suspendue));
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
    $("localisation-detail").textContent = image.etat ? image.etat.localisation_detail : "";
    // Boutons indisponibles : grisés, leur raison en info-bulle — jamais un texte permanent.
    griser("recalage", !(image.ordres && image.ordres.recalage_conseille) ? "Proposé quand le signal est revenu mais que la position calculée s'écarte de la position satellite." : occupe ? "Un ordre est en cours." : "");
    griser("reprendre-bouton", image.ordres && image.ordres.reprise_possible ? "" : "Le robot n'est pas en pause : rien à reprendre.");
    griser("bon-signal-bouton", image.carte_connue ? "" : "Aucune carte connue.");
    ["pause-bouton", "base-bouton", "annuler-bouton", "pave-plateau"].forEach(function (id) {
      griser(id, image.liaison_suspendue ? "La main est rendue à l'application Lymow." : occupe ? "Un ordre est en cours : attendez sa fin, ou STOP." : "");
    });
    if (!$("base-bouton").disabled && image.ordres && image.ordres.retour_base) {
      $("base-bouton").title = Ordres.bulleRetourBase(image.ordres.retour_base);
    }
    dernierEtat = image.etat;
    if (recalageAvant && image.resultat && !occupe && JSON.stringify(image.resultat) !== recalageResultat) {
      $("pause-etat").textContent = Ordres.bilanDeRecalage(recalageAvant, image.etat);  // après l'aller-retour
      recalageAvant = null;
    }
    var erreur = Ordres.diagnosticErreur(image.ordres ? image.ordres.erreur : null);
    $("erreur-robot").hidden = !erreur;
    if (erreur) {
      $("erreur-libelle").textContent = erreur.titre;
      $("erreur-phrase").textContent = erreur.phrase;
      $("erreur-annuler").hidden = !erreur.bouton;
      $("erreur-annuler").disabled = occupe;
      $("erreur-suite").textContent = erreur.suite;
    }
    $("recalage-mention").textContent = image.recalage && image.recalage.actif ? image.recalage.texte : "";
    var essai = Ordres.essaiPlateauPossible({ mode: image.mode, occupe: occupe });
    document.querySelectorAll("[data-plateau-essai]").forEach(function (bouton) { bouton.disabled = !essai.possible; });
    if (!essai.possible) { $("plateau-essai-etat").textContent = essai.motif; }
    dernierNettoyage = image.nettoyage || {};
    $("nettoyage").hidden = !dernierNettoyage.posee;
    $("nettoyage-texte").textContent = dernierNettoyage.texte || "";
    $("nettoyage-essai").disabled = occupe || !!dernierNettoyage.essai;
    $("voyants-etat").textContent = image.voyants ? image.voyants.texte : "";
    dernierPhares = Ordres.boutonPhares(image.phares);
    $("phares-libelle").textContent = dernierPhares.libelle;
    griser("phares-bouton", image.liaison_suspendue ? "La main est rendue à l'application Lymow." : occupe ? "Un ordre est en cours : rappuyez à la fin du pas." : "");
    var plateau = Ordres.textePlateau(image.plateau);
    $("plateau-etat").textContent = plateau.texte;
    $("plateau-jauge").hidden = !plateau.jauge;
    if (plateau.jauge) {
      $("plateau-jauge").min = plateau.jauge.min;
      $("plateau-jauge").max = plateau.jauge.max;
      $("plateau-jauge").value = plateau.jauge.valeur;
    }
    liaisonSuspendue = Boolean(image.liaison_suspendue);
    $("diagnostic-rtk").checked = Boolean(image.diagnostic_rtk);
    $("diagnostic-texte").textContent = image.diagnostic_texte || "";
    dernieresOrdres = image.ordres || null;
    $("rendre-main").disabled = liaisonSuspendue;
    $("rendre-main").title = liaisonSuspendue ? "La main est déjà rendue : « Prendre la main » la reprend." : "";
    avancerRendre();
    afficherAttestation(image.attestation);
    afficherEmission();
    $("appareil").textContent = image.appareil || "";
    $("etat-resume").textContent = Ordres.resumeEtat(image.etat, Boolean(image.liaison_suspendue));
    // Aucun compte connecté (hors démonstration) : la section « Compte » s'ouvre d'elle-même.
    if (window.LymowVolets && image.compte && !demonstration) { window.LymowVolets.compte(image.compte.connecte); }
    afficherCompte(image.compte);
    if (window.LymowTableau) {
      image.alerteBord = $("bord-zone").classList.contains("alerte-carte");
      window.LymowTableau.afficher(image);
    }
    if (window.LymowCarte) {
      window.LymowCarte.robot(image.etat, image.ordres && image.ordres.bord ? image.ordres.bord.texte : "");
      window.LymowCarte.connue(Boolean(image.carte_connue));
      window.LymowCarte.recalage(image.recalage);
    }
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
    proposer(reponse.ordres, reponse.bilan, reponse.identifiant, estTache, reponse.etapes, reponse.garde_carte);
  }

  /* Affiche une prévisualisation et arme « Émettre » si le serveur a fourni un identifiant. */
  function proposer(titre, detail, identifiant, estTache, etapes, gardeCarte) {
    oublierPrevisualisation("");
    var reponse = { ordres: titre, identifiant: identifiant };
    etapes = etapes && etapes.length ? etapes : [titre];
    $("previsualisation-titre").textContent = etapes.length > 1
      ? "Séquence de " + etapes.length + " étapes, émises dans cet ordre :" : "Un ordre sera émis :";
    // La liste vient du serveur : c'est exactement ce qui partira, étape par étape.
    // SEC-13 : un pas que le garde-fou carte raccourcira ou refusera est annoncé ici, avant « Émettre ».
    afficherEtapes(etapes, [(gardeCarte || []).join(" ; "), Ordres.avertissementOrdreSeul(etapes, estTache ? 0 : sequence.length)]
      .filter(Boolean).join(" — "));
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
    // STOP annule TOUT : prévisualisation, séquence composée, flèches et file du direct — puis arrête le robot.
    // Il ne rend pas la main : on peut repartir aussitôt en réactivant les flèches.
    oublierPrevisualisation("");
    sequence = [];
    objectifSequence = null;
    afficherSequence();
    $("rendre-choix").hidden = true;
    if (window.LymowPave) { window.LymowPave.desactiverFleches(false); }
    try {
      var reponse = await appeler("/api/stop");
      if (reponse && reponse.bilan) {
        afficherEtapes([], "");
        $("previsualisation-titre").textContent = "STOP (simulation)";
        vider($("previsualisation-detail"));
        rendre(reponse.bilan, $("previsualisation-detail"));
      } else {
        noteEmission = "STOP envoyé : tout est annulé, le robot est arrêté et sa tâche mise en pause. Réactivez les flèches pour repartir.";
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

  /* Chaque étape est dite sous l'image ET rapportée au serveur (fil d'activité, journal) : jamais d'adresse ni de clé. */
  function etapeVideo(etape, motif) {
    appeler("/api/video/etape", { etape: etape, motif: motif || "" }).catch(function () { return undefined; });
  }

  function fermerVideo() {
    if (video.minuterie) { window.clearTimeout(video.minuterie); }
    if (video.renvoi) { window.clearInterval(video.renvoi); }
    if (video.ws) { video.ws.onclose = null; video.ws.onerror = null; video.ws.close(); }
    if (video.pc) { video.pc.onconnectionstatechange = null; video.pc.oniceconnectionstatechange = null; video.pc.close(); }
    video = { pc: null, ws: null };
    $("video").srcObject = null;
    $("video-demarrer").disabled = false;
    $("video-arreter").disabled = true;
  }

  function arreterVideo(texte) {
    fermerVideo();
    if (videoOuverte) {
      videoOuverte = false;
      appeler("/api/video/arreter").catch(function () { return undefined; });
    }
    $("video-reessayer").hidden = true;
    etatVideo(texte || "arrêtée");
  }

  /* Échec : dit en clair où ça bloque, rapporté au serveur (qui libère la session), « Réessayer » proposé. */
  function echecVideo(motif) {
    fermerVideo();
    videoOuverte = false;
    etapeVideo("echec", motif);
    etatVideo("échec : " + motif);
    $("video-reessayer").hidden = false;
  }

  async function demarrerVideo() {
    fermerVideo();
    $("video-demarrer").disabled = true;
    $("video-reessayer").hidden = true;
    etatVideo("demande des réglages au cloud Lymow…");
    var reglages;
    try {
      reglages = await appeler("/api/video/demarrer");
    } catch (erreur) {  // le serveur a déjà écrit le refus et son code dans le fil et le journal
      fermerVideo();
      etatVideo("refusée par l'interface : " + erreur.message);
      $("video-reessayer").hidden = false;
      return;
    }
    videoOuverte = true;
    $("video-arreter").disabled = false;
    var etape = "ouverture de la signalisation";
    var pc;
    var ws;
    try {
      pc = new RTCPeerConnection({ iceServers: reglages.iceServers || [], bundlePolicy: "max-bundle" });
      ws = new WebSocket(reglages.wss);
    } catch (erreur) {
      if (pc) { pc.close(); }
      echecVideo("le navigateur refuse d'ouvrir la signalisation (" + erreur.name + ") : rechargez la page");
      return;
    }
    var enAttente = [];
    reglages = null; // secrets temporaires : rien n'est gardé au-delà de l'ouverture
    video = { pc: pc, ws: ws, minuterie: null };
    video.minuterie = window.setTimeout(function () {
      if (video.pc === pc && pc.connectionState !== "connected") { echecVideo("délai dépassé (75 s) à l'étape : " + etape); }
    }, Ordres.DELAI_VIDEO_MS);
    etatVideo(etape + "…");
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.ontrack = function (evenement) {
      $("video").srcObject = evenement.streams[0] || new MediaStream([evenement.track]);
      $("video").play().catch(function () { return undefined; });
    };
    pc.onconnectionstatechange = function () {
      if (video.pc !== pc) { return; }
      if (pc.connectionState === "connected") {
        window.clearTimeout(video.minuterie);
        etape = "connexion établie";
        etapeVideo("connexion-etablie");
        etatVideo("connexion établie");
      } else if (pc.connectionState === "failed") {
        echecVideo("liaison vidéo en échec après l'étape : " + etape);
      } else {
        etatVideo(etape + " — liaison vidéo : " + pc.connectionState);
      }
    };
    pc.oniceconnectionstatechange = function () {
      if (video.pc !== pc) { return; }
      if (pc.iceConnectionState === "failed") {
        echecVideo("état ICE failed (aucun chemin réseau vers le robot) après l'étape : " + etape);
      } else if (pc.iceConnectionState === "disconnected") {
        etapeVideo("echec", "état ICE disconnected (flux interrompu)");
        etatVideo("flux interrompu (ICE disconnected) : attente, ou « Réessayer »");
        $("video-reessayer").hidden = false;
      }
    };
    pc.onicecandidate = function (evenement) {
      if (evenement.candidate && ws.readyState === WebSocket.OPEN) {
        ws.send(Signalisation.messageCandidat(evenement.candidate.toJSON()));
      }
    };
    ws.onopen = async function () {
      etape = "signalisation ouverte";
      etapeVideo("signalisation-ouverte");
      etatVideo("signalisation ouverte, préparation de l'offre…");
      try {
        var offre = await pc.createOffer();
        await pc.setLocalDescription(offre);
        var trame = Signalisation.messageOffre(offre.sdp);
        ws.send(trame);
        // Le canal ne garde pas l'offre : la caméra du robot peut mettre 30 à 60 s à le rejoindre → on la renvoie.
        video.renvoi = window.setInterval(function () {
          if (video.ws !== ws || pc.remoteDescription || ws.readyState !== WebSocket.OPEN) { window.clearInterval(video.renvoi); return; }
          ws.send(trame);
        }, 4000);
        etape = "offre envoyée, attente de la réponse du robot";
        etapeVideo("offre-envoyee");
        etatVideo(etape + "…");
      } catch (erreur) {
        echecVideo("offre vidéo impossible (" + erreur.name + ")");
      }
    };
    ws.onmessage = async function (evenement) {
      var message = Signalisation.decoderMessage(evenement.data);
      if (!message) { return; }
      try {
        if (message.type === "SDP_ANSWER") {
          await pc.setRemoteDescription(message.charge);
          etape = "réponse reçue, établissement de la liaison";
          etapeVideo("reponse-recue");
          etatVideo(etape + "…");
          while (enAttente.length) { await pc.addIceCandidate(enAttente.shift()); }
        } else if (!pc.remoteDescription) {
          enAttente.push(message.charge);
        } else {
          await pc.addIceCandidate(message.charge);
        }
      } catch (erreur) {
        etatVideo("message de signalisation refusé par le navigateur (" + erreur.name + ")");
      }
    };
    ws.onclose = function (evenement) {
      if (video.ws !== ws || pc.connectionState === "connected") { return; }
      echecVideo("signalisation fermée (code " + evenement.code + ") à l'étape : " + etape);
    };
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
  // Tâches : action immédiate, sans prévisualisation ni boîte de dialogue — sauf « annuler » et « reprendre quand
  // même » (confirmation), et « reprendre », qui relance les lames : deux clics, le second dans les 5 s.
  var reprendreArme = 0;
  async function lancerTache(nom, bouton) {
    if ((nom === "annuler" || nom === "reprendre-quand-meme") && !window.confirm(nom === "annuler"
      ? "Annuler la tâche en cours ? Elle est ABANDONNÉE : le robot s'arrête sur place et se remet en attente."
      : "Reprendre quand même ? La position du robot est imprécise : la tonte reprise ainsi peut être erratique.")) { return; }
    if (nom === "reprendre" && !Ordres.secondClic(reprendreArme, Date.now())) {
      reprendreArme = Date.now();
      dire("Reprendre relance les lames : cliquez une seconde fois pour confirmer (5 s).", "alerte");
      if (bouton) { bouton.classList.add("a-confirmer"); window.setTimeout(function () { bouton.classList.remove("a-confirmer"); }, 5000); }
      return;
    }
    reprendreArme = 0;
    try {
      var reponse = await appeler("/api/tache", { nom: nom, confirmer: true });
      if (reponse.simulation) { dire("Simulation : « " + nom + " » parcouru, rien n'est émis.", "info"); }
    } catch (erreur) {
      dire("« " + nom + " » refusé : " + erreur.message, "alerte");
    }
  }
  function dire(texte, gravite) {
    if (window.LymowTableau) { window.LymowTableau.dire(texte, gravite); }
  }
  document.querySelectorAll("[data-tache]").forEach(function (bouton) {
    bouton.addEventListener("click", function () { lancerTache(bouton.dataset.tache, bouton); });
  });
  $("sequence-previsualiser").addEventListener("click", function () { previsualiser(sequence.slice(), false, objectifSequence); });
  $("sequence-vider").addEventListener("click", function () { sequence = []; afficherSequence(); });
  $("emettre").addEventListener("click", emettre);
  $("phares-bouton").addEventListener("click", async function () {
    // Un clic, sans prévisualisation : ordre sans mouvement. Le serveur dit ensuite, dans le fil, si le robot a obéi.
    try {
      var reponse = await appeler("/api/phares", { allumer: dernierPhares.allumer });
      if (reponse.simulation) { dire("Phares : simulation, rien n'est émis.", "info"); }
    } catch (erreur) {
      dire("Phares : refusé — " + erreur.message, "alerte");
    }
  });
  document.querySelectorAll("[data-voyants]").forEach(function (bouton) {
    bouton.addEventListener("click", async function () {
      // Voyants LED (pas les phares) : un clic, sans prévisualisation ; pendant un pas, le serveur met en attente.
      try {
        var reponse = await appeler("/api/voyants", { allumer: bouton.dataset.voyants === "allumer" });
        if (reponse.simulation) { $("voyants-etat").textContent = "Simulation : rien n'est émis."; }
      } catch (erreur) {
        $("voyants-etat").textContent = "Refusé : " + erreur.message;
      }
    });
  });
  document.querySelectorAll("[data-plateau-essai]").forEach(function (bouton) {
    bouton.addEventListener("click", async function () {
      if (!window.confirm(bouton.textContent + "\n\n" + Ordres.CONFIRMATION_ESSAI_PLATEAU)) { return; }
      try {
        await appeler("/api/plateau/essai", { ordre: bouton.dataset.plateauEssai, confirmer: true });
        $("plateau-essai-etat").textContent = "Ordre émis : le résultat s'affiche dans « Résultat ». Regardez le plateau.";
      } catch (erreur) {
        $("plateau-essai-etat").textContent = "Refusé : " + erreur.message;
      }
    });
  });
  $("nettoyage-essai").addEventListener("click", async function () {
    if (!window.confirm(dernierNettoyage.confirmation)) { return; }
    try {
      await appeler("/api/plateau/pas-leve", { confirmer: true });
      $("nettoyage-etat").textContent = "Essai autorisé 2 minutes : un pas à la fois, avec les flèches.";
    } catch (erreur) {
      $("nettoyage-etat").textContent = "Refusé : " + erreur.message;
    }
  });
  $("garde-carte").addEventListener("change", async function () {
    try {
      await appeler("/api/garde-carte", { arreter: $("garde-carte").checked });
    } catch (erreur) {
      $("pause-etat").textContent = "Réglage refusé : " + erreur.message;
    }
  });
  $("erreur-annuler").addEventListener("click", async function () {
    if (!window.confirm("Annuler la tâche en cours ?\n\nLa tâche est ABANDONNÉE : le robot s'arrête sur place et se remet en "
      + "attente. Effet sur une erreur de pare-chocs : inconnu — si l'erreur persiste, il faudra intervenir sur place.")) { return; }
    try {
      await appeler("/api/erreur/annuler", { confirmer: true });
    } catch (erreur) {
      $("erreur-suite").textContent = "Refusé : " + erreur.message;
    }
  });
  $("diagnostic-rtk").addEventListener("change", async function () {
    // Geste explicite de l'opérateur : le robot n'est interrogé que case cochée.
    try {
      await appeler("/api/diagnostic-rtk", { actif: $("diagnostic-rtk").checked });
    } catch (erreur) {
      $("diagnostic-rtk").checked = false;
      $("diagnostic-texte").textContent = "Diagnostic de localisation refusé : " + erreur.message;
    }
  });
  $("recalage").addEventListener("click", function () {
    // Mode assisté : l'aller-retour est chargé dans la séquence et prévisualisé ; rien n'est émis sans « Émettre ».
    var pas = Number($("pave-distance").value) || 2;
    sequence = Ordres.sequenceDeRecalage(pas);
    objectifSequence = null;
    recalageAvant = dernierEtat;
    recalageResultat = dernierResultat;
    afficherSequence();
    previsualiser(sequence.slice(), false);
    $("pause-etat").textContent = "Recalage assisté chargé : à n'émettre qu'en zone dégagée. Effet non garanti (jamais essayé sur ce robot).";
  });
  // Cadre de droite : deux onglets, « Caméra » et « Carte ».
  function montrerOnglet(nom) {
    ["camera", "carte"].forEach(function (onglet) {
      $("vue-" + onglet).hidden = onglet !== nom;
      $("onglet-" + onglet).setAttribute("aria-selected", String(onglet === nom));
    });
    // Masqué, le cadre de la carte n'a pas de dimension : elle se charge et se redessine à l'affichage.
    if (nom === "carte" && window.LymowCarte) { window.LymowCarte.montrer(); }
  }
  $("onglet-camera").addEventListener("click", function () { montrerOnglet("camera"); });
  $("onglet-carte").addEventListener("click", function () { montrerOnglet("carte"); });
  // « Rendre la main » : refusé pendant un mouvement ; flèches, direct et attestation tombent ; le robot en
  // télécommande est attendu ; puis deux choix — laisser en pause, ou reprendre la tâche (garde SEC-12) —
  // et la liaison est suspendue pour laisser l'application Lymow se connecter.
  var rendreSansAttendre = false;
  function avancerRendre() {
    if ($("rendre-choix").hidden) { return; }
    var etape = Ordres.etapeRendreLaMain({
      suspendue: liaisonSuspendue, occupe: occupe, ordres: dernieresOrdres, sansAttendre: rendreSansAttendre
    });
    var attente = etape === "attendre";
    $("rendre-message").textContent = attente
      ? "Le robot est encore en télécommande : il revient seul en pause après une trentaine de secondes. Patientez…"
      : (dernieresOrdres && dernieresOrdres.avertissement_position
        ? dernieresOrdres.avertissement_position + " « Reprendre la tâche » sera refusé dans cet état."
        : "Que doit faire le robot une fois la main rendue ? « Reprendre la tâche » redémarre les lames.");
    $("rendre-pause").disabled = false;
    $("rendre-pause").textContent = attente ? "Rendre la main sans attendre (robot laissé tel quel)" : "Laisser le robot en pause";
    $("rendre-reprendre").disabled = attente || !(dernieresOrdres && dernieresOrdres.reprise_possible);
  }
  async function rendreLaMain(choix) {
    rendreSansAttendre = true;
    try {
      var reponse = await appeler("/api/liaison/rendre", { sans_attendre: true, choix: choix });
      if (reponse.suspendue) {
        $("rendre-choix").hidden = true;
        $("pause-etat").textContent = "";
      } else {
        $("rendre-message").textContent = "La main n'a pas été rendue : " + (reponse.refus || reponse.message || "refus");
      }
    } catch (erreur) {
      $("rendre-message").textContent = "Refusé : " + erreur.message;
    }
  }
  function clicExplicite(evenement) {  // `detail` vaut 0 pour un clic né du clavier (Entrée, Espace)
    return Ordres.clicRendreAccepte({
      clavier: evenement.detail === 0, flechesActives: Boolean(window.LymowPave && window.LymowPave.flechesActives())
    });
  }
  $("rendre-main").addEventListener("click", async function (evenement) {
    if (!clicExplicite(evenement)) { return; }
    var etape = Ordres.etapeRendreLaMain({ suspendue: liaisonSuspendue, occupe: occupe, ordres: dernieresOrdres, sansAttendre: false });
    if (etape === "deja") { $("pause-etat").textContent = "La main est déjà rendue : « Prendre la main » la reprend."; return; }
    if (etape === "refus") { $("pause-etat").textContent = "Un mouvement est en cours : utilisez STOP, puis rendez la main."; return; }
    if (window.LymowPave) { await window.LymowPave.desactiverFleches(true); }
    rendreSansAttendre = false;
    $("rendre-choix").hidden = false;
    avancerRendre();
  });
  $("rendre-pause").addEventListener("click", function (evenement) { if (clicExplicite(evenement)) { rendreLaMain("pause"); } });
  $("rendre-reprendre").addEventListener("click", function (evenement) { if (clicExplicite(evenement)) { rendreLaMain("reprendre"); } });
  $("rendre-annuler").addEventListener("click", function () { $("rendre-choix").hidden = true; });
  $("pause-raccourci").addEventListener("click", async function () {
    // La pause seule, tout de suite. Aucun mouvement n'est enchaîné : c'est à vous de rappuyer.
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
  $("video-reessayer").addEventListener("click", demarrerVideo);
  // La politique de la page a bloqué une connexion (page chargée avant la connexion au compte) : dit, pas tu.
  document.addEventListener("securitypolicyviolation", function (evenement) {
    if (evenement.effectiveDirective === "connect-src" && videoOuverte) {
      echecVideo("signalisation bloquée par la politique de sécurité de la page : rechargez la page");
    }
  });

  window.addEventListener("pagehide", function () {
    if (videoOuverte && navigator.sendBeacon) { navigator.sendBeacon("/api/video/arreter"); }
  });

  // Échap et Espace = STOP, partout : Espace n'active donc jamais un autre bouton.
  function estToucheStop(evenement) {
    return evenement.key === "Escape" || evenement.key === " " || evenement.key === "Spacebar";
  }
  document.addEventListener("keydown", function (evenement) {
    if (!estToucheStop(evenement)) { return; }
    // Échap = STOP complet, toujours, comme Espace : jamais un simple retrait des flèches.
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
    previsualiser: function (ordres) { previsualiser(ordres, false); },
    lancerTache: function (nom) { return lancerTache(nom, null); }
  };
})();
