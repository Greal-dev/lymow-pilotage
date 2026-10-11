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
| `lymow connexion` | Compte Google : affiche l'adresse à ouvrir, puis lit l'adresse de rappel `myapp://callback/?code=…&state=…` sans écho. `--mot-de-passe` : compte à adresse et mot de passe. |
| `lymow diagnostic` | Jeton présent, rafraîchissement, appareils, MQTT, réception d'un état, micrologiciel. Ne publie rien. |
| `lymow etat` | État du robot. `--passif` : n'émet aucune requête. `--attente S` : attente maximale (75 s). |
| `lymow tourner (<degrés> \| --duree S) [--vitesse V]` | Pivote ; positif = gauche. 180° au plus. |
| `lymow avancer (<mètres> \| --duree S) [--vitesse V]` | Avance ; 2 m au plus. |
| `lymow reculer (<mètres> \| --duree S) [--vitesse V]` | Recule ; 2 m au plus. |
| `lymow plateau <mm>` | Hauteur du plateau de coupe, entier de 20 à 100 mm. |
| `lymow plateau-haut` | Raccourci : hauteur maximale (100 mm, réglable de 80 à 100). |
| `lymow stop` | Consigne nulle émise 3 fois, sans précondition, puis mise en pause de la tâche (userCtrl 3 ; 21 si le dernier état connu indique un retour à la base). |
| `lymow sequence "plateau 90; tourner 45 --vitesse 0.2; reculer --duree 2 --vitesse 0.1"` | Enchaîne ; rien ne part si une étape est refusable ; s'arrête au premier refus, échec ou constat non confirmé. Étapes : `tourner`, `avancer`, `reculer`, `plateau`, `plateau-haut`, `stop`. `--reel` et `essai-arret-auto` y sont interdits. |
| `lymow reprendre` / `base` / `annuler` | Reprise de tâche (4), retour base tâche conservée (33), annulation sur place (28). |
| `lymow ui [--reel] [--port N]` | Interface web locale avec retour vidéo (voir « Interface web locale »). |
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

`lymow ui [--reel] [--port N]` (port 8377 par défaut) démarre un serveur sur
`127.0.0.1` uniquement — aucune option ne change cet hôte —, ouvre le
navigateur et s'arrête sur Ctrl+C. `lancer-lymow.cmd` (double-clic) lance
`lymow ui` en simulation.

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
  « non connecté — lancez `lymow connexion` ». Aucun identifiant n'est jamais
  saisi dans la page.

Protection du serveur local : jeton aléatoire de 256 bits par lancement, exigé
sur toutes les routes, échangé à la première requête contre un témoin
`HttpOnly`, `SameSite=Strict` (l'adresse est aussitôt redirigée sans le jeton) ;
en-tête `Host` limité à `127.0.0.1:port` et `localhost:port` ; `Origin` exigé
et vérifié sur toute requête `POST` ; aucune règle CORS ; politique de contenu
`default-src 'self'`, plus la seule signalisation vidéo
`wss://*.kinesisvideo.<région>.amazonaws.com` une fois un compte connu ; aucune
ressource externe (ni CDN, ni police, ni mesure d'audience).

### Vidéo — non vérifiée en réel

« Démarrer la vidéo » (jamais automatique) demande au serveur une session
Amazon Kinesis Video (canal, serveurs ICE, adresse de signalisation présignée :
secrets temporaires, jamais journalisés ni mis en cache), puis la page ouvre la
signalisation, envoie l'offre WebRTC en réception seule et affiche le flux. La
vidéo est une observation : elle est disponible aussi sans `--reel`.

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
  d'urgence (13), mise à jour, inconnu, hors ligne. Le statut est revérifié à chaque consigne : s'il sort de la
  liste ou si le robot passe hors ligne, arrêt immédiat. L'outil ne sait
  émettre aucun ordre de démarrage de tonte ; seul `reprendre` peut relancer
  les lames.
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
  déplacement nettement supérieur à l'ordre (plus de 1,5 fois). Si aucun état
  n'est arrivé depuis l'ordre, l'outil le dit au lieu d'affirmer.
- **SEC-05** Simulation par défaut, `--reel` à chaque commande, en toutes
  lettres (aucune abréviation d'option).
- **SEC-06** Journal `journaux/AAAA-MM-JJ.jsonl` de chaque ordre simulé ou émis
  et de chaque état reçu, filtré de tout secret.
- **SEC-07** Séquence : toutes les étapes sont validées avant la première
  émission (un plafond dépassé et rien ne part) ; elle s'interrompt au premier
  refus, échec ou constat qui ne confirme pas l'ordre, en disant pourquoi et
  quelles étapes n'ont pas été exécutées.

Codes de sortie : 0 fait ; 1 échec ou séquence interrompue ; 2 refus ou saisie
invalide ; 3 arrêt non confirmé ; 130 interruption au clavier (arrêt émis).

### Ce que l'arrêt garanti ne couvre pas

L'arrêt part du poste. Il ne peut donc rien si le programme disparaît en plein
mouvement : fermeture de la fenêtre du terminal, fin de tâche forcée, mise en
veille ou coupure de courant du poste, perte du réseau côté poste. Dans ces
cas aucun arrêt n'est émis, et rien dans les sources ne prouve que le robot
s'arrête seul quand les consignes cessent : l'exposition n'est alors bornée
que par le robot lui-même.

Tant que le programme vit, l'exposition maximale est : 9 s de mouvement, 1 s
d'attente d'une consigne non acquittée, puis 20 s d'essais d'arrêt, soit 30 s.
Si le robot conserve sa dernière consigne pendant tout ce temps, cela fait au
plus 7,5 m à 0,25 m/s (ou un pivot continu à 0,35 rad/s) avant « ARRÊT NON
CONFIRMÉ ». Premier essai : robot à vue, zone dégagée sur 8 m, main sur le
bouton STOP.

Secrets : seul le jeton de rafraîchissement est conservé, dans le Gestionnaire
d'identification Windows (service `lymow-pilotage`), avec la région,
l'identifiant d'appareil et la méthode de connexion. Rien dans un fichier.

En lecture (`etat`), deux messages « requête pure » peuvent partir, et eux
seuls : le battement de présence (`appConnect = 2` + identifiant de session) et
la demande de configuration (`version` + champ 9 `{10: 1}`). `--passif` et
`diagnostic` n'émettent rien.

## Ce qui reste incertain

- Le pilotage manuel par MQTT lui-même, et l'existence d'un chien de garde côté
  robot si les consignes cessent.
- L'acquittement vérifié est celui du serveur MQTT, pas celui du robot.
- `plateau-haut` : l'effet d'un changement de hauteur de coupe robot à l'arrêt
  est inconnu.
- Le statut « télécommande » (6) est tenu pour « lames à l'arrêt » afin de
  pouvoir enchaîner deux ordres ; à vérifier robot à vue.
- `stop` met aussi la tâche en pause, même si l'arrêt des consignes n'est pas
  confirmé ; un échec de la pause est signalé à part. L'effet réel de la pause
  sur une tonte autonome reste à constater. L'arrêt automatique de fin de
  mouvement, lui, n'émet que les consignes nulles.
- Lames à l'arrêt en pause (3) et en erreur (7) : non prouvé, à constater
  robot à vue.
- Robot sur sa base : `avancer` le pousserait contre la station.
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
