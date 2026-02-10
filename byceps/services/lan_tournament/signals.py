from blinker import Namespace


lan_tournament_signals = Namespace()


# fmt: off
tournament_created        = lan_tournament_signals.signal('tournament-created')
tournament_updated        = lan_tournament_signals.signal('tournament-updated')
tournament_deleted        = lan_tournament_signals.signal('tournament-deleted')
tournament_status_changed = lan_tournament_signals.signal('tournament-status-changed')

participant_joined = lan_tournament_signals.signal('participant-joined')
participant_left   = lan_tournament_signals.signal('participant-left')

team_created       = lan_tournament_signals.signal('team-created')
team_deleted       = lan_tournament_signals.signal('team-deleted')
team_member_joined = lan_tournament_signals.signal('team-member-joined')
team_member_left   = lan_tournament_signals.signal('team-member-left')

match_created = lan_tournament_signals.signal('match-created')
match_deleted = lan_tournament_signals.signal('match-deleted')
# fmt: on
