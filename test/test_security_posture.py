"""
F7: security posture findings + auth-bypass gating + localhost compose bindings.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import SecuritySettings, security_posture_findings  # noqa: E402


def test_defaults_are_flagged():
    findings = security_posture_findings(SecuritySettings())
    assert any("admin/admin" in finding for finding in findings)
    assert any("CONFIG_PASSWORD" in finding for finding in findings)


def test_debug_mode_is_flagged():
    settings = SecuritySettings(username="u", password="p" * 16, config_password="x" * 16, debug_mode=True)
    findings = security_posture_findings(settings)
    assert any("DEBUG_MODE" in finding for finding in findings)


def test_strong_posture_has_no_findings():
    settings = SecuritySettings(username="rotated-user", password="s3cure-passw0rd!",
                                config_password="a-strong-config-password", debug_mode=False)
    assert security_posture_findings(settings) == []


def test_short_config_password_is_flagged():
    settings = SecuritySettings(username="u", password="p" * 16, config_password="tooshort")
    findings = security_posture_findings(settings)
    assert any("CONFIG_PASSWORD" in finding for finding in findings)


def test_main_py_debug_bypass_requires_second_optin_and_posture_gate():
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'os.environ.get("HB_ALLOW_DEBUG_AUTH_BYPASS") == "1"' in source
    assert 'os.environ.get("HB_ENFORCE_STRONG_SECRETS") == "1"' in source
    assert "security_posture_findings(settings.security)" in source


def test_base_compose_binds_localhost_only():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    offenders = []
    for service_name, service in compose["services"].items():
        for port in service.get("ports", []) or []:
            if isinstance(port, str) and not port.startswith("127.0.0.1:"):
                offenders.append(f"{service_name}:{port}")
    assert offenders == [], f"non-localhost port bindings remain: {offenders}"
