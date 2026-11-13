"""Frozen deployed 7db2872 refresh, independent oracle for allocation changes.

Keep this reference unchanged when optimizing the production implementation.
"""

from pm_nautilus.rules import static_reason


def refresh_eligibility(self):
    now = self.now
    previous = self.events
    settled = set(self.business["settled"])
    eligible = {
        tid
        for tid, t in self._eligibility_candidates()
        if static_reason(t, self.preferences, now, self.official_tags) is None
        and t.condition_id not in settled
        and not self.market_is_unavailable(tid)
    }
    self.events = {}
    for tid in eligible:
        self.events.setdefault(self.cycle_id(self.tokens[tid]), set()).add(tid)
    self.monitored = set(eligible)
    for cycle in self.business["cycles"].values():
        self.monitored.add(cycle["token_id"])
    for eid in previous.keys() | self.events.keys():
        if previous.get(eid) != self.events.get(eid):
            self.dirty.add(eid)
    # Periodic control ticks still detect cash releases, completed/rejected
    # orders and eligibility time boundaries. Unchanged Events need no
    # repeated order preview; lifecycle ties retain their timed re-ranking.
    control = (
        self.preferences.model_dump_json(),
        self.status,
        self.storage_can_open,
        tuple(sorted(self.market_availability)),
        tuple(sorted(self.market_recovering)),
        self.cash(),
        self.held_cash(),
        tuple(self.business["banned"]),
        tuple(self.business["settled"]),
    )
    if control != self._refresh_control:
        self.dirty.update(self.events)
        self.dirty.update(self.business["cycles"])
        self._refresh_control = control
    self._cycle_scope_keys.clear()
    for key, cycle in self.business["cycles"].items():
        self._remember_cycle_scope(self.tokens[cycle["token_id"]], key)
    active = {}
    for oid, intent in self.store.intents(active_only=True).items():
        key = intent["event_id"]
        active.setdefault(key, []).append(oid)
        self._remember_cycle_scope(self.tokens[intent["token_id"]], key)
    states = {
        eid: (
            tuple(sorted(active.get(eid, []))),
            (
                cycle["quantity"],
                cycle["spent"],
                cycle["budget"],
                cycle["sold"],
                tuple(sorted(cycle["stop"].items())),
            )
            if (cycle := self.business["cycles"].get(eid))
            else None,
        )
        for eid in self.events.keys() | self.business["cycles"].keys() | active.keys()
    }
    for eid in states.keys() | self._refresh_events.keys():
        if states.get(eid) != self._refresh_events.get(eid):
            self.dirty.add(eid)
    self._refresh_events = states
    # An order can start and finish between two control samples. Remember
    # its committed terminal transition even when both samples see no active
    # order. Defer the retry to this tick to avoid a zero-fill dispatch loop.
    self.dirty.update(self._terminal_events)
    self._terminal_events.clear()
    self.dirty.update(self._time_sensitive_events & self.events.keys())
