import json
from types import SimpleNamespace

import pytest

from app.models.proxy import HysteriaSettings, ProxySettings
from app.subscription.v2ray import V2rayJsonConfig, V2rayShareLink
from app.xray.config import XRayConfig


def make_config(inbound):
    return {
        "inbounds": [inbound],
        "outbounds": [
            {
                "tag": "direct",
                "protocol": "freedom",
            }
        ],
    }


def make_hysteria_inbound(**overrides):
    inbound = {
        "tag": "hy2-in-8443",
        "listen": "0.0.0.0",
        "port": 8443,
        "protocol": "hysteria",
        "settings": {
            "version": 2,
            "users": [],
        },
        "streamSettings": {
            "network": "hysteria",
            "security": "tls",
            "tlsSettings": {
                "serverName": "example.com",
                "alpn": ["h3"],
            },
            "hysteriaSettings": {
                "version": 2,
            },
        },
    }
    inbound.update(overrides)
    return inbound


def test_proxy_settings_from_dict_accepts_hysteria():
    settings = ProxySettings.from_dict("hysteria", {})

    assert isinstance(settings, HysteriaSettings)
    assert settings.auth


def test_proxy_settings_from_dict_preserves_custom_hysteria_auth():
    settings = ProxySettings.from_dict("hysteria", {"auth": "custom-auth"})

    assert settings.auth == "custom-auth"


def test_proxy_settings_unknown_protocol_is_rejected():
    with pytest.raises(ValueError):
        ProxySettings.from_dict("unknown", {})


def test_hysteria_settings_revoke_regenerates_auth():
    settings = HysteriaSettings(auth="old-auth")

    settings.revoke()

    assert settings.auth
    assert settings.auth != "old-auth"


def test_hysteria2_inbound_is_resolved_by_protocol_and_tag():
    config = XRayConfig(make_config(make_hysteria_inbound()))

    inbound = config.inbounds_by_tag["hy2-in-8443"]
    assert inbound["protocol"] == "hysteria"
    assert inbound["network"] == "hysteria"
    assert inbound["tls"] == "tls"
    assert inbound["sni"] == ["example.com"]
    assert inbound["alpn"] == "h3"
    assert inbound["hysteria_version"] == 2
    assert config.inbounds_by_protocol["hysteria"] == [inbound]


def test_hysteria2_inbound_rejects_settings_version_other_than_2():
    inbound = make_hysteria_inbound(settings={"version": 1, "users": []})

    with pytest.raises(ValueError, match="settings.version = 2"):
        XRayConfig(make_config(inbound))


def test_hysteria2_inbound_rejects_reality_security():
    inbound = make_hysteria_inbound()
    inbound["streamSettings"]["security"] = "reality"
    inbound["streamSettings"]["realitySettings"] = {}

    with pytest.raises(ValueError, match="Reality is not supported"):
        XRayConfig(make_config(inbound))


def test_include_db_users_adds_hysteria_users_not_clients(monkeypatch):
    config = XRayConfig(make_config(make_hysteria_inbound()))
    row = SimpleNamespace(
        id=7,
        username="hy2user",
        type="hysteria",
        settings={"auth": "user-auth"},
        excluded_inbound_tags=None,
    )

    class FakeQuery:
        def join(self, *args, **kwargs):
            return self

        def outerjoin(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def group_by(self, *args, **kwargs):
            return self

        def all(self):
            return [row]

    class FakeDB:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        def query(self, *args, **kwargs):
            return FakeQuery()

    class FakeGetDB:
        def __enter__(self):
            return FakeDB()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("app.xray.config.GetDB", FakeGetDB)

    generated = config.include_db_users()
    settings = generated.get_inbound("hy2-in-8443")["settings"]

    assert settings["version"] == 2
    assert settings["users"] == [
        {
            "auth": "user-auth",
            "email": "7.hy2user",
            "level": 0,
        }
    ]
    assert "clients" not in settings


def test_hysteria2_share_link_encodes_auth_and_tls_params():
    link = V2rayShareLink.hysteria2(
        remark="hy2 user",
        address="example.com",
        port=8443,
        auth="pa ss/@",
        sni="sni.example.com",
        alpn="h3",
        ais=True,
    )

    assert link.startswith("hysteria2://pa%20ss%2F%40@example.com:8443?")
    assert "security=tls" in link
    assert "sni=sni.example.com" in link
    assert "alpn=h3" in link
    assert "insecure=1" in link
    assert link.endswith("#hy2%20user")


def test_hysteria2_v2ray_json_output_uses_xray_hysteria_protocol():
    conf = V2rayJsonConfig()
    conf.add(
        remark="hy2",
        address="example.com",
        inbound={
            "tag": "hy2-in-8443",
            "protocol": "hysteria",
            "network": "hysteria",
            "port": 8443,
            "tls": "tls",
            "sni": "sni.example.com",
            "host": "",
            "path": "",
            "header_type": "",
            "fragment_setting": "",
            "noise_setting": "",
            "alpn": "h3",
            "ais": False,
        },
        settings={"auth": "user-auth"},
    )

    rendered = json.loads(conf.render())
    outbound = rendered[0]["outbounds"][0]
    assert outbound["protocol"] == "hysteria"
    assert outbound["settings"] == {
        "version": 2,
        "address": "example.com",
        "port": 8443,
    }
    assert outbound["streamSettings"]["network"] == "hysteria"
    assert outbound["streamSettings"]["hysteriaSettings"] == {
        "version": 2,
        "auth": "user-auth",
    }
    assert outbound["streamSettings"]["security"] == "tls"
    assert "realitySettings" not in outbound["streamSettings"]
