from app.catalogs import get_tool
from app.l32_back_live_face import BackLiveFaceDraftRequest, compile_back_live_face_draft
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.operation_library import create_operation_instance


def _snapshot(with_back_live: bool = True):
    return snapshot_l32_instance(MachineInstance(
        id="l32-back-live-test",
        definition_id="citizen-cincom-l32",
        name="L32 back live test",
        serial_number="BACK-LIVE-001",
        variant="VIII",
        controller_revision="M70LPC-VU-site-1",
        operation_mode="guide_bushing",
        installed_modules=["U151B"] if with_back_live else [],
        bar_diameter_mm=32,
    ))


def _operation():
    return create_operation_instance(
        id="OP50-ALT", sequence=50, type="back_live_face_finishing",
        name="背面动力刀具端面精加工", channel_id="sub", spindle_id="sub",
        workpiece_side="back", synchronization_group="TRANSFER-1",
        feature_ids=["RP-OUTER-1-BACK-DATUM"], tool=get_tool("EM-1"),
        parameters={
            "stock_allowance_mm": 0.25, "step_over_mm": 0.55,
            "axial_clearance_mm": 1.0, "spindle_rpm": 6000,
            "feed_rate_mm_min": 1080, "plunge_rate_mm_min": 324,
        },
        rationale=["test"], confidence=0.8,
    )


def test_back_live_face_generates_bounded_sub_channel_raster() -> None:
    result = compile_back_live_face_draft(
        BackLiveFaceDraftRequest(
            machine_instance_id="l32-back-live-test", operation=_operation(),
            stock_radius_mm=2.815, axial_stock_mm=0.25,
        ),
        _snapshot(),
    )

    commands = result.toolpath.channels[0].commands
    cutting = [
        item for item in commands
        if item.type == "feed_move" and item.parameters.get("cut_side") == "facing"
    ]
    assert result.status == "passed"
    assert result.raster_pass_count == len(cutting)
    assert all(item.channel_id == "sub" and "Y" in item.axes for item in cutting)
    assert all(item.axes["Z"] == 0 for item in cutting)
    assert {item.status for item in result.checks} == {"passed"}


def test_back_live_face_requires_machine_capability() -> None:
    try:
        compile_back_live_face_draft(
            BackLiveFaceDraftRequest(
                machine_instance_id="l32-back-live-test", operation=_operation(),
                stock_radius_mm=2.815, axial_stock_mm=0.25,
            ),
            _snapshot(with_back_live=False),
        )
    except ValueError as error:
        assert "back_live_tool_milling" in str(error)
    else:
        raise AssertionError("expected back-live capability gate")
