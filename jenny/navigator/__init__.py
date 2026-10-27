"""Life Navigator runtime layer: perception, state aggregation, trigger and core.

This package is the *runtime* half of the Life Navigator. The *model* half
lives in :mod:`jenny.life_model` and is not replaced here: the runtime reads the
Life Model, turns observations into evidence, decides whether a thought is
worth an LLM call, and records what happened after the Navigator spoke.

The package ``__init__`` stays import-free on purpose (same rule as
``jenny.apps`` / ``jenny.bus``): importing a leaf such as
``jenny.navigator.trigger`` must not drag in the agent graph.
"""
