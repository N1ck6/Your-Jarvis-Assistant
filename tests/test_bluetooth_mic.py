"""Bluetooth headphones become the default microphone when they connect: Jarvis keeps the desk microphone, otherwise
the headphones switch into the hands-free ("call") profile — mono sound, a stuck volume, music cut on mute."""
import pytest

from assistant.audio import player

MME, WASAPI = 0, 2


def dev(name, hostapi, inputs, outputs=0):
    return {"name": name, "hostapi": hostapi, "max_input_channels": inputs, "max_output_channels": outputs}


DEVICES = [
    dev("Переназначение звуковых устр. - Input", MME, 2),
    dev("Микрофон (fifine Microphone)", MME, 2),
    dev("Головной телефон (Galaxy Buds2 ", MME, 1),
    dev("Микрофон (Realtek(R) Audio)", MME, 2),
    dev("Наушники (Galaxy Buds2 Stereo)", MME, 0, 2),
    dev("Головной телефон (Galaxy Buds2 Hands-Free AG Audio)", WASAPI, 1),
    dev("Микрофон (fifine Microphone)", WASAPI, 2),
]


@pytest.fixture
def devices(monkeypatch):
    state = {"default": 2}

    def query(device=None, kind=None):
        if kind == "input":
            return DEVICES[state["default"]]
        return DEVICES if device is None else DEVICES[device]

    monkeypatch.setattr(player.sd, "query_devices", query)
    monkeypatch.setattr(player, "last_good_input", "")
    return state


def test_hands_free_names():
    names = [d["name"] for d in DEVICES]
    assert player.is_hands_free("Головной телефон (Galaxy Buds2 ", names)       # cut by MME, full name decides
    assert player.is_hands_free("Headset (WH-1000XM4 Hands-Free AG Audio)")
    assert not player.is_hands_free("Наушники (Galaxy Buds2 Stereo)", names)
    assert not player.is_hands_free("Микрофон (fifine Microphone)", names)


def test_headset_default_is_skipped_for_the_desk_mic(devices):
    assert player.resolve_device("", "input") == 1       # the first real microphone, not the mapper


def test_the_mic_used_before_is_kept(devices):
    player.last_good_input = "Микрофон (Realtek(R) Audio)"
    assert player.resolve_device("", "input") == 3


def test_ordinary_default_is_left_alone(devices):
    devices["default"] = 1
    assert player.resolve_device("", "input") is None     # None = the system default as is


def test_headset_is_used_when_it_is_the_only_mic(monkeypatch):
    only = [dev("Головной телефон (Galaxy Buds2 Hands-Free AG Audio)", MME, 1)]
    monkeypatch.setattr(player.sd, "query_devices",
                        lambda device=None, kind=None: only[0] if kind == "input" else only)
    assert player.resolve_device("", "input") is None


def test_explicit_choice_wins(devices):
    assert player.resolve_device("Galaxy Buds2", "input") == 2
