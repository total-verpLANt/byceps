import pytest


@pytest.mark.parametrize('app_fixture', ['site_app', 'admin_app'])
def test_no_route_retries_match_invitations(app_fixture, request):
    rules = list(request.getfixturevalue(app_fixture).url_map.iter_rules())

    # A pin that finds no lan_tournament route at all would prove nothing.
    assert any(rule.rule.startswith('/lan-tournaments/') for rule in rules)
    assert [
        rule.rule
        for rule in rules
        if rule.rule.endswith('retry_match_invitations')
    ] == []
    assert [
        rule.endpoint
        for rule in rules
        if 'retry_match_invitations' in rule.endpoint
    ] == []
