"""Connexion en un clic : argument venu du navigateur, route locale, registre (toujours doublé)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import lignes_journal
from test_ui import AVANCER_1, HttpOAuth, _lancer_avancer, executer, lire, poster, serveur_ui
from test_ui import rappel as adresse_de
from test_ui import service_compte  # noqa: F401 — doubles du service de connexion, partagés

from lymow_pilotage import cli, rappel, ui
from lymow_pilotage.mouvement import ErreurSaisie

VALIDE = "myapp://callback/?code=abc-123_DEF.x~y&state=Zz09%3D"
NOTRE = Path(r"C:\outils\lymow\.venv\Scripts\lymow.exe")
COMMANDE = r"Software\Classes\myapp\shell\open\command"


# --- argument de `lymow rappel` ----------------------------------------------


def test_adresse_valide_acceptee():
    assert rappel.valider([VALIDE]) == VALIDE


@pytest.mark.parametrize(
    "arguments",
    [
        [], [VALIDE, "--reel"], ["--reel", VALIDE], [VALIDE, VALIDE], ["--help"], [""],
        ["myapp://callback/?code=a&state=b --reel"],  # espace : tentative d'ajout d'option
        ['myapp://callback/?code=a" --reel "'],  # guillemets : sortie de l'argument
        ["myapp://callback/?code=a&state=b\n--reel"], ["myapp://callback/?code=a;calc.exe"],
        ["myapp://callback/?code=a|b"], ["myapp://callback/?code=a&state=<b>"], ["myapp://callback/?code=é"],
        ["myapp://callback/code=a"], ["myapp://callback?code=a"], ["myapp://autre/?code=a"],
        ["https://callback/?code=a&state=b"], ["MYAPP://callback/?code=a"], [" myapp://callback/?code=a"],
        ["myapp://callback/?"], ["myapp://callback/?code=" + "a" * 2049], ["myapp://callback/?code=a#frag"],
        ["myapp://callback/?code=a\\..\\b"], ["myapp://callback/?code=a/../b"],
    ],
)
def test_argument_du_navigateur_refuse_sans_etre_cite(arguments):
    with pytest.raises(ErreurSaisie) as erreur:
        rappel.valider(arguments)
    for argument in arguments:
        assert not argument or argument not in str(erreur.value)


@pytest.mark.parametrize(
    "arguments",
    [["rappel"], ["rappel", VALIDE, "--reel"], ["rappel", "--reel", VALIDE], ["rappel", VALIDE, "--donnees", "x"],
     ["rappel", 'myapp://callback/?code=SECRET-CODE-999" --reel "'], ["rappel", "--json"]],
)
def test_commande_rappel_n_accepte_aucune_option_et_ne_transmet_rien(monkeypatch, capsys, arguments):
    async def interdit(*_args):
        raise AssertionError("rien ne doit être transmis")

    monkeypatch.setattr(rappel, "transmettre", interdit)
    assert cli.main(arguments) == 2
    sortie = capsys.readouterr()
    assert "refusée" in sortie.err and "SECRET-CODE" not in sortie.out + sortie.err


def test_rappel_n_est_pas_une_sous_commande_de_l_analyseur(tmp_path):
    with pytest.raises(SystemExit) as sortie:  # « lymow --reel rappel … » : l'analyseur ne connaît pas `rappel`
        cli.main(["--reel", "rappel", VALIDE])
    assert sortie.value.code == 2


def test_commande_rappel_sans_interface_message_clair_et_fenetre_retenue(monkeypatch, capsys, tmp_path):
    import sys

    invites = []
    monkeypatch.setattr(rappel, "PORT_DEFAUT", 1)  # aucun serveur n'écoute là
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda invite="": invites.append(invite) or "")
    assert cli.main(["rappel", VALIDE]) == 1
    sortie = capsys.readouterr()
    assert "Aucune interface Lymow n'attend cette connexion" in sortie.err and len(invites) == 1
    assert "abc-123" not in sortie.out + sortie.err and not (tmp_path / "journaux").exists()


# --- route locale ------------------------------------------------------------


async def remettre(http, serveur, adresse, **entetes_en_plus):
    async with http.post(serveur.adresse.rstrip("/") + rappel.ROUTE, json={"adresse": adresse}, headers=entetes_en_plus) as r:
        return r.status, await r.json()


def test_rappel_termine_la_connexion_comme_un_collage(tmp_path, monkeypatch, service_compte, session_reelle, capsys):
    coffre_factice = {}

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, session=session_reelle, coffre_factice=coffre_factice) as (serveur, http):
            serveur.ouvrir_http = service_compte
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            adresse = adresse_de(serveur)["adresse"]
            monkeypatch.setattr(rappel, "PORT_DEFAUT", serveur.port)
            assert await rappel.transmettre(adresse, rappel.ports_connus(None)) == rappel.REUSSIE  # ce que fait `lymow rappel`
            image = serveur.instantane()
            assert image["compte"]["connecte"] is True and serveur.tentative is None
            maintenant = serveur.horloge()
            serveur.horloge = lambda: maintenant + 5
            statut, corps = await remettre(http, serveur, adresse)
            assert statut == 409 and corps == {"interface": "lymow", "ok": False}  # adresse rejouée
            return adresse, json.dumps(image, ensure_ascii=False)

    adresse, ecran = executer(scenario())
    assert coffre_factice["jeton-rafraichissement"] == "raf-durable-ccc333" and len(service_compte.envois) == 1
    sortie = capsys.readouterr()
    disque = json.dumps(lignes_journal(tmp_path), ensure_ascii=False)
    for secret in (adresse, "CODE-OAUTH-fff666", "raf-durable-ccc333"):
        assert secret not in ecran and secret not in disque and secret not in sortie.out + sortie.err


def test_rappel_refuse_state_faux_sans_consommer_la_tentative(tmp_path, monkeypatch, service_compte):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, coffre_factice={}) as (serveur, http):
            serveur.ouvrir_http = service_compte
            temps = [serveur.horloge()]
            serveur.horloge = lambda: temps[0]

            async def essai(adresse, **entetes_en_plus):
                temps[0] += 2  # hors limitation de fréquence
                return await remettre(http, serveur, adresse, **entetes_en_plus)

            statut, corps = await essai(VALIDE)
            assert statut == 409 and corps == {"interface": "lymow", "ok": False}  # aucune tentative active
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            bonne = adresse_de(serveur)["adresse"]
            statut, _corps = await essai("myapp://callback/?code=CODE-OAUTH-fff666&state=un-autre-etat")
            assert statut == 403 and serveur.tentative is not None  # tentative intacte
            statut, _corps = await essai("myapp://callback/?code=CODE-OAUTH-fff666")
            assert statut == 403 and serveur.tentative is not None
            for mauvaise in ("https://evil.example/?code=a&state=b", "myapp://callback/?code=a b", 12, None):
                statut, _corps = await essai(mauvaise)
                assert statut == 400
            base = f"http://127.0.0.1:{serveur.port}"
            for en_plus in ({"Origin": base}, {"Referer": base + "/"}, {"Origin": "http://evil.example"}):
                statut, _corps = await essai(bonne, **en_plus)
                assert statut == 403  # un site web ne peut pas remettre d'adresse
            statut, _corps = await essai(bonne, Host="evil.example")
            assert statut == 403
            async with http.get(serveur.adresse.rstrip("/") + rappel.ROUTE) as reponse:
                assert reponse.status == 405
            assert serveur.tentative is not None and service_compte.envois == []

            # Fréquence : limitée seulement après un `state` refusé ; le vrai rappel passe sans attendre.
            temps[0] += 2
            fausse = "myapp://callback/?code=CODE-OAUTH-fff666&state=un-autre-etat"
            assert [(await remettre(http, serveur, fausse))[0] for _ in range(3)] == [403, 429, 429]
            statut, corps = await remettre(http, serveur, bonne)  # aussitôt après les fausses
            assert statut == 200 and corps == {"interface": "lymow", "ok": True}
            statut, _corps = await remettre(http, serveur, bonne)
            assert statut == 409  # tentative consommée

    executer(scenario())
    assert ui.DELAI_RAPPEL_S >= 1


def test_rappel_refuse_pendant_un_mouvement(tmp_path, monkeypatch, session_reelle, lien, service_compte):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=True, session=session_reelle) as (serveur, http):
            serveur.ouvrir_http = service_compte
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            bonne = adresse_de(serveur)["adresse"]
            await _lancer_avancer(http, serveur, lien)
            statut, corps = await remettre(http, serveur, bonne)
            assert statut == 409 and corps["ok"] is False and serveur.tentative is not None

    executer(scenario())
    assert service_compte.envois == []


# --- registre (double) -------------------------------------------------------


def test_installation_ecrit_la_commande_attendue_et_rien_d_autre(registre):
    assert rappel.etat() == {"pris_en_charge": True, "actif": False, "perime": False, "etranger": None}
    message = rappel.installer(executable=NOTRE)
    assert registre.cles[r"Software\Classes\myapp"] == {"": "URL:Lymow, retour de connexion", "URL Protocol": ""}
    assert registre.cles[COMMANDE] == {"": f'"{NOTRE}" rappel "%1"'}
    assert set(registre.cles) == {
        "Software", r"Software\Classes", r"Software\Classes\myapp", r"Software\Classes\myapp\shell",
        r"Software\Classes\myapp\shell\open", COMMANDE,
    }
    assert rappel.etat(executable=NOTRE)["actif"] is True and "activée" in message


def test_desinstallation_ne_retire_que_notre_cle(registre):
    registre.cles[r"Software\Classes\autre"] = {"": "à ne pas toucher"}
    assert "déjà inactive" in rappel.desinstaller()
    rappel.installer(executable=NOTRE)
    assert "désactivée" in rappel.desinstaller()
    assert not [cle for cle in registre.cles if "myapp" in cle]
    assert registre.cles[r"Software\Classes\autre"] == {"": "à ne pas toucher"}
    assert rappel.etat()["actif"] is False


@pytest.mark.parametrize("etrangere", [r'"C:\Program Files\AutreApp\app.exe" "%1"', r'"C:\x\lymow.exe" --reel "%1"', None])
def test_cle_myapp_etrangere_ni_ecrasee_ni_retiree(registre, etrangere):
    registre.cles[r"Software\Classes\myapp"] = {"": "URL:AutreApp", "URL Protocol": ""}
    if etrangere:
        registre.cles[COMMANDE] = {"": etrangere}
    avant, ecritures = json.dumps(registre.cles, sort_keys=True), registre.ecritures
    constat = rappel.etat()
    assert constat["actif"] is False and constat["etranger"] == (etrangere or "(clé myapp sans commande)")
    with pytest.raises(rappel.EchecRappel, match="ne pointe pas vers cet outil") as erreur:
        rappel.installer(executable=NOTRE)
    assert (etrangere or "sans commande") in str(erreur.value)  # affiche ce qui s'y trouve
    with pytest.raises(rappel.EchecRappel, match="n'est pas la nôtre"):
        rappel.desinstaller()
    assert json.dumps(registre.cles, sort_keys=True) == avant and registre.ecritures == ecritures


def test_hors_windows_non_pris_en_charge(monkeypatch):
    def absent():
        raise rappel.EchecRappel("connexion en un clic non prise en charge hors de Windows")

    monkeypatch.setattr(rappel, "_registre_reel", absent)
    assert rappel.etat() == {"pris_en_charge": False, "actif": False, "perime": False, "etranger": None}
    with pytest.raises(rappel.EchecRappel, match="non prise en charge"):
        rappel.installer()


def test_commandes_rappel_installer_et_desinstaller(capsys, tmp_path, registre):
    assert cli.main(["rappel-installer", "--json", "--donnees", str(tmp_path)]) == 0
    assert "activée" in json.loads(capsys.readouterr().out)["connexion_en_un_clic"]
    assert registre.cles[COMMANDE][""].endswith('lymow.exe" rappel "%1"') and registre.cles[COMMANDE][""].startswith('"')
    assert Path(registre.cles[COMMANDE][""].split('"')[1]).is_absolute()
    assert cli.main(["rappel-desinstaller", "--donnees", str(tmp_path)]) == 0
    assert COMMANDE not in registre.cles
    registre.cles[r"Software\Classes\myapp"] = {"": "URL:AutreApp"}
    assert cli.main(["rappel-installer", "--donnees", str(tmp_path)]) == 2
    assert "ne pointe pas vers cet outil" in capsys.readouterr().err


def test_page_active_et_desactive_par_les_memes_fonctions(tmp_path, monkeypatch, registre):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            assert serveur.instantane()["compte"]["un_clic"] == {
                "pris_en_charge": True, "actif": False, "perime": False, "etranger": None,
            }
            statut, corps, _recus = await poster(http, serveur, "/api/compte/un-clic", {"activer": True})
            assert statut == 200 and corps["un_clic"]["actif"] is True and COMMANDE in registre.cles
            assert serveur.instantane()["compte"]["un_clic"]["actif"] is True
            statut, corps, _recus = await poster(http, serveur, "/api/compte/un-clic", {"activer": False})
            assert statut == 200 and corps["un_clic"]["actif"] is False and COMMANDE not in registre.cles
            for corps_invalide in ({}, {"activer": "oui"}, {"activer": 1}):
                statut, _corps, _recus = await poster(http, serveur, "/api/compte/un-clic", corps_invalide)
                assert statut == 400
            registre.cles[r"Software\Classes\myapp"] = {"": "URL:AutreApp"}
            statut, erreur, _recus = await poster(http, serveur, "/api/compte/un-clic", {"activer": True})
            assert statut == 409 and "ne pointe pas vers cet outil" in erreur["erreur"]
            assert serveur.instantane()["compte"]["un_clic"]["etranger"] == "(clé myapp sans commande)"
            statut, _corps, _recus = await poster(http, serveur, "/api/compte/un-clic", {"activer": True}, Cookie=None)
            assert statut == 403
            statut, _corps, _recus = await poster(http, serveur, "/api/compte/un-clic", {"activer": True}, Origin="http://evil.example")
            assert statut == 403

    executer(scenario())


def test_ports_connus_et_fichier_de_ports(tmp_path):
    assert rappel.ports_connus(None) == [rappel.PORT_DEFAUT] and rappel.ports_connus(tmp_path) == [rappel.PORT_DEFAUT]
    rappel.noter_port(tmp_path, 9100, True)
    rappel.noter_port(tmp_path, 9200, True)
    rappel.noter_port(tmp_path, rappel.PORT_DEFAUT, True)
    assert rappel.ports_connus(tmp_path) == [rappel.PORT_DEFAUT, 9100, 9200]
    rappel.noter_port(tmp_path, 9100, False)
    assert rappel.ports_connus(tmp_path) == [rappel.PORT_DEFAUT, 9200]
    (tmp_path / rappel.FICHIER_PORTS).write_text("80 abc 70000 -1 9300 ../x", encoding="ascii")
    assert rappel.ports_connus(tmp_path) == [rappel.PORT_DEFAUT, 9300]


def test_page_explique_le_registre_et_garde_le_repli_manuel():
    page = (ui.STATIQUE / "index.html").read_text(encoding="utf-8")
    script = (ui.STATIQUE / "app.js").read_text(encoding="utf-8")
    assert "Activer la connexion en un clic" in page and "Windows va demander d'ouvrir Lymow : acceptez" in page
    assert "Repli manuel" in page and 'id="compte-adresse"' in page
    assert r"HKCU\\Software\\Classes\\myapp" in script and "lymow rappel-desinstaller" in script and "window.confirm(EXPLICATION_UN_CLIC)" in script
    assert AVANCER_1 and HttpOAuth and asyncio and lire  # outils partagés avec test_ui


# --- dixième avenant ---------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "affiche"),
    [
        ({"myapp": {"": "URL:AutreApp"}, r"myapp\shell\open\command": {"": r'"C:\Program Files\Autre\app.exe" "%1"'}}, "app.exe"),
        ({"myapp": {"": "URL:AutreApp"}}, "URL:AutreApp"),
        ({"myapp": {}}, "myapp sans commande"),
    ],
)
def test_cle_myapp_de_la_machine_vue_par_hkcr_refuse_l_installation(registre, machine, affiche):
    registre.machine.update(machine)
    constat = rappel.etat()
    assert constat["actif"] is False and constat["etranger"].startswith("clé de la machine") and affiche in constat["etranger"]
    with pytest.raises(rappel.EchecRappel, match="ne pointe pas vers cet outil") as erreur:
        rappel.installer(executable=NOTRE)
    assert affiche in str(erreur.value) and registre.cles == {} and registre.ecritures == 0
    with pytest.raises(rappel.EchecRappel, match="n'est pas la nôtre"):
        rappel.desinstaller()


def test_notre_cle_reconnue_par_egalite_exacte_sinon_perimee_et_reparable(registre):
    rappel.installer(executable=NOTRE)
    assert rappel.etat(executable=NOTRE) == {"pris_en_charge": True, "actif": True, "perime": False, "etranger": None}
    ailleurs = Path(r"D:\ancien\.venv\Scripts\lymow.exe")
    for periment in (ailleurs, Path(r"C:\outils\lymow\.venv\Scripts\LYMOW.EXE")):
        constat = rappel.etat(executable=periment)  # même libellé, autre exécutable : plus de simple suffixe
        assert (constat["actif"], constat["perime"], constat["etranger"]) == (False, True, None)
    registre.cles[COMMANDE][""] = f'"{NOTRE}" rappel "%1" --reel'  # notre libellé, commande altérée
    assert rappel.etat(executable=NOTRE)["perime"] is True
    message = rappel.installer(executable=ailleurs)  # « réparer » : réécrit vers l'exécutable courant
    assert "réparée" in message and registre.cles[COMMANDE][""] == f'"{ailleurs}" rappel "%1"'
    assert rappel.etat(executable=ailleurs)["actif"] is True


def test_desinstallation_d_une_cle_perimee_ou_incomplete_ne_laisse_rien(registre):
    rappel.installer(executable=NOTRE)  # périmée vis-à-vis de l'exécutable courant du test
    assert rappel.etat()["perime"] is True and "désactivée" in rappel.desinstaller()
    assert not [cle for cle in registre.cles if "myapp" in cle]  # plus de clé `myapp`, même vide
    registre.cles[r"Software\Classes\myapp"] = {"": rappel.LIBELLE, "URL Protocol": ""}  # notre libellé, sans commande
    assert rappel.etat()["perime"] is True and "désactivée" in rappel.desinstaller()
    assert not [cle for cle in registre.cles if "myapp" in cle]
    rappel.installer(executable=NOTRE)
    registre.cles[r"Software\Classes\myapp\shell\autre"] = {"": "ajout étranger"}
    with pytest.raises(rappel.EchecRappel, match="retrait incomplet"):
        rappel.desinstaller()


def test_commande_rappel_installer_repare_une_cle_perimee(capsys, tmp_path, registre):
    rappel.installer(executable=NOTRE)
    assert cli.main(["rappel-installer", "--json", "--donnees", str(tmp_path)]) == 0
    assert "réparée" in json.loads(capsys.readouterr().out)["connexion_en_un_clic"]
    assert rappel.etat()["actif"] is True


def test_page_propose_de_reparer(tmp_path, monkeypatch, registre):
    rappel.installer(executable=NOTRE)

    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False) as (serveur, http):
            assert serveur.instantane()["compte"]["un_clic"]["perime"] is True
            statut, corps, _recus = await poster(http, serveur, "/api/compte/un-clic", {"activer": True})
            assert statut == 200 and "réparée" in corps["message"] and corps["un_clic"] == {
                "pris_en_charge": True, "actif": True, "perime": False, "etranger": None,
            }

    executer(scenario())
    assert "Réparer la connexion en un clic" in (ui.STATIQUE / "app.js").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("issue", "code", "attendu"),
    [("réussie", 0, "Connexion réussie"), ("refusée", 1, "Connexion refusée par le service"), ("aucune", 1, "Aucune interface Lymow")],
)
def test_message_de_rappel_selon_la_reponse_reelle_de_l_interface(monkeypatch, capsys, issue, code, attendu):
    lus = []

    async def transmettre(_adresse, ports):
        lus.append(ports)
        return issue

    monkeypatch.setattr(rappel, "transmettre", transmettre)
    rappel.noter_port(rappel.dossier_ports(), 9123, True)
    assert cli.main(["rappel", VALIDE]) == code
    sortie = capsys.readouterr()
    assert attendu in sortie.out + sortie.err and "abc-123" not in sortie.out + sortie.err
    assert lus == [[rappel.PORT_DEFAUT, 9123]]  # même fichier de ports que l'interface


def test_rappel_echange_refuse_par_le_service_dit_refusee(tmp_path, monkeypatch, service_compte):
    async def scenario():
        async with serveur_ui(tmp_path, monkeypatch, reel=False, coffre_factice={}) as (serveur, http):
            serveur.ouvrir_http = service_compte
            service_compte.statut = 400
            await poster(http, serveur, "/api/compte/commencer", {"region": "eu-west-1"})
            bonne = adresse_de(serveur)["adresse"]
            monkeypatch.setattr(rappel, "PORT_DEFAUT", serveur.port)
            assert await rappel.transmettre(bonne, rappel.ports_connus(None)) == rappel.REFUSEE
            assert await rappel.transmettre(bonne, rappel.ports_connus(None)) == rappel.AUCUNE  # plus de tentative

    executer(scenario())


def test_dossier_des_ports_independant_du_repertoire_courant(monkeypatch, tmp_path, registre):
    monkeypatch.chdir(tmp_path)
    assert (registre.dossier_ports_reel() / "pyproject.toml").is_file()  # la vraie fonction, sans le double
