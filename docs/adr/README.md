# ADRs

A decision goes here when (a) someone could disagree with it for good reasons and (b) there is evidence — a test, a number, a diff — that it was actually made and not just written down.

| # | Decision | Evidence |
|---|---|---|
| [0001](0001-ownership-by-operation.md) | Ownership: the session to consent, the customer for everything else | isolation tests in `test_control_plane.py`, `borrowed_token` cell |
| [0002](0002-storage-seam.md) | One `Store`, two implementations, synchronous | empty `git diff` on the 33 existing tests; 8 integration tests |
| [0003](0003-confirmation-ttl-and-digest.md) | A confirmation expires; the action is sealed with an HMAC | `expired_yes` and `mutated_action` cells, removing each check turns its cell red |
| [0004](0004-agentcore-runtime-sa-east-1.md) | AgentCore Runtime in `sa-east-1` on an in-region model; pools check connections on checkout | smoke turn on the deployed runtime, `test_pg_pool.py`, `test_trace_links.py` |

## Open decisions

None of these can be written today without inventing a number, and an ADR that points at an invented number is worse than no ADR.

- **Which model extracts the typed action.** Needs a frozen golden set and a benchmark across real candidate models.
- **Bedrock or an external model API.** Needs the same benchmark, plus latency and cost measured from `sa-east-1`.
- **This control plane or AgentCore Policy.** Needs AgentCore Gateway with Cedar policies in log-only mode, mirroring the payment action, compared against this plane's decisions.
- **Prompts and outputs in CloudWatch spans.** Keep, redact or drop customer content in traces (see ADR 0004).
