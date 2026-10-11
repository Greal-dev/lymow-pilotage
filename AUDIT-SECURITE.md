# Audit de sécurité des dépôts sources — pilotage Lymow One Plus

Date : 2026-10-11. Méthode : lecture seule, rien installé, rien exécuté. Clones dans
`C:\Users\aleau\Documents\Projets VS Code\lymow-pilotage\sources\`.

## Commits figés

| Dépôt | Commit | Date | Licence |
|---|---|---|---|
| d3dfantasy99/Lymow-HA | `f38a4da1ac1e64d9f433ec72408c61e37a07694c` | 2026-07-07 | MIT |
| 8408323/ha-lymow | `826dde08feca410feb51415f484e3ab0f9966964` | 2026-10-06 | MIT |
| Mortimer452/Lymow-One-MQTT | `31bd0e69f4608193e1de7f929a78426655a97dcb` | 2026-09-26 | MIT |

AppGuy77/lymow-remote-downloads : non cloné, non installé (binaire non auditable).

## Choix de base proposé

Base hybride, code recopié dans notre paquet avec attribution MIT :

- **Socle (authentification, MQTT, état, commandes de tâche, caméra)** : `ha-lymow`.
  Le plus récent (version de protocole 49 contre 40 pour Lymow-HA), modules `api.py`,
  `auth.py`, `mqtt.py`, `protocol.py`, `const.py` sans aucun import Home Assistant,
  signalisation caméra Kinesis incluse.
- **Pilotage manuel par le cloud** : le seul encodeur MQTT existant est celui de `Lymow-HA`
  (`custom_components/lymow/protocol.py:494`, `PbInput.remoteControl` champ 10 =
  `{linearSpeed, angularSpeed}`). La trame Bluetooth de `ha-lymow`
  (`protocol.py:2425`) porte le même champ 10 : les deux se recoupent.

## Constats

Gravité : P1 = bloquant pour notre outil, P2 = à corriger dans notre copie, P3 = mineur.

| # | Gravité | Dépôt | Fichier:ligne | Constat | Correction dans notre copie |
|---|---|---|---|---|---|
| 1 | P1 | Lymow-HA | `coordinator.py:1259-1279`, `:860-863` | Aucun arrêt garanti : un ordre, une attente, un stop sans `finally` ni vérification ; si MQTT tombe pendant l'attente, le stop n'est jamais émis. Aucun délai de sécurité côté robot n'est démontré. | Stop dans un `finally`, répété 3 fois en QoS 1 ; republication de la consigne à 5-10 Hz avec homme mort ; stop sur déconnexion ; refus d'émettre si MQTT non connecté. |
| 2 | P2 | ha-lymow | `.claude/settings.json:3-12` | Active un marketplace de plugins tiers (`8408323/dotclaude`) : une session Claude Code ouverte dans ce dossier pourrait charger du code. `CLAUDE.md` et `BRANCH_STATUS.md` contiennent des instructions pour agents. | Ne jamais ouvrir de session dans `sources\ha-lymow`. Ne recopier que les `.py` utiles. Instructions non suivies. |
| 3 | P2 | Lymow-HA | `protocol.py:437` | `encode_debug_setting(exec_cmd=…)` : envoi d'une commande texte au robot. | Ne pas recopier cette fonction. |
| 4 | P2 | Lymow-HA, Lymow-One-MQTT | `__init__.py:111-114`, `config_flow.py:204-247` | Mot de passe et jetons persistés en clair (stockage Home Assistant). | Gestionnaire d'identification Windows (keyring) ; ne persister que le jeton de rafraîchissement. |
| 5 | P2 | Lymow-HA | `manifest.json:8-12` | `paho-mqtt>=1.6.1` et `protobuf>=5.26.1` non figés ; code incompatible paho 2.x ; le fichier généré exige protobuf ≥ 6.31.1. | Sans objet si le socle est `ha-lymow` (`aiohttp` + `aiomqtt`, verrou `uv.lock`) ; figer toutes les versions. |
| 6 | P3 | les trois | `const.py` | Domaines `eu/ap/us-auth.lymow.com` hors `amazonaws.com` (interface de connexion Cognito hébergée par Lymow). | Vérifier la résolution DNS vers Cognito avant première connexion. |
| 7 | P3 | Lymow-HA | `config_flow.py:142` ; Lymow-One-MQTT `auth.py:120-125` | Paramètre `state` OAuth facultatif ou absent ; pas de PKCE dans Lymow-One-MQTT. | PKCE et `state` obligatoires (déjà le cas dans `ha-lymow` `auth.py:164-176`). |
| 8 | P3 | les trois | `api.py`, `auth.py` | Corps des réponses d'erreur et e-mail dans les journaux. | Journal sans corps de réponse ni identifiant. |
| 9 | P3 | Lymow-HA | `coordinator.py:1233-1250` | Bornes [-1, 1] sans contrôle NaN/infini ; unités non documentées. | Bornes dures, rejet NaN/infini. `ha-lymow` `const.py:626-634` donne ±0,5 m/s et ±0,6 rad/s (positif = gauche). |
| 10 | P3 | ha-lymow, Lymow-HA | `mqtt.py` | URL WebSocket présignée valable 24 h. | Ne jamais la journaliser. |

Points conformes dans les trois dépôts : aucun hôte hors AWS et domaines Lymow à l'exécution,
aucune télémétrie, aucun `eval`/`exec`/`pickle`, aucune vérification TLS désactivée, aucun
secret en dur (seulement des identifiants publics Cognito extraits de l'application).

## Protocole recoupé

- Sujets MQTT : `/device/{thing}/pbinput` (ordres), `/device/{thing}/pboutput` (état),
  `/device/{thing}/notify-app` (en ligne / hors ligne). Charge utile `{"message": base64(protobuf)}`, QoS 1.
- Connexion : WebSocket signé SigV4 sur `a3j5zqqo5iuph9-ats.iot.<région>.amazonaws.com:443`.
- Compte Google : pris en charge (interface Cognito, PKCE, redirection `myapp://callback/`,
  code à recopier une fois depuis le navigateur, puis jeton de rafraîchissement). Apple : non.
- Commandes de tâche (`userCtrl`) : 1 démarrer, 2 base (tâche annulée), 3 pause, 4 reprise,
  33 base (tâche conservée), 32 quitter le mode télécommande, 28 réinitialisation forcée.
- État : position, cap (`pose.theta`), RTK, batterie, Wi-Fi/4G, codes d'erreur, statut de travail
  (6 = télécommande). Battement d'environ 60 s hors sollicitation.

## Risques ouverts (à lever robot à vue)

1. **Pilotage manuel par le cloud non prouvé** dans les sources lisibles : `ha-lymow` le note
   « undecoded » et n'a observé que du Bluetooth ; `Lymow-HA` l'encode avec la version 40.
   Seul indice positif : Lymow Remote annonce des joysticks par le cloud.
2. **Levée du plateau** : `SIGNAL_ONE_CLICK_LIFT` est noté « BLE-only » (`Lymow-HA const.py:461`).
   La voie cloud possible passe par la hauteur de coupe ; effet à l'arrêt inconnu.
3. **État des lames** : pas de booléen ; seulement `cutSpeed` et le statut de travail. La règle
   « lames à l'arrêt confirmées » devra s'appuyer sur le statut de travail.
4. **Fréquence de l'état** (≈ 60 s) probablement trop lente pour asservir sur la mesure :
   prévoir des ordres courts en durée, puis relecture.
5. **Caméra à distance** : signalisation extractible, mais le flux exige un navigateur
   (échec démontré de `aiortc`) : à afficher dans la page web locale.
6. Mode télécommande : entrée implicite ou non, sortie par `userCtrl` 32 — à observer.
