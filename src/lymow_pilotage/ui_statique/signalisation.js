/* Signalisation WebRTC Amazon Kinesis Video — logique pure, sans réseau ni page.
 *
 * Forme des messages reprise de ha-lymow (commit 826dde0, MIT), `frontend/src/views/Camera.tsx`
 * et `scripts/camera_feed_test.py` :
 *   spectateur vers robot : {"action": "SDP_OFFER" | "ICE_CANDIDATE", "messagePayload": base64(JSON)}
 *   robot vers spectateur : {"messageType": "SDP_ANSWER" | "ICE_CANDIDATE", "messagePayload": base64(JSON)}
 * Le robot ignore une offre sans la ligne « a=ice-options:trickle renomination ».
 * Fichier testé hors navigateur (voir tests/test_signalisation.py).
 */
(function (racine) {
  "use strict";

  var OPTIONS_ICE = "a=ice-options:trickle renomination";

  function encoderBase64(texte) {
    var octets = new TextEncoder().encode(texte);
    var binaire = "";
    for (var i = 0; i < octets.length; i += 1) {
      binaire += String.fromCharCode(octets[i]);
    }
    return btoa(binaire);
  }

  function decoderBase64(base64) {
    var binaire = atob(base64);
    var octets = new Uint8Array(binaire.length);
    for (var i = 0; i < binaire.length; i += 1) {
      octets[i] = binaire.charCodeAt(i);
    }
    return new TextDecoder("utf-8", { fatal: true }).decode(octets);
  }

  /* Remplace toute ligne ice-options, ou en ajoute une après chaque ice-pwd si aucune n'existe. */
  function ajusterSdp(sdp) {
    var lignes = String(sdp).split(/\r?\n/);
    var presente = lignes.some(function (ligne) { return ligne.indexOf("a=ice-options:") === 0; });
    var sortie = [];
    lignes.forEach(function (ligne) {
      if (ligne.indexOf("a=ice-options:") === 0) {
        sortie.push(OPTIONS_ICE);
        return;
      }
      sortie.push(ligne);
      if (!presente && ligne.indexOf("a=ice-pwd:") === 0) {
        sortie.push(OPTIONS_ICE);
      }
    });
    return sortie.join("\r\n");
  }

  function message(action, charge) {
    return JSON.stringify({ action: action, messagePayload: encoderBase64(JSON.stringify(charge)) });
  }

  function messageOffre(sdp) {
    return message("SDP_OFFER", { type: "offer", sdp: ajusterSdp(sdp) });
  }

  function messageCandidat(candidat) {
    return message("ICE_CANDIDATE", candidat);
  }

  /* Rend {type, charge} pour une réponse SDP ou un candidat ICE bien formés, sinon null. */
  function decoderMessage(texte) {
    var enveloppe;
    var charge;
    try {
      enveloppe = JSON.parse(texte);
      charge = JSON.parse(decoderBase64(enveloppe.messagePayload));
    } catch (erreur) {
      return null; // battements non JSON du canal de signalisation
    }
    var type = enveloppe.messageType || enveloppe.action;
    if (!charge || typeof charge !== "object") {
      return null;
    }
    if (type === "SDP_ANSWER" && typeof charge.sdp === "string") {
      return { type: type, charge: { type: "answer", sdp: charge.sdp } };
    }
    if (type === "ICE_CANDIDATE" && typeof charge.candidate === "string") {
      return { type: type, charge: charge };
    }
    return null;
  }

  var api = {
    OPTIONS_ICE: OPTIONS_ICE,
    encoderBase64: encoderBase64,
    decoderBase64: decoderBase64,
    ajusterSdp: ajusterSdp,
    messageOffre: messageOffre,
    messageCandidat: messageCandidat,
    decoderMessage: decoderMessage
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    racine.Signalisation = api;
  }
})(typeof self !== "undefined" ? self : this);
