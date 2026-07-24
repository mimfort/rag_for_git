# Board providers

This is the authoritative reference for the registered task-board providers. A board type is
supported only when its adapter is registered with the full lifecycle contract; the names below are
the current matrix, not a closed list of future choices.

## Capability matrix

| Capability | YouGile | YouTrack | Jira Cloud |
|---|---:|---:|---:|
| Sync/pagination | ✓ | ✓ | ✓ |
| Markdown normalization | HTML↔MD | Native MD | ADF↔MD |
| Links/subtasks | ✓ | ✓ | ✓ |
| Attachments | ✓ | ✓ | ✓ |
| Single read | ✓ | ✓ | ✓ |
| Discovery | ✓ | ✓ | ✓ |
| Create/target | ✓ | ✓ | ✓ |
| Finish/PR link | ✓ | ✓ | ✓ |
| Write-through | ✓ | ✓ | ✓ |

All adapters implement validation, full pagination, normalized reads, target discovery, create,
idempotent finish, and write-through reindexing. The registry exposes only configured types whose
credential schema is complete; secrets never leave the reviewer-mcp process.

## Configuration

Use the same shape for every registered provider. `type` is a registered provider key;
`create_target`, `done_target`, and `options` are resolved by discovery rather than by a
provider-specific playbook.

```yaml
task_board:
  type: <registered-provider>
  project: PRI
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
finish_task(key, pr_url, note=None, mark_done=True, board_type=None, target=None,
            provider_options=None)
```

Credentials are server-side environment variables, never values in `.review.yml`,
`provider_options`, a client MCP configuration, commits, previews, errors, or logs. Run
`reviewer check` after setup or rotation; it validates each configured provider and reports only
safe identity, project, permission, and configuration metadata.

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
input, validate it with `reviewer check`, then revoke the old token after a successful rotation.
Validation checks `/rest/api/3/myself`, project visibility, and the permissions required by the
enabled lifecycle operations. See the official
[Jira Cloud REST API v3 introduction](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/).

## Legacy migration

Legacy configuration is read but never generated. For **one compatibility release** the resolver
maps `done_column` and `done_state` to `done_target`, and `status_field` to
`options.status_field`. If both forms are present, the new generic field wins and the validator
returns a warning identifying the ignored legacy field.

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
