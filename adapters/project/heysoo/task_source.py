"""Hey Soo! task source — the shared GitHub-Issues source, re-exported.

The implementation moved to ``adapters/project/github_issues.py`` when the selfhost
adapter started using it too (2026-07-01); this module keeps the historical import
path (`adapters.project.heysoo.task_source.GitHubIssuesSource`) working.
"""

from __future__ import annotations

from adapters.project.github_issues import GitHubIssuesSource

__all__ = ["GitHubIssuesSource"]
