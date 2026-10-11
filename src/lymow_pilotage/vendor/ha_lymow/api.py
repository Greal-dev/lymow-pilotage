"""Client REST Lymow — copie minimale.

Provenance : https://github.com/8408323/ha-lymow — custom_components/lymow/api.py
Commit     : 826dde08feca410feb51415f484e3ab0f9966964
Licence    : MIT (voir LICENSES-TIERS.md à la racine du projet)

Modifications par rapport à l'original :
- seules ``get_devices``, ``get_device_info`` et la vidéo sont recopiées (aucune
  écriture : ni mise à jour, ni carte, ni micrologiciel, ni renommage) ;
- ``raise_for_status`` remplacé par ``LymowApiError`` : l'exception d'origine
  cite l'URL complète, qui contient l'identité du compte et de l'appareil ;
  la nôtre ne porte que le code HTTP ;
- signature SigV4 S3 retirée ;
- signalisation vidéo Kinesis recopiée telle quelle (``start_video_session``,
  ``get_signaling_channel_endpoint``, ``get_ice_server_config``,
  ``presign_signaling_url``, ``viewer_client_id``, ``_kvs_sigv4_headers``), avec
  deux retouches : ``LymowApiError`` au lieu de ``raise_for_status`` et
  ``allow_redirects=False`` sur les POST. Rien n'y est journalisé.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

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

    async def start_video_session(self, thing_name: str) -> dict[str, Any]:
        """POST /prod/kvs/cmd with action="start".

        Response:
            {"credentials": {"accessKeyId", "secretAccessKey",
                             "sessionToken", "expiration"},
             "channelARN": "arn:aws:kinesisvideo:<region>:<acct>:channel/<thing>_stream_channel/<ts>",
             "region": "<region>",
             "deviceThingName": "<thing>"}

        Credentials expire in ~15 minutes; callers must complete the
        WebRTC handshake within that window.
        """
        if not REGION_CONFIG[self._region].get("api_kvs"):
            raise NotImplementedError(f"Kinesis Video gateway not configured for region {self._region!r}")
        url = _api_url(self._region, "api_kvs", "/prod/kvs/cmd")
        body = {"deviceThingName": thing_name, "action": "start"}
        async with self._session.post(url, headers=self._headers, json=body, allow_redirects=False) as resp:
            if not resp.ok:
                raise LymowApiError(f"Lymow video request failed with HTTP {resp.status}")
            return await resp.json(content_type=None)

    async def get_signaling_channel_endpoint(
        self, channel_arn: str, creds: dict[str, str], *, role: str = "VIEWER", region: str | None = None
    ) -> dict[str, str]:
        """Resolve a KVS signaling channel's endpoints (SigV4-signed, KVS temp creds).

        Returns ``{"WSS": "wss://…", "HTTPS": "https://…"}`` from the
        ResourceEndpointList. ``creds`` is the ``credentials`` object from
        :meth:`start_video_session` (accessKeyId/secretAccessKey/sessionToken).
        """
        region = region or self._region
        host = f"kinesisvideo.{region}.amazonaws.com"
        payload = json.dumps(
            {
                "ChannelARN": channel_arn,
                "SingleMasterChannelEndpointConfiguration": {"Protocols": ["WSS", "HTTPS"], "Role": role},
            }
        ).encode()
        headers = _kvs_sigv4_headers(
            "POST",
            host,
            "/getSignalingChannelEndpoint",
            payload,
            region,
            creds["accessKeyId"],
            creds["secretAccessKey"],
            creds["sessionToken"],
        )
        async with self._session.post(
            f"https://{host}/getSignalingChannelEndpoint", headers=headers, data=payload, allow_redirects=False
        ) as resp:
            if not resp.ok:
                raise LymowApiError(f"Lymow video request failed with HTTP {resp.status}")
            data = await resp.json(content_type=None)
        endpoints = {}
        items = data.get("ResourceEndpointList", []) if isinstance(data, dict) else []
        for ep in items if isinstance(items, list) else []:
            if isinstance(ep, dict) and ep.get("Protocol") and ep.get("ResourceEndpoint"):
                endpoints[ep["Protocol"]] = ep["ResourceEndpoint"]
        return endpoints

    async def get_ice_server_config(
        self, channel_arn: str, https_endpoint: str, creds: dict[str, str], *, region: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch TURN/STUN servers for the channel (SigV4-signed, KVS temp creds).

        ``https_endpoint`` is the HTTPS entry from
        :meth:`get_signaling_channel_endpoint`. Returns the IceServerList.
        """
        region = region or self._region
        host = urlparse(https_endpoint).netloc
        payload = json.dumps({"ChannelARN": channel_arn}).encode()
        headers = _kvs_sigv4_headers(
            "POST",
            host,
            "/v1/get-ice-server-config",
            payload,
            region,
            creds["accessKeyId"],
            creds["secretAccessKey"],
            creds["sessionToken"],
        )
        async with self._session.post(
            f"{https_endpoint}/v1/get-ice-server-config", headers=headers, data=payload, allow_redirects=False
        ) as resp:
            if not resp.ok:
                raise LymowApiError(f"Lymow video request failed with HTTP {resp.status}")
            data = await resp.json(content_type=None)
        servers = data.get("IceServerList", []) if isinstance(data, dict) else []
        return servers if isinstance(servers, list) else []

    def presign_signaling_url(
        self,
        wss_endpoint: str,
        channel_arn: str,
        client_id: str,
        creds: dict[str, str],
        *,
        role: str = "VIEWER",
        region: str | None = None,
        expires: int = 299,
    ) -> str:
        """SigV4-presign the KVS signaling WebSocket connect URL.

        The query carries X-Amz-ChannelARN plus, for a VIEWER, X-Amz-ClientId
        alongside the standard SigV4 params; unlike the IoT MQTT presign the
        session token is part of the *signed* canonical query string. A MASTER
        connects without a client id. ``creds`` is the ``credentials`` object
        from :meth:`start_video_session`.
        """
        if role not in {"VIEWER", "MASTER"}:
            raise ValueError("role must be VIEWER or MASTER")

        parsed_endpoint = urlparse(wss_endpoint)
        region = region or self._region
        host = parsed_endpoint.netloc
        path = parsed_endpoint.path or "/"
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_str = now.strftime("%Y%m%d")
        scope = f"{date_str}/{region}/kinesisvideo/aws4_request"
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-ChannelARN": channel_arn,
            "X-Amz-Credential": f"{creds['accessKeyId']}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-Security-Token": creds["sessionToken"],
            "X-Amz-SignedHeaders": "host",
        }
        if role == "VIEWER":
            query["X-Amz-ClientId"] = client_id
        canonical_qs = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(query.items()))
        canonical = f"GET\n{path}\n{canonical_qs}\nhost:{host}\n\nhost\n{hashlib.sha256(b'').hexdigest()}"
        sts = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        k = _hmac_sha256(("AWS4" + creds["secretAccessKey"]).encode(), date_str)
        k = _hmac_sha256(k, region)
        k = _hmac_sha256(k, "kinesisvideo")
        k = _hmac_sha256(k, "aws4_request")
        signature = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        return urlunparse(
            (
                parsed_endpoint.scheme,
                parsed_endpoint.netloc,
                path,
                parsed_endpoint.params,
                f"{canonical_qs}&X-Amz-Signature={signature}",
                parsed_endpoint.fragment,
            )
        )

    def viewer_client_id(self) -> str:
        """Build a unique KVS viewer client id carrying the account's Cognito sub.

        The robot's master answers a viewer whose id ends in ``_userId_<sub>``
        (the app uses the same shape); the random prefix keeps concurrent HA
        viewers distinct so they don't collide on the signaling channel.
        """
        sub = ""
        try:
            payload = self._access_token.split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub", "")
        except (ValueError, IndexError, KeyError):
            sub = ""
        return f"ha-lymow_{secrets.token_hex(4)}_userId_{sub}"


def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode(), hashlib.sha256).digest()


def _kvs_sigv4_headers(
    method: str,
    host: str,
    uri: str,
    body: bytes,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    service: str = "kinesisvideo",
) -> dict[str, str]:
    """Build SigV4 request headers for a kinesisvideo (or other AWS) JSON POST.

    Uses the temporary credentials returned by /prod/kvs/cmd. Unlike the S3
    helper this signs over a JSON body and does not send x-amz-content-sha256.
    """
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_str = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    signed_headers = "host;x-amz-date;x-amz-security-token"
    canonical = (
        f"{method}\n{uri}\n\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-security-token:{session_token}\n\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )
    scope = f"{date_str}/{region}/{service}/aws4_request"
    sts = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    k = _hmac_sha256(("AWS4" + secret_key).encode(), date_str)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, service)
    k = _hmac_sha256(k, "aws4_request")
    signature = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return {
        "Authorization": auth,
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
        "Content-Type": "application/json",
    }
