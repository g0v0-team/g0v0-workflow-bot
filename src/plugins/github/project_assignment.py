from typing import get_args

from src.service.github.project_assignment import ProjectAssignmentConfig, ProjectAssignments

import nonebot
from nonebot import on_type
from nonebot.adapters.github import (
    GitHubBot,
    IssuesAssigned,
    IssuesClosed,
    IssuesOpened,
    IssuesReopened,
    IssuesUnassigned,
    PullRequestAssigned,
    PullRequestClosed,
    PullRequestConvertedToDraft,
    PullRequestEdited,
    PullRequestOpened,
    PullRequestReadyForReview,
    PullRequestReopened,
    PullRequestReviewDismissed,
    PullRequestReviewRequested,
    PullRequestReviewRequestRemoved,
    PullRequestReviewSubmitted,
    PullRequestUnassigned,
)

ProjectIssueEvents = IssuesAssigned | IssuesClosed | IssuesOpened | IssuesReopened | IssuesUnassigned
ProjectPullRequestEvents = (
    PullRequestAssigned
    | PullRequestClosed
    | PullRequestConvertedToDraft
    | PullRequestEdited
    | PullRequestOpened
    | PullRequestReadyForReview
    | PullRequestReopened
    | PullRequestReviewDismissed
    | PullRequestReviewRequested
    | PullRequestReviewRequestRemoved
    | PullRequestReviewSubmitted
    | PullRequestUnassigned
)
project_assignments = ProjectAssignments(nonebot.get_plugin_config(ProjectAssignmentConfig))
# Match alongside the existing PR review handlers, which block lower-priority matchers.
project_assignment_event = on_type(get_args(ProjectIssueEvents | ProjectPullRequestEvents), block=False)


@project_assignment_event.handle()
async def handle_project_assignment(bot: GitHubBot, event: ProjectIssueEvents | ProjectPullRequestEvents) -> None:
    installation = event.payload.installation
    if not installation:
        return
    owner = event.payload.repository.owner.login
    item = event.payload.issue if isinstance(event, ProjectIssueEvents) else event.payload.pull_request
    await project_assignments.sync(bot, installation.id, owner, item.node_id)
