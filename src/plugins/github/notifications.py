from datetime import UTC, datetime
from typing import get_args

from src.service.github.models import GitHubAccountBinding, GitHubReviewReminder, GitHubReviewWatch

from .project_assignment import project_assignments

from githubkit.exception import GitHubException
import nonebot
from nonebot import on_type
from nonebot.adapters.github import (
    GitHubBot,
    IssueCommentCreated,
    IssuesAssigned,
    PullRequestAssigned,
    PullRequestClosed,
    PullRequestReviewCommentCreated,
    PullRequestReviewCommentEdited,
    PullRequestReviewDismissed,
    PullRequestReviewEdited,
    PullRequestReviewRequested,
    PullRequestReviewRequestRemoved,
    PullRequestReviewSubmitted,
    PullRequestReviewThreadResolved,
    PullRequestReviewThreadUnresolved,
)
from nonebot.adapters.onebot.v11 import (
    Bot as OneBot,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import OneBotV11AdapterException
from nonebot.log import logger
from nonebot_plugin_orm import get_session
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


class GitHubNotificationConfig(BaseModel):
    github_notification_onebot_id: str | None = None


config = nonebot.get_plugin_config(GitHubNotificationConfig)
owner = project_assignments.config.project_assignment_owner.casefold()
developer_logins = {login.casefold() for login in project_assignments.config.project_assignment_developers}
SUBMITTED_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def send_github_notification(github_id: int, message: str) -> None:
    bots = [
        bot
        for bot in nonebot.get_bots().values()
        if isinstance(bot, OneBot)
        and (config.github_notification_onebot_id is None or bot.self_id == config.github_notification_onebot_id)
    ]
    if len(bots) != 1:
        return
    async with get_session() as session:
        qq_id = await session.scalar(
            select(GitHubAccountBinding.qq_id).where(GitHubAccountBinding.github_id == github_id)
        )
    if qq_id is not None:
        try:
            await bots[0].send_private_msg(user_id=qq_id, message=Message(MessageSegment.text(message)))
        except OneBotV11AdapterException as exc:
            logger.warning("GitHub notification could not be sent through OneBot: {}", type(exc).__name__)


assigned = on_type((IssuesAssigned, PullRequestAssigned), block=False)
review_requested = on_type(PullRequestReviewRequested, block=False)
review_ended = on_type((PullRequestReviewRequestRemoved, PullRequestClosed), block=False)
ReviewActivity = (
    PullRequestReviewSubmitted
    | PullRequestReviewEdited
    | PullRequestReviewDismissed
    | PullRequestReviewCommentCreated
    | PullRequestReviewCommentEdited
    | PullRequestReviewThreadResolved
    | PullRequestReviewThreadUnresolved
    | IssueCommentCreated
)
review_activity = on_type(get_args(ReviewActivity), block=False)


@assigned.handle()
async def notify_assignment(event: IssuesAssigned | PullRequestAssigned) -> None:
    payload = event.payload
    if payload.repository.owner.login.casefold() != owner or not payload.assignee:
        return
    item = event.payload.issue if isinstance(event, IssuesAssigned) else event.payload.pull_request
    kind = "Issue" if isinstance(event, IssuesAssigned) else "PR"
    author = item.user.login if item.user else "未知"
    title = " ".join(item.title.splitlines())
    await send_github_notification(
        payload.assignee.id,
        f"GitHub {kind} 分配给你\n作者：{author}\n标题：{title}\n分配者：{payload.sender.login}\n链接：{item.html_url}",
    )


@review_requested.handle()
async def notify_review_requested(bot: GitHubBot, event: PullRequestReviewRequested) -> None:
    payload = event.payload
    repository = payload.repository
    installation = payload.installation
    if repository.owner.login.casefold() != owner or not installation:
        return
    pull = payload.pull_request
    reviewer = getattr(payload, "requested_reviewer", None)
    now = datetime.now(UTC)
    first_requested_at = requested_at = now
    review_requests: list[tuple[str, datetime]] = []
    submitted_reviews: list[tuple[str, datetime]] = []
    try:
        async with bot.as_installation(installation.id):
            timeline = await bot.github.async_graphql(
                """
                query($id: ID!) {
                  node(id: $id) {
                    ... on PullRequest {
                      first: timelineItems(first: 1, itemTypes: [REVIEW_REQUESTED_EVENT]) {
                        nodes { ... on ReviewRequestedEvent { createdAt } }
                      }
                      recent: timelineItems(last: 50, itemTypes: [REVIEW_REQUESTED_EVENT, PULL_REQUEST_REVIEW]) {
                        nodes {
                          ... on ReviewRequestedEvent { createdAt requestedReviewer { ... on User { login } } }
                          ... on PullRequestReview { state submittedAt author { __typename login } }
                        }
                      }
                    }
                  }
                }
                """,
                {"id": pull.node_id},
            )
        node = timeline.get("node") or {}
        first = (node.get("first") or {}).get("nodes") or []
        if first and first[0] and first[0].get("createdAt"):
            first_requested_at = as_utc(datetime.fromisoformat(first[0]["createdAt"]))
        if reviewer:
            for entry in (node.get("recent") or {}).get("nodes") or []:
                if not entry:
                    continue
                requested_login = (entry.get("requestedReviewer") or {}).get("login")
                author = entry.get("author") or {}
                if requested_login and entry.get("createdAt"):
                    when = as_utc(datetime.fromisoformat(entry["createdAt"]))
                    login = requested_login.casefold()
                    review_requests.append((login, when))
                elif (
                    author.get("__typename") == "User"
                    and author.get("login")
                    and entry.get("state") in SUBMITTED_REVIEW_STATES
                    and entry.get("submittedAt")
                ):
                    submitted_reviews.append(
                        (author["login"].casefold(), as_utc(datetime.fromisoformat(entry["submittedAt"])))
                    )
            requested_at = max(
                (when for login, when in review_requests if login == reviewer.login.casefold()), default=now
            )
    except (GitHubException, ValueError) as exc:
        logger.warning("Could not read GitHub review request time: {}", type(exc).__name__)

    author = pull.user.login if pull.user else "未知"
    title = " ".join(pull.title.splitlines())
    requester = payload.sender.login
    notified_reviewer = None
    async with get_session() as session, session.begin():
        insert = (
            sqlite_insert if session.get_bind(mapper=GitHubReviewWatch).dialect.name == "sqlite" else postgres_insert
        )
        repo = {
            "owner": repository.owner.login,
            "repo": repository.name,
            "app_id": bot.self_id,
            "installation_id": installation.id,
        }
        statement = insert(GitHubReviewWatch).values(
            repository_id=repository.id, pull_number=pull.number, first_requested_at=first_requested_at, **repo
        )
        watch = await session.scalar(
            statement.on_conflict_do_update(
                index_elements=[GitHubReviewWatch.repository_id, GitHubReviewWatch.pull_number],
                set_=repo,
            ).returning(GitHubReviewWatch)
        )
        if watch is None or not reviewer or reviewer.type != "User":
            return
        round_start = as_utc(watch.first_requested_at)
        if requested_at < round_start:
            return

        last_review = as_utc(watch.last_review_submitted_at) if watch.last_review_submitted_at else None
        scope = (
            GitHubReviewReminder.repository_id == repository.id,
            GitHubReviewReminder.pull_number == pull.number,
        )
        if submitted_reviews:
            stored_requests = await session.execute(
                select(GitHubReviewReminder.reviewer_login, GitHubReviewReminder.requested_at).where(
                    *scope, GitHubReviewReminder.requested_at >= round_start
                )
            )
            invitations = review_requests + [(login.casefold(), as_utc(when)) for login, when in stored_requests]
            for login, when in submitted_reviews:
                if when < round_start or (last_review is not None and when <= last_review):
                    continue
                invited = any(user == login and round_start <= time <= when for user, time in invitations)
                if invited or (login in developer_logins and login != author.casefold()):
                    last_review = when

        if last_review is not None and round_start <= last_review < requested_at:
            # A later request can arrive first, so take the earliest request in the new round.
            next_start = min(
                (when for _, when in review_requests if last_review < when <= requested_at),
                default=requested_at,
            )
            watch.first_requested_at = next_start
            watch.last_review_submitted_at = None
            watch.has_activity = watch.has_volunteer = watch.finished = False
            await session.execute(
                update(GitHubReviewReminder)
                .where(*scope, GitHubReviewReminder.requested_at < next_start)
                .values(active=False)
            )
        elif last_review is not None:
            watch.last_review_submitted_at = last_review
            watch.has_activity = True

        details = {
            "reviewer_login": reviewer.login,
            "requested_at": requested_at,
            "requester": requester,
            "warnings": 0,
            "active": True,
        }
        reminder = insert(GitHubReviewReminder).values(
            repository_id=repository.id, pull_number=pull.number, reviewer_id=reviewer.id, **details
        )
        notified_reviewer = await session.scalar(
            reminder.on_conflict_do_update(
                index_elements=[
                    GitHubReviewReminder.repository_id,
                    GitHubReviewReminder.pull_number,
                    GitHubReviewReminder.reviewer_id,
                ],
                set_=details,
                where=GitHubReviewReminder.requested_at < requested_at,
            ).returning(GitHubReviewReminder.reviewer_id)
        )
    if notified_reviewer is not None:
        await send_github_notification(
            notified_reviewer,
            f"GitHub PR 请求你审查\n作者：{author}\n标题：{title}\n请求者：{requester}\n链接：{pull.html_url}",
        )


@review_ended.handle()
async def stop_review_reminders(event: PullRequestReviewRequestRemoved | PullRequestClosed) -> None:
    payload = event.payload
    if payload.repository.owner.login.casefold() != owner:
        return
    repository_id, number = payload.repository.id, payload.pull_request.number
    query = update(GitHubReviewReminder).where(
        GitHubReviewReminder.repository_id == repository_id, GitHubReviewReminder.pull_number == number
    )
    if isinstance(event, PullRequestReviewRequestRemoved):
        reviewer = getattr(event.payload, "requested_reviewer", None)
        if not reviewer:
            return
        query = query.where(GitHubReviewReminder.reviewer_id == reviewer.id)
    async with get_session() as session, session.begin():
        await session.execute(query.values(active=False))
        if isinstance(event, PullRequestClosed):
            await session.execute(
                update(GitHubReviewWatch)
                .where(GitHubReviewWatch.repository_id == repository_id, GitHubReviewWatch.pull_number == number)
                .values(finished=True)
            )


@review_activity.handle()
async def record_review_activity(event: ReviewActivity) -> None:
    payload = event.payload
    sender = payload.sender
    if payload.repository.owner.login.casefold() != owner or not sender or sender.type != "User":
        return
    if isinstance(event, IssueCommentCreated):
        item = event.payload.issue
        if not item.pull_request:
            return
    else:
        item = event.payload.pull_request
    author = item.user.login.casefold() if item.user else None
    activity_at = datetime.now(UTC)
    submitted_at = None
    if isinstance(event, PullRequestReviewSubmitted):
        review = event.payload.review
        if review.submitted_at is not None:
            activity_at = as_utc(review.submitted_at)
            if review.state.upper() in SUBMITTED_REVIEW_STATES:
                submitted_at = activity_at
    elif isinstance(event, (IssueCommentCreated, PullRequestReviewCommentCreated)):
        activity_at = as_utc(event.payload.comment.created_at)
    elif isinstance(event, PullRequestReviewCommentEdited):
        activity_at = as_utc(event.payload.comment.updated_at)

    async with get_session() as session, session.begin():
        watch = await session.get(GitHubReviewWatch, (payload.repository.id, item.number), with_for_update=True)
        if watch is None or activity_at < as_utc(watch.first_requested_at):
            return
        reminder = await session.get(GitHubReviewReminder, (payload.repository.id, item.number, sender.id))
        if reminder is not None and as_utc(reminder.requested_at) >= as_utc(watch.first_requested_at):
            if as_utc(reminder.requested_at) <= activity_at:
                reminder.active = False
        elif sender.login.casefold() in developer_logins and sender.login.casefold() != author:
            watch.has_volunteer = True
        else:
            return
        watch.has_activity = True
        if submitted_at is not None and (
            watch.last_review_submitted_at is None or submitted_at > as_utc(watch.last_review_submitted_at)
        ):
            watch.last_review_submitted_at = submitted_at
