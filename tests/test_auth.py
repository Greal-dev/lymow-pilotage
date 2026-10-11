"""Connexion : URL PKCE + state, vérification du state, échange simulé, aucune fuite."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from lymow_pilotage import auth
from lymow_pilotage.vendor.ha_lymow.auth import LymowAuth, LymowAuthError

CORPS_SECRET = '{"__type":"NotAuthorizedException","message":"jean.dupont@example.org jeton-fuite-9876"}'


class FausseReponse:
    def __init__(self, statut: int, donnees: dict | None = None, texte: str = "") -> None:
        self.status, self.ok, self._donnees, self._texte = statut, statut < 400, donnees, texte

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def json(self, content_type=None):
        return self._donnees

    async def text(self):
        return self._texte

    async def read(self):
        return self._texte.encode()


class FauxHttp:
    def __init__(self, *reponses: FausseReponse) -> None:
        self.reponses = list(reponses)
        self.appels: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.appels.append((url, kwargs))
        return self.reponses.pop(0)


def test_url_d_autorisation_porte_pkce_s256_et_state():
    demande = auth.preparer_connexion("eu-west-1")
    url = urlparse(demande.url)
    q = {cle: valeurs[0] for cle, valeurs in parse_qs(url.query).items()}
    assert (url.scheme, url.netloc, url.path) == ("https", "eu-auth.lymow.com", "/oauth2/authorize")
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "myapp://callback/"
    assert q["identity_provider"] == "Google"
    assert q["client_id"] == "3h1sqv3hishjiofbv8giskjgb0"
    assert q["state"] == demande.etat and len(demande.etat) >= 32
    assert q["code_challenge_method"] == "S256"
    defi = base64.urlsafe_b64encode(hashlib.sha256(demande.verificateur.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == defi
    assert demande.verificateur not in demande.url
    assert demande.verificateur not in repr(demande)


def test_state_et_verificateur_changent_a_chaque_demande():
    a, b = auth.preparer_connexion("eu-west-1"), auth.preparer_connexion("eu-west-1")
    assert a.etat != b.etat and a.verificateur != b.verificateur


def test_region_inconnue_refusee():
    with pytest.raises(auth.ErreurAuth):
        auth.preparer_connexion("mars-nord-1")


def test_socle_refuse_une_url_sans_state_ou_sans_pkce():
    for state, defi in (("", "defi"), ("etat", "")):
        with pytest.raises(LymowAuthError):
            LymowAuth(None).get_oauth_authorize_url(
                region="eu-west-1", redirect_uri="myapp://callback/", state=state, code_challenge=defi
            )


def test_extraire_code_accepte_l_url_de_rappel_au_bon_state():
    assert auth.extraire_code(" myapp://callback/?code=LE-CODE&state=bon-etat \n", "bon-etat") == "LE-CODE"


@pytest.mark.parametrize(
    "saisie",
    [
        "myapp://callback/?code=LE-CODE&state=autre-etat",  # state différent
        "myapp://callback/?code=LE-CODE",  # state absent
        "myapp://callback/?state=bon-etat",  # code absent
        "myapp://callback/?error=access_denied&state=bon-etat",
        "LE-CODE",  # code seul : state invérifiable
        "https://evil.example/?code=LE-CODE&state=bon-etat",
        "",
    ],
)
def test_extraire_code_rejette(saisie):
    with pytest.raises(auth.ErreurAuth) as erreur:
        auth.extraire_code(saisie, "bon-etat")
    assert "LE-CODE" not in str(erreur.value)


def test_echange_simule_envoie_le_verificateur_et_rend_les_jetons():
    demande = auth.preparer_connexion("eu-west-1")
    http = FauxHttp(FausseReponse(200, {"access_token": "acc-1234", "id_token": "idt-1234", "refresh_token": "raf-1234"}))
    saisie = f"myapp://callback/?code=LE-CODE&state={demande.etat}"
    jetons = asyncio.run(auth.echanger_code(http, demande, saisie))
    assert jetons["RefreshToken"] == "raf-1234" and jetons["AccessToken"] == "acc-1234"
    url, kwargs = http.appels[0]
    assert url == "https://eu-auth.lymow.com/oauth2/token"
    assert kwargs["data"] == {
        "grant_type": "authorization_code",
        "client_id": "3h1sqv3hishjiofbv8giskjgb0",
        "code": "LE-CODE",
        "redirect_uri": "myapp://callback/",
        "code_verifier": demande.verificateur,
    }


def test_echange_avec_un_mauvais_state_ne_contacte_pas_le_serveur():
    demande = auth.preparer_connexion("eu-west-1")
    http = FauxHttp()
    with pytest.raises(auth.ErreurAuth):
        asyncio.run(auth.echanger_code(http, demande, "myapp://callback/?code=LE-CODE&state=faux"))
    assert http.appels == []


def test_echec_d_echange_sans_corps_de_reponse_dans_l_erreur():
    demande = auth.preparer_connexion("eu-west-1")
    http = FauxHttp(FausseReponse(400, texte=CORPS_SECRET))
    with pytest.raises(LymowAuthError) as erreur:
        asyncio.run(auth.echanger_code(http, demande, f"myapp://callback/?code=LE-CODE&state={demande.etat}"))
    assert "400" in str(erreur.value)
    assert "jeton-fuite" not in str(erreur.value) and "example.org" not in str(erreur.value)


@pytest.mark.parametrize("methode", [auth.METHODE_GOOGLE, auth.METHODE_MOT_DE_PASSE])
def test_echec_de_rafraichissement_sans_corps_ni_jeton_dans_l_erreur(methode):
    http = FauxHttp(FausseReponse(400, texte=CORPS_SECRET))
    with pytest.raises(LymowAuthError) as erreur:
        asyncio.run(auth.rafraichir(http, "raf-secret-555", "eu-west-1", methode))
    for interdit in ("jeton-fuite", "example.org", "raf-secret-555"):
        assert interdit not in str(erreur.value)


def test_rafraichissement_google_conserve_le_jeton_si_le_serveur_n_en_rend_pas():
    http = FauxHttp(FausseReponse(200, {"access_token": "acc-1234", "id_token": "idt-1234"}))
    jetons = asyncio.run(auth.rafraichir(http, "raf-secret-555", "eu-west-1", auth.METHODE_GOOGLE))
    assert jetons["RefreshToken"] == "raf-secret-555"
    assert http.appels[0][1]["data"]["grant_type"] == "refresh_token"


def test_echec_de_connexion_par_mot_de_passe_sans_fuite():
    http = FauxHttp(FausseReponse(400, texte=CORPS_SECRET))
    with pytest.raises(LymowAuthError) as erreur:
        asyncio.run(auth.connexion_mot_de_passe(http, "jean.dupont@example.org", "motdepasse-777", "eu-west-1"))
    for interdit in ("jeton-fuite", "example.org", "motdepasse-777"):
        assert interdit not in str(erreur.value)
