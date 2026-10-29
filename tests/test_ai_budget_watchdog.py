from __future__ import annotations


def test_ai_budget_alert_is_quiet_without_online_provider_or_reservations():
    from tgfarm.pipeline import _ai_budget_alert

    assert _ai_budget_alert(10, 30, online_provider_enabled=False) is None
    assert _ai_budget_alert(0, 30, online_provider_enabled=True) is None


def test_ai_budget_alert_warns_at_eighty_percent_without_overstating_money():
    from tgfarm.pipeline import _ai_budget_alert

    alert = _ai_budget_alert(24, 30, online_provider_enabled=True)

    assert alert is not None
    assert alert["level"] == "warning"
    assert alert["code"] == "ai_daily_budget_near_limit"
    assert "24 из 30" in alert["message"]
    assert "деньг" not in alert["message"].lower()


def test_ai_budget_alert_warns_when_limit_is_exhausted_and_offline_fallback_remains():
    from tgfarm.pipeline import _ai_budget_alert

    alert = _ai_budget_alert(30, 30, online_provider_enabled=True)

    assert alert is not None
    assert alert["level"] == "warning"
    assert alert["code"] == "ai_daily_budget_exhausted"
    assert "30/30" in alert["message"]
    assert "офлайн-сценарии" in alert["message"]


def test_zero_budget_with_real_reservations_is_reported_as_exhausted():
    from tgfarm.pipeline import _ai_budget_alert

    alert = _ai_budget_alert(1, 0, online_provider_enabled=True)

    assert alert is not None
    assert alert["code"] == "ai_daily_budget_exhausted"
    assert "1/0" in alert["message"]


def test_ai_budget_alert_does_not_warn_before_near_limit():
    from tgfarm.pipeline import _ai_budget_alert

    assert _ai_budget_alert(23, 30, online_provider_enabled=True) is None
