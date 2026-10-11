"""Constantes du nuage Lymow — copie minimale.

Provenance : https://github.com/8408323/ha-lymow — custom_components/lymow/const.py
Commit     : 826dde08feca410feb51415f484e3ab0f9966964
Licence    : MIT (voir LICENSES-TIERS.md à la racine du projet)

Modifications par rapport à l'original :
- sous-ensemble uniquement : domaines Cognito, configuration par région, statuts
  de travail, libellés d'erreur et d'avertissement, bornes de vitesse relevées
  en Bluetooth, qualité RTK ;
- codes userCtrl : seuls les cinq codes que l'outil a le droit d'émettre sont
  recopiés (aucun code de démarrage de tonte) ;
- tout ce qui concerne Home Assistant, la caméra et le Bluetooth est retiré.
"""

from __future__ import annotations

COGNITO_DOMAINS: dict[str, str] = {
    "eu-west-1": "eu-auth.lymow.com",
    "ap-southeast-2": "ap-auth.lymow.com",
    "us-east-2": "us-auth.lymow.com",
    "ap-east-1": "lymow.auth.ap-east-1.amazoncognito.com",
}

# Per-region AWS configuration — all values extracted from traffic capture and APK analysis
REGION_CONFIG: dict[str, dict[str, str | None]] = {
    "eu-west-1": {
        "client_id": "3h1sqv3hishjiofbv8giskjgb0",
        "user_pool_id": "eu-west-1_6qNPbnrrd",
        "identity_pool_id": "eu-west-1:c905a69c-0153-401a-a879-0c50b892015b",
        "iot_host": "a3j5zqqo5iuph9-ats.iot.eu-west-1.amazonaws.com",
        "api_device_list": "asjqh5wbtj",
        "api_device_info": "6ghz1zkccg",
        "api_ota_check": "eigc6a2ds9",
        "api_ota_job": "io4nsakkt8",  # from APK strings; create-ota-job + get-ota-job-summary
        "api_map": "3q1zxz98l2",
        "api_user_account": "l3hazobjk0",
        "api_kvs": "frgai1jfwg",  # confirmed from live capture 2026-05-19
        "s3_bucket": None,  # not yet confirmed from capture
    },
    "us-east-2": {
        "client_id": "3ftv5jumkv375hic8dpdqodj8n",  # from APK bundle
        "user_pool_id": "us-east-2_GAyiLkZQf",  # from APK bundle
        "identity_pool_id": "us-east-2:037db699-5df0-4ed2-92b8-0dd0f1843918",
        "iot_host": "a3j5zqqo5iuph9-ats.iot.us-east-2.amazonaws.com",
        "api_device_list": "453ahng0z4",
        "api_device_info": "xuw7gtx113",
        "api_ota_check": "6at3p6r6ce",
        "api_ota_job": "tvdfyh81d1",  # from APK strings; us-east-2 OTA job gateway
        "api_map": "suk4e76xe5",
        "api_user_account": "6r8m5rxeth",
        "api_kvs": "xuw7gtx113",  # per API.md table; unverified live
        "s3_bucket": None,  # not yet confirmed from capture
    },
    "ap-southeast-2": {
        "client_id": "2ch3nqqr0usf5sadvcrj2hp6ll",  # from APK bundle
        "user_pool_id": "ap-southeast-2_vNriuUNeQ",
        "identity_pool_id": "ap-southeast-2:87d0fe24-16af-4189-b02f-984a7ed14ee0",
        "iot_host": "a3j5zqqo5iuph9-ats.iot.ap-southeast-2.amazonaws.com",
        "api_device_list": "1sfa49lnl8",
        "api_device_info": "7k2iuc99h7",
        "api_ota_check": "v7tlj1gnw7",
        "api_ota_job": None,  # not present in APK strings or capture
        "api_map": "2xipi98nw3",
        "api_user_account": "l2gobpcoqc",
        "api_kvs": None,  # not present in API.md; unknown
        "s3_bucket": None,  # not yet confirmed from capture
    },
    "ap-east-1": {
        "client_id": "46mirppdlu6mrbjd5bkiil0n20",  # from APK bundle
        "user_pool_id": "ap-east-1_23Lf1WZer",
        "identity_pool_id": "ap-east-1:3e9265aa-f564-4083-8e1e-988e6cfdc446",
        "iot_host": "a3j5zqqo5iuph9-ats.iot.ap-east-1.amazonaws.com",
        "api_device_list": "08ydw34dfj",
        "api_device_info": "i1pbnu30si",
        "api_ota_check": "kdueg6qcwl",
        "api_ota_job": None,  # not present in APK strings or capture
        "api_map": "m35t3px95i",
        "api_user_account": "1h2q9awtqd",
        "api_kvs": "t0da44vtxf",  # per API.md table; unverified live
        "s3_bucket": None,  # not yet confirmed from capture
    },
}

# ---------------------------------------------------------------------------
# Work status codes (from pboutput PbRobotInfo.workStatus field)
# ---------------------------------------------------------------------------
WORK_STATUS_NONE = 0  # idle at station
WORK_STATUS_WAITING = 1  # ready, awaiting command
WORK_STATUS_MOWING = 2  # actively cutting
WORK_STATUS_PAUSE = 3  # paused mid-mow
WORK_STATUS_DOCKING = 4  # returning to base
WORK_STATUS_CHARGING = 5  # charging at station
WORK_STATUS_REMOTE_CONTROL = 6  # manual remote control
WORK_STATUS_ERROR = 7  # error state
WORK_STATUS_RESUME = 8  # resuming after pause
WORK_STATUS_ZONE_PARTITION = 9  # zone-specific cutting
WORK_STATUS_PAUSE_DOCKING = 10  # paused while returning
WORK_STATUS_UPDATING = 11  # OTA firmware update in progress
WORK_STATUS_CHARGING_FULL = 12  # fully charged
WORK_STATUS_EMERGENCY_STOP = 13  # emergency stop triggered
WORK_STATUS_ESCAPING = 14  # escaping obstacle
WORK_STATUS_RTT = 15  # factory RTT test
WORK_STATUS_AGING_TEST = 16  # factory aging test
WORK_STATUS_OFFLINE = -1  # virtual — no MQTT shadow

# Groups used for LawnMowerActivity mapping
WORK_STATUS_MOWING_GROUP = frozenset({WORK_STATUS_MOWING, WORK_STATUS_RESUME, WORK_STATUS_ZONE_PARTITION})
WORK_STATUS_RETURNING_GROUP = frozenset({WORK_STATUS_DOCKING, WORK_STATUS_PAUSE_DOCKING, WORK_STATUS_ESCAPING})
WORK_STATUS_DOCKED_GROUP = frozenset(
    {WORK_STATUS_NONE, WORK_STATUS_WAITING, WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL, WORK_STATUS_UPDATING}
)
WORK_STATUS_PAUSED_GROUP = frozenset({WORK_STATUS_PAUSE, WORK_STATUS_REMOTE_CONTROL})
WORK_STATUS_ERROR_GROUP = frozenset({WORK_STATUS_ERROR, WORK_STATUS_EMERGENCY_STOP})

# ---------------------------------------------------------------------------
# MQTT command codes (published to pbinput topic) — sous-ensemble autorisé
# ---------------------------------------------------------------------------
USER_CTRL_RESUME = 4  # resume from pause
USER_CTRL_FORCE_REINIT = 28  # stop in place, reset to waiting
USER_CTRL_EXIT_REMOTE = 32  # exit remote control mode
USER_CTRL_RECHARGE_DOCK = 33  # dock + keep task progress
USER_CTRL_SET_RUN_TIME_CONFIG = 50  # set runtime configuration

ERROR_DESCRIPTIONS: dict[int, str] = {
    0: "No error",
    1: "Wheel drive malfunction",
    2: "Wheel temperature",
    3: "Wheel comm lost",
    4: "Battery temperature",
    5: "Battery charging fault",
    6: "Battery voltage",
    7: "First lift sensor blocked",
    8: "Second lift sensor blocked",
    9: "SoC comm lost",
    10: "Blade comm lost",
    11: "Blade RPM abnormal",
    12: "Localisation: missing calibration",
    13: "Localisation: VIO failed",
    14: "Localisation: EKF failed",
    15: "Localisation init: RTK not fixed",
    16: "Localisation init: timeout",
    17: "Robot at cliff",
    18: "Robot incline too steep",
    19: "Robot wheel slip",
    20: "Robot outside map",
    21: "Robot stuck",
    22: "Segmentation model failed",
    23: "Map missing",
    24: "Map incorrect",
    25: "Map: no dock recorded",
    26: "Map: no channel to dock",
    27: "Map: zero go-zones",
    28: "Map: zone unreachable",
    29: "Charging dock not found",
    30: "Dock error",
    31: "Low battery",
    32: "Camera sensor error",
    33: "IMU sensor error",
    34: "GNSS sensor error",
    35: "Bluetooth init failed",
    36: "Bluetooth broadcast failed",
    37: "MCU comm lost",
    38: "Wi-Fi SSID not found",
    39: "Wi-Fi connect failed",
    40: "OTA: battery too low",
    41: "OTA: robot not in wait state",
    42: "OTA: download failed",
    43: "OTA: upgrade failed",
    44: "Bumper stuck",
    45: "Blade stuck",
    46: "Localisation comm lost",
    47: "Segmentation comm lost",
    48: "Path-planning: back timeout",
    49: "Path-planning: channel broken",
    50: "Path-planning: channel error",
    51: "Path-planning: dock signal lost",
    52: "Path-planning: dock path not found",
    53: "Path-planning: execution error",
    54: "Map: base station moved",
    55: "Charging station not found",
    56: "Robot outside operating domain",
    57: "No pose output",
    58: "Base station invalid",
    59: "Front ultrasonic sensor error",
    60: "Rear ultrasonic sensor error",
    61: "Localisation: RTK base error",
    62: "Map does not match",
    63: "Charging station invalid",
    64: "Robot inside no-go zone",
    65: "Robot at no-go wall",
    66: "Robot stuck (trapped)",
    67: "Path-planning solver failed",
    68: "Path-planning search failed",
    69: "Boundary-driving failed",
    70: "Channel offset too large",
    71: "Action timeout",
    72: "Command/wheel-speed incompatible",
    73: "Costmap error",
    74: "Channel bumper triggered",
    75: "Channel obstacle",
    76: "Edge-follow: obstacle",
    77: "Edge unpassable",
    78: "Blade over-current",
    79: "Localisation: yaw abnormal",
    80: "Dock timeout",
    81: "Localisation: low edge score",
    82: "Localisation: low boundary-driving score",
    83: "Channel slip",
    84: "Slope slip",
    85: "Init failed (repeat count)",
    86: "Resume: out of map",
    87: "Start: out of map",
    88: "Path-planning: out of where",
    89: "Thick blade stuck",
    90: "Max error code (sentinel)",
}

WARNING_DESCRIPTIONS: dict[int, str] = {
    0: "No warning",
    1: "Wheel over-current",
    2: "Wheel over-voltage",
    3: "Wheel under-voltage",
    4: "Battery current abnormal",
    5: "First lift timeout",
    6: "Second lift timeout",
    7: "Front ultrasonic lost",
    8: "Rear ultrasonic lost",
    9: "SoC comm abnormal",
    10: "MCU thread schedule abnormal",
    11: "Blade over-temperature",
    12: "Blade over-current",
    13: "Blade comm abnormal",
    14: "Localisation ignored command",
    15: "Localisation init failed",
    16: "Localisation: invalid sensor data",
    17: "Localisation: camera blocked",
    18: "Localisation: camera data unsynced",
    19: "Localisation: RTK signal bad",
    20: "Localisation: weak texture",
    21: "Localisation: VIO abnormal",
    22: "Localisation: EKF abnormal",
    23: "Segmentation: low light",
    24: "Robot escaping obstacle",
    25: "MCU comm abnormal",
    26: "Camera sensor temperature abnormal",
    27: "Camera sensor abnormal",
    28: "IMU sensor abnormal",
    29: "GNSS sensor abnormal",
    30: "Robot slipping",
    31: "Localisation comm abnormal",
    32: "Blade stuck",
    33: "Segmentation comm abnormal",
    34: "Path-planning: large lateral error",
    35: "Localisation: low light",
    36: "Path-planning: execution warning",
    37: "Zone not connected",
    38: "Zone: end far from start",
    39: "Zone area too small",
    40: "No-go zone not inside go-zone",
    41: "Channel start not inside zone",
    42: "Only one docking channel allowed",
    43: "Zone has figure-8 path",
    44: "Modified zone too far from edge",
    45: "Modified zone start too close to end",
    46: "Modified zone changed channel point",
    47: "Modify-zone internal failure",
    48: "Cannot find objects",
    49: "Add docking channel",
    50: "Docking channel unnecessary",
    51: "Localisation: no RTK base",
    52: "RTK bind failed",
    53: "Base station invalid",
    54: "Localisation: yaw abnormal",
    55: "No-go zone illegal",
    56: "Schedule modified",
    58: "Map operate failed",
    59: "Divide: narrow part",
    60: "Divide: area too small",
    61: "Charging station invalid",
    62: "Zone not overlapped",
    63: "Max warning code (sentinel)",
}

# Velocity ranges confirmed from ADB joystick swipe captures
BLE_DRIVE_LINEAR_MAX = 0.5  # m/s (forward: +, backward: -)
# Confirmed from capture (see encode_ble_drive): +0.6 = full left turn (CCW), -0.6 = right.
BLE_DRIVE_ANGULAR_MAX = 0.6  # rad/s (left: +, right: -)
# The app refreshes the drive characteristic ~10 Hz while the joystick is held.
BLE_DRIVE_REFRESH_HZ = 10
# Safety cap: a single ble_drive service call may not move the robot longer than this.
BLE_DRIVE_MAX_DURATION_S = 5.0

# rtkStatus values surfaced as the LymowRtkSensor state (PbOutput.rtkStatus,
# field 4 of the GPS/RTK sub-message). Matches the LymowRtkSensor label map.
RTK_SIGNAL_QUALITY = {
    0: "NO_SIGNAL",
    1: "SINGLE_POINT",
    2: "FLOAT_FIXED",
    3: "FIXED",
}
