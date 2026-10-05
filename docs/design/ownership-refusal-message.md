# Public ownership refusal messages

An ownership refusal contains an authored public message and a device ID.
Public callers read `public_message`. Exception diagnostics use a fixed message.
The public message does not depend on `args`, `str`, `repr`, or chained causes.
Existing permission checks, refusal status codes, redirects, and transaction
rollback stay in the same callers.

The blind designs agreed on an explicit public field. GPT-6.1 Sol (high)
proposed a fixed diagnostic message. The merged design uses that message and
updates both the HTTP action and the link-role result. Astra (xhigh) ratified
revision r1 with no blocking findings. It required real HTTP and ORM tests and
an actual CodeQL scan.

CodeQL 2.27.1 with python-queries 1.8.11 reproduces one baseline
`py/stack-trace-exposure` alert in the HTTP refusal response. The same query
flags diagnostic rendering in a minimal Django example and accepts the public
field. The field separates public text from diagnostics; it does not sanitize
tracebacks or authorize rendering other exception data.

The local OpenGrep rule covers direct `str`, `repr`, and `.args` reads inside
bare or module-qualified ownership refusal handlers. It permits the public
field and unrelated exception handlers. It does not track aliases or helper
functions. CodeQL remains enabled for the complete HTTP data flow.
