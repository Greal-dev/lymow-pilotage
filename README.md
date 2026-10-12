# lymow-pilotage

Commande `lymow` : pilotage à distance prudent d'un robot tondeuse Lymow One Plus
par le nuage du fabricant (AWS IoT MQTT). **Simulation par défaut** : sans
`--reel`, aucun ordre n'ouvre de connexion.

Outil non officiel. Le pilotage manuel par le nuage n'est pas prouvé dans les
sources étudiées (voir `AUDIT-SECURITE.md`) : premier essai robot à vue, lames
à l'arrêt, main sur le bouton STOP.

## Installation (Windows, Python 3.12)

Depuis la racine du projet, dans Git Bash :

```sh
/c/Users/aleau/python312-embed/python.exe -m pip install virtualenv
/c/Users/aleau/python312-embed/python.exe -m virtualenv .venv
.venv/Scripts/python.exe -m pip install --require-hashes -r requirements.lock
.venv/Scripts/python.exe -m pip install --no-deps --no-build-isolation -e .
.venv/Scripts/python.exe -m pytest
```

La distribution « embeddable » n'a pas le module `venv` : d'où `virtualenv`.
`requirements.lock` fige toutes les versions avec leurs empreintes (Windows,
Python 3.12) ; il se régénère avec
`uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 --python-platform windows`
(ajouter `setuptools`, utilisé pour l'installation locale).

## Commandes

| Commande | Effet |
|---|---|
| `lymow connexion` | Compte Google : affiche l'adresse à ouvrir, puis lit l'adresse de rappel `myapp://callback/?code=…&state=…` sans écho. `--mot-de-passe` : compte à adresse et mot de passe. La connexion Google peut aussi se faire depuis la page de `lymow ui`. |
| `lymow deconnexion` | Efface de ce poste l'accès au compte (les quatre entrées du coffre) ; rien n'est supprimé côté Lymow. |
| `lymow rappel-installer` / `rappel-desinstaller` | Active ou retire la connexion Google en un clic (gestionnaire Windows du protocole `myapp`, registre de l'utilisateur). |
| `lymow diagnostic` | Jeton présent, rafraîchissement, appareils, MQTT, réception d'un état, micrologiciel. Ne publie rien. |
| `lymow etat` | État du robot. `--passif` : n'émet aucune requête. `--attente S` : attente maximale (75 s). |
| `lymow capture` | Diagnostic, lecture seule : écoute `--secondes N` (30 par défaut, 120 au plus) et enregistre chaque message reçu tel quel dans `captures/<horodatage>/` (ou `--dossier D`) : une fiche par message, `index.jsonl`, et un vidage structurel du protobuf. `--carte` : demande aussi la carte. Ces fichiers contiennent l'origine GPS du jardin : ils restent hors du dépôt. |
| `lymow tourner (<degrés> \| --duree S) [--vitesse V]` | Pivote ; positif = gauche. 180° au plus. |
| `lymow avancer (<mètres> \| --duree S) [--vitesse V]` | Avance ; 2 m au plus. |
| `lymow reculer (<mètres> \| --duree S) [--vitesse V]` | Recule ; 2 m au plus. |
| `lymow plateau <mm>` | Hauteur du plateau de coupe, entier de 20 à 100 mm. |
| `lymow plateau-haut` | Raccourci : hauteur maximale (100 mm, réglable de 80 à 100). |
| `lymow stop` | Consigne nulle émise 3 fois, sans précondition, puis mise en pause de la tâche (userCtrl 3 ; 21 si le dernier état connu indique un retour à la base). |
| `lymow sequence "plateau 90; tourner 45 --vitesse 0.2; reculer --duree 2 --vitesse 0.1"` | Enchaîne ; rien ne part si une étape est refusable ; s'arrête au premier refus, échec ou constat non confirmé. Étapes : `tourner`, `avancer`, `reculer`, `plateau`, `plateau-haut`, `stop`. `--reel` et `essai-arret-auto` y sont interdits. |
| `lymow pause` | Met la tâche en pause (3), ou le retour à la base (21) : la pause de `stop`, sans les consignes nulles. |
| `lymow calibration-constat` | Hors ligne : relit les constats vérifiés des ordres réels du journal et donne, par vitesse, le rapport mesuré / commandé, sa dispersion et le facteur suggéré. Ne modifie rien. |
| `lymow reprendre` / `base` / `annuler` | Reprise de tâche (4), retour base tâche conservée (33), annulation sur place (28). |
| `lymow ui [--reel \| --demo] [--port N]` | Interface web locale avec retour vidéo (voir « Interface web locale ») ; `--demo` : robot et jardin fictifs, sans compte ni réseau. |
| `lymow carte` | Carte du robot : zones, zones interdites, passages, station (lecture seule ; contours complets avec `--json`). Une grande carte arrive coupée à 81 920 octets (constat du 2026-10-12) : les tranches suivantes sont demandées une à une (hypothèse à confirmer en réel), et une carte restée tronquée est refusée avec son motif. |
| `lymow ajuster-zone <nom\|id> --de I --a J --cm X [--cote court\|long\|tout] [--raccord M]` | Élargit ou rogne une bande le long d'un bord d'une zone (voir « Carte et ajustement de zone »). |
| `lymow restaurer-zone <sauvegarde.pb> [--zone nom]` | Remet le contour d'une zone depuis une sauvegarde. |
| `lymow essai-arret-auto [--vitesse V] [--observation S]` | Calibration, robot à vue : une seule consigne avant, puis silence, puis arrêt (voir « Calibration »). |

Options communes, avant ou après la commande : `--reel` (émettre vraiment),
`--json`, `--donnees DOSSIER` (journaux et `calibration.toml` ; par défaut la
variable `LYMOW_DONNEES`, sinon le dossier qui contient `pyproject.toml`).

Sans `--reel`, chaque ordre affiche ce qu'il enverrait : sujet MQTT, champs
décodés, hexadécimal, base64, vitesse, durée, déplacement commandé, nombre
d'émissions, cadence.

### Paramètres réglables par ordre

| Paramètre | Où | Bornes | Défaut |
|---|---|---|---|
| Angle | `tourner <degrés>` | 0 exclu à ±180° ; positif = gauche | obligatoire, sauf `--duree` |
| Distance | `avancer <mètres>`, `reculer <mètres>` | 0 exclu à 2 m, toujours positive | obligatoire, sauf `--duree` |
| Durée | `--duree S` (remplace l'angle ou la distance : exactement l'un des deux) | 0,1 à 9 s, et vitesse × durée ≤ 2 m ou 180° | aucune |
| Vitesse linéaire | `avancer`/`reculer --vitesse V` en m/s | 0,05 à 0,25 | 0,25 |
| Vitesse angulaire | `tourner --vitesse V` en rad/s (une seule forme, pas de °/s) | 0,05 à 0,35 | 0,35 |
| Sens | signe de l'angle ; `avancer` ou `reculer` ; avec `tourner --duree`, signe de `--vitesse` (négatif = droite) | — | gauche, avant |
| Hauteur de plateau | `plateau <mm>` | entier de 20 à 100 mm | aucune (`plateau-haut` : 100) |
| Essai | `essai-arret-auto --vitesse V --observation S` | 0,05 à 0,15 m/s ; 1 à 4 s | 0,10 m/s ; 3 s |

À vitesse réduite la durée plafonne la portée : `avancer 2 --vitesse 0.05`
demanderait 40 s et est refusé (au plus vitesse × 9 s). Avec `--duree`, la
consigne est tenue telle quelle : les facteurs de `calibration.toml` ne
s'appliquent pas.

## Interface web locale

**Tableau de bord (colonne de gauche)** — peu de texte, des boutons :

- l'état du robot en **pastilles** : statut, batterie, position ±x m (couleur
  selon la qualité), bord de zone d'après la carte, plateau, liaison ; une
  erreur apparaît en pastille rouge, qui ouvre le diagnostic ;
- un **pavé de flèches** cliquable, STOP au centre (en plus des flèches sur
  la caméra et du clavier) ;
- pas et allure en **groupes de boutons** : 0,25 · 0,5 · 1 · 2 m (touches 1
  à 4), 15 · 45 · 90°, auto · lente · normale ;
- une **grille d'actions** : Pause, Reprendre, Retour base, Annuler tâche,
  Plateau haut, Recalage assisté, Bon signal. Un bouton
  indisponible est grisé, sa raison en info-bulle. Ces ordres sans
  déplacement partent d'un clic, sans prévisualisation ; « Reprendre », qui
  relance les lames, demande deux clics (le second dans les 5 s) ; « Annuler
  tâche » et « Reprendre quand même » gardent une confirmation ;
- un **fil d'activité** de trois lignes (la plus récente en haut, horodatée,
  colorée selon la gravité) remplace les messages dispersés ; un
  avertissement non bloquant y passe, avec un rappel discret de 4 s sous
  l'en-tête — jamais une boîte de dialogue ;
- la séquence et la prévisualisation détaillée n'apparaissent que si
  « Composer une séquence » est coché (ou si une séquence est chargée) ;
- les explications sont en info-bulle : survolez un bouton, un titre de
  groupe ou une pastille.

Les paragraphes ci-dessous décrivent le détail de chaque fonction ; quand
ils parlent de « prévisualiser puis émettre » pour une tâche (pause,
reprise, retour à la base, plateau), lire : un clic sur le bouton du
tableau de bord. Les mouvements composés en séquence, eux, se
prévisualisent toujours avant d'être émis.

`lymow ui [--reel] [--port N]` (port 8377 par défaut) démarre un serveur sur
`127.0.0.1` uniquement — aucune option ne change cet hôte —, ouvre le
navigateur et s'arrête sur Ctrl+C. `lancer-lymow.cmd` (double-clic) lance
`lymow ui` en simulation.

La page tient en un écran de pilotage, sans défilement en 1366×768. En haut :
mode, état du robot en une ligne, et **trois boutons toujours visibles** :

- **Prendre la main** — un seul geste, une seule confirmation : rétablit la
  liaison si elle est suspendue ; si le robot tond ou rentre à sa base, le met
  en pause et attend que le robot l'annonce ; puis active les flèches. Si le
  robot reste non pilotable, rien n'est activé et l'attestation de
  l'opérateur est proposée. « Flèches : activer » fait la même chose.
- **Rendre la main** — refusé pendant un mouvement (STOP d'abord) ; les
  flèches, le pilotage direct et l'attestation tombent ; si le robot est
  encore en télécommande, la page attend son retour en pause ; puis deux
  choix : « Laisser le robot en pause » ou « Reprendre la tâche » (garde de
  localisation SEC-12 : refusée, la main n'est pas rendue) ; enfin la
  liaison est suspendue pour laisser l'application Lymow se connecter.
  La main n'est rendue que sur ce clic suivi de ce choix — jamais au clavier
  quand les flèches sont actives, jamais en différé. Tant qu'elle est
  rendue, l'outil n'ouvre aucune liaison et ne publie rien, par aucun
  chemin (veille, compte, vidéo, carte, diagnostic : refusés en clair) ;
  seuls STOP et « Prendre la main » la reprennent.
- **STOP** (Échap, Espace) — annule tout : mouvement en cours, file du
  pilotage direct, séquence composée, prévisualisation, armement ; puis
  émet l'arrêt (consignes nulles ×3) et la mise en pause de la tâche. Il ne
  rend pas la main et reste disponible main rendue (il rétablit alors la
  liaison pour émettre).

Dessous, deux
colonnes sur écran large (une seule, caméra d'abord, sur écran étroit) :

- **à gauche, les commandes** : statut du robot et précision de position, le
  gros bouton « Flèches », les pas ↑↓ et ←→, les actions (Pause, Reprendre,
  Retour base, Plateau haut), les autorisations et refus avec
  leurs boutons, la séquence et le retour des derniers pas ;
- **à droite, un cadre à deux onglets** : « Caméra » (flèches en
  surimpression) et « Carte » (position du robot avec son cercle
  d'incertitude, « suivre le robot », précision relevée).

La carte affichée est la dernière carte complète reçue, gardée sur disque par
appareil (`carte-reference/`, hors dépôt) : elle s'affiche dès l'ouverture, et
une réponse partielle du robot pendant une tâche ne la remplace pas — elle
est seulement signalée. Tout le reste est rangé sous les deux colonnes, dans
des sections repliées par défaut, dont l'état est mémorisé par le navigateur
(rien d'autre n'y est stocké) : « Ajustement de zone », « Ordres détaillés »,
« Calibration », « Compte » (ouverte d'elle-même si aucun compte n'est
connecté), « Journal et résultat détaillé ».

- Version. Au lancement, l'interface calcule une empreinte de son code et
  charge sa page en mémoire : la page servie reste cohérente avec le serveur,
  même si les fichiers sont mis à jour pendant qu'il tourne. L'empreinte et
  l'heure de démarrage figurent en tête de page. Relancer `lymow ui` après une
  mise à jour arrête proprement l'ancienne instance du même mode et démarre la
  nouvelle (« Ancienne version remplacée ») — sauf si un ordre y est en cours
  (« une ancienne version est en cours d'utilisation, fermez sa fenêtre »).
  Une instance d'avant cette fonction doit être fermée à la main. Serveur
  arrêté : la page affiche « Interface arrêtée — relancez le raccourci ».
- Une seule instance par port. Relancer `lymow ui` (ou le raccourci) alors
  qu'une interface du même mode tourne déjà ne démarre rien : l'instance en
  place rouvre elle-même la page dans le navigateur, avec un nouveau jeton à
  usage unique, et la seconde commande se termine (« interface déjà ouverte :
  page rouverte dans le navigateur », code 0). Si l'instance en place est dans
  l'autre mode, le lancement est refusé : fermez d'abord sa fenêtre. Si le port
  est pris par un autre programme : `lymow ui --port N`. La demande de
  réouverture est une route locale que les sites web ne peuvent pas atteindre
  (refusée dès qu'un en-tête `Origin` ou `Referer` est présent), limitée à une
  fois toutes les 3 s, et elle ne touche ni la session ni un mouvement en cours.
- Au double-clic, une erreur de démarrage reste affichée jusqu'à l'appui sur
  Entrée. Si le navigateur ne s'ouvre pas, l'adresse à ouvrir est affichée
  dans la fenêtre, qui reste ouverte.
- **Démonstration** : `lymow ui --demo` (incompatible avec `--reel`) ouvre la
  même page sur un robot et un jardin FICTIFS tenus en mémoire — deux
  parterres, une allée en équerre, un bassin et un massif interdits, deux
  passages dont celui de la station. Aucun compte n'est lu, aucun coffre,
  aucune connexion au cloud ; un bandeau permanent « DÉMONSTRATION — robot
  fictif » le rappelle. Les ordres et la retouche de zone y sont « émis » vers
  le robot fictif, qui se déplace et applique le contour : de quoi essayer
  l'interface de bout en bout, avec les mêmes règles. La vidéo et le compte y
  sont désactivés. Les sauvegardes de carte vont dans
  `sauvegardes-carte/demonstration/` et sont refusées pour une restauration
  réelle. Le robot fictif ne dit rien du vrai : il avance d'un pas par
  consigne reçue et acquitte tout.
- La page passe par les mêmes fonctions que la ligne de commande : mêmes
  validations, mêmes règles SEC-01 à SEC-07, même journal.
- Sans `--reel` au lancement, le serveur refuse tout ordre réel quoi que
  demande la page (et la session elle-même ne sait émettre que les requêtes
  pures). Avec `--reel` : bandeau rouge « MODE RÉEL », et chaque ordre se fait
  en deux temps, « Prévisualiser » puis « Émettre » ; le serveur n'accepte que
  la dernière prévisualisation qu'il a produite, une seule fois, pendant 60 s.
- STOP (bouton, Échap ou Espace) : sans prévisualisation ; il interrompt le
  mouvement ou la séquence en cours, puis applique le `stop` de la commande.
  Un seul ordre à la fois : tout autre ordre est refusé pendant un mouvement.
- Le mouvement vit dans le serveur : fermer l'onglet ne supprime pas son arrêt
  final. Ctrl+C pendant un mouvement arrête le robot, puis le serveur.
- Sans compte enregistré, la page fonctionne en prévisualisation et affiche
  « non connecté » : aucune connexion au cloud tant qu'on ne se connecte pas.
- **Connexion en un clic** (Windows). Quand le navigateur est déjà connecté à
  Google, la redirection vers `myapp://callback/…` est instantanée et l'adresse
  est impossible à copier à temps. Le bouton « Activer la connexion en un
  clic » du panneau « Compte » (ou `lymow rappel-installer`) inscrit Lymow comme
  gestionnaire du protocole `myapp` : une clé `HKCU\Software\Classes\myapp` dans
  le registre de votre session, dont la commande est
  `"<chemin de lymow.exe>" rappel "%1"`. Rien d'autre n'est modifié ; une clé
  `myapp` qui appartiendrait à un autre programme (dans votre session ou dans
  le registre de la machine) n'est ni écrasée ni retirée : son contenu est
  affiché. Si le dossier de l'outil a été déplacé, la clé est signalée « à
  réparer » : le bouton « Réparer » (ou `lymow rappel-installer`) la réécrit
  vers le `lymow.exe` courant.
  Pour annuler : bouton « Désactiver », ou `lymow rappel-desinstaller`. Une fois
  actif : « Se connecter avec Google », accepter quand Windows propose d'ouvrir
  Lymow, et la page se met à jour seule. `lymow rappel <adresse>` n'accepte
  qu'un seul argument, de forme stricte, et aucune option (l'argument vient
  d'un navigateur) ; il remet l'adresse à l'interface en cours par une route
  locale fermée aux sites web, qui ne l'accepte que si une tentative est active
  et que son `state` correspond. L'adresse n'est ni affichée ni journalisée.
  Le collage manuel ci-dessous reste disponible en repli.
- **Connexion depuis la page** (panneau « Compte », compte Google uniquement) :
  « Se connecter avec Google » ouvre la page de connexion dans un nouvel
  onglet ; la marche à suivre s'affiche (F12, onglet Réseau, « Conserver le
  journal » coché avant de valider, copier l'adresse `myapp://callback/?code=…`)
  et l'adresse de rappel se colle dans un champ masqué. Le serveur garde seul
  le vérificateur PKCE et le `state` (une tentative à la fois, 10 minutes,
  usage unique), puis passe par les mêmes fonctions que `lymow connexion` :
  `state` vérifié, échange, choix de l'appareil s'il y en a plusieurs,
  écriture au coffre, ouverture de la liaison. Le code expire en une minute
  environ : coller et valider sans attendre. « Se déconnecter de ce poste »
  efface les entrées du coffre, comme `lymow deconnexion`. Ni l'adresse de
  rappel, ni le code, ni aucun jeton n'apparaissent dans les journaux, dans la
  console du serveur ou dans les messages renvoyés à la page. Aucun mot de
  passe ne transite jamais par la page : la variante `--mot-de-passe` reste
  réservée à la ligne de commande. Ces actions sont refusées pendant un
  mouvement.
- Avec un compte enregistré, l'interface ouvre la liaison même en simulation,
  pour afficher l'état : elle n'émet alors que des requêtes pures (présence,
  demande de configuration, et demande de carte : une fois d'office à la
  première liaison si aucune carte n'est connue, puis sur « Actualiser la
  carte »),
  jamais un ordre ni une écriture de carte.
- En mode réel, ne fermez pas la fenêtre du terminal pendant un mouvement
  (voir « Ce que l'arrêt garanti ne couvre pas »).

Protection du serveur local : jeton d'adresse aléatoire de 256 bits par
lancement, à usage unique, échangé à la première requête contre un témoin
distinct, lui aussi aléatoire, `HttpOnly`, `SameSite=Strict` (l'adresse est
aussitôt redirigée sans le jeton, qui ne vaut plus rien ensuite) ; le témoin
est exigé sur toutes les routes ; en-tête `Host` limité à `127.0.0.1:port` et `localhost:port` ; `Origin` exigé
et vérifié sur toute requête `POST` ; aucune règle CORS ; politique de contenu
`default-src 'self'`, plus la seule signalisation vidéo
`wss://*.kinesisvideo.<région>.amazonaws.com` une fois un compte connu ; aucune
ressource externe (ni CDN, ni police, ni mesure d'audience).

### Vidéo — non vérifiée en réel

« Démarrer la vidéo » (jamais automatique) demande au serveur une session
Amazon Kinesis Video (canal, serveurs ICE, adresse de signalisation présignée :
secrets temporaires, jamais journalisés ni mis en cache), puis la page ouvre la
signalisation, envoie l'offre WebRTC en réception seule et affiche le flux. La
vidéo est une observation : elle est disponible aussi sans `--reel`. Attention :
« Démarrer la vidéo » contacte le cloud Lymow et active la caméra du robot,
même en simulation. Une seule session à la fois : un second démarrage est
refusé tant que la page n'a pas arrêté la première (ou n'a pas été rechargée).

Seule la logique pure est testée (messages, base64, ligne
`a=ice-options:trickle renomination` de l'offre ; ces tests demandent Node.js
et sont sautés sans lui). Le reste n'a jamais tourné contre le robot. Au
premier essai, ouvrir la console du navigateur (F12) et observer :

1. La requête `video/demarrer` répond 200 (sinon : message affiché sous la vidéo).
2. Le WebSocket vers `kinesisvideo` passe à l'état 101 ; un refus par la
   politique de contenu apparaît en rouge dans la console.
3. Un message `SDP_ANSWER` arrive après l'offre ; s'il n'arrive pas, le robot
   n'a pas reconnu l'offre ou l'identifiant de spectateur.
4. L'état affiché passe à « liaison vidéo : connected » ; « failed » indique un
   échec ICE (serveurs TURN, pare-feu).
5. L'image s'affiche ; sinon, `chrome://webrtc-internals` : images décodées,
   paquets reçus.
6. « Arrêter » ferme la signalisation et la liaison ; aucune commande d'arrêt
   de canal n'est connue côté service.

## Pilotage par flèches

But : écrire vite une suite d'étapes — se dégager, rejoindre une zone où le
réseau revient — et non piloter à vue. Les flèches sont sur la vidéo même.

- **Un seul geste : « Flèches : activer »** (ou un clic dans la caméra), avec
  une confirmation. Par défaut c'est le **pilotage direct** : chaque flèche
  fait bouger le robot tout de suite. La case « Composer une séquence (ne
  pas émettre tout de suite) » rend l'autre comportement : les flèches
  ajoutent des étapes, qui ne partent qu'avec « Prévisualiser » puis
  « Émettre ». Un liseré orange autour de la caméra montre l'activation.
  Elle ne tombe que par le bouton (qui interrompt aussi le pas en cours), par
  STOP — Échap ou Espace, toujours un STOP complet — ou par le désarmement du
  serveur (5 minutes sans action, statut non pilotable, arrêt non confirmé,
  liaison perdue) — plus en cliquant ailleurs.
- **Clavier.** Flèches actives, les touches ↑ ↓ ← →, Entrée et Retour arrière
  sont captées au niveau de la page entière : elles ne la font plus défiler,
  quel que soit l'élément qui a le focus ; elles ne sont pas captées dans un
  champ de saisie ou une liste. Une touche maintenue vaut un seul appui.
- **Robot en tonte ou en retour : un mouvement est refusé** (SEC-01), et la
  page le dit avant même d'essayer : « Le robot tond : mettez la tâche en
  pause pour prendre la main », avec le bouton « Mettre en pause » à côté (une
  confirmation, puis la pause seule : `userCtrl` 3, ou 21 pendant un retour à
  la base). Les flèches attendent ensuite le statut « pause » reçu du robot ;
  rien ne part tout seul après la pause. « Reprendre la tâche » reste proposé
  (les lames peuvent redémarrer). Limite connue : un retour à la base mis en
  pause donne un statut où aucun mouvement n'est permis.

- Quatre flèches (boutons, ou clavier quand le panneau a le focus) : ↑ avancer,
  ↓ reculer, ← tourner à gauche, → tourner à droite. Pas choisis dans deux
  listes : 0,25 / 0,5 / 1 (défaut) / 2 m et 15 / 45 (défaut) / 90°. Vitesses
  par défaut de l'outil. STOP reste Échap ou Espace, partout.
- **Composition (case cochée).** Chaque flèche ajoute une étape à la séquence de la
  page (la même liste que le compositeur). Une miniature trace le trajet cumulé
  et donne distance, rotation et durée estimée. Entrée prévisualise la
  séquence ; « Émettre » l'émet. Retour arrière annule la dernière étape.
- **Pilotage direct (défaut).** Chaque flèche émet aussitôt son
  étape, sans prévisualisation ; les appuis faits pendant un mouvement
  attendent dans une file de huit au plus, visible en pastilles. Bandeau
  « PILOTAGE DIRECT ARMÉ » tant que c'est armé. **La main n'est retirée que
  pour la sécurité ou sur demande** : STOP (qui vide la file), « Rendre la
  main », fermeture de la page, statut non pilotable sans attestation
  (SEC-01), arrêt non confirmé, liaison perdue (45 s sans aucune trame),
  5 minutes sans action sur la page (le temps restant ne s'affiche que dans
  la dernière minute). Elle n'est PAS retirée pour un patinage — « pas
  d'avance : patinage ? », autant de fois qu'il le faut —, un déplacement
  non vérifiable, un mauvais sens, un avertissement de carte, un refus de
  validation ou une liaison lente. « Prendre la main » ne demande
  confirmation qu'une fois par page ouverte : réarmer après STOP est un
  clic. Mesuré avec le robot fictif : 0,2 s entre deux pas de la file. La route du serveur n'accepte que ces quatre ordres aux
  pas des listes — ni plateau, ni tâche, ni vitesse, ni séquence libre — et
  chaque pas passe par les mêmes règles que tout ordre (SEC-01 à SEC-03, un
  mouvement à la fois, journal). Sans `--reel` : même parcours, rien n'est
  émis.
- Le clavier n'agit que si le panneau a le focus (clic dedans, ou « Activer le
  clavier »), jamais pendant une saisie ; une touche maintenue vaut un appui.
- **Avertissement.** L'essai d'arrêt automatique (`essai-arret-auto`) n'a pas
  encore été fait sur ce robot : on ignore s'il s'arrête seul quand les
  consignes cessent. Si la liaison tombe pendant un pas, son comportement est
  donc inconnu, et l'arrêt garanti de l'outil ne peut plus l'atteindre. En
  zone de mauvaise couverture, préférez des pas courts (0,25 ou 0,5 m), robot
  à vue ou vidéo ouverte. Non vérifié en réel : ce panneau n'a été testé que
  hors ligne.

### Robot en erreur

Constat de l'essai réel du 2026-10-12 : en statut « en erreur » (erreur 44,
pare-chocs enfoncé), cinq « reculer 1 » émis et acquittés n'ont produit
aucun déplacement. **En erreur, le robot ignore le pilotage manuel**, et le
protocole ne connaît aucun ordre « acquitter l'erreur ».

- Un encadré rouge dit l'erreur en français, avec son libellé d'origine
  (« 44 : pare-chocs enfoncé (Bumper stuck) »), et propose **« Annuler la
  tâche et remettre le robot en attente »** : l'ordre « annuler » déjà connu
  de l'outil (28), sur confirmation — la tâche en cours est abandonnée. La
  page attend ensuite le statut « prêt » : « les flèches sont de nouveau
  utilisables — reculez pour dégager le pare-chocs ». Si le statut reste « en
  erreur » : « l'erreur persiste : intervention sur place nécessaire, ou
  essayez l'application Lymow » — rien n'est retenté de lui-même. **L'effet
  de cet ordre sur une erreur de pare-chocs n'a jamais été essayé.**
- Près des flèches : « Robot en erreur : il ignorera probablement cet
  ordre. » Le pas reste possible. « Prendre la main » ne tente aucune pause.
- « Le robot n'a pas bougé » ne retire plus la main au premier constat : il
  est affiché avec sa cause probable (« en erreur : ordre ignoré »,
  « patinage possible », « position transmise trop rarement : non
  vérifiable ») ; le pilotage direct n'est jamais désarmé pour cela : c'est
  quand le robot patine qu'on a besoin d'insister.

## Localisation

Ce qui est **observé** (trames réelles du 2026-10-12) :

- le robot annonce à chaque trame le nombre de satellites, une précision
  horizontale en mètres, une qualité et l'état de son nœud de localisation ;
- la précision est passée de 0,03 m (matin, station) à 1,1–1,5 m après une
  perte de signal, puis est revenue à 0,01 m, robot immobile, sans aucun saut
  de la pose : on ne peut pas dire si la pose était juste ou si le filtre du
  robot ne s'est pas recalé ;
- l'origine de la carte est codée en simple précision.

Ce qui est **documenté par le socle** (ha-lymow), non encore vu dans nos
captures : le robot n'envoie le détail RTK qu'à un client qui le demande
(demandes 57 et 58, toutes les 5 s) — âge des corrections de la base, état
de la base, taux d'erreur du lien radio, motif d'erreur. L'outil sait émettre
ces deux demandes comme **requêtes pures** (forme exacte, contrôlée à
l'octet), mais **jamais d'office** : seulement si vous cochez « Diagnostic
de localisation (interroge le robot toutes les 5 s) », dans le bloc d'état
à gauche (la réponse s'affiche à côté ; sans réponse après 20 s : « le
robot ne répond pas à la demande de diagnostic ») —
décoché par défaut, décoché de nouveau quand la main est rendue, jamais
pendant un mouvement — ou par `lymow capture --rtk`. Ce que les sources
établissent : le socle nomme ces codes « QUERY » et les émet tels quels ;
son relevé d'écrans associe ces octets à l'ouverture de l'écran « RTK
Diagnostic » de l'application. Nous ne l'avons pas constaté nous-mêmes. La page affiche alors « corrections de la base reçues il y a 2 s »
ou « plus de corrections depuis 45 s » : c'est ce qui distingue un lien de
base perdu d'un ciel masqué.

Ce qui est **supposé** : les coordonnées brutes du robot (champ 26 du
schéma) permettraient de mesurer l'écart entre position satellite et
position calculée. L'outil le calcule quand elles arrivent, l'affiche, et le
trace sur la carte au-delà de 0,20 m. La garde de reprise (SEC-12) exige
alors un écart de 0,30 m au plus — **seulement si ces coordonnées sont en
double précision** ; en simple précision (cas de l'origine de la carte),
l'écart n'est qu'indicatif et ne bloque rien. Tant qu'aucune trame réelle
n'a été capturée, cette partie n'est vérifiée que sur des trames construites
d'après le schéma.

**Aucune commande de recalage n'est émise.** Les codes 47 et l'entrée de
localisation du schéma n'ont jamais été essayés par personne : l'outil ne les
encode pas, et sa liste blanche les refuse. Le « recalage assisté » proposé
quand l'écart est trop grand ne fait que charger dans la séquence un
aller-retour en ligne droite (deux pas en avant, deux en arrière), à
n'émettre qu'en zone dégagée : pratique décrite par la communauté, effet non
garanti, jamais essayé sur ce robot. Sinon : retour à la base, ou
redémarrage du robot.

## Obstacles : ce que l'outil protège, ce qu'il ne voit pas

On ignore si le robot garde sa propre détection (ultrasons, vision,
pare-chocs) en pilotage manuel : rien dans les sources ne le dit, et les
550 trames reçues pendant les pilotages réels du 2026-10-12 ne portent
aucun code d'erreur ni d'avertissement, ni aucun champ de perception —
mais aucun obstacle n'a été approché ce jour-là. L'outil se protège donc
avec ce qu'il voit :

- **la carte** (SEC-13) : il avertit quand un pas sortirait des zones
  connues — et ne l'empêche que si la case « Arrêter aux bords de zone »
  est cochée ;
- **les indices de choc** (SEC-14) : il s'arrête si le robot annonce un
  choc ou cesse d'avancer — donc APRÈS le contact, pas avant ;
- **l'allure** : lente près d'un bord, quand la position est fiable.

Il **ne voit pas** : un obstacle absent de la carte (mur non cartographié,
mobilier, outil laissé au sol), une personne, un animal. Le pilotage se
fait robot à vue ou caméra ouverte, main sur STOP.

**Essai conseillé, robot à vue** : en pilotage manuel, allure lente, pas de
0,25 m, approcher un carton vide posé sur la pelouse. Si le robot s'arrête
de lui-même avant ou au contact, sa détection reste active en pilotage
manuel ; noter alors le code affiché. S'il pousse le carton, elle ne l'est
pas : seuls les garde-fous ci-dessus protègent.

## Éclairage : voyants LED et phares

**Voyants LED** (« Vehicle LED » de l'application) — `lymow voyants
allumer|eteindre`, et deux boutons dans la section repliée « Ordres
détaillés ». Forme émise : celle que le socle a capturée (configuration du
robot, signal 10 pour allumer, 11 pour éteindre). **Ce ne sont pas les
phares** : en essai réel (2026-10-12), le signal 10 est parti six fois sans
allumer les phares — les voyants étaient d'ailleurs déjà allumés (niveau 3
toute la journée d'après la configuration du robot).

**Phares** (éclairage avant, celui de la caméra) — **lecture seule** : la
pastille « phares allumés / éteints » et son info-bulle donnent le niveau
annoncé par le robot et la plage d'allumage réglée dans l'application.
Observé dans les trames réelles du 2026-10-12 : éteints le matin (aucune
plage), allumés au niveau moyen entre 20:26 et 21:30 pendant la plage
20:30–21:30, éteints après. **L'outil n'a pas de bouton « Phares »** :

- attesté par capture de l'application (par le socle, en Bluetooth) : le
  réglage « Headlight Mode », une plage horaire quotidienne ; l'extinction
  y porte le signal 7 ;
- non attesté par capture : l'allumage immédiat (signal 6, niveaux 15 et
  16) — connu seulement par le code de l'application et par une intégration
  tierce qui dit l'avoir vérifié.

Pour l'obtenir avec certitude : capturer ce que l'application officielle
envoie quand on appuie sur son bouton d'éclairage dans la vue caméra (on
attend `10 31 6a 02 40 06`), ou décider d'essayer ce signal en ordre
expérimental. En attendant, les phares s'allument en réglant leur plage
dans l'application.

## Essai dans un vrai navigateur

`python outils/essai_navigateur.py <dossier> [recalage] [boutons]` lance
l'interface de **démonstration** (robot fictif, aucun réseau, rien n'est
émis) et la pilote dans Edge ou Chrome sans affichage : clics de souris
réels, touches, boîtes de dialogue, captures d'écran, erreurs de console.
Il n'est pas lancé par les tests. Le scénario « recalage » ouvre la carte,
pointe le robot d'un clic réel (y compris un clic « tremblé ») et vérifie
le décalage ; « boutons » clique chaque bouton du tableau de bord et relève
ce que le journal de démonstration en dit.

## Ce que le diagnostic de localisation a réellement répondu

169 réponses réelles, le 2026-10-12 de 20:41 à 20:53 (85 de niveau 1, 84 de
niveau 2), relues hors ligne :

- âge des corrections de la base : 2 s (79 fois), 4 s (5 fois) ;
- base en ligne, taux d'erreur du lien radio 0 %, sur toutes ;
- précision annoncée de 0,03 à 0,105 m (médiane 0,046 m), identique à
  celle des trames d'état ; 23 à 29 satellites ;
- **aucune position** : ni le niveau 1 ni le niveau 2 ne portent la position
  satellite brute, et aucune trame ne porte les coordonnées du robot.

L'écart « position satellite / position calculée » **ne peut donc pas être
calculé** avec ce que le robot transmet : ce n'est ni une erreur de
décodage, ni une affaire de repère. La question « le robot se recale-t-il ? »
reste sans réponse directe ; ce que ces réponses établissent, c'est que ce
soir-là les corrections arrivaient normalement — un décalage constaté à ce
moment ne vient pas d'un lien de base perdu.

## Hauteur du plateau

Le robot **ne transmet aucune hauteur mesurée du plateau** : dans les
trames reçues le 2026-10-12, ni la sortie de base (qui porterait la hauteur
de coupe), ni la hauteur de la configuration du robot ne sont présentes ; et
le « plateau 100 » réel émis ce jour-là n'a été suivi d'aucune hauteur.

Ce qui se lit : la **consigne enregistrée par le robot**, dans la carte
(réglages globaux de tonte, hauteur de coupe confirmée par le socle contre
l'application ; ou réglages de la tâche en cours, s'ils sont présents). La
carte de référence du 12/10 à 19:38 donne 96 mm. La page l'affiche à gauche,
avec une jauge : « Plateau : 96 mm (maximum 100) — consigne enregistrée par
le robot (…), pas une hauteur mesurée », « au maximum » quand c'est le cas,
« hauteur inconnue : le robot ne l'a pas transmise » sans carte.

Après un ordre de plateau, la carte est relue (requête pure déjà permise,
après l'ordre, jamais pendant un mouvement) et la page dit : « consigne
passée de 60 à 100 mm », ou « valeur relue inchangée : l'ordre n'a pas été
pris en compte, ou cette lecture ne reflète pas la position du plateau ».
On ne sait pas encore lequel des deux : à constater au premier essai réel.

Aucune demande de lecture nouvelle n'a été ajoutée : le socle sait émettre
la demande 51 (réglages en cours) mais dit lui-même ne pas en décoder la
réponse, et aucune capture ne l'atteste.

### Levée de nettoyage du plateau (essai)

L'application sait lever le plateau au-delà de sa course de coupe, pour
nettoyer les lames. Ce que les sources en disent :

- **attesté** : l'application connaît deux signaux, « levée en un geste »
  (8) et « redescente » (9), dans la même énumération que les phares ; et
  la forme du message des phares par le nuage a été capturée par le socle
  (`10 31 6a 02 40 0a` : configuration du robot, signal 10) ;
- **supposé** : que le robot accepte les signaux 8 et 9 par le nuage. Deux
  autres sources les rangent parmi les signaux « Bluetooth seulement » ; le
  socle ne les émet jamais ;
- **absent** : tout essai réel par leurs auteurs, la hauteur atteinte, le
  comportement du robot dans cette position (peut-il rouler ? erreurs de
  capteur de soulèvement 7 et 8 ?), la façon dont il en sort.

L'outil propose donc deux ordres **expérimentaux**, dans la section repliée
« Calibration » : « Plateau : position de nettoyage (essai) » et « Plateau :
redescendre (essai) ». Mode réel seulement ; statut du robot sur la liste
blanche SEC-01, sans attestation possible ; confirmation au texte
explicite ; journalisés ; suivis d'une relecture de l'état et d'un constat
(le plus souvent « aucun changement observable par l'outil » : regardez le
plateau). Seuls les numéros 8 et 9 peuvent sortir : la même énumération
porte l'extinction du robot. Aucune séquence automatique « lever, avancer,
rabaisser » : enchaînez à la main (lever, une flèche de 0,25 m,
redescendre) après avoir constaté l'effet réel. Si rien ne se passe, c'est
que le geste est bien réservé au Bluetooth : il faudrait alors capturer ce
que l'application envoie quand on appuie sur sa levée de nettoyage.

## Carte décalée : mesure et recalage d'affichage

Constat du propriétaire sur place (2026-10-12) : la position affichée est
décalée d'environ un mètre par rapport au robot réel.

**Mesure à la station.** À chaque arrivée à la station (statut « en
charge »), l'outil compare la dernière position annoncée à la station de la
carte, journalise l'écart (`ecart-station`) et l'affiche : « écart
carte/robot mesuré à la station : 0,9 m vers l'est, le 12/10 à 20:40 ». Un
écart stable et non nul, d'arrivée en arrivée, mesurerait un décalage de
carte.

Ce que disent les données du 2026-10-12, relues hors ligne : **une seule**
présence sur la station (11:12–11:23, batterie de 96 à 99 %, 42 positions
identiques) ; écart position − station : 0,46 m vers l'ouest, 0,04 m vers
le nord. L'écart est dans l'axe de la station (latéralement : 4 cm) ; il
peut tenir à la géométrie de l'accostage (point de référence du robot,
point de la station) autant qu'à un décalage de carte. Aucune arrivée « en
charge » n'a été enregistrée ce jour-là, et la précision n'était pas encore
relevée le matin. **Un décalage stable n'est donc pas établi** : il faut
plusieurs arrivées. Le mètre constaté le soir suit une perte de signal
(précision de 1 à 4,7 m l'après-midi) ; il peut venir de l'estimation du
robot plutôt que de la carte.

**« Le robot est ici »** (onglet Carte) : cliquez le bouton, puis l'endroit
réel du robot sur la carte. L'outil enregistre le décalage entre la
position annoncée et l'endroit indiqué — en pointage, le curseur est une
croix, un bandeau le rappelle, Échap l'annule, et la carte ne glisse pas :
c'est l'endroit où vous relâchez qui compte ; un trait relie ensuite
l'ancienne et la nouvelle position, avec la valeur (« décalage appliqué : 1,2 m vers
l'est, 0,4 m vers le nord »), le mémorise avec sa date (`recalage.json` du
dossier de données, lu aussi par la commande) et l'applique partout où il
place le robot : marqueur et cercle d'incertitude, avertissement de bord de
zone, cible et trajet « bon signal », relevé affiché (le fichier du relevé
garde les valeurs brutes, avec le décalage du moment). La mention « carte
recalée par l'opérateur » reste affichée ; « Annuler le décalage » le
retire. Si un écart a été mesuré à la station, « Utiliser l'écart mesuré à
la station » l'applique en un clic. 20 m au plus. La dernière position
annoncée sert de repère quel que soit son âge.

**Ce recalage ne concerne que l'affichage de l'outil : rien n'est envoyé au
robot.** Si la carte du robot est réellement décalée, le robot lui-même
navigue et tond avec cette carte décalée. L'outil ne peut pas corriger la
carte interne en bloc : seul le contour d'UNE zone est modifiable, les
zones interdites et les passages ne le sont pas. La correction durable
passe par l'application Lymow (recalage de la station, ou nouvelle
cartographie).

**Liaison lente.** Si aucune trame d'état fraîche n'arrive dans les 12 s qui
suivent un pas direct, la main n'est plus retirée : la page affiche
« liaison lente : en attente d'une nouvelle trame du robot… », les appuis
restent en file et le pas suivant ne part qu'à l'arrivée d'une trame. La
main n'est retirée qu'après 45 s sans aucune trame. STOP reste immédiat.

## Retour vers le bon signal

But : quand le robot a perdu sa position précise, le ramener là où le signal
était bon, puis reprendre la tonte. **Mode assisté seulement : rien ne
s'exécute tout seul.**

- **Relevé.** Tant que l'interface est ouverte et reliée au robot — tonte
  normale comprise, en écoute seule —, la précision annoncée est relevée par
  position, avec sa date (`releve-precision.jsonl`, hors dépôt). La carte
  l'affiche (case « précision relevée ») ; « derniers 30 jours » écarte les
  relevés anciens.
- **Bon signal le plus proche** (onglet « Carte »). Les points relevés à
  0,10 m ou mieux sont regroupés ; la cible proposée est le groupe le plus
  proche du robot situé dans une zone de tonte (à 0,5 m au moins de son
  contour) ou sur un passage, hors zones interdites. La carte montre la
  cible, la distance et le cap à prendre.
- **Trajet proposé.** Ligne droite si elle tient dans les zones et les
  passages ; sinon un contournement simple par les sommets des contours ;
  sinon l'outil le dit et ne propose que le cap. Le trajet est découpé en pas
  admis par SEC-02 (avances de 2 m au plus, rotations sous l'angle maximal
  demandable). « Charger le trajet dans la séquence » le place dans la
  séquence : vous prévisualisez et émettez comme d'habitude, ou vous pilotez
  à la main en suivant le tracé. Pendant son exécution, la séquence s'arrête
  d'elle-même dès que la précision redevient fiable (« objectif atteint »).
- **Ensuite.** Position fiable, robot en pause : la page propose « Reprendre »
  (la garde SEC-12 passe ; les lames redémarrent).
- **Limites.** Quand le signal est perdu, la position du robot est
  incertaine : son point porte un cercle au rayon annoncé et le trajet est
  dit « indicatif, surveillez la caméra ». Les obstacles non cartographiés
  sont ignorés. Le calcul est borné à 3 s : sur une grande carte, un détour
  long à travers plusieurs zones n'est souvent pas trouvé, et seul le cap est
  proposé. Jamais essayé en réel.

## Calibration (robot à vue, zone dégagée, main sur le bouton STOP)

1. **`lymow essai-arret-auto --reel` en premier.** But : savoir si le robot
   s'arrête seul quand les consignes cessent, ce dont dépend tout le reste de
   l'exposition. L'outil émet une seule consigne avant (0,10 m/s), n'émet rien
   pendant 3 s tout en surveillant le statut, puis envoie l'arrêt garanti.
   Il affiche ensuite le déplacement mesuré et une lecture prudente : « le
   robot semble conserver la dernière consigne », « le robot semble s'arrêter
   seul » ou « constat instrumental impossible, fiez-vous à l'observation
   visuelle ». **L'observation visuelle fait foi.** Exposition maximale :
   0,15 m/s × 4 s = 0,6 m, plus le délai d'arrêt (jusqu'à 20 s d'essais si la
   liaison tombe à ce moment-là, soit 3 m de plus à 0,15 m/s).
2. `lymow tourner 10 --reel` : sens de rotation (`signe_angulaire`).
3. `lymow avancer 0.5 --reel`, puis `lymow tourner 90 --reel` : échelles
   (`facteur_distance`, `facteur_angle`).
   Constat du 2026-10-12 (premier ordre réel, depuis la page) : `reculer 2`
   à 0,25 m/s a produit environ 2 m réels, mesurés à l'œil par le
   propriétaire — facteur de distance voisin de 1,0 à cette vitesse, trame
   variante `mqtt`, version 49. `calibration.toml` n'a pas été modifié ;
   Constat du même jour, 15 h 32 à 15 h 39 (`lymow calibration-constat` sur le
   journal réel) : 12 rotations de 45° commandées à 0,35 rad/s, 30 à 42°
   mesurés, rapport mesuré / commandé 0,788 → **`facteur_angle = 0.79`
   appliqué** dans `calibration.toml` (décision du propriétaire). « tourner
   45 » dure donc 2,84 s au lieu de 2,24 s, et l'angle maximal demandable par
   ordre devient 142° : « tourner 180 » est refusé avec ce maximum (SEC-02 :
   jamais plus de 180° commandés). Distance : rapport 0,94 sur 11 pas à
   0,25 m/s (0,81 à 1,02), **non appliqué** — `facteur_distance` reste 1.0.
   Réserve : ces mesures sont la position annoncée par le robot, alors
   imprécise de plus d'un mètre ; à reprendre position fiable.
4. Vérifier à l'œil que les lames sont à l'arrêt dans les statuts pause (3),
   erreur (7) et télécommande (6).
5. `lymow plateau 60 --reel` puis `lymow plateau-haut --reel` : effet réel de
   la hauteur de coupe robot à l'arrêt.

## Règles de sécurité (codées en dur, aucune option ne les lève)

- **SEC-01 Lames** : avant chaque mouvement réel, l'outil sollicite le robot
  et exige un état reçu dans la session, après cette demande, vieux de 30 s au
  plus (les messages conservés par le serveur, dits `retain`, sont ignorés),
  une présence en ligne explicitement confirmée, et un statut « lames à
  l'arrêt » : 0 repos, 1 attente, 5 charge, 12 charge terminée,
  6 télécommande, ainsi que 3 pause et 7 erreur (ajoutés sur décision du
  propriétaire le 2026-10-11 ; arrêt des lames dans ces deux statuts à
  constater robot à vue lors de la calibration). Tout le reste refuse : tonte
  (2), reprise (8), tonte de zone (9), retour à la base (4, 10, 14), arrêt
  d'urgence (13), mise à jour, inconnu, hors ligne.
  **Le statut jugé est le statut VIVANT du robot**, `robotStatus` (champ 1 de
  `robotInfo`), et lui seul. Constat du 2026-10-12, trames réelles :
  `workStatus` (champ 6), jugé jusque-là, reste à 2 (tonte) pendant toute la
  tâche, pause comprise — l'outil refusait donc à tort un robot en pause.
  Valeurs observées (champ 1 / champ 6) : en charge 5/1, au repos 1/1, tâche
  en pause 3/2, télécommande 6/2. Le champ 6 n'est plus qu'une information
  (« tâche : tonte en cours »). Une trame sans champ 1 laisse le statut
  inconnu : refus, jamais de repli sur le champ 6. La liste elle-même n'a pas
  changé. Le statut est revérifié à chaque consigne : s'il sort de la
  liste ou si le robot passe hors ligne, arrêt immédiat. L'outil ne sait
  émettre aucun ordre de démarrage de tonte ; seul `reprendre` peut relancer
  les lames.
- **SEC-01b Attestation de l'opérateur** (décision du propriétaire,
  2026-10-12). Elle porte, comme tout SEC-01, sur le statut VIVANT du robot
  (`robotStatus`, champ 1). Le constat qui l'a motivée — robot en pause,
  lames arrêtées à vue, « statut 2 » publié — venait du champ 6, qui n'est
  plus jugé : un robot en pause est désormais admis sans attestation. Elle
  reste utile si le robot se déclare vraiment en tonte ou en retour alors que
  l'opérateur voit ses lames arrêtées. Règle : un mouvement manuel (`tourner`, `avancer`, `reculer`, flèches
  comprises) est admis si le statut est dans la liste de SEC-01, **ou** si
  une attestation « lames à l'arrêt constatées par l'opérateur » est active
  et que le robot se déclare en tonte (2, 8, 9) ou en retour (4, 10, 14).
  Aucune attestation ne lève le refus en arrêt d'urgence (13), mise à jour
  (11), essais d'usine (15, 16), hors ligne, statut absent ou inconnu. Le
  reste de SEC-01 est inchangé : état frais, présence en ligne, un mouvement
  à la fois, statut revérifié à chaque consigne (avec attestation, un
  statut 2 n'interrompt pas ; un passage à 13, 11 ou hors ligne interrompt
  toujours, comme l'expiration ou le retrait de l'attestation en cours de
  mouvement). SEC-02, SEC-03 et les suivantes sont inchangées.
  - *Vie de l'attestation.* 10 minutes au plus, dans la mémoire du processus
    seulement : jamais active au lancement, jamais écrite sur disque, aucune
    variable d'environnement ni clé de `calibration.toml` ne la pose. Elle
    tombe sur STOP, à l'arrêt du serveur, à la réouverture de la liaison, dès
    que le robot passe dans un statut toujours refusé ou hors ligne, et à la
    demande (« Retirer »).
  - *Dans la page* (`lymow ui --reel`, ou `--demo` ; refusée en simulation et
    pendant un mouvement) : quand le statut seul bloque, la ligne de statut
    du bloc Pilotage propose d'abord « Mettre en pause », puis « Le robot se
    déclare : … » et le bouton « Je vois le robot, lames à l'arrêt :
    autoriser le pilotage 10 min », avec une confirmation explicite. Tant
    qu'elle est active : bandeau orange en haut de page, avec le temps
    restant et « Retirer ».
  - *En ligne de commande* : `--lames-arretees-constatees`, avec `--reel`
    seulement, sur `tourner`, `avancer`, `reculer` et `sequence` ; elle vaut
    pour cette commande.
  - *Journal.* Chaque pose, retrait et expiration y figure
    (`attestation-posee`, `attestation-retiree`, `attestation-expiree`), et
    chaque ordre émis sous attestation porte la mention et le statut déclaré.
  - *Limites.* **L'outil ne peut pas vérifier ce que l'opérateur atteste** :
    si les lames tournent, il fera bouger un robot lames en marche. À
    n'utiliser que robot à vue. Le plateau de coupe et l'essai d'arrêt
    automatique n'en bénéficient pas : ils restent sur la liste de SEC-01.
    Non vérifié en réel.
- **SEC-02 Plafonds** : 2 m, 180°, 0,25 m/s, 0,35 rad/s, 9 s par mouvement ;
  le déplacement réellement commandé (vitesse × durée) respecte les mêmes
  2 m et 180° quel que soit le facteur de calibration ; `plateau-haut` entre
  80 et 100 mm ; NaN, infini, nombres exotiques (`1_0`, chiffres non ASCII) et
  mouvements trop courts pour une émission refusés.
- **SEC-03 Arrêt garanti** : consigne republiée à 10 Hz ; à la fin, sur erreur,
  sur Ctrl+C ou sur Ctrl+Break, trois arrêts publiés en parallèle en QoS 1,
  acquittements vérifiés. Une publication manquée ou une liaison perdue
  interrompt le mouvement, puis l'arrêt est retenté après reconnexion (sans
  abonnements) jusqu'à une échéance fixe de 20 s. Pendant l'arrêt, Ctrl+C et
  Ctrl+Break sont ignorés (« Arrêt en cours ») et rien n'est écrit au journal.
  Sans les 3 acquittements : « ARRÊT NON CONFIRMÉ » en tête sur la sortie
  d'erreur et code de sortie 3.
- **SEC-04 Constat** : après chaque ordre réel, position et cap avant/après et
  déplacement mesuré. La position de départ doit être aussi fraîche que l'état
  de SEC-01, sinon « constat impossible ». Sont signalés : « le robot n'a pas
  bougé » (sous 5 cm ou 3°), un déplacement dans le mauvais sens, un
  déplacement nettement supérieur à l'ordre (plus de 1,5 fois). Le constat
  attend une trame d'état fraîche, 12 s au plus. Constat du 2026-10-12 : le
  robot réel envoie son état toutes les demi-secondes après un ordre, mais
  pas sa position ; l'outil conclut alors aussitôt « position non fournie
  par le robot : déplacement non vérifiable par l'outil (utilisez la
  vidéo) » — un constat normal. Sans aucune trame d'état en 12 s : « liaison
  douteuse ».
- **SEC-05** Simulation par défaut, `--reel` à chaque commande, en toutes
  lettres (aucune abréviation d'option).
- **SEC-06** Journal `journaux/AAAA-MM-JJ.jsonl` de chaque ordre simulé ou émis
  et de chaque état reçu, filtré de tout secret.
- **SEC-07** Séquence : toutes les étapes sont validées avant la première
  émission (un plafond dépassé et rien ne part). Révisé le 2026-10-12 au vu
  du réel : elle s'interrompt si un ordre est refusé ou échoue, si l'arrêt
  n'est pas confirmé, si le statut reçu sort de la liste blanche, si aucune
  trame d'état fraîche n'arrive dans le délai, ou si une position EST
  fournie et contredit l'ordre (« n'a pas bougé », « mauvais sens ») — plus
  parce que le robot ne fournit pas sa position. Chaque étape repasse par
  SEC-01 (état frais). Le bilan dit pour chaque étape « émise, vérifiée »,
  « émise, non vérifiable »…, pourquoi la séquence s'est arrêtée, et les
  étapes restantes ; la page propose « Reprendre à l'étape N », qui repasse
  par Prévisualiser puis Émettre.
- **SEC-12 Reprise et qualité de localisation** (constat du 2026-10-12 :
  après une perte de signal, la position annoncée suit les manœuvres mais
  reste imprécise de 1,1 à 1,5 m, et la tonte reprise ainsi « fait n'importe
  quoi »). La qualité se lit sur la précision horizontale en mètres que le
  robot annonce à chaque trame : **fiable** jusqu'à 0,10 m, **dégradée**
  jusqu'à 0,50 m, **perdue** au-delà ou si elle est inconnue. Elle est
  affichée dans l'en-tête et sous la vidéo (« position : ±1,32 m —
  perdue »). `reprendre` est refusé quand elle n'est pas fiable, avec ce
  qu'il faut faire ; « Reprendre quand même » (page) ou
  `lymow reprendre --position-imprecise-acceptee` lève la garde, sur
  confirmation, et le journal le note (`reprise-forcee`) : c'est une aide,
  pas un verrou. Le retour à la base porte le même avertissement, sans
  blocage. Dans les trames réelles, seuls existent : satellites, précision
  horizontale, précision verticale, qualité, état du nœud de localisation ;
  ni l'âge des corrections RTK ni l'état du lien radio de la base n'y
  figurent.
- **SEC-13 Garde-fou carte : un AVERTISSEMENT par défaut** (décision du
  propriétaire, 2026-10-12 : en essai réel, la carte plaçait le robot à
  0,35 m d'un bord alors qu'il en était loin, et le garde-fou refusait à
  tort). Avant et après chaque avance ou recul, l'outil dit ce que la carte
  indique (« d'après la carte : bord de zone devant à 0,35 m », en orange
  quand le bord est avant la fin du pas), puis **émet le pas en entier** :
  il ne raccourcit et ne refuse rien. La case « Arrêter aux bords de zone
  (d'après la carte) », **décochée par défaut** et mémorisée sur ce poste
  (`preferences.json` du dossier de données, lue aussi par la commande),
  rétablit l'ancien comportement : marge de 0,30 m plus l'incertitude
  annoncée, pas raccourci ou refusé, « Forcer ce pas » (qui n'apparaît
  qu'alors) ou `--forcer` pour passer outre. Sans pose récente (5 s), sans
  carte ou robot hors des zones : « distance au bord inconnue ». Seule la
  distance dans l'axe du pas est regardée ; la rotation n'est pas
  concernée. La prévisualisation annonce la même chose, étape par étape.
  **Aucun refus de carte ne désarme le pilotage direct.** Avant émission,
  seuls un refus de statut (SEC-01, SEC-01b) et un échec de liaison
  désarment ; les refus de bornes (SEC-02), de carte (SEC-13), de
  localisation (SEC-12), de pas hors liste, ou « un autre ordre est en
  cours » sont dits et la main est gardée.
  Vérification hors ligne du repère (2 512 poses réelles du 2026-10-12,
  carte de référence) : toutes les poses de précision fiable ou dégradée
  tombent dans les zones et passages de la carte, et aucune autre
  orientation des axes ne fait mieux — pose et carte sont dans le même
  repère. Un décalage de l'ordre du mètre ne se voit pas par ce contrôle,
  et aucune pose « en charge » n'existe dans les captures pour le comparer
  à la station. Sur la carte, une position reçue il y a plus de 5 s est
  dite telle (« le marqueur n'est plus à jour »).
- **SEC-14 Arrêt sur indice de choc** pendant un mouvement : arrêt immédiat
  si un code d'erreur ou d'avertissement de choc, pare-chocs, ultrasons,
  soulèvement, blocage ou patinage **apparaît** (un code déjà présent au
  départ n'empêche pas de dégager le robot) ; si le statut passe à erreur
  ou arrêt d'urgence (SEC-01) ; ou si la position n'avance plus alors que
  les consignes continuent (moins de 20 % de l'attendu sur 1,5 s, après
  1 s d'élan) : « obstacle ou patinage : arrêt ». Ce dernier contrôle
  n'opère que si la position arrive au moins chaque seconde ; sinon le
  bilan dit « surveillance de progression inactive ».
- **Allure.** Liste « Allure » à côté des pas : auto, lente (0,10 m/s),
  normale (0,25 m/s). En auto, l'allure lente n'est prise que si la
  position est fiable ET que l'arrivée est à moins de 1 m d'un bord de
  zone ; position dégradée ou perdue : la carte n'est pas crédible,
  l'allure reste normale. C'est annoncé au bilan. Un ordre dure 9 s au plus (SEC-02) : un
  pas trop long pour 0,10 m/s garde sa longueur et prend la plus faible
  vitesse qui tient (0,12 m/s pour 1 m, 0,23 m/s pour 2 m). Pour rester
  vraiment lent, choisir un pas de 0,5 m.
- **Retrouver le signal.** Après chaque pas, le retour donne la précision et
  sa tendance (« position ±0,84 m ↘ en amélioration »). La précision est
  relevée par position dans `releve-precision.jsonl` (dossier de données,
  hors dépôt ; un point au plus tous les 2 m ou 30 s) et la carte peut
  l'afficher (case « précision relevée » : vert, orange, rouge).
- **Rendre la main à l'application** (barre de commande) : ferme la liaison
  de l'outil et suspend sa reconnexion automatique jusqu'à « Reprendre la
  liaison » ; retire l'attestation et l'armement direct ; n'émet rien vers
  le robot ; refusé pendant un mouvement. Si le robot est encore en
  télécommande, la page propose d'attendre son retour en pause.
- **Diagnostic des ordres réels** : pendant un ordre réel (et 20 s après,
  depuis la page), les trames reçues sont enregistrées telles quelles dans
  `captures/ordre-<horodatage>/`, au format de `lymow capture`. Jamais
  bloquant ; rien n'est écrit pendant un arrêt (SEC-03).

Codes de sortie : 0 fait ; 1 échec ou séquence interrompue ; 2 refus ou saisie
invalide ; 3 arrêt non confirmé ; 130 interruption au clavier (arrêt émis).

### Ce que l'arrêt garanti ne couvre pas

L'arrêt part du poste. Sous Windows, fermer la fenêtre du terminal, fermer la
session ou éteindre la machine pendant un mouvement déclenche encore l'arrêt :
l'outil intercepte l'événement (commande comme interface) et publie les trois
arrêts dans les quelques secondes que le système accorde (environ 5 s). Ce
filet est court — une liaison lente peut le dépasser — : ne fermez pas la
fenêtre pendant un mouvement.

Restent non couverts : processus tué (Gestionnaire des tâches, `taskkill /F`),
plantage de l'interpréteur, mise en veille, coupure de courant du poste, perte
du réseau côté poste. Dans ces cas aucun arrêt n'est émis, et rien dans les
sources ne prouve que le robot s'arrête seul quand les consignes cessent :
l'exposition n'est alors bornée que par le robot lui-même (c'est ce que mesure
`essai-arret-auto`).

Tant que le programme vit, l'exposition maximale est : 9 s de mouvement, 1 s
d'attente d'une consigne non acquittée, puis 20 s d'essais d'arrêt, soit 30 s.
Si le robot conserve sa dernière consigne pendant tout ce temps, cela fait au
plus 7,5 m à 0,25 m/s (ou un pivot continu à 0,35 rad/s) avant « ARRÊT NON
CONFIRMÉ ». Premier essai : robot à vue, zone dégagée sur 8 m, main sur le
bouton STOP.

Secrets : seul le jeton de rafraîchissement est conservé, dans le Gestionnaire
d'identification Windows (service `lymow-pilotage`), avec la région,
l'identifiant d'appareil et la méthode de connexion. Rien dans un fichier.

En lecture, ces messages « requête pure » peuvent partir, et eux seuls
(avec les deux demandes de diagnostic RTK, voir « Localisation »),
contrôlés sur la structure exacte de leurs octets : le battement de présence
(`appConnect = 2` + identifiant de session), la demande de configuration
(`version` + champ 9 `{10: 1}`) et la demande de carte entière (`version`,
`userCtrl 19`, champ 23 `{1: 0, 4: 1}`). `--passif` et `diagnostic` n'émettent
rien. La demande de trajet parcouru (userCtrl 23) n'est pas retenue : elle
porte un index de paquet, et rien ne dit qu'un seul paquet suffise.

## Carte et ajustement de zone

**La retouche de contour n'a jamais été essayée sur ce robot.** Le socle la dit
« vérifiée en réel » sur le sien ; ici, seule la logique est testée, sur un faux
robot. Commencez par **+5 cm sur une zone sans enjeu**, robot à la station, et
regardez le verdict et l'application du fabricant avant d'aller plus loin.

Lecture : `lymow carte [--json]` et le panneau « Carte » de la page affichent
les zones de tonte (nom, contour, surface), les zones interdites, les passages
(dont le passage de station), la station, la position et le cap du robot en
direct et la qualité RTK, dans le repère local en mètres (est vers la droite,
nord en haut), avec échelle, zoom, déplacement et légende. La carte reçue est
gardée en mémoire par le serveur ; son heure de réception est affichée. C'est
une lecture : disponible sans `--reel` dès qu'un compte est connecté.

Ajustement : « cette zone est en fait plus grande, ou plus petite, d'une bande
de X cm le long de ce bord ».

- Page : choisir la zone, cliquer deux sommets de son contour (début et fin de
  la portion), choisir le côté court ou long de la boucle ou tout le contour,
  saisir X en cm (positif = élargir vers l'extérieur, négatif = rogner) et la
  longueur des raccords, puis « Prévisualiser l'ajustement » : ancien et
  nouveau contours superposés, surface avant et après, déplacement maximal,
  nombre de sommets déplacés, avertissements. « Émettre » ensuite, comme pour
  un ordre (prévisualisation à usage unique, 60 s, confirmation).
- Commande : `lymow ajuster-zone <nom|id> --de I --a J --cm X [--cote
  court|long|tout] [--raccord M] [--nouvelle-reference]`. Sans `--reel` : lit
  la carte, affiche le résumé, une **empreinte** de 12 caractères, et écrit un
  aperçu SVG dans `apercus-carte/`. Pour émettre, relancer la même commande
  avec `--reel --confirmer <empreinte>` : l'empreinte dépend de la carte lue,
  de la zone, des paramètres et du nouveau contour ; absente ou différente,
  c'est un refus. Les indices de sommet sont ceux de `lymow carte --json` (à
  partir de 0).
- Retour arrière : `lymow restaurer-zone <sauvegarde.pb> [--zone identifiant]`
  renvoie l'ancien contour d'UNE zone par le même chemin et sous les mêmes
  règles (simulation, empreinte, `--reel --confirmer`). La zone se désigne par
  son identifiant exact, jamais par son nom ; la sauvegarde doit venir du même
  appareil et son fichier `.json` doit correspondre au `.pb` ; 50 cm au plus
  par restauration (une sauvegarde par opération).

Ce que fait le calcul : il déplace les sommets existants de la portion le long
de la normale extérieure, sans en ajouter ni en retirer ; sur une portion
ouverte l'écart croît de 0 à X sur la longueur du raccord à chaque extrémité.
Aux angles, la bande est donc un peu plus mince que X.

| Paramètre | Bornes | Défaut |
|---|---|---|
| Largeur X | −50 à +50 cm, non nulle | obligatoire |
| Sommets `--de`, `--a` | indices du contour de la zone | obligatoires |
| Côté | `court`, `long`, `tout` | `court` |
| Raccord | 0 à 5 m (0 = toute la portion d'un bloc) | 0,5 m |

Règles, codées en dur :

- **SEC-08** Une carte ne s'écrit qu'en mode réel, après prévisualisation,
  robot en ligne avec un état frais et un statut au repos ou en charge (0, 1,
  5, 12 — refus en tonte, pause, erreur, télécommande, retour), et sans aucun
  autre ordre en cours. Le statut est revérifié une seconde fois juste avant
  la publication, après la lecture de la carte et la sauvegarde.
- **SEC-09** Une carte partielle (signalée incomplète par le robot, sans
  station ou sans origine) s'affiche avec la mention « carte incomplète » mais
  ne sert jamais de base à une écriture. Avant toute écriture, la carte est
  lue deux fois de suite : les deux lectures doivent être complètes,
  identiques (zones, zones interdites, passages, station) et égales à la carte
  de la prévisualisation. Elle est alors sauvegardée telle que reçue dans
  `sauvegardes-carte/` (octets bruts `.pb` et JSON lisible), puis relue sur le
  disque ; si la sauvegarde échoue : refus.
- **SEC-10** Bornes de la retouche : |X| ≤ 50 cm ; contour résultant simple,
  sans sommets confondus, de même sens et de même nombre de sommets ; surface
  qui varie dans le sens de X ; aucun sommet déplacé de plus de |X| ; toute
  zone interdite rattachée reste strictement à l'intérieur, à 5 cm au moins du
  contour ; chaque passage et la station qui touchaient la zone la touchent
  encore. Cumul : aucun sommet ne s'éloigne de plus de 1 m de la référence
  locale de sa zone (la plus ancienne sauvegarde de cet appareil) ; au-delà,
  refus, sauf `--nouvelle-reference`, qui prend la carte actuelle du robot
  comme nouvelle référence. Un seul manquement : refus, avec le motif exact.
- **SEC-11** Après l'écriture, la carte est relue deux fois (environ 1 s puis
  5 s) et comparée : contour relu contre contour envoyé, et nom, réglages,
  zones interdites, passages, station et autres zones inchangés. Verdict :
  « appliqué » ; « non confirmé (le robot n'a pas encore renvoyé le nouveau
  contour — ne relancez pas, relisez la carte) » ; « DIFFÉRENCE INATTENDUE » ;
  « non vérifié » si la carte n'a pas pu être relue ou si l'opération a été
  interrompue (STOP, Ctrl+C). Hors « appliqué », le chemin de la sauvegarde et
  la commande de restauration sont affichés en tête. Une publication non
  acquittée ne conclut rien : la carte est relue et le verdict donné.

Les identifiants de zone venus du robot ne sont utilisés dans un nom de fichier
qu'après contrôle (lettres, chiffres, tiret, soulignement) ; sinon l'opération
est refusée. Le décodage de la carte est strict : charge tronquée, longueur
annoncée non tenue, type inconnu ou point sans coordonnée rendent la carte
illisible plutôt que partiellement lue.

Seule écriture de carte de l'outil : la retouche du contour d'une zone
(userCtrl 9 + zone et polygone). L'envoi de carte entière, que le robot ignore
d'après le socle, n'existe pas dans ce projet.

À savoir : la carte est supposée tenir dans une seule réponse du robot ; les
contrôles de SEC-10 ne regardent pas le chevauchement avec une autre zone de
tonte ; la surface et les bornes que le robot recalcule ne sont pas comparées.

## Ce qui reste incertain

- Le pilotage manuel par MQTT lui-même, et l'existence d'un chien de garde côté
  robot si les consignes cessent.
- L'acquittement vérifié est celui du serveur MQTT, pas celui du robot.
- `plateau-haut` : l'effet d'un changement de hauteur de coupe robot à l'arrêt
  est inconnu.
- Le statut « télécommande » (6) est tenu pour « lames à l'arrêt » afin de
  pouvoir enchaîner deux ordres ; à vérifier robot à vue. Constat du
  2026-10-12 : toute trame de pilotage manuel, y compris la consigne nulle
  de l'arrêt, fait passer le robot en « télécommande » (6) pendant une
  trentaine de secondes (26 à 32 s observées), puis il revient en pause (3).
- Qualité de position : les sources se contredisent sur le sens des valeurs
  (1 = point seul, ou flottant). L'outil affiche la précision horizontale en
  mètres quand le robot la donne, et le libellé seulement comme indication
  « non confirmé ».
- `stop` met aussi la tâche en pause, même si l'arrêt des consignes n'est pas
  confirmé ; un échec de la pause est signalé à part. L'effet réel de la pause
  sur une tonte autonome reste à constater. L'arrêt automatique de fin de
  mouvement, lui, n'émet que les consignes nulles.
- Lames à l'arrêt en pause (3) et en erreur (7) : non prouvé, à constater
  robot à vue.
- Robot sur sa base : `avancer` le pousserait contre la station.
- Mode télécommande : les sources ne décrivent aucune entrée explicite (la
  trame de consigne suffit ; le 2026-10-12 le statut est resté « prêt »
  pendant le recul) ; ha-lymow connaît une sortie, `userCtrl 32`, que cet
  outil n'émet pas.
- Liaison coupée par le service (par exemple quand une autre application
  prend la main) : la page le dit et se reconnecte seule ; la cause exacte
  n'est pas connue de l'outil.
- La présence en ligne est tenue pour confirmée dès qu'un état non conservé
  arrive du robot pendant la session ; on ignore si le robot répond à une
  sollicitation ou seulement à son battement d'environ 60 s (attente jusqu'à
  75 s avant chaque mouvement dans ce cas).
- Le sens du déplacement est jugé en supposant le cap mesuré depuis l'est, sens
  trigonométrique ; si la convention du robot diffère, « mauvais sens » sera
  signalé à tort.
- Sens de rotation, échelle des distances, disposition et version des trames :
  réglables dans `calibration.toml`.

Licences des sources reprises : `LICENSES-TIERS.md`.
