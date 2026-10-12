"""Carte de référence : la dernière carte complète tient l'affichage, quoi que le robot renvoie ensuite.

Constat du 2026-10-12 : pendant une tâche, le robot diffuse des réponses de TRAJET (4 728 octets,
394 points) par le même chemin que la carte. Elles chassaient la carte de la mémoire : la page
n'affichait plus rien.
"""

from __future__ import annotations

import json

import jardin_etendu
from conftest import index_demande_carte
from test_carte import contenu
from test_ui import executer, lire, poster, serveur_ui

from lymow_pilotage import carte as cartes
from lymow_pilotage import reference
from lymow_pilotage.vendor.ha_lymow import protocol as pb

APPAREIL = "robot-test"


def trajet(points: int = 394) -> bytes:
    """Réponse de trajet, de la forme réelle : f23 {f2 {f1, f3 = points}, f3 = 1}."""
    corps = b"".join(pb._field_bytes(1, pb._field_f32(1, float(i)) + pb._field_f32(2, 1.0)) for i in range(points))
    return pb._field_bytes(23, pb._field_bytes(2, pb._field_i32(1, 1) + pb._field_bytes(3, corps)) + pb._field_i32(3, 1))


def test_reponse_de_trajet_de_la_forme_reelle_n_est_pas_une_carte():
    assert pb.extract_raw_map_content(trajet()) is None
    reponse_carte = pb._field_bytes(23, pb._field_bytes(2, pb._field_i32(1, 2) + pb._field_bytes(3, contenu())) + pb._field_i32(4, 1))
    assert pb.extract_raw_map_content(reponse_carte) == contenu()


def test_une_reponse_non_demandee_ne_chasse_plus_la_carte_en_memoire(session, lien):
    import asyncio

    lien.carte = contenu()
    carte = asyncio.run(cartes.demander(session, 20.0))
    assert cartes.en_memoire(session).empreinte() == carte.empreinte()
    # Le robot diffuse autre chose par le même chemin (trajet, écho partiel) : la carte lue reste.
    session.recevoir_etat({"mapRaw": b"\x0a\x04\x0d\x00\x00\x80\x3f"})
    session.recevoir_etat({"mapRaw": contenu()[:40]})
    assert cartes.en_memoire(session).empreinte() == carte.empreinte()


def test_reference_enregistree_relue_et_revalidee(tmp_path):
    complete = cartes.decoder(contenu())
    meta = reference.enregistrer(tmp_path, APPAREIL, complete)
    assert meta["octets"] == len(contenu()) and reference.libelle(meta).startswith("carte complète du ")
    relue, meta_relue = reference.charger(tmp_path, APPAREIL)
    assert relue.octets == contenu() and relue.complete and meta_relue == meta
    assert sorted(f.name for f in (tmp_path / reference.DOSSIER).iterdir()) == ["robot-test.json", "robot-test.pb"]
    assert reference.libelle({"horodatage": "2026-10-12T10:50:03"}) == "carte complète du 12/10 à 10:50"
    # Une carte partielle n'est jamais enregistrée et ne remplace donc jamais la référence.
    partielle = cartes.decoder(contenu(station=None))
    assert not partielle.complete and reference.enregistrer(tmp_path, APPAREIL, partielle) is None
    assert reference.charger(tmp_path, APPAREIL)[0].octets == contenu()
    # Fichier abîmé, autre appareil, identifiant hostile : pas de référence, pas d'erreur.
    assert reference.charger(tmp_path, "autre-robot") is None
    assert reference.charger(tmp_path, "../ailleurs") is None and reference.enregistrer(tmp_path, "../x", complete) is None
    (tmp_path / reference.DOSSIER / "robot-test.pb").write_bytes(contenu()[:-3])
    assert reference.charger(tmp_path, APPAREIL) is None
    (tmp_path / reference.DOSSIER / "robot-test.pb").write_bytes(contenu())
    (tmp_path / reference.DOSSIER / "robot-test.json").write_text("pas du json", encoding="utf-8")
    assert reference.charger(tmp_path, APPAREIL) is None


def test_page_affiche_la_reference_des_le_demarrage_et_malgre_une_reponse_partielle(tmp_path, monkeypatch, session_reelle, lien):
    reference.enregistrer(tmp_path, APPAREIL, cartes.decoder(contenu()))

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            # Dès l'ouverture, sans clic ni demande au robot.
            statut, texte, _recus = await lire(http, serveur, "/api/carte")
            image = json.loads(texte)
            assert statut == 200 and image["carte"]["complete"] is True and image["reference"].startswith("carte complète du ")
            assert image["age_s"] is None and image["note"] is None and len(image["carte"]["zones"]) == 2
            assert all(index_demande_carte(trame) is None for trame in lien.trames)  # aucune demande de carte

            # Le robot, en tâche, ne renvoie qu'une carte partielle (ou autre chose) : la référence reste.
            lien.carte = contenu(station=None)
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 200 and corps["carte"]["complete"] is True and corps["reference"] is not None
            assert "carte partielle" in corps["note"] and "carte de référence affichée" in corps["note"]
            lien.carte = contenu()[:60]  # réponse tronquée
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 200 and corps["carte"]["complete"] is True and "carte partielle" in corps["note"]
            session_reelle.recevoir_etat({"mapRaw": b"\x0a\x04\x0d\x00\x00\x80\x3f"})  # réponse non demandée
            statut, texte, _recus = await lire(http, serveur, "/api/carte")
            assert json.loads(texte)["carte"]["complete"] is True
            # L'aperçu d'une retouche se calcule sur la carte affichée ; l'écriture, elle, relirait le robot (SEC-09).
            ajustement = {"zone": "zoneA1", "de": 0, "a": 2, "cm": 20, "cote": "court", "raccord": 0.5}
            statut, corps, _recus = await poster(http, serveur, "/api/carte/previsualiser", ajustement)
            assert statut == 200 and corps["identifiant"] is None

            # Une nouvelle carte complète devient la référence, avec sa date.
            lien.carte = jardin_etendu.contenu()
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 200 and len(corps["carte"]["zones"]) == 55 and corps["reference"] is None and corps["note"] is None

    executer(scenario())
    assert len(reference.charger(tmp_path, APPAREIL)[0].zones) == 55


def test_sans_reference_une_carte_refusee_reste_une_erreur_et_la_demonstration_n_ecrit_rien(tmp_path, monkeypatch, session_reelle, lien):
    lien.carte = contenu()[:60]

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            statut, corps, _recus = await poster(http, serveur, "/api/carte/actualiser")
            assert statut == 422 and corps["erreur"].startswith("carte reçue mais refusée")
            serveur.demonstration = True
            assert serveur._appareil_de_la_carte() is None

    executer(scenario())
    assert not (tmp_path / reference.DOSSIER).exists()
    ignore = (reference.Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "carte-reference/" in ignore
