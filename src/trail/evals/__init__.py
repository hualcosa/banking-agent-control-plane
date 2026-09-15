"""The eval harness: a golden set, driven over the interface a human drives.

Four modules and one rule between them. ``cases`` is the vocabulary — a case, a
turn as it came off the wire, a finding. ``runner`` drives the agent's HTTP
endpoint and produces one outcome per case. ``metrics`` turns outcomes into
numbers and compares them against bars the *example* registered, not bars this
package invented. ``report`` renders them.

The rule is that none of it imports the agent. The harness speaks HTTP to
``POST /threads/{id}/turns/stream`` — the same endpoint the CLI and the browser
call — because a harness with its own entry point measures a system that does
not exist in production.
"""
