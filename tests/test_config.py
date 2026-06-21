import pytest

from vaultrequestrr.config import Config, ConfigError, _int_list


def _set_required(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("SEERR_URL", "http://seerr:5055")
    monkeypatch.setenv("SEERR_API_KEY", "k")


def test_int_list_parses_and_trims():
    assert _int_list("111, 222 ,333") == (111, 222, 333)


def test_int_list_handles_blank_and_none():
    assert _int_list("") == ()
    assert _int_list(None) == ()
    assert _int_list(" , ,") == ()


def test_int_list_rejects_garbage():
    with pytest.raises(ConfigError):
        _int_list("111,notanid")


def test_anime_routing_unset_defaults_to_none(monkeypatch):
    _set_required(monkeypatch)
    for var in (
        "ANIME_SONARR_SERVER_ID", "ANIME_SONARR_PROFILE_ID", "ANIME_SONARR_ROOT_FOLDER",
        "ANIME_RADARR_SERVER_ID", "ANIME_RADARR_PROFILE_ID", "ANIME_RADARR_ROOT_FOLDER",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = Config.from_env()

    assert cfg.anime_sonarr_server_id is None
    assert cfg.anime_radarr_server_id is None
    assert cfg.anime_sonarr_root_folder is None


def test_anime_routing_parses_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("ANIME_SONARR_SERVER_ID", "2")
    monkeypatch.setenv("ANIME_SONARR_PROFILE_ID", "7")
    monkeypatch.setenv("ANIME_SONARR_ROOT_FOLDER", "/tv/anime")
    monkeypatch.setenv("ANIME_RADARR_SERVER_ID", "3")

    cfg = Config.from_env()

    assert cfg.anime_sonarr_server_id == 2
    assert cfg.anime_sonarr_profile_id == 7
    assert cfg.anime_sonarr_root_folder == "/tv/anime"
    assert cfg.anime_radarr_server_id == 3
