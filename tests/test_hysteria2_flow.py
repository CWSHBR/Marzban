import importlib
import json
from types import SimpleNamespace

import pytest
import yaml

from app.models.proxy import HysteriaSettings, ProxySettings, ProxyTypes
from app.subscription import (
    ClashConfiguration,
    ClashMetaConfiguration,
    OutlineConfiguration,
    SingBoxConfiguration,
)
from app.subscription import share as share_module
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
                "udpIdleTimeout": 60,
            },
            "finalmask": {
                "udp": [
                    {
                        "type": "salamander",
                        "settings": {
                            "password": "mask-pass",
                        },
                    }
                ],
                "quicParams": {
                    "congestion": "bbr",
                    "bbrProfile": "standard",
                    "maxIdleTimeout": 30,
                    "keepAlivePeriod": 10,
                    "disablePathMTUDiscovery": False,
                },
            },
        },
    }
    inbound.update(overrides)
    return inbound


def make_vless_inbound(**overrides):
    inbound = {
        "tag": "vless-tcp",
        "listen": "0.0.0.0",
        "port": 443,
        "protocol": "vless",
        "settings": {
            "clients": [],
            "decryption": "none",
        },
        "streamSettings": {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "vless.example.com",
            },
        },
    }
    inbound.update(overrides)
    return inbound


def make_resolved_hysteria_inbound(**overrides):
    inbound = {
        "tag": "hy2-in-8443",
        "protocol": "hysteria",
        "network": "hysteria",
        "port": 8443,
        "tls": "tls",
        "sni": ["sni.example.com"],
        "host": [],
        "path": "",
        "header_type": "",
        "fragment_setting": "",
        "noise_setting": "",
        "alpn": "h3",
        "ais": False,
        "hysteria_settings": {
            "version": 2,
            "udpIdleTimeout": 60,
        },
        "finalmask": {},
    }
    inbound.update(overrides)
    return inbound


def make_host_override(**overrides):
    host = {
        "remark": "hy2 {USERNAME}",
        "address": ["example.com"],
        "port": None,
        "path": None,
        "sni": [],
        "host": [],
        "alpn": "",
        "fingerprint": "",
        "tls": None,
        "allowinsecure": False,
        "mux_enable": False,
        "fragment_setting": "",
        "noise_setting": "",
        "random_user_agent": False,
        "use_sni_as_host": False,
    }
    host.update(overrides)
    return host


def patch_subscription_xray(monkeypatch, inbound, host):
    xray_stub = SimpleNamespace(
        config=SimpleNamespace(inbounds_by_tag={inbound["tag"]: inbound}),
        hosts={inbound["tag"]: [host]},
    )
    monkeypatch.setattr(share_module, "xray", xray_stub)


def make_subscription_inputs():
    return {
        "proxies": {
            ProxyTypes.Hysteria: HysteriaSettings(auth="user-auth"),
        },
        "inbounds": {
            ProxyTypes.Hysteria: ["hy2-in-8443"],
        },
        "extra_data": {
            "username": "hy2user",
            "used_traffic": 0,
            "status": "active",
        },
        "reverse": False,
    }


def test_proxy_settings_from_dict_accepts_hysteria():
    settings = ProxySettings.from_dict("hysteria", {})

    assert isinstance(settings, HysteriaSettings)
    assert settings.auth


def test_proxy_settings_from_dict_preserves_custom_hysteria_auth():
    settings = ProxySettings.from_dict("hysteria", {"auth": "custom-auth"})

    assert settings.auth == "custom-auth"


def test_vless_settings_default_to_xtls_rprx_vision():
    settings = ProxySettings.from_dict("vless", {})

    assert settings.flow == "xtls-rprx-vision"


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
    assert inbound["hysteria_settings"]["udpIdleTimeout"] == 60
    assert inbound["finalmask"]["udp"][0]["settings"]["password"] == "mask-pass"
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


def test_hysteria2_inbound_rejects_none_security():
    inbound = make_hysteria_inbound()
    inbound["streamSettings"]["security"] = "none"

    with pytest.raises(ValueError, match='streamSettings.security = "tls"'):
        XRayConfig(make_config(inbound))


def test_hysteria2_inbound_rejects_missing_security():
    inbound = make_hysteria_inbound()
    del inbound["streamSettings"]["security"]

    with pytest.raises(ValueError, match='streamSettings.security = "tls"'):
        XRayConfig(make_config(inbound))


def test_vless_inbound_still_resolves_with_tls():
    config = XRayConfig(make_config(make_vless_inbound()))

    inbound = config.inbounds_by_tag["vless-tcp"]
    assert inbound["protocol"] == "vless"
    assert inbound["network"] == "tcp"
    assert inbound["tls"] == "tls"
    assert inbound["sni"] == ["vless.example.com"]
    assert config.inbounds_by_protocol["vless"] == [inbound]


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

    config_module = importlib.import_module("app.xray.config")
    monkeypatch.setattr(config_module, "GetDB", FakeGetDB)

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
        fp="chrome",
        finalmask={
            "udp": [
                {
                    "type": "salamander",
                    "settings": {
                        "password": "mask-pass",
                    },
                }
            ],
            "quicParams": {
                "congestion": "bbr",
                "disablePathMTUDiscovery": False,
            },
        },
        hysteria_settings={"udpIdleTimeout": 60},
    )

    assert link.startswith("hysteria2://pa%20ss%2F%40@example.com:8443?")
    assert "security=tls" in link
    assert "sni=sni.example.com" in link
    assert "alpn=h3" in link
    assert "insecure=1" in link
    assert "fp=chrome" in link
    assert "udpIdleTimeout=60" in link
    assert "obfs=salamander" in link
    assert "obfs-password=mask-pass" in link
    assert "congestion=bbr" in link
    assert "disablePathMTUDiscovery=false" in link
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
            "hysteria_settings": {
                "version": 2,
                "udpIdleTimeout": 60,
            },
            "finalmask": {
                "udp": [
                    {
                        "type": "salamander",
                        "settings": {
                            "password": "mask-pass",
                        },
                    }
                ],
                "quicParams": {
                    "congestion": "bbr",
                    "bbrProfile": "standard",
                    "maxIdleTimeout": 30,
                    "keepAlivePeriod": 10,
                    "disablePathMTUDiscovery": False,
                },
            },
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
        "udpIdleTimeout": 60,
    }
    assert outbound["streamSettings"]["finalmask"] == {
        "udp": [
            {
                "type": "salamander",
                "settings": {
                    "password": "mask-pass",
                },
            }
        ],
        "quicParams": {
            "congestion": "bbr",
            "bbrProfile": "standard",
            "maxIdleTimeout": 30,
            "keepAlivePeriod": 10,
            "disablePathMTUDiscovery": False,
        },
    }
    assert outbound["streamSettings"]["security"] == "tls"
    assert "realitySettings" not in outbound["streamSettings"]


def test_hysteria2_host_override_without_alpn_keeps_inbound_alpn(monkeypatch):
    inbound = make_resolved_hysteria_inbound()
    patch_subscription_xray(monkeypatch, inbound, make_host_override())

    link = share_module.generate_v2ray_links(**make_subscription_inputs())[0]

    assert "alpn=h3" in link


def test_hysteria2_host_override_with_explicit_alpn_replaces_inbound_alpn(monkeypatch):
    inbound = make_resolved_hysteria_inbound()
    patch_subscription_xray(monkeypatch, inbound, make_host_override(alpn="h2"))

    link = share_module.generate_v2ray_links(**make_subscription_inputs())[0]

    assert "alpn=h2" in link
    assert "alpn=h3" not in link


def test_hysteria2_host_override_alpn_is_preserved_in_client_outputs(monkeypatch):
    inbound = make_resolved_hysteria_inbound()
    patch_subscription_xray(monkeypatch, inbound, make_host_override())
    inputs = make_subscription_inputs()

    v2ray_json = json.loads(share_module.generate_v2ray_json_subscription(**inputs))
    v2ray_outbound = v2ray_json[0]["outbounds"][0]
    assert v2ray_outbound["streamSettings"]["tlsSettings"]["alpn"] == ["h3"]

    singbox_json = json.loads(share_module.generate_singbox_subscription(**inputs))
    hysteria_outbound = next(
        outbound for outbound in singbox_json["outbounds"]
        if outbound["type"] == "hysteria2"
    )
    assert hysteria_outbound["tls"]["alpn"] == ["h3"]

    clash_meta = yaml.safe_load(
        share_module.generate_clash_subscription(**inputs, is_meta=True)
    )
    hysteria_proxy = next(
        proxy for proxy in clash_meta["proxies"]
        if proxy["type"] == "hysteria2"
    )
    assert hysteria_proxy["alpn"] == ["h3"]


def test_hysteria2_singbox_output_uses_hysteria2_type():
    conf = SingBoxConfiguration()
    conf.add(
        remark="hy2",
        address="example.com",
        inbound=make_resolved_hysteria_inbound(sni="sni.example.com"),
        settings={"auth": "user-auth"},
    )

    rendered = json.loads(conf.render())
    outbound = next(
        outbound for outbound in rendered["outbounds"]
        if outbound["type"] == "hysteria2"
    )
    assert outbound["server"] == "example.com"
    assert outbound["server_port"] == 8443
    assert outbound["password"] == "user-auth"
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "sni.example.com"
    assert outbound["tls"]["alpn"] == ["h3"]
    assert "multiplex" not in outbound


def test_hysteria2_clash_meta_output_uses_hysteria2_type():
    conf = ClashMetaConfiguration()
    conf.add(
        remark="hy2",
        address="example.com",
        inbound=make_resolved_hysteria_inbound(sni="sni.example.com"),
        settings={"auth": "user-auth"},
    )

    rendered = yaml.safe_load(conf.render())
    proxy = rendered["proxies"][0]
    assert proxy["name"] == "hy2"
    assert proxy["type"] == "hysteria2"
    assert proxy["server"] == "example.com"
    assert proxy["port"] == 8443
    assert proxy["password"] == "user-auth"
    assert proxy["sni"] == "sni.example.com"
    assert proxy["alpn"] == ["h3"]


def test_hysteria2_is_not_added_to_classic_clash_or_outline():
    inbound = make_resolved_hysteria_inbound(sni="sni.example.com")

    clash = ClashConfiguration()
    clash.add(
        remark="hy2",
        address="example.com",
        inbound=inbound,
        settings={"auth": "user-auth"},
    )
    assert yaml.safe_load(clash.render())["proxies"] == []

    outline = OutlineConfiguration()
    outline.add(
        remark="hy2",
        address="example.com",
        inbound=inbound,
        settings={"auth": "user-auth"},
    )
    assert json.loads(outline.render()) == {}


@pytest.mark.parametrize(
    ("protocol", "settings", "expected_prefix"),
    [
        ("vmess", {"id": "35e4e39c-7d5c-4f4b-8b71-558e4f37ff53"}, "vmess://"),
        ("vless", {"id": "35e4e39c-7d5c-4f4b-8b71-558e4f37ff53"}, "vless://"),
        ("trojan", {"password": "secret"}, "trojan://"),
        (
            "shadowsocks",
            {"password": "secret", "method": "chacha20-ietf-poly1305"},
            "ss://",
        ),
    ],
)
def test_existing_protocol_share_links_still_render(protocol, settings, expected_prefix):
    conf = V2rayShareLink()
    conf.add(
        remark=f"{protocol} user",
        address="example.com",
        inbound={
            "tag": f"{protocol}-in",
            "protocol": protocol,
            "network": "tcp",
            "port": 443,
            "tls": "tls",
            "sni": "example.com",
            "host": "",
            "path": "",
            "header_type": "",
            "fragment_setting": "",
            "alpn": "",
            "ais": False,
        },
        settings=settings,
    )

    links = conf.render()
    assert len(links) == 1
    assert links[0].startswith(expected_prefix)
