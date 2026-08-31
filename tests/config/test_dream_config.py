from nanobot.config.schema import DreamConfig


def test_dream_config_defaults_to_interval_hours() -> None:
    cfg = DreamConfig()

    assert cfg.interval_h == 2
    assert cfg.cron is None
    assert cfg.max_batch_size == 20
    assert cfg.max_iterations == 10
    assert cfg.timeout_s == 300


def test_dream_config_builds_every_schedule_from_interval() -> None:
    cfg = DreamConfig(interval_h=3)

    schedule = cfg.build_schedule("UTC")

    assert schedule.kind == "every"
    assert schedule.every_ms == 3 * 3_600_000
    assert schedule.expr is None


def test_dream_config_honors_legacy_cron_override() -> None:
    cfg = DreamConfig.model_validate({"cron": "0 */4 * * *"})

    schedule = cfg.build_schedule("UTC")

    assert schedule.kind == "cron"
    assert schedule.expr == "0 */4 * * *"
    assert schedule.tz == "UTC"
    assert cfg.describe_schedule() == "cron 0 */4 * * * (legacy)"


def test_dream_config_dump_uses_interval_h_and_hides_legacy_cron() -> None:
    cfg = DreamConfig.model_validate({"intervalH": 5, "cron": "0 */4 * * *"})

    dumped = cfg.model_dump(by_alias=True)

    assert dumped["intervalH"] == 5
    assert "cron" not in dumped


def test_dream_config_uses_model_override_name_and_accepts_legacy_model() -> None:
    cfg = DreamConfig.model_validate({"model": "openrouter/sonnet"})

    dumped = cfg.model_dump(by_alias=True)

    assert cfg.model_override == "openrouter/sonnet"
    assert dumped["modelOverride"] == "openrouter/sonnet"
    assert "model" not in dumped


def test_dream_config_guardrail_limits_defaults() -> None:
    cfg = DreamConfig()

    assert cfg.max_changed_files == 8
    assert cfg.max_diff_chars == 32_000


def test_dream_config_accepts_camel_case_guardrail_aliases() -> None:
    cfg = DreamConfig.model_validate({"maxChangedFiles": 4, "maxDiffChars": 500})

    assert cfg.max_changed_files == 4
    assert cfg.max_diff_chars == 500
    dumped = cfg.model_dump(by_alias=True)
    assert dumped["maxChangedFiles"] == 4
    assert dumped["maxDiffChars"] == 500


def test_dream_config_back_compatible_defaults_for_old_configs() -> None:
    # A config written before the guardrail fields existed still parses.
    cfg = DreamConfig.model_validate({"intervalH": 3, "maxBatchSize": 10})

    assert cfg.max_changed_files == 8
    assert cfg.max_diff_chars == 32_000
