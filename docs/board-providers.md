# Board providers

This is the authoritative reference for the registered task-board providers. A board type is
supported only when its adapter is registered with the full lifecycle contract; the names below are
the current matrix, not a closed list of future choices.

## Capability matrix

One row per registered provider; every row must fill all ten capability columns.

| Provider | Sync/pagination | Markdown normalization | Links/subtasks | Attachments | Single read | Discovery | Create/target | Finish/PR link | Write-through | Native-subtask writes |
|---|:-:|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| YouGile | ✓ | HTML↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Supported |
| YouTrack | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| Jira Cloud | ✓ | ADF↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| GitHub Issues | ✓ | Native MD | ✓ | metadata only | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| Trello | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| Linear | ✓ | Native MD | ✓ | metadata only | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| ClickUp | ✓ | Native MD (query flag) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| Asana | ✓ | HTML↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| Yandex Tracker | ✓ | YFM→MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| Kaiten | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |
| Weeek | ✓ | HTML↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Unsupported |

All adapters implement validation, full pagination, normalized reads, target discovery, create,
idempotent finish, and write-through reindexing. The registry exposes only configured types whose
credential schema is complete; secrets never leave the reviewer-mcp process.

Attachment indexing is fail-soft everywhere: when a file cannot be fetched with the board
credential — an off-host or short-lived signed URL, a size or format limit — the adapter reports
the attachment's metadata plus a warning instead of failing the task. `metadata only` marks a
board whose API never exposes attachment bytes at all.

## Native subtask writes

The generic MCP operation is:

```text
create_subtasks(parent_key, subtasks, idempotency_key, board_type=None,
                project=None, provider_options=None)
```

Clients discover support through `get_board_targets`: its registry-owned capability
`native_subtasks` is the authoritative discovery result. Capability metadata comes from the
immutable registry spec, not provider-returned validation or discovery data; a provider result
cannot add or self-spoof it. YouGile alone is registered with this capability today. Every other
row is explicitly unsupported even if that board API has some concept of child work.

An unsupported provider returns `category=unsupported` before a board write. There is no fallback
to individual task writes through repeated `create_task` calls: doing that would lose native
attachment semantics and batch retry safety. The currently supported adapter creates real child
cards and attaches their board IDs through the native parent `subtasks` field. Sync then exposes
the provider identities as canonical `subtask` links; clients must not infer child keys or URLs.

Each child card carries the visible technical marker
`reviewer-subtask:<64 lowercase hex>`. It remains on the raw board card so an unknown-outcome retry
can scan and reconcile already-created children. Normalization strips and hides the exact
`reviewer-subtask:<64 lowercase hex>` marker from normalized user-facing text before task-store,
search, graph, or model context is built. The marker is deterministic for the confirmed operation
and child, but is not a user-visible task identity or a substitute for a canonical link.

Completion requires strict parent-and-children write-through: the updated parent and every
returned child are normalized and stored, and their canonical links are written to the task graph.
A board-complete batch whose write-through has not completed remains partial and retryable; it is
never reported as fully indexed. Warnings are sanitized and preserve reconciliation or manual
recovery facts without exposing credentials.

### Result buckets

The operation reports overlapping lifecycle views rather than one ambiguous task list:

| Bucket | Meaning |
|---|---|
| `created` | Children known to exist on the board; this includes attached and unattached children. |
| `attached` | Created children present in the native parent relationship. |
| `unattached` | Created children not yet present in the native parent relationship. |
| `pending` | Children not yet known to be created, including unresolved in-flight work. |
| `warnings` | Sanitized reconciliation, provider, write-through, or manual-recovery details. |

### Durable recovery

Durable retries prefer safety over liveness. Persisted state determines the only allowed retry
work; an ambiguous board outcome is never converted into another speculative write.

| Persisted state | Retry behavior | Safety boundary |
|---|---|---|
| `in_flight` | A same-key retry reconciles all persisted `in_flight` markers before any create attempt. | With no exact marker match or multiple matches, reviewer never issues a child `POST` again. That child remains in `pending` output with `manual_required=true`; operator/manual board verification is required, and repeated retry must not be expected to make progress. |
| `board_complete` | A retry performs only strict parent-and-children write-through/reindex. | It performs no child `POST` and no parent attachment `PUT`; the completed board relationships are only verified and reindexed. |

### Retry safety

A retry with the same idempotency key and the same full payload is safe: the durable operation
ledger and reconciliation markers resume known progress instead of intentionally duplicating a
child. The same idempotency key with a different payload is a non-retryable `conflict`. Exact retry
therefore preserves parent, child order and wording, provider configuration, and the complete
payload; it never retries only a guessed remainder.

## Shared transport

New adapters build on the shared transport layer instead of re-implementing HTTP behaviour:
`restbase.py` (`RestBoardBase` — client lifecycle, secret redaction, read/write split),
`pagination.py` (offset, page, cursor and `Link`-header generators), `graphql.py`
(`GraphQLClient` with cursor pagination and error categorisation), and `yfm.py`. Retries,
`Retry-After` handling and status categorisation live in `BoardHttpClient`; a provider only
supplies an optional `rate_limit_hint` for board-specific limit headers.

**Known debt:** the three original adapters (YouGile, YouTrack, Jira Cloud) predate this layer and
still own their httpx wiring. Retrofitting them is deliberately out of scope of the provider
expansion — it would rewrite three green adapters and their tests — and is tracked as follow-up.

## Configuration

Use the same shape for every registered provider. `type` is a registered provider key;
`create_target`, `done_target`, and `options` are resolved by discovery rather than by a
provider-specific playbook.

```yaml
task_board:
  type: <registered-provider>
  project: PRI
  key_pattern: "[A-Z]+-\\d+"      # optional non-secret task-key metadata
  url_template: "https://tasks.example/{code}"  # optional non-secret link metadata; also feeds the PR backlink
  create_target: Backlog
  done_target: Done
  options:
    <provider-option>: <discovered-value>
```

Use the non-secret `provider_options` object with MCP tools. The public signatures are:

```text
sync_board(board=None, limit=None, purge_orphaned=False, keep_with_prs=True,
           board_type=None, provider_options=None, force_renormalize=False)
get_board_targets(board_type=None, project=None, provider_options=None)
create_task(title, problem="", steps=None, criteria=None, context=None, board_type=None,
            project=None, target=None, provider_options=None)
create_subtasks(parent_key, subtasks, idempotency_key, board_type=None,
                project=None, provider_options=None)
finish_task(key, pr_url, note=None, mark_done=True, board_type=None, target=None,
            provider_options=None)
```

Credentials are server-side environment variables, never values in `.review.yml`,
`provider_options`, a client MCP configuration, commits, previews, errors, or logs. Run
`reviewer check` after setup or rotation; it validates each configured provider and reports only
safe identity, project, permission, and configuration metadata. Pass a non-secret project for
project-scoped validation, for example `reviewer check --board-project jira=PRI`; repeat
`--board-project TYPE=PROJECT` for a multi-provider deployment. Without a Jira project, identity
is still checked, but create/transition permissions are reported as unknown.

## YouGile

Set `YOUGILE_API_KEY` and, when needed, `YOUGILE_API_BASE`. Follow the official
[REST API v2 guide](https://ru.yougile.com/api-v2). The installer can request login/password by
hidden input only to obtain a company and create an API key; it removes the password immediately
and stores only `YOUGILE_API_KEY`. Rotate by creating a replacement key, updating the protected
reviewer-mcp environment, running `reviewer check`, then revoking the old key after validation.

OpenID Connect in self-hosted YouGile is for user sign-in, not an authorization flow for REST integrations. If
`allowOnlyOpenId` is enabled, provide an API key created by a separate
API-capable account with the minimum required permissions; the acquisition flow must not try to
exchange an OIDC session for a REST credential.

## YouTrack

Set `YOUTRACK_BASE_URL` and `YOUTRACK_TOKEN`. Create a permanent token with the required YouTrack
service scope and preserve its `perm:` prefix; see JetBrains'
[permanent-token documentation](https://www.jetbrains.com/help/youtrack/devportal/authentication-with-permanent-token.html).
Keep it in the protected reviewer-mcp environment through hidden input or a secret manager. For
rotation, create and validate the replacement with `reviewer check`, switch the environment, then
revoke the old token.

## Jira Cloud

Jira support is **Cloud only**. Set `JIRA_BASE_URL` to the direct site URL (for example,
`https://company.atlassian.net`), plus `JIRA_EMAIL` and `JIRA_API_TOKEN`. Create an API token
[in the Atlassian account UI](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/)
without scopes for this integration. The adapter uses Basic authentication with
email and API token; it never accepts an Atlassian password.

This first release deliberately does not support scoped tokens: they require the
`api.atlassian.com/ex/jira/<cloudId>` gateway rather than the direct site URL used by
`JIRA_BASE_URL`. Create a new unscoped token, keep it in a secret manager or hidden installer
input, validate it with `reviewer check --board-project jira=<PROJECT_KEY>`, then revoke the old
token after a successful rotation.
Validation checks `/rest/api/3/myself`, project visibility, and the permissions required by the
enabled lifecycle operations. See the official
[Jira Cloud REST API v3 introduction](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/).

## GitHub Issues

Set `GITHUB_ISSUES_TOKEN`; for GitHub Enterprise Server also set `GITHUB_ISSUES_API_BASE`
(`https://<host>/api/v3`). Create a fine-grained personal access token with `Issues: Read and
write` on the target repository — a classic token needs the `repo` scope — following
[GitHub's personal-access-token documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
The board is deliberately opt-in on its own variable: the review pipeline's `GITHUB_TOKEN` is
**not** an alias, so an existing deployment never silently gains an unconfigured issues board.
Required option `repo` (`owner/name`); `key_prefix` sets the synthetic task key (`WIDGETS-7`) since
GitHub has no human-readable issue key. Pull requests are filtered out of the issues listing.
Issue attachments cannot be downloaded through the API and live on third-party hosts, so they are
reported as metadata with a warning rather than indexed text. Rotate by issuing a replacement
token, updating the protected reviewer-mcp environment, running `reviewer check`, then revoking the
old token.

## Trello

Set `TRELLO_API_KEY` and `TRELLO_API_TOKEN`, optionally `TRELLO_API_BASE`. Both credentials are
treated as secrets: they travel in the query string of every request and together grant full
account access. Generate them from a Power-Up in the
[Trello developer admin](https://trello.com/apps/admin) — the API Key tab, then the adjacent Token
link — as described in the
[REST API introduction](https://developer.atlassian.com/cloud/trello/guides/rest-api/api-introduction/).
Required option `board_id`; `key_prefix` builds the task key from the card's short id. Trello has
no reliable server-side "modified since" filter, so the adapter paginates the board by creation
date, orders by `dateLastActivity`, and relies on the sync watermark for incrementality. Attachment
download needs an `Authorization: OAuth` header, and off-host link attachments are never fetched
(metadata plus warning) so credentials cannot leak to a third party. Rotate by generating a new
token, validating with `reviewer check`, then revoking the old one.

## Linear

Set `LINEAR_API_KEY`, optionally `LINEAR_API_BASE`. Create a personal API key with read and write
access (plus create-issue permission) in Linear's
[account security settings](https://linear.app/settings/account/security); see the
[API documentation](https://linear.app/developers/graphql). The key is sent as a bare
`Authorization` header **without** the `Bearer` prefix, and OAuth is not supported. Required option
`team_key` — the human-readable team key that also prefixes every issue `identifier`, resolved to a
team id through one filtered query. Keys are native (`ENG-123`), so no `key_prefix` is needed.
Done targets are the team's workflow states; a state of type `completed` or `canceled` is offered
as a done target, which keeps target selection independent of workspace language. Attachments are
private and require separate authorization, so they are reported as metadata with a warning.
Linear signals rate limiting as HTTP 400 with `RATELIMITED`, which the adapter maps to a retryable
category through the shared transport's `rate_limit_hint`.

## ClickUp

Set `CLICKUP_API_TOKEN`, optionally `CLICKUP_API_BASE`. Create a personal token (`pk_…`, no
expiry) under avatar → Settings → Apps as documented in
[ClickUp authentication](https://developer.clickup.com/docs/authentication); it is sent in
`Authorization` without a `Bearer` prefix. Required option `list_id`; `key_prefix` builds the task
key, and `team_id` is needed only when the workspace uses custom task ids. ClickUp stores
descriptions as plain text and returns markdown only when the request asks for it, so every read
sets `include_markdown_description=true` and every write uses `markdown_content`; a response
without `markdown_description` falls back to the plain field with a warning. Done targets are list
status names, which are also exactly what the write API accepts. Rotate by generating a new token,
validating with `reviewer check`, then deleting the old one.

## Asana

Set `ASANA_ACCESS_TOKEN`, optionally `ASANA_API_BASE`. Create a personal access token in the
[Asana developer console](https://app.asana.com/0/my-apps); see the
[personal-access-token documentation](https://developers.asana.com/docs/personal-access-token).
Required option `project_gid`; `key_prefix` builds the task key from the task `gid`. Descriptions
round-trip through `html_notes`: Asana's
[rich-text subset](https://developers.asana.com/docs/rich-text) has no `<p>` and no longer supports
`<br/>`, so the adapter maps markdown into the supported tag set and reads it back into markdown.
Closing a task sets the boolean `completed`; a `done_target` section is an additional board
placement reported separately. Attachment `download_url` values are short-lived signed URLs on
third-party storage and are fetched without the Asana credential. Rotate by creating a replacement
token, validating with `reviewer check`, then revoking the old one.

## Yandex Tracker

Set `YANDEX_TRACKER_TOKEN` plus exactly one organization header — `YANDEX_TRACKER_ORG_ID` for a
Yandex 360 organization or `YANDEX_TRACKER_CLOUD_ORG_ID` for a Yandex Cloud organization; the
adapter refuses to start with both or neither. `YANDEX_TRACKER_AUTH_SCHEME` selects `OAuth`
(default) or `Bearer` for a Yandex Cloud IAM token, and `YANDEX_TRACKER_API_BASE` overrides the
default `https://api.tracker.yandex.net/v3`. Follow the official
[API access guide](https://yandex.ru/support/tracker/ru/api-ref/access); a long-lived deployment
should prefer an OAuth token because an IAM token expires within 12 hours. Required option `queue`.
Keys are native (`TREK-123`), so no `key_prefix` is needed. Descriptions are Yandex Flavored
Markdown and are converted to markdown on read, while writes are sent with `markupType: md`.
Closing a task executes a workflow **transition** — the adapter never sets a status directly and
never uses the command DSL — so a missing transition yields a warning instead of bypassing the
workflow. Page-based enumeration is documented up to 10 000 issues per queue; larger queues would
need scroll-cursor support, which is not implemented.

## Kaiten

Set `KAITEN_BASE_URL` to the company address (`https://<company>.kaiten.ru`) and
`KAITEN_API_TOKEN`. The adapter appends the API suffix itself and accepts a self-hosted subpath.
Create a permanent API key in the user profile's API-key section; the developer reference is
[developers.kaiten.ru](https://developers.kaiten.ru/). There is no single cloud base URL, so the
base URL is a required non-secret credential rather than an option — the same shape as YouTrack.
Required option `board_id` (numeric); `key_prefix` builds the task key from the card id, and
`space_id` optionally narrows the listing. Done targets are board columns: a column of `type` 3 is
offered as done, so a localized column name never has to be guessed. Card descriptions are markdown
natively. Checklists become acceptance criteria while child cards become subtask links, because
checklist items are not tasks and would otherwise create dangling task stubs in the graph.
Attachments hosted on external storage are reported as metadata with a warning instead of being
fetched with the board credential.

## Weeek

Set `WEEEK_API_TOKEN`, optionally `WEEEK_API_BASE`. Create an access token in the workspace API
settings as described in the
[Weeek developer reference](https://developers.weeek.net/#generating-access-token); every request
acts as the token's creator, so use an account with access to the task project. Required options
`project_id` and `board_id`; `key_prefix` builds the task key from the numeric id. Descriptions are
HTML and are converted to markdown on read and back on write. Columns have no "done" type, so
completion is the task's `isCompleted` flag; moving the task to a done column is an additional
cosmetic step reported separately. Weeek's public API exposes no "modified since" filter, so a sync
walks every page and the watermark only saves indexing work. Attachment URLs for files stored in
Weeek expire after an hour, so the text is extracted during normalization rather than later.

Two behaviours are implemented against unconfirmed documentation and marked `TODO(weeek)` in the
adapter: the maximum `perPage` value, and whether `PUT /tm/tasks/{id}` accepts `description` — the
official request body omits it, third-party clients rely on it, and there is no comment endpoint to
use instead. The PR-link write is fail-soft: a rejection is reported as a warning and the task is
still closed.

## Legacy migration

Legacy configuration is read but never generated. For **one compatibility release** the resolver
maps `done_column` and `done_state` to `done_target`, and `status_field` to
`options.status_field`. It also reads `TASK_BOARD_API_KEY → YOUGILE_API_KEY` and
`TASK_BOARD_API_BASE → YOUGILE_API_BASE` only for YouGile; those aliases never configure another
provider. New generic fields win over a legacy value and the validator returns a warning identifying
the ignored legacy field.

The legacy fields will be removed **no earlier than the next breaking release**. Track the removal
in the documented [future breaking-cleanup task](#future-breaking-cleanup-task); do not delete the
compatibility path in this release.

### Future breaking-cleanup task

After the compatibility release has shipped and migration warnings have had their published window,
create the breaking-cleanup task to remove the legacy fields and their warnings. This follow-up is
intentionally local to this documentation until it is created on the team's board; no external key
or URL is implied.

## Adding a provider

Follow every item in order:

1. Implement the adapter with the complete `TaskBoardProvider` lifecycle.
2. Export its immutable spec, including credential, setup, validation, and option metadata.
3. Add one explicit registry line; there are no decorators, import side effects, or entry points.
4. Add a full contract fixture and run the shared lifecycle contract suite.
5. Add provider-specific tests for transport and format behaviour.
6. Add its documentation matrix row, setup, rotation, and migration notes as applicable.

Partial registration is forbidden. A provider without an immutable spec, explicit registry entry,
full contract fixture, provider-specific tests, and its documentation matrix row is not supported
and must be rejected rather than exposed to generic MCP or sync code.
