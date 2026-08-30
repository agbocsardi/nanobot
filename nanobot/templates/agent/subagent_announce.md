[Subagent '{{ label }}' {{ status_text }}]

Task: {{ task }}

Result:
{{ result }}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs.

{% if record_path %}Run record: {{ record_path }}

Internal note: the persisted run log (tool calls with args and previews, token usage) is in that file. Read it when you need details. Keep the path and its contents out of the user-facing summary.{% endif %}
