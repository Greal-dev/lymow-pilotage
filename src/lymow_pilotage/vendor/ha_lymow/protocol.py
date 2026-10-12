"""Encodage et décodage protobuf des messages MQTT Lymow — copie minimale.

Provenance : https://github.com/8408323/ha-lymow — custom_components/lymow/protocol.py
Commit     : 826dde08feca410feb51415f484e3ab0f9966964
Licence    : MIT (voir LICENSES-TIERS.md à la racine du projet)

Modifications par rapport à l'original :
- sous-ensemble uniquement : primitives varint/champs, enveloppe JSON base64,
  décodeur générique, ``decode_pboutput`` réduit à l'état utile au pilotage,
  battement de présence, demande de configuration ;
- ``decode_pboutput`` : cartes, chemins, planifications, rapports de tonte et
  diagnostics RTK retirés ; du profil d'appareil seules les versions logicielles
  sont lues (adresse IP, adresse MAC, numéro de série, SSID jamais décodés) ;
  de la configuration robot seule la hauteur de coupe (``rcCutHeight``) est lue ;
- ``encode_query_robot_config`` : version de protocole en paramètre (défaut
  inchangé : 49) ;
- ``encode_userctrl`` (générique, tout code) non recopié : les seuls codes
  émissibles passent par la liste fermée de ``lymow_pilotage.protocole`` ;
- ``unwrap_envelope`` : le message d'erreur ne cite plus les clés reçues ;
- carte : seuls sont recopiés la demande de carte (``encode_query_map``), l'extraction
  du contenu brut (``extract_raw_map_content``), le codage des points et des polygones
  et la retouche du contour d'UNE zone (``encode_set_zone_polygon``, seule écriture de
  carte que le socle dit vérifiée en réel), avec la version de protocole en paramètre ;
  ``decode_map_response`` n'est pas recopié (le projet garde les octets bruts) ;
- envoi de carte entière (``encode_sync_map*``), création, suppression, fusion,
  découpe et renommage de zones, passages, réglages, Wi-Fi, code PIN et trame de
  pilotage Bluetooth : non recopiés.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

# Protocol version used in all outgoing PbInput messages (confirmed from ADB capture: field 2 = 49)
PB_VERSION = 49

# ---------------------------------------------------------------------------
# Varint helpers
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = []
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            break
    return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _decode_packed_int32s(data: bytes) -> list[int]:
    """Decode a packed repeated int32 field (raw varint stream, no tags)."""
    pos = 0
    values: list[int] = []
    while pos < len(data):
        v, pos = _decode_varint(data, pos)
        values.append(_signed32(v))
    return values


# ---------------------------------------------------------------------------
# Field encoding helpers
# ---------------------------------------------------------------------------


def _field_i32(field_no: int, value: int) -> bytes:
    """Encode a signed/unsigned int32 field (wire type 0 = varint)."""
    tag = _encode_varint((field_no << 3) | 0)
    return tag + _encode_varint(value & 0xFFFFFFFFFFFFFFFF)


def _field_bytes(field_no: int, data: bytes) -> bytes:
    """Encode a length-delimited field (wire type 2)."""
    tag = _encode_varint((field_no << 3) | 2)
    return tag + _encode_varint(len(data)) + data


def _field_str(field_no: int, value: str) -> bytes:
    return _field_bytes(field_no, value.encode("utf-8"))


def _field_f32(field_no: int, value: float) -> bytes:
    """Encode a float32 field (wire type 5 = 32-bit fixed)."""
    tag = _encode_varint((field_no << 3) | 5)
    return tag + struct.pack("<f", value)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def wrap_envelope(pb_bytes: bytes) -> str:
    """Encode protobuf bytes as a JSON envelope string ready for MQTT publish."""
    return json.dumps({"message": base64.b64encode(pb_bytes).decode()})


def unwrap_envelope(payload: str | bytes) -> bytes:
    """Decode the JSON envelope from an MQTT message and return raw protobuf bytes."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    obj = json.loads(payload)
    for key in ("message", "value", "data", "payload"):
        if key in obj:
            return base64.b64decode(obj[key])
    raise ValueError("No known envelope key")


# ---------------------------------------------------------------------------
# Protobuf decoder (minimal — handles varint, length-delimited, 32/64-bit)
# ---------------------------------------------------------------------------


def _decode_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Return list of (field_no, wire_type, value) tuples from a protobuf blob."""
    pos = 0
    fields: list[tuple[int, int, Any]] = []
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_no = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            value, pos = _decode_varint(data, pos)
            fields.append((field_no, wire_type, value))
        elif wire_type == 1:  # 64-bit
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
            fields.append((field_no, wire_type, value))
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(data, pos)
            value = data[pos : pos + length]
            pos += length
            fields.append((field_no, wire_type, value))
        elif wire_type == 5:  # 32-bit
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            fields.append((field_no, wire_type, value))
        else:
            break  # unknown wire type — stop parsing
    return fields


def _first(fields: list[tuple[int, int, Any]], field_no: int, default: Any = None) -> Any:
    for fn, _wt, val in fields:
        if fn == field_no:
            return val
    return default


def _all(fields: list[tuple[int, int, Any]], field_no: int) -> list[Any]:
    return [val for fn, _wt, val in fields if fn == field_no]


def _signed32(v: int) -> int:
    """Interpret a varint as a signed int32, handling negative 64-bit encodings."""
    v &= 0xFFFFFFFF
    if v >= 0x80000000:
        v -= 0x100000000
    return v


def _decode_f32(raw: int) -> float:
    """Decode a raw uint32 (from wire type 5) as an IEEE 754 single-precision float."""
    return struct.unpack("<f", struct.pack("<I", raw & 0xFFFFFFFF))[0]
def decode_pboutput(pb_bytes: bytes) -> dict[str, Any]:
    """Decode a PbOutput protobuf blob into a flat state dict.

    Field layout (from capture analysis of the robot ↔ app protocol):
      field 2  (varint)   protocol version
      field 3  (bytes)    packed errorCodes (repeated int32)
      field 4  (bytes)    packed warningCodes (repeated int32)
      field 5  (bytes)    PbRobotInfo sub-message
      field 10 (bytes)    PbDeviceProfile sub-message
    """
    state: dict[str, Any] = {}
    fields = _decode_fields(pb_bytes)

    # Error / warning codes — packed repeated int32 (raw varint stream, no field tags)
    error_raw = _first(fields, 3)
    if isinstance(error_raw, bytes) and error_raw:
        state["errorCodes"] = _decode_packed_int32s(error_raw)
        state["errorCode"] = state["errorCodes"][0] if state["errorCodes"] else 0
    else:
        state["errorCodes"] = []
        state["errorCode"] = 0

    warning_raw = _first(fields, 4)
    if isinstance(warning_raw, bytes) and warning_raw:
        state["warningCodes"] = _decode_packed_int32s(warning_raw)
    else:
        state["warningCodes"] = []

    # PbRobotInfo (field 5) — field map verified against PbRobotInfo.encode
    # (Hermes fn #9734 at offset 0x004b842c): f1 robotStatus, f2 battery,
    # f3 wifiSignalQuality, f4 lteSignalQuality, f5 btSignalQuality,
    # f6 workStatus, f7 isRecharging, f8 isCharging, f9 wifiWorking,
    # f10 lteWorking. All int32 / bool varints.
    robot_info_raw = _first(fields, 5)
    if isinstance(robot_info_raw, bytes):
        ri = _decode_fields(robot_info_raw)
        ws_raw = _first(ri, 6)
        state["workStatus"] = _signed32(ws_raw) if ws_raw is not None else -1
        robot_state_raw = _first(ri, 1)
        if robot_state_raw is not None:
            state["robotState"] = _signed32(robot_state_raw)
        battery = _first(ri, 2)
        if battery is not None:
            state["battery"] = _signed32(battery)
        state["isCharging"] = bool(_first(ri, 8, 0))
        state["isRecharging"] = bool(_first(ri, 7, 0))
        wifi_sig = _first(ri, 3)
        if wifi_sig is not None:
            state["wifiSignalQuality"] = _signed32(wifi_sig)
        lte_sig = _first(ri, 4)
        if lte_sig is not None:
            state["lteSignalQuality"] = _signed32(lte_sig)
        # f5 btSignalQuality, f9 wifiWorking, f10 lteWorking — names APK-verified
        # (Hermes v96). The "working" flags say which link is currently active.
        bt_sig = _first(ri, 5)
        if bt_sig is not None:
            state["btSignalQuality"] = _signed32(bt_sig)
        wifi_working = _first(ri, 9)
        if wifi_working is not None:
            state["wifiWorking"] = bool(wifi_working)
        lte_working = _first(ri, 10)
        if lte_working is not None:
            state["lteWorking"] = bool(lte_working)

    # PbDeviceProfile (champ 10) : versions logicielles seulement. Les autres
    # champs (IP, MAC, numéro de série, SSID) sont des identifiants : jamais lus.
    profile_raw = _first(fields, 10)
    if isinstance(profile_raw, bytes):
        dp = _decode_fields(profile_raw)
        for field_no, key in ((1, "fwVersion"), (2, "mcuVersion"), (3, "softwareVersion")):
            val = _first(dp, field_no)
            if isinstance(val, bytes):
                state[key] = val.decode("utf-8", errors="replace")

    # PbRobotConfig (champ 17) : f3 rcCutHeight (mm), seule valeur utile ici.
    robot_config_raw = _first(fields, 17)
    if isinstance(robot_config_raw, bytes):
        cut_height = _first(_decode_fields(robot_config_raw), 3)
        if isinstance(cut_height, int):
            state["rcCutHeight"] = _signed32(cut_height)

    # GPS / RTK (field 6 of outer PbOutput):
    #   f1=satellites(int), f2=eastM(float32), f3=northM(float32), f4=rtkStatus(int)
    rtk_raw = _first(fields, 6)
    if isinstance(rtk_raw, bytes):
        rtk = _decode_fields(rtk_raw)
        sats = _first(rtk, 1)
        if sats is not None:
            state["rtkSatellites"] = _signed32(sats)
        east = _first(rtk, 2)
        if east is not None:
            state["rtkEastM"] = _decode_f32(east)
        north = _first(rtk, 3)
        if north is not None:
            state["rtkNorthM"] = _decode_f32(north)
        rtk_status = _first(rtk, 4)
        if rtk_status is not None:
            state["rtkStatus"] = _signed32(rtk_status)
    # Wi-Fi sub-message (field 22): f6=rssiDbm (UTF-8 string like "-77")
    wifi22_raw = _first(fields, 22)
    if isinstance(wifi22_raw, bytes):
        wifi22_fields = _decode_fields(wifi22_raw)
        rssi_raw = _first(wifi22_fields, 6)
        if isinstance(rssi_raw, bytes):
            try:
                state["wifiRssiDbm"] = int(rssi_raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                pass  # best-effort parse; leave the field unset

    # Robot pose ENU (field 14): f1=eastM, f2=northM, f3=thetaRad (all float32)
    pose_raw = _first(fields, 14)
    if isinstance(pose_raw, bytes):
        pose_fields = _decode_fields(pose_raw)
        east_m = _first(pose_fields, 1)
        north_m = _first(pose_fields, 2)
        theta_rad = _first(pose_fields, 3)
        if east_m is not None:
            state["poseEastM"] = _decode_f32(east_m)
        if north_m is not None:
            state["poseNorthM"] = _decode_f32(north_m)
        if theta_rad is not None:
            state["poseThetaRad"] = _decode_f32(theta_rad)

    return state


# ---------------------------------------------------------------------------
# PbInput encoders — commands sent to the robot
# ---------------------------------------------------------------------------


def encode_app_connect_heartbeat(session_id: str) -> bytes:
    """Encode the app-presence heartbeat the app sends to register as connected.

    Wire layout from APK/Hermes `heartbeat` (fn #8981): PbInput{appConnect = field 7
    = ConnectToggle.TOGGLE_CONNECTED (2), uuid = field 27 (string)}. No version field.
    """
    return _field_i32(7, 2) + _field_bytes(27, session_id.encode())


def encode_query_robot_config(version: int = PB_VERSION) -> bytes:
    """Encode a getRobotConfig command using the f9 sub-message format.

    The robot does NOT respond to a plain userCtrl=35 (f5=35). The correct wire
    format confirmed from app capture is PbInput{f2=version, f9={f10=1}}.
    """
    pb = _field_i32(2, version)
    pb += _field_bytes(9, _field_i32(10, 1))  # PbInput.f9={f10=1} = getRobotConfig
    return pb


# ---------------------------------------------------------------------------
# Carte : lecture (QUERY_MAP) et retouche du contour d une zone (MODIFY_ZONE_INFO)
# ---------------------------------------------------------------------------


def _decode_map_point(data: bytes) -> dict[str, float]:
    """Decode an x/y point sub-message where f1=x and f2=y are i32 floats."""
    f = _decode_fields(data)
    x_raw = _first(f, 1)
    y_raw = _first(f, 2)
    return {
        "x": _decode_f32(x_raw) if x_raw is not None else 0.0,
        "y": _decode_f32(y_raw) if y_raw is not None else 0.0,
    }


def _decode_map_polygon(data: bytes) -> list[dict[str, float]]:
    """Decode a polygon sub-message containing repeated f1 point sub-messages."""
    f = _decode_fields(data)
    return [_decode_map_point(p) for p in _all(f, 1) if isinstance(p, bytes)]


def extract_raw_map_content(pb_bytes: bytes) -> bytes | None:
    """Extract raw PbMap content bytes from a map-response PbOutput blob.

    Navigation path: PbOutput.f23 → f2 → f3 = raw PbMap content.
    Returns None if the map response is not present in the message.
    """
    top = _decode_fields(pb_bytes)
    outer_raw = _first(top, 23)
    if not isinstance(outer_raw, bytes):
        return None
    wrapper_raw = _first(_decode_fields(outer_raw), 2)
    if not isinstance(wrapper_raw, bytes):
        return None
    content_raw = _first(_decode_fields(wrapper_raw), 3)
    return content_raw if isinstance(content_raw, bytes) else None


def encode_query_map(queryIndex: int = 0, version: int = PB_VERSION) -> bytes:
    """Encode a query-map command (userCtrl=19)."""
    sub = _field_i32(1, queryIndex) + _field_i32(4, 1)
    pb = _field_i32(2, version)
    pb += _field_i32(5, 19)  # USER_CTRL_QUERY_MAP
    pb += _field_bytes(23, sub)
    return pb


def _encode_map_point(pt: dict) -> bytes:
    """Encode an x/y point sub-message (f1=x, f2=y, both wire-type-5 float32)."""
    return _field_f32(1, pt.get("x", 0.0)) + _field_f32(2, pt.get("y", 0.0))


def _encode_map_polygon(points: list) -> bytes:
    """Encode a polygon as repeated f1 point sub-messages."""
    out = b""
    for pt in points:
        out += _field_bytes(1, _encode_map_point(pt))
    return out


def encode_set_zone_polygon(hash_id: str, polygon: list, version: int = PB_VERSION) -> bytes:
    """Encode a go-zone boundary edit over WiFi (USER_CTRL_MODIFY_ZONE_INFO).

    Same envelope as rename (PbInput.field12 → PbMap.goZones → PbZone.basicInfo),
    but basicInfo carries the new polygon (f5) instead of the name. The robot
    applies the pushed polygon — verified live. (NOT sync_map, which the robot
    ignores.) ``polygon`` is a list of {"x","y"} ENU points.
    """
    from .const import USER_CTRL_MODIFY_ZONE_INFO

    basic_info = _field_str(3, hash_id) + _field_bytes(5, _encode_map_polygon(polygon))
    zone = _field_bytes(1, basic_info)
    pb_map = _field_bytes(1, zone)  # PbMap.goZones
    return _field_i32(2, version) + _field_i32(5, USER_CTRL_MODIFY_ZONE_INFO) + _field_bytes(12, pb_map)
