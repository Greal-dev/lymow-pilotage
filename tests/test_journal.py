"""SEC-06 : journal par jour, et aucun secret ne le traverse."""

from __future__ import annotations

from datetime import datetime

from conftest import lignes_journal

from lymow_pilotage import journal
from lymow_pilotage.journal import Journal

JETON = "JETON-FACTICE-0123456789abcdefXYZ"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"
URL = "wss://hote.iot.eu-west-1.amazonaws.com/mqtt?X-Amz-Signature=deadbeef&X-Amz-Security-Token=zzz"


def test_sec06_un_fichier_par_jour(tmp_path):
    jour = datetime(2026, 10, 11, 9, 30, 0)
    Journal(tmp_path, horloge=lambda: jour).ecrire("ordre-simule", ordre="tourner 10")
    fichier = tmp_path / "journaux" / "2026-10-11.jsonl"
    assert fichier.is_file()
    (ligne,) = lignes_journal(tmp_path)
    assert ligne == {"horodatage": "2026-10-11T09:30:00.000", "evenement": "ordre-simule", "ordre": "tourner 10"}


def test_sec06_jeton_factice_injecte_partout_jamais_ecrit(tmp_path):
    journal.declarer_secret(JETON)
    Journal(tmp_path).ecrire(
        "ordre-emis",
        RefreshToken=JETON,
        detail=f"échec avec {JETON} dans le texte",
        imbrique={"liste": [JETON, {"profond": f"x{JETON}y"}], JETON: "valeur"},
        jwt_non_declare=f"réponse {JWT}",
        adresse=URL,
        rappel="myapp://callback/?code=abc123&state=def456",
        courriel_libre="compte jean.dupont@example.org refusé",
        ordre="avancer 1",
    )
    brut = next((tmp_path / "journaux").glob("*.jsonl")).read_text(encoding="utf-8")
    for interdit in (JETON, JWT, "deadbeef", "X-Amz-Signature", "abc123", "def456", "jean.dupont"):
        assert interdit not in brut
    (ligne,) = lignes_journal(tmp_path)
    assert ligne["RefreshToken"] == journal.MASQUE
    assert ligne["ordre"] == "avancer 1"


def test_sec06_cles_sensibles_masquees_meme_sans_declaration():
    propre = journal.assainir(
        {"access_token": "a", "mot_de_passe": "b", "Authorization": "c", "code": "d", "url": "e", "batterie": 80}
    )
    assert propre == {
        "access_token": journal.MASQUE, "mot_de_passe": journal.MASQUE, "Authorization": journal.MASQUE,
        "code": journal.MASQUE, "url": journal.MASQUE, "batterie": 80,
    }


def test_sec06_chaque_etat_recu_et_chaque_ordre_emis_sont_journalises(session, tmp_path):
    import asyncio

    from lymow_pilotage import mouvement

    asyncio.run(mouvement.executer_mouvement(session, mouvement.Ordre("avancer", 0.5)))
    evenements = [ligne["evenement"] for ligne in lignes_journal(tmp_path)]
    assert evenements.count("etat-recu") == 2
    assert evenements.count("ordre-emis") == 1
    assert evenements.count("arret-emis") == 1
    emis = next(ligne for ligne in lignes_journal(tmp_path) if ligne["evenement"] == "ordre-emis")
    assert emis["sujet"] == "/device/robot-test/pbinput"
    assert emis["trame"]["hexadecimal"] == "10 31 52 0a 0d 00 00 80 3e 15 00 00 00 00"


def test_sec06_journal_en_panne_n_empeche_pas_l_arret_mais_bloque_un_ordre(tmp_path):
    (tmp_path / "journaux").write_text("pas un dossier", encoding="utf-8")
    panne = Journal(tmp_path)
    panne.ecrire("arret-emis")  # ne lève pas
    try:
        panne.ecrire("ordre-emis", strict=True)
    except OSError:
        return
    raise AssertionError("un ordre réel doit être refusé si le journal est indisponible")


def test_sec03_journal_garde_large_aucune_panne_ne_remonte(tmp_path):
    def horloge_cassee():
        raise RuntimeError("panne quelconque, pas une erreur de disque")

    Journal(tmp_path, horloge=horloge_cassee).ecrire("arret-emis", acquittes=3)  # ne lève pas
