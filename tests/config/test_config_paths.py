from pathlib import Path

from nanobot.config.paths import (
    get_cli_history_path,
    get_cron_dir,
    get_data_dir,
    get_legacy_sessions_dir,
    get_logs_dir,
    get_media_dir,
    get_runtime_subdir,
    get_workspace_path,
    is_default_workspace,
)


def test_runtime_dirs_follow_config_path(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance-a" / "config.json"
    monkeypatch.setattr("nanobot.config.paths.get_config_path", lambda: config_file)

    assert get_data_dir() == config_file.parent
    assert get_runtime_subdir("cron") == config_file.parent / "cron"
    assert get_cron_dir() == config_file.parent / "cron"
    assert get_logs_dir() == config_file.parent / "logs"


def test_media_dir_supports_channel_namespace(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance-b" / "config.json"
    monkeypatch.setattr("nanobot.config.paths.get_config_path", lambda: config_file)

    assert get_media_dir() == config_file.parent / "media"
    assert get_media_dir("telegram") == config_file.parent / "media" / "telegram"


def test_shared_and_legacy_paths_remain_global() -> None:
    assert get_cli_history_path() == Path.home() / ".nanobot" / "history" / "cli_history"
    assert get_legacy_sessions_dir() == Path.home() / ".nanobot" / "sessions"


def test_workspace_path_is_explicitly_resolved() -> None:
    assert get_workspace_path() == Path.home() / ".nanobot" / "workspace"
    assert get_workspace_path("~/custom-workspace") == Path.home() / "custom-workspace"


def test_is_default_workspace_distinguishes_default_and_custom_paths() -> None:
    assert is_default_workspace(None) is True
    assert is_default_workspace(Path.home() / ".nanobot" / "workspace") is True
    assert is_default_workspace("~/custom-workspace") is False


def test_config_path_context_scopes_get_config_path(tmp_path: Path) -> None:
    from nanobot.config.loader import config_path_context, get_config_path

    instance = tmp_path / "instance-a" / "config.json"
    before = get_config_path()
    with config_path_context(instance):
        assert get_config_path() == instance
    assert get_config_path() == before


def test_concurrent_config_path_contexts_are_isolated(tmp_path: Path) -> None:
    """Overlapping scopes must not observe each other's instance path."""
    import asyncio

    from nanobot.config.loader import config_path_context, get_config_path

    path_a = tmp_path / "a" / "config.json"
    path_b = tmp_path / "b" / "config.json"

    async def scoped(
        label: str,
        path: Path,
        entered: asyncio.Event,
        resume: asyncio.Event,
    ) -> tuple[str, Path]:
        with config_path_context(path):
            entered.set()
            await resume.wait()
            return label, get_config_path()

    async def main():
        a_entered, b_entered = asyncio.Event(), asyncio.Event()
        task_a = asyncio.create_task(scoped("a", path_a, a_entered, b_entered))
        task_b = asyncio.create_task(scoped("b", path_b, b_entered, a_entered))
        return await asyncio.gather(task_a, task_b)

    (label_a, seen_a), (label_b, seen_b) = asyncio.run(main())
    assert (label_a, seen_a) == ("a", path_a)
    assert (label_b, seen_b) == ("b", path_b)
