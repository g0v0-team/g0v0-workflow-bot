# g0v0-workflow-bot

## GitHub Project assignment

Project assignment is enabled by default for `g0v0-team` Project 6. The GitHub App must be installed on the organization repositories with **Organization permissions → Projects → Read and write** and repository **Issues** and **Pull requests** read access. Subscribe to **Issues**, **Pull request**, and **Pull request review** webhook events.

- Issues created by a listed developer or assigned to one start in **To Do**. When a developer opens a PR that GitHub links to an issue, the issue moves to **In Progress**.
- A developer's PR starts in **In Progress**. An outstanding review request for a listed developer moves it to **Pending Review** for the author and assignees and **Pending Your Review** for the requested reviewer. Completing or removing the request clears that reviewer's pending status.
- Closing a tracked issue or closing or merging a tracked PR moves it to **Done** on the corresponding boards.

Use `project_assignment_enabled=false` to disable this integration. The project and roster can be configured with `project_assignment_owner`, `project_assignment_number`, and `project_assignment_developers`. The per-developer single-select fields in the project must have the same names as the configured GitHub logins.

## Review provider configuration

- `provider: str | None = "deepseek"` selects a built-in Pydantic AI provider and its model implementation.
- `review_base_url: str | None = None` overrides `provider` when supplied, using an OpenAI-compatible endpoint.
- `review_api_key` is passed to the selected provider. When omitted for a built-in provider, its normal environment-based authentication is used.
- If both `provider` and `review_base_url` are `None`, startup fails before review queues are restored.
- `review_request_limit` defaults to 1000 requests per iteration. The cumulative request limit is `review_request_limit × review_iteration` (1000 for iteration 1, 2000 for iteration 2, etc.), and historical request usage counts toward that cumulative limit. A positive integer changes the per-iteration allowance; `None` disables the limit.

For the built-in DeepSeek provider, leave `review_base_url` unset:

```dotenv
provider=deepseek
review_model=deepseek-flash
```

To use a custom gateway, set `review_base_url`; it takes priority over `provider`:

```dotenv
review_base_url=https://gateway.example.com/v1
review_api_key=your_api_key_here
review_model=your_model_here
```
