"""team task class T CLASS: give a task whose store record has no class (made before task classes existed) its class,
once. The event is task.created with only {"class": CLASS}, the partial upsert `team state import` already writes for a
rename: the store folds it into the task's row, and stats.task_classes reads the class of every task.created. Nothing
new for either to learn; a store without the event reads as before. The review floor never drops: the class must
require at least the reviews the store assumes for a classless task (class_options(.., "standard")). A leaf: state.do_task
hands the arguments over; each reaches the other at call time."""

from __future__ import annotations

import importlib

from teamlib import core, flow
from teamlib.core import config, die

FLOOR = "standard"


def do_class(args: list[str], usage: str) -> int:
    state = importlib.import_module("teamlib.state")  # a leaf never imports another leaf by name
    positional, _ = state.parse(args, usage)
    if len(positional) != 2:
        die(usage)
    task, klass = state.need(state.task_id, positional[0]), positional[1]

    def prepare(snap):
        have = state.known_task(snap, task).get("class")
        if have:
            die(f"{task} already has the class {have}; a class is given once")
        classes = core.known_classes()
        if klass not in classes:
            die(f"class {klass!r} is not one of {', '.join(classes)}")
        ini, floor = config(), flow.class_options(config(), FLOOR)["reviews"]
        if flow.class_options(ini, klass)["reviews"] < floor:
            die(f"class {klass} requires fewer reviews than {FLOOR} ({floor}), which the store assumes for a task "
                f"without a class; the review floor does not drop")
        return [("task.created", "lead", task, {"class": klass})], f"{task} class {klass}"
    return state.transact(prepare)
