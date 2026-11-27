from datetime import UTC, datetime, timedelta, timezone

from src.service.github.models import GitHubReviewReminder, GitHubReviewWatch

from .notifications import (
    SUBMITTED_REVIEW_STATES,
    as_utc as _utc,
    developer_logins,
    send_github_notification,
)

from githubkit.exception import GitHubException, RequestFailed
from githubkit.versions.latest.models import PullRequest, SimpleUser
from nonebot import get_bots
from nonebot.adapters.github import GitHubBot
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

_DAY = timedelta(hours=24)
_APPROVAL_MARKER = "<!-- g0v0-review-timeout -->"
_APPROVAL_ROUND = "<!-- g0v0-review-round:"


async def _finish(watch: GitHubReviewWatch, at: datetime) -> None:
    async with get_session() as session, session.begin():
        finished = await session.scalar(
            update(GitHubReviewWatch)
            .where(
                GitHubReviewWatch.repository_id == watch.repository_id,
                GitHubReviewWatch.pull_number == watch.pull_number,
                GitHubReviewWatch.first_requested_at == watch.first_requested_at,
            )
            .values(finished=True)
            .returning(GitHubReviewWatch.repository_id)
        )
        if finished is None:
            return
        await session.execute(
            update(GitHubReviewReminder)
            .where(
                GitHubReviewReminder.repository_id == watch.repository_id,
                GitHubReviewReminder.pull_number == watch.pull_number,
                GitHubReviewReminder.requested_at >= watch.first_requested_at,
                GitHubReviewReminder.requested_at <= at,
            )
            .values(active=False)
        )


async def _latest_developer_request(bot: GitHubBot, pull: PullRequest, first: datetime) -> tuple[str, datetime] | None:
    before = None
    latest = None
    reviewers: set[str] = set()
    while True:
        response = await bot.github.async_graphql(
            """
            query($id: ID!, $before: String) {
              node(id: $id) {
                ... on PullRequest {
                  timelineItems(last: 50, before: $before, itemTypes: [REVIEW_REQUESTED_EVENT]) {
                    pageInfo { hasPreviousPage startCursor }
                    nodes {
                      ... on ReviewRequestedEvent {
                        createdAt
                        requestedReviewer { ... on User { login } }
                      }
                    }
                  }
                }
              }
            }
            """,
            {"id": pull.node_id, "before": before},
        )
        timeline = (response.get("node") or {}).get("timelineItems") or {}
        for event in reversed(timeline.get("nodes") or ()):
            reviewer = (((event or {}).get("requestedReviewer") or {}).get("login") or "").casefold()
            if reviewer not in developer_logins:
                continue
            try:
                requested_at = datetime.fromisoformat(event["createdAt"])
            except (KeyError, TypeError, ValueError):
                return None
            if _utc(requested_at) < first:
                return None
            if latest is None:
                latest = (reviewer, requested_at)
            reviewers.add(reviewer)
            if len(reviewers) >= 2:
                return latest
        page = timeline.get("pageInfo") or {}
        cursor = page.get("startCursor")
        if not page.get("hasPreviousPage") or not cursor or cursor == before:
            return None
        before = cursor


async def _approve_if_current(bot: GitHubBot, watch: GitHubReviewWatch, pull: PullRequest, now: datetime) -> bool:
    first = _utc(watch.first_requested_at)
    latest_request = await _latest_developer_request(bot, pull, first)
    if latest_request is None:
        return False
    login, requested_at = latest_request
    if _utc(requested_at) > now - _DAY:
        return False

    pr = (watch.owner, watch.repo, watch.pull_number)
    rest = bot.github.rest
    latest_pull = (await rest.pulls.async_get(*pr)).parsed_data
    if (
        latest_pull.base.repo.id != watch.repository_id
        or latest_pull.state != "open"
        or latest_pull.draft is not False
        or latest_pull.head.sha != pull.head.sha
        or latest_pull.updated_at != pull.updated_at
    ):
        return False
    requests = (await rest.pulls.async_list_requested_reviewers(*pr)).parsed_data
    reviewer = next((user for user in requests.users if user.login.casefold() == login), None)
    if reviewer is None:
        return False

    key = (watch.repository_id, watch.pull_number)
    async with get_session() as session:
        current = await session.get(GitHubReviewWatch, key)
        latest_reminder = await session.get(GitHubReviewReminder, (*key, reviewer.id))
        newest = await session.scalar(
            select(func.max(GitHubReviewReminder.requested_at)).where(
                GitHubReviewReminder.repository_id == watch.repository_id,
                GitHubReviewReminder.pull_number == watch.pull_number,
                GitHubReviewReminder.requested_at >= watch.first_requested_at,
                func.lower(GitHubReviewReminder.reviewer_login).in_(developer_logins),
            )
        )
        if (
            current is None
            or _utc(current.first_requested_at) != first
            or current.finished
            or current.has_activity
            or latest_reminder is None
            or not latest_reminder.active
            or newest is None
            or _utc(latest_reminder.requested_at) != _utc(newest)
            or not _utc(requested_at) <= _utc(newest) <= now - _DAY
        ):
            return False

    await rest.pulls.async_create_review(
        *pr,
        commit_id=latest_pull.head.sha,
        event="APPROVE",
        body=(
            "At least two developers were asked to review this pull request. The latest request has been pending "
            "for 24 hours without a response from a team developer or a requested reviewer. "
            f"The bot is approving this pull request under the review timeout policy.\n\n"
            f"{_APPROVAL_MARKER}\n{_APPROVAL_ROUND}{first.isoformat()} -->"
        ),
    )
    await _finish(watch, now)
    return True


async def _check_pr(watch: GitHubReviewWatch, now: datetime, *, private: bool) -> None:
    bot = get_bots().get(watch.app_id)
    if not isinstance(bot, GitHubBot):
        return

    pr = (watch.owner, watch.repo, watch.pull_number)
    key = (watch.repository_id, watch.pull_number)
    first = _utc(watch.first_requested_at)
    scope = (
        GitHubReviewReminder.repository_id == watch.repository_id,
        GitHubReviewReminder.pull_number == watch.pull_number,
        GitHubReviewReminder.requested_at >= watch.first_requested_at,
        GitHubReviewReminder.requested_at <= now,
    )
    async with get_session() as session:
        tracked_reviewers = set(await session.scalars(select(GitHubReviewReminder.reviewer_id).where(*scope)))

    async with bot.as_installation(watch.installation_id):
        rest = bot.github.rest
        pull = (await rest.pulls.async_get(*pr)).parsed_data
        if pull.base.repo.id != watch.repository_id:
            return
        if pull.state != "open":
            await _finish(watch, now)
            return
        if pull.draft is not False:
            return

        requests = (await rest.pulls.async_list_requested_reviewers(*pr)).parsed_data
        requested = {user.id for user in requests.users}
        solicited = tracked_reviewers | requested
        activity: dict[int, datetime] = {}
        volunteer = False
        latest_submitted: datetime | None = None
        previous_approval: datetime | None = None
        approval_round = f"{_APPROVAL_ROUND}{first.isoformat()} -->"

        def record_activity(user: SimpleUser | None, when: datetime, *, submitted: bool = False) -> None:
            nonlocal volunteer, latest_submitted
            when = _utc(when)
            if not user or user.type != "User" or when < first:
                return
            if user.id in solicited:
                activity[user.id] = max(when, activity.get(user.id, first))
            elif user.id != pull.user.id and user.login.casefold() in developer_logins:
                volunteer = True
            else:
                return
            if submitted and (latest_submitted is None or when > latest_submitted):
                latest_submitted = when

        async for review in rest.paginate(rest.pulls.async_list_reviews, None, *pr, per_page=100):
            user = review.user
            when = review.submitted_at
            if user and user.type == "Bot":
                if (
                    review.state in {"APPROVED", "DISMISSED"}
                    and _APPROVAL_MARKER in review.body
                    and (_APPROVAL_ROUND not in review.body or approval_round in review.body)
                    and isinstance(when, datetime)
                    and _utc(when) >= first
                ):
                    previous_approval = max(_utc(when), previous_approval or first)
                continue
            if review.state == "PENDING":
                record_activity(user, now)
            elif isinstance(when, datetime):
                record_activity(user, when, submitted=review.state in SUBMITTED_REVIEW_STATES)

        if previous_approval is not None and not watch.finished:
            await _finish(watch, previous_approval)

        async for comment in rest.paginate(rest.pulls.async_list_review_comments, None, *pr, since=first, per_page=100):
            record_activity(comment.user, comment.created_at)

        markers: set[str] = set()
        async for comment in rest.paginate(rest.issues.async_list_comments, None, *pr, since=first, per_page=100):
            if comment.user is None:
                continue
            if comment.user.type == "Bot":
                if isinstance(comment.body, str):
                    markers.add(comment.body)
            else:
                record_activity(comment.user, comment.created_at)

        async with get_session(expire_on_commit=False) as session, session.begin():
            current = await session.get(GitHubReviewWatch, key, with_for_update=True)
            if current is None or _utc(current.first_requested_at) != first:
                return
            if activity or volunteer:
                current.has_activity = True
            if volunteer:
                current.has_volunteer = True
            if latest_submitted is not None and (
                current.last_review_submitted_at is None or latest_submitted > _utc(current.last_review_submitted_at)
            ):
                current.last_review_submitted_at = latest_submitted
            reminders = []
            rows = list(await session.scalars(select(GitHubReviewReminder).where(*scope)))
            for reminder in rows:
                if not reminder.active:
                    continue
                if reminder.reviewer_id not in requested or activity.get(
                    reminder.reviewer_id, datetime.min.replace(tzinfo=UTC)
                ) >= _utc(reminder.requested_at):
                    reminder.active = False
                else:
                    reminders.append(reminder)

            developer_requests = [row for row in rows if row.reviewer_login.casefold() in developer_logins]
            newest = max((_utc(row.requested_at) for row in developer_requests), default=now)
            approve = (
                not private
                and not current.finished
                and not current.has_activity
                and len({row.reviewer_login.casefold() for row in developer_requests}) >= 2
                and newest <= now - _DAY
                and any(row.active and _utc(row.requested_at) == newest for row in developer_requests)
            )

        if approve and await _approve_if_current(bot, watch, pull, now):
            return

        for reminder in reminders:
            requested_at = _utc(reminder.requested_at)
            elapsed = now - requested_at
            if (private and elapsed >= _DAY) or (not private and (elapsed < _DAY or reminder.warnings)):
                continue
            async with get_session() as session:
                latest_reminder = await session.get(GitHubReviewReminder, (*key, reminder.reviewer_id))
                if (
                    latest_reminder is None
                    or not latest_reminder.active
                    or _utc(latest_reminder.requested_at) != requested_at
                    or (not private and latest_reminder.warnings)
                ):
                    continue
                current = await session.get(GitHubReviewWatch, key)
                if current is None or _utc(current.first_requested_at) != first:
                    continue
                has_volunteer = current.has_volunteer

            if private:
                if datetime.now(UTC) - requested_at >= _DAY:
                    continue
                await send_github_notification(
                    reminder.reviewer_id,
                    f"PR 审查提醒\n作者 @{pull.user.login}\n标题 {pull.title}\n"
                    f"请求者 @{reminder.requester}\n链接 {pull.html_url}",
                )
                continue

            marker = f"<!-- g0v0-review-wait:{reminder.reviewer_id}:{requested_at.isoformat()}:1 -->"
            if not any(marker in body for body in markers):
                suggestion = (
                    f" @{pull.user.login}, please consider requesting another reviewer." if not has_volunteer else ""
                )
                await rest.issues.async_create_comment(
                    *pr,
                    body=(
                        f"@{reminder.reviewer_login} has not responded to this review request within 24 hours."
                        f"{suggestion}\n\n{marker}"
                    ),
                )
            async with get_session() as session, session.begin():
                await session.execute(
                    update(GitHubReviewReminder)
                    .where(
                        *scope,
                        GitHubReviewReminder.reviewer_id == reminder.reviewer_id,
                        GitHubReviewReminder.requested_at == reminder.requested_at,
                        GitHubReviewReminder.active.is_(True),
                    )
                    .values(warnings=1)
                )


async def run_review_reminders(*, private: bool = False) -> None:
    now = datetime.now(UTC)
    pending = select(GitHubReviewReminder.reviewer_id).where(
        GitHubReviewReminder.repository_id == GitHubReviewWatch.repository_id,
        GitHubReviewReminder.pull_number == GitHubReviewWatch.pull_number,
        GitHubReviewReminder.requested_at >= GitHubReviewWatch.first_requested_at,
        GitHubReviewReminder.active.is_(True),
        GitHubReviewReminder.warnings == 0,
    )
    if private:
        due = exists(pending.where(GitHubReviewReminder.requested_at > now - _DAY))
    else:
        developer_rows = select(GitHubReviewReminder.repository_id).where(
            GitHubReviewReminder.repository_id == GitHubReviewWatch.repository_id,
            GitHubReviewReminder.pull_number == GitHubReviewWatch.pull_number,
            GitHubReviewReminder.requested_at >= GitHubReviewWatch.first_requested_at,
            func.lower(GitHubReviewReminder.reviewer_login).in_(developer_logins),
        )
        due = or_(
            and_(
                GitHubReviewWatch.finished.is_(False),
                GitHubReviewWatch.has_activity.is_(False),
                exists(
                    developer_rows.group_by(
                        GitHubReviewReminder.repository_id, GitHubReviewReminder.pull_number
                    ).having(
                        func.count(func.distinct(func.lower(GitHubReviewReminder.reviewer_login))) >= 2,
                        func.max(GitHubReviewReminder.requested_at) <= now - _DAY,
                    )
                ),
                developer_rows.with_only_columns(GitHubReviewReminder.active)
                .order_by(GitHubReviewReminder.requested_at.desc(), GitHubReviewReminder.active.desc())
                .limit(1)
                .scalar_subquery()
                .is_(True),
            ),
            exists(pending.where(GitHubReviewReminder.requested_at <= now - _DAY)),
        )
    async with get_session() as session:
        watches = list(await session.scalars(select(GitHubReviewWatch).where(due)))
    for watch in watches:
        try:
            await _check_pr(watch, now, private=private)
        except (GitHubException, SQLAlchemyError) as exc:
            reason = f"GitHub HTTP {exc.response.status_code}" if isinstance(exc, RequestFailed) else type(exc).__name__
            logger.warning(
                "GitHub review reminder failed for {}/{}#{}: {}", watch.owner, watch.repo, watch.pull_number, reason
            )


scheduler.add_job(run_review_reminders, "cron", minute="*", id="github_review_deadlines", replace_existing=True)
scheduler.add_job(
    run_review_reminders,
    "cron",
    hour="12,19",
    minute=0,
    timezone=timezone(timedelta(hours=8)),
    kwargs={"private": True},
    id="github_review_private_reminders",
    replace_existing=True,
)
