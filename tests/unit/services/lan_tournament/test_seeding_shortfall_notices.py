import pytest

from byceps.services.lan_tournament import (
    tournament_seeding_domain_service as domain,
    tournament_seeding_service,
)


NOBODY_ELIMINATED = tournament_seeding_service.NOTICE_NOBODY_ELIMINATED


# fmt: off
@pytest.mark.parametrize(
    ('n', 'param', 'groups', 'per_group', 'expected'),
    [
        (8, 2, 2, 4, [(NOBODY_ELIMINATED, {'size': 4, 'per_group': 4})]),
        (8, 2, 2, 5, [
            (tournament_seeding_service.NOTICE_FEWER_QUALIFIERS,
             {'qualifiers': 8, 'configured': 10}),
            (NOBODY_ELIMINATED, {'size': 4, 'per_group': 5}),
        ]),
        (8, 2, 2, 3, []),
        (16, 4, 4, 2, []),
        (6, 4, 4, 2, [
            (tournament_seeding_service.NOTICE_FEWER_GROUPS,
             {'groups': 3, 'configured': 4}),
            (tournament_seeding_service.NOTICE_FEWER_QUALIFIERS,
             {'qualifiers': 6, 'configured': 8}),
            (NOBODY_ELIMINATED, {'size': 2, 'per_group': 2}),
        ]),
    ],
)
# fmt: on
def test_group_stage_notices_say_when_nobody_is_eliminated(
    n, param, groups, per_group, expected
):
    shortfall = domain.group_shortfall(
        n, param, groups, per_group, double_elimination=False
    )

    notices = tournament_seeding_service.shortfall_notices(shortfall)

    assert notices == expected


def test_qualifier_shortfall_never_says_nobody_is_eliminated():
    shortfall = domain.qualifier_shortfall(3, 8, double_elimination=False)

    notices = tournament_seeding_service.shortfall_notices(shortfall)

    assert [msgid for msgid, _ in notices] == [
        tournament_seeding_service.NOTICE_FEWER_QUALIFIERS
    ]
