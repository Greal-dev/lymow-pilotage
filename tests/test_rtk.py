"""Diagnostic RTK : requêtes pures 57 / 58, décodage des réponses, écart brut / calculé, garde SEC-12 étendue.

Les trames de réponse sont CONSTRUITES selon le schéma et les libellés du socle (ha-lymow) : aucune
trame réelle de ce type n'existe encore dans nos captures (le robot ne les envoie qu'à qui les demande).
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import struct
import subprocess

import pytest
from conftest import fragment
from test_carte import contenu

from lymow_pilotage import carte as cartes
from lymow_pilotage import cli, mouvement, protocole, securite, ui
from lymow_pilotage.config import Calibration
from lymow_pilotage.etat import Etat
from lymow_pilotage.mouvement import Ordre
from lymow_pilotage.protocole import Categorie, est_requete_pure
from lymow_pilotage.securite import RefusSecurite
from lymow_pilotage.vendor.ha_lymow import protocol as pb
from lymow_pilotage.vendor.ha_lymow.protocol import decode_pboutput

ORIGINE = (46.5, -0.5)  # origine fictive de la carte d'essai (test_carte.contenu)


def f64(numero: int, valeur: float) -> bytes:
    return bytes([numero << 3 | 1]) + struct.pack("<d", valeur)


def reponse_rtk(age: float = 2.0, base: int = 0, precision: float = 0.012, erreur: str | None = None) -> bytes:
    """PbOutput : champ 35 (niveau 1), champ 36 (niveau 2), champ 8 (diagnostic avancé) — schéma du socle."""
    l1 = pb._field_f32(2, precision) + pb._field_i32(3, 24) + pb._field_i32(10, base) + pb._field_f32(11, 1.5)
    l2 = pb._field_f32(1, age)
    avance = {"precision": precision, "quality": 4, "diff_age": age} | ({"primary_error_desc": erreur} if erreur else {})
    iot = pb._field_i32(1, 3) + pb._field_bytes(2, json.dumps(avance).encode())
    return pb._field_bytes(35, l1) + pb._field_bytes(36, l2) + pb._field_bytes(8, iot)


def coordonnees(est: float, nord: float, double: bool = True) -> bytes:
    """PbOutput champ 26 : la position (est, nord) du repère de la carte, exprimée en latitude / longitude."""
    lat = ORIGINE[0] + math.degrees(nord / 6_378_137.0)
    lon = ORIGINE[1] + math.degrees(est / (6_378_137.0 * math.cos(math.radians(ORIGINE[0]))))
    corps = f64(1, lat) + f64(2, lon) if double else pb._field_f32(1, lat) + pb._field_f32(2, lon)
    return pb._field_bytes(26, corps)


# --- requêtes pures ------------------------------------------------------------------------


def test_demandes_de_diagnostic_rtk_requetes_pures_octet_pour_octet():
    niveau1, niveau2 = (protocole.trame_diagnostic_rtk(Calibration(), n) for n in (1, 2))
    assert niveau1.octets == bytes.fromhex("10 31 28 39") and niveau2.octets == bytes.fromhex("10 31 28 3a")
    assert niveau1.categorie is Categorie.REQUETE_PURE and est_requete_pure(niveau1.octets) and est_requete_pure(niveau2.octets)
    assert est_requete_pure(bytes.fromhex("10 28 28 39"))  # version 40, connue
    assert protocole.PERIODE_DIAGNOSTIC_RTK_S == 5.0
    with pytest.raises(ValueError):
        protocole.trame_diagnostic_rtk(Calibration(), 3)


@pytest.mark.parametrize(
    "octets",
    [
        "10 31 28 38", "10 31 28 3b",  # codes voisins : 56, 59
        "10 31 28 2f",  # 47 RESET_INIT : jamais testé par personne, interdit
        "10 31 28 1c",  # 28 : annulation de tâche — un ordre, pas une lecture
        "10 31 28 04", "10 31 28 03", "10 31 28 21",  # reprendre, pause, retour : des ordres
        "10 31 28 39 52 0a 0d 00 00 80 3e 15 00 00 00 00",  # demande suivie d'une consigne de mouvement
        "10 31 28 39 28 04",  # deux codes
        "10 0c 28 39",  # version inconnue
        "28 39", "10 31 28 b9 00",  # sans version ; même valeur, autre codage des octets
        "10 31 28 39 9a 01 00",  # champ en plus (entrée de localisation : jamais encodée)
    ],
)
def test_variantes_des_demandes_rtk_refusees(octets):
    assert not est_requete_pure(bytes.fromhex(octets))


def test_session_en_lecture_seule_emet_les_demandes_rtk_et_rien_d_autre(session, lien):
    session.autorisees = frozenset({Categorie.REQUETE_PURE})
    for niveau in (1, 2):
        asyncio.run(session.publier(protocole.trame_diagnostic_rtk(session.cal, niveau), 3.0))
    assert lien.trames == [bytes.fromhex("10 31 28 39"), bytes.fromhex("10 31 28 3a")] and lien.ordres == []
    with pytest.raises(RefusSecurite):  # un ordre maquillé en requête pure ne passe pas
        faux = protocole.Trame("faux", Categorie.REQUETE_PURE, bytes.fromhex("10 31 28 2f"), {})
        asyncio.run(session.publier(faux, 3.0))


# --- décodage (trames construites selon le schéma) -------------------------------------------


def test_reponses_de_diagnostic_decodees_age_des_corrections_base_et_motif():
    etat = Etat(en_ligne=True)
    etat.appliquer(decode_pboutput(reponse_rtk(age=2.0)), 10.0)
    assert etat.age_corrections_s == 2.0 and etat.recu_rtk == 10.0
    assert etat.detail_localisation() == "corrections de la base reçues il y a 2 s"
    perdu = decode_pboutput(reponse_rtk(age=45.0, base=1, erreur="ERTK_LORA_DATA_ERROR_RATE"))
    assert perdu["rtkL1"]["baseStationStatus"] == "offline" and perdu["rtkL1"]["gnssSatellites"] == 24
    assert perdu["rtkL2"] == {"differentialAgeSec": 45.0} and perdu["rtkDiagnostic"]["primaryError"] == "ERTK_LORA_DATA_ERROR_RATE"
    etat.appliquer(perdu, 20.0)
    assert etat.detail_localisation() == (
        "plus de corrections depuis 45 s ; base hors ligne ; motif annoncé : ERTK_LORA_DATA_ERROR_RATE"
    )
    assert etat.resume(20.0)["age_corrections_s"] == 45.0
    # Charge abîmée ou hostile : ignorée sans erreur.
    for abime in (pb._field_bytes(8, pb._field_bytes(2, b"pas du json")), pb._field_bytes(8, pb._field_bytes(2, b"[1, 2]")),
                  pb._field_bytes(35, b"\xff"), pb._field_bytes(26, pb._field_i32(1, 5))):
        try:
            decode = decode_pboutput(abime)
        except (IndexError, struct.error):
            continue  # le socle rejette la trame entière : l'écoute l'ignore
        assert "rtkDiagnostic" not in decode and "llaCoords" not in decode
    assert Etat().detail_localisation() == "" and Etat().age_corrections_s is None


def test_ecart_entre_position_satellite_brute_et_position_calculee():
    etat = Etat(en_ligne=True)
    etat.appliquer(fragment(statut=3, tache=2, est=10.0, nord=5.0, precision=0.01), 1.0)
    etat.appliquer(decode_pboutput(coordonnees(10.3, 5.4)), 1.0)
    assert etat.position_brute is None and etat.ecart_brut_m is None  # sans origine de carte : rien à comparer
    etat.origine = ORIGINE
    est, nord, fine = etat.position_brute
    assert (est, nord) == (pytest.approx(10.3, abs=0.01), pytest.approx(5.4, abs=0.01)) and fine is True
    ecart, fiable = etat.ecart_brut_m
    assert ecart == pytest.approx(0.5, abs=0.01) and fiable is True and etat.ecart_pour_la_garde == pytest.approx(0.5, abs=0.01)
    resume = etat.resume(1.0)
    assert resume["ecart_brut_m"] == 0.5 and resume["position_brute"] == [10.3, 5.4]
    assert "écart position satellite / position calculée : 0,50 m" in resume["localisation_detail"]
    assert "lat" not in json.dumps(resume) and "46.5" not in json.dumps(resume)  # jamais de coordonnées GPS hors mémoire
    # Coordonnées en simple précision : l'écart est affiché comme indicatif et ne nourrit PAS la garde.
    grossier = Etat(en_ligne=True, origine=ORIGINE)
    grossier.appliquer(fragment(statut=3, tache=2, est=10.0, nord=5.0, precision=0.01), 1.0)
    grossier.appliquer(decode_pboutput(coordonnees(10.3, 5.4, double=False)), 1.0)
    assert grossier.ecart_brut_m[1] is False and grossier.ecart_pour_la_garde is None
    assert "indicatif : coordonnées en simple précision" in grossier.detail_localisation()
    assert cartes.origine(cartes.decoder(contenu())) == ORIGINE and cartes.origine(cartes.decoder(contenu(origine=False))) is None


# --- garde SEC-12 étendue --------------------------------------------------------------------


def test_sec12_fiable_exige_aussi_un_ecart_brut_calcule_contenu_quand_il_est_connu():
    assert (securite.PRECISION_FIABLE_M, securite.ECART_BRUT_MAX_M, securite.ECART_BRUT_AFFICHE_M) == (0.10, 0.30, 0.20)
    assert securite.avertissement_position(0.01) is None and securite.avertissement_position(0.01, 0.30) is None
    assert securite.avertissement_position(0.01, 0.45) == (
        "Le signal est revenu mais la position calculée par le robot s'écarte de 0,45 m de sa position satellite : "
        "recalage conseillé."
    )
    assert "Position imprécise (±1,32 m)" in securite.avertissement_position(1.32, 0.05)  # la précision d'abord
    with pytest.raises(RefusSecurite, match=r"recalage conseillé\. \(SEC-12\)$"):
        securite.verifier_precision_pour_reprise(0.01, 0.45)
    securite.verifier_precision_pour_reprise(0.01, None)  # écart inconnu : la précision seule juge


def test_sec12_reprise_refusee_quand_la_pose_s_ecarte_de_la_position_satellite(session, lien):
    session.etat.origine = ORIGINE
    lien.robot = fragment(statut=3, tache=2, est=10.0, nord=5.0, precision=0.01) | decode_pboutput(coordonnees(10.3, 5.4))
    with pytest.raises(RefusSecurite, match="s'écarte de 0,50 m de sa position satellite"):
        asyncio.run(mouvement.executer(session, Ordre("reprendre")))
    assert lien.ordres == []
    lien.robot = fragment(statut=3, tache=2, est=10.0, nord=5.0, precision=0.01) | decode_pboutput(coordonnees(10.1, 5.1))
    asyncio.run(session.solliciter())
    assert asyncio.run(mouvement.executer(session, Ordre("reprendre")))["codes_emis"] == [4]  # écart de 0,14 m : admis


# --- interface et capture -----------------------------------------------------------------------


def test_interface_sonde_le_diagnostic_rtk_liaison_active_seulement(tmp_path, monkeypatch, session_reelle, lien):
    from test_ui import attendre, executer, poster, serveur_ui

    monkeypatch.setattr(protocole, "PERIODE_DIAGNOSTIC_RTK_S", 0.05)
    lien.carte = contenu()
    demandes = lambda: [t for t in lien.trames if t in (bytes.fromhex("10 31 28 39"), bytes.fromhex("10 31 28 3a"))]  # noqa: E731

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle) as (serveur, http):
            await asyncio.sleep(0.3)
            assert demandes() == [] and serveur.instantane()["diagnostic_rtk"] is False  # jamais d'office
            statut, corps, _recus = await poster(http, serveur, "/api/diagnostic-rtk", {"actif": True})
            assert statut == 200 and corps == {"actif": True}
            await attendre(lambda: len(demandes()) >= 4)  # simulation comprise : ce sont des lectures
            assert lien.ordres == [] and all(est_requete_pure(t) for t in lien.trames)
            await poster(http, serveur, "/api/carte/actualiser")
            await attendre(lambda: serveur.session.etat.origine == ORIGINE)  # l'origine de la carte sert à l'écart
            await poster(http, serveur, "/api/liaison/rendre", {"choix": "pause"})  # main rendue : plus aucune demande
            avant = len(demandes())
            await asyncio.sleep(0.3)
            assert len(demandes()) == avant
            statut, _corps, _recus = await poster(http, serveur, "/api/diagnostic-rtk", {"actif": True})
            assert statut == 409  # main rendue : refusé
            await poster(http, serveur, "/api/liaison/reprendre")
            await asyncio.sleep(0.3)
            assert len(demandes()) == avant and serveur.diagnostic_rtk is False  # rendre la main l'a décoché
            await poster(http, serveur, "/api/diagnostic-rtk", {"actif": True})
            await attendre(lambda: len(demandes()) > avant)
            serveur.tache = asyncio.ensure_future(asyncio.sleep(30))  # pendant un ordre : rien ne s'ajoute
            await asyncio.sleep(0.1)
            pendant = len(demandes())
            await asyncio.sleep(0.3)
            assert len(demandes()) == pendant
            serveur.tache.cancel()
            await asyncio.gather(serveur.tache, return_exceptions=True)

    executer(scenario())


def test_capture_rtk_demande_le_diagnostic_et_rien_que_des_lectures(capsys, tmp_path, monkeypatch, session, lien):
    from contextlib import asynccontextmanager

    from lymow_pilotage import liaison

    @asynccontextmanager
    async def ouvrir(_journal, _cal, autorisees, *_reste, brut=None):
        session.autorisees = autorisees
        yield session

    monkeypatch.setattr(liaison, "ouvrir_session", ouvrir)
    lien.robot = None
    cli.main(["capture", "--rtk", "--secondes", "12", "--json", "--donnees", str(tmp_path)])
    capsys.readouterr()
    assert lien.trames.count(bytes.fromhex("10 31 28 39")) == 3 and lien.trames.count(bytes.fromhex("10 31 28 3a")) == 3  # toutes les 5 s
    assert lien.ordres == [] and all(est_requete_pure(t) for t in lien.trames)


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js absent : logique de la page non testée")
def test_page_recalage_assiste_aller_retour_et_bilan():
    def js(expression, *arguments):
        script = (
            "const M = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
            f"process.stdout.write(JSON.stringify({expression} ?? null));"
        )
        sortie = subprocess.run(
            [NODE, "-e", script, str(ui.STATIQUE / "ordres.js"), json.dumps(arguments)],
            capture_output=True, text=True, timeout=20, check=True, encoding="utf-8",
        )
        return json.loads(sortie.stdout)

    sequence = js("M.sequenceDeRecalage(2)")
    assert [(e["nom"], e["valeur"]) for e in sequence] == [("avancer", 2), ("avancer", 2), ("reculer", 2), ("reculer", 2)]
    for etape in sequence:
        mouvement.valider(Ordre(etape["nom"], float(etape["valeur"])), Calibration())  # SEC-02
    mieux = js("M.bilanDeRecalage(a[0], a[1])", {"precision_m": 0.01, "ecart_brut_m": 0.5}, {"precision_m": 0.01, "ecart_brut_m": 0.1})
    assert mieux == "Recalage : précision 0.01 m → 0.01 m, écart 0.5 m → 0.1 m : amélioré."
    pire = js("M.bilanDeRecalage(a[0], a[1])", {"precision_m": 0.01, "ecart_brut_m": 0.5}, {"precision_m": 0.01, "ecart_brut_m": 0.6})
    assert "pas d'amélioration" in pire and "redémarrage du robot" in pire
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    assert 'id="recalage"' in page and "Effet non garanti : jamais essayé sur ce robot" in page and 'id="localisation-detail"' in page
    app = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    recalage = app[app.index('$("recalage").addEventListener') :][:900]
    assert "previsualiser(sequence.slice(), false)" in recalage and "/api/emettre" not in recalage  # jamais émis d'ici
