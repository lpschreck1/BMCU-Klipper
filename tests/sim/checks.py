"""The shared end-of-scenario definition of "recoverable" (plan 2, "Every scenario ends with
the same checks").

After any fault, restart or disconnect, plus whatever the user does:

1. the print **completes**, or it pauses with a message that names what to do; "stuck" means
   no sequence of the given user actions finishes it;
2. every source BMCU reports as loaded is physically in the head, and none that it reports
   empty is physically in the head (``UNCERTAIN`` is allowed: BMCU knows it does not know);
3. no channel or endpoint lock is left once the print ends;
4. no head printed dry (extruder asked for plastic while the world shows the nozzle empty).

``recover(sim, actions)`` drives the user; ``verify(sim)`` runs checks 2-4;
``assert_recoverable(test, sim, actions)`` does both and fails the test with the reasons.
"""
import re

from bmcu_core import protocol as P

MAX_RESUMES = 3
DRY_TOLERANCE_MM = 1.0
NAMES_WHAT_TO_DO = re.compile(
    r'(head \d|extruder\[?\d|\bT\d\b|channel \d|u1_head\d|bmcu\d?|filament|resume|clear)',
    re.I)


class Outcome(object):
    def __init__(self):
        self.kind = 'stuck'          # 'no_effect', 'resumed' or 'stuck'
        self.state = ''
        self.first_message = ''      # the message of the first pause
        self.messages = []
        self.resumes = 0
        self.problems = []           # why the scenario is not recoverable

    @property
    def ok(self):
        return not self.problems

    def __repr__(self):
        return 'Outcome(%s, resumes=%d, problems=%r, first=%r)' % (
            self.kind, self.resumes, self.problems, self.first_message[:80])


def _apply_actions(sim, actions):
    for action in actions:
        if action == 'clear_fault':
            sim.user.clear_fault()
        elif action == 'replug':
            sim.user.clear_fault()            # BmcuDisconnect.clear reconnects the cable
        elif callable(action):
            action(sim)
        else:
            raise ValueError('unknown user action %r' % (action,))
    sim.settle(8.0)                           # the human takes a few seconds


def recover(sim, actions=('clear_fault',), start=True, max_resumes=MAX_RESUMES,
            settle=True):
    """Run (or continue) the print and play the user until it completes or gives up."""
    outcome = Outcome()
    if start:
        sim.start()
    state = sim.run_until_stopped()
    outcome.state = state
    if state == 'complete':
        outcome.kind = 'no_effect'
        return outcome
    for attempt in range(max_resumes):
        message = sim.print_stats.message or ''
        outcome.messages.append(message)
        if not outcome.first_message:
            outcome.first_message = message
        if state == 'paused' and not NAMES_WHAT_TO_DO.search(message):
            outcome.problems.append('pause message does not say what to do: %r' % message)
        _apply_actions(sim, actions)
        try:
            state = sim.user.resume()
        except Exception as exc:              # Snapmaker refused the Resume itself
            state = 'refused'
            outcome.messages.append('resume refused: %s' % exc)
        outcome.resumes += 1
        outcome.state = state
        if state == 'complete':
            outcome.kind = 'resumed'
            return outcome
    outcome.problems.append('stuck: %s after %d resumes (%s)' % (
        outcome.state, outcome.resumes, (outcome.messages or [''])[-1][:160]))
    return outcome


def verify(sim, settle=20.0):
    """Checks 2-4 on a finished print. Returns a list of problems."""
    problems = []
    sim.settle(settle)                        # let background jobs finish
    # 2. BMCU's view against the world
    status = sim.manager.get_status()
    for device in status['devices']:
        for channel in device['channels']:
            source = sim.world.sources.get('bmcu%d' % channel['channel'])
            if source is None:
                continue
            route = channel['route_state']
            if route == P.ROUTE_LOADED and not source.past(source.gears_p):
                problems.append('BMCU says channel %d is loaded, but its tip is at %r' %
                                (channel['channel'], source.tip))
            if route == P.ROUTE_EMPTY and source.past(source.sensor_p):
                problems.append('BMCU says channel %d is empty, but its tip is in the head '
                                '(%r)' % (channel['channel'], source.tip))
    problems.extend(sim.world.consistent())
    # 3. locks (the web page's "locked"); no public status carries them, so read the sets
    manager = sim.manager
    for name in ('endpoint_locks', 'path_locks', '_u1_background_channel_locks',
                 'active_operations'):
        value = getattr(manager, name, None)
        if value:
            problems.append('lock left behind: %s=%r' % (name, value))
    # 4. nothing printed dry
    for index, dry in sim.dry_mm().items():
        if dry > DRY_TOLERANCE_MM:
            problems.append('head %d printed %.1f mm of plastic it did not have' %
                            (index, dry))
    return problems


def assert_recoverable(test, sim, actions=('clear_fault',), start=True, **kwargs):
    outcome = recover(sim, actions, start=start, **kwargs)
    if outcome.state == 'complete':
        outcome.problems.extend(verify(sim))
    if outcome.problems:
        test.fail('not recoverable: %s' % '; '.join(outcome.problems))
    return outcome
