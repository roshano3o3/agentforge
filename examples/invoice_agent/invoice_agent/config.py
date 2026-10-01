"""How this build of the invoice agent behaves.

`invoice_agent.adapter:answer` -- the adapter the CI release gate evaluates --
runs exactly this. The gate evaluates the base branch's copy of this file
and the pull request's copy, so changing a setting here is a code change the
gate judges. (The fixed `answer_v1` / `answer_v2` presets ignore this file.)
"""

from invoice_agent.agent import Behavior

BEHAVIOR = Behavior(
    approval_exempt_under_usd=0.0,
    approval_check="value",
    amount_as_string=False,
    lookup_attempts=1,
    check_status_before_reminder=True,
    void_tool="void_invoice",
)
