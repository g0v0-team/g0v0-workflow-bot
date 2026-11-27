# g0v0-workflow-bot

## GitHub Project assignment

Project assignment is enabled by default for `g0v0-team` Project 6. The GitHub App must be installed on the organization repositories with **Organization permissions → Projects → Read and write** and repository **Issues** and **Pull requests** read access. Subscribe to **Issues**, **Pull request**, and **Pull request review** webhook events.

- Issues created by a listed developer or assigned to one start in **To Do**. When a developer opens a PR that GitHub links to an issue, the issue moves to **In Progress**.
- A developer's PR starts in **In Progress**. An outstanding review request for a listed developer moves it to **Pending Review** for the author and assignees and **Pending Your Review** for the requested reviewer. Completing or removing the request clears that reviewer's pending status.
- Closing a tracked issue or closing or merging a tracked PR moves it to **Done** on the corresponding boards.

Use `project_assignment_enabled=false` to disable this integration. The project and roster can be configured with `project_assignment_owner`, `project_assignment_number`, and `project_assignment_developers`. The per-developer single-select fields in the project must have the same names as the configured GitHub logins.

## GitHub account binding

Register a dedicated GitHub OAuth App with `<github_oauth_base_url>/gh-oauth` as its authorization callback URL. The default callback is `https://example.com/gh-oauth`. Set these values in the deployed bot's environment.

```dotenv
github_oauth_client_id=your_oauth_app_client_id
github_oauth_client_secret=your_oauth_app_client_secret
github_oauth_base_url=https://example.com
```

Run `LOG_LEVEL=INFO nb orm upgrade` before restarting the bot, and route `GET /gh-oauth` through the HTTPS reverse proxy to the bot. Set the deployment log level to `INFO` and omit callback query parameters from access logs.

Send `/ghbind` to the bot in a OneBot private chat. The link expires after 10 minutes and can be used once. The OAuth App requests an empty scope and reads the authenticated user's GitHub ID and login. The database stores these values with the initiating QQ ID. A successful authorization can update that QQ user's binding, and each GitHub ID can belong to only one QQ account.

## GitHub notifications and review timeouts

The bot sends OneBot private messages for issue and PR assignments, and for individual PR review requests. Recipients must have a GitHub account linked with `/ghbind`. If more than one OneBot account is connected, set `github_notification_onebot_id` to the account that should send these messages.

- Pending reviewers get reminders at 12:00 and 19:00 UTC+8 during the first 24 hours of each request. A review response or withdrawal of the request stops that reviewer's reminders.
- After 24 hours without a response, the bot comments on the PR and suggests another reviewer. Each new review request starts its own 24-hour reminder period.
- A PR becomes eligible for automatic approval after individual review requests have been sent to at least two distinct developers in `project_assignment_developers` in the current round. The latest request to a configured developer starts a 24-hour countdown. A later request restarts the countdown. Before approving, the bot checks that the latest request is still pending and that the PR is open and out of draft.
- A requested reviewer or a configured developer who intervenes cancels automatic approval. If a configured developer who was never requested intervenes through a public PR comment or review, the original reviewers keep their reminders and the timeout comment omits the suggestion to find someone else.
- After a requested reviewer or a configured developer submits a review, the next individual review request starts a new round. Earlier requests stop generating reminders and their reviewers no longer count toward the two-developer threshold. The bot writes timeout comments and automatic approval messages on GitHub in English.

GitHub only exposes public review activity to the bot. A private draft review becomes visible after it is submitted. Apply the ORM migration before enabling these jobs.

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
