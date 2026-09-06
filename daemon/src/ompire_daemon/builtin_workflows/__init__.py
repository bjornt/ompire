"""Workflow definitions shipped with the daemon, as package resources.

The `.yaml` files here are data, not code: they are read through
`importlib.resources` so an installed daemon finds them inside its own
distribution rather than relative to a checkout. This module exists to make
that a real package rather than a namespace directory, so packaging tools
carry the resources into the wheel.
"""
