# Future: Capability Discovery and Self-Extension

ReflexMesh may eventually discover devices and software capabilities that are available in its environment but absent from its current catalog. With an explicitly enabled autonomy mode, it could inspect a device's interface, install or create an adapter, and register the resulting capability.

Discovery does not imply that a device can fulfill a task. For example, a request to hand the user an apple requires a reachable robot that can locate and grasp the apple, hand it over, and provide evidence of the outcome. If any requirement cannot be established, the task remains unfulfilled.

Creating an adapter and authorizing actions through it are separate decisions. A new adapter must pass bounded checks before it is used, and each action remains subject to the task's permissions, limits, and verification rules. ReflexMesh reports success only when the requested outcome is confirmed; otherwise it reports a blocker or an unknown outcome.

This is a future direction, not a V0.5 deliverable or a claim about current functionality.
