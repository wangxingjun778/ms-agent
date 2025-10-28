# Copyright (c) Alibaba, Inc. and its affiliates.
from dataclasses import dataclass

from .prompts import DEFAULT_IMPLEMENTATION, DEFAULT_PLAN, DEFAULT_TASKS


@dataclass
class Spec:
    """
    Specification for an AI agent's task planning and execution.
    """

    plan: str

    tasks: str

    implementation: str = ''

    def __post_init__(self):

        if not self.plan:
            self.plan = DEFAULT_PLAN

        if not self.tasks:
            self.tasks = DEFAULT_TASKS

        if not self.implementation:
            self.implementation = DEFAULT_IMPLEMENTATION


if __name__ == '__main__':
    spec = Spec(plan='', tasks='')
    print('Plan:', spec.plan)
    print('Tasks:', spec.tasks)
    print('Implementation:', spec.implementation)
