from src.model_catalog import filter_model_entries

ENTRY = {"id": "vendor/model", "label": "Model", "description": "", "price": 0.4, "rank": 8, "year": 2025, "textCompatible": True}
SAVED = {"id": "saved/model", "label": "saved/model", "description": "", "price": None, "rank": None, "year": None, "textCompatible": True}


def test_price_rank_and_release_year_filters_compose() -> None:
    assert filter_model_entries([ENTRY], "", True, 0.5, 10, 2025) == [ENTRY]
    assert filter_model_entries([ENTRY], "", True, 0.1, 0, 0) == []
    assert filter_model_entries([ENTRY], "", True, -1, 5, 0) == []
    assert filter_model_entries([ENTRY], "", True, -1, 0, 2024) == []


def test_disabled_filters_keep_entries_without_catalog_metadata() -> None:
    assert filter_model_entries([ENTRY, SAVED], " SAVED ", True, -1, 0, 0) == [SAVED]
    assert filter_model_entries([ENTRY, {**SAVED, "textCompatible": False}], "", True, -1, 0, 0) == [ENTRY]
    assert filter_model_entries([SAVED], "", True, 1.0, 0, 0) == []
