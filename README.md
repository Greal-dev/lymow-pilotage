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
| `lymow tourner <degrés>` | Pivote ; positif = gauche. 180° au plus. |
| `lymow avancer <mètres>` / `reculer <mètres>` | Translation ; 2 m au plus. |
| `lymow plateau-haut` | Hauteur de coupe au maximum (100 mm). |
| `lymow stop` | Arrêt du pilotage manuel, émis 3 fois. |
| `lymow sequence "plateau-haut; tourner 90; avancer 1"` | Enchaîne ; rien ne part si une étape est refusable ; s'arrête au premier refus, échec ou constat non confirmé. |
| `lymow reprendre` / `base` / `annuler` | Reprise de tâche (4), retour base tâche conservée (33), annulation sur place (28). |

Options communes, avant ou après la commande : `--reel` (émettre vraiment),
`--json`, `--donnees DOSSIER` (journaux et `calibration.toml` ; par défaut la
variable `LYMOW_DONNEES`, sinon le dossier qui contient `pyproject.toml`).

Sans `--reel`, chaque ordre affiche ce qu'il enverrait : sujet MQTT, champs
décodés, hexadécimal, base64, durée, cadence.

## Règles de sécurité (codées en dur, aucune option ne les lève)

- **SEC-01 Lames** : avant chaque mouvement réel, l'outil sollicite le robot
  et exige un état reçu dans la session, après cette demande, vieux de 30 s au
  plus (les messages conservés par le serveur, dits `retain`, sont ignorés),
  une présence en ligne explicitement confirmée, et un statut « lames à
  l'arrêt » : 0 repos, 1 attente, 5 charge, 12 charge terminée,
  6 télécommande. Tout le reste refuse (tonte, pause, retour, erreur, inconnu,
  hors ligne). Le statut est revérifié à chaque consigne : s'il sort de la
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
- `stop` n'émet que la consigne nulle du pilotage manuel : il n'interrompt pas
  une tonte autonome (utiliser `annuler`, l'application ou le bouton STOP).
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
