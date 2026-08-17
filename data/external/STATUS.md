# Nordstrom Fragrance Collection — STATUS

**Result:** FAILED — collection did not run. No data collected.
**UTC timestamp:** 2026-08-17T08:36:46Z

## Outcome summary

| Item | Value |
|------|-------|
| Records returned | 0 |
| Credits/records consumed | 0 (of 2,500 hard cap; ~9 previously used on free tier) |
| Snapshot triggered | none |
| Data file (`nordstrom-fragrance.json`) | omitted — collection failed |

## Failure

The collection could not start because outbound access to the Bright Data API
is blocked by this session's organization egress policy.

- **Blocked host:** `api.brightdata.com:443`
- **Symptom:** HTTPS CONNECT tunnel refused by the policy-enforcing egress proxy.
- **Exact error (curl):** `curl: (56) CONNECT tunnel failed, response 403` → HTTP code `000`.
- **Exact proxy-side reason** (from `GET $HTTPS_PROXY/__agentproxy/status`,
  `recentRelayFailures`):

  ```json
  {
    "ts": "2026-08-17T08:35:30.341Z",
    "kind": "connect_rejected",
    "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
    "host": "api.brightdata.com:443"
  }
  ```

Per the egress proxy README and the run's cost/safety rules, a 403 from the
egress proxy is an organization policy denial and must **not** be retried or
routed around. Collection was therefore stopped at the sanity-check step,
before any trigger was fired — so no snapshot was created and no free-tier
credits were spent.

## Dataset / snapshot identifiers

Not resolved. Because the Bright Data API was unreachable, the snapshot list
could not be retrieved and the target dataset could not be identified.

- Intended reference snapshot: `sd_mswxt38p26yq3bmzkg` (per task spec) — not verified.
- `dataset_id`: unknown (could not be read; API blocked).

## Validation stats

Not applicable — no data file was produced.

## What was verified before stopping

1. `BRIGHT_DATA_API_KEY` is present in the environment (value never printed/logged).
2. Bright Data reachability probe (`GET /datasets/v3/snapshots`): **403 at the
   egress proxy** (blocked).
3. Delivery repo `rusa3046/TestFragProj` is reachable and credentialed via the
   proxy; this branch was created from `origin/main`.

## Note on run assumptions

The task assumed a pre-existing local clone of `rusa3046/TestFragProj` with
push credentials in the working directory. No such clone was present; the
working directory was empty. The repo was reachable over HTTPS through the
session's git proxy, so it was cloned fresh from `origin/main` to deliver this
status. Only `data/external/STATUS.md` was added; `data/corpus/` and
`data/curation/` were not touched.

## To unblock

Add `api.brightdata.com` to this session's allowed egress hosts (organization
network policy), then re-run the collection. No code or credential change is
needed on the Bright Data side.
