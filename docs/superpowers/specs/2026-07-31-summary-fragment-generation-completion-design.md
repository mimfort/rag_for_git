# Cluster-specific summary fragment generation completion

## Goal

Make capped first-upgrade bootstrap and depth-change rebuilds converge across
multiple passes without regenerating fragments for clusters already completed at
the current effective depth.

## Completion evidence

Every newly generated file fragment receives server-owned provenance:

```json
{
  "_reviewer": {
    "generation": "summary-fragment-v1",
    "layout_token": "<canonical-sha256>",
    "depth": 2
  }
}
```

The MCP service always overwrites a client-provided `_reviewer` value before
calling the store. Existing user/model provenance keys remain unchanged.

A cluster is complete for the current generation only when every current path
has a same-cluster stored fragment with:

- the exact current file fingerprint;
- `_reviewer.generation == "summary-fragment-v1"`;
- `_reviewer.layout_token == effective_layout_token`;
- `_reviewer.depth == effective_depth`.

Unstamped, wrong-depth, cross-cluster, missing, or stale-fingerprint fragments
do not prove completion.

## Cluster-specific state

Global `completed_layout` records the canonical identity of the fully completed
branch pass; `completed_depth` remains diagnostic. The token covers default depth
and normalized/sorted depth overrides. A legacy state row without
`completed_layout` is incomplete. While layout completion is missing, a cluster
is `bootstrap=true` only until that cluster has
complete current-generation evidence. While completed layout differs from the
effective token, a cluster is `full_rebuild=true` only until that cluster has complete
current-generation evidence.

Pending bootstrap/full-rebuild applies to the entire incomplete cluster, so all
of its current files are regenerated once. Completed clusters switch to normal
delta classification, reuse their fragments, and leave the capped rebuild set
even before prune records global `completed_depth`.

`stale` remains cluster work freshness: an incomplete depth rebuild is stale;
after its bundle is stored at the current hash/depth it becomes fresh. Bootstrap
remains a separate flag because a legacy summary hash may already match.

## Multi-pass flow

With `cap=1` and multiple incomplete clusters:

1. The first list returns one cluster and defers the rest.
2. Persisting that cluster stamps all generated fragments at effective depth.
3. The next list excludes the completed cluster from bootstrap/full-rebuild
   work and selects the next incomplete cluster.
4. After the last cluster persists, the next list reports `deferred=0`; no
   completed cluster has pending file jobs.
5. The skill may then pass the exact list `layout_token` and cluster source hashes
   to verified prune. Service re-derives them; store validates every summary and
   exact fragment coverage under the branch advisory lock before deleting orphans
   and finalizing `completed_depth + completed_layout`.

## Compatibility and failure behavior

- Existing unstamped fragments bootstrap once and are not trusted as completion
  evidence.
- Ordinary incremental reuse continues after global depth completion.
- Client provenance cannot spoof generation completion because `_reviewer` is
  overwritten server-side.
- Optimistic source-hash/fingerprint validation remains unchanged.
- Schema migration idempotently adds nullable `completed_layout`; existing rows
  remain intentionally incomplete until a verified full pass.

## Tests

- First-upgrade bootstrap with multiple fresh legacy summaries and `cap=1`
  converges across passes; a completed cluster is not selected or reread.
- Depth-change rebuild with multiple clusters and `cap=1` converges across
  passes; a completed cluster is not regenerated while global depth remains old.
- Persistence overwrites spoofed client `_reviewer` provenance with the
  effective server depth/generation.
