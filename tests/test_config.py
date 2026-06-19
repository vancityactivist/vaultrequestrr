import pytest

from vaultrequestrr.config import ConfigError, _int_list


def test_int_list_parses_and_trims():
    assert _int_list("111, 222 ,333") == (111, 222, 333)


def test_int_list_handles_blank_and_none():
    assert _int_list("") == ()
    assert _int_list(None) == ()
    assert _int_list(" , ,") == ()


def test_int_list_rejects_garbage():
    with pytest.raises(ConfigError):
        _int_list("111,notanid")
