from dataclasses import dataclass
from typing import Any

from githubkit import GitHub


@dataclass(frozen=True)
class SingleSelectField:
    id: str
    options: dict[str, str]


@dataclass(frozen=True)
class Project:
    id: str
    fields: dict[str, SingleSelectField]


@dataclass(frozen=True)
class ProjectItem:
    id: str
    statuses: dict[str, str]


@dataclass(frozen=True)
class Content:
    id: str
    owner: str
    is_pull_request: bool
    open: bool
    author: str | None
    assignees: set[str]
    reviewers: set[str]
    linked_issues: list[str]
    linked_pr_authors: set[str]
    project_item: ProjectItem | None


_PROJECT_QUERY = """
query AssignmentProject($owner: String!, $number: Int!, $cursor: String) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      viewerCanUpdate
      fields(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
      }
    }
  }
}
"""

_CONTENT_FIELDS = """
id state
repository { owner { login } }
author { login }
assignees(first: 50, after: $assignees) {
  pageInfo { hasNextPage endCursor }
  nodes { login }
}
projectItems(first: 25, after: $projectItems, includeArchived: true) {
  pageInfo { hasNextPage endCursor }
  nodes {
    id
    project { id }
    fieldValues(first: 50) {
      nodes {
        ... on ProjectV2ItemFieldSingleSelectValue {
          name
          field { ... on ProjectV2FieldCommon { name } }
        }
      }
    }
  }
}
"""

_CONTENT_QUERY = (
    """
query AssignmentContent(
  $id: ID!,
  $assignees: String,
  $projectItems: String,
  $reviewRequests: String,
  $closingIssuesReferences: String,
  $closedByPullRequestsReferences: String
) {
  node(id: $id) {
    __typename
    ... on Issue {"""
    + _CONTENT_FIELDS
    + """
      closedByPullRequestsReferences(first: 50, after: $closedByPullRequestsReferences, includeClosedPrs: false) {
        pageInfo { hasNextPage endCursor }
        nodes { state author { login } repository { owner { login } } }
      }
    }
    ... on PullRequest {"""
    + _CONTENT_FIELDS
    + """
      closingIssuesReferences(first: 50, after: $closingIssuesReferences) {
        pageInfo { hasNextPage endCursor }
        nodes { id state repository { owner { login } } }
      }
      reviewRequests(first: 50, after: $reviewRequests) {
        pageInfo { hasNextPage endCursor }
        nodes { requestedReviewer { ... on User { login } } }
      }
    }
  }
}
"""
)

_ADD_ITEM = """
mutation AssignmentAddItem($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) { item { id } }
}
"""


class ProjectAPI:
    def __init__(self, github: GitHub) -> None:
        self.github = github

    async def get_project(self, owner: str, number: int) -> Project:
        fields: dict[str, SingleSelectField] = {}
        cursor = None
        while True:
            result = await self.github.async_graphql(
                _PROJECT_QUERY, {"owner": owner, "number": number, "cursor": cursor}
            )
            project = (result["organization"] or {}).get("projectV2")
            if project is None:
                raise ValueError(
                    f"GitHub App cannot read Project {owner}/{number}. Check organization Projects access."
                )
            if not project["viewerCanUpdate"]:
                raise ValueError(f"GitHub App needs organization Projects write access for {owner}/{number}.")
            connection = project["fields"]
            for field in connection["nodes"]:
                if field and "options" in field:
                    fields[field["name"]] = SingleSelectField(
                        field["id"], {option["name"]: option["id"] for option in field["options"]}
                    )
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                return Project(project["id"], fields)
            cursor = page["endCursor"]

    async def get_content(self, node_id: str, project_id: str) -> Content | None:
        node = (await self.github.async_graphql(_CONTENT_QUERY, {"id": node_id}))["node"]
        if node is None or node["__typename"] not in {"Issue", "PullRequest"}:
            return None

        is_pull_request = node["__typename"] == "PullRequest"
        names = ["assignees", "projectItems"]
        if is_pull_request:
            names.extend(["closingIssuesReferences", "reviewRequests"])
        else:
            names.append("closedByPullRequestsReferences")

        values: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
        page_node = node
        while names:
            cursors = {}
            for name in names:
                if not (connection := page_node[name]):
                    continue
                values[name].extend(value for value in connection["nodes"] if value is not None)
                page = connection["pageInfo"]
                if page["hasNextPage"]:
                    cursors[name] = page["endCursor"]
            names = list(cursors)
            if names:
                page_node = (await self.github.async_graphql(_CONTENT_QUERY, {"id": node_id, **cursors}))["node"]
                if page_node is None:
                    return None

        project_item = None
        for item in values["projectItems"]:
            if item["project"]["id"] == project_id:
                project_item = ProjectItem(
                    item["id"],
                    {
                        value["field"]["name"]: value["name"]
                        for value in item["fieldValues"]["nodes"]
                        if value and value.get("field")
                    },
                )
                break

        owner = node["repository"]["owner"]["login"]
        linked_issues = [
            issue["id"]
            for issue in values.get("closingIssuesReferences", [])
            if issue["state"] == "OPEN" and issue["repository"]["owner"]["login"].casefold() == owner.casefold()
        ]
        linked_pr_authors = {
            pull["author"]["login"]
            for pull in values.get("closedByPullRequestsReferences", [])
            if pull["state"] == "OPEN"
            and pull["author"] is not None
            and pull["repository"]["owner"]["login"].casefold() == owner.casefold()
        }
        reviewers = {
            reviewer["login"]
            for link in values.get("reviewRequests", [])
            if (reviewer := link["requestedReviewer"]) and "login" in reviewer
        }
        return Content(
            id=node["id"],
            owner=owner,
            is_pull_request=is_pull_request,
            open=node["state"] == "OPEN",
            author=node["author"]["login"] if node["author"] else None,
            assignees={assignee["login"] for assignee in values["assignees"]},
            reviewers=reviewers,
            linked_issues=linked_issues,
            linked_pr_authors=linked_pr_authors,
            project_item=project_item,
        )

    async def apply(self, project: Project, content: Content, statuses: dict[str, str | None]) -> None:
        item = content.project_item
        current = item.statuses if item else {}
        changes = {field_name: value for field_name, value in statuses.items() if current.get(field_name) != value}
        if not changes:
            return

        updates = []
        for field_name, status in sorted(changes.items()):
            field = project.fields.get(field_name)
            if field is None:
                raise ValueError(f"Project is missing the single-select field {field_name!r}.")
            option_id = field.options.get(status) if status is not None else None
            if status is not None and option_id is None:
                raise ValueError(f"Project field {field_name!r} is missing the option {status!r}.")
            updates.append((field.id, option_id))

        if item is None:
            result = await self.github.async_graphql(_ADD_ITEM, {"projectId": project.id, "contentId": content.id})
            item_id = result["addProjectV2ItemById"]["item"]["id"]
        else:
            item_id = item.id

        declarations = []
        operations = []
        variables = {}
        for index, (field_id, option_id) in enumerate(updates):
            key = f"change{index}"
            field_input: dict[str, Any] = {"projectId": project.id, "itemId": item_id, "fieldId": field_id}
            if option_id is None:
                mutation = "clearProjectV2ItemFieldValue"
                input_type = "ClearProjectV2ItemFieldValueInput"
            else:
                mutation = "updateProjectV2ItemFieldValue"
                input_type = "UpdateProjectV2ItemFieldValueInput"
                field_input["value"] = {"singleSelectOptionId": option_id}
            declarations.append(f"${key}: {input_type}!")
            operations.append(f"{key}: {mutation}(input: ${key}) {{ projectV2Item {{ id }} }}")
            variables[key] = field_input
        query = f"mutation AssignmentUpdateStatus({', '.join(declarations)}) {{ {' '.join(operations)} }}"
        await self.github.async_graphql(query, variables)
