from src.model_catalog import matches_model_filters
from src.models import ModelOption


def test_price_capability_and_release_year_filters_compose() -> None:
    model = ModelOption(
        "vendor/model",
        "Model",
        prompt_price=0.0000001,
        completion_price=0.0000003,
        created=1735689600,
        artificial_analysis_rank=8,
    )

    assert matches_model_filters(
        model,
        max_price_per_million=0.5,
        max_artificial_analysis_rank=10,
        release_year=2025,
    )
    assert not matches_model_filters(model, max_price_per_million=0.1)
    assert not matches_model_filters(model, max_artificial_analysis_rank=5)
    assert not matches_model_filters(model, release_year=2024)
