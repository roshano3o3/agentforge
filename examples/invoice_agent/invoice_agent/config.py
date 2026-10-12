"""How this build of the invoice agent behaves.

`invoice_agent.adapter:answer` -- the adapter the CI release gate evaluates --
runs exactly this. The gate evaluates the base branch's copy of this file
and the pull request's copy, so changing a setting here is a code change the
gate judges. (The fixed `answer_v1` / `answer_v2` presets ignore this file.)
"""

from invoice_agent.agent import V1

BEHAVIOR = V1
