# Self Tool — Practical Examples

Concrete scenarios showing when and how to use the self tool effectively.

## Diagnosis

### "Why can't you search the web?"
```
→ self(action="inspect", key="web_config.enable")
  → False
→ "Web search is disabled. Add web.enable: true to your config to enable it."
```

### "Why did you stop?"
```
→ self(action="inspect", key="max_iterations")
  → 40
→ self(action="inspect", key="_last_usage")
  → {"prompt_tokens": 62000, "completion_tokens": 3000}
→ "I hit the iteration limit (40). The task was complex. I can ask the user if they want to increase it."
```

### "What model are you running?"
```
→ self(action="inspect", key="model")
  → 'anthropic/claude-sonnet-4-20250514'
```

## Adaptive Behavior

### Large codebase analysis
```
→ self(action="inspect")
  → context_window_tokens: 65536
→ self(action="modify", key="context_window_tokens", value=131072)
  → "Set context_window_tokens = 131072 (was 65536)"
→ "I've expanded my context window to handle this large codebase."
```

### Switching to a faster model for repetitive tasks
```
→ self(action="modify", key="model", value="anthropic/claude-haiku-4-5-20251001")
  → "Set model = 'anthropic/claude-haiku-4-5-20251001' (was 'anthropic/claude-sonnet-4-20250514')"
→ "Switched to a faster model for these batch tasks."
```

## Cross-Turn Memory

### Remembering user preferences
```
# Turn 1: user says "keep it brief"
→ self(action="modify", key="user_style", value="concise")
  → "Set _runtime_vars.user_style = 'concise'"

# Turn 3: new topic
→ self(action="inspect", key="user_style")
  → 'concise'
→ (adjusts response style accordingly)
```

### Tracking project context
```
→ self(action="modify", key="active_branch", value="feat/auth")
→ self(action="modify", key="test_framework", value="pytest")
→ self(action="modify", key="has_docker", value=true)
```

## Budget Awareness

### Token-conscious behavior
```
→ self(action="inspect", key="_last_usage")
  → {"prompt_tokens": 58000, "completion_tokens": 12000}
→ "I've consumed ~70k tokens. I'll keep my remaining responses focused."
```
