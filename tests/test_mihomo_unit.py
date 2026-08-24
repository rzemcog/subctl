from pathlib import Path


UNIT = Path(__file__).parents[1] / "deploy" / "mihomo.service"


def test_mihomo_unit_is_a_hardened_unprivileged_canary():
    text = UNIT.read_text(encoding="utf-8")

    required = {
        "User=mihomo",
        "Group=mihomo",
        "UMask=0077",
        "StateDirectory=mihomo",
        "StateDirectoryMode=0700",
        "ExecStartPre=/usr/local/bin/mihomo -t -d /var/lib/mihomo -f /etc/mihomo/config.yaml",
        "ExecStart=/usr/local/bin/mihomo -d /var/lib/mihomo -f /etc/mihomo/config.yaml",
        "Restart=on-failure",
        "NoNewPrivileges=true",
        "PrivateDevices=true",
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/mihomo",
        "ReadOnlyPaths=/var/lib/mihomo/ui",
        "RestrictNamespaces=true",
        "WantedBy=multi-user.target",
    }
    assert required <= set(text.splitlines())
    assert text.index("ReadWritePaths=/var/lib/mihomo") < text.index(
        "ReadOnlyPaths=/var/lib/mihomo/ui"
    )


def test_mihomo_unit_does_not_request_tun_or_network_privileges():
    text = UNIT.read_text(encoding="utf-8")

    assert "CAP_NET_ADMIN" not in text
    assert "AmbientCapabilities" not in text
    assert "/dev/net/tun" not in text
    assert "IPAddressAllow" not in text
