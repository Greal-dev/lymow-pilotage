# Licences des sources tierces

Ce projet recopie ou s'inspire de deux dépôts tiers. Aucun n'est importé à
l'exécution : le code utile est recopié dans `src/lymow_pilotage/vendor/` ou
réécrit, avec l'attribution ci-dessous.

## ha-lymow — code recopié (socle)

- Dépôt : https://github.com/8408323/ha-lymow
- Commit : `826dde08feca410feb51415f484e3ab0f9966964` (2026-10-06)
- Licence : MIT
- Fichiers concernés : `src/lymow_pilotage/vendor/ha_lymow/{auth,api,mqtt,protocol,const}.py`,
  copies partielles de `custom_components/lymow/` ; chaque fichier liste ses
  modifications dans son en-tête.

```
MIT License

Copyright (c) 2026 Jonathan Haraldsson

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Lymow-HA — disposition des trames de pilotage (réécrite)

- Dépôt : https://github.com/d3dfantasy99/Lymow-HA
- Commit : `f38a4da1ac1e64d9f433ec72408c61e37a07694c` (2026-07-07)
- Licence : MIT, annoncée par le badge du `README.md` ; le fichier `LICENSE`
  est absent du dépôt à ce commit (le nom du titulaire n'y figure donc pas).
- Fichier concerné : `src/lymow_pilotage/protocole.py`. Les fonctions
  `encode_remote_control`, `encode_remote_stop` et `encode_set_cut_height`
  reprennent la disposition des champs de
  `custom_components/lymow/protocol.py` (PbInput.remoteControl, champ 10 ;
  PbInput.map.runTimeConfig.cutHeight), réécrite sans dépendance protobuf.
  `encode_debug_setting` / `exec_cmd` ne sont pas repris.

## Lymow-One-MQTT — recoupement seulement

- Dépôt : https://github.com/Mortimer452/Lymow-One-MQTT, commit
  `31bd0e69f4608193e1de7f929a78426655a97dcb`, MIT. Aucun code repris.
