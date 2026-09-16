from app.rotational_features import RotationalProfile, RotationalProfilePoint
from app.turning_simulation import (
    TurningSimulationMetrics,
    TurningSimulationResult,
    TurningStockSample,
)
from app.turning_verification import verify_turning_profile


def profile(side: str = "outer") -> RotationalProfile:
    return RotationalProfile(
        id="RP-VERIFY",
        axis_id="RA-1",
        side=side,
        extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-10, radius=5),
            RotationalProfilePoint(z=0, radius=5),
        ],
        confidence=1,
        review_state="accepted",
    )


def simulation(*, outer: float = 5, inner: float = 0) -> TurningSimulationResult:
    return TurningSimulationResult(
        status="completed",
        resolution_mm=5,
        samples=[
            TurningStockSample(z=z_value, outer_radius=outer, inner_radius=inner)
            for z_value in (-10, -5, 0)
        ],
        metrics=TurningSimulationMetrics(
            initial_volume_mm3=1000,
            remaining_volume_mm3=500,
            removed_volume_mm3=500,
            removal_percent=50,
        ),
    )


def test_outer_profile_verification_distinguishes_pass_excess_and_overcut() -> None:
    passed = verify_turning_profile(profile(), simulation(outer=5.02), tolerance_mm=0.05)
    excess = verify_turning_profile(profile(), simulation(outer=5.2), tolerance_mm=0.05)
    allowed = verify_turning_profile(
        profile(), simulation(outer=5.2), tolerance_mm=0.05, expected_allowance_mm=0.2,
    )
    overcut = verify_turning_profile(profile(), simulation(outer=4.8), tolerance_mm=0.05)

    assert passed.status == "passed"
    assert excess.status == "warning"
    assert excess.metrics.excess_stock_sample_count == 3
    assert excess.metrics.estimated_excess_stock_volume_mm3 > 0
    assert allowed.status == "passed"
    assert overcut.status == "failed"
    assert overcut.metrics.overcut_sample_count == 3
    assert overcut.metrics.estimated_overcut_volume_mm3 > 0


def test_inner_profile_reverses_material_side_classification() -> None:
    excess = verify_turning_profile(profile("inner"), simulation(outer=10, inner=4.7), tolerance_mm=0.05)
    overcut = verify_turning_profile(profile("inner"), simulation(outer=10, inner=5.2), tolerance_mm=0.05)

    assert excess.status == "warning"
    assert excess.metrics.maximum_excess_stock_mm == 0.3
    assert overcut.status == "failed"
    assert overcut.metrics.maximum_overcut_mm == 0.2


def test_profile_verification_ignores_stock_outside_profile_z_extent() -> None:
    result = verify_turning_profile(
        profile(),
        TurningSimulationResult(
            status="completed",
            resolution_mm=5,
            samples=[
                TurningStockSample(z=-20, outer_radius=10, inner_radius=0),
                TurningStockSample(z=-10, outer_radius=5, inner_radius=0),
                TurningStockSample(z=0, outer_radius=5, inner_radius=0),
                TurningStockSample(z=10, outer_radius=10, inner_radius=0),
            ],
            metrics=TurningSimulationMetrics(
                initial_volume_mm3=1000, remaining_volume_mm3=500,
                removed_volume_mm3=500, removal_percent=50,
            ),
        ),
    )

    assert result.status == "passed"
    assert result.metrics.evaluated_sample_count == 2
