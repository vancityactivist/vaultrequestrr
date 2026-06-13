import logging

from vaultrequestrr import logbuffer


def test_install_captures_records():
    logbuffer.install()
    logging.getLogger("vaultrequestrr.unit").warning("marker-%s", 123)
    messages = [r.message for r in logbuffer.get_records()]
    assert any("marker-123" in m for m in messages)


def test_records_include_level_and_name():
    logbuffer.install()
    logging.getLogger("vaultrequestrr.unit").error("boom")
    rec = next(r for r in reversed(logbuffer.get_records()) if r.message == "boom")
    assert rec.level == "ERROR"
    assert rec.name == "vaultrequestrr.unit"


def test_install_is_idempotent():
    h1 = logbuffer.install()
    h2 = logbuffer.install()
    assert h1 is h2
    # only one ring-buffer handler attached to root
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, logbuffer.RingBufferHandler)]
    assert len(handlers) == 1
