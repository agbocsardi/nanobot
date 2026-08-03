The scheduled time has arrived. Execute this scheduled cron job now and report the result to the user in the same session.

Rules:
- Use English by default. Use another language only when the live conversation that created this job is clearly in that language or the job explicitly requests it. For isolated scheduled runs, do not infer the language from the user's profile, name, or locale.
- Do not narrate internal progress.
- Do not include user IDs.
- Do not add status reports like "Done" or "Reminded" unless they are the natural response.

Cron job: {{ message }}
