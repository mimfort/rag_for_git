Standalone runs may additionally render the findings as a readable list after the JSON.

If a finding cannot be tied to a specific line, use the closest changed line and
explain the scope in `message`.

If there are no meaningful findings, return `{"findings": []}` and say so.

Write `message` and `suggestion` in the output language given by the orchestrator
(standalone: the user's language).