import json

import pytest

from src.config import NUM_CONTACTS, Config


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / "config.json")


def test_fresh_config_has_six_empty_slots(config):
    contacts = config.contacts()
    assert len(contacts) == NUM_CONTACTS
    assert [c["slot"] for c in contacts] == list(range(1, NUM_CONTACTS + 1))
    assert all(not c["enabled"] for c in contacts)


def test_unassigned_slot_returns_none(config):
    assert config.contact(4) is None


def test_set_and_read_a_contact(config):
    config.set_contact(4, "Grandma", "+447700900123")
    entry = config.contact(4)
    assert entry is not None
    assert entry["name"] == "Grandma"
    assert entry["number"] == "+447700900123"


@pytest.mark.parametrize("slot", [0, 10, -1])
def test_out_of_range_slots_are_rejected(config, slot):
    with pytest.raises(ValueError):
        config.set_contact(slot, "X", "+441")


def test_contact_with_no_number_is_never_enabled(config):
    config.set_contact(2, "Nobody", "", enabled=True)
    assert config.contact(2) is None


def test_disabled_contact_is_inert(config):
    config.set_contact(6, "Dad", "+447700900999", enabled=False)
    assert config.contact(6) is None
    assert config.slot_for_number("+447700900999") is None


def test_inbound_number_maps_back_to_its_slot(config):
    config.set_contact(5, "Mum", "+447700900555")
    assert config.slot_for_number("+447700900555") == 5


def test_number_matching_ignores_formatting(config):
    """Signal may report a number differently from how a parent typed it."""
    config.set_contact(1, "Nana", "+44 7700 900555")
    assert config.slot_for_number("+447700900555") == 1
    assert config.slot_for_number("447700900555") == 1


def test_unknown_number_maps_to_nothing(config):
    config.set_contact(1, "Nana", "+447700900555")
    assert config.slot_for_number("+15550000000") is None


def test_settings_persist_across_reload(tmp_path):
    path = tmp_path / "config.json"
    first = Config(path)
    first.set_contact(3, "Grandad", "+447700900777")
    first.set(45, "behaviour", "selection_timeout_seconds")

    second = Config(path)
    assert second.contact(3)["name"] == "Grandad"
    assert second.get("behaviour", "selection_timeout_seconds") == 45


def test_new_defaults_appear_after_an_upgrade(tmp_path):
    """An old config missing a key must pick up the new default."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"contacts": [], "audio": {"ringtone": "mine.wav"}}))

    config = Config(path)
    assert config.get("audio", "ringtone") == "mine.wav"     # kept
    assert config.get("audio", "max_record_seconds") == 60   # filled in
    assert len(config.contacts()) == NUM_CONTACTS            # rebuilt


def test_legacy_quiet_time_labels_are_renamed_on_load(tmp_path):
    """Boxes set up before the "Slot N" rename keep "School"/"Nap"/"Bedtime"
    forever otherwise - quiet_times is a list, and the default-merge
    replaces lists wholesale rather than filling in missing keys."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "quiet_times": [
            {"id": "school", "label": "School", "enabled": True,
             "start": "09:00", "end": "15:15", "days": [0, 1, 2, 3, 4]},
            {"id": "nap", "label": "Nap", "enabled": False,
             "start": "13:00", "end": "14:30", "days": [0, 1, 2, 3, 4, 5, 6]},
            {"id": "bedtime", "label": "Bedtime", "enabled": False,
             "start": "19:00", "end": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]},
        ],
    }))

    config = Config(path)
    labels = [w["label"] for w in config.get("quiet_times")]
    assert labels == ["Slot 1", "Slot 2", "Slot 3"]
    # Nothing else about the window was touched.
    assert config.get("quiet_times")[0]["enabled"] is True

    on_disk = json.loads(path.read_text())
    assert [w["label"] for w in on_disk["quiet_times"]] == ["Slot 1", "Slot 2", "Slot 3"]


def test_a_parent_renamed_quiet_time_is_left_alone(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "quiet_times": [
            {"id": "school", "label": "Piano practice", "enabled": False,
             "start": "09:00", "end": "15:15", "days": [0, 1, 2, 3, 4]},
        ],
    }))

    config = Config(path)
    assert config.get("quiet_times")[0]["label"] == "Piano practice"


def test_corrupt_config_falls_back_instead_of_bricking(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ this is not json")

    config = Config(path)

    assert len(config.contacts()) == NUM_CONTACTS
    assert path.with_suffix(".json.broken").exists()


def test_clear_contact_empties_the_slot(config):
    last = NUM_CONTACTS
    config.set_contact(last, "Auntie", "+447700900321")
    config.clear_contact(last)
    assert config.contact(last) is None
    assert config.contacts()[last - 1]["name"] == ""
