"""Runner: pulls jobs from the workspace Worker, runs the engine, posts results.

See docs/RUNNER.md. The runner holds one bearer token bound to one campaign
and never receives mail, MuckRock or Brevo credentials; every external
effect it wants is proposed as an external_action for organizer approval.
"""
__version__ = "0.1.0"
