from asyncio import Lock

from src.service.github.project_api import Content, ProjectAPI

from nonebot.adapters.github import GitHubBot
from pydantic import BaseModel, PositiveInt


class ProjectAssignmentConfig(BaseModel):
    project_assignment_enabled: bool = True
    project_assignment_owner: str = "g0v0-team"
    project_assignment_number: PositiveInt = 6
    project_assignment_developers: tuple[str, ...] = (
        "MingxuanGame",
        "telecomadm1145",
        "GooGuJiang",
        "CloneWith",
        "chenjintang-shrimp",
    )


class ProjectAssignments:
    def __init__(self, config: ProjectAssignmentConfig) -> None:
        self.config = config
        self.owner = config.project_assignment_owner.casefold()
        self.developers = {login.casefold(): login for login in config.project_assignment_developers}
        self.lock = Lock()

    def _developer_fields(self, logins: set[str]) -> set[str]:
        return {self.developers[login.casefold()] for login in logins if login.casefold() in self.developers}

    def _statuses(self, content: Content, linked_pr_author: str | None = None) -> dict[str, str | None]:
        owners = self._developer_fields(content.assignees | ({content.author} if content.author else set()))
        reviewers = self._developer_fields(content.reviewers)
        previous = content.project_item.statuses.keys() & self.developers.values() if content.project_item else set()

        if not content.is_pull_request:
            linked_authors = content.linked_pr_authors | ({linked_pr_author} if linked_pr_author else set())
            linked_developers = self._developer_fields(linked_authors)
            owners |= linked_developers
            status = "In Progress" if linked_developers else "To Do"
        else:
            status = "Pending Review" if reviewers else "In Progress"

        statuses: dict[str, str | None] = {}
        if content.open:
            if not (owners or reviewers or previous):
                return {}
            statuses.update(dict.fromkeys(previous - owners - reviewers))
            statuses.update(dict.fromkeys(owners, status))
            statuses.update(dict.fromkeys(reviewers, "Pending Your Review"))
        else:
            if not (owners or reviewers or content.project_item):
                return {}
            status = "Done"
            statuses.update(dict.fromkeys(previous | owners | reviewers, status))
        statuses["Status"] = status
        return statuses

    async def sync(self, bot: GitHubBot, installation_id: int, owner: str, node_id: str) -> None:
        if not self.config.project_assignment_enabled or owner.casefold() != self.owner:
            return

        # Read current GitHub state inside the lock so delayed deliveries cannot restore stale review requests.
        async with self.lock, bot.as_installation(installation_id):
            api = ProjectAPI(bot.github)
            project = await api.get_project(self.config.project_assignment_owner, self.config.project_assignment_number)
            content = await api.get_content(node_id, project.id)
            if content is None or content.owner.casefold() != self.owner:
                return

            statuses = self._statuses(content)
            if statuses:
                await api.apply(project, content, statuses)

            if (
                content.is_pull_request
                and content.open
                and content.author
                and content.author.casefold() in self.developers
            ):
                for issue_id in content.linked_issues:
                    issue = await api.get_content(issue_id, project.id)
                    if issue is not None and issue.open and issue.owner.casefold() == self.owner:
                        await api.apply(project, issue, self._statuses(issue, linked_pr_author=content.author))
