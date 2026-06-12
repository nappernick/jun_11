import asyncio
import socket
import tempfile
from pathlib import Path

import boto3

from bakeoff.quality.optimizer.backends import build_offline_backend
from bakeoff.quality.optimizer.controller import IterationController
from bakeoff.quality.optimizer.events import OptimizerEventEmitter
from bakeoff.quality.optimizer.store import OptimizerStore
from bakeoff.types import CohortKey, GoldFragment, Item, Turn


class _NullBroker:
    def publish(self, event_type, payload):
        pass


class NoNetwork:
    _BLOCKED = ("connect", "connect_ex")

    def __enter__(self):
        def block(*a, **k):
            raise AssertionError("network/boto3 use detected")

        self._s_connect = socket.socket.connect
        self._s_connect_ex = socket.socket.connect_ex
        self._create = socket.create_connection
        self._gai = socket.getaddrinfo
        self._b_client = boto3.client
        self._sess_client = boto3.Session.client
        socket.socket.connect = block
        socket.socket.connect_ex = block
        socket.create_connection = block
        socket.getaddrinfo = block
        boto3.client = block
        boto3.Session.client = block
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._s_connect
        socket.socket.connect_ex = self._s_connect_ex
        socket.create_connection = self._create
        socket.getaddrinfo = self._gai
        boto3.client = self._b_client
        boto3.Session.client = self._sess_client
        return False


def _cohort(ans="full"):
    return CohortKey(geography="US", proficiency="fluent", tone="neutral",
                     entry_route="slack", momentary_state="neutral",
                     answerability=ans, turn_type="multi")


def _gold_item(item_id):
    return Item(id=item_id, turn_type="multi", cohort=_cohort("full"),
                wants="how to request a corporate card", answerability="full",
                gold=[GoldFragment(node_id="g1", title="Corporate Card",
                                   markdown="Request a corporate card through the expense portal; it arrives within five business days.")],
                turns=(Turn(turn=1, user_utterance="How do I get a corporate card?",
                            momentary_state="neutral", answerability="full"),))


def _abstention_item(item_id):
    return Item(id=item_id, turn_type="multi", cohort=_cohort("none"), answerability="none",
                turns=(Turn(turn=1, user_utterance="Can I expense my neighbor's dental surgery?",
                            momentary_state="neutral", answerability="none"),))


items = [_gold_item("g-a"), _abstention_item("ab-c")]

with tempfile.TemporaryDirectory() as td:
    store = OptimizerStore(
        iterations_path=Path(td) / "it.jsonl", audit_path=Path(td) / "au.jsonl",
        errors_path=Path(td) / "er.jsonl", results_path=Path(td) / "res.json")
    backend = build_offline_backend()
    ctrl = IterationController(model="haiku-4.5", backend=backend, tuning_items=items,
                              store=store, emitter=OptimizerEventEmitter(_NullBroker()),
                              stop_limit=2, reps=1, seed_instruction="You are an FAQ assistant.")
    loop = asyncio.new_event_loop()
    try:
        with NoNetwork():
            res = loop.run_until_complete(ctrl.run_phase_a())
    finally:
        loop.close()
    print("OK guarded run, n_iters:", len(store.read_iterations()), "converged:", res.converged_iteration)
