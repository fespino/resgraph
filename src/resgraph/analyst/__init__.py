"""resgraph-analyst — a single agent over the registry's read tools.

Deliberately empty of re-exports: the registry registers this package's
privileged executor, so pulling the agent runtime in here would make
that import a cycle. Import from the submodules.
"""
