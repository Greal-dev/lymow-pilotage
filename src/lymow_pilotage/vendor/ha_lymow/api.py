"""Client REST Lymow — copie minimale.

Provenance : https://github.com/8408323/ha-lymow — custom_components/lymow/api.py
Commit     : 826dde08feca410feb51415f484e3ab0f9966964
Licence    : MIT (voir LICENSES-TIERS.md à la racine du projet)

Modifications par rapport à l'original :
- seules ``get_devices`` et ``get_device_info`` sont recopiées (aucune écriture :
  ni mise à jour, ni carte, ni caméra, ni micrologiciel, ni renommage) ;
- ``raise_for_status`` remplacé par ``LymowApiError`` : l'exception d'origine
  cite l'URL complète, qui contient l'identité du compte et de l'appareil ;
  la nôtre ne porte que le code HTTP ;
- signatures SigV4 S3 et Kinesis retirées.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from .const import REGION_CONFIG


class LymowApiError(RuntimeError):
    """Raised when the Lymow REST API answers with an HTTP error."""


def _api_url(region: str, gateway_key: str, path: str) -> str:
    gw_id = REGION_CONFIG[region][gateway_key]
    assert isinstance(gw_id, str)
    return f"https://{gw_id}.execute-api.{region}.amazonaws.com{path}"


class LymowApiClient:
    def __init__(self, session: aiohttp.ClientSession, access_token: str, region: str, identity_id: str) -> None:
        self._session = session
        self._access_token = access_token
        self._region = region
        self._identity_id = identity_id

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._access_token}

    async def _get(self, url: str, params: dict[str, str]) -> Any:
        async with self._session.get(url, headers=self._headers, params=params) as resp:
            if not resp.ok:
                raise LymowApiError(f"Lymow API request failed with HTTP {resp.status}")
            return await resp.json(content_type=None)

    async def get_devices(self) -> list[dict[str, Any]]:
        url = _api_url(self._region, "api_device_list", "/prod/device-list-query")
        return await self._get(url, {"p": "devices", "identityId": self._identity_id})

    async def get_device_info(self, thing_name: str) -> dict[str, Any]:
        url = _api_url(self._region, "api_device_info", "/prod/get-device-info")
        return await self._get(url, {"deviceThingName": thing_name})
